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
from pydantic import BaseModel, Field, ValidationError

from .config import Settings
from .db import Store, open_store, AlertRepo
from .agents import (
    AGENT_ROLES,
    CLAUDIO_ROLES,
    AgentConfig,
    AgentError,
    AgentRegistry,
    default_agents,
    probe_agent,
)
from .provision import (
    ProvisionError,
    clone_repo,
    detect_default_branch,
    is_git_repo,
    project_worktree_path,
    provision_project,
    sync_repo,
)
from .project_state import compute_project_status, refresh_project_status

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


class ProjectCreate(BaseModel):
    """Payload for registering a new project (issue #15 / §6.1)."""

    name: str
    url: str


class AlertAckAll(BaseModel):
    """Bulk-acknowledge alerts, optionally filtered by severity (§6.3)."""

    severity: Optional[str] = None


class AgentCreate(BaseModel):
    """Register a new agent (issue #19)."""

    id: str
    role: str
    base_url: str
    auth_token: str = ""
    enabled: bool = True
    request_timeout: float = 120.0


class AgentUpdate(BaseModel):
    """Patch an existing agent. Omitted/None fields are left untouched.

    ``id`` is immutable — it is the registry key, and changing it is a
    delete + add, not an edit.
    """

    role: Optional[str] = None
    base_url: Optional[str] = None
    auth_token: Optional[str] = None
    enabled: Optional[bool] = None
    request_timeout: Optional[float] = None


def _agent_error_text(exc: Exception) -> str:
    """Turn a validation failure into one clean line for the UI.

    Pydantic wraps the ``AgentError`` raised inside a field validator in a
    ``ValidationError``; surfacing the raw object is unreadable, so flatten
    it to the first human-readable message.
    """
    if isinstance(exc, ValidationError):
        errs = exc.errors()
        if errs:
            first = errs[0]
            loc = ".".join(str(p) for p in first.get("loc", ())) or "agent"
            return f"{loc}: {first.get('msg', 'invalid value')}"
    return str(exc)


def normalize_github_url(raw: str) -> tuple[Optional[str], Optional[str]]:
    """Validate and normalize a GitHub repository URL.

    Returns ``(normalized_url, error)``. On success ``error`` is ``None`` and
    ``normalized_url`` is ``https://github.com/owner/repo`` (``.git`` suffix
    stripped). On failure ``normalized_url`` is ``None`` and ``error`` is a
    human-readable message safe to surface to the UI.
    """
    from urllib.parse import urlsplit

    candidate = (raw or "").strip()
    if not candidate:
        return None, "Repository URL is required."
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None, "Enter a valid repository URL."
    if parts.scheme not in ("http", "https"):
        return None, "URL must use http or https."
    host = (parts.hostname or "").lower()
    if host not in ("github.com", "www.github.com"):
        return None, "Only github.com repository URLs are supported."
    segs = [s for s in parts.path.split("/") if s]
    if len(segs) < 2:
        return None, "URL should look like https://github.com/owner/repository"
    owner = segs[0]
    repo = segs[1]
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    if not owner or not repo:
        return None, "URL should look like https://github.com/owner/repository"
    return f"https://github.com/{owner}/{repo}", None


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


def compute_state(store: Store, settings: Settings,
                 global_paused: bool = False) -> str:
    """Derive the global badge state: Idle | Executing | Paused | Error.

    ``global_paused`` is the operator-controlled flag (topbar Pause / Emergency
    stop). When set it overrides the derived Executing state so the badge shows
    Paused even while claims are still live.
    """
    alerts = store.alerts.list_unacked(severity="critical")
    if alerts:
        return "Error"
    if global_paused:
        return "Paused"
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
    # Issue #124: the operator's stop/pause used to live only in this dict, so a
    # restart silently resumed dispatching something a human had deliberately
    # halted. Restore it from the durable copy.
    if store.runtime.get_bool("operator_paused"):
        runtime["global_paused"] = True
        if store.runtime.get_bool("operator_stopped"):
            runtime["stopped"] = True

    # Agent registry (issue #19): role -> address, persisted as JSON.
    # Seeded from the CLAMS__AGENTS__* settings so .env actually drives the
    # initial addresses rather than the built-in constants.
    agents = AgentRegistry.load(
        settings.paths.agents_file,
        defaults=default_agents(
            host=settings.agents.host,
            planner_port=settings.agents.planner_port,
            executor_port=settings.agents.executor_port,
            auth_token=settings.agents.auth_token,
            request_timeout=settings.agents.request_timeout,
        ),
    )

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

    # ---- agents (issue #19) ----------------------------------------
    # The registry is persisted separately from the hot-edit settings blob,
    # because the generic config path redacts secrets on the way out and
    # round-tripping a masked auth_token through PUT would clobber it.
    def _sync_settings_from_registry() -> None:
        """Mirror the planner/executor agents into the live Settings.

        Existing code dials ``settings.planner.base_url`` and
        ``settings.executor.base_url`` directly, so any registry change has
        to be reflected there or the running loop keeps using the old address.
        """
        changed = False
        for role, section in (("planner", settings.planner),
                            ("executor", settings.executor)):
            agent = agents.primary_for_role(role)
            if agent is None:
                continue
            if section.base_url != agent.effective_base_url():
                section.base_url = agent.effective_base_url()
                changed = True
            if agent.auth_token and section.auth_token != agent.auth_token:
                section.auth_token = agent.auth_token
                changed = True
        if changed:
            runtime["agents_synced_at"] = _now_iso()

    _sync_settings_from_registry()

    @app.get("/api/v1/agents")
    async def list_agents():
        return {
            "agents": [a.public_dict() for a in agents.all()],
            "roles": list(AGENT_ROLES),
            "claudio_roles": sorted(CLAUDIO_ROLES),
            "defaults": {
                "host": settings.agents.host,
                "planner_port": settings.agents.planner_port,
                "executor_port": settings.agents.executor_port,
                "request_timeout": settings.agents.request_timeout,
            },
        }

    @app.post("/api/v1/agents", status_code=201)
    async def create_agent(req: AgentCreate):
        try:
            agent = AgentConfig(**req.model_dump())
            agents.add(agent)
        except (AgentError, ValidationError) as exc:
            raise HTTPException(status_code=422, detail=_agent_error_text(exc))
        _sync_settings_from_registry()
        return {"ok": True, "agent": agent.public_dict(),
                "agents": [a.public_dict() for a in agents.all()]}

    @app.put("/api/v1/agents/{agent_id}")
    async def update_agent(agent_id: str, req: AgentUpdate):
        changes = {k: v for k, v in req.model_dump().items() if v is not None}
        try:
            agent = agents.update(agent_id, changes)
        except (AgentError, ValidationError) as exc:
            raise HTTPException(status_code=422, detail=_agent_error_text(exc))
        _sync_settings_from_registry()
        return {"ok": True, "agent": agent.public_dict(),
                "agents": [a.public_dict() for a in agents.all()]}

    @app.delete("/api/v1/agents/{agent_id}")
    async def delete_agent(agent_id: str):
        try:
            agents.remove(agent_id)
        except AgentError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        _sync_settings_from_registry()
        return {"ok": True, "agents": [a.public_dict() for a in agents.all()]}

    @app.post("/api/v1/agents/{agent_id}/probe")
    async def probe_agent_endpoint(agent_id: str):
        agent = agents.get(agent_id)
        if agent is None:
            raise HTTPException(status_code=404,
                               detail=f"no agent with id {agent_id!r}")
        result = await asyncio.to_thread(probe_agent, agent)
        return {"ok": result["ok"], "agent_id": agent_id, **result}

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
        valid = {"retry", "pause", "resume", "alter", "approve", "reject", "stop"}
        if req.action not in valid:
            raise HTTPException(
                status_code=400,
                detail=f"unknown action {req.action!r}; valid: {sorted(valid)}",
            )
        # Apply the local store effect FIRST so the header controls take effect
        # even if the bus is unavailable. The bus publish is best-effort after.
        if req.requirement_number is not None:
            if req.action == "pause":
                store.work_items.set_paused_for_requirement(req.requirement_number, True)
            elif req.action == "resume":
                store.work_items.set_paused_for_requirement(req.requirement_number, False)
        else:
            # Global (no requirement scoped) control from the topbar. These must
            # touch every project's rows: the old per-row loop passed the default
            # project_id=0, so a "stop" paused nothing outside the legacy project
            # and a "resume" could never undo a stop issued by the steering
            # channel (which pauses all rows). The pipeline then sat at
            # planned=0 dispatched=0 forever with no UI affordance to fix it.
            if req.action in ("pause", "stop"):
                runtime["global_paused"] = True
                store.runtime.set("operator_paused", "1")
                if req.action == "stop":
                    runtime["stopped"] = True
                    store.runtime.set("operator_stopped", "1")
                    # Pause every work item so the stop survives a state recompute.
                    store.work_items.set_paused_all(True)
            elif req.action == "resume":
                runtime["global_paused"] = False
                runtime["stopped"] = False
                store.runtime.set("operator_paused", "0")
                store.runtime.set("operator_stopped", "0")
                store.work_items.set_paused_all(False)

        payload = {
            "action": req.action,
            "requirement_number": req.requirement_number,
            "task_number": req.task_number,
            "detail": req.detail,
            "timestamp": _now_iso(),
        }
        # Issue #123: this used to publish to ``clams:planner-actions``, a
        # stream nothing ever consumed -- the pause looked applied but the
        # pipeline ignored it. The effects are now applied here *and* read by
        # the orchestrator (projects.paused / the persisted operator pause),
        # so the dead publish is gone. bus_error is kept for API compatibility.
        return {"ok": True, **payload, "bus_error": None}

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
            "state": compute_state(
                store, settings,
                global_paused=bool(runtime.get("global_paused")),
            ),
            "repository": settings.github.full_name,
            "active_agents": len(active),
            "heartbeats": hb,
            "unacked_alerts": store.alerts.count_unacked(),
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
    async def get_alerts(limit: int = 100, severity: Optional[str] = None,
                         acknowledged: Optional[bool] = None):
        rows = store.alerts.list_all(limit=limit)
        out = [dict(r) for r in rows]
        if severity:
            out = [r for r in out if r.get("severity") == severity]
        if acknowledged is not None:
            out = [r for r in out if bool(r.get("acknowledged")) == acknowledged]
        return {"alerts": out}

    @app.delete("/api/v1/alerts")
    async def purge_alerts(severity: Optional[str] = None,
                           before: Optional[str] = None,
                           acknowledged_only: bool = True):
        """Delete alert rows out of the Activity feed.

        Acknowledging only marks an alert read, so a resolved incident kept its
        rows in the feed forever. Defaults to acknowledged rows only; pass
        ``acknowledged_only=false`` to also drop untriaged ones.
        """
        if severity is not None and severity not in AlertRepo.SEVERITIES:
            raise HTTPException(
                status_code=422,
                detail=f"invalid severity {severity!r}; "
                       f"valid: {list(AlertRepo.SEVERITIES)}",
            )
        removed = store.alerts.purge(acknowledged_only=acknowledged_only,
                                     severity=severity, before=before)
        return {"ok": True, "removed": removed,
                "remaining": store.alerts.count_all()}

    # ---- alert acknowledgement (issue #15 / §6.3) -------------------
    @app.post("/api/v1/alerts/ack")
    async def ack_all_alerts(body: AlertAckAll):
        """Bulk-acknowledge alerts, optionally filtered by severity.

        Declared BEFORE the /{alert_id} route so the literal ``ack`` path is not
        swallowed by the integer path parameter.
        """
        if body.severity is not None and body.severity not in AlertRepo.SEVERITIES:
            raise HTTPException(
                status_code=422,
                detail=f"invalid severity {body.severity!r}; "
                       f"valid: {list(AlertRepo.SEVERITIES)}",
            )
        updated = store.alerts.ack_all(severity=body.severity)
        return {"ok": True, "acknowledged": updated,
                "unacked_alerts": store.alerts.count_unacked()}

    @app.post("/api/v1/alerts/{alert_id}/ack")
    async def ack_alert(alert_id: int):
        row = store.db.conn.execute(
            "SELECT * FROM alerts WHERE id = ?", (alert_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404,
                               detail=f"alert {alert_id} not found")
        store.alerts.ack(alert_id)
        return {"ok": True, "id": alert_id,
                "unacked_alerts": store.alerts.count_unacked()}

    # ---- projects registry (issue #15 / #18) ------------------------
    def _project_dict(row) -> dict:
        d = dict(row)
        d["updated"] = _relative_time(d.get("updated_at"))
        d["paused"] = bool(d.get("paused", 0))
        return d

    @app.get("/api/v1/projects")
    async def list_projects():
        rows = store.projects.all()
        projects = [_project_dict(r) for r in rows]
        return {"projects": projects}

    @app.post("/api/v1/projects")
    async def create_project(body: ProjectCreate):
        """Register a project AND provision its working tree (issue #18).

        The clone is the point of the operation: a registered project must have
        an addressable working tree the pipeline can operate on. If the clone
        fails the registration is rolled back so the invariant holds and the
        operator sees the error instead of a half-provisioned project.
        """
        name = (body.name or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="Project name is required.")
        if len(name) > 80:
            raise HTTPException(
                status_code=422, detail="Project name must be 80 characters or fewer."
            )
        normalized, err = normalize_github_url(body.url)
        if err:
            raise HTTPException(status_code=422, detail=err)
        if store.projects.url_exists(normalized):
            raise HTTPException(
                status_code=409,
                detail="That repository is already registered.",
            )
        row = store.projects.add(name=name, url=normalized)
        pid = int(row["id"])
        try:
            result = provision_project(
                name, normalized, settings.paths.work_root,
                project_id=pid, token=settings.github.token,
            )
        except ProvisionError as exc:
            # Roll back: a registered project must have a working tree.
            store.db.conn.execute("DELETE FROM projects WHERE id = ?", (pid,))
            raise HTTPException(
                status_code=502,
                detail=f"Registered name but could not clone the repository: {exc}",
            )
        store.projects.set_worktree(pid, result.worktree_path, result.default_branch)
        store.projects.clear_error(pid)
        row = store.projects.get(pid)
        return {"ok": True, "project": _project_dict(row)}

    @app.post("/api/v1/projects/{project_id}/sync")
    async def sync_project(project_id: int):
        """Re-sync a project's working tree with origin (issue #18)."""
        row = store.projects.get(project_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"project {project_id} not found")
        worktree = row["worktree_path"]
        if not worktree:
            # Provision on demand for projects registered before #18.
            try:
                result = provision_project(
                    row["name"], row["url"], settings.paths.work_root,
                    project_id=project_id, token=settings.github.token,
                )
            except ProvisionError as exc:
                store.projects.set_error(project_id, str(exc))
                raise HTTPException(status_code=502, detail=str(exc))
            store.projects.set_worktree(project_id, result.worktree_path,
                                       result.default_branch)
            store.projects.clear_error(project_id)
            return {"ok": True, "project": _project_dict(store.projects.get(project_id))}
        try:
            sha = sync_repo(worktree, token=settings.github.token,
                           branch=row["default_branch"])
        except ProvisionError as exc:
            store.projects.set_error(project_id, str(exc))
            raise HTTPException(status_code=502, detail=str(exc))
        store.projects.clear_error(project_id)
        out = _project_dict(store.projects.get(project_id))
        out["commit"] = sha[:12]
        return {"ok": True, "project": out}

    @app.post("/api/v1/projects/{project_id}/pause")
    async def pause_project(project_id: int):
        row = store.projects.get(project_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"project {project_id} not found")
        store.projects.set_paused(project_id, True)
        refresh_project_status(store, project_id)
        return {"ok": True, "project": _project_dict(store.projects.get(project_id))}

    @app.post("/api/v1/projects/{project_id}/resume")
    async def resume_project(project_id: int):
        row = store.projects.get(project_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"project {project_id} not found")
        store.projects.set_paused(project_id, False)
        refresh_project_status(store, project_id)
        return {"ok": True, "project": _project_dict(store.projects.get(project_id))}

    # ---- SSE --------------------------------------------------------
    @app.get("/api/v1/events")
    async def events(request: Request):
        """Server-Sent Events stream.

        Emits `telemetry:update` on a fixed cadence and forwards bus events
        (`logs:stream`, `planner:reply`, alerts) as they arrive.
        """
        async def generate():
            yield _sse("connected", {"timestamp": _now_iso()})
            # Start the tail at the CURRENT end of each stream. Reading from the
            # beginning (the old behaviour) replayed every historical alert —
            # including stale criticals days old — into the live feed on every
            # page load. Seeding the cursor at the last id means only events
            # published after this connection are forwarded.
            try:
                last_alert_id = await bus.last_id("clams:alerts")
            except Exception:
                last_alert_id = "0-0"
            try:
                last_result_id = await bus.last_id("clams:results")
            except Exception:
                last_result_id = "0-0"
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
                # Forward only bus events that arrived since the last tick.
                try:
                    evts, last_alert_id, last_result_id = await _drain_bus(
                        bus, last_alert_id, last_result_id)
                    for evt in evts:
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


def _relative_time(value: Optional[str]) -> str:
    """Render an ISO timestamp as a short human 'x ago' string.

    Falls back to the raw value (or 'unknown') if it cannot be parsed, so a
    missing/odd timestamp never breaks the UI.
    """
    if not value:
        return "unknown"
    try:
        from datetime import datetime, timezone

        ts = value[:-1] + "+00:00" if value.endswith("Z") else value
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        return str(value)
    delta = (datetime.now(timezone.utc) - dt).total_seconds()
    if delta < 0:
        delta = 0
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)} min ago"
    if delta < 86400:
        return f"{int(delta // 3600)} hr ago"
    days = int(delta // 86400)
    if days == 1:
        return "Yesterday"
    return f"{days} days ago"


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def _drain_bus(bus, last_alert_id: str = "0-0",
                     last_result_id: str = "0-0") -> tuple[list[dict], str, str]:
    """Tail the alert + result streams for SSE forwarding.

    Reads only entries newer than the supplied cursors (never the whole stream)
    and returns the updated cursors so the next call resumes where this one
    stopped. The previous implementation used ``bus.drain`` (``XRANGE - +``),
    which re-read every historical entry on every tick and replayed stale
    alerts into the live feed.
    """
    out: list[dict] = []
    try:
        msgs, last_alert_id = await bus.read_since("clams:alerts", last_alert_id)
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
        msgs, last_result_id = await bus.read_since("clams:results", last_result_id)
        for m in msgs:
            out.append({"event": "planner:reply", "data": {
                "kind": "task_result", "payload": m.payload,
                "timestamp": _now_iso(),
            }})
    except Exception:
        pass
    return out, last_alert_id, last_result_id


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


# Config keys whose values must never reach the browser.
SECRET_KEYS = ("token", "auth_token", "api_key", "password", "secret")
REDACTED = "***redacted***"


def _redact(obj: Any) -> Any:
    """Recursively mask secret-valued keys before sending config to the UI."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if any(s in str(k).lower() for s in SECRET_KEYS):
                out[k] = REDACTED if v else ""
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def _settings_dict(settings: Settings) -> dict:
    """Serialise the live settings for the UI editor, with secrets masked."""
    try:
        return _redact(settings.model_dump(mode="json"))
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


def _find_dist(settings: Settings) -> Optional[Path]:
    """Locate the built SPA.

    An installed package lives in site-packages, so a path relative to
    ``__file__`` alone is wrong in a container where the app tree is at /app.
    Try the configured path first, then the locations that actually occur in
    practice, and only fall back silently if none exist.
    """
    candidates: list[Path] = []
    configured = getattr(settings.api, "static_dir", "") or ""
    if configured:
        candidates.append(Path(configured).expanduser())
    here = Path(__file__).resolve()
    candidates += [
        Path("/app/frontend/dist"),
        here.parents[2] / "frontend" / "dist",
        Path.cwd() / "frontend" / "dist",
    ]
    for c in candidates:
        if (c / "index.html").is_file():
            return c
    return None


def _mount_static(app: FastAPI, settings: Settings) -> None:
    """Serve the built SPA if it exists, with an SPA fallback route."""
    dist = _find_dist(settings)
    if dist is None:
        log.warning(
            "frontend build not found; serving API only. Expected an index.html "
            "under /app/frontend/dist, %s, or %s",
            str(Path(__file__).resolve().parents[2] / "frontend" / "dist"),
            str(Path.cwd() / "frontend" / "dist"),
        )
        return
    log.info("serving frontend from %s", dist)
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
