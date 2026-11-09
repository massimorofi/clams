"""Clams HTTP + SSE backend for the web frontend (issue #14).

Implements the API contract from the frontend spec:

REST
    GET  /healthz                 liveness
    GET  /api/v1/project          target repo metadata and status
    GET  /api/v1/config           current live configuration
    PUT  /api/v1/config          hot-update configuration (validated, diffed)
    POST /api/v1/cli/exec         run a Clams CLI command
    POST /api/v1/planner/action   structured planner directive (retry/pause/alter)
    GET  /api/v1/status           header-bar aggregate state
    GET  /api/v1/tasks            task list with state
    GET  /api/v1/alerts           alert feed

Streaming
    GET  /api/v1/events           SSE: telemetry:update, logs:stream, planner:reply
    WS   /ws                      planner:message / planner:reply

The API is a thin, read-mostly projection over the same SQLite store, Redis bus
and config the CLI uses. It never invents a second source of truth.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings
from .db import Store, open_store

log = logging.getLogger("clams.api")

# CLI subcommands the UI may invoke. Whitelisted on purpose: an unguarded
# subprocess spawn of an arbitrary user string is a remote shell.
ALLOWED_COMMANDS = {
    "status", "intake", "plan", "dispatch", "execute", "worker",
    "review", "doctor", "healthcheck", "init-db",
}


# ---------------------------------------------------------------- schemas ----
class ConfigUpdate(BaseModel):
    """Partial config patch. Keys use the dotted CLAMS__ path form."""

    values: dict[str, Any] = Field(default_factory=dict)


class CliExecRequest(BaseModel):
    command: str
    args: list[str] = Field(default_factory=list)
    timeout: float = 300.0


class PlannerActionRequest(BaseModel):
    action: str                      # retry | pause | resume | alter | approve | reject
    requirement_number: Optional[int] = None
    task_number: Optional[int] = None
    detail: str = ""


class PlannerMessage(BaseModel):
    message: str
    requirement_number: Optional[int] = None


# ---------------------------------------------------------------- helpers ----
def _read_heartbeat(heartbeat_dir: str, name: str) -> Optional[dict]:
    p = Path(heartbeat_dir) / f"{name}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _heartbeat_age(hb: Optional[dict]) -> Optional[float]:
    if not hb:
        return None
    from datetime import datetime, timezone

    try:
        ts = datetime.fromisoformat(str(hb["timestamp"]).replace("Z", "+00:00"))
    except Exception:
        return None
    return (datetime.now(timezone.utc) - ts).total_seconds()


def compute_state(store: Store, settings: Settings) -> str:
    """Derive the global badge state: Idle | Executing | Paused | Error."""
    alerts = store.alerts.list_unacked(severity="critical")
    if alerts:
        return "Error"
    paused = 0
    total = 0
    for item in store.work_items.all():
        total += 1
        if getattr(item, "user_paused", False):
            paused += 1
    active = store.task_claims.active()
    if active:
        return "Executing"
    if total and paused == total:
        return "Paused"
    return "Idle"


def _git(repo_path: str, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=repo_path, capture_output=True, text=True,
            timeout=30,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except Exception:
        return ""


# ---------------------------------------------------------------- app -------
def create_app(settings: Optional[Settings] = None,
              store: Optional[Store] = None,
              bus=None) -> FastAPI:
    """Build the FastAPI app.

    Dependencies are injectable so tests can pass an in-memory bus and a
    throwaway store instead of live Redis/GitHub.
    """
    settings = settings or Settings.load()
    store = store or open_store(settings.paths.db_path)
    if bus is None:
        from .bus import RedisBus
        bus = RedisBus(settings.redis.url, settings.redis.namespace)

    app = FastAPI(title="Clams API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Runtime-mutable config overlay. Hot-apply writes here and rebuilds the
    # Settings object so a running loop picks changes up without a restart.
    runtime: dict[str, Any] = {}

    # ---- health -----------------------------------------------------
    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "service": "clams-api"}

    # ---- project ----------------------------------------------------
    @app.get("/api/v1/project")
    async def get_project():
        repo = settings.paths.repo_path
        return {
            "owner": settings.github.owner,
            "repo": settings.github.repo,
            "full_name": settings.github.full_name,
            "branch": _git(repo, "rev-parse", "--abbrev-ref", "HEAD") or "unknown",
            "commit": _git(repo, "rev-parse", "HEAD")[:12] or "unknown",
            "worktree_dir": repo,
            "clean": _git(repo, "status", "--porcelain") == "",
            "api_base": settings.github.api_base,
        }

    @app.post("/api/v1/project/pull")
    async def pull_project():
        proc = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=settings.paths.repo_path, capture_output=True, text=True,
            timeout=120,
        )
        return {
            "ok": proc.returncode == 0,
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-4000:],
            "commit": _git(settings.paths.repo_path, "rev-parse", "HEAD")[:12],
        }

    # ---- config -----------------------------------------------------
    @app.get("/api/v1/config")
    async def get_config():
        return {
            "settings": _settings_dict(settings),
            "runtime_overlay": runtime,
            "editable": _editable_schema(),
        }

    @app.put("/api/v1/config")
    async def put_config(update: ConfigUpdate):
        # Validate before applying: build a candidate Settings and run the
        # same device-limit check the CLI runs at startup. A bad patch must not
        # half-apply to a live system.
        candidate_values = {**runtime, **update.values}
        try:
            candidate = _build_settings(candidate_values)
            candidate.limits.validate_against_device()
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        before = _settings_dict(settings)
        runtime.clear()
        runtime.update(candidate_values)
        # Replace the live settings object in place so holders see new values.
        _overwrite_settings(settings, candidate)
        after = _settings_dict(settings)
        return {
            "ok": True,
            "changed": _diff_dicts(before, after),
            "settings": after,
        }

    # ---- CLI --------------------------------------------------------
    @app.post("/api/v1/cli/exec")
    async def cli_exec(req: CliExecRequest):
        if req.command not in ALLOWED_COMMANDS:
            raise HTTPException(
                status_code=400,
                detail=f"command {req.command!r} not allowed; "
                       f"allowed: {sorted(ALLOWED_COMMANDS)}",
            )
        # Invoke through the current interpreter rather than a bare `clams` on
        # PATH: the console script only exists inside the installed venv, so a
        # PATH-dependent spawn fails whenever the API is started any other way.
        cmd = [sys.executable, "-m", "clams.cli", req.command, *req.args]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=req.timeout,
                cwd=str(Path.cwd()),
            )
            return {
                "ok": proc.returncode == 0,
                "exit_code": proc.returncode,
                "stdout": proc.stdout[-200_000:],
                "stderr": proc.stderr[-200_000:],
                "command": " ".join(cmd),
            }
        except subprocess.TimeoutExpired:
            return {
                "ok": False, "exit_code": 124,
                "stdout": "", "stderr": f"timed out after {req.timeout}s",
                "command": " ".join(cmd),
            }
        except FileNotFoundError:
            raise HTTPException(status_code=500, detail="clams CLI not on PATH")

    # ---- planner ----------------------------------------------------
    @app.post("/api/v1/planner/action")
    async def planner_action(req: PlannerActionRequest):
        valid = {"retry", "pause", "resume", "alter", "approve", "reject"}
        if req.action not in valid:
            raise HTTPException(
                status_code=400,
                detail=f"unknown action {req.action!r}; valid: {sorted(valid)}",
            )
        payload = {
            "action": req.action,
            "requirement_number": req.requirement_number,
            "task_number": req.task_number,
            "detail": req.detail,
            "timestamp": _now_iso(),
        }
        try:
            await bus.publish("clams:planner-actions", payload)
        except Exception as exc:
            raise HTTPException(status_code=502,
                               detail=f"bus publish failed: {exc}")
        # Pause/resume also drive the store directly: the header controls must
        # take effect even if nothing is consuming the stream yet.
        if req.requirement_number is not None:
            if req.action == "pause":
                store.work_items.set_paused(req.requirement_number, True)
            elif req.action == "resume":
                store.work_items.set_paused(req.requirement_number, False)
        return {"ok": True, **payload}

    @app.post("/api/v1/planner/message")
    async def planner_message(msg: PlannerMessage):
        return await _send_planner_message(settings, msg, bus)

    # ---- status / tasks / alerts ------------------------------------
    @app.get("/api/v1/status")
    async def get_status():
        hb = {}
        for name in ("orchestrator", "planner", "executor", "watchdog"):
            data = _read_heartbeat(settings.paths.heartbeat_dir, name)
            hb[name] = {
                "alive": data is not None,
                "age_seconds": _heartbeat_age(data),
            }
        active = store.task_claims.active()
        return {
            "state": compute_state(store, settings),
            "repository": settings.github.full_name,
            "active_agents": len(active),
            "heartbeats": hb,
            "unacked_alerts": store.alerts.count(),
            "outbox": store.outbox.stats(),
            "timestamp": _now_iso(),
        }

    @app.get("/api/v1/tasks")
    async def get_tasks():
        items = []
        for it in store.work_items.all():
            items.append(it.to_dict())
        return {"tasks": items}

    @app.get("/api/v1/alerts")
    async def get_alerts(limit: int = 100, severity: Optional[str] = None):
        rows = store.alerts.list_all(limit=limit)
        out = [dict(r) for r in rows]
        if severity:
            out = [r for r in out if r.get("severity") == severity]
        return {"alerts": out}

    # ---- SSE --------------------------------------------------------
    @app.get("/api/v1/events")
    async def events(request: Request):
        """Server-Sent Events stream.

        Emits `telemetry:update` on a fixed cadence and forwards bus events
        (`logs:stream`, `planner:reply`, alerts) as they arrive.
        """
        async def generate():
            yield _sse("connected", {"timestamp": _now_iso()})
            counter = 0
            while True:
                if await request.is_disconnected():
                    break
                counter += 1
                try:
                    status = await get_status()
                    yield _sse("telemetry:update", status)
                except Exception as exc:  # noqa: BLE001
                    yield _sse("logs:stream", {
                        "level": "error", "source": "api",
                        "message": f"telemetry failed: {exc}",
                        "timestamp": _now_iso(),
                    })
                # Drain any pending bus events without blocking the cadence.
                try:
                    for evt in await _drain_bus(bus):
                        yield _sse(evt["event"], evt["data"])
                except Exception:
                    pass
                await asyncio.sleep(2.0)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ---- WebSocket --------------------------------------------------
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        await ws.send_json({"event": "connected",
                          "data": {"timestamp": _now_iso()}})
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw) if raw.strip() else {}
                except json.JSONDecodeError:
                    msg = {"type": "planner:message", "message": raw}
                etype = msg.get("type") or msg.get("event") or "planner:message"
                if etype == "planner:message":
                    reply = await _send_planner_message(
                        settings,
                        PlannerMessage(
                            message=str(msg.get("message", "")),
                            requirement_number=msg.get("requirement_number"),
                        ),
                        bus,
                    )
                    await ws.send_json({"event": "planner:reply", "data": reply})
                elif etype == "ping":
                    await ws.send_json({"event": "pong",
                                       "data": {"timestamp": _now_iso()}})
                else:
                    await ws.send_json({
                        "event": "error",
                        "data": {"message": f"unsupported type {etype!r}"},
                    })
        except WebSocketDisconnect:
            return
        except Exception as exc:  # noqa: BLE001 - never leak a stack over the socket
            try:
                await ws.send_json({"event": "error",
                                   "data": {"message": str(exc)}})
            except Exception:
                return

    _mount_static(app, settings)
    return app


# ------------------------------------------------------------- internals ----
def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def _drain_bus(bus) -> list[dict]:
    """Non-blocking read of the alert + result streams for SSE forwarding."""
    out: list[dict] = []
    try:
        msgs = await bus.drain("clams:alerts")
        for m in msgs:
            out.append({"event": "logs:stream", "data": {
                "level": (m.payload or {}).get("severity", "info")
                if isinstance(m.payload, dict) else "info",
                "source": "bus",
                "message": json.dumps(m.payload) if not isinstance(m.payload, str)
                           else m.payload,
                "timestamp": _now_iso(),
            }})
    except Exception:
        pass
    try:
        msgs = await bus.drain("clams:results")
        for m in msgs:
            out.append({"event": "planner:reply", "data": {
                "kind": "task_result", "payload": m.payload,
                "timestamp": _now_iso(),
            }})
    except Exception:
        pass
    return out


async def _send_planner_message(settings: Settings, msg: PlannerMessage,
                               bus) -> dict:
    """Forward a chat message to the planner Claudio instance and return its reply."""
    from .claudio_client import ClaudioClient

    client = ClaudioClient(settings.planner.base_url, settings.planner.auth_token,
                          settings.planner.request_timeout)
    try:
        result = await client.chat(msg.message)
        reply = getattr(result, "final_output", None) or str(result)
    except Exception as exc:  # noqa: BLE001 - surface, don't crash the request
        reply = f"(planner unreachable: {exc})"
    try:
        await bus.publish("clams:planner-messages", {
            "direction": "user", "message": msg.message,
            "requirement_number": msg.requirement_number,
            "timestamp": _now_iso(),
        })
    except Exception:
        pass
    return {
        "reply": reply,
        "requirement_number": msg.requirement_number,
        "timestamp": _now_iso(),
    }


def _settings_dict(settings: Settings) -> dict:
    """Serialise the live settings for the UI editor."""
    try:
        return settings.model_dump(mode="json")
    except Exception:
        return {}


def _build_settings(values: dict) -> Settings:
    """Build a Settings from a flat {dotted.path: value} mapping."""
    nested: dict[str, Any] = {}
    for key, value in values.items():
        parts = key.split("__") if "__" in key else key.split(".")
        cur = nested
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value
    return Settings(**nested)


def _overwrite_settings(target: Settings, source: Settings) -> None:
    """Copy each sub-settings object so existing holders see new values."""
    for name in type(target).model_fields:
        setattr(target, name, getattr(source, name))


def _diff_dicts(before: dict, after: dict) -> dict:
    changed: dict[str, Any] = {}
    for section, new_val in after.items():
        old_val = before.get(section)
        if isinstance(new_val, dict) and isinstance(old_val, dict):
            for k, v in new_val.items():
                if old_val.get(k) != v:
                    changed[f"{section}.{k}"] = {"from": old_val.get(k), "to": v}
        elif old_val != new_val:
            changed[section] = {"from": old_val, "to": new_val}
    return changed


def _editable_schema() -> list[dict]:
    """Describe the fields the UI renders, with control types and bounds."""
    return [
        {"path": "poll_interval", "label": "Poll interval (s)", "type": "number",
         "min": 1, "max": 300, "step": 1},
        {"path": "limits.max_attempts", "label": "Max attempts", "type": "number",
         "min": 1, "max": 10, "step": 1},
        {"path": "limits.max_turns", "label": "Max turns", "type": "number",
         "min": 1, "max": 200, "step": 1},
        {"path": "limits.max_wall_seconds", "label": "Max wall seconds",
         "type": "number", "min": 60, "max": 7200, "step": 60},
        {"path": "limits.max_replans", "label": "Max replans", "type": "number",
         "min": 1, "max": 10, "step": 1},
        {"path": "limits.max_concurrent_engines", "label": "Concurrent engines",
         "type": "number", "min": 1, "max": 8, "step": 1},
        {"path": "limits.queues_per_engine", "label": "Queues per engine",
         "type": "number", "min": 1, "max": 8, "step": 1},
        {"path": "watchdog.interval_seconds", "label": "Watchdog interval (s)",
         "type": "number", "min": 1, "max": 300, "step": 1},
        {"path": "watchdog.heartbeat_max_age", "label": "Heartbeat max age (s)",
         "type": "number", "min": 5, "max": 600, "step": 5},
        {"path": "project.enabled", "label": "Projects board sync",
         "type": "boolean"},
        {"path": "project.number", "label": "Project number", "type": "number",
         "min": 0, "max": 100000, "step": 1},
        {"path": "project.status_field", "label": "Status field name",
         "type": "text"},
        {"path": "executor.heartbeat_interval", "label": "Executor heartbeat (s)",
         "type": "number", "min": 1, "max": 300, "step": 1},
    ]


def _mount_static(app: FastAPI, settings: Settings) -> None:
    """Serve the built SPA if it exists, with an SPA fallback route."""
    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if not dist.exists():
        return
    app.mount("/assets", StaticFiles(directory=str(dist / "assets")),
              name="assets")

    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        if full_path.startswith(("api/", "healthz", "ws")):
            raise HTTPException(status_code=404)
        candidate = dist / full_path
        if full_path and candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(dist / "index.html"))
