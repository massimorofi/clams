"""Deterministic, bounded selection of terminal workflows.

Rules
-----
* Only *terminal* workflows are eligible (``done``, ``failed``,
  ``cancelled``). A workflow that is still in flight has no post-mortem to
  write yet.
* ``since`` filters on ``updated_at`` (the closest proxy for "when it reached
  its terminal state") and accepts either a SQL timestamp string
  (``YYYY-MM-DD HH:MM:SS``) or a ``datetime``.
* ``limit`` bounds the result set. It is required (a default is applied) so a
  post-mortem can never fan out over the whole history.
* Ordering is stable and total: ``updated_at DESC, created_at DESC,
  workflow_id ASC``. The ``workflow_id`` tie-break makes the order a total
  order, so two runs over an unchanged database return byte-identical lists
  even when many workflows share a timestamp.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Optional, Sequence, Union

#: workflow statuses a post-mortem may be written for
TERMINAL_STATUSES: tuple[str, ...] = ("done", "failed", "cancelled")

#: hard ceiling on one selection, whatever the caller asks for
MAX_LIMIT = 200

#: default window size for one post-mortem run
DEFAULT_LIMIT = 25

_ORDER_BY = "ORDER BY updated_at DESC, created_at DESC, workflow_id ASC"


def _as_sql_ts(value: Union[str, datetime, None]) -> Optional[str]:
    """Normalise ``since`` to the SQL timestamp text SQLite stores."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    text = str(value).strip()
    if not text:
        return None
    # Accept ISO-8601 with a "T" (and a trailing Z) as well as SQL form.
    text = text.replace("T", " ")
    if text.endswith("Z"):
        text = text[:-1].strip()
    # validate shape early: a bad bound must fail loudly, not silently widen
    try:
        datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise ValueError(
            f"since must be 'YYYY-MM-DD HH:MM:SS' (or ISO-8601), got {value!r}"
        ) from exc
    return text


def normalise_statuses(statuses: Optional[Iterable[str]]) -> tuple[str, ...]:
    """Validate + de-duplicate the requested statuses, keeping a stable order."""
    if statuses is None:
        return TERMINAL_STATUSES
    if isinstance(statuses, str):
        statuses = (statuses,)
    out: list[str] = []
    for s in statuses:
        s = str(s).strip()
        if not s:
            continue
        if s not in TERMINAL_STATUSES:
            raise ValueError(
                f"not a terminal workflow status: {s!r} "
                f"(choose from {', '.join(TERMINAL_STATUSES)})"
            )
        if s not in out:
            out.append(s)
    if not out:
        raise ValueError("statuses must contain at least one terminal status")
    return tuple(out)


def clamp_limit(limit: Optional[int]) -> int:
    """Bound the page size into ``1..MAX_LIMIT`` (never unbounded)."""
    if limit is None:
        return DEFAULT_LIMIT
    limit = int(limit)
    if limit < 1:
        raise ValueError("limit must be >= 1")
    return min(limit, MAX_LIMIT)


def select_terminal_workflows(
    conn: sqlite3.Connection,
    statuses: Optional[Iterable[str]] = None,
    since: Union[str, datetime, None] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    """Return eligible workflows, newest first, at most ``limit`` rows.

    Read-only. Each row is a plain ``dict`` (JSON-safe) with the columns the
    rest of the package needs.
    """
    wanted = normalise_statuses(statuses)
    page = clamp_limit(limit)
    bound = _as_sql_ts(since)

    marks = ",".join("?" * len(wanted))
    sql = (
        "SELECT workflow_id, project_id, source, gh_issue_number, goal, status, "
        "replans, created_at, updated_at FROM workflows "
        f"WHERE status IN ({marks})"
    )
    params: list[object] = list(wanted)
    if bound is not None:
        sql += " AND COALESCE(updated_at, created_at) >= ?"
        params.append(bound)
    sql += f" {(_ORDER_BY)} LIMIT ?"
    params.append(page)

    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def count_terminal_workflows(
    conn: sqlite3.Connection,
    statuses: Optional[Iterable[str]] = None,
    since: Union[str, datetime, None] = None,
) -> int:
    """How many workflows *would* be selected (ignores ``limit``).

    Used to record truncation honestly: ``total_matched > len(selected)``.
    """
    wanted = normalise_statuses(statuses)
    bound = _as_sql_ts(since)
    marks = ",".join("?" * len(wanted))
    sql = f"SELECT COUNT(*) AS n FROM workflows WHERE status IN ({marks})"
    params: list[object] = list(wanted)
    if bound is not None:
        sql += " AND COALESCE(updated_at, created_at) >= ?"
        params.append(bound)
    row = conn.execute(sql, params).fetchone()
    return int(row["n"]) if row else 0


def workflow_ids(rows: Sequence[dict]) -> list[str]:
    """Stable id list for a selection (order preserved)."""
    return [str(r["workflow_id"]) for r in rows]
