"""Regression tests for the issue #18 multi-project pipeline.

These exercise the REAL PlannerService / ReviewService / Orchestrator with a
non-zero project id -- the wiring the production CLI uses (``ensure_default_project``
always returns id >= 1). Before the fix, planning wrote to the project-0 bucket,
so nothing was ever dispatched or reviewed and the same requirement was re-planned
every cycle.
"""
from __future__ import annotations

import json

import pytest

from clams.bus import InMemoryBus, publish_result, read_task
from clams.config import Settings
from clams.contracts import (ArtifactDelta, CommandEvidence, EnvironmentState,
                             Evidence, ExecutionResult)
from clams.db import open_store
from clams.gh import Issue, STATUS_READY
from clams.intake import IntakeService, _is_task_subissue
from clams.orchestrator import Orchestrator, ProjectContext
from clams.planner import PlannerService
from clams.review import ReviewService


class FakeGH:
    def __init__(self, issues=None):
        self.issues = dict(issues or {})
        self._next = max(self.issues, default=1000) + 1
        self.comments = []
        self.statuses = []
        self.sub_links = []

    async def list_issues(self, state="open", labels=None, since=None, per_page=100):
        labels = labels or []
        return [i for i in self.issues.values()
                if i.state == state and all(l in i.labels for l in labels)]

    async def get_issue(self, number):
        return self.issues[number]

    async def create_issue(self, title, body="", labels=None):
        n = self._next
        self._next += 1
        self.issues[n] = Issue(number=n, title=title, body=body,
                               labels=list(labels or []))
        return self.issues[n]

    async def add_sub_issue(self, parent, child):
        self.sub_links.append((parent, child))
        return True

    async def add_comment(self, number, body):
        self.comments.append((number, body))
        return {}

    async def add_labels(self, number, labels):
        return []

    async def set_status(self, number, status_label, keep=None):
        self.statuses.append((number, status_label))
        return []


def _plan_llm(prompt: str) -> str:
    if "PASS or FAIL" in prompt:
        return "PASS"
    return json.dumps({"tasks": [
        {"title": f"task {i}", "objective": f"obj {i}",
         "acceptance_criterion": f"crit {i}", "writable": ["src/"],
         "read_only": [], "forbidden": []} for i in range(1, 4)]})


def _result(task_issue: int, project_id: int) -> ExecutionResult:
    return ExecutionResult(
        task_issue=task_issue, status="completed", summary="done",
        evidence=Evidence(commands=[CommandEvidence(cmd="pytest", exit_code=0,
                                                    tail="ok")]),
        artifact_delta=ArtifactDelta(files_changed=["a.py"], commit="c0ffee",
                                     branch=f"task/{task_issue}"),
        environment_state=EnvironmentState(git_head="c0ffee",
                                           working_tree_clean=True),
        project_id=project_id)


@pytest.fixture()
def env(tmp_path):
    s = Settings(paths={
        "data_dir": str(tmp_path / "d"),
        "db_path": str(tmp_path / "d" / "mp.db"),
        "work_root": str(tmp_path / "work"),
        "repo_path": str(tmp_path),
        "heartbeat_dir": str(tmp_path / "d" / "hb"),
    })
    store = open_store(s.paths.db_path)
    # Two registered projects with real (>=1) ids, like ensure_default_project.
    pid_def = int(store.projects.add(name="widgets",
                                     url="https://github.com/acme/widgets")["id"])
    pid_ext = int(store.projects.add(name="gadgets",
                                     url="https://github.com/acme/gadgets")["id"])
    assert pid_def >= 1 and pid_ext >= 1
    # Both projects have been provisioned a working tree of their own, which is
    # what the dispatcher must use (#115). The orchestrator's own checkout
    # (``repo_path``) is deliberately a *different* directory.
    store.projects.set_worktree(pid_def, str(tmp_path / "def"), "main")
    store.projects.set_worktree(pid_ext, str(tmp_path / "ext"), "main")

    gh_def = FakeGH({100: Issue(number=100, title="Req A", body="AC",
                                labels=[STATUS_READY])})
    gh_ext = FakeGH({200: Issue(number=200, title="Req B", body="AC",
                                labels=[STATUS_READY])})

    planner = PlannerService(store, gh_def, s, llm_call=_plan_llm,
                            project_id=pid_def)
    review = ReviewService(store, gh_def, s, judge=planner.judge_review,
                          project_id=pid_def)
    ctx_planner = PlannerService(store, gh_ext, s, llm_call=_plan_llm,
                                project_id=pid_ext)
    ctx = ProjectContext(
        project_id=pid_ext, gh=gh_ext,
        intake=IntakeService(store, gh_ext, s, project_id=pid_ext),
        planner=ctx_planner,
        review=ReviewService(store, gh_ext, s, judge=ctx_planner.judge_review,
                             project_id=pid_ext),
        worktree_path=str(tmp_path / "ext"), default_branch="main")

    bus = InMemoryBus()
    orch = Orchestrator(store, bus, s, gh=gh_def,
                        intake=IntakeService(store, gh_def, s, project_id=pid_def),
                        planner=planner, review=review,
                        project_contexts=[ctx], default_project_id=pid_def)
    yield dict(s=s, store=store, orch=orch, bus=bus, gh_def=gh_def,
               gh_ext=gh_ext, pid_def=pid_def, pid_ext=pid_ext)
    store.db.close()


@pytest.mark.asyncio
async def test_full_cycle_advances_both_projects(env):
    orch, store = env["orch"], env["store"]
    pid_def, pid_ext = env["pid_def"], env["pid_ext"]

    # Cycle 1: intake -> plan -> dispatch (no results yet).
    rep1 = await orch.cycle()
    assert sorted(rep1.intake) == [100, 200]
    assert sorted(rep1.planned) == [100, 200]
    assert sorted(rep1.dispatched) == [101, 102, 103, 201, 202, 203]
    assert rep1.errors == []

    # The plan lives on the OWNING project, not the project-0 bucket.
    assert store.work_items.get(100, pid_def).status == "dispatched"
    assert store.work_items.get(200, pid_ext).status == "dispatched"
    assert store.work_items.get(100, 0) is None
    assert store.work_items.get(200, 0) is None

    # Executor returns passing evidence for every dispatched task.
    while True:
        brief = await read_task(env["bus"], "executors", "e1", count=1, block_ms=10)
        if brief is None:
            break
        await publish_result(env["bus"], _result(brief.task_issue,
                                                 brief.project_id))

    # Cycle 2: review the results, deliver the outbox, refresh project status.
    rep2 = await orch.cycle()
    assert sorted(rep2.reviewed) == [100, 200]
    assert rep2.errors == []
    assert store.work_items.get(100, pid_def).status == "review"
    assert store.work_items.get(200, pid_ext).status == "review"
    assert store.projects.get(pid_def)["status"] == "In review"
    assert store.projects.get(pid_ext)["status"] == "In review"


@pytest.mark.asyncio
async def test_outbox_routes_side_effects_to_owning_project(env):
    orch, store = env["orch"], env["store"]
    await orch.cycle()  # claims both -> queues gh_status/gh_comment per project
    await orch.step_outbox(__import__("clams.orchestrator",
                                       fromlist=["CycleReport"]).CycleReport())
    # Each project's claim comment landed on that project's client only.
    assert any(n == 100 for n, _ in env["gh_def"].comments)
    assert not any(n == 100 for n, _ in env["gh_ext"].comments)
    assert any(n == 200 for n, _ in env["gh_ext"].comments)
    assert not any(n == 200 for n, _ in env["gh_def"].comments)
    unsent = store.db.conn.execute(
        "SELECT COUNT(*) c FROM outbox WHERE status='pending'").fetchone()["c"]
    assert unsent == 0


@pytest.mark.asyncio
async def test_planner_task_subissues_are_not_re_intaken(env):
    orch = env["orch"]
    await orch.cycle()  # plans #100 -> creates task sub-issues labelled Ready
    subs = [i for n, i in env["gh_def"].issues.items() if n > 100]
    assert subs, "planner should have created sub-issue tasks"
    assert all(_is_task_subissue(s) for s in subs)

    before = len(env["gh_def"].issues)
    rep = await orch.cycle()  # intake must skip the tasks, not re-plan them
    assert 100 not in rep.planned
    assert len(env["gh_def"].issues) == before  # no runaway issue creation


# ---- issue #115: the default project must dispatch against its own tree ----

@pytest.mark.asyncio
async def test_default_project_dispatch_uses_provisioned_worktree(env):
    """A Claudio task must be cut from Claudio's clone, not Clams' checkout."""
    orch, store, s = env["orch"], env["store"], env["s"]
    pid_def, pid_ext = env["pid_def"], env["pid_ext"]
    await orch.cycle()

    briefs = {}
    while True:
        b = await read_task(env["bus"], "executors", "e1", count=1, block_ms=10)
        if b is None:
            break
        briefs.setdefault(b.project_id, b)

    assert briefs[pid_def].git.repo_path == str(s.paths.work_root) + "/def" or \
        briefs[pid_def].git.repo_path.endswith("/def")
    assert briefs[pid_ext].git.repo_path.endswith("/ext")
    # The decisive assertion: neither project's brief points at the directory
    # the orchestrator itself runs in.
    assert briefs[pid_def].git.repo_path != s.paths.repo_path
    assert briefs[pid_ext].git.repo_path != s.paths.repo_path


@pytest.mark.asyncio
async def test_unprovisioned_default_project_is_skipped_not_run_in_cwd(env):
    """No provisioned tree => skip and warn; never dispatch into the cwd."""
    orch, store, s = env["orch"], env["store"], env["s"]
    store.projects.set_worktree(env["pid_def"], "", "main")
    # Move the requirement to planned so dispatch has something to do.
    await orch.step_intake(_rep())
    await orch.step_plan(_rep())
    rep = _rep()
    await orch.step_dispatch(rep)
    assert not [t for t in rep.dispatched if t < 200], \
        "default project tasks must not be dispatched without a worktree"
    assert any("no provisioned working tree" in e for e in rep.errors)


def _rep():
    from clams.orchestrator import CycleReport
    return CycleReport()
