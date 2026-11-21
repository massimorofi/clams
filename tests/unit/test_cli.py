"""CLI surface: migrate / status / command / healthcheck."""
from __future__ import annotations

import json

import pytest

from clams.cli import main


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAMS_DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("CLAMS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CLAMS_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("CLAMS_CONFIG_FILE", str(tmp_path / "none.yaml"))
    monkeypatch.delenv("CLAMS_ROLE", raising=False)
    return tmp_path


def test_migrate_then_status(env, capsys):
    assert main(["migrate"]) == 0
    capsys.readouterr()
    assert main(["status"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schema_version"] == 2


def test_command_pause(env, capsys):
    """`clams command pause` queues a command; the planner drains it."""
    main(["migrate"])
    assert main(["command", "pause", "--by", "operator"]) == 0
    from clams.config import Settings
    from clams.db import connect, repo_commands, repo_runtime
    from clams.planner.commands import drain_commands
    conn = connect(Settings(_env_file=None).db_path)
    assert len(repo_commands.fetch_new(conn)) == 1
    drain_commands(conn)
    assert repo_runtime.is_paused(conn)
    conn.close()


def test_healthcheck_planner_fails_without_heartbeat(env):
    main(["migrate"])
    assert main(["healthcheck", "planner"]) == 1   # no heartbeat yet


def test_healthcheck_planner_ok_after_heartbeat(env):
    main(["migrate"])
    from clams.config import Settings
    from clams.db import connect, repo_runtime
    conn = connect(Settings(_env_file=None).db_path)
    repo_runtime.heartbeat(conn)
    conn.close()
    assert main(["healthcheck", "planner"]) == 0
