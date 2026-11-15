"""Per-project status & progress derivation (issue #18).

A project's lifecycle status is *derived* from the work items that belong to
it, never set independently — the same principle that keeps the Projects V2
board a projection of the label rather than a second source of truth.

Mapping (work-item status -> project status)
    no items                 -> Queued
    any item in review       -> In review
    all items done           -> Done
    anything else active     -> Running
    operator-paused          -> Paused (overrides the derived state)

Progress is the fraction of the project's work items that have reached
``done`` (0-100). A project with no work items is 0%.
"""
from __future__ import annotations

from typing import Iterable, Optional

from .db import Store, WorkItem

# Work-item statuses that mean "this requirement is finished".
DONE_STATUSES = frozenset({"done"})
# Statuses that mean "this requirement is awaiting review".
REVIEW_STATUSES = frozenset({"review"})


def compute_project_status(work_items: Iterable[WorkItem],
                          paused: bool = False) -> tuple[str, int]:
    """Derive ``(status, progress)`` from a project's work items.

    ``paused`` is the operator-controlled flag; when set it overrides the
    derived state so the card shows Paused even while claims are still live.
    """
    items = list(work_items)
    if paused:
        return "Paused", 0
    total = len(items)
    if total == 0:
        return "Queued", 0
    done = sum(1 for w in items if (w.status or "") in DONE_STATUSES)
    review = sum(1 for w in items if (w.status or "") in REVIEW_STATUSES)
    progress = int(round(done / total * 100))
    if done == total:
        return "Done", 100
    if review > 0:
        return "In review", progress
    return "Running", progress


def _project_paused(store: Store, project_id: int) -> bool:
    row = store.projects.get(project_id)
    return bool(row and row["paused"])


def refresh_project_status(store: Store, project_id: int) -> tuple[str, int]:
    """Recompute and persist one project's status + progress.

    Reads the project's work items, derives the status, and writes both the
    status and the progress back to the ``projects`` row. Returns the derived
    ``(status, progress)``.
    """
    items = [w for w in store.work_items.all() if w.project_id == project_id]
    paused = _project_paused(store, project_id)
    status, progress = compute_project_status(items, paused=paused)
    store.projects.set_status(project_id, status)
    store.projects.set_progress(project_id, progress)
    return status, progress


def refresh_all_project_statuses(store: Store) -> dict[int, tuple[str, int]]:
    """Recompute and persist status + progress for every registered project.

    Returns a mapping of ``project_id -> (status, progress)``.
    """
    out: dict[int, tuple[str, int]] = {}
    for row in store.projects.all():
        pid = int(row["id"])
        out[pid] = refresh_project_status(store, pid)
    return out
