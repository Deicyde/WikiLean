"""Tests of check_pins on fixture manifests (offline)."""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from scripts.brain_producers import check_pins as C  # noqa: E402

T, M = {"sha256": "1" * 64, "docs_commit": "a" * 40}, {"sha256": "2" * 64, "docs_commit": "b" * 40}


def manifest(schema, **inputs):
    return {"schema": schema, "inputs": inputs, "logical_root": "0" * 64}


class CheckPins(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, name, m):
        d = os.path.join(self.tmp, name)
        os.makedirs(d)
        with open(os.path.join(d, "manifest.json"), "w") as f:
            json.dump(m, f)
        return d

    def run_main(self, dirs):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = C.main(dirs)
        return code, out.getvalue(), err.getvalue()

    def exports(self, p2_tauceti=T, p3_mathlib=M, p3_trough="c" * 40):
        return [self.write("p1", manifest("wikilean.brain.plans/v1", trough={"commit": "c" * 40},
                                          tauceti_decls=T, mathlib_decls=M, atlas={"sha256": "9" * 64})),
                self.write("p2", manifest("wikilean.brain.tauceti-decls/v1", trough={"commit": "c" * 40},
                                          tauceti_decls=p2_tauceti)),
                self.write("p3", manifest("wikilean.brain.mathlib-decls/v1", trough={"commit": p3_trough},
                                          mathlib_decls=p3_mathlib))]

    def test_agreeing_exports_pass(self):
        code, out, _ = self.run_main(self.exports())
        self.assertEqual(code, 0)
        self.assertIn(f"ok        trough.commit = {'c' * 40} (3 exports)", out)
        self.assertIn("ok        tauceti_decls.docs_commit", out)
        self.assertIn("ok        mathlib_decls.sha256", out)

    def test_every_mismatch_is_named(self):
        dirs = self.exports(p2_tauceti={**T, "docs_commit": None}, p3_mathlib={**M, "sha256": "3" * 64},
                            p3_trough="d" * 40)
        code, out, err = self.run_main(dirs)
        self.assertEqual(code, 1)
        self.assertIn("MISMATCH  trough.commit", out)
        self.assertIn(f"{dirs[2]}={'d' * 40}", out)
        self.assertIn(f"MISMATCH  tauceti_decls.docs_commit: {dirs[0]}={'a' * 40}, {dirs[1]}=None", out)
        self.assertIn("MISMATCH  mathlib_decls.sha256", out)
        self.assertIn("ok        mathlib_decls.docs_commit", out)
        self.assertIn("3 mismatch(es)", err)

    def test_unshared_inputs_are_not_compared(self):
        dirs = self.exports()
        code, out, _ = self.run_main(dirs[1:])  # producers 2 and 3 share only trough
        self.assertEqual(code, 0)
        self.assertNotIn("tauceti_decls", out.split("logical_root")[-1])

    def test_producer_4_is_read_and_shares_no_pin(self):
        rh = self.write("p4", manifest("wikilean.brain.rh-decls/v1", rh={"commit": "e" * 40}))
        code, out, _ = self.run_main(self.exports() + [rh])
        self.assertEqual(code, 0)
        self.assertNotIn("rh.", out)
        self.assertEqual(self.run_main([rh])[0], 0)

    def test_unreadable_manifest(self):
        bad = self.write("bad", {"schema": "something/else", "inputs": {}})
        self.assertEqual(self.run_main([bad])[0], 2)
        self.assertEqual(self.run_main([os.path.join(self.tmp, "missing")])[0], 2)


if __name__ == "__main__":
    unittest.main()
