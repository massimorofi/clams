# Quickstart

How to run Clams, configure the environment, understand every exposed address,
and set up the tokens — including the UI token.

- [1. Prerequisites](#1-prerequisites)
- [2. Install & configure](#2-install--configure)
- [3. Addresses & ports](#3-addresses--ports)
- [4. Tokens](#4-tokens)
- [5. First run & verify](#5-first-run--verify)
- [6. Native dev mode (optional)](#6-native-dev-mode-optional)

---

## 1. Prerequisites

| Requirement | Why |
|---|---|
| Docker + Compose v2 | the full stack (`./clamsctl`) |
| Python ≥ 3.12 + venv | native dev mode and the test suite |
| Node ≥ 20 | frontend build / Playwright |
| A **Claudio agent image** (`claudio/agent:latest`) | the two agent containers; build it from the [claudio repo](https://github.com/massimorofi/claudio) (`docker compose build agent`) or set `CLAUDIO_IMAGE` |
| An **OpenAI-compatible LLM endpoint** | both Claudio instances need a model (local or hosted) |
| A **GitHub token** with `repo` + `project` scopes | intake + board projection — see [GitHub setup](github.md) |

## 2. Install & configure

```bash
git clone https://github.com/massimorofi/clams && cd clams

# 1) Clams config
cp .env.example .env

# 2) Claudio container env (LLM endpoint etc.)
cp .env.claudio.example .env.claudio-planner
cp .env.claudio.example .env.claudio-executor

# 3) Project registry + limits
$EDITOR config/clams.yaml
```

Three config layers, in order of precedence:

1. **`config/agents/planner/config.yaml` / `config/agents/executor/config.yaml`** —
   bind-mounted (whole directory, so UI config edits can `rename()` over
   `config.yaml`) *over* the Claudio image's own config dir. The YAML wins
   over `CLAUDIO__*` env vars, so the LLM endpoint, auth token, approval mode,
   agent name and allowed workspace roots are set **here**.
   > ⚠ YAML gotcha: quote `approval_mode: "off"` — bare `off` parses as boolean
   > and silently blanks the whole config (auth included).
2. **`.env.claudio-planner` / `.env.claudio-executor`** — `CLAUDIO__*` env vars;
   only effective for keys the YAML leaves unset.
3. **`.env`** — Clams' own settings (`CLAMS_*`), parity-checked against
   `.env.example` by `tests/unit/test_env_parity.py`.

Key `.env` settings (full list in `.env.example`):

| Variable | Default | Notes |
|---|---|---|
| `CLAMS_API_TOKEN` | *(empty)* | **must set** — protects the API/UI |
| `CLAMS_GITHUB_TOKEN` | *(empty)* | **must set** for GitHub intake |
| `CLAMS_CLAUDIO_PLANNER_TOKEN` | *(empty)* | must equal `server.auth_token` in `config/agents/planner/config.yaml` |
| `CLAMS_CLAUDIO_EXECUTOR_TOKEN` | *(empty)* | must equal `server.auth_token` in `config/agents/executor/config.yaml` |
| `CLAMS_WORKTREE_UID/GID` | `1000` | owner of provisioned worktrees (the Claudio agent uid) |
| `CLAUDIO_IMAGE` | `claudio/agent:latest` | compose interpolation var for the agent image |

## 3. Addresses & ports

What each address exposes (per `docker-compose.yml`):

| Host address | Container | What it is | Reachable from |
|---|---|---|---|
| `:1075` | `api` :8200 | **Clams UI + REST + SSE** — the SPA at `/`, API under `/api/v1` | whole LAN |
| `:1072` | `claudio-planner` :8100 | **Claudio-P agent HTTP API** — `/healthz`, `/api/v1/chat` | whole LAN — debugging the planning agent |
| `:1073` | `claudio-executor` :8100 | **Claudio-E agent HTTP API** — same surface, headless executor | whole LAN — debugging the executor agent |
| `:1076` | `claudio-ui` :8080 | **Claudio web UI** — one instance, backend switchable between Planner and Executor | whole LAN — supervising planning & execution |
| *(internal)* `redis:6379` | redis | Redis Streams `clams:tasks` / `clams:events` / `clams:dlq` | docker network only — **never published** |
| *(internal)* `planner`, `executor-runner` | — | no published ports; they only *call* the services above | docker network only |

**Claudio web UI (1076) — backend URL:** by default **leave the URL empty**
(or clear it in the UI's backend settings). Empty = *same origin*: requests go
to `/api/…` on port 1076 and the UI's nginx proxies them to the **Planner**
agent. To drive the **Executor**, set the backend URL to
`http://<machine-lan-ip>:1073` (direct mode — both agents' CORS already allow
the 1076 origin); switch back to empty for the Planner. Never use `:8100` —
that port is container-internal and will fail with "Failed to fetch". The
token is the selected agent's `server.auth_token` (see §4.2).

Inside the `clams` docker network, services address each other by name
(`http://claudio-planner:8100`, `redis://redis:6379/0`) — this is wired in the
compose `environment:` block, which overrides `.env` inside containers.

The Clams API endpoints: `GET /api/v1/overview|workflows|tasks/{id}|questions|events(SSE)`,
`POST /api/v1/commands`, `GET /healthz|/readyz`.

## 4. Tokens

Four independent credentials — do not mix them up:

### 4.1 Clams API token (`CLAMS_API_TOKEN`) — the UI token

Protects every Clams API route (except `/healthz`). Generate one:

```bash
openssl rand -hex 32    # put the result in .env as CLAMS_API_TOKEN=...
```

**Setting it in the UI:** open the web UI → click the **gear icon** (top right)
→ paste the token into *API bearer token* → Save. It is stored in the browser's
`localStorage` under `clams_api_token` and sent as `Authorization: Bearer …`
on every request. The live event stream (SSE) passes it as a `?token=` query
parameter because `EventSource` cannot set headers — this is handled
automatically. Without a token the UI loads but every panel shows
*unauthorized*.

### 4.2 Claudio agent tokens (`CLAMS_CLAUDIO_PLANNER_TOKEN`, `CLAMS_CLAUDIO_EXECUTOR_TOKEN`)

Clams authenticates to each Claudio container with a bearer token. The token
the container actually checks comes from the **bind-mounted YAML**
(`server.auth_token`, default `dev-token-change-me`), *not* from env vars.
So: either keep the YAML default and set both `CLAMS_CLAUDIO_*_TOKEN` values to
`dev-token-change-me`, or change the YAML and mirror it in `.env`. A mismatch
shows up as `claudio chat failed 401` in the executor/planner logs.

### 4.3 GitHub token (`CLAMS_GITHUB_TOKEN`)

Classic PAT with `repo` + `project` scopes (the `project` scope is what lets
Clams move board cards). Full walkthrough, verification and failure modes:
[GitHub setup](github.md).

### 4.4 LLM credentials

Set in `config/claudio-{planner,executor}.yaml` under `llm:`
(`base_url`, `api_key`, `model`) — these win over `.env.claudio-*`.
For a local endpoint an empty/`local` key is fine.

## 5. First run & verify

```bash
./clamsctl rebuild      # build clams:local, pull base layers, recreate all
./clamsctl status       # every service "healthy" + status report
```

Checklist:

- [ ] `docker compose -p clams ps` → 8 services running; services with configured healthchecks report healthy
- [ ] `curl -s localhost:1075/readyz` → `{"status":"ready","db":true}`
- [ ] UI at `http://localhost:1075` renders the Overview after the token is set
- [ ] `clams status` shows `Planner alive` (heartbeat < 60 s)
- [ ] Register a project ([GitHub setup](github.md)) and drop a card in **Ready** —
      within ~60 s a workflow appears in the UI and the card moves to *In progress*

## 6. Native dev mode (optional)

To run Clams' three processes natively against dockerized Redis + Claudio
agents (the containers above):

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
docker compose -p clams up -d redis claudio-planner claudio-executor
clams migrate
clams planner & clams executor & clams api     # three terminals, or & together
```

The `.env` defaults (`127.0.0.1:1072/1073`, `./data`, `./work`) are made for
this mode.

## 7. Next steps

- Operate the stack: [Deployment & state machines](deployment.md) (§8 operational
  procedures), [clamsctl reference](clamsctl.md), [GitHub setup](github.md).
- A workflow is stuck in `blocked`/`failed`? One command recovers it:
  [Workflow recovery runbook](workflow-recovery.md).
