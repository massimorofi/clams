"""WP1: every repository + the task state-machine transition table."""
from __future__ import annotations

import json
import sqlite3

import pytest

from clams.db import connect, migrate, schema_version, immediate
from clams.db import (repo_projects, repo_workflows, repo_tasks,
                      repo_questions, repo_commands, repo_outbox,
                      repo_events, repo_executors, repo_alerts, repo_runtime)
from clams.db.conn import db_integrity_ok, plus_s, utcnow_s


def test_migrations_apply_once(db):
    assert schema_version(db) == 2   # 001_init + 002_tasks_started_at
    assert migrate(db) == []          # idempotent
    assert db_integrity_ok(db)


def test_immediate_tx_rolls_back(db):
    repo_projects.upsert_project(db, "o/r")
    with pytest.raises(RuntimeError):
        with immediate(db):
            repo_projects.upsert_project(db, "o/fail")
            raise RuntimeError("boom")
    assert repo_projects.get_project(db, "o/r") is not None
    assert repo_projects.get_project(db, "o/fail") is None


# ---------------------------------------------------------------- projects
def test_projects_upsert(db):
    pid = repo_projects.upsert_project(db, "o/r", gh_project_node_id="N1")
    pid2 = repo_projects.upsert_project(db, "o/r", enabled=False)
    assert pid == pid2
    row = repo_projects.get_project(db, "o/r")
    assert row["gh_project_node_id"] == "N1"   # COALESCE keeps old value
    assert row["enabled"] == 0
    assert repo_projects.list_projects(db, enabled_only=True) == []


def test_projects_delete(db):
    repo_projects.upsert_project(db, "o/r")
    assert repo_projects.delete_project(db, "o/r") == 1
    assert repo_projects.get_project(db, "o/r") is None
    assert repo_projects.delete_project(db, "o/r") == 0  # idempotent


# --------------------------------------------------------------- workflows
def test_workflow_lifecycle(db):
    pid = repo_projects.upsert_project(db, "o/r")
    wf = repo_workflows.create_workflow(db, pid, "github", "goal", 7)
    row = repo_workflows.get_workflow(db, wf)
    assert row["status"] == "intake"
    assert repo_workflows.find_by_issue(db, pid, 7)["workflow_id"] == wf
    repo_workflows.set_status(db, wf, "planning")
    assert repo_workflows.list_by_status(db, "planning")[0]["workflow_id"] == wf
    assert repo_workflows.bump_replans(db, wf) == 1
    repo_workflows.set_plan(db, wf, {"tasks": []})
    assert repo_workflows.get_workflow(db, wf)["plan_json"] == '{"tasks": []}'
    with pytest.raises(ValueError):
        repo_workflows.set_status(db, wf, "nope")


def test_workflow_unique_issue(db):
    pid = repo_projects.upsert_project(db, "o/r")
    repo_workflows.create_workflow(db, pid, "github", "g1", 1)
    with pytest.raises(sqlite3.IntegrityError):
        repo_workflows.create_workflow(db, pid, "github", "g2", 1)


# ------------------------------------------------------------------- tasks
def _mk_wf(db):
    pid = repo_projects.upsert_project(db, "o/r")
    return repo_workflows.create_workflow(db, pid, "adhoc", "goal")


def _brief():
    return {"objective": "o", "acceptance": "a", "run": ["pytest"],
            "writable": ["src/**"], "repo": "o/r", "max_wall_s": 1800}


def test_task_create_and_list(db):
    wf = _mk_wf(db)
    t1 = repo_tasks.create_task(db, wf, 1, "first", "o", "a", _brief())
    t2 = repo_tasks.create_task(db, wf, 2, "second", "o", "a", _brief(),
                                depends_on=[1])
    rows = repo_tasks.list_tasks(db, wf)
    assert [r["task_id"] for r in rows] == [t1, t2]
    assert rows[1]["depends_on"] == "[1]"
    with pytest.raises(sqlite3.IntegrityError):
        repo_tasks.create_task(db, wf, 1, "dup-index", "o", "a", _brief())
    with pytest.raises(sqlite3.IntegrityError):
        repo_tasks.create_task(db, wf, 3, "first", "o", "a", _brief())


def test_transition_table_enforced(db):
    wf = _mk_wf(db)
    t = repo_tasks.create_task(db, wf, 1, "t", "o", "a", _brief())
    with pytest.raises(repo_tasks.IllegalTransition):
        repo_tasks.transition(db, t, "success")     # pending -> success illegal
    repo_tasks.transition(db, t, "ready")
    repo_tasks.transition(db, t, "dispatched")
    assert repo_tasks.get_task(db, t)["status"] == "dispatched"
    # terminal states are frozen
    repo_tasks.transition(db, t, "running")
    repo_tasks.transition(db, t, "success")
    with pytest.raises(repo_tasks.IllegalTransition):
        repo_tasks.transition(db, t, "ready")


def test_promote_ready_respects_deps(db):
    wf = _mk_wf(db)
    t1 = repo_tasks.create_task(db, wf, 1, "a", "o", "a", _brief())
    t2 = repo_tasks.create_task(db, wf, 2, "b", "o", "a", _brief(), depends_on=[1])
    assert repo_tasks.promote_ready(db) == [t1]
    assert repo_tasks.get_task(db, t2)["status"] == "pending"
    repo_tasks.transition(db, t1, "ready")
    repo_tasks.transition(db, t1, "dispatched")
    repo_tasks.transition(db, t1, "running")
    repo_tasks.transition(db, t1, "success")
    assert repo_tasks.promote_ready(db) == [t2]


def test_claim_for_dispatch_atomic(db):
    wf = _mk_wf(db)
    t = repo_tasks.create_task(db, wf, 1, "a", "o", "a", _brief())
    repo_tasks.transition(db, t, "ready")
    row = repo_tasks.claim_for_dispatch(db, t, lease_seconds=120)
    assert row["status"] == "dispatched"
    assert row["attempt"] == 1
    assert row["lease_token"]
    # second claim fails (no longer ready)
    assert repo_tasks.claim_for_dispatch(db, t, 120) is None


def test_on_started_lease_survives_old_created_at(db):
    """Regression: wall cap anchors on started_at, not created_at — a task
    retried hours after creation must still get a future lease."""
    import time
    from clams.db.conn import epoch_to_iso, utcnow_s
    pid = repo_projects.upsert_project(db, "o/r")
    wf = repo_workflows.create_workflow(db, pid, "adhoc", "g")
    brief = {"objective": "o", "acceptance": "a", "run": ["echo x"],
             "writable": ["*.py"], "repo": "o/r", "max_wall_s": 1800}
    t = repo_tasks.create_task(db, wf, 1, "t", "o", "a", brief)
    repo_tasks.transition(db, t, "ready")
    row, tok = repo_tasks.claim_for_dispatch(db, t, 120), None
    tok = row["lease_token"]
    # task was created 20h ago (past any max_wall_s cap)
    db.execute("UPDATE tasks SET created_at=? WHERE task_id=?",
               (epoch_to_iso(time.time() - 20 * 3600), t))
    started = repo_tasks.on_started(db, t, 1, tok, "e1", "s1", extend_s=180)
    assert started is not None and started["started_at"] is not None
    assert started["lease_expires_at"] > utcnow_s()   # not clamped to the past
    hb = repo_tasks.on_heartbeat(db, t, 1, tok, extend_s=180)
    assert hb is not None and hb["lease_expires_at"] > utcnow_s()


def test_on_started_fencing(db):
    wf = _mk_wf(db)
    t = repo_tasks.create_task(db, wf, 1, "a", "o", "a", _brief())
    repo_tasks.promote_ready(db)
    row = repo_tasks.claim_for_dispatch(db, t, 120)
    tok = row["lease_token"]
    assert repo_tasks.on_started(db, t, 1, "wrong-token", "e1", "s1") is None
    started = repo_tasks.on_started(db, t, 1, tok, "e1", "s1")
    assert started["status"] == "running"
    assert started["session_ref"] == "s1"


def test_on_result_paths(db):
    wf = _mk_wf(db)
    t = repo_tasks.create_task(db, wf, 1, "a", "o", "a", _brief())
    repo_tasks.promote_ready(db)
    row = repo_tasks.claim_for_dispatch(db, t, 120)
    tok = row["lease_token"]
    repo_tasks.on_started(db, t, 1, tok, "e1", "s1")
    # fenced result (old token) ignored
    assert repo_tasks.on_result(db, t, 1, "old", "success", True, {}) is None
    # success + gate pass
    r = repo_tasks.on_result(db, t, 1, tok, "success", True,
                             {"commit_sha": "abc", "branch": "b"})
    assert r["status"] == "success" and r["commit_sha"] == "abc"


def test_on_result_fail_retries_then_fails(db):
    wf = _mk_wf(db)
    t = repo_tasks.create_task(db, wf, 1, "a", "o", "a", _brief(),
                               max_attempts=2)
    repo_tasks.promote_ready(db)
    for expected in ("ready", "failed"):
        row = repo_tasks.claim_for_dispatch(db, t, 120)
        tok = row["lease_token"]
        repo_tasks.on_started(db, t, row["attempt"], tok, "e1", "s1")
        r = repo_tasks.on_result(db, t, row["attempt"], tok, "failed", False,
                                 {"log_tail": "boom"})
        assert r["status"] == expected
    # exhausted: cannot claim anymore
    assert repo_tasks.claim_for_dispatch(db, t, 120) is None


def test_on_result_needs_input_and_resume(db):
    wf = _mk_wf(db)
    t = repo_tasks.create_task(db, wf, 1, "a", "o", "a", _brief())
    repo_tasks.promote_ready(db)
    row = repo_tasks.claim_for_dispatch(db, t, 120)
    tok = row["lease_token"]
    repo_tasks.on_started(db, t, 1, tok, "e1", "s1")
    q = repo_tasks.on_question(db, t, 1, tok)
    assert q["status"] == "waiting_input"
    # answer -> ready with answer carried in brief
    repo_tasks.set_brief_field(db, t, {"answer": "use sqlite", "session_ref": "s1"})
    r = repo_tasks.transition(db, t, "ready")
    assert r["status"] == "ready"
    brief = json.loads(r["brief_json"])
    assert brief["answer"] == "use sqlite" and brief["session_ref"] == "s1"


def test_heartbeat_extends_lease(db):
    wf = _mk_wf(db)
    t = repo_tasks.create_task(db, wf, 1, "a", "o", "a", _brief())
    repo_tasks.promote_ready(db)
    row = repo_tasks.claim_for_dispatch(db, t, 120)
    tok = row["lease_token"]
    repo_tasks.on_started(db, t, 1, tok, "e1", "s1")
    before = repo_tasks.get_task(db, t)["lease_expires_at"]
    hb = repo_tasks.on_heartbeat(db, t, 1, tok, extend_s=180, hard_cap_s=1800)
    assert hb["lease_expires_at"] >= before
    # fenced heartbeat ignored
    assert repo_tasks.on_heartbeat(db, t, 1, "nope", 180, 1800) is None


def test_lease_expiry_requeue(db):
    wf = _mk_wf(db)
    t = repo_tasks.create_task(db, wf, 1, "a", "o", "a", _brief())
    repo_tasks.promote_ready(db)
    row = repo_tasks.claim_for_dispatch(db, t, 120)
    db.execute("UPDATE tasks SET lease_expires_at=? WHERE task_id=?",
               (plus_s(-1), t))
    expired = repo_tasks.expired_leases(db)
    assert [r["task_id"] for r in expired] == [t]
    r = repo_tasks.requeue_after_lease(db, t)
    assert r["status"] == "ready" and r["attempt"] == 1


def test_waiting_input_expiry_blocks(db):
    wf = _mk_wf(db)
    t = repo_tasks.create_task(db, wf, 1, "a", "o", "a", _brief())
    repo_tasks.transition(db, t, "ready")
    repo_tasks.transition(db, t, "dispatched")
    repo_tasks.transition(db, t, "running")
    repo_tasks.transition(db, t, "waiting_input")
    db.execute("UPDATE tasks SET updated_at=? WHERE task_id=?",
               (plus_s(-25 * 3600), t))
    assert repo_tasks.expire_waiting_input(db, hours=24) == [t]
    assert repo_tasks.get_task(db, t)["status"] == "blocked"


def test_workflow_outcome(db):
    wf = _mk_wf(db)
    t1 = repo_tasks.create_task(db, wf, 1, "a", "o", "a", _brief())
    t2 = repo_tasks.create_task(db, wf, 2, "b", "o", "a", _brief())
    assert not repo_tasks.workflow_tasks_all_terminal(db, wf)
    for t in (t1, t2):
        repo_tasks.transition(db, t, "ready")
        repo_tasks.transition(db, t, "dispatched")
        repo_tasks.transition(db, t, "running")
        repo_tasks.transition(db, t, "success")
    assert repo_tasks.workflow_tasks_all_terminal(db, wf)
    assert repo_tasks.workflow_outcome(db, wf) == "done"


def test_block_dependents_transitive(db):
    """Regression: a failed task must strand its (transitive) dependents —
    otherwise the workflow never reaches all-terminal and is never reviewed."""
    wf = _mk_wf(db)
    t1 = repo_tasks.create_task(db, wf, 1, "a", "o", "a", _brief())
    t2 = repo_tasks.create_task(db, wf, 2, "b", "o", "a", _brief(),
                                depends_on=[1])
    t3 = repo_tasks.create_task(db, wf, 3, "c", "o", "a", _brief(),
                                depends_on=[2])
    t4 = repo_tasks.create_task(db, wf, 4, "d", "o", "a", _brief())  # indep
    repo_tasks.transition(db, t1, "ready")
    repo_tasks.transition(db, t1, "dispatched")
    repo_tasks.transition(db, t1, "running")
    repo_tasks.transition(db, t1, "failed")
    blocked = repo_tasks.block_dependents(db, wf)
    assert set(blocked) == {t2, t3}
    assert repo_tasks.get_task(db, t2)["status"] == "blocked"
    assert repo_tasks.get_task(db, t3)["status"] == "blocked"
    assert repo_tasks.get_task(db, t4)["status"] == "pending"  # untouched
    # idempotent
    assert repo_tasks.block_dependents(db, wf) == []


def test_all_terminal_and_outcome_blocked(db):
    wf = _mk_wf(db)
    t1 = repo_tasks.create_task(db, wf, 1, "a", "o", "a", _brief())
    t2 = repo_tasks.create_task(db, wf, 2, "b", "o", "a", _brief(),
                                depends_on=[1])
    repo_tasks.transition(db, t1, "ready")
    repo_tasks.transition(db, t1, "dispatched")
    repo_tasks.transition(db, t1, "running")
    repo_tasks.transition(db, t1, "failed")
    repo_tasks.block_dependents(db, wf)          # t2 -> blocked
    assert repo_tasks.workflow_tasks_all_terminal(db, wf)
    assert repo_tasks.workflow_outcome(db, wf) == "failed"   # failed wins
    # blocked-only (question expiry shape): needs a human, not a replan
    wf2 = _mk_wf(db)
    s1 = repo_tasks.create_task(db, wf2, 1, "a", "o", "a", _brief())
    s2 = repo_tasks.create_task(db, wf2, 2, "b", "o", "a", _brief())
    for t in (s1,):
        repo_tasks.transition(db, t, "ready")
        repo_tasks.transition(db, t, "dispatched")
        repo_tasks.transition(db, t, "running")
        repo_tasks.transition(db, t, "success")
    repo_tasks.transition(db, s2, "blocked")
    assert repo_tasks.workflow_tasks_all_terminal(db, wf2)
    assert repo_tasks.workflow_outcome(db, wf2) == "blocked"


def test_delete_workflow_tasks_removes_questions(db):
    from clams.db import repo_questions
    wf = _mk_wf(db)
    t = repo_tasks.create_task(db, wf, 1, "a", "o", "a", _brief())
    repo_questions.open_question(db, t, 1, "why?", ["a", "b"], "k1")
    assert repo_tasks.delete_workflow_tasks(db, wf) == 1
    assert repo_tasks.list_tasks(db, wf) == []
    assert db.execute("SELECT COUNT(*) n FROM questions").fetchone()["n"] == 0
    assert repo_tasks.delete_workflow_tasks(db, wf) == 0  # idempotent


# --------------------------------------------------------------- questions
def test_questions_idempotent_and_answer(db):
    wf = _mk_wf(db)
    t = repo_tasks.create_task(db, wf, 1, "a", "o", "a", _brief())
    qid = repo_questions.open_question(db, t, 1, "which db?", ["sqlite", "pg"],
                                       "k1")
    assert qid is not None
    assert repo_questions.open_question(db, t, 1, "which db?", [], "k1") is None
    assert len(repo_questions.list_open(db)) == 1
    assert repo_questions.answer(db, qid, "sqlite", "massimorofi")
    assert not repo_questions.answer(db, qid, "again", "x")  # already answered
    row = repo_questions.get_for_task(db, t, 1)
    assert row["status"] == "answered" and row["answer"] == "sqlite"


def test_question_expiry(db):
    wf = _mk_wf(db)
    t = repo_tasks.create_task(db, wf, 1, "a", "o", "a", _brief())
    qid = repo_questions.open_question(db, t, 1, "q", [], "k-exp")
    db.execute("UPDATE questions SET expires_at=? WHERE question_id=?",
               (plus_s(-1), qid))
    assert repo_questions.expire_due(db) == [qid]


# ---------------------------------------------------------------- commands
def test_commands_roundtrip(db):
    cid = repo_commands.insert_command(db, "pause", {}, "operator")
    rows = repo_commands.fetch_new(db)
    assert [r["command_id"] for r in rows] == [cid]
    repo_commands.mark(db, cid, "applied", {"ok": True})
    assert repo_commands.fetch_new(db) == []
    got = repo_commands.get(db, cid)
    assert got["status"] == "applied" and json.loads(got["result"]) == {"ok": True}
    with pytest.raises(ValueError):
        repo_commands.insert_command(db, "teleport", {}, "x")


# ------------------------------------------------------------------ outbox
def test_outbox_idempotent_claim_backoff(db):
    oid = repo_outbox.enqueue(db, "bus_publish", {"stream": "s", "msg": {}}, "k1")
    assert repo_outbox.enqueue(db, "bus_publish", {}, "k1") is None  # dedup
    claimed = repo_outbox.claim_due(db)
    assert [r["outbox_id"] for r in claimed] == [oid]
    assert repo_outbox.claim_due(db) == []          # already inflight
    # fail with backoff
    st = repo_outbox.mark_failed(db, oid, "boom", backoff_base_s=2, max_attempts=5)
    assert st == "pending"
    assert repo_outbox.claim_due(db) == []          # not due yet (backoff)
    db.execute("UPDATE outbox SET next_attempt_at=? WHERE outbox_id=?",
               (utcnow_s(), oid))
    assert len(repo_outbox.claim_due(db)) == 1
    repo_outbox.mark_sent(db, oid)
    assert repo_outbox.counts(db).get("sent") == 1


def test_outbox_dead_after_max(db):
    oid = repo_outbox.enqueue(db, "gh_comment", {}, "k-dead")
    repo_outbox.claim_due(db)
    st = repo_outbox.mark_failed(db, oid, "x", backoff_base_s=1, max_attempts=1)
    assert st == "dead"
    assert repo_outbox.dead_count(db) == 1


def test_outbox_reclaim_stale(db):
    oid = repo_outbox.enqueue(db, "bus_publish", {}, "k-stale")
    repo_outbox.claim_due(db)
    db.execute("UPDATE outbox SET next_attempt_at=? WHERE outbox_id=?",
               (plus_s(-600), oid))
    assert repo_outbox.reclaim_stale(db, older_than_s=300) == 1
    assert repo_outbox.claim_due(db)[0]["outbox_id"] == oid


# ------------------------------------------------------------------ events
def test_events_tail_and_processed(db):
    e1 = repo_events.append_event(db, "task", "t1", "info", "hello", {"a": 1})
    e2 = repo_events.append_event(db, "workflow", "w1", "warn", "hi")
    rows = repo_events.tail_events(db, 0)
    assert [r["event_id"] for r in rows] == [e1, e2]
    assert repo_events.tail_events(db, e1)[0]["event_id"] == e2
    assert repo_events.max_event_id(db) == e2
    assert not repo_events.was_processed(db, "m1")
    repo_events.mark_processed(db, "m1")
    repo_events.mark_processed(db, "m1")  # idempotent
    assert repo_events.was_processed(db, "m1")


# -------------------------------------------------------------- executors
def test_executors_alive(db):
    repo_executors.upsert_alive(db, "e1", "2.0.0", 1)
    repo_executors.upsert_alive(db, "e1", "2.0.1", 2)
    rows = repo_executors.list_executors(db)
    assert len(rows) == 1 and rows[0]["version"] == "2.0.1"
    assert repo_executors.live_executors(db, max_idle_s=60)
    assert repo_executors.live_executors(db, max_idle_s=-1) == []


# ------------------------------------------------------------------ alerts
def test_alerts_dedup(db):
    assert repo_alerts.raise_alert(db, "redis", "error", "down") is not None
    assert repo_alerts.raise_alert(db, "redis", "error", "still down") is None
    assert repo_alerts.open_count(db) == 1
    repo_alerts.clear_alert(db, "redis")
    assert repo_alerts.open_count(db) == 0
    assert repo_alerts.raise_alert(db, "redis", "error", "down again") is not None


# ----------------------------------------------------------- runtime state
def test_pause_provenance(db):
    assert not repo_runtime.is_paused(db)
    repo_runtime.pause(db, "operator")
    assert repo_runtime.is_paused(db) and repo_runtime.paused_by(db) == "operator"
    # watchdog cannot clear an operator pause
    assert repo_runtime.resume(db, "watchdog") is False
    assert repo_runtime.is_paused(db)
    assert repo_runtime.resume(db, "operator") is True
    # watchdog pause can be cleared by watchdog
    repo_runtime.pause(db, "watchdog")
    assert repo_runtime.resume(db, "watchdog") is True


def test_heartbeat(db):
    assert repo_runtime.heartbeat_age_s(db) is None
    repo_runtime.heartbeat(db)
    assert repo_runtime.heartbeat_age_s(db) < 5


def test_read_only_connection(db_path):
    conn = connect(db_path)
    migrate(conn)
    repo_projects.upsert_project(conn, "o/r")
    conn.close()
    ro = connect(db_path, read_only=True)
    assert ro.execute("SELECT * FROM projects").fetchall()
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO runtime_state(key,value) VALUES('x','y')")
    ro.close()
