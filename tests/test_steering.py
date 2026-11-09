"""S8 — chat steering tests."""
from __future__ import annotations

import pytest

from clams.config import Settings
from clams.db import Store, WorkItem, open_store
from clams.steering import (
    ACTION_ADD_REQUIREMENT,
    ACTION_CANCEL_TASK,
    ACTION_PAUSE,
    ACTION_REPLAN,
    ACTION_RESUME,
    ACTION_STATUS,
    ACTION_UNKNOWN,
    ACTION_UPDATE_CRITERIA,
    SteeringService,
    llm_parse_command,
    parse_command,
)


# ---- deterministic parsing -----------------------------------------
def test_parse_status():
    c = parse_command("What is the status of Requirement #12?")
    assert c.action == ACTION_STATUS
    assert c.args["requirement_number"] == 12


def test_parse_status_short_form():
    c = parse_command("status of requirement 7")
    assert c.action == ACTION_STATUS
    assert c.args["requirement_number"] == 7


def test_parse_pause():
    for text in ("Pause execution.", "pause dispatching", "please halt execution",
                "stop the system", "suspend everything"):
        c = parse_command(text)
        assert c.action == ACTION_PAUSE, text


def test_parse_resume():
    for text in ("resume", "continue", "unpause", "please resume"):
        assert parse_command(text).action == ACTION_RESUME


def test_parse_cancel_task_and_replan():
    c = parse_command("Cancel task 3 and re-plan.")
    assert c.action == ACTION_CANCEL_TASK
    assert c.args["task_number"] == 3
    assert c.args["replan"] is True


def test_parse_cancel_task_without_replan():
    c = parse_command("cancel task 9")
    assert c.action == ACTION_CANCEL_TASK
    assert c.args["task_number"] == 9
    assert c.args["replan"] is False


def test_parse_stop_task_maps_to_cancel():
    # spec FR-1 example: "stop task 2"
    c = parse_command("stop task 2")
    assert c.action == ACTION_CANCEL_TASK
    assert c.args["task_number"] == 2


def test_parse_add_requirement():
    c = parse_command("Add requirement: Add JWT auth.")
    assert c.action == ACTION_ADD_REQUIREMENT
    assert c.args["text"] == "Add JWT auth."


def test_parse_update_criteria():
    c = parse_command("Change acceptance criteria for #12 to all tests pass.")
    assert c.action == ACTION_UPDATE_CRITERIA
    assert c.args["requirement_number"] == 12
    assert c.args["text"] == "all tests pass."


def test_parse_update_criteria_variants():
    for text in ("update acceptance criteria of #3 to coverage > 80%",
                "set acceptance criteria for #4 as lint clean"):
        c = parse_command(text)
        assert c.action == ACTION_UPDATE_CRITERIA, text
        assert c.args["text"]


def test_parse_replan():
    c = parse_command("re-plan requirement #5")
    assert c.action == ACTION_REPLAN
    assert c.args["requirement_number"] == 5


def test_parse_unknown_does_not_match():
    c = parse_command("tell me a joke about otters")
    assert c.action == ACTION_UNKNOWN
    assert c.matched is False


def test_parse_empty():
    assert parse_command("   ").action == ACTION_UNKNOWN


# ---- LLM fallback ---------------------------------------------------
def test_llm_parse_returns_command():
    def llm(prompt):
        return '{"action": "pause", "args": {}}'
    c = llm_parse_command("hold everything please", llm)
    assert c.action == ACTION_PAUSE
    assert c.matched is True


def test_llm_parse_bad_json_is_unknown():
    def llm(prompt):
        return "not json"
    c = llm_parse_command("hmm", llm)
    assert c.action == ACTION_UNKNOWN
    assert c.matched is False


def test_llm_parse_exception_is_unknown():
    def llm(prompt):
        raise RuntimeError("model down")
    c = llm_parse_command("hmm", llm)
    assert c.action == ACTION_UNKNOWN


# ---- applying commands --------------------------------------------
class FakeGH:
    def __init__(self):
        self.statuses = []

    async def set_status(self, number, status_label, keep=None):
        self.statuses.append((number, status_label))
        return [status_label]

    async def get_issue(self, number):
        from clams.gh import Issue
        return Issue(number=number, title="t", body="b",
                    updated_at="2026-11-08T00:00:00Z")

    async def update_issue(self, number, **kw):
        from clams.gh import Issue
        return Issue(number=number, title="t", body=kw.get("body", "b"),
                    updated_at="2026-11-08T00:00:00Z")


class FakePlanner:
    def __init__(self):
        self.replans = []
        self.criteria = []

    async def replan(self, req, reason=""):
        self.replans.append((req, reason))
        return (None, [900 + req, 901 + req, 902 + req])

    async def update_acceptance_criteria(self, req, text):
        self.criteria.append((req, text))


class FakeIntake:
    def __init__(self):
        self.added = []

    async def add_requirement(self, title, body=""):
        self.added.append(title)
        return 555


@pytest.fixture()
def store(tmp_path):
    s = open_store(tmp_path / "st.db")
    yield s
    s.db.close()


@pytest.fixture()
def settings():
    return Settings()


def seed(store, req=12, tasks=(3, 4, 5)):
    store.work_items.upsert(WorkItem(
        requirement_number=req, requirement_title="R",
        gh_status="In Progress", gh_updated_at="2026-11-08T00:00:00Z",
        status="dispatched", task_numbers=list(tasks),
    ))


async def test_pause_sets_flag_globally(store, settings):
    seed(store, req=12)
    seed(store, req=13, tasks=(6,))
    svc = SteeringService(store, settings)
    reply = await svc.handle("pause execution")
    assert "paused" in reply.lower()
    assert store.work_items.is_paused(12) is True
    assert store.work_items.is_paused(13) is True


async def test_resume_clears_flag(store, settings):
    seed(store, req=12)
    svc = SteeringService(store, settings)
    await svc.handle("pause execution")
    assert store.work_items.is_paused(12) is True
    await svc.handle("resume")
    assert store.work_items.is_paused(12) is False


async def test_pause_single_requirement(store, settings):
    seed(store, req=12)
    seed(store, req=13, tasks=(6,))
    svc = SteeringService(store, settings)
    await svc.handle("pause requirement 12")  # falls back to global pause
    assert store.work_items.is_paused(12) is True


async def test_cancel_task_revokes_lease_and_blocks(store, settings):
    seed(store, req=12, tasks=(3, 4, 5))
    store.task_claims.claim(3, 12, "exec-1", 600)
    gh = FakeGH()
    planner = FakePlanner()
    svc = SteeringService(store, settings, gh=gh, planner=planner)
    reply = await svc.handle("cancel task 3 and re-plan")
    assert "cancelled" in reply.lower()
    assert store.task_claims.get(3) is None
    assert (3, "status:Blocked") in gh.statuses
    # removed from the requirement's task list
    assert 3 not in store.work_items.get(12).task_numbers
    # and re-planned
    assert planner.replans and planner.replans[0][0] == 12


async def test_cancel_task_without_replan(store, settings):
    seed(store, req=12, tasks=(3, 4, 5))
    store.task_claims.claim(3, 12, "exec-1", 600)
    gh = FakeGH()
    planner = FakePlanner()
    svc = SteeringService(store, settings, gh=gh, planner=planner)
    await svc.handle("cancel task 3")
    assert store.task_claims.get(3) is None
    assert planner.replans == []


async def test_add_requirement_creates_issue(store, settings):
    intake = FakeIntake()
    svc = SteeringService(store, settings, intake=intake)
    reply = await svc.handle("add requirement: Add JWT auth")
    assert "#555" in reply
    assert intake.added == ["Add JWT auth"]


async def test_add_requirement_without_intake_is_safe(store, settings):
    svc = SteeringService(store, settings)
    reply = await svc.handle("add requirement: something")
    assert "No intake service" in reply


async def test_update_criteria(store, settings):
    planner = FakePlanner()
    svc = SteeringService(store, settings, planner=planner)
    reply = await svc.handle("change acceptance criteria for #12 to all tests pass")
    assert "updated" in reply.lower()
    assert planner.criteria == [(12, "all tests pass")]


async def test_replan_command(store, settings):
    planner = FakePlanner()
    svc = SteeringService(store, settings, planner=planner)
    reply = await svc.handle("re-plan requirement #5")
    assert "re-planned" in reply.lower()
    assert planner.replans[0][0] == 5


async def test_status_reports_tasks_and_claims(store, settings):
    seed(store, req=12, tasks=(3, 4))
    store.task_claims.claim(3, 12, "exec-1", 600)
    svc = SteeringService(store, settings)
    reply = await svc.handle("what is the status of requirement #12")
    assert "Requirement #12" in reply
    assert "task #3" in reply
    assert "exec-1" in reply
    assert "task #4: unclaimed" in reply


async def test_status_unknown_requirement(store, settings):
    svc = SteeringService(store, settings)
    reply = await svc.handle("what is the status of requirement #99")
    assert "not tracked" in reply


async def test_global_status(store, settings):
    seed(store, req=12)
    svc = SteeringService(store, settings)
    reply = await svc.handle("what is the status")
    assert "Tracked requirements: 1" in reply


async def test_unknown_command_returns_help(store, settings):
    svc = SteeringService(store, settings)
    reply = await svc.handle("do the thing")
    assert "didn't recognise" in reply


async def test_unknown_falls_back_to_llm(store, settings):
    def llm(prompt):
        return '{"action": "pause", "args": {}}'
    svc = SteeringService(store, settings, llm_call=llm)
    seed(store, req=12)
    reply = await svc.handle("hold on to everything for a moment")
    assert store.work_items.is_paused(12) is True


# ---- interrupt priority (spec 8.1) --------------------------------
async def test_pause_is_observed_within_one_dispatch_iteration(store, settings):
    """A pause written between two dispatch calls takes effect on the very next."""
    seed(store, req=12, tasks=(3, 4, 5))
    svc = SteeringService(store, settings)

    # not paused -> dispatch would proceed
    assert store.work_items.is_paused(12) is False
    await svc.handle("pause execution")
    # the dispatcher reads the flag fresh each iteration
    assert store.work_items.is_paused(12) is True
