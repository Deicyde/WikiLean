#!/usr/bin/env python3
"""Contract checks for the generated Frontier queue UI."""
from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BUILDER = HERE / "build_brain_page.py"
PAGE = HERE / "out" / "brain.html"


class FrontierPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run(["python3", str(BUILDER)], cwd=ROOT, check=True,
                       capture_output=True, text=True)
        cls.html = PAGE.read_text(encoding="utf-8")

    def test_candidate_first_sort_is_primary_in_every_mode(self):
        self.assertIn("const bySuitability = (a, b)", self.html)
        self.assertRegex(
            self.html,
            r'if \(flSort === "az"\)\s+rows\.sort\(\(a, b\) => bySuitability\(a, b\) \|\|',
        )
        self.assertRegex(
            self.html,
            r'else if \(flSort === "evidence"\)\s+rows\.sort\(\(a, b\) => bySuitability\(a, b\) \|\|',
        )
        self.assertRegex(
            self.html,
            r'else\s+rows\.sort\(\(a, b\) => bySuitability\(a, b\) \|\|',
        )

    def test_assessment_is_visible_and_deprioritized_rows_are_not_filtered(self):
        self.assertIn(">assessment</span>", self.html)
        self.assertIn("candidates · ${reviewN.toLocaleString()} review needed", self.html)
        self.assertIn("Every structural frontier cell remains searchable", self.html)
        self.assertIn('class="flsuit ${row.suitability.candidate ?', self.html)
        self.assertNotIn("if (!suitability.candidate) continue", self.html)

    def test_library_rescoring_does_not_mutate_suitability(self):
        score_cells = self.html.index("function scoreCells(")
        active_prox = self.html.index("function activeProxFor(")
        suitability = self.html.index("const SUITABILITY_LABELS")
        self.assertNotIn("suitability", self.html[score_cells:active_prox])
        self.assertGreater(suitability, active_prox)

    def test_generated_inline_script_parses(self):
        scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", self.html,
                             flags=re.DOTALL)
        self.assertGreaterEqual(len(scripts), 2)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            for script in scripts:
                fh.write(script)
                fh.write("\n")
            script_path = Path(fh.name)
        self.addCleanup(script_path.unlink, missing_ok=True)
        result = subprocess.run(["node", "--check", str(script_path)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
