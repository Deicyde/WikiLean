import Wikifunctions.Python.Z13667Prog
import Mathlib.Data.Nat.Factorial.Basic
import Mathlib.Tactic

/-!
# Wikifunctions Z13667 "factorial" — verified against Mathlib's `Nat.factorial`

Proves that the **actual** Python implementation deployed on Wikifunctions
(function `Z13667`, implementation `Z13668`) computes exactly Mathlib's
specification `Nat.factorial`.

## The deployed Python

```python
def Z13667(Z13667K1):
    k = 1
    for i in range(1, Z13667K1 + 1):
        k *= i
    return k
```

## What this file does

The imperative-Python deep embedding (AST + fuel-interpreter semantics) lives in
`Wikifunctions.Python.Imp`, and the concrete program value `runFac` is built in
`Wikifunctions.Python.Z13667Prog` as a 1:1 transcription of the Python above
(the `for i in range(1, n+1): k *= i` loop desugared to
`i = 1; while i <= n: (k, i) = (k * i, i + 1)`, using the real Wikifunctions
variable names `Z13667K1`/`k`/`i`).

Here we prove `runFac_eq_factorial`: for **all** `n : ℕ`, running the program from
`{Z13667K1 := n, k := 1, i := 1}` terminates and returns `some (Nat.factorial n)`.

The spec is Mathlib's own `Nat.factorial` (imported); we do **not** define our own
factorial. Correctness is a genuine loop-invariant argument: the loop maintains
`k = (i - 1)!`, so when the loop exits with `i = n + 1` we have `k = n!`. The key
algebraic step is `Nat.mul_factorial_pred` (i.e. `i * (i - 1)! = i!`). Termination
is established by exhibiting sufficient fuel (`n + 1 - i` strictly decreases each
step).

The single trust assumption (modelling CPython on this subset) is documented in
`Imp`. Everything here is proved with no `sorry` and no extra axioms
(`#print axioms` reports only `propext`, `Classical.choice`, `Quot.sound`).
-/

namespace Wikifunctions.Python

/-! ### Variable-distinctness lemmas -/

theorem facK_ne_facI : facK ≠ facI := by decide
theorem facI_ne_facN : facI ≠ facN := by decide
theorem facK_ne_facN : facK ≠ facN := by decide

/-! ### Initial-state read lemmas -/

@[simp] theorem facInit_facI (n : Nat) : facInit n facI = 1 := by
  simp [facInit, State.set]

@[simp] theorem facInit_facK (n : Nat) : facInit n facK = 1 := by
  simp [facInit, State.set, facK_ne_facI]

@[simp] theorem facInit_facN (n : Nat) : facInit n facN = n := by
  simp [facInit, State.set, facK_ne_facN.symm, facI_ne_facN.symm]

/-! ### Loop-body read lemmas -/

/-- The loop body sets `k` to `(old k) * (old i)` (using the *old* values). -/
theorem body_facK (s : State) :
    (doPassign s facK facI (.mul (.var facK) (.var facI)) (.add (.var facI) (.lit 1))) facK
      = s facK * s facI := by
  simp [doPassign, State.set, facK_ne_facI, Expr.eval]

/-- The loop body sets `i` to `(old i) + 1`. -/
theorem body_facI (s : State) :
    (doPassign s facK facI (.mul (.var facK) (.var facI)) (.add (.var facI) (.lit 1))) facI
      = s facI + 1 := by
  simp [doPassign, State.set, Expr.eval]

/-- The loop body leaves `Z13667K1` (the input `n`) unchanged. -/
theorem body_facN (s : State) :
    (doPassign s facK facI (.mul (.var facK) (.var facI)) (.add (.var facI) (.lit 1))) facN
      = s facN := by
  simp [doPassign, State.set, facI_ne_facN.symm, facK_ne_facN.symm, Expr.eval]

/-! ### Loop correctness and termination -/

/-- **Loop correctness and termination.** Carrying the invariant
`s facN = n ∧ 1 ≤ s facI ∧ s facI ≤ n + 1 ∧ s facK = (s facI - 1)!` on the loop
state, together with the fuel bound `n + 1 - s facI < fuel`, the loop terminates
and the final `k` is `n!`. Proved by induction on the fuel; the recursive call is
justified because `n + 1 - i` strictly decreases each step. -/
theorem facLoop_correct (n : Nat) (fuel : Nat) :
    ∀ s : State, s facN = n → 1 ≤ s facI → s facI ≤ n + 1 →
      s facK = Nat.factorial (s facI - 1) → n + 1 - s facI < fuel →
      ∃ t : State, facLoop.run fuel s = some t ∧ t facK = Nat.factorial n := by
  induction fuel with
  | zero => intro s _ _ _ _ hfuel; exact absurd hfuel (Nat.not_lt_zero _)
  | succ m ih =>
    intro s hN hI1 hIn hK hfuel
    by_cases hc : s facI ≤ n
    · -- Loop continues: take one body step.
      set s' := doPassign s facK facI (.mul (.var facK) (.var facI))
        (.add (.var facI) (.lit 1)) with hs'
      have hN' : s' facN = n := by rw [hs', body_facN]; exact hN
      have hI' : s' facI = s facI + 1 := by rw [hs', body_facI]
      have hI1' : 1 ≤ s' facI := by rw [hI']; omega
      have hIn' : s' facI ≤ n + 1 := by rw [hI']; omega
      have hKstep : s' facK = s facK * s facI := by rw [hs', body_facK]
      have hK' : s' facK = Nat.factorial (s' facI - 1) := by
        rw [hKstep, hK, hI']
        rw [show s facI + 1 - 1 = s facI from by omega]
        have hstep : s facI - 1 + 1 = s facI := by omega
        rw [Nat.mul_comm]
        calc s facI * Nat.factorial (s facI - 1)
            = (s facI - 1 + 1) * Nat.factorial (s facI - 1) := by rw [hstep]
          _ = Nat.factorial (s facI - 1 + 1) := (Nat.factorial_succ _).symm
          _ = Nat.factorial (s facI) := by rw [hstep]
      have hfuel' : n + 1 - s' facI < m := by rw [hI']; omega
      obtain ⟨t, hrun, htK⟩ := ih s' hN' hI1' hIn' hK' hfuel'
      refine ⟨t, ?_, htK⟩
      have hcond : (Cond.le (.var facI) (.var facN)).eval s = true := by
        simp [Cond.eval, Expr.eval, hN, hc]
      simp only [facLoop, Stmt.run, hcond, if_true]
      show (match facBody.run m s with
            | none => none
            | some s'' => (Stmt.while_ (.le (.var facI) (.var facN)) facBody).run m s'')
            = some t
      simp only [facBody, Stmt.run]
      rw [← hs']
      exact hrun
    · -- Loop exits: i = n + 1, so k = n!.
      have hIeq : s facI = n + 1 := by omega
      refine ⟨s, ?_, ?_⟩
      · have hcond : (Cond.le (.var facI) (.var facN)).eval s = false := by
          simp [Cond.eval, Expr.eval, hN, hc]
        simp only [facLoop, Stmt.run, hcond, Bool.false_eq_true, if_false]
      · rw [hK, hIeq]
        simp

/-! ### Main theorem -/

/-- **Main theorem.** For all `n : ℕ`, the embedded Python program terminates and
returns `some (Nat.factorial n)`, with `Nat.factorial` being Mathlib's own. -/
theorem runFac_eq_factorial (n : Nat) : runFac n = some (Nat.factorial n) := by
  obtain ⟨t, hrun, htK⟩ :=
    facLoop_correct n (n + 1) (facInit n)
      (facInit_facN n) (by simp) (by simp) (by simp) (by simp)
  simp only [runFac, hrun, htK]

/-! ### Executable checks reproducing the Wikifunctions testers -/

-- `Z13667(0) = 1`, via the theorem (`0! = 1`).
example : runFac 0 = some 1 := by rw [runFac_eq_factorial]; rfl
-- `Z13667(5) = 120`, via the theorem (`5! = 120`).
example : runFac 5 = some 120 := by rw [runFac_eq_factorial]; rfl
-- `Z13667(6) = 720`, via the theorem (`6! = 720`).
example : runFac 6 = some 720 := by rw [runFac_eq_factorial]; rfl

-- Direct evaluation of the operational semantics (the interpreter actually runs):
-- expected outputs: `some 1`, `some 1`, `some 120`, `some 3628800`.
#eval runFac 0
#eval runFac 1
#eval runFac 5
#eval runFac 10

end Wikifunctions.Python

#print axioms Wikifunctions.Python.runFac_eq_factorial
