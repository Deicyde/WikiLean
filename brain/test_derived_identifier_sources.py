#!/usr/bin/env python3
"""Evidence, compatibility, and actual compiler tests for pure ID derivatives."""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import derived_identifier_sources as core
import export_derived_identifiers as producer
import test_wikidata_crossref_sources as cross_fixture

io = core.io
WHEN = cross_fixture.WHEN
TOOL = {"name": "fixture-source", "version": "1", "sha256": "a" * 64}


class IdentifierSourcesTest(unittest.TestCase):
    def setUp(self):
        self.xref = cross_fixture.CrossrefExportTest()
        self.xref.setUp()
        self.addCleanup(self.xref.doCleanups)
        self.root = self.xref.root
        self.parents = self.root / "identifier-parents"
        self.parents.mkdir(mode=0o700)
        self.store = self.root / "identifier-exports"
        self.store.mkdir(mode=0o700)
        registry = cross_fixture.registry(mathworld="P2812", nlab="P4215")
        (self.xref.repository / io.REGISTRY_PATH).write_bytes(registry)
        self.xref.git("add", io.REGISTRY_PATH)
        self.xref.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "MathWorld registry")
        self.xref.commit = self.xref.git("rev-parse", "HEAD").decode().strip()
        entity = cross_fixture.fixture.entity
        first, second = entity("Q1"), entity("Q20", requested="Q2")
        first["claims"]["P2812"] = [cross_fixture.statement(value, pid="P2812") for value in ("AlphaBeta", "Shared")]
        second["claims"]["P2812"] = [cross_fixture.statement(value, pid="P2812") for value in ("Gamma", "Shared")]
        capture = cross_fixture.fixture
        toolchain = capture.fake_toolchain()
        self.xref.bundle = capture.acquire.publish_transcript(capture.plan_bytes(["Q1", "Q2"]),
            [capture.response({"entities": {"Q1": first, "Q2": second}})], store=self.root / "mathworld-capture",
            acquisition_tool=capture.fake_tool(toolchain), acquisition_toolchain=toolchain, audit_time=WHEN)
        exported = self.xref.export()
        self.sources = json.loads((exported / "source-fragment.json").read_bytes())["sources"]
        self.manifests = {s["source"]: io.source_plan_contracts._source_manifest_from_plan(s, "fixture") for s in self.sources}
        self.roots = {io.PHYSICAL_ROOT: exported, io.GIT_ROOT: self.xref.repository, "identifier_parent": self.parents}
        self.mathlib_files = {
            "Mathlib/A.lean": b"namespace Foo\n@[stacks 0001, wikidata Q1] theorem a : True := by trivial\nend Foo\n",
            "Mathlib/B.lean": b"namespace Foo\n@[kerodon 0002] theorem b : True := by trivial\n@[stacks 0003] theorem missing : True := by trivial\nend Foo\n",
            "README.md": b"fixture source tree\n"}
        commit, tree = "1" * 40, "2" * 40
        data = {"file-" + io.sha(path.encode()): raw for path, raw in self.mathlib_files.items()}
        data["git_tree"] = io.canonical({"commit": commit, "tree": tree, "entries": [{"path": path, "mode": "100644",
            "git_blob": io.git_oid("blob", raw), "sha256": io.sha(raw), "bytes": len(raw)} for path, raw in sorted(self.mathlib_files.items())]})
        mathlib = self.source("mathlib-source", data, commit=commit, license="Apache-2.0")
        self.oracle = io.artifact({"declarations": {"Foo.a": {}, "Foo.b": {}}})
        self.source("mathlib-docs", {"declaration_oracle": self.oracle, "provenance": io.canonical({
            "mathlib_commit": commit, "mathlib_tree": tree, "oracle_sha256": io.sha(self.oracle)})}, parent=mathlib)
        self.plan = {"schema": core.PLAN_SCHEMA, "parents": sorted(self.sources, key=lambda s: s["source"]),
            "reviewed_parent_manifest_ids": {name: manifest["source_manifest_id"] for name, manifest in self.manifests.items()}}

    def source(self, name, data, *, commit=None, parent=None, license="LicenseRef-Fixture"):
        def ref(path, raw):
            target = self.parents / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            return {"root": "identifier_parent", "path": path, "sha256": io.sha(raw), "bytes": len(raw), "media_type": "application/json"}
        objects = [{"name": obj, **ref("objects/" + io.sha(raw), raw), "roles": ["normalized"] if parent else ["normalized", "raw"],
            "redistribution": "restricted"} for obj, raw in sorted(data.items())]
        pin = {"type": "git_commit", "value": commit} if commit else {"type": "dataset_revision", "value": "fixture-v1"}
        receipts, receipt_ids, preimages, inputs, parent_ids = [], [], [], [], []
        if parent is None:
            request = ref(name + "/request", b"")
            requests = [{"kind": "http_get", "uri": "https://example.invalid/" + name, "parameters_sha256": io.sha(b"")}]
            receipt = {"schema": core.contracts.ACQUISITION_RECEIPT_SCHEMA_V1, "acquisition_receipt_id": "sha256:" + "0" * 64,
                "source": name, "pin": pin, "tool": TOOL, "upstream_uri": "https://example.invalid/" + name,
                "requests": requests, "batch": {"status": "complete", "request_set_root": core.contracts.acquisition_request_set_root(requests),
                    "requests_total": 1, "requests_succeeded": 1, "requests_failed": 0},
                "outputs": [io.object_ref(item) for item in objects], "audit": {"acquired_at": WHEN}}
            receipt["acquisition_receipt_id"] = core.contracts.acquisition_receipt_identity(receipt)
            receipt_ids = [receipt["acquisition_receipt_id"]]
            receipts = [{**ref(name + "/receipt.json", io.canonical(receipt)), "acquisition_receipt_id": receipt_ids[0]}]
            preimages = [{**request, "parameters_sha256": io.sha(b"")}]
            inputs = [{**io.object_ref(item), "origin": {"kind": "acquisition_receipt", "id": receipt_ids[0]}} for item in objects]
        else:
            parent_ids = [self.manifests[parent["source"]]["source_manifest_id"]]
            raw = [{**item, "roles": ["raw"]} for item in parent["objects"] if "normalized" in item["roles"]]
            objects += raw
            inputs = [{**io.object_ref(item), "origin": {"kind": "source_manifest", "id": parent_ids[0]}} for item in raw]
        lineage = {"schema": core.contracts.NORMALIZATION_LINEAGE_SCHEMA_V1, "normalization_lineage_id": "sha256:" + "0" * 64,
            "source": name, "mode": "transform" if parent else "identity", "acquisition_receipt_ids": receipt_ids,
            "parent_source_manifest_ids": parent_ids, "normalization_schema": "fixture/v1", "configuration_sha256": "b" * 64,
            "tool": TOOL, "inputs": sorted(inputs, key=lambda item: item["object"]),
            "outputs": [io.object_ref(item) for item in sorted(objects, key=lambda item: item["name"]) if "normalized" in item["roles"]],
            "result": "complete", "audit": {"normalized_at": WHEN}}
        lineage["normalization_lineage_id"] = core.contracts.normalization_lineage_identity(lineage)
        source = {"source": name, "source_kind": "sealed_snapshot" if parent else "acquired_dataset", "pin": pin,
            "objects": sorted(objects, key=lambda item: item["name"]), "license": {"expression": license, "redistribution": "restricted"},
            "acquisition": TOOL, "normalization": {"schema": "fixture/v1", "tool": TOOL,
                "inputs": sorted(item["name"] for item in objects if "raw" in item["roles"]),
                "outputs": sorted(item["name"] for item in objects if "normalized" in item["roles"])},
            "evidence": {"acquisition_receipts": receipts, "request_parameter_preimages": preimages,
                "normalization_lineage": {**ref(name + "/lineage.json", io.canonical(lineage)), "normalization_lineage_id": lineage["normalization_lineage_id"]}}}
        self.sources.append(source)
        self.manifests[name] = io.source_plan_contracts._source_manifest_from_plan(source, "fixture")
        return source

    def export(self):
        return producer.export(self.plan, self.roots, self.store, normalized_at=WHEN)

    @staticmethod
    def rows(path):
        return [json.loads(line) for line in path.read_bytes().splitlines()]

    def captured(self):
        return core.capture_parents(self.plan, self.roots)

    def test_complete_private_export_verifies_and_has_revision_bound_clock_free_metadata(self):
        path = self.export()
        producer.verify(path, self.roots)
        records = self.rows(path / "normalized/mathlib_tag_xrefs.jsonl")
        meta = records[0]["_meta"]
        self.assertEqual((meta["n_files"], meta["counts"], meta["unverified_rows"]), (2, {"stacks": 2, "kerodon": 1, "wikidata": 1}, 1))
        self.assertEqual(meta["oracle"]["mathlib_commit"], meta["commit"])
        self.assertEqual(meta["oracle"]["revision_status"], "verified-official-docs-build-lineage")
        self.assertFalse({"root", "path", "fetched_at", "built_at"} & set(meta["oracle"]))
        self.assertNotIn(str(self.root), json.dumps(meta))
        self.assertTrue(any(row.get("unverified") for row in records[1:]))
        pages = self.rows(path / "normalized/mathworld_pages.jsonl")
        links = self.rows(path / "normalized/mathworld_links.jsonl")
        core.build_context.validate_external_pair("mathworld", pages[0]["_meta"], pages[1:], links[0]["_meta"], links[1:])
        self.assertIsNone(pages[0]["_meta"]["sitemap_inventory"])
        self.assertEqual(next(row["qid"] for row in pages[1:] if row["id"] == "Shared"), "Q1")
        self.assertEqual(len(links), 1)

    def test_tag_semantic_projection_matches_legacy_harvest_text(self):
        rows, problems = [], []
        for path, raw in sorted(self.mathlib_files.items()):
            if path.endswith(".lean"):
                core.tags.harvest_text(raw.decode(), path, {"Foo.a", "Foo.b"}, rows, problems)
        rows.sort(key=lambda row: (row["file"], row["line"], row["db"], row["tag"], row["decl"]))
        exported = self.export()
        self.assertEqual(self.rows(exported / "normalized/mathlib_tag_xrefs.jsonl")[1:], rows)
        self.assertEqual(json.loads((exported / "normalized/tag_harvest_diagnostics.json").read_bytes())["problems"], problems)

    def test_mathworld_semantic_projection_matches_legacy_without_sitemap_or_network(self):
        path = self.export()
        expected = self.rows(path / "normalized/mathworld_pages.jsonl")[1:]
        legacy = self.root / "legacy"
        legacy.mkdir(mode=0o700)
        refs = self.root / "crossrefs.json"
        refs.write_bytes(self.captured()[3][("wikidata-crossrefs", "wikidata_crossrefs")])
        with mock.patch.object(core.common, "CROSSREFS", refs), mock.patch.object(core.common, "EXTERNAL_DIR", legacy), \
             mock.patch.object(core.mathworld, "sitemap_inventory", return_value=None), \
             mock.patch("socket.socket", side_effect=AssertionError("network access")):
            core.mathworld.main()
        self.assertEqual(self.rows(legacy / "mathworld_pages.jsonl")[1:], expected)

    def test_bound_oracle_content_or_revision_mismatch_fails(self):
        sources, manifests, objects, captured, lineages = self.captured()
        for key, value in (("mathlib_commit", "3" * 40), ("mathlib_tree", "4" * 40), ("oracle_sha256", "5" * 64)):
            wrong = copy.deepcopy(captured)
            facts = io.parse(wrong[("mathlib-docs", "provenance")], "facts")
            facts[key] = value
            wrong[("mathlib-docs", "provenance")] = io.canonical(facts)
            with self.subTest(key=key), self.assertRaisesRegex(io.ExportError, "revision/content differ"):
                core.reduce_mathlib(manifests, objects, wrong, lineages)

    def test_oracle_requires_selected_source_lineage(self):
        _sources, manifests, objects, captured, lineages = self.captured()
        lineages["mathlib-docs"]["parent_source_manifest_ids"] = []
        with self.assertRaisesRegex(io.ExportError, "no selected Mathlib source"):
            core.reduce_mathlib(manifests, objects, captured, lineages)

    def test_missing_and_extra_mathlib_normalized_members_fail(self):
        _sources, manifests, objects, captured, lineages = self.captured()
        missing = dict(captured)
        missing.pop(("mathlib-source", "file-" + io.sha(b"Mathlib/A.lean")))
        with self.assertRaisesRegex(io.ExportError, "incomplete"):
            core.reduce_mathlib(manifests, objects, missing, lineages)
        extra = {**captured, ("mathlib-source", "file-" + "9" * 64): b"unbound"}
        with self.assertRaisesRegex(io.ExportError, "undeclared Mathlib"):
            core.reduce_mathlib(manifests, objects, extra, lineages)

    def test_changed_source_bytes_fail_before_derivation(self):
        member = next(item for source in self.sources if source["source"] == "mathlib-source"
                      for item in source["objects"] if item["name"].startswith("file-"))
        (self.parents / member["path"]).write_bytes(b"changed source")
        with self.assertRaisesRegex(io.ExportError, "parent bytes differ"):
            self.export()
        self.assertEqual(list(self.store.iterdir()), [])

    def test_source_name_cannot_replace_explicit_reviewed_identity(self):
        self.plan["reviewed_parent_manifest_ids"]["mathlib-docs"] = "sha256:" + "f" * 64
        with self.assertRaisesRegex(io.ExportError, "explicitly reviewed"):
            self.export()

    def test_mathworld_requires_p2812_mapping_and_queried_scope(self):
        _sources, manifests, objects, captured, _lineages = self.captured()
        bad = dict(captured)
        bad[(io.CURATED_SOURCE, "source_registry")] = cross_fixture.registry(mathworld="P4215")
        with self.assertRaisesRegex(io.ExportError, "P2812 mapping"):
            core.reduce_mathworld(manifests, objects, bad)
        bad = dict(captured)
        bad[("wikidata-crossrefs", "requested_qid_scope")] = io.canonical({"schema": io.SCOPE_SCHEMA, "qids": ["Q1"]})
        with self.assertRaisesRegex(io.ExportError, "requested crossref scope"):
            core.reduce_mathworld(manifests, objects, bad)

    def test_rehashed_output_tamper_fails_independent_reduction(self):
        path = self.export()
        (path / "normalized/mathworld_pages.jsonl").write_bytes(b'{}\n')
        files = io.capture_tree(path)
        document = io.parse(files.pop("export.json"), "export")
        document["files"] = {name: {"sha256": io.sha(raw), "bytes": len(raw)} for name, raw in sorted(files.items())}
        document["export_id"] = core.contracts.domain_hash(core.EXPORT_SCHEMA, {key: value for key, value in document.items() if key != "export_id"})
        (path / "export.json").write_bytes(io.canonical(document))
        with self.assertRaisesRegex(io.ExportError, "independent reduction"):
            producer.verify(path, self.roots)

    def test_actual_compiler_accepts_both_derivatives_and_all_parent_evidence(self):
        import test_compile_offline_pack_v2 as compiler_fixture
        path = self.export()
        fragment = json.loads((path / "source-fragment.json").read_bytes())
        packed_fixture = compiler_fixture.OfflinePackCompilerTest()
        packed_fixture.setUp()
        self.addCleanup(packed_fixture.tearDown)
        packed_fixture._upgrade_plan_v3()
        inventory, plan = packed_fixture.inventory, packed_fixture.plan
        inventory["inputs"] = [item for item in inventory["inputs"] if item["id"] != "optional_external"]
        plan["input_bindings"] = [item for item in plan["input_bindings"] if item["input_id"] != "optional_external"]
        for binding in fragment["input_bindings"]:
            member = binding["members"][0]
            root = "repo" if binding["input_id"] == "mathlib-tag-xrefs" else "external"
            inventory["inputs"].append({"id": binding["input_id"], "class": "immutable_source_object", "root": root,
                "path": member["path"], "cardinality": "one", "requirement": "required", "consumers": ["brain/replay.py"], "purpose": "identifier fixture"})
            physical = (packed_fixture.repo if root == "repo" else packed_fixture.external) / member["path"]
            physical.parent.mkdir(parents=True, exist_ok=True)
            physical.write_bytes((path / ("normalized/" + member["object"] + ".jsonl")).read_bytes())
        inventory["inputs"].sort(key=lambda item: item["id"])
        inventory["inventory_id"] = core.contracts.reducer_input_inventory_identity(inventory)
        packed_fixture.inventory_path.write_bytes(io.canonical(inventory))
        plan["inventory_id"] = inventory["inventory_id"]
        plan["sources"] = sorted([*plan["sources"], *fragment["sources"]], key=lambda item: item["source"])
        plan["input_bindings"] = sorted([*plan["input_bindings"], *fragment["input_bindings"]], key=lambda item: item["input_id"])
        packed_fixture.plan_path.write_bytes(io.canonical(plan))
        result = compiler_fixture.compiler.compile_offline_pack_v2(packed_fixture.plan_path.resolve(), packed_fixture.inventory_path.resolve(),
            (packed_fixture.base / "identifier-pack").resolve(), roots={**self.roots, core.PHYSICAL_ROOT: path,
                "repo": packed_fixture.repo.resolve(), "external": packed_fixture.external.resolve()}, git_executable="/usr/bin/git")
        manifest, _ = core.contracts.load_canonical_json(result.manifest_path)
        core.contracts.verify_offline_pack_files(manifest, result.root, manifest_path=result.manifest_path)

    def test_no_replace_and_no_network_or_ambient_caches(self):
        with mock.patch("socket.socket", side_effect=AssertionError("network access")), \
                mock.patch.object(core.tags, "ORACLE", self.root / "absent"), mock.patch.object(core.common, "CROSSREFS", self.root / "absent"), \
                mock.patch.object(core.mathworld, "sitemap_inventory", side_effect=AssertionError("sitemap access")):
            first = self.export()
            self.assertEqual(self.export(), first)
        self.assertEqual(list(self.store.iterdir()), [first])

    def test_wrong_imported_helper_origin_fails(self):
        with mock.patch.object(core.mathworld, "__file__", "/unreviewed/mathworld.py"):
            with self.assertRaisesRegex(io.ExportError, "identifier helper origin"):
                producer.implementation()


if __name__ == "__main__":
    unittest.main()
