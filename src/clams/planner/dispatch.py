"""Dispatch: promote ready tasks and enqueue their bus_publish (§9.5).

Promotion + claim + outbox enqueue happen in ONE transaction, so a crash can
never leave a task 'dispatched' with no queued message (invariant 2: the DB is
truth; the outbox is the only path to Redis).
"""
from __future__ import annotations

import json
import logging

from .. import contracts
from ..db import repo_events, repo_outbox, repo_tasks, repo_workflows
from ..db.conn import immediate

log = logging.getLogger("clams.dispatch")


def dispatch_ready(conn, bus, settings) -> list[str]:
    """pending->ready (deps) then ready->dispatched + outbox publish."""
    with immediate(conn):
        promoted = repo_tasks.promote_ready(conn)
        for tid in promoted:
            repo_events.append_event(conn, "task", tid, "info", "ready")
        # A promotion can mean a stranded workflow is live again: an operator
        # retry let unblock_stranded revive cascade-blocked dependents.
        # review_workflow returns early for 'blocked', so without this the
        # workflow could never reach done/replan however many tasks then
        # succeeded (claudio#133).
        for wf_id in {repo_tasks.get_task(conn, tid)["workflow_id"]
                      for tid in promoted}:
            wf = repo_workflows.get_workflow(conn, wf_id)
            if wf is not None and wf["status"] == "blocked":
                repo_workflows.set_status(conn, wf_id, "running")
                repo_events.append_event(conn, "workflow", wf_id, "info",
                                         "reopened: dependents unblocked")
    dispatched: list[str] = []
    for row in repo_tasks.by_status(conn, "ready"):
        task_id = row["task_id"]
        with immediate(conn):
            claimed = repo_tasks.claim_for_dispatch(conn, task_id,
                                                    settings.lease_dispatch_s)
            if claimed is None:
                continue
            brief = json.loads(claimed["brief_json"])
            # operator-edited executor prompt (if any) rides the dispatch so
            # the bus-only executor never reads the DB; absent -> built-in
            # default in the executor
            from ..prompts import get_override
            override = get_override(conn, "executor_task")
            if override:
                brief["prompt_template"] = override
            # retry work reuse: tell attempt N>1 exactly why attempt N-1
            # failed the gate (the executor seeds from its branch)
            if claimed["attempt"] > 1:
                from .plan import failure_summary
                fs = failure_summary(claimed["result_json"],
                                     claimed["error_log"])
                if fs:
                    brief["prior_failure"] = fs
            msg = contracts.TaskDispatch(
                task_id=task_id, workflow_id=claimed["workflow_id"],
                attempt=claimed["attempt"], lease_token=claimed["lease_token"],
                brief=contracts.Brief(**brief))
            # lease_token is unique per dispatch: without it, a retry that
            # resets attempt numbering would dedup against the original
            # publish and the task would never reach the executor.
            repo_outbox.enqueue(
                conn, "bus_publish",
                {"stream": settings.stream_tasks,
                 "msg": json.loads(contracts.dump_message(msg))},
                idempotency_key=f"dispatch:{task_id}:{claimed['attempt']}"
                                f":{claimed['lease_token'][:12]}")
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
