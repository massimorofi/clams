"""Project bootstrap for the multi-project pipeline (issue #18).

Two responsibilities:

1. :func:`ensure_default_project` — guarantee that the repo Clams is configured
   to work (``settings.github``) is itself a registered project, so the
   single-repo flow and the multi-project flow share one identity for it.

2. :func:`build_project_contexts` — turn every *additional* registered project
   into a :class:`~clams.orchestrator.ProjectContext` with its own GitHub
   client, intake, planner and review, so the orchestrator can work each
   project independently.
"""
from __future__ import annotations

from typing import Callable, Optional
from urllib.parse import urlsplit

from .config import Settings
from .db import Store
from .gh import GitHubClient
from .intake import IntakeService
from .orchestrator import ProjectContext
from .planner import PlannerService
from .provision import ProvisionError, provision_project
from .review import ReviewService


def repo_url_for(owner: str, repo: str) -> str:
    return f"https://github.com/{owner}/{repo}"


def parse_owner_repo(url: str) -> tuple[str, str]:
    """Extract ``(owner, repo)`` from a github.com repository URL."""
    parts = urlsplit(url)
    segs = [s for s in (parts.path or "").split("/") if s]
    if len(segs) < 2:
        raise ValueError(f"cannot parse owner/repo from {url!r}")
    owner, repo = segs[0], segs[1]
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    return owner, repo


def ensure_default_project(store: Store, settings: Settings) -> int:
    """Ensure the configured repo exists as a project; return its id.

    Provisioning the working tree is best-effort: a clone failure is recorded
    on the project row (so the UI shows it) but does not abort startup — the
    operator can re-sync later.
    """
    default_url = repo_url_for(settings.github.owner, settings.github.repo)
    row = store.projects.find_by_url(default_url)
    if row is None:
        row = store.projects.add(name=settings.github.repo, url=default_url)
    pid = int(row["id"])
    if not row["worktree_path"]:
        try:
            result = provision_project(
                settings.github.repo, default_url, settings.paths.work_root,
                project_id=pid, token=settings.github.token,
            )
            store.projects.set_worktree(pid, result.worktree_path,
                                       result.default_branch)
            store.projects.clear_error(pid)
        except ProvisionError as exc:
            store.projects.set_error(pid, str(exc))
    return pid


def build_project_contexts(store: Store, settings: Settings,
                          llm_call: Callable[[str], str]) -> list[ProjectContext]:
    """Build a :class:`ProjectContext` for every registered project except the
    default repo (which the orchestrator serves with its own single services).

    Each context gets its own GitHub client bound to that project's repo, plus
    intake / planner / review wired to it. A project whose working tree is
    missing is provisioned on the spot (best-effort).
    """
    default_url = repo_url_for(settings.github.owner, settings.github.repo)
    contexts: list[ProjectContext] = []
    for row in store.projects.all():
        pid = int(row["id"])
        url = row["url"]
        if url == default_url:
            continue
        try:
            owner, repo = parse_owner_repo(url)
        except ValueError:
            store.projects.set_error(pid, f"invalid repository URL: {url}")
            continue
        gh = GitHubClient(owner, repo, settings.github.token,
                         settings.github.api_base)
        intake = IntakeService(store, gh, settings, project_id=pid)
        planner = PlannerService(store, gh, settings, llm_call=llm_call,
                                project_id=pid)
        review = ReviewService(store, gh, settings,
                              judge=planner.judge_review, project_id=pid)
        worktree = row["worktree_path"]
        default_branch = row["default_branch"] or "main"
        if not worktree:
            try:
                res = provision_project(row["name"], url,
                                       settings.paths.work_root,
                                       project_id=pid,
                                       token=settings.github.token)
                worktree = res.worktree_path
                default_branch = res.default_branch
                store.projects.set_worktree(pid, worktree, default_branch)
                store.projects.clear_error(pid)
            except ProvisionError as exc:
                store.projects.set_error(pid, str(exc))
                # Never fall back to the orchestrator's own checkout: that is
                # how another repo's tasks ended up running inside the Clams
                # tree (#115). An empty path means "unprovisioned" and the
                # dispatcher skips the project instead.
                worktree = ""
        contexts.append(ProjectContext(
            project_id=pid,
            gh=gh,
            intake=intake,
            planner=planner,
            review=review,
            worktree_path=worktree,
            default_branch=default_branch,
        ))
    return contexts
