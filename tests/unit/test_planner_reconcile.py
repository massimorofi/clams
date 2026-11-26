"""T3: planner.reconcile.apply_replanned_plan - non-destructive replan swap.

Imports ONLY clams.planner.reconcile + clams.db and feeds plain dicts, so the
replan path is proven free of pydantic / clams.github / clams.planner.plan.
"""
from __future__ import annotations

import json

from clams.db import (connect, immediate, migrate, repo_projects, repo_tasks,
                      repo_workflows)
from clams.planner.reconcile import apply_replanned_plan

import pytest


@pytest.fixture()
def db(tmp_path):
    conn = connect(str(tmp_path / "reconcile.db"))
    migrate(conn)
    yield conn
    conn.close()


def _entry(seq_title, i, depends_on=None):
    """A plain planned-task dict, exactly what the replan entry points build."""
    return {
        "title": seq_title,
        "objective": f"objective {i}",
        "acceptance": f"acceptance {i}",
        "depends_on": depends_on or [],
        "max_attempts": 3,
        "brief": {"objective": seq_title, "run": [f"echo {i}"],
                  "writable": ["*.py"], "repo": "o/r", "base_ref": "main"},
    }


def _plan(titles):
    return [_entry(t, i + 1, [i] if i else []) for i, t in enumerate(titles)]


@pytest.fixture()
def wf(db):
    pid = repo_projects.upsert_project(db, "o/r")
    return repo_workflows.create_workflow(db, pid, "github", "goal", 7)


def _apply(db, wf, planned):
    with immediate(db):
        return apply_replanned_plan(db, wf, planned)


def _titles(db, wf):
    return [t["title"] for t in repo_tasks.list_tasks(db, wf)]


def _ids(db, wf):
    return {t["title"]: t["task_id"] for t in repo_tasks.list_tasks(db, wf)}


def _events(db, wf):
    return [r["message"] for r in db.execute(
        "SELECT message FROM events WHERE scope='workflow' AND ref=? "
        "ORDER BY event_id", (wf,)).fetchall()]


def test_first_apply_creates_and_logs_summary(db, wf):
    res = _apply(db, wf, _plan(["alpha", "beta"]))
    assert res["created_count"] == 2 and res["reused_count"] == 0
    assert res["cancelled_count"] == 0
    assert _titles(db, wf) == ["alpha", "beta"]
    assert _events(db, wf)[-1] == "replan: reused 0, created 2, cancelled 0"


def test_same_titles_twice_create_nothing(db, wf):
    first = _apply(db, wf, _plan(["alpha", "beta", "gamma"]))
    before = _ids(db, wf)

    second = _apply(db, wf, _plan(["alpha", "beta", "gamma"]))

    # the acceptance criterion: zero new task_ids for the same titles
    assert second["created"] == []
    assert second["created_count"] == 0
    assert second["reused_count"] == 3
    assert second["cancelled"] == []
    assert _ids(db, wf) == before
    assert sorted(second["reused"].values()) == sorted(before.values())
    assert _events(db, wf)[-1] == "replan: reused 3, created 0, cancelled 0"


def test_reorder_keeps_task_ids_and_renumbers(db, wf):
    _apply(db, wf, _plan(["alpha", "beta", "gamma"]))
    before = _ids(db, wf)

    _apply(db, wf, list(reversed(_plan(["alpha", "beta", "gamma"]))))

    after = _ids(db, wf)
    assert after == before, "identity must survive a re-ordered plan"
    rows = repo_tasks.list_tasks(db, wf)
    assert [r["sequence_index"] for r in rows] == [1, 2, 3]
    assert [r["title"] for r in rows] == ["gamma", "beta", "alpha"]


def test_sequence_index_stays_unique(db, wf):
    _apply(db, wf, _plan(["a", "b", "c", "d"]))
    _apply(db, wf, _plan(["c", "a", "e", "b"]))
    dupes = db.execute(
        "SELECT sequence_index, COUNT(*) c FROM tasks WHERE workflow_id=? "
        "GROUP BY sequence_index HAVING c>1", (wf,)).fetchall()
    assert dupes == []
    seqs = [r["sequence_index"] for r in repo_tasks.list_tasks(db, wf)
            if r["status"] != "cancelled"]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def test_dropped_title_is_cancelled_not_deleted(db, wf):
    _apply(db, wf, _plan(["alpha", "beta", "gamma"]))
    gamma = _ids(db, wf)["gamma"]

    res = _apply(db, wf, _plan(["alpha", "beta"]))

    assert res["cancelled_count"] == 1 and res["cancelled"] == [gamma]
    row = repo_tasks.get_task(db, gamma)
    assert row is not None, "reconcile must never DELETE a task row"
    assert row["status"] == "cancelled"
    assert res["created"] == [] and res["reused_count"] == 2
    assert _events(db, wf)[-1] == "replan: reused 2, created 0, cancelled 1"


def test_readded_title_matches_its_cancelled_row(db, wf):
    _apply(db, wf, _plan(["alpha", "beta"]))
    alpha = _ids(db, wf)["alpha"]
    _apply(db, wf, _plan(["alpha"]))
    res = _apply(db, wf, _plan(["alpha", "beta"]))
    assert res["created"] == []
    assert repo_tasks.get_task(db, alpha)["workflow_id"] == wf


def test_brief_objective_and_deps_are_written_through(db, wf):
    planned = _plan(["alpha", "beta"])
    planned[1]["depends_on"] = [1]
    planned[1]["objective"] = "changed objective"
    _apply(db, wf, planned)
    row = [t for t in repo_tasks.list_tasks(db, wf) if t["title"] == "beta"][0]
    assert row["objective"] == "changed objective"
    assert json.loads(row["depends_on"]) == [1]
    assert json.loads(row["brief_json"])["run"] == ["echo 2"]


def test_empty_plan_cancels_everything_and_logs(db, wf):
    _apply(db, wf, _plan(["alpha"]))
    res = _apply(db, wf, [])
    assert res["cancelled_count"] == 1 and res["created"] == []
    assert all(t["status"] == "cancelled"
               for t in repo_tasks.list_tasks(db, wf))
    assert _events(db, wf)[-1] == "replan: reused 0, created 0, cancelled 1"


def test_actor_is_attributed_in_event_payload(db, wf):
    from clams.planner.reconcile import apply_replanned_plan as f
    with immediate(db):
        f(db, wf, _plan(["alpha"]), actor="web-ui")
    row = db.execute("SELECT level, data FROM events WHERE scope='workflow' "
                     "ORDER BY event_id DESC", ()).fetchone()
    assert row["level"] == "info"
    assert json.loads(row["data"])["actor"] == "web-ui"


def test_cancelled_replan_is_logged_as_warn(db, wf):
    _apply(db, wf, _plan(["alpha", "gone"]))
    _apply(db, wf, _plan(["alpha"]))
    level = db.execute("SELECT level FROM events WHERE scope='workflow' "
                       "ORDER BY event_id DESC", ()).fetchone()["level"]
    assert level == "warn"


def test_module_stays_stdlib_only():
    """reconcile.py may import stdlib + clams.db repos, nothing else."""
    import ast
    import pathlib

    path = (pathlib.Path(__file__).resolve().parents[2]
            / "src" / "clams" / "planner" / "reconcile.py")
    tree = ast.parse(path.read_text())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mods.add(("." * node.level) + (node.module or ""))
        elif isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
    allowed = {"__future__", "sqlite3", "typing", "json", "uuid"}
    for m in mods:
        if m.startswith(".."):
            assert m in {"..db"}, f"unexpected import {m}"
        else:
            assert m.split(".")[0] in allowed, f"unexpected import {m}"
    assert not any(m in mods for m in ("pydantic", "clams.github"))
