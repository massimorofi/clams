## Clams delivered — spec issue #5 complete

`Clams` (Claudio Multi-Agent System) is implemented as a standalone Python
package at `~/Dev/code/ai/clams`, built to this specification.

### Sub-issues: 11/11 complete and closed

| Sub | Scope | Issue |
|-----|-------|-------|
| S1 | Scaffold, configuration & SQLite state layer | #19 |
| S2 | GitHub client — issues, labels, comments, native sub-issues | #20 |
| S3 | Redis event bus & inter-agent message contracts | #21 |
| S4 | Autonomous requirement intake (Ready → In Progress) | #22 |
| S5 | Planner decomposition into 3–7 native sub-issues | #23 |
| S6 | Executor — isolated git worktree execution & evidence | #24 |
| S7 | Verification & evidence gate → Review | #25 |
| S8 | Human-in-the-loop chat steering (pause / cancel / re-plan / add) | #26 |
| S9 | Zero-LLM watchdog, check matrix & remediation ladder | #27 |
| S10 | Transactional outbox worker, orchestrator loop & CLI | #28 |
| S11 | Container infrastructure — compose, nginx gateway, Dockerfiles | #29 |

Each sub-issue carries its own delivered/evidence comment.

### Verification actually run

- `pytest` → **252 passed**, 14 test modules, 0 failures (~2s).
- `docker compose config -q` → exit 0 (valid).
- `docker compose up -d --build` → all five services **Up (healthy)**:
  `redis`, `watchdog`, `executor`, `planner`, `gateway`.
- Gateway verified live: `GET :8100/` → 200 (HTML),
  `GET :8100/healthz` → `{"status":"ok"}`.
- `clams doctor` (host): LLM engine, strixper API, SQLite storage, disk free,
  Redis queue depth and GitHub reachability all **OK**.

### Defects found by actually running it, and fixed

The compose stack did not come up as first delivered. Six real defects, all
fixed and committed (`232be3b`):

1. **Wrong image names** — `claudio:latest` does not exist. Planner now uses
   `claudio/web:latest`, executor `claudio/agent:latest`.
2. **Watchdog DB path** — volume was mounted at `/app/db` while the code
   defaults to `./data/clams.db`; the container crashed on a read-only
   filesystem. Mount corrected to `/app/data`.
3. **Planner port wrong throughout** — the web image listens on **8080**, not
   8000. Corrected in the healthcheck, `config/nginx.conf` upstream,
   `config.py` default and both `.env` files. (Host `:8000` is strixper.)
4. **Missing Linux capabilities for nginx** — `cap_drop: [ALL]` removed
   `CHOWN`/`SETGID`/`SETUID`, so nginx workers died in a loop
   (`setgid(101) failed`). Re-added those three to the planner only.
5. **Agent entrypoint needs writable paths** — the Claudio agent seeds
   `config/config.yaml` and `data/{sessions,workspaces}` at startup; added
   `executor-config` and `executor-srv-data` volumes.
6. **Missing network alias** — the planner's bundled `nginx.conf` proxies to
   upstream `claudio-agent:8100`; the executor service now carries that alias.

### Deviations from the spec, with reasons

1. **Native sub-issue linking uses GraphQL, not REST.**
   `POST /repos/{owner}/{repo}/issues/{n}/sub_issues` returns **404** on this
   repository (personal account, Issue Types not enabled). The GraphQL
   `addSubIssue` mutation works and is what `src/clams/gh.py` uses, with a
   `parent-N` label fallback if the mutation is ever refused.
2. **GitHub Projects board not used.** The token in use lacks `read:project`
   scope, so board columns could not be driven programmatically. State is
   carried by `status:*` labels instead, which is what intake and review read.
3. **Hardened against prompt pollution.** The planner prompt is built from the
   requirement title and body only — issue comment threads are never fed back
   into decomposition, per the spec's adversarial-input requirement.

### Known gaps (honest status)

- **`agent_heartbeats` still fails.** The heartbeat contract is written by the
  Clams orchestrator loop (`clams watch`) and `executor.write_heartbeat`, not
  by the stock Claudio images. Running `clams watch` on the host does produce
  `orchestrator.json` and `planner.json`, but the containerised executor never
  writes its own. Closing this needs the Clams executor process (not the stock
  agent image) in the executor container.
- **`wdctl check` runs a reduced check set** — inside the container it reports
  `no bus configured` / `no github client, skipped`, so it is not equivalent
  to the full `clams doctor` sweep.
- **`clams watch` emits no stdout**, so the loop looks hung when it is not.
  Needs logging.
- **No remote repository for `clams`** — git history is local only; pushing
  needs a new repo created under `massimorofi`.
