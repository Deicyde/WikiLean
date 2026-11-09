// The Brain data interface (docs/BRAIN-D1.md section 8, step 3a).
//
// Every read of a release's Brain data goes through a BrainStore: the routes in
// brain-api.ts, brain-edits.ts and (through them) mcp.ts keep validation,
// precedence, ranking, paging, cursors and response bodies, and the store only
// answers "what does this release hold". Today the only implementation is the
// StaticStore (brain-store-static.ts), which reads the release's static JSON
// files exactly as the routes used to. A D1Store (step 3b) implements the same
// methods with the bounded statements of docs/BRAIN-D1.md section 1; the
// bracket after each method below is its result bound there.
//
// Where the two stores deliberately answer differently (search tiers, short
// filter pages, the bounded statement pass), the method sits at the level where
// each store applies its own algorithm, and the doc comment says what the
// static answer is.
//
// Errors: a method throws BrainReleaseUnavailableError(release.releaseId,
// path | "BRAIN_DB", reason) when the release's data is missing, fails its
// digest, has the wrong shape or (D1) fails the readiness check; withRelease,
// MCP and brain-edits' unavailableReleaseResponse turn it into a 503. A real
// miss is null, an absent key or an empty list, never a throw. Any other throw
// is a bug (500).
//
// Input bounds: each bracketed bound holds only because the callers cap their
// inputs (the bridge's 12 depends partners, /edges' 40 pages, the
// neighborhood's 200 rows). They are preconditions, not checks: a longer input
// is a caller bug, and a D1Store may throw a plain Error (500) on one. A
// D1Store answers an empty input (ids = [], pages = []) without sending a
// batch. In 3b the search route rejects a q over 200 characters with a 400, so
// no store sees a longer q.
//
// Round trips (for 3b): the routes await these methods one after another, so a
// D1Store cannot coalesce two calls into one batch behind the routes' backs.
// keyHits already merges the alias and exact-name lookups for that reason.
// Section 1's per-route counts are not reachable through these methods alone:
// searchFor runs searchNames, then keyHits (inside resolveAtomKey), then
// labelRows, 4 sequential batches on D1 against the plan's 2; bridgeFor runs 6
// to 9 against "2 or 3, at most 6". 3b adds per-route composite methods with
// their callers, along the lines of
//   searchPage(q, kind, limit, lookup: KeyLookup)
//     -> { named, keyHits, promoted?: AtomHead }
//   bridgeCandidates(q, lookup, nameLimit, stmtLimit)
//     -> { keyHits, names, statement }
// with resolveAtomKey's explicit-atom-id branch folded in as an existence
// check. Each one's static implementation runs today's loads in today's order,
// which the fetch-count tests pin. storeFor hands out one store per request and
// release, so a D1Store can keep one readiness result, and rows it already
// read, for the rest of the request.
import type { Context } from "hono";
import type { Env } from "./env.js";
import type { BrainLabelRow, BrainReleaseContext } from "./brain.js";
import { StaticStore } from "./brain-store-static.js";

type Ctx = Context<{ Bindings: Env }>;

// The test hooks for the static store's isolate caches. brain-edits re-exports
// them under their old names; no other module imports brain-store-static.
export { _resetXrefCaches, _xrefCacheReleaseIds } from "./brain-store-static.js";

export type Crumb = { id: string; label?: string | null };

// ---- shipped shapes (brain/build_cell_shards.py output, brain/SCHEMA.md#v3) ----

// An organ is a PARTICLE, never a node: it exists only inside a cell (or, for
// rule-5 field concepts and area pages, inside a supercell). Payloads are
// EMBEDDED by the builder — one shard fetch renders the whole card, so no route
// fans out to fetch an organ's content.
export interface Organ {
  kind: string; // concept | decl | page | article | statement
  id: string;
  label?: string;
  bond?: string; // exact | generalization | special_case | xref | field | … (absent on an anchor organ)
  prov?: number; // index into the manifest `prov` table
  // decl
  module?: string;
  decl_kind?: string;
  docstring?: string;
  code?: string;
  library?: string;
  renamed_to?: string; // the verified current FQ name when the cited name is dead (decl_renames.jsonl, baked in)
  // concept
  description?: string;
  slug?: string;
  article_annotations?: unknown;
  status?: string;
  // page
  db?: string;
  url?: string;
  kind_hint?: string;
  qid?: string;
  snippet?: string;
  snippet_license?: string;
  // article
  annotations?: unknown;
  // statement
  arxiv_id?: string;
  ref?: string;
  license_open?: boolean;
  [k: string]: unknown;
}

// One constituent bond of a synapse; keeps its OWN direction (a synapse is an
// undirected aggregate — SCHEMA v3 "src/dst are ordered lexicographically").
// `src`/`dst` are ORGAN ids, not cell ids: the trace names the actual particles.
export interface Trace {
  kind: string;
  src: string;
  dst: string;
  prov?: number;
  evidence?: Record<string, unknown>;
}

export interface Synapse {
  id: string; // the OTHER atom (cell:… or path:…)
  w: number; // weight = every constituent bond, capped or not
  kinds: Record<string, number>;
  traces?: Trace[]; // trimmed to `caps.traces_per_synapse`; `tt` = the true total
  tt?: number;
}

export interface CellHead {
  id: string;
  anchor: string;
  label?: string;
  supercells?: string[];
  f?: number;
  xy?: [number, number];
}

export interface CellEntry {
  cell: CellHead;
  organs?: Organ[];
  syn?: Synapse[];
  counts?: { syn?: number; organs?: number };
  truncated?: { syn?: number };
  breadcrumb?: Crumb[];
}

// supercells.json rows. Only 156 of ~9k carry organs and 37 carry synapses —
// most are pure containment. `fa` is the subtree-AGGREGATE facet mask.
export interface SupercellEntry {
  label?: string;
  fa?: number;
  parent?: string;
  children?: string[];
  cells?: string[];
  organs?: Organ[];
  syn?: Synapse[];
  counts?: { syn?: number };
}

// The head fields a supercell search hit or label lookup needs, never the whole
// row (a supercells.json entry also holds up to 200 synapses and 5,000 cell
// ids). `aka` is every organ's `label ?? id`, in organ order, duplicates kept,
// present only when the supercell has organs; `n_cells` is present only when
// its `cells` list is non-empty. `??`, not `||`: an organ whose label is ""
// contributes "", not its id, so a D1 head column must be built with
// `label if label is not None else id` (not Python `or`), and the D1 labelRows
// and search statements must select it.
export interface SupercellHead {
  id: string;
  label?: string;
  aka?: string[];
  n_cells?: number;
}

// A cell's raw labels.json row (unnormalized: an absent label stays absent) or
// a supercell's head.
export type AtomHead =
  | { kind: "cell"; row: BrainLabelRow }
  | { kind: "supercell"; head: SupercellHead };

// The trace fields of one synapse, as the partner side stores them (`tt` is the
// true total when known); the neighborhood route reads nothing else.
export interface SynapseTraces {
  traces: Trace[];
  tt?: number;
}

// One filter page in ord order. `next` is the ord to resume at; null when the
// pool is exhausted.
export interface FilterPage<R> {
  rows: R[];
  next: number | null;
}

// keyHits input. A field left undefined is not looked up.
export interface KeyLookup {
  organ?: string; // aliases.json `organs` key: QID, decl id, xref page, slug, lit statement
  decl?: string; // aliases.json `decls` key: a bare fully-qualified decl name
  slug?: string; // aliases.json `slugs` key
  // Exact name: the first cell whose label or aka lowercases to keyLc, else the
  // first supercell with an organ label lowercasing to keyLc or slugLc.
  name?: { keyLc: string; slugLc: string };
}

// keyHits output: the owning atom id per lookup that found its key. Callers
// treat only a truthy alias value as a hit (atomIdForOrgan passes `organ`
// through as is).
export interface KeyHits {
  organ?: string;
  decl?: string;
  slug?: string;
  name?: string;
}

/** Every Brain data read for one request against one release. */
export interface BrainStore {
  /** prov and _meta of cells/manifest.json, for snapshotFor. Static validates
   *  the whole manifest (scheme and shards too), as before. D1: the slim static
   *  manifest, validating prov and _meta only; no D1 statement. */
  snapshotSource(): Promise<{ prov: Array<Record<string, string>>; _meta?: Record<string, unknown> }>;

  /** The stored entry for cell `id`, as written; null if absent.   [1]  D1: cellPage */
  cellPage(id: string): Promise<CellEntry | null>;

  /** The supercell entry plus its breadcrumb, root first; null if absent.
   *  Static walks `parent` with a seen-set; D1 caps the depth at 64.
   *                                                       [1 + 64]  D1: supercell */
  supercell(path: string): Promise<{ entry: SupercellEntry; breadcrumb: Crumb[] } | null>;

  /** Alias and exact-name lookups for one key, in one call so a D1Store can
   *  send them as one batch. Precedence (organ > decl > slug > name) belongs to
   *  the caller. A store may stop at the first truthy hit in that order: the static
   *  store does, so it reads labels.json and supercells.json only after every
   *  requested alias lookup missed, as before. A D1Store may answer every
   *  lookup.                                    [3 + 1]  D1: aliasHits + exactName */
  keyHits(k: KeyLookup): Promise<KeyHits>;

  /** Agent-search name hits in rank order, at most `limit` per kind; the caller
   *  promotes, dedupes and caps. `q` is lowercased by the store (toLowerCase).
   *  Static: searchLabels over the cells, then supercells whose label or organ
   *  label starts with q, then those containing it; it loads labels.json and
   *  then supercells.json whatever the kind. D1: exact, prefix and fragment
   *  tiers, cells and supercells interleaved per tier; 3b widens the result
   *  with each hit's tier and the text hits, together with the route that
   *  reports them.                                    [2 x limit]  D1: search */
  searchNames(q: string, kind: "" | "cell" | "supercell", limit: number): Promise<AtomHead[]>;

  /** Cell name hits only, at most `limit`, for suggestions, the transfer
   *  fallback and bridge candidates. `q` is lowercased by the store. Static:
   *  searchLabels over labels.json only.     [5]  D1: search, cells only */
  searchCellNames(q: string, limit: number): Promise<BrainLabelRow[]>;

  /** Atoms whose label or aka (4 or more UTF-16 units, lowercased) appears
   *  word-bounded in q (lowercased by the store), each with its longest such
   *  name's length, sorted by (-len, id), at most `limit`. 3b adds a
   *  `truncated` flag (the bounded D1 pass stopped early) together with the
   *  bridge field that reports it.               [5]  D1: bridge candidates */
  statementAtoms(q: string, limit: number): Promise<Array<{ id: string; len: number }>>;

  /** Heads for ids: a cell gets its raw labels.json row, a supercell its head.
   *  An unknown id is absent. A cell row is exactly the labels.json row (see
   *  filterCells), never with `libs`. Static loads labels.json and
   *  supercells.json on every call, even for ids = [], as the bridge's partner
   *  labels did.                                          [500]  D1: labelRows */
  labelRows(ids: string[]): Promise<Map<string, AtomHead>>;

  /** For a supercell's synapse rows, the partner-side traces this store can
   *  supply, keyed by partner id (traces, plus tt where known). Static: the
   *  mirror rows in the shards of the first 50 traceless cell partners. D1:
   *  the trace table, traces trimmed to 6.     [200]  D1: synapseTraces */
  supercellTraces(atomId: string, rows: Synapse[]): Promise<Map<string, SynapseTraces>>;

  /** Facet-mask page of cells in labels.json order from ord `from`, at most
   *  `limit` rows. `under` is already trimmed; "" means no restriction. Static
   *  never returns a short page; D1 may (decision 7). The rows go straight into
   *  the response body, so they are labels.json rows exactly as the builder
   *  writes them: keys id, label, then f, aka and p when present, in that order,
   *  absent fields omitted, nothing else (no `libs`).
   *                                  [10,000 rows examined]  D1: filterCells */
  filterCells(mask: number, under: string, from: number, limit: number): Promise<FilterPage<BrainLabelRow>>;

  /** The same over supercells by their aggregate mask `fa`, in file order. Rows
   *  are {id, label ?? null, fa?, parent?, n_cells?}.
   *                             [10,000 rows examined]  D1: filterSupercells */
  filterSupercells(mask: number, under: string, from: number, limit: number): Promise<FilterPage<Record<string, unknown>>>;

  /** The first 40 distinct static xref pages of `node`, in index order. Static
   *  loads the xref index even when the node has none, so a missing index is a
   *  503 on every /edges call.                            [40]  D1: xrefPages */
  xrefPages(node: string): Promise<string[]>;

  /** For up to 40 pages, each page's static nodes in index order: at least the
   *  first 101 (static: all). A page with none is absent. D1: the builder
   *  writes node_ord dense from 0 per page, so `node_ord <= 100` on
   *  xrefs_page(ds, page, node_ord) reads at most 101 rows per page, with no
   *  window function; a builder change must keep it dense.
   *                                                  [4,040]  D1: xrefNodes */
  xrefNodes(pages: string[]): Promise<Map<string, string[]>>;
}

// Added in 3b together with their callers, so 3a ships no uncalled methods:
//   organOwners / organPages (keys <= 100)  quickstatements' one batch
//   atomsExist (ids <= 200)                 quickstatements, POST /api/brain/node,
//                                           brainNodeExists and atomIdForOrgan's
//                                           atom-id branch (through atomFor, each is
//                                           a whole cellPage read, for a boolean)
//   declOwners (names <= 30)                suggestRename: resolve every dead name of
//                                           a decl_exists batch, the premise seeds or
//                                           the bridge's organs in one call (keyHits
//                                           per name is N+1, up to 32 batches)
//   cellPages (ids <= 200)                  suggestRename batching, the page's `cells`
//   level(path), explorerScope(scope, part) the page endpoints
//   searchPage, bridgeCandidates            see "Round trips" in the header
// Moving quickstatements and POST /api/brain/node onto organOwners and
// atomsExist changes behavior: rows resolve one by one today, so an outage
// partway through a quickstatements submit leaves the earlier rows written,
// while resolving every endpoint first makes it all-or-nothing. 3b makes that
// change on purpose and says so; it is not part of the store swap.

let _storeOverride: ((c: Ctx, release: BrainReleaseContext) => BrainStore) | null = null;

/** test-only: answer every Brain read through `make` (null restores storeFor) */
export function _setBrainStoreForTests(
  make: ((c: Ctx, release: BrainReleaseContext) => BrainStore) | null,
): void {
  _storeOverride = make;
}

// One store per request and release, so a store may keep what it read for the
// rest of the request (the static store keeps the xref index /edges reads
// twice; a D1Store, its readiness result). Keyed weakly by the request's
// context, so nothing outlives the request.
const _requestStores = new WeakMap<Ctx, Map<string, BrainStore>>();

// The store for one release. 3b switches on the release's backend here.
export function storeFor(c: Ctx, release: BrainReleaseContext): BrainStore {
  let stores = _requestStores.get(c);
  if (!stores) {
    stores = new Map();
    _requestStores.set(c, stores);
  }
  let store = stores.get(release.releaseId);
  if (!store) {
    store = _storeOverride ? _storeOverride(c, release) : new StaticStore(c, release);
    stores.set(release.releaseId, store);
  }
  return store;
}
