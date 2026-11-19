"""End-to-end tests of the Tau Ceti declaration build on the fixtures (offline; local git only)."""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from scripts.brain_producers import decls as D  # noqa: E402
from scripts.brain_producers.testing import TAUCETI_REV, make_inputs, read_rows, read_tree, write_pins  # noqa: E402
from scripts.tauceti_decls import build as B  # noqa: E402

FOO, BAR, BAZ = "decl:TauCeti:TauCeti.A.foo", "decl:TauCeti:TauCeti.A.bar", "decl:TauCeti:TauCeti.B.baz"


class BuildFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.inputs = make_inputs(cls.tmp, {"tauceti_decls": TAUCETI_REV})
        cls.out = os.path.join(cls.tmp, "out")
        cls.manifest = B.build(cls.inputs, cls.out)
        cls.rows = {n: {r["id"]: r for r in read_rows(cls.out, n)} for n in B.FILES}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def rejected(self, **kw):
        return [r for r in self.rows["rejected.jsonl"].values() if all(r.get(k) == v for k, v in kw.items())]

    def test_two_builds_are_byte_identical_and_refetch_keeps_the_root(self):
        out2 = os.path.join(self.tmp, "out2")
        B.build(self.inputs, out2)
        self.assertEqual(read_tree(self.out), read_tree(out2))
        write_pins(self.inputs, {"tauceti_decls": TAUCETI_REV}, "2030-01-01T00:00:00Z")
        try:
            m = B.build(self.inputs, os.path.join(self.tmp, "out3"))
        finally:
            write_pins(self.inputs, {"tauceti_decls": TAUCETI_REV}, "2026-11-18T00:00:00Z")
        self.assertEqual(m["logical_root"], self.manifest["logical_root"])
        self.assertEqual(m["inputs"]["trough"]["fetched_at"], "2030-01-01T00:00:00Z")

    def test_names_are_the_whole_index(self):
        names = self.rows["names.jsonl"]
        self.assertEqual(len(names), 5)  # Bad.link has no parseable docLink
        self.assertEqual(names[FOO], {"id": FOO, "name": "TauCeti.A.foo", "module": "TauCeti.A", "kind": "theorem",
                                      "doc_link": "./TauCeti/A.html#TauCeti.A.foo"})
        self.assertIn("decl:TauCeti:TauCeti.B.unmapped", names)
        self.assertEqual(len(self.rejected(reason="doc_link_unparseable", candidate="Bad.link")), 1)
        self.assertNotIn("doc", names[FOO])

    def test_mapped_decls(self):
        decls = self.rows["decls.jsonl"]
        self.assertEqual(sorted(decls), sorted([BAR, FOO, BAZ, "decl:TauCeti:TauCeti.B.extra"]))
        foo = decls[FOO]
        self.assertEqual(foo["mapped_by"], ["deps", "selection"])
        self.assertEqual(foo["selection"], "named theorem")
        self.assertEqual((foo["line"], foo["doc"], foo["signature"]), (10, "The **foo** theorem.", "TauCeti.A.foo : True"))
        self.assertEqual(foo["source"], {"repo": "TauCetiProject/TauCeti", "commit": TAUCETI_REV,
                                         "path": "TauCeti/A.lean", "line": 10})
        self.assertEqual(decls[BAR]["roadmaps"], ["tauceti:Completed/Gamma", "tauceti:TauCetiRoadmap/Alpha"])
        self.assertEqual(self.rejected(reason="not_in_index")[0]["candidate"], "TauCeti.NotThere.q")
        self.assertEqual(self.rejected(reason="trough_missing")[0]["candidate"], "TauCeti.Gone.x")

    def test_module_mismatch_drops_trough_fields_and_kind_is_the_index(self):
        baz = self.rows["decls.jsonl"][BAZ]
        self.assertEqual((baz["module"], baz["kind"], baz["line"], baz["source"]), ("TauCeti.B", "instance", None, None))
        self.assertEqual(self.rejected(reason="module_mismatch")[0]["trough_module"], "TauCeti.Wrong")

    def test_module_mismatch_drops_stated_too(self):
        """`stated` is read from the same trough record the mismatch rejected, so it is exactly
        as untrustworthy as the four fields dropped with it — and it is the load-bearing field."""
        baz = self.rows["decls.jsonl"][BAZ]
        self.assertEqual((baz["stated"], baz["stated_reason"]), (None, "module_mismatch"))
        self.assertEqual(self.manifest["counts"]["decls_by_stated"]["null/module_mismatch"], 1)

    def test_stated_when_commits_match(self):
        decls = self.rows["decls.jsonl"]
        self.assertIs(decls[FOO]["stated"], True)
        self.assertIs(decls[BAR]["stated"], False)
        self.assertNotIn("stated_reason", decls[FOO])
        extra = decls["decl:TauCeti:TauCeti.B.extra"]
        self.assertEqual((extra["stated"], extra["stated_reason"]), (None, "not_in_deps"))
        self.assertIsNone(self.manifest["stated"]["reason"])

    def test_stated_null_otherwise(self):
        for docs, why in (({}, "docs_commit_unknown"), ({"tauceti_decls": "0" * 40}, "commit_mismatch")):
            tmp = tempfile.mkdtemp()
            try:
                inputs = make_inputs(tmp, docs)
                out = os.path.join(tmp, "out")
                m = B.build(inputs, out)
                rows = read_rows(out, "decls.jsonl")
                self.assertEqual({(r["stated"], r["stated_reason"]) for r in rows}, {(None, why)})
                self.assertEqual(m["stated"]["reason"], why)
            finally:
                shutil.rmtree(tmp)

    def test_deps(self):
        deps = self.rows["deps.jsonl"]
        ev = {"repo": "Deicyde/trough", "commit": self.manifest["inputs"]["trough"]["commit"],
              "path": "data/tauceti/deps.json"}
        self.assertEqual(deps[f"uses_in_statement:{FOO}->{BAR}"],
                         {"id": f"uses_in_statement:{FOO}->{BAR}", "kind": "uses_in_statement", "source": FOO,
                          "target": BAR, "evidence": ev})
        self.assertEqual(sorted(deps), sorted([f"uses_in_statement:{FOO}->{BAR}", f"uses_in_proof:{FOO}->{BAR}",
                                               f"uses_in_proof:{FOO}->{BAZ}"]))
        self.assertEqual(len(self.rejected(reason="self_edge")), 1)
        self.assertEqual(self.rejected(reason="target_not_mapped")[0]["candidate"], "TauCeti.Gone.x")

    def test_manifest(self):
        m = self.manifest
        self.assertEqual(m["schema"], "wikilean.brain.tauceti-decls/v1")
        self.assertEqual(m["inputs"]["tauceti_decls"]["docs_commit"], TAUCETI_REV)
        self.assertEqual(m["inputs"]["trough"]["tauceti_rev"], TAUCETI_REV)
        self.assertEqual(set(m["inputs"]["trough"]["files"]),
                         {f"data/tauceti/{f}" for f in B.TROUGH_FILES})
        self.assertEqual(m["files"]["decls.jsonl"]["rows"], 4)
        self.assertEqual(m["attribution"][0]["license"], "Apache-2.0")
        self.assertEqual(m["logical_root"], D.logical_root(m))

    def test_pin_mismatch_and_out_in_work_tree_refused(self):
        tmp = tempfile.mkdtemp()
        try:
            inputs = make_inputs(tmp)
            write_pins(inputs, {}, "x", {"tauceti_decls": {"sha256": "0" * 64}})
            with self.assertRaisesRegex(D.BuildError, "sha256"):
                B.build(inputs, os.path.join(tmp, "out"))
            write_pins(inputs, {}, "x", {"trough": {"commit": "0" * 40}})
            with self.assertRaisesRegex(D.BuildError, "^trough: HEAD"):
                B.build(inputs, os.path.join(tmp, "out"))
            with self.assertRaisesRegex(D.BuildError, "git work tree"):
                B.build(inputs, os.path.join(inputs, "trough", "out"))
            self.assertFalse(os.path.exists(os.path.join(tmp, "out")))
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
