"""The shared input guards of `build`: it is offline, it refuses an unpinned input, and a bad
input is a BuildError rather than a traceback. Offline; local git only."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from scripts.brain_producers import decls as D  # noqa: E402
from scripts.brain_producers.common import GitTree, sha256_file  # noqa: E402
from scripts.brain_producers.test_fetch_decls import FakeOpener  # noqa: E402
from scripts.brain_producers.testing import FIX, make_inputs  # noqa: E402
from scripts.mathlib_decls import build as B3  # noqa: E402
from scripts.mathlib_decls import fetch as F3  # noqa: E402
from scripts.tauceti_decls import build as B2  # noqa: E402
from scripts.tauceti_plans.test_build import _git_repo  # noqa: E402


class OfflineBuildTest(unittest.TestCase):
    """`fetch` clones trough with --filter=blob:none, so a file outside this producer's sparse
    set has no blob locally. `build` must say so, not fetch it from the remote."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.remote = os.path.join(self.tmp, "remote", "trough")
        _git_repo(os.path.join(FIX, "trough"), self.remote)
        subprocess.run(["git", "-C", self.remote, "config", "uploadpack.allowFilter", "true"], check=True)
        self.inputs = os.path.join(self.tmp, "inputs")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_a_blob_outside_the_sparse_set_is_an_error_not_a_lazy_fetch(self):
        F3.fetch(self.inputs, opener=FakeOpener(self.remote))  # producer 3: data/mathlib only
        trough = os.path.join(self.inputs, "trough")
        self.assertFalse(os.path.exists(os.path.join(trough, "data", "tauceti")))
        with self.assertRaises(D.BuildError) as cm:
            B2.build(self.inputs, os.path.join(self.tmp, "out"))
        self.assertIn("data/tauceti/deps.json", str(cm.exception))
        self.assertIn("fetch", str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "out")))
        # The remote was reachable throughout: git fetches the very same blob on demand here,
        # so the build failed because it refused to go and get it, not because it could not.
        oid = D.GitTree(trough).blobs["data/tauceti/deps.json"]
        self.assertEqual(subprocess.run(["git", "-C", trough, "cat-file", "-e", oid],
                                        capture_output=True).returncode, 0)

    def test_a_trough_that_is_not_a_checkout_is_a_build_error(self):
        F3.fetch(self.inputs, opener=FakeOpener(self.remote))
        shutil.rmtree(os.path.join(self.inputs, "trough", ".git"))
        with self.assertRaisesRegex(D.BuildError, "^trough: cannot read the checkout"):
            B2.build(self.inputs, os.path.join(self.tmp, "out"))


class UnpinnedInputTest(unittest.TestCase):
    """`build` recomputes every pin, so an input with no pin at all must stop it: it would
    otherwise seal a normal-looking export that was never checked against anything. A failed
    `fetch` leaves exactly that state behind for the inputs it touched."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.inputs = make_inputs(self.tmp)
        self.out = os.path.join(self.tmp, "out")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def repin(self, **overrides):
        with open(os.path.join(self.inputs, "PINS.json")) as f:
            pins = json.load(f)["inputs"]
        pins.update(overrides)
        with open(os.path.join(self.inputs, "PINS.json"), "w") as f:
            json.dump({"inputs": pins}, f)

    def test_an_unpinned_input_refuses(self):
        for producer, index, rel in ((B2, "tauceti_decls", "decls/tauceti.bmp"),
                                     (B3, "mathlib_decls", "decls/mathlib.bmp")):
            self.repin(trough={})
            with self.assertRaisesRegex(D.BuildError, "^trough: the pins carry no commit"):
                producer.build(self.inputs, self.out)
            self.repin(trough={"commit": GitTree(os.path.join(self.inputs, "trough")).commit},
                       **{index: {}})
            with self.assertRaisesRegex(D.BuildError,
                                        f"^{re.escape(rel)}: the pins carry no sha256"):
                producer.build(self.inputs, self.out)
            self.repin(**{index: {"sha256": sha256_file(os.path.join(self.inputs, rel))}})
            self.assertFalse(os.path.exists(self.out))

    def test_no_pins_file_refuses(self):
        os.remove(os.path.join(self.inputs, "PINS.json"))
        for producer in (B2, B3):
            with self.assertRaisesRegex(D.BuildError, "no PINS.json or PINS.txt"):
                producer.build(self.inputs, self.out)
        self.assertFalse(os.path.exists(self.out))


if __name__ == "__main__":
    unittest.main()
