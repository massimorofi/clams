-- 014: allow the github_event command kind.
--
-- The webhook endpoint enqueues one normalized github_event command per
-- accepted delivery (the API's only write target remains the commands inbox).
-- Same table-rebuild pattern as 004/007/008/012; commands has no inbound FKs.
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
