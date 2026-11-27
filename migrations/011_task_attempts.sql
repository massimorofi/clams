-- #2: append-only attempt history for tasks.
-- One row per (task_id, attempt_no) result event, written by
-- repo_tasks.on_result inside the same fenced transaction that applies the
-- result. The retry brief's prior_failure block is built from the cumulative
-- summary over these rows (repo_attempts.cumulative_retry_summary), so an
-- attempt sees every earlier failure -- not just the latest one.
-- UNIQUE(task_id, attempt_no) makes recording idempotent: a replayed result
-- event appends nothing and overwrites nothing.
-- Deliberately no FK to tasks: history must survive task deletion (replan),
-- and the table is append-only by contract.
CREATE TABLE IF NOT EXISTS task_attempts (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id       TEXT NOT NULL,
  workflow_id   TEXT NOT NULL,
  attempt_no    INTEGER NOT NULL,
  lease_token   TEXT,
  status        TEXT NOT NULL,
  failure_class TEXT NOT NULL DEFAULT '',
  checks_json   TEXT NOT NULL DEFAULT '[]',
  result_json   TEXT NOT NULL DEFAULT '{}',
  error_excerpt TEXT NOT NULL DEFAULT '',
  branch_name   TEXT NOT NULL DEFAULT '',
  commit_sha    TEXT NOT NULL DEFAULT '',
  created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (task_id, attempt_no)
);
