import Wikifunctions.Python.Imp

/-!
# Z13701 — the executable program (Mathlib-free)

The concrete embedded Python program for `Z13701`, as data, with **no Mathlib
dependency** — it needs only the `Imp` core. Kept separate from the proof
(`Z13701.lean`, which imports this plus Mathlib) so that tools which only need to
*run* the embedding — notably the differential-test driver `DiffDriver.lean` — can
load it instantly instead of pulling in Mathlib's full tactic closure.

This is a 1:1 transcription of the deployed Python (impl `Z29182`):

```python
def Z13701(Z13701K1, Z13701K2):
    while Z13701K2 != 0:
        Z13701K1, Z13701K2 = Z13701K2, Z13701K1 % Z13701K2
    return Z13701K1 == 1
```
-/

namespace Wikifunctions.Python

/-- The variable holding `Z13701K1` (the first argument, `a`). -/
def varA : String := "Z13701K1"
/-- The variable holding `Z13701K2` (the second argument, `b`). -/
def varB : String := "Z13701K2"

/-- The loop body `Z13701K1, Z13701K2 = Z13701K2, Z13701K1 % Z13701K2`. -/
def loopBody : Stmt :=
  .passign varA varB (.var varB) (.mod (.var varA) (.var varB))

/-- The whole loop `while Z13701K2 != 0: …`. -/
def loop : Stmt :=
  .while_ (.ne0 (.var varB)) loopBody

/-- The initial state `{Z13701K1 := a, Z13701K2 := b}` (all other variables `0`). -/
def initState (a b : Nat) : State :=
  (State.set (fun _ => 0) varA a).set varB b

/-- The full program: run the loop with sufficient fuel (`b + 1`), then return
`Z13701K1 == 1`. This is exactly `def Z13701(a, b): ...; return Z13701K1 == 1`. -/
def runProgram (a b : Nat) : Option Bool :=
  match loop.run (b + 1) (initState a b) with
  | none => none
  | some t => some (t varA == 1)

end Wikifunctions.Python
