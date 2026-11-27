"""Post-mortem job: read-only evidence capture + versioned report contract.

Stdlib only (sqlite3 + json + datetime). Nothing in this package may write to
a domain table: the whole point is that a post-mortem can run against a live
planner database without perturbing it.

Modules
-------
select    deterministic, bounded selection of terminal workflows
snapshot  read-only evidence snapshot (workflows, tasks, task_attempts, events)
report    versioned report contract (findings cite evidence, proposals only)
validate  strict validation (schema version, evidence refs, payload budget)
"""
from __future__ import annotations

from typing import Iterable, Optional, Union
from datetime import datetime

from .report import (  # noqa: F401
    REPORT_SCHEMA_VERSION,
    build_report,
    utc_stamp,
)
from .select import (  # noqa: F401
    DEFAULT_LIMIT,
    TERMINAL_STATUSES,
    count_terminal_workflows,
    select_terminal_workflows,
    workflow_ids,
)
from .snapshot import (  # noqa: F401
    TRUNCATION_MARKER,
    Snapshot,
    SnapshotBudget,
    build_snapshot,
)
from .validate import (  # noqa: F401
    MAX_REPORT_BYTES,
    ReportValidationError,
    is_valid_report,
    payload_bytes,
    validate_report,
)

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_REPORT_BYTES",
    "REPORT_SCHEMA_VERSION",
    "ReportValidationError",
    "Snapshot",
    "SnapshotBudget",
    "TERMINAL_STATUSES",
    "TRUNCATION_MARKER",
    "build_report",
    "build_snapshot",
    "count_terminal_workflows",
    "is_valid_report",
    "payload_bytes",
    "report_filename",
    "run_postmortem",
    "select_terminal_workflows",
    "utc_stamp",
    "validate_report",
    "workflow_ids",
]


def run_postmortem(conn,
                   *,
                   statuses: Optional[Iterable[str]] = None,
                   since: Union[str, datetime, None] = None,
                   limit: Optional[int] = None,
                   budget: Optional[SnapshotBudget] = None,
                   max_report_bytes: int = MAX_REPORT_BYTES,
                   generated_at: Optional[str] = None) -> dict:
    """select -> snapshot -> report -> validate. Raises on invalid output.

    Read-only end to end: the only statements issued are SELECTs.
    """
    from .select import normalise_statuses
    wanted = normalise_statuses(statuses)
    rows = select_terminal_workflows(conn, statuses=statuses, since=since,
                                      limit=limit)
    total_matched = count_terminal_workflows(conn, statuses=statuses,
                                             since=since)
    ids = workflow_ids(rows)
    snap = build_snapshot(conn, ids, budget=budget)
    report = build_report(
        snap,
        generated_at=generated_at,
        selection={
            "statuses": list(wanted),
            "since": str(since) if since is not None else None,
            "limit": limit if limit is not None else DEFAULT_LIMIT,
            "matched": total_matched,
            "selected": len(ids),
            "page_truncated": total_matched > len(ids),
        })
    return validate_report(report, max_bytes=max_report_bytes)


def report_filename(report: dict, *, stamp: Optional[str] = None) -> str:
    """``postmortem_<workflowid>_<utc>.json`` (joined ids when several)."""
    ids = [str(w) for w in (report.get("workflow_ids") or [])]
    who = "_".join(ids[:3]) if ids else "none"
    if len(ids) > 3:
        who += f"_plus{len(ids) - 3}"
    who = "".join(c if c.isalnum() or c in "-_" else "-" for c in who)[:64]
    when = (stamp or utc_stamp()).replace(":", "").replace("-", "").replace("T", "T")
    return f"postmortem_{who}_{when}.json"
