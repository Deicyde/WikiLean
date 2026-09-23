# WikiLean

**Live at [wikilean.jackmccarthy.org](https://wikilean.jackmccarthy.org)** — an annotated mirror of Wikipedia's mathematics articles, with each definition, proposition, theorem, and example mapped (where possible) to its formalization in **[Mathlib4](https://leanprover-community.github.io/mathlib4_docs/)**, the Lean 4 mathematics library.

A reader can scan an article and see at a glance which statements are formalized (green), partially formalized (yellow), or not yet formalized (red), with a one-click link out to the Mathlib declaration. A formalizer can use the same view as a coverage map: "what's a notable concept Mathlib hasn't reached yet?"

## Current status (September 2026)

- **The editable wiki is live.** Sign in with GitHub to add, correct, or discuss
  annotations directly in an article. D1 remains the canonical store for article
  annotations, revisions, moderation state, and the community-edge overlay.
- **The SQLite and immutable Brain release stack is merged**, including PRs #32–#34
  on 2026-09-21, but remains inactive in production. The snapshot/API check on
  2026-09-23 still showed the 2026-08-28 Brain without a release identity;
  [`/assets/brain/current.json`](https://wikilean.jackmccarthy.org/assets/brain/current.json)
  still returned 404 on 2026-09-23. The repository has no automatic deploy-on-merge workflow.
- **Private migration evidence recovered on 2026-09-23:** the candidate source plan
  now covers 114 sources and all 43 input groups, including completed Kerodon and
  OpenAlex evidence. The current native Linux runtime is qualified and three search
  indexes are prepared. The first full pack, legacy baseline, two full replay builds,
  policy decisions, and production activation remain unfinished. About 10 GiB of free
  disk space currently blocks the large runs. See the
  [September 23 handoff](docs/BRAIN-SQLITE-HANDOFF-2026-09-23.md) and
  [remaining queue](docs/ROADMAP.md#current-brain-execution-queue-updated-2026-09-23).
- **Live annotation snapshot checked on 2026-09-21:** 778 articles and 37,936
  annotated results: 27.9% formalized and 14.1% partial.
- **Two complementary editable exports** remain keyed to Wikidata entities:
  the per-article [W3C annotation layer](site/out_w3c/) and the per-QID
  [RDF concept layer](catalog/data/wikilean_mathlib.ttl).
- **Upstream work:** the [Wikidata property proposal](docs/wikidata_property_proposal.md)
  for a Mathlib declaration identifier and Mathlib `@[wikidata]` tags provide the
  two directions of the same mapping.

## Architecture

WikiLean has two deliberately separate data planes:

1. **Mutable collaboration plane.** The Cloudflare Worker in `wiki/` serves articles,
   authentication, review tools, REST endpoints, and MCP. D1 is canonical for article
   annotations and revisions; KV holds caches. Seeding is edit-safe and never overwrites
   an article with a real user revision.
2. **Immutable Brain plane.** The Brain is a graph of mathematical **cells**. A cell
   combines organs that denote the same mathematical object: Wikidata concepts,
   Mathlib declarations, WikiLean articles, external-database pages, and literature
   statements. Mathlib folders are **supercells**; provenance-bearing relationships
   between cells are aggregated as **synapses**. [`brain/SCHEMA.md`](brain/SCHEMA.md)
   is the normative data contract.

The target immutable release path implemented by the merged tooling is:

```text
reviewed sources + explicit sealed acquisitions
                       |
                       v
 source plan + receipts + normalization lineage
                       |
                       v
       deterministic, network-free replay
                       |
          +------------+-------------+
          |                          |
          v                          v
 reviewable JSONL graph       SQLite query projection
          |
          v
 cells + synapses + shards + release-neutral /brain page
          |
          v
 frozen release: logical release_id + exact manifest_sha256
          |
          v
 reviewed public baseline + activation evidence bundle
          |
          v
 explicit operator promotion to the Cloudflare Worker
          |
          +--> /brain and immutable release assets
          +--> /api/brain/*
          +--> POST /mcp
```

Acquisition is separate from replay. Networked tools first capture immutable source
objects and evidence; the reducer then runs offline from an explicit inventory. JSONL is
the reviewable source of truth, while SQLite is a generated local query projection and is
never a Cloudflare asset. The merged machinery supports sealed offline replay releases,
but the current nightly still freezes the compatibility profile; no full-corpus v2/v3
replay has been accepted as production authority.

Every frozen release has two identities. `release_id` names the logical graph content;
`manifest_sha256` names the exact `release.json` bytes and therefore the public namespace
`/assets/brain/releases/<manifest_sha256>/`. A v2 selector at
`/assets/brain/current.json` chooses the current and optional previous manifest. The
Worker resolves that selector once per request, verifies the exact manifest, and verifies
every manifest-declared JSON asset before parsing it. The browser follows the same rule.

The nightly may acquire approved inputs, build, test, freeze, and shadow-stage a candidate,
but it cannot deploy. Production promotion requires a reviewed public baseline, a reviewed
activation bundle, an unchanged clean `main`, exact toolchain identities, a durable journal,
and a release-qualified canary. See the [Brain API](docs/BRAIN-API.md),
[authority contracts](brain/authority/README.md), and
[release runbook](docs/BRAIN-RELEASE-RUNBOOK.md).

### Release state is four separate facts

| State | Current value |
|---|---|
| Architecture code merged to `main` | Yes |
| New Worker/site bundle deployed | No |
| Immutable Brain release assets published | No production v2 selector observed |
| Production selector activated | No |

A successful merge or CI run changes only the first row. It does not deploy Worker code,
publish Brain data, or authorize activation.

## Repository layout

| Path | What's there |
|---|---|
| [`catalog/`](catalog/) — see [catalog/README.md](catalog/README.md) | Catalog of WikiProject Math articles, AI Mathlib-tagging, concept layer, RDF export, Wikidata enrichment. |
| [`site/`](site/) | Annotation pipeline (`render.py`, `batch_annotate.py`), local review editor (`serve_review.py`), W3C export, sources for the static fallback. |
| [`wiki/`](wiki/) — see [wiki/README.md](wiki/README.md) | Cloudflare Worker + D1 backend that serves the live editable site. |
| [`brain/`](brain/) | Brain schema, deterministic reducer, acquisition and replay contracts, SQLite projection, release builder, and verification tests. |
| [`site/ops/`](site/ops/) | Shadow nightly, public-baseline freezer, activation evidence, promotion journal, exact release promoter, and canary. |
| [`bot/`](bot/) | Human-gated automation for upstream Mathlib `@[wikidata]` work. |
| [`manage/`](manage/) | Coverage and centrality control plane for choosing the next work. |
| [`wikifunctions/`](wikifunctions/) | Experimental specification and verification work. |
| [`docs/`](docs/) | Long-form docs (Wikidata property proposal, …). |

## How to contribute

Three independent paths, you can pick any:

1. **Annotate articles directly on the live site.** Sign in with GitHub at [wikilean.jackmccarthy.org](https://wikilean.jackmccarthy.org), open any article, hover a highlight to edit it, or select text to add a new one. Edits are saved to D1 and visible to the next reader. See [CONTRIBUTING.md](CONTRIBUTING.md#annotating-on-the-live-site).
2. **Improve the pipeline / engine.** Patches to `site/`, `wiki/`, or `catalog/` welcome. See [CONTRIBUTING.md](CONTRIBUTING.md#code-contributions).
3. **Help upstream.** Vote on the Wikidata property proposal once it's posted, or review the in-flight Mathlib `@[wikidata]` PRs (see the docs).

## License

Code: MIT. Annotation data is published under CC0 (it's a description of Wikipedia + Mathlib, both public). Article text shown on the site remains under the original CC BY-SA terms of the upstream Wikipedia source.

If you contribute, please also read the **data & research notice** and the **token-donation policy** in [CONTRIBUTING.md](CONTRIBUTING.md) — they cover how edit metadata may be studied and how donating compute will work.
