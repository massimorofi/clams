"""events (UI timeline / SSE source) + processed_events (idempotency)."""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from .conn import plus_s, utcnow_s


def append_event(conn: sqlite3.Connection, scope: str, ref: str, level: str,
                 message: str, data: Optional[dict[str, Any]] = None) -> int:
    with conn.execute(
        "INSERT INTO events(scope, ref, level, message, data) "
        "VALUES(?,?,?,?,?) RETURNING event_id",
        (scope, ref, level, message,
         json.dumps(data) if data is not None else None)) as cur:
        return cur.fetchone()["event_id"]


def max_event_id(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COALESCE(MAX(event_id),0) m FROM events").fetchone()["m"]


def tail_events(conn: sqlite3.Connection, after_id: int,
                limit: int = 200) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM events WHERE event_id>? ORDER BY event_id LIMIT ?",
        (after_id, limit)).fetchall()


def prune_events(conn: sqlite3.Connection, days: int) -> int:
    cutoff = plus_s(-days * 86400)
    cur = conn.execute("DELETE FROM events WHERE ts<?", (cutoff,))
    return cur.rowcount


# ------------------------------------------------------------- processed_events
def was_processed(conn: sqlite3.Connection, msg_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM processed_events WHERE msg_id=?", (msg_id,)).fetchone() is not None


def mark_processed(conn: sqlite3.Connection, msg_id: str) -> None:
    conn.execute("INSERT OR IGNORE INTO processed_events(msg_id) VALUES(?)",
                 (msg_id,))


def prune_processed(conn: sqlite3.Connection, days: int) -> int:
    cutoff = plus_s(-days * 86400)
    cur = conn.execute("DELETE FROM processed_events WHERE processed_at<?",
                       (cutoff,))
    return cur.rowcount
