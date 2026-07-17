// Wikibrain agent API (src/brain-api.ts), BRAIN v3 — the CELL model: resolving
// every organ id (and every v2 entry point) to its owning atom through
// aliases.json, the atom card with embedded organ payloads, transfer in both
// directions incl. the rule-5 field-concept → supercell answer, synapse
// projection with traces, snippets + the licence floor, facet-filter mask math
// + cursor paging, `aka` search, and the /brain/api reference page. All assets
// come from the shared fixture (helpers/brain-fixture.ts); no network.

import { beforeEach, describe, it, expect } from "vitest";
import { _resetBrainAssetMemo } from "../src/brain.js";
import { SYNAPSE_KINDS, SYNAPSE_KINDS_CSV } from "../src/brain-api.js";
import { setup, get, put, blockNetwork, PIPELINE_TOKEN, type Harness } from "./helpers/harness.js";
import {
  installBrainFixture,
  ABELIAN_CELL,
  MODULE_CELL,
  EMPTY_CELL,
  DECL_CELL,
  BASIS_CELL,
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
    expect(j).toMatchObject({ id: ABELIAN_CELL, kind: "cell", resolved_from: "organ", match: "exact" });
    // item 2: every decl hit ships module + import_line + the statement code
    expect(j.hits).toEqual([
      {
        decl: "CommGroup",
        module: "Mathlib.Algebra.Group.Defs",
        import_line: "import Mathlib.Algebra.Group.Defs",
        bond: "exact",
        decl_kind: "class",
        code: "class CommGroup (G : Type u) extends Group G, CommMonoid G",
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

  // supercells.json ships its syn rows traceless and names THIS endpoint as
  // where to get them. It used to answer `traces: []` — a confident "the Brain
  // holds no evidence for this bond" for 5,160 rows that all have one.
  it("a supercell's traces are HYDRATED from the partner cell's mirror row", async () => {
    const h = harness();
    const { j } = await getJson(h, `/api/brain/neighborhood?id=${q(LINALG_SUPER)}`);
    expect(j).toMatchObject({ id: LINALG_SUPER, kind: "supercell", traces_hydrated: 1 });
    const syn = j.synapses as Array<Record<string, unknown>>;
    expect(syn[0]).toMatchObject({ id: MODULE_CELL, w: 9, traces_total: 9 });
    // the real evidence, read off MODULE_CELL's shard — not an empty list
    expect(syn[0].traces).toMatchObject([
      { kind: "depends", src: MODULE_DECL, dst: FIELD_Q },
      { kind: "invocation", src: MODULE_Q, dst: FIELD_Q },
    ]);
  });

  // Where hydration genuinely cannot reach, the row must say so — never `[]`.
  it("an unreachable trace is DECLARED, never reported as an empty trace list", async () => {
    const h = harness();
    const { j } = await getJson(h, `/api/brain/neighborhood?id=${q(LINALG_SUPER)}`);
    const syn = j.synapses as Array<Record<string, unknown>>;
    const superSuper = syn.find((s) => s.id === ALGEBRA_SUPER)!;
    expect(superSuper.traces).toBeUndefined();
    expect(String(superSuper.traces_unavailable)).toContain("traceless on both endpoints");
    const noMirror = syn.find((s) => s.id === DECL_CELL)!;
    expect(noMirror.traces).toBeUndefined();
    expect(String(noMirror.traces_unavailable)).toContain("shard-capped");
    // every unreachable row points at the surface that DOES serve it
    for (const s of [superSuper, noMirror]) expect(String(s.traces_unavailable)).toContain("brain/query.py --full");
  });

  // THE regression: supercells.json carries no `truncated` field on any of its
  // 9,052 entries, so reading one yielded undefined and a supercell withholding
  // 728 of 928 synapses reported `truncated: false`. It must derive from the
  // TRUE total (counts.syn), not from the capped list's length.
  it("a supercell declares synapses the shard withheld (counts.syn > the list)", async () => {
    const h = harness();
    const { j } = await getJson(h, `/api/brain/neighborhood?id=${q(LINALG_SUPER)}&traces=0`);
    expect(j.counts).toMatchObject({ syn: 5 }); // the true total
    expect((j.synapses as unknown[]).length).toBe(3); // what the shard carries
    expect(j.withheld_by_shard).toBe(2); // a COUNT, per SCHEMA — not just a flag
    expect(j.truncated).toBe(true);
    // `matched` only ever counts rows already in the capped list, which is
    // exactly why it must not be the source of the flag
    expect(j.matched).toBe(3);
    // and the atom card declares the same count
    const card = await getJson(h, `/api/brain/cell?key=${q(LINALG_SUPER)}`);
    expect(card.j.truncated).toEqual({ syn: 2 });
  });

  it("does not cry truncation when the whole list ships", async () => {
    const h = harness();
    const { j } = await getJson(h, `/api/brain/neighborhood?id=${ABELIAN}&traces=0`);
    expect(j.truncated).toBe(false);
    expect(j.withheld_by_shard).toBe(0);
  });

  it("400s a bad id; 404s an unknown one", async () => {
    const h = harness();
    expect((await get(h.env, "/api/brain/neighborhood?id=")).status).toBe(400);
    expect((await get(h.env, "/api/brain/neighborhood?id=Q999999999")).status).toBe(404);
  });

  // The documented enum used to be wrong in BOTH directions: it advertised
  // formalizes/matches (which are organ attachments the merge function consumes,
  // never synapses — 0 rows on every atom) and omitted the five rule-2/3/4 kinds
  // that carry 2,326 real bonds. A caller sending the old "complete" list got a
  // silent partial answer, so an unknown kind must now name itself.
  it("names a kind that is not a synapse kind instead of answering 0 rows", async () => {
    const h = harness();
    const { j } = await getJson(h, `/api/brain/neighborhood?id=${MODULE_CELL}&traces=0&kinds=depends,formalizes,matches`);
    expect(j.unknown_kinds).toEqual(["formalizes", "matches"]);
    expect(String(j.hint)).toContain("organ attachments");
    expect((j.synapses as unknown[]).length).toBeGreaterThan(0); // the known kind still answers
  });

  it("the documented kind set is exactly the set the data emits", async () => {
    // both directions — a kind here that the shards never emit is as much a
    // defect as one they emit that is missing
    expect(SYNAPSE_KINDS).toContain("co-page");
    expect(SYNAPSE_KINDS).toContain("co-statement");
    expect(SYNAPSE_KINDS).not.toContain("formalizes");
    expect(SYNAPSE_KINDS).not.toContain("matches");
    const h = harness();
    const { j } = await getJson(h, `/api/brain/neighborhood?id=${MODULE_CELL}&traces=0&kinds=${SYNAPSE_KINDS_CSV}`);
    expect(j.unknown_kinds).toBeUndefined();
  });
});

// v3 drops two v2 populations on purpose; they must fail HONESTLY rather than
// contradict a documentation promise or read as "unknown to the Brain".
describe("dropped-in-v3 ids 404 with a reason", () => {
  it("an unanchored ext page names the drop", async () => {
    const h = harness();
    const { status, j } = await getJson(h, `/api/brain/cell?key=${q(UNKNOWN_XREF)}`);
    expect(status).toBe(404);
    expect(j.error).toBe("no atom owns this organ id");
    expect(String(j.reason)).toContain("no cell claims it");
    expect(String(j.reason)).toContain("Dropped in v3");
  });

  it("an arXiv PAPER id explains it has no atom (only statements are organs)", async () => {
    const h = harness();
    const { status, j } = await getJson(h, "/api/brain/cell?key=lit:1612.08419");
    expect(status).toBe(404);
    expect(String(j.reason)).toContain("PAPER");
    expect(String(j.reason)).toContain("co-statement");
  });

  it("a genuinely unknown id keeps the plain error (no false reason)", async () => {
    const h = harness();
    const { status, j } = await getJson(h, "/api/brain/cell?key=Q999999999");
    expect(status).toBe(404);
    expect(j.error).toBe("unresolvable key");
    expect(j.reason).toBeUndefined();
  });

  it("the same reason reaches /neighborhood and /snippets", async () => {
    const h = harness();
    for (const route of ["neighborhood", "snippets"]) {
      const { status, j } = await getJson(h, `/api/brain/${route}?id=${q(UNKNOWN_XREF)}`);
      expect(status).toBe(404);
      expect(String(j.reason)).toContain("no cell claims it");
    }
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

  // A cell may legitimately have >1 supercell and "renders inside each", but
  // labels.json `p` names only the deepest — so `p` alone hid every such cell
  // from the subtree of its OTHER supercell, while that folder's own card listed
  // it and its `fa` mask advertised the match. MODULE_CELL's `p` is under
  // Algebra; LINALG_SUPER.cells lists it.
  it("under= finds a cell that spans two supercells, from EITHER of them", async () => {
    const h = harness();
    const linalg = await getJson(h, `/api/brain/filter?f=0&under=${q(LINALG_SUPER)}`);
    expect(ids(linalg.j)).toContain(MODULE_CELL);
    // and it stays reachable from the supercell its `p` does name
    const algebra = await getJson(h, `/api/brain/filter?f=0&under=${q(ALGEBRA_SUPER)}`);
    expect(ids(algebra.j)).toContain(MODULE_CELL);
    // the filter and the supercell card must agree on membership
    const card = await getJson(h, `/api/brain/cell?key=${q(LINALG_SUPER)}`);
    expect((card.j.supercell as { cells: string[] }).cells).toContain(MODULE_CELL);
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

// BRIDGE item 1 — batch decl_exists with labelled rename suggestions.
describe("GET /api/brain/decl — batch existence + rename suggestions", () => {
  it("the batch example: ['Basis','Module.Basis','AddCircle.fourierCoeff','NotARealName']", async () => {
    const h = harness();
    const { status, j } = await getJson(
      h,
      `/api/brain/decl?names=${q("Basis,Module.Basis,AddCircle.fourierCoeff,NotARealName")}`,
    );
    expect(status).toBe(200);
    const results = j.results as Array<Record<string, unknown>>;
    const byName = Object.fromEntries(results.map((r) => [String(r.decl), r]));
    // Basis is dead → verified-rename to Module.Basis, whose module is served
    expect(byName["Basis"]).toMatchObject({
      exists: false,
      renamed_to: "Module.Basis",
      suggestion_basis: "verified-rename",
      module: "Mathlib.LinearAlgebra.Basis.Defs",
      import_line: "import Mathlib.LinearAlgebra.Basis.Defs",
    });
    // Module.Basis is real → module + import_line (item 2)
    expect(byName["Module.Basis"]).toMatchObject({
      exists: true,
      module: "Mathlib.LinearAlgebra.Basis.Defs",
      import_line: "import Mathlib.LinearAlgebra.Basis.Defs",
    });
    // a removed-prefix rename resolves the same way
    expect(byName["AddCircle.fourierCoeff"]).toMatchObject({
      exists: false,
      renamed_to: "fourierCoeff",
      suggestion_basis: "verified-rename",
    });
    // a true hallucination gets NO suggestion — never a forced one
    expect(byName["NotARealName"]).toMatchObject({ exists: false });
    expect(byName["NotARealName"].renamed_to).toBeUndefined();
    expect(String(byName["NotARealName"].hint)).toContain("decl index");
    // everything dropped is counted, never silent
    expect(j.counts).toEqual({ total: 4, exists: 1, renamed: 2, missing: 1 });
  });

  it("a unique last-segment match is a labelled suggestion, verified against the oracle", async () => {
    const h = harness();
    const { j } = await getJson(h, `/api/brain/decl?name=sum_comm`);
    expect(j).toMatchObject({
      exists: false,
      renamed_to: "Finset.sum_comm",
      suggestion_basis: "unique-suffix-match",
      module: "Mathlib.Algebra.BigOperators.Basic",
    });
  });

  it("the single-name form still works and gains import_line", async () => {
    const h = harness();
    const { j } = await getJson(h, "/api/brain/decl?name=CommGroup");
    expect(j).toMatchObject({
      ok: true,
      exists: true,
      module: "Mathlib.Algebra.Group.Defs",
      import_line: "import Mathlib.Algebra.Group.Defs",
    });
    expect(j.results).toBeUndefined(); // single, not batch
  });

  it("rejects an over-long batch and a bad name", async () => {
    const h = harness();
    const many = Array.from({ length: 17 }, (_, i) => `X${i}`).join(",");
    expect((await get(h.env, `/api/brain/decl?names=${q(many)}`)).status).toBe(400);
    expect((await get(h.env, `/api/brain/decl?names=${q("Ok,bad name")}`)).status).toBe(400);
  });
});

// BRIDGE item 3 — the generalization note (Module generalizes Vector space).
describe("GET /api/brain/transfer — generalization surfaced + honest abstention", () => {
  it("querying an absorbed generalization concept says the decl generalizes it", async () => {
    const h = harness();
    const { j } = await getJson(h, `/api/brain/transfer?q=${q("Vector space")}&direction=informal_to_formal`);
    expect(j).toMatchObject({ id: MODULE_CELL, match: "generalization" });
    expect((j.hits as Array<{ decl: string }>)[0].decl).toBe("Module");
    expect(String(j.note)).toContain("generalizes");
    expect(String(j.note)).toContain("Vector space");
    // the confidence-floor rule is stated in the response itself (item 4)
    expect(String(j.confidence_floor)).toContain("clears the floor");
  });

  it("an exact identity match is match:'exact' with no note", async () => {
    const h = harness();
    const { j } = await getJson(h, `/api/brain/transfer?q=${ABELIAN}&direction=informal_to_formal`);
    expect(j.match).toBe("exact");
    expect(j.note).toBeUndefined();
    // item 3: the atom's breadcrumb is shared across its hits
    expect(j.breadcrumb).toEqual([
      { id: "path:Mathlib", label: "Mathlib" },
      { id: ALGEBRA_SUPER, label: "Algebra" },
    ]);
  });
});

// BRIDGE item 5 — cursored, filterable neighborhood.
describe("GET /api/brain/neighborhood — cursor, min_w, min_conf", () => {
  it("paginates by opaque cursor in stable (-w, id) order without silent caps", async () => {
    const h = harness();
    const p1 = await getJson(h, `/api/brain/neighborhood?id=${MODULE_CELL}&limit=1&traces=0`);
    const s1 = p1.j.synapses as Array<{ id: string; w: number }>;
    expect(s1).toHaveLength(1);
    expect(s1[0].id).toBe(ABELIAN_CELL); // heaviest (w=15) first
    expect(typeof p1.j.next_cursor).toBe("string");
    expect(p1.j.truncated).toBe(true);
    const p2 = await getJson(
      h,
      `/api/brain/neighborhood?id=${MODULE_CELL}&limit=1&traces=0&cursor=${q(String(p1.j.next_cursor))}`,
    );
    expect((p2.j.synapses as Array<{ id: string }>)[0].id).toBe(LINALG_SUPER); // next heaviest (w=9)
    // walk to the end
    const p3 = await getJson(
      h,
      `/api/brain/neighborhood?id=${MODULE_CELL}&limit=1&traces=0&cursor=${q(String(p2.j.next_cursor))}`,
    );
    expect((p3.j.synapses as Array<{ id: string }>)[0].id).toBe(EMPTY_CELL); // w=2
    expect(p3.j.next_cursor).toBeUndefined(); // no more in the shard list
    // but the shard cap is still declared, never silently swallowed
    expect(p3.j.withheld_by_shard).toBe(3);
  });

  it("min_w floors the synapse weight", async () => {
    const h = harness();
    const { j } = await getJson(h, `/api/brain/neighborhood?id=${MODULE_CELL}&min_w=9&traces=0`);
    const ids = (j.synapses as Array<{ id: string }>).map((s) => s.id);
    expect(ids).toEqual([ABELIAN_CELL, LINALG_SUPER]); // w=15, 9 — EMPTY (w=2) dropped
    expect(j.min_w).toBe(9);
    expect(j.matched).toBe(2);
  });

  it("min_conf drops only sub-threshold traces that CARRY a confidence, keeping unscored ones", async () => {
    const h = harness();
    const { j } = await getJson(h, `/api/brain/neighborhood?id=${MODULE_CELL}&min_conf=0.5`);
    expect(j.min_conf).toBe(0.5);
    // the Module→Empty links trace carries confidence 0.4 < 0.5 → dropped + counted
    expect(j.traces_conf_filtered).toBe(1);
    const empty = (j.synapses as Array<Record<string, unknown>>).find((s) => s.id === EMPTY_CELL)!;
    expect(empty.traces).toEqual([]); // its only trace was below the floor
    // the Abelian synapse's traces carry no confidence, so they are KEPT
    const abelian = (j.synapses as Array<Record<string, unknown>>).find((s) => s.id === ABELIAN_CELL)!;
    expect((abelian.traces as unknown[]).length).toBeGreaterThan(0);
  });

  it("a garbage cursor restarts from the top rather than throwing", async () => {
    const h = harness();
    const { status, j } = await getJson(h, `/api/brain/neighborhood?id=${MODULE_CELL}&traces=0&cursor=not-base64`);
    expect(status).toBe(200);
    expect((j.synapses as Array<{ id: string }>)[0].id).toBe(ABELIAN_CELL);
  });
});

// BRIDGE item 6 — snapshot echo on EVERY response.
describe("snapshot echo", () => {
  it("every route carries snapshot:{generated_at, pin} from the manifest", async () => {
    const h = harness();
    for (const path of [
      `/api/brain/cell?key=${ABELIAN}`,
      `/api/brain/transfer?q=${ABELIAN}&direction=informal_to_formal`,
      `/api/brain/neighborhood?id=${ABELIAN}`,
      `/api/brain/snippets?id=${ABELIAN}`,
      "/api/brain/filter?f=0",
      "/api/brain/search?q=abelian",
      "/api/brain/decl?name=CommGroup",
      "/api/brain/bridge?q=abelian%20group",
    ]) {
      const { j } = await getJson(h, path);
      // pin is the first Mathlib-source prov pin in the fixture manifest
      expect(j.snapshot).toEqual({ generated_at: "2026-07-15", pin: "2026-07-04" });
    }
  });

  it("error responses echo it too", async () => {
    const h = harness();
    const { j } = await getJson(h, "/api/brain/cell?key=Q999999999");
    expect(j.snapshot).toMatchObject({ generated_at: "2026-07-15" });
  });
});

// BRIDGE item 7 — the composite brain_bridge call.
describe("GET /api/brain/bridge — the first call of an autoformalization loop", () => {
  it("informal statement → existence-verified decls with signatures, imports, depends", async () => {
    const h = harness();
    const { status, j } = await getJson(h, `/api/brain/bridge?q=${q("abelian group")}`);
    expect(status).toBe(200);
    expect(j).toMatchObject({ q: "abelian group", match: "exact" });
    // the candidate atoms considered, with resolution provenance
    expect((j.atoms as Array<{ id: string }>)[0].id).toBe(ABELIAN_CELL);
    const hits = j.hits as Array<Record<string, unknown>>;
    expect(hits[0]).toMatchObject({
      decl: "CommGroup",
      exists: true, // verified against the decl-index oracle
      module: "Mathlib.Algebra.Group.Defs",
      import_line: "import Mathlib.Algebra.Group.Defs",
      bond: "exact",
      code: "class CommGroup (G : Type u) extends Group G, CommMonoid G",
      via_cell: ABELIAN_CELL,
    });
    // per-hit breadcrumb (hits span atoms in the bridge)
    expect(hits[0].breadcrumb).toEqual([
      { id: "path:Mathlib", label: "Mathlib" },
      { id: ALGEBRA_SUPER, label: "Algebra" },
    ]);
    // one-hop depends from the primary atom: ids + labels, capped + counted
    expect(j.depends).toMatchObject({ returned: 1, total: 1 });
    expect((j.depends as { partners: Array<{ id: string; label: string }> }).partners[0]).toMatchObject({
      id: MODULE_CELL,
      label: "Module (mathematics)",
    });
    // it teaches the next step of the loop
    expect((j.next_tools as string[]).join(" ")).toContain("decl_exists");
    expect(j.snapshot).toBeTruthy();
  });

  it("surfaces a rename suggestion when a cited decl is dead", async () => {
    const h = harness();
    // resolve straight to the Basis atom by its concept QID (an organ handle)
    const { j } = await getJson(h, `/api/brain/bridge?q=Q189569`);
    const hits = j.hits as Array<Record<string, unknown>>;
    const dead = hits.find((x) => x.decl === "Basis")!;
    expect(dead).toMatchObject({
      exists: false, // `Basis` is not in the decl-index oracle
      renamed_to: "Module.Basis",
      suggestion_basis: "verified-rename",
      suggested_import_line: "import Mathlib.LinearAlgebra.Basis.Defs",
    });
    // and the current name verifies clean on the same atom
    expect(hits.find((x) => x.decl === "Module.Basis")).toMatchObject({ exists: true });
  });

  it("abstains with match:'none' + nearest when nothing clears the floor", async () => {
    const h = harness();
    // "Parity conjecture" resolves (exact label) but holds no decl
    const { j } = await getJson(h, `/api/brain/bridge?q=${q("Parity conjecture")}`);
    expect(j.match).toBe("none");
    expect(j.hits).toEqual([]);
    expect((j.nearest as Array<{ id: string }>)[0].id).toBe(EMPTY_CELL);
    expect(String((j.nearest as Array<{ why: string }>)[0].why)).toContain("no Mathlib declaration");
  });

  it("404s (with match:none + suggestions) when no atom matches, 400s empty q", async () => {
    const h = harness();
    const miss = await getJson(h, "/api/brain/bridge?q=zzzz-nothing-at-all");
    expect(miss.status).toBe(404);
    expect(miss.j.match).toBe("none");
    expect((await get(h.env, "/api/brain/bridge?q=")).status).toBe(400);
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
