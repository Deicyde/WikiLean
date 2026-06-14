# WikiLean reviewer/moderator skills

Search tools for the AI bots that review and moderate WikiLean's
Wikipedia↔Mathlib annotations. Each skill is a `SKILL.md` (frontmatter +
guidance the agent loads on demand) wrapping a **stdlib-only** Python CLI — no
`requests`, no venv, so the harness reviewer agents, the `site/moderate.py`
pipeline (via Bash), and you at the terminal can all run them with plain
`python3`. All endpoints were live-probed when authored (2026-06-14).

| Skill | CLI | Subcommands | Use when |
|---|---|---|---|
| **mathlib-search** | `mathlib_search.py` | `loogle`, `semantic`, `decl` | Find or verify a Mathlib4 declaration: Loogle for name/type/conclusion patterns (zero hallucination), `semantic` for natural-language prose, `decl` to confirm a cited name is real + its module. |
| **wikidata-search** | `wikidata.py` | `search`, `entity`, `xrefs`, `sitelinks`, `sparql`, `reconcile` | Resolve a concept→QID, inspect its formal-library cross-refs (Metamath/nLab/MathWorld/ProofWiki/defining-formula — the `@[wikidata]` / property-proposal workflow), or run graph/coverage queries. |
| **wikipedia-search** | `wikipedia.py` | `search`, `summary`, `section`, `revid` | Quick source-article context during annotation review: find the canonical article, one-paragraph summary + QID, one section's plaintext to verify an anchor, current revid for drift. Complements `site/render.py` (does not duplicate the full render). |

## Quick examples (all return live data)

```sh
python3 .claude/skills/mathlib-search/mathlib_search.py loogle "Real.sin, Continuous"
python3 .claude/skills/mathlib-search/mathlib_search.py semantic "commutativity of addition"
python3 .claude/skills/mathlib-search/mathlib_search.py decl Nat.add_comm
python3 .claude/skills/wikidata-search/wikidata.py search "prime ideal"
python3 .claude/skills/wikidata-search/wikidata.py xrefs Q11518          # Pythagorean thm: all cross-refs
python3 .claude/skills/wikidata-search/wikidata.py sparql "SELECT ?i ?iLabel WHERE { ?i wdt:P12888 ?m. SERVICE wikibase:label { bd:serviceParam wikibase:language 'en'. } } LIMIT 5"
python3 .claude/skills/wikipedia-search/wikipedia.py summary "Prime ideal"
python3 .claude/skills/wikipedia-search/wikipedia.py section "Prime ideal" "Examples"
```

Add `--json` for machine output (and `--help` on any subcommand).

## Semantic Mathlib engines

`mathlib_search.py semantic --engine <…>`:

- **leansearch** (default, keyless) — `leansearch.net`; returns the formal
  signature **plus** an LLM informal name/description, ideal for "does this decl
  formalize this statement?" judgments.
- **leanfinder** (keyless) — HuggingFace-hosted; may rotate.
- **numina** (keyless) — Project Numina's Leandex.
- **leanexplore** (optional) — needs `LEANEXPLORE_API_KEY` (`Authorization:
  Bearer`); errors with a clear "set the env var" message if unset.

## Conventions

- **User-Agent**: every tool sends `WikiLean-reviewer/1.0
  (https://github.com/Deicyde/WikiLean; wikilean@jackmccarthy.org)`; Wikimedia
  calls add `maxlag=5`. One retry on 429/5xx with backoff.
- **WDQS** serves the *main* graph only (post-March-2025 split) — fine for math.
- **Caching**: `mathlib-search` caches the ~62 MB declaration-data index under
  `.cache/` (gitignored, ETag-revalidated). `decl` also reuses WikiLean's
  prebuilt shards at `wiki/public/assets/decl-index/` when present.

## Not yet wired into the live pipeline

These are loadable by harness reviewer agents today. The `site/moderate.py`
moderation pipeline's Agent 2 currently gets only `Read/Grep/Glob` over the
Mathlib checkout — adding Bash + these CLIs (or an MCP server) is the follow-up
that lets it verify QIDs and decls *as it annotates*.
