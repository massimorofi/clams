"""Stall-evidence bundles raised by the watchdog's liveness monitor.

One row per (task_id, attempt): the deterministic evidence the planner
collected when a running task's session stopped advancing (session tail,
health probes, heartbeat snapshot). Alert-only artifact: a human reads it
via `clams monitor` / the DB and decides cancel or retry.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

from .conn import plus_s, utcnow_s


def upsert(conn: sqlite3.Connection, task_id: str, attempt: int,
           bundle: dict) -> None:
    conn.execute(
        "INSERT INTO stall_evidence (task_id, attempt, updated_at, bundle_json)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(task_id, attempt) DO UPDATE SET"
        "   updated_at = excluded.updated_at,"
        "   bundle_json = excluded.bundle_json",
        (task_id, attempt, utcnow_s(), json.dumps(bundle, default=str)))


def get(conn: sqlite3.Connection, task_id: str,
        attempt: int) -> Optional[dict]:
    row = conn.execute(
        "SELECT bundle_json FROM stall_evidence WHERE task_id=? AND attempt=?",
        (task_id, attempt)).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["bundle_json"])
    except ValueError:
        return None


def list_recent(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM stall_evidence ORDER BY updated_at DESC LIMIT ?",
        (limit,)).fetchall()


def prune(conn: sqlite3.Connection, max_age_days: int) -> int:
    cur = conn.execute(
        "DELETE FROM stall_evidence WHERE updated_at < ?",
        (plus_s(-max_age_days * 86400),))
    return cur.rowcount
