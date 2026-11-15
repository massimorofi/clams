"""Tests for issue #18: project provisioning (clone working tree)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from clams.provision import (
    ProvisionError,
    authed_clone_url,
    clone_repo,
    detect_default_branch,
    is_git_repo,
    project_worktree_path,
    provision_project,
    remove_worktree,
    slugify,
    sync_repo,
)


# ---- slugify ----------------------------------------------------------

def test_slugify_basic():
    assert slugify("Website refresh!") == "website-refresh"


def test_slugify_collapses_and_strips():
    assert slugify("  My   Cool--Repo  ") == "my-cool-repo"


def test_slugify_empty_falls_back():
    assert slugify("!!!") == "project"
    assert slugify("") == "project"
    assert slugify("   ", fallback="fallback") == "fallback"


def test_slugify_keeps_digits():
    assert slugify("Project 42") == "project-42"


# ---- worktree path ----------------------------------------------------

def test_worktree_path_layout(tmp_path):
    p = project_worktree_path(tmp_path, 7, "My Repo")
    assert p == tmp_path / "projects" / "my-repo-7"


def test_worktree_path_unique_per_id_even_same_slug(tmp_path):
    a = project_worktree_path(tmp_path, 1, "Same Name")
    b = project_worktree_path(tmp_path, 2, "Same Name")
    assert a != b
    assert a.name == "same-name-1"
    assert b.name == "same-name-2"


# ---- authed url -------------------------------------------------------

def test_authed_url_embeds_token():
    out = authed_clone_url("https://github.com/acme/widgets.git", "TOK123")
    assert out == "https://x-access-token:TOK123@github.com/acme/widgets.git"


def test_authed_url_no_token_unchanged():
    assert authed_clone_url("https://github.com/a/b.git", None) == "https://github.com/a/b.git"
    assert authed_clone_url("https://github.com/a/b.git", "") == "https://github.com/a/b.git"


def test_authed_url_non_https_unchanged():
    assert authed_clone_url("git@github.com:a/b.git", "TOK") == "git@github.com:a/b.git"


# ---- helpers to build a real local git repo ---------------------------

def make_origin(tmp_path: Path) -> Path:
    """Create a real git repo with one commit on a 'main' branch."""
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(origin)], check=True,
                   capture_output=True)
    (origin / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=origin, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-m", "init"], cwd=origin, check=True, capture_output=True)
    return origin


# ---- is_git_repo ------------------------------------------------------

def test_is_git_repo(tmp_path):
    origin = make_origin(tmp_path)
    assert is_git_repo(origin) is True
    assert is_git_repo(tmp_path / "nope") is False


# ---- clone_repo -------------------------------------------------------

def test_clone_creates_tree(tmp_path):
    origin = make_origin(tmp_path)
    dest = tmp_path / "clone"
    res = clone_repo(str(origin), dest)
    assert res.cloned is True
    assert res.default_branch == "main"
    assert (dest / "README.md").exists()
    assert is_git_repo(dest)


def test_clone_resets_token_from_remote(tmp_path):
    # Even when a token is supplied, the persisted origin URL must be clean.
    origin = make_origin(tmp_path)
    dest = tmp_path / "clone"
    clone_repo(str(origin), dest, token="SECRET")
    remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=dest,
                           capture_output=True, text=True).stdout.strip()
    assert "SECRET" not in remote
    assert remote == str(origin)


def test_clone_existing_tree_refreshes_not_reclones(tmp_path):
    origin = make_origin(tmp_path)
    dest = tmp_path / "clone"
    first = clone_repo(str(origin), dest)
    assert first.cloned is True
    # second call must not fail and must report a refresh, not a fresh clone
    second = clone_repo(str(origin), dest)
    assert second.cloned is False
    assert is_git_repo(dest)


def test_clone_refuses_non_empty_non_git_dest(tmp_path):
    dest = tmp_path / "occupied"
    dest.mkdir()
    (dest / "file.txt").write_text("junk")
    with pytest.raises(ProvisionError, match="not a git repository"):
        clone_repo(str(tmp_path / "origin"), dest)


# ---- detect_default_branch -------------------------------------------

def test_detect_default_branch(tmp_path):
    origin = make_origin(tmp_path)
    dest = tmp_path / "clone"
    clone_repo(str(origin), dest)
    assert detect_default_branch(dest) == "main"


# ---- sync_repo --------------------------------------------------------

def test_sync_brings_new_commits(tmp_path):
    origin = make_origin(tmp_path)
    dest = tmp_path / "clone"
    clone_repo(str(origin), dest)
    # add a commit upstream
    (origin / "NEW.md").write_text("new\n")
    subprocess.run(["git", "add", "NEW.md"], cwd=origin, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-m", "second"], cwd=origin, check=True, capture_output=True)
    sha = sync_repo(dest)
    assert len(sha) == 40
    assert (dest / "NEW.md").exists()


def test_sync_requires_git_repo(tmp_path):
    with pytest.raises(ProvisionError):
        sync_repo(tmp_path / "missing")


# ---- provision_project ----------------------------------------------

def test_provision_project_clones_to_canonical_path(tmp_path):
    origin = make_origin(tmp_path)
    res = provision_project("My App", str(origin), tmp_path / "work",
                           project_id=3)
    assert res.worktree_path == str(tmp_path / "work" / "projects" / "my-app-3")
    assert res.cloned is True
    assert (Path(res.worktree_path) / "README.md").exists()


def test_provision_project_idempotent(tmp_path):
    origin = make_origin(tmp_path)
    a = provision_project("App", str(origin), tmp_path / "work", project_id=1)
    b = provision_project("App", str(origin), tmp_path / "work", project_id=1)
    assert a.worktree_path == b.worktree_path
    assert b.cloned is False  # reused, not re-cloned


# ---- remove_worktree --------------------------------------------------

def test_remove_worktree(tmp_path):
    origin = make_origin(tmp_path)
    dest = tmp_path / "clone"
    clone_repo(str(origin), dest)
    remove_worktree(dest)
    assert not dest.exists()
