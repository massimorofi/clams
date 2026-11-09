"""S3 — event bus tests (in-memory bus; Redis parity covered in test_bus_redis.py)."""
from __future__ import annotations

import json

import pytest

from clams.bus import (
    InMemoryBus,
    STREAM_ALERTS,
    STREAM_COMMANDS,
    STREAM_DEAD,
    STREAM_RESULTS,
    STREAM_TASKS,
    publish_alert,
    publish_command,
    publish_result,
    publish_task,
    read_alert,
    read_command,
    read_result,
    read_task,
)
from clams.contracts import (
    AlertEvent,
    CommandEnvelope,
    ExecutionResult,
    TaskBrief,
)

BRIEF = TaskBrief(
    task_issue=42,
    requirement_issue=17,
    objective="Implement POST /runs/{id}/messages",
    acceptance_criterion="pytest exits 0",
)


async def test_publish_and_read_task():
    bus = InMemoryBus()
    await publish_task(bus, BRIEF)
    got = await read_task(bus, "executors", "w1")
    assert got is not None
    assert got.task_issue == 42
    assert got.scope.writable == []


async def test_read_advances_cursor():
    bus = InMemoryBus()
    await publish_task(bus, BRIEF)
    assert (await read_task(bus, "g", "c")).task_issue == 42
    assert await read_task(bus, "g", "c") is None


async def test_two_groups_each_see_message():
    bus = InMemoryBus()
    await publish_task(bus, BRIEF)
    a = await read_task(bus, "group-a", "c1")
    b = await read_task(bus, "group-b", "c1")
    assert a is not None and b is not None


async def test_result_roundtrip():
    bus = InMemoryBus()
    res = ExecutionResult(
        task_issue=42,
        status="completed",
        summary="ok",
    )
    await publish_result(bus, res)
    got = await read_result(bus, "planners", "p1")
    assert got is not None
    assert got.status == "completed"


async def test_alert_roundtrip():
    bus = InMemoryBus()
    await publish_alert(bus, AlertEvent(severity="warning", source="wd",
                                       message="slow", timestamp="2026-11-08T00:00:00Z"))
    got = await read_alert(bus, "g", "c")
    assert got is not None and got.severity == "warning"


async def test_command_roundtrip():
    bus = InMemoryBus()
    await publish_command(bus, CommandEnvelope(action="pause", raw_text="pause execution",
                                             issued_at="2026-11-08T00:00:00Z"))
    got = await read_command(bus, "g", "c")
    assert got is not None and got.action == "pause"


async def test_malformed_payload_dead_letters_not_crash():
    bus = InMemoryBus()
    await bus.publish(STREAM_TASKS, {"not": "a task brief"}, msg_type="TaskBrief")
    got = await read_task(bus, "g", "c")
    assert got is None
    assert len(bus.dead) == 1
    assert bus.dead[0]["stream"] == STREAM_TASKS
    assert "TaskBrief" in bus.dead[0]["reason"]


async def test_garbage_json_dead_letters():
    bus = InMemoryBus()
    await bus.publish(STREAM_RESULTS, "this is not json {{{", msg_type="ExecutionResult")
    got = await read_result(bus, "g", "c")
    assert got is None
    assert len(bus.dead) == 1


async def test_pending_counts_unread():
    bus = InMemoryBus()
    for i in range(3):
        await publish_task(bus, BRIEF)
    p = await bus.pending(STREAM_TASKS, "g")
    assert p["count"] == 3
    await read_task(bus, "g", "c")
    p = await bus.pending(STREAM_TASKS, "g")
    assert p["count"] == 2


async def test_drain_returns_all():
    bus = InMemoryBus()
    await publish_task(bus, BRIEF)
    await publish_task(bus, BRIEF)
    items = await bus.drain(STREAM_TASKS)
    assert len(items) == 2


async def test_purge_clears_stream():
    bus = InMemoryBus()
    await publish_task(bus, BRIEF)
    await bus.purge(STREAM_TASKS)
    assert await bus.drain(STREAM_TASKS) == []


async def test_published_json_is_valid_json():
    bus = InMemoryBus()
    await publish_task(bus, BRIEF)
    raw = bus.messages[STREAM_TASKS][0].payload
    assert isinstance(raw, dict)
    assert raw["task_issue"] == 42
