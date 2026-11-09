-- Self-service pipeline intake. These are private staging tables, never live
-- annotations, article_tags, or Brain data. Exact uploaded bytes are retained in
-- bounded chunks; only the trusted Python validator may accept or reject a set.
-- Idempotent. After an encrypted backup, apply this file explicitly with
-- wrangler d1 execute wikilean --remote --file=migrations/0020_pipeline_submissions.sql
-- (remote migration tracking is not synchronized; never migrations apply --remote).
CREATE TABLE IF NOT EXISTS pipeline_submissions (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL REFERENCES users(id),
  owner_id TEXT NOT NULL REFERENCES users(id),
  seal_sha256 TEXT NOT NULL CHECK(length(seal_sha256) = 64),
  body_sha256 TEXT NOT NULL CHECK(length(body_sha256) = 64),
  body_bytes INTEGER NOT NULL CHECK(body_bytes BETWEEN 1 AND 4194304),
  received_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('queued','validating','accepted','rejected','failed')),
  attempts INTEGER NOT NULL DEFAULT 0,
  lease_id TEXT,
  lease_until INTEGER,
  verdict TEXT,
  UNIQUE(agent_id, seal_sha256)
);
CREATE INDEX IF NOT EXISTS idx_pipeline_submissions_owner_day
  ON pipeline_submissions(owner_id, received_at);
CREATE INDEX IF NOT EXISTS idx_pipeline_submissions_queue
  ON pipeline_submissions(state, lease_until, received_at);
CREATE TABLE IF NOT EXISTS pipeline_submission_chunks (
  submission_id TEXT NOT NULL REFERENCES pipeline_submissions(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 63),
  bytes BLOB NOT NULL CHECK(length(bytes) BETWEEN 1 AND 65536),
  PRIMARY KEY(submission_id, ordinal)
);
