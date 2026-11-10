// The Brain data interface (docs/BRAIN-D1.md section 8, steps 3a and 3b).
//
// Every read of a release's Brain data goes through a BrainStore: the routes in
// brain-api.ts, brain-edits.ts and (through them) mcp.ts keep validation,
// precedence, ranking, paging, cursors and response bodies, and the store only
// answers "what does this release hold". The StaticStore
// (brain-store-static.ts) reads the release's static JSON files exactly as the
// routes used to. The D1Store (brain-store-d1.ts, step 3b) implements the same
// methods with the bounded statements of docs/BRAIN-D1.md section 1; the
// bracket after each method below is its result bound there. storeFor picks
// the D1Store for a release whose release.json lists the serving-tables
// sidecar when BRAIN_DB is bound, and the StaticStore otherwise.
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
// is a caller bug, and the D1Store throws a plain Error (500) on one. The
// D1Store answers an empty input (ids = [], pages = []) without sending a
// batch. On a D1 release searchFor rejects a q over 200 UTF-16 units with a
// 400 (docs/BRAIN-D1.md section 1, Request bounds). Other callers of
// searchNames and searchCellNames (suggestionsFor, informalToFormal) may pass a
// longer q: for that q, nameTiers runs only the exact tier (and D1Store.tiers
// runs no tier for a cell q longer than longestName), so the bound is enforced
// in nameTiers, not by the callers.
//
// Round trips: the routes await these methods one after another, so a store
// cannot coalesce two calls into one batch behind the routes' backs. keyHits
// merges the alias and exact-name lookups for that reason, and the composite
// methods of BrainStoreBatches below carry the two routes whose method-by-
// method reads would cost more round trips than section 1 allows (search,
// bridge), plus the quickstatements prefetch. They are a separate interface,
// not BrainStore methods with a static version: the StaticStore keeps today's
// loads in today's order (the fetch-count tests pin that), so a route asks
// batchesOf(store) and, for a store without them, runs its old code
// unchanged. storeFor hands out one store per request and release, so the
// D1Store keeps its readiness result, and the rows it already read, for the
// rest of the request.
import type { Context } from "hono";
import type { Env } from "./env.js";
import type { BrainLabelRow, BrainReleaseContext } from "./brain.js";
import { StaticStore } from "./brain-store-static.js";
import { D1Store } from "./brain-store-d1.js";
export { PrefetchBudgetError } from "./brain-store-d1.js";

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

// What resolveAtomKey looks up for a key (brain-api.ts atomKeyQuery): an atom
// id it reads directly, or the alias and exact-name lookups. A composite takes
// it so it can run those lookups in its own batch; the caller applies
// precedence (brain-api.ts resolvedFromHits), as it does for keyHits.
export type AtomKeyQuery =
  | { atomId: string }
  | { lookup: KeyLookup; isDeclId: boolean };

export interface SearchPageResult {
  named: AtomHead[]; // what searchNames returns
  hits: KeyHits; // every key lookup that found its key (none for an atom id)
  heads: Map<string, AtomHead>; // the heads of every hit id, or of the atom id when it exists
}

export interface BridgeCandidatesResult {
  hits: KeyHits; // as above
  atomExists: boolean; // for an atom-id query: whether the atom exists
  names: string[]; // searchCellNames(q, 5)'s ids
  statement: Array<{ id: string; len: number }>; // statementAtoms(q, 5)
  statementTruncated: boolean; // the bounded pass stopped at one of its bounds
}

/** Per-route composites, for a store whose method-by-method reads would cost
 *  a round trip each (the D1Store). Each answers what its route's sequence of
 *  BrainStore calls would, in fewer batches. */
export interface BrainStoreBatches {
  /** searchFor's reads: searchNames(q, kind, limit), the key resolution of q,
   *  and the heads of both, in 2 batches.           [limit + 4 heads] */
  searchPage(q: string, kind: "" | "cell" | "supercell", limit: number, query: AtomKeyQuery | null): Promise<SearchPageResult>;
  /** bridgeFor's candidates: the key resolution of q, searchCellNames(q, 5)
   *  and statementAtoms(q, 5), the first chunk of the statement pass in the
   *  same batch.                            [1 + 5 + 5, up to 4 chunks] */
  bridgeCandidates(q: string, query: AtomKeyQuery | null): Promise<BridgeCandidatesResult>;
  /** Loads the organ owners, cell pages and supercells of node ids (organ
   *  keys, cell: and path: ids, and the supercells organ keys name) for the
   *  rest of the request, so quickstatements resolves every endpoint before
   *  its first write in a few batches. Throws PrefetchBudgetError when the
   *  pages pass the request's budget, before the caller writes anything.
   *  [50 keys a batch; 8 M characters of pages kept; pages over the batch
   *  caps read in one more batch] */
  prefetchAtoms(ids: string[]): Promise<void>;
}

/** The store's composites, or null for a store without them (the StaticStore
 *  and every test store built on it), whose routes then run their old reads. */
export function batchesOf(store: BrainStore): BrainStoreBatches | null {
  const s = store as unknown as Partial<BrainStoreBatches>;
  return typeof s.searchPage === "function" &&
    typeof s.bridgeCandidates === "function" &&
    typeof s.prefetchAtoms === "function"
    ? (store as unknown as BrainStoreBatches)
    : null;
}

/** For a write route (POST node, POST edge, quickstatements): the D1 store
 *  starts every later batch with the readiness check, so a dataset that
 *  leaves `ready` mid-request answers 503 instead of "not a node", which would
 *  mint a community node (docs/BRAIN-D1.md section 1). Other stores ignore it. */
export function checkReadinessEveryBatch(store: BrainStore): void {
  if (store instanceof D1Store) store.checkEveryBatch();
}

// Still deferred, until a caller needs them: atomsExist (POST /api/brain/node
// and brainNodeExists read a whole cell page for a boolean; the D1Store keeps
// it for the request), declOwners (suggestRename resolves dead names one
// keyHits call each), cellPages, and level(path) / explorerScope(scope, part)
// for the page endpoints of step 3b's second PR.

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

// The store for one release: the D1Store for a release that lists the serving
// sidecar when BRAIN_DB is bound, else the StaticStore. A D1 release still
// carries every static file until the layout rule (step 3b's second PR), so
// an unbound BRAIN_DB serves it from those files.
export function storeFor(c: Ctx, release: BrainReleaseContext): BrainStore {
  let stores = _requestStores.get(c);
  if (!stores) {
    stores = new Map();
    _requestStores.set(c, stores);
  }
  let store = stores.get(release.releaseId);
  if (!store) {
    store = _storeOverride
      ? _storeOverride(c, release)
      : release.backend === "d1" && c.env.BRAIN_DB
        ? new D1Store(c, release)
        : new StaticStore(c, release);
    stores.set(release.releaseId, store);
  }
  return store;
}
