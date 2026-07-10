// Wikibrain agent API (BRAIN v2 axis 5 — docs/BRAIN-API.md, docs/BRAIN-V2.md).
//
// Read-only query routes over the brain shards, one altitude above
// GET /api/brain/node: unit resolution, informal↔formal transfer, filtered
// neighborhoods, content snippets, facet-bitmask enumeration. All logic lives
// in exported `*For()` helpers returning {status, body} so the MCP endpoint
// (src/mcp.ts) calls the SAME code paths — the two surfaces cannot drift.
//
//   GET /api/brain/unit?key=                       any member key → unit card
//   GET /api/brain/transfer?q=&direction=&limit=   informal ↔ formal jump
//   GET /api/brain/neighborhood?id=&kinds=&dir=&limit=   edge projection
//   GET /api/brain/snippets?id=                    stored source snippets
//   GET /api/brain/filter?f=&type=&limit=&cursor=  facet enumeration
//   GET /brain/api                                 human-readable reference
//
// Everything here is shard/asset-backed and safe to cache for the nightly
// rebuild cadence (Cache-Control public, max-age=3600 — same as /api/brain/node).
// The v2 data artifacts (node.unit, labels `f`, ext nodes, aliases.json) ship
// from separate builders; every consumer below FEATURE-DETECTS and degrades:
// a missing unit is assembled from edges, a missing aliases.json falls back to
// shard in-edge resolution, a missing `f` reads as 0.
import type { Context, Hono } from "hono";
import type { Env } from "./env.js";
import {
  assetJson,
  resolveBrainEntry,
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
const QID_RE = /^Q[1-9][0-9]{0,11}$/;
const XREF_ID_RE = /^xref:([a-z0-9_]+):(.+)$/i;
const CONF_RANK: Record<string, number> = { high: 0, medium: 1, low: 2 };
const KEY_HINT =
  "accepted key forms: QID | decl:<Lib>:<Name> | bare FQ decl name | article slug | " +
  "xref:<db>:<id> | exact concept label — for fuzzy text use /api/brain/search?q=";

// ---- shard-entry shapes (brain/build_shards.py output, brain/SCHEMA.md) -----

export interface ShardEdge {
  id: string;
  kind: string;
  confidence?: string;
  evidence?: Record<string, unknown>;
  prov?: number;
}

export interface UnitDecl {
  name: string;
  module: string | null;
  match_kind: string | null;
  confidence: string | null;
}

export interface Unit {
  qid: string;
  label: string | null;
  description?: string;
  article?: { slug: string; annotations?: unknown };
  decls: UnitDecl[];
  containers: string[];
  xrefs: Record<string, Array<{ id: string; label?: string; url?: string }>>;
}

export interface BrainNodePayload {
  id: string;
  type: string;
  label?: string;
  slug?: string;
  // ext-node fields (v2)
  db?: string;
  url?: string;
  snippet?: string;
  snippet_license?: string;
  qid?: string;
  // v2 concept fields
  unit?: Unit;
  f?: number;
  display?: Record<string, unknown>;
  article_annotations?: unknown;
  [k: string]: unknown;
}

export interface ShardEntry {
  node: BrainNodePayload;
  breadcrumb?: Array<{ id: string; label?: string | null; type: string }>;
  edges?: {
    out?: ShardEdge[];
    in?: ShardEdge[];
    counts?: { out: number; in: number };
    truncated?: { out: boolean; in: boolean };
  };
  [k: string]: unknown;
}

// aliases.json (v2 builder): decl name / article slug → owning QID(s). May not
// be deployed yet — every caller treats null as "fall back to shard edges".
interface BrainAliases {
  decls?: Record<string, string | string[]>;
  slugs?: Record<string, string | string[]>;
}

// ---- small utilities ---------------------------------------------------------

async function entryFor(c: Ctx, id: string): Promise<ShardEntry | null> {
  if (!BRAIN_ID_RE.test(id)) return null;
  const r = await resolveBrainEntry(c, id);
  return r ? (r.entry as ShardEntry) : null;
}

function getLabels(c: Ctx): Promise<BrainLabelRow[] | null> {
  return assetJson<BrainLabelRow[]>(c, "/assets/brain/labels.json");
}

// own-property read — a JSON.parse'd map must never serve inherited names
// (__proto__/constructor/toString), same gotcha as /api/atlas/:key.
function own<T>(map: Record<string, T> | undefined, key: string): T | undefined {
  return map && Object.prototype.hasOwnProperty.call(map, key) ? map[key] : undefined;
}

function aliasQids(v: string | string[] | undefined): string[] {
  if (!v) return [];
  return (Array.isArray(v) ? v : [v]).filter((q) => typeof q === "string" && QID_RE.test(q));
}

function intOr(v: unknown, dflt: number): number {
  const n = typeof v === "number" ? v : typeof v === "string" && v.trim() !== "" ? Number(v) : NaN;
  return Number.isFinite(n) ? Math.floor(n) : dflt;
}

function clampLimit(v: unknown, dflt: number, max: number): number {
  return Math.min(Math.max(intOr(v, dflt), 1), max);
}

// Confidence, then exact-match preference, then name — the ranking used for
// unit.decls, transfer hits, and "owning concept" selection.
function rankDecl(a: UnitDecl, b: UnitDecl): number {
  const ca = CONF_RANK[a.confidence ?? ""] ?? 3;
  const cb = CONF_RANK[b.confidence ?? ""] ?? 3;
  if (ca !== cb) return ca - cb;
  const ea = a.match_kind === "exact" ? 0 : 1;
  const eb = b.match_kind === "exact" ? 0 : 1;
  if (ea !== eb) return ea - eb;
  return a.name < b.name ? -1 : a.name > b.name ? 1 : 0;
}

// QIDs formalizing a decl entry, best first (a decl's inbound formalizes edges
// are many-to-many by design — SCHEMA law 4 — so order matters for "owning").
function formalizingQids(entry: ShardEntry): string[] {
  const rows = (entry.edges?.in ?? []).filter((e) => e.kind === "formalizes" && QID_RE.test(e.id));
  rows.sort((a, b) => {
    const ca = CONF_RANK[a.confidence ?? ""] ?? 3;
    const cb = CONF_RANK[b.confidence ?? ""] ?? 3;
    if (ca !== cb) return ca - cb;
    const ea = a.evidence?.match_kind === "exact" ? 0 : 1;
    const eb = b.evidence?.match_kind === "exact" ? 0 : 1;
    if (ea !== eb) return ea - eb;
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
  });
  return [...new Set(rows.map((e) => e.id))];
}

function pickSuggestion(r: BrainLabelRow): Record<string, unknown> {
  return { id: r.id, type: r.type, label: r.label, ...(r.slug ? { slug: r.slug } : {}) };
}

async function suggestionsFor(c: Ctx, text: string, type: string): Promise<Record<string, unknown>[]> {
  const q = text.trim().toLowerCase();
  if (q.length < 2) return [];
  const labels = await getLabels(c);
  return labels ? searchLabels(labels, q, type, 5).map(pickSuggestion) : [];
}

// ---- the unit card (axis 2's atomic unit, served) ----------------------------

// v2 shards carry node.unit prebuilt; older shards get an on-the-fly assembly
// from the same evidence (formalizes/xref edges) so the API works either way.
export function unitFromEntry(entry: ShardEntry): Unit {
  const node = entry.node;
  if (node.unit) return node.unit;
  const decls: UnitDecl[] = [];
  const containers: string[] = [];
  const xrefs: Record<string, Array<{ id: string; label?: string; url?: string }>> = {};
  for (const e of entry.edges?.out ?? []) {
    const ev = e.evidence ?? {};
    if (e.kind === "formalizes" && e.id.startsWith("decl:")) {
      decls.push({
        name: e.id.split(":").slice(2).join(":"),
        module: typeof ev.module === "string" ? ev.module : null,
        match_kind: typeof ev.match_kind === "string" ? ev.match_kind : null,
        confidence: e.confidence ?? null,
      });
    } else if (e.kind === "formalizes" && e.id.startsWith("path:")) {
      containers.push(e.id);
    } else if (e.kind === "xref") {
      const m = XREF_ID_RE.exec(e.id);
      if (m) (xrefs[m[1].toLowerCase()] ??= []).push({ id: m[2] });
    }
  }
  decls.sort(rankDecl);
  containers.sort();
  return {
    qid: node.id,
    label: node.label ?? null,
    ...(node.slug
      ? {
          article: {
            slug: node.slug,
            ...(node.article_annotations !== undefined ? { annotations: node.article_annotations } : {}),
          },
        }
      : {}),
    decls,
    containers,
    xrefs,
  };
}

interface ResolvedKey {
  qid: string;
  resolved_from: "qid" | "decl" | "slug" | "xref" | "label";
  entry?: ShardEntry; // set when resolution already fetched the concept entry
}

// Resolve ANY member key of an atomic unit to its owning concept QID.
// Order (docs/BRAIN-V2.md): exact QID → decl (aliases.json, then the decl
// entry's inbound formalizes edges) → slug (aliases.json, then labels.json) →
// xref (its shard entry's own qid, then inbound xref edges) → exact label.
export async function resolveUnitKey(c: Ctx, key: string): Promise<ResolvedKey | null> {
  if (QID_RE.test(key)) {
    const entry = await entryFor(c, key);
    if (entry) return { qid: key, resolved_from: "qid", entry };
    // fall through: a QID-shaped string can legitimately be a slug/label
  }

  if (key.startsWith("xref:")) {
    const entry = await entryFor(c, key);
    if (!entry) return null;
    const ownQid = entry.node.qid;
    if (ownQid && QID_RE.test(ownQid)) return { qid: ownQid, resolved_from: "xref" };
    const anchors = (entry.edges?.in ?? [])
      .filter((e) => e.kind === "xref" && QID_RE.test(e.id))
      .map((e) => e.id)
      .sort();
    return anchors.length ? { qid: anchors[0], resolved_from: "xref" } : null;
  }

  const aliases = await assetJson<BrainAliases>(c, "/assets/brain/aliases.json");

  // decl: explicit 'decl:<Lib>:<Name>' or a bare fully-qualified decl name
  const isDeclId = key.startsWith("decl:");
  const bareName = isDeclId ? key.split(":").slice(2).join(":") : key;
  const declId = isDeclId ? key : `decl:Mathlib:${key}`;
  const viaAlias = aliasQids(own(aliases?.decls, key) ?? own(aliases?.decls, bareName) ?? own(aliases?.decls, declId));
  if (viaAlias.length) return { qid: viaAlias[0], resolved_from: "decl" };
  const declEntry = await entryFor(c, declId);
  if (declEntry?.node.type === "decl") {
    const qids = formalizingQids(declEntry);
    if (qids.length) return { qid: qids[0], resolved_from: "decl" };
  }
  if (isDeclId) return null; // an explicit decl id must not fall through to labels

  // slug
  const viaSlug = aliasQids(own(aliases?.slugs, key));
  if (viaSlug.length) return { qid: viaSlug[0], resolved_from: "slug" };
  const labels = await getLabels(c);
  if (labels) {
    const bySlug = labels.find((r) => r.slug === key && QID_RE.test(r.id));
    if (bySlug) return { qid: bySlug.id, resolved_from: "slug" };
    // exact label, case-insensitive, concepts only
    const kl = key.toLowerCase();
    const byLabel = labels.find(
      (r) => QID_RE.test(r.id) && (r.label || "").toLowerCase() === kl,
    );
    if (byLabel) return { qid: byLabel.id, resolved_from: "label" };
  }
  return null;
}

export async function unitFor(c: Ctx, keyRaw: string): Promise<ApiResult> {
  const key = (keyRaw || "").trim();
  if (!key || !BRAIN_ID_RE.test(key)) {
    return { status: 400, body: { ok: false, error: "missing or malformed ?key=", hint: KEY_HINT } };
  }
  const resolved = await resolveUnitKey(c, key);
  if (!resolved) {
    return { status: 404, body: { ok: false, error: "unresolvable key", key, hint: KEY_HINT } };
  }
  const entry = resolved.entry ?? (await entryFor(c, resolved.qid));
  if (!entry) {
    return {
      status: 404,
      body: { ok: false, error: "resolved concept is not in the brain shards", key, qid: resolved.qid },
    };
  }
  const edgesSummary: Record<string, number> = {};
  for (const dir of ["out", "in"] as const) {
    for (const e of entry.edges?.[dir] ?? []) edgesSummary[e.kind] = (edgesSummary[e.kind] ?? 0) + 1;
  }
  return {
    status: 200,
    body: {
      ok: true,
      resolved_from: resolved.resolved_from,
      key,
      qid: resolved.qid,
      unit: unitFromEntry(entry),
      display: entry.node.display ?? null,
      ...(entry.breadcrumb ? { breadcrumb: entry.breadcrumb } : {}),
      edges_summary: edgesSummary,
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

async function informalToFormal(c: Ctx, q: string, limit: number): Promise<ApiResult> {
  let resolved = BRAIN_ID_RE.test(q) ? await resolveUnitKey(c, q) : null;
  let resolvedFrom: string | null = resolved?.resolved_from ?? null;
  if (!resolved && q.length >= 2) {
    // free text: best label-search concept hit
    const labels = await getLabels(c);
    const hits = labels ? searchLabels(labels, q.toLowerCase(), "concept", 5) : [];
    const first = hits.find((h) => QID_RE.test(h.id));
    if (first) {
      resolved = { qid: first.id, resolved_from: "label" };
      resolvedFrom = "search";
    }
  }
  if (!resolved) {
    return {
      status: 404,
      body: {
        ok: false,
        error: "no concept matched q",
        q,
        suggestions: await suggestionsFor(c, q, ""),
        hint: "try /api/brain/search?q= for fuzzy lookup",
      },
    };
  }
  const qid = resolved.qid;
  const entry = resolved.entry ?? (await entryFor(c, qid));
  if (!entry) {
    return { status: 404, body: { ok: false, error: "concept not in the brain shards", qid } };
  }
  const unit = unitFromEntry(entry);
  const qidLabel = entry.node.label ?? null;
  const ranked = [...unit.decls].sort(rankDecl);
  const hits = ranked.slice(0, limit).map((d) => ({
    decl: d.name,
    module: d.module,
    match_kind: d.match_kind,
    confidence: d.confidence,
    docs_url: d.module ? docsUrlFor(d.module, d.name) : `${SITE_ORIGIN}/decl/${encodeURIComponent(d.name)}`,
    via_qid: qid,
    qid_label: qidLabel,
  }));
  const body: Record<string, unknown> = {
    ok: true,
    direction: "informal_to_formal",
    q,
    resolved_from: resolvedFrom,
    qid,
    qid_label: qidLabel,
    hits,
  };
  if (!hits.length) {
    body.note = "no formalizing decls recorded for this concept";
    if (unit.containers.length) body.containers = unit.containers; // field-level home, if any
    body.suggestions = await suggestionsFor(c, q, "concept");
  }
  return { status: 200, body };
}

async function formalToInformal(c: Ctx, q: string, limit: number): Promise<ApiResult> {
  const name = q.startsWith("decl:") ? q.split(":").slice(2).join(":") : q;
  const declId = q.startsWith("decl:") ? q : `decl:Mathlib:${q}`;
  const entry = await entryFor(c, declId);
  const qids = entry?.node.type === "decl" ? formalizingQids(entry) : [];
  const hits: Record<string, unknown>[] = [];
  for (const qid of qids.slice(0, limit)) {
    const ce = await entryFor(c, qid);
    if (!ce) {
      hits.push({ qid, label: null, slug: null, article_url: null, description: null, snippet_sources: [] });
      continue;
    }
    const u = unitFromEntry(ce);
    const slug = ce.node.slug ?? null;
    hits.push({
      qid,
      label: ce.node.label ?? null,
      slug,
      article_url: slug ? `${SITE_ORIGIN}/${encodeURIComponent(slug)}` : null,
      description: u.description ?? null,
      snippet_sources: Object.keys(u.xrefs).sort(),
    });
  }
  const body: Record<string, unknown> = {
    ok: true,
    direction: "formal_to_informal",
    q,
    decl: name,
    hits,
  };
  if (!hits.length) {
    body.note = entry
      ? "decl is a brain node but no concept formalizes-edge points at it"
      : "decl is not a brain node — it may still exist in Mathlib (check the decl_exists tool or /decl/<name>)";
    body.suggestions = await suggestionsFor(c, name.split(".").pop() ?? name, "concept");
  }
  return { status: 200, body };
}

// ---- neighborhood: filtered projection of a shard entry's edges --------------

export async function neighborhoodFor(
  c: Ctx,
  id: string,
  kindsCsv?: string,
  dirRaw?: string,
  limitRaw?: unknown,
): Promise<ApiResult> {
  if (!BRAIN_ID_RE.test(id || "")) return { status: 400, body: { ok: false, error: "bad node id" } };
  const dir = dirRaw || "both";
  if (dir !== "out" && dir !== "in" && dir !== "both") {
    return { status: 400, body: { ok: false, error: "dir must be out | in | both" } };
  }
  const limit = clampLimit(limitRaw, 50, 200);
  const kinds = kindsCsv
    ? new Set(kindsCsv.split(",").map((s) => s.trim()).filter(Boolean))
    : null;
  const entry = await entryFor(c, id);
  if (!entry) return { status: 404, body: { ok: false, error: "unknown node id", id } };
  const dirs: Array<"out" | "in"> = dir === "both" ? ["out", "in"] : [dir];
  const rows: Record<string, unknown>[] = [];
  const matched = { out: 0, in: 0 };
  for (const d of dirs) {
    for (const e of entry.edges?.[d] ?? []) {
      if (kinds && !kinds.has(e.kind)) continue;
      matched[d] += 1;
      if (rows.length < limit) rows.push({ direction: d, ...e });
    }
  }
  const shardTruncated = dirs.some((d) => entry.edges?.truncated?.[d]);
  return {
    status: 200,
    body: {
      ok: true,
      id,
      dir,
      ...(kinds ? { kinds: [...kinds] } : {}),
      edges: rows,
      returned: rows.length,
      matched, // matches within the (capped) shard lists, per direction
      counts: entry.edges?.counts ?? { out: 0, in: 0 }, // total edges on the node
      truncated: rows.length < matched.out + matched.in || shardTruncated,
    },
  };
}

// ---- snippets: every stored content snippet for a unit ------------------------

const MAX_SNIPPET_FETCHES = 16;

function extRow(id: string, node: BrainNodePayload): Record<string, unknown> {
  return {
    source_db: node.db ?? "",
    id,
    label: node.label ?? null,
    ...(node.snippet ? { snippet: node.snippet } : {}),
    ...(node.snippet_license ? { license: node.snippet_license } : {}),
    ...(node.url ? { url: node.url } : {}),
  };
}

export async function snippetsFor(c: Ctx, id: string): Promise<ApiResult> {
  if (!BRAIN_ID_RE.test(id || "")) return { status: 400, body: { ok: false, error: "bad node id" } };
  const entry = await entryFor(c, id);
  if (!entry) return { status: 404, body: { ok: false, error: "unknown node id", id } };
  const node = entry.node;
  if (node.type === "ext") {
    return { status: 200, body: { ok: true, id, rows: [extRow(node.id, node)] } };
  }
  const unit = unitFromEntry(entry);
  const rows: Record<string, unknown>[] = [];
  if (QID_RE.test(node.id)) {
    rows.push({
      source_db: "wikidata",
      id: node.id,
      label: node.label ?? null,
      ...(unit.description ? { snippet: unit.description, license: "CC0 (Wikidata)" } : {}),
      url: `https://www.wikidata.org/wiki/${node.id}`,
    });
  }
  if (node.slug) {
    // pointer to the annotated WikiLean article (annotations live in D1, not here)
    rows.push({
      source_db: "wikilean",
      id: node.slug,
      label: node.label ?? null,
      url: `${SITE_ORIGIN}/${encodeURIComponent(node.slug)}`,
    });
  }
  const xrefTargets = (entry.edges?.out ?? [])
    .filter((e) => e.kind === "xref" && e.id.startsWith("xref:"))
    .slice(0, MAX_SNIPPET_FETCHES);
  for (const e of xrefTargets) {
    const xe = await entryFor(c, e.id);
    if (xe && xe.node.type === "ext") {
      rows.push(extRow(e.id, xe.node));
    } else {
      // xref target without a minted ext node (pre-v2 data / beyond the cap):
      // still surface the pointer so the agent knows the identity exists
      const m = XREF_ID_RE.exec(e.id);
      rows.push({ source_db: m ? m[1].toLowerCase() : "", id: e.id, label: m ? m[2] : e.id });
    }
  }
  return { status: 200, body: { ok: true, id, rows } };
}

// ---- filter: facet-bitmask enumeration over labels.json -----------------------

export async function filterFor(
  c: Ctx,
  fRaw: unknown,
  type?: string,
  limitRaw?: unknown,
  cursorRaw?: unknown,
): Promise<ApiResult> {
  const mask = intOr(fRaw, -1);
  if (mask < 0 || mask > 0x7fffffff || (typeof fRaw === "string" && fRaw.trim() === "")) {
    return {
      status: 400,
      body: { ok: false, error: "f must be a non-negative integer bitmask (see brain/SCHEMA.md facet bits)" },
    };
  }
  const limit = clampLimit(limitRaw, 100, 500);
  const cursor = intOr(cursorRaw, 0);
  if (cursor < 0) return { status: 400, body: { ok: false, error: "bad cursor" } };
  const labels = await getLabels(c);
  if (!labels) return { status: 503, body: { ok: false, error: "brain data unavailable" } };
  const hits: BrainLabelRow[] = [];
  let nextCursor: number | null = null;
  for (let i = cursor; i < labels.length; i++) {
    const r = labels[i];
    if (type && r.type !== type) continue;
    if (((r.f ?? 0) & mask) !== mask) continue;
    if (hits.length >= limit) {
      nextCursor = i; // index of the first matching row NOT returned — stable
      break;
    }
    hits.push(r);
  }
  return {
    status: 200,
    body: {
      ok: true,
      f: mask,
      ...(type ? { type } : {}),
      hits,
      returned: hits.length,
      cursor,
      next_cursor: nextCursor,
    },
  };
}

// ---- search + node (MCP twins of the existing REST routes in brain.ts) --------

export async function searchFor(c: Ctx, qRaw: string, type?: string, limitRaw?: unknown): Promise<ApiResult> {
  const q = (qRaw || "").trim().toLowerCase();
  if (q.length < 2) return { status: 400, body: { ok: false, error: "query too short (min 2 chars)" } };
  const limit = clampLimit(limitRaw, 25, 100);
  const labels = await getLabels(c);
  if (!labels) return { status: 503, body: { ok: false, error: "brain data unavailable" } };
  return { status: 200, body: { ok: true, q, hits: searchLabels(labels, q, type || "", limit) } };
}

export async function nodeFor(c: Ctx, id: string): Promise<ApiResult> {
  if (!BRAIN_ID_RE.test(id || "")) return { status: 400, body: { ok: false, error: "bad node id" } };
  const resolved = await resolveBrainEntry(c, id);
  if (!resolved) return { status: 404, body: { ok: false, error: "unknown node id", id } };
  return {
    status: 200,
    body: { ok: true, id, ...(resolved.entry as Record<string, unknown>), prov_table: resolved.prov },
  };
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
  const manifest = await assetJson<DeclManifest>(c, "/assets/decl-index/manifest.json");
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

export function registerBrainApiRoutes(app: Hono<{ Bindings: Env }>): void {
  app.get("/api/brain/unit", async (c) => send(c, await unitFor(c, c.req.query("key") ?? "")));

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
        c.req.query("dir"),
        c.req.query("limit"),
      ),
    ),
  );

  app.get("/api/brain/snippets", async (c) => send(c, await snippetsFor(c, c.req.query("id") ?? "")));

  app.get("/api/brain/filter", async (c) =>
    send(
      c,
      await filterFor(c, c.req.query("f"), c.req.query("type"), c.req.query("limit"), c.req.query("cursor")),
    ),
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
    <a href="/articles">Articles</a>
    <a href="/">Home</a>
  </nav>
</header>
<main>
<h1>Wikibrain API <span class="pill">v2</span></h1>
<p class="muted">Read-only, unauthenticated, cached (<code>Cache-Control: public, max-age=3600</code> —
data rebuilds nightly). Base URL <code>https://wikilean.jackmccarthy.org</code>.
Full reference with response schemas: <a href="https://github.com/Deicyde/WikiLean/blob/main/docs/BRAIN-API.md">docs/BRAIN-API.md</a>.</p>

<h2>Connect over MCP (recommended for agents)</h2>
<pre><code>claude mcp add --transport http wikibrain https://wikilean.jackmccarthy.org/mcp</code></pre>
<p>A dependency-free streamable-HTTP MCP server (JSON-RPC 2.0, stateless, single-response
mode) exposing eight tools: <code>brain_search</code>, <code>brain_node</code>,
<code>brain_unit</code>, <code>brain_transfer</code>, <code>brain_neighborhood</code>,
<code>brain_snippets</code>, <code>brain_filter</code>, <code>decl_exists</code>.
Rate limit: 120 requests/min per IP.</p>

<h2>Node id grammar</h2>
<table>
<tr><th>form</th><th>type</th><th>example</th></tr>
<tr><td><code>Q&lt;digits&gt;</code></td><td>concept (Wikidata QID)</td><td><code>Q181296</code></td></tr>
<tr><td><code>path:&lt;Lib&gt;[/&lt;Dir&gt;…]</code></td><td>container (Mathlib folder)</td><td><code>path:Mathlib/CategoryTheory</code></td></tr>
<tr><td><code>decl:&lt;Lib&gt;:&lt;FQ name&gt;</code></td><td>Lean declaration</td><td><code>decl:Mathlib:CommGroup</code></td></tr>
<tr><td><code>lit:&lt;arxiv&gt;#&lt;ref&gt;</code></td><td>literature statement</td><td><code>lit:1707.04448#thm1.2</code></td></tr>
<tr><td><code>xref:&lt;db&gt;:&lt;id&gt;</code></td><td>external DB page</td><td><code>xref:lmfdb_knowl:group.abelian</code></td></tr>
</table>

<h2>REST endpoints</h2>

<h3>GET /api/brain/unit?key=</h3>
<p>Resolve <em>any</em> member key — QID, <code>decl:Lib:Name</code>, bare decl name,
article slug, <code>xref:db:id</code>, or exact concept label — to the owning concept's
atomic unit card (article ∘ QID ∘ decls ∘ containers ∘ cross-refs).</p>
<pre><code>curl 'https://wikilean.jackmccarthy.org/api/brain/unit?key=CommGroup'</code></pre>

<h3>GET /api/brain/transfer?q=&amp;direction=&amp;limit=</h3>
<p>The informal ↔ formal jump. <code>direction=informal_to_formal</code>: concept text /
QID / slug → ranked Mathlib decls with modules, docs URLs, <code>match_kind</code> and
confidence. <code>direction=formal_to_informal</code>: a decl name → concepts, article
URLs and snippet sources. Empty results include near-miss suggestions.</p>
<pre><code>curl 'https://wikilean.jackmccarthy.org/api/brain/transfer?q=abelian%20group&amp;direction=informal_to_formal'
curl 'https://wikilean.jackmccarthy.org/api/brain/transfer?q=CommGroup&amp;direction=formal_to_informal'</code></pre>

<h3>GET /api/brain/neighborhood?id=&amp;kinds=&amp;dir=&amp;limit=</h3>
<p>Filtered projection of a node's typed edges. <code>kinds</code> is a CSV of
<code>formalizes,mentions,depends,matches,xref,relates,links,cites</code>;
<code>dir</code> ∈ <code>out|in|both</code>; <code>limit</code> ≤ 200.</p>
<pre><code>curl 'https://wikilean.jackmccarthy.org/api/brain/neighborhood?id=Q181296&amp;kinds=xref&amp;dir=out'</code></pre>

<h3>GET /api/brain/snippets?id=</h3>
<p>Every stored content snippet for a unit — Wikidata description, WikiLean article
pointer, and each cross-referenced external page's stored snippet — with a per-row
license. No-content sources (MathWorld, DLMF, EoM, Kerodon) return deep links only.</p>
<pre><code>curl 'https://wikilean.jackmccarthy.org/api/brain/snippets?id=Q181296'</code></pre>

<h3>GET /api/brain/filter?f=&amp;type=&amp;limit=&amp;cursor=</h3>
<p>Enumerate nodes whose facet bitmask contains <code>f</code> (i.e. <code>(node.f &amp; f) == f</code>).
Bits (brain/SCHEMA.md): 0 gold <code>@[wikidata]</code> · 1 <code>@[stacks]</code> ·
2 <code>@[kerodon]</code> · 3 any xref · 4 formalized · 5 partial · 6 has article ·
7 has literature · 8 is ext · 9 lmfdb · 10 nlab · 11 mathworld · 12 proofwiki ·
13 stacks-tag · 14 oeis · 15 has snippet. Paginate with the returned
<code>next_cursor</code>.</p>
<pre><code>curl 'https://wikilean.jackmccarthy.org/api/brain/filter?f=1&amp;limit=50'</code></pre>

<h3>Existing routes</h3>
<p><code>GET /api/brain/node?id=</code> (full shard entry) ·
<code>GET /api/brain/search?q=&amp;type=&amp;limit=</code> (label search) ·
<code>GET /api/brain/edges?id=</code> (live community overlay, uncached) ·
<code>GET /decl/&lt;name&gt;</code> (decl → docs redirect; JSON with <code>Accept: application/json</code>).</p>

<h2>Provenance &amp; licensing</h2>
<p>Brain node/edge data is CC0. Every edge carries provenance
(<code>prov_table</code> on <code>/api/brain/node</code>). Snippets are stored only where
the source license permits and each row carries its license
(nLab attribution · Stacks GFDL · LMFDB/OEIS CC-BY-SA-4.0 · ProofWiki CC-BY-SA-3.0 ·
PlanetMath CC-BY-SA); other sources deep-link out.</p>
</main>
</body>
</html>`;
