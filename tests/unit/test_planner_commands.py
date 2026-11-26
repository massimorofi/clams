"""WP4: command drain — every kind, applied or rejected."""
from __future__ import annotations

import json

from clams.db import (repo_commands, repo_projects, repo_questions,
                      repo_runtime, repo_tasks, repo_workflows)


def _wf_with_task(db, status="running"):
    pid = repo_projects.upsert_project(db, "o/r")
    wf = repo_workflows.create_workflow(db, pid, "adhoc", "goal")
    brief = {"objective": "o", "acceptance": "a", "run": ["echo x"],
             "writable": ["*.py"], "repo": "o/r"}
    t = repo_tasks.create_task(db, wf, 1, "t", "o", "a", brief)
    repo_tasks.transition(db, t, "ready")
    repo_tasks.transition(db, t, "dispatched")
    repo_tasks.transition(db, t, "running")
    repo_workflows.set_status(db, wf, status)
    return wf, t


def _drain(db):
    from clams.planner.commands import drain_commands
    return drain_commands(db)


def test_pause_resume_operator(db):
    repo_commands.insert_command(db, "pause", {}, "operator")
    assert _drain(db) == 1
    assert repo_runtime.is_paused(db) and repo_runtime.paused_by(db) == "operator"
    repo_commands.insert_command(db, "resume", {}, "operator")
    _drain(db)
    assert not repo_runtime.is_paused(db)


def test_cancel_task_and_workflow(db):
    wf, t = _wf_with_task(db)
    repo_commands.insert_command(db, "cancel", {}, "op", target=t)
    _drain(db)
    assert repo_tasks.get_task(db, t)["status"] == "cancelled"
    # cancel unknown target -> rejected
    cid = repo_commands.insert_command(db, "cancel", {}, "op", target="nope")
    _drain(db)
    assert repo_commands.get(db, cid)["status"] == "rejected"


def test_cancel_stops_claudio_run_and_clears_lease(db):
    """Issue #135.3: cancel must actually stop the running agent (best-effort
    claudio run cancel) and clear the lease so late results fence off."""
    from clams.planner.commands import drain_commands

    class FakeCE:
        def __init__(self):
            self.cancelled: list[str] = []

        def active_run(self, session_ref):
            return "run-42"

        def cancel(self, run_id):
            self.cancelled.append(run_id)
            return True

    wf, t = _wf_with_task(db)
    db.execute("UPDATE tasks SET session_ref='sess-9', lease_token='tok', "
               "lease_expires_at='2099-01-01T00:00:00+00:00' WHERE task_id=?",
               (t,))
    ce = FakeCE()
    repo_commands.insert_command(db, "cancel", {}, "op", target=wf)
    assert drain_commands(db, claudio_e=ce) == 1
    row = repo_tasks.get_task(db, t)
    assert row["status"] == "cancelled"
    assert row["lease_token"] is None and row["lease_expires_at"] is None
    assert ce.cancelled == ["run-42"]
    assert repo_workflows.get_workflow(db, wf)["status"] == "cancelled"


def test_replan_sets_planning(db):
    wf, _ = _wf_with_task(db, status="review")
    repo_commands.insert_command(db, "replan", {}, "op", target=wf)
    _drain(db)
    assert repo_workflows.get_workflow(db, wf)["status"] == "planning"


def test_retry_failed_task(db):
    wf, t = _wf_with_task(db)
    repo_tasks.transition(db, t, "failed")
    repo_commands.insert_command(db, "retry", {}, "op", target=t)
    _drain(db)
    row = repo_tasks.get_task(db, t)
    assert row["status"] == "ready" and row["attempt"] == 0


def test_answer_resumes_task(db):
    wf, t = _wf_with_task(db)
    repo_tasks.transition(db, t, "waiting_input")
    qid = repo_questions.open_question(db, t, 1, "which?", ["a", "b"], "k1")
    repo_commands.insert_command(db, "answer",
                                 {"question_id": qid, "answer": "a"},
                                 "massimorofi")
    _drain(db)
    row = repo_tasks.get_task(db, t)
    assert row["status"] == "ready"
    brief = json.loads(row["brief_json"])
    assert brief["answer"] == "a"
    assert repo_questions.get_for_task(db, t, 1)["status"] == "answered"


def test_adhoc_creates_workflow(db):
    repo_commands.insert_command(db, "adhoc", {"goal": "fix typo",
                                               "repo": "o/r"}, "op")
    _drain(db)
    wfs = repo_workflows.list_by_status(db, "intake")
    assert len(wfs) == 1 and wfs[0]["source"] == "adhoc"


def test_config_patch_projects(db):
    repo_commands.insert_command(
        db, "config_patch",
        {"projects": [{"repo": "o/new", "enabled": True,
                       "gh_project_node_id": "P1"}]}, "op")
    _drain(db)
    p = repo_projects.get_project(db, "o/new")
    assert p and p["gh_project_node_id"] == "P1"
    # invalid patch rejected
    cid = repo_commands.insert_command(db, "config_patch", {"projects": "x"}, "op")
    _drain(db)
    assert repo_commands.get(db, cid)["status"] == "rejected"


def test_config_patch_remove_project(db):
    repo_commands.insert_command(db, "config_patch",
                                 {"projects": [{"repo": "o/gone"}]}, "op")
    _drain(db)
    assert repo_projects.get_project(db, "o/gone") is not None
    cid = repo_commands.insert_command(
        db, "config_patch", {"projects": [{"repo": "o/gone", "remove": True}]}, "op")
    _drain(db)
    assert repo_commands.get(db, cid)["status"] == "applied"
    assert repo_projects.get_project(db, "o/gone") is None


def test_config_patch_remove_blocked_while_workflows_exist(db):
    _wf_with_task(db)  # creates project "o/r" with one workflow
    cid = repo_commands.insert_command(
        db, "config_patch", {"projects": [{"repo": "o/r", "remove": True}]}, "op")
    _drain(db)
    row = repo_commands.get(db, cid)
    assert row["status"] == "rejected"
    assert "workflow" in json.loads(row["result"])["error"]
    assert repo_projects.get_project(db, "o/r") is not None


def test_config_patch_remove_unknown_project_rejected(db):
    cid = repo_commands.insert_command(
        db, "config_patch", {"projects": [{"repo": "no/such", "remove": True}]}, "op")
    _drain(db)
    assert repo_commands.get(db, cid)["status"] == "rejected"


def test_config_patch_toggle_preserves_metadata(db):
    repo_commands.insert_command(
        db, "config_patch",
        {"projects": [{"repo": "o/t", "gh_project_node_id": "P9"}]}, "op")
    _drain(db)
    repo_commands.insert_command(
        db, "config_patch", {"projects": [{"repo": "o/t", "enabled": False}]}, "op")
    _drain(db)
    p = repo_projects.get_project(db, "o/t")
    assert p["enabled"] == 0 and p["gh_project_node_id"] == "P9"


class _FakeGH:
    def __init__(self, info):
        self.info = info
        self.calls = []

    def resolve_project(self, owner, number):
        self.calls.append((owner, number))
        return self.info


_BOARD_INFO = {"node_id": "PVT_1", "status_field_id": "PVTSSF_1",
               "options": {"Ready": "r1", "Done": "d1"}}


def _drain_gh(db, gh):
    from clams.planner.commands import drain_commands
    return drain_commands(db, gh_client=gh)


def test_config_patch_board_url_resolves(db):
    cid = repo_commands.insert_command(
        db, "config_patch",
        {"projects": [{"repo": "o/b",
                       "gh_board_url":
                       "https://github.com/users/massimorofi/projects/2"}]}, "op")
    gh = _FakeGH(_BOARD_INFO)
    _drain_gh(db, gh)
    assert gh.calls == [("massimorofi", 2)]
    assert repo_commands.get(db, cid)["status"] == "applied"
    p = repo_projects.get_project(db, "o/b")
    assert p["gh_project_node_id"] == "PVT_1"
    assert p["status_field_id"] == "PVTSSF_1"
    assert p["ready_option_id"] == "r1"


def test_config_patch_board_url_org_style_parses(db):
    repo_commands.insert_command(
        db, "config_patch",
        {"projects": [{"repo": "o/b",
                       "gh_board_url": "https://github.com/organizations/acme/projects/7"}]},
        "op")
    gh = _FakeGH(_BOARD_INFO)
    _drain_gh(db, gh)
    assert gh.calls == [("acme", 7)]


def test_config_patch_board_url_without_ready_option_rejected(db):
    cid = repo_commands.insert_command(
        db, "config_patch",
        {"projects": [{"repo": "o/b",
                       "gh_board_url": "github.com/users/o/projects/1"}]}, "op")
    _drain_gh(db, _FakeGH({"node_id": "PVT_1", "status_field_id": "F",
                           "options": {"Todo": "t"}}))
    row = repo_commands.get(db, cid)
    assert row["status"] == "rejected"
    assert "Ready" in json.loads(row["result"])["error"]


def test_config_patch_board_url_resolve_error_is_friendly(db):
    class _Boom(_FakeGH):
        def resolve_project(self, owner, number):
            raise RuntimeError("graphql errors: INSUFFICIENT_SCOPES ...")

    cid = repo_commands.insert_command(
        db, "config_patch",
        {"projects": [{"repo": "o/b",
                       "gh_board_url": "https://github.com/users/o/projects/1"}]}, "op")
    _drain_gh(db, _Boom(_BOARD_INFO))
    row = repo_commands.get(db, cid)
    assert row["status"] == "rejected"
    err = json.loads(row["result"])["error"]
    assert "project' scope" in err and "o/projects/1" in err


def test_config_patch_board_url_unparseable_rejected(db):
    cid = repo_commands.insert_command(
        db, "config_patch",
        {"projects": [{"repo": "o/b", "gh_board_url": "not a url"}]}, "op")
    _drain_gh(db, _FakeGH(_BOARD_INFO))
    assert repo_commands.get(db, cid)["status"] == "rejected"


def test_config_patch_board_url_without_gh_client_rejected(db):
    cid = repo_commands.insert_command(
        db, "config_patch",
        {"projects": [{"repo": "o/b",
                       "gh_board_url": "https://github.com/users/o/projects/1"}]}, "op")
    _drain(db)  # no gh_client wired
    row = repo_commands.get(db, cid)
    assert row["status"] == "rejected"
    assert "GitHub client" in json.loads(row["result"])["error"]


def test_bad_command_rejected_not_applied(db):
    cid = repo_commands.insert_command(db, "answer", {"question_id": 999}, "op")
    _drain(db)
    assert repo_commands.get(db, cid)["status"] == "rejected"


def test_settings_patch_applies_and_persists(db):
    from clams.db.conn import get_runtime_state
    from clams.config import Settings
    import json as _json
    repo_commands.insert_command(
        db, "settings_patch", {"settings": {"max_attempts": 7,
                                            "stall_alert_minutes": 30}},
        "operator", target=None)
    assert _drain(db) == 1
    row = db.execute("SELECT status, result FROM commands WHERE kind="
                     "'settings_patch'").fetchone()
    assert row["status"] == "applied", row["result"]
    ov = _json.loads(get_runtime_state(db, "settings_overrides"))
    assert ov == {"max_attempts": 7, "stall_alert_minutes": 30}


def test_settings_patch_rejects_unknown_and_out_of_range(db):
    repo_commands.insert_command(db, "settings_patch",
                                 {"settings": {"api_token": "x"}}, "op")
    repo_commands.insert_command(db, "settings_patch",
                                 {"settings": {"max_attempts": 99}}, "op")
    _drain(db)
    statuses = [r["status"] for r in db.execute(
        "SELECT status FROM commands WHERE kind='settings_patch' "
        "ORDER BY command_id")]
    assert statuses == ["rejected", "rejected"]


def test_retry_reopens_blocked_workflow(db):
    """claudio#133: review_workflow returns early for 'blocked', so a retried
    task could never drive its workflow back to done/replan."""
    wf, t = _wf_with_task(db, status="blocked")
    repo_tasks.transition(db, t, "failed")
    repo_commands.insert_command(db, "retry", {}, "op", target=t)
    _drain(db)
    assert repo_tasks.get_task(db, t)["status"] == "ready"
    assert repo_workflows.get_workflow(db, wf)["status"] == "running"


def test_retry_unblocks_cascade_blocked_dependents(db):
    """The gap: block_dependents strands 2 and 3, and nothing ever undid it,
    so retrying task 1 revived the head of the chain only."""
    pid = repo_projects.upsert_project(db, "o/r")
    wf = repo_workflows.create_workflow(db, pid, "adhoc", "goal")
    brief = {"objective": "o", "acceptance": "a", "run": ["echo x"],
             "writable": ["*.py"], "repo": "o/r"}
    t1 = repo_tasks.create_task(db, wf, 1, "a", "o", "a", brief)
    t2 = repo_tasks.create_task(db, wf, 2, "b", "o", "a", brief, depends_on=[1])
    t3 = repo_tasks.create_task(db, wf, 3, "c", "o", "a", brief, depends_on=[2])
    for st in ("ready", "dispatched", "running", "failed"):
        repo_tasks.transition(db, t1, st)
    from clams.planner.review import strand_dependents
    strand_dependents(db, wf)
    assert repo_tasks.get_task(db, t2)["status"] == "blocked"
    assert repo_tasks.get_task(db, t3)["status"] == "blocked"

    repo_commands.insert_command(db, "retry", {}, "op", target=t1)
    _drain(db)
    for st in ("dispatched", "running", "success"):
        repo_tasks.transition(db, t1, st)
    repo_tasks.promote_ready(db)
    assert repo_tasks.get_task(db, t2)["status"] == "ready"
    # ordering holds: 3 still waits on 2, it must not jump the queue
    assert repo_tasks.get_task(db, t3)["status"] == "blocked"

    for st in ("dispatched", "running", "success"):
        repo_tasks.transition(db, t2, st)
    repo_tasks.promote_ready(db)
    assert repo_tasks.get_task(db, t3)["status"] == "ready"
