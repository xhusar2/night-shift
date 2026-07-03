"""night-shift command line interface."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path

from . import __version__
from .config import DEFAULT_CONFIG_PATH, EXAMPLE_CONFIG, load_config
from .gitwt import branch_merged, list_worktrees, run_git
from .orchestrator import run_night
from .report import REPORTS_DIR, latest_report
from .state import load_state, read_runfile
from .usage import UsageError, fetch_raw, fetch_usage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="night-shift",
        description="Churn through your backlog with Claude Code while you're AFK, "
                    "leaving your 5h session 100% full at wake time.")
    parser.add_argument("--version", action="version", version=f"night-shift {__version__}")
    parser.add_argument("--config", type=Path, default=None,
                        help=f"config file (default: {DEFAULT_CONFIG_PATH})")
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="start a night run (blocks until it stops)")
    p_start.add_argument("--until", metavar="WHEN",
                         help="override wake time: HH:MM, +Nh/+Nm, or 'YYYY-MM-DD HH:MM'")
    p_start.add_argument("--dry-run", action="store_true",
                         help="show the plan (deadline, budget, tasks) without running anything")

    sub.add_parser("status", help="show the current run, if any")
    sub.add_parser("stop", help="gracefully stop the current run")
    p_usage = sub.add_parser("usage", help="show current 5h/weekly utilization")
    p_usage.add_argument("--raw", action="store_true", help="dump the raw endpoint response")
    p_report = sub.add_parser("report", help="print the latest morning report")
    p_report.add_argument("--list", action="store_true", help="list all reports")
    p_clean = sub.add_parser("clean", help="remove night-shift worktrees whose branch is merged")
    p_clean.add_argument("--all", action="store_true",
                         help="remove ALL night-shift worktrees (branches are kept)")
    sub.add_parser("init", help="write an example config if none exists")

    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _dispatch(args) -> int:
    if args.command == "init":
        return _cmd_init(args)
    if args.command == "usage":
        return _cmd_usage(args)
    if args.command == "status":
        return _cmd_status()
    if args.command == "stop":
        return _cmd_stop()
    if args.command == "report":
        return _cmd_report(args)

    cfg = load_config(args.config)
    if args.command == "start":
        return run_night(cfg, until=args.until, dry_run=args.dry_run)
    if args.command == "clean":
        return _cmd_clean(cfg, args)
    return 1


def _cmd_init(args) -> int:
    path = args.config or DEFAULT_CONFIG_PATH
    if path.exists():
        print(f"config already exists at {path}")
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(EXAMPLE_CONFIG)
    print(f"wrote example config to {path} — edit it, then run `night-shift start --dry-run`")
    return 0


def _cmd_usage(args) -> int:
    try:
        if args.raw:
            print(json.dumps(fetch_raw(), indent=2))
            return 0
        u = fetch_usage()
        state = load_state()
        five_reset = f", resets {u.five_hour.resets_at.astimezone():%H:%M}" \
            if u.five_hour.resets_at else ""
        week_reset = f", resets {u.seven_day.resets_at.astimezone():%a %H:%M}" \
            if u.seven_day.resets_at else ""
        print(f"5h window:  {u.five_hour.utilization:.0f}%{five_reset}")
        print(f"weekly:     {u.seven_day.utilization:.0f}%{week_reset}")
        print(f"agent used: {state.get('agent_used_percent', 0.0):.1f}% this week")
        return 0
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _cmd_status() -> int:
    run = read_runfile()
    if run is None:
        print("no night run active")
        return 0
    if run.get("stale"):
        print(f"stale runfile (pid {run.get('pid')} is gone) — last state:")
    finished = run.get("finished", {})
    done = sum(1 for s in finished.values() if s == "done")
    print(f"pid {run.get('pid')}  started {run.get('started_at')}  "
          f"wake {run.get('wake')}  hard stop {run.get('deadline')}")
    print(f"queued {run.get('queued')}  running {', '.join(run.get('running', [])) or '-'}  "
          f"finished {done} done / {len(finished)} total"
          f"{'  [PAUSED: 5h window full]' if run.get('paused') else ''}")
    print(f"agent budget: {run.get('agent_used_percent')}% of "
          f"{run.get('weekly_agent_percent')}% ceiling")
    return 0


def _cmd_stop() -> int:
    run = read_runfile()
    if run is None or run.get("stale"):
        print("no active night run")
        return 1
    os.kill(run["pid"], signal.SIGTERM)
    print(f"sent stop to pid {run['pid']}; it will terminate tasks and write the report")
    return 0


def _cmd_report(args) -> int:
    if args.list:
        if REPORTS_DIR.exists():
            for p in sorted(REPORTS_DIR.glob("*.md")):
                if p.name != "latest.md":
                    print(p)
        return 0
    p = latest_report()
    if p is None:
        print("no reports yet")
        return 1
    print(p.read_text())
    return 0


def _cmd_clean(cfg, args) -> int:
    removed = 0
    for repo in cfg.repos:
        for path, branch in list_worktrees(repo):
            if args.all or branch_merged(repo, branch):
                run_git(repo.path, "worktree", "remove", "--force", str(path), check=False)
                if not args.all:  # merged branch: safe to delete too
                    run_git(repo.path, "branch", "-D", branch, check=False)
                print(f"removed {path} ({branch})")
                removed += 1
    print(f"{removed} worktree(s) removed" if removed else "nothing to clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
