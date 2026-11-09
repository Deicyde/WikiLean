// Brain CELL-shard ASSETS shim shared by brain-api.test.ts and mcp.test.ts: a
// small but complete v3 fixture (cells with organs of every kind, embedded
// payloads + licensed snippets, aggregated synapses with traces, supercells
// owning a rule-5 field concept, aliases, labels with `aka`, decl-index shards,
// and the suffix-index + premise-index sibling assets) served through a fake
// ASSETS fetcher, keyed with the REAL declShardKey so shard resolution matches
// production (same approach as brain-edges.test.ts).
//
// Shapes mirror site/assets/brain/cells/ exactly — including the ones that bite:
// a supercell's synapses ship WITHOUT traces, `tt` appears only when traces were
// trimmed, and `truncated.syn` is a COUNT, not a flag.

import { declShardKey } from "../../src/decl.js";
import type { Env } from "../../src/env.js";
import { nameParts } from "../../src/brain-name-parts.js";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { makeD1, type OnStatement } from "./d1shim.js";
import { servingRows, type Cell, type ServingRows, type ServingTree } from "./brain-d1-rows.ts";

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

// The doc-gen4 decl-index oracle. `Basis` and `AddCircle.fourierCoeff` are
// DELIBERATELY ABSENT (their prefixes `ba`/`ad` are not shards) — they are dead
// names whose current forms `Module.Basis` / `fourierCoeff` DO resolve, so a
// batch decl_exists can serve a verified rename. `Finset.sum_comm` is present so
// a unique-suffix candidate can be verified against the oracle. Pairs stay sorted
// (lookupInShard binary-searches). The manifest carries the snapshot etag the
// suffix index must match (suffixLookup's pin check).
export const FIXTURE_DECL_ETAG = 'W/"fixture-decl-snapshot"';
const DECL_MANIFEST = {
  scheme: { min_len: 2, max_len: 2, pad: "_" },
  shards: { co: 2, do: 1, ha: 2, mo: 2, fi: 1, fo: 1, se: 1 } as Record<string, number>,
  source_sha_or_etag: FIXTURE_DECL_ETAG,
};
const DECL_SHARDS: Record<string, Array<[string, string]>> = {
  co: [
    ["CommGroup", "Mathlib.Algebra.Group.Defs"],
    ["Commute.exp_right", "Mathlib.Analysis.SpecialFunctions.Exponential"],
  ],
  do: [["DoubleCoset.mk_out_eq_mul", "Mathlib.GroupTheory.DoubleCoset"]],
  ha: [
    ["HAdd", "Mathlib.Init.Prelude"],
    ["HAdd.hAdd", "Mathlib.Init.Prelude"],
  ],
  mo: [
    ["Module", "Mathlib.Algebra.Module.Defs"],
    ["Module.Basis", "Mathlib.LinearAlgebra.Basis.Defs"],
  ],
  fi: [["Finset.sum_comm", "Mathlib.Algebra.BigOperators.Basic"]],
  fo: [["fourierCoeff", "Mathlib.Analysis.Fourier.AddCircle"]],
  se: [["Semiconj.exp_right", "Mathlib.Analysis.SpecialFunctions.Exponential"]],
};

// The suffix index — final-segment → candidate FQ names over the FULL decl
// universe (wiki/scripts/build-suffix-index.ts; API tranche #18). Shards
// resolve with the same declShardFor prefix scheme, keyed on the NORMALIZED
// final segment. Both builder shapes ship here: the bare entries array
// (uncapped — the majority shape) and { total_count, entries } when storage is
// capped, with total_count the TRUE total ("extreme minority" lesson: the cap
// must never masquerade as the population). The manifest's source_sha_or_etag
// matches the decl manifest's (suffixLookup refuses a mismatched pair).
//   sum_comm      → unique, oracle-verified, BARE-ARRAY bucket (namespace-resolution)
//   mk_out_eq_mul → unique — THE mathlibmpr_063/003 shape: the agent had the
//                   exact segment, only the namespace was wrong (Doset →
//                   DoubleCoset)
//   exp_right     → 3 stored, 1 stale: `Retired.exp_right` is NOT in the
//                   oracle, so the served namespace_matches list holds 2
//   gone_lemma    → unique but FAILS oracle verification ⇒ no suggestion
//   mk            → the hub segment: 10 stored of n=6133 (real data: "mk")
//   hadd          → the normalized-collision bucket: `HAdd` and `HAdd.hAdd`
//                   share the key but their EXACT final segments differ — the
//                   consumer must disambiguate on the stored exact names
//   capped_seg    → a CAPPED bucket (total_count > entries stored): the view
//                   is incomplete, so no uniqueness claim may come from it
const SUFFIX_MANIFEST = {
  scheme: { min_len: 2, max_len: 2, pad: "_" },
  shards: { su: 1, mk: 2, ex: 1, go: 1, ha: 1, ca: 1 } as Record<string, number>,
  source_sha_or_etag: FIXTURE_DECL_ETAG,
};
const SUFFIX_SHARDS: Record<string, Record<string, unknown>> = {
  su: {
    // the bare-array (uncapped) shape — what ~all real buckets ship as
    sum_comm: [["Finset.sum_comm", "Mathlib.Algebra.BigOperators.Basic"]],
  },
  mk: {
    mk: {
      entries: [
        ["Equiv.mk", "Mathlib.Logic.Equiv.Defs"],
        ["Fin.mk", "Mathlib.Data.Fin.Basic"],
        ["Finset.mk", "Mathlib.Data.Finset.Basic"],
        ["Int.mk", "Mathlib.Data.Int.Defs"],
        ["List.mk", "Mathlib.Data.List.Basic"],
        ["Multiset.mk", "Mathlib.Data.Multiset.Basic"],
        ["Nat.mk", "Mathlib.Data.Nat.Defs"],
        ["Option.mk", "Mathlib.Data.Option.Basic"],
        ["Prod.mk", "Mathlib.Data.Prod.Basic"],
        ["Quot.mk", "Mathlib.Data.Quot"],
      ],
      total_count: 6133,
    },
    mk_out_eq_mul: { entries: [["DoubleCoset.mk_out_eq_mul", "Mathlib.GroupTheory.DoubleCoset"]], total_count: 1 },
  },
  ex: {
    exp_right: {
      entries: [
        ["Commute.exp_right", "Mathlib.Analysis.SpecialFunctions.Exponential"],
        ["Retired.exp_right", "Mathlib.Analysis.OldExponential"], // stale — not in the oracle
        ["Semiconj.exp_right", "Mathlib.Analysis.SpecialFunctions.Exponential"],
      ],
      total_count: 3,
    },
  },
  go: {
    gone_lemma: { entries: [["Deleted.gone_lemma", "Mathlib.Old.Gone"]], total_count: 1 },
  },
  ha: {
    // key-normalized collision: two DISTINCT exact segments in one bucket
    hadd: {
      entries: [
        ["HAdd", "Mathlib.Init.Prelude"],
        ["HAdd.hAdd", "Mathlib.Init.Prelude"],
      ],
      total_count: 2,
    },
  },
  ca: {
    // capped: 5 sharers exist, only 1 stored — enumeration/uniqueness forbidden
    capped_seg: { entries: [["Stored.capped_seg", "Mathlib.Some.Module"]], total_count: 5 },
  },
};

// The premise index — per-decl stored premise lists (API tranche #18; derived
// from MathNetwork/MathlibGraph, Apache-2.0). Shards resolve with the same
// declShardFor prefix scheme keyed on the SOURCE decl name; values are int
// lists (stored rank order, K≤12) into fixed 8192-name chunk tables
// names/<chunk>.json. The manifest mirrors the BUILDER's real shape
// (build-premise-index.ts): `chunk_size` (the Worker validates it — a silent
// divergence mis-decodes every int into a wrong-but-real name) and `pin` as
// the exact-revision source object (with a legacy-mtime fixture option).
//   Module                   → [CommGroup, Finset.sum_comm, fourierCoeff]
//   Module.Basis             → [Finset.sum_comm, Semiconj.exp_right]
//   DoubleCoset.mk_out_eq_mul → [CommGroup, Ghost.gone, Finset.sum_comm,
//                               fourierCoeff] — `Ghost.gone` is NOT in the
//                               oracle, so it drops and rows below the limit
//                               cutoff must backfill.
// Union of Module+Module.Basis ranks Finset.sum_comm first (multiplicity 2),
// then CommGroup (rank 0) < Semiconj.exp_right (rank 1) < fourierCoeff (rank 2).
export const FIXTURE_EDGES_MTIME = "2026-07-03T00:00:00.000Z";
export const FIXTURE_DATASET_REVISION = "8c706461fe266802197b62af324de12a3f1aa7fb";
export const FIXTURE_EDGES_SHA256 = "78b20d6311388159bdab03ddfb68d5ef5687ced629ee04a99d9880bcd043a08f";
export const FIXTURE_EDGES_URL =
  `https://huggingface.co/datasets/MathNetwork/MathlibGraph/resolve/${FIXTURE_DATASET_REVISION}/edges.csv`;
const PREMISE_NAMES: string[][] = [
  ["CommGroup", "Module", "Finset.sum_comm", "Module.Basis", "fourierCoeff", "Semiconj.exp_right", "Ghost.gone"],
];
const PREMISE_MANIFEST = {
  scheme: { min_len: 2, max_len: 2, pad: "_" },
  shards: { mo: 2, fi: 1, do: 1 } as Record<string, number>,
  chunk_size: 8192,
  chunks: 1,
  source: "MathNetwork/MathlibGraph (arXiv 2604.24797, Apache-2.0)",
  pin: {
    dataset_revision: FIXTURE_DATASET_REVISION,
    edges_sha256: FIXTURE_EDGES_SHA256,
    edges_bytes: 753711915,
    edges_url: FIXTURE_EDGES_URL,
    decl_index_etag: FIXTURE_DECL_ETAG,
  },
  filters: { explicit_only: true, per_decl_cap: 12 },
  hub_drop: ["Eq.mpr", "Eq.mp", "id"],
};
const PREMISE_SHARDS: Record<string, Record<string, number[]>> = {
  mo: {
    Module: [0, 2, 4],
    "Module.Basis": [2, 5],
  },
  fi: {
    "Finset.sum_comm": [0],
  },
  do: {
    "DoubleCoset.mk_out_eq_mul": [0, 6, 2, 4],
  },
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

const KEY_LEN = 6; // every fixture atom id is ≥6 chars, so fixed-length keys suffice

export interface BrainFixtureOpts {
  // undefined → serve DEFAULT_ALIASES; null → 404 aliases.json (degradation path)
  aliases?: object | null;
  // null → 404 the whole suffix index (decl_exists must still answer; only the
  // namespace-resolution suggestions go away)
  suffixIndex?: null;
  // null → 404 the whole premise index (brain_premises must 503, decl_exists
  // must be unaffected)
  premiseIndex?: null;
  // override the premise manifest's chunk_size (the Worker must 503 a mismatch
  // instead of mis-decoding every int)
  premiseChunkSize?: number;
  // Serve the currently deployed pre-pin manifest shape for compatibility.
  legacyPremisePin?: boolean;
  // override the suffix manifest's source_sha_or_etag (a mismatch with the decl
  // manifest must degrade namespace suggestions to none)
  suffixEtag?: string;
  // Perturb a valid manifest's identity to model selector rollover.
  releaseVariant?: string;
  // Override only the manifest claim after its identity is computed.
  manifestReleaseId?: string;
  // Set before identity calculation, including malformed profile bindings.
  releaseProfile?: string;
  releaseReplay?: unknown;
  // Mutate a complete manifest after its identity is computed, to model tampering.
  mutateReleaseManifest?: (manifest: Record<string, unknown>) => void;
  // Change only an identity-excluded audit timestamp while preserving release_id.
  releaseCreatedAt?: string;
  // Override only the selector's exact manifest claim, to model byte-identity drift.
  selectorManifestSha256?: string;
  // Serve the deployed v1 selector shape during the v2 migration.
  legacyReleaseSelector?: boolean;
  // Omit paths before computing identity, producing a self-consistent but incomplete release.
  omitReleaseArtifacts?: string[];
  // Set before computing identity; current runtime profile must reject non-null
  // changeset claims until accepted-changeset replay verification exists.
  throughChangeset?: string;
  xrefIndex?: Record<string, string[]>;
  // Shape cells/manifest.json like an offline-replay release: generated_at is
  // the sha256: generation id and the prov rows follow the live order
  // (concept-graph family digest first, attribute-lineage digest next, no
  // git-shaped pin on any mathlib/kerodon/stacks row). `leadingProv` rows are
  // prepended, to model a different first-seen row order.
  replaySnapshot?: { generatedAt: string; mathlibPin: string; leadingProv?: Array<Record<string, string>> };
  // Return a transient 404 this many times before serving xref_index.json.
  xrefIndexFailures?: number;
  // Return 404 for a declared release-relative asset (for example cells/mo____.json).
  missingAssets?: string[];
  onAssetPath?: (path: string) => void;
  // The parity variant (PARITY_* below): a tree build_serving_db accepts, so
  // one release can be served through both stores. Off, nothing changes.
  parity?: boolean;
  // List brain/data/serving-tables.json with this body, which makes the
  // release a D1 release; its logical root (the body's digest) is the dataset id.
  servingSidecar?: Record<string, unknown>;
}

interface FixtureRelease {
  assetBodies: Map<string, string>;
  manifest: Record<string, unknown>;
  releaseId: string;
  releaseHex: string;
  manifestSha256: string;
  datasetId: string | null;
}

// ---- the parity variant ------------------------------------------------------
//
// The default fixture ships shards for three cells labels.json leaves out
// (BASIS_CELL, HUB_CELL, FOURIER_DEAD_CELL), which build_serving_db refuses.
// The parity variant adds their label rows (one with non-ASCII and astral
// names), a trace sidecar for LINALG_SUPER's and ALGEBRA_SUPER's synapses, two
// more hydratable synapses (one with at most 6 traces and no `tt`, one trimmed
// from 8 to 6) and an xref index that repeats a node under one page.

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
  return doc;
}

function parityCells(cells: Record<string, CellSpec>): void {
  cells[ABELIAN_CELL].syn!.push({
    id: ALGEBRA_SUPER, w: 8, kinds: { depends: 8 }, traces: ABELIAN_ALGEBRA_TRACES.slice(0, 6), tt: 8,
  });
  cells[EMPTY_CELL].syn = [{ id: LINALG_SUPER, w: 1, kinds: { links: 1 }, traces: [EMPTY_LINALG_TRACE] }];
}

interface FixtureData {
  cells: Record<string, CellSpec>;
  labels: Array<Record<string, unknown>>;
  supercells: typeof SUPERCELLS;
}

function fixtureData(opts: BrainFixtureOpts): FixtureData {
  const cells = fixtureCells();
  if (!opts.parity) return { cells, labels: LABELS, supercells: SUPERCELLS };
  parityCells(cells);
  return { cells, labels: [...LABELS, ...PARITY_EXTRA_LABELS], supercells: paritySupercells() };
}

function shardEntry(spec: CellSpec): Record<string, unknown> {
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
export function servingTreeFor(opts: BrainFixtureOpts): ServingTree {
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

function fixtureDigest(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function canonicalJson(value: unknown): string {
  if (value === null || typeof value === "boolean" || typeof value === "string") return JSON.stringify(value);
  if (typeof value === "number" && Number.isSafeInteger(value)) return String(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  const object = value as Record<string, unknown>;
  return `{${Object.keys(object).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(object[key])}`).join(",")}}`;
}

function fixtureReleaseIdentity(manifest: Record<string, unknown>): string {
  const identityValue = { ...manifest };
  delete identityValue.release_id;
  delete identityValue.attestations;
  delete identityValue.created_at;
  const payload = `wikilean\0wikilean.release.v1\0canonical-json-v1\0${canonicalJson(identityValue)}`;
  return `sha256:${fixtureDigest(payload)}`;
}

function buildFixtureRelease(opts: BrainFixtureOpts): FixtureRelease {
  const { cells, labels, supercells } = fixtureData(opts);
  const shards: Record<string, number> = {};
  const data: Record<string, Record<string, unknown>> = {};
  for (const [id, spec] of Object.entries(cells)) {
    const key = declShardKey(id, KEY_LEN);
    shards[key] = (shards[key] ?? 0) + 1;
    (data[key] ??= {})[id] = shardEntry(spec);
  }
  const replay = opts.replaySnapshot;
  const cellsManifest = {
    scheme: { kind: "prefix", min_len: KEY_LEN, max_len: KEY_LEN, pad: "_" },
    shards,
    prov: replay
      ? [
        ...(replay.leadingProv ?? []),
        // Live order (2026-10 offline replay): the concept-graph family row
        // first, then attribute rows pinned by a different lineage digest,
        // then TheoremGraph's 40-hex dataset revision (not a Mathlib source).
        { source: "mathlib", method: "agent+oracle", pin: replay.mathlibPin },
        { source: "mathlib", method: "@[wikidata] attribute (mathlib4 source)", pin: "sha256:" + "a".repeat(64) },
        { source: "kerodon", method: "@[kerodon] attribute (mathlib4 source)", pin: "sha256:" + "a".repeat(64) },
        { source: "theoremgraph", method: "dual-judge match", pin: "5caba941dd716f17dba4880bd7173edfb1cc36d1" },
      ]
      : [
        { source: "test", method: "fixture", pin: "2026-07-15" },
        { source: "mathlib", method: "@[wikidata] attribute (mathlib4 source)", pin: "2026-07-04" },
        { source: "mathlib_deps", method: "kernel extraction", pin: "2026-07-04" },
        { source: "theoremgraph", method: "dual-judge match", pin: "2026-07-04" },
      ],
    roots: ["path:Mathlib"],
    _meta: {
      schema: "brain/SCHEMA.md#v3",
      generated_at: replay ? replay.generatedAt : "2026-07-15",
      caps: { synapses_per_cell: 200, traces_per_synapse: 6 },
    },
  };
  const aliases = opts.aliases === undefined ? DEFAULT_ALIASES : opts.aliases;
  const xrefIndex = opts.xrefIndex ?? (opts.parity ? PARITY_XREF_INDEX : {});
  const assetValues = new Map<string, unknown>([
    ["sources.json", {}],
    ["xref_index.json", xrefIndex],
    ["cells/manifest.json", cellsManifest],
    ["cells/aliases.json", aliases ?? DEFAULT_ALIASES],
    ["cells/labels.json", labels],
    ["cells/supercells.json", supercells],
    ["cells/explorer.json", {}],
    ["cells/frontier_graph.json", {}],
  ]);
  for (const [key, shard] of Object.entries(data)) assetValues.set(`cells/${key}.json`, shard);
  const assetBodies = new Map(
    [...assetValues].map(([path, value]) => [path, JSON.stringify(value)]),
  );
  const artifactBodies = new Map<string, string>(
    [...assetBodies].map(([path, bytes]) => [`site/assets/brain/${path}`, bytes]),
  );
  artifactBodies.set("site/out/brain.html", "<!doctype html><title>Fixture Brain</title>");
  if (opts.servingSidecar) artifactBodies.set("brain/data/serving-tables.json", JSON.stringify(opts.servingSidecar));
  const omitted = new Set(opts.omitReleaseArtifacts ?? []);
  const artifacts = [...artifactBodies]
    .filter(([path]) => !omitted.has(path))
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([path, bytes], index) => ({
      logical_name: `fixture_${index}`,
      path,
      media_type: path.endsWith(".json") ? "application/json" : "text/html",
      sha256: fixtureDigest(bytes),
      bytes: new TextEncoder().encode(bytes).byteLength,
      logical_format: path.endsWith(".json") ? "json" : "opaque",
      logical_root: path.endsWith(".json") ? `sha256:${fixtureDigest(bytes)}` : null,
    }));
  const variant = fixtureDigest(opts.releaseVariant ?? "default").slice(0, 16);
  const releaseManifest: Record<string, unknown> = {
    schema: "wikilean.release/v1",
    profile: opts.releaseProfile ?? "brain-current-v1",
    ...(opts.releaseReplay === undefined ? {} : { replay: opts.releaseReplay }),
    authority: {
      git_commit: "0".repeat(40),
      semantic_state_root: `sha256:${"1".repeat(64)}`,
      through_changeset: opts.throughChangeset ?? null,
    },
    source_set_root: `sha256:${"2".repeat(64)}`,
    semantic_epoch: `fixture-${variant}`,
    reducer: {
      schedule: "fixture",
      version: "1",
      git_commit: "0".repeat(40),
      configuration_sha256: "3".repeat(64),
      environment_sha256: "4".repeat(64),
    },
    artifacts,
    attestations: [
      { kind: "build", path: "attestations/build.json", sha256: "a".repeat(64), bytes: 1 },
      { kind: "validation", path: "attestations/validation.json", sha256: "b".repeat(64), bytes: 1 },
    ],
    compatible_overlay_generation_ids: [],
    created_at: opts.releaseCreatedAt ?? "2026-07-15T00:00:00Z",
  };
  const releaseId = fixtureReleaseIdentity(releaseManifest);
  releaseManifest.release_id = opts.manifestReleaseId ?? releaseId;
  opts.mutateReleaseManifest?.(releaseManifest);
  const manifestSha256 = fixtureDigest(JSON.stringify(releaseManifest));
  const sidecarBody = artifactBodies.get("brain/data/serving-tables.json");
  return {
    assetBodies,
    manifest: releaseManifest,
    releaseId,
    releaseHex: releaseId.slice("sha256:".length),
    manifestSha256,
    datasetId: sidecarBody !== undefined && !omitted.has("brain/data/serving-tables.json")
      ? `sha256:${fixtureDigest(sidecarBody)}`
      : null,
  };
}

const DEFAULT_FIXTURE_RELEASE = buildFixtureRelease({});
export const FIXTURE_RELEASE_ID = DEFAULT_FIXTURE_RELEASE.releaseId;
export const FIXTURE_RELEASE_HEX = DEFAULT_FIXTURE_RELEASE.releaseHex;

export function installBrainFixture(
  env: Env,
  opts: BrainFixtureOpts = {},
): { releaseId: string; releaseHex: string; manifestSha256: string; datasetId: string | null } {
  const fixture = buildFixtureRelease(opts);
  const { releaseId, releaseHex } = fixture;
  const manifestSha256 = opts.selectorManifestSha256 ?? fixture.manifestSha256;
  const releaseBase = `/assets/brain/releases/${opts.legacyReleaseSelector ? releaseHex : manifestSha256}`;
  const selector = opts.legacyReleaseSelector
    ? {
      schema: "wikilean.release-selector/v1",
      release_id: releaseId,
      release: releaseHex,
      manifest: `${releaseBase}/release.json`,
      audited_at: "2026-07-15T00:00:00Z",
    }
    : {
      schema: "wikilean.release-selector/v2",
      release_id: releaseId,
      release: releaseHex,
      manifest_sha256: manifestSha256,
      manifest: `${releaseBase}/release.json`,
      audited_at: "2026-07-15T00:00:00Z",
    };
  let remainingXrefFailures = opts.xrefIndexFailures ?? 0;
  const missingAssets = new Set(opts.missingAssets ?? []);
  (env as unknown as { ASSETS: { fetch: (r: Request) => Promise<Response> } }).ASSETS = {
    fetch: async (req: Request) => {
      const path = new URL(req.url).pathname;
      opts.onAssetPath?.(path);
      const json = (o: unknown) => new Response(JSON.stringify(o), { status: 200 });
      if (path === "/assets/brain/current.json") return json(selector);
      if (path === `${releaseBase}/release.json`) return json(fixture.manifest);
      if (path.startsWith(`${releaseBase}/`)) {
        const relative = path.slice(releaseBase.length + 1);
        if (
          missingAssets.has(relative) ||
          (relative === "cells/aliases.json" && opts.aliases === null) ||
          (relative === "xref_index.json" && remainingXrefFailures-- > 0)
        ) {
          return new Response("not found", { status: 404 });
        }
        const body = fixture.assetBodies.get(relative);
        if (body !== undefined) return new Response(body, { status: 200 });
      }
      if (path === "/assets/decl-index/manifest.json") return json(DECL_MANIFEST);
      const dm = /^\/assets\/decl-index\/([a-z0-9_]+)\.json$/.exec(path);
      if (dm && DECL_SHARDS[dm[1]]) return json(DECL_SHARDS[dm[1]]);
      if (opts.suffixIndex !== null) {
        if (path === "/assets/suffix-index/manifest.json")
          return json(
            opts.suffixEtag ? { ...SUFFIX_MANIFEST, source_sha_or_etag: opts.suffixEtag } : SUFFIX_MANIFEST,
          );
        const sm = /^\/assets\/suffix-index\/([a-z0-9_]+)\.json$/.exec(path);
        if (sm && SUFFIX_SHARDS[sm[1]]) return json(SUFFIX_SHARDS[sm[1]]);
      }
      if (opts.premiseIndex !== null) {
        if (path === "/assets/premise-index/manifest.json")
          return json({
            ...PREMISE_MANIFEST,
            ...(opts.premiseChunkSize ? { chunk_size: opts.premiseChunkSize } : {}),
            ...(opts.legacyPremisePin
              ? {
                  pin: {
                    edges_mtime: FIXTURE_EDGES_MTIME,
                    edges_bytes: 753711915,
                    decl_index_etag: FIXTURE_DECL_ETAG,
                  },
                }
              : {}),
          });
        const pn = /^\/assets\/premise-index\/names\/(\d+)\.json$/.exec(path);
        if (pn && PREMISE_NAMES[Number(pn[1])]) return json(PREMISE_NAMES[Number(pn[1])]);
        const pm = /^\/assets\/premise-index\/([a-z0-9_]+)\.json$/.exec(path);
        if (pm && PREMISE_SHARDS[pm[1]]) return json(PREMISE_SHARDS[pm[1]]);
      }
      return new Response("not found", { status: 404 });
    },
  };
  return { releaseId, releaseHex, manifestSha256, datasetId: fixture.datasetId };
}

// ---- the D1 fixture ---------------------------------------------------------
//
// installBrainD1Fixture serves the parity variant as a D1 release: the release
// lists the serving sidecar (so storeFor picks the D1 store) and still carries
// every static file (so the static store can serve the same release), and
// env.BRAIN_DB is an in-memory wikilean-brain created from
// wiki/migrations-brain and filled with the rows brain-d1-rows.ts builds from
// the same values, plus the dataset's names_<hex> table as the importer
// creates it.

const MIGRATIONS_BRAIN = ["0001_brain.sql", "0002_name_keys_exact.sql"].map(
  (name) => new URL(`../../migrations-brain/${name}`, import.meta.url),
);

// Every D1 column after ds, per table (site/ops/brain_d1_import.py D1_COLUMNS),
// with what the fixture writes into the columns the port leaves out.
const D1_TABLE_COLUMNS: Record<string, string[]> = {
  cell_pages: ["seq", "id", "json"],
  aliases: ["seq", "kind", "key", "owner"],
  labels: ["seq", "ord", "id", "label", "aka", "f", "p", "libs", "xy"],
  supercells: ["seq", "ord", "path", "label", "aka", "parent", "fa", "frontier", "n_cells", "n_sub", "fhist", "kids", "syn", "json"],
  name_keys: ["seq", "name_lc", "kind", "owner", "len16"],
  hot_prefixes: ["seq", "prefix", "cells", "sups", "live_sups"],
  cell_under: ["seq", "anc", "ord"],
  synapse_traces: ["seq", "pair", "json"],
  xrefs: ["seq", "page_ord", "node_ord", "page", "node"],
};
const PLACEHOLDERS: Record<string, Cell> = { libs: null, xy: null, n_sub: 0, fhist: "{}", kids: null };

export const DECOY_DATASET_ID = `sha256:${"d".repeat(64)}`;

export interface BrainD1FixtureOpts extends Omit<BrainFixtureOpts, "parity" | "servingSidecar"> {
  // build_serving_db.HOT_PREFIX_NAMES for the rows (default 1,000; 2 reaches the hot path)
  hotPrefixNames?: number;
  namesMode?: "trigram" | "parts";
  datasetState?: "importing" | "ready" | "deleting";
  schemaVersion?: number;
  // false: the release names a dataset the database does not hold
  registerDataset?: boolean;
  // a copy of every row under another ready dataset, with altered text, seeded
  // before (lower ds) or after (higher ds) the release's own
  decoy?: "before" | "after";
  // a distinct sidecar body, so a second fixture gets its own dataset id
  datasetVariant?: string;
  onStatement?: OnStatement;
  // install into this database instead of a new one (two releases, one database)
  db?: DatabaseSync;
}

function decoyRow(table: string, columns: string[], row: Cell[]): Cell[] {
  return row.map((value, i) => {
    const column = columns[i];
    if (typeof value !== "string") return value;
    if (column === "owner" || column === "node" || column === "label") return `DECOY ${value}`;
    if (column === "json" && table === "cell_pages") {
      const entry = JSON.parse(value) as { cell?: Record<string, unknown> };
      if (entry.cell) entry.cell = { ...entry.cell, label: "DECOY" };
      return JSON.stringify(entry);
    }
    if (column === "json" && table === "supercells") return JSON.stringify({ ...JSON.parse(value), label: "DECOY" });
    if (column === "json" && table === "synapse_traces") {
      return JSON.stringify({ tt: 1, traces: [{ kind: "DECOY", src: "DECOY", dst: "DECOY" }] });
    }
    return value;
  });
}

function seedDataset(
  db: DatabaseSync,
  datasetId: string,
  rows: ServingRows,
  opts: { state: string; schemaVersion: number; namesMode: "trigram" | "parts"; decoy: boolean },
): void {
  db.prepare(
    "INSERT INTO datasets (dataset_id, schema_version, tables_json, longest_name, sup_names_from, names_mode, state, imported_at) " +
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
  ).run(datasetId, opts.schemaVersion, "{}", rows.longest_name, rows.sup_names_from, opts.namesMode, opts.state,
    "2026-11-09T00:00:00Z");
  const ds = Number((db.prepare("SELECT ds FROM datasets WHERE dataset_id = ?").get(datasetId) as { ds: number }).ds);
  for (const [table, columns] of Object.entries(D1_TABLE_COLUMNS)) {
    const spec = rows.tables[table];
    const insert = db.prepare(
      `INSERT INTO ${table} (ds, ${columns.join(", ")}) VALUES (${["?", ...columns.map(() => "?")].join(", ")})`,
    );
    for (const raw of spec.rows) {
      const row = opts.decoy ? decoyRow(table, spec.columns, raw) : raw;
      const values = columns.map((column) => {
        if (table === "hot_prefixes" && column === "live_sups") return row[spec.columns.indexOf("sups")];
        const at = spec.columns.indexOf(column);
        return at >= 0 ? row[at] : PLACEHOLDERS[column];
      });
      insert.run(ds, ...values);
    }
  }
  if (opts.decoy) return;
  // the importer's statements (brain_d1_import.fts_create / fts_content)
  const hex = datasetId.slice("sha256:".length);
  const names = rows.tables.name_keys;
  if (opts.namesMode === "trigram") {
    db.exec(`CREATE VIRTUAL TABLE IF NOT EXISTS names_${hex} USING fts5(name, content='', contentless_delete=1, tokenize='trigram')`);
  } else {
    db.exec(`CREATE VIRTUAL TABLE IF NOT EXISTS names_${hex} USING fts5(parts, content='', contentless_delete=1, prefix='2 3')`);
  }
  const fill = db.prepare(`INSERT INTO names_${hex} (rowid, ${opts.namesMode === "trigram" ? "name" : "parts"}) VALUES (?, ?)`);
  const seqAt = names.columns.indexOf("seq");
  const nameAt = names.columns.indexOf("name");
  for (const row of names.rows) {
    const name = String(row[nameAt]);
    fill.run(row[seqAt], opts.namesMode === "trigram" ? name : nameParts(name));
  }
}

/** A fresh wikilean-brain stand-in: both migrations applied, no dataset. */
export function brainDatabase(): DatabaseSync {
  const db = new DatabaseSync(":memory:");
  for (const url of MIGRATIONS_BRAIN) db.exec(readFileSync(url, "utf8"));
  return db;
}

export function installBrainD1Fixture(
  env: Env,
  opts: BrainD1FixtureOpts = {},
): { releaseId: string; releaseHex: string; manifestSha256: string; datasetId: string; db: DatabaseSync; rows: ServingRows } {
  const fixtureOpts: BrainFixtureOpts = {
    ...opts,
    parity: true,
    servingSidecar: { schema: "wikilean.brain-serving-tables/v1", fixture: opts.datasetVariant ?? "parity" },
  };
  const installed = installBrainFixture(env, fixtureOpts);
  const datasetId = installed.datasetId!;
  const rows = servingRows(servingTreeFor(fixtureOpts), { hotPrefixNames: opts.hotPrefixNames });
  const db = opts.db ?? brainDatabase();
  const base = { state: "ready", schemaVersion: 1, namesMode: opts.namesMode ?? "trigram" as const, decoy: false };
  const decoyed = db.prepare("SELECT 1 FROM datasets WHERE dataset_id = ?").get(DECOY_DATASET_ID) !== undefined;
  if (opts.decoy === "before" && !decoyed) seedDataset(db, DECOY_DATASET_ID, rows, { ...base, decoy: true });
  const known = db.prepare("SELECT 1 FROM datasets WHERE dataset_id = ?").get(datasetId) !== undefined;
  if (opts.registerDataset !== false && !known) {
    seedDataset(db, datasetId, rows, {
      ...base,
      state: opts.datasetState ?? "ready",
      schemaVersion: opts.schemaVersion ?? 1,
    });
  }
  if (opts.decoy === "after" && !decoyed) seedDataset(db, DECOY_DATASET_ID, rows, { ...base, decoy: true });
  (env as unknown as { BRAIN_DB: unknown }).BRAIN_DB = makeD1(db, { onStatement: opts.onStatement });
  return { ...installed, datasetId, db, rows };
}
