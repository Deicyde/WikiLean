-- Native Literature Links annotations live inside work-item snapshots in articles.annotations.
-- No automatic rewrite: use the explicit local migration rehearsal, and keep
-- source pins absent for old revisions rather than guessing historical context.
ALTER TABLE revisions ADD COLUMN source_revid INTEGER;
ALTER TABLE revisions ADD COLUMN schema_version INTEGER;

-- Exact pre-migration bytes for auditing and explicit reverse migration.
-- New edits must never be discarded by restoring this archive blindly.
CREATE TABLE companion_migrations (
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
