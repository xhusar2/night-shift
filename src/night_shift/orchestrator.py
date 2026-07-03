"""The night run: dispatch tasks in parallel, watch the clock and the budget.

Stop conditions (whichever comes first):
  * backlog drained (or max_tasks_per_night reached)
  * hard deadline: wake - 5h - margin  -> running tasks are terminated
  * agent weekly ceiling reached       -> running tasks are terminated
  * account weekly limit reached (100%)
  * manual stop (SIGTERM/SIGINT via `night-shift stop`)

If the 5h window fills up mid-run, dispatching pauses until it resets
(rate-limited tasks are requeued once); if it resets only after the
deadline, the run ends.
"""

from __future__ import annotations

import signal
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone

from . import report as report_mod
from .backlog import Task, collect_tasks
from .config import Config
from .runner import ProcessRegistry, TaskResult, night_log_dir, run_task
from .schedule import resolve_wake, work_deadline
from .state import clear_runfile, load_state, rollover_week, save_state, write_runfile
from .usage import Usage, UsageError, fetch_usage


def run_night(cfg: Config, until: str | None, dry_run: bool = False) -> int:
    started = datetime.now()
    wake = resolve_wake(cfg, until, started)
    deadline = work_deadline(wake, cfg)

    if deadline <= started:
        print(f"Nothing to do: wake is {wake:%a %H:%M}, so the session guard deadline "
              f"({deadline:%a %H:%M}) is already past. A night run needs >5h+margin of AFK time.")
        return 1

    tasks = collect_tasks(cfg)
    usage, usage_err = _try_usage()
    state = load_state()
    if usage:
        rollover_week(state, usage)

    if dry_run:
        _print_plan(cfg, tasks, wake, deadline, usage, usage_err, state)
        return 0

    if not tasks:
        print("Backlog is empty across all repos; nothing to do.")
        return 0
    if usage is None and cfg.require_usage_api:
        print(f"Refusing to start: usage endpoint unreadable ({usage_err}).\n"
              "Fix credentials or set budget.require_usage_api = false.")
        return 1
    if usage and state["agent_used_percent"] >= cfg.weekly_agent_percent:
        print(f"Agent weekly budget already spent "
              f"({state['agent_used_percent']:.1f}% >= {cfg.weekly_agent_percent}%).")
        return 1
    if usage and usage.seven_day.utilization >= 100:
        print("Weekly limit already exhausted; nothing night-shift can do.")
        return 1

    print(f"night-shift: {len(tasks)} task(s), wake {wake:%a %H:%M}, "
          f"hard stop {deadline:%a %H:%M}, parallel {cfg.parallel}, "
          f"agent budget {state['agent_used_percent']:.1f}/{cfg.weekly_agent_percent:.0f}% weekly.")

    ctrl = {"reason": None, "pause_until": None}
    stop = threading.Event()
    pause = threading.Event()
    registry = ProcessRegistry()
    save_state(state)

    def request_stop(reason: str, terminate: bool) -> None:
        if ctrl["reason"] is None:
            ctrl["reason"] = reason
        stop.set()
        if terminate:
            registry.terminate_all()

    def on_signal(signum, frame):
        request_stop("manual stop", terminate=True)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    monitor = threading.Thread(
        target=_monitor, daemon=True,
        args=(cfg, deadline, state, usage, stop, pause, ctrl, request_stop))
    monitor.start()

    results = _dispatch(cfg, tasks, stop, pause, registry, started, wake, deadline, state, ctrl)

    if ctrl["reason"] is None:
        ctrl["reason"] = "backlog drained"
    end_usage, _ = _try_usage()
    save_state(state)
    path = report_mod.write_report(cfg, started, wake, deadline, ctrl["reason"],
                                   results, usage, end_usage, state)
    clear_runfile()

    done = sum(1 for r in results if r.status == "done")
    failed = sum(1 for r in results if r.status == "failed")
    print(f"night-shift finished: {ctrl['reason']}. "
          f"{done} done, {failed} failed, {len(results) - done - failed} other.")
    print(f"Report: {path}")
    return 0


def _dispatch(cfg: Config, tasks: list[Task], stop: threading.Event, pause: threading.Event,
              registry: ProcessRegistry, started: datetime, wake: datetime,
              deadline: datetime, state: dict, ctrl: dict) -> list[TaskResult]:
    queue: deque[Task] = deque(tasks)
    results: list[TaskResult] = []
    launched = 0
    log_dir = night_log_dir(started)

    with ThreadPoolExecutor(max_workers=cfg.parallel) as pool:
        futures: dict = {}
        while queue or futures:
            if stop.is_set() and not futures:
                break
            max_hit = cfg.max_tasks_per_night and launched >= cfg.max_tasks_per_night
            while (queue and len(futures) < cfg.parallel and not stop.is_set()
                   and not pause.is_set() and not max_hit):
                task = queue.popleft()
                futures[pool.submit(run_task, task, cfg, stop, registry, log_dir)] = task
                launched += 1
                max_hit = cfg.max_tasks_per_night and launched >= cfg.max_tasks_per_night

            if max_hit and not futures and not stop.is_set():
                ctrl["reason"] = ctrl["reason"] or "max_tasks_per_night reached"
                break
            if not futures:
                time.sleep(3)  # paused with nothing running
                continue

            done, _ = wait(list(futures), timeout=10, return_when=FIRST_COMPLETED)
            for fut in done:
                task = futures.pop(fut)
                try:
                    res = fut.result()
                except Exception as e:  # defensive: a task crash must not kill the night
                    res = TaskResult(task=task, status="failed", note=f"runner crashed: {e}")
                results.append(res)
                print(f"[{datetime.now():%H:%M}] {res.status:>12}  {task.slug}  {res.note[:100]}")
                if res.status == "rate_limited" and not task.retried:
                    task.retried = True
                    queue.append(task)  # retried once the window resets

            write_runfile({
                "started_at": started.isoformat(timespec="seconds"),
                "wake": wake.isoformat(timespec="seconds"),
                "deadline": deadline.isoformat(timespec="seconds"),
                "queued": len(queue),
                "running": [futures[f].slug for f in futures],
                "finished": {r.task.slug: r.status for r in results},
                "paused": pause.is_set(),
                "agent_used_percent": round(state.get("agent_used_percent", 0.0), 2),
                "weekly_agent_percent": cfg.weekly_agent_percent,
                "stop_reason": ctrl["reason"],
            })
    return results


def _monitor(cfg: Config, deadline: datetime, state: dict, initial: Usage | None,
             stop: threading.Event, pause: threading.Event, ctrl: dict,
             request_stop) -> None:
    last_util = initial.seven_day.utilization if initial else None
    next_poll = time.monotonic()  # poll immediately-ish, then every interval
    errors = 0

    while not stop.is_set():
        now = datetime.now()
        if now >= deadline:
            request_stop("session-guard deadline (wake - 5h - margin)", terminate=True)
            return
        resume_at = ctrl.get("pause_until")
        if pause.is_set() and resume_at and datetime.now(timezone.utc) >= resume_at:
            pause.clear()
            ctrl["pause_until"] = None
            print("[monitor] 5h window reset; resuming dispatch.")

        if time.monotonic() >= next_poll:
            next_poll = time.monotonic() + cfg.poll_interval_minutes * 60
            try:
                usage = fetch_usage()
                errors = 0
            except UsageError as e:
                errors += 1
                if cfg.require_usage_api and errors >= 3:
                    request_stop(f"usage endpoint failing repeatedly ({e})", terminate=True)
                    return
                usage = None
            if usage:
                rollover_week(state, usage)
                util = usage.seven_day.utilization
                if last_util is not None:
                    state["agent_used_percent"] += max(0.0, util - last_util)
                last_util = util
                save_state(state)

                if state["agent_used_percent"] >= cfg.weekly_agent_percent:
                    request_stop(
                        f"agent weekly budget reached "
                        f"({state['agent_used_percent']:.1f}% >= {cfg.weekly_agent_percent}%)",
                        terminate=True)
                    return
                if util >= 100:
                    request_stop("account weekly limit reached (100%)", terminate=True)
                    return
                if usage.five_hour.utilization >= 100:
                    resets = usage.five_hour.resets_at
                    if resets and resets.astimezone().replace(tzinfo=None) >= deadline:
                        request_stop("5h window exhausted and only resets after the deadline",
                                     terminate=False)
                        return
                    if not pause.is_set():
                        when = f" until {resets.astimezone():%H:%M}" if resets else ""
                        print(f"[monitor] 5h window full; pausing dispatch{when}.")
                    pause.set()
                    ctrl["pause_until"] = resets
        stop.wait(timeout=15)


def _try_usage() -> tuple[Usage | None, str]:
    try:
        return fetch_usage(), ""
    except UsageError as e:
        return None, str(e)


def _print_plan(cfg: Config, tasks: list[Task], wake: datetime, deadline: datetime,
                usage: Usage | None, usage_err: str, state: dict) -> None:
    hours = (deadline - datetime.now()).total_seconds() / 3600
    print(f"wake:      {wake:%a %Y-%m-%d %H:%M}")
    print(f"hard stop: {deadline:%a %Y-%m-%d %H:%M}  ({hours:.1f}h of work time)")
    if usage:
        print(f"usage:     5h {usage.five_hour.utilization:.0f}%  |  "
              f"week {usage.seven_day.utilization:.0f}%  |  "
              f"agent {state['agent_used_percent']:.1f}/{cfg.weekly_agent_percent:.0f}%")
    else:
        print(f"usage:     UNAVAILABLE ({usage_err})")
    print(f"tasks ({len(tasks)}):")
    for t in tasks:
        print(f"  [{t.repo.name}] {t.id}  {t.title[:80]}")
    if not tasks:
        print("  (backlog empty)")
