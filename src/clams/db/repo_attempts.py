"""Append-only attempt history for tasks (#2).

One row per (task_id, attempt_no) result event, written from
``repo_tasks.on_result`` inside the same fenced transaction, so the table is
exactly the set of results the state machine accepted. Idempotent on
``(task_id, attempt_no)``: a replayed result event appends nothing and
overwrites nothing.

Stdlib only (sqlite3 + json). This module must stay importable from the
executor and from plain scripts: pydantic, fastapi, httpx and
clams.contracts must never be imported here.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

#: mirrors repo_tasks.CONFIG_CLASS_ERRORS / INFRA_ERROR_CODES. Duplicated on
#: purpose: importing repo_tasks here would create a cycle, and this module's
#: stdlib-only import surface is a hard constraint.
_CONFIG_CLASS_ERRORS = ("claudio:auth", "claudio:config", "infra:config")
_INFRA_ERROR_CODES = ("claudio:unavailable",)

#: caps that keep one attempt row (and one summary line) small
MAX_CHECKS = 4
MAX_CHECK_TAIL = 160
MAX_ERROR_EXCERPT = 1000
DEFAULT_SUMMARY_MAX_CHARS = 1200


def classify_failure(outcome: str, gate_passed: bool,
                     error_code: str = "") -> str:
    """Coarse 'why did this attempt fail' class for the history row.

    '' for a gated success; otherwise config | infra | needs_input | gate
    (evidence gate rejected the work) | exec (run errored before the gate).
    """
    ec = error_code or ""
    if outcome == "success" and gate_passed:
        return ""
    if ec.startswith(_CONFIG_CLASS_ERRORS):
        return "config"
    if ec.startswith(_INFRA_ERROR_CODES):
        return "infra"
    if outcome == "needs_input":
        return "needs_input"
    if not gate_passed:
        return "gate"
    return "exec"


def failed_checks(result: Optional[dict]) -> list[dict]:
    """Compact [{cmd, exit, tail}] for the evidence checks that did not pass."""
    out: list[dict] = []
    try:
        checks = ((result or {}).get("evidence") or {}).get("checks") or []
    except AttributeError:
        return out
    if not isinstance(checks, list):
        return out
    for c in checks:
        if not isinstance(c, dict) or c.get("ok"):
            continue
        cmd = str(c.get("cmd") or c.get("check") or "")[:100]
        tail_lines = [ln for ln in str(c.get("output_tail") or "")
                      .splitlines() if ln.strip()]
        out.append({"cmd": cmd,
                    "exit": c.get("exit", c.get("detail", "?")),
                    "tail": tail_lines[-1][:MAX_CHECK_TAIL] if tail_lines else ""})
        if len(out) >= MAX_CHECKS:
            break
    return out


def record_attempt(conn: sqlite3.Connection, *, task_id: str, workflow_id: str,
                   attempt_no: int, lease_token: Optional[str] = None,
                   status: str, failure_class: str = "",
                   checks: Optional[list[dict]] = None,
                   result: Optional[dict[str, Any]] = None,
                   error_excerpt: str = "", branch_name: str = "",
                   commit_sha: str = "") -> bool:
    """Append one attempt row. True if inserted, False if it already existed.

    ON CONFLICT DO NOTHING (never DO UPDATE): a replayed result event must not
    duplicate the row nor overwrite the evidence recorded first.
    """
    cur = conn.execute(
        "INSERT INTO task_attempts(task_id, workflow_id, attempt_no, "
        "lease_token, status, failure_class, checks_json, result_json, "
        "error_excerpt, branch_name, commit_sha) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(task_id, attempt_no) DO NOTHING",
        (task_id, workflow_id, attempt_no, lease_token, status,
         failure_class,
         json.dumps(checks if checks is not None else []),
         json.dumps(result if result is not None else {}),
         (error_excerpt or "")[:MAX_ERROR_EXCERPT],
         branch_name or "", commit_sha or ""))
    return cur.rowcount > 0


def get_attempts(conn: sqlite3.Connection,
                 task_id: str) -> list[sqlite3.Row]:
    """Every recorded attempt for a task, oldest first."""
    return conn.execute(
        "SELECT * FROM task_attempts WHERE task_id=? ORDER BY attempt_no",
        (task_id,)).fetchall()


def _format_attempt(r: sqlite3.Row) -> str:
    head = f"attempt {r['attempt_no']} [{r['status']}"
    fc = (r["failure_class"] or "").strip()
    if fc:
        head += f"/{fc}"
    refs: list[str] = []
    if r["branch_name"]:
        refs.append(f"branch {r['branch_name']}")
    if r["commit_sha"]:
        refs.append(f"commit {str(r['commit_sha'])[:12]}")
    if refs:
        head += f" ({'; '.join(refs)})"
    detail = ""
    try:
        checks = json.loads(r["checks_json"] or "[]")
    except ValueError:
        checks = []
    parts = []
    for c in checks:
        if not isinstance(c, dict):
            continue
        seg = f"`{c.get('cmd', '')}` exit {c.get('exit', '?')}"
        if c.get("tail"):
            seg += f" | {c['tail']}"
        parts.append(seg)
    if parts:
        detail = "; ".join(parts)
    if not detail:
        detail = (r["error_excerpt"] or "").strip().replace("\n", " ")[:200]
    return head + (": " + detail if detail else "")


def cumulative_retry_summary(conn: sqlite3.Connection, task_id: str,
                             max_chars: int = DEFAULT_SUMMARY_MAX_CHARS) -> str:
    """One bounded block merging EVERY prior attempt, oldest first.

    Each line carries the attempt's failure class, its failed checks and its
    branch/commit refs. The block is bounded by ``max_chars``: per-attempt
    detail shrinks to fit so every attempt still appears (the fixed prefix
    'attempt N' is what the retry brief needs most). Returns '' when the task
    has no recorded attempts (e.g. tasks that predate this table).
    """
    rows = get_attempts(conn, task_id)
    if not rows:
        return ""
    max_chars = max(64, int(max_chars))
    per = max(24, (max_chars - (len(rows) - 1)) // len(rows))
    lines: list[str] = []
    for r in rows:
        line = _format_attempt(r)
        if len(line) > per:
            line = line[:max(1, per - 1)] + "…"
        lines.append(line)
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[:max_chars]
