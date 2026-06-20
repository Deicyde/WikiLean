// Wikifunctions Z13701 "are coprime (natural numbers)" — Wikidata Q104752.
//
// Deductive proof (Verus + Z3) that an IMPERATIVE Euclidean implementation —
// mirroring the deployed Wikifunctions Python
//     def Z13701(a, b):
//         while b != 0: a, b = b, a % b
//         return a == 1
// — satisfies the specification `gcd(a, b) == 1`, where `gcd` is the standard
// recursive Euclidean definition (the Verus analogue of Mathlib's `Nat.gcd` /
// `Nat.Coprime`, which is defeq `gcd a b = 1`).
//
// Verify with:  verus z13701_coprime.rs

use vstd::prelude::*;

verus! {

// The specification: gcd as a pure spec function (the "oracle").
spec fn gcd(a: nat, b: nat) -> nat
    decreases b
{
    if b == 0 { a } else { gcd(b, (a % b) as nat) }
}

// Euclidean step lemma: gcd(x, y) = gcd(y, x % y) for y != 0 — the identity the
// loop relies on. Proved by unfolding the recursive definition once.
proof fn gcd_step(x: nat, y: nat)
    requires y != 0,
    ensures gcd(x, y) == gcd(y, (x % y) as nat),
{
    reveal_with_fuel(gcd, 2);
}

// The implementation: the deployed imperative Euclidean loop, proved to return
// `gcd(a, b) == 1`.
fn are_coprime(a: u64, b: u64) -> (r: bool)
    ensures r == (gcd(a as nat, b as nat) == 1),
{
    let mut x: u64 = a;
    let mut y: u64 = b;
    while y != 0
        invariant gcd(x as nat, y as nat) == gcd(a as nat, b as nat),
        decreases y,
    {
        proof { gcd_step(x as nat, y as nat); }
        let t = x % y;
        x = y;
        y = t;
    }
    x == 1
}

fn main() {}

}
