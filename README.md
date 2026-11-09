# WikiLean

**Connect mathematical writing to formal proofs.** WikiLean links passages in
Wikipedia’s mathematics articles to Lean declarations, with evidence readers can
inspect and improve.

[Browse articles](https://wikilean.jackmccarthy.org/articles) ·
[Contribute](https://wikilean.jackmccarthy.org/contribute) ·
[Explore the Brain](https://wikilean.jackmccarthy.org/brain)

## Contribute

- **Check a match:** open an article, select a passage, and confirm or report its link.
- **Add or correct a tag:** sign in, accept the CC0 agreement, and save a declaration
  with its relation to the passage. Tags are public and attributed.
- **Use your own agent:** create an account for it at
  [/agents](https://wikilean.jackmccarthy.org/agents). Keep your model credentials
  and spending limits on your own machine.
- **Work on the pipeline or Brain:** follow the
  [contributor guide](docs/START-CONTRIBUTING.md). Full pipeline results still use
  operator intake; the general submission endpoint is not live.

Matches are preliminary. Lean verifies formal proofs, but the match between a
formal statement and a passage still needs review. An unsuccessful search covers
only the libraries and versions searched.

## Find your way around

| Area | Purpose | Start here |
|---|---|---|
| `wiki/` | Live Worker, reader, accounts, community tags | [Worker guide](wiki/README.md) |
| `scripts/articles/` | Formalization reader builds and contribution intake | [Articles](docs/ARTICLES.md), [intake](docs/INTAKE.md) |
| `brain/` | Concept, code, database, and literature graph | [Brain API](docs/BRAIN-API.md), [schema](brain/SCHEMA.md) |
| `site/`, `catalog/` | Annotation, moderation, upstream data, asset sources | [Catalog](catalog/README.md) |
| `bot/` | Human-reviewed `@[wikidata]` contributions to Mathlib | [Bot sources](bot/) |
| `manage/` | Coverage and worklists | [Management](manage/README.md) |

For development commands and the contribution policy, read
[CONTRIBUTING.md](CONTRIBUTING.md). For project direction, read the canonical
[roadmap](docs/ROADMAP.md). [HANDOFF.md](HANDOFF.md) is the operator and agent entry point.

## Data and releases

D1 is canonical for article annotations, revisions, and community edits. Files in
`site/annotations/` are caches and backups, never a replacement for live edits.
The formalization reader uses a published record-set store plus community tags
from D1. The Brain serves immutable, verified releases with a community overlay.

Canonical annotations use [Literature Links](docs/COMPANION-NATIVE.md). The native
conversion and contributor release are documented in the
[November 8 activation record](docs/BRAIN-ACTIVATION-2026-11-08.md).
Historical activation records describe past deployments; check the live
[/api/version](https://wikilean.jackmccarthy.org/api/version) for current identity.

All production changes follow the [release runbook](docs/BRAIN-RELEASE-RUNBOOK.md).
CI validates changes; it does not deploy them.

## Licence and research

Code is MIT; WikiLean annotation data is CC0. Wikipedia text remains CC BY-SA,
and Lean code retains its library’s licence. Read the
[data and research notice](CONTRIBUTING.md#data--research-notice) before contributing.
