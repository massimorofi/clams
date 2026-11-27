"""github_events: the durable, normalized webhook intake ledger.

The API is the only writer of a *row* here (one per accepted delivery, keyed by
``delivery_id``); the planner consumes it. Nothing in this table is raw payload:
only the bounded, redacted fields produced by
``clams.github.webhooks.normalize_event``.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from ..github.webhooks import NormalizedEvent
from .conn import immediate, utcnow_s
from .repo_commands import insert_command

#: Command-inbox kind for an accepted GitHub delivery. The planner drains it;
#: an unknown kind is marked rejected (never silently dropped).
COMMAND_KIND = "github_event"

STATES = ("new", "processing", "done", "failed", "skipped")

_COLUMNS = ("delivery_id", "repo", "event_action", "pr_number", "head_ref",
            "head_sha", "check_id", "summary")


def insert_event(conn: sqlite3.Connection, record: NormalizedEvent) -> bool:
    """INSERT OR IGNORE one normalized delivery.

    Returns ``True`` when the row is new, ``False`` when ``delivery_id`` was
    already present (a GitHub redelivery) - i.e. the duplicate is a no-op.
    """
    row = record.as_row()
    values = [row[c] for c in _COLUMNS]
    cur = conn.execute(
        "INSERT OR IGNORE INTO github_events(" + ", ".join(_COLUMNS) + ") "
        "VALUES (" + ", ".join("?" * len(_COLUMNS)) + ")", values)
    return cur.rowcount > 0


def record_delivery(conn: sqlite3.Connection, record: NormalizedEvent,
                    issued_by: str = "github-webhook"
                    ) -> tuple[bool, Optional[int]]:
    """Persist the delivery and enqueue its command, atomically.

    Returns ``(is_new, command_id)``. Because the ledger row is written first
    inside one ``BEGIN IMMEDIATE`` transaction, a duplicate ``delivery_id``
    can never enqueue work twice.
    """
    with immediate(conn):
        if not insert_event(conn, record):
            return False, None
        command_id = insert_command(
            conn, COMMAND_KIND, record.as_command_payload(), issued_by,
            target=record.repo)
    return True, command_id


def get_event(conn: sqlite3.Connection,
              delivery_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM github_events WHERE delivery_id=?",
                        (delivery_id,)).fetchone()


def list_events(conn: sqlite3.Connection, state: Optional[str] = None,
                limit: int = 50) -> list[sqlite3.Row]:
    limit = max(1, min(int(limit), 500))
    if state:
        return conn.execute(
            "SELECT * FROM github_events WHERE processed_state=? "
            "ORDER BY received_at, delivery_id LIMIT ?", (state, limit)
        ).fetchall()
    return conn.execute(
        "SELECT * FROM github_events ORDER BY received_at DESC, delivery_id "
        "LIMIT ?", (limit,)).fetchall()


def mark_processed(conn: sqlite3.Connection, delivery_id: str,
                   state: str) -> None:
    if state not in STATES:
        raise ValueError(f"bad processed_state {state!r}")
    conn.execute("UPDATE github_events SET processed_state=? "
                 "WHERE delivery_id=?", (state, delivery_id))


def counts_by_state(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT processed_state, COUNT(*) n FROM github_events "
        "GROUP BY processed_state").fetchall()
    return {r["processed_state"]: r["n"] for r in rows}
