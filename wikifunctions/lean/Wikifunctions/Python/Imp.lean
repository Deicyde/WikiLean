/-!
# `Imp` — a deep embedding of the imperative Python subset

Shared operational-semantics core for verifying Wikifunctions' **native
(imperative) Python** implementations against Mathlib specifications. It defines:

* a variable store `State := String → Nat`;
* expressions `Expr` (variable / nat literal / `%`), boolean conditions `Cond`
  (`e != 0`), and statements `Stmt` (a *parallel* assignment, a `while` loop, and
  sequencing); and
* a fuel-interpreter operational semantics `Stmt.run`.

Per-function proofs (e.g. `Wikifunctions/Python/Z13701.lean`) `import` this module
and reason about one specific program built from these constructors.

## Trust assumption

Any proof built on this file inherits a single modelling assumption: that these
definitions faithfully model CPython's behaviour on the modelled subset — in
particular Python's `while`, `%` on non-negative integers, `!= 0`, and
simultaneous tuple assignment (`a, b = b, a % b` evaluates the whole right-hand
side before updating either target). This is the irreducible language-semantics
gap of verifying real code; it can be checked empirically by running the real
CPython implementation against the same Mathlib oracle (e.g. via lean.py).
-/

namespace Wikifunctions.Python

/-- Program variables are named by strings: a faithful `String → Nat` variable store. -/
abbrev State := String → Nat

/-- Update one variable in a state. -/
def State.set (s : State) (x : String) (v : Nat) : State :=
  fun y => if y = x then v else s y

/-- Expressions of the imperative Python subset. -/
inductive Expr where
  | var (x : String)        -- a variable reference
  | lit (n : Nat)           -- a nat literal
  | add (e₁ e₂ : Expr)      -- the `+` operator
  | mul (e₁ e₂ : Expr)      -- the `*` operator
  | mod (e₁ e₂ : Expr)      -- the `%` operator
  deriving Repr

/-- Boolean conditions: `e != 0` (used by `while … != 0`) and `e₁ <= e₂`
    (used by a `for i in range(...)` desugared to `while i <= n`). -/
inductive Cond where
  | ne0 (e : Expr)          -- `e != 0`
  | le (e₁ e₂ : Expr)       -- `e₁ <= e₂`
  deriving Repr

/-- Statements: a *parallel* (simultaneous) assignment, a `while` loop, and sequencing. -/
inductive Stmt where
  /-- `x₁, x₂ = e₁, e₂`: evaluate BOTH right-hand sides in the current state, then
  update both targets simultaneously (a faithful swap, not a sequential assignment). -/
  | passign (x₁ x₂ : String) (e₁ e₂ : Expr)
  | while_ (c : Cond) (body : Stmt)
  | seq (s₁ s₂ : Stmt)
  deriving Repr

/-- Expression evaluation. -/
def Expr.eval (s : State) : Expr → Nat
  | .var x => s x
  | .lit n => n
  | .add e₁ e₂ => (e₁.eval s) + (e₂.eval s)
  | .mul e₁ e₂ => (e₁.eval s) * (e₂.eval s)
  | .mod e₁ e₂ => (e₁.eval s) % (e₂.eval s)

/-- Condition evaluation. -/
def Cond.eval (s : State) : Cond → Bool
  | .ne0 e => e.eval s ≠ 0
  | .le e₁ e₂ => e₁.eval s ≤ e₂.eval s

/-- Semantics of the parallel assignment `x₁, x₂ = e₁, e₂`: both right-hand sides
are evaluated in the *current* state `s`, then both targets are updated
simultaneously. -/
def doPassign (s : State) (x₁ x₂ : String) (e₁ e₂ : Expr) : State :=
  let v₁ := e₁.eval s
  let v₂ := e₂.eval s
  (s.set x₁ v₁).set x₂ v₂

/-- Fuel interpreter giving the operational semantics of statements.
`none` means the loop ran out of fuel (did not terminate within the budget). -/
def Stmt.run (fuel : Nat) (s : State) : Stmt → Option State
  | .passign x₁ x₂ e₁ e₂ => some (doPassign s x₁ x₂ e₁ e₂)
  | .seq a b =>
      match a.run fuel s with
      | none => none
      | some s' => b.run fuel s'
  | .while_ c body =>
      match fuel with
      | 0 => none
      | fuel + 1 =>
          if c.eval s then
            match body.run fuel s with
            | none => none
            | some s' => (Stmt.while_ c body).run fuel s'
          else
            some s

end Wikifunctions.Python
