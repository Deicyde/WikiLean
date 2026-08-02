# Hallucination-oracle validation + run-level rates (REVIEW-2 §7b)

Responds to `docs/research/review/REVIEW-2.md` §7: *"Validate the regex/oracle on a
blinded sample and give a paired run-level comparison for 'any hallucination'
(the current 'roughly 3×' applies to citation-level rates, and citations cluster
within runs)."*

Everything below is reproducible from `bench/analysis/halluc_validation.py`
(seed 20260801 fixed); machine-readable results in
`bench/analysis/halluc_validation.json`; blinded-protocol intermediates in
`bench/analysis/halluc_blind/`.

```
python3 bench/analysis/halluc_validation.py table      # Part 2 (run-level, raw oracle)
python3 bench/analysis/halluc_validation.py sample     # Part 1 blinded sample (seeded)
python3 bench/analysis/halluc_validation.py evidence   # oracle-free pinned-tree evidence
#   <human/agent grades halluc_blind/truth_labels.json from the blinded files only>
python3 bench/analysis/halluc_validation.py compare    # unblind + precision/recall
python3 bench/analysis/halluc_validation.py adjusted   # sensitivity: repaired instrument
```

## Provenance

| item | source |
|---|---|
| runs | `bench/data/runs/{A..E}/fresh_*.json`, 100/arm, **post-repair** (31 arm-E rows rerun 2026-07-27; provenance chain `bench/analysis/snapshot_fresh_orig/MANIFEST.json` + `bridge_summary_v2.json → v2_provenance`) |
| classifier under test | `bench/score_bridge.py` `extract_cited` (regex) + `Oracle` (imported, not re-implemented) |
| oracle decl index | `.claude/skills/mathlib-search/.cache/declaration-data.json`, etag `6a6dbedd-3f3c0b9`, 418,630 declarations (current cache; per-arm citation aggregates reproduce the draft-v2 §"Hallucinated citations" table **exactly**: A 94/443, B 76/429, C 108/517, D 32/472, E 107/505 — so verdicts are stable vs the July scoring) |
| oracle rename map | `catalog/data/decl_renames.jsonl` (331 verified renames) |
| ground-truth tree | `mathlib4 @ 61a5e4f338` (the rev the agents' file tools saw), cross-checked at `9944fe2973` (the `bench-lean-fresh` typecheck env's mathlib rev) via `git grep` at those revs |
| arms | A no-tools · B wiki · C formal-search · D wikibrain · E wiki+formal union (`bench/arms/mcp-*.json`) |

## Part 2 — run-level "any hallucination", raw oracle

Outcome per (arm, task): ≥1 citation classified `hallucinated`. n=100 paired
fresh tasks per arm.

| arm | runs w/ ≥1 halluc | rate | Wilson 95% | citation-level |
|---|---|---|---|---|
| A | 54/100 | 54% | [44.3, 63.4] | 94/443 = 21.2% |
| B | 48/100 | 48% | [38.5, 57.7] | 76/429 = 17.7% |
| C | 49/100 | 49% | [39.4, 58.7] | 108/517 = 20.9% |
| **D** | **23/100** | **23%** | [15.8, 32.2] | 32/472 = 6.8% |
| E | 49/100 | 49% | [39.4, 58.7] | 107/505 = 21.2% |

Paired exact McNemar (two-sided binomial on discordant pairs):

| pair | X-only | Y-only | discordant | p | rate diff (paired Wald 95%) |
|---|---|---|---|---|---|
| **D vs E** | 10 | 36 | 46 | **1.56e-4** | −0.26 [−0.383, −0.137] |
| **D vs C** | 8 | 34 | 42 | **6.88e-5** | −0.26 [−0.376, −0.144] |
| D vs A | 7 | 38 | 45 | 3.12e-6 | −0.31 [−0.427, −0.193] |
| D vs B | 8 | 33 | 41 | 1.12e-4 | −0.25 [−0.366, −0.135] |
| E vs C | 10 | 10 | 20 | 1.0 | 0.00 [−0.088, 0.088] |

The reviewer's point stands quantitatively: the citation-level ratio is ~3.1×
(E/D) and ~3.1× (C/D), but the **run-level** ratio is **2.13×** (49% vs 23%)
— citations cluster within runs. Under the raw oracle the run-level paired
contrast is nevertheless strongly significant.

## Part 1 — blinded oracle validation (n = 60)

**Protocol.** Seeded (20260801) stratified sample of 60 **distinct** cited
names over arm × oracle-verdict strata (per arm: 6 hallucinated, 5 exists,
1 renamed; the oracle is deterministic per name, so distinct names maximize
information). The grader saw only `halluc_blind/blinded_sample.json` (name +
one context line, shuffled, **no verdicts**) and `halluc_blind/evidence.json`
(raw `git grep` output at the pin — no oracle lookups), determined ground truth
for all 60, wrote `halluc_blind/truth_labels.json`, and only then ran
`compare`, which unseals `halluc_blind/sample_key.json`. Grading required
namespace resolution, `@[to_dual]`-generation tracing, `_root_.` declarations,
and structure-field projections — i.e., exactly the care the exact-string
oracle cannot apply.

**Truth labels.** real 46 · nonexistent 4 · not_a_citation 9 (extractor noise)
· renamed 1.

**Confusion (oracle → truth).**

| | truth real/renamed | truth nonexistent | truth not_a_citation |
|---|---|---|---|
| oracle hallucinated (30) | 18 | 4 | 8 |
| oracle exists (25) | 24 | 0 | 1 |
| oracle renamed (5) | 5 | 0 | 0 |

**Binary scores** (positive = hallucinated; truth-positive = nonexistent).
Strict counts noise flagged as hallucinated as FP (the pipeline reports those
tokens as hallucinated *citations* and they are not); lenient excludes noise
rows entirely.

| metric | strict (n=60) | lenient (n=51) |
|---|---|---|
| hallucinated precision | **4/30 = 13.3%** [5.3, 29.7] | 4/22 = 18.2% [7.3, 38.5] |
| hallucinated recall | 4/4 = 100% [51.0, 100] | 4/4 = 100% |
| real precision (oracle exists/renamed) | 30/30 = 100% [88.7, 100] | 29/29 = 100% |
| real recall | 30/56 = 53.6% | 29/47 = 61.7% |
| accuracy | 56.7% | 64.7% |

**The four confirmed hallucinations** (all verified zero-hit at both mathlib
revs): `FractionField` (A, fresh_041; real name `FractionRing`),
`Localization.At` (B, fresh_078; real name `Localization.AtPrime`),
`Ideal.vanishingLocus` (B, fresh_077; real names `zeroLocus`/`vanishingIdeal`),
`Nat.divisorSum` (B, fresh_063; the divisor sum is `ArithmeticFunction.sigma`).
Notably all four sit in no-Brain arms; none of the 30 oracle-negative items was
a missed hallucination (recall caveat: only 4 truth-positives in sample).

**The 26 false "hallucinated" flags decompose into six mechanical modes:**

| mode | n | examples |
|---|---|---|
| namespace short name (decl real, cited without its namespace prefix, standard under `open`) | 13 | `IntegrableOn`→`MeasureTheory.IntegrableOn`, `Epi`→`CategoryTheory.Epi` (×2), `SignedMeasure` (×2), `Measure`, `AddContent`, `Ici`, `Icc`, `PseudoMetrizableSpace`, `BoundedAtFilter`, `JordanDecomposition.negPart`, `JordanDecomposition.toSignedMeasure` |
| self-declared theorem name (the output's own `theorem X.y` header) | 4 | `Monotone.const_smul_nonneg` (D), `SignedMeasure.jordan` (A), `Submodule.eq_span_of_finrank_eq_one` (C), `List.find_congr` (A). (A 5th sampled self-declaration, `LinearIsometry.normDet_eq_one`, landed oracle-exists — it is the task's own target decl, present at the pin.) |
| dot-notation on a variable (`M.det` = `Matrix.det M`) | 5 | `M.det` (×2), `M.restrict`, `S.map`, `S.Exact` |
| comment prose | 2 | `Bottom`, `Then` (both from `--` comment lines) |
| import-line module name | 1 | `Mathlib.LinearAlgebra.Eigenspace.Semisimple` |
| mid-dot-chain fragment | 1 | `Laurent.coeff.support` (regex starts at the capital inside `f.toLaurent.coeff.support`) |

(The renamed stratum was 4× `IsImmersion` + `Basis` — all resolve at the pin;
no binary error.)

## Sensitivity — repaired instrument, and what survives

The five FP modes are mechanically detectable, so `adjusted` re-scores every
citation with rules R1–R4 (drop comments / import lines / self-declarations /
single-letter dot-notation heads) and R5 (a name is `exists` if some
single-segment prefix `NS.` makes it an indexed declaration — the `open NS`
convention). Validation: the adjusted classifier agrees with the blinded truth
labels on **59/60** items (sole miss: `Laurent.coeff.support`, a mid-dot-chain
fragment of `f.toLaurent.coeff.support` that R4 does not catch; sample
precision 4/5, recall 4/4).

| arm | runs w/ ≥1 halluc (adj) | Wilson 95% | citation-level (adj) | R5 reclassified | R1–R4 dropped |
|---|---|---|---|---|---|
| A | 37/100 | [28.2, 46.8] | 50/428 = 11.7% | 29 | 15 |
| B | 30/100 | [21.9, 39.6] | 38/416 = 9.1% | 25 | 13 |
| C | 11/100 | [6.3, 18.6] | 26/500 = 5.2% | **65** | 17 |
| **D** | **6/100** | [2.8, 12.5] | 7/457 = 1.5% | **10** | 15 |
| E | 10/100 | [5.5, 17.4] | 14/483 = 2.9% | **71** | 22 |

| pair | X-only | Y-only | p (McNemar exact) |
|---|---|---|---|
| **D vs E** | 6 | 10 | **0.454** |
| **D vs C** | 5 | 10 | **0.302** |
| D vs A | 3 | 34 | 1.23e-7 |
| D vs B | 5 | 29 | 3.86e-5 |
| E vs C | 3 | 4 | 1.0 |

## Implications for the report (§sec:halluc / §sec:verifier)

1. **The asked-for run-level counterpart exists and is honest either way:**
   raw oracle D 23% vs E/C 49%, McNemar p ≈ 1e-4 — but run-level 2.13×, not
   the citation-level 3.1×. The "roughly 3×" phrasing should be replaced by
   the run-level numbers.
2. **The raw oracle is not a valid absolute-hallucination instrument.** On a
   blinded sample its hallucinated-class precision is 13% [5.3, 29.7]
   (18% excluding extractor noise). Most flags are real declarations cited by
   their conventional short names, plus extractor noise.
3. **The D-vs-E/C contrast largely dissolves under the repaired instrument**
   (6 vs 10/11 runs; p = 0.45 / 0.30). Direction preserved but n=100 cannot
   distinguish it. What survives decisively is **tools vs no-Brain-free-text
   arms**: D/C/E at 6–11% vs A 37%, B 30% (D-vs-A p = 1.2e-7).
4. **Mechanism re-attribution:** much of D's raw-oracle advantage is citation
   *style*, not grounding — D copies fully-qualified names out of Brain/
   `decl_exists` payloads (only 10 R5 reclassifications) while C/E write
   idiomatic namespace-short names (65/71 reclassifications). The §sec:verifier
   claim "collapses hallucinated citations roughly 3×" should be restated as:
   the verifier arm's citations are far more often exact fully-qualified
   index hits; after normalizing for namespace style, its true-hallucination
   edge over other tool arms is directionally positive but not significant at
   n=100, while all tool arms crush the no-tools baselines.
5. All four confirmed fabricated names came from A/B runs; none from D/C/E in
   the sample — consistent with (4).
