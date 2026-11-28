"""Recovery replans: ``recover`` rebuilds the plan tail and re-opens the workflow.

Extends ``test_recovery_sweeps.py`` (which owns the orphan / stale-success
sweep) with the four behaviours the recovery command gained:

* the plan tail is rebuilt from the surviving task rows and reconciled, so
  task identity survives and nothing is deleted;
* the workflow moves to ``planning`` and a ``replan requested by <issuer>``
  event is logged, which is what the planner loop picks up;
* a workflow with nothing to sweep takes the idempotent noop path: zero outbox
  rows and a ``nothing to recover`` event;
* every recovery ends with one ``workflow recovered:`` audit event carrying the
  full summary (sweep + reconcile counts).

No network: either ``gh_client=None`` (sub-issues derived from the DB) or a
recording fake, exactly as in the sweeps test.
"""
from __future__ import annotations

import json

import pytest

from clams.db import (connect, migrate, repo_projects, repo_tasks,
                      repo_workflows)
from clams.planner import commands

BRIEF = {"objective": "o", "acceptance": "a", "run": ["echo x"],
         "writable": ["*.py"], "repo": "o/r"}

WF_ISSUE = 500

_OUTBOX_WIDENED = """
CREATE TABLE outbox (
  outbox_id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN
    ('bus_publish','gh_comment','gh_label','gh_board_move','gh_subissue',
     'gh_close')),
  payload TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN
    ('pending','inflight','sent','dead')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT DEFAULT CURRENT_TIMESTAMP,
  last_error TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_outbox_due ON outbox(status, next_attempt_at);
"""


def _allow_gh_close(conn) -> None:
    """Add 'gh_close' to the outbox kind CHECK (idempotent)."""
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='outbox'"
    ).fetchone()["sql"]
    if "'gh_close'" in ddl:
        return
    conn.execute("PRAGMA foreign_keys=OFF")
    with conn:
        conn.execute("ALTER TABLE outbox RENAME TO outbox_old")
        conn.execute("DROP INDEX IF EXISTS idx_outbox_due")
        conn.execute(_OUTBOX_WIDENED)
        conn.execute("INSERT INTO outbox SELECT * FROM outbox_old")
        conn.execute("DROP TABLE outbox_old")


@pytest.fixture()
def db(tmp_path):
    conn = connect(str(tmp_path / "clams.db"))
    migrate(conn)
    _allow_gh_close(conn)
    yield conn
    conn.close()


class FakeGH:
    """Records calls; returns canned open issues (never touches the network)."""

    def __init__(self, labelled=(), subissues=()):
        self.labelled = [dict(i) for i in labelled]
        self.subissues = [dict(i) for i in subissues]
        self.calls: list[tuple] = []

    def list_issues_by_label(self, repo, label):
        self.calls.append(("list_issues_by_label", repo, label))
        return [dict(i) for i in self.labelled]

    def list_sub_issues(self, repo, parent):
        self.calls.append(("list_sub_issues", repo, parent))
        return [dict(i) for i in self.subissues]


def _issue(number, title, state="open"):
    return {"number": number, "title": title, "state": state}


def _workflow(db, status="blocked"):
    pid = repo_projects.upsert_project(db, "o/r")
    wf = repo_workflows.create_workflow(db, pid, "adhoc", "goal",
                                        gh_issue_number=WF_ISSUE)
    repo_workflows.set_status(db, wf, status)
    return wf


def _task(db, wf, seq, title, *, status="pending", depends_on=None,
          gh_issue_number=None):
    tid = repo_tasks.create_task(db, wf, seq, title, f"obj {title}",
                                 f"acc {title}", BRIEF,
                                 depends_on=depends_on or [],
                                 gh_issue_number=gh_issue_number)
    if status != "pending":
        db.execute("UPDATE tasks SET status=? WHERE task_id=?", (status, tid))
    return tid


def _rows(db, wf):
    return repo_tasks.list_tasks(db, wf)


def _by_title(db, wf):
    return {r["title"]: r for r in _rows(db, wf)}


def _events(db, wf):
    return [(r["message"], r["data"]) for r in db.execute(
        "SELECT message, data FROM events WHERE scope='workflow' AND ref=? "
        "ORDER BY event_id", (wf,)).fetchall()]


def _messages(db, wf):
    return [m for m, _d in _events(db, wf)]


def _close_rows(db):
    return db.execute(
        "SELECT * FROM outbox WHERE kind='gh_close'").fetchall()


def _last_data(db, wf, prefix):
    for message, data in reversed(_events(db, wf)):
        if message.startswith(prefix):
            return json.loads(data) if data else {}
    raise AssertionError(f"no event starting with {prefix!r}")


# ------------------------------------------------- status transition + replan
def test_recover_moves_workflow_to_planning_and_logs_replan_requested(db):
    wf = _workflow(db)
    _task(db, wf, 1, "alpha", status="failed")

    res = commands._recover(db, wf, "operator")

    assert res["status"] == "planning"
    assert repo_workflows.get_workflow(db, wf)["status"] == "planning"
    assert "replan requested by operator" in _messages(db, wf)


def test_recover_from_failed_workflow_also_reopens(db):
    wf = _workflow(db, status="failed")
    _task(db, wf, 1, "alpha", status="blocked")

    commands._recover(db, wf, "me")

    assert repo_workflows.get_workflow(db, wf)["status"] == "planning"
    assert "replan requested by me" in _messages(db, wf)


def test_recover_rejects_a_running_workflow(db):
    wf = _workflow(db, status="running")
    with pytest.raises(ValueError):
        commands._recover(db, wf, "operator")


# ------------------------------------------------------ plan-tail rebuild
def test_rebuild_preserves_task_ids_and_reladders(db):
    wf = _workflow(db)
    _task(db, wf, 1, "alpha", status="success")
    _task(db, wf, 2, "beta", status="failed", depends_on=[1])
    _task(db, wf, 3, "gamma", status="blocked", depends_on=[2])
    before = {t: r["task_id"] for t, r in _by_title(db, wf).items()}

    res = commands._recover(db, wf, "operator")

    after = {t: r["task_id"] for t, r in _by_title(db, wf).items()}
    assert after == before, "recovery must not mint new task_ids"
    assert [r["sequence_index"] for r in _rows(db, wf)] == [1, 2, 3]
    assert res["tasks_reused"] == 3
    assert res["tasks_created"] == 0 and res["tasks_cancelled"] == 0


def test_rebuild_never_deletes_a_task_row(db):
    wf = _workflow(db)
    _task(db, wf, 1, "alpha", status="success")
    _task(db, wf, 2, "beta", status="failed")
    n_before = len(_rows(db, wf))

    commands._recover(db, wf, "operator")

    assert len(_rows(db, wf)) == n_before


def test_rebuild_drops_cancelled_rows_and_remaps_deps(db):
    """A cancelled row leaves the tail; deps that pointed past it are renumbered."""
    wf = _workflow(db)
    _task(db, wf, 1, "gone", status="cancelled")
    _task(db, wf, 2, "beta", status="failed", depends_on=[1])
    _task(db, wf, 3, "gamma", status="blocked", depends_on=[2])

    commands._recover(db, wf, "operator")

    rows = {r["title"]: r for r in _rows(db, wf)}
    assert rows["beta"]["sequence_index"] == 1
    assert rows["gamma"]["sequence_index"] == 2
    # beta's dep pointed at the dropped row -> removed, not left dangling
    assert json.loads(rows["beta"]["depends_on"]) == []
    # gamma depended on beta (old 2 -> new 1)
    assert json.loads(rows["gamma"]["depends_on"]) == [1]
    assert rows["gone"]["status"] == "cancelled", "cancelled row is kept, not deleted"


def test_rebuild_is_a_fixed_point(db):
    """Recovering twice reconciles the same plan: no new rows the second time."""
    wf = _workflow(db)
    _task(db, wf, 1, "alpha", status="failed")
    _task(db, wf, 2, "beta", status="blocked", depends_on=[1])
    first = commands._recover(db, wf, "operator")
    # recovery moved it to 'planning'; put it back to exercise a second sweep
    repo_workflows.set_status(db, wf, "blocked")

    second = commands._recover(db, wf, "operator")

    assert second["tasks_created"] == first["tasks_created"] == 0
    assert second["tasks_reused"] == first["tasks_reused"] == 2
    assert [r["sequence_index"] for r in _rows(db, wf)] == [1, 2]


def test_recover_with_no_tasks_reconciles_nothing(db):
    wf = _workflow(db)

    res = commands._recover(db, wf, "operator")

    assert res["tasks_created"] == 0 and res["tasks_reused"] == 0
    assert repo_workflows.get_workflow(db, wf)["status"] == "planning"


# ------------------------------------------------------------- the noop path
def test_noop_path_queues_nothing_and_logs_nothing_to_recover(db):
    """Nothing published, nothing stale -> zero outbox rows + a noop event."""
    wf = _workflow(db)
    _task(db, wf, 1, "alpha", status="failed")

    res = commands._recover(db, wf, "operator")

    assert _close_rows(db) == []
    assert res["outbox_rows"] == 0
    assert res["noop"] is True
    assert "nothing to recover by operator" in _messages(db, wf)


def test_noop_path_is_idempotent(db):
    wf = _workflow(db)
    _task(db, wf, 1, "alpha", status="failed")

    commands._recover(db, wf, "operator")
    repo_workflows.set_status(db, wf, "blocked")
    second = commands._recover(db, wf, "operator")

    assert _close_rows(db) == []
    assert second["outbox_rows"] == 0
    assert _messages(db, wf).count("nothing to recover by operator") == 2


# ------------------------------------------------------- the sweep + summary
def test_sweep_counts_outbox_rows_and_reports_them(db):
    wf = _workflow(db)
    _task(db, wf, 1, "done, issue still open", status="success",
          gh_issue_number=201)
    _task(db, wf, 2, "still running", status="running", gh_issue_number=202)
    gh = FakeGH(
        labelled=[_issue(201, "done, issue still open"),
                  _issue(202, "still running")],
        subissues=[_issue(201, "done, issue still open"),
                   _issue(202, "still running"),
                   _issue(900, "left behind by a replan")],
    )

    res = commands._recover(db, wf, "operator", gh_client=gh)

    assert res["orphans_closed"] == [900]
    assert res["stale_success_closed"] == [201]
    assert res["outbox_rows"] == 2
    assert len(_close_rows(db)) == 2


def test_second_sweep_adds_no_outbox_rows(db):
    """Re-running the sweep dedups on the idempotency key: zero new rows."""
    wf = _workflow(db)
    _task(db, wf, 1, "done, issue still open", status="success",
          gh_issue_number=201)
    gh = FakeGH(labelled=[_issue(201, "done, issue still open")],
                subissues=[_issue(201, "done, issue still open")])

    commands._recover(db, wf, "operator", gh_client=gh)
    repo_workflows.set_status(db, wf, "blocked")
    second = commands._recover(db, wf, "operator", gh_client=gh)

    assert len(_close_rows(db)) == 1
    assert second["outbox_rows"] == 0


# --------------------------------------------------- the final audit event
def test_final_audit_event_is_last_and_carries_the_summary(db):
    wf = _workflow(db)
    _task(db, wf, 1, "alpha", status="failed")
    _task(db, wf, 2, "beta", status="blocked", depends_on=[1])

    commands._recover(db, wf, "operator")

    messages = _messages(db, wf)
    assert messages[-1].startswith("workflow recovered:"), messages
    data = _last_data(db, wf, "workflow recovered:")
    for key in ("orphans_closed", "stale_success_closed", "orphan_count",
                "stale_success_count", "preserved_success", "outbox_rows",
                "noop", "tasks_created", "tasks_reused", "tasks_cancelled",
                "status"):
        assert key in data, f"missing {key} in {sorted(data)}"
    assert data["status"] == "planning"
    assert data["tasks_reused"] == 2
    assert data["outbox_rows"] == 0
    assert data["noop"] is True
    assert "operator" in messages[-1]


def test_final_audit_event_records_the_sweep(db):
    wf = _workflow(db)
    _task(db, wf, 1, "done, issue still open", status="success",
          gh_issue_number=201)
    gh = FakeGH(labelled=[_issue(201, "done, issue still open")],
                subissues=[_issue(201, "done, issue still open")])

    commands._recover(db, wf, "operator", gh_client=gh)

    data = _last_data(db, wf, "workflow recovered:")
    assert data["stale_success_closed"] == [201]
    assert data["orphans_closed"] == []
    assert data["outbox_rows"] == 1
    assert data["noop"] is False
