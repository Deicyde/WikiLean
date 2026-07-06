-- Community-added brain nodes (docs/BRAIN-EDITS-ROADMAP.md): a logged-in user or
-- API caller may introduce a NEW concept node — but ONLY a validated Wikidata
-- item (the QID must resolve on Wikidata). This is the constrained, low-risk form
-- of "new nodes": a QID either exists or it doesn't, so there's no free-form junk.
-- Enables the @[wikidata]-style workflow of tagging a Mathlib decl with a QID the
-- brain hasn't ingested yet (e.g. Q5530428 "Gelfand–Naimark–Segal construction").
--
-- Only holds QIDs NOT already in the static base; a QID that IS a static concept
-- node is referenced directly. Same live/gravestone + attribution model as
-- brain_edges. The nightly harvest can later promote these into the static graph.
--
-- NB deploy: apply remote via
--   wrangler d1 execute wikilean --remote --file=migrations/0011_brain_nodes.sql
CREATE TABLE IF NOT EXISTS brain_nodes (
  id          TEXT PRIMARY KEY,             -- the Wikidata QID (a durable node id)
  label       TEXT NOT NULL,                -- Wikidata label (en)
  description TEXT,                          -- Wikidata description (en)
  node_type   TEXT NOT NULL DEFAULT 'concept',
  added_by    TEXT NOT NULL,                -- users.id (or 'pipeline' bearer)
  actor_type  TEXT NOT NULL,                -- 'human' | 'ai'
  status      TEXT NOT NULL DEFAULT 'live', -- 'live' | 'deleted'
  created_at  INTEGER NOT NULL,             -- ms
  deleted_by  TEXT,
  deleted_at  INTEGER,
  version     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_brain_nodes_created ON brain_nodes (status, created_at);
CREATE INDEX IF NOT EXISTS idx_brain_nodes_added_by ON brain_nodes (added_by);
