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


def test_bad_command_rejected_not_applied(db):
    cid = repo_commands.insert_command(db, "answer", {"question_id": 999}, "op")
    _drain(db)
    assert repo_commands.get(db, cid)["status"] == "rejected"
