"""Regression test for Claudio issue #133 - a parent issue that accumulated
~27 mostly-duplicated sub-issues.

Root cause: a replan went through ``repo_tasks.delete_workflow_tasks``, which
threw away each task's ``gh_issue_number`` along with the row. The re-inserted
task carried a brand-new ``task_id``, so the outbox idempotency key
``gh:gh_subissue:<task_id>:created`` looked never-seen and GitHub minted a fresh
sub-issue for work that already had one. Every replan cycle minted more, and
nothing ever closed the old ones - hence ~27 children for 3 tasks.

The fix reconciles a plan against the existing rows by ``plan_key``
(``repo_tasks.normalize_plan_key(title)``): matched entries are UPDATEd in
place and keep their ``task_id`` and ``gh_issue_number``, removed entries are
cancelled (never deleted) and get exactly one ``gh_close``, and only genuinely
new entries are inserted and projected.

Everything here runs against a migrated temp DB with no GitHub and no outbox
worker: ``httpx`` is stubbed before ``clams.github.projection`` is imported, and
the sub-issues the projection enqueues are stamped onto the tasks the way the
relay worker would.
"""
from __future__ import annotations

import sys
import types

# ``clams.github.client`` imports httpx at module scope and the projection
# imports the client. This test never talks to GitHub - the import only has to
# resolve - so stub httpx *before* importing clams.github.projection. Real
# installs (or an already-imported httpx) are left untouched.
_httpx_stub = types.ModuleType("httpx")


class _HTTPError(Exception):
    pass


class _RequestError(_HTTPError):
    pass


_httpx_stub.HTTPError = _HTTPError
_httpx_stub.RequestError = _RequestError
sys.modules.setdefault("httpx", _httpx_stub)

import json  # noqa: E402

import pytest  # noqa: E402

from clams.db import (connect, immediate, migrate,  # noqa: E402
                      repo_projects, repo_tasks, repo_workflows)
from clams.github import projection  # noqa: E402
from clams.planner.reconcile import apply_replanned_plan  # noqa: E402

REPO = "massimorofi/clams"
PARENT_ISSUE = 500
TITLES = ["Alpha task", "Beta task", "Gamma task"]


# ----------------------------------------------------------------- fixtures
def _widen_outbox_kinds(conn):
    """Recreate ``outbox`` with ``gh_close`` allowed by the CHECK constraint.

    The shipped ``001_init`` enum predates ``gh_close`` and widening it is a
    migration (out of scope here), exactly as in
    ``tests/unit/test_gh_close_issue.py`` / ``test_projection_replan.py``.
    Without this, ``enqueue`` (``INSERT OR IGNORE``) would silently enqueue
    nothing and this regression test could not see the close rows.
    """
    conn.execute("DROP TABLE IF EXISTS outbox")
    conn.execute(
        "CREATE TABLE outbox ("
        "  outbox_id INTEGER PRIMARY KEY,"
        "  kind TEXT NOT NULL CHECK (kind IN"
        "    ('bus_publish','gh_comment','gh_label','gh_board_move',"
        "     'gh_subissue','gh_close')),"
        "  payload TEXT NOT NULL,"
        "  idempotency_key TEXT NOT NULL UNIQUE,"
        "  status TEXT NOT NULL DEFAULT 'pending'"
        "    CHECK (status IN ('pending','inflight','sent','dead')),"
        "  attempts INTEGER NOT NULL DEFAULT 0,"
        "  next_attempt_at TEXT DEFAULT CURRENT_TIMESTAMP,"
        "  last_error TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_due "
                 "ON outbox(status, next_attempt_at)")


@pytest.fixture()
def db():
    """A migrated temp database whose outbox CHECK allows gh_close."""
    conn = connect(":memory:")
    migrate(conn)
    _widen_outbox_kinds(conn)
    yield conn
    conn.close()


@pytest.fixture()
def wf(db):
    """Project + workflow whose parent issue already exists on GitHub."""
    pid = repo_projects.upsert_project(db, REPO)
    return repo_workflows.create_workflow(db, pid, "github", "goal",
                                           PARENT_ISSUE)


# ------------------------------------------------------------------ helpers
def _entry(title, i):
    return {"title": title, "objective": f"objective {title}",
            "acceptance": f"acceptance {title}",
            "depends_on": [i - 1] if i else [], "max_attempts": 3,
            "brief": {"objective": title, "run": [f"echo {i}"],
                      "writable": ["*.py"], "repo": REPO, "base_ref": "main"}}


def _plan(titles):
    """Planner-shaped plan items (``depends_on`` holds sequence indices)."""
    return [_entry(t, i) for i, t in enumerate(titles)]


def _rows(db, kind):
    return db.execute("SELECT * FROM outbox WHERE kind=? ORDER BY outbox_id",
                      (kind,)).fetchall()


def _payloads(db, kind):
    return [json.loads(r["payload"]) for r in _rows(db, kind)]


def _subissues(db):
    """``gh_subissue`` rows that open a sub-issue (the ``:created`` key)."""
    return [r for r in _rows(db, "gh_subissue")
            if r["idempotency_key"].endswith(":created")]


def _snapshot(db, wf):
    """{task_id: gh_issue_number} for every live (non-cancelled) task."""
    return {r["task_id"]: r["gh_issue_number"]
            for r in repo_tasks.list_tasks(db, wf)
            if r["status"] != "cancelled"}


def _all_issues(db, wf):
    """{task_id: gh_issue_number} for every task, cancelled ones included."""
    return {r["task_id"]: r["gh_issue_number"]
            for r in repo_tasks.list_tasks(db, wf) if r["gh_issue_number"]}


def _replan(db, wf, titles):
    """One replan cycle: reconcile the plan, then project what is genuinely new.

    ``apply_replanned_plan`` already closes superseded sub-issues; the planner
    calls ``project_tasks_created`` right after to open the new ones.
    """
    with immediate(db):
        res = apply_replanned_plan(db, wf, _plan(titles))
        projected = projection.project_tasks_created(db, wf, REPO)
    return res, projected


def _initial_plan(db, wf):
    """Plan 3 tasks, project them, then stamp issue numbers like the worker."""
    with immediate(db):
        res = apply_replanned_plan(db, wf, _plan(TITLES))
        assert len(res["created"]) == 3 and not res["reused"]
        assert projection.project_tasks_created(db, wf, REPO) == 3
    rows = repo_tasks.list_tasks(db, wf)
    assert [r["title"] for r in rows] == TITLES
    for n, row in enumerate(rows, start=101):
        db.execute("UPDATE tasks SET gh_issue_number=? WHERE task_id=?",
                   (n, row["task_id"]))
    return _snapshot(db, wf)


# -------------------------------------------------------------------- tests
def test_repeated_identical_replan_changes_nothing(db, wf):
    """The same 3 titles replanned 3x: same task_ids, same issue numbers, and
    not one extra ``gh_subissue`` row."""
    issues = _initial_plan(db, wf)
    ids_before = [r["task_id"] for r in repo_tasks.list_tasks(db, wf)]
    assert len(_subissues(db)) == 3

    for i in range(3):
        res, projected = _replan(db, wf, TITLES)
        assert res["created"] == [], f"replan #{i + 1} inserted new tasks"
        assert not res.get("cancelled"), f"replan #{i + 1} superseded a task"
        assert len(res["reused"]) == 3
        assert set(res["reused"]) == {repo_tasks.normalize_plan_key(t)
                                      for t in TITLES}
        assert projected == 0, f"replan #{i + 1} projected a new sub-issue"
        assert [r["task_id"] for r in repo_tasks.list_tasks(db, wf)] == ids_before
        assert _snapshot(db, wf) == issues, "a task lost its gh_issue_number"
        assert len(_subissues(db)) == 3, "duplicate gh_subissue rows"
        assert _rows(db, "gh_close") == []

    keys = {r["idempotency_key"] for r in _subissues(db)}
    assert len(keys) == 3, "the outbox idempotency key must not churn"


def test_replan_drop_one_add_one_is_surgical(db, wf):
    """Drop 1 task, add 1: exactly one new ``gh_subissue`` and exactly one
    ``gh_close``, and that close names the dropped task's issue."""
    issues = _initial_plan(db, wf)
    for _ in range(3):
        _replan(db, wf, TITLES)
    before_sub = len(_subissues(db))
    dropped = next(r["task_id"] for r in repo_tasks.list_tasks(db, wf)
                   if r["title"] == "Gamma task")

    res, projected = _replan(db, wf,
                             ["Alpha task", "Beta task", "Delta task"])

    assert len(res["created"]) == 1
    assert res["cancelled"] == [dropped]
    assert projected == 1
    assert len(_subissues(db)) == before_sub + 1, \
        "exactly one new sub-issue for the one genuinely new task"
    closes = _payloads(db, "gh_close")
    assert len(closes) == 1
    assert closes[0]["issue"] == issues[dropped]
    assert dropped in closes[0]["comment"]
    # the dropped row survives (cancelled, never deleted): its issue stays
    # mapped, so it can be closed instead of orphaned
    row = db.execute("SELECT status, gh_issue_number FROM tasks "
                     "WHERE task_id=?", (dropped,)).fetchone()
    assert row["status"] == "cancelled"
    assert row["gh_issue_number"] == issues[dropped]
    # the two untouched tasks kept their identity and their sub-issue
    live = _snapshot(db, wf)
    assert len(live) == 3
    by_key = {r["plan_key"]: r["task_id"]
              for r in repo_tasks.list_tasks(db, wf)
              if r["status"] != "cancelled"}
    for title in ("Alpha task", "Beta task"):
        tid = by_key[repo_tasks.normalize_plan_key(title)]
        assert live[tid] == issues[tid]


def test_no_orphan_open_subissue(db, wf):
    """One ``gh_subissue`` per distinct plan_key ever planned - no orphans."""
    _initial_plan(db, wf)
    planned_keys = {repo_tasks.normalize_plan_key(t) for t in TITLES}
    for _ in range(3):
        _replan(db, wf, TITLES)
    final = ["Alpha task", "Beta task", "Delta task"]
    _replan(db, wf, final)
    planned_keys |= {repo_tasks.normalize_plan_key(t) for t in final}

    rows = _subissues(db)
    assert len(rows) == len(planned_keys) == 4, \
        "created sub-issues must equal distinct plan_keys ever planned"
    # one sub-issue per task, and no task projected twice
    projected_tasks = [json.loads(r["payload"])["task_id"] for r in rows]
    assert len(set(projected_tasks)) == len(projected_tasks)
    # every sub-issue still open belongs to a live task, and every live task
    # has exactly one - i.e. no orphan open sub-issue is left behind
    closed_tasks = {r["idempotency_key"].split(":")[2]
                    for r in _rows(db, "gh_close")}
    open_tasks = set(projected_tasks) - closed_tasks
    assert open_tasks == set(_snapshot(db, wf))
    assert len(open_tasks) == 3
    # and the issue numbers agree: open sub-issue numbers are exactly the ones
    # the live tasks carry (the new task's issue is still only queued)
    closed_issues = {p["issue"] for p in _payloads(db, "gh_close")}
    assert set(_all_issues(db, wf).values()) - closed_issues == \
        {v for v in _snapshot(db, wf).values() if v}
