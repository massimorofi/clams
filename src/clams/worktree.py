"""Isolated git worktree lifecycle (spec FR-4).

Each task runs in its own worktree under ``/work/task-{id}`` created from an
explicit base SHA, so a task can never see or corrupt another task's tree, and a
failed task leaves nothing behind.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class WorktreeError(Exception):
    pass


@dataclass
class DiffSummary:
    files_changed: list[str]
    insertions: int = 0
    deletions: int = 0
    raw_stat: str = ""

    def as_text(self) -> str:
        if not self.files_changed:
            return "no changes"
        return (
            f"{len(self.files_changed)} file(s) changed, "
            f"{self.insertions} insertion(s), {self.deletions} deletion(s)\n"
            + self.raw_stat
        )


def run_git(args: list[str], cwd: str | Path, timeout: float = 120.0,
            check: bool = True) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorktreeError(f"git {' '.join(args)} timed out") from exc
    if check and proc.returncode != 0:
        raise WorktreeError(
            f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()[:400]}"
        )
    return proc


def ensure_repo(repo_path: str | Path) -> Path:
    p = Path(repo_path).expanduser().resolve()
    if not (p / ".git").exists():
        raise WorktreeError(f"{p} is not a git repository")
    return p


def current_head(repo_path: str | Path) -> str:
    return run_git(["rev-parse", "HEAD"], repo_path).stdout.strip()


def worktree_exists(path: str | Path) -> bool:
    return Path(path).exists()


def create_worktree(repo_path: str | Path, worktree_path: str | Path,
                   branch: str, base_sha: str = "HEAD") -> Path:
    """Create an isolated worktree at ``worktree_path`` on ``branch`` from ``base_sha``.

    If the branch already exists it is reused; otherwise it is created at base_sha.
    """
    repo = ensure_repo(repo_path)
    target = Path(worktree_path).expanduser().resolve()
    if target.exists():
        raise WorktreeError(f"worktree path already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)

    branch_exists = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=str(repo), capture_output=True,
    ).returncode == 0

    if branch_exists:
        run_git(["worktree", "add", str(target), branch], repo)
    else:
        run_git(["worktree", "add", "-b", branch, str(target), base_sha], repo)
    return target


def remove_worktree(repo_path: str | Path, worktree_path: str | Path,
                   force: bool = True) -> None:
    """Remove a worktree and make sure the directory is really gone."""
    repo = ensure_repo(repo_path)
    target = Path(worktree_path).expanduser().resolve()
    if not target.exists():
        return
    args = ["worktree", "remove", str(target)]
    if force:
        args.append("--force")
    proc = run_git(args, repo, check=False)
    if proc.returncode != 0:
        # Fall back to a manual removal + prune so no orphan directory survives.
        shutil.rmtree(target, ignore_errors=True)
        run_git(["worktree", "prune"], repo, check=False)


def prune_worktrees(repo_path: str | Path) -> int:
    """Prune worktrees whose directories were deleted out from under git."""
    repo = ensure_repo(repo_path)
    before = list_worktrees(repo)
    run_git(["worktree", "prune"], repo, check=False)
    after = list_worktrees(repo)
    return max(0, len(before) - len(after))


def list_worktrees(repo_path: str | Path) -> list[dict[str, str]]:
    repo = ensure_repo(repo_path)
    out = run_git(["worktree", "list", "--porcelain"], repo).stdout
    worktrees: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in out.splitlines():
        if not line.strip():
            if current:
                worktrees.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            current = {"path": line[len("worktree "):].strip()}
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):].strip()
        elif line.startswith("branch "):
            current["branch"] = line[len("branch "):].strip()
    if current:
        worktrees.append(current)
    return worktrees


def is_clean(path: str | Path) -> bool:
    proc = run_git(["status", "--porcelain"], path, check=False)
    return proc.stdout.strip() == ""


def diff_summary(path: str | Path, against: str = "HEAD") -> DiffSummary:
    """Files changed in the worktree relative to ``against``.

    Includes both committed changes and uncommitted/untracked work, because an
    executor may finish without committing.
    """
    committed = run_git(
        ["diff", "--numstat", f"{against}...HEAD"], path, check=False
    ).stdout
    uncommitted = run_git(["diff", "--numstat", against], path, check=False).stdout
    untracked = run_git(
        ["ls-files", "--others", "--exclude-standard"], path, check=False
    ).stdout

    files: list[str] = []
    insertions = 0
    deletions = 0
    seen: set[str] = set()

    for blob in (committed, uncommitted):
        for line in blob.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            ins, dele, name = parts[0], parts[1], parts[2]
            if name not in seen:
                seen.add(name)
                files.append(name)
            insertions += int(ins) if ins.isdigit() else 0
            deletions += int(dele) if dele.isdigit() else 0

    for line in untracked.splitlines():
        name = line.strip()
        if name and name not in seen:
            seen.add(name)
            files.append(name)

    raw = run_git(["diff", "--stat", against], path, check=False).stdout.strip()
    return DiffSummary(
        files_changed=files,
        insertions=insertions,
        deletions=deletions,
        raw_stat=raw,
    )


def enforce_scope(worktree_path: str | Path, writable: list[str],
                 forbidden: list[str]) -> list[str]:
    """Return the list of changed files that violate the task's scope.

    Empty list means the worker stayed inside its allowed boundaries.
    """
    changed = diff_summary(worktree_path).files_changed
    violations: list[str] = []
    for f in changed:
        norm = f.strip().lstrip("./")
        if any(_matches(norm, fb) for fb in forbidden):
            violations.append(f)
            continue
        if writable:
            if not any(_matches(norm, w) for w in writable):
                violations.append(f)
    return violations


def _matches(path: str, pattern: str) -> bool:
    """Match a path against a scope entry.

    A trailing ``/`` means "this directory subtree"; otherwise exact file match,
    with a ``*`` glob allowed.
    """
    import fnmatch

    pattern = pattern.strip().lstrip("./")
    if not pattern:
        return False
    if pattern.endswith("/"):
        return path.startswith(pattern)
    if "*" in pattern or "?" in pattern:
        return fnmatch.fnmatch(path, pattern)
    return path == pattern


def cleanup_stale_worktrees(work_root: str | Path, active_paths: set[str]) -> list[str]:
    """Delete task worktrees under ``work_root`` that are not currently active."""
    root = Path(work_root).expanduser().resolve()
    removed: list[str] = []
    if not root.exists():
        return removed
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        if str(entry) in active_paths or entry.name in active_paths:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        removed.append(str(entry))
    return removed
