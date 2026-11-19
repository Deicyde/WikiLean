"""End-to-end tests of the Mathlib declaration build and its list reader on the fixtures
(offline; local git only)."""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from scripts.brain_producers import decls as D  # noqa: E402
from scripts.brain_producers.testing import MATHLIB_REV, make_inputs, read_rows, read_tree  # noqa: E402
from scripts.mathlib_decls import build as B  # noqa: E402
from scripts.mathlib_decls import lists as L  # noqa: E402

BAR, BAZ, QUX = "decl:Mathlib:Foo.bar", "decl:Mathlib:Foo.baz", "decl:Mathlib:Foo.qux"
ADD = "decl:Mathlib:Nat.add_comm"


class ListsTest(unittest.TestCase):
    def test_scalars(self):
        self.assertEqual(L.scalar("'Stream''.x'"), "Stream'.x")
        self.assertEqual(L.scalar("'Foo.bar' # note"), "Foo.bar")
        self.assertEqual(L.scalar('"a\\"b"'), 'a"b')
        self.assertEqual(L.scalar("Foo.bar  # x"), "Foo.bar")
        self.assertIsNone(L.scalar("''"))
        self.assertIsNone(L.scalar("'unclosed"))

    def test_classify(self):
        self.assertEqual(L.classify("Mathlib/Order/LiminfLimsup.html"), ("module", "Mathlib.Order.LiminfLimsup"))
        self.assertEqual(L.classify("https://x.org/y"), ("reject", "not_lean_name"))
        self.assertEqual(L.classify("not a name"), ("reject", "not_lean_name"))
        self.assertEqual(L.classify("Stream'.x"), ("decl", "Stream'.x"))

    def test_entry_lists(self):
        text = "1:\n  decl : A.b\n2:\n  decls:\n  - C.d\n  - E.f\n  note: >-\n    - G.h\n3:\n  decls: [I.j, K.l]\n"
        got = [(e["entry"], e["value"]) for e in L.entries("100", text)]
        self.assertEqual(got, [("1", "A.b"), ("2", "C.d"), ("2", "E.f"), ("3", "I.j"), ("3", "K.l")])

    def test_a_quoted_key_is_a_key_not_the_line_above_s_value(self):
        """A quoted key after a key with no value of its own (its parent, or an empty one) is
        still a mapping line: its value must not be read as the previous key's value."""
        text = ("General algebra:\n  Numbers:\n    \"$p$-adic number\": 'A.b'\n"
                "    empty: ''\n    'quoted key': \"C.d\"\n    plain: 'E.f'\n")
        self.assertEqual([(e["value"], e["line"]) for e in L.entries("overview", text)],
                         [("A.b", 3), ("C.d", 5), ("E.f", 6)])

    def test_a_value_on_the_next_line_still_resolves(self):
        text = "Top:\n  Sub:\n    long one:\n      'A.b'\n    after: 'C.d'\n"
        self.assertEqual([(e["value"], e["line"]) for e in L.entries("overview", text)],
                         [("A.b", 4), ("C.d", 5)])


class BuildFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.inputs = make_inputs(cls.tmp, {"mathlib_decls": "a" * 40})
        cls.out = os.path.join(cls.tmp, "out")
        cls.manifest = B.build(cls.inputs, cls.out)
        cls.rows = {n: {r["id"]: r for r in read_rows(cls.out, n)} for n in B.FILES}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def rejected(self, **kw):
        return [r for r in self.rows["rejected.jsonl"].values() if all(r.get(k) == v for k, v in kw.items())]

    def test_two_builds_are_byte_identical(self):
        out2 = os.path.join(self.tmp, "out2")
        B.build(self.inputs, out2)
        self.assertEqual(read_tree(self.out), read_tree(out2))

    def test_names(self):
        names = self.rows["names.jsonl"]
        self.assertEqual(len(names), 7)
        self.assertEqual(names[ADD]["module"], "Init.Core")  # the index's library, the real module

    def test_mapped_decls_and_lists(self):
        decls = self.rows["decls.jsonl"]
        self.assertEqual(sorted(decls), sorted([BAR, BAZ, QUX, ADD, "decl:Mathlib:Listed.only",
                                                "decl:Mathlib:Stream'.x"]))
        bar = decls[BAR]
        self.assertEqual(bar["mapped_by"], ["concepts", "lists"])
        self.assertEqual(bar["lists"], ["100", "1000", "overview"])
        self.assertEqual([(p["list"], p["entry"], p["line"]) for p in bar["list_entries"]],
                         [("100", "1", 4), ("1000", "Q11518", 3), ("overview", None, 4)])
        self.assertEqual(bar["source"], {"repo": "leanprover-community/mathlib4", "commit": MATHLIB_REV,
                                         "path": "Mathlib/Foo.lean", "line": 5})
        self.assertEqual(bar["doc"], "Foo's theorem.")
        self.assertNotIn("stated", bar)
        self.assertEqual(decls[BAZ]["lists"], ["100", "1000", "overview"])  # block, inline, next-line value
        self.assertEqual((decls[BAZ]["doc"], decls[BAZ]["deprecated"]), (None, "2026-01-01"))
        self.assertEqual(decls[QUX]["mapped_by"], ["lists", "module_decls"])
        self.assertEqual(decls[QUX]["lists"], ["1000"])  # not the folded note's "- Foo.qux"
        self.assertIsNone(decls[ADD]["source"])  # Init is not in mathlib4
        self.assertEqual(decls[ADD]["line"], 1)
        self.assertEqual(decls["decl:Mathlib:Listed.only"]["lists"], ["100"])  # `statement:`
        self.assertEqual(decls["decl:Mathlib:Stream'.x"]["lists"], ["undergrad"])

    def test_rejections(self):
        self.assertEqual(self.rejected(reason="not_in_index")[0]["candidate"], "NotInIndex.y")
        self.assertEqual(self.rejected(reason="module_entry")[0]["candidate"], "Mathlib.Foo")
        self.assertEqual({r["candidate"] for r in self.rejected(reason="not_lean_name")},
                         {"https://en.wikipedia.org/wiki/Example", "not a name"})
        self.assertEqual(self.rejected(reason="trough_unresolved")[0]["candidate"], "Mathlib.Foo")
        self.assertEqual(self.rejected(reason="target_not_mapped")[0]["candidate"], "Missing.x")
        self.assertEqual(len(self.rejected(reason="self_edge")), 1)

    def test_deps(self):
        self.assertEqual(sorted(self.rows["deps.jsonl"]),
                         sorted([f"uses_in_statement:{BAR}->{ADD}", f"uses_in_proof:{BAR}->{BAZ}"]))
        ev = self.rows["deps.jsonl"][f"uses_in_proof:{BAR}->{BAZ}"]["evidence"]
        self.assertEqual(ev["path"], "data/mathlib/concept-deps.json")

    def test_manifest_commits(self):
        m = self.manifest
        self.assertEqual(m["schema"], "wikilean.brain.mathlib-decls/v1")
        self.assertEqual((m["mathlib_commit"], m["docs_commit"], m["mathlib_commit_matches_docs"]),
                         (MATHLIB_REV, "a" * 40, False))
        self.assertEqual(m["counts"]["decls_on_list"], {"100": 3, "1000": 3, "overview": 3, "undergrad": 1})
        self.assertEqual(m["logical_root"], D.logical_root(m))
        for docs, want in (({}, None), ({"mathlib_decls": MATHLIB_REV}, True)):
            tmp = tempfile.mkdtemp()
            try:
                got = B.build(make_inputs(tmp, docs), os.path.join(tmp, "out"))
                self.assertIs(got["mathlib_commit_matches_docs"], want)
            finally:
                shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
