"""Tests for the issue #15 backend additions: projects registry, alert
acknowledgement, and the global stop action."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from clams.api import create_app, normalize_github_url
from clams.bus import InMemoryBus
from clams.config import Settings
from clams.db import open_store


@pytest.fixture()
def client(tmp_path):
    s = Settings()
    s.paths.db_path = str(tmp_path / "t.db")
    s.paths.data_dir = str(tmp_path)
    s.paths.heartbeat_dir = str(tmp_path / "hb")
    (tmp_path / "hb").mkdir(parents=True, exist_ok=True)
    bus = InMemoryBus()
    app = create_app(s, bus=bus)
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------- URL validation ----
def test_normalize_github_url_ok():
    url, err = normalize_github_url("https://github.com/acme/widgets")
    assert err is None
    assert url == "https://github.com/acme/widgets"


def test_normalize_github_url_strips_git_suffix():
    url, err = normalize_github_url("https://github.com/acme/widgets.git")
    assert err is None
    assert url == "https://github.com/acme/widgets"


def test_normalize_github_url_rejects_bare_owner_repo():
    # The mockup requires a github.com URL; a bare owner/repo has no host to
    # validate, so it is rejected.
    url, err = normalize_github_url("acme/widgets")
    assert url is None
    assert err is not None


def test_normalize_github_url_rejects_non_github():
    url, err = normalize_github_url("https://gitlab.com/acme/widgets")
    assert url is None
    assert err is not None


def test_normalize_github_url_rejects_missing_repo():
    url, err = normalize_github_url("https://github.com/acme")
    assert url is None
    assert err is not None


# ------------------------------------------------------- projects registry ----
def test_projects_empty_initially(client):
    r = client.get("/api/v1/projects")
    assert r.status_code == 200
    assert r.json()["projects"] == []


def test_project_create_and_list(client):
    r = client.post(
        "/api/v1/projects",
        json={"name": "Website refresh",
              "url": "https://github.com/acme/website.git"},
    )
    assert r.status_code == 200
    proj = r.json()["project"]
    assert proj["name"] == "Website refresh"
    assert proj["url"] == "https://github.com/acme/website"
    assert proj["status"] == "Queued"
    assert proj["progress"] == 0
    assert "updated" in proj

    lst = client.get("/api/v1/projects").json()["projects"]
    assert len(lst) == 1
    assert lst[0]["url"] == "https://github.com/acme/website"


def test_project_create_rejects_empty_name(client):
    r = client.post("/api/v1/projects", json={"name": "  ",
                                            "url": "https://github.com/a/b"})
    assert r.status_code == 422


def test_project_create_rejects_long_name(client):
    r = client.post("/api/v1/projects",
                   json={"name": "x" * 81, "url": "https://github.com/a/b"})
    assert r.status_code == 422


def test_project_create_rejects_bad_url(client):
    r = client.post("/api/v1/projects",
                   json={"name": "ok", "url": "https://example.com/a/b"})
    assert r.status_code == 422


def test_project_create_rejects_duplicate_url(client):
    client.post("/api/v1/projects",
                json={"name": "One", "url": "https://github.com/a/b"})
    r = client.post("/api/v1/projects",
                   json={"name": "Two", "url": "https://github.com/a/b.git"})
    assert r.status_code == 409


# --------------------------------------------------- alert acknowledgement ----
def _seed_alerts(db_path, rows):
    store = open_store(db_path)
    ids = []
    for sev, src, msg in rows:
        ids.append(store.alerts.add(sev, src, msg))
    return ids


def test_ack_single_alert(tmp_path):
    s = Settings()
    s.paths.db_path = str(tmp_path / "t.db")
    s.paths.data_dir = str(tmp_path)
    s.paths.heartbeat_dir = str(tmp_path / "hb")
    (tmp_path / "hb").mkdir(parents=True, exist_ok=True)
    app = create_app(s, bus=InMemoryBus())
    with TestClient(app) as c:
        ids = _seed_alerts(str(tmp_path / "t.db"),
                          [("info", "a", "m1"), ("warning", "b", "m2")])
        assert c.get("/api/v1/status").json()["unacked_alerts"] == 2
        r = c.post(f"/api/v1/alerts/{ids[0]}/ack")
        assert r.status_code == 200
        assert r.json()["unacked_alerts"] == 1
        assert c.get("/api/v1/status").json()["unacked_alerts"] == 1


def test_ack_missing_alert_404(client):
    r = client.post("/api/v1/alerts/9999/ack")
    assert r.status_code == 404


def test_ack_all_by_severity(tmp_path):
    s = Settings()
    s.paths.db_path = str(tmp_path / "t.db")
    s.paths.data_dir = str(tmp_path)
    s.paths.heartbeat_dir = str(tmp_path / "hb")
    (tmp_path / "hb").mkdir(parents=True, exist_ok=True)
    app = create_app(s, bus=InMemoryBus())
    with TestClient(app) as c:
        _seed_alerts(str(tmp_path / "t.db"),
                     [("info", "a", "m1"), ("warning", "b", "m2"),
                      ("warning", "c", "m3")])
        r = c.post("/api/v1/alerts/ack", json={"severity": "warning"})
        assert r.status_code == 200
        assert r.json()["acknowledged"] == 2
        assert r.json()["unacked_alerts"] == 1
        # bulk with no filter acks the rest
        r2 = c.post("/api/v1/alerts/ack", json={})
        assert r2.json()["acknowledged"] == 1
        assert r2.json()["unacked_alerts"] == 0


def test_ack_all_rejects_bad_severity(client):
    r = client.post("/api/v1/alerts/ack", json={"severity": "bogus"})
    assert r.status_code == 422


# --------------------------------------------------------- stop action --------
def test_stop_action_is_valid(client):
    r = client.post("/api/v1/planner/action", json={"action": "stop"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_stop_pauses_all_work_items(tmp_path):
    s = Settings()
    s.paths.db_path = str(tmp_path / "t.db")
    s.paths.data_dir = str(tmp_path)
    s.paths.heartbeat_dir = str(tmp_path / "hb")
    (tmp_path / "hb").mkdir(parents=True, exist_ok=True)
    app = create_app(s, bus=InMemoryBus())
    with TestClient(app) as c:
        store = open_store(str(tmp_path / "t.db"))
        from clams.db import WorkItem
        store.work_items.upsert(WorkItem(
            requirement_number=1, requirement_title="t1", gh_status="Open",
            gh_updated_at="x", status="planned"))
        store.work_items.upsert(WorkItem(
            requirement_number=2, requirement_title="t2", gh_status="Open",
            gh_updated_at="x", status="planned"))
        r = c.post("/api/v1/planner/action", json={"action": "stop"})
        assert r.status_code == 200
        # both items now paused
        assert store.work_items.is_paused(1) is True
        assert store.work_items.is_paused(2) is True
        # state reflects paused
        assert c.get("/api/v1/status").json()["state"] == "Paused"
        # resume clears
        c.post("/api/v1/planner/action", json={"action": "resume"})
        assert store.work_items.is_paused(1) is False
        assert c.get("/api/v1/status").json()["state"] == "Idle"
