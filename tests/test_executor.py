"""S6 — executor service tests."""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from clams.bus import InMemoryBus, STREAM_RESULTS, STREAM_TASKS
from clams.config import Settings
from clams.contracts import CommandEvidence, ExecutionResult, TaskBrief
from clams.db import Store, WorkItem, open_store
from clams.executor import (
    ClaudioTaskRunner,
    ExecutorService,
    TaskRunOutcome,
    render_task_prompt,
    run_command_logged,
)
from clams.worktree import create_worktree


@pytest.fixture()
def repo(tmp_path) -> Path:
    p = tmp_path / "repo"
    p.mkdir()
    (p / "hello.py").write_text("print('hello')\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=p, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=p, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=p, check=True)
    subprocess.run(["git", "add", "-A"], cwd=p, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=p, check=True)
    return p


@pytest.fixture()
def settings(tmp_path, repo) -> Settings:
    return Settings(
        paths={
            "data_dir": str(tmp_path / "data"),
            "db_path": str(tmp_path / "data" / "exec.db"),
            "work_root": str(tmp_path / "work"),
            "repo_path": str(repo),
            "heartbeat_dir": str(tmp_path / "data" / "heartbeats"),
        }
    )


@pytest.fixture()
def store(tmp_path):
    s = open_store(tmp_path / "data" / "exec.db")
    # task_claims has a FK into work_items: seed the requirements used below.
    for req in (17,):
        s.work_items.upsert(WorkItem(
            requirement_number=req, requirement_title=f"Requirement {req}",
            gh_status="In Progress", gh_updated_at="2026-11-08T00:00:00Z",
        ))
    yield s
    s.db.close()


def brief(task=42, req=17, writable=None, forbidden=None, wall=60, criterion="pytest -q"):
    return TaskBrief(
        task_issue=task,
        requirement_issue=req,
        objective="do the thing",
        acceptance_criterion=criterion,
        scope={
            "writable": writable or ["hello.py"],
            "read_only": ["README.md"],
            "forbidden": forbidden or ["migrations/"],
        },
        git={"base_branch": "main", "base_sha": "HEAD", "work_branch": f"task/{task}"},
        limits={"max_turns": 40, "max_wall_seconds": wall},
    )


# ---- helpers ---------------------------------------------------------
def test_run_command_logged_captures_exit_and_tail():
    ev = run_command_logged("echo hello", Path("."))
    assert ev.exit_code == 0
    assert "hello" in ev.tail


def test_run_command_logged_nonzero():
    ev = run_command_logged("exit 3", Path("."))
    assert ev.exit_code == 3


def test_run_command_logged_timeout():
    ev = run_command_logged("sleep 5", Path("."), timeout=0.2)
    assert ev.exit_code == 124
    assert "TIMEOUT" in ev.tail


def test_render_task_prompt_includes_scope():
    p = render_task_prompt(brief(), Path("/work/task-42"))
    assert "task #42" in p
    assert "hello.py" in p
    assert "/work/task-42" in p


# ---- happy path ------------------------------------------------------
async def test_process_brief_success(store, settings):
    bus = InMemoryBus()

    def runner(br, wt):
        (wt / "hello.py").write_text("print('updated')\n")
        return TaskRunOutcome(
            ok=True,
            commands=[CommandEvidence(cmd="pytest -q", exit_code=0, tail="1 passed")],
            summary="done",
            turns=3,
        )

    svc = ExecutorService(store, bus, settings, task_runner=runner)
    res = await svc.process_brief(brief())
    assert res.status == "completed"
    assert res.evidence.commands[0].exit_code == 0
    assert res.artifact_delta.commit
    assert res.environment_state.working_tree_clean is True
    # result published
    assert len(bus.messages[STREAM_RESULTS]) == 1
    # lease released
    assert store.task_claims.get(42) is None
    # worktree cleaned up
    assert not (Path(settings.paths.work_root) / "task-42").exists()


async def test_success_writes_result_to_bus(store, settings):
    bus = InMemoryBus()

    def runner(br, wt):
        (wt / "hello.py").write_text("x\n")
        return TaskRunOutcome(ok=True, summary="ok")

    svc = ExecutorService(store, bus, settings, task_runner=runner)
    await svc.process_brief(brief(task=43))
    msgs = bus.messages[STREAM_RESULTS]
    assert len(msgs) == 1
    parsed = ExecutionResult.from_json(__import__("json").dumps(msgs[0].payload))
    assert parsed.task_issue == 43
    assert parsed.status == "completed"


# ---- failure paths ---------------------------------------------------
async def test_task_failure_increments_attempts(store, settings):
    bus = InMemoryBus()

    def runner(br, wt):
        return TaskRunOutcome(ok=False, error="agent failed")

    svc = ExecutorService(store, bus, settings, task_runner=runner)
    res = await svc.process_brief(brief(task=44))
    assert res.status == "failed"
    row = store.task_claims.get(44)
    assert row["attempts"] == 1
    # worktree removed on failure
    assert not (Path(settings.paths.work_root) / "task-44").exists()


async def test_max_attempts_blocks_task(store, settings):
    bus = InMemoryBus()

    class GH:
        def __init__(self):
            self.statuses = []

        async def set_status(self, number, status_label, keep=None):
            self.statuses.append((number, status_label))
            return [status_label]

    gh = GH()

    def runner(br, wt):
        return TaskRunOutcome(ok=False, error="nope")

    svc = ExecutorService(store, bus, settings, task_runner=runner, gh=gh)
    for expected in range(1, settings.limits.max_attempts):
        await svc.process_brief(brief(task=45))
        row = store.task_claims.get(45)
        assert row["attempts"] == expected
    # final attempt: attempts reach the cap, the lease is released and the
    # issue is marked Blocked so nothing retries it.
    await svc.process_brief(brief(task=45))
    row = store.task_claims.get(45)
    assert row["attempts"] == settings.limits.max_attempts
    assert gh.statuses[-1] == (45, "status:Blocked")
    alerts = store.alerts.list_unacked()
    assert any("exhausted" in a["message"] for a in alerts)
    # and it can never be claimed again
    assert store.task_claims.claim(45, 17, "someone-else", 60) is False


async def test_wall_clock_overrun_reports_timeout(store, settings):
    bus = InMemoryBus()

    async def slow_runner(br, wt):
        await asyncio.sleep(5)
        return TaskRunOutcome(ok=True, summary="too late")

    svc = ExecutorService(store, bus, settings, task_runner=slow_runner)
    res = await svc.process_brief(brief(task=46, wall=1))
    assert res.status == "timeout"
    assert "max_wall_seconds" in res.summary
    assert not (Path(settings.paths.work_root) / "task-46").exists()


async def test_scope_violation_fails_task(store, settings):
    bus = InMemoryBus()

    def runner(br, wt):
        (wt / "migrations").mkdir(exist_ok=True)
        (wt / "migrations" / "x.sql").write_text("bad\n")
        return TaskRunOutcome(ok=True, summary="did bad")

    svc = ExecutorService(store, bus, settings, task_runner=runner)
    res = await svc.process_brief(brief(task=47))
    assert res.status == "failed"
    assert "scope violations" in res.summary


async def test_claim_conflict_blocks_second_executor(store, settings):
    bus = InMemoryBus()
    # pre-claim the task with another executor, lease far in the future
    store.work_items.upsert(WorkItem(requirement_number=17, requirement_title="r",
                                    gh_status="In Progress",
                                    gh_updated_at="2026-11-08T00:00:00Z"))
    assert store.task_claims.claim(48, 17, "other-exec", 9999) is True

    def runner(br, wt):
        return TaskRunOutcome(ok=True, summary="should not run")

    svc = ExecutorService(store, bus, settings, task_runner=runner)
    res = await svc.process_brief(brief(task=48))
    assert res.status == "blocked"
    assert "other-exec" in res.summary


async def test_run_once_reads_from_bus(store, settings):
    bus = InMemoryBus()
    await bus.publish(STREAM_TASKS, brief(task=49), msg_type="TaskBrief")

    def runner(br, wt):
        (wt / "hello.py").write_text("z\n")
        return TaskRunOutcome(ok=True, summary="ok")

    svc = ExecutorService(store, bus, settings, task_runner=runner)
    res = await svc.run_once()
    assert res is not None
    assert res.status == "completed"
    assert await svc.run_once() is None


async def test_heartbeat_written(store, settings):
    bus = InMemoryBus()
    svc = ExecutorService(store, bus, settings, task_runner=lambda b, w: TaskRunOutcome(ok=True))
    path = svc.write_heartbeat("idle")
    assert path.exists()
    import json
    data = json.loads(path.read_text())
    assert data["service"] == "executor"
    assert data["detail"] == "idle"


# ---- ClaudioTaskRunner ----------------------------------------------
async def test_claudio_task_runner_runs_verification(repo, tmp_path):
    calls = []

    class FakeClaudio:
        async def chat(self, message, **kw):
            calls.append(message)
            class R:
                final_output = "implemented"
            return R()

    runner = ClaudioTaskRunner(FakeClaudio())
    target = tmp_path / "wt-runner"
    create_worktree(repo, target, branch="task/50", base_sha="HEAD")
    out = await runner(brief(task=50, criterion="python -c \"print('ok')\""), target)
    assert out.ok is True
    assert out.commands[0].exit_code == 0
    assert "task #50" in calls[0]


async def test_claudio_task_runner_detects_failing_verification(repo, tmp_path):
    class FakeClaudio:
        async def chat(self, message, **kw):
            class R:
                final_output = "done"
            return R()

    runner = ClaudioTaskRunner(FakeClaudio())
    target = tmp_path / "wt-runner2"
    create_worktree(repo, target, branch="task/51", base_sha="HEAD")
    out = await runner(brief(task=51, criterion="exit 1"), target)
    assert out.ok is False


async def test_claudio_task_runner_handles_claudio_error():
    class BrokenClaudio:
        async def chat(self, message, **kw):
            raise RuntimeError("engine down")

    runner = ClaudioTaskRunner(BrokenClaudio())
    out = await runner(brief(), Path("."))
    assert out.ok is False
    assert "engine down" in out.error


def test_verify_commands_prefers_explicit_callable():
    runner = ClaudioTaskRunner(None, verification_cmd=lambda b: ["make test"])
    assert runner.verify_commands(brief()) == ["make test"]


def test_verify_commands_parses_run_prefix():
    runner = ClaudioTaskRunner(None)
    assert runner.verify_commands(brief(criterion="run: pytest -q tests")) == ["pytest -q tests"]


# -- heartbeat (issue #2) ---------------------------------------------------

def test_write_heartbeat_creates_valid_file(settings):
    from clams.contracts import Heartbeat

    svc = ExecutorService(Store(settings.paths.db_path), InMemoryBus(), settings,
                         task_runner=lambda b, w: None)
    path = svc.write_heartbeat("hello")
    assert path.exists()
    hb = Heartbeat.model_validate_json(path.read_text())
    assert hb.service == "executor"
    assert hb.detail == "hello"


async def test_run_once_refreshes_heartbeat_even_when_idle(settings):
    """The watchdog requires executor.json; an idle poll must still write it."""
    svc = ExecutorService(Store(settings.paths.db_path), InMemoryBus(), settings,
                         task_runner=lambda b, w: None)
    hb = Path(settings.paths.heartbeat_dir) / "executor.json"
    assert not hb.exists()
    await svc.run_once()
    assert hb.exists(), "run_once() must refresh the executor heartbeat"


async def test_run_forever_keeps_beating(settings):
    """A long-lived loop must rewrite the heartbeat, not write it once."""
    import json

    svc = ExecutorService(Store(settings.paths.db_path), InMemoryBus(), settings,
                         task_runner=lambda b, w: None)
    hb = Path(settings.paths.heartbeat_dir) / "executor.json"

    task = asyncio.create_task(svc.run_forever())
    await asyncio.sleep(0.2)
    assert hb.exists()
    first = json.loads(hb.read_text())["timestamp"]
    await asyncio.sleep(1.2)
    second = json.loads(hb.read_text())["timestamp"]
    task.cancel()
    assert first != second, "heartbeat timestamp must advance between beats"
