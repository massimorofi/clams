# Claudio Autonomous Multi-Agent System: System Requirements & Architecture Specification
**Version:** 1.1.0
**Target Platform:** Single Host (Linux / AMD Strix Halo / Docker Compose)
**Project Scope:** Standalone Orchestrator Project wrapping decoupled Claudio Agent instances, featuring Human-in-the-Loop Chat Steering via the Planner Agent Web UI.
## 1. Project Scope & Standalone Architecture
### 1.1 Standalone System Boundary & Web UI Integration
This project—**Claudio Orchestrator**—is built and maintained as a **separate repository and project** from the core `claudio` agent. 
* **Core `claudio` Agent:** Provides single-agent execution, tool running, local conversation context, and the **Claudio Web Front-End (Chat & SSE)**.
* **`claudio-orchestrator` (This Project):** Manages project work queues, GitHub synchronization, handoffs, supervisor watchdogs, local state DB, queue routing, and LLM inference engine containers (via `strixper`).
**User Interaction & Chat Steering:**
The human operator interacts directly with the system through the **Claudio Web UI attached to the Planner Agent** (routed via the reverse proxy gateway at `http://localhost:8100`). The Planner acts as the user-facing copilot and strategic supervisor:
* **Interactive Chat:** The user can ask questions about active tasks, review execution context, or check status.
* **Dynamic Steering:** The user can instruct the Planner via chat to modify the current plan, add new tasks/requirements, cancel ongoing steps, or trigger a full re-plan.
* **Background Autonomous Loop:** Concurrent with chat interaction, the Planner process periodically polls GitHub and processes Redis events/alerts.
```
 +---------------------------------------------------------------------------------------+
 |                        Claudio Web Front-End (Browser / UI)                           |
 +------------------------------------------+--------------------------------------------+
                                            |
                                            | Chat / SSE Stream (Port 8100)
                                            v
 +---------------------------------------------------------------------------------------+
 |                       claudio-orchestrator (Orchestrator & Agents)                   |
 |                                                                                       |
 |  +-----------------------------------+    +----------------------------------------+  |
 |  |    Planner Agent (Claudio Instance|    |     Executor Agent (Claudio Instance)  |  |
 |  |   + Core Agent Engine             |    |    + Core Agent Engine                 |  |
 |  |   + Web UI / SSE Front-End        |    |    + Shell / Worktree / Test Tools     |  |
 |  |   + GitHub / Planning Tools       |    |    + Task Brief Executor Profile       |  |
 |  +-----------------+-----------------+    +-------------------+--------------------+  |
 |                    |                                          |                       |
 |                    | Commands / Interventions                 | Results / Evidence    |
 |                    v                                          v                       |
 |  +-----------------+------------------------------------------+--------------------+  |
 |  |                                 Redis Event Bus                                 |  |
 |  |                     (Task Queues, Results, Injected Alerts)                     |  |
 |  +-----------------+------------------------------------------+--------------------+  |
 |                    ^                                          ^                       |
 |                    | Sync & Heartbeats                        | Claims & Outbox       |
 |  +-----------------+------------------------------------------+--------------------+  |
 |  |                        Local SQLite Database (WAL Mode)                         |  |
 |  |                   (work_items, task_claims, alerts, outbox)                     |  |
 |  +-----------------------------------+---------------------------------------------+  |
 |                                      ^                                                |
 |                                      | Health Checks & Interrupt Injection            |
 |  +-----------------------------------+---------------------------------------------+  |
 |  |                         No-LLM Watchdog Supervisor                              |  |
 |  +---------------------------------------------------------------------------------+  |
 +---------------------------------------------------------------------------------------+
                                        |
                                        v
                            +-----------------------+
                            |    strixper Engine    |
                            |   (Host Inference)    |
                            +-----------------------+
```
### 1.2 The Four Operational Planes
1. **Work Plane (GitHub Projects + Issues):** Source of truth for human-facing requirements, breakdown tasks, status transitions, and execution evidence.
2. **Agents Plane (2× Claudio Instances):** 
   * **Planner Instance:** Hosts the Web UI, listens to chat input, processes user interventions, and generates task briefs.
   * **Executor Instance:** Runs headlessly, picking up individual scoped tasks and running code/tests in isolated worktrees.
3. **Models Plane (Strixper):** Host-managed LLM inference runner controlling engine container lifecycles (`Halogen`, `GUFO`, `llama.cpp`).
4. **Supervision Plane (No-LLM Watchdog):** Deterministic health daemon managing checks, state transitions, alerts, deadman switches, and automated remediation.
---
## 2. Requirements & System Constraints
### 2.1 Functional Requirements
* **FR-1 Interactive Chat & Steering:** The system MUST expose the Claudio Web UI via the Planner container. Chat messages from the user (e.g., "stop task 2", "add error handling to the plan", "re-plan requirement #5") take immediate priority over autonomous background dispatching.
* **FR-2 Autonomous Requirement Intake:** Poll GitHub Projects for issues in `Status = Ready`. Lock item atomically and transition to `In Progress`.
* **FR-3 Task Decomposition:** Planner Agent deconstructs requirements into 3–7 atomic sub-issues using native GitHub Sub-Issues APIs (`POST /repos/{owner}/{repo}/issues/{id}/sub_issues`).
* **FR-4 Isolated Task Execution:** Executor Agent picks up individual sub-issues, creates/runs in isolated git worktrees, executes tests, and packages evidence logs.
* **FR-5 Verification & Evidence Gate:** Requirement transition to `Status = Review` requires real execution evidence (command stdout, exit code `0`, diff summary) posted as a GitHub comment and rendered in the Web UI.
* **FR-6 Fault Tolerance & Interrupts:** System failures (LLM engine down, queue stall, lease expiry) trigger immediate planner alerts injected as context blocks into the Planner's conversation stream.
### 2.2 System & Hardware Constraints (AMD Strix Halo / `gfx1151`)
* **HW-1 Compute Queue Ceiling:** The `gfx1151` command processor supports a maximum of **8 concurrent compute queues** across the entire device (4 HIP processes max).
* **HW-2 Engine Limit:** System MUST NOT run more than **2 concurrent LLM engines** (3 queues each = 6 queues used). Any third LLM role (e.g., Critic) MUST reuse an existing engine or run off-host.
* **HW-3 Cache Directory Isolation:** Each inference engine launched by `strixper` MUST have a dedicated, distinct host path for `--cache-disk`.
* **HW-4 Non-Gated Local Execution:** Local inference cost is \$0. Safety is strictly enforced via `max_attempts` (3), `max_turns` (40), `max_wall_seconds` (1800), and `max_replans` (3).
---
## 3. Human-in-the-Loop Chat Steering & Web UI
### 3.1 Web UI Gateway Routing
The Gateway (`nginx`) routes all user web traffic directly to the Planner container:
```nginx
# config/nginx.conf snippet
server {
    listen 80;
    
    # Web UI and SSE stream directed to Planner Agent
    location / {
        proxy_pass http://planner:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_buffering off; # Critical for real-time SSE streaming
        proxy_cache off;
    }
}
```
### 3.2 Chat Commands & Steering Semantics
When interacting with the Planner via the Web UI, the Planner interprets natural language requests into specific orchestrator actions:
| User Action in Chat | Planner Internal Action | State / Queue Effect |
|---|---|---|
| **"What is the status of Requirement #12?"** | Reads `work_items` & `task_claims` | Responds in chat with active steps, executor state, and test results. |
| **"Pause execution."** | Writes pause flag to `work_items` | Suspends dispatching new tasks to Redis; leaves running executor untouched. |
| **"Cancel task 3 and re-plan."** | Updates `work_items`, revokes task claim | Marks task issue `Blocked` on GitHub, revokes lease, and generates revised sub-issues. |
| **"Add requirement: Add JWT auth."** | Calls GitHub API to create issue | Creates new Requirement issue, adds to project board, and initiates intake. |
| **"Change acceptance criteria for #12 to X."** | Updates GitHub Issue body & DB | Updates local spec and forces Executor brief update on next turn. |
### 3.3 Alert Surface in Web UI
When the Watchdog detects a critical issue (e.g., LLM engine timeout, disk full), it injects a formatted alert into the Planner. The Planner immediately displays this in the Web UI:
```
+------------------------------------------------------------------------------------+
| [SYSTEM ALERT - CRITICAL] 10:14:22Z                                                |
| Source: LLM Engine B (Executor Model)                                             |
| Message: Inference endpoint http://127.0.0.1:8731 timed out after 3 probes.        |
| Orchestrator Action: Task #42 paused. Retrying engine restart via strixper...      |
| [Button: Resume]  [Button: Force Re-Plan]  [Button: Cancel Requirement]           |
+------------------------------------------------------------------------------------+
```
---
## 4. Agent Plane Specifications & Handoff Envelopes
### 4.1 Claudio Agent Profiles
| Profile Config | Planner Agent (with Web UI) | Executor Agent (Headless) |
|---|---|---|
| **Primary Interface** | **Claudio Web UI (HTTP/SSE on port 8000)** | Headless (Redis Stream listener) |
| **Primary Goal** | Interact with user, deconstruct requirements, evaluate evidence, re-plan | Execute single scoped task, run tests, return evidence |
| **Tool Allowlist** | GitHub API, Git (read-only), Local DB read, Orchestrator Control API | Bash/Shell, Git (read-write worktree), File IO, Test runners |
| **Workspace Mode** | Read-only clone or no repo volume | Read-write isolated worktree per task (`/work/task-{id}`) |
| **Context Limits** | Interactive session, mid-run compaction enabled | Max 40 turns, max 1800s wall-clock per task |
| **Container Caps** | `read_only: true`, `cap_drop: [ALL]` | `read_only: true`, `cap_drop: [ALL]`, `tmpfs: [/tmp]` |
### 4.2 Inter-Agent Message Envelopes (Redis Streams)
#### Planner -> Executor (Task Brief Payload)
```json
{
  "$schema_version": "1.0",
  "task_issue": 42,
  "requirement_issue": 17,
  "objective": "Implement POST /runs/{id}/messages endpoint with status 202 response",
  "acceptance_criterion": "pytest tests/test_inbox.py exits code 0",
  "scope": {
    "writable": ["src/api/runs.py", "tests/test_inbox.py"],
    "read_only": ["src/core/"],
    "forbidden": ["migrations/"]
  },
  "git": { "base_branch": "main", "base_sha": "4a9a5ec", "work_branch": "task/42" },
  "limits": { "max_turns": 40, "max_wall_seconds": 1800 }
}
```
#### Executor -> Planner (Execution Result Payload)
```json
{
  "$schema_version": "1.0",
  "task_issue": 42,
  "status": "completed",
  "summary": "Implemented endpoint and verified integration tests.",
  "evidence": {
    "commands": [
      { "cmd": "pytest -q tests/test_inbox.py", "exit_code": 0, "tail": "18 passed in 4.2s" }
    ],
    "log_file": "/work/task-42/test-output.log"
  },
  "artifact_delta": {
    "files_changed": ["src/api/runs.py", "tests/test_inbox.py"],
    "commit": "b21c9f0",
    "branch": "task/42"
  },
  "environment_state": {
    "git_head": "b21c9f0",
    "working_tree_clean": true
  }
}
```
---
## 5. Local State & Database Schema
The orchestrator maintains local transactional state in SQLite to handle claims, leases, user steering flags, outbox side-effects, and alerts independently of GitHub connectivity.
```sql
-- SQLite PRAGMA Configuration
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA busy_timeout = 5000;
PRAGMA wal_autocheckpoint = 1000;
PRAGMA foreign_keys = ON;
-- 1. GitHub Item Sync & User Steering Cache
CREATE TABLE IF NOT EXISTS work_items (
  requirement_number  INTEGER PRIMARY KEY,
  requirement_title   TEXT NOT NULL,
  gh_status           TEXT NOT NULL,
  gh_updated_at       TEXT NOT NULL,
  plan_json           TEXT,
  task_numbers        TEXT NOT NULL DEFAULT '[]', -- JSON Array
  planner_session_id  TEXT,
  user_paused         INTEGER NOT NULL DEFAULT 0, -- Set via Web UI Chat
  status              TEXT NOT NULL DEFAULT 'pending',
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL
);
-- 2. Task Claims & Lease Locking (Single-Writer: Executor / Watchdog)
CREATE TABLE IF NOT EXISTS task_claims (
  task_number         INTEGER PRIMARY KEY,
  requirement_number INTEGER NOT NULL,
  executor_id         TEXT NOT NULL,
  lease_expires_at    TEXT NOT NULL,
  attempts            INTEGER NOT NULL DEFAULT 0,
  max_attempts        INTEGER NOT NULL DEFAULT 3,
  claimed_at          TEXT NOT NULL,
  FOREIGN KEY(requirement_number) REFERENCES work_items(requirement_number)
);
-- 3. System Alerts & Preemption Logs (Single-Writer: Watchdog)
CREATE TABLE IF NOT EXISTS alerts (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  severity            TEXT NOT NULL CHECK(severity IN ('info', 'warning', 'critical')),
  source              TEXT NOT NULL,
  message             TEXT NOT NULL,
  acknowledged        INTEGER NOT NULL DEFAULT 0,
  created_at          TEXT NOT NULL
);
-- 4. Transactional Outbox for External Side-Effects
CREATE TABLE IF NOT EXISTS outbox (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  kind                TEXT NOT NULL CHECK(kind IN ('gh_comment', 'gh_status', 'notification')),
  payload             TEXT NOT NULL, -- JSON Payload
  status              TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'sent', 'failed')),
  attempts            INTEGER NOT NULL DEFAULT 0,
  created_at          TEXT NOT NULL,
  sent_at             TEXT
);
```
---
## 6. Supervision & Watchdog Plane
### 6.1 Check Matrix
The watchdog daemon runs every 15–20 seconds. It is a **zero-LLM, deterministic process**.
```
+------------------------+------------------------------------+-----------------------+
| Check Category         | Verification Mechanism             | Critical Trigger      |
+------------------------+------------------------------------+-----------------------+
| LLM Engine Health      | 1-token probe request to base_url  | 3 consecutive timeouts|
| Strixper API           | GET /api/v1/runner/active          | Engine switch error   |
| SQLite Storage         | BEGIN IMMEDIATE; ROLLBACK;         | DB locked > 5000ms    |
| Storage Volume Free    | statvfs(/app/data)                 | Available disk < 2GB  |
| Redis Task Queue       | XPENDING depth & max pending age   | Message pending > 15m |
| GitHub API Reachability| HEAD https://api.github.com        | Network fail > 3 sweeps|
| Agent Heartbeats       | Validate heartbeat timestamp file  | Heartbeat age > 60s   |
+------------------------+------------------------------------+-----------------------+
```
### 6.2 Remediation Ladder & Web UI Notification
1. **Pause & Notify UI:** Freeze task dispatching. Inject alert block directly into the Planner Web UI stream and mark active item `user_paused = 1` in SQLite.
2. **Diagnose:** Re-run failing health probes to verify non-transience.
3. **Automated Recovery:**
   * *Step 3a:* Issue engine restart command to `strixper` API.
   * *Step 3b:* If process hung, issue `docker restart <container_id>`.
   * *Step 3c:* Prune abandoned tmpfs/worktree paths.
4. **Verification:** Re-execute check set. If green, offer a 1-click "Resume" prompt in the Web UI or auto-resume if unmanaged.
5. **Human Escalation:** If recovery fails after 3 attempts, display action required banner in the Web UI and play system alert tone/notification.
---
## 7. Container Infrastructure Architecture
### 7.1 `docker-compose.yml`
```yaml
name: claudio-orchestrator
x-claudio-base: &claudio-base
  init: true
  restart: unless-stopped
  stop_grace_period: 120s
  logging:
    driver: json-file
    options:
      max-size: "20m"
      max-file: "5"
services:
  planner:
    <<: *claudio-base
    image: claudio:latest
    env_file: [.env.planner]      # Enable Web UI mode, port 8000
    ports:
      - "8000:8000"               # Internal UI port
    volumes:
      - planner-data:/app/data
      - ./repo-ro:/repo:ro
    read_only: true
    tmpfs: [/tmp]
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    extra_hosts: ["host.docker.internal:host-gateway"]
    depends_on:
      watchdog: { condition: service_healthy }
      redis: { condition: service_healthy }
  executor:
    <<: *claudio-base
    image: claudio:latest
    env_file: [.env.executor]     # Headless executor mode
    volumes:
      - executor-data:/app/data
      - claudio-work:/work
    read_only: true
    tmpfs: [/tmp]
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    extra_hosts: ["host.docker.internal:host-gateway"]
    depends_on:
      redis: { condition: service_healthy }
  watchdog:
    <<: *claudio-base
    build:
      context: ./services/watchdog
    env_file: [.env.watchdog]
    volumes:
      - orchestrator-db:/app/db
    read_only: true
    tmpfs: [/tmp]
    cap_drop: [ALL]
    healthcheck:
      test: ["CMD", "/app/wdctl", "selfcheck"]
      interval: 15s
      timeout: 3s
      retries: 3
  redis:
    image: redis:7-alpine
    command: >
      redis-server --appendonly yes --appendfsync everysec
                 --maxmemory 256mb --maxmemory-policy noeviction
    volumes:
      - redis-data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 3
  gateway:
    image: nginx:alpine
    ports: ["8100:80"]           # Primary entry point for Web UI Chat
    volumes:
      - ./config/nginx.conf:/etc/nginx/nginx.conf:ro
    depends_on:
      - planner
      - executor
volumes:
  planner-data: {}
  executor-data: {}
  orchestrator-db: {}
  claudio-work: {}
  redis-data: {}
```
---
## 8. Potential Architectural Pitfalls & Explicit Mitigations
1. **User Chat Interrupting Autonomous Flow:**
   * *Risk:* User sends a message via Web UI while the Planner is processing a long GitHub sweep or background event, causing race conditions or context collisions.
   * *Mitigation:* User chat inputs are treated as **high-priority interrupts**. The background event loop checks for incoming user messages at turn boundaries before processing the next Redis event.
2. **Context Pollution via Self-Reading Comments:**
   * *Risk:* The Planner agent reads its own historical comments from GitHub, continuously expanding its context window.
   * *Mitigation:* The Planner MUST only read the original Requirement description and Executor result JSON payloads. It is strictly prohibited from pulling comment threads authored by its own agent ID.
3. **SQLite Write Contention:**
   * *Risk:* Multiple processes writing to SQLite simultaneously cause `SQLITE_BUSY` errors.
   * *Mitigation:* Strict single-writer ownership per table: `work_items` (Planner/Sync/UI), `task_claims` (Executor), `alerts` (Watchdog), `outbox` (Outbox Worker). All connections set `PRAGMA busy_timeout = 5000`.
4. **Strixper Engine Cold Starts:**
   * *Risk:* Healthchecks fail while `strixper` downloads or initializes models on first start.
   * *Mitigation:* Set `start_period: 120s` on agent container healthchecks and pre-warm models via orchestrator startup scripts.
---
## 9. Phased Implementation Roadmap
```
+-----------------------------------------------------------------------------------+
| Phase 0: Environment & Verification Rails                                         |
| - Verify strixper multi-engine vs single active run behavior                      |
| - Expose Planner Web UI over Nginx proxy (Port 8100)                              |
| - Set non-optional limits (max_turns: 40, max_attempts: 3, max_wall_seconds: 1800)|
+-----------------------------------------------------------------------------------+
                                         |
                                         v
+-----------------------------------------------------------------------------------+
| Phase 1: Interactive Planner & Work Plane Integration                             |
| - Implement GitHub Projects polling intake service                                |
| - Wire Web UI Chat commands to local orchestrator state (Pause/Steer/Re-plan)    |
| - Implement Task Brief & Result JSON contracts                                   |
| - Connect Review Gate evidence verification pipeline                              |
+-----------------------------------------------------------------------------------+
                                         |
                                         v
+-----------------------------------------------------------------------------------+
| Phase 2: Supervision & Alert Preemption in UI                                     |
| - Build zero-LLM Watchdog binary/container with check matrix                      |
| - Stream Watchdog alerts into Planner Web UI chat window                          |
| - Implement automated remediation ladder (Step 1-5)                              |
+-----------------------------------------------------------------------------------+
                                         |
                               .---------+---------.
                               |                   |
                               v                   v
+---------------------------------------+ +-----------------------------------------+
| Phase 3: Scaling & Worktree Isolation | | Phase 4: Durable Engine (Optional)     |
| - Enable N Executor replicas in compose| | - Evaluate Temporal integration if     |
| - Auto-provision isolated git worktrees| |   custom state recovery complexity grows|
+---------------------------------------+ +-----------------------------------------+
```
