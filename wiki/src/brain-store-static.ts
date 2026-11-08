// StaticStore: the BrainStore over a release's static JSON files
// (cells/manifest.json, aliases.json, labels.json, supercells.json, the cell
// shards and xref_index.json). This is the code the routes used to run inline,
// moved behind the interface unchanged: the same files, loaded in the same
// order under the same conditions, the same shape checks and the same memos.
// Fetch-count tests pin that, so keep every loader awaited one after another
// (never Promise.all two loaders): on a failure the later file must not be
// requested at all.
//
// Every parsed file stays in brain.ts's release-keyed _assetMemo (through
// requiredBrainAssetJson); the xref caches below are keyed by release id too.
// A StaticStore instance holds nothing but its request and release, so one is
// built per call. Keep this module's top level free of brain.ts value uses:
// brain.ts imports brain-api.ts, which imports this module, so when brain.ts
// is the entry point this module evaluates before brain.ts's own bindings
// exist.
import type { Context } from "hono";
import type { Env } from "./env.js";
import {
  BrainReleaseUnavailableError,
  own,
  registerBrainReleaseCacheEvictor,
  requiredBrainAssetJson,
  searchLabels,
  type BrainLabelRow,
  type BrainReleaseContext,
} from "./brain.js";
import { declShardFor } from "./decl.js";
import type { CellEntry, SupercellEntry, Synapse } from "./brain-api.js";
import type { AtomHead, BrainStore, Crumb, FilterPage, KeyHits, KeyLookup, SupercellHead } from "./brain-store.js";

type Ctx = Context<{ Bindings: Env }>;

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

// Walk `parent` to the root — supercells.json is a tree, so the breadcrumb is
// derived rather than stored (cells ship theirs prebuilt).
function supercellBreadcrumb(map: Record<string, SupercellEntry>, path: string): Crumb[] {
  const crumbs: Crumb[] = [];
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

// The fields a search hit or label lookup reads off a supercells.json entry
// (the search route's superRow shapes them), never the whole entry.
function supercellHead(path: string, e: SupercellEntry): SupercellHead {
  return {
    id: path,
    label: e.label,
    ...(e.organs?.length ? { aka: e.organs.map((o) => o.label ?? o.id) } : {}),
    ...(e.cells?.length ? { n_cells: e.cells.length } : {}),
  };
}

// Rows hydrated from partner shards per request (below). Each row costs at most
// one shard fetch, so this bounds the fan-out; it equals the neighborhood's
// default `limit`, so an unmodified call is fully hydrated. Whatever it drops is
// DECLARED per row (`traces_unavailable`) and counted in `traces_hydrated` —
// never silent.
const TRACE_HYDRATION_MAX = 50;

// A statement query ("every finitely generated vector space has a basis")
// matches no single label, so label search (labels CONTAINING the query) finds
// nothing. The bridge also resolves the other direction: atoms whose label/aka
// appears IN the statement ("vector space", "basis"), word-bounded and length-
// floored to keep short English words out, ranked longest-first (more specific).
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

// ---- the static xref index (GET /api/brain/edges) -----------------------------

// Static external-page → nodes reverse index (built by build_shards.py →
// <release>/xref_index.json). Cached by immutable release within the shared
// two-release overlap window; community (D1) partners are still queried live.
const _xrefIndex = new Map<string, Promise<Record<string, string[]>>>();
// Its inversion, node id → [pages the node statically xrefs]. Both caches are
// keyed by release because one isolate may overlap two current selectors.
const _xrefPagesByNode = new Map<string, Promise<Record<string, string[]>>>();
// Registered on first use, not at load: this module can evaluate before
// brain.ts's evictor set exists (see the header). Nothing is cached before the
// first use, so nothing can miss an eviction.
let _xrefEvictorRegistered = false;
function registerXrefEvictor(): void {
  if (_xrefEvictorRegistered) return;
  _xrefEvictorRegistered = true;
  registerBrainReleaseCacheEvictor((releaseId) => {
    _xrefIndex.delete(releaseId);
    _xrefPagesByNode.delete(releaseId);
  });
}
/** test-only: clear the isolate-lifetime static-index caches between cases */
export function _resetXrefCaches(): void {
  _xrefIndex.clear();
  _xrefPagesByNode.clear();
}
/** test-only: expose release keys so the overlap bound can be asserted */
export function _xrefCacheReleaseIds(): {
  xrefIndex: string[];
  xrefPagesByNode: string[];
} {
  return {
    xrefIndex: [..._xrefIndex.keys()],
    xrefPagesByNode: [..._xrefPagesByNode.keys()],
  };
}
async function getXrefIndex(c: Ctx, release: BrainReleaseContext): Promise<Record<string, string[]>> {
  registerXrefEvictor();
  let hit = _xrefIndex.get(release.releaseId);
  if (!hit) {
    let pending: Promise<Record<string, string[]>>;
    pending = requiredBrainAssetJson<unknown>(c, release, "xref_index.json")
      .then((value) => {
        if (typeof value !== "object" || value === null || Array.isArray(value)) {
          throw new BrainReleaseUnavailableError(
            release.releaseId,
            "xref_index.json",
            "declared asset has an invalid shape",
          );
        }
        for (const nodes of Object.values(value)) {
          if (!Array.isArray(nodes) || nodes.some((node) => typeof node !== "string")) {
            throw new BrainReleaseUnavailableError(
              release.releaseId,
              "xref_index.json",
              "declared asset has an invalid shape",
            );
          }
        }
        return value as Record<string, string[]>;
      })
      .catch((error) => {
        if (_xrefIndex.get(release.releaseId) === pending) _xrefIndex.delete(release.releaseId);
        throw error;
      });
    hit = pending;
    _xrefIndex.set(release.releaseId, pending);
  }
  return hit;
}
async function getXrefPagesByNode(c: Ctx, release: BrainReleaseContext): Promise<Record<string, string[]>> {
  registerXrefEvictor();
  let hit = _xrefPagesByNode.get(release.releaseId);
  if (!hit) {
    let pending: Promise<Record<string, string[]>>;
    pending = getXrefIndex(c, release).then((idx) => {
      const inv: Record<string, string[]> = Object.create(null);
      for (const [page, nodes] of Object.entries(idx)) {
        for (const n of nodes) (inv[n] ??= []).push(page);
      }
      return inv;
    }).catch((error) => {
      if (_xrefPagesByNode.get(release.releaseId) === pending) {
        _xrefPagesByNode.delete(release.releaseId);
      }
      throw error;
    });
    hit = pending;
    _xrefPagesByNode.set(release.releaseId, pending);
  }
  return hit;
}

// ---- the store ------------------------------------------------------------------

export class StaticStore implements BrainStore {
  private readonly c: Ctx;
  private readonly release: BrainReleaseContext;

  constructor(c: Ctx, release: BrainReleaseContext) {
    this.c = c;
    this.release = release;
  }

  async snapshotSource(): Promise<{ prov: Array<Record<string, string>>; _meta?: Record<string, unknown> }> {
    return cellsManifest(this.c, this.release);
  }

  async cellPage(id: string): Promise<CellEntry | null> {
    return cellEntry(this.c, this.release, id);
  }

  async supercell(path: string): Promise<{ entry: SupercellEntry; breadcrumb: Crumb[] } | null> {
    const file = await supercellsFile(this.c, this.release);
    const map = file?.supercells;
    const e = map ? own(map, path) : undefined;
    if (!e || !map) return null;
    return { entry: e, breadcrumb: supercellBreadcrumb(map, path) };
  }

  // Lazy, in precedence order: aliases.json first, labels.json only after every
  // requested alias lookup missed, supercells.json only after the labels did.
  // A present alias value is reported even when falsy (atomIdForOrgan returns
  // it as is), but only a truthy one stops the walk, as resolveAtomKey's own
  // checks did.
  async keyHits(k: KeyLookup): Promise<KeyHits> {
    const aliases = await cellAliases(this.c, this.release);
    const out: KeyHits = {};
    if (k.organ !== undefined) {
      const organ = own(aliases?.organs, k.organ);
      if (organ !== undefined) out.organ = organ;
      if (organ) return out;
    }
    if (k.decl !== undefined) {
      const decl = own(aliases?.decls, k.decl);
      if (decl !== undefined) out.decl = decl;
      if (decl) return out;
    }
    if (k.slug !== undefined) {
      const slug = own(aliases?.slugs, k.slug);
      if (slug !== undefined) out.slug = slug;
      if (slug) return out;
    }
    if (k.name === undefined) return out;
    const { keyLc, slugLc } = k.name;

    // exact label / aka, case-insensitive, over the atom label index
    const labels = await cellLabels(this.c, this.release);
    const byLabel = labels?.find(
      (r) =>
        (r.label || "").toLowerCase() === keyLc ||
        (r.aka || []).some((a) => a.toLowerCase() === keyLc),
    );
    if (byLabel) return { ...out, name: byLabel.id };

    // supercell labels are not in labels.json (it indexes cells) — match a
    // field concept's own label through the supercell's organs.
    //
    // `aliases.json.slugs` indexes CELL slugs only and supercell organs ship no
    // `slug` at all, so a rule-5 field concept's own article slug missed every
    // index above and 404'd — 61 of them, including `Linear_algebra`, the very
    // example the docs give for a slug resolving. An enwiki slug IS the title with
    // spaces underscored (SCHEMA: "an article is the `enwiki` sitelink of its
    // concept QID"), so the caller undoes that (slugLc) and the organ label
    // matches. Cells are matched first, above, and so still win any collision.
    const file = await supercellsFile(this.c, this.release);
    for (const [path, e] of Object.entries(file?.supercells ?? {})) {
      if (
        (e.organs ?? []).some((o) => {
          const ol = (o.label || "").toLowerCase();
          return ol === keyLc || ol === slugLc;
        })
      ) {
        return { ...out, name: path };
      }
    }
    return out;
  }

  async searchNames(q: string, kind: "" | "cell" | "supercell", limit: number): Promise<AtomHead[]> {
    const ql = q.toLowerCase();
    const labels = await cellLabels(this.c, this.release);
    const file = await supercellsFile(this.c, this.release);
    const out: AtomHead[] = [];
    // cells by label + aka (searchLabels already ranks prefix before substring)
    if (kind !== "supercell") {
      for (const row of searchLabels(labels, ql, "", limit)) out.push({ kind: "cell", row });
    }
    // supercells, matched on the folder label AND its organ labels — a folder's
    // human name lives on its field concept ("Linear algebra", not
    // "LinearAlgebra"). A full page of cells already fills every caller's
    // limit, so the scan is skipped then.
    if (kind !== "cell" && out.length < limit) {
      const starts: AtomHead[] = [], contains: AtomHead[] = [];
      for (const [path, e] of Object.entries(file?.supercells ?? {})) {
        const names = [e.label ?? "", ...(e.organs ?? []).map((o) => o.label ?? "")]
          .map((n) => n.toLowerCase())
          .filter(Boolean);
        if (names.some((n) => n.startsWith(ql))) starts.push({ kind: "supercell", head: supercellHead(path, e) });
        else if (names.some((n) => n.includes(ql))) contains.push({ kind: "supercell", head: supercellHead(path, e) });
      }
      out.push(...[...starts, ...contains].slice(0, limit));
    }
    return out;
  }

  async searchCellNames(q: string, limit: number): Promise<BrainLabelRow[]> {
    const labels = await cellLabels(this.c, this.release);
    return searchLabels(labels, q.toLowerCase(), "", limit);
  }

  async statementAtoms(q: string, limit: number): Promise<{ hits: Array<{ id: string; len: number }>; truncated: boolean }> {
    const labels = await cellLabels(this.c, this.release);
    return { hits: atomsInStatement(labels, q.toLowerCase()).slice(0, limit), truncated: false };
  }

  // Cells from labels.json (the first row per id; ids are unique there), then
  // supercells from supercells.json, which win a shared id as they did in the
  // bridge's old partner map.
  async labelRows(ids: string[]): Promise<Map<string, AtomHead>> {
    const labels = await cellLabels(this.c, this.release);
    const file = await supercellsFile(this.c, this.release);
    const want = new Set(ids);
    const out = new Map<string, AtomHead>();
    if (!want.size) return out;
    for (const row of labels) {
      if (want.has(row.id) && !out.has(row.id)) out.set(row.id, { kind: "cell", row });
    }
    for (const id of want) {
      const e = own(file?.supercells, id);
      if (e) out.set(id, { kind: "supercell", head: supercellHead(id, e) });
    }
    return out;
  }

  // Supercell `syn` rows ship traceless: supercells.json is fetched eagerly and
  // carrying them would treble it. That file's own `_meta.traces` names the
  // neighborhood endpoint as the remedy — and it was a dead pointer, because the
  // Worker reads only static shards and no shard carries supercell traces.
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
  async supercellTraces(atomId: string, rows: Synapse[]): Promise<Map<string, Synapse>> {
    const want = rows
      .filter((s) => !s.id.startsWith("path:") && !s.traces?.length)
      .slice(0, TRACE_HYDRATION_MAX)
      .map((s) => s.id);
    const entries = await cellEntries(this.c, this.release, want);
    const out = new Map<string, Synapse>();
    for (const id of want) {
      const mirror = entries.get(id)?.syn?.find((s) => s.id === atomId);
      if (mirror?.traces?.length) out.set(id, mirror);
    }
    return out;
  }

  // `under` restricts to a containment subtree. A CELL cannot be tested on its
  // own path: labels.json's `p` is its DEEPEST supercell only, but SCHEMA v3
  // says `supercells` may hold >1 entry and such a cell "renders inside each" —
  // so testing `p` alone drops every cell that spans two folders from the
  // subtree of all but one of them (31 cells; e.g. Cauchy-Schwarz is under
  // Analysis/InnerProductSpace AND LinearAlgebra/SesquilinearForm, but `p` names
  // only the first, so under=path:Mathlib/LinearAlgebra never returned it while
  // that folder's own card listed it and its `fa` mask advertised the match).
  // So take the UNION of both containment signals: `p`, and membership of the
  // `cells` list of any supercell in the subtree — the same field
  // /api/brain/cell serves, so the two surfaces agree. Either signal alone
  // is sufficient evidence of containment, so a union cannot over-match, and it
  // keeps the enumeration whole if either index drifts (today they agree
  // exactly: 7,398 cells carry `p`, the same 7,398 are listed).
  async filterCells(mask: number, under: string, from: number, limit: number): Promise<FilterPage<BrainLabelRow>> {
    const labels = await cellLabels(this.c, this.release);
    const inPrefix = (p: string) => p === under || p.startsWith(under + "/");
    let underSet: Set<string> | null = null;
    if (under) {
      const file = await supercellsFile(this.c, this.release);
      underSet = new Set<string>();
      for (const [path, e] of Object.entries(file?.supercells ?? {})) {
        if (!inPrefix(path)) continue;
        for (const cid of e.cells ?? []) underSet.add(cid);
      }
    }
    const rows: BrainLabelRow[] = [];
    let next: number | null = null;
    for (let i = from; i < labels.length; i++) {
      const r = labels[i];
      if (((r.f ?? 0) & mask) !== mask) continue;
      if (under && !(inPrefix(r.p ?? "") || (underSet?.has(r.id) ?? false))) continue;
      if (rows.length >= limit) {
        next = i; // index of the first matching row NOT returned — stable
        break;
      }
      rows.push(r);
    }
    return { rows, next };
  }

  // A supercell matches `under` on its own path prefix.
  async filterSupercells(
    mask: number,
    under: string,
    from: number,
    limit: number,
  ): Promise<FilterPage<Record<string, unknown>>> {
    const file = await supercellsFile(this.c, this.release);
    const entries = Object.entries(file?.supercells ?? {});
    const inPrefix = (p: string) => p === under || p.startsWith(under + "/");
    const rows: Array<Record<string, unknown>> = [];
    let next: number | null = null;
    for (let i = from; i < entries.length; i++) {
      const [path, e] = entries[i];
      if (((e.fa ?? 0) & mask) !== mask) continue;
      if (under && !inPrefix(path)) continue;
      if (rows.length >= limit) {
        next = i; // index of the first matching row NOT returned — stable
        break;
      }
      rows.push({
        id: path,
        label: e.label ?? null,
        ...(e.fa !== undefined ? { fa: e.fa } : {}),
        ...(e.parent ? { parent: e.parent } : {}),
        ...(e.cells?.length ? { n_cells: e.cells.length } : {}),
      });
    }
    return { rows, next };
  }

  // The inversion can list a page twice for one node (build_shards.py appends
  // base and graduated xref edges without deduping), so dedupe before taking 40:
  // the route's union then sees every distinct page it saw with the whole list.
  async xrefPages(node: string): Promise<string[]> {
    const inversion = await getXrefPagesByNode(this.c, this.release);
    return [...new Set(inversion[node] || [])].slice(0, 40);
  }

  // Plain indexing on the parsed index, as the route always did.
  async xrefNodes(pages: string[]): Promise<Map<string, string[]>> {
    const index = await getXrefIndex(this.c, this.release);
    const out = new Map<string, string[]>();
    for (const p of pages) {
      const nodes = index[p];
      if (nodes) out.set(p, nodes);
    }
    return out;
  }
}
