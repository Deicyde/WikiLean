import Mathlib.Data.Rat.Defs

/-!
# Core of the Wikifunctions composite-evaluator embedding

An **open**, ZID-referenced composition language and a **registry-parameterized**
evaluator. This mirrors the real Z-object model: a function call (`Z7`) names
another function by its ZID (a string), resolved in a global context — here the
`Registry`. Each Wikifunction lives in its own module and contributes its
denotation/proof; nothing is baked into a closed enum.
-/

namespace Wikifunctions

/-- A Wikifunctions object id, e.g. `"Z13612"`. -/
abbrev ZID := String

/-- Value universe (the subset the current functions need): `Z13518` naturals,
    `Z40` booleans, and rationals. Extend as new functions need new types. -/
inductive Val
  | nat  : Nat → Val
  | bool : Bool → Val
  | rat  : ℚ → Val
deriving DecidableEq, Repr

/-- Composition AST: `Z18` argument references, value literals, and `Z7` calls
    that name a function by ZID. This is the shape of a `Z14K2` composite body. -/
inductive Expr
  | arg  : Nat → Expr
  | lit  : Val → Expr
  | call : ZID → List Expr → Expr

/-- A function/leaf denotation: total on well-typed inputs, `none` otherwise. -/
abbrev Denotation := List Val → Option Val

/-- A registry resolves a ZID to its denotation — the global function context
    the evaluator runs against. -/
abbrev Registry := ZID → Option Denotation

/- The registry-parameterized evaluator. Total: returns `none` on a missing
   ZID, a type error, or an out-of-range argument reference. `eval`/`evalArgs`
   are a mutually-structural recursion over `Expr` / `List Expr`. -/
mutual
  def eval (R : Registry) (env : List Val) : Expr → Option Val
    | .arg i     => env[i]?
    | .lit v     => some v
    | .call f es => (evalArgs R env es).bind fun vs => (R f).bind fun d => d vs
  def evalArgs (R : Registry) (env : List Val) : List Expr → Option (List Val)
    | []      => some []
    | e :: es => (eval R env e).bind fun v => (evalArgs R env es).bind fun vs => some (v :: vs)
end

end Wikifunctions
