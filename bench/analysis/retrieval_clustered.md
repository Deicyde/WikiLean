# Bridge v2 — cluster-aware retrieval reanalysis

Reproduce: `python3 bench/analysis/retrieval_clustered.py` (seed 20260727, B=10,000 bootstrap resamples). Scoring reuses `bench/v2/score_retrieval.py` verbatim (exact full-name match).

## 1-2. MathlibQR fair-810 — declaration-clustered

810 rows are **171 declaration clusters** (2-6 query styles each); all inference below is at the declaration level. Row-level points match the headline scorer.

| arm | R@10 (row) | R@10 95% CI (cluster boot) | nDCG@10 (row) | nDCG@10 95% CI |
|---|---|---|---|---|
| N | 0.6333 | [0.5813, 0.6839] | 0.5975 | [0.5469, 0.6472] |
| F | 0.8309 | [0.7918, 0.8680] | 0.7901 | [0.7503, 0.8285] |
| W | 0.8160 | [0.7666, 0.8622] | 0.7806 | [0.7314, 0.8269] |
| WF | 0.8852 | [0.8490, 0.9185] | 0.8394 | [0.8013, 0.8743] |

### Pairwise differences (declaration-resampled, paired)

| contrast | metric | diff | 95% CI | Wilcoxon p | sign test (+/-) | sign p |
|---|---|---|---|---|---|---|
| F − W | R@10 | +0.0149 | [-0.0283, +0.0590] | 2.56e-01 | 33/36 | 8.10e-01 |
| WF − F | R@10 | +0.0543 | [+0.0265, +0.0818] | 1.71e-03 | 40/12 | 1.28e-04 |
| WF − W | R@10 | +0.0692 | [+0.0321, +0.1075] | 3.09e-04 | 33/13 | 4.53e-03 |
| F − W | nDCG@10 | +0.0095 | [-0.0288, +0.0483] | 8.34e-01 | 45/51 | 6.10e-01 |
| WF − F | nDCG@10 | +0.0493 | [+0.0255, +0.0735] | 1.10e-04 | 61/24 | 7.40e-05 |
| WF − W | nDCG@10 | +0.0588 | [+0.0256, +0.0933] | 1.37e-03 | 45/27 | 4.44e-02 |

## 3. MathlibMPR — 69 PR tasks (no clustering)

| arm | gR@10 (per-task mean) | boot 95% CI | pooled groups | Wilson 95% CI (pooled) |
|---|---|---|---|---|
| N | 0.2025 | [0.1309, 0.2824] | 31/204 | [0.1092, 0.2076] |
| F | 0.4532 | [0.3652, 0.5404] | 82/204 | [0.3371, 0.4705] |
| W | 0.2721 | [0.1964, 0.3536] | 48/204 | [0.1823, 0.2981] |
| WF | 0.5569 | [0.4720, 0.6415] | 103/204 | [0.4368, 0.5728] |

Wilson intervals apply to the pooled group-level proportion (groups within a PR treated as independent); the per-task-mean metric (the scorer's) carries the task-bootstrap CI.

| contrast | mean diff | boot 95% CI | sign (+/-) | sign p | Wilcoxon p |
|---|---|---|---|---|---|
| WF − F | +0.1037 | [+0.0072, +0.2011] | 22/9 | 2.94e-02 | 4.92e-02 |
| WF − W | +0.2848 | [+0.1822, +0.3855] | 41/7 | 6.24e-07 | 5.78e-06 |

## 4. SorryDB — repo-clustered uncertainty

Intention-to-treat over the 171 frozen tasks in 10 repos; a task counts as proved only with a verified `proved` verdict.

| repo | n | N | F | WF |
|---|---|---|---|---|
| AlexKontorovich/PrimeNumberTheoremAnd | 21 | 0 | 0 | 0 |
| Beneficial-AI-Foundation/curve25519-dalek-lean-verify | 21 | 0 | 0 | 0 |
| FormalizedFormalLogic/Foundation | 1 | 0 | 0 | 0 |
| FredRaj3/SemicircleLaw | 7 | 0 | 0 | 0 |
| Paul-Lez/PersistentDecomp | 20 | 1 | 3 | 3 |
| RemyDegenne/brownian-motion | 20 | 0 | 0 | 0 |
| VCA-EPFL/graphiti | 20 | 0 | 0 | 0 |
| YaelDillies/LeanAPAP | 20 | 0 | 4 | 5 |
| YaelDillies/MiscYD | 20 | 1 | 2 | 2 |
| rkirov/category-theory-in-context-lean | 21 | 0 | 0 | 0 |
| **total** | **171** | **2** | **9** | **10** |

| arm | proved | rate | repo-boot 95% CI |
|---|---|---|---|
| N | 2/171 | 0.0117 | [0.0000, 0.0272] |
| F | 9/171 | 0.0526 | [0.0000, 0.1065] |
| WF | 10/171 | 0.0585 | [0.0000, 0.1223] |

**WF − F**: +0.0058, repo-clustered bootstrap 95% CI [+0.0000, +0.0185] — includes zero (P(diff ≤ 0) = 0.344). Task-level exact McNemar (non-clustered reference): 4 WF-only vs 3 F-only, p = 1.000.

**Plain statement:** WF vs F on SorryDB is **not statistically distinguishable** at this sample size. The point difference is a single extra proof (10 vs 9 of 171); the repo-clustered bootstrap puts substantial mass at zero (the lower CI endpoint is exactly 0 because WF ≥ F in every repo, and 34% of resamples show no difference), and the task-level exact McNemar is p = 1.00. Proofs are concentrated in 3 of 10 repos; even the N-vs-{F,WF} gap should be described cautiously with only 10 repo clusters.

