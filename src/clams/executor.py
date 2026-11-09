"""Headless task executor (spec FR-4, HW-4).

Consumes ``TaskBrief`` messages, runs each one in an isolated git worktree under
a hard wall-clock budget, and publishes an ``ExecutionResult`` carrying the
evidence the review gate needs.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .bus import Bus, publish_result, read_task
from .config import Settings
from .contracts import (
    ArtifactDelta,
    CommandEvidence,
    EnvironmentState,
    Evidence,
    ExecutionResult,
    TaskBrief,
)
from .db import Store
from .gh import GitHubClient, STATUS_BLOCKED
from .worktree import (
    WorktreeError,
    create_worktree,
    diff_summary,
    enforce_scope,
    is_clean,
    remove_worktree,
)

TAIL_CHARS = 2000


class TaskExecutionError(Exception):
    pass


@dataclass
class TaskRunOutcome:
    ok: bool
    commands: list[CommandEvidence] = field(default_factory=list)
    summary: str = ""
    turns: int = 0
    error: str = ""


class ClaudioTaskRunner:
    """Runs a task brief by driving the Executor Claudio instance.

    The brief is rendered into a self-contained instruction; the agent works
    inside the worktree and the verification command is then run by us so the
    evidence is ours, not the model's claim.
    """

    def __init__(self, claudio, verification_cmd: Optional[Callable[[TaskBrief], list[str]]] = None):
        self.claudio = claudio
        self._verification_cmd = verification_cmd

    async def __call__(self, brief: TaskBrief, worktree: Path) -> TaskRunOutcome:
        prompt = render_task_prompt(brief, worktree)
        try:
            resp = await self.claudio.chat(prompt)
        except Exception as exc:  # noqa: BLE001
            return TaskRunOutcome(ok=False, summary="", error=str(exc))
        commands = []
        for cmd in self.verify_commands(brief):
            ev = run_command_logged(cmd, worktree)
            commands.append(ev)
        ok = all(c.exit_code == 0 for c in commands) if commands else True
        return TaskRunOutcome(
            ok=ok,
            commands=commands,
            summary=(resp.final_output or "")[:500],
            turns=1,
        )

    def verify_commands(self, brief: TaskBrief) -> list[str]:
        if self._verification_cmd:
            return self._verification_cmd(brief)
        crit = (brief.acceptance_criterion or "").strip()
        if crit.lower().startswith(("run:", "cmd:")):
            return [crit.split(":", 1)[1].strip()]
        return [crit] if crit else []


def render_task_prompt(brief: TaskBrief, worktree: Path) -> str:
    return (
        f"You are executing task #{brief.task_issue} in the working tree at "
        f"{worktree}.\n\n"
        f"OBJECTIVE\n{brief.objective}\n\n"
        f"ACCEPTANCE CRITERION\n{brief.acceptance_criterion}\n\n"
        f"SCOPE\n"
        f"- You may modify: {', '.join(brief.scope.writable) or '(unspecified)'}\n"
        f"- You may read: {', '.join(brief.scope.read_only) or '(unspecified)'}\n"
        f"- You must NOT touch: {', '.join(brief.scope.forbidden) or '(unspecified)'}\n\n"
        f"Work only inside {worktree}. Do not push. Do not touch other directories.\n"
    )


def run_command_logged(cmd: str, cwd: Path, timeout: float = 600.0) -> CommandEvidence:
    """Run a shell command and capture exit code + stdout tail as evidence."""
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
        )
        out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        return CommandEvidence(cmd=cmd, exit_code=proc.returncode, tail=out[-TAIL_CHARS:])
    except subprocess.TimeoutExpired:
        return CommandEvidence(cmd=cmd, exit_code=124, tail=f"TIMEOUT after {timeout}s")
    except Exception as exc:  # noqa: BLE001
        return CommandEvidence(cmd=cmd, exit_code=127, tail=f"EXEC ERROR: {exc}")


class ExecutorService:
    def __init__(
        self,
        store: Store,
        bus: Bus,
        settings: Settings,
        task_runner: Callable[[TaskBrief, Path], Any],
        gh: Optional[GitHubClient] = None,
        executor_id: Optional[str] = None,
    ):
        self.store = store
        self.bus = bus
        self.settings = settings
        self.task_runner = task_runner
        self.gh = gh
        self.executor_id = executor_id or settings.executor.executor_id

    # -- single task -----------------------------------------------------
    async def process_brief(self, brief: TaskBrief) -> ExecutionResult:
        """Run one task brief end to end and return the result."""
        task_number = brief.task_issue
        max_attempts = self.settings.limits.max_attempts
        max_wall = brief.limits.max_wall_seconds or self.settings.limits.max_wall_seconds

        won = self.store.task_claims.claim(
            task_number, brief.requirement_issue, self.executor_id,
            lease_seconds=self.settings.executor.lease_seconds,
            max_attempts=max_attempts,
        )
        if not won:
            claim = self.store.task_claims.get(task_number)
            holder = claim["executor_id"] if claim else "unknown"
            return ExecutionResult(
                task_issue=task_number,
                status="blocked",
                summary=f"task already claimed by {holder}",
            )

        worktree_path = Path(self.settings.paths.work_root) / f"task-{task_number}"
        started = time.monotonic()
        created = False
        try:
            create_worktree(
                self.settings.paths.repo_path, worktree_path,
                branch=brief.git.work_branch or f"task/{task_number}",
                base_sha=brief.git.base_sha or "HEAD",
            )
            created = True
            outcome = await asyncio.wait_for(
                self._run(brief, worktree_path), timeout=max_wall
            )
        except asyncio.TimeoutError:
            if created:
                remove_worktree(self.settings.paths.repo_path, worktree_path)
            return await self._fail(
                brief, "timeout",
                f"exceeded max_wall_seconds={max_wall}",
                worktree_path=worktree_path if created else None,
            )
        except (WorktreeError, Exception) as exc:  # noqa: BLE001
            if created:
                remove_worktree(self.settings.paths.repo_path, worktree_path)
            return await self._fail(brief, "failed", str(exc),
                                   worktree_path=worktree_path if created else None)

        if not outcome.ok:
            if created:
                remove_worktree(self.settings.paths.repo_path, worktree_path)
            return await self._fail(
                brief, "failed", outcome.error or outcome.summary or "task failed",
                commands=outcome.commands,
            )

        # success: verify scope + tree state, commit, publish evidence
        violations = enforce_scope(
            worktree_path, brief.scope.writable, brief.scope.forbidden
        )
        if violations:
            if created:
                remove_worktree(self.settings.paths.repo_path, worktree_path)
            return await self._fail(
                brief, "failed",
                f"scope violations: {', '.join(violations)}",
                commands=outcome.commands,
            )

        commit = self._commit(worktree_path, task_number)
        diff = diff_summary(worktree_path)
        result = ExecutionResult(
            task_issue=task_number,
            status="completed",
            summary=outcome.summary or "task completed",
            evidence=Evidence(
                commands=outcome.commands,
                log_file=str(worktree_path / "test-output.log"),
            ),
            artifact_delta=ArtifactDelta(
                files_changed=diff.files_changed,
                commit=commit,
                branch=brief.git.work_branch or f"task/{task_number}",
            ),
            environment_state=EnvironmentState(
                git_head=commit,
                working_tree_clean=is_clean(worktree_path),
            ),
        )
        await publish_result(self.bus, result)
        self.store.task_claims.release(task_number)
        if created:
            remove_worktree(self.settings.paths.repo_path, worktree_path)
        return result

    async def _run(self, brief: TaskBrief, worktree_path: Path) -> TaskRunOutcome:
        out = self.task_runner(brief, worktree_path)
        if asyncio.iscoroutine(out):
            out = await out
        if not isinstance(out, TaskRunOutcome):
            raise TaskExecutionError(f"task runner returned {type(out)!r}")
        return out

    async def _fail(self, brief: TaskBrief, status: str, reason: str,
                   commands: Optional[list[CommandEvidence]] = None,
                   worktree_path: Optional[Path] = None) -> ExecutionResult:
        attempts = self.store.task_claims.increment_attempts(brief.task_issue)
        result = ExecutionResult(
            task_issue=brief.task_issue,
            status="failed" if status != "timeout" else "timeout",
            summary=reason[:500],
            evidence=Evidence(commands=commands or []),
        )
        await publish_result(self.bus, result)
        if attempts >= self.settings.limits.max_attempts:
            # Keep the row (with attempts at the cap) so the task can never be
            # re-claimed; only the lease is released.
            self.store.task_claims.expire(brief.task_issue)
            if self.gh is not None:
                try:
                    await self.gh.set_status(brief.task_issue, STATUS_BLOCKED)
                except Exception:  # noqa: BLE001
                    pass
            self.store.alerts.add(
                "critical", "executor",
                f"task #{brief.task_issue} exhausted {attempts} attempts: {reason}",
            )
        else:
            # Expire the lease so another (or the same) executor can retry.
            self.store.task_claims.expire(brief.task_issue)
        return result

    def _commit(self, worktree_path: Path, task_number: int) -> str:
        subprocess.run(["git", "add", "-A"], cwd=str(worktree_path),
                      capture_output=True, check=False)
        subprocess.run(
            ["git", "commit", "-m", f"task #{task_number}"],
            cwd=str(worktree_path), capture_output=True, check=False,
        )
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(worktree_path),
                             capture_output=True, text=True).stdout.strip()

    # -- loop -----------------------------------------------------------
    async def run_once(self) -> Optional[ExecutionResult]:
        # Refresh the heartbeat on every poll so an idle-but-healthy executor is
        # still distinguishable from a dead one.
        self.write_heartbeat("poll")
        brief = await read_task(self.bus, "executors", self.executor_id,
                               count=1, block_ms=1000)
        if brief is None:
            return None
        return await self.process_brief(brief)

    async def run_forever(self) -> None:
        """Poll for tasks indefinitely, keeping the heartbeat fresh.

        A background task refreshes the heartbeat on ``heartbeat_interval`` so
        the watchdog sees a live executor even while a long-running task is in
        flight (a task may legitimately run for ``max_wall_seconds``).
        """
        import asyncio

        stop = asyncio.Event()

        async def beat() -> None:
            while not stop.is_set():
                try:
                    self.write_heartbeat("loop")
                except Exception:  # noqa: BLE001 - heartbeat must never kill the loop
                    pass
                try:
                    await asyncio.wait_for(stop.wait(),
                                         timeout=self.settings.executor.heartbeat_interval)
                except asyncio.TimeoutError:
                    pass

        hb_task = asyncio.create_task(beat())
        try:
            while True:
                try:
                    await self.run_once()
                except Exception as exc:  # noqa: BLE001 - keep the loop alive
                    print(f"executor cycle error: {exc}")
                await asyncio.sleep(self.settings.executor.poll_interval)
        finally:
            stop.set()
            await hb_task

    def write_heartbeat(self, detail: str = "") -> Path:
        from datetime import datetime, timezone

        from .contracts import Heartbeat

        d = Path(self.settings.paths.heartbeat_dir)
        d.mkdir(parents=True, exist_ok=True)
        hb = Heartbeat(
            service="executor",
            pid=os.getpid(),
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            detail=detail,
        )
        path = d / "executor.json"
        path.write_text(hb.to_json())
        return path
