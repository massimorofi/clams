"""Worker progress + handoff contracts (stdlib dataclasses, no pydantic).

Why a separate module: these payloads arrive from *untrusted* executors over
the bus, and they must stay importable from the executor, the planner and
plain scripts. So: stdlib only (dataclasses, enum), no pydantic, no fastapi,
no clams.db import.

Two guarantees every constructor here enforces:

1. Hard length bounds. Every free-text field is clamped to a documented
   maximum and, when clamped, gets an explicit truncation marker so an
   operator reading the timeline knows text is missing rather than silently
   absent.
2. Strict enums. ``kind`` / ``status`` / ``disposition`` are validated
   against closed vocabularies; an unknown value raises ``ValueError``
   instead of being stored.

Handoffs are *requests*, never edits: a ``scope_change`` handoff carries the
operator-facing ask in ``requested_action`` and nothing here touches a task
brief.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

# --------------------------------------------------------------- hard bounds
#: one progress line: long output belongs in the attempt result, not here
MAX_PROGRESS_TEXT = 500
#: why the worker is handing off
MAX_HANDOFF_REASON = 1000
#: what the worker is asking the operator to do
MAX_REQUESTED_ACTION = 500
#: who disposed of a handoff (username / operator id)
MAX_DISPOSED_BY = 100
#: short labels (task ids, kinds echoed back, free-form notes)
MAX_LABEL = 200

#: appended to any text that had to be cut (so truncation is visible)
TRUNCATION_MARKER = "...[truncated]"

#: most progress lines one append call may carry
MAX_PROGRESS_BATCH = 20


class _ClosedEnum(str, Enum):
    """str enum whose members form a closed vocabulary."""

    @classmethod
    def values(cls) -> tuple[str, ...]:
        return tuple(m.value for m in cls)

    @classmethod
    def coerce(cls, value: Any, field_name: str) -> "str":
        """Validate ``value`` against the vocabulary; raise on anything else.

        Empty is never accepted: a missing kind is a producer bug, and
        defaulting it would hide it.
        """
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"{field_name} must be one of {cls.values()}, got {value!r}")
        text = value.strip()
        if text not in cls.values():
            raise ValueError(
                f"{field_name} must be one of {cls.values()}, got {text!r}")
        return text


class ProgressKind(_ClosedEnum):
    """What kind of progress line a worker_progress row is."""

    STATUS = "status"      # phase change the worker reports
    LOG = "log"            # one line of worker output
    TOOL = "tool"          # a tool/command the worker invoked
    NOTE = "note"          # free-form commentary
    WARNING = "warning"    # non-fatal problem
    ERROR = "error"        # fatal problem the worker observed


class HandoffKind(_ClosedEnum):
    """Why a worker is handing off to a human."""

    BLOCKED = "blocked"                  # cannot proceed without input
    NEEDS_DECISION = "needs_decision"    # two valid paths, worker must pick
    SCOPE_CHANGE = "scope_change"        # brief looks wrong; asks for a change
    STALLED = "stalled"                  # no forward progress, worker gave up
    OTHER = "other"


class HandoffStatus(_ClosedEnum):
    """Operator workflow for a handoff.

    Forward-only: open -> acknowledged -> resolved | rejected. A direct
    open -> resolved/rejected is allowed (dismissing without ceremony);
    nothing ever moves backwards, and re-applying the same status is a no-op.
    """

    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    REJECTED = "rejected"


#: forward-only transition table (same shape as repo_tasks.TRANSITIONS)
HANDOFF_TRANSITIONS: dict[str, set[str]] = {
    HandoffStatus.OPEN.value: {HandoffStatus.ACKNOWLEDGED.value,
                               HandoffStatus.RESOLVED.value,
                               HandoffStatus.REJECTED.value},
    HandoffStatus.ACKNOWLEDGED.value: {HandoffStatus.RESOLVED.value,
                                       HandoffStatus.REJECTED.value},
    HandoffStatus.RESOLVED.value: set(),
    HandoffStatus.REJECTED.value: set(),
}

#: dispositions an operator may record alongside a terminal status
DISPOSITIONS: tuple[str, ...] = ("", "accepted", "denied", "deferred",
                                 "retried", "cancelled", "other")


def bounded_text(value: Any, limit: int, *, field_name: str = "text") -> str:
    """Clamp ``value`` to ``limit`` chars, marking the result when cut.

    Non-strings are stringified (never raise on a stray int/None from a
    worker). Control characters are stripped so one progress line stays one
    line. The marker is part of the budget: the stored text is always
    ``<= limit`` chars.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\n", " ").replace("\t", " ")
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    keep = max(0, limit - len(TRUNCATION_MARKER))
    return text[:keep].rstrip() + TRUNCATION_MARKER


def is_truncated(value: str) -> bool:
    return value.endswith(TRUNCATION_MARKER)


def _ident(value: Any, field_name: str) -> str:
    """Identifier-ish field (task/workflow id): bounded, never None."""
    if value is None:
        raise ValueError(f"{field_name} is required")
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return bounded_text(text, MAX_LABEL, field_name=field_name)


def _pos_int(value: Any, field_name: str) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be an integer, got {value!r}")
    if n < 0:
        raise ValueError(f"{field_name} must be >= 0, got {n}")
    return n


# ------------------------------------------------------------------ records
@dataclass(frozen=True)
class WorkerProgress:
    """One fenced progress line from an executor."""

    task_id: str
    attempt_no: int
    seq: int
    kind: str
    text: str
    lease_token: str = ""
    created_at: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "WorkerProgress":
        if not isinstance(payload, dict):
            raise ValueError("progress payload must be an object")
        return cls(
            task_id=_ident(payload.get("task_id"), "task_id"),
            attempt_no=_pos_int(payload.get("attempt_no", 0), "attempt_no"),
            seq=_pos_int(payload.get("seq", 0), "seq"),
            kind=ProgressKind.coerce(payload.get("kind"), "kind"),
            text=bounded_text(payload.get("text", ""), MAX_PROGRESS_TEXT,
                              field_name="text"),
            lease_token=bounded_text(payload.get("lease_token", "") or "",
                                     MAX_LABEL, field_name="lease_token"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "attempt_no": self.attempt_no,
            "seq": self.seq, "kind": self.kind, "text": self.text,
            "lease_token": self.lease_token, "created_at": self.created_at,
        }


@dataclass(frozen=True)
class Handoff:
    """A worker's request for a human decision. Never mutates a brief."""

    workflow_id: str
    task_id: str
    attempt_no: int
    kind: str
    reason: str = ""
    requested_action: str = ""
    status: str = HandoffStatus.OPEN.value
    disposition: str = ""
    disposed_by: str = ""
    created_at: str = ""
    updated_at: str = ""
    id: Optional[int] = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Handoff":
        if not isinstance(payload, dict):
            raise ValueError("handoff payload must be an object")
        kind = HandoffKind.coerce(payload.get("kind"), "kind")
        status = (HandoffStatus.coerce(payload.get("status"), "status")
                  if payload.get("status") else HandoffStatus.OPEN.value)
        return cls(
            workflow_id=_ident(payload.get("workflow_id"), "workflow_id"),
            task_id=_ident(payload.get("task_id"), "task_id"),
            attempt_no=_pos_int(payload.get("attempt_no", 0), "attempt_no"),
            kind=kind,
            reason=bounded_text(payload.get("reason", ""),
                                MAX_HANDOFF_REASON, field_name="reason"),
            requested_action=bounded_text(
                payload.get("requested_action", ""), MAX_REQUESTED_ACTION,
                field_name="requested_action"),
            status=status,
            disposition=cls._coerce_disposition(payload.get("disposition")),
            disposed_by=bounded_text(payload.get("disposed_by", "") or "",
                                     MAX_DISPOSED_BY,
                                     field_name="disposed_by"),
        )

    @staticmethod
    def _coerce_disposition(value: Any) -> str:
        if value in (None, ""):
            return ""
        text = str(value).strip()
        if text not in DISPOSITIONS:
            raise ValueError(
                f"disposition must be one of {DISPOSITIONS}, got {text!r}")
        return text

    @property
    def asks_scope_change(self) -> bool:
        return self.kind == HandoffKind.SCOPE_CHANGE.value

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "workflow_id": self.workflow_id,
            "task_id": self.task_id, "attempt_no": self.attempt_no,
            "kind": self.kind, "reason": self.reason,
            "requested_action": self.requested_action, "status": self.status,
            "disposition": self.disposition, "disposed_by": self.disposed_by,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }


def can_move_status(src: str, dst: str) -> bool:
    """True if a handoff may move from ``src`` to ``dst`` (forward-only)."""
    if src == dst:
        return True
    return dst in HANDOFF_TRANSITIONS.get(src, set())
