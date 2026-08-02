# Bridge fresh-set blind LLM-judge equivalence grading (exploratory)

> **UNCALIBRATED LLM JUDGE — exploratory numbers only. The judge is claude-sonnet-5 (blind: sees only informal statement, gold formal statement + its variable context, and the candidate output; no arm identity, no tools, empty cwd). The preregistered 50-item human calibration of judge-vs-human agreement remains UNDONE (planned as future work), so these rates must not be read as confirmatory. The grounded-typecheck leg (bridge_summary_v2.json) is the mechanical anchor; the judge adds an equivalence signal on top.**

Reproduce: `python3 bench/analysis/judge_fresh_run.py` then `python3 bench/analysis/judge_fresh_summary.py`. Judge: claude-sonnet-5 over 500 candidate outputs (0 no-output rows auto-failed; 0 judge errors). Total judge cost (CLI-reported): $81.86.

## Fresh-100 — strict equivalence (same proposition, same hypotheses)

| arm | k/n | rate | Wilson 95% CI |
|---|---|---|---|
| A | 11/100 | 0.110 | [0.062, 0.186] |
| B | 9/100 | 0.090 | [0.048, 0.162] |
| C | 43/100 | 0.430 | [0.337, 0.528] |
| D | 8/100 | 0.080 | [0.041, 0.150] |
| E | 46/100 | 0.460 | [0.366, 0.557] |

## Fresh-100 — evaluated equivalence (mathematical equivalence, high confidence)

| arm | k/n | rate | Wilson 95% CI |
|---|---|---|---|
| A | 17/100 | 0.170 | [0.109, 0.256] |
| B | 16/100 | 0.160 | [0.101, 0.244] |
| C | 51/100 | 0.510 | [0.413, 0.606] |
| D | 19/100 | 0.190 | [0.125, 0.278] |
| E | 53/100 | 0.530 | [0.433, 0.625] |

## Fresh-100 — conjunction: grounded-typecheck (v2) AND judge-evaluated

| arm | k/n | rate | Wilson 95% CI |
|---|---|---|---|
| A | 8/100 | 0.080 | [0.041, 0.150] |
| B | 9/100 | 0.090 | [0.048, 0.162] |
| C | 16/100 | 0.160 | [0.101, 0.244] |
| D | 15/100 | 0.150 | [0.093, 0.233] |
| E | 18/100 | 0.180 | [0.117, 0.267] |

## McNemar (exact binomial two-sided), fresh-100 pairs

On judge-evaluated equivalence:

- **D vs E**: both=18, D-only=1, E-only=35, neither=46 -> p = **1.08e-09**
- **D vs C**: both=16, D-only=3, C-only=35, neither=46 -> p = **6.68e-08**
- **D vs A**: both=12, D-only=7, A-only=5, neither=76 -> p = **0.774**
- **E vs A**: both=16, E-only=37, A-only=1, neither=46 -> p = **2.84e-10**
- **E vs B**: both=14, E-only=39, B-only=2, neither=45 -> p = **7.84e-10**
- **E vs C**: both=40, E-only=13, C-only=11, neither=36 -> p = **0.839**

On the conjunction (typecheck AND evaluated):

- **D vs E**: both=9, D-only=6, E-only=9, neither=76 -> p = **0.607**
- **D vs C**: both=7, D-only=8, C-only=9, neither=76 -> p = **1**
- **D vs A**: both=6, D-only=9, A-only=2, neither=83 -> p = **0.0654**
- **E vs A**: both=7, E-only=11, A-only=1, neither=81 -> p = **0.00635**
- **E vs B**: both=6, E-only=12, B-only=3, neither=79 -> p = **0.0352**
- **E vs C**: both=12, E-only=6, C-only=4, neither=78 -> p = **0.754**

## Continuity — completed-69 subset (tasks arm E finished in the original campaign)

Evaluated equivalence:

| arm | k/n | rate | Wilson 95% CI |
|---|---|---|---|
| A | 10/69 | 0.145 | [0.081, 0.247] |
| B | 9/69 | 0.130 | [0.070, 0.230] |
| C | 37/69 | 0.536 | [0.420, 0.649] |
| D | 11/69 | 0.159 | [0.091, 0.263] |
| E | 37/69 | 0.536 | [0.420, 0.649] |

Conjunction:

| arm | k/n | rate | Wilson 95% CI |
|---|---|---|---|
| A | 3/69 | 0.043 | [0.015, 0.120] |
| B | 3/69 | 0.043 | [0.015, 0.120] |
| C | 8/69 | 0.116 | [0.060, 0.212] |
| D | 10/69 | 0.145 | [0.081, 0.247] |
| E | 9/69 | 0.130 | [0.070, 0.230] |

McNemar on judge-evaluated (69 pairs):

- **D vs E**: both=11, D-only=0, E-only=26, neither=32 -> p = **2.98e-08**
- **D vs C**: both=9, D-only=2, C-only=28, neither=30 -> p = **8.68e-07**
- **D vs A**: both=7, D-only=4, A-only=3, neither=55 -> p = **1**
- **E vs A**: both=9, E-only=28, A-only=1, neither=31 -> p = **1.12e-07**
- **E vs B**: both=7, E-only=30, B-only=2, neither=30 -> p = **2.46e-07**
- **E vs C**: both=30, E-only=7, C-only=7, neither=25 -> p = **1**

McNemar on the conjunction (69 pairs):

- **D vs E**: both=6, D-only=4, E-only=3, neither=56 -> p = **1**
- **D vs C**: both=3, D-only=7, C-only=5, neither=54 -> p = **0.774**
- **D vs A**: both=3, D-only=7, A-only=0, neither=59 -> p = **0.0156**
- **E vs A**: both=2, E-only=7, A-only=1, neither=59 -> p = **0.0703**
- **E vs B**: both=1, E-only=8, B-only=2, neither=58 -> p = **0.109**
- **E vs C**: both=6, E-only=3, C-only=2, neither=58 -> p = **1**

## Self-consistency (fixed 50-item re-grade, seed 20260727, 10 per arm)

- strict agreement: **98.00%** (1 flips: A/fresh_015)
- evaluated agreement: **100.00%** (0 flips: none)

## Human grading queue — D/E discordant on judge-evaluated equivalence (36 tasks)

Exactly one of arms D/E judged equivalent; these drive the D-vs-E McNemar, so hand-grade them first:

fresh_002, fresh_004, fresh_006, fresh_007, fresh_011, fresh_012, fresh_013, fresh_014, fresh_019, fresh_021, fresh_023, fresh_026, fresh_027, fresh_032, fresh_044, fresh_048, fresh_049, fresh_050, fresh_051, fresh_056, fresh_057, fresh_059, fresh_062, fresh_063, fresh_064, fresh_067, fresh_069, fresh_076, fresh_077, fresh_085, fresh_086, fresh_092, fresh_094, fresh_095, fresh_096, fresh_099

## Exposure addendum — retrieval vs formalization caveat

Arms C/E carry Mathlib source-grep tools and ~half the fresh golds exist in their tool tree (fresh_exposure.md), so their judge-equivalence can reflect retrieval of the gold statement. Evaluated-equivalence split (exposed n=51 / unexposed n=49, own-module basis):

| arm | exposed | unexposed |
|---|---|---|
| A | 8/51 (0.157) | 9/49 (0.184) |
| B | 9/51 (0.176) | 7/49 (0.143) |
| C | 36/51 (0.706) | 15/49 (0.306) |
| D | 10/51 (0.196) | 9/49 (0.184) |
| E | 35/51 (0.686) | 18/49 (0.367) |
- D vs E on evaluated, exposed: both=9, D-only=1, E-only=26, neither=15 -> p = 4.17e-07
- D vs E on evaluated, unexposed: both=9, D-only=0, E-only=9, neither=31 -> p = 0.00391
