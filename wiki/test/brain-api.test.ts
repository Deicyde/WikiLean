// Wikibrain agent API (src/brain-api.ts): unit resolution across every key
// form (with and without the v2 aliases.json), transfer in both directions,
// neighborhood filtering, snippets, facet-filter mask math + cursor paging,
// and the /brain/api reference page. All shard/label/alias assets come from
// the shared fixture (helpers/brain-fixture.ts); no network.

import { describe, it, expect } from "vitest";
import { setup, get, put, blockNetwork, PIPELINE_TOKEN, type Harness } from "./helpers/harness.js";
import {
  installBrainFixture,
  ABELIAN,
  MODULE_Q,
  VSPACE_Q,
  EMPTY_Q,
  COMM_DECL,
  MODULE_DECL,
  LMFDB_EXT,
  NLAB_EXT,
  MW_XREF,
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

const unitOf = (j: Record<string, unknown>) => j.unit as Record<string, unknown>;

describe("GET /api/brain/unit — key resolution", () => {
  it("resolves an exact QID and assembles the unit from edges (pre-v2 shard)", async () => {
    const h = harness();
    const { status, j } = await getJson(h, `/api/brain/unit?key=${ABELIAN}`);
    expect(status).toBe(200);
    expect(j).toMatchObject({ ok: true, resolved_from: "qid", qid: ABELIAN });
    const unit = unitOf(j);
    expect(unit.qid).toBe(ABELIAN);
    expect(unit.article).toMatchObject({ slug: "Abelian_group" });
    expect(unit.decls).toEqual([
      { name: "CommGroup", module: "Mathlib.Algebra.Group.Defs", match_kind: "exact", confidence: "high" },
    ]);
    expect(unit.xrefs).toMatchObject({
      lmfdb_knowl: [{ id: "group.abelian" }],
      nlab: [{ id: "abelian+group" }],
      mathworld: [{ id: "AbelianGroup" }],
    });
    // kind:count over both directions of the shard entry
    expect(j.edges_summary).toEqual({ formalizes: 1, xref: 3, relates: 2 });
    // caches at the nightly cadence like /api/brain/node
    const res = await get(h.env, `/api/brain/unit?key=${ABELIAN}`);
    expect(res.headers.get("Cache-Control")).toBe("public, max-age=3600");
  });

  it("passes a prebuilt node.unit through verbatim", async () => {
    const h = harness();
    const { j } = await getJson(h, `/api/brain/unit?key=${MODULE_Q}`);
    expect(unitOf(j).description).toBe("algebraic structure over a ring");
    expect(unitOf(j).decls).toEqual([
      { name: "Module", module: "Mathlib.Algebra.Module.Defs", match_kind: "exact", confidence: "high" },
    ]);
  });

  it("resolves a bare decl name via aliases.json when present", async () => {
    const h = harness();
    const { j } = await getJson(h, "/api/brain/unit?key=CommGroup");
    expect(j).toMatchObject({ ok: true, resolved_from: "decl", qid: ABELIAN });
  });

  it("alias order wins over edge confidence when aliases.json exists", async () => {
    const h = harness(); // DEFAULT_ALIASES lists VSPACE_Q first for "Module"
    const { j } = await getJson(h, "/api/brain/unit?key=decl%3AMathlib%3AModule");
    expect(j).toMatchObject({ resolved_from: "decl", qid: VSPACE_Q });
  });

  it("falls back to inbound formalizes edges when aliases.json is missing", async () => {
    const h = harness({ aliases: null });
    // bare name → decl:Mathlib:<name> → in-edges, ranked by confidence
    const bare = await getJson(h, "/api/brain/unit?key=Module");
    expect(bare.j).toMatchObject({ resolved_from: "decl", qid: MODULE_Q });
    const full = await getJson(h, "/api/brain/unit?key=decl%3AMathlib%3ACommGroup");
    expect(full.j).toMatchObject({ resolved_from: "decl", qid: ABELIAN });
  });

  it("resolves an article slug via aliases, and via labels.json without them", async () => {
    const withAliases = await getJson(harness(), "/api/brain/unit?key=Abelian_group");
    expect(withAliases.j).toMatchObject({ resolved_from: "slug", qid: ABELIAN });
    // Vector_space is NOT in the aliases fixture → labels.json slug match
    const viaLabels = await getJson(harness({ aliases: null }), "/api/brain/unit?key=Vector_space");
    expect(viaLabels.j).toMatchObject({ resolved_from: "slug", qid: VSPACE_Q });
  });

  it("resolves xref ids: through the ext node's own qid, then through in-edges", async () => {
    const h = harness();
    const viaQid = await getJson(h, `/api/brain/unit?key=${encodeURIComponent(LMFDB_EXT)}`);
    expect(viaQid.j).toMatchObject({ resolved_from: "xref", qid: ABELIAN });
    // NLAB_EXT has no qid field — its inbound xref edge names the concept
    const viaEdges = await getJson(h, `/api/brain/unit?key=${encodeURIComponent(NLAB_EXT)}`);
    expect(viaEdges.j).toMatchObject({ resolved_from: "xref", qid: ABELIAN });
  });

  it("resolves an exact concept label case-insensitively", async () => {
    const h = harness();
    const { j } = await getJson(h, "/api/brain/unit?key=abelian%20GROUP");
    expect(j).toMatchObject({ resolved_from: "label", qid: ABELIAN });
  });

  it("404s an unresolvable key with a hint to /api/brain/search", async () => {
    const h = harness();
    const { status, j } = await getJson(h, "/api/brain/unit?key=NoSuchThingAnywhere");
    expect(status).toBe(404);
    expect(j.ok).toBe(false);
    expect(String(j.hint)).toContain("/api/brain/search");
    // an xref target that was never minted as a node is also a 404
    const unminted = await getJson(h, `/api/brain/unit?key=${encodeURIComponent(MW_XREF)}`);
    expect(unminted.status).toBe(404);
  });

  it("400s a missing key", async () => {
    const h = harness();
    expect((await get(h.env, "/api/brain/unit")).status).toBe(400);
  });
});

describe("GET /api/brain/transfer — informal_to_formal", () => {
  it("QID → ranked decls with docs URLs, via_qid and qid_label", async () => {
    const h = harness();
    const { status, j } = await getJson(
      h, `/api/brain/transfer?q=${ABELIAN}&direction=informal_to_formal`);
    expect(status).toBe(200);
    const hits = j.hits as Array<Record<string, unknown>>;
    expect(hits).toHaveLength(1);
    expect(hits[0]).toEqual({
      decl: "CommGroup",
      module: "Mathlib.Algebra.Group.Defs",
      match_kind: "exact",
      confidence: "high",
      docs_url:
        "https://leanprover-community.github.io/mathlib4_docs/Mathlib/Algebra/Group/Defs.html#CommGroup",
      via_qid: ABELIAN,
      qid_label: "Abelian group",
    });
  });

  it("free text falls back to label search (resolved_from=search)", async () => {
    const h = harness();
    const { j } = await getJson(
      h, "/api/brain/transfer?q=vector%20spa&direction=informal_to_formal");
    expect(j).toMatchObject({ ok: true, resolved_from: "search", qid: VSPACE_Q });
    const hits = j.hits as Array<Record<string, unknown>>;
    expect(hits[0]).toMatchObject({ decl: "Module", match_kind: "related", via_qid: VSPACE_Q });
  });

  it("a concept with no decls returns empty hits + suggestions", async () => {
    const h = harness();
    const { status, j } = await getJson(
      h, "/api/brain/transfer?q=Parity%20conjecture&direction=informal_to_formal");
    expect(status).toBe(200);
    expect(j.qid).toBe(EMPTY_Q);
    expect(j.hits).toEqual([]);
    expect((j.suggestions as unknown[]).length).toBeGreaterThan(0);
  });

  it("404s (with suggestions) when nothing matches at all", async () => {
    const h = harness();
    const { status, j } = await getJson(
      h, "/api/brain/transfer?q=zzzz-nothing&direction=informal_to_formal");
    expect(status).toBe(404);
    expect(j.suggestions).toEqual([]);
  });

  it("400s a bad direction and a missing q", async () => {
    const h = harness();
    expect((await get(h.env, "/api/brain/transfer?q=x&direction=sideways")).status).toBe(400);
    expect((await get(h.env, "/api/brain/transfer?direction=informal_to_formal")).status).toBe(400);
  });
});

describe("GET /api/brain/transfer — formal_to_informal", () => {
  it("a decl formalized by two concepts lists both, best confidence first", async () => {
    const h = harness();
    const { j } = await getJson(h, "/api/brain/transfer?q=Module&direction=formal_to_informal");
    const hits = j.hits as Array<Record<string, unknown>>;
    expect(hits.map((x) => x.qid)).toEqual([MODULE_Q, VSPACE_Q]); // high before medium
    expect(hits[0]).toMatchObject({
      label: "Module",
      slug: "Module_(mathematics)",
      article_url: "https://wikilean.jackmccarthy.org/Module_(mathematics)",
      description: "algebraic structure over a ring", // from the prebuilt unit
    });
  });

  it("reports snippet sources from the concept's cross-refs", async () => {
    const h = harness();
    const { j } = await getJson(h, "/api/brain/transfer?q=CommGroup&direction=formal_to_informal");
    const hits = j.hits as Array<Record<string, unknown>>;
    expect(hits[0]).toMatchObject({ qid: ABELIAN, slug: "Abelian_group" });
    expect(hits[0].snippet_sources).toEqual(["lmfdb_knowl", "mathworld", "nlab"]);
  });

  it("accepts the full decl:Lib:Name form", async () => {
    const h = harness();
    const { j } = await getJson(
      h, `/api/brain/transfer?q=${encodeURIComponent(MODULE_DECL)}&direction=formal_to_informal`);
    expect((j.hits as unknown[]).length).toBe(2);
    expect(j.decl).toBe("Module");
  });

  it("an unknown decl returns empty hits with near-miss suggestions", async () => {
    const h = harness();
    const { status, j } = await getJson(
      h, "/api/brain/transfer?q=Nope.Module&direction=formal_to_informal");
    expect(status).toBe(200);
    expect(j.hits).toEqual([]);
    // suggestion search runs on the final name segment ("Module")
    expect((j.suggestions as Array<{ id: string }>).some((s) => s.id === MODULE_Q)).toBe(true);
  });
});

describe("GET /api/brain/neighborhood", () => {
  it("filters by kinds and direction with counts", async () => {
    const h = harness();
    const { j } = await getJson(h, `/api/brain/neighborhood?id=${ABELIAN}&kinds=xref&dir=out`);
    const edges = j.edges as Array<Record<string, unknown>>;
    expect(edges).toHaveLength(3);
    expect(edges.every((e) => e.kind === "xref" && e.direction === "out")).toBe(true);
    expect(j.counts).toEqual({ out: 5, in: 1 });
    expect(j.truncated).toBe(false);
  });

  it("marks truncation when limit cuts matching edges", async () => {
    const h = harness();
    const { j } = await getJson(h, `/api/brain/neighborhood?id=${ABELIAN}&kinds=xref&dir=out&limit=2`);
    expect((j.edges as unknown[]).length).toBe(2);
    expect(j.matched).toEqual({ out: 3, in: 0 });
    expect(j.truncated).toBe(true);
  });

  it("dir=both walks in-edges too (a decl's formalizing concepts)", async () => {
    const h = harness();
    const { j } = await getJson(
      h, `/api/brain/neighborhood?id=${encodeURIComponent(MODULE_DECL)}&kinds=formalizes`);
    const edges = j.edges as Array<Record<string, unknown>>;
    expect(edges.map((e) => e.id).sort()).toEqual([VSPACE_Q, MODULE_Q].sort());
  });

  it("400s a bad id/dir; 404s an unknown node", async () => {
    const h = harness();
    expect((await get(h.env, "/api/brain/neighborhood?id=")).status).toBe(400);
    expect((await get(h.env, `/api/brain/neighborhood?id=${ABELIAN}&dir=up`)).status).toBe(400);
    expect((await get(h.env, "/api/brain/neighborhood?id=Q999999999")).status).toBe(404);
  });
});

describe("GET /api/brain/snippets", () => {
  it("a concept gathers article pointer + ext snippets with licenses", async () => {
    const h = harness();
    const { j } = await getJson(h, `/api/brain/snippets?id=${ABELIAN}`);
    const rows = j.rows as Array<Record<string, unknown>>;
    const byDb = Object.fromEntries(rows.map((r) => [String(r.source_db), r]));
    expect(byDb.wikidata).toMatchObject({ id: ABELIAN, url: `https://www.wikidata.org/wiki/${ABELIAN}` });
    expect(byDb.wikilean).toMatchObject({
      id: "Abelian_group",
      url: "https://wikilean.jackmccarthy.org/Abelian_group",
    });
    expect(byDb.lmfdb_knowl).toMatchObject({
      snippet: "An abelian group is a group whose operation is commutative.",
      license: "CC-BY-SA-4.0 (LMFDB)",
      url: "https://www.lmfdb.org/knowledge/show/group.abelian",
    });
    // nlab ext node exists but stores no snippet — row is a deep link
    expect(byDb.nlab).toMatchObject({ url: "https://ncatlab.org/nlab/show/abelian+group" });
    expect(byDb.nlab.snippet).toBeUndefined();
    // unminted mathworld xref still surfaces as a pointer row
    expect(byDb.mathworld).toMatchObject({ id: MW_XREF, label: "AbelianGroup" });
  });

  it("the wikidata row carries the description when a unit provides one", async () => {
    const h = harness();
    const { j } = await getJson(h, `/api/brain/snippets?id=${MODULE_Q}`);
    const rows = j.rows as Array<Record<string, unknown>>;
    expect(rows.find((r) => r.source_db === "wikidata")).toMatchObject({
      snippet: "algebraic structure over a ring",
      license: "CC0 (Wikidata)",
    });
  });

  it("an ext node returns its own single row", async () => {
    const h = harness();
    const { j } = await getJson(h, `/api/brain/snippets?id=${encodeURIComponent(LMFDB_EXT)}`);
    expect(j.rows).toEqual([
      {
        source_db: "lmfdb_knowl",
        id: LMFDB_EXT,
        label: "group.abelian",
        snippet: "An abelian group is a group whose operation is commutative.",
        license: "CC-BY-SA-4.0 (LMFDB)",
        url: "https://www.lmfdb.org/knowledge/show/group.abelian",
      },
    ]);
  });

  it("404s an unknown id", async () => {
    const h = harness();
    expect((await get(h.env, "/api/brain/snippets?id=Q999999999")).status).toBe(404);
  });
});

describe("GET /api/brain/filter — facet mask math + paging", () => {
  const ids = (j: Record<string, unknown>) => (j.hits as Array<{ id: string }>).map((r) => r.id);

  it("(row.f & mask) == mask — subset masks match supersets", async () => {
    const h = harness();
    // mask 5 (bits 0+2): f=5 ✓, f=7 ✓, f=1 ✗, absent ✗
    const m5 = await getJson(h, "/api/brain/filter?f=5");
    expect(ids(m5.j)).toEqual([ABELIAN, MODULE_Q]);
    // mask 1: any row with bit0
    const m1 = await getJson(h, "/api/brain/filter?f=1");
    expect(ids(m1.j)).toEqual([ABELIAN, MODULE_Q, VSPACE_Q]);
    // mask 256 (bit8 = is ext)
    const m256 = await getJson(h, "/api/brain/filter?f=256");
    expect(ids(m256.j)).toEqual([LMFDB_EXT]);
  });

  it("f=0 matches every row (rows without f read as 0); type narrows", async () => {
    const h = harness();
    const all = await getJson(h, "/api/brain/filter?f=0");
    expect(ids(all.j)).toHaveLength(6);
    const concepts = await getJson(h, "/api/brain/filter?f=0&type=concept");
    expect(ids(concepts.j)).toHaveLength(4);
    const extOnly = await getJson(h, "/api/brain/filter?f=256&type=concept");
    expect(ids(extOnly.j)).toEqual([]);
  });

  it("cursor pagination is stable and terminates with next_cursor null", async () => {
    const h = harness();
    const p1 = await getJson(h, "/api/brain/filter?f=1&limit=1");
    expect(ids(p1.j)).toEqual([ABELIAN]);
    expect(typeof p1.j.next_cursor).toBe("number");
    const p2 = await getJson(h, `/api/brain/filter?f=1&limit=1&cursor=${p1.j.next_cursor}`);
    expect(ids(p2.j)).toEqual([MODULE_Q]);
    const p3 = await getJson(h, `/api/brain/filter?f=1&limit=10&cursor=${p2.j.next_cursor}`);
    expect(ids(p3.j)).toEqual([VSPACE_Q]);
    expect(p3.j.next_cursor).toBeNull();
  });

  it("400s a missing/negative/garbage mask", async () => {
    const h = harness();
    expect((await get(h.env, "/api/brain/filter")).status).toBe(400);
    expect((await get(h.env, "/api/brain/filter?f=-1")).status).toBe(400);
    expect((await get(h.env, "/api/brain/filter?f=abc")).status).toBe(400);
  });
});

describe("GET /api/brain/search — refactored onto searchLabels, output pinned", () => {
  it("prefix hits rank before substring hits; rows pass through verbatim", async () => {
    const h = harness();
    const { status, j } = await getJson(h, "/api/brain/search?q=module");
    expect(status).toBe(200);
    const hits = j.hits as Array<Record<string, unknown>>;
    // "Module" is a prefix hit and keeps every labels.json field
    expect(hits[0]).toEqual({
      id: MODULE_Q, type: "concept", label: "Module",
      slug: "Module_(mathematics)", status: "formalized", f: 7,
    });
  });

  it("a bare QID query matches by id; type= filters", async () => {
    const h = harness();
    const byQid = await getJson(h, `/api/brain/search?q=${ABELIAN}`);
    expect((byQid.j.hits as Array<{ id: string }>)[0].id).toBe(ABELIAN);
    const typed = await getJson(h, "/api/brain/search?q=abelian&type=ext");
    expect((typed.j.hits as Array<{ id: string }>).every((r) => r.id === LMFDB_EXT)).toBe(true);
    expect((await get(h.env, "/api/brain/search?q=a")).status).toBe(400);
  });
});

describe("GET /brain/api — the reference page", () => {
  it("serves self-contained HTML documenting REST + MCP", async () => {
    const h = harness();
    const res = await get(h.env, "/brain/api");
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toContain("text/html");
    const html = await res.text();
    expect(html).toContain("/api/brain/unit");
    expect(html).toContain("/api/brain/transfer");
    expect(html).toContain("claude mcp add --transport http wikibrain");
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
