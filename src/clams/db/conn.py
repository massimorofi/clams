"""SQLite connection management: PRAGMAs, BEGIN IMMEDIATE, migrations.

Single writer (the planner). The API opens read-only connections
(``mode=ro``) and only inserts into ``commands`` via its own writer conn.
Dialect-neutral SQL only (RETURNING supported by SQLite >= 3.35 and Postgres).
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional


def _resolve_migrations_dir() -> Path:
    """Find the migrations/ dir in dev (repo) or container (/app) layouts."""
    candidates = []
    if os.environ.get("CLAMS_MIGRATIONS_DIR"):
        candidates.append(Path(os.environ["CLAMS_MIGRATIONS_DIR"]))
    candidates += [
        Path(__file__).resolve().parents[3] / "migrations",   # editable install
        Path.cwd() / "migrations",                            # run from repo root
        Path("/app/migrations"),                              # container layout
    ]
    for c in candidates:
        if c.is_dir() and any(c.glob("*.sql")):
            return c
    return candidates[0]


MIGRATIONS_DIR = _resolve_migrations_dir()

SCHEMA_KEY = "schema_version"


def utcnow_s() -> str:
    """UTC timestamp in the same format as SQLite CURRENT_TIMESTAMP."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def iso_to_epoch(ts: str) -> float:
    return time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S")) - time.timezone


def epoch_to_iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch))


def plus_s(seconds: float) -> str:
    return epoch_to_iso(time.time() + seconds)


class _CursorProxy:
    """Cursor wrapper that supports ``with conn.execute(...) as cur:``.

    Python 3.14 removed the context-manager protocol from sqlite3.Cursor;
    this keeps the ergonomic ``with`` form (auto-close) while delegating
    everything else (fetchone/fetchall/rowcount/...) to the real cursor.
    """
    __slots__ = ("_cur",)

    def __init__(self, cur: sqlite3.Cursor):
        self._cur = cur

    def __getattr__(self, name):
        return getattr(self._cur, name)

    def __iter__(self):
        return iter(self._cur)

    def __enter__(self) -> sqlite3.Cursor:
        return self._cur

    def __exit__(self, *exc) -> bool:
        self._cur.close()
        return False


class Connection(sqlite3.Connection):
    def execute(self, sql, parameters=()):  # type: ignore[override]
        return _CursorProxy(super().execute(sql, parameters))


def connect(db_path: str, read_only: bool = False,
            timeout: float = 5.0,
            check_same_thread: bool = True) -> sqlite3.Connection:
    # NOTE: connections are thread-affine by default; the API opens one per
    # request but FastAPI may tear a sync dependency down on a different
    # threadpool thread -> pass check_same_thread=False there.
    """Open a connection with the mandated PRAGMAs."""
    p = Path(db_path)
    if read_only:
        if not p.exists():
            raise FileNotFoundError(f"db not found for read-only open: {db_path}")
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=timeout,
                               isolation_level=None, factory=Connection,
                               check_same_thread=check_same_thread)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(p), timeout=timeout, isolation_level=None,
                               factory=Connection,
                               check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    if not read_only:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
    return conn


@contextmanager
def immediate(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE transaction: commit on success, rollback on error."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def get_runtime_state(conn: sqlite3.Connection, key: str,
                      default: Optional[str] = None) -> Optional[str]:
    row = conn.execute(
        "SELECT value FROM runtime_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_runtime_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO runtime_state(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value))


def schema_version(conn: sqlite3.Connection) -> int:
    try:
        v = get_runtime_state(conn, SCHEMA_KEY)
    except sqlite3.OperationalError:
        return 0
    return int(v) if v else 0


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply pending migrations/*.sql in filename order. Returns applied names."""
    applied: list[str] = []
    current = schema_version(conn)
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    for f in files:
        version = int(f.name.split("_", 1)[0])
        if version <= current:
            continue
        sql = f.read_text(encoding="utf-8")
        # executescript() would commit the open transaction implicitly, so split
        # the migration into statements and run them inside BEGIN IMMEDIATE.
        with immediate(conn):
            stmt, buf = "", ""
            for line in sql.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("--"):
                    continue
                buf += line + "\n"
                if sqlite3.complete_statement(buf):
                    conn.execute(buf)
                    buf = ""
            if buf.strip():
                raise ValueError(f"unterminated statement in {f.name}: {buf[:80]}")
            set_runtime_state(conn, SCHEMA_KEY, str(version))
        applied.append(f.name)
    return applied


def db_integrity_ok(conn: sqlite3.Connection) -> bool:
    row = conn.execute("PRAGMA quick_check").fetchone()
    return bool(row) and row[0] == "ok"
