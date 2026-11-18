# Clams v2 — Planner–Executor orchestrator for Claudio

Clams turns GitHub Issues into a durable work queue and drives two **unmodified**
Claudio agent containers — a **Planner** (decomposition + review judgement) and
an **Executor** (headless task runner in isolated git worktrees) — with a
web UI for supervision. One SQLite DB, exactly two Redis streams.

Spec: [CLAMS_V2_SUPER_PLAN.md](doc/CLAMS_V2_SUPER_PLAN.md) ·
Tracking issue: [massimorofi/clams#130](https://github.com/massimorofi/clams/issues/130)

## Architecture

```
GitHub Issues/Projects ──intake──► PLANNER (singleton, only DB writer)
     ▲  │                          intake · plan · dispatch · review
outbox │  ▼                        reaper · watchdog · outbox relay
     ┌─┴──────────────┐  read-only ┌──────────────┐
     │ SQLite (WAL)   ◄────────────│ API :8200    ├──► Browser (SPA)
     └────────────────┘  commands  │ FastAPI+SSE  │
                                   └──────────────┘
planner ──XADD clams:tasks──► Redis 7 ──XREADGROUP──► EXECUTOR-RUNNER (1..N)
planner ◄──XADD clams:events── Redis ◄──XACK discipline── │  worktree · gate
                                                          └─HTTP─► Claudio-E
```

Five invariants (enforced and tested):

1. **Single writer** — only the planner mutates domain tables; the API inserts
   into `commands` only.
2. **DB is truth, queues are transport** — losing Redis loses no work.
3. **Every side-effect via outbox** — no GitHub call or XADD inside a business
   transaction.
4. **Evidence beats claims** — task success = deterministic gate pass
   (`run:` commands exit 0 with output, ≥1 commit, scope respected, clean
   tree), never model text.
5. **Everything bounded** — attempts, turns, wall time, replans, plan size,
   outbox retries, LLM calls per cycle.

## Quick start (dev)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env                      # defaults are parity-checked in CI
clams migrate
docker run -d -p 127.0.0.1:6379:6379 redis:7-alpine
clams planner        # terminal 1
clams executor       # terminal 2 (needs a Claudio-E on :1073)
clams api            # terminal 3 → http://127.0.0.1:8200
```

## Full stack (Docker)

```bash
cp .env.example .env
cp .env.claudio.example .env.claudio-planner    # fill LLM keys
cp .env.claudio.example .env.claudio-executor   # approval_mode=off
./clamsctl rebuild && ./clamsctl status
```

## CLI

| Command | Purpose |
|---|---|
| `clams planner` | run the planner loop (intake/plan/dispatch/review/reaper/outbox/watchdog) |
| `clams executor` | run an executor-runner (consumes `clams:tasks`) |
| `clams api` | serve REST + SSE + SPA on :8200 |
| `clams migrate` | apply pending migrations |
| `clams status` | JSON status snapshot |
| `clams command pause\|resume --by operator` | queue a control command |
| `clams healthcheck planner\|executor\|api` | container healthcheck |

## Tests

```bash
pytest                    # unit + contract + integration + chaos
pytest -m redis           # needs local Redis
pytest tests/contract -m live   # needs a live claudio/agent container on :1072
```

## Layout

```
clamsctl · docker-compose.yml · Dockerfile · pyproject.toml · .env.example
config/clams.yaml        projects, limits, agents
migrations/001_init.sql  schema v1
src/clams/
  config.py contracts.py bus.py outbox.py claudio_client.py cli.py
  db/        conn + repositories (single-writer SQLite)
  github/    client · intake · projection
  planner/   loop · plan · dispatch · review · reaper · commands · watchdog
  executor/  runner · worktree · evidence · scope
  api/       app · sse · auth
frontend/    Vite + React + TS + Tailwind + TanStack Query
tests/       unit · contract · integration · chaos
```
