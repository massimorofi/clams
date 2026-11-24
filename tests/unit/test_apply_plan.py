"""P3: apply_plan command — operator-approved plan injection.
P4: replan context carries gate evidence + durations."""
from __future__ import annotations

import json

from clams.config import Settings
from clams.claudio_client import ChatResponse
from clams.db import repo_commands, repo_projects, repo_tasks, repo_workflows
from clams.planner.commands import drain_commands
from clams.planner.plan import plan_workflow

PLAN = {"tasks": [
    {"title": "step one", "objective": "o1", "acceptance": "a1",
     "run": ["echo one"], "writable": ["*.py"], "depends_on": []},
    {"title": "step two", "objective": "o2", "acceptance": "a2",
     "run": ["echo two"], "writable": ["*.py"], "depends_on": [1]},
]}


def _wf(db, status="blocked"):
    pid = repo_projects.upsert_project(db, "o/r")
    wf = repo_workflows.create_workflow(db, pid, "github", "goal", 7)
    brief = {"objective": "old", "acceptance": "old", "run": ["x"],
             "writable": ["*"], "repo": "o/r"}
    repo_tasks.create_task(db, wf, 1, "old task", "o", "a", brief)
    repo_workflows.set_status(db, wf, status)
    return wf


def test_apply_plan_swaps_tasks_and_runs(db):
    wf = _wf(db)
    repo_commands.insert_command(db, "apply_plan",
                                 {"workflow_id": wf, "plan": PLAN}, "web-ui")
    assert drain_commands(db) == 1
    row = repo_workflows.get_workflow(db, wf)
    assert row["status"] == "running"
    tasks = repo_tasks.list_tasks(db, wf)
    assert [t["title"] for t in tasks] == ["step one", "step two"]
    assert "old task" not in [t["title"] for t in tasks]
    brief = json.loads(tasks[0]["brief_json"])
    assert brief["repo"] == "o/r" and brief["run"] == ["echo one"]
    assert json.loads(row["plan_json"])["tasks"][1]["depends_on"] == [1]


def test_apply_plan_rejects_invalid_plan(db):
    wf = _wf(db)
    bad = {"tasks": [{"title": "t", "objective": "o", "acceptance": "a",
                      "run": [], "writable": ["*.py"], "depends_on": []}]}
    cid = repo_commands.insert_command(
        db, "apply_plan", {"workflow_id": wf, "plan": bad}, "web-ui")
    drain_commands(db)
    assert repo_commands.get(db, cid)["status"] == "rejected"
    # untouched
    assert repo_workflows.get_workflow(db, wf)["status"] == "blocked"
    assert [t["title"] for t in repo_tasks.list_tasks(db, wf)] == ["old task"]


def test_apply_plan_rejects_terminal_workflow(db):
    wf = _wf(db, status="done")
    cid = repo_commands.insert_command(
        db, "apply_plan", {"workflow_id": wf, "plan": PLAN}, "web-ui")
    drain_commands(db)
    assert repo_commands.get(db, cid)["status"] == "rejected"


def test_apply_plan_target_fallback(db):
    wf = _wf(db)
    repo_commands.insert_command(db, "apply_plan", {"plan": PLAN}, "web-ui",
                                 target=wf)
    assert drain_commands(db) == 1
    assert repo_workflows.get_workflow(db, wf)["status"] == "running"


# ------------------------------------------------------------------ P4

class _P:
    def __init__(self, reply):
        self.prompts = []
        self.reply = reply

    def chat(self, message, **kw):
        self.prompts.append(message)
        return ChatResponse(run_id="r", session_id="s",
                            final_output=self.reply, usage={})


def test_replan_context_includes_gate_evidence_and_duration(db):
    wf = _wf(db, status="failed")
    t = repo_tasks.list_tasks(db, wf)[0]["task_id"]
    result = {"outcome": "failed", "evidence": {"passed": False, "checks": [
        {"check": "run", "cmd": "python3 -m pytest test_x.py -q", "exit": 1,
         "output_tail": "collected 3 items\nModuleNotFoundError: No module named 'claudio_core'\n",
         "ok": False},
        {"check": "commits", "count": 1, "ok": True},
    ]}}
    db.execute("UPDATE tasks SET result_json=?, attempt=3, max_attempts=3, "
               "started_at='2026-11-24 10:00:00', updated_at='2026-11-24 11:00:00' "
               "WHERE task_id=?", (json.dumps(result), t))
    db.commit()
    p = _P(json.dumps(PLAN))
    plan_workflow(db, p, Settings(_env_file=None), wf, "o/r")
    prompt = p.prompts[0]
    assert "gate `python3 -m pytest test_x.py -q` exit 1" in prompt
    assert "ModuleNotFoundError" in prompt
    assert "attempt 3/3" in prompt
    assert "~60m" in prompt
    assert "REPLAN" in prompt
