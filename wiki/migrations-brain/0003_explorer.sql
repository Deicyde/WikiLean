-- The explorer tables of serving schema_version 2 (the D1 read-layer design, section 4, Explorer
-- per scope): the explorer graph edges by cell id, and the payload of every scope too big for the
-- Worker to compute live, in parts of at most 1 MB.
--
-- Apply by hand, once, after 0001_brain.sql and 0002_name_keys_exact.sql and before the first
-- schema_version 2 import (the importer, site/ops/brain_d1_import.py, refuses to import while a
-- table, key or index here is missing), with --command carrying the text of this file,
-- NEVER --file (see 0001_brain.sql for why):
--
--   cd wiki && npx wrangler d1 execute wikilean-brain --remote --config <brain config> \
--     --command "$(cat migrations-brain/0003_explorer.sql)"
--
-- Then verify: SELECT name FROM pragma_table_info(<explorer_scopes, quoted>) lists scope, part
-- and json, and SELECT name FROM pragma_index_list(<explorer_edges, quoted>) lists
-- explorer_edges_b. Every statement is CREATE ... IF NOT EXISTS, so a rerun changes nothing.
--
-- It keeps the schema rule of 0001_brain.sql: it only adds tables, and its one index sits on a
-- table it creates empty. A database whose explorer_edges already holds rows without that index
-- cannot get it this way: prune every schema_version 2 dataset first and apply it then.
--
-- explorer_edges: one row per edge of cells/explorer.json, a and b the cell ids in the synapse
-- order, seq in (a, b) order. The Worker walks the UNIQUE key from a and explorer_edges_b from b.
-- explorer_scopes: part 0 carries the part count. A path scope with no part 0 is computed live by
-- the Worker from labels and explorer_edges, and the scope all always has rows.

CREATE TABLE IF NOT EXISTS explorer_edges(
  ds INTEGER NOT NULL, seq INTEGER NOT NULL,
  a TEXT NOT NULL, b TEXT NOT NULL, w INTEGER NOT NULL,
  UNIQUE(ds, seq), UNIQUE(ds, a, b));
CREATE INDEX IF NOT EXISTS explorer_edges_b ON explorer_edges(ds, b);
CREATE TABLE IF NOT EXISTS explorer_scopes(
  ds INTEGER NOT NULL, seq INTEGER NOT NULL,
  scope TEXT NOT NULL, part INTEGER NOT NULL,
  json TEXT NOT NULL,   -- one part of the scope payload, at most 1 MB
  UNIQUE(ds, seq), UNIQUE(ds, scope, part));
