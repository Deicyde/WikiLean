#!/usr/bin/env python3
"""Tests for bot/split.py's trim against a local mathlib-shaped repository (no network, no lake)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPLIT = HERE / "split.py"
BRANCH = "wikilean/wikidata-batch-9"
IMPORT = "public import Mathlib.Tactic.CrossRefAttribute\n"
BATCH = IMPORT + "\n@[wikidata Q12]\ntheorem b : True := trivial\n\n@[simp, wikidata Q7]\ntheorem c : True := trivial\n"


class TrimTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.tmp),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "wikilean-bot",
            "GIT_AUTHOR_EMAIL": "bot@example.invalid",
            "GIT_COMMITTER_NAME": "wikilean-bot",
            "GIT_COMMITTER_EMAIL": "bot@example.invalid",
        }
        self.origin = self.tmp / "origin.git"
        self.git("init", "--quiet", "--bare", "-b", "master", str(self.origin), cwd=self.tmp)
        self.seed = self.tmp / "seed"
        self.git("init", "--quiet", "-b", "master", str(self.seed), cwd=self.tmp)
        # master already carries Q12 elsewhere, and Q123 starts with Q12
        self.commit("master", {
            "Mathlib/Upstream.lean": IMPORT + "\n@[wikidata Q12]\ntheorem up : True := trivial\n",
            "Mathlib/Longer.lean": IMPORT + "\n@[wikidata Q123]\ntheorem longer : True := trivial\n",
            "Mathlib/Batch.lean": "theorem b : True := trivial\n\ntheorem c : True := trivial\n",
        })
        self.git("checkout", "--quiet", "-b", BRANCH, cwd=self.seed)
        self.commit(BRANCH, {"Mathlib/Batch.lean": BATCH})
        # the poller's clone: on master, with the fork as origin and an upstream remote
        self.mathlib = self.tmp / "mathlib"
        self.git("clone", "--quiet", str(self.origin), str(self.mathlib), cwd=self.tmp)
        self.git("remote", "add", "upstream", str(self.origin), cwd=self.mathlib)

    def git(self, *args, cwd):
        r = subprocess.run(["git", *args], cwd=cwd, env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"git {args}: {r.stderr}")
        return r.stdout.strip()

    def commit(self, branch, files):
        for rel, text in files.items():
            path = self.seed / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.git("add", *files, cwd=self.seed)
        self.git("commit", "--quiet", "-m", branch, cwd=self.seed)
        self.git("push", "--quiet", str(self.origin), branch, cwd=self.seed)

    def split(self, recycle):
        return subprocess.run([sys.executable, str(SPLIT), "--mathlib", str(self.mathlib), "--branch", BRANCH,
                               "--recycle", recycle, "--apply", "--no-build"],
                              env=self.env, capture_output=True, text=True)

    def pushed(self, rel):
        return self.git("show", f"{BRANCH}:{rel}", cwd=self.origin)

    def test_trims_only_the_files_the_pr_tagged(self):
        r = self.split("Q12=Mathlib/Batch.lean,Q7=Mathlib/Batch.lean")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.pushed("Mathlib/Batch.lean"),
                         "theorem b : True := trivial\n\n@[simp]\ntheorem c : True := trivial")
        self.assertIn("@[wikidata Q12]", self.pushed("Mathlib/Upstream.lean"))
        self.assertIn("@[wikidata Q123]", self.pushed("Mathlib/Longer.lean"))

    def test_a_tag_missing_from_its_file_fails_without_pushing(self):
        before = self.git("rev-parse", BRANCH, cwd=self.origin)
        for recycle in ("Q12=Mathlib/Longer.lean",  # only Q123 there
                        "Q99=Mathlib/Batch.lean",
                        "Q12=Mathlib/Gone.lean"):
            with self.subTest(recycle=recycle):
                r = self.split(recycle)
                self.assertNotEqual(r.returncode, 0, r.stdout)
                self.assertIn("refusing to trim", r.stderr)
                self.assertEqual(self.git("rev-parse", BRANCH, cwd=self.origin), before)

    def test_a_tag_left_after_the_edit_fails_without_pushing(self):
        self.commit(BRANCH, {"Mathlib/Batch.lean": BATCH + "\n@[wikidata Q12]\ntheorem d : True := trivial\n"})
        before = self.git("rev-parse", BRANCH, cwd=self.origin)
        r = self.split("Q12=Mathlib/Batch.lean")
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("TRIM INCOMPLETE: Q12", r.stderr)
        self.assertEqual(self.git("rev-parse", BRANCH, cwd=self.origin), before)
        self.assertEqual(self.git("status", "--porcelain", cwd=self.mathlib), "")

    def test_bare_qids_are_refused(self):
        r = self.split("Q12")
        self.assertEqual(r.returncode, 2)
        self.assertIn("QID=FILE", r.stderr)


if __name__ == "__main__":
    unittest.main()
