# night-shift

Churn through your backlog with Claude Code while you're asleep or AFK — and hand you a
**100% full 5-hour session window** the moment you're back.

You tell night-shift when you'll be back and how much of your weekly limit the agent may
burn. It drains task backlogs across your repos in parallel git worktrees, opens draft PRs,
and stops on its own when:

- the backlog is drained,
- your **agent weekly budget** ceiling is reached (or the account weekly limit),
- or the **session guard** fires: all activity hard-stops at `wake − 5h − margin`, so no
  5h window can overlap your wake time — even if you wake a bit early, your session is full.

## Install

```bash
pipx install ~/Code/night-shift      # or: uv tool install ~/Code/night-shift
night-shift init                     # writes ~/.config/night-shift/config.toml
```

Requires Python ≥ 3.11, the `claude` CLI (logged in), `git`, and `gh` (for GitHub
backlogs / draft PRs).

## Usage

```bash
night-shift start --dry-run      # show plan: deadline, budget, tasks found
night-shift start                # run until wake time from [schedule]
night-shift start --until 14:00  # ad-hoc AFK override (also +8h, "2026-07-05 09:00")

night-shift status               # what's running right now
night-shift stop                 # graceful stop (terminates tasks, writes report)
night-shift usage                # 5h / weekly utilization + agent budget used
night-shift report               # print the latest morning report
night-shift clean                # remove worktrees whose branch got merged
```

`start` blocks until the run ends — launch it in tmux, `nohup`, or a systemd-run unit
when you walk away:

```bash
nohup night-shift start >/tmp/night-shift.out 2>&1 &
```

## Configuration

`~/.config/night-shift/config.toml` (see `config.example.toml`):

```toml
[schedule]                        # when you're back at the keyboard
mon = "07:00"
sat = "09:00"                     # days omitted = no default wake; use --until

[budget]
weekly_agent_percent = 40         # ceiling: % of your weekly limit the agent may burn
session_guard_margin_minutes = 30 # hard stop at wake - 5h - margin

[run]
parallel = 2                      # tasks worked concurrently, each in its own worktree
task_timeout_minutes = 90
max_tasks_per_night = 0           # 0 = unlimited
commit_backlog = false            # commit BACKLOG.md check-offs back to the repo

[[repos]]
path = "~/Code/myproject"
backlog = "BACKLOG.md"            # markdown checklist: "- [ ] task text"
finish = "pr"                     # pr | branch | none
priority = 1

[[repos]]
path = "~/Code/other"
backlog = "github:night-shift"    # open issues carrying this label
finish_command = "claude -p '/no-mistakes' --dangerously-skip-permissions"
```

### Backlog sources

- **Markdown** — unchecked `- [ ]` items in the configured file, top to bottom. Finished
  items are checked off with an HTML comment noting the PR/branch; failed items keep their
  checkbox but get a `night-shift: failed` marker and are skipped on later nights (edit the
  line to retry).
- **GitHub issues** — open issues with the configured label. On completion the issue gets a
  comment with the PR link and a `night-shift:done` label (never auto-closed); failures get
  `night-shift:failed`.

### Per-task lifecycle

1. A fresh worktree + branch (`night-shift/<date>-<slug>`) is created from `base` (default `HEAD`).
2. `claude -p` runs the task **with `--dangerously-skip-permissions`** in that worktree and commits.
3. On success, the finish action runs: `pr` pushes and opens a **draft PR**, `branch` just
   pushes, `none` leaves the local worktree, or your custom `finish_command` runs in the worktree.
4. The backlog is marked; failures are recorded and the night moves on. A task interrupted by
   the deadline/budget stays unmarked and is retried the next night.

## How the budget works

Usage comes from the same (unofficial) endpoint Claude Code's `/usage` reads, via your
existing `~/.claude/.credentials.json` OAuth token. While a run is active you're AFK, so
every increase in weekly utilization between polls is attributed to the agent and
accumulated in `~/.local/state/night-shift/state.json`; the counter resets when the weekly
window rolls over. If the 5h window fills mid-run, dispatch pauses until it resets (unless
that's after the deadline, in which case the night ends).

If the endpoint changes shape, `night-shift usage --raw` shows the raw response;
parsing lives in `src/night_shift/usage.py`. Set `budget.require_usage_api = false` to run
without budget tracking (schedule guard still applies).

## Morning report

Written to `~/.local/state/night-shift/reports/` (and `latest.md`): tasks completed with
PR links, failures with reasons, weekly % burned, remaining agent budget, stop reason, and
per-task logs (`~/.local/state/night-shift/logs/`).

## Safety notes

- The agent runs with permissions bypassed, but only inside dedicated worktrees; it is
  instructed never to push — pushes and PRs are done by the orchestrator.
- Nothing is ever committed to your checked-out branch (unless you opt into
  `commit_backlog = true`, which commits only the backlog file bookkeeping).
- `night-shift stop` at any time terminates children gracefully and still writes the report.

## Status

v0.1 — the usage endpoint is unofficial and may change; everything else is plain
git/gh/claude plumbing.
