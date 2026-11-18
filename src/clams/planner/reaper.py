"""Lease reaper + question expiry (§6, §9.7)."""
from __future__ import annotations

import logging

from ..db import repo_events, repo_questions, repo_tasks
from ..db.conn import immediate

log = logging.getLogger("clams.reaper")


def reap_expired_leases(conn, settings) -> list[str]:
    """dispatched/running with expired lease -> ready (attempt<max) else failed."""
    reaped: list[str] = []
    for row in repo_tasks.expired_leases(conn):
        with immediate(conn):
            r = repo_tasks.requeue_after_lease(conn, row["task_id"])
            if r is not None:
                repo_events.append_event(
                    conn, "task", row["task_id"], "warn",
                    f"lease expired -> {r['status']} (attempt {r['attempt']})")
                reaped.append(row["task_id"])
    return reaped


def expire_questions(conn, settings) -> dict:
    """Open questions past expiry + waiting_input tasks -> blocked."""
    with immediate(conn):
        qids = repo_questions.expire_due(conn)
        tids = repo_tasks.expire_waiting_input(conn, settings.question_expiry_h)
    return {"questions": qids, "tasks": tids}
