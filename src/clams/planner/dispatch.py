"""Dispatch: promote ready tasks and enqueue their bus_publish (§9.5).

Promotion + claim + outbox enqueue happen in ONE transaction, so a crash can
never leave a task 'dispatched' with no queued message (invariant 2: the DB is
truth; the outbox is the only path to Redis).
"""
from __future__ import annotations

import json
import logging
import sqlite3

from .. import contracts
from ..db import (repo_attempts, repo_events, repo_outbox, repo_skills,
                  repo_tasks, repo_workflows)
from ..db.conn import immediate

log = logging.getLogger("clams.dispatch")


def _skill_snapshot(conn, task_id: str, workflow_id: str,
                    phase: str = "dispatch") -> dict:
    """Stamp which skill versions were in force, into task dispatch/result history.

    Post-mortems must attribute an outcome to the artifact that was live *then*,
    not to whatever is current when the report runs. The rows are written beside
    the dispatch (same transaction as the claim) so a task can never be
    dispatched without its skill context being recorded, and a task with no
    active skills records nothing (an empty record is a real answer: "no skill
    was in force").

    Best-effort by construction: a DB that predates migration 015 must still
    dispatch, so a missing table is skipped rather than raised.
    """
    try:
        active = repo_skills.active_skill_versions(conn)
        if active:
            repo_skills.record_task_skills(
                conn, task_id=task_id, workflow_id=workflow_id, phase=phase,
                active=active)
        return active
    except sqlite3.OperationalError as exc:  # pragma: no cover - old schema
        log.warning("skill attribution skipped for %s: %s", task_id, exc)
        return {}


def record_result_skills(conn, task_id: str, workflow_id: str) -> dict:
    """Record the skill set in force when a task's outcome is handled.

    Exposed for the result-ingest path so 'dispatch' and 'result' rows can be
    compared: a difference means the skill changed underneath the task, which is
    exactly the thing a post-mortem should be told about.
    """
    return _skill_snapshot(conn, task_id, workflow_id, phase="result")


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
            # retry work reuse: tell attempt N>1 why every earlier attempt
            # failed the gate (the executor seeds from the previous branch).
            # Cumulative history from task_attempts (#2); falls back to the
            # last-result summary for tasks that predate the attempt table.
            if claimed["attempt"] > 1:
                fs = repo_attempts.cumulative_retry_summary(conn, task_id)
                if not fs:
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
            # skill attribution, in the same transaction as the claim: the
            # dispatch and the record of what was live are one atomic fact
            active = _skill_snapshot(conn, task_id, claimed["workflow_id"])
            if active:
                repo_events.append_event(
                    conn, "task", task_id, "info",
                    "skills live: " + ",".join(
                        f"{k}@{v['version']}" for k, v in sorted(active.items())))
        dispatched.append(task_id)
    return dispatched


def rebuild_dispatch(conn, bus, settings) -> list[str]:
    """Invariant 2 recovery: re-enqueue dispatched/ready tasks after Redis loss.

    Called at planner start-up: any task stuck in 'ready' or 'dispatched' with
    an expired lease gets a fresh dispatch message (new attempt on re-claim).
    """
    return dispatch_ready(conn, bus, settings)
