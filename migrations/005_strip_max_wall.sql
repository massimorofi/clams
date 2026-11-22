-- 005: drop the removed max_wall_s key from stored briefs.
-- Brief forbids unknown fields, so a stored brief_json that predates the
-- timeout removal made dispatch_ready raise ValidationError every cycle
-- (live incident 2026-11-22: #58 stuck in 'ready', LLM idle, poison
-- message in the DLQ).
UPDATE tasks
SET brief_json = json_remove(brief_json, '$.max_wall_s')
WHERE brief_json LIKE '%max_wall_s%';
