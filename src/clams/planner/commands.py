"""Command drain — the API's writes reach the planner only through here (§9.1)."""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from ..db import (repo_commands, repo_events, repo_projects, repo_questions,
                  repo_runtime, repo_tasks, repo_workflows)
from ..db import repo_handoffs, repo_progress
from ..handoffs import Handoff, WorkerProgress
from ..db.conn import immediate

log = logging.getLogger("clams.commands")

TERMINAL_TASK = {"success", "failed", "cancelled"}

# https://github.com/users/<owner>/projects/<n> | /organizations/<owner>/... | <owner>/...
_BOARD_URL_RE = re.compile(
    r"github\.com/(?:users/|organizations/|orgs/)?([^/]+)/projects/(\d+)")


def drain_commands(conn, *, adhoc_handler=None, gh_client=None,
                   claudio_e=None) -> int:
    """Apply all new commands. Returns count applied. claudio_e (if given)
    enables cooperative cancellation of in-flight agent runs."""
    rows = repo_commands.fetch_new(conn)
    n = 0
    for row in rows:
        try:
            with immediate(conn):
                result = _apply(conn, row, adhoc_handler=adhoc_handler,
                                gh_client=gh_client, claudio_e=claudio_e)
            repo_commands.mark(conn, row["command_id"], "applied", result)
            n += 1
        except Exception as exc:  # noqa: BLE001
            repo_commands.mark(conn, row["command_id"], "rejected",
                               {"error": str(exc)[:500]})
            log.warning("command %s (%s) rejected: %s",
                        row["command_id"], row["kind"], exc)
    return n


def _apply(conn, row, *, adhoc_handler=None, gh_client=None,
           claudio_e=None) -> dict[str, Any]:
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
        return _cancel(conn, target, payload, issuer, claudio_e=claudio_e)

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
        if task["status"] not in ("failed", "blocked"):
            raise ValueError(
                f"task {target} is {task['status']}, not failed/blocked")
        # operator override: re-open a failed task with a fresh attempt budget
        conn.execute(
            "UPDATE tasks SET status='ready', attempt=0, lease_token=NULL, "
            "lease_expires_at=NULL, executor_id=NULL, updated_at=CURRENT_TIMESTAMP "
            "WHERE task_id=?", (task["task_id"],))
        repo_events.append_event(conn, "task", task["task_id"], "info",
                                 f"retried by {issuer}")
        # A retry re-opens the workflow it came from. review_workflow returns
        # early for 'blocked', so without this the workflow could never reach
        # done/replan again even after every task succeeded.
        wf = repo_workflows.get_workflow(conn, task["workflow_id"])
        if wf is not None and wf["status"] == "blocked":
            repo_workflows.set_status(conn, wf["workflow_id"], "running")
            repo_events.append_event(conn, "workflow", wf["workflow_id"],
                                     "info", f"reopened by retry from {issuer}")
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
        return _config_patch(conn, payload, issuer, gh_client=gh_client)

    if kind == "settings_patch":
        return _settings_patch(conn, payload, issuer)

    if kind == "prompt_patch":
        return _prompt_patch(conn, payload, issuer)

    if kind == "apply_plan":
        return _apply_plan(conn, payload, issuer, target)

    if kind == "dispose_handoff":
        return _dispose_handoff(conn, payload, issuer, target)

    raise ValueError(f"unhandled command kind {kind}")


def _settings_patch(conn, payload: dict, issuer: str) -> dict:
    """Runtime tuning of planner-side execution-flow settings.

    Overrides persist in runtime_state and are re-applied by the planner loop
    at the start of every cycle, so they survive restarts. Env/infra settings
    (paths, URLs, tokens) are intentionally not editable here.
    """
    from ..db.conn import get_runtime_state, set_runtime_state
    from ..config import EDITABLE_SETTINGS, coerce_setting
    patch = payload.get("settings") or {}
    if not isinstance(patch, dict) or not patch:
        raise ValueError("settings_patch needs payload.settings {name: value}")
    unknown = [k for k in patch if k not in EDITABLE_SETTINGS]
    if unknown:
        raise ValueError(f"not editable at runtime: {', '.join(unknown)}")
    merged = json.loads(get_runtime_state(conn, "settings_overrides") or "{}")
    applied = {}
    for k, v in patch.items():
        merged[k] = coerce_setting(k, v)
        applied[k] = merged[k]
    set_runtime_state(conn, "settings_overrides", json.dumps(merged))
    repo_events.append_event(conn, "system", "settings", "info",
                             f"settings_patch by {issuer}: "
                             + ", ".join(f"{k}={v}" for k, v in applied.items()))
    return {"applied": applied, "overrides": merged}


def _prompt_patch(conn, payload: dict, issuer: str) -> dict:
    """Install (or reset) an operator prompt template.

    Overrides persist in runtime_state and are read at every render, so they
    take effect from the next planning call / dispatch onward. An empty or
    null template resets that prompt to the built-in default.
    """
    from .. import prompts
    name = payload.get("name")
    if not isinstance(name, str) or name not in prompts.PROMPTS:
        raise ValueError(f"prompt_patch needs payload.name in "
                         f"{', '.join(prompts.PROMPTS)}")
    template = payload.get("template")
    if template is not None and not isinstance(template, str):
        raise ValueError("prompt_patch payload.template must be a string or null")
    error = prompts.validate(name, template)
    if error:
        raise ValueError(error)
    merged = prompts.set_override(conn, name, template)
    action = "reset to default" if not template else "updated"
    repo_events.append_event(conn, "system", "prompts", "info",
                             f"prompt_patch by {issuer}: {name} {action}")
    return {"name": name, "action": action,
            "overridden": sorted(merged.keys())}


def _apply_plan(conn, payload: dict, issuer: str,
                target: Optional[str]) -> dict:
    """Install an operator-approved plan directly — no LLM call.

    Closes the loop for plans authored by the planner agent (or by hand):
    the plan must pass the exact validation LLM plans go through, then the
    swap mirrors plan_workflow (old task set superseded atomically). NOTE:
    runs inside drain_commands' BEGIN IMMEDIATE — must not open its own.
    """
    from .. import contracts
    from ..config import get_settings
    from .plan import build_brief

    wf_id = payload.get("workflow_id") or target or ""
    wf = repo_workflows.get_workflow(conn, wf_id)
    if wf is None:
        raise ValueError(f"unknown workflow {wf_id}")
    if wf["status"] not in ("failed", "blocked", "review", "running"):
        raise ValueError(f"workflow {wf_id} not replannable from {wf['status']}")
    raw = payload.get("plan")
    if not isinstance(raw, dict):
        raise ValueError("apply_plan needs payload.plan {tasks: [...]}")
    s = get_settings()
    try:
        plan = contracts.Plan.model_validate(raw)
        contracts.validate_plan(plan, max_tasks=s.max_plan_tasks)
    except Exception as exc:  # noqa: BLE001 - surfaced as command rejection
        raise ValueError(f"invalid plan: {str(exc)[:300]}") from exc
    row = conn.execute("SELECT repo FROM projects WHERE project_id=?",
                       (wf["project_id"],)).fetchone()
    repo = row["repo"] if row else "unknown/repo"
    base_ref = "main"
    for p in s.load_projects_yaml().get("projects", []):
        if p.get("repo") == repo:
            base_ref = p.get("base_ref", "main")
            break
    n_old = repo_tasks.delete_workflow_tasks(conn, wf_id)
    for i, pt in enumerate(plan.tasks, start=1):
        repo_tasks.create_task(
            conn, wf_id, i, pt.title, pt.objective, pt.acceptance,
            build_brief(pt, repo, base_ref, s),
            depends_on=pt.depends_on, max_attempts=s.max_attempts)
    repo_workflows.set_plan(conn, wf_id, plan.model_dump(), status="running")
    repo_events.append_event(
        conn, "workflow", wf_id, "warn",
        f"apply_plan by {issuer}: replaced {n_old} tasks with "
        f"{len(plan.tasks)}")
    log.info("apply_plan on %s by %s: %d tasks", wf_id, issuer, len(plan.tasks))
    return {"workflow": wf_id, "tasks": len(plan.tasks), "replaced": n_old}


def _stop_agent_run(claudio_e, session_ref: str) -> bool:
    """Best-effort: ask Claudio to abort the session's in-flight run so the
    agent actually stops instead of burning turns until its chat call ends.
    The DB cancel already fenced the task; this is the courtesy stop."""
    if claudio_e is None or not session_ref:
        return False
    run_id = claudio_e.active_run(session_ref)
    return bool(run_id) and claudio_e.cancel(run_id)


def _cancel(conn, target: Optional[str], payload: dict, issuer: str,
            *, claudio_e=None) -> dict:
    if not target:
        raise ValueError("cancel needs a target (workflow or task id)")
    task = repo_tasks.get_task(conn, target)
    if task is not None:
        if task["status"] in TERMINAL_TASK:
            raise ValueError(f"task {target} already {task['status']}")
        was_running = task["status"] == "running"
        repo_tasks.transition(conn, target, "cancelled",
                              lease_token=None, lease_expires_at=None,
                              executor_id=None)
        repo_questions.cancel_open_for_task(conn, target)
        repo_events.append_event(conn, "task", target, "info",
                                 f"cancelled by {issuer}")
        if was_running and _stop_agent_run(claudio_e, task["session_ref"]):
            repo_events.append_event(conn, "task", target, "info",
                                     "claudio run cancelled")
        # a cancelled task strands its dependents just like a failure; the
        # loop's sweep reviews the workflow once all tasks are terminal
        from .review import strand_dependents
        strand_dependents(conn, task["workflow_id"])
        return {"task": target, "status": "cancelled"}
    wf = repo_workflows.get_workflow(conn, target)
    if wf is None:
        raise ValueError(f"unknown cancel target {target}")
    stopped = 0
    for t in repo_tasks.list_tasks(conn, target):
        if t["status"] not in TERMINAL_TASK:
            was_running = t["status"] == "running"
            repo_tasks.transition(conn, t["task_id"], "cancelled",
                                  lease_token=None, lease_expires_at=None,
                                  executor_id=None)
            if was_running and _stop_agent_run(claudio_e, t["session_ref"]):
                stopped += 1
    repo_workflows.set_status(conn, target, "cancelled")
    repo_events.append_event(conn, "workflow", target, "info",
                             f"cancelled by {issuer}"
                             + (f" ({stopped} running agent run(s) stopped)"
                                if stopped else ""))
    return {"workflow": target, "status": "cancelled",
            "agent_runs_stopped": stopped}


def _resolve_board(gh_client, board_url: str) -> dict:
    """Turn a Projects V2 board URL into stored node IDs.

    Returns a dict with gh_project_node_id / status_field_id / ready_option_id.
    Raises ValueError with an operator-readable reason on any failure.
    """
    if gh_client is None:
        raise ValueError("gh_board_url needs a configured GitHub client "
                         "(CLAMS_GITHUB_TOKEN)")
    m = _BOARD_URL_RE.search(str(board_url))
    if not m:
        raise ValueError(f"cannot parse board URL {board_url!r} "
                         "(expected github.com/users/<owner>/projects/<n>)")
    owner, number = m.group(1), int(m.group(2))
    try:
        info = gh_client.resolve_project(owner, number)
    except Exception as exc:  # noqa: BLE001 — raw GraphQL/HTTP dumps are noise
        raise ValueError(
            f"could not resolve board {owner}/projects/{number} — is it a "
            "user board, and does the GitHub token have the 'project' scope? "
            f"({str(exc)[:120]})") from exc
    if info is None:
        raise ValueError(
            f"board {owner}/projects/{number} not found — user boards only, "
            "and the token needs the 'project' scope")
    options = info.get("options", {})
    if "Ready" not in options:
        raise ValueError(f"board {owner}/projects/{number} has no 'Ready' "
                         f"Status option (has: {sorted(options)})")
    return {"gh_project_node_id": info["node_id"],
            "status_field_id": info["status_field_id"],
            "ready_option_id": options["Ready"]}


def _config_patch(conn, payload: dict, issuer: str,
                  gh_client=None) -> dict:
    projects = payload.get("projects")
    if not isinstance(projects, list):
        raise ValueError("config_patch needs projects: [...]")
    applied, removed = [], []
    for p in projects:
        repo = p.get("repo")
        if not repo or "/" not in repo:
            raise ValueError(f"bad project repo {repo!r}")
        if p.get("gh_board_url") and not p.get("gh_project_node_id"):
            p = {**p, **_resolve_board(gh_client, p["gh_board_url"])}
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


# ------------------------------------------------------------- handoffs
def _dispose_handoff(conn, payload: dict, issuer: str,
                     target: Optional[str]) -> dict:
    """Operator disposition of a worker handoff (open -> acknowledged -> ...).

    The API never touches the handoffs table: it enqueues this command and the
    planner (the only domain-state writer) applies the transition. Idempotent:
    re-running the same disposition is a no-op, so a replayed command cannot
    corrupt the history. A handoff that asked for a scope change is *answered*
    here; the task brief is never edited by this path.
    """
    hid = payload.get("handoff_id")
    if hid is None and target is not None:
        try:
            hid = int(target)
        except (TypeError, ValueError):
            raise ValueError("dispose_handoff needs payload.handoff_id")
    if hid is None:
        raise ValueError("dispose_handoff needs payload.handoff_id")
    try:
        hid = int(hid)
    except (TypeError, ValueError):
        raise ValueError(f"bad handoff id {hid!r}")
    to_status = payload.get("status") or payload.get("to_status") or ""
    if not to_status:
        raise ValueError("dispose_handoff needs payload.status "
                         "(acknowledged|resolved|rejected)")
    res = repo_handoffs.dispose(
        conn, hid, to_status,
        disposition=payload.get("disposition") or "",
        disposed_by=issuer or "operator",
        note=payload.get("note") or "")
    if not res.get("ok"):
        reason = res.get("reason", "failed")
        raise ValueError(f"cannot dispose handoff {hid}: {reason}")
    return {"handoff": hid, "status": res["status"],
            "changed": res.get("changed", False),
            "disposition": res.get("disposition", "")}


# ------------------------------------------------- worker narration ingest
#: bus event types that carry worker progress / handoff requests
WORKER_NARRATION_TYPES = ("task.progress", "task.handoff")


def _decode_payload(raw) -> Optional[dict]:
    """Best-effort decode of a bus payload into a dict (None if not one)."""
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return raw if isinstance(raw, dict) else None


def worker_narration_type(raw) -> Optional[str]:
    """The narration type if this payload is worker progress/handoff, else None.

    Checked on the *raw* payload (before the pydantic message contracts, which
    only know the core event types) so the loop can route these messages
    without a contract change.
    """
    data = _decode_payload(raw)
    if not data:
        return None
    etype = data.get("type") or data.get("event")
    return etype if etype in WORKER_NARRATION_TYPES else None


def ingest_worker_payload(conn, raw, *, msg_id: Optional[str] = None) -> dict:
    """Persist one worker progress/handoff message and surface it.

    Called by the planner loop when it reads clams:events. Returns a result
    dict ``{"kind", "accepted"/"id", ...}``. Raises ValueError on a malformed
    payload so the caller can leave the message unacked / DLQ it.

    Idempotent on redelivery: progress is fenced on (task, attempt, lease, seq)
    and a duplicate seq is rejected without writing; handoffs dedupe on their
    natural key.
    """
    data = _decode_payload(raw)
    if data is None:
        raise ValueError("worker narration payload is not a JSON object")
    if msg_id and repo_events.was_processed(conn, msg_id):
        return {"kind": data.get("type"), "duplicate": True}
    etype = data.get("type") or data.get("event")
    body = data.get("payload") if isinstance(data.get("payload"), dict) else data
    if etype == "task.progress":
        res = _ingest_progress(conn, body, data)
    elif etype == "task.handoff":
        res = _ingest_handoff(conn, body, data)
    else:
        raise ValueError(f"not a worker narration message: {etype!r}")
    if msg_id:
        repo_events.mark_processed(conn, msg_id)
    return res


def _task_workflow_id(conn, task_id: str) -> str:
    row = conn.execute("SELECT workflow_id FROM tasks WHERE task_id=?",
                       (task_id,)).fetchone()
    return row["workflow_id"] if row else ""


def _ingest_progress(conn, body: dict, envelope: dict) -> dict:
    """Fenced append of one worker progress line."""
    rec = WorkerProgress.from_payload({
        "task_id": body.get("task_id") or envelope.get("task_id"),
        "attempt_no": body.get("attempt_no", envelope.get("attempt", 0)),
        "seq": body.get("seq", envelope.get("seq", 0)),
        "kind": body.get("kind") or "log",
        "text": body.get("text", envelope.get("text", "")),
        "lease_token": body.get("lease_token") or envelope.get("lease_token") or "",
    })
    res = repo_progress.append_progress_record(conn, rec)
    if res["accepted"]:
        log.debug("progress %s/%s seq=%s %s", rec.task_id, rec.attempt_no,
                  rec.seq, rec.kind)
    else:
        # surfaced (never silent): a stale executor is worth seeing in the
        # timeline, but it changes no state
        repo_events.append_event(
            conn, "task", rec.task_id, "warn",
            f"rejected stale progress ({res['reason']}, attempt "
            f"{rec.attempt_no}, seq {rec.seq})",
            {"reason": res["reason"], "attempt_no": rec.attempt_no,
             "seq": rec.seq})
        log.info("rejected progress for %s: %s", rec.task_id, res["reason"])
    return {"kind": "task.progress", **res}


def _ingest_handoff(conn, body: dict, envelope: dict) -> dict:
    """Record a handoff request. Never edits the task brief."""
    payload = {
        "task_id": body.get("task_id") or envelope.get("task_id"),
        "workflow_id": (body.get("workflow_id") or envelope.get("workflow_id")
                        or ""),
        "attempt_no": body.get("attempt_no", envelope.get("attempt", 0)),
        "kind": body.get("kind") or "other",
        "reason": body.get("reason", ""),
        "requested_action": body.get("requested_action", ""),
    }
    if not payload["workflow_id"]:
        payload["workflow_id"] = _task_workflow_id(conn, payload["task_id"])
    if not payload["workflow_id"]:
        raise ValueError(f"handoff for unknown task {payload['task_id']!r}")
    handoff = Handoff.from_payload(payload)
    res = repo_handoffs.record_handoff(conn, handoff)
    if handoff.asks_scope_change:
        # Explicitly NOT an edit: the brief stays exactly as planned. The
        # operator answers by disposing the handoff (and, if they agree,
        # replanning / apply_plan through the normal path).
        repo_events.append_event(
            conn, "task", handoff.task_id, "warn",
            f"scope change requested (handoff #{res['id']}); brief left "
            "immutable - answer via dispose_handoff or replan",
            {"handoff_id": res["id"],
             "requested_action": handoff.requested_action})
    log.info("handoff #%s recorded for %s (%s)", res["id"], handoff.task_id,
             handoff.kind)
    return {"kind": "task.handoff", **res}
