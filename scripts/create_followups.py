#!/usr/bin/env python3
"""Create the follow-up tickets discovered while fixing #2/#3/#4/#6/#7/#9,
each linked as a native sub-issue of #1.

Run BEFORE scripts/update_tickets.py so the issue numbers referenced in the
fix comments match reality.
"""
import json
import subprocess
import sys

OWNER = "massimorofi"
REPO = "clams"
PARENT = 1

ITEMS = [
    (
        "Compose runs stock Claudio images, so no service actually runs the Clams loop",
        ["bug"],
        """### Problem

The deployed stack never runs the Clams orchestrator or executor loop.
`docker-compose.yml` points the `planner` and `executor` services at the
**stock** images:

```yaml
planner:
  image: claudio/web:latest      # stock Claudio web UI
executor:
  image: claudio/agent:latest    # stock Claudio agent
```

Only `watchdog` uses `build:`. The Clams-wrapped images that *do* run the
loops are built but unused:

```
docker/planner/Dockerfile   ->  CMD ["clams", "watch"]
docker/executor/Dockerfile  ->  CMD ["clams", "execute"]
```

### Consequence

This is the root cause of the heartbeat failure that survived the #2 fix. The
code is correct — `clams watch` writes `orchestrator.json` + `planner.json`,
and `clams execute` writes `executor.json` — but neither command runs in this
deployment, so `wdctl check` still reports:

```
[FAIL] agent_heartbeats: stale heartbeats: planner (2356s old), executor (missing)
```

### The architectural confusion

`docker/planner/Dockerfile` builds a plain Python image running `clams watch`,
which is the **orchestrator loop**, not a web server. But the planner is
supposed to be the user-facing Claudio instance **with a Web UI**, which is
what `claudio/web:latest` provides. The two roles got merged into one service
name and only one of them is currently satisfied.

Note `clams execute` is a *driver* that calls a Claudio agent over HTTP at
`executor.base_url:8101` — so the agent and the driver are two different
processes and cannot be the same container.

### Proposed shape (needs a decision)

Four distinct roles, currently collapsed into three services:

| Role | Image | Command |
|------|-------|---------|
| planner web UI | `claudio/web` | stock entrypoint |
| orchestrator loop | built from `docker/planner/Dockerfile` | `clams watch` |
| Claudio agent (executor side) | `claudio/agent` | stock entrypoint |
| executor driver | built from `docker/executor/Dockerfile` | `clams execute` |

Plus the existing `redis`, `watchdog` and `gateway`.

### Acceptance

- `wdctl check` reports `agent_heartbeats: all heartbeats fresh`.
- Every Clams process that the spec describes is actually running and visible in
  `clamsctl status`.
"""),
    (
        "Watchdog checks report OK when they are skipped",
        ["bug"],
        """### Problem

Injecting the bus and GitHub client fixed the immediate bug in #3, but the
underlying pattern is still unsafe and will bite the next check that gets added.

`check_queue()` and `check_github()` both begin with:

```python
if self.bus is None:
    return CheckResult(CHECK_QUEUE, True, "info", "no bus configured, skipped")
```

`ok=True` on the "did not run" path. A check that cannot run is reported as
**passing**.

### Why this matters

The watchdog is the last line of defence. If a config regression drops the bus
wiring again, the sweep prints a wall of `[OK]` while queue-backlog detection
is silently gone — which is worse than no watchdog, because it manufactures
false confidence.

### Fix

Introduce a third state distinct from pass/fail:

```python
@dataclass
class CheckResult:
    name: str
    ok: bool
    severity: str
    detail: str
    skipped: bool = False
```

- `skipped=True` must never satisfy `report.healthy`.
- Print `[SKIP]` rather than `[OK]`.
- If a check is skipped in a mode where it is required, treat that as a
  startup error rather than a runtime surprise.

### Acceptance

- With no bus configured, `report.healthy` is False and the line reads
  `[SKIP] redis_task_queue`.
- A test asserts that a skipped check cannot produce a healthy sweep.
"""),
    (
        "Gateway service has no healthcheck",
        ["bug"],
        """### Problem

Found by `clamsctl health` while delivering #9. The gateway reports:

```
[FAIL] gateway: Up 7 hours
```

where every other service reports `healthy`. `docker-compose.yml` defines a
healthcheck for `planner`, `executor` and `watchdog`, but not for `gateway`:

```yaml
gateway:
  image: nginx:alpine
  ports: ["8100:80"]
  volumes:
    - ./config/nginx.conf:/etc/nginx/nginx.conf:ro
  depends_on:
    - planner
    - executor
```

### Consequence

`depends_on: condition: service_healthy` can never gate on the gateway, and a
nginx that is up but misconfigured (bad config reload, upstream gone) looks
identical to a healthy one from the orchestrator's point of view.

Note this also bit us earlier in a different form: the planner's nginx failed
with `host not found in upstream "claudio-agent:8100"` — a config error that a
healthcheck would have surfaced immediately instead of after the container
reported `Up`.

### Fix

Add a healthcheck that actually exercises the proxy path rather than just the
process:

```yaml
healthcheck:
  test: ["CMD", "wget", "-q", "--spider", "http://127.0.0.1/healthz"]
  interval: 15s
  timeout: 5s
  retries: 3
  start_period: 10s
```

(`nginx:alpine` ships `wget`, not `curl`.)

### Acceptance

- `clamsctl health` reports `gateway: healthy`.
- Breaking the nginx config makes the gateway go unhealthy within one interval.
"""),
]


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def create(title, labels, body):
    cmd = ["gh", "issue", "create", "--repo", f"{OWNER}/{REPO}",
           "--title", title, "--body", body]
    for lab in labels:
        cmd += ["--label", lab]
    r = run(cmd)
    if r.returncode != 0:
        print(f"  FAIL create: {r.stderr.strip()[:200]}")
        return None
    return r.stdout.strip()


def link(child, parent=PARENT):
    q = (f'{{ repository(owner:"{OWNER}", name:"{REPO}") {{ '
         f'issue(number:{parent}) {{ id }} }} }}')
    r = run(["gh", "api", "graphql", "-f", "query=" + q])
    if r.returncode != 0:
        return False, r.stderr.strip()[:150]
    pid = json.loads(r.stdout)["data"]["repository"]["issue"]["id"]

    q2 = (f'{{ repository(owner:"{OWNER}", name:"{REPO}") {{ '
          f'issue(number:{child}) {{ id }} }} }}')
    r2 = run(["gh", "api", "graphql", "-f", "query=" + q2])
    if r2.returncode != 0:
        return False, r2.stderr.strip()[:150]
    cid = json.loads(r2.stdout)["data"]["repository"]["issue"]["id"]

    mut = ("mutation($p: ID!, $s: ID!) { addSubIssue(input: {issueId: $p, "
           "subIssueId: $s}) { issue { number } } }")
    r3 = run(["gh", "api", "graphql", "-f", f"p={pid}", "-f", f"s={cid}",
              "-f", "query=" + mut])
    if r3.returncode != 0:
        return False, r3.stderr.strip()[:150]
    return True, ""


def main():
    made = []
    for i, (title, labels, body) in enumerate(ITEMS, 1):
        print(f"[{i}/{len(ITEMS)}] {title}")
        url = create(title, labels, body)
        if not url:
            continue
        num = url.rstrip("/").split("/")[-1]
        ok, err = link(int(num))
        print(f"  -> #{num} {'linked' if ok else 'LINK FAILED: ' + err}  {url}")
        made.append(num)
    print(f"\nCreated: {', '.join('#' + n for n in made)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
