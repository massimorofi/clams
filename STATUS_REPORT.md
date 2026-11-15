# Clams pipeline recovery — status report

Date: 2026-11-15. Repo: `massimorofi/clams`, branch `main`. Suite: **513 passed**.

All 14 planned issues (#114–#127) are fixed, committed, tested and closed on
GitHub. Two further bugs were found while verifying the fixes live; they are
filed as #128 and #129 (one already fixed).

## The pipeline is running again

Verified on the live deployment after the fixes, not just in tests:

* both agent containers report **healthy** (they were permanently `unhealthy`);
* the orchestrator cycles with `errors=0`;
* the stale reaper re-dispatched the abandoned tasks (45, 47, 48, 110 —
  `attempts` 0 → 1), the executor consumed them, and **task 45 completed** with
  real agent work against the correct Claudio working tree;
* requirements claudio#36 and claudio#40 finally left `dispatched` and are now
  `blocked` — the review gate fires, which had never happened before;
* the outbox drains to 0 pending.

## Root causes, in order of damage

**#114 — nothing was ever acked.** `read_task`/`read_result` returned the decoded
object and dropped the message id, so no consumer could ack after processing.
Every message stayed in the Redis PEL and a restart lost the work in flight.
Added a `Delivery` handle with `take_*`/`redeliver_*` helpers, `XREADGROUP … 0`
redelivery and `XAUTOCLAIM`; `InMemoryBus` now models the PEL so the bug is
testable without Redis. The executor re-runs its PEL on start-up; the
orchestrator re-records unacked results.

**#115 — Claudio tasks ran against the Clams repo.** The default project's
dispatch scope was the orchestrator's cwd. `_default_scope()` now resolves the
registered project's provisioned worktree, and an unprovisioned project is
skipped with an alert instead of silently running in the cwd. Verified live:
`work/p2-task-45` was a worktree of the Claudio clone at Claudio's commit.

**#116 — the executor had no working shell.** The agent config enabled
host-execution over a socket `dev.sh` never mounted, so `run_command` failed for
essentially every task. `dev.sh` now mounts `CLAMS_HOST_EXEC_SOCKET` when
present and otherwise forces sandboxed execution.

**#117 — abandoned tasks were never retried.** `step_dispatch` only looked at
`planned` items, so a `dispatched` requirement was never revisited. New
`step_reap_stale` re-dispatches tasks whose lease expired, and at the attempt cap
publishes a failure so the gate blocks the requirement.

## Remaining fixes

| Issue | Fix | Commit |
|---|---|---|
| #119 | Prose acceptance criteria were executed as shell commands (exit 127 on every task). Only `run:`/`cmd:`-marked lines run now; prose is recorded as not machine-verifiable. | `0fa86fd` |
| #122 | Lease (900s) was shorter than the task budget (1800s) → double-runs and a lying status badge. Default is now 2100s, `Settings.load()` rejects a short lease, and the executor renews its claim while a task runs. | `dd74e55` |
| #118 | The "already claimed" path built a result and discarded it. It now publishes. | `dd74e55` |
| #120 | `xpending(name, group, '-', '+', 100)` raised `TypeError` on every call and the exception was swallowed, so queue age always read 0. Now `xpending_range`, errors surface as a failing check, and the prune uses live claim paths. | `a08e5e7` |
| #125 | Tokens baked into clone remote URLs. Added `scrub_remote_tokens`, `clams scrub-credentials`, and a critical watchdog check. | `a08e5e7` |
| #124 | A healthy watchdog sweep resumed an operator's emergency stop. Pause provenance is now persisted in a `runtime_state` table and the watchdog re-applies the operator pause. | `a69bd32` |
| #123 | "Pause project" wrote a flag nothing read. `project_paused()` now gates intake, plan, dispatch and reap; the dead `clams:planner-actions` publish was removed. | `4a1b520` |
| #121 | The image HEALTHCHECK probed port 8100 while the agents listen on 1072/1073, so every container read `unhealthy`. `dev.sh` overrides the healthcheck with the real port. | `1cbfcf1` |
| #126 | Data repair applied: 29 stale PEL entries purged, dead worktree registrations and 9 empty `task/*` branches removed. DB backed up first. | `30a8477` |
| #127 | 76 duplicate runaway task sub-issues closed on Clams (keeping 88–94 and 106–111); Claudio 43/44/46/49–52 closed, 45/47/48 left for the reaper. | `19fad73` |

## New bugs found while verifying

**#129 (fixed, `9bc547c`) — the worktree cleaner deleted the base clones.**
`cleanup_stale_worktrees` removed *every* directory under `work_root` not in the
active set, which included `work/projects/` — the persistent clones every
worktree is cut from. The watchdog's auto-recover wiped them, and every
subsequent task failed with "is not a git repository". Only `p<n>-task-<n>`
directories are eligible now, removal goes through `git worktree remove`, and
`projects/` is never touched. Found live; 4 regression tests added.

**#128 (open) — a missing working tree is never re-provisioned.**
`build_project_contexts` only provisions when `worktree_path` is empty, so a
recorded-but-deleted tree never self-heals. Restoring service required clearing
the paths and restarting. Needs `if not worktree or not Path(worktree).exists()`.

## Things the operator must still do

1. **Rotate the GitHub token** that was written into clone configs during the
   earlier provisioning. Scrubbing the files does not un-leak it.
2. Requirement clams#105 is still `dispatched` (tasks 110/111); it will resolve
   through the reaper now that the clones are back.

## Notes on the work

* Two commits (#114, #115) did not land when first attempted and their content
  was swept into later commits (`28e405b`, `a08e5e7`, `65c0e83`). The issue
  comments reference where the code actually is.
* The plan described the working tree as clean; it was not. A large body of
  uncommitted issue #18/#15 work — including `src/clams/project_setup.py`, which
  the code imports — was committed as `65c0e83` because nothing runs without it.
