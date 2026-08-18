# Factorial Stage-1 mechanical scoring (+ judge summary when folded)

Prereg: `docs/research/BRIDGE-FACTORIAL.md @ 3658bd58`. Rows: `/Users/jack/Desktop/LEAN/WikiLean/bench/data/runs_factorial @ ba35fe7f`. Rig: leanprover/lean4:v4.33.0-rc1, mathlib 9944fe2973b8.

Primary outcome (repaired oracle): produced AND zero classify_adjusted-hallucinated citations AND typecheck ok.

| arm | grounded-tc (repaired) | Wilson 95% | raw-oracle | typecheck ok | any-halluc (rep.) | capped | mean turns | mean tool calls |
|---|---|---|---|---|---|---|---|
| Ep | 31/100 (0.310) | [0.228, 0.406] | 26/100 | 33/100 | 9/100 | 18 | 20.43 | 30.36 |
| X | 41/100 (0.410) | [0.319, 0.508] | 31/100 | 42/100 | 5/100 | 22 | 21.39 | 30.32 |
| J | 39/100 (0.390) | [0.300, 0.488] | 36/100 | 39/100 | 11/100 | 0 | 17.06 | 26.76 |
| Dp | 39/100 (0.390) | [0.300, 0.488] | 39/100 | 40/100 | 10/100 | 6 | 17.42 | 26.65 |

decl_exists manipulation check: X 213 calls (94/100 runs), Dp 536 calls (92/100 runs); J/Ep 0 by construction (manifest-verified).
Informal-tool touches: Ep 2/100 runs, X 4/100 runs.
Run-phase cost: $40.91.
