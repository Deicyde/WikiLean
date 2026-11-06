-- Agent contributions (docs/AGENT-CONTRIBUTIONS.md): a signed-in person mints an
-- API token for an AI agent at /agents. Each token belongs to its own users row
-- (role 'agent', named by the person) and writes as that agent: annotations and
-- Brain edits labelled AI, human annotations protected exactly as for the
-- pipeline bearer, changes to them filed as proposals.
--
-- One token per agent. Only the SHA-256 of the token is stored; the token itself
-- is shown once, at minting. owner_id is the person accountable for the agent:
-- blocking the owner (users.role 'blocked') disables every agent they minted,
-- and blocking the agent's own users row disables just that agent. revoked_at
-- retires the token for good; the agent's users row stays so its edits keep
-- their attribution.
--
-- Idempotent. Apply it before promoting any Worker built from a commit that
-- contains this file: that Worker reads agent_tokens for every agent-token
-- request, every profile page and /agents.
-- The promoter refuses such a commit until the table exists.
--
-- NB deploy: remote migration tracking is out of sync. After a D1 backup
-- (`npm run backup:d1`, see docs/BACKUP.md), apply remote once via
--   wrangler d1 execute wikilean --remote --file=migrations/0017_agent_tokens.sql
-- and verify:
--   SELECT name FROM pragma_table_info('agent_tokens');
CREATE TABLE IF NOT EXISTS agent_tokens (
  agent_id     TEXT PRIMARY KEY REFERENCES users(id),
  owner_id     TEXT NOT NULL REFERENCES users(id),
  token_hash   TEXT NOT NULL UNIQUE,
  created_at   INTEGER NOT NULL,
  last_used_at INTEGER,
  revoked_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_agent_tokens_owner ON agent_tokens (owner_id);
