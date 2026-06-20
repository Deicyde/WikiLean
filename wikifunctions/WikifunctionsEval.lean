import Mathlib.Data.Nat.GCD.Basic
import Mathlib.Tactic

/-!
# A verified evaluator for the Wikifunctions composite layer

Proof of concept for *verifying Wikifunctions for real*: a small deep embedding of
the Wikifunctions **composition** language — the `Z7` function-call / `Z18`
argument-reference model that composite implementations (`Z14K2`) are built from —
together with a total evaluator, and an end-to-end correctness proof for the
composite implementation of Wikifunctions `Z13701` "are coprime (natural numbers)"
(Wikidata `Q104752`).

The real composite implementation `Z13702` of `Z13701` is

    Z13522( Z13612(K₁, K₂), 1 )        -- equals( gcd(a, b), 1 )

where `Z13612` is "greatest common divisor" and `Z13522` is "equality of natural
numbers". We give those two leaves Mathlib-backed semantics and prove that the
evaluator, run on this composite, computes exactly Mathlib's `Nat.Coprime`. The
composite therefore *inherits* correctness from (i) the verified leaves and (ii) the
verified evaluator — the compositional verification story.

This file builds against Mathlib with zero `sorry` and no axiom cheats.
-/

namespace Wikifunctions

/-- Wikifunctions values, restricted to the universe this composite needs:
    `Z13518` (natural number) and `Z40` (boolean). -/
inductive Val
  | nat  : Nat → Val
  | bool : Bool → Val
deriving DecidableEq, Repr

/-- Leaf (builtin / native) functions, addressed by their ZID. -/
inductive Leaf
  | gcd     -- Z13612  greatest common divisor
  | natEq   -- Z13522  equality of natural numbers
deriving DecidableEq, Repr

/-- The composition language: `Z18` argument references, value literals, and
    `Z7` calls of a leaf to argument expressions. -/
inductive Expr
  | arg  : Nat → Expr
  | lit  : Val → Expr
  | call : Leaf → List Expr → Expr
deriving Repr

/-- Denotation of each leaf. `Option`-valued because arguments may be ill-typed;
    each clause *is* the leaf's Mathlib-backed specification
    (`gcd ↦ Nat.gcd`, `natEq ↦ decidable equality). -/
def Leaf.eval : Leaf → List Val → Option Val
  | .gcd,   [.nat a, .nat b] => some (.nat (Nat.gcd a b))
  | .natEq, [.nat a, .nat b] => some (.bool (decide (a = b)))
  | _,      _                => none

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

/-- Wikifunctions `Z13701`'s composite implementation `Z13702`:
    `equals( gcd(arg₀, arg₁), 1 )`. -/
def coprimeComposite : Expr :=
  .call .natEq [.call .gcd [.arg 0, .arg 1], .lit (.nat 1)]

/-! ## Leaf correctness (holds by construction) -/

theorem gcd_leaf_spec (a b : Nat) :
    Leaf.gcd.eval [.nat a, .nat b] = some (.nat (Nat.gcd a b)) := rfl

theorem natEq_leaf_spec (a b : Nat) :
    Leaf.natEq.eval [.nat a, .nat b] = some (.bool (decide (a = b))) := rfl

/-! ## Composite correctness: the evaluator computes `Nat.Coprime` -/

/-- **Main theorem.** Evaluating the composite implementation of `Z13701` on two
    naturals yields the boolean `decide (Nat.Coprime a b)`. That is, the composite
    Wikifunction is correct with respect to the Mathlib specification `Nat.Coprime`
    (and hence to the corpus oracle `Z13701_spec`). -/
theorem coprimeComposite_correct (a b : Nat) :
    eval [.nat a, .nat b] coprimeComposite
      = some (.bool (decide (Nat.Coprime a b))) := by
  -- `Nat.Coprime` is reducibly `Nat.gcd a b = 1`, so the evaluator output and the
  -- spec coincide after unfolding the evaluator on this expression.
  simp only [coprimeComposite, eval, evalArgs, Leaf.eval, Option.bind,
    List.getElem?_cons_zero, List.getElem?_cons_succ]

/-- The boolean the evaluator extracts is exactly true iff the inputs are coprime. -/
theorem coprimeComposite_true_iff (a b : Nat) :
    eval [.nat a, .nat b] coprimeComposite = some (.bool true) ↔ Nat.Coprime a b := by
  rw [coprimeComposite_correct]
  simp

/-! ## Executable sanity checks (match Wikifunctions' own testers, e.g. Z13703) -/

-- coprime(64, 99) = true   (this is exactly tester Z13703)
example : eval [.nat 64, .nat 99] coprimeComposite = some (.bool true) := by decide
-- gcd(12, 8) = 4 ≠ 1  → not coprime
example : eval [.nat 12, .nat 8] coprimeComposite = some (.bool false) := by decide
-- coprime(17, 5) = true
example : eval [.nat 17, .nat 5] coprimeComposite = some (.bool true) := by decide

#eval eval [.nat 64, .nat 99] coprimeComposite   -- some (Wikifunctions.Val.bool true)
#eval eval [.nat 12, .nat 8]  coprimeComposite   -- some (Wikifunctions.Val.bool false)

end Wikifunctions
