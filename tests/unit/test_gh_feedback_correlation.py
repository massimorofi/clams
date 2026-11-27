"""The GitHub feedback route: correlate, classify, dedupe, route.

Synchronous, stdlib-only (plus the project's own modules) against a temporary
SQLite file from the shared ``db`` fixture - no network, no Redis, no FastAPI.

Every test asserts a *route* property, not an implementation detail:

* correlation happens only through a stored repo+branch/PR+SHA mapping,
* an event whose head SHA is not the current one cannot dispatch or restyle,
* a redelivered delivery or a planner restart yields one event row and at most
  one follow-up / outbox side effect,
* a failed check becomes an operator handoff unless the project opted in,
* infrastructure/configuration failures are their own class,
* a green GitHub check is never Clams gate evidence.
"""
from __future__ import annotations

import json

from clams.db import (repo_github_events, repo_projects, repo_pr_links,
                      repo_tasks, repo_workflows)
from clams.github.webhooks import NormalizedEvent
from clams.planner import github_feedback

REPO = "o/r"
SHA_A = "a" * 40
SHA_B = "b" * 40
BRANCH = "claudio/task-1"
FAIL_SUMMARY = "check_suite completed conclusion=failure runs=2 PR #7"
PASS_SUMMARY = "check_suite completed conclusion=success runs=2 PR #7"
INFRA_SUMMARY = "check_suite completed conclusion=timed_out runs=1 PR #7"


# --------------------------------------------------------------------- fixtures
def _wf_task(db, *, status="running"):
    """One project -> workflow -> running task with a real brief."""
    pid = repo_projects.upsert_project(db, REPO)
    wf = repo_workflows.create_workflow(db, pid, "adhoc", "goal")
    brief = {"objective": "ship it", "acceptance": "tests pass",
             "run": ["pytest -q"], "writable": ["src/**"],
             "forbidden": [".env"], "repo": REPO, "base_ref": "main",
             "max_turns": 40}
    tid = repo_tasks.create_task(db, wf, 1, "implement feature", "ship it",
                                 "tests pass", brief)
    for step in ("ready", "dispatched", "running"):
        repo_tasks.transition(db, tid, step)
    repo_workflows.set_status(db, wf, status)
    return wf, tid


def _link(db, wf, tid, *, sha=SHA_A, pr=7, ref=BRANCH):
    """The explicit stored mapping - the only thing that can correlate."""
    return repo_pr_links.link_task_commit(
        db, workflow_id=wf, task_id=tid, repo=REPO, head_sha=sha,
        head_ref=ref, pr_number=pr)


def _ev(delivery="d-1", *, event="check_suite", action="completed",
        sha=SHA_A, pr=7, ref=BRANCH, summary=FAIL_SUMMARY, repo=REPO,
        check_id="555"):
    return NormalizedEvent(delivery_id=delivery, event=event, action=action,
                           repo=repo, pr_number=pr, head_ref=ref,
                           head_sha=sha, check_id=check_id, summary=summary)


def _n(db, sql, *args):
    return db.execute(sql, args).fetchone()[0]


# ------------------------------------------------------------------ correlation
def test_unambiguous_correlation_uses_only_the_stored_mapping(db):
    wf, tid = _wf_task(db)
    link = _link(db, wf, tid)

    verdict = github_feedback.apply_delivery(db, _ev())

    assert verdict["correlation"] == repo_pr_links.MATCHED
    assert verdict["task_id"] == tid and verdict["workflow_id"] == wf
    assert verdict["link_id"] == link
    row = repo_github_events.get_event(db, "d-1")
    assert row["processed_state"] == "done"
    outcome = repo_github_events.get_outcome(db, "d-1")
    assert outcome["correlation"] == "matched"
    assert outcome["disposition"] == github_feedback.DISPOSITION_HANDOFF
    # the ledger keeps exactly the one logical event
    assert _n(db, "SELECT COUNT(*) FROM github_events") == 1


def test_unknown_repo_branch_or_sha_is_recorded_but_never_dispatches(db):
    wf, tid = _wf_task(db)
    _link(db, wf, tid)                       # a different PR/branch/SHA exists
    before = _n(db, "SELECT COUNT(*) FROM tasks")

    # same repo, unknown PR + branch + SHA
    v = github_feedback.apply_delivery(
        db, _ev(delivery="d-unknown", pr=99, ref="claudio/ghost", sha=SHA_B))
    assert v["correlation"] == repo_pr_links.UNMATCHED
    assert v["disposition"] == github_feedback.DISPOSITION_AUDIT
    assert v["followup_task_id"] is None and v["handoff_id"] is None

    # a repo that is not a configured project at all
    v2 = github_feedback.apply_delivery(
        db, _ev(delivery="d-norepo", repo="stranger/repo"))
    assert v2["correlation"] == repo_pr_links.NO_REPO

    assert _n(db, "SELECT COUNT(*) FROM github_events") == 2   # recorded
    assert _n(db, "SELECT COUNT(*) FROM tasks") == before      # nothing made
    assert _n(db, "SELECT COUNT(*) FROM handoffs") == 0
    assert _n(db, "SELECT COUNT(*) FROM outbox") == 0
    assert repo_tasks.get_task(db, tid)["status"] == "running"


def test_stale_head_sha_cannot_dispatch_and_changes_nothing(db):
    wf, tid = _wf_task(db)
    _link(db, wf, tid, sha=SHA_A)
    task_before = dict(repo_tasks.get_task(db, tid))
    wf_before = dict(repo_workflows.get_workflow(db, wf))

    verdict = github_feedback.apply_delivery(
        db, _ev(sha=SHA_B, summary=FAIL_SUMMARY))

    # audit-only: no dispatch, no status change, no handoff, no projection
    assert verdict["correlation"] == repo_pr_links.STALE_SHA
    assert verdict["disposition"] == github_feedback.DISPOSITION_AUDIT
    assert verdict["followup_task_id"] is None
    assert verdict["handoff_id"] is None
    assert dict(repo_tasks.get_task(db, tid)) == task_before
    assert dict(repo_workflows.get_workflow(db, wf)) == wf_before
    assert _n(db, "SELECT COUNT(*) FROM tasks") == 1
    assert _n(db, "SELECT COUNT(*) FROM handoffs") == 0
    assert _n(db, "SELECT COUNT(*) FROM outbox") == 0
    # the stale event did not make its SHA current
    cur = repo_pr_links.current_links(db, repo=REPO, pr_number=7)
    assert [r["head_sha"] for r in cur] == [SHA_A]
    # ... and the real head still correlates afterwards
    assert github_feedback.apply_delivery(
        db, _ev(delivery="d-now", sha=SHA_A))["correlation"] == "matched"


def test_stale_head_sha_cannot_dispatch_even_under_auto_dispatch(db):
    wf, tid = _wf_task(db)
    _link(db, wf, tid, sha=SHA_A)
    repo_pr_links.set_followup_policy(db, REPO, repo_pr_links.POLICY_AUTO_DISPATCH)

    v = github_feedback.apply_delivery(db, _ev(sha=SHA_B))

    assert v["correlation"] == repo_pr_links.STALE_SHA
    assert v["followup_task_id"] is None
    assert _n(db, "SELECT COUNT(*) FROM tasks") == 1
    assert repo_tasks.get_task(db, tid)["status"] == "running"


# --------------------------------------------------------------------- dedupe
def test_duplicate_delivery_and_restart_replay_yield_one_event_one_followup(db):
    wf, tid = _wf_task(db)
    _link(db, wf, tid)

    first = github_feedback.apply_delivery(db, _ev(delivery="dup"))
    assert first["disposition"] == github_feedback.DISPOSITION_HANDOFF

    # GitHub redelivery: same delivery_id, planner sees it again
    replay = github_feedback.apply_delivery(db, _ev(delivery="dup"))
    assert replay["status"] == "duplicate"
    assert replay["followup_task_id"] is None and replay["handoff_id"] is None

    # Planner restart: the ledger row was committed but the command was never
    # marked applied, so the delivery is re-consumed from the inbox. Reset the
    # state the way a restart would find it and run it again.
    db.execute("UPDATE github_events SET processed_state='new' "
               "WHERE delivery_id='dup'")
    restart = github_feedback.apply_delivery(db, _ev(delivery="dup"))
    assert restart["followup_task_id"] is None
    assert restart["handoff_id"] is None

    # exactly one logical event...
    assert _n(db, "SELECT COUNT(*) FROM github_events") == 1
    # ... and at most one follow-up + one projection of each kind
    assert _n(db, "SELECT COUNT(*) FROM handoffs") == 1
    assert _n(db, "SELECT COUNT(*) FROM outbox") <= 1
    assert _n(db, "SELECT COUNT(*) FROM gh_followups") <= 2
    assert _n(db, "SELECT COUNT(*) FROM tasks") == 1


def test_duplicate_delivery_under_auto_dispatch_makes_one_task_one_projection(db):
    wf, tid = _wf_task(db)
    _link(db, wf, tid)
    repo_pr_links.set_followup_policy(db, REPO, repo_pr_links.POLICY_AUTO_DISPATCH)

    github_feedback.apply_delivery(db, _ev(delivery="dup2"))
    db.execute("UPDATE github_events SET processed_state='new' "
               "WHERE delivery_id='dup2'")
    github_feedback.apply_delivery(db, _ev(delivery="dup2"))
    github_feedback.apply_delivery(db, _ev(delivery="dup2"))

    assert _n(db, "SELECT COUNT(*) FROM github_events") == 1
    assert _n(db, "SELECT COUNT(*) FROM tasks") == 2          # one follow-up
    assert _n(db, "SELECT COUNT(*) FROM gh_followups "
                  "WHERE kind='task'") == 1
    assert _n(db, "SELECT COUNT(*) FROM outbox") <= 1


def test_sweep_consumes_api_written_rows_once(db):
    """The API writes the ledger row; the planner's sweep is the consumer."""
    wf, tid = _wf_task(db)
    _link(db, wf, tid)
    ev = _ev(delivery="swept")
    assert repo_github_events.insert_event(db, ev) is True   # state 'new'

    assert github_feedback.sweep_unprocessed(db) == 1
    assert repo_github_events.get_event(db, "swept")["processed_state"] == "done"
    assert _n(db, "SELECT COUNT(*) FROM handoffs") == 1
    assert github_feedback.sweep_unprocessed(db) == 0
    assert _n(db, "SELECT COUNT(*) FROM handoffs") == 1


# ----------------------------------------------------------------- routing
def test_failed_check_becomes_an_operator_handoff_by_default(db):
    """No policy configured at all -> zero follow-up tasks."""
    wf, tid = _wf_task(db)
    _link(db, wf, tid)
    assert repo_pr_links.followup_policy(db, REPO) == repo_pr_links.POLICY_HANDOFF

    verdict = github_feedback.apply_delivery(db, _ev(summary=FAIL_SUMMARY))

    assert verdict["classification"] == github_feedback.CLASS_CODE
    assert verdict["disposition"] == github_feedback.DISPOSITION_HANDOFF
    assert verdict["handoff_id"]
    assert verdict["followup_task_id"] is None
    # the acceptance criterion, stated directly:
    assert _n(db, "SELECT COUNT(*) FROM tasks") == 1
    assert _n(db, "SELECT COUNT(*) FROM tasks WHERE workflow_id=?",
              wf) == 1
    handoffs = db.execute("SELECT * FROM handoffs").fetchall()
    assert len(handoffs) == 1
    assert handoffs[0]["task_id"] == tid
    assert handoffs[0]["status"] == "open"
    # the original task is untouched: a handoff is a request, never an edit
    assert repo_tasks.get_task(db, tid)["status"] == "running"
    # and the projection went through the idempotent outbox, not the client
    rows = db.execute("SELECT kind, idempotency_key FROM outbox").fetchall()
    assert [r["kind"] for r in rows] == ["gh_comment"]
    assert "d-1" in rows[0]["idempotency_key"]


def test_review_request_becomes_a_decision_handoff_by_default(db):
    wf, tid = _wf_task(db)
    _link(db, wf, tid)

    verdict = github_feedback.apply_delivery(db, _ev(
        delivery="rev", event="pull_request_review", action="submitted",
        summary="PR #7 submitted review changes_requested by alice"))

    assert verdict["outcome"] == github_feedback.OUT_REVIEW_CHANGES
    assert verdict["disposition"] == github_feedback.DISPOSITION_HANDOFF
    assert _n(db, "SELECT COUNT(*) FROM tasks") == 1
    kind = db.execute("SELECT kind FROM handoffs").fetchall()[0]["kind"]
    assert kind == "needs_decision"


def test_infrastructure_check_failure_is_classified_apart_from_code(db):
    wf, tid = _wf_task(db)
    _link(db, wf, tid)
    repo_pr_links.set_followup_policy(db, REPO, repo_pr_links.POLICY_AUTO_DISPATCH)

    verdict = github_feedback.apply_delivery(db, _ev(summary=INFRA_SUMMARY))

    assert verdict["classification"] == github_feedback.CLASS_INFRA
    assert verdict["disposition"] == github_feedback.DISPOSITION_HANDOFF
    # policy never turns an infra failure into a code-fix task
    assert verdict["followup_task_id"] is None
    assert _n(db, "SELECT COUNT(*) FROM tasks") == 1
    assert _n(db, "SELECT COUNT(*) FROM alerts") == 1
    assert github_feedback.classify_check(FAIL_SUMMARY) == github_feedback.CLASS_CODE
    assert github_feedback.classify_check(PASS_SUMMARY) == github_feedback.CLASS_NONE


def test_policy_enabled_dispatch_creates_a_validated_followup(db):
    from clams import contracts

    wf, tid = _wf_task(db)
    _link(db, wf, tid)
    original = repo_tasks.get_task(db, tid)
    repo_pr_links.set_followup_policy(db, REPO, repo_pr_links.POLICY_AUTO_DISPATCH)

    verdict = github_feedback.apply_delivery(db, _ev(summary=FAIL_SUMMARY))

    followup_id = verdict["followup_task_id"]
    assert followup_id
    assert verdict["disposition"] == github_feedback.DISPOSITION_DISPATCH
    followup = repo_tasks.get_task(db, followup_id)
    assert followup["workflow_id"] == wf
    assert followup["status"] in ("pending", "ready")
    assert followup["objective"] and followup["acceptance"]
    # it passed the same validation every planned task must pass
    brief = json.loads(followup["brief_json"])
    contracts.Brief(**brief)
    github_feedback.validate_followup(brief, followup["title"])
    # the original task contract is kept: same repo, base ref, scope, commands
    src = json.loads(original["brief_json"])
    for key in ("repo", "base_ref", "writable", "forbidden", "run", "max_turns"):
        assert brief[key] == src[key], key
    # exactly one follow-up, and the source task is untouched
    assert _n(db, "SELECT COUNT(*) FROM tasks") == 2
    assert repo_tasks.get_task(db, tid)["status"] == "running"


def test_followup_that_fails_validation_falls_back_to_a_handoff(db):
    wf, tid = _wf_task(db)
    _link(db, wf, tid)
    # a brief with no verification command cannot be validated as a task
    db.execute("UPDATE tasks SET brief_json=? WHERE task_id=?",
               (json.dumps({"objective": "o", "acceptance": "a", "run": [],
                            "writable": [], "repo": REPO}), tid))
    repo_pr_links.set_followup_policy(db, REPO, repo_pr_links.POLICY_AUTO_DISPATCH)

    verdict = github_feedback.apply_delivery(db, _ev())

    assert verdict["followup_task_id"] is None
    assert verdict["disposition"] == github_feedback.DISPOSITION_HANDOFF
    assert verdict["handoff_id"]
    assert _n(db, "SELECT COUNT(*) FROM tasks") == 1


# ------------------------------------------------- evidence gate independence
def test_passing_github_check_never_satisfies_the_clams_evidence_gate(db):
    wf, tid = _wf_task(db)
    _link(db, wf, tid)
    before = dict(repo_tasks.get_task(db, tid))
    assert before["status"] == "running"
    assert github_feedback.GATE_SATISFYING_OUTCOMES == frozenset()

    verdict = github_feedback.apply_delivery(db, _ev(summary=PASS_SUMMARY))

    after = dict(repo_tasks.get_task(db, tid))
    assert after == before                       # nothing moved
    assert after["status"] != "success"
    assert after["result_json"] in (None, "")    # no evidence written
    assert not repo_tasks.workflow_tasks_all_terminal(db, wf)
    assert verdict["disposition"] == github_feedback.DISPOSITION_AUDIT
    assert verdict["classification"] == github_feedback.CLASS_NONE
    assert _n(db, "SELECT COUNT(*) FROM tasks") == 1
    assert _n(db, "SELECT COUNT(*) FROM handoffs") == 0
    # the note is on the timeline and says out loud it is not gate evidence
    note = db.execute("SELECT message FROM events ORDER BY event_id DESC "
                      "LIMIT 1").fetchone()["message"]
    assert "not Clams gate evidence" in note


def test_merged_or_closed_pr_is_timeline_only_and_never_rewrites_evidence(db):
    wf, tid = _wf_task(db)
    _link(db, wf, tid)
    evidence = json.dumps({"outcome": "success", "commit_sha": SHA_A,
                           "evidence": {"passed": True}})
    db.execute("UPDATE tasks SET result_json=?, commit_sha=?, status='success' "
               "WHERE task_id=?", (evidence, SHA_A, tid))
    db.execute("UPDATE tasks SET status='success' WHERE task_id=?", (tid,))

    verdict = github_feedback.apply_delivery(
        db, _ev(delivery="closed", event="pull_request", action="closed",
                summary="PR #7 closed -> main by bot"))

    assert verdict["outcome"] == github_feedback.OUT_PR_CLOSED
    assert verdict["disposition"] == github_feedback.DISPOSITION_TIMELINE
    assert repo_tasks.get_task(db, tid)["result_json"] == evidence
    assert repo_tasks.get_task(db, tid)["commit_sha"] == SHA_A
    assert repo_workflows.get_workflow(db, wf)["status"] == "running"
    assert _n(db, "SELECT COUNT(*) FROM tasks") == 1
    assert _n(db, "SELECT COUNT(*) FROM handoffs") == 0
    timeline = db.execute(
        "SELECT scope, ref, message FROM events WHERE scope='workflow' "
        "ORDER BY event_id DESC LIMIT 1").fetchone()
    assert timeline["ref"] == wf and "PR closed" in timeline["message"]
    # the link is closed, so a later event about it is no longer current
    assert repo_pr_links.correlate(db, repo=REPO, pr_number=7,
                                   head_sha=SHA_A)["status"] == \
        repo_pr_links.UNMATCHED


# ------------------------------------------------------------- inbox wiring
def test_command_inbox_routes_a_github_event(db):
    from clams.planner.commands import drain_commands

    wf, tid = _wf_task(db)
    _link(db, wf, tid)
    ev = _ev(delivery="cmd")
    assert repo_github_events.record_delivery(db, ev)[0] is True
    assert repo_github_events.record_delivery(db, ev) == (False, None)

    assert drain_commands(db) == 1
    assert repo_github_events.get_event(db, "cmd")["processed_state"] == "done"
    assert _n(db, "SELECT COUNT(*) FROM github_events") == 1
    assert _n(db, "SELECT COUNT(*) FROM handoffs") == 1
    assert _n(db, "SELECT COUNT(*) FROM tasks") == 1
    drain_commands(db)                           # nothing left to apply
    assert _n(db, "SELECT COUNT(*) FROM handoffs") == 1


def test_end_to_end_raw_delivery_to_handoff_through_the_inbox(db):
    """Raw payload -> normalize_event -> ledger+command -> drain -> handoff."""
    from clams.github.webhooks import normalize_event
    from clams.planner.commands import drain_commands

    repo = "acme/widgets"
    sha = "c" * 40
    pid = repo_projects.upsert_project(db, repo)
    wf = repo_workflows.create_workflow(db, pid, "github", "goal",
                                        gh_issue_number=42)
    brief = {"objective": "o", "acceptance": "a", "run": ["pytest -q"],
             "writable": ["src/**"], "repo": repo}
    tid = repo_tasks.create_task(db, wf, 1, "task", "o", "a", brief)
    repo_pr_links.link_task_commit(db, workflow_id=wf, task_id=tid, repo=repo,
                                   head_sha=sha, head_ref="feature/flux",
                                   pr_number=42)

    payload = {"action": "completed",
               "repository": {"full_name": repo},
               "check_suite": {"id": 7001, "conclusion": "failure",
                               "head_sha": sha, "head_branch": "feature/flux",
                               "check_runs_count": 3,
                               "pull_requests": [{"number": 42}]}}
    ev = normalize_event(payload, "d-e2e", event="check_suite")
    assert ev.event_action == "check_suite.completed" and ev.head_sha == sha

    assert repo_github_events.record_delivery(db, ev)[0] is True
    assert repo_github_events.record_delivery(db, ev) == (False, None)
    assert drain_commands(db) == 1

    assert _n(db, "SELECT COUNT(*) FROM github_events") == 1
    assert _n(db, "SELECT COUNT(*) FROM commands WHERE kind='github_event'") == 1
    assert repo_github_events.get_event(db, "d-e2e")["processed_state"] == "done"
    verdict = repo_github_events.get_outcome(db, "d-e2e")
    assert verdict["correlation"] == "matched"
    assert verdict["classification"] == "code"
    assert verdict["disposition"] == github_feedback.DISPOSITION_HANDOFF
    assert _n(db, "SELECT COUNT(*) FROM tasks") == 1        # no auto dispatch
    assert _n(db, "SELECT COUNT(*) FROM handoffs") == 1
    assert _n(db, "SELECT COUNT(*) FROM outbox") == 1
