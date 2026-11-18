-- Community coverage bundles on the formalization reader's overlay (issue #173;
-- docs/ARTICLES.md section 10a, "The coverage bundle"). A community tag is one
-- declaration with one relation, so until now the overlay had no way to say
-- "these declarations together state this passage". Without it, the only way to
-- keep a jointly formalized passage's status right was to leave relations the
-- editor knows are overstated: correcting each link to `hasPart` drops the
-- passage to partial, because a part states only part of it.
--
-- This table is the overlay's mirror of the sealed store's coverage claim
-- (build_store.coverage_claim, the /2 bundle {complete, parts, challenge,
-- relation, conjuncts, bundle_key}). It is a D1 overlay over an immutable base,
-- exactly as `article_tags` (migration 0019) is: live on save, attributed,
-- human/agent labelled, and a soft delete that leaves a gravestone
-- (removed_by / removed_at / removed_reason). The sealed record store is never
-- written, and `/api/articles/article/:pageid` stays the sealed, cacheable view
-- model; the merge is per request (wiki/src/articles-overlay.ts).
--
-- One row per `bundle_key`:
--
--   bundle_key = sha256 of the passage key and the parts' pair keys, sorted,
--                one per line  (build_store.bundle_key / cflib.hashing.bundle_key)
--   pair_key   = sha256("<passage_key>\n<repository>\n<declaration>")
--   passage_key = sha256("<pageid>\n<span_sha256>")
--
-- All three are revision- and commit-free, so a bundle survives a store rebuild
-- and two editors who name the same parts write THE SAME ROW. Only a
-- cf-locate-recordset/2 store carries the keys; the routes refuse an article
-- from a /1 store (422), as the tag routes do.
--
-- `parts` is a JSON array of 2 to 5 pair keys and `conjuncts` a JSON array of
-- {id, text, part} (ids c1..cN in order, each part one of `parts`, every part
-- stating at least one), the shapes the builder's bundle_problems checks. SQLite
-- has no array type and D1 has no JSON column type; the route validates both
-- before the write and the reader parses them defensively, so a row that somehow
-- holds something else is ignored rather than fatal. Their CHECKs use SQLite's
-- JSON1 functions (json_valid, json_type, json_array_length), which D1 supports:
-- a second pair of eyes on the shape, never the only one.
--
-- `relation` is the bundle's own — its weakest part's — and is one of the three
-- strong relations, as the producer's BUNDLE_RELATIONS is. `campaign`,
-- `revision`, `record_set` and `universe_sha256` are copied from the store at
-- write time, never from the client. `version` is the row's optimistic-concurrency
-- counter: 1 on create, +1 per edit, and a write whose base_version does not match
-- answers 409 "stale".
--
-- Idempotent (CREATE TABLE/INDEX IF NOT EXISTS).
--
-- NB deploy: remote migration tracking is out of sync. After a D1 backup
-- (`npm run backup:d1`, see docs/BACKUP.md), apply remote once via
--   wrangler d1 execute wikilean --remote --file=migrations/0021_article_coverage.sql
-- and verify (it must list the table's columns):
--   SELECT name FROM pragma_table_info('article_coverage');
-- The promoter refuses any Worker built from a commit containing this file until
-- article_coverage exists (site/ops/brain_promote_release.py D1_REQUIRED_COLUMNS).
-- Until it is applied the two coverage routes answer 503 and the rest of the
-- overlay works, exactly as an unapplied 0019 leaves the sealed article readable.

CREATE TABLE IF NOT EXISTS article_coverage (
  bundle_key     TEXT PRIMARY KEY
                 CHECK (length(bundle_key) = 64 AND bundle_key NOT GLOB '*[^0-9a-f]*'),
  pageid         INTEGER NOT NULL CHECK (pageid > 0),
  passage_key    TEXT NOT NULL
                 CHECK (length(passage_key) = 64 AND passage_key NOT GLOB '*[^0-9a-f]*'),
  -- A JSON array of 2 to 5 pair keys (the parts), and a JSON array of 2 to 5
  -- {id, text, part} objects (the clauses each part states).
  parts          TEXT NOT NULL CHECK (json_valid(parts)
                   AND json_type(parts) = 'array'
                   AND json_array_length(parts) BETWEEN 2 AND 5),
  conjuncts      TEXT NOT NULL CHECK (json_valid(conjuncts)
                   AND json_type(conjuncts) = 'array'
                   AND json_array_length(conjuncts) BETWEEN 2 AND 5),
  relation       TEXT NOT NULL CHECK (relation IN ('exactMatch', 'closeMatch', 'broadMatch')),
  state          TEXT NOT NULL DEFAULT 'live' CHECK (state IN ('live', 'removed')),
  origin         TEXT NOT NULL DEFAULT 'community' CHECK (origin IN ('community')),
  added_by       TEXT NOT NULL REFERENCES users(id),
  actor_type     TEXT NOT NULL DEFAULT 'human' CHECK (actor_type IN ('human', 'agent')),
  via            TEXT NOT NULL DEFAULT 'web' CHECK (via IN ('web', 'api', 'intake')),
  -- Reserved for release R5's machine_tokens, as article_tags.token_id is: an
  -- agent is identified by added_by (agent_tokens.agent_id), so it needs none.
  token_id       TEXT,
  model          TEXT CHECK (model IS NULL OR length(model) <= 100),
  run_id         TEXT CHECK (run_id IS NULL OR length(run_id) <= 100),
  campaign       TEXT NOT NULL,
  revision       INTEGER NOT NULL CHECK (revision > 0),
  record_set     TEXT NOT NULL
                 CHECK (length(record_set) = 64 AND record_set NOT GLOB '*[^0-9a-f]*'),
  universe_sha256 TEXT NOT NULL
                 CHECK (length(universe_sha256) = 64 AND universe_sha256 NOT GLOB '*[^0-9a-f]*'),
  version        INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  created_at     INTEGER NOT NULL,
  updated_at     INTEGER NOT NULL,
  removed_by     TEXT REFERENCES users(id),
  removed_at     INTEGER,
  removed_reason TEXT CHECK (removed_reason IS NULL OR length(removed_reason) <= 500)
);

-- The reader's one indexed read per article view, as article_tags has.
CREATE INDEX IF NOT EXISTS idx_article_coverage_page ON article_coverage (pageid, state);
-- A person's own bundles, for attribution and for the daily caps, which count
-- writes in a window (updated_at / removed_at) rather than creations.
CREATE INDEX IF NOT EXISTS idx_article_coverage_author ON article_coverage (added_by, created_at);
CREATE INDEX IF NOT EXISTS idx_article_coverage_author_day ON article_coverage (added_by, updated_at);
CREATE INDEX IF NOT EXISTS idx_article_coverage_remover_day ON article_coverage (removed_by, removed_at)
  WHERE removed_by IS NOT NULL;
-- The patrol feed R2/R3 build on, and the newest-first listing R1 already needs.
CREATE INDEX IF NOT EXISTS idx_article_coverage_recent ON article_coverage (state, created_at);
