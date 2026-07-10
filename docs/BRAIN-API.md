# Wikibrain API — the agent-facing query surface over the Brain

> BRAIN v2 axis 5 (`docs/BRAIN-V2.md`). REST routes + a remote MCP server so
> AI-for-math agents can jump informal ↔ formal mid-proof. Human-readable
> version of this reference is served live at
> <https://wikilean.jackmccarthy.org/brain/api>.
> Data contract: `brain/SCHEMA.md`. Implementation: `wiki/src/brain-api.ts`
> (REST + shared helpers) and `wiki/src/mcp.ts` (MCP). The MCP tools call the
> same exported helpers the REST routes use — the two surfaces cannot drift.

Base URL: `https://wikilean.jackmccarthy.org`

## Quickstart (MCP — recommended for agents)

```bash
claude mcp add --transport http wikibrain https://wikilean.jackmccarthy.org/mcp
```

That's it. The server is stateless streamable-HTTP (JSON-RPC 2.0 over POST,
`application/json` single-response mode — no SSE, no sessions, no auth).
Supported protocol revisions: `2025-06-18` and `2025-03-26` (the server echoes
a supported version the client requests, else offers `2025-06-18`).
`GET /mcp` returns a 405 with a connect hint.

Raw JSON-RPC example:

```bash
curl -s https://wikilean.jackmccarthy.org/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"brain_transfer","arguments":{"q":"abelian group","direction":"informal_to_formal"}}}'
```

## Node id grammar

Every Brain node has a globally unique string id (see `brain/SCHEMA.md`):

| form | type | example |
|---|---|---|
| `Q<digits>` | concept (Wikidata QID — the only dedup layer) | `Q181296` |
| `path:<Lib>[/<Dir>…]` | container (Mathlib folder) | `path:Mathlib/CategoryTheory` |
| `decl:<Lib>:<FQ name>` | Lean declaration | `decl:Mathlib:CommGroup` |
| `lit:<arxiv>#<ref>` | literature statement | `lit:1707.04448#thm1.2` |
| `obj:<db>:<label>` | mathematical object | `obj:oeis:A000045` |
| `xref:<db>:<id>` | external DB page (v2 `ext` node) | `xref:lmfdb_knowl:group.abelian` |

Edge kinds: `formalizes`, `mentions`, `depends`, `matches`, `xref`, `relates`,
`links`, `cites` (+ machine-only `contains`). Every edge carries
`{kind, confidence, evidence}` and a provenance index into the shard
manifest's `prov` table — the Brain can always answer "why do you believe
this?".

`match_kind` semantics on `formalizes` evidence: `exact` = the decl IS the
concept's formalization; `related`/`partial` = nearby or partial; `field` =
the concept is a whole area whose formal home is a Mathlib *folder*
(container). Confidence is `high | medium | low`.

## REST endpoints

All GET, read-only, unauthenticated, JSON, `Cache-Control: public,
max-age=3600` on success (the underlying shards rebuild nightly). Errors are
`{ok:false, error, …hint}` with 400/404/503 and are not cached.

### `GET /api/brain/unit?key=<any member key>` — the flagship

Resolve **any** member key of an atomic unit to the owning concept's unit
card — the one identity joining Wikipedia article ∘ Wikidata QID ∘ Mathlib
decls ∘ folder homes ∘ external-DB cross-refs.

Accepted key forms, tried in order:

1. exact QID (`Q181296`)
2. decl — `decl:Mathlib:CommGroup` or a bare FQ decl name (`CommGroup`),
   via the `aliases.json` decl map, falling back to the decl shard entry's
   inbound `formalizes` edges
3. article slug (`Abelian_group`), via `aliases.json`, falling back to the
   label index
4. `xref:<db>:<id>`, via the ext node's own `qid`, falling back to its
   inbound `xref` edges
5. exact concept label, case-insensitive (`abelian group`)

```bash
curl 'https://wikilean.jackmccarthy.org/api/brain/unit?key=CommGroup'
```

```jsonc
{
  "ok": true,
  "resolved_from": "decl",        // qid | decl | slug | xref | label
  "key": "CommGroup",
  "qid": "Q181296",
  "unit": {
    "qid": "Q181296",
    "label": "Abelian group",
    "description": "…",           // Wikidata description (when built)
    "article": { "slug": "Abelian_group", "annotations": { "total": 60, "formalized": 39 } },
    "decls": [ { "name": "CommGroup", "module": "Mathlib.Algebra.Group.Defs",
                 "match_kind": "exact", "confidence": "high" } ],
    "containers": [],             // formalizes → path:… (field-level concepts)
    "xrefs": { "lmfdb_knowl": [ { "id": "group.abelian" } ], "nlab": [ { "id": "abelian+group" } ] }
  },
  "display": { "primary_decl": "CommGroup", "status": "formalized" },
  "edges_summary": { "formalizes": 1, "xref": 3, "relates": 2 }
}
```

404 responses include a `hint` pointing at `/api/brain/search` for fuzzy
lookup. `resolved_from` tells you which key form matched.

### `GET /api/brain/transfer?q=&direction=&limit=` — informal ↔ formal

`direction=informal_to_formal`: `q` is a concept (QID / slug / exact label /
free text — free text falls back to label search, `resolved_from:"search"`) →
ranked decls:

```bash
curl 'https://wikilean.jackmccarthy.org/api/brain/transfer?q=abelian%20group&direction=informal_to_formal'
```

```jsonc
{ "ok": true, "qid": "Q181296", "qid_label": "Abelian group",
  "hits": [ {
    "decl": "CommGroup",
    "module": "Mathlib.Algebra.Group.Defs",
    "match_kind": "exact",
    "confidence": "high",
    "docs_url": "https://leanprover-community.github.io/mathlib4_docs/Mathlib/Algebra/Group/Defs.html#CommGroup",
    "via_qid": "Q181296",
    "qid_label": "Abelian group"
  } ] }
```

Hits are ranked by confidence, then `exact` match_kind first. When a decl's
module is unknown, `docs_url` falls back to the durable
`https://wikilean.jackmccarthy.org/decl/<name>` resolver (302 → current docs).

`direction=formal_to_informal`: `q` is a decl name (bare or `decl:Lib:Name`) →
the concepts it formalizes (multi-to-multi by design):

```bash
curl 'https://wikilean.jackmccarthy.org/api/brain/transfer?q=Module&direction=formal_to_informal'
```

```jsonc
{ "ok": true, "decl": "Module",
  "hits": [ { "qid": "Q18848", "label": "Module", "slug": "Module_(mathematics)",
              "article_url": "https://wikilean.jackmccarthy.org/Module_(mathematics)",
              "description": "…", "snippet_sources": ["lmfdb_knowl", "nlab"] },
            { "qid": "Q125977", "label": "Vector space", … } ] }
```

Empty results in either direction return 200 with `hits: []`, a `note`, and
near-miss `suggestions` from the label index — read them before concluding
something is unformalized. `limit` defaults to 10, caps at 50.

### `GET /api/brain/neighborhood?id=&kinds=&dir=&limit=`

Filtered projection of a node's shard edges. `kinds` = CSV subset of the edge
kinds above; `dir` ∈ `out|in|both` (default `both`); `limit` ≤ 200 (default
50).

```bash
curl 'https://wikilean.jackmccarthy.org/api/brain/neighborhood?id=Q181296&kinds=xref&dir=out'
```

Response: `edges` (each row `{direction, id, kind, confidence, evidence,
prov}`), `returned`, `matched` (kind-filtered matches per direction within the
shard's capped lists), `counts` (the node's TOTAL edges per direction), and
`truncated` (true when `limit` cut matches OR the shard itself capped the
direction at 200 — fetch `/api/brain/node?id=` and the nightly
`brain/data/edges.jsonl` for the full set).

### `GET /api/brain/snippets?id=`

Every stored content snippet for a unit, one row per source:
`{source_db, id, label, snippet?, license?, url?}`.

- concept id → Wikidata description row (CC0) + WikiLean article pointer +
  each cross-referenced external page's stored snippet
- ext id (`xref:<db>:<id>`) → that page's own row

Snippets are stored only where the source license permits (nLab, Stacks,
LMFDB, ProofWiki, PlanetMath, OEIS); no-content sources (MathWorld, DLMF, EoM,
Kerodon) return deep-link rows without snippets. Cross-ref targets not (yet)
minted as ext nodes still appear as pointer rows.

```bash
curl 'https://wikilean.jackmccarthy.org/api/brain/snippets?id=Q181296'
```

### `GET /api/brain/filter?f=&type=&limit=&cursor=`

Enumerate label-index rows whose facet bitmask contains `f`:
`(row.f & f) == f` (rows without `f` read as 0, so `f=0` matches everything).
`type` optionally narrows to `concept | container | ext`. `limit` ≤ 500
(default 100).

Facet bits (`brain/SCHEMA.md`): 0 gold `@[wikidata]` tag · 1 `@[stacks]` ·
2 `@[kerodon]` · 3 any xref · 4 formalized · 5 partial · 6 has WikiLean
article · 7 has literature · 8 is ext · 9 lmfdb · 10 nlab · 11 mathworld ·
12 proofwiki · 13 stacks-tag · 14 oeis · 15 has snippet.

Pagination is by stable row-index cursor: pass the previous response's
`next_cursor` back as `cursor`; `next_cursor: null` means done. (Cursors are
positions in the nightly-built label index — treat a nightly rebuild as
invalidating outstanding cursors.)

```bash
# everything carrying a gold @[wikidata] tag AND formalized (bits 0+4 = 17)
curl 'https://wikilean.jackmccarthy.org/api/brain/filter?f=17&limit=50'
```

### Pre-existing routes (unchanged)

- `GET /api/brain/node?id=` — the full shard entry: node payload, capped
  1-hop edges both directions, breadcrumb, children, rollups, `prov_table`.
- `GET /api/brain/search?q=&type=&limit=` — label search (prefix hits rank
  before substring hits; a bare QID matches by id).
- `GET /api/brain/edges?id=` — the LIVE community-edit overlay (D1-backed,
  `Cache-Control: no-store`) including inferred xref-shared partners.
- `GET /decl/<name>` — durable decl resolver; 302 → mathlib4_docs, or JSON
  (module, docs_url, reverse citations) with `Accept: application/json`.
- `GET /api/atlas`, `/api/atlas/:key`, `/graph_data.json`,
  `/atlas_data.json` — the coarse-grain atlas/graph data surfaces.

## The MCP server

`POST /mcp` — JSON-RPC 2.0 methods: `initialize`,
`notifications/initialized` (202, empty), `tools/list`, `tools/call`, `ping`.
Unknown methods → `-32601`; malformed JSON-RPC → `-32700`/`-32600`; batch
arrays are not supported. Unknown tool names → `-32602`. **Input-validation
failures are tool results with `isError: true`** (the model can read the JSON
error + hint and self-correct), never protocol errors.

Tool results: `{content: [{type: "text", text: "<JSON>"}], isError?}` where
the text is exactly the corresponding REST response body.

| tool | arguments | REST twin |
|---|---|---|
| `brain_search` | `q` (req), `type?`, `limit?` | `/api/brain/search` |
| `brain_node` | `id` (req) | `/api/brain/node` |
| `brain_unit` | `key` (req) | `/api/brain/unit` |
| `brain_transfer` | `q`, `direction` (req), `limit?` | `/api/brain/transfer` |
| `brain_neighborhood` | `id` (req), `kinds?`, `dir?`, `limit?` | `/api/brain/neighborhood` |
| `brain_snippets` | `id` (req) | `/api/brain/snippets` |
| `brain_filter` | `f` (req), `type?`, `limit?`, `cursor?` | `/api/brain/filter` |
| `decl_exists` | `name` (req) | (decl-index oracle; see below) |

`decl_exists` verifies a fully-qualified Lean decl name against the same
doc-gen4 declaration index `GET /decl/<name>` resolves with, returning
`{exists, module?, docs_url?}`. Agents should call it before citing any decl
name — hallucinated/renamed names (`Basis` → `Module.Basis`) are the #1
failure mode.

A typical mid-proof loop: `brain_search` (text → ids) → `brain_unit` (one
identity, everything known) → `brain_transfer` (jump directions) →
`decl_exists` (verify before citing) → `brain_neighborhood(kinds=depends)`
(walk formal dependencies).

## Rate limits & caching

- `POST /mcp`: **120 requests/min per IP** (JSON-RPC error `-32000`, HTTP 429
  when exceeded).
- REST routes: unauthenticated and edge-cached (`public, max-age=3600`);
  responses change on the nightly data rebuild. `/api/brain/edges` is the one
  live, uncached route.
- Be a good citizen: batch-style crawling should use the static shard assets
  (`/assets/brain/manifest.json` + shards, `labels.json`) or the repo's
  `brain/data/*.jsonl` instead of hammering the API.

## Provenance & licensing

- Brain node/edge data itself is **CC0**. Every edge carries provenance;
  `prov` indexes on `/api/brain/node` edges resolve through the response's
  `prov_table`. `catalog/data/source_registry.json` is the provenance
  single-source-of-truth.
- Snippets carry per-row licenses and are stored only where permitted:
  nLab (attribution) · Stacks (GFDL) · LMFDB / OEIS (CC-BY-SA-4.0) ·
  ProofWiki (CC-BY-SA-3.0; reference use only) · PlanetMath (CC-BY-SA).
  MathWorld / DLMF / EoM / Kerodon are ids + titles + links only — display
  deep-links out.
- TheoremGraph-derived edges carry CC-BY-SA attribution in artifact `_meta`;
  arXiv statement text is never redistributed.

## Graceful degradation (implementation contract)

The v2 data artifacts ship independently of the Worker: `node.unit`
(prebuilt unit cards), labels `f` bits, `ext` nodes, and
`/assets/brain/aliases.json` may lag a deploy. Every endpoint
feature-detects: a missing `unit` is assembled on the fly from
formalizes/xref edges, missing `aliases.json` falls back to shard in-edge /
label-index resolution, missing `f` reads as 0, and unminted xref targets
degrade to pointer rows. The dedicated `MCP_LIMITER` binding likewise falls
back to `BRAIN_API_LIMITER` (same 120/min budget) until configured in
`wrangler.jsonc`.
