"""Event application (fenced + idempotent) and workflow review/replan (§6)."""
from __future__ import annotations

import json
import logging
from typing import Optional

from .. import contracts
from ..db import (repo_alerts, repo_events, repo_executors, repo_outbox,
                  repo_questions, repo_tasks, repo_workflows)
from ..db.conn import immediate

log = logging.getLogger("clams.review")


def apply_event(conn, msg, msg_id: str, settings, *, gh_enabled: bool = False) -> str:
    """Apply one clams:events message idempotently. Returns 'applied'|'dup'|'fenced'.

    The caller XACKs only after this transaction commits.
    """
    with immediate(conn):
        from ..db import repo_events as re_
        if re_.was_processed(conn, msg_id):
            return "dup"
        status = _apply(conn, msg, settings, gh_enabled=gh_enabled)
        re_.mark_processed(conn, msg_id)
        return status


def _apply(conn, msg, settings, *, gh_enabled: bool) -> str:
    t = msg.type
    if t == "executor.alive":
        repo_executors.upsert_alive(conn, msg.executor_id, msg.version,
                                    msg.capacity)
        return "applied"

    if t == "task.started":
        row = repo_tasks.on_started(conn, msg.task_id, msg.attempt,
                                    msg.lease_token, msg.executor_id,
                                    msg.session_ref,
                                    extend_s=settings.lease_running_extend_s)
        if row is None:
            log.info("fenced task.started %s a%s", msg.task_id, msg.attempt)
            return "fenced"
        repo_events.append_event(conn, "task", msg.task_id, "info",
                                 f"started by {msg.executor_id}")
        return "applied"

    if t == "task.heartbeat":
        # metrics only — the watchdog renews the lease from session evidence
        row = repo_tasks.on_heartbeat(conn, msg.task_id, msg.attempt,
                                      msg.lease_token,
                                      turns_used=msg.turns_used,
                                      elapsed_s=msg.elapsed_s)
        return "applied" if row else "fenced"

    if t == "task.question":
        qrow = repo_tasks.get_task(conn, msg.task_id)
        if qrow is None or qrow["attempt"] != msg.attempt \
                or qrow["lease_token"] != msg.lease_token:
            return "fenced"
        repo_questions.open_question(conn, msg.task_id, msg.attempt,
                                     msg.question, msg.options,
                                     msg.idempotency_key,
                                     expires_hours=settings.question_expiry_h)
        row = repo_tasks.on_question(conn, msg.task_id, msg.attempt,
                                     msg.lease_token)
        if row is None:
            return "fenced"
        repo_events.append_event(conn, "task", msg.task_id, "warn",
                                 f"question: {msg.question[:200]}")
        if gh_enabled:
            _project_question(conn, msg, settings)
        return "applied"

    if t == "task.result":
        gate_passed = bool(msg.evidence.get("passed"))
        row = repo_tasks.on_result(conn, msg.task_id, msg.attempt,
                                   msg.lease_token, msg.outcome,
                                   gate_passed,
                                   {"outcome": msg.outcome,
                                    "evidence": msg.evidence,
                                    "commit_sha": msg.commit_sha,
                                    "branch": msg.branch,
                                    "changed_files": msg.changed_files,
                                    "log_tail": msg.log_tail[-2000:],
                                    "session_ref": msg.session_ref},
                                   error_code=getattr(msg, "error_code", ""))
        if row is None:
            log.info("fenced task.result %s a%s", msg.task_id, msg.attempt)
            return "fenced"
        if row["status"] == "failed" and getattr(msg, "error_code", "") \
                and msg.error_code.startswith(repo_tasks.CONFIG_CLASS_ERRORS):
            repo_alerts.raise_alert(
                conn, f"config:{msg.error_code}", "error",
                f"task {msg.task_id} failed with config-class error "
                f"{msg.error_code} — fix the configuration, retries were "
                f"skipped: {msg.log_tail[:200]}")
        infra_requeue = (row["status"] == "ready" and getattr(msg, "error_code", "")
                         and msg.error_code.startswith(repo_tasks.INFRA_ERROR_CODES))
        if infra_requeue:
            repo_alerts.raise_alert(
                conn, f"infra:{msg.task_id}", "warn",
                f"task {msg.task_id} requeued without burning attempt "
                f"{msg.attempt}: infrastructure fault ({msg.error_code}) — "
                "claudio unreachable")
        level = "info" if row["status"] == "success" else "warn"
        repo_events.append_event(
            conn, "task", msg.task_id, level,
            f"attempt {msg.attempt} -> {row['status']}"
            + (" (infra requeue, attempt preserved)" if infra_requeue else "")
            + (f" ({msg.evidence.get('reason', '')})"
               if row["status"] == "ready" and not infra_requeue else ""))
        if row["status"] == "failed":
            strand_dependents(conn, row["workflow_id"])
        if gh_enabled:
            from ..github import projection
            projection.project_task_state(conn, msg.task_id,
                                          _repo_of(conn, msg.task_id),
                                          row["status"], msg.attempt)
        review_workflow(conn, row["workflow_id"], settings,
                        gh_enabled=gh_enabled)
        return "applied"

    log.warning("ignoring event type %s", t)
    return "applied"


def strand_dependents(conn, workflow_id: str) -> list[str]:
    """Cascade-block pending/ready tasks stranded by a failed/blocked/cancelled
    dependency, with audit events. Idempotent; caller holds the transaction."""
    blocked = repo_tasks.block_dependents(conn, workflow_id)
    for tid in blocked:
        repo_events.append_event(conn, "task", tid, "warn",
                                 "blocked: dependency failed")
    return blocked


def review_workflow(conn, workflow_id: str, settings,
                    *, gh_enabled: bool = False) -> None:
    """When all tasks terminal: done / replan (<=max) / blocked + alert (§6).

    Public: called from event application, the lease reaper, command handling
    and the loop's self-heal sweep. A workflow mid-planning (replan in flight)
    or in intake is never reviewed here.
    """
    wf = repo_workflows.get_workflow(conn, workflow_id)
    if wf is None or wf["status"] in ("cancelled", "paused", "done", "blocked",
                                      "intake", "planning"):
        return
    if not repo_tasks.workflow_tasks_all_terminal(conn, workflow_id):
        return
    repo_workflows.set_status(conn, workflow_id, "review")
    outcome = repo_tasks.workflow_outcome(conn, workflow_id)
    if outcome == "blocked":
        # stranded work (question expiry / cancelled dep): needs a human,
        # not a replan — replanning would discard successes and re-ask.
        repo_workflows.set_status(conn, workflow_id, "blocked")
        repo_alerts.raise_alert(conn, "workflow", "error",
                                f"workflow {workflow_id} blocked: "
                                "task(s) need attention")
        repo_events.append_event(conn, "workflow", workflow_id, "error",
                                 "blocked: task(s) need attention")
        if gh_enabled:
            _project_blocked(conn, workflow_id, settings)
            from ..github import projection
            projection.project_workflow_state(conn, workflow_id,
                                              _wf_repo(conn, wf), "blocked")
        return
    if outcome == "done":
        repo_workflows.set_status(conn, workflow_id, "done")
        repo_events.append_event(conn, "workflow", workflow_id, "info", "done")
        if gh_enabled:
            _project_done(conn, workflow_id, settings)
            from ..github import projection
            projection.project_workflow_state(conn, workflow_id,
                                              _wf_repo(conn, wf), "done")
        return
    if outcome == "cancelled":
        repo_workflows.set_status(conn, workflow_id, "cancelled")
        if gh_enabled:
            from ..github import projection
            projection.project_workflow_state(conn, workflow_id,
                                              _wf_repo(conn, wf), "cancelled")
        return
    # failed -> replan or block
    if wf["replans"] < settings.max_replans:
        n = repo_workflows.bump_replans(conn, workflow_id)
        repo_workflows.set_status(conn, workflow_id, "planning")
        repo_events.append_event(conn, "workflow", workflow_id, "warn",
                                 f"replan {n}/{settings.max_replans}")
    else:
        repo_workflows.set_status(conn, workflow_id, "blocked")
        repo_alerts.raise_alert(conn, "workflow", "error",
                                f"workflow {workflow_id} blocked after "
                                f"{settings.max_replans} replans")
        repo_events.append_event(conn, "workflow", workflow_id, "error",
                                 "blocked: replans exhausted")
        if gh_enabled:
            _project_blocked(conn, workflow_id, settings)
            from ..github import projection
            projection.project_workflow_state(conn, workflow_id,
                                              _wf_repo(conn, wf), "blocked")


# --------------------------------------------------------------- projections
def _task_issue(conn, task_id: str) -> Optional[int]:
    row = repo_tasks.get_task(conn, task_id)
    return row["gh_issue_number"] if row else None


def _project_question(conn, msg, settings) -> None:
    issue = _task_issue(conn, msg.task_id)
    if issue is None:
        return
    repo_outbox.enqueue(
        conn, "gh_comment",
        {"repo": _repo_of(conn, msg.task_id), "issue": issue,
         "body": f"**Clams needs input** (attempt {msg.attempt}): "
                 f"{msg.question}\nOptions: {', '.join(msg.options) or '-'}\n"
                 f"<!-- gh:gh_comment:{msg.task_id}:{msg.attempt}:question -->",
         "idempotency_key": f"gh:gh_comment:{msg.task_id}:{msg.attempt}:question"},
        idempotency_key=f"gh:gh_comment:{msg.task_id}:{msg.attempt}:question")


def _project_done(conn, workflow_id: str, settings) -> None:
    wf = repo_workflows.get_workflow(conn, workflow_id)
    if wf and wf["gh_issue_number"]:
        repo_outbox.enqueue(
            conn, "gh_comment",
            {"repo": _wf_repo(conn, wf), "issue": wf["gh_issue_number"],
             "body": "Clams: workflow complete, all tasks passed the evidence "
                     f"gate. <!-- gh:gh_comment:{workflow_id}:done -->",
             "idempotency_key": f"gh:gh_comment:{workflow_id}:done"},
            idempotency_key=f"gh:gh_comment:{workflow_id}:done")


def _project_blocked(conn, workflow_id: str, settings) -> None:
    wf = repo_workflows.get_workflow(conn, workflow_id)
    if wf and wf["gh_issue_number"]:
        repo_outbox.enqueue(
            conn, "gh_comment",
            {"repo": _wf_repo(conn, wf), "issue": wf["gh_issue_number"],
             "body": "Clams: workflow blocked after exhausting replans. "
                     f"<!-- gh:gh_comment:{workflow_id}:blocked -->",
             "idempotency_key": f"gh:gh_comment:{workflow_id}:blocked"},
            idempotency_key=f"gh:gh_comment:{workflow_id}:blocked")


def _repo_of(conn, task_id: str) -> str:
    row = repo_tasks.get_task(conn, task_id)
    if row:
        try:
            return json.loads(row["brief_json"]).get("repo", "")
        except Exception:  # noqa: BLE001
            return ""
    return ""


def _wf_repo(conn, wf) -> str:
    from ..db import repo_projects
    p = conn.execute("SELECT repo FROM projects WHERE project_id=?",
                     (wf["project_id"],)).fetchone()
    return p["repo"] if p else ""
