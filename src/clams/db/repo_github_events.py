"""github_events: the durable, normalized webhook intake ledger.

The API is the only writer of a *row* here (one per accepted delivery, keyed by
``delivery_id``); the planner consumes it. Nothing in this table is raw payload:
only the bounded, redacted fields produced by
``clams.github.webhooks.normalize_event``.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from ..github.webhooks import NormalizedEvent
from .conn import immediate, utcnow_s
from .repo_commands import insert_command

#: Command-inbox kind for an accepted GitHub delivery. The planner drains it;
#: an unknown kind is marked rejected (never silently dropped).
COMMAND_KIND = "github_event"

STATES = ("new", "processing", "done", "failed", "skipped")

_COLUMNS = ("delivery_id", "repo", "event_action", "pr_number", "head_ref",
            "head_sha", "check_id", "summary")


def insert_event(conn: sqlite3.Connection, record: NormalizedEvent) -> bool:
    """INSERT OR IGNORE one normalized delivery.

    Returns ``True`` when the row is new, ``False`` when ``delivery_id`` was
    already present (a GitHub redelivery) - i.e. the duplicate is a no-op.
    """
    row = record.as_row()
    values = [row[c] for c in _COLUMNS]
    cur = conn.execute(
        "INSERT OR IGNORE INTO github_events(" + ", ".join(_COLUMNS) + ") "
        "VALUES (" + ", ".join("?" * len(_COLUMNS)) + ")", values)
    return cur.rowcount > 0


def record_delivery(conn: sqlite3.Connection, record: NormalizedEvent,
                    issued_by: str = "github-webhook"
                    ) -> tuple[bool, Optional[int]]:
    """Persist the delivery and enqueue its command, atomically.

    Returns ``(is_new, command_id)``. Because the ledger row is written first
    inside one ``BEGIN IMMEDIATE`` transaction, a duplicate ``delivery_id``
    can never enqueue work twice.
    """
    with immediate(conn):
        if not insert_event(conn, record):
            return False, None
        command_id = insert_command(
            conn, COMMAND_KIND, record.as_command_payload(), issued_by,
            target=record.repo)
    return True, command_id


def get_event(conn: sqlite3.Connection,
              delivery_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM github_events WHERE delivery_id=?",
                        (delivery_id,)).fetchone()


def list_events(conn: sqlite3.Connection, state: Optional[str] = None,
                limit: int = 50) -> list[sqlite3.Row]:
    limit = max(1, min(int(limit), 500))
    if state:
        return conn.execute(
            "SELECT * FROM github_events WHERE processed_state=? "
            "ORDER BY received_at, delivery_id LIMIT ?", (state, limit)
        ).fetchall()
    return conn.execute(
        "SELECT * FROM github_events ORDER BY received_at DESC, delivery_id "
        "LIMIT ?", (limit,)).fetchall()


def mark_processed(conn: sqlite3.Connection, delivery_id: str,
                   state: str) -> None:
    if state not in STATES:
        raise ValueError(f"bad processed_state {state!r}")
    conn.execute("UPDATE github_events SET processed_state=? "
                 "WHERE delivery_id=?", (state, delivery_id))


def counts_by_state(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT processed_state, COUNT(*) n FROM github_events "
        "GROUP BY processed_state").fetchall()
    return {r["processed_state"]: r["n"] for r in rows}



# --------------------------------------------------- planner-side consumption
#: follow-up side-effect kinds (gh_followups.kind, migration 014)
FOLLOWUP_KINDS = ("task", "handoff", "outbox_comment", "outbox_label",
                  "timeline")

#: correlation / classification / disposition vocabularies stored on the row
#: (written by clams.planner.github_feedback; kept as plain TEXT columns)
CORRELATIONS = ("", "matched", "unmatched", "stale_sha", "ambiguous",
                "unknown_repo")
CLASSIFICATIONS = ("", "none", "code", "infra_config")
DISPOSITIONS = ("", "audit_only", "timeline", "handoff", "dispatch",
                "rejected")


def event_from_payload(payload: dict) -> "NormalizedEvent":
    """Rebuild the bounded record from a stored row / command-inbox payload.

    The planner never re-parses a raw delivery (it does not keep one), so this
    is the only path back to a :class:`NormalizedEvent`. Unknown keys are
    ignored; a missing ``event``/``action`` pair is reconstructed from
    ``event_action`` (``"check_suite.completed"``).
    """
    if not isinstance(payload, dict):
        raise ValueError("github event payload must be an object")
    event = str(payload.get("event") or "")
    action = str(payload.get("action") or "")
    if not (event and action):
        combined = str(payload.get("event_action") or "")
        name, _, tail = combined.partition(".")
        event = event or name
        action = action or tail
    number = payload.get("pr_number")

    def _opt(key: str, limit: int) -> Optional[str]:
        value = payload.get(key)
        return None if value is None else str(value)[:limit]

    return NormalizedEvent(
        delivery_id=str(payload.get("delivery_id") or "")[:64],
        event=event[:80], action=action[:80],
        repo=str(payload.get("repo") or "")[:200],
        pr_number=int(number) if number not in (None, "") else None,
        head_ref=_opt("head_ref", 255), head_sha=_opt("head_sha", 64),
        check_id=_opt("check_id", 64),
        summary=str(payload.get("summary") or "")[:240])


def event_from_row(row: sqlite3.Row) -> "NormalizedEvent":
    """Same reconstruction from a ``github_events`` row."""
    return event_from_payload({
        "delivery_id": row["delivery_id"], "repo": row["repo"],
        "event_action": row["event_action"], "pr_number": row["pr_number"],
        "head_ref": row["head_ref"], "head_sha": row["head_sha"],
        "check_id": row["check_id"], "summary": row["summary"]})


def record_outcome(conn: sqlite3.Connection, delivery_id: str, *,
                   correlation: str, classification: str, disposition: str,
                   outcome: str = "", link_id: Optional[int] = None,
                   task_id: Optional[str] = None,
                   workflow_id: Optional[str] = None,
                   detail: str = "") -> None:
    """Record what the planner *did* with this delivery.

    ``github_events`` is never modified (the API owns those columns); the
    verdict goes to ``gh_feedback``, keyed by the same ``delivery_id``, so one
    delivery has exactly one recorded outcome however often it is replayed.
    """
    if correlation not in CORRELATIONS:
        raise ValueError(f"bad correlation {correlation!r}")
    if classification not in CLASSIFICATIONS:
        raise ValueError(f"bad classification {classification!r}")
    if disposition not in DISPOSITIONS:
        raise ValueError(f"bad disposition {disposition!r}")
    conn.execute(
        "INSERT INTO gh_feedback(delivery_id, correlation, classification, "
        "disposition, outcome, link_id, task_id, workflow_id, detail) "
        "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(delivery_id) DO UPDATE SET "
        "  correlation=excluded.correlation,"
        "  classification=excluded.classification,"
        "  disposition=excluded.disposition,"
        "  outcome=excluded.outcome,"
        "  link_id=excluded.link_id,"
        "  task_id=excluded.task_id,"
        "  workflow_id=excluded.workflow_id,"
        "  detail=excluded.detail",
        (delivery_id, correlation, classification, disposition, outcome,
         link_id, task_id, workflow_id, str(detail or "")[:500]))


def get_outcome(conn: sqlite3.Connection,
                delivery_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM gh_feedback WHERE delivery_id=?",
                        (delivery_id,)).fetchone()


def claim_followup(conn: sqlite3.Connection, delivery_id: str,
                   kind: str, ref_id: str = "") -> bool:
    """Reserve "one ``kind`` side effect for this delivery". True if reserved.

    ``gh_followups`` has PRIMARY KEY (delivery_id, kind), so a replayed
    delivery (GitHub redelivery, or a planner restart between the ledger commit
    and the command being marked applied) gets ``False`` and must do nothing.
    The caller holds the transaction.
    """
    if kind not in FOLLOWUP_KINDS:
        raise ValueError(f"bad follow-up kind {kind!r}")
    cur = conn.execute(
        "INSERT OR IGNORE INTO gh_followups(delivery_id, kind, ref_id) "
        "VALUES(?,?,?)", (delivery_id, kind, str(ref_id or "")[:200]))
    if cur.rowcount == 0:
        return False
    if ref_id:
        conn.execute("UPDATE gh_followups SET ref_id=? WHERE delivery_id=? "
                     "AND kind=?", (str(ref_id)[:200], delivery_id, kind))
    return True


def followups_for(conn: sqlite3.Connection, delivery_id: str,
                  kind: Optional[str] = None) -> list[sqlite3.Row]:
    if kind:
        return conn.execute(
            "SELECT * FROM gh_followups WHERE delivery_id=? AND kind=? "
            "ORDER BY kind", (delivery_id, kind)).fetchall()
    return conn.execute(
        "SELECT * FROM gh_followups WHERE delivery_id=? ORDER BY kind",
        (delivery_id,)).fetchall()


def count_events(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) n FROM github_events").fetchone()["n"]


def unprocessed(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    """Ledger rows no planner cycle has consumed yet (restart safety net)."""
    return conn.execute(
        "SELECT * FROM github_events WHERE processed_state='new' "
        "ORDER BY received_at, delivery_id LIMIT ?",
        (max(1, min(int(limit), 200)),)).fetchall()
