"""Issue #124: watchdog recovery must not erase an operator's deliberate pause."""
from __future__ import annotations

import pytest

from clams.config import Settings
from clams.db import WorkItem, open_store
from clams.watchdog import WatchdogService

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def env(tmp_path):
    s = Settings(paths={
        "data_dir": str(tmp_path), "db_path": str(tmp_path / "p.db"),
        "work_root": str(tmp_path / "work"), "repo_path": str(tmp_path),
        "heartbeat_dir": str(tmp_path / "hb")})
    store = open_store(s.paths.db_path)
    for req in (16, 36):
        store.work_items.upsert(WorkItem(requirement_number=req,
                                         requirement_title=f"r{req}",
                                         gh_status="In Progress",
                                         gh_updated_at="2026-11-15T00:00:00Z"))
    svc = WatchdogService(store, s)
    yield svc, store, s
    store.db.close()


def _all_paused(store):
    return all(bool(i.user_paused) for i in store.work_items.all())


def test_runtime_state_roundtrip(env):
    _, store, _ = env
    assert store.runtime.get_bool("operator_paused") is False
    store.runtime.set("operator_paused", "1")
    assert store.runtime.get_bool("operator_paused") is True
    store.runtime.set("operator_paused", "0")
    assert store.runtime.get_bool("operator_paused") is False


def test_healthy_sweep_resumes_a_watchdog_pause(env):
    svc, store, _ = env
    svc._step_pause_and_notify(_report())
    assert _all_paused(store)
    svc._lift_watchdog_pause()
    assert not _all_paused(store)


def test_healthy_sweep_never_resumes_an_operator_pause(env):
    """The bug: a passing probe resumed the emergency stop."""
    svc, store, _ = env
    store.runtime.set("operator_paused", "1")
    store.work_items.set_paused_all(True)
    svc._paused_by_watchdog = True          # watchdog also paused it earlier
    svc._lift_watchdog_pause()
    assert _all_paused(store), \
        "an operator stop must survive the watchdog lifting its own pause"


def test_watchdog_pause_is_recorded_in_runtime_state(env):
    svc, store, _ = env
    svc._step_pause_and_notify(_report())
    assert store.runtime.get_bool("watchdog_paused") is True
    svc._lift_watchdog_pause()
    assert store.runtime.get_bool("watchdog_paused") is False


def test_pause_survives_a_restart(tmp_path):
    """A new Store over the same database still sees the operator stop."""
    s = Settings(paths={"data_dir": str(tmp_path), "db_path": str(tmp_path / "r.db"),
                        "work_root": str(tmp_path / "w"), "repo_path": str(tmp_path),
                        "heartbeat_dir": str(tmp_path / "hb")})
    store = open_store(s.paths.db_path)
    store.runtime.set("operator_paused", "1")
    store.db.close()
    again = open_store(s.paths.db_path)
    assert again.runtime.get_bool("operator_paused") is True
    again.db.close()


def _report():
    from clams.watchdog import SweepReport, CheckResult, CHECK_LLM
    r = SweepReport()
    r.results = [CheckResult(CHECK_LLM, False, "critical", "engine down")]
    r.criticals = [CHECK_LLM]
    return r
