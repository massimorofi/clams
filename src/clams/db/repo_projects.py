"""projects repository."""
from __future__ import annotations

import sqlite3
from typing import Optional


def upsert_project(conn: sqlite3.Connection, repo: str,
                   gh_project_node_id: Optional[str] = None,
                   status_field_id: Optional[str] = None,
                   ready_option_id: Optional[str] = None,
                   enabled: bool = True) -> int:
    with conn.execute(
        "INSERT INTO projects(repo, gh_project_node_id, status_field_id, "
        "ready_option_id, enabled) VALUES(?,?,?,?,?) "
        "ON CONFLICT(repo) DO UPDATE SET "
        "  gh_project_node_id=COALESCE(excluded.gh_project_node_id, projects.gh_project_node_id),"
        "  status_field_id=COALESCE(excluded.status_field_id, projects.status_field_id),"
        "  ready_option_id=COALESCE(excluded.ready_option_id, projects.ready_option_id),"
        "  enabled=excluded.enabled "
        "RETURNING project_id",
        (repo, gh_project_node_id, status_field_id, ready_option_id,
         1 if enabled else 0),
    ) as cur:
        return cur.fetchone()["project_id"]


def get_project(conn: sqlite3.Connection, repo: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM projects WHERE repo=?", (repo,)).fetchone()


def list_projects(conn: sqlite3.Connection,
                  enabled_only: bool = False) -> list[sqlite3.Row]:
    q = "SELECT * FROM projects"
    if enabled_only:
        q += " WHERE enabled=1"
    return conn.execute(q + " ORDER BY project_id").fetchall()


def set_enabled(conn: sqlite3.Connection, repo: str, enabled: bool) -> None:
    conn.execute("UPDATE projects SET enabled=? WHERE repo=?",
                 (1 if enabled else 0, repo))


def delete_project(conn: sqlite3.Connection, repo: str) -> int:
    """Hard-delete a project row. Returns rowcount (0 if absent). Callers
    must ensure no workflows reference it (FK is NO ACTION)."""
    cur = conn.execute("DELETE FROM projects WHERE repo=?", (repo,))
    return cur.rowcount
