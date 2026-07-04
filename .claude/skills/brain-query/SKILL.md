---
name: brain-query
description: Use when an agent needs the BRAIN — WikiLean's unified concept/dependency graph of mathematics — to look up a node (Wikidata concept, Mathlib/Lean container path, decl, or arXiv statement), fetch its typed neighborhood (formalizes / xref / depends / cites / matches edges with provenance), get a containment breadcrumb, or search concepts and areas by label. Reach for it to transfer between informal concepts and formal Lean declarations in either direction, to find which QIDs a decl formalizes (multi-to-multi), which external DBs (LMFDB, nLab, MathWorld, Metamath, …) a concept cross-references, or which folder of Mathlib is a field's formal home.
---

# brain-query

One stdlib-only CLI over the BRAIN dataset (`brain/SCHEMA.md` is the contract).
Everything returns JSON on stdout; nonzero exit = not found.

```
python3 brain/query.py node <id>                  # full shard entry: payload + edges + breadcrumb + children
python3 brain/query.py neighborhood <id> [--kinds formalizes,xref] [--full]
python3 brain/query.py path <id>                  # containment breadcrumb only
python3 brain/query.py search <text> [--type concept|container|decl]
```

## Node ids (brain/SCHEMA.md)

| type | form | example |
|---|---|---|
| concept | bare QID | `Q181296` |
| container | `path:<Lib>[/<Dir>…]` | `path:Mathlib/CategoryTheory` |
| decl | `decl:<Lib>:<FQ name>` | `decl:Mathlib:CommGroup` |
| literature | `lit:<arxiv>#<ref>` | `lit:1707.04448#thm1.2` |

## What the edges mean

- `formalizes` concept→decl OR concept→container (field-of-study altitude). Multi-to-multi by design.
- `xref` concept→external DB page (`xref:lmfdb_knowl:group.abelian`); evidence carries the Wikidata property.
- `depends` formal dependency (typed weights `w_types.{sig,def,proof}` — `sig` is the statement-level signal).
- `cites`/`matches` concept/decl→arXiv statement (dual-judge TheoremGraph matches; never copy statement text — link only unless `license_open`).
- `mentions` concept→decl citation in a WikiLean article — NOT a formalization claim.
- Every edge has `{provenance, confidence, evidence}`; rows with `evidence.skeptic == "pending"` are agent-proposed and not yet adversarially reviewed — treat as candidate-quality.

## Formal ↔ informal transfer recipes

- Concept → Lean: `node Q181296` → out-edges kind=formalizes → decls to cite/verify (then `mathlib-search decl` to double-check freshness).
- Lean decl → concepts/papers: `node decl:Mathlib:Module` → in-formalizes = the QIDs it covers; out-matches = judged arXiv statements.
- Field → its Mathlib home: `search "category theory" --type concept` → node → formalizes→`path:…` edge.
- `--full` scans brain/data/edges.jsonl for untruncated lists (shards cap at 200/direction).

## Remote (same data, live site)

`GET https://wikilean.jackmccarthy.org/api/brain/node?id=<id>` and
`/api/brain/search?q=…&type=…` — identical entries served from the deployed shards.
