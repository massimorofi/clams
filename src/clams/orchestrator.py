"""Orchestrator: the background loop wiring every service together (spec §1.1).

One iteration:
  1. drain user commands (highest priority — spec §8.1)
  2. intake sweep (Ready -> claimed)
  3. plan any claimed-but-unplanned requirement
  4. dispatch planned tasks to the executor queue
  5. consume executor results and run the evidence gate
  6. deliver the outbox
  7. heartbeat

Each step is independently guarded: one failing step never stops the loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .bus import (Bus, publish_result, publish_task, redeliver_results,
                  take_command, take_result)
from .config import Settings
from .contracts import ExecutionResult, GitRef, Scope, TaskBrief, TaskLimits
from .db import Store, WorkItem, parse_iso
from .outbox import OutboxWorker

DISPATCH_PREFIX = "dispatch-task-"

_log = logging.getLogger("clams.orchestrator")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class CycleReport:
    commands: int = 0
    intake: list[int] = field(default_factory=list)
    planned: list[int] = field(default_factory=list)
    dispatched: list[int] = field(default_factory=list)
    reviewed: list[int] = field(default_factory=list)
    delivered: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"commands={self.commands} intake={len(self.intake)} "
            f"planned={len(self.planned)} dispatched={len(self.dispatched)} "
            f"reviewed={len(self.reviewed)} delivered={self.delivered} "
            f"errors={len(self.errors)}"
        )


@dataclass
class ProjectContext:
    """One project's slice of the pipeline (issue #18).

    Each registered project gets its own GitHub client, intake, planner and
    review service, plus the provisioned working tree its tasks run against.
    The orchestrator iterates these so every project is worked independently.
    """

    project_id: int
    gh: Any
    intake: Any
    planner: Any
    review: Any
    worktree_path: str
    default_branch: str = "main"


class Orchestrator:
    def __init__(
        self,
        store: Store,
        bus: Bus,
        settings: Settings,
        gh=None,
        planner=None,
        intake=None,
        steering=None,
        review=None,
        watchdog=None,
        project_contexts: Optional[list] = None,
        default_project_id: int = 0,
    ):
        self.store = store
        self.bus = bus
        self.settings = settings
        self.gh = gh
        self.planner = planner
        self.intake = intake
        self.steering = steering
        self.review = review
        self.watchdog = watchdog
        # Multi-project support (issue #18). The single intake/planner/review
        # above serve the *default* project (``default_project_id``); any
        # additional registered projects arrive as ``project_contexts``.
        self.project_contexts = list(project_contexts or [])
        self.default_project_id = default_project_id
        self._stop = asyncio.Event()
        self.outbox = OutboxWorker(store, gh=gh, settings=settings,
                                   gh_resolver=self._gh_for_project)

    def _gh_for_project(self, project_id: int):
        """The GitHub client that owns ``project_id`` (issue #18).

        Outbox side-effects must be applied to the repo the work item came
        from, not always the default repo.
        """
        if project_id == self.default_project_id or not self.project_contexts:
            return self.gh
        for ctx in self.project_contexts:
            if ctx.project_id == project_id:
                return ctx.gh
        return self.gh

    # -- lifecycle -----------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        try:
            recovered = await self.recover_pending_results()
            if recovered:
                _log.info("re-recorded %s unacknowledged result(s) from a "
                          "previous run", recovered)
        except Exception as exc:  # noqa: BLE001 - recovery must not block startup
            _log.warning("result recovery failed: %s", exc)
        while not self._stop.is_set():
            await self.cycle()
            try:
                await asyncio.wait_for(self._stop.wait(),
                                     timeout=self.settings.poll_interval)
            except asyncio.TimeoutError:
                pass

    # -- one cycle -----------------------------------------------------
    async def cycle(self) -> CycleReport:
        report = CycleReport()
        # Beat BEFORE the steps, not only after them. Planning and review make
        # LLM calls that can take minutes; with the heartbeat written only at the
        # end of a cycle, a busy-but-healthy orchestrator looked dead, the
        # watchdog paused dispatching and raised criticals until the cycle
        # finally finished.
        self._write_heartbeat("cycle start")
        for step in (
            self.step_commands,
            self.step_intake,
            self.step_plan,
            self.step_dispatch,
            self.step_reap_stale,
            self.step_review_results,
            self.step_outbox,
            self.step_projects,
            self.step_heartbeat,
        ):
            try:
                await step(report)
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"{step.__name__}: {exc}")
                _log.warning("%s failed: %s", step.__name__, exc)
        _log.info("cycle done: %s", report.summary())
        return report

    # -- steps ---------------------------------------------------------
    async def step_commands(self, report: CycleReport) -> None:
        """Drain user steering commands FIRST (interrupt priority)."""
        if self.steering is None:
            return
        while True:
            delivery = await take_command(self.bus, "planner", "orchestrator",
                                        count=1, block_ms=50)
            if delivery is None:
                break
            try:
                await self.steering.apply(delivery.item)
            finally:
                await delivery.ack()
            report.commands += 1

    def project_paused(self, project_id: int) -> bool:
        """True when the operator paused this project (issue #123).

        ``POST /api/v1/projects/{id}/pause`` only wrote a flag nothing read, so
        intake, planning and dispatch kept running for a project the operator
        had explicitly stopped.
        """
        if not project_id:
            return False
        row = self.store.projects.get(project_id)
        return bool(row and row["paused"])

    async def step_intake(self, report: CycleReport) -> None:
        # The default project's intake (single-repo path) plus every additional
        # registered project's intake (issue #18).
        if self.intake is not None and not self.project_paused(self.default_project_id):
            result = await self.intake.sweep()
            report.intake.extend(result.claimed)
        for ctx in self.project_contexts:
            if ctx.intake is None or self.project_paused(ctx.project_id):
                continue
            result = await ctx.intake.sweep()
            report.intake.extend(result.claimed)

    async def step_plan(self, report: CycleReport) -> None:
        # Default project first (single-repo path), then each additional project
        # with its own planner bound to that project's repo (issue #18).
        if self.planner is not None and not self.project_paused(self.default_project_id):
            for item in self.store.work_items.list_by_status(
                    "claimed", self.default_project_id):
                if item.user_paused:
                    continue
                await self._plan_one(self.planner, item, report)
        for ctx in self.project_contexts:
            if ctx.planner is None or self.project_paused(ctx.project_id):
                continue
            for item in self.store.work_items.list_by_status(
                    "claimed", ctx.project_id):
                if item.user_paused:
                    continue
                await self._plan_one(ctx.planner, item, report)

    async def _plan_one(self, planner, item, report: CycleReport) -> None:
        """Plan one requirement, giving up permanently once it is proven unplannable.

        A requirement the decomposer rejects (e.g. it is too small to split into
        ``MIN_TASKS`` tasks) used to fail identically on every cycle forever:
        two LLM calls every ``poll_interval``, an error line each time, and the
        item stuck in ``claimed``. Bounded by ``limits.max_replans`` now, after
        which it is marked blocked and escalated once.
        """
        try:
            await planner.plan_requirement(item.requirement_number)
            report.planned.append(item.requirement_number)
            return
        except Exception as exc:  # noqa: BLE001 - one bad plan must not stall the cycle
            replans = self.store.work_items.increment_replans(
                item.requirement_number, item.project_id)
            cap = self.settings.limits.max_replans
            if replans < cap:
                _log.warning("planning #%s failed (%s/%s): %s",
                             item.requirement_number, replans, cap, exc)
                report.errors.append(f"plan #{item.requirement_number}: {exc}")
                return
            self.store.work_items.set_status(
                item.requirement_number, "blocked", item.project_id)
            msg = (f"Requirement #{item.requirement_number} could not be planned "
                   f"after {replans} attempts: {exc}")
            _log.error(msg)
            report.errors.append(msg)
            self.store.alerts.add("warning", "planner", msg)

    def _default_scope(self) -> tuple[int, str, str]:
        """``(project_id, worktree_path, base_branch)`` for the default project.

        Issue #115: this used to be ``settings.paths.repo_path`` — the
        orchestrator's own checkout. The default project is a *registered*
        project like any other, so its tasks must run against the working tree
        provisioned for it, never against the tree Clams happens to live in.
        Only the legacy single-repo mode (no registered default project,
        ``default_project_id == 0``) keeps the configured repo path.
        """
        row = None
        if self.default_project_id:
            row = self.store.projects.get(self.default_project_id)
        if row is None:
            return (self.default_project_id, self.settings.paths.repo_path,
                    "main")
        return (self.default_project_id, row["worktree_path"] or "",
                row["default_branch"] or "main")

    def dispatch_scopes(self) -> list[tuple[int, str, str]]:
        """Every project's ``(id, worktree_path, base_branch)`` dispatch scope."""
        scopes = [self._default_scope()]
        for ctx in self.project_contexts:
            scopes.append((ctx.project_id, ctx.worktree_path,
                          ctx.default_branch or "main"))
        return scopes

    async def step_dispatch(self, report: CycleReport) -> None:
        """Publish a TaskBrief for every planned task not yet dispatched.

        Dispatch is project-scoped: each project's tasks are published with
        that project's working tree as the git base, and the dedup key carries
        the project id so two projects sharing a task number never collide.

        A project with no provisioned working tree is *skipped* rather than
        dispatched against the orchestrator's checkout (#115): running another
        repo's tasks inside the Clams tree produced confident nonsense.
        """
        already = self.dispatched_tasks()
        for project_id, worktree_path, base_branch in self.dispatch_scopes():
            if self.project_paused(project_id):
                continue          # operator paused this project (#123)
            if not worktree_path:
                if self.store.work_items.list_by_status("planned", project_id):
                    msg = (f"project {project_id} has planned work but no "
                           f"provisioned working tree; dispatch skipped")
                    report.errors.append(msg)
                    self.store.alerts.add("warning", "orchestrator", msg)
                continue
            for item in self.store.work_items.list_by_status(
                    "planned", project_id):
                if item.user_paused:
                    continue
                for task_number in item.task_numbers:
                    if (project_id, task_number) in already:
                        continue
                    await publish_task(
                        self.bus,
                        self._brief_for(item, task_number,
                                       worktree_path=worktree_path,
                                       base_branch=base_branch),
                    )
                    # Record the dispatch as an outbox side-effect so it
                    # survives a crash and is never replayed as a second brief.
                    self.store.outbox.enqueue(
                        "notification",
                        {"type": "dispatch", "task": task_number,
                         "requirement": item.requirement_number,
                         "project_id": project_id},
                        idempotency_key=(
                            f"{DISPATCH_PREFIX}{project_id}-{task_number}"),
                    )
                    already.add((project_id, task_number))
                    report.dispatched.append(task_number)
                if item.task_numbers:
                    self.store.work_items.set_status(
                        item.requirement_number, "dispatched", project_id)
        return report

    # -- stale task reaping (issue #117) ---------------------------------
    def _is_stale(self, claim, now) -> bool:
        """True when a claimed task's lease has expired (worker died mid-run)."""
        if claim is None:
            return False
        return parse_iso(claim["lease_expires_at"]) <= now

    def _dispatched_long_ago(self, item, now, grace_seconds: float) -> bool:
        """True when a task was dispatched but never claimed, past the grace."""
        updated = getattr(item, "updated_at", "") or ""
        try:
            then = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return False
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return (now - then).total_seconds() > grace_seconds

    async def step_reap_stale(self, report: CycleReport) -> None:
        """Re-dispatch tasks whose lease expired with no result (issue #117).

        ``step_dispatch`` only looks at items in ``planned``, so once a
        requirement flipped to ``dispatched`` nothing ever revisited it: a task
        that died left ``attempts`` at 0, the ``max_attempts`` cap was never
        reached and ``status:Blocked`` was never applied. Requirements hung in
        ``dispatched`` forever, which is half of "statuses are not tracked".
        """
        now = datetime.now(timezone.utc)
        max_attempts = self.settings.limits.max_attempts
        no_claim_grace = (self.settings.executor.lease_seconds
                          + self.settings.limits.max_wall_seconds
                          + self.settings.limits.stale_task_grace_seconds)
        for project_id, worktree_path, base_branch in self.dispatch_scopes():
            if self.project_paused(project_id):
                continue
            if not worktree_path:
                continue
            reported = self.reported_tasks(project_id)
            for item in self.store.work_items.list_by_status(
                    "dispatched", project_id):
                if item.user_paused:
                    continue
                for task_number in item.task_numbers:
                    if task_number in reported:
                        continue
                    claim = self.store.task_claims.get(task_number, project_id)
                    if claim is not None and not self._is_stale(claim, now):
                        continue          # still running: leave it alone
                    if claim is None and not self._dispatched_long_ago(
                            item, now, no_claim_grace):
                        continue          # may still be queued: not lost yet
                    attempts = self.store.task_claims.register_abandoned_attempt(
                        task_number, item.requirement_number,
                        "orchestrator-reaper", max_attempts, project_id)
                    if attempts >= max_attempts:
                        # Give up honestly: publish a failure so the review gate
                        # sees a complete task set and marks the requirement.
                        await publish_result(self.bus, ExecutionResult(
                            task_issue=task_number,
                            status="failed",
                            summary=(f"abandoned: lease expired with no result "
                                     f"after {attempts} attempts"),
                            project_id=project_id,
                        ))
                        self.store.alerts.add(
                            "warning", "orchestrator",
                            f"task #{task_number} abandoned after {attempts} "
                            f"attempts (requirement #{item.requirement_number})",
                        )
                        continue
                    await publish_task(self.bus, self._brief_for(
                        item, task_number, worktree_path=worktree_path,
                        base_branch=base_branch))
                    self.store.outbox.enqueue(
                        "notification",
                        {"type": "dispatch", "task": task_number,
                         "requirement": item.requirement_number,
                         "project_id": project_id, "redispatch": attempts},
                        idempotency_key=(f"redispatch-task-{project_id}-"
                                         f"{task_number}-{attempts}"),
                    )
                    report.dispatched.append(task_number)
                    _log.info("re-dispatched task #%s (attempt %s/%s) for #%s",
                              task_number, attempts, max_attempts,
                              item.requirement_number)
        return report

    def dispatched_tasks(self) -> set:
        """Set of ``(project_id, task_number)`` already dispatched.

        Old payloads written before issue #18 carry no ``project_id``; they
        are read as project 0 so an upgrade never re-dispatches existing work.
        """
        rows = self.store.db.conn.execute(
            "SELECT payload FROM outbox WHERE kind = 'notification'"
        ).fetchall()
        out: set = set()
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (json.JSONDecodeError, TypeError):
                continue
            if payload.get("type") == "dispatch" and payload.get("task") is not None:
                out.add((int(payload.get("project_id", 0)), int(payload["task"])))
        return out

    def _brief_for(self, item: WorkItem, task_number: int, *,
                  worktree_path: str = "", base_branch: str = "main") -> TaskBrief:
        spec = self._spec_for(item, task_number)
        return TaskBrief(
            task_issue=task_number,
            requirement_issue=item.requirement_number,
            objective=spec.get("objective") or item.requirement_title,
            acceptance_criterion=spec.get("acceptance_criterion") or "",
            scope=Scope(
                writable=spec.get("writable", []),
                read_only=spec.get("read_only", []),
                forbidden=spec.get("forbidden", []),
            ),
            git=GitRef(base_branch=base_branch, base_sha="",
                       work_branch=f"task/{task_number}",
                       repo_path=worktree_path),
            limits=TaskLimits(
                max_turns=self.settings.limits.max_turns,
                max_wall_seconds=self.settings.limits.max_wall_seconds,
            ),
            project_id=item.project_id,
        )

    def _spec_for(self, item: WorkItem, task_number: int) -> dict[str, Any]:
        if not item.plan_json:
            return {}
        try:
            plan = json.loads(item.plan_json)
        except (json.JSONDecodeError, TypeError):
            return {}
        tasks = plan.get("tasks", [])
        if task_number not in item.task_numbers:
            return {}
        idx = item.task_numbers.index(task_number)
        if idx >= len(tasks):
            return {}
        return tasks[idx] or {}

    async def step_review_results(self, report: CycleReport) -> None:
        """Consume executor results and gate the requirement when all reported.

        The review is routed to the project the result belongs to (issue #18),
        so each project's evidence is posted with that project's GitHub client.

        The result is acked only after it has been recorded in the outbox, so a
        crash between "read" and "record" redelivers it instead of dropping it
        (#114).
        """
        while True:
            delivery = await take_result(self.bus, "planner", "orchestrator",
                                        count=1, block_ms=50)
            if delivery is None:
                break
            result = delivery.item
            try:
                self.record_result(result)
                project_id = getattr(result, "project_id", 0) or 0
                req = self.requirement_for_task(result.task_issue, project_id)
                if req is not None:
                    review = self._review_for_project(project_id)
                    if review is not None and self.all_tasks_reported(req, project_id):
                        await review.review_requirement(req)
                        report.reviewed.append(req)
            finally:
                await delivery.ack()

    async def recover_pending_results(self) -> int:
        """Re-take results this orchestrator claimed but never recorded (#114)."""
        deliveries = await redeliver_results(self.bus, "planner", "orchestrator")
        for delivery in deliveries:
            try:
                self.record_result(delivery.item)
            finally:
                await delivery.ack()
        return len(deliveries)

    def _review_for_project(self, project_id: int):
        """Return the review service bound to ``project_id``.

        The default project uses the orchestrator's own ``review``; additional
        projects use the review carried on their context.
        """
        if project_id == self.default_project_id:
            return self.review
        for ctx in self.project_contexts:
            if ctx.project_id == project_id:
                return ctx.review
        return None

    def record_result(self, result: ExecutionResult) -> None:
        # The status alone ("result: failed") told an operator nothing, so every
        # failure needed a dig through the DB and process logs. Carry the
        # executor's own summary and the verification commands alongside it.
        lines = [f"**[clams] task #{result.task_issue} {result.status}**"]
        summary = (getattr(result, "summary", "") or "").strip()
        if summary:
            lines += ["", summary[:1500]]
        commands = getattr(getattr(result, "evidence", None), "commands", []) or []
        if commands:
            lines += ["", "Commands:"]
            for cmd in commands:
                lines.append(f"- `{cmd.cmd}` -> exit `{cmd.exit_code}`")
        self.store.outbox.enqueue(
            "gh_comment",
            {
                "issue": result.task_issue,
                "body": "\n".join(lines),
                "result": json.loads(result.to_json()),
                "project_id": getattr(result, "project_id", 0) or 0,
            },
            idempotency_key=f"result-{result.project_id}-{result.task_issue}",
        )

    def requirement_for_task(self, task_number: int,
                            project_id: int = 0) -> Optional[int]:
        for item in self.store.work_items.all():
            if item.project_id != project_id:
                continue
            if task_number in item.task_numbers:
                return item.requirement_number
        return None

    def all_tasks_reported(self, requirement_number: int,
                          project_id: int = 0) -> bool:
        item = self.store.work_items.get(requirement_number, project_id)
        if not item or not item.task_numbers:
            return False
        reported = self.reported_tasks(project_id)
        return all(t in reported for t in item.task_numbers)

    def reported_tasks(self, project_id: int = 0) -> set:
        rows = self.store.db.conn.execute(
            "SELECT payload FROM outbox WHERE kind = 'gh_comment'"
        ).fetchall()
        out: set = set()
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (json.JSONDecodeError, TypeError):
                continue
            raw = payload.get("result")
            if isinstance(raw, dict) and raw.get("task_issue") is not None:
                if int(raw.get("project_id", 0) or 0) == project_id:
                    out.add(int(raw["task_issue"]))
        return out

    async def step_outbox(self, report: CycleReport) -> None:
        delivered = await self.outbox.deliver_once()
        report.delivered = delivered.delivered

    async def step_projects(self, report: CycleReport) -> None:
        """Recompute every registered project's status + progress (issue #18).

        Runs after the work-plane steps so the derived state reflects the
        transitions that just happened this cycle.
        """
        from .project_state import refresh_all_project_statuses
        refresh_all_project_statuses(self.store)

    def _write_heartbeat(self, detail: str) -> None:
        """Write the orchestrator + planner heartbeat files.

        The orchestrator loop runs inside the Planner container, so it publishes
        the ``planner`` heartbeat on the planner's behalf as well as its own.
        """
        hb_dir = Path(self.settings.paths.heartbeat_dir)
        hb_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "timestamp": _now_iso(),
            "detail": detail,
        }
        for name in ("orchestrator", "planner"):
            (hb_dir / f"{name}.json").write_text(json.dumps({**payload, "service": name}))

    async def step_heartbeat(self, report: CycleReport) -> None:
        """Write heartbeats the watchdog looks for (see :meth:`_write_heartbeat`)."""
        self._write_heartbeat(report.summary())
