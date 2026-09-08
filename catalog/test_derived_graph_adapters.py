"""Semantic parity fixtures for explicit-input catalog source normalization."""
from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import derived_graph_adapters as adapters


class DerivedAdaptersTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.metadata = {"revision": "a" * 40, "file_url": "https://huggingface.co/datasets/fixture/resolve/" + "a" * 40 + "/input.csv",
                         "size": 123, "sha256": "b" * 64}
        self.statements = [
            {"statement_id": "a", "decl_name": "Alpha", "module": "Mathlib.Topology.Basic", "file_path": "Mathlib/Topology/Basic.lean"},
            {"statement_id": "b", "decl_name": "Ns.beta'", "module": "Mathlib.Std.Legacy", "file_path": "Mathlib/Std/Legacy.lean"},
            {"statement_id": "c", "decl_name": "Gamma", "module": "Mathlib.Topology", "file_path": "Mathlib/Topology.lean"},
        ]

    def csv_path(self, name, rows):
        path = self.root / name
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        return path

    def test_concept_rows_reproduce_legacy_merge_and_prior_projection(self):
        first = {"title": "One", "wikidata_qid": "Q1", "primary_decl": None}
        second = {"title": "The One", "wikidata_qid": "Q1", "primary_decl": "Alpha",
                  "mathlib_decls": [{"decl": "Alpha", "module": "Mathlib.Fixture", "confidence": "high"}], "importance": "Top"}
        tagged = {"pilot_tagged.jsonl": [first], "tier2_tagged.jsonl": [second, {"title": "Missing", "wikidata_qid": None}]}
        for name, rows in tagged.items():
            (self.root / name).write_text("".join(json.dumps(row) + "\n" for row in rows))
        rows = adapters.concept_layer(tagged)
        self.assertEqual(rows, adapters.build_concept_layer.build_rows(self.root)[0])
        self.assertEqual(rows[0]["titles"], ["One", "The One"])
        self.assertEqual(adapters.prior_nodes(rows)[0]["primary_decl"], "Alpha")
        with self.assertRaises(ValueError):
            adapters.concept_layer({"pilot_tagged.jsonl": [first]})

    def test_formal_weights_roles_oracle_rescue_and_wikidata_first_property(self):
        dependencies = [{"src_id": "a", "dep_id": "b", "edge_type": kind} for kind in ("sig", "sig", "def", "docref")]
        statement_path = self.csv_path("statement.csv", self.statements)
        dependency_path = self.csv_path("dependency.csv", dependencies)
        mapping = {"Alpha": ["Q1"], "Ns.beta'": ["Q2"]}
        with mock.patch.multiple(adapters.lift_formal_edges, STMT=statement_path, DEP=dependency_path), contextlib.redirect_stderr(io.StringIO()):
            old = adapters.lift_formal_edges._lift(mapping)
        self.assertEqual(adapters.formal_edges(mapping, iter(self.statements), iter(dependencies)), old)
        self.assertEqual(old[0]["weight"], 1)
        self.assertEqual(old[0]["w_types"], {"sig": 1, "def": 1, "proof": 0})
        grounding = [{"qid": qid, "slug": slug, "formalizations": [{"decl": name, "module": "Mathlib.Fixture",
                      "match_kind": "related", "confidence": "high"}]} for qid, slug, name in
                     (("Q1", "One", "Alpha"), ("Q2", "Two", "Ns.beta'"))]
        original = json.dumps(grounding, sort_keys=True)
        result = adapters.concept_graph(grounding=grounding,
            overrides=[{"qid": "Q2", "set": {"match_kind:Ns.beta'": "exact"}}],
            crossrefs={"xrefs": {"Q1": {"nlab": "one"}}}, prior=[{"qid": "Q1", "label": "Prior One", "importance": "Top"}],
            annotations=[("site/annotations/One.json", {"slug": "One", "annotations": [
                {"mathlib": {"decl": "Alpha"}}, {"mathlib": {"decl": "Gamma"}}]})],
            oracle={"declarations": {"Alpha": {}}}, mathlib_text=["theorem Namespaced.beta' : True := by trivial\n"],
            statements=iter(self.statements), dependencies=iter(dependencies), wikidata_edges=[
                {"s": "Q1", "o": "Q2", "p": "P31", "p_label": "first"},
                {"s": "Q1", "o": "Q2", "p": "P279", "p_label": "second"}])
        self.assertEqual(json.dumps(grounding, sort_keys=True), original)
        self.assertEqual(result["decl-qid-roles"]["Alpha"]["Q1"], "formalization")
        self.assertEqual(result["decl-qid-roles"]["Gamma"]["Q1"], "citation")
        self.assertEqual(result["concept-graph"]["nodes"][1]["status"], "formalized")
        self.assertEqual(result["concept-graph"]["nodes"][0]["xrefs_keys"], ["nlab"])
        self.assertEqual(result["concept-graph"]["edges"][-1]["props"], [{"p": "P31", "label": "first"}])

    def test_empty_oracle_does_not_drop_every_decl(self):
        with self.assertRaisesRegex(ValueError, "oracle"):
            adapters.concept_graph(grounding=[], overrides=[], crossrefs={}, prior=[], annotations=[], oracle={},
                mathlib_text=[], statements=[], dependencies=[], wikidata_edges=[])

    def test_hierarchy_preserves_ancestor_counts_and_superseded_flags(self):
        module = adapters.build_hierarchy
        path = self.csv_path("statements.csv", self.statements)
        output = self.root / "hierarchy.json"
        with mock.patch.multiple(module, STMT=path, OUT=output, SPLIT_AT=1), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            module._build(argparse.Namespace(revision=self.metadata["revision"]), self.metadata)
            actual = adapters.hierarchy(iter(self.statements), self.metadata)
        self.assertEqual(actual, json.loads(output.read_text()))
        self.assertTrue(actual["libraries"]["Mathlib"]["modules"]["Std"]["superseded"])
        self.assertEqual(sum(row["n_decls"] for row in actual["subfields"]), 3)

    def test_theorem_links_preserve_affirmed_tier_and_primary_citation_joins(self):
        prior = [{"qid": "Q1", "slug": "One", "primary_decl": "Alpha"}]
        annotations = [("site/annotations/One.json", {"slug": "One", "annotations": [{"mathlib": {"decl": "Gamma"}}]})]
        matches = [{"formal_decl": name, "arxiv_id": arxiv, "gpt54_label": label, "deepseek_label": "exact",
                    "sim": "0.9876", "informal_ref": "Theorem 1", "paper_title": "Fixture"}
                   for name, arxiv, label in (("Alpha", "1", "inexact"), ("Gamma", "2", "exact"), ("Alpha", "3", "rejected"))]
        module = adapters.ingest_theorem_graph
        path = self.csv_path("matching.csv", matches)
        graph = self.root / "graph.json"
        graph.write_text(json.dumps({"nodes": prior}))
        annotation_dir = self.root / "annotations"
        annotation_dir.mkdir()
        (annotation_dir / "One.json").write_text(json.dumps(annotations[0][1]))
        output = self.root / "links.json"
        with mock.patch.multiple(module, CACHE=path, CONCEPT_GRAPH=graph, ANNOT=annotation_dir, OUT=output), contextlib.redirect_stdout(io.StringIO()):
            module.ingest(argparse.Namespace(tier="affirmed"), self.metadata)
        actual = adapters.theoremgraph_links(prior=prior, annotations=annotations, matches=iter(matches), metadata=self.metadata)
        self.assertEqual(actual, json.loads(output.read_text()))
        self.assertEqual([row["arxiv_id"] for row in actual["links"]["Q1"]], ["1", "2"])
        with self.assertRaisesRegex(ValueError, "affirmed"):
            adapters.theoremgraph_links(prior=prior, annotations=[], matches=[], metadata=self.metadata, tier="exact")


if __name__ == "__main__":
    unittest.main()
