# Bridge v2 — the bias-proof benchmark suite

> Drafted 2026-07-24 from four primary-source research sweeps (LeanExplore
> arXiv:2506.11085; LeanSearch arXiv:2403.13310 + v2 arXiv:2605.13137;
> TheoremGraph arXiv:2606.25363; kernel-graded landscape survey, sources at
> bottom). Motivation: Tier-1's remaining faithfulness leg depends on an LLM
> judge whose error could correlate with arm — Jack's "arm-correlated bias"
> critique. This suite replaces judge-dependent grading with third-party gold
> labels and kernel grading everywhere.

## What the field actually does (the four sweeps, one line each)

- **LeanExplore**: 300 AI-generated queries, NO gold labels, single-LLM-judge
  preference ranking (Gemini 2.5 Flash), order-permutation as the only bias
  control, nothing reusable, MCP never benchmarked. *Weaker than our Tier-1.*
- **LeanSearch v1**: 50 hand-written queries, human 3-tier qrels pooled from
  their own engine (pro-home bias, unaddressed), eval set claimed released but
  never shipped.
- **LeanSearch v2**: the fix — expert-built MathlibQR (946 rows) + MathlibMPR
  (69 post-cutoff PR theorems, expert premise groups, MECHANICAL group-recall),
  all released CC BY 4.0 at github.com/frenzymath/LeanSearch-v2.
- **TheoremGraph**: judge-free head-to-head on MathlibQR fair-810 (R@10/nDCG);
  their §7 statement-autoformalization eval is our Tier-1a design (24 fresh
  theorems; TC 22/24 but evaluated-correct 5/24) and needed a hand-checked LLM
  judge — they even rejected their first judge as over-generous. When a judge
  is unavoidable, calibration IS the field practice; the better answer is to
  pick tasks where no judge exists.

## The design principle

**A benchmark result is arm-bias-proof iff the grader existed before the arms
did.** Two graders qualify: fixed third-party gold labels (retrieval), and the
Lean kernel (proving). Every v2 tier uses one of the two. The LLM-judge
faithfulness leg of Tier-1 is DEPRIORITIZED, not repaired: Tier-1's judge-free
results (typecheck + hallucination + the fresh-set McNemar) stand as published;
the faithfulness upgrade comes from BEq+ (mechanical bidirectional `exact?`)
if we still want it, not from a calibrated judge.

## The suite (in order of leverage)

### V2-1. Premise retrieval — MathlibMPR (third-party gold, mechanical)
69 merged-PR theorems, >6mo post-cutoff, expert premise GROUPS (mean 2.96/query,
interchangeable within group), metric = group-Recall@10 / Covered@k.
Published baselines: LeanSearch-v2 reasoning 46.1, DIVER 38.0, ReProver /
LeanStateSearch / LeanPremise lower, TheoremGraph's concept-tuned config 16.5
(their negative transfer, framed as a scope boundary).
**Run Wikibrain as a retrieval system** (statement → ranked decls via
brain_bridge + neighborhood expansion). Pre-registered expectation: our
concept-tuned bridge underperforms specialist premise retrievers TODAY — this
benchmark then becomes the development target for `brain_premises`
(BRIDGE-ISSUES #7), with a held-out split discipline (tune on nothing; MPR is
eval-only; develop against LeanDojo B4's premise split instead).

### V2-2. Concept retrieval — MathlibQR fair-810 (third-party gold, mechanical)
810 expert-written query rows (6 styles) over 171 Mathlib targets, judge-free
metrics only (R@10, nDCG@10 — skip LSv2's judged ranking metric, per
TheoremGraph footnote 7). Published: TheoremGraph 0.775/0.548,
LSv2 retriever+reranker 0.780/0.623.
This is the "is the join a good concept index?" test on someone else's labels.

### V2-3. Sorry-filling — SorryDB (kernel-graded, contamination-proof by construction)
Live unsolved sorries from 78 real Lean repos; snapshot SorryDB-2601 = 1,000
tasks; success = the repo BUILDS with your proof (LeanInteract, in-situ).
Published agentic best ≈30%, union 35.7%; ReAct+LeanSearch is an existing
pattern, so ± wikibrain-MCP arms are directly comparable. No judge exists
anywhere in the loop; no solutions exist to memorize. **This is Jack's
sorry-filling idea, instantiated on the most contamination-resistant set
available.** Arms: no-tools / formal-only / wikibrain (D-vs-C-vs-A; E optional).

### V2-4. Frontier — FATE-H/X (kernel-graded, fresh expert formalizations)
FATE-H: SOTA 3%; FATE-X: SOTA 0%, includes beyond-Mathlib definitions; paper
ran pure sampling only — NO tool-augmented numbers exist. Any nonzero
tool-augmented FATE-X result would be a first. High variance, low cost to try
(100 problems/tier, pass@k small). Already named in our Tier-2 prereg.

### V2-5 (infrastructure permitting). miniCTX-v2
668 theorems, temporal cutoff 2024-11-28, regenerable, harness public
(cmu-l3/minictx-eval). The repo-context middle ground between MPR and SorryDB.

## Execution decisions (Jack, 2026-07-25: "Go for it")

- **Model: claude-sonnet-5** in every agent arm (not Haiku — Jack's call).
- **Maximal tool-usage telemetry**: raw stream-json transcripts retained per
  run (gzipped), tool_trace caps raised (inputs 2,000 chars, result heads
  4,000), plus the derived per-call trace. Disk is cheap; blindness isn't.
- Order: MathlibMPR + QR-810 first (system-mode = the API scored directly
  with no LLM, then agent-mode Sonnet±tools), SorryDB flagship after,
  FATE-H/X opportunistic.

## Infrastructure decisions

- **Adopt LeanInteract** as the version-agnostic REPL layer for V2-3/V2-4
  (SorryDB and RLMEval both use it; our typecheck.py stays for the pinned
  Tier-1 envs).
- Precedent for the agent shape: Numina-Lean-Agent (Claude Code + Lean MCP,
  12/12 on Putnam 2025) — general coding agent + MCP tools is a published,
  competitive pattern; lean-lsp-mcp exists off-the-shelf for the formal arm.
- Corrected-variant discipline: always pin + report forks (ProofNet# not
  ProofNet — already our choice; miniF2F-v2 if ever used; >50% of miniF2F v1
  and 31.8% of ProofNet's Lean4 port have statement errors).
- Runs dirs keyed by (benchmark, arm, model) this time — the second-model grid
  was blocked in Tier-1 by unkeyed dirs.

## What stays from Tier-1

The fresh-set result (D 42% vs E 16%, McNemar p<1e-4; hallucination 6.8% vs
17.7–26.3%) is already judge-free and stands. The trace-attribution findings
(35% bridge-attributed citations; decl_exists discipline) stand. Only the
"faithful@budget" judged metric is dropped/deferred to BEq+.

## Sources
LeanExplore arXiv:2506.11085 · LeanSearch arXiv:2403.13310 (EMNLP'24 Findings)
· LeanSearch-v2 arXiv:2605.13137 + github.com/frenzymath/LeanSearch-v2 (MathlibQR,
MathlibMPR, FATE-H.jsonl, CC BY 4.0) · TheoremGraph arXiv:2606.25363 +
theoremsearch.com · SorryDB arXiv:2603.02668 + sorrydb.org · FATE
arXiv:2511.02872 + github.com/frenzymath/FATE · miniCTX arXiv:2408.03350 +
l3lab/miniCTX-v2 · LeanDojo arXiv:2306.15626 (premise split; ReProver ablation
51.2/26.3 with retrieval vs 47.6/23.2 without) · LeanAgent arXiv:2410.06209
(162 real-repo sorries closed) · Numina-Lean-Agent arXiv:2601.14027 ·
LeanInteract github.com/augustepoiroux/leaninteract · miniF2F-v2
arXiv:2511.03108 · ProofNet#/ProofNetVerif arXiv:2406.07222 · benchmark-faults
survey arXiv:2606.29493.
