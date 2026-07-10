# BRAIN schema — one graph, six node types, two edge families

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
| literature | `lit:<arxiv_id>#<ref>` | `lit:1707.04448#thm1.2` | arXiv id + printed label; TheoremGraph UUID kept as session key in payload |
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
- **`f`** (every node payload, children entry, and labels.json row) — facet bitmask:
  bit0 gold `@[wikidata]` tag · bit1 `@[stacks]` · bit2 `@[kerodon]` · bit3 any xref ·
  bit4 formalized · bit5 partial · bit6 has WikiLean article · bit7 has literature ·
  bit8 is ext · bit9 lmfdb · bit10 nlab · bit11 mathworld · bit12 proofwiki ·
  bit13 stacks(xref) · bit14 oeis · bit15 has stored snippet. Omitted when 0.

## Edge families

**Taxonomy (`contains`)** — strict single-parent containment, mechanically derived,
one tree: `path:Mathlib` → `path:Mathlib/Algebra` → … → `decl:Mathlib:CommGroup`.
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
| `links` | ext → ext; concept → concept (projected) | `{"context": "statement"\|"proof"\|"body"\|"related"}`; projected form adds `{"projected": true, "via": "<db>", "src_page", "dst_page"}` | `catalog/data/external/<db>_links.jsonl` (v2 ingest adapters); projection joins page-level links through xref anchors |
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
brain/build_edges.py      → brain/data/edges.jsonl     (v2: + links kind, projections)
brain/build_rollups.py    → brain/data/rollup_edges.*.jsonl
brain/build_shards.py     → wiki/public/assets/brain/*.json (via wiki build-public)
                            (v2: + views/xref_explorer.json, f bits in labels/children)
brain/test_acceptance.py  → CI gate; the datapoints + schema invariants
```

Agent-generated links (Sonnet discovery passes) NEVER write these files directly —
they write `brain/proposals/*.jsonl`, which a deterministic verifier
(existence oracle + schema check) folds into the build inputs, mirroring the
propose-then-approve moderation stack.
