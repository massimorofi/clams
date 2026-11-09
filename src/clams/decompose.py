"""Requirement decomposition (spec FR-3, §8.2).

The Planner turns one requirement into 3–7 atomic sub-issues. The LLM output is
treated as untrusted: it must parse as strict JSON and pass validation before any
GitHub write happens.

Anti-pollution rule (spec §8.2): the prompt is built from the requirement title
and body **only**. Comment threads — especially ones authored by this same agent —
are never fed back in.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .contracts import Scope
from .db import Store
from .gh import GitHubClient, STATUS_READY, parent_label

MIN_TASKS = 3
MAX_TASKS = 7


class DecompositionError(Exception):
    """Raised when the planner output cannot be turned into a valid plan."""


class SubTaskSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    objective: str
    acceptance_criterion: str
    writable: list[str] = Field(default_factory=list)
    read_only: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)

    @field_validator("title", "objective", "acceptance_criterion")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must be a non-empty string")
        return v.strip()

    def to_scope(self) -> Scope:
        return Scope(writable=list(self.writable), read_only=list(self.read_only),
                    forbidden=list(self.forbidden))


class Plan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tasks: list[SubTaskSpec]

    def __len__(self) -> int:
        return len(self.tasks)

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, raw: str) -> "Plan":
        return Plan.model_validate_json(raw)


PLAN_PROMPT = """You are the planning agent of an autonomous multi-agent system.

Decompose the requirement below into 3 to 7 ATOMIC implementation tasks.
Each task must be independently executable by a worker that can only see that
task's brief, and independently verifiable.

Return ONLY a JSON object, no prose, no markdown fences, shaped exactly like:
{{
  "tasks": [
    {{
      "title": "short imperative title",
      "objective": "exactly what the worker must change and why",
      "acceptance_criterion": "a concrete, machine-checkable condition",
      "writable": ["paths the worker may modify"],
      "read_only": ["paths the worker may read but not modify"],
      "forbidden": ["paths the worker must not touch"]
    }}
  ]
}}

REQUIREMENT TITLE: {title}

REQUIREMENT BODY:
{body}
"""


def build_prompt(title: str, body: str = "") -> str:
    """Build the planner prompt from the requirement only (never from comments)."""
    return PLAN_PROMPT.format(title=title or "", body=(body or "").strip() or "(none)")


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of an LLM response.

    Handles bare JSON, fenced ```json blocks, and JSON wrapped in prose.
    """
    if text is None:
        raise DecompositionError("empty planner response")
    raw = text.strip()
    # strip common fences
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1).strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    m = _JSON_BLOCK.search(raw)
    if not m:
        raise DecompositionError("no JSON object found in planner response")
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        raise DecompositionError(f"planner JSON is malformed: {exc}") from exc
    if not isinstance(obj, dict):
        raise DecompositionError("planner response is not a JSON object")
    return obj


def parse_plan(payload: Any) -> Plan:
    if isinstance(payload, str):
        payload = extract_json(payload)
    if not isinstance(payload, dict):
        raise DecompositionError("plan payload must be an object")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        raise DecompositionError("plan payload is missing a 'tasks' array")
    specs: list[SubTaskSpec] = []
    for i, item in enumerate(tasks):
        if not isinstance(item, dict):
            raise DecompositionError(f"task #{i} is not an object")
        try:
            specs.append(SubTaskSpec.model_validate(item))
        except Exception as exc:  # pydantic
            raise DecompositionError(f"task #{i} invalid: {exc}") from exc
    return Plan(tasks=specs)


def validate_plan(plan: Plan) -> Plan:
    n = len(plan.tasks)
    if n < MIN_TASKS:
        raise DecompositionError(
            f"plan has {n} task(s); the spec requires at least {MIN_TASKS}"
        )
    if n > MAX_TASKS:
        raise DecompositionError(
            f"plan has {n} task(s); the spec allows at most {MAX_TASKS}"
        )
    seen: set[str] = set()
    for i, t in enumerate(plan.tasks):
        if t.title in seen:
            raise DecompositionError(f"duplicate task title at index {i}: {t.title!r}")
        seen.add(t.title)
    return plan


def generate_plan(llm_call, title: str, body: str = "", retries: int = 1) -> Plan:
    """Call the LLM and return a validated plan, retrying on invalid output.

    ``llm_call`` is a callable taking the prompt string and returning text.
    """
    prompt = build_prompt(title, body)
    last_error: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            text = llm_call(prompt)
            plan = parse_plan(text if isinstance(text, str) else json.dumps(text))
            return validate_plan(plan)
        except DecompositionError as exc:
            last_error = exc
    raise DecompositionError(
        f"planner produced an invalid plan after {retries + 1} attempt(s): {last_error}"
    )


async def create_sub_issues(
    gh: GitHubClient,
    requirement_number: int,
    plan: Plan,
    base_branch: str = "main",
    base_sha: str = "",
) -> list[int]:
    """Create the child issues on GitHub and link them to the requirement."""
    created: list[int] = []
    for idx, spec in enumerate(plan.tasks, start=1):
        body = _child_body(requirement_number, idx, spec)
        issue = await gh.create_issue(
            title=spec.title,
            body=body,
            labels=[STATUS_READY],
        )
        await gh.add_sub_issue(requirement_number, issue.number)
        created.append(issue.number)
    return created


def _child_body(requirement_number: int, idx: int, spec: SubTaskSpec) -> str:
    return (
        f"Parent: #{requirement_number}\n\n"
        f"### Objective\n{spec.objective}\n\n"
        f"### Acceptance criterion\n{spec.acceptance_criterion}\n\n"
        f"### Scope\n"
        f"- Writable: {', '.join(spec.writable) or '(unspecified)'}\n"
        f"- Read-only: {', '.join(spec.read_only) or '(unspecified)'}\n"
        f"- Forbidden: {', '.join(spec.forbidden) or '(unspecified)'}\n"
    )


def plan_to_dict(plan: Plan) -> dict[str, Any]:
    return {"tasks": [t.model_dump() for t in plan.tasks]}
