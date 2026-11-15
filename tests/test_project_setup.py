"""Tests for issue #18: project bootstrap (default project + contexts)."""
from __future__ import annotations

from pathlib import Path

import pytest

from clams.config import Settings
from clams.db import open_store
from clams.provision import ProvisionError, ProvisionResult
from clams.project_setup import (
    build_project_contexts,
    ensure_default_project,
    parse_owner_repo,
    repo_url_for,
)


def _fake_provision(name, url, work_root, *, project_id, token=None,
                   timeout=300.0):
    path = Path(work_root) / "projects" / f"{name}-{project_id}"
    path.mkdir(parents=True, exist_ok=True)
    return ProvisionResult(worktree_path=str(path), default_branch="main",
                          cloned=True)


@pytest.fixture()
def store(tmp_path):
    s = open_store(tmp_path / "setup.db")
    yield s
    s.db.close()


@pytest.fixture()
def settings(tmp_path):
    s = Settings()
    s.paths.db_path = str(tmp_path / "setup.db")
    s.paths.data_dir = str(tmp_path)
    s.paths.work_root = str(tmp_path / "work")
    s.github.owner = "acme"
    s.github.repo = "widgets"
    return s


# ---- parse_owner_repo ------------------------------------------------

def test_parse_owner_repo():
    assert parse_owner_repo("https://github.com/acme/widgets") == ("acme", "widgets")


def test_parse_owner_repo_strips_git():
    assert parse_owner_repo("https://github.com/acme/widgets.git") == ("acme", "widgets")


def test_parse_owner_repo_rejects_short():
    with pytest.raises(ValueError):
        parse_owner_repo("https://github.com/acme")


def test_repo_url_for():
    assert repo_url_for("acme", "widgets") == "https://github.com/acme/widgets"


# ---- ensure_default_project -----------------------------------------

def test_ensure_creates_default_when_absent(store, settings, monkeypatch):
    monkeypatch.setattr("clams.project_setup.provision_project", _fake_provision)
    pid = ensure_default_project(store, settings)
    row = store.projects.get(pid)
    assert row["url"] == "https://github.com/acme/widgets"
    assert row["worktree_path"] is not None


def test_ensure_reuses_existing_default(store, settings, monkeypatch):
    monkeypatch.setattr("clams.project_setup.provision_project", _fake_provision)
    existing = store.projects.add(name="Widgets",
                                 url="https://github.com/acme/widgets")
    pid = ensure_default_project(store, settings)
    assert pid == existing["id"]
    # only one project total
    assert store.projects.count() == 1


def test_ensure_provisions_missing_worktree(store, settings, monkeypatch):
    monkeypatch.setattr("clams.project_setup.provision_project", _fake_provision)
    row = store.projects.add(name="Widgets",
                            url="https://github.com/acme/widgets")
    # row has no worktree yet
    assert row["worktree_path"] is None
    pid = ensure_default_project(store, settings)
    after = store.projects.get(pid)
    assert after["worktree_path"] is not None


def test_ensure_records_error_on_clone_failure(store, settings, monkeypatch):
    def boom(*a, **k):
        raise ProvisionError("clone failed: auth")
    monkeypatch.setattr("clams.project_setup.provision_project", boom)
    pid = ensure_default_project(store, settings)
    after = store.projects.get(pid)
    assert after["error"] is not None
    assert "clone failed" in after["error"]


# ---- build_project_contexts -----------------------------------------

def test_build_contexts_skips_default(store, settings, monkeypatch):
    monkeypatch.setattr("clams.project_setup.provision_project", _fake_provision)
    default_pid = ensure_default_project(store, settings)
    other = store.projects.add(name="Other",
                              url="https://github.com/acme/other")
    contexts = build_project_contexts(store, settings, llm_call=lambda p: "x")
    ids = {c.project_id for c in contexts}
    assert default_pid not in ids
    assert other["id"] in ids


def test_build_contexts_wires_each_project(store, settings, monkeypatch):
    monkeypatch.setattr("clams.project_setup.provision_project", _fake_provision)
    ensure_default_project(store, settings)
    other = store.projects.add(name="Other",
                              url="https://github.com/acme/other")
    contexts = build_project_contexts(store, settings, llm_call=lambda p: "x")
    ctx = next(c for c in contexts if c.project_id == other["id"])
    assert ctx.gh.owner == "acme"
    assert ctx.gh.repo == "other"
    assert ctx.worktree_path is not None
    assert ctx.intake is not None
    assert ctx.planner is not None
    assert ctx.review is not None


def test_build_contexts_records_error_on_bad_url(store, settings, monkeypatch):
    monkeypatch.setattr("clams.project_setup.provision_project", _fake_provision)
    ensure_default_project(store, settings)
    bad = store.projects.add(name="Bad", url="https://github.com/onlyowner")
    contexts = build_project_contexts(store, settings, llm_call=lambda p: "x")
    # the bad project is not turned into a context
    assert all(c.project_id != bad["id"] for c in contexts)
    assert store.projects.get(bad["id"])["error"] is not None
