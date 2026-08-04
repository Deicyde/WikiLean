// Shared BRAIN asset plumbing + the community-edit node-existence oracle.
//
// **The v2 (particle) layer is retired.** BRAIN v3 made the CELL the node: the
// /brain page reads /assets/brain/cells/* and the agent surface is
// /api/brain/cell (src/brain-api.ts). The v2 per-node shards + manifest are no
// longer built or shipped, and the old GET /api/brain/node route answers 410
// Gone below (same pattern as the retired graph/atlas endpoints in index.ts).
//
// What lives here now: `brainNodeExists` — the node-existence oracle the
// community-edit write path validates edge endpoints against
// (src/brain-edits.ts) — as a thin wrapper over the STRICT v3 resolver
// (brain-api.ts atomIdForOrgan: aliases.json `organs` ∪ supercells.json, no
// label/slug/bare-decl fuzz), plus the model-agnostic asset helpers shared
// with the v3 API (assetJson, memoAssetJson, searchLabels, BRAIN_ID_RE).
//
// Node ids stay in the v2 grammar per brain/SCHEMA.md: Q181296 |
// path:Mathlib/CategoryTheory | decl:Mathlib:CommGroup | lit:<arxiv>#<ref>.
// Ids carry ':'/'/' so they ride in a query param, not a path segment. All of
// them are ORGAN ids in v3, which is what keeps the oracle one alias lookup.
import type { Context, Hono } from "hono";
import type { Env } from "./env.js";
import { atomIdForOrgan } from "./brain-api.js";

// Interior spaces are legal (lit anchors like "lit:2110.15741#Theorem 2");
// only control chars and blank/overlong ids are rejected.
export const BRAIN_ID_RE = /^(?!\s*$)[^\p{C}]{1,400}$/u;

// Shared by decl.ts-style asset lookups in brain-api.ts (the cell agent API).
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
// `aka` = every organ's label, `p` = the atom's deepest supercell. The v2-only
// fields (`type`/`slug`/`status`) linger for callers that stored old rows;
// every field is optional so one row type + one search serve all of them.
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

// True iff `id` is a real brain node (used to validate edge endpoints). A thin
// wrapper over the STRICT v3 resolver: an exact aliases.json `organs` key, or
// an atom id (cell:… via the cell shards, path:… via supercells.json). Never
// resolves labels, aka, bare decl names, or slug guesses — an edge endpoint
// must be a real id, not prose that happens to match one.
export async function brainNodeExists(c: Context<{ Bindings: Env }>, id: string): Promise<boolean> {
  if (!BRAIN_ID_RE.test(id)) return false;
  return (await atomIdForOrgan(c, id)) !== null;
}

export function registerBrainRoutes(app: Hono<{ Bindings: Env }>): void {
  // Retired 2026-08-04 with the v2 per-node shards (docs/BRAIN-V3.md phase 5):
  // same 410 pattern as /graph_data.json etc. in index.ts. Every id this route
  // ever served is an ORGAN id, so /api/brain/cell?key= resolves it (except the
  // two populations v3 dropped on purpose, which 404 there with a `reason`).
  app.get("/api/brain/node", (c) =>
    c.json(
      {
        ok: false, error: "gone",
        note: "this endpoint is retired — the v2 per-node shards were replaced by the cell layer; every v2 node id resolves as an organ id on the cell API",
        see: { api: "/api/brain/cell?key=", reference: "/brain/api", mcp: "/mcp" },
      },
      410,
      { "Cache-Control": "public, max-age=86400" },
    ),
  );
  // GET /api/brain/search lives in brain-api.ts (v3): it searches the CELL
  // label index, where a hit's `aka` carries every organ label. Registering it
  // there keeps ONE search implementation — this module is registered first, so
  // a route defined here would silently shadow it.
}
