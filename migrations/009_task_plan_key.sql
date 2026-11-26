-- 009: stable plan identity for replan dedupe.
--
-- Root cause (fixed in a later step): every replan hard-deletes all task rows
-- (repo_tasks.delete_workflow_tasks) and re-inserts them with brand-new
-- task_ids, so projection.project_tasks_created enqueues gh_subissue with a
-- never-seen idempotency key (gh:gh_subissue:<task_id>:created) and GitHub
-- mints a duplicate sub-issue while the old ones stay open. Matching a replan
-- against what already exists needs an identity that survives the delete, so
-- we store a normalized form of the task title in plan_key.
--
-- The column is added NULL-able and the index is NON-unique on purpose:
-- pre-existing duplicate titles must not make the migration fail. Dedupe
-- lookups pick a winner explicitly (ORDER BY sequence_index).
--
-- Backfill uses lower(trim(title)) in SQL (SQLite has no whitespace-collapse).
-- create_task() writes the exact repo_tasks.normalize_plan_key() value from
-- here on, so newly planned rows are canonical; the backfill only has to be
-- good enough to match titles that were never re-planned.
ALTER TABLE tasks ADD COLUMN plan_key TEXT;
UPDATE tasks SET plan_key = lower(trim(title));
CREATE INDEX idx_tasks_workflow_plan_key ON tasks(workflow_id, plan_key);
