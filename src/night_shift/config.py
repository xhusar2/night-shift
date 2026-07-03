"""Config loading and defaults.

Config lives at ~/.config/night-shift/config.toml (XDG aware).
State (weekly bookkeeping, worktrees, logs, reports) lives under
~/.local/state/night-shift/.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    / "night-shift"
    / "config.toml"
)
STATE_DIR = (
    Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    / "night-shift"
)

DEFAULT_PROMPT_TEMPLATE = """\
You are running unattended overnight (night-shift). Work on exactly this task and nothing else:

<task>
{task}
</task>

Rules:
- You are in a dedicated git worktree on branch {branch}; the repository is {repo}.
- Implement the task fully. Run the project's tests/build if available and make them pass for your change.
- Commit your work with clear messages. Do NOT push and do NOT open PRs; the orchestrator handles that.
- If the task is impossible, ambiguous beyond reasonable interpretation, or unsafe, make NO commits and explain why in your final message.
"""


@dataclass
class RepoConfig:
    path: Path
    backlog: str  # relative path to a markdown checklist, or "github:<label>"
    finish: str = "pr"  # pr | branch | none  (ignored when finish_command is set)
    finish_command: str | None = None  # shell command run in the worktree on success
    base: str = "HEAD"  # ref the task branch is created from
    priority: int = 100  # lower = drained first

    @property
    def is_github(self) -> bool:
        return self.backlog.startswith("github:")

    @property
    def github_label(self) -> str:
        return self.backlog.split(":", 1)[1]

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class Config:
    # schedule: weekday -> "HH:MM" wake time (mon..sun)
    schedule: dict[str, str] = field(default_factory=dict)
    # budget
    weekly_agent_percent: float = 40.0  # ceiling of the weekly limit the agent may burn
    session_guard_margin_minutes: int = 30  # hard stop at wake - 5h - margin
    poll_interval_minutes: int = 5  # usage endpoint polling cadence
    require_usage_api: bool = True  # refuse to run if the usage endpoint is unreadable
    # run
    parallel: int = 1
    task_timeout_minutes: int = 90
    max_tasks_per_night: int = 0  # 0 = unlimited
    commit_backlog: bool = False  # commit BACKLOG.md bookkeeping edits to the repo
    claude_args: list[str] = field(default_factory=list)
    prompt_template: str = DEFAULT_PROMPT_TEMPLATE
    repos: list[RepoConfig] = field(default_factory=list)


VALID_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def load_config(path: Path | None = None) -> Config:
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"No config at {path}. Run `night-shift init` to create an example config."
        )
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    cfg = Config()

    sched = raw.get("schedule", {})
    for day, val in sched.items():
        if day not in VALID_DAYS:
            raise ValueError(f"[schedule] unknown day {day!r} (use {'/'.join(VALID_DAYS)})")
        _parse_hhmm(val)  # validate
        cfg.schedule[day] = val

    budget = raw.get("budget", {})
    cfg.weekly_agent_percent = float(budget.get("weekly_agent_percent", cfg.weekly_agent_percent))
    cfg.session_guard_margin_minutes = int(
        budget.get("session_guard_margin_minutes", cfg.session_guard_margin_minutes)
    )
    cfg.poll_interval_minutes = int(budget.get("poll_interval_minutes", cfg.poll_interval_minutes))
    cfg.require_usage_api = bool(budget.get("require_usage_api", cfg.require_usage_api))

    run = raw.get("run", {})
    cfg.parallel = max(1, int(run.get("parallel", cfg.parallel)))
    cfg.task_timeout_minutes = int(run.get("task_timeout_minutes", cfg.task_timeout_minutes))
    cfg.max_tasks_per_night = int(run.get("max_tasks_per_night", cfg.max_tasks_per_night))
    cfg.commit_backlog = bool(run.get("commit_backlog", cfg.commit_backlog))
    cfg.claude_args = list(run.get("claude_args", []))
    cfg.prompt_template = run.get("prompt_template", cfg.prompt_template)

    for entry in raw.get("repos", []):
        if "path" not in entry or "backlog" not in entry:
            raise ValueError("each [[repos]] entry needs `path` and `backlog`")
        repo = RepoConfig(
            path=Path(entry["path"]).expanduser().resolve(),
            backlog=entry["backlog"],
            finish=entry.get("finish", "pr"),
            finish_command=entry.get("finish_command"),
            base=entry.get("base", "HEAD"),
            priority=int(entry.get("priority", 100)),
        )
        if repo.finish not in ("pr", "branch", "none"):
            raise ValueError(f"repo {repo.path}: finish must be pr|branch|none")
        if not (repo.path / ".git").exists():
            raise ValueError(f"repo {repo.path} is not a git repository")
        cfg.repos.append(repo)

    if not cfg.repos:
        raise ValueError("config has no [[repos]] entries; nothing to work on")
    cfg.repos.sort(key=lambda r: r.priority)
    return cfg


def _parse_hhmm(value: str) -> tuple[int, int]:
    try:
        hh, mm = value.split(":")
        h, m = int(hh), int(mm)
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError
        return h, m
    except ValueError:
        raise ValueError(f"invalid time {value!r}, expected HH:MM") from None


EXAMPLE_CONFIG = """\
# night-shift configuration
# Wake times: when you are back at the keyboard. night-shift hard-stops all
# activity at wake - 5h - margin so the 5h session window at wake is 100% full.

[schedule]
mon = "07:00"
tue = "07:00"
wed = "07:00"
thu = "07:00"
fri = "07:00"
sat = "09:00"
sun = "09:00"

[budget]
weekly_agent_percent = 40        # night-shift may burn at most this % of your weekly limit
session_guard_margin_minutes = 30
poll_interval_minutes = 5
require_usage_api = true         # refuse to run when usage can't be read

[run]
parallel = 2                     # concurrent tasks (each in its own worktree)
task_timeout_minutes = 90
max_tasks_per_night = 0          # 0 = unlimited
commit_backlog = false           # commit the BACKLOG.md check-offs to the repo
# claude_args = ["--model", "sonnet"]

[[repos]]
path = "~/Code/myproject"
backlog = "BACKLOG.md"           # markdown checklist: "- [ ] task"
finish = "pr"                    # pr | branch | none
priority = 1

# [[repos]]
# path = "~/Code/other"
# backlog = "github:night-shift" # open GitHub issues with this label
# finish_command = "claude -p '/no-mistakes' --dangerously-skip-permissions"
"""
