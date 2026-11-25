#!/usr/bin/env python3
"""Scopes in build_common._lean_decl_lines: every case below is a pattern found in
mathlib, riemann-hypothesis or formal-conjectures, and each expected name is the one
Lean 4.37 gives the declaration."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_common  # noqa: E402


def names(source: str) -> list[str]:
    return sorted(build_common._lean_decl_lines(source.splitlines()))


class LeanDeclScopeTests(unittest.TestCase):
    def test_noncomputable_section_end_keeps_namespace(self) -> None:
        # mathlib Data/NNReal/Defs.lean
        self.assertEqual(names("""
namespace NNReal
noncomputable section
def a : Nat := 0
end
theorem b : True := trivial
end NNReal
"""), ["NNReal.a", "NNReal.b"])

    def test_public_sections_and_named_section(self) -> None:
        self.assertEqual(names("""
@[expose] public section
namespace A
public section B
theorem x : True := trivial
end B
public noncomputable section
theorem y : True := trivial
end
theorem z : True := trivial
end A
end
"""), ["A.x", "A.y", "A.z"])

    def test_mutual_end_keeps_namespace(self) -> None:
        self.assertEqual(names("""
namespace M
mutual
def even : Nat → Bool
  | 0 => true
  | n + 1 => odd n
def odd : Nat → Bool
  | 0 => false
  | n + 1 => even n
end
theorem after : True := trivial
end M
"""), ["M.after", "M.even", "M.odd"])

    def test_end_dotted_closes_scopes_opened_one_at_a_time(self) -> None:
        # mathlib Topology/Maps/Basic.lean: `section IsInducing`, `namespace IsInducing`,
        # closed together by `end IsInducing.IsInducing`
        self.assertEqual(names("""
namespace Topology
section IsInducing
namespace IsInducing
theorem inside : True := trivial
end IsInducing.IsInducing
theorem outside : True := trivial
end Topology
"""), ["Topology.IsInducing.inside", "Topology.outside"])

    def test_end_closes_last_part_of_dotted_namespace(self) -> None:
        # mathlib Topology/FiberBundle/Trivialization.lean
        self.assertEqual(names("""
namespace Bundle.Pretrivialization
def p : Nat := 0
end Pretrivialization
namespace Trivialization
def t : Nat := 0
end Bundle.Trivialization
def top : Nat := 0
"""), ["Bundle.Pretrivialization.p", "Bundle.Trivialization.t", "top"])

    def test_end_parts_after_reopening(self) -> None:
        # riemann-hypothesis QuantitativeMahlerCriterionUnitaryGroup.lean
        self.assertEqual(names("""
namespace G.C.M
theorem a : True := trivial
end M
end G.C
namespace G.C.M
theorem b : True := trivial
end G.C.M
"""), ["G.C.M.a", "G.C.M.b"])

    def test_guillemet_namespace_with_dots(self) -> None:
        # formal-conjectures FormalConjectures/Arxiv/0911.2077/Conjecture6_3.lean
        self.assertEqual(names("""
namespace Arxiv.«0911.2077»
theorem c : True := trivial
end Arxiv.«0911.2077»
theorem d : True := trivial
"""), ["Arxiv.«0911.2077».c", "d"])

    def test_root_comments_and_bare_section(self) -> None:
        self.assertEqual(names("""
namespace N
section
/- end
namespace Fake -/
-- end
theorem s : True := trivial
end
theorem _root_.r : True := trivial
end N
"""), ["N.s", "r"])

    def test_end_dotted_closes_two_namespaces(self) -> None:
        self.assertEqual(names("""
namespace A
namespace B
theorem x : True := trivial
end A.B
theorem y : True := trivial
"""), ["A.B.x", "y"])

    def test_name_parts(self) -> None:
        self.assertEqual(build_common._lean_name_parts("A.«b.c».d"), ["A", "«b.c»", "d"])
        self.assertEqual(build_common._lean_name_parts("A"), ["A"])


if __name__ == "__main__":
    unittest.main()
