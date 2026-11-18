"""Dispatch: promote ready tasks and enqueue their bus_publish (§9.5).

Promotion + claim + outbox enqueue happen in ONE transaction, so a crash can
never leave a task 'dispatched' with no queued message (invariant 2: the DB is
truth; the outbox is the only path to Redis).
"""
from __future__ import annotations

import json
import logging

from .. import contracts
from ..db import repo_events, repo_outbox, repo_tasks
from ..db.conn import immediate

log = logging.getLogger("clams.dispatch")


def dispatch_ready(conn, bus, settings) -> list[str]:
    """pending->ready (deps) then ready->dispatched + outbox publish."""
    with immediate(conn):
        promoted = repo_tasks.promote_ready(conn)
        for tid in promoted:
            repo_events.append_event(conn, "task", tid, "info", "ready")
    dispatched: list[str] = []
    for row in repo_tasks.by_status(conn, "ready"):
        task_id = row["task_id"]
        with immediate(conn):
            claimed = repo_tasks.claim_for_dispatch(conn, task_id,
                                                    settings.lease_dispatch_s)
            if claimed is None:
                continue
            brief = json.loads(claimed["brief_json"])
            msg = contracts.TaskDispatch(
                task_id=task_id, workflow_id=claimed["workflow_id"],
                attempt=claimed["attempt"], lease_token=claimed["lease_token"],
                brief=contracts.Brief(**brief))
            repo_outbox.enqueue(
                conn, "bus_publish",
                {"stream": settings.stream_tasks,
                 "msg": json.loads(contracts.dump_message(msg))},
                idempotency_key=f"dispatch:{task_id}:{claimed['attempt']}")
            repo_events.append_event(conn, "task", task_id, "info",
                                     f"dispatched attempt {claimed['attempt']}")
        dispatched.append(task_id)
    return dispatched


def rebuild_dispatch(conn, bus, settings) -> list[str]:
    """Invariant 2 recovery: re-enqueue dispatched/ready tasks after Redis loss.

    Called at planner start-up: any task stuck in 'ready' or 'dispatched' with
    an expired lease gets a fresh dispatch message (new attempt on re-claim).
    """
    return dispatch_ready(conn, bus, settings)
