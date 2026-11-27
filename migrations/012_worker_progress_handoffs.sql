-- 012: worker progress stream + operator handoffs.
--
-- worker_progress: append-only narration from the executor while a task runs
-- (one row per (task_id, attempt_no, seq)). Written only through
-- repo_progress.append_progress, which fences on the task's *current*
-- attempt_no + lease_token and requires seq to strictly increase, so a stale
-- executor (retried task, reclaimed lease, replayed bus message) can never
-- rewrite or pollute the current attempt's history. UNIQUE(task_id,
-- attempt_no, seq) makes a redelivered message a no-op insert.
-- Deliberately no FK to tasks: progress must survive a replan that deletes
-- and recreates tasks, mirroring task_attempts (011).
CREATE TABLE IF NOT EXISTS worker_progress (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     TEXT NOT NULL,
  attempt_no  INTEGER NOT NULL,
  lease_token TEXT NOT NULL DEFAULT '',
  seq         INTEGER NOT NULL,
  kind        TEXT NOT NULL CHECK (kind IN
    ('status', 'log', 'tool', 'note', 'warning', 'error')),
  text        TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (task_id, attempt_no, seq)
);
CREATE INDEX IF NOT EXISTS idx_worker_progress_task
  ON worker_progress(task_id, attempt_no, seq);

-- handoffs: a worker asking a human to take over (blocked, needs a decision,
-- scope change requested). The handoff is a *request*, never an edit: a
-- handoff that asks for a scope change records requested_action and leaves
-- the task brief untouched (the planner remains the only domain writer).
-- Dispositions are append-safe: a row is never deleted and never moves
-- backwards (open -> acknowledged -> resolved|rejected); every transition is
-- also mirrored into the append-only events timeline.
CREATE TABLE IF NOT EXISTS handoffs (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id      TEXT NOT NULL,
  task_id          TEXT NOT NULL,
  attempt_no       INTEGER NOT NULL,
  kind             TEXT NOT NULL CHECK (kind IN
    ('blocked', 'needs_decision', 'scope_change', 'stalled', 'other')),
  reason           TEXT NOT NULL DEFAULT '',
  requested_action TEXT NOT NULL DEFAULT '',
  status           TEXT NOT NULL DEFAULT 'open' CHECK (status IN
    ('open', 'acknowledged', 'resolved', 'rejected')),
  disposition      TEXT NOT NULL DEFAULT '',
  disposed_by      TEXT NOT NULL DEFAULT '',
  created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_handoffs_status ON handoffs(status, id);
CREATE INDEX IF NOT EXISTS idx_handoffs_task ON handoffs(task_id, attempt_no, id);
CREATE INDEX IF NOT EXISTS idx_handoffs_wf ON handoffs(workflow_id, id);

-- Allow the dispose_handoff command kind: the API records operator
-- dispositions through the command inbox, never by mutating handoffs domain
-- state directly. Same table-rebuild pattern as 004/007/008; commands has no
-- inbound FKs.
CREATE TABLE commands_new (
  command_id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN
    ('pause','resume','cancel','replan','retry','answer','adhoc',
     'config_patch','settings_patch','prompt_patch','apply_plan',
     'dispose_handoff')),
  target TEXT, payload TEXT NOT NULL, issued_by TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','applied','rejected')),
  result TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, applied_at TEXT
);
INSERT INTO commands_new SELECT * FROM commands;
DROP TABLE commands;
ALTER TABLE commands_new RENAME TO commands;
