-- better-auth tables (Phase 4). `users` already exists from 0002 with a
-- compatible column layout. Timestamps are stored as integer seconds.

CREATE TABLE sessions (
  id          TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL REFERENCES users(id),
  token       TEXT NOT NULL,
  expires_at  INTEGER NOT NULL,
  ip_address  TEXT,
  user_agent  TEXT,
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL
);
CREATE INDEX idx_sessions_user ON sessions (user_id);
CREATE UNIQUE INDEX idx_sessions_token ON sessions (token);

CREATE TABLE accounts (
  id                       TEXT PRIMARY KEY,
  user_id                  TEXT NOT NULL REFERENCES users(id),
  account_id               TEXT NOT NULL,
  provider_id              TEXT NOT NULL,
  access_token             TEXT,
  refresh_token            TEXT,
  id_token                 TEXT,
  access_token_expires_at  INTEGER,
  refresh_token_expires_at INTEGER,
  scope                    TEXT,
  password                 TEXT,
  created_at               INTEGER NOT NULL,
  updated_at               INTEGER NOT NULL
);
CREATE INDEX idx_accounts_user ON accounts (user_id);

CREATE TABLE verifications (
  id          TEXT PRIMARY KEY,
  identifier  TEXT NOT NULL,
  value       TEXT NOT NULL,
  expires_at  INTEGER NOT NULL,
  created_at  INTEGER,
  updated_at  INTEGER
);
CREATE INDEX idx_verifications_identifier ON verifications (identifier);
