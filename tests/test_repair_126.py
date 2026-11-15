"""Issue #126: the repair tooling must correctly identify stuck requirements.

The repair itself is a one-shot data operation; what is worth locking down is
its *detection* logic, so a future change to the outbox/claim schema cannot make
it silently report "nothing stuck" while requirements hang in dispatched.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from clams.config import Settings
from clams.db import WorkItem, open_store

SPEC = importlib.util.spec_from_file_location(
    "repair_126", Path(__file__).resolve().parents[1] / "scripts/repair_126.py")
repair_126 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repair_126)


@pytest.fixture()
def store(tmp_path):
    s = Settings(paths={"data_dir": str(tmp_path),
                        "db_path": str(tmp_path / "r.db")})
    st = open_store(s.paths.db_path)
    yield st
    st.db.close()


def _report(st, project_id, task, status="completed"):
    st.outbox.enqueue("gh_comment", {
        "issue": task, "body": "x", "project_id": project_id,
        "result": {"task_issue": task, "status": status, "project_id": project_id},
    }, idempotency_key=f"r-{project_id}-{task}")


def test_detects_task_with_no_result(store):
    store.work_items.upsert(WorkItem(
        requirement_number=36, requirement_title="t", gh_status="In Progress",
        gh_updated_at="x", task_numbers=[43, 44], status="dispatched",
        project_id=2))
    _report(store, 2, 43)
    assert repair_126.missing_tasks(store) == [(2, 36, [44])]


def test_nothing_stuck_when_every_task_reported(store):
    store.work_items.upsert(WorkItem(
        requirement_number=36, requirement_title="t", gh_status="In Progress",
        gh_updated_at="x", task_numbers=[43, 44], status="dispatched",
        project_id=2))
    _report(store, 2, 43)
    _report(store, 2, 44)
    assert repair_126.missing_tasks(store) == []


def test_live_claim_is_not_reported_stuck(store):
    store.work_items.upsert(WorkItem(
        requirement_number=36, requirement_title="t", gh_status="In Progress",
        gh_updated_at="x", task_numbers=[45], status="dispatched", project_id=2))
    store.task_claims.claim(45, 36, "executor-1", lease_seconds=900, project_id=2)
    assert repair_126.missing_tasks(store) == []


def test_blocked_requirement_is_ignored(store):
    store.work_items.upsert(WorkItem(
        requirement_number=16, requirement_title="t", gh_status="In Progress",
        gh_updated_at="x", task_numbers=[88], status="blocked", project_id=1))
    assert repair_126.missing_tasks(store) == []


def test_results_from_another_project_do_not_count(store):
    store.work_items.upsert(WorkItem(
        requirement_number=36, requirement_title="t", gh_status="In Progress",
        gh_updated_at="x", task_numbers=[43], status="dispatched", project_id=2))
    _report(store, 1, 43)          # same task number, different project
    assert repair_126.missing_tasks(store) == [(2, 36, [43])]


def test_parse_worktree_name():
    assert repair_126._parse_worktree_name("p2-task-47") == (2, 47)
    assert repair_126._parse_worktree_name("projects") == (None, None)


def test_backup_db_creates_timestamped_copy(tmp_path):
    src = tmp_path / "clams.db"
    src.write_bytes(b"sqlite data")
    s = Settings(paths={"data_dir": str(tmp_path), "db_path": str(src)})
    dst = repair_126.backup_db(s)
    assert dst.exists() and dst.read_bytes() == b"sqlite data"
