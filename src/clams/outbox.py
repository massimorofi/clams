"""Outbox relay — the ONLY path to Redis and GitHub (invariant 3).

Business transactions only INSERT outbox rows. This relay (running inside the
planner process) delivers them: ``bus_publish`` -> XADD, ``gh_*`` -> GitHub
client. Delivery is at-least-once; the idempotency key dedups at enqueue time
and consumers dedup by ``processed_events`` / marker.

Failure policy: exponential backoff, ``dead`` + alert after max attempts.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .db import repo_alerts, repo_events, repo_outbox

log = logging.getLogger("clams.outbox")


class OutboxRelay:
    def __init__(self, bus, gh_client: Optional[Any] = None, *,
                 backoff_base_s: int = 2, max_attempts: int = 5,
                 on_subissue_created=None):
        self.bus = bus
        self.gh = gh_client
        self.backoff_base_s = backoff_base_s
        self.max_attempts = max_attempts
        self.on_subissue_created = on_subissue_created

    def relay_once(self, conn, batch: int = 20) -> dict[str, int]:
        """Deliver due rows. Returns counts {sent, failed, dead}."""
        repo_outbox.reclaim_stale(conn, older_than_s=300)
        rows = repo_outbox.claim_due(conn, batch=batch)
        sent = failed = dead = 0
        for row in rows:
            try:
                self._dispatch(row)
                repo_outbox.mark_sent(conn, row["outbox_id"])
                sent += 1
            except Exception as exc:  # noqa: BLE001 - relay must not die
                status = repo_outbox.mark_failed(
                    conn, row["outbox_id"], str(exc),
                    self.backoff_base_s, self.max_attempts)
                if status == "dead":
                    dead += 1
                    repo_alerts.raise_alert(
                        conn, "outbox", "error",
                        f"outbox {row['outbox_id']} ({row['kind']}) dead: {exc}")
                    repo_events.append_event(
                        conn, "outbox", str(row["outbox_id"]), "error",
                        f"outbox row dead after {self.max_attempts} attempts: {exc}")
                else:
                    failed += 1
                    log.warning("outbox %s attempt failed: %s",
                                row["outbox_id"], exc)
        return {"sent": sent, "failed": failed, "dead": dead}

    # ------------------------------------------------------------------
    def _dispatch(self, row) -> None:
        kind = row["kind"]
        payload = json.loads(row["payload"])
        if kind == "bus_publish":
            stream = payload.get("stream")
            msg = payload.get("msg")
            if not stream or msg is None:
                raise ValueError("bus_publish payload needs stream+msg")
            self.bus.publish(stream, msg if isinstance(msg, str)
                             else json.dumps(msg))
            return
        if kind.startswith("gh_"):
            if self.gh is None:
                raise RuntimeError("no GitHub client configured")
            self._dispatch_gh(kind, payload)
            return
        raise ValueError(f"unknown outbox kind {kind!r}")

    def _dispatch_gh(self, kind: str, payload: dict) -> None:
        gh = self.gh
        if kind == "gh_comment":
            gh.add_comment(payload["repo"], payload["issue"],
                           payload["body"],
                           idempotency_key=payload.get("idempotency_key", ""))
        elif kind == "gh_label":
            gh.set_labels(payload["repo"], payload["issue"],
                          payload["labels"],
                          idempotency_key=payload.get("idempotency_key", ""))
        elif kind == "gh_board_move":
            gh.move_card(payload.get("project_node_id"), payload["issue"],
                         payload["option_name"],
                         idempotency_key=payload.get("idempotency_key", ""))
        elif kind == "gh_subissue":
            if payload.get("child"):
                gh.add_subissue(payload["repo"], payload["parent"],
                                payload["child"],
                                idempotency_key=payload.get("idempotency_key", ""))
            elif payload.get("task_id"):
                created = gh.create_issue(
                    payload["repo"], payload["title"], payload["body"],
                    payload.get("labels", []))
                number = created["number"]
                gh.add_subissue(payload["repo"], payload["parent"], number)
                if self.on_subissue_created:
                    self.on_subissue_created(payload["task_id"], number)
            else:
                raise ValueError("gh_subissue needs child or task_id")
        else:
            raise ValueError(f"unknown gh kind {kind!r}")
