-- Formalization atlas feedback (docs/ATLAS.md, "Feedback"): a signed-in person
-- confirms or reports ONE location of the read-only atlas store (one passage
-- linked to one declaration at one commit). This is the atlas plane's only D1
-- table: it never touches articles, revisions or annotations, and nothing here
-- bumps articles.version.
--
-- location_key is the locate pipeline's run-free key for (source, passage,
-- library, commit, declaration); the Worker stores only keys present in the
-- current store article. revision and campaign are copied from that store
-- article at write time, so a verdict names exactly what the person saw. One
-- row per (location_key, user_id): a second verdict replaces the first
-- (updated_at moves, created_at keeps the first verdict's time). note is
-- bounded by the Worker (500 characters) and by the CHECK below.
--
-- GET /api/atlas/feedback/export (pipeline bearer only) streams these rows,
-- users pseudonymized, as benchmark seeds.
--
-- Idempotent (CREATE ... IF NOT EXISTS). A Worker built from a commit with
-- this file answers 503 "atlas feedback not configured" on the two feedback
-- routes until it is applied; every other route is unaffected.
--
-- NB deploy: remote migration tracking is out of sync. After a D1 backup
-- (`npm run backup:d1`, see docs/BACKUP.md), apply remote via
--   wrangler d1 execute wikilean --remote --file=migrations/0015_atlas_feedback.sql
-- and verify:
--   SELECT name FROM sqlite_master WHERE type='table' AND name='atlas_feedback';
CREATE TABLE IF NOT EXISTS atlas_feedback (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  location_key TEXT NOT NULL CHECK (length(location_key) = 64),
  pageid       INTEGER NOT NULL,             -- Wikipedia page id (the store key)
  revision     INTEGER NOT NULL,             -- the revision the store article pinned
  campaign     TEXT NOT NULL,                -- the locate campaign the person saw
  verdict      TEXT NOT NULL CHECK (verdict IN ('confirm', 'report')),
  note         TEXT CHECK (note IS NULL OR length(note) <= 500),
  user_id      TEXT NOT NULL REFERENCES users(id),
  created_at   INTEGER NOT NULL,             -- ms, first verdict
  updated_at   INTEGER NOT NULL,             -- ms, latest verdict
  UNIQUE (location_key, user_id)
);
CREATE INDEX IF NOT EXISTS idx_atlas_feedback_user_page ON atlas_feedback (user_id, pageid);
CREATE INDEX IF NOT EXISTS idx_atlas_feedback_location ON atlas_feedback (location_key);
