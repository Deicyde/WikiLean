# Wikidata property proposal — "Mathlib declaration"

Draft for *Wikidata:Property proposal/Natural science*. **Modeled directly on
the accepted [Metamath statement ID (P12888)](https://www.wikidata.org/wiki/Property:P12888)**
(proposed as "Metamath statement label", created 2024-07-10) — the closest
precedent, since Metamath is a formalization system like Lean/Mathlib. Also in
the family of math-reference external IDs: ProofWiki ID (P6781), nLab ID
(P4215), MathWorld ID (P2812).

The field layout below matches the Metamath proposal so it can be pasted into
the `{{Property proposal}}` template with minimal editing.

> **Pre-submission checklist** (do these before posting on-wiki):
> - [ ] **Human-review the seed mappings** — REFRAMED 2026-07-02: the seed batch is
>   now the ~126 tags already **merged into mathlib master** (each passed ≥2 human
>   reviewers incl. a maintainer, plus CI) rather than the 815 AI mappings.
>   Sign-off artifacts (from `bot/export_property_seed.py`):
>   `bot/data/property_seed.tsv` (126 rows with PR provenance),
>   `property_seed_quickstatements.csv` (119 ready rows, `PXXXX` placeholder),
>   `property_seed_flags.tsv` (17 rows needing hand review). The 815-mapping
>   import can follow later via mix'n'match as phase 2.
> - [x] **Formatter URL is LIVE** (2026-07-02): `https://wikilean.jackmccarthy.org/decl/$1`
>   resolves the decl name to its current module (doc-gen4 index) and 302s to the
>   mathlib4_docs page; unknown names fall back to docs search; JSON (with reverse
>   WikiLean citations) on `Accept: application/json`. Code: `wiki/src/decl.ts`.
> - [x] Property doesn't already exist / wasn't already proposed (checked).
> - [x] Datatype chosen (external identifier); value scheme decided (Option A).
> - [x] Name decided ("Mathlib declaration").
> - [ ] Read *Wikidata:Creating a property proposal* and transpose into the
>   `{{Property proposal}}` preload template.
> - [x] Floated on the Lean/Mathlib Zulip; [ ] post to *Wikidata talk:WikiProject Mathematics*.

---

## Proposal (template fields)

| Field | Value |
|---|---|
| **Description** | identifier of the declaration in Mathlib (the Lean 4 mathematics library) that formalizes this mathematical concept |
| **Represents** | Lean (Q6509476) — *software for interactive and automated theorem proving* |
| **Data type** | External identifier |
| **Domain** | mathematical object (Q246672), mathematical concept (Q24034552) |
| **Allowed values** | `[A-Za-z_][A-Za-z0-9_'.]*` — a fully-qualified Lean declaration name (dot-separated namespaces; may contain `'`) |
| **Example 1** | Prime ideal (Q863912) → `Ideal.IsPrime` |
| **Example 2** | Group (mathematics) (Q83478) → `Group` |
| **Example 3** | Complete metric space (Q848569) → `CompleteSpace` |
| **Source** | https://leanprover-community.github.io/mathlib4_docs/ |
| **Formatter URL** | `https://wikilean.jackmccarthy.org/decl/$1` (resolver → Mathlib docs; see below) |
| **Wikipedia infobox source** | none — no math infobox carries a formalization parameter; this is novel structured data (as with Metamath P12888) |
| **Planned use** | import WikiLean's human-verified concept→declaration mappings via mix'n'match / QuickStatements |
| **Number of IDs in source** | Mathlib4 has tens of thousands of named declarations (large and growing); only those formalizing a notable concept are in scope |
| **Expected completeness** | will remain **incomplete** — most concepts have no Mathlib formalization, and most Mathlib declarations are low-level lemmas with no concept item |
| **Implied notability** | does not imply notability (Q62589320) |
| **WikiProject** | Mathematics (Q8487137) |

---

## Formatter URL — the one open question (where we differ from Metamath)

Metamath got a trivial formatter URL because its docs are **flat** — one page per label:

```
value "cayley"  →  https://us.metamath.org/mpeuni/$1.html  →  …/mpeuni/cayley.html
```

Wikidata formatter URLs only do literal `$1` substitution. Mathlib's docs are **hierarchical** (module path + declaration), so the same trick doesn't directly apply:

```
Ideal.IsPrime lives in module Mathlib.RingTheory.Ideal.Prime, rendered at
…/mathlib4_docs/Mathlib/RingTheory/Ideal/Prime.html#Ideal.IsPrime
```

Two viable schemes — this is the thing to settle in the proposal discussion:

- **Option A — value = declaration name** `Ideal.IsPrime` (recommended as the *identity*). Stable and citable; survives Mathlib's frequent module refactors. **Cost:** no pure-`$1` formatter URL; store the module as a qualifier and/or rely on a name→URL resolver.
- **Option B — value = docs path** `Mathlib/RingTheory/Ideal/Prime.html#Ideal.IsPrime`. Gives a clean formatter `https://leanprover-community.github.io/mathlib4_docs/$1` exactly like Metamath today. **Cost:** verbose, and it breaks whenever a declaration changes module — undercutting the "stable persistent URL" selling point.

**Decision: Option A.** The value is the **fully-qualified declaration name** (the durable, citable identity, which survives Mathlib's frequent module refactors). The Mathlib **module** is recorded as a qualifier so the docs URL is reconstructable. Because a bare `$1` substitution can't build the hierarchical docs URL from the name alone, the **formatter URL is served by a small WikiLean resolver** (`https://wikilean.jackmccarthy.org/decl/$1`) that looks up the declaration's current module and redirects to the Mathlib docs — so the link survives refactors even though the underlying module path changes. (If reviewers prefer no external dependency, the fallback is to omit the formatter URL and rely on the module qualifier; Option B is the last resort.)

---

## Motivation

Wikidata already links mathematical concepts to *informal* reference works (ProofWiki, nLab, MathWorld) and, since P12888, to the *formal* Metamath library. Mathlib is the de-facto-standard Lean 4 mathematics library, with versioned, stable per-declaration HTML documentation — the same qualities that justified P12888. Its standing was recognized by the **2026 Jean-Pierre Demailly Prize for Open Science in Mathematics**, awarded to Mathlib for its "exceptionally broad structural significance" to the mathematical community — independent evidence that it is an established, citable body of formalized mathematics. This property is its analogue and enables queries no current dataset can answer, e.g. *"which Wikidata mathematical concepts are formalized in Mathlib?"* or *"which Top/High-importance concepts are not yet formalized?"* (a formalization to-do list), joinable across the existing ProofWiki/nLab/Metamath links.

**This is the inverse of something Mathlib already does.** Mathlib carries an in-source `@[wikidata]` attribute that tags declarations with their Wikidata QID (e.g. `@[wikidata "Q863912"] theorem …`), and that coverage is actively being expanded. This property points the other way — from the Wikidata item back to the declaration — closing the loop. The two directions cross-validate each other, and the maintainer-curated `@[wikidata]` tags are an authoritative, high-quality source for populating and verifying the property (more reliable than any automated matching). That the Lean community already maintains the forward link is itself evidence the formal community values this integration.

## Anticipated concerns & responses (drawn from the P12888 discussion)

- **"Notability of low-level synthetic statements."** *(The main concern raised for Metamath.)* Not an issue here: this property goes **on the concept item** (which already exists and is notable) pointing **to** its primary declaration — we never create items for declarations, so low-level helper lemmas never become items. Pairs with `does not imply notability`.
- **"Definitions vs. theorems."** Metamath debated treating `df-*` and theorems separately. Irrelevant for us: the property points from a concept to its formalizing declaration regardless of whether that declaration is a `def`, `theorem`, `structure`, or `class`.
- **Licensing.** Mathlib is Apache-2.0 and its declaration names/docs are public; the concept→declaration mapping dataset is WikiLean's own and can be released CC0 for import.
- **Data quality / provenance.** Seed mappings are generated by LLM agents that read the Mathlib source, then **human-verified**; only reviewed values are uploaded. mix'n'match additionally allows community verification, as used for Metamath.

## Companion dataset

WikiLean publishes the mapping as QID-keyed RDF independent of this proposal, so it is usable whether or not the property is accepted:

```turtle
wd:Q863912 wl:mathlibDeclaration "Ideal.IsPrime" ;
           wl:mathlibModule "Mathlib.RingTheory.Ideal.Prime" ;
           wl:mathlibDocs <https://leanprover-community.github.io/mathlib4_docs/Mathlib/RingTheory/Ideal/Prime.html#Ideal.IsPrime> .
```

`wl:mathlibDeclaration` maps 1:1 onto the proposed property's value; `wl:mathlibModule` onto the module qualifier. ~815 human-reviewable mappings are ready as the initial import (via mix'n'match or QuickStatements).
