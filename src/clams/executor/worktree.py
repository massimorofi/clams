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


def trust_env(base: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Env that disables git's ownership check for /work trees.

    The runner (root) operates on trees chowned to the agent uid, and the
    evidence gate runs brief `run:` commands (which may themselves call
    git) there too — git's safety check would refuse both. /work is
    Clams-owned storage, so trust it wholesale.
    """
    import os
    env = {**(base if base is not None else os.environ),
           "GIT_CONFIG_COUNT": "1",
           "GIT_CONFIG_KEY_0": "safe.directory",
           "GIT_CONFIG_VALUE_0": "*"}
    return env


def _run(args: list[str], cwd: Optional[Path] = None,
         timeout: Optional[int] = None) -> str:
    proc = subprocess.run(args, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True, timeout=timeout,
                          env=trust_env())
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
            _run(["git", "clone", "--branch", base_ref, url, str(d)])
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
            _run(["git", "fetch", "--all", "--prune"], cwd=self.repo_dir(repo))
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
    def ref_exists(self, repo: str, ref: str) -> bool:
        """True when the base clone has `ref` (branch/tag). Used to seed a
        retry from the previous attempt's branch instead of base_ref."""
        d = self.repo_dir(repo)
        if not (d / ".git").exists():
            return False
        try:
            _run(["git", "rev-parse", "--verify", "--quiet", ref], cwd=d)
            return True
        except GitError:
            return False

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
        _run(["git", "push", "-u", "origin", branch], cwd=path)

    # ------------------------------------------------ workflow integration ref
    # The container gitconfig supplies a credential helper but no committer
    # identity, so any merge we make has to carry one explicitly.
    INTEGRATION_IDENTITY = ("clams-integrator", "clams@invalid")

    def integration_branch(self, workflow_id: str) -> str:
        # the planner uses the same helper (contracts.integration_branch) when
        # it asks for the merge, so the two sides cannot drift
        from ..contracts import integration_branch
        return integration_branch(workflow_id)

    def _ref_sha(self, clone: Path, ref: str) -> Optional[str]:
        try:
            sha = _run(["git", "rev-parse", "--verify", "--quiet", ref],
                       cwd=clone).strip()
        except GitError:
            return None
        return sha or None

    def _is_ancestor(self, clone: Path, a: str, b: str) -> bool:
        """True when `a` is reachable from `b`. Exit 1 is a legitimate answer
        here, not an error, so this cannot go through ``_run``."""
        proc = subprocess.run(["git", "merge-base", "--is-ancestor", a, b],
                              cwd=str(clone), capture_output=True, text=True,
                              env=trust_env())
        return proc.returncode == 0

    def integrate(self, repo: str, workflow_id: str, worktree: Path,
                  task_branch: str) -> str:
        """Advance the workflow's integration ref to include `task_branch`, so
        the next task's *first* attempt seeds from it instead of from the
        workflow base ref.

        Pure ref plumbing in the common case: with a single executor slot a
        task branch is a descendant of the tip, so this is one compare-and-swap
        ``update-ref`` and no working tree is touched. Only a genuine
        non-fast-forward (a retry branch predating a tip advance, or a future
        capacity>1) needs a real merge, and that runs in the still-alive task
        worktree rather than a dedicated integration worktree — a per-workflow
        checkout would have no lifecycle owner, since ``reap_old_failures``
        only removes task dirs and the runner never learns "workflow done".

        The ref is local to the clone and never pushed: lose the volume and
        seeding falls back to base_ref, which is exactly the old behaviour.

        Raises on conflict or a lost race; callers must treat that as
        non-fatal (a task that passed the gate stays passed). To start a
        workflow's integration over, delete ``refs/heads/clams/wf/<workflow_id>``.
        """
        clone = self.repo_dir(repo)
        if not (clone / ".git").exists():
            raise GitError(f"no clone for {repo}")
        integ = self.integration_branch(workflow_id)
        task_sha = self._ref_sha(clone, f"refs/heads/{task_branch}")
        if task_sha is None:
            raise GitError(f"task branch {task_branch} not found in clone")
        tip = self._ref_sha(clone, f"refs/heads/{integ}")
        if tip is None:
            # CAS against the empty old value means "create only if absent", so
            # two racers cannot both win; the loser gets a non-zero exit.
            _run(["git", "update-ref", f"refs/heads/{integ}", task_sha, ""],
                 cwd=clone)
            return task_sha
        if tip == task_sha:
            return tip
        if self._is_ancestor(clone, tip, task_sha):
            _run(["git", "update-ref", f"refs/heads/{integ}", task_sha, tip],
                 cwd=clone)
            return task_sha
        # non-ff: merge the tip into the task branch (its tree is clean — the
        # gate just checked it) and advance the ref onto the merge commit.
        name, email = self.INTEGRATION_IDENTITY
        try:
            _run(["git", "-c", f"user.name={name}", "-c", f"user.email={email}",
                  "merge", "--no-edit", integ], cwd=worktree)
        except GitError:
            try:
                _run(["git", "merge", "--abort"], cwd=worktree)
            except GitError:
                pass
            raise
        merged = self.head_commit(worktree)
        _run(["git", "update-ref", f"refs/heads/{integ}", merged, tip],
             cwd=clone)
        return merged

    def merge_to_base(self, repo: str, integration_ref: str,
                      base_ref: str) -> str:
        """Merge `integration_ref` into `base_ref` and push it. Returns the sha
        that was pushed.

        Unlike ``integrate`` this needs a real checkout, so it uses a temporary
        worktree created and removed inside the call — it has an owner, so the
        lifecycle objection to a *standing* integration worktree does not apply.

        Two things that bite: the clone's own checkout already holds `base_ref`,
        so the temp worktree must be `--detach`ed (git refuses to check out a
        branch twice); and it must start from `origin/<base_ref>`, not the local
        branch, because a checked-out local branch is never fast-forwarded by
        ``fetch`` and would silently merge into a stale tip.

        Raises GitError on a conflict or a rejected push (a protected base
        branch, for instance). Nothing changes in that case: the merge is
        aborted and nothing is pushed.
        """
        clone = self.ensure_clone(repo, base_ref)
        self.fetch(repo)
        # merge into the current remote tip, not a possibly-stale local branch
        start = base_ref
        if self._ref_sha(clone, f"refs/remotes/origin/{base_ref}"):
            start = f"origin/{base_ref}"
        path = self.work_root / _slug(repo) / "integrate-base"
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            try:
                _run(["git", "worktree", "prune"], cwd=clone)
            except GitError:
                pass
        try:
            _run(["git", "worktree", "add", "--detach", str(path), start],
                 cwd=clone)
            name, email = self.INTEGRATION_IDENTITY
            try:
                _run(["git", "-c", f"user.name={name}",
                      "-c", f"user.email={email}",
                      "merge", "--no-edit", integration_ref], cwd=path)
            except GitError:
                try:
                    _run(["git", "merge", "--abort"], cwd=path)
                except GitError:
                    pass
                raise
            sha = self.head_commit(path)
            _run(["git", "push", "origin", f"HEAD:refs/heads/{base_ref}"],
                 cwd=path)
            return sha
        finally:
            try:
                _run(["git", "worktree", "remove", "--force", str(path)],
                     cwd=clone)
            except GitError:
                shutil.rmtree(path, ignore_errors=True)
                try:
                    _run(["git", "worktree", "prune"], cwd=clone)
                except GitError:
                    pass
