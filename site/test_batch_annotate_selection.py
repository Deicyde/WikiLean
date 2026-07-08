#!/usr/bin/env python3
"""Selection tests for batch_annotate.py.

These are intentionally pure/local: they do not fetch Wikipedia, call agents, or
render pages. They pin the queue semantics so generated site/out HTML stops
acting like the source of truth for annotation work.

Run:
    python3 site/test_batch_annotate_selection.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch_annotate as ba  # noqa: E402


def article(title: str) -> dict:
    return {"title": title}


class BatchAnnotateSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.old_annot = ba.ANNOT
        self.old_out = ba.OUT
        ba.ANNOT = root / "annotations"
        ba.OUT = root / "out"
        ba.ANNOT.mkdir()
        ba.OUT.mkdir()

    def tearDown(self) -> None:
        ba.ANNOT = self.old_annot
        ba.OUT = self.old_out
        self.tmp.cleanup()

    def write_annotations(self, slug: str, annotations: list[dict]) -> None:
        payload = {
            "slug": slug,
            "wikipedia_title": slug.replace("_", " "),
            "display_title": slug.replace("_", " "),
            "schema_version": 3,
            "annotations": annotations,
        }
        (ba.ANNOT / f"{slug}.json").write_text(json.dumps(payload), encoding="utf-8")

    def write_render(self, slug: str) -> None:
        (ba.OUT / f"{slug}.html").write_text("<html></html>", encoding="utf-8")

    def selected(self, mode: str, titles: list[str]) -> list[str]:
        articles, _counts = ba.select_articles([article(t) for t in titles], mode)
        return [ba.make_slug(a["title"]) for a in articles]

    def test_new_mode_uses_annotations_not_render_cache(self) -> None:
        self.write_annotations("Existing", [{"kind": "definition"}])

        self.assertEqual(
            self.selected("new", ["Existing", "Missing"]),
            ["Missing"],
        )

    def test_formalize_mode_selects_nonempty_without_status(self) -> None:
        self.write_annotations("Needs_Status", [{"kind": "definition"}])
        self.write_annotations("Already_Statused", [{"status": "formalized"}])
        self.write_annotations("Empty", [])

        self.assertEqual(
            self.selected(
                "formalize",
                ["Needs Status", "Already Statused", "Empty", "Absent"],
            ),
            ["Needs_Status"],
        )

    def test_render_missing_mode_is_render_only_queue(self) -> None:
        self.write_annotations("Needs_Render", [{"status": "formalized"}])
        self.write_annotations("Already_Rendered", [{"status": "formalized"}])
        self.write_render("Already_Rendered")

        self.assertEqual(
            self.selected(
                "render-missing",
                ["Needs Render", "Already Rendered", "No Annotation"],
            ),
            ["Needs_Render"],
        )

    def test_regen_mode_selects_every_loaded_article(self) -> None:
        self.write_annotations("Existing", [{"status": "formalized"}])

        self.assertEqual(
            self.selected("regen", ["Existing", "Missing"]),
            ["Existing", "Missing"],
        )


if __name__ == "__main__":
    unittest.main()
