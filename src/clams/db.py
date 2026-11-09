"""Durable local state layer: SQLite in WAL mode (spec §5, §8.3).

Single-writer ownership per table:
  * ``work_items``  — Planner / Sync / UI
  * ``task_claims`` — Executor (and Watchdog for expiry)
  * ``alerts``      — Watchdog
  * ``outbox``      — Outbox worker
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_items (
  requirement_number  INTEGER PRIMARY KEY,
  requirement_title   TEXT NOT NULL,
  gh_status           TEXT NOT NULL,
  gh_updated_at       TEXT NOT NULL,
  plan_json           TEXT,
  task_numbers        TEXT NOT NULL DEFAULT '[]',
  planner_session_id  TEXT,
  user_paused         INTEGER NOT NULL DEFAULT 0,
  status              TEXT NOT NULL DEFAULT 'pending',
  replans             INTEGER NOT NULL DEFAULT 0,
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_claims (
  task_number         INTEGER PRIMARY KEY,
  requirement_number INTEGER NOT NULL,
  executor_id         TEXT NOT NULL,
  lease_expires_at    TEXT NOT NULL,
  attempts            INTEGER NOT NULL DEFAULT 0,
  max_attempts        INTEGER NOT NULL DEFAULT 3,
  claimed_at          TEXT NOT NULL,
  FOREIGN KEY(requirement_number) REFERENCES work_items(requirement_number)
);

CREATE TABLE IF NOT EXISTS alerts (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  severity            TEXT NOT NULL CHECK(severity IN ('info', 'warning', 'critical')),
  source              TEXT NOT NULL,
  message             TEXT NOT NULL,
  acknowledged        INTEGER NOT NULL DEFAULT 0,
  created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  kind                TEXT NOT NULL CHECK(kind IN ('gh_comment', 'gh_status', 'notification')),
  payload             TEXT NOT NULL,
  idempotency_key     TEXT UNIQUE,
  status              TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'sent', 'failed')),
  attempts            INTEGER NOT NULL DEFAULT 0,
  created_at          TEXT NOT NULL,
  sent_at             TEXT
);

CREATE INDEX IF NOT EXISTS idx_work_items_status ON work_items(status);
CREATE INDEX IF NOT EXISTS idx_work_items_gh_status ON work_items(gh_status);
CREATE INDEX IF NOT EXISTS idx_task_claims_expiry ON task_claims(lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime] = None) -> str:
    return (dt or utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class Database:
    """Owns the SQLite connection and applies the spec's PRAGMAs."""

    def __init__(self, path: str | Path):
        self.path = str(Path(path).expanduser().resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    # -- lifecycle ---------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(
                self.path,
                timeout=5.0,
                isolation_level=None,  # explicit transaction control
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = FULL;")
            conn.execute("PRAGMA busy_timeout = 5000;")
            conn.execute("PRAGMA wal_autocheckpoint = 1000;")
            conn.execute("PRAGMA foreign_keys = ON;")
            self._conn = conn
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        return self.connect()

    def migrate(self) -> None:
        self.conn.executescript(SCHEMA)

    # -- introspection -----------------------------------------------------
    def journal_mode(self) -> str:
        return self.conn.execute("PRAGMA journal_mode;").fetchone()[0]

    def ping(self) -> float:
        """BEGIN IMMEDIATE / ROLLBACK round-trip duration (watchdog check)."""
        start = utcnow()
        self.conn.execute("BEGIN IMMEDIATE;")
        self.conn.execute("ROLLBACK;")
        return (utcnow() - start).total_seconds()


@dataclass
class WorkItem:
    requirement_number: int
    requirement_title: str
    gh_status: str
    gh_updated_at: str
    plan_json: Optional[str] = None
    task_numbers: list[int] = field(default_factory=list)
    planner_session_id: Optional[str] = None
    user_paused: int = 0
    status: str = "pending"
    replans: int = 0
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "WorkItem":
        d = dict(row)
        d["task_numbers"] = json.loads(d.get("task_numbers") or "[]")
        return cls(**d)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "requirement_number": self.requirement_number,
            "requirement_title": self.requirement_title,
            "gh_status": self.gh_status,
            "gh_updated_at": self.gh_updated_at,
            "plan_json": self.plan_json,
            "task_numbers": json.dumps(self.task_numbers),
            "planner_session_id": self.planner_session_id,
            "user_paused": self.user_paused,
            "status": self.status,
            "replans": self.replans,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        return d


class WorkItemRepo:
    """Single writer: Planner / Sync / UI."""

    def __init__(self, db: Database):
        self.db = db

    def upsert(self, item: WorkItem) -> WorkItem:
        now = iso()
        if not item.created_at:
            item.created_at = now
        item.updated_at = now
        self.db.conn.execute(
            """
            INSERT INTO work_items (requirement_number, requirement_title, gh_status,
              gh_updated_at, plan_json, task_numbers, planner_session_id, user_paused,
              status, replans, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(requirement_number) DO UPDATE SET
              requirement_title = excluded.requirement_title,
              gh_status = excluded.gh_status,
              gh_updated_at = excluded.gh_updated_at,
              plan_json = excluded.plan_json,
              task_numbers = excluded.task_numbers,
              planner_session_id = excluded.planner_session_id,
              status = excluded.status,
              updated_at = excluded.updated_at
            """,
            (
                item.requirement_number,
                item.requirement_title,
                item.gh_status,
                item.gh_updated_at,
                item.plan_json,
                json.dumps(item.task_numbers),
                item.planner_session_id,
                item.user_paused,
                item.status,
                item.replans,
                item.created_at,
                item.updated_at,
            ),
        )
        return item

    def get(self, requirement_number: int) -> Optional[WorkItem]:
        row = self.db.conn.execute(
            "SELECT * FROM work_items WHERE requirement_number = ?",
            (requirement_number,),
        ).fetchone()
        return WorkItem.from_row(row) if row else None

    def exists(self, requirement_number: int) -> bool:
        row = self.db.conn.execute(
            "SELECT 1 FROM work_items WHERE requirement_number = ?",
            (requirement_number,),
        ).fetchone()
        return row is not None

    def list_by_status(self, status: str) -> list[WorkItem]:
        rows = self.db.conn.execute(
            "SELECT * FROM work_items WHERE status = ? ORDER BY requirement_number",
            (status,),
        ).fetchall()
        return [WorkItem.from_row(r) for r in rows]

    def all(self) -> list[WorkItem]:
        rows = self.db.conn.execute(
            "SELECT * FROM work_items ORDER BY requirement_number"
        ).fetchall()
        return [WorkItem.from_row(r) for r in rows]

    def set_paused(self, requirement_number: int, paused: bool) -> None:
        self.db.conn.execute(
            "UPDATE work_items SET user_paused = ?, updated_at = ? WHERE requirement_number = ?",
            (1 if paused else 0, iso(), requirement_number),
        )

    def is_paused(self, requirement_number: int) -> bool:
        row = self.db.conn.execute(
            "SELECT user_paused FROM work_items WHERE requirement_number = ?",
            (requirement_number,),
        ).fetchone()
        return bool(row and row[0])

    def set_status(self, requirement_number: int, status: str) -> None:
        self.db.conn.execute(
            "UPDATE work_items SET status = ?, updated_at = ? WHERE requirement_number = ?",
            (status, iso(), requirement_number),
        )

    def set_plan(self, requirement_number: int, plan_json: str, task_numbers: Iterable[int],
                title: Optional[str] = None) -> None:
        """Persist a plan. Creates the row if intake has not created it yet."""
        now = iso()
        existing = self.get(requirement_number)
        if existing is None:
            self.db.conn.execute(
                """INSERT INTO work_items (requirement_number, requirement_title, gh_status,
                     gh_updated_at, plan_json, task_numbers, planner_session_id, user_paused,
                     status, replans, created_at, updated_at)
                   VALUES (?, ?, 'In Progress', ?, ?, ?, NULL, 0, 'planned', 0, ?, ?)""",
                (requirement_number, title or f"Requirement #{requirement_number}",
                 now, plan_json, json.dumps(list(task_numbers)), now, now),
            )
            return
        self.db.conn.execute(
            """UPDATE work_items SET plan_json = ?, task_numbers = ?, status = ?, updated_at = ?
               WHERE requirement_number = ?""",
            (plan_json, json.dumps(list(task_numbers)), "planned", now, requirement_number),
        )

    def increment_replans(self, requirement_number: int) -> int:
        self.db.conn.execute(
            "UPDATE work_items SET replans = replans + 1, updated_at = ? WHERE requirement_number = ?",
            (iso(), requirement_number),
        )
        row = self.db.conn.execute(
            "SELECT replans FROM work_items WHERE requirement_number = ?",
            (requirement_number,),
        ).fetchone()
        return int(row[0]) if row else 0

    def claim_requirement(self, requirement_number: int, title: str, gh_updated_at: str) -> bool:
        """Atomically take ownership of a requirement. True if this caller won."""
        now = iso()
        cur = self.db.conn.execute(
            """
            INSERT INTO work_items (requirement_number, requirement_title, gh_status,
              gh_updated_at, plan_json, task_numbers, planner_session_id, user_paused,
              status, replans, created_at, updated_at)
            VALUES (?, ?, 'In Progress', ?, NULL, '[]', NULL, 0, 'claimed', 0, ?, ?)
            ON CONFLICT(requirement_number) DO NOTHING
            """,
            (requirement_number, title, gh_updated_at, now, now),
        )
        return cur.rowcount == 1


class TaskClaimRepo:
    """Single writer: Executor (Watchdog may release expired leases)."""

    def __init__(self, db: Database):
        self.db = db

    def claim(self, task_number: int, requirement_number: int, executor_id: str,
             lease_seconds: int, max_attempts: int = 3) -> bool:
        """Atomically claim a task if unclaimed or its lease has expired.

        A task that has already burned through ``max_attempts`` can never be
        claimed again — it must be re-planned instead (spec HW-4).
        """
        now = utcnow()
        expires = iso(now + timedelta(seconds=lease_seconds))
        self.db.conn.execute("BEGIN IMMEDIATE;")
        try:
            row = self.db.conn.execute(
                "SELECT lease_expires_at, attempts, max_attempts FROM task_claims "
                "WHERE task_number = ?",
                (task_number,),
            ).fetchone()
            if row is None:
                self.db.conn.execute(
                    """INSERT INTO task_claims (task_number, requirement_number, executor_id,
                         lease_expires_at, attempts, max_attempts, claimed_at)
                       VALUES (?, ?, ?, ?, 0, ?, ?)""",
                    (task_number, requirement_number, executor_id, expires, max_attempts, iso(now)),
                )
                self.db.conn.execute("COMMIT;")
                return True
            if int(row["attempts"]) >= int(row["max_attempts"] or max_attempts):
                self.db.conn.execute("COMMIT;")
                return False
            if parse_iso(row["lease_expires_at"]) <= now:
                self.db.conn.execute(
                    """UPDATE task_claims SET executor_id = ?, lease_expires_at = ?, claimed_at = ?
                       WHERE task_number = ?""",
                    (executor_id, expires, iso(now), task_number),
                )
                self.db.conn.execute("COMMIT;")
                return True
            self.db.conn.execute("COMMIT;")
            return False
        except Exception:
            self.db.conn.execute("ROLLBACK;")
            raise

    def expire(self, task_number: int) -> None:
        """Expire a lease immediately so another executor may take the task."""
        self.db.conn.execute(
            "UPDATE task_claims SET lease_expires_at = ? WHERE task_number = ?",
            (iso(utcnow()), task_number),
        )

    def renew(self, task_number: int, executor_id: str, lease_seconds: int) -> bool:
        expires = iso(utcnow() + timedelta(seconds=lease_seconds))
        cur = self.db.conn.execute(
            """UPDATE task_claims SET lease_expires_at = ?
               WHERE task_number = ? AND executor_id = ?""",
            (expires, task_number, executor_id),
        )
        return cur.rowcount == 1

    def release(self, task_number: int) -> None:
        self.db.conn.execute("DELETE FROM task_claims WHERE task_number = ?", (task_number,))

    def increment_attempts(self, task_number: int) -> int:
        self.db.conn.execute(
            "UPDATE task_claims SET attempts = attempts + 1 WHERE task_number = ?",
            (task_number,),
        )
        row = self.db.conn.execute(
            "SELECT attempts FROM task_claims WHERE task_number = ?", (task_number,)
        ).fetchone()
        return int(row[0]) if row else 0

    def get(self, task_number: int) -> Optional[sqlite3.Row]:
        return self.db.conn.execute(
            "SELECT * FROM task_claims WHERE task_number = ?", (task_number,)
        ).fetchone()

    def expired(self, at: Optional[datetime] = None) -> list[sqlite3.Row]:
        return self.db.conn.execute(
            "SELECT * FROM task_claims WHERE lease_expires_at <= ?", (iso(at or utcnow()),)
        ).fetchall()

    def active(self) -> list[sqlite3.Row]:
        return self.db.conn.execute(
            "SELECT * FROM task_claims WHERE lease_expires_at > ?", (iso(utcnow()),)
        ).fetchall()


class AlertRepo:
    """Single writer: Watchdog."""

    SEVERITIES = ("info", "warning", "critical")

    def __init__(self, db: Database):
        self.db = db

    def add(self, severity: str, source: str, message: str) -> int:
        if severity not in self.SEVERITIES:
            raise ValueError(f"invalid severity {severity!r}")
        cur = self.db.conn.execute(
            "INSERT INTO alerts (severity, source, message, acknowledged, created_at) VALUES (?, ?, ?, 0, ?)",
            (severity, source, message, iso()),
        )
        return int(cur.lastrowid)

    def list_unacked(self, severity: Optional[str] = None) -> list[sqlite3.Row]:
        if severity:
            return self.db.conn.execute(
                "SELECT * FROM alerts WHERE acknowledged = 0 AND severity = ? ORDER BY id",
                (severity,),
            ).fetchall()
        return self.db.conn.execute(
            "SELECT * FROM alerts WHERE acknowledged = 0 ORDER BY id"
        ).fetchall()

    def list_all(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.db.conn.execute(
            "SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def ack(self, alert_id: int) -> None:
        self.db.conn.execute(
            "UPDATE alerts SET acknowledged = 1 WHERE id = ?", (alert_id,)
        )

    def count(self) -> int:
        return int(self.db.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0])


class OutboxRepo:
    """Single writer: Outbox worker."""

    KINDS = ("gh_comment", "gh_status", "notification")

    def __init__(self, db: Database):
        self.db = db

    def enqueue(self, kind: str, payload: dict[str, Any], idempotency_key: str) -> int:
        if kind not in self.KINDS:
            raise ValueError(f"invalid outbox kind {kind!r}")
        cur = self.db.conn.execute(
            """INSERT INTO outbox (kind, payload, idempotency_key, status, attempts, created_at)
               VALUES (?, ?, ?, 'pending', 0, ?)
               ON CONFLICT(idempotency_key) DO NOTHING""",
            (kind, json.dumps(payload), idempotency_key, iso()),
        )
        if cur.lastrowid:
            return int(cur.lastrowid)
        row = self.db.conn.execute(
            "SELECT id FROM outbox WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        return int(row["id"])

    def claim_pending(self, limit: int = 20) -> list[sqlite3.Row]:
        self.db.conn.execute("BEGIN IMMEDIATE;")
        try:
            rows = self.db.conn.execute(
                """SELECT * FROM outbox WHERE status = 'pending' AND attempts < 3
                   ORDER BY id LIMIT ?""",
                (limit,),
            ).fetchall()
            if rows:
                ids = ",".join(str(r["id"]) for r in rows)
                self.db.conn.execute(
                    f"""UPDATE outbox SET status = 'sent', attempts = attempts + 1, sent_at = ?
                        WHERE id IN ({ids}) AND status = 'pending'"""
                    , (iso(),),
                )
            self.db.conn.execute("COMMIT;")
            return rows
        except Exception:
            self.db.conn.execute("ROLLBACK;")
            raise

    def mark(self, outbox_id: int, status: str) -> None:
        if status not in ("pending", "sent", "failed"):
            raise ValueError(f"invalid outbox status {status!r}")
        self.db.conn.execute(
            "UPDATE outbox SET status = ? WHERE id = ?", (status, outbox_id)
        )

    def mark_failed(self, outbox_id: int) -> None:
        self.mark(outbox_id, "failed")

    def requeue_failed(self) -> int:
        cur = self.db.conn.execute(
            "UPDATE outbox SET status = 'pending' WHERE status = 'failed' AND attempts < 3"
        )
        return cur.rowcount

    def stats(self) -> dict[str, int]:
        rows = self.db.conn.execute(
            "SELECT status, COUNT(*) AS n FROM outbox GROUP BY status"
        ).fetchall()
        out = {"pending": 0, "sent": 0, "failed": 0}
        for r in rows:
            out[r["status"]] = int(r["n"])
        return out


@dataclass
class Store:
    """Bundle of all repositories over one database."""

    db: Database
    work_items: WorkItemRepo = field(init=False)
    task_claims: TaskClaimRepo = field(init=False)
    alerts: AlertRepo = field(init=False)
    outbox: OutboxRepo = field(init=False)

    def __post_init__(self) -> None:
        self.work_items = WorkItemRepo(self.db)
        self.task_claims = TaskClaimRepo(self.db)
        self.alerts = AlertRepo(self.db)
        self.outbox = OutboxRepo(self.db)

    def migrate(self) -> None:
        self.db.migrate()


def open_store(path: str | Path) -> Store:
    db = Database(path)
    db.connect()
    db.migrate()
    return Store(db)
