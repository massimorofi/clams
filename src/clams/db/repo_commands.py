"""commands repository — the API's ONLY write target; planner drains them."""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from .conn import utcnow_s

COMMAND_KINDS = ('pause', 'resume', 'cancel', 'replan', 'retry', 'answer',
                 'adhoc', 'config_patch', 'settings_patch', 'prompt_patch',
                 'apply_plan', 'github_event', 'recover')


def insert_command(conn: sqlite3.Connection, kind: str, payload: dict[str, Any],
                   issued_by: str, target: Optional[str] = None) -> int:
    if kind not in COMMAND_KINDS:
        raise ValueError(f"unknown command kind {kind!r}")
    with conn.execute(
        "INSERT INTO commands(kind, target, payload, issued_by) "
        "VALUES(?,?,?,?) RETURNING command_id",
        (kind, target, json.dumps(payload), issued_by)) as cur:
        return cur.fetchone()["command_id"]


def fetch_new(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM commands WHERE status='new' ORDER BY command_id LIMIT ?",
        (limit,)).fetchall()


def mark(conn: sqlite3.Connection, command_id: int, status: str,
         result: Optional[dict[str, Any]] = None) -> None:
    if status not in ("applied", "rejected"):
        raise ValueError(f"bad command status {status}")
    conn.execute(
        "UPDATE commands SET status=?, result=?, applied_at=? WHERE command_id=?",
        (status, json.dumps(result) if result is not None else None,
         utcnow_s(), command_id))


def get(conn: sqlite3.Connection, command_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM commands WHERE command_id=?",
                        (command_id,)).fetchone()
