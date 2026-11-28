"""reconcile_plan: replan in place, never delete, never re-mint task_id.

The bug this pins down: a replan used to hard-delete every task row and
re-insert it with a fresh task_id, so the gh_subissue outbox key
(``gh:gh_subissue:<task_id>:created``) looked new and GitHub collected a
duplicate sub-issue per replan. stdlib + clams.db only.

Runnable in a bare container: ``src`` is put on sys.path from ``__file__`` and
nothing outside stdlib + clams.db.* is imported.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from clams.db import connect, migrate                              # noqa: E402
from clams.db import repo_projects, repo_tasks, repo_workflows     # noqa: E402


@pytest.fixture()
def db(tmp_path):
    conn = connect(str(tmp_path / "clams.db"))
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture()
def wf(db):
    pid = repo_projects.upsert_project(db, "o/r")
    return repo_workflows.create_workflow(db, pid, "adhoc", "goal")


def entry(seq, title, **kw):
    d = {"sequence_index": seq, "title": title,
         "objective": f"obj {title}", "acceptance": f"acc {title}",
         "brief": {"steps": [f"do {title}"]}, "depends_on": [],
         "max_attempts": 3}
    d.update(kw)
    return d


def plan(*titles):
    return [entry(i, t) for i, t in enumerate(titles, start=1)]


def ids_of(db, wf):
    return [r["task_id"] for r in repo_tasks.list_tasks(db, wf)]


def count_tasks(db):
    return db.execute("SELECT COUNT(*) n FROM tasks").fetchone()["n"]


def row(db, task_id):
    return repo_tasks.get_task(db, task_id)


def by_key(db, wf):
    """{plan_key: task_id} as stored, for tests that need ids after a create."""
    return {repo_tasks.row_key(r): r["task_id"]
            for r in repo_tasks.list_tasks(db, wf)}


# ------------------------------------------------------- acceptance criteria
def test_reconciling_same_titles_twice_is_identity(db, wf):
    """Same 3 titles twice -> identical task_ids, reused len 3, created == []."""
    first = repo_tasks.reconcile_plan(db, wf, plan("Alpha", "Beta", "Gamma"))
    assert len(first["created"]) == 3
    before = ids_of(db, wf)

    again = repo_tasks.reconcile_plan(db, wf, plan("Alpha", "Beta", "Gamma"))
    assert again["created"] == []
    assert len(again["reused"]) == 3
    assert set(again["reused"]) == {"alpha", "beta", "gamma"}
    assert again["stale"] == []
    assert again["superseded_terminal"] == []
    assert sorted(again["reused"].values()) == sorted(before)
    assert ids_of(db, wf) == before
    assert count_tasks(db) == 3


def test_reconcile_never_issues_a_delete(db, wf):
    """Hard proof: no DELETE statement at all, whatever the plan does."""
    stmts: list[str] = []
    db.set_trace_callback(stmts.append)
    try:
        repo_tasks.reconcile_plan(db, wf, plan("Alpha", "Beta", "Gamma"))
        repo_tasks.reconcile_plan(db, wf, plan("Alpha", "Gamma"))
        repo_tasks.reconcile_plan(db, wf, plan("Zulu"))
        repo_tasks.reconcile_plan(db, wf, [])
    finally:
        db.set_trace_callback(None)
    deletes = [s for s in stmts if "delete" in s.lower()]
    assert deletes == []
    assert count_tasks(db) == 4            # every row ever created is still there


def test_gh_issue_number_survives_reconcile(db, wf):
    tid = repo_tasks.create_task(db, wf, 1, "Alpha", "o", "a", {},
                                 gh_issue_number=101)
    repo_tasks.create_task(db, wf, 2, "Beta", "o", "a", {})
    db.execute("UPDATE tasks SET gh_issue_number=101 WHERE task_id=?", (tid,))

    res = repo_tasks.reconcile_plan(db, wf, plan("Alpha", "Beta"))
    assert res["reused"][repo_tasks.normalize_plan_key("Alpha")] == tid
    assert row(db, tid)["gh_issue_number"] == 101
    # and it stays put across a second replan
    repo_tasks.reconcile_plan(db, wf, plan("Beta", "Alpha"))
    assert row(db, tid)["gh_issue_number"] == 101



def test_dropped_title_is_stale_cancelled_but_never_deleted(db, wf):
    first = repo_tasks.reconcile_plan(db, wf, plan("Alpha", "Beta", "Gamma"))
    assert len(first["created"]) == 3
    dropped = by_key(db, wf)["beta"]
    n_before = count_tasks(db)

    res = repo_tasks.reconcile_plan(db, wf, plan("Alpha", "Gamma"))
    assert res["stale"] == [dropped]
    assert row(db, dropped)["status"] == "cancelled"
    assert res["superseded_terminal"] == []   # it was 'pending', not terminal
    assert count_tasks(db) == n_before            # row still present
    assert db.execute("SELECT COUNT(*) n FROM tasks WHERE task_id=?",
                      (dropped,)).fetchone()["n"] == 1
    # a second reconcile with the same short plan re-reports it, still no delete
    res2 = repo_tasks.reconcile_plan(db, wf, plan("Alpha", "Gamma"))
    assert res2["stale"] == [dropped]
    assert count_tasks(db) == n_before


def test_adding_a_new_title_creates_exactly_one(db, wf):
    repo_tasks.reconcile_plan(db, wf, plan("Alpha", "Beta"))
    res = repo_tasks.reconcile_plan(db, wf, plan("Alpha", "Beta", "Delta"))
    assert len(res["created"]) == 1
    assert len(res["reused"]) == 2
    assert res["stale"] == []
    assert count_tasks(db) == 3
    new = row(db, res["created"][0])
    assert new["title"] == "Delta"
    assert new["status"] == "pending"


# --------------------------------------------------------------- more guards
def test_matched_row_updates_fields_but_not_identity(db, wf):
    res = repo_tasks.reconcile_plan(db, wf, plan("Alpha"))
    tid = res["created"][0]
    db.execute("UPDATE tasks SET gh_issue_number=7, attempt=2, status='running' "
               "WHERE task_id=?", (tid,))
    created_at = row(db, tid)["created_at"]

    plan2 = [entry(1, "Alpha", objective="new obj", acceptance="new acc",
                   brief={"steps": ["x"], "extra": 1}, depends_on=[9],
                   max_attempts=5)]
    out = repo_tasks.reconcile_plan(db, wf, plan2)
    r = row(db, tid)
    assert out["reused"] == {repo_tasks.normalize_plan_key("Alpha"): tid}
    assert out["created"] == [] and out["stale"] == []
    assert r["objective"] == "new obj"
    assert r["acceptance"] == "new acc"
    assert json.loads(r["brief_json"]) == {"steps": ["x"], "extra": 1}
    assert json.loads(r["depends_on"]) == [9]
    assert r["max_attempts"] == 5
    assert repo_tasks.row_key(r) == "alpha"
    # untouched by a match
    assert r["gh_issue_number"] == 7
    assert r["attempt"] == 2
    assert r["status"] == "running"
    assert r["created_at"] == created_at


def test_title_whitespace_and_case_still_match(db, wf):
    res = repo_tasks.reconcile_plan(db, wf, plan("Fix Replan Dedupe"))
    tid = res["created"][0]
    out = repo_tasks.reconcile_plan(
        db, wf, [entry(1, "  fix   REPLAN dedupe ")])
    assert out["created"] == [] and out["stale"] == []
    assert out["reused"] == {"fix replan dedupe": tid}


def test_duplicate_plan_keys_in_one_plan_first_wins(db, wf):
    dup = [entry(1, "Alpha", objective="first"),
           entry(2, "alpha", objective="second"),
           entry(3, "Beta")]
    out = repo_tasks.reconcile_plan(db, wf, dup)
    assert len(out["created"]) == 2
    assert count_tasks(db) == 2
    alpha = row(db, by_key(db, wf)["alpha"])
    assert alpha["objective"] == "first"


def test_renumbering_and_reordering_keeps_rows_unique(db, wf):
    first = repo_tasks.reconcile_plan(db, wf, plan("Alpha", "Beta", "Gamma"))
    assert len(first["created"]) == 3
    keys = by_key(db, wf)
    # reverse the order and renumber 1..3 -> no UNIQUE(workflow, sequence) clash
    out = repo_tasks.reconcile_plan(
        db, wf, [entry(1, "Gamma"), entry(2, "Beta"), entry(3, "Alpha")])
    assert out["created"] == [] and out["stale"] == []
    seqs = {r["sequence_index"]: r["task_id"]
            for r in repo_tasks.list_tasks(db, wf)}
    assert seqs == {1: keys["gamma"], 2: keys["beta"], 3: keys["alpha"]}
    assert count_tasks(db) == 3


def test_dropping_middle_title_renumbering_does_not_collide(db, wf):
    """The cancelled leftover must not block the new numbering."""
    first = repo_tasks.reconcile_plan(db, wf, plan("Alpha", "Beta", "Gamma"))
    assert len(first["created"]) == 3
    dropped = by_key(db, wf)["beta"]
    out = repo_tasks.reconcile_plan(db, wf, plan("Alpha", "Gamma"))
    assert out["stale"] == [dropped]
    assert count_tasks(db) == 3
    live = {r["sequence_index"]: r["title"]
            for r in repo_tasks.list_tasks(db, wf) if r["status"] != "cancelled"}
    assert live == {1: "Alpha", 2: "Gamma"}
    assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_delivered_success_is_stale_but_untouched(db, wf):
    """'success' is the one status a supersede never overrides: it records
    delivered work and must not stop the workflow reaching 'done'."""
    res = repo_tasks.reconcile_plan(db, wf, plan("Alpha", "Beta"))
    assert len(res["created"]) == 2
    done = by_key(db, wf)["beta"]
    repo_tasks.transition(db, done, "ready")
    repo_tasks.transition(db, done, "dispatched")
    repo_tasks.transition(db, done, "running")
    repo_tasks.transition(db, done, "success")

    out = repo_tasks.reconcile_plan(db, wf, plan("Alpha"))
    assert out["stale"] == [done]
    assert out["superseded_terminal"] == []
    assert row(db, done)["status"] == "success"   # delivered: left alone
    assert count_tasks(db) == 2


def test_empty_plan_cancels_everything_without_deleting(db, wf):
    repo_tasks.reconcile_plan(db, wf, plan("Alpha", "Beta"))
    before = set(ids_of(db, wf))
    out = repo_tasks.reconcile_plan(db, wf, [])
    assert set(out["stale"]) == before
    assert out["created"] == [] and out["reused"] == {}
    assert count_tasks(db) == 2
    assert all(row(db, t)["status"] == "cancelled" for t in before)


def test_reconcile_never_deletes_and_is_repeatable(db, wf):
    plans = [plan("A"), plan("A", "B"), plan("B", "C"), plan("A", "B", "C"),
             plan("A"), []]
    for p in plans:
        repo_tasks.reconcile_plan(db, wf, p)
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert count_tasks(db) == 3
    # running it twice in a row changes nothing
    snap = [(r["task_id"], r["sequence_index"], r["status"])
            for r in repo_tasks.list_tasks(db, wf)]
    repo_tasks.reconcile_plan(db, wf, [])
    assert [(r["task_id"], r["sequence_index"], r["status"])
            for r in repo_tasks.list_tasks(db, wf)] == snap


def test_reconcile_is_isolated_per_workflow(db, wf):
    other = repo_workflows.create_workflow(
        db, repo_projects.upsert_project(db, "o/r"), "adhoc", "goal 2")
    a = repo_tasks.reconcile_plan(db, wf, plan("Alpha"))
    b = repo_tasks.reconcile_plan(db, other, plan("Alpha"))
    assert set(a["created"]) != set(b["created"])
    assert count_tasks(db) == 2
    # a replan of one workflow never touches the other
    out = repo_tasks.reconcile_plan(db, wf, plan("Alpha", "Beta"))
    assert len(out["created"]) == 1 and out["stale"] == []
    assert len(repo_tasks.list_tasks(db, other)) == 1


def test_repo_layer_stays_stdlib_only():
    """reconcile_plan must not drag framework deps into the repo layer."""
    import ast
    import pathlib
    tree = ast.parse(
        pathlib.Path(repo_tasks.__file__).read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            mods.add(node.module.split(".")[0])
    assert mods <= {"__future__", "json", "sqlite3", "uuid", "typing", "time",
                    "contextlib"}, mods
