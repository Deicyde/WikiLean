-- The claims API (docs/ROADMAP.md, outside contributors: Jack, 2026-11-11, "Build a claims
-- API"): work packages, each one planned locate run whose bytes are one object in the
-- private R2 bucket wikilean-contrib (packages/v1/<envelope sha256>.json); the claims
-- agents hold on them; and the intake submissions a claim produced (src/claims.ts).
-- Coordination only: no public page reads these, and intake receipts are unchanged.
--
-- Idempotent. Apply after 0022 (contrib_packages references index_bundles), after an
-- encrypted backup (cd wiki && npm run backup:d1):
--   npx wrangler d1 execute wikilean --remote --file=migrations/0023_contrib_claims.sql
-- Verify:
--   SELECT name FROM pragma_table_info('contrib_claim_submissions');
--   SELECT value FROM site_settings WHERE key = 'claims';
-- Never wrangler d1 migrations apply --remote (remote tracking is out of sync).
-- Created in this order; the promoter refuses a Worker built from a commit containing this
-- file until contrib_claim_submissions exists (site/ops/brain_promote_release.py
-- D1_REQUIRED_COLUMNS). Agent revocation (src/agents.ts) ends the agent's live claims, so
-- that code needs this table on D1 before it is promoted.

-- The lever, shut: 'open' takes claims; anything else, a missing row or a missing table
-- refuses (site-settings.ts leverOpen). The operator opens it with one statement and no
-- deploy, and INSERT OR IGNORE never reopens it:
--   UPDATE site_settings SET value = 'open', updated_at = <now in ms> WHERE key = 'claims';
INSERT OR IGNORE INTO site_settings (key, value, updated_at)
  VALUES ('claims', 'closed', CAST(strftime('%s', 'now') AS INTEGER) * 1000);

-- One row per published package (POST /api/packages, the pipeline bearer only), never
-- replaced in place. The coordinator moves it between open (claimable), done and retired.
CREATE TABLE IF NOT EXISTS contrib_packages (
  run_id          TEXT PRIMARY KEY CHECK (length(run_id) BETWEEN 1 AND 64),
  envelope_sha256 TEXT NOT NULL UNIQUE CHECK (length(envelope_sha256) = 64),
  bytes           INTEGER NOT NULL CHECK (bytes > 0),
  bundle_sha256   TEXT NOT NULL REFERENCES index_bundles(manifest_sha256),
  universe_sha256 TEXT NOT NULL CHECK (length(universe_sha256) = 64),
  index_sha256    TEXT CHECK (index_sha256 IS NULL OR length(index_sha256) = 64),
  campaign        TEXT NOT NULL CHECK (length(campaign) BETWEEN 1 AND 140),
  mode            TEXT NOT NULL CHECK (length(mode) BETWEEN 1 AND 32),
  tasks           INTEGER NOT NULL CHECK (tasks > 0),
  sources         INTEGER NOT NULL CHECK (sources > 0),
  max_cost        REAL,
  state           TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'done', 'retired')),
  published_at    INTEGER NOT NULL,
  updated_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contrib_packages_state ON contrib_packages (state, published_at);

-- An agent's hold on a package: live, then completed, released, expired or revoked.
CREATE TABLE IF NOT EXISTS contrib_claims (
  id          TEXT PRIMARY KEY CHECK (length(id) = 36),              -- 'clm-' + 32 hex
  run_id      TEXT NOT NULL REFERENCES contrib_packages(run_id),
  agent_id    TEXT NOT NULL REFERENCES users(id),
  owner_id    TEXT NOT NULL REFERENCES users(id),
  request_key TEXT NOT NULL CHECK (length(request_key) = 32),       -- the client's idempotency key
  state       TEXT NOT NULL CHECK (state IN ('live', 'completed', 'released', 'expired', 'revoked')),
  claimed_at  INTEGER NOT NULL,
  renewed_at  INTEGER NOT NULL,
  lease_until INTEGER NOT NULL,
  ended_at    INTEGER,
  end_reason  TEXT CHECK (end_reason IS NULL OR length(end_reason) <= 40),
  note        TEXT CHECK (note IS NULL OR length(note) <= 500),
  UNIQUE (agent_id, request_key)
);
-- One live claim per package, whatever any statement does.
CREATE UNIQUE INDEX IF NOT EXISTS idx_contrib_claims_one_live ON contrib_claims (run_id) WHERE state = 'live';
CREATE INDEX IF NOT EXISTS idx_contrib_claims_owner ON contrib_claims (owner_id, state, claimed_at);
CREATE INDEX IF NOT EXISTS idx_contrib_claims_lease ON contrib_claims (state, lease_until);

-- Which intake submission a claim produced. No foreign key to pipeline_submissions, so an
-- archive of old submissions never has to touch these rows.
CREATE TABLE IF NOT EXISTS contrib_claim_submissions (
  submission_id TEXT PRIMARY KEY CHECK (length(submission_id) = 68), -- pipeline_submissions.id
  claim_id      TEXT NOT NULL REFERENCES contrib_claims(id),
  linked_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contrib_claim_submissions_claim ON contrib_claim_submissions (claim_id);
