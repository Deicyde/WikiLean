# lean.py in-process check — real CPython vs the Lean spec

The strongest form of "does the deployed Python match the spec": this runs the
**actual Wikifunctions `Z13701` Python inside real CPython**, loaded into a Lean
process by [lean.py](https://github.com/BasisResearch/lean.py), and compares its
output to the Lean spec `Nat.gcd a b == 1` — which our proof
(`../../lean/Wikifunctions/Python/Z13701.lean`) shows equals our embedding and
Mathlib's `Nat.Coprime`.

Unlike the cross-process harness (`../difftest.py`, which compares our *compiled
Lean embedding* to CPython) and the deductive proof (which assumes our Lean
semantics models CPython), this check has **no semantics-model assumption at all**
— it executes the genuine CPython interpreter and checks it against the formal
spec directly, in one process.

## Result

`tested 1607 cases, 0 mismatches (real CPython Z13701 vs Nat.gcd a b == 1)` —
all small pairs (0..39)² plus adversarial cases (Fibonacci worst case, twin
primes, edges) up to 10⁶.

## Build & run

This is an **isolated** Lean project (its own toolchain — Lean **4.29.1**, what
lean.py pins — separate from the Mathlib-4.31 project, and pulling Pantograph):

```bash
cd wikifunctions/native/leanpy
lake build leanpycheck        # heavy first time: builds Pantograph + LeanPy + bridge
LEANPY_LIBPYTHON=/Library/Frameworks/Python.framework/Versions/3.14/lib/libpython3.14.dylib \
  ./.lake/build/bin/leanpycheck
```

`LEANPY_LIBPYTHON` must point at a `libpython3.x.dylib` (lean.py `dlopen`s it at
runtime). Find yours with:
`python3 -c "import sysconfig,os;print(os.path.join(sysconfig.get_config_var('LIBDIR'),'libpython'+sysconfig.get_config_var('VERSION')+'.dylib'))"`.

## Notes / limitations

- lean.py's integer bridge is int64 via `Py.ofInt`; inputs around 2⁴⁰ tripped an
  "Int out of int64 range" error, so cases here are kept ≤ 10⁶. (The deductive
  proof and the cross-process harness cover arbitrarily large inputs.)
- `.lake/` (Pantograph build + the ~124 MB binary) is gitignored; `lake build`
  regenerates it. `lake-manifest.json` is committed to pin the exact
  LeanPy/Pantograph/Regex revisions.
