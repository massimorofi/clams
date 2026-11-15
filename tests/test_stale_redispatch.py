"""Issue #117: failed or abandoned tasks were never re-dispatched.

``step_dispatch`` only iterates items in ``planned``, so a requirement that had
already flipped to ``dispatched`` was never revisited. A task that died left
``attempts`` at 0, the ``max_attempts`` cap was never reached, and
``status:Blocked`` -- applied only at the cap -- was never set.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from clams.bus import InMemoryBus, STREAM_TASKS, read_result, read_task
from clams.config import Settings
from clams.db import WorkItem, open_store
from clams.orchestrator import CycleReport, Orchestrator

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def env(tmp_path):
    s = Settings(paths={
        "data_dir": str(tmp_path / "d"),
        "db_path": str(tmp_path / "d" / "stale.db"),
        "work_root": str(tmp_path / "work"),
        "repo_path": str(tmp_path),
        "heartbeat_dir": str(tmp_path / "d" / "hb"),
    }, limits={"max_attempts": 3, "max_wall_seconds": 60,
               "stale_task_grace_seconds": 0},
       executor={"lease_seconds": 900})
    store = open_store(s.paths.db_path)
    bus = InMemoryBus()
    orch = Orchestrator(store, bus, s, default_project_id=0)
    store.work_items.upsert(WorkItem(
        requirement_number=36, requirement_title="Req 36", gh_status="In Progress",
        gh_updated_at="2026-11-15T00:00:00Z", task_numbers=[43, 44],
        status="dispatched", project_id=0))
    yield dict(s=s, store=store, bus=bus, orch=orch)
    store.db.close()


def _expire(store, task, project_id=0, minutes=15):
    """Simulate a worker that died mid-task: lease in the past, no result."""
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    store.task_claims.claim(task, 36, "executor-1", lease_seconds=1,
                            project_id=project_id)
    store.db.conn.execute(
        "UPDATE task_claims SET lease_expires_at = ? WHERE project_id=? AND task_number=?",
        (when.strftime("%Y-%m-%dT%H:%M:%SZ"), project_id, task))


async def _queued_tasks(bus):
    out = []
    while True:
        b = await read_task(bus, "executors", "probe", count=1, block_ms=10)
        if b is None:
            return out
        out.append(b.task_issue)


async def test_expired_lease_is_re_dispatched(env):
    store, bus, orch = env["store"], env["bus"], env["orch"]
    _expire(store, 43)
    rep = CycleReport()
    await orch.step_reap_stale(rep)
    assert 43 in rep.dispatched
    assert 43 in await _queued_tasks(bus)
    row = store.task_claims.get(43)
    assert int(row["attempts"]) == 1


async def test_live_lease_is_left_alone(env):
    store, bus, orch = env["store"], env["bus"], env["orch"]
    store.task_claims.claim(43, 36, "executor-1", lease_seconds=900)
    rep = CycleReport()
    await orch.step_reap_stale(rep)
    assert rep.dispatched == []
    assert await _queued_tasks(bus) == []


async def test_reported_task_is_not_re_dispatched(env):
    store, bus, orch = env["store"], env["bus"], env["orch"]
    _expire(store, 43)
    orch.record_result.__self__  # noqa: B018 - keep attribute check honest
    from clams.contracts import Evidence, ExecutionResult
    orch.record_result(ExecutionResult(task_issue=43, status="completed",
                                       summary="ok",
                                       evidence=Evidence(commands=[])))
    rep = CycleReport()
    await orch.step_reap_stale(rep)
    assert rep.dispatched == []


async def test_attempts_cap_blocks_the_requirement(env):
    """3 abandoned attempts => a failure result lands and the gate blocks."""
    store, bus, orch = env["store"], env["bus"], env["orch"]
    for _ in range(3):
        _expire(store, 43)
        await orch.step_reap_stale(CycleReport())
    assert int(store.task_claims.get(43)["attempts"]) == 3
    # The third pass published a synthetic failure onto the results stream.
    res = await read_result(bus, "planner", "probe", count=1, block_ms=10)
    assert res is not None and res.status == "failed"
    assert "abandoned" in res.summary


async def test_no_claim_row_waits_for_the_grace_window(env):
    """A task merely queued must not be counted as lost immediately."""
    store, bus, orch = env["store"], env["bus"], env["orch"]
    env["s"].limits.stale_task_grace_seconds = 3600
    rep = CycleReport()
    await orch.step_reap_stale(rep)
    assert rep.dispatched == []


async def test_no_claim_row_past_grace_is_re_dispatched(env):
    store, bus, orch = env["store"], env["bus"], env["orch"]
    # Backdate the requirement so the "dispatched but never claimed" window has
    # clearly elapsed (lease + wall + grace).
    old = (datetime.now(timezone.utc) - timedelta(days=1)
           ).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.db.conn.execute("UPDATE work_items SET updated_at = ?", (old,))
    rep = CycleReport()
    await orch.step_reap_stale(rep)
    assert sorted(rep.dispatched) == [43, 44]
    assert sorted(await _queued_tasks(bus)) == [43, 44]


async def test_paused_requirement_is_not_reaped(env):
    store, bus, orch = env["store"], env["bus"], env["orch"]
    _expire(store, 43)
    store.work_items.set_paused(36, True, 0)
    rep = CycleReport()
    await orch.step_reap_stale(rep)
    assert rep.dispatched == []


# ---- acceptance: a task that fails 3x ends up status:Blocked -------------

class FakeGH:
    def __init__(self):
        self.comments = []
        self.statuses = []

    async def add_comment(self, number, body):
        self.comments.append((number, body))
        return {}

    async def set_status(self, number, status):
        self.statuses.append((number, status))
        return []


@pytest.mark.asyncio
async def test_three_abandoned_attempts_block_the_requirement(tmp_path):
    from clams.review import ReviewService
    s = Settings(paths={
        "data_dir": str(tmp_path / "d"),
        "db_path": str(tmp_path / "d" / "block.db"),
        "work_root": str(tmp_path / "work"),
        "repo_path": str(tmp_path),
        "heartbeat_dir": str(tmp_path / "d" / "hb"),
    }, limits={"max_attempts": 3, "max_wall_seconds": 60})
    store = open_store(s.paths.db_path)
    store.work_items.upsert(WorkItem(
        requirement_number=40, requirement_title="Req 40", gh_status="In Progress",
        gh_updated_at="2026-11-15T00:00:00Z", task_numbers=[47],
        status="dispatched", project_id=0))
    gh = FakeGH()
    bus = InMemoryBus()
    orch = Orchestrator(store, bus, s, review=ReviewService(store, gh, s))

    for _ in range(3):
        store.task_claims.claim(47, 40, "executor-1", lease_seconds=1)
        store.db.conn.execute(
            "UPDATE task_claims SET lease_expires_at = '2020-01-01T00:00:00Z' "
            "WHERE task_number = 47")
        await orch.step_reap_stale(CycleReport())
        await orch.step_review_results(CycleReport())

    assert store.work_items.get(40, 0).status == "blocked", \
        "requirement must leave 'dispatched' once its task is given up on"
    assert (40, "status:Blocked") in gh.statuses
    store.db.close()
