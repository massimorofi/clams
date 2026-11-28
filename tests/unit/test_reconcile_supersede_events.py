"""Supersede audit events written by ``planner.reconcile.apply_replanned_plan``.

The bug this pins down: ``repo_tasks.reconcile_plan`` rewrites a stale
``failed``/``blocked`` row to ``cancelled`` so it cannot poison
``workflow_outcome`` forever. That erases the *visible* failure from the tasks
table, so the planner side must copy the pre-flip evidence into the events
table before it disappears: one ``task``-scoped ``warn`` event per superseded
id, plus the workflow-level summary that now also reports ``superseded_terminal``.

Without these events a replan that threw away three failed attempts leaves no
trace of what failed or why.

Runnable in a bare container: stdlib + clams.db.* + clams.planner.reconcile
only (no pydantic/httpx/fastapi/redis, no pytest import), and ``src`` is put on
sys.path from ``__file__``.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from clams.db import connect, migrate                                   # noqa: E402
from clams.db import repo_events, repo_projects, repo_tasks, repo_workflows  # noqa: E402
from clams.planner import reconcile                                     # noqa: E402


# ------------------------------------------------------------------ fixtures
def fresh_db(name: str = "reconcile_supersede_events.db") -> sqlite3.Connection:
    """A migrated, throwaway database (no shared fixtures, no framework deps)."""
    tmpdir = tempfile.mkdtemp(prefix="clams-test-")
    conn = connect(str(Path(tmpdir) / name))
    migrate(conn)
    return conn


def new_workflow(conn: sqlite3.Connection) -> str:
    pid = repo_projects.upsert_project(conn, "o/r")
    return repo_workflows.create_workflow(conn, pid, "adhoc", "goal")


def entry(seq: int, title: str, **kw) -> dict:
    d = {"sequence_index": seq, "title": title,
         "objective": f"obj {title}", "acceptance": f"acc {title}",
         "brief": {"steps": [f"do {title}"]}, "depends_on": [],
         "max_attempts": 3}
    d.update(kw)
    return d


def plan(*titles: str) -> list[dict]:
    return [entry(i, t) for i, t in enumerate(titles, start=1)]


def task_id_of(conn: sqlite3.Connection, wf: str, title: str) -> str:
    key = repo_tasks.normalize_plan_key(title)
    for r in repo_tasks.list_tasks(conn, wf):
        if repo_tasks.row_key(r) == key:
            return r["task_id"]
    raise AssertionError(f"no task for title {title!r}")


def make_failed(conn: sqlite3.Connection, task_id: str, error_log: str,
                attempt: int = 3, max_attempts: int = 3) -> None:
    """Force terminal evidence directly (setup only - the state machine is not
    the SUT here; the point is a row that *looks* like a real failed attempt)."""
    conn.execute("UPDATE tasks SET status='failed', error_log=?, attempt=?, "
                 "max_attempts=? WHERE task_id=?",
                 (error_log, attempt, max_attempts, task_id))


def events(conn: sqlite3.Connection, scope: str) -> list[dict]:
    """All events of one scope, oldest first, with ``data`` decoded."""
    rows = conn.execute(
        "SELECT * FROM events WHERE scope=? ORDER BY event_id", (scope,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["data"] = json.loads(r["data"]) if r["data"] else None
        out.append(d)
    return out


def task_events(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    return [e for e in events(conn, "task") if e["ref"] == task_id]


def summary_event(conn: sqlite3.Connection, wf: str) -> dict:
    rows = [e for e in events(conn, "workflow") if e["ref"] == wf]
    assert rows, "no workflow-level event was appended"
    return rows[-1]


# ------------------------------------------------- (1) task-scoped warn event
def test_superseded_failed_task_gets_task_scoped_event_with_pre_flip_evidence():
    conn = fresh_db()
    wf = new_workflow(conn)
    reconcile.apply_replanned_plan(conn, wf, plan("Alpha", "Beta"))
    failed = task_id_of(conn, wf, "Beta")
    make_failed(conn, failed, "gate `pytest tests/unit` exit 1\n"
                              "AssertionError: expected 200 got 500")

    out = reconcile.apply_replanned_plan(conn, wf, plan("Alpha"))

    assert out["superseded_terminal"] == [failed]
    assert repo_tasks.get_task(conn, failed)["status"] == "cancelled"

    evs = task_events(conn, failed)
    assert len(evs) == 1, f"expected exactly one task event, got {len(evs)}"
    ev = evs[0]
    assert ev["scope"] == "task" and ev["ref"] == failed
    assert ev["level"] == "warn"
    # the message says it was superseded by a replan and names the flip
    assert "replan" in ev["message"]
    assert "failed" in ev["message"] and "cancelled" in ev["message"]

    data = ev["data"]
    assert data["previous_status"] == "failed"
    assert data["new_status"] == "cancelled"
    assert data["workflow_id"] == wf
    assert data["attempt"] == 3 and data["max_attempts"] == 3
    assert "AssertionError: expected 200 got 500" in data["error_log_excerpt"]
    conn.close()


def test_error_log_excerpt_is_truncated():
    conn = fresh_db()
    wf = new_workflow(conn)
    reconcile.apply_replanned_plan(conn, wf, plan("Alpha", "Beta"))
    failed = task_id_of(conn, wf, "Beta")
    make_failed(conn, failed, "x" * 5000)

    reconcile.apply_replanned_plan(conn, wf, plan("Alpha"))

    data = task_events(conn, failed)[0]["data"]
    assert 0 < len(data["error_log_excerpt"]) <= reconcile.ERROR_LOG_EXCERPT_CHARS
    conn.close()


def test_blocked_row_is_superseded_the_same_way():
    conn = fresh_db()
    wf = new_workflow(conn)
    reconcile.apply_replanned_plan(conn, wf, plan("Alpha", "Beta"))
    blocked = task_id_of(conn, wf, "Beta")
    conn.execute("UPDATE tasks SET status='blocked', error_log='needs input' "
                 "WHERE task_id=?", (blocked,))

    out = reconcile.apply_replanned_plan(conn, wf, plan("Alpha"))

    assert out["superseded_terminal"] == [blocked]
    ev = task_events(conn, blocked)[0]
    assert ev["data"]["previous_status"] == "blocked"
    assert ev["data"]["error_log_excerpt"] == "needs input"
    conn.close()


# ------------------------------------------- (2) workflow summary carries ids
def test_summary_event_reports_superseded_terminal_and_is_warn():
    conn = fresh_db()
    wf = new_workflow(conn)
    reconcile.apply_replanned_plan(conn, wf, plan("Alpha", "Beta", "Gamma"))
    b = task_id_of(conn, wf, "Beta")
    g = task_id_of(conn, wf, "Gamma")
    make_failed(conn, b, "boom")
    make_failed(conn, g, "boom too")

    reconcile.apply_replanned_plan(conn, wf, plan("Alpha"))

    ev = summary_event(conn, wf)
    assert ev["scope"] == "workflow" and ev["ref"] == wf
    assert ev["level"] == "warn"
    assert ev["message"] == "replan: reused 1, created 0, cancelled 2"
    st = ev["data"]["superseded_terminal"]
    assert st["count"] == 2
    assert sorted(st["ids"]) == sorted([b, g])
    # the counts stay in the payload too
    assert ev["data"]["reused"] == 1 and ev["data"]["created"] == 0
    assert ev["data"]["cancelled"] == 2
    conn.close()


# ------------------------------- (3) nothing superseded -> no extra task events
def test_replan_without_superseded_terminal_rows_emits_no_task_events():
    conn = fresh_db()
    wf = new_workflow(conn)
    reconcile.apply_replanned_plan(conn, wf, plan("Alpha", "Beta"))
    # Beta is still pending (not terminal); a dropped pending row is stale but
    # was never a visible failure, so there is nothing to preserve.
    before = len(task_events(conn, task_id_of(conn, wf, "Beta")))

    out = reconcile.apply_replanned_plan(conn, wf, plan("Alpha"))

    assert out["superseded_terminal"] == []
    assert out["stale"]  # the pending row is still reported as stale
    assert before == 0
    assert not task_events(conn, task_id_of(conn, wf, "Beta"))
    ev = summary_event(conn, wf)
    assert ev["level"] == "info"
    assert ev["data"]["superseded_terminal"] == {"ids": [], "count": 0}
    conn.close()


def test_success_row_dropped_from_plan_is_not_superseded():
    conn = fresh_db()
    wf = new_workflow(conn)
    reconcile.apply_replanned_plan(conn, wf, plan("Alpha", "Beta"))
    done = task_id_of(conn, wf, "Beta")
    conn.execute("UPDATE tasks SET status='success' WHERE task_id=?", (done,))

    out = reconcile.apply_replanned_plan(conn, wf, plan("Alpha"))

    assert out["superseded_terminal"] == []
    assert repo_tasks.get_task(conn, done)["status"] == "success"
    assert not task_events(conn, done)
    assert summary_event(conn, wf)["level"] == "info"
    conn.close()


def test_first_plan_of_an_empty_workflow_is_quiet():
    conn = fresh_db()
    wf = new_workflow(conn)
    out = reconcile.apply_replanned_plan(conn, wf, plan("Alpha", "Beta"))
    assert len(out["created"]) == 2 and out["superseded_terminal"] == []
    assert not events(conn, "task")
    ev = summary_event(conn, wf)
    assert ev["level"] == "info"
    assert ev["message"] == "replan: reused 0, created 2, cancelled 0"
    conn.close()


# ------------------------------------------------------- module import hygiene
def test_module_stays_import_light_and_projection_is_lazy():
    """clams.github must not be pulled in by importing the planner module."""
    import importlib
    mod = importlib.reload(reconcile)
    src = (SRC / "clams" / "planner" / "reconcile.py").read_text()
    assert "__import__(\"clams.github.projection\"" in src
    assert "\nfrom ..github" not in src and "\nimport clams.github" not in src
    assert mod.ERROR_LOG_EXCERPT_CHARS <= 600
