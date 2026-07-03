"""Git worktree management: one throwaway worktree + branch per task.

Worktrees live under the night-shift state dir, not inside the target repo,
so they never pollute the user's checkout.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .config import STATE_DIR, RepoConfig

WORKTREES_DIR = STATE_DIR / "worktrees"


class GitError(RuntimeError):
    pass


@dataclass
class Worktree:
    repo: RepoConfig
    path: Path
    branch: str
    base_sha: str

    def new_commit_count(self) -> int:
        out = run_git(self.path, "rev-list", "--count", f"{self.base_sha}..HEAD")
        return int(out.strip() or 0)


def run_git(cwd: Path, *args: str, check: bool = True) -> str:
    res = subprocess.run(["git", "-C", str(cwd), *args],
                         capture_output=True, text=True, timeout=300)
    if check and res.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
    return res.stdout


def repo_worktree_root(repo: RepoConfig) -> Path:
    digest = hashlib.sha1(str(repo.path).encode()).hexdigest()[:8]
    return WORKTREES_DIR / f"{repo.name}-{digest}"


def create_worktree(repo: RepoConfig, slug: str) -> Worktree:
    base_sha = run_git(repo.path, "rev-parse", repo.base).strip()
    branch = f"night-shift/{date.today().isoformat()}-{slug}"
    path = repo_worktree_root(repo) / slug
    path.parent.mkdir(parents=True, exist_ok=True)

    # A rerun of the same task reuses nothing: clear leftovers first.
    if path.exists():
        run_git(repo.path, "worktree", "remove", "--force", str(path), check=False)
    run_git(repo.path, "branch", "-D", branch, check=False)

    run_git(repo.path, "worktree", "add", "-b", branch, str(path), base_sha)
    return Worktree(repo=repo, path=path, branch=branch, base_sha=base_sha)


def remove_worktree(wt: Worktree, delete_branch: bool = False) -> None:
    run_git(wt.repo.path, "worktree", "remove", "--force", str(wt.path), check=False)
    if delete_branch:
        run_git(wt.repo.path, "branch", "-D", wt.branch, check=False)


def list_worktrees(repo: RepoConfig) -> list[tuple[Path, str]]:
    """Return (path, branch) for night-shift worktrees of this repo."""
    out = run_git(repo.path, "worktree", "list", "--porcelain", check=False)
    result, path, branch = [], None, None
    for line in out.splitlines() + [""]:
        if line.startswith("worktree "):
            path = Path(line.split(" ", 1)[1])
        elif line.startswith("branch "):
            branch = line.split(" ", 1)[1].removeprefix("refs/heads/")
        elif not line:
            if path and branch and branch.startswith("night-shift/") \
                    and path.is_relative_to(WORKTREES_DIR):
                result.append((path, branch))
            path = branch = None
    return result


def branch_merged(repo: RepoConfig, branch: str) -> bool:
    out = run_git(repo.path, "branch", "--merged", "HEAD", "--format=%(refname:short)",
                  check=False)
    return branch in out.split()
