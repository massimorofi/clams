"""Planner service: planning, re-planning and review judgement (spec FR-1/FR-3).

The Planner is a Claudio instance that hosts the Web UI. This service wraps the
LLM calls it makes and keeps the durable plan in the local DB.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .config import Settings
from .db import Store, WorkItem
from .decompose import (
    Plan,
    SubTaskSpec,
    create_sub_issues,
    generate_plan,
    parse_plan,
    validate_plan,
)
from .gh import GitHubClient, STATUS_BLOCKED, STATUS_IN_PROGRESS, STATUS_REVIEW


class ReplanLimitError(Exception):
    """Raised when ``max_replans`` has been exhausted (spec HW-4)."""


class PlanningError(Exception):
    pass


class PlannerService:
    def __init__(
        self,
        store: Store,
        gh: GitHubClient,
        settings: Settings,
        llm_call: Callable[[str], str],
    ):
        self.store = store
        self.gh = gh
        self.settings = settings
        self.llm_call = llm_call

    # -- planning --------------------------------------------------------
    async def plan_requirement(
        self,
        requirement_number: int,
        title: Optional[str] = None,
        body: Optional[str] = None,
        base_branch: str = "main",
        base_sha: str = "",
    ) -> tuple[Plan, list[int]]:
        """Decompose a requirement into sub-issues and persist the plan."""
        if title is None or body is None:
            issue = await self.gh.get_issue(requirement_number)
            title = title if title is not None else issue.title
            body = body if body is not None else issue.body

        plan = generate_plan(self.llm_call, title or "", body or "", retries=1)
        task_numbers = await create_sub_issues(
            self.gh, requirement_number, plan,
            base_branch=base_branch, base_sha=base_sha,
        )
        self.store.work_items.set_plan(
            requirement_number, plan.to_json(), task_numbers
        )
        return plan, task_numbers

    async def replan(
        self,
        requirement_number: int,
        reason: str = "",
        title: Optional[str] = None,
        body: Optional[str] = None,
        base_branch: str = "main",
        base_sha: str = "",
    ) -> tuple[Plan, list[int]]:
        """Re-plan a requirement, enforcing the ``max_replans`` cap."""
        item = self.store.work_items.get(requirement_number)
        if item is None:
            raise PlanningError(f"requirement #{requirement_number} is not tracked")
        if item.replans >= self.settings.limits.max_replans:
            raise ReplanLimitError(
                f"requirement #{requirement_number} already re-planned "
                f"{item.replans} time(s); max_replans="
                f"{self.settings.limits.max_replans}"
            )
        self.store.work_items.increment_replans(requirement_number)
        return await self.plan_requirement(
            requirement_number, title=title, body=body,
            base_branch=base_branch, base_sha=base_sha,
        )

    # -- review judgement ------------------------------------------------
    async def judge_review(self, requirement_number: int, evidence_summary: str) -> bool:
        """Ask the planner whether the evidence satisfies the requirement.

        Returns True only on an unambiguous affirmative. Anything unparseable is
        treated as a rejection so a confused model can never wave work through.
        """
        prompt = (
            "You are reviewing whether an implementation satisfies a requirement.\n"
            "Answer with exactly one word: PASS or FAIL.\n\n"
            f"REQUIREMENT #{requirement_number}\n\nEVIDENCE\n{evidence_summary}\n"
        )
        try:
            answer = (self.llm_call(prompt) or "").strip().lower()
        except Exception:
            return False
        return answer.startswith("pass")

    # -- criteria updates -----------------------------------------------
    async def update_acceptance_criteria(
        self, requirement_number: int, new_criteria: str
    ) -> None:
        """Update the requirement body and force the stored plan to be refreshed."""
        issue = await self.gh.get_issue(requirement_number)
        new_body = (
            f"{issue.body.rstrip()}\n\n"
            f"## Acceptance criteria (updated by planner)\n{new_criteria}\n"
        )
        await self.gh.update_issue(requirement_number, body=new_body)
        item = self.store.work_items.get(requirement_number)
        if item is not None:
            payload = json.loads(item.plan_json) if item.plan_json else {"tasks": []}
            payload["acceptance_criteria"] = new_criteria
            payload["criteria_stale"] = True
            self.store.db.conn.execute(
                "UPDATE work_items SET plan_json = ?, updated_at = ? WHERE requirement_number = ?",
                (json.dumps(payload), _now(), requirement_number),
            )


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
