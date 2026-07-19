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

## What remains before headline claims

1. Judge/BEq+ leg: `judge_bridge.py --arm A..E` then `--calibration 50` →
   Jack hand-grades 50 → report judge–human agreement FIRST (prereg rule).
2. Typecheck ≠ faithfulness: folded success can still reward well-formed
   wrong statements; the judge leg closes this.
3. Model generality: single model (haiku). A second-model grid needs per-model
   runs dirs first (noted in BRIDGE-ISSUES).
