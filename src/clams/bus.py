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


class Bus(Protocol):
    """Event bus interface used by every service."""

    async def publish(self, stream: str, payload: Any, *, msg_type: str = "") -> str: ...
    async def read(self, stream: str, group: str, consumer: str, *,
                  count: int = 1, block_ms: int = 1000) -> list[Received]: ...
    async def ack(self, stream: str, group: str, message_id: str) -> None: ...
    async def dead_letter(self, stream: str, raw: Any, reason: str) -> None: ...
    async def pending(self, stream: str, group: str) -> dict[str, Any]: ...
    async def drain(self, stream: str) -> list[Received]: ...


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

    async def publish(self, stream: str, payload: Any, *, msg_type: str = "") -> str:
        client = await self.connect()
        if isinstance(payload, BaseModel_):  # pydantic model
            body = payload.model_dump_json()
            msg_type = msg_type or type(payload).__name__
        elif isinstance(payload, str):
            body = payload
        else:
            body = json.dumps(payload)
        data = {PAYLOAD_FIELD: body, TYPE_FIELD: msg_type}
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
        out: list[Received] = []
        if not resp:
            return out
        for _stream_name, entries in resp:
            for msg_id, fields in entries:
                out.append(
                    Received(
                        message_id=msg_id,
                        type=(fields or {}).get(TYPE_FIELD, ""),
                        payload=_decode((fields or {}).get(PAYLOAD_FIELD, "{}")),
                    )
                )
        return out

    async def ack(self, stream: str, group: str, message_id: str) -> None:
        client = await self.connect()
        await client.xack(self._name(stream), group, message_id)

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
        client = await self.connect()
        name = self._name(stream)
        try:
            info = await client.xpending(name, group)
        except Exception:
            return {"count": 0, "oldest_idle_ms": 0}
        if not info:
            return {"count": 0, "oldest_idle_ms": 0}
        count = int(info.get("pending", 0) if isinstance(info, dict) else info[0])
        oldest_idle_ms = 0
        pending_detail = await self._pending_detail(name, group)
        if pending_detail:
            oldest_idle_ms = max(p["idle_ms"] for p in pending_detail)
        return {"count": count, "oldest_idle_ms": oldest_idle_ms}

    async def _pending_detail(self, name: str, group: str) -> list[dict[str, Any]]:
        client = await self.connect()
        try:
            resp = await client.xpending(name, group, "-", "+", 100)
        except Exception:
            return []
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
    """Drop-in bus for tests: same interface, no Redis."""

    def __init__(self) -> None:
        self.messages: dict[str, list[Received]] = {}
        self._cursor: dict[tuple[str, str], int] = {}
        self._counter = 0
        self.dead: list[dict[str, Any]] = []

    def _next_id(self) -> str:
        self._counter += 1
        return f"{self._counter}-0"

    async def publish(self, stream: str, payload: Any, *, msg_type: str = "") -> str:
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
        self.messages.setdefault(stream, []).append(msg)
        return msg.message_id

    async def ensure_group(self, stream: str, group: str) -> None:
        self._cursor.setdefault((stream, group), 0)

    async def read(self, stream: str, group: str, consumer: str, *,
                  count: int = 1, block_ms: int = 1000) -> list[Received]:
        key = (stream, group)
        idx = self._cursor.get(key, 0)
        items = self.messages.get(stream, [])[idx: idx + count]
        self._cursor[key] = idx + len(items)
        return items

    async def ack(self, stream: str, group: str, message_id: str) -> None:
        return None

    async def dead_letter(self, stream: str, raw: Any, reason: str) -> None:
        self.dead.append({"stream": stream, "raw": raw, "reason": reason})

    async def pending(self, stream: str, group: str) -> dict[str, Any]:
        key = (stream, group)
        idx = self._cursor.get(key, 0)
        total = len(self.messages.get(stream, []))
        return {"count": max(0, total - idx), "oldest_idle_ms": 0}

    async def drain(self, stream: str) -> list[Received]:
        return list(self.messages.get(stream, []))

    async def purge(self, stream: str) -> None:
        self.messages[stream] = []
        for k in list(self._cursor):
            if k[0] == stream:
                self._cursor[k] = 0


# ---- typed helpers ----------------------------------------------------

async def publish_task(bus: Bus, brief: TaskBrief) -> str:
    return await bus.publish(STREAM_TASKS, brief, msg_type="TaskBrief")


async def read_task(bus: Bus, group: str, consumer: str, **kw) -> Optional[TaskBrief]:
    items = await bus.read(STREAM_TASKS, group, consumer, **kw)
    if not items:
        return None
    msg = items[0]
    try:
        brief = TaskBrief.from_json(msg.payload if isinstance(msg.payload, str)
                                  else json.dumps(msg.payload))
    except (ContractError, json.JSONDecodeError) as exc:
        await bus.dead_letter(STREAM_TASKS, msg.payload, f"TaskBrief decode failed: {exc}")
        await bus.ack(STREAM_TASKS, group, msg.message_id)
        return None
    return brief


async def publish_result(bus: Bus, result: ExecutionResult) -> str:
    return await bus.publish(STREAM_RESULTS, result, msg_type="ExecutionResult")


async def read_result(bus: Bus, group: str, consumer: str, **kw) -> Optional[ExecutionResult]:
    items = await bus.read(STREAM_RESULTS, group, consumer, **kw)
    if not items:
        return None
    msg = items[0]
    try:
        return ExecutionResult.from_json(
            msg.payload if isinstance(msg.payload, str) else json.dumps(msg.payload)
        )
    except (ContractError, json.JSONDecodeError) as exc:
        await bus.dead_letter(STREAM_RESULTS, msg.payload, f"ExecutionResult decode failed: {exc}")
        await bus.ack(STREAM_RESULTS, group, msg.message_id)
        return None


async def publish_alert(bus: Bus, alert: AlertEvent) -> str:
    return await bus.publish(STREAM_ALERTS, alert, msg_type="AlertEvent")


async def read_alert(bus: Bus, group: str, consumer: str, **kw) -> Optional[AlertEvent]:
    items = await bus.read(STREAM_ALERTS, group, consumer, **kw)
    if not items:
        return None
    msg = items[0]
    try:
        return AlertEvent.from_json(
            msg.payload if isinstance(msg.payload, str) else json.dumps(msg.payload)
        )
    except (ContractError, json.JSONDecodeError) as exc:
        await bus.dead_letter(STREAM_ALERTS, msg.payload, f"AlertEvent decode failed: {exc}")
        await bus.ack(STREAM_ALERTS, group, msg.message_id)
        return None


async def publish_command(bus: Bus, cmd: CommandEnvelope) -> str:
    return await bus.publish(STREAM_COMMANDS, cmd, msg_type="CommandEnvelope")


async def read_command(bus: Bus, group: str, consumer: str, **kw) -> Optional[CommandEnvelope]:
    items = await bus.read(STREAM_COMMANDS, group, consumer, **kw)
    if not items:
        return None
    msg = items[0]
    try:
        return CommandEnvelope.from_json(
            msg.payload if isinstance(msg.payload, str) else json.dumps(msg.payload)
        )
    except (ContractError, json.JSONDecodeError) as exc:
        await bus.dead_letter(STREAM_COMMANDS, msg.payload, f"CommandEnvelope decode failed: {exc}")
        await bus.ack(STREAM_COMMANDS, group, msg.message_id)
        return None


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
