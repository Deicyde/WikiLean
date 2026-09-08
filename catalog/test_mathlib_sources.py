#!/usr/bin/env python3
"""Hermetic regression tests for portable, honestly pinned Mathlib observations."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harvest_mathlib_tags as tags  # noqa: E402
import normalize_decl_renames as renames  # noqa: E402


class MathlibHarvestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "source"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "WikiLean fixture")
        self.git("config", "user.email", "test@wikilean.invalid")
        self.source = self.repo / "Mathlib/Example.lean"
        self.source.parent.mkdir()
        self.source.write_text(
            "namespace Example\n"
            "@[stacks 0ABC, wikidata Q123]\n"
            "theorem committed : True := by trivial\n"
            "@[kerodon 0XYZ]\n"
            "def missing := true\n"
            "end Example\n",
            encoding="utf-8",
        )
        self.git("add", "Mathlib/Example.lean")
        self.git("commit", "-qm", "fixture")
        self.commit = self.git("rev-parse", "HEAD")
        self.tree = self.git("rev-parse", "HEAD^{tree}")
        self.oracle = self.root / "oracle.json"
        self.oracle.write_text(json.dumps({
            "declarations": {"Example.committed": {"kind": "theorem"}},
            # A self-claimed revision is insufficient and must not be copied
            # into a purported oracle/source coherence assertion.
            "mathlib_commit": self.commit,
        }), encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(self, *arguments: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(self.repo), *arguments], stderr=subprocess.PIPE,
        ).decode("utf-8").strip()

    def test_harvest_reads_committed_blobs_and_records_independent_evidence(self) -> None:
        self.source.write_text("@[wikidata Q999]\ndef staged := true\n", encoding="utf-8")
        self.git("add", "Mathlib/Example.lean")
        (self.repo / "Mathlib/Untracked.lean").write_text(
            "@[wikidata Q888]\ndef untracked := true\n", encoding="utf-8",
        )
        records, problems = tags.harvest(self.repo, self.oracle)
        meta = records[0]["_meta"]
        self.assertEqual(meta["commit"], self.commit)
        self.assertEqual(meta["tree"], self.tree)
        self.assertEqual(meta["source_root"], "mathlib")
        self.assertEqual(meta["harvested_from"], "Mathlib")
        self.assertEqual(meta["oracle"], {
            "root": "decl_oracle", "path": "declaration-data.json",
            "sha256": hashlib.sha256(self.oracle.read_bytes()).hexdigest(),
            "bytes": self.oracle.stat().st_size,
            "mathlib_commit": None, "revision_status": "unbound",
        })
        self.assertEqual({row["tag"] for row in records[1:]}, {"Q123", "0ABC", "0XYZ"})
        self.assertEqual({row["file"] for row in records[1:]}, {"Mathlib/Example.lean"})
        self.assertEqual(meta["unverified_rows"], 1)
        self.assertEqual(len(problems), 1)
        self.assertNotIn(str(self.root), json.dumps(records))

    def test_relocation_and_checkout_root_spelling_do_not_change_bytes(self) -> None:
        # A valid worktree may itself be named Mathlib. It must not be mistaken
        # for its library subdirectory when resolving the legacy argument form.
        moved = self.root / "another-location" / "Mathlib"
        shutil.copytree(self.repo, moved)
        moved_oracle = self.root / "another-oracle.json"
        shutil.copyfile(self.oracle, moved_oracle)
        first, _ = tags.harvest(self.repo, self.oracle)
        second, _ = tags.harvest(moved / "Mathlib", moved_oracle)
        self.assertEqual(second, tags.harvest(moved, moved_oracle)[0])
        first_out, second_out = self.root / "first.jsonl", self.root / "second.jsonl"
        tags.write_rows(first_out, first)
        tags.write_rows(second_out, second)
        self.assertEqual(first_out.read_bytes(), second_out.read_bytes())

    def test_oracle_digest_binds_exact_bytes_not_only_parsed_declarations(self) -> None:
        first, _ = tags.harvest(self.repo, self.oracle)
        self.oracle.write_bytes(self.oracle.read_bytes() + b"\n")
        second, _ = tags.harvest(self.repo, self.oracle)
        self.assertEqual(first[1:], second[1:])
        self.assertNotEqual(first[0]["_meta"]["oracle"]["sha256"],
                            second[0]["_meta"]["oracle"]["sha256"])

    def test_bad_oracle_preserves_previous_published_output(self) -> None:
        output = self.root / "output.jsonl"
        output.write_bytes(b"previous generation\n")
        for content in ("not JSON", "{}", '{"declarations": []}', '{"declarations": {}}'):
            with self.subTest(content=content):
                self.oracle.write_text(content, encoding="utf-8")
                self.assertEqual(tags.main([
                    "--mathlib", str(self.repo), "--oracle", str(self.oracle),
                    "--output", str(output),
                ]), 1)
                self.assertEqual(output.read_bytes(), b"previous generation\n")

    def test_atomic_publication_failure_keeps_prior_bytes_and_removes_temporary(self) -> None:
        output = self.root / "output.jsonl"
        output.write_bytes(b"previous generation\n")
        records, _ = tags.harvest(self.repo, self.oracle)
        with mock.patch.object(tags.os, "replace", side_effect=OSError("injected")):
            with self.assertRaisesRegex(OSError, "injected"):
                tags.write_rows(output, records)
        self.assertEqual(output.read_bytes(), b"previous generation\n")
        self.assertFalse(list(self.root.glob(".output.jsonl.*")))

    def test_cli_accepts_host_configuration_without_hardcoded_machine_default(self) -> None:
        output = self.root / "output.jsonl"
        with mock.patch.dict(os.environ, {"WIKILEAN_MATHLIB": str(self.repo)}, clear=True):
            self.assertEqual(tags.main([
                "--oracle", str(self.oracle), "--output", str(output),
            ]), 0)
        self.assertTrue(output.is_file())


class RenameLocationsTests(unittest.TestCase):
    def records(self, prefix: str = "/old/host/mathlib") -> list[dict]:
        return [{"_meta": {"source": "reviewed agent workflow", "generated": "2026-07-16"}}, {
            "cited": "old", "current": "Example.current", "kind": "rename",
            "module": "Mathlib.Example", "file_line": prefix + "/Mathlib/Example.lean:42",
            "evidence": "Historical reviewed statement", "verified_by": "reviewer",
            "confidence": "high",
        }]

    def test_normalization_preserves_review_and_never_invents_source_revision(self) -> None:
        original = self.records()
        output = renames.normalize_records(original)
        self.assertEqual(output[1]["file_line"], "Mathlib/Example.lean:42")
        self.assertEqual(output[1]["file_line_root"], "mathlib")
        self.assertEqual({k: v for k, v in output[1].items() if k not in {"file_line", "file_line_root"}},
                         {k: v for k, v in original[1].items() if k != "file_line"})
        self.assertEqual(output[0]["_meta"]["generated"], "2026-07-16")
        self.assertFalse({"commit", "tree", "oracle", "mathlib_commit"}.intersection(output[0]["_meta"]))
        self.assertEqual(original[1]["file_line"], "/old/host/mathlib/Mathlib/Example.lean:42")
        self.assertEqual(output, renames.normalize_records(output))
        self.assertEqual(output, renames.normalize_records(self.records("/other/machine")))

    def test_archive_core_and_multiple_line_citations_keep_distinct_roots(self) -> None:
        records = self.records()
        for module, location, expected, root in (
            ("Archive.Example", "/checkout/Archive/Example.lean", "Archive/Example.lean", "mathlib"),
            ("Init.Data.Rat.Basic (Lean core, not Mathlib)",
             "/toolchain/src/lean/Init/Data/Rat/Basic.lean:33", "src/Init/Data/Rat/Basic.lean:33", "lean4"),
            ("Mathlib.Example", "/checkout/Mathlib/Example.lean:807 (namespace), :70 (retarget def)",
             "Mathlib/Example.lean:807 (namespace), :70 (retarget def)", "mathlib"),
        ):
            with self.subTest(module=module):
                records[1].update(module=module, file_line=location)
                output = renames.normalize_records(records)
                self.assertEqual(output[1]["file_line"], expected)
                self.assertEqual(output[1]["file_line_root"], root)
                self.assertEqual(output, renames.normalize_records(output))

    def test_mismatched_and_ambiguous_locations_fail_closed(self) -> None:
        for location in (
            "/host/Mathlib/Other.lean:42", "/host/../Mathlib/Example.lean:42",
            "relative/Mathlib/Example.lean:42", "//server/Mathlib/Example.lean:42",
            "/host/Mathlib/Example.lean:0", "/host/Mathlib/Example.lean:-1",
            "/host/Mathlib/Example.lean:42\n", "C:\\Mathlib\\Example.lean:42",
        ):
            with self.subTest(location=location), self.assertRaises(ValueError):
                renames.normalize_location(location, "Mathlib.Example")

    def test_existing_root_conflict_is_not_silently_overwritten(self) -> None:
        records = self.records()
        records[1]["file_line_root"] = "lean4"
        with self.assertRaisesRegex(ValueError, "disagrees"):
            renames.normalize_records(records)

    def test_cli_requires_separate_output_and_preserves_prior_on_invalid_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "reviewed.jsonl"
            output = Path(directory) / "normalized.jsonl"
            tags.write_rows(source, self.records())
            original = source.read_bytes()
            self.assertEqual(renames.main(["--input", str(source), "--output", str(source)]), 1)
            self.assertEqual(source.read_bytes(), original)
            output.write_bytes(b"previous generation\n")
            records = self.records()
            records[1]["file_line"] = "/host/Mathlib/Wrong.lean:42"
            tags.write_rows(source, records)
            self.assertEqual(renames.main(["--input", str(source), "--output", str(output)]), 1)
            self.assertEqual(output.read_bytes(), b"previous generation\n")


if __name__ == "__main__":
    unittest.main()
