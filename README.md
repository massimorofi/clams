# Clams — Claudio Multi-Agent System

`clams` is a standalone orchestrator that turns GitHub Issues into a durable work
queue and drives two decoupled Claudio instances — a **Planner** (user-facing
copilot, Web UI + chat steering) and an **Executor** (headless task runner in
isolated git worktrees) — with a **zero-LLM watchdog** supervising the whole thing.

Built to the specification in
[massimorofi/claudio#5](https://github.com/massimorofi/claudio/issues/5).
Implementation plan: [PLAN.md](./PLAN.md).

## Quick start

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # fill in GitHub token + Claudio URLs
clams init-db
clams doctor                  # verify Redis / strixper / Claudio / GitHub
clams intake                  # pull status:Ready issues into the queue
clams watch                   # run the full autonomous loop
```

Web UI (Planner) is served through the nginx gateway on **http://localhost:8100**.

## Docker

```bash
docker compose up -d
docker compose ps
clams doctor
```

Services: `planner`, `executor`, `watchdog`, `redis`, `gateway`.

## CLI

| Command | Purpose |
|---|---|
| `clams init-db` | create / migrate the local SQLite database |
| `clams intake` | sweep GitHub for `status:Ready` requirements |
| `clams plan <req>` | decompose a requirement into 3–7 sub-issues |
| `clams dispatch` | publish task briefs for a planned requirement |
| `clams execute` | run one task from the queue |
| `clams review <req>` | run the evidence gate on a requirement |
| `clams watch` | run the orchestrator loop |
| `clams status` | show tracked requirements, leases, outbox, alerts |
| `clams doctor` | run the health-check matrix once |
| `wdctl selfcheck` | container healthcheck entrypoint |

## Chat steering

Talk to the Planner in the Web UI:

| Say | Effect |
|---|---|
| `what is the status of requirement #12` | report tasks, leases, attempts |
| `pause execution` | stop dispatching; running work untouched |
| `resume` | re-enable dispatch |
| `cancel task 3 and re-plan` | revoke lease, mark Blocked, re-plan |
| `add requirement: Add JWT auth` | create the issue and queue it |
| `change acceptance criteria for #12 to X` | update the issue and refresh briefs |

## Tests

```bash
pytest            # 250+ unit tests, no external services required
```

## Layout

```
src/clams/
  config.py         settings + HW-4 limits
  db.py             SQLite WAL schema + repositories
  contracts.py      TaskBrief / ExecutionResult / AlertEvent
  gh.py             GitHub REST + GraphQL sub-issue client
  claudio_client.py Claudio backend HTTP client
  bus.py            Redis Streams event bus (+ InMemoryBus)
  intake.py         FR-2 requirement intake
  decompose.py      FR-3 3–7 sub-issue decomposition
  planner.py        planning, re-planning, review judgement
  worktree.py       isolated git worktree lifecycle
  executor.py       FR-4 headless task execution
  review.py         FR-5 evidence gate
  steering.py       FR-1 chat steering
  watchdog.py       FR-6 check matrix + remediation ladder
  outbox.py         transactional outbox worker
  orchestrator.py   background loop
  cli.py            `clams` CLI
```
