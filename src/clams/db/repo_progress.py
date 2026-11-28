"""worker_progress repository — fenced, append-only worker narration.

The only writer of ``worker_progress``. Every append is fenced against the
task's *current* dispatch so a stale executor cannot pollute the live attempt:

  * unknown task                      -> rejected
  * attempt_no != tasks.attempt       -> rejected (task was retried)
  * lease_token != tasks.lease_token  -> rejected (lease was reclaimed)
  * seq <= last stored seq for that
    (task_id, attempt_no)             -> rejected (replay / out-of-order)

A rejection is a pure read: no INSERT, no UPDATE, nothing on ``tasks``. The
brief columns (title/objective/acceptance/brief_json) are never written from
this module — progress is evidence, not state.

Stdlib only (sqlite3 + the stdlib dataclasses in clams.handoffs). Must stay
importable from the executor and plain scripts: no pydantic, no fastapi.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable, Optional

from ..handoffs import (MAX_PROGRESS_TEXT, ProgressKind, WorkerProgress,
                        bounded_text)
from .conn import utcnow_s

#: rejection reasons (stable strings; surfaced in the API and tests)
REJECT_UNKNOWN_TASK = "unknown_task"
REJECT_STALE_ATTEMPT = "stale_attempt"
REJECT_STALE_LEASE = "stale_lease"
REJECT_BAD_SEQ = "non_monotonic_seq"
REJECT_BAD_KIND = "invalid_kind"

DEFAULT_LIST_LIMIT = 200


@contextmanager
def _tx(conn: sqlite3.Connection):
    """Join the caller's transaction if one is open, else own a BEGIN IMMEDIATE."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def last_seq(conn: sqlite3.Connection, task_id: str, attempt_no: int) -> int:
    """Highest accepted seq for (task_id, attempt_no); 0 when none."""
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) s FROM worker_progress "
        "WHERE task_id=? AND attempt_no=?", (task_id, attempt_no)).fetchone()
    return int(row["s"]) if row else 0


def next_seq(conn: sqlite3.Connection, task_id: str, attempt_no: int) -> int:
    """Seq a well-behaved worker should use next for this attempt."""
    return last_seq(conn, task_id, attempt_no) + 1


def fence(conn: sqlite3.Connection, task_id: str, attempt_no: int,
          lease_token: str, seq: int) -> tuple[bool, str]:
    """Check a progress message without writing anything.

    Returns ``(accepted, reason)``; reason is "" when accepted.
    """
    task = conn.execute(
        "SELECT attempt, lease_token FROM tasks WHERE task_id=?",
        (task_id,)).fetchone()
    if task is None:
        return False, REJECT_UNKNOWN_TASK
    if int(task["attempt"] or 0) != int(attempt_no):
        return False, REJECT_STALE_ATTEMPT
    if (task["lease_token"] or "") != (lease_token or ""):
        return False, REJECT_STALE_LEASE
    if int(seq) <= last_seq(conn, task_id, attempt_no):
        return False, REJECT_BAD_SEQ
    return True, ""


def append_progress(conn: sqlite3.Connection, *, task_id: str, attempt_no: int,
                    seq: int, kind: str, text: Any,
                    lease_token: str = "") -> dict[str, Any]:
    """Fenced append of one progress line.

    Returns ``{"accepted": bool, "reason": str, "id": int|None,
    "seq": int, "text": <stored text>}``. On rejection nothing is written and
    ``id`` is None.
    """
    try:
        kind_v = ProgressKind.coerce(kind, "kind")
    except ValueError:
        return {"accepted": False, "reason": REJECT_BAD_KIND, "id": None,
                "seq": seq, "text": ""}
    stored = bounded_text(text, MAX_PROGRESS_TEXT, field_name="text")
    with _tx(conn):
        ok, reason = fence(conn, task_id, attempt_no, lease_token, seq)
        if not ok:
            return {"accepted": False, "reason": reason, "id": None,
                    "seq": seq, "text": stored}
        with conn.execute(
            "INSERT INTO worker_progress(task_id, attempt_no, lease_token, "
            "seq, kind, text, created_at) VALUES(?,?,?,?,?,?,?) "
            "RETURNING id",
            (task_id, int(attempt_no), lease_token or "", int(seq), kind_v,
             stored, utcnow_s())) as cur:
            row = cur.fetchone()
        return {"accepted": True, "reason": "", "id": row["id"],
                "seq": int(seq), "text": stored}


def append_progress_record(conn: sqlite3.Connection,
                           record: WorkerProgress) -> dict[str, Any]:
    """Append a validated :class:`WorkerProgress` contract."""
    return append_progress(
        conn, task_id=record.task_id, attempt_no=record.attempt_no,
        seq=record.seq, kind=record.kind, text=record.text,
        lease_token=record.lease_token)


def append_progress_batch(conn: sqlite3.Connection,
                          records: Iterable[WorkerProgress]) -> list[dict]:
    """Append several lines in one transaction, stopping at the first reject.

    A rejected line means the sender is stale, so later lines from the same
    sender are not worth trying.
    """
    out: list[dict] = []
    with _tx(conn):
        for rec in records:
            res = append_progress_record(conn, rec)
            out.append(res)
            if not res["accepted"]:
                break
    return out


# ------------------------------------------------------------------- reads
def list_progress(conn: sqlite3.Connection, task_id: str,
                  attempt_no: Optional[int] = None, *, after_id: int = 0,
                  limit: int = DEFAULT_LIST_LIMIT) -> list[sqlite3.Row]:
    """Progress rows for a task (newest attempt scope optional), ascending."""
    if attempt_no is None:
        return conn.execute(
            "SELECT * FROM worker_progress WHERE task_id=? AND id>?"
            " ORDER BY id LIMIT ?", (task_id, after_id, limit)).fetchall()
    return conn.execute(
        "SELECT * FROM worker_progress WHERE task_id=? AND attempt_no=?"
        " AND id>? ORDER BY id LIMIT ?",
        (task_id, int(attempt_no), after_id, limit)).fetchall()


def count_progress(conn: sqlite3.Connection, task_id: str) -> int:
    row = conn.execute("SELECT COUNT(*) n FROM worker_progress WHERE task_id=?",
                       (task_id,)).fetchone()
    return int(row["n"]) if row else 0


def progress_rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [{"id": r["id"], "task_id": r["task_id"],
             "attempt_no": r["attempt_no"], "seq": r["seq"],
             "kind": r["kind"], "text": r["text"],
             "lease_token": r["lease_token"], "created_at": r["created_at"]}
            for r in rows]
