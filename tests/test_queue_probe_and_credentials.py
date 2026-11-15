"""Issue #120 (watchdog queue probe was a no-op) and #125 (token in remotes)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from clams.bus import InMemoryBus, RedisBus, STREAM_TASKS, publish_task
from clams.config import Settings
from clams.contracts import TaskBrief, TaskLimits
from clams.db import WorkItem, open_store
from clams.provision import (clean_remote_url, find_token_bearing_remotes,
                             scrub_remote_tokens)
from clams.watchdog import (CHECK_DISK, CHECK_QUEUE, CheckResult,
                            WatchdogService, probe_queue)

pytestmark = pytest.mark.asyncio


def _brief(n=42):
    return TaskBrief(task_issue=n, requirement_issue=7, objective="o",
                     acceptance_criterion="run: true",
                     limits=TaskLimits(max_turns=1, max_wall_seconds=5))


# ---- #120 --------------------------------------------------------------

async def test_probe_queue_reports_a_real_age():
    """The old code always reported 0s, so a 16h backlog looked healthy."""
    bus = InMemoryBus()
    await publish_task(bus, _brief())
    await bus.read(STREAM_TASKS, "executors", "e1", count=1, block_ms=10)
    # Age is measured from delivery; force it into the past.
    key = (STREAM_TASKS, "executors")
    bus._pel[key] = [(m, c, ts - 3600) for m, c, ts in bus._pel[key]]
    ok, detail = await probe_queue(bus, max_age_seconds=900)
    assert ok is False
    assert "3600" in detail


async def test_probe_queue_surfaces_a_broken_probe():
    class BrokenBus:
        async def pending(self, stream, group):
            return {"count": 0, "oldest_idle_ms": 0,
                    "error": "xpending: ResponseError: NOAUTH"}
    ok, detail = await probe_queue(BrokenBus(), 900)
    assert ok is False
    assert "NOAUTH" in detail


async def test_redis_pending_uses_xpending_range():
    """The exact TypeError from the issue must no longer occur."""
    pytest.importorskip("redis")
    import redis.asyncio as aioredis
    bus = RedisBus("redis://127.0.0.1:6379/9", namespace="clamstest")
    try:
        await bus.connect()
    except Exception as exc:
        pytest.skip(f"no redis: {exc}")
    try:
        await bus.purge(STREAM_TASKS)
        await bus.ensure_group(STREAM_TASKS, "executors")
        await publish_task(bus, _brief(92))
        await bus.read(STREAM_TASKS, "executors", "e1", count=1, block_ms=50)
        info = await bus.pending(STREAM_TASKS, "executors")
        assert "error" not in info, info
        assert info["count"] == 1
        assert info["oldest_idle_ms"] >= 0
        detail = await bus._pending_detail(bus._name(STREAM_TASKS), "executors")
        assert detail and "idle_ms" in detail[0]
    finally:
        await bus.purge(STREAM_TASKS)
        await bus.close()


@pytest.fixture()
def wd(tmp_path):
    s = Settings(paths={
        "data_dir": str(tmp_path), "db_path": str(tmp_path / "w.db"),
        "work_root": str(tmp_path / "work"), "repo_path": str(tmp_path),
        "heartbeat_dir": str(tmp_path / "hb")})
    store = open_store(s.paths.db_path)
    store.work_items.upsert(WorkItem(requirement_number=7, requirement_title="t",
                                     gh_status="In Progress",
                                     gh_updated_at="2026-11-15T00:00:00Z",
                                     task_numbers=[42]))
    yield WatchdogService(store, s), store, s
    store.db.close()


def test_auto_recover_does_not_prune_live_worktrees(wd, tmp_path):
    """#120: cleanup_stale_worktrees(work_root, set()) deleted running tasks."""
    svc, store, s = wd
    live = Path(s.paths.work_root) / "p0-task-42"
    gone = Path(s.paths.work_root) / "p0-task-99"
    live.mkdir(parents=True)
    gone.mkdir(parents=True)
    (live / "important.txt").write_text("do not delete me")
    store.task_claims.claim(42, 7, "executor-1", lease_seconds=900)

    import asyncio
    asyncio.run(svc._step_auto_recover([CheckResult(CHECK_QUEUE, False, "critical",
                                                  "x")]))
    assert live.exists(), "a live task's worktree must survive recovery"
    assert not gone.exists(), "an abandoned worktree should still be pruned"


def test_live_worktree_paths_empty_claims_yields_empty_set(wd):
    svc, store, s = wd
    assert svc._live_worktree_paths() == set()


# ---- #125 --------------------------------------------------------------

def test_clean_remote_url_strips_credentials():
    assert clean_remote_url(
        "https://x-access-token:ghp_SECRET@github.com/acme/widgets"
    ) == "https://github.com/acme/widgets"
    assert clean_remote_url("https://github.com/acme/widgets") == \
        "https://github.com/acme/widgets"
    assert clean_remote_url("git@github.com:acme/widgets") == \
        "git@github.com:acme/widgets"


def _mk_repo(path: Path, remote: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for args in (["init", "-q", "-b", "main"], ["remote", "add", "origin", remote]):
        subprocess.run(["git", *args], cwd=path, check=True)
    return path


def test_find_and_scrub_token_bearing_remotes(tmp_path):
    work = tmp_path / "work"
    dirty = _mk_repo(work / "projects" / "claudio-2",
                     "https://x-access-token:ghp_LEAKED@github.com/acme/claudio")
    clean = _mk_repo(work / "projects" / "clams-1",
                     "https://github.com/acme/clams")
    assert find_token_bearing_remotes(work) == [str(dirty)]
    fixed = scrub_remote_tokens(work)
    assert fixed == [str(dirty)]
    url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=dirty,
                         capture_output=True, text=True).stdout.strip()
    assert "ghp_LEAKED" not in url and url == "https://github.com/acme/claudio"
    assert find_token_bearing_remotes(work) == []
    assert clean.exists()


def test_scrub_is_idempotent(tmp_path):
    work = tmp_path / "work"
    _mk_repo(work / "projects" / "a", "https://github.com/a/a")
    assert scrub_remote_tokens(work) == []


def test_watchdog_flags_token_bearing_clone(wd, tmp_path, monkeypatch):
    svc, store, s = wd
    _mk_repo(Path(s.paths.work_root) / "projects" / "x",
             "https://x-access-token:ghp_BAD@github.com/x/x")
    result = svc.check_git_credentials()
    assert not result.ok and result.critical
    assert "rotate" in result.detail


def test_watchdog_passes_when_remotes_are_clean(wd):
    svc, store, s = wd
    assert svc.check_git_credentials().ok
