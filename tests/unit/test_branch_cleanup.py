"""Post-integration branch cleanup: after a workflow lands on the base
branch, its fully-merged task branches are deleted from origin.

Regression for the #139-#144 situation: workflows ended leaving every
``clams/<task>-aN`` branch on origin even though each tip was already an
ancestor of main — merged litter that looked unmerged.
"""
from __future__ import annotations

import subprocess

import pytest

from clams import contracts
from clams.bus import InMemoryBus
from clams.config import Settings
from clams.executor.runner import ExecutorRunner
from clams.executor.worktree import GitError, WorktreeManager

_ENV = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": "*"}


def _git(*args, cwd):
    import os
    env = {**os.environ, **_ENV}
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True, env=env).stdout


def _heads(remote) -> list[str]:
    out = subprocess.run(["git", "ls-remote", "--heads", str(remote)],
                         check=True, capture_output=True, text=True).stdout
    return [line.split("refs/heads/", 1)[1] for line in out.splitlines()]


@pytest.fixture()
def git_repo(tmp_path):
    """A bare remote with a `main`, plus a WorktreeManager rooted in tmp."""
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                   capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(seed)], check=True,
                   capture_output=True)
    _git("config", "user.email", "t@t", cwd=seed)
    _git("config", "user.name", "t", cwd=seed)
    (seed / "hello.txt").write_text("hi\n")
    _git("add", ".", cwd=seed)
    _git("commit", "-m", "init", cwd=seed)
    _git("remote", "add", "origin", str(remote), cwd=seed)
    _git("push", "-u", "origin", "main", cwd=seed)
    return WorktreeManager(tmp_path / "work",
                           remote_url_template=str(remote)), remote


def _make_task(wt, task_id, attempt=1, base="main", name=None):
    """Add a worktree, commit one file, push it; return (path, branch)."""
    path, branch = wt.add("o/r", task_id, attempt, base)
    (path / (name or f"{task_id}.txt")).write_text(f"{task_id} a{attempt}\n")
    _git("add", ".", cwd=path)
    _git("commit", "-m", f"{task_id} work", cwd=path)
    wt.push(path, branch)
    return path, branch


def _land_workflow(wt, task_ids):
    """Chain tasks onto the wf ref, integrate each, merge to main.

    Returns the pushed sha.
    """
    tip = "main"
    for i, tid in enumerate(task_ids, start=1):
        path, branch = _make_task(wt, tid, base=tip, name=f"f{i}.txt")
        wt.integrate("o/r", "w1", path, branch)
        tip = wt.integration_branch("w1")
    return wt.merge_to_base("o/r", wt.integration_branch("w1"), "main")


# ------------------------------------------------------------- worktree level

def test_cleanup_deletes_merged_task_branches(git_repo):
    wt, remote = git_repo
    sha = _land_workflow(wt, ["t1", "t2"])
    assert {"clams/t1-a1", "clams/t2-a1"} <= set(_heads(remote))

    deleted = wt.cleanup_merged_branches("o/r", sha)

    assert sorted(deleted) == ["clams/t1-a1", "clams/t2-a1"]
    heads = _heads(remote)
    assert "clams/t1-a1" not in heads and "clams/t2-a1" not in heads
    assert "main" in heads  # the base branch itself is never touched


def test_cleanup_keeps_unmerged_branch(git_repo):
    """A branch that never made it into the integration is stranded work:
    deleting it would destroy the only copy of the commits."""
    wt, remote = git_repo
    # t1 lands; t2 is pushed but never integrated
    path1, branch1 = _make_task(wt, "t1", name="one.txt")
    wt.integrate("o/r", "w1", path1, branch1)
    _make_task(wt, "t2", base=wt.integration_branch("w1"), name="two.txt")
    sha = wt.merge_to_base("o/r", wt.integration_branch("w1"), "main")

    deleted = wt.cleanup_merged_branches("o/r", sha)

    assert deleted == ["clams/t1-a1"]
    assert "clams/t2-a1" in _heads(remote)


def test_cleanup_keeps_retry_branch_not_merged(git_repo):
    """An a2 retry that failed the gate (never integrated) stays."""
    wt, remote = git_repo
    path1, branch1 = _make_task(wt, "t1", name="one.txt")
    wt.integrate("o/r", "w1", path1, branch1)
    _make_task(wt, "t1", attempt=2, base=wt.integration_branch("w1"))
    sha = wt.merge_to_base("o/r", wt.integration_branch("w1"), "main")

    deleted = wt.cleanup_merged_branches("o/r", sha)

    assert deleted == ["clams/t1-a1"]
    assert "clams/t1-a2" in _heads(remote)


def test_cleanup_ignores_non_task_refs(git_repo):
    """Only clams/<task>-aN refs are candidates: wf refs and anything else
    matching the clams/ prefix are left alone even when merged."""
    wt, remote = git_repo
    sha = _land_workflow(wt, ["t1"])
    clone = wt.repo_dir("o/r")
    _git("push", "origin", f"{sha}:refs/heads/clams/wf/w1", cwd=clone)
    _git("push", "origin", f"{sha}:refs/heads/clams/manual-review", cwd=clone)
    wt.fetch("o/r")

    deleted = wt.cleanup_merged_branches("o/r", sha)

    assert deleted == ["clams/t1-a1"]
    heads = _heads(remote)
    assert "clams/wf/w1" in heads and "clams/manual-review" in heads


def test_cleanup_requires_clone(git_repo):
    wt, _ = git_repo
    with pytest.raises(GitError):
        wt.cleanup_merged_branches("nobody/none", "deadbeef")


# ---------------------------------------------------------------- runner level

def _runner(wt, **overrides):
    s = Settings(_env_file=None, **overrides)
    bus = InMemoryBus()
    bus.ensure_group(s.stream_events, s.group_planner)
    return ExecutorRunner(s, bus, None, wt), bus, s


def _integrate(runner, bus, s):
    msg = contracts.WorkflowIntegrate(workflow_id="w1", repo="o/r",
                                      integration_branch="clams/wf/w1")
    runner._integrate_workflow(msg)
    got = bus.read(s.stream_events, s.group_planner, "p", count=10)
    events = [contracts.parse_message(d.payload) for d in got]
    return [e for e in events if e.type == "workflow.integrated"]


def test_runner_cleans_up_after_successful_merge(git_repo):
    wt, remote = git_repo
    _land_workflow(wt, ["t1", "t2"])
    runner, bus, s = _runner(wt)

    events = _integrate(runner, bus, s)

    assert events and events[0].ok
    heads = _heads(remote)
    assert "clams/t1-a1" not in heads and "clams/t2-a1" not in heads


def test_runner_keeps_branches_when_disabled(git_repo):
    wt, remote = git_repo
    _land_workflow(wt, ["t1"])
    runner, bus, s = _runner(wt, cleanup_merged_branches=False)

    events = _integrate(runner, bus, s)

    assert events and events[0].ok
    assert "clams/t1-a1" in _heads(remote)


def test_runner_cleanup_failure_does_not_change_outcome(git_repo, monkeypatch):
    wt, remote = git_repo
    _land_workflow(wt, ["t1"])
    runner, bus, s = _runner(wt)
    monkeypatch.setattr(wt, "cleanup_merged_branches",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("boom")))

    events = _integrate(runner, bus, s)

    assert events and events[0].ok  # merge succeeded; cleanup is best-effort


def test_runner_no_cleanup_after_failed_merge(git_repo):
    """A conflicted integration strands the work: branches must survive."""
    wt, remote = git_repo
    path1, branch1 = _make_task(wt, "t1", name="one.txt")
    wt.integrate("o/r", "w1", path1, branch1)
    # move main ahead with a conflicting edit to the same file
    clone = wt.repo_dir("o/r")
    conflict = wt.work_root / "conflict"
    _git("worktree", "add", "--detach", str(conflict), "origin/main",
         cwd=clone)
    (conflict / "one.txt").write_text("main wins\n")
    _git("add", ".", cwd=conflict)
    _git("commit", "-m", "conflicting main", cwd=conflict)
    _git("push", "origin", "HEAD:refs/heads/main", cwd=conflict)

    runner, bus, s = _runner(wt)
    events = _integrate(runner, bus, s)

    assert events and not events[0].ok
    assert "clams/t1-a1" in _heads(remote)
