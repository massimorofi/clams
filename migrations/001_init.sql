-- Clams v2 schema — single-writer SQLite (planner-owned).
-- PRAGMAs (journal_mode=WAL, synchronous=FULL, busy_timeout=5000,
-- foreign_keys=ON) are applied per-connection in clams.db.conn, not here.

CREATE TABLE projects (
  project_id INTEGER PRIMARY KEY,
  repo TEXT NOT NULL UNIQUE,            -- owner/name
  gh_project_node_id TEXT,              -- Projects V2
  status_field_id TEXT, ready_option_id TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE workflows (
  workflow_id TEXT PRIMARY KEY,                     -- uuid
  project_id INTEGER NOT NULL REFERENCES projects(project_id),
  source TEXT NOT NULL CHECK (source IN ('github','adhoc')),
  gh_issue_number INTEGER,
  goal TEXT NOT NULL,                               -- issue title + body only
  status TEXT NOT NULL CHECK (status IN
    ('intake','planning','running','review','done','failed','blocked','paused','cancelled')),
  replans INTEGER NOT NULL DEFAULT 0,
  plan_json TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (project_id, gh_issue_number)
);

CREATE TABLE tasks (
  task_id TEXT PRIMARY KEY,
  workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id),
  sequence_index INTEGER NOT NULL,
  depends_on TEXT NOT NULL DEFAULT '[]',
  title TEXT NOT NULL, objective TEXT NOT NULL, acceptance TEXT NOT NULL,
  brief_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN
    ('pending','ready','dispatched','running','waiting_input','success','failed','blocked','cancelled')),
  attempt INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
  lease_token TEXT, lease_expires_at TEXT, executor_id TEXT,
  session_ref TEXT, commit_sha TEXT, branch TEXT,
  result_json TEXT, error_log TEXT,
  gh_issue_number INTEGER,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (workflow_id, sequence_index), UNIQUE (workflow_id, title)
);
CREATE INDEX idx_tasks_status ON tasks(status, lease_expires_at);
CREATE INDEX idx_tasks_wf ON tasks(workflow_id, sequence_index);

CREATE TABLE questions (
  question_id INTEGER PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id), attempt INTEGER NOT NULL,
  question TEXT NOT NULL, options TEXT,
  status TEXT NOT NULL CHECK (status IN ('open','answered','expired','cancelled')),
  answer TEXT, answered_by TEXT, asked_at TEXT DEFAULT CURRENT_TIMESTAMP,
  answered_at TEXT, expires_at TEXT, idempotency_key TEXT UNIQUE
);

CREATE TABLE commands (                 -- the API's ONLY write target
  command_id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN
    ('pause','resume','cancel','replan','retry','answer','adhoc','config_patch')),
  target TEXT, payload TEXT NOT NULL, issued_by TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','applied','rejected')),
  result TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, applied_at TEXT
);

CREATE TABLE outbox (
  outbox_id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN
    ('bus_publish','gh_comment','gh_label','gh_board_move','gh_subissue')),
  payload TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','inflight','sent','dead')),
  attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT DEFAULT CURRENT_TIMESTAMP,
  last_error TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_outbox_due ON outbox(status, next_attempt_at);

CREATE TABLE processed_events (
  msg_id TEXT PRIMARY KEY, processed_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE events (                   -- UI timeline + SSE source (append-only)
  event_id INTEGER PRIMARY KEY, scope TEXT, ref TEXT, level TEXT, message TEXT, data TEXT,
  ts TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE executors (
  executor_id TEXT PRIMARY KEY, version TEXT, capacity INTEGER, last_seen TEXT
);

CREATE TABLE alerts (
  alert_id INTEGER PRIMARY KEY, probe TEXT, severity TEXT, message TEXT,
  status TEXT DEFAULT 'open', created_at TEXT DEFAULT CURRENT_TIMESTAMP, cleared_at TEXT
);

CREATE TABLE runtime_state (
  key TEXT PRIMARY KEY, value TEXT NOT NULL
);
-- keys: paused ('0'/'1'), paused_by ('operator'|'watchdog'), schema_version, heartbeat
