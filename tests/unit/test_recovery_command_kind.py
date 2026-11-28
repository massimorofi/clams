"""`recover` command kind: schema + repo plumbing only (no behaviour yet).

Imports only from clams.db — clams.api / fastapi are not installed in the gate.
"""
from __future__ import annotations

import sqlite3

import pytest

from clams.db import connect, migrate, repo_commands, schema_version
from clams.db.conn import MIGRATIONS_DIR

NEW_KIND = "recover"
MIGRATION = "016_commands_recover_kind"


def _migration_ledger(conn: sqlite3.Connection) -> list[str]:
    """Migration names the runner has recorded as applied.

    clams.db.conn.migrate() keeps its ledger in runtime_state(schema_version);
    a schema_migrations table is used instead when a deployment has one.
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if row:
        return [r[0] for r in conn.execute("SELECT name FROM schema_migrations")]
    version = schema_version(conn)
    return [f.stem for f in sorted(MIGRATIONS_DIR.glob("*.sql"))
            if int(f.name.split("_", 1)[0]) <= version]


# (a) the kind is known to the repository layer
def test_recover_is_a_known_command_kind() -> None:
    assert NEW_KIND in repo_commands.COMMAND_KINDS
    # last entry, matching the order of the SQL CHECK list in migration 016
    assert repo_commands.COMMAND_KINDS[-1] == NEW_KIND


# (b) insert_command accepts it and the row reads back with kind='recover'
def test_insert_command_accepts_recover(db: sqlite3.Connection) -> None:
    command_id = repo_commands.insert_command(
        db, NEW_KIND, {}, "operator", target="wf-1")

    assert command_id
    row = repo_commands.get(db, command_id)
    assert row is not None
    assert row["kind"] == NEW_KIND
    assert row["target"] == "wf-1"
    assert row["issued_by"] == "operator"
    assert row["status"] == "new"
    assert row["payload"] == "{}"


# (c) an unknown kind is still rejected by the repository guard
def test_insert_command_rejects_unknown_kind(db: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        repo_commands.insert_command(db, "not_a_kind", {}, "operator")


# (d) the table rebuild kept the kind CHECK constraint on the live schema
def test_kind_check_constraint_still_enforced(db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO commands(kind, payload, issued_by) "
            "VALUES('bogus_kind', '{}', 'operator')")

    ddl = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='commands'"
    ).fetchone()["sql"]
    assert f"'{NEW_KIND}'" in ddl


# (e) migration 016 is recorded as applied by migrate()
def test_migration_016_recorded_as_applied(db: sqlite3.Connection,
                                           tmp_path) -> None:
    assert MIGRATION in _migration_ledger(db)

    fresh = connect(str(tmp_path / "fresh.db"))
    try:
        assert f"{MIGRATION}.sql" in migrate(fresh)
        assert MIGRATION in _migration_ledger(fresh)
    finally:
        fresh.close()
