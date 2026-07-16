# The Bridge Experiment — testing whether informal↔formal joining moves end-task Lean performance

> Answers Jack's question (2026-07-16): *what is the best way to test the hypothesis
> that easy informal↔formal jumping is necessary for difficult autoformalization /
> math tasks?* Grounded in a 6-agent research sweep over TheoremGraph, LeanSearch/
> LeanExplore, PutnamBench/miniF2F/ProofNet, formal-conjectures/Erdős, and the
> ablation literature (full sourced reports: the `bench-hypothesis-research`
> workflow, 2026-07-16). Companion: `bench/README.md` (the existing retrieval
> bench, which this DEMOTES to a diagnostic), `docs/BRAIN-API.md`.

## The headline finding from the literature

**TheoremGraph (arXiv:2606.25363) already ran the closest experiment, at n=24, and
the bridge won.** Retrieval over judged informal↔formal matches raised
evaluated-correct autoformalization from 5/24 to 8/24 while cutting output tokens
**52k → 14k** and tool calls **276 → 68** versus grinding through the formal library
with grep. And typechecking was exposed as a non-signal: the ungrounded arm
typechecked 22/24 while only 5/24 were actually correct. That is Jack's hypothesis
in miniature — *faster, less tedious, less context* — but at statement level, one
model, n=24. Our job is the same experiment done properly, plus the control nobody
has run.

**The open slot in the literature:** every published ablation (ReProver ±retrieval,
Draft-Sketch-Prove ±informal drafts +5.3pp, DeepSeek informal CoT +10.6pp, miniCTX
file context +15pp) compares *tool-on vs tool-off*. **Nobody has separated "has both
corpora" from "has the dictionary between them."** That separation is precisely what
the Wikibrain claims to be, and testing it is genuinely novel.

## What the hypothesis predicts (operationalized — "necessary" is unprovable)

- **P1 (the bridge beats its halves):** an agent with the *joined* dictionary beats
  an agent given an informal search tool AND a formal search tool *separately*.
  If P1 fails, the value was "access to both corpora," not the bridge.
- **P2 (cheaper reasoning):** the bridge arm solves at materially lower cost —
  tokens, tool calls, compile iterations (TheoremGraph's 52k→14k is the target).
- **P3 (formal grounding is load-bearing):** the bridge lifts *faithfulness*
  (equivalence-checked success), not just compile rate, and cuts hallucinated-decl
  citations.

We test the operational surrogate of "necessary": bridge access raises success at
fixed budget, and no-bridge arms plateau under budget escalation (pass@k curves).

## The five arms — identical model, prompts, budgets; only the tool manifest differs

| Arm | Tools | Isolates |
|---|---|---|
| A `no_tools` | none (typecheck only) | floor |
| B `informal-only` | Wikipedia/nLab search+fetch (raw text, no Lean mapping) | "informal reasoning is faster" alone |
| C `formal-only` | loogle + name-grep over pinned Mathlib + semantic decl search | the LeanSearch-class status quo |
| D `wikibrain` | the full MCP (search/cell/transfer/decl_exists/neighborhood/snippets) | the bridge |
| E `B+C unjoined` | informal search AND formal search, **no join** | **the decisive control: D>E ⇒ the JOIN carries the effect** |

Equalize surface area (2–3 tools per arm B/C so D's tool count isn't a confound);
log calls per tool. Harness: `bench/run_benchmark.py` (Max-auth `claude` CLI,
resumable) with per-arm `--mcp-config`; typechecking via a pinned Lean toolchain +
Mathlib rev recorded in every result row. Two models: Haiku-class full grid +
Sonnet/Opus-class on the primary set (a bridge that only helps weak models is a
different finding).

Budget per problem, all arms identical: ≤30 turns, ≤4 typecheck calls (statement
tasks) / ≤8 reflection rounds (proof tasks), 10-min wall-clock, pass@1 grid +
3 reseeds on a 50-problem subset (reseed SD is ±1.4pp per VERITAS).

## Tier 1 — PRIMARY: statement autoformalization (the informal→formal jump itself)

- **1a. ProofNet# (371 problems)** — NL statement → Lean 4 decl ending `:= sorry`.
  Undergraduate textbook math = exactly where Wikipedia/Wikidata/Mathlib coverage is
  dense; SOTA ≈45% leaves headroom; 371 *paired* problems give McNemar real power
  (~5pp detectable — unpaired designs at n≈244 cannot resolve <6pp).
- **1b. Fresh-decls set (~100, contamination-proof)** — TheoremGraph's recipe at 4×
  their n: theorems new in Mathlib v4.(N), every index pinned at v4.(N−1), query =
  back-translated informal statement, decl name stripped. **Determinacy pre-screen**
  (TheoremGraph lost ~40% of targets to underdetermined informal statements): two
  independent formalizers must produce equivalent statements from the informal text
  or the target is excluded (fraction reported). Regenerates every Mathlib release —
  doubles as the ongoing regression bench.

**Grading — typecheck is NEVER success** (TheoremGraph: 22/24 TC, 5/24 correct):
1. **faithful@budget** = typechecks AND BEq+-equivalent to gold (ProofNetVerif,
   validated on 3,752 human annotations; Lean-FRO Comparator as fallback).
2. LLM judge (dual strict/evaluated) **calibrated on 50 human-graded items first** —
   TheoremGraph dropped an over-generous judge after expert audit; assume ours needs
   the same treatment and report judge–human agreement.
3. Cost axes: tokens, tokens-to-solve, tool calls, compile iterations.
4. **Hallucinated-decl rate** — nonexistent names cited per run (the cleanest
   mechanism-level signature; `decl_exists` should crush it in arm D).

Analysis, preregistered (the commit hash of this file = the preregistration):
McNemar D-vs-E (the hypothesis test) and D-vs-C (vs status quo), discordant-pair
overlap tables, per-arm cost distributions, pass@k on the reseed subset. Success =
D>E on faithful@budget (p<.05) AND D≤E on tokens-to-solve.

## Tier 2 — proof-level end task

Replicate **LeanSearch-v2's downstream protocol** (the field's cleanest
retrieval→proving evidence: 4%→20% on FATE-H): one fixed reflection-prover loop,
swap only the tool arm. Sets: FATE-H (100) + MathlibMPR-Prop (50, post-cutoff
Mathlib PRs). Arms A/C/D/E. **Expected and pre-registered:** arm D may LOSE to arm
C here — TheoremGraph proved concept retrieval and premise retrieval need different
signals (their concept-tuned config *lowered* premise Recall@10 0.224→0.165). If
that happens the conclusion is "the API needs a premise mode" (see requirements),
not "the hypothesis is false."

**PutnamBench** (672 Lean problems, informal+formal statements shipped) is the
visibility play, not the hypothesis test — its leaderboard for agentic
answer-construction is nearly empty, so any credible entry is first-on-board; but
competition math needs little concept dictionary, so a null there is uninformative.

## Tier 3 — the unsolved-problems track (long-horizon, zero contamination)

- **3a. Offline re-formalization (the measurable core):** sample 150 accepted
  statements from `google-deepmind/formal-conjectures` ErdosProblems/ (496
  available). Agent sees ONLY the informal source (erdosproblems.com + linked
  Wikipedia); produces a `sorry`'d statement per arm; graded by equivalence vs the
  accepted repo statement, plus **definition-reuse rate** (the repo's dominant
  review complaint — bespoke definitions instead of Mathlib's) and
  **counterexample-audit survival** (a fixed disprover budget against each produced
  "open" statement — trivially decidable ⇒ misformalized; the repo's own AlphaProof
  audit mechanism).
- **3b. Live queue (descriptive, NOT inference):** work the open Erdős-milestone
  issues with the arm-D agent **under Jack's review**; only Jack-approved PRs are
  submitted, with AI disclosure and NO Co-authored-by trailer (the CLA blocker that
  hit PR #4377). Record build-pass rate, reviewer faithfulness comments, acceptance
  vs the ~56% base rate — and harvest the tool-call traces as the API's design
  feedback loop.

## What the API/MCP must gain (derived from what the papers' agents needed)

Implement now (cheap, shard-served):
1. **Batch `decl_exists`** — a list of names in, per-name verdict + canonical rename
   out (agents draft statements citing 3–8 decls; TheoremGraph's winning arm used
   68 tool calls vs 276 — round-trip economy is a measured outcome).
2. **Full Lean signature + module + `import` line on every decl hit** — existence
   plus name is not enough to write compiling code.
3. **Generalization surfaced per-hit** — when the exact formalization doesn't exist
   but the atom holds a `generalization` (VectorSpace→Module), say so per-hit with
   the breadcrumb. This is the dominant dictionary failure in formal-conjectures
   reviews ("reuse existing definitions") and the v3 cell layer already encodes it.
4. **Honest abstention** — a calibrated "no confident match" with nearest neighbors
   + confidence, never a forced weak match (forced retrieval CREATES the
   hallucinated-citation failure the bridge exists to prevent).
5. **Cursored, filterable neighborhood** — pagination cursor, bond-kind+confidence
   filters, stable order (a 60-minute agent must walk chains across turns without
   truncation surprises — the "extreme minority / silent cap" bug class).
6. **Snapshot echo** — every response carries the brain snapshot id + Mathlib pin
   (held-out evaluation is dishonest without it). Full `?rev=` time-travel comes
   later with archived snapshots.
7. **One composite `brain_bridge` call** — informal statement in → top decls with
   signatures, existence-verified, generalization-flagged, one-hop depends out —
   collapsing the common 3-call chain (search→cell→transfer) into one turn.
8. **Tool descriptions that teach the canonical mid-proof sequence.**

Deferred (needs new data/infra — tracked, not forgotten):
- **`brain_premises(goal_state)`** — premise selection is a DIFFERENT ranking
  problem (TheoremGraph's negative result). Needs a premise-level index.
- **Ingest formal-conjectures + erdosproblems.com as first-class atoms**
  (`decl:FormalConjectures:*` joined via the teorth/erdosproblems YAML) — Tier 3
  is only a Wikibrain test if the brain spans that corpus.
- Statement-level embedding transfer (LSv2-style sketch-then-retrieve) — currently
  transfer ranks by concept labels/aliases; hypotheses get lost (TheoremGraph:
  slogans compress away hypotheses).

## Threats to validity (the ones that bite, with mitigations)

- **Contamination** (ProofNet# is memorized): paired design cancels level effects
  but shrinks the tool delta; Tier 1b/3 are contamination-proof; report side by side.
- **"More tools" confound**: arm E answers it; make arm C genuinely strong (no
  strawman grep); equalize tool counts and framing.
- **Judge bias**: BEq+ primary; judges calibrated against humans and reported.
- **Power**: retrieval effects run 3–6pp; pair everything, McNemar, preregister.
- **Version drift** (miniF2F forks: 16 unprovable + 85 altered statements across
  Lean versions): one pinned toolchain/Mathlib/brain snapshot per campaign, recorded
  per row; never compare across pins.
- **Gold circularity**: the existing 180-task bench draws gold from the same
  grounding the brain serves — it stays a diagnostic; all end-task golds external.
- **Tier-2 D<C is pre-registered as an API finding**, not a hypothesis failure.

## Execution order

1. Freeze prompts on the existing dev split; this doc's commit = preregistration.
2. Build arm B/C/E manifests + the grading rig (BEq+ via ProofNetVerif; judge
   calibration set of 50).
3. Tier 1a full grid → first number. Then Tier 1b fresh set.
4. Tier 3a offline Erdős set (same grading rig).
5. Tier 2 proving loop (most expensive; after Tier 1 confirms the arms behave).
6. Tier 3b live queue as ongoing background, under Jack's review.
