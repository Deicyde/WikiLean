// D1Store: the BrainStore over the shared serving database `wikilean-brain`
// (docs/BRAIN-D1.md section 1, step 3b). storeFor hands one out for a release
// whose release.json lists the serving-tables sidecar, when BRAIN_DB is bound.
//
// Every statement is scoped to the release's serving dataset through
//   $DS = (SELECT ds FROM datasets WHERE dataset_id = ?1 AND state = 'ready')
// so a dataset that is importing, being deleted or absent matches nothing.
// ?1 is always the dataset id from the release's signed manifest; every other
// value is a bound parameter. The only text pasted into SQL is the FTS table
// name names_<hex>, where <hex> is the dataset id's 64 hex digits, checked
// again where it is pasted. Each statement builder below is exported so the
// query-plan test can run EXPLAIN QUERY PLAN over it, and each carries its
// result bound.
//
// Readiness: the first batch a store sends starts with the readiness check;
// once it has passed, a read route's later batches omit it (a request's
// dataset can leave `ready` only through two promotions and a finished prune,
// doc section 5), and a failure is kept, so later calls rethrow it with no
// query. A write route (POST node, POST edge, quickstatements) asks for the
// check in every batch (checkEveryBatch), because there an empty result that
// should have been a 503 reads as "not a node" and mints a community node.
// Batches that start together before any has passed each carry the check. A
// missing row, a schema_version this Worker cannot read, an unknown
// names_mode or a D1 exception throws BrainReleaseUnavailableError(release,
// "BRAIN_DB", reason), which every route answers with a 503. A real miss after
// a passing check is null, an absent key or an empty list.
//
// The per-dataset constants (names_mode, longest_name, sup_names_from) are
// memoized per isolate by dataset id. names_mode is the importer's choice, so
// a dataset pruned and imported again can come back with another one: every
// check compares the row with the constants its batch was built from, and a
// difference refreshes the memo and answers 503 for that request. Rows are
// kept for the rest of the request only (cell pages, supercells and organ
// owners), so the routes' repeated reads of one atom cost one statement.
//
// Keep this module's top level free of brain.ts value uses, as
// brain-store-static.ts does: brain.ts imports brain-api.ts, which imports
// brain-store.ts and so this module.
import type { Context } from "hono";
import type { Env } from "./env.js";
import {
  BrainReleaseUnavailableError,
  brainManifestMeta,
  type BrainLabelRow,
  type BrainReleaseContext,
} from "./brain.js";
import { nameParts } from "./brain-name-parts.js";
import type {
  AtomHead,
  AtomKeyQuery,
  BrainStore,
  BrainStoreBatches,
  BridgeCandidatesResult,
  CellEntry,
  Crumb,
  FilterPage,
  KeyHits,
  KeyLookup,
  SearchPageResult,
  SupercellEntry,
  SupercellHead,
  Synapse,
  SynapseTraces,
  Trace,
} from "./brain-store.js";

type Ctx = Context<{ Bindings: Env }>;
type Row = Record<string, unknown>;

/** One statement: its SQL and the values bound to ?2, ?3, ... (?1 is always
 *  the dataset id). Every statement references its highest parameter, since
 *  SQLite refuses a value bound past it. */
export interface Stmt {
  sql: string;
  params: unknown[];
}

export const DS = "(SELECT ds FROM datasets WHERE dataset_id = ?1 AND state = 'ready')";
const SUP_NAMES_FROM = "(SELECT sup_names_from FROM datasets WHERE dataset_id = ?1 AND state = 'ready')";
export const READINESS_SQL =
  "SELECT schema_version, names_mode, longest_name, sup_names_from FROM datasets " +
  "WHERE dataset_id = ?1 AND state = 'ready'";
// The schema versions this Worker reads (brain/build_serving_db.py SCHEMA_VERSION).
export const SUPPORTED_SCHEMA_VERSIONS: ReadonlySet<number> = new Set([1]);
const HEX_RE = /^[0-9a-f]{64}$/;
const AKA_SEP = "\u001f";
const MAX_SEQ = Number.MAX_SAFE_INTEGER;

// Search (doc section 1, Search). The builder lists every prefix of 2 to 200
// UTF-16 units that starts more than 1,000 names in hot_prefixes, so a prefix
// range over any other prefix in that span holds at most 1,000 rows.
const QUERY_MIN = 2;
const QUERY_MAX = 200;
const FRAGMENT_MIN = 3; // the trigram index matches nothing shorter
const EXACT_ROWS = 200;
const PREFIX_ROWS = 1000; // build_serving_db.HOT_PREFIX_NAMES
const FRAGMENT_ROWS_PER_HIT = 20;
const FRAGMENT_ROWS_CAP = 2000;

// The bounded statement pass (doc section 1, Bridge candidates).
const STMT_SCAN_UNITS = 2000;
const STMT_CHUNK = 2000;
// and at most this many bytes of JSON in a chunk's one bound value (D1 takes at
// most 2 MB per value; 2,000 long non-ASCII candidates come close)
const STMT_CHUNK_BYTES = 512 * 1024;
const STMT_CHUNKS = 4;
const STMT_MIN_LABEL = 4; // the static pass's MIN_STMT_LABEL
const STMT_HITS = 5;

// Input caps (brain-store.ts's brackets). A longer input is a caller bug.
const LABEL_ROWS_CAP = 500;
const TRACE_ROWS_CAP = 200;
const XREF_PAGES_CAP = 40;
const PREFETCH_CHUNK = 50; // keys per prefetch batch
// The prefetch keeps whole pages (cell pages and supercell entries) for the
// rest of the request, at most PREFETCH_KEPT_CHARS characters (SQLite's
// length) of them in all, every one read before the first write. A batch of
// keys asks for pages of at most min(PREFETCH_PAGE_CHARS, what the budget has
// left / its keys) characters, so no batch passes the budget; a longer page
// comes back as its length alone and is read afterwards, PREFETCH_ALONE_BATCH
// pages a batch, at most PREFETCH_ALONE_PAGES of them. A submit whose pages
// pass either bound gets PrefetchBudgetError, before anything is written.
const PREFETCH_PAGE_CHARS = 128 * 1024;
const PREFETCH_KEPT_CHARS = 8 * 1024 * 1024;
const PREFETCH_ALONE_BATCH = 4;
const PREFETCH_ALONE_PAGES = 64;
const TRACES_PER_ROW = 6; // build_cell_shards.SHARD_TRACE_CAP

// Rows a filter call examines at most (decision 7). Tests lower it.
const FILTER_EXAMINED = 10000;
let _filterExamined = FILTER_EXAMINED;
/** test-only: lower the filter examination cap (null restores 10,000) */
export function _setFilterExaminedForTests(n: number | null): void {
  _filterExamined = n ?? FILTER_EXAMINED;
}

export interface DatasetConstants {
  namesMode: "trigram" | "parts";
  longestName: number;
  supNamesFrom: number;
}

// Per isolate, by dataset id. names_mode can change when a dataset is pruned
// and imported again, so every check compares the row with the memo and
// refreshes it (see the header). FIFO of 4: a Worker serves at most a current
// and a previous release.
const _constants = new Map<string, DatasetConstants>();
const MAX_MEMOIZED_DATASETS = 4;
/** test-only */
export function _resetD1StoreMemo(): void {
  _constants.clear();
}

function rememberConstants(datasetId: string, value: DatasetConstants): void {
  _constants.delete(datasetId);
  _constants.set(datasetId, value);
  while (_constants.size > MAX_MEMOIZED_DATASETS) {
    const oldest = _constants.keys().next().value as string | undefined;
    if (oldest === undefined) break;
    _constants.delete(oldest);
  }
}

/** prefetchAtoms' refusal: the submit names more page text than one request
 *  keeps (PREFETCH_KEPT_CHARS) or more pages over the batch caps than it reads
 *  alone (PREFETCH_ALONE_PAGES). Thrown before the route writes anything;
 *  quickstatements answers 413. */
export class PrefetchBudgetError extends Error {
  constructor(reason: string) {
    super(reason);
    this.name = "PrefetchBudgetError";
  }
}

function ids(values: readonly string[], cap: number, what: string): string {
  if (values.length > cap) throw new Error(`D1Store: ${values.length} ${what} where at most ${cap} belong`);
  return JSON.stringify(values);
}

// The FTS table name: a fixed prefix and the checked 64 hex digits, nothing else.
export function namesTable(hex: string): string {
  if (!HEX_RE.test(hex)) throw new Error("D1Store: refusing a names table for a malformed dataset id");
  return `names_${hex}`;
}

// SQLite compares TEXT by code point; JavaScript's < by UTF-16 unit. The trace
// table's pair keys are min|max by Python's (code point) order.
function codePointLess(a: string, b: string): boolean {
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) {
    const x = a.codePointAt(i)!;
    const y = b.codePointAt(i)!;
    if (x !== y) return x < y;
    if (x > 0xffff) i++;
  }
  return a.length < b.length;
}

export function pairKey(a: string, b: string): string {
  return codePointLess(b, a) ? `${b}|${a}` : `${a}|${b}`;
}

// ---- statement builders -----------------------------------------------------------

/** [1 row] */
export function cellPageStmt(id: string): Stmt {
  return { sql: `SELECT json FROM cell_pages WHERE ds = ${DS} AND id = ?2`, params: [id] };
}

/** [50 rows]: chars is the page's length; json is null for a page longer
 *  than maxChars characters. */
export function cellPagesStmt(list: readonly string[], maxChars = PREFETCH_PAGE_CHARS): Stmt {
  return {
    sql:
      `SELECT id, length(json) AS chars, CASE WHEN length(json) <= ?3 THEN json END AS json FROM cell_pages ` +
      `WHERE ds = ${DS} AND id IN (SELECT value FROM json_each(?2))`,
    params: [ids(list, PREFETCH_CHUNK, "cell ids"), maxChars],
  };
}

/** [1 row] */
export function supercellStmt(path: string): Stmt {
  return { sql: `SELECT json FROM supercells WHERE ds = ${DS} AND path = ?2`, params: [path] };
}

/** [50 rows]: chars is the entry's length; json is null for an entry longer
 *  than maxChars characters. */
export function supercellsStmt(paths: readonly string[], maxChars = PREFETCH_PAGE_CHARS): Stmt {
  return {
    sql:
      `SELECT path, length(json) AS chars, CASE WHEN length(json) <= ?3 THEN json END AS json FROM supercells ` +
      `WHERE ds = ${DS} AND path IN (SELECT value FROM json_each(?2))`,
    params: [ids(paths, PREFETCH_CHUNK, "paths"), maxChars],
  };
}

/** [1 row]: whether an atom id exists, a supercell path or a cell page. */
export function atomExistsStmt(atomId: string): Stmt {
  return atomId.startsWith("path:")
    ? { sql: `SELECT path FROM supercells WHERE ds = ${DS} AND path = ?2`, params: [atomId] }
    : { sql: `SELECT id FROM cell_pages WHERE ds = ${DS} AND id = ?2`, params: [atomId] };
}

/** [64 rows]: the ancestors of `path`, root first. A depth cap replaces the
 *  static walk's seen-set; on a tree the two agree. */
export function breadcrumbStmt(path: string): Stmt {
  return {
    sql:
      `WITH RECURSIVE up(path, d) AS (` +
      `SELECT parent, 1 FROM supercells WHERE ds = ${DS} AND path = ?2 ` +
      `UNION ALL SELECT s.parent, up.d + 1 FROM up JOIN supercells s ON s.ds = ${DS} AND s.path = up.path ` +
      `WHERE up.d < 64) ` +
      `SELECT up.path AS path, s.label AS label FROM up LEFT JOIN supercells s ON s.ds = ${DS} AND s.path = up.path ` +
      `WHERE up.path IS NOT NULL AND up.path <> '' ORDER BY up.d DESC`,
    params: [path],
  };
}

/** [50 x 64 rows]: breadcrumbStmt for each of `paths` in one statement, each
 *  row tagged with its start path and depth; the caller orders each walk. */
export function breadcrumbsStmt(paths: readonly string[]): Stmt {
  return {
    sql:
      `WITH RECURSIVE up(start, path, d) AS (` +
      `SELECT path, parent, 1 FROM supercells WHERE ds = ${DS} AND path IN (SELECT value FROM json_each(?2)) ` +
      `UNION ALL SELECT up.start, s.parent, up.d + 1 FROM up JOIN supercells s ON s.ds = ${DS} AND s.path = up.path ` +
      `WHERE up.d < 64) ` +
      `SELECT up.start AS start, up.path AS path, s.label AS label, up.d AS d FROM up ` +
      `LEFT JOIN supercells s ON s.ds = ${DS} AND s.path = up.path WHERE up.path IS NOT NULL AND up.path <> ''`,
    params: [ids(paths, PREFETCH_CHUNK, "paths")],
  };
}

/** [3 + 2 rows returned]: the alias lookups (organ, decl, slug) and the
 *  exact-name lookup in one statement. The name lookup takes the first cell
 *  name in seq order (cells come before every supercell name, in labels order,
 *  which is the static walk's order; name_keys_exact bounds the read by its
 *  LIMIT) and the first supercell organ label. That branch reads every sorgan
 *  row of the two names on the UNIQUE (ds, name_lc, kind, owner) key, one per
 *  supercell with an organ of that label: a data bound, not a LIMIT. It names
 *  that key's index (sqlite_autoindex_name_keys_2: SQLite numbers a table's
 *  UNIQUE constraints in order, and (ds, seq) is the first), because for
 *  min(seq) the planner prefers name_keys_exact, which would read the name's
 *  rows of every kind. Reading name_keys_exact in seq order from
 *  sup_names_from instead would stop at the first sorgan row, but would pass
 *  every slabel and spath row of the name first (folder names such as Basic
 *  repeat across many supercells), which is the larger set. null when no
 *  lookup is asked. */
export function keyHitsStmt(k: KeyLookup): Stmt | null {
  const parts: string[] = [];
  const params: unknown[] = [];
  const bind = (value: unknown): string => {
    params.push(value);
    return `?${params.length + 1}`;
  };
  for (const kind of ["organ", "decl", "slug"] as const) {
    const key = k[kind];
    if (key === undefined) continue;
    parts.push(
      `SELECT '${kind}' AS hit, owner, 0 AS seq FROM aliases WHERE ds = ${DS} AND kind = '${kind}' AND key = ${bind(key)}`,
    );
  }
  if (k.name) {
    const keyLc = bind(k.name.keyLc);
    const slugLc = bind(k.name.slugLc);
    parts.push(
      `SELECT 'cell' AS hit, owner, seq FROM (SELECT owner, seq FROM name_keys INDEXED BY name_keys_exact WHERE ds = ${DS} ` +
      `AND name_lc = ${keyLc} AND seq < ${SUP_NAMES_FROM} ORDER BY seq LIMIT 1)`,
    );
    parts.push(
      `SELECT 'sorgan' AS hit, owner, seq FROM (SELECT owner, min(seq) AS seq FROM name_keys ` +
      `INDEXED BY sqlite_autoindex_name_keys_2 WHERE ds = ${DS} AND kind = 'sorgan' AND name_lc IN (${keyLc}, ${slugLc}))`,
    );
  }
  return parts.length ? { sql: parts.join(" UNION ALL "), params } : null;
}

/** [500 rows]: label rows of cells, then heads of supercells. */
export function labelRowsStmts(list: readonly string[]): [Stmt, Stmt] {
  const json = ids(list, LABEL_ROWS_CAP, "ids");
  return [
    {
      sql: `SELECT id, label, aka, f, p FROM labels WHERE ds = ${DS} AND id IN (SELECT value FROM json_each(?2))`,
      params: [json],
    },
    {
      sql: `SELECT path, label, aka, n_cells FROM supercells WHERE ds = ${DS} AND path IN (SELECT value FROM json_each(?2))`,
      params: [json],
    },
  ];
}

/** [200 rows]: names equal to q, in seq order. name_keys_exact (migration
 *  0002) reads them in seq order, so the LIMIT bounds the read; the statement
 *  names the index, so a database without it fails (503) rather than sort
 *  every row that shares the name. */
export function exactTierStmt(qlc: string, lo: number, hi: number): Stmt {
  return {
    sql:
      `SELECT seq, owner FROM name_keys INDEXED BY name_keys_exact WHERE ds = ${DS} AND name_lc = ?2 AND seq >= ?3 AND seq < ?4 ` +
      `ORDER BY seq LIMIT ${EXACT_ROWS}`,
    params: [qlc, lo, hi],
  };
}

/** [100 + 100 rows]: a hot prefix's listed owners (each owner's first name
 *  seq). The agent search reads `sups`; the page's `live_sups` is PR 2's.
 *  CROSS JOIN keeps the listed seqs outer, so each one probes name_keys by
 *  (ds, seq); a plain join let the planner walk the dataset's names instead. */
export function hotTierStmt(qlc: string, cells: boolean, sups: boolean): Stmt {
  const branch = (list: string) =>
    `SELECT k.seq AS seq, k.owner AS owner FROM hot_prefixes h CROSS JOIN json_each(h.${list}) j ` +
    `CROSS JOIN name_keys k WHERE h.ds = ${DS} AND h.prefix = ?2 AND k.ds = h.ds AND k.seq = j.value`;
  const lists = [...(cells ? ["cells"] : []), ...(sups ? ["sups"] : [])];
  return { sql: lists.map(branch).join(" UNION ALL "), params: [qlc] };
}

/** [1,001 rows]: names that start with q (and are longer), when q is not hot.
 *  The outer one-row source runs first (CROSS JOIN keeps the order), so a hot
 *  q reads no name row at all; for any other q the builder guarantees at most
 *  1,000 rows, and a 1,001st is a builder disagreement. Kinds are filtered
 *  in JavaScript, so the LIMIT bounds the rows read. */
export function rangeTierStmt(qlc: string): Stmt {
  return {
    sql:
      `SELECT k.seq AS seq, k.owner AS owner FROM ` +
      `(SELECT 1 AS one WHERE NOT EXISTS (SELECT 1 FROM hot_prefixes WHERE ds = ${DS} AND prefix = ?2)) g ` +
      `CROSS JOIN name_keys k WHERE k.ds = ${DS} AND k.name_lc > ?2 AND k.name_lc < ?2 || char(1114111) ` +
      `LIMIT ${PREFIX_ROWS + 1}`,
    params: [qlc],
  };
}

/** [k rows]: names containing the fragment, in seq order, from the dataset's
 *  names_<hex> FTS table (rowid = name_keys.seq). CROSS JOIN keeps the k
 *  matches outer, so each probes name_keys by (ds, seq). In parts mode
 *  fragmentMatch never asks for a 1-unit prefix, which the table's
 *  prefix='2 3' indexes do not serve: FTS5 would merge the doclist of every
 *  term that starts with it before the rowid range and the LIMIT apply.
 *  (A prefix of 4 or more units merges only the terms that start with it; it
 *  is on step 0's measurement list.) */
export function fragmentTierStmt(hex: string, match: string, lo: number, hi: number, k: number): Stmt {
  const table = namesTable(hex);
  return {
    sql:
      `SELECT k.seq AS seq, k.owner AS owner FROM (SELECT rowid AS seq FROM ${table} WHERE ${table} MATCH ?2 ` +
      `AND rowid >= ?3 AND rowid < ?4 ORDER BY rowid LIMIT ?5) f CROSS JOIN name_keys k WHERE k.ds = ${DS} AND k.seq = f.seq`,
    params: [match, lo, hi, k],
  };
}

/** The MATCH string for a fragment: one quoted phrase. In parts mode the query
 *  is split as the importer split the names (nameParts), and its last token
 *  (the last run of letters and digits, which is what the tokenizer makes of
 *  the end of the phrase) is a prefix when it is at least 2 UTF-16 units long.
 *  A 1-unit last token matches only as a whole token, so "Nat.s" finds the
 *  names with the parts "nat s", not "Nat.succ" (the prefix tier still finds
 *  the names that start with "nat.s"). A query with no letter or digit in it
 *  has no parts mode match. */
export function fragmentMatch(q: string, namesMode: "trigram" | "parts"): string | null {
  if (namesMode === "trigram") return `"${q.replace(/"/g, '""')}"`;
  const parts = nameParts(q);
  const last = /[\p{L}\p{N}]+(?=[^\p{L}\p{N}]*$)/u.exec(parts);
  if (!last) return null;
  return `"${parts.replace(/"/g, '""')}"${last[0].length >= 2 ? "*" : ""}`;
}

const utf8 = new TextEncoder();

/** Where the statement chunk that starts at `from` ends: after at most 2,000
 *  candidates, and before its JSON (the one bound value) passes 512 KB. */
export function statementChunkEnd(candidates: readonly string[], from: number): number {
  let bytes = 2; // the brackets
  let end = from;
  while (end < candidates.length && end - from < STMT_CHUNK) {
    const add = utf8.encode(JSON.stringify(candidates[end])).length + (end > from ? 1 : 0);
    if (end > from && bytes + add > STMT_CHUNK_BYTES) break;
    bytes += add;
    end += 1;
  }
  return end;
}

/** [5 rows]: the atoms one chunk of statement candidates names, each with its
 *  longest matching name. CROSS JOIN fixes the order: each of the at most
 *  2,000 candidates, then each of the two cell kinds, probes the UNIQUE
 *  (ds, name_lc, kind, owner) key (named, as in keyHitsStmt) on all three
 *  columns, so the GROUP BY and ORDER BY read only the label and aka rows that
 *  match a candidate. Left to the planner, sqlite_stat1 rows (ANALYZE or
 *  PRAGMA optimize) switch an IN form to name_keys_exact, which reads every
 *  kind's rows of each candidate. */
export function statementChunkStmt(candidates: readonly string[]): Stmt {
  return {
    sql:
      `SELECT owner, max(len16) AS len FROM json_each(?2) j ` +
      `CROSS JOIN (SELECT 'label' AS kind UNION ALL SELECT 'aka') t ` +
      `CROSS JOIN name_keys k INDEXED BY sqlite_autoindex_name_keys_2 ` +
      `WHERE k.ds = ${DS} AND k.name_lc = j.value AND k.kind = t.kind ` +
      `GROUP BY owner ORDER BY len DESC, owner LIMIT ${STMT_HITS}`,
    params: [ids(candidates, STMT_CHUNK, "candidates")],
  };
}

/** [200 rows] */
export function tracesStmt(pairs: readonly string[]): Stmt {
  return {
    sql: `SELECT pair, json FROM synapse_traces WHERE ds = ${DS} AND pair IN (SELECT value FROM json_each(?2))`,
    params: [ids(pairs, TRACE_ROWS_CAP, "pairs")],
  };
}

/** [examined rows; limit + 1 returned] plus the probe that says whether the
 *  pool goes on past the examined window. Under a folder, CROSS JOIN keeps
 *  cell_under's ord order outer, so the outer LIMIT stops the read once
 *  limit + 1 rows match, with no sort; at most `examined` rows otherwise. */
export function filterCellsStmts(mask: number, under: string, from: number, limit: number, examined: number): [Stmt, Stmt] {
  if (!under) {
    return [
      {
        sql:
          `SELECT ord, id, label, aka, f, p FROM labels WHERE ds = ${DS} AND ord >= ?2 AND ord < ?2 + ?4 ` +
          `AND (coalesce(f, 0) & ?3) = ?3 ORDER BY ord LIMIT ?5 + 1`,
        params: [from, mask, examined, limit],
      },
      { sql: `SELECT ord FROM labels WHERE ds = ${DS} AND ord = ?2 + ?3`, params: [from, examined] },
    ];
  }
  return [
    {
      sql:
        `SELECT l.ord AS ord, l.id AS id, l.label AS label, l.aka AS aka, l.f AS f, l.p AS p ` +
        `FROM (SELECT ord FROM cell_under WHERE ds = ${DS} AND anc = ?4 AND ord >= ?2 ORDER BY ord LIMIT ?5) u ` +
        `CROSS JOIN labels l WHERE l.ds = ${DS} AND l.ord = u.ord AND (coalesce(l.f, 0) & ?3) = ?3 ` +
        `ORDER BY u.ord LIMIT ?6 + 1`,
      params: [from, mask, under, examined, limit],
    },
    {
      sql: `SELECT ord FROM cell_under WHERE ds = ${DS} AND anc = ?3 AND ord >= ?2 ORDER BY ord LIMIT 1 OFFSET ?4`,
      params: [from, under, examined],
    },
  ];
}

/** [examined rows; limit + 1 returned] plus the probe. */
export function filterSupercellsStmts(mask: number, under: string, from: number, limit: number, examined: number): [Stmt, Stmt] {
  return [
    {
      sql:
        `SELECT ord, path, label, fa, parent, n_cells FROM supercells WHERE ds = ${DS} AND ord >= ?2 AND ord < ?2 + ?5 ` +
        `AND (coalesce(fa, 0) & ?3) = ?3 AND (?4 = '' OR path = ?4 OR (path >= ?4 || '/' AND path < ?4 || '0')) ` +
        `ORDER BY ord LIMIT ?6 + 1`,
      params: [from, mask, under, examined, limit],
    },
    { sql: `SELECT ord FROM supercells WHERE ds = ${DS} AND ord = ?2 + ?3`, params: [from, examined] },
  ];
}

/** [40 rows] */
export function xrefPagesStmt(node: string): Stmt {
  return {
    sql: `SELECT page FROM xrefs WHERE ds = ${DS} AND node = ?2 ORDER BY page_ord LIMIT ${XREF_PAGES_CAP}`,
    params: [node],
  };
}

/** [40 x 101 rows]: node_ord is dense from 0 per page (the builder dedupes a
 *  page's nodes), so this is a range on xrefs_page. */
export function xrefNodesStmt(pages: readonly string[]): Stmt {
  return {
    sql:
      `SELECT page, node, node_ord FROM xrefs WHERE ds = ${DS} ` +
      `AND page IN (SELECT value FROM json_each(?2)) AND node_ord <= 100`,
    params: [ids(pages, XREF_PAGES_CAP, "pages")],
  };
}

/** [50 rows]: organ owners, with the length of the owner's cell page (null
 *  when it has none, as a supercell owner) and the page itself when it is at
 *  most maxChars characters. */
export function organPagesStmt(keys: readonly string[], maxChars = PREFETCH_PAGE_CHARS): Stmt {
  return {
    sql:
      `SELECT a.key AS key, a.owner AS owner, length(cp.json) AS chars, ` +
      `CASE WHEN length(cp.json) <= ?3 THEN cp.json END AS json FROM aliases a ` +
      `LEFT JOIN cell_pages cp ON cp.ds = a.ds AND cp.id = a.owner ` +
      `WHERE a.ds = ${DS} AND a.kind = 'organ' AND a.key IN (SELECT value FROM json_each(?2))`,
    params: [ids(keys, PREFETCH_CHUNK, "organ keys"), maxChars],
  };
}

// ---- name tiers ------------------------------------------------------------------

export interface NameTiers {
  stmts: Stmt[];
  /** owners in rank order, deduplicated, at most `limit` */
  decode(rows: Row[][]): string[];
}

/** The tiers for q over name seqs [lo, hi): exact; then prefix (the hot row,
 *  or the range) for q of 2 to 200 units; then fragment for q of 3 to 200.
 *  Each tier is in seq order (cells before supercells); owners keep their
 *  first place. */
export function nameTiers(
  q: string,
  lo: number,
  hi: number,
  limit: number,
  consts: DatasetConstants,
  hex: string,
  overBound: (reason: string) => Error,
): NameTiers {
  const qlc = q.toLowerCase();
  const units = qlc.length;
  const stmts: Stmt[] = [exactTierStmt(qlc, lo, hi)];
  const prefix = units >= QUERY_MIN && units <= QUERY_MAX;
  if (prefix) {
    stmts.push(hotTierStmt(qlc, lo < consts.supNamesFrom, hi > consts.supNamesFrom));
    stmts.push(rangeTierStmt(qlc));
  }
  const match = units >= FRAGMENT_MIN && units <= QUERY_MAX && [...q].length >= FRAGMENT_MIN
    ? fragmentMatch(q, consts.namesMode)
    : null;
  if (match !== null) {
    stmts.push(fragmentTierStmt(hex, match, lo, hi, Math.min(FRAGMENT_ROWS_PER_HIT * limit, FRAGMENT_ROWS_CAP)));
  }
  return {
    stmts,
    decode(rows: Row[][]): string[] {
      const bySeq = (list: Row[]) =>
        list
          .map((r) => ({ seq: Number(r.seq), owner: String(r.owner) }))
          .filter((r) => r.seq >= lo && r.seq < hi)
          .sort((a, b) => a.seq - b.seq);
      let i = 0;
      const tiers: Array<Array<{ seq: number; owner: string }>> = [bySeq(rows[i++] ?? [])];
      if (prefix) {
        const hot = rows[i++] ?? [];
        const range = rows[i++] ?? [];
        if (range.length > PREFIX_ROWS) throw overBound("a prefix range holds more names than the builder allows");
        tiers.push(bySeq([...hot, ...range]));
      }
      if (match !== null) tiers.push(bySeq(rows[i++] ?? []));
      const out: string[] = [];
      const seen = new Set<string>();
      for (const tier of tiers) {
        for (const { owner } of tier) {
          if (out.length >= limit) return out;
          if (seen.has(owner)) continue;
          seen.add(owner);
          out.push(owner);
        }
      }
      return out;
    },
  };
}

// ---- the statement pass --------------------------------------------------------------

/** Statement candidates, longest first: the substrings of the lowercased q
 *  (its first 2,000 UTF-16 units) that start at 0 or after a character
 *  outside [a-z0-9], end at the end of q or before such a character, and are 4
 *  to `longest` units long, deduplicated. A name passes the static pass's test
 *  exactly when it equals one. At most 4 chunks are ever sent, so at most one
 *  more than that many are made (the extra one says whether the pass stopped
 *  at its bound). `cut` is true when q was longer than the scanned window. */
export function statementCandidates(q: string, longest: number): { candidates: string[]; cut: boolean } {
  const full = q.toLowerCase();
  let end = Math.min(full.length, STMT_SCAN_UNITS);
  // never split a surrogate pair at the cut
  if (end < full.length && end > 0) {
    const last = full.charCodeAt(end - 1);
    if (last >= 0xd800 && last <= 0xdbff) end -= 1;
  }
  const cut = end < full.length;
  const word = (ch: string | undefined) => ch !== undefined && /[a-z0-9]/.test(ch);
  const isStart: boolean[] = [];
  const isEnd: boolean[] = [];
  for (let i = 0; i <= end; i++) {
    isStart[i] = i < end && (i === 0 || !word(full[i - 1]));
    // an end at the cut needs the character after it, in the whole q, to be a boundary
    isEnd[i] = i > 0 && (i === full.length || !word(full[i]));
  }
  const max = STMT_CHUNK * STMT_CHUNKS + 1;
  const out: string[] = [];
  const seen = new Set<string>();
  for (let len = Math.min(longest, end); len >= STMT_MIN_LABEL; len--) {
    const level: string[] = [];
    for (let i = 0; i + len <= end; i++) {
      if (!isStart[i] || !isEnd[i + len]) continue;
      const cand = full.slice(i, i + len);
      if (seen.has(cand)) continue;
      seen.add(cand);
      level.push(cand);
    }
    level.sort();
    for (const cand of level) {
      out.push(cand);
      if (out.length >= max) return { candidates: out, cut };
    }
  }
  return { candidates: out, cut };
}

function rankStatementHits(best: Map<string, number>): Array<{ id: string; len: number }> {
  return [...best]
    .map(([id, len]) => ({ id, len }))
    .sort((a, b) => b.len - a.len || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
}

// ---- row decoding ------------------------------------------------------------------

function record(value: unknown): Row | null {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Row) : null;
}

// A labels row rebuilt as the labels.json row: id, label, then f, aka and p
// when present, in that order (build_cell_shards.py writes the label key
// always), nothing else.
function labelRow(r: Row): BrainLabelRow {
  const row: Row = { id: r.id, label: r.label ?? null };
  if (r.f !== null && r.f !== undefined) row.f = r.f;
  if (typeof r.aka === "string") row.aka = r.aka.split(AKA_SEP);
  if (r.p !== null && r.p !== undefined) row.p = r.p;
  return row as unknown as BrainLabelRow;
}

// A supercell head. The builder already took each organ's `label ?? id`, so an
// organ labelled "" gives "", and an aka of "" (one such organ) is [""].
function supercellHead(r: Row): SupercellHead {
  return {
    id: String(r.path),
    label: r.label === null || r.label === undefined ? undefined : String(r.label),
    ...(typeof r.aka === "string" ? { aka: r.aka.split(AKA_SEP) } : {}),
    ...(r.n_cells ? { n_cells: Number(r.n_cells) } : {}),
  };
}

function decodeKeyHits(rows: Row[]): KeyHits {
  const out: KeyHits = {};
  let cell: string | undefined;
  let sorgan: string | undefined;
  for (const r of rows) {
    if (r.owner === null || r.owner === undefined) continue; // min() over no rows
    const owner = String(r.owner);
    if (r.hit === "organ") out.organ = owner;
    else if (r.hit === "decl") out.decl = owner;
    else if (r.hit === "slug") out.slug = owner;
    else if (r.hit === "cell") cell = owner;
    else if (r.hit === "sorgan") sorgan = owner;
  }
  const name = cell ?? sorgan;
  if (name !== undefined) out.name = name;
  return out;
}

// ---- the store ------------------------------------------------------------------------

// prefetchAtoms' pages over their batch's cap, read alone at its end
interface Alone {
  cells: Map<string, number>; // id -> characters
  sups: Map<string, { chars: number; breadcrumb: Crumb[] }>;
}

export class D1Store implements BrainStore, BrainStoreBatches {
  private readonly c: Ctx;
  private readonly release: BrainReleaseContext;
  private readonly datasetId: string;
  private readonly hex: string;
  private checked = false;
  private everyBatch = false;
  private failure: BrainReleaseUnavailableError | null = null;
  private consts: DatasetConstants | null = null;
  // what this request already read
  private readonly cellMemo = new Map<string, CellEntry | null>();
  private readonly supercellMemo = new Map<string, { entry: SupercellEntry; breadcrumb: Crumb[] } | null>();
  private readonly organMemo = new Map<string, string | null>();
  private prefetchedChars = 0; // characters (SQLite's length) of page text the prefetch kept

  constructor(c: Ctx, release: BrainReleaseContext) {
    this.c = c;
    this.release = release;
    this.datasetId = release.datasetId ?? "";
    this.hex = release.datasetHex ?? "";
    if (!release.datasetId || !HEX_RE.test(this.hex) || release.datasetId !== `sha256:${this.hex}`) {
      this.failure = this.unavailable("the release names no well-formed serving dataset");
    }
  }

  /** For a write route: every later batch starts with the readiness check. */
  checkEveryBatch(): void {
    this.everyBatch = true;
  }

  private unavailable(reason: string): BrainReleaseUnavailableError {
    return new BrainReleaseUnavailableError(this.release.releaseId, "BRAIN_DB", reason);
  }

  // The first failure is kept: later calls in this request rethrow it.
  private fail(reason: string): BrainReleaseUnavailableError {
    const failure = this.failure ?? this.unavailable(reason);
    this.failure = failure;
    return failure;
  }

  private parse(json: unknown, table: string): Row {
    let value: unknown;
    try {
      value = typeof json === "string" ? JSON.parse(json) : undefined;
    } catch {
      value = undefined;
    }
    const row = record(value);
    if (!row) throw this.fail(`a ${table} row has an invalid shape`);
    return row;
  }

  // One BRAIN_DB.batch: the readiness check first while none has passed (or
  // always, after checkEveryBatch), then `stmts`. An empty `stmts` sends the
  // check alone, or nothing once it passed.
  private async run(stmts: Stmt[]): Promise<Row[][]> {
    if (this.failure) throw this.failure;
    const db = this.c.env.BRAIN_DB;
    if (!db) throw this.fail("BRAIN_DB is not bound");
    const withCheck = !this.checked || (this.everyBatch && stmts.length > 0);
    if (!withCheck && !stmts.length) return [];
    const prepared = stmts.map((s) => db.prepare(s.sql).bind(this.datasetId, ...s.params));
    if (withCheck) prepared.unshift(db.prepare(READINESS_SQL).bind(this.datasetId));
    let results: Array<{ results?: unknown[] }>;
    try {
      results = await db.batch(prepared);
    } catch (error) {
      throw this.fail(`the D1 batch failed: ${String(error)}`);
    }
    if (this.failure) throw this.failure; // a concurrent batch failed its check
    const rows = results.map((r) => (r.results ?? []) as Row[]);
    if (withCheck) this.accept(rows.shift() ?? []);
    return rows;
  }

  private accept(rows: Row[]): void {
    const row = rows[0];
    if (!row) throw this.fail("the serving dataset is not ready");
    const version = Number(row.schema_version);
    if (!SUPPORTED_SCHEMA_VERSIONS.has(version)) {
      throw this.fail(`schema_version ${String(row.schema_version)} is not one this Worker reads`);
    }
    const namesMode = row.names_mode;
    if (namesMode !== "trigram" && namesMode !== "parts") {
      throw this.fail(`names_mode ${String(namesMode)} is unknown`);
    }
    const longestName = Number(row.longest_name);
    const supNamesFrom = Number(row.sup_names_from);
    if (!Number.isSafeInteger(longestName) || !Number.isSafeInteger(supNamesFrom) || supNamesFrom < 1) {
      throw this.fail("the datasets row has an invalid shape");
    }
    // the constants this batch was built from (this store's, or the isolate's memo)
    const used = this.consts;
    const fresh: DatasetConstants = { namesMode, longestName, supNamesFrom };
    rememberConstants(this.datasetId, fresh);
    if (
      used &&
      (used.namesMode !== namesMode || used.longestName !== longestName || used.supNamesFrom !== supNamesFrom)
    ) {
      throw this.fail("the dataset's constants differ from the memoized ones (imported again)");
    }
    this.consts = fresh;
    this.checked = true;
  }

  // The dataset's constants, from this store or the isolate's memo, else from a
  // readiness check sent alone.
  private async constants(): Promise<DatasetConstants> {
    if (this.failure) throw this.failure;
    if (this.consts) return this.consts;
    const known = _constants.get(this.datasetId);
    if (known) return (this.consts = known);
    await this.run([]);
    return this.constantsOrFail();
  }

  // run() sets the constants when the check passes, or throws.
  private constantsOrFail(): DatasetConstants {
    if (!this.consts) throw this.fail("the readiness check did not run");
    return this.consts;
  }

  private range(kind: "" | "cell" | "supercell", consts: DatasetConstants): [number, number] {
    if (kind === "cell") return [1, consts.supNamesFrom];
    if (kind === "supercell") return [consts.supNamesFrom, MAX_SEQ];
    return [1, MAX_SEQ];
  }

  private tiers(q: string, kind: "" | "cell" | "supercell", limit: number, consts: DatasetConstants): NameTiers | null {
    // no cell name can equal, contain or start with a q longer than every cell name
    if (kind === "cell" && q.toLowerCase().length > consts.longestName) return null;
    const [lo, hi] = this.range(kind, consts);
    if (lo >= hi) return null; // a dataset with no cell names
    return nameTiers(q, lo, hi, limit, consts, this.hex, (reason) => this.fail(reason));
  }

  async snapshotSource(): Promise<{ prov: Array<Record<string, string>>; _meta?: Record<string, unknown> }> {
    return brainManifestMeta(this.c, this.release);
  }

  async cellPage(id: string): Promise<CellEntry | null> {
    if (this.failure) throw this.failure;
    if (this.cellMemo.has(id)) return this.cellMemo.get(id)!;
    const [rows] = await this.run([cellPageStmt(id)]);
    const entry = rows[0] ? (this.parse(rows[0].json, "cell_pages") as unknown as CellEntry) : null;
    this.cellMemo.set(id, entry);
    return entry;
  }

  async supercell(path: string): Promise<{ entry: SupercellEntry; breadcrumb: Crumb[] } | null> {
    if (this.failure) throw this.failure;
    if (this.supercellMemo.has(path)) return this.supercellMemo.get(path)!;
    const [rows, crumbs] = await this.run([supercellStmt(path), breadcrumbStmt(path)]);
    const found = rows[0]
      ? {
        entry: this.parse(rows[0].json, "supercells") as unknown as SupercellEntry,
        breadcrumb: crumbs.map((r) => ({ id: String(r.path), label: r.label === null ? null : (r.label as string) })),
      }
      : null;
    this.supercellMemo.set(path, found);
    return found;
  }

  // Every requested lookup in one statement; the caller applies precedence.
  // An organ lookup this request already answered costs nothing.
  async keyHits(k: KeyLookup): Promise<KeyHits> {
    if (this.failure) throw this.failure;
    const onlyOrgan = k.organ !== undefined && k.decl === undefined && k.slug === undefined && k.name === undefined;
    if (onlyOrgan && this.organMemo.has(k.organ!)) {
      const owner = this.organMemo.get(k.organ!);
      return owner === null || owner === undefined ? {} : { organ: owner };
    }
    const stmt = keyHitsStmt(k);
    if (!stmt) return {};
    const [rows] = await this.run([stmt]);
    const hits = decodeKeyHits(rows);
    if (k.organ !== undefined) this.organMemo.set(k.organ, hits.organ ?? null);
    return hits;
  }

  async searchNames(q: string, kind: "" | "cell" | "supercell", limit: number): Promise<AtomHead[]> {
    return (await this.searchPage(q, kind, limit, null)).named;
  }

  async searchCellNames(q: string, limit: number): Promise<BrainLabelRow[]> {
    const named = (await this.searchPage(q, "cell", limit, null)).named;
    return named.flatMap((h) => (h.kind === "cell" ? [h.row] : []));
  }

  async statementAtoms(q: string, limit: number): Promise<Array<{ id: string; len: number }>> {
    const consts = await this.constants();
    const pass = await this.statementPass(q, consts, []);
    return pass.hits.slice(0, limit);
  }

  // The bounded statement pass. `first` holds chunk 1's rows when the caller
  // already sent it in its own batch (bridgeCandidates).
  private async statementPass(
    q: string,
    consts: DatasetConstants,
    first: Row[],
    firstSent = false,
  ): Promise<{ hits: Array<{ id: string; len: number }>; truncated: boolean }> {
    const { candidates, cut } = statementCandidates(q, consts.longestName);
    const best = new Map<string, number>();
    const merge = (rows: Row[]) => {
      for (const r of rows) {
        const id = String(r.owner);
        const len = Number(r.len);
        if (len > (best.get(id) ?? 0)) best.set(id, len);
      }
    };
    let sent = 0;
    let chunks = 0;
    let truncated = cut;
    while (sent < candidates.length) {
      if (chunks === STMT_CHUNKS) {
        truncated = true;
        break;
      }
      const chunk = candidates.slice(sent, statementChunkEnd(candidates, sent));
      if (chunks === 0 && firstSent) merge(first);
      else merge((await this.run([statementChunkStmt(chunk)]))[0]);
      sent += chunk.length;
      chunks += 1;
      const ranked = rankStatementHits(best);
      // a tie class is always finished: stop only below the 5th hit's length
      if (ranked.length >= STMT_HITS && sent < candidates.length && candidates[sent].length < ranked[STMT_HITS - 1].len) {
        break;
      }
    }
    return { hits: rankStatementHits(best).slice(0, STMT_HITS), truncated };
  }

  // Cells from labels, then supercells, which win a shared id.
  async labelRows(list: string[]): Promise<Map<string, AtomHead>> {
    if (this.failure) throw this.failure;
    const want = [...new Set(list)];
    const out = new Map<string, AtomHead>();
    if (!want.length) return out;
    const [cells, sups] = await this.run(labelRowsStmts(want));
    const byId = new Map(cells.map((r) => [String(r.id), r]));
    const byPath = new Map(sups.map((r) => [String(r.path), r]));
    for (const id of want) {
      const sup = byPath.get(id);
      const cell = byId.get(id);
      if (sup) out.set(id, { kind: "supercell", head: supercellHead(sup) });
      else if (cell) out.set(id, { kind: "cell", row: labelRow(cell) });
    }
    return out;
  }

  // The trace table holds every supercell-involving pair's traces (up to 24)
  // with its true total. Each partner gets the shard rule back: the first 6
  // traces (pick_traces keeps its prefix whatever the cap), and tt only when
  // that drops some, so a row the static store hydrates reads the same here.
  async supercellTraces(atomId: string, rows: Synapse[]): Promise<Map<string, SynapseTraces>> {
    if (this.failure) throw this.failure;
    const partners = rows.filter((s) => !s.traces?.length).map((s) => s.id);
    const out = new Map<string, SynapseTraces>();
    if (!partners.length) return out;
    const byPair = new Map(partners.map((id) => [pairKey(atomId, id), id]));
    const [found] = await this.run([tracesStmt([...byPair.keys()])]);
    for (const r of found) {
      const partner = byPair.get(String(r.pair));
      if (partner === undefined) continue;
      const value = this.parse(r.json, "synapse_traces");
      if (!Array.isArray(value.traces)) throw this.fail("a synapse_traces row has an invalid shape");
      const traces = (value.traces as Trace[]).slice(0, TRACES_PER_ROW);
      if (!traces.length) continue;
      const tt = typeof value.tt === "number" && value.tt > traces.length ? value.tt : undefined;
      out.set(partner, { traces, ...(tt !== undefined ? { tt } : {}) });
    }
    return out;
  }

  async filterCells(mask: number, under: string, from: number, limit: number): Promise<FilterPage<BrainLabelRow>> {
    if (this.failure) throw this.failure;
    const [rows, probe] = await this.run(filterCellsStmts(mask, under, from, limit, _filterExamined));
    const page = rows.slice(0, limit).map(labelRow);
    const next = rows.length > limit ? Number(rows[limit].ord) : probe[0] ? Number(probe[0].ord) : null;
    return { rows: page, next };
  }

  async filterSupercells(
    mask: number,
    under: string,
    from: number,
    limit: number,
  ): Promise<FilterPage<Record<string, unknown>>> {
    if (this.failure) throw this.failure;
    const [rows, probe] = await this.run(filterSupercellsStmts(mask, under, from, limit, _filterExamined));
    const page = rows.slice(0, limit).map((r) => ({
      id: r.path,
      label: r.label ?? null,
      ...(r.fa !== null && r.fa !== undefined ? { fa: r.fa } : {}),
      ...(r.parent ? { parent: r.parent } : {}),
      ...(r.n_cells ? { n_cells: r.n_cells } : {}),
    }));
    const next = rows.length > limit ? Number(rows[limit].ord) : probe[0] ? Number(probe[0].ord) : null;
    return { rows: page, next };
  }

  async xrefPages(node: string): Promise<string[]> {
    if (this.failure) throw this.failure;
    const [rows] = await this.run([xrefPagesStmt(node)]);
    return rows.map((r) => String(r.page));
  }

  async xrefNodes(pages: string[]): Promise<Map<string, string[]>> {
    if (this.failure) throw this.failure;
    const out = new Map<string, string[]>();
    if (!pages.length) return out;
    const [rows] = await this.run([xrefNodesStmt(pages)]);
    const byPage = new Map<string, Array<{ node: string; ord: number }>>();
    for (const r of rows) {
      const page = String(r.page);
      const list = byPage.get(page) ?? [];
      list.push({ node: String(r.node), ord: Number(r.node_ord) });
      byPage.set(page, list);
    }
    for (const p of pages) {
      const list = byPage.get(p);
      if (list) out.set(p, list.sort((a, b) => a.ord - b.ord).map((x) => x.node));
    }
    return out;
  }

  // ---- composites (BrainStoreBatches) --------------------------------------------------

  // Batch 1: the key lookups and the name tiers; batch 2: the heads of the
  // ranked owners and of every key hit (or of the atom id). Two round trips,
  // plus a readiness check alone on an isolate's first request for a dataset.
  async searchPage(
    q: string,
    kind: "" | "cell" | "supercell",
    limit: number,
    query: AtomKeyQuery | null,
  ): Promise<SearchPageResult> {
    const consts = await this.constants();
    const tiers = this.tiers(q, kind, limit, consts);
    const keyStmt = query && "lookup" in query ? keyHitsStmt(query.lookup) : null;
    const stmts = [...(keyStmt ? [keyStmt] : []), ...(tiers?.stmts ?? [])];
    const rows = await this.run(stmts);
    const hits = keyStmt ? decodeKeyHits(rows[0]) : {};
    if (query && "lookup" in query && query.lookup.organ !== undefined) {
      this.organMemo.set(query.lookup.organ, hits.organ ?? null);
    }
    const owners = tiers ? tiers.decode(rows.slice(keyStmt ? 1 : 0)) : [];
    const extra = query && "atomId" in query
      ? [query.atomId]
      : [hits.organ, hits.decl, hits.slug, hits.name].filter((id): id is string => !!id);
    const heads = await this.labelRows([...owners, ...extra]);
    const named = owners.flatMap((id) => {
      const head = heads.get(id);
      return head ? [head] : [];
    });
    return { named, hits, heads };
  }

  // Batch 1: the key lookups (or the atom id's existence), the cell tiers
  // limited to 5 and the statement pass's first chunk; then further chunks
  // only while the stop rule asks for them (at most 4 in all).
  async bridgeCandidates(q: string, query: AtomKeyQuery | null): Promise<BridgeCandidatesResult> {
    const consts = await this.constants();
    const stmts: Stmt[] = [];
    let keyAt = -1;
    let existsAt = -1;
    if (query && "lookup" in query) {
      const stmt = keyHitsStmt(query.lookup);
      if (stmt) keyAt = stmts.push(stmt) - 1;
    } else if (query && "atomId" in query) {
      existsAt = stmts.push(atomExistsStmt(query.atomId)) - 1;
    }
    const tiers = this.tiers(q, "cell", STMT_HITS, consts);
    const tiersAt = stmts.length;
    if (tiers) stmts.push(...tiers.stmts);
    const { candidates } = statementCandidates(q, consts.longestName);
    const chunkAt = candidates.length
      ? stmts.push(statementChunkStmt(candidates.slice(0, statementChunkEnd(candidates, 0)))) - 1
      : -1;
    const rows = await this.run(stmts);
    const hits = keyAt >= 0 ? decodeKeyHits(rows[keyAt]) : {};
    if (keyAt >= 0 && query && "lookup" in query && query.lookup.organ !== undefined) {
      this.organMemo.set(query.lookup.organ, hits.organ ?? null);
    }
    const names = tiers ? tiers.decode(rows.slice(tiersAt, tiersAt + tiers.stmts.length)) : [];
    const pass = await this.statementPass(q, consts, chunkAt >= 0 ? rows[chunkAt] : [], chunkAt >= 0);
    return {
      hits,
      atomExists: existsAt >= 0 && rows[existsAt].length > 0,
      names,
      statement: pass.hits,
      statementTruncated: pass.truncated,
    };
  }

  // Loads the owners, cell pages and supercells of many node ids into this
  // request's memos, in batches of 50 keys, so a quickstatements submit
  // resolves its endpoints in a few batches instead of up to three statements
  // per endpoint: organ keys load their owners (and a cell owner's page), cell:
  // ids their pages and path: ids their entries and breadcrumbs, then a second
  // pass loads the supercells that organ keys named as owners. Each batch's
  // page cap is its share of what the kept budget has left; a page or entry
  // over it is read alone at the end, so every page an endpoint reads is in a
  // memo when this returns. Throws PrefetchBudgetError, after reads only, when
  // the pages pass the budget or too many go alone.
  async prefetchAtoms(list: string[]): Promise<void> {
    if (this.failure) throw this.failure;
    const organs = [...new Set(list.filter((id) => !id.startsWith("cell:") && !id.startsWith("path:") && !this.organMemo.has(id)))];
    const cells = [...new Set(list.filter((id) => id.startsWith("cell:") && !this.cellMemo.has(id)))];
    const paths = [...new Set(list.filter((id) => id.startsWith("path:") && !this.supercellMemo.has(id)))];
    // pages over their batch's cap, by id or path, with their lengths
    const alone: Alone = { cells: new Map(), sups: new Map() };
    for (let i = 0; i < Math.max(organs.length, cells.length, paths.length); i += PREFETCH_CHUNK) {
      const organChunk = organs.slice(i, i + PREFETCH_CHUNK);
      // a cell an earlier batch's organ key already loaded is not asked for again
      const cellChunk = cells.slice(i, i + PREFETCH_CHUNK).filter((id) => !this.cellMemo.has(id) && !alone.cells.has(id));
      const pathChunk = paths.slice(i, i + PREFETCH_CHUNK);
      const cap = this.pageCap(organChunk.length + cellChunk.length + pathChunk.length);
      const stmts: Stmt[] = [];
      if (organChunk.length) stmts.push(organPagesStmt(organChunk, cap));
      if (cellChunk.length) stmts.push(cellPagesStmt(cellChunk, cap));
      if (pathChunk.length) stmts.push(supercellsStmt(pathChunk, cap), breadcrumbsStmt(pathChunk));
      const rows = await this.run(stmts);
      let at = 0;
      if (organChunk.length) {
        const found = new Map<string, Row>();
        for (const r of rows[at++]) found.set(String(r.key), r);
        for (const key of organChunk) {
          const r = found.get(key);
          this.organMemo.set(key, r ? String(r.owner) : null);
          // atomFor reads a path: owner as a supercell (the pass below), any other as a cell page
          if (r && !String(r.owner).startsWith("path:")) this.rememberCell(String(r.owner), r, alone);
        }
      }
      if (cellChunk.length) {
        const found = new Map<string, Row>();
        for (const r of rows[at++]) found.set(String(r.id), r);
        for (const id of cellChunk) {
          const r = found.get(id);
          if (r) this.rememberCell(id, r, alone);
          else if (!this.cellMemo.has(id)) this.cellMemo.set(id, null);
        }
      }
      if (pathChunk.length) {
        this.rememberSupercells(pathChunk, rows[at], rows[at + 1], alone);
      }
    }
    const owners = [...new Set(organs.map((key) => this.organMemo.get(key)))].filter(
      (owner): owner is string =>
        typeof owner === "string" && owner.startsWith("path:") && !this.supercellMemo.has(owner) && !alone.sups.has(owner),
    );
    for (let i = 0; i < owners.length; i += PREFETCH_CHUNK) {
      const chunk = owners.slice(i, i + PREFETCH_CHUNK);
      const [entries, crumbs] = await this.run([supercellsStmt(chunk, this.pageCap(chunk.length)), breadcrumbsStmt(chunk)]);
      this.rememberSupercells(chunk, entries, crumbs, alone);
    }
    await this.readAlone(alone);
  }

  // A batch's page cap: an equal share of what the kept budget has left, at
  // most PREFETCH_PAGE_CHARS, so the batch's pages cannot pass the budget.
  private pageCap(keys: number): number {
    const left = PREFETCH_KEPT_CHARS - this.prefetchedChars;
    return left <= 0 || keys <= 0 ? 0 : Math.min(PREFETCH_PAGE_CHARS, Math.floor(left / keys));
  }

  // A page the prefetch keeps, counted by its length as SQLite measures it
  // (the length the caps compare).
  private keptPage(json: unknown, chars: number, table: string): Row {
    this.prefetchedChars += chars;
    return this.parse(json, table);
  }

  // A row of cellPagesStmt or organPagesStmt for the cell page `id`: no page
  // (chars null) is remembered as null, a page over the cap goes alone.
  private rememberCell(id: string, r: Row, alone: Alone): void {
    if (this.cellMemo.has(id) || alone.cells.has(id)) return; // an organ key and a cell id in one batch
    if (r.chars === null || r.chars === undefined) this.cellMemo.set(id, null);
    else if (r.json === null || r.json === undefined) alone.cells.set(id, Number(r.chars));
    else this.cellMemo.set(id, this.keptPage(r.json, Number(r.chars), "cell_pages") as unknown as CellEntry);
  }

  // supercellsStmt's and breadcrumbsStmt's rows for `chunk`: an absent path is
  // remembered as null, an entry over the cap goes alone with its breadcrumb.
  private rememberSupercells(chunk: string[], entries: Row[], crumbs: Row[], alone: Alone): void {
    const found = new Map<string, Row>();
    for (const r of entries) found.set(String(r.path), r);
    const walks = new Map<string, Row[]>();
    for (const r of crumbs) {
      const start = String(r.start);
      const walk = walks.get(start) ?? [];
      walk.push(r);
      walks.set(start, walk);
    }
    for (const path of chunk) {
      const r = found.get(path);
      if (!r) {
        this.supercellMemo.set(path, null);
        continue;
      }
      const walk = (walks.get(path) ?? []).sort((a, b) => Number(b.d) - Number(a.d));
      const breadcrumb = walk.map((c) => ({ id: String(c.path), label: c.label === null ? null : (c.label as string) }));
      if (r.json === null || r.json === undefined) {
        alone.sups.set(path, { chars: Number(r.chars), breadcrumb });
        continue;
      }
      this.supercellMemo.set(path, {
        entry: this.keptPage(r.json, Number(r.chars), "supercells") as unknown as SupercellEntry,
        breadcrumb,
      });
    }
  }

  // The pages the batch caps left out, PREFETCH_ALONE_BATCH single-row
  // statements a batch (each row is at most D1's 2 MB), once the budget is
  // known to hold them. The checks come before any of these reads.
  private async readAlone(alone: Alone): Promise<void> {
    const pages = alone.cells.size + alone.sups.size;
    if (!pages) return;
    let chars = 0;
    for (const n of alone.cells.values()) chars += n;
    for (const s of alone.sups.values()) chars += s.chars;
    if (pages > PREFETCH_ALONE_PAGES) {
      throw new PrefetchBudgetError(`${pages} Brain pages over the batch caps, where one request reads at most ${PREFETCH_ALONE_PAGES}`);
    }
    if (this.prefetchedChars + chars > PREFETCH_KEPT_CHARS) {
      throw new PrefetchBudgetError(
        `${this.prefetchedChars + chars} characters of Brain pages, where one request keeps at most ${PREFETCH_KEPT_CHARS}`,
      );
    }
    const work: Array<{ cell: string; chars: number } | { path: string; chars: number; breadcrumb: Crumb[] }> = [
      ...[...alone.cells].map(([cell, n]) => ({ cell, chars: n })),
      ...[...alone.sups].map(([path, s]) => ({ path, ...s })),
    ];
    for (let i = 0; i < work.length; i += PREFETCH_ALONE_BATCH) {
      const batch = work.slice(i, i + PREFETCH_ALONE_BATCH);
      const rows = await this.run(batch.map((w) => ("cell" in w ? cellPageStmt(w.cell) : supercellStmt(w.path))));
      batch.forEach((w, j) => {
        const r = rows[j][0];
        if ("cell" in w) {
          this.cellMemo.set(w.cell, r ? (this.keptPage(r.json, w.chars, "cell_pages") as unknown as CellEntry) : null);
        } else {
          this.supercellMemo.set(
            w.path,
            r ? { entry: this.keptPage(r.json, w.chars, "supercells") as unknown as SupercellEntry, breadcrumb: w.breadcrumb } : null,
          );
        }
      });
    }
  }
}
