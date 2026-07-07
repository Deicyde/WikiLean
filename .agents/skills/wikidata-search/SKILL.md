---
name: wikidata-search
description: Use when a WikiLean reviewer/moderator bot needs to resolve a math concept or Wikipedia title to a Wikidata QID, check which formal-library cross-references (Metamath/nLab/MathWorld/ProofWiki/defining-formula) already exist on it, verify a concept maps back to the expected Wikipedia article, or run a coverage/gap query over the Wikidata graph (the @[wikidata] tagging and "Mathlib declaration" property-proposal workflows).
---
# wikidata-search

A self-contained CLI (`wikidata.py`, stdlib only) that wraps the Wikidata Action API,
the Wikibase REST API, WDQS SPARQL, and the reconciliation API for one job:
**map a concept to a QID and inspect its formal/reference cross-refs + graph position.**

Run it directly:

```
.Codex/skills/wikidata-search/wikidata.py <subcommand> ...
```

Every subcommand takes `--json` for machine output (default is human-readable).
No API keys required for any path — **all subcommands are keyless** (see Auth below).

## WHEN to use which subcommand

| You want to…                                                              | Use            |
|---------------------------------------------------------------------------|----------------|
| Turn a concept title / Wikipedia title into candidate QIDs                | `search`       |
| Turn an exact enwiki article title/slug into ITS QID (no search)          | `by_slug`      |
| Find a QID by MEANING when phrasing may not match the label               | `semantic`     |
| Disambiguate candidates by description + P31/P279                         | `entity`       |
| **Check what formal cross-refs a concept already has** (the core job)     | `xrefs`        |
| Verify a QID points back to the expected Wikipedia article / list mirrors | `sitelinks`    |
| Answer a set-shaped question (coverage/gap report, type joins)            | `sparql`       |
| Bulk-reconcile many titles at once with a 0-100 score (optional)          | `reconcile`    |

**Typical reviewer flow:** `search "<title>"` → eyeball candidates → `entity <QID>`
or `xrefs <QID>` on the top 1-3 to disambiguate by description/P31 →
`xrefs` to read the formal links → `sitelinks` to confirm the enwiki article matches.
`wbsearchentities` does **not** type-filter and routinely returns several same-label
items (ring-theory vs order-theory vs noncommutative "prime ideal"), so **never trust
the top hit** — always disambiguate before pinning a QID.

## Subcommands (real commands + trimmed real output)

### `search "<label>"` — concept → ranked QID candidates
Label/alias **prefix** match (not free text). Cheap single round-trip. `--limit` 1-50, `--type item|property`.

```
$ wikidata.py search "prime ideal" --limit 3
3 candidate(s) for 'prime ideal' (DISAMBIGUATE by description/claims -- do not trust the top hit):
  Q863912      prime ideal
               ideal such that, whenever a product belongs to it...  [matched: label]
  Q11710992    prime ideal
               notion in order theory  [matched: label]
  Q139919115   prime ideal
               a concept of non-commutative ring theory  [matched: label]
```

### `by_slug "<enwiki title>"` — exact article title/slug → QID (NOT a search)
The article being annotated maps to its QID by an **exact enwiki sitelink lookup**
(`wbgetentities sites=enwiki`), so the top-level concept needs no guessing.
Underscores are normalized to spaces (`Pythagorean_theorem` and `Pythagorean theorem`
both resolve). A clean miss (no such article) is reported, not an error.

```
$ wikidata.py by_slug "Determinant"
Q178546  determinant  (enwiki: Determinant)
  sum of signed terms of n factors from n×n matrix with no two factors sharing row or column
```

### `semantic "<description>"` — meaning-based search over the math-QID universe
**Local** embedding search (sentence-transformers `all-MiniLM-L6-v2`, 384-d) over the
curated ~11.7k-row `catalog/data/wikidata_universe.jsonl`. No network at query time.
Fixes the **broad-QID failure mode** of `search` (label-prefix only): describe the
concept in prose and get candidates ranked by cosine similarity. `--k` sets the count
(default 8). Requires the index built once by `catalog/build_wikidata_embeddings.py`
(rebuilt in the nightly when the universe changes). Confirm the pick with `xrefs`.

```
$ wikidata.py semantic "the squeeze theorem" --k 3
3 semantic match(es) for 'the squeeze theorem' (cosine; DISAMBIGUATE by description, confirm with xrefs):
  Q1065257     0.9572  squeeze theorem
                       theorem
  Q3527214     0.6911  Non-squeezing theorem
                       theorem
  Q7582217     0.6463  Squeeze operator
                       formula
```
NB: the index covers only the curated math universe, so a concept absent from
`wikidata_universe.jsonl` (some base concepts are) won't appear — fall back to `search`.

### `entity <QID>` — labels/description + P31/P279, via the clean REST API
Use to disambiguate `search` candidates. Surfaces instance-of/subclass-of QIDs, alias list, enwiki sitelink, statement count.

```
$ wikidata.py entity Q863912
Q863912  prime ideal
  ideal such that, whenever a product belongs to it, at least one of its factors also belong to it
  subclass of (P279): Q1142699, Q17098198
  enwiki: Prime ideal
  (18 statements total)
```

### `xrefs <QID>` — JUST the formal/reference cross-refs (CORE)
The @[wikidata] / property-proposal use case: what formal links already exist?
Reads P12888 Metamath, P4215 nLab, P2812 MathWorld, P6781 ProofWiki, P10283 OpenAlex,
P2534 defining-formula, plus the enwiki sitelink as the inverse sanity check.

```
$ wikidata.py xrefs Q863912
Q863912  formal cross-references  (enwiki: Prime ideal)
  Metamath   P12888   df-prmidl
                      https://us.metamath.org/mpeuni/df-prmidl.html
  nLab       P4215    prime ideal
  MathWorld  P2812    PrimeIdeal
  ProofWiki  P6781    Definition:Prime_Ideal_of_Ring
  OpenAlex   P10283   C2779467367
  defining formula P2534    \mathfrak p \ne R \land \left(\forall (x,y)\in R^2\colon ...\right)
```
Empty result (concept exists but has no formal links) is normal — `--json` returns
`"xrefs": []` plus whatever `enwiki` sitelink it has.

### `sitelinks <QID>` — Wikipedia articles for a QID
Confirms the concept→QID map points back to the right enwiki article and lists
other-language mirrors. Wikipedias only by default; `--all` adds commons/wikibooks/etc.

```
$ wikidata.py sitelinks Q863912 | head -4
Q863912  29 sitelink(s):
  bgwiki         Прост идеал
  dewiki         Primideal
  enwiki         Prime ideal
```

### `sparql "<query>"` — WDQS main graph (set-shaped questions)
Pass the query as an arg, or `-` to read from stdin (better for multiline). Strips
entity URIs to bare QIDs in a table; `--json` gives `[{var: value}, ...]` rows.

```
$ wikidata.py sparql 'SELECT ?item ?itemLabel ?metamath WHERE {
    ?item wdt:P12888 ?metamath .
    ?item wdt:P31/wdt:P279* wd:Q24034552 .
    SERVICE wikibase:label { bd:serviceParam wikibase:language "en". } } LIMIT 3'
3 row(s):
  item      itemLabel              metamath
  Q26708    binomial theorem       binom
  Q134237   square root            df-sqrt
  Q187235   Riemann zeta function  df-zeta
```

### `reconcile "<label>" [--type Q...]` — OPTIONAL, bulk, 3rd-party
W3C Reconciliation service with a 0-100 score. Use only for bulk column matching.
Runs on third-party WMCloud (no SLA) — **do not make it load-bearing**; for the
per-concept reviewer path, `search` + an `entity`/`xrefs` disambiguation is more
controllable and runs on Wikimedia prod.

```
$ wikidata.py reconcile "prime ideal" --limit 2
2 candidate(s) for 'prime ideal' (3rd-party WMCloud, no SLA; score 0-100):
  Q863912      score=100.0  match=False  prime ideal
  Q11710992    score=100.0  match=False  prime ideal
```
Gotcha: a `--type` that isn't actually in the item's P31/P279* tree **penalizes** the
score (e.g. `--type Q24034552` drops these to 50). Pick a type that's really in the
tree, or omit it.

## Ready SPARQL templates

**(a) One concept → ALL formal cross-refs in one query** (substitute the QID from `search`):
```sparql
SELECT ?item ?itemLabel ?metamath ?nlab ?mathworld ?proofwiki ?formula WHERE {
  VALUES ?item { wd:Q863912 }
  OPTIONAL { ?item wdt:P12888 ?metamath . }   # Metamath
  OPTIONAL { ?item wdt:P4215  ?nlab . }        # nLab
  OPTIONAL { ?item wdt:P2812  ?mathworld . }   # MathWorld
  OPTIONAL { ?item wdt:P6781  ?proofwiki . }   # ProofWiki
  OPTIONAL { ?item wdt:P2534  ?formula . }     # defining formula
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
```

**(b) Coverage/gap report — math concepts WITH a Metamath ID but NO Mathlib-decl property.**
`P99999` is a **PLACEHOLDER** for the proposed "Mathlib declaration" property (no real
Pid assigned yet — replace it once the property proposal is accepted):
```sparql
SELECT ?item ?itemLabel ?metamath WHERE {
  ?item wdt:P12888 ?metamath .
  ?item wdt:P31/wdt:P279* wd:Q24034552 .          # is a mathematical concept
  FILTER NOT EXISTS { ?item wdt:P99999 [] . }     # PLACEHOLDER: lacks Mathlib-decl
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
} LIMIT 200
```

**(c) QID → its Wikipedia sitelinks** (verify the inverse map / enumerate mirrors;
`sitelinks` does this too, this is the SPARQL form for joining):
```sparql
SELECT ?article ?lang ?title WHERE {
  ?article schema:about wd:Q863912 ;
           schema:inLanguage ?lang ; schema:name ?title ; schema:isPartOf ?wiki .
  FILTER( CONTAINS(STR(?wiki), "wikipedia.org") )
} ORDER BY ?lang
```

## Gotchas

- **WDQS requires a User-Agent.** No/empty UA → **HTTP 403** (verified: empty=403, our
  UA=200). The script always sends `WikiLean-reviewer/1.0`, so this is handled — but
  know it if you ever hit the endpoint by hand.
- **WDQS is the MAIN graph only** (since March 2025; no scholarly/lexeme split). Fine
  for math concepts.
- **Anchor P279* climbs on `wd:Q24034552` ("mathematical concept"), NOT `Q151885`
  ("concept", far too broad).** And always add `LIMIT` + a type anchor — WDQS has a
  60s query timeout.
- **`search` ranks by label match only, no P31 filter** — it returns multiple same-label
  items. Disambiguate with `entity`/`xrefs` before pinning a QID.
- **REST is one item per request (no batching).** For reading many QIDs at once, prefer
  a single `sparql` query over looping `xrefs`.
- **`reconcile` is third-party** (307-redirects reconci.link → wmcloud.org; the script
  uses the GET form to avoid the POST-downgrade trap). Treat as best-effort.

## Auth / env-vars

- **No API keys needed.** Every subcommand (including `reconcile`) is keyless against
  Wikimedia production (or WMCloud for `reconcile`).
- **TLS:** the script auto-locates a CA bundle (default trust store, else
  `/etc/ssl/cert.pem` and other standard system paths) so it works on the python.org
  macOS build even when "Install Certificates.command" was never run. If you somehow
  still get a cert-verification error, the script tells you to either run that installer
  or set `SSL_CERT_FILE`, e.g.:
  ```
  SSL_CERT_FILE=/etc/ssl/cert.pem wikidata.py search "prime ideal"
  ```
- **Politeness (built in):** descriptive UA on every call, `maxlag=5` on Action-API
  calls, a short timeout, and one retry with backoff on 429/5xx. SPARQL/REST 4xx and TLS
  failures are reported immediately (not retried) with a clear message + nonzero exit.
