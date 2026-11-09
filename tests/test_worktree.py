"""S6 — git worktree isolation tests (real git repos in temp dirs)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from clams.worktree import (
    WorktreeError,
    cleanup_stale_worktrees,
    create_worktree,
    current_head,
    diff_summary,
    enforce_scope,
    ensure_repo,
    is_clean,
    list_worktrees,
    prune_worktrees,
    remove_worktree,
    run_git,
)


@pytest.fixture()
def repo(tmp_path) -> Path:
    """A real git repo with one commit."""
    p = tmp_path / "repo"
    p.mkdir()
    (p / "hello.py").write_text("print('hello')\n")
    (p / "README.md").write_text("# demo\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=p, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=p, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=p, check=True)
    subprocess.run(["git", "add", "-A"], cwd=p, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=p, check=True)
    return p


def test_ensure_repo_rejects_non_repo(tmp_path):
    with pytest.raises(WorktreeError):
        ensure_repo(tmp_path)


def test_current_head_returns_sha(repo):
    sha = current_head(repo)
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


def test_create_worktree_creates_isolated_dir(repo, tmp_path):
    target = tmp_path / "wt" / "task-1"
    create_worktree(repo, target, branch="task/1", base_sha="HEAD")
    assert target.exists()
    assert (target / "hello.py").exists()
    # the worktree is on its own branch
    branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"], target).stdout.strip()
    assert branch == "task/1"


def test_create_worktree_rejects_existing_path(repo, tmp_path):
    target = tmp_path / "wt2"
    target.mkdir()
    with pytest.raises(WorktreeError):
        create_worktree(repo, target, branch="task/2")


def test_create_worktree_reuses_existing_branch(repo, tmp_path):
    t1 = tmp_path / "a"
    create_worktree(repo, t1, branch="task/3", base_sha="HEAD")
    remove_worktree(repo, t1)
    t2 = tmp_path / "b"
    create_worktree(repo, t2, branch="task/3", base_sha="HEAD")
    assert (t2 / "hello.py").exists()


def test_changes_in_worktree_do_not_leak_to_main_repo(repo, tmp_path):
    target = tmp_path / "wt3"
    create_worktree(repo, target, branch="task/4", base_sha="HEAD")
    (target / "new_file.py").write_text("x = 1\n")
    assert not (repo / "new_file.py").exists()
    assert is_clean(repo)
    assert not is_clean(target)


def test_remove_worktree_removes_directory(repo, tmp_path):
    target = tmp_path / "wt4"
    create_worktree(repo, target, branch="task/5", base_sha="HEAD")
    assert target.exists()
    remove_worktree(repo, target)
    assert not target.exists()


def test_remove_worktree_is_idempotent(repo, tmp_path):
    target = tmp_path / "wt5"
    create_worktree(repo, target, branch="task/6", base_sha="HEAD")
    remove_worktree(repo, target)
    remove_worktree(repo, target)  # no error
    assert not target.exists()


def test_remove_worktree_with_dirty_tree(repo, tmp_path):
    target = tmp_path / "wt6"
    create_worktree(repo, target, branch="task/7", base_sha="HEAD")
    (target / "dirty.py").write_text("messy\n")
    remove_worktree(repo, target, force=True)
    assert not target.exists()


def test_list_worktrees_reports_created(repo, tmp_path):
    target = tmp_path / "wt7"
    create_worktree(repo, target, branch="task/8", base_sha="HEAD")
    wts = list_worktrees(repo)
    assert len(wts) == 2
    paths = {w["path"] for w in wts}
    assert str(target) in paths


def test_prune_recovers_from_deleted_dir(repo, tmp_path):
    target = tmp_path / "wt8"
    create_worktree(repo, target, branch="task/9", base_sha="HEAD")
    before = len(list_worktrees(repo))
    import shutil
    shutil.rmtree(target)
    removed = prune_worktrees(repo)
    assert removed == before - len(list_worktrees(repo))
    assert removed >= 1


def test_diff_summary_detects_new_and_modified_files(repo, tmp_path):
    target = tmp_path / "wt9"
    create_worktree(repo, target, branch="task/10", base_sha="HEAD")
    (target / "hello.py").write_text("print('changed')\n")
    (target / "brand_new.py").write_text("y = 2\n")
    ds = diff_summary(target)
    assert "hello.py" in ds.files_changed
    assert "brand_new.py" in ds.files_changed
    assert ds.insertions >= 1


def test_diff_summary_empty_for_clean_tree(repo):
    ds = diff_summary(repo)
    assert ds.files_changed == []
    assert ds.as_text() == "no changes"


def test_enforce_scope_allows_writable_files(repo, tmp_path):
    target = tmp_path / "wtA"
    create_worktree(repo, target, branch="task/11", base_sha="HEAD")
    (target / "src" ).mkdir(exist_ok=True)
    (target / "src" / "app.py").write_text("ok\n")
    v = enforce_scope(target, writable=["src/"], forbidden=["migrations/"])
    assert v == []


def test_enforce_scope_flags_forbidden_file(repo, tmp_path):
    target = tmp_path / "wtB"
    create_worktree(repo, target, branch="task/12", base_sha="HEAD")
    (target / "migrations").mkdir(exist_ok=True)
    (target / "migrations" / "001.sql").write_text("DROP TABLE x;\n")
    v = enforce_scope(target, writable=["src/"], forbidden=["migrations/"])
    assert any("migrations" in x for x in v)


def test_enforce_scope_flags_out_of_scope_file(repo, tmp_path):
    target = tmp_path / "wtC"
    create_worktree(repo, target, branch="task/13", base_sha="HEAD")
    (target / "elsewhere.py").write_text("nope\n")
    v = enforce_scope(target, writable=["src/"], forbidden=[])
    assert "elsewhere.py" in v


def test_enforce_scope_glob_pattern(repo, tmp_path):
    target = tmp_path / "wtD"
    create_worktree(repo, target, branch="task/14", base_sha="HEAD")
    (target / "tests").mkdir(exist_ok=True)
    (target / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
    v = enforce_scope(target, writable=["tests/*.py", "src/"], forbidden=[])
    assert v == []


def test_cleanup_stale_worktrees_removes_orphans(tmp_path):
    root = tmp_path / "work"
    (root / "task-1").mkdir(parents=True)
    (root / "task-2").mkdir(parents=True)
    (root / "task-1" / "f.py").write_text("x\n")
    removed = cleanup_stale_worktrees(root, active_paths={str(root / "task-2")})
    assert str(root / "task-1") in removed
    assert not (root / "task-1").exists()
    assert (root / "task-2").exists()


def test_cleanup_stale_worktrees_missing_root(tmp_path):
    assert cleanup_stale_worktrees(tmp_path / "nope", set()) == []
