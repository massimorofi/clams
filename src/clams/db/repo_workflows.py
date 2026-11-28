"""workflows repository."""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Optional

from .conn import utcnow_s
from .repo_events import append_event

WF_STATUSES = ('intake', 'planning', 'running', 'review', 'done',
               'failed', 'blocked', 'paused', 'cancelled')


def create_workflow(conn: sqlite3.Connection, project_id: int, source: str,
                    goal: str, gh_issue_number: Optional[int] = None,
                    workflow_id: Optional[str] = None) -> str:
    wf = workflow_id or str(uuid.uuid4())
    conn.execute(
        "INSERT INTO workflows(workflow_id, project_id, source, gh_issue_number, "
        "goal, status) VALUES(?,?,?,?,?,'intake')",
        (wf, project_id, source, gh_issue_number, goal))
    return wf


def get_workflow(conn: sqlite3.Connection, workflow_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM workflows WHERE workflow_id=?",
                        (workflow_id,)).fetchone()


def find_by_issue(conn: sqlite3.Connection, project_id: int,
                  issue_number: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM workflows WHERE project_id=? AND gh_issue_number=?",
        (project_id, issue_number)).fetchone()


def count_for_project(conn: sqlite3.Connection, project_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM workflows WHERE project_id=?",
        (project_id,)).fetchone()["n"]


def set_status(conn: sqlite3.Connection, workflow_id: str, status: str) -> None:
    if status not in WF_STATUSES:
        raise ValueError(f"bad workflow status {status}")
    conn.execute("UPDATE workflows SET status=?, updated_at=? WHERE workflow_id=?",
                 (status, utcnow_s(), workflow_id))


def set_plan(conn: sqlite3.Connection, workflow_id: str, plan: dict[str, Any],
             status: str = "running") -> None:
    conn.execute(
        "UPDATE workflows SET plan_json=?, status=?, updated_at=? WHERE workflow_id=?",
        (json.dumps(plan), status, utcnow_s(), workflow_id))


def set_plan_json(conn: sqlite3.Connection, workflow_id: str,
                  plan: dict[str, Any]) -> None:
    """Record the plan in force WITHOUT touching the workflow status.

    ``set_plan`` is the planner's entry point (it also flips the workflow to
    'running'); this is the narrow write for a plan applied in place, where the
    status is the caller's business. It matters because
    ``repo_tasks.workflow_outcome`` reads ``plan_json`` to decide which terminal
    failed/blocked rows are superseded, so the column has to track the plan that
    is actually in force wherever a plan is written.
    """
    conn.execute(
        "UPDATE workflows SET plan_json=?, updated_at=? WHERE workflow_id=?",
        (json.dumps(plan), utcnow_s(), workflow_id))


def bump_replans(conn: sqlite3.Connection, workflow_id: str) -> int:
    with conn.execute(
        "UPDATE workflows SET replans=replans+1, updated_at=? WHERE workflow_id=? "
        "RETURNING replans", (utcnow_s(), workflow_id)) as cur:
        return cur.fetchone()["replans"]


def reset_replan_budget(conn: sqlite3.Connection, workflow_id: str, *,
                        reason: str = 'operator_replan') -> int:
    """Zero the replan counter and return the count it was reset from.

    An operator-issued replan is a deliberate override, so the workflow must
    start again with a *full* budget. Without this the counter keeps whatever
    the automated path had already spent: a workflow that reached 'blocked'
    through ``review.review_workflow``'s exhaustion branch is sitting at
    ``max_replans``, so the first poisoned outcome after the override re-enters
    that same branch and blocks immediately with 'replans exhausted' instead of
    actually replanning.

    Only ``replans`` (and ``updated_at``) is written. ``max_replans`` is a
    planner setting (``clams.config.Settings``), never a column on workflows,
    so the cap itself can neither be read nor mutated here. Automated /
    planner-issued replans keep incrementing via ``bump_replans``; this helper
    is for the operator override only.

    Appends one event recording the previous count and the reason.
    Raises ValueError on an unknown workflow.
    """
    row = conn.execute("SELECT replans FROM workflows WHERE workflow_id=?",
                       (workflow_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown workflow {workflow_id}")
    prev = int(row["replans"] or 0)
    conn.execute("UPDATE workflows SET replans=0, updated_at=? "
                 "WHERE workflow_id=?", (utcnow_s(), workflow_id))
    append_event(conn, "workflow", workflow_id, "info",
                 f"replan budget reset (was {prev}): {reason}",
                 {"previous_replans": prev, "replans": 0, "reason": reason})
    return prev


def list_by_status(conn: sqlite3.Connection, *statuses: str) -> list[sqlite3.Row]:
    if not statuses:
        return conn.execute(
            "SELECT * FROM workflows ORDER BY created_at").fetchall()
    marks = ",".join("?" * len(statuses))
    return conn.execute(
        f"SELECT * FROM workflows WHERE status IN ({marks}) ORDER BY created_at",
        statuses).fetchall()


def counts_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) n FROM workflows GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}
