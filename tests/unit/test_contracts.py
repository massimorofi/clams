"""WP1: message + plan contracts."""
from __future__ import annotations

import json

import pytest

from clams import contracts as c


def _brief(**kw):
    base = dict(objective="obj", acceptance="acc", run=["pytest -q"],
                writable=["src/**"], repo="o/r")
    base.update(kw)
    return c.Brief(**base)


def test_dispatch_roundtrip():
    m = c.TaskDispatch(task_id="t1", workflow_id="w1", attempt=1,
                       lease_token="L", brief=_brief())
    raw = c.dump_message(m)
    parsed = c.parse_message(raw)
    assert isinstance(parsed, c.TaskDispatch)
    assert parsed.brief.run == ["pytest -q"]
    assert parsed.schema_version == 1


def test_unknown_type_rejected():
    with pytest.raises(c.ContractError):
        c.parse_message({"type": "task.teleport"})


def test_unknown_schema_version_rejected():
    with pytest.raises(c.ContractError):
        c.parse_message({"type": "task.cancel", "schema_version": 99,
                         "msg_id": "m", "ts": "t", "task_id": "t",
                         "attempt": 1, "lease_token": "L"})


def test_extra_fields_rejected():
    with pytest.raises(c.ContractError):
        c.parse_message({"type": "task.heartbeat", "msg_id": "m", "ts": "t",
                         "task_id": "t", "attempt": 1, "lease_token": "L",
                         "surprise": 1})


def test_payload_cap_enforced():
    big = _brief(objective="x" * (c.MAX_PAYLOAD_BYTES + 10))
    m = c.TaskDispatch(task_id="t", workflow_id="w", attempt=1,
                       lease_token="L", brief=big)
    with pytest.raises(c.ContractError):
        c.dump_message(m)


def test_result_outcomes():
    r = c.TaskResult(task_id="t", attempt=2, lease_token="L",
                     outcome="needs_input", session_ref="s")
    assert c.parse_message(r.model_dump_json()).outcome == "needs_input"
    with pytest.raises(Exception):
        c.TaskResult(task_id="t", attempt=0, lease_token="L", outcome="success")


def test_brief_requires_run_and_writable():
    with pytest.raises(Exception):
        c.Brief(objective="o", acceptance="a", run=[], writable=["x"], repo="o/r")
    with pytest.raises(Exception):
        c.Brief(objective="o", acceptance="a", run=["x"], writable=[], repo="o/r")


# ------------------------------------------------------------------- plan
def _ptitle(i):
    return f"task {i}"


def _plan(n=3, deps=None):
    deps = deps or {}
    return c.Plan(tasks=[
        c.PlanTask(title=_ptitle(i), objective="o", acceptance="a",
                   run=["pytest"], writable=["src/**"],
                   depends_on=deps.get(i, []))
        for i in range(1, n + 1)
    ])


def test_plan_valid():
    c.validate_plan(_plan())


def test_plan_empty_or_too_big():
    with pytest.raises(c.PlanValidationError):
        c.validate_plan(c.Plan(tasks=[]))
    with pytest.raises(c.PlanValidationError):
        c.validate_plan(_plan(8))


def test_plan_duplicate_titles():
    p = _plan(2)
    p.tasks[1].title = p.tasks[0].title
    with pytest.raises(c.PlanValidationError):
        c.validate_plan(p)


def test_plan_forward_dependency_rejected():
    p = _plan(3, deps={1: [2]})
    with pytest.raises(c.PlanValidationError):
        c.validate_plan(p)


def test_plan_self_dependency_rejected():
    p = _plan(2, deps={2: [2]})
    with pytest.raises(c.PlanValidationError):
        c.validate_plan(p)


def test_plan_needs_run_and_writable():
    p = _plan(1)
    p.tasks[0].run = ["  "]
    with pytest.raises(c.PlanValidationError):
        c.validate_plan(p)


def test_plan_parses_from_llm_json():
    raw = json.dumps({"tasks": [{
        "title": "t", "objective": "o", "acceptance": "a",
        "run": ["make test"], "writable": ["app/**"], "depends_on": []}]})
    plan = c.Plan.model_validate(json.loads(raw))
    c.validate_plan(plan)
