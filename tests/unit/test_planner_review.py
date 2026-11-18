"""WP4: event application — fencing, idempotency, review/replan/blocked."""
from __future__ import annotations

import json

from clams import contracts
from clams.config import Settings
from clams.db import (repo_events, repo_outbox, repo_tasks, repo_workflows,
                      repo_projects)
from clams.planner.review import apply_event


def _dispatched(db, s, n_tasks=1):
    pid = repo_projects.upsert_project(db, "o/r")
    wf = repo_workflows.create_workflow(db, pid, "adhoc", "g")
    repo_workflows.set_status(db, wf, "running")
    ids = []
    for i in range(1, n_tasks + 1):
        brief = {"objective": "o", "acceptance": "a", "run": ["echo x"],
                 "writable": ["*.py"], "repo": "o/r", "max_wall_s": 1800}
        ids.append(repo_tasks.create_task(db, wf, i, f"t{i}", "o", "a", brief,
                                          max_attempts=s.max_attempts))
    repo_tasks.promote_ready(db)
    out = []
    for t in ids:
        row = repo_tasks.claim_for_dispatch(db, t, 120)
        out.append((t, row["attempt"], row["lease_token"]))
    return wf, out


def _started(db, s, t, a, tok, msg_id="m1"):
    m = contracts.TaskStarted(task_id=t, attempt=a, lease_token=tok,
                              executor_id="e1", session_ref="sess-1")
    return apply_event(db, m, msg_id, s)


def test_started_fencing_and_idempotency(db):
    s = Settings(_env_file=None)
    wf, [(t, a, tok)] = _dispatched(db, s)
    assert _started(db, s, t, a, tok) == "applied"
    assert repo_tasks.get_task(db, t)["status"] == "running"
    # same msg_id again -> dup (no state change)
    assert _started(db, s, t, a, tok, msg_id="m1") == "dup"
    # different msg_id but stale token -> fenced
    m = contracts.TaskStarted(task_id=t, attempt=a, lease_token="old",
                              executor_id="e1", session_ref="x")
    assert apply_event(db, m, "m2", s) == "fenced"


def test_result_success_completes_workflow(db):
    s = Settings(_env_file=None)
    wf, [(t, a, tok)] = _dispatched(db, s)
    _started(db, s, t, a, tok)
    m = contracts.TaskResult(task_id=t, attempt=a, lease_token=tok,
                             outcome="success",
                             evidence={"passed": True},
                             commit_sha="abc", branch="clams/x",
                             session_ref="sess-1")
    assert apply_event(db, m, "m3", s) == "applied"
    assert repo_tasks.get_task(db, t)["status"] == "success"
    assert repo_workflows.get_workflow(db, wf)["status"] == "done"


def test_result_gate_fail_retries(db):
    s = Settings(_env_file=None)
    wf, [(t, a, tok)] = _dispatched(db, s)
    _started(db, s, t, a, tok)
    m = contracts.TaskResult(task_id=t, attempt=a, lease_token=tok,
                             outcome="success",  # agent CLAIMS success...
                             evidence={"passed": False, "reason": "no commit"},
                             log_tail="claims success, gate disagrees")
    apply_event(db, m, "m4", s)
    # ...but the gate decides: back to ready for attempt 2
    assert repo_tasks.get_task(db, t)["status"] == "ready"


def test_question_flow(db):
    s = Settings(_env_file=None)
    wf, [(t, a, tok)] = _dispatched(db, s)
    _started(db, s, t, a, tok)
    q = contracts.TaskQuestion(task_id=t, attempt=a, lease_token=tok,
                               question="which?", options=["a", "b"],
                               idempotency_key="q:t:1")
    assert apply_event(db, q, "m5", s) == "applied"
    assert repo_tasks.get_task(db, t)["status"] == "waiting_input"
    row = db.execute("SELECT * FROM questions").fetchone()
    assert row["question"] == "which?"


def test_failure_replans_then_blocks(db):
    s = Settings(_env_file=None, max_replans=1, max_attempts=1)
    wf, [(t, a, tok)] = _dispatched(db, s)
    _started(db, s, t, a, tok)
    m = contracts.TaskResult(task_id=t, attempt=a, lease_token=tok,
                             outcome="failed", evidence={"passed": False},
                             log_tail="boom")
    apply_event(db, m, "m6", s)
    # task failed (max_attempts=1) -> workflow review -> replan (replans=1)
    assert repo_workflows.get_workflow(db, wf)["status"] == "planning"
    # second failure -> blocked. Re-open the task for a second failure round
    # (operator-style override; the state table keeps 'failed' terminal).
    repo_workflows.set_status(db, wf, "running")
    db.execute("UPDATE tasks SET status='running' WHERE task_id=?", (t,))
    m2 = contracts.TaskResult(task_id=t, attempt=1, lease_token=tok,
                              outcome="failed", evidence={"passed": False})
    apply_event(db, m2, "m8", s)
    assert repo_workflows.get_workflow(db, wf)["status"] == "blocked"


def test_executor_alive_upserts(db):
    s = Settings(_env_file=None)
    m = contracts.ExecutorAlive(executor_id="e9", capacity=2, version="2.0.0")
    assert apply_event(db, m, "m9", s) == "applied"
    row = db.execute("SELECT * FROM executors WHERE executor_id='e9'").fetchone()
    assert row["capacity"] == 2


def test_heartbeat_extends(db):
    s = Settings(_env_file=None)
    wf, [(t, a, tok)] = _dispatched(db, s)
    _started(db, s, t, a, tok)
    hb = contracts.TaskHeartbeat(task_id=t, attempt=a, lease_token=tok,
                                 turns_used=3, elapsed_s=60)
    assert apply_event(db, hb, "m10", s) == "applied"
