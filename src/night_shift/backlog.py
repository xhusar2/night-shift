"""Backlog sources: markdown checklists and GitHub issues.

Markdown: unchecked items ("- [ ] task") in the configured file. Items that
already carry a "night-shift:" marker (done or failed) are skipped.

GitHub: open issues with the configured label, via the gh CLI. Issues
labelled night-shift:done or night-shift:failed are skipped; issues are
never auto-closed.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import threading
from dataclasses import dataclass
from datetime import date

from .config import Config, RepoConfig

CHECKBOX_RE = re.compile(r"^(\s*[-*]\s*)\[ \](\s+)(.*\S)\s*$")
MARKER = "night-shift:"


@dataclass
class Task:
    repo: RepoConfig
    id: str  # "md:<sha8>" or "gh:<number>"
    title: str
    body: str
    retried: bool = False

    @property
    def slug(self) -> str:
        base = re.sub(r"[^a-z0-9]+", "-", self.title.lower()).strip("-")[:40].strip("-")
        digest = hashlib.sha1(f"{self.repo.path}{self.id}".encode()).hexdigest()[:6]
        return f"{base or 'task'}-{digest}"

    @property
    def prompt_text(self) -> str:
        return f"{self.title}\n\n{self.body}".strip()


def collect_tasks(cfg: Config) -> list[Task]:
    tasks: list[Task] = []
    for repo in cfg.repos:  # already sorted by priority
        if repo.is_github:
            tasks.extend(_github_tasks(repo))
        else:
            tasks.extend(_markdown_tasks(repo))
    return tasks


# --- markdown -----------------------------------------------------------

def _markdown_tasks(repo: RepoConfig) -> list[Task]:
    path = repo.path / repo.backlog
    if not path.exists():
        return []
    tasks = []
    for line in path.read_text().splitlines():
        m = CHECKBOX_RE.match(line)
        if not m or MARKER in line:
            continue
        text = m.group(3)
        task_id = "md:" + hashlib.sha1(text.encode()).hexdigest()[:8]
        tasks.append(Task(repo=repo, id=task_id, title=text, body=""))
    return tasks


def _annotate_markdown(task: Task, done: bool, note: str, commit: bool) -> None:
    path = task.repo.path / task.repo.backlog
    lines = path.read_text().splitlines(keepends=True)
    stamp = f"<!-- {MARKER} {'done' if done else 'failed'} {date.today().isoformat()}"
    if note:
        stamp += f", {note}"
    stamp += " -->"

    file_was_clean = not _git(task.repo, "status", "--porcelain", "--", task.repo.backlog).strip()

    for i, line in enumerate(lines):
        m = CHECKBOX_RE.match(line.rstrip("\n"))
        if m and m.group(3) == task.title and MARKER not in line:
            box = "[x]" if done else "[ ]"
            lines[i] = f"{m.group(1)}{box}{m.group(2)}{m.group(3)} {stamp}\n"
            break
    else:
        return  # item vanished; nothing to annotate
    path.write_text("".join(lines))

    if commit and file_was_clean:
        _git(task.repo, "add", "--", task.repo.backlog)
        _git(task.repo, "commit", "-m",
             f"night-shift: mark task {'done' if done else 'failed'}: {task.title[:60]}",
             "--", task.repo.backlog)


# --- github -------------------------------------------------------------

def _github_tasks(repo: RepoConfig) -> list[Task]:
    out = _gh(repo, "issue", "list", "--label", repo.github_label, "--state", "open",
              "--limit", "100", "--json", "number,title,body,labels")
    if out is None:
        return []
    tasks = []
    for issue in json.loads(out):
        labels = {l["name"] for l in issue.get("labels", [])}
        if f"{MARKER}done" in labels or f"{MARKER}failed" in labels:
            continue
        tasks.append(Task(repo=repo, id=f"gh:{issue['number']}",
                          title=issue["title"], body=issue.get("body") or ""))
    return tasks


def _annotate_github(task: Task, done: bool, note: str) -> None:
    number = task.id.split(":", 1)[1]
    label = f"{MARKER}{'done' if done else 'failed'}"
    body = f"night-shift: {'completed' if done else 'failed'} on {date.today().isoformat()}."
    if note:
        body += f"\n\n{note}"
    _gh(task.repo, "issue", "comment", number, "--body", body)
    color = "0e8a16" if done else "d93f0b"
    _gh(task.repo, "label", "create", label, "--color", color)  # best effort
    _gh(task.repo, "issue", "edit", number, "--add-label", label)


# --- shared -------------------------------------------------------------

# Parallel workers finish concurrently; annotation does read-modify-write on a
# shared file, so it must be serialized or check-offs clobber each other.
_MARK_LOCK = threading.Lock()


def mark_task(task: Task, done: bool, note: str, cfg: Config) -> None:
    try:
        with _MARK_LOCK:
            _mark_task_locked(task, done, note, cfg)
    except Exception:
        pass  # bookkeeping must never take down the run; the report still records it


def _mark_task_locked(task: Task, done: bool, note: str, cfg: Config) -> None:
    if task.repo.is_github:
        _annotate_github(task, done, note)
    else:
        _annotate_markdown(task, done, note, commit=cfg.commit_backlog)


def _git(repo: RepoConfig, *args: str) -> str:
    res = subprocess.run(["git", "-C", str(repo.path), *args],
                         capture_output=True, text=True, timeout=60)
    return res.stdout


def _gh(repo: RepoConfig, *args: str) -> str | None:
    try:
        res = subprocess.run(["gh", *args], cwd=repo.path,
                             capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        return None
    return res.stdout if res.returncode == 0 else None
