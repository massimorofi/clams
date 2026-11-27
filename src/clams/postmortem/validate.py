"""Strict validation of a post-mortem report.

Rejects (loudly, with every problem listed at once):
* an unknown ``schema_version`` (the report must declare
  ``REPORT_SCHEMA_VERSION`` exactly),
* a finding with missing / empty / non-list ``evidence_refs``,
* an evidence ref that does not resolve to a record in the report,
* a recommendation that is not a proposal (``auto_applied`` true, a status
  other than ``proposal``, or ``requires_human_approval`` false),
* ``recommendations_are_proposals`` not being exactly ``True``,
* a payload over the byte budget.

Validation never repairs. A caller that gets an error back must not write the
report as if it were valid.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from .report import (
    FINDING_TYPES,
    PROPOSAL_STATUS,
    REPORT_SCHEMA_VERSION,
    REPORT_TYPE,
    SEVERITIES,
)

#: hard ceiling on one serialised report (bounded everything)
MAX_REPORT_BYTES = 1_000_000

REQUIRED_TOP_LEVEL = (
    "schema_version", "report_type", "generated_at", "workflow_ids",
    "findings", "recommendations", "recommendations_are_proposals",
    "evidence",
)


class ReportValidationError(ValueError):
    """Raised when a report fails the contract. ``errors`` lists every fault."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("post-mortem report failed validation: "
                         + "; ".join(self.errors[:8]))


def payload_bytes(report: Any) -> int:
    return len(json.dumps(report, default=str, sort_keys=True).encode("utf-8"))


def validate_report(report: Any, *, max_bytes: int = MAX_REPORT_BYTES,
                    require_resolvable_refs: bool = True) -> dict:
    """Return ``report`` if it satisfies the contract, else raise.

    ``require_resolvable_refs`` additionally checks that every cited reference
    exists in the report's own evidence map.
    """
    errors: list[str] = []

    if not isinstance(report, dict):
        raise ReportValidationError([f"report must be an object, got "
                                      f"{type(report).__name__}"])

    missing = [k for k in REQUIRED_TOP_LEVEL if k not in report]
    if missing:
        errors.append(f"missing required field(s): {', '.join(missing)}")

    sv = report.get("schema_version")
    if sv != REPORT_SCHEMA_VERSION:
        errors.append(f"unknown schema_version {sv!r} "
                      f"(expected {REPORT_SCHEMA_VERSION!r})")

    if report.get("report_type") not in (None, REPORT_TYPE):
        errors.append(f"report_type must be {REPORT_TYPE!r}, got "
                      f"{report.get('report_type')!r}")

    if report.get("recommendations_are_proposals") is not True:
        errors.append("recommendations_are_proposals must be exactly true - "
                      "a post-mortem may never auto-apply anything")

    evidence = report.get("evidence")
    if evidence is not None and not isinstance(evidence, dict):
        errors.append("evidence must be an object keyed by reference")
        evidence = {}
    evidence = evidence or {}

    findings = report.get("findings")
    if findings is not None and not isinstance(findings, list):
        errors.append("findings must be a list")
        findings = []
    findings = findings or []
    seen_ids: set[str] = set()
    for i, f in enumerate(findings):
        where = f"findings[{i}]"
        if not isinstance(f, dict):
            errors.append(f"{where} must be an object")
            continue
        where = f"finding {f.get('finding_id') or i}"
        fid = f.get("finding_id")
        if not fid:
            errors.append(f"{where} is missing finding_id")
        elif fid in seen_ids:
            errors.append(f"duplicate finding_id {fid!r}")
        else:
            seen_ids.add(str(fid))
        if f.get("type") not in FINDING_TYPES:
            errors.append(f"{where} has unknown type {f.get('type')!r}")
        if f.get("severity") not in SEVERITIES:
            errors.append(f"{where} has unknown severity {f.get('severity')!r}")
        if not str(f.get("summary") or "").strip():
            errors.append(f"{where} has an empty summary")
        refs = f.get("evidence_refs")
        if not isinstance(refs, list) or not refs:
            errors.append(f"{where} must cite at least one evidence reference")
        elif not all(str(r).strip() for r in refs):
            errors.append(f"{where} has a blank evidence reference")
        elif require_resolvable_refs:
            dangling = [str(r) for r in refs if str(r) not in evidence]
            if dangling:
                errors.append(f"{where} cites unknown evidence reference(s): "
                              f"{', '.join(sorted(dangling)[:5])}")

    recs = report.get("recommendations")
    if recs is not None and not isinstance(recs, list):
        errors.append("recommendations must be a list")
        recs = []
    for i, r in enumerate(recs or []):
        where = f"recommendation {r.get('recommendation_id') or i if isinstance(r, dict) else i}"
        if not isinstance(r, dict):
            errors.append(f"recommendations[{i}] must be an object")
            continue
        if r.get("auto_applied") is not False:
            errors.append(f"{where} must have auto_applied=false")
        if r.get("status") != PROPOSAL_STATUS:
            errors.append(f"{where} must have status={PROPOSAL_STATUS!r}, got "
                          f"{r.get('status')!r}")
        if r.get("requires_human_approval") is not True:
            errors.append(f"{where} must require human approval")
        if r.get("finding_id") and seen_ids and r["finding_id"] not in seen_ids:
            errors.append(f"{where} cites unknown finding "
                          f"{r['finding_id']!r}")

    wids = report.get("workflow_ids")
    if wids is not None and not isinstance(wids, list):
        errors.append("workflow_ids must be a list")

    size = payload_bytes(report)
    if size > max_bytes:
        errors.append(f"report payload is {size} bytes, over the budget of "
                      f"{max_bytes} bytes")

    if errors:
        raise ReportValidationError(errors)
    return report


def is_valid_report(report: Any, *, max_bytes: int = MAX_REPORT_BYTES) -> bool:
    try:
        validate_report(report, max_bytes=max_bytes)
        return True
    except ReportValidationError:
        return False


def validation_errors(report: Any,
                      *, max_bytes: int = MAX_REPORT_BYTES) -> list[str]:
    try:
        validate_report(report, max_bytes=max_bytes)
    except ReportValidationError as exc:
        return exc.errors
    return []


def validate_json(text: str, *, max_bytes: Optional[int] = None) -> dict:
    """Parse + validate a serialised report."""
    if max_bytes is not None and len(text.encode("utf-8")) > max_bytes:
        raise ReportValidationError(
            [f"report payload is {len(text.encode('utf-8'))} bytes, over the "
             f"budget of {max_bytes} bytes"])
    try:
        obj = json.loads(text)
    except ValueError as exc:
        raise ReportValidationError([f"report is not valid JSON: {exc}"]) from exc
    return validate_report(obj, max_bytes=max_bytes or MAX_REPORT_BYTES)
