import Wikifunctions.Python.Z13701Prog

/-! Differential-test driver: reads `a b` pairs (one per line) on stdin and prints
    our Lean embedding's result (`runProgram a b`) as `true`/`false`/`none`, one per
    line. Run via `lake env lean --run DiffDriver.lean` from the project dir; the
    companion `../native/difftest.py` feeds the same inputs to the real CPython
    implementation and diffs the two. -/

open Wikifunctions.Python

def main : IO Unit := do
  let input ← (← IO.getStdin).readToEnd
  for line in input.splitOn "\n" do
    match line.splitOn " " with
    | [a, b] =>
      match a.toNat?, b.toNat? with
      | some a, some b =>
        IO.println (match runProgram a b with
                    | some true  => "true"
                    | some false => "false"
                    | none       => "none")
      | _, _ => pure ()
    | _ => pure ()
