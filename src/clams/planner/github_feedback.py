"""Planner consumption of normalized GitHub deliveries (``github_events``).

The API only records a delivery (ledger row + one ``github_event`` command).
Everything that *reacts* to it lives here, inside the planner, because the
planner is the only domain-state writer.

The five rules this module exists to enforce:

1. **Correlation is a lookup, never an inference.** A delivery is attached to a
   task/workflow only through an explicit ``pr_links`` row (repo + branch/PR +
   head SHA). There is no title/goal similarity anywhere in this file - only
   keys. No stored mapping means no correlation, and no correlation means no
   side effect beyond the audit row.
2. **The event SHA must equal the currently associated PR head.** ``pr_links``
   ``state='current'`` defines "current". A delivery about an older commit (a
   late check, a re-run, a redelivery) is recorded as ``stale_sha`` and stays
   audit-only: it cannot dispatch work and cannot mark a PR current.
3. **Infrastructure/configuration failures are their own class.** A check that
   timed out, was cancelled, needs an action or failed for setup/credential/
   quota reasons is routed to the operator as a decision, never turned into a
   "fix the code" task - an automatic retry of an infra failure just burns
   attempts.
4. **One delivery, one logical event, one follow-up.** ``delivery_id`` is the
   intake idempotency key; ``gh_followups`` is the side-effect key. A GitHub
   redelivery or a planner restart between commit and command-ack therefore
   produces at most one event row and at most one follow-up of each kind.
5. **Outcomes route through the handoff model.** By default a review request or
   a failed check creates a Planner-visible decision / operator handoff.
   Automatic follow-up *task* dispatch happens only when the project explicitly
   opts in (``projects.gh_followup_policy = 'auto_dispatch'``); the follow-up
   must pass the same plan/brief validation as any planned task and keeps the
   original task's contract (repo, base ref, writable scope, run commands).

What this module never does: rewrite task evidence, treat a green GitHub check
as Clams gate evidence (the gate is the executor's own ``evidence.passed`` from
``task.result``), or talk to GitHub directly (projections go through the
idempotent outbox).
"""
from __future__ import annotations

import json
import logging
import re
from contextlib import contextmanager
from typing import Any, Optional

from .. import contracts
from ..contracts import Plan, PlanTask, validate_plan
from ..db import (repo_alerts, repo_events, repo_github_events, repo_handoffs,
                  repo_outbox, repo_pr_links, repo_tasks, repo_workflows)
from ..github.webhooks import NormalizedEvent
from ..handoffs import Handoff, HandoffKind

log = logging.getLogger("clams.github_feedback")

# --- vocabularies -----------------------------------------------------------
CORR = repo_pr_links            # verdict names come from the repo helper
DISPOSITION_AUDIT = "audit_only"
DISPOSITION_TIMELINE = "timeline"
DISPOSITION_HANDOFF = "handoff"
DISPOSITION_DISPATCH = "dispatch"

CLASS_NONE = "none"
CLASS_CODE = "code"
CLASS_INFRA = "infra_config"

#: outcomes derived from the normalized ``event_action`` + bounded summary
OUT_CHECK_PASS = "check_passed"
OUT_CHECK_FAIL = "check_failed"
OUT_REVIEW_CHANGES = "review_changes_requested"
OUT_REVIEW_APPROVED = "review_approved"
OUT_REVIEW_REQUEST = "review_requested"
OUT_PR_MERGED = "pr_merged"
OUT_PR_CLOSED = "pr_closed"
OUT_PR_UPDATE = "pr_updated"
OUT_OTHER = "other"

#: outcomes that never produce a follow-up, whatever the policy says
TIMELINE_ONLY = frozenset({OUT_PR_MERGED, OUT_PR_CLOSED, OUT_PR_UPDATE,
                           OUT_REVIEW_APPROVED, OUT_CHECK_PASS, OUT_OTHER})

#: conclusions that are never "your code is wrong"
_INFRA_CONCLUSIONS = frozenset({"timed_out", "cancelled", "action_required",
                                "stale", "skipped"})
#: conservative substrings that mark a *configuration / platform* failure.
#: Deliberately about the runner/setup, not about test output.
_INFRA_MARKERS = (
    "action_required", "timed out", "timeout", "no runner", "runner offline",
    "setup", "quota", "rate limit", "rate-limit", "registry", "network",
    "dns", "credential", "token expired", "permission denied (auth)",
    "invalid workflow", "workflow syntax", "unknown action", "oomkilled",
    "503", "502", "500 internal",
)
_CONCLUSION_RE = re.compile(r"conclusion=([A-Za-z_\-]+)")
_REVIEW_RE = re.compile(r"review ([a-z_\-]+)")

#: the Clams evidence gate is executor-reported; GitHub checks are NOT in it
GATE_SATISFYING_OUTCOMES: frozenset = frozenset()


@contextmanager
def _tx(conn):
    """Join the caller's transaction if one is open, else own a BEGIN IMMEDIATE.

    ``drain_commands`` already wraps each command in ``BEGIN IMMEDIATE``;
    nesting one would raise "cannot start a transaction within a transaction".
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# --------------------------------------------------------------- classification
def check_conclusion(summary: str) -> str:
    """The bounded ``conclusion=<x>`` token of a check event (or ``''``)."""
    m = _CONCLUSION_RE.search(summary or "")
    return (m.group(1) if m else "").strip().lower()


def classify_check(summary: str) -> str:
    """``code`` | ``infra_config`` | ``none`` for a completed check.

    ``success``/``success``-ish conclusions are ``none`` (nothing to route).
    Anything the platform owns - timeout, cancellation, ``action_required``,
    or a setup/credential/quota marker - is ``infra_config``. A plain
    ``failure`` is a code failure.
    """
    text = (summary or "").lower()
    conclusion = check_conclusion(text)
    if conclusion in ("", "success", "neutral"):
        # no conclusion token at all: fall back to the words in the summary
        return CLASS_CODE if "fail" in text else CLASS_NONE
    if conclusion in _INFRA_CONCLUSIONS:
        return CLASS_INFRA
    if any(marker in text for marker in _INFRA_MARKERS):
        return CLASS_INFRA
    return CLASS_CODE


def outcome_of(event_action: str, summary: str) -> str:
    """Map the normalized ``event_action`` (+ summary) onto a routing outcome."""
    name, _, action = (event_action or "").partition(".")
    if name in ("check_suite", "check_run"):
        conclusion = check_conclusion(summary)
        if conclusion in ("", "success", "neutral"):
            return OUT_CHECK_PASS
        return OUT_CHECK_FAIL
    if name == "pull_request_review":
        m = _REVIEW_RE.search(summary or "")
        state = (m.group(1) if m else "").strip().lower()
        if state in ("changes_requested", "changes-requested",
                     "changes requested"):
            return OUT_REVIEW_CHANGES
        if state == "approved":
            return OUT_REVIEW_APPROVED
        return OUT_OTHER
    if name == "pull_request":
        if action == "closed":
            # normalization keeps no merged flag (free text is dropped), so a
            # closed PR is recorded as closed; a merged one is still a terminal
            # timeline entry either way.
            text = (summary or "").lower()
            return OUT_PR_MERGED if "merged" in text else OUT_PR_CLOSED
        if action == "ready_for_review":
            return OUT_REVIEW_REQUEST
        return OUT_PR_UPDATE
    return OUT_OTHER


# ------------------------------------------------------------------- mapping
def link_task_head(conn, *, task_id: str, repo: str, head_sha: str,
                   head_ref: Optional[str] = None,
                   pr_number: Optional[int] = None,
                   created_by: str = "planner") -> Optional[int]:
    """Store the explicit mapping an event is later correlated against.

    This is the *only* way a delivery can become attributable: whoever knows
    "task T pushed SHA S to PR N of repo R" calls this. Returns ``None`` when
    the task is unknown (a mapping to nothing is not a mapping).
    """
    task = repo_tasks.get_task(conn, task_id)
    if task is None or not head_sha:
        return None
    return repo_pr_links.link_task_commit(
        conn, workflow_id=task["workflow_id"], task_id=task_id, repo=repo,
        head_sha=head_sha, head_ref=head_ref, pr_number=pr_number,
        created_by=created_by)


# --------------------------------------------------------------- side effects
def _next_sequence_index(conn, workflow_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(sequence_index),0) m FROM tasks WHERE workflow_id=?",
        (workflow_id,)).fetchone()
    return int(row["m"]) + 1


def _unique_title(conn, workflow_id: str, base: str) -> str:
    """tasks has UNIQUE(workflow_id, title): keep the follow-up insertable."""
    title = base[:200]
    n = 1
    while conn.execute("SELECT 1 FROM tasks WHERE workflow_id=? AND title=?",
                       (workflow_id, title)).fetchone() is not None:
        n += 1
        title = f"{base[:190]} ({n})"
    return title


def build_followup_brief(task, *, objective: str, acceptance: str,
                         default_max_turns: int = 40) -> dict:
    """A follow-up brief that *keeps the original task contract*.

    Everything that defines the working agreement - repo, base ref, writable
    scope, forbidden paths, the verification commands, the turn budget - is
    copied from the original brief. Only the objective and acceptance change.
    """
    original = {}
    try:
        original = json.loads(task["brief_json"] or "{}")
    except (TypeError, ValueError):
        original = {}
    brief = {
        "schema_version": contracts.SCHEMA_VERSION,
        "objective": objective,
        "acceptance": acceptance,
        "run": list(original.get("run") or []),
        "writable": list(original.get("writable") or []),
        "forbidden": list(original.get("forbidden") or []),
        "repo": original.get("repo") or "",
        "base_ref": original.get("base_ref") or "main",
        "max_turns": int(original.get("max_turns") or default_max_turns),
    }
    return brief


def validate_followup(brief: dict, title: str) -> None:
    """Run the *existing* validation machinery over the follow-up.

    ``contracts.Brief`` is the dispatch contract; ``validate_plan`` is what the
    planner applies to every planned task. A follow-up that does not pass both
    is never created - the caller falls back to an operator handoff.
    """
    contracts.Brief(**{k: v for k, v in brief.items()
                       if k in contracts.Brief.model_fields})
    plan = Plan(tasks=[PlanTask(
        title=title, objective=brief["objective"],
        acceptance=brief["acceptance"], run=brief["run"],
        writable=brief["writable"], forbidden=brief["forbidden"],
        depends_on=[])])
    validate_plan(plan, max_tasks=1)


def _create_followup_task(conn, task, ev: NormalizedEvent, *, reason: str,
                          requested_action: str,
                          default_max_turns: int = 40) -> Optional[str]:
    """Policy-enabled path: one validated follow-up task per delivery."""
    if not repo_github_events.claim_followup(conn, ev.delivery_id, "task"):
        return None                      # a replay already produced one
    wf_id = task["workflow_id"]
    title = _unique_title(
        conn, wf_id, f"Fix GitHub {reason}"
        + (f" (PR #{ev.pr_number})" if ev.pr_number else ""))
    brief = build_followup_brief(
        task,
        objective=(f"{requested_action}\n\nContext: GitHub {reason} on "
                   f"{ev.repo}"
                   + (f" PR #{ev.pr_number}" if ev.pr_number else "")
                   + (f" at {ev.head_sha}" if ev.head_sha else "")
                   + f". {ev.summary}"[:900]),
        acceptance=(f"The GitHub {reason} no longer reproduces and the task's "
                    f"own verification commands pass. Original acceptance: "
                    f"{task['acceptance']}"[:900]),
        default_max_turns=default_max_turns)
    try:
        validate_followup(brief, title)
    except (ValueError, contracts.PlanValidationError) as exc:
        # never invent an invalid task: release the claim and let the caller
        # fall back to a handoff
        conn.execute("DELETE FROM gh_followups WHERE delivery_id=? AND kind='task'",
                     (ev.delivery_id,))
        log.warning("follow-up for %s failed validation: %s", ev.delivery_id, exc)
        return None
    task_id = repo_tasks.create_task(
        conn, wf_id, _next_sequence_index(conn, wf_id), title,
        brief["objective"], brief["acceptance"], brief,
        depends_on=[], max_attempts=task["max_attempts"],
        gh_issue_number=task["gh_issue_number"])
    repo_events.append_event(
        conn, "workflow", wf_id, "info",
        f"github feedback: follow-up task created ({reason})",
        {"delivery_id": ev.delivery_id, "task_id": task_id,
         "source_task_id": task["task_id"]})
    repo_tasks.promote_ready(conn)
    return task_id


def _create_handoff(conn, task, ev: NormalizedEvent, *, kind: str,
                    reason: str, requested_action: str) -> Optional[int]:
    """Default path: a Planner-visible operator handoff (never an edit)."""
    if not repo_github_events.claim_followup(conn, ev.delivery_id, "handoff"):
        return None
    res = repo_handoffs.record_handoff(conn, Handoff(
        workflow_id=task["workflow_id"], task_id=task["task_id"],
        attempt_no=int(task["attempt"]), kind=kind, reason=reason[:1000],
        requested_action=requested_action[:500]))
    return res.get("id")


def _project_comment(conn, ev: NormalizedEvent, body: str) -> Optional[int]:
    """GitHub projection through the idempotent outbox (invariant 3)."""
    if not ev.pr_number:
        return None
    if not repo_github_events.claim_followup(conn, ev.delivery_id,
                                             "outbox_comment"):
        return None
    key = f"clams:gh-feedback:{ev.delivery_id}"
    # the marker rides in the body: gh_client.add_comment dedups by finding the
    # idempotency key in an existing comment, which is what makes a relay retry
    # after a partial failure safe.
    return repo_outbox.enqueue(conn, "gh_comment",
                               {"repo": ev.repo, "issue": ev.pr_number,
                                "body": f"{body[:1900]}\n\n<!-- {key} -->",
                                "idempotency_key": key},
                               idempotency_key=key)


# ------------------------------------------------------------------ the route
def apply_delivery(conn, ev: NormalizedEvent, *,
                   settings: Any = None) -> dict[str, Any]:
    """Consume one normalized delivery. Idempotent on ``delivery_id``.

    Returns a verdict dict: ``correlation``, ``classification``, ``outcome``,
    ``disposition``, ``task_id``, ``workflow_id``, ``handoff_id``,
    ``followup_task_id``, ``outbox_id``, ``status``
    (``applied`` | ``duplicate`` | ``invalid``).
    """
    if not ev or not ev.delivery_id or not ev.repo:
        return {"status": "invalid", "reason": "missing delivery_id or repo"}
    event_action = ev.event_action
    with _tx(conn):
        # The API writes the ledger row (state 'new') and the command together;
        # the planner writes the verdict and moves the state. So "row already
        # there" is normal - "row already *consumed*" is the duplicate.
        repo_github_events.insert_event(conn, ev)
        row = repo_github_events.get_event(conn, ev.delivery_id)
        if row is not None and row["processed_state"] in ("done", "skipped"):
            prior = repo_github_events.get_outcome(conn, ev.delivery_id)
            return {"status": "duplicate", "delivery_id": ev.delivery_id,
                    "correlation": prior["correlation"] if prior else "",
                    "disposition": prior["disposition"] if prior else "",
                    "followup_task_id": None, "handoff_id": None,
                    "outbox_id": None}
        verdict = _route(conn, ev, event_action, settings=settings)
        repo_github_events.record_outcome(
            conn, ev.delivery_id, correlation=verdict["correlation"],
            classification=verdict["classification"],
            disposition=verdict["disposition"], outcome=verdict.get("outcome", ""),
            link_id=verdict.get("link_id"), task_id=verdict.get("task_id"),
            workflow_id=verdict.get("workflow_id"),
            detail=verdict.get("reason", ""))
        repo_github_events.mark_processed(conn, ev.delivery_id, "done")
        return verdict


def _route(conn, ev: NormalizedEvent, event_action: str, *,
           settings: Any = None) -> dict[str, Any]:
    corr = repo_pr_links.correlate(conn, repo=ev.repo, pr_number=ev.pr_number,
                                   head_ref=ev.head_ref, head_sha=ev.head_sha)
    status = corr["status"]
    outcome = outcome_of(event_action, ev.summary)
    classification = (classify_check(ev.summary)
                      if outcome in (OUT_CHECK_PASS, OUT_CHECK_FAIL)
                      else CLASS_NONE)
    link = corr.get("link")
    base: dict[str, Any] = {
        "status": "applied", "delivery_id": ev.delivery_id,
        "correlation": status, "classification": classification,
        "outcome": outcome, "disposition": DISPOSITION_AUDIT,
        "link_id": int(link["link_id"]) if link else None,
        "task_id": None, "workflow_id": None, "handoff_id": None,
        "followup_task_id": None, "outbox_id": None}

    # --- 1. anything but a matched, current link is audit-only --------------
    if status != repo_pr_links.MATCHED or link is None:
        level = "info" if status == repo_pr_links.UNMATCHED else "warn"
        reason = {
            repo_pr_links.NO_REPO: "repository is not a configured project",
            repo_pr_links.UNMATCHED:
                "no stored repo+branch/PR+SHA mapping for this delivery",
            repo_pr_links.STALE_SHA:
                "event head_sha is not the currently associated PR head",
            repo_pr_links.AMBIGUOUS:
                "several current links claim this PR/branch; not attributable",
        }.get(status, "not correlated")
        repo_events.append_event(
            conn, "github", ev.repo, level,
            f"github feedback audit-only ({event_action}): {reason}",
            {"delivery_id": ev.delivery_id, "correlation": status,
             "head_sha": ev.head_sha, "pr_number": ev.pr_number,
             "classification": classification})
        base["reason"] = reason
        return base

    base["task_id"] = link["task_id"]
    base["workflow_id"] = link["workflow_id"]
    task = repo_tasks.get_task(conn, link["task_id"])
    wf = repo_workflows.get_workflow(conn, link["workflow_id"])
    if task is None or wf is None:
        base["reason"] = "linked task or workflow no longer exists"
        base["disposition"] = DISPOSITION_AUDIT
        return base

    # --- 2. timeline-only outcomes (never rewrite task evidence) ------------
    if outcome in (OUT_PR_MERGED, OUT_PR_CLOSED):
        repo_pr_links.set_state(conn, int(link["link_id"]),
                                repo_pr_links.STATE_CLOSED)
        repo_events.append_event(
            conn, "workflow", wf["workflow_id"], "info",
            f"{'PR merged' if outcome == OUT_PR_MERGED else 'PR closed'}"
            f" ({ev.repo}#{ev.pr_number} @ {ev.head_sha or '?'})",
            {"delivery_id": ev.delivery_id, "task_id": task["task_id"],
             "head_sha": ev.head_sha, "pr_number": ev.pr_number})
        repo_github_events.claim_followup(conn, ev.delivery_id, "timeline",
                                          ref_id=wf["workflow_id"])
        base["disposition"] = DISPOSITION_TIMELINE
        base["reason"] = "recorded on the workflow timeline; task evidence untouched"
        return base

    if outcome == OUT_CHECK_PASS:
        # A green GitHub check is *not* Clams gate evidence. The gate is the
        # executor's own evidence.passed in task.result (planner/review.py).
        # So: note it, touch nothing.
        repo_events.append_event(
            conn, "workflow", wf["workflow_id"], "info",
            f"GitHub check passed on {ev.repo}"
            + (f"#{ev.pr_number}" if ev.pr_number else "")
            + f" @ {ev.head_sha or '?'} (informational: not Clams gate evidence)",
            {"delivery_id": ev.delivery_id, "task_id": task["task_id"],
             "gate_evidence": False})
        base["disposition"] = DISPOSITION_AUDIT
        base["reason"] = "passing GitHub check is not Clams gate evidence"
        return base

    if outcome in (OUT_PR_UPDATE, OUT_REVIEW_APPROVED, OUT_OTHER):
        repo_events.append_event(
            conn, "workflow", wf["workflow_id"], "info",
            f"github feedback: {event_action} ({ev.summary})",
            {"delivery_id": ev.delivery_id, "task_id": task["task_id"]})
        base["disposition"] = DISPOSITION_TIMELINE
        base["reason"] = "timeline entry only"
        return base

    # --- 3. actionable outcomes: failed check / review request --------------
    if outcome in TIMELINE_ONLY:      # belt and braces: handled above, never here
        base["disposition"] = DISPOSITION_AUDIT
        base["reason"] = "timeline-only outcome"
        return base
    actionable = outcome in (OUT_CHECK_FAIL, OUT_REVIEW_CHANGES,
                             OUT_REVIEW_REQUEST)
    if not actionable:
        base["disposition"] = DISPOSITION_AUDIT
        return base

    if classification == CLASS_INFRA:
        # infrastructure / configuration: an operator decision, never a
        # code-fix task, whatever the project's dispatch policy says
        repo_alerts.raise_alert(
            conn, f"gh:infra:{ev.repo}:{ev.check_id or ev.delivery_id}", "warn",
            f"GitHub check failure on {ev.repo}"
            + (f" PR #{ev.pr_number}" if ev.pr_number else "")
            + f" classified as infrastructure/configuration: {ev.summary}")
        reason = f"infrastructure/configuration check failure: {ev.summary}"
        action = ("Fix the CI configuration or runner, then re-run the check. "
                  "No code change is implied.")
        hid = _create_handoff(conn, task, ev, kind=HandoffKind.NEEDS_DECISION.value,
                              reason=reason, requested_action=action)
        base["handoff_id"] = hid
        base["disposition"] = DISPOSITION_HANDOFF
        base["reason"] = reason
        base["outbox_id"] = _project_comment(
            conn, ev, "Clams: this check failure looks like an "
            "infrastructure/configuration problem, not a code failure, so it "
            "was routed to an operator handoff (no automatic fix task).")
        return base

    reason = (f"GitHub check failed: {ev.summary}" if outcome == OUT_CHECK_FAIL
              else f"GitHub review asks for changes: {ev.summary}")
    action = (f"Make the failing GitHub check on {ev.repo}"
              + (f" PR #{ev.pr_number}" if ev.pr_number else "")
              + f" pass without changing the task's acceptance criteria.")
    policy = repo_pr_links.followup_policy(conn, ev.repo)
    if policy == repo_pr_links.POLICY_AUTO_DISPATCH:
        followup = _create_followup_task(
            conn, task, ev, reason=reason, requested_action=action,
            default_max_turns=int(getattr(settings, "max_turns", 40) or 40))
        if followup:
            base["followup_task_id"] = followup
            base["disposition"] = DISPOSITION_DISPATCH
            base["reason"] = reason
            base["outbox_id"] = _project_comment(
                conn, ev, "Clams: project policy allows automatic follow-up, "
                "so a fix task was created for this failure.")
            return base
        # validation refused the task -> operator handoff instead
        base["reason"] = f"{reason} (follow-up failed validation)"
    else:
        base["reason"] = reason
    hid = _create_handoff(
        conn, task, ev,
        kind=(HandoffKind.NEEDS_DECISION.value
              if outcome in (OUT_REVIEW_CHANGES, OUT_REVIEW_REQUEST)
              else HandoffKind.BLOCKED.value),
        reason=reason, requested_action=action)
    base["handoff_id"] = hid
    base["disposition"] = DISPOSITION_HANDOFF
    base["outbox_id"] = _project_comment(
        conn, ev, "Clams: this failure was routed to an operator handoff. "
        "Automatic follow-up dispatch is disabled for this project.")
    return base


# ------------------------------------------------------------- entry points
def apply_payload(conn, payload: dict, *, settings: Any = None) -> dict[str, Any]:
    """Consume a delivery from a stored row / command-inbox payload."""
    return apply_delivery(conn, repo_github_events.event_from_payload(payload),
                          settings=settings)


def handle_command(conn, payload: dict, *, settings: Any = None) -> dict[str, Any]:
    """Entry point used by ``planner/commands.py`` for kind == 'github_event'."""
    result = apply_payload(conn, payload, settings=settings)
    log.info("github_event %s: %s", result.get("delivery_id"),
             result.get("disposition") or result.get("status"))
    return result


def sweep_unprocessed(conn, *, settings: Any = None, limit: int = 20) -> int:
    """Consume ledger rows no cycle has taken yet (restart / lost command net).

    Idempotent by construction: a delivery already applied returns
    ``duplicate`` and produces nothing.
    """
    n = 0
    for row in repo_github_events.unprocessed(conn, limit=limit):
        apply_delivery(conn, repo_github_events.event_from_row(row),
                       settings=settings)
        n += 1
    return n
