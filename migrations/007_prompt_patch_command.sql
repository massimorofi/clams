-- 007: allow the prompt_patch command kind (operator-editable prompt
-- templates from the UI Configuration tab). SQLite cannot ALTER a CHECK
-- constraint, so rebuild the table preserving rows and ids (same pattern
-- as 004). commands has no inbound FKs.
CREATE TABLE commands_new (
  command_id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN
    ('pause','resume','cancel','replan','retry','answer','adhoc',
     'config_patch','settings_patch','prompt_patch')),
  target TEXT, payload TEXT NOT NULL, issued_by TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','applied','rejected')),
  result TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, applied_at TEXT
);
INSERT INTO commands_new SELECT * FROM commands;
DROP TABLE commands;
ALTER TABLE commands_new RENAME TO commands;
