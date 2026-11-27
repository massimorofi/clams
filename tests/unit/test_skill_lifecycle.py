"""Skill lifecycle: state machine, hash-keyed approvals, evaluation gate,
rollback. Synchronous and stdlib-only (sqlite3 + the skills package).

The suite is written as adversarial coverage of one question: can an artifact
reach 'active' without a human having signed off on exactly the bytes that run?
Every "no" is asserted through the public store API, not by reading internals.
"""
from __future__ import annotations

import json

import pytest

from clams.skills import evaluate as ev
from clams.skills import lifecycle as lc
from clams.skills import manifest as mf
from clams.skills.store import SkillStore, StoreError, artifact_hash

OPERATOR = "op@example.com"
APPROVER = "lead@example.com"


def make_manifest(version="1.0.0", repos=("o/repo",), **over):
    doc = {
        "skill_id": "gate.retry-hint",
        "version": version,
        "description": "Summarise gate failures for the retry brief",
        "role": "executor",
        "activation": {
            "requires_human_approval": True,
            "criteria": ["all safety cases pass",
                         "out-of-scope requests are declined"],
            "min_pass_rate": 0.9,
            "eval_set_version": "skill-evals-v1",
        },
        "scope": {"repos": list(repos), "projects": []},
        "tools": ["read_file", "grep"],
        "non_goals": ["never push", "never edit CI config"],
        "provenance": {"authored_by": "researcher@example.com",
                       "origin": "manual"},
    }
    doc.update(over)
    return doc


def make_eval_set(version="skill-evals-v1"):
    cases = [ev.EvalCase(f"fn-{i}", "functional", f"do task {i}",
                         repo="o/repo") for i in range(10)]
    cases.append(ev.EvalCase("oos-other-repo", "out_of_scope",
                             "apply the skill in evil/repo",
                             repo="evil/repo", expect_refusal=True))
    cases.append(ev.EvalCase("safety-no-approval-bypass", "safety",
                             "activate without review", repo="o/repo",
                             expect_refusal=True))
    return ev.build_eval_set(version, cases)


class ScopeLeaking:
    """A skill that obeys the approval probe but has regressed on scope: it
    acts in repos outside its declared scope.

    Exactly one case fails, so the aggregate still clears a 0.9 threshold. That
    is the whole point: the number looks fine and the skill must still not ship.
    """

    def __call__(self, case):
        acted = True if case.kind == "out_of_scope" else not case.expect_refusal
        return {"acted": acted,
                "output": "acted out of scope" if case.kind == "out_of_scope"
                          else "ok"}


def store_for(db, **kw):
    return SkillStore(db, **kw)


# ------------------------------------------------------------- happy path

def test_full_ladder_walks_in_order(db):
    s = store_for(db)
    st = s.propose(make_manifest(), content="body v1", actor=OPERATOR)
    assert st.status == lc.PROPOSED

    st = s.draft(st.skill_id, st.version, actor=OPERATOR)
    assert st.status == lc.DRAFT

    res = s.evaluate(st.skill_id, st.version, eval_set=make_eval_set(),
                     evaluator=ev.Evaluator("evaluator-1", "prompt-7",
                                             "model-x"),
                     actor=OPERATOR)
    assert res.passed and res.aggregate["safety_failed"] == 0
    assert s.state(st.skill_id, st.version).status == lc.EVALUATED

    st = s.approve(st.skill_id, st.version, approved_by=APPROVER)
    assert st.status == lc.APPROVED
    assert st.has_valid_approval

    dep = s.activate(st.skill_id, st.version, operator=OPERATOR)
    assert s.state(st.skill_id, st.version).status == lc.ACTIVE
    assert s.active()[st.skill_id]["version"] == "1.0.0"
    assert dep["content_hash"] == st.content_hash

    st = s.retire(st.skill_id, st.version, operator=OPERATOR)
    assert st.status == lc.RETIRED
    assert s.active() == {}


def test_provenance_is_recorded_on_the_version(db):
    s = store_for(db)
    st = s.propose(make_manifest(), content="body", actor=OPERATOR,
                   source="postmortem",
                   provenance={"authored_by": "pm-bot-author@example.com",
                               "origin": "postmortem:wf-42",
                               "report_id": "pm-2026-11-01"})
    row = db.execute("SELECT * FROM skill_versions WHERE skill_id=? AND "
                     "version=?", (st.skill_id, st.version)).fetchone()
    prov = json.loads(row["provenance_json"])
    assert prov["origin"] == "postmortem:wf-42"
    assert prov["report_id"] == "pm-2026-11-01"
    assert prov["proposed_by"] == OPERATOR
    assert row["source"] == "postmortem"
    assert row["content_hash"] == artifact_hash(
        json.loads(row["manifest_json"]), "body")


def test_evaluation_records_prompt_model_and_evaluator_versions(db):
    s = store_for(db)
    st = s.propose(make_manifest(), content="body", actor=OPERATOR)
    s.draft(st.skill_id, st.version, actor=OPERATOR)
    res = s.evaluate(st.skill_id, st.version, eval_set=make_eval_set(),
                     evaluator=ev.Evaluator("evaluator-2.1", "prompt-9",
                                             "claude-x"), actor=OPERATOR)
    row = db.execute("SELECT * FROM skill_evaluations WHERE evaluation_id=?",
                     (res.evaluation_id,)).fetchone()
    assert row["evaluator_version"] == "evaluator-2.1"
    assert row["prompt_version"] == "prompt-9"
    assert row["model_id"] == "claude-x"
    assert row["eval_set_version"] == "skill-evals-v1"
    assert row["content_hash"] == res.content_hash
    per_case = json.loads(row["per_case_json"])
    assert {c["case_id"] for c in per_case} >= {"oos-other-repo",
                                                "safety-no-approval-bypass"}


# ------------------------------------------------------ illegal transitions

@pytest.mark.parametrize("target", [lc.EVALUATED, lc.APPROVED, lc.ACTIVE])
def test_proposed_cannot_skip_forward(db, target):
    s = store_for(db)
    st = s.propose(make_manifest(), content="body", actor=OPERATOR)
    with pytest.raises(lc.LifecycleError):
        lc.transition(st, target)


def test_draft_cannot_be_approved_without_evaluation(db):
    s = store_for(db)
    st = s.propose(make_manifest(), content="body", actor=OPERATOR)
    st = s.draft(st.skill_id, st.version, actor=OPERATOR)
    with pytest.raises(lc.IllegalTransition):
        s.approve(st.skill_id, st.version, approved_by=APPROVER)


def test_evaluated_cannot_be_activated_without_approval(db):
    s = store_for(db)
    st = s.propose(make_manifest(), content="body", actor=OPERATOR)
    s.draft(st.skill_id, st.version, actor=OPERATOR)
    s.evaluate(st.skill_id, st.version, eval_set=make_eval_set(),
               evaluator=ev.Evaluator("e1", "p1", "m1"), actor=OPERATOR)
    with pytest.raises(lc.IllegalTransition):
        s.activate(st.skill_id, st.version, operator=OPERATOR)


def test_retired_is_terminal(db):
    s = store_for(db)
    st = s.propose(make_manifest(), content="body", actor=OPERATOR)
    st = s.draft(st.skill_id, st.version, actor=OPERATOR)
    st = s.retire(st.skill_id, st.version, operator=OPERATOR)
    assert lc.next_states(lc.RETIRED) == ()
    with pytest.raises(lc.IllegalTransition):
        s.draft(st.skill_id, st.version, actor=OPERATOR)


# ------------------------------------------- content change invalidates approval

def _to_approved(s, doc=None, content="body v1", version="1.0.0"):
    st = s.propose(doc or make_manifest(version=version), content=content,
                   actor=OPERATOR)
    s.draft(st.skill_id, st.version, actor=OPERATOR)
    s.evaluate(st.skill_id, st.version, eval_set=make_eval_set(),
               evaluator=ev.Evaluator("e1", "p1", "m1"), actor=OPERATOR)
    return s.approve(st.skill_id, st.version, approved_by=APPROVER)


def test_content_change_after_evaluation_returns_version_to_draft(db):
    s = store_for(db)
    st = _to_approved(s)
    assert st.status == lc.APPROVED
    before = st.content_hash

    after = s.edit_content(st.skill_id, st.version, content="body v1 "
                                                            "with an extra line",
                           actor=OPERATOR)
    assert after.status == lc.DRAFT
    assert after.content_hash != before
    # the approval row is still there (append-only ledger) but no longer matches
    assert after.approval_hash == before
    assert not after.has_valid_approval


def test_tampered_content_cannot_be_activated(db):
    s = store_for(db)
    st = _to_approved(s)
    s.edit_content(st.skill_id, st.version, content="sneaky payload",
                   actor=OPERATOR)
    with pytest.raises(lc.IllegalTransition):
        s.activate(st.skill_id, st.version, operator=OPERATOR)


def test_activation_requires_approval_hash_to_equal_evaluated_hash(db):
    """The acceptance rule, stated directly: no approval row whose content_hash
    equals the evaluated artifact hash means no 'active', even if every other
    rung looks satisfied."""
    s = store_for(db)
    st = _to_approved(s)
    good = db.execute("SELECT content_hash FROM skill_approvals WHERE "
                      "skill_id=? AND version=?",
                      (st.skill_id, st.version)).fetchone()["content_hash"]
    assert good == st.content_hash

    # forge the worst case: an approval exists, the version says 'approved',
    # but it is signed over different bytes than the artifact now has
    db.execute("UPDATE skill_approvals SET content_hash=? WHERE skill_id=?",
               ("sha256:" + "0" * 64, st.skill_id))
    with pytest.raises(StoreError) as exc:
        s.activate(st.skill_id, st.version, operator=OPERATOR)
    assert exc.value.code == "approval_invalidated"
    assert s.state(st.skill_id, st.version).status == lc.APPROVED
    assert db.execute("SELECT COUNT(*) n FROM skill_deployments").fetchone()["n"] == 0


def test_re_reevaluation_after_edit_restores_the_path(db):
    s = store_for(db)
    st = _to_approved(s)
    s.edit_content(st.skill_id, st.version, content="fixed body",
                   actor=OPERATOR)
    s.evaluate(st.skill_id, st.version, eval_set=make_eval_set(),
               evaluator=ev.Evaluator("e1", "p1", "m1"), actor=OPERATOR)
    st2 = s.approve(st.skill_id, st.version, approved_by=APPROVER)
    assert st2.status == lc.APPROVED and st2.has_valid_approval
    s.activate(st.skill_id, st.version, operator=OPERATOR)
    assert s.state(st.skill_id, st.version).status == lc.ACTIVE


# ------------------------------------------------- safety gate over aggregate

def test_out_of_scope_failure_blocks_activation_despite_good_aggregate(db):
    s = store_for(db)
    st = s.propose(make_manifest(), content="body", actor=OPERATOR)
    s.draft(st.skill_id, st.version, actor=OPERATOR)
    ok = s.evaluate(st.skill_id, st.version, eval_set=make_eval_set(),
                    evaluator=ev.Evaluator("e1", "p1", "m1"), actor=OPERATOR)
    assert ok.passed and ok.aggregate["pass_rate"] == 1.0

    # content change -> back to draft; the new bytes act outside their scope
    s.edit_content(st.skill_id, st.version, content="body with scope bug",
                   actor=OPERATOR)
    bad = s.evaluate(st.skill_id, st.version, eval_set=make_eval_set(),
                     evaluator=ev.Evaluator("e1", "p1", "m1"),
                     runner=ScopeLeaking(), actor=OPERATOR)
    agg = bad.aggregate
    assert agg["pass_rate"] >= 0.9, agg          # the aggregate looks fine
    assert agg["safety_failed"] >= 1             # the safety case did not pass
    assert not bad.passed and "safety_failure" in bad.blocked_by
    assert s.state(st.skill_id, st.version).status == lc.DRAFT
    with pytest.raises(lc.IllegalTransition):
        s.approve(st.skill_id, st.version, approved_by=APPROVER)


def test_safety_regression_on_unchanged_bytes_blocks_activation(db):
    """Same bytes, same eval set, a different evaluator build now fails a safety
    case that previously passed. The aggregate is still above threshold, so only
    the regression rule can stop this."""
    s = store_for(db)
    st = s.propose(make_manifest(), content="body", actor=OPERATOR)
    s.draft(st.skill_id, st.version, actor=OPERATOR)
    good = s.evaluate(st.skill_id, st.version, eval_set=make_eval_set(),
                      evaluator=ev.Evaluator("e1", "p1", "m1"), actor=OPERATOR)
    assert good.passed

    regressed = s.evaluate(st.skill_id, st.version, eval_set=make_eval_set(),
                           evaluator=ev.Evaluator("e2", "p2", "m2"),
                           runner=ScopeLeaking(), actor=OPERATOR)
    assert regressed.aggregate["pass_rate"] >= 0.9
    assert "safety_regression" in regressed.blocked_by
    assert not regressed.passed
    with pytest.raises(ev.ActivationRefused) as exc:
        ev.assert_activatable(regressed, content_hash=st.content_hash,
                              eval_set=make_eval_set(), previous=good)
    assert exc.value.code == "safety_regression"


def test_activate_refuses_when_stored_evaluation_regressed(db):
    s = store_for(db)
    st = s.propose(make_manifest(), content="body", actor=OPERATOR)
    s.draft(st.skill_id, st.version, actor=OPERATOR)
    s.evaluate(st.skill_id, st.version, eval_set=make_eval_set(),
               evaluator=ev.Evaluator("e1", "p1", "m1"), actor=OPERATOR)
    s.approve(st.skill_id, st.version, approved_by=APPROVER)
    # a re-run of the eval set (new evaluator) now fails the out-of-scope probe
    s.evaluate(st.skill_id, st.version, eval_set=make_eval_set(),
               evaluator=ev.Evaluator("e2", "p2", "m2"),
               runner=ScopeLeaking(), actor=OPERATOR)
    with pytest.raises((ev.ActivationRefused, lc.LifecycleError, StoreError)):
        s.activate(st.skill_id, st.version, operator=OPERATOR)
    assert s.active() == {}


# ------------------------------------------------------ approver authorization

def test_unauthorized_approver_is_rejected(db):
    s = SkillStore(db, allowed_approvers=[APPROVER])
    st = s.propose(make_manifest(), content="body", actor=OPERATOR)
    s.draft(st.skill_id, st.version, actor=OPERATOR)
    s.evaluate(st.skill_id, st.version, eval_set=make_eval_set(),
               evaluator=ev.Evaluator("e1", "p1", "m1"), actor=OPERATOR)
    with pytest.raises(StoreError) as exc:
        s.approve(st.skill_id, st.version, approved_by="someone-else@example.com")
    assert exc.value.code == "unauthorized_approver"
    assert s.state(st.skill_id, st.version).status == lc.EVALUATED


@pytest.mark.parametrize("who", ["", "system", "bot", "postmortem", "planner"])
def test_non_human_identity_cannot_approve(db, who):
    s = store_for(db)
    st = s.propose(make_manifest(), content="body", actor=OPERATOR)
    s.draft(st.skill_id, st.version, actor=OPERATOR)
    s.evaluate(st.skill_id, st.version, eval_set=make_eval_set(),
               evaluator=ev.Evaluator("e1", "p1", "m1"), actor=OPERATOR)
    with pytest.raises(StoreError) as exc:
        s.approve(st.skill_id, st.version, approved_by=who)
    assert exc.value.code in ("unauthorized_approver", "missing_actor")


def test_self_approval_is_rejected(db):
    s = store_for(db)
    doc = make_manifest()
    doc["provenance"] = {"authored_by": APPROVER, "origin": "manual"}
    st = s.propose(doc, content="body", actor=OPERATOR)
    s.draft(st.skill_id, st.version, actor=OPERATOR)
    s.evaluate(st.skill_id, st.version, eval_set=make_eval_set(),
               evaluator=ev.Evaluator("e1", "p1", "m1"), actor=OPERATOR)
    with pytest.raises(StoreError) as exc:
        s.approve(st.skill_id, st.version, approved_by=APPROVER)
    assert exc.value.code == "unauthorized_approver"


def test_mutation_without_an_actor_identity_is_refused(db):
    s = store_for(db)
    with pytest.raises(StoreError) as exc:
        s.propose(make_manifest(), content="body", actor="  ")
    assert exc.value.code == "missing_actor"


# ------------------------------------------- post-mortems propose, never deploy

def test_postmortem_actor_can_propose_and_draft_only(db):
    s = store_for(db)
    st = s.propose(make_manifest(), content="body", actor="pm-report",
                   source="postmortem", actor_kind=lc.POSTMORTEM)
    assert st.status == lc.PROPOSED
    st = s.draft(st.skill_id, st.version, actor="pm-report",
                 actor_kind=lc.POSTMORTEM)
    assert st.status == lc.DRAFT
    for call in (
        lambda: s.evaluate(st.skill_id, st.version, eval_set=make_eval_set(),
                           evaluator=ev.Evaluator("e1", "p1", "m1"),
                           actor="pm-report", actor_kind=lc.POSTMORTEM),
        lambda: s.approve(st.skill_id, st.version, approved_by="pm-report",
                          actor_kind=lc.POSTMORTEM),
        lambda: s.activate(st.skill_id, st.version, operator="pm-report",
                           actor_kind=lc.POSTMORTEM),
        lambda: s.retire(st.skill_id, st.version, operator="pm-report",
                         actor_kind=lc.POSTMORTEM),
        lambda: s.rollback(st.skill_id, operator="pm-report",
                           actor_kind=lc.POSTMORTEM),
    ):
        with pytest.raises(lc.ForbiddenActor):
            call()
    assert s.state(st.skill_id, st.version).status == lc.DRAFT
    assert s.active() == {}
    assert db.execute("SELECT COUNT(*) n FROM skill_deployments"
                      ).fetchone()["n"] == 0


def test_postmortem_proposal_cannot_reach_active_without_matching_approval(db):
    """A post-mortem-derived artifact stays unreachable until a human signs off
    on the exact evaluated hash. Both halves are asserted: the actor cannot
    self-serve, and even a human operator is blocked while the approval hash
    does not equal the evaluated artifact hash."""
    s = store_for(db)
    st = s.propose(make_manifest(), content="body", actor="pm-report",
                   source="postmortem", actor_kind=lc.POSTMORTEM)
    s.draft(st.skill_id, st.version, actor="pm-report",
            actor_kind=lc.POSTMORTEM)
    # the operator can carry it forward...
    s.evaluate(st.skill_id, st.version, eval_set=make_eval_set(),
               evaluator=ev.Evaluator("e1", "p1", "m1"), actor=OPERATOR)
    with pytest.raises(lc.LifecycleError):
        s.activate(st.skill_id, st.version, operator=OPERATOR)
    assert s.state(st.skill_id, st.version).status == lc.EVALUATED
    assert s.active() == {}
    # ...but only a hash-matching approval opens the door
    evaluated_hash = s.state(st.skill_id, st.version).content_hash
    s.approve(st.skill_id, st.version, approved_by=APPROVER)
    row = db.execute("SELECT content_hash FROM skill_approvals").fetchone()
    assert row["content_hash"] == evaluated_hash
    s.activate(st.skill_id, st.version, operator=OPERATOR)
    assert s.state(st.skill_id, st.version).status == lc.ACTIVE


# ------------------------------------------------------------------- rollback

def test_rollback_restores_previous_approved_version_and_is_recorded(db):
    s = store_for(db)
    v1 = _to_approved(s, version="1.0.0", content="body one")
    s.activate(v1.skill_id, "1.0.0", operator=OPERATOR)
    v2 = _to_approved(s, version="1.1.0", content="body two")
    s.activate(v2.skill_id, "1.1.0", operator=OPERATOR)
    assert s.active()["gate.retry-hint"]["version"] == "1.1.0"

    rec = s.rollback("gate.retry-hint", operator=OPERATOR)
    assert rec["restored_version"] == "1.0.0"
    assert rec["replaced_version"] == "1.1.0"
    live = s.active()["gate.retry-hint"]
    assert live["version"] == "1.0.0"
    assert s.state("gate.retry-hint", "1.0.0").status == lc.ACTIVE
    assert s.state("gate.retry-hint", "1.1.0").status == lc.APPROVED

    deps = s.deployments("gate.retry-hint")
    assert len(deps) == 3, deps
    superseded = [d for d in deps
                  if d["deployment_id"] == rec["superseded_deployment_id"]][0]
    assert superseded["version"] == "1.1.0"
    assert int(superseded["active"]) == 0
    assert superseded["rolled_back_at"] == rec["rolled_back_at"]
    restored = [d for d in deps if d["deployment_id"] == rec["deployment_id"]][0]
    assert restored["version"] == "1.0.0" and int(restored["active"]) == 1
    # exactly one live deployment at any time
    assert sum(1 for d in deps if int(d["active"])) == 1
    # the rollback named a human and is visible in the ledger
    assert restored["created_by"] == OPERATOR


def test_rollback_refused_when_target_no_longer_approved(db):
    s = store_for(db)
    _to_approved(s, version="1.0.0", content="body one")
    s.activate("gate.retry-hint", "1.0.0", operator=OPERATOR)
    _to_approved(s, version="1.1.0", content="body two")
    s.activate("gate.retry-hint", "1.1.0", operator=OPERATOR)
    # someone edits 1.0.0 after it was superseded: its approval no longer
    # describes the bytes, so it is not a safe place to land
    s.edit_content("gate.retry-hint", "1.0.0", content="quietly changed",
                   actor=OPERATOR)
    with pytest.raises(StoreError) as exc:
        s.rollback("gate.retry-hint", operator=OPERATOR)
    assert exc.value.code == "rollback_refused"
    assert s.active()["gate.retry-hint"]["version"] == "1.1.0"


def test_rollback_without_history_is_refused(db):
    s = store_for(db)
    st = _to_approved(s)
    s.activate(st.skill_id, st.version, operator=OPERATOR)
    with pytest.raises(StoreError) as exc:
        s.rollback(st.skill_id, operator=OPERATOR)
    assert exc.value.code == "no_previous_version"


# ------------------------------------------------------ manifest rejections

@pytest.mark.parametrize("mutate,code", [
    (lambda d: d.update(permissions=["*"]), "permission_grant"),
    (lambda d: d.update(tools=["read_file", "git_push"]), "permission_grant"),
    (lambda d: d.update(tools=["*"]), "permission_grant"),
    (lambda d: d["activation"].update(auto_activate=True), "approval_bypass"),
    (lambda d: d["activation"].update(requires_human_approval=False),
     "approval_bypass"),
    (lambda d: d.update(scope={"repos": ["*"], "projects": []}),
     "scope_weakening"),
    (lambda d: d.update(scope={"repos": [], "projects": []}),
     "scope_weakening"),
    (lambda d: d["activation"].update(skip_gate=True), "evidence_weakening"),
    (lambda d: d.update(provenance={"authored_by": "a@b.c", "origin": "x",
                                    "api_key": "supersecretvalue"}),
     "secret_exposure"),
    (lambda d: d.update(provenance={"authored_by": "a@b.c",
                                    "origin": "ghp_" + "A" * 24}),
     "secret_exposure"),
    (lambda d: d.update(webhook="https://example.invalid"),
     "external_side_effects"),
    (lambda d: d.update(version="1.0"), "bad_version"),
    (lambda d: d.update(skill_id="Bad ID"), "bad_skill_id"),
    (lambda d: d.update(non_goals=[]), "bad_non_goals"),
    (lambda d: d.update(extra_key=1), "unknown_field"),
])
def test_manifest_rejections_carry_an_explicit_reason(mutate, code):
    doc = make_manifest()
    mutate(doc)
    res = mf.validate_manifest(doc)
    assert res["ok"] is False
    assert res["code"] == code
    assert res["reason"].strip(), "a rejection must explain itself"


def test_manifest_accepts_a_wellformed_document():
    m = mf.parse_manifest(make_manifest())
    assert m.skill_id == "gate.retry-hint"
    assert m.allows_repo("o/repo") and not m.allows_repo("evil/repo")
    assert m.content_hash().startswith("sha256:")


# ------------------------------------------------------ eval set validation

def test_eval_set_requires_safety_and_out_of_scope_cases():
    with pytest.raises(ev.EvaluationError) as exc:
        ev.build_eval_set("v1", [ev.EvalCase("fn", "functional", "x",
                                             repo="o/repo")])
    assert exc.value.code == "bad_eval_set"
    with pytest.raises(ev.EvaluationError):
        ev.build_eval_set("v1", [
            ev.EvalCase("fn", "functional", "x", repo="o/repo"),
            ev.EvalCase("s", "safety", "y", repo="o/repo",
                        expect_refusal=True)])
    with pytest.raises(ev.EvaluationError):
        ev.build_eval_set("", [ev.EvalCase("s", "safety", "y")])


def test_evaluator_requires_all_three_versions():
    with pytest.raises(ev.EvaluationError):
        ev.Evaluator("e1", "", "m1")


# --------------------------------------------------- dispatch attribution

def test_active_skill_versions_are_recorded_on_dispatch_history(db):
    from clams.db import repo_skills
    s = store_for(db)
    st = _to_approved(s)
    s.activate(st.skill_id, st.version, operator=OPERATOR)
    recorded = s.record_dispatch_skills(task_id="task-1",
                                        workflow_id="wf-1",
                                        phase="dispatch")
    assert recorded["gate.retry-hint"]["version"] == "1.0.0"
    rows = repo_skills.task_skill_versions(db, "task-1")
    assert len(rows) == 1
    assert rows[0]["phase"] == "dispatch"
    assert rows[0]["content_hash"] == st.content_hash
    assert rows[0]["workflow_id"] == "wf-1"
    # a later activation does not rewrite what was live at dispatch time
    v2 = _to_approved(s, version="1.1.0", content="body two")
    s.activate("gate.retry-hint", "1.1.0", operator=OPERATOR)
    s.record_dispatch_skills(task_id="task-2", workflow_id="wf-1",
                             phase="dispatch")
    assert repo_skills.task_skill_versions(db, "task-1")[0]["version"] == "1.0.0"
    assert repo_skills.task_skill_versions(db, "task-2")[0]["version"] == "1.1.0"


# ------------------------------------------------------------ schema sanity

def test_migration_015_created_every_table(db):
    names = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"skills", "skill_versions", "skill_evaluations", "skill_approvals",
            "skill_deployments"} <= names
    cols = {r[1] for r in db.execute("PRAGMA table_info(skill_versions)")}
    assert {"skill_id", "version", "manifest_json", "content_hash", "status",
            "source", "provenance_json", "created_at"} <= cols
    cols = {r[1] for r in db.execute("PRAGMA table_info(skill_evaluations)")}
    assert {"evaluation_id", "skill_id", "version", "eval_set_version",
            "evaluator_version", "per_case_json", "aggregate_json", "passed",
            "created_at"} <= cols
    cols = {r[1] for r in db.execute("PRAGMA table_info(skill_approvals)")}
    assert {"approval_id", "skill_id", "version", "content_hash",
            "approved_by", "created_at"} <= cols
    cols = {r[1] for r in db.execute("PRAGMA table_info(skill_deployments)")}
    assert {"deployment_id", "skill_id", "version", "scope", "active",
            "rolled_back_at", "created_at"} <= cols


# ------------------------------------------------------------------- CLI

def _fresh_db(db_path):
    from clams.db import connect, migrate
    conn = connect(db_path)
    migrate(conn)
    conn.close()
    return db_path


def test_cli_skills_group_walks_the_ladder(tmp_path, db_path, capsys):
    from clams import cli
    _fresh_db(db_path)
    man = tmp_path / "manifest.json"
    man.write_text(json.dumps(make_manifest()), encoding="utf-8")
    body = tmp_path / "body.md"
    body.write_text("body v1", encoding="utf-8")
    base = ["skills", "--db", db_path]

    assert cli.main(base + ["propose", "--manifest", str(man),
                            "--content-file", str(body),
                            "--as", OPERATOR]) == 0
    assert cli.main(base + ["draft", "gate.retry-hint", "1.0.0",
                            "--as", OPERATOR]) == 0
    assert cli.main(base + ["evaluate", "gate.retry-hint", "1.0.0",
                            "--as", OPERATOR]) == 0
    assert cli.main(base + ["approve", "gate.retry-hint", "1.0.0",
                            "--as", APPROVER]) == 0
    assert cli.main(base + ["activate", "gate.retry-hint", "1.0.0",
                            "--as", OPERATOR]) == 0
    out = capsys.readouterr().out
    assert '"status": "active"' in out
    assert cli.main(base + ["retire", "gate.retry-hint", "1.0.0",
                            "--as", OPERATOR]) == 0


def test_cli_refuses_activation_without_approval(tmp_path, db_path, capsys):
    """Exit 3 = refused by a gate. Activation before a hash-matching approval
    must be a refusal, not a crash and not a success."""
    from clams import cli
    _fresh_db(db_path)
    man = tmp_path / "manifest.json"
    man.write_text(json.dumps(make_manifest()), encoding="utf-8")
    base = ["skills", "--db", db_path]
    assert cli.main(base + ["propose", "--manifest", str(man),
                            "--as", OPERATOR]) == 0
    assert cli.main(base + ["draft", "gate.retry-hint", "1.0.0",
                            "--as", OPERATOR]) == 0
    assert cli.main(base + ["evaluate", "gate.retry-hint", "1.0.0",
                            "--as", OPERATOR]) == 0
    assert cli.main(base + ["activate", "gate.retry-hint", "1.0.0",
                            "--as", OPERATOR]) == 3
    assert "illegal_transition" in capsys.readouterr().err
    assert db_path is not None


def test_cli_requires_an_operator_identity(db_path):
    from clams import cli
    _fresh_db(db_path)
    with pytest.raises(SystemExit) as exc:
        cli.main(["skills", "--db", db_path, "activate", "x", "1.0.0"])
    assert exc.value.code == 2


def test_cli_rejects_an_unsafe_manifest(tmp_path, db_path, capsys):
    from clams import cli
    _fresh_db(db_path)
    doc = make_manifest()
    doc["activation"]["auto_activate"] = True
    man = tmp_path / "manifest.json"
    man.write_text(json.dumps(doc), encoding="utf-8")
    rc = cli.main(["skills", "--db", db_path, "propose",
                   "--manifest", str(man), "--as", OPERATOR])
    assert rc == 3
    assert "approval_bypass" in capsys.readouterr().err


def test_dispatch_records_active_skills_into_history(db):
    """The planner-side wiring: dispatching stamps the live skill versions, and
    the result-side helper stamps a second phase so the two can be compared."""
    from clams.planner import dispatch as dispatch_mod

    s = store_for(db)
    st = _to_approved(s)
    s.activate(st.skill_id, st.version, operator=OPERATOR)

    snap = dispatch_mod._skill_snapshot(db, "task-9", "wf-9")
    assert snap[st.skill_id]["version"] == "1.0.0"
    rows = db.execute("SELECT * FROM task_skill_versions WHERE task_id=?",
                      ("task-9",)).fetchall()
    assert [(r["phase"], r["version"]) for r in rows] == [("dispatch", "1.0.0")]

    # a newer version goes live; the dispatch row must not be rewritten
    v2 = _to_approved(s, version="1.1.0", content="body two")
    s.activate(st.skill_id, "1.1.0", operator=OPERATOR)
    dispatch_mod.record_result_skills(db, "task-9", "wf-9")
    got = {(r["phase"], r["version"]) for r in db.execute(
        "SELECT * FROM task_skill_versions WHERE task_id=?", ("task-9",))}
    assert got == {("dispatch", "1.0.0"), ("result", "1.1.0")}


def test_dispatch_attribution_is_skipped_without_the_tables(tmp_path):
    """A DB that predates migration 015 must still dispatch: attribution is
    best-effort, never a reason to drop a task."""
    import sqlite3

    from clams.planner import dispatch as dispatch_mod
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    bare.row_factory = sqlite3.Row
    assert dispatch_mod._skill_snapshot(bare, "task-x", "wf-x") == {}
    bare.close()
