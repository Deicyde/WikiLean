-- P2 instrumentation + anonymous-flag pipeline (Wave D). Three parts in one
-- migration:
--   1. annotation_events: server-side field-level change log, emitted on
--      EVERY write path (session save, bot save, create, revert, endorse)
--   2. flags: anonymous reader problem reports (feed moderation_state.
--      flag_count and the /api/work priority queue)
--   3. articles: per-status annotation counts for the dynamic homepage
-- App-table timestamps are integer milliseconds (0001 convention).
-- D1/SQLite: one ADD COLUMN per ALTER; NOT NULL adds require a DEFAULT.

-- 1. annotation_events --------------------------------------------------------
-- One row per annotation-level change, diffed BY ID (stored vs persisted) at
-- write time. event_type 'reject' = status flipped to 'rejected' (tombstone);
-- 'endorse' = the human-agreement provenance flip; 'revert_restore' = the
-- annotation changed in a revert. actor_type comes from the auth seam
-- (bearer = 'pipeline', session = 'human'); user_id is the acting user.
-- field_changes: JSON {field: [old, new]} with dotted paths for nested fields
-- (e.g. mathlib.decl), capped at 4 KB ({"_truncated":true} marks a cut).
CREATE TABLE annotation_events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  revision_id   INTEGER NOT NULL,
  slug          TEXT NOT NULL,
  annotation_id TEXT NOT NULL,
  event_type    TEXT NOT NULL CHECK(event_type IN ('add','modify','delete','endorse','reject','revert_restore')),
  actor_type    TEXT NOT NULL CHECK(actor_type IN ('human','pipeline')),
  user_id       TEXT,
  field_changes TEXT,
  created_at    INTEGER NOT NULL
);
CREATE INDEX idx_annotation_events_annotation ON annotation_events (annotation_id, created_at);
CREATE INDEX idx_annotation_events_slug ON annotation_events (slug, created_at);

-- 2. flags --------------------------------------------------------------------
-- Anonymous (no-auth) reader reports from the tooltip micro-form. annotation_id
-- NULL = a whole-article report. ip_hash is the sha256 hex of CF-Connecting-IP
-- (pseudonymous abuse handle; never exported). status: 'open' until a
-- patroller/admin resolves it to 'fixed' or 'dismissed'.
CREATE TABLE flags (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  slug          TEXT NOT NULL,
  annotation_id TEXT,
  reason        TEXT NOT NULL CHECK(reason IN ('wrong_decl','wrong_status','irrelevant','missing_formalization','other')),
  comment       TEXT,
  user_id       TEXT,
  ip_hash       TEXT,
  status        TEXT NOT NULL DEFAULT 'open',
  resolved_by   TEXT,
  resolved_at   INTEGER,
  created_at    INTEGER NOT NULL
);
CREATE INDEX idx_flags_slug_status ON flags (slug, status);

-- 3. articles: per-status counts ----------------------------------------------
-- Computed from the FINAL persisted annotations (tombstones excluded) in every
-- write path, in the same UPDATE as the annotations blob. NULL = not yet
-- computed (backfill fills them; a write also converges a row). These never
-- appear in render-cache keys.
ALTER TABLE articles ADD COLUMN n_formalized INTEGER;
ALTER TABLE articles ADD COLUMN n_partial INTEGER;
ALTER TABLE articles ADD COLUMN n_not_formalized INTEGER;
