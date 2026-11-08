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
// Round trips (for 3b): the routes await these methods one after another, so a
// D1Store cannot coalesce two calls into one batch behind the routes' backs.
// keyHits already merges the alias and exact-name lookups for that reason.
// Section 1's per-route counts for search (2) and bridge (2 or 3) still need
// 3b either to start a route's independent reads together (the key
// resolution, the name search, the statement pass) or to add a per-route
// composite method; static releases keep today's sequential order, which the
// fetch-count tests pin.
import type { Context } from "hono";
import type { Env } from "./env.js";
import type { BrainLabelRow, BrainReleaseContext } from "./brain.js";
import type { CellEntry, SupercellEntry, Synapse } from "./brain-api.js"; // types only: no new runtime cycle
import { StaticStore } from "./brain-store-static.js";

type Ctx = Context<{ Bindings: Env }>;

export type Crumb = { id: string; label?: string | null };

// The head fields a supercell search hit or label lookup needs, never the whole
// row (a supercells.json entry also holds up to 200 synapses and 5,000 cell
// ids). `aka` is every organ's `label ?? id`, in organ order, duplicates kept,
// present only when the supercell has organs; `n_cells` is present only when
// its `cells` list is non-empty.
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
   *  tiers, cells and supercells interleaved per tier.  [2 x limit]  D1: search */
  searchNames(q: string, kind: "" | "cell" | "supercell", limit: number): Promise<AtomHead[]>;

  /** Cell name hits only, at most `limit`, for suggestions, the transfer
   *  fallback and bridge candidates. `q` is lowercased by the store. Static:
   *  searchLabels over labels.json only.     [5]  D1: search, cells only */
  searchCellNames(q: string, limit: number): Promise<BrainLabelRow[]>;

  /** Atoms whose label or aka (4 or more UTF-16 units, lowercased) appears
   *  word-bounded in q (lowercased by the store), each with its longest such
   *  name's length, sorted by (-len, id), at most `limit`. `truncated`: the
   *  scan stopped at a bound before finishing (static: always false).
   *                                                [5]  D1: bridge candidates */
  statementAtoms(q: string, limit: number): Promise<{ hits: Array<{ id: string; len: number }>; truncated: boolean }>;

  /** Heads for ids: a cell gets its raw labels.json row, a supercell its head.
   *  An unknown id is absent. Static loads labels.json and supercells.json on
   *  every call, even for ids = [], as the bridge's partner labels did.
   *                                                       [500]  D1: labelRows */
  labelRows(ids: string[]): Promise<Map<string, AtomHead>>;

  /** For a supercell's synapse rows, the partner-side rows whose traces this
   *  store can supply, keyed by partner id (traces, plus tt where known). Static:
   *  the mirror rows in the shards of the first 50 traceless cell partners. D1:
   *  the trace table, traces trimmed to 6.     [200]  D1: synapseTraces */
  supercellTraces(atomId: string, rows: Synapse[]): Promise<Map<string, Synapse>>;

  /** Facet-mask page of cells in labels.json order from ord `from`, at most
   *  `limit` rows (raw label rows). `under` is already trimmed; "" means no
   *  restriction. Static never returns a short page; D1 may (decision 7).
   *                                  [10,000 rows examined]  D1: filterCells */
  filterCells(mask: number, under: string, from: number, limit: number): Promise<FilterPage<BrainLabelRow>>;

  /** The same over supercells by their aggregate mask `fa`, in file order. Rows
   *  are {id, label ?? null, fa?, parent?, n_cells?}.
   *                             [10,000 rows examined]  D1: filterSupercells */
  filterSupercells(mask: number, under: string, from: number, limit: number): Promise<FilterPage<Record<string, unknown>>>;

  /** The first 40 distinct static xref pages of `node`, in index order.
   *                                                        [40]  D1: xrefPages */
  xrefPages(node: string): Promise<string[]>;

  /** For up to 40 pages, each page's static nodes in index order: at least the
   *  first 101 (static: all). A page with none is absent. [4,040]  D1: xrefNodes */
  xrefNodes(pages: string[]): Promise<Map<string, string[]>>;
}

// Added in 3b together with their callers, so 3a ships no uncalled methods:
//   organOwners / organPages (keys <= 100)  quickstatements' one batch
//   atomsExist (ids <= 200)                 quickstatements, POST /api/brain/node
//   cellPages (ids <= 200)                  suggestRename batching, the page's `cells`
//   level(path), explorerScope(scope, part) the page endpoints

let _storeOverride: ((c: Ctx, release: BrainReleaseContext) => BrainStore) | null = null;

/** test-only: answer every Brain read through `make` (null restores storeFor) */
export function _setBrainStoreForTests(
  make: ((c: Ctx, release: BrainReleaseContext) => BrainStore) | null,
): void {
  _storeOverride = make;
}

// The store for one release. It holds no state of its own (every memo stays
// module-level, keyed by release identity), so building one per call is free.
// 3b switches on the release's backend here.
export function storeFor(c: Ctx, release: BrainReleaseContext): BrainStore {
  if (_storeOverride) return _storeOverride(c, release);
  return new StaticStore(c, release);
}
