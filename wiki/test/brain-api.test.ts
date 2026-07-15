// Wikibrain agent API (src/brain-api.ts), BRAIN v3 — the CELL model: resolving
// every organ id (and every v2 entry point) to its owning atom through
// aliases.json, the atom card with embedded organ payloads, transfer in both
// directions incl. the rule-5 field-concept → supercell answer, synapse
// projection with traces, snippets + the licence floor, facet-filter mask math
// + cursor paging, `aka` search, and the /brain/api reference page. All assets
// come from the shared fixture (helpers/brain-fixture.ts); no network.

import { beforeEach, describe, it, expect } from "vitest";
import { _resetBrainAssetMemo } from "../src/brain.js";
import { setup, get, put, blockNetwork, PIPELINE_TOKEN, type Harness } from "./helpers/harness.js";
import {
  installBrainFixture,
  ABELIAN_CELL,
  MODULE_CELL,
  EMPTY_CELL,
  DECL_CELL,
  LINALG_SUPER,
  ALGEBRA_SUPER,
  ABELIAN,
  MODULE_Q,
  VSPACE_Q,
  EMPTY_Q,
  FIELD_Q,
  COMM_DECL,
  MODULE_DECL,
  LMFDB_PAGE,
  NLAB_PAGE,
  MW_PAGE,
  LIT_STMT,
  UNKNOWN_XREF,
  type BrainFixtureOpts,
} from "./helpers/brain-fixture.js";

blockNetwork();

function harness(opts: BrainFixtureOpts = {}): Harness {
  const h = setup();
  installBrainFixture(h.env, opts);
  return h;
}

async function getJson(h: Harness, path: string): Promise<{ status: number; j: Record<string, unknown> }> {
  const res = await get(h.env, path);
  return { status: res.status, j: (await res.json()) as Record<string, unknown> };
}

const organs = (j: Record<string, unknown>) => j.organs as Array<Record<string, unknown>>;
const organ = (j: Record<string, unknown>, id: string) => organs(j).find((o) => o.id === id);
const q = encodeURIComponent;

// the isolate-lifetime asset memo must not leak fixtures across tests
beforeEach(() => _resetBrainAssetMemo());

describe("GET /api/brain/cell — every organ id resolves to its atom", () => {
  // The headline of the v3 cut: Module and Vector space are ONE atom, so every
  // handle on either of them — QID, decl id, article slug — answers identically.
  it.each([
    ["the anchor QID", MODULE_Q, "organ"],
    ["an absorbed concept QID (Vector space)", VSPACE_Q, "organ"],
    ["the decl id", MODULE_DECL, "organ"],
    ["a bare decl name", "Module", "decl"],
    ["an article slug", "Vector_space", "organ"],
    ["a page organ", "xref:nlab:module", "organ"],
    ["a statement organ", LIT_STMT, "organ"],
    ["the atom id itself", MODULE_CELL, "cell"],
    ["an exact label", "Module (mathematics)", "label"],
    ["an exact organ label (aka)", "Vector space", "label"],
  ])("resolves %s → the Module atom", async (_name, key, resolvedFrom) => {
    const h = harness();
    const { status, j } = await getJson(h, `/api/brain/cell?key=${q(key)}`);
    expect(status).toBe(200);
    expect(j).toMatchObject({ ok: true, id: MODULE_CELL, kind: "cell", resolved_from: resolvedFrom });
  });

  // Rule 5: a field-of-study concept's formal home is a FOLDER, and it is never
  // a cell — "Linear algebra" must not resolve to a stray atom of its own.
  it("resolves a rule-5 field concept to its SUPERCELL, not a cell", async () => {
    const h = harness();
    const { status, j } = await getJson(h, `/api/brain/cell?key=${FIELD_Q}`);
    expect(status).toBe(200);
    expect(j).toMatchObject({ ok: true, id: LINALG_SUPER, kind: "supercell", resolved_from: "organ" });
    // the field concept is an ORGAN of the folder
    expect(organ(j, FIELD_Q)).toMatchObject({ kind: "concept", bond: "field" });
    expect(j.supercell).toMatchObject({
      path: LINALG_SUPER, parent: "path:Mathlib", cells: [MODULE_CELL], fa: 5,
    });
    // `fa` is the SUBTREE aggregate, not the folder's own facets — it must not
    // masquerade as a cell's `f`
    expect(j.f).toBeUndefined();
    // breadcrumb is derived by walking `parent` (supercells.json is a tree)
    expect(j.breadcrumb).toEqual([{ id: "path:Mathlib", label: "Mathlib" }]);
  });

  it("returns the whole card in one request: organs with embedded payloads", async () => {
    const h = harness();
    const { j } = await getJson(h, `/api/brain/cell?key=${COMM_DECL}`);
    expect(j).toMatchObject({ id: ABELIAN_CELL, kind: "cell", label: "Abelian group", f: 5 });
    expect(j.organs_by_kind).toEqual({ concept: 1, decl: 1, page: 3, article: 1 });
    // the Lean code + docstring ride along — no second fetch
    expect(organ(j, COMM_DECL)).toMatchObject({
      bond: "exact",
      module: "Mathlib.Algebra.Group.Defs",
      docstring: "A commutative group is a group with commutative multiplication.",
    });
    // …as do the Wikidata description and the licensed DB snippet
    expect(organ(j, ABELIAN)).toMatchObject({ description: "group whose operation is commutative" });
    expect(organ(j, LMFDB_PAGE)).toMatchObject({
      snippet: "An abelian group is a group whose operation is commutative.",
      snippet_license: "CC-BY-SA-4.0 (LMFDB)",
    });
    expect(j.breadcrumb).toEqual([
      { id: "path:Mathlib", label: "Mathlib" },
      { id: ALGEBRA_SUPER, label: "Algebra" },
    ]);
    expect(j.cell).toMatchObject({ anchor: ABELIAN, supercells: ["path:Mathlib/Algebra/Group/Defs"] });
    // caches at the nightly cadence
    const res = await get(h.env, `/api/brain/cell?key=${COMM_DECL}`);
    expect(res.headers.get("Cache-Control")).toBe("public, max-age=3600");
  });

  it("summarizes synapses and previews the strongest partners", async () => {
    const h = harness();
    const { j } = await getJson(h, `/api/brain/cell?key=${MODULE_CELL}`);
    // kind:count summed across the atom's synapses
    expect(j.synapses_summary).toEqual({ depends: 16, relates: 2, mentions: 1, links: 5, invocation: 1, cites: 1 });
    const preview = j.synapses_preview as Array<{ id: string; w: number }>;
    expect(preview.map((s) => s.id)).toEqual([ABELIAN_CELL, LINALG_SUPER, EMPTY_CELL]); // heaviest first
    expect(preview[0]).not.toHaveProperty("traces"); // the card stays an identity answer
    // counts.syn is the TRUE total; `truncated.syn` is a COUNT of what the shard dropped
    expect(j.counts).toEqual({ syn: 6, organs: 8 });
    expect(j.truncated).toEqual({ syn: 3 });
  });

  it("NEVER serves a snippet that lost its license", async () => {
    const h = harness();
    const { j } = await getJson(h, `/api/brain/cell?key=${MODULE_CELL}`);
    const pm = organ(j, "xref:planetmath:VectorSpace")!;
    expect(pm.snippet).toBeUndefined(); // the fixture ships text with no license
    expect(JSON.stringify(j)).not.toContain("UNLICENSED TEXT");
    // the row itself survives as a deep link — we lose the text, not the identity
    expect(pm.url).toBe("https://planetmath.org/vectorspace");
  });

  it("/api/brain/unit is an alias — the v2 entry point still resolves", async () => {
    const h = harness();
    const { status, j } = await getJson(h, "/api/brain/unit?key=CommGroup");
    expect(status).toBe(200);
    expect(j).toMatchObject({ ok: true, id: ABELIAN_CELL, kind: "cell", resolved_from: "decl" });
  });

  it("404s an unresolvable key with a hint; 400s a missing one", async () => {
    const h = harness();
    const miss = await getJson(h, "/api/brain/cell?key=NoSuchThingAnywhere");
    expect(miss.status).toBe(404);
    expect(String(miss.j.hint)).toContain("/api/brain/search");
    // an xref page that is nobody's organ
    expect((await getJson(h, `/api/brain/cell?key=${q(UNKNOWN_XREF)}`)).status).toBe(404);
    // an explicit atom id that does not exist must not fall through to label search
    expect((await getJson(h, "/api/brain/cell?key=cell:Q999999999")).status).toBe(404);
    expect((await getJson(h, "/api/brain/cell?key=path:Mathlib/Nope")).status).toBe(404);
    expect((await get(h.env, "/api/brain/cell")).status).toBe(400);
  });

  it("without aliases.json only atom ids and labels resolve (it IS the compat layer)", async () => {
    const h = harness({ aliases: null });
    expect((await getJson(h, `/api/brain/cell?key=${MODULE_CELL}`)).status).toBe(200);
    expect((await getJson(h, `/api/brain/cell?key=${q("Vector space")}`)).j.id).toBe(MODULE_CELL);
    expect((await getJson(h, `/api/brain/cell?key=${VSPACE_Q}`)).status).toBe(404);
  });
});

describe("GET /api/brain/transfer — informal_to_formal", () => {
  it("a concept resolves to its atom and reads the decl organs off it", async () => {
    const h = harness();
    const { status, j } = await getJson(h, `/api/brain/transfer?q=${ABELIAN}&direction=informal_to_formal`);
    expect(status).toBe(200);
    expect(j).toMatchObject({ id: ABELIAN_CELL, kind: "cell", resolved_from: "organ" });
    expect(j.hits).toEqual([
      {
        decl: "CommGroup",
        module: "Mathlib.Algebra.Group.Defs",
        bond: "exact",
        decl_kind: "class",
        docs_url:
          "https://leanprover-community.github.io/mathlib4_docs/Mathlib/Algebra/Group/Defs.html#CommGroup",
        via_cell: ABELIAN_CELL,
        cell_label: "Abelian group",
      },
    ]);
  });

  // The merge function is the whole point: "vector space" has no VectorSpace to
  // find, and the atom answers with Module anyway.
  it("an absorbed concept answers with its atom's decl (Vector space → Module)", async () => {
    const h = harness();
    const { j } = await getJson(h, `/api/brain/transfer?q=${VSPACE_Q}&direction=informal_to_formal`);
    expect(j.id).toBe(MODULE_CELL);
    expect((j.hits as Array<{ decl: string }>)[0].decl).toBe("Module");
  });

  it("free text falls back to label/aka search (resolved_from=search)", async () => {
    const h = harness();
    const { j } = await getJson(h, "/api/brain/transfer?q=vector%20spa&direction=informal_to_formal");
    expect(j).toMatchObject({ ok: true, resolved_from: "search", id: MODULE_CELL });
    expect((j.hits as Array<{ decl: string }>)[0].decl).toBe("Module");
  });

  it("a field-of-study concept answers with its supercell, not an empty result", async () => {
    const h = harness();
    const { status, j } = await getJson(h, `/api/brain/transfer?q=${FIELD_Q}&direction=informal_to_formal`);
    expect(status).toBe(200);
    expect(j).toMatchObject({ id: LINALG_SUPER, kind: "supercell", container: LINALG_SUPER, cells_in_container: 1 });
    expect(j.hits).toEqual([]);
    expect(String(j.note)).toContain("Mathlib folder");
  });

  it("a concept with no decl organ returns empty hits + suggestions", async () => {
    const h = harness();
    const { status, j } = await getJson(
      h, "/api/brain/transfer?q=Parity%20conjecture&direction=informal_to_formal");
    expect(status).toBe(200);
    expect(j.id).toBe(EMPTY_CELL);
    expect(j.hits).toEqual([]);
    expect((j.suggestions as unknown[]).length).toBeGreaterThan(0);
  });

  it("404s (with suggestions) when nothing matches; 400s a bad direction/missing q", async () => {
    const h = harness();
    const { status, j } = await getJson(h, "/api/brain/transfer?q=zzzz-nothing&direction=informal_to_formal");
    expect(status).toBe(404);
    expect(j.suggestions).toEqual([]);
    expect((await get(h.env, "/api/brain/transfer?q=x&direction=sideways")).status).toBe(400);
    expect((await get(h.env, "/api/brain/transfer?direction=informal_to_formal")).status).toBe(400);
  });
});

describe("GET /api/brain/transfer — formal_to_informal", () => {
  it("a decl answers with EVERY concept organ of its atom (multi-to-multi)", async () => {
    const h = harness();
    const { j } = await getJson(h, "/api/brain/transfer?q=Module&direction=formal_to_informal");
    expect(j).toMatchObject({ decl: "Module", id: MODULE_CELL });
    const hits = j.hits as Array<Record<string, unknown>>;
    expect(hits.map((x) => x.qid)).toEqual([MODULE_Q, VSPACE_Q]); // both, in organ order
    expect(hits[0]).toMatchObject({
      label: "Module",
      bond: "exact",
      slug: "Module_mathematics",
      article_url: "https://wikilean.jackmccarthy.org/Module_mathematics",
      description: "algebraic structure over a ring",
    });
    // the absorbed concept keeps its own bond — the card says WHY it is here
    expect(hits[1]).toMatchObject({ qid: VSPACE_Q, bond: "generalization", slug: "Vector_space" });
  });

  it("reports snippet sources from the atom's page organs", async () => {
    const h = harness();
    const { j } = await getJson(h, "/api/brain/transfer?q=CommGroup&direction=formal_to_informal");
    const hits = j.hits as Array<Record<string, unknown>>;
    expect(hits[0]).toMatchObject({ qid: ABELIAN, slug: "Abelian_group" });
    expect(hits[0].snippet_sources).toEqual(["lmfdb_knowl", "mathworld", "nlab"]);
  });

  it("accepts the full decl:Lib:Name form", async () => {
    const h = harness();
    const { j } = await getJson(h, `/api/brain/transfer?q=${q(MODULE_DECL)}&direction=formal_to_informal`);
    expect((j.hits as unknown[]).length).toBe(2);
    expect(j.decl).toBe("Module");
  });

  it("a formal-only atom says so; an unknown decl returns near-miss suggestions", async () => {
    const h = harness();
    const formalOnly = await getJson(h, "/api/brain/transfer?q=Finset.sum_comm&direction=formal_to_informal");
    expect(formalOnly.j.id).toBe(DECL_CELL);
    expect(formalOnly.j.hits).toEqual([]);
    expect(String(formalOnly.j.note)).toContain("formal-only cell");

    const unknown = await getJson(h, "/api/brain/transfer?q=Nope.Module&direction=formal_to_informal");
    expect(unknown.status).toBe(200);
    expect(unknown.j.hits).toEqual([]);
    // suggestion search runs on the final name segment ("Module")
    expect((unknown.j.suggestions as Array<{ id: string }>).some((s) => s.id === MODULE_CELL)).toBe(true);
  });
});

describe("GET /api/brain/neighborhood — synapses, not edges", () => {
  it("returns aggregated synapses with weight, kinds and every trace", async () => {
    const h = harness();
    const { j } = await getJson(h, `/api/brain/neighborhood?id=${MODULE_CELL}`);
    const syn = j.synapses as Array<Record<string, unknown>>;
    expect(syn).toHaveLength(3);
    expect(syn[0]).toMatchObject({ id: ABELIAN_CELL, w: 15, kinds: { depends: 12, relates: 2, mentions: 1 }, traces_total: 15 });
    // a trace names the ORGANS that witnessed the bond and keeps its own direction
    expect((syn[0].traces as Array<Record<string, unknown>>)[0]).toMatchObject({
      kind: "depends", src: MODULE_DECL, dst: COMM_DECL, prov: 2,
    });
    // one synapse per PAIR: the partner may be a supercell (rule-5 field hubs)
    expect(syn[1]).toMatchObject({ id: LINALG_SUPER, w: 9 });
    // the shard trims traces per synapse AND caps the list — say so, don't hide it
    expect(j.truncated).toBe(true);
    expect(j.counts).toEqual({ syn: 6, organs: 8 });
  });

  it("accepts any organ id, and filters synapses AND traces by kind", async () => {
    const h = harness();
    const { j } = await getJson(h, `/api/brain/neighborhood?id=${VSPACE_Q}&kinds=relates`);
    expect(j).toMatchObject({ id: MODULE_CELL, key: VSPACE_Q, resolved_from: "organ", kinds: ["relates"] });
    const syn = j.synapses as Array<Record<string, unknown>>;
    expect(syn).toHaveLength(1); // only the ABELIAN synapse carries `relates`
    expect(syn[0].id).toBe(ABELIAN_CELL);
    // the traces are filtered too — asking for `relates` must not dump `depends`
    expect((syn[0].traces as Array<{ kind: string }>).every((t) => t.kind === "relates")).toBe(true);
  });

  it("traces=0 gives a compact partner list; limit marks truncation", async () => {
    const h = harness();
    const compact = await getJson(h, `/api/brain/neighborhood?id=${MODULE_CELL}&traces=0`);
    expect((compact.j.synapses as Array<Record<string, unknown>>)[0]).not.toHaveProperty("traces");
    const capped = await getJson(h, `/api/brain/neighborhood?id=${ABELIAN_CELL}&limit=1`);
    expect((capped.j.synapses as unknown[]).length).toBe(1);
    expect(capped.j.matched).toBe(1);
  });

  it("a supercell's synapses resolve too (they ship without traces)", async () => {
    const h = harness();
    const { j } = await getJson(h, `/api/brain/neighborhood?id=${q(LINALG_SUPER)}`);
    expect(j).toMatchObject({ id: LINALG_SUPER, kind: "supercell" });
    const syn = j.synapses as Array<Record<string, unknown>>;
    expect(syn[0]).toMatchObject({ id: MODULE_CELL, w: 9 });
    expect(syn[0].traces).toEqual([]); // build_cell_shards strips them from supercell rows
  });

  it("400s a bad id; 404s an unknown one", async () => {
    const h = harness();
    expect((await get(h.env, "/api/brain/neighborhood?id=")).status).toBe(400);
    expect((await get(h.env, "/api/brain/neighborhood?id=Q999999999")).status).toBe(404);
  });
});

describe("GET /api/brain/snippets", () => {
  it("reads every embedded organ payload — one row per source, each licensed", async () => {
    const h = harness();
    const { j } = await getJson(h, `/api/brain/snippets?id=${ABELIAN}`);
    expect(j.id).toBe(ABELIAN_CELL);
    const rows = j.rows as Array<Record<string, unknown>>;
    const byDb = Object.fromEntries(rows.map((r) => [String(r.source_db), r]));
    expect(byDb.wikidata).toMatchObject({
      id: ABELIAN,
      snippet: "group whose operation is commutative",
      license: "CC0 (Wikidata)",
      url: `https://www.wikidata.org/wiki/${ABELIAN}`,
    });
    expect(byDb.wikilean).toMatchObject({
      id: "Abelian_group",
      url: "https://wikilean.jackmccarthy.org/Abelian_group",
    });
    expect(byDb.lmfdb_knowl).toMatchObject({
      snippet: "An abelian group is a group whose operation is commutative.",
      license: "CC-BY-SA-4.0 (LMFDB)",
    });
    // the Lean side is a source like any other, licensed from source_registry.json
    expect(byDb.mathlib).toMatchObject({
      id: COMM_DECL,
      snippet: "A commutative group is a group with commutative multiplication.",
      license: "Apache-2.0 (Mathlib)",
      url: "https://leanprover-community.github.io/mathlib4_docs/Mathlib/Algebra/Group/Defs.html#CommGroup",
    });
    // no-content sources are deep links only
    expect(byDb.mathworld.snippet).toBeUndefined();
    expect(byDb.mathworld.url).toBe("https://mathworld.wolfram.com/AbelianGroup.html");
    expect(byDb.nlab.snippet).toBeUndefined();
  });

  it("never emits a snippet without a license, and never arXiv statement text", async () => {
    const h = harness();
    const { j } = await getJson(h, `/api/brain/snippets?id=${MODULE_CELL}`);
    const rows = j.rows as Array<Record<string, unknown>>;
    for (const r of rows) if (r.snippet !== undefined) expect(r.license).toBeTruthy();
    expect(JSON.stringify(j)).not.toContain("UNLICENSED TEXT");
    // an arXiv statement is ids + labels + links only
    expect(rows.find((r) => r.id === LIT_STMT)).toEqual({
      source_db: "arxiv",
      id: LIT_STMT,
      label: "Proposition 2.13",
      license_open: false,
      url: "https://arxiv.org/abs/2411.12318",
    });
  });

  it("resolves through any organ id, including a page organ", async () => {
    const h = harness();
    const viaPage = await getJson(h, `/api/brain/snippets?id=${q(NLAB_PAGE)}`);
    expect(viaPage.j.id).toBe(ABELIAN_CELL); // the page is an organ — you get the whole atom
    const viaMw = await getJson(h, `/api/brain/snippets?id=${q(MW_PAGE)}`);
    expect(viaMw.j.id).toBe(ABELIAN_CELL);
  });

  it("400s a bad id; 404s an unknown one", async () => {
    const h = harness();
    expect((await get(h.env, "/api/brain/snippets?id=")).status).toBe(400);
    expect((await get(h.env, "/api/brain/snippets?id=Q999999999")).status).toBe(404);
  });
});

describe("GET /api/brain/filter — facet mask math + paging", () => {
  const ids = (j: Record<string, unknown>) => (j.hits as Array<{ id: string }>).map((r) => r.id);

  it("(row.f & mask) == mask — subset masks match supersets", async () => {
    const h = harness();
    // mask 5 (bits 0+2): f=5 ✓, f=7 ✓, f=16 ✗, absent ✗
    expect(ids((await getJson(h, "/api/brain/filter?f=5")).j)).toEqual([ABELIAN_CELL, MODULE_CELL]);
    expect(ids((await getJson(h, "/api/brain/filter?f=1")).j)).toEqual([ABELIAN_CELL, MODULE_CELL]);
    expect(ids((await getJson(h, "/api/brain/filter?f=16")).j)).toEqual([DECL_CELL]);
  });

  it("f=0 matches every atom (rows without f read as 0)", async () => {
    const h = harness();
    const all = await getJson(h, "/api/brain/filter?f=0");
    expect(ids(all.j)).toHaveLength(4);
    expect(all.j.type).toBe("cell");
  });

  it("under= restricts to a containment subtree (cells carry `p`)", async () => {
    const h = harness();
    const algebra = await getJson(h, `/api/brain/filter?f=0&under=${q(ALGEBRA_SUPER)}`);
    expect(ids(algebra.j)).toEqual([ABELIAN_CELL, MODULE_CELL, DECL_CELL]);
    const defs = await getJson(h, `/api/brain/filter?f=0&under=${q("path:Mathlib/Algebra/Module/Defs")}`);
    expect(ids(defs.j)).toEqual([MODULE_CELL]);
  });

  it("type=supercell enumerates the containment tree by its AGGREGATE mask `fa`", async () => {
    const h = harness();
    const all = await getJson(h, "/api/brain/filter?f=0&type=supercell");
    expect(ids(all.j)).toEqual(["path:Mathlib", ALGEBRA_SUPER, LINALG_SUPER]);
    expect((all.j.hits as Array<Record<string, unknown>>)[2]).toMatchObject({
      label: "LinearAlgebra", fa: 5, parent: "path:Mathlib", n_cells: 1,
    });
    // `fa` is the SUBTREE aggregate, not a supercell's own facets: bit 1 is set
    // on Mathlib and Algebra (fa 31, 7) but not on LinearAlgebra (fa 5)
    const bit1 = await getJson(h, "/api/brain/filter?f=2&type=supercell");
    expect(ids(bit1.j)).toEqual(["path:Mathlib", ALGEBRA_SUPER]);
  });

  it("cursor pagination is stable and terminates with next_cursor null", async () => {
    const h = harness();
    const p1 = await getJson(h, "/api/brain/filter?f=1&limit=1");
    expect(ids(p1.j)).toEqual([ABELIAN_CELL]);
    const p2 = await getJson(h, `/api/brain/filter?f=1&limit=1&cursor=${p1.j.next_cursor}`);
    expect(ids(p2.j)).toEqual([MODULE_CELL]);
    expect((await getJson(h, `/api/brain/filter?f=1&limit=10&cursor=${p2.j.next_cursor}`)).j.next_cursor).toBeNull();
  });

  it("400s a missing/negative/garbage mask, and a v2 node type", async () => {
    const h = harness();
    expect((await get(h.env, "/api/brain/filter")).status).toBe(400);
    expect((await get(h.env, "/api/brain/filter?f=-1")).status).toBe(400);
    expect((await get(h.env, "/api/brain/filter?f=abc")).status).toBe(400);
    // the v2 types are gone — fail loudly rather than silently ignoring the filter
    const v2 = await getJson(h, "/api/brain/filter?f=0&type=concept");
    expect(v2.status).toBe(400);
    expect(String(v2.j.hint)).toContain("organs inside cells");
  });
});

describe("GET /api/brain/search — atoms, matched by any organ's label", () => {
  it("`aka` makes an organ's label find its atom", async () => {
    const h = harness();
    const { status, j } = await getJson(h, "/api/brain/search?q=vector%20space");
    expect(status).toBe(200);
    const hits = j.hits as Array<Record<string, unknown>>;
    // "Vector space" is not the atom's label — it is an organ's — and it must win
    expect(hits[0]).toMatchObject({ id: MODULE_CELL, label: "Module (mathematics)" });
    expect(hits[0].aka).toContain("Vector space");
  });

  it("a key that resolves exactly is promoted to the top hit", async () => {
    const h = harness();
    const byQid = await getJson(h, `/api/brain/search?q=${VSPACE_Q}`);
    expect((byQid.j.hits as Array<Record<string, unknown>>)[0]).toMatchObject({
      id: MODULE_CELL, matched: "organ",
    });
    const byDecl = await getJson(h, "/api/brain/search?q=CommGroup");
    expect((byDecl.j.hits as Array<{ id: string }>)[0].id).toBe(ABELIAN_CELL);
  });

  // labels.json indexes CELLS only, so a field concept has no row of its own:
  // without promoting a supercell resolution, q=Q82571 matched nothing at all.
  it("a field concept's QID or exact label finds its folder, not nothing", async () => {
    const h = harness();
    const byQid = await getJson(h, `/api/brain/search?q=${FIELD_Q}`);
    expect((byQid.j.hits as Array<Record<string, unknown>>)[0]).toMatchObject({
      id: LINALG_SUPER, kind: "supercell", matched: "organ",
    });
    const byLabel = await getJson(h, "/api/brain/search?q=Linear%20algebra");
    expect((byLabel.j.hits as Array<Record<string, unknown>>)[0]).toMatchObject({
      id: LINALG_SUPER, matched: "label",
    });
  });

  it("type=supercell searches folders through their organ labels", async () => {
    const h = harness();
    const { j } = await getJson(h, "/api/brain/search?q=linear%20algebra&type=supercell");
    expect((j.hits as Array<{ id: string }>)[0].id).toBe(LINALG_SUPER);
  });

  it("400s a short query and a v2 node type", async () => {
    const h = harness();
    expect((await get(h.env, "/api/brain/search?q=a")).status).toBe(400);
    expect((await get(h.env, "/api/brain/search?q=abelian&type=ext")).status).toBe(400);
  });
});

describe("GET /brain/api — the reference page", () => {
  it("serves self-contained HTML documenting the cell model + REST + MCP", async () => {
    const h = harness();
    const res = await get(h.env, "/brain/api");
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toContain("text/html");
    const html = await res.text();
    expect(html).toContain("/api/brain/cell");
    expect(html).toContain("/api/brain/transfer");
    expect(html).toContain("claude mcp add --transport http wikibrain");
    // the page must teach the model it serves, not the retired one
    expect(html).toContain("supercell");
    expect(html).toContain("synapse");
  });
});

describe("RESERVED", () => {
  it("'mcp' is a reserved slug — article creation there is rejected", async () => {
    const h = harness();
    const res = await put(h.env, "/api/article/mcp", {}, { bearer: PIPELINE_TOKEN, origin: null });
    expect(res.status).toBe(400);
    expect(((await res.json()) as { error: string }).error).toBe("reserved slug");
  });
});
