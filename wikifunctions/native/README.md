# Native-implementation verification (Dafny / Verus)

Deductive verification of Wikifunctions' **imperative implementations** with
heavyweight, SMT-backed provers.

> **Read this first — what these files do and don't prove.** Dafny verifies
> Dafny; Verus verifies Rust. Each proves a *re-implementation* of the algorithm
> against a spec written **in its own logic** — it does **not** ingest the
> deployed Python, and it cannot use Mathlib's `Nat.Coprime` as the spec. So
> these are "verified re-implementations against a transcribed spec": a real but
> *weaker and different* claim than the cross-system goal.
>
> The artifact that actually proves **the deployed Python computes Mathlib's
> spec** is the Lean deep embedding in
> [`../lean/Wikifunctions/Python/Z13701.lean`](../lean/Wikifunctions/Python/Z13701.lean):
> the actual Python is embedded as data and proved equal to Mathlib's
> `Nat.Coprime`, all in one trusted kernel. Prefer that as the model for "verify
> the real code vs the Lean spec." These Dafny/Verus files are best understood as
> (a) cross-prover corroboration, and (b) *verified implementations that could be
> contributed back* to Wikifunctions (which is multi-implementation).

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
- `rustpython_int_floordiv.rs` — **a different, more faithful kind of proof** (see below): it certifies the *leaf integer semantics* RustPython actually uses, not a re-implemented algorithm.

## Verifying the real interpreter's leaf, not a re-implementation (`rustpython_int_floordiv.rs`)

The two files above verify a *re-implementation* of the gcd algorithm — the
weaker claim the box at the top warns about. `rustpython_int_floordiv.rs` attacks
the gap from the other end: it grounds a proof in **RustPython's actual deployed
code path** at the granularity of a single operation (the `%` / `//` leaf the
Lean embedding's `Imp.Expr.mod` assumes).

The deployed chain is real and citable — in RustPython
`crates/vm/src/builtins/int.rs`:

```rust
fn inner_mod(a, b)      { if b.is_zero() {err} else { a.mod_floor(b) } }      // Python  %
fn inner_floordiv(a, b) { ...                         a.div_floor(b)   }      // Python  //
fn inner_divmod(a, b)   { ...                  a.div_mod_floor(b)      }      // divmod
```

So Python `%`/`//` on `int` are exactly `num_integer`'s **floored** division on
`num_bigint::BigInt`, which is realized as "truncated div/rem, then adjust toward
−∞." The Verus file proves that adjustment refines Python's exact floored
contract — `a == q·b + r`, remainder takes the **divisor's** sign, `|r| < |b|` —
for **all integers** (arbitrary precision, no range bound), and cross-checks the
output against CPython's actual results in all four sign quadrants.

```bash
verus rustpython_int_floordiv.rs    # → 5 verified, 0 errors
```

**Trust boundary (minimal and explicit).** The one assumed primitive is
`is_trunc_divmod`: that `BigInt`'s truncated `/`,`%` (num-bigint) meet the
standard truncation contract — the same bignum primitive `difftest.py` + `leanpy`
exercise empirically. *Not* proved: num-bigint's internal bignum algorithms, or
the compiled WASM. What *is* proved is the Python-floored-semantics logic
RustPython layers on top — the part where bugs would actually live.

**How it slots in.** Under the Lean embedding, `Imp.Expr.mod a b` currently uses
Lean's `Nat.mod`, *assumed* to match Python. This makes that leaf assumption a
*theorem about RustPython's real delegation chain* instead. For Z13701's loop
(`a % b` on non-negatives, where floored = truncated = ordinary remainder) the
corollary `coprime_loop_leaf_is_nat_mod` shows the leaf is exactly `Nat.mod`.
The `*` leaf (factorial Z13667) and comparisons are pure pass-throughs to
num-bigint (`int1 * int2`, `cmp`) with **no** Python-specific logic — nothing to
verify there beyond the same num-bigint trust assumption, so they're documented,
not re-proved.

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
