-- 015: skill lifecycle — versioned, evaluated, approved, deployed, rollbackable.
--
-- WHY this exists: a "skill" is a reusable instruction artifact (manifest +
-- content) that gets attached to planner/executor work. Because it changes what
-- an agent does, it may only reach 'active' by walking an auditable ladder:
--   proposed -> draft -> evaluated -> approved -> active -> retired
-- and every rung above 'draft' is keyed to a *content hash*, not to a version
-- string. Reusing the hash (instead of trusting a bumped version number) is
-- what makes "the thing we approved is the thing that runs" checkable.
--
-- Invariants enforced here (behaviour lives in clams.skills):
--   * content_hash is stored on the version, on the evaluation and on the
--     approval. Activation requires all three to agree, so editing content
--     after evaluation silently invalidates the stored approval.
--   * an evaluation row records eval_set_version, evaluator_version,
--     prompt_version and model_id, so a pass can be reproduced (or re-run)
--     later instead of being folklore.
--   * approvals and deployments are append-only ledgers: nothing is UPDATEd
--     away. Rollback writes a new deployment row and stamps rolled_back_at on
--     the one it replaces, so history shows exactly what was live when.
--   * source is constrained: a 'postmortem' artifact may only ever be
--     proposed/draft. The DB cannot express "this actor may not activate", so
--     clams.skills.store enforces it; the CHECK here just keeps the ledger
--     honest about where an artifact came from.
CREATE TABLE IF NOT EXISTS skills (
  skill_id        TEXT PRIMARY KEY,
  current_version TEXT NOT NULL DEFAULT '',
  status          TEXT NOT NULL CHECK (status IN
                    ('proposed','draft','evaluated','approved','active','retired')),
  created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at      TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_skills_status ON skills(status, skill_id);

-- One row per (skill, version). 'version' is semver and immutable: a content
-- change never rewrites an existing row's content_hash, it produces a new
-- version (or, for an in-place edit of an unapproved draft, a hash restamp that
-- clams.skills.lifecycle pairs with a status reset back to 'draft').
CREATE TABLE IF NOT EXISTS skill_versions (
  skill_id        TEXT NOT NULL REFERENCES skills(skill_id),
  version         TEXT NOT NULL,
  manifest_json   TEXT NOT NULL,
  content_hash    TEXT NOT NULL,
  status          TEXT NOT NULL CHECK (status IN
                    ('proposed','draft','evaluated','approved','active','retired')),
  source          TEXT NOT NULL CHECK (source IN
                    ('operator','postmortem','import','system')),
  provenance_json TEXT NOT NULL DEFAULT '{}',
  created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at      TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (skill_id, version)
);
CREATE INDEX IF NOT EXISTS idx_skill_versions_status
    ON skill_versions(status, skill_id, version);
CREATE INDEX IF NOT EXISTS idx_skill_versions_hash
    ON skill_versions(content_hash);

-- Append-only evaluation ledger. per_case_json is the ordered list of
-- {case_id, kind, passed, detail}; aggregate_json is the roll-up
-- {total, passed, failed, pass_rate, safety_total, safety_failed}.
-- 'passed' is NOT just pass_rate >= threshold: clams.skills.evaluate also
-- requires zero safety-case failures, so a good aggregate can still record
-- passed=0. content_hash ties the verdict to the exact bytes that ran.
CREATE TABLE IF NOT EXISTS skill_evaluations (
  evaluation_id     TEXT PRIMARY KEY,
  skill_id          TEXT NOT NULL REFERENCES skills(skill_id),
  version           TEXT NOT NULL,
  content_hash      TEXT NOT NULL,
  eval_set_version  TEXT NOT NULL,
  evaluator_version TEXT NOT NULL,
  prompt_version    TEXT NOT NULL DEFAULT '',
  model_id          TEXT NOT NULL DEFAULT '',
  per_case_json     TEXT NOT NULL,
  aggregate_json    TEXT NOT NULL,
  passed            INTEGER NOT NULL CHECK (passed IN (0, 1)),
  created_at        TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_skill_evaluations_lookup
    ON skill_evaluations(skill_id, version, evaluation_id);
CREATE INDEX IF NOT EXISTS idx_skill_evaluations_hash
    ON skill_evaluations(content_hash);

-- Append-only approval ledger. An approval is an assertion by a named human
-- about ONE content_hash: "I read these bytes and I allow them to run".
-- Nothing here is deleted or rewritten when content changes; the mismatch
-- between approval.content_hash and skill_versions.content_hash is itself the
-- evidence that the approval no longer applies. approved_by is never empty and
-- is never an automated identity for a postmortem-sourced artifact (enforced in
-- clams.skills.store, which is the only writer).
CREATE TABLE IF NOT EXISTS skill_approvals (
  approval_id  TEXT PRIMARY KEY,
  skill_id     TEXT NOT NULL REFERENCES skills(skill_id),
  version      TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  approved_by  TEXT NOT NULL,
  created_at   TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_skill_approvals_lookup
    ON skill_approvals(skill_id, version, approval_id);
CREATE INDEX IF NOT EXISTS idx_skill_approvals_hash
    ON skill_approvals(content_hash);

-- Deployment ledger: at most one row per skill with active=1 and
-- rolled_back_at IS NULL. Activating a version inserts a row and deactivates
-- the previously active one; a rollback inserts a NEW row for the previous
-- approved version and stamps rolled_back_at on the row it supersedes, so
-- "what was live at time T" is answerable from this table alone.
-- scope is the JSON-encoded permitted project/repo scope copied from the
-- manifest at deploy time, so a later manifest edit cannot widen what is live.
CREATE TABLE IF NOT EXISTS skill_deployments (
  deployment_id TEXT PRIMARY KEY,
  skill_id      TEXT NOT NULL REFERENCES skills(skill_id),
  version       TEXT NOT NULL,
  content_hash  TEXT NOT NULL DEFAULT '',
  scope         TEXT NOT NULL DEFAULT '{}',
  active        INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  rolled_back_at TEXT,
  created_by    TEXT NOT NULL DEFAULT '',
  created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_skill_deployments_active
    ON skill_deployments(skill_id, active, deployment_id);
CREATE INDEX IF NOT EXISTS idx_skill_deployments_created
    ON skill_deployments(created_at, deployment_id);

-- Dispatch/result attribution: every task dispatch records which skill version
-- ids were active at that moment, so a later post-mortem can attribute an
-- outcome to the artifact that was actually in force (rather than to whatever
-- is current when the report is written). Append-only, one row per
-- (task_id, skill_id) observation.
CREATE TABLE IF NOT EXISTS task_skill_versions (
  task_id    TEXT NOT NULL,
  workflow_id TEXT NOT NULL DEFAULT '',
  skill_id   TEXT NOT NULL,
  version    TEXT NOT NULL,
  content_hash TEXT NOT NULL DEFAULT '',
  phase      TEXT NOT NULL CHECK (phase IN ('dispatch', 'result')),
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (task_id, skill_id, phase)
);
CREATE INDEX IF NOT EXISTS idx_task_skill_versions_skill
    ON task_skill_versions(skill_id, version);
