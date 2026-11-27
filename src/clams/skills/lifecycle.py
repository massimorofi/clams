"""Skill state machine (stdlib only).

States
------
    proposed -> draft -> evaluated -> approved -> active -> retired

``proposed`` is "someone suggested this"; ``draft`` is "a manifest+content
exists and is editable"; ``evaluated`` means "this exact content_hash ran
against the versioned evaluation set"; ``approved`` means "a named human signed
off on this exact content_hash"; ``active`` means "deployed and in force";
``retired`` is terminal.

The two rules this module exists to make impossible to get wrong
-----------------------------------------------------------------
1. **Content is identity.** ``content_hash`` is computed from the manifest plus
   the artifact body. Any content change after evaluation drops the version
   back to ``draft`` and invalidates the stored approval — not because the
   approval row is deleted (the ledger is append-only) but because
   ``approval.content_hash != version.content_hash``, which every downstream
   gate checks. A bumped version string with unchanged bytes, and unchanged
   bytes with a bumped version string, are both caught: the hash, not the
   label, is what the approval is about.

2. **Authorship does not confer authority.** A post-mortem report may create
   proposals and drafts and nothing else. That is enforced here as an actor
   capability, so the CLI, the store and any future caller all inherit it
   instead of each re-remembering it.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

PROPOSED = "proposed"
DRAFT = "draft"
EVALUATED = "evaluated"
APPROVED = "approved"
ACTIVE = "active"
RETIRED = "retired"

STATES = (PROPOSED, DRAFT, EVALUATED, APPROVED, ACTIVE, RETIRED)

#: the ladder, plus the two non-forward edges that are legal on purpose:
#:   * back to draft, when content changed after evaluation
#:   * active -> approved, when a newer version supersedes the live one (that
#:     superseded-but-still-approved version is exactly what rollback restores)
TRANSITIONS: dict[str, tuple[str, ...]] = {
    PROPOSED: (DRAFT, RETIRED),
    DRAFT: (EVALUATED, RETIRED),
    EVALUATED: (APPROVED, DRAFT, RETIRED),
    APPROVED: (ACTIVE, DRAFT, RETIRED),
    ACTIVE: (APPROVED, DRAFT, RETIRED),
    RETIRED: (),
}

#: actor kinds. ``postmortem`` is report-driven and therefore proposal-only.
OPERATOR = "operator"
POSTMORTEM = "postmortem"
ACTOR_KINDS = (OPERATOR, POSTMORTEM)

#: the states each actor kind may drive an artifact INTO. An operator owns the
#: whole ladder plus retirement (retiring is a safety action, so it must never
#: require going "up" first). A post-mortem actor owns the top two rungs only:
#: it may suggest and draft, and everything past that needs a human.
ACTOR_ALLOWED_TARGETS: dict[str, frozenset[str]] = {
    OPERATOR: frozenset({PROPOSED, DRAFT, EVALUATED, APPROVED, ACTIVE, RETIRED}),
    POSTMORTEM: frozenset({PROPOSED, DRAFT}),
}

#: ordering, used only for messages and for "how far along is this artifact"
_RANK = {s: i for i, s in enumerate(STATES)}


class LifecycleError(ValueError):
    """Refused a state change. ``code`` is machine-readable."""

    def __init__(self, code: str, reason: str):
        super().__init__(f"[{code}] {reason}")
        self.code = code
        self.reason = reason


class IllegalTransition(LifecycleError):
    def __init__(self, current: str, target: str, why: str = ""):
        super().__init__(
            "illegal_transition",
            f"{current} -> {target} is not a legal skill transition"
            + (f": {why}" if why else ""))
        self.current, self.target = current, target


class ForbiddenActor(LifecycleError):
    """The actor is not allowed to request this state at all."""


@dataclass(frozen=True)
class VersionState:
    """The lifecycle-relevant slice of one skill version."""

    skill_id: str
    version: str
    status: str
    content_hash: str
    source: str = "operator"
    approval_hash: str = ""     # content_hash of the newest approval row, if any
    evaluation_hash: str = ""   # content_hash the newest evaluation ran on

    @property
    def has_valid_approval(self) -> bool:
        return bool(self.approval_hash) and self.approval_hash == self.content_hash

    @property
    def has_valid_evaluation(self) -> bool:
        return bool(self.evaluation_hash) and \
            self.evaluation_hash == self.content_hash


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, ())


def next_states(current: str) -> tuple[str, ...]:
    return TRANSITIONS.get(current, ())


def assert_actor_may(actor_kind: str, target: str) -> None:
    """A post-mortem actor may only ever produce proposals and drafts.

    This is the single place that rule lives, so ``propose``/``draft`` paths can
    be automated while ``evaluate``/``approve``/``activate``/``retire`` cannot.
    """
    if actor_kind not in ACTOR_ALLOWED_TARGETS:
        raise ForbiddenActor("unknown_actor",
                             f"actor kind '{actor_kind}' is not one of "
                             f"{list(ACTOR_ALLOWED_TARGETS)}")
    if target not in _RANK:
        raise IllegalTransition("?", target, "unknown target state")
    if target not in ACTOR_ALLOWED_TARGETS[actor_kind]:
        raise ForbiddenActor(
            "actor_not_permitted",
            f"a '{actor_kind}' actor may not move a skill to '{target}'; "
            f"permitted targets are "
            f"{sorted(ACTOR_ALLOWED_TARGETS[actor_kind])} — post-mortem "
            f"reports create proposals and drafts only, and activation "
            f"always requires a human operator")


def transition(state: VersionState, target: str, *,
               actor_kind: str = OPERATOR) -> VersionState:
    """Return the state after a legal transition, or raise.

    Moving *forward* past ``draft`` additionally requires the evidence for that
    rung to still be about the current bytes: an evaluation only licenses
    ``evaluated``, an approval only licenses ``approved``, and both are keyed to
    ``content_hash``.
    """
    cur = state.status
    if cur not in TRANSITIONS:
        raise IllegalTransition(cur, target, "unknown current state")
    if target not in TRANSITIONS[cur]:
        why = ""
        if cur == RETIRED:
            why = "retired is terminal; re-author the skill as a new version"
        elif target == APPROVED and cur == DRAFT:
            why = "a version must be evaluated before it can be approved"
        elif target == ACTIVE and cur in (DRAFT, PROPOSED, EVALUATED):
            why = "activation requires an approval for this exact content_hash"
        raise IllegalTransition(cur, target, why)

    assert_actor_may(actor_kind, target)

    if target == EVALUATED and not state.has_valid_evaluation:
        raise LifecycleError(
            "missing_evaluation",
            f"{state.skill_id}@{state.version} cannot enter 'evaluated': no "
            f"evaluation exists for content_hash {state.content_hash}")
    if target == APPROVED and not state.has_valid_approval:
        raise LifecycleError(
            "approval_hash_mismatch",
            f"{state.skill_id}@{state.version} cannot enter 'approved': the "
            f"newest approval is for "
            f"{state.approval_hash or '<no approval>'}, not the current "
            f"{state.content_hash}")
    if target == ACTIVE and not (state.has_valid_approval
                                 and state.has_valid_evaluation):
        raise LifecycleError(
            "not_activatable",
            f"{state.skill_id}@{state.version} cannot be activated: evaluation "
            f"and approval must both match the current content_hash")
    return replace(state, status=target)


def apply_content_change(state: VersionState, new_hash: str, *,
                         actor_kind: str = OPERATOR) -> VersionState:
    """Any content change after evaluation returns the artifact to ``draft``.

    The stored approval is not deleted; it is invalidated by hash mismatch,
    which keeps it visible in the audit trail while making it unusable. A
    change to something still ``proposed`` stays ``proposed`` (nothing was ever
    evaluated about it), and a retired version is frozen.
    """
    if new_hash == state.content_hash:
        return state
    if state.status == PROPOSED or state.status == DRAFT:
        # nothing has ever been evaluated about these bytes, so there is no
        # approval to invalidate and no rung to fall back from
        return replace(state, content_hash=new_hash)
    if state.status == RETIRED:
        raise IllegalTransition(RETIRED, DRAFT,
                                "a retired version is immutable; author a new "
                                "version instead")
    # approval_hash / evaluation_hash are deliberately KEPT: the mismatch
    # against the new content_hash is the audit evidence that the old sign-off
    # no longer covers these bytes.
    changed = replace(state, content_hash=new_hash)
    return transition(changed, DRAFT, actor_kind=actor_kind)


def approval_is_current(state: VersionState) -> bool:
    """True iff a stored approval covers the bytes that would actually run."""
    return state.has_valid_approval


def invalidation_reason(state: VersionState) -> str:
    """Human-readable explanation of why an evaluation/approval no longer
    applies. Empty string when everything lines up."""
    if not state.has_valid_evaluation:
        return (f"no evaluation for content_hash {state.content_hash} "
                f"(latest ran on {state.evaluation_hash or '<none>'})")
    if not state.has_valid_approval:
        return (f"approval is for {state.approval_hash or '<no approval>'}, "
                f"not the current {state.content_hash}")
    return ""
