"""T5: replan-aware GitHub projection.

Two guarantees, both proven against the real outbox table:

* ``project_tasks_created`` is *strictly* idempotent - a second pass for the
  same workflow enqueues nothing and returns 0, because ``repo_outbox.enqueue``
  dedupes on ``gh:gh_subissue:<task_id>:created``.
* ``project_tasks_superseded`` closes exactly the sub-issues of stale tasks
  that actually have one, on key ``gh:gh_subissue:<task_id>:closed``, and is
  itself idempotent.

The gate container has no third-party deps, so ``httpx`` is stubbed *before*
``clams.github.projection`` is imported (``clams.github.client`` imports it at
module scope). The same trick lets ``clams.planner.reconcile`` resolve the
projection at call time, so the replan wiring is exercised for real here.
"""
import sys, types  # noqa: E401
sys.modules.setdefault('httpx', types.ModuleType('httpx'))

import json  # noqa: E402

import pytest  # noqa: E402

from clams.db import (connect, immediate, migrate, repo_outbox,  # noqa: E402
                      repo_projects, repo_tasks, repo_workflows)
from clams.github import projection  # noqa: E402
from clams.planner.reconcile import apply_replanned_plan  # noqa: E402

REPO = "o/r"
PARENT_ISSUE = 100


# ----------------------------------------------------------------- fixtures
def _widen_outbox_kinds(conn):
    """Recreate ``outbox`` with ``gh_close`` allowed by the CHECK constraint.

    The shipped ``001_init`` enum predates ``gh_close`` and widening it
    is a migration (out of scope here), exactly as in
    ``tests/unit/test_gh_close_issue.py``. Without this, ``enqueue`` would hit
    the CHECK, swallow the ``IntegrityError`` and silently enqueue nothing.
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
    """A migrated, in-memory v2 database (outbox CHECK allows gh_close)."""
    conn = connect(":memory:")
    migrate(conn)
    _widen_outbox_kinds(conn)
    yield conn
    conn.close()


@pytest.fixture()
def wf(db):
    """A GitHub-sourced workflow whose parent issue exists (project o/r)."""
    pid = repo_projects.upsert_project(db, REPO)
    return repo_workflows.create_workflow(db, pid, "github", "goal",
                                           PARENT_ISSUE)


@pytest.fixture()
def wf_no_gh(db):
    """Same, but with no parent issue - the 'not wired to GitHub' case."""
    pid = repo_projects.upsert_project(db, REPO)
    return repo_workflows.create_workflow(db, pid, "github", "goal", None)


# ------------------------------------------------------------------ helpers
def _task(db, workflow_id, seq, title, issue=None):
    return repo_tasks.create_task(
        db, workflow_id, seq, title, f"objective {title}",
        f"acceptance {title}",
        {"run": [f"echo {title}"], "writable": ["*.py"]},
        gh_issue_number=issue)


def _stamp(db, task_id, issue):
    """Simulate the relay having created the sub-issue for this task."""
    db.execute("UPDATE tasks SET gh_issue_number=? WHERE task_id=?",
               (issue, task_id))


def _rows(db, kind):
    return db.execute(
        "SELECT * FROM outbox WHERE kind=? ORDER BY outbox_id",
        (kind,)).fetchall()


def _payloads(db, kind):
    return [json.loads(r["payload"]) for r in _rows(db, kind)]


def _entry(title, i, depends_on=None):
    return {"title": title, "objective": f"objective {title}",
            "acceptance": f"acceptance {title}",
            "depends_on": depends_on or [], "max_attempts": 3,
            "brief": {"objective": title, "run": [f"echo {i}"],
                      "writable": ["*.py"], "repo": REPO, "base_ref": "main"}}


def _plan(titles):
    return [_entry(t, i + 1, [i] if i else []) for i, t in enumerate(titles)]


def _replan(db, workflow_id, planned):
    with immediate(db):
        return apply_replanned_plan(db, workflow_id, planned)


# ----------------------------------------------- project_tasks_created
def test_created_twice_enqueues_exactly_one_row_per_task(db, wf):
    ids = [_task(db, wf, i, f"task-{i}") for i in (1, 2, 3)]

    first = projection.project_tasks_created(db, wf, REPO)
    second = projection.project_tasks_created(db, wf, REPO)

    assert first == 3
    assert second == 0, "a retried pass must report nothing new"
    rows = _rows(db, "gh_subissue")
    assert len(rows) == 3, "exactly one gh_subissue row per task"
    assert [r["idempotency_key"] for r in rows] == [
        f"gh:gh_subissue:{t}:created" for t in ids]
    assert repo_outbox.counts(db) == {"pending": 3}

    # a third pass changes nothing either
    assert projection.project_tasks_created(db, wf, REPO) == 0
    assert len(_rows(db, "gh_subissue")) == 3


def test_created_skips_tasks_that_already_have_a_subissue(db, wf):
    _task(db, wf, 1, "has-one", issue=555)
    provisioned = _task(db, wf, 2, "provisioned")
    _stamp(db, provisioned, 555)
    fresh = _task(db, wf, 3, "fresh")

    assert projection.project_tasks_created(db, wf, REPO) == 1
    assert [p["task_id"] for p in _payloads(db, "gh_subissue")] == [fresh]
    assert _payloads(db, "gh_subissue")[0]["parent"] == PARENT_ISSUE


def test_created_is_noop_without_parent_issue(db, wf_no_gh):
    _task(db, wf_no_gh, 1, "orphan")
    assert projection.project_tasks_created(db, wf_no_gh, REPO) == 0
    assert _rows(db, "gh_subissue") == []


def test_created_resolves_repo_from_project(db, wf):
    """Callers that do not know the repo still get a usable payload."""
    _task(db, wf, 1, "task-1")
    assert projection.project_tasks_created(db, wf, "") == 1
    assert _payloads(db, "gh_subissue")[0]["repo"] == REPO


# -------------------------------------------- project_tasks_superseded
def test_superseded_closes_only_tasks_with_a_subissue(db, wf):
    with_issue = _task(db, wf, 1, "closed-me", issue=11)
    without_issue = _task(db, wf, 2, "never-provisioned")
    stale = [with_issue, without_issue, "no-such-task-id", ""]

    n = projection.project_tasks_superseded(db, wf, REPO, stale)

    assert n == 1, "one row per stale task that has a gh_issue_number"
    rows = _rows(db, "gh_close")
    assert len(rows) == 1
    assert rows[0]["idempotency_key"] == f"gh:gh_subissue:{with_issue}:closed"
    payload = json.loads(rows[0]["payload"])
    assert payload["repo"] == REPO
    assert payload["issue"] == 11
    assert "superseded" in payload["comment"].lower()
    assert "replan" in payload["comment"].lower()
    assert without_issue not in json.dumps([r["payload"] for r in rows])


def test_superseded_rerun_adds_no_rows(db, wf):
    ids = [_task(db, wf, i, f"t{i}", issue=20 + i) for i in (1, 2)]

    assert projection.project_tasks_superseded(db, wf, REPO, ids) == 2
    assert projection.project_tasks_superseded(db, wf, REPO, ids) == 0
    assert len(_rows(db, "gh_close")) == 2, "idempotency-key dedupe"
    assert repo_outbox.counts(db) == {"pending": 2}


def test_superseded_empty_list_is_a_noop(db, wf):
    _task(db, wf, 1, "t1", issue=31)
    assert projection.project_tasks_superseded(db, wf, REPO, []) == 0
    assert _rows(db, "gh_close") == []


def test_superseded_is_noop_without_parent_issue(db, wf_no_gh):
    t = _task(db, wf_no_gh, 1, "t1", issue=41)
    assert projection.project_tasks_superseded(db, wf_no_gh, REPO, [t]) == 0
    assert _rows(db, "gh_close") == []


def test_superseded_ignores_foreign_task_ids(db, wf):
    other = repo_workflows.create_workflow(
        db, repo_projects.upsert_project(db, "other/repo"), "github", "g", 9)
    foreign = _task(db, other, 1, "elsewhere", issue=77)
    mine = _task(db, wf, 2, "mine", issue=78)

    assert projection.project_tasks_superseded(db, wf, REPO,
                                               [foreign, mine]) == 1
    keys = [r["idempotency_key"] for r in _rows(db, "gh_close")]
    assert keys == [f"gh:gh_subissue:{mine}:closed"]


def test_superseded_without_project_config_does_not_raise(db, wf):
    """Missing project config must not blow up a replan - just no rows."""
    t = _task(db, wf, 1, "t1", issue=91)
    pid = repo_workflows.get_workflow(db, wf)["project_id"]
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("DELETE FROM projects WHERE project_id=?", (pid,))
    db.execute("PRAGMA foreign_keys=ON")

    assert projection.project_tasks_superseded(db, wf, "", [t]) == 0
    assert _rows(db, "gh_close") == []
    # an unknown workflow id is equally harmless
    assert projection.project_tasks_superseded(
        db, "no-such-wf", REPO, [t]) == 0
    assert projection.project_tasks_created(db, "no-such-wf", REPO) == 0


# ------------------------------------------------------- replan wiring
def test_replan_closes_the_superseded_subissue(db, wf):
    _replan(db, wf, _plan(["alpha", "beta", "gamma"]))
    ids = {t["title"]: t["task_id"] for t in repo_tasks.list_tasks(db, wf)}
    for title, issue in (("alpha", 11), ("beta", 12), ("gamma", 13)):
        _stamp(db, ids[title], issue)

    res = _replan(db, wf, _plan(["alpha", "beta"]))

    assert res["stale"] == [ids["gamma"]]
    rows = _rows(db, "gh_close")
    assert [r["idempotency_key"] for r in rows] == [
        f"gh:gh_subissue:{ids['gamma']}:closed"]
    payload = json.loads(rows[0]["payload"])
    assert payload["issue"] == 13 and payload["repo"] == REPO
    assert "superseded" in payload["comment"].lower()
    # the kept tasks are untouched
    assert _rows(db, "gh_subissue") == []


def test_replan_twice_with_same_drop_does_not_double_close(db, wf):
    _replan(db, wf, _plan(["alpha", "beta"]))
    ids = {t["title"]: t["task_id"] for t in repo_tasks.list_tasks(db, wf)}
    _stamp(db, ids["beta"], 12)

    _replan(db, wf, _plan(["alpha"]))
    _replan(db, wf, _plan(["alpha"]))

    assert len(_rows(db, "gh_close")) == 1


def test_replan_without_gh_config_cannot_raise(db, wf_no_gh):
    _replan(db, wf_no_gh, _plan(["alpha", "beta"]))
    res = _replan(db, wf_no_gh, _plan(["alpha"]))
    assert len(res["stale"]) == 1
    assert _rows(db, "gh_close") == []
    assert _rows(db, "gh_subissue") == []


def test_replan_with_nothing_stale_enqueues_nothing(db, wf):
    _replan(db, wf, _plan(["alpha"]))
    _stamp(db, [t["task_id"] for t in repo_tasks.list_tasks(db, wf)][0], 12)
    res = _replan(db, wf, _plan(["alpha"]))
    assert res["stale"] == []
    assert _rows(db, "gh_close") == []
