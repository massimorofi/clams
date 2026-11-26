"""Retry work reuse: A = seed attempt N from attempt N-1's branch;
B = inject the previous attempt's gate failure into the retry prompt."""
from __future__ import annotations

import json
import os
import subprocess

from clams.bus import InMemoryBus
from clams.config import Settings
from clams.contracts import Brief
from clams.db import repo_outbox, repo_projects, repo_tasks, repo_workflows
from clams.executor.runner import ExecutorRunner, build_prompt
from clams.executor.worktree import WorktreeManager
from clams.planner.dispatch import dispatch_ready

_GIT_ENV = {**os.environ, "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": "*"}


def _git(*args, cwd=None):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    *args], cwd=cwd and str(cwd), check=True,
                   capture_output=True, env=_GIT_ENV)


def _src_repo(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _git("init", "-b", "main", str(src))
    (src / "a.txt").write_text("1")
    _git("add", ".", cwd=src)
    _git("commit", "-m", "base", cwd=src)
    return src


class _B:
    repo = "o/r"
    base_ref = "main"


# ---------------------------------------------------------------- A: seeding

def test_retry_base_seeds_from_previous_attempt(tmp_path):
    src = _src_repo(tmp_path)
    wt = WorktreeManager(tmp_path / "work", remote_url_template=str(src))
    p1, _ = wt.add("o/r", "task1", 1, "main")
    (p1 / "work.py").write_text("real work")
    _git("add", ".", cwd=p1)
    _git("commit", "-m", "attempt 1 work", cwd=p1)

    assert wt.ref_exists("o/r", wt.branch_name("task1", 1))
    assert not wt.ref_exists("o/r", wt.branch_name("task1", 9))

    r = ExecutorRunner.__new__(ExecutorRunner)
    r.wt = wt
    assert r._retry_base(_B(), "task1", 1) == "main"        # first attempt
    assert r._retry_base(_B(), "task1", 2) == "clams/task1-a1"

    p2, _ = wt.add("o/r", "task1", 2, r._retry_base(_B(), "task1", 2))
    assert wt.head_commit(p2) == wt.head_commit(p1)          # work carried over
    assert (p2 / "work.py").exists()


def test_retry_base_falls_back_when_no_previous_branch(tmp_path):
    src = _src_repo(tmp_path)
    wt = WorktreeManager(tmp_path / "work", remote_url_template=str(src))
    r = ExecutorRunner.__new__(ExecutorRunner)
    r.wt = wt
    wt.add("o/r", "task9", 5, "main")   # clone exists, but no a4 branch
    assert r._retry_base(_B(), "task9", 5) == "main"


def _commit(path, name, body="real work"):
    (path / name).write_text(body)
    _git("add", ".", cwd=path)
    _git("commit", "-m", f"{name} work", cwd=path)


def test_attempt1_seeds_from_integration_branch(tmp_path):
    """The claudio#133 fix: a chained task's FIRST attempt must start from its
    predecessors' merged work, not from base_ref."""
    src = _src_repo(tmp_path)
    wt = WorktreeManager(tmp_path / "work", remote_url_template=str(src))
    p1, _ = wt.add("o/r", "task1", 1, "main")
    _commit(p1, "work.py")
    wt.integrate("o/r", "w1", p1, wt.branch_name("task1", 1))

    r = ExecutorRunner.__new__(ExecutorRunner)
    r.wt = wt
    # a different task, first attempt: it has no previous branch of its own
    assert r._retry_base(_B(), "task2", 1, "w1") == "clams/wf/w1"
    p2, _ = wt.add("o/r", "task2", 1, r._retry_base(_B(), "task2", 1, "w1"))
    assert (p2 / "work.py").exists()
    # no workflow id, or a workflow that has not integrated anything yet
    assert r._retry_base(_B(), "task2", 1) == "main"
    assert r._retry_base(_B(), "task2", 1, "w-other") == "main"


def test_retry_prefers_previous_attempt_over_integration(tmp_path):
    """Work reuse must not regress: a retry resumes its OWN branch even when
    the shared tip has moved past it."""
    src = _src_repo(tmp_path)
    wt = WorktreeManager(tmp_path / "work", remote_url_template=str(src))
    p1, _ = wt.add("o/r", "task1", 1, "main")
    _commit(p1, "own.py")
    # another task advances the shared tip beyond task1's own branch
    p2, _ = wt.add("o/r", "task2", 1, "main")
    _commit(p2, "other.py")
    wt.integrate("o/r", "w1", p2, wt.branch_name("task2", 1))

    r = ExecutorRunner.__new__(ExecutorRunner)
    r.wt = wt
    assert r._retry_base(_B(), "task1", 2, "w1") == "clams/task1-a1"


# ---------------------------------------------------------------- B: prompt

def test_build_prompt_includes_prior_failure_block():
    b = Brief(objective="o", acceptance="a", run=["x"], writable=["*.py"],
              repo="o/r",
              prior_failure="gate `pytest -q` exit 1 | ModuleNotFoundError: x")
    out = build_prompt(b)
    assert "PREVIOUS ATTEMPT FAILED THE EVIDENCE GATE" in out
    assert "ModuleNotFoundError: x" in out
    assert "fix exactly these failures" in out
    # default path unchanged
    b2 = Brief(objective="o", acceptance="a", run=["x"], writable=["*.py"],
               repo="o/r")
    assert "PREVIOUS ATTEMPT" not in build_prompt(b2)


def test_dispatch_injects_prior_failure_on_attempt_two(db):
    s = Settings(_env_file=None)
    bus = InMemoryBus()
    pid = repo_projects.upsert_project(db, "o/r")
    wf = repo_workflows.create_workflow(db, pid, "adhoc", "g")
    brief = {"objective": "o", "acceptance": "a", "run": ["echo x"],
             "writable": ["*.py"], "repo": "o/r", "base_ref": "main"}
    t = repo_tasks.create_task(db, wf, 1, "t1", "o", "a", brief)
    dispatch_ready(db, bus, s)                       # attempt 1
    repo_outbox.claim_due(db)                        # drain first publish
    result = {"outcome": "failed", "evidence": {"passed": False, "checks": [
        {"check": "run", "cmd": "pytest -q", "exit": 1,
         "output_tail": "ModuleNotFoundError: No module named 'claudio_core'\n",
         "ok": False}]}}
    db.execute("UPDATE tasks SET result_json=? WHERE task_id=?",
               (json.dumps(result), t))
    db.commit()
    repo_tasks.requeue_after_lease(db, t)            # -> ready, attempt stays 1
    dispatch_ready(db, bus, s)                       # attempt 2
    row = repo_outbox.claim_due(db)[0]
    msg = json.loads(row["payload"])["msg"]
    assert msg["attempt"] == 2
    pf = msg["brief"]["prior_failure"]
    assert "pytest -q" in pf and "ModuleNotFoundError" in pf
    # attempt 1 briefs never carried it (stored brief stays clean)
    assert "prior_failure" not in json.loads(
        repo_tasks.get_task(db, t)["brief_json"])
