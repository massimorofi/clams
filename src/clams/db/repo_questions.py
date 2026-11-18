"""questions repository — executor questions awaiting a human answer."""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

from .conn import plus_s, utcnow_s


def open_question(conn: sqlite3.Connection, task_id: str, attempt: int,
                  question: str, options: list[str],
                  idempotency_key: str,
                  expires_hours: int = 24) -> Optional[int]:
    """Insert unless the idempotency key already exists. Returns question_id."""
    try:
        with conn.execute(
            "INSERT INTO questions(task_id, attempt, question, options, status, "
            "idempotency_key, expires_at) VALUES(?,?,?,?,'open',?,?) "
            "RETURNING question_id",
            (task_id, attempt, question, json.dumps(options),
             idempotency_key, plus_s(expires_hours * 3600))) as cur:
            return cur.fetchone()["question_id"]
    except sqlite3.IntegrityError:
        return None  # duplicate idempotency_key


def list_open(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM questions WHERE status='open' ORDER BY question_id"
    ).fetchall()


def get_for_task(conn: sqlite3.Connection, task_id: str,
                 attempt: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM questions WHERE task_id=? AND attempt=? "
        "ORDER BY question_id DESC LIMIT 1", (task_id, attempt)).fetchone()


def answer(conn: sqlite3.Connection, question_id: int, answer_text: str,
           answered_by: str) -> bool:
    with conn.execute(
        "UPDATE questions SET status='answered', answer=?, answered_by=?, "
        "answered_at=? WHERE question_id=? AND status='open' RETURNING question_id",
        (answer_text, answered_by, utcnow_s(), question_id)) as cur:
        return cur.fetchone() is not None


def cancel_open_for_task(conn: sqlite3.Connection, task_id: str) -> int:
    cur = conn.execute(
        "UPDATE questions SET status='cancelled' WHERE task_id=? AND status='open'",
        (task_id,))
    return cur.rowcount


def expire_due(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute(
        "SELECT question_id FROM questions WHERE status='open' AND expires_at<?",
        (utcnow_s(),)).fetchall()
    ids = [r["question_id"] for r in rows]
    if ids:
        marks = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE questions SET status='expired' WHERE question_id IN ({marks})",
            ids)
    return ids
