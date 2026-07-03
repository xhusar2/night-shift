<p align="center">
  <img src="banner.png" alt="night-shift — a Claude Code utility. Work smarter. Ship after dark." width="100%">
</p>

<p align="center">
  <a href="https://github.com/xhusar2/night-shift/tags"><img alt="Version" src="https://img.shields.io/github/v/tag/xhusar2/night-shift?label=version&color=blueviolet"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white">
  <img alt="Dependencies: none" src="https://img.shields.io/badge/dependencies-none-brightgreen">
  <img alt="Platform: Linux | macOS" src="https://img.shields.io/badge/platform-linux%20%7C%20macos-lightgrey">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green"></a>
  <img alt="Made for Claude Code" src="https://img.shields.io/badge/made%20for-Claude%20Code-d97757">
</p>

<p align="center"><b>Churn through your backlog with Claude Code while you're asleep or AFK —<br>
and wake up to a 100% full 5-hour session window.</b></p>

---

You tell night-shift when you'll be back and how much of your weekly limit the agent may
burn. It drains task backlogs across your repos in parallel git worktrees, opens draft PRs,
and stops on its own when:

- the backlog is drained (or `max_tasks_per_night` is reached),
- your **agent weekly budget** ceiling is reached (or the account weekly limit hits 100%),
- or the **session guard** fires: all activity hard-stops at `wake − 5h − margin`, so no
  5h window can overlap your wake time — even if you wake a bit early, your session is full.

## Requirements

- Python ≥ 3.11 (no third-party dependencies)
- the [`claude` CLI](https://claude.com/claude-code), logged in (night-shift reuses its
  OAuth credentials for usage tracking)
- `git`, and `gh` (only for GitHub-issue backlogs and the `pr` finish action)
- Linux/macOS (credentials are read from `~/.claude/.credentials.json`)

## Install

```bash
git clone https://github.com/xhusar2/night-shift
pipx install ./night-shift           # or: uv tool install ./night-shift
night-shift init                     # writes ~/.config/night-shift/config.toml
```

## Usage

```bash
night-shift start --dry-run      # show the plan: deadline, budget, tasks found
night-shift start                # run until the next wake time from [schedule]
night-shift start --until 14:00  # ad-hoc AFK override (also +8h, +90m, "2026-07-05 09:00")

night-shift status               # what's running right now
night-shift stop                 # graceful stop (terminates tasks, writes the report)
night-shift usage                # 5h / weekly utilization + agent budget used
night-shift usage --raw          # dump the raw usage endpoint response (debugging)
night-shift report               # print the latest morning report
night-shift report --list        # list all reports
night-shift clean                # remove worktrees whose branch got merged (+ the branch)
night-shift clean --all          # remove ALL night-shift worktrees (branches are kept)
```

`start` blocks until the run ends — launch it in tmux, `nohup`, or a systemd-run unit
when you walk away:

```bash
nohup night-shift start >/tmp/night-shift.out 2>&1 &
```

## Configuration

`~/.config/night-shift/config.toml` (see [`config.example.toml`](config.example.toml)).
All keys with their defaults:

```toml
[schedule]                # when you're back at the keyboard, per weekday (mon..sun)
mon = "07:00"             # days omitted = no default wake that day; use --until
sat = "09:00"

[budget]
weekly_agent_percent = 40         # ceiling: % of your weekly limit the agent may burn
session_guard_margin_minutes = 30 # hard stop at wake - 5h - margin
poll_interval_minutes = 5         # how often the usage endpoint is polled
require_usage_api = true          # refuse to start / abort if usage can't be read

[run]
parallel = 1                      # tasks worked concurrently, each in its own worktree
task_timeout_minutes = 90         # per task; timed-out tasks are marked failed
max_tasks_per_night = 0           # 0 = unlimited
commit_backlog = false            # commit BACKLOG.md check-offs back to the repo
claude_args = []                  # extra args for claude, e.g. ["--model", "sonnet"]
# prompt_template = "..."         # override the task prompt; placeholders:
                                  # {task} {branch} {repo}

[[repos]]                         # one entry per repo, drained in priority order
path = "~/Code/myproject"
backlog = "BACKLOG.md"            # markdown checklist, or "github:<label>"
finish = "pr"                     # pr | branch | none (see below)
# finish_command = "..."          # custom shell command instead of `finish`
base = "HEAD"                     # ref task branches are created from (e.g. "origin/main")
priority = 100                    # lower = drained first

[[repos]]
path = "~/Code/other"
backlog = "github:night-shift"    # open issues carrying this label
finish_command = "claude -p '/no-mistakes' --dangerously-skip-permissions"
```

### Backlog sources

- **Markdown** — unchecked `- [ ]` items in the configured file, top to bottom. Finished
  items are checked off with an HTML comment noting the PR/branch; failed items keep their
  checkbox but get a `night-shift: failed` marker and are skipped on later nights (delete
  the marker to retry).
- **GitHub issues** — open issues with the configured label (via `gh`). On completion the
  issue gets a comment with the PR link and a `night-shift:done` label (never auto-closed);
  failures get `night-shift:failed`. Issues already carrying either label are skipped.

### Per-task lifecycle

1. A fresh worktree + branch (`night-shift/<date>-<slug>`) is created from `base`.
   Worktrees live under `~/.local/state/night-shift/worktrees/`, never inside your checkout.
2. `claude -p` runs the task **with `--dangerously-skip-permissions`** in that worktree
   and commits its work (it is instructed not to push).
3. On success (exit 0 **and** at least one new commit), the finish action runs:
   - `pr` — push the branch and open a **draft PR** via `gh`
   - `branch` — just push the branch
   - `none` — leave the local worktree/branch for you to inspect
   - `finish_command` — run your own command in the worktree (non-zero exit = failed)
4. The backlog is marked done/failed and the night moves on. A task interrupted by the
   deadline or budget stays **unmarked** and is picked up again the next night.

## How the budget works

Usage comes from the same (unofficial) endpoint Claude Code's `/usage` reads, using your
existing `~/.claude/.credentials.json` OAuth token. While a run is active you're AFK, so
every increase in weekly utilization between polls is attributed to the agent and
accumulated in `~/.local/state/night-shift/state.json`; the counter resets when the weekly
window rolls over — so `weekly_agent_percent` is a ceiling **across all nights of the week**,
not per night.

If the 5h window fills mid-run, dispatching pauses until it resets and rate-limited tasks
are retried once; if the reset would land after the deadline, the night ends instead.

If the endpoint changes shape, `night-shift usage --raw` shows the raw response; parsing
lives in `src/night_shift/usage.py`. Set `require_usage_api = false` to run without budget
tracking (the session guard still applies — it's pure clock math).

## Morning report

Written to `~/.local/state/night-shift/reports/` (and `latest.md`): tasks completed with
PR links, failures with reasons, weekly % burned by the run, remaining agent budget, stop
reason, and per-task logs under `~/.local/state/night-shift/logs/`.

## Safety notes

- The agent runs with permissions bypassed — point night-shift only at repos where you'd
  accept that. All work happens in dedicated worktrees; pushes and PRs are done by the
  orchestrator, and PRs are always drafts.
- Nothing is ever committed to your checked-out branch (unless you opt into
  `commit_backlog = true`, which commits only the backlog file's bookkeeping edits, and
  only if that file had no uncommitted changes).
- `night-shift stop` (or SIGTERM/Ctrl-C) terminates children gracefully and still writes
  the report.

## Status

v0.1 — works, but the usage endpoint is unofficial and may change without notice;
everything else is plain git/gh/claude plumbing. MIT licensed.
