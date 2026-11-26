"""Planning: Claudio-P decomposes a workflow goal into a validated DAG (§8).

Budget: at most ``planning_llm_budget_per_cycle`` LLM calls per planner cycle
(the loop enforces it; this module exposes the call itself).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from .. import contracts, prompts
from ..claudio_client import ClaudioUnavailable
from ..contracts import Plan, PlanTask, PlanValidationError, validate_plan
from ..db import (repo_alerts, repo_events, repo_tasks, repo_workflows)
from ..db.conn import immediate, iso_to_epoch
from . import reconcile

log = logging.getLogger("clams.plan")

# The prompt templates are operator-editable (UI Configuration tab ->
# Prompts); the built-in defaults and the render/override machinery live in
# clams.prompts.


def extract_json(text: str) -> dict[str, Any]:
    """Pull the first balanced JSON object out of a model reply."""
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in reply")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start:i + 1])
    raise ValueError("unbalanced JSON in reply")


def build_brief(pt: PlanTask, repo: str, base_ref: str, settings) -> dict:
    return {
        "schema_version": contracts.SCHEMA_VERSION,
        "objective": pt.objective,
        "acceptance": pt.acceptance,
        "run": pt.run,
        "writable": pt.writable,
        "forbidden": pt.forbidden,
        "repo": repo,
        "base_ref": base_ref,
        "max_turns": settings.max_turns,
    }


def failure_summary(result_json: Optional[str],
                    error_log: Optional[str] = None) -> str:
    """Compact 'why did this attempt fail' line from the evidence gate result.

    Gate-check evidence beats a bare error_log: gate/env failures look
    identical to code failures otherwise. Returns '' when nothing is known.
    """
    detail = ""
    if result_json:
        try:
            res = json.loads(result_json)
            ev = res.get("evidence") or {}
            parts = []
            for c in ev.get("checks", []):
                if c.get("ok"):
                    continue
                cmd = str(c.get("cmd") or c.get("check") or "")[:100]
                tail = [ln for ln in str(c.get("output_tail") or "")
                        .splitlines() if ln.strip()]
                last = f" | {tail[-1][:160]}" if tail else ""
                parts.append(f"gate `{cmd}` exit "
                             f"{c.get('exit', c.get('detail', '?'))}{last}")
            if parts:
                detail = "; ".join(parts[:3])[:400]
            if not detail:
                detail = str(ev.get("reason") or res.get("log_tail") or "")[:200]
        except Exception:  # noqa: BLE001 - malformed result_json must not wedge anything
            detail = ""
    if not detail:
        detail = (error_log or "")[:200]
    return detail


def _prior_task_line(t) -> str:
    """One replan-context line per prior task: status, attempt budget,
    duration, and WHY it failed."""
    detail = failure_summary(t["result_json"], t["error_log"])
    extra = f" (attempt {t['attempt']}/{t['max_attempts']}"
    if t["started_at"] and t["updated_at"]:
        try:
            mins = (iso_to_epoch(t["updated_at"])
                    - iso_to_epoch(t["started_at"])) / 60
            if mins >= 1:
                extra += f", ~{mins:.0f}m"
        except Exception:  # noqa: BLE001
            pass
    extra += ")"
    return (f"- [{t['status']}] {t['title']}{extra}"
            + (f": {detail}" if detail else ""))


def plan_workflow(conn, claudio_p, settings, workflow_id: str,
                  repo: str, base_ref: str = "main") -> str:
    """Plan one workflow. Returns new workflow status.

    Invalid plan -> one repair retry -> else blocked + alert.
    """
    wf = repo_workflows.get_workflow(conn, workflow_id)
    if wf is None:
        raise ValueError(f"unknown workflow {workflow_id}")
    goal = wf["goal"]
    # replan context: the prior task set is superseded by the new plan (§8:
    # the planner receives goal + prior results so it does not redo successes)
    prior = repo_tasks.list_tasks(conn, workflow_id)
    prior_ctx = ""
    if prior:
        lines = [_prior_task_line(t) for t in prior]
        prior_ctx = prompts.render(conn, "plan_replan",
                                   prior="\n".join(lines))
    base_prompt = (prompts.render(conn, "plan",
                                  max_tasks=settings.max_plan_tasks,
                                  repo=repo, base_ref=base_ref, goal=goal)
                   + prior_ctx)
    prompt = base_prompt
    errors: list[str] = []
    plan: Optional[Plan] = None
    transient = False
    for attempt in range(2):  # initial + one repair retry
        try:
            # no timeout: planning is agentic work too; the stall
            # probe alerts if it wedges
            reply = claudio_p.chat(prompt, approval_mode="off",
                                   timeout=None)
            raw = extract_json(reply.final_output)
            plan = Plan.model_validate(raw)
            validate_plan(plan, max_tasks=settings.max_plan_tasks)
            break
        except Exception as exc:  # noqa: BLE001 - any LLM failure -> repair retry
            # an unreachable/timed-out endpoint is an infra fault, not a bad
            # goal; it must not be treated the same as an invalid plan
            transient = isinstance(exc, ClaudioUnavailable)
            errors.append(str(exc)[:300])
            plan = None
            # keep the full original prompt (goal + schema) — a bare error
            # message gives the model nothing to repair against
            prompt = base_prompt + prompts.render(conn, "plan_repair",
                                                  error=errors[-1])
    if plan is None:
        # A workflow with no plan and no tasks must never be parked in
        # 'blocked' by a momentary endpoint outage: 'blocked' is a dead end
        # (the loop only revisits intake/planning), so the workflow would
        # wedge forever without ever having had a plan. Transient faults stay
        # retryable, bounded by the replan budget so a sustained outage still
        # escalates instead of hot-looping every cycle. (claudio#136 shape.)
        if transient and wf["replans"] < settings.max_replans:
            with immediate(conn):
                n = repo_workflows.bump_replans(conn, workflow_id)
                repo_workflows.set_status(conn, workflow_id, "planning")
                repo_alerts.raise_alert(
                    conn, "plan", "warn",
                    f"workflow {workflow_id}: planning deferred, claudio "
                    f"planner unreachable (retry {n}/{settings.max_replans}): "
                    f"{errors[-1] if errors else 'unknown'}")
                repo_events.append_event(
                    conn, "workflow", workflow_id, "warn",
                    f"planning deferred: endpoint unreachable "
                    f"(retry {n}/{settings.max_replans})")
            return "planning"
        with immediate(conn):
            repo_workflows.set_status(conn, workflow_id, "blocked")
            repo_alerts.raise_alert(conn, "plan", "error",
                                    f"workflow {workflow_id} unplanable: "
                                    f"{errors[-1] if errors else 'unknown'}")
            repo_events.append_event(conn, "workflow", workflow_id, "error",
                                     "planning failed after repair retry")
            from ..github import projection
            projection.project_workflow_state(conn, workflow_id, repo,
                                              "blocked")
        return "blocked"

    with immediate(conn):
        # replan swap: the fresh plan is reconciled into the existing task set
        # rather than replacing it. Deleting the old rows minted a new task_id
        # per replan, which made the gh_subissue outbox key look unseen and
        # duplicated GitHub sub-issues for work that already had one.
        # reconcile keeps task identity and assigns the sequence_index ladder
        # itself, so UNIQUE(workflow_id, sequence_index) / UNIQUE(workflow_id,
        # title) still hold and the workflow cannot stick in 'planning'.
        planned = [{**pt.model_dump(),
                    "brief": build_brief(pt, repo, base_ref, settings),
                    "max_attempts": settings.max_attempts}
                   for pt in plan.tasks]
        # NOTE: tasks must NOT inherit the workflow's gh_issue_number —
        # project_tasks_created skips tasks that already carry one, so
        # stamping the parent here silently disabled sub-issue creation.
        # Each task gets its own sub-issue number via the outbox callback.
        reconcile.apply_replanned_plan(conn, workflow_id, planned)
        repo_workflows.set_plan(conn, workflow_id,
                                plan.model_dump(), status="running")
        # a successful plan resolves any earlier deferral/block alert on this
        # probe, so the alert reflects the *current* planning state
        repo_alerts.clear_alert(conn, "plan")
        if n_old:
            repo_events.append_event(
                conn, "workflow", workflow_id, "warn",
                f"replan: replaced {n_old} superseded tasks with "
                f"{len(plan.tasks)}")
        else:
            repo_events.append_event(conn, "workflow", workflow_id, "info",
                                     f"planned {len(plan.tasks)} tasks")
    return "running"
