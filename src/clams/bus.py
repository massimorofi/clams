"""Redis Streams event bus (spec §1.1, §4.2, §6.1 queue checks).

Streams
  clams:tasks    Planner -> Executor   (TaskBrief)
  clams:results  Executor -> Planner  (ExecutionResult)
  clams:alerts   Watchdog -> Planner  (AlertEvent)
  clams:commands User/UI -> Planner   (CommandEnvelope)
  clams:dead     dead-letter for undecodable / poison messages

An ``InMemoryBus`` with the identical interface is provided so unit tests never
need a live Redis.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from pydantic import BaseModel as BaseModel_

from .contracts import (
    AlertEvent,
    CommandEnvelope,
    ContractError,
    ExecutionResult,
    TaskBrief,
)

STREAM_TASKS = "clams:tasks"
STREAM_RESULTS = "clams:results"
STREAM_ALERTS = "clams:alerts"
STREAM_COMMANDS = "clams:commands"
STREAM_DEAD = "clams:dead"

PAYLOAD_FIELD = "payload"
TYPE_FIELD = "type"


@dataclass
class Received:
    message_id: str
    type: str
    payload: dict[str, Any]


class Delivery:
    """A received message plus the handle needed to acknowledge it.

    Issue #114: the old helpers returned the decoded object and dropped the
    message id, so nothing could ack *after* processing. Everything stayed in
    the pending-entries list forever and a restart lost the work in flight.
    Ack when the work is durably dealt with, not before.
    """

    __slots__ = ("item", "message_id", "stream", "group", "_bus")

    def __init__(self, bus: "Bus", stream: str, group: str, message_id: str,
                 item: Any) -> None:
        self._bus = bus
        self.stream = stream
        self.group = group
        self.message_id = message_id
        self.item = item

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Delivery {self.stream} {self.message_id} {type(self.item).__name__}>"

    async def ack(self) -> None:
        await self._bus.ack(self.stream, self.group, self.message_id)


class Bus(Protocol):
    """Event bus interface used by every service."""

    async def publish(self, stream: str, payload: Any, *, msg_type: str = "",
                     maxlen: Optional[int] = None) -> str: ...
    async def read(self, stream: str, group: str, consumer: str, *,
                  count: int = 1, block_ms: int = 1000) -> list[Received]: ...
    async def ack(self, stream: str, group: str, message_id: str) -> None: ...
    async def dead_letter(self, stream: str, raw: Any, reason: str) -> None: ...
    async def pending(self, stream: str, group: str) -> dict[str, Any]: ...
    async def drain(self, stream: str) -> list[Received]: ...
    async def last_id(self, stream: str) -> str: ...
    async def read_since(self, stream: str, last_id: str, *,
                        count: int = 100) -> tuple[list[Received], str]: ...
    async def redeliver(self, stream: str, group: str, consumer: str, *,
                        count: int = 100) -> list[Received]: ...
    async def claim_idle(self, stream: str, group: str, consumer: str,
                        min_idle_ms: int, *,
                        count: int = 100) -> list[Received]: ...


class RedisBus:
    """Redis Streams backed bus with consumer groups and dead-lettering."""

    def __init__(self, url: str = "redis://127.0.0.1:6379/0", namespace: str = "clams"):
        self.url = url
        self.namespace = namespace
        self._client = None

    async def connect(self):
        if self._client is None:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(self.url, decode_responses=True)
            await self._client.ping()
        return self._client

    def _name(self, stream: str) -> str:
        return f"{self.namespace}:{stream.split(':', 1)[-1]}" if stream.startswith("clams:") else stream

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def publish(self, stream: str, payload: Any, *, msg_type: str = "",
                     maxlen: Optional[int] = None) -> str:
        client = await self.connect()
        if isinstance(payload, BaseModel_):  # pydantic model
            body = payload.model_dump_json()
            msg_type = msg_type or type(payload).__name__
        elif isinstance(payload, str):
            body = payload
        else:
            body = json.dumps(payload)
        data = {PAYLOAD_FIELD: body, TYPE_FIELD: msg_type}
        if maxlen:
            return await client.xadd(self._name(stream), data, maxlen=maxlen)  # type: ignore[return-value]
        return await client.xadd(self._name(stream), data)  # type: ignore[return-value]

    async def ensure_group(self, stream: str, group: str) -> None:
        client = await self.connect()
        try:
            await client.xgroup_create(self._name(stream), group, id="0", mkstream=True)
        except Exception as exc:  # BUSYGROUP: already exists
            if "BUSYGROUP" not in str(exc):
                raise

    async def read(self, stream: str, group: str, consumer: str, *,
                  count: int = 1, block_ms: int = 1000) -> list[Received]:
        client = await self.connect()
        await self.ensure_group(stream, group)
        resp = await client.xreadgroup(
            group, consumer, {self._name(stream): ">"}, count=count, block=block_ms
        )
        return _entries(resp)

    async def ack(self, stream: str, group: str, message_id: str) -> None:
        client = await self.connect()
        await client.xack(self._name(stream), group, message_id)

    async def redeliver(self, stream: str, group: str, consumer: str, *,
                        count: int = 100) -> list[Received]:
        """Re-read this consumer's unacknowledged entries (its PEL).

        ``XREADGROUP ... 0`` returns everything the consumer already claimed
        but never acked. That is precisely the set a crashed worker leaves
        behind, so calling this on start-up is what makes a restart lose
        nothing (issue #114).
        """
        client = await self.connect()
        await self.ensure_group(stream, group)
        try:
            resp = await client.xreadgroup(
                group, consumer, {self._name(stream): "0"}, count=count
            )
        except Exception:
            return []
        return _entries(resp)

    async def claim_idle(self, stream: str, group: str, consumer: str,
                         min_idle_ms: int, *, count: int = 100) -> list[Received]:
        """Steal entries idle for longer than ``min_idle_ms`` from dead consumers.

        A message parked in a *different* consumer's PEL is invisible to
        ``redeliver``. ``XAUTOCLAIM`` with a min-idle above the task lease
        recovers those without racing a worker that is still running.
        """
        client = await self.connect()
        await self.ensure_group(stream, group)
        try:
            resp = await client.xautoclaim(self._name(stream), group, consumer,
                                           min_idle_ms, "0-0", count=count)
        except Exception:
            return []
        if not resp:
            return []
        # redis-py returns (next_start_id, [[id, fields], ...], [deleted_ids])
        entries = resp[1] if isinstance(resp, (list, tuple)) and len(resp) >= 2 else []
        return _entries([(self._name(stream), entries)])

    async def dead_letter(self, stream: str, raw: Any, reason: str) -> None:
        client = await self.connect()
        await client.xadd(
            self._name(STREAM_DEAD),
            {
                PAYLOAD_FIELD: raw if isinstance(raw, str) else json.dumps(raw),
                "source_stream": self._name(stream),
                "reason": reason,
                "dead_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )

    async def pending(self, stream: str, group: str) -> dict[str, Any]:
        """Queue depth plus the age of the oldest unacked entry.

        Issue #120: failures used to be swallowed and reported as
        ``oldest_idle_ms = 0``, so a queue backed up for 16 hours read as
        healthy. An error is now returned explicitly for the watchdog to raise.
        """
        client = await self.connect()
        name = self._name(stream)
        try:
            info = await client.xpending(name, group)
        except Exception as exc:
            if "NOGROUP" in str(exc):
                # No consumer group yet simply means nothing has been consumed.
                return {"count": 0, "oldest_idle_ms": 0}
            return {"count": 0, "oldest_idle_ms": 0,
                    "error": f"xpending: {type(exc).__name__}: {exc}"}
        if not info:
            return {"count": 0, "oldest_idle_ms": 0}
        count = int(info.get("pending", 0) if isinstance(info, dict) else info[0])
        oldest_idle_ms = 0
        try:
            pending_detail = await self._pending_detail(name, group)
        except Exception as exc:
            return {"count": count, "oldest_idle_ms": 0,
                    "error": f"xpending_range: {type(exc).__name__}: {exc}"}
        if pending_detail:
            oldest_idle_ms = max(p["idle_ms"] for p in pending_detail)
        return {"count": count, "oldest_idle_ms": oldest_idle_ms}

    async def _pending_detail(self, name: str, group: str) -> list[dict[str, Any]]:
        """Per-entry PEL detail.

        The async redis-py client exposes the ranged form as
        ``xpending_range``; the old ``xpending(name, group, "-", "+", 100)``
        call raised ``TypeError`` on every invocation and, because the caller
        swallowed it, the queue age was always reported as zero (#120).
        """
        client = await self.connect()
        resp = await client.xpending_range(name, group, min="-", max="+", count=100)
        out = []
        for entry in resp or []:
            # entry: [msg_id, consumer, idle_time_ms, delivery_count]
            if isinstance(entry, dict):
                out.append({
                    "id": entry.get("message_id"),
                    "idle_ms": int(entry.get("time_since_delivered", 0)),
                })
            elif isinstance(entry, (list, tuple)) and len(entry) >= 3:
                out.append({"id": entry[0], "idle_ms": int(entry[2] or 0)})
        return out

    async def drain(self, stream: str) -> list[Received]:
        """Read everything currently in the stream (no consumer group)."""
        client = await self.connect()
        try:
            entries = await client.xrange(self._name(stream))
        except Exception:
            return []
        out = []
        for msg_id, fields in entries or []:
            out.append(
                Received(
                    message_id=msg_id,
                    type=(fields or {}).get(TYPE_FIELD, ""),
                    payload=_decode((fields or {}).get(PAYLOAD_FIELD, "{}")),
                )
            )
        return out

    async def last_id(self, stream: str) -> str:
        """Id of the newest entry in the stream, or ``"0-0"`` if empty/missing.

        Used to start a tail read at the current end of the stream so historical
        entries are never replayed to a freshly connected consumer.
        """
        client = await self.connect()
        try:
            rev = await client.xrevrange(self._name(stream), max="+", min="-", count=1)
        except Exception:
            return "0-0"
        if not rev:
            return "0-0"
        return rev[0][0]

    async def read_since(self, stream: str, last_id: str, *,
                        count: int = 100) -> tuple[list[Received], str]:
        """Read only entries newer than ``last_id`` (no consumer group).

        Returns ``(messages, new_last_id)``. ``new_last_id`` advances to the id
        of the last message returned, or stays at ``last_id`` when there are no
        new entries, so the caller can persist it and resume exactly where it
        left off on the next call.
        """
        client = await self.connect()
        try:
            resp = await client.xread({self._name(stream): last_id}, count=count)
        except Exception:
            return [], last_id
        out: list[Received] = []
        new_id = last_id
        if resp:
            for _stream_name, entries in resp:
                for msg_id, fields in entries:
                    out.append(
                        Received(
                            message_id=msg_id,
                            type=(fields or {}).get(TYPE_FIELD, ""),
                            payload=_decode((fields or {}).get(PAYLOAD_FIELD, "{}")),
                        )
                    )
                    new_id = msg_id
        return out, new_id

    async def trim(self, stream: str, maxlen: int = 1000) -> None:
        client = await self.connect()
        await client.xtrim(self._name(stream), maxlen=maxlen)

    async def purge(self, stream: str) -> None:
        client = await self.connect()
        try:
            await client.delete(self._name(stream))
        except Exception:
            pass


# ``BaseModel`` import kept local so the module imports even if pydantic changes.
from pydantic import BaseModel as BaseModel_  # noqa: E402


class InMemoryBus:
    """Drop-in bus for tests: same interface, no Redis.

    It models the part of Redis Streams that mattered for issue #114: a message
    delivered to a consumer group sits in that group's pending-entries list
    until it is acked, and an unacked message is handed back by
    :meth:`redeliver`. Without this the tests could never catch the bug, because
    the old stub simply forgot everything it had delivered.
    """

    def __init__(self) -> None:
        self.messages: dict[str, list[Received]] = {}
        self._cursor: dict[tuple[str, str], int] = {}
        # (stream, group) -> list of (Received, consumer, delivered_at)
        self._pel: dict[tuple[str, str], list[tuple[Received, str, float]]] = {}
        self._counter = 0
        self.dead: list[dict[str, Any]] = []
        self.acked: list[tuple[str, str, str]] = []

    def _next_id(self) -> str:
        self._counter += 1
        return f"{self._counter}-0"

    async def publish(self, stream: str, payload: Any, *, msg_type: str = "",
                     maxlen: Optional[int] = None) -> str:
        if isinstance(payload, BaseModel_):
            body = payload.model_dump_json()
            msg_type = msg_type or type(payload).__name__
        elif isinstance(payload, str):
            body = payload
        else:
            body = json.dumps(payload)
        # Store the decoded dict when possible; keep the raw string otherwise so a
        # malformed message survives publish and is dead-lettered on read.
        try:
            stored = _decode(body)
        except ContractError:
            stored = body  # type: ignore[assignment]
        msg = Received(message_id=self._next_id(), type=msg_type, payload=stored)
        items = self.messages.setdefault(stream, [])
        items.append(msg)
        if maxlen and len(items) > maxlen:
            del items[:-maxlen]
        return msg.message_id

    async def ensure_group(self, stream: str, group: str) -> None:
        self._cursor.setdefault((stream, group), 0)
        self._pel.setdefault((stream, group), [])

    async def read(self, stream: str, group: str, consumer: str, *,
                  count: int = 1, block_ms: int = 1000) -> list[Received]:
        key = (stream, group)
        idx = self._cursor.get(key, 0)
        items = self.messages.get(stream, [])[idx: idx + count]
        self._cursor[key] = idx + len(items)
        now = time.monotonic()
        pel = self._pel.setdefault(key, [])
        for msg in items:
            pel.append((msg, consumer, now))
        return items

    async def ack(self, stream: str, group: str, message_id: str) -> None:
        key = (stream, group)
        pel = self._pel.get(key, [])
        self._pel[key] = [e for e in pel if e[0].message_id != message_id]
        self.acked.append((stream, group, message_id))

    async def redeliver(self, stream: str, group: str, consumer: str, *,
                        count: int = 100) -> list[Received]:
        pel = self._pel.get((stream, group), [])
        return [msg for msg, who, _ts in pel if who == consumer][:count]

    async def claim_idle(self, stream: str, group: str, consumer: str,
                         min_idle_ms: int, *, count: int = 100) -> list[Received]:
        key = (stream, group)
        pel = self._pel.get(key, [])
        cutoff = time.monotonic() - (min_idle_ms / 1000.0)
        claimed: list[Received] = []
        rest: list[tuple[Received, str, float]] = []
        for entry in pel:
            msg, who, ts = entry
            if who != consumer and ts <= cutoff and len(claimed) < count:
                claimed.append(msg)
                rest.append((msg, consumer, ts))
            else:
                rest.append(entry)
        self._pel[key] = rest
        return claimed

    async def dead_letter(self, stream: str, raw: Any, reason: str) -> None:
        self.dead.append({"stream": stream, "raw": raw, "reason": reason})

    async def pending(self, stream: str, group: str) -> dict[str, Any]:
        """``count`` = undelivered + delivered-but-unacked, ``oldest_idle_ms``
        measured from delivery time (#120 needs a real number)."""
        key = (stream, group)
        idx = self._cursor.get(key, 0)
        total = len(self.messages.get(stream, []))
        undelivered = max(0, total - idx)
        pel = self._pel.get(key, [])
        now = time.monotonic()
        oldest_ms = int(max(((now - ts) * 1000.0 for _m, _c, ts in pel), default=0))
        return {"count": undelivered + len(pel), "oldest_idle_ms": oldest_ms}

    async def pel_count(self, stream: str, group: str) -> int:
        return len(self._pel.get((stream, group), []))

    async def drain(self, stream: str) -> list[Received]:
        return list(self.messages.get(stream, []))

    async def last_id(self, stream: str) -> str:
        items = self.messages.get(stream, [])
        return items[-1].message_id if items else "0-0"

    async def read_since(self, stream: str, last_id: str, *,
                        count: int = 100) -> tuple[list[Received], str]:
        items = self.messages.get(stream, [])
        try:
            last_n = int(str(last_id).split("-", 1)[0])
        except (ValueError, AttributeError):
            last_n = 0
        new = [m for m in items if int(str(m.message_id).split("-", 1)[0]) > last_n]
        new = new[:count]
        new_id = new[-1].message_id if new else last_id
        return new, new_id

    async def purge(self, stream: str) -> None:
        self.messages[stream] = []
        self._pel = {k: v for k, v in self._pel.items() if k[0] != stream}
        for k in list(self._cursor):
            if k[0] == stream:
                self._cursor[k] = 0


# ---- typed helpers ----------------------------------------------------

def _payload_str(payload: Any) -> str:
    return payload if isinstance(payload, str) else json.dumps(payload)


async def _take(bus: Bus, stream: str, group: str, consumer: str,
                model: Any, **kw) -> Optional[Delivery]:
    """Read one message and hand back an ack handle *without* acking.

    The caller owns the acknowledgement: it must fire once the work is durably
    dealt with. That ordering is the whole of the #114 fix.
    """
    items = await bus.read(stream, group, consumer, **kw)
    if not items:
        return None
    msg = items[0]
    try:
        item = model.from_json(_payload_str(msg.payload))
    except (ContractError, json.JSONDecodeError) as exc:
        await bus.dead_letter(stream, msg.payload,
                              f"{model.__name__} decode failed: {exc}")
        await bus.ack(stream, group, msg.message_id)
        return None
    return Delivery(bus, stream, group, msg.message_id, item)


async def _redeliver(bus: Bus, stream: str, group: str, consumer: str,
                     model: Any, *, count: int = 100) -> list[Delivery]:
    """Re-take this consumer's unacked entries (see :meth:`Bus.redeliver`)."""
    items = await bus.redeliver(stream, group, consumer, count=count)
    out: list[Delivery] = []
    for msg in items:
        try:
            item = model.from_json(_payload_str(msg.payload))
        except (ContractError, json.JSONDecodeError) as exc:
            await bus.dead_letter(stream, msg.payload,
                                  f"{model.__name__} redeliver decode failed: {exc}")
            await bus.ack(stream, group, msg.message_id)
            continue
        out.append(Delivery(bus, stream, group, msg.message_id, item))
    return out


async def publish_task(bus: Bus, brief: TaskBrief) -> str:
    return await bus.publish(STREAM_TASKS, brief, msg_type="TaskBrief")


async def take_task(bus: Bus, group: str, consumer: str, **kw) -> Optional[Delivery]:
    """``Delivery`` carrying a :class:`TaskBrief`; ack after the task ran."""
    return await _take(bus, STREAM_TASKS, group, consumer, TaskBrief, **kw)


async def redeliver_tasks(bus: Bus, group: str, consumer: str,
                          *, count: int = 100) -> list[Delivery]:
    return await _redeliver(bus, STREAM_TASKS, group, consumer, TaskBrief,
                            count=count)


async def read_task(bus: Bus, group: str, consumer: str, **kw) -> Optional[TaskBrief]:
    """Decode and ack in one step (at-most-once).

    Kept for tests and for readers that genuinely do not need a delivery
    guarantee. Production consumers must use :func:`take_task` so a crash
    mid-work leaves the message in the PEL instead of losing it (#114).
    """
    delivery = await take_task(bus, group, consumer, **kw)
    if delivery is None:
        return None
    await delivery.ack()
    return delivery.item


async def publish_result(bus: Bus, result: ExecutionResult) -> str:
    return await bus.publish(STREAM_RESULTS, result, msg_type="ExecutionResult")


async def take_result(bus: Bus, group: str, consumer: str,
                      **kw) -> Optional[Delivery]:
    return await _take(bus, STREAM_RESULTS, group, consumer, ExecutionResult, **kw)


async def redeliver_results(bus: Bus, group: str, consumer: str,
                            *, count: int = 100) -> list[Delivery]:
    return await _redeliver(bus, STREAM_RESULTS, group, consumer, ExecutionResult,
                            count=count)


async def read_result(bus: Bus, group: str, consumer: str,
                      **kw) -> Optional[ExecutionResult]:
    delivery = await take_result(bus, group, consumer, **kw)
    if delivery is None:
        return None
    await delivery.ack()
    return delivery.item


# Bound the alerts stream so a long-running watchdog cannot grow it without limit.
# The SSE feed tails this stream; an unbounded stream is both a memory leak and the
# reason old alerts used to be replayed into the live feed.
ALERT_STREAM_MAXLEN = 500


async def publish_alert(bus: Bus, alert: AlertEvent) -> str:
    return await bus.publish(STREAM_ALERTS, alert, msg_type="AlertEvent",
                            maxlen=ALERT_STREAM_MAXLEN)


async def take_alert(bus: Bus, group: str, consumer: str, **kw) -> Optional[Delivery]:
    return await _take(bus, STREAM_ALERTS, group, consumer, AlertEvent, **kw)


async def read_alert(bus: Bus, group: str, consumer: str, **kw) -> Optional[AlertEvent]:
    delivery = await take_alert(bus, group, consumer, **kw)
    if delivery is None:
        return None
    await delivery.ack()
    return delivery.item


async def publish_command(bus: Bus, cmd: CommandEnvelope) -> str:
    return await bus.publish(STREAM_COMMANDS, cmd, msg_type="CommandEnvelope")


async def take_command(bus: Bus, group: str, consumer: str,
                       **kw) -> Optional[Delivery]:
    return await _take(bus, STREAM_COMMANDS, group, consumer, CommandEnvelope, **kw)


async def read_command(bus: Bus, group: str, consumer: str,
                       **kw) -> Optional[CommandEnvelope]:
    delivery = await take_command(bus, group, consumer, **kw)
    if delivery is None:
        return None
    await delivery.ack()
    return delivery.item


def _entries(resp: Any) -> list[Received]:
    """Flatten an ``XREAD``/``XREADGROUP`` reply into ``Received`` items."""
    out: list[Received] = []
    if not resp:
        return out
    for _stream_name, entries in resp:
        for msg_id, fields in entries or []:
            out.append(
                Received(
                    message_id=msg_id,
                    type=(fields or {}).get(TYPE_FIELD, ""),
                    payload=_decode((fields or {}).get(PAYLOAD_FIELD, "{}")),
                )
            )
    return out


def _decode(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except json.JSONDecodeError as exc:
            raise ContractError(f"payload is not valid JSON: {exc}") from exc
    raise ContractError(f"unsupported payload type {type(raw)!r}")
