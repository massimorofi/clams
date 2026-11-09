"""Inter-agent message contracts (spec §4.2).

Every payload crossing the Redis bus is validated with pydantic on publish *and*
on consume. A payload that does not validate is never trusted by the receiving
agent; it is dead-lettered instead.
"""
from __future__ import annotations

import json
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = "1.0"


class ContractError(ValueError):
    """Raised when a payload does not match its contract."""


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    writable: list[str] = Field(default_factory=list)
    read_only: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)


class GitRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_branch: str = "main"
    base_sha: str = ""
    work_branch: str = ""


class TaskLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_turns: int = 40
    max_wall_seconds: int = 1800

    @field_validator("max_turns", "max_wall_seconds")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("must be >= 1")
        return v


class TaskBrief(BaseModel):
    """Planner -> Executor."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: str = Field(default=SCHEMA_VERSION, alias="$schema_version")
    task_issue: int
    requirement_issue: int
    objective: str
    acceptance_criterion: str
    scope: Scope = Field(default_factory=Scope)
    git: GitRef = Field(default_factory=GitRef)
    limits: TaskLimits = Field(default_factory=TaskLimits)

    @field_validator("task_issue", "requirement_issue")
    @classmethod
    def _positive_int(cls, v: int) -> int:
        if v < 1:
            raise ValueError("issue numbers must be >= 1")
        return v

    def to_json(self) -> str:
        return self.model_dump_json(by_alias=True)

    @classmethod
    def from_json(cls, raw: str | bytes) -> "TaskBrief":
        return validate_task_brief(json.loads(raw))


class CommandEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cmd: str
    exit_code: int
    tail: str = ""


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commands: list[CommandEvidence] = Field(default_factory=list)
    log_file: str = ""


class ArtifactDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files_changed: list[str] = Field(default_factory=list)
    commit: str = ""
    branch: str = ""


class EnvironmentState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    git_head: str = ""
    working_tree_clean: bool = False


class ExecutionResult(BaseModel):
    """Executor -> Planner."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: str = Field(default=SCHEMA_VERSION, alias="$schema_version")
    task_issue: int
    status: Literal["completed", "failed", "timeout", "blocked"]
    summary: str = ""
    evidence: Evidence = Field(default_factory=Evidence)
    artifact_delta: ArtifactDelta = Field(default_factory=ArtifactDelta)
    environment_state: EnvironmentState = Field(default_factory=EnvironmentState)

    def to_json(self) -> str:
        return self.model_dump_json(by_alias=True)

    @classmethod
    def from_json(cls, raw: str | bytes) -> "ExecutionResult":
        return validate_execution_result(json.loads(raw))


class AlertEvent(BaseModel):
    """Watchdog -> Planner / UI."""

    model_config = ConfigDict(extra="forbid")

    severity: Literal["info", "warning", "critical"]
    source: str
    message: str
    timestamp: str
    requirement_number: Optional[int] = None
    task_number: Optional[int] = None
    actions: list[str] = Field(default_factory=list)

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, raw: str | bytes) -> "AlertEvent":
        return AlertEvent.model_validate_json(raw)


class CommandEnvelope(BaseModel):
    """User chat -> orchestrator steering."""

    model_config = ConfigDict(extra="forbid")

    action: str
    args: dict[str, Any] = Field(default_factory=dict)
    raw_text: str = ""
    issued_at: str = ""

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, raw: str | bytes) -> "CommandEnvelope":
        return CommandEnvelope.model_validate_json(raw)


class Heartbeat(BaseModel):
    """Long-running service -> watchdog."""

    model_config = ConfigDict(extra="forbid")

    service: str
    pid: int = 0
    timestamp: str
    detail: str = ""

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, raw: str | bytes) -> "Heartbeat":
        return Heartbeat.model_validate_json(raw)


# ---- explicit validators (clear errors, stable surface) ----------------

def validate_task_brief(payload: Any) -> TaskBrief:
    try:
        return TaskBrief.model_validate(payload)
    except Exception as exc:  # pydantic ValidationError
        raise ContractError(f"invalid TaskBrief: {exc}") from exc


def validate_execution_result(payload: Any) -> ExecutionResult:
    try:
        return ExecutionResult.model_validate(payload)
    except Exception as exc:
        raise ContractError(f"invalid ExecutionResult: {exc}") from exc


def validate_alert(payload: Any) -> AlertEvent:
    try:
        return AlertEvent.model_validate(payload)
    except Exception as exc:
        raise ContractError(f"invalid AlertEvent: {exc}") from exc


def validate_command(payload: Any) -> CommandEnvelope:
    try:
        return CommandEnvelope.model_validate(payload)
    except Exception as exc:
        raise ContractError(f"invalid CommandEnvelope: {exc}") from exc


VALIDATORS = {
    "task_brief": validate_task_brief,
    "execution_result": validate_execution_result,
    "alert": validate_alert,
    "command": validate_command,
    "heartbeat": Heartbeat.model_validate,
}


def validate_json(schema_name: str, payload: Any) -> BaseModel:
    try:
        validator = VALIDATORS[schema_name]
    except KeyError:
        raise ContractError(f"unknown schema {schema_name!r}")
    return validator(payload)
