-- 013: durable GitHub webhook intake ledger.
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
