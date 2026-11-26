"""WP4: dispatch — promote+claim+outbox in one tx; idempotent keys."""
from __future__ import annotations

import json

from clams.bus import InMemoryBus
from clams.config import Settings
from clams.db import repo_outbox, repo_tasks, repo_workflows, repo_projects
from clams.planner.dispatch import dispatch_ready


def _wf_tasks(db, n=2):
    pid = repo_projects.upsert_project(db, "o/r")
    wf = repo_workflows.create_workflow(db, pid, "adhoc", "g")
    ids = []
    for i in range(1, n + 1):
        brief = {"objective": "o", "acceptance": "a", "run": ["echo x"],
                 "writable": ["*.py"], "repo": "o/r", "base_ref": "main"}
        ids.append(repo_tasks.create_task(db, wf, i, f"t{i}", "o", "a", brief,
                                          depends_on=[i - 1] if i > 1 else []))
    return wf, ids


def test_dispatch_respects_deps_and_queues(db):
    s = Settings(_env_file=None)
    bus = InMemoryBus()
    wf, (t1, t2) = _wf_tasks(db)
    got = dispatch_ready(db, bus, s)
    assert got == [t1]                       # t2 blocked on t1
    assert repo_tasks.get_task(db, t1)["status"] == "dispatched"
    rows = repo_outbox.claim_due(db)
    assert len(rows) == 1 and rows[0]["kind"] == "bus_publish"
    payload = json.loads(rows[0]["payload"])
    msg = payload["msg"]
    assert msg["task_id"] == t1 and msg["attempt"] == 1
    assert msg["brief"]["repo"] == "o/r"
    # second dispatch call: nothing new until t1 succeeds
    assert dispatch_ready(db, bus, s) == []


def test_dispatch_idempotent_key_per_attempt(db):
    s = Settings(_env_file=None)
    bus = InMemoryBus()
    wf, (t1,) = _wf_tasks(db, n=1)
    dispatch_ready(db, bus, s)
    # simulate lease expiry requeue -> new attempt -> new key
    repo_tasks.transition(db, t1, "ready")
    dispatch_ready(db, bus, s)
    row = repo_tasks.get_task(db, t1)
    assert row["attempt"] == 2
    keys = [r["idempotency_key"] for r in db.execute(
        "SELECT idempotency_key FROM outbox").fetchall()]
    assert keys.count(f"dispatch:{t1}:1") == 0  # old bare-attempt format gone
    assert len([k for k in keys if k.startswith(f"dispatch:{t1}:1:")]) == 1
    assert len([k for k in keys if k.startswith(f"dispatch:{t1}:2:")]) == 1


def test_dispatch_after_retry_reset_republishes(db):
    """Regression: retry resets attempt to 0; the republish must NOT dedup
    against the original attempt-1 publish (lease token makes keys unique)."""
    s = Settings(_env_file=None)
    bus = InMemoryBus()
    wf, (t1,) = _wf_tasks(db, n=1)
    dispatch_ready(db, bus, s)
    # operator retry: status ready, attempt reset to 0 (see commands._apply)
    db.execute("UPDATE tasks SET status='ready', attempt=0, lease_token=NULL, "
               "lease_expires_at=NULL WHERE task_id=?", (t1,))
    got = dispatch_ready(db, bus, s)
    assert got == [t1]
    pubs = db.execute("SELECT COUNT(*) AS n FROM outbox "
                      "WHERE kind='bus_publish'").fetchone()["n"]
    assert pubs == 2  # original + retry republish


def test_dispatch_reopens_workflow_when_dependents_unblock(db):
    """claudio#133: a retry revived the head task and unblock_stranded revived
    its dependents, but the workflow stayed 'blocked' — so review_workflow
    skipped it forever and it could never reach done, however many tasks then
    succeeded."""
    s = Settings(_env_file=None)
    bus = InMemoryBus()
    wf, (t1, t2) = _wf_tasks(db)
    for st in ("ready", "dispatched", "running", "failed"):
        repo_tasks.transition(db, t1, st)
    from clams.planner.review import strand_dependents
    strand_dependents(db, wf)
    repo_workflows.set_status(db, wf, "blocked")
    assert repo_tasks.get_task(db, t2)["status"] == "blocked"

    db.execute("UPDATE tasks SET status='success' WHERE task_id=?", (t1,))
    got = dispatch_ready(db, bus, s)
    assert t2 in got
    assert repo_workflows.get_workflow(db, wf)["status"] == "running"
