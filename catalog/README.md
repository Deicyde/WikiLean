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
