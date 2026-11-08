# All of TauCeti in the Brain, one cell per declaration

Plan, 2026-11-05, revised 2026-11-08. Tracking issue: #152. The read-layer design is
[`docs/BRAIN-D1.md`](BRAIN-D1.md). Code references are repo-relative `path:line` at main
`596aa7bb92e89c1f7a44f768392c3b895b049c47`.

## Goal

Every TauCeti declaration (286,070 unique when first measured, 311,515 a commit-day later)
becomes its own cell on /brain, reachable by search, deep link, the Worker API and MCP, with
its code, its docstring and its uses of Mathlib declarations as synapses.

## What was measured

**First pass: projections.** From TauCeti `e8758b7b`, with the harvest cap raised in a scratch
checkout that was never pushed:

| | Live release | All of TauCeti |
|---|---|---|
| TauCeti harvest rows | 7,860 | 286,352 (286,070 unique) from 24,453 files |
| `catalog/data/tauceti.jsonl` | 4 MB | 154 MB (22.6 MB gzipped) |
| TauCeti decls that use a Mathlib decl | | 125,616, about 201k invocation edges |
| Brain nodes / edges / cells | 63k / 175k / 21k | about 345k / 652k / 299k (projected) |
| `brain/data` total | | about 2.5 GB, SQLite about 1.6 GB (projected) |

The invocation scan matches only Mathlib names (`brain/build_common.py:1083-1100`), so there
are no TauCeti-to-TauCeti edges, and no declaration hits the 50-name overflow cap.

**Second pass: a measurement build on a separate Linux build machine (2026-11-06).** It took
the tracked Brain graph (the 2026-08-28 build), rebuilt its TauCeti layer from a fresh harvest
of TauCeti `afa08a3e` with `build_common`'s own TauCeti layer, then ran the real cells, layout
and shard stages. Link synapses and four other build inputs are not in Git, so this is not a
full build.

| | Result |
|---|---|
| TauCeti harvest | 311,515 declarations, 53 s, 1.2 GB |
| Brain | 370,665 nodes, 707,931 edges; 324,416 cells, 330,238 synapses |
| Cells stage (no layout) | 20 s, 2.7 GB |
| Force layout, all 152,922 connected cells | collapses: candidate pairs 28M, 114M, 261M, 507M, 872M at steps 0-50; 27 s a step and rising; stopped |
| Force layout, core only (13,213 cells) | 150 s with the grid |
| Shard stage | 137 s, 8.7 GB; 20,765 files, 783 MB; 21 shards over 150 KB |
| Whole files | aliases 80.3 MB, explorer 69.5 MB, labels 60.2 MB, supercells 24.7 MB |
| Serving SQLite prototype | about 1.0 GB with plain rowid tables (1.73 GB as WITHOUT ROWID, because big rows spill to overflow pages); name trigram index 48 MB, docstring index 116 MB |
| Local query times (plain SQLite, not D1) | cell page 0.2 ms, alias 0.02 ms, name fragment 1-5 ms, docstring search 0.7 ms |

So a D1 database fits (about 1 GB a dataset, against 10 GB per database on Workers Paid), and
TauCeti's declarations cannot go through the force simulation.

## What breaks, worst first

1. **The harvest cap.** `DECL_CAP = 20_000` (`brain/ingest/lean_repo.py:57`). The nightly runs
   the harvest fail-soft (`site/ops/brain-nightly.sh:597`), so it keeps the old 7,860-row file
   without any signal. The live Brain carries TauCeti as of an old harvest.
2. **GitHub's 100 MB file limit.** Four tracked files cross it: `tauceti.jsonl` (154 MB) and
   `brain/data/nodes.jsonl`, `edges.jsonl` and `cells.jsonl` (about 208, 216 and 134 MB).
3. **Cloudflare's 25 MiB per-asset limit.** The /brain page and the Worker load four whole
   files: aliases, labels, explorer and supercells (sizes above). The limit holds on every
   plan, so these files must be split or replaced.
4. **Worker memory.** aliases, labels and supercells are about 150 MB of JSON, parsed whole
   per isolate (`wiki/src/brain-api.ts:319-342`), above the 128 MB isolate limit. Some
   requests (resolve, search, bridge) hold all three.
5. **Release staging caps.** 20,000 objects and 512 MiB
   (`wiki/scripts/brain-release-public.ts:63-64`), over the current release, the previous
   release and a mutable copy of all of `cells/`: 36-51k objects and 1.4-2.0 GB.
6. **Force layout.** All-pairs repulsion (`brain/layout.py:88-101`) over the connected cells,
   about 150k instead of at most 21k. It collapses (above).
7. **Promoter limits.** The 900 s per-command timeout (`site/ops/brain_promote_release.py:1450`)
   covers verify, staging and deploy, and is expected to trip at 2.5 GB (not measured). The
   first activation's semantic diff (about 1 GB) meets its 1 GiB cap
   (`site/ops/brain_activation_bundle.py:96`).
8. **Shard key length.** Splitting stops at 64 characters (`brain/build_shards.py:72`). 48% of
   TauCeti cell ids are longer, so some shards stay well over the 150 KB target and no gate
   flags them.

Everything else scales linearly: facets, merges, the frontier, the SQLite store and the
acceptance gates (none has a ratio or maximum that trips).

## Decisions (Jack)

- **Storage**, 2026-11-06: "Sure take them out of git. The data should really live on our
  CloudFlare SQLite database."
- **Search**, 2026-11-06: "Whatever you think would be most useful for humans and agents."
- **Explorer**, 2026-11-06: "I like the current explorer, but I'm open to improving it if you
  have ideas."
- **Layout**, 2026-11-06: "Yes this bubble idea sounds very cool to me I love it."
- **Plan and database**, 2026-11-06: Workers Paid, confirmed in issue #135 (a D1 Time Travel
  bookmark 17 days back, where Free keeps 7). Database: "I think one shared database is best".
- **Row keying**, 2026-11-08: rows are keyed by serving dataset (content-addressed), not by
  release.
- **Replay**, 2026-11-08. Asked whether D1 releases should be compatibility-only until the
  replay tools learn the D1 layout, Jack answered "Whatever is better long-term." So the
  replay tools move onto the D1 layout before the cutover, and D1 releases can be full
  offline replay releases from the first one. Why:
  - after the cutover the D1 layout is the only layout, so there is no window without a
    replay path;
  - replay hazards (for example, SQLite bytes that depend on the SQLite version) surface
    before the first D1 release binds them;
  - the TauCeti release, the largest data change so far, gets full replay evidence.

## Architecture: the Brain is served from D1

Today the Worker and /brain read static JSON files. Instead, each release's Brain tables go
into a D1 database, and the Worker answers every Brain read with a bounded, indexed query. This
removes breaks 3, 4 and most of 5: no whole file is loaded, the Worker holds only the rows a
request needs, and the shard files become rows.

- **One shared database**, `wikilean-brain`, separate from `wikilean`. The daily backup exports
  all of `wikilean`, and about 1 GB per dataset of rebuildable rows would swamp it. The
  community overlay stays in `wikilean`.
- **Rows belong to serving datasets, not releases.** The build writes
  `brain/data/serving.sqlite3` and a sidecar, `brain/data/serving-tables.json`, with each
  table's row count and digest. The sidecar's logical root is the dataset id. A `releases`
  table maps each release to one dataset, so a release whose Brain tables did not change
  imports nothing, and releases with equal tables share one copy.
- **The selector stays static.** `current.json` and `release.json` stay files and roll back
  together with the Worker. `release.json` binds the serving file and the sidecar like any
  other artifact.
- **Imports are batched and resumable.** The importer copies each table in small ranges
  through D1's query API, never a file import (which blocks the database), reads every range
  back, compares digests and journals it. A failed or repeated call is repaired by rerunning
  its range. The live release keeps serving throughout.
- **Pruning** keeps the live current and previous releases' datasets and one candidate's, so
  at most three datasets at once.
- **Small files stay static**: `brain.html`, the release manifest, a slim cell manifest,
  `cells/frontier.json`, `cells/frontier_graph.json` and `sources.json`.
- **The big generated files leave Git** (`tauceti.jsonl` and `brain/data` nodes, edges and
  cells) and stay as build files on the operator Mac. Releases already bind inputs by digest.

## Search

Two indexes per dataset, both SQLite FTS5:

- **Names.** Every cell label, other name and supercell path. Results come in tiers: exact
  matches, then prefix matches, then fragments anywhere in a name (`Semigroup` finds
  `StronglyContinuousSemigroup`), cells before supercells within each tier. Two-character
  queries get exact and prefix matches only. If D1 lacks the trigram tokenizer (#150 checks),
  names are indexed by their parts instead, and fragments must start at a capital, dot,
  underscore or slash.
- **Text.** A word index over docstrings and descriptions, ranked by relevance. An agent can
  ask "spectrum of a compact operator" and get declarations whose docstrings say so.
- One endpoint returns name hits, then text hits, and each hit says which index matched, so
  an agent can tell an exact name from a topical match. The /brain search box,
  `GET /api/brain/search` and the MCP search tool all use it.

## Explorer

It stays as it is. Its data comes from D1 per scope instead of one file. Small scopes are
computed per request; larger scopes and `all` are precomputed, at most 8 MB each. Cells that
sit in a module bubble show as one dot per module, sized by its declaration count, so Mathlib
scopes look as they do now, and a scope too big to draw collapses its own bubbled cells the
same way. Ideas for later, not in this plan: colour by library, and highlight the Mathlib
declarations a TauCeti declaration uses.

## Layout: module bubbles

The force simulation collapses at about 150k connected cells. So only the core (Mathlib,
concepts and the other small layers, about 13k cells) is simulated, as today. Each module of a
big non-Mathlib library becomes a bubble sized by its declaration count, placed as near as it
can get to the Mathlib cells its declarations use, overlapping no other bubble and not the
core. The declarations spiral inside their module's bubble.

A prototype on the build machine took 153 s for the core, 150 s for 4,114 TauCeti bubbles and
2 s for the declarations. TauCeti forms a ring around the core, and the map's radius grows
from about 3k to 19k units. The prototype left 0.5% of bubble pairs overlapping; the real
version ends with an exact no-overlap pass. PR #137 implements grid-bucketed repulsion and the
bubbles.

## Plan

[affecting] marks a release-affecting PR under `site/ops/brain_release_authority.py`. Such a
PR merges only when no built release is waiting to be promoted, and ships in a new release with
a fresh P1B and Jack's approval.

- Layout at TauCeti scale: grid repulsion and module bubbles, PR #137 [affecting].
- 0. D1 probe on the operator Mac (#150), then a full-size measurement build.
- 1. Serving database builder [affecting], PR #153.
- 1b. Explorer tables, after #137.
- 2. `wikilean-brain` migration and importer.
- 3a. Worker: a `BrainStore` interface, no behaviour change [affecting].
- 3b. Worker: D1 reads and the page endpoints, dormant until a release binds a serving
  database [affecting].
- 4. Release contract: freeze, verify, canary.
- 5. Promoter: import, readiness checks, verify after the canary.
- 6. Replay tools on the D1 layout [affecting].
- 7. Cutover: the page reads the endpoints and the nightly builds the serving database
  [affecting], held until steps 1-6 land and a remote scratch import is verified.
- 8. TauCeti harvest cap and the big files out of Git [affecting], last.
- 9. The release: fresh P1B, Jack's approval, promote.
- 10. Cleanup: delete the static reader.

**Why step 8 lands last.** Raising the cap first would put all of TauCeti into the next
release build before the build and the site can carry it, so no release could ship from main
until everything else lands. Untracking `tauceti.jsonl` first is worse: the operator Mac's pull
deletes the file, the still-capped weekly harvest fails, and TauCeti drops out of the next
release entirely. So steps 1-7 land while TauCeti stays at the old 7,860-row harvest, and step
8 is the last PR before the release. When it lands, the operator Mac runs the TauCeti harvest
right after pulling. Step 8 also makes a failed TauCeti harvest show in the nightly log.

Step 0's signal: a serving file under about 2 GB and bounded queries answering in the tens of
milliseconds mean the plan holds. Details of every step are in
[`docs/BRAIN-D1.md`](BRAIN-D1.md), section 8.
