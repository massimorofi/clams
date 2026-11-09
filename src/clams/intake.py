"""Autonomous requirement intake (spec FR-2).

Polls GitHub for issues labelled ``status:Ready``, claims each one atomically in
the local DB (exactly one poller wins), and queues the GitHub side-effects through
the transactional outbox — never calling GitHub inside the claim transaction.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import Settings
from .db import Store, WorkItem
from .gh import GitHubClient, GitHubError, STATUS_IN_PROGRESS, STATUS_READY


class IntakeError(Exception):
    pass


@dataclass
class IntakeResult:
    scanned: int = 0
    claimed: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)
    failed: list[tuple[int, str]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"scanned={self.scanned} claimed={len(self.claimed)} "
            f"skipped={len(self.skipped)} failed={len(self.failed)}"
        )


class IntakeService:
    def __init__(self, store: Store, gh: GitHubClient, settings: Settings):
        self.store = store
        self.gh = gh
        self.settings = settings

    async def sweep(self) -> IntakeResult:
        """Find ``status:Ready`` issues and claim the ones we do not already track."""
        result = IntakeResult()
        try:
            issues = await self.gh.list_issues(state="open", labels=[STATUS_READY])
        except GitHubError as exc:
            # GitHub unreachable: leave local state untouched, retry next sweep.
            self.store.alerts.add("warning", "intake", f"GitHub sweep failed: {exc}")
            return result

        result.scanned = len(issues)
        for issue in issues:
            number = issue.number
            if await self._already_tracked(number):
                result.skipped.append(number)
                continue
            try:
                won = await self.claim(number, issue.title, issue.updated_at)
            except Exception as exc:  # noqa: BLE001 - one bad item must not kill the sweep
                result.failed.append((number, str(exc)))
                continue
            if won:
                result.claimed.append(number)
            else:
                result.skipped.append(number)
        return result

    async def _already_tracked(self, number: int) -> bool:
        return self.store.work_items.exists(number)

    async def claim(self, requirement_number: int, title: str, gh_updated_at: str) -> bool:
        """Atomically take ownership of one requirement.

        Returns True when this caller won the claim. The GitHub transition and the
        acknowledgement comment are written to the outbox in the same local
        transaction, so they are never lost and never applied before the claim.
        """
        won = self.store.work_items.claim_requirement(
            requirement_number, title, gh_updated_at or _now()
        )
        if not won:
            return False
        self._queue_transition(requirement_number)
        return True

    def _queue_transition(self, requirement_number: int) -> None:
        self.store.outbox.enqueue(
            "gh_status",
            {"issue": requirement_number, "status": STATUS_IN_PROGRESS},
            idempotency_key=f"intake-status-{requirement_number}",
        )
        self.store.outbox.enqueue(
            "gh_comment",
            {
                "issue": requirement_number,
                "body": (
                    "**[clams]** Requirement claimed for autonomous execution.\n\n"
                    "Status moved to `In Progress`. A plan will follow shortly."
                ),
            },
            idempotency_key=f"intake-comment-{requirement_number}",
        )

    async def add_requirement(self, title: str, body: str = "",
                             labels: Optional[list[str]] = None) -> int:
        """Create a new requirement issue on GitHub and let the next sweep claim it.

        Deliberately does **not** insert a ``work_items`` row: intake owns that
        write, so the normal atomic-claim path (and its outbox side-effects) is
        exercised exactly as it would be for a human-labelled issue.
        """
        labels = list(labels or [])
        if STATUS_READY not in labels:
            labels.append(STATUS_READY)
        if self.settings.project_label not in labels:
            labels.append(self.settings.project_label)
        issue = await self.gh.create_issue(title=title, body=body, labels=labels)
        return issue.number


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
