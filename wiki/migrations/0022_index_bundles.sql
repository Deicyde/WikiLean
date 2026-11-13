-- Token-gated downloads of the declaration-index bundles (docs/ROADMAP.md, outside
-- contributors: Jack, 2026-11-11, "Token-gated download"). The bytes live in the
-- private R2 bucket wikilean-contrib (binding CONTRIB), which the Worker only reads;
-- scripts/articles/upload_contrib.py puts a bundle there under
-- index/v1/<manifest sha256>/. These tables record which bundles the Worker serves
-- (src/bundles.ts) and every range it served, which is also each owner's daily quota.
--
-- Idempotent. After an encrypted backup (cd wiki && npm run backup:d1), apply once:
--   npx wrangler d1 execute wikilean --remote --file=migrations/0022_index_bundles.sql
-- Verify:
--   SELECT name FROM pragma_table_info('index_downloads');
--   SELECT value FROM site_settings WHERE key = 'index_downloads';
-- Never wrangler d1 migrations apply --remote (remote tracking is out of sync).
-- Created in this order; the promoter refuses a Worker built from a commit containing
-- this file until index_downloads exists (site/ops/brain_promote_release.py
-- D1_REQUIRED_COLUMNS).

-- The lever, shut. 'open' serves bundles; any other value, a missing row or a missing
-- table refuses (site-settings.ts leverOpen). The operator opens it with one statement
-- and no deploy:
--   UPDATE site_settings SET value = 'open', updated_at = <now in ms> WHERE key = 'index_downloads';
-- INSERT OR IGNORE, so re-applying this file never re-opens a lever the operator closed.
INSERT OR IGNORE INTO site_settings (key, value, updated_at)
  VALUES ('index_downloads', 'closed', CAST(strftime('%s', 'now') AS INTEGER) * 1000);

-- One row per registered bundle (POST /api/index/bundles, the pipeline bearer only).
-- files_json is the manifest's file list verbatim, and the per-request allowlist of
-- what may be downloaded. Retiring a bundle is a state change, never a delete:
--   UPDATE index_bundles SET state = 'retired' WHERE manifest_sha256 = '<64 hex>';
CREATE TABLE IF NOT EXISTS index_bundles (
  manifest_sha256 TEXT PRIMARY KEY
                  CHECK (length(manifest_sha256) = 64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  bundle          TEXT NOT NULL CHECK (length(bundle) BETWEEN 1 AND 300),
  universe_sha256 TEXT NOT NULL CHECK (length(universe_sha256) = 64),
  index_path      TEXT NOT NULL CHECK (length(index_path) BETWEEN 1 AND 300),
  index_sha256    TEXT NOT NULL CHECK (length(index_sha256) = 64),
  manifest_bytes  INTEGER NOT NULL CHECK (manifest_bytes > 0),
  bytes           INTEGER NOT NULL CHECK (bytes > 0),
  files_json      TEXT NOT NULL,
  restricted_json TEXT NOT NULL,
  state           TEXT NOT NULL DEFAULT 'live' CHECK (state IN ('live', 'retired')),
  registered_at   INTEGER NOT NULL
);

-- One row per range served: the audit log (who downloaded what, never an IP) and the
-- owner's daily byte quota, written by the statement that re-checks access.
CREATE TABLE IF NOT EXISTS index_downloads (
  id            INTEGER PRIMARY KEY,
  owner_id      TEXT NOT NULL REFERENCES users(id),
  agent_id      TEXT NOT NULL REFERENCES users(id),
  bundle_sha256 TEXT NOT NULL REFERENCES index_bundles(manifest_sha256),
  path          TEXT NOT NULL CHECK (length(path) BETWEEN 1 AND 300),
  range_start   INTEGER NOT NULL CHECK (range_start >= 0),
  bytes         INTEGER NOT NULL CHECK (bytes >= 0),
  at            INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_index_downloads_owner_at ON index_downloads (owner_id, at);
