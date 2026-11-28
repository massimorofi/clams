"""Recovery classifier: pure orphan / stale-success decisions.

No network: every call goes through ``gh_client=None`` (DB-derived state).
"""
from __future__ import annotations

from clams.db import repo_projects, repo_tasks, repo_workflows
from clams.planner.recovery import (
    collect_open_subissues,
    find_orphans,
    find_stale_success,
    plan_recovery,
    referenced_issue_numbers,
)

BRIEF = {"objective": "o", "acceptance": "a", "run": ["x"],
         "writable": ["*"], "repo": "o/r"}


def _workflow(db, repo="o/r", goal="g"):
    pid = repo_projects.upsert_project(db, repo)
    return repo_workflows.create_workflow(db, pid, "adhoc", goal)


def _task(db, wf, seq, title, *, status="pending", gh_issue_number=None):
    tid = repo_tasks.create_task(db, wf, seq, title, "objective", "acceptance",
                                 BRIEF, gh_issue_number=gh_issue_number)
    if status != "pending":
        db.execute("UPDATE tasks SET status=? WHERE task_id=?", (status, tid))
    return tid


def _sub(number, title="s", task_id="t", state="open"):
    return {"number": number, "title": title, "task_id": task_id, "state": state}


# ----------------------------------------------------- referenced_issue_numbers
def test_referenced_issue_numbers_ignores_none_and_zero(db):
    wf = _workflow(db)
    _task(db, wf, 1, "a", gh_issue_number=None)
    _task(db, wf, 2, "b", gh_issue_number=0)
    _task(db, wf, 3, "c", gh_issue_number=101)
    _task(db, wf, 4, "d", gh_issue_number=102)

    assert referenced_issue_numbers(repo_tasks.list_tasks(db, wf)) == {101, 102}
    assert referenced_issue_numbers([]) == set()


# ------------------------------------------------------------------ orphans
def test_orphan_detected_when_no_task_references_the_number(db):
    wf = _workflow(db)
    t1 = _task(db, wf, 1, "owned", gh_issue_number=101)
    tasks = repo_tasks.list_tasks(db, wf)

    subs = [_sub(101, "owned", t1), _sub(999, "stray", "missing")]
    orphans = find_orphans(subs, tasks)

    assert [o["number"] for o in orphans] == [999]
    assert orphans[0]["title"] == "stray"


def test_subissue_referenced_by_live_task_elsewhere_is_not_orphan(db):
    wf = _workflow(db)
    _task(db, wf, 1, "owned", gh_issue_number=101)

    other = _workflow(db)
    _task(db, other, 1, "live elsewhere", gh_issue_number=777)

    subs = [_sub(101, "owned"), _sub(777, "adopted by a sibling workflow"),
            _sub(555, "nobody owns this")]
    orphans = find_orphans(subs, repo_tasks.list_tasks(db, wf),
                           live_tasks=repo_tasks.list_tasks(db, other))

    assert [o["number"] for o in orphans] == [555]


def test_closed_subissue_is_never_swept_as_orphan(db):
    wf = _workflow(db)
    tasks = repo_tasks.list_tasks(db, wf)
    assert find_orphans([_sub(4242, state="closed")], tasks) == []


# ----------------------------------------------------------- stale success
def test_stale_success_detected_for_open_subissue_whose_task_is_success(db):
    wf = _workflow(db)
    t_ok = _task(db, wf, 1, "done but issue still open", status="success",
                 gh_issue_number=202)
    _task(db, wf, 2, "still running", status="running", gh_issue_number=203)
    tasks = repo_tasks.list_tasks(db, wf)

    subs = [_sub(202, "done but issue still open", t_ok),
            _sub(203, "still running")]
    stale = find_stale_success(subs, tasks)

    assert [s["number"] for s in stale] == [202]


def test_stale_success_matches_by_task_id_when_number_is_missing(db):
    wf = _workflow(db)
    t_ok = _task(db, wf, 1, "done", status="success", gh_issue_number=None)
    stale = find_stale_success([_sub(303, "done", t_ok)],
                               repo_tasks.list_tasks(db, wf))
    assert [s["task_id"] for s in stale] == [t_ok]


def test_closed_subissue_is_never_swept_as_stale_success(db):
    wf = _workflow(db)
    t_ok = _task(db, wf, 1, "done and closed", status="success",
                 gh_issue_number=404)
    tasks = repo_tasks.list_tasks(db, wf)
    assert find_stale_success([_sub(404, "done and closed", t_ok,
                                    state="closed")], tasks) == []


# ------------------------------------------------- collect_open_subissues (db)
def test_collect_open_subissues_derives_state_from_db(db):
    wf = _workflow(db)
    t_open = _task(db, wf, 1, "published", gh_issue_number=501)
    t_done = _task(db, wf, 2, "published and succeeded", status="success",
                   gh_issue_number=502)
    _task(db, wf, 3, "cancelled", status="cancelled", gh_issue_number=503)
    _task(db, wf, 4, "never published", gh_issue_number=None)

    subs = collect_open_subissues(db, wf)

    assert [s["number"] for s in subs] == [501, 502]
    assert all(s["state"] == "open" for s in subs)
    assert subs[0]["task_id"] == t_open
    assert subs[1]["title"] == "published and succeeded"
    assert 503 not in [s["number"] for s in subs]


def test_collect_open_subissues_unknown_workflow_is_empty(db):
    assert collect_open_subissues(db, "no-such-workflow") == []


# --------------------------------------------------------------- plan_recovery
def test_plan_recovery_noop_when_nothing_is_stale(db):
    wf = _workflow(db)
    t_ok = _task(db, wf, 1, "done, issue already closed", status="success")
    _task(db, wf, 2, "not published yet", status="running")

    plan = plan_recovery(db, wf)

    assert plan["noop"] is True
    assert plan["orphans"] == []
    assert plan["stale_success"] == []
    assert plan["orphan_count"] == 0
    assert plan["stale_success_count"] == 0
    assert plan["preserved_success"] == [t_ok]

    # classifier-level idempotency: a second pass says exactly the same thing
    assert plan_recovery(db, wf) == plan


def test_plan_recovery_flags_stale_success(db):
    wf = _workflow(db)
    _task(db, wf, 1, "done, issue still open", status="success",
          gh_issue_number=701)
    _task(db, wf, 2, "running", status="running", gh_issue_number=702)

    plan = plan_recovery(db, wf)

    assert plan["noop"] is False
    assert plan["stale_success_count"] == 1
    assert [s["number"] for s in plan["stale_success"]] == [701]
    assert plan["orphans"] == [] and plan["orphan_count"] == 0
    assert plan["preserved_success"] == []


def test_plan_recovery_ignores_cancelled_tasks(db):
    wf = _workflow(db)
    _task(db, wf, 1, "cancelled", status="cancelled", gh_issue_number=801)

    plan = plan_recovery(db, wf)

    assert plan["noop"] is True
    assert plan["stale_success"] == [] and plan["orphans"] == []


def test_plan_recovery_with_gh_client_keeps_only_open_workflow_issues(db):
    """The gh path: closed sub-issues are never swept, foreign issues ignored."""
    import pytest

    pytest.importorskip("httpx")  # clams.github.client sits behind httpx

    class FakeGH:
        def __init__(self, issues):
            self._issues = issues
            self.calls = []

        def list_issues_by_label(self, repo, label, state="open"):
            self.calls.append((repo, label))
            return self._issues

    wf = _workflow(db)
    t_ok = _task(db, wf, 1, "done", status="success", gh_issue_number=601)
    _task(db, wf, 2, "closed already", gh_issue_number=602)

    gh = FakeGH([{"number": 601, "title": "done", "state": "open"},
                 {"number": 602, "title": "closed already", "state": "closed"},
                 {"number": 700, "title": "not this workflow", "state": "open"}])

    plan = plan_recovery(db, wf, gh_client=gh)

    assert gh.calls == [("o/r", "clams:task")]
    assert [s["number"] for s in plan["stale_success"]] == [601]
    assert plan["orphans"] == []          # 700 belongs to no task here
    assert plan["noop"] is False
    assert plan["preserved_success"] == []
    assert t_ok not in plan["preserved_success"]
