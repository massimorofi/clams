"""S9 — watchdog tests."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from clams.bus import InMemoryBus
from clams.config import Settings
from clams.db import Store, WorkItem, open_store
from clams.watchdog import (
    ALL_CHECKS,
    CHECK_CACHE_ISOLATION,
    CHECK_DISK,
    CHECK_GITHUB,
    CHECK_HEARTBEAT,
    CHECK_LLM,
    CHECK_QUEUE,
    CHECK_SQLITE,
    CHECK_STRIXPER,
    CheckResult,
    WatchdogService,
    probe_cache_isolation,
    probe_disk,
    probe_heartbeats,
    probe_sqlite,
    selfcheck,
)


@pytest.fixture()
def store(tmp_path):
    s = open_store(tmp_path / "wd.db")
    yield s
    s.db.close()


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        paths={
            "data_dir": str(tmp_path / "data"),
            "db_path": str(tmp_path / "data" / "wd.db"),
            "work_root": str(tmp_path / "work"),
            "repo_path": str(tmp_path),
            "heartbeat_dir": str(tmp_path / "data" / "heartbeats"),
        }
    )


def fresh_heartbeat(dirpath, name="planner"):
    Path(dirpath).mkdir(parents=True, exist_ok=True)
    Path(dirpath, f"{name}.json").write_text(json.dumps({
        "service": name, "pid": 1,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "detail": "",
    }))


def make_svc(store, settings, **kw):
    from clams.bus import InMemoryBus

    defaults = dict(
        llm_probe=lambda url, timeout: True,
        strixper_probe=lambda url, path: (True, "ok"),
        github_probe=lambda gh: True,
        # A real bus so check_queue actually runs. Without it the check is
        # skipped, and a skipped check no longer counts as a healthy sweep.
        bus=InMemoryBus(),
    )
    defaults.update(kw)
    return WatchdogService(store, settings, **defaults)


# ---- zero-LLM guarantee --------------------------------------------
def test_watchdog_module_has_no_llm_imports():
    """The supervisor must not depend on the thing it supervises."""
    import clams.watchdog as wd
    src = Path(wd.__file__).read_text()
    forbidden = [
        "from .planner", "import planner",
        "from .decompose", "import decompose",
        "from .claudio_client", "import claudio_client",
    ]
    for bad in forbidden:
        assert bad not in src, f"watchdog must not {bad}"


def test_watchdog_does_not_construct_a_planner(store, settings):
    svc = make_svc(store, settings)
    assert not hasattr(svc, "planner")
    assert not hasattr(svc, "llm_call")


# ---- individual checks ---------------------------------------------
async def test_llm_ok_first_pass(store, settings):
    svc = make_svc(store, settings, llm_probe=lambda url, timeout: True)
    r = await svc.check_llm()
    assert r.ok is True
    assert r.name == CHECK_LLM


async def test_llm_warning_then_critical_after_three_consecutive(store, settings):
    svc = make_svc(store, settings, llm_probe=lambda url, timeout: False)
    r1 = await svc.check_llm()
    assert r1.severity == "warning"
    r2 = await svc.check_llm()
    assert r2.severity == "warning"
    r3 = await svc.check_llm()
    assert r3.severity == "critical"
    assert r3.critical is True


async def test_llm_recovery_resets_counter(store, settings):
    state = {"ok": False}

    def probe(url, timeout):
        return state["ok"]

    svc = make_svc(store, settings, llm_probe=probe)
    await svc.check_llm()
    await svc.check_llm()
    state["ok"] = True
    r = await svc.check_llm()
    assert r.ok is True
    assert svc._llm_failures == 0


async def test_strixper_engine_switch_error_is_critical(store, settings):
    svc = make_svc(store, settings,
                   strixper_probe=lambda url, path: (False, "engine switch error: boom"))
    r = await svc.check_strixper()
    assert r.critical is True
    assert "engine switch" in r.detail


def test_sqlite_check_ok(store, settings):
    svc = make_svc(store, settings)
    r = svc.check_sqlite()
    assert r.ok is True


def test_sqlite_check_slow_is_critical(store, settings):
    svc = WatchdogService(store, settings,
                         llm_probe=lambda u, t: True,
                         strixper_probe=lambda u, p: (True, "ok"),
                         github_probe=lambda g: True)
    # force a tiny threshold so the check fails
    object.__setattr__(svc.settings.watchdog, "disk_min_free_bytes", 1)
    ok, detail = probe_sqlite(store, max_ms=-1.0)
    assert ok is False


def test_disk_probe_low_space_is_critical(tmp_path):
    ok, detail = probe_disk(str(tmp_path), min_free_bytes=10 ** 18)
    assert ok is False
    assert "GiB free" in detail


def test_disk_probe_ok(tmp_path):
    ok, detail = probe_disk(str(tmp_path), min_free_bytes=1)
    assert ok is True


async def test_queue_check_stale_message_is_critical(store, settings):
    bus = InMemoryBus()
    svc = WatchdogService(store, settings, bus=bus,
                         llm_probe=lambda u, t: True,
                         strixper_probe=lambda u, p: (True, "ok"),
                         github_probe=lambda g: True)
    # simulate an old pending message by overriding pending()
    async def fake_pending(stream, group):
        return {"count": 5, "oldest_idle_ms": 20 * 60 * 1000}
    svc.bus.pending = fake_pending
    r = await svc.check_queue()
    assert r.critical is True
    assert "idle" in r.detail


async def test_github_check_three_failures_is_critical(store, settings):
    svc = make_svc(store, settings, github_probe=lambda gh: False)
    assert (await svc.check_github()).severity == "warning"
    assert (await svc.check_github()).severity == "warning"
    assert (await svc.check_github()).severity == "critical"


def test_heartbeat_missing_is_critical(store, settings):
    svc = make_svc(store, settings)
    r = svc.check_heartbeat()
    assert r.critical is True
    assert "missing" in r.detail


def test_heartbeat_stale_is_critical(store, settings):
    d = settings.paths.heartbeat_dir
    Path(d).mkdir(parents=True, exist_ok=True)
    old = (datetime.now(timezone.utc) - timedelta(seconds=300)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for name in ("planner", "executor"):
        Path(d, f"{name}.json").write_text(json.dumps(
            {"service": name, "pid": 1, "timestamp": old, "detail": ""}))
    svc = make_svc(store, settings)
    r = svc.check_heartbeat()
    assert r.critical is True
    assert "stale" in r.detail


def test_heartbeat_fresh_is_ok(store, settings):
    d = settings.paths.heartbeat_dir
    for name in ("planner", "executor"):
        fresh_heartbeat(d, name)
    svc = make_svc(store, settings)
    r = svc.check_heartbeat()
    assert r.ok is True


def test_cache_isolation_detects_collision():
    ok, detail = probe_cache_isolation({
        "engine-a": "/cache/a",
        "engine-b": "/cache/a",
    })
    assert ok is False
    assert "collision" in detail


def test_cache_isolation_ok_when_distinct(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir(); b.mkdir()
    ok, detail = probe_cache_isolation({"engine-a": str(a), "engine-b": str(b)})
    assert ok is True


def test_cache_isolation_missing_path_fails():
    ok, detail = probe_cache_isolation({"engine-a": ""})
    assert ok is False


# ---- sweep / remediation ladder ------------------------------------
async def test_healthy_sweep_does_not_pause(store, settings):
    store.work_items.upsert(WorkItem(requirement_number=1, requirement_title="r",
                                    gh_status="In Progress",
                                    gh_updated_at="2026-11-08T00:00:00Z"))
    d = settings.paths.heartbeat_dir
    for name in ("planner", "executor"):
        fresh_heartbeat(d, name)
    svc = make_svc(store, settings)
    report = await svc.sweep()
    assert report.healthy is True
    assert store.work_items.is_paused(1) is False


async def test_critical_sweep_pauses_and_notifies(store, settings):
    store.work_items.upsert(WorkItem(requirement_number=2, requirement_title="r",
                                    gh_status="In Progress",
                                    gh_updated_at="2026-11-08T00:00:00Z"))
    d = settings.paths.heartbeat_dir
    for name in ("planner", "executor"):
        fresh_heartbeat(d, name)
    # strixper failure is critical on the first occurrence
    svc = make_svc(store, settings,
                  strixper_probe=lambda u, p: (False, "engine switch error"))
    report = await svc.sweep()
    assert CHECK_STRIXPER in report.criticals
    assert store.work_items.is_paused(2) is True
    alerts = store.alerts.list_unacked()
    assert any("dispatching paused" in a["message"] for a in alerts)


async def test_remediation_recovers_and_resumes(store, settings):
    store.work_items.upsert(WorkItem(requirement_number=3, requirement_title="r",
                                    gh_status="In Progress",
                                    gh_updated_at="2026-11-08T00:00:00Z"))
    d = settings.paths.heartbeat_dir
    for name in ("planner", "executor"):
        fresh_heartbeat(d, name)

    state = {"ok": False}
    restarts = []

    async def engine_restart():
        restarts.append(1)
        state["ok"] = True

    def strixper(url, path):
        if state["ok"]:
            return (True, "ok")
        return (False, "engine switch error")

    svc = WatchdogService(store, settings,
                         llm_probe=lambda u, t: True,
                         strixper_probe=strixper,
                         github_probe=lambda g: True,
                         bus=InMemoryBus(),
                         engine_restart=engine_restart)
    # The first sweep detects the failure, restarts the engine, and returns the
    # verified (recovered) report.
    first = await svc.sweep()
    assert restarts, "expected an engine restart to be issued"
    assert first.remediated is True
    good = await svc.sweep()
    assert good.healthy is True


async def test_escalation_after_three_failed_recoveries(store, settings):
    store.work_items.upsert(WorkItem(requirement_number=4, requirement_title="r",
                                    gh_status="In Progress",
                                    gh_updated_at="2026-11-08T00:00:00Z"))
    d = settings.paths.heartbeat_dir
    for name in ("planner", "executor"):
        fresh_heartbeat(d, name)

    svc = WatchdogService(store, settings,
                         llm_probe=lambda u, t: True,
                         strixper_probe=lambda u, p: (False, "engine switch error"),
                         github_probe=lambda g: True)
    escalated = False
    for _ in range(3):
        report = await svc.sweep()
        escalated = report.escalated
    assert escalated is True
    alerts = store.alerts.list_all()
    assert any("Human action required" in a["message"] for a in alerts)


def test_sweep_report_to_text(store, settings):
    svc = make_svc(store, settings)
    r = CheckResult("x", False, "critical", "boom")
    from clams.watchdog import SweepReport
    rep = SweepReport(results=[r], criticals=["x"])
    txt = rep.to_text()
    assert "FAIL" in txt and "boom" in txt


# ---- wdctl --------------------------------------------------------
def test_selfcheck_writes_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAMS__PATHS__HEARTBEAT_DIR", str(tmp_path / "hb"))
    monkeypatch.setenv("CLAMS__PATHS__DB_PATH", str(tmp_path / "d.db"))
    monkeypatch.setenv("CLAMS__PATHS__DATA_DIR", str(tmp_path / "d"))
    rc = selfcheck()
    assert rc == 0
    assert (tmp_path / "hb" / "watchdog.json").exists()


def test_wdctl_check_command(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAMS__PATHS__HEARTBEAT_DIR", str(tmp_path / "hb"))
    monkeypatch.setenv("CLAMS__PATHS__DB_PATH", str(tmp_path / "d.db"))
    monkeypatch.setenv("CLAMS__PATHS__DATA_DIR", str(tmp_path / "d"))
    from clams.watchdog import main
    # heartbeats missing -> unhealthy -> exit 1
    rc = main(["check"])
    assert rc == 1


# ---- skipped checks must never look healthy (issue #11) -------------------

async def test_skipped_check_makes_sweep_unhealthy(store, settings):
    """A check that could not run is not a passing check."""
    d = settings.paths.heartbeat_dir
    for name in ("planner", "executor"):
        fresh_heartbeat(d, name)

    # No bus passed -> check_queue is skipped.
    svc = WatchdogService(store, settings,
                         llm_probe=lambda u, t: True,
                         strixper_probe=lambda u, p: (True, "ok"),
                         github_probe=lambda g: True)
    report = await svc.run_checks()
    assert report.skipped_names, "expected at least one skipped check"
    assert report.healthy is False, (
        "a sweep with skipped checks must not report healthy")


async def test_skipped_check_is_printed_as_skip_not_ok(store, settings):
    d = settings.paths.heartbeat_dir
    for name in ("planner", "executor"):
        fresh_heartbeat(d, name)

    svc = WatchdogService(store, settings,
                         llm_probe=lambda u, t: True,
                         strixper_probe=lambda u, p: (True, "ok"),
                         github_probe=lambda g: True)
    report = await svc.run_checks()
    text = report.to_text()
    assert "[SKIP]" in text, "skipped checks must render as [SKIP], not [OK]"
    assert "skipped:" in text


async def test_fully_wired_sweep_has_no_skips(store, settings):
    """With every dependency injected, nothing should be skipped."""
    from clams.bus import InMemoryBus

    d = settings.paths.heartbeat_dir
    for name in ("planner", "executor"):
        fresh_heartbeat(d, name)

    svc = WatchdogService(store, settings,
                         llm_probe=lambda u, t: True,
                         strixper_probe=lambda u, p: (True, "ok"),
                         github_probe=lambda g: True,
                         bus=InMemoryBus())
    report = await svc.run_checks()
    assert report.skipped_names == []
    assert report.healthy is True
