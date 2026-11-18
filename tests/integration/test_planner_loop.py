"""WP4: full planner loop — a sequential 3-task plan runs in order end-to-end."""
from __future__ import annotations

import json

import pytest

from clams import contracts
from clams.bus import InMemoryBus
from clams.config import Settings
from clams.claudio_client import ChatResponse
from clams.db import repo_tasks, repo_workflows
from clams.outbox import OutboxRelay
from clams.planner.loop import PlannerLoop

PLAN3 = json.dumps({"tasks": [
    {"title": "step one", "objective": "o1", "acceptance": "a1",
     "run": ["echo 1"], "writable": ["a/**"], "depends_on": []},
    {"title": "step two", "objective": "o2", "acceptance": "a2",
     "run": ["echo 2"], "writable": ["b/**"], "depends_on": [1]},
    {"title": "step three", "objective": "o3", "acceptance": "a3",
     "run": ["echo 3"], "writable": ["c/**"], "depends_on": [2]},
]})


class FakeP:
    def __init__(self, reply=PLAN3):
        self.reply = reply

    def chat(self, message, **kw):
        return ChatResponse(run_id="r", session_id="s", final_output=self.reply,
                            usage={})

    def health(self):
        return True


@pytest.fixture()
def loop(tmp_path):
    s = Settings(_env_file=None, db_path=str(tmp_path / "c.db"),
                 planner_cycle_s=0.01, events_block_ms=0,
                 watchdog_probe_interval_s=0)
    bus = InMemoryBus()
    lp = PlannerLoop(s, bus, FakeP())
    lp.outbox = OutboxRelay(bus)
    yield lp, bus, s
    lp.close()


def _seed_workflow(lp):
    from clams.db import repo_projects, repo_workflows as rw
    pid = repo_projects.upsert_project(lp.conn, "o/r")
    wf = rw.create_workflow(lp.conn, pid, "adhoc", "three steps")
    return wf


def test_sequential_three_task_plan(loop):
    lp, bus, s = loop
    wf = _seed_workflow(lp)

    executed: list[str] = []
    order: list[int] = []

    for _ in range(12):
        lp.cycle()
        # act as the executor: consume any dispatched task and succeed it
        bus.ensure_group(s.stream_tasks, s.group_executors)
        for d in bus.read(s.stream_tasks, s.group_executors, "e1", count=10):
            msg = contracts.parse_message(d.payload)
            if msg.type != "task.dispatch":
                continue
            order.append(msg.brief.objective)
            started = contracts.TaskStarted(task_id=msg.task_id,
                                            attempt=msg.attempt,
                                            lease_token=msg.lease_token,
                                            executor_id="e1", session_ref="x")
            bus.publish(s.stream_events, contracts.dump_message(started))
            result = contracts.TaskResult(task_id=msg.task_id,
                                          attempt=msg.attempt,
                                          lease_token=msg.lease_token,
                                          outcome="success",
                                          evidence={"passed": True},
                                          commit_sha="c", branch="b")
            bus.publish(s.stream_events, contracts.dump_message(result))
            executed.append(msg.task_id)
        lp.cycle()   # apply the events

    assert len(executed) == 3
    assert order == ["o1", "o2", "o3"]          # strict sequence
    assert repo_workflows.get_workflow(lp.conn, wf)["status"] == "done"
    statuses = [t["status"] for t in repo_tasks.list_tasks(lp.conn, wf)]
    assert statuses == ["success"] * 3


def test_cyclic_plan_blocked(loop):
    lp, bus, s = loop
    lp.cp = FakeP(json.dumps({"tasks": [
        {"title": "a", "objective": "o", "acceptance": "a", "run": ["x"],
         "writable": ["*"], "depends_on": [2]},
        {"title": "b", "objective": "o", "acceptance": "a", "run": ["x"],
         "writable": ["*"], "depends_on": [1]},
    ]}))
    wf = _seed_workflow(lp)
    lp.cycle()
    assert repo_workflows.get_workflow(lp.conn, wf)["status"] == "blocked"


def test_pause_stops_dispatch_but_processes_events(loop):
    lp, bus, s = loop
    wf = _seed_workflow(lp)
    from clams.db import repo_runtime
    repo_runtime.pause(lp.conn, "operator")
    lp.cycle()
    # planning/dispatch skipped while paused
    assert repo_workflows.get_workflow(lp.conn, wf)["status"] == "intake"
    # but events are still consumed (no pending -> no crash)
    assert bus.pending_count(s.stream_events, s.group_planner) == 0


def test_planner_heartbeat_written(loop):
    lp, bus, s = loop
    from clams.db import repo_runtime
    lp.cycle()
    assert repo_runtime.heartbeat_age_s(lp.conn) is not None
