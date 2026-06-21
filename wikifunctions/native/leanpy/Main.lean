import LeanPy

/-!
# In-process check: the REAL CPython Z13701 vs the Lean spec, via lean.py

Runs the **actual deployed Wikifunctions Python** for `Z13701` (impl `Z29182`)
inside CPython — loaded into this Lean process by lean.py — and compares its
result, on many inputs, to `Nat.gcd a b == 1` (core Lean). That spec is exactly
what our verified embedding (`Wikifunctions.Python.runProgram`, proved equal to
Mathlib's `Nat.Coprime`) computes. So a clean run is independent evidence — using
the real interpreter, with no Lean-models-CPython assumption — that the deployed
Python agrees with the formal spec.

Build+run:  lake exe leanpycheck
-/

open LeanPy.Python

/-- The exact deployed Wikifunctions Python for Z13701 (impl Z29182). -/
def z13701Src : String :=
  "def Z13701(a, b):\n    while b != 0:\n        a, b = b, a % b\n    return a == 1\n"

/-- The exact deployed Wikifunctions Python for Z13667 factorial (impl Z13668). -/
def z13667Src : String :=
  "def Z13667(n):\n    k = 1\n    for i in range(1, n + 1):\n        k *= i\n    return k\n"

/-- Factorial spec in core Lean (definitionally Mathlib's `Nat.factorial`). -/
def fact : Nat → Nat
  | 0 => 1
  | n + 1 => (n + 1) * fact n

def main : IO Unit := do
  init ()
  exec z13701Src
  let f ← eval "Z13701"
  let tested ← IO.mkRef (0 : Nat)
  let mismatches ← IO.mkRef (0 : Nat)
  let check : Nat → Nat → IO Unit := fun a b => do
    let r ← f.call #[← Py.ofInt (a : Int), ← Py.ofInt (b : Int)]
    let py ← r.toBool                 -- real CPython result
    let spec := (Nat.gcd a b == 1)    -- what our verified embedding computes
    tested.modify (· + 1)
    if py != spec then
      mismatches.modify (· + 1)
      IO.println s!"MISMATCH a={a} b={b}  python={py}  spec(gcd==1)={spec}"
  -- all small pairs
  for a in [0:40] do
    for b in [0:40] do
      check a b
  -- larger / adversarial cases (Fibonacci worst case, twin primes, edges)
  for (a, b) in [(917299, 533305), (832040, 514229), (1000003, 1000033),
                 (123456, 789012), (1000000, 999999), (0, 1000000),
                 (1000000, 1)] do
    check a b
  IO.println s!"tested {← tested.get} cases, {← mismatches.get} mismatches \
(real CPython Z13701 vs Nat.gcd a b == 1)"
  -- (b) Factorial Z13667: real CPython vs `fact` (= Nat.factorial). n ≤ 20 fits int64.
  exec z13667Src
  let fac ← eval "Z13667"
  let facMis ← IO.mkRef (0 : Nat)
  for n in [0:21] do
    let r ← fac.call #[← Py.ofInt (n : Int)]
    let py ← r.toInt
    let spec := (fact n : Int)
    if py != spec then
      facMis.modify (· + 1)
      IO.println s!"FACT MISMATCH n={n}  python={py}  spec={spec}"
  IO.println s!"factorial: tested 21 cases (n=0..20), {← facMis.get} mismatches \
(real CPython Z13667 vs Nat.factorial)"
