"""Versioned post-mortem report contract.

Contract
--------
* ``schema_version`` is ``REPORT_SCHEMA_VERSION`` and is always present.
* Every finding cites at least one evidence reference that exists in the
  report's own ``evidence`` map (``new_finding`` refuses an empty citation
  list, so a finding without evidence cannot be constructed).
* Recommendations are **proposals only**: ``auto_applied`` is always ``False``,
  ``requires_human_approval`` always ``True``, and the report-level flag
  ``recommendations_are_proposals`` is always ``True``. There is no code path
  in this package that applies a recommendation.
* A truncated snapshot produces an explicit ``evidence_truncated`` finding, so
  a bounded report says so instead of looking complete.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from .snapshot import Snapshot, ref_attempt, ref_task, ref_workflow

#: bump the minor version for additive changes, the major for breaking ones
REPORT_SCHEMA_VERSION = "1.0.0"

REPORT_TYPE = "postmortem"

SEVERITIES = ("info", "warn", "error")

FINDING_TYPES = (
    "workflow_failed",
    "workflow_cancelled",
    "task_failed",
    "repeated_attempts",
    "gate_rejected",
    "infra_failure",
    "excessive_replans",
    "no_attempt_history",
    "evidence_truncated",
)

#: the only status a recommendation may ever have
PROPOSAL_STATUS = "proposal"


class ReportContractError(ValueError):
    """Raised when a caller tries to build something off-contract."""


def utc_stamp(dt: Optional[datetime] = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_finding(*, finding_id: str, ftype: str, severity: str,
                summary: str, evidence_refs: Iterable[str],
                workflow_id: str = "") -> dict:
    """A finding that cites nothing is not a finding: it is a guess."""
    if ftype not in FINDING_TYPES:
        raise ReportContractError(f"unknown finding type {ftype!r}")
    if severity not in SEVERITIES:
        raise ReportContractError(f"unknown severity {severity!r}")
    refs = [str(r) for r in evidence_refs if str(r).strip()]
    if not refs:
        raise ReportContractError(
            f"finding {finding_id!r} must cite at least one evidence reference")
    return {
        "finding_id": finding_id,
        "type": ftype,
        "severity": severity,
        "workflow_id": workflow_id,
        "summary": summary,
        "evidence_refs": sorted(set(refs)),
    }


def new_recommendation(*, recommendation_id: str, title: str, rationale: str,
                       finding_id: str = "",
                       evidence_refs: Optional[Iterable[str]] = None) -> dict:
    """A recommendation is a proposal for a human, never an applied change."""
    return {
        "recommendation_id": recommendation_id,
        "finding_id": finding_id,
        "title": title,
        "rationale": rationale,
        "evidence_refs": sorted({str(r) for r in (evidence_refs or [])}),
        "status": PROPOSAL_STATUS,
        "auto_applied": False,
        "requires_human_approval": True,
    }


def _attempts_for(snapshot: Snapshot, task_id: str) -> list[dict]:
    out = []
    for rec in snapshot.evidence.values():
        if rec.get("kind") == "task_attempt" and rec.get("task_id") == task_id:
            out.append(rec)
    return sorted(out, key=lambda r: r.get("attempt_no", 0))


def _tasks_for(snapshot: Snapshot, workflow_id: str) -> list[dict]:
    out = []
    for rec in snapshot.evidence.values():
        if rec.get("kind") == "task" and rec.get("workflow_id") == workflow_id:
            out.append(rec)
    return sorted(out, key=lambda r: (r.get("sequence_index", 0),
                                      str(r.get("task_id"))))


def derive_findings(snapshot: Snapshot) -> list[dict]:
    """Deterministic findings from the snapshot (no LLM, no guessing)."""
    findings: list[dict] = []
    n = 0

    def fid() -> str:
        nonlocal n
        n += 1
        return f"F{n:03d}"

    wf_recs = [r for r in snapshot.evidence.values() if r.get("kind") == "workflow"]
    wf_recs.sort(key=lambda r: str(r.get("workflow_id")))

    for wf in wf_recs:
        wid = str(wf["workflow_id"])
        wf_ref = ref_workflow(wid)
        tasks = _tasks_for(snapshot, wid)
        failed_tasks = [t for t in tasks if t.get("status") == "failed"]

        if wf.get("status") == "failed":
            refs = [wf_ref] + [ref_task(str(t["task_id"])) for t in failed_tasks]
            findings.append(new_finding(
                finding_id=fid(), ftype="workflow_failed", severity="error",
                workflow_id=wid,
                summary=(f"workflow {wid} ended 'failed' with "
                         f"{len(failed_tasks)} failed task(s)"),
                evidence_refs=refs))
        elif wf.get("status") == "cancelled":
            findings.append(new_finding(
                finding_id=fid(), ftype="workflow_cancelled", severity="warn",
                workflow_id=wid,
                summary=f"workflow {wid} was cancelled before completing",
                evidence_refs=[wf_ref]))

        if int(wf.get("replans") or 0) > 0:
            findings.append(new_finding(
                finding_id=fid(), ftype="excessive_replans", severity="warn",
                workflow_id=wid,
                summary=(f"workflow {wid} was replanned "
                         f"{wf['replans']} time(s)"),
                evidence_refs=[wf_ref]))

        for t in tasks:
            tid = str(t["task_id"])
            t_ref = ref_task(tid)
            atts = _attempts_for(snapshot, tid)
            a_refs = [ref_attempt(tid, a.get("attempt_no")) for a in atts]

            if t.get("status") == "failed":
                findings.append(new_finding(
                    finding_id=fid(), ftype="task_failed", severity="error",
                    workflow_id=wid,
                    summary=(f"task {tid} ('{str(t.get('title'))[:60]}') "
                             f"ended 'failed' after {t.get('attempt')} "
                             f"attempt(s)"),
                    evidence_refs=[t_ref] + a_refs))
            if int(t.get("attempt") or 0) > 1:
                findings.append(new_finding(
                    finding_id=fid(), ftype="repeated_attempts",
                    severity="warn", workflow_id=wid,
                    summary=(f"task {tid} needed {t['attempt']} attempts "
                             f"(max {t.get('max_attempts')})"),
                    evidence_refs=[t_ref] + a_refs))
            gate = [a for a in atts if a.get("failure_class") == "gate"]
            if gate:
                findings.append(new_finding(
                    finding_id=fid(), ftype="gate_rejected", severity="warn",
                    workflow_id=wid,
                    summary=(f"task {tid} had {len(gate)} attempt(s) rejected "
                             f"by the evidence gate"),
                    evidence_refs=[t_ref] +
                    [ref_attempt(tid, a.get("attempt_no")) for a in gate]))
            infra = [a for a in atts
                     if a.get("failure_class") in ("infra", "config")]
            if infra:
                findings.append(new_finding(
                    finding_id=fid(), ftype="infra_failure", severity="warn",
                    workflow_id=wid,
                    summary=(f"task {tid} hit {len(infra)} infra/config "
                             f"failure class(es)"),
                    evidence_refs=[t_ref] +
                    [ref_attempt(tid, a.get("attempt_no")) for a in infra]))
            if not atts and t.get("status") in ("failed", "success"):
                findings.append(new_finding(
                    finding_id=fid(), ftype="no_attempt_history",
                    severity="info", workflow_id=wid,
                    summary=(f"no task_attempts rows recorded for task {tid}"),
                    evidence_refs=[t_ref]))

    if snapshot.truncated:
        refs = [ref_workflow(str(w["workflow_id"])) for w in wf_recs]
        if refs:
            findings.append(new_finding(
                finding_id=fid(), ftype="evidence_truncated", severity="warn",
                summary=("the evidence snapshot hit its budget and was "
                         f"truncated ({len(snapshot.truncations)} bound(s))"),
                evidence_refs=refs))
    return findings


def derive_recommendations(findings: list[dict]) -> list[dict]:
    """One proposal per finding type. Proposals only - nothing is applied."""
    by_type: dict[str, dict] = {}
    for f in findings:
        by_type.setdefault(f["type"], f)

    playbook = {
        "workflow_failed": (
            "Review the failed workflow's goal and acceptance criteria",
            "A failed workflow usually means the plan or the acceptance bar "
            "was wrong; a human should decide which."),
        "workflow_cancelled": (
            "Confirm the cancellation was intentional",
            "Cancelled work is invisible unless someone records why."),
        "task_failed": (
            "Re-run the failed task with a tightened brief",
            "The recorded evidence points at a brief that was too vague to "
            "satisfy the gate."),
        "repeated_attempts": (
            "Consider splitting the task or raising its attempt budget",
            "Repeated attempts burn budget; a human should pick which."),
        "gate_rejected": (
            "Align the task's verification command with the evidence gate",
            "Gate rejections mean the agent's own checks disagreed with the "
            "verifier."),
        "infra_failure": (
            "Check executor/model-server health before retrying",
            "Infra-class failures are not the agent's fault."),
        "excessive_replans": (
            "Tighten the intake prompt so the first plan holds",
            "Replanning is a symptom of an under-specified goal."),
        "no_attempt_history": (
            "Backfill attempt history for tasks predating migration 011",
            "Without attempt rows the root cause cannot be reconstructed."),
        "evidence_truncated": (
            "Re-run with a narrower --since window or a larger budget",
            "A truncated snapshot can hide the decisive event."),
    }

    recs: list[dict] = []
    for i, ftype in enumerate(sorted(by_type), start=1):
        f = by_type[ftype]
        title, rationale = playbook[ftype]
        recs.append(new_recommendation(
            recommendation_id=f"R{i:03d}", title=title, rationale=rationale,
            finding_id=f["finding_id"], evidence_refs=f["evidence_refs"]))
    return recs


def build_report(snapshot: Snapshot, *,
                 generated_at: Optional[str] = None,
                 selection: Optional[dict[str, Any]] = None) -> dict:
    """Assemble the versioned report for one snapshot."""
    findings = derive_findings(snapshot)
    recommendations = derive_recommendations(findings)
    wf_recs = [r for r in snapshot.evidence.values()
               if r.get("kind") == "workflow"]
    wf_recs.sort(key=lambda r: str(r.get("workflow_id")))
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_type": REPORT_TYPE,
        "generated_at": generated_at or utc_stamp(),
        "selection": dict(selection or {}),
        "workflow_ids": list(snapshot.workflow_ids),
        "workflows": [{k: w.get(k) for k in
                       ("workflow_id", "status", "goal", "replans",
                        "created_at", "updated_at")} for w in wf_recs],
        "findings": findings,
        "recommendations": recommendations,
        "recommendations_are_proposals": True,
        "truncated": snapshot.truncated,
        "truncations": list(snapshot.truncations),
        "evidence": snapshot.evidence,
        "evidence_refs": snapshot.refs(),
    }
