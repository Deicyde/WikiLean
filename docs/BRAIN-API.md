# Wikibrain API — the agent-facing query surface over the Brain

> **BRAIN v3 — the cell model** (`docs/BRAIN-V3.md`). REST routes + a remote MCP
> server so AI-for-math agents can jump informal ↔ formal mid-proof.
> Human-readable version of this reference is served live at
> <https://wikilean.jackmccarthy.org/brain/api>.
> Data contract: `brain/SCHEMA.md` (the v3 section is normative). Implementation:
> `wiki/src/brain-api.ts` (REST + shared helpers) and `wiki/src/mcp.ts` (MCP).
> The MCP tools call the same exported helpers the REST routes use — the two
> surfaces cannot drift.

Base URL: `https://wikilean.jackmccarthy.org`

## The model: cells, organs, supercells, synapses

The addressable thing is the **cell** — an *atom* of mathematics, id
`cell:<anchor>`. A Mathlib declaration, a Wikidata concept, an external-database
page, a WikiLean article and an arXiv statement that all denote **one object**
are **organs** of that one cell. `Module`, `Q18848` (module) and `Q125977`
(vector space) are the same atom, because Mathlib has no `VectorSpace` —
`Module` fully generalizes it.

| thing | what it is |
|---|---|
| **organ** | A particle — *never* a node. Kinds: `concept` (`Q<digits>`) · `decl` (`decl:<Lib>:<FQ name>`) · `page` (`xref:<db>:<id>`) · `article` (a WikiLean slug) · `statement` (`lit:<arxiv>#<ref>`). Payloads are **embedded**: the Lean docstring + code, the Wikidata description, the licensed DB snippet all ship on the cell, so one fetch renders the whole card. |
| **cell** | The atom, and the node of the graph. The anchor NAMES it — the cell's `exact` concept, not merely its lowest QID. |
| **supercell** | A Mathlib folder, `path:<Lib>/<Dir>`. Cells render inside it, and it owns organs of its own: **field-of-study concepts** (`Q82571` "Linear algebra" → `path:Mathlib/LinearAlgebra`, *never* a cell — SCHEMA rule 5) and area-level pages. A synapse endpoint may therefore be a supercell. |
| **synapse** | ONE aggregated edge per atom pair: `w` (weight — every constituent bond), a `kinds` histogram, and the individual `traces`, each with its own direction, provenance and evidence. **Undirected by construction** — A may `depends` on B while B `links` A — so there is no `dir` parameter; direction lives on each trace. |

An organ's `bond` says *why* it is in the cell: `exact` = it IS the atom
(identity, and transitive — this is what fuses both zeta decls into one atom);
`generalization`/`special_case` = the concept has no formal home of its own and
attaches to its single best target; `xref` = a cross-reference; `field` = an area
concept on a supercell. v3 organs carry `bond` + `prov`, **not** the v2
`confidence` — confidence lived on the grounding edge the builder consumed.

### Every v2 entry point still resolves

`aliases.json` maps every organ id to its owning atom, and **every route accepts
any organ id or an atom id**. `Q125977`, `decl:Mathlib:Module` and
`Vector_space` all answer as `cell:Q18848`; `Q82571` answers as
`path:Mathlib/LinearAlgebra`. Breaking v2 ids/API/MCP was explicitly authorized
(Jack, 2026-07-17) — this is early-phase — but nothing that resolved before 404s
now. Since the alias table is a total function over organs (SCHEMA C4), a miss
in it is a real miss: the v2 fallbacks (shard in-edges, an ext node's own `qid`)
have no v3 analogue, because organs have no inbound edges — they ARE the atom's
content.

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

## Id grammar

| form | what | example |
|---|---|---|
| `cell:<anchor>` | **an atom — the node** | `cell:Q18848` |
| `path:<Lib>[/<Dir>…]` | **supercell** (Mathlib folder) | `path:Mathlib/LinearAlgebra` |
| `Q<digits>` | concept organ (Wikidata QID) | `Q181296` |
| `decl:<Lib>:<FQ name>` | decl organ | `decl:Mathlib:CommGroup` |
| `xref:<db>:<id>` | page organ (external DB) | `xref:lmfdb_knowl:group.abelian` |
| `lit:<arxiv>#<ref>` | statement organ | `lit:1707.04448#thm1.2` |
| *(an article slug)* | article organ | `Abelian_group` |

Synapse kinds — **exactly these eleven**, derived from every `kinds` key in
`brain/data/synapses.jsonl` and exported once as `SYNAPSE_KINDS` in
`wiki/src/brain-api.ts` so the code and this table cannot drift:

| kind | what it is | bonds |
|---|---|---|
| `depends` | formal dependency (`evidence.w_types.{sig,def,proof}`) | 85,673 |
| `links` | an internal page-to-page hyperlink inside one external DB | 11,596 |
| `mentions` | a decl cited on another atom's article — NOT a formalization claim | 10,461 |
| `cites` | literature citation | 3,535 |
| `relates` | Wikidata claim (P279/P361/…) | 2,512 |
| `co-page` | both atoms cross-reference one external page (rule 4) | 677 |
| `co-statement` | both atoms are matched to one arXiv statement (rule 4) | 290 |
| `invocation` | a `formalizes` grade that never merges (rule 3) | 205 |
| `related` | a `formalizes` grade that never merges (rule 3) | 123 |
| `special_case` | an attach grade that did NOT produce a merge (rule 2) | 60 |
| `generalization` | an attach grade that did NOT produce a merge (rule 2) | 42 |

**`formalizes` and `matches` are NOT synapse kinds** — they are strong bonds that
fuse organs INTO a cell (SCHEMA rules 1/4), so they are never a bond *between* atoms;
read them off an organ's `bond` on the cell card. Asking for them matched 0 rows on
every atom while crowding out the five rule-2/3/4 kinds above, so a caller trusting
the old enum silently dropped real bonds. An unknown kind now returns `unknown_kinds`
rather than an empty result. Every synapse carries `w` + `kinds`, and
every trace carries `{kind, src, dst, prov, evidence}` — the Brain can always
answer "why do you believe this?". A trace's `src`/`dst` are the **organ** ids
that witnessed the bond, not the atom ids.

`prov` indexes the shard manifest's `prov` table (returned as `prov_table` /
`_prov_table` where relevant). The provenance single-source-of-truth is
`catalog/data/source_registry.json`.

## REST endpoints

All GET, read-only, unauthenticated, JSON, `Cache-Control: public,
max-age=3600` on success (the underlying shards rebuild nightly). Errors are
`{ok:false, error, …hint}` with 400/404/503 and are not cached.

### `GET /api/brain/cell?key=<any organ id>` — the flagship

Resolve **any** organ id (or an atom id) to the owning atom's card — the one
identity joining Wikipedia article ∘ Wikidata QID ∘ Mathlib decls ∘ folder
homes ∘ external-DB cross-refs — **in one request**, organ payloads embedded.

`GET /api/brain/unit?key=` is an **alias** of this route, not a shim: the v2
unit card *became* the cell card (a unit was QID ∘ article ∘ decls ∘ xrefs,
which is exactly a cell's organs).

Accepted key forms, tried in order:

1. an atom id — `cell:<anchor>` or `path:<Lib>/<Dir>` (an explicit atom id that
   does not exist 404s; it must not fall through to label search)
2. any organ id via `aliases.json` `organs` — QID, `decl:<Lib>:<Name>`,
   `xref:<db>:<id>`, an article slug, `lit:<arxiv>#<ref>`
3. a bare FQ decl name (`CommGroup`) via the `decls` index
4. an article slug via the `slugs` index
5. an exact label or `aka` (an organ's label), case-insensitive — including a
   field concept's label, matched through the supercell organs

```bash
curl 'https://wikilean.jackmccarthy.org/api/brain/cell?key=CommGroup'
curl 'https://wikilean.jackmccarthy.org/api/brain/cell?key=Vector_space'   # → cell:Q18848
```

```jsonc
{
  "ok": true,
  "resolved_from": "decl",        // cell | supercell | organ | decl | slug | label
  "key": "CommGroup",
  "id": "cell:Q181296",
  "kind": "cell",                 // cell | supercell
  "label": "Abelian group",
  "f": 5,
  "cell": { "id": "cell:Q181296", "anchor": "Q181296", "label": "Abelian group",
            "supercells": ["path:Mathlib/Algebra/Group/Defs"], "f": 5, "xy": [12.5, 8.1] },
  "organs": [
    { "kind": "concept", "id": "Q181296", "label": "Abelian group", "bond": "exact", "prov": 0,
      "description": "…", "slug": "Abelian_group",
      "article_annotations": { "total": 60, "formalized": 39 }, "status": "formalized" },
    { "kind": "decl", "id": "decl:Mathlib:CommGroup", "label": "CommGroup", "bond": "exact",
      "module": "Mathlib.Algebra.Group.Defs", "decl_kind": "class", "library": "Mathlib",
      "docstring": "…", "code": "class CommGroup …" },
    { "kind": "page", "id": "xref:lmfdb_knowl:group.abelian", "db": "lmfdb_knowl", "bond": "xref",
      "url": "…", "snippet": "…", "snippet_license": "CC-BY-SA-4.0 (LMFDB)" },
    { "kind": "article", "id": "Abelian_group", "bond": "article", "annotations": {…} }
  ],
  "organs_by_kind": { "concept": 1, "decl": 1, "page": 3, "article": 1 },
  "breadcrumb": [ { "id": "path:Mathlib", "label": "Mathlib" }, … ],
  "synapses_summary": { "depends": 12, "relates": 2, "mentions": 1 },
  "synapses_preview": [ { "id": "cell:Q18848", "w": 15, "kinds": {…} } ],  // strongest 10, no traces
  "counts": { "syn": 757, "organs": 16 },
  "truncated": { "syn": 557 }     // a COUNT of synapses the shard dropped, not a flag
}
```

For a **supercell** the body carries `supercell: {path, parent, children, cells,
fa}` in place of `cell`, and its `organs` are the rule-5 field concepts and area
pages. Traces are deliberately not on this card — `/api/brain/neighborhood`
serves them, so the card stays an identity answer rather than a graph dump.
404 responses include a `hint`; `resolved_from` tells you which key form matched.

### `GET /api/brain/transfer?q=&direction=&limit=` — informal ↔ formal

With cells this is "resolve to the atom, read its organs" — no edge walk, which
is both simpler and better: an atom's decls ARE its own organs, so an absorbed
concept answers correctly for free.

`direction=informal_to_formal`: `q` is a concept (QID / slug / exact label /
free text — free text falls back to label+`aka` search, `resolved_from:"search"`)
→ the atom's ranked decl organs:

```bash
curl 'https://wikilean.jackmccarthy.org/api/brain/transfer?q=abelian%20group&direction=informal_to_formal'
```

```jsonc
{ "ok": true, "id": "cell:Q181296", "kind": "cell", "label": "Abelian group",
  "resolved_from": "search", "match": "exact",
  "confidence_floor": "a hit clears the floor when the atom resolved by IDENTITY …",
  "hits": [ {
    "decl": "CommGroup",
    "module": "Mathlib.Algebra.Group.Defs",
    "import_line": "import Mathlib.Algebra.Group.Defs",
    "bond": "exact",
    "decl_kind": "class",
    "code": "class CommGroup (G : Type u) extends Group G, CommMonoid G",
    "docs_url": "https://leanprover-community.github.io/mathlib4_docs/Mathlib/Algebra/Group/Defs.html#CommGroup",
    "via_cell": "cell:Q181296",
    "cell_label": "Abelian group"
  } ] }
```

Every decl hit carries `module`, `import_line` (`"import " + module`) and the
statement `code` when the shard has it — existence plus a name is not enough to
write compiling code. Hits rank `exact` bonds first, then any other graded bond,
then an ungraded one (the anchor decl of a lone-particle cell), then name. When
a decl's module is unknown, `docs_url` falls back to the durable
`https://wikilean.jackmccarthy.org/decl/<name>` resolver (302 → current docs).

`match` classifies the answer and `confidence_floor` states the rule in the
response itself: `exact` · `generalization`/`special_case` (the query resolved
through a non-exact concept — a `note` explains, e.g. "Module generalizes Vector
space") · `field` (a supercell answer) · `none`. **Honest abstention**: a fuzzy
free-text match whose best bond is weaker than `exact` does not clear the floor —
the response returns `match: "none"` with `nearest` (top 3, each with a `why`)
instead of a forced weak grounding.

A **field-of-study concept resolves to a supercell**, and that is the honest
answer rather than an empty result: `hits: []` plus `kind: "supercell"`,
`container: "path:Mathlib/LinearAlgebra"`, `cells_in_container`, and a `note`.

`direction=formal_to_informal`: `q` is a decl name (bare or `decl:Lib:Name`) →
the same atom's concept organs (multi-to-multi by design — one fetch, where v2
walked in-edges):

```bash
curl 'https://wikilean.jackmccarthy.org/api/brain/transfer?q=Module&direction=formal_to_informal'
```

```jsonc
{ "ok": true, "decl": "Module", "id": "cell:Q18848",
  "hits": [ { "qid": "Q18848", "label": "Module", "bond": "exact", "slug": "Module_mathematics",
              "article_url": "https://wikilean.jackmccarthy.org/Module_mathematics",
              "description": "…", "snippet_sources": ["nlab", "mathworld"],
              "via_cell": "cell:Q18848" },
            { "qid": "Q125977", "label": "Vector space", "bond": "generalization", … } ] }
```

Empty results in either direction return 200 with `hits: []`, a `note`, and
near-miss `suggestions` — read them before concluding something is
unformalized. A decl whose atom holds no concept organ is a *formal-only cell*
(5,082 of these ship), which the `note` says explicitly. `limit` defaults to 10,
caps at 50.

### `GET /api/brain/neighborhood?id=&kinds=&limit=&traces=&min_w=&cursor=&min_conf=`

An atom's **synapses** — one aggregated row per partner atom, not raw edges.
Accepts any organ id. `kinds` = CSV subset of the synapse kinds above (it
filters the traces too, so asking for `depends` never dumps `links`);
`limit` ≤ 200 (default 50); `traces=0` omits traces for a compact partner list.

Rows come in a **stable `(-w, id)` order** so a long-running agent can walk a
chain across turns:

- `cursor` — an **opaque** token; pass the previous response's `next_cursor`
  back to resume. `next_cursor` is present only while filtered rows remain.
- `min_w` — floor the synapse weight.
- `min_conf` — drop traces whose `evidence.confidence` is below the floor
  (traces with no confidence are KEPT, and the number dropped is reported in
  `traces_conf_filtered`; shipped traces carry no confidence, so this is inert
  on prod but correct where present).

The shard cap stays the only HARD stop and is always declared in
`withheld_by_shard` — cursoring never silently caps. **There is no `dir`**: a
synapse is an undirected aggregate of bonds that may run either way; direction
lives on each trace.

```bash
curl 'https://wikilean.jackmccarthy.org/api/brain/neighborhood?id=Q18848&kinds=depends'
```

```jsonc
{ "ok": true, "id": "cell:Q18848", "kind": "cell",
  "synapses": [ { "id": "cell:Q1000660", "w": 15,
                  "kinds": { "depends": 12, "mentions": 1, "relates": 2 },
                  "traces_total": 15,
                  "traces": [ { "kind": "depends", "src": "Q1000660", "dst": "Q125977", "prov": 24,
                                "evidence": { "weight": 16, "w_types": { "sig": 15, "def": 2, "proof": 0 },
                                              "witnesses": [["AlgHom", "Algebra"]] } } ] } ],
  "returned": 50, "matched": 199, "counts": { "syn": 757, "organs": 16 }, "truncated": true }
```

`traces_total` (`tt` in the shard) vs the returned list is how you know traces
were trimmed — the shard keeps **6 traces per synapse** and caps the synapse
list at **200**, heaviest first. `brain/query.py neighborhood <key> --full`
serves the untruncated set with full traces from `brain/data/synapses.jsonl`.

> **Data caveat:** a *supercell's* synapses ship **without traces**
> (`build_cell_shards.py` strips them from `supercells.json` rows for byte
> budget), so a supercell neighborhood returns `traces: []` with `w`/`kinds`
> intact. Use `brain/query.py --full` for those traces.

### `GET /api/brain/snippets?id=`

Every stored content snippet on an atom, one row per source:
`{source_db, id, label, snippet?, license?, url?}`. Accepts any organ id.
Read straight from the **embedded organ payloads** — v2 fanned out one shard
fetch per xref target; v3 answers the whole call from one fetch.

- `concept` organ → Wikidata description row (CC0)
- `article` organ → WikiLean article pointer (annotations live in D1)
- `page` organ → its stored snippet + license, or a deep link
- `decl` organ → the Mathlib docstring (`snippet`) + source (`code`), licensed
  Apache-2.0 per `source_registry.json` `node_sources.mathlib.target_license`
- `statement` organ → an arXiv link; statement TEXT is never redistributed

Snippets are stored only where the source license permits (nLab, Stacks, LMFDB,
ProofWiki, PlanetMath, OEIS); no-content sources (MathWorld, DLMF, EoM, Kerodon)
return deep-link rows without snippets. **A snippet is never served without its
license**: the builder guarantees the pair and the API re-checks it, degrading a
licence-less snippet to a deep link rather than emitting unlicensed text.

```bash
curl 'https://wikilean.jackmccarthy.org/api/brain/snippets?id=Q181296'
```

### `GET /api/brain/filter?f=&type=&under=&limit=&cursor=`

Enumerate atoms whose facet bitmask contains `f`: `(f_row & f) == f` (rows
without `f` read as 0, so `f=0` matches everything). `limit` ≤ 500 (default 100).

- `type=cell` (default) — `labels.json`, each cell's **own** mask.
- `type=supercell` — `supercells.json` by `fa`, the subtree-**aggregate** mask
  ("something under this folder matches"). A deliberately different question,
  hence a separate type rather than one mixed list.
- `under=path:…` — restrict to a containment subtree (cells carry `p`, their
  deepest supercell; supercells match on their own path prefix).

The v2 `type=concept|container|ext` values **400** — they no longer denote
anything (an external page is an organ, never an atom), and silently ignoring
them would be worse than failing.

Facet bits (`brain/SCHEMA.md`): 0 gold `@[wikidata]` tag · 1 `@[stacks]` ·
2 `@[kerodon]` · 3 any xref · 4 formalized · 5 partial · 6 has WikiLean
article · 7 has literature · ~~8 is ext~~ · 9 lmfdb · 10 nlab · 11 mathworld ·
12 proofwiki · 13 stacks-tag · 14 oeis · 15 has stored snippet.

**Bit 8 is never set on a cell** (verified on the shipped build: 0 of 8,914) —
it meant "this node IS an external page", which no atom is; the builder masks
it off when a page organ's facets fold into its cell. A cell's mask is the OR
over its organs, so `f=1` returns every atom holding a gold-tagged declaration
and `f=17` (bits 0+4) every formalized atom whose formalization carries a gold
`@[wikidata]` tag.

Pagination is by stable row-index cursor: pass the previous response's
`next_cursor` back as `cursor`; `next_cursor: null` means done. (Cursors are
positions in the nightly-built index — treat a nightly rebuild as invalidating
outstanding cursors.)

```bash
curl 'https://wikilean.jackmccarthy.org/api/brain/filter?f=17&limit=50'
curl 'https://wikilean.jackmccarthy.org/api/brain/filter?f=1&under=path:Mathlib/Algebra'
```

### `GET /api/brain/search?q=&type=&limit=`

Label search over the atom index. Matches an atom's own label **and its `aka`
list — every organ's label** — so `q=Vector space` returns the **Module** atom
(they are one atom, named by its anchor). Prefix hits rank before substring
hits. A key that resolves exactly (QID, decl name, article slug, xref id) is
promoted to the top hit with `matched: <resolved_from>`, which keeps the v2
"a bare QID query matches by id" behavior alive now that ids are `cell:<anchor>`.
`type` ∈ `cell | supercell`; supercells are matched through their folder label
AND their organ labels (so "linear algebra" finds `path:Mathlib/LinearAlgebra`).

```bash
curl 'https://wikilean.jackmccarthy.org/api/brain/search?q=vector%20space'
```

### `GET /api/brain/decl?name=` · `?names=<comma-separated>`

Verify decl names against the doc-gen4 oracle. `name` is the single form;
`names` (comma-separated, cap 16) is the batch — a drafted statement's 3–8
citations verify in one round trip. Each verdict:

```jsonc
// GET /api/brain/decl?names=Basis,Module.Basis,AddCircle.fourierCoeff,NotARealName
{ "ok": true,
  "results": [
    { "decl": "Basis", "exists": false, "renamed_to": "Module.Basis",
      "suggestion_basis": "verified-rename", "module": "Mathlib.LinearAlgebra.Basis.Defs",
      "import_line": "import Mathlib.LinearAlgebra.Basis.Defs", "docs_url": "…" },
    { "decl": "Module.Basis", "exists": true, "library": "mathlib",
      "module": "Mathlib.LinearAlgebra.Basis.Defs", "import_line": "…", "docs_url": "…" },
    { "decl": "AddCircle.fourierCoeff", "exists": false, "renamed_to": "fourierCoeff",
      "suggestion_basis": "verified-rename", "module": "Mathlib.Analysis.Fourier.AddCircle", … },
    { "decl": "NotARealName", "exists": false, "hint": "…" }
  ],
  "counts": { "total": 4, "exists": 1, "renamed": 2, "missing": 1 } }
```

A dead name returns a **labelled** suggestion, never a fact:
`suggestion_basis: "verified-rename"` (the agent-and-adversary-verified
`decl_renames.jsonl`, read off the owning cell's decl organ) or
`"unique-suffix-match"` (exactly one decl in the brain's decl-organ index shares
the last segment, then verified to exist against the oracle). Two or more
suffix candidates ⇒ no suggestion (never force one).

### `GET /api/brain/bridge?q=<informal statement>&limit=`

The composite **first call of an autoformalization loop**: search → resolve to
atoms → rank decl organs across the top atoms → verify existence → attach
signature / `import_line` / `bond` / breadcrumb → one-hop `depends`. `limit` ≤ 16
(default 8).

```bash
curl 'https://wikilean.jackmccarthy.org/api/brain/bridge?q=every%20finitely%20generated%20vector%20space%20has%20a%20basis'
```

```jsonc
{ "ok": true, "q": "every finitely generated vector space has a basis",
  "match": "exact", "confidence_floor": "…",
  "atoms": [ { "id": "cell:Q18848", "label": "Module (mathematics)", "resolved_from": "statement", "breadcrumb": […] }, … ],
  "hits": [
    { "decl": "Basis", "exists": false, "module": "Mathlib.LinearAlgebra.Basis.Defs",
      "import_line": "…", "bond": "exact", "code": "structure Basis where",
      "via_cell": "cell:Q189569", "breadcrumb": […],
      "renamed_to": "Module.Basis", "suggestion_basis": "verified-rename", "suggested_import_line": "…" },
    { "decl": "Module", "exists": true, "module": "Mathlib.Algebra.Module.Defs", "import_line": "…", "bond": "exact", … }
  ],
  "depends": { "partners": [ { "id": "cell:Q1000660", "label": "Algebra over a field", "w": 15 }, … ],
               "returned": 12, "total": 155, "withheld_by_shard": 557, "truncated": true },
  "next_tools": [ "brain_cell <via_cell> — …", "decl_exists {names:[…]} — …", … ] }
```

It resolves a full statement two ways — labels that CONTAIN the query (short
concept queries) and atoms whose label appears IN the query (statement queries,
word-bounded and length-floored) — then ranks decl organs `exact`-first across
the top three atoms. Every hit is **existence-verified** against the decl-index
oracle; a dead cited name gets the same labelled `renamed_to` suggestion
`decl_exists` serves. Honest abstention applies: nothing clearing the floor ⇒
`match: "none"` with `nearest`. Statement-level embedding transfer stays deferred
(a slogan compresses away hypotheses), so this ranks by concept labels/aliases.

### Snapshot echo

**Every** API/MCP response carries `snapshot: {generated_at, pin}` — the brain
build time (`cells/manifest.json` `_meta.generated_at`) and the Mathlib rev the
decl organs were built against (the commit-shaped `pin` off the manifest's
`prov` table, e.g. `"bf3266149cda603f"`). A held-out evaluation is dishonest
without it. `snapshot: null` when the manifest is unavailable. `?rev=`
time-travel over archived snapshots comes later.

### Related routes

- `GET /api/brain/edges?id=` — the LIVE community-edit overlay (D1-backed,
  `Cache-Control: no-store`) including inferred xref-shared partners.
- `GET /decl/<name>` — durable decl resolver; 302 → mathlib4_docs, or JSON
  (module, docs_url, reverse citations) with `Accept: application/json`.
- `GET /api/brain/node?id=` — **legacy**: the v2 particle shards
  (`wiki/src/brain.ts`). Still served because the community-edit write path uses
  the v2 shard set as its node-existence oracle and the v2 `/brain` page reads
  it; it retires with the v2 render path (docs/BRAIN-V3.md phase 5). Use
  `/api/brain/cell`. Note its ids are ORGAN ids, so they all feed the v3 routes.
- `/map`, `/graph`, `/atlas`, `/article-graph` → 301 `/brain`;
  `/graph_data.json`, `/atlas_data.json`, `/api/atlas` → **410** (retired
  2026-07-10).

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
| `brain_bridge` | `q` (req), `limit?` | `/api/brain/bridge` |
| `brain_search` | `q` (req), `type?`, `limit?` | `/api/brain/search` |
| `brain_cell` | `key` (req) | `/api/brain/cell` |
| `brain_transfer` | `q`, `direction` (req), `limit?` | `/api/brain/transfer` |
| `brain_neighborhood` | `id` (req), `kinds?`, `traces?`, `limit?`, `min_w?`, `cursor?`, `min_conf?` | `/api/brain/neighborhood` |
| `brain_snippets` | `id` (req) | `/api/brain/snippets` |
| `brain_filter` | `f` (req), `type?`, `under?`, `limit?`, `cursor?` | `/api/brain/filter` |
| `decl_exists` | `name` OR `names` (array, cap 16) | `/api/brain/decl` |

**Eight tools**: `brain_bridge` is the composite first call of an
autoformalization loop (below); `brain_cell` replaces `brain_node` +
`brain_unit` (v3 has no particle nodes, and the unit card became the cell card).
`brain_unit` and `brain_node` remain as **dispatch-only aliases** — not
advertised in `tools/list`, but still answering (with the atom) so an agent
session holding the old catalog does not hard-fail. Both accept either `key` or
`id`, since the two tools disagreed on the name.

`decl_exists` verifies fully-qualified Lean decl names against the same doc-gen4
declaration index `GET /decl/<name>` resolves with. Pass `name` (one) or `names`
(a batch, cap 16 — a drafted statement's 3–8 citations verify in one round
trip). Each verdict is `{exists, module?, import_line?, docs_url?}`; a DEAD name
also returns a labelled suggestion — `suggestion_basis: "verified-rename"` (the
verified rename map, e.g. `Basis` → `Module.Basis`) or `"unique-suffix-match"`
(one indexed decl shares the last segment; verified against the oracle before
suggesting) — **never presented as fact**. The batch response adds a `counts`
summary. Hallucinated/renamed names are the #1 failure mode.

**THE CANONICAL LOOP** (autoformalization): `brain_bridge` (informal statement →
existence-verified decls with signatures + one-hop depends — the FIRST call) →
`brain_cell` (the full atom for the winner) → `decl_exists` (batch: re-verify
every name you write) → `brain_neighborhood(kinds=depends)` (walk the dependency
chain across turns — it is cursored). `brain_search`/`brain_transfer` are the
lower-level jumps `brain_bridge` composes.

**Honest abstention**: `brain_bridge` and `brain_transfer` (informal→formal)
carry a `match` and a `confidence_floor`; a fuzzy match that does not clear the
floor returns `match: "none"` with `nearest` candidates rather than a forced
weak grounding — a forced match is what CREATES hallucinated citations. When the
query resolves through a generalization/special_case concept (Vector space →
`Module`, which is exact for the atom but a generalization of the query), a
top-level `note` says so.

## Rate limits & caching

- `POST /mcp`: **120 requests/min per IP** (JSON-RPC error `-32000`, HTTP 429
  when exceeded).
- REST routes: unauthenticated and edge-cached (`public, max-age=3600`);
  responses change on the nightly data rebuild. `/api/brain/edges` is the one
  live, uncached route.
- Be a good citizen: batch-style crawling should use the static cell assets
  (`/assets/brain/cells/manifest.json` + shards, `labels.json`, `aliases.json`,
  `supercells.json`, `explorer.json`) or the repo's `brain/data/*.jsonl`
  instead of hammering the API.

## The local CLI (`brain/query.py`)

Same data, no network, and the only surface that serves **untruncated** traces:

```bash
python3 brain/query.py cell Vector_space              # → cell:Q18848, organs embedded
python3 brain/query.py organs Q82571                  # → path:Mathlib/LinearAlgebra's organs
python3 brain/query.py neighborhood Q18848 --kinds depends --full
python3 brain/query.py search "vector space"          # label + aka, prefix-ranked
python3 brain/query.py supercell Mathlib/LinearAlgebra
```

`--full` scans `brain/data/synapses.jsonl` for the whole synapse set with every
trace — the shards trim to 6 traces per synapse and 200 synapses per atom.
(`unit`/`node` remain as aliases of `cell`.)

## Provenance & licensing

- Brain cell/synapse data itself is **CC0**. Every organ and every synapse trace
  carries a `prov` index resolving through the manifest's `prov` table.
  `catalog/data/source_registry.json` is the provenance single-source-of-truth.
- Snippets carry per-row licenses and are stored only where permitted:
  nLab (attribution) · Stacks (GFDL) · LMFDB / OEIS (CC-BY-SA-4.0) ·
  ProofWiki (CC-BY-SA-3.0; reference use only) · PlanetMath (CC-BY-SA) ·
  Mathlib (Apache-2.0). MathWorld / DLMF / EoM / Kerodon are ids + titles +
  links only — display deep-links out.
- TheoremGraph-derived edges carry CC-BY-SA attribution in artifact `_meta`;
  arXiv statement text is never redistributed.

## Graceful degradation (implementation contract)

The cell artifacts ship independently of the Worker and may lag a deploy. The
posture changed with v3 and the change is deliberate:

- **`aliases.json` is load-bearing, not optional.** In v2 a missing alias table
  fell back to shard in-edge resolution; v3 has no such fallback, because organs
  have no inbound edges — they ARE the atom's content. Without it only atom ids
  and exact labels resolve. It is a total function over organs (SCHEMA C4), so a
  miss in it is a real miss, and every route treats it as authoritative.
- A missing `f` reads as 0; a missing `aka`/`p` degrades search/`under` rather
  than erroring; a supercell absent from `supercells.json` 404s rather than
  guessing.
- A snippet that arrives without `snippet_license` is served **as a deep link
  with the text dropped** — the licence floor is enforced in the API, not only
  trusted from the builder.
- The dedicated `MCP_LIMITER` binding falls back to `BRAIN_API_LIMITER` (same
  120/min budget) until configured in `wrangler.jsonc`.
