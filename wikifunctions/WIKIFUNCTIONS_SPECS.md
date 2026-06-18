# Wikifunctions Specs — addressable set & spec corpus

This is the **addressable set**: the Wikifunctions that have a WikiLean Mathlib mapping, enriched with a *computable oracle* (the Mathlib decl or composed expression that produces the ground-truth value), a *faithfulness verdict* (does the mapped Mathlib object actually compute what the Wikifunction returns, or is it the wrong target / a representation mismatch?), and a *Lean spec* that pins each function's defining properties. Every entry was mapped to a Mathlib oracle and adversarially verified; all 25 verdicts are `confirmed`. The companion file `WikifunctionsSpecs.lean` **builds green against Mathlib** (all 25 blocks, zero `sorry`, zero axiom cheats).

**float64 functions use Lean's `Float` type, not `Real`.** A `float64` Wikifunction is an IEEE-754 binary64 routine, so its operational oracle is `Z*_spec : Float → …` (Lean `Float` is binary64, `@[extern]` to libm), which is `#eval`-comparable bit-for-bit to the live function. Each also carries a `noncomputable Z*_ideal : ℝ → …` documenting the exact mathematical function it approximates. This moves the 5 float64 functions (`Z21003`, `Z21005`, `Z12665`, `Z13341`, `Z35278`) out of `spec_only` into `oracle_testable`, and resolves their real-vs-float representation mismatch (now `faithful`). The corpus is the seed for a Lean-backed conformance harness: `composite_provable` + `decidable` functions are kernel-checkable today; `oracle_testable` functions (ℚ/ℤ + Float) are differential-test ground truth; the one remaining `spec_only` function (arbitrary-precision Gamma) specifies exact-ℝ behavior.

## Summary tables (grouped by tier)

### Tier: `composite_provable` (15)

| zid | qid | oracle_decl | computable | faithfulness | verdict |
|---|---|---|---|---|---|
| Z12427 | Q49008 | `decide (Nat.Prime n)` | decidable | faithful | confirmed |
| Z13612 | Q131752 | `Nat.gcd` | computable_nat_int | wrong_target | confirmed |
| Z13660 | Q102761 | `Nat.lcm` | computable_nat_int | wrong_target | confirmed |
| Z13667 | Q120976 | `Nat.factorial` | computable_nat_int | faithful | confirmed |
| Z13701 | Q104752 | `decide (Nat.Coprime m n)` | decidable | faithful | confirmed |
| Z13822 | Q2741788 | `((a : ZMod n)⁻¹).val` (`ZMod.inv`) | computable_nat_int | faithful | confirmed |
| Z13835 | Q23835349 | `Nat.fib` | computable_nat_int | faithful | confirmed |
| Z13955 | Q190026 | `Nat.totient` | computable_nat_int | faithful | confirmed |
| Z14933 | Q170043 | `decide (Nat.Perfect n)` | decidable | faithful | confirmed |
| Z18194 | Q205170 | `Finset.powerset` | computable_nat_int | representation_mismatch | confirmed |
| Z13521 | Q32043 | `Nat.add` | computable_nat_int | wrong_target | confirmed |
| Z15483 | Q331350 | `Nat.choose (n+r-1) r` (`Nat.multichoose`) | computable_nat_int | wrong_target | confirmed |
| Z15849 | Q192826 | `if i = j then 1 else 0` (`Matrix.one_apply`) | computable_nat_int | faithful | confirmed |
| Z20000 | Q182505 | `fun pBA pA pB : ℚ => pBA * pA / pB` | computable_nat_int | representation_mismatch | confirmed |
| Z28925 | Q208225 | `decide (a*a + b*b = c*c)` (`PythagoreanTriple`) | decidable | faithful | confirmed |

### Tier: `oracle_testable` (9)

| zid | qid | oracle (`Z*_spec`) | computable | faithfulness | verdict |
|---|---|---|---|---|---|
| Z30840 | Q19033 | `(l.map Nat.cast).sum / l.length` over ℚ | computable_nat_int | wrong_target | confirmed |
| Z10862 | Q40276 | `*` on ℤ / ℚ (`HMul.hMul`) | computable_nat_int | wrong_target | confirmed |
| Z21917 | Q381040 | `fun p : ℤ×ℤ => (p.1, -p.2)` (`starRingEnd ℂ`) | computable_nat_int | representation_mismatch | confirmed |
| Z31173 | Q852973 | `fun V E F : ℕ => (V:ℤ) - E + F` | computable_nat_int | wrong_target | confirmed |
| Z12665 | Q33456 | `fun x y => x ^ y` on `Float` (ideal `Real.rpow`) | float | faithful | confirmed |
| Z13341 | Q2266329 | `(1-t)*a + t*b` on `Float` (ideal `AffineMap.lineMap`) | float | faithful | confirmed |
| Z21003 | Q204037 | `Float.log` (ideal `Real.log`) | float | faithful | confirmed |
| Z21005 | Q966582 | `Float.log x / Float.log 10` (ideal `Real.logb 10`) | float | faithful | confirmed |
| Z35278 | Q204570 | Float entropy sum (ideal `Real.negMulLog`/nats) | float | faithful | confirmed |

### Tier: `spec_only` (1)

| zid | qid | oracle_decl | computable | faithfulness | verdict |
|---|---|---|---|---|---|
| Z16483 | Q190573 | `Real.Gamma` (WF is arbitrary-precision String I/O) | noncomputable_real | representation_mismatch | confirmed |

## Faithfulness flags

Every function whose `faithfulness != faithful`. For each: the issue and what the correct target should be. Note the recurring pattern — WikiLean's `primary_decl` is frequently a **typeclass** (the abstract algebraic *structure*, not the *operation*), which can never serve as a value oracle; the correct oracle is the concrete computable operation that the typeclass instance is built from.

> **Resolved since this section was written:** the five float64 functions (`Z21003`, `Z21005`, `Z12665`, `Z13341`, `Z35278`) are now graded `faithful` — switching their operational oracle from `Real` to Lean's IEEE-754 `Float` removes the real-vs-float representation gap. The `Real` bullets below remain as the documented *mathematical ideal* each float impl approximates.

### Typeclass / structure `wrong_target` (the mapped decl is an abstraction, not an operation)

- **Z13521 (Q32043, add)** — primary_decl `AddCommMonoid` is the abstract additive-commutative-monoid **typeclass** (`Mathlib/Algebra/Group/Defs.lean:788`), not addition. Correct oracle: **`Nat.add`** (Lean core), which the `AddCommMonoid ℕ` instance is literally built from (`add := Nat.add`). The `(ℕ,ℕ)→ℕ` signature itself is faithful; only the decl was mis-targeted.
- **Z10862 (Q40276, multiply numeric strings)** — primary_decl `Mul` is the multiplication **typeclass** AND is mis-cited (it is Lean core `Init.Prelude`, not `Mathlib.Algebra.Group.Defs`, which only defines `Semigroup extends Mul`). Correct oracle: concrete **`*` (`HMul.hMul`) on ℤ** (or ℚ for the decimal variant). *Compounded* by a representation mismatch: the WF I/O is `(String, String) -> String` decimal-digit encoding (the "(!)" community flag), so verification goes through a parse/render correspondence to the numeric oracle.
- **Z13612 (Q131752, gcd)** — primary_decl `GCDMonoid` is a **typeclass** over `CommMonoidWithZero` (`Basic.lean:246`); it does not compute on concrete ℕ. Correct oracle: **`Nat.gcd`**; the `GCDMonoid ℕ` instance sets `gcd := Nat.gcd` and `gcd_eq_nat_gcd : gcd m n = Nat.gcd m n := rfl`.
- **Z13660 (Q102761, lcm)** — same `GCDMonoid` typeclass mis-target. Correct oracle: **`Nat.lcm`** (Lean core), `Nat.lcm m n = m * n / Nat.gcd m n`. The typeclass projection `GCDMonoid.lcm` would not even defeq `Nat.lcm` on ℕ (routed through `NormalizedGCDMonoid`), so it is the wrong handle.
- **Z15483 (Q331350, nth r-simplex number)** — primary_decl `Affine.Simplex` is a **geometric structure** (n+1 affinely independent points), matched only on the word "simplex." The WF is the figurate/simplicial number. Correct oracle: **`Nat.choose (n + r - 1) r`** ≡ **`Nat.multichoose n r`**. (Mathlib has no dedicated figurate-number def.)
- **Z30840 (Q19033, arithmetic mean → ℚ)** — primary_decl `MeasureTheory.average` is the **noncomputable Bochner-integral** average of a function over a measure — a wholly different object. There is no single Mathlib decl for "arithmetic mean of a list as ℚ"; correct oracle is the **composite** `(l.map Nat.cast).sum / l.length` over ℚ (computable).
- **Z31173 (Q852973, Euler characteristic of polyhedron)** — primary_decl `HomologicalComplex.eulerChar` exists but is the **noncomputable homological-algebra** Euler characteristic of a chain complex (alternating finsum of `Module.finrank`), not the elementary V−E+F. Q852973 is listed *unformalized* in `docs/1000.yaml` (no `decls`). Correct oracle: the **arithmetic expression** `(V:ℤ) - E + F`.

### `wrong_target` where primary_decl is a *correct-concept but wrong-kind* decl

- **Z13701 (Q104752, are coprime)** — `IsCoprime` (the Bezout `∃ a b, a*x+b*y=1` predicate over a CommSemiring) genuinely carries `@[wikidata Q104752]`, but it is a bare `Prop` with **no general Decidable instance** (its own docstring warns it is not a generalization of `Nat.Coprime`). Correct computable oracle: **`Nat.Coprime`** (`= gcd = 1`, `[reducible]`, decidable); for ℕ the two agree via `Nat.isCoprime_iff_coprime`. *(This entry is graded `faithful` in the corpus because the concept and decidable ℕ oracle line up cleanly; flagged here only because the originally-tagged decl needed swapping.)*

### Representation `mismatch` (right concept, wrong encoding)

- **Z18194 (Q205170, powerset)** — `Set.powerset` is the faithful mathematical def but returns `Set (Set α)`, a **non-enumerable Prop-valued predicate** (noncomputable). Correct computable oracle: **`Finset.powerset`** (enumerates subsets; `card_powerset : card = 2^card`).
- **Z20000 (Q182505, Bayes / conditional probability)** — `ProbabilityTheory.cond_eq_inv_mul_cond_mul` is the correct Bayes identity but is the abstract **measure-theoretic** statement over `ℝ≥0∞` inside a `noncomputable section`. The WF returns plain ℚ arithmetic; correct oracle is the **composite** `pBA * pA / pB`. Caveat: WF arg order (likelihood/prior/evidence) must be confirmed against impl test vectors to fix which arg is the divisor.
- **Z21917 (Q381040, complex conjugate of integer pair)** — `starRingEnd ℂ` (= conj) is the right operation, but the WF encodes a complex number as a **ℤ×ℤ Gaussian-integer pair** while Mathlib's ℂ is `ℝ×ℝ` (and noncomputable). Correct computable oracle: **`fun (a,b) => (a, -b)`** on ℤ×ℤ; use `starRingEnd ℂ` only as the spec-level correspondence target.
- **Z16483 (Q190573, gamma function)** — Concept matches `Real.Gamma`/`Complex.Gamma`, but the WF signature is `String -> String` (decimal-string encoding of a transcendental value; "(!)" flag). Oracle `Real.Gamma` is **noncomputable** (Bochner integral); spec only.
- **Z12665 (Q33456, exponentiation)** — *Double flag.* primary_decl `Monoid.npow` is wrong_target (a monoid-to-**natural**-power typeclass field; exponent type ℕ) when the WF is **real**-base^**real**-exponent. Correct oracle: **`Real.rpow`** (noncomputable). Plus a float64↔ℝ representation gap: IEEE `pow` returns NaN for negative base / non-integer exponent, whereas `Real.rpow` uses the `exp(log x · y)` branch convention.
- **Z13341 (Q2266329, linear interpolation)** — `AffineMap.lineMap` is the right (non-typeclass) operation, but the WF is over **float64** vs exact ℝ (noncomputable). `lerp(a,b,t) = (1-t)*a + t*b`.
- **Z21003 (Q204037, natural logarithm)** — `Real.log` is the right operation but **noncomputable** and over exact ℝ vs float64. Totalization also differs: Mathlib's `Real.log` is total (`log 0 = 0`, `log x = log|x|`) whereas the WF natural log is defined only for `x > 0` (likely NaN/error otherwise). Correspondence holds only on `x > 0`. (Note: Mathlib's own tag on `Real.log` is Q11197, not the supplied Q204037 — reconcile.)
- **Z21005 (Q966582, log base 10)** — `Real.logb 10` is the right operation but noncomputable and over exact ℝ vs float64.
- **Z35278 (Q204570, Shannon entropy from string)** — `Real.negMulLog` (`-x log x`) is only the **per-term summand**; the WF returns the full entropy `∑_c negMulLog(count c / len)`. Mathlib has **no** string/list/empirical-distribution Shannon entropy def. Plus a units gap: Mathlib uses natural log (nats); "entropy from string" conventionally reports bits (`Real.logb 2`). Oracle must be assembled and is noncomputable.

## Recommended pilots

The first verification targets should be the cleanest `composite_provable` functions where the oracle is `decidable` (a `Prop` reduced by `decide`) — these are kernel-checkable end to end with zero `sorry`, pure ℕ I/O, and no representation or noncomputability caveats. Pick these five (all graded `faithful`):

1. **Z13701 — are coprime (`decide (Nat.Coprime m n)`).** Reducible def `gcd m n = 1`, fully decidable, plus a verified bridge to the very `IsCoprime`/`@[wikidata]` decl WikiLean tagged. Demonstrates the typeclass-vs-decidable-oracle swap cleanly.
2. **Z12427 — is prime (`decide (Nat.Prime n)`).** The decl already carries `@[wikidata Q49008]` — the exact QID — so the concept match is self-certifying; backed by `Nat.decidablePrime`, kernel-evaluable on concrete ℕ.
3. **Z13667 — factorial (`Nat.factorial`).** Oracle *is* the primary_decl (no correction), structurally recursive, `@[wikidata Q120976]` tagged, with a clean recurrence + product characterization.
4. **Z13835 — nth Fibonacci (`Nat.fib`).** Oracle is the primary_decl, computable, with the defining two-step recurrence and base cases by `rfl`.
5. **Z13955 — Euler totient (`Nat.totient`).** Oracle is the primary_decl (`#{a ∈ range n | n.Coprime a}`), decidable, with `totient_prime` and the coprime-count characterization for property tests.

Two strong runners-up, both `decidable` and `faithful`, that exercise the "concrete-ℕ oracle bridged to a richer Mathlib predicate" pattern: **Z14933 perfect number** (`decide (Nat.Perfect n)`, with the `σ = 2n` reformulation) and **Z28925 is-Pythagorean-triple** (`decide (a²+b²=c²)`, bridged to Mathlib's ℤ-valued `PythagoreanTriple`). Recommended sequencing: land the five decidable-Prop pilots first (they need only `decide` plus one or two named lemmas), then the two bridged predicates, then the arithmetic `composite_provable` set (gcd/lcm/add/multichoose) which additionally needs the typeclass-instance equalities. Defer all `spec_only` (real/transcendental) functions to a separate numeric-tolerance harness — they cannot be kernel-evaluated.

## Counts

**By tier**
- `composite_provable`: 15
- `oracle_testable`: 9 (4 ℚ/ℤ + 5 Float)
- `spec_only`: 1
- **Total: 25**

**By `oracle_computable`**
- `decidable`: 4 (Z12427, Z13701, Z14933, Z28925)
- `computable_nat_int`: 15
- `float`: 5 (Z21003, Z21005, Z12665, Z13341, Z35278)
- `noncomputable_real`: 1 (Z16483)

**By faithfulness**
- `faithful`: 14
- `wrong_target`: 7 (Z13612, Z13660, Z13521, Z15483, Z10862, Z30840, Z31173)
- `representation_mismatch`: 4 (Z18194, Z20000, Z21917, Z16483)

**By verdict**
- `confirmed`: 25
- (no `rejected` / `needs_review`)

**decl_status**
- `exists`: 21
- `wrong_decl_corrected`: 4 (Z10862, Z12665, Z13521, Z15483)
