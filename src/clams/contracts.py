"""Clams v2 message + plan + brief contracts (pydantic, schema_version=1).

Two streams only:
  clams:tasks   (Planner -> Executors): task.dispatch, task.cancel
  clams:events  (Executors -> Planner): task.started, task.heartbeat,
                task.question, task.result, executor.alive

Every message: ``{schema_version:1, msg_id:uuid, type, ts, ...}``.
Payload <= 16 KB (enforced by ``envelope_size_ok``).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = 1
MAX_PAYLOAD_BYTES = 16 * 1024

TaskOutcome = Literal["success", "failed", "needs_input"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_msg_id() -> str:
    return str(uuid.uuid4())


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    msg_id: str = Field(default_factory=new_msg_id)
    ts: str = Field(default_factory=_now_iso)

    @field_validator("schema_version")
    @classmethod
    def _known_version(cls, v: int) -> int:
        if v != SCHEMA_VERSION:
            raise ValueError(f"unknown schema_version {v}")
        return v


# --------------------------------------------------------------------------- brief
class Brief(BaseModel):
    """Immutable per-attempt task brief carried inside task.dispatch.

    extra="ignore": stored brief_json rows may predate contract changes
    (e.g. the removed max_wall_s) — an old row must never wedge dispatch.
    Migrations still strip known-obsolete keys.
    """
    model_config = ConfigDict(extra="ignore")

    schema_version: int = SCHEMA_VERSION
    objective: str
    acceptance: str
    run: list[str] = Field(min_length=1)          # verify commands, all must pass
    writable: list[str] = Field(min_length=1)     # globs the agent may touch
    forbidden: list[str] = Field(default_factory=list)
    repo: str                                     # owner/name
    base_ref: str = "main"
    max_turns: int = 40
    answer: Optional[str] = None                  # set when resuming after question
    session_ref: Optional[str] = None             # set when resuming a session
    # operator-edited executor prompt template (clams.prompts), injected by
    # the planner at dispatch time so the bus-only executor needs no DB read
    prompt_template: Optional[str] = None
    # compact summary of why the previous attempt failed the gate, injected
    # at dispatch time for attempt > 1 (retry work reuse)
    prior_failure: Optional[str] = None

    @field_validator("schema_version")
    @classmethod
    def _known_version(cls, v: int) -> int:
        if v != SCHEMA_VERSION:
            raise ValueError(f"unknown brief schema_version {v}")
        return v


# ------------------------------------------------------------------ clams:tasks
class TaskDispatch(_Base):
    type: Literal["task.dispatch"] = "task.dispatch"
    task_id: str
    workflow_id: str
    attempt: int = Field(ge=1)
    lease_token: str
    brief: Brief


class TaskCancel(_Base):
    type: Literal["task.cancel"] = "task.cancel"
    task_id: str
    attempt: int = Field(ge=1)
    lease_token: str


def integration_branch(workflow_id: str) -> str:
    """The per-workflow ref the executor advances as each task succeeds.

    Single spelling on purpose: the planner names it when asking for the merge
    and the executor names it when seeding a retry, and if those ever drift the
    merge silently becomes a no-op."""
    return f"clams/wf/{workflow_id}"


class WorkflowIntegrate(_Base):
    """Planner -> executor, when a workflow reaches done: merge the workflow's
    integration ref into the project base branch and push it.

    The planner has no git access at all (no /work volume, no credentials), so
    it can only ask the executor to do it. See executor.worktree.merge_to_base.
    """
    type: Literal["workflow.integrate"] = "workflow.integrate"
    workflow_id: str
    repo: str
    integration_branch: str
    base_ref: str = "main"


# ----------------------------------------------------------------- clams:events
class TaskStarted(_Base):
    type: Literal["task.started"] = "task.started"
    task_id: str
    attempt: int = Field(ge=1)
    lease_token: str
    executor_id: str
    session_ref: str


class TaskHeartbeat(_Base):
    type: Literal["task.heartbeat"] = "task.heartbeat"
    task_id: str
    attempt: int = Field(ge=1)
    lease_token: str
    turns_used: int = 0
    elapsed_s: int = 0


class TaskQuestion(_Base):
    type: Literal["task.question"] = "task.question"
    task_id: str
    attempt: int = Field(ge=1)
    lease_token: str
    question: str
    options: list[str] = Field(default_factory=list)
    idempotency_key: str


class Evidence(BaseModel):
    model_config = ConfigDict(extra="allow")
    checks: list[dict[str, Any]] = Field(default_factory=list)
    passed: bool = False
    reason: str = ""


class TaskResult(_Base):
    type: Literal["task.result"] = "task.result"
    task_id: str
    attempt: int = Field(ge=1)
    lease_token: str
    outcome: TaskOutcome
    evidence: dict[str, Any] = Field(default_factory=dict)
    commit_sha: str = ""
    branch: str = ""
    changed_files: list[str] = Field(default_factory=list)
    log_tail: str = ""
    session_ref: str = ""
    # structured failure class (see executor.runner ERROR_* codes): lets the
    # planner distinguish retryable failures from config-class ones that
    # would burn every attempt identically (e.g. claudio:auth)
    error_code: str = ""


class WorkflowIntegrated(_Base):
    """Executor -> planner: the outcome of a ``workflow.integrate`` request.

    The executor has no DB access, so an unsuccessful merge (a conflict, or a
    protected base branch rejecting the push) has to travel back this way to
    become an alert instead of dying in a runner log line.
    """
    type: Literal["workflow.integrated"] = "workflow.integrated"
    workflow_id: str
    base_ref: str = ""
    ok: bool = False
    detail: str = ""


class ExecutorAlive(_Base):
    type: Literal["executor.alive"] = "executor.alive"
    executor_id: str
    capacity: int = 1
    version: str = ""


Message = Union[
    TaskDispatch, TaskCancel, WorkflowIntegrate,
    TaskStarted, TaskHeartbeat, TaskQuestion, TaskResult, ExecutorAlive,
    WorkflowIntegrated,
]

_MESSAGE_TYPES: dict[str, type[BaseModel]] = {
    "task.dispatch": TaskDispatch,
    "task.cancel": TaskCancel,
    "workflow.integrate": WorkflowIntegrate,
    "task.started": TaskStarted,
    "task.heartbeat": TaskHeartbeat,
    "task.question": TaskQuestion,
    "task.result": TaskResult,
    "executor.alive": ExecutorAlive,
    "workflow.integrated": WorkflowIntegrated,
}

TASK_STREAM_TYPES = {"task.dispatch", "task.cancel", "workflow.integrate"}
EVENT_STREAM_TYPES = set(_MESSAGE_TYPES) - TASK_STREAM_TYPES


class ContractError(Exception):
    pass


def parse_message(raw: Union[str, bytes, dict]) -> Message:
    """Parse + validate one bus message. Raises ContractError on anything invalid."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ContractError(f"not json: {exc}") from exc
    if not isinstance(raw, dict):
        raise ContractError("message must be an object")
    mtype = raw.get("type")
    model = _MESSAGE_TYPES.get(mtype)
    if model is None:
        raise ContractError(f"unknown message type {mtype!r}")
    try:
        return model.model_validate(raw)  # type: ignore[return-value]
    except Exception as exc:
        raise ContractError(f"invalid {mtype}: {exc}") from exc


def envelope_size_ok(msg: _Base) -> bool:
    return len(msg.model_dump_json().encode("utf-8")) <= MAX_PAYLOAD_BYTES


def dump_message(msg: _Base) -> str:
    if not envelope_size_ok(msg):
        raise ContractError(
            f"message {msg.type} exceeds {MAX_PAYLOAD_BYTES} bytes payload cap")
    return msg.model_dump_json()


# ------------------------------------------------------------------- plan schema
class PlanTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    acceptance: str = Field(min_length=1)
    run: list[str] = Field(min_length=1)
    writable: list[str] = Field(min_length=1)
    forbidden: list[str] = Field(default_factory=list)
    depends_on: list[int] = Field(default_factory=list)  # sequence_index values


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tasks: list[PlanTask]


class PlanValidationError(Exception):
    pass


def validate_plan(plan: Plan, max_tasks: int = 7) -> None:
    """Raise PlanValidationError unless the plan is structurally sound.

    Rules (§8): 1..max_tasks tasks, unique titles, contiguous sequence_index
    (implicit by list order, 1-based), depends_on acyclic and referencing
    earlier tasks only, every task has >=1 run command and writable globs.
    """
    if not (1 <= len(plan.tasks) <= max_tasks):
        raise PlanValidationError(
            f"plan must have 1..{max_tasks} tasks, got {len(plan.tasks)}")
    titles = [t.title for t in plan.tasks]
    if len(set(titles)) != len(titles):
        raise PlanValidationError("task titles must be unique")
    n = len(plan.tasks)
    for i, t in enumerate(plan.tasks, start=1):
        for dep in t.depends_on:
            if dep >= i:
                raise PlanValidationError(
                    f"task {i} ({t.title!r}) depends on {dep}: depends_on must "
                    "reference earlier tasks only (acyclic by construction)")
            if dep < 1:
                raise PlanValidationError(f"task {i} has invalid depends_on {dep}")
        if not t.run or not all(r.strip() for r in t.run):
            raise PlanValidationError(f"task {i} needs >=1 non-empty run command")
        if not t.writable or not all(g.strip() for g in t.writable):
            raise PlanValidationError(f"task {i} needs >=1 writable glob")
    # explicit cycle guard (defence in depth; earlier-only refs already forbid cycles)
    _assert_acyclic(plan)


def _assert_acyclic(plan: Plan) -> None:
    deps = {i + 1: set(t.depends_on) for i, t in enumerate(plan.tasks)}
    seen: set[int] = set()
    stack: set[int] = set()

    def visit(node: int) -> None:
        if node in stack:
            raise PlanValidationError(f"dependency cycle at task {node}")
        if node in seen:
            return
        stack.add(node)
        for d in deps.get(node, set()):
            visit(d)
        stack.discard(node)
        seen.add(node)

    for node in deps:
        visit(node)
