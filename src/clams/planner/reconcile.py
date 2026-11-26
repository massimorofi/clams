"""Non-destructive replan: fold a new plan into the existing task set.

Both replan entry points (``clams.planner.plan.plan_workflow`` and
``clams.planner.commands._apply_plan``) used to delete every task of a
workflow and re-insert the new plan. That minted a brand-new ``task_id`` on
every replan, so the outbox key ``gh:gh_subissue:<task_id>:created`` looked
unseen and GitHub issued a duplicate sub-issue for work that already had one.

This module is the single place that turns a *planned* list of plain dicts into
DB writes: it delegates the identity-preserving merge to
``repo_tasks.reconcile_plan``, appends one summary event, and hands the
superseded task ids to the GitHub projection so their sub-issues get closed.

Deliberately stdlib-only: it imports nothing from pydantic, ``clams.github``
or ``clams.planner.plan`` (the latter would import the planner's LLM/prompt
machinery into every caller of a replan). Callers build the ``brief`` dict
themselves (``plan.build_brief``) and pass plain dicts in. The one exception is
the projection call below, which is resolved at *call* time (``__import__``,
not an ``import`` statement) so this module keeps its import-time shape while a
replan still mirrors cancellations to GitHub.

Entry dict keys understood by ``reconcile_plan``:
    title, objective, acceptance, brief, depends_on, max_attempts,
    sequence_index (optional - see ``_with_sequence_indices``).
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from ..db import repo_events, repo_tasks

__all__ = ["apply_replanned_plan"]


def _with_sequence_indices(
        planned: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give every entry a 1-based ``sequence_index`` in plan order.

    ``tasks`` has UNIQUE(workflow_id, sequence_index) and ``depends_on``
    values *are* sequence indices, so the ladder must be exactly 1..N with no
    duplicates. ``reconcile_plan`` renumbers in two passes (matched rows
    first, then fresh ones) and resolves a collision by parking the current
    holder on a scratch index - correct but it would scatter the plan across
    negative slots and invert the intended order. Assigning the index here,
    from the plan's own order, means each entry lands on the slot its
    dependencies point at, and reconcile_plan still vacates/parks whatever
    stale row was sitting on it.
    """
    out: list[dict[str, Any]] = []
    for i, entry in enumerate(planned or [], start=1):
        e = dict(entry)
        if e.get("sequence_index") is None:
            e["sequence_index"] = i
        out.append(e)
    return out


def _project_superseded(conn: sqlite3.Connection, workflow_id: str,
                        stale: list[str]) -> int:
    """Close the sub-issues of the tasks this replan superseded (best effort).

    ``clams.github.projection`` is resolved at *call* time (``__import__``
    rather than an ``import`` statement) so this module keeps the import-light
    shape asserted by ``tests/unit/test_planner_reconcile.py`` while a replan
    still mirrors its cancellations to GitHub - an ``import clams.github`` here
    would pull ``httpx`` into every caller of a replan.

    Every failure is swallowed on purpose: a workflow with no
    ``gh_issue_number``, no project row, or no GitHub config at all must not
    turn an already-committed replan into a failed command. Returns the number
    of outbox rows enqueued (0 when there is nothing to mirror).
    """
    if not stale:
        return 0
    try:
        projection = __import__("clams.github.projection",
                                fromlist=["project_tasks_superseded"])
        return projection.project_tasks_superseded(conn, workflow_id, "",
                                                   stale)
    except Exception:  # noqa: BLE001 - mirroring must never fail a replan
        return 0


def apply_replanned_plan(conn: sqlite3.Connection, workflow_id: str,
                         planned: list[dict[str, Any]],
                         actor: Optional[str] = None) -> dict[str, Any]:
    """Reconcile ``planned`` into ``workflow_id`` and log one summary event.

    Never deletes a task: matched titles are updated in place (keeping
    ``task_id`` / ``gh_issue_number`` / ``status``), unmatched titles are
    inserted, and titles that disappeared are reported as cancelled.

    ``actor`` (e.g. the issuer of an apply_plan command) is recorded in the
    event payload for attribution; it does not change the summary message.

    The caller owns the transaction (wrap in ``conn.immediate``), exactly as
    for ``repo_tasks.reconcile_plan``.

    Returns::

        {'created': [task_id, ...],      # brand new rows
         'reused': {plan_key: task_id},  # updated in place
         'cancelled': [task_id, ...],    # superseded, status -> cancelled
         'reused_count': int, 'created_count': int, 'cancelled_count': int,
         'stale': [task_id, ...]}        # alias of 'cancelled'
    """
    entries = _with_sequence_indices(planned)
    result: dict[str, Any] = repo_tasks.reconcile_plan(conn, workflow_id,
                                                       entries)
    created = list(result.get("created") or [])
    reused = dict(result.get("reused") or {})
    cancelled = list(result.get("stale") or [])

    # One timeline line per replan, so the UI shows what the swap did instead
    # of the old "replaced N tasks with M" (which said nothing about how much
    # work was actually re-used). Who asked is kept in data, not in the
    # message, so the summary line stays machine-comparable.
    data: dict[str, Any] = {"reused": len(reused), "created": len(created),
                            "cancelled": len(cancelled),
                            "created_ids": created,
                            "cancelled_ids": cancelled}
    if actor:
        data["actor"] = actor
    repo_events.append_event(
        conn, "workflow", workflow_id,
        "warn" if cancelled else "info",
        f"replan: reused {len(reused)}, created {len(created)}, "
        f"cancelled {len(cancelled)}",
        data)

    # GitHub mirror: a cancelled task keeps its row *and* its sub-issue number,
    # so the sub-issue has to be closed explicitly or it sits open on the board
    # claiming work the replan dropped. Guarded - projection is best-effort and
    # a workflow without gh_issue_number / project config must not raise here.
    _project_superseded(conn, workflow_id, cancelled)

    return {"created": created, "reused": reused, "cancelled": cancelled,
            "stale": cancelled, "reused_count": len(reused),
            "created_count": len(created), "cancelled_count": len(cancelled)}


