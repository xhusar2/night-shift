"""Execute one task: worktree -> claude -p -> finish action -> bookkeeping."""

from __future__ import annotations

import json
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .backlog import Task, mark_task
from .config import STATE_DIR, Config
from .gitwt import Worktree, create_worktree, run_git

RATE_LIMIT_HINTS = ("usage limit", "rate limit", "rate_limit", "limit reached", "overloaded")


@dataclass
class TaskResult:
    task: Task
    status: str  # done | failed | rate_limited | interrupted
    note: str = ""
    branch: str = ""
    pr_url: str = ""
    commits: int = 0
    duration_s: float = 0.0
    cost_usd: float | None = None
    log_path: str = ""
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


class ProcessRegistry:
    """Live child processes, so the orchestrator can hard-stop everything."""

    def __init__(self) -> None:
        self._procs: set[subprocess.Popen] = set()
        self._lock = threading.Lock()

    def add(self, p: subprocess.Popen) -> None:
        with self._lock:
            self._procs.add(p)

    def discard(self, p: subprocess.Popen) -> None:
        with self._lock:
            self._procs.discard(p)

    def terminate_all(self) -> None:
        with self._lock:
            procs = list(self._procs)
        for p in procs:
            if p.poll() is None:
                p.terminate()
        deadline = time.monotonic() + 15
        for p in procs:
            if p.poll() is None and time.monotonic() < deadline:
                try:
                    p.wait(timeout=max(0.1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    pass
        for p in procs:
            if p.poll() is None:
                p.kill()


def run_task(task: Task, cfg: Config, stop: threading.Event,
             registry: ProcessRegistry, log_dir: Path) -> TaskResult:
    start = time.monotonic()
    result = TaskResult(task=task, status="failed")
    log_path = log_dir / f"{task.slug}.log"
    result.log_path = str(log_path)
    log_dir.mkdir(parents=True, exist_ok=True)

    try:
        wt = create_worktree(task.repo, task.slug)
    except Exception as e:
        result.note = f"worktree creation failed: {e}"
        mark_task(task, done=False, note=result.note, cfg=cfg)
        return result
    result.branch = wt.branch

    prompt = cfg.prompt_template.format(
        task=task.prompt_text, branch=wt.branch, repo=str(task.repo.path))
    cmd = ["claude", "-p", prompt, "--output-format", "json",
           "--dangerously-skip-permissions", *cfg.claude_args]

    stdout, code, timed_out = _run_child(cmd, wt.path, cfg.task_timeout_minutes * 60,
                                         stop, registry, log_path)
    result.duration_s = time.monotonic() - start

    if stop.is_set():
        result.status = "interrupted"
        result.note = "stopped by orchestrator (deadline or budget); worktree kept"
        return result  # backlog untouched so the task is picked up again next night
    if timed_out:
        result.note = f"timed out after {cfg.task_timeout_minutes} min"
        mark_task(task, done=False, note=result.note, cfg=cfg)
        return result

    payload = _parse_claude_json(stdout)
    final_msg = (payload.get("result") or "") if payload else stdout[-2000:]
    if payload and payload.get("total_cost_usd") is not None:
        result.cost_usd = float(payload["total_cost_usd"])

    lowered = (stdout or "")[-4000:].lower()
    if code != 0 and any(h in lowered for h in RATE_LIMIT_HINTS):
        result.status = "rate_limited"
        result.note = "hit a usage limit mid-task"
        return result  # orchestrator requeues after the window resets

    result.commits = _safe_commits(wt)
    if code == 0 and result.commits > 0:
        ok, finish_note = _finish(task, wt, cfg, stop, registry, log_path)
        result.status = "done" if ok else "failed"
        result.note = finish_note
        if ok and finish_note.startswith("https://"):
            result.pr_url = finish_note
        mark_task(task, done=ok,
                  note=(result.pr_url or f"branch {wt.branch}") if ok else finish_note,
                  cfg=cfg)
    else:
        result.note = _first_line(final_msg) or f"claude exited {code} with no commits"
        mark_task(task, done=False, note=result.note, cfg=cfg)
    return result


def _run_child(cmd: list[str], cwd: Path, timeout_s: float, stop: threading.Event,
               registry: ProcessRegistry, log_path: Path) -> tuple[str, int, bool]:
    """Run a child process; poll so stop/timeout can interrupt it. Returns
    (stdout, returncode, timed_out)."""
    with open(log_path, "a") as log:
        log.write(f"\n=== {datetime.now().isoformat(timespec='seconds')} $ {shlex.join(cmd)}\n")
        log.flush()
        p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=log,
                             text=True, start_new_session=True)
        registry.add(p)
        chunks: list[str] = []
        reader = threading.Thread(target=lambda: chunks.append(p.stdout.read()), daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout_s
        timed_out = False
        try:
            while p.poll() is None:
                if stop.is_set():
                    p.terminate()
                    break
                if time.monotonic() > deadline:
                    timed_out = True
                    p.terminate()
                    break
                time.sleep(2)
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        finally:
            registry.discard(p)
        reader.join(timeout=10)
        stdout = "".join(chunks)
        log.write(stdout)
    return stdout, p.returncode if p.returncode is not None else -1, timed_out


def _parse_claude_json(stdout: str) -> dict:
    try:
        payload = json.loads(stdout.strip())
        return payload if isinstance(payload, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def _safe_commits(wt: Worktree) -> int:
    try:
        return wt.new_commit_count()
    except Exception:
        return 0


def _finish(task: Task, wt: Worktree, cfg: Config, stop: threading.Event,
            registry: ProcessRegistry, log_path: Path) -> tuple[bool, str]:
    repo = task.repo
    if repo.finish_command:
        out, code, timed_out = _run_child(["bash", "-lc", repo.finish_command], wt.path,
                                          cfg.task_timeout_minutes * 60, stop, registry, log_path)
        if code != 0:
            return False, f"finish_command exited {code}: {_first_line(out[-500:])}"
        return True, "finish_command succeeded"

    if repo.finish == "none":
        return True, f"left on branch {wt.branch} (local worktree kept)"

    try:
        run_git(wt.path, "push", "-u", "origin", wt.branch)
    except Exception as e:
        return False, f"push failed: {e}"
    if repo.finish == "branch":
        return True, f"pushed branch {wt.branch}"

    # finish == "pr": draft PR via gh
    body = (f"Automated overnight change by night-shift.\n\nTask: {task.title}\n\n"
            "🤖 Generated with [Claude Code](https://claude.com/claude-code)")
    res = subprocess.run(
        ["gh", "pr", "create", "--draft", "--head", wt.branch,
         "--title", task.title[:120], "--body", body],
        cwd=wt.path, capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        return False, f"pushed {wt.branch} but PR creation failed: {res.stderr.strip()[:300]}"
    url = res.stdout.strip().splitlines()[-1] if res.stdout.strip() else ""
    return True, url or f"draft PR opened for {wt.branch}"


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0].strip() if text.strip() else ""


def night_log_dir(started: datetime) -> Path:
    return STATE_DIR / "logs" / started.strftime("%Y-%m-%d_%H%M")
