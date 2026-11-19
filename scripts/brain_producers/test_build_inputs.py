"""The shared input guards of `build`: it is offline, it refuses an unpinned input, and a bad
input is a BuildError rather than a traceback. Offline; local git only."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from scripts.brain_producers import decls as D  # noqa: E402
from scripts.brain_producers.test_fetch_decls import FakeOpener  # noqa: E402
from scripts.brain_producers.testing import FIX  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
