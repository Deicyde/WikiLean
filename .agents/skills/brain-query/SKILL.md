---
name: brain-query
description: Use when an agent needs the BRAIN — WikiLean's unified concept/dependency graph of mathematics — to look up a mathematical object from any handle on it (a Wikidata QID, a Mathlib/Lean decl, an external DB page, an article slug, an arXiv statement, or a Mathlib folder path), fetch its typed neighborhood (depends / links / mentions / cites / relates / co-page / co-statement / invocation / related / special_case / generalization bonds with provenance and evidence), see everything known about it in one call (Lean code ∘ Wikipedia article ∘ QID ∘ LMFDB/nLab/Stacks entries), get a containment breadcrumb, or search by label. Reach for it to transfer between informal concepts and formal Lean declarations in either direction, to find which QIDs a decl formalizes (multi-to-multi), which external DBs (LMFDB, nLab, Stacks, MathWorld, ProofWiki, …) a concept cross-references, or which folder of Mathlib is a field's formal home.
---

# brain-query

One stdlib-only CLI over the BRAIN dataset (`brain/SCHEMA.md` is the contract;
the v3 section is normative). Everything returns JSON on stdout; nonzero exit =
not found.

## The model: cells, organs, supercells, synapses

The node is a **cell** — an *atom* of mathematics, id `cell:<anchor>`. A Lean
decl, a Wikidata concept, an external-DB page, a WikiLean article and an arXiv
statement that denote **one object** are **organs** of that one cell.
`Module`, `Q18848` (module) and `Q125977` (vector space) are the *same atom* —
Mathlib has no `VectorSpace` because `Module` fully generalizes it. So don't
look for a vector-space decl; ask the atom.

- **organ** — a particle, never a node. Its content is **embedded**: one `cell`
  call returns the Lean docstring + code, the Wikidata description, and each
  licensed DB snippet. No fan-out.
- **supercell** — a Mathlib folder (`path:Mathlib/Algebra`). Owns
  **field-of-study concepts**: `Q82571` "Linear algebra" **is**
  `path:Mathlib/LinearAlgebra`, not a cell. That is the answer to "where does
  this field live formally?".
- **synapse** — ONE aggregated edge per atom pair: `w` (weight), a `kinds`
  histogram, and every `trace` ({kind, src, dst, prov, evidence}, src/dst being
  the ORGAN ids that witnessed it). **Undirected** — direction lives on each
  trace, so there is no `--dir`.

**Pass any organ id anywhere.** `aliases.json` maps every organ to its atom, so
a QID, a decl name, a slug or an `xref:` page all resolve.

```
python3 brain/query.py cell <key>            # ANY handle → the owning atom's card
python3 brain/query.py organs <key> [--kind decl|concept|page|article|statement]
python3 brain/query.py neighborhood <key> [--kinds depends,links] [--full]
python3 brain/query.py path <key>            # containment breadcrumb
python3 brain/query.py search <text> [--type cell|supercell]
python3 brain/query.py supercell <path>      # a folder: organs, children, cells
```

`unit` and `node` still work as aliases of `cell` (the unit card became the cell
card; v3 has no particle nodes).

## Ids

| kind | form | example |
|---|---|---|
| **cell** (the node) | `cell:<anchor>` | `cell:Q18848` |
| **supercell** (folder) | `path:<Lib>[/<Dir>…]` | `path:Mathlib/LinearAlgebra` |
| concept organ | bare QID | `Q181296` |
| decl organ | `decl:<Lib>:<FQ name>` (or a bare FQ name) | `decl:Mathlib:CommGroup` |
| page organ | `xref:<db>:<id>` | `xref:nlab:module`, `xref:stacks:0001` |
| article organ | the WikiLean slug | `Vector_space` |
| statement organ | `lit:<arxiv>#<ref>` | `lit:1707.04448#thm1.2` |

## Reading a cell

- `organs[].bond` says **why** the organ is in the atom: `exact` = it IS the
  atom (identity); `generalization`/`special_case` = it has no formal home of
  its own and attaches to its single best target; `xref` = a cross-reference;
  `field` = an area concept (on a supercell). An ungraded decl organ is the
  anchor of a lone-particle cell. There is no `confidence` on organs — that
  lived on the grounding edge the builder consumed.
- `counts` = the TRUE totals; `truncated.syn` = how many synapses the shard
  dropped (a **count**, not a flag). `tt` on a synapse = how many traces exist
  vs the ≤6 shipped.
- Page organs carry `snippet` + `snippet_license` only where the license permits
  (nlab/stacks/lmfdb/proofwiki/planetmath/oeis); mathworld/dlmf/eom/kerodon are
  ids+titles+links — deep-link out. **Never quote a snippet without its
  license**, and never copy arXiv statement text (link only unless
  `license_open`).
- `cell_review.jsonl` flags 23 atoms whose grades are suspect (a ballooned cell
  = bad AI tagger grades). If an atom looks too big to be one object, check
  there before trusting it.

## Synapse kinds

Exactly eleven — anything else returns `unknown_kinds`:

`depends` (formal dependency; `evidence.w_types.{sig,def,proof}` — `sig` is the
statement-level signal) · `links` (an internal page-to-page hyperlink inside one
external DB) · `mentions` (a decl cited on another atom's article — NOT a
formalization claim) · `cites` (literature) · `relates` (Wikidata P279/P361/…) ·
`co-page` and `co-statement` (both atoms cross-reference the same external page /
arXiv statement) · `invocation` and `related` (a formalizes grade that never merges)
· `special_case` and `generalization` (an attach grade that did NOT merge).

**`formalizes` and `matches` are NOT synapse kinds.** They are strong bonds that fuse
organs INTO one cell, so they never join two atoms — read them off an organ's `bond`
on the cell card. The old enum listed them (0 rows, always) and omitted five kinds
that carry real bonds, so a caller trusting it silently dropped them.

Rows whose `evidence.skeptic == "pending"` are agent-proposed and not yet
adversarially reviewed — treat as candidate-quality.

## Recipes

- **Concept → Lean**: `cell Q181296` → the `decl` organs (`--kind decl` to
  isolate). Then `mathlib-search decl <name>` to double-check freshness.
- **Lean decl → concepts/articles**: `cell CommGroup` → the `concept` +
  `article` organs, with descriptions and slugs.
- **An absorbed concept**: `cell Q125977` (vector space) answers `cell:Q18848`
  and its `Module` organ — the atom IS the answer.
- **Field → its Mathlib home**: `search "linear algebra" --type supercell`, or
  just `cell Q82571` → `path:Mathlib/LinearAlgebra`.
- **Untruncated evidence**: `neighborhood <key> --full` scans
  `brain/data/synapses.jsonl` for every synapse with every trace (the shards cap
  at 200 synapses / 6 traces). This is the ONLY way to get full traces —
  including for supercells, whose shard rows ship with no traces at all.

## Remote (same data, live site) + MCP

- `GET https://wikilean.jackmccarthy.org/api/brain/cell?key=<any organ id>`,
  `/api/brain/search?q=…&type=cell|supercell`,
  `/api/brain/transfer?q=…&direction=informal_to_formal|formal_to_informal`,
  `/api/brain/neighborhood?id=…&kinds=…&traces=`, `/api/brain/snippets?id=…`,
  `/api/brain/filter?f=<mask>&type=&under=` — full reference:
  `docs/BRAIN-API.md` or https://wikilean.jackmccarthy.org/brain/api
- Remote MCP for any agent:
  `claude mcp add --transport http wikibrain https://wikilean.jackmccarthy.org/mcp`
  (tools: brain_search, brain_cell, brain_transfer, brain_neighborhood,
  brain_snippets, brain_filter, decl_exists — `brain_unit`/`brain_node` still
  answer as aliases of `brain_cell`).
