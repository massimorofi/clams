"""WP2: outbox relay — backoff, dead, crash recovery, no dupes."""
from __future__ import annotations

import json

import pytest

from clams.bus import InMemoryBus
from clams.db import repo_outbox
from clams.outbox import OutboxRelay


class BoomBus(InMemoryBus):
    def __init__(self):
        super().__init__()
        self.fail = True

    def publish(self, stream, payload):
        if self.fail:
            raise ConnectionError("redis down")
        return super().publish(stream, payload)


def test_relay_publishes_bus_messages(db):
    bus = InMemoryBus()
    relay = OutboxRelay(bus)
    repo_outbox.enqueue(db, "bus_publish",
                        {"stream": "clams:tasks", "msg": {"hello": 1}}, "k1")
    out = relay.relay_once(db)
    assert out["sent"] == 1
    assert bus.xlen("clams:tasks") == 1


def test_relay_backoff_then_dead(db):
    bus = BoomBus()
    relay = OutboxRelay(bus, backoff_base_s=1, max_attempts=3)
    oid = repo_outbox.enqueue(db, "bus_publish",
                              {"stream": "s", "msg": {}}, "k-boom")
    r1 = relay.relay_once(db)
    assert r1["failed"] == 1
    # not due immediately (backoff)
    assert relay.relay_once(db) == {"sent": 0, "failed": 0, "dead": 0}
    db.execute("UPDATE outbox SET next_attempt_at='2000-01-01 00:00:00'")
    relay.relay_once(db)
    db.execute("UPDATE outbox SET next_attempt_at='2000-01-01 00:00:00'")
    r3 = relay.relay_once(db)
    assert r3["dead"] == 1
    from clams.db import repo_alerts
    assert repo_alerts.open_count(db) == 1


def test_kill_relay_midbatch_no_loss_no_dupes(db):
    """Crash after claim but before mark: reclaim re-delivers; consumer-side
    idempotency (processed_events) keeps application exactly-once."""
    bus = InMemoryBus()
    relay = OutboxRelay(bus)
    repo_outbox.enqueue(db, "bus_publish", {"stream": "s", "msg": {"i": 1}}, "k-a")
    repo_outbox.enqueue(db, "bus_publish", {"stream": "s", "msg": {"i": 2}}, "k-b")
    # simulate crash: claim rows then die before marking
    rows = repo_outbox.claim_due(db, batch=1)
    assert len(rows) == 1
    # relay restarts: stale inflight reclaimed, both delivered
    db.execute("UPDATE outbox SET next_attempt_at='2000-01-01 00:00:00' "
               "WHERE status='inflight'")
    out = relay.relay_once(db)
    assert out["sent"] == 2
    assert bus.xlen("s") == 2
    # enqueue dedup: re-inserting same key is a no-op
    assert repo_outbox.enqueue(db, "bus_publish", {}, "k-a") is None


def test_unknown_kind_dead(db):
    bus = InMemoryBus()
    relay = OutboxRelay(bus, max_attempts=1)
    repo_outbox.enqueue(db, "gh_comment", {"repo": "o/r", "issue": 1,
                                           "body": "x"}, "k-g")
    out = relay.relay_once(db)  # no gh client -> fail -> dead (max_attempts=1)
    assert out["dead"] == 1


def test_gh_dispatch_with_client(db):
    calls = []

    class FakeGH:
        def add_comment(self, repo, issue, body, idempotency_key=""):
            calls.append(("comment", repo, issue, body, idempotency_key))

    relay = OutboxRelay(InMemoryBus(), gh_client=FakeGH())
    repo_outbox.enqueue(db, "gh_comment",
                        {"repo": "o/r", "issue": 5, "body": "done",
                         "idempotency_key": "gh:gh_comment:t:1:done"}, "k1")
    assert relay.relay_once(db)["sent"] == 1
    assert calls[0][1:3] == ("o/r", 5)
