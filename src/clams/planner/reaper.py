"""Lease reaper + question expiry (§6, §9.7)."""
from __future__ import annotations

import logging

from ..db import repo_events, repo_questions, repo_tasks
from ..db.conn import immediate

log = logging.getLogger("clams.reaper")


def reap_expired_leases(conn, settings, *, gh_enabled: bool = False) -> list[str]:
    """dispatched/running with expired lease -> ready (attempt<max) else failed.

    A task failed by exhaustion strands its dependents (they can never reach
    'success' deps again) -> cascade-block + review, same as the result path.
    """
    reaped: list[str] = []
    workflows: set[str] = set()
    for row in repo_tasks.expired_leases(conn):
        with immediate(conn):
            r = repo_tasks.requeue_after_lease(conn, row["task_id"])
            if r is not None:
                repo_events.append_event(
                    conn, "task", row["task_id"], "warn",
                    f"lease expired -> {r['status']} (attempt {r['attempt']})")
                reaped.append(row["task_id"])
                if r["status"] == "failed":
                    workflows.add(r["workflow_id"])
    from . import review as review_mod
    for wf in workflows:
        with immediate(conn):
            review_mod.strand_dependents(conn, wf)
            review_mod.review_workflow(conn, wf, settings,
                                       gh_enabled=gh_enabled)
    return reaped


def expire_questions(conn, settings, *, gh_enabled: bool = False) -> dict:
    """Open questions past expiry + waiting_input tasks -> blocked.

    A blocked task strands its dependents too; the workflow then reviews to
    'blocked' + alert (outcome 'blocked' = needs a human, not a replan).
    """
    with immediate(conn):
        qids = repo_questions.expire_due(conn)
        tids = repo_tasks.expire_waiting_input(conn, settings.question_expiry_h)
    workflows: set[str] = set()
    for tid in tids:
        row = repo_tasks.get_task(conn, tid)
        if row is not None:
            workflows.add(row["workflow_id"])
    from . import review as review_mod
    for wf in workflows:
        with immediate(conn):
            review_mod.strand_dependents(conn, wf)
            review_mod.review_workflow(conn, wf, settings,
                                       gh_enabled=gh_enabled)
    return {"questions": qids, "tasks": tids}
