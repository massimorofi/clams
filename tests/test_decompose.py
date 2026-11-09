"""S5 — decomposition tests."""
from __future__ import annotations

import json

import pytest

from clams.config import Settings
from clams.db import open_store
from clams.decompose import (
    DecompositionError,
    MAX_TASKS,
    MIN_TASKS,
    Plan,
    SubTaskSpec,
    build_prompt,
    create_sub_issues,
    extract_json,
    generate_plan,
    parse_plan,
    plan_to_dict,
    validate_plan,
)
from clams.planner import PlanningError, PlannerService, ReplanLimitError


def task(i, title=None):
    return {
        "title": title or f"Task {i}",
        "objective": f"Do thing {i}",
        "acceptance_criterion": f"test {i} passes",
        "writable": [f"src/{i}.py"],
        "read_only": ["src/core/"],
        "forbidden": ["migrations/"],
    }


def plan_json(n):
    return json.dumps({"tasks": [task(i) for i in range(1, n + 1)]})


# ---- prompt construction ---------------------------------------------
def test_prompt_contains_only_requirement_text():
    p = build_prompt("Add JWT auth", "We need tokens.")
    assert "Add JWT auth" in p
    assert "We need tokens." in p
    assert "3 to 7" in p


def test_prompt_is_built_from_requirement_only():
    # spec 8.2: no comment ingestion — the prompt is exactly title + body, so a
    # requirement with no body contributes nothing beyond the placeholder.
    p = build_prompt("Title", "Body only")
    assert "Title" in p and "Body only" in p
    assert "comment" not in p.lower()


def test_prompt_handles_empty_body():
    p = build_prompt("Title", "")
    assert "(none)" in p


# ---- JSON extraction -------------------------------------------------
def test_extract_bare_json():
    assert extract_json('{"a": 1}')["a"] == 1


def test_extract_fenced_json():
    text = 'Sure!\n```json\n{"tasks": []}\n```\nDone.'
    assert extract_json(text) == {"tasks": []}


def test_extract_json_wrapped_in_prose():
    text = 'Here you go: {"tasks": [{"a":1}]} hope that helps'
    out = extract_json(text)
    assert "tasks" in out


def test_extract_json_missing_raises():
    with pytest.raises(DecompositionError):
        extract_json("no json here")


def test_extract_json_malformed_raises():
    with pytest.raises(DecompositionError):
        extract_json('{"tasks": [,,,]}')


# ---- plan parsing ----------------------------------------------------
def test_parse_plan_valid():
    plan = parse_plan(plan_json(4))
    assert len(plan) == 4
    assert isinstance(plan.tasks[0], SubTaskSpec)
    assert plan.tasks[0].writable == ["src/1.py"]


def test_parse_plan_missing_tasks_array():
    with pytest.raises(DecompositionError):
        parse_plan({"nope": []})


def test_parse_plan_task_not_object():
    with pytest.raises(DecompositionError):
        parse_plan({"tasks": ["just a string"]})


def test_parse_plan_task_missing_field():
    bad = task(1)
    del bad["acceptance_criterion"]
    with pytest.raises(DecompositionError):
        parse_plan({"tasks": [bad] * 3})


def test_parse_plan_rejects_empty_title():
    bad = task(1, title="   ")
    with pytest.raises(DecompositionError):
        parse_plan({"tasks": [bad] * 3})


# ---- plan validation -----------------------------------------------
def test_validate_plan_accepts_3_to_7():
    for n in range(MIN_TASKS, MAX_TASKS + 1):
        plan = validate_plan(parse_plan(plan_json(n)))
        assert len(plan) == n


def test_validate_plan_rejects_too_few():
    with pytest.raises(DecompositionError, match="at least 3"):
        validate_plan(parse_plan(plan_json(2)))


def test_validate_plan_rejects_too_many():
    with pytest.raises(DecompositionError, match="at most 7"):
        validate_plan(parse_plan(plan_json(8)))


def test_validate_plan_rejects_duplicate_titles():
    plan = parse_plan({"tasks": [task(i, title="same") for i in range(1, 5)]})
    with pytest.raises(DecompositionError, match="duplicate"):
        validate_plan(plan)


# ---- generate_plan with retry ---------------------------------------
def test_generate_plan_succeeds_first_try():
    calls = []

    def llm(prompt):
        calls.append(prompt)
        return plan_json(5)

    plan = generate_plan(llm, "T", "B")
    assert len(plan) == 5
    assert len(calls) == 1


def test_generate_plan_retries_then_succeeds():
    seq = iter(["garbage", plan_json(4)])

    def llm(prompt):
        return next(seq)

    plan = generate_plan(llm, "T", "B", retries=1)
    assert len(plan) == 4


def test_generate_plan_fails_after_retries():
    def llm(prompt):
        return "not json at all"

    with pytest.raises(DecompositionError, match="after 2 attempt"):
        generate_plan(llm, "T", "B", retries=1)


def test_generate_plan_rejects_out_of_range_even_after_retry():
    def llm(prompt):
        return plan_json(2)

    with pytest.raises(DecompositionError):
        generate_plan(llm, "T", "B", retries=1)


# ---- create_sub_issues ---------------------------------------------
class RecordingGH:
    def __init__(self):
        self.created = []
        self.linked = []
        self._next = 100

    async def create_issue(self, title, body="", labels=None):
        from clams.gh import Issue
        self._next += 1
        self.created.append({"title": title, "body": body, "labels": labels})
        return Issue(number=self._next, title=title, body=body, labels=labels or [])

    async def add_sub_issue(self, parent, child):
        self.linked.append((parent, child))
        return True


async def test_create_sub_issues_creates_and_links():
    gh = RecordingGH()
    plan = parse_plan(plan_json(4))
    nums = await create_sub_issues(gh, 5, plan)
    assert nums == [101, 102, 103, 104]
    assert gh.linked == [(5, n) for n in nums]
    assert "Parent: #5" in gh.created[0]["body"]
    assert "Acceptance criterion" in gh.created[0]["body"]


def test_plan_to_dict_roundtrip():
    plan = parse_plan(plan_json(3))
    d = plan_to_dict(plan)
    assert len(d["tasks"]) == 3
    assert Plan.from_json(json.dumps(d)).tasks[0].title == plan.tasks[0].title


# ---- PlannerService -----------------------------------------------
@pytest.fixture()
def store(tmp_path):
    s = open_store(tmp_path / "plan.db")
    yield s
    s.db.close()


@pytest.fixture()
def settings():
    return Settings()


class GH(RecordingGH):
    def __init__(self):
        super().__init__()
        self.updated = []

    async def get_issue(self, number):
        from clams.gh import Issue
        return Issue(number=number, title="Req title", body="Req body",
                    labels=["status:In Progress"], updated_at="2026-11-08T00:00:00Z")

    async def update_issue(self, number, **kw):
        self.updated.append((number, kw))
        from clams.gh import Issue
        return Issue(number=number, title=kw.get("title", "Req title"),
                    body=kw.get("body", "Req body"),
                    labels=["status:In Progress"],
                    updated_at="2026-11-08T00:00:00Z")


async def test_plan_requirement_persists_plan_and_tasks(store, settings):
    gh = GH()
    svc = PlannerService(store, gh, settings, llm_call=lambda p: plan_json(4))
    plan, nums = await svc.plan_requirement(5)
    assert len(plan) == 4
    item = store.work_items.get(5)
    assert item.status == "planned"
    assert item.task_numbers == nums
    assert json.loads(item.plan_json)["tasks"][0]["title"] == plan.tasks[0].title


async def test_replan_increments_and_replans(store, settings):
    gh = GH()
    svc = PlannerService(store, gh, settings, llm_call=lambda p: plan_json(3))
    await svc.plan_requirement(7)
    plan, nums = await svc.replan(7, reason="scope changed")
    assert len(plan) == 3
    assert store.work_items.get(7).replans == 1


async def test_replan_respects_max_replans(store, settings):
    gh = GH()
    svc = PlannerService(store, gh, settings, llm_call=lambda p: plan_json(3))
    await svc.plan_requirement(9)
    for expected in range(1, settings.limits.max_replans + 1):
        await svc.replan(9)
        assert store.work_items.get(9).replans == expected
    with pytest.raises(ReplanLimitError):
        await svc.replan(9)


async def test_replan_unknown_requirement_raises(store, settings):
    gh = GH()
    svc = PlannerService(store, gh, settings, llm_call=lambda p: plan_json(3))
    with pytest.raises(PlanningError):
        await svc.replan(404)


async def test_judge_review_pass_and_fail(store, settings):
    gh = GH()
    ok = PlannerService(store, gh, settings, llm_call=lambda p: "PASS")
    bad = PlannerService(store, gh, settings, llm_call=lambda p: "FAIL")
    weird = PlannerService(store, gh, settings, llm_call=lambda p: "maybe?")
    boom = PlannerService(store, gh, settings, llm_call=lambda p: (_ for _ in ()).throw(RuntimeError()))
    assert await ok.judge_review(1, "evidence") is True
    assert await bad.judge_review(1, "evidence") is False
    assert await weird.judge_review(1, "evidence") is False
    assert await boom.judge_review(1, "evidence") is False


async def test_update_acceptance_criteria(store, settings):
    gh = GH()
    svc = PlannerService(store, gh, settings, llm_call=lambda p: plan_json(3))
    await svc.plan_requirement(11)
    await svc.update_acceptance_criteria(11, "All tests green and coverage > 80%")
    item = store.work_items.get(11)
    payload = json.loads(item.plan_json)
    assert payload["criteria_stale"] is True
    assert "coverage > 80%" in payload["acceptance_criteria"]
    assert gh.updated, "expected the GitHub issue body to be updated"
    assert "coverage > 80%" in gh.updated[-1][1]["body"]
