# Session handoff — 2026-07-04 (the BRAIN build sessions)

> Written at 90% context before forced compaction. Audience: the next Claude
> session (and Jack). Read this + `brain/SCHEMA.md` + `brain/README.md` +
> `docs/BRAIN-STATUS.md` before touching anything brain-related.
> Memory file `brain_project.md` has the compressed version.

## What exists now (all LIVE at wikilean.jackmccarthy.org/brain)

The Math Brain shipped end-to-end across ~15 commits on branch
`fix/smaller-batches-and-review-connect` (local; main is also local-ahead;
NOTHING pushed to origin — Jack pushes when he wants):

- **Dataset** (`brain/data/`): 21,263 nodes (2,671 concepts / 9,052 containers /
  7,045 decls / 2,495 literature), 127,931 edges in 8 kinds (contains,
  formalizes, mentions, depends, relates, xref, cites, matches), every edge
  `{provenance, confidence, evidence}` + pin. Contract = `brain/SCHEMA.md`
  (IT WINS over docs/BRAIN.md). Acceptance = `brain/test_acceptance.py`,
  10/10 green, includes Jack's four example datapoints.
- **Pipeline**: catalog inputs → `brain/build_nodes|edges|rollups|shards.py`;
  agent output enters ONLY via `brain/proposals/*` → skeptic `.verified.jsonl`
  → `brain/fold_proposals.py` (deterministic; any-reject-wins across batches;
  overrides need explicit skeptic accept; disputes of shipped grades →
  `grading_disputes.jsonl`).
- **UI** (`site/build_brain_page.py` → /brain, ~70KB self-contained page,
  d3@7 CDN): zoomable circle-pack **bubbles** view; **web** view (same level,
  force-directed, edge-first); **ego** view (click any concept/decl/paper →
  it centers and its whole neighborhood expands; click neighbors to walk the
  graph); satellites; informal rollups drawn between bubbles; provenance
  filter (human/machine/AI) + layer toggles + de-hub (lift) + logical
  communities (client-side greedy modularity per level); Sources legend;
  evidence drawers on every edge; code snippets + docstrings on decl panels;
  ghost decls (dimmed) for not-yet-linked declarations; "Connections at this
  level" clickable list on focused containers.
- **AI surface**: `GET /api/brain/node?id=…`, `/api/brain/search?q=…`
  (wiki/src/brain.ts), `brain/query.py`, skill `.claude/skills/brain-query`.
- **Ingests added this session**: MathNetwork/MathlibGraph (`w_types.exp` =
  explicit-subgraph counts, arXiv 2604.24797), @[stacks]/@[kerodon]/@[wikidata]
  harvest from the checkout (`catalog/harvest_mathlib_tags.py`, 677 rows,
  30/30 verified; 27 gold @[wikidata] pairs MINTED as edges), full-pool
  Wikidata relations re-harvest (2,557 relates edges), tree-grain rollups
  with `lift` (null-model de-hub), informal rollups (concept-home aggregation).

## THE NEXT BIG PROJECT (Jack's directive, verbatim intent)

**Overhaul the /brain UI: more animations, visually striking graphics,
dynamic and customizable.** Explicitly deferred from this session as too big.
Additional signal from Jack this session:

- The evidence cards are NOT self-explanatory ("that is not obvious from the
  screen") — the provenance/aggregation concepts need visual onboarding, not
  JSON dumps. Legibility of what a line/chip/count MEANS is part of the brief.
- He loved: /map bubbles' slickness (d3.interpolateZoom feel), the ego view,
  edges as first-class clickable objects, provenance filtering.
- He complained about (already fixed functionally, but keep in mind): edges
  hard to click/piled up, bubble view hiding informal layers, file-browser
  aesthetics ("plain file browser… I don't like it at all").

Design ammunition already researched (docs/research/mathdb-unification-research.json
`synthesis.ui_concepts` — 10 concrete concepts): formal coastline (formalization
status as terrain + time slider), evidence-on-tap (done, needs better visuals),
trust altitude (provenance as visual weight — solid/dim/hatched), literature halo
(paper dust orbiting concepts), stable continents / live weather, search-as-teleport
with camera flight, to-do heatmap overlay, disagreement beacons (pulsing QA nodes),
multi-homed chips (done as "Also in"), granularity zoom = semantic zoom (done).
"Customizable" suggests: user-togglable palettes/layouts, savable views,
maybe per-user layer presets (localStorage), configurable physics.

Tech notes for the overhaul: page is one generated HTML string in
`site/build_brain_page.py` (~1,300 lines) — consider splitting JS into a real
asset before it grows more. d3 transitions MUST keep the background-tab
fallbacks (rAF pauses → hard setTimeout resets, see `zoomInto`/`renderFocus`).
All data comes from `/assets/brain/` shards — one fetch per node, version-pinned
via `?v=<manifest generated_at>` + self-healing retry (do not break this; it
fixed a real cache-skew bug). Preview quirk: viewport often boots ~580px wide —
`preview_resize` to 1360x850 and RELOAD before judging layout; screenshots
render small but DOM checks are reliable.

## Verified backlog (docs/BRAIN-STATUS.md, twice-verified)

1. Multi-library grounding: FLT/Physlib/Cslib/Carleson/SpherePacking = ZERO
   concept coverage (~35k decls). Next discovery-fleet target; expect low QID
   yield (frontier decls often have no Wikidata item).
2. Concepts D1 table + POST /api/concept (roadmap P2, human web editing).
   Precondition: diffFields array-order fix (spawned as separate task chip).
3. Standing decl-collision guard in test_acceptance (Q3968/Q1000660 class).
4. OpenAlex Topics cross-check for informal placement (keyless, unused).
5. Nightly wiring: brain rebuild is NOT in site/ops (only /map is). The
   rebuild chain is documented in brain/README.md incl. fail-hard prereqs
   (oracle cache, mathlib4 checkout, catalog/.cache CSVs via fetchers).
6. Semantic layer: no decl/concept embeddings endpoint yet (audit: corpus was
   the binding constraint; universe now larger).
7. Concept↔concept AI semantic synapses were NEVER a fleet deliverable —
   informal web is thin at concept level beyond Wikidata relations. A fleet
   with that explicit deliverable (+ re-running the dead deep-connect pass
   over the ~350 remaining isolated nodes) would densify it.
8. 6 WDQS batches of the relates harvest throttled out (logged in the script
   output) — a re-run fills them.

## Policies / gotchas learned this session (do not re-learn the hard way)

- **Slogans are REMOVED by policy** (2026-07-04): theorem_matching.csv's
  license is contested upstream (CC-BY-SA card vs CC-BY-NC-SA paper) and
  slogan.csv (CC-BY-4.0) is informal-only (0/2.57M formal ids). Decl gloss =
  docstring + code. Reinstate only after upstream clarification
  (vilin@uw.edu) or attach slogan.csv to LITERATURE nodes.
- **Deploys**: wrangler pinned 4.107.0 (4.95 exits 0 after silently failing
  large asset uploads — bit us twice). Never pipe deploy output through
  `tail` alone (masks failures); check for "Deployed … Version ID".
  `npm run deploy` ships the WHOLE branch Worker — fine, Jack approved deploys.
- **Shard rebuilds rename shard keys** → any open tab needs the ?v= pinning
  (implemented). Sitemap cache key bumped to page:sitemap:v3.
- **Budget**: Jack watches token limits. Fleets died once on session caps
  mid-workflow (the fold recovered via base∪verified union — keep that).
  Prefer main-loop work for UI; Sonnet fleets for volume; verify passes for
  anything entering brain/data.
- The mathlib4 checkout is read-only, BRAIN_MATHLIB_CHECKOUT overrides its
  path; builds fail hard without oracle/checkout (deliberate).
- brain/data rollups file/dir/tree + site/assets/brain are gitignored derived
  data; nodes/edges/module-rollup are committed and ARE the dataset.
  Reproducibility caveat: build_graph_v2 reads the LIVE annotations cache
  (D1-canonical) → ~10% depends drift across annotation snapshots.

## Key files

brain/{SCHEMA.md,README.md,build_common.py,build_shards.py,fold_proposals.py,
test_acceptance.py,query.py} · site/build_brain_page.py (the whole UI) ·
wiki/src/brain.ts · catalog/{harvest_mathlib_tags,fetch_math_graph,
fetch_mathlib_graph,fetch_universe_extension}.py ·
catalog/mathlib_deps/fetch_wikidata_edges.py · docs/BRAIN-STATUS.md ·
docs/BRAIN.md (original spec; SCHEMA.md supersedes).

Session commits (oldest→newest): 01a8e05 catalog audit fixes · 4638ded brain
dataset · 3b39e7a /brain UI+API · 7e8407d self-review fixes · b34cc95 sitemap
key · 5111318 bubble canvas · b960be9 edge web + tree grain · cd276f4
hub_stats · a4100f7 lift + communities (arXiv 2604.24797) · ed0370f sources/
chips/lit-titles/code · 298488e MathlibGraph + ghosts + version pinning ·
8063be4 web view + edge UX + docs labels · f5a91b3 ego view · 958bf2a informal
synapses · 153117c BRAIN-STATUS + slogan removal · 11cd9c9 reciprocal dedup.
