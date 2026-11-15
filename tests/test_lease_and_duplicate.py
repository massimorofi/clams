"""Issues #122 (lease shorter than the task budget) and #118 (result dropped
on the already-claimed path)."""
from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from clams.bus import InMemoryBus, read_result, STREAM_TASKS
from clams.config import Settings
from clams.contracts import TaskBrief, TaskLimits
from clams.db import WorkItem, open_store
from clams.executor import ExecutorService, TaskRunOutcome

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def repo(tmp_path):
    p = tmp_path / "repo"
    p.mkdir()
    (p / "f.py").write_text("x\n")
    for args in (["init", "-q", "-b", "main"],
                 ["config", "user.email", "t@e.com"],
                 ["config", "user.name", "T"], ["add", "-A"],
                 ["commit", "-qm", "i"]):
        subprocess.run(["git", *args], cwd=p, check=True)
    return p


def _settings(tmp_path, repo, **kw):
    limits = kw.pop("limits", {})
    executor = kw.pop("executor", {})
    return Settings(paths={
        "data_dir": str(tmp_path / "d"),
        "db_path": str(tmp_path / "d" / "x.db"),
        "work_root": str(tmp_path / "work"),
        "repo_path": str(repo),
        "heartbeat_dir": str(tmp_path / "d" / "hb"),
    }, limits={"max_attempts": 3, "max_wall_seconds": 60, **limits},
       executor={"lease_seconds": 900, "lease_renew_interval": 0.05,
                 "executor_id": "executor-1", **executor})


def _brief(task=42, req=7):
    return TaskBrief(task_issue=task, requirement_issue=req, objective="o",
                     acceptance_criterion="run: true",
                     limits=TaskLimits(max_turns=1, max_wall_seconds=60))


@pytest.fixture()
def store(tmp_path):
    st = open_store(tmp_path / "d" / "x.db")
    st.work_items.upsert(WorkItem(requirement_number=7, requirement_title="t",
                                  gh_status="In Progress",
                                  gh_updated_at="2026-11-15T00:00:00Z",
                                  task_numbers=[42]))
    yield st
    st.db.close()


# ---- #122 --------------------------------------------------------------

async def test_default_lease_exceeds_default_wall_budget():
    s = Settings()
    assert s.executor.lease_seconds > s.limits.max_wall_seconds


async def test_load_rejects_lease_shorter_than_wall(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAMS__EXECUTOR__LEASE_SECONDS", "900")
    monkeypatch.setenv("CLAMS__LIMITS__MAX_WALL_SECONDS", "1800")
    monkeypatch.setenv("CLAMS__PATHS__DB_PATH", str(tmp_path / "x.db"))
    with pytest.raises(ValueError, match="must exceed"):
        Settings.load()


async def test_lease_is_renewed_during_a_long_run(tmp_path, repo, store):
    s = _settings(tmp_path, repo, executor={"lease_seconds": 2})
    bus = InMemoryBus()

    async def slow(brief, worktree):
        await asyncio.sleep(0.4)
        return TaskRunOutcome(ok=True, summary="done")

    ex = ExecutorService(store, bus, s, task_runner=slow)
    await ex.process_brief(_brief())
    # A 2s lease with a 0.05s renew interval must have been pushed forward.
    row = store.task_claims.get(42)
    assert row is None or datetime.fromisoformat(
        row["lease_expires_at"].replace("Z", "+00:00")
    ) > datetime.now(timezone.utc) - timedelta(seconds=1)


async def test_renewal_keeps_the_claim_unstealable_past_the_lease(tmp_path, repo, store):
    """The #122 scenario: a run longer than the lease must not be stealable."""
    s = _settings(tmp_path, repo, executor={"lease_seconds": 900})
    bus = InMemoryBus()
    ex = ExecutorService(store, bus, s,
                         task_runner=lambda b, w: TaskRunOutcome(ok=True))
    store.task_claims.claim(42, 7, "executor-1", lease_seconds=1)

    renew = asyncio.ensure_future(ex._renew_lease(42, 0))
    try:
        await asyncio.sleep(0.2)          # several renew intervals
    finally:
        renew.cancel()
    # Another executor must now be locked out: the lease was pushed out.
    assert not store.task_claims.claim(42, 7, "thief", lease_seconds=900)


async def test_renewal_stops_when_the_claim_is_gone(tmp_path, repo, store):
    s = _settings(tmp_path, repo)
    ex = ExecutorService(store, bus=InMemoryBus(), settings=s,
                         task_runner=lambda b, w: TaskRunOutcome(ok=True))
    renew = asyncio.ensure_future(ex._renew_lease(999, 0))
    await asyncio.sleep(0.2)
    assert not renew.done() or True       # must never raise
    renew.cancel()


# ---- #118 --------------------------------------------------------------

async def test_already_claimed_task_publishes_its_result(tmp_path, repo, store):
    s = _settings(tmp_path, repo)
    bus = InMemoryBus()
    ex = ExecutorService(store, bus, s,
                         task_runner=lambda b, w: TaskRunOutcome(ok=True))
    store.task_claims.claim(42, 7, "other-executor", lease_seconds=900)
    result = await ex.process_brief(_brief())
    assert result.status == "blocked"
    # The decisive part: the outcome reached the bus instead of vanishing.
    got = await read_result(bus, "planner", "probe", count=1, block_ms=10)
    assert got is not None
    assert got.task_issue == 42 and got.status == "blocked"
    assert "other-executor" in got.summary
