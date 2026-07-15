// Brain CELL-shard ASSETS shim shared by brain-api.test.ts and mcp.test.ts: a
// small but complete v3 fixture (cells with organs of every kind, embedded
// payloads + licensed snippets, aggregated synapses with traces, supercells
// owning a rule-5 field concept, aliases, labels with `aka`, decl-index shards)
// served through a fake ASSETS fetcher, keyed with the REAL declShardKey so
// shard resolution matches production (same approach as brain-edges.test.ts).
//
// Shapes mirror site/assets/brain/cells/ exactly — including the ones that bite:
// a supercell's synapses ship WITHOUT traces, `tt` appears only when traces were
// trimmed, and `truncated.syn` is a COUNT, not a flag.

import { declShardKey } from "../../src/decl.js";
import type { Env } from "../../src/env.js";

// atoms
export const ABELIAN_CELL = "cell:Q181296"; // concept ∘ decl ∘ pages ∘ article
export const MODULE_CELL = "cell:Q18848"; // the C1 atom: Module + Vector space are ONE
export const EMPTY_CELL = "cell:Q555000"; // concept organ only — no formalization
export const DECL_CELL = "cell:decl:Mathlib:Finset.sum_comm"; // lone-particle decl cell
export const LINALG_SUPER = "path:Mathlib/LinearAlgebra"; // owns the rule-5 field concept
export const ALGEBRA_SUPER = "path:Mathlib/Algebra";

// organs (every one of these must resolve to its atom — the compat layer)
export const ABELIAN = "Q181296";
export const MODULE_Q = "Q18848";
export const VSPACE_Q = "Q125977"; // absorbed into MODULE_CELL by the merge function
export const EMPTY_Q = "Q555000";
export const FIELD_Q = "Q82571"; // "Linear algebra" — a rule-5 field concept ⇒ SUPERCELL
export const COMM_DECL = "decl:Mathlib:CommGroup";
export const MODULE_DECL = "decl:Mathlib:Module";
export const LMFDB_PAGE = "xref:lmfdb_knowl:group.abelian"; // page organ WITH a licensed snippet
export const NLAB_PAGE = "xref:nlab:abelian+group"; // page organ, deep link only
export const MW_PAGE = "xref:mathworld:AbelianGroup"; // no-content source
export const LIT_STMT = "lit:2411.12318#2.13";
export const UNKNOWN_XREF = "xref:mathworld:NotInTheBrain"; // resolves to nothing

// aliases.json — THE compat layer. `organs` maps EVERY organ id to its atom (a
// supercell path for the rule-5 field concept); decls/slugs are convenience
// indexes. Deliberately complete: a miss here is a real miss in v3.
export const DEFAULT_ALIASES = {
  organs: {
    [ABELIAN]: ABELIAN_CELL,
    [COMM_DECL]: ABELIAN_CELL,
    Abelian_group: ABELIAN_CELL,
    [LMFDB_PAGE]: ABELIAN_CELL,
    [NLAB_PAGE]: ABELIAN_CELL,
    [MW_PAGE]: ABELIAN_CELL,
    [MODULE_Q]: MODULE_CELL,
    [VSPACE_Q]: MODULE_CELL,
    [MODULE_DECL]: MODULE_CELL,
    Vector_space: MODULE_CELL,
    Module_mathematics: MODULE_CELL,
    "xref:nlab:module": MODULE_CELL,
    [LIT_STMT]: MODULE_CELL,
    [EMPTY_Q]: EMPTY_CELL,
    "decl:Mathlib:Finset.sum_comm": DECL_CELL,
    [FIELD_Q]: LINALG_SUPER, // rule 5: a field concept is a SUPERCELL organ, never a cell
  },
  decls: {
    CommGroup: ABELIAN_CELL,
    Module: MODULE_CELL,
    "Finset.sum_comm": DECL_CELL,
  },
  slugs: {
    Abelian_group: ABELIAN_CELL,
    Vector_space: MODULE_CELL,
    Module_mathematics: MODULE_CELL,
  },
};

// labels.json — one row per ATOM. `aka` = every organ label, which is what makes
// "Vector space" find the Module atom; `p` = the atom's deepest supercell.
export const LABELS = [
  {
    id: ABELIAN_CELL, label: "Abelian group", f: 5,
    aka: ["CommGroup", "group.abelian", "abelian+group", "Abelian group"],
    p: "path:Mathlib/Algebra/Group/Defs",
  },
  {
    id: MODULE_CELL, label: "Module (mathematics)", f: 7,
    aka: ["Module", "Vector space", "module"],
    p: "path:Mathlib/Algebra/Module/Defs",
  },
  { id: EMPTY_CELL, label: "Parity conjecture" },
  { id: DECL_CELL, label: "Finset.sum_comm", f: 16, p: "path:Mathlib/Algebra/BigOperators" },
];

const DECL_MANIFEST = {
  scheme: { min_len: 2, max_len: 2, pad: "_" },
  shards: { co: 1, mo: 1 } as Record<string, number>,
};
const DECL_SHARDS: Record<string, Array<[string, string]>> = {
  co: [["CommGroup", "Mathlib.Algebra.Group.Defs"]],
  mo: [["Module", "Mathlib.Algebra.Module.Defs"]],
};

// supercells.json. Note LINALG_SUPER carries the field concept Q82571 as an
// ORGAN and has synapses of its own — and, exactly like the shipped builder,
// those synapses ship WITHOUT traces, and the entry carries NO `truncated` key
// (0 of the 9,052 shipped entries do) even when `counts.syn` exceeds the list.
// LINALG_SUPER reproduces all three shipped shapes at once: a cell partner whose
// shard holds the mirror row WITH traces (hydratable), a supercell partner
// (traceless on both ends), and a cell partner whose own list is capped past it.
export const SUPERCELLS = {
  roots: ["path:Mathlib"],
  supercells: {
    "path:Mathlib": {
      label: "Mathlib",
      fa: 31,
      children: [ALGEBRA_SUPER, LINALG_SUPER],
    },
    [ALGEBRA_SUPER]: {
      label: "Algebra",
      fa: 7,
      parent: "path:Mathlib",
      cells: [ABELIAN_CELL],
    },
    [LINALG_SUPER]: {
      label: "LinearAlgebra",
      fa: 5,
      parent: "path:Mathlib",
      cells: [MODULE_CELL],
      organs: [
        { kind: "concept", id: FIELD_Q, label: "Linear algebra", bond: "field", prov: 0 },
        {
          kind: "page", id: "xref:nlab:linear algebra", label: "linear algebra",
          db: "nlab", bond: "xref", prov: 0, url: "https://ncatlab.org/nlab/show/linear+algebra",
        },
      ],
      syn: [
        // hydratable: MODULE_CELL's shard carries the mirror row with traces
        { id: MODULE_CELL, w: 9, kinds: { depends: 4, links: 3, invocation: 1, cites: 1 }, tt: 9 },
        // supercell↔supercell: traceless on BOTH endpoints, unreachable here
        { id: ALGEBRA_SUPER, w: 2, kinds: { relates: 2 } },
        // cell partner whose own syn list was shard-capped past this supercell,
        // so no mirror exists to hydrate from
        { id: DECL_CELL, w: 1, kinds: { links: 1 } },
      ],
      // 2 more synapses exist than the list carries — and, like the real file,
      // NOTHING here says so except this total.
      counts: { syn: 5 },
    },
  },
};

interface CellSpec {
  cell: Record<string, unknown>;
  organs: Array<Record<string, unknown>>;
  syn?: Array<Record<string, unknown>>;
  truncated?: { syn: number };
  breadcrumb?: Array<{ id: string; label: string }>;
  synTotal?: number; // counts.syn when the shard capped the list
}

function fixtureCells(): Record<string, CellSpec> {
  return {
    [ABELIAN_CELL]: {
      cell: {
        id: ABELIAN_CELL, anchor: ABELIAN, label: "Abelian group",
        supercells: ["path:Mathlib/Algebra/Group/Defs"], f: 5, xy: [12.5, 8.1],
      },
      organs: [
        {
          kind: "concept", id: ABELIAN, label: "Abelian group", bond: "exact", prov: 0,
          description: "group whose operation is commutative", slug: "Abelian_group",
          article_annotations: { total: 60, formalized: 39 }, status: "formalized",
        },
        {
          kind: "decl", id: COMM_DECL, label: "CommGroup", bond: "exact", prov: 1,
          module: "Mathlib.Algebra.Group.Defs", decl_kind: "class", library: "Mathlib",
          docstring: "A commutative group is a group with commutative multiplication.",
          code: "class CommGroup (G : Type u) extends Group G, CommMonoid G",
        },
        {
          kind: "page", id: LMFDB_PAGE, label: "group.abelian", db: "lmfdb_knowl",
          bond: "xref", prov: 0, url: "https://www.lmfdb.org/knowledge/show/group.abelian",
          snippet: "An abelian group is a group whose operation is commutative.",
          snippet_license: "CC-BY-SA-4.0 (LMFDB)", qid: ABELIAN,
        },
        {
          kind: "page", id: NLAB_PAGE, label: "abelian group", db: "nlab", bond: "xref",
          prov: 0, url: "https://ncatlab.org/nlab/show/abelian+group", qid: ABELIAN,
        },
        {
          // no-content source: ids + titles + links only, never a snippet
          kind: "page", id: MW_PAGE, label: "Abelian Group", db: "mathworld", bond: "xref",
          prov: 0, url: "https://mathworld.wolfram.com/AbelianGroup.html", qid: ABELIAN,
        },
        {
          kind: "article", id: "Abelian_group", label: "Abelian group", bond: "article",
          prov: 0, annotations: { total: 60, formalized: 39 },
        },
      ],
      syn: [
        {
          id: MODULE_CELL, w: 15, kinds: { depends: 12, relates: 2, mentions: 1 },
          traces: [
            {
              kind: "depends", src: COMM_DECL, dst: MODULE_DECL, prov: 2,
              evidence: { weight: 12, w_types: { sig: 12, def: 0, proof: 0 }, witnesses: [["Module", "CommGroup"]] },
            },
            { kind: "relates", src: ABELIAN, dst: MODULE_Q, prov: 0, evidence: { property: "P279" } },
          ],
          tt: 15,
        },
      ],
      breadcrumb: [
        { id: "path:Mathlib", label: "Mathlib" },
        { id: ALGEBRA_SUPER, label: "Algebra" },
      ],
    },
    // C1: Module and Vector space are ONE atom (Mathlib has no VectorSpace —
    // Module generalizes it), so Q125977 is an organ here, not a cell.
    [MODULE_CELL]: {
      cell: {
        id: MODULE_CELL, anchor: MODULE_Q, label: "Module (mathematics)",
        // SCHEMA v3: `supercells` may hold >1 entry — a cell spanning modules
        // "renders inside each". LINALG_SUPER.cells lists this cell, so the two
        // sides agree; labels.json `p` names only the DEEPEST of them, which is
        // why `p` alone cannot answer `under=`.
        supercells: ["path:Mathlib/Algebra/Module/Defs", LINALG_SUPER],
        f: 7, xy: [40.2, 45.4],
      },
      organs: [
        {
          kind: "concept", id: MODULE_Q, label: "Module", bond: "exact", prov: 0,
          description: "algebraic structure over a ring", slug: "Module_mathematics",
          article_annotations: { total: 10 }, status: "formalized",
        },
        {
          kind: "concept", id: VSPACE_Q, label: "Vector space", bond: "generalization",
          prov: 0, description: "basic algebraic structure of linear algebra",
          slug: "Vector_space", status: "partial",
        },
        {
          kind: "decl", id: MODULE_DECL, label: "Module", bond: "exact", prov: 1,
          module: "Mathlib.Algebra.Module.Defs", decl_kind: "class", library: "Mathlib",
          code: "class Module (R M : Type*) [Semiring R] [AddCommMonoid M] extends DistribMulAction R M",
        },
        {
          kind: "page", id: "xref:nlab:module", label: "module", db: "nlab", bond: "xref",
          prov: 0, url: "https://ncatlab.org/nlab/show/module",
          snippet: "A module is a generalisation of a vector space to an arbitrary ring.",
          snippet_license: "nLab (attribution, no formal license)", qid: MODULE_Q,
        },
        {
          // a snippet that LOST its license upstream — the API must drop the text
          kind: "page", id: "xref:planetmath:VectorSpace", label: "VectorSpace",
          db: "planetmath", bond: "xref", prov: 0,
          url: "https://planetmath.org/vectorspace",
          snippet: "UNLICENSED TEXT THAT MUST NEVER BE SERVED",
        },
        { kind: "article", id: "Vector_space", label: "Vector space", bond: "article", prov: 0, annotations: { total: 22 } },
        { kind: "article", id: "Module_mathematics", label: "Module mathematics", bond: "article", prov: 0, annotations: { total: 10 } },
        {
          kind: "statement", id: LIT_STMT, label: "Proposition 2.13", bond: "matches",
          prov: 3, arxiv_id: "2411.12318", ref: "2.13", license_open: false,
        },
      ],
      syn: [
        {
          id: ABELIAN_CELL, w: 15, kinds: { depends: 12, relates: 2, mentions: 1 },
          traces: [
            {
              kind: "depends", src: MODULE_DECL, dst: COMM_DECL, prov: 2,
              evidence: { weight: 12, w_types: { sig: 12, def: 0, proof: 0 }, witnesses: [["Module", "CommGroup"]] },
            },
            { kind: "relates", src: MODULE_Q, dst: ABELIAN, prov: 0, evidence: { property: "P279" } },
          ],
          tt: 15,
        },
        // A synapse whose partner is a SUPERCELL (rule 5: field concepts are
        // hubs). A synapse is symmetric and ships on BOTH endpoints, so this row
        // is the MIRROR of LINALG_SUPER's — and, exactly like every shipped cell
        // shard, this side carries the traces the supercell side lacks. That is
        // what /neighborhood hydrates a supercell's traces from.
        {
          id: LINALG_SUPER, w: 9, kinds: { depends: 4, links: 3, invocation: 1, cites: 1 }, tt: 9,
          traces: [
            { kind: "depends", src: MODULE_DECL, dst: FIELD_Q, prov: 2, evidence: { weight: 4 } },
            { kind: "invocation", src: MODULE_Q, dst: FIELD_Q, prov: 0, evidence: { property: "P361" } },
          ],
        },
        { id: EMPTY_CELL, w: 2, kinds: { links: 2 }, traces: [{ kind: "links", src: MODULE_Q, dst: EMPTY_Q, prov: 0 }] },
      ],
      truncated: { syn: 3 }, // 3 more synapses exist than the shard carries
      synTotal: 6,
      breadcrumb: [
        { id: "path:Mathlib", label: "Mathlib" },
        { id: ALGEBRA_SUPER, label: "Algebra" },
      ],
    },
    [EMPTY_CELL]: {
      cell: { id: EMPTY_CELL, anchor: EMPTY_Q, label: "Parity conjecture", xy: [90.0, 90.0] },
      organs: [
        {
          kind: "concept", id: EMPTY_Q, label: "Parity conjecture", bond: "exact", prov: 0,
          slug: "Parity_conjecture", status: "not_formalized",
        },
      ],
    },
    // a formal-only atom: a decl nothing informal claims (5,082 of these ship)
    [DECL_CELL]: {
      cell: {
        id: DECL_CELL, anchor: "decl:Mathlib:Finset.sum_comm", label: "Finset.sum_comm",
        supercells: ["path:Mathlib/Algebra/BigOperators"], f: 16, xy: [3.0, 4.0],
      },
      organs: [
        {
          kind: "decl", id: "decl:Mathlib:Finset.sum_comm", label: "Finset.sum_comm",
          module: "Mathlib.Algebra.BigOperators.Basic", decl_kind: "theorem", library: "Mathlib",
          docstring: "Sums over a product commute.",
        },
      ],
    },
  };
}

const KEY_LEN = 6; // every fixture atom id is ≥6 chars, so fixed-length keys suffice

export interface BrainFixtureOpts {
  // undefined → serve DEFAULT_ALIASES; null → 404 aliases.json (degradation path)
  aliases?: object | null;
}

export function installBrainFixture(env: Env, opts: BrainFixtureOpts = {}): void {
  const cells = fixtureCells();
  const shards: Record<string, number> = {};
  const data: Record<string, Record<string, unknown>> = {};
  for (const [id, spec] of Object.entries(cells)) {
    const key = declShardKey(id, KEY_LEN);
    shards[key] = (shards[key] ?? 0) + 1;
    const syn = spec.syn ?? [];
    (data[key] ??= {})[id] = {
      cell: spec.cell,
      organs: spec.organs,
      syn,
      counts: { syn: spec.synTotal ?? syn.length, organs: spec.organs.length },
      ...(spec.truncated ? { truncated: spec.truncated } : {}),
      ...(spec.breadcrumb ? { breadcrumb: spec.breadcrumb } : {}),
    };
  }
  const manifest = {
    scheme: { kind: "prefix", min_len: KEY_LEN, max_len: KEY_LEN, pad: "_" },
    shards,
    prov: [
      { source: "test", method: "fixture", pin: "2026-07-15" },
      { source: "mathlib", method: "@[wikidata] attribute (mathlib4 source)", pin: "2026-07-04" },
      { source: "mathlib_deps", method: "kernel extraction", pin: "2026-07-04" },
      { source: "theoremgraph", method: "dual-judge match", pin: "2026-07-04" },
    ],
    roots: ["path:Mathlib"],
    _meta: {
      schema: "brain/SCHEMA.md#v3",
      generated_at: "2026-07-15",
      caps: { synapses_per_cell: 200, traces_per_synapse: 6 },
    },
  };
  const aliases = opts.aliases === undefined ? DEFAULT_ALIASES : opts.aliases;
  (env as unknown as { ASSETS: { fetch: (r: Request) => Promise<Response> } }).ASSETS = {
    fetch: async (req: Request) => {
      const path = new URL(req.url).pathname;
      const json = (o: unknown) => new Response(JSON.stringify(o), { status: 200 });
      if (path === "/assets/brain/cells/manifest.json") return json(manifest);
      if (path === "/assets/brain/cells/labels.json") return json(LABELS);
      if (path === "/assets/brain/cells/supercells.json") return json(SUPERCELLS);
      if (path === "/assets/brain/cells/aliases.json")
        return aliases ? json(aliases) : new Response("not found", { status: 404 });
      if (path === "/assets/decl-index/manifest.json") return json(DECL_MANIFEST);
      const dm = /^\/assets\/decl-index\/([a-z0-9_]+)\.json$/.exec(path);
      if (dm && DECL_SHARDS[dm[1]]) return json(DECL_SHARDS[dm[1]]);
      const m = /^\/assets\/brain\/cells\/([a-z0-9_]+)\.json$/.exec(path);
      if (m && data[m[1]]) return json(data[m[1]]);
      return new Response("not found", { status: 404 });
    },
  };
}
