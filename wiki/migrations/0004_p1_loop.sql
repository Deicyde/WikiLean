-- P1 "close the loop" schema (Wave A). Three parts in one migration:
--   1. articles: upstream-tracking columns + annotation schema generation
--   2. moderation_state: THE single work table (binding decision — absorbs
--      the would-be article_updates table)
--   3. revisions: kind/meta/parent_id + backfill (must land BEFORE the first
--      bearer write — a 'pipeline' users row breaks the NULL-user convention)
-- App-table timestamps are integer milliseconds (0001 convention).
-- D1/SQLite: one ADD COLUMN per ALTER; NOT NULL adds require a DEFAULT.

-- 1. articles ---------------------------------------------------------------
-- latest_revid: newest upstream Wikipedia revid seen by drift detection
-- (null = unknown). last_upstream_check: ms of the last drift check (null =
-- never checked). CACHE INVARIANT: writes to latest_revid /
-- last_upstream_check must NEVER bump `version` — the staleness UI is
-- injected per-request, never baked into the cached base page. `revid` still
-- advances only atomically with a re-anchored annotations payload.
ALTER TABLE articles ADD COLUMN latest_revid INTEGER;
ALTER TABLE articles ADD COLUMN last_upstream_check INTEGER;
-- schema_version: generation of the annotations JSON blob (currently 3
-- everywhere; v4 = multi-library formalizations[] is deferred).
ALTER TABLE articles ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 3;

-- 2. moderation_state -------------------------------------------------------
-- The single work table: feeds GET /api/work with one ORDER BY —
-- flagged > drifted > human-edited-since-review > oldest-reviewed > new.
-- Latest-revid data lives on `articles` (decided), never duplicated here.
-- `state` is null in the normal flow; update-flow values include
-- 'needs_human', 'moved', 'deleted'. `proposal` holds the JSON payload of a
-- pending re-anchor awaiting review.
CREATE TABLE moderation_state (
  slug                  TEXT PRIMARY KEY REFERENCES articles(slug),
  last_reviewed_at      INTEGER,                     -- ms; null = never reviewed
  last_reviewed_version INTEGER,                     -- articles.version at last review
  wp_drifted            INTEGER NOT NULL DEFAULT 0,  -- boolean: upstream moved past pinned revid
  flag_count            INTEGER NOT NULL DEFAULT 0,
  state                 TEXT,
  proposal              TEXT,
  updated_at            INTEGER                      -- ms
);
CREATE INDEX idx_moderation_state_reviewed ON moderation_state (last_reviewed_at);

-- 3. revisions --------------------------------------------------------------
-- kind vocabulary: edit | revert | seed | pipeline | contribution.
-- meta: JSON (run_id, model, tokens, cost, mathlib_sha, auth_mode,
-- approved_by, ...). parent_id: the revision this edit was based on (no FK —
-- SQLite ALTER ... ADD COLUMN cannot add one).
ALTER TABLE revisions ADD COLUMN kind TEXT NOT NULL DEFAULT 'edit';
ALTER TABLE revisions ADD COLUMN meta TEXT;
ALTER TABLE revisions ADD COLUMN parent_id INTEGER;

-- Backfill. Patterns verified against the actual writers: src/index.ts uses
-- `revert to #${revid}` (always has a user_id); scripts/seed.ts /
-- seed-delta.ts / seed-refresh.ts write 'seed import' / 'orphan refresh
-- import' / 'pipeline refresh', all with user_id NULL — so user_id IS NULL
-- covers every system import to date.
UPDATE revisions SET kind = 'revert' WHERE comment LIKE 'revert to #%';
UPDATE revisions SET kind = 'seed' WHERE user_id IS NULL;
