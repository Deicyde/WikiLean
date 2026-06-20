import Mathlib.Data.Nat.GCD.Basic
import Mathlib.Tactic

/-!
# Wikifunctions Z13701 "are coprime" — verified against Mathlib's `Nat.Coprime`

This file proves that the **actual** Python implementation deployed on Wikifunctions
(function `Z13701`, implementation `Z29182`) computes exactly Mathlib's specification
`Nat.Coprime`.

## The deployed Python

```python
def Z13701(Z13701K1, Z13701K2):
    while Z13701K2 != 0:
        Z13701K1, Z13701K2 = Z13701K2, Z13701K1 % Z13701K2
    return Z13701K1 == 1
```

## What this is

A **deep embedding** of the imperative Python subset this program uses. We define:

* an AST of expressions (`Expr`: variable / nat literal / `%`), boolean conditions
  (`Cond`: `e != 0`), and statements (`Stmt`: a *parallel* assignment, a `while` loop,
  and sequencing);
* an explicit **operational semantics** over a variable `State := String → Nat`
  (a fuel interpreter `Stmt.run`);
* the concrete program value `loop` that is a 1:1 transcription of the Python above; and
* the theorem `runProgram_eq_coprime`, proved for **all** `a b : ℕ`, that running the
  program from the initial state `{Z13701K1 := a, Z13701K2 := b}` terminates and its
  boolean result equals `decide (Nat.Coprime a b)`.

The embedding is a deep embedding of the *actual source*: `loop` and `loopBody` are data
(an `Stmt`), not a Lean function, and the reader can see the AST mirrors the Python line
for line. The parallel assignment `Z13701K1, Z13701K2 = Z13701K2, Z13701K1 % Z13701K2`
is modelled faithfully by `doPassign`, which evaluates the *entire* right-hand side in
the current state and then updates both variables simultaneously (a true swap, not a
sequential assignment).

The correctness proof is a genuine invariant argument: the Euclidean invariant
`Nat.gcd (s Z13701K1) (s Z13701K2)` is preserved by the loop body (`gcd_body`, via
`Nat.gcd_rec` and `Nat.gcd_comm`), and at loop exit `Z13701K2 = 0` so the invariant
collapses to `Nat.gcd a b` by `Nat.gcd_zero_right`. Coprimality is then read off via
Mathlib's `Nat.coprime_iff_gcd_eq_one`. Termination is established by exhibiting
sufficient fuel: `Z13701K2` strictly decreases each iteration (`Nat.mod_lt`), so
`b + 1` units of fuel always suffice.

The spec is Mathlib's own `Nat.Coprime`; we do **not** define our own gcd or coprimality.

## Trust assumption

The single trust assumption is that the operational semantics in this file (`Stmt.run`,
`doPassign`, `Expr.eval`, `Cond.eval`) faithfully models CPython's behaviour on this
subset of the language — in particular that Python's `while`, `%` on non-negative ints,
`!= 0`, simultaneous tuple assignment, and `== 1` behave as encoded here. Everything
downstream of that modelling is proved in Lean with no `sorry` and no extra axioms
(`#print axioms` reports only `propext`, `Classical.choice`, `Quot.sound`).
-/

namespace Wikifunctions.Python

/-- Program variables are named by strings: a faithful `String → Nat` variable store. -/
abbrev State := String → Nat

/-- Update one variable in a state. -/
def State.set (s : State) (x : String) (v : Nat) : State :=
  fun y => if y = x then v else s y

/-- Expressions of the imperative Python subset used by `Z13701`. -/
inductive Expr where
  | var (x : String)        -- a variable reference, e.g. `Z13701K1`
  | lit (n : Nat)           -- a nat literal, e.g. `0` or `1`
  | mod (e₁ e₂ : Expr)      -- the `%` operator
  deriving Repr

/-- Boolean conditions. Only `e != 0` is needed (`while Z13701K2 != 0`). -/
inductive Cond where
  | ne0 (e : Expr)          -- `e != 0`
  deriving Repr

/-- Statements: a *parallel* (simultaneous) assignment, a `while` loop, and sequencing. -/
inductive Stmt where
  /-- `x₁, x₂ = e₁, e₂`: evaluate BOTH right-hand sides in the current state, then update
  both targets simultaneously (a faithful swap, not a sequential assignment). -/
  | passign (x₁ x₂ : String) (e₁ e₂ : Expr)
  | while_ (c : Cond) (body : Stmt)
  | seq (s₁ s₂ : Stmt)
  deriving Repr

/-- Expression evaluation. -/
def Expr.eval (s : State) : Expr → Nat
  | .var x => s x
  | .lit n => n
  | .mod e₁ e₂ => (e₁.eval s) % (e₂.eval s)

/-- Condition evaluation: `e != 0` is `e.eval s ≠ 0`. -/
def Cond.eval (s : State) : Cond → Bool
  | .ne0 e => e.eval s ≠ 0

/-- Semantics of the parallel assignment `x₁, x₂ = e₁, e₂`: both right-hand sides are
evaluated in the *current* state `s`, then both targets are updated simultaneously. -/
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

/-! ### The concrete program (a 1:1 transcription of the Python) -/

/-- The variable holding `Z13701K1` (the first argument, `a`). -/
def varA : String := "Z13701K1"
/-- The variable holding `Z13701K2` (the second argument, `b`). -/
def varB : String := "Z13701K2"

/-- The loop body `Z13701K1, Z13701K2 = Z13701K2, Z13701K1 % Z13701K2`. -/
def loopBody : Stmt :=
  .passign varA varB (.var varB) (.mod (.var varA) (.var varB))

/-- The whole loop `while Z13701K2 != 0: Z13701K1, Z13701K2 = Z13701K2, Z13701K1 % Z13701K2`. -/
def loop : Stmt :=
  .while_ (.ne0 (.var varB)) loopBody

/-- The initial state `{Z13701K1 := a, Z13701K2 := b}` (all other variables `0`). -/
def initState (a b : Nat) : State :=
  (State.set (fun _ => 0) varA a).set varB b

/-- The two program variable names are distinct. -/
theorem varA_ne_varB : varA ≠ varB := by decide

@[simp] theorem initState_a (a b : Nat) : initState a b varA = a := by
  simp [initState, State.set, varA_ne_varB]

@[simp] theorem initState_b (a b : Nat) : initState a b varB = b := by
  simp [initState, State.set]

/-- The loop body sets `Z13701K1` to the *old* value of `Z13701K2`. -/
theorem body_a (s : State) :
    (doPassign s varA varB (.var varB) (.mod (.var varA) (.var varB))) varA
      = s varB := by
  simp [doPassign, State.set, varA_ne_varB, Expr.eval]

/-- The loop body sets `Z13701K2` to `Z13701K1 % Z13701K2`, using the *old* values
(this is the content of the simultaneous assignment). -/
theorem body_b (s : State) :
    (doPassign s varA varB (.var varB) (.mod (.var varA) (.var varB))) varB
      = s varA % s varB := by
  simp [doPassign, State.set, Expr.eval]

/-- **The Euclidean invariant.** The loop body preserves `gcd Z13701K1 Z13701K2`:
after `a, b = b, a % b` we have `gcd b (a % b) = gcd a b`. Proved via `Nat.gcd_rec`. -/
theorem gcd_body (s : State) :
    Nat.gcd
      ((doPassign s varA varB (.var varB) (.mod (.var varA) (.var varB))) varA)
      ((doPassign s varA varB (.var varB) (.mod (.var varA) (.var varB))) varB)
      = Nat.gcd (s varA) (s varB) := by
  rw [body_a, body_b]
  -- goal: gcd (s b) (s a % s b) = gcd (s a) (s b)
  rw [Nat.gcd_comm (s varA) (s varB)]
  -- goal: gcd (s b) (s a % s b) = gcd (s b) (s a)
  conv_rhs => rw [Nat.gcd_rec (s varB) (s varA)]
  -- rhs becomes gcd (s a % s b) (s b)
  rw [Nat.gcd_comm (s varA % s varB) (s varB)]

/-- **Loop correctness and termination.** With strictly more fuel than the current
value of `Z13701K2`, the loop terminates, and the final `Z13701K1` is `gcd a b`.
Proved by induction on the fuel; the recursive call is justified because `Z13701K2`
strictly decreases (`Nat.mod_lt`). -/
theorem loop_correct (fuel : Nat) :
    ∀ s : State, s varB < fuel →
      ∃ t : State, loop.run fuel s = some t ∧
        t varA = Nat.gcd (s varA) (s varB) := by
  induction fuel with
  | zero => intro s hb; exact absurd hb (Nat.not_lt_zero _)
  | succ n ih =>
    intro s hb
    by_cases hc : s varB = 0
    · -- loop exits immediately: final a = a = gcd a 0
      refine ⟨s, ?_, ?_⟩
      · simp only [loop, Stmt.run, Cond.eval, Expr.eval, hc]
        simp
      · rw [hc, Nat.gcd_zero_right]
    · -- one iteration at fuel `n+1`, then recurse at fuel `n`
      have hpos : 0 < s varB := Nat.pos_of_ne_zero hc
      set s' := doPassign s varA varB (.var varB) (.mod (.var varA) (.var varB))
        with hs'
      -- new `Z13701K2` is `a % b < b ≤ n`, hence `< n`: enough fuel for the recursion
      have hb' : s' varB < n := by
        have hsb : s' varB = s varA % s varB := by rw [hs']; exact body_b s
        rw [hsb]
        have hmod : s varA % s varB < s varB := Nat.mod_lt _ hpos
        omega
      obtain ⟨t, hrun, hta⟩ := ih s' hb'
      refine ⟨t, ?_, ?_⟩
      · -- unfold one step of the while loop at fuel `n+1`
        have hcond : (Cond.ne0 (.var varB)).eval s = true := by
          simp [Cond.eval, Expr.eval, hc]
        simp only [loop, Stmt.run, hcond, if_true]
        -- `loopBody.run n s = some s'`, then the recursive while on `s'`
        show (match loopBody.run n s with
              | none => none
              | some s'' => (Stmt.while_ (.ne0 (.var varB)) loopBody).run n s'')
              = some t
        simp only [loopBody, Stmt.run]
        rw [← hs']
        exact hrun
      · rw [hta, hs', gcd_body]

/-- The full program: run the loop with sufficient fuel (`b + 1`), then return
`Z13701K1 == 1`. This is exactly `def Z13701(a, b): ...; return Z13701K1 == 1`. -/
def runProgram (a b : Nat) : Option Bool :=
  match loop.run (b + 1) (initState a b) with
  | none => none
  | some t => some (t varA == 1)

/-- **Main theorem.** For all `a b : ℕ`, the embedded Python program terminates and its
boolean result equals `decide (Nat.Coprime a b)`, with `Nat.Coprime` being Mathlib's. -/
theorem runProgram_eq_coprime (a b : Nat) :
    runProgram a b = some (decide (Nat.Coprime a b)) := by
  obtain ⟨t, hrun, hta⟩ := loop_correct (b + 1) (initState a b) (by simp)
  simp only [runProgram, hrun, hta, initState_a, initState_b]
  rw [show decide (Nat.Coprime a b) = decide (Nat.gcd a b = 1) from by
        simp [Nat.coprime_iff_gcd_eq_one]]
  by_cases h : Nat.gcd a b = 1 <;> simp [h]

/-- The same statement phrased as an `↔`: the program returns `some true` exactly when
`a` and `b` are coprime. -/
theorem runProgram_true_iff_coprime (a b : Nat) :
    runProgram a b = some true ↔ Nat.Coprime a b := by
  rw [runProgram_eq_coprime]
  by_cases h : Nat.Coprime a b <;> simp [h]

/-! ### Executable checks reproducing the Wikifunctions testers -/

-- `Z13701(64, 99) = True` (coprime).
example : runProgram 64 99 = some true := by rw [runProgram_eq_coprime]; decide
-- `Z13701(12, 8) = False` (gcd 4).
example : runProgram 12 8 = some false := by rw [runProgram_eq_coprime]; decide
-- `Z13701(1, 0) = True` (gcd 1).
example : runProgram 1 0 = some true := by rw [runProgram_eq_coprime]; decide

-- Direct evaluation of the operational semantics (the interpreter actually runs):
-- expected outputs: `some true`, `some false`, `some true`, `some true`.
#eval runProgram 64 99
#eval runProgram 12 8
#eval runProgram 1 0
#eval runProgram 17 5

end Wikifunctions.Python

#print axioms Wikifunctions.Python.runProgram_eq_coprime
