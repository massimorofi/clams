#!/usr/bin/env python3
"""Create the Clams implementation sub-issues under parent issue #5.

Native sub-issue linking uses the GraphQL `addSubIssue` mutation: the REST
POST /issues/{n}/sub_issues route 404s on this personal-account repo
(verified 2026-11-08), GraphQL works. A `parent:#5` label is applied as a
belt-and-braces marker so the hierarchy survives even if GraphQL is down.
"""
import json
import subprocess
import sys

OWNER = "massimorofi"
REPO = "claudio"
PARENT = 5

SUBISSUES = [
    dict(
        key="S1",
        title="S1: Scaffold, configuration & SQLite state layer",
        labels=["enhancement", "priority:HIGH", "clams"],
        body="""## Goal
Project skeleton, settings, and the durable local state layer (spec §5, §8.3, HW-4).

## Scope
- `pyproject.toml` (package `clams`, console script `clams`), `.gitignore`, `.env.example`
- `src/clams/config.py` — `pydantic-settings`, env prefix `CLAMS__`, file `.env`
  - GitHub: owner/repo/token; Redis URL; Claudio base URL + token; strixper base URL
  - HW-4 limits: `max_attempts=3`, `max_turns=40`, `max_wall_seconds=1800`, `max_replans=3`
  - HW-1/2 caps: `max_compute_queues=8`, `max_concurrent_engines=2`
  - Paths: db path, work root, data dir
- `src/clams/db.py` — SQLite WAL (`journal_mode=WAL`, `synchronous=FULL`,
  `busy_timeout=5000`, `wal_autocheckpoint=1000`, `foreign_keys=ON`)
  - Schema: `work_items`, `task_claims`, `alerts`, `outbox` exactly as spec §5
  - Repositories with single-writer ownership:
    - `WorkItemRepo`: upsert, get, list by status, set pause flag, set plan_json
    - `TaskClaimRepo`: atomic claim (INSERT ... WHERE NOT EXISTS / lease), renew, release, expired
    - `AlertRepo`: add, list unacked, ack
    - `OutboxRepo`: enqueue, claim pending, mark sent/failed
  - `migrate()` idempotent (CREATE TABLE IF NOT EXISTS)

## Acceptance criteria
- `pytest tests/test_config.py tests/test_db.py` passes
- Claim is atomic under concurrent callers (exactly one winner)
- WAL mode verified on the created DB file
""",
    ),
    dict(
        key="S2",
        title="S2: GitHub client — issues, labels, comments, native sub-issues",
        labels=["enhancement", "priority:HIGH", "clams"],
        body="""## Goal
Thin, well-tested GitHub client for the Work Plane (spec §3.2, FR-3).

## Scope
`src/clams/gh.py`:
- `list_issues(state, labels, since)` with pagination
- `get_issue(number)`, `create_issue(title, body, labels)`, `update_issue(...)`
- `add_comment(number, body)`, `list_comments(number)`
- `add_labels` / `remove_labels`, `ensure_labels()` (create the label set if missing)
- Sub-issues: `add_sub_issue(parent, child)` / `remove_sub_issue` / `list_sub_issues`
  - **Verified constraint:** REST `POST /issues/{n}/sub_issues` returns 404 on this
    personal repo (no Issue Types). GraphQL `addSubIssue` works → use GraphQL, and
    additionally apply a `parent:#N` label + `Parent: #N` body marker as fallback.
- `ping()` — HEAD https://api.github.com reachability probe (watchdog)
- Errors: typed `GitHubError` with status/body; retry with backoff on 5xx/secondary rate limit

## Acceptance criteria
- `pytest tests/test_gh.py` passes (transport mocked)
- Sub-issue add/list verified against the live API (smoke test, marked `live`)
""",
    ),
    dict(
        key="S3",
        title="S3: Redis event bus & inter-agent message contracts",
        labels=["enhancement", "priority:HIGH", "clams"],
        body="""## Goal
Redis Streams transport + validated JSON contracts (spec §4.2).

## Scope
- `src/clams/contracts.py` (pydantic v2):
  - `TaskBrief` — `$schema_version`, `task_issue`, `requirement_issue`, `objective`,
    `acceptance_criterion`, `scope{writable,read_only,forbidden}`,
    `git{base_branch,base_sha,work_branch}`, `limits{max_turns,max_wall_seconds}`
  - `ExecutionResult` — `task_issue`, `status`, `summary`,
    `evidence{commands[{cmd,exit_code,tail}], log_file}`,
    `artifact_delta{files_changed,commit,branch}`, `environment_state{git_head,working_tree_clean}`
  - `AlertEvent`, `CommandEnvelope` (user steering), `Heartbeat`
  - `validate_json(schema_name, payload)` → typed model or `ContractError`
- `src/clams/bus.py`:
  - Streams: `clams:tasks`, `clams:results`, `clams:alerts`, `clams:commands`
  - Consumer groups per worker; `XACK`, `XAUTOCLAIM` for redelivery
  - Dead-letter stream `clams:dead` for undecodable payloads
  - `pending_depth()` / `oldest_pending_age()` for the watchdog queue check
  - Pluggable `InMemoryBus` for unit tests (same interface)

## Acceptance criteria
- `pytest tests/test_bus.py tests/test_contracts.py` passes
- Round-trip encode/decode of both spec payloads byte-for-byte stable
- Malformed payload lands on dead-letter, never crashes the consumer
""",
    ),
    dict(
        key="S4",
        title="S4: Autonomous requirement intake (Ready → In Progress)",
        labels=["enhancement", "priority:HIGH", "clams"],
        body="""## Goal
FR-2: poll GitHub for `Status = Ready`, lock atomically, transition to `In Progress`.

## Scope
`src/clams/intake.py`:
- Sweep: query issues with label `status:Ready` (label-based status so it works without
  the Projects GraphQL `read:project` scope, which this token lacks — verified)
- For each candidate: `WorkItemRepo.claim_requirement()` in one transaction
  (INSERT ... ON CONFLICT DO NOTHING) → exactly one poller wins
- On win: set `gh_status = In Progress` on GitHub, mirror in `work_items`,
  enqueue `gh_status`/`gh_comment` outbox rows (never call GitHub inside the tx)
- Skip already-tracked items; tolerate GitHub outage (log + retry next sweep)
- Idempotent: re-running the sweep never double-claims

## Acceptance criteria
- `pytest tests/test_intake.py` passes
- Two concurrent sweeps claim disjoint sets (no double claim)
- GitHub transition failures roll the local row back to `pending`
""",
    ),
    dict(
        key="S5",
        title="S5: Planner decomposition into 3–7 native sub-issues",
        labels=["enhancement", "priority:HIGH", "clams"],
        body="""## Goal
FR-3: Planner deconstructs a requirement into 3–7 atomic sub-issues.

## Scope
- `src/clams/decompose.py`:
  - Build planner prompt from the requirement title/body + acceptance criteria
  - Call the Planner Claudio instance (`claudio_client.chat`) expecting strict JSON
  - Validate: 3 ≤ n ≤ 7, each with objective/acceptance_criterion/scope
  - Create child issues on GitHub, link with `add_sub_issue`, write `plan_json`
  - Retry once on invalid JSON; on second failure raise `DecompositionError` and
    alert (never silently accept a bad plan)
  - **Anti-pollution (spec §8.2):** input is the requirement body only — the planner
    never reads its own comment threads
- `src/clams/planner.py`: session management, `plan_requirement()`, `replan()`
  (increments `replans`, hard-capped at `max_replans`)

## Acceptance criteria
- `pytest tests/test_decompose.py` passes (LLM mocked)
- <3 or >7 items rejected with a clear error
- Re-plan beyond `max_replans` is refused
""",
    ),
    dict(
        key="S6",
        title="S6: Executor — isolated git worktree execution & evidence",
        labels=["enhancement", "priority:HIGH", "clams"],
        body="""## Goal
FR-4: Executor picks up a sub-issue, runs it in an isolated git worktree, collects evidence.

## Scope
- `src/clams/worktree.py`: `create(path, base_sha)`, `remove(path)`, `prune(orphans)`,
  `diff_summary(path)` (files changed + stat), `is_clean(path)`
- `src/clams/executor.py`:
  - Consume `TaskBrief` from `clams:tasks` (consumer group)
  - Take/extend the `task_claims` lease; refuse if claimed by another executor
  - Create `/work/task-{id}` worktree off `base_sha`, branch `task/{id}`
  - Run the task via the Executor Claudio instance with `max_turns` / `max_wall_seconds`
    enforced (async timeout, hard cancel)
  - Collect `ExecutionResult`: commands, exit codes, stdout tails, diff summary, git head
  - Publish to `clams:results`; on failure increment `attempts`, release lease
  - `attempts >= max_attempts` → mark `Blocked`, stop retrying

## Acceptance criteria
- `pytest tests/test_worktree.py tests/test_executor.py` passes
- Wall-clock overrun cancels the run and reports `timeout`
- Worktree is removed on completion and on failure (no orphans)
""",
    ),
    dict(
        key="S7",
        title="S7: Verification & evidence gate → Review",
        labels=["enhancement", "priority:HIGH", "clams"],
        body="""## Goal
FR-5: a requirement may only reach `Status = Review` with real execution evidence.

## Scope
`src/clams/review.py`:
- `verify_evidence(result)` — every required command `exit_code == 0`, non-empty stdout,
  `working_tree_clean`, commit present, acceptance criterion satisfied
- Aggregate per requirement: all sub-issues passed → requirement eligible for `Review`
- Post the evidence as a GitHub comment (formatted: commands, exit codes, tails, diff stat)
- Transition GitHub status label `status:Review`; mirror `work_items.status`
- Reject with `status:Blocked` + reason when evidence is missing/failing
- Evidence is re-checked from the stored payload, not trusted from the executor alone

## Acceptance criteria
- `pytest tests/test_review.py` passes
- A result with a non-zero exit code can never reach `Review`
- Missing diff / missing commit is rejected
""",
    ),
    dict(
        key="S8",
        title="S8: Human-in-the-loop chat steering (pause / cancel / re-plan / add)",
        labels=["enhancement", "priority:HIGH", "clams"],
        body="""## Goal
FR-1: chat commands from the Planner Web UI drive orchestrator state with priority.

## Scope
`src/clams/steering.py`:
- Parse user chat into a `CommandEnvelope`:
  | Utterance | Action |
  |---|---|
  | "what is the status of requirement #12" | read `work_items` + `task_claims`, render report |
  | "pause execution" | `user_paused = 1`, stop dispatch, running executor untouched |
  | "resume" | clear pause, re-enable dispatch |
  | "cancel task 3 and re-plan" | revoke lease, child issue → `Blocked`, trigger re-plan |
  | "add requirement: X" | create GitHub issue + enqueue intake |
  | "change acceptance criteria for #12 to X" | update issue body + `plan_json`, force brief refresh |
- Deterministic regex parser with an LLM fallback for fuzzy phrasing
- **Interrupt priority (spec §8.1):** the background loop checks the command stream
  *before* processing each queued event; pause takes effect immediately
- Every steering action is written to `work_items` and echoed to chat

## Acceptance criteria
- `pytest tests/test_steering.py` passes
- Pause observed by the dispatcher within one loop iteration
- Cancel revokes the lease and blocks the child issue
""",
    ),
    dict(
        key="S9",
        title="S9: Zero-LLM watchdog, check matrix & remediation ladder",
        labels=["enhancement", "priority:HIGH", "clams"],
        body="""## Goal
FR-6 + §6: deterministic supervision with no LLM in the loop.

## Scope
`src/clams/watchdog.py`:
- Sweep every 15–20s. Checks (spec §6.1):
  1. LLM engine health — 1-token probe to `base_url`; critical after 3 consecutive timeouts
  2. Strixper API — `GET /api/v1/runner/active`; engine switch error
  3. SQLite storage — `BEGIN IMMEDIATE; ROLLBACK`; critical if > 5000ms
  4. Storage volume free — `statvfs`; critical if < 2GB
  5. Redis queue — `XPENDING` depth + oldest age; critical if pending > 15m
  6. GitHub reachability — HEAD api.github.com; critical after 3 failed sweeps
  7. Agent heartbeats — heartbeat file age; critical if > 60s
- Remediation ladder (§6.2): pause+notify → diagnose → auto-recovery (engine restart via
  strixper, `docker restart`, prune worktrees) → verify → human escalation after 3 tries
- Every finding → `alerts` row + `clams:alerts` stream event
- HW-3: verify each engine's `--cache-disk` path is distinct; alert on collision
- `wdctl selfcheck` binary entrypoint for the Docker healthcheck
- **Zero LLM calls** — asserted by test (no planner/LLM client is even constructed)

## Acceptance criteria
- `pytest tests/test_watchdog.py` passes
- Each check fires the right severity on injected failure
- Ladder escalates to human after 3 failed recoveries
""",
    ),
    dict(
        key="S10",
        title="S10: Transactional outbox worker, orchestrator loop & CLI",
        labels=["enhancement", "priority:HIGH", "clams"],
        body="""## Goal
Wire every service into one durable loop with a usable CLI.

## Scope
- `src/clams/outbox.py`: claim `pending` rows → perform GitHub side-effect → mark `sent`;
  on error increment `attempts`, `failed` after 3; exponential backoff; at-least-once
  delivery with idempotency keys so a retry never double-posts a comment
- `src/clams/orchestrator.py`: background loop = intake → (plan) → dispatch → review,
  checking the user-command stream at every turn boundary; graceful shutdown on SIGTERM
- `src/clams/cli.py` (`clams`):
  `init-db`, `intake`, `plan <req>`, `dispatch`, `execute`, `review <req>`,
  `watch`, `status`, `doctor`, `up`, `down`
- `doctor` runs the check matrix once and prints a pass/fail table

## Acceptance criteria
- `pytest tests/test_outbox.py tests/test_orchestrator.py` passes
- Outbox retry is idempotent (no duplicate GitHub comments)
- `clams doctor` returns non-zero when a dependency is down
""",
    ),
    dict(
        key="S11",
        title="S11: Container infrastructure — compose, nginx gateway, Dockerfiles",
        labels=["enhancement", "priority:HIGH", "clams"],
        body="""## Goal
§3.1 + §7.1: run the whole system under Docker Compose with the UI on `:8100`.

## Scope
- `docker-compose.yml`: `planner`, `executor`, `watchdog`, `redis`, `gateway`
  per spec §7.1 (`read_only`, `cap_drop: [ALL]`, `tmpfs: /tmp`,
  `no-new-privileges`, healthchecks, `depends_on` conditions)
- `config/nginx.conf`: `:8100 → planner:8000`, `proxy_buffering off`, SSE-safe,
  websocket upgrade headers
- `docker/planner/Dockerfile`, `docker/executor/Dockerfile`, `docker/watchdog/Dockerfile`
- `.env.planner`, `.env.executor`, `.env.watchdog` templates
- Engine cold-start mitigation: `start_period: 120s`
- `Makefile`: `make up`, `make down`, `make test`, `make doctor`

## Acceptance criteria
- `docker compose config` validates
- Gateway serves the planner UI on `http://localhost:8100`
- Watchdog container healthcheck passes
""",
    ),
]


def gh(*args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def main():
    created = []
    for spec in SUBISSUES:
        r = gh("gh", "issue", "create", "--repo", f"{OWNER}/{REPO}",
               "--title", spec["title"], "--body", spec["body"],
               "--label", ",".join(spec["labels"]))
        if r.returncode != 0:
            print(f"FAIL create {spec['key']}: {r.stderr[:300]}")
            sys.exit(1)
        url = r.stdout.strip()
        num = int(url.rsplit("/", 1)[-1])
        created.append((spec["key"], num, url))
        print(f"{spec['key']} -> #{num} {url}")

    # link as native sub-issues of #5
    parent_gql = gh("gh", "api", "graphql", "-f",
                    'query={ repository(owner:"%s", name:"%s") { issue(number: %d) { id } } }'
                    % (OWNER, REPO, PARENT))
    parent_id = json.loads(parent_gql.stdout)["data"]["repository"]["issue"]["id"]
    for key, num, _ in created:
        child_gql = gh("gh", "api", "graphql", "-f",
                       'query={ repository(owner:"%s", name:"%s") { issue(number: %d) { id } } }'
                       % (OWNER, REPO, num))
        child_id = json.loads(child_gql.stdout)["data"]["repository"]["issue"]["id"]
        r = gh("gh", "api", "graphql", "-f",
               'query=mutation($p: ID!, $s: ID!) { addSubIssue(input: {issueId: $p, subIssueId: $s}) { issue { number } } }',
               "-f", f"p={parent_id}", "-f", f"s={child_id}")
        if r.returncode != 0:
            print(f"WARN link {key} #{num}: {r.stderr[:200]}")
        else:
            print(f"linked {key} #{num} under #{PARENT}")
    print(json.dumps(created, indent=2))


if __name__ == "__main__":
    main()
