#!/usr/bin/env python3
"""Post evidence comments to the leftover tickets (#5 #8 #10 #13) and close them."""
import subprocess
import sys

REPO = "massimorofi/clams"

EVIDENCE = {
    13: """### Fixed

The LLM probe target is now resolved from configuration instead of a hard-coded
loopback, with container detection.

**What changed**
- `src/clams/config.py`: added `LOOPBACK_HOSTS`, `detect_in_container()` /
  `in_container()`, `resolve_llm_base_url()`, and `LlmSettings.container_base_url`
  with a URL validator. `LlmSettings.resolved_base_url` is the URL to actually dial.
- `src/clams/watchdog.py`: `WatchdogService` now uses `settings.llm.resolved_base_url`
  when no explicit override is passed, and logs the effective target at startup.
- `.env.example`: documented `CLAMS__LLM__BASE_URL` / `CLAMS__LLM__CONTAINER_BASE_URL`.
- `tests/test_llm_url.py`: 9 new tests covering loopback detection, native vs
  container resolution, explicit overrides, URL validation, and watchdog wiring.

**Evidence — acceptance criteria**

`wdctl check` inside the watchdog container:

```
Watchdog sweep @ 2026-11-09T18:15:10Z
  [OK  ] llm_engine_health: engine responded
  [OK  ] strixper_api: Halogen-Qwen3.8-flash-next-NPU-Prefill
  [OK  ] sqlite_storage: 0.1ms
  [OK  ] storage_volume_free: 101.36 GiB free
  [OK  ] redis_task_queue: depth=0, oldest idle 0s
  [OK  ] github_reachability: reachable
  [OK  ] agent_heartbeats: all heartbeats fresh
  [OK  ] engine_cache_isolation: no engines declared
```

- `llm_engine_health: engine responded` — **met**.
- No false critical escalation: the LLM check passes, so `llm_failures_critical`
  never increments on a healthy engine — **met**.

Commit: `dc573f6` (with #10).
""",
    10: """### Fixed

The compose file now runs four distinct roles instead of pointing planner and
executor at stock Claudio images.

**What changed**
- `docker-compose.yml` rewritten with the four-role topology:

| Service | Image | Command |
|---|---|---|
| `planner` | `claudio/web:latest` | stock entrypoint (Web UI + chat, :8080) |
| `agent` | `claudio/agent:latest` | stock entrypoint (executor-side agent, :8100) |
| `orchestrator` | built `docker/orchestrator/Dockerfile` | `clams watch` |
| `executor` | built `docker/executor/Dockerfile` | `clams worker` |

  plus `redis`, `watchdog`, `gateway`.
- `docker/orchestrator/Dockerfile` added (the Clams loop, previously mislabelled
  "planner").
- **Port bug found while fixing this**: the executor targeted
  `http://executor:8101`, but the Claudio agent serves **8100**. Verified against
  the running container (`0x1FA4 = 8100`) and `claudio/compose.yaml`
  (`CLAUDIO__SERVER__PORT: "8100"`). Corrected to `http://agent:8100`.
- `clams worker` added (`src/clams/cli.py`): `clams execute` is a one-shot that
  exits 1 on an empty queue, which under `restart: unless-stopped` is an
  indistinguishable crash loop. The daemon form polls indefinitely.
- `clams healthcheck` added: heartbeat-freshness liveness probe, used as the
  Docker `HEALTHCHECK` for `orchestrator` and `executor`.
- `.env.orchestrator` / `.env.agent` added; `CLAMS__PATHS__*` set explicitly so
  the DB, work root and heartbeat dir agree across containers.
- `clamsctl.SERVICES` updated to the new service list.

**Evidence — acceptance criteria**

```
$ clamsctl health
Container healthchecks:
  [OK  ] redis: healthy
  [OK  ] watchdog: healthy
  [OK  ] planner: healthy
  [OK  ] agent: healthy
  [OK  ] orchestrator: healthy
  [OK  ] executor: healthy
  [OK  ] gateway: healthy
HTTP probes:
  [OK  ] planner http://127.0.0.1:8100/healthz -> 200
  [OK  ] gateway http://127.0.0.1:8100/ -> 200

All checks passed.
```

`wdctl check` reports `agent_heartbeats: all heartbeats fresh`, and the shared
heartbeat volume now contains all four:

```
executor.json  orchestrator.json  planner.json  watchdog.json
```

Commit: `dc573f6`.
""",
    8: """### Fixed

History rewritten so every commit carries one identity.

**Before**

```
$ git log --format='%an <%ae>' | sort -u
Claudio <claudio@local>
massimorofi <massimorofi@users.noreply.github.com>
MaX <massimo.rofi@gmail.com>

$ git log --format='%cn <%ce>' | sort -u
Claudio <claudio@local>
GitHub <noreply@github.com>
massimorofi <massimorofi@users.noreply.github.com>
```

**After**

```
$ git log --format='%an <%ae>' | sort -u
massimorofi <massimorofi@users.noreply.github.com>

$ git log --format='%cn <%ce>' | sort -u
massimorofi <massimorofi@users.noreply.github.com>
```

**How**

`git-filter-repo` with a mailmap mapping all three prior identities to
`massimorofi <massimorofi@users.noreply.github.com>`. `filter-repo` was used
rather than `rebase -i --root` because the history contains a merge commit
(`Merge remote 'Initial commit'`) which an interactive rebase would flatten.

**Content integrity verified**

The tree hash is unchanged across the rewrite, so only authorship metadata moved:

```
OLD HEAD tree: c3e4d10530285cb2c5ddcbc4b8ec1384917a2d04
NEW HEAD tree: c3e4d10530285cb2c5ddcbc4b8ec1384917a2d04
```

A full backup of the pre-rewrite history was taken first
(`git bundle create /tmp/clams-pre-history-rewrite.bundle --all`).

**Also fixed while here**

The commit that added the new role env files had written the GitHub token into
tracked history (`.env.orchestrator`). Those files are now gitignored and
untracked; `git log --all -S <token>` returns nothing. The repo-local git
identity was also set to the noreply address so future commits are consistent.

Commit: `10ee668` (history rewritten and force-pushed with `--force-with-lease`).
""",
    5: """### Implemented — full verification blocked on token scope

**What was built**

`src/clams/projects.py`:
- `ProjectBoard` — Projects V2 client using the new GraphQL schema
  (`ProjectV2`, `addProjectV2ItemById`, `updateProjectV2ItemFieldValue`),
  not the deprecated classic `Project` API.
- `resolve()` — looks up the project ID, the status field ID and the option ID
  for every status **once at startup**. Tries the `user` lookup first and falls
  back to `organization`, since the owner may be either.
- `set_status_option()`, `add_item()`, `find_item()` (paginated), `move()`.
- `board_from_settings(settings)` — builds and validates the board from
  `CLAMS__PROJECT__*`.
- `DEFAULT_STATUS_MAP` maps the existing `status:*` labels to board columns,
  overridable via `CLAMS__PROJECT__STATUS_MAP`.

**Design points from the ticket**

1. *Same state transition* — the card move is issued from
   `GitHubClient.set_status()`, the single choke point that already writes the
   `status:*` label. Label and column are written by one call, so they cannot
   diverge.
2. *Resolve once at startup* — resolution happens in `board_from_settings()`
   before any work item is touched, wired into `clams watch` and `clams doctor`.
3. *Fail loudly* — a missing project, a missing/renamed status field, or a
   missing status option raises `ProjectConfigError` at startup with the list of
   what was actually found. Every mutation also checks both the top-level GraphQL
   `errors` array and the `...errors { field message }` payload, so a rejected
   write never passes silently.

**Verification**

- 22 unit tests in `tests/test_projects.py` covering resolution, the renamed-option
  startup failure, field-level error surfacing, pagination, add-on-missing, and the
  label/board coupling through `set_status`. Full suite: **301 passed**.
- Live loud-failure confirmed against the real GitHub API:

```
$ CLAMS__PROJECT__ENABLED=true CLAMS__PROJECT__NUMBER=1 python -c "..."
LOUD STARTUP FAILURE (expected with a non-project token):
  cannot read project #1 for 'massimorofi': Your token has not been granted the
  required scopes to execute this query. The 'projectV2' field requires one of
  the following scopes: ['read:project'], but your token has only been granted
  the: ['gist', 'read:org', 'repo', 'workflow'] scopes.
```

**What remains to close this out (needs you)**

The token in this environment has `gist, read:org, repo, workflow` and **no
project scope**, and no other token exists in the environment. Minting one needs
your GitHub account, so the live card-movement check could not be run.

To enable:
1. Create a token with `repo` + `read:project` + `update:project`.
2. Set `CLAMS__PROJECT__ENABLED=true`, `CLAMS__PROJECT__NUMBER=<n>` (the number
   from `/users/massimorofi/projects/<n>`), and optionally
   `CLAMS__PROJECT__STATUS_FIELD` / `CLAMS__PROJECT__STATUS_MAP` to match your
   board's actual column names.
3. Restart. If the board's columns differ from the defaults, startup now fails
   with the exact list of missing options instead of corrupting cards.

Commit: `10ee668`.
""",
}


def main():
    # #8, #10, #13 are fully verified -> comment and close.
    # #5 is implemented but its acceptance criterion needs a token with
    # read:project/update:project that does not exist in this environment, so it
    # gets the evidence comment and stays open with the blocker stated.
    close = {8, 10, 13}
    for num, body in EVIDENCE.items():
        subprocess.run(
            ["gh", "issue", "comment", str(num), "--repo", REPO, "--body", body],
            check=True, capture_output=True, text=True,
        )
        print(f"commented #{num}")
        if num in close:
            subprocess.run(
                ["gh", "issue", "close", str(num), "--repo", REPO],
                check=True, capture_output=True, text=True,
            )
            print(f"closed   #{num}")
        else:
            print(f"left open #{num} (blocked on token scope)")


if __name__ == "__main__":
    sys.exit(main())
