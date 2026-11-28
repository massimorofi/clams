"""Per-workflow integration ref: the mechanism that lets a chained task's
*first* attempt seed from its predecessors' merged work.

Regression for claudio#133, where T5 and T6 each burned attempt 1 recreating
their dependencies' files and being failed by the scope gate for them.
"""
from __future__ import annotations

import subprocess

import pytest

from clams.executor.worktree import GitError, WorktreeManager

_ENV = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": "*"}


def _git(*args, cwd):
    import os
    env = {**os.environ, **_ENV}
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True, env=env).stdout


def _git_rc(*args, cwd):
    import os
    env = {**os.environ, **_ENV}
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, env=env).returncode


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


def _make_task(wt, task_id, attempt=1, base="main", name="f.txt", body=None):
    """Add a worktree, commit one file, return (path, branch, head_sha)."""
    path, branch = wt.add("o/r", task_id, attempt, base)
    (path / name).write_text(body if body is not None else f"{task_id}\n")
    _git("add", ".", cwd=path)
    _git("commit", "-m", f"{task_id} work", cwd=path)
    return path, branch, wt.head_commit(path)


def _tip(wt, wf="w1"):
    clone = wt.repo_dir("o/r")
    return wt._ref_sha(clone, f"refs/heads/{wt.integration_branch(wf)}")


def _tree(wt, ref):
    return _git("ls-tree", "-r", "--name-only", ref,
                cwd=wt.repo_dir("o/r")).split()


# ------------------------------------------------------------------ happy path

def test_integrate_creates_then_fast_forwards(git_repo):
    wt, _ = git_repo
    _, _, h1 = _make_task(wt, "t1", name="one.txt")
    assert _tip(wt) is None

    assert wt.integrate("o/r", "w1", wt.worktree_path("o/r", "t1", 1),
                        "clams/t1-a1") == h1
    assert _tip(wt) == h1

    # a dependent seeded from the tip: pure fast-forward, no merge commit
    _, _, h2 = _make_task(wt, "t2", base="clams/wf/w1", name="two.txt")
    assert wt.integrate("o/r", "w1", wt.worktree_path("o/r", "t2", 1),
                        "clams/t2-a1") == h2
    assert _tip(wt) == h2
    assert _git("rev-list", "--count", _tip(wt),
                cwd=wt.repo_dir("o/r")).strip() == "3"  # init + t1 + t2
    assert set(_tree(wt, _tip(wt))) >= {"one.txt", "two.txt", "hello.txt"}


def test_integrate_is_idempotent(git_repo):
    wt, _ = git_repo
    _, _, h1 = _make_task(wt, "t1")
    p = wt.worktree_path("o/r", "t1", 1)
    assert wt.integrate("o/r", "w1", p, "clams/t1-a1") == h1
    assert wt.integrate("o/r", "w1", p, "clams/t1-a1") == h1
    assert _tip(wt) == h1


def test_integration_ref_is_local_only(git_repo):
    """Decision: the integration ref is never pushed to origin."""
    wt, remote = git_repo
    _make_task(wt, "t1")
    wt.integrate("o/r", "w1", wt.worktree_path("o/r", "t1", 1), "clams/t1-a1")
    heads = _git("ls-remote", "--heads", str(remote), cwd=wt.repo_dir("o/r"))
    assert "clams/wf/w1" not in heads
    assert "refs/heads/main" in heads


# ------------------------------------------------------------------- non-ff

def test_integrate_non_ff_merges_diamond(git_repo):
    """Two branches off main (the T2/T3 diamond shape): neither contains the
    other, so the tip has to become a real merge commit."""
    wt, _ = git_repo
    _, _, h1 = _make_task(wt, "t1", name="one.txt")
    wt.integrate("o/r", "w1", wt.worktree_path("o/r", "t1", 1), "clams/t1-a1")

    # t2 resolved its seed before t1 integrated, so it starts from main
    p2, _, h2 = _make_task(wt, "t2", base="main", name="two.txt")
    tip = wt.integrate("o/r", "w1", p2, "clams/t2-a1")

    clone = wt.repo_dir("o/r")
    assert _git_rc("merge-base", "--is-ancestor", h1, tip, cwd=clone) == 0
    assert _git_rc("merge-base", "--is-ancestor", h2, tip, cwd=clone) == 0
    assert set(_tree(wt, tip)) >= {"one.txt", "two.txt"}


def test_integrate_conflict_aborts_and_degrades(git_repo):
    """Conflicting work must leave the tip exactly where it was and the
    worktree clean — never a half-merged tree that poisons later seeds."""
    wt, _ = git_repo
    _, _, h1 = _make_task(wt, "t1", name="shared.txt", body="from t1\n")
    wt.integrate("o/r", "w1", wt.worktree_path("o/r", "t1", 1), "clams/t1-a1")

    p2, _, h2 = _make_task(wt, "t2", base="main", name="shared.txt",
                           body="from t2\n")
    with pytest.raises(GitError):
        wt.integrate("o/r", "w1", p2, "clams/t2-a1")

    assert _tip(wt) == h1                       # degraded to the last good tip
    assert wt.status_porcelain(p2).strip() == ""  # merge really was aborted
    assert wt.head_commit(p2) == h2             # task branch untouched


# ------------------------------------------------------------ merge to base

def _remote_main(wt, remote):
    return _git("ls-remote", str(remote), "refs/heads/main",
                cwd=wt.repo_dir("o/r")).split()[0]


def test_merge_to_base_lands_work_on_the_remote(git_repo):
    wt, remote = git_repo
    _make_task(wt, "t1", name="new.py", body="VALUE = 1\n")
    wt.integrate("o/r", "w1", wt.worktree_path("o/r", "t1", 1), "clams/t1-a1")

    sha = wt.merge_to_base("o/r", "clams/wf/w1", "main")

    assert "new.py" in _git("ls-tree", "-r", "--name-only", "main",
                            cwd=remote).split()
    assert _remote_main(wt, remote) == sha


def test_merge_to_base_conflict_leaves_remote_untouched(git_repo, tmp_path):
    """A human moving main over the same file must not lose either side: the
    merge aborts, nothing is pushed, and the caller gets an error to alert on."""
    wt, remote = git_repo
    _make_task(wt, "t1", name="shared.txt", body="from clams\n")
    wt.integrate("o/r", "w1", wt.worktree_path("o/r", "t1", 1), "clams/t1-a1")

    other = tmp_path / "other"
    # -b main: the fixture's bare remote has HEAD on master, so a plain clone
    # would check out nothing at all
    _git("clone", "-b", "main", str(remote), str(other), cwd=tmp_path)
    # shared.txt is new on both sides (the clams commit only ever reached the
    # integration ref), so this is an add/add conflict; `git add`, not `-am`,
    # because the file is untracked in the human's clone
    (other / "shared.txt").write_text("from a human\n")
    _git("add", "shared.txt", cwd=other)
    _git("commit", "-m", "human work", cwd=other)
    _git("push", "origin", "main", cwd=other)
    before = _remote_main(wt, remote)

    with pytest.raises(GitError):
        wt.merge_to_base("o/r", "clams/wf/w1", "main")

    assert _remote_main(wt, remote) == before
    # the temporary worktree is gone even on the failure path
    assert not (wt.work_root / "o-r" / "integrate-base").exists()
