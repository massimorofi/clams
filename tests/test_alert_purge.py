"""Alert purge + task-labelling regressions.

Two operator-visible annoyances:

* Acknowledging an alert only marked it read, so a resolved incident stayed in
  the Activity feed forever and there was no way to clear it.
* Planner-generated *tasks* were labelled ``status:Ready``, which is intake's
  trigger for "unclaimed requirement". The board therefore showed dozens of
  "Ready" issues that intake was correct to ignore.
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
    for i in range(3):
        store.alerts.add("warning", "outbox", f"old warning {i}")
    store.alerts.add("critical", "watchdog", "live critical")
    store.alerts.ack_all("warning")


def test_purge_removes_acknowledged_only(store):
    _seed(store)
    assert store.alerts.purge() == 3
    assert store.alerts.count_all() == 1
    assert store.alerts.count_unacked() == 1


def test_purge_can_include_unacked(store):
    _seed(store)
    assert store.alerts.purge(acknowledged_only=False) == 4
    assert store.alerts.count_all() == 0


def test_purge_by_severity(store):
    _seed(store)
    store.alerts.ack_all("critical")
    assert store.alerts.purge(severity="critical") == 1
    assert store.alerts.count_all() == 3


def test_purge_endpoint(store):
    from fastapi.testclient import TestClient

    from clams.api import create_app
    from clams.bus import InMemoryBus
    from clams.config import Settings

    _seed(store)
    app = create_app(Settings(), store=store, bus=InMemoryBus())
    with TestClient(app) as client:
        r = client.delete("/api/v1/alerts")
        assert r.status_code == 200
        assert r.json()["removed"] == 3
        assert r.json()["remaining"] == 1
        bad = client.delete("/api/v1/alerts", params={"severity": "nope"})
        assert bad.status_code == 422


def test_get_alerts_can_filter_acknowledged(store):
    from fastapi.testclient import TestClient

    from clams.api import create_app
    from clams.bus import InMemoryBus
    from clams.config import Settings

    _seed(store)
    app = create_app(Settings(), store=store, bus=InMemoryBus())
    with TestClient(app) as client:
        unacked = client.get("/api/v1/alerts", params={"acknowledged": False}).json()
        assert len(unacked["alerts"]) == 1
        assert unacked["alerts"][0]["severity"] == "critical"


def test_generated_tasks_are_not_labelled_ready():
    """A task must never wear intake's ``status:Ready`` trigger label."""
    import inspect

    from clams import decompose

    src = inspect.getsource(decompose.create_sub_issues)
    assert "STATUS_READY" not in src
    assert "STATUS_IN_PROGRESS" in src
