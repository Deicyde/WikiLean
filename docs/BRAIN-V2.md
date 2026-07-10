# BRAIN v2 — external databases, atomic units, filters, live sync, agent API

> Design doc for the five-axis Brain upgrade (2026-07-10). Companion to `brain/SCHEMA.md`
> (the binding data contract — v2 amendments live THERE; this doc explains why and how).
> Predecessors: `docs/BRAIN.md` (v1 vision), `docs/BRAIN-STATUS.md`, `docs/BRAIN-EDITS-ROADMAP.md`.

## The five axes (Jack, 2026-07-10)

1. **Database integration** — external math databases become real, explorable nodes with
   their own internal-link edges (nLab, Stacks, LMFDB, ProofWiki, PlanetMath, EoM, OEIS,
   Kerodon, MathWorld, DLMF), not just `xref:` string targets.
2. **Atomic units** — `Mathlib.LinearAlgebra.Matrix.Defs` ↔ `Q44337` ↔ WikiLean
   `Matrix_mathematics` render and query as ONE unit, not three disconnected nodes.
3. **Human UI** — clicking a concept shows Lean code, article snippets, Wikidata
   description, LMFDB knowl text, Stacks statements, nLab Idea sections; node-level
   FILTERS reduce the graph to tractable views (e.g. only `@[wikidata]`-tagged decls).
4. **Live updates** — the Brain grows nightly with Mathlib, Wikidata, and the external
   DBs; an AI agent team (propose) + deterministic verifier (approve) manages growth.
5. **Wikibrain API + MCP** — a remote, documented, benchmarked query surface so
   AI-for-math agents can jump informal ↔ formal mid-proof. Optimize for the benchmark:
   does Brain access measurably improve agent outcomes on Lean/math tasks?

## Verified ingestion matrix (feasibility research 2026-07-10, all paths live-tested)

| db | access path | pages | edges | snippet license | strategy |
|---|---|---|---|---|---|
| nlab | git `ncatlab/nlab-content` (daily push) | ~20.7k | `[[wikilink]]` + `[[!redirects]]` alias map | attribution (informal, per HomePage) | full |
| stacks | git `stacks/stacks-project` `tags/tags` + chapter `.tex` | 21,436 tags | `\ref{}` (statement vs proof) | GFDL + attribution | full |
| lmfdb | Postgres mirror `devmirror.lmfdb.xyz:5432` lmfdb/lmfdb, table `kwl_knowls` | 1,717 live knowls | precomputed `links` text[] (5,216) + `{{KNOWL()}}` regex | CC-BY-SA 4.0 | full |
| proofwiki | nightly dump `proofwiki.org/xmldump/latest.xml.gz` (36 MB) | ~46k (collapse `/Proof_N` subpages) | wikilinks from dump wikitext | CC-BY-SA 3.0 (robots: ai-train=no — reference use only) | full |
| planetmath | git clone ~63 `planetmath/*` MSC repos | ~10k `.tex` | `\pmrelated` + NNexus anchors | CC-BY-SA | full |
| eom | open MediaWiki API `encyclopediaofmath.org/api.php` (`generator=allpages&prop=links`) | 16,234 | prop=links (~35 paginated calls) | Springer-copyrighted body — **no content** | links+titles |
| oeis | `names.gz` daily dump + per-entry JSON for anchored subset | ~370k ids (names only) | `Cf. A\d{6}` xrefs (anchored subset only) | CC-BY-SA 4.0 | names + anchored |
| kerodon | Gerby JSON `/data/tag/<T>/structure` (2 roots: 0000, 02GZ) + `content/full` href regex | 7,509 tags | `/tag/` hrefs | **unlicensed** — ids/titles/edges only, deep-link out | links+metadata |
| mathworld | Wikidata P2812 only (6,763 ids); one-time sitemap inventory OK | 17,236 slugs | **none** (ToS forbids crawling; Wolfram IP-blocks) | proprietary — **no content, no crawl** | ids-only |
| dlmf | polite crawl of ~600 section pages for hrefs | ~600 sections | relative hrefs (equation-granular) | NIST bars redistribution — **no content** | ids+titles+edges |

Wikidata property coverage (join seeds): nlab P4215 4,331 · mathworld P2812 6,763 ·
proofwiki P6781 2,556 · eom P7554 1,215 · planetmath P7726 938 · oeis P829 444 ·
dlmf P11497 199 · lmfdb P12987 109. Stacks/Kerodon have NO Wikidata property (proposal
opportunity alongside "Mathlib declaration") — they join through Mathlib `@[stacks]`/
`@[kerodon]` attributes (542 + 14 harvested rows).

## Data-model changes (normative text in brain/SCHEMA.md; summary here)

- **New node type `ext`** with id form `xref:<db>:<value>` — deliberately identical to
  the existing xref edge dst strings, so every existing `xref` edge becomes node-to-node
  with ZERO migration, `xref_index.json` keys stay node ids, and the community-edit
  validation path (`brain-edits.ts`) is untouched. Payload:
  `{id, type:"ext", db, label, url, snippet?, snippet_license?, kind_hint?, qid?}`.
- **New edge kind `links`** — directed page→page hyperlink inside one external DB
  (`evidence.context ∈ statement|proof|body|related`), PLUS the concept-level projection:
  when page A → page B and A,B anchor to QIDs, emit `links` concept→concept with
  `evidence.projected=true, via=<db>, src_page, dst_page`. Projection is the
  inter-connectivity win; it never overwrites `relates` (Wikidata P-props stay separate).
- **Node minting policy** (tractability): mint `ext` nodes for (a) every xref target of a
  concept ("anchored"), (b) pages ≤1 link-hop from anchored pages, capped per source
  (`EXT_NODE_CAP`, default 8,000/db). Full corpora live in `catalog/data/external/` for
  querying and future expansion; the Brain graph stays legible.
- **`unit` object on concept nodes** — the atomic unit of axis 2:
  `{qid, label, article:{slug, annotations}, wikipedia_slug, decls:[{name,module,match_kind,confidence}],
    containers:[path...], xrefs:{db:[{id,label?,url}]}, description}` — assembled at build
  time from formalizes/xref edges + grounding. `display.*` stays a hint; `unit` is the
  render/query surface.
- **Facet bitmask `f`** on every node payload, children entry, and labels row (int):
  bit0 gold `@[wikidata]` tag · bit1 `@[stacks]` · bit2 `@[kerodon]` · bit3 any xref ·
  bit4 formalized · bit5 partial · bit6 has WikiLean article · bit7 has literature ·
  bit8 is ext · bit9 lmfdb · bit10 nlab · bit11 mathworld · bit12 proofwiki ·
  bit13 stacks-tag(xref) · bit14 oeis · bit15 has snippet. Documented in SCHEMA.md;
  UI + API filter on it without new artifacts.
- **Wikidata descriptions** fetched at build time (wbgetentities, batched) into
  `catalog/data/wikidata_descriptions.json` → `node.unit.description`.

### External data contracts (produced by `brain/ingest/<db>.py`, consumed by build_common)

```
catalog/data/external/<db>_pages.jsonl   {"db","id","title","url","snippet"?,"aliases"?[],"qid"?,"kind_hint"?}
catalog/data/external/<db>_links.jsonl   {"db","src","dst","context"}        # native ids
catalog/.cache/external/<db>/            raw dumps/clones (gitignored)
```
First line of each jsonl = `{"_meta":{...counts, fetched_at, source_pin}}`. Adapters are
deterministic, atomic-write, fail-soft (a failed fetch leaves the previous file intact),
rate-limit compliant (LMFDB crawl-delay 30 via Postgres instead; OEIS delay 10;
DLMF ~1 req/s; Kerodon polite evening crawl, cached).

## Publish path (axis 4 unblocker)

Brain shards are static Worker assets → previously required a manual deploy, which the
nightly refused (WIP risk). v2: **clean-tree-gated deploy** — the nightly rebuilds
shards and deploys ONLY IF `git status --porcelain wiki/src` is empty AND
`npx tsc --noEmit` passes AND `brain/test_acceptance.py` is green. R2 was evaluated and
is the better home (no deploys at all, ~$0 on free tier) but **R2 is not enabled on the
account** (dashboard opt-in required — Jack action item). The serving code keeps an
ASSETS-first helper so flipping to R2-first later is a localized change.

## Nightly brain sync (axis 4)

`site/ops/brain-nightly.sh` (launchd 02:20, before the moderation jobs) with per-source
staleness windows in `nightly.env`: daily = nlab pull / proofwiki dump / oeis names /
stacks pull / harvest_mathlib_tags / fetch_crossrefs; weekly = lmfdb pg / eom API /
planetmath pull; monthly = kerodon / dlmf / mathworld sitemap inventory. Then:
`fold_proposals → build_nodes/edges → test_acceptance → build_shards → build-public →
gated deploy`. Rollups rebuild only when TheoremGraph inputs change (they are pinned).

**Agent team** (`brain/sync_agents.py`, SDK, Max auth, budget-gated): the LLM step
proposes, never writes: (1) *cartographer* — title-matches unanchored external pages ↔
Wikidata concepts → `brain/proposals/ext_anchor_*.jsonl`; (2) *groundskeeper* — new
Mathlib decls since pin ↔ concepts (reuses batch_annotate Agent-2 tool surface + the
brain MCP tools); (3) *skeptic* — the existing adversarial verdict pass. All folds go
through `fold_proposals.py` (existence oracle + live Wikidata check + any-reject veto).
Integration: `site/search_tools.py` gains brain tools so the EXISTING nightly annotation
agents also query the Brain while writing annotations.

## Wikibrain API + MCP (axis 5)

New Worker routes (all read-only, cached, documented at `/brain/api`):

- `GET /api/brain/unit?key=` — resolve ANY member key (QID | decl:… | bare decl name |
  slug | xref:db:id | title) → the atomic unit card. **The flagship agent call.**
- `GET /api/brain/transfer?q=&direction=informal_to_formal|formal_to_informal` —
  informal↔formal jump: concept text/QID/slug → ranked decls with modules + docs URLs +
  match_kind + confidence; decl → concepts/articles/snippets.
- `GET /api/brain/neighborhood?id=&kinds=&dir=&limit=` — typed neighborhood (shard-backed,
  truncation flagged).
- `GET /api/brain/snippets?id=` — every stored content snippet for a unit (Lean source,
  knowl, Stacks statement, nLab Idea, Wikidata description) with per-snippet license.
- `GET /api/brain/filter?f=<mask>&type=&limit=&cursor=` — facet-filtered node enumeration.
- Existing `/api/brain/node|search|edges` unchanged.

**MCP**: dependency-free streamable-HTTP JSON-RPC endpoint `POST /mcp` on the Worker
(stateless; initialize / tools/list / tools/call; no Durable Objects, no SDK). Tools:
`brain_search, brain_node, brain_unit, brain_transfer, brain_neighborhood,
brain_snippets, brain_filter, decl_exists`. Connect:
`claude mcp add --transport http wikibrain https://wikilean.jackmccarthy.org/mcp`.
`RESERVED` gains `mcp`; rate-limited like the bearer path.

## Benchmark (axis 5's referee)

`bench/`: task generator + runner + scorer. Tasks derived from held-out gold pairs
(`@[wikidata]` tag rows; grounding formalizations with `match_kind=exact` and high
confidence; annotation statements with formalized status): (T1) concept/statement →
exact Mathlib decl name; (T2) decl → QID + enwiki slug; (T3) statement → "formalized or
not" + witness. Arms: `no_tools` vs `wikibrain_mcp` (same model, same prompt). Scored by
exact match + decl-existence oracle. Runner shells `claude -p --mcp-config` (Max auth,
ANTHROPIC_API_KEY unset). The API design target: **T1/T2 lift is the number we optimize**.

## UI (axis 3) — in site/build_brain_page.py

- Panel gains a **unit card** header (one identity: article ∘ QID ∘ decls ∘ xref chips)
  and a **Sources accordion**: Wikidata description (build-time), Wikipedia lead
  (client-side REST summary fetch, CORS-safe, cached), WikiLean article annotations
  (already present), Lean code + docstring (already present), LMFDB knowl (KaTeX),
  Stacks statement, nLab Idea, PlanetMath para — each with license/attribution footer,
  ext sources deep-link out.
- **Filter bar (node-level)**: "show only" chips driven by facet bits — `@[wikidata]`
  tagged · has cross-refs · formalized · by external DB · concepts/decls/ext. Filters
  HIDE NODES (and their edges) in level/web/ego views — reducing to a tractable subgraph,
  not just decluttering edges.
- **X-ref explorer view**: a new global view rendered from
  `assets/brain/views/xref_explorer.json` (compact: all tagged/cross-referenced nodes +
  inter-edges) — the "cross-ref explorer for Mathlib" Jack asked for.
- ext nodes render as first-class bubbles (db-colored ring), ego-view navigable.

## Sequencing (this session)

S1 me: SCHEMA amendments, registry entries, ingest contract + `brain/ingest/common.py`.
S2 parallel agents: adapters (2 batches) · build_common/build_shards (units, ext, facets,
projections) · UI · Worker API+MCP+docs+tests · nightly+agents · bench.
S3 integrate: run ingest, rebuild data, `test_acceptance`, shards, build-public,
typecheck, wiki tests, deploy, live verify, bench smoke.
S4 adversarial review workflow → fixes → commit/push → report.

## Cost & services (for Jack)

- No new paid services required today. Action item: enable R2 in the dashboard
  (free tier covers 145 MB × daily churn easily) to get the nightly off the deploy path.
- External ingest is bandwidth-only (git pulls + one 36 MB dump/night + tiny API calls).
- Agent-team tokens ride the existing Max subscription budget gates; benchmark runs are
  opt-in and metered. If benchmark lift is real, a small paid API budget (~$20–50/mo)
  for a hosted always-on triage agent is the first thing worth buying. Second:
  Workers Paid ($5/mo) if KV/queue usage grows past free tier.
