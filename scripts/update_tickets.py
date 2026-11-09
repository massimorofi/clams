#!/usr/bin/env python3
"""Post fix evidence to the leftover issues and close them.

Fixed issues are closed; issues that are blocked or only partially fixed get
their comment but are left open.
"""
import subprocess
import sys

OWNER = "massimorofi"
REPO = "clams"

# number -> (close?, body)
TICKETS = {
    2: (True, """## Fixed in `45405c5`

**Root cause confirmed:** `Executor.write_heartbeat()` had zero call sites — it
was dead code, so `executor.json` was never created and the watchdog's
`agent_heartbeats` check could never pass.

**Fix**
- `run_once()` now calls `self.write_heartbeat("poll")` on every poll, so an
  idle-but-healthy executor still refreshes its liveness signal.
- New `run_forever()` runs a **background beat task** on
  `executor.heartbeat_interval`. This matters because a task may legitimately
  run for `max_wall_seconds` (default 1800s) against a 60s heartbeat budget —
  writing the heartbeat only between tasks would flag a busy executor as dead.
- New settings: `CLAMS__EXECUTOR__HEARTBEAT_INTERVAL` (20s),
  `CLAMS__EXECUTOR__POLL_INTERVAL` (1s).

**Verified**
- Unit tests: `write_heartbeat` produces a valid `Heartbeat`; `run_once`
  creates `executor.json` from an empty queue; `run_forever` advances the
  timestamp between beats.
- Live: running `clams watch` produced `orchestrator.json` and `planner.json`
  with fresh timestamps.

**Caveat — still failing in the deployed stack.** The fix is correct, but
`docker-compose.yml` runs the *stock* `claudio/agent` image for the executor
service, so `clams execute` never actually runs there and `executor.json` is
still absent in the running stack. That is a deployment-wiring problem, tracked
separately in #10.
"""),

    3: (True, """## Fixed in `45405c5`

**Fix:** `wdctl check` and `wdctl run` now construct and inject both
dependencies:

```python
bus = RedisBus(settings.redis.url, settings.redis.namespace)
gh  = GitHubClient(settings.github.owner, settings.github.repo,
                   settings.github.token, settings.github.api_base)
svc = WatchdogService(store, settings, bus=bus, gh=gh)
```

**Verified live** — the two previously-silent checks now report real values:

```
[OK] redis_task_queue: depth=0, oldest idle 0s
[OK] github_reachability: reachable
```

(previously: `no bus configured, skipped` / `no github client, skipped`)

**Not done — the deeper design issue.** Injecting the dependencies fixes the
observed bug, but the underlying pattern is still unsafe: `bus` and `gh` are
`Optional` and their absence yields `ok=True`. A check that reports success
when it did not run is a latent trap for the next person who adds a check the
same way. Making "skipped" a distinct non-OK state is filed as #11.
"""),

    4: (True, """## Fixed in `45405c5`

**Root cause was not missing logging — it was buffering.** `cmd_watch` *did*
`print("clams orchestrator running")`, but stdout is **block-buffered when
redirected**, so the line sat in the buffer and was lost when the process was
killed. That is why the captured log was exactly 0 bytes.

**Fix**
- `logging.basicConfig(..., stream=sys.stderr, force=True)` — stderr is
  line-buffered, so output survives SIGTERM and shows under `docker logs`.
- `flush=True` on the existing prints.
- Per-cycle `INFO` line from the orchestrator:
  `cycle done: commands=0 intake=0 planned=0 dispatched=0 reviewed=0 delivered=0 errors=0`
- Per-step failures now logged with the step name, not just appended to the
  report.
- Global `--log-level {DEBUG,INFO,WARNING,ERROR}` flag.

**Verified** — same command that previously produced 0 bytes now produces 811:

```
clams orchestrator running (Ctrl-C to stop)
2026-11-09T12:16:59 INFO clams.watch: orchestrator started: poll_interval=20.0s ...
2026-11-09T12:16:59 INFO clams.orchestrator: cycle done: commands=0 intake=0 ...
```
"""),

    6: (True, """## Fixed in `45405c5`

`PLAN.md:59` corrected from `:8100 → planner:8000` to `:8100 → planner:8080`.

Verified: `grep -n "planner:8000" PLAN.md` returns nothing, and
`config/nginx.conf:17` reads `server planner:8080;`.
"""),

    7: (True, """## Fixed in `45405c5`

Removed `issue5_body.md` and `FINAL_UPDATE_5.md` and added both to
`.gitignore` under a `# scratch / one-off artifacts` section.

Verified: neither file is tracked; `git status` clean after the commit.
"""),

    9: (True, """## Delivered in `45405c5`

`clamsctl` — a console entry point (`clams.ctl:main`), matching the repo's
existing `clams` / `wdctl` convention rather than a loose shell script, so it
is installed with the package and covered by tests.

| Command | Does |
|---------|-----|
| `clamsctl build [services...]` | build images (optionally a subset) |
| `clamsctl start` | `up -d --remove-orphans` |
| `clamsctl stop` | stop, containers kept |
| `clamsctl restart` | stop then start |
| `clamsctl recreate [--yes]` | `down` then fresh `up -d --force-recreate` |
| `clamsctl status` | container status table |
| `clamsctl health` | docker healthchecks + HTTP probes, non-zero exit on failure |
| `clamsctl cleanup [--yes]` | remove containers, project volumes, dangling images |
| `clamsctl logs <svc> [--tail N]` | follow logs |

**Design choices**
- Every action echoes the exact `docker` command before running it — the
  script is inspectable, not magic.
- `recreate` and `cleanup` **refuse to run without `--yes` when stdin is not a
  TTY**, so they cannot fire accidentally from a script or CI.
- Exit codes are meaningful: 0 success, 1 failure, 2 refused.

**Verified live**
- `status` listed all five containers correctly.
- `health` ran 5 healthchecks + 2 HTTP probes; both endpoints returned 200.
- `cleanup` without `--yes` under a non-interactive stdin → exit 2, nothing run.
- 11 unit tests in `tests/test_ctl.py`, including the guard behaviour.

**Found while testing:** `health` correctly flagged the **gateway as unhealthy —
it has no healthcheck defined** in `docker-compose.yml`, so it reports `Up`
rather than `healthy`. Filed as #12.
"""),
}


def main():
    for num, (close, body) in TICKETS.items():
        r = subprocess.run(
            ["gh", "issue", "comment", str(num), "--repo", f"{OWNER}/{REPO}",
             "--body", body],
            capture_output=True, text=True)
        if r.returncode != 0:
            print(f"FAIL comment #{num}: {r.stderr[:200]}")
            continue
        if close:
            c = subprocess.run(
                ["gh", "issue", "close", str(num), "--repo", f"{OWNER}/{REPO}"],
                capture_output=True, text=True)
            if c.returncode != 0:
                print(f"FAIL close #{num}: {c.stderr[:200]}")
                continue
            print(f"#{num} updated and closed")
        else:
            print(f"#{num} updated (left open)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
