# Replan and supersede: how a failed task stops poisoning a workflow

Issue: **claudio#58** (reporter cadfa395).
Regression test: `tests/unit/test_replan_poison_regression.py`.

## The symptom

A workflow whose task #1 failed every attempt was blocked with
`blocked: replans exhausted`. An operator issued a `replan`, the new plan ran
100% green, and the workflow still read `failed`. It could never close out.

One stale terminal row was enough. `workflow_outcome` reports `failed` if *any*
task is failed and `blocked` if any is blocked, and the superseded row kept its
status across the replan, so it poisoned the outcome forever.

## The four rules

### 1. Superseded failed/blocked rows become `cancelled` at replan time

`repo_tasks.reconcile_plan` is the write side. A row whose plan key is **absent
from the incoming plan** is superseded:

* `failed` / `blocked` -> rewritten to **`cancelled`**, and the id is reported
  under `superseded_terminal` as well as `stale`. This is the fix: the stale
  pass used to skip every terminal row (`if r["status"] not in TERMINAL`), so a
  failure survived the replan that had already thrown it away.
* `success` -> **stays `success`**. It records delivered work, it never blocks
  `done`, and rewriting it would erase shipped progress.
* already `cancelled` -> reported as stale, not rewritten.
* a row whose key **is** in the new plan keeps its status exactly as before
  (only its content is refreshed), so a retried task is still `failed`.

The set that gets flipped is `SUPERSEDE_TERMINAL = ("failed", "blocked")`.
Nothing is ever `DELETE`d by `reconcile_plan` — that is what keeps
`task_id`s, and therefore GitHub sub-issue mappings, stable across a replan.

Production entry point: `planner.reconcile.apply_replanned_plan`, which wraps
`reconcile_plan` in one transaction and writes the audit events described
below. `commands._apply_plan` and `plan.plan_workflow` still use the older
delete-and-reinsert swap, where the row disappears instead of being cancelled.

### 2. `workflow_outcome` ignores rows outside the current plan

`repo_tasks.workflow_outcome` is the read-side mirror, for rows that went
terminal *before* rule 1 landed and for workflows replanned by
delete-and-reinsert. It skips a terminal `failed`/`blocked` row that is no
longer part of the plan, so a replanned workflow that finishes green reads
`done`.

"Outside the current plan" is decided by `is_superseded_row`:

* **with a stored plan** (`workflows.plan_json`, written by `set_plan`,
  `set_plan_json` and by `reconcile_plan` itself): the row is superseded iff its
  plan key — `plan_key`, else `normalize_plan_key(title)` — is absent from the
  plan. `normalize_plan_key` strips, collapses internal whitespace and
  casefolds, so `"  Fix   Replan Dedupe "` matches `"fix replan dedupe"`.
* **without one** (no `plan_json`, malformed, empty, or a schema without the
  column): the fallback is `last_replan_ts` — the row is superseded iff a
  replan event landed strictly *after* the row went terminal. A missing
  timestamp or a tie counts as **not** superseded, i.e. the conservative
  pre-fix behaviour.

`SUPERSEDEABLE_STATUSES = ("failed", "blocked")`. `success` is excluded on
purpose: it is delivered work. In-plan `failed`, `blocked`, `cancelled` and
`success` rows keep their old rules, so a real failure in the live plan still
reports `failed`.

### 3. An operator replan resets the replan budget

`commands._apply` handles `kind == "replan"` by calling
`repo_workflows.reset_replan_budget(conn, wf, reason=f"operator_replan by {issuer}")`,
which zeroes `workflows.replans` and returns the count it reset from, then
moves the workflow to `planning`.

Without the reset the counter stayed at `max_replans`, so the first outcome
after the override re-entered `review_workflow`'s exhaustion branch and blocked
again with `replans exhausted` instead of replanning. The override is a
deliberate human decision, so it starts from a **full** budget.

Two things this does *not* do:

* `max_replans` is a planner setting (`clams.config.Settings`), never a column
  on `workflows`, so the cap itself cannot be read or mutated here.
* automated / planner-issued replans keep incrementing the counter via
  `bump_replans` inside `review_workflow`. Only the operator path resets.

`blocked` stays a replannable status on purpose (`("failed", "blocked",
"review", "running")`): that is exactly the state the exhausted budget produces,
and an operator replan is how a human hands it a fresh budget. Terminal
`cancelled` / `done` workflows are still rejected, and a rejected command must
not reset anything.

### 4. Where the failure record still survives

Rule 1 removes the *visible* failure from the tasks table, so the evidence is
copied out before the flip and is never deleted by a replan:

| Where | What survives |
| --- | --- |
| `events` (task scope) | `attempt N -> failed` per attempt, written by `review._apply`; plus `superseded by replan: failed -> cancelled` from `apply_replanned_plan`, whose payload carries `previous_status`, an `error_log` excerpt, `attempt`, `max_attempts` and the workflow id. |
| `events` (workflow scope) | `blocked: replans exhausted`, `replan budget reset`, `replan requested by <issuer>`, and the `replan: reused N, created M, cancelled K` summary carrying `superseded_terminal` ids + count. |
| `tasks.error_log` | the tail of the failing attempt's log. `reconcile_plan` writes `status` and `updated_at` only, so `error_log` survives the flip to `cancelled` untouched. |
| `tasks.result_json` | the full per-attempt result payload (outcome, evidence, commit_sha, branch, changed_files, log_tail, session_ref). |
| attempt counters | there is **no separate attempts table** in this schema: `tasks.attempt` / `tasks.max_attempts` are the attempt record, and `reconcile_plan` never writes them for a superseded row, so the exhausted count (`attempt == max_attempts`) is still readable after the row is `cancelled`. |

So a `cancelled` row is bookkeeping, not a cover-up: the timeline still shows
which attempt failed and why.

## The chain that now works

1. task #1 fails every attempt with the budget already spent -> workflow
   `blocked`, event `blocked: replans exhausted`, `workflow_outcome == 'failed'`.
2. operator `replan` -> `replans = 0`, status `planning`; the fresh plan's
   titles do not match the failed task -> that row becomes **`cancelled`**.
3. every new task reaches `success` -> `workflow_outcome == 'done'`, and
   `review_workflow` closes the workflow out as **`done`** — not `replan`, not
   `blocked`.
