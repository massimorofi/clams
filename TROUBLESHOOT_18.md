# Clams pipeline failure — diagnosis and fixes

Date: 2026-11-14. Deployment: `massimorofi/clams` + `massimorofi/claudio`,
stack managed by `scripts/dev.sh` (backend :8200, planner agent :1072,
executor agent :1073, worker, watchdog, frontend :5173).

## Symptom

Both projects were registered and polled, but every orchestrator cycle reported
`intake=0 planned=0 dispatched=0`, the global badge sat in **Error**, and the
alert list was full of criticals.

## What was actually wrong

Five independent defects, stacked. Polling itself was never broken — the log
shows both repos being fetched with `labels=status:Ready` every 20s, HTTP 200.

### 1. Every work item was paused, and resume could not un-pause it (the blocker)

`step_plan` and `step_dispatch` both skip items with `user_paused`. All 71 rows
had `user_paused = 1`.

The chain that produced it:

- `watchdog._step_pause_and_notify()` ran a blanket
  `UPDATE work_items SET user_paused = 1` on **any** critical, and nothing ever
  reversed it.
- The critical was a false alarm: `check_heartbeat()` went critical on its very
  first read, so leftover heartbeat files from the previous run (planner 23871s
  old) tripped it seconds after boot, before the orchestrator's first cycle.
- The operator's "Resume" could not undo it: `api.py` looped
  `set_paused(it.requirement_number, False)` without a project id, so it
  defaulted to `project_id = 0` and silently no-opped on every row of the real
  projects (1 and 2).

Net effect: a routine restart permanently froze the pipeline with no UI
affordance to recover.

**Fix** — `db.py` gained `set_paused_all()` and `set_paused_for_requirement()`;
`api.py` and `steering.py` now use them so pause and resume are symmetric and
project-correct.

### 2. The watchdog had no grace period and no de-duplication

`check_heartbeat()` was the only check without a consecutive-failure counter
(the LLM and GitHub probes have one), and `raise_alert()` inserted a new row on
every 15s sweep. Result: 206 alerts, 100 unacked, and `compute_state()` returns
`Error` whenever any critical is unacked.

**Fix** — added `heartbeat_failures_critical` (3), `startup_grace_seconds` (120)
and `alert_cooldown_seconds` (900); the watchdog now records that *it* paused
dispatching and lifts exactly that pause once checks are healthy again, leaving a
deliberate operator pause alone.

### 3. The executor could never run a task: `409 approval_required`

`ClaudioTaskRunner` called `/chat` with no `approval_mode`, so the agent used
its `on_request` default and paused at the first tool call. Reproduced directly:

```
{"error":{"code":"approval_required","message":"The run paused for approval;
 use the streaming endpoint.","retryable":false}}
```

An unattended executor has nobody to approve, so every task was claimed,
conflicted and released. **Fix** — `executor.approval_mode` defaults to `off`
and is passed through. Verified: the agent then ran the task with real tool use
(GitHub search, many LLM calls) instead of 409.

### 4. Timeout mismatch killed every task that did real work

The task budget was `max_wall_seconds = 1800` but the HTTP call used the client
default of 120s, so working tasks came back `claudio chat timed out`. On top of
that, the agent's own `/chat` handler cancels a run after
`llm.timeout_seconds * 2` = 600s ("Root node claudio was cancelled" → HTTP 500).

**Fix** — the chat timeout now follows the task's own wall budget, and
`.env.agent` sets `CLAUDIO__LLM__TIMEOUT_SECONDS=900` so the agent ceiling
(1800s) sits above the Clams budget instead of below it.

### 5. A crashed run left a worktree that blocked all retries

`create_worktree()` raised `worktree path already exists` if the directory was
present. A killed run leaves it behind, so task 91 failed instantly on every
retry and the requirement went `blocked`. **Fix** — prune the stale checkout and
retry.

### 6. The executor agent could not write to the worktree at all

The agent container mounted only `/workspace` (`.dev/agents/executor`), while
Clams hands it a brief naming a worktree under the *resolved* work root
(`/run/media/briggen/DEV/code/ai/clams/work/...`). The agent therefore failed
every write:

```
tool write_file raised: [Errno 13] Permission denied: '/run/media'
```

A task could run for ten minutes, do real reasoning, and still produce no
artifact. **Fix** — `dev.sh` now bind-mounts the repo at its own resolved path
(`$(pwd -P):$(pwd -P)`), which is the path Clams writes into the brief. Verified
writable from inside the container.

### 7. The 600s agent ceiling came from a config file, not the environment

`api/chat.py` waits `supervisor.config.llm.timeout_seconds * 2`. The agent
entrypoint seeds `config/config.yaml` from the shipped example on first boot,
and that file sets `timeout_seconds: 300` → a hard 600s ceiling, after which the
run is cancelled ("Root node claudio was cancelled") and `/chat` returns 500
`Run failed`. `CLAUDIO__LLM__TIMEOUT_SECONDS` did **not** override it, because
the seeded file wins.

**Fix** — `.dev/agents/<role>/config.yaml` now holds a real config with
`timeout_seconds: 900` (→ 1800s ceiling), bind-mounted read-only into the
container by `dev.sh`. Verified live: a task then ran **1323s and beyond**, past
the old 600s cutoff, and the agent wrote a real artifact
(`doc/configuration.md`) into the worktree.

### 8. An unplannable requirement retried forever

`step_plan` called `plan_requirement` with no failure bookkeeping. A requirement
the decomposer rejects — e.g. one too small to split into `MIN_TASKS = 3` tasks —
failed identically on **every cycle**: two LLM calls every `poll_interval`, an
error line each time, item stuck in `claimed` indefinitely. Observed live with a
deliberately trivial test requirement (#104): `plan has 1 task(s); the spec
requires at least 3`, repeating.

**Fix** — `orchestrator._plan_one` increments `replans` on failure and, once
`limits.max_replans` is reached, marks the requirement `blocked` and raises a
single alert instead of looping.

## Data repair

`scripts/repair_state.py` (dry-run by default, `--apply` to act) cleaned the
poison left by the pre-fix code:

- **70 bogus work items deleted** — 65 of the 68 remaining were planner-generated
  *task sub-issues* (`Parent: #n`) that had been intaken as requirements before
  the `_is_task_subissue` filter existed, plus 3 legacy `project_id = 0` rows.
  Planning a task decomposes it into sub-tasks, which is what flooded the repo.
- **94 failed outbox rows purged** — written before payloads carried
  `project_id`, so clams issues 81–85 were posted against claudio and returned
  404 (confirmed: clams #81–85 → 200, claudio #81–85 → 404).
- **100 alerts acknowledged**, every work item resumed.

Only requirement **#16** is a genuine open requirement. The ~30 `status:Ready`
issues in clams are all task sub-issues, so `intake=0` for them is **correct**
behaviour, not a bug. claudio has no `status:Ready` issues at all, so its `0`
is also correct.

## Verified after the fixes

| | before | after |
|---|---|---|
| global state | `Error` | `Idle` / `Executing` |
| unacked alerts | 100 | 0 |
| outbox failed | 94 | 0 |
| work items paused | 71 | 0 |
| executor | 409 on every task | claims, runs, writes artifacts |
| agent run ceiling | 600s | 1800s (ran 1323s+ live) |

Both projects are polled every cycle (`claudio` and `clams`, HTTP 200), and
outbox side effects now land on the **owning** repo — the log shows
`POST /repos/massimorofi/clams/issues/92/comments → 201 Created`.

Live end-to-end evidence: requirement **#104** was created on GitHub, picked up
by intake (`intake=1`), and driven through the planner; requirement **#16** was
claimed, planned, dispatched, executed by the agent (which wrote
`doc/configuration.md` into the task worktree) and its result published back to
GitHub with a status transition.

## Verification

- Unit/integration suite: **439 passed** (`pytest tests/`).
- Full-pipeline harness: **33/33 checks passed**
  (`scripts/e2e_workflow_test.py`), including Part 4 which drives
  intake → plan → dispatch → execute → review across **two projects** with real
  git clones, and asserts no runaway re-plan and correct project ownership.
  Three Part 3 assertions were updated: they required claiming a live
  `status:Ready` issue, but every such issue in clams is now a task sub-issue
  that intake *must* skip.

### 9. `status:Ready` meant two different things (why "Ready issues are not picked up")

`status:Ready` is intake's trigger for "unclaimed **requirement**"
(`intake.py` sweeps `labels=[status:Ready]`). But `decompose.py` also labelled
every planner-generated **task** with `status:Ready`, meaning "ready to execute".

So the board showed **89 open `status:Ready` issues in clams — all 89 of them
task sub-issues, and 0 genuine requirements** (claudio likewise had 10, all
tasks). Intake was right to skip every one: claiming a task would decompose a
task into sub-tasks. The label, not the intake logic, was lying.

**Fix** — tasks are now created with `status:In Progress` instead of
`status:Ready` (`decompose.create_sub_issues`), and
`scripts/relabel_task_issues.py` cleaned up the existing backlog: **89 clams +
10 claudio** task issues relabelled. Both repos now report **0 open
`status:Ready`**, which is the truth.

Proof the pipeline does pick up real work: after the fix, Clams independently
intaken, planned and dispatched three genuine requirements — clams **#105**
("Kanban Status Correct Source", tasks 106–111), claudio **#36** (tasks 43–46)
and claudio **#40** (tasks 47–52). All three are `parent: False` (real
requirements) and now sit at `status:In Progress`.

### 10. The alert feed could never forget, and a busy orchestrator looked dead

Two separate reasons the Activity list stayed full:

- **No purge existed.** `/api/v1/alerts` returns `list_all()` regardless of
  `acknowledged`, so acknowledging only flips a row to "ACKED" and the 206
  historical alerts (200 from the old outbox-404 storm, 6 from the watchdog
  storm) stayed in the feed forever. Added `AlertRepo.purge()`,
  `DELETE /api/v1/alerts`, `clams clear-alerts`, and a "Clear acknowledged"
  button. **All 219 rows deleted.**
- **A self-recovered blip still left the badge in Error.** Observed live: the LLM
  engine hiccuped, the watchdog paused dispatching and raised two criticals, then
  recovered on its own — but the criticals stayed unacked, so `compute_state()`
  reported Error for a problem that had already cleared. Recovery now
  auto-acknowledges the criticals it raised.
- **The heartbeat was written last.** `step_heartbeat` was the final step of a
  cycle, so any cycle spending minutes in LLM planning or review never refreshed
  it; with `heartbeat_max_age = 60` the watchdog declared a healthy orchestrator
  dead, paused dispatching and escalated. Fixed by beating at cycle start,
  raising `heartbeat_max_age` to 300s, and making the escalation message
  volatile-detail-free so the 900s cooldown actually matches (it embedded
  "planner (301s old)", so every sweep looked like a new alert).

## What remains

- **Requirement #16 is `blocked`.** Its tasks are large documentation jobs and
  the local model does not finish one inside `max_wall_seconds = 1800`. The
  pipeline handled this correctly (timeout → result published → status
  transitioned); the task is simply too big for the current engine. Smaller
  requirements, or a faster model, will complete.
- The clams repo still carries ~30 stale `status:Ready` task sub-issues from the
  earlier runaway. Correctly ignored now, but closing them would make the board
  readable.
- Tasks run serially on one executor, so throughput is bounded by the agent.
- `runtime["global_paused"]` is in-memory and resets on restart while
  `user_paused` persists — worth reconciling if the topbar pause should survive a
  restart.

## Regression coverage

`tests/test_pause_deadlock.py` reproduces the freeze deterministically (no LLM,
no network): it asserts the pre-fix scoped-resume no-op, and drives the real
`/api/v1/planner/action` handler to prove global stop and resume now move every
project's rows. `tests/test_worktree.py` gained the stale-checkout retry case.
Full suite: **439 passed**.
