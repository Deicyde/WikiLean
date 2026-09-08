"""Closed derived normalization: semantic fixtures and adversarial evidence tests."""
from __future__ import annotations

import copy
import csv
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import derived_catalog_sources as core
import export_derived_catalog as producer

WHEN = "2026-09-08T20:00:00Z"
TOOL = {"name": "fixture-acquirer", "version": "1", "sha256": "a" * 64}


class DerivedSourcesTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.parents = self.root / "parents"; self.parents.mkdir(mode=0o700)
        self.store = self.root / "store"; self.store.mkdir(mode=0o700)
        self.repository = self.root / "repository"; self.repository.mkdir(mode=0o700)
        self.sources = []
        self.manifests = {}
        self.profile = {"files": [{"path": path, "sha256": core.sha((core.ROOT / path).read_bytes())} for path in core.TOOL_FILES]}
        self.profile["profile_id"] = core.profile_id(self.profile)
        self.registry = {"schema": core.PROFILE_SCHEMA, "current_profile": self.profile["profile_id"], "profiles": [self.profile]}
        registry = self.root / "profiles.json"; registry.write_bytes(core.canonical(self.registry))
        patch = mock.patch.object(core, "REGISTRY", registry); patch.start(); self.addCleanup(patch.stop)
        tagged = {"title": "One", "wikidata_qid": "Q1", "primary_decl": "Alpha",
                  "mathlib_decls": [{"decl": "Alpha", "module": "Mathlib.Fixture"}]}
        self.curated = {"pilot-tagged": core.artifact(tagged) + b"\n", "tier2-tagged": b"",
            "grounding": core.artifact({"concepts": [{"qid": "Q1", "slug": "One", "formalizations": [
                {"decl": "Alpha", "module": "Mathlib.Fixture", "match_kind": "exact", "confidence": "high"}]}]}),
            "grounding-overrides": b""}
        for name, path in core.CURATED_PATHS.items():
            target = self.repository / path; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(self.curated[name])
        self.git("init", "-q")
        self.git("add", *sorted(core.CURATED_PATHS.values()))
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "Fixture curated inputs")
        commit = self.git("rev-parse", "HEAD").decode().strip()
        article = {"slug": "One", "annotations": [{"mathlib": {"decl": "Alpha"}, "provenance": "human"}]}
        original = self.source("wikilean-d1", {"articles": core.artifact(article)})
        annotations = self.source("wikilean-d1-brain-sidecars", {"article-" + core.sha(b"One"): core.artifact(article)}, parent=original)
        lean_path, lean = "Mathlib/Fixture.lean", b"theorem Alpha : True := by trivial\n"
        tree = {"commit": "b" * 40, "entries": [{"path": lean_path, "mode": "100644", "sha256": core.sha(lean), "bytes": len(lean)}]}
        mathlib = self.source("mathlib-source", {"git_tree": core.artifact(tree), "file-" + core.sha(lean_path.encode()): lean}, git_commit="b" * 40)
        docs = self.source("mathlib-docs", {"declaration_oracle": core.artifact({"declarations": {"Alpha": {}}})}, parent=mathlib)
        statements = self.csv([{ "statement_id": "a", "decl_name": "Alpha", "module": "Mathlib.Fixture", "file_path": "Mathlib/Fixture.lean"}])
        hf = self.source("hf-uw-math-graph", {"statement_formal_csv": statements,
            "formal_dependency_csv": b"src_id,dep_id,edge_type\n"}, git_commit="c" * 40)
        matching = self.source("hf-uw-theorem-matching", {"theorem_matching_csv": self.csv([{
            "formal_decl": "Alpha", "arxiv_id": "1", "gpt54_label": "exact", "deepseek_label": "exact", "sim": "0.95",
            "informal_ref": "Theorem 1", "paper_title": "Fixture"}])}, git_commit="d" * 40)
        wd = self.source("wikidata-observation", {"wikidata_edges": b'{"s":"Q1","o":"Q2","p":"P31"}\n'},
            support={"request_plan": core.canonical({"schema": "wikilean.wikidata-observation-plan/v2", "edge_qids": ["Q1", "Q2", "Q10"]})})
        crossrefs = self.source("wikidata-crossrefs", {"wikidata_crossrefs": core.artifact({"xrefs": {"Q1": {"nlab": "one"}}}),
            "requested_qid_scope": core.canonical({"schema": "wikilean.wikidata-crossref-scope/v1", "qids": ["Q1"]})})
        def member(source, obj, path): return [{"source": source["source"], "object": obj, "path": path}]
        self.plan = {"schema": core.PLAN_SCHEMA, "curated_git_commit": commit, "parents": self.sources,
            "reviewed_parent_manifest_ids": {name: value["source_manifest_id"] for name, value in self.manifests.items()}, "options": copy.deepcopy(core.OPTIONS),
            "bindings": {"annotations": member(annotations, "article-" + core.sha(b"One"), "site/annotations/One.json"),
                "mathlib-source-tree": member(mathlib, "file-" + core.sha(lean_path.encode()), lean_path),
                "declaration-oracle": member(docs, "declaration_oracle", "declaration-data.bmp"),
                "statement-formal": member(hf, "statement_formal_csv", "statement_formal.csv"),
                "formal-dependency": member(hf, "formal_dependency_csv", "formal_dependency.csv"),
                "theorem-matching": member(matching, "theorem_matching_csv", "theorem_matching.csv"),
                "wikidata-edges": member(wd, "wikidata_edges", "wikidata_edges.jsonl"),
                "wikidata-crossrefs": member(crossrefs, "wikidata_crossrefs", "wikidata_crossrefs.json")}}
        self.roots = {"fixture_parent": self.parents}

    def git(self, *arguments):
        return subprocess.check_output(["/usr/bin/git", "-C", str(self.repository), *arguments], stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "LC_ALL": "C"})

    @staticmethod
    def csv(rows):
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
        return output.getvalue().encode()

    def source(self, name, data, *, parent=None, git_commit=None, support=None):
        def ref(path, raw):
            target = self.parents / path; target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            return {"root": "fixture_parent", "path": path, "sha256": core.sha(raw), "bytes": len(raw), "media_type": "application/json"}
        objects = [{"name": obj, **ref("objects/" + core.sha(raw), raw), "roles": ["normalized", "raw"] if parent is None else ["normalized"],
                    "redistribution": "restricted"} for obj, raw in sorted(data.items())]
        pin = {"type": "git_commit", "value": git_commit} if git_commit else {"type": "dataset_revision", "value": "fixture-v1"}
        receipts, preimages, receipt_ids, lineage_inputs, parent_ids = [], [], [], [], []
        if parent is None:
            request_ref = ref(name + "/request", b"")
            requests = [{"kind": "http_get", "uri": "https://example.invalid/" + name, "parameters_sha256": core.sha(b"")}]
            receipt = {"schema": core.contracts.ACQUISITION_RECEIPT_SCHEMA_V1, "acquisition_receipt_id": "sha256:" + "0" * 64,
                "source": name, "pin": pin, "tool": TOOL, "upstream_uri": "https://example.invalid/" + name, "requests": requests,
                "batch": {"request_set_root": core.contracts.acquisition_request_set_root(requests), "requests_failed": 0,
                    "requests_succeeded": 1, "requests_total": 1, "status": "complete"},
                "outputs": [core.object_ref(item) for item in objects], "audit": {"acquired_at": WHEN}}
            receipt["acquisition_receipt_id"] = core.contracts.acquisition_receipt_identity(receipt)
            receipt_ids = [receipt["acquisition_receipt_id"]]
            receipts = [{**ref(name + "/receipt.json", core.canonical(receipt)), "acquisition_receipt_id": receipt_ids[0]}]
            preimages = [{**request_ref, "parameters_sha256": core.sha(b"")}]
            lineage_inputs = [{**core.object_ref(item), "origin": {"kind": "acquisition_receipt", "id": receipt_ids[0]}} for item in objects]
        else:
            parent_id = self.manifests[parent["source"]]["source_manifest_id"]
            parent_ids = [parent_id]
            raw = [{**item, "roles": ["raw"]} for item in parent["objects"] if "normalized" in item["roles"]]
            objects += raw
            lineage_inputs = [{**core.object_ref(item), "origin": {"kind": "source_manifest", "id": parent_id}} for item in raw]
        lineage = {"schema": core.contracts.NORMALIZATION_LINEAGE_SCHEMA_V1, "normalization_lineage_id": "sha256:" + "0" * 64,
            "source": name, "mode": "identity" if parent is None else "transform", "acquisition_receipt_ids": receipt_ids,
            "parent_source_manifest_ids": parent_ids, "normalization_schema": "fixture.normalized/v1", "configuration_sha256": "e" * 64,
            "tool": TOOL, "inputs": sorted(lineage_inputs, key=lambda item: item["object"]),
            "outputs": [core.object_ref(item) for item in sorted(objects, key=lambda item: item["name"]) if "normalized" in item["roles"]],
            "result": "complete", "audit": {"normalized_at": WHEN}}
        lineage["normalization_lineage_id"] = core.contracts.normalization_lineage_identity(lineage)
        objects.extend({"name": obj, **ref("objects/" + core.sha(raw), raw), "roles": ["receipt"], "redistribution": "restricted"}
                       for obj, raw in sorted((support or {}).items()))
        source = {"source": name, "source_kind": "acquired_dataset" if parent is None else "sealed_snapshot", "pin": pin,
            "objects": sorted(objects, key=lambda item: item["name"]), "license": {"expression": "CC0-1.0", "redistribution": "restricted"},
            "acquisition": TOOL, "normalization": {"schema": "fixture.normalized/v1", "tool": TOOL,
                "inputs": sorted(item["name"] for item in objects if "raw" in item["roles"]),
                "outputs": sorted(item["name"] for item in objects if "normalized" in item["roles"])},
            "evidence": {"acquisition_receipts": receipts, "request_parameter_preimages": preimages,
                "normalization_lineage": {**ref(name + "/lineage.json", core.canonical(lineage)), "normalization_lineage_id": lineage["normalization_lineage_id"]}}}
        self.manifests[name] = core.source_plan_contracts._source_manifest_from_plan(source, "fixture")
        self.sources.append(source)
        return source

    def build(self):
        return producer.export(self.plan, self.roots, self.repository, self.store, normalized_at=WHEN)

    def test_complete_export_reconstructs_all_six_outputs_and_original_lineage(self):
        path = self.build()
        verified = producer.verify(path, self.roots)
        self.assertEqual(len(verified["derived_source_manifest_ids"]), 4)
        fragment = json.loads((path / "source-fragment.json").read_bytes())
        self.assertEqual({row["input_id"] for row in fragment["input_bindings"]}, set(core.OUTPUTS))
        for original in self.plan["parents"]:
            self.assertIn(original, fragment["sources"])
        graph = json.loads((path / "normalized/catalog/data/concept_graph_v2.json").read_bytes())
        self.assertEqual(graph["nodes"][0]["xrefs"], {"nlab": "one"})
        self.assertEqual(self.build(), path)

    def test_dirty_worktree_and_ambient_cache_do_not_supply_curated_inputs(self):
        first = self.build()
        for relative in core.CURATED_PATHS.values(): (self.repository / relative).write_bytes(b"uncommitted wrong bytes")
        with mock.patch.dict(os.environ, {"WIKILEAN_REPLAY_CONTEXT": "/absent", "HTTPS_PROXY": "https://unused.invalid"}), \
                mock.patch.object(core.adapters.build_graph_v2, "XREFS", self.root / "absent"), \
                mock.patch("socket.socket", side_effect=AssertionError("network access")):
            self.assertEqual(self.build(), first)

    def test_actual_pack_compiler_reads_curated_git_members_and_private_child_copies(self):
        import test_compile_offline_pack_v2 as compiler_fixture
        path = self.build()
        fragment = json.loads((path / "source-fragment.json").read_bytes())
        selected = [source for source in fragment["sources"] if source["source"] in
                    {"wikilean-derived-curation", "wikilean-derived-concept-layer"}]
        curated_source = next(source for source in selected if source["source_kind"] == "curated_git_tree")
        self.assertEqual({item["root"] for item in curated_source["objects"]}, {"repo"})
        self.assertEqual({item["path"] for item in curated_source["objects"]}, set(core.CURATED_PATHS.values()))
        fixture = compiler_fixture.OfflinePackCompilerTest()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture._upgrade_plan_v3()
        def relocate(value):
            if isinstance(value, dict):
                return {key: "fixture_repo" if key == "root" and item == "repo" else relocate(item) for key, item in value.items()}
            if isinstance(value, list): return [relocate(item) for item in value]
            return value
        inventory = relocate(fixture.inventory)
        for root in inventory["roots"]:
            if root["id"] == "repo": root["id"] = "fixture_repo"
        inventory["roots"].append({"id": "derived_normalized", "kind": "external_tree"})
        inventory["roots"].sort(key=lambda root: root["id"])
        inventory["inputs"].append({"id": "concept-layer", "class": "immutable_source_object", "cardinality": "one",
            "consumers": ["brain/replay.py"], "path": core.OUTPUTS["concept-layer"], "purpose": "derived source integration",
            "requirement": "required", "root": "derived_normalized"})
        inventory["inputs"].sort(key=lambda item: item["id"])
        inventory["inventory_id"] = core.contracts.reducer_input_inventory_identity(inventory)
        plan = relocate(fixture.plan)
        plan["inventory_id"] = inventory["inventory_id"]
        plan["sources"] = sorted([*plan["sources"], *selected], key=lambda source: source["source"])
        plan["input_bindings"].append(next(binding for binding in fragment["input_bindings"] if binding["input_id"] == "concept-layer"))
        plan["input_bindings"].sort(key=lambda binding: binding["input_id"])
        fixture.inventory_path.write_bytes(core.canonical(inventory))
        fixture.plan_path.write_bytes(core.canonical(plan))
        # Both the curated parent and its derived raw inputs must still work
        # after the checkout is dirtied: Git reads and private copies differ.
        for relative in core.CURATED_PATHS.values(): (self.repository / relative).write_bytes(b"uncommitted")
        packed = compiler_fixture.compiler.compile_offline_pack_v2(fixture.plan_path.resolve(), fixture.inventory_path.resolve(),
            (fixture.base / "derived-pack").resolve(), roots={"fixture_repo": fixture.repo.resolve(), "external": fixture.external.resolve(),
                "repo": self.repository, core.PHYSICAL_ROOT: path, "derived_normalized": path / "normalized"}, git_executable="/usr/bin/git")
        manifest, _ = core.contracts.load_canonical_json(packed.manifest_path)
        core.contracts.verify_offline_pack_files(manifest, packed.root, manifest_path=packed.manifest_path)

    def test_rehashed_changed_normalized_output_still_fails_independent_reduction(self):
        path = self.build()
        changed = path / "normalized/catalog/data/concept_graph_v2.json"
        changed.write_bytes(core.artifact({"nodes": [], "edges": []}))
        self.rehash(path)
        with self.assertRaisesRegex(core.DerivationError, "independent normalization"):
            producer.verify(path, self.roots)

    def rehash(self, path):
        control = json.loads((path / "export.json").read_bytes())
        for ref in control["files"]:
            raw = (path / ref["path"]).read_bytes(); ref.update(sha256=core.sha(raw), bytes=len(raw))
        control["export_id"] = core.contracts.domain_hash("wikilean.derived-catalog-export.v1", {key: value for key, value in control.items() if key != "export_id"})
        (path / "export.json").write_bytes(core.canonical(control))

    def test_git_proof_rejects_fabricated_committed_blob(self):
        curated, tree, _, proof = core.capture_curated(self.repository, self.plan["curated_git_commit"])
        curated["grounding"] += b" "
        with self.assertRaisesRegex(core.DerivationError, "pinned regular Git object"):
            core.verify_curated_proof(curated, self.plan["curated_git_commit"], tree, proof)

    def test_parent_bytes_are_verified_before_normalization(self):
        member = core.selected_member(self.plan, "theorem-matching")
        item = next(row for source in self.sources if source["source"] == member["source"] for row in source["objects"] if row["name"] == member["object"])
        (self.parents / item["path"]).write_bytes(b"bad CSV")
        with self.assertRaisesRegex(core.DerivationError, "captured input differs"):
            self.build()
        self.assertEqual(list(self.store.iterdir()), [])

    def test_source_name_cannot_substitute_for_an_explicitly_reviewed_identity(self):
        self.plan["reviewed_parent_manifest_ids"]["mathlib-source"] = "sha256:" + "9" * 64
        with self.assertRaisesRegex(core.DerivationError, "reviewed manifest identity"):
            self.build()

    def test_formal_csvs_cannot_mix_parent_generations(self):
        other = self.source("other-math-graph", {"formal_dependency_csv": b"src_id,dep_id,edge_type\n"}, git_commit="f" * 40)
        self.plan["reviewed_parent_manifest_ids"][other["source"]] = self.manifests[other["source"]]["source_manifest_id"]
        self.plan["bindings"]["formal-dependency"][0]["source"] = other["source"]
        with self.assertRaisesRegex(core.DerivationError, "same complete HF generation"):
            self.build()

    def test_reviewed_edge_observation_must_cover_the_graph_universe(self):
        self.sources[:] = [source for source in self.sources if source["source"] != "wikidata-observation"]
        self.source("wikidata-observation", {"wikidata_edges": b'{"s":"Q2","o":"Q3","p":"P31"}\n'},
            support={"request_plan": core.canonical({"schema": "wikilean.wikidata-observation-plan/v2", "edge_qids": ["Q2", "Q3"]})})
        self.plan["reviewed_parent_manifest_ids"]["wikidata-observation"] = self.manifests["wikidata-observation"]["source_manifest_id"]
        with self.assertRaisesRegex(core.DerivationError, "edge observation scope omits a grounded QID"):
            self.build()

    def test_observation_qid_scope_uses_numeric_length_order(self):
        self.build()  # The fixture's normative scope is Q1, Q2, Q10.
        self.sources[:] = [source for source in self.sources if source["source"] != "wikidata-observation"]
        self.source("wikidata-observation", {"wikidata_edges": b'{"s":"Q1","o":"Q2","p":"P31"}\n'},
            support={"request_plan": core.canonical({"schema": "wikilean.wikidata-observation-plan/v2", "edge_qids": ["Q1", "Q10", "Q2"]})})
        self.plan["reviewed_parent_manifest_ids"]["wikidata-observation"] = self.manifests["wikidata-observation"]["source_manifest_id"]
        with self.assertRaisesRegex(core.DerivationError, "unsupported plan"):
            self.build()

    def test_queried_crossref_scope_must_cover_grounding_and_returned_qids(self):
        for qids, xrefs, message in ((["Q2"], {}, "omits a grounded QID"), (["Q1"], {"Q2": {}}, "excludes a returned QID")):
            self.sources[:] = [source for source in self.sources if source["source"] != "wikidata-crossrefs"]
            self.source("wikidata-crossrefs", {"wikidata_crossrefs": core.artifact({"xrefs": xrefs}),
                "requested_qid_scope": core.canonical({"schema": "wikilean.wikidata-crossref-scope/v1", "qids": qids})})
            self.plan["reviewed_parent_manifest_ids"]["wikidata-crossrefs"] = self.manifests["wikidata-crossrefs"]["source_manifest_id"]
            with self.subTest(qids=qids), self.assertRaisesRegex(core.DerivationError, message):
                self.build()

    def test_no_replace_preserves_changed_existing_target(self):
        path = self.build()
        sentinel = path / "source-fragment.json"; sentinel.write_bytes(b"unrelated changed content")
        with self.assertRaisesRegex(core.DerivationError, "captured input differs"):
            self.build()
        self.assertEqual(sentinel.read_bytes(), b"unrelated changed content")
        self.assertEqual(list(self.store.iterdir()), [path])

    def test_ordinary_publication_failure_removes_owned_staging(self):
        with mock.patch.object(core.stage_io, "_rename_no_replace", side_effect=OSError("fixture rename failure")):
            with self.assertRaisesRegex(OSError, "rename failure"):
                self.build()
        self.assertEqual(list(self.store.iterdir()), [])

    def test_error_after_kernel_rename_cleans_owned_final_even_when_staging_is_recreated(self):
        rename = core.stage_io._rename_no_replace
        replaced = []
        def uncertain(source, destination):
            rename(source, destination)
            source.mkdir(mode=0o700)
            (source / "unrelated").write_bytes(b"preserve")
            replaced.append(source)
            raise OSError("fixture uncertain rename")
        with mock.patch.object(core.stage_io, "_rename_no_replace", side_effect=uncertain):
            with self.assertRaisesRegex(OSError, "uncertain rename"):
                self.build()
        self.assertEqual(list(self.store.iterdir()), replaced)
        self.assertEqual((replaced[0] / "unrelated").read_bytes(), b"preserve")

    def test_postpublication_failure_removes_owned_output(self):
        actual = producer.captured_bundle
        calls = []
        def fail_final(path):
            calls.append(path)
            if len(calls) == 2:
                raise core.DerivationError("fixture final verification failure")
            return actual(path)
        with mock.patch.object(producer, "captured_bundle", side_effect=fail_final):
            with self.assertRaisesRegex(core.DerivationError, "final verification failure"):
                self.build()
        self.assertEqual(list(self.store.iterdir()), [])

    def test_unreviewed_rehashed_tool_preimage_is_rejected(self):
        path = self.build()
        target = path / "normalization/tool-profile.json"
        profile = json.loads(target.read_bytes())
        profile["files"][0]["sha256"] = "f" * 64
        profile["profile_id"] = core.profile_id(profile)
        target.write_bytes(core.canonical(profile)); self.rehash(path)
        with self.assertRaisesRegex(core.DerivationError, "reviewed complete generation"):
            producer.verify(path, self.roots)

    def test_audit_time_changes_export_generation_but_not_derived_source_identity(self):
        first = self.build()
        second = producer.export(self.plan, self.roots, self.repository, self.store, normalized_at="2026-09-09T20:00:00Z")
        left = json.loads((first / "export.json").read_bytes())
        right = json.loads((second / "export.json").read_bytes())
        self.assertNotEqual(first, second)
        self.assertEqual(left["derived_source_manifest_ids"], right["derived_source_manifest_ids"])

    def test_parent_receipt_lineage_and_preimage_are_required(self):
        self.plan["parents"] = [row for row in self.sources if row["source"] != "wikilean-d1"]
        del self.plan["reviewed_parent_manifest_ids"]["wikilean-d1"]
        with self.assertRaisesRegex(core.DerivationError, "parent evidence is missing"):
            self.build()

    def test_explicit_options_and_input_members_cannot_be_omitted(self):
        for mutation in (lambda p: p["options"].update(theorem_matching_tier="exact"),
                         lambda p: p["bindings"].pop("wikidata-crossrefs"),
                         lambda p: p["bindings"]["mathlib-source-tree"].clear()):
            changed = copy.deepcopy(self.plan); mutation(changed)
            with self.assertRaises(core.DerivationError): core.validate_plan(changed)

    def test_csv_duplicate_headers_and_ragged_rows_fail(self):
        for data in (b"a,a\n1,2\n", b"a,b\n1\n", b"a,b\n1,2,3\n"):
            path = self.root / "bad.csv"; path.write_bytes(data)
            with self.assertRaises(core.DerivationError): list(core.csv_rows(path))

    def test_descriptor_capture_rejects_aliases_and_copies_verified_bytes(self):
        raw = self.root / "raw"; raw.write_bytes(b"verified")
        copy_path = self.root / "captured"
        core.capture_file(raw, {"sha256": core.sha(b"verified"), "bytes": 8}, copy_path)
        raw.write_bytes(b"later")
        self.assertEqual(copy_path.read_bytes(), b"verified")
        alias = self.root / "alias"; alias.symlink_to(raw)
        with self.assertRaises(core.DerivationError): core.read(alias)
        alias.unlink(); os.link(raw, alias)
        with self.assertRaises(core.DerivationError): core.read(raw)

    def test_module_origin_and_postload_helper_change_fail_closed(self):
        with mock.patch.object(core.adapters, "__file__", str(self.root / "wrong.py")):
            with self.assertRaisesRegex(core.DerivationError, "origin"):
                core.current_profile()
        original = producer.LOADED_IMPLEMENTATION
        with mock.patch.object(producer, "LOADED_IMPLEMENTATION", {**original, "catalog/build_hierarchy.py": b"changed"}):
            with self.assertRaisesRegex(core.DerivationError, "after loading"):
                producer.implementation()


if __name__ == "__main__":
    unittest.main()
