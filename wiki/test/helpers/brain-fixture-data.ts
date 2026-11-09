// The Brain fixture's data: the atoms, aliases, labels, supercells and cell
// shard entries brain-fixture.ts serves, and the parity variant's tree
// (servingTreeFor), which the D1 fixture's rows are built from.
//
// It lives apart from brain-fixture.ts so node can load it without npm ci:
// erasable TypeScript only and no imports but a type, like brain-d1-rows.ts.
// site/ops/test_brain_d1_import.py has brain-d1-rows.ts write the parity tree
// to disk (`parity-tree`), runs build_serving_db.build on it, and compares the
// builder's rows with the port's, so the rows the Worker tests serve are the
// ones the real builder makes from that tree.
import type { ServingTree } from "./brain-d1-rows.ts";

/** What servingTreeFor and fixtureData read of the fixture options. */
export interface ServingTreeOpts {
  aliases?: object | null;
  parity?: boolean;
  xrefIndex?: Record<string, string[]>;
}

// atoms
export const ABELIAN_CELL = "cell:Q181296"; // concept ∘ decl ∘ pages ∘ article
export const MODULE_CELL = "cell:Q18848"; // the C1 atom: Module + Vector space are ONE
export const EMPTY_CELL = "cell:Q555000"; // concept organ only — no formalization
export const DECL_CELL = "cell:decl:Mathlib:Finset.sum_comm"; // lone-particle decl cell
export const BASIS_CELL = "cell:Q189569"; // decl organ carries a verified `renamed_to`
export const FOURIER_DEAD_CELL = "cell:decl:Mathlib:AddCircle.fourierCoeff"; // dead-name decl cell
export const HUB_CELL = "cell:Q424242"; // dead decl organ whose final segment is the `mk` hub
export const LINALG_SUPER = "path:Mathlib/LinearAlgebra"; // owns the rule-5 field concept
export const ALGEBRA_SUPER = "path:Mathlib/Algebra";
export const TOPOLOGY_SUPER = "path:Mathlib/Topology"; // parity variant only
export const ORDER_SUPER = "path:Mathlib/Topology/Order"; // parity variant only, at depth 2

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
    // the dead cited name `Basis` and its current form `Module.Basis` are both
    // organs of the same atom; the dead one carries `renamed_to`
    "Q189569": BASIS_CELL,
    "decl:Mathlib:Basis": BASIS_CELL,
    "decl:Mathlib:Module.Basis": BASIS_CELL,
    "decl:Mathlib:AddCircle.fourierCoeff": FOURIER_DEAD_CELL,
    Q424242: HUB_CELL,
    "decl:Mathlib:Widget.mk": HUB_CELL,
    [FIELD_Q]: LINALG_SUPER, // rule 5: a field concept is a SUPERCELL organ, never a cell
  },
  decls: {
    CommGroup: ABELIAN_CELL,
    Module: MODULE_CELL,
    "Finset.sum_comm": DECL_CELL,
    Basis: BASIS_CELL,
    "Module.Basis": BASIS_CELL,
    "AddCircle.fourierCoeff": FOURIER_DEAD_CELL,
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

export interface CellSpec {
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
        {
          id: EMPTY_CELL, w: 2, kinds: { links: 2 },
          // this trace carries an explicit low confidence — the only shipped-shape
          // trace that does — so min_conf trace filtering (item 5) is exercisable
          traces: [{ kind: "links", src: MODULE_Q, dst: EMPTY_Q, prov: 0, evidence: { confidence: 0.4 } }],
        },
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
    // The `Basis` → `Module.Basis` rename, exactly as the shipped cell:Q189569
    // carries it: the dead-name decl organ holds `renamed_to`, and the current
    // name is a second decl organ of the SAME atom. Not in LABELS/SUPERCELLS —
    // it exists only to exercise batch decl_exists' rename resolution.
    [BASIS_CELL]: {
      cell: {
        id: BASIS_CELL, anchor: "Q189569", label: "Basis (linear algebra)",
        supercells: ["path:Mathlib/LinearAlgebra/Basis/Defs"], f: 20, xy: [43.8, -1.2],
      },
      organs: [
        {
          kind: "concept", id: "Q189569", label: "Basis (linear algebra)", bond: "exact", prov: 0,
          description: "set of vectors that spans a space and is linearly independent",
          slug: "Basis_(linear_algebra)", status: "formalized",
        },
        {
          kind: "decl", id: "decl:Mathlib:Basis", label: "Basis", bond: "exact", prov: 1,
          module: "Mathlib.LinearAlgebra.Basis.Defs", library: "Mathlib",
          code: "structure Basis where", renamed_to: "Module.Basis",
        },
        {
          kind: "decl", id: "decl:Mathlib:Module.Basis", label: "Module.Basis", bond: "exact", prov: 1,
          module: "Mathlib.LinearAlgebra.Basis.Defs", decl_kind: "struct", library: "Mathlib",
          code: "structure Basis where",
        },
      ],
    },
    // A dead decl organ whose final segment is a HUB ("mk", capped bucket):
    // bridgeFor must serve the same namespace_match_count + hint decl_exists
    // does. Not in LABELS — reachable only by identity (Q424242).
    [HUB_CELL]: {
      cell: { id: HUB_CELL, anchor: "Q424242", label: "Widget hub", xy: [7.0, 7.0] },
      organs: [
        {
          kind: "concept", id: "Q424242", label: "Widget hub", bond: "exact", prov: 0,
          description: "a made-up object whose decl name died into a hub segment",
        },
        {
          kind: "decl", id: "decl:Mathlib:Widget.mk", label: "Widget.mk", bond: "exact",
          prov: 1, library: "Mathlib",
        },
      ],
    },
    // A dead-name lone-particle decl cell (cell:decl:Mathlib:AddCircle.fourierCoeff
    // in the shipped build): the organ's `renamed_to` points at the current bare
    // `fourierCoeff`, which lives on a DIFFERENT atom.
    [FOURIER_DEAD_CELL]: {
      cell: {
        id: FOURIER_DEAD_CELL, anchor: "decl:Mathlib:AddCircle.fourierCoeff", label: "AddCircle.fourierCoeff",
        supercells: ["path:Mathlib/Analysis/Fourier/AddCircle"], xy: [1.0, 2.0],
      },
      organs: [
        {
          kind: "decl", id: "decl:Mathlib:AddCircle.fourierCoeff", label: "AddCircle.fourierCoeff",
          bond: "exact", prov: 1, module: "Mathlib.Analysis.Fourier.AddCircle", library: "Mathlib",
          code: "def fourierCoeff (f : AddCircle T → E) (n : ℤ) : E :=", renamed_to: "fourierCoeff",
        },
      ],
    },
  };
}

// ---- the parity variant ------------------------------------------------------
//
// The default fixture ships shards for three cells labels.json leaves out
// (BASIS_CELL, HUB_CELL, FOURIER_DEAD_CELL), which build_serving_db refuses.
// The parity variant adds their label rows (one with non-ASCII and astral
// names), a trace sidecar for LINALG_SUPER's and ALGEBRA_SUPER's synapses, two
// more hydratable synapses (one with at most 6 traces and no `tt`, one trimmed
// from 8 to 6), an xref index that repeats a node under one page, and a
// folder at depth 2 (ORDER_SUPER) whose breadcrumb has two crumbs.

const PARITY_EXTRA_LABELS = [
  { id: BASIS_CELL, label: "Basis (linear algebra)", f: 20, p: "path:Mathlib/LinearAlgebra/Basis/Defs" },
  { id: HUB_CELL, label: "Widget hub", aka: ["Größe der Gruppe", "\u{1D53D}-widget"] },
  { id: FOURIER_DEAD_CELL, label: "AddCircle.fourierCoeff", p: "path:Mathlib/Analysis/Fourier/AddCircle" },
];

const trace = (kind: string, src: string, dst: string, n: number) => ({
  kind, src, dst, prov: 0, evidence: { weight: n },
});
// ABELIAN_CELL <-> ALGEBRA_SUPER: 8 traces, the shard keeps pick_traces' first 6
const ABELIAN_ALGEBRA_TRACES = Array.from({ length: 8 }, (_, i) => trace("depends", COMM_DECL, `decl:Mathlib:Algebra.t${i}`, i));
const EMPTY_LINALG_TRACE = trace("links", EMPTY_Q, FIELD_Q, 1);

// The trace sidecar (build_cell_shards' traces/<key>.json rows): every
// supercell-involving pair, src < dst, with its true total.
export const PARITY_TRACES: Record<string, { tt: number; traces: unknown[] }> = {
  [`${ABELIAN_CELL}|${ALGEBRA_SUPER}`]: { tt: 8, traces: ABELIAN_ALGEBRA_TRACES },
  [`${MODULE_CELL}|${LINALG_SUPER}`]: {
    tt: 9,
    traces: [
      { kind: "depends", src: MODULE_DECL, dst: FIELD_Q, prov: 2, evidence: { weight: 4 } },
      { kind: "invocation", src: MODULE_Q, dst: FIELD_Q, prov: 0, evidence: { property: "P361" } },
    ],
  },
  [`${EMPTY_CELL}|${LINALG_SUPER}`]: { tt: 1, traces: [EMPTY_LINALG_TRACE] },
  // the two pairs the static store cannot hydrate (traces_unavailable there)
  [`${DECL_CELL}|${LINALG_SUPER}`]: { tt: 1, traces: [trace("links", "decl:Mathlib:Finset.sum_comm", FIELD_Q, 1)] },
  [`${ALGEBRA_SUPER}|${LINALG_SUPER}`]: {
    tt: 2,
    traces: [trace("relates", ABELIAN, FIELD_Q, 1), trace("relates", COMM_DECL, FIELD_Q, 1)],
  },
};

export const PARITY_XREF_INDEX: Record<string, string[]> = {
  [LMFDB_PAGE]: [ABELIAN, "Q11650", ABELIAN],
  "xref:nlab:module": [MODULE_Q, ABELIAN],
};

function paritySupercells(): typeof SUPERCELLS {
  const doc = structuredClone(SUPERCELLS);
  const map = doc.supercells as unknown as Record<string, { syn?: Array<Record<string, unknown>> }>;
  map[ALGEBRA_SUPER].syn = [{ id: ABELIAN_CELL, w: 8, kinds: { depends: 8 } }];
  map[LINALG_SUPER].syn!.push({ id: EMPTY_CELL, w: 1, kinds: { links: 1 } });
  // a folder at depth 2, so a breadcrumb holds two crumbs and their order shows
  const all = doc.supercells as unknown as Record<string, Record<string, unknown>>;
  (all["path:Mathlib"].children as string[]).push(TOPOLOGY_SUPER);
  all[TOPOLOGY_SUPER] = { label: "Topology", fa: 1, parent: "path:Mathlib", children: [ORDER_SUPER] };
  all[ORDER_SUPER] = { label: "Order", fa: 1, parent: TOPOLOGY_SUPER };
  return doc;
}

function parityCells(cells: Record<string, CellSpec>): void {
  cells[ABELIAN_CELL].syn!.push({
    id: ALGEBRA_SUPER, w: 8, kinds: { depends: 8 }, traces: ABELIAN_ALGEBRA_TRACES.slice(0, 6), tt: 8,
  });
  cells[EMPTY_CELL].syn = [{ id: LINALG_SUPER, w: 1, kinds: { links: 1 }, traces: [EMPTY_LINALG_TRACE] }];
}

export interface FixtureData {
  cells: Record<string, CellSpec>;
  labels: Array<Record<string, unknown>>;
  supercells: typeof SUPERCELLS;
}

export function fixtureData(opts: ServingTreeOpts): FixtureData {
  const cells = fixtureCells();
  if (!opts.parity) return { cells, labels: LABELS, supercells: SUPERCELLS };
  parityCells(cells);
  return { cells, labels: [...LABELS, ...PARITY_EXTRA_LABELS], supercells: paritySupercells() };
}

export function shardEntry(spec: CellSpec): Record<string, unknown> {
  const syn = spec.syn ?? [];
  return {
    cell: spec.cell,
    organs: spec.organs,
    syn,
    counts: { syn: spec.synTotal ?? syn.length, organs: spec.organs.length },
    ...(spec.truncated ? { truncated: spec.truncated } : {}),
    ...(spec.breadcrumb ? { breadcrumb: spec.breadcrumb } : {}),
  };
}

/** The tree build_serving_db would read for this fixture, from the same values
 *  the static release serves. */
export function servingTreeFor(opts: ServingTreeOpts): ServingTree {
  const { cells, labels, supercells } = fixtureData(opts);
  return {
    cells: Object.fromEntries(Object.entries(cells).map(([id, spec]) => [id, shardEntry(spec)])),
    aliases: (opts.aliases ?? DEFAULT_ALIASES) as Record<string, unknown>,
    labels,
    supercells: supercells as unknown as Record<string, unknown>,
    traces: opts.parity ? PARITY_TRACES : {},
    xrefIndex: opts.xrefIndex ?? (opts.parity ? PARITY_XREF_INDEX : {}),
  };
}
