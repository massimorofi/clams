"""WP3: executor-runner end-to-end against a fixture repo + fake Claudio."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from clams import contracts
from clams.bus import InMemoryBus
from clams.config import Settings
from clams.claudio_client import ChatResponse, ClaudioError
from clams.executor.runner import ExecutorRunner, build_prompt, detect_needs_input
from clams.executor.worktree import GitError, WorktreeManager


@pytest.fixture()
def remote(tmp_path):
    r = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", str(r)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(seed)], check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=seed, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=seed, check=True)
    (seed / "hello.txt").write_text("hi\n")
    subprocess.run(["git", "add", "."], cwd=seed, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=seed, check=True,
                   capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(r)], cwd=seed,
                   check=True, capture_output=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=seed,
                   check=True, capture_output=True)
    return r


class FakeClaudio:
    """Scripted agent: performs an action in the workspace, returns text."""

    def __init__(self, action):
        self.action = action
        self.calls = []

    def chat(self, message, session_id=None, approval_mode=None,
             workspace_dir=None, timeout=None):
        self.calls.append({"message": message, "session_id": session_id,
                           "approval_mode": approval_mode,
                           "workspace_dir": workspace_dir})
        self.action(Path(workspace_dir))
        return ChatResponse(run_id="r1", session_id=session_id or "s1",
                            final_output="done the work", usage={})


def _settings(tmp_path):
    return Settings(_env_file=None, heartbeat_interval_s=1,
                    executor_alive_interval_s=0, work_root=str(tmp_path / "work"),
                    db_path=str(tmp_path / "c.db"))


def _dispatch(bus, s, brief=None, task_id="t1", attempt=1, token="L1"):
    b = brief or contracts.Brief(
        objective="add a module", acceptance="new.py exists and imports",
        run=["test -f new.py && echo verified"], writable=["*.py"], repo="o/r",
        base_ref="main")
    m = contracts.TaskDispatch(task_id=task_id, workflow_id="w1",
                               attempt=attempt, lease_token=token, brief=b)
    bus.publish(s.stream_tasks, contracts.dump_message(m))
    return m


def _drain_events(bus, s):
    bus.ensure_group(s.stream_events, "testgrp")
    return [contracts.parse_message(d.payload)
            for d in bus.read(s.stream_events, "testgrp", "t", count=50)]


def test_success_flow(tmp_path, remote):
    s = _settings(tmp_path)
    bus = InMemoryBus()
    bus.ensure_group(s.stream_tasks, s.group_executors)
    wt = WorktreeManager(s.work_root, remote_url_template=str(remote))

    def agent(ws: Path):
        (ws / "new.py").write_text("VALUE = 1\n")
        subprocess.run(["git", "add", "."], cwd=ws, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "feat"], cwd=ws, check=True,
                       capture_output=True)

    runner = ExecutorRunner(s, bus, FakeClaudio(agent), wt)
    m = _dispatch(bus, s)
    got = bus.read(s.stream_tasks, s.group_executors, "e1", count=1)
    runner._handle(got[0])

    events = _drain_events(bus, s)
    types = [e.type for e in events]
    assert types == ["task.started", "task.result"]
    res = events[-1]
    assert res.outcome == "success"
    assert res.evidence["passed"] is True
    assert res.commit_sha
    assert res.branch == "clams/t1-a1"
    # branch pushed to remote
    refs = subprocess.run(["git", "ls-remote", "--heads",
                           str(remote)],
                          capture_output=True, text=True).stdout
    assert "clams/t1-a1" in refs
    # worktree removed on success
    assert not wt.worktree_path("o/r", "t1", 1).exists()
    # dispatch acked
    assert bus.pending_count(s.stream_tasks, s.group_executors) == 0


def _commit(ws: Path, msg: str):
    subprocess.run(["git", "add", "."], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=ws, check=True,
                   capture_output=True)


def _run_one(runner, bus, s):
    got = bus.read(s.stream_tasks, s.group_executors, "e1", count=1)
    runner._handle(got[0])
    return [e for e in _drain_events(bus, s) if e.type == "task.result"][0]


def test_second_task_seeds_from_integration_branch(tmp_path, remote):
    """claudio#133, the wasted attempt: a chained task's first attempt seeded
    from main, found none of its dependencies' files, recreated them, and was
    then failed by the scope gate for the recreation. It must instead start
    from the workflow's integration ref — and not be charged for what was
    already there."""
    s = _settings(tmp_path)
    bus = InMemoryBus()
    bus.ensure_group(s.stream_tasks, s.group_executors)
    wt = WorktreeManager(s.work_root, remote_url_template=str(remote))

    def build_module(ws: Path):
        (ws / "new.py").write_text("VALUE = 1\n")
        _commit(ws, "t1 work")

    _dispatch(bus, s, task_id="t1", token="L1")
    res1 = _run_one(ExecutorRunner(s, bus, FakeClaudio(build_module), wt),
                    bus, s)
    assert res1.outcome == "success"
    assert wt.ref_exists("o/r", "clams/wf/w1")

    seen: list[bool] = []

    def write_notes(ws: Path):
        seen.append((ws / "new.py").exists())   # proof the seed carried t1
        (ws / "notes.txt").write_text("notes\n")
        _commit(ws, "t2 work")

    b2 = contracts.Brief(objective="document the module",
                         acceptance="notes.txt exists",
                         run=["test -f new.py && echo ok"],
                         writable=["*.txt"], repo="o/r", base_ref="main")
    _dispatch(bus, s, brief=b2, task_id="t2", token="L2")
    res2 = _run_one(ExecutorRunner(s, bus, FakeClaudio(write_notes), wt),
                    bus, s)

    assert seen == [True]
    assert res2.outcome == "success", res2.evidence
    assert res2.changed_files == ["notes.txt"]  # t1's new.py not charged
    assert "integration_error" not in res2.evidence
    # the tip advanced to t2's commit
    assert wt.ref_exists("o/r", "clams/wf/w1")


def test_integration_failure_does_not_fail_task(tmp_path, remote, monkeypatch):
    """Integration is an optimisation, never a verdict: a task that passed the
    gate stays passed if the ref cannot be advanced."""
    s = _settings(tmp_path)
    bus = InMemoryBus()
    bus.ensure_group(s.stream_tasks, s.group_executors)
    wt = WorktreeManager(s.work_root, remote_url_template=str(remote))

    def boom(*a, **k):
        raise GitError("simulated integration failure")

    monkeypatch.setattr(wt, "integrate", boom)

    def agent(ws: Path):
        (ws / "new.py").write_text("VALUE = 1\n")
        _commit(ws, "work")

    _dispatch(bus, s)
    res = _run_one(ExecutorRunner(s, bus, FakeClaudio(agent), wt), bus, s)
    assert res.outcome == "success"
    assert "simulated integration failure" in res.evidence["integration_error"]
    assert not wt.ref_exists("o/r", "clams/wf/w1")


def test_gate_fail_keeps_worktree(tmp_path, remote):
    s = _settings(tmp_path)
    bus = InMemoryBus()
    bus.ensure_group(s.stream_tasks, s.group_executors)
    wt = WorktreeManager(s.work_root,
                         remote_url_template=str(remote))

    def agent(ws: Path):
        (ws / "bad.txt").write_text("nope\n")  # outside writable globs
        subprocess.run(["git", "add", "."], cwd=ws, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "bad"], cwd=ws, check=True,
                       capture_output=True)

    runner = ExecutorRunner(s, bus, FakeClaudio(agent), wt)
    _dispatch(bus, s)
    got = bus.read(s.stream_tasks, s.group_executors, "e1", count=1)
    runner._handle(got[0])
    events = _drain_events(bus, s)
    res = [e for e in events if e.type == "task.result"][0]
    assert res.outcome == "failed"
    assert res.evidence["passed"] is False
    # failed worktree kept for forensics
    assert wt.worktree_path("o/r", "t1", 1).exists()


def test_needs_input_flow(tmp_path, remote):
    s = _settings(tmp_path)
    bus = InMemoryBus()
    bus.ensure_group(s.stream_tasks, s.group_executors)
    wt = WorktreeManager(s.work_root,
                         remote_url_template=str(remote))

    class AskClaudio:
        def chat(self, message, session_id=None, approval_mode=None,
                 workspace_dir=None, timeout=None):
            return ChatResponse(run_id="r", session_id=session_id or "s",
                                final_output="NEEDS_INPUT: which DB?\n"
                                             "OPTIONS: sqlite | postgres",
                                usage={})

    runner = ExecutorRunner(s, bus, AskClaudio(), wt)
    _dispatch(bus, s)
    got = bus.read(s.stream_tasks, s.group_executors, "e1", count=1)
    runner._handle(got[0])
    events = _drain_events(bus, s)
    q = [e for e in events if e.type == "task.question"]
    assert len(q) == 1
    assert q[0].question == "which DB?"
    assert q[0].options == ["sqlite", "postgres"]
    assert q[0].idempotency_key == "q:t1:1"
    assert not any(e.type == "task.result" for e in events)
    assert bus.pending_count(s.stream_tasks, s.group_executors) == 0


def test_claudio_down_fails_task(tmp_path, remote):
    s = _settings(tmp_path)
    bus = InMemoryBus()
    bus.ensure_group(s.stream_tasks, s.group_executors)
    wt = WorktreeManager(s.work_root,
                         remote_url_template=str(remote))

    class Down:
        def chat(self, *a, **k):
            raise ClaudioError("boom", status=500)

    runner = ExecutorRunner(s, bus, Down(), wt)
    _dispatch(bus, s)
    got = bus.read(s.stream_tasks, s.group_executors, "e1", count=1)
    runner._handle(got[0])
    events = _drain_events(bus, s)
    res = [e for e in events if e.type == "task.result"][0]
    assert res.outcome == "failed"


def test_poison_message_dlq(tmp_path, remote):
    s = _settings(tmp_path)
    bus = InMemoryBus()
    bus.ensure_group(s.stream_tasks, s.group_executors)
    wt = WorktreeManager(s.work_root,
                         remote_url_template=str(remote))
    runner = ExecutorRunner(s, bus, FakeClaudio(lambda ws: None), wt)
    bus.publish(s.stream_tasks, "{not json")
    got = bus.read(s.stream_tasks, s.group_executors, "e1", count=1)
    runner._handle(got[0])
    assert bus.xlen(s.stream_dlq) == 1
    assert bus.pending_count(s.stream_tasks, s.group_executors) == 0


def test_over_max_deliveries_dlq(tmp_path, remote):
    s = Settings(_env_file=None, dlq_max_deliveries=2,
                 heartbeat_interval_s=1, work_root=str(tmp_path / "w"),
                 db_path=str(tmp_path / "c.db"))
    bus = InMemoryBus()
    bus.ensure_group(s.stream_tasks, s.group_executors)
    wt = WorktreeManager(s.work_root,
                         remote_url_template=str(remote))
    runner = ExecutorRunner(s, bus, FakeClaudio(lambda ws: None), wt)
    _dispatch(bus, s)
    got = bus.read(s.stream_tasks, s.group_executors, "e1", count=1)
    got[0].deliveries = 3
    runner._handle(got[0])
    assert bus.xlen(s.stream_dlq) == 1


def test_prompt_contains_brief_and_answer():
    b = contracts.Brief(objective="obj", acceptance="acc", run=["make test"],
                        writable=["src/**"], forbidden=[".env"], repo="o/r",
                        answer="use sqlite")
    p = build_prompt(b)
    assert "obj" in p and "make test" in p and ".env" in p
    assert "use sqlite" in p


def test_detect_needs_input():
    assert detect_needs_input("NEEDS_INPUT: q?") == ("q?", [])
    q, o = detect_needs_input("blah\nNEEDS_INPUT: which?\nOPTIONS: a | b")
    assert q == "which?" and o == ["a", "b"]
    assert detect_needs_input("all good") is None
