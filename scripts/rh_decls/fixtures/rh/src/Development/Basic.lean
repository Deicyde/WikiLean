module

/-!
# Development
-/

@[expose] public section

namespace Development

/-- The **foo** lemma. -/
theorem foo : 1 = 1 := rfl

private theorem helper : True := trivial

@[simp] def bar : Nat := 1

instance : Inhabited Nat := ⟨0⟩

theorem _root_.topLevel : True := trivial

theorem dup : True := trivial

noncomputable section

def baz : Nat := 2

end

mutual
def even : Nat → Bool
  | 0 => true
  | n + 1 => odd n
def odd : Nat → Bool
  | 0 => false
  | n + 1 => even n
end

/-- Still in `Development`: the two `end`s above close the section and the `mutual`. -/
theorem afterEnd : True := trivial

end Development
