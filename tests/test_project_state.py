"""Tests for issue #18: per-project status & progress derivation."""
from __future__ import annotations

import pytest

from clams.db import WorkItem, open_store
from clams.project_state import (
    compute_project_status,
    refresh_all_project_statuses,
    refresh_project_status,
)


def wi(status, num=1, project_id=0):
    return WorkItem(requirement_number=num, requirement_title=f"R{num}",
                   gh_status=status, gh_updated_at="2026-01-01T00:00:00Z",
                   status=status, project_id=project_id)


# ---- pure derivation --------------------------------------------------

def test_no_items_is_queued():
    status, progress = compute_project_status([])
    assert status == "Queued"
    assert progress == 0


def test_all_done_is_done_100():
    items = [wi("done", 1), wi("done", 2)]
    status, progress = compute_project_status(items)
    assert status == "Done"
    assert progress == 100


def test_any_review_is_in_review():
    items = [wi("done", 1), wi("review", 2), wi("dispatched", 3)]
    status, progress = compute_project_status(items)
    assert status == "In review"
    # 1 of 3 done
    assert progress == 33


def test_active_items_are_running():
    items = [wi("claimed", 1), wi("planned", 2), wi("dispatched", 3)]
    status, progress = compute_project_status(items)
    assert status == "Running"
    assert progress == 0


def test_blocked_counts_as_running_not_done():
    items = [wi("blocked", 1), wi("dispatched", 2)]
    status, progress = compute_project_status(items)
    assert status == "Running"
    assert progress == 0


def test_paused_overrides_everything():
    items = [wi("done", 1), wi("done", 2)]
    status, progress = compute_project_status(items, paused=True)
    assert status == "Paused"
    assert progress == 0


def test_progress_rounds():
    items = [wi("done", 1), wi("dispatched", 2), wi("dispatched", 3)]
    status, progress = compute_project_status(items)
    # 1/3 = 33.33 -> 33
    assert progress == 33


# ---- store-backed refresh --------------------------------------------

@pytest.fixture()
def store(tmp_path):
    s = open_store(tmp_path / "ps.db")
    yield s
    s.db.close()


def test_refresh_writes_status_and_progress(store):
    proj = store.projects.add(name="P", url="https://github.com/a/p")
    pid = proj["id"]
    store.work_items.upsert(wi("done", 1, project_id=pid))
    store.work_items.upsert(wi("review", 2, project_id=pid))
    status, progress = refresh_project_status(store, pid)
    assert status == "In review"
    row = store.projects.get(pid)
    assert row["status"] == "In review"
    assert row["progress"] == 50


def test_refresh_respects_project_paused_flag(store):
    proj = store.projects.add(name="P", url="https://github.com/a/p")
    pid = proj["id"]
    store.work_items.upsert(wi("dispatched", 1, project_id=pid))
    store.projects.set_paused(pid, True)
    status, progress = refresh_project_status(store, pid)
    assert status == "Paused"
    assert store.projects.get(pid)["status"] == "Paused"


def test_refresh_all_projects(store):
    p1 = store.projects.add(name="A", url="https://github.com/a/one")
    p2 = store.projects.add(name="B", url="https://github.com/a/two")
    store.work_items.upsert(wi("done", 1, project_id=p1["id"]))
    store.work_items.upsert(wi("done", 2, project_id=p1["id"]))
    store.work_items.upsert(wi("claimed", 1, project_id=p2["id"]))
    result = refresh_all_project_statuses(store)
    assert result[p1["id"]] == ("Done", 100)
    assert result[p2["id"]] == ("Running", 0)


def test_refresh_isolated_per_project(store):
    """A done project's items must not leak into another project's status."""
    p1 = store.projects.add(name="A", url="https://github.com/a/one")
    p2 = store.projects.add(name="B", url="https://github.com/a/two")
    store.work_items.upsert(wi("done", 1, project_id=p1["id"]))
    # p2 has no items -> Queued, unaffected by p1's done item
    status, progress = refresh_project_status(store, p2["id"])
    assert status == "Queued"
    assert progress == 0
