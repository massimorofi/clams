#!/usr/bin/env python3
"""Create one GitHub issue per leftover item from the initial Clams
implementation, and link each as a native sub-issue of the parent issue.

Parent: massimorofi/clams#1  ("Initial open issues fixing")

Each item below was verified against the delivered tree rather than
recounted from memory; the verification command is recorded in the body.
"""
import json
import subprocess
import sys

OWNER = "massimorofi"
REPO = "clams"
PARENT = 1

# (title, labels, body)
ITEMS = [
    (
        "Executor heartbeat is never written — watchdog always reports agent_heartbeats FAIL",
        ["bug"],
        """### Problem

The watchdog's primary liveness signal for the executor can never be satisfied.
`wdctl check` reports:

```
[FAIL] agent_heartbeats: stale heartbeats: executor (missing)
```

### Root cause (verified)

`Executor.write_heartbeat()` is defined at `src/clams/executor.py:289` but is
**never called** from anywhere in the codebase:

```
$ grep -rn "write_heartbeat" src/clams/
src/clams/executor.py:289:    def write_heartbeat(self, detail: str = "") -> Path:
```

`run_once()`, `process_brief()` and the executor loop never call it. Meanwhile
`probe_heartbeats()` in `src/clams/watchdog.py:159` defaults to
`required=("planner", "executor")`, so a missing `executor.json` is a hard
failure.

The orchestrator's `step_heartbeat()` writes only `orchestrator` and `planner`
(`src/clams/orchestrator.py:285`), never `executor`.

### Impact

The remediation ladder for a dead or wedged executor cannot fire correctly,
because the condition it keys on (`executor` heartbeat stale) is permanently
true — it cannot distinguish "executor died" from "executor was never wired".

### Fix

Call `self.write_heartbeat(...)` once per executor loop iteration (and at
startup), and ensure `CLAMS__PATHS__HEARTBEAT_DIR` resolves to a volume shared
with the watchdog container.

### Acceptance

- `wdctl check` reports `agent_heartbeats: all heartbeats fresh` with the
  executor running.
- Killing the executor makes `agent_heartbeats` go stale within
  `HEARTBEAT_MAX_AGE` and triggers the recovery step.
""",
    ),
    (
        "wdctl check / run never inject the bus or GitHub client — 2 of 8 checks silently pass",
        ["bug"],
        """### Problem

The containerised watchdog silently skips two of its eight checks and reports
them as **OK**:

```
[OK  ] redis_task_queue: no bus configured, skipped
[OK  ] github_reachability: no github client, skipped
```

### Root cause (verified)

`WatchdogService.__init__` accepts `bus` and `gh` as optional dependencies
(`src/clams/watchdog.py:230`), but `main()` constructs it without them:

```python
# src/clams/watchdog.py, main()
settings = Settings.load()
store = open_store_for(settings)
svc = WatchdogService(store, settings)   # no bus=, no gh=
```

`check_queue()` and `check_github()` therefore take their "not configured"
branch and return `ok=True`.

### Impact

This is a safety gap, not a cosmetic one: a Redis backlog or a GitHub outage is
**invisible** to the watchdog that is supposed to catch it, and the sweep output
actively misleads by printing `[OK]` for checks that never ran.

### Fix

Either construct and inject the Redis bus and GitHub client in `main()` (the
host `clams doctor` path already does this), or make "skipped" a distinct state
that is never reported as `ok` and never counted toward a healthy sweep.

### Acceptance

- `wdctl check` in the container actually probes Redis and GitHub.
- With Redis stopped, the sweep reports `redis_task_queue` FAIL (not OK).
""",
    ),
    (
        "clams watch emits no output — the loop looks hung",
        ["bug"],
        """### Problem

`clams watch` writes nothing at all. Verified:

```
$ timeout 45 clams watch > /tmp/watch.log 2>&1; echo rc=$?
rc=124
$ wc -c /tmp/watch.log
0
```

Zero bytes on stdout and stderr over 45 seconds, then killed by the timeout.
There is no way to tell the loop is alive, which cycle it is in, or what it did.

### Impact

An operator cannot distinguish a healthy idle loop from a deadlock. Combined
with the heartbeat gap, the primary operational surface for the orchestrator is
completely silent.

### Fix

Add logging at INFO level: cycle start/end, intake counts, tasks dispatched,
tasks completed, outbox deliveries, and any per-step error. Configure the root
logger in the CLI entrypoint so it works without extra setup.

### Acceptance

- `clams watch` prints at least one line per cycle.
- Errors in a step are visible without a debugger.
""",
    ),
    (
        "GitHub Projects board is not integrated — token lacks project scope",
        ["enhancement"],
        """### Problem

The GitHub Project (the "Claim" board) is not driven by Clams at all. Item
state lives only in `status:*` labels.

### Root cause (verified)

The token in use has no project scope:

```
$ gh auth status | grep scopes
- Token scopes: 'gist', 'read:org', 'repo', 'workflow'
```

Any project query fails:

```
$ gh api graphql -f query='{ viewer { projects(first:5) { nodes { number } } } }'
{"errors":[{"type":"NOT_FOUND","path":["viewer","projects"], ...
  "message":"Projects (classic) is being deprecated in favor of the new
   Projects experience ..."}]}
```

Note this is the **new** Projects experience, so the implementation needs the
V2 GraphQL schema (`ProjectV2`, `addProjectV2ItemById`,
`updateProjectV2ItemFieldValue`), not the classic `Project` API.

### Fix

1. Mint a token with `read:project` and `update:project`.
2. Implement board column sync driven by the same state transitions that
   currently drive `status:*` labels, so label and column never diverge.
3. Resolve the project and its status field IDs once at startup rather than
   per-item.

### Acceptance

- Moving a work item through its lifecycle moves its card on the board.
- A missing/renamed status option fails loudly at startup, not per-item.
""",
    ),
    (
        "PLAN.md documents the wrong planner port (8000 vs actual 8080)",
        ["documentation"],
        """### Problem

`PLAN.md:59` documents the gateway route as:

```
├── config/nginx.conf         # :8100 → planner:8000, SSE-safe
```

The planner's bundled nginx config actually listens on **8080**:

```nginx
server {
    listen 8080;
```

This was one of the defects found while bringing the stack up; the compose
file, `config/nginx.conf`, `config.py` and the `.env` files were corrected,
but `PLAN.md` was not.

### Fix

Update the `PLAN.md` line to `:8100 → planner:8080` and grep the docs for any
other stale `8000` references that mean the planner (host `:8000` legitimately
belongs to strixper).

### Acceptance

- `grep -n "planner:8000" PLAN.md` returns nothing.
""",
    ),
    (
        "Scratch files committed to the repository",
        ["documentation"],
        """### Problem

Two working files from the original spec-reading and ticket-writing process are
tracked in the repo and are not part of the package:

- `issue5_body.md` — a 23 KB dump of the upstream spec issue body.
- `FINAL_UPDATE_5.md` — the text of a comment posted to `claudio#5`.

Both are build artifacts. They ship in the sdist/wheel file listing and will
drift out of date immediately.

### Fix

`git rm issue5_body.md FINAL_UPDATE_5.md` and add them to `.gitignore`. If the
spec text is genuinely useful to the project, move it to `docs/` with a note
recording its source and date rather than leaving it at the repo root.

### Acceptance

- Neither file is tracked.
- `git status` is clean.
""",
    ),
    (
        "Inconsistent commit authorship in the initial history",
        ["documentation"],
        """### Problem

The first two commits were authored by the agent identity rather than by you:

```
232be3b  Clams: Claudio Multi-Agent System orchestrator (spec issue #5)
         Author: Claudio <claudio@local>
df032ee  docs: final evidence update posted to claudio#5
         Author: Claudio <claudio@local>
```

Later commits use `massimorofi <massimorofi@users.noreply.github.com>`.

### Impact

Cosmetic but it affects attribution and, if you rely on commit-based
contribution stats or signed commits, those two commits will not match your
identity.

### Fix

If you want consistent authorship, rewrite the two commits:

```bash
git rebase -i --root \\
  --exec 'git commit --amend --no-edit \\
    --author=\\"massimorofi <massimorofi@users.noreply.github.com>\\"'
git push --force-with-lease
```

This rewrites published history, so it should only be done while the repo has
no other consumers.

### Acceptance

- `git log --format='%an <%ae>' | sort -u` shows one identity.
""",
    ),
]


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def create(title, labels, body):
    cmd = [
        "gh", "issue", "create",
        "--repo", f"{OWNER}/{REPO}",
        "--title", title,
        "--body", body,
    ]
    for lab in labels:
        cmd += ["--label", lab]
    r = run(cmd)
    if r.returncode != 0:
        print(f"  FAIL create: {r.stderr.strip()[:200]}")
        return None
    return r.stdout.strip()


def link(child, parent=PARENT):
    """Link child as a native sub-issue of parent via GraphQL."""
    q = f'{{ repository(owner:"{OWNER}", name:"{REPO}") {{ issue(number:{parent}) {{ id }} }} }}'
    r = run(["gh", "api", "graphql", "-f", "query=" + q])
    if r.returncode != 0:
        return False, r.stderr.strip()[:150]
    pid = json.loads(r.stdout)["data"]["repository"]["issue"]["id"]

    q2 = f'{{ repository(owner:"{OWNER}", name:"{REPO}") {{ issue(number:{child}) {{ id }} }} }}'
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
    created = []
    for i, (title, labels, body) in enumerate(ITEMS, 1):
        print(f"[{i}/{len(ITEMS)}] {title}")
        url = create(title, labels, body)
        if not url:
            continue
        num = url.rstrip("/").split("/")[-1]
        ok, err = link(int(num))
        if ok:
            print(f"  -> #{num} created and linked to #{PARENT}   {url}")
        else:
            print(f"  -> #{num} created, LINK FAILED: {err}   {url}")
        created.append(num)

    print(f"\nCreated {len(created)} issues: {', '.join('#' + n for n in created)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
