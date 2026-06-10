# WikiLean

**Live at [wikilean.jackmccarthy.org](https://wikilean.jackmccarthy.org)** — an annotated mirror of Wikipedia's mathematics articles, with each definition, proposition, theorem, and example mapped (where possible) to its formalization in **[Mathlib4](https://leanprover-community.github.io/mathlib4_docs/)**, the Lean 4 mathematics library.

A reader can scan an article and see at a glance which statements are formalized (green), partially formalized (yellow), or not yet formalized (red), with a one-click link out to the Mathlib declaration. A formalizer can use the same view as a coverage map: "what's a notable concept Mathlib hasn't reached yet?"

## Current status (June 2026)

- **709 articles** annotated across 12 Mathlib areas (Analysis, Algebra, Topology, …), with **~29,500 individual annotations**: 28% formalized, 14% partial, 58% not formalized.
- **Live editable wiki** — sign in with GitHub at the site to add/correct/discuss annotations directly in-context on any article.
- **Two complementary data layers** keyed to the same Wikidata entities:
  - *Annotation layer* — per-article, span-level (W3C Web Annotation Data Model export at [`site/out_w3c/`](site/out_w3c/)).
  - *Concept layer* — per-QID, concept → primary Mathlib declaration ([RDF/Turtle](catalog/data/wikilean_mathlib.ttl), 815 entries).
- **Upstream contributions in flight:**
  - A [Wikidata property proposal](docs/wikidata_property_proposal.md) for **"Mathlib declaration"** (modeled on the accepted Metamath statement ID P12888).
  - PRs adding `@[wikidata]` tags to Mathlib4 declarations themselves (the inverse direction of the proposed property).

## Architecture (briefly)

```
        Wikipedia (live)                        Mathlib4 (source)
              │                                       │
              ▼                                       ▼
  ┌───────────────────────────────────────────────────────────────┐
  │  Local pipeline (Python)                                       │
  │   catalog/        WikiProject Math enumeration + AI tagging   │
  │   site/           render.py · 2-agent annotation pipeline ·   │
  │                   moderation mode · W3C/RDF exports           │
  └───────────────────────────────────────────────────────────────┘
              │
              │   seed:delta / seed:refresh (incremental, edit-safe)
              ▼
  ┌───────────────────────────────────────────────────────────────┐
  │  wiki/   Cloudflare Worker (Hono · Drizzle · better-auth)      │
  │          D1 articles + revisions   KV cache   GitHub OAuth     │
  │          serves wikilean.jackmccarthy.org dynamically          │
  └───────────────────────────────────────────────────────────────┘
```

The Worker reads each article's current annotations from D1, injects the in-page review editor when you're signed in, and persists edits as new rows in `revisions`. The seeding pipeline is **edit-safe**: it never overwrites an article that has a real user revision.

## Repository layout

| Path | What's there |
|---|---|
| [`catalog/`](catalog/) — see [catalog/README.md](catalog/README.md) | Catalog of WikiProject Math articles, AI Mathlib-tagging, concept layer, RDF export, Wikidata enrichment. |
| [`site/`](site/) | Annotation pipeline (`render.py`, `batch_annotate.py`, `update_old_annotations.py`), local review editor (`serve_review.py`), W3C export, sources for the static fallback. |
| [`wiki/`](wiki/) — see [wiki/README.md](wiki/README.md) | Cloudflare Worker + D1 backend that serves the live editable site. |
| [`docs/`](docs/) | Long-form docs (Wikidata property proposal, …). |

## How to contribute

Three independent paths, you can pick any:

1. **Annotate articles directly on the live site.** Sign in with GitHub at [wikilean.jackmccarthy.org](https://wikilean.jackmccarthy.org), open any article, hover a highlight to edit it, or select text to add a new one. Edits are saved to D1 and visible to the next reader. See [CONTRIBUTING.md](CONTRIBUTING.md#annotating-on-the-live-site).
2. **Improve the pipeline / engine.** Patches to `site/`, `wiki/`, or `catalog/` welcome. See [CONTRIBUTING.md](CONTRIBUTING.md#code-contributions).
3. **Help upstream.** Vote on the Wikidata property proposal once it's posted, or review the in-flight Mathlib `@[wikidata]` PRs (see the docs).

## License

Code: MIT. Annotation data is published under CC0 (it's a description of Wikipedia + Mathlib, both public). Article text shown on the site remains under the original CC BY-SA terms of the upstream Wikipedia source.

If you contribute, please also read the **data & research notice** and the **token-donation policy** in [CONTRIBUTING.md](CONTRIBUTING.md) — they cover how edit metadata may be studied and how donating compute will work.
