#!/usr/bin/env python3
"""Hermetic tests for WikiLean authority manifests and verification commands."""
from __future__ import annotations

import copy
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE / "tools"
sys.path.insert(0, str(TOOLS))

import authority_contracts as contracts  # noqa: E402
import store  # noqa: E402

ZERO_DIGEST = "0" * 64
ZERO_HASH = "sha256:" + ZERO_DIGEST
GIT_COMMIT = "a" * 40


def write_canonical(path: Path, value: object) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = contracts.canonical_json_bytes(value)
    path.write_bytes(data)
    return data


def file_ref(root: Path, relative: str, media_type: str = "application/octet-stream") -> dict[str, object]:
    digest, size = contracts.digest_file(root / relative)
    return {"path": relative, "sha256": digest, "bytes": size, "media_type": media_type}


class CanonicalJsonTest(unittest.TestCase):
    def test_canonical_bytes_and_domain_hash_are_stable(self) -> None:
        value = {"z": [True, None, 4], "a": "é"}
        self.assertEqual(
            contracts.canonical_json_bytes(value),
            b'{"a":"\xc3\xa9","z":[true,null,4]}',
        )
        self.assertEqual(
            contracts.domain_hash("fixture.v1", value),
            "sha256:2744dee221792f937bb5b77b056642e40bf108447522d1fd19124acc9da73c7c",
        )

    def test_strict_parser_rejects_duplicate_keys_numbers_and_non_nfc(self) -> None:
        bad_documents = [
            b'{"a":1,"a":2}',
            b'{"a":1.0}',
            b'{"a":1e2}',
            b'{"a":-0}',
            b'{"a":9007199254740992}',
            b'{"a":NaN}',
            json.dumps({"a": unicodedata.normalize("NFD", "é")}, ensure_ascii=False).encode(),
        ]
        for data in bad_documents:
            with self.subTest(data=data):
                with self.assertRaises(contracts.VerificationError):
                    contracts.parse_json_bytes(data, location="fixture")

    def test_artifact_logical_roots_canonicalize_finite_decimals(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "first.json"
            second = root / "second.json"
            first.write_text('{"xy":[1.0,-0.0,1e2]}')
            second.write_text('{"xy":[1,0,100.000]}')
            self.assertEqual(
                contracts.logical_json_root(first), contracts.logical_json_root(second)
            )
            first.write_text('{"value":1.12345678901234567890123456781}')
            second.write_text('{"value":1.12345678901234567890123456782}')
            self.assertNotEqual(
                contracts.logical_json_root(first), contracts.logical_json_root(second)
            )
            with self.assertRaises(contracts.VerificationError):
                contracts.parse_json_bytes(first.read_bytes(), location="manifest")
            with self.assertRaisesRegex(contracts.VerificationError, "surrogates"):
                contracts.parse_artifact_json_bytes(
                    b'{"value":"\\ud800"}', location="artifact"
                )
            with self.assertRaisesRegex(contracts.VerificationError, "supported bounds"):
                contracts.parse_artifact_json_bytes(
                    b'{"value":1e1000000000000000000}', location="artifact"
                )
            bounded = contracts.parse_artifact_json_bytes(
                b'{"value":1e9999}', location="artifact"
            )
            self.assertEqual(len(contracts._decimal_json(bounded["value"])), 10000)
            self.assertEqual(contracts._normalized_prefix("a😀b", 4, "_"), "a__b")
            self.assertEqual(contracts._normalized_prefix("Kelvin", 2, "_"), "ke")
            with self.assertRaisesRegex(contracts.VerificationError, "table of size 0"):
                contracts._validate_provenance_indexes(
                    {"prov": 0}, 0, "artifact.row"
                )

    def test_jsonl_logical_root_ignores_meta_and_row_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "first.jsonl"
            second = root / "second.jsonl"
            first.write_text('{"_meta":{"generated_at":"one"}}\n{"id":2,"x":1.0}\n{"id":1}\n')
            second.write_text('{"_meta":{"generated_at":"two"}}\n{"id":1}\n{"id":2,"x":1.00}\n')
            self.assertEqual(
                contracts.logical_jsonl_root(first),
                contracts.logical_jsonl_root(second),
            )


class SourceSetVerificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for relative, content in {
            "objects/raw.bin": b"raw bytes\x00",
            "objects/normalized.json": b'{"rows":[1,2]}',
            "tools/reducer.py": b"print('offline')\n",
            "config/reducer.json": b'{"cap":8}',
            "schemas/input.json": b'{"type":"object"}',
        }.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_source_manifest(self) -> dict[str, object]:
        manifest: dict[str, object] = {
            "schema": contracts.SOURCE_SCHEMA,
            "source_manifest_id": ZERO_HASH,
            "source": "fixture",
            "pin": {"type": "git_commit", "value": GIT_COMMIT},
            "objects": [
                {"role": "normalized", **file_ref(self.root, "objects/normalized.json", "application/json")},
                {"role": "raw", **file_ref(self.root, "objects/raw.bin")},
            ],
            "license": {
                "expression": "CC0-1.0",
                "redistribution": "allowed",
                "attribution": None,
            },
            "acquisition": {"name": "fixture-fetch", "version": "1", "sha256": ZERO_DIGEST},
            "normalization": {
                "schema": "fixture/v1",
                "tool": {"name": "fixture-normalize", "version": "1", "sha256": ZERO_DIGEST},
            },
            "audit": {"acquired_at": "2030-01-01T00:00:00Z"},
        }
        manifest["source_manifest_id"] = contracts.source_manifest_identity(manifest)
        return manifest

    def make_pack(self, source_manifest: dict[str, object]) -> dict[str, object]:
        source_path = "manifests/fixture.json"
        write_canonical(self.root / source_path, source_manifest)
        objects = [
            file_ref(self.root, "objects/normalized.json", "application/json"),
            file_ref(self.root, "objects/raw.bin"),
        ]
        objects.sort(key=lambda item: str(item["path"]))
        manifest_ref = {
            **file_ref(self.root, source_path, "application/json"),
            "source_manifest_id": source_manifest["source_manifest_id"],
        }
        pack: dict[str, object] = {
            "schema": contracts.PACK_SCHEMA,
            "offline_pack_id": ZERO_HASH,
            "source_set_root": contracts.source_set_root([str(source_manifest["source_manifest_id"])]),
            "source_manifests": [manifest_ref],
            "objects": objects,
            "reducer": file_ref(self.root, "tools/reducer.py", "text/x-python"),
            "configuration": file_ref(self.root, "config/reducer.json", "application/json"),
            "schemas": [file_ref(self.root, "schemas/input.json", "application/schema+json")],
            "audit": {"created_at": "2030-01-01T00:00:00Z"},
        }
        pack["offline_pack_id"] = contracts.offline_pack_identity(pack)
        return pack

    def test_verifies_complete_offline_pack_and_ignores_audit_timestamp_for_id(self) -> None:
        source = self.make_source_manifest()
        pack = self.make_pack(source)
        pack_path = self.root / "pack.json"
        write_canonical(pack_path, pack)
        result = contracts.verify_offline_pack_files(
            contracts.validate_offline_pack(pack), self.root, manifest_path=pack_path
        )
        self.assertEqual(result, {"source_manifests": 1, "source_objects": 2, "files": 6})

        changed = copy.deepcopy(pack)
        changed["audit"]["created_at"] = "2040-01-01T00:00:00Z"  # type: ignore[index]
        self.assertEqual(contracts.offline_pack_identity(pack), contracts.offline_pack_identity(changed))

        process = subprocess.run(
            [sys.executable, str(TOOLS / "verify_source_set.py"), "--manifest", str(pack_path), "--root", str(self.root)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["offline_pack_id"], pack["offline_pack_id"])

    def test_rejects_digest_length_unknown_version_and_noncanonical_bytes(self) -> None:
        source = self.make_source_manifest()
        pack = self.make_pack(source)

        bad_digest = copy.deepcopy(pack)
        bad_digest["objects"][0]["sha256"] = ZERO_DIGEST  # type: ignore[index]
        bad_digest["offline_pack_id"] = contracts.offline_pack_identity(bad_digest)
        with self.assertRaisesRegex(contracts.VerificationError, "sha256"):
            contracts.verify_offline_pack_files(contracts.validate_offline_pack(bad_digest), self.root)

        bad_length = copy.deepcopy(pack)
        bad_length["objects"][0]["bytes"] += 1  # type: ignore[index,operator]
        bad_length["offline_pack_id"] = contracts.offline_pack_identity(bad_length)
        with self.assertRaisesRegex(contracts.VerificationError, "bytes"):
            contracts.verify_offline_pack_files(contracts.validate_offline_pack(bad_length), self.root)

        bad_version = copy.deepcopy(pack)
        bad_version["schema"] = "wikilean.offline-pack/v2"
        with self.assertRaisesRegex(contracts.VerificationError, "unknown schema/version"):
            contracts.validate_offline_pack(bad_version)

        noncanonical = self.root / "noncanonical.json"
        noncanonical.write_text(json.dumps(pack, indent=2))
        with self.assertRaisesRegex(contracts.VerificationError, "not canonical"):
            contracts.load_canonical_json(noncanonical)

    def test_rejects_unreferenced_source_object(self) -> None:
        source = self.make_source_manifest()
        pack = self.make_pack(source)
        extra = self.root / "objects/extra.bin"
        extra.write_bytes(b"extra")
        pack["objects"].append(file_ref(self.root, "objects/extra.bin"))
        pack["objects"].sort(key=lambda item: item["path"])
        pack["offline_pack_id"] = contracts.offline_pack_identity(pack)
        with self.assertRaisesRegex(contracts.VerificationError, "unreferenced source objects"):
            contracts.verify_offline_pack_files(contracts.validate_offline_pack(pack), self.root)

    def test_rejects_undeclared_pack_file(self) -> None:
        source = self.make_source_manifest()
        pack = self.make_pack(source)
        (self.root / "extra.txt").write_text("not declared")
        with self.assertRaisesRegex(contracts.VerificationError, "undeclared files"):
            contracts.verify_offline_pack_files(pack, self.root)

    def test_requires_manifest_inside_pack_root_and_rejects_special_entries(self) -> None:
        source = self.make_source_manifest()
        pack = self.make_pack(source)
        outside = Path(self.temp.name).parent / f"{self.root.name}-outside-pack.json"
        write_canonical(outside, pack)
        try:
            with self.assertRaisesRegex(contracts.VerificationError, "must reside beneath"):
                contracts.verify_offline_pack_files(
                    pack, self.root, manifest_path=outside
                )
        finally:
            outside.unlink(missing_ok=True)

        fifo = self.root / "undeclared.fifo"
        try:
            os.mkfifo(fifo)
        except (AttributeError, OSError, NotImplementedError):
            self.skipTest("FIFOs are unavailable")
        try:
            with self.assertRaisesRegex(contracts.VerificationError, "non-regular entry"):
                contracts.verify_offline_pack_files(pack, self.root)
        finally:
            fifo.unlink(missing_ok=True)

    def test_rejects_traversal_and_symlink(self) -> None:
        source = self.make_source_manifest()
        pack = self.make_pack(source)
        traversal = copy.deepcopy(pack)
        traversal["objects"][0]["path"] = "../escape"  # type: ignore[index]
        traversal["offline_pack_id"] = contracts.offline_pack_identity(traversal)
        with self.assertRaisesRegex(contracts.VerificationError, "normalized, relative"):
            contracts.validate_offline_pack(traversal)

        target = self.root / "outside.bin"
        target.write_bytes(b"outside")
        link = self.root / "objects" / "link.bin"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable")
        ref = file_ref(self.root, "objects/link.bin")
        with self.assertRaisesRegex(contracts.VerificationError, "cannot safely open"):
            contracts.verify_file_ref(self.root, ref, "$.objects[0]")

    def test_offline_runner_executes_verified_python_and_blocks_socket_access(self) -> None:
        source = self.make_source_manifest()

        success_pack = self.make_pack(source)
        success_path = self.root / "success-pack.json"
        write_canonical(success_path, success_pack)
        success = subprocess.run(
            [sys.executable, str(TOOLS / "run_offline.py"),
             "--manifest", str(success_path), "--root", str(self.root)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertEqual(success.stdout, "offline\n")
        success_path.unlink()

        helper_path = self.root / "tools/helper.py"
        helper_path.write_text("VALUE = 'sibling-import'\n")
        reducer_path = self.root / "tools/reducer.py"
        reducer_path.write_text("from helper import VALUE\nprint(VALUE)\n")
        import_pack = self.make_pack(source)
        # Exact closure requires every reducer module to be declared.
        import_pack["schemas"].append(file_ref(self.root, "tools/helper.py", "text/x-python"))
        import_pack["schemas"].sort(key=lambda item: item["path"])
        import_pack["offline_pack_id"] = contracts.offline_pack_identity(import_pack)
        import_path = self.root / "import-pack.json"
        write_canonical(import_path, import_pack)
        imported = subprocess.run(
            [sys.executable, str(TOOLS / "run_offline.py"),
             "--manifest", str(import_path), "--root", str(self.root)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertEqual(imported.stdout, "sibling-import\n")
        import_path.unlink()
        helper_path.unlink()
        shutil.rmtree(self.root / "tools/__pycache__", ignore_errors=True)

        reducer_path = self.root / "tools/reducer.py"
        reducer_path.write_text(
            "import socket\nsocket.create_connection(('example.com', 443))\n"
        )
        blocked_pack = self.make_pack(source)
        blocked_path = self.root / "blocked-pack.json"
        write_canonical(blocked_path, blocked_pack)
        blocked = subprocess.run(
            [sys.executable, str(TOOLS / "run_offline.py"),
             "--manifest", str(blocked_path), "--root", str(self.root)],
            capture_output=True, text=True, check=False,
        )
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("network access is disabled", blocked.stderr)


class ReleaseVerificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_release_artifacts(self) -> list[dict[str, object]]:
        base_generation = "2030-01-01T00:00:00Z"
        cell_generation = "2030-01-01T00:01:00Z"
        base_meta = f'{{"generated_at":"{base_generation}","snapshot_id":"fixture-snapshot"}}'
        cell_meta = (
            f'{{"base_generated_at":"{base_generation}","base_snapshot_id":"fixture-snapshot",'
            f'"generated_at":"{cell_generation}"}}'
        )
        contents: dict[str, bytes] = {
            "brain/data/nodes.jsonl": f'{{"_meta":{base_meta}}}\n{{"id":"Q1","label":"One","type":"concept"}}\n'.encode(),
            "brain/data/edges.jsonl": f'{{"_meta":{base_meta}}}\n{{"dst":"Q2","kind":"links","src":"Q1"}}\n'.encode(),
            "brain/data/edges_links.jsonl": f'{{"_meta":{base_meta}}}\n'.encode(),
            "brain/data/cells.jsonl": f'{{"_meta":{cell_meta}}}\n{{"anchor":"Q1","id":"cell:Q1","label":"One","organs":[],"supercells":[],"xy":[0.0,0.0]}}\n'.encode(),
            "brain/data/synapses.jsonl": f'{{"_meta":{cell_meta}}}\n'.encode(),
            "brain/data/frontier.jsonl": b'{"_meta":{"generated_at":"2030-01-01T00:01:00Z"}}\n',
            "brain/data/frontier_graph.json": b'{"_meta":{"generated_at":"2030-01-01T00:01:00Z"},"cells":[]}',
            "brain/data/community_edges.jsonl": b'',
            "catalog/data/source_registry.json": b'{"brain_sources":{},"crossref_sources":{},"edge_sources":{},"frontier_sources":{},"layers":{},"literature_sources":{},"node_sources":{},"our_data_license":"CC0-1.0","spine":{"key":"wikidata","name":"Wikidata"}}',
            "site/assets/brain/sources.json": b'{"layers":{},"our_data_license":"CC0-1.0","sources":[{"group":"spine","homepage":"","key":"wikidata","kind":"","layer":"","name":"Wikidata","note":"","our_provenance":"","target_license":"","wikidata_property":""}]}',
            "site/assets/brain/xref_index.json": b'{}',
            "site/assets/brain/cells/aliases.json": b'{"_meta":{"generated_at":"2030-01-01T00:01:00Z"},"decls":{},"organs":{},"slugs":{}}',
            "site/assets/brain/cells/labels.json": b'[{"id":"cell:Q1","label":"One"}]',
            "site/assets/brain/cells/supercells.json": b'{"_meta":{"counts":{"frontier_areas":0,"frontier_cells":0,"frontier_homeless":0,"frontier_unclaimed":0,"supercells":0,"synapse_rows":0,"with_cells":0},"generated_at":"2030-01-01T00:01:00Z"},"roots":[],"supercells":{}}',
            "site/assets/brain/cells/explorer.json": b'{"_meta":{"counts":{"edges":0,"nodes":1,"supercell_edges_on_supercells_json":0},"format":"edges are [node_index, node_index, weight] into `nodes`","generated_at":"2030-01-01T00:01:00Z","schema":"brain/SCHEMA.md#v3","truncated":false},"edges":[],"nodes":[{"id":"cell:Q1","label":"One","xy":[0.0,0.0]}]}',
            "site/assets/brain/cells/frontier_graph.json": b'{"_meta":{"generated_at":"2030-01-01T00:01:00Z"},"cells":[]}',
            "site/assets/brain/cells/ce.json": b'{"cell:Q1":{"cell":{"anchor":"Q1","id":"cell:Q1","label":"One","supercells":[],"xy":[0.0,0.0]},"counts":{"organs":0,"syn":0},"organs":[],"syn":[]}}',
            "site/assets/brain/cells/traces/ce.json": b'{}',
            "site/out/brain.html": b"<!doctype html><title>Brain</title>",
        }
        cell_manifest = {
            "_meta": {
                "generated_at": cell_generation,
                "counts": {"cells": 1, "shards": 1},
            },
            "roots": [],
            "scheme": {"kind": "prefix", "min_len": 2, "max_len": 7, "max_bytes": 150000, "pad": "_"},
            "shards": {"ce": 1},
            "traces": {
                "scheme": {"kind": "prefix", "min_len": 2, "max_len": 2, "max_bytes": 150000, "pad": "_"},
                "files": {"ce": 0},
            },
        }
        contents["site/assets/brain/cells/manifest.json"] = contracts.canonical_json_bytes(cell_manifest)
        for relative, data in contents.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        sqlite_path = self.root / "brain/data/brain.sqlite3"
        store.write_sqlite_from_jsonl(sqlite_path, self.root / "brain/data")
        contents["brain/data/brain.sqlite3"] = sqlite_path.read_bytes()

        artifacts = []
        for index, relative in enumerate(sorted(contents)):
            path = self.root / relative
            if relative.endswith(".jsonl"):
                logical_format = "jsonl-rowset"
                logical_root = contracts.logical_jsonl_root(path)
                media_type = "application/x-ndjson"
            elif relative.endswith(".json"):
                logical_format = "json"
                logical_root = contracts.logical_json_root(path)
                media_type = "application/json"
            else:
                logical_format = "opaque"
                logical_root = None
                media_type = "text/html" if relative.endswith(".html") else "application/vnd.sqlite3"
            artifacts.append({
                "logical_name": f"artifact-{index:02d}",
                **file_ref(self.root, relative, media_type),
                "logical_format": logical_format,
                "logical_root": logical_root,
            })
        return artifacts

    def make_release(self) -> tuple[dict[str, object], Path]:
        artifacts = self.write_release_artifacts()
        release: dict[str, object] = {
            "schema": contracts.RELEASE_SCHEMA,
            "profile": contracts.RELEASE_PROFILE,
            "release_id": ZERO_HASH,
            "authority": {"git_commit": GIT_COMMIT, "semantic_state_root": ZERO_HASH},
            "source_set_root": "sha256:" + "1" * 64,
            "semantic_epoch": "brain-v3",
            "reducer": {
                "schedule": "brain-v3-current",
                "version": "1",
                "git_commit": GIT_COMMIT,
                "configuration_sha256": "2" * 64,
                "environment_sha256": "3" * 64,
            },
            "artifacts": artifacts,
            "attestations": [],
            "compatible_overlay_generation_ids": ["overlay-fixture"],
            "created_at": "2030-01-01T00:02:00Z",
        }
        release["release_id"] = contracts.release_identity(release)

        build = {
            "schema": contracts.BUILD_ATTESTATION_SCHEMA,
            "attestation_id": ZERO_HASH,
            "release_id": release["release_id"],
            "builder": {
                "name": "fixture-builder",
                "version": "1",
                "git_commit": GIT_COMMIT,
                "configuration_sha256": "2" * 64,
                "environment_sha256": "3" * 64,
                "network": "disabled",
            },
            "input_roots": {
                "authority": ZERO_HASH,
                "source_set": "sha256:" + "1" * 64,
                "prior_state": None,
            },
            "output_root": ZERO_HASH,
            "artifacts": [
                {key: artifact[key] for key in ("logical_name", "sha256", "bytes", "logical_root")}
                for artifact in artifacts
            ],
            "metrics": {"artifact_count": len(artifacts)},
            "recorded_at": "2030-01-01T00:02:00Z",
        }
        build["artifacts"].sort(key=lambda item: item["logical_name"])
        build["attestation_id"] = contracts.attestation_identity(build)
        build_path = "attestations/build.json"
        write_canonical(self.root / build_path, build)

        validation = {
            "schema": contracts.VALIDATION_ATTESTATION_SCHEMA,
            "attestation_id": ZERO_HASH,
            "release_id": release["release_id"],
            "validator": {
                "name": "fixture-validator",
                "version": "1",
                "git_commit": GIT_COMMIT,
                "environment_sha256": "3" * 64,
                "network": "disabled",
            },
            "checks": [
                {"name": "artifact-closure", "status": "pass"},
                {"name": "digests", "status": "pass"},
            ],
            "result": "pass",
            "recorded_at": "2030-01-01T00:03:00Z",
        }
        validation["attestation_id"] = contracts.attestation_identity(validation)
        validation_path = "attestations/validation.json"
        write_canonical(self.root / validation_path, validation)

        build_digest, build_size = contracts.digest_file(self.root / build_path)
        validation_digest, validation_size = contracts.digest_file(self.root / validation_path)
        release["attestations"] = [
            {"kind": "build", "path": build_path, "sha256": build_digest, "bytes": build_size},
            {"kind": "validation", "path": validation_path, "sha256": validation_digest, "bytes": validation_size},
        ]
        release["attestations"].sort(key=lambda item: item["path"])
        manifest_path = self.root / "release.json"
        write_canonical(manifest_path, release)
        return release, manifest_path

    def test_verifies_release_cli_and_timestamp_independent_identity(self) -> None:
        release, manifest_path = self.make_release()
        result = contracts.verify_release_files(contracts.validate_release_manifest(release), self.root)
        self.assertEqual(result, {"artifacts": 21, "attestations": 2})

        changed = copy.deepcopy(release)
        changed["created_at"] = "2040-01-01T00:00:00Z"
        changed["attestations"] = list(reversed(changed["attestations"]))
        self.assertEqual(contracts.release_identity(release), contracts.release_identity(changed))

        process = subprocess.run(
            [sys.executable, str(TOOLS / "verify_release.py"), "--manifest", str(manifest_path), "--root", str(self.root)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["artifacts"], 21)

    def test_rejects_missing_shard_wrong_digest_and_mixed_generation(self) -> None:
        release, _ = self.make_release()

        missing_shard = copy.deepcopy(release)
        missing_shard["artifacts"] = [
            artifact for artifact in missing_shard["artifacts"]
            if artifact["path"] != "site/assets/brain/cells/traces/ce.json"
        ]
        missing_shard["release_id"] = contracts.release_identity(missing_shard)
        with self.assertRaisesRegex(contracts.VerificationError, "static cell manifest closure"):
            contracts.verify_release_files(contracts.validate_release_manifest(missing_shard), self.root)

        stale = self.root / "site/assets/brain/cells/stale.json"
        stale.write_text("{}")
        with self.assertRaisesRegex(contracts.VerificationError, "unlisted files"):
            contracts.verify_release_files(contracts.validate_release_manifest(release), self.root)
        stale.unlink()

        corrupt = copy.deepcopy(release)
        corrupt["artifacts"][0]["sha256"] = ZERO_DIGEST
        corrupt["release_id"] = contracts.release_identity(corrupt)
        with self.assertRaisesRegex(contracts.VerificationError, "sha256"):
            contracts.verify_release_files(contracts.validate_release_manifest(corrupt), self.root)

        frontier = self.root / "brain/data/frontier.jsonl"
        frontier.write_text('{"_meta":{"generated_at":"2031-01-01T00:00:00Z"}}\n')
        mixed = copy.deepcopy(release)
        for artifact in mixed["artifacts"]:
            if artifact["path"] == "brain/data/frontier.jsonl":
                digest, size = contracts.digest_file(frontier)
                artifact["sha256"], artifact["bytes"] = digest, size
                artifact["logical_root"] = contracts.logical_jsonl_root(frontier)
        mixed["release_id"] = contracts.release_identity(mixed)
        with self.assertRaisesRegex(contracts.VerificationError, "mixed or missing generated_at"):
            contracts.verify_release_files(contracts.validate_release_manifest(mixed), self.root)

    def test_rejects_shard_count_and_stale_static_generation(self) -> None:
        release, _ = self.make_release()
        shard_path = self.root / "site/assets/brain/cells/ce.json"
        shard_path.write_text("{}")
        changed = copy.deepcopy(release)
        for artifact in changed["artifacts"]:
            if artifact["path"] == "site/assets/brain/cells/ce.json":
                artifact["sha256"], artifact["bytes"] = contracts.digest_file(shard_path)
                artifact["logical_root"] = contracts.logical_json_root(shard_path)
        changed["release_id"] = contracts.release_identity(changed)
        with self.assertRaisesRegex(contracts.VerificationError, "manifest declares 1 entries"):
            contracts.verify_release_files(contracts.validate_release_manifest(changed), self.root)

        release, _ = self.make_release()
        stale_shard = self.root / "site/assets/brain/cells/cell_stale.json"
        stale_shard.write_text("{}")
        stale_declared = copy.deepcopy(release)
        stale_digest, stale_bytes = contracts.digest_file(stale_shard)
        stale_declared["artifacts"].append({
            "logical_name": "static-cell-stale",
            "path": "site/assets/brain/cells/cell_stale.json",
            "media_type": "application/json",
            "sha256": stale_digest,
            "bytes": stale_bytes,
            "logical_format": "json",
            "logical_root": contracts.logical_json_root(stale_shard),
        })
        stale_declared["artifacts"].sort(key=lambda item: item["path"])
        stale_declared["release_id"] = contracts.release_identity(stale_declared)
        with self.assertRaisesRegex(contracts.VerificationError, "contains stale files"):
            contracts.verify_release_files(
                contracts.validate_release_manifest(stale_declared), self.root
            )
        stale_shard.unlink()

        release, _ = self.make_release()
        aliases_path = self.root / "site/assets/brain/cells/aliases.json"
        aliases_path.write_text('{"_meta":{"generated_at":"2029-01-01T00:00:00Z"},"decls":{},"organs":{},"slugs":{}}')
        stale = copy.deepcopy(release)
        for artifact in stale["artifacts"]:
            if artifact["path"] == "site/assets/brain/cells/aliases.json":
                artifact["sha256"], artifact["bytes"] = contracts.digest_file(aliases_path)
                artifact["logical_root"] = contracts.logical_json_root(aliases_path)
        stale["release_id"] = contracts.release_identity(stale)
        with self.assertRaisesRegex(contracts.VerificationError, "mixed or missing generated_at"):
            contracts.verify_release_files(contracts.validate_release_manifest(stale), self.root)

    def test_rejects_reducer_attestation_mismatch_and_stale_sqlite(self) -> None:
        release, _ = self.make_release()
        build_path = self.root / "attestations/build.json"
        build, _ = contracts.load_canonical_json(build_path)
        build["builder"]["git_commit"] = "b" * 40
        build["attestation_id"] = contracts.attestation_identity(build)
        write_canonical(build_path, build)
        changed = copy.deepcopy(release)
        for ref in changed["attestations"]:
            if ref["kind"] == "build":
                ref["sha256"], ref["bytes"] = contracts.digest_file(build_path)
        with self.assertRaisesRegex(contracts.VerificationError, "builder.git_commit"):
            contracts.verify_release_files(contracts.validate_release_manifest(changed), self.root)

        release, _ = self.make_release()
        sqlite_path = self.root / "brain/data/brain.sqlite3"
        connection = sqlite3.connect(sqlite_path)
        connection.execute("UPDATE artifacts SET source_digest = ? WHERE name = 'nodes'", (ZERO_DIGEST,))
        connection.commit()
        connection.close()
        stale = copy.deepcopy(release)
        for artifact in stale["artifacts"]:
            if artifact["path"] == "brain/data/brain.sqlite3":
                artifact["sha256"], artifact["bytes"] = contracts.digest_file(sqlite_path)
        stale["release_id"] = contracts.release_identity(stale)
        with self.assertRaisesRegex(contracts.VerificationError, "SQLite is stale"):
            contracts.verify_release_files(contracts.validate_release_manifest(stale), self.root)

        release, _ = self.make_release()
        sqlite_path = self.root / "brain/data/brain.sqlite3"
        connection = sqlite3.connect(sqlite_path)
        connection.execute("UPDATE nodes SET payload_json = ?", ('{"id":"Q999"}',))
        connection.commit()
        connection.close()
        corrupt_rows = copy.deepcopy(release)
        for artifact in corrupt_rows["artifacts"]:
            if artifact["path"] == "brain/data/brain.sqlite3":
                artifact["sha256"], artifact["bytes"] = contracts.digest_file(sqlite_path)
        corrupt_rows["release_id"] = contracts.release_identity(corrupt_rows)
        with self.assertRaisesRegex(contracts.VerificationError, "payload rows do not match"):
            contracts.verify_release_files(
                contracts.validate_release_manifest(corrupt_rows), self.root
            )

        release, _ = self.make_release()
        sqlite_path = self.root / "brain/data/brain.sqlite3"
        connection = sqlite3.connect(sqlite_path)
        connection.execute("UPDATE nodes SET id = 'Q999'")
        connection.commit()
        connection.close()
        corrupt_index = copy.deepcopy(release)
        for artifact in corrupt_index["artifacts"]:
            if artifact["path"] == "brain/data/brain.sqlite3":
                artifact["sha256"], artifact["bytes"] = contracts.digest_file(sqlite_path)
        corrupt_index["release_id"] = contracts.release_identity(corrupt_index)
        with self.assertRaisesRegex(contracts.VerificationError, "indexed columns disagree"):
            contracts.verify_release_files(
                contracts.validate_release_manifest(corrupt_index), self.root
            )

    def test_rejects_unknown_release_version_and_failed_attestation(self) -> None:
        release, _ = self.make_release()
        bad_version = copy.deepcopy(release)
        bad_version["schema"] = "wikilean.release/v2"
        with self.assertRaisesRegex(contracts.VerificationError, "unknown schema/version"):
            contracts.validate_release_manifest(bad_version)

        validation_path = self.root / "attestations/validation.json"
        validation, _ = contracts.load_canonical_json(validation_path)
        validation["checks"][0]["status"] = "fail"
        validation["result"] = "pass"
        validation["attestation_id"] = contracts.attestation_identity(validation)
        with self.assertRaisesRegex(contracts.VerificationError, "only passing checks"):
            contracts.validate_validation_attestation(validation)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    raise SystemExit(0 if result.wasSuccessful() else 1)
