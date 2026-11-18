"""WP3: writable-scope rules + evidence gate checks."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from clams.executor import scope
from clams.executor.evidence import EvidenceGate
from clams.executor.worktree import WorktreeManager


# ------------------------------------------------------------------- scope
def test_glob_prefix():
    assert scope.is_writable("src/app/main.py", ["src/**"], [])
    assert not scope.is_writable("tests/x.py", ["src/**"], [])
    assert scope.is_writable("docs/a.md", ["docs/**"], [])


def test_forbidden_wins():
    assert not scope.is_writable("src/secret.py", ["src/**"], ["src/secret.py"])
    assert not scope.is_writable("src/deep/x.py", ["src/**"], ["src/deep/**"])


def test_bare_filename_glob():
    assert scope.is_writable("README.md", ["*.md"], [])
    assert not scope.is_writable("notes.txt", ["*.md"], [])


def test_check_scope_lists_violations():
    ok, v = scope.check_scope(["a.py", "b.md"], ["*.py"], [])
    assert not ok and v == ["b.md"]


# --------------------------------------------------------------- evidence gate
@pytest.fixture()
def git_repo(tmp_path):
    """A bare remote + a worktree manager rooted at tmp."""
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                   capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(seed)], check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=seed, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=seed, check=True)
    (seed / "hello.txt").write_text("hi\n")
    subprocess.run(["git", "add", "."], cwd=seed, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=seed, check=True,
                   capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=seed,
                   check=True, capture_output=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=seed, check=True,
                   capture_output=True)
    wt = WorktreeManager(tmp_path / "work",
                         remote_url_template=str(remote))
    return wt, remote


def _commit_something(wt_dir: Path, name: str = "new.py"):
    (wt_dir / name).write_text("print('ok')\n")
    subprocess.run(["git", "add", "."], cwd=wt_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "work"], cwd=wt_dir, check=True,
                   capture_output=True)


def test_gate_all_pass(git_repo):
    wt, _ = git_repo
    path, _branch = wt.add("o/r", "t1", 1, "main")
    _commit_something(path)
    brief = {"run": ["echo verified"], "writable": ["*.py"], "forbidden": []}
    ev = EvidenceGate(wt).run(path, brief, "main")
    assert ev["passed"], ev["reason"]
    assert ev["changed_files"] == ["new.py"]


def test_gate_fail_no_commit(git_repo):
    wt, _ = git_repo
    path, _ = wt.add("o/r", "t2", 1, "main")
    ev = EvidenceGate(wt).run(path, {"run": ["echo x"], "writable": ["*.py"]},
                             "main")
    assert not ev["passed"]
    assert any(c["check"] == "commits" and not c["ok"] for c in ev["checks"])


def test_gate_fail_command_nonzero(git_repo):
    wt, _ = git_repo
    path, _ = wt.add("o/r", "t3", 1, "main")
    _commit_something(path)
    ev = EvidenceGate(wt).run(path, {"run": ["exit 3"], "writable": ["*.py"]},
                             "main")
    assert not ev["passed"]


def test_gate_fail_empty_output(git_repo):
    wt, _ = git_repo
    path, _ = wt.add("o/r", "t4", 1, "main")
    _commit_something(path)
    ev = EvidenceGate(wt).run(path, {"run": ["true"], "writable": ["*.py"]},
                             "main")
    assert not ev["passed"]  # exit 0 but empty output


def test_gate_fail_forbidden_edit(git_repo):
    wt, _ = git_repo
    path, _ = wt.add("o/r", "t5", 1, "main")
    (path / "secret.env").write_text("leak\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "bad"], cwd=path, check=True,
                   capture_output=True)
    ev = EvidenceGate(wt).run(
        path, {"run": ["echo ok"], "writable": ["*.py"],
               "forbidden": ["*.env"]}, "main")
    assert not ev["passed"]
    assert any(c["check"] == "scope" and not c["ok"] for c in ev["checks"])


def test_gate_fail_dirty_tree(git_repo):
    wt, _ = git_repo
    path, _ = wt.add("o/r", "t6", 1, "main")
    _commit_something(path)
    (path / "dirty.txt").write_text("uncommitted\n")
    ev = EvidenceGate(wt).run(path, {"run": ["echo ok"], "writable": ["*.py",
                                                                     "*.txt"]},
                             "main")
    assert not ev["passed"]
    assert any(c["check"] == "clean_tree" and not c["ok"] for c in ev["checks"])


def test_gate_missing_run_fails_closed(git_repo):
    wt, _ = git_repo
    path, _ = wt.add("o/r", "t7", 1, "main")
    _commit_something(path)
    ev = EvidenceGate(wt).run(path, {"writable": ["*.py"]}, "main")
    assert not ev["passed"]


# ------------------------------------------------------------------ worktree
def test_worktree_add_remove_push(git_repo):
    wt, remote = git_repo
    path, branch = wt.add("o/r", "t8", 1, "main")
    assert path.exists() and branch == "clams/t8-a1"
    _commit_something(path)
    wt.push(path, branch)
    refs = subprocess.run(["git", "ls-remote", "--heads", str(remote)],
                          capture_output=True, text=True).stdout
    assert branch in refs
    wt.remove("o/r", "t8", 1)
    assert not path.exists()


def test_worktree_token_scrubbed(tmp_path, git_repo):
    wt, remote = git_repo
    # simulate a clone whose remote carries a token
    d = wt.ensure_clone("o/r")
    subprocess.run(["git", "remote", "set-url", "origin",
                    f"https://ghp_secret@{remote}"], cwd=d, check=True,
                   capture_output=True)
    wt._scrub_remote(d)
    url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=d,
                         capture_output=True, text=True).stdout
    assert "ghp_secret" not in url
