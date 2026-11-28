"""Planner-side regression tests for ``planner.reconcile.apply_replanned_plan``.

The repo layer (``repo_tasks.reconcile_plan``) owns the row arithmetic; this
file owns the *planner contract* built on top of it: identity is preserved, the
plan in force is recorded, exactly one workflow-level summary event is written
per apply, the caller's transaction is respected, and the returned dict is the
repo result untouched.

Runnable in a bare container: stdlib + clams.db.* + clams.planner.reconcile
(no pytest import, no third-party deps); ``src`` goes on sys.path from
``__file__``.
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

from clams.db import connect, migrate, immediate                        # noqa: E402
from clams.db import repo_projects, repo_tasks, repo_workflows          # noqa: E402
from clams.planner import reconcile                                      # noqa: E402


def fresh_db(name: str = "planner_reconcile.db") -> sqlite3.Connection:
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


def ids_by_title(conn: sqlite3.Connection, wf: str) -> dict[str, str]:
    return {r["title"]: r["task_id"] for r in repo_tasks.list_tasks(conn, wf)}


def workflow_events(conn: sqlite3.Connection, wf: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM events WHERE scope='workflow' AND ref=? ORDER BY event_id",
        (wf,)).fetchall()


def task_event_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) n FROM events WHERE scope='task'").fetchone()["n"]


def test_apply_creates_rows_and_returns_the_repo_result():
    conn = fresh_db()
    wf = new_workflow(conn)
    out = reconcile.apply_replanned_plan(conn, wf, plan("Alpha", "Beta"))
    assert set(out) == {"created", "reused", "stale", "superseded_terminal"}
    assert len(out["created"]) == 2
    assert out["reused"] == {} and out["stale"] == []
    assert sorted(ids_by_title(conn, wf)) == ["Alpha", "Beta"]
    conn.close()


def test_reapply_preserves_task_identity_and_gh_issue_numbers():
    conn = fresh_db()
    wf = new_workflow(conn)
    reconcile.apply_replanned_plan(conn, wf, plan("Alpha", "Beta"))
    before = ids_by_title(conn, wf)
    conn.execute("UPDATE tasks SET gh_issue_number=17 WHERE title='Beta'")

    out = reconcile.apply_replanned_plan(conn, wf, plan("Alpha", "Beta"))

    assert out["created"] == []
    assert set(out["reused"].values()) == set(before.values())
    after = ids_by_title(conn, wf)
    assert after == before, "a replan must never re-mint task_ids"
    beta = [r for r in repo_tasks.list_tasks(conn, wf) if r["title"] == "Beta"][0]
    assert beta["gh_issue_number"] == 17
    conn.close()


def test_reordering_the_plan_renumbers_without_unique_collisions():
    conn = fresh_db()
    wf = new_workflow(conn)
    reconcile.apply_replanned_plan(conn, wf, plan("Alpha", "Beta", "Gamma"))
    before = ids_by_title(conn, wf)

    reconcile.apply_replanned_plan(
        conn, wf, [entry(1, "Gamma"), entry(2, "Beta"), entry(3, "Alpha")])

    rows = repo_tasks.list_tasks(conn, wf)
    assert [r["sequence_index"] for r in rows] == [1, 2, 3]
    assert [r["title"] for r in rows] == ["Gamma", "Beta", "Alpha"]
    assert {r["title"]: r["task_id"] for r in rows} == before
    conn.close()


def test_dropped_rows_are_cancelled_not_deleted_and_plan_json_tracks_the_plan():
    conn = fresh_db()
    wf = new_workflow(conn)
    reconcile.apply_replanned_plan(conn, wf, plan("Alpha", "Beta", "Gamma"))
    n_before = conn.execute("SELECT COUNT(*) n FROM tasks").fetchone()["n"]

    out = reconcile.apply_replanned_plan(conn, wf, plan("Alpha"))

    assert conn.execute("SELECT COUNT(*) n FROM tasks").fetchone()["n"] == n_before
    assert len(out["stale"]) == 2
    statuses = {r["title"]: r["status"] for r in repo_tasks.list_tasks(conn, wf)}
    assert statuses["Beta"] == "cancelled" and statuses["Gamma"] == "cancelled"
    plan_json = json.loads(repo_workflows.get_workflow(conn, wf)["plan_json"])
    assert [t["title"] for t in plan_json["tasks"]] == ["Alpha"]
    conn.close()


def test_one_workflow_summary_event_per_apply_and_no_task_events_when_clean():
    conn = fresh_db()
    wf = new_workflow(conn)
    reconcile.apply_replanned_plan(conn, wf, plan("Alpha"))
    reconcile.apply_replanned_plan(conn, wf, plan("Alpha", "Beta"))
    evs = workflow_events(conn, wf)
    assert len(evs) == 2
    assert task_event_count(conn) == 0
    assert json.loads(evs[-1]["data"])["superseded_terminal"]["count"] == 0
    conn.close()


def test_apply_respects_a_transaction_already_open_by_the_caller():
    """BEGIN IMMEDIATE inside BEGIN IMMEDIATE is an SQLite error; the helper
    must join the caller's transaction instead of nesting one."""
    conn = fresh_db()
    wf = new_workflow(conn)
    with immediate(conn):
        out = reconcile.apply_replanned_plan(conn, wf, plan("Alpha", "Beta"))
    assert len(out["created"]) == 2
    assert repo_tasks.list_tasks(conn, wf)
    conn.close()


def test_repo_argument_is_optional_and_projection_is_a_noop_without_an_issue():
    conn = fresh_db()
    wf = new_workflow(conn)
    reconcile.apply_replanned_plan(conn, wf, plan("Alpha"), repo="o/r")
    reconcile.apply_replanned_plan(conn, wf, plan("Alpha", "Beta"))
    assert len(workflow_events(conn, wf)) == 2
    conn.close()


def test_apply_is_idempotent_on_an_unchanged_plan():
    conn = fresh_db()
    wf = new_workflow(conn)
    first = reconcile.apply_replanned_plan(conn, wf, plan("Alpha", "Beta"))
    again = reconcile.apply_replanned_plan(conn, wf, plan("Alpha", "Beta"))
    assert first["created"] and not again["created"]
    assert again["superseded_terminal"] == [] and again["stale"] == []
    conn.close()
