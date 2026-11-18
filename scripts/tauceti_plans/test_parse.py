"""Unit tests of the text rules (offline)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from scripts.tauceti_plans import parse  # noqa: E402


class Anchors(unittest.TestCase):
    def test_github_slug_rule(self):
        s = parse.Slugger()
        self.assertEqual(s.slug("Layer 3: Abhyankar's lemma"), "layer-3-abhyankars-lemma")
        self.assertEqual(s.slug("Layer 1 — second"), "layer-1--second")
        self.assertEqual(s.slug("Layer 2: the E₈ cap (P7)"), "layer-2-the-e₈-cap-p7")
        self.assertEqual(s.slug("Étale `IsUnit.mul`"), "étale-isunitmul")
        # repeats are numbered in document order
        self.assertEqual(s.slug("Layer 1 — second"), "layer-1--second-1")
        self.assertEqual(s.slug("Layer 1 — second"), "layer-1--second-2")

    def test_headings_skip_fences_and_clean_markup(self):
        lines = parse.lines_of("# T\n```\n## Layer 9: fenced\n```\n## **Layer 0**: [a](x) `b.c`\n")
        hs = parse.headings(lines)
        self.assertEqual([h["line"] for h in hs], [1, 5])
        self.assertEqual(hs[1]["title"], "Layer 0: a b.c")


class LayerKeys(unittest.TestCase):
    def test_keys(self):
        cases = {
            "Layer 5: foo": "Layer 5", "Layer 2.1 — x": "Layer 2.1", "Layer 3A. y": "Layer 3A",
            "Layer AI-0: z": "Layer AI-0", "Y1 — w": "Y1", "Part A: v": "Part A",
            "Milestone 2": "Milestone 2", "Lane F4: Heegaard": "Lane F4", "Layer RGN.2: q": "Layer RGN.2",
        }
        for title, key in cases.items():
            self.assertEqual(parse.layer_key(title), key, title)

    def test_non_layers(self):
        for title in ("Layer 3 prerequisites: x", "F0.1 constructions", "Scope and ownership",
                      "Layers and lanes", "N1.Tensor. Completed", "0.1 Unramified"):
            self.assertIsNone(parse.layer_key(title), title)


class LeanNames(unittest.TestCase):
    def test_backticks(self):
        got = parse.backticked_names("`Nat.succ_le`, `f x`, `x.1`, `A.b'`, `_root_.C.d`, `Foo.lean`, `ab`")
        self.assertEqual(got, [("Nat.succ_le", None), ("A.b'", None), ("C.d", None), ("Foo.lean", "file_name")])

    def test_lean_scan_namespaces(self):
        text = (
            "namespace A.B\n"
            "/-! ## Layer 0: x -/\n"
            "section S\n"
            "theorem t1 : True := trivial\n"
            "end S\n"
            "namespace C\n"
            "noncomputable def d : Nat := 1\n"
            "end C\n"
            "/- theorem hidden : True\n  still hidden -/\n"
            "-- lemma hidden2 : True\n"
            "@[simp] protected theorem _root_.Z.w : True := trivial\n"
            "instance : Inhabited Nat := ⟨0⟩\n"
            "instance (priority := 10) named : Inhabited Nat := ⟨0⟩\n"
            "end A.B\n"
            "structure Top where\n"
        )
        decls, markers = parse.lean_scan(text)
        self.assertEqual([d["name"] for d in decls], ["A.B.t1", "A.B.C.d", "Z.w", "A.B.named", "Top"])
        self.assertEqual(markers, [{"line": 2, "title": "Layer 0: x"}])


class Xrefs(unittest.TestCase):
    def test_stacks(self):
        self.assertEqual(parse.stacks_tags("See [Stacks, Tag 0A1B] and 00X9."), [("0A1B", None), ("00X9", None)])
        self.assertEqual(parse.stacks_tags("Stacks 2019 and LEAN and ABCD"), [("2019", "looks_like_year")])
        self.assertEqual(parse.stacks_tags("1234 0A1B but no keyword"), [])
        self.assertEqual(parse.stacks_tags("tags 0A1B lowercase prose"), [])
        self.assertEqual(parse.stacks_tags("https://stacks.math.columbia.edu/tag/01AB"), [("01AB", None)])
        self.assertEqual(parse.stacks_tags("Tag word followed by NOTE and SL2Z."), [("SL2Z", None)])
        self.assertEqual(parse.stacks_tags("[Stacks, 0A1B]; arXiv 1507.01234, doi 10.1507/x"), [("0A1B", None)])

    def test_arxiv(self):
        self.assertEqual(parse.arxiv_ids("arXiv:2101.01234v2"), [("2101.01234", None)])
        self.assertEqual(parse.arxiv_ids("arXiv 0704.0001"), [("0704.0001", None)])
        self.assertEqual(parse.arxiv_ids("arXiv 2101.0123"), [("2101.0123", "invalid_arxiv_id")])
        self.assertEqual(parse.arxiv_ids("arXiv 1313.01234"), [("1313.01234", "invalid_arxiv_id")])
        self.assertEqual(parse.arxiv_ids("version 2101.01234 without the word"), [])
        self.assertEqual(parse.arxiv_ids("arXiv math/0601001v1"), [("math/0601001", "old_style_arxiv_id")])


if __name__ == "__main__":
    unittest.main()
