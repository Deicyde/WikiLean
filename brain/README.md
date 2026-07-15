# BRAIN — the WikiLean math knowledge graph

The BRAIN is WikiLean's informal brain behind a formal Mathlib: one hierarchical,
locality-scoped graph whose concept nodes (Wikidata QIDs) join to the formal skeleton
(Mathlib declarations, files, and modules) only through verified edges — an existence
oracle or a judged match, never name similarity. It carries six node types (concept,
container, decl, literature, object, and — v2 — `ext` external-DB pages) and two edge
families (a strict `contains` tree plus typed, weighted ontology edges: `formalizes`,
`mentions`, `depends`, `matches`, `xref`, `relates`, `cites`, and — v2 — `links`),
every edge with `{kind, provenance, confidence, evidence}` and a version pin.
`brain/SCHEMA.md` is the binding contract; agents extend the graph only through
`brain/proposals/` and a deterministic verifier, mirroring the site's
propose-then-approve moderation stack.

## Pipeline

```
catalog/data/{rebuild_grounding,wikidata_universe(+extension),wikidata_crossrefs,hierarchy,...}
catalog/data/external/<db>_{pages,links}.jsonl (v2: brain/ingest/<db>.py adapters)
catalog/data/wikidata_descriptions.json        (v2: unit.description)
catalog/.cache/{statement_formal,formal_dependency,theorem_matching}.csv
brain/proposals/*.jsonl (agent fleets)  ─┐
        │                                ▼
        │                brain/fold_proposals.py   → data/container_links.jsonl
        │                (deterministic verifier)    data/discovery_proposals.jsonl
        ▼                                            data/discovery_rejected.jsonl
brain/build_nodes.py      → brain/data/nodes.jsonl     (v2: + ext nodes, unit, f bits)
brain/build_edges.py      → brain/data/edges.jsonl     (every kind EXCEPT links)
        │                 + brain/data/edges_links.jsonl (ONLY kind=links; gitignored)
brain/build_rollups.py    → brain/data/rollup_edges.*.jsonl
brain/build_shards.py     → site/assets/brain/*.json   (per-node shards + manifest
        │                    + labels.json search index; v2: + views/xref_explorer.json
        ▼                    + aliases.json, f bits on labels/children, ext in labels)
wiki build-public         → wiki/public/assets/brain/  (wipe-then-recursive-copy; deployed)
brain/test_acceptance.py  → CI gate; datapoints P1-P9 + schema invariants
brain/test_v2.py          → fixture unit tests for the v2 external layer
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
python3 brain/build_nodes.py       # nodes.jsonl (concepts + containers + decls + ext + literature)
python3 brain/build_edges.py       # edges.jsonl (all kinds EXCEPT links) + edges_links.jsonl (links only)
python3 brain/build_rollups.py     # rollup_edges.<grain>.jsonl (streams the 1 GB CSV)
python3 brain/test_acceptance.py   # exit 0 = green (reads edges.jsonl + edges_links.jsonl)
cd brain && python3 build_shards.py && cd ..          # site/assets/brain/ (gitignored)
cd wiki && node --experimental-strip-types scripts/build-public.ts   # ship shards to wiki/public
```

All writers are atomic (tmp file + rename), so a crashed build never leaves a torn
artifact. The rollups and the shards are gitignored derived data (rebuild in
seconds–minutes); `nodes.jsonl` + `edges.jsonl` are committed — they ARE the dataset.

> **⚠️ The edge set ships as TWO files.** The v2 external layer's `links` edges
> (page→page hyperlinks + concept projections, ~393k rows / ~83 MB) pushed the joint
> `edges.jsonl` past GitHub's **100 MB per-file hard limit**, so `build_edges.py`
> splits: **`edges.jsonl` = every kind except `links`** (~42 MB, committed; its
> `_meta` line still counts the FULL edge set, and its rows are byte-compatible with
> the historical joint file minus the links rows) and **`edges_links.jsonl` = only
> `kind=="links"` rows** (same row schema, its own `_meta`). `edges_links.jsonl` is
> **GITIGNORED — never commit it** — and deterministically rebuildable with
> `python3 brain/build_edges.py` from the committed `catalog/data/external/` inputs
> (plus the `catalog/.cache` pins). Every reader (`build_shards.py`,
> `test_acceptance.py`, `query.py --full`) merges both files transparently and
> treats a missing `edges_links.jsonl` as empty.

## v2 external layer (ext nodes · links edges · units · facets)

`build_common.py` consumes `catalog/data/external/<db>_{pages,links}.jsonl` (written
by the `brain/ingest/<db>.py` adapters — never by the build) and degrades to an exact
no-op when the directory is empty: zero ext nodes, zero `links` edges.

- **ext nodes** — id `xref:<db>:<value>`, byte-identical to the historical xref edge
  dst strings, so pre-v2 `xref` edges resolve to the new nodes with zero migration.
  Minting policy (SCHEMA v2): anchored pages (the page is an xref target of a graph
  node, or its CC0 `qid` is a graph concept) plus pages ≤1 link-hop from an anchored
  page, capped per db (`BRAIN_EXT_NODE_CAP`, default 8000; anchored first, then the
  frontier by inbound-link count). Snippets are stored only where the registry's
  `ingest.snippets` license flag permits — enforced here again regardless of what the
  ingest emitted; no-content sources (mathworld/dlmf/eom/kerodon) ship ids+titles+links.
- **links edges** — page→page between minted ext nodes (`evidence.context`, deduped to
  the strongest context per pair), plus the concept projection: page A → page B where
  both anchor to graph concepts becomes concept→concept `links` with
  `evidence.{projected:true, via, src_page, dst_page}`, deduped on (src, dst, via).
  Pages whose `qid` is a graph concept also get a concept→ext `xref` edge when no
  pipeline emitted one.
- **`unit`** — every concept node carries the SCHEMA v2 atomic-unit card: decls +
  containers from `formalizes` edges, xrefs (ext label + registry `url_template`),
  article (slug + annotation summary), description from
  `catalog/data/wikidata_descriptions.json`.
- **`f` facet bitmask** — on every node payload, labels.json row, and children entry;
  bit table in SCHEMA.md (bit0 gold `@[wikidata]` … bit15 has-snippet). Omitted at 0.
- **decl `module`** — the containment altitude of a decl organ, resolved in order:
  TheoremGraph module votes → `theorem_matching.csv` → `statement_formal.csv` →
  the `@[wikidata]`/`@[stacks]` tag row's source file → **the decl-module oracle**
  (`_decl_module_oracle`). The first four only cover decls the *corpus* saw; a decl
  cited solely by a WikiLean annotation resolved nowhere and landed at the library
  ROOT (the grey "filed here" ball — 567 of them). The oracle is the last resort:
  the doc-gen4 declaration index (416k decls, incl. structure fields and
  `to_additive` output that no source scan can see — it is elaborator output, not
  syntax) with the checkout's own `.ilean` files as the floor. **Exact
  fully-qualified names only**, and only inside the decl's own library — a
  suffix guess misfiles a cell into the wrong area of mathematics (`zero_mul` →
  `Mathlib.Data.Holor`), and a cross-library hit (`And.left` → `Init.Prelude`) has
  no container tree here and would orphan the decl. Both are worse than the root.
  Measured 2026-07-17: 193/567 filed (the other 374 are names that no longer exist
  in mathlib — stale renames + hallucinated citations, an annotation-quality
  problem for `manage/decl_existence_sweep.py`, not a placement one). Fail-soft:
  no oracle ⇒ the pre-fix behaviour.
- **decl `code`** — the statement header, read from the live checkout by
  `_lean_decl_lines`, which indexes a file's declarations by **fully-qualified**
  name (tracking the `namespace` stack, honouring `_root_.`, skipping comments).
  It used to match the decl's BARE last segment with any namespace prefix and take
  the first hit, which silently showed a DIFFERENT declaration's statement: 344
  decls carried the wrong code (`AddGroup.FG` displayed `def Submonoid.FG`;
  `AddCircle.toCircle` displayed `Real.Angle.toCircle`) — shipped to readers and
  agents under the mathlib license as that decl's source. Same bar as `module`: a
  wrong fact is worse than none, so this **fails closed** — no exact match, no
  snippet. Elaborator output (structure/class fields, `to_additive` twins) is never
  textually declared and now correctly shows nothing instead of a lookalike.
  Coverage still went UP (6,301 → 6,340: −70 false positives, +109 exact hits the
  old pattern missed, incl. `public` decls). Validated against Lean's own `.ilean`
  index over 600 modules (97% of returned names confirmed verbatim; the rest are
  `private` decls, whose true names are mangled, and stale .ilean modules).
- **Env overrides**: `BRAIN_EXTERNAL_DIR` (external dir, used by tests),
  `BRAIN_EXT_NODE_CAP` (per-db mint cap), `BRAIN_MATHLIB_CHECKOUT` (checkout root —
  code snippets + the `.ilean` oracle floor), `BRAIN_DECL_ORACLE` (path to
  doc-gen4 `declaration-data.json`; default is the mathlib-search skill's cache).

New shard-level assets (`build_shards.py`): `views/xref_explorer.json` — the global
cross-ref explorer view (seeds = facet bits 0-3, plus connected concepts/decls via
formalizes/xref/links; deterministically trimmed under a 3.9 MB budget) — and
`aliases.json` (`{decls: {FQ name: [QID…]}, slugs: {slug: QID}}`) for Worker-side
unit-key resolution. Both live inside the atomic directory swap; `labels.json` gains
ext rows (searchable, `type:"ext"`, with `db`).

```bash
python3 brain/test_v2.py           # fixture unit tests for all of the above
```

**Reproducibility caveat:** `build_graph_v2.py` densifies the decl→QID map from the
LIVE `site/annotations/*.json` working tree — which is a cache of D1, the canonical
store. A rebuild therefore reflects the current annotation state, not the one the
committed dataset was built from; bit-exact reproduction of a committed
`edges.jsonl` requires the same annotation snapshot (~10% of `depends` edges shift
otherwise). This is by design (D1 is canonical); treat committed brain/data as the
pinned dataset and rebuilds as newer snapshots.

## Query surfaces

- **Local CLI (agents):** `python3 brain/query.py node|neighborhood|path|search|unit …` —
  JSON to stdout; see `.claude/skills/brain-query/SKILL.md`. v2: `search --type ext`
  finds external-DB pages; `unit <key>` resolves ANY atomic-unit member key
  (QID | `decl:Lib:Name` | bare decl name | slug | `xref:db:id`) to the owning
  concept's node payload with its `unit` card (aliases.json/xref_index.json fast
  path, full-scan fallback; exit 1 on miss); `neighborhood --full` scans
  `edges.jsonl` + `edges_links.jsonl` merged.
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
`labels.json` is the concept+container+ext search index (v2: rows and children
entries carry the `f` facet bitmask; `views/xref_explorer.json` and `aliases.json`
ship alongside — see the v2 section above).

## Acceptance

```bash
python3 brain/test_acceptance.py   # exit 0 = green (14/14 as of 2026-07-10)
```

Checks the regression datapoints of `SCHEMA.md` §Acceptance (P1–P4 name specific
nodes/edges — abelian group's LMFDB+CommGroup joins, Module's multi-QID, insphere's
two inscribed QIDs, Category theory's container-level home; P5 is the invariant
sweep: edge shape, provenance.source ∈ `catalog/data/source_registry.json`,
`formalizes` dst existence, `contains` referential integrity, node-id uniqueness).
The v2 datapoints P6–P9 (lmfdb ext node + snippet, projected concept links, ext
db/snippet licensing, unit + gold facet-bit equality) auto-SKIP with a printed note
when `catalog/data/external/` lacks the needed ingest file (P6–P8) or when
`nodes.jsonl` predates the v2 unit build (P9); skipped checks never gate exit.

## Artifact inventory (2026-07-10 v2 build — full external layer:
45,642 nodes / 520,986 edges of which 392,990 are `links`)

| artifact | what | size | committed |
|---|---|---|---|
| `brain/SCHEMA.md` | the binding data contract | 15 KB | yes |
| `brain/data/nodes.jsonl` | 45,642 nodes (2,674 concepts / 9,052 containers / 7,051 decls / 24,370 ext / 2,495 literature) | 15.3 MB | yes |
| `brain/data/edges.jsonl` | 127,996 edges — every kind EXCEPT `links` (contains 16,064 / formalizes 1,721 / mentions 10,471 / depends 85,824 / relates 2,557 / xref 3,740 / cites 4,857 / matches 2,762); `_meta` counts span BOTH edge files | 41.8 MB | yes |
| `brain/data/edges_links.jsonl` | 392,990 `links` edges (386,565 page-level + 6,425 projected) — split out for GitHub's 100 MB limit | 83.2 MB | **gitignored** — rebuild via `brain/build_edges.py` from committed `catalog/data/external/` |
| `brain/data/rollup_edges.module.jsonl` | `depends` @ library/top-module grain | 2.1 MB | **gitignored** (rebuild via build_rollups.py) |
| `brain/data/rollup_edges.{file,dir,tree}.jsonl` | `depends` @ file/dir/tree grain | 173/114/100 MB | **gitignored** (rebuild ~15 s) |
| `brain/data/hub_stats.json` | per grain, top-50 inbound-weight hubs (render pruning) | 20 KB | yes |
| `brain/data/container_links.jsonl` | 81 concept→container `formalizes` (field altitude) | 26 KB | yes |
| `brain/data/discovery_proposals.jsonl` | 153 verified discovery links | 79 KB | yes |
| `brain/data/discovery_rejected.jsonl` | rejected rows + reasons (audit trail) | 104 KB | yes |
| `brain/data/grading_disputes.jsonl` | skeptic-rejected audits of SHIPPED grounding grades — the human-review queue feeding grounding_overrides.jsonl | small | yes |
| `brain/data/community_edges.jsonl` | graduated live D1 community edges (harvest_community_edges.py) | small | yes |
| `brain/proposals/*.jsonl` | raw agent proposals + skeptic verdicts | ~820 KB | yes |
| `site/assets/brain/` | 7,809 neighborhood shards + manifest + labels.json + aliases.json + xref_index.json + views/ | 242 MB | **gitignored** (rebuild ~85 s) |

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
