"""Regression tests for the pause deadlock that froze the whole pipeline.

Symptom: both projects polled fine, but every cycle reported
``planned=0 dispatched=0`` and the badge sat in Error. Cause: the watchdog
paused *every* work item on a (false-positive) critical, and the API's resume
walked rows with the default ``project_id=0``, so it silently no-opped on every
row belonging to a real project. Nothing in the UI could undo it.
"""
from __future__ import annotations

import pytest

from clams.db import open_store


@pytest.fixture()
def store(tmp_path):
    s = open_store(tmp_path / "t.db")
    yield s
    s.db.close()


def _seed(store):
    """Two projects each tracking one requirement, like the live deployment."""
    for pid in (1, 2):
        store.work_items.claim_requirement(10 + pid, f"R{pid}", "2026-01-01T00:00:00Z",
                                           project_id=pid)


def test_paused_items_are_skipped_by_plan_and_dispatch_queries(store):
    """Documents the contract the orchestrator relies on."""
    _seed(store)
    store.work_items.set_paused_all(True)
    assert all(i.user_paused for i in store.work_items.all())
    store.work_items.set_paused_all(False)
    assert not any(i.user_paused for i in store.work_items.all())


def test_set_paused_scoped_to_one_project_leaves_others(store):
    """The old global-resume bug, isolated: default project_id=0 misses rows."""
    _seed(store)
    store.work_items.set_paused_all(True)
    # Pre-fix behaviour: resume walked every row but passed no project id, so
    # only the legacy project-0 rows were ever cleared.
    store.work_items.set_paused(11, False)          # project 0 -> no-op
    assert store.work_items.is_paused(11, 1) is True
    assert store.work_items.is_paused(12, 2) is True
    # Correct call clears the right row only.
    store.work_items.set_paused(11, False, project_id=1)
    assert store.work_items.is_paused(11, 1) is False
    assert store.work_items.is_paused(12, 2) is True


def test_set_paused_all_clears_every_project(store):
    _seed(store)
    store.work_items.set_paused_all(True)
    assert store.work_items.set_paused_all(False) == 2
    assert not any(i.user_paused for i in store.work_items.all())


def test_set_paused_for_requirement_spans_projects(store):
    store.work_items.claim_requirement(77, "shared", "2026-01-01T00:00:00Z", project_id=1)
    store.work_items.claim_requirement(77, "shared", "2026-01-01T00:00:00Z", project_id=2)
    assert store.work_items.set_paused_for_requirement(77, True) == 2
    assert store.work_items.is_paused(77, 1) is True
    assert store.work_items.is_paused(77, 2) is True
    assert store.work_items.set_paused_for_requirement(77, False) == 2


def test_api_global_resume_unpauses_real_projects(store):
    """End-to-end through the API handler, which is what the UI calls."""
    from fastapi.testclient import TestClient

    from clams.api import create_app
    from clams.config import Settings

    from clams.bus import InMemoryBus

    _seed(store)
    store.work_items.set_paused_all(True)
    app = create_app(Settings(), store=store, bus=InMemoryBus())
    with TestClient(app) as client:
        r = client.post("/api/v1/planner/action",
                        json={"action": "resume", "detail": ""})
        assert r.status_code == 200
    assert not any(i.user_paused for i in store.work_items.all())


def test_api_global_stop_pauses_real_projects(store):
    from fastapi.testclient import TestClient

    from clams.api import create_app
    from clams.config import Settings

    from clams.bus import InMemoryBus

    _seed(store)
    app = create_app(Settings(), store=store, bus=InMemoryBus())
    with TestClient(app) as client:
        r = client.post("/api/v1/planner/action",
                        json={"action": "stop", "detail": ""})
        assert r.status_code == 200
    assert all(i.user_paused for i in store.work_items.all())
    # and it must be reversible again
    with TestClient(app) as client:
        client.post("/api/v1/planner/action", json={"action": "resume", "detail": ""})
    assert not any(i.user_paused for i in store.work_items.all())
