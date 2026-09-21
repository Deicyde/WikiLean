#!/usr/bin/env python3
"""Hermetic provenance and private publication tests for D1 source exports."""
from __future__ import annotations

import copy
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import acquire_d1_snapshot as acquirer  # noqa: E402
import d1_snapshot_bundle as snapshots  # noqa: E402
import export_d1_sources as exporter  # noqa: E402

contracts = exporter.contracts
AUDIT = "2026-09-08T15:00:00Z"
LATER = "2026-09-08T16:00:00Z"
PROGRAMS = {path: ("# fixture " + path + "\n").encode() for path in exporter.IMPLEMENTATION_PATHS}
REGISTRY = contracts.canonical_json_bytes({"our_data_license": {"annotations": "CC0-1.0"}})


def toolchain(generation: int = 0) -> dict:
    wrapper, dependencies = snapshots.REVIEWED_ACQUIRER_GENERATIONS[generation]
    return {
        "schema": snapshots.TOOLCHAIN_SCHEMA,
        "invocation": {"config_sha256": snapshots.CONFIG_SHA256, "database_binding": snapshots.D1_BINDING,
                       "forwarded_environment": snapshots.FORWARDED_ENVIRONMENT,
                       "forced_environment": snapshots.FORCED_ENVIRONMENT},
        "node": {"version": "v22.0.0", "sha256": "1" * 64},
        "python": {"implementation": "CPython", "version": "3.12.12", "sha256": "2" * 64,
                   "startup_flags": snapshots.REQUIRED_PYTHON_STARTUP_FLAGS},
        "local_dependencies": copy.deepcopy(list(dependencies)),
        "wrangler": {"version": snapshots.WRANGLER_VERSION, "package_integrity": snapshots.WRANGLER_INTEGRITY,
                     "cli_sha256": snapshots.WRANGLER_CLI_SHA256,
                     "package_lock_sha256": snapshots.PACKAGE_LOCK_SHA256},
        "wrapper": {"sha256": wrapper},
    }


def rows(*, edge: bool = False, node: bool = False, annotations: str | None = None) -> list[dict]:
    def record(kind: str, key: str, value: dict) -> dict:
        return {"record_type": kind, "record_key": key,
                "payload": json.dumps(value, ensure_ascii=False, separators=(",", ":"))}

    result = [record("article", "Cafe\u0301_group", {
        "slug": "Cafe\u0301_group", "wikipedia_title": "Café group", "display_title": "Café group",
        "wikidata_qid": "Q181296", "revid": 123, "latest_revid": 124,
        "last_upstream_check": 1700000000000,
        "annotations": annotations or '[{"text":"café","provenance":"human","deleted":true,"score":0.1234567890123456789}]',
        "schema_version": 3, "version": 9, "n_formalized": 1, "n_partial": 0,
        "n_not_formalized": 0, "created_at": 1600000000000, "updated_at": 1700000000000,
    })]
    if edge:
        result.append(record("brain_edge", "aaaaaaaaaaaa", {
            "id": "aaaaaaaaaaaa", "src": "Q181296", "dst": "decl:Mathlib:CommGroup",
            "kind": "formalizes", "evidence": '{"note":"reviewed"}', "added_by": "jack",
            "actor_type": "human", "status": "live", "created_at": 1700000000001,
            "deleted_by": None, "deleted_at": None, "version": 1,
        }))
    if node:
        result.append(record("brain_node", "Q5530428", {
            "id": "Q5530428", "label": "GNS construction", "description": None,
            "node_type": "concept", "added_by": "pipeline", "actor_type": "ai", "status": "live",
            "created_at": 1700000000003, "deleted_by": None, "deleted_at": None, "version": 1,
        }))
    result.append(record("control", "counts", {
        "schema": snapshots.CONTROL_SCHEMA, "articles": 1, "brain_edges": int(edge), "brain_nodes": int(node),
        "article_columns": list(snapshots.ARTICLE_TABLE_COLUMNS),
        "brain_edge_columns": list(snapshots.EDGE_FIELDS), "brain_node_columns": list(snapshots.NODE_FIELDS),
        "rows_total": 1 + int(edge) + int(node),
    }))
    return result


def fixture(*, generation: int = 0, audit: str = AUDIT, **row_options) -> tuple[Path, dict[str, bytes]]:
    chain = toolchain(generation)
    tool = {"name": "wikilean-d1-acquirer", "version": "1",
            "sha256": exporter.digest(contracts.canonical_json_bytes(chain))}
    canonical = acquirer.parse_wrangler_output(json.dumps([{"success": True, "results": rows(**row_options)}]))
    with mock.patch.object(acquirer, "LOADED_SCRIPT_SHA256", chain["wrapper"]["sha256"]):
        identity, files = acquirer._bundle_files(canonical, acquisition_tool=tool,
                                                acquisition_toolchain=chain, audit_time=audit)
    return Path("/fixture") / identity.removeprefix("sha256:"), files


def build(path: Path, files: dict[str, bytes], *, audit: str = AUDIT) -> dict[str, bytes]:
    return exporter.build_export(path, files, normalized_at=audit, implementation=PROGRAMS, registry_bytes=REGISTRY)


def document(files: dict[str, bytes], name: str) -> dict:
    return contracts.parse_json_bytes(files[name], location=name)


class D1GenerationTests(unittest.TestCase):
    def test_current_and_historical_complete_bundles_verify(self) -> None:
        for generation in range(len(snapshots.REVIEWED_ACQUIRER_GENERATIONS)):
            with self.subTest(generation=generation):
                path, files = fixture(generation=generation)
                self.assertEqual(len(snapshots.verify_snapshot_bundle_files(path, files).articles), 1)

    def test_cross_generation_wrapper_and_dependencies_are_rejected(self) -> None:
        for current, other in ((0, 1), (1, 0)):
            with self.subTest(current=current):
                value = toolchain(current)
                value["local_dependencies"] = toolchain(other)["local_dependencies"]
                with self.assertRaisesRegex(snapshots.SnapshotBundleError, "reviewed acquisition generation"):
                    snapshots._validate_toolchain(value)

    def test_individually_recognized_dependency_hashes_cannot_be_mixed(self) -> None:
        value = toolchain()
        value["local_dependencies"][1] = toolchain(1)["local_dependencies"][1]
        with self.assertRaisesRegex(snapshots.SnapshotBundleError, "reviewed acquisition generation"):
            snapshots._validate_toolchain(value)

    def test_live_producer_and_consumer_use_same_current_generation(self) -> None:
        self.assertEqual(acquirer.LOADED_SCRIPT_SHA256, snapshots.ACQUIRER_WRAPPER_SHA256)
        self.assertEqual(acquirer._local_dependency_records(), list(snapshots.LOCAL_DEPENDENCY_PINS))


class D1SourceDerivationTests(unittest.TestCase):
    def test_fresh_sidecars_preserve_all_sealed_fields_and_decimal_human_tombstone(self) -> None:
        path, original = fixture()
        verified = snapshots.verify_snapshot_bundle_files(path, original)
        # Building from captured bytes must never read an ambient sidecar.
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("ambient filesystem read")):
            output = build(path, original)
        sidecar = output["normalized/site/annotations/Café_group.json"]
        self.assertEqual(sidecar, contracts.canonical_artifact_json_bytes(dict(verified.articles[0])))
        self.assertIn(b'"provenance":"human"', sidecar)
        self.assertIn(b'"deleted":true', sidecar)
        self.assertIn(b'0.1234567890123456789', sidecar)
        self.assertIn('Cafe\u0301_group'.encode(), sidecar)
        self.assertEqual(output["normalized/brain/data/community_edges.jsonl"], b"")

    def test_original_evidence_and_both_empty_parent_outputs_are_preserved(self) -> None:
        path, original = fixture(generation=1)
        output = build(path, original)
        for name, data in original.items():
            self.assertEqual(output["acquisition/" + name], data)
        parent = document(output, "source-manifests/parent.json")
        child = document(output, "source-manifests/derived.json")
        lineage = document(output, "evidence/export-lineage.json")
        self.assertEqual(parent["normalization"]["outputs"], ["articles", "brain_edges", "brain_nodes", "control"])
        self.assertEqual(lineage["parent_source_manifest_ids"], [parent["source_manifest_id"]])
        self.assertEqual(lineage["acquisition_receipt_ids"], [])
        self.assertTrue(all(row["origin"]["id"] == parent["source_manifest_id"] for row in lineage["inputs"]))
        self.assertEqual(child["source_kind"], "sealed_snapshot")
        self.assertEqual(len([row for row in child["objects"] if row["bytes"] == 0]), 3)
        fragment = document(output, "source-fragment.json")
        self.assertFalse(fragment["source_publishable"])
        self.assertEqual(fragment["redistribution"], "restricted")
        for source in fragment["sources"]:
            self.assertEqual(source["license"]["redistribution"], "restricted")
            self.assertTrue(all(row["redistribution"] == "restricted" for row in source["objects"]))

    def test_audit_generations_are_preserved_without_changing_source_identity(self) -> None:
        path1, files1 = fixture(audit=AUDIT)
        path2, files2 = fixture(audit=LATER)
        first = build(path1, files1, audit=AUDIT)
        later = build(path2, files2, audit=LATER)
        identity1, identity2 = document(first, "export.json"), document(later, "export.json")
        self.assertNotEqual(identity1["bundle_id"], identity2["bundle_id"])
        self.assertNotEqual(identity1["export_id"], identity2["export_id"])
        self.assertEqual(identity1["parent_source_manifest_id"], identity2["parent_source_manifest_id"])
        self.assertEqual(identity1["derived_source_manifest_id"], identity2["derived_source_manifest_id"])
        self.assertNotEqual(first["acquisition/acquisition-receipt.json"], later["acquisition/acquisition-receipt.json"])

    def test_relocation_does_not_change_any_export_bytes(self) -> None:
        path, files = fixture()
        self.assertEqual(build(path, files), build(Path("/different") / path.name, files))

    def test_nonempty_edges_or_nodes_cannot_be_exported_as_verified_absence(self) -> None:
        for options in ({"edge": True}, {"node": True}, {"edge": True, "node": True}):
            with self.subTest(options=options):
                path, files = fixture(**options)
                snapshots.verify_snapshot_bundle_files(path, files)
                with self.assertRaisesRegex(exporter.ExportError, "nonempty community"):
                    build(path, files)

    def test_tampered_raw_normalized_or_evidence_is_rejected_before_export(self) -> None:
        path, files = fixture()
        for name in ("acquired.jsonl", "normalized/articles.jsonl", "acquisition-receipt.json", "normalization-lineage.json"):
            with self.subTest(name=name):
                changed = dict(files)
                changed[name] += b" "
                with self.assertRaises((snapshots.SnapshotBundleError, contracts.VerificationError)):
                    build(path, changed)

    def test_extra_missing_and_mutable_captured_members_are_rejected(self) -> None:
        path, files = fixture()
        extra = {**files, "unexpected": b""}
        missing = dict(files)
        del missing["request.sql"]
        mutable = {**files, "request.sql": bytearray(files["request.sql"])}
        for changed in (extra, missing, mutable):
            with self.assertRaisesRegex(snapshots.SnapshotBundleError, "exact immutable byte closure"):
                build(path, changed)


@unittest.skipUnless(sys.platform == "darwin" or sys.platform.startswith("linux"), "exclusive publication platform")
class D1SourcePublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        path, self.source_files = fixture(generation=1)
        self.bundle = self.root / path.name
        self.bundle.mkdir(mode=0o700)
        for name, data in self.source_files.items():
            destination = self.bundle / name
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            destination.write_bytes(data)
            destination.chmod(0o644)
        self.store = self.root / "export-store"

    def tearDown(self) -> None:
        for directory, names, _files in os.walk(self.root):
            Path(directory).chmod(0o700)
            for name in names:
                (Path(directory) / name).chmod(0o700)
        self.temporary.cleanup()

    def publish(self) -> Path:
        return exporter.export_bundle(self.bundle, self.store, normalized_at=AUDIT)

    def test_private_readonly_publication_is_reused_without_changing_source(self) -> None:
        output = self.publish()
        inode = output.stat().st_ino
        self.assertEqual(self.publish(), output)
        self.assertEqual(output.stat().st_ino, inode)
        self.assertEqual(stat.S_IMODE(self.store.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o555)
        self.assertEqual(stat.S_IMODE((output / "export.json").stat().st_mode), 0o444)
        self.assertFalse(list(self.store.glob(".d1-export-*")))
        for name, data in self.source_files.items():
            self.assertEqual((self.bundle / name).read_bytes(), data)
            self.assertEqual(stat.S_IMODE((self.bundle / name).stat().st_mode), 0o644)

    def test_failure_after_rename_cleans_up_our_publication(self) -> None:
        primitive = exporter.publisher._publish_no_replace

        def rename_then_fail(*args):
            primitive(*args)
            raise OSError("injected after rename")

        with mock.patch.object(exporter.publisher, "_publish_no_replace", side_effect=rename_then_fail):
            with self.assertRaisesRegex(OSError, "injected after rename"):
                self.publish()
        self.assertEqual(list(self.store.iterdir()), [])

    def test_collision_is_preserved_and_candidate_is_cleaned(self) -> None:
        target = self.publish()
        target.chmod(0o700)
        manifest = target / "export.json"
        manifest.chmod(0o644)
        manifest.write_bytes(b"unrelated target")
        manifest.chmod(0o444)
        target.chmod(0o555)
        inode = target.stat().st_ino
        with self.assertRaisesRegex(exporter.ExportError, "published export bytes differ"):
            self.publish()
        self.assertEqual(target.stat().st_ino, inode)
        self.assertEqual(manifest.read_bytes(), b"unrelated target")
        self.assertEqual(list(self.store.iterdir()), [target])

    def test_store_path_replacement_is_rejected_without_removing_the_replacement(self) -> None:
        verify = exporter._verify_written
        displaced = self.root / "displaced-store"

        def replace_store(root, files):
            verify(root, files)
            self.store.rename(displaced)
            self.store.mkdir(mode=0o700)
            (self.store / "unrelated").write_bytes(b"preserve")

        with mock.patch.object(exporter, "_verify_written", side_effect=replace_store):
            with self.assertRaisesRegex(exporter.publisher.PackCompilationError, "inode or ownership changed"):
                self.publish()
        self.assertEqual((self.store / "unrelated").read_bytes(), b"preserve")
        self.assertEqual(list(displaced.iterdir()), [])

    def test_replaced_staging_name_cannot_prevent_owned_target_cleanup(self) -> None:
        primitive = exporter.publisher._publish_no_replace
        for kind in ("directory", "file", "symlink"):
            with self.subTest(kind=kind):
                self.store = self.root / ("export-" + kind)

                def rename_then_replace(descriptor, source, target):
                    primitive(descriptor, source, target)
                    replacement = self.store / source
                    if kind == "directory":
                        replacement.mkdir(mode=0o700)
                        (replacement / "keep").write_bytes(b"unrelated")
                    elif kind == "file":
                        replacement.write_bytes(b"unrelated")
                    else:
                        replacement.symlink_to(self.bundle)
                    raise OSError("injected replaced source")

                with mock.patch.object(exporter.publisher, "_publish_no_replace", side_effect=rename_then_replace):
                    with self.assertRaisesRegex(OSError, "injected replaced source"):
                        self.publish()
                leftovers = list(self.store.iterdir())
                self.assertEqual(len(leftovers), 1)
                self.assertTrue(leftovers[0].name.startswith(".d1-export-"))
                self.assertTrue(leftovers[0].exists())
                if kind == "symlink":
                    self.assertTrue(leftovers[0].is_symlink())
                    leftovers[0].unlink()

    def test_changed_implementation_cannot_be_claimed_as_loaded_source(self) -> None:
        for path in exporter.IMPLEMENTATION_PATHS:
            with self.subTest(path=path):
                changed = {**exporter.LOADED_IMPLEMENTATION, path: b"changed"}
                with mock.patch.object(exporter, "LOADED_IMPLEMENTATION", changed):
                    with self.assertRaisesRegex(exporter.ExportError, "implementation changed"):
                        self.publish()
                self.assertFalse(self.store.exists())


if __name__ == "__main__":
    unittest.main()
