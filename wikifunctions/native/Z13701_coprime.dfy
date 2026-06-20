// Wikifunctions Z13701 "are coprime (natural numbers)" — Wikidata Q104752.
//
// Deductive proof (Dafny + Z3) that an IMPERATIVE Euclidean implementation —
// mirroring the deployed Wikifunctions Python
//     def Z13701(a, b):
//         while b != 0: a, b = b, a % b
//         return a == 1
// — satisfies the specification `Gcd(a, b) == 1`, where `Gcd` is the standard
// recursive Euclidean definition (the Dafny analogue of Mathlib's `Nat.gcd` /
// `Nat.Coprime`, which is defeq `gcd a b = 1`).
//
// Verify with:  dafny verify Z13701_coprime.dfy

// The specification: gcd as a pure recursive function (the "oracle").
function Gcd(a: nat, b: nat): nat
  decreases b
{
  if b == 0 then a else Gcd(b, a % b)
}

// The implementation: the deployed imperative Euclidean loop, proved to return
// `Gcd(a, b) == 1`. The loop invariant carries the Euclidean identity
// `Gcd(x, y) == Gcd(a, b)`, which holds because `Gcd(x, y) = Gcd(y, x % y)`
// for `y != 0` — exactly the loop's update step.
method AreCoprime(a: nat, b: nat) returns (r: bool)
  ensures r == (Gcd(a, b) == 1)
{
  var x, y := a, b;
  while y != 0
    invariant Gcd(x, y) == Gcd(a, b)
    decreases y
  {
    x, y := y, x % y;
  }
  r := x == 1;
}
