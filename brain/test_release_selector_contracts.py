#!/usr/bin/env python3
"""Cross-language conformance tests for public Brain release selectors."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import jsonschema

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "tools"))

import release_selector_contracts as contracts  # noqa: E402


class ReleaseSelectorContractsTest(unittest.TestCase):
    def test_cross_language_conformance_vectors(self) -> None:
        fixture = json.loads(
            (HERE / "authority/fixtures/release-selector-v2-conformance.json").read_text(
                encoding="utf-8"
            )
        )
        for case in fixture["cases"]:
            with self.subTest(case=case["name"]):
                if case["valid"]:
                    self.assertIs(
                        contracts.validate_release_selector(case["selector"]),
                        case["selector"],
                    )
                else:
                    with self.assertRaises(contracts.VerificationError):
                        contracts.validate_release_selector(case["selector"])

    def test_v2_schema_matches_executable_shape(self) -> None:
        schema = json.loads(
            (HERE / "authority/schemas/release-selector/v2.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            schema["required"],
            ["schema", "release_id", "release", "manifest_sha256", "manifest"],
        )
        self.assertEqual(
            schema["properties"]["schema"]["const"],
            contracts.RELEASE_SELECTOR_SCHEMA,
        )
        self.assertIn("previous_manifest_sha256", schema["properties"])

        fixture = json.loads(
            (HERE / "authority/fixtures/release-selector-v2-conformance.json").read_text(
                encoding="utf-8"
            )
        )
        validator = jsonschema.Draft202012Validator(schema)
        for case in fixture["cases"]:
            if (
                not case["valid"]
                or case["selector"].get("schema") != contracts.RELEASE_SELECTOR_SCHEMA
            ):
                continue
            with self.subTest(case=case["name"]):
                self.assertEqual(list(validator.iter_errors(case["selector"])), [])
        missing_digest = next(
            case["selector"]
            for case in fixture["cases"]
            if case["name"] == "missing-manifest-digest"
        )
        self.assertTrue(list(validator.iter_errors(missing_digest)))


if __name__ == "__main__":
    unittest.main()
