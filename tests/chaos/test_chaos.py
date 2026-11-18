"""WP10: mandatory chaos / acceptance tests (§13).

Scenarios 1-10 run against in-process fakes (InMemoryBus, fake Claudio, local
git fixtures) so they are deterministic and CI-safe. The real-Redis variant of
#2 runs when a local Redis is up.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from clams import contracts
from clams.bus import InMemoryBus
from clams.claudio_client import ChatResponse
from clams.config import Settings
from clams.db import (repo_alerts, repo_events, repo_outbox, repo_projects,
                      repo_runtime, repo_tasks, repo_workflows)
from clams.db.conn import plus_s
from clams.executor.runner import ExecutorRunner
from clams.executor.worktree import WorktreeManager
from clams.outbox import OutboxRelay
from clams.planner import reaper
from clams.planner.dispatch import dispatch_ready
from clams.planner.review import apply_event


def _settings(tmp_path, **kw):
    return Settings(_env_file=None, db_path=str(tmp_path / "c.db"),
                    work_root=str(tmp_path / "work"),
                    heartbeat_interval_s=1, **kw)


def _ready_task(db, s, max_attempts=3):
    pid = repo_projects.upsert_project(db, "o/r")
    wf = repo_workflows.create_workflow(db, pid, "adhoc", "g")
    repo_workflows.set_status(db, wf, "running")
    brief = {"objective": "o", "acceptance": "a", "run": ["echo ok"],
             "writable": ["*.py"], "repo": "o/r", "base_ref": "main",
             "max_wall_s": 1800}
    t = repo_tasks.create_task(db, wf, 1, "t", "o", "a", brief,
                               max_attempts=max_attempts)
    repo_tasks.promote_ready(db)
    return wf, t


# ---------------------------------------------------------------- scenario 1
def test_executor_death_redispatch_and_fence(db, tmp_path):
    """docker kill executor mid-task -> re-dispatch after lease, attempt+1,
    old result fenced."""
    s = _settings(tmp_path)
    bus = InMemoryBus()
    wf, t = _ready_task(db, s)
    dispatch_ready(db, bus, s)
    OutboxRelay(bus).relay_once(db)
    row = repo_tasks.get_task(db, t)
    old_token, old_attempt = row["lease_token"], row["attempt"]
    repo_tasks.on_started(db, t, old_attempt, old_token, "dead-e", "s")
    # executor dies: lease expires
    db.execute("UPDATE tasks SET lease_expires_at=? WHERE task_id=?",
               (plus_s(-1), t))
    reaper.reap_expired_leases(db, s)
    assert repo_tasks.get_task(db, t)["status"] == "ready"
    dispatch_ready(db, bus, s)
    OutboxRelay(bus).relay_once(db)
    row2 = repo_tasks.get_task(db, t)
    assert row2["attempt"] == old_attempt + 1
    assert row2["lease_token"] != old_token
    # the dead executor finally posts its stale result -> fenced
    stale = contracts.TaskResult(task_id=t, attempt=old_attempt,
                                 lease_token=old_token, outcome="success",
                                 evidence={"passed": True})
    assert apply_event(db, stale, "stale-1", s) == "fenced"
    assert repo_tasks.get_task(db, t)["status"] == "dispatched"


# ---------------------------------------------------------------- scenario 2
def test_redis_outage_no_loss_no_dupes(db, tmp_path):
    """Redis down mid-dispatch: outbox holds the message; on recovery it is
    delivered exactly once; no task lost."""
    s = _settings(tmp_path)

    class FlakyBus(InMemoryBus):
        down = True

        def publish(self, stream, payload):
            if self.down:
                raise ConnectionError("redis down")
            return super().publish(stream, payload)

    bus = FlakyBus()
    wf, t = _ready_task(db, s)
    dispatch_ready(db, bus, s)
    relay = OutboxRelay(bus, backoff_base_s=1, max_attempts=5)
    out = relay.relay_once(db)
    assert out["failed"] == 1                      # publish failed
    assert repo_tasks.get_task(db, t)["status"] == "dispatched"  # DB truth
    bus.down = False                               # redis back
    db.execute("UPDATE outbox SET next_attempt_at='2000-01-01 00:00:00'")
    out2 = relay.relay_once(db)
    assert out2["sent"] == 1
    assert bus.xlen(s.stream_tasks) == 1           # exactly one delivery
    # and it is a valid dispatch for our task
    m = contracts.parse_message(bus._streams[s.stream_tasks][0].payload)
    assert m.task_id == t


@pytest.mark.redis
def test_real_redis_roundtrip(tmp_path):
    import os
    from clams.bus import RedisBus
    url = os.environ.get("CLAMS_REDIS_URL", "redis://127.0.0.1:6379/0")
    b = RedisBus(url)
    if not b.ping():
        pytest.skip("no redis")
    b._r.delete("clams:chaos:tasks")
    b.ensure_group("clams:chaos:tasks", "executors")
    b.publish("clams:chaos:tasks", "x")
    got = b.read("clams:chaos:tasks", "executors", "e1", block_ms=100)
    assert len(got) == 1
    b.ack("clams:chaos:tasks", "executors", got[0].id)
    b._r.delete("clams:chaos:tasks")


# ---------------------------------------------------------------- scenario 3
def test_kill_between_commit_and_ack(db, tmp_path):
    """Planner crashes after DB commit but before XACK: redelivery applies
    idempotently (processed_events)."""
    s = _settings(tmp_path)
    bus = InMemoryBus()
    wf, t = _ready_task(db, s)
    row = repo_tasks.claim_for_dispatch(db, t, 120)
    tok, a = row["lease_token"], row["attempt"]
    started = contracts.TaskStarted(task_id=t, attempt=a, lease_token=tok,
                                    executor_id="e1", session_ref="s")
    bus.publish(s.stream_events, contracts.dump_message(started))
    bus.ensure_group(s.stream_events, s.group_planner)
    d = bus.read(s.stream_events, s.group_planner, "planner", count=1)[0]
    assert apply_event(db, started, d.id, s) == "applied"
    # crash here: no ack. Redeliver (PEL replay):
    again = bus.replay_pending(s.stream_events, s.group_planner, "planner")
    assert [x.id for x in again] == [d.id]
    assert apply_event(db, started, d.id, s) == "dup"
    # state changed exactly once
    assert repo_tasks.get_task(db, t)["status"] == "running"


# ---------------------------------------------------------------- scenario 4
def test_relay_crash_after_gh_call_no_duplicate_comment(db, tmp_path):
    """Relay posts the comment, dies before marking sent; on reclaim the
    marker in the body prevents a second comment."""
    posted: list[str] = []

    class FakeGH:
        def add_comment(self, repo, issue, body, idempotency_key=""):
            if any(idempotency_key in b for b in posted):
                return {"deduped": True}
            posted.append(body)
            return {}

    s = _settings(tmp_path)
    bus = InMemoryBus()
    relay = OutboxRelay(bus, gh_client=FakeGH())
    key = "gh:gh_comment:5:done"
    repo_outbox.enqueue(db, "gh_comment",
                        {"repo": "o/r", "issue": 5,
                         "body": f"done <!-- {key} -->",
                         "idempotency_key": key}, key)
    # claim then crash (never marked sent)
    repo_outbox.claim_due(db)
    db.execute("UPDATE outbox SET next_attempt_at='2000-01-01 00:00:00' "
               "WHERE status='inflight'")
    # simulate the first attempt's side effect already happened:
    posted.append(f"done <!-- {key} -->")
    out = relay.relay_once(db)
    assert out["sent"] == 1
    assert len(posted) == 1          # dedup by marker: no second comment


# ------------------------------------------------------------- scenarios 5+6
@pytest.fixture()
def remote(tmp_path):
    r = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", str(r)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(seed)], check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=seed, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=seed, check=True)
    (seed / "hello.txt").write_text("hi\n")
    subprocess.run(["git", "add", "."], cwd=seed, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=seed, check=True,
                   capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(r)], cwd=seed,
                   check=True, capture_output=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=seed,
                   check=True, capture_output=True)
    return r


def _run_runner(tmp_path, remote, agent_action, brief_overrides=None):
    s = _settings(tmp_path)
    bus = InMemoryBus()
    bus.ensure_group(s.stream_tasks, s.group_executors)
    wt = WorktreeManager(s.work_root, remote_url_template=str(remote))

    class FakeClaudio:
        def chat(self, message, session_id=None, approval_mode=None,
                 workspace_dir=None, timeout=None):
            agent_action(Path(workspace_dir))
            return ChatResponse(run_id="r", session_id=session_id or "s",
                                final_output="I succeeded, trust me", usage={})

    brief = contracts.Brief(objective="o", acceptance="a",
                            run=["test -f done.txt && echo ok"],
                            writable=["*.py"], forbidden=["*.env"],
                            repo="o/r", base_ref="main")
    if brief_overrides:
        brief = brief.model_copy(update=brief_overrides)
    m = contracts.TaskDispatch(task_id="chaos", workflow_id="w", attempt=1,
                               lease_token="L", brief=brief)
    bus.publish(s.stream_tasks, contracts.dump_message(m))
    d = bus.read(s.stream_tasks, s.group_executors, "e1", count=1)[0]
    runner = ExecutorRunner(s, bus, FakeClaudio(), wt)
    runner._handle(d)
    bus.ensure_group(s.stream_events, "planner")
    evs = [contracts.parse_message(x.payload)
           for x in bus.read(s.stream_events, "planner", "p", count=10)]
    return [e for e in evs if e.type == "task.result"][0]


def test_claimed_success_no_commit_gate_fail(tmp_path, remote):
    """5: agent claims success but made no commit -> gate FAIL."""
    res = _run_runner(tmp_path, remote, lambda ws: None)
    assert res.outcome == "failed"
    assert res.evidence["passed"] is False


def test_forbidden_edit_gate_fail(tmp_path, remote):
    """6: agent edits a forbidden path -> gate FAIL."""
    def agent(ws: Path):
        (ws / "done.py").write_text("x\n")
        (ws / "prod.env").write_text("SECRET=1\n")
        subprocess.run(["git", "add", "."], cwd=ws, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "bad"], cwd=ws, check=True,
                       capture_output=True)
    res = _run_runner(tmp_path, remote, agent)
    assert res.outcome == "failed"
    assert any(c["check"] == "scope" and not c["ok"]
               for c in res.evidence["checks"])


# ---------------------------------------------------------------- scenario 7
def test_three_failures_then_blocked_with_alert(db, tmp_path):
    """7: task fails 3x -> failed; workflow replans <=3 then blocked + alert."""
    s = _settings(tmp_path, max_attempts=3, max_replans=2)
    bus = InMemoryBus()
    wf, t = _ready_task(db, s, max_attempts=3)
    for expected in ("ready", "ready", "failed"):
        row = repo_tasks.claim_for_dispatch(db, t, 120)
        tok, a = row["lease_token"], row["attempt"]
        repo_tasks.on_started(db, t, a, tok, "e1", "s")
        m = contracts.TaskResult(task_id=t, attempt=a, lease_token=tok,
                                 outcome="failed", evidence={"passed": False})
        apply_event(db, m, f"r{a}", s)
        assert repo_tasks.get_task(db, t)["status"] == expected
    # workflow: after task failed -> replan twice -> blocked + alert
    # (first two failures requeued the task; the third failed it)
    st = repo_workflows.get_workflow(db, wf)["status"]
    assert st in ("planning", "blocked")
    # drive replans out
    for _ in range(3):
        if repo_workflows.get_workflow(db, wf)["status"] != "planning":
            break
        repo_workflows.set_status(db, wf, "running")
        db.execute("UPDATE tasks SET status='running' WHERE task_id=?", (t,))
        row = repo_tasks.get_task(db, t)
        m = contracts.TaskResult(task_id=t, attempt=row["attempt"],
                                 lease_token=row["lease_token"],
                                 outcome="failed", evidence={"passed": False})
        apply_event(db, m, f"x{time.time()}", s)
    assert repo_workflows.get_workflow(db, wf)["status"] == "blocked"
    assert repo_alerts.open_count(db) >= 1


# ---------------------------------------------------------------- scenario 8
def test_two_runners_one_task_exactly_one_execution(tmp_path, remote):
    """8: two runners, one task -> exactly one execution."""
    s = _settings(tmp_path)
    bus = InMemoryBus()
    bus.ensure_group(s.stream_tasks, s.group_executors)
    wt = WorktreeManager(s.work_root, remote_url_template=str(remote))
    executions = []

    class FakeClaudio:
        def chat(self, message, session_id=None, approval_mode=None,
                 workspace_dir=None, timeout=None):
            executions.append(1)
            (Path(workspace_dir) / "done.txt").write_text("ok\n")
            subprocess.run(["git", "add", "."], cwd=workspace_dir, check=True,
                           capture_output=True)
            subprocess.run(["git", "commit", "-m", "w"], cwd=workspace_dir,
                           check=True, capture_output=True)
            return ChatResponse(run_id="r", session_id=session_id,
                                final_output="ok", usage={})

    brief = contracts.Brief(objective="o", acceptance="a",
                            run=["test -f done.txt && echo ok"],
                            writable=["*.txt"], repo="o/r", base_ref="main")
    m = contracts.TaskDispatch(task_id="solo", workflow_id="w", attempt=1,
                               lease_token="L", brief=brief)
    bus.publish(s.stream_tasks, contracts.dump_message(m))
    r1 = ExecutorRunner(s, bus, FakeClaudio(), wt)
    r1.executor_id = "e1"
    r2 = ExecutorRunner(s, bus, FakeClaudio(), wt)
    r2.executor_id = "e2"
    for r in (r1, r2):
        for d in bus.read(s.stream_tasks, s.group_executors, r.executor_id,
                          count=1):
            r._handle(d)
    assert len(executions) == 1


# ---------------------------------------------------------------- scenario 9
def test_operator_pause_survives_watchdog(db, tmp_path):
    """9: operator pause + watchdog healthy sweeps -> stays paused."""
    from clams.planner.watchdog import Watchdog

    class Alive:
        def health(self):
            return True

    s = _settings(tmp_path, watchdog_probe_interval_s=0)
    wd = Watchdog(InMemoryBus(), Alive(), Alive(), s, disk_path=str(tmp_path))
    from clams.db import repo_executors
    repo_executors.upsert_alive(db, "e1", "2.0.0", 1)
    repo_runtime.pause(db, "operator")
    for _ in range(5):
        wd.probe(db)
    assert repo_runtime.is_paused(db)
    assert repo_runtime.paused_by(db) == "operator"


# --------------------------------------------------------------- scenario 10
def test_work_volume_deleted_runner_recovers(tmp_path, remote):
    """10: delete /work volume -> runner recreates worktrees, no crash."""
    s = _settings(tmp_path)
    bus = InMemoryBus()
    bus.ensure_group(s.stream_tasks, s.group_executors)
    wt = WorktreeManager(s.work_root, remote_url_template=str(remote))

    class FakeClaudio:
        def chat(self, message, session_id=None, approval_mode=None,
                 workspace_dir=None, timeout=None):
            (Path(workspace_dir) / "done.txt").write_text("ok\n")
            subprocess.run(["git", "add", "."], cwd=workspace_dir, check=True,
                           capture_output=True)
            subprocess.run(["git", "commit", "-m", "w"], cwd=workspace_dir,
                           check=True, capture_output=True)
            return ChatResponse(run_id="r", session_id=session_id,
                                final_output="ok", usage={})

    brief = contracts.Brief(objective="o", acceptance="a",
                            run=["test -f done.txt && echo ok"],
                            writable=["*.txt"], repo="o/r", base_ref="main")
    m = contracts.TaskDispatch(task_id="vol", workflow_id="w", attempt=1,
                               lease_token="L", brief=brief)
    bus.publish(s.stream_tasks, contracts.dump_message(m))
    runner = ExecutorRunner(s, bus, FakeClaudio(), wt)
    d = bus.read(s.stream_tasks, s.group_executors, "e1", count=1)[0]
    runner._handle(d)
    # wipe the whole work volume
    import shutil
    shutil.rmtree(s.work_root, ignore_errors=True)
    # next task still works: clone + worktree recreated
    m2 = contracts.TaskDispatch(task_id="vol2", workflow_id="w", attempt=1,
                                lease_token="L2", brief=brief)
    bus.publish(s.stream_tasks, contracts.dump_message(m2))
    d2 = bus.read(s.stream_tasks, s.group_executors, "e1", count=1)[0]
    runner._handle(d2)
    bus.ensure_group(s.stream_events, "planner")
    evs = [contracts.parse_message(x.payload)
           for x in bus.read(s.stream_events, "planner", "p", count=10)]
    results = [e for e in evs if e.type == "task.result"]
    assert results and results[-1].outcome == "success"
