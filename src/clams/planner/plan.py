"""Planning: Claudio-P decomposes a workflow goal into a validated DAG (§8).

Budget: at most ``planning_llm_budget_per_cycle`` LLM calls per planner cycle
(the loop enforces it; this module exposes the call itself).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from .. import contracts
from ..contracts import Plan, PlanTask, PlanValidationError, validate_plan
from ..db import (repo_alerts, repo_events, repo_tasks, repo_workflows)
from ..db.conn import immediate

log = logging.getLogger("clams.plan")

PLAN_PROMPT = """You are the planning agent of Clams. Decompose the goal below \
into 1-{max_tasks} sequential tasks for autonomous execution in a git worktree.

Return ONLY a JSON object, no prose, matching:
{{"tasks": [{{"title": str, "objective": str, "acceptance": str,
"run": [str, ...], "writable": [glob, ...], "forbidden": [glob, ...],
"depends_on": [sequence_index, ...]}}]}}

Rules: tasks are ordered (sequence_index = position, 1-based); depends_on may \
only reference earlier tasks; every task needs >=1 run command (shell, must \
print output and exit 0 to pass) and >=1 writable glob; acceptance must be \
objectively checkable.
Keep each task SMALL: one focused change an agent can finish well under one \
hour of tool work — split large features into several tasks rather than one. \
Run commands execute in the executor container which has python3, pytest, \
git and grep; they must be non-interactive and finish within 5 minutes.

Goal (repo {repo}, base ref {base_ref}):
{goal}
"""

REPAIR_SUFFIX = """

IMPORTANT: your previous attempt was invalid: {error}
Fix exactly that and return the corrected JSON object only — no prose.
"""

REPLAN_SUFFIX = """

This is a REPLAN after a previous attempt failed. Prior task outcomes:
{prior}
Do not redo work already marked success; focus the new plan on what failed.
"""


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
        lines = []
        for t in prior:
            detail = (t["error_log"] or "")[:200]
            if not detail and t["result_json"]:
                try:
                    detail = str(json.loads(t["result_json"])
                                 .get("log_tail", ""))[:200]
                except Exception:  # noqa: BLE001
                    detail = ""
            lines.append(f"- [{t['status']}] {t['title']}"
                         + (f": {detail}" if detail else ""))
        prior_ctx = REPLAN_SUFFIX.format(prior="\n".join(lines))
    base_prompt = (PLAN_PROMPT.format(max_tasks=settings.max_plan_tasks,
                                      repo=repo, base_ref=base_ref,
                                      goal=goal)
                   + prior_ctx)
    prompt = base_prompt
    errors: list[str] = []
    plan: Optional[Plan] = None
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
            errors.append(str(exc)[:300])
            plan = None
            # keep the full original prompt (goal + schema) — a bare error
            # message gives the model nothing to repair against
            prompt = base_prompt + REPAIR_SUFFIX.format(error=errors[-1])
    if plan is None:
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
        # replan swap: the fresh plan supersedes the old task set. Without
        # this the inserts collide with UNIQUE(workflow_id, sequence_index)
        # / UNIQUE(workflow_id, title) and the workflow sticks in 'planning'.
        n_old = repo_tasks.delete_workflow_tasks(conn, workflow_id)
        for i, pt in enumerate(plan.tasks, start=1):
            repo_tasks.create_task(
                conn, workflow_id, i, pt.title, pt.objective, pt.acceptance,
                build_brief(pt, repo, base_ref, settings),
                depends_on=pt.depends_on,
                max_attempts=settings.max_attempts)
            # NOTE: tasks must NOT inherit the workflow's gh_issue_number —
            # project_tasks_created skips tasks that already carry one, so
            # stamping the parent here silently disabled sub-issue creation.
            # Each task gets its own sub-issue number via the outbox callback.
        repo_workflows.set_plan(conn, workflow_id,
                                plan.model_dump(), status="running")
        if n_old:
            repo_events.append_event(
                conn, "workflow", workflow_id, "warn",
                f"replan: replaced {n_old} superseded tasks with "
                f"{len(plan.tasks)}")
        else:
            repo_events.append_event(conn, "workflow", workflow_id, "info",
                                     f"planned {len(plan.tasks)} tasks")
    return "running"
