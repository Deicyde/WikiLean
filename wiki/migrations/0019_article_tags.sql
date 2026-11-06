-- Community tags on the formalization reader's overlay (docs/ARTICLES.md
-- "Community tags"; the signed public-editing plan, sections 2.2, 2.5, 2.9 and
-- release R1). The sealed record store is never written: a community tag is a D1
-- row over an immutable base, the pattern brain_edges/brain_nodes already runs
-- for the Brain. Live on save, attributed, human/agent labelled; correction is a
-- SOFT delete that leaves a gravestone (removed_by/removed_at/removed_reason).
--
-- Three tables, created in this order so the last one standing is the one the
-- promoter checks (D1_REQUIRED_COLUMNS names article_tags; its presence means the
-- two before it ran):
--
--   site_settings    the site-wide write brake. One row, key 'site_write_mode',
--                    value 'open' (writes allowed) or anything else (refused).
--                    An admin flips it with one D1 write — no release, which
--                    matters because there is no Worker-only hotfix path.
--   user_agreements  the CC0 agreement a person gives at their first overlay
--                    edit, with its timestamp, and when Turnstile cleared them.
--                    A table of its own: `users` is never altered (CLAUDE.md,
--                    "Site/D1" — an ADD COLUMN is the non-idempotent exception).
--   article_tags     one row per pair_key: the community's add, change or
--                    removal of one (passage, repository, declaration).
--
-- pair_key = sha256("<passage_key>\n<repository>\n<declaration>") and
-- passage_key = sha256("<pageid>\n<span_sha256>"), both revision- and commit-free
-- (scripts/articles/build_store.py pair_key/passage_key). A community tag of the
-- declaration the pipeline already located on that passage is therefore THE SAME
-- ROW as the pipeline's location: agreement, correction and removal share one key
-- and there is no re-pairing problem. Only a cf-locate-recordset/2 store carries
-- the keys; the routes refuse an article from a /1 store.
--
-- `revision`, `campaign`, `record_set` and `universe_sha256` are copied from the
-- store at write time, never from the client, exactly as the feedback route does
-- (articles.ts) — an edit always names what the editor saw. `base_relation` holds
-- the pipeline relation a change overrides (NULL for a brand-new location).
-- `version` is the row's optimistic-concurrency counter: 1 on create, +1 per edit,
-- and a write whose base_version does not match answers 409 "stale".
--
-- R1 deliberately has no history, diff or revert table (release R2 adds
-- article_overlay_revisions and article_overlay). An admin reverts by writing the
-- row back, as the Brain overlay's gravestone model allows.
--
-- Idempotent (CREATE TABLE/INDEX IF NOT EXISTS, INSERT OR IGNORE).
--
-- NB deploy: remote migration tracking is out of sync. After a D1 backup
-- (`npm run backup:d1`, see docs/BACKUP.md), apply remote once via
--   wrangler d1 execute wikilean --remote --file=migrations/0019_article_tags.sql
-- and verify (each must list its columns, and the third must print 'open'):
--   SELECT name FROM pragma_table_info('site_settings');
--   SELECT name FROM pragma_table_info('user_agreements');
--   SELECT name FROM pragma_table_info('article_tags');
--   SELECT value FROM site_settings WHERE key = 'site_write_mode';
-- The promoter refuses any Worker built from a commit containing this file until
-- article_tags exists (site/ops/brain_promote_release.py D1_REQUIRED_COLUMNS).

CREATE TABLE IF NOT EXISTS site_settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_by TEXT REFERENCES users(id),
  updated_at INTEGER NOT NULL
);

-- The brake, open. An admin closes every R1 write surface and every agent write
-- with one statement and no deploy:
--   UPDATE site_settings SET value = 'locked', updated_at = <now in ms> WHERE key = 'site_write_mode';
-- and re-opens it with value = 'open'. INSERT OR IGNORE, so re-applying this file
-- never re-opens a brake the maintainer closed.
INSERT OR IGNORE INTO site_settings (key, value, updated_at)
  VALUES ('site_write_mode', 'open', CAST(strftime('%s', 'now') AS INTEGER) * 1000);

CREATE TABLE IF NOT EXISTS user_agreements (
  user_id      TEXT PRIMARY KEY REFERENCES users(id),
  cc0_version  TEXT NOT NULL,
  accepted_at  INTEGER NOT NULL,
  -- When a Turnstile challenge cleared this account's first edit; NULL when no
  -- site key was configured at the time (the check is then skipped and logged).
  verified_at  INTEGER
);

CREATE TABLE IF NOT EXISTS article_tags (
  pair_key       TEXT PRIMARY KEY
                 CHECK (length(pair_key) = 64 AND pair_key NOT GLOB '*[^0-9a-f]*'),
  pageid         INTEGER NOT NULL CHECK (pageid > 0),
  passage_key    TEXT NOT NULL
                 CHECK (length(passage_key) = 64 AND passage_key NOT GLOB '*[^0-9a-f]*'),
  library        TEXT NOT NULL CHECK (length(library) BETWEEN 1 AND 128),
  repository     TEXT NOT NULL CHECK (length(repository) BETWEEN 1 AND 300),
  declaration    TEXT NOT NULL CHECK (length(declaration) BETWEEN 1 AND 240),
  relation       TEXT NOT NULL CHECK (relation IN
                   ('exactMatch', 'closeMatch', 'broadMatch', 'narrowMatch', 'hasPart', 'relatedMatch')),
  note           TEXT CHECK (note IS NULL OR length(note) <= 500),
  state          TEXT NOT NULL DEFAULT 'live' CHECK (state IN ('live', 'removed')),
  origin         TEXT NOT NULL DEFAULT 'community' CHECK (origin IN ('community')),
  -- The pipeline relation this row overrides, NULL for a location the pipeline
  -- never made. The card shows it ("the pipeline said broadMatch").
  base_relation  TEXT,
  added_by       TEXT NOT NULL REFERENCES users(id),
  actor_type     TEXT NOT NULL DEFAULT 'human' CHECK (actor_type IN ('human', 'agent')),
  via            TEXT NOT NULL DEFAULT 'web' CHECK (via IN ('web', 'api', 'intake')),
  -- Reserved for release R5's machine_tokens; an agent is identified by added_by
  -- (agent_tokens.agent_id), so it needs none.
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

-- The reader's one indexed read per article view.
CREATE INDEX IF NOT EXISTS idx_article_tags_page ON article_tags (pageid, state);
-- A person's own edits, for attribution and for the daily caps. The caps count
-- writes in a window, so they read updated_at/removed_at rather than created_at:
-- changing a row that was created yesterday is a write today.
CREATE INDEX IF NOT EXISTS idx_article_tags_author ON article_tags (added_by, created_at);
CREATE INDEX IF NOT EXISTS idx_article_tags_author_day ON article_tags (added_by, updated_at);
CREATE INDEX IF NOT EXISTS idx_article_tags_remover_day ON article_tags (removed_by, removed_at)
  WHERE removed_by IS NOT NULL;
-- The patrol feed R2/R3 build on, and the newest-first listing R1 already needs.
CREATE INDEX IF NOT EXISTS idx_article_tags_recent ON article_tags (state, created_at);
