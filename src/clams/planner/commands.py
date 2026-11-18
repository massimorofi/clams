"""Command drain — the API's writes reach the planner only through here (§9.1)."""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from ..db import (repo_commands, repo_events, repo_projects, repo_questions,
                  repo_runtime, repo_tasks, repo_workflows)
from ..db.conn import immediate

log = logging.getLogger("clams.commands")

TERMINAL_TASK = {"success", "failed", "cancelled"}


def drain_commands(conn, *, adhoc_handler=None) -> int:
    """Apply all new commands. Returns count applied."""
    rows = repo_commands.fetch_new(conn)
    n = 0
    for row in rows:
        try:
            with immediate(conn):
                result = _apply(conn, row, adhoc_handler=adhoc_handler)
            repo_commands.mark(conn, row["command_id"], "applied", result)
            n += 1
        except Exception as exc:  # noqa: BLE001
            repo_commands.mark(conn, row["command_id"], "rejected",
                               {"error": str(exc)[:500]})
            log.warning("command %s (%s) rejected: %s",
                        row["command_id"], row["kind"], exc)
    return n


def _apply(conn, row, *, adhoc_handler=None) -> dict[str, Any]:
    kind = row["kind"]
    target = row["target"]
    payload = json.loads(row["payload"]) if row["payload"] else {}
    issuer = row["issued_by"]

    if kind == "pause":
        repo_runtime.pause(conn, "operator")
        repo_events.append_event(conn, "system", "runtime", "info",
                                 f"paused by {issuer}")
        return {"paused": True}

    if kind == "resume":
        ok = repo_runtime.resume(conn, "operator")
        repo_events.append_event(conn, "system", "runtime", "info",
                                 f"resumed by {issuer}")
        return {"resumed": ok}

    if kind == "cancel":
        return _cancel(conn, target, payload, issuer)

    if kind == "replan":
        wf = repo_workflows.get_workflow(conn, target or "")
        if wf is None:
            raise ValueError(f"unknown workflow {target}")
        if wf["status"] not in ("failed", "blocked", "review", "running"):
            raise ValueError(f"workflow {target} not replannable from {wf['status']}")
        repo_workflows.set_status(conn, wf["workflow_id"], "planning")
        repo_events.append_event(conn, "workflow", wf["workflow_id"], "info",
                                 f"replan requested by {issuer}")
        return {"workflow": wf["workflow_id"], "status": "planning"}

    if kind == "retry":
        task = repo_tasks.get_task(conn, target or "")
        if task is None:
            raise ValueError(f"unknown task {target}")
        if task["status"] != "failed":
            raise ValueError(f"task {target} is {task['status']}, not failed")
        # operator override: re-open a failed task with a fresh attempt budget
        conn.execute(
            "UPDATE tasks SET status='ready', attempt=0, lease_token=NULL, "
            "lease_expires_at=NULL, executor_id=NULL, updated_at=CURRENT_TIMESTAMP "
            "WHERE task_id=?", (task["task_id"],))
        repo_events.append_event(conn, "task", task["task_id"], "info",
                                 f"retried by {issuer}")
        return {"task": task["task_id"], "status": "ready"}

    if kind == "answer":
        qid = payload.get("question_id")
        answer = payload.get("answer", "")
        q = conn.execute("SELECT * FROM questions WHERE question_id=?",
                         (qid,)).fetchone()
        if q is None:
            raise ValueError(f"unknown question {qid}")
        if not repo_questions.answer(conn, qid, answer, issuer):
            raise ValueError(f"question {qid} not open")
        task = repo_tasks.get_task(conn, q["task_id"])
        if task and task["status"] == "waiting_input":
            repo_tasks.set_brief_field(
                conn, task["task_id"],
                {"answer": answer,
                 "session_ref": task["session_ref"] or None})
            repo_tasks.transition(conn, task["task_id"], "ready")
        repo_events.append_event(conn, "question", str(qid), "info",
                                 f"answered by {issuer}")
        return {"question": qid, "task": q["task_id"]}

    if kind == "adhoc":
        goal = payload.get("goal", "").strip()
        if not goal:
            raise ValueError("adhoc command needs payload.goal")
        repo = payload.get("repo") or "adhoc/local"
        pid = repo_projects.upsert_project(conn, repo)
        wf = repo_workflows.create_workflow(conn, pid, "adhoc", goal)
        repo_events.append_event(conn, "workflow", wf, "info",
                                 f"adhoc workflow created by {issuer}")
        if adhoc_handler:
            adhoc_handler(conn, wf)
        return {"workflow": wf}

    if kind == "config_patch":
        return _config_patch(conn, payload, issuer)

    raise ValueError(f"unhandled command kind {kind}")


def _cancel(conn, target: Optional[str], payload: dict, issuer: str) -> dict:
    if not target:
        raise ValueError("cancel needs a target (workflow or task id)")
    task = repo_tasks.get_task(conn, target)
    if task is not None:
        if task["status"] in TERMINAL_TASK:
            raise ValueError(f"task {target} already {task['status']}")
        repo_tasks.transition(conn, target, "cancelled")
        repo_questions.cancel_open_for_task(conn, target)
        repo_events.append_event(conn, "task", target, "info",
                                 f"cancelled by {issuer}")
        return {"task": target, "status": "cancelled"}
    wf = repo_workflows.get_workflow(conn, target)
    if wf is None:
        raise ValueError(f"unknown cancel target {target}")
    for t in repo_tasks.list_tasks(conn, target):
        if t["status"] not in TERMINAL_TASK:
            repo_tasks.transition(conn, t["task_id"], "cancelled")
    repo_workflows.set_status(conn, target, "cancelled")
    repo_events.append_event(conn, "workflow", target, "info",
                             f"cancelled by {issuer}")
    return {"workflow": target, "status": "cancelled"}


def _config_patch(conn, payload: dict, issuer: str) -> dict:
    projects = payload.get("projects")
    if not isinstance(projects, list):
        raise ValueError("config_patch needs projects: [...]")
    applied, removed = [], []
    for p in projects:
        repo = p.get("repo")
        if not repo or "/" not in repo:
            raise ValueError(f"bad project repo {repo!r}")
        if p.get("remove"):
            existing = repo_projects.get_project(conn, repo)
            if existing is None:
                raise ValueError(f"project {repo!r} is not registered")
            n = repo_workflows.count_for_project(conn, existing["project_id"])
            if n:
                raise ValueError(
                    f"{repo} has {n} workflow(s) — cancel them before removing")
            repo_projects.delete_project(conn, repo)
            removed.append(repo)
            continue
        repo_projects.upsert_project(
            conn, repo,
            gh_project_node_id=p.get("gh_project_node_id"),
            status_field_id=p.get("status_field_id"),
            ready_option_id=p.get("ready_option_id"),
            enabled=bool(p.get("enabled", True)))
        applied.append(repo)
    repo_events.append_event(conn, "system", "config", "info",
                             f"config_patch by {issuer}: "
                             f"applied={applied} removed={removed}")
    return {"projects": applied, "removed": removed}
