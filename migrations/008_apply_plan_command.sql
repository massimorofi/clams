-- 008: allow the apply_plan command kind (operator-approved plan injection,
-- e.g. a plan authored by the planner agent and applied through the UI/API).
-- Same table-rebuild pattern as 004/007; commands has no inbound FKs.
CREATE TABLE commands_new (
  command_id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN
    ('pause','resume','cancel','replan','retry','answer','adhoc',
     'config_patch','settings_patch','prompt_patch','apply_plan')),
  target TEXT, payload TEXT NOT NULL, issued_by TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','applied','rejected')),
  result TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, applied_at TEXT
);
INSERT INTO commands_new SELECT * FROM commands;
DROP TABLE commands;
ALTER TABLE commands_new RENAME TO commands;
