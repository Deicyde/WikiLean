#!/usr/bin/env python3
"""Tests for scripts/pipeline/links.py, the one format both repositories vendor.

New code. The pin is a fact about this checkout, so these tests assert it rather than
read it: an upgrade of the vendored Literature Links release fails here until it is
deliberate (docs/PIPELINE.md section 6, rule 4). The rest checks that the schema's bytes
are held to the provenance before anything is validated, and that the checker is the one
WikiLean already uses — one checker and one pin in this repository, not two.

Run: python3 scripts/pipeline/test_links.py
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import links  # noqa: E402

ROOT = HERE.parents[1]


def minimal_record() -> dict:
    """A valid Literature Links record: one html subject end, one lean object end."""
    return {
        "id": "enwiki-1-2::In-text #0123abcd",
        "subject": [{"type": "html",
                     "uri": "https://en.wikipedia.org/w/index.php?curid=1&oldid=2",
                     "version": "2",
                     "location": {"quote": {"exact": "A widget is a gadget with a handle."}},
                     "citation": {"id": "enwiki-1-2", "type": "webpage", "title": "Widget",
                                  "URL": "https://en.wikipedia.org/w/index.php?curid=1&oldid=2"}}],
        "object": [{"type": "lean", "uri": "https://github.com/example/lib",
                    "version": "0" * 40, "role": "key",
                    "location": {"declaration": "Widget", "module": "Lib.Widget"}}],
        "relation": "exactMatch",
    }


class PinTests(unittest.TestCase):
    """The vendored release, and what happens when the vendored bytes are not it."""

    def test_the_pin_is_literature_links_1_0_1(self):
        provenance = links.pin()
        self.assertEqual(provenance["tag"], "v1.0.1")
        self.assertEqual(provenance["version"], "1.0.1")
        self.assertEqual(provenance["commit"], "789af9af85ae7590d7149cb8312f09cfac0752b4")
        self.assertEqual(provenance["repository"],
                         "https://github.com/mathlib-initiative/literature-links")
        self.assertEqual(provenance["schema"], "schema/companion.schema.json")

    def test_the_pin_is_the_file_the_worker_and_the_store_builder_read(self):
        # One vendored copy: the Worker's validate.js, site/native_annotations.py and this
        # pipeline all read wiki/src/vendor/literature-links/.
        self.assertEqual(links.VENDOR, ROOT / "wiki" / "src" / "vendor" / "literature-links")
        self.assertTrue(links.SCHEMA.is_file())
        self.assertTrue(links.PROVENANCE.is_file())

    def test_the_schema_bytes_are_held_to_the_provenance(self):
        provenance = json.loads(links.PROVENANCE.read_text(encoding="utf-8"))
        self.assertEqual(provenance["sha256"],
                         __import__("hashlib").sha256(links.SCHEMA.read_bytes()).hexdigest())

    def test_a_drifted_schema_is_refused(self):
        original = links.SCHEMA
        try:
            links.SCHEMA = HERE / "test_links.py"  # any file that is not the schema
            with self.assertRaises(links.LinksProblem) as caught:
                links.pin()
            self.assertIn("provenance.json sha256", str(caught.exception))
        finally:
            links.SCHEMA = original

    def test_another_release_is_refused(self):
        original = links.PINNED_VERSION
        try:
            links.PINNED_VERSION = "2.0.0"
            with self.assertRaises(links.LinksProblem) as caught:
                links.pin()
            self.assertIn("not the pinned", str(caught.exception))
        finally:
            links.PINNED_VERSION = original


class ValidateTests(unittest.TestCase):
    """The checker: the vendored schema and the format's numeric invariants."""

    def test_a_minimal_record_validates(self):
        self.assertEqual(links.validate(minimal_record()), [])

    def test_a_record_without_a_subject_is_refused(self):
        record = minimal_record()
        del record["subject"]
        self.assertTrue(links.validate(record))

    def test_a_citation_without_an_id_is_refused(self):
        # docs/ARTICLES.md contract note 1: the vendored schema requires citation.id, which
        # the proposal's own example omits.
        record = minimal_record()
        del record["subject"][0]["citation"]["id"]
        self.assertTrue(links.validate(record))

    def test_a_reversed_line_interval_is_refused(self):
        record = minimal_record()
        record["object"][0]["location"]["lines"] = [40, 10]
        problems = links.validate(record)
        self.assertEqual(len(problems), 1)
        self.assertIn("line interval is reversed", problems[0])

    def test_validate_all_names_the_record(self):
        broken = minimal_record()
        del broken["object"][0]["type"]
        problems = links.validate_all([minimal_record(), broken])
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("enwiki-1-2::In-text #0123abcd: "))


if __name__ == "__main__":
    unittest.main(verbosity=2)
