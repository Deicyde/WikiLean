// GET /decl/:name — the durable per-declaration resolver, and the Wikidata
// "Mathlib declaration" property proposal's formatter URL
// (docs/wikidata_property_proposal.md: https://wikilean.jackmccarthy.org/decl/$1).
//
// The property stores the fully-qualified decl NAME (Option A — survives
// Mathlib's module refactors); this route resolves name → current module via
// the doc-gen4 decl-index shards (static assets, rebuilt with build-public)
// and 302-redirects to the official mathlib4_docs page. With
// `Accept: application/json` it instead returns JSON incl. the reverse
// citations: every WikiLean statement citing the decl, from the KV blob
// `declcites:v1` that the nightly refreshes (site/build_decl_citations.py →
// `wrangler kv key put` — no Worker deploy, same pattern as graph:data:v1).
import type { Context, Hono } from "hono";
import type { Env } from "./env.js";

export const MATHLIB_DOCS = "https://leanprover-community.github.io/mathlib4_docs/";
const CITES_KV_KEY = "declcites:v1";
// Decl names: Lean identifiers incl. dots, primes, unicode subscripts, «guillemets».
// Reject anything with whitespace/control chars or absurd length.
const NAME_RE = /^[^\s\p{C}/\\]{1,300}$/u;

interface Manifest {
  scheme: { min_len: number; max_len: number; pad: string };
  shards: Record<string, number>;
}

// ---- pure logic (unit-tested) ----------------------------------------------

// Shard-key normalization — must mirror scripts/build-decl-index.ts exactly:
// lowercase [a-z0-9], everything else "_", pad short names with "_".
export function declShardKey(name: string, len: number): string {
  let k = "";
  for (let i = 0; i < len; i++) {
    if (i < name.length) {
      const l = name[i].toLowerCase();
      k += /[a-z0-9]/.test(l) ? l : "_";
    } else {
      k += "_";
    }
  }
  return k;
}

// Longest manifest key that prefixes the padded normalized name (leaf keys are
// prefix-free, so at most one length matches). Mirrors editor.js declShardFor
// INCLUDING the upward padded retry: names shorter than every key under them
// pad upward (e.g. "Set" lives in a shard like "set_" when "set" split), so a
// descending-only probe silently misses them.
export function declShardFor(m: Manifest, name: string): string | null {
  const maxLen = m.scheme?.max_len || 2;
  for (let len = Math.min(maxLen, Math.max(name.length, 2)); len >= 2; len--) {
    const k = declShardKey(name, len);
    if (m.shards[k] !== undefined) return k;
  }
  for (let len = Math.max(name.length, 2) + 1; len <= maxLen; len++) {
    const k = declShardKey(name, len);
    if (m.shards[k] !== undefined) return k;
  }
  return null;
}

export function docsUrlFor(module: string, name: string): string {
  return `${MATHLIB_DOCS}${module.replace(/\./g, "/")}.html#${encodeURIComponent(name)}`;
}

// Binary search a shard's sorted [decl, module] pairs (code-unit order).
export function lookupInShard(pairs: Array<[string, string]>, name: string): string | null {
  let lo = 0, hi = pairs.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const d = pairs[mid][0];
    if (d === name) return pairs[mid][1];
    if (d < name) lo = mid + 1;
    else hi = mid - 1;
  }
  return null;
}

// ---- route ------------------------------------------------------------------

async function assetJson<T>(c: Context<{ Bindings: Env }>, path: string): Promise<T | null> {
  const res = await c.env.ASSETS.fetch(new Request(new URL(path, c.req.url)));
  if (!res.ok) return null;
  return (await res.json()) as T;
}

async function resolveModule(c: Context<{ Bindings: Env }>, name: string): Promise<string | null> {
  const manifest = await assetJson<Manifest>(c, "/assets/decl-index/manifest.json");
  if (!manifest?.shards) return null;
  const key = declShardFor(manifest, name);
  if (!key) return null;
  const pairs = await assetJson<Array<[string, string]>>(c, `/assets/decl-index/${key}.json`);
  if (!pairs) return null;
  return lookupInShard(pairs, name);
}

export interface DeclCitation {
  slug: string;
  id: string;
  label: string;
  status: string;
}

// ---- multi-library fabric (Phase 1.4 of the math-DB unification) ------------
// Beyond Mathlib (the sharded asset index), other Lean libraries' own decls
// live in one KV blob refreshed nightly from each library's doc-gen4
// declaration-data (site/build_library_decls.py + catalog/data/libraries.json).
// The blob carries each library's docs_base, so library URL churn (Physlib has
// renamed twice) is a data update, never a Worker deploy.
export const LIBDECLS_KV_KEY = "libdecls:v1";

interface LibDeclsBlob {
  libraries: Record<string, { label: string; docs_base: string; aliases: string[] }>;
  decls: Record<string, Record<string, string>>; // library → name → module
}

export async function resolveInLibraries(
  blob: LibDeclsBlob | null,
  name: string,
): Promise<{ library: string; label: string; module: string; docs_url: string } | null> {
  if (!blob?.decls) return null;
  for (const [key, decls] of Object.entries(blob.decls)) {
    const module = decls[name];
    if (!module) continue;
    const lib = blob.libraries[key];
    if (!lib?.docs_base) continue;
    return {
      library: key,
      label: lib.label,
      module,
      docs_url: `${lib.docs_base}${module.replace(/\./g, "/")}.html#${encodeURIComponent(name)}`,
    };
  }
  return null;
}

async function citationsFor(c: Context<{ Bindings: Env }>, name: string): Promise<DeclCitation[]> {
  try {
    const blob = await c.env.RENDER_CACHE.get(CITES_KV_KEY, { cacheTtl: 300 });
    if (!blob) return [];
    const map = JSON.parse(blob) as Record<string, DeclCitation[]>;
    return Array.isArray(map[name]) ? map[name] : [];
  } catch {
    return []; // citations are best-effort garnish; the redirect must not break
  }
}

export function registerDeclRoutes(app: Hono<{ Bindings: Env }>): void {
  app.get("/decl/:name", async (c) => {
    const name = c.req.param("name");
    if (!NAME_RE.test(name)) return c.json({ ok: false, error: "bad declaration name" }, 400);
    const wantsJson = (c.req.header("Accept") || "").includes("application/json");
    const module = await resolveModule(c, name);
    if (!module) {
      // Not in Mathlib — try the other Lean libraries (CSLib, Physlib,
      // Formal Conjectures, …) via the nightly KV blob. Membership is exact
      // (existence oracle), never string-guessing.
      let libBlob: LibDeclsBlob | null = null;
      try {
        const raw = await c.env.RENDER_CACHE.get(LIBDECLS_KV_KEY, { cacheTtl: 300 });
        libBlob = raw ? (JSON.parse(raw) as LibDeclsBlob) : null;
      } catch {
        libBlob = null; // fabric is additive; a bad blob must not break /decl
      }
      const hit = await resolveInLibraries(libBlob, name);
      if (hit) {
        if (wantsJson) {
          return c.json(
            { ok: true, decl: name, library: hit.library, library_label: hit.label,
              module: hit.module, docs_url: hit.docs_url, cited_by: await citationsFor(c, name) },
            200,
            { "Cache-Control": "public, max-age=3600" },
          );
        }
        return c.redirect(hit.docs_url, 302);
      }
      if (wantsJson) return c.json({ ok: false, error: "unknown declaration", decl: name }, 404);
      // Human fallback: the docs search page, which handles renames gracefully.
      return c.redirect(`${MATHLIB_DOCS}search.html?q=${encodeURIComponent(name)}`, 302);
    }
    const docs = docsUrlFor(module, name);
    if (wantsJson) {
      return c.json(
        { ok: true, decl: name, library: "mathlib", module, docs_url: docs, cited_by: await citationsFor(c, name) },
        200,
        // Module moves only across docs builds; citations refresh nightly.
        { "Cache-Control": "public, max-age=3600" },
      );
    }
    return c.redirect(docs, 302);
  });
}
