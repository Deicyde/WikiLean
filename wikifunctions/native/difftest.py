#!/usr/bin/env python3
"""Differential test: the ACTUAL deployed Wikifunctions Z13701 Python vs. our Lean
embedding (`runProgram` in Wikifunctions/Python/Z13701.lean).

This is the empirical complement to the deductive proof. The proof shows the Lean
embedding computes Mathlib's `Nat.Coprime`; its one assumption is that the Lean
operational semantics models CPython. This harness checks exactly that assumption:
it runs the real Python in CPython and our Lean interpreter on the same inputs and
confirms they agree on every case.

Run:  python3 difftest.py        (exit 0 = all match)
"""
import os, random, subprocess, sys

# ---- The actual deployed Wikifunctions implementation Z29182 of Z13701, verbatim ----
def Z13701(a, b):
    while b != 0:
        a, b = b, a % b
    return a == 1
# -------------------------------------------------------------------------------------

LEAN_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "lean"))


def gen_cases():
    cases = [(a, b) for a in range(0, 8) for b in range(0, 8)]      # all small pairs
    cases += [(1, n) for n in range(0, 40)] + [(n, 1) for n in range(0, 40)]
    cases += [(n, 0) for n in range(0, 40)] + [(0, n) for n in range(0, 40)]
    random.seed(0)
    cases += [(random.randint(0, 10**6), random.randint(0, 10**6)) for _ in range(600)]
    cases += [(2**40, 2**40 - 1), (10**12, 7), (0, 10**9), (10**9, 1), (123456, 789012)]
    return cases


def lean_results(cases):
    inp = "".join(f"{a} {b}\n" for a, b in cases)
    # Run the compiled native driver (`lake build diffdriver`). Native code needs
    # almost no CPU, so it completes even under heavy system load — unlike the
    # interpreted `lean --run`. Self-contained; no LEAN_PATH / Mathlib needed.
    binary = os.path.join(LEAN_DIR, ".lake", "build", "bin", "diffdriver")
    out = subprocess.run(
        [binary], cwd=LEAN_DIR, input=inp, capture_output=True, text=True,
    )
    if out.returncode != 0:
        sys.stderr.write("LEAN DRIVER FAILED:\n" + out.stderr[-3000:] + "\n")
        sys.exit(1)
    # keep only result tokens, robust to any stray Lean info/deprecation lines
    return [t for t in out.stdout.split() if t in ("true", "false", "none")]


def main():
    cases = gen_cases()
    leans = lean_results(cases)
    if len(leans) != len(cases):
        sys.stderr.write(f"count mismatch: lean {len(leans)} vs cases {len(cases)}\n")
        sys.exit(1)
    mismatches = []
    for (a, b), lean in zip(cases, leans):
        py = "true" if Z13701(a, b) else "false"
        if py != lean:
            mismatches.append((a, b, py, lean))
    print(f"cases: {len(cases)}   mismatches: {len(mismatches)}")
    for m in mismatches[:20]:
        print(f"  MISMATCH a={m[0]} b={m[1]}  python={m[2]}  lean={m[3]}")
    if mismatches:
        print("RESULT: our Lean embedding does NOT match CPython — investigate.")
        sys.exit(2)
    print("RESULT: our Lean embedding matches the real CPython implementation on all cases.")


if __name__ == "__main__":
    main()
