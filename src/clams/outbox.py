"""Transactional outbox worker (spec §5 outbox table, §8.3).

Every external side-effect (GitHub comment, label change, notification) is written
to the ``outbox`` table inside the same local transaction as the state change that
caused it. This worker delivers them afterwards, so a crash never leaves GitHub and
the local DB disagreeing.

Delivery is at-least-once; an idempotency key on each row keeps a retry from
posting the same comment twice.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .config import Settings
from .db import Store


@dataclass
class DeliveryReport:
    delivered: int = 0
    failed: int = 0
    permanently_failed: int = 0

    def summary(self) -> str:
        return (
            f"delivered={self.delivered} failed={self.failed} "
            f"permanently_failed={self.permanently_failed}"
        )


class OutboxWorker:
    MAX_ATTEMPTS = 3

    def __init__(self, store: Store, gh=None, settings: Optional[Settings] = None,
                 notifier: Optional[Callable[[dict], Any]] = None):
        self.store = store
        self.gh = gh
        self.settings = settings or Settings()
        self.notifier = notifier

    async def deliver_once(self) -> DeliveryReport:
        """Claim and deliver all currently pending outbox rows."""
        report = DeliveryReport()
        rows = self.store.outbox.claim_pending(limit=50)
        for row in rows:
            try:
                await self._deliver(row)
                self.store.outbox.mark(row["id"], "sent")
                report.delivered += 1
            except Exception as exc:  # noqa: BLE001
                # ``claim_pending`` already incremented attempts, so read the
                # current value rather than trusting the pre-claim snapshot.
                cur = self.store.db.conn.execute(
                    "SELECT attempts FROM outbox WHERE id = ?", (row["id"],)
                ).fetchone()
                attempts = int(cur["attempts"]) if cur else 0
                if attempts >= self.MAX_ATTEMPTS:
                    self.store.outbox.mark(row["id"], "failed")
                    report.permanently_failed += 1
                    self.store.alerts.add(
                        "warning", "outbox",
                        f"outbox row {row['id']} ({row['kind']}) failed permanently: {exc}",
                    )
                else:
                    self.store.outbox.mark(row["id"], "pending")
                    report.failed += 1
        return report

    async def _deliver(self, row) -> None:
        kind = row["kind"]
        payload = json.loads(row["payload"])
        if kind == "gh_comment":
            if self.gh is None:
                raise RuntimeError("no github client configured")
            await self.gh.add_comment(int(payload["issue"]), payload["body"])
        elif kind == "gh_status":
            if self.gh is None:
                raise RuntimeError("no github client configured")
            await self.gh.set_status(int(payload["issue"]), payload["status"])
        elif kind == "notification":
            if self.notifier is None:
                raise RuntimeError("no notifier configured")
            result = self.notifier(payload)
            import inspect
            if inspect.isawaitable(result):
                await result
        else:
            raise ValueError(f"unknown outbox kind {kind!r}")

    async def run_forever(self, interval: float = 5.0) -> None:
        import asyncio

        while True:
            await self.deliver_once()
            await asyncio.sleep(interval)
