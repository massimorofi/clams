"""Apply a (re)plan in place and keep the superseded failures auditable (§8).

Why this module exists
----------------------
A replan used to hard-delete every task row and re-insert it, which re-minted
``task_id``s and made GitHub mint a duplicate sub-issue per replan. The repo
layer (``repo_tasks.reconcile_plan``) now reconciles in place: matched rows are
updated, new rows inserted, and rows the new plan dropped are *cancelled*
instead of deleted.

That fix has a side effect this module is responsible for: a row that was
``failed`` (or ``blocked``) is rewritten to ``cancelled``, so the visible
failure disappears from the tasks table. The audit trail is the only place
left where "this attempt failed, and here is why" survives, so the planner side
must write it while it still holds the pre-flip row.

Contract
--------
``apply_replanned_plan`` appends, inside one transaction:

* one ``task``-scoped ``warn`` event per id reported under
  ``superseded_terminal``, saying the row was superseded by a replan
  (``<old status> -> cancelled``) and carrying the pre-flip evidence
  (previous status, ``error_log`` excerpt, attempt/max_attempts, workflow id);
* one ``workflow``-scoped summary event
  ``replan: reused N, created M, cancelled K`` whose data carries the counts
  plus ``superseded_terminal`` (ids + count). Its level is ``warn`` whenever
  anything was superseded and ``info`` otherwise.

Import-light: ``clams.github`` pulls in the HTTP client and the intake poller,
and the planner imports this module at startup, so the GitHub projection is
resolved lazily with ``__import__`` inside the only function that needs it.
"""
from __future__ import annotations

import contextlib
import logging
from typing import Any, Optional

from ..db import repo_events, repo_tasks, repo_workflows
from ..db.conn import immediate

log = logging.getLogger("clams.planner.reconcile")

#: How much of ``tasks.error_log`` is copied into the supersede event payload.
#: Enough to see the failing command / gate output, small enough that a replan
#: over a dozen superseded rows cannot bloat the events table.
ERROR_LOG_EXCERPT_CHARS = 400


def excerpt(text: Optional[str], limit: int = ERROR_LOG_EXCERPT_CHARS) -> str:
    """First ``limit`` chars of ``text``, whitespace-normalised.

    ``error_log`` is capped at 4000 chars on write; an event payload repeats
    only the head, which is where the failing step is.
    """
    if not text:
        return ""
    flat = " ".join(str(text).split())
    return flat[:limit]


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _supersede_snapshot(conn, workflow_id: str) -> dict[str, Any]:
    """Every current row, keyed by task_id, captured *before* the flip."""
    return {r["task_id"]: r for r in repo_tasks.list_tasks(conn, workflow_id)}


def _supersede_payload(row: Optional[Any], workflow_id: str,
                       task_id: str) -> dict[str, Any]:
    """Pre-flip evidence for one superseded terminal row."""
    if row is None:
        # Defensive: the id came back from reconcile_plan, so the row exists.
        # If it ever does not, still emit the event with what is known.
        return {
            "workflow_id": workflow_id,
            "task_id": task_id,
            "previous_status": "",
            "new_status": "cancelled",
            "reason": "superseded_by_replan",
            "error_log_excerpt": "",
            "attempt": 0,
            "max_attempts": 0,
        }
    return {
        "workflow_id": workflow_id,
        "task_id": task_id,
        "title": row["title"],
        "previous_status": row["status"],
        "new_status": "cancelled",
        "reason": "superseded_by_replan",
        "error_log_excerpt": excerpt(row["error_log"]),
        "attempt": _int(row["attempt"]),
        "max_attempts": _int(row["max_attempts"]),
    }


@contextlib.contextmanager
def _transaction(conn):
    """Own a transaction unless the caller already holds one.

    ``reconcile_plan`` documents that the caller owns the transaction; this
    helper lets ``apply_replanned_plan`` be either the outer writer or a
    participant without nesting ``BEGIN IMMEDIATE`` (which SQLite rejects).
    """
    if getattr(conn, "in_transaction", False):
        yield
    else:
        with immediate(conn):
            yield


def apply_replanned_plan(conn, workflow_id: str, planned: list[dict[str, Any]],
                         *, repo: Optional[str] = None,
                         project: bool = True) -> dict[str, Any]:
    """Reconcile ``planned`` into ``workflow_id`` and record what it overrode.

    Returns the ``reconcile_plan`` result
    (``{'created', 'reused', 'stale', 'superseded_terminal'}``).

    ``repo`` (``"owner/name"``) is optional: when given, the workflow state is
    re-projected to GitHub after the swap (a no-op unless the workflow carries
    a ``gh_issue_number``).
    """
    with _transaction(conn):
        before = _supersede_snapshot(conn, workflow_id)
        result = repo_tasks.reconcile_plan(conn, workflow_id, planned)

        superseded = list(result.get("superseded_terminal") or [])
        if superseded:
            log.info("workflow %s: replan superseded %d terminal task(s): %s",
                     workflow_id, len(superseded), ", ".join(superseded))
        for task_id in superseded:
            row = before.get(task_id)
            payload = _supersede_payload(row, workflow_id, task_id)
            repo_events.append_event(
                conn, "task", task_id, "warn",
                f"superseded by replan: {payload['previous_status']} -> cancelled",
                payload)

        reused = len(result.get("reused") or {})
        created = len(result.get("created") or [])
        cancelled = len(result.get("stale") or [])
        level = "warn" if superseded else "info"
        repo_events.append_event(
            conn, "workflow", workflow_id, level,
            f"replan: reused {reused}, created {created}, cancelled {cancelled}",
            {"reused": reused, "created": created, "cancelled": cancelled,
             "superseded_terminal": {"ids": superseded,
                                     "count": len(superseded)}})

        if project and repo:
            wf = repo_workflows.get_workflow(conn, workflow_id)
            if wf is not None:
                # lazy on purpose: clams.github drags in httpx + the intake
                # poller, and this module is imported by the planner at startup
                projection = __import__("clams.github.projection",
                                        fromlist=["projection"])
                projection.project_workflow_state(conn, workflow_id, repo,
                                                  wf["status"])

    return result
