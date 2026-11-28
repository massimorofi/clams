"""T1: every planned task gets a stable ``plan_key`` (migrations/009).

Root cause being set up for (fixed in a later step): on every replan
``plan.py::plan_workflow`` / ``commands.py::_apply_plan`` hard-delete all task
rows (``repo_tasks.delete_workflow_tasks``), discarding each task's
``gh_issue_number``; the re-inserted tasks get brand-new ``task_id``s, so
``projection.project_tasks_created`` enqueues ``gh_subissue`` with a never-seen
idempotency key (``gh:gh_subissue:<task_id>:created``) and GitHub mints a
duplicate sub-issue while the old ones stay open.

Step 1 gives each planned task an identity that survives the delete: a
normalized title (``plan_key``) written by ``create_task`` and backfilled onto
pre-existing rows by migration 009.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from clams.db import connect, migrate, schema_version
from clams.db import repo_projects, repo_tasks, repo_workflows
from clams.db.repo_tasks import normalize_plan_key

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = ROOT / "migrations"
MIGRATION_009 = MIGRATIONS_DIR / "009_task_plan_key.sql"
MIGRATION_010 = MIGRATIONS_DIR / "010_outbox_gh_close.sql"


# --------------------------------------------------------------------- helpers
def _columns(conn: sqlite3.Connection, table: str = "tasks") -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _seed_workflow(conn: sqlite3.Connection, goal: str = "dedupe replans") -> str:
    project_id = repo_projects.upsert_project(conn, "massimorofi/clams")
    return repo_workflows.create_workflow(conn, project_id, "adhoc", goal)


def _apply_migrations_through(conn: sqlite3.Connection, max_version: int) -> None:
    """Bring a fresh DB to ``max_version`` without running later migrations, so
    migration 009 can be observed backfilling rows that predate it."""
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(path.name.split("_", 1)[0]) > max_version:
            continue
        conn.executescript(path.read_text(encoding="utf-8"))
    conn.execute(
        "INSERT INTO runtime_state(key, value) VALUES('schema_version', ?)",
        (str(max_version),))


# ------------------------------------------------------- (a) migration present
def test_migration_file_exists():
    assert MIGRATION_009.is_file(), f"missing {MIGRATION_009}"
    sql = MIGRATION_009.read_text(encoding="utf-8")
    assert "ALTER TABLE tasks ADD COLUMN plan_key TEXT" in sql
    assert "UPDATE tasks SET plan_key = lower(trim(title))" in sql
    assert "CREATE INDEX idx_tasks_workflow_plan_key" in sql
    # non-unique on purpose: pre-existing duplicate titles must not break it
    assert "CREATE UNIQUE INDEX" not in sql.upper()


# ------------------------------------------------- (b) migrate() adds column
def test_migrate_adds_plan_key_column(db_path):
    conn = connect(db_path)
    try:
        _apply_migrations_through(conn, 8)
        assert schema_version(conn) == 8
        assert "plan_key" not in _columns(conn)
        # migrate() applies every pending migration, so 010 (outbox
        # gh_close kind) rides along with the 009 this test targets.
        assert migrate(conn) == [MIGRATION_009.name, MIGRATION_010.name]
        assert "plan_key" in _columns(conn)
        assert schema_version(conn) == 10
    finally:
        conn.close()


def test_migrated_db_has_plan_key(db):
    """(b) with the shared fixture: the migrated schema exposes plan_key."""
    assert "plan_key" in _columns(db)


# --------------------------------------------- (d) normalize_plan_key import
def test_normalize_plan_key_is_importable():
    assert callable(repo_tasks.normalize_plan_key)
    assert normalize_plan_key("  Fix   Replan Dedupe ") == "fix replan dedupe"
    assert normalize_plan_key("Fix Replan Dedupe") == "fix replan dedupe"
    assert normalize_plan_key("fix\treplan\n\n  dedupe") == "fix replan dedupe"
    assert normalize_plan_key("") == ""
    # casefold, not lower
    assert normalize_plan_key("STRASSE") == normalize_plan_key("strasse")


# ------------------------------------------------- (c) create_task populates
def test_create_task_populates_plan_key(db):
    wf = _seed_workflow(db)
    task_id = repo_tasks.create_task(
        db, wf, sequence_index=1, title="  Fix   Replan Dedupe ",
        objective="stop duplicate sub-issues", acceptance="plan_key set",
        brief={"why": "dedupe"})
    row = repo_tasks.get_task(db, task_id)
    assert row["plan_key"] == normalize_plan_key("Fix Replan Dedupe")
    assert row["plan_key"] == "fix replan dedupe"


def test_plan_key_is_stable_across_a_replan_swap(db):
    """The point of the column: a re-planned task matches the row it replaces."""
    wf = _seed_workflow(db)
    old_id = repo_tasks.create_task(
        db, wf, 1, "Fix Replan Dedupe", "obj", "acc", {}, gh_issue_number=101)
    old_key = repo_tasks.get_task(db, old_id)["plan_key"]

    repo_tasks.delete_workflow_tasks(db, wf)          # what a replan does
    new_id = repo_tasks.create_task(
        db, wf, 1, "  fix  replan  dedupe ", "obj", "acc", {})
    new = repo_tasks.get_task(db, new_id)

    assert new_id != old_id and new["gh_issue_number"] is None
    assert new["plan_key"] == old_key                 # <- matchable again


# ------------------------------------------------------ index + backfill shape
def test_index_is_non_unique_and_backfill_runs(db_path):
    conn = connect(db_path)
    try:
        _apply_migrations_through(conn, 8)
        wf = _seed_workflow(conn)
        conn.execute(
            "INSERT INTO tasks(task_id, workflow_id, sequence_index, title, "
            "objective, acceptance, brief_json, status) "
            "VALUES('legacy-1', ?, 1, '  Legacy Task  ', 'o', 'a', '{}', "
            "'pending')",
            (wf,))
        conn.execute(
            "INSERT INTO tasks(task_id, workflow_id, sequence_index, title, "
            "objective, acceptance, brief_json, status) "
            "VALUES('legacy-2', ?, 2, 'Legacy   Task', 'o', 'a', '{}', "
            "'pending')",
            (wf,))
        assert migrate(conn) == [MIGRATION_009.name, MIGRATION_010.name]
        # backfill is lower(trim(title)): SQLite cannot collapse internal runs,
        # so only padding is normalized on pre-existing rows. create_task()
        # writes the canonical normalize_plan_key() value from here on.
        assert conn.execute(
            "SELECT plan_key FROM tasks WHERE task_id='legacy-1'"
        ).fetchone()[0] == "legacy task"
        assert conn.execute(
            "SELECT plan_key FROM tasks WHERE task_id='legacy-2'"
        ).fetchone()[0] == "legacy   task"

        # the same plan_key several times in one workflow must stay storable
        repo_tasks.create_task(conn, wf, 3, "Legacy Task", "o", "a", {})
        repo_tasks.create_task(conn, wf, 4, "legacy  TASK", "o", "a", {})
        dupes = conn.execute(
            "SELECT plan_key, COUNT(*) n FROM tasks GROUP BY plan_key "
            "HAVING n > 1").fetchall()
        assert {r["plan_key"]: r["n"] for r in dupes} == {"legacy task": 3}

        idx = {r["name"]: r for r in conn.execute("PRAGMA index_list(tasks)")}
        assert "idx_tasks_workflow_plan_key" in idx
        assert idx["idx_tasks_workflow_plan_key"]["unique"] == 0
        cols = [r["name"] for r in conn.execute(
            "PRAGMA index_info('idx_tasks_workflow_plan_key')")]
        assert cols == ["workflow_id", "plan_key"]
    finally:
        conn.close()


def test_repo_tasks_stays_stdlib_only():
    """Repo layer must not pull in pydantic / httpx / clams.github."""
    src = (ROOT / "src" / "clams" / "db" / "repo_tasks.py").read_text(
        encoding="utf-8")
    for banned in ("pydantic", "httpx", "clams.github", "from ..github"):
        assert banned not in src, f"repo_tasks must not import {banned}"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
