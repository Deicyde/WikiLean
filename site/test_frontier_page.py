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

    def test_release_selector_pins_one_immutable_page_session(self):
        self.assertIn('const RELEASE_SELECTOR_URL = "/assets/brain/current.json"', self.html)
        self.assertIn('fetch(RELEASE_SELECTOR_URL, {cache: "no-cache"})', self.html)
        self.assertEqual(self.html.count("await selectRelease()"), 1)
        self.assertIn('const releaseBase = "/assets/brain/releases/" + match[1] + "/"', self.html)
        self.assertIn('"wikilean.release.v1", releaseManifest, ["release_id", "attestations", "created_at"]', self.html)
        self.assertIn('crypto.subtle.digest("SHA-256", bytes)', self.html)
        self.assertIn('"wikilean\\0" + domain + "\\0canonical-json-v1\\0"', self.html)
        self.assertNotIn('"wikilean\\\\0" + domain', self.html)
        self.assertIn('BASE = releaseBase + "cells/"', self.html)
        self.assertIn('SOURCES_URL = releaseBase + "sources.json"', self.html)
        self.assertIn('const required = ["schema", "release_id", "release", "manifest"]', self.html)
        self.assertIn('Object.keys(selector).some(key => !allowed.has(key))', self.html)
        self.assertIn('selector.previous_release_id === selector.release_id', self.html)
        self.assertIn('"audited_at" in selector', self.html)
        self.assertIn('const previousKeys = ["previous_release_id", "previous_release", "previous_manifest"]', self.html)
        self.assertIn('releaseManifest.release_id !== selector.release_id', self.html)
        self.assertIn('releaseEl.textContent = `release ${RELEASE_HEX.slice(0, 12)}`', self.html)
        self.assertIn("releaseEl.title = RELEASE_ID", self.html)
        self.assertNotIn('const BASE = "/assets/brain/cells/"', self.html)
        self.assertNotIn('const SOURCES_URL = "/assets/brain/sources.json"', self.html)
        self.assertNotIn("selector_id", self.html)
        self.assertNotIn('"updated_at" in selector', self.html)
        self.assertNotIn("await fetchManifest(); } catch { return null; }", self.html)

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
