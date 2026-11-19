"""WP5: intake idempotency + two inbound gestures + projection via relay."""
from __future__ import annotations

import json

from clams.bus import InMemoryBus
from clams.db import (repo_commands, repo_outbox, repo_projects, repo_tasks,
                      repo_workflows)
from clams.github import projection
from clams.github.intake import run_intake
from clams.outbox import OutboxRelay


class FakeGH:
    def __init__(self, ready=None, cancelled=None, board=None, issues=None):
        self.ready = ready or []
        self.cancelled = cancelled or []
        self.board = board or {}          # {issue_number: status option name}
        self.issues = issues or {}        # {issue_number: issue dict}
        self.created = []
        self.moves = []

    def list_issues_by_label(self, repo, label, state="open"):
        if label == "status:Ready":
            return self.ready
        if label == "status:Cancelled":
            return self.cancelled
        return []

    def list_board_items(self, project_node_id):
        return dict(self.board)

    def get_issue(self, repo, number):
        return self.issues[number]

    def create_issue(self, repo, title, body, labels):
        self.created.append((repo, title, labels))
        return {"number": 100 + len(self.created)}

    def add_subissue(self, repo, parent, child, idempotency_key=""):
        return True

    def add_comment(self, *a, **k):
        return {}

    def set_labels(self, *a, **k):
        return {}

    def move_card(self, project_node_id, issue_number, option_name,
                  idempotency_key=""):
        self.moves.append((issue_number, option_name))
        return True


def _issue(n, title="do it", body="details", labels=()):
    return {"number": n, "title": title, "body": body,
            "labels": [{"name": l} for l in labels]}


def test_intake_enqueues_once(db):
    repo_projects.upsert_project(db, "o/r")
    gh = FakeGH(ready=[_issue(7)])
    assert run_intake(db, gh, None) == 1
    wf = repo_workflows.find_by_issue(db, 1, 7)
    assert wf and wf["status"] == "intake"
    # re-poll: no duplicate
    assert run_intake(db, gh, None) == 0
    assert len(repo_workflows.list_by_status(db)) == 1
    # outbox got the In Progress label projection
    keys = [r["idempotency_key"] for r in db.execute(
        "SELECT idempotency_key FROM outbox")]
    assert "gh:gh_label:7:inprogress" in keys


def test_intake_skips_clams_subissues(db):
    repo_projects.upsert_project(db, "o/r")
    gh = FakeGH(ready=[_issue(8, labels=["clams:task"])])
    assert run_intake(db, gh, None) == 0


def test_intake_cancelled_issues_cancel_command(db):
    pid = repo_projects.upsert_project(db, "o/r")
    wf_id = repo_workflows.create_workflow(db, pid, "github", "g", 9)
    gh = FakeGH(cancelled=[_issue(9)])
    run_intake(db, gh, None)
    cmds = repo_commands.fetch_new(db)
    assert len(cmds) == 1 and cmds[0]["kind"] == "cancel"
    assert cmds[0]["target"] == wf_id


def test_projection_subissue_created_via_relay(db):
    pid = repo_projects.upsert_project(db, "o/r")
    wf = repo_workflows.create_workflow(db, pid, "github", "g", 10)
    brief = {"objective": "o", "acceptance": "a", "run": ["x"],
             "writable": ["*"], "repo": "o/r"}
    t = repo_tasks.create_task(db, wf, 1, "the task", "o", "a", brief)
    assert projection.project_tasks_created(db, wf, "o/r") == 1
    gh = FakeGH()
    linked = {}

    def on_created(tid, n):
        linked[tid] = n
        db.execute("UPDATE tasks SET gh_issue_number=? WHERE task_id=?",
                   (n, tid))

    relay = OutboxRelay(InMemoryBus(), gh_client=gh,
                        on_subissue_created=on_created)
    out = relay.relay_once(db)
    assert out["sent"] == 1
    assert linked == {t: 101}
    row = repo_tasks.get_task(db, t)
    assert row["gh_issue_number"] == 101
    # re-project: no duplicate enqueue
    assert projection.project_tasks_created(db, wf, "o/r") == 0


# ------------------------------------------------------------- board-driven


def _board_project(db, repo="o/r", node="PVT_1"):
    return repo_projects.upsert_project(db, repo, gh_project_node_id=node)


def _outbox_rows(db):
    return [dict(r) for r in db.execute(
        "SELECT kind, payload, idempotency_key FROM outbox")]


def test_intake_board_ready_enqueues_and_moves(db):
    _board_project(db)
    gh = FakeGH(board={56: "Ready"}, issues={56: _issue(56, "Compare agents")})
    assert run_intake(db, gh, None) == 1
    wf = repo_workflows.find_by_issue(db, 1, 56)
    assert wf and wf["status"] == "intake"
    rows = _outbox_rows(db)
    moves = [r for r in rows if r["kind"] == "gh_board_move"]
    assert len(moves) == 1
    payload = json.loads(moves[0]["payload"])
    assert payload["option_name"] == "In progress"
    assert payload["project_node_id"] == "PVT_1"
    # board-only: no status label projection on the parent issue
    assert not [r for r in rows if r["kind"] == "gh_label"]
    # re-poll: idempotent
    assert run_intake(db, gh, None) == 0


def test_intake_board_backlog_cancels_active_workflow(db):
    pid = _board_project(db)
    wf_id = repo_workflows.create_workflow(db, pid, "github", "g", 5)
    gh = FakeGH(board={5: "Backlog"})
    run_intake(db, gh, None)
    cmds = repo_commands.fetch_new(db)
    assert len(cmds) == 1 and cmds[0]["kind"] == "cancel"
    assert cmds[0]["target"] == wf_id


def test_intake_board_skips_clams_subissues(db):
    _board_project(db)
    gh = FakeGH(board={6: "Ready"},
                issues={6: _issue(6, labels=["clams:task"])})
    assert run_intake(db, gh, None) == 0


def test_intake_board_skips_other_columns(db):
    _board_project(db)
    gh = FakeGH(board={7: "Done", 8: "In progress", 9: "In review"})
    assert run_intake(db, gh, None) == 0


def test_projection_workflow_state_board_moves(db):
    pid = _board_project(db)
    wf = repo_workflows.create_workflow(db, pid, "github", "g", 12)
    projection.project_workflow_state(db, wf, "o/r", "done")
    projection.project_workflow_state(db, wf, "o/r", "blocked")
    rows = _outbox_rows(db)
    assert [r["kind"] for r in rows] == ["gh_board_move", "gh_board_move"]
    opts = [json.loads(r["payload"])["option_name"] for r in rows]
    assert opts == ["Done", "In review"]   # blocked -> In review (no column)
    assert not [r for r in rows if r["kind"] == "gh_label"]


def test_projection_workflow_state_label_when_no_board(db):
    pid = repo_projects.upsert_project(db, "o/r")
    wf = repo_workflows.create_workflow(db, pid, "github", "g", 13)
    projection.project_workflow_state(db, wf, "o/r", "done")
    rows = _outbox_rows(db)
    assert [r["kind"] for r in rows] == ["gh_label"]
    assert json.loads(rows[0]["payload"])["labels"] == ["status:Done"]


def test_projection_task_state_labels(db):
    pid = repo_projects.upsert_project(db, "o/r")
    wf = repo_workflows.create_workflow(db, pid, "github", "g", 11)
    brief = {"objective": "o", "acceptance": "a", "run": ["x"],
             "writable": ["*"], "repo": "o/r"}
    t = repo_tasks.create_task(db, wf, 1, "task", "o", "a", brief)
    db.execute("UPDATE tasks SET gh_issue_number=50 WHERE task_id=?", (t,))
    projection.project_task_state(db, t, "o/r", "success", 1)
    row = db.execute("SELECT payload FROM outbox").fetchone()
    payload = json.loads(row["payload"])
    assert payload["labels"] == ["status:Done"]
    assert payload["issue"] == 50
