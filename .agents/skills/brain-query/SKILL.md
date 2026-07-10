---
name: brain-query
description: Use when an agent needs the BRAIN — WikiLean's unified concept/dependency graph of mathematics — to look up a node (Wikidata concept, Mathlib/Lean container path, decl, external DB page, or arXiv statement), fetch its typed neighborhood (formalizes / xref / links / depends / cites / matches edges with provenance), resolve any key to its atomic unit (QID ∘ article ∘ decls ∘ xrefs), get a containment breadcrumb, or search concepts, areas, and external pages by label. Reach for it to transfer between informal concepts and formal Lean declarations in either direction, to find which QIDs a decl formalizes (multi-to-multi), which external DBs (LMFDB, nLab, Stacks, MathWorld, ProofWiki, …) a concept cross-references, or which folder of Mathlib is a field's formal home.
---

# brain-query

One stdlib-only CLI over the BRAIN dataset (`brain/SCHEMA.md` is the contract).
Everything returns JSON on stdout; nonzero exit = not found.

```
python3 brain/query.py node <id>                  # full shard entry: payload + edges + breadcrumb + children
python3 brain/query.py unit <key>                 # ANY member key → the owning concept's atomic unit
python3 brain/query.py neighborhood <id> [--kinds formalizes,xref,links] [--full]
python3 brain/query.py path <id>                  # containment breadcrumb only
python3 brain/query.py search <text> [--type concept|container|decl|ext]
```

`unit` accepts a QID, `decl:Lib:Name`, a bare FQ decl name, an article slug, or
`xref:db:id` — and returns the concept payload whose `unit` field bundles the
article, formalizing decls (with match_kind), containers, xrefs, and description.

## Node ids (brain/SCHEMA.md)

| type | form | example |
|---|---|---|
| concept | bare QID | `Q181296` |
| container | `path:<Lib>[/<Dir>…]` | `path:Mathlib/CategoryTheory` |
| decl | `decl:<Lib>:<FQ name>` | `decl:Mathlib:CommGroup` |
| ext (external DB page) | `xref:<db>:<id>` | `xref:nlab:abelian group`, `xref:stacks:0001` |
| literature | `lit:<arxiv>#<ref>` | `lit:1707.04448#thm1.2` |

## What the edges mean

- `formalizes` concept→decl OR concept→container (field-of-study altitude). Multi-to-multi by design.
- `xref` concept/decl→external DB page node; evidence carries the Wikidata property or `@[stacks]`/`@[kerodon]` tag.
- `links` ext→ext internal hyperlink (evidence.context = statement|proof|body|related); concept→concept when `evidence.projected` — the link was projected through both pages' Wikidata anchors.
- `depends` formal dependency (typed weights `w_types.{sig,def,proof}` — `sig` is the statement-level signal).
- `cites`/`matches` concept/decl→arXiv statement (dual-judge TheoremGraph matches; never copy statement text — link only unless `license_open`).
- `mentions` concept→decl citation in a WikiLean article — NOT a formalization claim.
- Every edge has `{provenance, confidence, evidence}`; rows with `evidence.skeptic == "pending"` are agent-proposed and not yet adversarially reviewed — treat as candidate-quality.
- Ext nodes carry `snippet` + `snippet_license` only where the source license permits (nlab/stacks/lmfdb/proofwiki/planetmath/oeis); mathworld/dlmf/eom/kerodon are ids+titles+links, deep-link out.

## Formal ↔ informal transfer recipes

- Concept → Lean: `unit Q181296` → `unit.decls` with match_kind (then `mathlib-search decl` to double-check freshness).
- Lean decl → concepts/articles: `unit CommGroup` → owning concept + article slug + description.
- Field → its Mathlib home: `search "category theory" --type concept` → node → formalizes→`path:…` edge.
- `--full` scans brain/data/edges.jsonl + edges_links.jsonl for untruncated lists (shards cap at 200/direction).

## Remote (same data, live site) + MCP

- `GET https://wikilean.jackmccarthy.org/api/brain/node?id=<id>`, `/api/brain/search?q=…&type=…`,
  `/api/brain/unit?key=…`, `/api/brain/transfer?q=…&direction=informal_to_formal|formal_to_informal`,
  `/api/brain/neighborhood?id=…&kinds=…`, `/api/brain/snippets?id=…`, `/api/brain/filter?f=<mask>` —
  full reference: `docs/BRAIN-API.md` or https://wikilean.jackmccarthy.org/brain/api
- Remote MCP for any agent: `claude mcp add --transport http wikibrain https://wikilean.jackmccarthy.org/mcp`
  (tools: brain_search, brain_node, brain_unit, brain_transfer, brain_neighborhood, brain_snippets, brain_filter, decl_exists).
