"""WP2: bus ack discipline, PEL, XAUTOCLAIM, DLQ — InMemoryBus."""
from __future__ import annotations

import time

import pytest

from clams.bus import InMemoryBus


def test_publish_read_ack():
    b = InMemoryBus()
    b.ensure_group("s", "g")
    b.publish("s", "m1")
    b.publish("s", "m2")
    got = b.read("s", "g", "c1", count=10)
    assert [d.payload for d in got] == ["m1", "m2"]
    assert b.pending_count("s", "g") == 2
    b.ack("s", "g", got[0].id)
    assert b.pending_count("s", "g") == 1
    # already-delivered messages are not re-read with ">"
    assert b.read("s", "g", "c1", count=10) == []


def test_replay_pending():
    b = InMemoryBus()
    b.ensure_group("s", "g")
    b.publish("s", "m1")
    first = b.read("s", "g", "c1")
    replay = b.replay_pending("s", "g", "c1")
    assert [d.id for d in replay] == [d.id for d in first]
    # another consumer's pending is not replayed to c2
    assert b.replay_pending("s", "g", "c2") == []


def test_autoclaim_after_idle():
    b = InMemoryBus()
    b.ensure_group("s", "g")
    b.publish("s", "m1")
    first = b.read("s", "g", "dead-executor")
    # not idle yet
    assert b.autoclaim("s", "g", "c2", min_idle_ms=50) == []
    time.sleep(0.06)
    claimed = b.autoclaim("s", "g", "c2", min_idle_ms=50)
    assert [d.payload for d in claimed] == ["m1"]
    assert claimed[0].deliveries == 2
    # c2 now owns it; c1's ack of a foreign id does nothing
    assert b.ack("s", "g", first[0].id) == 1


def test_dlq_push():
    b = InMemoryBus()
    b.push_dlq("clams:dlq", "poison", "undecodable")
    assert b.xlen("clams:dlq") == 1


def test_deliveries_counter():
    b = InMemoryBus()
    b.ensure_group("s", "g")
    b.publish("s", "x")
    assert b.read("s", "g", "c1")[0].deliveries == 1
    time.sleep(0.02)
    assert b.autoclaim("s", "g", "c2", min_idle_ms=10)[0].deliveries == 2


# ------------------------------------------------------------- real Redis
@pytest.mark.redis
def test_redis_bus_roundtrip():
    import os
    from clams.bus import RedisBus
    url = os.environ.get("CLAMS_REDIS_URL", "redis://127.0.0.1:6379/0")
    b = RedisBus(url)
    if not b.ping():
        pytest.skip("no redis")
    stream, group = "clams:test:tasks", "tgrp"
    # clean slate
    b._r.delete(stream)
    b.ensure_group(stream, group)
    b.publish(stream, "hello")
    got = b.read(stream, group, "c1", count=10, block_ms=100)
    assert [d.payload for d in got] == ["hello"]
    assert b.pending_count(stream, group) == 1
    # unacked -> autoclaim to another consumer after idle
    claimed = b.autoclaim(stream, group, "c2", min_idle_ms=0)
    assert [d.payload for d in claimed] == ["hello"]
    b.ack(stream, group, got[0].id)
    assert b.pending_count(stream, group) == 0
    b._r.delete(stream)
