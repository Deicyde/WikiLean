import Wikifunctions.Python.Imp

/-!
# Z13667 factorial — the executable program (Mathlib-free)

A 1:1 embedding of the deployed Wikifunctions Python for `Z13667` (impl `Z13668`):

```python
def Z13667(Z13667K1):
    k = 1
    for i in range(1, Z13667K1 + 1):
        k *= i
    return k
```

The `for i in range(1, n+1): k *= i` loop desugars to the `while` form
`i = 1; while i <= n: (k, i) = (k * i, i + 1)`, modelled here with `Imp`'s
parallel assignment and the `le` condition. Variables use the deployed names
(`Z13667K1` for the input `n`, plus the locals `k` and `i`). The proof that this
computes `Nat.factorial` lives in `Z13667.lean`.
-/

namespace Wikifunctions.Python

/-- Input variable `Z13667K1` (the argument `n`). -/
def facN : String := "Z13667K1"
/-- Accumulator local `k`. -/
def facK : String := "k"
/-- Loop counter local `i`. -/
def facI : String := "i"

/-- Loop body `k, i = k * i, i + 1`. -/
def facBody : Stmt :=
  .passign facK facI (.mul (.var facK) (.var facI)) (.add (.var facI) (.lit 1))

/-- The whole loop `while i <= Z13667K1: k, i = k * i, i + 1`. -/
def facLoop : Stmt :=
  .while_ (.le (.var facI) (.var facN)) facBody

/-- Initial state `{Z13667K1 := n, k := 1, i := 1}`. -/
def facInit (n : Nat) : State :=
  ((State.set ([] : State) facN n).set facK 1).set facI 1

/-- The full program: run the loop with sufficient fuel (`n + 1`), then return `k`.
This is exactly `def Z13667(n): k=1; for i in range(1,n+1): k*=i; return k`. -/
def runFac (n : Nat) : Option Nat :=
  match facLoop.run (n + 1) (facInit n) with
  | none => none
  | some t => some (State.get t facK)

end Wikifunctions.Python
