-- Per-attempt wall-clock anchor: the running-lease cap must measure from the
-- moment the attempt starts running, not task creation (retries hours later
-- would otherwise get an already-expired lease).
ALTER TABLE tasks ADD COLUMN started_at TEXT;
