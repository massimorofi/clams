"""S10 — orchestrator loop tests."""
from __future__ import annotations

import json

import pytest

from clams.bus import InMemoryBus, STREAM_TASKS
from clams.config import Settings
from clams.contracts import (
    ArtifactDelta,
    CommandEnvelope,
    EnvironmentState,
    Evidence,
    CommandEvidence,
    ExecutionResult,
)
from clams.db import WorkItem, open_store
from clams.orchestrator import CycleReport, Orchestrator


@pytest.fixture()
def store(tmp_path):
    s = open_store(tmp_path / "orch.db")
    yield s
    s.db.close()


@pytest.fixture()
def settings(tmp_path):
    return Settings(paths={
        "data_dir": str(tmp_path / "d"),
        "db_path": str(tmp_path / "d" / "orch.db"),
        "work_root": str(tmp_path / "work"),
        "repo_path": str(tmp_path),
        "heartbeat_dir": str(tmp_path / "d" / "hb"),
    })


class FakePlanner:
    def __init__(self, n=3):
        self.n = n
        self.calls = []

    async def plan_requirement(self, req, **kw):
        self.calls.append(req)
        tasks = [req * 100 + i for i in range(1, self.n + 1)]
        plan_json = json.dumps({"tasks": [
            {"objective": f"obj {t}", "acceptance_criterion": f"crit {t}",
             "writable": [f"src/{t}.py"], "read_only": [], "forbidden": []}
            for t in tasks
        ]})
        store = self._store
        store.work_items.set_plan(req, plan_json, tasks)
        return (None, tasks)

    async def judge_review(self, req, summary):
        return True


class FakeIntake:
    def __init__(self, claimed=()):
        self.claimed = list(claimed)
        self.sweeps = 0

    async def sweep(self):
        self.sweeps += 1
        res = type("R", (), {"claimed": list(self.claimed)})()
        self.claimed = []
        return res


class FakeReview:
    def __init__(self):
        self.reviewed = []

    async def review_requirement(self, req):
        self.reviewed.append(req)
        return type("V", (), {"passed": True})()


class FakeSteering:
    def __init__(self):
        self.applied = []

    async def apply(self, cmd):
        self.applied.append(cmd)
        return "ok"


def good_result(task):
    return ExecutionResult(
        task_issue=task, status="completed", summary="ok",
        evidence=Evidence(commands=[CommandEvidence(cmd="pytest", exit_code=0, tail="pass")]),
        artifact_delta=ArtifactDelta(files_changed=["a.py"], commit="abc", branch="task/x"),
        environment_state=EnvironmentState(git_head="abc", working_tree_clean=True),
    )


async def test_cycle_runs_all_steps(store, settings):
    store.work_items.upsert(WorkItem(requirement_number=1, requirement_title="R",
                                    gh_status="In Progress",
                                    gh_updated_at="2026-11-08T00:00:00Z",
                                    status="claimed"))
    bus = InMemoryBus()
    planner = FakePlanner(3)
    planner._store = store
    intake = FakeIntake(claimed=())
    review = FakeReview()
    steering = FakeSteering()
    orch = Orchestrator(store, bus, settings, planner=planner, intake=intake,
                       steering=steering, review=review)
    rep = await orch.cycle()
    assert planner.calls == [1]
    assert rep.planned == [1]
    assert sorted(rep.dispatched) == [101, 102, 103]
    assert len(bus.messages[STREAM_TASKS]) == 3


async def test_dispatch_does_not_replay_already_dispatched(store, settings):
    store.work_items.upsert(WorkItem(requirement_number=2, requirement_title="R",
                                    gh_status="In Progress",
                                    gh_updated_at="2026-11-08T00:00:00Z",
                                    status="planned", task_numbers=[201, 202]))
    bus = InMemoryBus()
    orch = Orchestrator(store, bus, settings)
    rep1 = await orch.step_dispatch(CycleReport())
    assert sorted(rep1.dispatched) == [201, 202]
    rep2 = await orch.step_dispatch(CycleReport())
    assert rep2.dispatched == []


async def test_paused_requirement_is_not_dispatched(store, settings):
    store.work_items.upsert(WorkItem(requirement_number=3, requirement_title="R",
                                    gh_status="In Progress",
                                    gh_updated_at="2026-11-08T00:00:00Z",
                                    status="planned", task_numbers=[301],
                                    user_paused=1))
    bus = InMemoryBus()
    orch = Orchestrator(store, bus, settings)
    rep = await orch.step_dispatch(CycleReport())
    assert rep.dispatched == []


async def test_commands_are_drained_first(store, settings):
    bus = InMemoryBus()
    await bus.publish("clams:commands",
                     CommandEnvelope(action="pause", raw_text="pause execution",
                                    issued_at="2026-11-08T00:00:00Z"),
                     msg_type="CommandEnvelope")
    steering = FakeSteering()
    orch = Orchestrator(store, bus, settings, steering=steering)
    rep = await orch.cycle()
    assert rep.commands == 1
    assert len(steering.applied) == 1


async def test_intake_results_reported(store, settings):
    bus = InMemoryBus()
    intake = FakeIntake(claimed=(9, 10))
    orch = Orchestrator(store, bus, settings, intake=intake)
    rep = await orch.cycle()
    assert sorted(rep.intake) == [9, 10]


async def test_results_trigger_review_when_all_reported(store, settings):
    store.work_items.upsert(WorkItem(requirement_number=4, requirement_title="R",
                                    gh_status="In Progress",
                                    gh_updated_at="2026-11-08T00:00:00Z",
                                    status="dispatched", task_numbers=[401]))
    bus = InMemoryBus()
    await bus.publish("clams:results", good_result(401), msg_type="ExecutionResult")
    review = FakeReview()
    orch = Orchestrator(store, bus, settings, review=review)
    rep = await orch.cycle()
    assert review.reviewed == [4]


async def test_review_waits_until_every_task_reported(store, settings):
    store.work_items.upsert(WorkItem(requirement_number=5, requirement_title="R",
                                    gh_status="In Progress",
                                    gh_updated_at="2026-11-08T00:00:00Z",
                                    status="dispatched", task_numbers=[501, 502]))
    bus = InMemoryBus()
    await bus.publish("clams:results", good_result(501), msg_type="ExecutionResult")
    review = FakeReview()
    orch = Orchestrator(store, bus, settings, review=review)
    await orch.cycle()
    assert review.reviewed == []


async def test_outbox_is_delivered_each_cycle(store, settings):
    store.outbox.enqueue("notification", {"type": "noop"}, "noop-1")
    bus = InMemoryBus()
    delivered = []
    orch = Orchestrator(store, bus, settings)
    # patch the worker's notifier so delivery succeeds
    orch.outbox.notifier = lambda p: delivered.append(p)
    rep = await orch.cycle()
    assert rep.delivered >= 1


async def test_heartbeat_written(store, settings):
    bus = InMemoryBus()
    orch = Orchestrator(store, bus, settings)
    await orch.step_heartbeat(CycleReport())
    hb = store.settings if False else None
    path = settings.paths.heartbeat_dir + "/orchestrator.json"
    import os
    assert os.path.exists(path)
    data = json.loads(open(path).read())
    assert data["service"] == "orchestrator"


async def test_one_failing_step_does_not_stop_the_cycle(store, settings):
    class Boom:
        async def sweep(self):
            raise RuntimeError("intake exploded")

    bus = InMemoryBus()
    orch = Orchestrator(store, bus, settings, intake=Boom())
    rep = await orch.cycle()
    assert any("intake exploded" in e for e in rep.errors)


async def test_stop_terminates_run_forever(store, settings):
    bus = InMemoryBus()
    orch = Orchestrator(store, bus, settings)

    async def stopper():
        await asyncio.sleep(0.05)
        orch.stop()

    import asyncio
    await asyncio.wait_for(orch.run_forever(), timeout=2.0) if False else None
    # start run_forever, then stop it
    task = asyncio.create_task(orch.run_forever())
    await asyncio.sleep(0.05)
    orch.stop()
    await asyncio.wait_for(task, timeout=2.0)
    assert True


def test_cycle_report_summary():
    assert "commands=0" in CycleReport().summary()
