#!/usr/bin/env python3
"""Round 3: create new follow-up tickets and update/close the fixed ones."""
import json
import subprocess
import sys

OWNER = "massimorofi"
REPO = "clams"
PARENT = 1


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


NEW = [
    (
        "Watchdog container cannot reach the LLM engine (hardcoded 127.0.0.1)",
        ["bug"],
        """### Problem

Found while verifying #11 live. Inside the watchdog container the LLM probe
fails:

```
[FAIL] llm_engine_health: probe failed (1/3)
```

The same probe passes from the host. The cause is the default base URL:

```python
# src/clams/watchdog.py
self.llm_base_url = llm_base_url or "http://127.0.0.1:8731"
```

`127.0.0.1` inside a container is the container itself, not the host. The
engine runs on the host at `:8731`, so the watchdog can never reach it.

Note the inconsistency: `.env.watchdog` already sets
`CLAMS__STRIXPER__BASE_URL=http://host.docker.internal:8000` for the same
reason, but the LLM default was never updated to match.

### Impact

The watchdog's LLM health check is permanently red in the containerised
deployment. After `llm_failures_critical` (3) consecutive sweeps this is
reported as a **critical** condition, which triggers pause-and-notify and the
engine-restart rung of the remediation ladder — for an engine that is
perfectly healthy.

Worse, this interacts badly with the #11 change: the failure is real now
rather than silently OK, so a misconfigured default URL produces a loud false
alarm.

### Fix

Default to `http://host.docker.internal:8731` for containerised use, or make
the value required rather than defaulted. Also worth adding to `.env.example`
so it is discoverable:

```
CLAMS__LLM__BASE_URL=http://host.docker.internal:8731
```

### Acceptance

- `wdctl check` inside the watchdog container reports
  `llm_engine_health: engine responded`.
- No false critical escalation from the LLM check on a healthy engine.
"""),
]

# (number, close?, body)
UPDATES = {
    11: (True, """## Fixed in `7d4f7d7`

Implemented exactly as proposed.

**Changes**
- `CheckResult` gained a `skipped: bool = False` field.
- `SweepReport.healthy` now returns `not self.criticals and not self.skipped_names`
  — a skipped check can never produce a healthy sweep.
- `SweepReport.skipped_names` added.
- `to_text()` renders `[SKIP]` for skipped checks and appends a
  `skipped: <names>` summary line.
- Both "not configured" paths (`check_queue`, `check_github`) now return
  `ok=False, severity="warning", skipped=True` instead of `ok=True`.

**Note on severity:** I used `warning` rather than `critical` deliberately.
Marking skips as critical would list them in `criticals` *and* `skipped_names`
simultaneously, double-reporting the same thing and making it ambiguous whether
the system failed or was merely unwired. `healthy` is False either way, so the
safety property is preserved without the ambiguity.

**Tests** — three added, covering the contract:
- `test_skipped_check_makes_sweep_unhealthy`
- `test_skipped_check_is_printed_as_skip_not_ok`
- `test_fully_wired_sweep_has_no_skips`

Two existing tests (`test_healthy_sweep_does_not_pause`,
`test_remediation_recovers_and_resumes`) had to be updated: they built the
service without a bus and asserted a healthy sweep, which the old broken
semantics permitted. They now inject an `InMemoryBus`.

**Verified live** — rebuilt watchdog now reports real probe results:

```
[OK  ] redis_task_queue: depth=0, oldest idle 0s
[OK  ] github_reachability: reachable
```

(previously both printed `skipped`)

**One consequence worth knowing:** because a skipped check now blocks
`healthy`, the remediation ladder's final `verify.healthy` gate can never pass
if a dependency is genuinely absent — the watchdog escalates instead of
claiming recovery. That is the intended behaviour per the ticket, but it means
a partially-wired deployment escalates loudly rather than limping along.

**New finding surfaced during verification** — the LLM probe inside the
container fails because of a hardcoded `127.0.0.1` default. Filed separately.
"""),
    12: (True, """## Fixed in `7d4f7d7`

Added a healthcheck to the `gateway` service:

```yaml
healthcheck:
  test: ["CMD-SHELL", "wget -q --spider -T 5 http://127.0.0.1/healthz || exit 1"]
  interval: 15s
  timeout: 5s
  retries: 3
  start_period: 10s
```

**Two implementation notes**
- `nginx:alpine` ships busybox `wget`, **not** `curl` — a `curl`-based test
  would fail outright.
- The probe hits `/healthz` through the proxy rather than just checking the
  process is alive. That matters here specifically: earlier in this project
  the planner's nginx started fine and then died with
  `host not found in upstream "claudio-agent:8100"` — a live process with a
  broken config. A process-liveness check would have called that healthy.

**Verified live**

```
$ clamsctl health
  [OK  ] redis: healthy
  [OK  ] watchdog: healthy
  [OK  ] executor: healthy
  [OK  ] planner: healthy
  [OK  ] gateway: healthy      <-- was "Up 7 hours" before
All checks passed.
```

`docker inspect` confirms `healthy`, and `docker compose config -q` validates.
"""),
}


def main():
    for i, (title, labels, body) in enumerate(NEW, 1):
        print(f"[new {i}/{len(NEW)}] {title}")
        url = create(title, labels, body)
        if not url:
            continue
        num = url.rstrip("/").split("/")[-1]
        ok, err = link(int(num))
        print(f"  -> #{num} {'linked' if ok else 'LINK FAILED: ' + err}  {url}")

    for num, (close, body) in UPDATES.items():
        r = run(["gh", "issue", "comment", str(num), "--repo", f"{OWNER}/{REPO}",
                 "--body", body])
        if r.returncode != 0:
            print(f"FAIL comment #{num}: {r.stderr[:200]}")
            continue
        if close:
            c = run(["gh", "issue", "close", str(num), "--repo", f"{OWNER}/{REPO}"])
            if c.returncode != 0:
                print(f"FAIL close #{num}: {c.stderr[:200]}")
                continue
            print(f"#{num} updated and closed")
        else:
            print(f"#{num} updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
