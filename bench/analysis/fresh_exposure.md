# Fresh-set exposure & robustness (Bridge report v2)

Pinned tree: mathlib4 `61a5e4f338` (content date 2026-07-10), read-only `git archive` extraction. Outcomes: snapshot of `bridge_summary.json` paired matrix (success = produced ∧ no-halluc ∧ TYPECHECK (judge pending calibration)). Arm E rows fresh_069-099 errored (session-limit 429) in the snapshot and count as failures in primary E numbers; `E_attempted_only` rows exclude them.

## Exposure counts (of 100 fresh tasks)

| flag | n |
|---|---|
| gold full dotted name appears verbatim anywhere | 37 |
| gold basename as decl-keyword header anywhere | 64 |
| gold basename as decl header in task's own module | 51 |
| gold commit is ancestor of pin (decl truly in tree) | 49 |
| added_date strictly before 2026-07-10 | 49 |

date<2026-07-10 coincides exactly with ancestor-of-pin on this task set; the 7 tasks dated 2026-07-10 all landed after the pin commit and are in the post-pin stratum

## Robustness split: exposed vs unexposed (own-module basis)

exposed = gold basename appears as a decl-keyword header in the task's own module file at the pin

### Exposed (n = 51)

| arm | grounded-typecheck | Wilson 95% CI |
|---|---|---|
| A | 9/51 = 17.6% | [9.6%, 30.2%] |
| B | 8/51 = 15.7% | [8.2%, 28.0%] |
| C | 11/51 = 21.6% | [12.5%, 34.6%] |
| D | 24/51 = 47.1% | [34.1%, 60.5%] |
| E | 12/51 = 23.5% | [14.0%, 36.8%] |
| E_attempted_only | 12/44 = 27.3% | [16.4%, 41.9%] |

| pair | both | D-only | other-only | neither | exact McNemar p |
|---|---|---|---|---|---|
| D_vs_E | 9 | 15 | 3 | 24 | 0.00754 |
| D_vs_C | 6 | 18 | 5 | 22 | 0.0106 |

### Unexposed (n = 49)

| arm | grounded-typecheck | Wilson 95% CI |
|---|---|---|
| A | 11/49 = 22.4% | [13.0%, 35.9%] |
| B | 14/49 = 28.6% | [17.8%, 42.4%] |
| C | 14/49 = 28.6% | [17.8%, 42.4%] |
| D | 18/49 = 36.7% | [24.7%, 50.7%] |
| E | 4/49 = 8.2% | [3.2%, 19.2%] |
| E_attempted_only | 4/25 = 16.0% | [6.4%, 34.6%] |

| pair | both | D-only | other-only | neither | exact McNemar p |
|---|---|---|---|---|---|
| D_vs_E | 1 | 17 | 3 | 28 | 0.00258 |
| D_vs_C | 8 | 10 | 6 | 25 | 0.454 |

## Split by merge date vs the pin (2026-07-10)

in_pin = added_in.commit is a git ancestor of the pinned commit (== added_date < 2026-07-10 here)

### Merged before pin (gold in tree) (n = 49)

| arm | grounded-typecheck | Wilson 95% CI |
|---|---|---|
| A | 9/49 = 18.4% | [10.0%, 31.4%] |
| B | 8/49 = 16.3% | [8.5%, 29.0%] |
| C | 11/49 = 22.4% | [13.0%, 35.9%] |
| D | 24/49 = 49.0% | [35.6%, 62.5%] |
| E | 12/49 = 24.5% | [14.6%, 38.1%] |
| E_attempted_only | 12/42 = 28.6% | [17.2%, 43.6%] |

| pair | both | D-only | other-only | neither | exact McNemar p |
|---|---|---|---|---|---|
| D_vs_E | 9 | 15 | 3 | 22 | 0.00754 |
| D_vs_C | 6 | 18 | 5 | 20 | 0.0106 |

### Merged after pin (gold NOT in tree) (n = 51)

| arm | grounded-typecheck | Wilson 95% CI |
|---|---|---|
| A | 11/51 = 21.6% | [12.5%, 34.6%] |
| B | 14/51 = 27.5% | [17.1%, 40.9%] |
| C | 14/51 = 27.5% | [17.1%, 40.9%] |
| D | 18/51 = 35.3% | [23.6%, 49.0%] |
| E | 4/51 = 7.8% | [3.1%, 18.5%] |
| E_attempted_only | 4/27 = 14.8% | [5.9%, 32.5%] |

| pair | both | D-only | other-only | neither | exact McNemar p |
|---|---|---|---|---|---|
| D_vs_E | 1 | 17 | 3 | 30 | 0.00258 |
| D_vs_C | 8 | 10 | 6 | 27 | 0.454 |

## Overall fresh set (n = 100)

### All fresh (n = 100)

| arm | grounded-typecheck | Wilson 95% CI |
|---|---|---|
| A | 20/100 = 20.0% | [13.3%, 28.9%] |
| B | 22/100 = 22.0% | [15.0%, 31.1%] |
| C | 25/100 = 25.0% | [17.5%, 34.3%] |
| D | 42/100 = 42.0% | [32.8%, 51.8%] |
| E | 16/100 = 16.0% | [10.1%, 24.4%] |
| E_attempted_only | 16/69 = 23.2% | [14.8%, 34.4%] |

| pair | both | D-only | other-only | neither | exact McNemar p |
|---|---|---|---|---|---|
| D_vs_E | 10 | 32 | 6 | 52 | 2.43e-05 |
| D_vs_C | 14 | 28 | 11 | 47 | 0.00948 |

## Per-task exposure table

| id | gold decl | module | added | in-pin | full-name | base-anywhere | base-in-module |
|---|---|---|---|---|---|---|---|
| fresh_000 | `Module.exists_localizedMap_surjective_of_surjective` | Algebra.Module.FinitePresentation | 2026-07-10 |  |  |  |  |
| fresh_001 | `MonoidAlgebra.addMonoidHom_ext` | Algebra.MonoidAlgebra.Defs | 2026-07-04 | Y | Y | Y | Y |
| fresh_002 | `MonoidAlgebra.single_left_injective` | Algebra.MonoidAlgebra.Defs | 2026-07-04 | Y |  | Y | Y |
| fresh_003 | `MonoidAlgebra.coeff_mul_mul_of_uniqueMul` | Algebra.MonoidAlgebra.NoZeroDivisors | 2026-07-04 | Y |  | Y | Y |
| fresh_004 | `MonoidAlgebra.mem_span_support_coeff` | Algebra.MonoidAlgebra.Support | 2026-07-04 | Y |  | Y | Y |
| fresh_005 | `Antitone.const_smul` | Algebra.Order.Module.Defs | 2026-07-08 | Y | Y | Y | Y |
| fresh_006 | `Monotone.const_smul` | Algebra.Order.Module.Defs | 2026-07-08 | Y | Y | Y | Y |
| fresh_007 | `StrictMono.const_smul` | Algebra.Order.Module.Defs | 2026-07-08 | Y | Y | Y | Y |
| fresh_008 | `LaurentPolynomial.support_coeff_toLaurent` | Algebra.Polynomial.Laurent | 2026-07-04 | Y |  | Y | Y |
| fresh_009 | `AbsoluteValue.denseRange_algebraMap_pi` | Analysis.AbsoluteValue.Equivalence | 2026-07-03 | Y | Y | Y | Y |
| fresh_010 | `LinearMap._root_.LinearIsometry.normDet_eq_one` | Analysis.InnerProductSpace.NormDet | 2026-07-08 | Y |  | Y | Y |
| fresh_011 | `LinearMap.normDet_eq_norm_det` | Analysis.InnerProductSpace.NormDet | 2026-07-08 | Y | Y | Y | Y |
| fresh_012 | `LinearMap.normDet_sq_eq_det_gram` | Analysis.InnerProductSpace.NormDet | 2026-07-08 | Y | Y | Y | Y |
| fresh_013 | `Submodule.mem_span_singleton_of_inner_eq_zero_of_inner_eq_zero` | Analysis.InnerProductSpace.Projection.FiniteDimensional | 2026-07-08 | Y |  | Y | Y |
| fresh_014 | `MeromorphicAt.meromorphicOrderAt_nonneg_iff` | Analysis.Meromorphic.Order | 2026-07-07 | Y | Y | Y | Y |
| fresh_015 | `integral_inv_div_log` | Analysis.SpecialFunctions.Integrals.Basic | 2026-07-08 | Y | Y | Y | Y |
| fresh_016 | `integral_inv_div_log_sq` | Analysis.SpecialFunctions.Integrals.Basic | 2026-07-08 | Y | Y | Y | Y |
| fresh_017 | `Real.abs_sub_sin_le` | Analysis.SpecialFunctions.Trigonometric.Bounds | 2026-07-07 | Y |  | Y | Y |
| fresh_018 | `Real.sin_ge_sub_cube` | Analysis.SpecialFunctions.Trigonometric.Bounds | 2026-07-07 | Y |  | Y | Y |
| fresh_019 | `AntitoneOn.abs_tsum_sub_sum_range_le_integral` | Analysis.SumIntegralComparisons | 2026-07-07 | Y | Y | Y | Y |
| fresh_020 | `AntitoneOn.integrableOn_Ioi_of_summable_comp_add` | Analysis.SumIntegralComparisons | 2026-07-07 | Y | Y | Y | Y |
| fresh_021 | `AntitoneOn.integral_le_tsum` | Analysis.SumIntegralComparisons | 2026-07-07 | Y | Y | Y | Y |
| fresh_022 | `AntitoneOn.sum_Ico_le_integral` | Analysis.SumIntegralComparisons | 2026-07-07 | Y | Y | Y | Y |
| fresh_023 | `AntitoneOn.sum_range_le_integral` | Analysis.SumIntegralComparisons | 2026-07-07 | Y | Y | Y | Y |
| fresh_024 | `AntitoneOn.summable_of_integrableOn_Ioi` | Analysis.SumIntegralComparisons | 2026-07-07 | Y | Y | Y | Y |
| fresh_025 | `AntitoneOn.summable_of_integrableOn_Ioi_zero` | Analysis.SumIntegralComparisons | 2026-07-07 | Y | Y | Y | Y |
| fresh_026 | `AntitoneOn.tsum_add_one_le_integral` | Analysis.SumIntegralComparisons | 2026-07-07 | Y | Y | Y | Y |
| fresh_027 | `AntitoneOn.tsum_le_integral` | Analysis.SumIntegralComparisons | 2026-07-07 | Y | Y | Y | Y |
| fresh_028 | `CategoryTheory.cube_lemma_of_epi` | CategoryTheory.EpiMono | 2026-07-07 | Y | Y | Y | Y |
| fresh_029 | `CategoryTheory.cube_lemma_of_mono` | CategoryTheory.EpiMono | 2026-07-07 | Y | Y | Y | Y |
| fresh_030 | `PowerSeries.WithPiTopology.hasProd_one_sub_X_pow` | Combinatorics.Enumerative.Pentagonal.PowerSeries | 2026-07-15 |  |  |  |  |
| fresh_031 | `PowerSeries.WithPiTopology.hasSum_pentagonalSeries` | Combinatorics.Enumerative.Pentagonal.PowerSeries | 2026-07-15 |  |  |  |  |
| fresh_032 | `List.find?_congr` | Data.List.Find | 2026-07-13 |  |  |  |  |
| fresh_033 | `List.find?_eq_find?_of_perm` | Data.List.Find | 2026-07-13 |  |  |  |  |
| fresh_034 | `Manifold.IsImmersion._root_.ContMDiff.iff_comp_isImmersion` | Geometry.Manifold.Immersion | 2026-07-11 |  |  |  |  |
| fresh_035 | `Manifold.IsImmersionAt._root_.ContMDiffAt.iff_comp_isImmersionAt` | Geometry.Manifold.Immersion | 2026-07-11 |  |  |  |  |
| fresh_036 | `Manifold.IsImmersionOfComplement._root_.ContMDiff.iff_comp_isImmersionOfComplement` | Geometry.Manifold.Immersion | 2026-07-11 |  |  |  |  |
| fresh_037 | `Manifold.IsImmersionOfComplement.contMDiff` | Geometry.Manifold.Immersion | 2026-07-11 |  |  | Y | Y |
| fresh_038 | `CommGroup.finite_torsion_of_descent` | GroupTheory.Descent | 2026-07-08 | Y | Y | Y | Y |
| fresh_039 | `CommGroup.finite_torsion_of_descent'` | GroupTheory.Descent | 2026-07-08 | Y | Y | Y | Y |
| fresh_040 | `UniqueFactorizationMonoid.of_isLocalization` | GroupTheory.MonoidLocalization.UniqueFactorization | 2026-07-13 |  |  | Y |  |
| fresh_041 | `IsFractionRing.finrank_right_eq` | LinearAlgebra.Dimension.Localization | 2026-07-14 |  |  |  |  |
| fresh_042 | `IsFractionRing.rank_right_eq` | LinearAlgebra.Dimension.Localization | 2026-07-14 |  |  |  |  |
| fresh_043 | `Module.IsTorsion.rank_eq_zero` | LinearAlgebra.Dimension.Torsion.Finite | 2026-07-14 |  |  | Y |  |
| fresh_044 | `Module.End.IsFinitelySemisimple.iSup_maxGenEigenspace_eq_top_iff` | LinearAlgebra.Eigenspace.Semisimple | 2026-07-14 |  |  |  |  |
| fresh_045 | `Module.End.IsSemisimple.iSup_maxGenEigenspace_eq_top_iff` | LinearAlgebra.Eigenspace.Semisimple | 2026-07-14 |  |  |  |  |
| fresh_046 | `eq_span_singleton_of_mem_of_finrank_eq_one` | LinearAlgebra.FiniteDimensional.Basic | 2026-07-08 | Y | Y | Y | Y |
| fresh_047 | `Matrix.exists_rank_normal_form` | LinearAlgebra.Matrix.Rank | 2026-07-07 | Y | Y | Y | Y |
| fresh_048 | `SymplecticGroup.det_eq_one` | LinearAlgebra.SymplecticGroup | 2026-07-07 | Y | Y | Y | Y |
| fresh_049 | `ContinuousOn.measurable_of_countable_compl` | MeasureTheory.Constructions.BorelSpace.Basic | 2026-07-09 | Y | Y | Y | Y |
| fresh_050 | `measurable_of_countable_not_continuousAt` | MeasureTheory.Constructions.BorelSpace.Basic | 2026-07-09 | Y | Y | Y | Y |
| fresh_051 | `MeasureTheory.StronglyMeasurable._root_.ContinuousOn.stronglyMeasurable_of_countable_compl` | MeasureTheory.Function.StronglyMeasurable.Basic | 2026-07-09 | Y |  | Y | Y |
| fresh_052 | `MeasureTheory.integral_comp_exp_Ioi` | MeasureTheory.Integral.IntegralEqImproper | 2026-07-16 |  |  |  |  |
| fresh_053 | `MeasureTheory.integral_comp_log_Ioi` | MeasureTheory.Integral.IntegralEqImproper | 2026-07-16 |  |  |  |  |
| fresh_054 | `MeasureTheory.IsSetSemiring.exists_disjoint_finset_sdiff_eq` | MeasureTheory.SetSemiring | 2026-07-16 |  | Y | Y | Y |
| fresh_055 | `MeasureTheory.VectorMeasure.exists_extension_of_isSetSemiring_of_le_measure` | MeasureTheory.VectorMeasure.AddContent | 2026-07-14 |  |  |  |  |
| fresh_056 | `MeasureTheory.SignedMeasure.apply_eq_posPart_real_sub_negPart_real` | MeasureTheory.VectorMeasure.Decomposition.Jordan | 2026-07-16 |  |  |  |  |
| fresh_057 | `MeasureTheory.SignedMeasure.enorm_le_totalVariation` | MeasureTheory.VectorMeasure.Variation.SignedMeasure | 2026-07-16 |  |  |  |  |
| fresh_058 | `MeasureTheory.SignedMeasure.norm_le_totalVariation` | MeasureTheory.VectorMeasure.Variation.SignedMeasure | 2026-07-16 |  |  |  |  |
| fresh_059 | `MeasureTheory.SignedMeasure.totalVariation_eq_variation` | MeasureTheory.VectorMeasure.Variation.SignedMeasure | 2026-07-16 |  |  |  |  |
| fresh_060 | `log_riemannZeta_eq` | NumberTheory.EulerProduct.DirichletLSeries | 2026-07-16 |  | Y |  |  |
| fresh_061 | `LSeries_def₀` | NumberTheory.LSeries.Basic | 2026-07-16 |  |  |  |  |
| fresh_062 | `EisensteinSeries.isBoundedAtImInfty_E2` | NumberTheory.ModularForms.EisensteinSeries.E2.Summable | 2026-07-03 | Y |  | Y | Y |
| fresh_063 | `EisensteinSeries.summable_sigma_mul_cexp_pow` | NumberTheory.ModularForms.EisensteinSeries.QExpansion | 2026-07-03 | Y | Y | Y | Y |
| fresh_064 | `UpperHalfPlane.isBoundedAtImInfty_of_hasSum_qExpansion` | NumberTheory.ModularForms.QExpansion | 2026-07-03 | Y |  | Y | Y |
| fresh_065 | `NumberField.InfinitePlace.IsRamified.finrank_eq_two` | NumberTheory.NumberField.Completion.Ramification | 2026-07-14 |  |  | Y |  |
| fresh_066 | `NumberField.InfinitePlace.IsUnramified.finrank_eq_one` | NumberTheory.NumberField.Completion.Ramification | 2026-07-14 |  |  | Y |  |
| fresh_067 | `NumberField.exists_not_isUnramifiedIn` | NumberTheory.NumberField.ExistsRamified | 2026-07-03 | Y | Y | Y | Y |
| fresh_068 | `Ideal.inertiaDeg'_algebra_tower` | NumberTheory.RamificationInertia.Inertia | 2026-07-04 | Y |  | Y | Y |
| fresh_069 | `Ideal.inertiaDeg'_pos'` | NumberTheory.RamificationInertia.Inertia | 2026-07-04 | Y |  | Y | Y |
| fresh_070 | `BddAbove.range_comp_right` | Order.Bounds.Basic | 2026-07-13 |  |  |  |  |
| fresh_071 | `JordanHolderLattice.Iso.rel` | Order.JordanHolder | 2026-07-14 |  |  | Y |  |
| fresh_072 | `Finite.of_wellFoundedLT_wellFoundedGT` | Order.OrderIsoNat | 2026-07-13 |  |  |  |  |
| fresh_073 | `Infinite.exists_strictMono_or_strictAnti` | Order.OrderIsoNat | 2026-07-13 |  |  |  |  |
| fresh_074 | `StrictMono.apply_eq` | Order.Preorder.Finite | 2026-07-13 |  | Y |  |  |
| fresh_075 | `StrictMono.eq_id` | Order.Preorder.Finite | 2026-07-13 |  |  | Y |  |
| fresh_076 | `Rep.res_map_exact` | RepresentationTheory.Rep.Res | 2026-07-04 | Y |  | Y | Y |
| fresh_077 | `ModuleCat.exists_isRegular_of_exists_subsingleton_ext` | RingTheory.Depth.Rees | 2026-07-03 | Y |  | Y | Y |
| fresh_078 | `ModuleCat.exists_isRegular_tfae` | RingTheory.Depth.Rees | 2026-07-03 | Y | Y | Y | Y |
| fresh_079 | `Module.Free.away_of_finite_of_flat_of_rankAtStalk_constant` | RingTheory.Flat.LocallyFree | 2026-07-10 |  |  |  |  |
| fresh_080 | `MvPowerSeries.pderiv.ext` | RingTheory.MvPowerSeries.Derivative | 2026-07-16 |  |  | Y |  |
| fresh_081 | `MvPowerSeries.pderiv_pow` | RingTheory.MvPowerSeries.Derivative | 2026-07-16 |  |  | Y |  |
| fresh_082 | `Module.Invertible.exists_finset_free_localization` | RingTheory.PicardGroup | 2026-07-10 |  |  |  |  |
| fresh_083 | `Cardinal.toNat_eq_of_forall_le_iff` | SetTheory.Cardinal.ToNat | 2026-07-14 |  |  |  |  |
| fresh_084 | `LinearMap.isClosed_range_of_isClosed_map_of_finiteDimensional_quotient` | Topology.Algebra.Module.FiniteDimension | 2026-07-14 |  |  |  |  |
| fresh_085 | `IsPathConnected.pi` | Topology.Connected.PathConnected | 2026-07-10 |  |  | Y |  |
| fresh_086 | `IsPathConnected.prod` | Topology.Connected.PathConnected | 2026-07-10 |  |  | Y |  |
| fresh_087 | `BoundedVariationOn.id_Icc` | Topology.EMetricSpace.BoundedVariation | 2026-07-16 |  |  |  |  |
| fresh_088 | `MonotoneOn.eVariationOn_eq` | Topology.EMetricSpace.BoundedVariation | 2026-07-16 |  |  |  |  |
| fresh_089 | `eVariationOn._root_.BoundedVariationOn.of_finite` | Topology.EMetricSpace.BoundedVariation | 2026-07-16 |  |  | Y |  |
| fresh_090 | `eVariationOn.image_range_of_monotone` | Topology.EMetricSpace.BoundedVariation | 2026-07-16 |  |  |  |  |
| fresh_091 | `eVariationOn.pair` | Topology.EMetricSpace.BoundedVariation | 2026-07-16 |  |  | Y |  |
| fresh_092 | `eVariationOn.union'` | Topology.EMetricSpace.BoundedVariation | 2026-07-16 |  |  | Y |  |
| fresh_093 | `eVariationOn_id` | Topology.EMetricSpace.BoundedVariation | 2026-07-16 |  |  |  |  |
| fresh_094 | `eVariationOn_id_Icc` | Topology.EMetricSpace.BoundedVariation | 2026-07-16 |  |  |  |  |
| fresh_095 | `LocallyBoundedVariationOn.exists_monotoneOn_sub_monotoneOn'` | Topology.EMetricSpace.VariationOnFromTo | 2026-07-07 | Y | Y | Y | Y |
| fresh_096 | `Continuous.of_ordContinuous` | Topology.Order.Basic | 2026-07-08 | Y | Y | Y | Y |
| fresh_097 | `exists_mem_Icc_isFixedPt_of_surjOn` | Topology.Order.IntermediateValue | 2026-07-10 |  |  |  |  |
| fresh_098 | `exists_mem_uIcc_isFixedPt_of_mapsTo` | Topology.Order.IntermediateValue | 2026-07-10 |  |  |  |  |
| fresh_099 | `Continuous.leftOrdContinuous` | Topology.Order.Monotone | 2026-07-08 | Y | Y | Y | Y |

Reproduce: `python3 bench/analysis/fresh_exposure.py` (reads only the snapshot in `bench/analysis/snapshot_fresh_orig/`).
