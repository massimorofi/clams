# clamsctl — operator command reference

`clamsctl` wraps `docker compose -p clams -f docker-compose.yml` and the
in-container `clams` CLI. Run it from anywhere; it `cd`s to its own directory.

Prerequisites checked by every command: Docker daemon running, Compose v2,
`.env` present (else: `cp .env.example .env`).

## Commands

| Command | Parameters | What it does |
|---|---|---|
| `clamsctl start` | — | `up -d --wait` — start the stack and block until healthchecks pass; prints the UI URL |
| `clamsctl stop` | — | `stop` — stop containers, keep everything |
| `clamsctl restart` | `[service]` | restart all services or one (`planner`, `executor-runner`, `api`, `redis`, `claudio-planner`, `claudio-executor`) |
| `clamsctl rebuild` | — | `pull --ignore-buildable` (best-effort) → `build --pull` (rebuilds `clams:local` from the current source) → `up -d --wait --force-recreate` (recreates everything, so Claudio containers pick up a newer `claudio/agent:latest`) |
| `clamsctl status` | — | `docker compose ps` + `clams status` inside the planner container — a human-readable report (planner heartbeat, pause state, schema version, workflow/task/outbox counts split into in-flight vs terminal, executor online/offline, open alerts). `clams status --json` for the machine-readable snapshot |
| `clamsctl logs` | `[service]` | `logs -f --tail=200` for one service or all |
| `clamsctl migrate` | — | `docker compose run --rm planner clams migrate` — apply pending SQL migrations (idempotent; the planner also migrates at boot) |
| `clamsctl backup` | — | SQLite online backup inside the container, copied out to `backups/clams-<timestamp>.db` |
| `clamsctl monitor` | — | zero-LLM monitoring sweep: `docker compose ps`, per-service log grep (ERROR/WARNING/Traceback/500, last 15 min), DLQ sample from Redis, then the in-planner `clams monitor` report (gate-failure histogram, rolling success rate, turns/success, ready backlog, open alerts, running-task lease table, recent stall evidence). `clams monitor --json` for machine-readable output |
| `clamsctl pause` | — | queues a `pause` command **as operator** — planner stops intake/planning/dispatch (events + reaper keep running) |
| `clamsctl resume` | — | clears an operator pause. Note: the watchdog never auto-resumes an *operator* pause; a watchdog pause auto-resumes after 3 healthy probes |
| `clamsctl down` | — | removes containers, **keeps volumes** (DB, Redis, sessions, worktrees). Prompts; `YES=1` to skip |
| `clamsctl nuke` | — | `down -v` — **deletes all data** (DB, queues, sessions, worktrees). Prompts; `YES=1` to skip |

## Parameters & environment

- **`YES=1`** — non-interactive mode for the two destructive commands
  (`YES=1 ./clamsctl down`).
- **`CLAUDIO_IMAGE`** (in `.env`) — which Claudio agent image the Claudio
  services use; `clamsctl rebuild` picks up a newly built image automatically.
- Service names accepted by `restart`/`logs`: `redis`, `claudio-planner`,
  `claudio-executor`, `planner`, `executor-runner`, `api`.

## The `clams` CLI (inside any container, or from the venv)

```
clams planner | executor | api        run one role
clams migrate                         apply migrations
clams status [--json]                 status snapshot (human-readable; --json for scripts)
clams command pause|resume --by WHO   queue a control command
clams healthcheck planner|executor|api  container healthcheck (exit 0 = healthy)
```

`clams healthcheck planner` fails when `runtime_state.heartbeat` is older than
`CLAMS_PLANNER_HEARTBEAT_MAX_AGE_S` (60 s); the executor healthcheck probes
Redis + Claudio-E; the api healthcheck hits its own `/healthz`.

## Control commands via the API

`clamsctl pause/resume` are shortcuts for the general command surface —
`POST /api/v1/commands` with `{kind, target, payload}`. Kinds:
`pause`, `resume`, `cancel` (workflow or task id), `replan` (workflow),
`retry` (failed/blocked task), `answer` (`question_id` + `answer`),
`adhoc` (`goal` + `repo`), `config_patch` (project registry edits). The planner
applies them at the start of the next cycle (5 s), even while paused.
