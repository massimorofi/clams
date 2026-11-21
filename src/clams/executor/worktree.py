"""Git worktree management for the executor-runner (§7).

Layout: ``<work_root>/<project_slug>/repo`` holds the base clone; each attempt
gets ``<work_root>/<project_slug>/<task_id>-a<attempt>`` on branch
``clams/<task_id>-a<attempt>``. Tokens are scrubbed from remotes after clone;
pushing uses a credential helper from the environment.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("clams.worktree")


class GitError(Exception):
    pass


def _slug(repo: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", repo)


def _run(args: list[str], cwd: Optional[Path] = None,
         timeout: int = 300) -> str:
    # The runner (root) operates on trees chowned to the agent uid, and the
    # evidence gate runs there too — git's ownership safety check would
    # refuse both. /work is Clams-owned storage, so trust it wholesale.
    import os
    env = {**os.environ,
           "GIT_CONFIG_COUNT": "1",
           "GIT_CONFIG_KEY_0": "safe.directory",
           "GIT_CONFIG_VALUE_0": "*"}
    proc = subprocess.run(args, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True, timeout=timeout,
                          env=env)
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])}... failed {proc.returncode}: "
                       f"{proc.stderr[:400]}")
    return proc.stdout


class WorktreeManager:
    def __init__(self, work_root: str | Path,
                 remote_url_template: str = "https://github.com/{repo}.git",
                 git_env: Optional[dict[str, str]] = None,
                 chown_uid: Optional[int] = None,
                 chown_gid: Optional[int] = None):
        self.work_root = Path(work_root)
        self.remote_url_template = remote_url_template
        self.git_env = git_env or {}
        # The runner provisions worktrees as root but the Claudio-E agent runs
        # as an unprivileged uid (1000 in the published image); without the
        # handoff the agent cannot edit or commit (issue #130 live finding).
        self.chown_uid = chown_uid
        self.chown_gid = chown_gid

    def _chown(self, path: Path) -> None:
        if self.chown_uid is None:
            return
        import os
        try:
            for root, dirs, files in os.walk(path):
                for name in dirs + files:
                    try:
                        os.lchown(os.path.join(root, name),
                                  self.chown_uid, self.chown_gid or -1)
                    except (PermissionError, OSError):
                        pass
            os.lchown(str(path), self.chown_uid, self.chown_gid or -1)
        except (PermissionError, OSError) as exc:
            log.warning("worktree chown to %s failed for %s: %s",
                        self.chown_uid, path, exc)

    # ------------------------------------------------------------- base clone
    def repo_dir(self, repo: str) -> Path:
        return self.work_root / _slug(repo) / "repo"

    def ensure_clone(self, repo: str, base_ref: str = "main") -> Path:
        d = self.repo_dir(repo)
        if not (d / ".git").exists():
            d.parent.mkdir(parents=True, exist_ok=True)
            url = self.remote_url_template.format(repo=repo)
            _run(["git", "clone", "--branch", base_ref, url, str(d)],
                 timeout=600)
        # scrub any token from the remote URL (idempotent, always clean)
        self._scrub_remote(d)
        self._chown(d.parent)
        return d

    def _scrub_remote(self, d: Path) -> None:
        try:
            url = _run(["git", "remote", "get-url", "origin"], cwd=d).strip()
        except GitError:
            return
        clean = re.sub(r"https://[^@]+@", "https://", url)
        if clean != url:
            _run(["git", "remote", "set-url", "origin", clean], cwd=d)

    def fetch(self, repo: str) -> None:
        try:
            _run(["git", "fetch", "--all", "--prune"], cwd=self.repo_dir(repo),
                 timeout=300)
        except GitError as exc:
            log.warning("fetch failed for %s: %s", repo, exc)

    # --------------------------------------------------------------- worktree
    def worktree_path(self, repo: str, task_id: str, attempt: int) -> Path:
        return self.work_root / _slug(repo) / f"{task_id}-a{attempt}"

    def branch_name(self, task_id: str, attempt: int) -> str:
        return f"clams/{task_id}-a{attempt}"

    def add(self, repo: str, task_id: str, attempt: int,
            base_ref: str) -> tuple[Path, str]:
        clone = self.ensure_clone(repo, base_ref)
        path = self.worktree_path(repo, task_id, attempt)
        branch = self.branch_name(task_id, attempt)
        if path.exists():
            self.remove(repo, task_id, attempt)
        # a stale branch from a previous crashed attempt? reset it.
        try:
            _run(["git", "branch", "-D", branch], cwd=clone)
        except GitError:
            pass
        _run(["git", "worktree", "add", str(path), "-b", branch, base_ref],
             cwd=clone)
        self._scrub_remote(path)
        # the agent commits into the shared object store and the worktree's
        # gitdir under the clone — both must be writable by its uid
        self._chown(clone)
        self._chown(path)
        return path, branch

    def remove(self, repo: str, task_id: str, attempt: int) -> None:
        path = self.worktree_path(repo, task_id, attempt)
        clone = self.repo_dir(repo)
        if path.exists():
            try:
                _run(["git", "worktree", "remove", "--force", str(path)],
                     cwd=clone)
            except GitError:
                shutil.rmtree(path, ignore_errors=True)
                try:
                    _run(["git", "worktree", "prune"], cwd=clone)
                except GitError:
                    pass

    def reap_old_failures(self, max_age_hours: int) -> int:
        """Remove failed worktrees older than max_age_hours (runner-side reaper)."""
        n = 0
        cutoff = time.time() - max_age_hours * 3600
        if not self.work_root.exists():
            return 0
        for proj in self.work_root.iterdir():
            if not proj.is_dir():
                continue
            for wt in proj.iterdir():
                if wt.name == "repo" or not wt.is_dir():
                    continue
                try:
                    if wt.stat().st_mtime < cutoff:
                        shutil.rmtree(wt, ignore_errors=True)
                        n += 1
                except OSError:
                    pass
        return n

    # ---------------------------------------------------------------- queries
    def head_commit(self, path: Path) -> str:
        return _run(["git", "rev-parse", "HEAD"], cwd=path).strip()

    def changed_files(self, path: Path, base_ref: str) -> list[str]:
        try:
            base = _run(["git", "merge-base", "HEAD", base_ref],
                        cwd=path).strip()
        except GitError:
            base = base_ref
        out = _run(["git", "diff", "--name-only", f"{base}...HEAD"], cwd=path)
        return [l for l in out.splitlines() if l.strip()]

    def commits_ahead(self, path: Path, base_ref: str) -> int:
        try:
            base = _run(["git", "merge-base", "HEAD", base_ref],
                        cwd=path).strip()
        except GitError:
            base = base_ref
        out = _run(["git", "rev-list", "--count", f"{base}..HEAD"], cwd=path)
        return int(out.strip() or 0)

    def status_porcelain(self, path: Path) -> str:
        return _run(["git", "status", "--porcelain"], cwd=path)

    def push(self, path: Path, branch: str) -> None:
        _run(["git", "push", "-u", "origin", branch], cwd=path, timeout=300)
