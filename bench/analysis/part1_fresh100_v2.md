# Part 1 — fresh-100 grounded-typecheck, arm-E 429 block repaired (v2)

Reproduce: `python3 bench/analysis/score_e31_v2.py` (fresh rig: leanprover/lean4:v4.33.0-rc1, mathlib 9944fe2973b8; metric: produced ∧ no-halluc ∧ TYPECHECK (judge pending calibration)).

## Fresh-100 grounded-typecheck (all arms, all rows completed)

| arm | k/n | rate | Wilson 95% CI |
|---|---|---|---|
| A | 20/100 | 0.200 | [0.133, 0.289] |
| B | 22/100 | 0.220 | [0.150, 0.311] |
| C | 25/100 | 0.250 | [0.175, 0.343] |
| D | 42/100 | 0.420 | [0.328, 0.518] |
| E | 30/100 | 0.300 | [0.219, 0.396] |

## McNemar (exact binomial two-sided), full 100 pairs

- **D vs E**: both=17, D-only=25, E-only=13, neither=45 -> p = **0.07295**
- **E vs A**: both=11, E-only=19, A-only=9, neither=61 -> p = **0.08716**
- **E vs B**: both=12, E-only=18, B-only=10, neither=60 -> p = **0.1849**
- **E vs C**: both=15, E-only=15, C-only=10, neither=60 -> p = **0.4244**
- **D vs C**: both=14, D-only=28, C-only=11, neither=47 -> p = **0.009475**
- **D vs A**: both=13, D-only=29, A-only=7, neither=51 -> p = **0.000313**

D-vs-E absolute risk difference (paired Wald): **+0.120** 95% CI [+0.002, +0.238]

## Continuity: completed-69 (from tier1_reanalysis.json, unchanged)

| arm | k/n | rate | Wilson 95% CI |
|---|---|---|---|
| A | 10/69 | 0.145 | [0.081, 0.247] |
| B | 12/69 | 0.174 | [0.102, 0.280] |
| C | 12/69 | 0.174 | [0.102, 0.280] |
| D | 28/69 | 0.406 | [0.298, 0.524] |
| E | 16/69 | 0.232 | [0.148, 0.344] |

McNemar on the 69 completed pairs (tier1_reanalysis.json):

- **D vs E**: both=10, D-only=18, E-only=6, neither=35 -> p = 0.02266
- **D vs C**: both=7, D-only=21, C-only=5, neither=36 -> p = 0.002494
- **D vs A**: both=6, D-only=22, A-only=4, neither=37 -> p = 0.000534

(Of the 31 repaired E rows, 14 became successes; 17 remain failures. Per-cell detail: bridge_summary_v2.json `v2_provenance.changed_cells`.)
