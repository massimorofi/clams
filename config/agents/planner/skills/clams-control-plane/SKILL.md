---
name: clams-control-plane
description: Read-only visibility into the Clams orchestrator (workflows, tasks, executor activity, transcripts, event log) via the Clams control-plane API. Use whenever asked about current workflow state, what an executor is doing, failure causes, or remaining-time estimates.
allowed-tools: run_command web_fetch
---

# Clams control plane (read-only)

The Clams API is reachable from this container and is the authoritative source
for orchestrator state. Use it instead of guessing from git or GitHub issues.

Credentials are in the environment: `$CLAMS_API_URL` (e.g. `http://api:8200`)
and `$CLAMS_API_TOKEN`. Always send the bearer token:

```
curl -s -H "Authorization: Bearer $CLAMS_API_TOKEN" "$CLAMS_API_URL/api/v1/<endpoint>"
```

This API is READ-ONLY for you. Never POST to `/commands` or mutate anything;
proposals go to the operator as text/files, and the operator applies them.

## Endpoints

- `GET /api/v1/overview` — planner heartbeat, scheduling paused?, schema,
  task counts by status, executors (online, capacity, last seen), open alerts.
- `GET /api/v1/workflows` — all workflows (status, goal, replans).
- `GET /api/v1/workflows/{id}` — workflow + its tasks in order
  (status, attempt, depends_on, timestamps).
- `GET /api/v1/tasks/{id}` — one task: brief (objective/acceptance/run gates),
  `result` (evidence gate checks incl. failing command + output tail),
  `heartbeat_json` (turns_used, elapsed_s, progress_ts), lease, branch, session.
- `GET /api/v1/tasks/{id}/transcript` — the live Claudio session of the task:
  last tool calls / reasoning of the executor RIGHT NOW. Non-invasive: it only
  reads the session log.
- `GET /api/v1/events` — operational event log (dispatches, failures,
  requeues, settings/prompt patches). Filter client-side by `ref`.

## Answering the common questions

**"What is the executor doing?"** — find the `running` task in the workflow
detail, then read its `/transcript` tail and `heartbeat_json`. A fresh
`progress_ts` means real work; frozen progress + lease near expiry means the
watchdog will send evidence to the planner.

**"Why did a task fail?"** — `result.evidence.checks[]` shows each gate command
with exit code and output tail. Distinguish: gate/environment failures
(ModuleNotFoundError, missing pytest, scope violations) vs genuine code
failures (assertion errors in the feature's own tests).

**"How much time remains?"** — from the tasks table compute per-completed-task
durations (`started_at` → `updated_at`, note attempt count), then estimate
remaining tasks as mean-duration × (pending + failed-attempts-left). State
your assumptions and the observed variance; say "estimate" explicitly.

**Queue waits** — with one executor (cap=1) tasks serialize; a `dispatched`
task that is not `running` is queued behind the current one, not stuck.

## Ground rules

- Quote real numbers from the API (task ids, attempts, timestamps) — never
  reconstruct state from commit logs or issue titles.
- If the API is unreachable, say so plainly; do not fall back to guessing.
- Your proposed plans are PROPOSALS. To apply one, the operator uses the
  `apply_plan` command (POST /api/v1/commands, kind `apply_plan`,
  payload `{"workflow_id": ..., "plan": {"tasks": [...]}}`); you must not
  call it yourself.
