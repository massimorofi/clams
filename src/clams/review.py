"""Verification & evidence gate (spec FR-5).

A requirement may only reach ``Review`` when there is *real* execution evidence:
commands that actually ran and exited 0, a non-empty output, a commit, and a
clean tree. Evidence is re-checked from the stored payload — the executor's own
claim of success is never taken on faith.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import Settings
from .contracts import ExecutionResult
from .db import Store
from .gh import (
    GitHubClient,
    STATUS_BLOCKED,
    STATUS_DONE,
    STATUS_REVIEW,
)


@dataclass
class GateVerdict:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    detail: str = ""

    def summary(self) -> str:
        if self.passed:
            return "evidence gate PASSED"
        return "evidence gate FAILED: " + "; ".join(self.reasons)


def verify_evidence(result: ExecutionResult) -> GateVerdict:
    """Check one task's evidence for the properties the gate requires."""
    reasons: list[str] = []

    if result.status != "completed":
        reasons.append(f"task status is {result.status!r}, not 'completed'")

    commands = result.evidence.commands
    if not commands:
        reasons.append("no commands were executed")
    else:
        failing = [c for c in commands if c.exit_code != 0]
        if failing:
            detail = ", ".join(f"{c.cmd!r} exit {c.exit_code}" for c in failing)
            reasons.append(f"non-zero exit code: {detail}")
        empty = [c for c in commands if not (c.tail or "").strip()]
        if empty:
            reasons.append(
                "empty stdout: " + ", ".join(repr(c.cmd) for c in empty)
            )

    if not (result.artifact_delta.commit or "").strip():
        reasons.append("no commit recorded")

    if not result.artifact_delta.files_changed:
        reasons.append("no files changed")

    if not result.environment_state.working_tree_clean:
        reasons.append("working tree is not clean")

    return GateVerdict(passed=not reasons, reasons=reasons)


class ReviewService:
    """Aggregates task evidence and drives the requirement status transition."""

    def __init__(
        self,
        store: Store,
        gh: GitHubClient,
        settings: Settings,
        judge=None,
    ):
        self.store = store
        self.gh = gh
        self.settings = settings
        # ``judge`` is an optional planner-side semantic check
        # (callable(requirement_number, evidence_summary) -> bool).
        self.judge = judge

    # -- single task -----------------------------------------------------
    async def record_task_result(self, result: ExecutionResult) -> GateVerdict:
        """Verify one task result and post the evidence to its GitHub issue."""
        verdict = verify_evidence(result)
        await self.gh.add_comment(
            result.task_issue, format_evidence_comment(result, verdict)
        )
        if verdict.passed:
            await self.gh.set_status(result.task_issue, STATUS_DONE)
        else:
            await self.gh.set_status(result.task_issue, STATUS_BLOCKED)
        return verdict

    # -- whole requirement ----------------------------------------------
    async def review_requirement(self, requirement_number: int) -> GateVerdict:
        """Gate a requirement on the evidence of all of its sub-tasks.

        Reads the evidence back out of the local DB rather than trusting whatever
        the caller hands over, so a stale or forged argument cannot slip through.
        """
        item = self.store.work_items.get(requirement_number)
        if item is None:
            reason = f"requirement #{requirement_number} not tracked"
            await self.gh.add_comment(
                requirement_number,
                format_requirement_comment(requirement_number, {}, passed=False,
                                         reasons=[reason]),
            )
            await self.gh.set_status(requirement_number, STATUS_BLOCKED)
            return GateVerdict(False, [reason])
        if not item.task_numbers:
            reason = "requirement has no sub-tasks"
            await self.gh.add_comment(
                requirement_number,
                format_requirement_comment(requirement_number, {}, passed=False,
                                         reasons=[reason]),
            )
            await self.gh.set_status(requirement_number, STATUS_BLOCKED)
            return GateVerdict(False, [reason])

        results = self._load_results(requirement_number)
        if len(results) < len(item.task_numbers):
            missing = sorted(set(item.task_numbers) - set(results))
            reason = f"missing evidence for task(s): {missing}"
            await self.gh.add_comment(
                requirement_number,
                format_requirement_comment(requirement_number, results,
                                         passed=False, reasons=[reason]),
            )
            await self.gh.set_status(requirement_number, STATUS_BLOCKED)
            self.store.work_items.set_status(requirement_number, "blocked")
            return GateVerdict(False, [reason])

        reasons: list[str] = []
        for task_number, result in results.items():
            verdict = verify_evidence(result)
            if not verdict.passed:
                reasons.append(f"task #{task_number}: " + "; ".join(verdict.reasons))

        if reasons:
            await self.gh.add_comment(
                requirement_number,
                format_requirement_comment(requirement_number, results, passed=False,
                                        reasons=reasons),
            )
            await self.gh.set_status(requirement_number, STATUS_BLOCKED)
            self.store.work_items.set_status(requirement_number, "blocked")
            return GateVerdict(False, reasons)

        if self.judge is not None:
            summary = self._evidence_summary(results)
            ok = await self.judge(requirement_number, summary)
            if not ok:
                reason = "planner judgement rejected the evidence"
                await self.gh.add_comment(
                    requirement_number,
                    format_requirement_comment(requirement_number, results,
                                             passed=False, reasons=[reason]),
                )
                await self.gh.set_status(requirement_number, STATUS_BLOCKED)
                self.store.work_items.set_status(requirement_number, "blocked")
                return GateVerdict(False, [reason])

        await self.gh.add_comment(
            requirement_number,
            format_requirement_comment(requirement_number, results, passed=True,
                                     reasons=[]),
        )
        await self.gh.set_status(requirement_number, STATUS_REVIEW)
        self.store.work_items.set_status(requirement_number, "review")
        return GateVerdict(True, detail=f"{len(results)} task(s) verified")

    # -- helpers --------------------------------------------------------
    def _load_results(self, requirement_number: int) -> dict[int, ExecutionResult]:
        """Load the latest verified result per task from the outbox payloads."""
        rows = self.store.db.conn.execute(
            "SELECT id, payload FROM outbox WHERE kind = 'gh_comment' ORDER BY id"
        ).fetchall()
        out: dict[int, ExecutionResult] = {}
        for row in rows:
            payload = json.loads(row["payload"])
            raw = payload.get("result")
            if not raw:
                continue
            try:
                res = ExecutionResult.model_validate(raw)
            except Exception:
                continue
            item = self.store.work_items.get(requirement_number)
            if item and res.task_issue in item.task_numbers:
                out[res.task_issue] = res
        return out

    def _evidence_summary(self, results: dict[int, ExecutionResult]) -> str:
        lines = []
        for task_number in sorted(results):
            r = results[task_number]
            cmds = "; ".join(f"{c.cmd} -> {c.exit_code}" for c in r.evidence.commands)
            lines.append(f"task #{task_number}: {r.summary} [{cmds}]")
        return "\n".join(lines)


# ---- comment formatting ---------------------------------------------

def format_evidence_comment(result: ExecutionResult, verdict: GateVerdict) -> str:
    lines = ["**[clams] execution evidence**", ""]
    lines.append(f"Task: #{result.task_issue}")
    lines.append(f"Status: `{result.status}`")
    lines.append("")
    lines.append("### Commands")
    if result.evidence.commands:
        for c in result.evidence.commands:
            mark = "✅" if c.exit_code == 0 else "❌"
            lines.append(f"- {mark} `{c.cmd}` → exit `{c.exit_code}`")
            if c.tail:
                lines.append("  ```")
                lines.append("  " + c.tail.strip().replace("\n", "\n  "))
                lines.append("  ```")
    else:
        lines.append("_no commands recorded_")
    lines.append("")
    lines.append("### Diff")
    delta = result.artifact_delta
    lines.append(f"- commit: `{delta.commit or '(none)'}`")
    lines.append(f"- branch: `{delta.branch or '(none)'}`")
    lines.append(f"- files changed: {len(delta.files_changed)}")
    for f in delta.files_changed[:30]:
        lines.append(f"  - `{f}`")
    lines.append("")
    lines.append("### Verdict")
    if verdict.passed:
        lines.append("✅ Evidence gate passed.")
    else:
        lines.append("❌ Evidence gate failed:")
        for r in verdict.reasons:
            lines.append(f"- {r}")
    return "\n".join(lines)


def format_requirement_comment(
    requirement_number: int,
    results: dict[int, ExecutionResult],
    passed: bool,
    reasons: list[str],
) -> str:
    lines = [f"**[clams] requirement #{requirement_number} verification**", ""]
    if passed:
        lines.append(f"✅ All {len(results)} sub-task(s) produced passing evidence.")
        lines.append("Moving to `Review`.")
    else:
        lines.append("❌ Verification failed:")
        for r in reasons:
            lines.append(f"- {r}")
    return "\n".join(lines)
