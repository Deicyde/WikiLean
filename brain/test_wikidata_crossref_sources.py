#!/usr/bin/env python3
"""Hermetic semantic, evidence-closure, Git, and publication regressions."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wikidata_crossref_sources as core
import export_wikidata_crossrefs as producer
import test_acquire_wikidata_entities as fixture

WHEN = fixture.AUDIT_TIME


def registry(**entries):
    return core.canonical({"crossref_sources": {key: {"wikidata_property": value} for key, value in entries.items()}})


def statement(value="one", *, pid="P4215", rank="normal", snaktype="value", datatype="external-id", value_type="string"):
    snak = {"property": pid, "snaktype": snaktype, "datatype": datatype}
    if snaktype == "value":
        snak["datavalue"] = {"type": value_type, "value": value}
    return {"rank": rank, "mainsnak": snak}


def claims(rows, *, requested="Q1", canonical="Q1"):
    return {"schema": core.CLAIMS_SCHEMA, "entities": {requested: {
        "requested": requested, "qid": canonical, "claims": {"P4215": rows}}}}


class CrossrefSemanticsTest(unittest.TestCase):
    def reduce(self, rows, **kwargs):
        return core.crossrefs(claims(rows, **kwargs), registry(nlab="P4215"))["xrefs"]

    def test_preferred_replaces_normal_and_deprecated(self):
        self.assertEqual(self.reduce([statement("old"), statement("bad", rank="deprecated"),
                                     statement("current", rank="preferred")]), {"Q1": {"nlab": ["current"]}})

    def test_all_normal_values_when_no_preferred(self):
        self.assertEqual(self.reduce([statement("z"), statement("a"), statement("z"), statement("bad", rank="deprecated")]),
                         {"Q1": {"nlab": ["a", "z"]}})

    def test_preferred_special_values_suppress_normal_before_value_filtering(self):
        for special in ("novalue", "somevalue"):
            with self.subTest(special=special):
                self.assertEqual(self.reduce([statement("old"), statement(rank="preferred", snaktype=special)]), {})

    def test_preferred_concrete_and_special_values_keep_only_concrete(self):
        self.assertEqual(self.reduce([statement("old"), statement(rank="preferred", snaktype="novalue"),
                                     statement("current", rank="preferred")]), {"Q1": {"nlab": ["current"]}})

    def test_no_concrete_external_id_does_not_fabricate_values(self):
        rows = [statement(snaktype="somevalue"), statement(snaktype="novalue"), statement("", rank="normal"),
                statement(42), statement({"id": "Q5"}, value_type="wikibase-entityid", datatype="wikibase-item"),
                statement("https://example.org", datatype="url"), statement("bad", rank="deprecated")]
        self.assertEqual(self.reduce(rows), {})

    def test_invalid_rank_and_mismatched_property_fail_closed(self):
        for row in (statement(rank="unknown"), statement(pid="P1"), {"mainsnak": {}}, statement(snaktype="missing")):
            with self.subTest(row=row), self.assertRaises(core.ExportError):
                self.reduce([row])

    def test_duplicate_property_to_key_mappings_and_slash_properties_are_unioned(self):
        value = claims([statement("one"), statement("one")])
        value["entities"]["Q1"]["claims"]["P646"] = [statement("two", pid="P646"), statement("one", pid="P646")]
        result = core.crossrefs(value, registry(nlab="P4215/P646/P4215", alias="P4215", absent=None))
        self.assertEqual(result["properties"], {"P4215": ["alias", "nlab"], "P646": ["nlab"]})
        self.assertEqual(result["xrefs"], {"Q1": {"alias": ["one"], "nlab": ["one", "two"]}})

    def test_exact_unicode_external_id_is_preserved(self):
        self.assertEqual(self.reduce([statement("Cafe\u0301"), statement("Café")]), {"Q1": {"nlab": ["Cafe\u0301", "Café"]}})

    def test_redirect_uses_original_requested_key(self):
        self.assertEqual(self.reduce([statement()], requested="Q2", canonical="Q20"), {"Q2": {"nlab": ["one"]}})

    def test_null_and_absent_registry_properties_are_ignored(self):
        value = json.loads(registry(nlab="P4215", stacks=None))
        value["crossref_sources"]["kerodon"] = {}
        self.assertEqual(core.properties(core.canonical(value)), {"P4215": ["nlab"]})

    def test_malformed_registry_property_rejected(self):
        for value in ([], "P0", "P1/", "P1//P2", "P1 P2"):
            with self.subTest(value=value), self.assertRaises(core.ExportError):
                core.properties(registry(nlab=value))


class CrossrefExportTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repository, self.store = self.root / "repository", self.root / "exports"
        self.repository.mkdir(mode=0o700)
        self.store.mkdir(mode=0o700)
        self.registry = registry(nlab="P4215", alias="P4215", kgmid="P2671/P646")
        self.git("init", "-q")
        target = self.repository / core.REGISTRY_PATH
        target.parent.mkdir(parents=True)
        target.write_bytes(self.registry)
        self.git("add", core.REGISTRY_PATH)
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "registry fixture")
        self.commit = self.git("rev-parse", "HEAD").decode().strip()
        first = fixture.entity("Q1", label="Cafe\u0301")
        first["claims"]["P4215"] = [statement("a"), statement("preferred", rank="preferred")]
        second = fixture.entity("Q20", requested="Q2")
        second["claims"]["P4215"] = [statement("redirected")]
        responses = [fixture.response({"entities": {"Q1": first, "Q2": second, "Q999": {"id": "Q999", "missing": ""}}})]
        toolchain = fixture.fake_toolchain()
        self.bundle = fixture.acquire.publish_transcript(fixture.plan_bytes(["Q1", "Q2", "Q999"]), responses,
            store=self.root / "captures", acquisition_tool=fixture.fake_tool(toolchain), acquisition_toolchain=toolchain, audit_time=WHEN)

    def git(self, *args):
        return subprocess.run(["/usr/bin/git", "-C", str(self.repository), *args], check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=core.git_snapshot._git_environment()).stdout

    def export(self):
        return producer.export(self.bundle, self.repository, self.commit, self.store, normalized_at=WHEN)

    def rewrite_manifest(self, path):
        files = core.capture_tree(path)
        value = core.parse(files.pop("export.json"), "export")
        value["files"] = {name: {"sha256": core.sha(raw), "bytes": len(raw)} for name, raw in sorted(files.items())}
        value["export_id"] = core.contracts.domain_hash(core.EXPORT_SCHEMA, {key: item for key, item in value.items() if key != "export_id"})
        (path / "export.json").write_bytes(core.canonical(value))

    def test_full_private_export_verified_and_scope_preserved(self):
        path = self.export()
        result = producer.verify(path)
        self.assertEqual((result["requested_qids"], result["crossref_qids"]), (3, 2))
        xrefs = core.parse((path / "normalized/wikidata_crossrefs.json").read_bytes(), "xref", data=True)
        self.assertEqual(xrefs["fetched_from"], core.entities.UPSTREAM_URI)
        self.assertEqual(xrefs["xrefs"], {"Q1": {"alias": ["preferred"], "nlab": ["preferred"]},
                                          "Q2": {"alias": ["redirected"], "nlab": ["redirected"]}})
        scope = json.loads((path / "normalized/requested_qid_scope.json").read_bytes())
        self.assertEqual(scope, {"schema": core.SCOPE_SCHEMA, "qids": ["Q1", "Q2", "Q999"]})
        claimed = json.loads((path / "normalized/entity_claims.json").read_bytes())["entities"]
        self.assertEqual(claimed["Q2"]["qid"], "Q20")
        self.assertEqual(claimed["Q999"], {"missing": True})

    def test_curated_source_uses_only_real_git_native_objects(self):
        path = self.export()
        fragment = json.loads((path / "source-fragment.json").read_bytes())
        curated = next(source for source in fragment["sources"] if source["source_kind"] == "curated_git_tree")
        self.assertEqual([(obj["root"], obj["path"]) for obj in curated["objects"]], [(core.GIT_ROOT, core.REGISTRY_PATH)])
        self.assertNotIn("evidence", curated)
        derived = next(source for source in fragment["sources"] if source["source"] == "wikidata-crossrefs")
        self.assertTrue(any(obj["name"].startswith("git_proof_") for obj in derived["objects"]))

    def test_actual_v3_pack_compiler_accepts_complete_parent_and_curated_closure(self):
        import test_compile_offline_pack_v2 as compiler_fixture
        path = self.export()
        fragment = json.loads((path / "source-fragment.json").read_bytes())
        fixture_pack = compiler_fixture.OfflinePackCompilerTest()
        fixture_pack.setUp()
        self.addCleanup(fixture_pack.tearDown)
        fixture_pack._upgrade_plan_v3()
        plan = fixture_pack.plan
        fixture_pack.inventory["inputs"].append({"id": "wikidata-crossrefs", "class": "immutable_source_object",
            "cardinality": "one", "consumers": ["brain/replay.py"], "path": "catalog/data/wikidata_crossrefs.json",
            "purpose": "verified crossref fixture", "requirement": "required", "root": "repo"})
        fixture_pack.inventory["inputs"].sort(key=lambda item: item["id"])
        fixture_pack.inventory["inventory_id"] = core.contracts.reducer_input_inventory_identity(fixture_pack.inventory)
        fixture_pack.inventory_path.write_bytes(core.canonical(fixture_pack.inventory))
        plan["inventory_id"] = fixture_pack.inventory["inventory_id"]
        plan["sources"] = sorted([*plan["sources"], *fragment["sources"]], key=lambda source: source["source"])
        plan["input_bindings"] = sorted([*plan["input_bindings"], *fragment["input_bindings"]], key=lambda item: item["input_id"])
        fixture_pack.plan_path.write_bytes(core.canonical(plan))
        (fixture_pack.repo / "catalog/data").mkdir(parents=True)
        (fixture_pack.repo / "catalog/data/wikidata_crossrefs.json").write_bytes((path / "normalized/wikidata_crossrefs.json").read_bytes())
        (self.repository / core.REGISTRY_PATH).write_bytes(b"dirty worktree must not supply curated bytes")
        result = compiler_fixture.compiler.compile_offline_pack_v2(fixture_pack.plan_path.resolve(),
            fixture_pack.inventory_path.resolve(), (fixture_pack.base / "crossref-pack").resolve(),
            roots={"repo": fixture_pack.repo.resolve(), "external": fixture_pack.external.resolve(),
                   core.GIT_ROOT: self.repository, core.PHYSICAL_ROOT: path}, git_executable="/usr/bin/git")
        manifest, _ = core.contracts.load_canonical_json(result.manifest_path)
        core.contracts.verify_offline_pack_files(manifest, result.root, manifest_path=result.manifest_path)
        broken = copy.deepcopy(plan)
        child = next(source for source in broken["sources"] if source["source"] == "wikidata-crossrefs")
        child["pin"] = {"type": "content_sha256", "value": "0" * 64}
        with self.assertRaisesRegex(core.contracts.VerificationError, "exactly one raw"):
            core.source_plan_contracts.validate_source_plan_v3(broken)

    def test_bisected_no_such_entity_transcript_keeps_complete_scope(self):
        plan, responses = fixture.complex_fixture()
        toolchain = fixture.fake_toolchain()
        self.bundle = fixture.acquire.publish_transcript(plan, responses, store=self.root / "bisected",
            acquisition_tool=fixture.fake_tool(toolchain), acquisition_toolchain=toolchain, audit_time=WHEN)
        path = self.export()
        self.assertEqual(producer.verify(path)["requested_qids"], 3)
        normalized = json.loads((path / "normalized/entity_claims.json").read_bytes())["entities"]
        self.assertEqual(normalized["Q999"], {"missing": True})
        self.assertEqual(normalized["Q2"]["qid"], "Q20")
        self.assertEqual(set(normalized), {"Q1", "Q2", "Q999"})

    def test_dirty_worktree_registry_cannot_replace_pinned_git_blob(self):
        original = self.export()
        (self.repository / core.REGISTRY_PATH).write_bytes(registry(nlab="P646"))
        self.assertEqual(self.export(), original)

    def test_new_parent_retains_original_and_new_normalization_preimages(self):
        path = self.export()
        parent = json.loads((path / "source-manifests/entities.json").read_bytes())
        self.assertEqual(parent["normalization"]["outputs"], ["entities", "entity_claims"])
        objects = {item["name"]: item for item in parent["objects"]}
        self.assertEqual(parent["normalization"]["tool"]["sha256"], objects["normalizer_profile"]["sha256"])
        self.assertEqual(objects["original_normalization_lineage"]["sha256"], core.sha((self.bundle / "normalization-lineage.json").read_bytes()))
        self.assertEqual(len(parent["evidence"]["request_parameter_preimages"]), 1)
        self.assertTrue(any(name.startswith("acquisition_program_") for name in objects))

    def test_rehashed_derived_output_tamper_fails_independent_replay(self):
        path = self.export()
        (path / "normalized/wikidata_crossrefs.json").write_bytes(core.artifact({"xrefs": {"Q1": {"nlab": ["invented"]}}}))
        self.rewrite_manifest(path)
        with self.assertRaisesRegex(core.ExportError, "independent replay"):
            producer.verify(path)

    def test_rehashed_scope_tamper_fails_independent_replay(self):
        path = self.export()
        (path / "normalized/requested_qid_scope.json").write_bytes(core.canonical({"schema": core.SCOPE_SCHEMA, "qids": ["Q1"]}))
        self.rewrite_manifest(path)
        with self.assertRaisesRegex(core.ExportError, "independent replay"):
            producer.verify(path)

    def test_rehashed_git_registry_tamper_fails_git_proof(self):
        path = self.export()
        (path / "curated/source_registry.json").write_bytes(registry(nlab="P646"))
        self.rewrite_manifest(path)
        with self.assertRaisesRegex(core.ExportError, "pinned regular Git blob"):
            producer.verify(path)

    def test_rehashed_acquisition_code_tamper_fails_source_preimage(self):
        path = self.export()
        (path / "acquisition-implementation/brain/acquire_wikidata_entities.py").write_bytes(b"unreviewed code")
        self.rewrite_manifest(path)
        with self.assertRaisesRegex(core.ExportError, "acquisition program preimages"):
            producer.verify(path)

    def test_rehashed_normalizer_code_tamper_fails_whole_profile(self):
        path = self.export()
        (path / "implementation/brain/wikidata_crossref_sources.py").write_bytes(b"unreviewed code")
        self.rewrite_manifest(path)
        with self.assertRaisesRegex(core.ExportError, "program preimages"):
            producer.verify(path)

    def test_altered_original_receipt_rejected_even_if_export_rehashed(self):
        path = self.export()
        receipt = next((path / "acquisition").glob("*/acquisition-receipt.json"))
        value = json.loads(receipt.read_bytes())
        value["batch"]["requests_succeeded"] = 0
        receipt.write_bytes(core.canonical(value))
        self.rewrite_manifest(path)
        with self.assertRaises(core.entities.WikidataEntityBundleError):
            producer.verify(path)

    def test_permissions_symlinks_and_extra_directories_fail_closed(self):
        path = self.export()
        target = path / "normalized/wikidata_crossrefs.json"
        target.chmod(0o666)
        with self.assertRaisesRegex(core.ExportError, "ownership or mode"):
            producer.verify(path)
        target.chmod(0o644)
        (path / "extra").mkdir(mode=0o700)
        with self.assertRaisesRegex(core.ExportError, "empty directories"):
            producer.verify(path)
        (path / "extra").rmdir()
        sentinel = self.root / "external.json"
        sentinel.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(sentinel)
        with self.assertRaisesRegex(core.ExportError, "real absolute ancestry"):
            producer.verify(path)

    def test_repeated_publication_is_no_replace_and_keeps_original(self):
        path = self.export()
        inode = path.stat().st_ino
        self.assertEqual(self.export(), path)
        self.assertEqual(path.stat().st_ino, inode)
        self.assertEqual(list(self.store.iterdir()), [path])

    def test_failed_publication_cleans_only_staging_and_preserves_existing_export(self):
        path = self.export()
        with mock.patch.object(core.stage_io, "publish_directory_no_replace", side_effect=OSError("fixture failure")):
            with self.assertRaisesRegex(OSError, "fixture failure"):
                self.export()
        self.assertEqual(list(self.store.iterdir()), [path])
        self.assertEqual(producer.verify(path)["requested_qids"], 3)

    def test_wrong_helper_origin_rejected(self):
        with mock.patch.object(core.git_snapshot, "__file__", "/unreviewed/git_snapshot.py"):
            with self.assertRaisesRegex(core.ExportError, "helper module origin"):
                producer.implementation()

    def test_changed_current_checkout_does_not_select_historical_acceptance(self):
        path = self.export()
        with mock.patch.object(core, "current_profile", side_effect=AssertionError("verification must use recorded profile")):
            self.assertEqual(producer.verify(path)["requested_qids"], 3)


if __name__ == "__main__":
    unittest.main()
