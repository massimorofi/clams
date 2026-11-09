"""S7 — verification & evidence gate tests."""
from __future__ import annotations

import json

import pytest

from clams.config import Settings
from clams.contracts import (
    ArtifactDelta,
    CommandEvidence,
    EnvironmentState,
    Evidence,
    ExecutionResult,
)
from clams.db import Store, WorkItem, open_store
from clams.review import (
    GateVerdict,
    ReviewService,
    format_evidence_comment,
    format_requirement_comment,
    verify_evidence,
)


def good_result(task=101):
    return ExecutionResult(
        task_issue=task,
        status="completed",
        summary="implemented and tested",
        evidence=Evidence(
            commands=[CommandEvidence(cmd="pytest -q", exit_code=0, tail="12 passed")],
            log_file="/work/task-101/test-output.log",
        ),
        artifact_delta=ArtifactDelta(
            files_changed=["src/a.py", "tests/test_a.py"],
            commit="b21c9f0",
            branch="task/101",
        ),
        environment_state=EnvironmentState(git_head="b21c9f0", working_tree_clean=True),
    )


# ---- verify_evidence -----------------------------------------------
def test_good_evidence_passes():
    v = verify_evidence(good_result())
    assert v.passed is True
    assert v.reasons == []


def test_nonzero_exit_fails_gate():
    r = good_result()
    r.evidence.commands = [CommandEvidence(cmd="pytest", exit_code=1, tail="failed")]
    v = verify_evidence(r)
    assert v.passed is False
    assert any("non-zero exit code" in x for x in v.reasons)


def test_one_failing_command_among_many_fails_gate():
    r = good_result()
    r.evidence.commands = [
        CommandEvidence(cmd="make lint", exit_code=0, tail="ok"),
        CommandEvidence(cmd="pytest", exit_code=2, tail="boom"),
    ]
    assert verify_evidence(r).passed is False


def test_missing_commands_fails_gate():
    r = good_result()
    r.evidence.commands = []
    v = verify_evidence(r)
    assert v.passed is False
    assert any("no commands" in x for x in v.reasons)


def test_empty_stdout_fails_gate():
    r = good_result()
    r.evidence.commands = [CommandEvidence(cmd="pytest", exit_code=0, tail="   ")]
    v = verify_evidence(r)
    assert v.passed is False
    assert any("empty stdout" in x for x in v.reasons)


def test_missing_commit_fails_gate():
    r = good_result()
    r.artifact_delta.commit = ""
    v = verify_evidence(r)
    assert v.passed is False
    assert any("no commit" in x for x in v.reasons)


def test_no_files_changed_fails_gate():
    r = good_result()
    r.artifact_delta.files_changed = []
    v = verify_evidence(r)
    assert v.passed is False
    assert any("no files changed" in x for x in v.reasons)


def test_dirty_tree_fails_gate():
    r = good_result()
    r.environment_state.working_tree_clean = False
    v = verify_evidence(r)
    assert v.passed is False
    assert any("not clean" in x for x in v.reasons)


def test_non_completed_status_fails_gate():
    r = good_result()
    r.status = "failed"
    assert verify_evidence(r).passed is False


def test_multiple_problems_are_all_reported():
    r = good_result()
    r.status = "failed"
    r.artifact_delta.commit = ""
    r.environment_state.working_tree_clean = False
    v = verify_evidence(r)
    assert len(v.reasons) == 3


# ---- formatting -----------------------------------------------------
def test_evidence_comment_contains_commands_and_verdict():
    txt = format_evidence_comment(good_result(), verify_evidence(good_result()))
    assert "pytest -q" in txt
    assert "exit `0`" in txt
    assert "Evidence gate passed" in txt


def test_evidence_comment_shows_failure_reasons():
    r = good_result()
    r.artifact_delta.commit = ""
    txt = format_evidence_comment(r, verify_evidence(r))
    assert "no commit recorded" in txt


def test_requirement_comment_pass_and_fail():
    ok = format_requirement_comment(5, {1: good_result(1)}, True, [])
    assert "All 1 sub-task(s)" in ok
    bad = format_requirement_comment(5, {}, False, ["task #1: nope"])
    assert "task #1: nope" in bad


# ---- ReviewService -------------------------------------------------
class FakeGH:
    def __init__(self):
        self.comments = []
        self.statuses = []

    async def add_comment(self, number, body):
        self.comments.append((number, body))
        return {"id": len(self.comments)}

    async def set_status(self, number, status_label, keep=None):
        self.statuses.append((number, status_label))
        return [status_label]


@pytest.fixture()
def store(tmp_path):
    s = open_store(tmp_path / "rev.db")
    yield s
    s.db.close()


@pytest.fixture()
def settings():
    return Settings()


def seed(store, req=200, tasks=(201, 202, 203)):
    store.work_items.upsert(WorkItem(
        requirement_number=req, requirement_title="R",
        gh_status="In Progress", gh_updated_at="2026-11-08T00:00:00Z",
        status="dispatched", task_numbers=list(tasks),
    ))


def enqueue_result(store, result, key=None):
    store.outbox.enqueue(
        "gh_comment",
        {"issue": result.task_issue, "body": "evidence", "result": json.loads(result.to_json())},
        idempotency_key=key or f"ev-{result.task_issue}",
    )


async def test_record_task_result_passes(store, settings):
    gh = FakeGH()
    svc = ReviewService(store, gh, settings)
    v = await svc.record_task_result(good_result(301))
    assert v.passed is True
    assert gh.statuses[-1] == (301, "status:Done")
    assert "pytest -q" in gh.comments[-1][1]


async def test_record_task_result_blocks_on_bad_evidence(store, settings):
    gh = FakeGH()
    svc = ReviewService(store, gh, settings)
    r = good_result(302)
    r.evidence.commands = [CommandEvidence(cmd="pytest", exit_code=1, tail="x")]
    v = await svc.record_task_result(r)
    assert v.passed is False
    assert gh.statuses[-1] == (302, "status:Blocked")


async def test_review_requirement_all_pass_moves_to_review(store, settings):
    seed(store, req=400, tasks=(401, 402, 403))
    for t in (401, 402, 403):
        enqueue_result(store, good_result(t))
    gh = FakeGH()
    svc = ReviewService(store, gh, settings)
    v = await svc.review_requirement(400)
    assert v.passed is True
    assert gh.statuses[-1] == (400, "status:Review")
    assert store.work_items.get(400).status == "review"


async def test_review_requirement_one_bad_task_blocks(store, settings):
    seed(store, req=410, tasks=(411, 412, 413))
    enqueue_result(store, good_result(411))
    bad = good_result(412)
    bad.evidence.commands = [CommandEvidence(cmd="pytest", exit_code=1, tail="fail")]
    enqueue_result(store, bad)
    enqueue_result(store, good_result(413))
    gh = FakeGH()
    svc = ReviewService(store, gh, settings)
    v = await svc.review_requirement(410)
    assert v.passed is False
    assert any("task #412" in r for r in v.reasons)
    assert gh.statuses[-1] == (410, "status:Blocked")
    assert store.work_items.get(410).status == "blocked"


async def test_review_requirement_missing_evidence_blocks(store, settings):
    seed(store, req=420, tasks=(421, 422, 423))
    enqueue_result(store, good_result(421))
    gh = FakeGH()
    svc = ReviewService(store, gh, settings)
    v = await svc.review_requirement(420)
    assert v.passed is False
    assert any("missing evidence" in r for r in v.reasons)
    assert gh.statuses[-1] == (420, "status:Blocked")


async def test_review_requirement_untracked(store, settings):
    gh = FakeGH()
    svc = ReviewService(store, gh, settings)
    v = await svc.review_requirement(999)
    assert v.passed is False
    assert "not tracked" in v.reasons[0]


async def test_review_requirement_with_no_tasks(store, settings):
    store.work_items.upsert(WorkItem(
        requirement_number=430, requirement_title="R", gh_status="In Progress",
        gh_updated_at="2026-11-08T00:00:00Z", status="dispatched", task_numbers=[],
    ))
    gh = FakeGH()
    svc = ReviewService(store, gh, settings)
    v = await svc.review_requirement(430)
    assert v.passed is False
    assert "no sub-tasks" in v.reasons[0]
    assert gh.statuses[-1] == (430, "status:Blocked")


async def test_judge_can_reject_even_with_good_evidence(store, settings):
    seed(store, req=440, tasks=(441, 442, 443))
    for t in (441, 442, 443):
        enqueue_result(store, good_result(t))
    gh = FakeGH()

    async def harsh_judge(req, summary):
        return False

    svc = ReviewService(store, gh, settings, judge=harsh_judge)
    v = await svc.review_requirement(440)
    assert v.passed is False
    assert "planner judgement" in v.reasons[0]
    assert gh.statuses[-1] == (440, "status:Blocked")


async def test_judge_accepts(store, settings):
    seed(store, req=450, tasks=(451, 452, 453))
    for t in (451, 452, 453):
        enqueue_result(store, good_result(t))
    gh = FakeGH()

    async def happy_judge(req, summary):
        assert "task #451" in summary
        return True

    svc = ReviewService(store, gh, settings, judge=happy_judge)
    v = await svc.review_requirement(450)
    assert v.passed is True
    assert gh.statuses[-1] == (450, "status:Review")


def test_gate_verdict_summary():
    assert GateVerdict(True).summary() == "evidence gate PASSED"
    assert "boom" in GateVerdict(False, ["boom"]).summary()
