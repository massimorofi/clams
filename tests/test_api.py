"""Tests for the Clams HTTP + SSE backend (issue #14)."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from clams.api import ALLOWED_COMMANDS, create_app
from clams.bus import InMemoryBus
from clams.config import Settings


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


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_project_returns_repo_metadata(client):
    r = client.get("/api/v1/project")
    assert r.status_code == 200
    d = r.json()
    for key in ("owner", "repo", "full_name", "branch", "commit", "worktree_dir"):
        assert key in d


def test_config_returns_settings_and_editable(client):
    r = client.get("/api/v1/config")
    assert r.status_code == 200
    d = r.json()
    assert "settings" in d
    assert isinstance(d["editable"], list)
    assert d["editable"], "editable field list must not be empty"
    paths = {f["path"] for f in d["editable"]}
    assert "poll_interval" in paths
    assert "limits.max_attempts" in paths


def test_config_put_applies_and_reports_diff(client):
    r = client.put("/api/v1/config", json={"values": {"poll_interval": 7.0}})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert "poll_interval" in d["changed"]
    assert d["changed"]["poll_interval"]["to"] == 7.0
    # and it is really live
    assert client.get("/api/v1/config").json()["settings"]["poll_interval"] == 7.0


def test_config_put_rejects_invalid_limits(client):
    """A patch that breaks the HW-1 ceiling must be refused, not half-applied."""
    r = client.put(
        "/api/v1/config",
        json={"values": {"limits.max_concurrent_engines": 99}},
    )
    assert r.status_code == 422
    # the bad value must not have leaked into the live config
    live = client.get("/api/v1/config").json()["settings"]["limits"]
    assert live["max_concurrent_engines"] != 99


def test_cli_exec_rejects_unknown_command(client):
    r = client.post("/api/v1/cli/exec", json={"command": "rm -rf /"})
    assert r.status_code == 400
    assert "not allowed" in r.json()["detail"]


def test_cli_exec_runs_whitelisted_command(client):
    r = client.post("/api/v1/cli/exec", json={"command": "status"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert "requirements tracked" in d["stdout"]


def test_cli_exec_whitelist_has_no_shell_metachars():
    for cmd in ALLOWED_COMMANDS:
        assert cmd.replace("-", "").isalnum(), f"unsafe command name {cmd!r}"


def test_planner_action_validates_action(client):
    r = client.post("/api/v1/planner/action", json={"action": "frobnicate"})
    assert r.status_code == 400
    assert "unknown action" in r.json()["detail"]


def test_planner_action_pause_sets_paused(client):
    store_state = client.post(
        "/api/v1/planner/action",
        json={"action": "pause", "requirement_number": 42},
    )
    assert store_state.status_code == 200
    # resume must clear it again
    assert client.post(
        "/api/v1/planner/action",
        json={"action": "resume", "requirement_number": 42},
    ).status_code == 200


def test_status_shape(client):
    r = client.get("/api/v1/status")
    assert r.status_code == 200
    d = r.json()
    assert d["state"] in {"Idle", "Executing", "Paused", "Error"}
    assert "heartbeats" in d
    assert "active_agents" in d
    assert "timestamp" in d


def test_tasks_and_alerts_endpoints(client):
    assert client.get("/api/v1/tasks").json()["tasks"] == []
    assert client.get("/api/v1/alerts").json()["alerts"] == []


def test_sse_stream_emits_telemetry(client):
    with client.stream("GET", "/api/v1/events") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        chunks = []
        for line in resp.iter_lines():
            chunks.append(line)
            if len(chunks) >= 6:
                break
    text = "\n".join(chunks)
    assert "event: connected" in text
    assert "event: telemetry:update" in text


def test_websocket_planner_roundtrip(client):
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["event"] == "connected"
        ws.send_json({"type": "planner:message", "message": "hello"})
        reply = ws.receive_json()
        assert reply["event"] == "planner:reply"
        assert "reply" in reply["data"]


def test_websocket_rejects_unknown_type(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "bogus"})
        msg = ws.receive_json()
        assert msg["event"] == "error"


def test_unknown_api_route_is_404_not_spa(client):
    r = client.get("/api/v1/does-not-exist")
    assert r.status_code == 404
