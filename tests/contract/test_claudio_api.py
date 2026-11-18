"""WP0: contract test against a LIVE Claudio agent container.

Run with a claudio/agent container up:
    docker run -d --name clams-wp0-claudio -p 127.0.0.1:1072:8100 claudio/agent:latest
    CLAUDIO_TEST_URL=http://127.0.0.1:1072 pytest tests/contract -m live

Verified contract (2026-11-18):
  GET  /healthz                       -> {"status": "ok"}
  POST /api/v1/chat                   -> 422 validation_error on empty message
  GET  /api/v1/sessions               -> {"sessions": [...]}
  workspace_dir outside allowed roots -> 422 validation_error
  error envelope                      -> {"error": {code, message, retryable}}
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.live

URL = os.environ.get("CLAUDIO_TEST_URL", "http://127.0.0.1:1072")
TOKEN = os.environ.get("CLAUDIO_TEST_TOKEN", "dev-token-change-me")


@pytest.fixture(scope="module")
def client():
    import httpx
    c = httpx.Client(base_url=URL, timeout=15.0,
                     headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        c.get("/healthz")
    except Exception:
        pytest.skip(f"no live claudio at {URL}")
    yield c
    c.close()


def test_health(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_auth_required(client):
    import httpx
    anon = httpx.Client(base_url=URL, timeout=10.0)
    r = anon.post("/api/v1/chat", json={"message": "x"})
    assert r.status_code == 401
    body = r.json()
    assert body["error"]["code"] == "unauthorized"


def test_chat_validation_error(client):
    r = client.post("/api/v1/chat", json={})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_sessions_list(client):
    r = client.get("/api/v1/sessions")
    assert r.status_code == 200
    assert "sessions" in r.json()


def test_workspace_dir_validated(client):
    r = client.post("/api/v1/chat",
                    json={"message": "hi", "workspace_dir": "/etc"})
    assert r.status_code == 422
    assert "workspace" in r.json()["error"]["message"].lower()


def test_cancel_unknown_run(client):
    r = client.post("/api/v1/chat/runs/does-not-exist/cancel")
    assert r.status_code in (404, 422)
