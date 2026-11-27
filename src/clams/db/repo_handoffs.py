"""handoffs repository — worker-to-operator requests + dispositions.

A handoff is a *request for a human*, recorded as its own row. It never edits
the task it refers to: a ``scope_change`` handoff stores the ask in
``requested_action`` and leaves ``tasks.title/objective/acceptance/brief_json``
untouched. The planner stays the only domain-state writer; the API reaches a
disposition through the command inbox (see planner/commands.py).

Disposition workflow (forward-only, append-safe):

    open -> acknowledged -> resolved | rejected
    open -> resolved | rejected          (dismiss without ceremony)

Nothing moves backwards and no row is ever deleted or rewritten with new
content: a transition only narrows ``status`` and fills ``disposition`` /
``disposed_by``, and every accepted transition is mirrored into the
append-only ``events`` timeline. Re-applying the current status is a no-op
(idempotent), which is what makes a replayed command harmless.

Stdlib only: sqlite3 + the dataclass contracts in clams.handoffs.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable, Optional

from ..handoffs import (DISPOSITIONS, Handoff, HandoffStatus, MAX_DISPOSED_BY,
                        MAX_HANDOFF_REASON, HandoffKind, bounded_text,
                        can_move_status)
from .conn import utcnow_s
from . import repo_events

#: rejection reasons (stable strings)
REJECT_UNKNOWN = "unknown_handoff"
REJECT_BAD_TRANSITION = "illegal_transition"
REJECT_BAD_DISPOSITION = "invalid_disposition"
REJECT_BAD_STATUS = "invalid_status"

DEFAULT_LIST_LIMIT = 100


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


def _row_to_handoff(row: sqlite3.Row) -> Handoff:
    return Handoff(
        id=row["id"], workflow_id=row["workflow_id"], task_id=row["task_id"],
        attempt_no=int(row["attempt_no"]), kind=row["kind"],
        reason=row["reason"] or "", requested_action=row["requested_action"] or "",
        status=row["status"], disposition=row["disposition"] or "",
        disposed_by=row["disposed_by"] or "",
        created_at=row["created_at"] or "", updated_at=row["updated_at"] or "")


# ------------------------------------------------------------------ writes
def record_handoff(conn: sqlite3.Connection, handoff: Handoff,
                   *, emit_event: bool = True) -> dict[str, Any]:
    """Append a handoff. Idempotent on the natural key.

    Natural key = (task_id, attempt_no, kind, reason, requested_action): a
    redelivered bus message appends nothing and returns the existing id with
    ``created=False``.

    The task's stored ``workflow_id`` wins over the payload's when the task
    still exists (authoritative); handoffs for deleted tasks (replanned away)
    are still recorded — history outlives the task row.
    """
    if not isinstance(handoff, Handoff):
        raise ValueError("record_handoff needs a Handoff contract")
    # validate the closed vocabularies before touching the DB
    HandoffKind.coerce(handoff.kind, "kind")
    HandoffStatus.coerce(handoff.status or HandoffStatus.OPEN.value, "status")
    with _tx(conn):
        task = conn.execute(
            "SELECT workflow_id FROM tasks WHERE task_id=?",
            (handoff.task_id,)).fetchone()
        wf_id = task["workflow_id"] if task else handoff.workflow_id
        existing = conn.execute(
            "SELECT id, status FROM handoffs WHERE task_id=? AND attempt_no=?"
            " AND kind=? AND reason=? AND requested_action=?",
            (handoff.task_id, handoff.attempt_no, handoff.kind,
             handoff.reason, handoff.requested_action)).fetchone()
        if existing is not None:
            return {"id": existing["id"], "created": False,
                    "status": existing["status"], "workflow_id": wf_id}
        now = utcnow_s()
        with conn.execute(
            "INSERT INTO handoffs(workflow_id, task_id, attempt_no, kind, "
            "reason, requested_action, status, disposition, disposed_by, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "RETURNING id",
            (wf_id, handoff.task_id, int(handoff.attempt_no), handoff.kind,
             handoff.reason, handoff.requested_action,
             handoff.status or HandoffStatus.OPEN.value,
             handoff.disposition, handoff.disposed_by, now, now)) as cur:
            hid = cur.fetchone()["id"]
        if emit_event:
            repo_events.append_event(
                conn, "task", handoff.task_id, "warn",
                f"handoff ({handoff.kind}) by worker: "
                f"{handoff.reason[:200] or 'no reason given'}",
                {"handoff_id": hid, "attempt_no": handoff.attempt_no,
                 "requested_action": handoff.requested_action})
        return {"id": hid, "created": True,
                "status": handoff.status or HandoffStatus.OPEN.value,
                "workflow_id": wf_id}


def record_handoff_payload(conn: sqlite3.Connection,
                           payload: dict[str, Any]) -> dict[str, Any]:
    """Build the contract from a raw worker payload, then record it."""
    return record_handoff(conn, Handoff.from_payload(payload))


def dispose(conn: sqlite3.Connection, handoff_id: int, to_status: str, *,
            disposition: str = "", disposed_by: str = "",
            note: str = "") -> dict[str, Any]:
    """Move a handoff forward along open -> acknowledged -> resolved|rejected.

    Append-safe: only ``status``/``disposition``/``disposed_by``/``updated_at``
    change, the row is never deleted, and the transition is mirrored into the
    append-only ``events`` table. Re-applying the current status is a no-op
    (``changed=False``) so a replayed command cannot corrupt history.
    """
    try:
        dst = (HandoffStatus.coerce(to_status, "status") if to_status
               else HandoffStatus.OPEN.value)
    except ValueError:
        return {"ok": False, "reason": REJECT_BAD_STATUS, "id": handoff_id}
    if disposition not in DISPOSITIONS:
        return {"ok": False, "reason": REJECT_BAD_DISPOSITION, "id": handoff_id}
    who = bounded_text(disposed_by or "operator", MAX_DISPOSED_BY,
                       field_name="disposed_by")
    with _tx(conn):
        row = conn.execute("SELECT * FROM handoffs WHERE id=?",
                           (handoff_id,)).fetchone()
        if row is None:
            return {"ok": False, "reason": REJECT_UNKNOWN, "id": handoff_id}
        src = row["status"]
        if src == dst:
            return {"ok": True, "changed": False, "id": handoff_id,
                    "status": src, "from": src}
        if not can_move_status(src, dst):
            return {"ok": False, "reason": REJECT_BAD_TRANSITION,
                    "id": handoff_id, "from": src, "to": dst}
        # a terminal status with no explicit disposition still needs a label
        disp = disposition or (row["disposition"] or "")
        if not disp and dst in (HandoffStatus.RESOLVED.value,
                                HandoffStatus.REJECTED.value):
            disp = "accepted" if dst == HandoffStatus.RESOLVED.value else "denied"
        conn.execute(
            "UPDATE handoffs SET status=?, disposition=?, disposed_by=?, "
            "updated_at=? WHERE id=?",
            (dst, disp, who if dst != HandoffStatus.OPEN.value
             else row["disposed_by"], utcnow_s(), handoff_id))
        repo_events.append_event(
            conn, "task", row["task_id"], "info",
            f"handoff #{handoff_id} {src} -> {dst} by {who}"
            + (f": {note[:200]}" if note else ""),
            {"handoff_id": handoff_id, "from": src, "to": dst,
             "disposition": disp})
        return {"ok": True, "changed": True, "id": handoff_id, "status": dst,
                "from": src, "disposition": disp, "disposed_by": who}


# ------------------------------------------------------------------- reads
def get_handoff(conn: sqlite3.Connection, handoff_id: int) -> Optional[Handoff]:
    row = conn.execute("SELECT * FROM handoffs WHERE id=?",
                       (handoff_id,)).fetchone()
    return _row_to_handoff(row) if row else None


def list_handoffs(conn: sqlite3.Connection, *, status: Optional[str] = None,
                  workflow_id: Optional[str] = None,
                  task_id: Optional[str] = None, kind: Optional[str] = None,
                  limit: int = DEFAULT_LIST_LIMIT) -> list[Handoff]:
    sql, args = "SELECT * FROM handoffs WHERE 1=1", []
    if status:
        sql += " AND status=?"
        args.append(status)
    if workflow_id:
        sql += " AND workflow_id=?"
        args.append(workflow_id)
    if task_id:
        sql += " AND task_id=?"
        args.append(task_id)
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    return [_row_to_handoff(r) for r in conn.execute(sql, args).fetchall()]


def counts_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) n FROM handoffs GROUP BY status").fetchall()
    return {r["status"]: int(r["n"]) for r in rows}


def handoffs_to_dicts(rows: Iterable[Handoff]) -> list[dict]:
    return [h.as_dict() for h in rows]


# ------------------------------------------------------- command inbox bridge
#: command kind the planner drains to apply a disposition (migration 012
#: extends commands.kind CHECK to allow it)
DISPOSE_COMMAND_KIND = "dispose_handoff"


def enqueue_disposition(conn: sqlite3.Connection, handoff_id: int,
                        to_status: str, *, issued_by: str = "operator",
                        disposition: str = "", note: str = "") -> int:
    """Queue an operator disposition as a command; returns command_id.

    This is the API's write path for handoffs: it inserts into ``commands``
    (the API's only permitted write target) and the planner applies the
    transition when it drains the inbox. The API therefore never mutates
    domain state directly.

    Inserted here rather than in repo_commands.insert_command because that
    module's COMMAND_KINDS tuple is frozen for this change; the DB CHECK
    (migration 012) is the authoritative gate.
    """
    import json as _json
    HandoffStatus.coerce(to_status, "status")
    if disposition not in DISPOSITIONS:
        raise ValueError(f"disposition must be one of {DISPOSITIONS}")
    payload = {"handoff_id": int(handoff_id), "status": to_status,
               "disposition": disposition}
    if note:
        payload["note"] = bounded_text(note, MAX_HANDOFF_REASON,
                                       field_name="note")
    with conn.execute(
        "INSERT INTO commands(kind, target, payload, issued_by) "
        "VALUES(?,?,?,?) RETURNING command_id",
        (DISPOSE_COMMAND_KIND, str(handoff_id), _json.dumps(payload),
         issued_by or "operator")) as cur:
        return cur.fetchone()["command_id"]
