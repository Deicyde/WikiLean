import Mathlib.NumberTheory.Divisors
import Mathlib.Data.Nat.Choose.Basic
import Mathlib.Data.Nat.GCD.Basic
import Mathlib.Data.Rat.Defs
import Mathlib.Tactic

/-!
# A verified evaluator for the Wikifunctions composite layer

A deep embedding of the Wikifunctions **composition** language — the `Z7`
function-call / `Z18` argument-reference model that composite implementations
(`Z14K2`) are built from — with a total evaluator, and end-to-end correctness
proofs for the real composite implementations of several Wikifunctions, each
against its Mathlib specification.

## What is modelled

`Val` is the value universe (`Z13518` naturals, `Z40` booleans, `Z70`-style
rationals); `Leaf` is a library of primitive functions, each given a
**Mathlib-backed denotation** in `Leaf.eval`; `Expr` is the composition AST.
The evaluator `eval`/`evalArgs` is a total, mutually-structural recursion.

## The compositional-correctness story

Each `Leaf.eval` clause *is* that leaf's specification. A composite is proved
correct **relative to its leaves**: `eval (composite) = <Mathlib oracle>`. The
primitive-arithmetic leaves (`mul`, `div`, `add`, `dec`, `natEq`, `ite`,
`ratMul`, `ratDiv`) are correct by construction (they are the Lean operations);
the structured leaves (`gcd`, `choose`, `sumProperDivisors`) *denote* their
Mathlib operation, and the correctness of the Wikifunctions sub-functions they
stand for is a separate obligation. For the flat composites below every leaf is
either primitive or denotes a named Mathlib decl, so the composite inherits a
complete proof.

## Scope: flat composites vs. the frontier

The composites proved here are *flat* — finite compositions with no recursion.
The remaining provable-tier Wikifunctions use **recursion or higher-order
combinators** in their real composite bodies and are out of reach of this
first-order embedding (their actual `Z14K2` ASTs):

* `Z13612` gcd — `If(equal(min a b, 0), max a b, gcd …)` (Euclidean recursion)
* `Z13667` factorial — `If(equal(n,0), 1, n * factorial(dec n))` (self-recursion)
* `Z18194` powerset — `If(isEmpty l, [[]], concat(powerset …, …))` (self-recursion)
* `Z13955` totient — `count(true, map(coprime n, range n))` (higher-order)
* `Z13835` fib, `Z13822` modular-inverse, `Z28925` Pythagorean-triple — delegate
  to recursive/list helpers (sort, range, k-bonacci).

Extending the embedding with bounded recursion (fuel) or a fold combinator is
the natural next step; the flat layer below is the foundation it builds on.

This file builds against Mathlib with zero `sorry` and no axiom cheats.
-/

namespace Wikifunctions

/-- Wikifunctions values: `Z13518` naturals, `Z40` booleans, and rationals. -/
inductive Val
  | nat  : Nat → Val
  | bool : Bool → Val
  | rat  : ℚ → Val
deriving DecidableEq

/-- A library of leaf (primitive / sub-function) operations, addressed loosely by
    role. Each is given a Mathlib-backed denotation in `Leaf.eval`. -/
inductive Leaf
  | gcd                -- Z13612  greatest common divisor      ↦ Nat.gcd
  | natEq              -- Z13522  equality of natural numbers   ↦ decide (· = ·)
  | mul                -- multiply two natural numbers          ↦ (· * ·)
  | div                -- divide natural numbers (floor)        ↦ (· / ·)
  | add                -- add two natural numbers               ↦ (· + ·)
  | dec                -- decrement natural number by one       ↦ (· - 1)
  | ite                -- If(cond, then, else)                  ↦ if · then · else ·
  | choose             -- binomial coefficient                  ↦ Nat.choose
  | sumProperDivisors  -- sum of proper divisors               ↦ ∑ i ∈ properDivisors ·
  | ratMul             -- multiply rationals                    ↦ (· * ·)
  | ratDiv             -- divide rationals                      ↦ (· / ·)
deriving DecidableEq

/-- The composition language: `Z18` argument references, value literals, and
    `Z7` calls of a leaf to argument expressions. -/
inductive Expr
  | arg  : Nat → Expr
  | lit  : Val → Expr
  | call : Leaf → List Expr → Expr

/-- Denotation of each leaf — `Option`-valued because arguments may be ill-typed.
    Each clause is the leaf's Mathlib-backed specification. -/
def Leaf.eval : Leaf → List Val → Option Val
  | .gcd,               [.nat a, .nat b] => some (.nat (Nat.gcd a b))
  | .natEq,             [.nat a, .nat b] => some (.bool (decide (a = b)))
  | .mul,               [.nat a, .nat b] => some (.nat (a * b))
  | .div,               [.nat a, .nat b] => some (.nat (a / b))
  | .add,               [.nat a, .nat b] => some (.nat (a + b))
  | .dec,               [.nat a]         => some (.nat (a - 1))
  | .ite,               [.bool c, t, e]  => some (if c then t else e)
  | .choose,            [.nat n, .nat k] => some (.nat (Nat.choose n k))
  | .sumProperDivisors, [.nat n]         => some (.nat (∑ i ∈ Nat.properDivisors n, i))
  | .ratMul,            [.rat a, .rat b] => some (.rat (a * b))
  | .ratDiv,            [.rat a, .rat b] => some (.rat (a / b))
  | _,                  _                => none

/- The evaluator over an environment (the argument list). Total: returns `none`
   on a type error or an out-of-range argument reference. `eval`/`evalArgs` are a
   mutually-structural recursion over `Expr` / `List Expr`. -/
mutual
  def eval (env : List Val) : Expr → Option Val
    | .arg i     => env[i]?
    | .lit v     => some v
    | .call f es => (evalArgs env es).bind f.eval
  def evalArgs (env : List Val) : List Expr → Option (List Val)
    | []      => some []
    | e :: es => (eval env e).bind fun v => (evalArgs env es).bind fun vs => some (v :: vs)
end

/- A `simp` set that unfolds the evaluator on a closed composite applied to a
   concrete environment. -/
attribute [local simp] eval evalArgs Leaf.eval

/-! ## The composites (real `Z14K2` bodies) and their correctness -/

/-- `Z13701` "are coprime" — `equals( gcd(K₀, K₁), 1 )`. -/
def coprimeComposite : Expr :=
  .call .natEq [.call .gcd [.arg 0, .arg 1], .lit (.nat 1)]

/-- The composite computes exactly Mathlib's `Nat.Coprime` (reducibly `gcd = 1`). -/
theorem coprimeComposite_correct (a b : Nat) :
    eval [.nat a, .nat b] coprimeComposite = some (.bool (decide (Nat.Coprime a b))) := by
  simp [coprimeComposite, Option.bind]

theorem coprimeComposite_true_iff (a b : Nat) :
    eval [.nat a, .nat b] coprimeComposite = some (.bool true) ↔ Nat.Coprime a b := by
  rw [coprimeComposite_correct]; simp

/-- `Z13660` "least common multiple" — `divide( multiply(K₀, K₁), gcd(K₀, K₁) )`. -/
def lcmComposite : Expr :=
  .call .div [.call .mul [.arg 0, .arg 1], .call .gcd [.arg 0, .arg 1]]

/-- The composite computes exactly `Nat.lcm` (defined as `a * b / gcd a b`). -/
theorem lcmComposite_correct (a b : Nat) :
    eval [.nat a, .nat b] lcmComposite = some (.nat (Nat.lcm a b)) := by
  simp [lcmComposite, Option.bind, Nat.lcm]

/-- `Z15849` "Kronecker delta" — `If( equals(K₀, K₁), 1, 0 )`. -/
def kroneckerComposite : Expr :=
  .call .ite [.call .natEq [.arg 0, .arg 1], .lit (.nat 1), .lit (.nat 0)]

/-- The composite computes `if i = j then 1 else 0` — the `Matrix.one_apply` /
    Kronecker-delta entry. -/
theorem kroneckerComposite_correct (i j : Nat) :
    eval [.nat i, .nat j] kroneckerComposite = some (.nat (if i = j then 1 else 0)) := by
  simp only [kroneckerComposite, eval, evalArgs, Leaf.eval, Option.bind,
    List.getElem?_cons_zero, List.getElem?_cons_succ]
  by_cases h : i = j <;> simp [h]

/-- `Z15483` "nth r-simplex number" — `binomial( add(K₀, dec(K₁)), K₁ )`. -/
def simplexComposite : Expr :=
  .call .choose [.call .add [.arg 0, .call .dec [.arg 1]], .arg 1]

/-- The composite computes the multiset/figurate number `C(n + (r-1), r)`
    (`= Nat.multichoose n r`). -/
theorem simplexComposite_correct (n r : Nat) :
    eval [.nat n, .nat r] simplexComposite = some (.nat (Nat.choose (n + (r - 1)) r)) := by
  simp [simplexComposite, Option.bind]

/-- `Z20000` "Bayes' theorem P(A|B)" — `divide( multiply(P(B|A), P(A)), P(B) )`. -/
def bayesComposite : Expr :=
  .call .ratDiv [.call .ratMul [.arg 0, .arg 1], .arg 2]

/-- The composite computes the posterior `P(B|A)·P(A) / P(B)`. -/
theorem bayesComposite_correct (pBA pA pB : ℚ) :
    eval [.rat pBA, .rat pA, .rat pB] bayesComposite = some (.rat (pBA * pA / pB)) := by
  simp [bayesComposite, Option.bind]

/-- `Nat.Perfect` is decidable: it is definitionally `(∑ properDivisors = n) ∧ 0 < n`,
    a conjunction of decidable props. (Mathlib does not register this instance.) -/
instance (n : ℕ) : Decidable (Nat.Perfect n) :=
  decidable_of_iff ((∑ i ∈ Nat.properDivisors n, i = n) ∧ 0 < n) Iff.rfl

/-- `Z14933` "is perfect number" — `equals( sumProperDivisors(K₀), K₀ )`. -/
def perfectComposite : Expr :=
  .call .natEq [.call .sumProperDivisors [.arg 0], .arg 0]

/-- The composite tests `∑(proper divisors of n) = n`. -/
theorem perfectComposite_correct (n : Nat) :
    eval [.nat n] perfectComposite
      = some (.bool (decide (∑ i ∈ Nat.properDivisors n, i = n))) := by
  simp [perfectComposite, Option.bind]

/-- For `n > 0` the composite agrees with Mathlib's `Nat.Perfect`
    (`∑ properDivisors = n ∧ 0 < n`). The composite omits the positivity guard,
    so they coincide exactly on positive inputs. -/
theorem perfectComposite_perfect (n : Nat) (hn : 0 < n) :
    eval [.nat n] perfectComposite = some (.bool (decide (Nat.Perfect n))) := by
  have h : (∑ i ∈ Nat.properDivisors n, i = n) ↔ Nat.Perfect n := by
    simp [Nat.Perfect, hn]
  rw [perfectComposite_correct]
  simp only [Option.some.injEq, Val.bool.injEq]
  exact decide_eq_decide.mpr h

/-! ## Executable sanity checks (match Wikifunctions' own testers) -/

-- coprime(64, 99) = true   (tester Z13703);  gcd(12,8)=4 ⇒ not coprime
example : eval [.nat 64, .nat 99] coprimeComposite = some (.bool true) := by decide
example : eval [.nat 12, .nat 8]  coprimeComposite = some (.bool false) := by decide
-- lcm(4, 6) = 12
example : eval [.nat 4, .nat 6] lcmComposite = some (.nat 12) := by decide
-- δ(3,3) = 1,  δ(3,4) = 0
example : eval [.nat 3, .nat 3] kroneckerComposite = some (.nat 1) := by decide
example : eval [.nat 3, .nat 4] kroneckerComposite = some (.nat 0) := by decide
-- 3rd triangular-ish: C(5 + (3-1), 3) = C(7,3) = 35
example : eval [.nat 5, .nat 3] simplexComposite = some (.nat (Nat.choose 7 3)) := by decide
-- 6 is perfect (1+2+3 = 6); 8 is not (1+2+4 = 7)
example : eval [.nat 6] perfectComposite = some (.bool true) := by decide
example : eval [.nat 8] perfectComposite = some (.bool false) := by decide

end Wikifunctions
