# Bridge Experiment — results log

> Companion to `BRIDGE-EXPERIMENT.md` (the preregistration; deviations 1–7
> recorded there). Numbers here are mechanical (typecheck + oracle); the
> judge/BEq+ leg is PENDING human calibration and no judge number appears here.
> Model: claude-haiku-4-5 in all arms. Arms differ ONLY in tools.

## Tier 1a — ProofNet# eval (n=371/arm, scored 2026-07-19)

success = produced ∧ no hallucinated citation ∧ typecheck (REPL-server graded)

| metric | A none | B wiki | C formal | D wikibrain | E B+C unjoined |
|---|---|---|---|---|---|
| success (folded) | 59.6% | 57.1% | 62.3% | **64.1%** | 60.9% |
| success_proxy (no-halluc) | 76.8% | 72.8% | 72.2% | **84.6%** | 71.2% |
| typecheck ok | 66.6% | 63.6% | 81.9% | 74.7% | **83.3%** |
| halluc-decl rate | 10.1% | 11.0% | 10.7% | **5.9%** | 11.3% |
| runs w/ halluc | 86 | 101 | 103 | **57** | 107 |

McNemar D-vs-E (preregistered): 63 vs 51 discordant, exact p=0.30 — direction
favors D, underpowered at this effect size. A at 59.6% with zero tools =
substantial ProofNet memorization (the motivation for Tier 1b).

## Tier 1b — FRESH set (n=100/arm, decls newer than the model's training data)

The contamination-proof cut. Memorization stripped, the field reorders:

| metric | A none | B wiki | C formal | D wikibrain | E B+C unjoined |
|---|---|---|---|---|---|
| success (folded) | 20% | 22% | 25% | **42%** | 16% |
| halluc-decl rate | 21.2% | 17.7% | 20.9% | **6.8%** | 26.3% |
| runs w/ halluc | 54 | 48 | 49 | **23** | 36 |

- **McNemar D-vs-E: 32 vs 6 discordant, exact p < 0.0001.** Also D-vs-C
  p=0.0095, D-vs-A p=0.0003. The preregistered hypothesis test is decisive on
  the held-out set: the JOIN carries the effect, not tool volume.
- A collapses 59.6% → 20% off-distribution (memorization quantified).
- D's hallucination advantage WIDENS off-distribution (6.8% vs 17.7–26.3%).
- E (both toolsets, unjoined) is WORST at 16% and produced no decl at all in
  31/100 fresh runs — unjoined tool volume can be actively harmful.

## Trace attribution (deviation-7 telemetry, eval C/D/E)

- 98–100% of traced tool-arm runs cite ≥1 decl that visibly surfaced in a tool
  result; only 10–17% of citations never touched the tools.
- **35% of arm-D runs cite a decl that came out of a brain_bridge /
  brain_transfer result** (English in → formal name out) — an UNDERCOUNT
  (result heads truncate at 200 chars).
- Checked-and-cited-anyway rate among *hallucinated* citations: C 35%, E 30%,
  **D 13%** — binary `decl_exists` verification disciplines the model where
  fuzzy search (loogle/grep neighborhoods) lets it fool itself.

## Bridge v2 — third-party retrieval benchmarks (scored 2026-07-25)

Model claude-sonnet-5; arms N (no tools) / F (loogle+decl_grep+decl_read) /
W (wikibrain MCP); mechanical scoring only; full stream-json transcripts
retained per run (bench/v2/runs/). Data: frenzymath/LeanSearch-v2@94f4888.

MathlibQR fair-810 (concept retrieval, R@10 / nDCG@10):
- system-mode (one brain_bridge call, no LLM): **0.036 / 0.031** — the API's
  free-text entry is a label resolver, not a semantic retriever (nickname
  queries score 6x the descriptive styles). The headline API gap.
- agent N: 0.633 / 0.598 (143/810 rows = format non-compliance, scored 0)
- agent F: 0.831 / 0.790 · agent W: 0.816 / 0.781 — **statistically tied**
  (paired 66 vs 78 discordant, exact p=0.36); both nominally above the
  published retriever rows (TheoremGraph .775, LSv2+rerank .780) with the
  apples-to-oranges caveat (agents reason + verify; retrievers embed once).
- Style texture: W wins special_case 0.523 vs 0.384 (the Brain's
  special_case bonds are the signal there); F wins lean-style queries.
- Tool census: F mean 2.2 calls (grep-heavy); W mean 3.5 (decl_exists 1251,
  brain_bridge 608 — the verify-then-cite habit again).

MathlibMPR (premise retrieval, group-recall@10):
- system-mode wikibrain: 0.000 · agent N: 0.203 · agent W: 0.272 ·
  agent **F: 0.453** — vs published LSv2 0.461 / DIVER 0.380.
- A generic Sonnet agent with grep matches the specialist premise retriever;
  wikibrain helps over memory (+7pp) but trails formal search by 18pp.
  The pre-registered concept≠premise boundary, now measured on our tools —
  brain_premises (BRIDGE-ISSUES #7) has a quantified 18pp target.

WF — the union arm (W∪F tools + the evidence-based AGENT_MANUAL.md,
2026-07-25; the manual is part of the condition, ablatable):
- **QR-810: 0.885 R@10 / 0.839 nDCG@10** — beats F (paired 71-27 discordant,
  p<1e-4) and W (83-27, p<1e-4); +10.5pp R@10 over the best published
  retriever row. Style spread narrows: every style ≥0.55, five of six ≥0.81.
- **MPR: 0.557 group-recall@10** — beats F (+10pp) AND the specialist SOTA
  (LSv2 0.461) on their own benchmark.
- Tool mix (mean 4.6 calls/run): decl_grep 1465 · decl_exists 925 ·
  brain_bridge 684 · decl_read 512 · loogle 398 — genuine dual-toolkit use:
  Brain for informal entry + verification, formal search for discovery.
  brain_cell misuse: gone (manual adherence).
- Reading: the toolkits are COMPLEMENTARY, and a briefed agent composes
  them into a new best row on both third-party benchmarks. The join plus
  the territory beats either alone — the Bridge thesis, restated in
  retrieval form on gold labels we didn't write.

## What remains before headline claims

1. Judge/BEq+ leg: `judge_bridge.py --arm A..E` then `--calibration 50` →
   Jack hand-grades 50 → report judge–human agreement FIRST (prereg rule).
2. Typecheck ≠ faithfulness: folded success can still reward well-formed
   wrong statements; the judge leg closes this.
3. Model generality: single model (haiku). A second-model grid needs per-model
   runs dirs first (noted in BRIDGE-ISSUES).
