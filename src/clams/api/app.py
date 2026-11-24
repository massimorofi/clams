"""FastAPI app: read-only DB views + the commands inbox + SSE + built SPA.

The API never mutates domain tables: reads use a per-request read-only
connection; the only write is INSERT into ``commands`` via its own connection.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterator, Optional

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.middleware.cors import CORSMiddleware

from ..claudio_client import (ClaudioClient, ClaudioUnavailable,
                              SessionNotFound)
from ..db import (repo_alerts, repo_commands, repo_events, repo_executors,
                  repo_outbox, repo_projects, repo_questions, repo_runtime,
                  repo_tasks, repo_workflows, connect)
from ..db.conn import db_integrity_ok
from .auth import check_auth
from .sse import event_stream

API_PREFIX = "/api/v1"


class CommandBody(BaseModel):
    kind: str
    target: Optional[str] = None
    payload: dict = Field(default_factory=dict)
    issued_by: str = "api"


def create_app(settings, *, claudio_p: Optional[ClaudioClient] = None,
               claudio_e: Optional[ClaudioClient] = None) -> FastAPI:
    app = FastAPI(title="Clams v2", version="2.0.0", docs_url=None,
                  redoc_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:8200", "http://localhost:8200"],
        allow_methods=["*"], allow_headers=["*"])

    def ro_dep() -> Iterator[sqlite3.Connection]:
        conn = connect(settings.db_path, read_only=True, check_same_thread=False)
        try:
            yield conn
        finally:
            conn.close()

    def writer_dep() -> Iterator[sqlite3.Connection]:
        # the API's ONLY write target: commands
        conn = connect(settings.db_path, check_same_thread=False)
        try:
            yield conn
        finally:
            conn.close()

    @app.middleware("http")
    async def auth_gate(request: Request, call_next):
        path = request.url.path
        if path.startswith(API_PREFIX) and request.method != "OPTIONS":
            # EventSource cannot set headers: the SSE route also accepts the
            # bearer token as a ?token= query parameter.
            if (path == f"{API_PREFIX}/events" and "token" in request.query_params
                    and request.query_params["token"] == settings.api_token):
                return await call_next(request)
            err = check_auth(request, settings.api_token)
            if err is not None:
                return err
        return await call_next(request)

    # ------------------------------------------------------------- health
    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "version": "2.0.0"}

    @app.get("/readyz")
    def readyz(ro: sqlite3.Connection = Depends(ro_dep)):
        try:
            ok = db_integrity_ok(ro)
        except Exception:  # noqa: BLE001
            ok = False
        return {"status": "ready" if ok else "degraded", "db": ok}

    # ------------------------------------------------------------- reads
    @app.get(f"{API_PREFIX}/overview")
    def overview(ro: sqlite3.Connection = Depends(ro_dep)):
        return {
            "workflows": repo_workflows.counts_by_status(ro),
            "tasks": repo_tasks.counts_by_status(ro),
            "success_rate": _success_rate(ro),
            "executors": [dict(r) for r in repo_executors.list_executors(ro)],
            "alerts": [dict(r) for r in repo_alerts.list_open(ro)],
            "paused": repo_runtime.is_paused(ro),
            "paused_by": repo_runtime.paused_by(ro),
            "outbox": repo_outbox.counts(ro),
        }

    @app.get(f"{API_PREFIX}/workflows")
    def workflows(status: Optional[str] = None,
                  ro: sqlite3.Connection = Depends(ro_dep)):
        rows = (repo_workflows.list_by_status(ro, status) if status
                else repo_workflows.list_by_status(ro))
        return [dict(r) for r in rows]

    @app.get(f"{API_PREFIX}/workflows/{{workflow_id}}")
    def workflow_detail(workflow_id: str,
                        ro: sqlite3.Connection = Depends(ro_dep)):
        wf = repo_workflows.get_workflow(ro, workflow_id)
        if wf is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        tasks = repo_tasks.list_tasks(ro, workflow_id)
        return {**dict(wf), "tasks": [_task_summary(t) for t in tasks]}

    @app.get(f"{API_PREFIX}/tasks/{{task_id}}")
    def task_detail(task_id: str, ro: sqlite3.Connection = Depends(ro_dep)):
        t = repo_tasks.get_task(ro, task_id)
        if t is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        d = dict(t)
        d["brief"] = json.loads(t["brief_json"])
        d["depends_on"] = json.loads(t["depends_on"])
        d["result"] = json.loads(t["result_json"]) if t["result_json"] else None
        return d

    @app.get(f"{API_PREFIX}/tasks/{{task_id}}/transcript")
    def task_transcript(task_id: str,
                        ro: sqlite3.Connection = Depends(ro_dep)):
        t = repo_tasks.get_task(ro, task_id)
        if t is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        ref = t["session_ref"]
        if not ref:
            return {"messages": []}
        client = claudio_e or claudio_p
        if client is None:
            return JSONResponse({"error": "claudio not configured"},
                                status_code=502)
        try:
            return {"messages": client.session_messages(ref)}
        except SessionNotFound:
            # `detail` (not `error`): the frontend surfaces this text
            return JSONResponse(
                {"detail": "transcript no longer available — the agent "
                           "session was pruned or predates session "
                           "persistence"},
                status_code=410)
        except ClaudioUnavailable as exc:
            return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.get(f"{API_PREFIX}/questions")
    def questions(status: str = "open",
                  ro: sqlite3.Connection = Depends(ro_dep)):
        if status == "open":
            return [dict(r) for r in repo_questions.list_open(ro)]
        rows = ro.execute(
            "SELECT * FROM questions WHERE status=? ORDER BY question_id",
            (status,)).fetchall()
        return [dict(r) for r in rows]

    @app.get(f"{API_PREFIX}/projects")
    def projects(ro: sqlite3.Connection = Depends(ro_dep)):
        return [dict(r) for r in repo_projects.list_projects(ro)]

    # ---------------------------------------------------------- config
    @app.get(f"{API_PREFIX}/config")
    def config(ro: sqlite3.Connection = Depends(ro_dep)):
        """Effective runtime settings + the editable schema, so the UI form
        is generated from the same registry the planner enforces."""
        from ..config import EDITABLE_SETTINGS, get_settings
        from ..db.conn import get_runtime_state
        s = get_settings()
        overrides = json.loads(
            get_runtime_state(ro, "settings_overrides") or "{}")
        effective = {k: getattr(s, k) for k in EDITABLE_SETTINGS}
        for k, v in overrides.items():
            effective[k] = v          # planner-applied overrides win
        return {"editable": EDITABLE_SETTINGS, "effective": effective,
                "overrides": overrides}

    @app.get(f"{API_PREFIX}/prompts")
    def prompts_catalog(ro: sqlite3.Connection = Depends(ro_dep)):
        """Prompt templates: built-in defaults + operator overrides.
        Edits go back through POST /commands kind=prompt_patch."""
        from ..prompts import catalog
        return {"prompts": catalog(ro)}

    # ---------------------------------------------------------- commands
    @app.post(f"{API_PREFIX}/commands", status_code=202)
    def post_command(body: CommandBody,
                     writer: sqlite3.Connection = Depends(writer_dep)):
        try:
            cid = repo_commands.insert_command(
                writer, body.kind, body.payload, body.issued_by, body.target)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        return {"command_id": cid, "status": "new"}

    @app.get(f"{API_PREFIX}/commands/{{command_id}}")
    def get_command(command_id: int,
                    ro: sqlite3.Connection = Depends(ro_dep)):
        row = repo_commands.get(ro, command_id)
        if row is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        d = dict(row)
        d["payload"] = json.loads(d["payload"]) if d["payload"] else {}
        d["result"] = json.loads(d["result"]) if d["result"] else None
        return d

    # --------------------------------------------------------------- SSE
    @app.get(f"{API_PREFIX}/events")
    def events(request: Request, max_events: int = 0):
        header = request.headers.get("last-event-id")
        conn = connect(settings.db_path, read_only=True, check_same_thread=False)

        def gen():
            try:
                start = int(header) if header and header.isdigit() \
                    else repo_events.max_event_id(conn)
                yield from event_stream(conn, last_event_id=start,
                                        max_events=max_events)
            finally:
                conn.close()

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # --------------------------------------------------------------- SPA
    spa = Path(settings.spa_dir)
    if spa.exists():
        @app.get("/{path:path}", include_in_schema=False)
        def spa_route(path: str):
            target = (spa / path).resolve()
            root = spa.resolve()
            if path and target.is_file() and str(target).startswith(str(root)):
                return FileResponse(target)
            index = root / "index.html"
            if index.exists():
                return FileResponse(index)
            return JSONResponse({"error": "spa not built"}, status_code=404)

    return app


def _success_rate(ro: sqlite3.Connection) -> float:
    row = ro.execute(
        "SELECT SUM(status='success') s, COUNT(*) n FROM tasks "
        "WHERE status IN ('success','failed')").fetchone()
    if not row or not row["n"]:
        return 0.0
    return round((row["s"] or 0) / row["n"], 4)


def _task_summary(t) -> dict:
    return {
        "task_id": t["task_id"], "sequence_index": t["sequence_index"],
        "title": t["title"], "status": t["status"], "attempt": t["attempt"],
        "depends_on": json.loads(t["depends_on"]),
        "commit_sha": t["commit_sha"], "branch": t["branch"],
        "gh_issue_number": t["gh_issue_number"],
    }
