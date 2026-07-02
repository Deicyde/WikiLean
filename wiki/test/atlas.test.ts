// Bubble-atlas agent API: bounded coarse-to-fine slices from the KV blob.
import { describe, it, expect } from "vitest";
import { setup, get } from "./helpers/harness.js";
import { ATLAS_KV_KEY } from "../src/atlas.js";

const BLOB = {
  continents: [
    { key: "algebra", label: "Algebra", color: "#0969da", subfields: ["linear-algebra"], n_concepts: 2 },
    { key: "analysis", label: "Analysis", color: "#1a7f37", subfields: ["measure-theory"], n_concepts: 1 },
  ],
  subfields: {
    "linear-algebra": { key: "linear-algebra", label: "Linear algebra", continent: "algebra", qids: ["Q18848", "Q125977"] },
    "measure-theory": { key: "measure-theory", label: "Measure theory", continent: "analysis", qids: ["Q1345"] },
  },
  nodes: {
    Q18848: { label: "Module (mathematics)", slug: "Module_mathematics", status: "formalized", subfield: "linear-algebra", assign_rule: "module" },
    Q125977: { label: "Vector space", slug: "Vector_space", status: "formalized", subfield: "linear-algebra", assign_rule: "module" },
    Q1345: { label: "Measure", slug: "Measure_mathematics", status: "formalized", subfield: "measure-theory", assign_rule: "module" },
  },
  supernodes: [{ decl: "Module", subfield: "linear-algebra", members: ["Q18848", "Q125977"] }],
  edges: {
    subfield_pairs: [{ a: "linear-algebra", b: "measure-theory", count: 4, mathlib: 4, wikidata: 0, examples: [] }],
    continent_pairs: [{ a: "algebra", b: "analysis", count: 4 }],
  },
};

function setupAtlas() {
  const h = setup();
  h.renderCache.store.set(ATLAS_KV_KEY, JSON.stringify(BLOB));
  return h;
}

describe("GET /api/atlas (level 0)", () => {
  it("returns continents with subfield summaries + continent edges", async () => {
    const h = setupAtlas();
    const res = await get(h.env, "/api/atlas");
    expect(res.status).toBe(200);
    const j = (await res.json()) as Record<string, any>;
    expect(j.ok).toBe(true);
    expect(j.continents).toHaveLength(2);
    expect(j.continents[0]).toMatchObject({ key: "algebra", n_concepts: 2 });
    expect(j.continents[0].subfields[0]).toMatchObject({ key: "linear-algebra", label: "Linear algebra", n_concepts: 2 });
    expect(j.continent_edges).toEqual([{ a: "algebra", b: "analysis", count: 4 }]);
  });
  it("503s when no blob exists anywhere", async () => {
    const h = setup();
    const res = await get(h.env, "/api/atlas");
    expect(res.status).toBe(503);
  });
});

describe("GET /api/atlas/:key (expand one bubble)", () => {
  it("expands a continent to its subfields + its edges", async () => {
    const h = setupAtlas();
    const j = (await (await get(h.env, "/api/atlas/algebra")).json()) as Record<string, any>;
    expect(j).toMatchObject({ ok: true, kind: "continent", key: "algebra" });
    expect(j.subfields).toHaveLength(1);
    expect(j.edges).toHaveLength(1);
  });
  it("expands a subfield to bounded concepts + supernodes + aggregated edges", async () => {
    const h = setupAtlas();
    const j = (await (await get(h.env, "/api/atlas/linear-algebra")).json()) as Record<string, any>;
    expect(j).toMatchObject({ ok: true, kind: "subfield", continent: "algebra", n_concepts: 2, truncated: false });
    expect(j.concepts.map((c: { qid: string }) => c.qid).sort()).toEqual(["Q125977", "Q18848"]);
    expect(j.supernodes).toEqual([{ decl: "Module", subfield: "linear-algebra", members: ["Q18848", "Q125977"] }]);
    expect(j.edges[0]).toMatchObject({ a: "linear-algebra", b: "measure-theory", count: 4 });
  });
  it("404s an unknown bubble", async () => {
    const h = setupAtlas();
    expect((await get(h.env, "/api/atlas/nope")).status).toBe(404);
  });
});

describe("GET /atlas_data.json", () => {
  it("serves the KV blob KV-first", async () => {
    const h = setupAtlas();
    const res = await get(h.env, "/atlas_data.json");
    expect(res.status).toBe(200);
    const j = (await res.json()) as Record<string, any>;
    expect(j.supernodes).toHaveLength(1);
  });
});
