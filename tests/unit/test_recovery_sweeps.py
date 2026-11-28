"""Recovery sweeps: the ``recover`` command closes orphans + stale successes.

Setup follows tests/unit/test_planner_commands.py (``clams.db`` repos, the
``db`` fixture, ``clams.planner.commands``). No network and no httpx: the gh
client is a fake that records calls and returns canned open sub-issues.

The ``db`` fixture below widens ``outbox.kind`` to admit ``gh_close``. That
CHECK widening is the recovery migration's job (owned by a separate change in
this series); until it lands here the fixture applies the same widening so the
sweep is exercised against a schema that can hold its rows. It is a no-op once
the migration is present.
"""
from __future__ import annotations

import json

import pytest

from clams.db import (connect, migrate, repo_commands, repo_projects,
                      repo_tasks, repo_workflows)
from clams.planner import commands

BRIEF = {"objective": "o", "acceptance": "a", "run": ["echo x"],
         "writable": ["*.py"], "repo": "o/r"}

WF_ISSUE = 500


# --------------------------------------------------------------------- schema
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
        conn.executescript(_OUTBOX_WIDENED)
        conn.execute("INSERT INTO outbox SELECT * FROM outbox_old")
        conn.execute("DROP TABLE outbox_old")


@pytest.fixture()
def db(tmp_path):
    """A migrated v2 database that can hold gh_close rows."""
    conn = connect(str(tmp_path / "clams.db"))
    migrate(conn)
    _allow_gh_close(conn)
    yield conn
    conn.close()


# ------------------------------------------------------------------ fake gh
class FakeGH:
    """Records every call; returns canned open sub-issues.

    ``labelled``   -> what ``list_issues_by_label`` (repo-wide clams:task) shows
    ``subissues``  -> what ``list_sub_issues`` (this workflow's parent) shows
    """

    def __init__(self, labelled=(), subissues=()):
        self.calls: list[tuple] = []
        self._labelled = list(labelled)
        self._subissues = list(subissues)

    def list_issues_by_label(self, repo, label, state="open"):
        self.calls.append(("list_issues_by_label", repo, label))
        return self._labelled

    def list_sub_issues(self, repo, parent):
        self.calls.append(("list_sub_issues", repo, parent))
        return self._subissues


def _issue(number, title, state="open"):
    return {"number": number, "title": title, "state": state}


# ------------------------------------------------------------------- fixtures
def _workflow(db, status="blocked", gh_issue_number=WF_ISSUE):
    pid = repo_projects.upsert_project(db, "o/r")
    wf = repo_workflows.create_workflow(db, pid, "adhoc", "goal",
                                        gh_issue_number=gh_issue_number)
    repo_workflows.set_status(db, wf, status)
    return wf


def _task(db, wf, seq, title, *, status="pending", gh_issue_number=None):
    tid = repo_tasks.create_task(db, wf, seq, title, "objective", "acceptance",
                                 BRIEF, gh_issue_number=gh_issue_number)
    if status != "pending":
        db.execute("UPDATE tasks SET status=? WHERE task_id=?", (status, tid))
    return tid


def _stale_and_orphan_fixture(db, status="blocked"):
    """1 stale-success (201), 1 healthy (202), 2 orphans (900, 901)."""
    wf = _workflow(db, status)
    t_ok = _task(db, wf, 1, "done, issue still open", status="success",
                 gh_issue_number=201)
    t_run = _task(db, wf, 2, "still running", status="running",
                  gh_issue_number=202)
    gh = FakeGH(
        labelled=[_issue(201, "done, issue still open"),
                  _issue(202, "still running")],
        subissues=[_issue(201, "done, issue still open"),
                   _issue(202, "still running"),
                   _issue(900, "left behind by a replan"),
                   _issue(901, "half-projected")],
    )
    return wf, (t_ok, t_run), gh


def _close_rows(db):
    rows = db.execute(
        "SELECT * FROM outbox WHERE kind='gh_close' ORDER BY outbox_id"
    ).fetchall()
    return [dict(r) for r in rows]


def _drain(db, gh):
    return commands.drain_commands(db, gh_client=gh)


# ------------------------------------------------------------------- the sweep
def test_blocked_workflow_closes_two_orphans_and_one_stale_success(db):
    """2 orphans + 1 stale success -> exactly 3 gh_close outbox rows."""
    wf, (t_ok, t_run), gh = _stale_and_orphan_fixture(db)
    cid = repo_commands.insert_command(db, "recover", {}, "operator", target=wf)

    assert _drain(db, gh) == 1

    rows = _close_rows(db)
    assert len(rows) == 3, [r["kind"] for r in rows]
    by_issue = {json.loads(r["payload"])["issue"]: json.loads(r["payload"])
                for r in rows}
    assert set(by_issue) == {900, 901, 201}
    assert 202 not in by_issue          # the still-running task's issue is safe

    # orphans: comment says superseded by recovery
    for number in (900, 901):
        payload = by_issue[number]
        assert "superseded by recovery" in payload["comment"]
        assert payload["repo"] == "o/r"
        assert payload["state"] == "closed"
        assert payload["reason"] == "superseded"
    # stale success: standard "Task succeeded" close comment
    assert "Task succeeded" in by_issue[201]["comment"]
    assert by_issue[201]["reason"] == "stale_success"

    # every row is a distinct idempotent intent
    keys = {r["idempotency_key"] for r in rows}
    assert len(keys) == 3
    assert all(k.startswith(f"gh:gh_close:{wf}:") for k in keys)

    # the command applied cleanly and reported the counts
    cmd = repo_commands.get(db, cid)
    assert cmd["status"] == "applied"
    result = json.loads(cmd["result"])
    assert result["orphan_count"] == 2
    assert result["stale_success_count"] == 1
    assert sorted(result["orphans_closed"]) == [900, 901]
    assert result["stale_success_closed"] == [201]


def test_recover_result_reports_numbers_and_counts(db):
    wf, _, gh = _stale_and_orphan_fixture(db)
    result = commands._recover(db, wf, "operator", gh_client=gh)

    assert result["workflow"] == wf
    assert sorted(result["orphans_closed"]) == [900, 901]
    assert result["stale_success_closed"] == [201]
    assert result["orphan_count"] == 2
    assert result["stale_success_count"] == 1


def test_recover_uses_the_gh_client_and_leaves_status_and_tasks_alone(db):
    wf, (t_ok, t_run), gh = _stale_and_orphan_fixture(db)
    commands._recover(db, wf, "operator", gh_client=gh)

    assert ("list_issues_by_label", "o/r", "clams:task") in gh.calls
    assert ("list_sub_issues", "o/r", WF_ISSUE) in gh.calls
    # this task must not touch the workflow status (task 4 owns that)
    assert repo_workflows.get_workflow(db, wf)["status"] == "blocked"
    # ... and must not delete a task row
    assert [t["task_id"] for t in repo_tasks.list_tasks(db, wf)] == [t_ok, t_run]


def test_recover_is_idempotent_on_a_second_sweep(db):
    """A second recover dedups on the idempotency key: still 3 rows."""
    wf, _, gh = _stale_and_orphan_fixture(db)
    commands._recover(db, wf, "operator", gh_client=gh)
    second = commands._recover(db, wf, "operator", gh_client=gh)

    assert len(_close_rows(db)) == 3
    assert second["orphan_count"] == 2   # classifier still reports the same plan
    assert second["stale_success_count"] == 1


def test_recover_appends_one_audit_event(db):
    wf, _, gh = _stale_and_orphan_fixture(db)
    commands._recover(db, wf, "operator", gh_client=gh)

    events = db.execute(
        "SELECT * FROM events WHERE scope='workflow' AND ref=?", (wf,)
    ).fetchall()
    assert len(events) == 1
    event = events[0]
    assert event["level"] == "info"
    assert event["message"] == (
        "recovery by operator: closed 2 orphan(s), 1 stale-success sub-issue(s)")
    data = json.loads(event["data"])
    assert data["orphan_count"] == 2
    assert data["stale_success_count"] == 1
    assert sorted(data["orphans_closed"]) == [900, 901]
    assert data["stale_success_closed"] == [201]


# ---------------------------------------------------------------- rejections
def test_running_workflow_is_rejected_by_the_drain(db):
    wf, _, gh = _stale_and_orphan_fixture(db, status="running")
    cid = repo_commands.insert_command(db, "recover", {}, "operator", target=wf)

    assert commands.drain_commands(db, gh_client=gh) == 0
    row = repo_commands.get(db, cid)
    assert row["status"] == "rejected"
    assert "not blocked/failed" in json.loads(row["result"])["error"]
    assert _close_rows(db) == []
    assert db.execute("SELECT COUNT(*) n FROM events").fetchone()["n"] == 0


def test_unknown_workflow_is_rejected_by_the_drain(db):
    cid = repo_commands.insert_command(db, "recover", {}, "operator",
                                       target="no-such-workflow")

    assert commands.drain_commands(db, gh_client=FakeGH()) == 0
    row = repo_commands.get(db, cid)
    assert row["status"] == "rejected"
    assert "unknown workflow" in json.loads(row["result"])["error"]
    assert _close_rows(db) == []


def test_recover_raises_directly_for_bad_targets(db):
    with pytest.raises(ValueError, match="unknown workflow"):
        commands._recover(db, "nope", "operator")
    wf = _workflow(db, status="review")
    with pytest.raises(ValueError, match="not blocked/failed"):
        commands._recover(db, wf, "operator")


def test_noop_sweep_still_records_an_event(db):
    """Nothing stale -> zero gh_close rows, but the audit line is still written."""
    wf = _workflow(db)
    _task(db, wf, 1, "never published", status="success")
    commands._recover(db, wf, "operator", gh_client=FakeGH())

    assert _close_rows(db) == []
    message = db.execute("SELECT message FROM events").fetchone()["message"]
    assert message == ("recovery by operator: closed 0 orphan(s), "
                       "0 stale-success sub-issue(s)")
