"""S3 — message contract tests."""
from __future__ import annotations

import json

import pytest

from clams.contracts import (
    AlertEvent,
    CommandEnvelope,
    ContractError,
    ExecutionResult,
    Heartbeat,
    TaskBrief,
    SCHEMA_VERSION,
    validate_json,
)

# Exact payload from spec §4.2
SPEC_TASK_BRIEF = {
    "$schema_version": "1.0",
    "task_issue": 42,
    "requirement_issue": 17,
    "objective": "Implement POST /runs/{id}/messages endpoint with status 202 response",
    "acceptance_criterion": "pytest tests/test_inbox.py exits code 0",
    "scope": {
        "writable": ["src/api/runs.py", "tests/test_inbox.py"],
        "read_only": ["src/core/"],
        "forbidden": ["migrations/"],
    },
    "git": {"base_branch": "main", "base_sha": "4a9a5ec", "work_branch": "task/42"},
    "limits": {"max_turns": 40, "max_wall_seconds": 1800},
}

SPEC_EXEC_RESULT = {
    "$schema_version": "1.0",
    "task_issue": 42,
    "status": "completed",
    "summary": "Implemented endpoint and verified integration tests.",
    "evidence": {
        "commands": [
            {"cmd": "pytest -q tests/test_inbox.py", "exit_code": 0, "tail": "18 passed in 4.2s"}
        ],
        "log_file": "/work/task-42/test-output.log",
    },
    "artifact_delta": {
        "files_changed": ["src/api/runs.py", "tests/test_inbox.py"],
        "commit": "b21c9f0",
        "branch": "task/42",
    },
    "environment_state": {"git_head": "b21c9f0", "working_tree_clean": True},
}


def test_task_brief_parses_spec_payload():
    b = TaskBrief.from_json(json.dumps(SPEC_TASK_BRIEF))
    assert b.task_issue == 42
    assert b.requirement_issue == 17
    assert b.scope.writable == ["src/api/runs.py", "tests/test_inbox.py"]
    assert b.git.base_sha == "4a9a5ec"
    assert b.limits.max_turns == 40
    assert b.schema_version == SCHEMA_VERSION


def test_task_brief_roundtrip_stable():
    b = TaskBrief.from_json(json.dumps(SPEC_TASK_BRIEF))
    again = TaskBrief.from_json(b.to_json())
    assert again.model_dump() == b.model_dump()


def test_task_brief_roundtrip_preserves_alias():
    b = TaskBrief.from_json(json.dumps(SPEC_TASK_BRIEF))
    dumped = json.loads(b.to_json())
    assert "$schema_version" in dumped
    assert dumped["$schema_version"] == "1.0"


def test_task_brief_rejects_missing_field():
    bad = dict(SPEC_TASK_BRIEF)
    del bad["objective"]
    with pytest.raises(ContractError):
        TaskBrief.from_json(json.dumps(bad))


def test_task_brief_rejects_extra_field():
    bad = dict(SPEC_TASK_BRIEF)
    bad["surprise"] = 1
    with pytest.raises(ContractError):
        TaskBrief.from_json(json.dumps(bad))


def test_task_brief_rejects_bad_issue_number():
    bad = dict(SPEC_TASK_BRIEF)
    bad["task_issue"] = 0
    with pytest.raises(ContractError):
        TaskBrief.from_json(json.dumps(bad))


def test_execution_result_parses_spec_payload():
    r = ExecutionResult.from_json(json.dumps(SPEC_EXEC_RESULT))
    assert r.status == "completed"
    assert r.evidence.commands[0].exit_code == 0
    assert r.artifact_delta.commit == "b21c9f0"
    assert r.environment_state.working_tree_clean is True


def test_execution_result_roundtrip_stable():
    r = ExecutionResult.from_json(json.dumps(SPEC_EXEC_RESULT))
    assert ExecutionResult.from_json(r.to_json()).model_dump() == r.model_dump()


def test_execution_result_rejects_unknown_status():
    bad = dict(SPEC_EXEC_RESULT)
    bad["status"] = "maybe"
    with pytest.raises(ContractError):
        ExecutionResult.from_json(json.dumps(bad))


def test_alert_event_roundtrip():
    a = AlertEvent(severity="critical", source="engine-b", message="timeout",
                   timestamp="2026-11-08T10:14:22Z", task_number=42,
                   actions=["Resume", "Force Re-Plan"])
    a2 = AlertEvent.from_json(a.to_json())
    assert a2.severity == "critical"
    assert a2.task_number == 42
    assert a2.actions == ["Resume", "Force Re-Plan"]


def test_alert_rejects_bad_severity():
    with pytest.raises(Exception):
        AlertEvent(severity="fatal", source="s", message="m", timestamp="t")


def test_command_envelope_roundtrip():
    c = CommandEnvelope(action="pause", args={"scope": "all"}, raw_text="pause execution",
                       issued_at="2026-11-08T10:00:00Z")
    c2 = CommandEnvelope.from_json(c.to_json())
    assert c2.action == "pause"
    assert c2.args["scope"] == "all"


def test_heartbeat_roundtrip():
    h = Heartbeat(service="planner", pid=123, timestamp="2026-11-08T10:00:00Z")
    assert Heartbeat.from_json(h.to_json()).service == "planner"


def test_validate_json_dispatch():
    obj = validate_json("task_brief", SPEC_TASK_BRIEF)
    assert isinstance(obj, TaskBrief)
    with pytest.raises(ContractError):
        validate_json("nope", {})
    with pytest.raises(ContractError):
        validate_json("task_brief", {"bogus": True})
