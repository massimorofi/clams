"""Regression: cleanup_stale_worktrees must never delete the base clones.

Discovered while verifying the #114/#117 restart on the live deployment: the
watchdog's auto-recover pruned ``work/projects/`` (the persistent per-project
clones every task worktree is cut from). Every task after that failed with
".../work/projects/claudio-2 is not a git repository".
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from clams.worktree import cleanup_stale_worktrees, create_worktree


@pytest.fixture()
def stack(tmp_path):
    """A repo with a base clone and two task worktrees under work_root."""
    origin = tmp_path / "origin"
    origin.mkdir()
    (origin / "f.py").write_text("x\n")
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@e.com"],
                 ["config", "user.name", "T"], ["add", "-A"], ["commit", "-qm", "i"]):
        subprocess.run(["git", *args], cwd=origin, check=True)

    work = tmp_path / "work"
    clone = work / "projects" / "widgets-1"
    clone.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    create_worktree(clone, work / "p1-task-10", branch="task/10", base_sha="HEAD")
    create_worktree(clone, work / "p1-task-11", branch="task/11", base_sha="HEAD")
    return work, clone


def test_base_clones_survive_cleanup(stack):
    work, clone = stack
    removed = cleanup_stale_worktrees(work, set())
    assert (work / "projects" / "widgets-1").exists(), \
        "the base clone must never be pruned"
    assert (clone / ".git").exists()
    # Only the task worktrees are eligible.
    assert not (work / "p1-task-10").exists()
    assert not (work / "p1-task-11").exists()
    assert len(removed) == 2


def test_active_task_worktree_is_kept(stack):
    work, _clone = stack
    active = {str(work / "p1-task-10")}
    cleanup_stale_worktrees(work, active)
    assert (work / "p1-task-10").exists()
    assert not (work / "p1-task-11").exists()


def test_cleanup_leaves_no_stale_registration(stack):
    """Removal must go through git so the clone keeps a consistent worktree list."""
    work, clone = stack
    cleanup_stale_worktrees(work, set())
    listing = subprocess.run(["git", "worktree", "list"], cwd=clone,
                             capture_output=True, text=True).stdout
    assert "p1-task-10" not in listing
    prune = subprocess.run(["git", "worktree", "prune", "--dry-run"], cwd=clone,
                           capture_output=True, text=True).stdout
    assert "p1-task-1" not in prune


def test_unrelated_directories_are_untouched(tmp_path):
    work = tmp_path / "work"
    (work / "something-else").mkdir(parents=True)
    (work / "logs").mkdir()
    assert cleanup_stale_worktrees(work, set()) == []
    assert (work / "something-else").exists()
    assert (work / "logs").exists()
