"""The API's write target accepts ``kind='recover'`` and the planner applies it.

Scope: the layer the gate can reach. ``clams.api`` (fastapi) is **not installed**
in the gate container, so this test exercises the exact call the API route makes
-- ``repo_commands.insert_command(conn, 'recover', {}, 'operator', target=wf)``
-- and then hands the queue to the planner's ``drain_commands``, which is the
only path from a queued command to domain state (§9.1).

Proven here:
* ``recover`` is a kind the insert guard lets through (no ValueError);
* ``drain_commands`` applies it (returns 1, row status ``applied``);
* the stored result names the workflow and hands it back as ``status='planning'``;
* the workflow row itself really moved ``blocked`` -> ``planning``, which is what
  the planner loop picks up and what the SPA's "Retry workflow" button relies on.

No network: the workflow has no open sub-issues, so the recovery sweeps come
back empty and no ``gh_close`` outbox row is written.
"""
from __future__ import annotations

import json
import sqlite3

from clams.db import repo_commands, repo_projects, repo_workflows
from clams.planner import commands


def _blocked_workflow(conn: sqlite3.Connection, status: str = "blocked") -> str:
    """A workflow in a recoverable state, with no sub-issues attached."""
    pid = repo_projects.upsert_project(conn, "o/recover-api-kind")
    wf = repo_workflows.create_workflow(conn, pid, "adhoc", "ship the recovery path")
    repo_workflows.set_status(conn, wf, status)
    conn.commit()
    return wf


def test_recover_is_a_kind_the_api_can_enqueue(db: sqlite3.Connection) -> None:
    """The insert guard (the API's only write path) knows the kind."""
    assert "recover" in repo_commands.COMMAND_KINDS

    wf = _blocked_workflow(db)
    command_id = repo_commands.insert_command(
        db, "recover", {}, "operator", target=wf)
    conn_row = repo_commands.get(db, command_id)

    assert conn_row is not None
    assert conn_row["kind"] == "recover"
    assert conn_row["target"] == wf
    assert conn_row["status"] == "new"


def test_drain_applies_recover_and_moves_workflow_to_planning(
        db: sqlite3.Connection) -> None:
    """recover -> applied, result carries {workflow, status: planning}."""
    wf = _blocked_workflow(db)
    command_id = repo_commands.insert_command(
        db, "recover", {}, "operator", target=wf)
    db.commit()  # the API's writer connection commits right after the insert

    assert commands.drain_commands(db) == 1

    row = repo_commands.get(db, command_id)
    assert row is not None
    assert row["status"] == "applied", row["result"]

    result = json.loads(row["result"])
    assert result["workflow"] == wf
    assert result["status"] == "planning"

    # the workflow really transitioned planning-ward for the planner loop
    assert repo_workflows.get_workflow(db, wf)["status"] == "planning"


def test_drain_applies_recover_on_a_failed_workflow(
        db: sqlite3.Connection) -> None:
    """The same kind works from ``failed`` (the button's other gate)."""
    wf = _blocked_workflow(db, status="failed")
    command_id = repo_commands.insert_command(
        db, "recover", {}, "operator", target=wf)

    assert commands.drain_commands(db) == 1
    row = repo_commands.get(db, command_id)
    assert row["status"] == "applied"
    assert json.loads(row["result"])["status"] == "planning"
    assert repo_workflows.get_workflow(db, wf)["status"] == "planning"


def test_recover_on_unknown_workflow_is_rejected(db: sqlite3.Connection) -> None:
    """A bad target is a rejected command, not a crash of the drain loop."""
    command_id = repo_commands.insert_command(
        db, "recover", {}, "operator", target="does-not-exist")

    assert commands.drain_commands(db) == 0
    row = repo_commands.get(db, command_id)
    assert row["status"] == "rejected"
    assert "unknown workflow" in json.loads(row["result"])["error"]
