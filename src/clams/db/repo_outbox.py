"""outbox repository — the ONLY path to Redis and GitHub (invariant 3)."""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from .conn import plus_s, utcnow_s

OUTBOX_KINDS = ('bus_publish', 'gh_comment', 'gh_label', 'gh_board_move',
                'gh_subissue', 'gh_close')


def enqueue(conn: sqlite3.Connection, kind: str, payload: dict[str, Any],
            idempotency_key: str) -> Optional[int]:
    """Insert unless the idempotency key exists (dedup). Returns outbox_id."""
    if kind not in OUTBOX_KINDS:
        raise ValueError(f"unknown outbox kind {kind!r}")
    try:
        with conn.execute(
            "INSERT INTO outbox(kind, payload, idempotency_key) "
            "VALUES(?,?,?) RETURNING outbox_id",
            (kind, json.dumps(payload), idempotency_key)) as cur:
            return cur.fetchone()["outbox_id"]
    except sqlite3.IntegrityError:
        return None


def claim_due(conn: sqlite3.Connection, batch: int = 20) -> list[sqlite3.Row]:
    """Atomically mark due rows inflight and return them.

    Rows left ``inflight`` by a crashed relay are re-claimed by ``reclaim_stale``
    before this runs, so at-least-once delivery holds without double-sends from
    a live relay (the relay is single-threaded inside the planner process).
    """
    with conn.execute(
        "UPDATE outbox SET status='inflight' "
        "WHERE outbox_id IN ("
        "  SELECT outbox_id FROM outbox "
        "  WHERE status='pending' AND next_attempt_at<=? "
        "  ORDER BY outbox_id LIMIT ?) "
        "RETURNING *",
        (utcnow_s(), batch)) as cur:
        return cur.fetchall()


def reclaim_stale(conn: sqlite3.Connection, older_than_s: int = 300) -> int:
    """Crash recovery: inflight rows stuck for >older_than_s go back to pending."""
    cutoff = plus_s(-older_than_s)
    cur = conn.execute(
        "UPDATE outbox SET status='pending', next_attempt_at=? "
        "WHERE status='inflight' AND next_attempt_at<?", (utcnow_s(), cutoff))
    return cur.rowcount


def mark_sent(conn: sqlite3.Connection, outbox_id: int) -> None:
    conn.execute("UPDATE outbox SET status='sent' WHERE outbox_id=?",
                 (outbox_id,))


def set_payload(conn: sqlite3.Connection, outbox_id: int,
                payload: dict[str, Any]) -> None:
    """Persist updated payload mid-delivery (e.g. the created sub-issue
    number) so a retry after partial success is not duplicated."""
    conn.execute("UPDATE outbox SET payload=? WHERE outbox_id=?",
                 (json.dumps(payload), outbox_id))


def mark_failed(conn: sqlite3.Connection, outbox_id: int, error: str,
                backoff_base_s: int, max_attempts: int) -> str:
    """Bump attempts + exponential backoff; dead after max_attempts. New status."""
    row = conn.execute("SELECT attempts FROM outbox WHERE outbox_id=?",
                       (outbox_id,)).fetchone()
    attempts = (row["attempts"] if row else 0) + 1
    if attempts >= max_attempts:
        conn.execute(
            "UPDATE outbox SET status='dead', attempts=?, last_error=? "
            "WHERE outbox_id=?", (attempts, error[:500], outbox_id))
        return "dead"
    delay = backoff_base_s * (2 ** (attempts - 1))
    conn.execute(
        "UPDATE outbox SET status='pending', attempts=?, last_error=?, "
        "next_attempt_at=? WHERE outbox_id=?",
        (attempts, error[:500], plus_s(delay), outbox_id))
    return "pending"


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) n FROM outbox GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def dead_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) n FROM outbox WHERE status='dead'").fetchone()["n"]


def prune_sent(conn: sqlite3.Connection, days: int) -> int:
    cutoff = plus_s(-days * 86400)
    cur = conn.execute(
        "DELETE FROM outbox WHERE status='sent' AND created_at<?", (cutoff,))
    return cur.rowcount
