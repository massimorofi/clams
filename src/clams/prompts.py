"""Editable prompt templates (planner + executor) with built-in defaults.

The defaults live here; operator edits live in ``runtime_state`` under
``prompt_overrides`` (a JSON ``{name: template}`` map) and are applied only
through the ``prompt_patch`` command, keeping the single-writer rule (the
planner drains commands; the API only inserts them).

Templates are ``str.format`` templates. Placeholders are fixed per prompt and
validated on save (see :func:`validate`); a template that fails to render at
run time falls back to the built-in default so a bad edit can never wedge
planning or execution.

The executor process has no DB access (bus-only), so the planner injects the
effective executor template into the dispatch brief
(``Brief.prompt_template``) at dispatch time; the executor renders it with
:func:`render_template`.
"""
from __future__ import annotations

import json
import logging
import string
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("clams.prompts")

RUNTIME_KEY = "prompt_overrides"


@dataclass(frozen=True)
class PromptSpec:
    name: str
    label: str
    description: str
    placeholders: tuple[str, ...]
    default: str
    sample: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Built-in defaults
# --------------------------------------------------------------------------

PLAN_DEFAULT = """You are the planning agent of Clams. Decompose the goal below \
into 1-{max_tasks} sequential tasks for autonomous execution in a git worktree.

Return ONLY a JSON object, no prose, matching:
{{"tasks": [{{"title": str, "objective": str, "acceptance": str,
"run": [str, ...], "writable": [glob, ...], "forbidden": [glob, ...],
"depends_on": [sequence_index, ...]}}]}}

Rules: tasks are ordered (sequence_index = position, 1-based); depends_on may \
only reference earlier tasks; every task needs >=1 run command (shell, must \
exit 0 to pass) and >=1 writable glob; acceptance must be \
objectively checkable. Every `run` command must PRINT a marker when it \
succeeds — append `&& echo NAME_OK` — and must never use `-q`/`--quiet`, \
`>/dev/null`, or a trailing `|| true`: a silent command gives the gate \
nothing to verify, and `|| true` hides its failure entirely. `writable` must cover EVERY path the work or its \
verification will create or modify — including result artifacts the task \
must commit (reports, JSON outputs); if an artifact is writable-forbidden \
the clean-tree/scope gate can never pass. Prefer `run` commands scoped to \
the task's own test files over whole-suite commands: a suite-wide gate \
fails on unrelated files' import errors and no agent fix can help that.
Keep each task SMALL: one focused change an agent can finish well under one \
hour of tool work — split large features into several tasks rather than one. \
Run commands execute in a bare gate container that has python3, pytest, git \
and grep but does NOT have the repo's own packages installed: a `run:` pytest \
command must make imports resolvable itself (prefix `PYTHONPATH=<pkg>/src:...` \
or have the test bootstrap sys.path), otherwise it dies with ModuleNotFoundError \
at the gate even when the code is correct. Commands must be non-interactive and \
finish within 5 minutes.

Goal (repo {repo}, base ref {base_ref}):
{goal}
"""

REPAIR_DEFAULT = """

IMPORTANT: your previous attempt was invalid: {error}
Fix exactly that and return the corrected JSON object only — no prose.
"""

REPLAN_DEFAULT = """

This is a REPLAN after a previous attempt failed. Prior task outcomes:
{prior}
Do not redo work already marked success; focus the new plan on what failed.
"""

EXECUTOR_DEFAULT = """Objective: {objective}
Acceptance criteria: {acceptance}

{prior_failure_block}You work in the current directory (a git worktree). Commit all changes.
Writable paths: {writable}
{forbidden_line}Verification commands that MUST pass: {run}
Max turns: {max_turns}.
{answer_block}
If you cannot proceed without a human decision, reply with a line
'NEEDS_INPUT: <question>' (optionally followed by 'OPTIONS: a | b').
Otherwise do the work and finish with a short summary."""

PROMPTS: dict[str, PromptSpec] = {
    "plan": PromptSpec(
        name="plan",
        label="Planner — task decomposition",
        description="Sent to the planning agent to turn a workflow goal into "
                    "a JSON task plan.",
        placeholders=("max_tasks", "repo", "base_ref", "goal"),
        default=PLAN_DEFAULT,
        sample={"max_tasks": 8, "repo": "owner/name", "base_ref": "main",
                "goal": "sample goal"},
    ),
    "plan_repair": PromptSpec(
        name="plan_repair",
        label="Planner — repair suffix",
        description="Appended to the plan prompt when the previous reply was "
                    "not valid JSON / failed validation.",
        placeholders=("error",),
        default=REPAIR_DEFAULT,
        sample={"error": "sample error"},
    ),
    "plan_replan": PromptSpec(
        name="plan_replan",
        label="Planner — replan suffix",
        description="Appended to the plan prompt on a replan, carrying prior "
                    "task outcomes.",
        placeholders=("prior",),
        default=REPLAN_DEFAULT,
        sample={"prior": "- [failed] task title"},
    ),
    "executor_task": PromptSpec(
        name="executor_task",
        label="Executor — task prompt",
        description="Sent to the executor agent for each task attempt. "
                    "`forbidden_line` renders the whole 'Forbidden paths' "
                    "line (with trailing newline) or is empty; `answer_block` "
                    "renders the human-answer block or is empty; "
                    "`prior_failure_block` renders the previous-attempt gate-"
                    "failure block on retries or is empty.",
        placeholders=("objective", "acceptance", "prior_failure_block",
                      "writable", "forbidden_line", "run", "max_turns",
                      "answer_block"),
        default=EXECUTOR_DEFAULT,
        sample={"objective": "o", "acceptance": "a",
                "prior_failure_block": "", "writable": "src/**",
                "forbidden_line": "", "run": "pytest -q", "max_turns": 40,
                "answer_block": ""},
    ),
}


# --------------------------------------------------------------------------
# Storage (runtime_state.prompt_overrides, planner-written)
# --------------------------------------------------------------------------

def _overrides_raw(conn) -> dict[str, str]:
    from .db.conn import get_runtime_state
    try:
        data = json.loads(get_runtime_state(conn, RUNTIME_KEY) or "{}")
    except (TypeError, ValueError):
        return {}
    return {k: v for k, v in data.items()
            if k in PROMPTS and isinstance(v, str)}


def get_override(conn, name: str) -> Optional[str]:
    """The operator template for `name`, or None when the built-in default
    is in effect."""
    return _overrides_raw(conn).get(name)


def effective_template(conn, name: str) -> str:
    return get_override(conn, name) or PROMPTS[name].default


def set_override(conn, name: str, template: Optional[str]) -> dict[str, str]:
    """Store (or with None, clear) an override. Returns the new override map.
    Callers must validate first; this is the low-level write used by the
    prompt_patch command."""
    from .db.conn import get_runtime_state, set_runtime_state
    merged = json.loads(get_runtime_state(conn, RUNTIME_KEY) or "{}")
    if template is None or template == "":
        merged.pop(name, None)
    else:
        merged[name] = template
    set_runtime_state(conn, RUNTIME_KEY, json.dumps(merged))
    return {k: v for k, v in merged.items() if k in PROMPTS}


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def render_template(template: str, name: str, **vars: Any) -> str:
    """Format `template`; on any formatting failure fall back to the built-in
    default for `name` (a broken edit must never wedge a run)."""
    try:
        return template.format(**vars)
    except Exception as exc:  # noqa: BLE001 - KeyError/Index/Value/...
        log.warning("prompt %r failed to render (%s); using built-in default",
                    name, exc)
        return PROMPTS[name].default.format(**vars)


def render(conn, name: str, **vars: Any) -> str:
    """Planner-side render: DB override (if any) else default."""
    return render_template(effective_template(conn, name), name, **vars)


# --------------------------------------------------------------------------
# Validation (used by the prompt_patch command)
# --------------------------------------------------------------------------

def _template_fields(template: str) -> set[str]:
    fields = set()
    for _, fname, _, _ in string.Formatter().parse(template):
        if fname is not None:
            # dotted/indexed refs like a.b or a[0] are not supported
            fields.add(fname.split(".")[0].split("[")[0])
    return fields


def validate(name: str, template: Optional[str]) -> Optional[str]:
    """Return an error message, or None when the template is acceptable
    (empty/None means 'reset to default' and is always valid)."""
    if name not in PROMPTS:
        return f"unknown prompt {name!r}"
    if template is None or template == "":
        return None
    spec = PROMPTS[name]
    try:
        used = _template_fields(template)
    except ValueError as exc:
        return f"template is not a valid format string: {exc}"
    unknown = sorted(used - set(spec.placeholders))
    if unknown:
        return (f"unknown placeholder(s) {', '.join(unknown)}; allowed: "
                + ", ".join(spec.placeholders))
    missing = sorted(set(spec.placeholders) - used)
    if missing:
        return (f"missing required placeholder(s): {', '.join(missing)}")
    try:
        template.format(**spec.sample)
    except Exception as exc:  # noqa: BLE001
        return f"template failed trial rendering: {exc}"
    return None


def catalog(conn) -> list[dict[str, Any]]:
    """Everything the UI needs to edit prompts: specs + defaults + overrides."""
    overrides = _overrides_raw(conn)
    out = []
    for name, spec in PROMPTS.items():
        override = overrides.get(name)
        out.append({
            "name": name,
            "label": spec.label,
            "description": spec.description,
            "placeholders": list(spec.placeholders),
            "default": spec.default,
            "override": override,
            "effective": override or spec.default,
        })
    return out
