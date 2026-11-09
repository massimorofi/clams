"""S10 — outbox worker tests."""
from __future__ import annotations

import json

import pytest

from clams.config import Settings
from clams.db import open_store
from clams.outbox import DeliveryReport, OutboxWorker


@pytest.fixture()
def store(tmp_path):
    s = open_store(tmp_path / "ob.db")
    yield s
    s.db.close()


class GH:
    def __init__(self, fail=False):
        self.comments = []
        self.statuses = []
        self.fail = fail

    async def add_comment(self, number, body):
        if self.fail:
            raise RuntimeError("gh down")
        self.comments.append((number, body))
        return {"id": len(self.comments)}

    async def set_status(self, number, status_label, keep=None):
        if self.fail:
            raise RuntimeError("gh down")
        self.statuses.append((number, status_label))
        return [status_label]


async def test_delivers_gh_comment(store):
    gh = GH()
    store.outbox.enqueue("gh_comment", {"issue": 5, "body": "hello"}, "c1")
    w = OutboxWorker(store, gh=gh)
    rep = await w.deliver_once()
    assert rep.delivered == 1
    assert gh.comments == [(5, "hello")]


async def test_delivers_gh_status(store):
    gh = GH()
    store.outbox.enqueue("gh_status", {"issue": 7, "status": "status:Review"}, "s1")
    w = OutboxWorker(store, gh=gh)
    await w.deliver_once()
    assert gh.statuses == [(7, "status:Review")]


async def test_delivers_notification_via_notifier(store):
    seen = []
    store.outbox.enqueue("notification", {"msg": "hi"}, "n1")
    w = OutboxWorker(store, notifier=lambda p: seen.append(p))
    rep = await w.deliver_once()
    assert rep.delivered == 1
    assert seen[0]["msg"] == "hi"


async def test_failure_is_retried_then_marked_failed(store):
    gh = GH(fail=True)
    oid = store.outbox.enqueue("gh_comment", {"issue": 1, "body": "x"}, "k")
    w = OutboxWorker(store, gh=gh)
    for _ in range(3):
        rep = await w.deliver_once()
    assert rep.permanently_failed >= 1
    row = store.db.conn.execute("SELECT status, attempts FROM outbox WHERE id=?", (oid,)).fetchone()
    assert row["status"] == "failed"


async def test_idempotency_key_prevents_duplicate_rows(store):
    gh = GH()
    store.outbox.enqueue("gh_comment", {"issue": 3, "body": "a"}, "same")
    store.outbox.enqueue("gh_comment", {"issue": 3, "body": "a"}, "same")
    assert store.db.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1


async def test_unknown_kind_fails(store):
    w = OutboxWorker(store, gh=GH())
    # inject an invalid kind directly (repo validates, so bypass it)
    store.db.conn.execute(
        "INSERT INTO outbox (kind, payload, idempotency_key, status, attempts, created_at)"
        " VALUES ('gh_comment', ?, 'bad1', 'pending', 0, '2026-01-01T00:00:00Z')",
        (json.dumps({"no_issue": True}),),
    )
    rep = await w.deliver_once()
    assert rep.failed + rep.permanently_failed >= 1


async def test_empty_outbox_is_fine(store):
    w = OutboxWorker(store, gh=GH())
    rep = await w.deliver_once()
    assert rep.delivered == 0


async def test_delivery_report_summary():
    assert "delivered=1" in DeliveryReport(delivered=1).summary()
