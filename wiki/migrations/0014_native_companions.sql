-- Native Literature Links annotations live inside work-item snapshots in articles.annotations.
-- No automatic rewrite: use the explicit local migration rehearsal, and keep
-- source pins absent for old revisions rather than guessing historical context.
--
-- Not idempotent: the two ADD COLUMN statements run once (SQLite has no
-- ADD COLUMN IF NOT EXISTS, so a second run fails on the duplicate column).
-- Apply it before deploying or promoting any Worker built from a commit that
-- contains this file. That Worker reads and writes both columns on every save,
-- revert and create, and answers 500 while they are missing. Workers built
-- before this file ignore the new columns, so applying it early is safe.
--
-- NB deploy: remote migration tracking is out of sync. After a D1 backup
-- (`npm run backup:d1`, see docs/BACKUP.md), apply remote once via
--   wrangler d1 execute wikilean --remote --file=migrations/0014_native_companions.sql
-- and verify that both columns and the archive table exist:
--   SELECT name FROM pragma_table_info('revisions') WHERE name IN ('source_revid','schema_version');
--   SELECT name FROM sqlite_master WHERE type='table' AND name='companion_migrations';
ALTER TABLE revisions ADD COLUMN source_revid INTEGER;
ALTER TABLE revisions ADD COLUMN schema_version INTEGER;

-- Exact pre-migration bytes for auditing and explicit reverse migration.
-- New edits must never be discarded by restoring this archive blindly.
CREATE TABLE IF NOT EXISTS companion_migrations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slug TEXT NOT NULL,
  article_version INTEGER NOT NULL,
  source_revid INTEGER NOT NULL,
  schema_version INTEGER NOT NULL,
  annotations TEXT NOT NULL,
  pending_proposals TEXT,
  rejected_proposals TEXT,
  diagnostics TEXT NOT NULL,
  migrated_at INTEGER NOT NULL,
  UNIQUE (slug, article_version)
);
