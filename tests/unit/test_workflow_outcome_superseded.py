"""``workflow_outcome`` is supersede-aware (cadfa395 / claudio#58, read side).

``reconcile_plan`` (the write side) cancels a stale ``failed``/``blocked`` row
when a replan drops it from the plan. That only helps workflows replanned
*after* the fix. Two classes of poisoned workflow remained:

  * rows that went terminal before the fix landed and were never rewritten, and
  * workflows whose plan was swapped by delete-and-reinsert.

Both left a ``failed`` row on the books for work the current plan no longer
contains, and because ``workflow_outcome`` reported 'failed' if ANY task was
failed, the workflow could never read 'done' however well the new plan ran.

Rule now: a terminal ``failed``/``blocked`` row whose plan key (``plan_key``,
else ``normalize_plan_key(title)``) is absent from the workflow's CURRENT plan
(``workflows.plan_json``) is superseded and is ignored by the outcome. When no
usable plan is stored, the sanctioned fallback applies instead: the row counts
as superseded when the workflow has a 'replan' event appended after the row
went terminal.

In-plan rows are untouched: a failed row that IS in the current plan still
yields 'failed', and the in-plan 'blocked' / 'cancelled' / 'done' rules are
exactly what they were before.

Runnable in a bare container: stdlib + clams.db.* only (no pydantic/httpx/
fastapi/redis), and ``src`` is put on sys.path from ``__file__``.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from clams.db import connect, migrate                            # noqa: E402
from clams.db import repo_events, repo_projects, repo_tasks, repo_workflows  # noqa: E402
from clams.db.conn import plus_s, utcnow_s                       # noqa: E402


# ------------------------------------------------------------------ fixtures
def fresh_db(tmp_name: str = "outcome_supersede.db") -> sqlite3.Connection:
    """A migrated, throwaway database (no shared fixtures, no framework deps)."""
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="clams-test-outcome-")
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


def plan_of(*titles: str) -> list[dict]:
    return [entry(i, t) for i, t in enumerate(titles, start=1)]


def store_plan(conn: sqlite3.Connection, wf: str, *titles: str) -> None:
    """Persist the workflow's CURRENT plan exactly as the planner does."""
    repo_workflows.set_plan(conn, wf, {"tasks": plan_of(*titles)})


def task_id(conn: sqlite3.Connection, wf: str, title: str) -> str:
    key = repo_tasks.normalize_plan_key(title)
    for r in repo_tasks.list_tasks(conn, wf):
        if repo_tasks.row_key(r) == key:
            return r["task_id"]
    raise AssertionError(f"no task for title {title!r}")


def set_status(conn: sqlite3.Connection, task_id: str, status: str,
               updated_at: str | None = None) -> None:
    """Force a status directly (setup only - the state machine is not the SUT)."""
    if updated_at is None:
        conn.execute("UPDATE tasks SET status=? WHERE task_id=?", (status, task_id))
    else:
        conn.execute("UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
                     (status, updated_at, task_id))


def outcome(conn: sqlite3.Connection, wf: str) -> str:
    return repo_tasks.workflow_outcome(conn, wf)


def mk(conn: sqlite3.Connection, wf: str, seq: int, title: str) -> str:
    """Create a task the way the planner does (plan_key set when the column exists)."""
    return repo_tasks.create_task(
        conn, wf, seq, title, f"obj {title}", f"acc {title}",
        {"objective": f"obj {title}", "acceptance": f"acc {title}",
         "run": ["echo x"], "writable": ["*.py"], "repo": "o/r"})


# =============================== (1) failed row NOT in the current plan
def test_failed_row_absent_from_current_plan_does_not_yield_failed():
    """The headline acceptance case: one failed row outside the current plan,
    every in-plan row success -> the workflow reads 'done'."""
    conn = fresh_db()
    wf = new_workflow(conn)
    a = mk(conn, wf, 1, "Alpha")
    b = mk(conn, wf, 2, "Beta")
    set_status(conn, a, "success")
    set_status(conn, b, "failed")
    store_plan(conn, wf, "Alpha")            # Beta was dropped by the replan

    assert repo_tasks.workflow_tasks_all_terminal(conn, wf)
    assert outcome(conn, wf) == "done"
    # the row itself is untouched: a read must never mutate state
    assert repo_tasks.get_task(conn, b)["status"] == "failed"
    conn.close()


def test_only_non_success_rows_are_superseded_failed_rows_yields_done():
    """Acceptance case: nothing in the plan failed, the graveyard is all stale."""
    conn = fresh_db()
    wf = new_workflow(conn)
    a = mk(conn, wf, 1, "Alpha")
    for seq, title in ((2, "Beta"), (3, "Gamma"), (4, "Delta")):
        set_status(conn, mk(conn, wf, seq, title), "failed")
    set_status(conn, a, "success")
    store_plan(conn, wf, "Alpha")

    assert outcome(conn, wf) == "done"
    conn.close()


def test_superseded_blocked_row_also_stops_blocking():
    """Same poisoning class: an out-of-plan 'blocked' row is stale bookkeeping."""
    conn = fresh_db()
    wf = new_workflow(conn)
    a = mk(conn, wf, 1, "Alpha")
    b = mk(conn, wf, 2, "Beta")
    set_status(conn, a, "success")
    set_status(conn, b, "blocked")
    store_plan(conn, wf, "Alpha")

    assert outcome(conn, wf) == "done"
    conn.close()


def test_plan_key_normalisation_matches_titles_across_plan_and_row():
    """Whitespace/case variants of a title are the SAME plan key, so a row whose
    title was re-worded that way stays live (and still yields 'failed')."""
    conn = fresh_db()
    wf = new_workflow(conn)
    t = mk(conn, wf, 1, "Fix Replan Dedupe")
    set_status(conn, t, "failed")
    store_plan(conn, wf, "  fix   REPLAN dedupe ")

    assert outcome(conn, wf) == "failed"
    conn.close()


# ================================== (2) failed row IS in the current plan
def test_failed_row_present_in_current_plan_still_yields_failed():
    conn = fresh_db()
    wf = new_workflow(conn)
    a = mk(conn, wf, 1, "Alpha")
    b = mk(conn, wf, 2, "Beta")
    set_status(conn, a, "success")
    set_status(conn, b, "failed")
    store_plan(conn, wf, "Alpha", "Beta")

    assert outcome(conn, wf) == "failed"
    conn.close()


def test_mixed_plan_reports_only_the_live_failure():
    """One superseded failure plus one live failure -> still 'failed'."""
    conn = fresh_db()
    wf = new_workflow(conn)
    a = mk(conn, wf, 1, "Alpha")
    b = mk(conn, wf, 2, "Beta")
    c = mk(conn, wf, 3, "Gamma")
    set_status(conn, a, "success")
    set_status(conn, b, "failed")            # dropped from the plan
    set_status(conn, c, "failed")            # still planned
    store_plan(conn, wf, "Alpha", "Gamma")

    assert outcome(conn, wf) == "failed"
    conn.close()


# ================== (3) in-plan 'blocked' / 'cancelled' / 'done' unchanged
def test_in_plan_blocked_row_still_blocks():
    conn = fresh_db()
    wf = new_workflow(conn)
    a = mk(conn, wf, 1, "Alpha")
    b = mk(conn, wf, 2, "Beta")
    set_status(conn, a, "success")
    set_status(conn, b, "blocked")
    store_plan(conn, wf, "Alpha", "Beta")

    assert outcome(conn, wf) == "blocked"
    conn.close()


def test_blocked_still_wins_over_failed_for_in_plan_rows():
    """Precedence unchanged: any blocked and none failed -> 'blocked'."""
    conn = fresh_db()
    wf = new_workflow(conn)
    a = mk(conn, wf, 1, "Alpha")
    b = mk(conn, wf, 2, "Beta")
    c = mk(conn, wf, 3, "Gamma")
    set_status(conn, a, "success")
    set_status(conn, b, "blocked")
    set_status(conn, c, "failed")            # superseded, so 'failed' is out
    store_plan(conn, wf, "Alpha", "Beta")

    assert outcome(conn, wf) == "blocked"
    conn.close()


def test_in_plan_cancelled_rows_still_read_cancelled():
    conn = fresh_db()
    wf = new_workflow(conn)
    a = mk(conn, wf, 1, "Alpha")
    b = mk(conn, wf, 2, "Beta")
    set_status(conn, a, "cancelled")
    set_status(conn, b, "cancelled")
    store_plan(conn, wf, "Alpha", "Beta")

    assert outcome(conn, wf) == "cancelled"
    conn.close()


def test_in_plan_success_plus_cancelled_is_done_and_all_success_is_done():
    conn = fresh_db()
    wf = new_workflow(conn)
    a = mk(conn, wf, 1, "Alpha")
    b = mk(conn, wf, 2, "Beta")
    set_status(conn, a, "success")
    set_status(conn, b, "cancelled")
    store_plan(conn, wf, "Alpha", "Beta")
    assert outcome(conn, wf) == "done"

    set_status(conn, b, "success")
    assert outcome(conn, wf) == "done"
    conn.close()


def test_no_plan_at_all_keeps_the_old_behaviour():
    """plan_json NULL and no replan event: nothing is superseded, 'failed' stands."""
    conn = fresh_db()
    wf = new_workflow(conn)
    a = mk(conn, wf, 1, "Alpha")
    b = mk(conn, wf, 2, "Beta")
    set_status(conn, a, "success")
    set_status(conn, b, "failed")

    assert repo_tasks.current_plan_keys(conn, wf) is None
    assert outcome(conn, wf) == "failed"
    conn.close()


# ============================ (4) sanctioned fallback: replan event
def test_fallback_replan_event_after_the_row_supersedes_it():
    """No stored plan -> a replan appended after the row went terminal means the
    plan that produced that row is gone, so it no longer holds the workflow."""
    conn = fresh_db()
    wf = new_workflow(conn)
    a = mk(conn, wf, 1, "Alpha")
    b = mk(conn, wf, 2, "Beta")
    set_status(conn, a, "success")
    set_status(conn, b, "failed", updated_at=plus_s(-3600))   # terminal 1h ago
    repo_events.append_event(conn, "workflow", wf, "warn", "replan 1/3")

    assert repo_tasks.last_replan_ts(conn, wf) is not None
    assert outcome(conn, wf) == "done"
    conn.close()


def test_fallback_failure_after_the_replan_still_fails():
    """The row went terminal *after* the last replan, so it is live work."""
    conn = fresh_db()
    wf = new_workflow(conn)
    a = mk(conn, wf, 1, "Alpha")
    b = mk(conn, wf, 2, "Beta")
    repo_events.append_event(conn, "workflow", wf, "warn", "replan 1/3")
    set_status(conn, a, "success")
    set_status(conn, b, "failed", updated_at=plus_s(3600))    # terminal later

    assert outcome(conn, wf) == "failed"
    conn.close()


def test_fallback_ignores_non_replan_events():
    """A 'done' event is not a replan; nothing gets superseded."""
    conn = fresh_db()
    wf = new_workflow(conn)
    a = mk(conn, wf, 1, "Alpha")
    b = mk(conn, wf, 2, "Beta")
    set_status(conn, a, "success")
    set_status(conn, b, "failed", updated_at=plus_s(-3600))
    repo_events.append_event(conn, "workflow", wf, "info", "done")

    assert repo_tasks.last_replan_ts(conn, wf) is None
    assert outcome(conn, wf) == "failed"
    conn.close()


def test_fallback_is_scoped_to_the_workflow():
    """Another workflow's replan must not supersede this one's failure."""
    conn = fresh_db()
    other = new_workflow(conn)
    repo_events.append_event(conn, "workflow", other, "warn", "replan 1/3")
    wf = new_workflow(conn)
    a = mk(conn, wf, 1, "Alpha")
    b = mk(conn, wf, 2, "Beta")
    set_status(conn, a, "success")
    set_status(conn, b, "failed", updated_at=plus_s(-3600))

    assert repo_tasks.last_replan_ts(conn, wf) is None
    assert outcome(conn, wf) == "failed"
    conn.close()


def test_unusable_plan_json_falls_back_to_the_replan_rule():
    """Malformed / empty plan_json means 'no usable plan', NOT 'empty plan':
    without a replan event nothing may be superseded."""
    conn = fresh_db()
    wf = new_workflow(conn)
    a = mk(conn, wf, 1, "Alpha")
    b = mk(conn, wf, 2, "Beta")
    set_status(conn, a, "success")
    set_status(conn, b, "failed")
    for raw in ("not json", '{"tasks": []}', '{"tasks": null}', '[]'):
        conn.execute("UPDATE workflows SET plan_json=? WHERE workflow_id=?",
                     (raw, wf))
        assert repo_tasks.current_plan_keys(conn, wf) is None, raw
        assert outcome(conn, wf) == "failed", raw
    conn.close()


def test_stored_plan_takes_precedence_over_the_replan_event():
    """With a plan on the row the event heuristic is not consulted at all: a
    replan event after the failure does not supersede an in-plan failure."""
    conn = fresh_db()
    wf = new_workflow(conn)
    a = mk(conn, wf, 1, "Alpha")
    b = mk(conn, wf, 2, "Beta")
    set_status(conn, a, "success")
    set_status(conn, b, "failed", updated_at=plus_s(-3600))
    store_plan(conn, wf, "Alpha", "Beta")
    repo_events.append_event(conn, "workflow", wf, "warn", "replan 2/3")

    assert outcome(conn, wf) == "failed"
    conn.close()


# ============================================================ plumbing
def test_workflows_row_persists_the_plan_the_outcome_reads():
    """No migration was needed: ``workflows.plan_json`` exists from 001_init and
    ``set_plan`` is the write path the outcome reads back."""
    conn = fresh_db()
    wf = new_workflow(conn)
    assert "plan_json" in repo_workflows.get_workflow(conn, wf).keys()
    store_plan(conn, wf, "Alpha", "Beta")
    keys = repo_tasks.current_plan_keys(conn, wf)
    assert keys == frozenset({repo_tasks.normalize_plan_key("Alpha"),
                              repo_tasks.normalize_plan_key("Beta")})
    conn.close()


def test_outcome_of_unknown_workflow_is_done_like_an_empty_workflow():
    """No tasks at all: unchanged behaviour (nothing failed, nothing blocked)."""
    conn = fresh_db()
    wf = new_workflow(conn)
    assert outcome(conn, wf) == "done"
    assert outcome(conn, "no-such-workflow") == "done"
    conn.close()


def test_current_time_helpers_are_comparable():
    """The fallback compares TEXT timestamps lexicographically; both sides must
    use SQLite's 'YYYY-MM-DD HH:MM:SS' UTC format for that to be chronological."""
    assert len(utcnow_s()) == 19 and utcnow_s()[4] == "-"
    assert plus_s(-1) < utcnow_s() <= plus_s(1)


def test_reconcile_plan_keeps_plan_json_in_step_with_the_rows():
    """Write side and read side agree: applying a plan in place records it, so
    the outcome is computed against the plan that is actually in force."""
    conn = fresh_db()
    wf = new_workflow(conn)
    repo_tasks.reconcile_plan(conn, wf, plan_of("Alpha", "Beta"))
    assert repo_tasks.current_plan_keys(conn, wf) == frozenset({"alpha", "beta"})

    a = task_id(conn, wf, "Alpha")
    b = task_id(conn, wf, "Beta")
    set_status(conn, a, "success")
    set_status(conn, b, "failed")
    assert outcome(conn, wf) == "failed"          # Beta is still planned

    repo_tasks.reconcile_plan(conn, wf, plan_of("Alpha"))
    assert repo_tasks.current_plan_keys(conn, wf) == frozenset({"alpha"})
    assert outcome(conn, wf) == "done"
    conn.close()


def test_legacy_poison_reproduced_end_to_end():
    """The cadfa395 shape: a row went terminal before the write-side fix, the
    plan was then swapped underneath it, and every live task succeeded."""
    conn = fresh_db()
    wf = new_workflow(conn)
    old = mk(conn, wf, 1, "Old discarded approach")
    set_status(conn, old, "failed")               # terminal, never rewritten
    new = mk(conn, wf, 2, "New approach")
    set_status(conn, new, "success")
    assert outcome(conn, wf) == "failed"          # no plan stored yet

    store_plan(conn, wf, "New approach")          # the replan that dropped it
    assert outcome(conn, wf) == "done"
    assert repo_tasks.workflow_tasks_all_terminal(conn, wf)
    conn.close()
