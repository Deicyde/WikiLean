import Mathlib

/-!
# Suggested targets for Alpha
-/

namespace TauCetiRoadmap.Alpha

/-! ## Layer 0: Abhyankar's lemma -/

section Layer0

/-- A docstring that must not be copied. -/
theorem foo : True := trivial

end Layer0

/-! ## Layer 1 — second -/

namespace Inner

noncomputable def bar : Nat := 1

end Inner

/- A block comment: theorem hidden : True := trivial -/
-- theorem alsoHidden : True := trivial

@[simp] theorem _root_.Nat.succ_le : True := trivial

end TauCetiRoadmap.Alpha

/-! ## Layer 7: not in the README -/

theorem top_level : True := trivial
