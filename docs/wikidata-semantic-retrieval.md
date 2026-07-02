# Semantic Wikidata retrieval for Agent 2

*Design sketch — 2026-07-02. Status: proposal, not yet built.*

## The problem

Agent 2 (the Mathlib matcher in [`site/batch_annotate.py`](../site/batch_annotate.py),
tools wired in [`site/search_tools.py`](../site/search_tools.py)) has **genuinely semantic
retrieval for Mathlib** — `mathlib_semantic` runs LeanSearch (`leansearch.net`), an
embedding search, with LeanFinder / Numina / LeanExplore as alternate engines.

Its **Wikidata** side is keyword-only. The one Wikidata *search* tool it holds,
`wikidata_search`, is `action=wbsearchentities` — a label/alias **prefix** match
([`.claude/skills/wikidata-search/wikidata.py:231`](../.claude/skills/wikidata-search/wikidata.py)).
There is no embedding/vector search over Wikidata anywhere in the repo. The skill CLI
*does* have a `sparql` subcommand (WDQS graph traversal), but it is **not exposed to the
agent** — `build_search_server()` wires only `wikidata_search` and `wikidata_xrefs`.

This asymmetry is the mechanism behind the **broad-QID failure mode** (see the
`wikidata_tag_quality_loop` memory): `wbsearchentities` ranks by label match, so for a
concept phrased differently from its Wikidata label — or one whose label collides with a
broader parent — the agent grabs a plausible-but-too-broad QID. Label matching has no
notion of *meaning*, only *spelling*.

## What we already have (this changes the calculus)

The math-concept universe is **already curated on disk** — we do not need to embed all of
Wikidata:

| File | Rows | Shape |
|---|---|---|
| [`catalog/data/wikidata_universe.jsonl`](../catalog/data/wikidata_universe.jsonl) | **11,681** | `{qid, label, classes:[QID…], enwiki_slug}` |
| [`catalog/data/concept_graph.json`](../catalog/data/concept_graph.json) | 1,376 | `{qid, label, slug, primary_decl, module, status, importance}` |
| [`catalog/data/wikidata_crossrefs.json`](../catalog/data/wikidata_crossrefs.json) | — | per-QID formal cross-refs |

**11.7k vectors is nothing.** Brute-force cosine over 12k rows is sub-millisecond in
NumPy; no FAISS, no Vectorize, no external index. The whole thing fits in a few MB on
disk and loads into the agent process. This is the decisive fact: the expensive part of
"semantic search over Wikidata" (deciding *which* entities to embed) is already done.

## The two options

### Option A — wire the existing SPARQL mode in as a 6th tool

Graph-semantic, not embedding-semantic. `wikidata.py` already implements `cmd_sparql`
against WDQS. Exposing it lets the agent ask structural questions:

> *"QIDs that are `wdt:P31/P279* Q(some math class)` and whose label/altLabel contains X"*

This directly attacks the broad-QID problem by letting the agent **constrain candidates to
the right branch of the class hierarchy** and then verify with `wikidata_xrefs`.

**Do NOT expose raw SPARQL to Agent 2.** Agent 2 ingests Wikipedia text — a prompt-injection
surface — which is exactly why `search_tools.py` gives it named, fixed-argv, read-only tools
instead of Bash. Raw WDQS is an SSRF/DoS surface (federated `SERVICE` calls, unbounded
traversals, 60s query timeouts). Expose **parameterized templates** instead: a tool that
takes a concept string + optional class-QID and fills a vetted, `LIMIT`-bounded query.

- **Effort:** ~half a day. New `cmd_sparql_concept` (template-filling) in the skill CLI +
  one `@tool` in `search_tools.py`.
- **Semantic strength:** structural/graph — great at *narrowing by type*, blind to
  *phrasing*. If the article calls it "the squeeze theorem" and Wikidata's label is
  "pinching theorem," a label filter still misses.
- **Network:** live WDQS call per lookup (~1–3s, occasionally rate-limited/503).

### Option B — local embedding index over the math-QID corpus

Real semantic retrieval, symmetric with what Mathlib already has. Build an embedding
index over the 11.7k-row universe once; query it locally per lookup.

**Build (offline, nightly or on-demand):**
1. For each QID in `wikidata_universe.jsonl`, compose an **embed-text** that carries
   meaning, not just the label. The composition matters more than the model:
   `label` + Wikidata `description` + **parent-class labels** (resolve `classes[]` QIDs to
   names) + optionally the enwiki one-paragraph summary (we already have the
   `wikipedia-search` skill for this). Embedding the class chain is what separates
   "continuity (Q170058)" from its broader parents.
2. Embed each text → an `(N, d)` float32 matrix + a parallel QID/label list. Persist as
   `catalog/data/wikidata_embeddings.npz` (+ a `.meta.jsonl`).
3. Version the artifact; rebuild in the nightly (`site/ops/`) when
   `wikidata_universe.jsonl` changes.

**Query (per lookup, fully local):** embed the agent's concept description with the same
model, cosine against the matrix, return top-k `{qid, label, description, score}`.

**Embedding model — the one real constraint.** The Max-plan auth used by the pipeline does
**not** provide an embeddings endpoint (unset `ANTHROPIC_API_KEY`; Messages only). Three
honest choices:
- **Local `sentence-transformers`** (e.g. `all-MiniLM-L6-v2`, 384-d, ~80MB, CPU-fine for
  12k rows). Zero network at query time, zero injection surface, zero marginal cost.
  *Recommended.* Adds a Python dep + a model download.
- **Cloudflare Workers AI embeddings** (`@cf/baai/bge-*`) → the site is already on
  Cloudflare, and `catalog/data` could be embedded via a Worker. But the agent runs
  locally, so this adds a network hop the local option avoids. Better fit if the index
  ever moves server-side.
- **OpenAI `text-embedding-3-small`** — cheapest to wire, but adds a second API credential
  and a per-query network call for no quality win over MiniLM at this scale.

- **Effort:** ~1–2 days (build script + query lib + tool wiring + nightly hook).
- **Semantic strength:** phrasing-robust; the intended win. Weaker at hard type
  constraints than SPARQL.
- **Network:** none at query time (with the local model).

### Option C — hybrid (recommended end state)

They're complementary — embeddings fix *phrasing*, SPARQL fixes *type*. The strongest
tool is **embed to generate candidates, then structurally verify**:

1. `wikidata_semantic(description)` → top-8 candidate QIDs from the local index.
2. Agent picks the best sense from descriptions (already the `wikidata_search`
   disambiguation discipline).
3. `wikidata_xrefs(qid)` (existing) confirms it carries the expected formal cross-refs /
   enwiki sitelink.

No new network dependency beyond the existing `xrefs` call, and the candidate set is
meaning-ranked instead of spelling-ranked.

### Cheap win, orthogonal to both

At annotation time we already know the **article's** enwiki slug. The article-level QID is
a direct `sitelink → QID` lookup (`wikidata.py` already has `cmd_sitelinks`), not a search
at all. Exposing a `wikidata_by_slug` tool gives Agent 2 an exact anchor for the top-level
concept for free; semantic search then only has to work for the *sub-concepts* inside the
article. Worth shipping regardless of A/B/C.

## Concrete wiring (Option B, the recommended core)

**1. New skill subcommand** — `.claude/skills/wikidata-search/wikidata.py`, following the
existing `cmd_*` + `sub.add_parser` + `--json` pattern:

```python
def cmd_semantic(args):
    # loads catalog/data/wikidata_embeddings.npz, embeds args.query with the
    # same local model, cosine top-k, prints [{qid,label,description,score}] --json
    ...
ps = sub.add_parser("semantic", help="embedding search over the math-QID universe")
ps.add_argument("query"); ps.add_argument("--k", type=int, default=8)
ps.set_defaults(func=cmd_semantic)
```

**2. New build script** — `catalog/build_wikidata_embeddings.py`: reads
`wikidata_universe.jsonl`, composes embed-text (label + description + class labels),
writes `wikidata_embeddings.npz`. Add to the nightly (`site/ops/`).

**3. New MCP tool** — `site/search_tools.py`, mirroring the existing `wikidata_search`
wrapper (fixed argv, read-only, single string arg — preserves the injection boundary):

```python
@tool("wikidata_semantic",
      "Find the best-matching Wikidata QID(s) for a concept by MEANING, not just "
      "label spelling. Describe the concept in prose; returns candidates ranked by "
      "semantic similarity with descriptions to disambiguate. Prefer this over "
      "wikidata_search when the concept's phrasing may not match its Wikidata label.",
      {"description": str})
async def wikidata_semantic(args):
    return _text(await asyncio.to_thread(_run_cli, WIKIDATA_CLI, "semantic", args["description"]))
```

Add it to the `tools = [...]` list in `build_search_server()`.

**4. Guidance** — extend `AGENT2_TOOLS_GUIDANCE` in `search_tools.py`: *"Prefer
`wikidata_semantic(description)` to `wikidata_search(label)` when the concept may be phrased
differently from its Wikidata label; confirm the chosen QID with `wikidata_xrefs`."*

Everything else (gating via `WIKILEAN_SEARCH_TOOLS=0`, the `_run_cli` fixed-argv wrapper,
the 45s timeout / 6k output cap) is inherited unchanged.

## Recommendation

Ship in this order:

1. **`wikidata_by_slug`** (hours) — exact article-QID anchor, immediate accuracy win.
2. **Option B** (1–2 days) — local MiniLM embedding index over
   `wikidata_universe.jsonl`; this is the symmetric fix to the LeanSearch/Wikidata gap and
   the corpus already exists.
3. **Option C** — keep `wikidata_xrefs` as the verification step after B; that *is* the
   hybrid, with no extra work.
4. **Option A (templated SPARQL)** — add later if measurement shows residual
   type-confusion that embeddings alone don't resolve. Templated, never raw.

## How we'll know it worked

The `wikidata_tag_quality_loop` already harvests corrections. Instrument the **broad-QID
rate** — corrections that retarget to a *narrower* QID (a `P279`-descendant of the one the
agent picked) — before and after. That's the metric this whole change exists to move.

## Open questions for Jack

- Local `sentence-transformers` dep on the pipeline box — OK, or keep it network-only?
- Embed against Wikidata descriptions, enwiki summaries, or both? (Both is best; enwiki
  summaries cost a fetch per QID at build time — one-time, cacheable.)
- Rebuild cadence: nightly, or only when `wikidata_universe.jsonl` changes?
