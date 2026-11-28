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

from clams.db import (connect, migrate, repo_commands, repo_projects,
                      repo_tasks, repo_workflows)
from clams.planner import commands, reconcile

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


def test_recover_rejects_a_running_workflow_with_work_to_sweep(db):
    """Still live work to clean -> 'running' is not a recovery target."""
    wf = _workflow(db, status="running")
    _task(db, wf, 1, "done, issue still open", status="success",
          gh_issue_number=201)
    gh = FakeGH(labelled=[_issue(201, "done, issue still open")],
                subissues=[_issue(201, "done, issue still open")])

    with pytest.raises(ValueError, match="not blocked/failed"):
        commands._recover(db, wf, "operator", gh_client=gh)

    assert _close_rows(db) == []


def test_recover_on_a_clean_running_workflow_is_a_noop(db):
    """Requirement 3: planning/running + noop -> one event, zero writes."""
    wf = _workflow(db, status="running")
    _task(db, wf, 1, "alpha", status="success", gh_issue_number=201)
    gh = FakeGH(labelled=[_issue(201, "done", state="closed")],
                subissues=[])
    before = _snapshot(db)

    res = commands._recover(db, wf, "operator", gh_client=gh)

    assert res["noop"] is True
    assert res["orphan_count"] == 0
    assert res["stale_success_count"] == 0
    assert res["status"] == "running"
    assert _close_rows(db) == []
    assert _snapshot(db) == before, "the noop path must not touch any row"
    messages = _messages(db, wf)
    assert messages.count("recovery: nothing to recover by operator") == 1
    assert not [m for m in messages if m.startswith("workflow recovered:")]


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



# ------------------------------------------------------- whole-table snapshots
def _snapshot(db) -> list[tuple]:
    """Every task row, all columns, ordered by task_id (stable for compare)."""
    return [tuple(r) for r in db.execute(
        "SELECT * FROM tasks ORDER BY task_id").fetchall()]


def _outbox_snapshot(db) -> list[tuple]:
    return [tuple(r) for r in db.execute(
        "SELECT * FROM outbox ORDER BY outbox_id").fetchall()]


def _event_count(db) -> int:
    return db.execute("SELECT COUNT(*) n FROM events").fetchone()["n"]


def _success_plus_failed_tail(db, status="blocked"):
    """The scenario the recover command exists for.

    One finished step (success, sub-issue #201 already closed by the auto-close
    that shipped later) and a two-step tail that never got there: #202 failed,
    #203 was stranded blocked behind it. GitHub reports all three sub-issues
    closed, so there is nothing for the sweeps to clean: recovery's whole job
    here is the replan.
    """
    wf = _workflow(db, status)
    t_ok = _task(db, wf, 1, "step one", status="success", gh_issue_number=201)
    t_bad = _task(db, wf, 2, "step two", status="failed", depends_on=[1],
                  gh_issue_number=202)
    t_stranded = _task(db, wf, 3, "step three", status="blocked",
                       depends_on=[2], gh_issue_number=203)
    gh = FakeGH(labelled=[_issue(201, "step one", state="closed"),
                          _issue(202, "step two", state="closed"),
                          _issue(203, "step three", state="closed")],
                subissues=[])
    return wf, (t_ok, t_bad, t_stranded), gh


# ------------------------------------------- the end-to-end recover (via drain)
def test_recover_command_preserves_success_and_replans_the_tail(db):
    """One ``recover`` on a blocked workflow: clean up AND retry from the
    last successful step, with the finished work left exactly as it was."""
    wf, (t_ok, t_bad, t_stranded), gh = _success_plus_failed_tail(db)
    cid = repo_commands.insert_command(db, "recover", {}, "operator",
                                       target=wf)

    assert commands.drain_commands(db, gh_client=gh) == 1
    assert repo_commands.get(db, cid)["status"] == "applied"

    rows = {r["task_id"]: r for r in _rows(db, wf)}
    assert set(rows) == {t_ok, t_bad, t_stranded}, "no task row was minted"

    # the success task is untouched: same identity, same issue, still success
    ok = rows[t_ok]
    assert ok["task_id"] == t_ok
    assert ok["gh_issue_number"] == 201
    assert ok["status"] == "success"
    # ... and recovery did not queue a close for its (already closed) sub-issue
    assert [json.loads(r["payload"])["issue"] for r in _close_rows(db)] == []

    # the failed / blocked tail was re-queued into the plan, not cancelled
    assert rows[t_bad]["status"] == "failed"
    assert rows[t_stranded]["status"] == "blocked"
    assert [r["sequence_index"] for r in _rows(db, wf)] == [1, 2, 3]
    result = json.loads(repo_commands.get(db, cid)["result"])
    assert result["replanned"] == 3
    assert result["tasks_reused"] == 3
    assert result["tasks_created"] == 0
    assert result["tasks_cancelled"] == 0
    assert result["tasks_preserved"] == 1

    # the workflow left blocked/failed and the planner loop will pick it up
    assert repo_workflows.get_workflow(db, wf)["status"] == "planning"

    messages = _messages(db, wf)
    assert "replan requested by operator" in messages
    assert any(m.startswith("workflow recovered:") for m in messages)
    data = _last_data(db, wf, "workflow recovered:")
    assert data["status"] == "planning"
    assert data["tasks_preserved"] == 1
    assert data["replanned"] == 3
    assert data["orphans_closed"] == []
    assert data["stale_success_closed"] == []


def test_recover_twice_is_a_noop_with_zero_outbox_rows(db):
    """Second ``recover`` on the now-clean workflow writes nothing at all."""
    wf, (_t_ok, _t_bad, _t_stranded), gh = _success_plus_failed_tail(db)
    repo_commands.insert_command(db, "recover", {}, "operator", target=wf)
    assert commands.drain_commands(db, gh_client=gh) == 1

    tasks_before = _snapshot(db)
    outbox_before = _outbox_snapshot(db)
    events_before = _event_count(db)

    cid2 = repo_commands.insert_command(db, "recover", {}, "operator",
                                        target=wf)
    assert commands.drain_commands(db, gh_client=gh) == 1
    assert repo_commands.get(db, cid2)["status"] == "applied"

    # zero new outbox rows, and not one task column moved
    assert _outbox_snapshot(db) == outbox_before
    assert _snapshot(db) == tasks_before

    result = json.loads(repo_commands.get(db, cid2)["result"])
    assert result["noop"] is True
    assert result["orphan_count"] == 0
    assert result["stale_success_count"] == 0
    assert result["status"] == "planning"

    messages = _messages(db, wf)
    assert messages.count("recovery: nothing to recover by operator") == 1
    # that single noop line is the only thing the second run appended
    assert _event_count(db) == events_before + 1
    assert not [m for m in messages[messages.index(
        "recovery: nothing to recover by operator") + 1:]
        if m.startswith("workflow recovered:")]
    assert repo_workflows.get_workflow(db, wf)["status"] == "planning"



# --------------------------- the reconcile call is provably exercised (req. 1)
def test_reconcile_runs_even_with_nothing_to_replan(db, monkeypatch):
    """Requirement 1: *every* recover goes through the identity-preserving
    reconcile, including one with an empty plan tail."""
    calls: list[tuple] = []
    real = reconcile.apply_replanned_plan

    def spy(conn, workflow_id, planned, actor=None):
        calls.append((workflow_id, list(planned), actor))
        return real(conn, workflow_id, planned, actor=actor)

    monkeypatch.setattr(reconcile, "apply_replanned_plan", spy)

    wf = _workflow(db)                      # a workflow with zero tasks
    res = commands._recover(db, wf, "operator")

    assert len(calls) == 1, "reconcile must run even with nothing to re-plan"
    called_wf, planned, actor = calls[0]
    assert called_wf == wf
    assert actor == "operator", "the issuer is recorded on the reconcile"
    assert planned == []
    assert res["replanned"] == 0 and res["tasks_reused"] == 0
    assert repo_workflows.get_workflow(db, wf)["status"] == "planning"


def test_reconcile_receives_the_planned_tail_with_the_success_step_in_it(
        db, monkeypatch):
    """The planned list is built from the workflow's own rows: the finished
    step stays in it on purpose (a replan cancels every title the plan omits),
    and the failed/blocked tail follows in ladder order."""
    calls: list[list] = []
    real = reconcile.apply_replanned_plan

    def spy(conn, workflow_id, planned, actor=None):
        calls.append([dict(e) for e in planned])
        return real(conn, workflow_id, planned, actor=actor)

    monkeypatch.setattr(reconcile, "apply_replanned_plan", spy)

    wf = _workflow(db)
    _task(db, wf, 1, "step one", status="success", gh_issue_number=201)
    _task(db, wf, 2, "step two", status="failed", depends_on=[1],
          gh_issue_number=202)
    _task(db, wf, 3, "step three", status="blocked", depends_on=[2],
          gh_issue_number=203)

    commands._recover(db, wf, "operator")

    assert len(calls) == 1
    planned = calls[0]
    assert [e["title"] for e in planned] == \
        ["step one", "step two", "step three"]
    assert [e["depends_on"] for e in planned] == [[], [1], [2]]
    assert all(e.get("objective") and e.get("acceptance") for e in planned)
    assert all(isinstance(e.get("brief"), dict) for e in planned)


def test_noop_path_never_reconciles_and_never_touches_tasks(db, monkeypatch):
    """Requirement 3, structurally: the planning/running noop writes nothing,
    so the reconciler is not even called."""
    calls: list[tuple] = []
    monkeypatch.setattr(reconcile, "apply_replanned_plan",
                        lambda *a, **k: calls.append(a))

    wf = _workflow(db, status="planning")
    _task(db, wf, 1, "alpha", status="success", gh_issue_number=201)
    gh = FakeGH(labelled=[_issue(201, "alpha", state="closed")], subissues=[])
    tasks_before, outbox_before = _snapshot(db), _outbox_snapshot(db)

    res = commands._recover(db, wf, "operator", gh_client=gh)

    assert calls == [], "the noop path must not reconcile"
    assert res["noop"] is True and res["status"] == "planning"
    assert _snapshot(db) == tasks_before
    assert _outbox_snapshot(db) == outbox_before


# ------------------------- the audit line is a real workflow event row (req. 4)
def test_final_event_row_is_a_workflow_scoped_info_line(db):
    wf = _workflow(db)
    _task(db, wf, 1, "alpha", status="failed")

    commands._recover(db, wf, "operator")

    row = db.execute(
        "SELECT * FROM events WHERE scope='workflow' AND ref=? "
        "ORDER BY event_id DESC LIMIT 1", (wf,)).fetchone()
    assert row["scope"] == "workflow"
    assert row["level"] == "info"
    assert row["message"].startswith("workflow recovered:")
    data = json.loads(row["data"])
    for key in ("orphans_closed", "stale_success_closed", "tasks_preserved",
                "replanned", "status"):
        assert key in data, f"missing {key} in {sorted(data)}"


def test_success_task_identity_survives_a_real_sweep(db):
    """The sweep and the replan together must not disturb the finished step:
    same row, same issue number, still ``success``."""
    wf = _workflow(db)
    t_ok = _task(db, wf, 1, "done, issue still open", status="success",
                 gh_issue_number=201)
    t_run = _task(db, wf, 2, "still running", status="running",
                  gh_issue_number=202)
    gh = FakeGH(
        labelled=[_issue(201, "done, issue still open"),
                  _issue(202, "still running")],
        subissues=[_issue(201, "done, issue still open"),
                   _issue(202, "still running"),
                   _issue(900, "left behind by a replan")])

    res = commands._recover(db, wf, "operator", gh_client=gh)

    rows = {r["task_id"]: r for r in _rows(db, wf)}
    assert set(rows) == {t_ok, t_run}, "no row minted, none dropped"
    assert rows[t_ok]["gh_issue_number"] == 201
    assert rows[t_ok]["status"] == "success"
    assert res["orphans_closed"] == [900]
    assert res["stale_success_closed"] == [201]
    assert res["tasks_preserved"] == 1
    assert _last_data(db, wf, "workflow recovered:")["tasks_preserved"] == 1
