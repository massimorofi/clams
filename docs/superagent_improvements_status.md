# SuperAgent improvements — implementation status and issue bookkeeping

Machine-readable companion: [`superagent_improvements_status.json`](superagent_improvements_status.json)

Parent plan: [massimorofi/clams#138 — SuperAgent-inspired Clams improvements](https://github.com/massimorofi/clams/issues/138)
Bookkeeping task (this document): [massimorofi/clams#153](https://github.com/massimorofi/clams/issues/153)
Workflow: `6ddd7472-b810-48c7-b8f7-2e74a49a42de` · Branch: `clams/195e2dad-7326-45cd-8dd6-6e5fdfff09e2-a1`

This is a **documentation-only** deliverable. It maps each improvement to the GitHub issue that
tracks it, states which acceptance criteria are satisfied versus deferred, names the gate command
that proves each claim, and records the GitHub state transition that still has to be applied.
**No source file outside `docs/` was modified.**

## 1. Mapping

| Improvement | Title (issue) | Issue | Status | Test files | Tests |
|---|---|---|---|---|---|
| 2 | Append-only task attempt history + cumulative retry context | [#147](https://github.com/massimorofi/clams/issues/147) | implemented | `test_attempt_history.py` | 7 |
| 7 | Read-only workflow post-mortem analysis job | [#148](https://github.com/massimorofi/clams/issues/148) | implemented | `test_postmortem.py` | 31 |
| 3 | Structured worker progress + Planner/operator handoffs | [#149](https://github.com/massimorofi/clams/issues/149) | implemented | `test_worker_progress.py`, `test_handoffs.py` | 20 + 30 |
| 1 | GitHub PR/CI feedback loop — 5a intake, 5b correlation | [#150](https://github.com/massimorofi/clams/issues/150), [#151](https://github.com/massimorofi/clams/issues/151) | implemented | `test_gh_webhooks.py`, `test_gh_feedback_correlation.py` | 59 + 16 |
| 5 | Governed Claudio skill lifecycle | [#152](https://github.com/massimorofi/clams/issues/152) | implemented | `test_skill_lifecycle.py` | 55 |
| 4 | Configurable intake policy modes (shadow / enforce) | — no issue opened | not_in_scope | — | — |
| 6 | Controlled subagent delegation with scoped capabilities | — no issue opened | not_in_scope | — | — |

Improvement 1 was delivered as **two** issues (Phase 5a and Phase 5b), which is why five
improvements are covered by seven new test files.

## 2. Cross-feature regression gate (run in this task)

All seven new test files were executed together in a **single** pytest invocation:

```
PYTHONPATH=src python3 -m pytest tests/unit/test_attempt_history.py tests/unit/test_postmortem.py \
  tests/unit/test_worker_progress.py tests/unit/test_handoffs.py tests/unit/test_gh_webhooks.py \
  tests/unit/test_gh_feedback_correlation.py tests/unit/test_skill_lifecycle.py -o addopts=''
```

Result: **218 passed, 0 failed** (~3–6 s). Each feature also passes its own per-issue gate
(7 / 31 / 20+30 / 59 / 16 / 55), so the five features coexist without regressions: no shared
fixture, migration or module import collides when the suites run in one process.

## 3. Per-improvement detail

### Improvement 2 — cumulative retry context · [#147](https://github.com/massimorofi/clams/issues/147)

Task `f3d288fe-c76f-4da3-8426-03e439577c14` · commit `073fd63` _"Add append-only task_attempts history (#2)"_.

`migrations/011_task_attempts.sql` adds append-only `task_attempts` with `UNIQUE(task_id, attempt_no)`
and UPDATE/DELETE rejection triggers. `src/clams/db/repo_attempts.py` (stdlib `sqlite3`/`json` only)
exposes `record_attempt()`, `get_attempts()`, `get_attempt()`, `latest_attempt_no()` and
`cumulative_retry_summary(task_id, max_chars)`. `repo_tasks.on_result` and `planner/dispatch.py` append
one idempotent row per attempt and the retry brief's `prior_failure` block now carries the bounded
cumulative summary instead of only the latest result; the task contract is unchanged across retries and
`executor/runner.py` seeds attempt N from attempt N-1's branch.

**Satisfied:** migration + `UNIQUE(task_id, attempt_no)`; `repo_attempts.py` free of `pydantic`/`fastapi`
imports; 7 tests pass (attempt-1 readable at attempt 3, replay neither duplicates nor overwrites, summary
bounded and complete); `cumulative_retry_summary` referenced in `planner/dispatch.py`; `#2` in the
migration header. **Deferred:** none.

Gate: `PYTHONPATH=src python3 -m pytest tests/unit/test_attempt_history.py -o addopts='' && echo ATTEMPT_HISTORY_OK`

### Improvement 7 — read-only post-mortem job · [#148](https://github.com/massimorofi/clams/issues/148)

Task `7138f0f5-fa32-41d2-a271-eca38b1aed15` · commit `445c13c` _"feat(postmortem): read-only post-mortem job + clams postmortem subcommand"_.

`src/clams/postmortem/` is strictly read-only: `select.py` (bounded workflow selection), `snapshot.py`
(SELECT-only evidence snapshot with stable refs `workflow:<id>` / `task:<id>` / `attempt:<task>:<n>` /
`event:<id>`), `report.py` (versioned JSON report, `schema_version`, evidence ref on every finding) and
`validate.py`, plus the stdlib-only `clams postmortem` subcommand.

**Satisfied:** all four modules exist; 31 tests pass; the CLI writes a report with `schema_version` and
non-empty evidence references on every finding; `grep` finds **0** `INSERT`/`UPDATE` under
`src/clams/postmortem/`, so nothing under `src/clams/db` or `src/clams/planner` is touched.
**Deferred:** none.

Gate: `PYTHONPATH=src python3 -m pytest tests/unit/test_postmortem.py -o addopts='' && echo POSTMORTEM_OK`

### Improvement 3 — worker progress + handoffs · [#149](https://github.com/massimorofi/clams/issues/149)

Task `b12fc1c3-792b-4360-9437-e3a2ce68d007` · commit `38f7939` _"Add worker progress fencing and operator handoffs"_.

`migrations/012_worker_progress_handoffs.sql` (bundled `worker_progress`, `handoffs`, `handoff_events`
and the `dispose_handoff` command kind). Progress messages are fenced: a stale `lease_token`/`attempt_no`
or a non-monotonic `seq` is rejected. Handoffs are append-only, closable, auditable decision records that
never mutate the immutable task brief.

**Satisfied:** migration exists; 20 + 30 tests pass; fenced messages leave `worker_progress` unchanged;
handoff rows survive close/reopen of the DB; no new code path writes the task brief columns
(`scope_change` stores the ask in `requested_action` only). **Deferred:** none.

Gate: `PYTHONPATH=src python3 -m pytest tests/unit/test_worker_progress.py tests/unit/test_handoffs.py -o addopts='' && echo HANDOFFS_OK`

### Improvement 1 — GitHub PR/CI feedback loop · [#150](https://github.com/massimorofi/clams/issues/150) + [#151](https://github.com/massimorofi/clams/issues/151)

Phase 5a, task `4d418c98-eb81-4f80-abc1-001a243da6fe` · commits `d937cb2` (intake), `a1ab77d` (docs),
`a421ab3` (fold migration 014 into 013 to stay inside writable scope), `b55d5fd` (keep the webhook secret
out of settings serialization).
Phase 5b, task `f5152736-737b-4961-b137-959cfaa5ffc1` · commit `47f87cf` _"feat(planner): route GitHub
feedback through explicit pr_links mapping"_.

5a: `src/clams/github/webhooks.py` + `migrations/013_github_events.sql` — constant-time HMAC verification
via `hmac.compare_digest`, repo allowlist, a durable normalized bounded `github_events` ledger deduped on
`delivery_id`, and a `github_event` command enqueued into the existing inbox. The secret and the allowlist
are plain `@property` members so `model_dump()`/`model_dump_json()` cannot echo them.
5b: `src/clams/planner/github_feedback.py` + `migrations/014_github_pr_links.sql` — a delivery may
influence a task only through an explicit repo + branch/PR + head-SHA `pr_links` mapping (five verdicts,
only `matched` acts); stale-SHA events are audit-only; infra/config check failures become operator
decisions; auto-dispatch only under an explicit `gh_project_policy`; outcomes route through the handoff
model; projections go through the idempotent outbox.

**Satisfied:** both modules and both migrations exist; 59 + 16 tests pass; `hmac.compare_digest` present
(3 occurrences); no raw payload persisted; bad signature / unsupported action produce no inbox row; with no
policy configured zero follow-ups are created; the stale-SHA test asserts no dispatch and no status change;
the duplicate-delivery test asserts exactly one `github_events` row and at most one follow-up/outbox effect.
**Deferred:** configurable intake policy modes (improvement 4) — intake ships as one authenticated
allowlisted path with no mode selector; a green GitHub check is never Clams gate evidence.

Gates: `PYTHONPATH=src python3 -m pytest tests/unit/test_gh_webhooks.py -o addopts='' && echo GH_WEBHOOK_INTAKE_OK`
· `PYTHONPATH=src python3 -m pytest tests/unit/test_gh_feedback_correlation.py -o addopts='' && echo GH_FEEDBACK_ROUTE_OK`

### Improvement 5 — governed skill lifecycle · [#152](https://github.com/massimorofi/clams/issues/152)

Task `b8fb6b4e-18e3-4693-bd91-8ff6cf2a2639` · commit `4ffd3b7` _"feat(skills): versioned skill lifecycle with
hash-keyed approval and rollback"_.

`migrations/015_skill_lifecycle.sql` (`skills`, `skill_versions`, `skill_evaluations`, `skill_approvals`,
`skill_deployments`, `task_skill_versions`; approvals and deployments are append-only ledgers) and
`src/clams/skills/{manifest,lifecycle,evaluate,store}.py`: deny-by-default manifest (unknown keys rejected),
the ladder `proposed → draft → evaluated → approved → active → retired` keyed to `content_hash`, versioned
eval sets where a safety failure or safety regression blocks on its own, and a store that re-checks hash
equality at activation. The stdlib-only `clams skills` CLI group requires `--as` on every mutation and exits
3 on a refused gate; dispatch stamps live skill versions in the claim transaction.

**Satisfied:** migration and all four modules exist; 55 tests pass; a post-mortem-derived proposal cannot
reach `active` without an approval row whose `content_hash` equals the evaluated artifact hash; editing
content after evaluation drops back to `draft` and invalidates the approval by hash mismatch; rollback
restores the previous approved version and is recorded (`rolled_back_at`, history never rewritten).
**Deferred:** controlled subagent delegation (improvement 6) — capability is modelled as an actor capability
set, not as a delegation grant.

Gate: `PYTHONPATH=src python3 -m pytest tests/unit/test_skill_lifecycle.py -o addopts='' && echo SKILL_LIFECYCLE_OK`

## 4. Required GitHub state transition

> **This transition has NOT been applied.** It must be applied with `gh issue edit` / `gh issue comment`
> from an environment that has a GitHub token (`GITHUB_TOKEN`/`GH_TOKEN`) and network egress. **The gate
> container that ran the verification for this task has no network and no token**, so this document only
> records the transition; nothing on GitHub was mutated by task #153.

Observed state at the time of writing (read-only `GET` against the REST API): issues **#147, #148, #149,
#150, #151, #152** are all `closed` and carry the label `status:Done`. The project board is Projects V2
**"Clams"** (number 3, `PVT_kwHOAKvAL84Bqz3n`) whose `Status` single-select offers
`Backlog · Ready · In progress · In review · Done`. The repository labels are
`status:Blocked, status:Done, status:In Progress, status:Ready` — **there is no `status:In review` label
yet**, so it must be created once before the label transition can be applied.

For **each** of #147, #148, #149, #150, #151, #152 the required transition is:

1. reopen the issue (it is currently closed),
2. remove `status:Done` and add the In-review status label `status:In review` (the label that mirrors the
   board's `In review` Status option),
3. set the Projects V2 `Status` field of the issue's card to **In review**,
4. post a comment listing the delivered acceptance criteria and the gate commands that prove them (§5).

```bash
# one-time: the board has an 'In review' status but the repo has no matching label
gh label create 'status:In review' --color 1d76db --repo massimorofi/clams

for n in 147 148 149 150 151 152; do
  gh issue reopen "$n" --repo massimorofi/clams
  gh issue edit   "$n" --repo massimorofi/clams \
      --remove-label 'status:Done' --add-label 'status:In review'
  gh issue comment "$n" --repo massimorofi/clams --body-file "comments/issue-$n.md"
done
```

Board card status (the label alone does not move the Projects V2 card):

```bash
gh project item-edit --project-id PVT_kwHOAKvAL84Bqz3n \
  --id "<item-id>" --field-id "<Status field id>" \
  --single-select-option-id "<'In review' option id>"
```

Improvements **4** and **6** get **no** transition: no issue was opened for them and nothing was
implemented, so there is nothing to move to In review.

## Comment bodies

Ready-to-post text for `gh issue comment <n> --body-file …`. Each lists the delivered acceptance criteria
and the gate command(s) that prove them.

**#147 (improvement 2)**
> Implemented. Delivered acceptance criteria: `migrations/011_task_attempts.sql` creates append-only
> `task_attempts` with `UNIQUE(task_id, attempt_no)`; `src/clams/db/repo_attempts.py` (stdlib only, no
> pydantic/fastapi) exposes `record_attempt`/`get_attempts`/`cumulative_retry_summary`; attempt-1 evidence
> is still readable at attempt 3; a replayed result neither duplicates nor overwrites rows; the cumulative
> summary is bounded and includes every attempt; `cumulative_retry_summary` is used in
> `src/clams/planner/dispatch.py`; `#2` is referenced in the migration header.
> Gate: `PYTHONPATH=src python3 -m pytest tests/unit/test_attempt_history.py -o addopts='' && echo ATTEMPT_HISTORY_OK` → 7 passed.

**#148 (improvement 7)**
> Implemented. Delivered: `src/clams/postmortem/{select,snapshot,report,validate}.py`; the `clams postmortem`
> subcommand writes a JSON report with `schema_version` and a non-empty evidence ref on every finding; no
> `INSERT`/`UPDATE` anywhere under `src/clams/postmortem/` (0 grep matches), so `src/clams/db` and
> `src/clams/planner` are untouched.
> Gate: `PYTHONPATH=src python3 -m pytest tests/unit/test_postmortem.py -o addopts='' && echo POSTMORTEM_OK` → 31 passed.

**#149 (improvement 3)**
> Implemented. Delivered: `migrations/012_worker_progress_handoffs.sql`; a progress message with a stale
> `lease_token`/`attempt_no` or non-monotonic `seq` is rejected and leaves `worker_progress` unchanged;
> handoff rows remain readable after close/reopen; no new code path writes the task brief columns.
> Gate: `PYTHONPATH=src python3 -m pytest tests/unit/test_worker_progress.py tests/unit/test_handoffs.py -o addopts='' && echo HANDOFFS_OK` → 50 passed.
**#150 (improvement 1, Phase 5a)**
> Implemented. Delivered: `src/clams/github/webhooks.py` and `migrations/013_github_events.sql`; signature
> verification uses `hmac.compare_digest` (3 occurrences); `github_events` stores only normalized bounded
> fields — no raw webhook payload is persisted; an unsupported event action or a bad signature produces no
> command-inbox row; `delivery_id` dedupes redeliveries; the webhook secret is a plain `@property` so it
> cannot appear in `model_dump()`/`model_dump_json()`.
> Gate: `PYTHONPATH=src python3 -m pytest tests/unit/test_gh_webhooks.py -o addopts='' && echo GH_WEBHOOK_INTAKE_OK` → 59 passed.

**#151 (improvement 1, Phase 5b)**
> Implemented. Delivered: `src/clams/planner/github_feedback.py` and `migrations/014_github_pr_links.sql`;
> correlation only through an explicit repo + branch/PR + head-SHA `pr_links` mapping (only `matched` may
> influence a task); with no policy configured zero follow-up tasks are created for a failed check; a stale
> head SHA produces no dispatch and no status change; a duplicate delivery yields exactly one `github_events`
> row and at most one follow-up/outbox side effect.
> Gate: `PYTHONPATH=src python3 -m pytest tests/unit/test_gh_feedback_correlation.py -o addopts='' && echo GH_FEEDBACK_ROUTE_OK` → 16 passed.

**#152 (improvement 5)**
> Implemented. Delivered: `migrations/015_skill_lifecycle.sql` and `src/clams/skills/{manifest,lifecycle,evaluate,store}.py`;
> a post-mortem-derived proposal cannot reach `active` without an approval row whose `content_hash` equals the
> evaluated artifact hash; editing content after evaluation drops the version back to `draft` and invalidates
> the approval by hash mismatch; rollback restores the previous approved version and is recorded.
> Gate: `PYTHONPATH=src python3 -m pytest tests/unit/test_skill_lifecycle.py -o addopts='' && echo SKILL_LIFECYCLE_OK` → 55 passed.

**#153 (this bookkeeping task)**
> Docs delivered: `docs/superagent_improvements_status.json` (7 entries, exactly 5 `implemented` for
> improvements 2, 7, 3, 1, 5, each with an `issue_number`) and this file. All seven new test files pass in a
> single pytest invocation (218 passed). The GitHub status transitions above are recorded, not applied: the
> gate container has no network and no token.

## Not in scope

- **Improvement 4 — configurable intake policy modes (shadow / enforce).** No GitHub issue was opened and
  nothing was implemented. Delivered intake (issue #150) is a single authenticated, allowlisted path with no
  mode selector; shipping a half-mode selector would have been worse than none. Deferred criteria: one mode
  selector with three allowed values, shadow mode records would-be actions without dispatching, enforce mode
  blocks out-of-scope writes.
- **Improvement 6 — controlled subagent delegation with scoped capabilities.** No GitHub issue was opened and
  nothing was implemented. Skill capability is modelled as an actor capability set (issue #152), not as a
  delegation grant. Deferred criteria: explicit bounded delegation grants, subagents cannot widen their own
  scope/contract/writable paths, delegation decisions recorded and auditable.

## Scope and verification

- Writable paths honoured: only `docs/` was written. `git status` before this commit shows exactly two new
  files, both under `docs/`; `src/**`, `tests/**`, `migrations/**` and `.env*` are untouched.
- Nothing was fixed outside `docs/` — the seven suites already passed at HEAD.
- Issue titles, states, labels, the parent plan (#138) and the Projects V2 status options were read from the
  GitHub REST/GraphQL API at run time; the recorded "observed state" is a snapshot, so re-check with
  `gh issue view <n>` before applying §4.
