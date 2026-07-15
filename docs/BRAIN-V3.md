# BRAIN v3 — cells, organs, supercells, synapses

> Jack's refactor (2026-07-17), wanted since the project began. Companion to
> `brain/SCHEMA.md` (the binding contract — v3 amendments live THERE).
> Predecessors: `docs/BRAIN-V2.md` (ext nodes + units), `docs/BRAIN.md`.
>
> **Thesis (Jack):** the v2 graph is too *granular*. Mathlib declarations and
> external-database entries are **subatomic particles**; the job of the Brain is
> to organize them into **atomic units** — *brain cells* — which become the
> actual low-level nodes of the graph. A cell has **organs** (a Mathlib decl, a
> Wikidata item, a Stacks tag, an LMFDB knowl, a WikiLean article, an arXiv
> statement). **Strong bonds** pull organs into a cell; **weak bonds** are the
> synapses that arrange cells into higher-order structure.

## Why (the v2 problem this solves)

v2 rendered 73,318 nodes of five heterogeneous types. That is (a) unstructured —
a pile of particles with no atomic unit, and (b) unrenderable — the browser
freezes on a ~5.7k-node force sim over ~18k edges, which forced a 4,000-edge
draw cap, which itself caused the phantom-ring bug. v3 collapses the particles
into **~8,960 cells** (of which ~1,878 are multi-organ; the rest are lone
particles). External pages stop being nodes and become organs *inside* cells, so
the entire ~49k ext-node population leaves the render budget. Layout is
precomputed at build time, so the client runs **no physics at all**.

## The model

### Organ — a particle. Never a node in v3.

| organ kind | id form | source |
|---|---|---|
| `decl` | `decl:<Lib>:<FQ name>` | Mathlib/TheoremGraph |
| `concept` | `Q<digits>` | Wikidata |
| `page` | `xref:<db>:<value>` | nLab / Stacks / LMFDB / … |
| `article` | the enwiki/WikiLean slug | D1 annotation stack |
| `statement` | `lit:<arxiv>#<ref>` | TheoremGraph |

### Cell — the atom. THE node of the v3 graph.

A cell is a set of organs that denote **one mathematical object**. Organs may
repeat within a cell (`Module` cell holds Q18848 *and* Q125977; the Riemann zeta
cell holds `riemannZeta` *and* `completedRiemannZeta` once its tag lands).

### Supercell — a module/folder. The containment altitude.

`path:Mathlib/Algebra` &c. Supercells carry organs too: **field-of-study
concepts** (Q82571 "Linear algebra" belongs to `Mathlib/LinearAlgebra`, NOT the
`Module` cell; "Category Theory" the module vs "Category" the object) and
**area-level pages** (DLMF §1.9 "Calculus of a Complex Variable" belongs to
`Mathlib/Analysis/Complex`, not to the "complex number" cell). Cells render
*inside* their supercell exactly as decls render inside folders today. A cell
spanning multiple modules (rare — only cells with several decls) renders inside
**each** of them.

### Synapse — an aggregated weak bond between two cells.

All weak bonds between cell A and cell B collapse to **one** rendered edge.
Weight = the bond count/strength (stronger bonds render more prominently);
**every constituent trace is retained** and listed in the evidence drawer.

## Bond taxonomy (normative)

### Strong — intra-cell (organ bonds)

| bond | rule |
|---|---|
| `formalizes` concept→decl, `match_kind ∈ {exact, generalization, special_case}` | **the merge function — see below** |
| `@[wikidata]` / `@[stacks]` / `@[kerodon]` Mathlib attributes | organ attach (decl ↔ page) |
| **tag-queue entries** (`/api/queue`) | organ attach — *the same kind of claim as `@[wikidata]`*, AI-generated. Status `rejected` ⇒ **no bond**. Provenance MUST distinguish merged-into-Mathlib from AI-queued. |
| WikiLean article about the object (concept `slug`) | organ attach |
| TheoremGraph `matches` (arXiv statement ↔ Mathlib theorem) | organ attach |
| Wikidata `xref` → external page (single claimant) | organ attach — **never a bridge** |

### Weak — inter-cell (synapses)

`depends` (formal dependency) · TheoremGraph informal dependency · `links`
(nLab/Stacks/Wikidata internal links) · `mentions` (a decl cited on another
cell's article) · `relates` (Wikidata P279/P361/…) · `cites` · a page claimed by
>1 cell (the coarse-page signal).

## The merge function (the load-bearing rule)

Cells are **not** a transitive closure — that chains (measured: a naive closure
fused Module↔EuclideanSpace↔plane↔3D-space into one 28-organ cell, because
Module *generalizes* Vector space and EuclideanSpace is a *special case* of it;
an earlier variant produced a 212-organ blob via coarse DLMF pages). Merging is
a **function**, which makes chaining structurally impossible:

1. **`exact` fuses both directions.** A concept fuses all of its `exact` decls
   (⇒ the zeta cell holds every exact zeta decl). A decl fuses every concept
   that `exact`-formalizes it (⇒ the `Module` case).
2. **`generalization` / `special_case` attach ONE way, ONE target.** A concept
   with **no `exact` decl of its own** — i.e. no formal home — attaches to its
   *single best* generalization/special_case target (rank: confidence, then
   `generalization` before `special_case`, then id). One target ⇒ it can never
   bridge two cells. *Rationale (Jack): "Mathlib genuinely does not contain a
   `VectorSpace` entry since it is fully generalized by `Module`, so this is
   okay."* Euclidean space **has** `EuclideanSpace`, so it keeps its own cell.
3. **`invocation` / `related` NEVER merge.** They are synapses.
4. **Pages never bridge.** A page claimed by exactly one cell attaches as an
   organ. A page claimed by >1 cell is an **area page** ⇒ organ of the supercell
   (the common module ancestor), and the claimant cells get a weak synapse.
5. **`field` match_kind / concept→container ⇒ supercell organ**, never a cell.

Measured on live data: **8,960 cells, largest 16 organs, no blob.**

### Known data errors this surfaces (fix via `grounding_overrides.jsonl`, not by bending the rule)

- `Q13471665` "Vector" → `Module` is labelled `generalization`; per Jack it is
  `related` (Module is not a generalization of *vector*). Same class:
  scalar / scalar multiplication.

## Identity

Cell id = `cell:<anchor>` where the anchor is the cell's canonical organ, chosen
deterministically: lowest QID if the cell has any concept, else its primary decl.
**Breaking v2 ids/API/MCP is explicitly authorized (Jack, 2026-07-17)** — this is
early-phase. `aliases.json` maps **every organ id → its cell id**, so `/brain#Q181296`,
`/api/brain/*`, the MCP tools, and the benchmark all keep resolving after the cut.

## Artifacts

```
brain/build_cells.py  → brain/data/cells.jsonl      {id, anchor, label, organs[], supercells[], f, xy}
                        brain/data/synapses.jsonl   {src, dst, weight, kinds[], traces[]}
brain/build_shards.py → assets/brain/ cell shards + aliases.json + views/
```

**Precomputed layout**: the force simulation moves to build time; cells ship
`{x, y}`. The client renders, never simulates — killing the multi-second freeze
AND making the map *stable* (same shape every visit, so it can be learned).

## Dropped in v3

**Unanchored frontier ext pages (~49k) leave the graph.** They contribute *zero*
concept-level connectivity today (a projection requires *both* endpoints
anchored), and they are ~90% of the node count. The corpora stay in
`catalog/data/external/`, so the second-order "co-cited by nLab page X" signal
remains computable offline as a **direct cell↔cell synapse with a trace** —
same information, zero node cost. (Jack: *"I think the graph will be strong
enough without these second-order connections."* Agreed.)

---

# Build plan (phased; branch `brain-v3-cells`)

Status legend: ☐ todo · ☑ done. Update this doc as phases land — it is the
session-crossing source of truth for the refactor.

### Phase 0 — contract ☑
`docs/BRAIN-V3.md` (this file) + `brain/SCHEMA.md` v3 section. Binding for every
later phase. Validated against live data (numbers above are measured, not
estimated).

### Phase 1 — builder ☐  `brain/build_cells.py`
Inputs: `brain/data/{nodes,edges}.jsonl` (the organ layer, unchanged) + the tag
queue + `catalog/data/external/`. Outputs `brain/data/cells.jsonl` +
`brain/data/synapses.jsonl` per the SCHEMA shapes.
1. Merge function (SCHEMA rules 1–5) — union-find, deterministic, sorted output.
2. Organ attach: pages (single-claimant), articles, `matches` statements,
   `@[…]` attributes, **tag-queue entries** (skip `rejected`; mark AI-queued vs
   merged-to-Mathlib in `bond`/`prov`).
3. Supercell organs: `field` concepts + multi-claimant (area) pages → the common
   module ancestor; multi-claimant pages ALSO emit a weak synapse between claimants.
4. Synapse aggregation: group every weak bond by (cell pair) → one row, `weight`,
   `kinds` histogram, **all traces**.
5. Precomputed layout: run the force sim HERE (not the client); write `xy` per
   cell. Seeded/deterministic so the map is stable across rebuilds.
6. `f` facet bits carry up from organs to the cell (union of organ bits).
7. `brain/test_cells.py` — acceptance C1–C7.

### Phase 2 — shards ☐  `brain/build_shards.py`
Cell shards (one fetch per cell), `aliases.json` = **every organ id → cell id**
(the compat layer that keeps `/brain#Q181296`, the API, MCP and bench resolving),
supercell children = cells, `views/` rebuilt from cells.

### Phase 3 — renderer ☐  `site/build_brain_page.py`
- Cells as nodes, **precomputed `xy`, no client sim** (delete the force block).
- Supercell bubbles: cells render inside their module(s) — a multi-module cell
  renders in each. Keeps the existing containment/bubble navigation.
- Cell card: organs grouped by kind, provenance-labelled (merged `@[wikidata]`
  vs AI-queued tag), reusing the v2 unit-card + Sources accordion.
- Synapse drawer: weight + **every trace**, reusing the v2 evidence-trace UI
  (`linkTraceHtml`/`enrichEvidence`) — already built and shipped.
- Retire the v2 explorer toggles that exist only to dodge the node count
  (`database pages` / `unlinked`) once cells make them moot.

### Phase 4 — API/MCP/bench/docs ☐
Breaking changes AUTHORIZED. `brain_cell` replaces `brain_unit`; `transfer`
resolves through cells; every route resolves any organ id via `aliases.json`.
Update `docs/BRAIN-API.md`, `/brain/api`, the `/mcp` docs page, the
`brain-query` skill (+ `.agents/` copy), `brain/query.py`, and `bench/`
(task generator keys on cells). `cd wiki && npx tsc --noEmit && npm test` green.

### Phase 5 — verify + land ☐
Acceptance green · browser-verify the bubble view + explorer + a synapse trace ·
perf check (the whole point: no freeze with everything on) · deploy from the
branch only after Jack reviews · then merge to `main`.

## Open items / carried debt

- **Grounding fix:** `Q13471665` "Vector" → `Module` is `generalization`, should
  be `related` (Jack). Route via `catalog/data/grounding_overrides.jsonl`. Same
  class: scalar / scalar multiplication → `Module`.
- The tag queue lives behind `/api/queue` (KV blob, bot/admin). The builder needs
  a local read path — mirror it into `catalog/data/` on the nightly, or fetch at
  build time (fail-soft: no queue ⇒ no queue organs, never a hard error).
- v2 artifacts that become dead once v3 lands: ext nodes in `nodes.jsonl` are
  still the organ layer (KEEP — cells derive from them), but the ext *rendering*
  path, `xref_explorer.json` seeding, and the ext-node shards go away.
- Nightly (`site/ops/brain-nightly.sh`) must gain `build_cells.py` between
  `build_edges.py` and `build_shards.py`.
