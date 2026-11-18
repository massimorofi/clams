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
