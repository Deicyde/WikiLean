# Bridge v2 — the race-repaired agent grid

194 original rows (F: 175 qr810 + 15 mpr; W: 2 + 2) ran with no tools attached (MCP cold-start race, de-facto arm N); originals preserved in `bench/v2/runs/agent/race_condemned_archive/`, rows rerun with the fixed harness. N/U/WF audit race-free before and after. Reproduce: `python3 bench/analysis/grid_repaired.py` (seed 20260727, B=10,000).

## Arm tables, before → after

| arm | qr810 R@10 | qr810 nDCG@10 | mpr gR@10 |
|---|---|---|---|
| N | 0.6333 | 0.5975 | 0.2025 |
| F | 0.8309 → **0.8457** | 0.7901 → **0.8089** | 0.4532 → **0.5468** |
| W | 0.8160 | 0.7806 | 0.2721 |
| U | 0.8296 | 0.7992 | 0.5485 |
| WF | 0.8852 | 0.8394 | 0.5569 |

## Final contrasts (repaired grid)

| contrast | metric | diff | 95% CI | excl. 0 | Wilcoxon p | sign (+/-) | sign p |
|---|---|---|---|---|---|---|---|
| WF − F | qr R@10 | +0.0395 | [+0.0148, +0.0636] | yes | 9.68e-03 | 38/14 | 1.20e-03 |
| WF − F | qr nDCG@10 | +0.0305 | [+0.0099, +0.0515] | yes | 6.37e-03 | 53/27 | 4.87e-03 |
| WF − F | mpr gR@10 | +0.0101 | [-0.0679, +0.0882] | no | 7.33e-01 | 12/10 | 8.32e-01 |
| WF − U | qr R@10 | +0.0556 | [+0.0320, +0.0802] | yes | 1.26e-04 | 38/10 | 6.17e-05 |
| WF − U | qr nDCG@10 | +0.0402 | [+0.0187, +0.0628] | yes | 1.23e-03 | 48/29 | 3.95e-02 |
| WF − U | mpr gR@10 | +0.0085 | [-0.0664, +0.0833] | no | 8.22e-01 | 11/9 | 8.24e-01 |
| U − F | qr R@10 | -0.0161 | [-0.0385, +0.0050] | no | 1.33e-01 | 20/24 | 6.52e-01 |
| U − F | qr nDCG@10 | -0.0097 | [-0.0294, +0.0095] | no | 4.97e-01 | 33/37 | 7.20e-01 |
| U − F | mpr gR@10 | +0.0017 | [-0.0725, +0.0713] | no | 8.34e-01 | 11/10 | 1.00e+00 |
| F − W | qr R@10 | +0.0297 | [-0.0150, +0.0756] | no | 8.55e-02 | 34/35 | 1.00e+00 |
| F − W | qr nDCG@10 | +0.0283 | [-0.0110, +0.0684] | no | 2.78e-01 | 45/46 | 1.00e+00 |
| F − W | mpr gR@10 | +0.2747 | [+0.1851, +0.3688] | yes | 2.31e-06 | 36/5 | 7.84e-07 |

