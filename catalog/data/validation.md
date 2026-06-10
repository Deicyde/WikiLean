# WikiLean validation pass — hand-check

Replace each `\_` with a mark. **Use exactly one character per cell, case-insensitive.**

- Per-decl marks (in the table rows):
  - `Y` — correct match (this decl formalizes the article's concept)
  - `P` — partial (right area, but too narrow/broad or wrong sense)
  - `N` — wrong (does not formalize this concept)
  - `?` — can't tell

- Per-article verdicts (matched articles):
  - `Y` Mathlib fully formalizes  ·  `P` partial coverage  ·  `N` not formalized  ·  `?` unclear

- No-match articles: `Y` = you agree there's no formalization; `N` = you found one; `?` = unclear.


Sample: 10 pilot-matched, 5 pilot-no-match, 15 tier2-matched, 10 tier2-no-match  (seed 20260520). Total: **40**.


---

### 1. [Complete metric space](https://en.wikipedia.org/wiki/Complete_metric_space) · C/High · _tier2_ · primary=`CompleteSpace`

> Mathlib defines completeness at the uniform-space level (CompleteSpace); metric spaces inherit it via their uniform structure.

| Mark | Decl | Conf | Source |
|---|---|---|---|
| `Y` | `CompleteSpace` | high | [Cauchy](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Topology/UniformSpace/Cauchy.lean#L368) |
| `Y` | `IsComplete` | high | [Cauchy](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Topology/UniformSpace/Cauchy.lean#L35) |
| `Y` | `CauchySeq` | high | [Cauchy](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Topology/UniformSpace/Cauchy.lean#L185) |
| `Y` | `UniformSpace.Completion` | high | [Completion](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Topology/UniformSpace/Completion.lean#L290) |
| `Y` | `Cauchy` | medium | [Cauchy](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Topology/UniformSpace/Cauchy.lean#L30) |

**Article-level verdict** (does Mathlib formalize this concept?): `Y`  _(Y=fully · P=partial · N=no · ?=unclear)_

---

### 2. [Rotation (mathematics)](https://en.wikipedia.org/wiki/Rotation_%28mathematics%29) · C/High · _tier2_ · primary=`Orientation.rotation`

> Mathlib formalizes rotation as an oriented-angle linear isometry on 2D inner product spaces; the rotation group is captured via the (special) orthogonal group of matrices.

| Mark | Decl | Conf | Source |
|---|---|---|---|
| `Y` | `Orientation.rotation` | high | [Rotation](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Geometry/Euclidean/Angle/Oriented/Rotation.lean#L62) |
| `Y` | `rotation` | high | [Isometry](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Analysis/Complex/Isometry.lean#L48) |
| `Y` | `Matrix.specialOrthogonalGroup` | high | [UnitaryGroup](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/LinearAlgebra/UnitaryGroup.lean#L315) |
| `P` | `Matrix.orthogonalGroup` | medium | [UnitaryGroup](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/LinearAlgebra/UnitaryGroup.lean#L295) |

**Article-level verdict** (does Mathlib formalize this concept?): `Y`  _(Y=fully · P=partial · N=no · ?=unclear)_

---

### 3. [Critical point (mathematics)](https://en.wikipedia.org/wiki/Critical_point_%28mathematics%29) · C/High · _tier2_ · NO MATCH (reason: _not formalized_)

> Mathlib has no dedicated `CriticalPoint` predicate; the concept appears only implicitly in Fermat's theorem (`IsLocalExtr.deriv_eq_zero`, `IsLocalExtr.fderiv_eq_zero`) and Rolle's theorem (`exists_deriv_eq_zero`), which state results about points where the derivative vanishes rather than defining critical points themselves.

**Do you agree there's no Mathlib formalization?** `Y`  _(Y=agree · N=disagree, formalization exists · ?=unclear)_

---

### 4. [Order of magnitude](https://en.wikipedia.org/wiki/Order_of_magnitude) · C/High · _tier2_ · NO MATCH (reason: _too elementary_)

> Order of magnitude is an informal/applied notion (powers of ten on a log scale); Mathlib has no direct formalization, though related primitives like Real.log and Int.log exist.

**Do you agree there's no Mathlib formalization?** `Y`  _(Y=agree · N=disagree, formalization exists · ?=unclear)_

---

### 5. [Borel–Weil–Bott theorem](https://en.wikipedia.org/wiki/Borel%E2%80%93Weil%E2%80%93Bott_theorem) · C/High · _tier2_ · NO MATCH (reason: _not formalized_)

> No Mathlib declarations found for Borel–Weil–Bott; the prerequisites (flag varieties, line bundles on G/B, sheaf cohomology of equivariant bundles, highest-weight representations of Lie groups) are not formalized.

**Do you agree there's no Mathlib formalization?** `Y`  _(Y=agree · N=disagree, formalization exists · ?=unclear)_

---

### 6. [Turán graph](https://en.wikipedia.org/wiki/Tur%C3%A1n_graph) · B/Mid · _tier2_ · primary=`SimpleGraph.turanGraph`

> turanGraph defines T(n,r) on Fin n via the modular partition; auxiliary lemmas capture clique-freeness and Turán-maximality.

| Mark | Decl | Conf | Source |
|---|---|---|---|
| `Y` | `SimpleGraph.turanGraph` | high | [Turan](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Combinatorics/SimpleGraph/Extremal/Turan.lean#L63) |
| `Y` | `SimpleGraph.IsTuranMaximal` | high | [Turan](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Combinatorics/SimpleGraph/Extremal/Turan.lean#L56) |
| `Y` | `SimpleGraph.turanGraph_cliqueFree` | high | [Turan](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Combinatorics/SimpleGraph/Extremal/Turan.lean#L85) |
| `Y` | `SimpleGraph.isTuranMaximal_turanGraph` | high | [Turan](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Combinatorics/SimpleGraph/Extremal/Turan.lean#L287) |
| `Y` | `SimpleGraph.isTuranMaximal_iff_nonempty_iso_turanGraph` | high | [Turan](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Combinatorics/SimpleGraph/Extremal/Turan.lean#L292) |

**Article-level verdict** (does Mathlib formalize this concept?): `Y`  _(Y=fully · P=partial · N=no · ?=unclear)_

---

### 7. [Steinmetz solid](https://en.wikipedia.org/wiki/Steinmetz_solid) · B/Mid · _tier2_ · NO MATCH (reason: _not formalized_)

> No Steinmetz solid, bicylinder, or tricylinder declarations exist in Mathlib.

**Do you agree there's no Mathlib formalization?** `Y`  _(Y=agree · N=disagree, formalization exists · ?=unclear)_

---

### 8. [Divergence](https://en.wikipedia.org/wiki/Divergence) · C/High · _tier2_ · primary=`MeasureTheory.integral_divergence_of_hasFDerivAt_off_countable`

> Mathlib has no standalone `divergence` operator definition; the divergence `∑ i, f' x (Pi.single i 1) i` appears inline inside the divergence theorem statements.

| Mark | Decl | Conf | Source |
|---|---|---|---|
| `P` | `MeasureTheory.integral_divergence_of_hasFDerivAt_off_countable` | high | [DivergenceTheorem](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/MeasureTheory/Integral/DivergenceTheorem.lean#L267) |
| `P` | `BoxIntegral.hasIntegral_GP_divergence_of_forall_hasDerivWithinAt` | high | [DivergenceTheorem](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Analysis/BoxIntegral/DivergenceTheorem.lean#L263) |
| `P` | `MeasureTheory.integral_divergence_of_hasFDerivAt_off_countable'` | medium | [DivergenceTheorem](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/MeasureTheory/Integral/DivergenceTheorem.lean#L297) |

**Article-level verdict** (does Mathlib formalize this concept?): `P`  _(Y=fully · P=partial · N=no · ?=unclear)_

---

### 9. [Brouwer fixed-point theorem](https://en.wikipedia.org/wiki/Brouwer_fixed-point_theorem) · B/High · _pilot_ · NO MATCH (reason: _not formalized_)

> Mathlib has no Brouwer fixed-point theorem; the only 'Brouwer' references are to Brouwer/Heyting algebras in order theory, and no continuous-self-map fixed-point result on the disk/ball/convex compact set was found.

**Do you agree there's no Mathlib formalization?** `_`  _(Y=agree · N=disagree, formalization exists · ?=unclear)_

---

### 10. [Three-dimensional space](https://en.wikipedia.org/wiki/Three-dimensional_space) · C/Top · _tier2_ · primary=`EuclideanSpace`

> 3D Euclidean space is `EuclideanSpace ℝ (Fin 3)`; the cross product is the canonical 3D-specific structure in Mathlib.

| Mark | Decl | Conf | Source |
|---|---|---|---|
| `Y` | `EuclideanSpace` | high | [PiL2](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Analysis/InnerProductSpace/PiL2.lean#L111) |
| `Y` | `crossProduct` | high | [CrossProduct](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/LinearAlgebra/CrossProduct.lean#L50) |

**Article-level verdict** (does Mathlib formalize this concept?): `Y`  _(Y=fully · P=partial · N=no · ?=unclear)_

---

### 11. [Graph coloring](https://en.wikipedia.org/wiki/Graph_coloring) · B/High · _pilot_ · primary=`SimpleGraph.Coloring`

> Mathlib formalizes vertex coloring as a graph hom into the complete graph, with Colorable and chromaticNumber built on top.

| Mark | Decl | Conf | Source |
|---|---|---|---|
| `_` | `SimpleGraph.Coloring` | high | [VertexColoring](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Combinatorics/SimpleGraph/Coloring/VertexColoring.lean#L74) |
| `_` | `SimpleGraph.Colorable` | high | [VertexColoring](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Combinatorics/SimpleGraph/Coloring/VertexColoring.lean#L162) |
| `_` | `SimpleGraph.chromaticNumber` | high | [VertexColoring](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Combinatorics/SimpleGraph/Coloring/VertexColoring.lean#L205) |
| `_` | `SimpleGraph.Coloring.colorClass` | medium | [VertexColoring](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Combinatorics/SimpleGraph/Coloring/VertexColoring.lean#L99) |

**Article-level verdict** (does Mathlib formalize this concept?): `_`  _(Y=fully · P=partial · N=no · ?=unclear)_

---

### 12. [Student's t-distribution](https://en.wikipedia.org/wiki/Student%27s_t-distribution) · B/High · _pilot_ · NO MATCH (reason: _not formalized_)

> Mathlib has many distributions (Gaussian, Cauchy, Gamma, Beta, etc.) but no Student's t-distribution; no occurrences of studentT/TDistribution found.

**Do you agree there's no Mathlib formalization?** `_`  _(Y=agree · N=disagree, formalization exists · ?=unclear)_

---

### 13. [Collatz conjecture](https://en.wikipedia.org/wiki/Collatz_conjecture) · C/High · _tier2_ · NO MATCH (reason: _not formalized_)

> No Collatz/hailstone declarations found in Mathlib; the conjecture is an open problem and not formalized in Mathlib.

**Do you agree there's no Mathlib formalization?** `Y`  _(Y=agree · N=disagree, formalization exists · ?=unclear)_

---

### 14. [History of geometry](https://en.wikipedia.org/wiki/History_of_geometry) · C/High · _tier2_ · NO MATCH (reason: _unclear scope_)

> The article is a historical survey of geometry as a discipline, not a specific mathematical concept that admits formalization.

**Do you agree there's no Mathlib formalization?** `Y`  _(Y=agree · N=disagree, formalization exists · ?=unclear)_

---

### 15. [Axiom of limitation of size](https://en.wikipedia.org/wiki/Axiom_of_limitation_of_size) · B/Mid · _tier2_ · primary=`None`

> Mathlib uses ZFC (with `ZFSet`/`Class` mirroring NBG's set/class distinction) but does not state von Neumann's axiom of limitation of size itself; only the surrounding class machinery and individual proper-class examples are present.

| Mark | Decl | Conf | Source |
|---|---|---|---|
| `P` | `Class` | low | [Class](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/SetTheory/ZFC/Class.lean#L35) |
| `P` | `Class.univ` | low | [Class](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/SetTheory/ZFC/Class.lean#L62) |
| `P` | `Class.mem_univ` | low | [Class](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/SetTheory/ZFC/Class.lean#L91) |
| `P` | `ZFSet.isOrdinal_notMem_univ` | low | [Class](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/SetTheory/ZFC/Class.lean#L373) |

**Article-level verdict** (does Mathlib formalize this concept?): `N`  _(Y=fully · P=partial · N=no · ?=unclear)_

---

### 16. [Finite difference](https://en.wikipedia.org/wiki/Finite_difference) · B/High · _pilot_ · primary=`fwdDiff`

> Mathlib formalizes the forward finite difference operator Δ_[h] f (x) = f (x + h) − f (x) along with the Gregory–Newton expansion.

| Mark | Decl | Conf | Source |
|---|---|---|---|
| `_` | `fwdDiff` | high | [ForwardDiff](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Algebra/Group/ForwardDiff.lean#L46) |
| `_` | `fwdDiff_aux.fwdDiffₗ` | high | [ForwardDiff](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Algebra/Group/ForwardDiff.lean#L94) |
| `_` | `shift_eq_sum_fwdDiff_iter` | high | [ForwardDiff](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Algebra/Group/ForwardDiff.lean#L175) |
| `_` | `fwdDiff_iter_eq_sum_shift` | high | [ForwardDiff](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Algebra/Group/ForwardDiff.lean#L147) |

**Article-level verdict** (does Mathlib formalize this concept?): `_`  _(Y=fully · P=partial · N=no · ?=unclear)_

---

### 17. [Linear approximation](https://en.wikipedia.org/wiki/Linear_approximation) · C/High · _tier2_ · primary=`HasFDerivAt`

> Linear (affine) approximation of a function is encoded by Mathlib's Fréchet-derivative predicate `HasFDerivAt` (and its filter/1-D variants); `ApproximatesLinearOn` quantifies how well an affine map approximates f on a set.

| Mark | Decl | Conf | Source |
|---|---|---|---|
| `Y` | `HasFDerivAt` | high | [Defs](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Analysis/Calculus/FDeriv/Defs.lean#L121) |
| `Y` | `HasFDerivAtFilter` | high | [Defs](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Analysis/Calculus/FDeriv/Defs.lean#L108) |
| `Y` | `HasDerivAt` | high | [Basic](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Analysis/Calculus/Deriv/Basic.lean#L130) |
| `Y` | `ApproximatesLinearOn` | high | [ApproximatesLinearOn](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Analysis/Calculus/InverseFunctionTheorem/ApproximatesLinearOn.lean#L72) |

**Article-level verdict** (does Mathlib formalize this concept?): `Y`  _(Y=fully · P=partial · N=no · ?=unclear)_

---

### 18. [Yang–Mills theory](https://en.wikipedia.org/wiki/Yang%E2%80%93Mills_theory) · B/High · _pilot_ · NO MATCH (reason: _not formalized_)

> Yang–Mills theory is a quantum field theory; no formalization of the Yang–Mills Lagrangian, action, or gauge theory framework exists in Mathlib.

**Do you agree there's no Mathlib formalization?** `_`  _(Y=agree · N=disagree, formalization exists · ?=unclear)_

---

### 19. [Euclidean space](https://en.wikipedia.org/wiki/Euclidean_space) · B/Top · _pilot_ · primary=`EuclideanSpace`

> Mathlib's `EuclideanSpace 𝕜 (Fin n)` is the standard finite-dimensional Euclidean n-space defined as PiLp 2 of copies of 𝕜.

| Mark | Decl | Conf | Source |
|---|---|---|---|
| `_` | `EuclideanSpace` | high | [PiL2](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Analysis/InnerProductSpace/PiL2.lean#L111) |
| `_` | `EuclideanSpace.equiv` | medium | [PiL2](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Analysis/InnerProductSpace/PiL2.lean#L276) |
| `_` | `EuclideanSpace.finAddEquivProd` | medium | [PiL2](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Analysis/InnerProductSpace/PiL2.lean#L378) |

**Article-level verdict** (does Mathlib formalize this concept?): `_`  _(Y=fully · P=partial · N=no · ?=unclear)_

---

### 20. [Exterior algebra](https://en.wikipedia.org/wiki/Exterior_algebra) · B/High · _pilot_ · primary=`ExteriorAlgebra`

> ExteriorAlgebra is defined as the Clifford algebra of the zero quadratic form; exterior powers and the canonical alternating map are also available.

| Mark | Decl | Conf | Source |
|---|---|---|---|
| `_` | `ExteriorAlgebra` | high | [Basic](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/LinearAlgebra/ExteriorAlgebra/Basic.lean#L60) |
| `_` | `ExteriorAlgebra.ι` | high | [Basic](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/LinearAlgebra/ExteriorAlgebra/Basic.lean#L69) |
| `_` | `ExteriorAlgebra.lift` | high | [Basic](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/LinearAlgebra/ExteriorAlgebra/Basic.lean#L106) |
| `_` | `exteriorPower` | high | [Basic](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/LinearAlgebra/ExteriorAlgebra/Basic.lean#L79) |
| `_` | `ExteriorAlgebra.ιMulti` | high | [Basic](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/LinearAlgebra/ExteriorAlgebra/Basic.lean#L269) |

**Article-level verdict** (does Mathlib formalize this concept?): `_`  _(Y=fully · P=partial · N=no · ?=unclear)_

---

### 21. [Frobenius theorem (differential topology)](https://en.wikipedia.org/wiki/Frobenius_theorem_%28differential_topology%29) · C/High · _tier2_ · NO MATCH (reason: _not formalized_)

> Mathlib has integral curves of a single vector field but no foliation/involutive distribution machinery, so the differential-topology Frobenius theorem is not formalized.

**Do you agree there's no Mathlib formalization?** `Y`  _(Y=agree · N=disagree, formalization exists · ?=unclear)_

---

### 22. [Closure (mathematics)](https://en.wikipedia.org/wiki/Closure_%28mathematics%29) · C/High · _tier2_ · primary=`ClosureOperator`

> ClosureOperator captures the general closure-operator notion; concrete generated sets (Submonoid.closure, Subgroup.closure, Submodule.span, etc.) are instances via LowerAdjoint.

| Mark | Decl | Conf | Source |
|---|---|---|---|
| `Y` | `ClosureOperator` | high | [Closure](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Order/Closure.lean#L60) |
| `Y` | `ClosureOperator.IsClosed` | high | [Closure](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Order/Closure.lean#L69) |
| `Y` | `LowerAdjoint` | high | [Closure](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Order/Closure.lean) |
| `Y` | `Submonoid.closure` | high | [Basic](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Order/Closure.lean#L22) |

**Article-level verdict** (does Mathlib formalize this concept?): `Y`  _(Y=fully · P=partial · N=no · ?=unclear)_

---

### 23. [Hexadecimal](https://en.wikipedia.org/wiki/Hexadecimal) · B/Mid · _tier2_ · primary=`Nat.digits`

> Hexadecimal is the b=16 instance of Mathlib's general positional digit representation `Nat.digits` / `Nat.ofDigits`; no hex-specific declaration exists.

| Mark | Decl | Conf | Source |
|---|---|---|---|
| `Y` | `Nat.digits` | high | [Defs](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Data/Nat/Digits/Defs.lean#L78) |
| `Y` | `Nat.ofDigits` | high | [Defs](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Data/Nat/Digits/Defs.lean#L145) |
| `Y` | `Nat.ofDigits_digits` | high | [Defs](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Data/Nat/Digits/Defs.lean#L242) |
| `Y` | `Nat.digits.injective` | medium | [Defs](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Data/Nat/Digits/Defs.lean#L297) |

**Article-level verdict** (does Mathlib formalize this concept?): `Y`  _(Y=fully · P=partial · N=no · ?=unclear)_

---

### 24. [Chinese mathematics](https://en.wikipedia.org/wiki/Chinese_mathematics) · C/High · _tier2_ · NO MATCH (reason: _unclear scope_)

> Chinese mathematics is a historical/cultural topic, not a single mathematical concept formalizable in Mathlib.

**Do you agree there's no Mathlib formalization?** `Y`  _(Y=agree · N=disagree, formalization exists · ?=unclear)_

---

### 25. [Theoretical computer science](https://en.wikipedia.org/wiki/Theoretical_computer_science) · B/Top · _pilot_ · NO MATCH (reason: _unclear scope_)

> Theoretical computer science is a broad umbrella field, not a single mathematical concept amenable to a specific Mathlib formalization.

**Do you agree there's no Mathlib formalization?** `_`  _(Y=agree · N=disagree, formalization exists · ?=unclear)_

---

### 26. [Young tableau](https://en.wikipedia.org/wiki/Young_tableau) · C/High · _tier2_ · primary=`YoungDiagram`

> Mathlib formalizes Young diagrams and semistandard Young tableaux; no standard Young tableau type is defined.

| Mark | Decl | Conf | Source |
|---|---|---|---|
| `Y` | `YoungDiagram` | high | [YoungDiagram](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Combinatorics/Young/YoungDiagram.lean#L66) |
| `Y` | `SemistandardYoungTableau` | high | [SemistandardTableau](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Combinatorics/Young/SemistandardTableau.lean#L56) |
| `Y` | `YoungDiagram.transpose` | high | [YoungDiagram](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Combinatorics/Young/YoungDiagram.lean#L186) |
| `Y` | `YoungDiagram.rowLens` | high | [YoungDiagram](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Combinatorics/Young/YoungDiagram.lean#L363) |
| `Y` | `YoungDiagram.equivListRowLens` | medium | [YoungDiagram](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Combinatorics/Young/YoungDiagram.lean#L459) |

**Article-level verdict** (does Mathlib formalize this concept?): `P`  _(Y=fully · P=partial · N=no · ?=unclear)_

---

### 27. [Fourier series](https://en.wikipedia.org/wiki/Fourier_series) · B/Top · _pilot_ · primary=`fourierCoeff`

> Mathlib formalizes Fourier series on AddCircle T via fourierCoeff and the Hilbert basis fourierBasis, with the L² convergence theorem hasSum_fourier_series_L2.

| Mark | Decl | Conf | Source |
|---|---|---|---|
| `_` | `fourierCoeff` | high | [AddCircle](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Analysis/Fourier/AddCircle.lean#L296) |
| `_` | `fourier` | high | [AddCircle](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Analysis/Fourier/AddCircle.lean#L123) |
| `_` | `fourierBasis` | high | [AddCircle](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Analysis/Fourier/AddCircle.lean#L411) |
| `_` | `hasSum_fourier_series_L2` | high | [AddCircle](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Analysis/Fourier/AddCircle.lean#L433) |
| `_` | `fourierCoeffOn` | high | [AddCircle](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Analysis/Fourier/AddCircle.lean#L351) |

**Article-level verdict** (does Mathlib formalize this concept?): `_`  _(Y=fully · P=partial · N=no · ?=unclear)_

---

### 28. [E (mathematical constant)](https://en.wikipedia.org/wiki/E_%28mathematical_constant%29) · GA/Top · _pilot_ · primary=`Real.exp`

> Mathlib has no dedicated `e` constant; `e` is `Real.exp 1`, with numerical bounds in ExponentialBounds and Euler's identity via `Complex.exp_pi_mul_I`.

| Mark | Decl | Conf | Source |
|---|---|---|---|
| `_` | `Real.exp` | high | [Exponential](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Analysis/Complex/Exponential.lean#L78) |
| `_` | `Complex.exp` | high | [Exponential](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Analysis/Complex/Exponential.lean#L60) |
| `_` | `Real.exp_one_near_20` | high | [ExponentialBounds](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Analysis/Complex/ExponentialBounds.lean#L28) |
| `_` | `Real.exp_one_gt_d9` | high | [ExponentialBounds](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Analysis/Complex/ExponentialBounds.lean#L34) |
| `_` | `Complex.exp_pi_mul_I` | high | [Basic](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Analysis/SpecialFunctions/Trigonometric/Basic.lean#L1209) |

**Article-level verdict** (does Mathlib formalize this concept?): `_`  _(Y=fully · P=partial · N=no · ?=unclear)_

---

### 29. [Diffeomorphism](https://en.wikipedia.org/wiki/Diffeomorphism) · C/High · _tier2_ · primary=`Diffeomorph`

> Diffeomorph M M' bundles an equivalence with C^n smoothness in both directions with respect to model spaces I, I'.

| Mark | Decl | Conf | Source |
|---|---|---|---|
| `Y` | `Diffeomorph` | high | [Diffeomorph](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Geometry/Manifold/Diffeomorph.lean#L81) |
| `Y` | `IsLocalDiffeomorph` | high | [LocalDiffeomorph](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Geometry/Manifold/LocalDiffeomorph.lean#L251) |
| `Y` | `IsLocalDiffeomorphAt` | high | [LocalDiffeomorph](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Geometry/Manifold/LocalDiffeomorph.lean#L142) |
| `Y` | `PartialDiffeomorph` | high | [LocalDiffeomorph](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Geometry/Manifold/LocalDiffeomorph.lean#L77) |

**Article-level verdict** (does Mathlib formalize this concept?): `Y`  _(Y=fully · P=partial · N=no · ?=unclear)_

---

### 30. [Universal property](https://en.wikipedia.org/wiki/Universal_property) · C/High · _tier2_ · primary=`IsInitial`

> Mathlib has no single 'UniversalProperty' decl; universal morphisms are formalized as initial/terminal objects of (co)structured arrow (comma) categories, with adjunctions packaging the universal property of left/right adjoint functors.

| Mark | Decl | Conf | Source |
|---|---|---|---|
| `Y` | `IsInitial` | high | [IsTerminal](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/CategoryTheory/Limits/Shapes/IsTerminal.lean#L59) |
| `Y` | `IsTerminal` | high | [IsTerminal](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/CategoryTheory/Limits/Shapes/IsTerminal.lean#L55) |
| `Y` | `StructuredArrow` | high | [Basic](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/CategoryTheory/Comma/StructuredArrow/Basic.lean#L40) |
| `Y` | `CostructuredArrow` | high | [Basic](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/CategoryTheory/Comma/StructuredArrow/Basic.lean#L422) |
| `Y` | `Adjunction` | medium | [Basic](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/CategoryTheory/Adjunction/Basic.lean#L109) |

**Article-level verdict** (does Mathlib formalize this concept?): `Y`  _(Y=fully · P=partial · N=no · ?=unclear)_

---

### 31. [Elementary algebra](https://en.wikipedia.org/wiki/Elementary_algebra) · B/Top · _pilot_ · NO MATCH (reason: _too elementary_)

> Elementary algebra is a pedagogical umbrella topic (variables, expressions, basic equation solving over ℝ/ℂ); Mathlib formalizes its constituent structures (e.g. CommRing, Field) but has no single declaration corresponding to 'elementary algebra' itself.

**Do you agree there's no Mathlib formalization?** `_`  _(Y=agree · N=disagree, formalization exists · ?=unclear)_

---

### 32. [Line (geometry)](https://en.wikipedia.org/wiki/Line_%28geometry%29) · C/Top · _tier2_ · primary=`AffineMap.lineMap`

> Mathlib has no standalone `Line` type; a Euclidean line is the image of `lineMap` or equivalently the 1-dimensional `affineSpan` of two distinct points, with `Collinear` expressing the predicate.

| Mark | Decl | Conf | Source |
|---|---|---|---|
| `Y` | `AffineMap.lineMap` | high | [AffineMap](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/LinearAlgebra/AffineSpace/AffineMap.lean#L482) |
| `Y` | `affineSpan` | high | [Defs](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/LinearAlgebra/AffineSpace/AffineSubspace/Defs.lean#L422) |
| `Y` | `Collinear` | high | [FiniteDimensional](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/LinearAlgebra/AffineSpace/FiniteDimensional.lean#L402) |

**Article-level verdict** (does Mathlib formalize this concept?): `Y`  _(Y=fully · P=partial · N=no · ?=unclear)_

---

### 33. [Prime ideal](https://en.wikipedia.org/wiki/Prime_ideal) · C/High · _tier2_ · primary=`Ideal.IsPrime`

> Ideal.IsPrime is the central definition; PrimeSpectrum packages prime ideals as a type.

| Mark | Decl | Conf | Source |
|---|---|---|---|
| `Y` | `Ideal.IsPrime` | high | [Prime](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/RingTheory/Ideal/Prime.lean#L40) |
| `Y` | `Ideal.isPrime_iff` | high | [Prime](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/RingTheory/Ideal/Prime.lean#L46) |
| `Y` | `Ideal.primeCompl` | high | [Prime](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/RingTheory/Ideal/Prime.lean#L116) |
| `Y` | `PrimeSpectrum` | high | [Defs](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/RingTheory/Spectrum/Prime/Defs.lean#L34) |
| `Y` | `Ideal.IsPrime.mem_or_mem` | high | [Prime](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/RingTheory/Ideal/Prime.lean#L61) |

**Article-level verdict** (does Mathlib formalize this concept?): `Y`  _(Y=fully · P=partial · N=no · ?=unclear)_

---

### 34. [Tangent bundle](https://en.wikipedia.org/wiki/Tangent_bundle) · B/High · _pilot_ · primary=`TangentBundle`

> TangentBundle is defined as the Bundle.TotalSpace of the per-point TangentSpace fibers, with FiberBundle/VectorBundle structure supplied via tangentBundleCore.

| Mark | Decl | Conf | Source |
|---|---|---|---|
| `_` | `TangentBundle` | high | [Basic](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Geometry/Manifold/IsManifold/Basic.lean#L1073) |
| `_` | `TangentSpace` | high | [Basic](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Geometry/Manifold/IsManifold/Basic.lean#L1041) |
| `_` | `tangentBundleCore` | high | [Tangent](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Geometry/Manifold/VectorBundle/Tangent.lean#L87) |
| `_` | `TangentSpace.fiberBundle` | high | [Tangent](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Geometry/Manifold/VectorBundle/Tangent.lean#L185) |
| `_` | `TangentSpace.vectorBundle` | high | [Tangent](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Geometry/Manifold/VectorBundle/Tangent.lean#L188) |

**Article-level verdict** (does Mathlib formalize this concept?): `_`  _(Y=fully · P=partial · N=no · ?=unclear)_

---

### 35. [Green's function](https://en.wikipedia.org/wiki/Green%27s_function) · B/Mid · _tier2_ · NO MATCH (reason: _not formalized_)

> No Green's function or fundamental solution of a linear differential operator is formalized in Mathlib; only incidental color-word matches appear.

**Do you agree there's no Mathlib formalization?** `Y`  _(Y=agree · N=disagree, formalization exists · ?=unclear)_

---

### 36. [Stationary point](https://en.wikipedia.org/wiki/Stationary_point) · B/High · _pilot_ · primary=`IsLocalExtr.hasFDerivAt_eq_zero`

> Mathlib has no standalone predicate for `IsStationaryPoint`/`IsCriticalPoint`; the closest formalization is Fermat's theorem stating that the derivative vanishes at local extrema.

| Mark | Decl | Conf | Source |
|---|---|---|---|
| `_` | `IsLocalExtr.hasFDerivAt_eq_zero` | medium | [Basic](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Analysis/Calculus/LocalExtr/Basic.lean#L197) |
| `_` | `IsLocalExtr.deriv_eq_zero` | medium | [Basic](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Analysis/Calculus/LocalExtr/Basic.lean#L261) |
| `_` | `IsLocalMax.hasFDerivAt_eq_zero` | medium | [Basic](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Analysis/Calculus/LocalExtr/Basic.lean#L187) |
| `_` | `IsLocalMin.hasFDerivAt_eq_zero` | medium | [Basic](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Analysis/Calculus/LocalExtr/Basic.lean#L174) |

**Article-level verdict** (does Mathlib formalize this concept?): `_`  _(Y=fully · P=partial · N=no · ?=unclear)_

---

### 37. [Picard–Lindelöf theorem](https://en.wikipedia.org/wiki/Picard%E2%80%93Lindel%C3%B6f_theorem) · C/High · _tier2_ · primary=`IsPicardLindelof.exists_eq_forall_mem_Icc_hasDerivWithinAt`

> Mathlib formalizes Picard-Lindelöf in Mathlib.Analysis.ODE.PicardLindelof, via the `IsPicardLindelof` hypothesis bundle, the Picard iteration `ODE.picard`, and existence theorems in integral and differential forms.

| Mark | Decl | Conf | Source |
|---|---|---|---|
| `Y` | `IsPicardLindelof` | high | [PicardLindelof](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Analysis/ODE/PicardLindelof.lean#L83) |
| `Y` | `IsPicardLindelof.exists_eq_forall_mem_Icc_hasDerivWithinAt` | high | [PicardLindelof](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Analysis/ODE/PicardLindelof.lean#L734) |
| `Y` | `IsPicardLindelof.exists_eq_forall_mem_Icc_eq_picard` | high | [PicardLindelof](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Analysis/ODE/PicardLindelof.lean#L724) |
| `Y` | `ContDiffAt.exists_forall_mem_closedBall_exists_eq_forall_mem_Ioo_hasDerivAt` | high | [PicardLindelof](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Analysis/ODE/PicardLindelof.lean#L822) |
| `Y` | `ODE.picard` | high | [PicardLindelof](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Analysis/ODE/PicardLindelof.lean#L109) |

**Article-level verdict** (does Mathlib formalize this concept?): `Y`  _(Y=fully · P=partial · N=no · ?=unclear)_

---

### 38. [Bijection, injection and surjection](https://en.wikipedia.org/wiki/Bijection%2C_injection_and_surjection) · B/High · _pilot_ · primary=`Function.Bijective`

> Function.Injective and Function.Surjective themselves are defined in core Lean (Init/Logic), not Mathlib; Function.Bijective and the bundled bijection type Equiv are the Mathlib-side formalizations.

| Mark | Decl | Conf | Source |
|---|---|---|---|
| `_` | `Function.Bijective` | high | [Defs](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Logic/Function/Defs.lean#L69) |
| `_` | `Equiv` | high | [Defs](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Logic/Equiv/Defs.lean#L69) |

**Article-level verdict** (does Mathlib formalize this concept?): `_`  _(Y=fully · P=partial · N=no · ?=unclear)_

---

### 39. [Commutative ring](https://en.wikipedia.org/wiki/Commutative_ring) · B/Top · _pilot_ · primary=`CommRing`

> CommRing is the central typeclass; CommSemiring is the unital semiring variant and CommRingCat is the category of commutative rings.

| Mark | Decl | Conf | Source |
|---|---|---|---|
| `_` | `CommRing` | high | [Defs](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Algebra/Ring/Defs.lean#L406) |
| `_` | `CommSemiring` | high | [Defs](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Algebra/Ring/Defs.lean#L222) |
| `_` | `CommRingCat` | high | [Basic](https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Algebra/Category/Ring/Basic.lean#L545) |

**Article-level verdict** (does Mathlib formalize this concept?): `_`  _(Y=fully · P=partial · N=no · ?=unclear)_

---

### 40. [Prisoner's dilemma](https://en.wikipedia.org/wiki/Prisoner%27s_dilemma) · C/High · _tier2_ · NO MATCH (reason: _not formalized_)

> Mathlib has no game-theory formalization of the prisoner's dilemma, Nash equilibria, or normal-form games; only `Mathlib/Order/GameAdd.lean` exists and it concerns well-founded relations, not strategic games.

**Do you agree there's no Mathlib formalization?** `Y`  _(Y=agree · N=disagree, formalization exists · ?=unclear)_

---
