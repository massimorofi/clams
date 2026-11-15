"""Issue #114: Redis messages were never acked, so in-flight work vanished.

Two halves:

* the success path must ack -- but only *after* the work is durable, otherwise
  a crash between read and process loses the message just as hard;
* a worker that starts up must pick up whatever its consumer group still has
  parked in the pending-entries list.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from clams.bus import (InMemoryBus, STREAM_RESULTS, STREAM_TASKS,
                       publish_result, publish_task, read_result, read_task,
                       redeliver_results, redeliver_tasks, take_result,
                       take_task)
from clams.config import Settings
from clams.contracts import Evidence, ExecutionResult, TaskBrief, TaskLimits
from clams.db import open_store
from clams.executor import ExecutorService, TaskRunOutcome

pytestmark = pytest.mark.asyncio


def _brief(n: int = 42) -> TaskBrief:
    return TaskBrief(task_issue=n, requirement_issue=7, objective="do it",
                     acceptance_criterion="run: true",
                     limits=TaskLimits(max_turns=1, max_wall_seconds=5))


def _result(n: int = 42) -> ExecutionResult:
    return ExecutionResult(task_issue=n, status="completed", summary="ok",
                           evidence=Evidence(commands=[]))


# ---- bus level ---------------------------------------------------------

async def test_take_leaves_message_in_pel_until_acked():
    bus = InMemoryBus()
    await publish_task(bus, _brief())
    d = await take_task(bus, "executors", "e1", count=1, block_ms=10)
    assert d is not None and d.item.task_issue == 42
    # Delivered but not acked: still pending, exactly like Redis' PEL.
    assert await bus.pel_count(STREAM_TASKS, "executors") == 1
    await d.ack()
    assert await bus.pel_count(STREAM_TASKS, "executors") == 0


async def test_redeliver_returns_unacked_entries():
    bus = InMemoryBus()
    await publish_task(bus, _brief(11))
    await publish_task(bus, _brief(12))
    first = await take_task(bus, "executors", "e1", count=1, block_ms=10)
    await first.ack()
    second = await take_task(bus, "executors", "e1", count=1, block_ms=10)
    assert second is not None and second.item.task_issue == 12
    pending = await redeliver_tasks(bus, "executors", "e1")
    assert [d.item.task_issue for d in pending] == [12]
    # Redelivery does not consume: the entry stays until it is acked.
    assert len(await redeliver_tasks(bus, "executors", "e1")) == 1
    for d in pending:
        await d.ack()
    assert await redeliver_tasks(bus, "executors", "e1") == []


async def test_read_helpers_ack_immediately():
    bus = InMemoryBus()
    await publish_task(bus, _brief())
    assert (await read_task(bus, "executors", "e1", count=1, block_ms=10)) is not None
    assert await bus.pel_count(STREAM_TASKS, "executors") == 0
    await publish_result(bus, _result())
    assert (await read_result(bus, "planner", "o1", count=1, block_ms=10)) is not None
    assert await bus.pel_count(STREAM_RESULTS, "planner") == 0


async def test_pending_count_drains_to_zero():
    bus = InMemoryBus()
    for i in range(5):
        await publish_task(bus, _brief(10 + i))
    assert (await bus.pending(STREAM_TASKS, "executors"))["count"] == 5
    while True:
        d = await take_task(bus, "executors", "e1", count=1, block_ms=10)
        if d is None:
            break
        await d.ack()
    assert (await bus.pending(STREAM_TASKS, "executors"))["count"] == 0


# ---- executor level ----------------------------------------------------

@pytest.fixture()
def env(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "hello.py").write_text("print('hello')\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)

    s = Settings(paths={
        "data_dir": str(tmp_path / "d"),
        "db_path": str(tmp_path / "d" / "ack.db"),
        "work_root": str(tmp_path / "work"),
        "repo_path": str(repo),
        "heartbeat_dir": str(tmp_path / "d" / "hb"),
    })
    store = open_store(s.paths.db_path)
    # task_claims has a FK onto work_items, so the requirement must exist.
    store.work_items.claim_requirement(7, "Req 7", "2026-11-15T00:00:00Z")
    bus = InMemoryBus()
    runs: list[int] = []

    async def runner(brief, worktree):
        runs.append(brief.task_issue)
        return TaskRunOutcome(ok=True, summary="done")

    ex = ExecutorService(store, bus, s, task_runner=runner,
                         executor_id="executor-1")
    yield dict(s=s, store=store, bus=bus, ex=ex, runs=runs)
    store.db.close()


async def test_executor_acks_brief_after_processing(env):
    bus, ex, runs = env["bus"], env["ex"], env["runs"]
    await publish_task(bus, _brief(42))
    await ex.run_once()
    assert runs == [42]
    assert await bus.pel_count(STREAM_TASKS, "executors") == 0


async def test_restart_mid_task_redelivers_and_completes(env):
    """The exact #114 scenario: claimed, worker died, nothing was ever seen again."""
    bus, ex, runs = env["bus"], env["ex"], env["runs"]
    await publish_task(bus, _brief(45))
    # Worker claims the brief and dies before acking (simulated crash).
    crashed = await take_task(bus, "executors", "executor-1", count=1, block_ms=10)
    assert crashed is not None
    assert runs == []
    assert await bus.pel_count(STREAM_TASKS, "executors") == 1

    # A fresh worker starts up and recovers what the dead one left behind.
    recovered = await ex.recover_pending()
    assert recovered == 1
    assert runs == [45]
    assert await bus.pel_count(STREAM_TASKS, "executors") == 0
    # And it is not replayed a second time.
    assert await ex.recover_pending() == 0


async def test_recovery_survives_a_failing_task(env):
    """A task that raises must still be acked, not replayed forever."""
    bus, ex = env["bus"], env["ex"]

    async def boom(brief, worktree):
        raise RuntimeError("agent exploded")
    ex.task_runner = boom
    await publish_task(bus, _brief(47))
    await ex.run_once()
    assert await bus.pel_count(STREAM_TASKS, "executors") == 0
    # The failure produced a result on the results stream.
    res = await read_result(bus, "planner", "o1", count=1, block_ms=10)
    assert res is not None and res.status == "failed"


# ---- orchestrator level ------------------------------------------------

async def test_orchestrator_acks_results_after_recording(tmp_path):
    from clams.orchestrator import CycleReport, Orchestrator
    s = Settings(paths={
        "data_dir": str(tmp_path / "d"),
        "db_path": str(tmp_path / "d" / "orch.db"),
        "work_root": str(tmp_path / "work"),
        "repo_path": str(tmp_path),
        "heartbeat_dir": str(tmp_path / "d" / "hb"),
    })
    store = open_store(s.paths.db_path)
    bus = InMemoryBus()
    orch = Orchestrator(store, bus, s)
    await publish_result(bus, _result(77))
    await orch.step_review_results(CycleReport())
    assert await bus.pel_count(STREAM_RESULTS, "planner") == 0
    # The result landed in the outbox, so it is not lost with the ack.
    rows = store.db.conn.execute(
        "SELECT COUNT(*) c FROM outbox WHERE kind='gh_comment'").fetchone()["c"]
    assert rows == 1
    store.db.close()


async def test_orchestrator_recovers_unacked_results(tmp_path):
    from clams.orchestrator import Orchestrator
    s = Settings(paths={
        "data_dir": str(tmp_path / "d"),
        "db_path": str(tmp_path / "d" / "orch2.db"),
        "work_root": str(tmp_path / "work"),
        "repo_path": str(tmp_path),
        "heartbeat_dir": str(tmp_path / "d" / "hb"),
    })
    store = open_store(s.paths.db_path)
    bus = InMemoryBus()
    orch = Orchestrator(store, bus, s)
    await publish_result(bus, _result(78))
    crashed = await take_result(bus, "planner", "orchestrator", count=1, block_ms=10)
    assert crashed is not None                      # claimed, never recorded
    assert await orch.recover_pending_results() == 1
    assert await bus.pel_count(STREAM_RESULTS, "planner") == 0
    rows = store.db.conn.execute(
        "SELECT COUNT(*) c FROM outbox WHERE kind='gh_comment'").fetchone()["c"]
    assert rows == 1
    store.db.close()


# ---- real Redis (skipped when unavailable) -----------------------------

REDIS_URL = os.environ.get("CLAMS_TEST_REDIS_URL", "redis://127.0.0.1:6379/9")


async def _redis_bus():
    pytest.importorskip("redis")
    from clams.bus import RedisBus
    bus = RedisBus(REDIS_URL, namespace="clamstest")
    try:
        await bus.connect()
    except Exception as exc:                       # no local redis -> skip
        pytest.skip(f"no redis available: {exc}")
    return bus


async def test_redis_redeliver_and_ack_roundtrip():
    bus = await _redis_bus()
    try:
        await bus.purge(STREAM_TASKS)
        await bus.ensure_group(STREAM_TASKS, "executors")
        await publish_task(bus, _brief(91))
        d = await take_task(bus, "executors", "e1", count=1, block_ms=50)
        assert d is not None
        assert (await bus.pending(STREAM_TASKS, "executors"))["count"] == 1
        again = await redeliver_tasks(bus, "executors", "e1")
        assert [x.item.task_issue for x in again] == [91]
        for x in again:
            await x.ack()
        assert (await bus.pending(STREAM_TASKS, "executors"))["count"] == 0
    finally:
        await bus.purge(STREAM_TASKS)
        await bus.close()
