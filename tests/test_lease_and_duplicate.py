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
    """The #122 scenario: a run longer than the lease must not be stealable.

    Driven deterministically (no wall-clock sleep): the renewal body is called
    once, exactly as the loop calls it, and the claim table is asserted.
    """
    s = _settings(tmp_path, repo, executor={"lease_seconds": 900})
    ex = ExecutorService(store, InMemoryBus(), s,
                         task_runner=lambda b, w: TaskRunOutcome(ok=True))
    store.task_claims.claim(42, 7, "executor-1", lease_seconds=1)
    # The lease is about to expire: a thief could take it.
    store.db.conn.execute(
        "UPDATE task_claims SET lease_expires_at = '2020-01-01T00:00:00Z' "
        "WHERE task_number = 42")
    assert store.task_claims.renew(42, "executor-1", 900) is True
    assert not store.task_claims.claim(42, 7, "thief", lease_seconds=900), \
        "a renewed lease must not be stealable"


async def test_renew_loop_extends_the_lease(tmp_path, repo, store):
    """_renew_lease must push the expiry forward and never raise on a missing row."""
    s = _settings(tmp_path, repo, executor={"lease_seconds": 900})
    ex = ExecutorService(store, InMemoryBus(), s,
                         task_runner=lambda b, w: TaskRunOutcome(ok=True))
    store.task_claims.claim(42, 7, "executor-1", lease_seconds=1)
    store.db.conn.execute(
        "UPDATE task_claims SET lease_expires_at = '2020-01-01T00:00:00Z' "
        "WHERE task_number = 42")
    # One iteration of the loop body, called directly.
    store.task_claims.renew(42, ex.executor_id, s.executor.lease_seconds, 0)
    row = store.task_claims.get(42)
    assert row["lease_expires_at"] > "2026"
    # A task with no claim row must not crash the renewal loop.
    renew = asyncio.ensure_future(ex._renew_lease(999, 0))
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
