import Wikifunctions.Python.Imp
import Mathlib.Data.Nat.GCD.Basic
import Mathlib.Tactic

/-!
# Wikifunctions Z13701 "are coprime" — verified against Mathlib's `Nat.Coprime`

Proves that the **actual** Python implementation deployed on Wikifunctions
(function `Z13701`, implementation `Z29182`) computes exactly Mathlib's
specification `Nat.Coprime`.

## The deployed Python

```python
def Z13701(Z13701K1, Z13701K2):
    while Z13701K2 != 0:
        Z13701K1, Z13701K2 = Z13701K2, Z13701K1 % Z13701K2
    return Z13701K1 == 1
```

## What this file does

The imperative-Python deep embedding (AST + fuel-interpreter semantics) lives in
`Wikifunctions.Python.Imp`. Here we:

* build the concrete program value `loop` as **data** — a 1:1 transcription of the
  Python above, using the real Wikifunctions variable names `Z13701K1`/`Z13701K2`;
* prove `runProgram_eq_coprime`: for **all** `a b : ℕ`, running the program from
  `{Z13701K1 := a, Z13701K2 := b}` terminates and its boolean result equals
  `decide (Nat.Coprime a b)`.

The spec is Mathlib's own `Nat.Coprime` (imported); we do **not** define our own
gcd/coprimality. Correctness is a genuine Euclidean-invariant argument
(`gcd_body` via `Nat.gcd_rec`/`Nat.gcd_comm`), and termination is established by
exhibiting sufficient fuel (`Z13701K2` strictly decreases each step, `Nat.mod_lt`).

The single trust assumption (modelling CPython on this subset) is documented in
`Imp`. Everything here is proved with no `sorry` and no extra axioms
(`#print axioms` reports only `propext`, `Classical.choice`, `Quot.sound`).
-/

namespace Wikifunctions.Python

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
  (State.set ([] : State) varA a).set varB b

/-- The two program variable names are distinct. -/
theorem varA_ne_varB : varA ≠ varB := by decide

@[simp] theorem initState_a (a b : Nat) : (initState a b).get varA = a := by
  simp [initState, State.get_set_ne, varA_ne_varB]

@[simp] theorem initState_b (a b : Nat) : (initState a b).get varB = b := by
  simp [initState]

/-- The loop body sets `Z13701K1` to the *old* value of `Z13701K2`. -/
theorem body_a (s : State) :
    (doPassign s varA varB (.var varB) (.mod (.var varA) (.var varB))).get varA
      = s.get varB := by
  simp [doPassign, State.get_set_ne, varA_ne_varB, Expr.eval]

/-- The loop body sets `Z13701K2` to `Z13701K1 % Z13701K2`, using the *old* values
(this is the content of the simultaneous assignment). -/
theorem body_b (s : State) :
    (doPassign s varA varB (.var varB) (.mod (.var varA) (.var varB))).get varB
      = s.get varA % s.get varB := by
  simp [doPassign, Expr.eval]

/-- **The Euclidean invariant.** The loop body preserves `gcd Z13701K1 Z13701K2`:
after `a, b = b, a % b` we have `gcd b (a % b) = gcd a b`. Proved via `Nat.gcd_rec`. -/
theorem gcd_body (s : State) :
    Nat.gcd
      ((doPassign s varA varB (.var varB) (.mod (.var varA) (.var varB))).get varA)
      ((doPassign s varA varB (.var varB) (.mod (.var varA) (.var varB))).get varB)
      = Nat.gcd (s.get varA) (s.get varB) := by
  rw [body_a, body_b]
  rw [Nat.gcd_comm (s.get varA) (s.get varB)]
  conv_rhs => rw [Nat.gcd_rec (s.get varB) (s.get varA)]
  rw [Nat.gcd_comm (s.get varA % s.get varB) (s.get varB)]

/-- **Loop correctness and termination.** With strictly more fuel than the current
value of `Z13701K2`, the loop terminates, and the final `Z13701K1` is `gcd a b`.
Proved by induction on the fuel; the recursive call is justified because `Z13701K2`
strictly decreases (`Nat.mod_lt`). -/
theorem loop_correct (fuel : Nat) :
    ∀ s : State, s.get varB < fuel →
      ∃ t : State, loop.run fuel s = some t ∧
        t.get varA = Nat.gcd (s.get varA) (s.get varB) := by
  induction fuel with
  | zero => intro s hb; exact absurd hb (Nat.not_lt_zero _)
  | succ n ih =>
    intro s hb
    by_cases hc : s.get varB = 0
    · refine ⟨s, ?_, ?_⟩
      · simp only [loop, Stmt.run, Cond.eval, Expr.eval, hc]
        simp
      · rw [hc, Nat.gcd_zero_right]
    · have hpos : 0 < s.get varB := Nat.pos_of_ne_zero hc
      set s' := doPassign s varA varB (.var varB) (.mod (.var varA) (.var varB))
        with hs'
      have hb' : s'.get varB < n := by
        have hsb : s'.get varB = s.get varA % s.get varB := by rw [hs']; exact body_b s
        rw [hsb]
        have hmod : s.get varA % s.get varB < s.get varB := Nat.mod_lt _ hpos
        omega
      obtain ⟨t, hrun, hta⟩ := ih s' hb'
      refine ⟨t, ?_, ?_⟩
      · have hcond : (Cond.ne0 (.var varB)).eval s = true := by
          simp [Cond.eval, Expr.eval, hc]
        simp only [loop, Stmt.run, hcond, if_true]
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
  | some t => some (State.get t varA == 1)

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
