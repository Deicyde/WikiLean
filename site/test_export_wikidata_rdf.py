#!/usr/bin/env python3
"""Offline tests for explicit D1 membership in the public concept exporter."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import export_wikidata_rdf as exporter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "brain"))
import d1_snapshot_bundle as snapshots


def article(slug: str) -> dict:
    return {
        "slug": slug, "wikipedia_title": slug.replace("_", " "),
        "display_title": "Cafe\u0301", "wikidata_qid": "Q999", "revid": 3,
        "latest_revid": 4, "last_upstream_check": None, "schema_version": 1,
        "version": 2, "n_formalized": None, "n_partial": None,
        "n_not_formalized": None, "created_at": 0, "updated_at": 1,
        "annotations": [
            {"status": "formalized", "mathlib": {"decl": "foo", "module": "Mathlib.Foo"}},
            {"status": "formalized", "mathlib": {"decl": "foo", "module": "Mathlib.Ignored"}},
            {"status": "formalized", "mathlib": {"decl": "bar"}},
            {"status": "partial", "mathlib": {"decl": "omit", "module": "Mathlib.Omit"}},
        ],
    }


class D1MembershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.annotations = self.root / "annotations"
        self.annotations.mkdir()
        self.d1 = self.root / "articles.jsonl"
        self.catalog = self.root / "catalog.jsonl"
        self.out = self.root / "out"
        self.w3c = self.root / "out_w3c"
        self.rows = [article("Zebra"), article("Alpha"), article("No_QID")]
        self.catalog.write_text(json.dumps({"title": "Alpha", "wikidata_qid": "Q1"}) + "\n"
                                + json.dumps({"title": "Zebra", "wikidata_qid": "Q2"}) + "\n")
        self.install()

    def install(self) -> None:
        for path in self.annotations.iterdir():
            path.unlink()
        lines = []
        for row in self.rows:
            raw = snapshots.contracts.canonical_artifact_json_bytes(row)
            (self.annotations / (row["slug"] + ".json")).write_bytes(raw)
            lines.append(raw + b"\n")
        self.d1.write_bytes(b"".join(lines))

    def args(self, *, strict: bool = True) -> list[str]:
        result = ["--annotations-dir", str(self.annotations), "--catalog", str(self.catalog),
                  "--out-dir", str(self.out), "--out-w3c-dir", str(self.w3c)]
        if strict:
            result += ["--d1-articles", str(self.d1), "--d1-articles-sha256",
                       hashlib.sha256(self.d1.read_bytes()).hexdigest()]
        return result

    def run_export(self, *, strict: bool = True) -> tuple[bytes, bytes, bytes]:
        with contextlib.redirect_stdout(io.StringIO()):
            exporter.main(self.args(strict=strict))
        return ((self.out / "concepts.html").read_bytes(),
                (self.out / "wikilean.ttl").read_bytes(),
                (self.w3c / "wikilean.ttl").read_bytes())

    def test_equivalent_complete_legacy_membership_has_exact_bytes(self) -> None:
        self.out.mkdir()
        for row in self.rows:
            (self.out / (row["slug"] + ".html")).write_text("fixture rendered article")
        old = self.run_export(strict=False)
        for row in self.rows:
            (self.out / (row["slug"] + ".html")).unlink()
        new = self.run_export()
        self.assertEqual(old, new)
        page, ttl, canonical_ttl = new
        self.assertEqual(ttl, canonical_ttl)
        self.assertIn(b'<a href="Alpha.html">Alpha</a>', page)
        self.assertLess(page.index(b'Alpha.html'), page.index(b'Zebra.html'))
        self.assertNotIn(b"Q999", page)  # Keep the existing catalog QID projection.
        self.assertNotIn(b"Mathlib.Ignored", ttl)
        self.assertNotIn(b'"omit"', ttl)
        self.assertEqual(ttl.count(b'wl:formalizedAs "foo"'), 2)
        self.assertNotIn(b"No_QID", page)

    def test_d1_output_ignores_ambient_html_but_default_keeps_legacy_semantics(self) -> None:
        before = self.run_export()
        (self.out / "Alpha.html").write_text("unrelated stale page")
        (self.out / "No_QID.html").write_text("unrelated stale page")
        self.assertEqual(before, self.run_export())
        legacy = self.run_export(strict=False)
        self.assertIn(b'<a href="Alpha.html">Alpha</a>', legacy[0])
        self.assertNotIn(b'<a href="Zebra.html">Zebra</a>', legacy[0])
        self.assertEqual(before[1:], legacy[1:])

    def test_default_paths_still_work_without_d1(self) -> None:
        with mock.patch.multiple(exporter, ANNOT=self.annotations, CATALOG=self.catalog,
                                 OUT=self.out, OUT_W3C=self.w3c), contextlib.redirect_stdout(io.StringIO()):
            exporter.main([])
        self.assertNotIn(b'href="Alpha.html"', (self.out / "concepts.html").read_bytes())

    def test_digest_is_required_and_detects_changed_object_before_output(self) -> None:
        args = self.args()
        self.d1.write_bytes(self.d1.read_bytes() + b"\n")
        with self.assertRaisesRegex(exporter.ExportInputError, "SHA-256 mismatch"):
            exporter.main(args)
        self.assertFalse(self.out.exists())
        for args in (["--d1-articles", str(self.d1)],
                     ["--d1-articles-sha256", "0" * 64],
                     ["--d1-articles", str(self.d1), "--d1-articles-sha256", "0" * 64]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                exporter.main(args)

    def test_exact_sidecar_correspondence_rejects_missing_extra_or_changed_data(self) -> None:
        for failure in ("missing", "extra", "changed", "format"):
            with self.subTest(failure=failure):
                self.install()
                path = self.annotations / "Alpha.json"
                if failure == "missing":
                    path.unlink()
                elif failure == "extra":
                    (self.annotations / "unused.txt").write_text("unexpected")
                elif failure == "format":
                    path.write_bytes(path.read_bytes() + b"\n")
                else:
                    row = copy.deepcopy(self.rows[1])
                    row["version"] += 1
                    path.write_bytes(snapshots.contracts.canonical_artifact_json_bytes(row))
                with self.assertRaises(exporter.ExportInputError):
                    exporter.main(self.args())
                self.assertFalse(self.out.exists())

    def test_canonical_row_schema_and_unique_safe_membership_are_mandatory(self) -> None:
        original = self.d1.read_bytes()
        changed = []
        for label, key, value in (("wrong type", "version", True),
                                   ("missing field", "revid", None),
                                   ("non NFC slug", "slug", "Cafe\u0301"),
                                   ("unsafe slug", "slug", "../escape"),
                                   ("reserved slug", "slug", "brain")):
            row = copy.deepcopy(self.rows[0])
            if label == "missing field":
                row.pop(key)
            else:
                row[key] = value
            changed.append((label, snapshots.contracts.canonical_artifact_json_bytes(row) + b"\n"))
        duplicate_key = original.replace(b'"version":2', b'"version":2,"version":2', 1)
        changed += [("duplicate slug", original + original.splitlines(keepends=True)[0]),
                    ("duplicate key", duplicate_key), ("empty", b""),
                    ("not canonical", original.replace(b'{"', b'{ "', 1)),
                    ("missing newline", original[:-1])]
        for label, raw in changed:
            with self.subTest(failure=label):
                self.d1.write_bytes(raw)
                with self.assertRaises((exporter.ExportInputError, snapshots.HarvestError,
                                        snapshots.contracts.VerificationError)):
                    exporter.main(self.args())
                self.assertFalse(self.out.exists())

    def test_case_collisions_rejected_even_on_case_sensitive_filesystem(self) -> None:
        row = article("alpha")
        self.d1.write_bytes(self.d1.read_bytes() + snapshots.contracts.canonical_artifact_json_bytes(row) + b"\n")
        with self.assertRaisesRegex(exporter.ExportInputError, "colliding"):
            exporter.main(self.args())

    def test_symlinks_and_special_inputs_are_rejected_without_blocking(self) -> None:
        path = self.annotations / "Alpha.json"
        raw = path.read_bytes()
        target = self.root / "real.json"
        target.write_bytes(raw)
        path.unlink()
        path.symlink_to(target)
        with self.assertRaises(OSError):
            exporter.main(self.args())
        path.unlink()
        os.mkfifo(path)
        with self.assertRaisesRegex(exporter.ExportInputError, "bounded regular"):
            exporter.main(self.args())
        self.assertFalse(self.out.exists())

    def test_outputs_cannot_overwrite_inputs_or_follow_output_symlink(self) -> None:
        args = self.args()
        args[args.index("--out-dir") + 1] = str(self.annotations)
        with self.assertRaisesRegex(exporter.ExportInputError, "overlaps"):
            exporter.main(args)
        self.out.mkdir()
        protected = self.root / "protected"
        protected.write_bytes(b"sentinel")
        (self.out / "concepts.html").symlink_to(protected)
        with self.assertRaisesRegex(exporter.ExportInputError, "symlink"):
            exporter.main(self.args())
        self.assertEqual(protected.read_bytes(), b"sentinel")
        (self.out / "concepts.html").unlink()
        os.link(protected, self.out / "concepts.html")
        with self.assertRaisesRegex(exporter.ExportInputError, "hardlink"):
            exporter.main(self.args())
        self.assertEqual(protected.read_bytes(), b"sentinel")
        (self.out / "concepts.html").unlink()
        os.mkfifo(self.out / "concepts.html")
        with self.assertRaisesRegex(exporter.ExportInputError, "regular"):
            exporter.main(self.args())

    def test_render_uses_captured_rows_not_a_second_sidecar_read(self) -> None:
        expected = self.run_export()
        original = exporter.load_d1_articles
        def change_after_capture(*args):
            rows = original(*args)
            (self.annotations / "Alpha.json").write_text("corrupt after capture")
            return rows
        with mock.patch.object(exporter, "load_d1_articles", side_effect=change_after_capture):
            self.assertEqual(expected, self.run_export())

    def test_reserved_set_matches_worker_routes(self) -> None:
        worker = (Path(__file__).resolve().parents[1] / "wiki/src/index.ts").read_text()
        block = worker.split("const RESERVED = new Set([", 1)[1].split("]);", 1)[0]
        self.assertEqual(exporter.RESERVED_ARTICLE_SLUGS,
                         set(re.findall(r'^\s*"([^"]+)"', block, re.MULTILINE)))

    def test_actual_isolated_cli_uses_explicit_paths(self) -> None:
        result = subprocess.run([sys.executable, "-I", "-S", str(Path(exporter.__file__)), *self.args()],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2 concepts, 4 formalized declarations", result.stdout)
        self.assertIn(b'href="Zebra.html"', (self.out / "concepts.html").read_bytes())


if __name__ == "__main__":
    unittest.main()
