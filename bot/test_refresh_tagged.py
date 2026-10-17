#!/usr/bin/env python3
"""refresh_tagged.py fetches upstream master as one full shallow pack, so the grep
that follows never fetches blobs one at a time in the poller's blob:none clone."""
from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import refresh_tagged  # noqa: E402


class RefreshTaggedTest(unittest.TestCase):
    def test_the_fetch_refetches_every_blob_before_the_grep(self):
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd)
            out = "FETCH_HEAD:Mathlib/A.lean:@[wikidata Q42]\n" if "grep" in cmd else ""
            return subprocess.CompletedProcess(cmd, 0, out, "")

        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(refresh_tagged.subprocess, "run", run), \
                mock.patch.object(refresh_tagged, "OUT", Path(td) / "tagged.txt"), \
                mock.patch.object(refresh_tagged, "PAIRS_OUT", Path(td) / "pairs.txt"), \
                mock.patch.object(sys, "argv", ["refresh_tagged.py", "--mathlib", td]), \
                contextlib.redirect_stdout(io.StringIO()):
            refresh_tagged.main()
            self.assertEqual((Path(td) / "tagged.txt").read_text(), "Q42\n")
            self.assertEqual((Path(td) / "pairs.txt").read_text(), "Q42\tMathlib/A.lean\n")
        self.assertEqual(calls[0][3:], ["fetch", "--depth=1", "--refetch", refresh_tagged.UPSTREAM, "master"])
        self.assertEqual(calls[1][3:5], ["grep", "-IE"])
        self.assertIn("FETCH_HEAD", calls[1])


if __name__ == "__main__":
    unittest.main()
