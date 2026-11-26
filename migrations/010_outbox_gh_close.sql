-- 010: widen the outbox kind CHECK to allow 'gh_close'.
--
-- gh_close shipped in code with the #136 replan work (task #142: close a
-- superseded task's sub-issue), but 001_init's kind CHECK was never widened.
-- repo_outbox.enqueue swallows IntegrityError as its dedupe signal, so on a
-- live database every gh_close enqueue was silently dropped: superseded
-- sub-issues stayed open. SQLite cannot ALTER a CHECK constraint, so the
-- table is rebuilt (copy -> drop -> rename -> reindex). The outbox is a leaf
-- table (no inbound FKs), and column order is preserved for SELECT *.
CREATE TABLE outbox_new (
  outbox_id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN
    ('bus_publish','gh_comment','gh_label','gh_board_move','gh_subissue',
     'gh_close')),
  payload TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','inflight','sent','dead')),
  attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT DEFAULT CURRENT_TIMESTAMP,
  last_error TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO outbox_new SELECT * FROM outbox;
DROP TABLE outbox;
ALTER TABLE outbox_new RENAME TO outbox;
CREATE INDEX idx_outbox_due ON outbox(status, next_attempt_at);
