"""workflows repository."""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Optional

from .conn import utcnow_s

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


def bump_replans(conn: sqlite3.Connection, workflow_id: str) -> int:
    with conn.execute(
        "UPDATE workflows SET replans=replans+1, updated_at=? WHERE workflow_id=? "
        "RETURNING replans", (utcnow_s(), workflow_id)) as cur:
        return cur.fetchone()["replans"]


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
