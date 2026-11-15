"""Durable local state layer: SQLite in WAL mode (spec §5, §8.3).

Single-writer ownership per table:
  * ``work_items``  — Planner / Sync / UI
  * ``task_claims`` — Executor (and Watchdog for expiry)
  * ``alerts``      — Watchdog
  * ``outbox``      — Outbox worker
  * ``projects``    — multi-project registry (issue #18)

Multi-project identity
---------------------
GitHub issue numbers are *repository*-scoped, not global: two different repos
can both have an issue ``#15``. A single-repo deployment hides this, but as
soon as Clams processes more than one project the bare ``requirement_number``
/ ``task_number`` stop being unique and silently collide.

Every work-plane row therefore carries a ``project_id`` and is identified by
the composite ``(project_id, <number>)``. The column defaults to ``0`` so
single-repo callers (and the original test suite) keep working unchanged;
multi-project code passes the real project id explicitly.
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
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id          INTEGER NOT NULL DEFAULT 0,
  requirement_number  INTEGER NOT NULL,
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
  updated_at          TEXT NOT NULL,
  UNIQUE(project_id, requirement_number)
);

CREATE TABLE IF NOT EXISTS task_claims (
  task_number         INTEGER NOT NULL,
  project_id          INTEGER NOT NULL DEFAULT 0,
  requirement_number INTEGER NOT NULL,
  executor_id         TEXT NOT NULL,
  lease_expires_at    TEXT NOT NULL,
  attempts            INTEGER NOT NULL DEFAULT 0,
  max_attempts        INTEGER NOT NULL DEFAULT 3,
  claimed_at          TEXT NOT NULL,
  PRIMARY KEY (project_id, task_number),
  FOREIGN KEY(project_id, requirement_number)
    REFERENCES work_items(project_id, requirement_number)
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

CREATE TABLE IF NOT EXISTS projects (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  name                TEXT NOT NULL,
  url                 TEXT NOT NULL UNIQUE,
  status              TEXT NOT NULL DEFAULT 'Queued',
  progress            INTEGER NOT NULL DEFAULT 0,
  worktree_path       TEXT,
  default_branch      TEXT,
  error               TEXT,
  paused              INTEGER NOT NULL DEFAULT 0,
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_state (
  key                 TEXT PRIMARY KEY,
  value               TEXT NOT NULL,
  updated_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_work_items_status ON work_items(status);
CREATE INDEX IF NOT EXISTS idx_work_items_gh_status ON work_items(gh_status);
CREATE INDEX IF NOT EXISTS idx_task_claims_expiry ON task_claims(lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status);
"""

# Created after migration rather than in SCHEMA: on a pre-#18 database the
# ``work_items`` table already exists without ``project_id``, so an index on it
# cannot be created until the migration has added the column.
_POST_MIGRATION_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_work_items_project ON work_items(project_id);",
]

# Columns that may be missing from a database created before issue #18.
# ``migrate()`` adds any that are absent so an existing deployment upgrades in
# place instead of requiring a fresh database.
_PROJECT_COLUMNS: list[tuple[str, str]] = [
    ("worktree_path", "TEXT"),
    ("default_branch", "TEXT"),
    ("error", "TEXT"),
    ("paused", "INTEGER NOT NULL DEFAULT 0"),
]


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
        # ``projects`` is referenced by work_items' migration, so create the
        # whole schema first (CREATE ... IF NOT EXISTS is idempotent), then fix
        # up any columns/keys that an older database is missing.
        self.conn.executescript(SCHEMA)
        _migrate_work_items(self.conn)
        _migrate_task_claims(self.conn)
        _add_missing_columns(self.conn, "projects", _PROJECT_COLUMNS)
        for stmt in _POST_MIGRATION_INDEXES:
            self.conn.execute(stmt)

    # -- introspection -----------------------------------------------------
    def journal_mode(self) -> str:
        return self.conn.execute("PRAGMA journal_mode;").fetchone()[0]

    def ping(self) -> float:
        """BEGIN IMMEDIATE / ROLLBACK round-trip duration (watchdog check)."""
        start = utcnow()
        self.conn.execute("BEGIN IMMEDIATE;")
        self.conn.execute("ROLLBACK;")
        return (utcnow() - start).total_seconds()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_missing_columns(conn: sqlite3.Connection, table: str,
                        columns: list[tuple[str, str]]) -> None:
    present = _table_columns(conn, table)
    for name, decl in columns:
        if name not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _migrate_work_items(conn: sqlite3.Connection) -> None:
    """Rebuild ``work_items`` onto a surrogate PK if it still uses the legacy
    ``requirement_number`` primary key (pre-#18 single-repo schema).

    Existing rows are preserved and assigned ``project_id = 0`` (the default
    single-repo bucket). A database that already has the ``project_id`` column
    is left untouched.
    """
    cols = _table_columns(conn, "work_items")
    if "project_id" in cols:
        return
    # Legacy table: requirement_number is the PK and there is no project_id.
    conn.execute("PRAGMA foreign_keys = OFF;")
    try:
        conn.execute("BEGIN;")
        conn.execute(
            """
            CREATE TABLE work_items_new (
              id                  INTEGER PRIMARY KEY AUTOINCREMENT,
              project_id          INTEGER NOT NULL DEFAULT 0,
              requirement_number  INTEGER NOT NULL,
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
              updated_at          TEXT NOT NULL,
              UNIQUE(project_id, requirement_number)
            );
            """
        )
        conn.execute(
            """
            INSERT INTO work_items_new (project_id, requirement_number,
                requirement_title, gh_status, gh_updated_at, plan_json,
                task_numbers, planner_session_id, user_paused, status, replans,
                created_at, updated_at)
            SELECT 0, requirement_number, requirement_title, gh_status,
                gh_updated_at, plan_json, task_numbers, planner_session_id,
                user_paused, status, replans, created_at, updated_at
            FROM work_items
            """
        )
        conn.execute("DROP TABLE work_items;")
        conn.execute("ALTER TABLE work_items_new RENAME TO work_items;")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_work_items_status ON work_items(status);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_work_items_gh_status ON work_items(gh_status);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_work_items_project ON work_items(project_id);"
        )
        conn.execute("COMMIT;")
    except Exception:
        conn.execute("ROLLBACK;")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON;")


def _migrate_task_claims(conn: sqlite3.Connection) -> None:
    """Rebuild ``task_claims`` onto a composite ``(project_id, task_number)``
    PK if it still uses the legacy bare ``task_number`` PK.
    """
    cols = _table_columns(conn, "task_claims")
    if "project_id" in cols:
        return
    conn.execute("PRAGMA foreign_keys = OFF;")
    try:
        conn.execute("BEGIN;")
        conn.execute(
            """
            CREATE TABLE task_claims_new (
              task_number         INTEGER NOT NULL,
              project_id          INTEGER NOT NULL DEFAULT 0,
              requirement_number INTEGER NOT NULL,
              executor_id         TEXT NOT NULL,
              lease_expires_at    TEXT NOT NULL,
              attempts            INTEGER NOT NULL DEFAULT 0,
              max_attempts        INTEGER NOT NULL DEFAULT 3,
              claimed_at          TEXT NOT NULL,
              PRIMARY KEY (project_id, task_number),
              FOREIGN KEY(project_id, requirement_number)
                REFERENCES work_items(project_id, requirement_number)
            );
            """
        )
        conn.execute(
            """
            INSERT INTO task_claims_new (task_number, project_id,
                requirement_number, executor_id, lease_expires_at, attempts,
                max_attempts, claimed_at)
            SELECT task_number, 0, requirement_number, executor_id,
                lease_expires_at, attempts, max_attempts, claimed_at
            FROM task_claims
            """
        )
        conn.execute("DROP TABLE task_claims;")
        conn.execute("ALTER TABLE task_claims_new RENAME TO task_claims;")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_claims_expiry ON task_claims(lease_expires_at);"
        )
        conn.execute("COMMIT;")
    except Exception:
        conn.execute("ROLLBACK;")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON;")


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
    project_id: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "WorkItem":
        d = dict(row)
        d.pop("id", None)  # surrogate PK is internal, not part of the model
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
            "project_id": self.project_id,
        }
        return d


class WorkItemRepo:
    """Single writer: Planner / Sync / UI.

    Every method takes ``project_id`` as a trailing argument defaulting to 0 so
    the original single-repo call sites keep working. Multi-project callers pass
    the real project id; the composite ``(project_id, requirement_number)`` is
    the true identity.
    """

    def __init__(self, db: Database):
        self.db = db

    def upsert(self, item: WorkItem) -> WorkItem:
        now = iso()
        if not item.created_at:
            item.created_at = now
        item.updated_at = now
        self.db.conn.execute(
            """
            INSERT INTO work_items (project_id, requirement_number, requirement_title,
              gh_status, gh_updated_at, plan_json, task_numbers, planner_session_id,
              user_paused, status, replans, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, requirement_number) DO UPDATE SET
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
                item.project_id,
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

    def get(self, requirement_number: int, project_id: int = 0) -> Optional[WorkItem]:
        row = self.db.conn.execute(
            "SELECT * FROM work_items WHERE project_id = ? AND requirement_number = ?",
            (project_id, requirement_number),
        ).fetchone()
        return WorkItem.from_row(row) if row else None

    def exists(self, requirement_number: int, project_id: int = 0) -> bool:
        row = self.db.conn.execute(
            "SELECT 1 FROM work_items WHERE project_id = ? AND requirement_number = ?",
            (project_id, requirement_number),
        ).fetchone()
        return row is not None

    def list_by_status(self, status: str,
                      project_id: Optional[int] = None) -> list[WorkItem]:
        if project_id is None:
            rows = self.db.conn.execute(
                "SELECT * FROM work_items WHERE status = ? "
                "ORDER BY project_id, requirement_number",
                (status,),
            ).fetchall()
        else:
            rows = self.db.conn.execute(
                "SELECT * FROM work_items WHERE status = ? AND project_id = ? "
                "ORDER BY requirement_number",
                (status, project_id),
            ).fetchall()
        return [WorkItem.from_row(r) for r in rows]

    def all(self) -> list[WorkItem]:
        rows = self.db.conn.execute(
            "SELECT * FROM work_items ORDER BY project_id, requirement_number"
        ).fetchall()
        return [WorkItem.from_row(r) for r in rows]

    def set_paused(self, requirement_number: int, paused: bool,
                  project_id: int = 0) -> None:
        self.db.conn.execute(
            "UPDATE work_items SET user_paused = ?, updated_at = ? "
            "WHERE project_id = ? AND requirement_number = ?",
            (1 if paused else 0, iso(), project_id, requirement_number),
        )

    def set_paused_all(self, paused: bool) -> int:
        """Pause or resume *every* tracked work item, across every project.

        The global topbar controls have to be symmetric. ``set_paused`` is scoped
        to one ``(project_id, requirement_number)`` pair, so a global pause that
        walked the rows while passing the default ``project_id=0`` only ever
        touched the legacy project-0 rows and silently no-opped on the rest —
        which left multi-project deployments (issue #18) stuck with every item
        paused and no way to clear it from the UI. Returns rows updated.
        """
        cur = self.db.conn.execute(
            "UPDATE work_items SET user_paused = ?, updated_at = ?",
            (1 if paused else 0, iso()),
        )
        return int(cur.rowcount or 0)

    def set_paused_for_requirement(self, requirement_number: int, paused: bool) -> int:
        """Pause/resume one requirement in every project that tracks it.

        Used by the API, whose request body carries no project id; scoping to a
        single project there would silently miss the row the operator meant.
        """
        cur = self.db.conn.execute(
            "UPDATE work_items SET user_paused = ?, updated_at = ? "
            "WHERE requirement_number = ?",
            (1 if paused else 0, iso(), requirement_number),
        )
        return int(cur.rowcount or 0)

    def is_paused(self, requirement_number: int, project_id: int = 0) -> bool:
        row = self.db.conn.execute(
            "SELECT user_paused FROM work_items WHERE project_id = ? AND requirement_number = ?",
            (project_id, requirement_number),
        ).fetchone()
        return bool(row and row[0])

    def set_status(self, requirement_number: int, status: str,
                  project_id: int = 0) -> None:
        self.db.conn.execute(
            "UPDATE work_items SET status = ?, updated_at = ? "
            "WHERE project_id = ? AND requirement_number = ?",
            (status, iso(), project_id, requirement_number),
        )

    def set_plan(self, requirement_number: int, plan_json: str,
                task_numbers: Iterable[int], title: Optional[str] = None,
                project_id: int = 0) -> None:
        """Persist a plan. Creates the row if intake has not created it yet."""
        now = iso()
        existing = self.get(requirement_number, project_id)
        if existing is None:
            self.db.conn.execute(
                """INSERT INTO work_items (project_id, requirement_number, requirement_title,
                     gh_status, gh_updated_at, plan_json, task_numbers, planner_session_id,
                     user_paused, status, replans, created_at, updated_at)
                   VALUES (?, ?, ?, 'In Progress', ?, ?, ?, NULL, 0, 'planned', 0, ?, ?)""",
                (project_id, requirement_number,
                 title or f"Requirement #{requirement_number}",
                 now, plan_json, json.dumps(list(task_numbers)), now, now),
            )
            return
        self.db.conn.execute(
            """UPDATE work_items SET plan_json = ?, task_numbers = ?, status = ?, updated_at = ?
               WHERE project_id = ? AND requirement_number = ?""",
            (plan_json, json.dumps(list(task_numbers)), "planned", now,
             project_id, requirement_number),
        )

    def increment_replans(self, requirement_number: int, project_id: int = 0) -> int:
        self.db.conn.execute(
            "UPDATE work_items SET replans = replans + 1, updated_at = ? "
            "WHERE project_id = ? AND requirement_number = ?",
            (iso(), project_id, requirement_number),
        )
        row = self.db.conn.execute(
            "SELECT replans FROM work_items WHERE project_id = ? AND requirement_number = ?",
            (project_id, requirement_number),
        ).fetchone()
        return int(row[0]) if row else 0

    def claim_requirement(self, requirement_number: int, title: str,
                         gh_updated_at: str, project_id: int = 0) -> bool:
        """Atomically take ownership of a requirement. True if this caller won."""
        now = iso()
        cur = self.db.conn.execute(
            """
            INSERT INTO work_items (project_id, requirement_number, requirement_title,
              gh_status, gh_updated_at, plan_json, task_numbers, planner_session_id,
              user_paused, status, replans, created_at, updated_at)
            VALUES (?, ?, ?, 'In Progress', ?, NULL, '[]', NULL, 0, 'claimed', 0, ?, ?)
            ON CONFLICT(project_id, requirement_number) DO NOTHING
            """,
            (project_id, requirement_number, title, gh_updated_at, now, now),
        )
        return cur.rowcount == 1


class TaskClaimRepo:
    """Single writer: Executor (Watchdog may release expired leases).

    Identified by the composite ``(project_id, task_number)``; ``project_id``
    is a trailing default of 0 for single-repo callers.
    """

    def __init__(self, db: Database):
        self.db = db

    def claim(self, task_number: int, requirement_number: int, executor_id: str,
             lease_seconds: int, max_attempts: int = 3,
             project_id: int = 0) -> bool:
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
                "WHERE project_id = ? AND task_number = ?",
                (project_id, task_number),
            ).fetchone()
            if row is None:
                self.db.conn.execute(
                    """INSERT INTO task_claims (task_number, project_id, requirement_number,
                         executor_id, lease_expires_at, attempts, max_attempts, claimed_at)
                       VALUES (?, ?, ?, ?, ?, 0, ?, ?)""",
                    (task_number, project_id, requirement_number, executor_id,
                     expires, max_attempts, iso(now)),
                )
                self.db.conn.execute("COMMIT;")
                return True
            if int(row["attempts"]) >= int(row["max_attempts"] or max_attempts):
                self.db.conn.execute("COMMIT;")
                return False
            if parse_iso(row["lease_expires_at"]) <= now:
                self.db.conn.execute(
                    """UPDATE task_claims SET executor_id = ?, lease_expires_at = ?, claimed_at = ?
                       WHERE project_id = ? AND task_number = ?""",
                    (executor_id, expires, iso(now), project_id, task_number),
                )
                self.db.conn.execute("COMMIT;")
                return True
            self.db.conn.execute("COMMIT;")
            return False
        except Exception:
            self.db.conn.execute("ROLLBACK;")
            raise

    def expire(self, task_number: int, project_id: int = 0) -> None:
        """Expire a lease immediately so another executor may take the task."""
        self.db.conn.execute(
            "UPDATE task_claims SET lease_expires_at = ? "
            "WHERE project_id = ? AND task_number = ?",
            (iso(utcnow()), project_id, task_number),
        )

    def renew(self, task_number: int, executor_id: str, lease_seconds: int,
             project_id: int = 0) -> bool:
        expires = iso(utcnow() + timedelta(seconds=lease_seconds))
        cur = self.db.conn.execute(
            """UPDATE task_claims SET lease_expires_at = ?
               WHERE project_id = ? AND task_number = ? AND executor_id = ?""",
            (expires, project_id, task_number, executor_id),
        )
        return cur.rowcount == 1

    def release(self, task_number: int, project_id: int = 0) -> None:
        self.db.conn.execute(
            "DELETE FROM task_claims WHERE project_id = ? AND task_number = ?",
            (project_id, task_number),
        )

    def increment_attempts(self, task_number: int, project_id: int = 0) -> int:
        self.db.conn.execute(
            "UPDATE task_claims SET attempts = attempts + 1 "
            "WHERE project_id = ? AND task_number = ?",
            (project_id, task_number),
        )
        row = self.db.conn.execute(
            "SELECT attempts FROM task_claims WHERE project_id = ? AND task_number = ?",
            (project_id, task_number),
        ).fetchone()
        return int(row[0]) if row else 0

    def register_abandoned_attempt(self, task_number: int, requirement_number: int,
                                   executor_id: str, max_attempts: int = 3,
                                   project_id: int = 0) -> int:
        """Count one more attempt for a task that died without reporting.

        Used by the stale-task reaper (#117): a task whose lease expired with no
        result never reached :meth:`increment_attempts`, so ``attempts`` stayed
        at 0 and the ``max_attempts`` cap -- the only thing that ever applies
        ``status:Blocked`` -- was unreachable. Creates the claim row when the
        task vanished before anyone claimed it.
        """
        now = utcnow()
        self.db.conn.execute(
            """INSERT INTO task_claims (task_number, project_id, requirement_number,
                   executor_id, lease_expires_at, attempts, max_attempts, claimed_at)
               VALUES (?, ?, ?, ?, ?, 0, ?, ?)
               ON CONFLICT(project_id, task_number) DO NOTHING""",
            (task_number, project_id, requirement_number, executor_id,
             iso(now), max_attempts, iso(now)),
        )
        return self.increment_attempts(task_number, project_id)

    def get(self, task_number: int, project_id: int = 0) -> Optional[sqlite3.Row]:
        return self.db.conn.execute(
            "SELECT * FROM task_claims WHERE project_id = ? AND task_number = ?",
            (project_id, task_number),
        ).fetchone()

    def expired(self, at: Optional[datetime] = None) -> list[sqlite3.Row]:
        return self.db.conn.execute(
            "SELECT * FROM task_claims WHERE lease_expires_at <= ?", (iso(at or utcnow()),)
        ).fetchall()

    def active(self) -> list[sqlite3.Row]:
        return self.db.conn.execute(
            "SELECT * FROM task_claims WHERE lease_expires_at > ?", (iso(utcnow()),)
        ).fetchall()


class RuntimeStateRepo:
    """Small key/value table for operator state that must survive a restart.

    Issue #124: the global pause lived only in process memory, so a restart
    silently resumed dispatching an operator had deliberately stopped, and the
    watchdog could not tell its own pause from an operator's.
    """

    def __init__(self, db: Database):
        self.db = db

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.db.conn.execute(
            "SELECT value FROM runtime_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def get_bool(self, key: str, default: bool = False) -> bool:
        row = self.db.conn.execute(
            "SELECT value FROM runtime_state WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        return str(row["value"]).lower() in ("1", "true", "yes")

    def set(self, key: str, value: Any) -> None:
        self.db.conn.execute(
            """INSERT INTO runtime_state (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                              updated_at = excluded.updated_at""",
            (key, str(value), iso()),
        )

    def delete(self, key: str) -> None:
        self.db.conn.execute("DELETE FROM runtime_state WHERE key = ?", (key,))


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

    def find_recent(self, severity: str, source: str, message: str,
                    within_seconds: float) -> Optional[int]:
        """Id of an unacked identical alert raised inside the cooldown window.

        Used by the watchdog to collapse a persistently failing check into one
        alert instead of one row per sweep.
        """
        if within_seconds <= 0:
            return None
        from datetime import datetime, timedelta, timezone

        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=within_seconds))
        rows = self.db.conn.execute(
            "SELECT id, created_at FROM alerts WHERE acknowledged = 0 "
            "AND severity = ? AND source = ? AND message = ? ORDER BY id DESC",
            (severity, source, message),
        ).fetchall()
        for row in rows:
            try:
                created = datetime.fromisoformat(
                    str(row["created_at"]).replace("Z", "+00:00"))
            except ValueError:
                continue
            if created >= cutoff:
                return int(row["id"])
        return None

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

    def purge(self, acknowledged_only: bool = True,
              severity: Optional[str] = None,
              before: Optional[str] = None) -> int:
        """Delete alert rows and return how many were removed.

        Acknowledging an alert only marks it read; the row stays in the Activity
        feed forever, so a past incident (a watchdog storm, a burst of misrouted
        outbox failures) keeps cluttering the log long after it is resolved.
        Deletion is opt-in and defaults to acknowledged rows only, so an alert
        nobody has triaged yet cannot be wiped by accident.
        """
        sql = "DELETE FROM alerts WHERE 1 = 1"
        args: list = []
        if acknowledged_only:
            sql += " AND acknowledged = 1"
        if severity:
            sql += " AND severity = ?"
            args.append(severity)
        if before:
            sql += " AND created_at < ?"
            args.append(before)
        cur = self.db.conn.execute(sql, args)
        self.db.conn.commit()
        return int(cur.rowcount or 0)

    def ack_all(self, severity: Optional[str] = None) -> int:
        """Acknowledge all unacknowledged alerts, optionally filtered by severity.

        Returns the number of rows updated.
        """
        if severity:
            cur = self.db.conn.execute(
                "UPDATE alerts SET acknowledged = 1 "
                "WHERE acknowledged = 0 AND severity = ?",
                (severity,),
            )
        else:
            cur = self.db.conn.execute(
                "UPDATE alerts SET acknowledged = 1 WHERE acknowledged = 0"
            )
        return int(cur.rowcount or 0)

    def count(self) -> int:
        return int(self.db.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0])

    def count_all(self) -> int:
        row = self.db.conn.execute("SELECT COUNT(*) c FROM alerts").fetchone()
        return int(row["c"]) if row else 0

    def count_unacked(self) -> int:
        return int(
            self.db.conn.execute(
                "SELECT COUNT(*) FROM alerts WHERE acknowledged = 0"
            ).fetchone()[0]
        )


class ProjectRepo:
    """Multi-project registry backing the Projects page (issue #15 / #18).

    Each row is a GitHub repository the operator has registered with Clams.
    ``progress`` is a 0-100 integer maintained by the system (or 0 until work
    is computed for that project). ``worktree_path`` is the cloned working tree
    provisioned when the project is added (issue #18).
    """

    STATUSES = ("Queued", "Running", "In review", "Paused", "Done")

    def __init__(self, db: Database):
        self.db = db

    def add(self, name: str, url: str, status: str = "Queued",
            progress: int = 0, worktree_path: Optional[str] = None,
            default_branch: Optional[str] = None) -> sqlite3.Row:
        now = iso()
        cur = self.db.conn.execute(
            """INSERT INTO projects (name, url, status, progress, worktree_path,
                   default_branch, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, url, status, int(progress), worktree_path, default_branch,
             now, now),
        )
        return self.get(int(cur.lastrowid))  # type: ignore[arg-type]

    def get(self, project_id: int) -> Optional[sqlite3.Row]:
        return self.db.conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()

    def find_by_url(self, url: str) -> Optional[sqlite3.Row]:
        return self.db.conn.execute(
            "SELECT * FROM projects WHERE url = ?", (url,)
        ).fetchone()

    def url_exists(self, url: str) -> bool:
        return self.find_by_url(url) is not None

    def all(self) -> list[sqlite3.Row]:
        return self.db.conn.execute(
            "SELECT * FROM projects ORDER BY id DESC"
        ).fetchall()

    def set_status(self, project_id: int, status: str) -> None:
        self.db.conn.execute(
            "UPDATE projects SET status = ?, updated_at = ? WHERE id = ?",
            (status, iso(), project_id),
        )

    def set_progress(self, project_id: int, progress: int) -> None:
        self.db.conn.execute(
            "UPDATE projects SET progress = ?, updated_at = ? WHERE id = ?",
            (int(progress), iso(), project_id),
        )

    def set_worktree(self, project_id: int, worktree_path: str,
                    default_branch: Optional[str] = None) -> None:
        self.db.conn.execute(
            "UPDATE projects SET worktree_path = ?, default_branch = ?, updated_at = ? "
            "WHERE id = ?",
            (worktree_path, default_branch, iso(), project_id),
        )

    def set_paused(self, project_id: int, paused: bool) -> None:
        self.db.conn.execute(
            "UPDATE projects SET paused = ?, updated_at = ? WHERE id = ?",
            (1 if paused else 0, iso(), project_id),
        )

    def set_error(self, project_id: int, error: Optional[str]) -> None:
        self.db.conn.execute(
            "UPDATE projects SET error = ?, updated_at = ? WHERE id = ?",
            (error, iso(), project_id),
        )

    def clear_error(self, project_id: int) -> None:
        self.set_error(project_id, None)

    def count(self) -> int:
        return int(self.db.conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0])


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
    projects: ProjectRepo = field(init=False)
    runtime: RuntimeStateRepo = field(init=False)

    def __post_init__(self) -> None:
        self.work_items = WorkItemRepo(self.db)
        self.task_claims = TaskClaimRepo(self.db)
        self.alerts = AlertRepo(self.db)
        self.outbox = OutboxRepo(self.db)
        self.projects = ProjectRepo(self.db)
        self.runtime = RuntimeStateRepo(self.db)

    def migrate(self) -> None:
        self.db.migrate()


def open_store(path: str | Path) -> Store:
    db = Database(path)
    db.connect()
    db.migrate()
    return Store(db)
