-- Per-user GitHub repo registration (the /repos page): a logged-in user picks
-- which of their public Lean repos the ops nightly should index, plus the Lean
-- library name to build. (user_id, owner, repo) is the natural key; `enabled`
-- toggles a repo without deleting the row (API callers may keep disabled rows).
-- GET /api/repos/enabled aggregates enabled rows (distinct owner/repo) for the
-- nightly. Validation lives in the Worker (src/repos.ts): owner/repo match
-- ^[A-Za-z0-9_.-]{1,100}$ (owner must not start with '.'), lib matches
-- ^[A-Z][A-Za-z0-9_]{0,63}$.
--
-- NB deploy: remote migration tracking is out of sync — apply remote via
--   wrangler d1 execute wikilean --remote --file=migrations/0012_user_repos.sql
CREATE TABLE IF NOT EXISTS user_repos (
  user_id    TEXT NOT NULL REFERENCES users(id),
  owner      TEXT NOT NULL,               -- GitHub account (user/org) login
  repo       TEXT NOT NULL,               -- repository name
  lib        TEXT NOT NULL,               -- Lean library name (default: repo CamelCased)
  enabled    INTEGER NOT NULL DEFAULT 1,  -- 0 = registered but not indexed
  created_at INTEGER NOT NULL,            -- ms
  PRIMARY KEY (user_id, owner, repo)
);
CREATE INDEX IF NOT EXISTS idx_user_repos_user ON user_repos (user_id);
