-- Users table (Phase 5). Field layout matches better-auth's `user` model so
-- Phase 4 can point better-auth at this table (modelName "users") + a `role`
-- additional field, rather than introducing a second user table.

CREATE TABLE users (
  id             TEXT PRIMARY KEY,
  name           TEXT,
  email          TEXT,
  email_verified INTEGER NOT NULL DEFAULT 0,
  image          TEXT,
  role           TEXT NOT NULL DEFAULT 'user',
  created_at     INTEGER NOT NULL,
  updated_at     INTEGER NOT NULL
);
