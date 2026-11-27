# GitHub feedback route (planner consumption)

`docs/github-webhook-intake.md` covers the **intake** half: an authenticated
delivery becomes one `github_events` ledger row plus one `github_event`
command, and nothing else. This document covers the **consumption** half —
`src/clams/planner/github_feedback.py`, the only place a delivery is allowed to
influence Clams state.

```
GitHub delivery
  -> POST /webhooks/github          (API: verify, normalize, ledger + command)
  -> github_event command           (the API's only write target)
  -> planner drain_commands / sweep_unprocessed
  -> github_feedback.apply_delivery (correlate -> classify -> route)
  -> handoff | validated follow-up task | workflow timeline | audit-only
```

## 1. Correlation is a lookup, never an inference

A delivery is attached to a task/workflow **only** through an explicit
`pr_links` row (migration 014): `repo` + `pr_number`/`head_ref` + `head_sha`.
Whoever knows the fact records it:

```python
github_feedback.link_task_head(conn, task_id=t, repo="o/r",
                               head_sha=sha, head_ref=branch, pr_number=7)
```

There is no title, goal or branch-name similarity anywhere in the route — the
schema has nothing to match such a thing against. `repo_pr_links.correlate()`
returns one of five verdicts, and only the first may influence anything:

| verdict | meaning | may dispatch? |
|---|---|---|
| `matched` | exactly one **current** link whose SHA equals the event SHA | yes |
| `stale_sha` | the PR/branch is linked, but to a different SHA | **no** |
| `ambiguous` | several current links claim this PR/branch | **no** |
| `unmatched` | nothing stored for this repo+branch/PR+SHA | **no** |
| `unknown_repo` | the repo is not a configured project | **no** |

Anything not `matched` is written to `gh_feedback` as `disposition='audit_only'`
with an `events` timeline note, and produces no task, no handoff and no
projection.

## 2. The event SHA must equal the currently associated PR head

`pr_links.state='current'` **is** "the currently associated PR head". A push
supersedes the previous row rather than rewriting it, so history shows which SHA
was current when a given delivery was processed — which is precisely what makes
a stale event detectable.

A delivery about an older commit (a late check, a re-run, a redelivery) is
`stale_sha`: it stays in `github_events` as audit history, cannot dispatch work,
cannot create a handoff, and cannot mark its own SHA current. A later event
carrying the real head SHA still correlates normally.

Note that a `pull_request.synchronize` event whose new SHA nobody recorded is
`stale_sha` by construction. That is intentional: only an explicitly recorded
commit may drive state.

## 3. Infrastructure/configuration failures are their own class

`classify_check()` reads the bounded `conclusion=<x>` token out of the
normalized summary:

* `success` / `neutral` / no token → `none`
* `timed_out`, `cancelled`, `action_required`, `stale`, `skipped`, or a
  setup/runner/quota/credential/network marker → **`infra_config`**
* plain `failure` → **`code`**

An `infra_config` failure raises an alert and creates a `needs_decision`
handoff ("fix the CI configuration or runner, then re-run; no code change is
implied"). **A project's auto-dispatch policy never turns an infrastructure
failure into a code-fix task** — retrying a platform fault just burns attempts.

## 4. One delivery, one logical event, one follow-up

Two independent fences:

1. `github_events.delivery_id` is the intake idempotency key (the API's
   `INSERT OR IGNORE`), and `processed_state` marks consumption. Re-applying an
   already-`done` delivery returns `status='duplicate'` and does nothing.
2. `gh_followups` has `PRIMARY KEY (delivery_id, kind)` with kinds
   `task | handoff | outbox_comment | outbox_label | timeline`. Reserving a
   kind is `INSERT OR IGNORE`; a replay gets `False` and must skip.

Fence 2 is what covers the restart window: a planner that commits the ledger row
and side effects but dies before the command row is marked `applied` will
re-drain that command on restart. The whole route runs in **one** transaction
(`_tx` joins an open `BEGIN IMMEDIATE` rather than nesting one), so the ledger
row, the verdict, the follow-up and the outbox row commit together or not at all.

## 5. Outcomes route through the handoff model

| correlated outcome | default (`policy='handoff'`) | `policy='auto_dispatch'` |
|---|---|---|
| failed check, `code` | `blocked` handoff | **one validated follow-up task** |
| failed check, `infra_config` | `needs_decision` handoff + alert | same (never a task) |
| review changes requested / ready for review | `needs_decision` handoff | one validated follow-up task |
| check passed | timeline note only | same |
| PR merged / closed | workflow timeline, link closed | same |
| PR opened/reopened/synchronize | workflow timeline note | same |

The policy lives in `gh_project_policy.followup_policy` and is **absent by
default**: no row, an empty value or an unknown value all collapse to
`handoff`. Absence is never read as consent.

```python
repo_pr_links.set_followup_policy(conn, "o/r", repo_pr_links.POLICY_AUTO_DISPATCH)
```

A follow-up task must survive the same validation as any planned task
(`contracts.Brief` + `contracts.validate_plan`), and it **keeps the original
task contract**: repo, base ref, writable globs, forbidden paths, run commands
and turn budget are copied from the source brief; only objective and acceptance
change. If validation refuses it, the claim is released and the route falls back
to an operator handoff — an invalid task is never invented.

## 6. What the route never does

* **Never rewrites task evidence.** A merged/closed PR appends to the workflow
  timeline and closes the link; `tasks.result_json`, `commit_sha` and `status`
  are untouched.
* **Never treats a green GitHub check as Clams gate evidence.** The gate is the
  executor's own `evidence.passed` on `task.result` (`planner/review.py`). A
  passing check is recorded as informational with `gate_evidence: false`;
  `GATE_SATISFYING_OUTCOMES` is an empty set on purpose.
* **Never talks to GitHub directly.** Projections are `gh_comment` rows enqueued
  on the idempotent outbox with key `clams:gh-feedback:<delivery_id>`, and the
  marker is embedded in the comment body so `GitHubClient.add_comment` can dedup
  a relay retry.

## Files

| file | role |
|---|---|
| `migrations/014_github_pr_links.sql` | `pr_links`, `gh_feedback`, `gh_followups`, `gh_project_policy` (all new tables; nothing existing is altered) |
| `src/clams/db/repo_pr_links.py` | stored mapping, correlation verdicts, project policy |
| `src/clams/db/repo_github_events.py` | payload/row reconstruction, outcome audit, follow-up claims |
| `src/clams/planner/github_feedback.py` | classify + route (the whole policy) |
| `src/clams/planner/commands.py` | `github_event` inbox adapter |
| `src/clams/planner/loop.py` | step 1b sweep of unconsumed ledger rows |
| `tests/unit/test_gh_feedback_correlation.py` | 15 tests, sync + stdlib + temp SQLite |
