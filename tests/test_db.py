"""S1 — SQLite state layer tests."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from clams.db import (
    AlertRepo,
    Database,
    OutboxRepo,
    Store,
    TaskClaimRepo,
    WorkItem,
    WorkItemRepo,
    iso,
    open_store,
    parse_iso,
)


@pytest.fixture()
def store(tmp_path):
    s = open_store(tmp_path / "t.db")
    yield s
    s.db.close()


def test_wal_mode_enabled(store):
    assert store.db.journal_mode().lower() == "wal"


def test_pragmas_applied(store):
    conn = store.db.conn
    assert int(conn.execute("PRAGMA synchronous;").fetchone()[0]) == 2  # FULL
    assert int(conn.execute("PRAGMA busy_timeout;").fetchone()[0]) == 5000
    assert int(conn.execute("PRAGMA wal_autocheckpoint;").fetchone()[0]) == 1000
    assert int(conn.execute("PRAGMA foreign_keys;").fetchone()[0]) == 1


def test_migrate_is_idempotent(store):
    store.db.migrate()
    store.db.migrate()
    assert store.work_items.all() == []


def test_iso_roundtrip():
    dt = datetime(2026, 11, 8, 12, 34, 56, tzinfo=timezone.utc)
    s = iso(dt)
    assert s == "2026-11-08T12:34:56Z"
    assert parse_iso(s) == dt


def test_workitem_upsert_and_get(store):
    item = WorkItem(
        requirement_number=12,
        requirement_title="Add JWT auth",
        gh_status="Ready",
        gh_updated_at=iso(),
    )
    store.work_items.upsert(item)
    got = store.work_items.get(12)
    assert got is not None
    assert got.requirement_title == "Add JWT auth"
    assert got.status == "pending"
    assert got.task_numbers == []

    got.status = "planned"
    got.task_numbers = [3, 4, 5]
    store.work_items.upsert(got)
    again = store.work_items.get(12)
    assert again.status == "planned"
    assert again.task_numbers == [3, 4, 5]


def test_workitem_list_by_status(store):
    for n in (1, 2, 3):
        store.work_items.upsert(
            WorkItem(requirement_number=n, requirement_title=f"R{n}",
                    gh_status="Ready", gh_updated_at=iso(), status="pending")
        )
    store.work_items.set_status(2, "planned")
    assert [w.requirement_number for w in store.work_items.list_by_status("pending")] == [1, 3]
    assert [w.requirement_number for w in store.work_items.list_by_status("planned")] == [2]


def test_pause_flag(store):
    store.work_items.upsert(WorkItem(requirement_number=7, requirement_title="x",
                                    gh_status="Ready", gh_updated_at=iso()))
    assert store.work_items.is_paused(7) is False
    store.work_items.set_paused(7, True)
    assert store.work_items.is_paused(7) is True
    store.work_items.set_paused(7, False)
    assert store.work_items.is_paused(7) is False


def test_set_plan_and_replans(store):
    store.work_items.upsert(WorkItem(requirement_number=9, requirement_title="x",
                                    gh_status="Ready", gh_updated_at=iso()))
    plan = json.dumps({"tasks": []})
    store.work_items.set_plan(9, plan, [10, 11, 12])
    w = store.work_items.get(9)
    assert w.plan_json == plan
    assert w.task_numbers == [10, 11, 12]
    assert w.status == "planned"
    assert store.work_items.increment_replans(9) == 1
    assert store.work_items.increment_replans(9) == 2


def test_claim_requirement_is_atomic(store):
    results = []

    def worker(i):
        s = open_store(store.db.path)
        try:
            won = s.work_items.claim_requirement(100, "req", iso())
            results.append(won)
        finally:
            s.db.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1
    assert results.count(False) == 7


def test_task_claim_lifecycle(store):
    store.work_items.upsert(WorkItem(requirement_number=1, requirement_title="r",
                                    gh_status="In Progress", gh_updated_at=iso()))
    assert store.task_claims.claim(50, 1, "exec-A", 60) is True
    # second claim fails while lease is live
    assert store.task_claims.claim(50, 1, "exec-B", 60) is False
    row = store.task_claims.get(50)
    assert row["executor_id"] == "exec-A"
    assert store.task_claims.renew(50, "exec-A", 120) is True
    assert store.task_claims.renew(50, "exec-B", 120) is False
    store.task_claims.release(50)
    assert store.task_claims.get(50) is None


def test_expired_lease_can_be_reclaimed(store):
    store.work_items.upsert(WorkItem(requirement_number=1, requirement_title="r",
                                    gh_status="In Progress", gh_updated_at=iso()))
    # insert an already-expired claim directly
    past = iso(datetime.now(timezone.utc) - timedelta(seconds=10))
    store.db.conn.execute(
        "INSERT INTO task_claims (task_number, requirement_number, executor_id, lease_expires_at,"
        " attempts, max_attempts, claimed_at) VALUES (60, 1, 'ghost', ?, 1, 3, ?)",
        (past, past),
    )
    assert len(store.task_claims.expired()) == 1
    assert store.task_claims.claim(60, 1, "exec-C", 60) is True
    assert store.task_claims.get(60)["executor_id"] == "exec-C"


def test_attempts_increment(store):
    store.work_items.upsert(WorkItem(requirement_number=1, requirement_title="r",
                                    gh_status="In Progress", gh_updated_at=iso()))
    store.task_claims.claim(70, 1, "e", 60)
    assert store.task_claims.increment_attempts(70) == 1
    assert store.task_claims.increment_attempts(70) == 2


def test_alerts(store):
    aid = store.alerts.add("critical", "watchdog", "engine down")
    assert aid >= 1
    assert len(store.alerts.list_unacked()) == 1
    store.alerts.ack(aid)
    assert store.alerts.list_unacked() == []
    assert store.alerts.count() == 1
    with pytest.raises(ValueError):
        store.alerts.add("bogus", "x", "y")


def test_alert_severity_constraint_at_db_level(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.db.conn.execute(
            "INSERT INTO alerts (severity, source, message, acknowledged, created_at)"
            " VALUES ('nope','s','m',0,'2026-01-01T00:00:00Z')"
        )


def test_outbox_enqueue_and_claim(store):
    oid = store.outbox.enqueue("gh_comment", {"issue": 5, "body": "hi"}, "c-5-1")
    assert oid >= 1
    claimed = store.outbox.claim_pending()
    assert len(claimed) == 1
    assert claimed[0]["kind"] == "gh_comment"
    # nothing left pending
    assert store.outbox.claim_pending() == []
    assert store.outbox.stats()["sent"] == 1


def test_outbox_idempotency_key(store):
    a = store.outbox.enqueue("gh_comment", {"issue": 1}, "same-key")
    b = store.outbox.enqueue("gh_comment", {"issue": 1}, "same-key")
    assert a == b
    assert store.db.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1


def test_outbox_invalid_kind(store):
    with pytest.raises(ValueError):
        store.outbox.enqueue("bogus", {}, "k")


def test_outbox_failed_not_reclaimed_after_max(store):
    oid = store.outbox.enqueue("gh_status", {"issue": 2}, "k2")
    for _ in range(3):
        store.outbox.claim_pending()
    store.outbox.mark(oid, "failed")
    assert store.outbox.claim_pending() == []


def test_foreign_key_enforced(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.db.conn.execute(
            "INSERT INTO task_claims (task_number, requirement_number, executor_id,"
            " lease_expires_at, attempts, max_attempts, claimed_at)"
            " VALUES (1, 9999, 'e', '2030-01-01T00:00:00Z', 0, 3, '2026-01-01T00:00:00Z')"
        )
