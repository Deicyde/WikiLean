/-
WikiLean: extract declaration-level dependency edges among target Mathlib decls.

For each target decl, collects the constants referenced by its type and value
(via `Expr.getUsedConstants`) and emits an edge whenever the referenced constant
is also in the target set. This is the same notion of "reference" that doc-gen4
hyperlinks in the Mathlib docs.

Usage (from a built mathlib4 checkout; `lake exe cache get` first):
  lake env lean --run extract_deps.lean targets.txt mathlib_edges.tsv

Output: TSV "from_decl<TAB>to_decl", one direct reference per line.
-/
import Lean
open Lean

def parseDeclName (s : String) : Name :=
  (s.splitOn ".").foldl
    (fun n part => if part.isEmpty then n else Name.mkStr n part)
    Name.anonymous

def main (args : List String) : IO UInt32 := do
  match args with
  | [inPath, outPath] =>
    initSearchPath (← findSysroot)
    IO.println "loading Mathlib..."
    let env ← importModules #[{ module := `Mathlib }] {} 0
    let txt ← IO.FS.readFile inPath
    let targets := txt.splitOn "\n" |>.map (·.trimAscii.toString) |>.filter (· ≠ "")
    let mut targetSet : NameSet := {}
    for t in targets do
      targetSet := targetSet.insert (parseDeclName t)
    IO.println s!"{targets.length} target decls"
    let edges ← IO.mkRef (0 : Nat)
    let misses ← IO.mkRef (0 : Nat)
    IO.FS.withFile outPath .write fun h => do
      for t in targets do
        let name := parseDeclName t
        match env.find? name with
        | none =>
          IO.eprintln s!"missing: {t}"
          misses.modify (· + 1)
        | some ci =>
          let typeUsed := ci.type.getUsedConstants
          let valUsed := (ci.value?.map Expr.getUsedConstants).getD #[]
          let mut seen : NameSet := {}
          for u in typeUsed ++ valUsed do
            if u != name && targetSet.contains u && !seen.contains u then
              h.putStrLn s!"{t}\t{u}"
              seen := seen.insert u
              edges.modify (· + 1)
    IO.println s!"wrote {(← edges.get)} edges; {(← misses.get)} decls not found"
    return 0
  | _ =>
    IO.eprintln "usage: extract_deps <targets.txt> <out.tsv>"
    return 1
