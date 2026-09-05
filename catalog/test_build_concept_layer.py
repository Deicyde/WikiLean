#!/usr/bin/env python3
"""Hermetic tests for deterministic concept-layer generation."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import build_concept_layer as builder


def tagged(
    title: str,
    qid: str | None,
    *,
    primary: str | None = None,
) -> dict:
    decls = []
    if primary is not None:
        decls.append({
            "decl": primary,
            "module": "Mathlib.Fixture",
            "confidence": "high",
        })
    return {
        "title": title,
        "wikidata_qid": qid,
        "class": "C",
        "importance": "High",
        "primary_decl": primary,
        "mathlib_decls": decls,
        "no_match_reason": None if primary else "not formalized",
    }


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


class ConceptLayerTest(unittest.TestCase):
    def test_output_is_clock_path_and_mtime_independent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_data = root / "first-input"
            second_data = root / "relocated-input"
            first_data.mkdir()
            second_data.mkdir()
            pilot = [
                tagged("Alpha", "Q2"),
                tagged("No QID", None),
            ]
            tier2 = [
                tagged("Beta", "Q1", primary="Fixture.beta"),
                tagged("Alpha alias", "Q2", primary="Fixture.alpha"),
            ]
            for data_dir in (first_data, second_data):
                write_jsonl(data_dir / "pilot_tagged.jsonl", pilot)
                write_jsonl(data_dir / "tier2_tagged.jsonl", tier2)
            for path in first_data.iterdir():
                os.utime(path, (1, 1))
            for path in second_data.iterdir():
                os.utime(path, (2_000_000_000, 2_000_000_000))

            first_out = root / "first.jsonl"
            second_out = root / "second.jsonl"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    builder.main(["--data-dir", str(first_data), "--out", str(first_out)]),
                    0,
                )
                self.assertEqual(
                    builder.main(["--data-dir", str(second_data), "--out", str(second_out)]),
                    0,
                )
            self.assertEqual(first_out.read_bytes(), second_out.read_bytes())
            rows = [json.loads(line) for line in first_out.read_text().splitlines()]
            self.assertEqual([row["qid"] for row in rows], ["Q1", "Q2"])
            self.assertTrue(all("built_at" not in row for row in rows))
            self.assertEqual(rows[1]["titles"], ["Alpha", "Alpha alias"])
            self.assertEqual(rows[1]["primary_decl"], "Fixture.alpha")

    def test_checked_in_artifact_matches_generator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "concept_layer.jsonl"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(builder.main(["--out", str(output)]), 0)
            self.assertEqual(output.read_bytes(), builder.OUT.read_bytes())

    def test_failed_atomic_publish_preserves_previous_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "concept_layer.jsonl"
            output.write_bytes(b"previous generation\n")
            with mock.patch.object(builder.os, "replace", side_effect=OSError("stop")):
                with self.assertRaisesRegex(OSError, "stop"):
                    builder.write_rows(output, [builder.build_record(tagged("A", "Q1"))])
            self.assertEqual(output.read_bytes(), b"previous generation\n")
            self.assertFalse(list(root.glob(".concept_layer.jsonl.*.tmp")))


if __name__ == "__main__":
    unittest.main()
