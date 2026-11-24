"""Editable prompt templates: render, validate, patch command, dispatch."""
from __future__ import annotations

import json

from clams import contracts, prompts
from clams.db import repo_commands, repo_projects, repo_tasks, repo_workflows
from clams.executor.runner import build_prompt
from clams.planner.commands import drain_commands


def _brief(**kw) -> contracts.Brief:
    base = dict(objective="o", acceptance="a", run=["echo x"],
                writable=["*.py"], repo="o/r")
    base.update(kw)
    return contracts.Brief(**base)


# ---------------------------------------------------------------- rendering

def test_default_render_matches_builtin(db):
    out = prompts.render(db, "plan", max_tasks=5, repo="o/r",
                         base_ref="main", goal="g")
    assert "1-5 sequential tasks" in out and "Goal (repo o/r" in out


def test_override_used_and_reset(db):
    prompts.set_override(db, "plan", "custom {goal} {max_tasks} {repo} {base_ref}")
    assert prompts.render(db, "plan", max_tasks=1, repo="r",
                          base_ref="b", goal="G") == "custom G 1 r b"
    prompts.set_override(db, "plan", None)
    assert "custom" not in prompts.render(db, "plan", max_tasks=1, repo="r",
                                          base_ref="b", goal="G")


def test_broken_template_falls_back_to_default(db):
    # bypasses validate(): simulates a template that renders badly at run time
    prompts.set_override(db, "plan_repair", "oops {nonexistent}")
    out = prompts.render(db, "plan_repair", error="boom")
    assert "boom" in out and "oops" not in out


# ---------------------------------------------------------------- validation

def test_validate_rejects_unknown_and_missing_placeholders():
    assert prompts.validate("plan", "x {goal} {max_tasks} {repo} {base_ref}") is None
    assert "unknown placeholder" in prompts.validate("plan", "{goal} {oops}")
    assert "missing required" in prompts.validate("plan", "{goal}")
    assert prompts.validate("nope", "x") is not None
    assert prompts.validate("plan", None) is None  # reset always ok


# ---------------------------------------------------------------- command

def test_prompt_patch_command_apply_and_reject(db):
    repo_commands.insert_command(
        db, "prompt_patch",
        {"name": "executor_task", "template": "Do: {objective} {acceptance} "
         "{prior_failure_block}{writable} {forbidden_line} {run} "
         "{max_turns} {answer_block}"},
        "web-ui")
    assert drain_commands(db) == 1
    assert prompts.get_override(db, "executor_task") is not None
    # invalid template -> command rejected, override untouched
    cid = repo_commands.insert_command(
        db, "prompt_patch", {"name": "plan", "template": "{bogus}"}, "web-ui")
    drain_commands(db)
    assert repo_commands.get(db, cid)["status"] == "rejected"
    assert prompts.get_override(db, "plan") is None
    # reset
    repo_commands.insert_command(
        db, "prompt_patch", {"name": "executor_task", "template": ""}, "web-ui")
    drain_commands(db)
    assert prompts.get_override(db, "executor_task") is None


# ---------------------------------------------------------------- executor

def test_build_prompt_default_is_stable():
    b = _brief(forbidden=["docs/**"], answer="use pytest")
    out = build_prompt(b)
    assert "Forbidden paths (never touch): docs/**" in out
    assert "The human answered your earlier question: use pytest" in out


def test_build_prompt_uses_template_from_brief():
    b = _brief(prompt_template="ONLY: {objective} | {max_turns}")
    assert build_prompt(b) == "ONLY: o | 40"


def test_build_prompt_bad_template_falls_back():
    b = _brief(prompt_template="{nope}")
    assert build_prompt(b).startswith("Objective: o")


# ---------------------------------------------------------------- dispatch

def test_dispatch_injects_override(db):
    from clams.bus import InMemoryBus
    from clams.config import Settings
    from clams.db import repo_outbox
    from clams.planner.dispatch import dispatch_ready

    prompts.set_override(db, "executor_task", "T: {objective}")
    pid = repo_projects.upsert_project(db, "o/r")
    wf = repo_workflows.create_workflow(db, pid, "adhoc", "g")
    brief = {"objective": "o", "acceptance": "a", "run": ["echo x"],
             "writable": ["*.py"], "repo": "o/r", "base_ref": "main"}
    repo_tasks.create_task(db, wf, 1, "t1", "o", "a", brief)
    dispatch_ready(db, InMemoryBus(), Settings(_env_file=None))
    row = repo_outbox.claim_due(db)[0]
    msg = json.loads(row["payload"])["msg"]
    assert msg["brief"]["prompt_template"] == "T: {objective}"
    # stored brief_json stays clean (override applied at dispatch time only)
    task = repo_tasks.get_task(db, msg["task_id"])
    assert "prompt_template" not in json.loads(task["brief_json"])
