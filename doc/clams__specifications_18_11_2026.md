# Clams — Reverse-Engineered Specification

**Analyst:** Claudio · **Date:** 2026-11-18 · **Repo:** `/home/briggen/Dev/code/ai/clams` (`massimorofi/clams`)
**Method:** read of the actual sources (`src/clams/*.py`, `docker-compose.yml`, `config/`, `frontend/`, `tests/`)
cross-checked against the in-repo docs (`README.md`, `PLAN.md`, `STATUS_REPORT.md`,
`doc/deployment_architecture.md`, `TROUBLESHOOT_18.md`). Where the docs and the code disagree,
**the code wins** and the divergence is called out in §12.

> Upstream requirement source: `massimorofi/claudio#5` —
> *"Claudio Autonomous Multi-Agent System: System Requirements & Architecture Specification v1.1.0"*.

---

## Contents

0. [Executive summary](#0-executive-summary)
1. [What Clams is (scope and boundary)](#1-what-clams-is-scope-and-boundary)
2. [System architecture — the four planes](#2-system-architecture--the-four-planes)
3. [Runtime topology (processes, containers, ports)](#3-runtime-topology-processes-containers-ports)
4. [Data model and durable state (SQLite)](#4-data-model-and-durable-state-sqlite)
5. [Message contracts and the event bus](#5-message-contracts-and-the-event-bus)
6. [The issue-processing workflow, end to end](#6-the-issue-processing-workflow-end-to-end)
7. [Component reference](#7-component-reference)
8. [The HTTP + SSE API and the web console](#8-the-http--sse-api-and-the-web-console)
9. [Supervision: watchdog checks, alerts, remediation ladder](#9-supervision-watchdog-checks-alerts-remediation-ladder)
10. [Configuration reference](#10-configuration-reference-clams)
11. [Limits, budgets and safety rails](#11-limits-budgets-and-safety-rails)
12. [Doc-vs-code divergences and known gaps](#12-doc-vs-code-divergences-and-known-gaps)
13. [Evidence index](#13-evidence-index)

---

## 0. Executive summary

**Clams (Claudio Multi-Agent System)** is a standalone Python orchestrator that turns
**GitHub Issues into a durable work queue** and drives an autonomous
*intake → plan → dispatch → execute → review → deliver* pipeline against a real repository,
using **two decoupled Claudio agent instances** (a *Planner* and an *Executor*) for the only
two steps that need a model, and **deterministic Python** for everything else — including a
**zero-LLM watchdog** that supervises the whole system.

The core design bets, all visible in the code:

1. **LLMs are untrusted producers.** Every model output is schema-validated before it can
   cause a side-effect: plans must parse as strict JSON and pass `validate_plan`
   (`decompose.py`), bus payloads are validated on publish *and* consume (`contracts.py`),
   and a review only passes on an unambiguous `PASS` (`planner.judge_review`).
2. **Evidence beats claims.** The executor runs the verification commands *itself*
   (`run_command_logged`), not the model, and `review.verify_evidence` re-checks exit codes,
   non-empty stdout, a commit, changed files and a clean tree before anything advances.
3. **Crash-consistency via a transactional outbox.** No GitHub write ever happens inside a
   state-change transaction; side-effects are rows in `outbox` with an `idempotency_key`,
   delivered later by `OutboxWorker` (at-least-once, deduped by key).
4. **Durable queues with real acks.** Redis Streams + consumer groups; messages are acked
   only *after* the work is recorded, and both the executor and the orchestrator replay
   their own PEL on start-up (`redeliver_tasks` / `redeliver_results`). This was bug #114.
5. **Bounded failure.** Hard budgets enforced in code: `max_attempts=3`, `max_turns=40`,
   `max_wall_seconds=1800`, `max_replans=3`, plus lease-expiry reaping and a watchdog
   remediation ladder that escalates to a human instead of looping forever.
6. **Multi-project from day two.** Every work-plane row is keyed by the composite
   `(project_id, <number>)` because GitHub issue numbers are repo-scoped, not global
   (`db.py` module docstring, issue #18).

**State of the project (as of the repo's own status report, 2026-11-15):** the pipeline runs
end-to-end on a live deployment; **513 tests pass**; 14 pipeline bugs (#114–#127) plus #129
were fixed in that recovery. One known-open bug remains (#128: a deleted working tree is
never re-provisioned). See §12.

**Reality check on the docs:** `README.md` lists 17 modules and `PLAN.md` 18.
`src/clams/` actually contains **25** (9,493 lines), and `tests/` holds 34 files / 7,042 lines
with **513 tests collected**. The deployment has grown from the 5 compose services the README
names to **8** — the old "planner"/"executor" pair is now four containers (`planner` + `agent`
stock Claudio, `orchestrator` + `executor` Clams loop and driver) plus a React `frontend` on
`:8200`. Multi-project provisioning, the agent registry and the HTTP/SSE console are absent
from the README's layout table entirely. §12 lists every divergence found.

---

## 1. What Clams is (scope and boundary)

From `PLAN.md` §2, still accurate against the code:

| In scope | Out of scope |
|---|---|
| Work queue, GitHub sync, handoffs, leases, outbox | Changes to the core `claudio` agent |
| Planner/Executor orchestration + prompts | Model training / conversion |
| Watchdog, alerts, remediation ladder | Multi-host / distributed scheduling |
| Nginx gateway `:8100` → Planner Web UI | Auth/SSO beyond a bearer token |
| `strixper` engine health integration | Replacing `strixper` |

**Upstream spec:** `massimorofi/claudio#5` v1.1.0. The code cites the spec by section in its
docstrings (`spec §4.2` for contracts, `§5/§8.3` for the DB, `§6` for the watchdog,
`§8.1` for interrupt priority, `§8.2` for the anti-prompt-pollution rule, `§10` for the CLI).

**Functional requirement map** (`PLAN.md` §4, verified against the modules):

| Req | Meaning | Code |
|---|---|---|
| FR-1 | Interactive chat & steering via Planner UI | `steering.py`, `planner.py`, `api.py` |
| FR-2 | Autonomous intake (`Ready` → `In Progress`, atomic lock) | `intake.py`, `db.claim_requirement` |
| FR-3 | Decomposition into 3–7 native GitHub sub-issues | `decompose.py`, `gh.add_sub_issue` |
| FR-4 | Isolated execution in git worktrees + evidence | `executor.py`, `worktree.py` |
| FR-5 | Verification & evidence gate → `Review` | `review.py` |
| FR-6 | Fault tolerance & interrupt injection | `watchdog.py`, `alerts` table, bus |
| HW-1/2 | ≤8 compute queues / ≤2 concurrent LLM engines | `Limits.validate_against_device()` |
| HW-3 | Distinct `--cache-disk` per engine | `probe_cache_isolation` |
| HW-4 | attempt/turn/wall/replan budgets | `config.Limits`, enforced in `executor.py` |

---

## 2. System architecture — the four planes

`PLAN.md` §3 defines four planes; the code implements them as follows.

```
┌─ WORK PLANE ──────────────────────────────────────────────────────────────┐
│ GitHub: issues, status:* labels, native sub-issues (GraphQL), comments,   │
│ Projects V2 board (projection of the label, never a 2nd source of truth)  │
│ code: gh.py (REST+GraphQL) · projects.py (board) · project_setup.py       │
└───────────────▲─────────────────────────────────────────┬─────────────────┘
       outbox (at-least-once, idempotent)                │ intake sweep / status
┌───────────────┴─────────────────────────────────────────▼─────────────────┐
│ CONTROL PLANE (plain Python, no LLM)  orchestrator.py cycle()              │
│  steering → intake → plan → dispatch → reap-stale → review → outbox →      │
│  project-status → heartbeat                                                │
│  state: db.py (SQLite WAL)   transport: bus.py (Redis Streams)             │
└──────▲──────────────────────────────────────────────▲──────────────────────┘
       │ TaskBrief / ExecutionResult (Redis)          │ HTTP /api/v1/chat
┌──────┴──────────────┐                    ┌──────────┴──────────────────────┐
│ AGENTS PLANE        │                    │ SUPERVISION PLANE (zero-LLM)     │
│ Planner (Claudio)   │◄── judgement ──────│ watchdog.py: 9 checks, ladder 1–5│
│ Executor  (Claudio) │                    │ alerts table + clams:alerts      │
└──────▲──────────────┘                    └──────────▲──────────────────────┘
       │ inference                                    │ probes
┌──────┴──────────────────────────────────────────────┴─────────────────────┐
│ MODELS PLANE: strixper (engine manager :8000) → OpenAI-compatible engine   │
│               (halogen / GUFO / llama.cpp, :8731)                          │
└────────────────────────────────────────────────────────────────────────────┘
```

**The critical distinction** (`doc/deployment_architecture.md` §1, confirmed in code): only
**Planner** and **Executor** are Claudio *agent* instances reached over HTTP. Intake, Review,
Steering, Outbox and Watchdog are **Python classes inside Clams** — there is no "agent" to
point them at. The Planner and Executor talk the *same* contract
(`POST /api/v1/chat {message} → {final_output}`), so they can technically be one instance;
the doc argues against it on workspace, concurrency, approval-posture and blast-radius
grounds, and the code backs that up: the executor needs `approval_mode="off"`
(`ExecutorSettings.approval_mode`) because an unattended run would otherwise stall forever
on a 409 `approval_required`.
---

## 3. Runtime topology (processes, containers, ports)

`docker-compose.yml` (project name `claudio-orchestrator`) defines **8 services**. Note the
header comment: an earlier version collapsed these into 3 and pointed `planner`/`executor`
at stock Claudio images, so *no container ever ran the Clams loop* — the root cause of the
heartbeat failures in issues #2 and #10.

| Service | Image | Role | Port / healthcheck |
|---|---|---|---|
| `redis` | `redis:7-alpine` | event bus, AOF `everysec`, `maxmemory 256mb noeviction` | `127.0.0.1:6379` only, `redis-cli ping` |
| `planner` | `claudio/web:latest` | **stock Claudio web instance** = the user-facing Planner UI + chat | internal `:8080`; `GET /healthz`, `start_period 120s` |
| `agent` | `claudio/agent:latest` | **stock Claudio headless agent** = the Executor's model backend; network alias `claudio-agent` | internal `:8100`; `GET /healthz` |
| `orchestrator` | `docker/orchestrator` | **`clams watch`** — the actual Clams control loop | `clams healthcheck --service orchestrator` |
| `executor` | `docker/executor` | **`clams execute`** — driver that claims briefs off the bus and drives `agent` over HTTP | `clams healthcheck --service executor` |
| `frontend` | `docker/frontend` | React SPA + Clams REST/SSE API on one origin | `8200:8200`, `GET /healthz` |
| `watchdog` | `docker/watchdog` | zero-LLM supervisor | `wdctl selfcheck` |
| `gateway` | `nginx:alpine` | `8100:80` → `planner:8080`, SSE-safe | `wget --spider /healthz` |

Key wiring facts read from the compose file:

- **Redis is loopback-published on purpose.** The default `CLAMS__REDIS__URL` is
  `redis://127.0.0.1:6379/0` so both a host-run `clams` (via `scripts/dev.sh`) and the
  containers reach it; it is never exposed further because "the bus carries unauthenticated
  task briefs".
- **`/repo` must be writable** for `orchestrator` and `executor`: `git worktree add` writes
  into the source repo's `.git`, so a `:ro` mount fails *at dispatch time, not at startup*.
  Both share `claudio-work:/work` so the executor sees the trees the orchestrator plans.
- **`orchestrator` and `executor` share `orchestrator-db:/app/data`** — one SQLite file, two
  processes, which is exactly why the WAL + single-writer-per-table rules in §4 exist.
- **`heartbeat-data`** is a shared volume so the watchdog can read the
  `orchestrator/planner/executor/watchdog` heartbeat JSON files.
- `orchestrator` `depends_on` `planner: service_healthy`; `executor` depends on `agent`;
  `gateway` depends on `planner`.
- Hardening on the Clams services: `init: true`, `restart: unless-stopped`,
  `security_opt: no-new-privileges:true`, `extra_hosts: host.docker.internal:host-gateway`.
  The `planner` container is `read_only: true` with `tmpfs: /tmp`, `cap_drop: [ALL]` and only
  `CHOWN/SETGID/SETUID` re-added for nginx.
- Two token/path contracts must hold (documented in `doc/deployment_architecture.md` §4):
  `CLAMS__{PLANNER,EXECUTOR}__AUTH_TOKEN` must equal the agent's
  `CLAUDIO__SERVER__AUTH_TOKEN` (else HTTP 401 on every call), and Clams' `BASE_URL` must
  include `/api/v1` because that is where the agent mounts its API.

**Two topologies coexist.** `doc/deployment_architecture.md` describes Clams running
*natively* in a host venv with the agents in Docker (Planner `:8100`, Executor `:8101`,
API `:8200`, engine `:8731`, strixper `:8000`); the compose file describes everything
containerised. `config.py` reconciles this with `detect_in_container()` (`/.dockerenv`,
`/run/.containerenv`, `/proc/1/cgroup`) and `resolve_llm_base_url()`, which rewrites a
loopback LLM URL to `http://host.docker.internal:8731` **only** when running containerised —
because inside a container `127.0.0.1` is the container itself, never the host.

**`clamsctl` (`ctl.py`)** is the operator's Docker front-end:
`build | start | stop | restart | recreate | status | health | cleanup | logs`, with
`SERVICES = (redis, watchdog, planner, agent, orchestrator, executor, frontend, gateway)` and
HTTP probes `planner→:8100/healthz`, `gateway→:8100/`, `frontend→:8200/healthz`. It prints
every docker command before running it, and destructive commands (`recreate`, `cleanup`)
require `--yes` when run without a TTY.

---

## 4. Data model and durable state (SQLite)

`db.py`. All five PRAGMAs are set on connect (verified in code, not just claimed by
`PLAN.md` §5.1): `journal_mode=WAL`, `synchronous=FULL`, `busy_timeout=5000`,
`wal_autocheckpoint=1000`, `foreign_keys=ON`. The connection also uses `timeout=5.0`,
`isolation_level=None` (**explicit transaction control** — the code issues its own
`BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`), `check_same_thread=False` (needed because the
executor's lease-renewal task touches the DB from another task) and `row_factory=sqlite3.Row`,
which is why every repository reads rows as `row["col"]`.
**Single-writer ownership per table** is the concurrency contract:

| Table | Single writer | Purpose |
|---|---|---|
| `work_items` | Planner / Sync / UI | one tracked requirement + its plan |
| `task_claims` | Executor (Watchdog may expire) | lease + attempt accounting |
| `alerts` | Watchdog | severity/source/message/acknowledged |
| `outbox` | Outbox worker | deferred external side-effects |
| `projects` | multi-project registry (issue #18) | name/url/status/progress/worktree/paused |
| `runtime_state` | any | `key/value/updated_at` KV for durable flags |

**The composite-key rule.** GitHub issue numbers are *repository*-scoped, so two repos can
both have `#15`. Every work-plane row therefore carries `project_id` (default `0`) and is
identified by `(project_id, <number>)`: `work_items UNIQUE(project_id, requirement_number)`,
`task_claims PRIMARY KEY(project_id, task_number)` with a composite FK to `work_items`, and
indexes on `status`, `gh_status`, `project_id`, `lease_expires_at`, `outbox.status`.
`project_id` defaults to 0 precisely so the original single-repo test suite keeps passing.

**`work_items` state machine** (statuses are plain strings set by `set_status`/`set_plan`):

```
                    ┌──────────── plan fails > max_replans ────────────┐
                    │                                                  ▼
 (intake wins) → claimed → planned → dispatched → [all tasks reported] → review → (done)
      pending (row default)                │                                  ▲
                                           └── reaper: attempts exhausted ──► blocked
```

Evidence: `claim_requirement` inserts `status='claimed'`; `set_plan` writes `status='planned'`;
`step_dispatch` writes `dispatched`; `review.review_requirement` writes `review` (pass) or
`blocked` (any failure); `orchestrator._plan_one` writes `blocked` after `max_replans`.
GitHub-side labels are a separate axis (`gh_status` column + `status:*` labels).

**Atomic claim (FR-2).** `claim_requirement` is a single
`INSERT ... ON CONFLICT(project_id, requirement_number) DO NOTHING` and returns
`cur.rowcount == 1` — exactly one poller wins, no read-then-write race.

**Task leases.** `TaskClaimRepo.claim` runs inside `BEGIN IMMEDIATE` and:
inserts with `attempts=0` if absent (win); **refuses forever if `attempts >= max_attempts`**
("must be re-planned instead", HW-4); steals the row only if `lease_expires_at <= now`;
otherwise loses. Plus `renew()` (extends, only for the current holder), `expire()` (release
now so another can retry), `release()` (delete on success), `increment_attempts()`,
`register_abandoned_attempt()` (used by the reaper), `expired()` and `active()`.

**Outbox.** `enqueue(kind, payload, idempotency_key)` validates `kind ∈ {gh_comment,
gh_status, notification}` and `ON CONFLICT(idempotency_key) DO NOTHING` — so re-enqueuing the
same logical side-effect is a no-op. `claim_pending(limit)` does `BEGIN IMMEDIATE`, selects
`status='pending' AND attempts < 3` and flips them to `'sent'` with `attempts+1` in the same
transaction (claim-then-deliver). `OutboxWorker.MAX_ATTEMPTS = 3`; on failure below the cap
the row goes back to `pending`, at the cap it becomes `failed` **and raises a `warning`
alert**.

**Migrations.** `migrate()` upgrades a pre-#18 DB in place: `_migrate_work_items` /
`_migrate_task_claims` rebuild the tables (`*_new` + copy + rename) to add `project_id`, and
`_PROJECT_COLUMNS` adds missing `projects` columns (`worktree_path`, `default_branch`,
`error`, `paused`). The `idx_work_items_project` index is created *after* migration
(`_POST_MIGRATION_INDEXES`) because it cannot exist before the column does.

**`runtime_state`** exists because of bug #124: a healthy watchdog sweep used to resume an
operator's emergency stop. Pause provenance is now persisted (`operator_paused`,
`watchdog_paused`) so the watchdog only lifts the pause **it** imposed.

**Derived project status** (`project_state.py`): never set independently — computed from its
work items. `no items → Queued`, `any in review → In review`, `all done → Done`,
`anything else active → Running`, `operator-paused → Paused` (overrides). Progress =
percentage of the project's work items that reached `done`. Refreshed every cycle by
`step_projects`, deliberately *after* the work-plane steps.
---

## 5. Message contracts and the event bus

### 5.1 Contracts (`contracts.py`, "spec §4.2")

`SCHEMA_VERSION = "1.0"`. Every model is `ConfigDict(extra="forbid")` — an unknown field is a
validation error, not a silent pass. The version travels as the alias `$schema_version`.

| Model | Direction | Fields |
|---|---|---|
| `TaskBrief` | Planner → Executor | `task_issue`, `requirement_issue`, `objective`, `acceptance_criterion`, `scope{writable,read_only,forbidden}`, `git{base_branch,base_sha,work_branch,repo_path}`, `limits{max_turns=40,max_wall_seconds=1800}`, `project_id=0` |
| `ExecutionResult` | Executor → Planner | `task_issue`, `status ∈ {completed,failed,timeout,blocked}`, `summary`, `evidence{commands[{cmd,exit_code,tail}],log_file}`, `artifact_delta{files_changed,commit,branch}`, `environment_state{git_head,working_tree_clean}`, `project_id` |
| `AlertEvent` | Watchdog → Planner/UI | `severity ∈ {info,warning,critical}`, `source`, `message`, `timestamp`, `requirement_number?`, `task_number?`, `actions[]` |
| `CommandEnvelope` | User/UI → steering | `action`, `args{}`, `raw_text`, `issued_at` |
| `Heartbeat` | service → watchdog | `service`, `pid`, `timestamp`, `detail` |

Validators `validate_task_brief` / `validate_execution_result` / `validate_alert` /
`validate_command` wrap pydantic errors into `ContractError`, and `VALIDATORS` /
`validate_json(schema_name, payload)` give a stable dispatch surface. Rule from the
docstring, enforced in code: **a payload that does not validate is never trusted; it is
dead-lettered instead.**

`TaskBrief`/`ExecutionResult` both require issue numbers `>= 1` and positive limits.
`GitRef.repo_path=""` means "use the process default", which keeps pre-#18 briefs valid.

### 5.2 Bus (`bus.py`) — Redis Streams + consumer groups

| Logical stream | Constant | Producer → Consumer |
|---|---|---|
| `clams:tasks` | `STREAM_TASKS` | Planner/orchestrator → Executor (`TaskBrief`) |
| `clams:results` | `STREAM_RESULTS` | Executor → Planner/orchestrator (`ExecutionResult`) |
| `clams:alerts` | `STREAM_ALERTS` | Watchdog → Planner/UI (`AlertEvent`), `ALERT_STREAM_MAXLEN = 500` |
| `clams:commands` | `STREAM_COMMANDS` | User/UI → Planner (`CommandEnvelope`) |
| `clams:dead` | `STREAM_DEAD` | dead-letter for undecodable / poison messages |

Each entry is `{payload: <json>, type: <ClassName>}` (`PAYLOAD_FIELD`, `TYPE_FIELD`).
`RedisBus._name()` namespaces streams as `clams:clams:<x>`-style via
`f"{namespace}:{stream.split(':',1)[-1]}"` with `namespace` from `CLAMS__REDIS__NAMESPACE`.

Consumer groups actually in use: **`executors`** (group) with per-executor consumers
(`settings.executor.executor_id`, default `executor-1`) on `clams:tasks`; **`planner`** (group)
with consumer `orchestrator` on `clams:results` and `clams:commands`.

The **ack discipline** (this is the #114 fix, and the single most important bus invariant):

- `read()` uses `XREADGROUP group consumer {stream: ">"}` — new messages only.
- `redeliver()` uses `XREADGROUP ... "0"` — **this consumer's own PEL**, i.e. everything it
  claimed but never acked. Called at start-up by both `ExecutorService.recover_pending()` and
  `Orchestrator.recover_pending_results()`.
- `claim_idle()` uses `XAUTOCLAIM` with `min_idle_ms` above the task lease to recover messages
  parked in a *dead* consumer's PEL, which `redeliver` cannot see, "without racing a worker
  that is still running".
- `Delivery` (with `.ack()`) exists because `read_task`/`read_result` used to return the
  decoded object and **drop the message id**, so nothing was ever acked and a restart lost
  every in-flight task.
- `dead_letter()` copies the raw payload plus `source_stream`, `reason`, `dead_at`.
- `pending()` returns `{count, oldest_idle_ms}` and — after #120 — an explicit `error` key on
  failure, because a swallowed exception made a 16-hour backlog read as healthy.
- `InMemoryBus` implements the identical interface **including a modelled PEL**, so the ack
  bugs are testable without Redis.

Typed helpers: `publish_task/take_task/redeliver_tasks/read_task`,
`publish_result/take_result/redeliver_results/read_result`,
`publish_alert/take_alert/read_alert`, `publish_command/take_command/read_command`.

### 5.3 LLM access (`llm.py`, `claudio_client.py`)

Two interchangeable ways to give the Planner a "model":

- `ClaudioClient` (`claudio_client.py`) — talks to a Claudio backend:
  `POST /chat {message, session_id?, approval_mode?, files?} → {run_id, session_id,
  final_output, usage}`, `POST /chat/stream` (SSE), `GET /healthz`, `GET /version`,
  `POST /chat/runs/{run_id}/cancel`, auth `Authorization: Bearer <token>`. Errors map to
  `ClaudioError(status, payload)` / `ClaudioUnavailable`.
- `OpenAILlmClient` (`llm.py`) — a direct OpenAI-compatible completion call
  (`/v1/completions`), with `temperature`, `max_tokens`, `enable_thinking`, and
  `run_sync()`/`_ensure_bridge_loop()` so a sync `llm_call` callable can wrap an async call.
  `make_planner_llm(settings)` picks between the Claudio planner and the direct endpoint.

This is why `PlannerService` takes an injected `llm_call: Callable[[str], str]` — planning,
review judgement and fuzzy command parsing all go through that one seam.

---

## 6. The issue-processing workflow, end to end

### 6.1 The loop

`Orchestrator.run_forever()` first calls `recover_pending_results()` (re-record unacked
results from a previous run), then repeats `cycle()` every `settings.poll_interval`
(**20 s** default). `cycle()` writes a heartbeat **before** the steps (a planning/review LLM
call can take minutes; beating only at the end made a busy orchestrator look dead), then runs
each step in order, each wrapped so **one failing step never stops the loop** — errors are
appended to `CycleReport` and logged:

```
1 step_commands        drain clams:commands   (interrupt priority, spec §8.1)
2 step_intake          GitHub status:Ready → work_items(claimed)
3 step_plan            claimed → planned (LLM decomposition + sub-issues)
4 step_dispatch        planned → dispatched (TaskBrief onto clams:tasks)
5 step_reap_stale      re-dispatch expired leases / publish terminal failure
6 step_review_results  clams:results → evidence gate → review|blocked
7 step_outbox          deliver deferred GitHub side-effects
8 step_projects        recompute derived project status/progress
9 step_heartbeat       write orchestrator + planner heartbeat files
```

`CycleReport.summary()` is literally
`commands=… intake=… planned=… dispatched=… reviewed=… delivered=… errors=…` — the line the
status report quotes as `errors=0`.

### 6.2 Step-by-step, with the code that does it

**0. Operator labels an issue `status:Ready`.** That label *is* the queue-entry trigger.

**1. Intake (`intake.py`).** `sweep()` lists open issues with `status:Ready`, then per issue:
- **skip task sub-issues**: `_is_task_subissue()` matches a `parent-<n>` label or a
  `Parent: #<n>` body line. Without this, intake decomposed its own generated tasks and
  flooded the repo (issue #18).
- skip if already tracked (`work_items.exists(number, project_id)`).
- `claim()` → `claim_requirement()` atomic INSERT; if won, `_queue_transition()` enqueues
  **two outbox rows in the same local transaction**: `gh_status → status:In Progress`
  (idempotency key `intake-status-{pid}-{n}`) and a `gh_comment` acknowledgement
  (`intake-comment-{pid}-{n}`). **GitHub is never called inside the claim transaction.**
- A `GitHubError` on the sweep adds a `warning` alert and leaves local state untouched.
- `add_requirement(title, body)` creates the issue with `status:Ready` + `clams` labels and
  deliberately does **not** insert a `work_items` row, so the chat path exercises the same
  atomic-claim route as a human.

**2. Plan (`step_plan` → `PlannerService.plan_requirement` → `decompose.py`).**
For each `claimed` item (skipping `user_paused`):
- `build_prompt(title, body)` — built from **title and body only**. The anti-pollution rule
  (spec §8.2) is explicit: comment threads, especially the agent's own, are never fed back.
- The prompt demands **3–7 atomic tasks** as *only* a JSON object with
  `title/objective/acceptance_criterion/writable/read_only/forbidden`, and states that a
  reviewable line must be prefixed `run: ` or it is prose and never executed (issue #119).
- `generate_plan(llm_call, ..., retries=1)` → `extract_json` (handles bare JSON, ```json
  fences, JSON wrapped in prose) → `parse_plan` (pydantic `SubTaskSpec`) → `validate_plan`
  (3 ≤ n ≤ 7, **no duplicate titles**). LLM output is untrusted until this passes.
- `create_sub_issues()` creates one GitHub issue per task labelled **`status:In Progress`**
  (deliberately *not* `Ready` — labelling tasks Ready made the board show 83 "Ready" issues
  that intake correctly skipped) with body `Parent: #N` + Objective / Acceptance criterion /
  Scope, then `add_sub_issue()` links it natively via GraphQL `addSubIssue`, falling back to
  a `parent-N` label if refused, so the hierarchy is never lost.
- `set_plan()` persists `plan_json` + `task_numbers` and flips status → `planned`.
- **Bounded retry**: `_plan_one` increments `replans` on failure; below `max_replans` it just
  logs; at the cap it sets `blocked`, adds a `warning` alert and stops retrying — previously
  an unplannable requirement burned two LLM calls every 20 s forever.

**3. Dispatch (`step_dispatch`).** For each project scope `(project_id, worktree_path,
base_branch)` from `dispatch_scopes()`:
- skip if `project_paused(project_id)` (reads `projects.paused` — issue #123, the flag used
  to be written and never read).
- **skip if no provisioned working tree**, raising a `warning` alert instead of silently
  running in the orchestrator's own checkout (issue #115 — "running another repo's tasks
  inside the Clams tree produced confident nonsense").
- for each task not already dispatched: `publish_task(TaskBrief)` where `_brief_for` pulls
  `objective/acceptance_criterion/scope` from `plan_json` by index, sets
  `git.repo_path=worktree_path`, `work_branch=task/<n>`, and copies `max_turns`/
  `max_wall_seconds` from `Limits`; then enqueues a `notification` outbox row with key
  `dispatch-task-{pid}-{task}`. **`dispatched_tasks()` reconstructs "already sent" by
  scanning outbox payloads**, so a crash cannot double-dispatch; pre-#18 rows read as
  project 0 so an upgrade never re-dispatches existing work.
- the requirement then goes `planned → dispatched`.

**4. Execute (`executor.py`, a separate process: `clams execute`).**
`run_forever()` → `recover_pending()` (replay own PEL) → loop `run_once()`:
`write_heartbeat("poll")` → `take_task(group="executors", consumer=executor_id, block_ms=1000)`
→ `process_brief()` → **ack only afterwards**.

`process_brief(brief)`:
1. `task_claims.claim(...)` with `lease_seconds` (2100) and `max_attempts` (3). If it loses,
   it builds a `blocked` "already claimed by X" result and **publishes it** (#118 — it used
   to be discarded, so the gate could never close).
2. Worktree path `work_root/p<project_id>-task-<n>`; `create_worktree(base_repo, path,
   branch=task/<n>, base_sha=HEAD)`. `base_repo = brief.git.repo_path or paths.repo_path`.
3. Start a lease-renewal task (`lease_renew_interval=120 s`) so a legitimate long run is not
   stolen mid-flight (#122), and run the task under `asyncio.wait_for(..., max_wall)`.
4. `ClaudioTaskRunner`: `render_task_prompt()` (objective, acceptance criterion, scope, and
   "Work only inside <worktree>. Do not push. Do not touch other directories.") →
   `claudio.chat(prompt, approval_mode="off", timeout=max_wall)`. **The HTTP timeout follows
   the task budget** — it used to be the 120 s client default while tasks were allowed
   1800 s, so every real task was abandoned as "chat timed out".
5. **Verification is Clams's own**: `verify_commands()` splits the acceptance criterion with
   `extract_commands()` — only lines prefixed `run:`/`cmd:`/`shell:`/`bash:` are executed
   (after stripping list markers and backticks); everything else is *prose*, recorded as
   not machine-verifiable (#119: prose was piped to the shell and failed with exit 127 on
   every task). Each command runs via `run_command_logged()` (`subprocess.run(shell=True,
   cwd=worktree, timeout=600)`, stdout+stderr tail `TAIL_CHARS=2000`; timeout → exit 124,
   exec error → 127).
6. On failure/timeout: remove worktree, `increment_attempts`, publish `failed`/`timeout`;
   at the attempt cap it also sets the task `status:Blocked` on GitHub and raises a
   **`critical`** alert; otherwise it expires the lease so a retry can happen.
7. On success: `enforce_scope(worktree, writable, forbidden)` — any changed file outside
   `writable` or matching `forbidden` fails the task (a trailing `/` means subtree, `*`/`?`
   globs allowed); then `git add -A && git commit -m "task #n"`, `diff_summary()` (committed
   + uncommitted + untracked), `is_clean()`, publish `ExecutionResult(completed, ...)`,
   `task_claims.release()`, remove the worktree.

**5. Review gate (`step_review_results` → `review.py`).**
Loop `take_result(group="planner", consumer="orchestrator")`: `record_result()` **first**
(enqueue a `gh_comment` outbox row carrying the full result JSON, key
`result-{pid}-{task}`), then if `all_tasks_reported(req)` run `review.review_requirement(req)`,
and **ack only in `finally`** so a crash between read and record redelivers (#114).

`verify_evidence(result)` requires **all** of: `status == "completed"`; at least one command;
every command exit 0; every command non-empty stdout; a non-empty `artifact_delta.commit`;
non-empty `files_changed`; `environment_state.working_tree_clean`. Any miss →
`GateVerdict(passed=False, reasons=[...])`.

`review_requirement(req)` re-reads the evidence **out of the local DB** (`_load_results`
scans `outbox` `gh_comment` payloads), not from the caller's argument, "so a stale or forged
argument cannot slip through". Outcomes: untracked / no sub-tasks / missing evidence for some
task / any task failing evidence → comment + `status:Blocked` + local `blocked`. If all pass
and a `judge` is wired, `planner.judge_review(req, evidence_summary)` asks the Planner for
**exactly one word, PASS or FAIL**, and anything unparseable counts as rejection. Only then:
comment + `status:Review` + local `review`. `record_task_result()` additionally moves each
*task* issue to `status:Done` (pass) or `status:Blocked` (fail).

**6. Stale reaping (`step_reap_stale`, issue #117).** `step_dispatch` only looks at `planned`,
so a `dispatched` requirement was never revisited and a dead task hung forever. Each cycle,
for every `dispatched` item's task: skip if already reported; skip if a live claim exists;
if no claim row at all, require `lease + max_wall + stale_task_grace_seconds (300)` to have
elapsed since `updated_at` before calling it lost. Then `register_abandoned_attempt()`;
below the cap → re-publish the brief (+ outbox key `redispatch-task-{pid}-{task}-{attempts}`);
at the cap → publish a synthetic `failed` ExecutionResult ("abandoned: lease expired with no
result after N attempts") so the gate sees a complete task set and blocks the requirement,
plus a `warning` alert.

**7. Deliver (`outbox.py`).** `deliver_once()` claims up to 50 pending rows and applies them:
`gh_comment → gh.add_comment`, `gh_status → gh.set_status`, `notification → optional
notifier` (absent notifier counts as delivered, so a row cannot burn retries and raise a
spurious alert). `_client_for(payload)` + the orchestrator's `gh_resolver` route each row to
**the repo the work item came from**, not always the default repo.

**8. Steering (`steering.py`, FR-1).** Chat text → `CommandEnvelope`/`ParsedCommand`. Parsing
is **deterministic regex first** (`pause|halt|suspend|stop`, `resume|continue|unpause`,
`cancel|stop|kill task #N [and re-plan]`, `add requirement: …`,
`change|update|set acceptance criteria for #N to …`, `re-plan requirement #N`,
`what is the status of requirement #N`), with an **optional LLM fallback** for fuzzy
phrasing; an unrecognised utterance **never mutates state**. Effects: pause/resume set
`user_paused` (per requirement or all — "already-running executors are untouched");
status reports local+github status, paused, re-plans, and per-task lease holder/expiry/
attempts; cancel releases the lease, marks the child `status:Blocked`, removes it from
`task_numbers`, and optionally re-plans (which enforces `max_replans`); add-requirement goes
through `intake.add_requirement`; update-criteria appends to the issue body and flags the
stored plan `criteria_stale`. Commands are drained **first** in every cycle, which is what
"interrupt priority" means in practice.
---

## 7. Component reference

`src/clams/` contains **27 modules** (README lists 18). Grouped by plane.

### 7.1 Foundation

| Module | Responsibility (verified in code) |
|---|---|
| `config.py` | `Settings` tree of `BaseSettings` sub-models, env prefix `CLAMS__`, nested delimiter `__`, `.env` file. Owns `Limits` (HW-1/2/4) and the two start-up guards: `validate_against_device()` (HW-1: `max_concurrent_engines × queues_per_engine ≤ max_compute_queues`) and `validate_lease_vs_wall()` (#122: `lease_seconds > max_wall_seconds`). Also container detection (`detect_in_container`, `in_container` cached) and `resolve_llm_base_url()` which rewrites loopback → `host.docker.internal` only when containerised. `get_settings()` is `lru_cache`d. |
| `db.py` | SQLite WAL store + repositories: `Database`, `WorkItem(Repo)`, `TaskClaimRepo`, `AlertRepo`, `ProjectRepo`, `OutboxRepo`, `RuntimeStateRepo`, `Store`, `open_store()`. See §4. |
| `contracts.py` | The five pydantic wire models + validators. See §5.1. |
| `bus.py` | `Bus` ABC, `RedisBus`, `InMemoryBus`, `Delivery`, typed publish/take helpers. See §5.2. |
| `gh.py` | `GitHubClient`: REST for issues/labels/comments + GraphQL for native sub-issues. `ping()`, `get_issue`, `list_issues`, `create_issue`, `update_issue`, `add_comment`, `list_comments`, `add_labels`, `replace_labels`, `remove_label`, `set_status`, `ensure_labels`, `issue_node_id`, `add_sub_issue`, `remove_sub_issue`, `list_sub_issues`. Label constants `STATUS_READY/IN_PROGRESS/REVIEW/BLOCKED/DONE`, `PROJECT_LABEL="clams"`, `parent_label(n)`. |
| `llm.py` | `OpenAILlmClient` (direct `/v1/completions`) + `make_planner_llm(settings)` + `run_sync()` bridge so an async call can satisfy a sync `llm_call`. |
| `claudio_client.py` | `ClaudioClient` over a Claudio backend: `chat()`, `stream()`, `cancel()`, `health()`, `version()`. |

### 7.2 Work plane

| Module | Responsibility |
|---|---|
| `intake.py` | `IntakeService.sweep()` (FR-2), `claim()`, `add_requirement()`, `_is_task_subissue()` guard. |
| `decompose.py` | `PLAN_PROMPT`, `build_prompt`, `extract_json`, `parse_plan`, `validate_plan` (`MIN_TASKS=3`, `MAX_TASKS=7`, unique titles), `generate_plan(retries=1)`, `create_sub_issues`, `_child_body`, `plan_to_dict`. |
| `planner.py` | `PlannerService.plan_requirement`, `replan` (enforces `max_replans`, raises `ReplanLimitError`), `judge_review` (one-word PASS/FAIL, fail-closed), `update_acceptance_criteria` (appends to body, sets `criteria_stale`). |
| `worktree.py` | `create_worktree` (reclaims a git-registered stale checkout instead of failing forever), `remove_worktree`, `prune_worktrees`, `list_worktrees`, `is_clean`, `diff_summary`, `enforce_scope`, `cleanup_stale_worktrees` (only `^(p\d+-)?task-?\d+$` names are eligible — `projects/` is untouchable after #129), `_owning_repo`. |
| `executor.py` | `ExecutorService` (claim → worktree → run → verify → commit → publish → release), `ClaudioTaskRunner`, `render_task_prompt`, `extract_commands` (`COMMAND_PREFIXES = run:/cmd:/shell:/bash:`), `run_command_logged`, `TaskRunOutcome`, lease renewal, PEL recovery, heartbeat. |
| `review.py` | `verify_evidence` → `GateVerdict`, `ReviewService.review_requirement` / `record_task_result`, `format_evidence_comment`, `format_requirement_comment`. |
| `outbox.py` | `OutboxWorker.deliver_once()` / `_deliver()` / `run_forever()`, `MAX_ATTEMPTS=3`, per-project `gh_resolver`. |
| `projects.py` | GitHub **Projects V2** board sync via GraphQL: `ProjectBoard.resolve()` (one pass at startup: project id, status field id, option ids), `add_item`, `set_status_option`, `find_item`, `move`, `board_from_settings`. Rule: the board is a *projection* of the `status:*` label, and every mutation checks the GraphQL `errors` payload — "fail loudly, never silently". Disabled by default (`CLAMS__PROJECT__ENABLED=false`) because it needs the `project` token scope. |
| `project_setup.py` | `ensure_default_project()` (the configured `settings.github` repo becomes a registered project so single-repo and multi-project share one identity) and `build_project_contexts()` (each additional registered project → a `ProjectContext` with its own `gh`/`intake`/`planner`/`review` + provisioned tree). |
| `project_state.py` | `compute_project_status`, `refresh_project_status`, `refresh_all_project_statuses`. Derived, never set independently. |
| `provision.py` | Clone/sync/verify a project's working tree at `<work_root>/projects/<slug>-<id>`. `slugify`, `project_worktree_path`, `authed_clone_url` (token embedded only for the network op, then `origin` reset to a clean URL), `clone_repo`, `sync_repo`, `detect_default_branch`, `is_git_repo`, `clean_remote_url`, `scrub_remote_tokens`, `find_token_bearing_remotes` (#125), `provision_project`, `remove_worktree`. |

### 7.3 Control plane

| Module | Responsibility |
|---|---|
| `orchestrator.py` | `Orchestrator` with the 9 steps of §6.1, `CycleReport`, `ProjectContext`, `_brief_for`/`_spec_for`, `dispatched_tasks`/`reported_tasks` (outbox-derived dedup), `requirement_for_task`, `all_tasks_reported`, `record_result`, `project_paused`, `_default_scope`/`dispatch_scopes`, `_write_heartbeat` (writes **both** `orchestrator.json` and `planner.json`, because the loop runs on the planner's behalf). |
| `steering.py` | `parse_command` (regex), `llm_parse_command` (fallback), `SteeringService.handle/apply` + `_do_*` handlers. Actions: `status, pause, resume, cancel_task, add_requirement, update_criteria, replan, unknown`. |
| `agents.py` | The **agent registry** (issue #19), persisted as JSON at `paths.agents_file` (`./data/agents.json`). `AGENT_ROLES = ("planner","executor","orchestrator","watchdog")`, `CLAUDIO_ROLES = {planner, executor}`. `AgentConfig` (id regex `^[a-z0-9][a-z0-9_-]{0,62}$`, role, `base_url` normalised to end in `/api/v1` for Claudio roles, `auth_token`, `enabled`, `request_timeout`), `AgentRegistry` (`load/save/all/get/for_role/enabled_for_role/primary_for_role/add/update/remove/replace`, unique-id assertion), `probe_agent`, `parse_agents_value`. Defaults: `DEFAULT_PLANNER_PORT=1072`, `DEFAULT_EXECUTOR_PORT=1073`, `DEFAULT_AUTH_TOKEN="dev-token-change-me"`. Ports 1072/1073 are a deliberate choice so a stock Claudio on 8100 can never collide (`dev.sh` header). |
| `api.py` | FastAPI app — see §8. |
| `cli.py` | `clams` argparse entrypoint — see §8.3. |
| `ctl.py` | `clamsctl` Docker control script — see §3. |

### 7.4 Supervision plane

| Module | Responsibility |
|---|---|
| `watchdog.py` | `WatchdogService` + 9 probes + remediation ladder + `wdctl` entrypoint. See §9. |

---

## 8. The HTTP + SSE API and the web console

### 8.1 API (`api.py`, "issue #14")

Design rule from the docstring: *"a thin, read-mostly projection over the same SQLite store,
Redis bus and config the CLI uses. It never invents a second source of truth."*
`create_app(settings, store, bus, runtime)` → FastAPI `title="Clams API", version="0.1.0"`,
served by `clams serve` on `CLAMS__API__{host=0.0.0.0,port=8200}`.

| Method + path | What it does |
|---|---|
| `GET /healthz` | liveness (used by the `frontend` container healthcheck) |
| `GET/POST /api/v1/project`, `/project/pull` | target repo metadata / pull |
| `GET/PUT /api/v1/config` | live config + **hot patch of a whitelisted subset** |
| `GET/POST/PUT/DELETE /api/v1/agents[/{id}]`, `POST /api/v1/agents/{id}/probe` | agent registry CRUD + liveness probe |
| `POST /api/v1/cli/exec` | run a CLI subcommand — **whitelisted**: `ALLOWED_COMMANDS = {status, intake, plan, dispatch, execute, worker, review, doctor, healthcheck, init-db}` ("an unguarded subprocess spawn of an arbitrary user string is a remote shell"), capped by `api.command_timeout=300 s` |
| `POST /api/v1/planner/action` | structured directive; valid `{retry, pause, resume, alter, approve, reject, stop}` |
| `POST /api/v1/planner/message` | free-text chat → steering |
| `GET /api/v1/status` | header-bar aggregate + heartbeats for `orchestrator, planner, executor, watchdog` |
| `GET /api/v1/tasks` | task list with state |
| `GET/DELETE /api/v1/alerts`, `POST /api/v1/alerts/ack`, `POST /api/v1/alerts/{id}/ack` | alert feed, purge, acknowledge |
| `GET/POST /api/v1/projects`, `POST /api/v1/projects/{id}/{sync,pause,resume}` | multi-project registry |
| `GET /api/v1/events` | **SSE**: `connected`, then `telemetry:update` every `api.telemetry_interval=2 s`, plus forwarded `logs:stream` / `planner:reply` / alerts |
| `WS /ws` | `planner:message` / `planner:reply` |
| `GET /{full_path:path}` | SPA fallback (`_mount_static`, `_find_dist` auto-detects the built bundle) |

Two details worth recording because they are load-bearing fixes:

- **SSE starts at the *current* stream end** (`bus.last_id("clams:alerts")`), not `0-0`.
  Reading from the beginning replayed every historical alert — including days-old criticals —
  into the live feed on every page load.
- **`planner/action` applies the local store effect *first*** so header controls work even if
  the bus is down, and the global branch touches **every project's rows** (`set_paused_all`)
  and persists `operator_paused` / `operator_stopped` in `runtime_state`. The old code
  published to `clams:planner-actions`, a stream **nothing consumed** (#123); that dead
  publish is gone and `bus_error` is kept only for API compatibility.

`compute_state()` derives the global badge: unacked criticals → `Error`, else operator-paused
→ `Paused`, else live claims → `Executing`, else `Idle`.
`PUT /api/v1/config` validates against `_editable_schema()` — exactly 13 fields
(`poll_interval`, `limits.{max_attempts,max_turns,max_wall_seconds,max_replans,
max_concurrent_engines,queues_per_engine}`, `watchdog.{interval_seconds,heartbeat_max_age}`,
`project.{enabled,number,status_field}`, `executor.heartbeat_interval`) with min/max bounds —
builds a candidate `Settings`, runs `Settings.load()` (so the HW-1 and lease-vs-wall guards
fire on a bad edit), diffs and applies. Agent **connection** settings are deliberately *not*
here: they are startup config from `.env` (`doc/deployment_architecture.md` §4).

### 8.2 Web console (`frontend/`)

React + TypeScript + Vite SPA. `src/App.tsx`, `src/api.ts`, `src/app-state.tsx`,
`src/types.ts`, and pages **Overview, Projects, Chat, Activity, Config, Agents, CliRunner**
(+ `components/AgentsConfig.tsx`). It maps 1:1 onto the API above (status badge + SSE
telemetry on Overview, alert feed on Activity, steering chat in Chat, registry in Agents,
whitelisted CLI in CliRunner). `doc/frontend_redesign_requirements.md` (20 KB) and
`doc/clams_console_mockup.html` (42 KB) are its design inputs. In Docker the built `dist/`
is served by the `frontend` container on `:8200` alongside the API (single origin); in dev,
`scripts/dev.sh` runs Vite on `0.0.0.0:5173`.

### 8.3 CLI (`cli.py`, "spec §10")

Three console scripts (`pyproject.toml`): `clams = clams.cli:main`,
`wdctl = clams.watchdog:main`, `clamsctl = clams.ctl:main`.

`clams` subcommands: `init-db`, `intake`, `plan <req>`, `dispatch`, `execute`,
`worker` (executor as a long-lived daemon), `review <req>`, `watch` (orchestrator loop),
`status`, `doctor` (health matrix once), `scrub-credentials` (#125), `clear-alerts`,
`healthcheck --service <name>` (Docker HEALTHCHECK), `serve` (HTTP+SSE backend).
Note `worker` and `serve` are **not in the README table** — see §12.

### 8.4 Dev supervisor (`scripts/dev.sh`, "issue #19")

Starts all eight components: `redis :6379`, `planner_agent :1072`, `executor_agent :1073`,
`backend :8200`, `orchestrator`, `worker`, `watchdog`, `frontend :5173`. Commands
`start|stop|status|restart|logs [component]|agents`. Its header states a dependency that is
easy to miss: **the worker and watchdog are not optional** — the watchdog's
`agent_heartbeats` check requires a fresh `executor.json`, and only `clams worker` writes it;
omitting the worker is exactly what produced
"Critical condition detected; dispatching paused. agent_heartbeats".
---

## 9. Supervision: watchdog checks, alerts, remediation ladder

`watchdog.py` is deliberately **zero-LLM**: *"It never imports the planner, the decomposer, or
any model client — a test asserts that, because the moment the supervisor depends on a model
it stops being able to supervise the model."* Loop: `run_forever()` writes its own heartbeat
first, then `sweep()`, then sleeps `watchdog.interval_seconds` (**15 s**).

### 9.1 The check matrix (9 checks)

| # | Check id | Probe | Fail severity |
|---|---|---|---|
| 1 | `llm_engine_health` | 1-token `POST {base}/v1/completions` (`model="probe"`, `max_tokens=1`), 200 = alive; timeout `llm_probe_timeout=5 s` | `warning` until `llm_failures_critical=3` consecutive, then `critical` |
| 2 | `strixper_api` | `GET {base}/api/v1/runner/active`; fails on non-200, `engine_switch_error`, or `engine_probe_error` | `critical` immediately |
| 3 | `sqlite_storage` | `BEGIN IMMEDIATE` + `ROLLBACK` round-trip; fails if it throws or exceeds 5000 ms | `critical` |
| 4 | `storage_volume_free` | `statvfs(paths.data_dir)` free ≥ `disk_min_free_bytes` (2 GiB) | `critical` |
| 5 | `redis_task_queue` | `bus.pending("clams:tasks","executors")`: oldest idle ≤ `queue_max_pending_age` (900 s); an `error` key in the probe result is itself a **failure** (#120) | `critical` (or `skipped` if no bus) |
| 6 | `github_reachability` | `gh.ping()` | `warning` until `github_failures_critical=3`, then `critical` |
| 7 | `agent_heartbeats` | `planner.json` + `executor.json` present, parseable and younger than `heartbeat_max_age` | `warning` until `heartbeat_failures_critical=3`, then `critical`; **skipped** during `startup_grace_seconds=120` |
| 8 | `engine_cache_isolation` | HW-3: every declared engine has a **distinct** resolved `--cache-disk` path | `critical` (passes trivially with no engines declared) |
| 9 | `git_clone_credentials` | `find_token_bearing_remotes(work_root)` — no GitHub token baked into any clone's remote URL (#125) | `critical` (`warning` if remotes cannot be inspected) |

Three anti-flapping devices are explicit in the code:
**consecutive-failure counters** (LLM/GitHub/heartbeat need 3 in a row, so a routine restart
no longer pauses the pipeline); a **startup grace period** (leftover heartbeat files from a
previous run cannot trip it before the orchestrator's first cycle); and
**`alert_cooldown_seconds=900`** — `raise_alert` first calls `alerts.find_recent(severity,
source, message, cooldown)` and returns the existing id instead of inserting, so a persistent
condition cannot mint a new critical every 15 s. Escalation messages are deliberately kept
free of volatile detail (ages, counts) *because* the cooldown de-duplicates on exact text.

`SweepReport.healthy` is `not criticals and not skipped_names` — **a skipped check means a
whole class of failure went undetected, so it must not read as healthy.**

### 9.2 The remediation ladder (spec §6.2, five steps)

```
sweep():
  run_checks() → healthy? → reset counter, _lift_watchdog_pause(), return
  1 PAUSE & NOTIFY     set_paused_all(True) + runtime{watchdog_paused=1}
                       + critical alert with actions [Resume, Force Re-Plan, Cancel Requirement]
  2 DIAGNOSE           re-run each failing check; if all pass now → remediated, return
  3 AUTO-RECOVER       3a LLM/Strixper down → engine_restart() via strixper
                       3b heartbeat stale → docker_restart("planner")
                       3c queue/disk problem → cleanup_stale_worktrees(work_root, live_paths)
                          SKIPPED if live claims exist but paths are undeterminable (#120)
  4 VERIFY             re-run all checks; healthy → reset counter, remediated
  5 ESCALATE           recovery_attempts++; at max_recovery_attempts(3) →
                       critical "Automated recovery failed … Human action required: <checks>"
                       with actions [Open runbook, Manual restart]
```

**Pause provenance (#124)** is the subtle one and it is implemented across three places:
`_step_pause_and_notify` records `runtime{watchdog_paused=1}` and sets an in-memory
`_paused_by_watchdog` flag; `_lift_watchdog_pause` returns immediately unless *that* flag is
set, and if `runtime{operator_paused}` is true it **re-applies** `set_paused_all(True)`.
Previously a healthy sweep called a blanket `set_paused_all(False)` and silently resumed an
emergency stop the moment a probe happened to pass. On recovery it also
`alerts.ack_all("critical")` so the badge does not stay red for a problem that is gone.

`wdctl` (`watchdog:main`) exposes `selfcheck` — the container HEALTHCHECK entrypoint, which
proves liveness by writing and reading its own heartbeat file.

### 9.3 Heartbeats

Plain JSON files (`Heartbeat` model) in `paths.heartbeat_dir`: `orchestrator.json` and
`planner.json` (both written by the orchestrator, at cycle start *and* end, because the loop
runs on the planner's behalf), `executor.json` (written on every poll and by a background
`beat()` task every `heartbeat_interval=20 s` so a long task does not look dead), and
`watchdog.json`. They are a shared Docker volume (`heartbeat-data`), which is what lets the
watchdog container read them.

---

## 10. Configuration reference (`CLAMS__*`)

`Settings` (env prefix `CLAMS__`, nested delimiter `__`, `.env` in the repo root, `extra="ignore"`).
Six env files exist: `.env` (host/dev), and one per container
(`.env.planner`, `.env.executor`, `.env.orchestrator`, `.env.watchdog`, `.env.agent`).

| Group | Key = default |
|---|---|
| top-level | `poll_interval=20.0`, `intake_label=status:Ready`, `inprogress_label=status:In Progress`, `review_label=status:Review`, `blocked_label=status:Blocked`, `done_label=status:Done`, `project_label=clams` |
| `github` | `owner=massimorofi`, `repo=claudio`, `token=""` (stripped), `api_base=https://api.github.com`, `full_name` property |
| `project` (V2 board) | `enabled=False`, `owner=""`, `number=0`, `status_field="Status"`, `status_map=""` (`parsed_status_map` merges `status:Label=Column` overrides over the built-in `Ready=Todo, In Progress=In Progress, Review=In Review, Blocked=Blocked, Done=Done`) |
| `redis` | `url=redis://127.0.0.1:6379/0`, `namespace=clams` |
| `planner` | `base_url=http://127.0.0.1:8100`, `auth_token=""`, `request_timeout=120.0`, `host=0.0.0.0`, `port=8080` |
| `executor` | `base_url=http://127.0.0.1:8101`, `auth_token=""`, `request_timeout=120.0`, `executor_id=executor-1`, `lease_seconds=2100`, `lease_renew_interval=120.0`, `heartbeat_interval=20.0`, `poll_interval=1.0`, `approval_mode="off"` |
| `llm` | `base_url=http://127.0.0.1:8731`, `container_base_url=http://host.docker.internal:8731`, `api_key=""`, `model=""`, `timeout=300.0`; `resolved_base_url` applies the container rewrite; base URLs validated as absolute http(s) |
| `strixper` | `base_url=http://127.0.0.1:8000`, `active_path=/api/v1/runner/active`, `restart_path=/api/v1/runner/restart`, `request_timeout=15.0` |
| `limits` | `max_attempts=3`, `max_turns=40`, `max_wall_seconds=1800`, `max_replans=3`, `stale_task_grace_seconds=300`, `max_compute_queues=8`, `max_concurrent_engines=2`, `queues_per_engine=3`, `queues_needed` property |
| `paths` | `data_dir=./data`, `db_path=./data/clams.db`, `work_root=./work`, `repo_path=.`, `heartbeat_dir=./data/heartbeats`, `agents_file=./data/agents.json` (all expanded + resolved to absolute) |
| `watchdog` | `interval_seconds=15`, `heartbeat_max_age=300`, `disk_min_free_bytes=2 GiB`, `queue_max_pending_age=900`, `llm_probe_timeout=5`, `llm_failures_critical=3`, `github_failures_critical=3`, `heartbeat_failures_critical=3`, `startup_grace_seconds=120`, `alert_cooldown_seconds=900`, `max_recovery_attempts=3` |
| `api` | `host=0.0.0.0`, `port=8200`, `static_dir=""` (auto-detect), `command_timeout=300`, `telemetry_interval=2.0` |
| `agents` | `host=127.0.0.1`, `planner_port=1072`, `executor_port=1073`, `auth_token=dev-token-change-me`, `request_timeout=120` |

`Settings.load()` is the only sanctioned constructor: it runs **both** start-up guards
(HW-1 queue ceiling and lease-vs-wall) so a misconfiguration fails at boot, not at 3 a.m. as
a double-run.

Dependencies (`pyproject.toml`): Python ≥ 3.11; `httpx`, `pydantic>=2.7`,
`pydantic-settings`, `redis>=5.0`, `aiosqlite`, `fastapi>=0.110`, `uvicorn[standard]`;
dev: `pytest`, `pytest-asyncio` (`asyncio_mode=auto`, marker `live` for tests that hit real
GitHub/Redis/Claudio). MIT licensed, version `0.1.0`.

---

## 11. Limits, budgets and safety rails

| Rail | Value | Where enforced |
|---|---|---|
| Tasks per plan | 3–7, unique titles | `decompose.validate_plan` |
| Max attempts per task | 3 | `TaskClaimRepo.claim` refuses at cap; `executor._fail` alerts `critical`; reaper publishes failure at cap |
| Max turns | 40 | `TaskLimits` → `TaskBrief.limits` |
| Max wall clock | 1800 s | `asyncio.wait_for(..., max_wall)` in `process_brief`; also used as the agent HTTP timeout |
| Max re-plans | 3 | `PlannerService.replan` raises `ReplanLimitError`; `_plan_one` blocks at cap |
| Lease | 2100 s, renewed every 120 s | `config` guard `lease > max_wall`; `TaskClaimRepo.renew` |
| Unclaimed-task grace | 300 s beyond lease+wall | `step_reap_stale._dispatched_long_ago` |
| Compute queues | engines × queues ≤ 8 | `Limits.validate_against_device()` (HW-1) |
| Concurrent engines | 2 | `Limits.max_concurrent_engines` (HW-2) |
| Cache isolation | distinct `--cache-disk` per engine | `probe_cache_isolation` (HW-3) |
| Outbox retries | 3 | `OutboxWorker.MAX_ATTEMPTS`, `claim_pending(attempts < 3)` |
| Command timeout (verification) | 600 s | `run_command_logged` (124 on timeout, 127 on exec error) |
| Evidence tail | 2000 chars | `TAIL_CHARS` |
| Scope enforcement | writable allowlist + forbidden denylist | `worktree.enforce_scope` |
| CLI from UI | 10 whitelisted subcommands, 300 s | `api.ALLOWED_COMMANDS` |
| Alert de-dup | 900 s cooldown | `WatchdogService.raise_alert` |
| Recovery escalation | 3 attempts → human | `watchdog.sweep` step 5 |

**Fail-closed defaults worth naming explicitly:** malformed payloads are dead-lettered, not
trusted; an unparseable review answer is a FAIL; an unrecognised chat command mutates nothing;
a project with no working tree is skipped rather than run in the wrong tree; a skipped
watchdog check makes the sweep unhealthy; prose acceptance criteria are recorded as
unverified rather than executed.

---

## 12. Doc-vs-code divergences and known gaps

The docs are partly stale. Everything below was verified by reading the code.

| # | Divergence | Evidence |
|---|---|---|
| 1 | **README/PLAN list 17–18 modules; `src/clams/` has 25.** Missing from the README layout: `agents.py`, `api.py`, `ctl.py`, `llm.py`, `project_setup.py`, `project_state.py`, `projects.py`, `provision.py`. | `ls src/clams/*.py` |
| 2 | **`alerts.py` is listed in `PLAN.md` §5 as the FR-6 alert module but does not exist.** Alerting lives in `db.AlertRepo` + `watchdog.raise_alert` + `contracts.AlertEvent`. | `ls src/clams/alerts.py` → no such file |
| 3 | **README says 5 compose services (`planner, executor, watchdog, redis, gateway`); the file defines 8** (`redis, planner, agent, orchestrator, executor, frontend, watchdog, gateway`). The compose header states the old 3-way collapse was itself the root cause of issues #2/#10. | `docker-compose.yml` |
| 4 | **README says the Web UI is served through nginx on `:8100`.** True for the *stock Claudio* planner UI; the **Clams console is a separate React SPA on `:8200`** (`frontend` service, `ApiSettings.port=8200`). | `docker-compose.yml`, `api.py` |
| 5 | **README CLI table omits `worker`, `serve`, `healthcheck`, `clear-alerts`.** All four exist in `cli.HANDLERS`; `worker` and `healthcheck` are load-bearing (the watchdog's heartbeat check needs `clams worker`; the containers' HEALTHCHECKs call `clams healthcheck --service …`). | `cli.py` |
| 6 | **`.env.example` pins `CLAMS__WATCHDOG__HEARTBEAT_MAX_AGE=60`, but the code default is `300.0`** and the code comment says the old 60 s is precisely what made a busy-but-healthy orchestrator look dead and paused dispatching. Copying `.env.example` reintroduces that bug. `.env.example` also omits `heartbeat_failures_critical`, `startup_grace_seconds`, `alert_cooldown_seconds`, `stale_task_grace_seconds`, `CLAMS__API__*`, `CLAMS__EXECUTOR__LEASE_RENEW_INTERVAL`, `CLAMS__EXECUTOR__APPROVAL_MODE`. | `config.WatchdogSettings` vs `.env.example` |
| 7 | **Container env files disagree about the executor address.** `.env.orchestrator` and `.env.executor` use `CLAMS__EXECUTOR__BASE_URL=http://agent:8100` (the Claudio agent — correct), while `.env.planner` and `.env.watchdog` use `http://executor:8101`, and no compose service publishes 8101. Latent misconfiguration for anything reading those two. | `.env.*` files |
| 8 | **`doc/deployment_architecture.md` describes Planner `:8100` / Executor `:8101` on a `claudio_claudio-net` Docker network**; the compose file puts the planner on `:8080` behind the gateway and the agent on `:8100`, and `dev.sh`/`agents.py` use **1072/1073**. Three port stories coexist; the code's own default (`PlannerSettings.base_url=…:8100`) matches none of the deployed values, which are all supplied by `.env`. | doc vs compose vs `dev.sh` |
| 9 | **`STATUS_REPORT.md` is the freshest source** (2026-11-15, 513 tests) and documents open items the README does not mention. | `STATUS_REPORT.md` |

**Open / remaining work, as recorded by the repo itself:**

- **#128 (open):** `build_project_contexts` only provisions when `worktree_path` is empty, so
  a recorded-but-deleted working tree never self-heals. Needs
  `if not worktree or not Path(worktree).exists()`.
- **Operator action outstanding:** rotate the GitHub token that was written into clone configs
  during earlier provisioning — "scrubbing the files does not un-leak it".
- **clams#105** was still `dispatched` (tasks 110/111) at the time of the status report.
- `TROUBLESHOOT_18.md` "What remains" documents the operator-side repair of #18 (29 stale PEL
  entries purged, dead worktree registrations and 9 empty `task/*` branches removed — #126).
- Working tree is **not clean**: `.gitignore` and `scripts/dev.sh` modified, and
  `docs/github-project-permissions.md` has been moved to `doc/` (uncommitted), so the
  README link `./docs/github-project-permissions.md` currently points at a deleted path.

**Design gaps I noticed while reading (not filed as issues):**

- `review._load_results` scans **every** `gh_comment` outbox row and re-fetches the work item
  per row; `orchestrator.dispatched_tasks`/`reported_tasks` likewise scan all outbox payloads
  each cycle. Correct, but O(outbox size) per cycle with no pruning of old rows.
- `OutboxRepo.claim_pending` marks rows `'sent'` *before* delivery and reverts to `'pending'`
  on failure — a crash between claim and delivery loses the row silently.
- `step_plan` and `step_intake` iterate the default project and then each context, but
  `step_plan` re-reads `list_by_status("claimed", …)` per project with no per-cycle cap, so a
  large backlog means many sequential LLM calls in one cycle (bounded only by the watchdog's
  300 s heartbeat allowance).
- `planner.update_acceptance_criteria` writes `plan_json` with raw SQL and sets
  `criteria_stale=True`, but nothing in the pipeline reads `criteria_stale`.
- `contracts.SCHEMA_VERSION` is `"1.0"` and is carried on the wire, but no consumer branches
  on it — a v2 payload would simply fail validation rather than be handled.

---

## 13. Evidence index

What I read to produce this document (all paths relative to the repo root):

**Source (all 25 modules under `src/clams/`)** — read in full: `config.py`, `contracts.py`,
`orchestrator.py`, `intake.py`, `decompose.py`, `planner.py`, `executor.py`, `worktree.py`,
`review.py`, `outbox.py`. Read structurally (module docstring + every class/function signature
+ the sections cited): `db.py` (schema, `claim_requirement`, `TaskClaimRepo`, `OutboxRepo`,
`set_plan`), `bus.py` (`RedisBus` streams/groups/ack/redeliver/claim_idle/dead_letter/pending),
`gh.py` (`set_status`, sub-issue GraphQL + fallback), `steering.py` (regexes, `apply`, all
`_do_*`), `watchdog.py` (all 9 checks, probes, ladder, provenance, `selfcheck`),
`api.py` (docstring, route list, `planner/action`, `_editable_schema`, SSE), `agents.py`,
`llm.py`, `projects.py`, `project_state.py`, `project_setup.py`, `provision.py`, `ctl.py`,
`claudio_client.py`, `cli.py`.

**Deployment & tooling:** `docker-compose.yml` (all 8 services, volumes, healthchecks),
`config/nginx.conf`, `pyproject.toml`, `Makefile`, `.env.example`, `.env`, `.env.planner`,
`.env.executor`, `.env.orchestrator`, `.env.watchdog`, `.env.agent`, `scripts/dev.sh` (header).

**Docs:** `README.md`, `PLAN.md`, `STATUS_REPORT.md`, `TROUBLESHOOT_18.md`,
`doc/deployment_architecture.md` (all 5 sections), `doc/frontend_redesign_requirements.md`
(listed), `frontend/` structure.

**Verification performed:** `ls src/clams/*.py` (25 modules + `__init__.py`);
`pytest --collect-only` → **513 tests collected** (matches `STATUS_REPORT.md`'s "513 passed");
`git log --oneline` (the #114–#129 fix commits named in §6/§12 are all present);
`git status --porcelain` (dirty tree, as noted); grep of every `set_status(...)` /
`list_by_status(...)` call site to derive the state machine in §4.

**Not verified / unverifiable here:** the live GitHub state of `massimorofi/claudio#5` and the
referenced issues (no network calls were made against GitHub); whether the running deployment
currently matches the committed compose file; runtime behaviour of `strixper`/the inference
engine. Claims about the *live* pipeline (containers healthy, task 45 completed, outbox
drained) are quoted from `STATUS_REPORT.md`, not independently observed.
