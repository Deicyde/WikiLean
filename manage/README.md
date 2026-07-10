# `manage/` — the WikiLean control plane

The connective tissue that turns the concept graph into a live prioritizer for
both the `@[wikidata]` tagging pipeline and the annotation-moderation loop.

The insight: the `wikidata_qid` already joins every subsystem (D1 articles,
the catalog, the concept graph, the pipeline pool), and the concept graph
already carries the edges that encode structural importance — Mathlib
decl-dependencies + Wikidata semantic relations. Nothing was reading them.
This directory computes centrality over that graph, joins it to live coverage,
and emits ranked worklists the rest of the project can act on.

Everything here is **pure Python, deterministic, offline** (except the optional
`--pull`), **no LLM**. It never writes upstream and never touches the files the
`@[wikidata]` bot owns — safe to run any time.

## Run it

```bash
python3 manage/refresh.py          # compute from current disk, write digest
python3 manage/refresh.py --pull   # `npm run pull` (live D1) first, then compute
```

Or a single pass:

```bash
python3 manage/centrality.py   # concept_graph.json    -> data/centrality.json
python3 manage/coverage.py     # site/annotations/*.json -> data/coverage.json
python3 manage/worklists.py    # join                  -> data/{moderation,pipeline}_worklist.json
```

Session snapshot + backlog feed:

```bash
python3 manage/status.py [--live]         # ground-truth snapshot (session-start hook runs this)
python3 manage/formalize_backlog.py       # verify the formalize worklist vs live D1 -> data/formalize_slugs.txt
```

## Artifacts (`manage/data/`, git-tracked — this is the control-plane state)

| File | What |
|---|---|
| `centrality.json` | per-QID PageRank + degree + 0–100 percentile over the concept graph |
| `coverage.json` | per-article status counts, coverage fraction, and `state` (see below) |
| `moderation_worklist.json` | `review` (mirrored, low-coverage, central) + `add` (central, not mirrored) |
| `pipeline_worklist.json` | pool candidates re-ranked by centrality, with the wikilink-rank delta |
| `digest.json` | the rollup `/wl-status` reads and the scheduled agent posts |

## How the signal is built

- **Centrality.** PageRank on the directed concept graph. A Mathlib edge `A→B`
  means "A's decl references B's decl" (A depends on B), so rank flows toward
  **foundational** concepts — the top of the list is Set / Ring / Arithmetic,
  which is what we want to prioritize. Degree measures are reported alongside.
- **Coverage.** Per-*statement*, from the annotation layer. An annotation with
  no `status` is Agent-1 extraction (`provenance: ai-agent1`) that never went
  through Agent-2 formalization. Article `state`:
  - `moderated` — has status-bearing annotations; `coverage = (formalized + ½·partial)/n_status`
  - `extracted` — statements found, none formalized yet (**awaiting Agent 2** — a cheap win)
  - `empty` — no annotations
- **The join is by `slug`**, not QID — upstream graph noise can double-assign a
  QID to a slug (`Q199` and `Q310395` both carry slug `1`), so the filename is
  the safe key. The `review` list is deduped by slug; `add` by QID.

## Worklists map to the mission's three routine operations

- `moderation_worklist.review` → op (2) review/correct existing articles; the
  `extracted` rows are the standing Agent-2 backlog (currently 31 articles /
  ~1,271 statements).
- `moderation_worklist.add` → op (1) generate annotations for **new** articles
  (central concepts with no mirror yet, e.g. Real number, Group theory).
- `pipeline_worklist` → what the `@[wikidata]` bot should tag next, ordered by
  structural importance instead of the frozen wikilink count in
  `bot/data/most_used_qids.json`.

## Moderation integration (the formalize backlog)

The `/api/work` ladder sorts by `lastReviewedAt`/`version` — it has no notion of
"has Agent-1 statements but no Agent-2 formalization," so the extracted backlog
is invisible to the runner. The fix, wired into the nightly job:

1. `formalize_backlog.py` reads the `formalize` worklist and verifies each slug
   against **live D1** (the on-disk layer lags D1 — an article can already be
   formalized in D1 while disk still shows Agent-1 only). It emits only the
   genuinely-still-extracted slugs to `data/formalize_slugs.txt`.
2. `moderate.py review --slugs data/formalize_slugs.txt` reviews exactly those
   (a new, general `--slugs` option on the runner — process_review GETs each
   article's live state, so only the slug is needed), running Agent-2 over the
   Agent-1 statements.
3. `site/ops/nightly-moderate.sh` runs both after wp-update and before the
   general review. **Tune the rate in `site/ops/nightly.env`** — one file, one
   line: `WIKILEAN_FORMALIZE_LIMIT` (articles/night, default 12; 0 to pause) and
   `WIKILEAN_FORMALIZE_BUDGET` (token cap, default 600k). A one-off env override
   still wins (`WIKILEAN_FORMALIZE_LIMIT=25 bash site/ops/run-now.sh`). Nightly
   agent spend ≈ FORMALIZE_BUDGET + BUDGET_TOKENS.

## Brain nightly (axis 4 — separate job, shared conventions)

The Brain has its own 02:20 launchd job, `site/ops/brain-nightly.sh`
(org.wikilean.brain), upstream of the 03:00-cluster jobs this control plane
feeds: external-DB ingest (per-source cadence) → propose-only agent team
(`brain/sync_agents.py`, gated OFF by default) → `brain/fold_proposals.py` →
node/edge rebuild → acceptance gate → shards → clean-tree-gated deploy. It
never touches `manage/data/`; tune it in `site/ops/nightly.env`
(`WIKILEAN_BRAIN_*`) and read `site/ops/README.md` for install/operate.

## Known limitations (documented, not hidden)

1. **The concept graph is rebuilt from the catalog, not from D1.** `refresh.py`
   flags when the annotation layer is newer than `concept_graph.json`; a full
   refresh of the graph's *node set / status* still needs
   `catalog/mathlib_deps/merge_graph.py`. Coverage always reflects live disk;
   centrality reflects the last graph build.
2. **The `review` queue can surface inherently-unformalizable articles**
   (biographies like *Mathematician*, fields like *Statistics*, physics like
   *General relativity*) — high centrality × permanently-low coverage keeps them
   near the top. Treat a persistently-0-coverage central article as a signal to
   *skip/flag*, not to grind. A `not_formalizable` flag is a sensible follow-up.
3. **Pipeline re-rank disables pool's P31 field filter** (to stay offline). The
   live batch still applies it, so a discipline-QID that slips into the worklist
   is dropped when the batch actually opens.
