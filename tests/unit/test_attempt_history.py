"""Attempt history (#2): append-only task_attempts + cumulative retry summary.

Synchronous, stdlib-only tests on a temp SQLite DB migrated via clams.db.conn.
Proves: attempt-1 evidence is still readable after attempt 3; replaying the
same result neither duplicates nor overwrites rows; the retry summary is
bounded and includes every attempt.
"""
from __future__ import annotations

import json

from clams.db import repo_attempts, repo_projects, repo_tasks, repo_workflows
from clams.db.conn import connect, migrate


def _db(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    migrate(conn)
    return conn


def _task(conn, max_attempts=3):
    repo_projects.upsert_project(conn, "o/r")
    wf = repo_workflows.create_workflow(conn, 1, "adhoc", "goal")
    brief = {"objective": "obj", "acceptance": "acc", "run": ["pytest -q"],
             "writable": ["*.py"], "repo": "o/r", "base_ref": "main"}
    tid = repo_tasks.create_task(conn, wf, 1, "t1", "obj", "acc", brief,
                                 max_attempts=max_attempts)
    conn.execute("UPDATE tasks SET status='ready' WHERE task_id=?", (tid,))
    return wf, tid


def _result(n, *, gate_passed=False):
    return {
        "evidence": {"ok": gate_passed, "checks": [
            {"cmd": f"pytest t{n}", "ok": gate_passed,
             "exit": 0 if gate_passed else 1,
             "output_tail": f"line one\nFAILED test_{n}: boom on attempt {n}"}]},
        "branch": f"task/a/a{n}", "commit_sha": f"deadbeef{n}" * 4,
        "log_tail": f"tail of attempt {n}",
    }


def _run_attempt(conn, tid, n, *, outcome="failed", gate_passed=False):
    """One full dispatch->start->result cycle for attempt n."""
    row = repo_tasks.claim_for_dispatch(conn, tid, 60)
    assert row is not None and row["attempt"] == n
    repo_tasks.on_started(conn, tid, n, row["lease_token"], "exec1", "sess1")
    return repo_tasks.on_result(conn, tid, row["attempt"], row["lease_token"],
                                 outcome, gate_passed, _result(n, gate_passed=gate_passed))


# ------------------------------------------------------- append-only history
def test_attempt1_still_visible_after_attempt3(tmp_path):
    conn = _db(tmp_path)
    wf, tid = _task(conn, max_attempts=3)
    for n in (1, 2, 3):
        _run_attempt(conn, tid, n)
    rows = repo_attempts.get_attempts(conn, tid)
    assert [r["attempt_no"] for r in rows] == [1, 2, 3]
    assert all(r["workflow_id"] == wf for r in rows)
    a1 = rows[0]
    assert a1["status"] == "ready"          # retried after failing
    assert a1["failure_class"] == "gate"
    assert a1["branch_name"] == "task/a/a1"
    assert a1["commit_sha"].startswith("deadbeef1")
    checks = json.loads(a1["checks_json"])
    assert checks[0]["cmd"] == "pytest t1" and "FAILED test_1" in checks[0]["tail"]
    assert a1["error_excerpt"] == "tail of attempt 1"
    assert a1["lease_token"]                # dispatch fence recorded
    assert a1["created_at"]                 # append time recorded
    assert rows[2]["status"] == "failed"    # attempts exhausted


def test_success_attempt_recorded(tmp_path):
    conn = _db(tmp_path)
    _, tid = _task(conn)
    _run_attempt(conn, tid, 1, outcome="success", gate_passed=True)
    rows = repo_attempts.get_attempts(conn, tid)
    assert len(rows) == 1 and rows[0]["status"] == "success"
    assert rows[0]["failure_class"] == ""


# --------------------------------------------------------- replay idempotency
def test_replayed_result_does_not_duplicate_or_overwrite(tmp_path):
    conn = _db(tmp_path)
    _, tid = _task(conn)
    row = repo_tasks.claim_for_dispatch(conn, tid, 60)
    repo_tasks.on_started(conn, tid, row["attempt"], row["lease_token"], "e", "s")
    result = {"evidence": {"checks": [{"cmd": "pytest", "ok": False, "exit": 1}]},
              "branch": "b1", "commit_sha": "c1", "log_tail": "first"}
    assert repo_tasks.on_result(conn, tid, row["attempt"], row["lease_token"],
                                 "failed", False, result) is not None
    # replay the exact same result event (same attempt + lease token)
    again = repo_tasks.on_result(conn, tid, row["attempt"], row["lease_token"],
                                  "failed", False, {**result, "log_tail": "REPLAYED"})
    assert again is None                    # fenced: state machine ignored it
    rows = repo_attempts.get_attempts(conn, tid)
    assert len(rows) == 1                   # not duplicated
    assert rows[0]["error_excerpt"] == "first"   # not overwritten


def test_record_attempt_is_idempotent_on_task_and_attempt_no(tmp_path):
    conn = _db(tmp_path)
    _, tid = _task(conn)
    kw = dict(task_id=tid, workflow_id="wf", attempt_no=1, lease_token="L",
              status="ready", failure_class="gate",
              checks=[{"cmd": "pytest", "exit": 1}], result={"a": 1},
              branch_name="b1", commit_sha="c1")
    assert repo_attempts.record_attempt(conn, **kw) is True
    assert repo_attempts.record_attempt(conn, **{**kw, "status": "failed",
                                                 "branch_name": "b9"}) is False
    rows = repo_attempts.get_attempts(conn, tid)
    assert len(rows) == 1
    assert rows[0]["status"] == "ready" and rows[0]["branch_name"] == "b1"


# ------------------------------------------------- bounded cumulative summary
def test_cumulative_summary_bounded_and_includes_every_attempt(tmp_path):
    conn = _db(tmp_path)
    _, tid = _task(conn, max_attempts=5)
    for n in range(1, 6):
        _run_attempt(conn, tid, n)
    max_chars = 600
    s = repo_attempts.cumulative_retry_summary(conn, tid, max_chars=max_chars)
    assert len(s) <= max_chars
    for n in range(1, 6):
        assert f"attempt {n}" in s          # every attempt present
        assert f"/a{n}" in s                # its branch ref present
    assert "gate" in s                      # failure class present
    assert "pytest t3" in s                 # failed check present
    assert s.index("attempt 1") < s.index("attempt 5")   # oldest first


def test_cumulative_summary_empty_without_history(tmp_path):
    conn = _db(tmp_path)
    assert repo_attempts.cumulative_retry_summary(conn, "no-such-task") == ""


def test_dispatch_brief_carries_cumulative_history(tmp_path):
    """The retry brief's prior_failure block merges ALL prior attempts."""
    from types import SimpleNamespace

    from clams.db import repo_outbox
    from clams.planner.dispatch import dispatch_ready

    conn = _db(tmp_path)
    _, tid = _task(conn, max_attempts=3)
    # dispatch_ready only reads these two settings; the bus is not touched
    # (the outbox is the only publish path), so no redis/pydantic-settings.
    s = SimpleNamespace(lease_dispatch_s=60, stream_tasks="tasks")
    bus = None

    def fail_current(n):
        row = repo_tasks.get_task(conn, tid)
        return repo_tasks.on_result(conn, tid, row["attempt"],
                                     row["lease_token"], "failed", False,
                                     _result(n))

    dispatch_ready(conn, bus, s)                    # attempt 1
    repo_outbox.claim_due(conn)                     # drain publish
    assert fail_current(1) is not None
    dispatch_ready(conn, bus, s)                    # attempt 2
    repo_outbox.claim_due(conn)
    assert fail_current(2) is not None
    dispatch_ready(conn, bus, s)                    # attempt 3 dispatches
    msg = json.loads(repo_outbox.claim_due(conn)[0]["payload"])["msg"]
    assert msg["attempt"] == 3
    pf = msg["brief"]["prior_failure"]
    assert "attempt 1" in pf and "attempt 2" in pf  # cumulative, not just latest
    assert "deadbeef1" in pf and "deadbeef2" in pf  # branch/commit refs
    # task contract unchanged across retries
    b = msg["brief"]
    assert b["objective"] == "obj" and b["acceptance"] == "acc"
    assert b["run"] == ["pytest -q"] and b["writable"] == ["*.py"]
