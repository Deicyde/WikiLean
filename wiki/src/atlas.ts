// Bubble-atlas routes (Phase A of the multilayer map).
//
// /atlas_data.json — the full hierarchy blob, KV-first (atlas:data:v1, pushed
// nightly by site/ops after site/build_atlas.py) with the last-deployed static
// asset as fallback — same pattern as /graph_data.json.
//
// /api/atlas + /api/atlas/:key — the AGENT surface: bounded, semantically
// coherent slices so an agent can traverse coarse-to-fine without ever loading
// the whole graph (progressive disclosure; aggregated inter-bubble edges act
// as routing tables). Every response is a bounded subset of the KV blob.
import type { Context, Hono } from "hono";
import type { Env } from "./env.js";

export const ATLAS_KV_KEY = "atlas:data:v1";

interface AtlasBlob {
  continents: Array<{ key: string; label: string; color: string; subfields: string[]; n_concepts: number }>;
  subfields: Record<string, { key: string; label: string; continent: string; qids: string[] }>;
  nodes: Record<string, Record<string, unknown>>;
  supernodes: Array<{ decl: string; subfield: string; members: string[] }>;
  edges: {
    subfield_pairs: Array<{ a: string; b: string; count: number; [k: string]: unknown }>;
    continent_pairs: Array<{ a: string; b: string; count: number }>;
  };
}

async function loadAtlas(c: Context<{ Bindings: Env }>): Promise<AtlasBlob | null> {
  try {
    const raw = await c.env.RENDER_CACHE.get(ATLAS_KV_KEY, { cacheTtl: 300 });
    if (raw) return JSON.parse(raw) as AtlasBlob;
  } catch {
    /* fall through to the static asset */
  }
  try {
    const res = await c.env.ASSETS.fetch(new Request(new URL("/atlas_data.json", c.req.url)));
    if (res.ok) return (await res.json()) as AtlasBlob;
  } catch {
    /* absent everywhere */
  }
  return null;
}

const JSON_HEADERS = { "Cache-Control": "public, max-age=600" };
const MAX_CONCEPTS = 500; // bounded responses — the whole point of the API

export function registerAtlasRoutes(app: Hono<{ Bindings: Env }>): void {
  app.get("/atlas_data.json", async (c) => {
    const raw = await c.env.RENDER_CACHE.get(ATLAS_KV_KEY, { cacheTtl: 300 });
    if (raw) {
      return c.body(raw, 200, { "Content-Type": "application/json; charset=utf-8", ...JSON_HEADERS });
    }
    return c.env.ASSETS.fetch(new Request(new URL("/atlas_data.json", c.req.url)));
  });

  // Level 0: the continents + their interconnections. Bounded by construction.
  app.get("/api/atlas", async (c) => {
    const atlas = await loadAtlas(c);
    if (!atlas) return c.json({ ok: false, error: "atlas unavailable" }, 503);
    return c.json(
      {
        ok: true,
        continents: atlas.continents.map((k) => ({
          key: k.key, label: k.label, n_concepts: k.n_concepts,
          subfields: k.subfields.map((s) => ({
            key: s, label: atlas.subfields[s]?.label ?? s,
            n_concepts: atlas.subfields[s]?.qids.length ?? 0,
          })),
        })),
        continent_edges: atlas.edges.continent_pairs,
      },
      200, JSON_HEADERS,
    );
  });

  // Level 1/2: expand one bubble (continent or subfield key). A subfield
  // returns its concepts (capped), super-nodes, and its aggregated edges —
  // one hop of context, never the whole graph.
  app.get("/api/atlas/:key", async (c) => {
    const key = c.req.param("key");
    const atlas = await loadAtlas(c);
    if (!atlas) return c.json({ ok: false, error: "atlas unavailable" }, 503);
    const cont = atlas.continents.find((k) => k.key === key);
    if (cont) {
      return c.json(
        {
          ok: true, kind: "continent", key: cont.key, label: cont.label,
          subfields: cont.subfields.map((s) => ({
            key: s, label: atlas.subfields[s]?.label ?? s,
            n_concepts: atlas.subfields[s]?.qids.length ?? 0,
          })),
          edges: atlas.edges.continent_pairs.filter((p) => p.a === cont.key || p.b === cont.key),
        },
        200, JSON_HEADERS,
      );
    }
    const sf = atlas.subfields[key];
    if (sf) {
      const concepts = sf.qids.slice(0, MAX_CONCEPTS).map((q) => ({ qid: q, ...atlas.nodes[q] }));
      return c.json(
        {
          ok: true, kind: "subfield", key: sf.key, label: sf.label, continent: sf.continent,
          n_concepts: sf.qids.length,
          truncated: sf.qids.length > MAX_CONCEPTS,
          concepts,
          supernodes: atlas.supernodes.filter((sn) => sn.subfield === key),
          edges: atlas.edges.subfield_pairs.filter((p) => p.a === key || p.b === key).slice(0, 20),
        },
        200, JSON_HEADERS,
      );
    }
    return c.json({ ok: false, error: "unknown bubble", key }, 404);
  });
}
