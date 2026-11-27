"""SQL access for the skill lifecycle tables (migration 015).

Thin and dumb on purpose: every function is one statement, no policy. All the
"may this happen" logic lives in :mod:`clams.skills.lifecycle` and
:mod:`clams.skills.store`, which are the only writers. Keeping the ledger
append-only is a rule about *how* these functions are called: approvals and
deployments are inserted, never updated away (deployments get exactly two
mutations: ``active`` flips to 0, and ``rolled_back_at`` is stamped).
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from .conn import utcnow_s

# ------------------------------------------------------------------- skills

def upsert_skill(conn: sqlite3.Connection, skill_id: str, status: str,
                 current_version: str = "") -> None:
    conn.execute(
        "INSERT INTO skills(skill_id, current_version, status) VALUES(?,?,?) "
        "ON CONFLICT(skill_id) DO UPDATE SET "
        "current_version=excluded.current_version, status=excluded.status, "
        "updated_at=?",
        (skill_id, current_version, status, utcnow_s()))


def get_skill(conn: sqlite3.Connection, skill_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM skills WHERE skill_id=?",
                       (skill_id,)).fetchone()
    return dict(row) if row else None


# ----------------------------------------------------------- skill_versions

def put_version(conn: sqlite3.Connection, *, skill_id: str, version: str,
                manifest: Any, content_hash: str, status: str, source: str,
                provenance: Any) -> None:
    conn.execute(
        "INSERT INTO skill_versions(skill_id, version, manifest_json, "
        "content_hash, status, source, provenance_json) "
        "VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(skill_id, version) DO UPDATE SET "
        "manifest_json=excluded.manifest_json, "
        "content_hash=excluded.content_hash, status=excluded.status, "
        "source=excluded.source, provenance_json=excluded.provenance_json, "
        "updated_at=?",
        (skill_id, version, json.dumps(manifest, sort_keys=True),
         content_hash, status, source,
         json.dumps(provenance, sort_keys=True), utcnow_s()))


def get_version(conn: sqlite3.Connection, skill_id: str,
                version: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM skill_versions WHERE skill_id=? AND version=?",
        (skill_id, version)).fetchone()
    return dict(row) if row else None


def set_version(conn: sqlite3.Connection, skill_id: str, version: str,
                status: str, content_hash: Optional[str] = None) -> None:
    if content_hash is None:
        conn.execute(
            "UPDATE skill_versions SET status=?, updated_at=? "
            "WHERE skill_id=? AND version=?",
            (status, utcnow_s(), skill_id, version))
    else:
        conn.execute(
            "UPDATE skill_versions SET status=?, content_hash=?, updated_at=? "
            "WHERE skill_id=? AND version=?",
            (status, content_hash, utcnow_s(), skill_id, version))


def list_versions(conn: sqlite3.Connection, skill_id: str,
                  status: Optional[str] = None) -> list[dict]:
    if status:
        rows = conn.execute(
            "SELECT * FROM skill_versions WHERE skill_id=? AND status=? "
            "ORDER BY created_at, rowid", (skill_id, status)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM skill_versions WHERE skill_id=? "
            "ORDER BY created_at, rowid", (skill_id,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------- skill_evaluations

def add_evaluation(conn: sqlite3.Connection, *, evaluation_id: str,
                   skill_id: str, version: str, content_hash: str,
                   eval_set_version: str, evaluator_version: str,
                   prompt_version: str, model_id: str,
                   per_case_json: str, aggregate_json: str,
                   passed: bool) -> None:
    conn.execute(
        "INSERT INTO skill_evaluations(evaluation_id, skill_id, version, "
        "content_hash, eval_set_version, evaluator_version, prompt_version, "
        "model_id, per_case_json, aggregate_json, passed) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (evaluation_id, skill_id, version, content_hash, eval_set_version,
         evaluator_version, prompt_version, model_id, per_case_json,
         aggregate_json, 1 if passed else 0))


def latest_evaluation(conn: sqlite3.Connection, skill_id: str,
                      version: str) -> Optional[dict]:
    """Newest evaluation for this (skill, version). Ties broken by
    evaluation_id so the answer is stable within one second."""
    row = conn.execute(
        "SELECT * FROM skill_evaluations WHERE skill_id=? AND version=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (skill_id, version)).fetchone()
    return dict(row) if row else None


def previous_evaluation_before(conn: sqlite3.Connection, skill_id: str,
                               version: str,
                               evaluation_id: str) -> Optional[dict]:
    """The evaluation immediately before ``evaluation_id`` — the baseline a
    safety regression is measured against."""
    row = conn.execute(
        "SELECT * FROM skill_evaluations WHERE skill_id=? AND version=? "
        "AND evaluation_id<>? ORDER BY created_at DESC, rowid DESC "
        "LIMIT 1", (skill_id, version, evaluation_id)).fetchone()
    return dict(row) if row else None


# ------------------------------------------------------------- skill_approvals

def add_approval(conn: sqlite3.Connection, *, approval_id: str, skill_id: str,
                 version: str, content_hash: str, approved_by: str) -> None:
    conn.execute(
        "INSERT INTO skill_approvals(approval_id, skill_id, version, "
        "content_hash, approved_by) VALUES(?,?,?,?,?)",
        (approval_id, skill_id, version, content_hash, approved_by))


def latest_approval(conn: sqlite3.Connection, skill_id: str,
                    version: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM skill_approvals WHERE skill_id=? AND version=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (skill_id, version)).fetchone()
    return dict(row) if row else None


def approvals_for_hash(conn: sqlite3.Connection, skill_id: str, version: str,
                       content_hash: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM skill_approvals WHERE skill_id=? AND version=? AND "
        "content_hash=? ORDER BY created_at, rowid",
        (skill_id, version, content_hash)).fetchall()
    return [dict(r) for r in rows]


def list_approvals(conn: sqlite3.Connection, skill_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM skill_approvals WHERE skill_id=? "
        "ORDER BY created_at, rowid", (skill_id,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------- skill_deployments

def add_deployment(conn: sqlite3.Connection, *, deployment_id: str,
                   skill_id: str, version: str, content_hash: str,
                   scope: Any, created_by: str) -> None:
    conn.execute(
        "INSERT INTO skill_deployments(deployment_id, skill_id, version, "
        "content_hash, scope, active, created_by) VALUES(?,?,?,?,?,1,?)",
        (deployment_id, skill_id, version, content_hash,
         json.dumps(scope, sort_keys=True), created_by))


def deactivate_deployment(conn: sqlite3.Connection, skill_id: str,
                          *, rolled_back_at: Optional[str] = None) -> int:
    """Close the currently active deployment row. ``rolled_back_at`` is stamped
    only on a rollback; a plain supersede just flips ``active`` to 0."""
    if rolled_back_at is None:
        cur = conn.execute(
            "UPDATE skill_deployments SET active=0 WHERE skill_id=? AND "
            "active=1", (skill_id,))
    else:
        cur = conn.execute(
            "UPDATE skill_deployments SET active=0, rolled_back_at=? WHERE "
            "skill_id=? AND active=1", (rolled_back_at, skill_id))
    return cur.rowcount


def active_deployment(conn: sqlite3.Connection,
                      skill_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM skill_deployments WHERE skill_id=? AND active=1 "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (skill_id,)).fetchone()
    return dict(row) if row else None


def list_deployments(conn: sqlite3.Connection,
                     skill_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM skill_deployments WHERE skill_id=? "
        "ORDER BY created_at, rowid", (skill_id,)).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------- dispatch/result attribution

def record_task_skills(conn: sqlite3.Connection, *, task_id: str,
                       workflow_id: str, phase: str,
                       active: dict[str, dict]) -> int:
    """Stamp which skill versions were in force for this task.

    ``phase`` is 'dispatch' at publish time and 'result' when the outcome lands,
    so a post-mortem compares what was live at the start against what was live
    at the end instead of attributing an outcome to whatever is current when the
    report happens to run.
    """
    n = 0
    for skill_id, info in sorted(active.items()):
        conn.execute(
            "INSERT INTO task_skill_versions(task_id, workflow_id, skill_id, "
            "version, content_hash, phase) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(task_id, skill_id, phase) DO UPDATE SET "
            "version=excluded.version, content_hash=excluded.content_hash",
            (task_id, workflow_id, skill_id, info["version"],
             info.get("content_hash", ""), phase))
        n += 1
    return n


def task_skill_versions(conn: sqlite3.Connection, task_id: str,
                        phase: Optional[str] = None) -> list[dict]:
    if phase:
        rows = conn.execute(
            "SELECT * FROM task_skill_versions WHERE task_id=? AND phase=? "
            "ORDER BY skill_id", (task_id, phase)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM task_skill_versions WHERE task_id=? "
            "ORDER BY phase, skill_id", (task_id,)).fetchall()
    return [dict(r) for r in rows]


def active_skill_versions(conn: sqlite3.Connection) -> dict[str, dict]:
    """skill_id -> {version, content_hash, scope} for every live deployment.

    Read from skill_deployments (not skills.current_version) because the
    deployment row is the one that froze the scope and hash at deploy time.
    """
    rows = conn.execute(
        "SELECT d.skill_id, d.version, d.content_hash, d.scope "
        "FROM skill_deployments d "
        "JOIN skills s ON s.skill_id = d.skill_id "
        "WHERE d.active=1 AND s.status='active' "
        "ORDER BY d.skill_id").fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        try:
            scope = json.loads(r["scope"] or "{}")
        except ValueError:
            scope = {}
        out[r["skill_id"]] = {"version": r["version"],
                              "content_hash": r["content_hash"],
                              "scope": scope}
    return out


def active_skill_ids(conn: sqlite3.Connection) -> list[str]:
    return sorted(active_skill_versions(conn))
