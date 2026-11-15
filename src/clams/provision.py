"""Project provisioning (issue #18).

"Add project" must do more than write a row: it has to hand Clams an actual,
addressable working tree for the new repository so the intake/plan/dispatch
pipeline can operate on it. This module owns that provisioning step — clone,
sync, verify — and the path layout that makes each project's tree predictable.

Layout
    Every project's working tree lives under ``<work_root>/projects/<slug>-<id>``.
    The slug is derived from the project name for human readability; the id is
    appended so two projects that slugify to the same string never collide on
    disk. The id is the durable key; the slug is cosmetic.

Authentication
    Private repos need a token. We embed it in the clone/fetch URL for the
    network operation and then reset ``origin`` to the clean URL so the token
    is never persisted in ``.git/config`` (which gets backed up, copied into
    worktrees, and read by other tools).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit


class ProvisionError(Exception):
    """Raised when a project cannot be cloned or its tree is invalid."""


# A slug is lowercase alphanumerics with single dashes between words.
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(name: str, *, fallback: str = "project") -> str:
    """Turn an arbitrary project name into a filesystem-safe slug.

    ``"Website refresh!"`` -> ``"website-refresh"``. Leading/trailing dashes
    are dropped and an empty result falls back to ``fallback``.
    """
    slug = _SLUG_STRIP.sub("-", (name or "").strip().lower()).strip("-")
    # Collapse runs of dashes produced by the substitution.
    slug = re.sub(r"-{2,}", "-", slug)
    return slug or fallback


def project_worktree_path(work_root: str | Path, project_id: int,
                         name: str) -> Path:
    """The canonical working-tree path for a project.

    ``<work_root>/projects/<slug>-<id>``. The id is always present so the
    path is unique even when two names slugify identically.
    """
    slug = slugify(name)
    return Path(work_root).expanduser().resolve() / "projects" / f"{slug}-{project_id}"


def authed_clone_url(url: str, token: Optional[str]) -> str:
    """Return ``url`` with ``token`` embedded for HTTPS git auth.

    GitHub accepts an OAuth token as the username (``x-access-token``). A URL
    that is not https, or an empty token, is returned unchanged.
    """
    if not token:
        return url
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return url
    host = parts.hostname
    if parts.port:
        host = f"{host}:{parts.port}"
    netloc = f"x-access-token:{token}@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _run_git(args: list[str], cwd: Optional[Path] = None,
            timeout: float = 300.0) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProvisionError(f"git {' '.join(args)} timed out") from exc
    if proc.returncode != 0:
        raise ProvisionError(
            f"git {' '.join(args)} failed ({proc.returncode}): "
            f"{(proc.stderr or '').strip()[:400]}"
        )
    return proc


def is_git_repo(path: str | Path) -> bool:
    """True if ``path`` is a valid git working tree."""
    p = Path(path)
    if not p.exists():
        return False
    proc = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=str(p), capture_output=True, text=True,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def detect_default_branch(path: str | Path) -> str:
    """Best-effort default branch of a cloned repo.

    Prefers ``origin/HEAD`` (set by a normal clone); falls back to the current
    checked-out branch. Returns ``"main"`` only as a last resort.
    """
    proc = subprocess.run(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        cwd=str(path), capture_output=True, text=True,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        # refs/remotes/origin/main -> main
        ref = proc.stdout.strip()
        return ref.rsplit("/", 1)[-1]
    proc = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(path), capture_output=True, text=True,
    )
    branch = proc.stdout.strip() if proc.returncode == 0 else ""
    return branch or "main"


@dataclass
class ProvisionResult:
    worktree_path: str
    default_branch: str
    cloned: bool  # True if a fresh clone was made, False if an existing tree was reused


def clone_repo(url: str, dest: str | Path, *, token: Optional[str] = None,
              timeout: float = 300.0) -> ProvisionResult:
    """Clone ``url`` into ``dest``.

    Idempotent: if ``dest`` already holds a valid git repo it is refreshed
    (``fetch``) instead of re-cloned, so re-provisioning never fails just
    because the tree exists. The clone uses a token-bearing URL for the
    network operation and then resets ``origin`` to the clean URL so the
    token is not written to ``.git/config``.
    """
    dest = Path(dest).expanduser().resolve()
    clean_url = url

    if is_git_repo(dest):
        # Refresh an existing tree rather than re-cloning. An explicit refspec
        # is used because `fetch --all` cannot take a repository argument.
        fetch_url = authed_clone_url(clean_url, token)
        _run_git(["fetch", "--tags", "--prune", fetch_url,
                  "+refs/heads/*:refs/remotes/origin/*"],
                 cwd=dest, timeout=timeout)
        branch = detect_default_branch(dest)
        return ProvisionResult(worktree_path=str(dest),
                              default_branch=branch, cloned=False)

    if dest.exists() and any(dest.iterdir()):
        raise ProvisionError(
            f"destination {dest} exists and is not a git repository; "
            f"refusing to overwrite"
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    clone_url = authed_clone_url(clean_url, token)
    _run_git(["clone", clone_url, str(dest)], cwd=dest.parent, timeout=timeout)

    # Drop the token from the persisted remote URL.
    _run_git(["remote", "set-url", "origin", clean_url], cwd=dest)
    branch = detect_default_branch(dest)
    return ProvisionResult(worktree_path=str(dest),
                          default_branch=branch, cloned=True)


def clean_remote_url(url: str) -> str:
    """Strip any embedded credentials from a git remote URL (issue #125)."""
    parts = urlsplit(url)
    if not parts.username and not parts.password:
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def scrub_remote_tokens(root: str | Path) -> list[str]:
    """Rewrite every ``origin`` under ``root`` that has a token baked in.

    Clones provisioned before the fix stored ``https://x-access-token:ghp_...@
    github.com/...`` in ``.git/config``, and every task worktree inherited it.
    Returns the paths that were rewritten.
    """
    root = Path(root).expanduser()
    fixed: list[str] = []
    if not root.exists():
        return fixed
    candidates = [root, *root.glob("projects/*"), *root.glob("*task-*")]
    for repo in candidates:
        if not ((repo / ".git").exists() or (repo / "config").name == "config"):
            continue
        proc = subprocess.run(["git", "remote", "get-url", "origin"], cwd=str(repo),
                              capture_output=True, text=True)
        if proc.returncode != 0:
            continue
        current = proc.stdout.strip()
        clean = clean_remote_url(current)
        if clean == current:
            continue
        subprocess.run(["git", "remote", "set-url", "origin", clean],
                       cwd=str(repo), capture_output=True, text=True)
        fixed.append(str(repo))
    return fixed


def find_token_bearing_remotes(root: str | Path) -> list[str]:
    """Paths under ``root`` whose origin still embeds credentials."""
    root = Path(root).expanduser()
    found: list[str] = []
    if not root.exists():
        return found
    for repo in [root, *root.glob("projects/*"), *root.glob("*task-*")]:
        proc = subprocess.run(["git", "remote", "get-url", "origin"], cwd=str(repo),
                              capture_output=True, text=True)
        if proc.returncode == 0 and clean_remote_url(proc.stdout.strip()) != proc.stdout.strip():
            found.append(str(repo))
    return found


def sync_repo(worktree_path: str | Path, *, token: Optional[str] = None,
             branch: Optional[str] = None, timeout: float = 300.0) -> str:
    """Bring an existing project working tree up to date with origin.

    Fetches the branch and hard-resets the tree to it. The provisioned tree is
    a mirror Clams reads from, not an operator's working copy, so a hard reset
    is the correct, deterministic way to sync it. Returns the new HEAD sha.
    """
    dest = Path(worktree_path).expanduser().resolve()
    if not is_git_repo(dest):
        raise ProvisionError(f"{dest} is not a git repository")
    if branch is None:
        branch = detect_default_branch(dest)
    fetch_url = authed_clone_url(
        _run_git(["remote", "get-url", "origin"], cwd=dest).stdout.strip(), token)
    _run_git(["fetch", "--prune", fetch_url, f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
             cwd=dest, timeout=timeout)
    _run_git(["reset", "--hard", f"refs/remotes/origin/{branch}"], cwd=dest)
    return _run_git(["rev-parse", "HEAD"], cwd=dest).stdout.strip()


def provision_project(name: str, url: str, work_root: str | Path,
                     *, project_id: int,
                     token: Optional[str] = None,
                     timeout: float = 300.0) -> ProvisionResult:
    """Provision a project's working tree: clone it into the canonical path.

    This is what "Add project" runs so the project is immediately addressable
    by the pipeline. Raises :class:`ProvisionError` if the clone fails.
    """
    dest = project_worktree_path(work_root, project_id, name)
    return clone_repo(url, dest, token=token, timeout=timeout)


def remove_worktree(worktree_path: str | Path) -> None:
    """Delete a provisioned working tree (used when unregistering a project)."""
    dest = Path(worktree_path).expanduser().resolve()
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
