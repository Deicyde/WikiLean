-- Formalization atlas feedback (docs/ATLAS.md section 10). Append-only: one row per verdict,
-- never updated. A person's current verdict on one judgment is the newest row for
-- (location_key, campaign, user_id): view atlas_feedback_latest, where 'withdraw' means none.
-- revision, campaign, record_set (the article's SEAL sha) and relation are copied from the
-- store at write time. pair_key: the producer's revision- and commit-free key, NULL until the
-- store carries it. Rows are human labels: the route refuses the pipeline bearer, the CHECK
-- keeps it so.
-- Shape guard: the pair_key index is created first, so on a database that still holds the
-- superseded atlas_feedback table this file stops there, before it creates anything else.
-- Drop that table first (it was never applied in production; check dev and local).
-- Deploy after a D1 backup: wrangler d1 execute wikilean --remote --file=migrations/0015_atlas_feedback.sql
CREATE TABLE IF NOT EXISTS atlas_feedback (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  location_key TEXT NOT NULL CHECK (length(location_key) = 64 AND location_key NOT GLOB '*[^0-9a-f]*'),
  pair_key     TEXT CHECK (pair_key IS NULL OR (length(pair_key) = 64 AND pair_key NOT GLOB '*[^0-9a-f]*')),
  pageid       INTEGER NOT NULL CHECK (pageid > 0),
  revision     INTEGER NOT NULL CHECK (revision > 0),
  campaign     TEXT NOT NULL,
  record_set   TEXT NOT NULL CHECK (length(record_set) = 64 AND record_set NOT GLOB '*[^0-9a-f]*'),
  relation     TEXT NOT NULL CHECK (length(relation) BETWEEN 1 AND 64),
  verdict      TEXT NOT NULL CHECK (verdict IN ('confirm', 'report', 'withdraw')),
  note         TEXT CHECK (note IS NULL OR length(note) <= 500),
  provenance   TEXT NOT NULL DEFAULT 'human' CHECK (provenance IN ('human')),
  user_id      TEXT NOT NULL REFERENCES users(id),
  created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_atlas_feedback_pair ON atlas_feedback (pair_key) WHERE pair_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_atlas_feedback_user_page ON atlas_feedback (user_id, pageid, campaign);
CREATE INDEX IF NOT EXISTS idx_atlas_feedback_judgment ON atlas_feedback (location_key, campaign, user_id, id);
CREATE VIEW IF NOT EXISTS atlas_feedback_latest AS
  SELECT f.* FROM atlas_feedback AS f
  WHERE f.id = (SELECT max(g.id) FROM atlas_feedback AS g
                WHERE g.location_key = f.location_key AND g.campaign = f.campaign AND g.user_id = f.user_id);
