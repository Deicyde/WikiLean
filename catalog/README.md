# catalog/

Pipeline that enumerates **WikiProject Mathematics** articles on English Wikipedia and emits a JSONL catalog with per-article metadata. This is the seed dataset that the Lean/Mathlib mapping passes will join against.

## Setup

```sh
cd catalog
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```sh
python fetch_catalog.py              # full catalog (~5–10 min, ~29k articles)
python fetch_catalog.py --limit 50   # quick smoke test
```

Output: `data/articles.jsonl`, one JSON object per article. Fields:

| field | description |
|-------|-------------|
| `title` | Wikipedia article title (mainspace) |
| `talk_title` | Talk-page title (where the banner lives) |
| `pageid` | Article pageid; `null` if missing/deleted |
| `talk_pageid` | Talk-page pageid |
| `wikidata_qid` | Wikidata item id (e.g. `"Q11518"`) |
| `p31` | List of Wikidata `instance of` (P31) values for the article's QID |
| `is_human` | `true` if `Q5` (human) is in `p31` — flags biographies |
| `class` | Quality rating: `FA`, `GA`, `B`, `C`, `Start`, `Stub`, `List`, etc. |
| `importance` | Importance: `Top`, `High`, `Mid`, `Low` |
| `field` | Subject field if set by the banner (`algebra`, `analysis`, …) |
| `historical` | `true` when the banner sets `historical=yes` |
| `talk_rev_id` | Revision id of the talk page at fetch time |
| `talk_rev_timestamp` | Revision timestamp |
| `raw_banner` | Full `{{WikiProject Mathematics\|…}}` snippet, for offline re-parsing |
| `article_missing` | `true` if the talk page exists but the mainspace article doesn't |
| `fetched_at` | ISO-8601 UTC timestamp of the run |

## How it works

Four sequential API passes:

1. **Enumerate** — `list=embeddedin&eititle=Template:WikiProject_Mathematics&einamespace=1` paginates all talk pages transcluding the banner.
2. **Banner content** — `prop=revisions&rvprop=content` on talk pages in batches of 50, extracts the banner snippet with brace-balanced matching, parses top-level params (`class`, `importance`, `field`, `historical`).
3. **Article metadata** — `prop=pageprops|info&ppprop=wikibase_item` on the mainspace article titles in batches of 50, follows redirects, captures the Wikidata QID.
4. **Wikidata P31** — `action=wbgetentities&props=claims` against `wikidata.org` for each QID, in batches of 50, extracts the `P31` (instance of) claim values. Used to distinguish biographies (`Q5`) from mathematical concepts.

Each phase writes an incremental JSONL cache under `data/.cache/`, so an interrupted run resumes by skipping already-fetched ids.

## Pilot subset

`build_pilot.py` writes `data/pilot.jsonl`, the high-value slice we use for matcher validation:

```sh
python build_pilot.py
```

Criteria: `class ∈ {FA, GA, B}` and `importance ∈ {Top, High}`. Current snapshot: **429 articles** (354 concepts + 75 biographies).

## Mathlib tagging

`tag_with_mathlib.py` spawns Claude agents (via `claude-agent-sdk`, authenticated against your local `claude login` — Max-plan session, no API key) to identify Mathlib4 declarations that formalize each pilot concept. Each agent has read-only access to `~/Desktop/LEAN/mathlib4` via `Read` / `Grep` / `Glob`, and emits a structured JSON record (`mathlib_decls`, `primary_decl`, `notes`, `no_match_reason`).

```sh
unset ANTHROPIC_API_KEY   # critical — see comments at top of the script
python tag_with_mathlib.py --concurrency 10
```

Output: `data/pilot_tagged.jsonl`. Resumable — re-running skips titles already in the output.

**Pilot (Opus 4.7, 354 concepts, FA/GA/B × Top/High):**
- **70.9%** (251/354) of concept articles have at least one matched Mathlib declaration.
- 60 "not formalized", 22 "not amenable", 16 "unclear scope", 5 "too elementary".
- Avg 7.2 turns/agent, 26.4s/agent, ~$0.14 equivalent/article. Full pilot: 15.6 min @ concurrency 10, ~$48 equivalent.

**Tier 2 (Opus 4.7, 1,023 concepts: B × Mid, C × Top, C × High):**
- **57.6%** (589/1,023) matched. 320 "not formalized" dominates the no-match bucket.
- Took two runs: the first hit the Max-plan Opus 5-hour rolling window after ~300 articles and the remaining 694 errored out with `Claude Code returned an error result: success`. The retry (~5h later, with the resume logic now skipping only successful rows) finished the remaining 694 in 28.7 min, 0 errors.
- Combined cost: ~$133 equivalent. The script now dedupes the output JSONL on completion so resumed runs leave a clean file.

The output file `data/tier2_tagged.jsonl` is keyed by title; each row is the per-article record described above.
