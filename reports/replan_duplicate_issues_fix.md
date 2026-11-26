# Issue #133 — parent issue accumulating ~27 mostly-duplicated sub-issues

**Status:** fixed (reconcile-by-`plan_key`), regression-tested by
`tests/unit/test_replan_no_duplicate_issues.py`.
**Docs:** `doc/github.md` §7 "Replan and sub-issue idempotency".

## Symptom

A single parent issue (one workflow) ended up with ~27 sub-issues while the
current plan had 3 tasks. The children were *mostly duplicates*: the same three
titles repeated over and over, only the `Clams task <uuid>` line in the body
differed. None of the duplicates were ever closed.

## Root cause

A replan replaced the whole task set destructively:

```
repo_tasks.delete_workflow_tasks(conn, workflow_id)   # <-- the bug
for entry in plan:
    repo_tasks.create_task(...)                       # fresh uuid4 task_id
projection.project_tasks_created(conn, workflow_id, repo)
```

Two things went wrong, in sequence:

1. **`delete_workflow_tasks` discarded `gh_issue_number`.** The task row was the
   only place the mapping *task → GitHub sub-issue* lived. Deleting the row
   threw that mapping away, so the re-inserted task looked like work that had
   never had a sub-issue.
2. **The new `task_id` defeated the outbox idempotency key.** Sub-issue
   projections dedupe on `gh:gh_subissue:<task_id>:created`. A fresh `uuid4`
   per replan makes that key look never-seen, so `repo_outbox.enqueue` inserted
   a new row instead of returning `None`, and the relay dutifully created a
   *new* GitHub sub-issue for work that already had one.

Nothing closed the old sub-issues either — the rows they belonged to had been
deleted, so no code could name them any more. Each replan cycle therefore added
`len(plan)` children and retired none: 3 → 6 → 9 → … → 27.

The deeper design error: **`task_id` was being used as a cross-plan identity**
when it is only a within-plan identity. The planner reproduces *titles* across
replans; it cannot reproduce primary keys.

## Chosen strategy

Reconcile the incoming plan against the existing rows by a **stable, derived
identity** instead of deleting and re-inserting:

* `tasks.plan_key` (migration `009_task_plan_key.sql`) =
  `repo_tasks.normalize_plan_key(title)` — strip, collapse internal whitespace
  runs, casefold. Backfilled in-place for pre-existing rows.
* `repo_tasks.reconcile_plan()` matches on `plan_key`:
  * **match** → `UPDATE` in place. `task_id` and `gh_issue_number` are *never*
    written by a match, so the outbox key stays stable and the sub-issue is
    reused. Only mutable fields (objective, acceptance, brief, deps, sequence)
    are updated.
  * **in the plan, not yet in the DB** → insert.
  * **in the DB, not in the plan** → `status='cancelled'` (never `DELETE`d), and
    parked at a negative `sequence_index` so the new plan owns `0..n-1`. Keeping
    the row is what makes the close possible: the mapping to `gh_issue_number`
    survives.
  * **rows with `plan_key IS NULL`** (pre-009) fall back to
    `normalize_plan_key(title)` when matching, so the first replan after the
    migration converges on the existing rows instead of duplicating them.
* `clams.planner.reconcile.apply_replanned_plan()` wraps the swap in one
  transaction and calls `projection.project_tasks_superseded()` for the
  cancelled ids, which enqueues exactly one `gh_close` per dropped task's issue
  on key `gh:gh_subissue:<task_id>:closed`.
* `projection.project_tasks_created()` is strictly idempotent: it skips any task
  that already carries a `gh_issue_number`, and `enqueue` dedupes on the key.

### Invariant

> replan reconciles by `plan_key`: unchanged tasks keep their sub-issue, removed
> tasks get their sub-issue closed, only genuinely new tasks create one.

Equivalently: the number of `gh_subissue` `:created` outbox rows always equals
the number of distinct `plan_key`s ever planned, and every still-open sub-issue
belongs to a live task.

### Rejected alternatives

* **Derive the outbox key from the title instead of `task_id`.** Fixes the
  duplicate-create but not the lost `gh_issue_number`, and leaves the deleted
  rows unaddressable so nothing can close the old sub-issues.
* **Close-then-recreate every sub-issue on replan.** Correct counts, but churns
  GitHub (27 closes + 27 creates), loses comment history on an unchanged task,
  and punishes the common case (a plan that barely changed).
* **Store the task→issue mapping in a separate table that survives the delete.**
  Keeps the mapping but still re-creates sub-issues, because the key is
  `task_id`-derived and the new row is a genuinely different task as far as the
  projection is concerned.
* **Suppress projection when the plan hash is unchanged.** A cheap guard, not a
  fix: one reworded title re-opens the whole bug for the untouched tasks.
