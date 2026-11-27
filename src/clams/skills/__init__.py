"""Skill lifecycle package.

Stdlib only (hashlib, json, dataclasses, re, time, uuid) so the safety gates can
run anywhere the planner, the executor or a post-mortem runs — including a
minimal container with no pydantic installed.

Layers
------
``manifest``   what a skill is allowed to ask for (deny-by-default schema)
``lifecycle``  which state changes are legal, and hash-keyed invalidation
``evaluate``   versioned evaluation sets; safety failures block on their own
``store``      the only writer: persistence, activation gate, rollback
"""
from __future__ import annotations

from .evaluate import (CaseResult, EvalCase, EvalSet, EvaluationError,
                       EvaluationResult, Evaluator, ActivationRefused,
                       build_eval_set, run_evaluation, scope_policy_runner)
from .lifecycle import (ACTIVE, APPROVED, DRAFT, EVALUATED, PROPOSED, RETIRED,
                        STATES, ForbiddenActor, IllegalTransition,
                        LifecycleError, VersionState, apply_content_change,
                        can_transition, transition)
from .manifest import (ManifestError, SkillManifest, content_hash,
                       parse_manifest, validate_manifest)
from .store import SkillStore, StoreError, artifact_hash

__all__ = [
    "ACTIVE", "APPROVED", "DRAFT", "EVALUATED", "PROPOSED", "RETIRED",
    "STATES", "ActivationRefused", "CaseResult", "EvalCase", "EvalSet",
    "EvaluationError", "EvaluationResult", "Evaluator", "ForbiddenActor",
    "IllegalTransition", "LifecycleError", "ManifestError", "SkillManifest",
    "SkillStore", "StoreError", "VersionState", "apply_content_change",
    "artifact_hash", "build_eval_set", "can_transition", "content_hash",
    "parse_manifest", "run_evaluation", "scope_policy_runner", "transition",
    "validate_manifest",
]
