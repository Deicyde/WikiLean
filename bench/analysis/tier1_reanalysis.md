# Bridge Experiment Tier-1 — corrective reanalysis (report v2)

Reproduce: `python3 bench/analysis/tier1_reanalysis.py` (outcomes from `bridge_summary.json` paired_matrix; metric: produced ∧ no-halluc ∧ TYPECHECK (judge pending calibration)).

## Data provenance

- Snapshot of all 500 fresh rows analyzed from `/Users/jack/Desktop/LEAN/WikiLean/bench/analysis/snapshot_fresh_orig` (taken before the concurrent arm-E repair job could rewrite rows).
- The 31 E rows were cross-checked byte-for-byte against the 429 archive: 0 differed — snapshot ≡ archive.
- Consistency guard: success_proxy recomputed for all 500 fresh rows with score_bridge.py's Oracle/extract_cited; 0 matrix contradictions.
- Typecheck is recomputed at scoring time by score_bridge.py (not stored per-row), so per-task success outcomes are reused from the typecheck-folded paired_matrix rather than re-typechecked.

## 1. The 31 arm-E 429-error rows

Arm E fresh rows errored: **31** (fresh_069…fresh_099, contiguous), all with session-limit **429** errors (`all_are_429=True`). Fresh error counts by arm: A=0, B=0, C=0, D=0, E=31 — A–D verified zero.

## 2. Fresh-set grounded-typecheck rate — full 100 (E errors count as failures)

| arm | k/n | rate | Wilson 95% CI | errors |
|---|---|---|---|---|
| A | 20/100 | 0.200 | [0.133, 0.289] | 0 |
| B | 22/100 | 0.220 | [0.150, 0.311] | 0 |
| C | 25/100 | 0.250 | [0.175, 0.343] | 0 |
| D | 42/100 | 0.420 | [0.328, 0.518] | 0 |
| E | 16/100 | 0.160 | [0.101, 0.244] | 31 |

McNemar (exact binomial, two-sided), all 100 pairs:

- **D vs E** (n=100): both=10, D-only=32, E-only=6, neither=52 → exact two-sided p = **0.0000**
- **D vs C** (n=100): both=14, D-only=28, C-only=11, neither=47 → exact two-sided p = **0.0095**
- **D vs A** (n=100): both=13, D-only=29, A-only=7, neither=51 → exact two-sided p = **0.0003**

## 3. Completed-pairs analysis — the 69 tasks arm E completed

| arm | k/n | rate | Wilson 95% CI |
|---|---|---|---|
| A | 10/69 | 0.145 | [0.081, 0.247] |
| B | 12/69 | 0.174 | [0.102, 0.280] |
| C | 12/69 | 0.174 | [0.102, 0.280] |
| D | 28/69 | 0.406 | [0.298, 0.524] |
| E | 16/69 | 0.232 | [0.148, 0.344] |

McNemar (exact binomial, two-sided) on the 69 completed pairs:

- **D vs E** (n=69): both=10, D-only=18, E-only=6, neither=35 → exact two-sided p = **0.0227**
- **D vs C** (n=69): both=7, D-only=21, C-only=5, neither=36 → exact two-sided p = **0.0025**
- **D vs A** (n=69): both=6, D-only=22, A-only=4, neither=37 → exact two-sided p = **0.0005**

## 4. Eval-341 (Tier-1a) grounded-typecheck rate

| arm | k/n | rate | Wilson 95% CI |
|---|---|---|---|
| A | 204/341 | 0.598 | [0.545, 0.649] |
| B | 198/341 | 0.581 | [0.528, 0.632] |
| C | 218/341 | 0.639 | [0.587, 0.689] |
| D | 219/341 | 0.642 | [0.590, 0.691] |
| E | 208/341 | 0.610 | [0.557, 0.660] |

## 5. Turn-budget sensitivity (turns ≤ 30 in BOTH arms of the pair)

- **D_vs_E** — n=45 pairs; D: 21/45 (0.467 [0.329, 0.609]), E: 11/45 (0.244 [0.142, 0.387]); McNemar 16 discordant → p = 0.0213
- **D_vs_E_completed_only** — n=27 pairs; D: 12/27 (0.444 [0.276, 0.627]), E: 11/27 (0.407 [0.245, 0.593]); McNemar 7 discordant → p = 1.0000
- **D_vs_C** — n=35 pairs; D: 19/35 (0.543 [0.382, 0.695]), C: 9/35 (0.257 [0.142, 0.421]); McNemar 12 discordant → p = 0.0063
- **D_vs_A** — n=62 pairs; D: 29/62 (0.468 [0.349, 0.590]), A: 16/62 (0.258 [0.166, 0.379]); McNemar 25 discordant → p = 0.0146

Per-arm rates on each arm's own turns ≤ 30 runs:

| arm | within-budget n | k | rate | Wilson 95% CI |
|---|---|---|---|---|
| A | 100 | 20 | 0.200 | [0.133, 0.289] |
| B | 100 | 22 | 0.220 | [0.150, 0.311] |
| C | 50 | 14 | 0.280 | [0.175, 0.417] |
| D | 62 | 29 | 0.468 | [0.349, 0.590] |
| E | 68 | 12 | 0.176 | [0.104, 0.284] |

## 6. Effect sizes — D vs E absolute risk difference

- **fresh_100_errors_as_failures** (n=100): D 0.420 vs E 0.160 → RD = **+0.260** 95% CI [+0.150, +0.369] (paired Wald (matched pairs))
- **completed_69** (n=69): D 0.406 vs E 0.232 → RD = **+0.174** 95% CI [+0.041, +0.307] (paired Wald (matched pairs))

