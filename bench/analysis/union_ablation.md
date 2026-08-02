# Bridge v2 — bare-union (U) ablation

U = the identical W ∪ F union toolset as WF, **no manual**. WF − U isolates the (test-set-tuned) manual's marginal effect; U − F / U − W isolate the union's effect without the manual. Reproduce: `python3 bench/analysis/union_ablation.py` (seed 20260727, B=10,000); stats identical to `retrieval_clustered.py` (imported), scoring identical to `bench/v2/score_retrieval.py`.

## MathlibQR fair-810 — declaration-clustered

| arm | R@10 (row) | R@10 95% CI | nDCG@10 (row) | nDCG@10 95% CI |
|---|---|---|---|---|
| N | 0.6333 | [0.5813, 0.6839] | 0.5975 | [0.5469, 0.6472] |
| F | 0.8309 | [0.7918, 0.8680] | 0.7901 | [0.7503, 0.8285] |
| W | 0.8160 | [0.7666, 0.8622] | 0.7806 | [0.7314, 0.8269] |
| U | 0.8296 | [0.7893, 0.8675] | 0.7992 | [0.7578, 0.8380] |
| WF | 0.8852 | [0.8490, 0.9185] | 0.8394 | [0.8013, 0.8743] |

### Per-style nDCG@10 (R@10 in parens)

| arm | q1a lean | q1b latex | q1c natural | q2 slogan | q3 nickname | q4 special case |
|---|---|---|---|---|---|---|
| N | 0.700 (0.729) | 0.643 (0.684) | 0.691 (0.729) | 0.496 (0.530) | 0.443 (0.472) | 0.282 (0.348) |
| F | 0.876 (0.900) | 0.759 (0.795) | 0.824 (0.871) | 0.777 (0.827) | 0.757 (0.796) | 0.384 (0.478) |
| W | 0.817 (0.841) | 0.765 (0.807) | 0.824 (0.841) | 0.753 (0.792) | 0.779 (0.833) | 0.522 (0.609) |
| U | 0.878 (0.888) | 0.778 (0.813) | 0.856 (0.888) | 0.765 (0.798) | 0.749 (0.787) | 0.436 (0.522) |
| WF | 0.888 (0.912) | 0.857 (0.901) | 0.856 (0.894) | 0.816 (0.869) | 0.808 (0.870) | 0.551 (0.696) |

### Decisive paired contrasts (declaration-resampled, paired)

| contrast | metric | diff | 95% CI | excl. 0 | Wilcoxon p | sign (+/-) | sign p |
|---|---|---|---|---|---|---|---|
| WF − U | R@10 | +0.0556 | [+0.0320, +0.0802] | yes | 1.26e-04 | 38/10 | 6.17e-05 |
| U − F | R@10 | -0.0013 | [-0.0258, +0.0224] | no | 7.33e-01 | 25/21 | 6.59e-01 |
| U − W | R@10 | +0.0136 | [-0.0257, +0.0543] | no | 2.80e-01 | 29/31 | 8.97e-01 |
| WF − U | nDCG@10 | +0.0402 | [+0.0187, +0.0628] | yes | 1.23e-03 | 48/29 | 3.95e-02 |
| U − F | nDCG@10 | +0.0091 | [-0.0117, +0.0298] | no | 2.84e-01 | 43/34 | 3.62e-01 |
| U − W | nDCG@10 | +0.0186 | [-0.0166, +0.0550] | no | 4.75e-01 | 46/39 | 5.15e-01 |

## MathlibMPR — 69 PR tasks

| arm | gR@10 (per-task mean) | boot 95% CI |
|---|---|---|
| N | 0.2025 | [0.1309, 0.2824] |
| F | 0.4532 | [0.3652, 0.5404] |
| W | 0.2721 | [0.1964, 0.3536] |
| U | 0.5485 | [0.4642, 0.6326] |
| WF | 0.5569 | [0.4720, 0.6415] |

| contrast | mean diff | boot 95% CI | excl. 0 | sign (+/-) | sign p | Wilcoxon p |
|---|---|---|---|---|---|---|
| WF − U | +0.0085 | [-0.0664, +0.0833] | no | 11/9 | 8.24e-01 | 8.22e-01 |
| U − F | +0.0953 | [+0.0064, +0.1889] | yes | 20/8 | 3.57e-02 | 3.76e-02 |
| U − W | +0.2764 | [+0.1935, +0.3608] | yes | 38/3 | 1.05e-08 | 3.28e-07 |

## Race-row sensitivity (contrasts on attach-clean rows)

contrasts restricted to rows where the comparison arm had >=1 mcp tool call (race rows dropped). The original F/W grids predate the cold-start-race condemnation (F: 175/810 qr + 15/69 mpr de-facto-N rows; W: 2 + 2); U and WF are attach-clean, so primary contrasts against F/W are biased UP. These are the debiased versions (incl. the headline WF − F / WF − W).

| contrast | bench | kept | hi | other | diff | 95% CI | sign p | Wilcoxon p |
|---|---|---|---|---|---|---|---|---|
| U − F | qr810 R@10 | 635 rows (−175) | 0.8346 | 0.8488 | -0.0142 | [-0.0409, +0.0111] | 8.68e-01 | 7.09e-02 |
| U − F | mpr gR@10 | 54 tasks (−15) | 0.5373 | 0.5212 | +0.0160 | [-0.0741, +0.1009] | 6.48e-01 | 5.86e-01 |
| U − W | qr810 R@10 | 808 rows (−2) | 0.8292 | 0.8156 | +0.0136 | [-0.0256, +0.0532] | 8.97e-01 | 2.80e-01 |
| U − W | mpr gR@10 | 67 tasks (−2) | 0.5549 | 0.2802 | +0.2747 | [+0.1921, +0.3618] | 1.95e-08 | 5.12e-07 |
| WF − F | qr810 R@10 | 635 rows (−175) | 0.8850 | 0.8488 | +0.0362 | [+0.0067, +0.0645] | 6.61e-03 | 7.92e-02 |
| WF − F | mpr gR@10 | 54 tasks (−15) | 0.5326 | 0.5212 | +0.0114 | [-0.0849, +0.1049] | 6.48e-01 | 8.56e-01 |
| WF − W | qr810 R@10 | 808 rows (−2) | 0.8849 | 0.8156 | +0.0693 | [+0.0321, +0.1078] | 4.53e-03 | 3.09e-04 |
| WF − W | mpr gR@10 | 67 tasks (−2) | 0.5686 | 0.2802 | +0.2884 | [+0.1822, +0.3921] | 1.07e-06 | 7.97e-06 |

## U run audit

- **qr810**: 810/810 rows, 0 errors, cost $159.37, mean turns 4.1457, mean tool calls 3.1457
- **mpr**: 69/69 rows, 0 errors, cost $27.84, mean turns 8.913, mean tool calls 7.913
- **total U cost**: $187.20

