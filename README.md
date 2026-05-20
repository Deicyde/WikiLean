# WikiLean

Categorize WikiProject Mathematics pages by whether (and where) they have been formalized in Lean, then host a Wikipedia mirror annotated with links into Mathlib, Physlib, and other formal libraries.

## Goals

1. **Catalog** — enumerate the set of Wikipedia articles under WikiProject Mathematics and, for each, identify the corresponding formalization(s) in Mathlib / Physlib / other Lean libraries (if any).
2. **Annotate** — produce a mirror of those Wikipedia pages with inline references to the formal definitions, theorems, and proofs that correspond to their content.
3. **Host** — serve the annotated mirror as a public, browsable site.

## Repo layout

- [catalog/](catalog/) — Python pipeline that enumerates WikiProject Mathematics articles from the MediaWiki API and writes a per-article JSONL catalog (class, importance, Wikidata QID, raw banner snippet). See [catalog/README.md](catalog/README.md).

## Status

- **2026-05-17:** Catalog pipeline scaffolded; verified 29,134 talk pages transclude `Template:WikiProject Mathematics`.
- **2026-05-18:** First full catalog snapshot ([catalog/data/articles.jsonl](catalog/data/articles.jsonl)) — 29,135 articles, 99.9% with a Wikidata QID, 94.8% with a class rating, 71.9% with importance.
  - Class breakdown: 31 FA · 226 GA · 1,412 B · 4,358 C · 13,655 Start · 7,521 Stub · 408 List · …
  - Importance breakdown: 212 Top · 952 High · 4,361 Mid · 15,367 Low · 8,199 unrated
- **2026-05-19:** Added Wikidata P31 (`instance of`) lookup — 26.4% of catalog articles are biographies (`Q5`). High-value pilot subset materialized: [catalog/data/pilot.jsonl](catalog/data/pilot.jsonl) (FA/GA/B × Top/High), **429 articles = 354 concepts + 75 biographies**.
- **2026-05-19 (later):** Mathlib tagging pipeline online. [catalog/tag_with_mathlib.py](catalog/tag_with_mathlib.py) spawns parallel Claude agents (via `claude-agent-sdk`, Max-plan auth — no API key) that grep and read your local mathlib4 clone to identify formalizing declarations. First pilot run: **70.9% (251/354) of concept articles have ≥1 verified Mathlib match**, 0 errors, ~15 min wall-clock, ~$48 equivalent (drawn from Max plan, not API).

Next: extend tagging to the rest of the catalog (or a wider sample), and start the annotation pillar — rendering Wikipedia pages with inline Mathlib links.
