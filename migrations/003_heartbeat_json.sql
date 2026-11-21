-- 003: store the last heartbeat's progress payload so the planner's stall
-- probe can see whether the agent is actually advancing (turns/messages
-- growing) rather than only that the runner thread is alive.
ALTER TABLE tasks ADD COLUMN heartbeat_json TEXT;
