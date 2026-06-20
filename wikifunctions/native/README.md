# Native-implementation verification (Dafny / Verus)

Deductive verification of Wikifunctions' **imperative implementations** against
specifications derived from Mathlib — the heavyweight, SMT-backed proof track
that complements the Lean composite-evaluator work in `../lean/`.

## Tools (installed locally, 2026-06)

| tool | version | how it's installed | invoke |
|---|---|---|---|
| **Dafny** | 4.11.0 | self-contained release zip → `~/.local/dafny/`, wrapper `~/.local/bin/dafny` (bundles .NET + Z3) | `dafny verify file.dfy` |
| **Verus** | 0.2026.06.14 | release zip → `~/.local/verus-dist/`, wrapper `~/.local/bin/verus`; needs rustup + Rust **1.96.0** (`~/.cargo`) | `verus file.rs` |

Both are on `PATH` (`~/.local/bin`) and run their bundled Z3. To remove: delete
those dirs + the two wrappers (Dafny), and the verus dir + wrapper (Verus;
`rustup self uninstall` removes Rust).

## What's here

- `Z13701_coprime.dfy` — Dafny proof that the Euclidean coprimality loop returns `Gcd(a,b) == 1`.
- `z13701_coprime.rs` — the same algorithm verified in Verus (Rust).

Both mirror the deployed Wikifunctions Python for `Z13701`:

```python
def Z13701(a, b):
    while b != 0: a, b = b, a % b
    return a == 1
```

and prove it against a recursive `gcd` spec (the analogue of Mathlib's `Nat.gcd`
/ `Nat.Coprime`, which is definitionally `gcd a b = 1`). The proof carries the
Euclidean loop invariant `gcd(x, y) == gcd(a, b)`.

Verify:

```bash
dafny verify Z13701_coprime.dfy     # → 2 verified, 0 errors
verus z13701_coprime.rs             # → 5 verified, 0 errors
```

## The faithfulness boundary (important)

Dafny verifies Dafny; Verus verifies Rust. **Neither runs on the literal
Python/JS source deployed on Wikifunctions.** What they prove is a *faithful
re-implementation* of the algorithm against the Mathlib-derived spec. Two honest
consequences:

1. The Dafny/Verus code must be a faithful transcription of the deployed
   algorithm — that correspondence is human-checked (the loop here is a
   line-for-line mirror of the Python).
2. A verified Dafny/Verus implementation is itself a candidate **Wikifunctions
   implementation** (the platform is multi-implementation), so this isn't only a
   proof artifact — it's a contributable, proven implementation.

To verify the *actual* Python source would need a Python deductive verifier
(e.g. Nagini → Viper) or differential testing against the live evaluator; those
are separate tracks.

## How the spec connects to Mathlib

The `gcd` spec here is written natively in Dafny/Verus and proved to be what the
imperative code computes. Tying that native `gcd` to Mathlib's `Nat.gcd` (so the
whole chain bottoms out at the same formal definition WikiLean maps from
Wikidata) is the cross-prover step — for now the specs are transcriptions of the
Mathlib definition, human-checked to match.
