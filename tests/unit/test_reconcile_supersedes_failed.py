"""reconcile_plan must supersede a stale `failed`/`blocked` row, not keep it.

The bug this pins down: the stale pass used to skip every terminal row
(``if r["status"] not in TERMINAL``), so a task that had already failed kept
its ``failed`` status after a replan dropped it from the plan. Because
``workflow_outcome`` reports 'failed' if ANY task is failed (and 'blocked' if
any is blocked), that one superseded row poisoned the workflow forever - the
new plan could go 100% success and the workflow still read 'failed'.

Rule now: a row whose plan key is absent from the incoming plan is cancelled
even when it is currently ``failed`` or ``blocked``; those ids are additionally
reported under ``superseded_terminal``. A ``success`` row absent from the plan
stays ``success`` (it records delivered work and never blocks 'done'). A row
whose key IS in the plan keeps its status exactly as before.

Runnable in a bare container: stdlib + clams.db.* only (no pydantic/httpx/
fastapi/redis), and ``src`` is put on sys.path from ``__file__``.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from clams.db import connect, migrate                     # noqa: E402
from clams.db import repo_projects, repo_tasks, repo_workflows  # noqa: E402


# ------------------------------------------------------------------ fixtures
def fresh_db(tmp_name: str = "reconcile_supersede.db") -> sqlite3.Connection:
    """A migrated, throwaway database (no shared fixtures, no framework deps)."""
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="clams-test-")
    conn = connect(str(Path(tmpdir) / tmp_name))
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


def set_status(conn: sqlite3.Connection, task_id: str, status: str) -> None:
    """Force a status directly (setup only - the state machine is not the SUT)."""
    conn.execute("UPDATE tasks SET status=? WHERE task_id=?", (status, task_id))


def task_by_title(conn: sqlite3.Connection, wf: str, title: str) -> sqlite3.Row:
    key = repo_tasks.normalize_plan_key(title)
    for r in repo_tasks.list_tasks(conn, wf):
        if repo_tasks.row_key(r) == key:
            return r
    raise AssertionError(f"no task for title {title!r}")


def status_of(conn: sqlite3.Connection, task_id: str) -> str:
    return repo_tasks.get_task(conn, task_id)["status"]


def titles(conn: sqlite3.Connection, wf: str) -> list[str]:
    return [r["title"] for r in repo_tasks.list_tasks(conn, wf)]


# ------------------------------------------------------------- (a) failed row
def test_failed_task_absent_from_plan_is_cancelled_and_reported():
    conn = fresh_db()
    wf = new_workflow(conn)
    repo_tasks.reconcile_plan(conn, wf, plan("Alpha", "Beta"))
    failed = task_by_title(conn, wf, "Beta")["task_id"]
    set_status(conn, failed, "failed")

    out = repo_tasks.reconcile_plan(conn, wf, plan("Alpha"))

    assert status_of(conn, failed) == "cancelled"
    assert out["superseded_terminal"] == [failed]
    assert out["stale"] == [failed]          # stale still lists every superseded id
    assert out["reused"] == {repo_tasks.normalize_plan_key("Alpha"):
                             task_by_title(conn, wf, "Alpha")["task_id"]}
    assert out["created"] == []
    conn.close()


def test_superseding_the_failure_unpoisons_workflow_outcome():
    """The whole point: the replanned workflow can now reach 'done'."""
    conn = fresh_db()
    wf = new_workflow(conn)
    repo_tasks.reconcile_plan(conn, wf, plan("Alpha", "Beta"))
    failed = task_by_title(conn, wf, "Beta")["task_id"]
    set_status(conn, failed, "failed")
    assert repo_tasks.workflow_outcome(conn, wf) == "failed"

    repo_tasks.reconcile_plan(conn, wf, plan("Alpha"))
    alpha = task_by_title(conn, wf, "Alpha")["task_id"]
    set_status(conn, alpha, "success")

    assert repo_tasks.workflow_outcome(conn, wf) == "done"
    conn.close()


# ------------------------------------------------------------- (b) still planned
def test_failed_task_still_in_plan_keeps_its_status():
    conn = fresh_db()
    wf = new_workflow(conn)
    repo_tasks.reconcile_plan(conn, wf, plan("Alpha", "Beta"))
    failed = task_by_title(conn, wf, "Beta")["task_id"]
    set_status(conn, failed, "failed")

    out = repo_tasks.reconcile_plan(
        conn, wf, [entry(1, "Alpha"), entry(2, "Beta", objective="retried")])

    assert status_of(conn, failed) == "failed"
    assert out["superseded_terminal"] == []
    assert out["stale"] == []
    assert out["created"] == []
    assert out["reused"][repo_tasks.normalize_plan_key("Beta")] == failed
    # content refreshed, identity + status untouched
    row = repo_tasks.get_task(conn, failed)
    assert row["objective"] == "retried"
    conn.close()


def test_whitespace_and_case_variants_of_a_matching_title_stay_failed():
    conn = fresh_db()
    wf = new_workflow(conn)
    repo_tasks.reconcile_plan(conn, wf, plan("Fix Replan Dedupe"))
    tid = task_by_title(conn, wf, "Fix Replan Dedupe")["task_id"]
    set_status(conn, tid, "failed")

    out = repo_tasks.reconcile_plan(conn, wf, [entry(1, "  fix   REPLAN dedupe ")])

    assert status_of(conn, tid) == "failed"
    assert out["superseded_terminal"] == [] and out["stale"] == []
    conn.close()


# ------------------------------------------------------------- (c) success row
def test_success_task_absent_from_plan_stays_success():
    conn = fresh_db()
    wf = new_workflow(conn)
    repo_tasks.reconcile_plan(conn, wf, plan("Alpha", "Beta"))
    done = task_by_title(conn, wf, "Beta")["task_id"]
    set_status(conn, done, "success")

    out = repo_tasks.reconcile_plan(conn, wf, plan("Alpha"))

    assert status_of(conn, done) == "success"
    assert out["superseded_terminal"] == []
    assert out["stale"] == [done]            # superseded, but never rewritten
    conn.close()


def test_success_row_absent_from_plan_still_lets_the_workflow_finish():
    conn = fresh_db()
    wf = new_workflow(conn)
    repo_tasks.reconcile_plan(conn, wf, plan("Alpha", "Beta"))
    done = task_by_title(conn, wf, "Beta")["task_id"]
    set_status(conn, done, "success")

    repo_tasks.reconcile_plan(conn, wf, plan("Alpha"))
    alpha = task_by_title(conn, wf, "Alpha")["task_id"]
    set_status(conn, alpha, "success")

    assert repo_tasks.workflow_outcome(conn, wf) == "done"
    assert repo_tasks.workflow_tasks_all_terminal(conn, wf)
    conn.close()


# ------------------------------------------------------------- (d) blocked row
def test_blocked_task_absent_from_plan_is_cancelled():
    conn = fresh_db()
    wf = new_workflow(conn)
    repo_tasks.reconcile_plan(conn, wf, plan("Alpha", "Beta"))
    blocked = task_by_title(conn, wf, "Beta")["task_id"]
    set_status(conn, blocked, "blocked")

    out = repo_tasks.reconcile_plan(conn, wf, plan("Alpha"))

    assert status_of(conn, blocked) == "cancelled"
    assert out["superseded_terminal"] == [blocked]
    assert out["stale"] == [blocked]
    conn.close()


# -------------------------------------------------------- (e) already cancelled
def test_previously_cancelled_row_stays_cancelled_and_is_not_flipped():
    conn = fresh_db()
    wf = new_workflow(conn)
    repo_tasks.reconcile_plan(conn, wf, plan("Alpha", "Beta", "Gamma"))
    gone = task_by_title(conn, wf, "Beta")["task_id"]
    set_status(conn, gone, "cancelled")
    before = repo_tasks.get_task(conn, gone)["updated_at"]

    out = repo_tasks.reconcile_plan(conn, wf, plan("Alpha", "Gamma"))

    assert status_of(conn, gone) == "cancelled"
    assert out["superseded_terminal"] == []
    assert out["stale"] == [gone]
    assert repo_tasks.get_task(conn, gone)["updated_at"] == before  # not rewritten
    conn.close()


# --------------------------------------------------------------- extra guards
def test_non_terminal_absent_rows_are_cancelled_but_not_reported_as_terminal():
    conn = fresh_db()
    wf = new_workflow(conn)
    repo_tasks.reconcile_plan(conn, wf, plan("Alpha", "Beta", "Gamma"))
    running = task_by_title(conn, wf, "Beta")["task_id"]
    set_status(conn, running, "running")
    pending = task_by_title(conn, wf, "Gamma")["task_id"]

    out = repo_tasks.reconcile_plan(conn, wf, plan("Alpha"))

    assert status_of(conn, running) == "cancelled"
    assert status_of(conn, pending) == "cancelled"
    assert out["superseded_terminal"] == []
    assert sorted(out["stale"]) == sorted([running, pending])
    conn.close()


def test_reconcile_never_deletes_and_is_repeatable_after_a_supersede():
    conn = fresh_db()
    wf = new_workflow(conn)
    repo_tasks.reconcile_plan(conn, wf, plan("Alpha", "Beta"))
    failed = task_by_title(conn, wf, "Beta")["task_id"]
    set_status(conn, failed, "failed")

    stmts: list[str] = []
    conn.set_trace_callback(stmts.append)
    try:
        first = repo_tasks.reconcile_plan(conn, wf, plan("Alpha"))
        again = repo_tasks.reconcile_plan(conn, wf, plan("Alpha"))
    finally:
        conn.set_trace_callback(None)

    assert [s for s in stmts if "delete" in s.lower()] == []
    assert first["superseded_terminal"] == [failed]
    # second pass: the row is already cancelled, so nothing is flipped again
    assert again["superseded_terminal"] == []
    assert again["stale"] == [failed]
    assert conn.execute("SELECT COUNT(*) n FROM tasks").fetchone()["n"] == 2
    conn.close()


def test_returned_keys_are_exactly_the_documented_four():
    conn = fresh_db()
    wf = new_workflow(conn)
    out = repo_tasks.reconcile_plan(conn, wf, plan("Alpha"))
    assert set(out) == {"created", "reused", "stale", "superseded_terminal"}
    assert isinstance(out["superseded_terminal"], list)
    assert json.dumps(out)          # plain JSON-able types (outbox/result friendly)
    conn.close()


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:            # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if fails else 0)
