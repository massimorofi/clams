-- 006: watchdog stall-evidence bundles. One row per (task, attempt) stall
-- episode, upserted while the episode is open; referenced from stall alerts
-- so a human can decide cancel/retry from the recorded evidence.
CREATE TABLE IF NOT EXISTS stall_evidence (
  task_id     TEXT NOT NULL,
  attempt     INTEGER NOT NULL,
  updated_at  TEXT NOT NULL,
  bundle_json TEXT NOT NULL,
  PRIMARY KEY (task_id, attempt)
);
