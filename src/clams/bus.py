"""Two-stream Redis bus wrapper with strict ack discipline (§4).

Streams: ``clams:tasks`` (planner -> executors, group ``executors``) and
``clams:events`` (executors -> planner, group ``planner``). ``clams:dlq`` is an
auxiliary dead-letter stream, not a work queue.

Ack rule: a consumer XACKs only after the DB transaction that applies the
message has committed. Start-up recovery: replay own PEL, then XAUTOCLAIM
entries idle beyond the threshold. After ``max_deliveries`` deliveries a message
is moved to the DLQ.

``InMemoryBus`` implements the identical interface for unit tests.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

try:
    import redis as _redis
except ImportError:  # pragma: no cover
    _redis = None

FIELD = "m"  # single message field; value is the JSON envelope


@dataclass
class Delivered:
    id: str
    payload: str
    deliveries: int = 1


class Bus:
    """Interface shared by RedisBus and InMemoryBus."""

    def publish(self, stream: str, payload: str) -> str: ...
    def read(self, stream: str, group: str, consumer: str, *, count: int = 10,
             block_ms: int = 0) -> list[Delivered]: ...
    def ack(self, stream: str, group: str, *ids: str) -> int: ...
    def autoclaim(self, stream: str, group: str, consumer: str,
                  min_idle_ms: int, count: int = 100) -> list[Delivered]: ...
    def replay_pending(self, stream: str, group: str, consumer: str,
                       count: int = 100) -> list[Delivered]: ...
    def pending_count(self, stream: str, group: str) -> int: ...
    def xlen(self, stream: str) -> int: ...
    def push_dlq(self, dlq_stream: str, payload: str, reason: str) -> str: ...
    def ping(self) -> bool: ...


class RedisBus(Bus):
    def __init__(self, url: str):
        if _redis is None:
            raise RuntimeError("redis package not installed")
        self._r = _redis.Redis.from_url(url, decode_responses=True)

    def ensure_group(self, stream: str, group: str) -> None:
        try:
            self._r.xgroup_create(stream, group, id="0", mkstream=True)
        except _redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def publish(self, stream: str, payload: str) -> str:
        return self._r.xadd(stream, {FIELD: payload})

    def read(self, stream: str, group: str, consumer: str, *, count: int = 10,
             block_ms: int = 0) -> list[Delivered]:
        resp = self._r.xreadgroup(group, consumer, {stream: ">"},
                                  count=count, block=block_ms or None)
        out: list[Delivered] = []
        for _stream, entries in resp or []:
            for msg_id, fields in entries:
                out.append(Delivered(id=msg_id, payload=fields.get(FIELD, ""),
                                     deliveries=1))
        if out:
            # real delivery counts (for DLQ-after-N); one XPENDING round-trip
            try:
                pend = self._r.xpending_range(stream, group, min="-", max="+",
                                              count=1000)
                counts = {p["message_id"]: p["times_delivered"] for p in pend}
                for d in out:
                    d.deliveries = counts.get(d.id, d.deliveries)
            except Exception:  # noqa: BLE001 - counts are best-effort
                pass
        return out

    def ack(self, stream: str, group: str, *ids: str) -> int:
        return int(self._r.xack(stream, group, *ids)) if ids else 0

    def autoclaim(self, stream: str, group: str, consumer: str,
                  min_idle_ms: int, count: int = 100) -> list[Delivered]:
        out: list[Delivered] = []
        start = "0-0"
        while True:
            next_start, entries, _deleted = self._r.xautoclaim(
                stream, group, consumer, min_idle_time=min_idle_ms,
                start_id=start, count=count)
            for msg_id, fields in entries:
                out.append(Delivered(id=msg_id, payload=fields.get(FIELD, "")))
            if next_start == "0-0" or not next_start:
                break
            start = next_start
        return out

    def replay_pending(self, stream: str, group: str, consumer: str,
                       count: int = 100) -> list[Delivered]:
        resp = self._r.xreadgroup(group, consumer, {stream: "0"}, count=count)
        out: list[Delivered] = []
        for _stream, entries in resp or []:
            for msg_id, fields in entries:
                out.append(Delivered(id=msg_id, payload=fields.get(FIELD, "")))
        return out

    def pending_count(self, stream: str, group: str) -> int:
        info = self._r.xpending(stream, group)
        return int(info["pending"]) if info else 0

    def xlen(self, stream: str) -> int:
        return int(self._r.xlen(stream))

    def push_dlq(self, dlq_stream: str, payload: str, reason: str) -> str:
        return self._r.xadd(dlq_stream, {FIELD: payload, "reason": reason})

    def ping(self) -> bool:
        try:
            return bool(self._r.ping())
        except Exception:
            return False


@dataclass
class _MemEntry:
    id: str
    payload: str
    seq: int


@dataclass
class _MemGroup:
    last_seq: int = 0
    #: msg_id -> (consumer, delivered_at_monotonic, deliveries)
    pending: dict[str, tuple[str, float, int]] = field(default_factory=dict)


class InMemoryBus(Bus):
    """Deterministic in-process bus with PEL + autoclaim semantics."""

    def __init__(self) -> None:
        self._streams: dict[str, list[_MemEntry]] = {}
        self._groups: dict[tuple[str, str], _MemGroup] = {}
        self._seq = 0

    # -- helpers ---------------------------------------------------------
    def _next_id(self) -> tuple[str, int]:
        self._seq += 1
        return f"{self._seq}-1", self._seq

    def ensure_group(self, stream: str, group: str) -> None:
        self._groups.setdefault((stream, group), _MemGroup())
        self._streams.setdefault(stream, [])

    # -- Bus interface ---------------------------------------------------
    def publish(self, stream: str, payload: str) -> str:
        msg_id, seq = self._next_id()
        self._streams.setdefault(stream, []).append(
            _MemEntry(id=msg_id, payload=payload, seq=seq))
        return msg_id

    def read(self, stream: str, group: str, consumer: str, *, count: int = 10,
             block_ms: int = 0) -> list[Delivered]:
        g = self._groups.setdefault((stream, group), _MemGroup())
        entries = self._streams.get(stream, [])
        out: list[Delivered] = []
        for e in entries:
            if e.seq <= g.last_seq:
                continue
            g.last_seq = e.seq
            deliveries = g.pending.get(e.id, (consumer, 0.0, 0))[2] + 1
            g.pending[e.id] = (consumer, time.monotonic(), deliveries)
            out.append(Delivered(id=e.id, payload=e.payload,
                                 deliveries=deliveries))
            if len(out) >= count:
                break
        return out

    def ack(self, stream: str, group: str, *ids: str) -> int:
        g = self._groups.get((stream, group))
        if not g:
            return 0
        n = 0
        for i in ids:
            if g.pending.pop(i, None) is not None:
                n += 1
        return n

    def autoclaim(self, stream: str, group: str, consumer: str,
                  min_idle_ms: int, count: int = 100) -> list[Delivered]:
        g = self._groups.get((stream, group))
        if not g:
            return []
        now = time.monotonic()
        out: list[Delivered] = []
        by_seq = {e.id: e.seq for e in self._streams.get(stream, [])}
        for msg_id in sorted(g.pending, key=lambda i: by_seq.get(i, 0)):
            owner, seen, deliveries = g.pending[msg_id]
            if owner == consumer:
                continue
            if (now - seen) * 1000 < min_idle_ms:
                continue
            deliveries += 1
            g.pending[msg_id] = (consumer, now, deliveries)
            entry = next((e for e in self._streams[stream] if e.id == msg_id),
                         None)
            out.append(Delivered(id=msg_id,
                                 payload=entry.payload if entry else "",
                                 deliveries=deliveries))
            if len(out) >= count:
                break
        return out

    def replay_pending(self, stream: str, group: str, consumer: str,
                       count: int = 100) -> list[Delivered]:
        g = self._groups.get((stream, group))
        if not g:
            return []
        by_seq = {e.id: e.seq for e in self._streams.get(stream, [])}
        out: list[Delivered] = []
        for msg_id in sorted(g.pending, key=lambda i: by_seq.get(i, 0)):
            owner, _seen, deliveries = g.pending[msg_id]
            if owner != consumer:
                continue
            entry = next((e for e in self._streams[stream] if e.id == msg_id),
                         None)
            out.append(Delivered(id=msg_id,
                                 payload=entry.payload if entry else "",
                                 deliveries=deliveries))
            if len(out) >= count:
                break
        return out

    def pending_count(self, stream: str, group: str) -> int:
        g = self._groups.get((stream, group))
        return len(g.pending) if g else 0

    def xlen(self, stream: str) -> int:
        return len(self._streams.get(stream, []))

    def push_dlq(self, dlq_stream: str, payload: str, reason: str) -> str:
        msg_id, seq = self._next_id()
        self._streams.setdefault(dlq_stream, []).append(
            _MemEntry(id=msg_id, payload=f"{reason}||{payload}", seq=seq))
        return msg_id

    def ping(self) -> bool:
        return True


def make_bus(url: str) -> Bus:
    """Factory: real Redis bus."""
    return RedisBus(url)
