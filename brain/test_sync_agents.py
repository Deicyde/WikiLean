#!/usr/bin/env python3
"""Hermetic tests for deterministic Brain agent candidate inputs."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sync_agents as agents  # noqa: E402


class LoadConceptsTest(unittest.TestCase):
    def load(self, descriptions: object):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            descriptions_path = tmp / "wikidata_descriptions.json"
            descriptions_path.write_text(json.dumps(descriptions), encoding="utf-8")
            nodes_path = tmp / "nodes.jsonl"
            nodes_path.write_text(
                "\n".join(
                    json.dumps(row)
                    for row in (
                        {"_meta": {"schema": 2}},
                        {"id": "Q1", "type": "concept", "label": "Alpha", "slug": "Alpha"},
                        {"id": "Q2", "type": "concept", "label": "Beta", "slug": "Beta"},
                        {"id": "decl:ignored", "type": "decl", "label": "Ignored"},
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            with (
                mock.patch.object(agents, "DESCRIPTIONS", descriptions_path),
                mock.patch.object(agents, "NODES", nodes_path),
            ):
                return agents.load_concepts()

    def test_reads_current_v2_description_envelope(self):
        concepts, index = self.load(
            {
                "_meta": {
                    "n_qids": 2,
                    "n_descriptions": 2,
                    "source": "fixture",
                },
                "descriptions": {
                    "Q1": "first description",
                    "Q2": "second description",
                },
            }
        )

        self.assertEqual(concepts["Q1"]["description"], "first description")
        self.assertEqual(concepts["Q2"]["description"], "second description")
        self.assertEqual(index["alpha"], ["Q1"])
        self.assertNotIn("_meta", concepts)
        self.assertNotIn("descriptions", concepts)

    def test_retains_legacy_flat_description_maps(self):
        concepts, _index = self.load(
            {
                "Q1": "legacy string",
                "Q2": {"description": "legacy object"},
            }
        )

        self.assertEqual(concepts["Q1"]["description"], "legacy string")
        self.assertEqual(concepts["Q2"]["description"], "legacy object")

    def test_malformed_description_payload_degrades_to_empty_descriptions(self):
        concepts, _index = self.load({"_meta": {}, "descriptions": []})

        self.assertEqual(concepts["Q1"]["description"], "")
        self.assertEqual(concepts["Q2"]["description"], "")

    def test_malformed_entry_does_not_discard_valid_descriptions(self):
        concepts, _index = self.load(
            {
                "_meta": {},
                "descriptions": {
                    "Q1": "valid",
                    "Q2": {"description": 7},
                    "Q3": None,
                },
            }
        )

        self.assertEqual(concepts["Q1"]["description"], "valid")
        self.assertEqual(concepts["Q2"]["description"], "")


if __name__ == "__main__":
    unittest.main()
