# Clams — Claudio Multi-Agent System: Implementation Plan

**Source spec:** [massimorofi/claudio#5](https://github.com/massimorofi/claudio/issues/5) — *Claudio Autonomous Multi-Agent System: System Requirements & Architecture Specification v1.1.0*
**Repo:** `clams` (standalone orchestrator wrapping decoupled Claudio agent instances)
**Author:** Claudio · **Date:** 2026-11-08

---

## 1. Goal

Build `clams`, a standalone orchestrator that turns GitHub Issues into a durable work queue and drives
two decoupled Claudio instances — a **Planner** (user-facing copilot, Web UI + chat steering) and an
**Executor** (headless task runner in isolated git worktrees) — with a **zero-LLM watchdog** supervising
the whole thing, all on a single AMD Strix Halo host under Docker Compose.

## 2. Scope boundary

| In scope | Out of scope |
|---|---|
| Work queue, GitHub sync, handoffs, leases, outbox | Changes to core `claudio` agent |
| Planner/Executor orchestration + prompts | Model training/conversion |
| Watchdog, alerts, remediation ladder | Multi-host / distributed scheduling |
| Nginx gateway on `:8100` → Planner Web UI | Auth/SSO beyond bearer token |
| `strixper` engine health integration | Replacing `strixper` |

## 3. Architecture recap (four planes)

1. **Work Plane** — GitHub Issues + Projects: requirements, sub-issues, status transitions, evidence.
2. **Agents Plane** — 2× Claudio instances: Planner (Web UI, planning, review) / Executor (scoped task runs).
3. **Models Plane** — `strixper` host inference runner (Halogen / GUFO / llama.cpp).
4. **Supervision Plane** — No-LLM watchdog: checks, state transitions, alerts, deadman switch, remediation.

## 4. Requirements traceability

| Req | Summary | Delivered by | Sub-issue |
|---|---|---|---|
| FR-1 | Interactive chat & steering via Planner Web UI | `steering.py`, `planner.py`, gateway | S1, S8 |
| FR-2 | Autonomous requirement intake (Ready → In Progress, atomic lock) | `intake.py`, `db.py` | S1, S4 |
| FR-3 | Task decomposition into 3–7 native GitHub sub-issues | `decompose.py`, `gh.py` | S2, S5 |
| FR-4 | Isolated task execution in git worktrees + evidence | `executor.py`, `worktree.py` | S6 |
| FR-5 | Verification & evidence gate → `Review` | `review.py` | S7 |
| FR-6 | Fault tolerance & interrupt injection into Planner stream | `watchdog.py`, `alerts.py` | S9 |
| HW-1/2 | ≤8 compute queues / ≤2 concurrent LLM engines | `limits.py`, compose caps | S1, S11 |
| HW-3 | Distinct `--cache-disk` per engine | `strixper_client.py` | S9 |
| HW-4 | max_attempts 3 / max_turns 40 / max_wall 1800 / max_replans 3 | `config.py`, enforced in executor | S1, S6 |

## 5. Technical design

**Language/runtime:** Python 3.11+ (asyncio). **Storage:** SQLite (WAL) + Redis Streams. **HTTP:** `httpx`.
**Config:** `pydantic-settings` (env `CLAMS__*` + `.env`). **Tests:** `pytest` + `pytest-asyncio`.

```
clams/
├── pyproject.toml            # package metadata, deps, pytest config
├── README.md                 # quickstart + runbook
├── PLAN.md                   # this file
├── .env.example
├── docker-compose.yml        # planner, executor, watchdog, redis, gateway
├── config/nginx.conf         # :8100 → planner:8000, SSE-safe
├── docker/{planner,executor,watchdog}/Dockerfile
├── src/clams/
│   ├── config.py             # Settings, limits (HW-4), engine caps (HW-1/2)
│   ├── db.py                 # WAL PRAGMAs, schema, single-writer repositories
│   ├── contracts.py          # TaskBrief / ExecutionResult / AlertEvent (pydantic)
│   ├── gh.py                 # GitHub REST + GraphQL (sub-issues) client
│   ├── claudio_client.py     # Claudio backend HTTP client (chat, SSE, health)
│   ├── bus.py                # Redis Streams event bus (task/result/alert)
│   ├── intake.py             # FR-2 Ready → In Progress, atomic lock
│   ├── decompose.py          # FR-3 3–7 atomic sub-issues
│   ├── planner.py            # FR-1 planner service + review judgement
│   ├── worktree.py           # FR-4 isolated git worktree lifecycle
│   ├── executor.py           # FR-4 headless task runner + evidence
│   ├── review.py             # FR-5 evidence gate → Review
│   ├── steering.py           # FR-1 chat command → orchestrator action
│   ├── alerts.py             # FR-6 alert block formatting + injection
│   ├── watchdog.py           # FR-6 check matrix + remediation ladder
│   ├── outbox.py             # transactional outbox worker
│   ├── orchestrator.py       # background loop wiring all services
│   └── cli.py                # argparse entrypoint (`clams`)
└── tests/                    # per-module unit tests + e2e loop test
```

### 5.1 Data model (SQLite, WAL)
Exactly the spec's four tables: `work_items`, `task_claims`, `alerts`, `outbox`, created with
`journal_mode=WAL, synchronous=FULL, busy_timeout=5000, wal_autocheckpoint=1000, foreign_keys=ON`.
Single-writer ownership per table (spec §8.3): `work_items` → Planner/Sync/UI, `task_claims` → Executor,
`alerts` → Watchdog, `outbox` → Outbox worker.

### 5.2 Message contracts
`TaskBrief` (Planner→Executor) and `ExecutionResult` (Executor→Planner) are pydantic models with
`$schema_version`, validated on both publish and consume; malformed payloads are dead-lettered, never trusted.

### 5.3 GitHub sub-issues — verified constraint
The spec mandates `POST /repos/{owner}/{repo}/issues/{id}/sub_issues`. **Verified on this repo:** that REST
route returns `404` (personal-account repo without Issue Types enabled), while the GraphQL `addSubIssue`
mutation **works**. `gh.py` therefore uses GraphQL for sub-issue add/remove/list and falls back to a
`parent:#N` label + body marker if GraphQL is unavailable, so the hierarchy is never lost.

### 5.4 Enforcement of HW limits
`config.py` exposes `max_concurrent_engines=2`, `max_compute_queues=8`, `max_attempts=3`,
`max_turns=40`, `max_wall_seconds=1800`, `max_replans=3`. The dispatcher refuses to start a third engine
and the executor hard-stops on wall-clock/turn budget — limits are enforced in code, not documentation.

## 6. Sub-issue breakdown (each is a GitHub issue under #5)

| # | Title | Scope | Done when |
|---|---|---|---|
| S1 | Scaffold, config & SQLite state layer | pyproject, settings, WAL schema, repositories | `pytest tests/test_config.py tests/test_db.py` green |
| S2 | GitHub client (issues/labels/comments/sub-issues) | REST + GraphQL sub-issue ops, reachability probe | `pytest tests/test_gh.py` green |
| S3 | Redis event bus & message contracts | streams, consumer groups, dead-letter, pydantic contracts | `pytest tests/test_bus.py tests/test_contracts.py` green |
| S4 | Autonomous requirement intake | poll `Ready`, atomic lock, `In Progress` | `pytest tests/test_intake.py` green |
| S5 | Planner decomposition | 3–7 atomic sub-issues, native linking, plan_json | `pytest tests/test_decompose.py` green |
| S6 | Executor: isolated worktree execution | worktree per task, run tests, evidence, budgets | `pytest tests/test_worktree.py tests/test_executor.py` green |
| S7 | Verification & evidence gate | exit-code/diff evidence → GH comment → `Review` | `pytest tests/test_review.py` green |
| S8 | Chat steering (pause/cancel/re-plan/add) | command parse → state effects, interrupt priority | `pytest tests/test_steering.py` green |
| S9 | Zero-LLM watchdog + remediation ladder | 7 checks, alerts, ladder 1–5, deadman switch | `pytest tests/test_watchdog.py` green |
| S10 | Outbox worker + orchestrator loop + CLI | transactional side-effects, main loop, `clams` CLI | `pytest tests/test_outbox.py tests/test_orchestrator.py` green |
| S11 | Container infra: compose + nginx gateway | planner/executor/watchdog/redis/gateway, healthchecks | `docker compose config` + smoke green |

## 7. Execution order & dependencies

S1 → S2 → S3 (foundation) → S4 → S5 → S6 → S7 (work plane) → S8 (steering) → S9 (supervision)
→ S10 (loop) → S11 (infra). Each sub-issue is independently testable; later ones consume earlier ones.

## 8. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Native sub-issue REST 404 on personal repo | Verified GraphQL path works; label+body fallback (S2) |
| Planner chat vs background loop race | Interrupt check at turn boundaries; pause flag read before each dispatch (S8) |
| SQLite `SQLITE_BUSY` | Per-table single writer + `busy_timeout=5000` (S1) |
| Self-reading comment pollution | Planner reads only requirement body + result JSON, never own comments (S5) |
| Engine cold start | Healthcheck `start_period: 120s`, pre-warm hook (S11) |
| Executor runaway | Hard wall-clock + turn budget enforcement, lease expiry (S6, S9) |

## 9. Definition of done

1. All 11 sub-issues created under #5 and closed with test evidence.
2. `pytest` full suite green.
3. `clams doctor` passes against the live host (Redis, strixper, Claudio, GitHub).
4. `docker compose config` validates; gateway routes `:8100` → planner.
5. README quickstart reproduces a full intake → plan → execute → review cycle.
