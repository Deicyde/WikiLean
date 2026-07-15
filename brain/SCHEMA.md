# BRAIN schema — cells of organs, joined by synapses

> **v3 (2026-07-17) supersedes the node model below.** The graph's node is now the
> **cell** (atom); the former node types (concept / decl / ext / literature) are
> **organs** inside cells and are no longer rendered as nodes. Containers become
> **supercells**. Ontology edges between cells aggregate into **synapses**. The
> v1/v2 sections below remain the contract for the *organ layer* and the ingest
> pipeline that feeds it — cells are DERIVED from them. Design: `docs/BRAIN-V3.md`.

## v3 — the cell model (normative)

**Organ** — a particle; never a node. Kinds + id forms: `decl:<Lib>:<FQ name>`,
`Q<digits>` (concept), `xref:<db>:<value>` (page), the WikiLean slug (article),
`lit:<arxiv>#<ref>` (statement). Organs MAY repeat within a cell (the `Module`
cell holds Q18848 *and* Q125977).

**Cell** — the atom, and the node of the v3 graph.
`brain/data/cells.jsonl`:
```json
{"id": "cell:Q18848", "anchor": "Q18848", "label": "Module (mathematics)",
 "organs": [{"kind": "concept", "id": "Q18848", "bond": "exact", "prov": 3},
            {"kind": "concept", "id": "Q125977", "bond": "generalization"},
            {"kind": "decl", "id": "decl:Mathlib:Module", "bond": "exact"},
            {"kind": "page", "id": "xref:nlab:module", "bond": "xref"}],
 "supercells": ["path:Mathlib/Algebra/Module/Defs"], "f": 40409, "xy": [412.5, 88.1]}
```
`id` = `cell:<anchor>`. The anchor NAMES the atom, so it is **the `exact` concept**
(lowest QID among them), falling back to lowest QID, then the primary decl. A plain
"lowest QID" rule is WRONG and was measured wrong: it names the Euclidean-space atom
"plane" (Q17285 < Q17295) and the polygon atom "Quadrilateral", because a
rule-2-absorbed organ often carries a lower QID than the exact match (27 cells).

`supercells` = the cell's decl organs' **immediate** containers; ancestors follow
from the container tree, so the bubble view nests exactly as it does today. >1 entry
means a cell spanning modules — it renders inside each. `prov` indexes `_meta.prov`.
`xy` is the BUILD-TIME layout (the client never simulates).

**Supercell** — a module/folder (`path:…`), carrying its own organs: `field`
concepts (Q82571 "Linear algebra" → `path:Mathlib/LinearAlgebra`) and area pages
(DLMF §1.9 → `path:Mathlib/Analysis/Complex`). Cells render inside them.

**Synapse** — ONE aggregated edge per cell pair. `brain/data/synapses.jsonl`:
```json
{"src": "cell:Q18848", "dst": "cell:Q11348", "weight": 37,
 "kinds": {"depends": 31, "links": 4, "relates": 2},
 "traces": [{"kind": "depends", "src": "decl:Mathlib:Module", "dst": "decl:Mathlib:Ring",
             "evidence": {...}, "prov": 0}]}
```
Every constituent bond is retained in `traces`, capped at `TRACE_CAP` (64) per
synapse at build time and again by the shard byte budget — never silently: whatever
a cap drops is counted in `truncated` (a COUNT, not a flag), and a shard row carries
`tt` (the true total) only when its traces were actually trimmed. Weight drives
prominence and counts every bond, capped or not. **`brain/query.py --full` is the
only surface serving the untruncated set.** Supercell synapses ship traceless in
`supercells.json` (it is fetched eagerly; carrying them would treble it) — declared
in that file's `_meta.traces`, fetch via `/api/brain/neighborhood`.

`src`/`dst` are ordered lexicographically, not directionally: a synapse is an
UNDIRECTED aggregate of bonds that may run either way (A depends on B while B links
A). Direction lives on each trace, which keeps its own `src`/`dst`.

Supercells and their organs ship in `cells.jsonl`'s `_meta.supercell_organs`
(`{path: [organ, ...]}`) — the cell rows carry only the `supercells` back-reference.

### The merge function — a FUNCTION, never a transitive closure

Precisely: **rule 1 is an equivalence relation and is transitive ON PURPOSE**;
**rules 2–5 cannot chain, and that is where the blob came from.** `exact` asserts
*identity*, and identity must be transitive — it is exactly what puts `riemannZeta`
AND `completedRiemannZeta` in one atom (C7), and Q18848+Q125977 on one decl (C1).
The cost is that a single over-broad `exact` grade welds everything it names: the
survey concept "Bijection, injection and surjection" `exact`-claims
`Function.{Bijective,Injective,Surjective}`, so Bijection and Surjective function
share an atom though no edge joins them. That is a **bad grade, not a bad rule**
(see the tagger-signal section below) — `cell_review.jsonl` flags it as
`rule1-exact-weld` (5 cells today). What is structurally forbidden is chaining
through rules 2–5, which is what produced the measured 28-organ Module↔EuclideanSpace
↔plane cell and the 212-organ DLMF blob.

1. `formalizes` `match_kind == exact` **fuses both ways** (concept↔decl).
2. `generalization`/`special_case` attach a concept that has **no `exact` decl of
   its own** to its **single best** target (rank: confidence, then generalization
   before special_case, then id). One target ⇒ cannot bridge two cells.
3. `invocation`/`related` NEVER merge — they are synapses.
4. **No organ ever bridges.** Claimed by exactly one cell ⇒ organ. Claimed by >1
   ⇒ it is evidence the claimants are *related*, not that either owns it: a page
   becomes a **supercell** organ (the common module ancestor) plus a weak synapse
   between claimants; a shared arXiv statement becomes a synapse alone. Applies to
   pages AND statements — TheoremGraph matches 219 statements to decls in different
   cells, and attaching those would put one organ in two cells, breaking C4.
5. `field` match_kind / concept→container ⇒ supercell organ, **never a cell** — not
   even a lone-particle one, so "Linear algebra" resolves to
   `path:Mathlib/LinearAlgebra` and not to a stray atom. It still keeps its bonds:
   they hang off that supercell, so **a synapse endpoint may be a cell OR a
   supercell** (field concepts are hubs — dropping their bonds cost 10,801 synapses).

A transitive closure over rules 2–5 is FORBIDDEN: measured, it fuses
Module↔EuclideanSpace↔plane (28 organs) and, via coarse DLMF pages, produces a
212-organ blob. The function above yields **8,982 cells, largest 17** — no blob.

### A ballooning cell is a TAGGER signal, not a merge-rule failure

Rule 2 is deliberately kept wide ({exact, generalization, special_case}) — *Jack,
2026-07-17: "If it causes a cell to balloon to a massive size, that probably means
that the AI taggers are doing a bad job (e.g. tagging something as special_case
when it is actually a related / invocation)."* So the rule does not narrow to dodge
bad data; instead cell size is EMITTED as a diagnostic. `brain/data/cell_review.jsonl`
ranks cells by how many home-less concepts they absorbed and names the exact claim
to re-grade, shaped to drop into `catalog/data/grounding_overrides.jsonl`.

It works: of 8,982 cells only **23** flag, and they are exactly the mis-grades —
`Real.binEntropy` (the binary entropy *function*) absorbing "Information",
"Information theory" AND "Entropy"; `Module.Dual` absorbing "Duality (mathematics)".
Fix the grade, not the rule.

Two flavours, because there are two ways a grade goes wrong:
- `rule2-absorption` (18) — one decl absorbs ≥2 home-less concepts.
- `rule1-exact-weld` (5) — ≥2 concepts `exact`-claim ≥2 decls, welding them into one
  atom (rule 1 is transitive by design, above). Scoping the worklist to rule 2 left
  this — the only chaining that actually occurs — invisible.

### Strong-bond sources (organ attach)

`@[wikidata]`/`@[stacks]`/`@[kerodon]` attributes · the **tag queue**
(`/api/queue`) — the same claim as `@[wikidata]` but AI-generated: status
`rejected` ⇒ NO bond, and provenance MUST distinguish merged-into-Mathlib from
AI-queued · the WikiLean article about the object · TheoremGraph `matches` ·
single-claimant Wikidata `xref` pages.

### Weak bonds (synapses)

`depends` · `mentions` · `links` · `relates` · `cites` · TheoremGraph informal
dependency · multi-claimant page sharing.

### Layout is BUILD-TIME, and repulsion must be short-range

`brain/layout.py` runs the force sim once at build time and writes `xy`; the client
renders and never simulates. Two properties are load-bearing:

- **Deterministic** (phyllotaxis seeding, no RNG, fixed iterations) — same inputs
  reproduce the same map, so the picture can be *learned*. A map that reshuffles
  every visit cannot be.
- **Short-range repulsion** (`REPULSION_RANGE = 4k`). Textbook Fruchterman-Reingold
  repels every pair at k²/d, which is long-range: a weakly-attached node is pushed out
  until `n·k²/r` balances gravity `g·r`, i.e. `r = √(n·k²/g)`. Under the constants the
  first cut shipped (k=100, g=0.012, n=8,982) that predicts **86,516** — and the run
  put its 488 isolated cells at r≈**84,200** while the 8,494 real cells sat at
  r≈**1,985**. Fit-to-content then zooms out ~42× and the graph renders as a dot inside
  a ring. **That is the reported v2 explorer artefact** ("a ring of nodes that circle
  around the outside, and then a ton of nodes that clump in the middle"); d3-force's
  charge is long-range by default too.

  With the cutoff (and g raised to 0.02, so the formula no longer describes the
  shipped build — there is no halo equilibrium left to predict): max/median radius
  **42× → 3.40×**, p99/median **3.14×**, isolated-cell median radius 84,200 → ~2,400,
  and no two cells share a point (0 exact collisions over 8,982). Regression-tested
  (L1/L2/L3).

Synapse-less cells never enter the sim (they would either halo or pile on the
origin — both read as the clump bug). They are parked deterministically around their
supercell's centre of mass, or in a tidy outer band if they have no supercell.

### v3 acceptance datapoints (brain/test_cells.py)

C1. `cell:Q18848` contains organs Q18848, Q125977 **and** `decl:Mathlib:Module`
    (Jack's example: Module and Vector space are one atom).
C2. Euclidean space is a **separate** cell from `cell:Q18848` (it has its own
    `exact` decl `EuclideanSpace`) — the anti-chaining guarantee.
C3. No cell exceeds `CELL_MAX_ORGANS` (48) — a blob means the merge rule broke.
C4. Every organ id resolves to exactly one cell via `aliases.json`.
C5. A page claimed by >1 cell is an organ of a supercell, never of a cell.
C6. Every synapse carries ≥1 trace, and no synapse duplicates a cell pair.
C7. Tag-queue organs with status `rejected` are absent; queued (AI) organs are
    provenance-distinguishable from merged `@[wikidata]` ones.
L1. Every cell gets an `xy`.
L2. No halo: max radius stays within 8× the median, and synapse-less cells sit near
    the core rather than in orbit (the regression above).
L3. The layout is deterministic across rebuilds.

Status: **32/32 green** (`python3 brain/test_cells.py`; the nightly aborts on red).

### v3 shard acceptance (brain/test_cell_shards.py) — the artifact the client reads

`site/assets/brain/cells/` is built by `brain/build_cell_shards.py` and can drift from
the atom layer independently (it trims, embeds and re-indexes), so S1–S6 check the
shipped bytes: S1 the manifest's own documented lookup rule resolves every cell and
keys are prefix-free · S2 `aliases.json` is a function and resolves the v2 entry
points (`Q125977`→the Module atom, `Q82571`→its folder) · S3 a cell entry is
SELF-CONTAINED (one fetch = the whole card: Lean code, Wikidata description, DB
snippets, breadcrumb, synapse traces) · S4 every explorer edge indexes a shipped node,
nothing is truncated, and omitted supercell edges reconcile · S5 `supercells.json` is
a consistent tree whose leaves are cells · S6 no licensed snippet ships without its
licence. Status: **23/23 green**.

---

# (v1/v2) organ-layer schema — one graph, six node types, two edge families

> The canonical data contract for the WikiLean Math Brain. Everything in `brain/data/` is
> built to this spec. It instantiates the granularity model + anti-slop doctrine of
> `docs/research/mathdb-unification-research.json` (`synthesis.granularity_model`,
> `synthesis.join_fabric`, `synthesis.anti_slop_doctrine`) and the roadmap of
> `docs/BRAIN.md`. Where this doc contradicts those, this doc wins — it is the buildable
> subset.

## Design laws (non-negotiable)

1. **The formal graph is ground truth; the informal graph is a legible index over it;
   the two join only through verified edges.** A node earns its place by resolving
   through an existence oracle or a judged match — never by name similarity alone.
2. **Every edge carries `{kind, provenance, confidence, evidence}` and a version pin.**
   No bare adjacency. An agent (or human) must always be able to ask "why does the
   BRAIN believe this?" and get a machine-checkable answer.
3. **Locality is the rendering unit.** No artifact ever requires a client (human or AI)
   to load the whole graph. Every query is "one node + its neighborhood at grain g".
4. **Multi-to-multi everywhere.** `formalizes` edges are many-to-many by construction
   (`Module` ↔ {Q18848, Q125977}; `Affine.Simplex.insphere` ↔ {Q354337, Q683362}).
   `primary_decl` is a display hint, never identity.
5. **Altitude is an overlay, not a field.** We store the *evidence* (P31 classes,
   match_kind spread, module span, MSC/OpenAlex placement) and the renderer/agent picks
   the altitude at zoom time. Low-confidence placements route to propose-then-approve;
   they are never silently committed.
6. **Tombstone, never delete; persist durable keys only.** No TheoremGraph UUIDs or
   LeanExplore int ids as identity — those are session keys kept inside evidence
   payloads. Durable keys: QID, (library, FQ decl name), arXiv id + ref label,
   LMFDB/OEIS labels, file paths @ pin.

## Node types and stable IDs

Every node has a globally unique string id with a type prefix. IDs are addressable in
the UI (`/brain#<id>`), the API (`/api/brain/node/<id>`), and by agents.

| type | id form | example | identity source |
|---|---|---|---|
| concept | `Q<digits>` | `Q181296` | Wikidata QID (the ONLY dedup layer) |
| container | `path:<Library>[/<Dir>...]` | `path:Mathlib/CategoryTheory` | file-tree path @ snapshot pin |
| decl | `decl:<Library>:<FQ name>` | `decl:Mathlib:CommGroup` | doc-gen4 / TheoremGraph decl name; commit pin in payload |
| literature | `lit:<arxiv_id>#<ref>` (statement) · `lit:<arxiv_id>` (paper) | `lit:1707.04448#thm1.2`, `lit:1707.04448` | arXiv id + printed label; TheoremGraph UUID kept as session key in payload. The paper node `contains` its statement nodes (derived from the id prefix); an empty-ref statement id IS the paper id and doubles as it. Papers carry `f` bit 7 natively |
| object | `obj:<db>:<label>` | `obj:lmfdb:11.a2`, `obj:oeis:A000045` | external DB's own never-reused label |
| ext | `xref:<db>:<value>` | `xref:nlab:abelian+group`, `xref:stacks:0001` | external DB PAGE (v2) — id form is deliberately identical to the historical xref edge dst string, so pre-v2 `xref` edges became node-to-node with zero migration and `xref_index.json` keys are node ids |

**`ext` node payload** (v2): `{"id","type":"ext","db","label","url","snippet"?,
"snippet_license"?,"kind_hint"?,"qid"?,"f"}`. `db` MUST be a key of
`source_registry.json` `crossref_sources`. `snippet` is stored ONLY where the source
license permits (nlab/stacks/lmfdb/proofwiki/planetmath/oeis); MathWorld/DLMF/EoM/Kerodon
ext nodes are ids+titles+links only — display deep-links out. Minting policy: anchored
(xref target of a concept) + ≤1 link-hop frontier, capped per source (default 8,000/db);
full corpora stay in `catalog/data/external/`.

WikiLean articles/annotations are NOT brain nodes; an article is the `enwiki` sitelink
of its concept QID and annotations attach through the existing D1 stack. The brain
links to them, it does not duplicate them.

Node payload (JSONL, one per line, `brain/data/nodes.jsonl`):

```json
{"id": "Q181296", "type": "concept", "label": "abelian group",
 "slug": "Abelian_group",            // enwiki sitelink when present
 "altitude_evidence": {"p31": ["Q1936384"], "module_span": ["Mathlib/Algebra"],
                        "match_kinds": ["exact"], "msc": ["20K"]},
 "display": {"primary_decl": "CommGroup", "status": "formalized", "importance": "Top"}}
```

`display.*` is derived, rebuilt nightly, never authoritative. `altitude_evidence` is
the overlay of law 5.

**v2 additions to node payloads:**

- **`unit`** (concept nodes) — the atomic-unit card of one mathematical object:
  `{"qid","label","description"?,"article":{"slug","annotations"}?,
    "decls":[{"name","module","match_kind","confidence"}],"containers":["path:..."],
    "xrefs":{"<db>":[{"id","label"?,"url"}]}}`. Assembled at build time from
  formalizes/xref edges + grounding + `catalog/data/wikidata_descriptions.json`.
  `display.primary_decl` stays a hint; `unit` is the render/query surface.
- **`fa`** (container nodes, children entries, manifest roots) — subtree-AGGREGATE
  facet bits: the OR of `f` over every decl/sub-container in the contains subtree
  plus concepts whose dots render inside it (via formalizes). Lets level views
  keep a folder visible/navigable when its subtree matches an active facet filter.
  Omitted when 0. Bits 0–2 additionally propagate from a tagged decl to the
  concept(s) it formalizes (so `f=1`/`f=17` masks are satisfiable on concepts).
- **`p`** (labels.json rows of facet-bearing decls) — the decl's containment
  path (deepest container id) so clients can restrict facet queries to a
  subtree ("all `@[wikidata]` decls under `path:Mathlib/Algebra`" — the /brain
  flatten view and `/api/brain/filter` consumers).
- **`f`** (every node payload, children entry, and labels.json row) — facet bitmask:
  bit0 gold `@[wikidata]` tag · bit1 `@[stacks]` · bit2 `@[kerodon]` · bit3 any xref ·
  bit4 formalized · bit5 partial · bit6 has WikiLean article · bit7 has literature ·
  bit8 is ext · bit9 lmfdb · bit10 nlab · bit11 mathworld · bit12 proofwiki ·
  bit13 stacks(xref) · bit14 oeis · bit15 has stored snippet. Omitted when 0.

## Edge families

**Taxonomy (`contains`)** — strict single-parent containment, mechanically derived,
one tree: `path:Mathlib` → `path:Mathlib/Algebra` → … → `decl:Mathlib:CommGroup`,
plus the literature forest `lit:<arxiv_id>` → `lit:<arxiv_id>#<ref>` (id-prefix derived).
Never inferred, never LLM-written. Concepts are NOT in the tree (they attach via
ontology edges at whatever altitude their evidence supports).

**Ontology** — many-to-many, typed, weighted, non-transitive:

| kind | src → dst | native evidence | source of truth |
|---|---|---|---|
| `formalizes` | concept → decl **or container** | match_kind + confidence + method | agent grounding, @[wikidata] tags, P14534, 1000.yaml, LMFDB `mathlib=` xids |
| `mentions` | concept → decl | `{"role": "citation"}` (provenance.source `annotations`) | `decl_qid_roles_v2.json` role=citation — annotation-cited decls; EXCLUDED from all formalization-status logic |
| `depends` | decl → decl (rolled up to file/dir/module grains) | edge_type from kernel extraction | TheoremGraph formal_dependency.csv @ pin |
| `matches` | decl ↔ literature | judge + similarity + license flag | theorem_matching.csv (dual-judge) |
| `xref` | concept → external DB page; decl → Stacks/Kerodon tag | Wikidata property id; `@[stacks]`/`@[kerodon]` attribute in the mathlib4 source | P12987 LMFDB, P4215 nLab, P2812 MathWorld, P6781 ProofWiki, P7554 EoM, P7726 PlanetMath, P829 OEIS, P12888 Metamath, P11497 DLMF, P3285 MSC; `mathlib_tag_xrefs.jsonl` (harvest_mathlib_tags.py) |
| `relates` | concept ↔ concept | Wikidata P-property (P279, P361, P2579...) | wikidata_edges.jsonl |
| `links` | ext → ext; concept → concept (projected); literature paper → paper | `{"context": "statement"\|"proof"\|"body"\|"related"\|"bibliography"}`; projected form adds `{"projected": true, "via": "<db>", "src_page", "dst_page"}`; `bibliography` = src paper's bibliography cites dst paper (OpenAlex `referenced_works`, CC0, both endpoints ours) | `catalog/data/external/<db>_links.jsonl` (v2 ingest adapters); projection joins page-level links through xref anchors; `catalog/data/external/arxiv_citations.jsonl` (brain/ingest/openalex_citations.py, provenance `openalex`) |
| `cites` | concept → literature | lifted via decl (transitive join) | theoremgraph_links.json |
| `instance_of` | object → concept | invariant agreement | LMFDB/OEIS joins (future) |

The **`formalizes` → container** case is the altitude answer for field-of-study
concepts: `Q217413` (Category theory) gets `formalizes → path:Mathlib/CategoryTheory`
with `match_kind: "field"` — a zoomed-out link the leaf-level pipeline cannot express.

Edge payload (`brain/data/edges.jsonl`):

```json
{"src": "Q181296", "dst": "decl:Mathlib:CommGroup", "kind": "formalizes",
 "provenance": {"source": "rebuild_grounding", "method": "agent+oracle", "pin": "2026-07-03"},
 "confidence": "high", "evidence": {"match_kind": "exact", "verified_by": "declaration-data"}}
```

Weights on rolled-up `depends` edges carry the witnessing count and the top witnessing
decl pairs (capped), matching the existing concept-graph precedent.

## Grains and locality artifacts

Five grains: `library` → `module` → `dir` (recursive) → `file` → `decl`, plus the
concept layer floating alongside at every grain. Derived, server-side artifacts:

- `brain/data/rollup_edges.<grain>.jsonl` — `depends` aggregated to each grain with
  hub-suppression fields (raw weight + distinct-witness count) so renderers can prune.
- `wiki/public/assets/brain/<shard>.json` — per-node neighborhood shards (the
  `decl-index/` pattern): node id → {payload, 1-hop ontology edges, containment path,
  children summary}. Client fetches ONE shard per interaction. Nothing global ships.

## Acceptance datapoints (regression tests, `brain/test_acceptance.py`)

1. `Q181296` has `xref → lmfdb:group.abelian` and `formalizes → decl:Mathlib:CommGroup`.
2. `decl:Mathlib:Module` has ≥2 inbound `formalizes` (Q18848 module, Q125977 vector space).
3. `decl:Mathlib:Affine.Simplex.insphere` has ≥2 inbound `formalizes` (Q354337, Q683362).
4. `Q217413` has `formalizes → path:Mathlib/CategoryTheory` (container, not leaf) and its
   altitude evidence contains P31 = Q1936384.
5. No edge lacks `kind`/`provenance`/`confidence`. No `formalizes` dst fails the
   existence oracle at build time.

v2 datapoints (active once `catalog/data/external/` is populated):

6. `xref:lmfdb:group.abelian` is an `ext` node with a stored CC-BY-SA snippet, and
   `Q181296` reaches it via an `xref` edge whose dst is now a real node.
7. At least one `links` edge exists with `evidence.projected == true` joining two
   concept QIDs through an external DB's internal link.
8. Every `ext` node's `db` is a `source_registry.json` crossref_sources key, and ext
   nodes for no-content sources (mathworld/dlmf/eom/kerodon) carry NO `snippet`.
9. Concept nodes with ≥1 formalization carry a `unit` whose `decls` is non-empty; `f`
   bit0 nodes (gold `@[wikidata]`) are exactly the tag-harvest rows present in the graph.
10. At least one `links` edge with `evidence.context == "bibliography"` joins two
    literature PAPER nodes (`lit:<arxiv_id>`) — OpenAlex `referenced_works`
    (auto-skips when `catalog/data/external/arxiv_citations.jsonl` is absent).

## Provenance & licensing

`catalog/data/source_registry.json` remains the single source of truth; every
`provenance.source` value MUST be a key in it. Brain's own edge/node data is CC0.
TheoremGraph-derived edges carry its CC-BY-SA attribution in artifact `_meta`. arXiv
statement TEXT is never redistributed — only ids/labels/links (license-open rows may
render text, per the existing ingest gate).

## Inter-artifact contracts (fixed by the 2026-07-03 audit; builders rely on these)

- **`concept_graph_v2.json` node `xrefs`** is a VALUE MAP joined at build time from
  `wikidata_crossrefs.json` by QID (`{"lmfdb_knowl": ["group.abelian"], "mathlib":
  ["CommGroup"], ...}`), lowercase-canonical keys from the `properties` map. The
  agent-echoed `xrefs_keys` list is dead — never trust it (657 case variants + junk).
- **`decl_qid_roles_v2.json`** (`{decl: {qid: "formalization"|"citation"}}`) sits
  beside `decl_to_qid_v2.json` — the two roles must never be conflated again (the
  insphere→Circle failure class).
- **`grounding_overrides.jsonl`** (`{qid, set:{field:value}, reason}`) — curated
  point-fixes applied by build_graph_v2 AFTER loading the grounding (the grounding
  file itself is the immutable agent audit trail). Seeded with the 6 field-of-study
  `exact`→`invocation` downgrades and the History_of_trigonometry contradiction.
- **`brain/data/container_links.jsonl`** (`{qid, path, match_kind:"field",
  confidence, evidence}`) — concept→container `formalizes` edges (the Q217413 class),
  paths validated against `hierarchy.json`.
- **`brain/data/discovery_proposals.jsonl`** — agent-proposed new links/nodes; folded
  only after the deterministic verifier passes (QID exists upstream, decl passes the
  oracle). Rejected rows stay with a `rejected_reason`.
- **`depends` rollups carry per-type weight components** `{"sig": n, "def": n,
  "proof": n}` (sig bucket = sig+field+extends). Default render layer = `sig`;
  weights count DISTINCT (src,dep) decl pairs, never raw rows (18.3% dup inflation);
  witness pairs deduped.
- **Decl slogans** come from `theorem_matching.csv`'s `formal_slogan` column
  (zero-download, covers 925/1,214 grounded decls) — CC-BY-SA-4.0, attribution in
  artifact `_meta`, rendered with a source credit.

## External-source ingest contract (v2 — brain/ingest/, deterministic, no LLM)

Each adapter `brain/ingest/<db>.py` fetches its source by the sanctioned bulk path
(git mirror / dump / open API / Postgres mirror — see `docs/BRAIN-V2.md` matrix), caches
raw material under `catalog/.cache/external/<db>/` (gitignored), and emits:

```
catalog/data/external/<db>_pages.jsonl  {"db","id","title","url","snippet"?,"aliases"?,"qid"?,"kind_hint"?}
catalog/data/external/<db>_links.jsonl  {"db","src","dst","context"}     # native page ids
```

First line = `{"_meta":{"db","fetched_at","source_pin","n_pages","n_links",...}}`.
Adapters are atomic-write and fail-soft (failed fetch leaves the previous file intact),
and honor each source's rate limits / robots policy. `qid` is set ONLY from CC0 Wikidata
property values (P4215/P2812/P6781/P7554/P7726/P829/P11497/P12987), never guessed —
fuzzy anchoring is the agent team's job via `brain/proposals/` + `fold_proposals.py`.

## Build pipeline (brain/*.py, all deterministic, no LLM on the build path)

```
catalog/data/{rebuild_grounding,wikidata_universe,wikidata_crossrefs,hierarchy,...}
catalog/data/external/<db>_{pages,links}.jsonl        (v2: brain/ingest/*.py)
catalog/data/wikidata_descriptions.json               (v2: brain/ingest/wikidata_descriptions.py)
catalog/.cache/{statement_formal,formal_dependency,theorem_matching}.csv
        │
        ▼
brain/build_nodes.py      → brain/data/nodes.jsonl     (v2: + ext nodes, unit, f bits)
brain/build_edges.py      → brain/data/edges.jsonl     (every kind EXCEPT links; committed)
                          + brain/data/edges_links.jsonl (links only — ~390k rows keeps the
                            joint file over GitHub's 100MB limit; gitignored, rebuilt from
                            the committed external inputs; readers merge both files)
brain/build_rollups.py    → brain/data/rollup_edges.*.jsonl
brain/build_shards.py     → wiki/public/assets/brain/*.json (via wiki build-public)
                            (v2: + views/xref_explorer.json, f bits in labels/children)
brain/test_acceptance.py  → CI gate; the datapoints + schema invariants
```

Agent-generated links (Sonnet discovery passes) NEVER write these files directly —
they write `brain/proposals/*.jsonl`, which a deterministic verifier
(existence oracle + schema check) folds into the build inputs, mirroring the
propose-then-approve moderation stack.
