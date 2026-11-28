-- 014: explicit GitHub PR/branch/SHA <-> task mapping + the github feedback route.
--
-- Everything here is a NEW table: github_events stays exactly the bounded
-- intake ledger migration 013 defined (the API owns those columns), and the
-- planner's verdicts live beside it instead of inside it.
--
-- WHY pr_links exists: the planner may only connect an inbound github_events
-- delivery to a task/workflow through an *explicitly stored* mapping of
-- (repo, branch/PR, head SHA). Title similarity, goal text or any other fuzzy
-- match is deliberately impossible: there is no column here to match against.
-- A delivery whose repo/branch/PR/SHA triple is not stored stays in
-- github_events as audit-only and dispatches nothing.
--
-- One row == one assertion "this task's work lives at this commit of this PR".
-- A push supersedes the previous row instead of rewriting it, so history shows
-- which SHA was current when a given delivery was processed (that is what makes
-- a stale-SHA event detectable at all).
CREATE TABLE IF NOT EXISTS pr_links (
  link_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL,
  task_id     TEXT NOT NULL,
  repo        TEXT NOT NULL,                    -- owner/name
  pr_number   INTEGER,
  head_ref    TEXT,                             -- branch name
  head_sha    TEXT NOT NULL,                    -- the SHA this link asserts
  state       TEXT NOT NULL DEFAULT 'current' CHECK (state IN
                ('current','superseded','closed')),
  created_by  TEXT NOT NULL DEFAULT 'planner',
  created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at  TEXT DEFAULT CURRENT_TIMESTAMP,
  -- one link per (repo, task, sha); re-linking the same SHA is a no-op
  UNIQUE (repo, task_id, head_sha)
);
CREATE INDEX IF NOT EXISTS idx_pr_links_pr
    ON pr_links(repo, pr_number, state, link_id);
CREATE INDEX IF NOT EXISTS idx_pr_links_ref
    ON pr_links(repo, head_ref, state, link_id);
CREATE INDEX IF NOT EXISTS idx_pr_links_sha
    ON pr_links(repo, head_sha, state, link_id);
CREATE INDEX IF NOT EXISTS idx_pr_links_task
    ON pr_links(task_id, state, link_id);

-- What the planner did with a delivery, keyed by the same delivery_id. One row
-- per consumed delivery: correlation verdict, failure classification, the
-- disposition it routed to, and the task/workflow it acted on (empty when the
-- delivery never correlated). Audit-only by design: it never feeds back into
-- task state.
CREATE TABLE IF NOT EXISTS gh_feedback (
  delivery_id    TEXT PRIMARY KEY,
  correlation    TEXT NOT NULL DEFAULT '',
  classification TEXT NOT NULL DEFAULT '',
  disposition    TEXT NOT NULL DEFAULT '',
  outcome        TEXT NOT NULL DEFAULT '',
  link_id        INTEGER,
  task_id        TEXT,
  workflow_id    TEXT,
  detail         TEXT NOT NULL DEFAULT '',
  created_at     TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_gh_feedback_wf ON gh_feedback(workflow_id, delivery_id);
CREATE INDEX IF NOT EXISTS idx_gh_feedback_disp ON gh_feedback(disposition, delivery_id);

-- Side-effect ledger for the feedback route: at most ONE follow-up of each kind
-- per delivery_id. github_events.delivery_id is already the intake idempotency
-- key, but a planner that crashes between "ledger row written" and "command
-- marked applied" replays the delivery on restart; this table is the second
-- fence that keeps a replay from producing a second follow-up task, handoff or
-- outbox projection.
CREATE TABLE IF NOT EXISTS gh_followups (
  delivery_id TEXT NOT NULL,
  kind        TEXT NOT NULL CHECK (kind IN
                ('task','handoff','outbox_comment','outbox_label','timeline')),
  ref_id      TEXT NOT NULL DEFAULT '',
  created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (delivery_id, kind)
);

-- Per-project follow-up policy for GitHub feedback, stored separately so the
-- projects table is not touched. A repo with no row here has NO policy, which
-- means the safe default: operator handoff only, never an automatic task.
--   'handoff'       operator handoff only, NEVER an automatic task
--   'auto_dispatch' automatic follow-up task allowed for CODE-class failures
CREATE TABLE IF NOT EXISTS gh_project_policy (
  repo             TEXT PRIMARY KEY,
  followup_policy  TEXT NOT NULL CHECK (followup_policy IN
                      ('handoff','auto_dispatch')),
  updated_at       TEXT DEFAULT CURRENT_TIMESTAMP
);
