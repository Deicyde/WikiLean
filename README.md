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

Next: cross-reference catalog entries against Mathlib (and other formal libraries) to mark which articles have formalizations.
