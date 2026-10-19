#!/usr/bin/env python3
"""refresh_tagged.py writes the merged-tag exclusion lists only from a plausible
grep: a count under half of the previous list's fails closed unless --allow-shrink."""
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
    def refresh(self, previous: list[str] | None, found: list[str], *flags: str):
        """Run main() against a stubbed fetch and grep that finds `found`; returns
        (exit message or None, OUT text, PAIRS_OUT text)."""
        grep_out = "".join(f"FETCH_HEAD:Mathlib/F{i}.lean:@[wikidata {q}]\n" for i, q in enumerate(found))

        def run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, grep_out if "grep" in cmd else "", "")

        with tempfile.TemporaryDirectory() as td:
            out, pairs = Path(td) / "tagged.txt", Path(td) / "pairs.txt"
            if previous is not None:
                out.write_text("\n".join(previous) + "\n")
                pairs.write_text("old pairs\n")
            exit_message = None
            with mock.patch.object(refresh_tagged.subprocess, "run", run), \
                    mock.patch.object(refresh_tagged, "OUT", out), \
                    mock.patch.object(refresh_tagged, "PAIRS_OUT", pairs), \
                    mock.patch.object(sys, "argv", ["refresh_tagged.py", "--mathlib", td, *flags]), \
                    contextlib.redirect_stdout(io.StringIO()):
                try:
                    refresh_tagged.main()
                except SystemExit as exc:
                    exit_message = str(exc.code)
            return exit_message, out.read_text() if out.exists() else None, \
                pairs.read_text() if pairs.exists() else None

    def test_a_first_refresh_writes_both_lists(self):
        message, out, pairs = self.refresh(None, ["Q2", "Q1"])
        self.assertIsNone(message)
        self.assertEqual(out, "Q1\nQ2\n")
        self.assertEqual(pairs, "Q1\tMathlib/F1.lean\nQ2\tMathlib/F0.lean\n")

    def test_a_small_drop_still_writes(self):
        previous = [f"Q{i}" for i in range(1, 11)]
        message, out, _ = self.refresh(previous, previous[:6])
        self.assertIsNone(message)
        self.assertEqual(out.split(), previous[:6])

    def test_a_count_under_half_of_the_previous_list_fails_closed(self):
        previous = [f"Q{i}" for i in range(1, 146)]
        message, out, pairs = self.refresh(previous, previous[:20])
        self.assertIn("only 20 tagged QIDs, under half of the previous 145", message)
        self.assertEqual(out.split(), previous)  # both lists untouched
        self.assertEqual(pairs, "old pairs\n")

    def test_allow_shrink_accepts_a_real_drop(self):
        previous = [f"Q{i}" for i in range(1, 146)]
        message, out, _ = self.refresh(previous, previous[:20], "--allow-shrink")
        self.assertIsNone(message)
        self.assertEqual(out.split(), previous[:20])


if __name__ == "__main__":
    unittest.main()
