/-!
# Alpha
-/

namespace Alpha

/-- The **foo** lemma. -/
theorem foo : 1 = 1 := rfl

private theorem helper : True := trivial

@[simp] def bar : Nat := 1

instance : Inhabited Nat := ⟨0⟩

theorem _root_.topLevel : True := trivial

theorem dup : True := trivial

end Alpha
