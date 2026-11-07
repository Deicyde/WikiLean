"""Hermetic cutover checks for readers and repair tools over Literature Links."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "site", ROOT / "manage", ROOT / "catalog", ROOT / "bench"):
    sys.path.insert(0, str(path))
import native_annotations as native
import build_decl_citations as citations
import export_w3c
import export_wikidata_rdf
import apply_decl_sweep
import apply_sweep_verdicts
import decl_existence_sweep
import generate_tasks
import derived_graph_adapters

REPO = native.MATHLIB_REPOSITORY
def target(name, repo=REPO, **extra):
    return {"type": "lean", "uri": repo,
            "location": {"declaration": name, "module": "Example"}, **extra}

def link(name, quote="A synthetic first mathematical statement.", repo=REPO):
    return {"id": name, "annotation": {"subject": [{
        "type": "html", "uri": "https://en.wikipedia.org/w/index.php?title=Example&oldid=123",
        "location": {"quote": {"exact": quote, "prefix": "before ", "suffix": " after"}},
    }], "object": [target(name, repo)], "relation": "exactMatch"}}

def item():
    return {"id": "a" * 12, "status": "formalized", "provenance": "ai",
            "links": [link("Example.first"), link("Example.second", "A different synthetic mathematical statement.")],
            "unlinked": [], "legacy": {"mathlib": {"decl": "Archived.wrong"}}}

class NativeConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.article = self.root / "Example.json"

    def write_article(self, annotations):
        self.article.write_text(json.dumps({"slug": "Example", "revid": 123,
                                            "annotations": annotations}), encoding="utf-8")

    def test_every_native_target_is_read_without_legacy_or_foreign_fallback(self):
        value = item()
        value["links"].append(link("Foreign.name", repo="https://example.org/lean"))
        absence = link("Absent.name")
        absence["annotation"]["relation"] = "noMatch"
        value["links"].append(absence)
        value["links"][0]["annotation"]["object"][0]["version"] = "v4.33.0"
        before = copy.deepcopy(value)
        refs = native.mathlib_targets(value)
        self.assertEqual([ref["decl"] for ref in refs], ["Example.first", "Example.second"])
        self.assertEqual(refs[0]["version"], "v4.33.0")
        self.assertEqual(value, before)

    def test_explicit_carrier_base_is_used_for_a_relative_repository(self):
        value = item()
        value["links"] = [link("Example.first")]
        value["links"][0]["base_uri"] = REPO
        value["links"][0]["annotation"]["object"][0]["uri"] = ""
        self.assertEqual(native.mathlib_targets(value)[0]["decl"], "Example.first")

    def test_reverse_citations_keep_all_native_declarations(self):
        value = item()
        value["links"].append(copy.deepcopy(value["links"][0]))
        self.write_article([value])
        out = self.root / "out" / "citations.json"
        with patch.object(citations, "ANNOT", self.root), patch.object(citations, "OUT", out):
            citations.main()
        rows = json.loads(out.read_text())
        self.assertEqual(set(rows), {"Example.first", "Example.second"})
        self.assertEqual(len(rows["Example.first"]), 1)

    def test_native_graph_citations_keep_multiple_mathlib_targets(self):
        value = item()
        value["links"].append(link("Foreign.name", repo="https://example.org/lean"))
        result = derived_graph_adapters.concept_graph(
            grounding=[{"qid": "Q1", "slug": "Example", "formalizations": []}],
            overrides=[], crossrefs={}, prior=[{"qid": "Q1", "label": "Example"}],
            annotations=[("site/annotations/Example.json", {"slug": "Example", "annotations": [value]})],
            oracle={"declarations": {"Example.first": {}, "Example.second": {}}},
            mathlib_text=[], statements=[], dependencies=[], wikidata_edges=[])
        roles = result["decl-qid-roles"]
        self.assertEqual(roles["Example.first"]["Q1"], "citation")
        self.assertEqual(roles["Example.second"]["Q1"], "citation")
        self.assertNotIn("Foreign.name", roles)
        self.assertNotIn("Archived.wrong", roles)

    def test_native_rdf_summary_keeps_both_mathlib_names_and_excludes_foreign_targets(self):
        value = item()
        value["links"].append(link("Foreign.name", repo="https://example.org/lean"))
        self.write_article([value])
        catalog = self.root / "catalog.jsonl"
        catalog.write_text(json.dumps({"title": "Example", "wikidata_qid": "Q1"}) + "\n")
        out = self.root / "out"
        w3c = self.root / "w3c"
        export_wikidata_rdf.main(["--annotations-dir", str(self.root), "--catalog", str(catalog),
                                 "--out-dir", str(out), "--out-w3c-dir", str(w3c)])
        data = (out / "wikilean.ttl").read_text()
        self.assertIn("Example.first", data)
        self.assertIn("Example.second", data)
        self.assertNotIn("Foreign.name", data)
        self.assertNotIn("Archived.wrong", data)

    def test_benchmark_statements_stay_with_their_own_native_link(self):
        self.write_article([item()])
        with patch.object(generate_tasks, "ANNOT_DIR", self.root):
            rows = generate_tasks.load_annotation_statements(
                {"Example.first", "Example.second"}, {"Example": "Q1"})
        self.assertEqual({r["decl"]: r["statement"] for r in rows}, {
            "Example.first": "A synthetic first mathematical statement.",
            "Example.second": "A different synthetic mathematical statement.",
        })

    def test_w3c_preserves_record_boundaries_pins_and_public_notes(self):
        value = item()
        value["proof_note"] = "private-proof-note"
        value["links"][0]["annotation"]["object"][0]["version"] = "v4.33.0"
        self.write_article([value])
        page = export_w3c.convert(self.article)
        self.assertEqual(len(page["items"]), 2)
        for i, row in enumerate(page["items"]):
            self.assertEqual(row["target"]["selector"]["exact"],
                             value["links"][i]["annotation"]["subject"][0]["location"]["quote"]["exact"])
            labels = [body["identifier"] for body in row["body"] if "identifier" in body]
            self.assertEqual(labels, [value["links"][i]["id"]])
        self.assertIn("v4.33.0", json.dumps(page))
        self.assertNotIn("private-proof-note", json.dumps(page))

    def test_unknown_locator_is_not_broadened_to_a_whole_resource(self):
        value = item()
        for entry in value["links"]:
            entry["annotation"]["subject"][0]["location"] = {"profile": "urn:test:opaque", "value": "keep"}
        self.write_article([value])
        self.assertEqual(export_w3c.convert(self.article)["items"], [])

    def test_current_checkout_sweep_does_not_condemn_pinned_targets(self):
        value = item()
        value["links"][0]["annotation"]["object"][0]["version"] = "v4.33.0"
        self.write_article([value])
        with patch.object(decl_existence_sweep, "ANNOT_DIR", self.root):
            pinned = []
            rows, _ = decl_existence_sweep.scan_corpus(pinned)
        self.assertEqual(set(rows), {"Example.second"})
        self.assertEqual(pinned[0]["version"], "v4.33.0")

    def test_hash_pinned_targets_are_deferred_from_current_checkout_checks(self):
        value = item()
        value["links"][0]["annotation"]["object"][0]["sha256"] = "a" * 64
        self.write_article([value])
        with patch.object(decl_existence_sweep, "ANNOT_DIR", self.root):
            pinned = []
            rows, _ = decl_existence_sweep.scan_corpus(pinned)
        self.assertEqual(set(rows), {"Example.second"})
        self.assertEqual(pinned[0]["sha256"], "a" * 64)
        with patch.object(generate_tasks, "ANNOT_DIR", self.root):
            rows = generate_tasks.load_annotation_statements(
                {"Example.first", "Example.second"}, {"Example": "Q1"})
        self.assertEqual([row["decl"] for row in rows], ["Example.second"])

    def test_rename_keeps_native_sources_and_drops_stale_module_hints(self):
        value = item()
        value["links"] = value["links"][:1]
        before = copy.deepcopy(value)
        changed = native.rename_mathlib_target(value, "Example.first", "Renamed.first")
        self.assertEqual(native.mathlib_targets(changed)[0]["decl"], "Renamed.first")
        self.assertEqual(changed["links"][0]["annotation"]["object"][0]["location"],
                         {"declaration": "Renamed.first"})
        self.assertEqual(changed["links"][0]["annotation"]["subject"],
                         value["links"][0]["annotation"]["subject"])
        self.assertEqual(value, before)

    def test_clear_retains_source_selection_and_carrier_base(self):
        value = item()
        value["links"] = value["links"][:1]
        value["links"][0]["base_uri"] = "https://example.org/"
        changed = native.clear_mathlib_target(value, "Example.first")
        self.assertEqual(changed["links"], [])
        self.assertEqual(changed["unlinked"][0]["base_uri"], "https://example.org/")
        self.assertEqual(changed["unlinked"][0]["location"],
                         value["links"][0]["annotation"]["subject"][0]["location"])

    def test_pinned_and_multi_target_repairs_are_refused_without_changes(self):
        for pinned in [False, True]:
            value = item()
            if pinned:
                value["links"] = value["links"][:1]
                value["links"][0]["annotation"]["object"][0]["version"] = "v4.33.0"
            before = copy.deepcopy(value)
            with self.assertRaises(native.NativeAnnotationError):
                native.rename_mathlib_target(value, "Example.first", "Renamed.first")
            self.assertEqual(value, before)

    def test_native_tombstones_and_junk_evidence_are_not_automatically_rewritten(self):
        for field, value in [("status", "rejected"), ("junk", ["legacy coordinate"])]:
            annotation = item()
            annotation["links"] = annotation["links"][:1]
            if field == "status":
                annotation[field] = value
            else:
                annotation["links"][0]["annotation"]["object"][0][field] = value
            before = copy.deepcopy(annotation)
            with self.assertRaises(native.NativeAnnotationError):
                native.clear_mathlib_target(annotation, "Example.first")
            self.assertEqual(annotation, before)

    def test_human_native_rows_are_not_auto_edited(self):
        value = item()
        value["links"] = value["links"][:1]
        value["provenance"] = "human"
        out, applied, skipped = apply_decl_sweep.apply_edits([value], [{
            "id": value["id"], "kind": "rename", "expect_decl": "Example.first", "new_decl": "Renamed.first"
        }], True)
        self.assertEqual(out, [value])
        self.assertFalse(applied)
        self.assertEqual(len(skipped), 1)

    def test_live_pinned_overclaim_is_deferred_even_from_an_old_sweep(self):
        value = item()
        value["links"] = value["links"][:1]
        value["links"][0]["annotation"]["object"][0]["version"] = "v4.33.0"
        with patch.object(apply_sweep_verdicts.ufu, "get_article",
                          return_value={"annotations": [value], "version": 1}):
            result = apply_sweep_verdicts.process_slug(None, "unused", "dry-run", "Example", [],
                     [{"id": value["id"], "expect_decl": "Example.first",
                       "verdict": {"action": "propose_status", "new_status": "partial", "reason": "old sweep"}}],
                     False, "test")
        self.assertEqual(result["outcome"], "no-op")
        self.assertEqual(result["proposals"], 0)

    def test_native_proposal_uses_links_not_a_new_legacy_mathlib_field(self):
        value = item()
        value["links"] = value["links"][:1]
        fields = apply_sweep_verdicts.proposal_fields_for(value,
                 {"action": "rename", "new_decl": "Renamed.first"})
        self.assertEqual(set(fields), {"links", "unlinked"})
        native.validate_items([{**value, **fields}])

if __name__ == "__main__":
    unittest.main()
