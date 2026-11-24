"""WP7: API routes, auth, commands inbox, SSE resume."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from clams.api import create_app
from clams.config import Settings
from clams.db import (connect, migrate, repo_events, repo_projects,
                      repo_questions, repo_tasks, repo_workflows)

TOKEN = "test-token"


@pytest.fixture()
def app_db(tmp_path):
    path = str(tmp_path / "api.db")
    conn = connect(path)
    migrate(conn)
    yield conn, path
    conn.close()


@pytest.fixture()
def client(app_db):
    conn, path = app_db
    s = Settings(_env_file=None, db_path=path, api_token=TOKEN)
    app = create_app(s)
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        yield c, conn


def _seed(conn):
    pid = repo_projects.upsert_project(conn, "o/r")
    wf = repo_workflows.create_workflow(conn, pid, "github", "goal", 3)
    repo_workflows.set_status(conn, wf, "running")
    brief = {"objective": "o", "acceptance": "a", "run": ["x"],
             "writable": ["*"], "repo": "o/r"}
    t1 = repo_tasks.create_task(conn, wf, 1, "one", "o", "a", brief)
    t2 = repo_tasks.create_task(conn, wf, 2, "two", "o", "a", brief,
                                depends_on=[1])
    repo_tasks.transition(conn, t1, "ready")
    repo_tasks.transition(conn, t1, "dispatched")
    repo_tasks.transition(conn, t1, "running")
    repo_tasks.transition(conn, t1, "success")
    return wf, t1, t2


def test_health(client):
    c, _ = client
    assert c.get("/healthz").json()["status"] == "ok"
    assert c.get("/readyz").json()["status"] == "ready"


def test_auth_required(app_db):
    conn, path = app_db
    s = Settings(_env_file=None, db_path=path, api_token=TOKEN)
    with TestClient(create_app(s)) as c:
        assert c.get("/api/v1/overview").status_code == 401
        c.headers["Authorization"] = "Bearer wrong"
        assert c.get("/api/v1/overview").status_code == 401


def test_overview(client):
    c, conn = client
    _seed(conn)
    r = c.get("/api/v1/overview").json()
    assert r["tasks"]["success"] == 1
    assert r["workflows"]["running"] == 1
    assert r["success_rate"] == 1.0
    assert r["paused"] is False


def test_workflow_detail_ordered_tasks(client):
    c, conn = client
    wf, t1, t2 = _seed(conn)
    d = c.get(f"/api/v1/workflows/{wf}").json()
    assert [t["task_id"] for t in d["tasks"]] == [t1, t2]
    assert d["tasks"][0]["status"] == "success"


def test_task_detail(client):
    c, conn = client
    _, t1, _ = _seed(conn)
    d = c.get(f"/api/v1/tasks/{t1}").json()
    assert d["brief"]["repo"] == "o/r"
    assert d["depends_on"] == []


def test_transcript_no_session(client):
    c, conn = client
    _, t1, _ = _seed(conn)
    assert c.get(f"/api/v1/tasks/{t1}/transcript").json() == {"messages": []}


def test_transcript_session_gone(app_db):
    """claudio 404 (pruned/lost session) -> 410 with a readable detail,
    not a generic 502 Bad Gateway."""
    from clams.claudio_client import SessionNotFound

    class DeadAgent:
        def session_messages(self, session_id, limit=200):
            raise SessionNotFound(f"no session {session_id}", status=404)

    conn, path = app_db
    s = Settings(_env_file=None, db_path=path, api_token=TOKEN)
    app = create_app(s, claudio_e=DeadAgent())
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        _, t1, _ = _seed(conn)
        conn.execute("UPDATE tasks SET session_ref='gone-123' "
                     "WHERE task_id=?", (t1,))
        conn.commit()
        r = c.get(f"/api/v1/tasks/{t1}/transcript")
        assert r.status_code == 410
        assert "no longer available" in r.json()["detail"]


def test_questions_inbox(client):
    c, conn = client
    _, t1, _ = _seed(conn)
    repo_questions.open_question(conn, t1, 1, "which?", ["a"], "k")
    rows = c.get("/api/v1/questions?status=open").json()
    assert len(rows) == 1 and rows[0]["question"] == "which?"


def test_command_roundtrip(client):
    c, conn = client
    r = c.post("/api/v1/commands",
               json={"kind": "pause", "payload": {}, "issued_by": "massimorofi"})
    assert r.status_code == 202
    cid = r.json()["command_id"]
    got = c.get(f"/api/v1/commands/{cid}").json()
    assert got["status"] == "new"
    # planner applies it
    from clams.planner.commands import drain_commands
    drain_commands(conn)
    got = c.get(f"/api/v1/commands/{cid}").json()
    assert got["status"] == "applied"


def test_command_config_patch_remove_roundtrip(client):
    c, conn = client
    r = c.post("/api/v1/commands", json={
        "kind": "config_patch", "issued_by": "web-ui",
        "payload": {"projects": [{"repo": "o/ui-added"}]}})
    assert r.status_code == 202
    from clams.planner.commands import drain_commands
    drain_commands(conn)
    assert any(p["repo"] == "o/ui-added"
               for p in c.get("/api/v1/projects").json())
    r = c.post("/api/v1/commands", json={
        "kind": "config_patch", "issued_by": "web-ui",
        "payload": {"projects": [{"repo": "o/ui-added", "remove": True}]}})
    cid = r.json()["command_id"]
    drain_commands(conn)
    assert c.get(f"/api/v1/commands/{cid}").json()["status"] == "applied"
    assert not any(p["repo"] == "o/ui-added"
                   for p in c.get("/api/v1/projects").json())


def test_command_bad_kind(client):
    c, _ = client
    r = c.post("/api/v1/commands", json={"kind": "teleport", "payload": {}})
    assert r.status_code == 422


def test_sse_token_query_param(client):
    """EventSource can't send headers -> ?token= works on the events route."""
    c, conn = client
    repo_events.append_event(conn, "task", "t", "info", "via-query")
    # strip the Authorization header to prove the query param alone suffices
    saved = c.headers.pop("Authorization")
    try:
        with c.stream("GET", f"/api/v1/events?max_events=1&token={TOKEN}",
                      headers={"Last-Event-ID": "0"}) as r:
            assert r.status_code == 200
            lines = list(r.iter_lines())
    finally:
        c.headers["Authorization"] = saved
    data = [json.loads(l[6:]) for l in lines if l.startswith("data:")]
    assert [d["message"] for d in data] == ["via-query"]


def test_sse_resume(client):
    c, conn = client
    e1 = repo_events.append_event(conn, "task", "t", "info", "first")
    e2 = repo_events.append_event(conn, "task", "t", "info", "second")
    with c.stream("GET", "/api/v1/events?max_events=2",
                  headers={"Last-Event-ID": "0"}) as r:
        assert r.status_code == 200
        lines = list(r.iter_lines())
    data = [json.loads(l[6:]) for l in lines if l.startswith("data:")]
    assert [d["message"] for d in data] == ["first", "second"]
    assert [d["event_id"] for d in data] == [e1, e2]


def test_sse_no_history_replay(client):
    import threading
    c, conn = client
    repo_events.append_event(conn, "task", "t", "info", "old")

    db_file = conn.execute("PRAGMA database_list").fetchone()[2]

    def delayed_append():
        import time
        from clams.db import connect as _connect
        time.sleep(0.5)
        own = _connect(db_file)
        try:
            repo_events.append_event(own, "task", "t", "info", "new-one")
        finally:
            own.close()

    th = threading.Thread(target=delayed_append, daemon=True)
    th.start()
    # connected at current max: "old" must NOT be replayed; only new-one
    with c.stream("GET", "/api/v1/events?max_events=1") as r:
        lines = list(r.iter_lines())
    th.join()
    data = [json.loads(l[6:]) for l in lines if l.startswith("data:")]
    assert [d["message"] for d in data] == ["new-one"]


def test_prompts_catalog(client):
    c, conn = client
    r = c.get("/api/v1/prompts")
    assert r.status_code == 200
    names = {p["name"] for p in r.json()["prompts"]}
    assert {"plan", "plan_repair", "plan_replan", "executor_task"} <= names
    plan = next(p for p in r.json()["prompts"] if p["name"] == "plan")
    assert plan["override"] is None and plan["effective"] == plan["default"]
    # an override shows up
    from clams import prompts
    prompts.set_override(conn, "plan", "x {goal} {max_tasks} {repo} {base_ref}")
    plan = next(p for p in c.get("/api/v1/prompts").json()["prompts"]
                if p["name"] == "plan")
    assert plan["override"].startswith("x ")
