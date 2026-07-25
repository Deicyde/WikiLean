# Tool manual: searching Mathlib with the Brain + formal search

You have two complementary toolkits. Every claim in this manual is measured
from ~5,500 logged tool calls across 3,500 benchmark runs (Tier-1 +
Bridge v2); numbers in brackets are those measurements.

## The mental model

- **The Brain (wikibrain, 8 tools)** is a *curated knowledge graph* joining
  informal mathematics (Wikipedia/Wikidata concepts, external databases) to
  Mathlib declarations. It knows what things *mean* and how concepts relate —
  but it only "atomizes" declarations that some concept, annotation, or tag
  claims (~12k of Mathlib's declarations). It is a map, not the territory.
- **Formal search (3 tools)** operates on *all* of Mathlib directly: type-
  pattern search (loogle), source grep, source reading. It knows everything
  that exists but nothing about what it means informally.

Rule of thumb: **route by what you're holding.** Holding a concept described
in words → start with the Brain. Holding a type shape or name fragment →
start with formal search. Always finish with `decl_exists` on every name you
intend to output.

## Formal search tools

### loogle
Type-pattern and constant search (the public loogle.lean-lang.org service).
- USE for: "a lemma whose statement looks like `_ * _ ≤ _ * _`", searches by
  involved constants (`Real.sqrt`, `tsum`), name substrings.
- Strengths: covers ALL of Mathlib; ~0% call errors [578 calls, 0 errors];
  the best premise-finding tool — the loogle+grep toolkit matched a
  specialist premise retriever [group-recall@10 0.453 vs 0.461 SOTA].
- Weaknesses: needs Lean syntax fluency; a wrong pattern silently returns
  unrelated hits; small result payloads mean you often need decl_read next.

### decl_grep
Regex search over the Mathlib source checkout (ripgrep).
- USE for: name fragments ("cantor"), notation, docstring phrases, finding
  the file a declaration lives in.
- Strengths: the workhorse [1,206 calls, 0% errors]; catches things loogle's
  type index can't (comments, docstrings, naming conventions).
- Weaknesses: lexical only — if the concept's name doesn't appear in source,
  grep cannot find it (descriptive queries like "special case of X" score
  worst for grep-based agents [nDCG 0.384 vs the Brain's 0.523]).

### decl_read
Read declaration source (with surrounding context) from the checkout.
- USE for: verifying a candidate actually says what you think BEFORE citing
  it; getting exact signatures and hypotheses.
- Strengths: ground truth for any decl in Mathlib [397 calls, 0% errors].
- Weaknesses: you need the file/name first — it is a confirmation tool, not
  a discovery tool.

## Brain tools

### brain_bridge — THE ENTRY POINT for informal→formal
Free text in, existence-verified declarations + owning atoms out, with
signatures, import lines, and one-hop dependency context.
- USE for: "the concept described as ⟨words⟩" — especially nicknames and
  special-case descriptions, where the Brain's curated bonds beat grep
  [special_case nDCG 0.523 vs 0.384; nickname 0.779 vs 0.757].
- CRITICAL LIMITATION (measured): its free-text matching is label/alias
  anchored, NOT semantic. A single call with a descriptive paraphrase
  usually misses [single-shot benchmark: 3.6% R@10 vs 82% for an agent that
  reformulates]. **Never accept one miss as the answer: reformulate 2–4
  times** — try the concept's canonical name, a synonym, the head noun alone.
  8.9% of calls return match:"none" [727 calls]; that response includes
  nearest-candidate suggestions — read them.
- The `hits` list is existence-verified; bond quality matters: `exact` means
  the decl IS the concept; `formalizes`/weaker bonds are nearby.

### brain_search
Fuzzy label + alias search returning atoms (cells).
- USE for: resolving a name you half-know into an atom id; browsing what the
  Brain calls something [400 calls, 0% errors — the reliable fallback when
  brain_bridge misses].
- Weaknesses: returns atoms, not ranked decls — follow with brain_cell on
  the winning atom.

### brain_cell
The full atom card: Lean code, Wikidata description, DB snippets, organs.
- USE for: everything known about ONE object after bridge/search resolved it.
- **THE #1 MISUSE [63% of 482 calls failed]: calling it with a bare
  `decl:Mathlib:Name` for an arbitrary declaration.** The Brain only has
  atoms for concept-joined decls; "unresolvable key" means *no atom owns
  this decl*, NOT that the decl doesn't exist. For arbitrary decls use
  decl_exists (works for all of Mathlib) and decl_read for source. Call
  brain_cell only with ids that came OUT of a Brain tool.

### decl_exists — THE DISCIPLINE TOOL
Batch existence check for declaration names (backed by the full oracle —
covers ALL of Mathlib, unlike brain_cell).
- **USE ALWAYS, on every name you are about to output.** This is the
  measured difference between grounded and hallucinated citations: agents
  that verified before citing cut hallucinated-but-cited-anyway to 13% vs
  33% for agents relying on fuzzy search results alone. It is cheap
  [1,473 calls, 0.1% errors] and batched — check 10 names in one call.
- Also returns verified-rename suggestions for stale names.

### brain_neighborhood
The synapse graph around an atom (depends/links/relates, cursored).
- USE for: "what connects to X" — finding the connecting lemma between two
  concepts, walking dependencies.
- Weaknesses: needs a valid atom id [24.8% of calls errored — same
  unresolvable-key trap as brain_cell; resolve the atom first].

### brain_transfer / brain_snippets / brain_filter
Narrower tools: label↔decl jumps for KNOWN labels (transfer), stored source
snippets (snippets), facet enumeration (filter). High misuse rates when fed
non-atom ids [40%/57% errors]. Prefer brain_bridge/brain_search entry; reach
for these only on ids the Brain already returned.

## Routing playbook

| You are holding | Do this |
|---|---|
| A concept in words | brain_bridge → (miss?) reformulate → brain_search → brain_cell |
| A special case / nickname | brain_bridge (the Brain's best terrain) |
| A type shape | loogle |
| A name fragment | decl_grep |
| Premises for a proof | loogle + decl_grep FIRST [0.453 vs 0.272], Brain for concept grounding |
| A candidate name to cite | decl_exists (batch, always) → decl_read if the statement matters |
| An atom id from any Brain tool | brain_cell / brain_neighborhood freely |
| A bare decl name needing info | decl_exists + decl_read — NOT brain_cell |

## Anti-patterns (each one measured in prior runs)

1. **Citing unverified names.** 33% of hallucinated citations came from
   agents that had search evidence in front of them and cited a wrong name
   anyway. decl_exists is binary and kills this.
2. **One query, then giving up.** The single-call miss rate on descriptive
   queries is ~96%; agents that reformulate reach ~82%. Iteration IS the
   algorithm.
3. **brain_cell as a generic decl inspector.** 63% failure rate. Brain
   atoms ≠ all of Mathlib.
4. **Answering outside the requested format.** 143/810 no-tool runs were
   scored 0 for exactly this. Re-read the output contract before replying.
5. **Inventing tool names** (e.g. `logue`, `lookie`). Use the names above.
