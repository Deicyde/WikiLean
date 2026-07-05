# BRAIN — the WikiLean math knowledge graph

The BRAIN is WikiLean's informal brain behind a formal Mathlib: one hierarchical,
locality-scoped graph whose concept nodes (Wikidata QIDs) join to the formal skeleton
(Mathlib declarations, files, and modules) only through verified edges — an existence
oracle or a judged match, never name similarity. It carries five node types (concept,
container, decl, literature, object) and two edge families (a strict `contains` tree
plus typed, weighted ontology edges: `formalizes`, `mentions`, `depends`, `matches`,
`xref`, `relates`, `cites`), every edge with `{kind, provenance, confidence, evidence}`
and a version pin. `brain/SCHEMA.md` is the binding contract; agents extend the graph
only through `brain/proposals/` and a deterministic verifier, mirroring the site's
propose-then-approve moderation stack.

## Pipeline

```
catalog/data/{rebuild_grounding,wikidata_universe(+extension),wikidata_crossrefs,hierarchy,...}
catalog/.cache/{statement_formal,formal_dependency,theorem_matching}.csv
brain/proposals/*.jsonl (agent fleets)  ─┐
        │                                ▼
        │                brain/fold_proposals.py   → data/container_links.jsonl
        │                (deterministic verifier)    data/discovery_proposals.jsonl
        ▼                                            data/discovery_rejected.jsonl
brain/build_nodes.py      → brain/data/nodes.jsonl
brain/build_edges.py      → brain/data/edges.jsonl        (all ontology kinds)
brain/build_rollups.py    → brain/data/rollup_edges.*.jsonl
brain/build_shards.py     → site/assets/brain/*.json      (per-node shards + manifest
        │                                                  + labels.json search index)
        ▼
wiki build-public         → wiki/public/assets/brain/     (wipe-then-copy; deployed)
brain/test_acceptance.py  → CI gate; the 5 datapoints + schema invariants
```

Everything on the build path is deterministic — no LLM calls. Agent discovery passes
write `brain/proposals/*.jsonl` only; each shard gets an adversarial skeptic pass
(`*.verified.jsonl`), then `fold_proposals.py` re-applies hard machine checks
(hierarchy-path existence, decl oracle + checkout grep, live Wikidata entity + label
agreement) to every row regardless of verdict. Rejected rows land in
`discovery_rejected.jsonl` with reasons — the audit trail. Rows folded before their
skeptic ran carry `evidence.skeptic: "pending"` with confidence capped at medium; the
2026-07-03 build has zero pending rows.

## Rebuild (ordered)

Prerequisites on a fresh clone (all gitignored, all fetchable):

```bash
python3 catalog/fetch_math_graph.py          # statement_formal + formal_dependency CSVs (~1.1 GB)
python3 .claude/skills/mathlib-search/mathlib_search.py --live decl Nat.add_comm  # warms the decl oracle cache
# plus a mathlib4 checkout (default /Users/jack/Desktop/LEAN/mathlib4; override
# with BRAIN_MATHLIB_CHECKOUT) — build_graph_v2 and fold_proposals FAIL HARD
# when the oracle/checkout are missing rather than silently dropping data.
```

```bash
cd /Users/jack/Desktop/LEAN/WikiLean
python3 brain/fold_proposals.py    # only when proposals/ changed (network: Wikidata)
python3 catalog/build_graph_v2.py --grounding catalog/data/rebuild_grounding.json
python3 brain/build_nodes.py       # nodes.jsonl (concepts + containers + decls + literature)
python3 brain/build_edges.py       # edges.jsonl (all edge kinds)
python3 brain/build_rollups.py     # rollup_edges.<grain>.jsonl (streams the 1 GB CSV)
python3 brain/test_acceptance.py   # exit 0 = green
cd brain && python3 build_shards.py && cd ..          # site/assets/brain/ (gitignored)
cd wiki && node --experimental-strip-types scripts/build-public.ts   # ship shards to wiki/public
```

All writers are atomic (tmp file + rename), so a crashed build never leaves a torn
artifact. The file/dir rollups and the shards are gitignored derived data (rebuild in
seconds); `nodes.jsonl`/`edges.jsonl`/the module rollup are committed — they ARE the
dataset.

**Reproducibility caveat:** `build_graph_v2.py` densifies the decl→QID map from the
LIVE `site/annotations/*.json` working tree — which is a cache of D1, the canonical
store. A rebuild therefore reflects the current annotation state, not the one the
committed dataset was built from; bit-exact reproduction of a committed
`edges.jsonl` requires the same annotation snapshot (~10% of `depends` edges shift
otherwise). This is by design (D1 is canonical); treat committed brain/data as the
pinned dataset and rebuilds as newer snapshots.

## Query surfaces

- **Local CLI (agents):** `python3 brain/query.py node|neighborhood|path|search …` —
  JSON to stdout; see `.claude/skills/brain-query/SKILL.md`.
- **Live API:** `GET /api/brain/node?id=<id>` and `GET /api/brain/search?q=…`
  (wiki/src/brain.ts, served from the deployed shards).
- **UI:** `/brain` (site/build_brain_page.py → brain.html) — Miller-column drill-down
  through the containment tree, per-node panel with every edge's provenance one click
  away, layer toggles per edge family, label search. One shard fetch per interaction;
  the whole graph never ships.

## Shards

`build_shards.py` mirrors `wiki/scripts/build-decl-index.ts`'s longest-prefix scheme
(normalize to `[a-z0-9_]`, start at 2-char keys, split shards over 150 KB): a client
loads `manifest.json` once, then any node is one fetch away. Each entry carries the
node payload, up to 200 ontology edges per direction (ranked, `truncated` flagged),
the containment breadcrumb, a children summary (first 50 + count), and for containers
the strongest `depends` rollups at module/dir grain. Provenance dicts are factored
into a manifest-level `prov` table. `manifest.roots` is the /brain boot payload;
`labels.json` is the concept+container search index.

## Acceptance

```bash
python3 brain/test_acceptance.py   # exit 0 = green (10/10 as of 2026-07-03)
```

Checks the 5 regression datapoints of `SCHEMA.md` §Acceptance (P1–P4 name specific
nodes/edges — abelian group's LMFDB+CommGroup joins, Module's multi-QID, insphere's
two inscribed QIDs, Category theory's container-level home; P5 is the invariant
sweep: edge shape, provenance.source ∈ `catalog/data/source_registry.json`,
`formalizes` dst existence, `contains` referential integrity, node-id uniqueness).

## Artifact inventory (2026-07-03 build)

| artifact | what | size | committed |
|---|---|---|---|
| `brain/SCHEMA.md` | the binding data contract | 11 KB | yes |
| `brain/data/nodes.jsonl` | 21,240 nodes (2,651 concepts / 9,052 containers / 7,042 decls / 2,495 literature) | 5.2 MB | yes |
| `brain/data/edges.jsonl` | 126,334 edges, all kinds | 40 MB | yes |
| `brain/data/rollup_edges.module.jsonl` | `depends` @ library/top-module grain | 2.1 MB | yes |
| `brain/data/rollup_edges.{file,dir}.jsonl` | `depends` @ file/dir grain | 173/114 MB | **gitignored** (rebuild ~15 s) |
| `brain/data/hub_stats.json` | per grain, top-50 inbound-weight hubs (render pruning) | 16 KB | yes |
| `brain/data/container_links.jsonl` | 81 concept→container `formalizes` (field altitude) | 25 KB | yes |
| `brain/data/discovery_proposals.jsonl` | 153 verified discovery links | 90 KB | yes |
| `brain/data/discovery_rejected.jsonl` | rejected rows + reasons (audit trail) | 60 KB | yes |
| `brain/data/grading_disputes.jsonl` | skeptic-rejected audits of SHIPPED grounding grades — the human-review queue feeding grounding_overrides.jsonl | small | yes |
| `brain/proposals/*.jsonl` | raw agent proposals + skeptic verdicts | ~700 KB | yes |
| `site/assets/brain/` | 2,165 neighborhood shards + manifest + labels.json | 65 MB | **gitignored** (rebuild ~6 s) |

## Network-structure calibration (arXiv 2604.24797)

"The Network Structure of Mathlib" (Li, Peng, Severini, Shafto 2026) measured the
biases the BRAIN must correct for, and three of its findings are wired in:

- **74.2% of dependency edges are compiler-synthesized; 43.9% are proof-only** —
  the graph records the process of proving, not mathematical content. The BRAIN's
  default render layer is therefore `w_types.sig` (statement-level dependencies,
  the closest analogue of the paper's "explicit subgraph ≈ human-intended").
- **Centrality captures infrastructure, not relevance** (Eq.refl ranks #2 by
  in-degree; the CRT isn't in the top 100). Tree-grain rollup rows carry a
  **`lift`** field: observed sig weight ÷ configuration-model expectation at that
  depth. Hub↔hub volume gets lift ≈ 1; genuine entanglement gets lift ≫ 1
  (measured on our data: FieldTheory↔ModelTheory 28.7×, MeasureTheory↔Probability
  22.9×, Probability↔InformationTheory 20.5×). /brain's "de-hub" toggle ranks and
  fades edges by lift; the edge card shows it.
- **Folders diverge from logical communities** (NMI 0.34, 50.9% cross-namespace
  edges). /brain computes greedy-modularity communities over each visible level's
  sibling graph (lift-weighted, client-side — locality-true) and colors bubble
  outlines by community, so the divergence between the containment tree and the
  logical structure is visible at every zoom level.

Their released dataset (MathNetwork/MathlibGraph, Apache-2.0) is now ingested:
`catalog/fetch_mathlib_graph.py` → `catalog/.cache/mathnetwork/edges.csv`
(10.9M edges, 29.8% explicit — confirming the paper's 74.2%-synthesized figure),
joined by decl name onto the pinned TheoremGraph substrate in
`build_rollups.py`. Tree-grain rows carry **`w_types.exp`** — the count of
distinct EXPLICIT (source-visible) decl pairs, the paper's closest proxy for
"a human deliberately cited this" (125,359 tree edges carry it). Unmatched
names (~9% of explicit rows, the fresher snapshot's drift) are counted and
skipped, never guessed. Full snapshot adoption (replacing TheoremGraph as the
substrate) remains open — it would need the informal-matching layer re-keyed.

## Provenance & licensing

`catalog/data/source_registry.json` is the single source of truth: every edge's
`provenance.source` must be a key there (enforced by the acceptance gate). Summary:

- **BRAIN's own node/edge data is CC0-1.0** — we store identifiers and link facts,
  which are facts. Sources `rebuild_grounding`, `brain_container_links`,
  `brain_discovery` (registry section `brain_sources`).
- **Mathlib-derived structure** (decl names, file tree, dependencies) — Apache-2.0;
  derived link facts CC0.
- **Wikidata** (QIDs, `relates` properties, all `xref` identifiers) — CC0-1.0. Each
  xref target's own content keeps its own license (see `crossref_sources`; MathWorld
  and DLMF are link-only).
- **TheoremGraph** (`matches` edges, decl slogans, `depends` CSV @ pin) —
  CC-BY-SA-4.0: attribution rides in artifact `_meta` and is rendered with a source
  credit. arXiv statement TEXT is never redistributed — only ids/labels/links
  (license-open rows may render text, per the existing ingest gate).
- **No TheoremGraph UUIDs / LeanExplore int ids as identity** — session keys live
  inside evidence payloads only; durable keys are QID, (library, FQ decl name),
  arXiv id + ref label, LMFDB/OEIS labels, and file paths @ pin.
