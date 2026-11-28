# Workflow recovery runbook

One command (`recover`) brings a stuck workflow back into the pipeline and cleans
up the GitHub sub-issue clutter left behind by earlier replans. It replaces the
hand-run procedure that was used for claudio#58 (close 8 orphans + 1 stale
success by hand, then POST a replan command).

- [When to use](#when-to-use)
- [What recovery does](#what-recovery-does)
- [Triggering recovery](#triggering-recovery)
- [Idempotency and audit trail](#idempotency-and-audit-trail)
- [What recovery never touches](#what-recovery-never-touches)
- [Retired manual recipe](#retired-manual-recipe)

## When to use

Reach for `recover` when a workflow is **`blocked`** or **`failed`** and you want
it retried from where it actually stopped:

- The planner exhausted its replans, a task failed hard, or planning produced an
  invalid plan, so the workflow sits in `blocked`/`failed` with an open question
  already answered or a fix now available.
- The parent issue has **orphan** sub-issues: open `clams:task` children that no
  live task references any more (left behind by the pre-`plan_key` replan bugs).
- The parent issue has **stale-success** sub-issues: children whose task already
  reached `success` but which were created before auto-close-on-success existed,
  so they are still open.

`recover` is valid **from `blocked`/`failed` only**. On a workflow in any other
status the command is rejected (`workflow <id> is <status>, not blocked/failed`)
— with one deliberate exception: re-running `recover` on a workflow already
handed back to the planner (`planning`/`running`) whose sweeps come back empty is
a recorded **no-op**, not an error. See
[Idempotency and audit trail](#idempotency-and-audit-trail).

## What recovery does

`recover` is one command that runs three steps in a single planner transaction,
in this order:

1. **Orphan sweep.** Every **open** sub-issue that **no task of the workflow
   references** (and that no live task in another workflow owns) is queued for
   closure with a *"superseded by recovery"* comment:

   > Clams: superseded by recovery — no live task owns this sub-issue, so the
   > recovery sweep closed it.

   Nothing owns an orphan, so closing it is pure clutter removal.

2. **Stale-success sweep.** Every **open** sub-issue whose task has already
   reached **`success`** is queued for closure with the standard success note
   (*"Clams: Task succeeded — closing this sub-issue (recovery sweep)."*). These
   are leftovers from before auto-close-on-success existed: the work is done and
   merged, the child issue just never got closed. The task row is left exactly as
   it is — only the sub-issue is closed.

   Both sweeps go out as `gh_close` rows on the **outbox**, never as a direct
   GitHub call inside the transaction (invariant 3). The relay is the only path
   out, so a crash between enqueue and send cannot double-close.

3. **Replan through `reconcile`.** Recovery has no LLM plan, so it rebuilds the
   plan from the task rows that still represent work (`pending`, `ready`,
   `dispatched`, `running`, `waiting_input`, `failed`, `blocked`) and pushes that
   tail through `planner.reconcile.apply_replanned_plan` — the same reconciler a
   normal replan uses. Because reconcile matches by **`plan_key`** (normalised
   title), not by `task_id`:

   - **success tasks keep their identity** — same `task_id`, same
     `gh_issue_number`, same `status`, no new sub-issue, no re-projection;
   - **only the failed/blocked tail is re-planned** — it is re-numbered 1..N and
     re-dispatched, which is what makes this a **retry from the last successful
     step** rather than a restart from scratch;
   - removed tasks are `cancelled`, never deleted.

   The tail is pushed through the reconciler **even when it is empty**, so
   "identity preserved" is proven on every recover, not only the ones that
   happened to have work left.

Finally the workflow status is set to **`planning`**, which is the state the
planner loop picks up: that hand-back *is* the retry. From here the normal
plan → dispatch → review pipeline runs, and the board card moves out of
*In review* back to *In progress*.

## Triggering recovery

### From the UI

Open the workflow (**Overview → click a workflow**, i.e. the workflow detail
panel). When — and only when — the workflow status is `blocked` or `failed`, the
detail **header** shows a **`Retry workflow`** button next to the existing
`Pause` / `Cancel` actions. Clicking it opens a **"Retry this workflow?"**
confirmation dialog that previews, before anything is queued:

- how many **orphan** sub-issues the sweep will close,
- how many **stale-success** sub-issues it will close,
- how many tasks will be re-planned (the tail) and how many `success` tasks are
  preserved untouched.

Confirming submits the `recover` command and the panel shows
*"Recovery command queued (pending) — waiting for the planner to apply it."*
The planner drains the command queue on its next cycle; the timeline then fills
with the recovery events below.

### From the API

The command goes to the same endpoint every operator command uses:

```bash
curl -X POST $CLAMS_API/api/v1/commands -d '{"kind":"recover","target":"<workflow_id>","issued_by":"operator"}'
```

In a real deployment the API is authenticated and JSON-typed, so add the two
headers (and use the workflow id, not the GitHub issue number):

```bash
curl -X POST $CLAMS_API/api/v1/commands \
  -H "Authorization: Bearer $CLAMS_API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"kind":"recover","target":"<workflow_id>","issued_by":"operator"}'
```

`recover` is a permitted command `kind` (migration `016_commands_recover_kind.sql`).
A `202` means the command was **queued**, not applied:

```json
{"command_id": 471, "status": "new"}
```

An unknown kind, a missing `target`/`issued_by`, or a workflow that is not
`blocked`/`failed` comes back as `422` with `{"error": "..."}`.

### Reading the result

**Command status** — poll the command by id. It moves `new` → `applied` (or
`rejected`), with `applied_at` and the structured `result` of the recovery:

```bash
curl -s -H "Authorization: Bearer $CLAMS_API_TOKEN" $CLAMS_API/api/v1/commands/471
```

```json
{"command_id": 471, "kind": "recover", "target": "wf-1a2b3c", "issued_by": "operator",
 "status": "applied", "applied_at": "2026-11-28T09:14:02Z",
 "result": {"workflow": "wf-1a2b3c", "status": "planning",
            "orphans_closed": [221, 222, 223, 224, 225, 226, 227, 228],
            "stale_success_closed": [219],
            "orphan_count": 8, "stale_success_count": 1,
            "tasks_preserved": 5, "replanned": 3,
            "tasks_created": 0, "tasks_reused": 3, "tasks_cancelled": 0,
            "outbox_rows": 9, "noop": false}}
```

**Events** — the workflow timeline (UI) or the SSE stream (scripted) carries the
same story as human-readable lines:

```bash
curl -sN -H "Authorization: Bearer $CLAMS_API_TOKEN" \
     -H 'Last-Event-ID: 0' "$CLAMS_API/api/v1/events?max_events=200"
```

Look for, in order:

- `replan requested by operator`
- `workflow recovered: <workflow_id> by operator — closed 8 orphan(s), 1 stale-success sub-issue(s), replanned 3 task(s)`
  (the event `data` holds the same counters as the command `result`)
- a `recovery: nothing to recover by operator` line instead, when the run was a
  no-op.

**GitHub side** — the sweeps land as soon as the outbox relay drains: each swept
sub-issue gets one comment (the superseded / succeeded note) and is closed.
Verify live with:

```bash
docker exec clams-planner-1 sqlite3 /data/clams.db \
  "SELECT status,count(*) FROM outbox WHERE kind='gh_close' GROUP BY status;"
# per-sub-issue proof (the idempotency key names the workflow + issue number):
docker exec clams-planner-1 sqlite3 /data/clams.db \
  "SELECT idempotency_key,status FROM outbox WHERE kind='gh_close' ORDER BY outbox_id DESC LIMIT 12;"
```

## Idempotency and audit trail

**Re-running `recover` is safe.** That is what makes it usable as an operator
escape hatch under pressure — a double-click, a retried `curl`, or a second
operator acting on the same workflow cannot make things worse.

- **Empty sweeps are a no-op.** If both sweeps come back empty, recovery queues
  *nothing*: zero `gh_close` outbox rows, zero task writes, zero status changes,
  and one timeline event (`nothing to recover by <operator>` /
  `recovery: nothing to recover by <operator>`). A second `recover` on a workflow
  already in `planning`/`running` therefore returns
  `{"noop": true, "orphan_count": 0, "stale_success_count": 0, "outbox_rows": 0}`
  instead of a rejected command.
- **Dedup is keyed, not best-effort.** Every close is enqueued with a stable
  idempotency key — `gh:gh_close:<workflow_id>:<issue_number>:orphan` and
  `gh:gh_close:<workflow_id>:<issue_number>:stale_success` — and `outbox.idempotency_key`
  is `UNIQUE`. A repeat of the same sweep is swallowed by the dedupe, so GitHub
  never sees a second comment-and-close for the same sub-issue.
- **Reconcile is idempotent.** The plan tail goes through the same `plan_key`
  reconciler as any replan: re-applying an unchanged tail reuses task rows
  (`tasks_reused`) and creates nothing (`tasks_created: 0`), so it cannot mint
  duplicate sub-issues.
- **State guard.** Outside `blocked`/`failed` (and outside the empty-sweep
  `planning`/`running` case) the command is rejected rather than half-applied.

**Audit trail.** Nothing recovery does is invisible:

| Where | What you can read back |
|---|---|
| `commands` table / `GET /api/v1/commands/{id}` | the `recover` row: `issued_by`, `target`, `created_at`, `status` (`new` → `applied`/`rejected`), `applied_at`, and the full `result` JSON (issue numbers closed, counts, `outbox_rows`, `noop`) |
| `events` / `GET /api/v1/events` (SSE) + UI timeline | `replan requested by <operator>` and `workflow recovered: … closed N orphan(s), M stale-success sub-issue(s), replanned K task(s)`, each with the same counters in `data` |
| `outbox` | one `gh_close` row per swept sub-issue, with its idempotency key and send status |
| GitHub | the comment left on each closed sub-issue, naming recovery as the reason |

`issued_by` is whatever you put in the command (`operator`, your username, a
ticket id), so the trail says who pulled the lever.

## What recovery never touches

**Unmerged branches and task history are untouched.**

- **No git operations at all.** Recovery never deletes, resets, rebases or pushes
  a branch — not a task branch whose work is unmerged, not a merged one, not a
  PR. If a failed task left a branch with useful commits, it is still there,
  exactly as it was, and you can cherry-pick or open a PR from it by hand.
- **No task history is rewritten or dropped.** Task rows are never `DELETE`d.
  Finished `success` tasks keep their `task_id`, `gh_issue_number`, status,
  attempts and evidence; tasks that fall out of the plan are marked `cancelled`,
  which keeps them in the record. Every transcript, gate result and review
  verdict stays queryable.
- **No GitHub history edits.** Comments, labels and the parent issue body are
  left alone; recovery only ever *closes* the sub-issues it swept.
- **No LLM re-planning of completed work.** The `success` prefix of the plan is
  never re-run, so already-verified work cannot be redone or regressed.

If a branch really must go, that is a separate, deliberate cleanup — not a
side-effect of recovery.

## Retired manual recipe

> **This hand procedure is no longer the supported path.** It is kept here only
> so old notes, tickets and commit messages (notably **claudio#58**) can be read.

Before `recover`, an operator had to do the cleanup by hand and then queue a
replan:

1. Work out which open sub-issues were orphans and which were stale successes —
   `gh issue list --label clams:task --state open` compared against the task list
   in the UI, by eye.
2. Close them one at a time with a manual comment (in claudio#58: **8 orphans +
   1 stale success**, ten `gh issue close` calls).
3. `POST` a `replan` command for the workflow and hope the reconciler matched
   titles cleanly.

Every step was manual, un-audited and easy to half-do: miss one orphan and the
board stays cluttered, run the replan first and the sweep re-creates the mess,
re-run the closes and you spam ten comments.

**Now: one command.** `POST /api/v1/commands` with `{"kind":"recover", …}` (or the
**Retry workflow** button) performs the orphan sweep, the stale-success sweep and
the replan through `reconcile` in one transaction, idempotently, with the audit
trail above. See [Triggering recovery](#triggering-recovery).

Related: [GitHub setup — replan and sub-issue idempotency](github.md),
[Deployment & state machines](deployment.md), [Quickstart](quickstart.md).
