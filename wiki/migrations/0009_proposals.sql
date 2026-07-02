-- Propose-then-approve lifecycle log + queue read model (Arc 2).
--
-- DUAL-WRITE design: moderation_state.proposal (JSON blob) remains the
-- OPERATIONAL pending queue — its contract is live and tested. This table is
-- the durable lifecycle record (every proposal from creation to decision,
-- including silent expiries) and the read model for /proposals and /stats.
-- Every blob mutation site in the Worker also writes here, same batch.
-- Telemetry-only: writes to this table never bump articles.version.
--
-- NB deploy: remote wrangler migration-tracking is out of sync (see memory /
-- 0008 precedent) — apply this file via `wrangler d1 execute wikilean --remote
-- --file=migrations/0009_proposals.sql`, NOT `d1 migrations apply`.
CREATE TABLE IF NOT EXISTS proposals (
  id TEXT PRIMARY KEY,               -- proposalId (12 hex; shared with the blob)
  slug TEXT NOT NULL,
  annotation_id TEXT NOT NULL,       -- target annotation (12 hex)
  fields TEXT NOT NULL,              -- JSON delta the AI proposed
  fields_sig TEXT NOT NULL,          -- stable-JSON signature (dedupe key)
  reason TEXT,                       -- the AI's one-line justification
  run_id TEXT,                       -- pipeline run that produced it
  model TEXT,
  status TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected | stale
  reject_reason TEXT,                -- human's enum on reject (nullable)
  created_at INTEGER NOT NULL,       -- ms
  decided_at INTEGER,                -- ms; null while pending
  decided_by TEXT                    -- users.id of the human who decided
);
CREATE INDEX IF NOT EXISTS idx_proposals_status_created ON proposals (status, created_at);
CREATE INDEX IF NOT EXISTS idx_proposals_slug_status ON proposals (slug, status);
