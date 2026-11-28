"""Read-only evidence snapshot: workflows, tasks, task_attempts, events.

Everything here is a ``SELECT``. The snapshot is the *only* place raw rows are
turned into evidence records, and every record is addressable by a stable
reference string so a finding can cite exactly what it is based on.

Budgets
-------
A post-mortem runs against a live database, so every collection is bounded:
workflows, tasks per workflow, attempts per task, events per workflow, chars
per text field and total payload chars. When a bound is hit the snapshot does
**not** fail and does **not** silently drop rows: it keeps a deterministic
prefix and appends a truncation record carrying ``TRUNCATION_MARKER``.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

#: written into every truncation record and into the snapshot's marker field
TRUNCATION_MARKER = "[TRUNCATED]"


def ref_workflow(workflow_id: str) -> str:
    return f"workflow:{workflow_id}"


def ref_task(task_id: str) -> str:
    return f"task:{task_id}"


def ref_attempt(task_id: str, attempt_no: Any) -> str:
    return f"attempt:{task_id}:{attempt_no}"


def ref_event(event_id: Any) -> str:
    return f"event:{event_id}"


@dataclass(frozen=True)
class SnapshotBudget:
    """Upper bounds for one snapshot. All defaults are deliberately small."""

    max_workflows: int = 25
    max_tasks_per_workflow: int = 50
    max_attempts_per_task: int = 10
    max_events_per_workflow: int = 100
    max_field_chars: int = 600
    max_total_chars: int = 400_000


@dataclass
class Snapshot:
    """Evidence bundle + honest truncation bookkeeping."""

    workflow_ids: list[str] = field(default_factory=list)
    evidence: dict[str, dict] = field(default_factory=dict)
    truncations: list[dict] = field(default_factory=list)

    @property
    def truncated(self) -> bool:
        return bool(self.truncations)

    def refs(self) -> list[str]:
        return sorted(self.evidence)

    def to_dict(self) -> dict:
        return {
            "workflow_ids": list(self.workflow_ids),
            "evidence": self.evidence,
            "truncated": self.truncated,
            "truncations": list(self.truncations),
        }


def _clip(text: Any, max_chars: int) -> str:
    s = "" if text is None else str(text)
    if len(s) <= max_chars:
        return s
    return s[: max(0, max_chars - len(TRUNCATION_MARKER))] + TRUNCATION_MARKER


def _loads(text: Any, fallback: Any) -> Any:
    if text in (None, ""):
        return fallback
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return fallback


def _size(obj: Any) -> int:
    try:
        return len(json.dumps(obj, default=str))
    except (TypeError, ValueError):
        return len(str(obj))


def _record_truncation(snap: Snapshot, *, scope: str, collection: str,
                       limit: int, total: int, detail: str = "") -> None:
    snap.truncations.append({
        "marker": TRUNCATION_MARKER,
        "scope": scope,
        "collection": collection,
        "limit": int(limit),
        "total": int(total),
        "dropped": max(0, int(total) - int(limit)),
        "detail": detail,
    })


def _select(conn: sqlite3.Connection, sql: str,
            params: Sequence[Any] = ()) -> list[dict]:
    """The one and only SQL entry point: SELECT-only by construction here."""
    if not sql.strip().upper().startswith("SELECT"):
        raise ValueError("snapshot is read-only: only SELECT is allowed")
    return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def _add(snap: Snapshot, ref: str, record: dict) -> None:
    """Index one evidence record under its stable reference."""
    snap.evidence[ref] = record


def build_snapshot(
    conn: sqlite3.Connection,
    workflow_ids: Iterable[str],
    budget: Optional[SnapshotBudget] = None,
) -> Snapshot:
    """Capture evidence for ``workflow_ids``. Never writes; never raises on
    size: an over-budget snapshot is truncated and marked instead.
    """
    budget = budget or SnapshotBudget()
    snap = Snapshot()

    ids = [str(w) for w in workflow_ids]
    if len(ids) > budget.max_workflows:
        _record_truncation(snap, scope="selection", collection="workflows",
                           limit=budget.max_workflows, total=len(ids),
                           detail="workflow page exceeded the snapshot budget")
        ids = ids[: budget.max_workflows]
    snap.workflow_ids = ids
    if not ids:
        return snap

    marks = ",".join("?" * len(ids))

    # --- workflows -------------------------------------------------------
    rows = _select(
        conn,
        "SELECT workflow_id, project_id, source, gh_issue_number, goal, status, "
        "replans, created_at, updated_at FROM workflows "
        f"WHERE workflow_id IN ({marks}) "
        "ORDER BY updated_at DESC, created_at DESC, workflow_id ASC",
        ids)
    for r in rows:
        wf = str(r["workflow_id"])
        _add(snap, ref_workflow(wf), {
            "kind": "workflow",
            "workflow_id": wf,
            "project_id": r["project_id"],
            "source": r["source"],
            "gh_issue_number": r["gh_issue_number"],
            "goal": _clip(r["goal"], budget.max_field_chars),
            "status": r["status"],
            "replans": r["replans"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        })

    # --- tasks -----------------------------------------------------------
    task_rows = _select(
        conn,
        "SELECT task_id, workflow_id, sequence_index, title, status, attempt, "
        "max_attempts, session_ref, commit_sha, branch, error_log, result_json, "
        "created_at, updated_at FROM tasks WHERE workflow_id IN (" + marks + ") "
        "ORDER BY workflow_id ASC, sequence_index ASC, task_id ASC",
        ids)
    per_wf: dict[str, int] = {}
    for r in task_rows:
        wf = str(r["workflow_id"])
        n = per_wf.get(wf, 0)
        if n >= budget.max_tasks_per_workflow:
            continue
        per_wf[wf] = n + 1
        tid = str(r["task_id"])
        _add(snap, ref_task(tid), {
            "kind": "task",
            "task_id": tid,
            "workflow_id": wf,
            "sequence_index": r["sequence_index"],
            "title": _clip(r["title"], budget.max_field_chars),
            "status": r["status"],
            "attempt": r["attempt"],
            "max_attempts": r["max_attempts"],
            "session_ref": r["session_ref"],
            "commit_sha": r["commit_sha"],
            "branch": r["branch"],
            "error_log": _clip(r["error_log"], budget.max_field_chars),
            "result_summary": _clip(
                json.dumps(_loads(r["result_json"], {}), default=str,
                           sort_keys=True),
                budget.max_field_chars),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        })
    for wf in ids:
        total = sum(1 for r in task_rows if str(r["workflow_id"]) == wf)
        if total > budget.max_tasks_per_workflow:
            _record_truncation(snap, scope=wf, collection="tasks",
                               limit=budget.max_tasks_per_workflow, total=total)

    # --- task_attempts ---------------------------------------------------
    att_rows = _select(
        conn,
        "SELECT id, task_id, workflow_id, attempt_no, status, failure_class, "
        "checks_json, error_excerpt, branch_name, commit_sha, created_at "
        "FROM task_attempts WHERE workflow_id IN (" + marks + ") "
        "ORDER BY workflow_id ASC, task_id ASC, attempt_no ASC",
        ids)
    per_task: dict[str, int] = {}
    for r in att_rows:
        tid = str(r["task_id"])
        n = per_task.get(tid, 0)
        if n >= budget.max_attempts_per_task:
            continue
        per_task[tid] = n + 1
        _add(snap, ref_attempt(tid, r["attempt_no"]), {
            "kind": "task_attempt",
            "task_id": tid,
            "workflow_id": str(r["workflow_id"]),
            "attempt_no": r["attempt_no"],
            "status": r["status"],
            "failure_class": r["failure_class"],
            "checks": _loads(r["checks_json"], []),
            "error_excerpt": _clip(r["error_excerpt"], budget.max_field_chars),
            "branch_name": r["branch_name"],
            "commit_sha": r["commit_sha"],
            "created_at": r["created_at"],
        })
    for tid in sorted(per_task):
        total = sum(1 for r in att_rows if str(r["task_id"]) == tid)
        if total > budget.max_attempts_per_task:
            _record_truncation(snap, scope=tid, collection="task_attempts",
                               limit=budget.max_attempts_per_task, total=total)

    # --- events (workflow-scoped, plus task-scoped events of these tasks) ---
    task_wf = {str(r["task_id"]): str(r["workflow_id"]) for r in task_rows}
    ev_rows = _select(
        conn,
        "SELECT event_id, scope, ref, level, message, data, ts FROM events "
        "WHERE (scope='workflow' AND ref IN (" + marks + ")) "
        "OR (scope='task' AND ref IN (SELECT task_id FROM tasks "
        "WHERE workflow_id IN (" + marks + "))) "
        "ORDER BY event_id ASC",
        list(ids) + list(ids))
    ev_by_wf: dict[str, int] = {}
    ev_total: dict[str, int] = {}
    kept_events = 0
    payload = _size(snap.evidence)
    budget_hit = False
    for r in ev_rows:
        wf = (str(r["ref"]) if r["scope"] == "workflow"
              else task_wf.get(str(r["ref"]), ""))
        ev_total[wf] = ev_total.get(wf, 0) + 1
        if ev_by_wf.get(wf, 0) >= budget.max_events_per_workflow:
            continue
        record = {
            "kind": "event",
            "event_id": r["event_id"],
            "scope": r["scope"],
            "ref": r["ref"],
            "workflow_id": wf,
            "level": r["level"],
            "message": _clip(r["message"], budget.max_field_chars),
            "data": _clip(r["data"], budget.max_field_chars),
            "ts": r["ts"],
        }
        if payload + _size(record) > budget.max_total_chars:
            _record_truncation(snap, scope=wf, collection="events",
                               limit=kept_events, total=len(ev_rows),
                               detail="global snapshot char budget reached")
            budget_hit = True
            break
        ev_by_wf[wf] = ev_by_wf.get(wf, 0) + 1
        kept_events += 1
        payload += _size(record)
        _add(snap, ref_event(r["event_id"]), record)
    if not budget_hit:
        for wf in ids:
            if ev_total.get(wf, 0) > budget.max_events_per_workflow:
                _record_truncation(snap, scope=wf, collection="events",
                                   limit=budget.max_events_per_workflow,
                                   total=ev_total[wf])

    # global char budget: truncate (mark), never fail
    if _size(snap.evidence) > budget.max_total_chars:
        _record_truncation(snap, scope="snapshot", collection="evidence",
                           limit=budget.max_total_chars,
                           total=_size(snap.evidence),
                           detail="global snapshot char budget reached")
        snap.evidence = _shrink_to(snap.evidence, budget.max_total_chars)
    return snap


def _shrink_to(evidence: dict[str, dict], max_chars: int) -> dict[str, dict]:
    """Drop the heaviest tail of an over-budget evidence map (deterministic:
    insertion order) until it fits, keeping at least one record."""
    refs = list(evidence)
    out = dict(evidence)
    while len(refs) > 1 and _size(out) > max_chars:
        out.pop(refs.pop())
    return out
