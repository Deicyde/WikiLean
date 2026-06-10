-- WikiLean live-wiki schema (Phase 2). Auth tables (users/sessions/accounts)
-- are added by better-auth in Phase 4.

CREATE TABLE articles (
  slug            TEXT PRIMARY KEY,
  wikipedia_title TEXT NOT NULL,
  display_title   TEXT NOT NULL,
  wikidata_qid    TEXT,
  revid           INTEGER,
  annotations     TEXT NOT NULL,
  version         INTEGER NOT NULL DEFAULT 1,
  created_at      INTEGER NOT NULL,
  updated_at      INTEGER NOT NULL
);

CREATE TABLE revisions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  slug        TEXT NOT NULL REFERENCES articles(slug),
  user_id     TEXT,
  annotations TEXT NOT NULL,
  comment     TEXT,
  created_at  INTEGER NOT NULL
);

CREATE INDEX idx_revisions_slug ON revisions (slug, created_at);
