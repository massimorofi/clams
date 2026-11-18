"""alerts repository — watchdog + dead-letter alerts, deduped while open."""
from __future__ import annotations

import sqlite3
from typing import Optional

from .conn import utcnow_s


def raise_alert(conn: sqlite3.Connection, probe: str, severity: str,
                message: str) -> Optional[int]:
    """Insert unless an open alert for the same probe exists. Returns alert_id."""
    existing = conn.execute(
        "SELECT alert_id FROM alerts WHERE probe=? AND status='open'",
        (probe,)).fetchone()
    if existing:
        return None
    with conn.execute(
        "INSERT INTO alerts(probe, severity, message) VALUES(?,?,?) "
        "RETURNING alert_id", (probe, severity, message)) as cur:
        return cur.fetchone()["alert_id"]


def clear_alert(conn: sqlite3.Connection, probe: str) -> int:
    cur = conn.execute(
        "UPDATE alerts SET status='cleared', cleared_at=? "
        "WHERE probe=? AND status='open'", (utcnow_s(), probe))
    return cur.rowcount


def list_open(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM alerts WHERE status='open' ORDER BY alert_id").fetchall()


def open_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) n FROM alerts WHERE status='open'").fetchone()["n"]
