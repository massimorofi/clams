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


#: Terminal statuses that record *undelivered* work. A replan supersedes the
#: attempt, so an absent row in one of these states must still go to
#: 'cancelled' (see ``reconcile_plan``): leaving it 'failed'/'blocked' keeps
#: ``workflow_outcome`` permanently 'failed'/'blocked' no matter how well the
#: new plan performs. 'success' is deliberately NOT here - it records delivered
#: work and must survive a replan untouched.
SUPERSEDE_TERMINAL = ("failed", "blocked")


def normalize_plan_key(title: str) -> str:
    """Canonical identity of a planned task, derived from its title.

    A replan must not re-mint ``task_id``s (a fresh id makes the
    ``gh:gh_subissue:<task_id>:created`` outbox key look new, which is how
    duplicate GitHub sub-issues get minted). The title is the one field the
    planner reproduces, so we key on it: strip, collapse internal whitespace
    runs, casefold. ``'  Fix   Replan Dedupe '`` -> ``'fix replan dedupe'``.

    Deliberately stdlib-only (this module is a repo layer: sqlite3/json/uuid).
    """
    return " ".join((title or "").split()).casefold()


def _field(row: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a sqlite3.Row or a plain dict, tolerating a missing
    column (``plan_key`` only exists from migration 009 onwards)."""
    try:
        keys = row.keys()
    except AttributeError:
        pass
    else:
        return row[name] if name in keys else default
    try:
        return row[name]
    except (KeyError, IndexError):
        return default


def row_key(row: Any) -> str:
    """Plan identity of an existing task row: its stored ``plan_key``, falling
    back to ``normalize_plan_key(title)`` for rows predating migration 009
    (NULL plan_key) or for schemas without the column at all."""
    plan_key = _field(row, "plan_key")
    if plan_key:
        return str(plan_key)
    return normalize_plan_key(str(_field(row, "title", "") or ""))


def _has_plan_key_column(conn: sqlite3.Connection) -> bool:
    """True when ``tasks.plan_key`` exists (migration 009+).

    The repo layer must stay runnable against a schema that predates the
    column, so every statement that mentions it is built conditionally.
    """
    try:
        rows = conn.execute("PRAGMA table_info(tasks)").fetchall()
    except sqlite3.Error:
        return False
    return any(r[1] == "plan_key" for r in rows)


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


def unblock_stranded(conn: sqlite3.Connection, workflow_id: str) -> list[str]:
    """blocked -> pending once every dependency has reached 'success'.

    Mirror of ``block_dependents``. An operator retry (or a late success)
    repairs the dependency graph, but nothing undid the cascade block, so a
    retried task's dependents stayed 'blocked' forever: ``promote_ready`` only
    looks at 'pending', and ``review_workflow`` skips blocked workflows, so no
    later pass could rescue them. Back to 'pending' rather than straight to
    'ready' so the single existing promotion rule (``deps_satisfied``) stays
    authoritative about ordering. Direct UPDATE, as in block_dependents.
    """
    unblocked: list[str] = []
    while True:
        rows = conn.execute(
            "SELECT task_id, depends_on FROM tasks "
            "WHERE workflow_id=? AND status='blocked'",
            (workflow_id,)).fetchall()
        progressed = False
        for r in rows:
            deps = json.loads(r["depends_on"])
            if deps:
                marks = ",".join("?" * len(deps))
                n = conn.execute(
                    f"SELECT COUNT(*) n FROM tasks WHERE workflow_id=? "
                    f"AND status='success' AND sequence_index IN ({marks})",
                    (workflow_id, *deps)).fetchone()["n"]
                if n != len(deps):
                    continue
            conn.execute(
                "UPDATE tasks SET status='pending', updated_at=? "
                "WHERE task_id=?", (utcnow_s(), r["task_id"]))
            unblocked.append(r["task_id"])
            progressed = True
        if not progressed:
            return unblocked


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
    cols = ["task_id", "workflow_id", "sequence_index", "depends_on", "title",
            "objective", "acceptance", "brief_json", "status", "max_attempts",
            "gh_issue_number"]
    vals: list[Any] = [task_id, workflow_id, sequence_index,
                       json.dumps(depends_on or []), title, objective,
                       acceptance, json.dumps(brief), "pending", max_attempts,
                       gh_issue_number]
    if _has_plan_key_column(conn):
        cols.append("plan_key")
        vals.append(normalize_plan_key(title))
    conn.execute(
        f"INSERT INTO tasks({', '.join(cols)}) "
        f"VALUES({', '.join('?' * len(cols))})", vals)
    return task_id


def reconcile_plan(conn: sqlite3.Connection, workflow_id: str,
                   planned: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply a (re)plan to ``workflow_id`` in place, preserving task identity.

    Why not delete-and-reinsert: ``delete_workflow_tasks`` mints brand-new
    ``task_id``s on every replan, so the outbox key
    ``gh:gh_subissue:<task_id>:created`` looks unseen and GitHub mints a
    duplicate sub-issue for work that already has one. Identity here is
    ``row_key()`` (``plan_key``, else ``normalize_plan_key(title)``): a plan
    entry that matches an existing row UPDATES that row and keeps its
    ``task_id`` (and therefore its ``gh_issue_number``) forever.

    Rules:
      * matched entry -> UPDATE in place of sequence_index, objective,
        acceptance, brief_json, depends_on, max_attempts, plan_key,
        updated_at. ``task_id``, ``gh_issue_number`` and ``status`` are never
        written by a match.
      * duplicate plan_keys inside ONE incoming plan collapse; first wins.
      * existing rows whose plan_key is absent from the new plan are reported
        as ``stale`` and set to 'cancelled' -- including rows that are already
        terminal ``failed`` or ``blocked``, because a superseded failure would
        otherwise hold ``workflow_outcome`` at 'failed'/'blocked' forever;
        those flips are also reported under ``superseded_terminal``. The one
        exception is ``success``: a delivered row keeps its status (it records
        shipped work and must never block 'done'). Already-'cancelled' rows are
        reported as stale but not rewritten. Nothing is ever DELETEd here (that
        is what keeps GitHub sub-issues mapped).
      * unmatched entries are inserted via ``create_task``.

    ``sequence_index`` is UNIQUE per workflow, so renumbering runs in two
    passes: matched rows are first parked on private scratch indices (a
    re-ordered plan can never transiently collide with itself), then written
    back in ascending order. A leftover cancelled row sitting on a needed slot
    is parked too - its row stays, only its ordering slot moves.

    Stdlib only (sqlite3/json), like the rest of this module. The caller owns
    the transaction (wrap in ``conn.immediate``), as elsewhere here.

    Note a re-added title matches its cancelled row and is therefore NOT
    resurrected: status is left alone, so reviving planned work stays an
    explicit decision for the caller (planner side, out of scope here).

    Returns {'created': [task_id...], 'reused': {plan_key: task_id},
             'stale': [task_id...], 'superseded_terminal': [task_id...]}.
    """
    rows = list_tasks(conn, workflow_id)
    has_plan_key = _has_plan_key_column(conn)

    # winner per plan_key: list_tasks is ORDER BY sequence_index, so the
    # lowest sequence_index wins, matching the dedupe rule in 009.
    existing: dict[str, sqlite3.Row] = {}
    for r in rows:
        existing.setdefault(row_key(r), r)

    entries: dict[str, dict[str, Any]] = {}
    for entry in (planned or []):
        key = normalize_plan_key(str(entry.get("title") or ""))
        if key not in entries:          # duplicate plan_key: first occurrence wins
            entries[key] = entry

    def as_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    now = utcnow_s()
    scratch = min([0] + [r["sequence_index"] for r in rows]) - 1

    def park(task_id: str) -> None:
        """Move a row off the sequence_index ladder onto a private slot."""
        nonlocal scratch
        scratch -= 1
        conn.execute("UPDATE tasks SET sequence_index=?, updated_at=? "
                     "WHERE task_id=?", (scratch, now, task_id))

    def vacate(seq: int, keep: Optional[str]) -> None:
        """Free ``seq``: whoever else in this workflow holds it gets parked."""
        sql = ("SELECT task_id FROM tasks WHERE workflow_id=? AND sequence_index=?"
               + (" AND task_id<>?" if keep else ""))
        vals: list[Any] = [workflow_id, seq] + ([keep] if keep else [])
        holder = conn.execute(sql, vals).fetchone()
        if holder is not None:
            park(holder["task_id"])

    taken: set[int] = set()

    def target_seq(entry: dict[str, Any], fallback: int) -> int:
        seq = as_int(entry.get("sequence_index"), fallback)
        while seq in taken:            # malformed plan: two entries, one slot
            seq += 1
        taken.add(seq)
        return seq

    created: list[str] = []
    reused: dict[str, str] = {}
    stale: list[str] = []
    superseded_terminal: list[str] = []

    # 1. stale first (status only; the row stays)
    for r in rows:
        key = row_key(r)
        if key in entries and existing.get(key) is r:
            continue
        stale.append(r["task_id"])
        status = r["status"]
        if status == "success":
            continue                   # delivered work is never rewritten
        if status in SUPERSEDE_TERMINAL:
            # superseded failure/block: flip it, and tell the caller it was
            # a terminal row we had to override
            conn.execute("UPDATE tasks SET status='cancelled', updated_at=? "
                         "WHERE task_id=?", (now, r["task_id"]))
            superseded_terminal.append(r["task_id"])
        elif status not in TERMINAL:
            conn.execute("UPDATE tasks SET status='cancelled', updated_at=? "
                         "WHERE task_id=?", (now, r["task_id"]))

    matched = [(k, e) for k, e in entries.items() if k in existing]
    fresh = [(k, e) for k, e in entries.items() if k not in existing]

    # 2. park the matched rows so pass 3 can write any numbering it likes
    for k, _e in matched:
        park(existing[k]["task_id"])

    # 3. update matched rows in place
    for i, (key, entry) in enumerate(matched, start=1):
        row = existing[key]
        seq = target_seq(entry, i)
        vacate(seq, row["task_id"])
        sets = ["sequence_index=?", "objective=?", "acceptance=?",
                "brief_json=?", "depends_on=?", "max_attempts=?"]
        vals: list[Any] = [seq, str(entry.get("objective") or ""),
                           str(entry.get("acceptance") or ""),
                           json.dumps(entry.get("brief") or {}),
                           json.dumps([as_int(d, 0)
                                       for d in (entry.get("depends_on") or [])]),
                           as_int(entry.get("max_attempts"), 3)]
        if has_plan_key:
            sets.append("plan_key=?")
            vals.append(key)
        sets.append("updated_at=?")
        vals += [now, row["task_id"]]
        conn.execute(
            f"UPDATE tasks SET {', '.join(sets)} WHERE task_id=?", vals)
        reused[key] = row["task_id"]

    # 4. everything else is new
    for i, (key, entry) in enumerate(fresh, start=1):
        seq = target_seq(entry, i)
        vacate(seq, None)
        created.append(create_task(
            conn, workflow_id, seq, str(entry.get("title") or ""),
            str(entry.get("objective") or ""), str(entry.get("acceptance") or ""),
            entry.get("brief") or {},
            [as_int(d, 0) for d in (entry.get("depends_on") or [])],
            as_int(entry.get("max_attempts"), 3)))

    return {"created": created, "reused": reused, "stale": stale,
            "superseded_terminal": superseded_terminal}


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
    """blocked -> pending (deps repaired by a retry), then pending -> ready
    where all deps are success. Returns the task_ids that became ready."""
    for wf in conn.execute(
            "SELECT DISTINCT workflow_id FROM tasks "
            "WHERE status='blocked'").fetchall():
        unblock_stranded(conn, wf["workflow_id"])
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


def _running_lease(row: sqlite3.Row, extend_s: int) -> str:
    """Lease = now + extend_s. NO wall cap: a task may run as long as its
    session evidence keeps advancing. The lease is set at on_started and
    renewed ONLY by the watchdog (renew_lease) from external evidence —
    executor heartbeats are metrics, never lease proof. Death is detected by
    the watchdog's evidence checks, not by elapsed duration."""
    import time
    return epoch_to_iso(time.time() + extend_s)


def on_started(conn: sqlite3.Connection, task_id: str, attempt: int,
               lease_token: str, executor_id: str,
               session_ref: str, extend_s: int = 180) -> Optional[sqlite3.Row]:
    """dispatched -> running, fenced on (task_id, attempt, lease_token).

    Also re-adopts an already-running task with the SAME attempt+token: an
    autoclaimed runner restarts the chat with a fresh session_ref, and the
    watchdog's session evidence must follow it (live case: runner recreated
    mid-task, DB kept pointing at the dead session)."""
    row = get_task(conn, task_id)
    if row is None:
        return None
    with conn.execute(
        "UPDATE tasks SET status='running', executor_id=?, session_ref=?, "
        "started_at=?, lease_expires_at=?, updated_at=? "
        "WHERE task_id=? AND attempt=? AND lease_token=? "
        "AND status IN ('dispatched','running') "
        "RETURNING *",
        (executor_id, session_ref, utcnow_s(),
         _running_lease(row, extend_s),
         utcnow_s(), task_id, attempt, lease_token)) as cur:
        return cur.fetchone()


def on_heartbeat(conn: sqlite3.Connection, task_id: str, attempt: int,
                 lease_token: str, *, turns_used: int = -1,
                 elapsed_s: int = 0) -> Optional[sqlite3.Row]:
    """Metrics only — the lease is renewed by the watchdog from session
    evidence (renew_lease), never by executor beats.

    The heartbeat payload (turns_used = session message count, -1 unknown)
    is stored so the watchdog's liveness monitor and the turn-budget check
    can see an alive-but-stuck agent — alert only, never kill.
    """
    row = get_task(conn, task_id)
    if row is None or row["status"] != "running":
        return None
    if row["attempt"] != attempt or row["lease_token"] != lease_token:
        return None
    # progress_ts advances only when the turn count actually grows, so the
    # stall probe measures "no progress", not "no heartbeat"
    now = utcnow_s()
    prev = {}
    if "heartbeat_json" in row.keys():
        try:
            prev = json.loads(row["heartbeat_json"] or "{}")
        except ValueError:
            prev = {}
    progressed = turns_used >= 0 and turns_used != prev.get("turns_used", -2)
    progress_ts = now if progressed else prev.get("progress_ts", now)
    hb = json.dumps({"turns_used": turns_used, "elapsed_s": elapsed_s,
                     "ts": now, "progress_ts": progress_ts})
    return conn.execute(
        "UPDATE tasks SET updated_at=?, heartbeat_json=? "
        "WHERE task_id=? RETURNING *",
        (now, hb, task_id)).fetchone()


def renew_lease(conn: sqlite3.Connection, task_id: str, attempt: int,
                lease_token: str, extend_s: int) -> Optional[sqlite3.Row]:
    """Watchdog-side lease renewal, fenced on (attempt, lease_token).

    Called only when external evidence (Claudio session growth) proves the
    task is alive. running-only; a reaped/re-dispatched task's stale token
    can never renew."""
    row = get_task(conn, task_id)
    if (row is None or row["status"] != "running"
            or row["attempt"] != attempt or row["lease_token"] != lease_token):
        return None
    return conn.execute(
        "UPDATE tasks SET lease_expires_at=?, updated_at=? "
        "WHERE task_id=? AND status='running' AND attempt=? AND lease_token=? "
        "RETURNING *",
        (_running_lease(row, extend_s), utcnow_s(),
         task_id, attempt, lease_token)).fetchone()


def requeue_infra(conn: sqlite3.Connection, task_id: str) -> Optional[sqlite3.Row]:
    """Infra fault (claudio unreachable): running -> ready WITHOUT burning the
    attempt. claim_for_dispatch increments attempt at claim time, so we give
    it back and the next claim reuses the same number. The lease token is
    cleared, so a zombie executor's late result stays fenced even when the
    same attempt number is re-dispatched."""
    row = get_task(conn, task_id)
    if row is None or row["status"] != "running":
        return None
    extra = {"lease_token": None, "lease_expires_at": None,
             "executor_id": None, "attempt": max(0, row["attempt"] - 1)}
    return transition(conn, task_id, "ready", **extra)


def on_question(conn: sqlite3.Connection, task_id: str, attempt: int,
                lease_token: str) -> Optional[sqlite3.Row]:
    return transition_fenced(conn, task_id, attempt, lease_token,
                             "waiting_input", from_status="running")


#: error classes that will fail identically on every retry (broken config,
#: bad credentials, missing repo) — retrying only burns attempts and time.
#: The task goes straight to `failed` with an actionable alert instead.
CONFIG_CLASS_ERRORS = ("claudio:auth", "claudio:config", "infra:config")

#: transient infrastructure faults (claudio container down/unreachable):
#: not the agent's fault — requeue WITHOUT burning the attempt. Infinite
#: retry is safe because the watchdog pauses dispatch while claudio is
#: critically failing its health probe.
INFRA_ERROR_CODES = ("claudio:unavailable",)


def on_result(conn: sqlite3.Connection, task_id: str, attempt: int,
              lease_token: str, outcome: str, gate_passed: bool,
              result: dict[str, Any],
              error_code: str = "") -> Optional[sqlite3.Row]:
    """Apply a task.result with fencing. Returns the updated row or None if fenced.

    Retry policy: a config-class error_code skips the retry ladder entirely
    (fail fast + alert); any other failure retries while attempts remain.
    """
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
    config_class = error_code.startswith(CONFIG_CLASS_ERRORS)
    if error_code.startswith(INFRA_ERROR_CODES):
        # infra fault: free requeue, attempt preserved (give back the number
        # claim_for_dispatch took at claim time)
        extra["lease_token"] = None
        extra["lease_expires_at"] = None
        extra["executor_id"] = None
        extra["attempt"] = max(0, row["attempt"] - 1)
        return transition(conn, task_id, "ready", **extra)
    # failed / gate fail: retry if attempts remain (config-class never does)
    if not config_class and row["attempt"] < row["max_attempts"]:
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
    """Running tasks whose lease lapsed — last-resort net only: the watchdog
    renews leases from session evidence every probe, so this fires only if
    the planner itself was dead longer than the lease TTL. Dispatched tasks
    are NOT expired: an unclaimed message simply waits in Redis (PEL +
    XAUTOCLAIM redeliver it when an executor returns)."""
    return conn.execute(
        "SELECT * FROM tasks WHERE status = 'running' "
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
