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
                 "writable": ["*.py"], "repo": "o/r"}
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


def test_failed_task_blocks_dependents_and_replans(db):
    """Regression (workflow 78013ea5 shape): task1 fails with dependents
    pending -> dependents blocked, workflow reaches review -> replan."""
    s = Settings(_env_file=None, max_attempts=1)
    pid = repo_projects.upsert_project(db, "o/r")
    wf = repo_workflows.create_workflow(db, pid, "adhoc", "g")
    repo_workflows.set_status(db, wf, "running")
    brief = {"objective": "o", "acceptance": "a", "run": ["echo x"],
             "writable": ["*.py"], "repo": "o/r"}
    t1 = repo_tasks.create_task(db, wf, 1, "t1", "o", "a", brief,
                                max_attempts=1)
    t2 = repo_tasks.create_task(db, wf, 2, "t2", "o", "a", brief,
                                depends_on=[1], max_attempts=1)
    t3 = repo_tasks.create_task(db, wf, 3, "t3", "o", "a", brief,
                                depends_on=[2], max_attempts=1)
    repo_tasks.promote_ready(db)
    row = repo_tasks.claim_for_dispatch(db, t1, 120)
    a, tok = row["attempt"], row["lease_token"]
    _started(db, s, t1, a, tok)
    m = contracts.TaskResult(task_id=t1, attempt=a, lease_token=tok,
                             outcome="failed", evidence={"passed": False},
                             log_tail="boom")
    assert apply_event(db, m, "m20", s) == "applied"
    assert repo_tasks.get_task(db, t1)["status"] == "failed"
    assert repo_tasks.get_task(db, t2)["status"] == "blocked"
    assert repo_tasks.get_task(db, t3)["status"] == "blocked"
    # all terminal now -> review -> replan (replans < max)
    assert repo_workflows.get_workflow(db, wf)["status"] == "planning"
    msgs = [r["message"] for r in db.execute(
        "SELECT message FROM events WHERE scope='task'").fetchall()]
    assert any("dependency failed" in m2 for m2 in msgs)


def test_blocked_only_workflow_blocks_with_alert(db):
    """Question-expiry shape: blocked but no failed -> workflow blocked +
    alert, NOT a replan (a human must answer, replanning discards work)."""
    from clams.db import repo_alerts
    from clams.planner.review import review_workflow
    s = Settings(_env_file=None)
    pid = repo_projects.upsert_project(db, "o/r")
    wf = repo_workflows.create_workflow(db, pid, "adhoc", "g")
    repo_workflows.set_status(db, wf, "running")
    brief = {"objective": "o", "acceptance": "a", "run": ["echo x"],
             "writable": ["*.py"], "repo": "o/r"}
    t1 = repo_tasks.create_task(db, wf, 1, "t1", "o", "a", brief)
    t2 = repo_tasks.create_task(db, wf, 2, "t2", "o", "a", brief)
    for t in (t1,):
        repo_tasks.transition(db, t, "ready")
        repo_tasks.transition(db, t, "dispatched")
        repo_tasks.transition(db, t, "running")
        repo_tasks.transition(db, t, "success")
    repo_tasks.transition(db, t2, "blocked")
    review_workflow(db, wf, s)
    assert repo_workflows.get_workflow(db, wf)["status"] == "blocked"
    assert repo_alerts.open_count(db) == 1
    assert repo_workflows.get_workflow(db, wf)["replans"] == 0


def test_review_skips_workflow_mid_planning(db):
    from clams.planner.review import review_workflow
    s = Settings(_env_file=None)
    pid = repo_projects.upsert_project(db, "o/r")
    wf = repo_workflows.create_workflow(db, pid, "adhoc", "g")
    repo_workflows.set_status(db, wf, "planning")
    brief = {"objective": "o", "acceptance": "a", "run": ["echo x"],
             "writable": ["*.py"], "repo": "o/r"}
    t = repo_tasks.create_task(db, wf, 1, "t1", "o", "a", brief)
    repo_tasks.transition(db, t, "ready")
    repo_tasks.transition(db, t, "dispatched")
    repo_tasks.transition(db, t, "running")
    repo_tasks.transition(db, t, "success")
    review_workflow(db, wf, s)   # must not touch a workflow mid-replan
    assert repo_workflows.get_workflow(db, wf)["status"] == "planning"


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


def test_stall_probe_alerts_without_killing(db_path):
    """Alive-but-stuck: heartbeats arrive, turns don't move -> alert + event,
    task stays running (the planner waits; a human decides)."""
    import json as _json
    from clams.bus import InMemoryBus
    from clams.db import repo_alerts, connect as _connect, migrate as _migrate
    from clams.db.conn import plus_s
    from clams.planner.loop import PlannerLoop
    _c = _connect(db_path); _migrate(_c); _c.close()
    db = _connect(db_path)
    s = Settings(_env_file=None, stall_alert_minutes=15, db_path=db_path)
    pid = repo_projects.upsert_project(db, "o/r")
    wf = repo_workflows.create_workflow(db, pid, "adhoc", "g")
    repo_workflows.set_status(db, wf, "running")
    brief = {"objective": "o", "acceptance": "a", "run": ["echo x"],
             "writable": ["*.py"], "repo": "o/r"}
    t = repo_tasks.create_task(db, wf, 1, "t1", "o", "a", brief)
    repo_tasks.promote_ready(db)
    row = repo_tasks.claim_for_dispatch(db, t, 120)
    repo_tasks.on_started(db, t, row["attempt"], row["lease_token"], "e1", "s")
    db.execute("UPDATE tasks SET heartbeat_json=? WHERE task_id=?",
               (_json.dumps({"turns_used": 5, "elapsed_s": 3600,
                             "ts": plus_s(-3600), "progress_ts": plus_s(-3600)}),
                t))
    loop = PlannerLoop(s, InMemoryBus(), None)
    loop.conn = db                      # test against the fixture DB
    assert loop._stall_probe() == 1
    assert repo_alerts.open_count(db) == 1
    assert repo_tasks.get_task(db, t)["status"] == "running"   # NOT killed
    # idempotent: same stalled turn count does not re-alert
    assert loop._stall_probe() == 0
