# The preregistered 2x2 factorial — analysis

Prereg `docs/research/BRIDGE-FACTORIAL.md` @ 3658bd58; seed 20260803, B=10,000, 44 commit clusters; machinery `cluster_boot_rd` (verbatim).
Primary outcome: grounded typecheck, repaired oracle.

## Four-arm table (primary outcome)

| arm | cell | k/n | rate | Wilson 95% |
|---|---|---|---|---|
| Ep | join-, ver- | 31/100 | 0.310 | [0.228, 0.406] |
| X | join-, ver+ | 41/100 | 0.410 | [0.319, 0.508] |
| J | join+, ver- | 39/100 | 0.390 | [0.300, 0.488] |
| Dp | join+, ver+ | 39/100 | 0.390 | [0.300, 0.488] |

## Preregistered effects (commit-clustered paired bootstrap)

- **JOIN main effect (confirmatory)**: +0.030 [-0.093, +0.139] p=0.6453 -> NULL at this sample size (p >= 0.05); the CI is the precision statement
- **VERIFIER main effect (confirmatory)**: +0.050 [-0.029, +0.121] p=0.2304 -> NULL at this sample size (p >= 0.05); the CI is the precision statement
- **Interaction (exploratory)**: -0.100 [-0.245, +0.047] p=0.1964 -> exploratory: no detectable interaction at this sample size

## Six pairwise contrasts (supporting descriptives, exploratory)

- Dp - Ep: +0.080 [-0.071, +0.207] p=0.3096
- Dp - X: -0.020 [-0.172, +0.120] p=0.8399
- Dp - J: +0.000 [-0.099, +0.080] p=1.0000
- X - Ep: +0.100 [-0.022, +0.215] p=0.1274
- J - Ep: +0.080 [-0.053, +0.202] p=0.2588
- J - X: -0.020 [-0.155, +0.116] p=0.8211

## Raw-oracle sensitivity (supplement)

Rates: Ep 26/100, X 31/100, J 36/100, Dp 39/100.
- join: +0.090 [-0.006, +0.183] p=0.0764
- verifier: +0.040 [-0.030, +0.103] p=0.2984
- interaction: -0.020 [-0.156, +0.134] p=0.8323

## Secondary: run-level repaired hallucination (lower better)

Rates: Ep 9/100, X 5/100, J 11/100, Dp 10/100.
- join: +0.035 [-0.026, +0.100] p=0.2892
- verifier: -0.025 [-0.081, +0.025] p=0.3812
- interaction: +0.030 [-0.082, +0.142] p=0.6663

## Secondary: judge evaluated-equivalence

Rates: Ep 46/100, X 42/100, J 20/100, Dp 15/100.
- join: -0.265 [-0.368, -0.154] p=0.0002
- verifier: -0.045 [-0.117, +0.029] p=0.2636
- interaction: -0.010 [-0.152, +0.117] p=0.9145

## Secondary: conjunction (grounded typecheck AND evaluated)

Rates: Ep 20/100, X 25/100, J 17/100, Dp 14/100.
- join: -0.070 [-0.161, +0.021] p=0.1408
- verifier: +0.010 [-0.053, +0.065] p=0.8221
- interaction: -0.080 [-0.176, +0.011] p=0.1078

## Manipulation checks

- decl_exists usage: X 213 calls / 94 runs; Dp 536 calls / 92 runs.
- informal-tool touches: Ep 2/100, X 4/100 runs.

## Sensitivity cuts (primary outcome, exploratory)

- **exposed 51** — rates Ep 17/51, X 25/51, J 20/51, Dp 18/51; join -0.039 [-0.205, +0.108] p=0.6525; verifier +0.059 [-0.060, +0.170] p=0.3914.
- **unexposed 49** — rates Ep 14/49, X 16/49, J 19/49, Dp 21/49; join +0.102 [-0.076, +0.243] p=0.2710; verifier +0.041 [-0.054, +0.130] p=0.4376.
- **drop 3 leak tasks** — rates Ep 31/97, X 39/97, J 38/97, Dp 38/97; join +0.031 [-0.093, +0.142] p=0.6321; verifier +0.041 [-0.039, +0.114] p=0.3526.
- **determinacy subset (both-annotator det2, n=74)** — rates Ep 25/74, X 35/74, J 30/74, Dp 30/74; join +0.000 [-0.156, +0.139] p=1.0000; verifier +0.068 [-0.013, +0.151] p=0.1104.
