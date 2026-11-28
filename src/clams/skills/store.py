"""Persistence + policy for the skill lifecycle (stdlib only).

One object, one connection, one rule set. Every mutation in this module goes
through :mod:`clams.skills.lifecycle` for state legality and
:mod:`clams.skills.evaluate` for the activation gate, so there is exactly one
implementation of "may this skill go live" no matter whether the caller is the
CLI, the planner, or a test.

Identity of an artifact
-----------------------
``artifact_hash(manifest, content)`` hashes the manifest document *together
with* the body. Two versions with the same bytes but different version strings
hash the same; two versions with the same string and different bytes hash
differently. Approvals, evaluations and deployments all store that hash, and
activation requires all three to agree with what is stored on the version.

Who may do what
---------------
* ``actor_kind='postmortem'`` can propose and draft, and nothing else. A
  post-mortem can therefore never activate or deploy a skill, even by calling
  the store directly.
* ``approve`` needs a human identity that is (a) not an automated one, (b) not
  the artifact's own author (no self-approval), and (c) if the operator wired an
  allowlist, present in it.
* every mutation records ``actor`` so the ledger names a person.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

from ..db import repo_skills
from ..db.conn import immediate, utcnow_s
from . import evaluate as ev
from . import lifecycle as lc
from .manifest import ManifestError, content_hash, parse_manifest

#: identities that are never allowed to sign an approval
NON_HUMAN_IDENTITIES = {"", "system", "auto", "bot", "clams", "postmortem",
                        "planner", "executor", "unknown"}


class StoreError(ValueError):
    def __init__(self, code: str, reason: str):
        super().__init__(f"[{code}] {reason}")
        self.code = code
        self.reason = reason


def artifact_hash(manifest_doc: Mapping[str, Any],
                  content: str = "") -> str:
    """sha256 over {manifest, body}. The single identity all gates compare."""
    payload = {"manifest": dict(manifest_doc), "content": content or ""}
    return "sha256:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False).encode("utf-8")).hexdigest()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


@dataclass(frozen=True)
class AuditEntry:
    action: str
    skill_id: str
    version: str
    actor: str
    detail: str = ""
    created_at: str = ""


class SkillStore:
    """The only writer of the skill tables. Wrap one SQLite connection."""

    def __init__(self, conn: sqlite3.Connection, *,
                 allowed_approvers: Optional[Sequence[str]] = None,
                 on_audit: Optional[Callable[[AuditEntry], None]] = None):
        self.conn = conn
        #: None = any named human may approve; a sequence = only those may.
        self.allowed_approvers = (None if allowed_approvers is None
                                  else {a.strip() for a in allowed_approvers})
        self.on_audit = on_audit
        self._has_events: Optional[bool] = None

    # ------------------------------------------------------------- internals

    def _audit(self, action: str, skill_id: str, version: str, actor: str,
               detail: str = "") -> None:
        entry = AuditEntry(action, skill_id, version, actor, detail, utcnow_s())
        # the shared events table is optional here: a standalone skill DB has
        # only migration 015, so a missing table must not lose the mutation.
        if self._has_events is None:
            row = self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND "
                "name='events'").fetchone()
            self._has_events = row is not None
        if self._has_events:
            self.conn.execute(
                "INSERT INTO events(scope, ref, level, message, data) "
                "VALUES(?,?,?,?,?)",
                ("skill", f"{skill_id}@{version}", "info", f"{action} by {actor}",
                 detail))
        if self.on_audit is not None:
            self.on_audit(entry)

    def _sync_skill(self, skill_id: str, status: str,
                    current_version: str = "") -> None:
        """Keep the skills roll-up row honest.

        ``skills.status`` describes the *skill*, which is only 'active' while a
        deployment is live. Editing or re-running some other version of the same
        skill must not downgrade it (that would silently un-deploy the live
        version from ``active_skill_versions``).
        """
        live = repo_skills.active_deployment(self.conn, skill_id)
        if live is not None:
            repo_skills.upsert_skill(self.conn, skill_id, lc.ACTIVE,
                                     current_version=live["version"])
        else:
            repo_skills.upsert_skill(self.conn, skill_id, status,
                                     current_version=current_version)

    def state(self, skill_id: str, version: str) -> lc.VersionState:
        """Rebuild the lifecycle view of a version from stored rows only."""
        v = repo_skills.get_version(self.conn, skill_id, version)
        if v is None:
            raise StoreError("unknown_version",
                             f"no skill version {skill_id}@{version}")
        ap = repo_skills.latest_approval(self.conn, skill_id, version)
        en = repo_skills.latest_evaluation(self.conn, skill_id, version)
        return lc.VersionState(
            skill_id=skill_id, version=version, status=v["status"],
            content_hash=v["content_hash"], source=v["source"],
            approval_hash=(ap["content_hash"] if ap else ""),
            evaluation_hash=(en["content_hash"] if en else ""))

    def _manifest_of(self, version_row: dict):
        return parse_manifest(json.loads(version_row["manifest_json"]),
                              source=version_row["source"])

    def _require_actor(self, actor: str, actor_kind: str,
                       target: str) -> None:
        if not str(actor).strip():
            raise StoreError("missing_actor",
                             "every skill mutation must name an operator")
        lc.assert_actor_may(actor_kind, target)

    # ---------------------------------------------------------------- propose

    def propose(self, manifest_doc: Mapping[str, Any], *,
                content: str = "", source: str = "operator",
                actor: str, actor_kind: str = lc.OPERATOR,
                provenance: Optional[Mapping[str, Any]] = None
                ) -> lc.VersionState:
        """Register a new skill version in ``proposed``.

        This is the one entry point a post-mortem may use: it creates a proposal
        and stops. The manifest is validated before anything is written, so an
        unsafe proposal is refused rather than stored and argued about later.
        """
        self._require_actor(actor, actor_kind, lc.PROPOSED)
        if source not in ("operator", "postmortem", "import", "system"):
            raise StoreError("bad_source", f"source '{source}' is not allowed")
        try:
            manifest = parse_manifest(manifest_doc, source=source)
        except ManifestError as exc:
            raise StoreError(exc.code, exc.reason) from exc
        prov = dict(provenance or manifest.provenance)
        prov.setdefault("proposed_by", actor)
        prov.setdefault("proposed_at", utcnow_s())
        h = artifact_hash(manifest.to_dict(), content)
        with immediate(self.conn):
            if repo_skills.get_skill(self.conn, manifest.skill_id) is None:
                repo_skills.upsert_skill(self.conn, manifest.skill_id,
                                         lc.PROPOSED)
            if repo_skills.get_version(self.conn, manifest.skill_id,
                                       manifest.version) is not None:
                raise StoreError(
                    "duplicate_version",
                    f"{manifest.skill_id}@{manifest.version} already exists; "
                    f"content changes need a new semver")
            repo_skills.put_version(
                self.conn, skill_id=manifest.skill_id, version=manifest.version,
                manifest=manifest.to_dict(), content_hash=h,
                status=lc.PROPOSED, source=source, provenance=prov)
            self._audit("propose", manifest.skill_id, manifest.version, actor,
                        f"source={source} hash={h}")
        return self.state(manifest.skill_id, manifest.version)


    # ------------------------------------------------------------------ draft

    def draft(self, skill_id: str, version: str, *, actor: str,
              actor_kind: str = lc.OPERATOR,
              content: Optional[str] = None,
              manifest_doc: Optional[Mapping[str, Any]] = None
              ) -> lc.VersionState:
        """Move proposed -> draft, and/or edit the artifact in the draft state.

        Editing a draft is free (nothing has been signed off yet). Editing
        anything *past* draft is where :meth:`edit_content` takes over, because
        that is where a hash stops matching an approval.
        """
        self._require_actor(actor, actor_kind, lc.DRAFT)
        cur = self.state(skill_id, version)
        if cur.status not in (lc.PROPOSED, lc.DRAFT):
            raise lc.IllegalTransition(
                cur.status, lc.DRAFT,
                "only a proposed or draft version is editable; a content change "
                "to an evaluated/approved/active artifact goes through "
                "edit_content, which drops it back to draft")
        row = repo_skills.get_version(self.conn, skill_id, version)
        new_hash = cur.content_hash
        manifest = self._manifest_of(row)
        if manifest_doc is not None and content is None:
            raise StoreError(
                "missing_content",
                "a manifest edit must also pass the new content: the stored "
                "hash covers manifest+body, so a manifest-only edit would "
                "silently re-hash against an empty body")
        if manifest_doc is not None:
            try:
                manifest = parse_manifest(manifest_doc, source=row["source"])
            except ManifestError as exc:
                raise StoreError(exc.code, exc.reason) from exc
            if (manifest.skill_id, manifest.version) != (skill_id, version):
                raise StoreError("identity_change",
                                 "a draft edit may not change skill_id or "
                                 "version; that is a new artifact")
            new_hash = artifact_hash(manifest.to_dict(),
                                     content if content is not None else "")
        elif content is not None:
            new_hash = artifact_hash(manifest.to_dict(), content)

        with immediate(self.conn):
            if new_hash != cur.content_hash:
                repo_skills.put_version(
                    self.conn, skill_id=skill_id, version=version,
                    manifest=manifest.to_dict(), content_hash=new_hash,
                    status=cur.status, source=row["source"],
                    provenance=json.loads(row["provenance_json"] or "{}"))
                cur = self.state(skill_id, version)
            if cur.status == lc.PROPOSED:
                cur = lc.transition(cur, lc.DRAFT, actor_kind=actor_kind)
                repo_skills.set_version(self.conn, skill_id, version, lc.DRAFT)
                self._sync_skill(skill_id, lc.DRAFT)
            self._audit("draft", skill_id, version, actor,
                        f"hash={self.state(skill_id, version).content_hash}")
        return self.state(skill_id, version)

    def edit_content(self, skill_id: str, version: str, *, content: str,
                     actor: str,
                     actor_kind: str = lc.OPERATOR) -> lc.VersionState:
        """Change the bytes of an existing version.

        The whole point of this method is the side effect: if the version had
        already been evaluated or approved, the new hash no longer matches those
        rows, so the version drops back to ``draft`` and the stored approval is
        invalidated by mismatch (kept in the ledger, unusable for activation).
        """
        cur = self.state(skill_id, version)
        row = repo_skills.get_version(self.conn, skill_id, version)
        manifest = self._manifest_of(row)
        new_hash = artifact_hash(manifest.to_dict(), content)
        if new_hash == cur.content_hash:
            return cur
        after = lc.apply_content_change(cur, new_hash, actor_kind=actor_kind)
        with immediate(self.conn):
            repo_skills.set_version(self.conn, skill_id, version,
                                    after.status, new_hash)
            self._sync_skill(skill_id, after.status)
            self._audit("content_change", skill_id, version, actor,
                        f"{cur.content_hash} -> {new_hash}; status "
                        f"{cur.status} -> {after.status}; approval "
                        f"{'invalidated' if cur.approval_hash and cur.approval_hash != new_hash else 'n/a'}")
        return self.state(skill_id, version)


    # --------------------------------------------------------------- evaluate

    def evaluate(self, skill_id: str, version: str, *,
                 eval_set: ev.EvalSet, evaluator: ev.Evaluator,
                 runner: Optional[ev.Runner] = None, actor: str,
                 actor_kind: str = lc.OPERATOR,
                 min_pass_rate: Optional[float] = None) -> ev.EvaluationResult:
        """Run the versioned eval set against this exact artifact and store the
        verdict. On a pass the version moves draft -> evaluated; on a fail it
        stays put and the failed row is still recorded (a failed evaluation is
        evidence, not something to hide)."""
        self._require_actor(actor, actor_kind, lc.EVALUATED)
        cur = self.state(skill_id, version)
        if cur.status not in (lc.DRAFT, lc.EVALUATED, lc.APPROVED):
            raise lc.IllegalTransition(
                cur.status, lc.EVALUATED,
                "only a draft, evaluated or approved version can be (re-)run "
                "against the eval set")
        row = repo_skills.get_version(self.conn, skill_id, version)
        manifest = self._manifest_of(row)
        required = manifest.activation.get("eval_set_version", "")
        if required and required != eval_set.eval_set_version:
            raise StoreError(
                "eval_set_mismatch",
                f"the manifest demands eval_set_version '{required}', got "
                f"'{eval_set.eval_set_version}'")
        rate = (float(manifest.activation.get("min_pass_rate",
                                             ev.DEFAULT_MIN_PASS_RATE))
                if min_pass_rate is None else float(min_pass_rate))
        prev_row = repo_skills.latest_evaluation(self.conn, skill_id, version)
        previous = (ev.result_from_row(prev_row)
                    if prev_row and prev_row["content_hash"] == cur.content_hash
                    else None)
        result = ev.run_evaluation(
            manifest=manifest, content_hash=cur.content_hash,
            eval_set=eval_set, evaluator=evaluator,
            runner=runner or ev.scope_policy_runner(manifest),
            min_pass_rate=rate, previous=previous)
        with immediate(self.conn):
            repo_skills.add_evaluation(
                self.conn, evaluation_id=result.evaluation_id,
                skill_id=skill_id, version=version,
                content_hash=result.content_hash,
                eval_set_version=result.eval_set_version,
                evaluator_version=result.evaluator_version,
                prompt_version=result.prompt_version,
                model_id=result.model_id,
                per_case_json=result.per_case_json(),
                aggregate_json=result.aggregate_json(),
                passed=result.passed)
            if result.passed:
                if cur.status == lc.DRAFT:
                    # re-read so the state now carries the evaluation hash the
                    # transition rule requires, then take the rung
                    nxt = lc.transition(self.state(skill_id, version),
                                        lc.EVALUATED, actor_kind=actor_kind)
                    repo_skills.set_version(self.conn, skill_id, version,
                                            nxt.status)
                    self._sync_skill(skill_id, nxt.status)
            elif cur.status in (lc.EVALUATED, lc.APPROVED):
                # a fresh failure on unchanged bytes is just as disqualifying as
                # an edit: the earlier verdict no longer describes the artifact
                lc.transition(cur, lc.DRAFT, actor_kind=actor_kind)
                repo_skills.set_version(self.conn, skill_id, version, lc.DRAFT)
                self._sync_skill(skill_id, lc.DRAFT)
            self._audit("evaluate", skill_id, version, actor,
                        f"eval={result.evaluation_id} set={result.eval_set_version} "
                        f"evaluator={result.evaluator_version} model={result.model_id} "
                        f"passed={int(result.passed)} {result.blocked_by}")
        return result

    # ----------------------------------------------------------------- approve

    def approve(self, skill_id: str, version: str, *, approved_by: str,
                actor_kind: str = lc.OPERATOR) -> lc.VersionState:
        """A named human signs off on the current content_hash.

        Refused unless the version is ``evaluated`` with a passing evaluation on
        exactly these bytes, and the approver is an authorized human. The
        approval row stores the hash, which is what makes a later content edit
        invalidate it without deleting anything.
        """
        self._require_actor(approved_by, actor_kind, lc.APPROVED)
        who = approved_by.strip()
        if who.lower() in NON_HUMAN_IDENTITIES:
            raise StoreError("unauthorized_approver",
                             f"'{approved_by}' is not a human identity; an "
                             f"approval must name a person who reviewed the "
                             f"bytes")
        if self.allowed_approvers is not None and \
                who not in self.allowed_approvers:
            raise StoreError(
                "unauthorized_approver",
                f"'{who}' is not in the configured approver allowlist "
                f"{sorted(self.allowed_approvers)}")
        cur = self.state(skill_id, version)
        if cur.status != lc.EVALUATED:
            raise lc.IllegalTransition(
                cur.status, lc.APPROVED,
                "only an evaluated version can be approved")
        eval_row = repo_skills.latest_evaluation(self.conn, skill_id, version)
        if eval_row is None or eval_row["content_hash"] != cur.content_hash:
            raise StoreError("not_evaluated",
                             f"no evaluation exists for "
                             f"{cur.content_hash}; approve refused")
        if not int(eval_row["passed"]):
            raise StoreError("evaluation_failed",
                             "the latest evaluation did not pass; approve "
                             "refused")
        row = repo_skills.get_version(self.conn, skill_id, version)
        author = str(json.loads(row["provenance_json"] or "{}")
                     .get("authored_by", "")).strip()
        if author and author.lower() == who.lower():
            raise StoreError("unauthorized_approver",
                             f"'{who}' authored this artifact; self-approval "
                             f"is refused")
        with immediate(self.conn):
            repo_skills.add_approval(self.conn, approval_id=_new_id("appr"),
                                     skill_id=skill_id, version=version,
                                     content_hash=cur.content_hash,
                                     approved_by=who)
            nxt = lc.transition(self.state(skill_id, version), lc.APPROVED,
                                actor_kind=actor_kind)
            repo_skills.set_version(self.conn, skill_id, version, nxt.status)
            self._sync_skill(skill_id, nxt.status)
            self._audit("approve", skill_id, version, who,
                        f"hash={cur.content_hash}")
        return self.state(skill_id, version)


    # ------------------------------------------------------- activate / deploy

    def activate(self, skill_id: str, version: str, *, operator: str,
                 actor_kind: str = lc.OPERATOR,
                 eval_set: Optional[ev.EvalSet] = None
                 ) -> dict:
        """Deploy this version. The only path to ``active``.

        Every gate is re-checked from stored rows at this moment rather than
        trusted from an earlier call: the approval must exist *for the current
        hash*, the evaluation must be for the current hash and passed, and the
        safety regression check runs against the previous evaluation. So an
        artifact edited between approval and activation cannot slip through.
        """
        self._require_actor(operator, actor_kind, lc.ACTIVE)
        cur = self.state(skill_id, version)
        if cur.status != lc.APPROVED:
            raise lc.IllegalTransition(
                cur.status, lc.ACTIVE,
                "activation requires an approved version whose approval "
                "matches the current content hash")
        if not cur.has_valid_approval:
            raise StoreError(
                "approval_invalidated",
                f"the newest approval for {skill_id}@{version} is for "
                f"'{cur.approval_hash or '<none>'}' but the artifact is "
                f"'{cur.content_hash}'; content changed after approval")
        eval_row = repo_skills.latest_evaluation(self.conn, skill_id, version)
        if eval_row is None:
            raise StoreError("not_evaluated", f"{skill_id}@{version} has no "
                                              f"evaluation; cannot activate")
        result = ev.result_from_row(eval_row)
        prev_row = repo_skills.previous_evaluation_before(
            self.conn, skill_id, version, result.evaluation_id)
        previous = ev.result_from_row(prev_row) if prev_row else None
        if eval_set is not None:
            ev.assert_activatable(result, content_hash=cur.content_hash,
                                  eval_set=eval_set, previous=previous)
        else:
            if result.content_hash != cur.content_hash:
                raise StoreError("evaluation_hash_mismatch",
                                 "the stored evaluation is for different bytes")
            if not result.passed:
                raise ev.ActivationRefused(
                    "evaluation_failed",
                    result.blocked_by or "stored evaluation did not pass")
            regressed = ev.safety_regressions(previous, result.per_case) \
                if previous else ()
            if regressed:
                raise ev.ActivationRefused(
                    "safety_regression",
                    f"safety case(s) {list(regressed)} regressed since "
                    f"{previous.evaluation_id}")
        row = repo_skills.get_version(self.conn, skill_id, version)
        scope = self._manifest_of(row).scope
        with immediate(self.conn):
            # supersede whatever is live now: the old row stays, flipped to
            # active=0 with no rolled_back_at, which is what marks it as the
            # version a rollback would restore. Its version row goes back to
            # 'approved' -- still signed off, no longer in force.
            previous_live = repo_skills.active_deployment(self.conn, skill_id)
            repo_skills.deactivate_deployment(self.conn, skill_id)
            if previous_live and previous_live["version"] != version:
                prev_state = self.state(skill_id, previous_live["version"])
                if prev_state.status == lc.ACTIVE:
                    repo_skills.set_version(self.conn, skill_id,
                                            previous_live["version"],
                                            lc.APPROVED)
            dep_id = _new_id("dep")
            repo_skills.add_deployment(self.conn, deployment_id=dep_id,
                                       skill_id=skill_id, version=version,
                                       content_hash=cur.content_hash,
                                       scope=scope, created_by=operator)
            nxt = lc.transition(cur, lc.ACTIVE, actor_kind=actor_kind)
            repo_skills.set_version(self.conn, skill_id, version, nxt.status)
            repo_skills.upsert_skill(self.conn, skill_id, lc.ACTIVE,
                                     current_version=version)
            self._audit("activate", skill_id, version, operator,
                        f"deployment={dep_id} hash={cur.content_hash}")
        return {"deployment_id": dep_id, "skill_id": skill_id,
                "version": version, "content_hash": cur.content_hash,
                "scope": scope}

    # ------------------------------------------------------------------ retire

    def retire(self, skill_id: str, version: str, *, operator: str,
               actor_kind: str = lc.OPERATOR) -> lc.VersionState:
        """Terminal for this version. Closes its deployment if it was live, so
        an artifact cannot stay in force after being retired."""
        self._require_actor(operator, actor_kind, lc.RETIRED)
        cur = self.state(skill_id, version)
        nxt = lc.transition(cur, lc.RETIRED, actor_kind=actor_kind)
        with immediate(self.conn):
            if cur.status == lc.ACTIVE:
                repo_skills.deactivate_deployment(self.conn, skill_id)
            repo_skills.set_version(self.conn, skill_id, version, nxt.status)
            self._sync_skill(skill_id, lc.RETIRED)
            self._audit("retire", skill_id, version, operator, "")
        return self.state(skill_id, version)


    # ---------------------------------------------------------------- rollback

    def rollback(self, skill_id: str, *, operator: str,
                 actor_kind: str = lc.OPERATOR) -> dict:
        """Restore the previous approved version and record that it happened.

        The target is the last deployment that was superseded (active=0, not
        rolled back). It is only restored if it is *still* valid: an approval
        for its current hash and a passing evaluation for its current hash. A
        rollback is not an escape hatch from the gates — if the old artifact was
        since edited or its approval withdrawn, the rollback is refused and the
        operator has to re-approve explicitly.

        Auditability: the superseded deployment row keeps its identity and gets
        ``rolled_back_at`` stamped, and a NEW deployment row is inserted for the
        restored version. So ``skill_deployments`` reads as a timeline: what was
        live, when it was replaced, when it was rolled back, and what replaced
        it.
        """
        self._require_actor(operator, actor_kind, lc.ACTIVE)
        live = repo_skills.active_deployment(self.conn, skill_id)
        if live is None:
            raise StoreError("nothing_active",
                             f"no active deployment for {skill_id}")
        history = [d for d in repo_skills.list_deployments(self.conn, skill_id)
                   if d["deployment_id"] != live["deployment_id"]
                   and not int(d["active"]) and not d["rolled_back_at"]]
        if not history:
            raise StoreError("no_previous_version",
                             f"{skill_id} has no previous deployment to roll "
                             f"back to")
        target = history[-1]
        tstate = self.state(skill_id, target["version"])
        if tstate.status == lc.RETIRED:
            raise StoreError("rollback_target_retired",
                             f"{skill_id}@{tstate.version} is retired and "
                             f"cannot be restored")
        if not tstate.has_valid_approval:
            raise StoreError(
                "rollback_refused",
                f"{skill_id}@{tstate.version} has no approval for its current "
                f"hash ({tstate.approval_hash or '<none>'} vs "
                f"{tstate.content_hash}); re-approve instead of rolling back")
        eval_row = repo_skills.latest_evaluation(self.conn, skill_id,
                                                 tstate.version)
        if eval_row is None or eval_row["content_hash"] != tstate.content_hash \
                or not int(eval_row["passed"]):
            raise StoreError("rollback_refused",
                             f"{skill_id}@{tstate.version} has no passing "
                             f"evaluation for its current hash")
        stamp = utcnow_s()
        with immediate(self.conn):
            repo_skills.deactivate_deployment(self.conn, skill_id,
                                              rolled_back_at=stamp)
            dep_id = _new_id("dep")
            row = repo_skills.get_version(self.conn, skill_id, tstate.version)
            repo_skills.add_deployment(
                self.conn, deployment_id=dep_id, skill_id=skill_id,
                version=tstate.version, content_hash=tstate.content_hash,
                scope=self._manifest_of(row).scope, created_by=operator)
            lc.transition(self.state(skill_id, live["version"]), lc.APPROVED,
                          actor_kind=actor_kind)
            repo_skills.set_version(self.conn, skill_id, live["version"],
                                    lc.APPROVED)
            repo_skills.set_version(self.conn, skill_id, tstate.version,
                                    lc.ACTIVE)
            repo_skills.upsert_skill(self.conn, skill_id, lc.ACTIVE,
                                     current_version=tstate.version)
            self._audit("rollback", skill_id, tstate.version, operator,
                        f"restored {tstate.version} (from "
                        f"{live['version']}), superseded_deployment="
                        f"{live['deployment_id']} at {stamp}")
        return {"deployment_id": dep_id, "skill_id": skill_id,
                "restored_version": tstate.version,
                "replaced_version": live["version"],
                "superseded_deployment_id": live["deployment_id"],
                "rolled_back_at": stamp}

    # ------------------------------------------------------------- read helpers

    def active(self) -> dict[str, dict]:
        """skill_id -> {version, content_hash, scope} currently in force."""
        return repo_skills.active_skill_versions(self.conn)

    def deployments(self, skill_id: str) -> list[dict]:
        return repo_skills.list_deployments(self.conn, skill_id)

    def approvals(self, skill_id: str) -> list[dict]:
        return repo_skills.list_approvals(self.conn, skill_id)

    def record_dispatch_skills(self, *, task_id: str, workflow_id: str,
                               phase: str = "dispatch") -> dict[str, dict]:
        """Stamp the live skill versions onto a task's dispatch/result history.

        Returns the mapping it recorded, so the caller can also put it in the
        dispatch brief that travels over the bus (the executor never reads the
        DB). Post-mortems attribute outcomes to these rows instead of to
        whatever happens to be current when the report is generated.
        """
        active = self.active()
        repo_skills.record_task_skills(
            self.conn, task_id=task_id, workflow_id=workflow_id,
            phase=phase, active=active)
        return active
