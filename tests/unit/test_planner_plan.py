"""WP4: planning via fake Claudio-P — valid, repair, blocked."""
from __future__ import annotations

import json

from clams.config import Settings
from clams.claudio_client import ChatResponse
from clams.db import repo_projects, repo_tasks, repo_workflows, repo_alerts
from clams.planner.plan import plan_workflow, extract_json


class ScriptedP:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def chat(self, message, **kw):
        self.prompts.append(message)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return ChatResponse(run_id="r", session_id="s", final_output=r,
                            usage={})


def _wf(db):
    pid = repo_projects.upsert_project(db, "o/r")
    return repo_workflows.create_workflow(db, pid, "github", "build a thing", 1)


GOOD = json.dumps({"tasks": [
    {"title": "add module", "objective": "create mod.py",
     "acceptance": "mod.py exists", "run": ["test -f mod.py && echo ok"],
     "writable": ["*.py"], "forbidden": [], "depends_on": []},
    {"title": "add test", "objective": "test it", "acceptance": "tests pass",
     "run": ["pytest -q"], "writable": ["tests/**"], "forbidden": [],
     "depends_on": [1]},
]})


def test_extract_json_with_prose():
    assert extract_json('Here you go:\n{"tasks": []}\nEnjoy!') == {"tasks": []}


def test_plan_creates_tasks(db):
    wf = _wf(db)
    s = Settings(_env_file=None)
    status = plan_workflow(db, ScriptedP([GOOD]), s, wf, "o/r")
    assert status == "running"
    tasks = repo_tasks.list_tasks(db, wf)
    assert [t["sequence_index"] for t in tasks] == [1, 2]
    assert tasks[1]["depends_on"] == "[1]"
    assert repo_workflows.get_workflow(db, wf)["status"] == "running"
    assert repo_workflows.get_workflow(db, wf)["plan_json"]


def test_plan_repair_retry_succeeds(db):
    wf = _wf(db)
    s = Settings(_env_file=None)
    p = ScriptedP(["not json at all", GOOD])
    status = plan_workflow(db, p, s, wf, "o/r")
    assert status == "running"
    assert "invalid" in p.prompts[1].lower() or "corrected" in p.prompts[1].lower()


def test_plan_blocked_after_two_bad(db):
    wf = _wf(db)
    s = Settings(_env_file=None)
    status = plan_workflow(db, ScriptedP(["garbage", "still garbage"]), s,
                           wf, "o/r")
    assert status == "blocked"
    assert repo_alerts.open_count(db) == 1


def test_plan_rejects_cyclic(db):
    wf = _wf(db)
    s = Settings(_env_file=None)
    cyclic = json.dumps({"tasks": [
        {"title": "a", "objective": "o", "acceptance": "a", "run": ["x"],
         "writable": ["*"], "depends_on": [2]},
        {"title": "b", "objective": "o", "acceptance": "a", "run": ["x"],
         "writable": ["*"], "depends_on": [1]},
    ]})
    status = plan_workflow(db, ScriptedP([cyclic, cyclic]), s, wf, "o/r")
    assert status == "blocked"


def test_replan_replaces_tasks_without_collision(db):
    """Regression: replanning a workflow that already has tasks used to hit
    UNIQUE(workflow_id, sequence_index)/(title) and stick in 'planning'."""
    wf = _wf(db)
    s = Settings(_env_file=None)
    assert plan_workflow(db, ScriptedP([GOOD]), s, wf, "o/r") == "running"
    # fail task 1 so the replan context has something to report
    t1 = repo_tasks.list_tasks(db, wf)[0]
    repo_tasks.transition(db, t1["task_id"], "ready")
    repo_tasks.transition(db, t1["task_id"], "dispatched")
    repo_tasks.transition(db, t1["task_id"], "running")
    db.execute("UPDATE tasks SET error_log='gate fail: no tests' WHERE task_id=?",
               (t1["task_id"],))
    repo_workflows.set_status(db, wf, "planning")   # what review does on replan
    # same plan back (identical titles -> the collision case)
    assert plan_workflow(db, ScriptedP([GOOD]), s, wf, "o/r") == "running"
    tasks = repo_tasks.list_tasks(db, wf)
    assert [t["sequence_index"] for t in tasks] == [1, 2]
    assert repo_workflows.get_workflow(db, wf)["status"] == "running"
    msgs = [r["message"] for r in db.execute(
        "SELECT message FROM events WHERE scope='workflow'").fetchall()]
    assert any("replaced" in m for m in msgs)


def test_replan_prompt_includes_prior_results(db):
    wf = _wf(db)
    s = Settings(_env_file=None)
    plan_workflow(db, ScriptedP([GOOD]), s, wf, "o/r")
    t1 = repo_tasks.list_tasks(db, wf)[0]
    db.execute("UPDATE tasks SET status='failed', error_log='boom' "
               "WHERE task_id=?", (t1["task_id"],))
    repo_workflows.set_status(db, wf, "planning")
    p = ScriptedP([GOOD])
    plan_workflow(db, p, s, wf, "o/r")
    assert "REPLAN" in p.prompts[0]
    assert "add module" in p.prompts[0]      # prior title carried
    assert "boom" in p.prompts[0]            # prior failure detail carried


def test_replan_blocked_keeps_old_tasks(db):
    """An invalid replan must not destroy the existing task set."""
    wf = _wf(db)
    s = Settings(_env_file=None)
    plan_workflow(db, ScriptedP([GOOD]), s, wf, "o/r")
    repo_workflows.set_status(db, wf, "planning")
    assert plan_workflow(db, ScriptedP(["nope", "still nope"]), s,
                         wf, "o/r") == "blocked"
    assert len(repo_tasks.list_tasks(db, wf)) == 2


def test_github_tasks_get_no_parent_issue_number(db):
    """Regression: tasks must not inherit the workflow's gh_issue_number —
    that silently disabled sub-issue creation (project_tasks_created skips
    tasks that already carry a number)."""
    from clams.github import projection
    from clams.db import repo_outbox
    pid = repo_projects.upsert_project(db, "o/r")
    wf = repo_workflows.create_workflow(db, pid, "github", "goal", 42)
    s = Settings(_env_file=None)
    plan_workflow(db, ScriptedP([GOOD]), s, wf, "o/r")
    for t in repo_tasks.list_tasks(db, wf):
        assert t["gh_issue_number"] is None
    n = projection.project_tasks_created(db, wf, "o/r")
    assert n == 2
    kinds = [r["kind"] for r in db.execute(
        "SELECT kind FROM outbox WHERE kind='gh_subissue'").fetchall()]
    assert kinds == ["gh_subissue", "gh_subissue"]
