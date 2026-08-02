# Bridge v2 — bare-union (U) ablation

U = the identical W ∪ F union toolset as WF, **no manual**. WF − U isolates the (test-set-tuned) manual's marginal effect; U − F / U − W isolate the union's effect without the manual. Reproduce: `python3 bench/analysis/union_ablation.py` (seed 20260727, B=10,000); stats identical to `retrieval_clustered.py` (imported), scoring identical to `bench/v2/score_retrieval.py`.

## MathlibQR fair-810 — declaration-clustered

| arm | R@10 (row) | R@10 95% CI | nDCG@10 (row) | nDCG@10 95% CI |
|---|---|---|---|---|
| N | 0.6333 | [0.5813, 0.6839] | 0.5975 | [0.5469, 0.6472] |
| F | 0.8457 | [0.8084, 0.8803] | 0.8089 | [0.7709, 0.8451] |
| W | 0.8160 | [0.7666, 0.8622] | 0.7806 | [0.7314, 0.8269] |
| U | 0.8296 | [0.7893, 0.8675] | 0.7992 | [0.7578, 0.8380] |
| WF | 0.8852 | [0.8490, 0.9185] | 0.8394 | [0.8013, 0.8743] |

### Per-style nDCG@10 (R@10 in parens)

| arm | q1a lean | q1b latex | q1c natural | q2 slogan | q3 nickname | q4 special case |
|---|---|---|---|---|---|---|
| N | 0.700 (0.729) | 0.643 (0.684) | 0.691 (0.729) | 0.496 (0.530) | 0.443 (0.472) | 0.282 (0.348) |
| F | 0.888 (0.906) | 0.782 (0.819) | 0.849 (0.888) | 0.803 (0.845) | 0.770 (0.815) | 0.356 (0.435) |
| W | 0.817 (0.841) | 0.765 (0.807) | 0.824 (0.841) | 0.753 (0.792) | 0.779 (0.833) | 0.522 (0.609) |
| U | 0.878 (0.888) | 0.778 (0.813) | 0.856 (0.888) | 0.765 (0.798) | 0.749 (0.787) | 0.436 (0.522) |
| WF | 0.888 (0.912) | 0.857 (0.901) | 0.856 (0.894) | 0.816 (0.869) | 0.808 (0.870) | 0.551 (0.696) |

### Decisive paired contrasts (declaration-resampled, paired)

| contrast | metric | diff | 95% CI | excl. 0 | Wilcoxon p | sign (+/-) | sign p |
|---|---|---|---|---|---|---|---|
| WF − U | R@10 | +0.0556 | [+0.0320, +0.0802] | yes | 1.26e-04 | 38/10 | 6.17e-05 |
| U − F | R@10 | -0.0161 | [-0.0385, +0.0050] | no | 1.33e-01 | 20/24 | 6.52e-01 |
| U − W | R@10 | +0.0136 | [-0.0257, +0.0543] | no | 2.80e-01 | 29/31 | 8.97e-01 |
| WF − U | nDCG@10 | +0.0402 | [+0.0187, +0.0628] | yes | 1.23e-03 | 48/29 | 3.95e-02 |
| U − F | nDCG@10 | -0.0097 | [-0.0294, +0.0095] | no | 4.97e-01 | 33/37 | 7.20e-01 |
| U − W | nDCG@10 | +0.0186 | [-0.0166, +0.0550] | no | 4.75e-01 | 46/39 | 5.15e-01 |

## MathlibMPR — 69 PR tasks

| arm | gR@10 (per-task mean) | boot 95% CI |
|---|---|---|
| N | 0.2025 | [0.1309, 0.2824] |
| F | 0.5468 | [0.4632, 0.6318] |
| W | 0.2721 | [0.1964, 0.3536] |
| U | 0.5485 | [0.4642, 0.6326] |
| WF | 0.5569 | [0.4720, 0.6415] |

| contrast | mean diff | boot 95% CI | excl. 0 | sign (+/-) | sign p | Wilcoxon p |
|---|---|---|---|---|---|---|
| WF − U | +0.0085 | [-0.0664, +0.0833] | no | 11/9 | 8.24e-01 | 8.22e-01 |
| U − F | +0.0017 | [-0.0725, +0.0713] | no | 11/10 | 1.00e+00 | 8.34e-01 |
| U − W | +0.2764 | [+0.1935, +0.3608] | yes | 38/3 | 1.05e-08 | 3.28e-07 |

## Race-row sensitivity (contrasts on attach-clean rows)

contrasts restricted to rows where the comparison arm had >=1 mcp tool call (race rows dropped). The 194 cold-start-race rows that originally deflated F/W (F: 175 qr + 15 mpr; W: 2 + 2) have been archived and rerun (see `grid_repaired.py`); this section is a residual guard and should nearly coincide with the primary contrasts above.

| contrast | bench | kept | hi | other | diff | 95% CI | sign p | Wilcoxon p |
|---|---|---|---|---|---|---|---|---|
| U − F | qr810 R@10 | 810 rows (−0) | 0.8296 | 0.8457 | -0.0160 | [-0.0388, +0.0050] | 6.52e-01 | 1.33e-01 |
| U − F | mpr gR@10 | 69 tasks (−0) | 0.5485 | 0.5468 | +0.0017 | [-0.0732, +0.0729] | 1.00e+00 | 8.34e-01 |
| U − W | qr810 R@10 | 810 rows (−0) | 0.8296 | 0.8160 | +0.0136 | [-0.0257, +0.0533] | 8.97e-01 | 2.80e-01 |
| U − W | mpr gR@10 | 69 tasks (−0) | 0.5485 | 0.2721 | +0.2764 | [+0.1949, +0.3616] | 1.05e-08 | 3.28e-07 |
| WF − F | qr810 R@10 | 810 rows (−0) | 0.8852 | 0.8457 | +0.0395 | [+0.0148, +0.0636] | 1.20e-03 | 9.68e-03 |
| WF − F | mpr gR@10 | 69 tasks (−0) | 0.5569 | 0.5468 | +0.0101 | [-0.0672, +0.0874] | 8.32e-01 | 7.33e-01 |
| WF − W | qr810 R@10 | 810 rows (−0) | 0.8852 | 0.8160 | +0.0691 | [+0.0312, +0.1082] | 4.53e-03 | 3.09e-04 |
| WF − W | mpr gR@10 | 69 tasks (−0) | 0.5569 | 0.2721 | +0.2848 | [+0.1842, +0.3868] | 6.24e-07 | 5.78e-06 |

## U run audit

- **qr810**: 810/810 rows, 0 errors, cost $159.37, mean turns 4.1457, mean tool calls 3.1457
- **mpr**: 69/69 rows, 0 errors, cost $27.84, mean turns 8.913, mean tool calls 7.913
- **total U cost**: $187.20

