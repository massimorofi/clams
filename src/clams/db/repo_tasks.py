"""tasks repository + task state machine (§6).

    pending ─(deps all success)→ ready ─(outbox bus_publish)→ dispatched
    dispatched ─(task.started)→ running
    running ─(result success + gate pass)→ success
    running ─(result failed | gate fail | lease expired)→ attempt<max ? ready : failed
    running ─(task.question)→ waiting_input ─(answer cmd)→ ready
    any non-terminal ─(cancel cmd)→ cancelled

Claim logic uses ``UPDATE … WHERE … RETURNING`` (dialect-neutral: SQLite>=3.35,
Postgres) so a claim is atomic even if the single-writer rule is ever relaxed.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Optional

from .conn import plus_s, utcnow_s, epoch_to_iso

TASK_STATUSES = ('pending', 'ready', 'dispatched', 'running', 'waiting_input',
                 'success', 'failed', 'blocked', 'cancelled')

TERMINAL = {"success", "failed", "cancelled"}

#: allowed transitions (source -> set of targets)
TRANSITIONS: dict[str, set[str]] = {
    "pending": {"ready", "blocked", "cancelled"},
    "ready": {"dispatched", "blocked", "cancelled"},
    "dispatched": {"running", "ready", "failed", "blocked", "cancelled"},
    "running": {"success", "ready", "failed", "waiting_input", "blocked",
                "cancelled"},
    "waiting_input": {"ready", "blocked", "cancelled"},
    "blocked": {"ready", "cancelled"},
    "success": set(),
    "failed": set(),
    "cancelled": set(),
}


class IllegalTransition(Exception):
    pass


def can_transition(src: str, dst: str) -> bool:
    return dst in TRANSITIONS.get(src, set())


def transition(conn: sqlite3.Connection, task_id: str, dst: str,
               **extra: Any) -> sqlite3.Row:
    """Move a task to ``dst`` enforcing the transition table."""
    row = conn.execute("SELECT status FROM tasks WHERE task_id=?",
                       (task_id,)).fetchone()
    if row is None:
        raise KeyError(f"no task {task_id}")
    src = row["status"]
    if src == dst:
        return conn.execute("SELECT * FROM tasks WHERE task_id=?",
                            (task_id,)).fetchone()
    if not can_transition(src, dst):
        raise IllegalTransition(f"task {task_id}: {src} -> {dst} not allowed")
    sets = ["status=?", "updated_at=?"]
    vals: list[Any] = [dst, utcnow_s()]
    for k, v in extra.items():
        sets.append(f"{k}=?")
        vals.append(v)
    vals.append(task_id)
    conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE task_id=?", vals)
    return conn.execute("SELECT * FROM tasks WHERE task_id=?",
                        (task_id,)).fetchone()


def block_dependents(conn: sqlite3.Connection, workflow_id: str) -> list[str]:
    """pending/ready tasks whose deps reference a failed/blocked/cancelled
    task -> blocked. Iterative (blocking one strands its own dependents).
    Idempotent; safe to call every cycle. Returns blocked task_ids.

    A task stranded this way can never run: its deps can no longer all reach
    'success'. Without this the workflow never reaches an all-terminal state
    and is never reviewed (§6). Callers emit the audit events; this helper
    uses a direct UPDATE (not ``transition``) so it stays a pure repo op.
    """
    blocked: list[str] = []
    while True:
        rows = conn.execute(
            "SELECT task_id, depends_on FROM tasks "
            "WHERE workflow_id=? AND status IN ('pending','ready')",
            (workflow_id,)).fetchall()
        progressed = False
        for r in rows:
            deps = json.loads(r["depends_on"])
            if not deps:
                continue
            marks = ",".join("?" * len(deps))
            n = conn.execute(
                f"SELECT COUNT(*) n FROM tasks WHERE workflow_id=? "
                f"AND status IN ('failed','blocked','cancelled') "
                f"AND sequence_index IN ({marks})",
                (workflow_id, *deps)).fetchone()["n"]
            if n:
                conn.execute(
                    "UPDATE tasks SET status='blocked', updated_at=? "
                    "WHERE task_id=?", (utcnow_s(), r["task_id"]))
                blocked.append(r["task_id"])
                progressed = True
        if not progressed:
            return blocked


def delete_workflow_tasks(conn: sqlite3.Connection, workflow_id: str) -> int:
    """Delete every task of a workflow (replan swap). Questions first: their
    FK to tasks has no cascade. Caller must hold the transaction."""
    ids = [r["task_id"] for r in conn.execute(
        "SELECT task_id FROM tasks WHERE workflow_id=?",
        (workflow_id,)).fetchall()]
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    conn.execute(f"DELETE FROM questions WHERE task_id IN ({marks})", ids)
    conn.execute("DELETE FROM tasks WHERE workflow_id=?", (workflow_id,))
    return len(ids)


def create_task(conn: sqlite3.Connection, workflow_id: str, sequence_index: int,
                title: str, objective: str, acceptance: str, brief: dict[str, Any],
                depends_on: Optional[list[int]] = None,
                max_attempts: int = 3,
                gh_issue_number: Optional[int] = None) -> str:
    import uuid as _uuid
    task_id = str(_uuid.uuid4())
    conn.execute(
        "INSERT INTO tasks(task_id, workflow_id, sequence_index, depends_on, "
        "title, objective, acceptance, brief_json, status, max_attempts, "
        "gh_issue_number) VALUES(?,?,?,?,?,?,?,?,'pending',?,?)",
        (task_id, workflow_id, sequence_index,
         json.dumps(depends_on or []), title, objective, acceptance,
         json.dumps(brief), max_attempts, gh_issue_number))
    return task_id


def get_task(conn: sqlite3.Connection, task_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM tasks WHERE task_id=?",
                        (task_id,)).fetchone()


def list_tasks(conn: sqlite3.Connection, workflow_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM tasks WHERE workflow_id=? ORDER BY sequence_index",
        (workflow_id,)).fetchall()


def by_status(conn: sqlite3.Connection, *statuses: str) -> list[sqlite3.Row]:
    marks = ",".join("?" * len(statuses))
    return conn.execute(
        f"SELECT * FROM tasks WHERE status IN ({marks}) "
        "ORDER BY workflow_id, sequence_index", statuses).fetchall()


def counts_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) n FROM tasks GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


# --------------------------------------------------------------- state machine
def deps_satisfied(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    deps = json.loads(row["depends_on"])
    if not deps:
        return True
    marks = ",".join("?" * len(deps))
    n = conn.execute(
        f"SELECT COUNT(*) n FROM tasks WHERE workflow_id=? AND status='success' "
        f"AND sequence_index IN ({marks})",
        (row["workflow_id"], *deps)).fetchone()["n"]
    return n == len(deps)


def promote_ready(conn: sqlite3.Connection) -> list[str]:
    """pending -> ready where all deps are success. Returns task_ids."""
    promoted: list[str] = []
    rows = conn.execute(
        "SELECT * FROM tasks WHERE status='pending' "
        "ORDER BY workflow_id, sequence_index").fetchall()
    for r in rows:
        if deps_satisfied(conn, r):
            transition(conn, r["task_id"], "ready")
            promoted.append(r["task_id"])
    return promoted


def claim_for_dispatch(conn: sqlite3.Connection, task_id: str,
                       lease_seconds: int) -> Optional[sqlite3.Row]:
    """ready -> dispatched atomically; new attempt + lease token."""
    token = str(uuid.uuid4())
    with conn.execute(
        "UPDATE tasks SET status='dispatched', attempt=attempt+1, "
        "lease_token=?, lease_expires_at=?, updated_at=? "
        "WHERE task_id=? AND status='ready' "
        "RETURNING *",
        (token, plus_s(lease_seconds), utcnow_s(), task_id)) as cur:
        return cur.fetchone()


def _running_lease(row: sqlite3.Row, extend_s: int,
                   started_epoch: Optional[float] = None) -> str:
    """Lease = now+extend_s, never beyond attempt start + max_wall_s + 300 (§6).

    Anchored on started_at (per-attempt) so a task retried hours after creation
    still gets a usable lease; falls back to created_at for legacy rows.
    """
    from .conn import iso_to_epoch
    import time
    brief = json.loads(row["brief_json"])
    if started_epoch is None:
        started_epoch = iso_to_epoch(row["started_at"] or row["created_at"])
    wall_cap = started_epoch + int(brief.get("max_wall_s", 1800)) + 300
    return epoch_to_iso(min(time.time() + extend_s, wall_cap))


def on_started(conn: sqlite3.Connection, task_id: str, attempt: int,
               lease_token: str, executor_id: str,
               session_ref: str, extend_s: int = 180) -> Optional[sqlite3.Row]:
    """dispatched -> running, fenced on (task_id, attempt, lease_token)."""
    row = get_task(conn, task_id)
    if row is None:
        return None
    import time
    now_e = time.time()
    with conn.execute(
        "UPDATE tasks SET status='running', executor_id=?, session_ref=?, "
        "started_at=?, lease_expires_at=?, updated_at=? "
        "WHERE task_id=? AND attempt=? AND lease_token=? AND status='dispatched' "
        "RETURNING *",
        (executor_id, session_ref, utcnow_s(),
         _running_lease(row, extend_s, started_epoch=now_e),
         utcnow_s(), task_id, attempt, lease_token)) as cur:
        return cur.fetchone()


def on_heartbeat(conn: sqlite3.Connection, task_id: str, attempt: int,
                 lease_token: str, extend_s: int = 180,
                 hard_cap_s: int = 1800) -> Optional[sqlite3.Row]:
    """running stays running; lease extended to now+extend_s, never past wall cap."""
    row = get_task(conn, task_id)
    if row is None or row["status"] != "running":
        return None
    if row["attempt"] != attempt or row["lease_token"] != lease_token:
        return None
    return conn.execute(
        "UPDATE tasks SET lease_expires_at=?, updated_at=? WHERE task_id=? "
        "RETURNING *", (_running_lease(row, extend_s), utcnow_s(),
                        task_id)).fetchone()


def on_question(conn: sqlite3.Connection, task_id: str, attempt: int,
                lease_token: str) -> Optional[sqlite3.Row]:
    return transition_fenced(conn, task_id, attempt, lease_token,
                             "waiting_input", from_status="running")


def on_result(conn: sqlite3.Connection, task_id: str, attempt: int,
              lease_token: str, outcome: str, gate_passed: bool,
              result: dict[str, Any]) -> Optional[sqlite3.Row]:
    """Apply a task.result with fencing. Returns the updated row or None if fenced."""
    row = get_task(conn, task_id)
    if row is None or row["attempt"] != attempt or row["lease_token"] != lease_token:
        return None
    if row["status"] not in ("running", "dispatched"):
        return None
    extra: dict[str, Any] = {
        "result_json": json.dumps(result),
        "session_ref": result.get("session_ref") or row["session_ref"],
        "commit_sha": result.get("commit_sha") or "",
        "branch": result.get("branch") or "",
    }
    if outcome == "success" and gate_passed:
        return transition(conn, task_id, "success", **extra)
    if outcome == "needs_input":
        return transition(conn, task_id, "waiting_input", **extra)
    # failed / gate fail: retry if attempts remain
    if row["attempt"] < row["max_attempts"]:
        extra["lease_token"] = None
        extra["lease_expires_at"] = None
        extra["executor_id"] = None
        return transition(conn, task_id, "ready", **extra)
    extra["error_log"] = str(result.get("log_tail", ""))[:4000]
    return transition(conn, task_id, "failed", **extra)


def transition_fenced(conn: sqlite3.Connection, task_id: str, attempt: int,
                      lease_token: str, dst: str,
                      from_status: str) -> Optional[sqlite3.Row]:
    row = get_task(conn, task_id)
    if (row is None or row["attempt"] != attempt
            or row["lease_token"] != lease_token or row["status"] != from_status):
        return None
    return transition(conn, task_id, dst)


def requeue_after_lease(conn: sqlite3.Connection, task_id: str) -> Optional[sqlite3.Row]:
    """Lease expired: attempt<max -> ready else failed."""
    row = get_task(conn, task_id)
    if row is None:
        return None
    extra = {"lease_token": None, "lease_expires_at": None, "executor_id": None}
    if row["attempt"] < row["max_attempts"]:
        return transition(conn, task_id, "ready", **extra)
    return transition(conn, task_id, "failed",
                      error_log="lease expired; attempts exhausted", **extra)


def expire_waiting_input(conn: sqlite3.Connection, hours: int) -> list[str]:
    """waiting_input older than ``hours`` -> blocked (question expiry)."""
    cutoff = plus_s(-hours * 3600)
    rows = conn.execute(
        "SELECT task_id FROM tasks WHERE status='waiting_input' AND updated_at<?",
        (cutoff,)).fetchall()
    for r in rows:
        transition(conn, r["task_id"], "blocked")
    return [r["task_id"] for r in rows]


def expired_leases(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM tasks WHERE status IN ('dispatched','running') "
        "AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
        (utcnow_s(),)).fetchall()


def oldest_ready_age_s(conn: sqlite3.Connection) -> Optional[float]:
    import time
    row = conn.execute(
        "SELECT MIN(updated_at) t FROM tasks WHERE status='ready'").fetchone()
    if not row or not row["t"]:
        return None
    from .conn import iso_to_epoch
    return max(0.0, time.time() - iso_to_epoch(row["t"]))


def workflow_tasks_all_terminal(conn: sqlite3.Connection, workflow_id: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) n FROM tasks WHERE workflow_id=? AND status NOT IN "
        "('success','failed','cancelled','blocked')", (workflow_id,)).fetchone()
    return row["n"] == 0


def workflow_outcome(conn: sqlite3.Connection, workflow_id: str) -> str:
    """'done' all success | 'failed' any failed | 'blocked' any blocked and
    none failed (stranded work needs a human, not a replan) | 'cancelled'."""
    rows = conn.execute(
        "SELECT status, COUNT(*) n FROM tasks WHERE workflow_id=? GROUP BY status",
        (workflow_id,)).fetchall()
    counts = {r["status"]: r["n"] for r in rows}
    if counts.get("failed", 0):
        return "failed"
    if counts.get("blocked", 0):
        return "blocked"
    if counts.get("cancelled", 0) and not counts.get("success", 0):
        return "cancelled"
    return "done"


def set_brief_field(conn: sqlite3.Connection, task_id: str,
                    updates: dict[str, Any]) -> None:
    """Patch the stored brief_json (e.g. carry an answer + session_ref on resume)."""
    row = get_task(conn, task_id)
    if row is None:
        return
    brief = json.loads(row["brief_json"])
    brief.update(updates)
    conn.execute("UPDATE tasks SET brief_json=?, updated_at=? WHERE task_id=?",
                 (json.dumps(brief), utcnow_s(), task_id))
