#!/usr/bin/env python3
"""Post the #14 evidence comment and close the issue."""
import subprocess
import sys

REPO = "massimorofi/clams"

BODY = """### Implemented

The Clams frontend is built and deployed per the spec in `frontend_specs.md`.

**Stack:** Vite + React 19 + TypeScript + Tailwind CSS v4, served by a FastAPI backend over one origin.

#### Backend — `src/clams/api.py`

Every endpoint from §4 of the spec:

| Method | Endpoint | Status |
|---|---|---|
| `GET` | `/api/v1/project` | ✅ repo, branch, commit, worktree, tree-clean state |
| `GET`/`PUT` | `/api/v1/config` | ✅ hot-update, validated before apply |
| `POST` | `/api/v1/cli/exec` | ✅ whitelisted subcommands |
| `POST` | `/api/v1/planner/action` | ✅ retry/pause/resume/alter/approve/reject |
| `GET` | `/api/v1/status` | ✅ header-bar aggregate |
| `GET` | `/api/v1/tasks`, `/api/v1/alerts` | ✅ feeds |
| `GET` | `/api/v1/events` (SSE) | ✅ `telemetry:update`, `logs:stream`, `planner:reply` |
| `WS` | `/ws` | ✅ `planner:message` → `planner:reply` |

Exposed as `clams serve` (`CLAMS__API__PORT`, default 8200).

#### Frontend — `frontend/`

All four tabs from §3 plus the header bar from §2:

- **Header** — state badge (`Idle`/`Executing`/`Paused`/`Error`), repo link, active agent counter, Pause / Resume / Emergency Stop.
- **Tab 1 Project & Config** — GitHub section with pull/sync; dual-mode editor (sliders/toggles/text **and** raw JSON) with a diff viewer and Hot-Apply.
- **Tab 2 CLI Runner** — command grid, dynamic parameter form generated per command, xterm.js terminal with search and export.
- **Tab 3 Activity & Agents** — pipeline visualiser, agent matrix with heartbeat state, filterable alert/diagnostic feed.
- **Tab 4 Planner Chat** — threaded chat over WebSocket with REST fallback, quick intervention buttons, context-tag help.

§5 responsive rules: dark theme by default, 44px minimum touch targets, breakpoints for desktop/tablet/mobile.

#### Deployment

`docker/frontend/Dockerfile` is a multi-stage build — Node builds the SPA, the Python runtime serves it from the API, so production is one container and one origin (no CORS). Added as the `frontend` service in `docker-compose.yml`.

### Evidence

All 8 services healthy after deploy:

```
$ clamsctl health
Container healthchecks:
  [OK  ] redis: healthy
  [OK  ] watchdog: healthy
  [OK  ] planner: healthy
  [OK  ] agent: healthy
  [OK  ] orchestrator: healthy
  [OK  ] executor: healthy
  [OK  ] frontend: healthy
  [OK  ] gateway: healthy
HTTP probes:
  [OK  ] planner http://127.0.0.1:8100/healthz -> 200
  [OK  ] gateway http://127.0.0.1:8100/ -> 200
  [OK  ] frontend http://127.0.0.1:8200/healthz -> 200
All checks passed.
```

SPA and assets served from the container:

```
$ curl -s -o /dev/null -w "%{http_code}\\n" http://127.0.0.1:8200/
200
$ curl -s -o /dev/null -w "%{http_code}\\n" http://127.0.0.1:8200/assets/index-DfsIBF2E.css
200
2026-11-09T22:31:41 INFO clams.api: serving frontend from /app/frontend/dist
```

Config hot-apply round-trip:

```
$ curl -X PUT .../api/v1/config -d '{"values":{"poll_interval":11.0}}'
{"ok":true,"changed":{"poll_interval":{"from":20.0,"to":11.0}}, ...}

$ curl -X PUT .../api/v1/config -d '{"values":{"limits.max_concurrent_engines":99}}'
422
```

The second call is the important one: a patch that breaks the HW-1 queue ceiling is refused rather than half-applied to a running system.

### Bugs found and fixed while building this

1. **Undeclared dependencies.** `fastapi`/`uvicorn` were installed into the dev venv but never added to `pyproject.toml`, so the built image crashed on start with `ModuleNotFoundError: No module named 'uvicorn'`. Added to `dependencies`.
2. **Static path wrong in containers.** The SPA mount resolved relative to `__file__`, which under an installed package lands in `site-packages`, not `/app` — so the SPA 404'd in Docker while working on the host. Now resolved by candidate search, overridable with `CLAMS__API__STATIC_DIR`.
3. **Credential leak.** `GET /api/v1/config` returned the GitHub token in cleartext to the browser. Secret keys (`token`, `auth_token`, `api_key`, `password`, `secret`) are now masked in every config response:

```
{"settings":{"github":{"owner":"massimorofi","repo":"clams","token":"***redacted***", ...
```

### Tests

`tests/test_api.py` — 18 tests: REST contract, config validation refusal on a bad patch, CLI whitelist enforcement (including a check that no whitelisted name contains shell metacharacters), SSE stream framing, WebSocket round-trip and unknown-type rejection, 404 handling, and secret redaction.

Full suite: **319 passed**.

### Commits

`011a5b5` backend · `a6c4659` SPA · `d386257` API tests · `44ebaa5` Docker + redaction
"""


def main():
    subprocess.run(
        ["gh", "issue", "comment", "14", "--repo", REPO, "--body", BODY],
        check=True, capture_output=True, text=True,
    )
    print("commented #14")
    subprocess.run(
        ["gh", "issue", "close", "14", "--repo", REPO],
        check=True, capture_output=True, text=True,
    )
    print("closed #14")


if __name__ == "__main__":
    sys.exit(main())
