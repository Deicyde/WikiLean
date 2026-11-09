-- The shared Brain serving database `wikilean-brain` (the D1 read-layer design, section 4).
-- This database is separate from `wikilean`: only site/ops/brain_d1_import.py (import, prune,
-- supersede) writes to it, and the Worker reads it through the BRAIN_DB binding.
--
-- Apply by hand, once, with --command carrying the text of this file, NEVER --file. On a remote
-- database --file goes through the import API, which makes the database unavailable to serve
-- queries while it runs. The Brain database has no entry in wiki/wrangler.jsonc yet (its id
-- comes from issue #150), so name a config that binds it:
--
--   cd wiki && npx wrangler d1 execute wikilean-brain --remote --config <brain config> \
--     --command "$(cat migrations-brain/0001_brain.sql)"
--
-- Then verify: SELECT name FROM pragma_table_info(<datasets, quoted>) lists dataset_id and state.
-- Every statement is CREATE ... IF NOT EXISTS, so a rerun changes nothing.
--
-- Rows belong to serving datasets, not to releases. A dataset is named by its dataset id, the
-- logical root of brain/data/serving-tables.json (the sidecar build_serving_db.py writes), and
-- each data row carries the small integer `ds` that `datasets` maps one-to-one to that id.
-- `releases` maps each release to one dataset, so releases with identical serving tables share
-- one copy. Every data table numbers its rows seq = 1..N in the key order of the builder, and the
-- importer writes, reads back and deletes by seq ranges, and the natural keys stay UNIQUE so a
-- retried range can never duplicate a row.
--
-- The data tables carry the columns of the builder (brain/build_serving_db.py SCHEMA) with two
-- omitted: name_keys.name (each name in its original case) and text_ids.body. Both are digested
-- in the local file and used only to fill the per-dataset FTS tables, which the importer creates
-- and fills from the local file and the pruner drops:
--   names_<hex>: fts5(name), contentless with contentless_delete=1 and the trigram tokenizer,
--                rowid = name_keys.seq (names_mode trigram), or fts5(parts) with prefix
--                indexes 2 and 3 over each name split into parts (names_mode parts)
--   texts_<hex>: fts5(body), contentless with contentless_delete=1, rowid = text_ids.seq
-- where <hex> is the 64 hex digits of the dataset id (site/ops/brain_d1_import.py has the
-- exact statements).
--
-- Schema rule: additive only. A later migration may add tables and nullable columns
-- (ALTER TABLE ... ADD COLUMN, which runs once and is verified after). It never creates an index
-- on a populated table, changes or drops a column in use, or backfills data: each of those is
-- one long statement on a shared database. A change that needs one adds a new table and a new
-- schema_version instead. The explorer tables of step 1b (explorer_edges, explorer_scopes) arrive
-- that way, in 0003_explorer.sql with schema_version 2 and the builder change that writes them.

CREATE TABLE IF NOT EXISTS datasets(
  ds INTEGER PRIMARY KEY AUTOINCREMENT,
  dataset_id TEXT NOT NULL UNIQUE,     -- logical_root of brain/data/serving-tables.json
  schema_version INTEGER NOT NULL,     -- from the sidecar
  tables_json TEXT NOT NULL,           -- the sidecar tables: {table: {rows, digest, delete_bounds}}
  longest_name INTEGER NOT NULL,       -- longest label or aka, UTF-16 units (bridge)
  sup_names_from INTEGER NOT NULL,     -- first name_keys.seq that names a supercell
  names_mode TEXT,                     -- trigram or parts, chosen by the importer
  state TEXT NOT NULL CHECK (state IN ('importing', 'ready', 'deleting')),
  imported_at TEXT);

CREATE TABLE IF NOT EXISTS releases(
  release_id TEXT PRIMARY KEY,         -- or scratch:<dataset id> for a scratch import
  seq INTEGER NOT NULL UNIQUE,         -- registration order, set again on re-registration
  ds INTEGER NOT NULL REFERENCES datasets(ds),
  manifest_sha256 TEXT NOT NULL,       -- information only: two manifests can share an id
  state TEXT NOT NULL CHECK (state IN ('candidate', 'superseded')),
  registered_at TEXT NOT NULL);

-- The journal of the importer: one row per verified seq range (tbl names a data table, or names /
-- texts for the FTS tables of the dataset). The pruner deletes these rows before the data.
CREATE TABLE IF NOT EXISTS import_ranges(
  ds INTEGER NOT NULL,
  tbl TEXT NOT NULL,
  lo INTEGER NOT NULL,
  hi INTEGER NOT NULL,
  digest TEXT NOT NULL,
  verified_at TEXT NOT NULL,
  UNIQUE(ds, tbl, lo));

CREATE TABLE IF NOT EXISTS cell_pages(
  ds INTEGER NOT NULL, seq INTEGER NOT NULL,
  id TEXT NOT NULL,
  json TEXT NOT NULL,                  -- the shard entry of the cell, byte for byte
  UNIQUE(ds, seq), UNIQUE(ds, id));

CREATE TABLE IF NOT EXISTS aliases(
  ds INTEGER NOT NULL, seq INTEGER NOT NULL,
  kind TEXT NOT NULL,                  -- organ, decl or slug
  key TEXT NOT NULL,
  owner TEXT NOT NULL,
  UNIQUE(ds, seq), UNIQUE(ds, kind, key));

CREATE TABLE IF NOT EXISTS labels(
  ds INTEGER NOT NULL, seq INTEGER NOT NULL,
  ord INTEGER NOT NULL,                -- the labels.json index, which filter cursors page on
  id TEXT NOT NULL,
  label TEXT,
  aka TEXT,                            -- U+001F-joined
  f INTEGER,
  p TEXT,
  libs TEXT,                           -- library roots of all its folders, U+001F-joined
  xy TEXT,                             -- build position, JSON text
  UNIQUE(ds, seq), UNIQUE(ds, ord), UNIQUE(ds, id));
CREATE INDEX IF NOT EXISTS labels_p ON labels(ds, p);

CREATE TABLE IF NOT EXISTS supercells(
  ds INTEGER NOT NULL, seq INTEGER NOT NULL,
  ord INTEGER NOT NULL,                -- file order: the sorted path: rows, then frontier: rows
  path TEXT NOT NULL,
  label TEXT,
  aka TEXT,
  parent TEXT,
  fa INTEGER,
  frontier INTEGER,
  n_cells INTEGER,                     -- direct cells
  n_sub INTEGER NOT NULL,              -- subtree cells, once per ancestor
  fhist TEXT NOT NULL,                 -- {"<f>|<libs>": cells} over the subtree
  kids TEXT,                           -- up to 64 children, the largest by n_sub
  syn TEXT,                            -- synapse rows of the folder, without traces
  json TEXT NOT NULL,
  UNIQUE(ds, seq), UNIQUE(ds, path), UNIQUE(ds, ord));
CREATE INDEX IF NOT EXISTS supercells_parent ON supercells(ds, parent, ord);

CREATE TABLE IF NOT EXISTS name_keys(
  ds INTEGER NOT NULL, seq INTEGER NOT NULL,
  name_lc TEXT NOT NULL,
  kind TEXT NOT NULL,                  -- label, aka (cells), slabel, spath, sorgan (supercells)
  owner TEXT NOT NULL,
  len16 INTEGER NOT NULL,              -- length of name_lc in UTF-16 units
  -- serving.sqlite3 also has name TEXT NOT NULL (original case, for the FTS fill), D1 omits it
  UNIQUE(ds, seq), UNIQUE(ds, name_lc, kind, owner));

CREATE TABLE IF NOT EXISTS hot_prefixes(
  ds INTEGER NOT NULL, seq INTEGER NOT NULL,
  prefix TEXT NOT NULL,
  cells TEXT NOT NULL,                 -- JSON arrays of up to 100 name_keys.seq each
  sups TEXT NOT NULL,
  live_sups TEXT NOT NULL,             -- sups minus the empty folders the page search drops
  UNIQUE(ds, seq), UNIQUE(ds, prefix));

CREATE TABLE IF NOT EXISTS cell_under(
  ds INTEGER NOT NULL, seq INTEGER NOT NULL,
  anc TEXT NOT NULL,
  ord INTEGER NOT NULL,
  UNIQUE(ds, seq), UNIQUE(ds, anc, ord));

CREATE TABLE IF NOT EXISTS synapse_traces(
  ds INTEGER NOT NULL, seq INTEGER NOT NULL,
  pair TEXT NOT NULL,                  -- src|dst with src < dst
  json TEXT NOT NULL,
  UNIQUE(ds, seq), UNIQUE(ds, pair));

CREATE TABLE IF NOT EXISTS xrefs(
  ds INTEGER NOT NULL, seq INTEGER NOT NULL,
  page_ord INTEGER NOT NULL,
  node_ord INTEGER NOT NULL,
  page TEXT NOT NULL,
  node TEXT NOT NULL,
  UNIQUE(ds, seq), UNIQUE(ds, page_ord, node_ord));
CREATE INDEX IF NOT EXISTS xrefs_node ON xrefs(ds, node, page_ord);  -- xrefPages, no sort
CREATE INDEX IF NOT EXISTS xrefs_page ON xrefs(ds, page, node_ord);

CREATE TABLE IF NOT EXISTS text_ids(
  ds INTEGER NOT NULL, seq INTEGER NOT NULL,
  node TEXT NOT NULL,
  owner TEXT NOT NULL,
  -- serving.sqlite3 also has body TEXT NOT NULL (label, docstring and description, for the
  -- FTS fill), D1 omits it
  UNIQUE(ds, seq), UNIQUE(ds, node));
