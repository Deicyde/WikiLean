# Held-out blinded validation of the repaired hallucination classifier

The 5-rule repaired classifier (`halluc_validation.classify_adjusted`, R1–R5) was
**derived from** the seed-20260801 blinded sample of 60 names, and its reported
59/60 agreement was measured on that same sample — an in-sample fit. This file
reports the held-out answer on a fresh, disjoint, seeded sample.

Reproducible from `bench/analysis/halluc_holdout.py` (seed **20260802** fixed);
machine-readable results in `bench/analysis/halluc_holdout.json`; blinded-protocol
intermediates in `bench/analysis/holdout_blind/`.

```
python3 bench/analysis/halluc_holdout.py sample     # seeded held-out sample (40, excludes the 60)
python3 bench/analysis/halluc_holdout.py evidence   # oracle-free pinned-tree evidence
#   <agent grades holdout_blind/truth_labels.json BLIND from the blinded files only>
python3 bench/analysis/halluc_holdout.py compare    # unblind + score BOTH instruments
```

## Protocol

Stratified sample of **40 distinct** cited names (per arm: 4 raw-hallucinated,
3 raw-exists, 1 raw-renamed; shortfalls → exists) from the post-repair fresh rows
`bench/data/runs/{A..E}/fresh_*.json`, **excluding every name audited in-sample**
(the 53 distinct names of `halluc_blind/blinded_sample.json`). The grader saw only
`holdout_blind/blinded_sample.json` (name + context line, shuffled, no verdicts),
`holdout_blind/evidence.json` (raw `git grep` at mathlib4 @ `61a5e4f338`,
cross-check `9944fe2973` — same `evidence_for` as the original), the run outputs
(`output_lean`, needed to distinguish self-declarations / local variables), and
targeted pinned-tree greps + doc-gen4-index cross-references for fully-qualified
names; wrote `truth_labels.json` for all 40; and only then ran `compare`, which
unseals `holdout_blind/sample_key.json` (raw **and** repaired verdicts, computed
at sample time).

**Truth labels** (same vocabulary as the in-sample audit): real 31 · nonexistent 3
· not_a_citation 5 · renamed 1.

The three confirmed hallucinations, all verified zero-hit at both mathlib revs:
`Subgroup.torsion` (B; real name `CommGroup.torsion`), `Localization.map`
(A; real names `IsLocalization.map` / `Submonoid.LocalizationMap.map`),
`Module.GeneralizedEigenspace` (B; current API `Module.End.genEigenspace`).
As in-sample, **all confirmed fabrications sit in no-Brain arms (A/B); none in D/C/E.**

One borderline label: `OrderedSemiring` (E) — a formerly-real Mathlib class removed
by the ordered-algebra refactor (no class/alias at either rev; replacement =
`Semiring + PartialOrder + IsOrderedRing`). Labeled **renamed** (Basis→Module.Basis
precedent); the nonexistent-reading is reported as a sensitivity below.

## Results on the held-out 40

Binary scoring: positive = flagged `hallucinated`; truth-positive = `nonexistent`;
strict counts extractor noise flagged positive as FP.

| metric (strict, n=40) | raw oracle | repaired classifier |
|---|---|---|
| hallucinated precision | **3/20 = 15.0%** [5.2, 36.0] | **3/7 = 42.9%** [15.8, 75.0] |
| hallucinated recall | 3/3 = 100% | 3/3 = 100% |
| accuracy | 57.5% | 90.0% |
| lenient precision (noise rows excluded) | 3/15 = 20.0% | 3/6 = 50.0% |
| collapsed 3-class agreement (real\|halluc\|noise) | 23/40 = 57.5% | 33/40 = 82.5% [68.1, 91.3] |
| binary agreement (the in-sample 59/60 measure) | 23/40 | **36/40 = 90.0%** [76.9, 96.0] |

Sensitivity (`OrderedSemiring` read as nonexistent): raw precision 4/20 = 20.0%,
repaired precision **4/7 = 57.1%** [25.0, 84.2], repaired accuracy 92.5%.

## Does the repaired instrument replicate out-of-sample?

**Directionally yes; the point estimates degrade in the way in-sample fits do.**

1. **The raw oracle's invalidity replicates almost exactly**: precision 13.3% →
   15.0%, accuracy 56.7% → 57.5% (in-sample → held-out). The conclusion that the
   raw oracle is not a valid absolute-hallucination instrument is confirmed on
   independent data.
2. **The repair transfers most of its value**: FPs on the sampled flags drop
   17 → 4 (strict), accuracy 57.5% → 90.0%, and recall stays 3/3. The repaired
   instrument remains dramatically better than the raw oracle out-of-sample.
3. **But the in-sample fit was optimistic**: binary agreement 59/60 = 98.3%
   [91.1, 99.7] in-sample vs **36/40 = 90.0%** [76.9, 96.0] held-out (Fisher
   two-sided p = 0.15 — consistent with, but not proof of, ordinary in-sample
   optimism at these n). Precision on flagged citations falls from the in-sample
   4/5 = 80% to **3/7 = 42.9%** (4/7 = 57.1% under the OrderedSemiring
   sensitivity). A repaired flag is roughly a coin toss, not a confirmation.
4. **The residual errors are three FP modes the 5 rules never saw** (all four
   strict FPs; zero FNs):
   | mode | held-out case | why R1–R5 miss it |
   |---|---|---|
   | multi-segment namespace short name | `IsZero` ← `CategoryTheory.Limits.IsZero` (E) | R5 strips only single-segment prefixes |
   | dot-notation on a namespaced def value | `MeasureTheory.volume.restrict` → `Measure.restrict` (B) | R4 catches only single-letter heads |
   | multi-char local variable | `M001` (C) | extractor noise; no rule targets it |
   | removed-class citation (borderline) | `OrderedSemiring` (E) | genuinely absent at the pin; renamed vs nonexistent is a truth-label judgment |
5. **The collapsed 3-class number (33/40) is stricter than the in-sample measure**:
   3 of its 7 disagreements are R4 dropping dot-notation-on-variable tokens
   (`M.rank`, `I.inertiaDeg`, `I.IsMaximal`) that the truth convention keeps as
   `real` because the underlying declaration exists — a labeling-convention
   mismatch with **zero** effect on binary hallucination scoring (all TN both ways).
6. **Implication for the per-arm adjusted rates** (halluc_validation.md §Sensitivity):
   with held-out precision ≈ 43–57% and recall ≈ 100%, the adjusted per-arm
   citation-hallucination rates likely **overstate true rates by roughly 2×**,
   and two of the four residual FP modes are namespace-style-correlated (both
   held-out instances land in C/E-style short-name arms), i.e. the residual bias
   still runs **against** the free-text tool arms and cannot manufacture D's
   advantage. The qualitative conclusions — tool arms crush no-tools baselines;
   D-vs-E/C is directionally positive but unresolved at n=100 — are unchanged;
   the adjusted rates should be quoted as upper bounds on true hallucination.

## Exact error cases (strict, both instruments)

Raw-oracle FPs (17): the 4 repaired-classifier FPs below, plus 13 the repair
fixes — namespace short names `Perm`, `Mono` (×2), `IsSemisimple`,
`IsBoundedAtImInfty`, `InfinitePlace`; self-declarations
`Rep.resFunctor.map_shortComplex_exact`, `MvPowerSeries.ext_of_const_and_partialDeriv`,
`LSeries_eq_tsum`; dot-notation variables `M.rank`, `I.inertiaDeg`, `I.IsMaximal`;
import line `Mathlib.LinearAlgebra.Eigenspace.Basic`.

Repaired-classifier FPs (4): `M001` (C, fresh_028 — local variable),
`IsZero` (E, fresh_078 — real under `open CategoryTheory.Limits`),
`MeasureTheory.volume.restrict` (B, fresh_019 — real API via dot-notation),
`OrderedSemiring` (E, fresh_005 — removed class, truth `renamed`).
False negatives: **none** (either instrument).
