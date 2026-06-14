---
name: wikipedia-search
description: Use during WikiLean annotation review to pull lightweight Wikipedia context from the English-Wikipedia APIs — find the canonical source article for a concept, grab a one-paragraph summary plus the Wikidata QID, fetch one section's plaintext to verify an annotation's anchor lives where it claims, or check the current revision id for drift against a pinned revid. This is the quick-context complement to site/render.py's full annotated render; reach for it whenever a reviewer/moderator needs to check an annotation against its source article without rendering the whole page.
---

# wikipedia-search

`wikipedia.py` — four targeted lookups against the live English-Wikipedia
action/REST APIs, for a reviewer checking a WikiLean annotation against its
source article. It is the COMPLEMENT to `site/render.py` (which reproduces the
whole anchored page); this tool answers small, fast questions during review and
must **not** reproduce render.py's full-page render.

Stdlib only (`urllib.request` + `json`) — run with plain `python3`, no venv, no
`pip install`. Every call sends the mandatory WikiLean reviewer User-Agent
(an empty UA returns HTTP 403), adds `maxlag=5` on action-API calls, and retries
once on 429/5xx with backoff (mirrors `catalog/fetch_catalog.py` `api_get`).
Add `--json` to any subcommand for machine output; the default is human-readable.

```
./wikipedia.py search  "<query>"                find the canonical article + alternatives
./wikipedia.py summary <Title>                  one-paragraph intro + Wikidata QID + revid
./wikipedia.py section <Title> "<section>"      plaintext of one section (verify an anchor)
./wikipedia.py revid   <Title>                  current revision id (drift vs pinned)
```

No auth, no env vars — all four engines are **keyless** (anonymous Wikimedia
APIs). The only requirement is outbound HTTPS to `en.wikipedia.org`.

## WHEN to use which subcommand

- **`search`** — DEFAULT "which article does this concept live on?" You have
  descriptive words from the annotation, not an exact title. Full-text
  CirrusSearch ranks by relevance over the article body and returns
  title + pageid + a stripped snippet. The tool prefers a result whose title
  **exactly** matches the query as `canonical` (disambiguation pages like
  "Prime ideal theorem" can otherwise out-rank the real article); when no exact
  match exists it falls back to the top hit and reports
  `exact_title_match: false`. Use this first, then feed the chosen title to the
  other subcommands.

- **`summary`** — DEFAULT "give me a quick context blurb." One REST call yields
  the lead's first paragraph (`extract`), the page `description`, the current
  `revision`, AND `wikibase_item` — the **Wikidata QID**, a free bridge into the
  R5/Wikidata cross-reference work (hand it straight to the wikidata-search
  skill; no separate pageprops call needed). Auto-follows redirects to the
  canonical title. For the FULL multi-paragraph lead use
  `action=query&prop=extracts&exintro` instead — this gives only the first
  paragraph.

- **`section`** — "does the annotation's anchor actually live in that section?"
  Two-step under the hood: resolve the section's display name (WikiLean anchors
  use the section heading `line` value) to its numeric index via
  `prop=sections`, then fetch just that one section's HTML, strip `<math>` +
  tags + unescape (render.py's recipe), and print clean prose. Pass
  `--oldid <revid>` to pin the exact revision the annotation was made against so
  you don't get drift false-positives; omit it to read the live page. Pass
  `--contains "<snippet>"` to assert the annotation's snippet is present — exit
  code `2` if absent (useful in scripts). The lead is section `0`; pass `0`,
  `lead`, or `intro` for it (it is not in the section TOC).

- **`revid`** — "has the article moved since the annotation was pinned?" Cheapest
  drift check: returns the live `lastrevid` plus who/when/why of the last edit.
  Pass `--pinned <revid>` (the value from `cache/<slug>.meta.json`) to diff
  against it — exit code `3` on drift, `0` if in sync.

## Real invocations + trimmed output

```
$ ./wikipedia.py search "prime ideal" --limit 3
Search: "prime ideal"  (totalhits=10112)
  did-you-mean: "prime idea"
  canonical [exact-title]: Prime ideal (pageid 24928)

1. Prime ideal  (pageid 24928, 2978 words)
     algebra, a prime ideal is a subset of a ring that shares many important ...
2. Ideal (ring theory)  (pageid 25977, 6665 words)
     ...the prime ideals of a ring are analogous to prime numbers...
3. Prime ideal theorem  (pageid 834957, 53 words)
     ...This disambiguation page...
```

```
$ ./wikipedia.py summary "Prime ideal"
Prime ideal
  Ideal in a ring which has properties similar to prime elements

In algebra, a prime ideal is a subset of a ring that shares many important
properties of a prime number in the ring of integers...

  QID:      Q863912            # <- hand to wikidata-search
  revision: 1359111119   (2026-06-13T04:28:42Z)
  url:      https://en.wikipedia.org/wiki/Prime_ideal
```

```
$ ./wikipedia.py section "Prime ideal" "Definition" --oldid 1359111119 --contains "is prime if"
Prime ideal — section [2] Definition @oldid 1359111119

Definition An ideal P of a commutative ring R is prime if it has the following
two properties: ...

  snippet "is prime if": FOUND          # exit 0 (would be exit 2 if NOT FOUND)
```

```
$ ./wikipedia.py revid "Prime ideal" --pinned 1356698429
Prime ideal  (pageid 24928)
  lastrevid: 1359111119   touched 2026-06-13T04:28:42Z
  last edit: 1359111119 by The Boolean @ 2026-06-13T04:28:42Z

  DRIFT: pinned 1356698429 != live 1359111119 — annotation may be stale.   # exit 3
```

## Exit codes

- `0` success (or `contains`/`pinned` check passed).
- `1` API/HTTP failure, missing page, or 404 — printed as `error: ...` to
  stderr, never a traceback.
- `2` `section --contains` snippet NOT found.
- `3` `revid --pinned` detected drift (live revid != pinned revid).

## Gotchas

- `search` snippets are HTML with `<span class="searchmatch">` highlight markup;
  the tool strips them before printing, so the output is already clean prose.
- `summary` `extract` is exactly ONE paragraph. When the lead has multiple
  paragraphs you may be missing context — use `prop=extracts&exintro` for the
  full lead.
- `summary` on a disambiguation page prints `[disambiguation page]` — don't
  treat it as a single source article; go back to `search`.
- For `section`, prefer the section's display NAME (the heading text WikiLean
  anchors against), e.g. `"Definition"`. The tool also accepts the anchor form
  and a case-insensitive substring as fallbacks; if nothing matches it errors
  with the list of available section names.
- Always pass `--oldid <pinned revid>` for `section` during review so you match
  the exact revision the annotation was pinned to — checking against the live
  page can show drift edits as false anchor mismatches.
- Disambiguation pages (e.g. "Prime ideal theorem") can rank in `search`
  results; trust the `canonical [exact-title]` line when present.

## Do NOT

- Do NOT use this to render the full annotated page — that is `site/render.py`.
  This is per-section, lightweight, read-only context only.
- Do NOT reimplement the plaintext stripping; the script already applies
  render.py's recipe (`<math>` elements → space, tags → space, `html.unescape`,
  drop the `[ edit ]` heading artifact, collapse whitespace).
