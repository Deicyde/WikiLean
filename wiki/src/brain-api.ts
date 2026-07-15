// Wikibrain agent API (BRAIN v3 — docs/BRAIN-API.md, docs/BRAIN-V3.md).
//
// Read-only query routes over the CELL shards (/assets/brain/cells/). The
// addressable thing is the **cell** — an atom of organs — not the v2 particle:
// a Mathlib decl, a Wikidata concept, an external DB page, a WikiLean article
// and an arXiv statement that all denote ONE object are organs of one cell.
// Modules are **supercells** (`path:…`) which own organs of their own (rule 5's
// field concepts) and carry synapses. Weak bonds between two atoms aggregate
// into ONE **synapse** carrying every trace.
//
//   GET /api/brain/cell?key=                       any organ id → the atom card
//   GET /api/brain/unit?key=                       alias of /cell (v2 entry point)
//   GET /api/brain/transfer?q=&direction=&limit=   informal ↔ formal jump
//   GET /api/brain/neighborhood?id=&kinds=&limit=  synapses (weight, kinds, traces)
//   GET /api/brain/snippets?id=                    stored source snippets
//   GET /api/brain/filter?f=&type=&under=&limit=&cursor=   facet enumeration
//   GET /api/brain/search?q=&type=&limit=          label + `aka` search
//   GET /brain/api                                 human-readable reference
//
// All logic lives in exported `*For()` helpers returning {status, body} so the
// MCP endpoint (src/mcp.ts) calls the SAME code paths — the two surfaces cannot
// drift.
//
// **aliases.json is the compat layer**: a v2 entry point (a QID, a decl id or
// bare name, an article slug, an `xref:` page id, a `lit:` statement) maps to
// the atom that owns it. A rule-5 field concept resolves to a SUPERCELL (Q82571
// → path:Mathlib/LinearAlgebra), which is why every route speaks
// `Atom = cell | supercell`.
//
// It is NOT total, and the older claim here — "nothing that resolved before the
// cell cut 404s now" — was false by 47,990 of the v2 index's 66,746 ids. What
// holds, measured against site/assets/brain/labels.json: every v2 concept
// (2,674 QIDs), decl (3,303) and container (9,052 paths) resolves, and so does
// every article slug. What does not is exactly what v3 DROPPED on purpose
// (docs/BRAIN-V3.md "Dropped in v3"): 45,996 unanchored frontier ext pages
// (anchored ones — 3,610 — still resolve) and 1,994 arXiv paper nodes. Those
// 404 with a `reason` naming the drop (see droppedInV3); they never pretend the
// id is unknown. `/api/brain/node` still serves them from the v2 shards, which
// is why the two route families answer differently for the same id.
//
// Everything here is shard/asset-backed and safe to cache for the nightly
// rebuild cadence (Cache-Control public, max-age=3600).
import type { Context, Hono } from "hono";
import type { Env } from "./env.js";
import {
  assetJson,
  memoAssetJson,
  searchLabels,
  BRAIN_ID_RE,
  type BrainLabelRow,
} from "./brain.js";
import { declShardFor, docsUrlFor, lookupInShard } from "./decl.js";

type Ctx = Context<{ Bindings: Env }>;

// Helper results carry a JSON body + the HTTP status the REST route would use;
// the MCP layer maps status>=400 to a tool result with isError:true.
export type ApiStatus = 200 | 400 | 404 | 503;
export interface ApiResult {
  status: ApiStatus;
  body: Record<string, unknown>;
}

const SITE_ORIGIN = "https://wikilean.jackmccarthy.org";
const CELLS = "/assets/brain/cells"; // the v3 asset namespace
const QID_RE = /^Q[1-9][0-9]{0,11}$/;
const XREF_ID_RE = /^xref:([a-z0-9_]+):(.+)$/i;
const KEY_HINT =
  "accepted key forms: cell:<anchor> | path:<Lib>/<Dir> (supercell) | QID | " +
  "decl:<Lib>:<Name> | bare FQ decl name | article slug | xref:<db>:<id> | " +
  "lit:<arxiv>#<ref> | exact label — for fuzzy text use /api/brain/search?q=";

// Mathlib's own license, per catalog/data/source_registry.json node_sources.mathlib
// (`target_license`) — the provenance single-source-of-truth. Carried on the decl
// rows /api/brain/snippets emits so no source text ever ships unattributed.
const MATHLIB_LICENSE = "Apache-2.0 (Mathlib)";

// ---- shipped shapes (brain/build_cell_shards.py output, brain/SCHEMA.md#v3) ----

// An organ is a PARTICLE, never a node: it exists only inside a cell (or, for
// rule-5 field concepts and area pages, inside a supercell). Payloads are
// EMBEDDED by the builder — one shard fetch renders the whole card, so no route
// below fans out to fetch an organ's content.
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
  breadcrumb?: Array<{ id: string; label?: string | null }>;
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

interface SupercellsFile {
  roots?: string[];
  supercells?: Record<string, SupercellEntry>;
}

interface CellsManifest {
  scheme: { min_len: number; max_len: number; pad: string };
  shards: Record<string, number>;
  prov: Array<Record<string, string>>;
  roots?: string[];
  _meta?: Record<string, unknown>;
}

// aliases.json — THE compat layer. `organs` maps every organ id (QID, decl id,
// xref page id, article slug, lit statement) to the atom that owns it, which is
// a cell id or — for rule-5 field concepts — a supercell path. `decls`/`slugs`
// are convenience indexes (bare FQ decl name / slug → atom).
interface CellAliases {
  organs?: Record<string, string>;
  decls?: Record<string, string>;
  slugs?: Record<string, string>;
}

// The normalized atom every route works with: a cell or a supercell.
export interface Atom {
  id: string;
  kind: "cell" | "supercell";
  label: string | null;
  f?: number;
  organs: Organ[];
  syn: Synapse[];
  counts: { syn: number; organs: number };
  truncated?: { syn?: number };
  breadcrumb?: Array<{ id: string; label?: string | null }>;
  cell?: CellHead; // kind==="cell"
  supercell?: { path: string; parent?: string; children?: string[]; cells?: string[]; fa?: number }; // kind==="supercell"
}

// ---- small utilities ---------------------------------------------------------

// own-property read — a JSON.parse'd map must never serve inherited names
// (__proto__/constructor/toString), same gotcha as /api/atlas/:key.
function own<T>(map: Record<string, T> | undefined, key: string): T | undefined {
  return map && Object.prototype.hasOwnProperty.call(map, key) ? map[key] : undefined;
}

function intOr(v: unknown, dflt: number): number {
  const n = typeof v === "number" ? v : typeof v === "string" && v.trim() !== "" ? Number(v) : NaN;
  return Number.isFinite(n) ? Math.floor(n) : dflt;
}

function clampLimit(v: unknown, dflt: number, max: number): number {
  return Math.min(Math.max(intOr(v, dflt), 1), max);
}

function isAtomId(id: string): boolean {
  return id.startsWith("cell:") || id.startsWith("path:");
}

// ---- asset loaders (all memoized — see brain.ts memoAssetJson) ----------------

function cellsManifest(c: Ctx): Promise<CellsManifest | null> {
  return memoAssetJson<CellsManifest>(c, `${CELLS}/manifest.json`);
}

function cellAliases(c: Ctx): Promise<CellAliases | null> {
  return memoAssetJson<CellAliases>(c, `${CELLS}/aliases.json`);
}

function cellLabels(c: Ctx): Promise<BrainLabelRow[] | null> {
  return memoAssetJson<BrainLabelRow[]>(c, `${CELLS}/labels.json`);
}

function supercellsFile(c: Ctx): Promise<SupercellsFile | null> {
  return memoAssetJson<SupercellsFile>(c, `${CELLS}/supercells.json`);
}

// One cell shard entry, via the manifest's documented prefix scheme (identical
// to the decl-index scheme, so declShardFor resolves it verbatim).
async function cellEntry(c: Ctx, id: string): Promise<CellEntry | null> {
  const manifest = await cellsManifest(c);
  if (!manifest?.shards) return null;
  const key = declShardFor(
    { scheme: manifest.scheme, shards: manifest.shards },
    id,
  );
  const shard = key ? await assetJson<Record<string, unknown>>(c, `${CELLS}/${key}.json`) : null;
  const entry = shard ? own(shard, id) : undefined;
  return entry ? (entry as CellEntry) : null;
}

// Many cell entries at once, grouped by shard so N partners living in ONE shard
// cost ONE fetch (the supercell-trace hydration below is the only caller, and
// its fan-out is what has to stay bounded).
async function cellEntries(c: Ctx, ids: string[]): Promise<Map<string, CellEntry>> {
  const out = new Map<string, CellEntry>();
  const manifest = await cellsManifest(c);
  if (!manifest?.shards) return out;
  const byShard = new Map<string, string[]>();
  for (const id of ids) {
    const key = declShardFor({ scheme: manifest.scheme, shards: manifest.shards }, id);
    if (!key) continue;
    const want = byShard.get(key);
    if (want) want.push(id);
    else byShard.set(key, [id]);
  }
  await Promise.all(
    [...byShard].map(async ([key, want]) => {
      const shard = await assetJson<Record<string, unknown>>(c, `${CELLS}/${key}.json`);
      if (!shard) return;
      for (const id of want) {
        const e = own(shard, id);
        if (e) out.set(id, e as CellEntry);
      }
    }),
  );
  return out;
}

// A licensed snippet must NEVER ship without its licence (SCHEMA S6). The
// builder guarantees the pair, but the API enforces it too — a data regression
// upstream must degrade to "no snippet", never to unlicensed text.
function safeOrgan(o: Organ): Organ {
  if (o.snippet !== undefined && !o.snippet_license) {
    const { snippet: _drop, ...rest } = o;
    return rest as Organ;
  }
  return o;
}

// The shard caps every atom's synapse LIST (`caps.synapses_per_cell` = 200) but
// `counts.syn` keeps the TRUE total, so the withheld count is ARITHMETIC — never
// a field we hope the builder wrote. It must be, because supercells.json ships
// no `truncated` on any of its 9,052 entries: reading one there yields
// undefined, and a supercell withholding 728 of 928 synapses then reports
// "nothing withheld". SCHEMA v3 is explicit that a cap applies "never silently:
// whatever a cap drops is counted in `truncated` (a COUNT, not a flag)".
//
// `declared` (a cell's builder-written count) is folded in with max(), so the
// API tells the truth whichever side drifts.
function synTruncation(
  total: number,
  shipped: number,
  declared?: { syn?: number },
): { syn: number } | undefined {
  const withheld = Math.max(total - shipped, declared?.syn ?? 0, 0);
  return withheld > 0 ? { syn: withheld } : undefined;
}

// Walk `parent` to the root — supercells.json is a tree, so the breadcrumb is
// derived rather than stored (cells ship theirs prebuilt).
function supercellBreadcrumb(
  map: Record<string, SupercellEntry>,
  path: string,
): Array<{ id: string; label?: string | null }> {
  const crumbs: Array<{ id: string; label?: string | null }> = [];
  const seen = new Set<string>();
  let cur: string | undefined = own(map, path)?.parent;
  while (cur && !seen.has(cur)) {
    seen.add(cur);
    const e: SupercellEntry | undefined = own(map, cur);
    crumbs.push({ id: cur, label: e?.label ?? null });
    cur = e?.parent;
  }
  return crumbs.reverse();
}

// Fetch an atom by its OWN id (cell:… | path:…). Callers that hold an organ id
// must go through resolveAtomKey first.
export async function atomFor(c: Ctx, id: string): Promise<Atom | null> {
  if (!BRAIN_ID_RE.test(id)) return null;
  if (id.startsWith("path:")) {
    const file = await supercellsFile(c);
    const map = file?.supercells;
    const e = map ? own(map, id) : undefined;
    if (!e || !map) return null;
    const organs = (e.organs ?? []).map(safeOrgan);
    const syn = e.syn ?? [];
    const counts = { syn: e.counts?.syn ?? syn.length, organs: organs.length };
    const truncated = synTruncation(counts.syn, syn.length);
    return {
      id,
      kind: "supercell",
      label: e.label ?? null,
      // deliberately NO `f`: a supercell carries `fa`, the subtree-AGGREGATE
      // mask ("something under here matches"), which is a different claim from
      // a cell's own facets. It ships as `supercell.fa` so the two never blur.
      organs,
      syn,
      counts,
      // supercells.json carries no `truncated` — derive it (see synTruncation)
      ...(truncated ? { truncated } : {}),
      breadcrumb: supercellBreadcrumb(map, id),
      supercell: {
        path: id,
        ...(e.parent ? { parent: e.parent } : {}),
        ...(e.children?.length ? { children: e.children } : {}),
        ...(e.cells?.length ? { cells: e.cells } : {}),
        ...(e.fa !== undefined ? { fa: e.fa } : {}),
      },
    };
  }
  const e = await cellEntry(c, id);
  if (!e?.cell) return null;
  const organs = (e.organs ?? []).map(safeOrgan);
  const syn = e.syn ?? [];
  const counts = {
    syn: e.counts?.syn ?? syn.length,
    organs: e.counts?.organs ?? organs.length,
  };
  const truncated = synTruncation(counts.syn, syn.length, e.truncated);
  return {
    id: e.cell.id ?? id,
    kind: "cell",
    label: e.cell.label ?? null,
    ...(e.cell.f !== undefined ? { f: e.cell.f } : {}),
    organs,
    syn,
    counts,
    ...(truncated ? { truncated } : {}),
    ...(e.breadcrumb ? { breadcrumb: e.breadcrumb } : {}),
    cell: e.cell,
  };
}

export type ResolvedFrom = "cell" | "supercell" | "organ" | "decl" | "slug" | "label";

export interface ResolvedKey {
  id: string;
  resolved_from: ResolvedFrom;
  atom?: Atom; // set when resolution already fetched it
}

// Resolve ANY key to the atom that owns it.
//
// Order: an atom id resolves directly; otherwise aliases.json — `organs` first
// (it holds every organ id: QIDs, decl ids, xref pages, slugs, lit statements),
// then the bare-decl-name and slug convenience indexes; finally an exact label
// or `aka` (an organ's label — searching "Vector space" must land on the Module
// atom). aliases.json IS the compat layer, so a miss there is a real miss: the
// v2 fallbacks (shard in-edges, ext-node `qid`) have no v3 analogue — organs
// carry no inbound edges, they ARE the atom's content.
export async function resolveAtomKey(c: Ctx, keyRaw: string): Promise<ResolvedKey | null> {
  const key = keyRaw.trim();
  if (!key || !BRAIN_ID_RE.test(key)) return null;

  if (isAtomId(key)) {
    const atom = await atomFor(c, key);
    if (atom) return { id: atom.id, resolved_from: atom.kind, atom };
    return null; // an explicit atom id must not fall through to label search
  }

  const aliases = await cellAliases(c);

  // Every organ id — QID, decl:<Lib>:<Name>, xref:<db>:<id>, slug, lit:… —
  // lands here. The value may be a supercell path (rule-5 field concepts).
  const viaOrgan = own(aliases?.organs, key);
  if (viaOrgan) return { id: viaOrgan, resolved_from: "organ" };

  // bare fully-qualified decl name ("CommGroup"), and decl:<Lib>:<Name> whose
  // library differs from the alias table's
  const isDeclId = key.startsWith("decl:");
  const bareName = isDeclId ? key.split(":").slice(2).join(":") : key;
  const viaDecl = own(aliases?.decls, bareName);
  if (viaDecl) return { id: viaDecl, resolved_from: "decl" };
  if (isDeclId) return null; // an explicit decl id must not fall through to labels

  const viaSlug = own(aliases?.slugs, key);
  if (viaSlug) return { id: viaSlug, resolved_from: "slug" };

  // exact label / aka, case-insensitive, over the atom label index
  const labels = await cellLabels(c);
  const kl = key.toLowerCase();
  const byLabel = labels?.find(
    (r) =>
      (r.label || "").toLowerCase() === kl ||
      (r.aka || []).some((a) => a.toLowerCase() === kl),
  );
  if (byLabel) return { id: byLabel.id, resolved_from: "label" };

  // supercell labels are not in labels.json (it indexes cells) — match a
  // field concept's own label through the supercell's organs.
  //
  // `aliases.json.slugs` indexes CELL slugs only and supercell organs ship no
  // `slug` at all, so a rule-5 field concept's own article slug missed every
  // index above and 404'd — 61 of them, including `Linear_algebra`, the very
  // example the docs give for a slug resolving. An enwiki slug IS the title with
  // spaces underscored (SCHEMA: "an article is the `enwiki` sitelink of its
  // concept QID"), so undo that and the organ label matches. Cells are matched
  // first, above, and so still win any collision.
  const kSlug = kl.replace(/_/g, " ");
  const file = await supercellsFile(c);
  for (const [path, e] of Object.entries(file?.supercells ?? {})) {
    if (
      (e.organs ?? []).some((o) => {
        const ol = (o.label || "").toLowerCase();
        return ol === kl || ol === kSlug;
      })
    ) {
      return { id: path, resolved_from: "label" };
    }
  }
  return null;
}

// v3 drops two whole v2 populations (docs/BRAIN-V3.md "Dropped in v3"), so their
// ids have no atom and MUST 404 — but a bare "unresolvable key" reads as "the
// Brain has never heard of this", which is false and contradicted the (now
// corrected) promise that every v2 entry point resolves. Name the reason.
// Measured against site/assets/brain/labels.json: 45,996 ext pages + 1,994 paper
// nodes = the 47,990 v2 ids with no v3 atom.
function droppedInV3(key: string): string | null {
  if (XREF_ID_RE.test(key)) {
    return (
      "this external page is an ORGAN, and no cell claims it — v3 drops the ~46k unanchored " +
      "frontier ext pages that carried no concept-level connectivity (docs/BRAIN-V3.md " +
      '"Dropped in v3"), so it has no atom to return. Anchored pages (a cell\'s xref target) ' +
      "do resolve. The full corpus stays in catalog/data/external/; the page's second-order " +
      "signal survives as a cell↔cell `co-page` synapse."
    );
  }
  if (/^lit:[^#]+$/.test(key)) {
    return (
      "this is an arXiv PAPER node; v3 has no paper atom — only STATEMENTS a cell claims " +
      "(lit:<arxiv>#<ref>) are organs. A shared statement between two cells is a " +
      "`co-statement` synapse (SCHEMA rule 4), so read the paper's role off /api/brain/neighborhood."
    );
  }
  return null;
}

function pickSuggestion(r: BrainLabelRow): Record<string, unknown> {
  return {
    id: r.id,
    label: r.label,
    ...(r.aka?.length ? { aka: r.aka } : {}),
    ...(r.p ? { supercell: r.p } : {}),
  };
}

async function suggestionsFor(c: Ctx, text: string): Promise<Record<string, unknown>[]> {
  const q = text.trim().toLowerCase();
  if (q.length < 2) return [];
  const labels = await cellLabels(c);
  return labels ? searchLabels(labels, q, "", 5).map(pickSuggestion) : [];
}

// ---- the atom card (v3's addressable unit, served) ----------------------------

const SYN_PREVIEW = 10; // strongest partners inlined on the card; full list via /neighborhood

// Decl organs rank `exact` bonds first, then any other graded bond, then an
// ungraded one (the anchor decl of a lone-particle cell carries no bond at all),
// then name. v3 organs carry `bond` + `prov`, NOT the v2 `confidence` —
// confidence lives on the grounding edge the builder consumed.
function rankDecl(a: Organ, b: Organ): number {
  const rank = (o: Organ) => (o.bond === "exact" ? 0 : o.bond ? 1 : 2);
  const ra = rank(a), rb = rank(b);
  if (ra !== rb) return ra - rb;
  const la = a.label ?? a.id, lb = b.label ?? b.id;
  return la < lb ? -1 : la > lb ? 1 : 0;
}

function organsOf(atom: Atom, kind: string): Organ[] {
  return atom.organs.filter((o) => o.kind === kind);
}

function organsByKind(atom: Atom): Record<string, number> {
  const out: Record<string, number> = {};
  for (const o of atom.organs) out[o.kind] = (out[o.kind] ?? 0) + 1;
  return out;
}

// kind:count across every synapse on the atom (the shard caps the LIST at
// `caps.synapses_per_cell`; `counts.syn` is the true total).
function synapsesSummary(atom: Atom): Record<string, number> {
  const out: Record<string, number> = {};
  for (const s of atom.syn) {
    for (const [k, n] of Object.entries(s.kinds ?? {})) out[k] = (out[k] ?? 0) + n;
  }
  return out;
}

// The atom card: the cell/supercell head, every organ WITH its embedded payload
// (Lean code, Wikidata description, licensed DB snippets, article annotation
// counts), the containment breadcrumb, and a synapse summary + strongest
// partners. Traces are deliberately NOT here — /api/brain/neighborhood serves
// them, so the card stays an identity answer rather than a graph dump.
export async function cellFor(c: Ctx, keyRaw: string): Promise<ApiResult> {
  const key = (keyRaw || "").trim();
  if (!key || !BRAIN_ID_RE.test(key)) {
    return { status: 400, body: { ok: false, error: "missing or malformed ?key=", hint: KEY_HINT } };
  }
  const resolved = await resolveAtomKey(c, key);
  if (!resolved) {
    const dropped = droppedInV3(key);
    return {
      status: 404,
      body: {
        ok: false,
        error: dropped ? "no atom owns this organ id" : "unresolvable key",
        key,
        ...(dropped ? { reason: dropped } : {}),
        hint: KEY_HINT,
      },
    };
  }
  const atom = resolved.atom ?? (await atomFor(c, resolved.id));
  if (!atom) {
    return {
      status: 404,
      body: { ok: false, error: "resolved atom is not in the brain shards", key, id: resolved.id },
    };
  }
  const preview = [...atom.syn]
    .sort((a, b) => b.w - a.w)
    .slice(0, SYN_PREVIEW)
    .map((s) => ({ id: s.id, w: s.w, kinds: s.kinds }));
  return {
    status: 200,
    body: {
      ok: true,
      resolved_from: resolved.resolved_from,
      key,
      id: atom.id,
      kind: atom.kind,
      label: atom.label,
      ...(atom.f !== undefined ? { f: atom.f } : {}),
      ...(atom.cell ? { cell: atom.cell } : {}),
      ...(atom.supercell ? { supercell: atom.supercell } : {}),
      organs: atom.organs,
      organs_by_kind: organsByKind(atom),
      ...(atom.breadcrumb ? { breadcrumb: atom.breadcrumb } : {}),
      synapses_summary: synapsesSummary(atom),
      synapses_preview: preview,
      counts: atom.counts,
      ...(atom.truncated ? { truncated: atom.truncated } : {}),
    },
  };
}

// ---- transfer: the informal ↔ formal jump (the flagship agent call) ----------

export async function transferFor(
  c: Ctx,
  qRaw: string,
  direction: string,
  limitRaw?: unknown,
): Promise<ApiResult> {
  const q = (qRaw || "").trim();
  if (!q) return { status: 400, body: { ok: false, error: "missing ?q=" } };
  const limit = clampLimit(limitRaw, 10, 50);
  if (direction === "informal_to_formal") return informalToFormal(c, q, limit);
  if (direction === "formal_to_informal") return formalToInformal(c, q, limit);
  return {
    status: 400,
    body: { ok: false, error: "direction must be informal_to_formal or formal_to_informal" },
  };
}

function declHit(o: Organ, atom: Atom): Record<string, unknown> {
  const name = o.label ?? o.id.split(":").slice(2).join(":");
  return {
    decl: name,
    module: o.module ?? null,
    bond: o.bond ?? null,
    decl_kind: o.decl_kind ?? null,
    docs_url: o.module ? docsUrlFor(o.module, name) : `${SITE_ORIGIN}/decl/${encodeURIComponent(name)}`,
    via_cell: atom.id,
    cell_label: atom.label,
  };
}

// Concept → the formal side. With cells this is "resolve to the atom, read its
// decl organs" — no edge walk: an atom's decls ARE its own organs by the merge
// function (`exact` fuses both ways), which is exactly why Vector space and
// Module answer identically.
async function informalToFormal(c: Ctx, q: string, limit: number): Promise<ApiResult> {
  let resolved = await resolveAtomKey(c, q);
  let resolvedFrom: string | null = resolved?.resolved_from ?? null;
  if (!resolved && q.length >= 2) {
    // free text: best label/aka search hit
    const labels = await cellLabels(c);
    const hits = labels ? searchLabels(labels, q.toLowerCase(), "", 5) : [];
    if (hits.length) {
      resolved = { id: hits[0].id, resolved_from: "label" };
      resolvedFrom = "search";
    }
  }
  if (!resolved) {
    return {
      status: 404,
      body: {
        ok: false,
        error: "no atom matched q",
        q,
        suggestions: await suggestionsFor(c, q),
        hint: "try /api/brain/search?q= for fuzzy lookup",
      },
    };
  }
  const atom = resolved.atom ?? (await atomFor(c, resolved.id));
  if (!atom) {
    return { status: 404, body: { ok: false, error: "atom not in the brain shards", id: resolved.id } };
  }
  const hits = organsOf(atom, "decl").sort(rankDecl).slice(0, limit).map((o) => declHit(o, atom));
  const body: Record<string, unknown> = {
    ok: true,
    direction: "informal_to_formal",
    q,
    resolved_from: resolvedFrom,
    id: atom.id,
    kind: atom.kind,
    label: atom.label,
    hits,
  };
  if (atom.kind === "supercell") {
    // rule 5: a field-of-study concept's formal home is a FOLDER, not a decl.
    // That is the honest answer, not an empty result — say so.
    body.note =
      "this is a field-of-study concept: its formal home is a Mathlib folder (supercell), not a single declaration";
    body.container = atom.id;
    body.cells_in_container = atom.supercell?.cells?.length ?? 0;
  } else if (!hits.length) {
    body.note = "no Mathlib declaration is an organ of this atom";
    if (atom.cell?.supercells?.length) body.containers = atom.cell.supercells;
    body.suggestions = await suggestionsFor(c, q);
  }
  return { status: 200, body };
}

// Decl name → the informal side: the atom's concept + article organs. A decl
// resolves to exactly ONE atom (aliases.json is a function — SCHEMA C4), and
// that atom's concept organs are the multi-to-multi answer v2 walked in-edges
// for (Module → Q18848 AND Q125977, one fetch).
async function formalToInformal(c: Ctx, q: string, limit: number): Promise<ApiResult> {
  const name = q.startsWith("decl:") ? q.split(":").slice(2).join(":") : q;
  const resolved = await resolveAtomKey(c, q.startsWith("decl:") ? q : `decl:Mathlib:${q}`)
    ?? (await resolveAtomKey(c, name));
  const atom = resolved ? resolved.atom ?? (await atomFor(c, resolved.id)) : null;
  const hits: Record<string, unknown>[] = [];
  if (atom) {
    const pages = organsOf(atom, "page");
    for (const o of organsOf(atom, "concept").slice(0, limit)) {
      const slug = o.slug ?? null;
      hits.push({
        qid: o.id,
        label: o.label ?? null,
        bond: o.bond ?? null,
        slug,
        article_url: slug ? `${SITE_ORIGIN}/${encodeURIComponent(slug)}` : null,
        description: o.description ?? null,
        snippet_sources: [...new Set(pages.map((p) => p.db ?? "").filter(Boolean))].sort(),
        via_cell: atom.id,
      });
    }
  }
  const body: Record<string, unknown> = {
    ok: true,
    direction: "formal_to_informal",
    q,
    decl: name,
    ...(atom ? { id: atom.id, kind: atom.kind, label: atom.label } : {}),
    hits,
  };
  if (!hits.length) {
    body.note = atom
      ? "the decl's atom holds no concept organ — it is a formal-only cell (see organs on /api/brain/cell)"
      : "decl is not an organ of any atom — it may still exist in Mathlib (check decl_exists or /decl/<name>)";
    body.suggestions = await suggestionsFor(c, name.split(".").pop() ?? name);
  }
  return { status: 200, body };
}

// ---- neighborhood: an atom's synapses ----------------------------------------

// THE synapse-kind set, derived from every `kinds` key in brain/data/synapses.jsonl
// and re-verified against the shipped shards (both yield exactly these 11, ordered
// by bond count). Agent-facing surfaces MUST render this list rather than restate
// one, which is how the previous enum came to be wrong in both directions at once.
//
// `formalizes` and `matches` are deliberately ABSENT: the merge function CONSUMES
// them as organ attachments (an `exact` formalizes fuses a concept and a decl into
// one cell — SCHEMA rule 1), so they are never a bond BETWEEN atoms. They are read
// off an organ's `bond` on the cell card. Asking for them here matched 0 rows on
// every atom, while the five rule-3/4/2 kinds they crowded out — `co-page`,
// `co-statement`, `related`, `special_case`, `generalization` — carry 2,326 real
// bonds that a caller trusting the old enum silently dropped.
export const SYNAPSE_KINDS = [
  "depends",
  "links",
  "mentions",
  "cites",
  "relates",
  "co-page",
  "co-statement",
  "invocation",
  "related",
  "special_case",
  "generalization",
] as const;

export const SYNAPSE_KINDS_CSV = SYNAPSE_KINDS.join(",");

// v2 returned raw per-particle edges; v3 returns SYNAPSES — one aggregated edge
// per atom pair, carrying `w` (every constituent bond), a `kinds` histogram and
// the individual `traces`. There is no `dir`: a synapse is an UNDIRECTED
// aggregate of bonds that may run either way, and direction lives on each trace.

// Rows hydrated from partner shards per request (below). Each row costs at most
// one shard fetch, so this bounds the fan-out; it equals the default `limit`, so
// an unmodified call is fully hydrated. Whatever it drops is DECLARED per row
// (`traces_unavailable`) and counted in `traces_hydrated` — never silent.
const TRACE_HYDRATION_MAX = 50;

const TRACES_ELSEWHERE = "brain/query.py --full serves the untruncated set";

// Supercell `syn` rows ship traceless: supercells.json is fetched eagerly and
// carrying them would treble it. That file's own `_meta.traces` names THIS
// endpoint as the remedy — and it was a dead pointer, because the Worker reads
// only static shards and no shard carries supercell traces.
//
// It does not need one. A synapse is SYMMETRIC and ships on BOTH endpoints, so
// the partner CELL's shard already holds the mirror row WITH its traces. We read
// them from there, which is what makes the shipped artifact's promise true.
//
// Reach is partial and the caller is told exactly where it ends: of 5,215
// supercell rows, 3,510 (67.3%) mirror a cell row that carries traces; 1,413
// have a cell partner whose OWN syn list was shard-capped past this supercell,
// and 292 join two supercells, traceless on both ends. Those get NO `traces`
// key plus a reason — never `traces: []`, which reads as "no evidence exists".
async function hydrateSupercellTraces(
  c: Ctx,
  atomId: string,
  rows: Synapse[],
): Promise<Map<string, Synapse>> {
  const want = rows
    .filter((s) => !s.id.startsWith("path:") && !s.traces?.length)
    .slice(0, TRACE_HYDRATION_MAX)
    .map((s) => s.id);
  const entries = await cellEntries(c, want);
  const out = new Map<string, Synapse>();
  for (const id of want) {
    const mirror = entries.get(id)?.syn?.find((s) => s.id === atomId);
    if (mirror?.traces?.length) out.set(id, mirror);
  }
  return out;
}

export async function neighborhoodFor(
  c: Ctx,
  id: string,
  kindsCsv?: string,
  limitRaw?: unknown,
  tracesRaw?: unknown,
): Promise<ApiResult> {
  if (!BRAIN_ID_RE.test(id || "")) return { status: 400, body: { ok: false, error: "bad atom id" } };
  const limit = clampLimit(limitRaw, 50, 200);
  const wantTraces = !(tracesRaw === "0" || tracesRaw === false || tracesRaw === "false");
  const kinds = kindsCsv
    ? new Set(kindsCsv.split(",").map((s) => s.trim()).filter(Boolean))
    : null;
  // A kind that is not a synapse kind matches nothing, and "0 rows" reads as
  // "no such bond exists" — the exact failure the old enum caused. Name it.
  const unknownKinds = kinds
    ? [...kinds].filter((k) => !(SYNAPSE_KINDS as readonly string[]).includes(k))
    : [];
  const resolved = await resolveAtomKey(c, id);
  const atom = resolved ? resolved.atom ?? (await atomFor(c, resolved.id)) : null;
  if (!atom) {
    const dropped = droppedInV3(id);
    return {
      status: 404,
      body: {
        ok: false,
        error: dropped ? "no atom owns this organ id" : "unknown atom id",
        id,
        ...(dropped ? { reason: dropped } : {}),
        hint: KEY_HINT,
      },
    };
  }
  const picked: Synapse[] = [];
  let matched = 0;
  for (const s of atom.syn) {
    const synKinds = Object.keys(s.kinds ?? {});
    if (kinds && !synKinds.some((k) => kinds.has(k))) continue;
    matched += 1;
    if (picked.length < limit) picked.push(s);
  }

  // A supercell's rows arrive traceless; fetch them from the partner cells.
  const hydrated =
    wantTraces && atom.kind === "supercell"
      ? await hydrateSupercellTraces(c, atom.id, picked)
      : null;

  const rows = picked.map((s) => {
    const mirror = hydrated?.get(s.id);
    const src = mirror ?? s;
    const row: Record<string, unknown> = {
      id: s.id,
      w: s.w,
      kinds: s.kinds,
      ...(src.tt !== undefined ? { traces_total: src.tt } : {}),
    };
    if (!wantTraces) return row;
    if (src.traces?.length) {
      row.traces = kinds ? src.traces.filter((t) => kinds.has(t.kind)) : src.traces;
    } else if (atom.kind === "supercell") {
      // NEVER `traces: []` here — the bond IS witnessed, we just cannot reach
      // the witness from a Worker. Say which, and where it does live.
      row.traces_unavailable = s.id.startsWith("path:")
        ? `supercell↔supercell synapses ship traceless on both endpoints — ${TRACES_ELSEWHERE}`
        : `partner cell's own synapse list is shard-capped past this supercell — ${TRACES_ELSEWHERE}`;
    } else {
      row.traces = [];
    }
    return row;
  });

  // The shard caps the synapse LIST at `caps.synapses_per_cell`; `counts.syn` is
  // the true total, so this is what the list is NOT telling you. Kind-agnostic:
  // with ?kinds= we cannot know how many withheld rows would have matched, which
  // is exactly why it is reported as a count beside `matched` rather than folded
  // into it.
  const withheldByShard = atom.truncated?.syn ?? 0;
  return {
    status: 200,
    body: {
      ok: true,
      id: atom.id,
      ...(atom.id !== id ? { resolved_from: resolved?.resolved_from, key: id } : {}),
      kind: atom.kind,
      ...(kinds ? { kinds: [...kinds] } : {}),
      ...(unknownKinds.length
        ? {
            unknown_kinds: unknownKinds,
            hint:
              `not synapse kinds (they match nothing, they are not absent bonds): ${unknownKinds.join(", ")}. ` +
              `Valid: ${SYNAPSE_KINDS_CSV}. ` +
              `formalizes/matches are organ attachments, not synapses — read an organ's \`bond\` on /api/brain/cell.`,
          }
        : {}),
      synapses: rows,
      returned: rows.length,
      matched, // matches within the (capped) shard list — see withheld_by_shard
      counts: atom.counts, // the atom's TOTAL synapse count
      withheld_by_shard: withheldByShard,
      ...(hydrated ? { traces_hydrated: hydrated.size } : {}),
      // TRUE whenever any synapse is missing from `synapses`, by the shard cap
      // (counts.syn vs the shipped list — NOT `matched`, which only ever counts
      // rows already in that list) or by ?limit=. brain/query.py serves the full
      // set; traces are additionally trimmed per synapse (`traces_total`).
      truncated: rows.length < matched || withheldByShard > 0,
    },
  };
}

// ---- snippets: every stored content snippet on an atom ------------------------

// v2 fanned out one shard fetch per xref target; v3 reads the EMBEDDED organ
// payloads — one shard fetch answers the whole call. Every row carries its
// licence; `safeOrgan` has already dropped any snippet that lost one.
export async function snippetsFor(c: Ctx, id: string): Promise<ApiResult> {
  if (!BRAIN_ID_RE.test(id || "")) return { status: 400, body: { ok: false, error: "bad atom id" } };
  const resolved = await resolveAtomKey(c, id);
  const atom = resolved ? resolved.atom ?? (await atomFor(c, resolved.id)) : null;
  if (!atom) {
    const dropped = droppedInV3(id);
    return {
      status: 404,
      body: {
        ok: false,
        error: dropped ? "no atom owns this organ id" : "unknown atom id",
        id,
        ...(dropped ? { reason: dropped } : {}),
        hint: KEY_HINT,
      },
    };
  }
  const rows: Record<string, unknown>[] = [];
  for (const o of atom.organs) {
    if (o.kind === "concept") {
      rows.push({
        source_db: "wikidata",
        id: o.id,
        label: o.label ?? null,
        ...(o.description ? { snippet: o.description, license: "CC0 (Wikidata)" } : {}),
        url: `https://www.wikidata.org/wiki/${o.id}`,
      });
    } else if (o.kind === "article") {
      // pointer to the annotated WikiLean article (annotations live in D1)
      rows.push({
        source_db: "wikilean",
        id: o.id,
        label: o.label ?? null,
        url: `${SITE_ORIGIN}/${encodeURIComponent(o.id)}`,
      });
    } else if (o.kind === "page") {
      rows.push({
        source_db: o.db ?? "",
        id: o.id,
        label: o.label ?? null,
        ...(o.snippet ? { snippet: o.snippet, license: o.snippet_license } : {}),
        ...(o.url ? { url: o.url } : {}),
      });
    } else if (o.kind === "decl") {
      const name = o.label ?? o.id.split(":").slice(2).join(":");
      rows.push({
        source_db: "mathlib",
        id: o.id,
        label: name,
        ...(o.docstring ? { snippet: o.docstring, license: MATHLIB_LICENSE } : {}),
        ...(o.code ? { code: o.code, code_license: MATHLIB_LICENSE } : {}),
        ...(o.module ? { url: docsUrlFor(o.module, name) } : {}),
      });
    } else if (o.kind === "statement") {
      // arXiv statement TEXT is never redistributed — ids/labels/links only
      rows.push({
        source_db: "arxiv",
        id: o.id,
        label: o.label ?? null,
        ...(o.license_open !== undefined ? { license_open: o.license_open } : {}),
        ...(o.arxiv_id ? { url: `https://arxiv.org/abs/${o.arxiv_id}` } : {}),
      });
    }
  }
  return { status: 200, body: { ok: true, id: atom.id, kind: atom.kind, rows } };
}

// ---- filter: facet-bitmask enumeration ----------------------------------------

// `type=cell` (default) enumerates labels.json — one row per atom, `f` = the
// cell's OWN facets. `type=supercell` enumerates supercells.json by `fa`, the
// subtree-AGGREGATE mask ("something under this folder matches"), which is a
// deliberately different question — hence a separate type rather than a mixed list.
export async function filterFor(
  c: Ctx,
  fRaw: unknown,
  type?: string,
  limitRaw?: unknown,
  cursorRaw?: unknown,
  under?: string,
): Promise<ApiResult> {
  const mask = intOr(fRaw, -1);
  if (mask < 0 || mask > 0x7fffffff || (typeof fRaw === "string" && fRaw.trim() === "")) {
    return {
      status: 400,
      body: { ok: false, error: "f must be a non-negative integer bitmask (see brain/SCHEMA.md facet bits)" },
    };
  }
  const kind = type || "cell";
  if (kind !== "cell" && kind !== "supercell") {
    return {
      status: 400,
      body: {
        ok: false,
        error: "type must be cell | supercell",
        hint: "v3 has two node kinds; the v2 concept/container/ext types are gone (ext pages are organs inside cells)",
      },
    };
  }
  const limit = clampLimit(limitRaw, 100, 500);
  const cursor = intOr(cursorRaw, 0);
  if (cursor < 0) return { status: 400, body: { ok: false, error: "bad cursor" } };

  let pool: Array<{ id: string; f?: number; row: Record<string, unknown> }>;
  if (kind === "supercell") {
    const file = await supercellsFile(c);
    if (!file?.supercells) return { status: 503, body: { ok: false, error: "brain data unavailable" } };
    pool = Object.entries(file.supercells).map(([path, e]) => ({
      id: path,
      f: e.fa,
      row: {
        id: path,
        label: e.label ?? null,
        ...(e.fa !== undefined ? { fa: e.fa } : {}),
        ...(e.parent ? { parent: e.parent } : {}),
        ...(e.cells?.length ? { n_cells: e.cells.length } : {}),
      },
    }));
  } else {
    const labels = await cellLabels(c);
    if (!labels) return { status: 503, body: { ok: false, error: "brain data unavailable" } };
    pool = labels.map((r) => ({ id: r.id, f: r.f, row: r as unknown as Record<string, unknown> }));
  }
  // `under` restricts to a containment subtree. A supercell matches on its own
  // path prefix.
  //
  // A CELL cannot: labels.json's `p` is its DEEPEST supercell only, but SCHEMA
  // v3 says `supercells` may hold >1 entry and such a cell "renders inside each"
  // — so testing `p` alone drops every cell that spans two folders from the
  // subtree of all but one of them (31 cells; e.g. Cauchy-Schwarz is under
  // Analysis/InnerProductSpace AND LinearAlgebra/SesquilinearForm, but `p` names
  // only the first, so under=path:Mathlib/LinearAlgebra never returned it while
  // that folder's own card listed it and its `fa` mask advertised the match).
  // So take the UNION of both containment signals: `p`, and membership of the
  // `cells` list of any supercell in the subtree — the same field
  // /api/brain/cell serves, so the two surfaces now agree. Either signal alone
  // is sufficient evidence of containment, so a union cannot over-match, and it
  // keeps the enumeration whole if either index drifts (today they agree
  // exactly: 7,398 cells carry `p`, the same 7,398 are listed).
  const prefix = (under || "").trim();
  const inPrefix = (p: string) => p === prefix || p.startsWith(prefix + "/");
  const underSet =
    prefix && kind === "cell"
      ? await (async () => {
          const file = await supercellsFile(c);
          const ids = new Set<string>();
          for (const [path, e] of Object.entries(file?.supercells ?? {})) {
            if (!inPrefix(path)) continue;
            for (const cid of e.cells ?? []) ids.add(cid);
          }
          return ids;
        })()
      : null;
  const inSubtree = (e: { id: string; row: Record<string, unknown> }): boolean => {
    if (!prefix) return true;
    if (kind === "supercell") return inPrefix(e.id);
    return inPrefix((e.row.p as string | undefined) ?? "") || (underSet?.has(e.id) ?? false);
  };

  const hits: unknown[] = [];
  let nextCursor: number | null = null;
  for (let i = cursor; i < pool.length; i++) {
    const e = pool[i];
    if (((e.f ?? 0) & mask) !== mask) continue;
    if (!inSubtree(e)) continue;
    if (hits.length >= limit) {
      nextCursor = i; // index of the first matching row NOT returned — stable
      break;
    }
    hits.push(e.row);
  }
  return {
    status: 200,
    body: {
      ok: true,
      f: mask,
      type: kind,
      ...(prefix ? { under: prefix } : {}),
      hits,
      returned: hits.length,
      cursor,
      next_cursor: nextCursor,
    },
  };
}

// ---- search over the atom label index -----------------------------------------

// Matches an atom's own label AND its `aka` list — every organ's label — so
// "Vector space" finds the Module atom (they are one atom; the anchor names it).
// A key that resolves exactly (QID, decl name, slug, xref id) is promoted to the
// top hit, which keeps the v2 "a bare QID query matches by id" behavior alive
// even though cell ids are now `cell:<anchor>`.
export async function searchFor(c: Ctx, qRaw: string, type?: string, limitRaw?: unknown): Promise<ApiResult> {
  const q = (qRaw || "").trim();
  if (q.length < 2) return { status: 400, body: { ok: false, error: "query too short (min 2 chars)" } };
  const limit = clampLimit(limitRaw, 25, 100);
  const kind = type || "";
  if (kind && kind !== "cell" && kind !== "supercell") {
    return {
      status: 400,
      body: {
        ok: false,
        error: "type must be cell | supercell",
        hint: "v3 has two node kinds; the v2 concept/container/ext types are gone (ext pages are organs inside cells)",
      },
    };
  }
  const ql = q.toLowerCase();
  const labels = await cellLabels(c);
  if (!labels && kind !== "supercell") {
    return { status: 503, body: { ok: false, error: "brain data unavailable" } };
  }
  const file = await supercellsFile(c);
  if (!file?.supercells && kind === "supercell") {
    return { status: 503, body: { ok: false, error: "brain data unavailable" } };
  }

  const hits: Record<string, unknown>[] = [];
  const seen = new Set<string>();
  const push = (r: Record<string, unknown>) => {
    const id = String(r.id);
    if (seen.has(id) || hits.length >= limit) return;
    seen.add(id);
    hits.push(r);
  };
  const superRow = (path: string, e: SupercellEntry, extra?: Record<string, unknown>) => ({
    id: path,
    kind: "supercell",
    label: e.label ?? null,
    ...(e.organs?.length ? { aka: e.organs.map((o) => o.label ?? o.id) } : {}),
    ...(e.cells?.length ? { n_cells: e.cells.length } : {}),
    ...extra,
  });

  // 1. An exactly-resolving key takes the top slot, whichever kind it names.
  // This is how a bare QID still "matches by id" now that atom ids are
  // cell:<anchor> — and it is the ONLY way q=Q82571 (or its exact label "Linear
  // algebra") finds its folder, since labels.json indexes cells alone.
  const exact = await resolveAtomKey(c, q);
  if (exact?.id.startsWith("cell:") && kind !== "supercell") {
    const row = labels?.find((r) => r.id === exact.id);
    if (row) push({ ...pickSuggestion(row), matched: exact.resolved_from });
  } else if (exact?.id.startsWith("path:") && kind !== "cell") {
    const e = own(file?.supercells, exact.id);
    if (e) push(superRow(exact.id, e, { matched: exact.resolved_from }));
  }

  // 2. cells by label + aka (searchLabels already ranks prefix before substring)
  if (kind !== "supercell" && labels) {
    for (const r of searchLabels(labels, ql, "", limit)) push(pickSuggestion(r));
  }

  // 3. supercells, matched on the folder label AND its organ labels — a folder's
  // human name lives on its field concept ("Linear algebra", not "LinearAlgebra")
  if (kind !== "cell" && hits.length < limit) {
    const starts: Record<string, unknown>[] = [], contains: Record<string, unknown>[] = [];
    for (const [path, e] of Object.entries(file?.supercells ?? {})) {
      const names = [e.label ?? "", ...(e.organs ?? []).map((o) => o.label ?? "")]
        .map((n) => n.toLowerCase())
        .filter(Boolean);
      if (names.some((n) => n.startsWith(ql))) starts.push(superRow(path, e));
      else if (names.some((n) => n.includes(ql))) contains.push(superRow(path, e));
    }
    for (const r of [...starts, ...contains]) push(r);
  }
  return { status: 200, body: { ok: true, q, ...(kind ? { type: kind } : {}), hits } };
}

// ---- decl existence oracle (the decl-index shards GET /decl resolves against) --

interface DeclManifest {
  scheme: { min_len: number; max_len: number; pad: string };
  shards: Record<string, number>;
}

export async function declExistsFor(c: Ctx, nameRaw: string): Promise<ApiResult> {
  const name = (nameRaw || "").trim();
  if (!name || name.length > 300 || /[\s\p{C}/\\]/u.test(name)) {
    return { status: 400, body: { ok: false, error: "bad declaration name" } };
  }
  const manifest = await memoAssetJson<DeclManifest>(c, "/assets/decl-index/manifest.json");
  if (!manifest?.shards) return { status: 503, body: { ok: false, error: "decl index unavailable" } };
  const key = declShardFor(manifest, name);
  const pairs = key ? await assetJson<Array<[string, string]>>(c, `/assets/decl-index/${key}.json`) : null;
  const module = pairs ? lookupInShard(pairs, name) : null;
  if (!module) {
    return {
      status: 200,
      body: {
        ok: true,
        decl: name,
        exists: false,
        hint: "not in the Mathlib decl index — check spelling/namespace; renames are common (e.g. Basis → Module.Basis). https://wikilean.jackmccarthy.org/decl/<name> redirects to docs search.",
      },
    };
  }
  return {
    status: 200,
    body: { ok: true, decl: name, exists: true, library: "mathlib", module, docs_url: docsUrlFor(module, name) },
  };
}

// ---- routes -------------------------------------------------------------------

const CACHE_HEADERS = { "Cache-Control": "public, max-age=3600" }; // nightly-rebuild cadence

function send(c: Ctx, r: ApiResult): Response {
  return c.json(r.body, r.status, r.status === 200 ? CACHE_HEADERS : undefined);
}

// Same anonymous budget as /mcp (review finding: the REST twins of the MCP
// tools must not be a rate-limit bypass). Keyed by IP; MCP_LIMITER when bound,
// else BRAIN_API_LIMITER — distinct "brainapi-ip:" prefix avoids colliding
// with the write path's "brainapi:<user.id>" keys.
async function rateLimitGate(
  c: Ctx,
  next: () => Promise<void>,
): Promise<Response | void> {
  const limiter = c.env.MCP_LIMITER ?? c.env.BRAIN_API_LIMITER;
  const ip = c.req.header("CF-Connecting-IP") || "unknown";
  const { success } = await limiter.limit({ key: `brainapi-ip:${ip}` });
  if (!success) return c.json({ ok: false, error: "rate limited (120/min)" }, 429);
  await next();
}

export function registerBrainApiRoutes(app: Hono<{ Bindings: Env }>): void {
  app.use("/api/brain/cell", rateLimitGate);
  app.use("/api/brain/unit", rateLimitGate);
  app.use("/api/brain/transfer", rateLimitGate);
  app.use("/api/brain/neighborhood", rateLimitGate);
  app.use("/api/brain/snippets", rateLimitGate);
  app.use("/api/brain/filter", rateLimitGate);
  app.use("/api/brain/search", rateLimitGate);

  app.get("/api/brain/cell", async (c) => send(c, await cellFor(c, c.req.query("key") ?? "")));

  // v2 entry point. The unit card became the CELL card (the atom subsumes it —
  // a unit was QID ∘ article ∘ decls ∘ xrefs, which is exactly a cell's organs),
  // so this is a true alias rather than a shim: nothing that resolved before 404s.
  app.get("/api/brain/unit", async (c) => send(c, await cellFor(c, c.req.query("key") ?? "")));

  app.get("/api/brain/transfer", async (c) =>
    send(
      c,
      await transferFor(c, c.req.query("q") ?? "", c.req.query("direction") ?? "", c.req.query("limit")),
    ),
  );

  app.get("/api/brain/neighborhood", async (c) =>
    send(
      c,
      await neighborhoodFor(
        c,
        c.req.query("id") ?? "",
        c.req.query("kinds"),
        c.req.query("limit"),
        c.req.query("traces"),
      ),
    ),
  );

  app.get("/api/brain/snippets", async (c) => send(c, await snippetsFor(c, c.req.query("id") ?? "")));

  app.get("/api/brain/filter", async (c) =>
    send(
      c,
      await filterFor(
        c,
        c.req.query("f"),
        c.req.query("type"),
        c.req.query("limit"),
        c.req.query("cursor"),
        c.req.query("under"),
      ),
    ),
  );

  app.get("/api/brain/search", async (c) =>
    send(c, await searchFor(c, c.req.query("q") ?? "", c.req.query("type"), c.req.query("limit"))),
  );

  // The human-readable reference for everything above + the MCP endpoint.
  app.get("/brain/api", (c) => c.html(API_REFERENCE_HTML, 200, CACHE_HEADERS));
}

// ---- /brain/api reference page (self-contained; style matches the dark /brain
// shell in home.ts brainLanding — no build step, no external assets) -----------

const API_REFERENCE_HTML = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wikibrain API — WikiLean</title>
<meta name="description" content="The Wikibrain agent API: REST + MCP query surface over WikiLean's Brain — jump between informal mathematics (Wikipedia/Wikidata) and formal Mathlib declarations.">
<style>
* { box-sizing:border-box; }
body { margin:0; background:#0b0e14; color:#e6e4de; line-height:1.55;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
a { color:#7cb3ff; text-decoration:none; } a:hover { text-decoration:underline; }
.wl-header { background:#10141d; border-bottom:1px solid #262c3a; padding:10px 20px;
  display:flex; align-items:baseline; justify-content:space-between; gap:12px; flex-wrap:wrap; }
.wl-brand { font-weight:700; color:#7cb3ff; font-size:18px; }
.tag { color:#9aa3b2; font-size:.85rem; }
.wl-nav { display:flex; gap:14px; align-items:center; flex-wrap:wrap; font-size:.9rem; }
main { max-width:880px; margin:0 auto; padding:24px 20px 80px; }
h1 { font-size:1.5rem; margin:0 0 4px; } h2 { font-size:1.15rem; margin:2.2em 0 .5em;
  border-bottom:1px solid #262c3a; padding-bottom:6px; }
h3 { font-size:1rem; margin:1.6em 0 .4em; color:#c9d4e3; }
p, li { color:#c4c2bb; font-size:.95rem; }
code { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.85em;
  background:#131826; border:1px solid #262c3a; border-radius:4px; padding:1px 5px; }
pre { background:#131826; border:1px solid #262c3a; border-radius:8px; padding:12px 14px;
  overflow-x:auto; font-size:.82rem; line-height:1.5; }
pre code { background:none; border:0; padding:0; }
table { border-collapse:collapse; width:100%; font-size:.88rem; margin:.6em 0; }
th, td { text-align:left; border-bottom:1px solid #262c3a; padding:6px 10px 6px 0; vertical-align:top; }
th { color:#9aa3b2; font-weight:600; }
.muted { color:#9aa3b2; font-size:.85rem; }
.pill { display:inline-block; background:#16233a; color:#7cb3ff; border-radius:10px;
  padding:0 8px; font-size:.75rem; margin-left:6px; vertical-align:middle; }
</style>
</head>
<body>
<header class="wl-header">
  <span><span class="wl-brand">WikiLean</span>
    <span class="tag">— Wikibrain API: the agent-facing query surface over the Brain.</span></span>
  <nav class="wl-nav" aria-label="Site">
    <a href="/brain">Brain</a>
    <a href="/mcp">MCP docs</a>
    <a href="/articles">Articles</a>
    <a href="/">Home</a>
  </nav>
</header>
<main>
<h1>Wikibrain API <span class="pill">v3 — cells</span></h1>
<p class="muted">Read-only, unauthenticated, cached (<code>Cache-Control: public, max-age=3600</code> —
data rebuilds nightly). Base URL <code>https://wikilean.jackmccarthy.org</code>.
Full reference with response schemas: <a href="https://github.com/Deicyde/WikiLean/blob/main/docs/BRAIN-API.md">docs/BRAIN-API.md</a>.</p>

<h2>The model: cells, organs, supercells, synapses</h2>
<p>The addressable thing is the <b>cell</b> — an <em>atom</em> of mathematics, id
<code>cell:&lt;anchor&gt;</code>. A Mathlib declaration, a Wikidata concept, an external-database
page, a WikiLean article and an arXiv statement that all denote <em>one object</em> are
<b>organs</b> of that one cell: <code>Module</code>, <code>Q18848</code> (module) and
<code>Q125977</code> (vector space) are the same atom, because Mathlib has no
<code>VectorSpace</code> — <code>Module</code> generalizes it.</p>
<table>
<tr><th>thing</th><th>what it is</th></tr>
<tr><td><b>organ</b></td><td>A particle — <em>never</em> a node. Kinds: <code>concept</code>
  (<code>Q&lt;digits&gt;</code>) · <code>decl</code> (<code>decl:&lt;Lib&gt;:&lt;Name&gt;</code>) ·
  <code>page</code> (<code>xref:&lt;db&gt;:&lt;id&gt;</code>) · <code>article</code> (a WikiLean slug) ·
  <code>statement</code> (<code>lit:&lt;arxiv&gt;#&lt;ref&gt;</code>). Payloads are EMBEDDED — the Lean
  code, the Wikidata description, the licensed DB snippet all ship on the cell.</td></tr>
<tr><td><b>cell</b></td><td>The atom, the node of the graph. <code>cell:&lt;anchor&gt;</code>, where the
  anchor is the cell's <code>exact</code> concept.</td></tr>
<tr><td><b>supercell</b></td><td>A Mathlib folder, <code>path:&lt;Lib&gt;/&lt;Dir&gt;</code>. Cells render
  inside it, and it owns organs of its own: <b>field-of-study concepts</b> (Q82571 "Linear
  algebra" → <code>path:Mathlib/LinearAlgebra</code>, <em>not</em> a cell) and area-level pages.</td></tr>
<tr><td><b>synapse</b></td><td>ONE aggregated edge per atom pair: <code>w</code> (weight — every
  constituent bond), a <code>kinds</code> histogram (<code>depends</code>, <code>links</code>,
  <code>relates</code>, <code>cites</code>, <code>mentions</code>, …) and the individual
  <code>traces</code>, each with its own direction, provenance and evidence. Undirected by
  construction, so there is no <code>dir</code> parameter.</td></tr>
</table>
<p><b>Every v2 concept, declaration, container and article slug still resolves.</b>
<code>aliases.json</code> maps an organ id to its owning atom, and every route below accepts
<em>any</em> such organ id or an atom id: <code>Q125977</code>, <code>decl:Mathlib:Module</code> and
<code>Vector_space</code> all answer as <code>cell:Q18848</code>; <code>Q82571</code> answers as
<code>path:Mathlib/LinearAlgebra</code>.</p>
<p><b>Two v2 populations were dropped on purpose and 404 here</b> (docs/BRAIN-V3.md
"Dropped in v3") — the response names the reason rather than claiming the id is unknown:
<b>unanchored frontier ext pages</b> (45,996 of 49,606 <code>xref:</code> ids — a page is an
organ, and one no cell claims has no atom; the 3,610 anchored ones do resolve, the corpus stays
in <code>catalog/data/external/</code>, and the page's signal survives as a
<code>co-page</code> synapse) and <b>arXiv paper nodes</b> (1,994 <code>lit:&lt;arxiv&gt;</code> ids —
only STATEMENTS a cell claims are organs). The v2 route <code>/api/brain/node</code> still serves
both from the v2 shards, so the two route families deliberately answer differently for those ids.</p>

<h2>Connect over MCP (recommended for agents)</h2>
<pre><code>claude mcp add --transport http wikibrain https://wikilean.jackmccarthy.org/mcp</code></pre>
<p>A dependency-free streamable-HTTP MCP server (JSON-RPC 2.0, stateless, single-response
mode) exposing seven tools: <code>brain_search</code>, <code>brain_cell</code>,
<code>brain_transfer</code>, <code>brain_neighborhood</code>, <code>brain_snippets</code>,
<code>brain_filter</code>, <code>decl_exists</code>. <code>brain_unit</code> and
<code>brain_node</code> still answer, as aliases of <code>brain_cell</code> — the v2 unit card
<em>became</em> the cell card, and v3 has no particle nodes. Rate limit: 120 requests/min per IP.</p>

<h2>Id grammar</h2>
<table>
<tr><th>form</th><th>what</th><th>example</th></tr>
<tr><td><code>cell:&lt;anchor&gt;</code></td><td>an atom (the node)</td><td><code>cell:Q18848</code></td></tr>
<tr><td><code>path:&lt;Lib&gt;[/&lt;Dir&gt;…]</code></td><td>supercell (Mathlib folder)</td><td><code>path:Mathlib/LinearAlgebra</code></td></tr>
<tr><td><code>Q&lt;digits&gt;</code></td><td>concept organ (Wikidata QID)</td><td><code>Q181296</code></td></tr>
<tr><td><code>decl:&lt;Lib&gt;:&lt;FQ name&gt;</code></td><td>decl organ</td><td><code>decl:Mathlib:CommGroup</code></td></tr>
<tr><td><code>xref:&lt;db&gt;:&lt;id&gt;</code></td><td>page organ (external DB)</td><td><code>xref:nlab:module</code></td></tr>
<tr><td><code>lit:&lt;arxiv&gt;#&lt;ref&gt;</code></td><td>statement organ</td><td><code>lit:1707.04448#thm1.2</code></td></tr>
</table>

<h2>REST endpoints</h2>

<h3>GET /api/brain/cell?key=</h3>
<p>Resolve <em>any</em> organ id — QID, <code>decl:Lib:Name</code>, bare decl name, article slug,
<code>xref:db:id</code>, <code>lit:…</code>, an exact label or <code>aka</code>, or an atom id — to the
owning atom's card: the cell head, <b>every organ with its embedded payload</b>, the containment
breadcrumb, a synapse summary and the strongest partners. One request renders the whole card.
<code>/api/brain/unit?key=</code> is an alias (the v2 unit card <em>became</em> the cell card).</p>
<pre><code>curl 'https://wikilean.jackmccarthy.org/api/brain/cell?key=CommGroup'
curl 'https://wikilean.jackmccarthy.org/api/brain/cell?key=Vector_space'   # → cell:Q18848</code></pre>

<h3>GET /api/brain/transfer?q=&amp;direction=&amp;limit=</h3>
<p>The informal ↔ formal jump. <code>direction=informal_to_formal</code>: concept text / QID /
slug → the atom's ranked Mathlib <code>decl</code> organs with modules, docs URLs and
<code>bond</code>. <code>direction=formal_to_informal</code>: a decl name → the same atom's
<code>concept</code> organs, article URLs and snippet sources. A field-of-study concept answers
with its <b>supercell</b> (folder), which is the honest formal home. Empty results include
near-miss suggestions.</p>
<pre><code>curl 'https://wikilean.jackmccarthy.org/api/brain/transfer?q=abelian%20group&amp;direction=informal_to_formal'
curl 'https://wikilean.jackmccarthy.org/api/brain/transfer?q=Module&amp;direction=formal_to_informal'</code></pre>

<h3>GET /api/brain/neighborhood?id=&amp;kinds=&amp;limit=&amp;traces=</h3>
<p>An atom's <b>synapses</b>: one row per partner atom with <code>w</code>, the <code>kinds</code>
histogram, <code>traces_total</code>, and the <code>traces</code> themselves (each
<code>{kind, src, dst, prov, evidence}</code> — <code>src</code>/<code>dst</code> are the ORGAN ids that
witnessed the bond). <code>kinds</code> is a CSV subset of the ${SYNAPSE_KINDS.length} synapse kinds
— <code>${SYNAPSE_KINDS_CSV}</code>; <code>limit</code> ≤ 200; <code>traces=0</code> omits traces for a
compact partner list. No <code>dir</code>: a synapse is an undirected aggregate — direction lives
on each trace. <code>formalizes</code>/<code>matches</code> are <em>not</em> synapse kinds: the merge
function consumes them as organ attachments, so read them off an organ's <code>bond</code> on
<code>/api/brain/cell</code>. A supercell's rows are hydrated from the partner cells' shards
(<code>traces_hydrated</code>); where a trace is unreachable the row says so in
<code>traces_unavailable</code> instead of shipping an empty list.</p>
<pre><code>curl 'https://wikilean.jackmccarthy.org/api/brain/neighborhood?id=Q18848&amp;kinds=depends'</code></pre>

<h3>GET /api/brain/snippets?id=</h3>
<p>Every stored content snippet on an atom, read from the embedded organ payloads (no fan-out):
Wikidata description (CC0), WikiLean article pointer, each page organ's stored snippet, the
Mathlib docstring + code, and arXiv statement links. Every row carries its license; no-content
sources (MathWorld, DLMF, EoM, Kerodon) return deep links only, and arXiv statement text is
never redistributed.</p>
<pre><code>curl 'https://wikilean.jackmccarthy.org/api/brain/snippets?id=Q181296'</code></pre>

<h3>GET /api/brain/filter?f=&amp;type=&amp;under=&amp;limit=&amp;cursor=</h3>
<p>Enumerate atoms whose facet bitmask contains <code>f</code> (i.e. <code>(f_row &amp; f) == f</code>).
<code>type=cell</code> (default) reads each cell's OWN mask; <code>type=supercell</code> reads
<code>fa</code>, the subtree-AGGREGATE mask. <code>under=path:…</code> restricts to a containment
subtree. Bits (brain/SCHEMA.md): 0 gold <code>@[wikidata]</code> · 1 <code>@[stacks]</code> ·
2 <code>@[kerodon]</code> · 3 any xref · 4 formalized · 5 partial · 6 has WikiLean article ·
7 has literature · <s>8 is ext</s> (never set on a cell — external pages are organs) ·
9 lmfdb · 10 nlab · 11 mathworld · 12 proofwiki · 13 stacks-tag · 14 oeis · 15 has stored
snippet. Paginate with the returned <code>next_cursor</code>.</p>
<pre><code>curl 'https://wikilean.jackmccarthy.org/api/brain/filter?f=1&amp;limit=50'
curl 'https://wikilean.jackmccarthy.org/api/brain/filter?f=1&amp;under=path:Mathlib/Algebra'</code></pre>

<h3>GET /api/brain/search?q=&amp;type=&amp;limit=</h3>
<p>Label search over the atom index. Matches an atom's own label AND its <code>aka</code> list —
every organ's label — so <code>q=Vector space</code> returns the <b>Module</b> atom. A key that
resolves exactly (QID, decl name, slug, xref id) is promoted to the top hit.
<code>type</code> ∈ <code>cell|supercell</code>.</p>
<pre><code>curl 'https://wikilean.jackmccarthy.org/api/brain/search?q=vector%20space'</code></pre>

<h3>Related routes</h3>
<p><code>GET /api/brain/edges?id=</code> (live community overlay, uncached) ·
<code>GET /decl/&lt;name&gt;</code> (decl → docs redirect; JSON with <code>Accept: application/json</code>) ·
<code>GET /api/brain/node?id=</code> (the v2 particle shards — <b>legacy</b>, retiring with the v2
render path; use <code>/api/brain/cell</code>).</p>

<h2>Provenance &amp; licensing</h2>
<p>Brain cell/synapse data is CC0. Every organ and every synapse trace carries a
<code>prov</code> index into the shard manifest's <code>prov</code> table. Snippets are stored only
where the source license permits and each row carries its license
(nLab attribution · Stacks GFDL · LMFDB/OEIS CC-BY-SA-4.0 · ProofWiki CC-BY-SA-3.0 ·
PlanetMath CC-BY-SA · Mathlib Apache-2.0); other sources deep-link out.</p>
</main>
</body>
</html>`;
