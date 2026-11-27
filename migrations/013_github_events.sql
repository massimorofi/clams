-- 013: durable GitHub webhook intake ledger + the github_event command kind.
--
-- The API writes exactly one row per accepted delivery (delivery_id is the
-- idempotency key, so a GitHub redelivery is a no-op INSERT OR IGNORE).
-- Only NORMALIZED, BOUNDED, REDACTED fields live here - never the raw
-- payload: free-text bodies are dropped during normalization.
-- The planner reads this table; the API never mutates domain state.
CREATE TABLE IF NOT EXISTS github_events (
  delivery_id     TEXT PRIMARY KEY,               -- X-GitHub-Delivery
  repo            TEXT NOT NULL,                  -- owner/name
  event_action    TEXT NOT NULL,                  -- e.g. pull_request.synchronize
  pr_number       INTEGER,
  head_ref        TEXT,
  head_sha        TEXT,
  check_id        TEXT,
  summary         TEXT NOT NULL DEFAULT '',       -- bounded + redacted
  received_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  processed_state TEXT NOT NULL DEFAULT 'new' CHECK (processed_state IN
    ('new','processing','done','failed','skipped'))
);

CREATE INDEX IF NOT EXISTS idx_github_events_state
    ON github_events(processed_state, received_at);
CREATE INDEX IF NOT EXISTS idx_github_events_repo
    ON github_events(repo, received_at);
CREATE INDEX IF NOT EXISTS idx_github_events_pr
    ON github_events(repo, pr_number, received_at);

-- Allow the github_event command kind: an accepted delivery enqueues exactly
-- one normalized command through the existing inbox, which remains the API's
-- only write target for work. Same table-rebuild pattern as 004/007/008/012;
-- commands has no inbound FKs.
CREATE TABLE commands_new (
  command_id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN
    ('pause','resume','cancel','replan','retry','answer','adhoc',
     'config_patch','settings_patch','prompt_patch','apply_plan',
     'dispose_handoff','github_event')),
  target TEXT, payload TEXT NOT NULL, issued_by TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','applied','rejected')),
  result TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, applied_at TEXT
);
INSERT INTO commands_new SELECT * FROM commands;
DROP TABLE commands;
ALTER TABLE commands_new RENAME TO commands;
