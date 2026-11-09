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

from .bus import Bus, publish_task, read_command, read_result
from .config import Settings
from .contracts import ExecutionResult, GitRef, Scope, TaskBrief, TaskLimits
from .db import Store, WorkItem
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
        self.outbox = OutboxWorker(store, gh=gh, settings=settings)
        self._stop = asyncio.Event()

    # -- lifecycle -----------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
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
        for step in (
            self.step_commands,
            self.step_intake,
            self.step_plan,
            self.step_dispatch,
            self.step_review_results,
            self.step_outbox,
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
            cmd = await read_command(self.bus, "planner", "orchestrator",
                                   count=1, block_ms=50)
            if cmd is None:
                break
            await self.steering.apply(cmd)
            report.commands += 1

    async def step_intake(self, report: CycleReport) -> None:
        if self.intake is None:
            return
        result = await self.intake.sweep()
        report.intake.extend(result.claimed)

    async def step_plan(self, report: CycleReport) -> None:
        if self.planner is None:
            return
        for item in self.store.work_items.list_by_status("claimed"):
            if item.user_paused:
                continue
            await self.planner.plan_requirement(item.requirement_number)
            report.planned.append(item.requirement_number)

    async def step_dispatch(self, report: CycleReport) -> None:
        """Publish a TaskBrief for every planned task not yet dispatched."""
        already = self.dispatched_tasks()
        for item in self.store.work_items.list_by_status("planned"):
            if item.user_paused:
                continue
            for task_number in item.task_numbers:
                if task_number in already:
                    continue
                await publish_task(self.bus, self._brief_for(item, task_number))
                # Record the dispatch as an outbox side-effect so it survives a
                # crash and is never replayed as a second brief.
                self.store.outbox.enqueue(
                    "notification",
                    {"type": "dispatch", "task": task_number,
                     "requirement": item.requirement_number},
                    idempotency_key=f"{DISPATCH_PREFIX}{task_number}",
                )
                already.add(task_number)
                report.dispatched.append(task_number)
            if item.task_numbers:
                self.store.work_items.set_status(item.requirement_number, "dispatched")
        return report

    def dispatched_tasks(self) -> set[int]:
        rows = self.store.db.conn.execute(
            "SELECT payload FROM outbox WHERE kind = 'notification'"
        ).fetchall()
        out: set[int] = set()
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (json.JSONDecodeError, TypeError):
                continue
            if payload.get("type") == "dispatch" and payload.get("task") is not None:
                out.add(int(payload["task"]))
        return out

    def _brief_for(self, item: WorkItem, task_number: int) -> TaskBrief:
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
            git=GitRef(base_branch="main", base_sha="", work_branch=f"task/{task_number}"),
            limits=TaskLimits(
                max_turns=self.settings.limits.max_turns,
                max_wall_seconds=self.settings.limits.max_wall_seconds,
            ),
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
        """Consume executor results and gate the requirement when all reported."""
        while True:
            result = await read_result(self.bus, "planner", "orchestrator",
                                      count=1, block_ms=50)
            if result is None:
                break
            self.record_result(result)
            req = self.requirement_for_task(result.task_issue)
            if req is None:
                continue
            if self.review is not None and self.all_tasks_reported(req):
                await self.review.review_requirement(req)
                report.reviewed.append(req)

    def record_result(self, result: ExecutionResult) -> None:
        self.store.outbox.enqueue(
            "gh_comment",
            {
                "issue": result.task_issue,
                "body": f"result: {result.status}",
                "result": json.loads(result.to_json()),
            },
            idempotency_key=f"result-{result.task_issue}",
        )

    def requirement_for_task(self, task_number: int) -> Optional[int]:
        for item in self.store.work_items.all():
            if task_number in item.task_numbers:
                return item.requirement_number
        return None

    def all_tasks_reported(self, requirement_number: int) -> bool:
        item = self.store.work_items.get(requirement_number)
        if not item or not item.task_numbers:
            return False
        reported = self.reported_tasks()
        return all(t in reported for t in item.task_numbers)

    def reported_tasks(self) -> set[int]:
        rows = self.store.db.conn.execute(
            "SELECT payload FROM outbox WHERE kind = 'gh_comment'"
        ).fetchall()
        out: set[int] = set()
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (json.JSONDecodeError, TypeError):
                continue
            raw = payload.get("result")
            if isinstance(raw, dict) and raw.get("task_issue") is not None:
                out.add(int(raw["task_issue"]))
        return out

    async def step_outbox(self, report: CycleReport) -> None:
        delivered = await self.outbox.deliver_once()
        report.delivered = delivered.delivered

    async def step_heartbeat(self, report: CycleReport) -> None:
        """Write heartbeats the watchdog looks for.

        The orchestrator loop runs inside the Planner container, so it publishes
        the ``planner`` heartbeat on the planner's behalf as well as its own.
        """
        hb_dir = Path(self.settings.paths.heartbeat_dir)
        hb_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "timestamp": _now_iso(),
            "detail": report.summary(),
        }
        for name in ("orchestrator", "planner"):
            (hb_dir / f"{name}.json").write_text(json.dumps({**payload, "service": name}))
