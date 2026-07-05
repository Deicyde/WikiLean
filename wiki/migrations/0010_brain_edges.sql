-- Community brain edges (docs/BRAIN-EDITS-ROADMAP.md, Project 2): logged-in users
-- and API callers add connections to the Brain. Every edge is live-on-create with
-- an "added by" attribution and a human/AI label (actor_type). Correction is by
-- SOFT delete — an edge is never removed; a delete flips status='deleted' and
-- stamps the gravestone (deleted_by/deleted_at), so we always see who added AND
-- who removed each edge.
--
-- Node ids are durable brain ids (Q…, decl:Lib:Name, path:…, lit:…). dst may be an
-- external "xref:<db>:<value>" for a cross-database link — the high-value case,
-- since two nodes sharing one external page let the Brain infer new connections.
-- This table is the LIVE tail; the nightly build folds live (non-deleted) edges
-- into the static base (community_edges.jsonl) and can never drop a live edge or
-- resurrect a gravestone.
--
-- NB deploy: apply remote via
--   wrangler d1 execute wikilean --remote --file=migrations/0010_brain_edges.sql
-- NOT `d1 migrations apply` (remote migration tracking is out of sync; see the
-- 0008/0009 precedent).
CREATE TABLE IF NOT EXISTS brain_edges (
  id          TEXT PRIMARY KEY,             -- 12-hex, server-minted
  src         TEXT NOT NULL,                -- existing brain node id (shard-validated)
  dst         TEXT NOT NULL,                -- brain node id, or xref:<db>:<value>
  kind        TEXT NOT NULL,                -- relates|xref|formalizes|mentions|matches|cites
  evidence    TEXT NOT NULL,                -- JSON {note, ...}
  added_by    TEXT NOT NULL,                -- users.id (or 'pipeline' for a bearer token)
  actor_type  TEXT NOT NULL,                -- 'human' | 'ai'
  status      TEXT NOT NULL DEFAULT 'live', -- 'live' | 'deleted' (gravestone)
  created_at  INTEGER NOT NULL,             -- ms
  deleted_by  TEXT,                         -- users.id who deleted it (nullable)
  deleted_at  INTEGER,                      -- ms (nullable)
  version     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_brain_edges_src ON brain_edges (src, status);
CREATE INDEX IF NOT EXISTS idx_brain_edges_dst ON brain_edges (dst, status);
CREATE INDEX IF NOT EXISTS idx_brain_edges_created ON brain_edges (status, created_at);
CREATE INDEX IF NOT EXISTS idx_brain_edges_added_by ON brain_edges (added_by);
-- At most one LIVE edge per (src, dst, kind); a re-add after deletion is a new
-- row, so the gravestone history is preserved.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_brain_edges_live
  ON brain_edges (src, dst, kind) WHERE status = 'live';
