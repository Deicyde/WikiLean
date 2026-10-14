#!/usr/bin/env python3
"""Offline tests for the local Literature Links/Brain boundary.

Projection/store tests run in ordinary CI without optional Literature Links. Reference tests
also run when it is installed; --require-reference makes absence a hard error.
"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from brain import store
from brain.ingest import companion

try:
    from literature_links import companion as reference
    from literature_links.discovery import collect
except ModuleNotFoundError:
    reference = None


def annotation():
    return {"id": "same-id", "source": {"type": "generic", "uri": "../source.md"},
            "location": {"profile": "urn:fixture:markdown", "unknown": [3, 1]},
            "target": "../formal.html#result", "extensions": {"custom": {"z": 2**80, "a": None}}}


def collection(path, documents):
    return SimpleNamespace(
        records=[SimpleNamespace(annotation=doc, path=path, pointer=f"/{i}",
                                 line=None, base_uri=path.as_uri())
                 for i, doc in enumerate(documents)],
        problems=[], files=[path])


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.shard = self.root / "shards" / "a.json"
        self.output = self.root / "local-brain"

    def import_fixture(self, documents=None):
        documents = documents if documents is not None else [annotation()]
        with patch.object(companion, "_collect", return_value=collection(self.shard, documents)), \
             patch.object(companion, "_validate_captured", side_effect=lambda value: value):
            return companion.import_companions([self.shard], source_key="fixture", output=self.output)

    def test_ordinary_brain_sqlite_reader(self):
        report = self.import_fixture()
        self.assertEqual(report["annotations"], 1)
        database = store.open_store(data_dir=self.output, backend="sqlite")
        self.addCleanup(database.close)
        self.assertEqual(list(database.iter_nodes()), [])
        target = (self.root / "formal.html").as_uri() + "#result"
        edges = list(database.iter_edges(endpoint=target, direction="in"))
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["kind"], "links")
        self.assertEqual(edges[0]["confidence"], "unreviewed")
        self.assertEqual(edges[0]["evidence"]["annotation"], annotation())

    def test_absent_and_endpoint_targets_are_indexed_without_inventing_references(self):
        absent = annotation(); del absent["target"]; absent["status"] = "not_formalized"
        endpoint = annotation(); endpoint["target"] = {"uri": "../formal.html#result", "pin": {"type": "repository", "url": "https://example.org/repo"}}
        self.import_fixture([absent, endpoint])
        rows = list(companion.iter_annotations(self.output))
        self.assertEqual([row["annotation"] for row in rows], [absent, endpoint])
        self.assertEqual(rows[0]["target"], companion.ABSENT_TARGET_KEY)
        self.assertEqual(rows[0]["resolution"]["target"], {"status": "absent"})
        self.assertEqual(rows[1]["target"], (self.root / "formal.html").as_uri() + "#result")
        self.assertEqual(rows[1]["resolution"]["target"]["status"], "resolved")
        self.assertEqual([row["annotation"] for row in companion.iter_annotations(self.output, target=companion.ABSENT_TARGET_KEY)], [absent])

    def test_selective_endpoint_uses_existing_sqlite_index(self):
        documents = []
        for i in range(200):
            doc = annotation()
            doc["target"] = f"urn:fixture:decl:{i}"
            documents.append(doc)
        self.import_fixture(documents)
        database = store.open_store(data_dir=self.output, backend="sqlite")
        self.addCleanup(database.close)
        target = "urn:fixture:decl:50"
        rows = list(database.iter_edges(endpoint=target, direction="in"))
        self.assertEqual(len(rows), 1)
        plan = database.connection.execute(
            "EXPLAIN QUERY PLAN SELECT payload_json FROM edges WHERE dst = ?", (target,)).fetchall()
        self.assertTrue(any("edges_dst_kind_idx" in row[3] for row in plan))

    def test_relative_resolution_preserves_rfc_components(self):
        base = "file:///a/b/c/d.json"
        cases = {
            "g": "file:///a/b/c/g", "./g": "file:///a/b/c/g",
            "g/": "file:///a/b/c/g/", "/g": "file:///g",
            "//g": "file://g", "?y": "file:///a/b/c/d.json?y",
            "#s": "file:///a/b/c/d.json#s", "g?y#s": "file:///a/b/c/g?y#s",
            ".": "file:///a/b/c/", "..": "file:///a/b/", "../g": "file:///a/b/g",
            "../../g": "file:///a/g", "../../../g": "file:///g",
            "../../../../g": "file:///g", "/./g": "file:///g", "/../g": "file:///g",
            "g/./h": "file:///a/b/c/g/h", "g/../h": "file:///a/b/c/h",
            "g?y/../x": "file:///a/b/c/g?y/../x",
            "g#s/../x": "file:///a/b/c/g#s/../x",
            "a//b": "file:///a/b/c/a//b", "a//../b": "file:///a/b/c/a/b",
            "#": "file:///a/b/c/d.json#", "?": "file:///a/b/c/d.json?",
            "?#": "file:///a/b/c/d.json?#", "g?#": "file:///a/b/c/g?#",
            "./%2E%2e/%2F%25": "file:///a/b/c/%2E%2e/%2F%25",
            "../café.tex#α": "file:///a/b/café.tex#α",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                key, resolution = companion._resolve_reference(base, value)
                self.assertEqual(key, expected)
                self.assertEqual(resolution, {"status": "resolved"})

    def test_absolute_identifiers_stay_verbatim(self):
        for value in ("file:foo", "FILE:foo", "urn:lean:My.result", "custom:a/../b",
                      "https://é.example/%2f#", "https://EXAMPLE.org/a/../b?", "https:g"):
            with self.subTest(value=value):
                key, resolution = companion._resolve_reference(self.shard.as_uri(), value)
                self.assertEqual(key, value)
                self.assertEqual(resolution, {"status": "absolute"})

    def test_malformed_authorities_preserve_occurrences_with_lookup_status(self):
        first, second = annotation(), annotation()
        first["source"]["uri"] = "https://[oops"
        second["source"]["uri"] = "//[oops"
        first["target"] = second["target"] = "file:foo"
        self.import_fixture([first, second])
        rows = list(companion.iter_annotations(self.output))
        self.assertEqual([row["annotation"] for row in rows], [first, second])
        self.assertEqual(rows[0]["source"], "https://[oops")
        self.assertTrue(rows[1]["source"].startswith("urn:wikilean:companion:unresolved:"))
        for row in rows:
            self.assertEqual(row["target"], "file:foo")
            self.assertEqual(row["resolution"]["source"]["status"], "unresolved")
            self.assertEqual(row["resolution"]["target"]["status"], "absolute")
            self.assertEqual(list(companion.iter_annotations(self.output, source=row["source"])), [row])

    def test_unresolved_relative_lookup_keys_are_scoped_to_carrier(self):
        a, status = companion._resolve_reference("file:///a/shard.json", "//[oops")
        b, _ = companion._resolve_reference("file:///b/shard.json", "//[oops")
        again, _ = companion._resolve_reference("file:///a/shard.json", "//[oops")
        self.assertEqual(a, again)
        self.assertNotEqual(a, b)
        self.assertEqual(status, {"status": "unresolved", "reason": "unsupported_authority_syntax"})

    def test_duplicates_and_order_preserved(self):
        self.import_fixture([annotation(), annotation()])
        rows = list(companion.iter_annotations(self.output))
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["annotation"] for row in rows], [annotation(), annotation()])
        self.assertNotEqual(rows[0]["origin"], rows[1]["origin"])
        self.assertEqual(list(rows[0]["annotation"]["extensions"]["custom"]), ["z", "a"])

    def test_bases_are_containing_shards_and_raw_references_survive(self):
        self.import_fixture()
        row, = companion.iter_annotations(self.output)
        self.assertEqual(row["origin"], {"base_uri": self.shard.as_uri(), "pointer": "/0"})
        self.assertEqual(row["source"], (self.root / "source.md").as_uri())
        self.assertEqual(row["annotation"]["source"]["uri"], "../source.md")
        self.assertFalse((self.root / "source.md").exists())
        self.assertFalse((self.root / "formal.html").exists())

    def test_query_both_endpoints_and_misses(self):
        different = annotation()
        different["target"] = "urn:fixture:other"
        self.import_fixture([annotation(), different])
        row, = companion.iter_annotations(self.output, target="urn:fixture:other")
        self.assertEqual(row["annotation"], different)
        self.assertEqual(list(companion.iter_annotations(self.output, source="urn:missing")), [])
        self.assertEqual(list(companion.iter_annotations(self.output, source=row["source"],
                                                        target="urn:fixture:other")), [row])

    def test_invalid_batch_cannot_write_partial_valid_records(self):
        batch = collection(self.shard, [annotation()])
        batch.problems = [SimpleNamespace(code="SCHEMA", path="bad.json", message="bad target")]
        with patch.object(companion, "_collect", return_value=batch):
            with self.assertRaisesRegex(companion.ImportError, "nothing imported"):
                companion.import_companions([self.shard], source_key="test", output=self.output)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_empty_batch_cannot_publish(self):
        with self.assertRaisesRegex(companion.ImportError, "No annotations"):
            self.import_fixture([])
        self.assertFalse(self.output.exists())

    def test_reject_existing_output_unchanged(self):
        self.output.mkdir()
        marker = self.output / "keep"
        marker.write_text("existing")
        with self.assertRaisesRegex(companion.ImportError, "already exists"):
            self.import_fixture()
        self.assertEqual(marker.read_text(), "existing")

    def test_dangling_output_symlink_rejected(self):
        self.output.symlink_to(self.root / "absent", target_is_directory=True)
        with self.assertRaisesRegex(companion.ImportError, "already exists"):
            self.import_fixture()
        self.assertTrue(self.output.is_symlink())

    def test_concurrent_empty_directory_is_never_replaced(self):
        publish = companion._rename_no_replace
        def race(source, destination):
            destination.mkdir()
            publish(source, destination)
        with patch.object(companion, "_rename_no_replace", side_effect=race):
            with self.assertRaises(FileExistsError):
                self.import_fixture()
        self.assertTrue(self.output.is_dir())
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual(list(self.root.iterdir()), [self.output])

    def test_unsupported_publication_platform_fails_closed(self):
        with patch.object(companion.sys, "platform", "unsupported"):
            with self.assertRaisesRegex(companion.ImportError, "unavailable"):
                self.import_fixture()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_old_reference_version_fails_with_upgrade_message(self):
        with patch.dict(sys.modules, {"literature_links": SimpleNamespace(__version__="0.9.0")}):
            with self.assertRaisesRegex(companion.ImportError, "found 0.9.0"):
                companion._collect([], include=None, exclude=[])

    def test_build_failure_cleans_stage(self):
        with patch.object(store, "write_sqlite_from_jsonl", side_effect=store.StoreError("failed")):
            with self.assertRaisesRegex(store.StoreError, "failed"):
                self.import_fixture()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_invalid_source_key_before_discovery(self):
        with patch.object(companion, "_collect") as mock:
            with self.assertRaisesRegex(companion.ImportError, "source-key"):
                companion.import_companions([], source_key="../bad", output=self.output)
            mock.assert_not_called()

    def test_missing_reference_is_actionable_never_fallback(self):
        with patch.dict(sys.modules, {"literature_links.discovery": None}):
            with self.assertRaisesRegex(companion.ImportError, "Validation is required"):
                companion._collect([], include=None, exclude=[])

    def test_read_generation_cannot_be_modified_silently(self):
        self.import_fixture()
        with (self.output / "edges_links.jsonl").open("a") as handle:
            handle.write("\n")
        with self.assertRaises(store.StaleSnapshotError):
            list(companion.iter_annotations(self.output))

    def test_deterministic_generation_for_same_origins_and_values(self):
        report1 = self.import_fixture()
        second = self.root / "second"
        self.output = second
        report2 = self.import_fixture()
        self.assertEqual(report1["generation"], report2["generation"])

    def test_query_cli_recovers_annotation_without_fca_installed(self):
        self.import_fixture()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(companion.main(["query", "--data-dir", str(self.output)]), 0)
        row = json.loads(stdout.getvalue())
        self.assertEqual(row["annotation"], annotation())


@unittest.skipIf(reference is None, "Optional Literature Links package is not installed")
class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / "annotations"
        self.input.mkdir()
        self.output = self.root / "brain"

    def write(self, name, value):
        path = self.input / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def ingest(self, **kwargs):
        return companion.import_companions([self.input], source_key="reference", output=self.output, **kwargs)

    def test_all_modes_opaque_extensions_and_no_location(self):
        docs = []
        for mode, location in (("pdf", {"page": 2, "rect": [.1, .2, .3, .4]}),
                               ("latex", {"label": "thm:test"}),
                               ("html", {"fragment": "result"}),
                               ("generic", {}), ("generic", None)):
            doc = annotation()
            doc["source"]["type"] = mode
            if location is not None:
                doc["location"] = location
            else:
                del doc["location"]
            docs.append(doc)
        self.write("all.json", docs)
        self.ingest()
        recovered = [row["annotation"] for row in companion.iter_annotations(self.output)]
        self.assertEqual(recovered, docs)
        for doc in recovered:
            self.assertFalse(reference.check(doc).problems)

    def test_malformed_json_and_semantic_rect_reject_entire_batch(self):
        self.write("a.json", annotation())
        bad = annotation()
        bad["source"]["type"] = "pdf"
        bad["location"] = {"page": 1, "rect": [.9, 0, .2, 1]}
        bad_path = self.write("bad.json", bad)
        with self.assertRaisesRegex(companion.ImportError, "PDF_RECT"):
            self.ingest()
        self.assertFalse(self.output.exists())
        bad_path.write_text('{"target":"urn:x","target":"urn:x"}')
        with self.assertRaisesRegex(companion.ImportError, "JSON"):
            self.ingest()
        self.assertFalse(self.output.exists())

    def test_jsonl_lines_and_overlapping_roots(self):
        path = self.input / "links.ndjson"
        path.write_text("\n" + json.dumps(annotation()) + "\n\n" + json.dumps(annotation()) + "\n")
        result = companion.import_companions([self.input, path], source_key="lines", output=self.output)
        self.assertEqual(result["annotations"], 2)
        rows = list(companion.iter_annotations(self.output))
        self.assertEqual([row["origin"]["line"] for row in rows], [2, 4])
        self.assertEqual([row["origin"]["pointer"] for row in rows], ["", ""])

    def test_explicit_selection_does_not_traverse_arbitrary_nested_data(self):
        self.write("nested/link.json", annotation())
        self.write("metadata.json", {"data": annotation()})
        with self.assertRaisesRegex(companion.ImportError, "SCHEMA"):
            self.ingest()
        result = self.ingest(include=["nested/*.json"], exclude=["**/ignored.json"])
        self.assertEqual(result["annotations"], 1)

    def test_symbolic_shards_are_not_followed(self):
        good = self.write("good.json", annotation())
        (self.input / "symbolic.json").symlink_to(good)
        result = self.ingest()
        self.assertEqual(result["annotations"], 1)
        with self.assertRaisesRegex(companion.ImportError, "SYMLINK"):
            companion.import_companions([self.input / "symbolic.json"], source_key="bad",
                                        output=self.root / "other")

    def test_unknown_anchor_remains_generic_not_whole_resource(self):
        doc = annotation()
        doc["location"] = {"unresolved": {"selector": "upstream-special-anchor"}}
        self.write("link.json", doc)
        self.ingest()
        recovered, = companion.iter_annotations(self.output)
        self.assertEqual(recovered["annotation"]["location"], doc["location"])

    def test_captured_collection_is_not_reread_after_input_change(self):
        original = annotation()
        shard = self.write("link.json", original)
        captured = collect([shard])
        shard.write_text("invalid changed source")
        companion.import_collected(captured, source_key="capture", output=self.output)
        row, = companion.iter_annotations(self.output)
        self.assertEqual(row["annotation"], original)
        self.assertEqual(row["origin"]["base_uri"], captured.records[0].path.as_uri())

    def test_captured_origin_does_not_follow_a_replaced_parent(self):
        original = self.write("nested/link.json", annotation())
        captured = collect([original])
        base_uri = captured.records[0].path.as_uri()
        moved = self.root / "moved"
        original.parent.rename(moved)
        original.parent.symlink_to(moved, target_is_directory=True)
        self.assertNotEqual(captured.records[0].path.resolve().as_uri(), base_uri)
        companion.import_collected(captured, source_key="capture", output=self.output)
        row, = companion.iter_annotations(self.output)
        self.assertEqual(row["origin"]["base_uri"], base_uri)

    def test_captured_mutated_invalid_annotation_is_revalidated(self):
        shard = self.write("link.json", annotation())
        captured = collect([shard])
        captured.records[0].annotation["target"] = None
        with self.assertRaisesRegex(companion.ImportError, "Invalid captured annotation"):
            companion.import_collected(captured, source_key="capture", output=self.output)
        self.assertFalse(self.output.exists())

    def test_captured_invalid_json_and_origins_are_rejected(self):
        shard = self.write("link.json", annotation())
        captured = collect([shard])
        captured.records[0].annotation["extensions"]["bad"] = float("nan")
        with self.assertRaises(companion.ImportError):
            companion.import_collected(captured, source_key="capture", output=self.output)
        captured = collect([shard])
        captured.records[0].path = Path("relative.json")
        with self.assertRaisesRegex(companion.ImportError, "absolute carrier"):
            companion.import_collected(captured, source_key="capture", output=self.output)
        self.assertFalse(self.output.exists())

    def test_captured_values_are_copied_before_projection(self):
        shard = self.write("link.json", annotation())
        captured = collect([shard])
        copied = companion._validate_captured(captured)
        captured.records[0].annotation["extensions"]["custom"]["z"] = "changed"
        self.assertEqual(copied.records[0].annotation, annotation())

    def test_core_lexical_reference_rule_does_not_require_full_uri_grammar(self):
        docs = []
        for source in ("https://[oops", "//[oops", "file:foo", "urn:opaque:a/../b",
                       "../café.tex#", "../%2E%2e/%2f", "a//b?#"):
            doc = annotation()
            doc["source"]["uri"] = source
            doc["target"] = source
            self.assertFalse(reference.check(doc).problems)
            docs.append(doc)
        self.write("links.json", docs)
        self.ingest()
        rows = list(companion.iter_annotations(self.output))
        self.assertEqual([row["annotation"] for row in rows], docs)
        self.assertEqual(rows[0]["resolution"]["source"]["status"], "unresolved")
        self.assertEqual(rows[1]["resolution"]["source"]["status"], "unresolved")
        self.assertEqual(rows[2]["source"], "file:foo")
        self.assertEqual(rows[3]["source"], "urn:opaque:a/../b")
        self.assertTrue(rows[4]["source"].endswith("/café.tex#"))
        self.assertTrue(rows[5]["source"].endswith("/%2E%2e/%2f"))
        self.assertTrue(rows[6]["source"].endswith("/a//b?#"))

    def test_command_line_ingest_and_query(self):
        self.write("link.json", annotation())
        command = [sys.executable, "-m", "brain.ingest.companion"]
        result = subprocess.run(command + ["ingest", str(self.input), "--source-key", "cli",
                                          "--output", str(self.output)], cwd=ROOT,
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["annotations"], 1)
        result = subprocess.run(command + ["query", "--data-dir", str(self.output)], cwd=ROOT,
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["annotation"], annotation())


@unittest.skipUnless(reference, "Literature Links package required")
class RetainedNativeTests(unittest.TestCase):
    def test_http_origin_preserves_values_duplicate_occurrences_and_large_integers(self):
        with tempfile.TemporaryDirectory() as root:
            entry = {"annotation": annotation(), "origin": {"base_uri": "https://example.org/shards/a.json", "pointer": "/links/0", "line": None, "custom": "retained"}}
            output = Path(root) / "index"
            companion.import_retained([entry, entry], source_key="native", output=output)
            rows = list(companion.iter_annotations(output))
            self.assertEqual(len(rows), 2)
            for row in rows:
                self.assertEqual(row["annotation"], annotation())
                self.assertEqual(row["origin"], entry["origin"])
                self.assertEqual(row["source"], "https://example.org/source.md")

    def test_origin_contract_matches_autoform_for_pointers_and_foreign_paths(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "index"
            for extra in [{"pointer":"/bad~2escape"}, {"path":42}, {"path":""}]:
                origin = {"base_uri":"https://example.org/shards/a.json","pointer":"",**extra}
                with self.assertRaises(companion.ImportError):
                    companion.import_retained([{"annotation":annotation(),"origin":origin}], source_key="native", output=output)
                self.assertFalse(output.exists())
            origin={"base_uri":"https://example.org/shards/a.json","pointer":"/valid~0~1","path":r"C:\foreign\shard.json","line":None}
            companion.import_retained([{"annotation":annotation(),"origin":origin}], source_key="native", output=output)
            row, = companion.iter_annotations(output)
            self.assertEqual(row["origin"],origin)

    def test_bad_carrier_base_rejected_without_output(self):
        with tempfile.TemporaryDirectory() as root:
            for base in ["relative.json", "https://example.org/a b", "https://example.org/a\\u0000", "https://example.org/a\\n"]:
                base = base.replace("\\u0000", "\x00").replace("\\n", "\n")
                output = Path(root) / "index"
                with self.assertRaises(companion.ImportError):
                    companion.import_retained([{"annotation": annotation(), "origin": {"base_uri": base, "pointer": ""}}], source_key="native", output=output)
                self.assertFalse(output.exists())

    def test_malformed_carrier_authority_is_retained_but_not_claimed_resolved(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "index"
            companion.import_retained([{"annotation": annotation(), "origin": {"base_uri":"https://[oops/shards/a.json","pointer":""}}], source_key="native", output=output)
            row, = companion.iter_annotations(output)
            self.assertEqual(row["annotation"], annotation())
            self.assertEqual(row["resolution"]["source"], {"status":"unresolved","reason":"unsupported_carrier_syntax"})
            self.assertTrue(row["source"].startswith("urn:wikilean:companion:unresolved:"))

    def test_opaque_carrier_retained_with_explicit_unresolved_lookup(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "index"
            companion.import_retained([{"annotation": annotation(), "origin": {"base_uri":"urn:carrier:opaque","pointer":""}}], source_key="native", output=output)
            row, = companion.iter_annotations(output)
            self.assertEqual(row["annotation"], annotation())
            self.assertEqual(row["resolution"]["source"], {"status":"unresolved","reason":"opaque_carrier_base"})


if __name__ == "__main__":
    if "--require-reference" in sys.argv:
        sys.argv.remove("--require-reference")
        if reference is None:
            raise SystemExit("Literature Links package required: see docs/COMPANION-IMPORT.md")
    unittest.main()
