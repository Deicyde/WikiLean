---
name: mathlib-search
description: Use when a reviewer/moderator bot must find or verify a Mathlib4 (Lean) declaration — confirm a cited decl name is real (not hallucinated/misspelled), discover the lemma that formalizes a stated proposition, or check a continuity/equality/inequality claim has a formalization. Reach for it whenever WikiLean text cites a `Mathlib.*`/Lean decl, or claims "this is formalized in Mathlib."
---

# mathlib-search

One stdlib-only CLI, `mathlib_search.py`, that finds/verifies Mathlib4 declarations three ways. No deps, no venv, no key for the defaults.

```
mathlib_search.py decl     <Decl.Name>          # EXACT existence + module/kind  (FIRST-line, zero elaboration cost)
mathlib_search.py loogle   "<pattern>"          # SYNTACTIC: name / type-pattern / |- conclusion  (zero hallucination)
mathlib_search.py semantic "<nl statement>"     # SEMANTIC: prose -> decl  (LeanSearch keyless default)
```

Add `--json` (works before OR after the subcommand) for machine output; default is human-readable. Every subcommand exits nonzero on "not found / error" so you can branch on it.

## WHEN to pick which (decision order)

1. **You were handed a specific decl NAME to check** (the text cites `Real.continuous_sin`, `Nat.add_comm`, a `Mathlib.*` decl) → **`decl`**. O(1) exact lookup: present ⇒ real (+ kind + module + canonical docs URL); absent ⇒ **hallucinated or misspelled — flag/reject**. This is the cheapest, highest-signal check; do it before spending any Loogle/semantic effort.
2. **You can express the claim as a TYPE or NAME pattern** (an equation, an inequality, "X is continuous", a naming hunch) → **`loogle`**. Precise, zero hallucination — it's a real Lean elaboration over Mathlib, so a hit's `name`+`type` are ground truth. Use it to DISCOVER the name from a statement's shape, or to corroborate that a shape is formalized.
3. **You only have PROSE / a concept and no shape** ("commutativity of addition", "the sine function is continuous", a Wikipedia sentence) → **`semantic`**. Returns the formal decl PLUS an LLM informal gloss you can compare against the source statement.

Typical reviewer flow: prose → `semantic` to surface a candidate name → `decl` to confirm that exact name exists → optionally `loogle --lucky` for the canonical docs URL to cite.

---

## `decl` — exact existence + module/kind

Resolves a dotted decl name against the doc-gen4 declaration-data index (~411k decls: Mathlib + Lean core + Std/Batteries + deps).

- Default fast path reuses WikiLean's pre-built prefix shards at `wiki/public/assets/decl-index/` (name+module, no kind, **no network**) when present.
- `--live` fetches the authoritative `declaration-data.bmp` (also returns **kind**: theorem/def/instance/...), caching it (~65 MB) in the skill's `.cache/` (gitignored) and using a conditional GET (`If-None-Match`) so repeat calls are a fast 304 cache hit, not a re-download. `--refresh` forces a full re-download (only needed when a new mathlib4_docs build shipped).

```
$ mathlib_search.py decl Real.continuous_sin
EXISTS: Real.continuous_sin
  kind:   (n/a from shards; use --live for kind)
  module: Mathlib.Analysis.SpecialFunctions.Trigonometric.Basic
  docs:   https://leanprover-community.github.io/mathlib4_docs/Mathlib/Analysis/SpecialFunctions/Trigonometric/Basic.html#Real.continuous_sin
  source: wikilean-shards

$ mathlib_search.py decl Real.continuous_sin --live    # adds kind, authoritative
  kind:   theorem    ...    source: declaration-data

$ mathlib_search.py decl Foo.totally_made_up_lemma ; echo $?
NOT FOUND: Foo.totally_made_up_lemma
  This exact decl name is not in declaration-data (hallucinated or misspelled).
1
```

`--json` shape: `{"name","exists":bool,"module","kind","docs","source"}`. **A clean `exists:false` is the negative signal a hallucinated-decl validator needs.** Use `--live` when you specifically need `kind`, or to settle a borderline case authoritatively (the shards can lag a fresh build by hours).

---

## `loogle` — syntactic search (Loogle JSON API, keyless)

A hit is `{name, type (signature, leading space, no name), module, doc}`. Top-level `count` is the TOTAL; `hits` is capped at the first 200 (and `--limit`, default 20, caps what's printed).

### Loogle DSL cheatsheet (comma = AND of all constraints)

| Form | Means | Example |
|---|---|---|
| `Real.sin` (bare dotted) | statement **mentions that constant** | `Real.sin, Continuous` |
| `"add_comm"` (quoted) | decl **NAME contains** the substring | `Real.sin, "add"` |
| `_` | wildcard subexpression | `_ * (_ ^ _)` |
| `?a` `?b` (named metavar) | reused hole, same value across the pattern | `Real.sqrt ?a * Real.sqrt ?a` |
| `\|- ...` or `⊢ ...` | matches the **main conclusion only** (after all hyps/arrows) | `\|- _ < _ → tsum _ < tsum _` |
| `⊢ (_ : Type _)` / `⊢ (_ : Prop)` | data-defs / theorems filter | `⊢ (_ : Prop)` |

Combine freely: `Real.sin, "two", _ * _, |- _ < _ → _`. Subexpression args match in **any order**.

```
$ mathlib_search.py loogle "Real.sin, Continuous"
1 match (heartbeats=4)
  Real.continuous_sin
    : Continuous Real.sin
    module: Mathlib.Analysis.SpecialFunctions.Trigonometric.Basic

$ mathlib_search.py loogle "|- ?a + ?b = ?b + ?a" --limit 3   # the commutativity lemmas
55 matches (heartbeats=157430)
  Nat.add_comm   (n m : ℕ) : n + m = m + n        module: Init.Data.Nat.Basic
  Int.add_comm   (a b : ℤ) : a + b = b + a        module: Init.Data.Int.Lemmas
  ...

$ mathlib_search.py loogle "Real.continuous_sin" --lucky    # known name -> canonical docs URL (302)
https://leanprover-community.github.io/mathlib4_docs/Mathlib/Analysis/SpecialFunctions/Trigonometric/Basic.html#Real.continuous_sin
```

### Loogle gotchas (bake these in)

- **NEVER send a bare un-anchored metavariable equation** like `?a + ?b = ?b + ?a` (no `|-`): it makes the engine enumerate ~18k `HAdd`/`Eq` decls and hits the deterministic 200000-heartbeat **timeout**. Always anchor under `|-` (the conclusion form is fast and succeeds) **or** add a constant constraint first (e.g. `HAdd.hAdd, |- ?a + ?b = ?b + ?a`).
- Error handling is automatic and clean (all are HTTP 200 with an `error` key; exit 1):
  - `unknown identifier 'Foo.bar'` → that constant **doesn't exist** in Mathlib. The CLI hints you to retry as a NAME-substring search (`loogle '"bar"'`) and to confirm spelling with `decl <Name>`.
  - parse error → fix the syntax (the message gives a column).
  - timeout → narrow the query (the CLI reminds you to anchor under `|-`).
- A genuine `count=0` with empty hits means "no such lemma / your pattern is wrong" — also a useful negative.
- `type` has a leading space and omits the decl name (the CLI strips/reformats it).
- `--lucky` is the fast path to a docs URL when you already trust the name (302 `Location`); it returns nonzero if there's no single best hit.

---

## `semantic` — natural-language search

`--engine` (default `leansearch`). Each result carries `name, module, kind, type, informal_name, informal_description, score`. The informal gloss is the gold for "does this decl formalize this Wikipedia statement?" — compare it to the source text.

```
$ mathlib_search.py semantic "commutativity of addition on natural numbers" --num-results 2
2 results from leansearch:
  Nat.instAddCommMonoid  [distance(lower=closer)=0.2108]
    kind: instance   type: AddCommMonoid ℕ   module: Mathlib.Algebra.Group.Nat.Defs
    gloss: Additive Commutative Monoid Structure on Natural Numbers
  IsAddCommutative  [distance(lower=closer)=0.2816]
    gloss: Commutativity of addition on M   desc: For all a,b in M, a+b=b+a.

$ mathlib_search.py semantic "the sine function is continuous" --engine leanfinder --num-results 1
  Real.continuous_sin  [similarity(higher=closer)=0.9737]   kind: theorem   type: Continuous Real.sin
```

### Engines: keyless-default vs key-gated

| `--engine` | Auth | Notes |
|---|---|---|
| `leansearch` | **keyless (DEFAULT)** | `leansearch.net`. ~1s. Best formal+informal pairing. Score is **distance, lower = closer**. |
| `leanfinder` | **keyless** | HuggingFace endpoint (opaque host; override via `LEAN_FINDER_URL`). Score is **similarity, higher = closer**. Strong intent-understanding; good A/B cross-check. |
| `numina` | **keyless** | `leandex.projectnumina.ai`, SSE-parsed for you. LLM query-expansion → good recall on hard/odd phrasings; no score/kind. Tertiary fallback. |
| `leanexplore` | **key-gated** | needs `LEANEXPLORE_API_KEY` (get one from leanexplore.com /api-keys). Adds multi-library coverage + dependency graph. Without the key you get a clean "set LEANEXPLORE_API_KEY" message (exit 2), never a stack trace. |

Confidence heuristic: if `leansearch` and `leanfinder` **agree** on the top decl → high confidence; if they disagree, or the cited name doesn't appear in either result set, **flag for human review**. Always finish by confirming the chosen name with `decl <Name>` before trusting it.

---

## Env vars & etiquette

- `LEANEXPLORE_API_KEY` — required only for `--engine leanexplore`.
- `LEAN_FINDER_URL` — override the (opaque, rotatable) Lean Finder HF endpoint.
- `LEANSEARCHCLIENT_LEANSEARCH_API_URL` — override the LeanSearch base URL.
- `MATHLIB_SEARCH_CACHE` — override the cache dir (default `<skill>/.cache/`, gitignored; falls back to /tmp logic if unwritable).
- `SSL_CERT_FILE` — point at a CA bundle if you hit cert-verify errors (the script auto-discovers system bundles / certifi first).

All calls send the descriptive UA `WikiLean-mathlib-search/1.0 (https://github.com/Deicyde/WikiLean; wikilean@jackmccarthy.org)`, a 30s timeout, and one retry with backoff on 429/5xx. These are small community services (Numina self-throttles to ~3 req/30s) — be conservative; cache and don't hammer. `decl` is local (shards) by default and costs nothing.
