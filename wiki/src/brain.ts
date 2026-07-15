// Shared BRAIN asset plumbing + the LEGACY v2 particle route.
//
//   GET /api/brain/node?id=<node id>     v2 shard entry: {node, edges,
//                                        breadcrumb, children, rollup} + prov
//
// **This is the v2 (particle) layer.** BRAIN v3 made the CELL the node, and the
// agent surface moved to /api/brain/cell (src/brain-api.ts, reading
// /assets/brain/cells/). This module stays for two live consumers until phase 5
// retires the v2 assets (docs/BRAIN-V3.md): `brainNodeExists` — the
// node-existence oracle the community-edit write path validates edge endpoints
// against (src/brain-edits.ts) — and the v2 /brain page. The exports below
// (assetJson, memoAssetJson, searchLabels, BRAIN_ID_RE) are model-agnostic and
// shared with the v3 API.
//
// v2 node ids per brain/SCHEMA.md: Q181296 | path:Mathlib/CategoryTheory |
// decl:Mathlib:CommGroup | lit:<arxiv>#<ref>. Ids carry ':'/'/' so they ride
// in a query param, not a path segment. The shard scheme is identical to the
// decl-index (build_shards.py mirrors build-decl-index.ts), so the resolver
// reuses declShardFor.
import type { Context, Hono } from "hono";
import type { Env } from "./env.js";
import { declShardFor } from "./decl.js";

// Interior spaces are legal (lit anchors like "lit:2110.15741#Theorem 2");
// only control chars and blank/overlong ids are rejected.
export const BRAIN_ID_RE = /^(?!\s*$)[^\p{C}]{1,400}$/u;
const ID_RE = BRAIN_ID_RE;

interface BrainManifest {
  scheme: { min_len: number; max_len: number; pad: string };
  shards: Record<string, number>;
  prov: Array<Record<string, string>>;
  roots: Array<Record<string, unknown>>;
  _meta: Record<string, unknown>;
}

// Shared by decl.ts-style asset lookups in brain-api.ts (the v2 agent API).
export async function assetJson<T>(c: Context<{ Bindings: Env }>, path: string): Promise<T | null> {
  const res = await c.env.ASSETS.fetch(new Request(new URL(path, c.req.url)));
  if (!res.ok) return null;
  return (await res.json()) as T;
}

// Isolate-lifetime memo for large parsed assets (labels.json is ~4MB and was
// re-fetched+parsed up to 3x per request; the manifest ~50x across one
// transfer call). Static assets change only on deploy, and deploys recycle
// isolates, so isolate-scoped caching is exactly as fresh as the assets
// themselves. Failed (null) loads are NOT cached — they stay retryable.
const _assetMemo = new Map<string, Promise<unknown>>();
export function memoAssetJson<T>(
  c: Context<{ Bindings: Env }>, path: string,
): Promise<T | null> {
  const hit = _assetMemo.get(path);
  if (hit) return hit as Promise<T | null>;
  const p = assetJson<T>(c, path).then((v) => {
    if (v === null) _assetMemo.delete(path);
    return v;
  }, (e) => { _assetMemo.delete(path); throw e; });
  _assetMemo.set(path, p);
  return p;
}
// test-only (mirrors brain-edits' _resetBrainEditCaches)
export function _resetBrainAssetMemo(): void { _assetMemo.clear(); }

// One labels.json row. The v3 cell index (build_cell_shards.py, the namespace
// /assets/brain/cells/) ships `{id, label, f?, aka?, p?}` — one row per ATOM,
// `aka` = every organ's label, `p` = the atom's deepest supercell. The v2 node
// index (still served by /api/brain/node's shards) ships `type`/`slug`/`status`
// instead; every field is optional so one row type + one search serve both.
export interface BrainLabelRow {
  id: string;
  label: string;
  aka?: string[]; // v3: organ labels — "Vector space" must find the Module atom
  p?: string; // v3: deepest supercell (path:…)
  type?: string; // v2 only
  slug?: string;
  status?: string;
  n_decls?: number;
  f?: number;
}

// Pure label search shared by GET /api/brain/search and the MCP brain_search
// tool (src/mcp.ts via brain-api.ts). `q` must already be trimmed+lowercased.
// Prefix hits rank before substring hits; a bare QID query matches by id.
// `aka` is searched at the same rank as the label: an atom is named by its
// anchor, so the organ labels are the only handle a caller may hold ("Vector
// space" ranks as a prefix hit on the Module atom, not a fuzzy afterthought).
export function searchLabels(
  labels: BrainLabelRow[],
  q: string,
  type: string,
  limit: number,
): BrainLabelRow[] {
  const isQid = /^q[1-9][0-9]{0,11}$/.test(q);
  const starts: BrainLabelRow[] = [], contains: BrainLabelRow[] = [];
  for (const r of labels) {
    if (type && r.type !== type) continue;
    const names = [(r.label || "").toLowerCase(), ...(r.aka ?? []).map((a) => a.toLowerCase())];
    if (names.some((n) => n.startsWith(q)) || (isQid && r.id.toLowerCase() === q)) starts.push(r);
    else if (names.some((n) => n.includes(q))) contains.push(r);
    if (starts.length >= limit) break;
  }
  return [...starts, ...contains].slice(0, limit);
}

// Resolve a node id to its shard entry (+ the manifest prov table), or null.
// Shared by GET /api/brain/node and the brain-edit write path (the shard set is
// the node-existence oracle — an edge endpoint must resolve to a real node).
export async function resolveBrainEntry(
  c: Context<{ Bindings: Env }>,
  id: string,
): Promise<{ entry: object; prov: Array<Record<string, string>> } | null> {
  const manifest = await memoAssetJson<BrainManifest>(c, "/assets/brain/manifest.json");
  if (!manifest?.shards) return null;
  const key = declShardFor(
    { scheme: { min_len: manifest.scheme.min_len, max_len: manifest.scheme.max_len, pad: manifest.scheme.pad },
      shards: manifest.shards },
    id,
  );
  const shard = key
    ? await assetJson<Record<string, unknown>>(c, `/assets/brain/${key}.json`)
    : null;
  // hasOwnProperty guard: JSON.parse yields a plain object, so id="__proto__"
  // would otherwise resolve Object.prototype as a truthy "entry" (the same
  // gotcha atlas.ts documents)
  const entry = shard && Object.prototype.hasOwnProperty.call(shard, id)
    ? shard[id] : undefined;
  if (!entry) return null;
  return { entry: entry as object, prov: manifest.prov };
}

// True iff `id` is a real brain node (used to validate edge endpoints).
export async function brainNodeExists(c: Context<{ Bindings: Env }>, id: string): Promise<boolean> {
  if (!BRAIN_ID_RE.test(id)) return false;
  return (await resolveBrainEntry(c, id)) !== null;
}

export function registerBrainRoutes(app: Hono<{ Bindings: Env }>): void {
  app.get("/api/brain/node", async (c) => {
    const id = c.req.query("id") || "";
    if (!ID_RE.test(id)) return c.json({ ok: false, error: "bad node id" }, 400);
    const resolved = await resolveBrainEntry(c, id);
    if (!resolved) return c.json({ ok: false, error: "unknown node id", id }, 404);
    return c.json(
      { ok: true, id, ...resolved.entry, prov_table: resolved.prov },
      200,
      // shards change only on nightly data rebuilds
      { "Cache-Control": "public, max-age=3600" },
    );
  });
  // GET /api/brain/search lives in brain-api.ts (v3): it searches the CELL
  // label index, where a hit's `aka` carries every organ label. Registering it
  // there keeps ONE search implementation — this module is registered first, so
  // a route defined here would silently shadow it.
}
