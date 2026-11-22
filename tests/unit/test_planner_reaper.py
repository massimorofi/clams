"""WP4/WP6: reaper + watchdog ladder + pause provenance."""
from __future__ import annotations

from clams.bus import InMemoryBus
from clams.config import Settings
from clams.db import (repo_alerts, repo_executors, repo_outbox, repo_runtime,
                      repo_tasks, repo_projects, repo_workflows)
from clams.db.conn import plus_s
from clams.planner.reaper import reap_expired_leases, expire_questions
from clams.planner.watchdog import Watchdog


class DeadExecutor:
    def health(self):
        return False


class AliveExecutor:
    def health(self):
        return True


def _running_task(db):
    pid = repo_projects.upsert_project(db, "o/r")
    wf = repo_workflows.create_workflow(db, pid, "adhoc", "g")
    brief = {"objective": "o", "acceptance": "a", "run": ["x"],
             "writable": ["*"], "repo": "o/r"}
    t = repo_tasks.create_task(db, wf, 1, "t", "o", "a", brief,
                               max_attempts=2)
    repo_tasks.promote_ready(db)
    row = repo_tasks.claim_for_dispatch(db, t, 120)
    repo_tasks.on_started(db, t, row["attempt"], row["lease_token"], "e1", "s")
    return t


def test_reap_expired_requeues_then_fails(db):
    s = Settings(_env_file=None, max_attempts=2)
    t = _running_task(db)
    db.execute("UPDATE tasks SET lease_expires_at=? WHERE task_id=?",
               (plus_s(-1), t))
    assert reap_expired_leases(db, s) == [t]
    assert repo_tasks.get_task(db, t)["status"] == "ready"
    # second expiry exhausts attempts -> failed
    row = repo_tasks.claim_for_dispatch(db, t, 120)
    repo_tasks.on_started(db, t, row["attempt"], row["lease_token"], "e1", "s")
    db.execute("UPDATE tasks SET lease_expires_at=? WHERE task_id=?",
               (plus_s(-1), t))
    reap_expired_leases(db, s)
    assert repo_tasks.get_task(db, t)["status"] == "failed"


def test_question_expiry_blocks(db):
    s = Settings(_env_file=None, question_expiry_h=24)
    t = _running_task(db)
    repo_tasks.transition(db, t, "waiting_input")
    db.execute("UPDATE tasks SET updated_at=? WHERE task_id=?",
               (plus_s(-25 * 3600), t))
    out = expire_questions(db, s)
    assert t in out["tasks"]
    assert repo_tasks.get_task(db, t)["status"] == "blocked"


def _wd(db, bus=None, cp=None, ce=None):
    s = Settings(_env_file=None, watchdog_probe_interval_s=0,
                 watchdog_healthy_probes_to_resume=2,
                 oldest_ready_alert_s=100)
    return Watchdog(bus or InMemoryBus(), cp, ce, s,
                    disk_path=str(db.execute("PRAGMA database_list")
                                  .fetchone()[2]) or ".")


def test_watchdog_pauses_on_redis_down(db):
    class DownBus(InMemoryBus):
        def ping(self):
            return False
    wd = _wd(db, bus=DownBus(), cp=AliveExecutor(), ce=AliveExecutor())
    res = wd.probe(db)
    assert res["redis"] == "fail"
    assert repo_runtime.is_paused(db)
    assert repo_runtime.paused_by(db) == "watchdog"
    assert repo_alerts.open_count(db) >= 1


def test_watchdog_autoresume_after_healthy_probes(db):
    wd = _wd(db, cp=AliveExecutor(), ce=AliveExecutor())
    # make it healthy: need a live executor row
    repo_executors.upsert_alive(db, "e1", "2.0.0", 1)
    wd.s.executor_alive_interval_s = 30
    res = wd.probe(db)
    assert all(v == "ok" for v in res.values()), res
    # pause via watchdog; one healthy probe is not enough (threshold=2)
    repo_runtime.pause(db, "watchdog")
    from clams.db.conn import set_runtime_state
    set_runtime_state(db, "watchdog_consecutive_healthy", "0")
    wd.probe(db)
    assert repo_runtime.is_paused(db)
    wd.probe(db)
    assert not repo_runtime.is_paused(db)


def test_operator_pause_never_auto_cleared(db):
    wd = _wd(db, cp=AliveExecutor(), ce=AliveExecutor())
    repo_executors.upsert_alive(db, "e1", "2.0.0", 1)
    repo_runtime.pause(db, "operator")
    for _ in range(5):
        wd.probe(db)
    assert repo_runtime.is_paused(db)
    assert repo_runtime.paused_by(db) == "operator"


def test_watchdog_dlq_and_dead_outbox(db):
    bus = InMemoryBus()
    bus.push_dlq("clams:dlq", "x", "poison")
    oid = repo_outbox.enqueue(db, "bus_publish", {}, "k1")
    repo_outbox.claim_due(db)
    repo_outbox.mark_failed(db, oid, "boom", backoff_base_s=1, max_attempts=1)
    wd = _wd(db, bus=bus, cp=AliveExecutor(), ce=AliveExecutor())
    res = wd.probe(db)
    assert res["dlq"] == "fail"
    assert res["outbox"] == "fail"


def _running_task_with_dependents(db, max_attempts=1):
    """t1 running (lease will expire), t2 pending depends on t1."""
    pid = repo_projects.upsert_project(db, "o/r")
    wf = repo_workflows.create_workflow(db, pid, "adhoc", "g")
    repo_workflows.set_status(db, wf, "running")
    brief = {"objective": "o", "acceptance": "a", "run": ["x"],
             "writable": ["*"], "repo": "o/r"}
    t1 = repo_tasks.create_task(db, wf, 1, "t1", "o", "a", brief,
                                max_attempts=max_attempts)
    t2 = repo_tasks.create_task(db, wf, 2, "t2", "o", "a", brief,
                                depends_on=[1], max_attempts=max_attempts)
    repo_tasks.promote_ready(db)
    row = repo_tasks.claim_for_dispatch(db, t1, 120)
    repo_tasks.on_started(db, t1, row["attempt"], row["lease_token"], "e1", "s")
    return wf, t1, t2


def test_lease_expiry_failure_blocks_dependents_and_reviews(db):
    """Regression: reaper failing a task (attempts exhausted) must strand +
    review, same as the result path — else workflow runs forever."""
    s = Settings(_env_file=None, max_attempts=1)
    wf, t1, t2 = _running_task_with_dependents(db, max_attempts=1)
    db.execute("UPDATE tasks SET lease_expires_at=? WHERE task_id=?",
               (plus_s(-1), t1))
    reap_expired_leases(db, s)
    assert repo_tasks.get_task(db, t1)["status"] == "failed"
    assert repo_tasks.get_task(db, t2)["status"] == "blocked"
    assert repo_workflows.get_workflow(db, wf)["status"] == "planning"


def test_question_expiry_blocks_dependents_and_workflow(db):
    """waiting_input expiry -> blocked -> dependents blocked -> workflow
    blocked + alert (outcome 'blocked', no replan)."""
    s = Settings(_env_file=None, question_expiry_h=24)
    pid = repo_projects.upsert_project(db, "o/r")
    wf = repo_workflows.create_workflow(db, pid, "adhoc", "g")
    repo_workflows.set_status(db, wf, "running")
    brief = {"objective": "o", "acceptance": "a", "run": ["x"],
             "writable": ["*"], "repo": "o/r"}
    t1 = repo_tasks.create_task(db, wf, 1, "t1", "o", "a", brief)
    t2 = repo_tasks.create_task(db, wf, 2, "t2", "o", "a", brief,
                                depends_on=[1])
    repo_tasks.transition(db, t1, "ready")
    repo_tasks.transition(db, t1, "dispatched")
    repo_tasks.transition(db, t1, "running")
    repo_tasks.transition(db, t1, "waiting_input")
    db.execute("UPDATE tasks SET updated_at=? WHERE task_id=?",
               (plus_s(-25 * 3600), t1))
    expire_questions(db, s)
    assert repo_tasks.get_task(db, t1)["status"] == "blocked"
    assert repo_tasks.get_task(db, t2)["status"] == "blocked"
    assert repo_workflows.get_workflow(db, wf)["status"] == "blocked"
    assert repo_alerts.open_count(db) == 1
