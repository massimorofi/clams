# Clams Frontend Redesign — Requirements Document

**Status:** Draft for review
**Date:** 2026-11-13
**Source mockup:** `todo/clams_console_mockup.html` ("Clams — Control room concept")
**Target repo:** `massimorofi/clams` · **Project board:** Clams (project #3)

---

## 1. Purpose & summary

The mockup `clams_console_mockup.html` is a static, fully-simulated "control room"
concept for the Clams web frontend. This document turns that mockup into a concrete
set of requirements to (a) adopt its look and feel and (b) implement every function,
button, control and view it depicts — but wired to the **real** Clams backend instead
of the mockup's fake data.

The mockup is intentionally illustrative: every panel says "simulated / prototype /
no backend connected". Our job is to keep the visual language and interaction model
and replace the simulation with live data from the existing FastAPI + SSE + WebSocket
backend (`src/clams/api.py`), and to close the backend gaps the mockup implies but
the current API does not yet provide.

### What changes vs. today

| Aspect | Current frontend | Target (mockup) |
|---|---|---|
| Theme | Dark (`bg-ink-950`) | **Light** (paper `#f6f8f5`, ink `#172523`) |
| Navigation | Top tab bar (4 tabs) | **Left sidebar** (7 items, grouped) |
| Pages | 4 tabs | **7 pages**: Overview, Projects, Activity & alerts, CLI runner, Configuration, Planner chat, Agents & health |
| Global controls | Pause / Resume / Emergency stop in header | Same 3 controls, restyled, plus **live SSE/WS transport badges** |
| Data | Live (single repo) | Mockup is fake; target = **live** + new endpoints |
| Routing | Tab state only | **Hash routing** (`#overview`, `#projects`, …) |

---

## 2. Scope

### In scope
- Full visual restyle to the mockup's light "control room" design system.
- Sidebar navigation with 7 pages and hash-based routing.
- All interactive controls shown in the mockup, wired to real backend endpoints.
- Backend additions required to make the mockup's features real (see §6).

### Out of scope (for this issue; note as follow-ups)
- New visual designs not present in the mockup.
- Mobile bottom-nav redesign beyond the responsive collapse described in §8.
- Authentication/authorization (backend currently has CORS `*` and no auth; tracked separately).

---

## 3. Design system (look & feel)

Reproduce the mockup's design tokens exactly. These are the CSS custom properties
defined in the mockup `:root`:

| Token | Value | Use |
|---|---|---|
| `--ink` | `#172523` | Primary text |
| `--muted` | `#71807b` | Secondary/label text |
| `--line` | `#e5eae6` | Borders/dividers |
| `--paper` | `#f6f8f5` | Page background |
| `--white` | `#ffffff` | Card background |
| `--green` | `#1f6d55` | Primary action / brand accent |
| `--mint` | `#e6f2e9` | Pills, selected states |
| `--orange` | `#a75d21` | Warning text |
| `--orangebg` | `#fff2e5` | Warning background |
| `--red` | `#a1433b` | Danger / error text |
| `--blue` | `#446a91` | Info / neutral status |
| `--shadow` | `0 12px 32px #21352b10` | Card shadow |

**Typography:** `14px/1.5 Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif`.
Headings use tight letter-spacing (`-0.5px` to `-0.8px`); small labels use uppercase
with wide letter-spacing (`1.05px`–`1.4px`).

**Core component styles to build (shared):**
- `.card` — white surface, 1px `--line` border, 11px radius, soft shadow.
- `.panelhead` — card header row (title + sub + optional action).
- `.btn` / `.btn.primary` / `.btn.danger` — buttons, min-height 38px, 8px radius.
- `.input` — inputs/selects/textareas, 38px min-height, 7px radius.
- `.status` (+ `.warn` / `.err` / `.blue`) — pill badges with a leading dot.
- `.pill` — mint rounded label.
- `.metric` — KPI card (label + big number + trend line).
- `.table` — data table with uppercase header row, row dividers.
- `.progress` — thin 5px progress bar.
- `.notice` — amber advisory banner.
- `.code` — dark terminal/pre block (monospace) for command output & JSON.
- `.diff` — light diff block.
- `.message` — chat bubble (`.user` right-aligned mint, `.system` amber).
- `.toast` — transient bottom notification.

**Accessibility (from mockup, keep):** visible `:focus-visible` outline (2px `--green`,
offset 2px) on all interactive elements; `aria-live="polite"` on toast, config message
and chat log; `role="status"` on toast; `aria-label`/`aria-expanded` on the mobile menu.

---

## 4. Navigation & app shell

### 4.1 Sidebar (fixed left, 238px)
- **Brand** row: logo tile (`◈`, mint bg) + "clams" + "control" label.
- **Workspace** card: avatar (`CL`), "Clams workspace", subtitle. In the real app
  this should show the actual configured repository (`GET /api/v1/project` →
  `full_name`), not a static string.
- **Nav groups:**
  - *Workspace*: Overview (`#overview`), Projects (`#projects`), Activity & alerts (`#activity`)
  - *Operations*: CLI runner (`#cli`), Configuration (`#config`), Planner chat (`#chat`), Agents & health (`#agents`)
- Each nav item: icon + label; `.active` state = mint background + bold.
- **Sidebar footer**: live status badge (`#side-state`, e.g. "◈ System executing") and
  transport line (`#side-transport`, e.g. "SSE demo · WS demo"). In the real app these
  reflect actual SSE/WS connection state, not "demo".

### 4.2 Top bar (sticky)
- Breadcrumb: `Clams / <Page name>` (`#crumb`).
- Mobile hamburger (`#menu`) that toggles the sidebar on small screens.
- Right cluster:
  - Global state badge (`#global-state`): Idle / Executing / Paused / Error.
  - SSE status badge (`#sse-state`): connected / offline.
  - WS status badge (`#ws-state`): connected / offline.
  - **Pause** button (`#pause`) → `POST /api/v1/planner/action {action:"pause"}`.
  - **Resume** button (`#resume`) → `POST /api/v1/planner/action {action:"resume"}`.
  - **Emergency stop** button (`#emergency`, `.danger`) → confirm dialog, then pause
    with `detail:"emergency stop"` (today's behavior). **Requirement:** backend should
    expose a real stop semantics (see §6.4).

### 4.3 Routing
- Hash routing: `#overview`, `#projects`, `#activity`, `#cli`, `#config`, `#chat`, `#agents`.
- On load and on `hashchange`, show the matching page; unknown hash → Overview.
- Clicking a nav item or any `[data-go]` shortcut updates the hash and the active state.
- Deep links must work (e.g. open `#config` directly).

---

## 5. Per-page functional requirements

> Each requirement lists the mockup element and the **real** data source. Where the
> current backend has no endpoint, the gap is called out and collected in §6.

### 5.1 Overview (`#overview`)
- **Header**: eyebrow "Workspace overview", H1 "Control room", subtitle.
- **Actions**: "Refresh" (re-fetch all data) and "Open CLI runner →" (nav to `#cli`).
- **KPI grid (4 metrics):**
  1. Active projects — count of projects (gap: §6.1).
  2. Running tasks — count of in-progress work items (`GET /api/v1/tasks`).
  3. Awaiting review — count of items in review state (`GET /api/v1/tasks`).
  4. Active agents — `GET /api/v1/status.active_agents` (live via SSE `telemetry:update`).
- **Projects table** (search + status filter): columns Project / Status / Progress /
  Updated. Search filters by name+repo+url; filter by status. (gap: §6.1, §6.2.)
- **Pipeline snapshot**: counts by stage (Ready / In progress / Review / Done) —
  derive from `GET /api/v1/tasks` grouped by `status`.
- **Recent activity**: event list (icon, actor, action, relative time) — from SSE
  `logs:stream` / alert feed.
- **System health**: engine online, planner socket (WS), telemetry stream (SSE) —
  from live connection state.

### 5.2 Projects (`#projects`)
- **"+ Add project"** button reveals an inline form:
  - Project name (required, max 80).
  - GitHub repository URL (required, `type=url`).
  - Validation: must parse as a URL; host must be `github.com`; path must have
    `owner/repo`; reject duplicate repo URLs. Normalize trailing `.git`.
  - On submit → create project (gap: §6.1 `POST /api/v1/projects`).
  - Cancel hides + resets the form.
- **Project table**: same columns as Overview (Project / Status / Progress / Updated).
- **Note:** the current backend models a **single** configured repository
  (`settings.github`), not a multi-project registry. This page requires new backend
  support (§6.1). Until then, the page may render the single configured repo as a
  one-row list and disable "Add project" with an explanatory notice.

### 5.3 Activity & alerts (`#activity`)
- **Severity filter** (`#severity`): All / Info / Warning / Critical →
  `GET /api/v1/alerts?severity=…`.
- **Alert count** (`#alert-count`): number of unacknowledged alerts
  (`GET /api/v1/status.unacked_alerts`).
- **Alert table**: Severity / Time / Source / Message / action.
  - Per-row **Acknowledge** link → gap: §6.3 (`POST /api/v1/alerts/{id}/ack`).
  - "ACKED" badge when acknowledged.
- **"Acknowledge filtered"** button (`#ack-all`): bulk-ack all alerts matching the
  current severity filter → gap: §6.3 (bulk ack).
- **Recent activity stream**: full event list (same renderer as Overview's recent
  activity).

### 5.4 CLI runner (`#cli`)
- **Command selector** (`#command`): dropdown of allowed commands. The backend
  allowlist is authoritative: `status, intake, plan, dispatch, execute, worker,
  review, doctor, healthcheck, init-db` (`ALLOWED_COMMANDS` in `api.py`).
- **Dynamic parameter form** (`#command-params`): generated per command. Mockup
  mapping: `plan`/`review` take `requirement`; `healthcheck` takes `service` +
  `max_age`; others take none. **Requirement:** parameter schema should come from the
  backend (or a shared TS constant mirroring the CLI), not be hardcoded per-command in
  the UI without validation.
- **Run** button (`#run-command`) → `POST /api/v1/cli/exec {command, args, timeout}`.
  Render `stdout`/`stderr`/`exit_code` in the terminal block.
- **Terminal output** (`#terminal`): monospace block; shows command line + output.
- **Terminal meta** (`#terminal-meta`): last run time / status.
- **Search output** (`#terminal-search`): count of matching lines.
- **Copy** (`#copy-terminal`): copy terminal text to clipboard (`navigator.clipboard`).
- **Export** (`#export-terminal`): download output as a `.log` file.
- **Safety (already in backend, must be preserved):** backend enforces the command
  allowlist, parameter validation, timeout and output truncation. The UI must surface
  errors (400/500) rather than silently succeed.

### 5.5 Configuration (`#config`)
- **Dirty indicator** (`#dirty-pill`): "N pending change(s)" / "No pending edits".
- **Reset** (`#reset-config`): revert form to loaded baseline.
- **Hot-apply changes** (`#hot-apply`): disabled until there is a change →
  `PUT /api/v1/config {values}`. Backend validates + diffs and returns `changed`.
- **Form fields**: the mockup shows `max_agents`, `poll_interval`, `auto_review`,
  `planner_model`. **The real editable schema is different** — it is returned by
  `GET /api/v1.config.editable` (a list of `{path,label,type,min,max,step}`).
  **Requirement:** render the form **from the backend `editable` schema**, not from
  the mockup's hardcoded four fields. Supported types: `number`, `boolean`, `text`.
- **Review diff** (`#review-diff`): toggles a diff panel showing `- old` / `+ new`
  per changed key.
- **Raw JSON preview** (`#json-preview`): live JSON of current form values.
- **Config message** (`#config-msg`, `aria-live`): status after apply/reset.
- **Requirement:** on apply, show the backend-returned `changed` diff and any
  validation error (422) clearly; do not claim success on failure.

### 5.6 Planner chat (`#chat`)
- **Transport badge** (`#chat-transport`): WS connected vs "REST fallback".
- **Chat log** (`#chatlog`, `aria-live`): message bubbles (You / Planner / System)
  with role + timestamp.
- **Requirement number** (optional, `#requirement`) attached to the message.
- **Draft textarea** (`#chat-draft`) + **Send** (`#send-chat`).
  - **Ctrl/Cmd + Enter** sends.
  - Send via WebSocket (`planner:message` → `planner:reply`); if WS is down, fall
    back to `POST /api/v1/planner/message` (already implemented in `PlannerSocket`).
- **Quick interventions** (right rail): Retry failed task, Pause planner, Resume
  planner, Approve plan, Reject plan → `POST /api/v1/planner/action` with the
  matching `action` (`retry`/`pause`/`resume`/`approve`/`reject`).
  - **Requirement:** consequential actions (approve/reject/pause) require a
    confirmation dialog and should carry `requirement_number`/`task_number` context.

### 5.7 Agents & health (`#agents`)
- **Simulate SSE reconnect** / **Simulate WS reconnect** buttons (`#toggle-sse`,
  `#toggle-ws`): in the real app these become **real** connection controls —
  reconnect the SSE stream / WS and reflect actual state (not a fake toggle).
- **KPI grid (4):** Overall state, Alive heartbeats (`n / total`), Unacknowledged
  alerts, Event transport (Connected/Offline).
- **Agent heartbeat matrix**: Agent / Status / Last heartbeat / Assignment / Queue.
  - Source: `GET /api/v1/status.heartbeats` (alive + `age_seconds` per agent:
    orchestrator, planner, executor, watchdog).
  - **Requirement:** show stale/offline distinctly from alive; update live via SSE.
- **Transport diagnostics**: Telemetry SSE and Planner WebSocket rows with live
  status (connected / reconnecting / offline).

---

## 6. Backend gaps to close (required for full fidelity)

The mockup assumes capabilities the current single-repo API does not expose. Each gap
below must be implemented (or explicitly degraded with a notice) for the feature to be
real rather than simulated.

### 6.1 Multi-project registry (HIGH)
- **Mockup:** Projects page lists many projects and lets you add one by name + GitHub URL.
- **Today:** backend models exactly one configured repo (`settings.github.owner/repo`).
- **Needed:**
  - `GET /api/v1/projects` → list of projects with `name`, `repo`, `url`, `status`,
    `progress`, `updated_at`.
  - `POST /api/v1/projects` → create a project (validate name + github.com URL,
    reject duplicates).
  - Persistence (a `projects` table in the SQLite store).
- **Interim fallback:** render the single configured repo as a one-row list; disable
  "Add project" with a notice until the registry exists.

### 6.2 Project progress percentage (MEDIUM)
- **Mockup:** each project shows a `NN%` progress bar.
- **Today:** no progress field in status/tasks payloads.
- **Needed:** a computed progress per project (e.g. completed items / total items)
  returned by the projects/status endpoint.

### 6.3 Alert acknowledgement (HIGH)
- **Mockup:** per-row "Acknowledge" and a bulk "Acknowledge filtered" action.
- **Today:** `GET /api/v1/alerts` exists; `compute_state` reads
  `store.alerts.list_unacked`, but there is **no HTTP endpoint to acknowledge**.
- **Needed:**
  - `POST /api/v1/alerts/{id}/ack` → set `acknowledged`.
  - `POST /api/v1/alerts/ack` (bulk) with optional `severity` filter.
  - Reflect the new unacked count in the header immediately (SSE push preferred).

### 6.4 Real emergency stop (MEDIUM)
- **Mockup:** "Emergency stop" is a distinct, confirmable, dangerous action.
- **Today:** mapped to `pause` with `detail:"emergency stop"`.
- **Needed:** a real stop semantic (e.g. `action:"stop"` that halts dispatch and
  drains/cancels in-flight work) distinct from pause, or an explicit decision that
  pause-with-detail is acceptable. Confirm dialog is mandatory either way.

### 6.5 Config schema mismatch (HIGH — already solvable)
- **Mockup:** four hardcoded fields (`max_agents`, `poll_interval`, `auto_review`,
  `planner_model`).
- **Today:** `GET /api/v1/config.editable` returns the real, richer schema
  (poll_interval, limits.*, watchdog.*, project.*, executor.*).
- **Resolution:** build the config form **from `editable`**, not from the mockup's
  four fields. No new backend work needed; this is a frontend correctness fix.

### 6.6 Pipeline stage counts (LOW)
- **Mockup:** "Ready / In progress / Review / Done" counts.
- **Today:** derivable from `GET /api/v1.tasks[].status`. No new endpoint required —
  compute client-side. Confirm the canonical status vocabulary with the store.

---

## 7. Real-time behavior

- **SSE (`GET /api/v1/events`)** drives: global state badge, active agent count,
  heartbeat ages, alert counts, recent activity. On SSE drop, show "offline" and
  reconnect with backoff; surface reconnect state in the transport badges.
- **WebSocket (`/ws`)** drives planner chat. On WS close, fall back to REST
  (`POST /api/v1/planner/message`) so chat never dead-ends (already implemented).
- **Connection state must be real**, not the mockup's "simulated" strings. The
  sidebar footer, topbar badges, chat transport badge and Agents-page diagnostics all
  read from the same connection manager.

---

## 8. Responsive & accessibility

- **Breakpoints (from the older spec, keep sensible):**
  - Desktop (>1024px): sidebar + multi-column dashboard.
  - Tablet (768–1023px): sidebar collapses to the hamburger; grids reflow.
  - Mobile (<768px): single column; hamburger opens the sidebar as a drawer
    (`#sidebar.open`); tables scroll horizontally.
- **Touch targets:** ≥44×44px for buttons/toggles.
- **A11y:** focus-visible outlines on all controls; `aria-live` regions for toast,
  config message, chat log; `aria-expanded` on the menu; labeled inputs/selects;
  sufficient color contrast (the light palette is high-contrast by design).
- **Theme change:** the current app is dark; the target is light. This is a global
  restyle — verify no dark-only Tailwind classes remain and that the terminal/code
  blocks (which stay dark) still read well against the light page.

---

## 9. Acceptance criteria

A change satisfies this issue when **all** of the following hold against the live
backend (not the mockup's fake data):

1. App loads in the light theme with the left sidebar; all 7 pages reachable via
   sidebar and via direct hash deep-links; active state correct.
2. Topbar shows real system state, SSE state and WS state; Pause/Resume/Emergency
   stop call the real endpoints and the UI updates from the response/SSE.
3. Overview KPIs, projects table (search + filter), pipeline counts, recent
   activity and system health all show live data.
4. Projects: Add-project form validates (github.com URL, owner/repo, duplicate
   check) and persists (or is disabled with a clear notice if §6.1 is not yet done).
5. Activity: severity filter works against the real alerts endpoint; per-row and
   bulk acknowledge work (or are disabled with a notice if §6.3 is not yet done).
6. CLI runner: command list matches the backend allowlist; running a command shows
   real stdout/stderr/exit code; copy and export work; errors surface.
7. Configuration: form is generated from the backend `editable` schema; dirty
   tracking, diff view, reset, and hot-apply (with backend validation + returned
   diff) all work; 422 errors are shown, not swallowed.
8. Planner chat: WS send/reply works with REST fallback; Ctrl/Cmd+Enter sends;
   quick interventions call the real action endpoints with confirmation.
9. Agents & health: heartbeat matrix shows real alive/age per agent and updates
   live; transport diagnostics reflect real SSE/WS state; reconnect controls perform
   a real reconnect.
10. Responsive at desktop/tablet/mobile; keyboard navigable; no console errors.

---

## 10. Suggested implementation order

1. **Design tokens + shell** (theme, sidebar, topbar, routing) — visual parity.
2. **Overview** wired to existing `/status`, `/tasks`, `/project`.
3. **CLI runner** wired to `/cli/exec` (already fully supported by backend).
4. **Configuration** generated from `/config.editable` (fixes §6.5).
5. **Planner chat** (WS + REST fallback already in `api.ts`).
6. **Agents & health** from `/status.heartbeats` + SSE.
7. **Backend additions** for §6.1 (projects), §6.3 (alert ack), §6.4 (stop) —
   then light up Projects and Activity fully.

---

## 11. Open questions / assumptions

- **Assumption:** the mockup's light theme is the intended final theme (the current
  app is dark). If a dark mode is still wanted, it becomes a theme-toggle follow-up.
- **Assumption:** "Add project" implies a real multi-project registry (§6.1). If
  Clams is intentionally single-repo, the Projects page should be reduced to the
  single-repo view and the add flow dropped.
- **Assumption:** the config form must follow the backend `editable` schema; the
  mockup's four fields are illustrative only.
- **To confirm:** canonical work-item status vocabulary for pipeline counts and
  project progress.
