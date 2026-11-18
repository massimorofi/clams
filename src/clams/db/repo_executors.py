"""executors registry (fed by executor.alive events)."""
from __future__ import annotations

import sqlite3

from .conn import plus_s, utcnow_s


def upsert_alive(conn: sqlite3.Connection, executor_id: str, version: str,
                 capacity: int) -> None:
    conn.execute(
        "INSERT INTO executors(executor_id, version, capacity, last_seen) "
        "VALUES(?,?,?,?) "
        "ON CONFLICT(executor_id) DO UPDATE SET version=excluded.version, "
        "capacity=excluded.capacity, last_seen=excluded.last_seen",
        (executor_id, version, capacity, utcnow_s()))


def list_executors(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM executors ORDER BY executor_id").fetchall()


def live_executors(conn: sqlite3.Connection, max_idle_s: int) -> list[sqlite3.Row]:
    cutoff = plus_s(-max_idle_s)
    return conn.execute(
        "SELECT * FROM executors WHERE last_seen>=? ORDER BY executor_id",
        (cutoff,)).fetchall()


def prune(conn: sqlite3.Connection, max_idle_s: int) -> int:
    cutoff = plus_s(-max_idle_s)
    cur = conn.execute("DELETE FROM executors WHERE last_seen<?", (cutoff,))
    return cur.rowcount
