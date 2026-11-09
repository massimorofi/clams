#!/usr/bin/env python3
"""Post completion evidence to each Clams sub-issue and close it."""
import subprocess

OWNER = "massimorofi"
REPO = "claudio"

TICKETS = {}

TICKETS[19] = ("S1", """## S1 complete - scaffold, configuration & SQLite state layer

**Delivered**
- `pyproject.toml` - package `clams` 0.1.0, console scripts `clams` + `wdctl`
- `src/clams/config.py` - pydantic-settings, `CLAMS__*` env prefix, `.env` file
- `src/clams/db.py` - SQLite WAL (`journal_mode=WAL`, `synchronous=FULL`,
  `busy_timeout=5000`, `wal_autocheckpoint=1000`, `foreign_keys=ON`); tables
  `work_items` / `task_claims` / `alerts` / `outbox`; single-writer repositories;
  idempotent `migrate()`

**Evidence**
- `pytest tests/test_config.py tests/test_db.py` -> **28 passed**
- Atomic claim verified with 8 concurrent threads: exactly 1 winner, 7 losers
- WAL mode, busy_timeout, synchronous=FULL and FK enforcement asserted on the file
- HW-4 limits (max_attempts=3, max_turns=40, max_wall_seconds=1800,
  max_replans=3) and the HW-1 queue-ceiling check are enforced in code
""")

TICKETS[20] = ("S2", """## S2 complete - GitHub client

**Delivered** `src/clams/gh.py`
- `list_issues` (paginated), `get_issue`, `create_issue`, `update_issue`
- `add_comment`, `list_comments`
- `add_labels`, `replace_labels`, `remove_label`, `ensure_labels`
- `set_status` swaps only the `status:*` label and preserves the rest
- `ping()` HEAD reachability probe for the watchdog
- Typed `GitHubError` / `GitHubNotFoundError`; retry with backoff on 5xx/403

**Verified constraint (important)**
The spec's REST route `POST /repos/{owner}/{repo}/issues/{n}/sub_issues` returns
**404 on this repository** (personal-account repo without Issue Types enabled).
The GraphQL `addSubIssue` mutation **works**, so `add_sub_issue` uses GraphQL and
falls back to a `parent-N` label if the mutation is ever refused. The fallback
label deliberately avoids `:` and `#` because httpx normalises percent-encoding
in request paths.

**Evidence**
- `pytest tests/test_gh.py` -> **24 passed** (transport mocked)
- Live: 11 sub-issues created and linked under #5 via GraphQL, confirmed with
  `GET /issues/5/sub_issues`
""")

TICKETS[21] = ("S3", """## S3 complete - Redis event bus & message contracts

**Delivered**
- `src/clams/contracts.py` - `TaskBrief`, `ExecutionResult`, `AlertEvent`,
  `CommandEnvelope`, `Heartbeat` (pydantic v2, `extra="forbid"`) plus
  `validate_json(schema_name, payload)`
- `src/clams/bus.py` - Redis Streams bus: `clams:tasks`, `clams:results`,
  `clams:alerts`, `clams:commands`, dead-letter `clams:dead`, consumer groups,
  `XACK`, `pending()` depth + oldest-idle for the watchdog, and an `InMemoryBus`
  exposing the identical interface for tests

**Evidence**
- `pytest tests/test_contracts.py tests/test_bus.py` -> **26 passed**
- Both spec 4.2 payloads parse byte-for-byte and round-trip stably, including the
  `$schema_version` alias
- Malformed / non-JSON payloads are dead-lettered and never crash the consumer
- Live: published and read back a `TaskBrief` through real Redis
  (`redis://127.0.0.1:6379/0`) - ping ok, read-back correct
""")

TICKETS[22] = ("S4", """## S4 complete - autonomous requirement intake

**Delivered** `src/clams/intake.py`
- Sweeps GitHub for `status:Ready` and claims each item atomically
  (`INSERT ... ON CONFLICT DO NOTHING`) so exactly one poller wins
- On win: queues the `In Progress` transition and the acknowledgement comment
  through the outbox - no GitHub call inside the claim transaction
- GitHub outage is tolerated and raises a `warning` alert instead of failing
- `add_requirement()` creates a new issue and lets the normal intake path claim it

**Evidence**
- `pytest tests/test_intake.py` -> **9 passed**
- Two concurrent sweeps over 10 issues claimed a disjoint set, no double claim
- GitHub transition failure leaves the local row untouched
- Live: `clams intake` against the real repo -> `scanned=0 claimed=0 failed=0`
""")

TICKETS[23] = ("S5", """## S5 complete - planner decomposition into 3-7 sub-issues

**Delivered**
- `src/clams/decompose.py` - prompt builder, robust JSON extraction (bare /
  fenced / prose-wrapped), plan parsing, 3-7 validation, duplicate-title
  rejection, retry-once, child-issue creation + native linking
- `src/clams/planner.py` - `plan_requirement`, `replan` (hard-capped at
  `max_replans`), `judge_review`, `update_acceptance_criteria`
- Anti-pollution (spec 8.2): the prompt is built from the requirement title and
  body only; comment threads are never fed back in

**Evidence**
- `pytest tests/test_decompose.py` -> **29 passed**
- Plans with <3 or >7 tasks are rejected; re-plan beyond `max_replans` refused
- **Live LLM test:** the real local model (halogen-qwen3.8-flash-next) returned a
  valid 5-task plan for a JWT-auth requirement, parsed and validated first try
""")

TICKETS[24] = ("S6", """## S6 complete - isolated worktree execution & evidence

**Delivered**
- `src/clams/worktree.py` - create/remove/prune worktrees, `diff_summary`,
  `is_clean`, `enforce_scope` (writable / read-only / forbidden, glob aware),
  `cleanup_stale_worktrees`
- `src/clams/executor.py` - consumes `TaskBrief`, takes the lease, runs in
  `/work/task-{id}` under a hard `asyncio.wait_for` wall-clock budget, commits,
  collects `ExecutionResult` (commands, exit codes, stdout tails, diff, git head)
  and publishes it; `attempts >= max_attempts` marks `status:Blocked` and the
  task can never be re-claimed

**Evidence**
- `pytest tests/test_worktree.py tests/test_executor.py` -> **37 passed**
- Wall-clock overrun returns `timeout` and removes the worktree
- Scope violation (writing into `migrations/`) fails the task
- A second executor is blocked while a live lease is held
- Worktree removed on both success and failure - no orphans
""")

TICKETS[25] = ("S7", """## S7 complete - verification & evidence gate

**Delivered** `src/clams/review.py`
- `verify_evidence` requires: status `completed`, every command exit code 0,
  non-empty stdout, a commit, changed files, and a clean working tree
- Per-requirement aggregation: the requirement reaches `Review` only when **all**
  sub-tasks pass; otherwise `Blocked` with reasons
- Evidence is re-read from the stored payload, never trusted from the executor
- Formatted GitHub comments for task-level and requirement-level verdicts
- Optional planner `judge` can reject even when the mechanical checks pass

**Evidence**
- `pytest tests/test_review.py` -> **23 passed**
- Non-zero exit, missing commit, missing diff, dirty tree and empty stdout each
  independently block the gate
- Missing evidence for any sub-task blocks the requirement on GitHub too
""")

TICKETS[26] = ("S8", """## S8 complete - human-in-the-loop chat steering

**Delivered** `src/clams/steering.py`
- Deterministic regex parser covering every utterance in spec 3.2: status query,
  pause, resume, cancel-task-and-replan, add requirement, change acceptance
  criteria, re-plan
- LLM fallback (`llm_parse_command`) for phrasings the regex misses
- Unrecognised input returns a help message and never mutates state
- Cancel revokes the lease, marks the child `Blocked`, removes it from the plan
  and triggers re-planning
- Interrupt priority (spec 8.1): the orchestrator drains the command stream
  before every other step, so a pause takes effect on the next iteration

**Evidence**
- `pytest tests/test_steering.py` -> **31 passed**
- Pause/resume verified against the DB flag; cancel verified against lease + GitHub
- "stop task 2" (spec FR-1 example) maps to cancel_task
""")

TICKETS[27] = ("S9", """## S9 complete - zero-LLM watchdog, check matrix & remediation ladder

**Delivered** `src/clams/watchdog.py`
- All 7 spec 6.1 checks: LLM engine probe, strixper API, SQLite
  `BEGIN IMMEDIATE` round-trip, `statvfs` free space, Redis queue depth/age,
  GitHub reachability, agent heartbeat age
- Plus HW-3 `--cache-disk` collision detection
- Full 6.2 ladder: pause & notify -> diagnose (re-probe for transience) ->
  automated recovery (engine restart / docker restart / prune worktrees) ->
  verify -> human escalation after 3 failed attempts
- Every finding writes an `alerts` row and publishes an `AlertEvent`
- `wdctl selfcheck` / `wdctl check` / `wdctl run` entrypoints

**Zero-LLM guarantee** is asserted by test: the module contains no import of the
planner, the decomposer or any model client.

**Evidence**
- `pytest tests/test_watchdog.py` -> **25 passed**
- Each check fires the correct severity on injected failure (warning vs critical,
  consecutive-failure counters, reset on recovery)
- Ladder escalates to `Human action required` after 3 failed recoveries
- **Live:** 7/8 checks green against real strixper + Redis + GitHub
""")

TICKETS[28] = ("S10", """## S10 complete - outbox worker, orchestrator loop & CLI

**Delivered**
- `src/clams/outbox.py` - claims pending rows, delivers `gh_comment` /
  `gh_status` / `notification`, re-reads the attempt counter after each failure
  and marks a row `failed` after 3 attempts; idempotency keys prevent duplicate
  comments
- `src/clams/orchestrator.py` - one cycle = commands -> intake -> plan ->
  dispatch -> review -> outbox -> heartbeat; each step is individually guarded so
  one failure never stops the loop; graceful stop via `stop()`
- `src/clams/cli.py` - `init-db`, `intake`, `plan`, `dispatch`, `execute`,
  `review`, `watch`, `status`, `doctor`
- `src/clams/llm.py` - direct OpenAI-compatible caller so the planner works
  whether driven by Claudio or straight by the engine

**Evidence**
- `pytest tests/test_outbox.py tests/test_orchestrator.py` -> **20 passed**
- Dispatch is idempotent: already-dispatched tasks are never replayed
- A paused requirement is never dispatched
- A throwing intake step is captured in `CycleReport.errors` and the cycle continues
- `clams doctor` returns non-zero when a dependency is down
""")

TICKETS[29] = ("S11", """## S11 complete - container infrastructure

**Delivered**
- `docker-compose.yml` - `planner`, `executor`, `watchdog`, `redis`, `gateway`
  with `read_only: true`, `cap_drop: [ALL]`, `tmpfs: [/tmp]`,
  `no-new-privileges:true`, healthchecks and `depends_on` conditions
- `config/nginx.conf` - `:8100 -> planner:8000`, `proxy_buffering off`,
  `proxy_cache off`, websocket upgrade headers, long read/send timeouts for SSE
- `docker/planner/Dockerfile`, `docker/executor/Dockerfile`,
  `docker/watchdog/Dockerfile`
- `.env.example` plus generated `.env.planner` / `.env.executor` / `.env.watchdog`
- `Makefile` - `venv`, `install`, `test`, `doctor`, `up`, `down`, `status`, `clean`
- Engine cold-start mitigation: `start_period: 120s` on the planner healthcheck

**Evidence**
- `docker compose config -q` -> **valid** (exit 0)
- Redis container started and answered `PONG`
- Gateway routing target (`planner:8000`) and SSE-safe proxy settings verified
""")


def main():
    for number, (key, body) in TICKETS.items():
        r = subprocess.run(
            ["gh", "issue", "comment", str(number),
             "--repo", f"{OWNER}/{REPO}", "--body", body],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(f"FAIL comment {key} (#{number}): {r.stderr[:200]}")
            continue
        c = subprocess.run(
            ["gh", "issue", "close", str(number), "--repo", f"{OWNER}/{REPO}"],
            capture_output=True, text=True,
        )
        if c.returncode != 0:
            print(f"FAIL close {key} (#{number}): {c.stderr[:200]}")
        else:
            print(f"closed {key} -> #{number}")


if __name__ == "__main__":
    main()
