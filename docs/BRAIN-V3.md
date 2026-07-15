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
into **8,982 cells** (3,900 multi-organ; 5,082 lone particles; largest 17 organs
— measured, `brain/data/cells.jsonl`). External pages stop being nodes and become
organs *inside* cells, so the entire ~49k ext-node population leaves the render
budget. Layout is precomputed at build time, so the client runs **no physics at all**.

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

Cells are **not** a transitive closure over the hierarchy relations — that chains
(measured: a naive closure fused Module↔EuclideanSpace↔plane↔3D-space into one
28-organ cell, because Module *generalizes* Vector space and EuclideanSpace is a
*special case* of it; an earlier variant produced a 212-organ blob via coarse DLMF
pages). Rules 2–5 below are a **function**, which makes that chaining structurally
impossible. Rule 1 is the deliberate exception: `exact` asserts *identity*, which
must be transitive (it is what puts both zeta decls in one atom), so one over-broad
`exact` grade welds everything it names — a bad grade, routed to `cell_review.jsonl`
rather than fixed by weakening the rule.

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

Measured on live data: **8,982 cells, largest 17 organs, no blob.** (The 8,960/16
figures were the pre-build validation experiment; the shipped builder adds tag-queue
bonds, statement organs and lone particles.)

Rule 1 is the one exception to "cannot chain", and deliberately: `exact` asserts
identity, which must be transitive — it is what puts both zeta decls in one atom.
An over-broad `exact` grade therefore welds everything it names, so
`cell_review.jsonl` flags that shape as `rule1-exact-weld`. See `brain/SCHEMA.md`.

### Known data errors this surfaces (fix via `grounding_overrides.jsonl`, not by bending the rule)

- `Q13471665` "Vector" → `Module` is labelled `generalization`; per Jack it is
  `related` (Module is not a generalization of *vector*). Same class:
  scalar / scalar multiplication.

## Identity

Cell id = `cell:<anchor>`. The anchor NAMES the atom, so it is the cell's **`exact`
concept** (lowest QID among them), falling back to lowest QID, then the primary decl
— see `brain/SCHEMA.md`, which is normative. (A plain lowest-QID rule was the first
cut and it was measured wrong: it named 27 cells after an absorbed organ, e.g. the
Euclidean-space atom came out labelled *"plane"*.)
**Breaking v2 ids/API/MCP is explicitly authorized (Jack, 2026-07-17)** — this is
early-phase. `aliases.json` maps **every organ id → its cell id**, so `/brain#Q181296`,
`/api/brain/*`, the MCP tools, and the benchmark all keep resolving after the cut.

## Artifacts

```
brain/build_cells.py  → brain/data/cells.jsonl       {id, anchor, label, organs[], supercells[], f, xy}
                        brain/data/synapses.jsonl    {src, dst, weight, kinds{}, traces[]}
                        brain/data/cell_review.jsonl  tagger-quality worklist (ballooned cells)
brain/layout.py       → the build-time force sim (imported by build_cells)
brain/test_cells.py   → acceptance C1-C7 + L1-L3
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

### Phase 1 — builder ☑  `brain/build_cells.py` + `brain/layout.py` + `brain/test_cells.py`
Measured on live data: **8,982 cells / 86,884 synapses / largest cell 17 organs**,
built in ~5s (+3min layout). Acceptance **29/29 green** (`python3 brain/test_cells.py`).
Hardened by an adversarial review (61 agents, 28 findings, 20 confirmed) — below.

All seven planned items landed (merge function; organ attach incl. the tag queue;
supercell organs; synapse aggregation with full traces; build-time layout; facet
bits; C1–C7). What the build **changed vs. the plan** — each one measured, not
speculative:

- **Anchor rule rewritten.** "Lowest QID" named 27 cells after the wrong concept —
  the Euclidean-space atom came out labelled *"plane"*. The anchor is now the
  `exact` concept. (SCHEMA updated.)
- **Rule 4 generalised from pages to ALL organs.** TheoremGraph matches 219 arXiv
  statements to decls in different cells; attaching them put one organ in two cells
  and broke C4 (`aliases.json` must be a function — every API/MCP route depends on
  it). Shared statements are now synapses. *C4 caught this, not review.*
- **Unmerged `formalizes` edges are now synapses.** 74 *concepts* (89 edges) were
  having their attach edges skipped — the concept already had an exact home — and
  silently dropped; the relationship is real and is kept.
- **`cell_review.jsonl` added** — the tagger-quality worklist that Jack's answer
  implies (below).
- **Layout repulsion made short-range** — this is what actually fixes the reported
  explorer artefact; see SCHEMA "Layout is BUILD-TIME".
- **Tag queue reads `bot/state/*.json` locally** (mirroring `bot/publish_queue.py`),
  so the carried "needs a local read path" debt is closed with no network call.

**Jack's ruling on merge width (2026-07-17)** — asked whether `special_case` should
still merge, given it absorbs 100 concepts (e.g. "Information" swallowing
*Information theory* and *Entropy*): *"I think special_case should be kept. If it
causes a cell to balloon to a massive size, that probably means that the AI taggers
are doing a bad job (e.g. tagging something as special_case when it is actually a
related / invocation)."* So the merge set is unchanged and size became a **signal**:
only 18 of 8,982 cells flag, and they are exactly the mis-grades. The rule stays
wide; the DATA gets fixed via `grounding_overrides.jsonl`.

**What the adversarial review then caught** (61 agents, 28 findings, 20 confirmed
after 2-lens refutation; every fix below is regression-tested):

- **`links` were double-counted.** `edges_links.jsonl` ships the same fact twice —
  618k raw `ext→ext` page links AND 11,540 `concept→concept` rows build_edges already
  projected from them. Consuming both gave one nLab hyperlink weight 2 and two traces.
  Blanket-skipping the projected rows was WORSE (6,415 vs 11,540 bonds): an area page
  owns no cell by rule 4, so its links cannot project through ownership — and area
  pages are exactly the hubs. Now deduplicated at the fact level: 11,600 bonds =
  6,415 raw + 5,241 projections the raw join misses, 6,299 duplicates dropped.
- **15 area pages attached to nothing** — not a cell, not a supercell — including
  DLMF §1.9, the docs' own worked example of rule 4. Now falls back to the shallowest
  supercell a claimant has: 108 homed, 13 fallback, 2 genuinely homeless (counted).
- **C7 was inverted on 12 organs**: the synthetic queue bond is appended after the
  real edges, so last-write-wins let AI-queued provenance overwrite a merged
  `@[wikidata]` one — destroying the exact distinction C7 exists to make. Provenance
  is now picked by rank, not by write order.
- **`cell_review.jsonl` was blind to rule-1 welds** — the only chaining that actually
  occurs. Now flags both shapes.
- `--attach` silently accepted unknown kinds (building a different graph) and
  KeyError'd on real-but-unranked ones; the facet mask leaked bit 8 ("is an ext page")
  onto 1,868 cells; the stats table over-counted every weak kind by counting bonds
  that `add()` then drops as intra-cell; `place_isolated` could stack cells on one
  point; a missing `edges_links.jsonl` dropped every link synapse silently.
- Doc drift the review caught in my own contract: the headline "chaining is
  structurally impossible" was false for rule 1 (see above), the anchor rule in this
  file still described the pre-fix version SCHEMA calls wrong, the layout numbers
  overstated (3.54× by L2's own metric, not 3.1×), the halo formula no longer
  reproduces from the shipped constants, and SCHEMA promised traces "capped only by
  shard byte budget" while the builder caps at 64.

**Carried debt closed:** the `Q13471665` "Vector" fix shipped as an override —
and note it needed BOTH attach options re-graded, because `generalization` outranks
`special_case`, so fixing only `Module` would have silently re-homed Vector into the
*EuclideanSpace* atom (worse). Correction to the record: `completedRiemannZeta`'s
tag was **not** rejected — it is `revise`, with Jack's own note asking to also tag
the completion — so the zeta atom legitimately holds both zeta decls, exactly as he
described (C7).

### Phase 2 — shards ☑  `brain/build_cell_shards.py` + `brain/test_cell_shards.py`
A NEW `cells/` namespace rather than a rewrite of `build_shards.py`: v2 keeps
serving `/brain` while v3 lands, and phase 5 deletes the old path. It reuses
build_shards' prefix scheme verbatim. Acceptance **23/23** (S1–S6).

```
site/assets/brain/cells/
  manifest.json    scheme + supercell roots + prov table + shard directory
  <key>.json       1,458 prefix shards, 50.1 MB   (v2: 73,318 nodes -> 333 MB)
  aliases.json     16,816 organ ids -> owning atom  (the v2->v3 compat layer)
  labels.json      8,914 atoms, `aka` = every organ label (search)
  supercells.json  the containment tree; leaves are CELLS; + rule-5 organs/synapses
  explorer.json    the COMPLETE flat graph: 8,914 cells with xy + 76,083 synapses, 2.3 MB
```

- **One fetch renders the whole card.** Organ payloads are embedded, not referenced:
  Lean docstring + code, the Wikidata description, licensed DB snippets (40,641 have
  one), article annotation counts. That is axis 3's "clicking a concept shows the Lean
  code, the article, the LMFDB knowl, the Stacks description" — in a single request.
- **The explorer ships COMPLETE.** Edges are index triples `[i, j, w]` into `nodes`,
  not `{src,dst}` id objects — ids average ~11 chars and repeat twice per edge, so
  objects cost ~4x. That is the difference between shipping all 76,083 synapses (2.3 MB)
  and silently dropping 39% to fit a byte budget. **No draw cap ⇒ the phantom-ring bug
  is structurally impossible**, since an edge can only index a node that shipped.
- **Rule 5 is now enforced, not just intended.** A `field` concept was becoming a
  supercell organ AND a lone-particle cell, so `Q82571` resolved to `cell:Q82571`
  instead of the folder. SCHEMA says "never a cell". Now `Q82571` →
  `path:Mathlib/LinearAlgebra` and `Q10380344` "manifold" → `path:Mathlib/Geometry/Manifold`
  (68 concepts).
- **…which forced supercells to become synapse endpoints.** Field concepts are hubs;
  dropping their cells first dropped 10,801 synapses (12% of the graph). Their bonds
  now hang off the module that holds them, so a synapse may legitimately land on a
  supercell — the shape v2 already drew as container rollups. Recovered 9,529.
  Supercell-level edges ship on `supercells.json`, not in the flat cell explorer, and
  the counts reconcile (S4).

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

### Phase 4 — API/MCP/bench/docs ☑
`wiki/src/brain-api.ts` reads `/assets/brain/cells/`; every route resolves any
organ id (or an atom id) through `aliases.json` before touching a shard, and
returns a **cell or a supercell** — `Q82571` resolving to
`path:Mathlib/LinearAlgebra` is not an edge case, it is rule 5, so `Atom = cell
| supercell` is threaded through every helper. `tsc --noEmit` clean;
`npm test` 619/620 (the one failure, `engine.golden.test.ts`, is the known
stale-`site/out`-fixture issue and fails identically at HEAD).

**MCP: seven tools, down from eight.** `brain_cell` replaces BOTH `brain_node`
and `brain_unit` — v3 has no particle nodes, and the unit card *became* the cell
card (a unit was QID ∘ article ∘ decls ∘ xrefs = exactly a cell's organs), so
keeping them separate would have been ceremony. Both survive as dispatch-only
aliases (not in `tools/list`, accepting either `key` or `id`) so a pre-cut agent
session does not hard-fail. serverInfo → 3.0.0; the instructions block and every
tool description now teach cells/organs/synapses.

What the work **changed vs. the plan** — each one forced by the shipped data:

- **`/api/brain/search` moved from `brain.ts` to `brain-api.ts`.** It has to read
  the CELL label index (an `aka` hit is how "Vector space" finds the Module
  atom), and `brain.ts` registers first — a route left there would have silently
  shadowed the v3 one. `searchLabels` gained `aka` matching at label rank and is
  still shared; the rest of `brain.ts` stays v2 for `brainNodeExists` (the
  community-edit node-existence oracle) until phase 5.
- **`neighborhood` lost `dir`.** A synapse is an undirected aggregate; direction
  lives on each trace. It gained `traces=0` for a compact partner list, and its
  `kinds` filter now filters the traces too (asking for `depends` must not dump
  `links`).
- **`filter` 400s the v2 `type=concept|container|ext`** rather than ignoring
  them — silently unfiltered results are worse than a loud failure. It gained
  `type=cell|supercell` (supercells enumerate by `fa`, the subtree aggregate —
  a different question, hence a separate type) and `under=path:…` via `p`.
- **The licence floor is enforced in the API, not just trusted.** `safeOrgan`
  drops a `snippet` that arrives without `snippet_license`, degrading to a deep
  link. Regression-tested.
- **`snippets` stopped fanning out** — it reads the embedded organ payloads, so
  one shard fetch answers the whole call (v2 fetched one shard per xref). Decl
  organs became a source row of their own (docstring + code), licensed
  Apache-2.0 from `source_registry.json` rather than an invented string.
- **Organs carry `bond`, not `confidence`** (confidence lived on the grounding
  edge the builder consumed), so decl ranking is exact → graded → ungraded →
  name. The API says `bond` everywhere v2 said `match_kind`.

**Bench: the accept sets are keyed on the atom layer, and this fixed real gold.**
A cell is one object, so its decl organs are interchangeable answers for its
concept organs. "Vector space" grades `generalization`, so the tag/grounding
sources accepted *nothing* for it and a model answering `Module` scored **wrong**;
its atom holds `Module`. Measured: task ids and splits unchanged (180), the
sampled gold never moves, 6 accept sets widened. **But 3 more widened from cells
`cell_review.jsonl` flags as mis-grades** — `MonoidHom` started accepting the
generic concept "Homomorphism", `Polygon` accepting "Hexagon" — so the generator
now excludes the 53 flagged suspect claims by qid (the review names claims, not
whole atoms). Gold must be at least as strict as the truth, or the benchmark
becomes an echo of the tagger it grades. `run_benchmark.py`'s allowlist named
`brain_node`/`brain_unit`, which no longer exist in `tools/list` (the
server-level catch-all was masking it) — now names the v3 seven plus the aliases.

`brain/query.py` is cell/synapse-native and is the ONLY surface serving
untruncated traces (`--full`: 682 synapses / 12 traces on Q18848 vs the shard's
199 / 6). Docs updated: `docs/BRAIN-API.md`, `/brain/api`, the `/mcp` page, the
`brain-query` skill + its `.agents/` copy.

**Data disagreements found vs. the contract** (the shipped bytes win; noted for
phase 5): a **supercell's synapses ship with no traces at all** —
`build_cell_shards.py:306` strips them for byte budget — so a supercell
neighborhood returns `w`/`kinds` with `traces: []`, and `query.py --full` is the
only way to get them. Also `truncated.syn` is a **count**, not a flag, and `tt`
appears only when traces were actually trimmed. All three are now documented
rather than papered over.

### Phase 5 — verify + land ☐
Acceptance green · browser-verify the bubble view + explorer + a synapse trace ·
perf check (the whole point: no freeze with everything on) · deploy from the
branch only after Jack reviews · then merge to `main`.

## Open items / carried debt

- ☑ **Grounding fix:** `Q13471665` "Vector" shipped in `grounding_overrides.jsonl`
  (both attach options re-graded — see Phase 1). Still open, same class: **scalar /
  scalar multiplication → `Module`** — not yet audited.
- ☑ **Tag queue local read path:** `load_tag_queue()` reads `bot/state/*.json`
  directly (mirroring `bot/publish_queue.py`), fail-soft, no network.
- **The 23 flagged cells in `cell_review.jsonl` need re-grading** (18
  `rule2-absorption` + 5 `rule1-exact-weld`) — this is the
  first real worklist for the tag-quality loop under Jack's "ballooning = bad
  tagger" rule. Worst offenders: `Real.binEntropy` absorbing Information /
  Information theory / Entropy; `Module.Dual` absorbing Duality (mathematics);
  `Configuration.ProjectivePlane` absorbing finite geometry / synthetic geometry.
  Several look like `field` concepts that should be supercell organs (rule 5).
- The upstream `catalog/build_graph_v2.py` also applies `grounding_overrides.jsonl`;
  `build_cells.py` applies it again late (idempotent) so a curated fix lands without
  a heavy upstream rebuild. If the two ever disagree, upstream wins on the next
  full rebuild.
- v2 artifacts that become dead once v3 lands: ext nodes in `nodes.jsonl` are
  still the organ layer (KEEP — cells derive from them), but the ext *rendering*
  path, `xref_explorer.json` seeding, and the ext-node shards go away.
- ☑ Nightly (`site/ops/brain-nightly.sh`) gained the atom layer: `build_cells` →
  `test_cells` → `build_cell_shards` → `test_cell_shards`, each RED aborting the
  publish exactly as the organ layer's acceptance does; it also logs the
  `cell_review` count as a signal.
