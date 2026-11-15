"""Issue #123: "Pause project" must actually stop that project's pipeline."""
from __future__ import annotations

import pytest

from clams.bus import InMemoryBus, read_task
from clams.config import Settings
from clams.db import WorkItem, open_store
from clams.gh import Issue, STATUS_READY
from clams.intake import IntakeService
from clams.orchestrator import CycleReport, Orchestrator, ProjectContext
from clams.planner import PlannerService
from clams.review import ReviewService

pytestmark = pytest.mark.asyncio


class FakeGH:
    def __init__(self, issues=None):
        self.issues = dict(issues or {})
        self._next = max(self.issues, default=1000) + 1
        self.comments = []
        self.statuses = []

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
        return True

    async def add_comment(self, number, body):
        self.comments.append((number, body))
        return {}

    async def set_status(self, number, status):
        self.statuses.append((number, status))
        return []

    async def add_labels(self, number, labels):
        return []

    async def remove_label(self, number, label):
        return []

    async def ping(self):
        return True


def _plan_llm(prompt):
    import json
    return json.dumps({"tasks": [
        {"title": f"t{i}", "objective": f"do {i}",
         "acceptance_criterion": f"run: pytest -q {i}",
         "writable": ["src/**"], "read_only": [], "forbidden": []}
        for i in range(3)]})


@pytest.fixture()
def env(tmp_path):
    s = Settings(paths={
        "data_dir": str(tmp_path / "d"), "db_path": str(tmp_path / "d" / "pp.db"),
        "work_root": str(tmp_path / "work"), "repo_path": str(tmp_path),
        "heartbeat_dir": str(tmp_path / "d" / "hb")})
    store = open_store(s.paths.db_path)
    pid = int(store.projects.add(name="widgets",
                                 url="https://github.com/acme/widgets")["id"])
    store.projects.set_worktree(pid, str(tmp_path / "wt"), "main")
    gh = FakeGH({100: Issue(number=100, title="Req A", body="AC",
                            labels=[STATUS_READY])})
    planner = PlannerService(store, gh, s, llm_call=_plan_llm, project_id=pid)
    orch = Orchestrator(
        store, InMemoryBus(), s, gh=gh,
        intake=IntakeService(store, gh, s, project_id=pid),
        planner=planner,
        review=ReviewService(store, gh, s, judge=planner.judge_review,
                             project_id=pid),
        default_project_id=pid)
    yield dict(s=s, store=store, orch=orch, gh=gh, pid=pid, bus=orch.bus)
    store.db.close()


async def test_paused_project_does_not_intake(env):
    store, orch, pid = env["store"], env["orch"], env["pid"]
    store.projects.set_paused(pid, True)
    rep = CycleReport()
    await orch.step_intake(rep)
    assert rep.intake == [], "a paused project must not pull in new requirements"


async def test_paused_project_does_not_plan_or_dispatch(env):
    store, orch, pid = env["store"], env["orch"], env["pid"]
    # Get the requirement into 'claimed' while unpaused, then pause.
    await orch.step_intake(CycleReport())
    store.projects.set_paused(pid, True)
    rep = CycleReport()
    await orch.step_plan(rep)
    await orch.step_dispatch(rep)
    assert rep.planned == []
    assert rep.dispatched == []
    assert store.work_items.get(100, pid).status == "claimed"


async def test_resume_lets_the_project_run_again(env):
    store, orch, pid = env["store"], env["orch"], env["pid"]
    await orch.step_intake(CycleReport())
    store.projects.set_paused(pid, True)
    await orch.step_plan(CycleReport())
    store.projects.set_paused(pid, False)
    rep = CycleReport()
    await orch.step_plan(rep)
    await orch.step_dispatch(rep)
    assert rep.planned == [100]
    assert len(rep.dispatched) == 3
    briefs = []
    while True:
        d = await read_task(env["bus"], "executors", "probe", count=1, block_ms=10)
        if d is None:
            break
        briefs.append(d.task_issue)
    assert len(briefs) == 3


async def test_paused_project_is_not_reaped(env):
    """#117's reaper must respect the pause too."""
    store, orch, pid = env["store"], env["orch"], env["pid"]
    store.work_items.upsert(WorkItem(
        requirement_number=120, requirement_title="r", gh_status="In Progress",
        gh_updated_at="2026-11-15T00:00:00Z", task_numbers=[121],
        status="dispatched", project_id=pid))
    store.task_claims.claim(121, 120, "executor-1", lease_seconds=1,
                            project_id=pid)
    store.db.conn.execute(
        "UPDATE task_claims SET lease_expires_at='2020-01-01T00:00:00Z' "
        "WHERE task_number=121")
    store.projects.set_paused(pid, True)
    rep = CycleReport()
    await orch.step_reap_stale(rep)
    assert rep.dispatched == []


async def test_unpaused_project_is_reaped(env):
    store, orch, pid = env["store"], env["orch"], env["pid"]
    store.work_items.upsert(WorkItem(
        requirement_number=120, requirement_title="r", gh_status="In Progress",
        gh_updated_at="2026-11-15T00:00:00Z", task_numbers=[121],
        status="dispatched", project_id=pid))
    store.task_claims.claim(121, 120, "executor-1", lease_seconds=1,
                            project_id=pid)
    store.db.conn.execute(
        "UPDATE task_claims SET lease_expires_at='2020-01-01T00:00:00Z' "
        "WHERE task_number=121")
    rep = CycleReport()
    await orch.step_reap_stale(rep)
    assert rep.dispatched == [121]
