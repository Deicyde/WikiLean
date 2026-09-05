// Wikibrain agent API (BRAIN v3 — docs/BRAIN-API.md, docs/BRAIN-V3.md).
//
// Read-only query routes over immutable release-qualified CELL shards. The
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
//   GET /api/brain/decl?name=|names=               decl existence oracle (batch ≤16)
//   GET /api/brain/premises?seeds=&limit=          stored-premise retrieval (seeds ≤8)
//   GET /api/brain/bridge?q=&limit=                the composite first call
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
// holds: every v2 concept, decl and container resolves, and so does every
// article slug. What does not is exactly what v3 DROPPED on purpose
// (docs/BRAIN-V3.md "Dropped in v3"): the unanchored frontier ext pages
// (anchored ones still resolve) and the arXiv paper nodes. Those 404 with a
// `reason` naming the drop (see droppedInV3); they never pretend the id is
// unknown. The v2 per-node shards are retired and the old GET /api/brain/node
// route is deleted outright (2026-08-04 — plain 404),
// so this cell layer is the ONLY resolver — the community-edit write path
// validates edge endpoints against it too, via the STRICT atomIdForOrgan
// (aliases ∪ supercells, no fuzzy resolution).
//
// Everything here is shard/asset-backed and safe to cache for the nightly
// release cadence (current-selector responses must revalidate).
import type { Context, Hono } from "hono";
import type { Env } from "./env.js";
import {
  assetJson,
  BrainReleaseUnavailableError,
  isBrainReleaseUnavailableError,
  memoAssetJson,
  requiredBrainAssetJson,
  resolveBrainRelease,
  searchLabels,
  BRAIN_ID_RE,
  type BrainLabelRow,
  type BrainReleaseContext,
} from "./brain.js";
import {
  bucketEntries,
  bucketTotal,
  declShardFor,
  declShardKey,
  docsUrlFor,
  finalSegment,
  lookupInShard,
  type DeclPair,
  type SuffixBucket,
} from "./decl.js";

type Ctx = Context<{ Bindings: Env }>;

// Helper results carry a JSON body + the HTTP status the REST route would use;
// the MCP layer maps status>=400 to a tool result with isError:true.
export type ApiStatus = 200 | 400 | 404 | 503;
export interface ApiResult {
  status: ApiStatus;
  body: Record<string, unknown>;
}

const SITE_ORIGIN = "https://wikilean.jackmccarthy.org";
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

// Existence plus a name is not enough to write compiling code (SCHEMA/BRIDGE
// item 2): every decl hit ships `module` + `import_line`. Adds `import_line`
// derived from the organ's module without mutating the shared Atom.organs.
function withImportLine(o: Organ): Organ {
  if (o.kind === "decl" && o.module && o.import_line === undefined) {
    return { ...o, import_line: `import ${o.module}` };
  }
  return o;
}

// The confidence floor for informal→formal answers (transfer + bridge), stated
// in the response itself so a reader never has to guess it. A forced weak match
// is what CREATES the hallucinated-citation failure the bridge exists to prevent
// (BRIDGE item 4), so the API abstains rather than answer under the floor.
const CONFIDENCE_FLOOR =
  "a hit clears the floor when the atom resolved by IDENTITY (an exact id/label match, " +
  "resolved_from≠'search') OR the best decl bond is 'exact'; a fuzzy free-text match whose " +
  "best bond is weaker than exact does NOT clear it — the API returns match:'none' with " +
  "nearest candidates instead of forcing a weak grounding.";

// exact | generalization | special_case | related | none — the classification of
// the best hit, and whether it clears the floor.
function matchClass(
  bestBond: string | null,
  resolvedByIdentity: boolean,
): { match: string; clears: boolean } {
  const clears = resolvedByIdentity || bestBond === "exact";
  if (!clears) return { match: "none", clears };
  // an ungraded organ is a lone-particle cell's anchor decl — you named the decl
  // and got the decl, so identity holds.
  if (bestBond === "exact" || bestBond == null) return { match: "exact", clears };
  return { match: bestBond, clears };
}

// The top-level `note` when the best hit is NOT exact (BRIDGE item 3).
function noteForBond(bond: string | null, declName: string, cellLabel: string | null): string {
  const kind =
    bond === "generalization"
      ? "a generalization"
      : bond === "special_case"
        ? "a special case"
        : "a related declaration";
  return `no exact formalization; nearest is ${kind} (${declName}${cellLabel ? ` on ${cellLabel}` : ""})`;
}

// ---- asset loaders (all memoized — see brain.ts memoAssetJson) ----------------

function dataRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function badReleaseData(release: BrainReleaseContext, path: string): never {
  throw new BrainReleaseUnavailableError(release.releaseId, path, "declared asset has an invalid shape");
}

async function cellsManifest(c: Ctx, release: BrainReleaseContext): Promise<CellsManifest> {
  const path = "cells/manifest.json";
  const value = await requiredBrainAssetJson<unknown>(c, release, path);
  const manifest = dataRecord(value);
  const scheme = dataRecord(manifest?.scheme);
  const shards = dataRecord(manifest?.shards);
  if (
    !manifest || !scheme || !shards || !Array.isArray(manifest.prov) ||
    !Number.isSafeInteger(scheme.min_len) || !Number.isSafeInteger(scheme.max_len) ||
    typeof scheme.pad !== "string" ||
    Object.values(shards).some((count) => !Number.isSafeInteger(count) || (count as number) < 0)
  ) badReleaseData(release, path);
  return value as CellsManifest;
}

async function cellAliases(c: Ctx, release: BrainReleaseContext): Promise<CellAliases> {
  const path = "cells/aliases.json";
  const value = await requiredBrainAssetJson<unknown>(c, release, path);
  const aliases = dataRecord(value);
  if (!aliases || !dataRecord(aliases.organs) || !dataRecord(aliases.decls) || !dataRecord(aliases.slugs)) {
    badReleaseData(release, path);
  }
  return value as CellAliases;
}

async function cellLabels(c: Ctx, release: BrainReleaseContext): Promise<BrainLabelRow[]> {
  const path = "cells/labels.json";
  const value = await requiredBrainAssetJson<unknown>(c, release, path);
  if (!Array.isArray(value)) badReleaseData(release, path);
  return value as BrainLabelRow[];
}

async function supercellsFile(c: Ctx, release: BrainReleaseContext): Promise<SupercellsFile> {
  const path = "cells/supercells.json";
  const value = await requiredBrainAssetJson<unknown>(c, release, path);
  const file = dataRecord(value);
  if (!file || !dataRecord(file.supercells)) badReleaseData(release, path);
  return value as SupercellsFile;
}

// ---- snapshot echo (SCHEMA v3; held-out evaluation is dishonest without it) ----

// The Mathlib pin the decl organs were built against. Organs carry a `prov`
// index; the pointed-at prov row carries the pin. Rather than open an organ, we
// read the SAME rows off the manifest's `prov` table (already fetched): prefer a
// git-commit-shaped pin from a Mathlib-family source (the @[wikidata]/@[stacks]/
// @[kerodon] attribute rows carry the mathlib4 checkout commit — e.g.
// "bf3266149cda603f"), else the first Mathlib-source pin (a data date), else
// null. Honest about what is available: no pin ⇒ null, never a guess.
function mathlibPin(prov: Array<Record<string, string>> | undefined): string | null {
  if (!prov) return null;
  let fallback: string | null = null;
  for (const p of prov) {
    const pin = p.pin;
    if (!pin) continue;
    if (p.source !== "mathlib" && p.source !== "kerodon" && p.source !== "stacks") continue;
    if (/^[0-9a-f]{7,40}$/.test(pin)) return pin; // a git commit — the real Mathlib rev
    if (fallback === null) fallback = pin;
  }
  return fallback;
}

export interface Snapshot {
  generated_at: string | null;
  pin: string | null;
}

// EVERY brain API/MCP response echoes this. Zero EXTRA fetches: the cells
// manifest is memoized and already loaded on every cell-backed path; the
// derivation (a 39-row scan) is trivial, so it recomputes rather than adding a
// second memo the tests would have to reset separately. A missing manifest is a
// release outage (503), never a response with an ambiguous null snapshot.
export async function snapshotFor(c: Ctx, release: BrainReleaseContext): Promise<Snapshot> {
  const manifest = await cellsManifest(c, release);
  const generatedAt = (manifest._meta?.generated_at as string | undefined) ?? null;
  return { generated_at: generatedAt, pin: mathlibPin(manifest.prov) };
}

// One cell shard entry, via the manifest's documented prefix scheme (identical
// to the decl-index scheme, so declShardFor resolves it verbatim).
async function cellEntry(c: Ctx, release: BrainReleaseContext, id: string): Promise<CellEntry | null> {
  const manifest = await cellsManifest(c, release);
  const key = declShardFor(
    { scheme: manifest.scheme, shards: manifest.shards },
    id,
  );
  const shard = key
    ? await requiredBrainAssetJson<Record<string, unknown>>(c, release, `cells/${key}.json`)
    : null;
  if (shard && !dataRecord(shard)) badReleaseData(release, `cells/${key}.json`);
  const entry = shard ? own(shard, id) : undefined;
  return entry ? (entry as CellEntry) : null;
}

// Many cell entries at once, grouped by shard so N partners living in ONE shard
// cost ONE fetch (the supercell-trace hydration below is the only caller, and
// its fan-out is what has to stay bounded).
async function cellEntries(c: Ctx, release: BrainReleaseContext, ids: string[]): Promise<Map<string, CellEntry>> {
  const out = new Map<string, CellEntry>();
  const manifest = await cellsManifest(c, release);
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
      const shard = await requiredBrainAssetJson<Record<string, unknown>>(c, release, `cells/${key}.json`);
      if (!dataRecord(shard)) badReleaseData(release, `cells/${key}.json`);
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
export async function atomFor(c: Ctx, release: BrainReleaseContext, id: string): Promise<Atom | null> {
  if (!BRAIN_ID_RE.test(id)) return null;
  if (id.startsWith("path:")) {
    const file = await supercellsFile(c, release);
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
  const e = await cellEntry(c, release, id);
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
export async function resolveAtomKey(c: Ctx, release: BrainReleaseContext, keyRaw: string): Promise<ResolvedKey | null> {
  const key = keyRaw.trim();
  if (!key || !BRAIN_ID_RE.test(key)) return null;

  if (isAtomId(key)) {
    const atom = await atomFor(c, release, key);
    if (atom) return { id: atom.id, resolved_from: atom.kind, atom };
    return null; // an explicit atom id must not fall through to label search
  }

  const aliases = await cellAliases(c, release);

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
  const labels = await cellLabels(c, release);
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
  // NOTE for future editors: everything below this point (and the label/aka
  // match above) is FUZZY resolution. The community-edit endpoint oracle must
  // never reach it — that is why atomIdForOrgan exists as a separate function
  // rather than a flag on this one.
  //
  // `aliases.json.slugs` indexes CELL slugs only and supercell organs ship no
  // `slug` at all, so a rule-5 field concept's own article slug missed every
  // index above and 404'd — 61 of them, including `Linear_algebra`, the very
  // example the docs give for a slug resolving. An enwiki slug IS the title with
  // spaces underscored (SCHEMA: "an article is the `enwiki` sitelink of its
  // concept QID"), so undo that and the organ label matches. Cells are matched
  // first, above, and so still win any collision.
  const kSlug = kl.replace(/_/g, " ");
  const file = await supercellsFile(c, release);
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

// STRICT organ → atom resolver: the node-existence oracle the community-edit
// write path (src/brain-edits.ts) validates edge endpoints against, replacing
// the retired v2 per-node shard set. Deliberately NOT resolveAtomKey: that
// also resolves bare decl names, article-title slug guesses, and exact
// labels/aka, which would let prose like "Vector space" become a valid edge
// endpoint — an accepted-invalid id written to D1, invisible in every overlay
// and irreversible. Accepted here, nothing else:
//   cell:… | path:…   an atom id that exists in the shards (supercells.json
//                     answers the path: side — aliases has no path: keys)
//   any exact organ-id key of aliases.json `organs` (QID, decl:<Lib>:<Name>,
//                     xref page id, article slug, lit statement)
// Returns the owning atom id, or null.
export async function atomIdForOrgan(c: Ctx, release: BrainReleaseContext, id: string): Promise<string | null> {
  if (!id || !BRAIN_ID_RE.test(id)) return null;
  if (isAtomId(id)) {
    const atom = await atomFor(c, release, id);
    return atom ? atom.id : null;
  }
  const aliases = await cellAliases(c, release);
  return own(aliases?.organs, id) ?? null;
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

async function suggestionsFor(c: Ctx, release: BrainReleaseContext, text: string): Promise<Record<string, unknown>[]> {
  const q = text.trim().toLowerCase();
  if (q.length < 2) return [];
  const labels = await cellLabels(c, release);
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
export async function cellFor(c: Ctx, release: BrainReleaseContext, keyRaw: string): Promise<ApiResult> {
  const key = (keyRaw || "").trim();
  if (!key || !BRAIN_ID_RE.test(key)) {
    return { status: 400, body: { ok: false, error: "missing or malformed ?key=", hint: KEY_HINT } };
  }
  const resolved = await resolveAtomKey(c, release, key);
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
  const atom = resolved.atom ?? (await atomFor(c, release, resolved.id));
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
      organs: atom.organs.map(withImportLine), // item 2: decl organs carry `import_line`
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
  release: BrainReleaseContext,
  qRaw: string,
  direction: string,
  limitRaw?: unknown,
): Promise<ApiResult> {
  const q = (qRaw || "").trim();
  if (!q) return { status: 400, body: { ok: false, error: "missing ?q=" } };
  const limit = clampLimit(limitRaw, 10, 50);
  if (direction === "informal_to_formal") return informalToFormal(c, release, q, limit);
  if (direction === "formal_to_informal") return formalToInformal(c, release, q, limit);
  return {
    status: 400,
    body: { ok: false, error: "direction must be informal_to_formal or formal_to_informal" },
  };
}

// A decl hit carries what it takes to WRITE the code, not just cite the name
// (BRIDGE item 2): module, `import_line`, the statement `code` when embedded, the
// organ `bond` (item 3), and a `renamed_to` when the cited name is already dead.
function declHit(o: Organ, atom: Atom): Record<string, unknown> {
  const name = o.label ?? o.id.split(":").slice(2).join(":");
  return {
    decl: name,
    module: o.module ?? null,
    import_line: o.module ? `import ${o.module}` : null,
    bond: o.bond ?? null,
    decl_kind: o.decl_kind ?? null,
    ...(o.code ? { code: o.code } : {}),
    ...(o.renamed_to ? { renamed_to: o.renamed_to } : {}),
    docs_url: o.module ? docsUrlFor(o.module, name) : `${SITE_ORIGIN}/decl/${encodeURIComponent(name)}`,
    via_cell: atom.id,
    cell_label: atom.label,
  };
}

// The bond of the CONCEPT organ the query resolved through (BRIDGE item 3): when
// you ask for "Vector space" (a `generalization` concept organ) the atom's exact
// decl is `Module`, which is exact for the ATOM but a GENERALIZATION of what you
// asked — the honest note is "Module generalizes Vector space". Decl organs carry
// exact/None, so this relationship lives on the concept organ, not the decl.
function queryConceptBond(atom: Atom, key: string): { bond: string | null; label: string | null } {
  const k = key.trim();
  const kl = k.toLowerCase();
  const concepts = organsOf(atom, "concept");
  const organ =
    concepts.find((o) => o.id === k) ?? // QID
    concepts.find((o) => o.slug === k) ?? // article slug
    concepts.find((o) => (o.label ?? "").toLowerCase() === kl); // exact label
  return { bond: organ?.bond ?? null, label: organ?.label ?? null };
}

// Concept → the formal side. With cells this is "resolve to the atom, read its
// decl organs" — no edge walk: an atom's decls ARE its own organs by the merge
// function (`exact` fuses both ways), which is exactly why Vector space and
// Module answer identically.
async function informalToFormal(c: Ctx, release: BrainReleaseContext, q: string, limit: number): Promise<ApiResult> {
  let resolved = await resolveAtomKey(c, release, q);
  let resolvedFrom: string | null = resolved?.resolved_from ?? null;
  if (!resolved && q.length >= 2) {
    // free text: best label/aka search hit
    const labels = await cellLabels(c, release);
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
        suggestions: await suggestionsFor(c, release, q),
        hint: "try /api/brain/search?q= for fuzzy lookup",
      },
    };
  }
  const atom = resolved.atom ?? (await atomFor(c, release, resolved.id));
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
    confidence_floor: CONFIDENCE_FLOOR,
    // item 3: all hits are organs of this ONE atom, so its breadcrumb is shared
    // (per-hit breadcrumb is reserved for the bridge, which spans atoms)
    ...(atom.breadcrumb ? { breadcrumb: atom.breadcrumb } : {}),
  };
  if (atom.kind === "supercell") {
    // rule 5: a field-of-study concept's formal home is a FOLDER, not a decl.
    // That is the honest answer, not an empty result — say so.
    body.match = "field";
    body.hits = [];
    body.note =
      "this is a field-of-study concept: its formal home is a Mathlib folder (supercell), not a single declaration";
    body.container = atom.id;
    body.cells_in_container = atom.supercell?.cells?.length ?? 0;
  } else if (!hits.length) {
    body.match = "none";
    body.hits = [];
    body.note = "no Mathlib declaration is an organ of this atom";
    if (atom.cell?.supercells?.length) body.containers = atom.cell.supercells;
    body.suggestions = await suggestionsFor(c, release, q);
  } else {
    // item 3: did the query resolve through a generalization/special_case concept
    // organ? Then the atom's exact decls formalize a MORE GENERAL / narrower object
    // than what was asked — surface that per the query, not the decl.
    const qc = queryConceptBond(atom, q);
    if (qc.bond === "generalization" || qc.bond === "special_case") {
      body.match = qc.bond;
      body.hits = hits;
      const rel = qc.bond === "generalization" ? "generalizes" : "is a special case of";
      const kind = qc.bond === "generalization" ? "more general" : "more specific";
      body.note =
        `no exact formalization of "${qc.label}"; ${atom.label} ${rel} it — ` +
        `the decl${hits.length > 1 ? "s" : ""} here formalize${hits.length > 1 ? "" : "s"} the ${kind} object`;
    } else {
      // item 4: honest abstention. A fuzzy label match landing on a non-exact bond
      // does not clear the floor — return nearest, never a forced weak answer.
      const bestBond = (hits[0].bond as string | null) ?? null;
      const { match, clears } = matchClass(bestBond, resolvedFrom !== "search");
      body.match = match;
      if (!clears) {
        body.hits = [];
        body.nearest = hits.slice(0, 3).map((h) => ({
          ...h,
          why: "atom matched by label similarity only, and the best bond is not exact",
        }));
        body.note =
          "no formalization cleared the confidence floor (fuzzy label match with a non-exact bond) — nearest candidates returned instead of a forced answer";
      } else {
        body.hits = hits;
        // a non-exact best decl bond still gets a plain note
        if (match !== "exact") body.note = noteForBond(bestBond, String(hits[0].decl), atom.label);
      }
    }
  }
  return { status: 200, body };
}

// Decl name → the informal side: the atom's concept + article organs. A decl
// resolves to exactly ONE atom (aliases.json is a function — SCHEMA C4), and
// that atom's concept organs are the multi-to-multi answer v2 walked in-edges
// for (Module → Q18848 AND Q125977, one fetch).
async function formalToInformal(c: Ctx, release: BrainReleaseContext, q: string, limit: number): Promise<ApiResult> {
  const name = q.startsWith("decl:") ? q.split(":").slice(2).join(":") : q;
  const resolved = await resolveAtomKey(c, release, q.startsWith("decl:") ? q : `decl:Mathlib:${q}`)
    ?? (await resolveAtomKey(c, release, name));
  const atom = resolved ? resolved.atom ?? (await atomFor(c, release, resolved.id)) : null;
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
    body.suggestions = await suggestionsFor(c, release, name.split(".").pop() ?? name);
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
  release: BrainReleaseContext,
  atomId: string,
  rows: Synapse[],
): Promise<Map<string, Synapse>> {
  const want = rows
    .filter((s) => !s.id.startsWith("path:") && !s.traces?.length)
    .slice(0, TRACE_HYDRATION_MAX)
    .map((s) => s.id);
  const entries = await cellEntries(c, release, want);
  const out = new Map<string, Synapse>();
  for (const id of want) {
    const mirror = entries.get(id)?.syn?.find((s) => s.id === atomId);
    if (mirror?.traces?.length) out.set(id, mirror);
  }
  return out;
}

// Stable (-w, id) ordering + an OPAQUE cursor over it (BRIDGE item 5): a
// 60-minute agent walks a chain across turns without a truncation surprise. A
// synapse comes AFTER the cursor when its weight is lower, or equal weight with a
// later id. The cursor is the ONLY soft boundary; the shard cap stays a HARD one,
// declared in `withheld_by_shard` — never silent.
function synCmp(a: Synapse, b: Synapse): number {
  return b.w - a.w || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
}
interface NeighborhoodCursor { w: number; id: string }
type CursorDecode<T> = { value: T | null; releaseMismatch: boolean; queryMismatch: boolean };

function encodeCursorJson(value: unknown): string {
  const bytes = new TextEncoder().encode(JSON.stringify(value));
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function decodeCursorJson(raw: string): unknown {
  const binary = atob(raw);
  const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
  return JSON.parse(new TextDecoder("utf-8", { fatal: true, ignoreBOM: false }).decode(bytes));
}

function encodeCursor(release: BrainReleaseContext, query: string, s: Synapse): string {
  return encodeCursorJson({ v: 2, r: release.releaseId, q: query, w: s.w, id: s.id });
}
function afterCursor(s: Synapse, cur: NeighborhoodCursor): boolean {
  return s.w < cur.w || (s.w === cur.w && s.id > cur.id);
}
function decodeCursor(raw: unknown, release: BrainReleaseContext, query: string): CursorDecode<NeighborhoodCursor> {
  const empty = { value: null, releaseMismatch: false, queryMismatch: false };
  if (typeof raw !== "string" || !raw) return empty;
  try {
    const o = decodeCursorJson(raw) as { v?: unknown; r?: unknown; q?: unknown; w?: unknown; id?: unknown };
    if (o && o.v === 2 && typeof o.r === "string" && typeof o.q === "string" && typeof o.w === "number" && typeof o.id === "string") {
      if (o.r !== release.releaseId) return { ...empty, releaseMismatch: true };
      return o.q === query
        ? { value: { w: o.w, id: o.id }, releaseMismatch: false, queryMismatch: false }
        : { ...empty, queryMismatch: true };
    }
    // Phase 1 compatibility: v1 tokens still identify a release, but do not
    // carry the normalized query. Pre-release tokens are deliberately rejected:
    // resuming one after a selector switch could silently mix generations.
    if (o && o.v === 1 && typeof o.r === "string" && typeof o.w === "number" && typeof o.id === "string") {
      return o.r === release.releaseId
        ? { value: { w: o.w, id: o.id }, releaseMismatch: false, queryMismatch: false }
        : { ...empty, releaseMismatch: true };
    }
    if (o && o.v === undefined && o.r === undefined && typeof o.w === "number" && typeof o.id === "string") {
      return { ...empty, releaseMismatch: true };
    }
  } catch {
    /* Historical behavior: a malformed neighborhood cursor restarts at the top. */
  }
  return empty;
}

export async function neighborhoodFor(
  c: Ctx,
  release: BrainReleaseContext,
  id: string,
  kindsCsv?: string,
  limitRaw?: unknown,
  tracesRaw?: unknown,
  minWRaw?: unknown,
  cursorRaw?: unknown,
  minConfRaw?: unknown,
): Promise<ApiResult> {
  if (!BRAIN_ID_RE.test(id || "")) return { status: 400, body: { ok: false, error: "bad atom id" } };
  const limit = clampLimit(limitRaw, 50, 200);
  const minW = Math.max(intOr(minWRaw, 0), 0);
  // min_conf floors trace-level confidence WHERE a trace carries one; shipped
  // traces do not, so it is inert on prod but correct where present. Traces with
  // no score are KEPT (we never drop evidence we cannot score) and the number
  // dropped is DECLARED in `traces_conf_filtered`.
  const minConf =
    typeof minConfRaw === "number"
      ? minConfRaw
      : typeof minConfRaw === "string" && minConfRaw.trim() !== "" && Number.isFinite(Number(minConfRaw))
        ? Number(minConfRaw)
        : null;
  const wantTraces = !(tracesRaw === "0" || tracesRaw === false || tracesRaw === "false");
  const kinds = kindsCsv
    ? new Set(kindsCsv.split(",").map((s) => s.trim()).filter(Boolean))
    : null;
  const cursorQuery = JSON.stringify({ id, kinds: kinds ? [...kinds].sort() : [], min_w: minW });
  const decodedCursor = decodeCursor(cursorRaw, release, cursorQuery);
  if (decodedCursor.releaseMismatch) {
    return { status: 400, body: { ok: false, error: "cursor belongs to a different Brain release" } };
  }
  if (decodedCursor.queryMismatch) {
    return { status: 400, body: { ok: false, error: "cursor belongs to a different Brain query" } };
  }
  const cursor = decodedCursor.value;
  // A kind that is not a synapse kind matches nothing, and "0 rows" reads as
  // "no such bond exists" — the exact failure the old enum caused. Name it.
  const unknownKinds = kinds
    ? [...kinds].filter((k) => !(SYNAPSE_KINDS as readonly string[]).includes(k))
    : [];
  const resolved = await resolveAtomKey(c, release, id);
  const atom = resolved ? resolved.atom ?? (await atomFor(c, release, resolved.id)) : null;
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
  // Stable ordering, then the kinds + min_w filter — `matched` is what the filter
  // selects from the (shard-capped) list, cursor/limit paginate WITHIN it.
  const ordered = [...atom.syn].sort(synCmp);
  const filtered = ordered.filter((s) => {
    if (kinds && !Object.keys(s.kinds ?? {}).some((k) => kinds.has(k))) return false;
    if (s.w < minW) return false;
    return true;
  });
  const matched = filtered.length;
  const startIdx = cursor ? filtered.findIndex((s) => afterCursor(s, cursor)) : 0;
  const from = startIdx < 0 ? filtered.length : startIdx;
  const picked = filtered.slice(from, from + limit);
  const nextCursor =
    picked.length > 0 && from + picked.length < filtered.length
      ? encodeCursor(release, cursorQuery, picked[picked.length - 1])
      : null;

  // A supercell's rows arrive traceless; fetch them from the partner cells.
  const hydrated =
    wantTraces && atom.kind === "supercell"
      ? await hydrateSupercellTraces(c, release, atom.id, picked)
      : null;

  let confFiltered = 0;
  const filterConf = (traces: Trace[]): Trace[] => {
    if (minConf == null) return traces;
    return traces.filter((t) => {
      const cv =
        t.evidence && typeof (t.evidence as { confidence?: unknown }).confidence === "number"
          ? ((t.evidence as { confidence: number }).confidence)
          : null;
      if (cv != null && cv < minConf) {
        confFiltered += 1;
        return false;
      }
      return true; // no score ⇒ keep; we never drop evidence we cannot score
    });
  };

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
      row.traces = filterConf(kinds ? src.traces.filter((t) => kinds.has(t.kind)) : src.traces);
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
      ...(minW ? { min_w: minW } : {}),
      ...(minConf != null ? { min_conf: minConf, traces_conf_filtered: confFiltered } : {}),
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
      matched, // rows passing the kinds/min_w filter within the (capped) shard list
      counts: atom.counts, // the atom's TOTAL synapse count
      withheld_by_shard: withheldByShard,
      ...(nextCursor ? { next_cursor: nextCursor } : {}),
      ...(hydrated ? { traces_hydrated: hydrated.size } : {}),
      // TRUE whenever any synapse is missing from `synapses`: by the shard cap
      // (counts.syn vs the shipped list — NOT `matched`, which only counts rows
      // in that list), by ?limit=/cursor pagination (next_cursor set), or by a
      // filter. brain/query.py serves the full set; traces are additionally
      // trimmed per synapse (`traces_total`).
      truncated: rows.length < matched || withheldByShard > 0 || nextCursor != null,
    },
  };
}

// ---- snippets: every stored content snippet on an atom ------------------------

// v2 fanned out one shard fetch per xref target; v3 reads the EMBEDDED organ
// payloads — one shard fetch answers the whole call. Every row carries its
// licence; `safeOrgan` has already dropped any snippet that lost one.
export async function snippetsFor(c: Ctx, release: BrainReleaseContext, id: string): Promise<ApiResult> {
  if (!BRAIN_ID_RE.test(id || "")) return { status: 400, body: { ok: false, error: "bad atom id" } };
  const resolved = await resolveAtomKey(c, release, id);
  const atom = resolved ? resolved.atom ?? (await atomFor(c, release, resolved.id)) : null;
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

function encodeFilterCursor(release: BrainReleaseContext, query: string, index: number): string {
  return encodeCursorJson({ v: 2, r: release.releaseId, q: query, i: index });
}

function decodeFilterCursor(raw: unknown, release: BrainReleaseContext, query: string): CursorDecode<number> {
  const empty = { value: null, releaseMismatch: false, queryMismatch: false };
  if (raw === undefined || raw === null || raw === "") return { value: 0, releaseMismatch: false, queryMismatch: false };
  // Pre-release integer cursors are intentionally invalid. They cannot prove
  // which immutable generation they paginate.
  if (typeof raw !== "string") return empty;
  try {
    const o = decodeCursorJson(raw) as { v?: unknown; r?: unknown; q?: unknown; i?: unknown };
    if (o?.v === 2 && typeof o.r === "string" && typeof o.q === "string" && Number.isSafeInteger(o.i) && (o.i as number) >= 0) {
      if (o.r !== release.releaseId) return { ...empty, releaseMismatch: true };
      return o.q === query
        ? { value: o.i as number, releaseMismatch: false, queryMismatch: false }
        : { ...empty, queryMismatch: true };
    }
    if (o?.v === 1 && typeof o.r === "string" && Number.isSafeInteger(o.i) && (o.i as number) >= 0) {
      return o.r === release.releaseId
        ? { value: o.i as number, releaseMismatch: false, queryMismatch: false }
        : { ...empty, releaseMismatch: true };
    }
  } catch {
    // Unlike the historical neighborhood token, filter cursors always rejected malformed input.
  }
  return empty;
}

// `type=cell` (default) enumerates labels.json — one row per atom, `f` = the
// cell's OWN facets. `type=supercell` enumerates supercells.json by `fa`, the
// subtree-AGGREGATE mask ("something under this folder matches"), which is a
// deliberately different question — hence a separate type rather than a mixed list.
export async function filterFor(
  c: Ctx,
  release: BrainReleaseContext,
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
  const prefix = (under || "").trim();
  const cursorQuery = JSON.stringify({ f: mask, type: kind, under: prefix });
  const decodedCursor = decodeFilterCursor(cursorRaw, release, cursorQuery);
  if (decodedCursor.releaseMismatch) {
    return { status: 400, body: { ok: false, error: "cursor belongs to a different Brain release" } };
  }
  if (decodedCursor.queryMismatch) {
    return { status: 400, body: { ok: false, error: "cursor belongs to a different Brain query" } };
  }
  if (decodedCursor.value === null) return { status: 400, body: { ok: false, error: "bad cursor" } };
  const cursor = decodedCursor.value;

  let pool: Array<{ id: string; f?: number; row: Record<string, unknown> }>;
  if (kind === "supercell") {
    const file = await supercellsFile(c, release);
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
    const labels = await cellLabels(c, release);
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
  const inPrefix = (p: string) => p === prefix || p.startsWith(prefix + "/");
  const underSet =
    prefix && kind === "cell"
      ? await (async () => {
          const file = await supercellsFile(c, release);
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
      next_cursor: nextCursor === null ? null : encodeFilterCursor(release, cursorQuery, nextCursor),
    },
  };
}

// ---- search over the atom label index -----------------------------------------

// Matches an atom's own label AND its `aka` list — every organ's label — so
// "Vector space" finds the Module atom (they are one atom; the anchor names it).
// A key that resolves exactly (QID, decl name, slug, xref id) is promoted to the
// top hit, which keeps the v2 "a bare QID query matches by id" behavior alive
// even though cell ids are now `cell:<anchor>`.
export async function searchFor(c: Ctx, release: BrainReleaseContext, qRaw: string, type?: string, limitRaw?: unknown): Promise<ApiResult> {
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
  const labels = await cellLabels(c, release);
  if (!labels && kind !== "supercell") {
    return { status: 503, body: { ok: false, error: "brain data unavailable" } };
  }
  const file = await supercellsFile(c, release);
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
  const exact = await resolveAtomKey(c, release, q);
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
  source_sha_or_etag?: string; // the doc-gen4 snapshot id (sibling indexes must match)
}

const DECL_NAME_BAD = /[\s\p{C}/\\]/u;
const BATCH_CAP = 16; // agents draft statements citing 3–8 decls; round-trip economy (BRIDGE item 1)
const DECL_MISS_HINT =
  "not in the Mathlib decl index — check spelling/namespace; renames are common " +
  "(e.g. Basis → Module.Basis). https://wikilean.jackmccarthy.org/decl/<name> redirects to docs search.";

// Hard per-call ASSETS fan-out caps (isolate-memoized manifests excluded).
// `suggest` bounds the rename-suggestion verification fan-out one decl_exists /
// bridge call may buy (a 16-name dead batch × 8-sharer buckets is ~160 fetches
// unbounded — past the Workers free-tier 50-subrequest cap); `premise` is
// premisesFor's total, of which `premiseChunkReserve` is held back for the
// name-chunk + module hydration phase so seed resolution can never starve the
// response to zero rows. Exported as a mutable object ONLY so tests can
// exercise the exhaustion paths; production code treats it as constants.
export const FETCH_CAPS = { suggest: 24, premise: 30, premiseChunkReserve: 8 };

// A per-call fetch allowance. Spent ONLY on cache misses (a shard already in a
// per-call promise cache is free); when a claim comes up short the response
// says so (`suggestion_truncated` / `truncated`) instead of silently fanning
// out further or silently under-answering.
interface FetchBudget {
  left: number;
  exhausted: boolean;
}

// claim up to `want` fetches; marks the budget exhausted when the claim is short
function takeBudget(budget: FetchBudget, want: number): number {
  const take = Math.min(want, Math.max(budget.left, 0));
  if (take < want) budget.exhausted = true;
  budget.left -= take;
  return take;
}

// bare fully-qualified name from either `decl:<Lib>:<Name>` or a bare name
function bareDeclName(name: string): string {
  return name.startsWith("decl:") ? name.split(":").slice(2).join(":") : name;
}

// Per-call shard caches store PROMISES, set before the first await (the
// memoAssetJson pattern) — concurrent misses on one shard share a single fetch.
type DeclShardCache = Map<string, Promise<DeclPair[] | null>>;
type SuffixShardCache = Map<string, Promise<Record<string, unknown> | null>>;

// One existence check against the decl-index (the doc-gen4 oracle GET /decl
// uses). The manifest is memoized; the per-call promise cache dedupes a batch
// that shares shards without memoizing every shard for the isolate's lifetime.
// A `budget` (suggestion/hydration paths only) is spent on cache misses; an
// empty budget suppresses the fetch and says so instead of guessing.
async function declLookup(
  c: Ctx,
  name: string,
  manifest: DeclManifest,
  shardCache: DeclShardCache,
  budget?: FetchBudget,
): Promise<{ exists: boolean; module?: string; suppressed?: boolean }> {
  const key = declShardFor(manifest, name);
  if (!key) return { exists: false };
  let p = shardCache.get(key);
  if (!p) {
    if (budget && takeBudget(budget, 1) < 1) return { exists: false, suppressed: true };
    p = assetJson<DeclPair[]>(c, `/assets/decl-index/${key}.json`);
    shardCache.set(key, p);
  }
  const pairs = await p;
  const module = pairs ? lookupInShard(pairs, name) : null;
  return module ? { exists: true, module } : { exists: false };
}

// ---- suffix index: final-segment → FQ names, over the FULL decl universe ------
//
// /assets/suffix-index/ shards the doc-gen4 universe (411k names) by NORMALIZED
// final segment, with the same longest-prefix scheme as the decl index (so
// declShardFor resolves its manifest verbatim, precedent: cellEntry). Shard
// values are buckets keyed by the normalized final segment:
//   { "total_count": <uncapped n>, "entries": [[fq, module], …] }   (≤64 stored)
// (a bare [[fq, module], …] array is accepted as an uncapped bucket). It
// replaces the old aliases.decls linear scan, whose uniqueness was scoped to
// the brain's ~19.6k decl organs — 4.8% of the universe (bench task 063's gold
// suffix was typed exactly and still got no suggestion).
const SUFFIX_INDEX = "/assets/suffix-index";
const NAMESPACE_MATCH_CAP = 8; // 2..8 verified matches enumerate; more → count + hint

// Bucket key normalization — declShardKey over the whole segment (lowercase
// [a-z0-9], "_" otherwise, no padding). Must mirror the suffix-index builder.
function suffixBucketKey(segment: string): string {
  return declShardKey(segment, segment.length);
}

// Runtime guard over unknown JSON → the shared SuffixBucket type (decl.ts).
// Exactly the builder's two shapes: a bare entries array, or
// { total_count, entries } for an over-cap bucket. Anything else is null.
function parseSuffixBucket(raw: unknown): SuffixBucket | null {
  if (Array.isArray(raw)) return raw as DeclPair[];
  if (typeof raw !== "object" || raw === null) return null;
  const o = raw as Record<string, unknown>;
  return Array.isArray(o.entries) && typeof o.total_count === "number"
    ? { total_count: o.total_count, entries: o.entries as DeclPair[] }
    : null;
}

interface SuffixLookupResult {
  bucket: SuffixBucket | null;
  stale?: true; // suffix index pinned to a DIFFERENT decl-index snapshot
  suppressed?: true; // the fetch budget ran out before the shard could load
}

// The suffix-index bucket for a final segment. `bucket: null` when the index is
// not shipped (namespace resolution degrades to the plain miss hint —
// decl_exists itself keeps working) or the bucket is absent. `stale` when the
// suffix manifest's decl-index pin does not match the live decl manifest — a
// mismatched deploy would verify suggestions against a different universe, so
// suggestions degrade to none (surfaced via the response hint, never silently).
async function suffixLookup(
  c: Ctx,
  segment: string,
  suffixShardCache: SuffixShardCache,
  declManifest: DeclManifest,
  budget?: FetchBudget,
): Promise<SuffixLookupResult> {
  const manifest = await memoAssetJson<DeclManifest>(c, `${SUFFIX_INDEX}/manifest.json`);
  if (!manifest?.shards) return { bucket: null };
  if (
    manifest.source_sha_or_etag &&
    declManifest.source_sha_or_etag &&
    manifest.source_sha_or_etag !== declManifest.source_sha_or_etag
  ) {
    return { bucket: null, stale: true };
  }
  const key = declShardFor(manifest, segment);
  if (!key) return { bucket: null };
  let p = suffixShardCache.get(key);
  if (!p) {
    if (budget && takeBudget(budget, 1) < 1) return { bucket: null, suppressed: true };
    p = assetJson<Record<string, unknown>>(c, `${SUFFIX_INDEX}/${key}.json`);
    suffixShardCache.set(key, p);
  }
  const shard = await p;
  const raw = shard ? own(shard, suffixBucketKey(segment)) : undefined;
  return { bucket: raw === undefined ? null : parseSuffixBucket(raw) };
}

interface RenameSuggestion {
  renamed_to?: string;
  suggestion_basis?: "verified-rename" | "namespace-resolution";
  module?: string;
  namespace_matches?: Array<{ decl: string; module: string }>;
  namespace_match_count?: number;
  hint?: string;
  suggestion_truncated?: true; // the shared fetch budget ran out mid-verification
}

// The "many sharers — qualify" hint. `normalized` marks counts taken from a
// CAPPED bucket, whose total counts normalized-segment sharers (Ne/ne merge),
// not exact-segment sharers — the wording must not overclaim.
function nsHint(n: number, suffix: string, normalized: boolean): string {
  return (
    `${n} indexed decls share this name's ${normalized ? "normalized " : ""}final segment ('${suffix}') — ` +
    "qualify the namespace (Namespace.name) and re-verify with decl_exists"
  );
}

// A rename SUGGESTION for a name the oracle rejects — never presented as a fact
// (BRIDGE item 1). Clearly-labelled bases:
//   verified-rename      — the owning cell's decl organ carries `renamed_to`
//                          (catalog/data/decl_renames.jsonl, agent + adversary
//                          verified, baked into the shards).
//   namespace-resolution — exactly one decl in the FULL decl universe (the
//                          suffix index) shares this name's EXACT final
//                          segment, and the oracle verifies it. Bucket keys are
//                          NORMALIZED (Ne/ne, prime/₁ variants share a bucket),
//                          so entries are first filtered to the exact segment —
//                          the builder contract says the consumer disambiguates
//                          on the stored exact names. 2–8 exact sharers come
//                          back as `namespace_matches` (each oracle-verified —
//                          never a forced pick, BRIDGE item 4); more than 8
//                          returns `namespace_match_count` + a hint to qualify.
//                          A CAPPED bucket ({total_count > entries.length})
//                          stores an incomplete view, so it never claims
//                          uniqueness or enumerates — it degrades to the
//                          normalized count + hint.
async function suggestRename(
  c: Ctx,
  release: BrainReleaseContext,
  name: string,
  manifest: DeclManifest,
  shardCache: DeclShardCache,
  suffixShardCache: SuffixShardCache,
  budget?: FetchBudget,
): Promise<RenameSuggestion | null> {
  const bare = bareDeclName(name);
  // (a) verified rename via the owning cell's organ. aliases.json (4.6MB) is
  // loaded only on this miss path — the all-exists hot path never pays it.
  const aliases = await cellAliases(c, release);
  const cellId = own(aliases?.decls, bare);
  if (cellId) {
    const atom = await atomFor(c, release, cellId);
    const organ = atom?.organs.find(
      (o) => o.kind === "decl" && o.renamed_to && (o.label ?? bareDeclName(o.id)) === bare,
    );
    if (organ?.renamed_to) {
      const tgt = await declLookup(c, organ.renamed_to, manifest, shardCache, budget);
      return {
        renamed_to: organ.renamed_to,
        suggestion_basis: "verified-rename",
        ...(tgt.exists ? { module: tgt.module } : {}),
        ...(tgt.suppressed ? { suggestion_truncated: true as const } : {}),
      };
    }
  }
  // (b) namespace resolution over the full universe, verified against the oracle
  const suffix = finalSegment(bare);
  const res = await suffixLookup(c, suffix, suffixShardCache, manifest, budget);
  if (res.stale) {
    return {
      hint:
        "namespace suggestions disabled: the suffix index was built from a different " +
        "decl-index snapshot (rebuild together: npm run build:indexes)",
    };
  }
  if (res.suppressed) {
    return {
      suggestion_truncated: true,
      hint: "rename-suggestion lookups exhausted this call's fetch budget — re-check in a smaller batch",
    };
  }
  const bucket = res.bucket;
  if (!bucket) return null;
  const entries = bucketEntries(bucket);
  const total = bucketTotal(bucket);
  if (total < 1) return null;
  if (total > entries.length) {
    // capped bucket: the stored entries are an incomplete view of the
    // population — never decide uniqueness or enumerate from it
    return { namespace_match_count: total, hint: nsHint(total, suffix, true) };
  }
  // uncapped: the stored entries ARE the population. Filter to the EXACT final
  // segment (never suggest the dead name itself) before any size decision.
  const exact = entries.filter(
    (e): e is DeclPair =>
      Array.isArray(e) && typeof e[0] === "string" && e[0] !== bare && finalSegment(e[0]) === suffix,
  );
  if (!exact.length) return null;
  if (exact.length > NAMESPACE_MATCH_CAP) {
    return { namespace_match_count: exact.length, hint: nsHint(exact.length, suffix, false) };
  }
  const checks = await Promise.all(
    exact.map(async ([fq]) => ({ fq, r: await declLookup(c, fq, manifest, shardCache, budget) })),
  );
  if (checks.some((x) => x.r.suppressed)) {
    // partial verification could mint a false unique — degrade honestly
    return {
      namespace_match_count: exact.length,
      hint: nsHint(exact.length, suffix, false),
      suggestion_truncated: true,
    };
  }
  const verified = checks
    .filter((x) => x.r.exists && x.r.module)
    .map((x) => ({ decl: x.fq, module: x.r.module! }));
  if (verified.length === 1) {
    return {
      renamed_to: verified[0].decl,
      suggestion_basis: "namespace-resolution",
      module: verified[0].module,
    };
  }
  if (verified.length >= 2) return { namespace_matches: verified };
  return null;
}

// One per-name verdict: exists (+module/import), a labelled rename suggestion,
// an oracle-verified `namespace_matches` shortlist (2–8 exact sharers), or —
// past the cap / on a capped bucket — `namespace_match_count` + a hint. The
// suggestion's fields spread through verbatim (one copy of the field list);
// only import_line/docs_url are derived here.
async function declVerdict(
  c: Ctx,
  release: BrainReleaseContext,
  name: string,
  manifest: DeclManifest,
  shardCache: DeclShardCache,
  suffixShardCache: SuffixShardCache,
  budget?: FetchBudget,
): Promise<Record<string, unknown>> {
  const hit = await declLookup(c, name, manifest, shardCache);
  if (hit.exists && hit.module) {
    return {
      decl: name,
      exists: true,
      library: "mathlib",
      module: hit.module,
      import_line: `import ${hit.module}`, // item 2: name alone won't compile
      docs_url: docsUrlFor(hit.module, name),
    };
  }
  const sugg = await suggestRename(c, release, name, manifest, shardCache, suffixShardCache, budget);
  if (!sugg) return { decl: name, exists: false, hint: DECL_MISS_HINT };
  return {
    decl: name,
    exists: false,
    ...sugg,
    ...(sugg.renamed_to && sugg.module
      ? { import_line: `import ${sugg.module}`, docs_url: docsUrlFor(sugg.module, sugg.renamed_to) }
      : {}),
  };
}

// Single `name` OR batch `names` (cap 16). Per name: exact existence, and when a
// name is dead, a CLEARLY-LABELLED rename suggestion (never a fact) so an agent
// that drafted 3–8 names fixes them in one round trip (BRIDGE item 1).
export async function declExistsFor(c: Ctx, release: BrainReleaseContext, nameRaw: string, namesRaw?: unknown): Promise<ApiResult> {
  const names = normalizeNames(nameRaw, namesRaw);
  if ("error" in names) return { status: 400, body: names.error };
  const manifest = await memoAssetJson<DeclManifest>(c, "/assets/decl-index/manifest.json");
  if (!manifest?.shards) return { status: 503, body: { ok: false, error: "decl index unavailable" } };
  const shardCache: DeclShardCache = new Map();
  const suffixShardCache: SuffixShardCache = new Map();
  // one suggestion budget across the whole batch (promise caches make shared
  // shards free; the budget bounds only genuinely new suggestion fetches)
  const budget: FetchBudget = { left: FETCH_CAPS.suggest, exhausted: false };

  if (!names.batch) {
    // single-name shape preserved for back-compat (adds renamed_to/import_line)
    const body = await declVerdict(c, release, names.list[0], manifest, shardCache, suffixShardCache, budget);
    return { status: 200, body: { ok: true, ...body } };
  }
  const results = await Promise.all(
    names.list.map((n) => declVerdict(c, release, n, manifest, shardCache, suffixShardCache, budget)),
  );
  const counts = { total: results.length, exists: 0, renamed: 0, missing: 0 };
  for (const r of results) {
    if (r.exists) counts.exists += 1;
    else if (r.renamed_to) counts.renamed += 1;
    else counts.missing += 1;
  }
  return { status: 200, body: { ok: true, results, counts } };
}

// Parse `name` / `names` into a validated list, or an error body. `names` may be
// a JSON array (MCP) or a comma-separated string (REST). Every name is validated
// the same way the single path always was. The ONE validator for every
// decl-name-list input: normalizeSeeds parameterizes it rather than re-stating
// the split/trim/cap/charset rules.
function normalizeNames(
  nameRaw: string,
  namesRaw: unknown,
  opts: { cap?: number; noun?: string; mapName?: (s: string) => string; dedupe?: boolean } = {},
): { list: string[]; batch: boolean } | { error: Record<string, unknown> } {
  const cap = opts.cap ?? BATCH_CAP;
  const noun = opts.noun ?? "names";
  let list: string[];
  let batch: boolean;
  if (namesRaw !== undefined && namesRaw !== null && namesRaw !== "") {
    const arr = Array.isArray(namesRaw)
      ? namesRaw.map((x) => (typeof x === "string" ? x : String(x)))
      : String(namesRaw).split(",");
    list = arr.map((s) => s.trim()).filter(Boolean);
    if (opts.mapName) list = list.map(opts.mapName);
    if (opts.dedupe) list = [...new Set(list)];
    batch = true;
    if (!list.length) return { error: { ok: false, error: `${noun} is empty` } };
    if (list.length > cap) return { error: { ok: false, error: `too many ${noun} (cap ${cap})` } };
  } else {
    const name = (nameRaw || "").trim();
    list = [name];
    batch = false;
    if (!name) return { error: { ok: false, error: "bad declaration name" } };
  }
  for (const n of list) {
    if (!n || n.length > 300 || DECL_NAME_BAD.test(n)) {
      return { error: { ok: false, error: `bad declaration name: ${JSON.stringify(n)}` } };
    }
  }
  return { list, batch };
}

// ---- premises: stored-premise retrieval (the DECL-grain depends signal) --------
//
// The Brain's cell-level depends synapses cannot serve premise selection (13.8%
// gold-premise coverage vs 99.4% for the decl universe — bench/analysis/
// brain_artifact.md), so premises come from a dedicated decl-grain asset:
// /assets/premise-index/ maps each source decl (prefix-sharded by name, same
// scheme as the decl index) to the top-K premises its stored proof used (K ≤ 12,
// hub-damped at build time; the manifest records source, pin, filters and the
// hub-drop list). Premise names are int-coded against FIXED 8192-name chunk
// tables (names/<chunk>.json — index i lives at names/<floor(i/8192)>.json at
// offset i%8192) so the shards stay small.
const PREMISE_INDEX = "/assets/premise-index";
const SEED_CAP = 8; // seeds are theorems the agent already FOUND — a handful, not a dragnet
const PREMISE_LIMIT_DEFAULT = 20;
const PREMISE_LIMIT_CAP = 50;
const PREMISE_NAME_CHUNK = 8192; // the Worker's decode constant — validated against manifest.chunk_size
// How far past `limit` the ranked window extends: rows the oracle drops (stale
// names, ~5.5% observed drift) backfill from below the cutoff instead of
// under-filling the response. The agg map already holds the candidates.
const PREMISE_BACKFILL = 12;

interface PremiseManifest {
  scheme: { min_len: number; max_len: number; pad: string };
  shards: Record<string, number>;
  chunk_size?: number; // the builder's name-table chunk size (must equal PREMISE_NAME_CHUNK)
  source?: string;
  pin?: {
    dataset_revision?: string;
    edges_sha256?: string;
    edges_bytes?: number;
    edges_url?: string;
    edges_mtime?: string; // legacy manifests only
    decl_index_etag?: string;
  };
  filters?: unknown;
  hub_drop?: unknown;
}

// `seeds` may be a JSON array (MCP) or a comma-separated string (REST). One
// validator with decl_exists (normalizeNames, parameterized): `decl:<Lib>:<Name>`
// ids are accepted and bared; duplicates collapse before the cap check.
function normalizeSeeds(seedsRaw: unknown): { list: string[] } | { error: Record<string, unknown> } {
  if (seedsRaw === undefined || seedsRaw === null || seedsRaw === "") {
    return { error: { ok: false, error: "missing seeds — pass 1–8 fully-qualified decl names" } };
  }
  const r = normalizeNames("", seedsRaw, { cap: SEED_CAP, noun: "seeds", mapName: bareDeclName, dedupe: true });
  return "error" in r ? r : { list: r.list };
}

// A premise-index shard through the per-call promise cache, budgeted like
// declLookup: a miss with an empty budget is suppressed (null), never fetched.
function premiseShard(
  c: Ctx,
  key: string,
  cache: SuffixShardCache,
  budget: FetchBudget,
): Promise<Record<string, unknown> | null> {
  let p = cache.get(key);
  if (!p) {
    if (takeBudget(budget, 1) < 1) return Promise.resolve(null);
    p = assetJson<Record<string, unknown>>(c, `${PREMISE_INDEX}/${key}.json`);
    cache.set(key, p);
  }
  return p;
}

// The ranked union of 1–8 seed theorems' stored premises. Ranking = multiplicity
// across seeds desc, then best stored per-seed rank asc (ties by chunk index for
// determinism). Every returned row carries module + import_line, resolved
// against the SAME decl oracle decl_exists uses, within the fetch budget. Seeds
// resolve through the oracle first; a dead seed goes through the SAME
// suggestRename decl_exists serves — verified renames (Basis → Module.Basis)
// and exact-segment-unique namespace resolutions both recover, noted via
// `resolved_via` — anything else lands in seeds_unknown rather than failing
// the call.
export async function premisesFor(c: Ctx, release: BrainReleaseContext, seedsRaw: unknown, limitRaw?: unknown): Promise<ApiResult> {
  const seeds = normalizeSeeds(seedsRaw);
  if ("error" in seeds) return { status: 400, body: seeds.error };
  const limit = clampLimit(limitRaw, PREMISE_LIMIT_DEFAULT, PREMISE_LIMIT_CAP);

  const premiseManifest = await memoAssetJson<PremiseManifest>(c, `${PREMISE_INDEX}/manifest.json`);
  if (!premiseManifest?.shards) {
    return { status: 503, body: { ok: false, error: "premise index unavailable" } };
  }
  // The chunk size is the builder's to choose and the manifest records it. A
  // Worker decoding with a different constant would serve wrong-but-REAL decl
  // names (they'd even pass oracle verification) — silent corruption of every
  // response — so a mismatch refuses loudly instead.
  if (premiseManifest.chunk_size !== PREMISE_NAME_CHUNK) {
    return { status: 503, body: { ok: false, error: "premise index unavailable (chunk-size mismatch)" } };
  }
  const declManifest = await memoAssetJson<DeclManifest>(c, "/assets/decl-index/manifest.json");
  if (!declManifest?.shards) return { status: 503, body: { ok: false, error: "decl index unavailable" } };

  const shardCache: DeclShardCache = new Map();
  const suffixShardCache: SuffixShardCache = new Map();
  const premiseShardCache: SuffixShardCache = new Map();

  // Fan-out budget: phases 1–2 (seed resolution + premise lists) spend from
  // cap − reserve; the reserve is released for name-chunk + module hydration,
  // so a pathological seed batch can never starve hydration to zero rows.
  // Promise caches make shared shards free; only genuinely new fetches spend.
  const budget: FetchBudget = {
    left: Math.max(FETCH_CAPS.premise - FETCH_CAPS.premiseChunkReserve, 0),
    exhausted: false,
  };

  // 1. resolve seeds against the oracle (parallel — the budget is claimed
  // synchronously per cache miss, so accounting stays exact)
  const outcomes = await Promise.all(
    seeds.list.map(async (seed) => {
      const hit = await declLookup(c, seed, declManifest, shardCache, budget);
      if (hit.exists) return { seed, decl: seed as string | null, resolved_via: undefined as string | undefined };
      const sugg = await suggestRename(c, release, seed, declManifest, shardCache, suffixShardCache, budget);
      return sugg?.renamed_to && sugg.module && sugg.renamed_to !== seed
        ? { seed, decl: sugg.renamed_to as string | null, resolved_via: sugg.suggestion_basis as string | undefined }
        : { seed, decl: null, resolved_via: undefined };
    }),
  );
  const seedsResolved: Array<Record<string, unknown>> = [];
  const seedsUnknown: string[] = [];
  const resolvedNames: string[] = [];
  for (const o of outcomes) {
    if (!o.decl) {
      seedsUnknown.push(o.seed);
      continue;
    }
    seedsResolved.push(
      o.resolved_via ? { seed: o.seed, decl: o.decl, resolved_via: o.resolved_via } : { seed: o.seed, decl: o.decl },
    );
    if (!resolvedNames.includes(o.decl)) resolvedNames.push(o.decl);
  }

  // 2. each resolved seed's stored premise ints (parallel; Promise.all keeps
  // seed order, so `via` stays deterministic)
  const listsBySeed = new Map<string, number[]>();
  const fetchedLists = await Promise.all(
    resolvedNames.map(async (decl) => {
      const key = declShardFor(premiseManifest, decl);
      if (!key) return null;
      const shard = await premiseShard(c, key, premiseShardCache, budget);
      const ints = shard ? own(shard, decl) : undefined;
      if (!Array.isArray(ints)) return null;
      // dedupe preserves first-occurrence order, so positions stay the stored rank
      const deduped = [...new Set(ints.filter((v): v is number => Number.isInteger(v) && v >= 0))];
      return [decl, deduped] as [string, number[]];
    }),
  );
  for (const row of fetchedLists) if (row) listsBySeed.set(row[0], row[1]);

  // 3. rank the FULL union BEFORE any hydration fetch: multiplicity desc, best
  // rank asc. No early slice — a bounded window past `limit` lets rows the
  // oracle drops backfill from below the cutoff.
  const agg = new Map<number, { count: number; best: number; via: string[] }>();
  for (const [decl, ints] of listsBySeed) {
    ints.forEach((idx, rank) => {
      const row = agg.get(idx);
      if (row) {
        row.count += 1;
        if (rank < row.best) row.best = rank;
        row.via.push(decl);
      } else {
        agg.set(idx, { count: 1, best: rank, via: [decl] });
      }
    });
  }
  const ranked = [...agg].sort((a, b) => b[1].count - a[1].count || a[1].best - b[1].best || a[0] - b[0]);
  const window = ranked.slice(0, limit + PREMISE_BACKFILL);

  budget.left += FETCH_CAPS.premiseChunkReserve; // release the hydration reserve

  // 4. name-table chunks for the window (deduped in rank order, budgeted)
  const wantChunks = [...new Set(window.map(([idx]) => Math.floor(idx / PREMISE_NAME_CHUNK)))];
  const chunkCache = new Map<number, unknown[] | null>();
  await Promise.all(
    wantChunks.slice(0, takeBudget(budget, wantChunks.length)).map(async (n) => {
      chunkCache.set(n, await assetJson<unknown[]>(c, `${PREMISE_INDEX}/names/${n}.json`));
    }),
  );

  // 5. int → name. Chunks are plain string arrays — the only shape the builder
  // emits (build-premise-index.ts assemble()).
  const cands = window.map(([idx, row]) => {
    const chunk = chunkCache.get(Math.floor(idx / PREMISE_NAME_CHUNK));
    const entry = chunk ? chunk[idx % PREMISE_NAME_CHUNK] : undefined;
    return { decl: typeof entry === "string" ? entry : null, score: row.count, via: row.via };
  });

  // 6. module resolution against the SAME oracle decl_exists uses (parallel,
  // promise-cached, budget claimed in rank order)
  const mods = await Promise.all(
    cands.map((cand) =>
      cand.decl
        ? declLookup(c, cand.decl, declManifest, shardCache, budget)
        : Promise.resolve({ exists: false, module: undefined as string | undefined }),
    ),
  );

  // fill to `limit`, walking past dropped rows (stale names, budget gaps) —
  // the window below the cutoff backfills what the oracle rejects
  const premises: Array<Record<string, unknown>> = [];
  let dropped = 0;
  for (let i = 0; i < cands.length && premises.length < limit; i++) {
    const cand = cands[i];
    const module = cand.decl ? mods[i].module : undefined;
    if (!cand.decl || !module) {
      dropped += 1; // chunk gap, stale name, or past the budget — counted, never silent
      continue;
    }
    premises.push({
      decl: cand.decl,
      module,
      import_line: `import ${module}`,
      score: cand.score, // multiplicity: how many seeds cite this premise
      via: cand.via, // the RESOLVED seed names that cite it
    });
  }

  const body: Record<string, unknown> = {
    ok: true,
    premises,
    seeds_resolved: seedsResolved,
    seeds_unknown: seedsUnknown,
    // THIS index's own pin (edges snapshot + the decl-index etag it was joined
    // against). The top-level `snapshot` echo tracks the brain cells manifest —
    // a different artifact on a different rebuild cadence — so staleness
    // questions about premise data must read index_pin, not snapshot.
    index_pin: premiseManifest.pin
      ? {
          ...(premiseManifest.pin.dataset_revision
            ? {
                dataset_revision: premiseManifest.pin.dataset_revision,
                edges_sha256: premiseManifest.pin.edges_sha256 ?? null,
                edges_bytes: premiseManifest.pin.edges_bytes ?? null,
                edges_url: premiseManifest.pin.edges_url ?? null,
              }
            : {}),
          ...(premiseManifest.pin.edges_mtime
            ? { edges_mtime: premiseManifest.pin.edges_mtime }
            : {}),
          decl_index_etag: premiseManifest.pin.decl_index_etag ?? null,
        }
      : null,
  };
  if (dropped > 0) body.premises_dropped = dropped; // never silently — the counter is the honesty
  if (budget.exhausted) body.truncated = true; // the fetch budget stopped resolution/hydration early
  if (!resolvedNames.length) {
    body.hint = "no seed resolved in the decl index — verify seed names with decl_exists first";
  }
  return { status: 200, body };
}

// ---- bridge: the composite first call of an autoformalization loop (item 7) ----

const NEXT_TOOLS = [
  "brain_cell <via_cell> — the full atom card (every organ, embedded Lean code, snippets, breadcrumb)",
  "decl_exists {names:[…]} — re-verify EVERY decl name you write before citing it",
  "brain_premises {seeds:[…]} — seed with the theorems you just found; their stored proof premises come back ranked + oracle-verified",
  "brain_neighborhood <via_cell> kinds=depends — walk the formal dependency chain across turns (cursored)",
  "brain_transfer direction=formal_to_informal — pull the informal side (article, description) back",
];
const BRIDGE_DEPENDS_CAP = 12; // one-hop depends partners inlined; the rest counted

// Build an id→label map for depends partners (cells from labels.json, supercells
// from supercells.json — both memoized). Labels only; the bridge never inlines a
// partner's whole neighborhood.
async function partnerLabels(c: Ctx, release: BrainReleaseContext): Promise<Map<string, string | null>> {
  const map = new Map<string, string | null>();
  const labels = await cellLabels(c, release);
  for (const r of labels ?? []) map.set(r.id, r.label ?? null);
  const file = await supercellsFile(c, release);
  for (const [p, e] of Object.entries(file?.supercells ?? {})) map.set(p, e.label ?? null);
  return map;
}

function oneHopDepends(
  atom: Atom,
  labelMap: Map<string, string | null>,
): Record<string, unknown> {
  const partners: Array<Record<string, unknown>> = [];
  let total = 0;
  for (const s of [...atom.syn].sort(synCmp)) {
    if (!s.kinds?.depends) continue;
    total += 1;
    if (partners.length < BRIDGE_DEPENDS_CAP) {
      partners.push({ id: s.id, label: labelMap.get(s.id) ?? null, w: s.w });
    }
  }
  const withheldByShard = atom.truncated?.syn ?? 0;
  return {
    partners,
    returned: partners.length,
    total, // depends synapses within the (shard-capped) list
    withheld_by_shard: withheldByShard, // depends bonds may also sit past the shard cap
    truncated: partners.length < total || withheldByShard > 0,
  };
}

// A statement query ("every finitely generated vector space has a basis")
// matches no single label, so label search (labels CONTAINING the query) finds
// nothing. The bridge also resolves the other direction: atoms whose label/aka
// appears IN the statement ("vector space", "basis"), word-bounded and length-
// floored to keep short English words out, ranked longest-first (more specific).
// This is still "resolve to atoms by label/alias" — statement-level EMBEDDING
// transfer stays deferred (BRIDGE, "hypotheses get lost").
const MIN_STMT_LABEL = 4;
function containsWord(hay: string, needle: string): boolean {
  let i = hay.indexOf(needle);
  while (i >= 0) {
    const before = i === 0 ? "" : hay[i - 1];
    const after = i + needle.length >= hay.length ? "" : hay[i + needle.length];
    if (!/[a-z0-9]/.test(before) && !/[a-z0-9]/.test(after)) return true;
    i = hay.indexOf(needle, i + 1);
  }
  return false;
}
function atomsInStatement(labels: BrainLabelRow[], ql: string): Array<{ id: string; len: number }> {
  const hits: Array<{ id: string; len: number }> = [];
  for (const r of labels) {
    let best = 0;
    for (const name of [r.label, ...(r.aka ?? [])]) {
      const n = (name || "").toLowerCase();
      if (n.length >= MIN_STMT_LABEL && n.length > best && containsWord(ql, n)) best = n.length;
    }
    if (best) hits.push({ id: r.id, len: best });
  }
  hits.sort((a, b) => b.len - a.len || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  return hits;
}

// GET /api/brain/bridge?q=<informal statement> — search + resolve to atoms + rank
// decl organs across the top atoms + verify existence + attach signature / import
// / bond / breadcrumb + one-hop depends. ONE response designed to be the FIRST
// call of an autoformalization loop, ending in `next_tools` hints. Honest
// abstention (item 4): under the confidence floor it returns match:"none" +
// nearest rather than a forced grounding.
export async function bridgeFor(c: Ctx, release: BrainReleaseContext, qRaw: string, limitRaw?: unknown): Promise<ApiResult> {
  const q = (qRaw || "").trim();
  if (!q) return { status: 400, body: { ok: false, error: "missing ?q= (an informal statement or concept)" } };
  const limit = clampLimit(limitRaw, 8, BATCH_CAP);

  // 1. candidate atoms — an exact id/label first (identity), then label+aka search
  const considered: Array<{ id: string; resolved_from: string }> = [];
  const seen = new Set<string>();
  const exact = await resolveAtomKey(c, release, q);
  if (exact) {
    considered.push({ id: exact.id, resolved_from: exact.resolved_from });
    seen.add(exact.id);
  }
  const labels = await cellLabels(c, release);
  const ql = q.toLowerCase();
  if (labels) {
    // labels CONTAINING the query (short concept queries)…
    for (const r of searchLabels(labels, ql, "", 5)) {
      if (!seen.has(r.id)) {
        considered.push({ id: r.id, resolved_from: "search" });
        seen.add(r.id);
      }
    }
    // …then atoms whose label appears IN the query (statement queries)
    for (const s of atomsInStatement(labels, ql).slice(0, 5)) {
      if (!seen.has(s.id)) {
        considered.push({ id: s.id, resolved_from: "statement" });
        seen.add(s.id);
      }
    }
  }
  if (!considered.length) {
    return {
      status: 404,
      body: {
        ok: false,
        error: "no atom matched q",
        q,
        match: "none",
        suggestions: await suggestionsFor(c, release, q),
        hint: "try /api/brain/search?q= for fuzzy lookup, then /api/brain/cell",
      },
    };
  }

  const top = considered.slice(0, 3);
  const fetched = await Promise.all(top.map((a) => atomFor(c, release, a.id)));
  const atomsOut = top.map((a, i) => ({
    id: a.id,
    kind: fetched[i]?.kind ?? null,
    label: fetched[i]?.label ?? null,
    resolved_from: a.resolved_from,
    ...(fetched[i]?.breadcrumb ? { breadcrumb: fetched[i]!.breadcrumb } : {}),
  }));

  // 2. rank decl organs ACROSS the top atoms (exact first, then atom order, name)
  const declOrgans: Array<{ o: Organ; atom: Atom }> = [];
  top.forEach((_a, i) => {
    const atom = fetched[i];
    if (!atom) return;
    for (const o of organsOf(atom, "decl").sort(rankDecl)) declOrgans.push({ o, atom });
  });
  declOrgans.sort((x, y) => {
    const rank = (o: Organ) => (o.bond === "exact" ? 0 : o.bond ? 1 : 2);
    const r = rank(x.o) - rank(y.o);
    if (r) return r;
    const lx = x.o.label ?? x.o.id, ly = y.o.label ?? y.o.id;
    return lx < ly ? -1 : lx > ly ? 1 : 0;
  });
  const chosen = declOrgans.slice(0, limit);

  // 3. verify existence + attach signature/module/import/bond/breadcrumb
  const manifest = await memoAssetJson<DeclManifest>(c, "/assets/decl-index/manifest.json");
  const shardCache: DeclShardCache = new Map();
  const suffixShardCache: SuffixShardCache = new Map();
  // one suggestion budget across all dead organs (same discipline as decl_exists)
  const suggestBudget: FetchBudget = { left: FETCH_CAPS.suggest, exhausted: false };
  const hits = await Promise.all(
    chosen.map(async ({ o, atom }) => {
      const name = o.label ?? bareDeclName(o.id);
      const exists = manifest ? (await declLookup(c, name, manifest, shardCache)).exists : null;
      const hit: Record<string, unknown> = {
        decl: name,
        exists, // verified against the oracle; null only if the index is unavailable
        module: o.module ?? null,
        ...(o.module ? { import_line: `import ${o.module}` } : {}),
        bond: o.bond ?? null,
        ...(o.decl_kind ? { decl_kind: o.decl_kind } : {}),
        ...(o.code ? { code: o.code } : {}),
        docs_url: o.module ? docsUrlFor(o.module, name) : `${SITE_ORIGIN}/decl/${encodeURIComponent(name)}`,
        via_cell: atom.id,
        cell_label: atom.label,
        ...(atom.breadcrumb ? { breadcrumb: atom.breadcrumb } : {}),
      };
      // a dead cited name gets the same labelled suggestion decl_exists serves
      if (exists === false && manifest) {
        const sugg = await suggestRename(c, release, name, manifest, shardCache, suffixShardCache, suggestBudget);
        if (sugg?.renamed_to) {
          hit.renamed_to = sugg.renamed_to;
          hit.suggestion_basis = sugg.suggestion_basis;
          if (sugg.module) hit.suggested_import_line = `import ${sugg.module}`;
        } else if (sugg?.namespace_matches) {
          hit.namespace_matches = sugg.namespace_matches; // 2–8 verified sharers, no forced pick
        } else if (sugg?.namespace_match_count) {
          hit.namespace_match_count = sugg.namespace_match_count; // hub segment: count + qualify hint
          hit.hint = sugg.hint;
        }
        if (sugg?.suggestion_truncated) hit.suggestion_truncated = true;
      }
      return hit;
    }),
  );

  // 4. one-hop depends from the PRIMARY atom + honest abstention
  const primary = fetched[0];
  const labelMap = await partnerLabels(c, release);
  const depends = primary ? oneHopDepends(primary, labelMap) : { partners: [], returned: 0, total: 0, truncated: false };

  const bestBond = (chosen[0]?.o.bond as string | null) ?? null;
  // "search"/"statement" are fuzzy resolutions — not identity
  const resolvedByIdentity = top[0].resolved_from !== "search" && top[0].resolved_from !== "statement";
  const { match, clears } = matchClass(bestBond, resolvedByIdentity);
  const body: Record<string, unknown> = {
    ok: true,
    q,
    match,
    confidence_floor: CONFIDENCE_FLOOR,
    atoms: atomsOut,
    depends,
    next_tools: NEXT_TOOLS,
  };
  if (!chosen.length || !clears) {
    body.match = "none";
    body.hits = [];
    body.nearest = atomsOut.slice(0, 3).map((a) => ({
      ...a,
      why: chosen.length
        ? "atom matched by label similarity only, and the best bond is not exact"
        : "no Mathlib declaration is an organ of this candidate atom",
    }));
    body.note = chosen.length
      ? "no formalization cleared the confidence floor — nearest candidate atoms returned instead of a forced grounding"
      : "the candidate atoms hold no Mathlib declaration — nearest atoms returned; try brain_neighborhood or a different phrasing";
  } else {
    body.hits = hits;
    if (match !== "exact") body.note = noteForBond(bestBond, String(hits[0].decl), String(hits[0].cell_label ?? ""));
  }
  return { status: 200, body };
}

// ---- routes -------------------------------------------------------------------

const CACHE_HEADERS = { "Cache-Control": "public, max-age=0, must-revalidate" };
const PAGE_CACHE_HEADERS = { "Cache-Control": "public, max-age=3600" };

// The unqualified compatibility URLs must revalidate so an edge cache cannot
// label an old body as the new current release. Immutable assets remain freely
// cacheable under their release-qualified URLs.
async function send(c: Ctx, release: BrainReleaseContext, r: ApiResult): Promise<Response> {
  r.body.snapshot = await snapshotFor(c, release);
  r.body.release_id = release.releaseId;
  return c.json(r.body, r.status, r.status === 200 ? CACHE_HEADERS : undefined);
}

async function withRelease(
  c: Ctx,
  run: (release: BrainReleaseContext) => Promise<Response>,
): Promise<Response> {
  try {
    const release = await resolveBrainRelease(c);
    if (!release) {
      return c.json(
        { ok: false, error: "brain release unavailable", snapshot: null, release_id: null },
        503,
      );
    }
    return await run(release);
  } catch (error) {
    if (isBrainReleaseUnavailableError(error)) {
      return c.json(
        { ok: false, error: "brain release unavailable", snapshot: null, release_id: error.releaseId },
        503,
      );
    }
    throw error;
  }
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
  app.use("/api/brain/decl", rateLimitGate);
  app.use("/api/brain/premises", rateLimitGate);
  app.use("/api/brain/bridge", rateLimitGate);

  app.get("/api/brain/cell", async (c) => withRelease(c, async (release) =>
    send(c, release, await cellFor(c, release, c.req.query("key") ?? ""))));

  // v2 entry point. The unit card became the CELL card (the atom subsumes it —
  // a unit was QID ∘ article ∘ decls ∘ xrefs, which is exactly a cell's organs),
  // so this is a true alias rather than a shim: nothing that resolved before 404s.
  app.get("/api/brain/unit", async (c) => withRelease(c, async (release) =>
    send(c, release, await cellFor(c, release, c.req.query("key") ?? ""))));

  app.get("/api/brain/transfer", async (c) => withRelease(c, async (release) =>
    send(c, release, await transferFor(
      c, release, c.req.query("q") ?? "", c.req.query("direction") ?? "", c.req.query("limit"),
    ))));

  app.get("/api/brain/neighborhood", async (c) => withRelease(c, async (release) =>
    send(c, release, await neighborhoodFor(
      c,
      release,
      c.req.query("id") ?? "",
      c.req.query("kinds"),
      c.req.query("limit"),
      c.req.query("traces"),
      c.req.query("min_w"),
      c.req.query("cursor"),
      c.req.query("min_conf"),
    ))));

  app.get("/api/brain/snippets", async (c) => withRelease(c, async (release) =>
    send(c, release, await snippetsFor(c, release, c.req.query("id") ?? ""))));

  // Batch decl existence + labelled rename suggestions (BRIDGE item 1). `names`
  // is comma-separated over REST (cap 16); `name` stays the single-decl form.
  app.get("/api/brain/decl", async (c) => withRelease(c, async (release) =>
    send(c, release, await declExistsFor(c, release, c.req.query("name") ?? "", c.req.query("names")))));

  // Stored-premise retrieval: `seeds` is comma-separated over REST (cap 8).
  app.get("/api/brain/premises", async (c) => withRelease(c, async (release) =>
    send(c, release, await premisesFor(c, release, c.req.query("seeds"), c.req.query("limit")))));

  // The composite first call of an autoformalization loop (BRIDGE item 7).
  app.get("/api/brain/bridge", async (c) => withRelease(c, async (release) =>
    send(c, release, await bridgeFor(c, release, c.req.query("q") ?? "", c.req.query("limit")))));

  app.get("/api/brain/filter", async (c) => withRelease(c, async (release) =>
    send(c, release, await filterFor(
      c,
      release,
      c.req.query("f"),
      c.req.query("type"),
      c.req.query("limit"),
      c.req.query("cursor"),
      c.req.query("under"),
    ))));

  app.get("/api/brain/search", async (c) => withRelease(c, async (release) =>
    send(c, release, await searchFor(
      c, release, c.req.query("q") ?? "", c.req.query("type"), c.req.query("limit"),
    ))));

  // The human-readable reference for everything above + the MCP endpoint.
  app.get("/brain/api", (c) => c.html(API_REFERENCE_HTML, 200, PAGE_CACHE_HEADERS));
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
<p class="muted">Read-only and unauthenticated. Current-selector responses revalidate
(<code>Cache-Control: public, max-age=0, must-revalidate</code>); immutable release assets remain cacheable.
Base URL <code>https://wikilean.jackmccarthy.org</code>.
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
only STATEMENTS a cell claims are organs). The cell layer is the only resolver.</p>

<h2>Connect over MCP (recommended for agents)</h2>
<pre><code>claude mcp add --transport http wikibrain https://wikilean.jackmccarthy.org/mcp</code></pre>
<p>A dependency-free streamable-HTTP MCP server (JSON-RPC 2.0, stateless, single-response
mode) exposing nine tools: <code>brain_bridge</code>, <code>brain_search</code>,
<code>brain_cell</code>, <code>brain_transfer</code>, <code>brain_neighborhood</code>,
<code>brain_snippets</code>, <code>brain_filter</code>, <code>decl_exists</code>,
<code>brain_premises</code>.
<code>brain_unit</code> still answers, as an alias of <code>brain_cell</code> — the v2 unit
card <em>became</em> the cell card. Rate limit: 120 requests/min per IP. Every response echoes
<code>release_id</code> and <code>snapshot:{generated_at,pin}</code>.</p>

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

<h3>GET /api/brain/bridge?q=&amp;limit=</h3>
<p>The composite <b>first call of an autoformalization loop</b>: an informal statement in,
existence-verified Mathlib decls out — each with its <code>code</code> signature,
<code>module</code> + <code>import_line</code>, <code>bond</code> quality
(<code>exact</code> vs <code>generalization</code>/…), the atom's breadcrumb, and capped
one-hop <code>depends</code> synapses. Abstains honestly: below the confidence floor it
returns <code>match:"none"</code> with the nearest atoms instead of a forced answer, and
says so in <code>match_rule</code>. Ends with <code>next_tools</code> hints.</p>
<pre><code>curl 'https://wikilean.jackmccarthy.org/api/brain/bridge?q=every%20finitely%20generated%20vector%20space%20has%20a%20basis'</code></pre>

<h3>GET /api/brain/decl?name= | names=&lt;csv, ≤16&gt;</h3>
<p>Existence oracle for declaration names — <b>batch it</b>: agents draft statements citing
several decls, and one round-trip beats eight. Per name: <code>exists</code>, and when false,
namespace resolution over the FULL decl index: a <code>renamed_to</code> suggestion labelled
by <code>suggestion_basis</code> — <code>"verified-rename"</code> (an agent read the
declaration in the checkout and an adversarial verifier upheld it) vs
<code>"namespace-resolution"</code> (exactly one indexed decl shares the final segment,
oracle-verified — a lead, not a fact). When 2&ndash;8 decls share the segment the verified
list returns as <code>namespace_matches:[{decl,module}]</code> with no forced pick; more
than 8 returns <code>namespace_match_count</code> + a hint to qualify the namespace.</p>
<pre><code>curl 'https://wikilean.jackmccarthy.org/api/brain/decl?names=Basis,Module.Basis,AddCircle.fourierCoeff,NotARealName'</code></pre>

<h3>GET /api/brain/premises?seeds=&lt;csv, ≤8&gt;&amp;limit=</h3>
<p>Stored-premise retrieval for proof drafting — use it <b>after</b> search, never instead
of it: seed with the anchor theorems you already found (1&ndash;8 fully-qualified decl names)
and get back the ranked union of the premises their stored proofs actually used. Ranking =
multiplicity across seeds, then stored per-seed rank; every row is oracle-verified and
carries <code>module</code> + <code>import_line</code> + <code>score</code> +
<code>via</code> (the seeds that cite it). A dead seed that namespace-resolves uniquely is
auto-resolved and says so (<code>resolved_via</code> in <code>seeds_resolved</code>);
unresolvable seeds return in <code>seeds_unknown</code> instead of failing the call.
<code>limit</code> defaults 20, cap 50.</p>
<pre><code>curl 'https://wikilean.jackmccarthy.org/api/brain/premises?seeds=Nat.ModEq.pow_totient,Nat.totient_prime'</code></pre>

<h3>GET /api/brain/neighborhood?id=&amp;kinds=&amp;limit=&amp;traces=&amp;min_w=&amp;min_conf=&amp;cursor=</h3>
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
<code>GET /decl/&lt;name&gt;</code> (decl → docs redirect; JSON with <code>Accept: application/json</code>).</p>

<h2>Provenance &amp; licensing</h2>
<p>Brain cell/synapse data is CC0. Every organ and every synapse trace carries a
<code>prov</code> index into the shard manifest's <code>prov</code> table. Snippets are stored only
where the source license permits and each row carries its license
(nLab attribution · Stacks GFDL · LMFDB/OEIS CC-BY-SA-4.0 · ProofWiki CC-BY-SA-3.0 ·
PlanetMath CC-BY-SA · Mathlib Apache-2.0); other sources deep-link out.</p>
</main>
</body>
</html>`;
