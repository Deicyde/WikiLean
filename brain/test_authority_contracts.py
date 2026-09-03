#!/usr/bin/env python3
"""Hermetic tests for WikiLean authority manifests and verification commands."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import unicodedata
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
TOOLS = HERE / "tools"
sys.path.insert(0, str(TOOLS))

import authority_contracts as contracts  # noqa: E402
import build_context  # noqa: E402
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

    def test_sqlite_payload_root_sorts_canonical_numeric_spellings(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "numbers.jsonl"
            path.write_text(
                '{"_meta":{}}\n{"n":0.0}\n{"n":1e-7}\n',
                encoding="utf-8",
            )
            connection = sqlite3.connect(":memory:")
            try:
                connection.execute("CREATE TABLE payloads (payload_json TEXT NOT NULL)")
                connection.executemany(
                    "INSERT INTO payloads VALUES (?)",
                    [('{"n":0.0}',), ('{"n":1e-7}',)],
                )
                self.assertEqual(
                    contracts._sqlite_payload_root(
                        connection,
                        "SELECT payload_json FROM payloads ORDER BY payload_json",
                    ),
                    contracts.logical_jsonl_root(path),
                )
            finally:
                connection.close()

    def test_routed_shard_checks_distinct_prefix_lengths_longest_first(self) -> None:
        keys = {"ab", "abc", "abcd"}
        with mock.patch.object(
            contracts,
            "_normalized_prefix",
            wraps=contracts._normalized_prefix,
        ) as normalized:
            self.assertEqual(
                contracts._routed_shard("ABCD-tail", keys, "_", (4, 3, 2)),
                "abcd",
            )
        normalized.assert_called_once_with("ABCD-tail", 4, "_")
        self.assertEqual(
            contracts._routed_shard("ABCD-tail", keys, "_", (3, 2)),
            "abc",
        )
        self.assertIsNone(
            contracts._routed_shard("xy", keys, "_", (4, 3, 2))
        )


class CompatibilityRootAndSelectorTest(unittest.TestCase):
    def test_compatibility_roots_are_domain_separated_and_order_stable(self) -> None:
        roots = {path: "sha256:" + format(index, "064x") for index, path in enumerate(contracts.COMPATIBILITY_SEMANTIC_PATHS, 1)}
        semantic = contracts.compatibility_semantic_state_root("brain-v3-current", "a" * 64, roots)
        self.assertTrue(semantic.startswith("sha256:"))
        self.assertNotEqual(semantic, contracts.domain_hash("unrelated", roots))

        inputs = [
            {"declaration": "path", "path": "b.json", "present": False},
            {"declaration": "path_pattern", "path": "a.json", "present": True, "sha256": "b" * 64, "bytes": 2},
        ]
        self.assertEqual(
            contracts.legacy_declared_input_root("c" * 64, inputs),
            contracts.legacy_declared_input_root("c" * 64, list(reversed(inputs))),
        )
        changed = copy.deepcopy(inputs)
        changed[1]["bytes"] = 3
        self.assertNotEqual(
            contracts.legacy_declared_input_root("c" * 64, inputs),
            contracts.legacy_declared_input_root("c" * 64, changed),
        )

    def test_release_selector_validation(self) -> None:
        current = "a" * 64
        previous = "b" * 64
        selector = {
            "schema": contracts.RELEASE_SELECTOR_SCHEMA,
            "release_id": f"sha256:{current}",
            "release": current,
            "manifest": f"/assets/brain/releases/{current}/release.json",
            "previous_release_id": f"sha256:{previous}",
            "previous_release": previous,
            "previous_manifest": f"/assets/brain/releases/{previous}/release.json",
            "audited_at": "2030-01-01T00:00:00Z",
        }
        self.assertIs(contracts.validate_release_selector(selector), selector)

        current_only = {key: selector[key] for key in ("schema", "release_id", "release", "manifest")}
        self.assertIs(contracts.validate_release_selector(current_only), current_only)

        partial = copy.deepcopy(selector)
        del partial["previous_manifest"]
        with self.assertRaisesRegex(contracts.VerificationError, "present together"):
            contracts.validate_release_selector(partial)

        wrong_manifest = copy.deepcopy(selector)
        wrong_manifest["manifest"] = f"/assets/brain/releases/{previous}/release.json"
        with self.assertRaisesRegex(contracts.VerificationError, "expected"):
            contracts.validate_release_selector(wrong_manifest)

        same_previous = copy.deepcopy(selector)
        same_previous.update({
            "previous_release_id": selector["release_id"],
            "previous_release": selector["release"],
            "previous_manifest": selector["manifest"],
        })
        with self.assertRaisesRegex(contracts.VerificationError, "must differ"):
            contracts.validate_release_selector(same_previous)

    def test_release_selector_schema_matches_strict_validator_shape(self) -> None:
        schema = json.loads(
            (HERE / "authority/schemas/release-selector/v1.json").read_text(encoding="utf-8")
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            schema["required"],
            ["schema", "release_id", "release", "manifest"],
        )
        self.assertIn("audited_at", schema["properties"])
        self.assertIn("previous_release_id", schema["properties"])
        self.assertIn("previous_release", schema["properties"])
        self.assertIn("previous_manifest", schema["properties"])
        self.assertNotIn("selector_id", schema["properties"])
        self.assertNotIn("previous", schema["properties"])
        self.assertNotIn("updated_at", schema["properties"])

        release = "a" * 64
        obsolete = {
            "schema": contracts.RELEASE_SELECTOR_SCHEMA,
            "release_id": f"sha256:{release}",
            "release": release,
            "manifest": f"/assets/brain/releases/{release}/release.json",
            "previous": {
                "release_id": f"sha256:{'b' * 64}",
                "release": "b" * 64,
                "manifest": f"/assets/brain/releases/{'b' * 64}/release.json",
            },
        }
        with self.assertRaisesRegex(contracts.VerificationError, "unknown members"):
            contracts.validate_release_selector(obsolete)


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
        bad_version["schema"] = "wikilean.offline-pack/v99"
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


class V2SourceSetVerificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_v2_schema_documents_match_executable_contracts(self) -> None:
        schema_root = HERE / "authority/schemas"
        inventory_schema = json.loads(
            (schema_root / "reducer-input-inventory/v2.json").read_text()
        )
        source_schema = json.loads(
            (schema_root / "source-manifest/v2.json").read_text()
        )
        pack_schema = json.loads((schema_root / "offline-pack/v2.json").read_text())
        build_schema = json.loads(
            (schema_root / "attestation/build-v2.json").read_text()
        )
        context_schema = json.loads(
            (schema_root / "build-context/v1.json").read_text()
        )
        reducer_config_schema = json.loads(
            (schema_root / "reducer-config/v1.json").read_text()
        )
        self.assertEqual(
            inventory_schema["properties"]["schema"]["const"],
            contracts.REDUCER_INPUT_INVENTORY_SCHEMA_V2,
        )
        self.assertEqual(source_schema["properties"]["schema"]["const"], contracts.SOURCE_SCHEMA_V2)
        self.assertEqual(source_schema["properties"]["objects"]["minItems"], 1)
        self.assertIn("roles", source_schema["$defs"]["sourceObject"]["required"])
        self.assertEqual(pack_schema["properties"]["schema"]["const"], contracts.PACK_SCHEMA_V2)
        self.assertEqual(
            build_schema["properties"]["schema"]["const"],
            contracts.BUILD_ATTESTATION_SCHEMA_V2,
        )
        self.assertEqual(
            build_schema["properties"]["build_kind"]["const"],
            "full-offline-replay",
        )
        self.assertEqual(
            inventory_schema["properties"]["scope"]["items"]["$ref"],
            "#/$defs/literalRelativePath",
        )
        self.assertEqual(
            inventory_schema["properties"]["inputs"]["items"]["properties"]
            ["path_pattern"]["$ref"],
            "#/$defs/pathPattern",
        )
        self.assertIn(
            "outputs",
            inventory_schema["properties"]["stages"]["items"]["required"],
        )
        self.assertEqual(
            inventory_schema["$defs"]["stageOutput"]["properties"]["kind"]["enum"],
            ["file", "tree"],
        )
        self.assertEqual(
            inventory_schema["properties"]["forbidden_ambient"]["items"]
            ["properties"]["consumers"]["items"]["oneOf"][1]["$ref"],
            "#/$defs/literalRelativePath",
        )
        self.assertEqual(
            pack_schema["$defs"]["bindingMember"]["properties"]["path"]["$ref"],
            "#/$defs/literalRelativePath",
        )
        self.assertIn("git_commit", pack_schema["properties"]["reducer"]["required"])
        self.assertEqual(
            pack_schema["properties"]["reducer"]["properties"]["git_commit"]["$ref"],
            "#/$defs/gitCommit",
        )
        self.assertEqual(
            pack_schema["properties"]["configuration"]["$ref"],
            "#/$defs/jsonFileRef",
        )
        self.assertEqual(
            pack_schema["$defs"]["jsonFileRef"]["properties"]["media_type"]["const"],
            "application/json",
        )
        self.assertEqual(len(source_schema["properties"]["pin"]["allOf"]), 3)
        self.assertEqual(len(pack_schema["$defs"]["inputBinding"]["allOf"]), 2)
        self.assertEqual(
            context_schema["properties"]["schema"]["const"],
            build_context.BUILD_CONTEXT_SCHEMA,
        )
        self.assertEqual(
            set(context_schema["required"]),
            {
                "schema",
                "generation_id",
                "replay",
                "roots",
                "bindings",
                "stages",
                "configuration",
            },
        )
        self.assertEqual(
            context_schema["properties"]["configuration"]["$ref"],
            "../reducer-config/v1.json",
        )
        self.assertIn("outputs", context_schema["$defs"]["stage"]["required"])
        self.assertEqual(
            reducer_config_schema["properties"]["schema"]["const"],
            build_context.REDUCER_CONFIGURATION_SCHEMA,
        )
        self.assertEqual(
            set(reducer_config_schema["required"]),
            {"schema", "external_node_cap", "cell_attach_kinds", "layout"},
        )
        self.assertEqual(
            build_context.GENERATION_DOMAIN,
            "wikilean.brain-generation.v1",
        )
        absolute_path = re.compile(context_schema["$defs"]["absolutePath"]["pattern"])
        self.assertIsNotNone(absolute_path.fullmatch("/safe/root"))
        self.assertIsNone(absolute_path.fullmatch("/../escape"))
        self.assertIsNone(absolute_path.fullmatch("/./escape"))
        inventory, _ = contracts.load_canonical_json(
            HERE / "authority/reducer-inputs-v2.json"
        )
        contracts.validate_reducer_input_inventory(inventory)
        self.assertTrue(contracts._matches_relative_pattern("x_pages.jsonl", "*_pages.jsonl"))
        self.assertFalse(
            contracts._matches_relative_pattern("nested/x_pages.jsonl", "*_pages.jsonl")
        )
        self.assertTrue(
            contracts._matches_relative_pattern("Mathlib/Foo.lean", "Mathlib/**/*.lean")
        )
        self.assertTrue(
            contracts._matches_relative_pattern("Mathlib/A/Foo.lean", "Mathlib/**/*.lean")
        )
        self.assertFalse(
            contracts._matches_relative_pattern(
                "prefix/Mathlib/Foo.lean", "Mathlib/**/*.lean"
            )
        )
        self.assertTrue(
            contracts._matches_relative_pattern(
                "/".join(["segment"] * 1_500 + ["leaf"]), "**/leaf"
            )
        )

    def test_v2_inventory_rejects_invalid_stage_output_ownership(self) -> None:
        inventory, _ = self.make_inventory()

        def reject(mutator, message: str) -> None:
            bad = copy.deepcopy(inventory)
            mutator(bad)
            bad["inventory_id"] = contracts.reducer_input_inventory_identity(bad)
            with self.assertRaisesRegex(contracts.VerificationError, message):
                contracts.validate_reducer_input_inventory(bad)

        reject(
            lambda value: value["stages"][0].update(outputs=[]),
            "array must not be empty",
        )
        reject(
            lambda value: value["stages"][0].update(
                outputs=[
                    {"path": "z.json", "kind": "file"},
                    {"path": "a.json", "kind": "file"},
                ]
            ),
            "sorted by path",
        )
        reject(
            lambda value: value["stages"][1].update(
                outputs=[{"path": "intermediate/prepared.json", "kind": "file"}]
            ),
            "already owned",
        )

        def overlap(value: dict[str, object]) -> None:
            value["stages"][0]["outputs"] = [{"path": "artifacts", "kind": "tree"}]
            value["stages"][1]["outputs"] = [
                {"path": "artifacts/result.json", "kind": "file"}
            ]

        reject(overlap, "output ownership overlaps")
        reject(
            lambda value: value["stages"][0].update(
                outputs=[{"path": "artifacts/*.json", "kind": "file"}]
            ),
            "glob metacharacters",
        )
        reject(
            lambda value: value["stages"][0]["outputs"][0].update(kind="directory"),
            "expected file or tree",
        )
        reject(
            lambda value: value["stages"][0].update(needs=["replay"]),
            "dependencies must name earlier stages",
        )

    def write_object(self, data: bytes, media_type: str) -> dict[str, object]:
        digest = hashlib.sha256(data).hexdigest()
        relative = f"objects/sha256/{digest}"
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            self.assertEqual(path.read_bytes(), data)
        else:
            path.write_bytes(data)
        return {
            "path": relative,
            "sha256": digest,
            "bytes": len(data),
            "media_type": media_type,
        }

    def make_inventory(self) -> tuple[dict[str, object], dict[str, object]]:
        for relative, data in {
            "reducer/brain/helper.py": b"VALUE = 1\n",
            "reducer/brain/replay.py": b"print('not wired')\n",
            "config/reducer.json": b'{"cap":8}',
            "environment/python.json": b'{"python":"3.12"}',
            "schemas/input.json": b'{"type":"object"}',
        }.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        inventory: dict[str, object] = {
            "schema": contracts.REDUCER_INPUT_INVENTORY_SCHEMA_V2,
            "inventory_id": ZERO_HASH,
            "boundary": "post-acquisition-fold",
            "roots": [
                {"id": "external", "kind": "external_tree"},
                {"id": "repo", "kind": "repository"},
            ],
            "scope": ["brain/helper.py", "brain/replay.py"],
            "stages": [
                {
                    "id": "prepare",
                    "program": "brain/helper.py",
                    "argv": [],
                    "needs": [],
                    "outputs": [{"path": "intermediate/prepared.json", "kind": "file"}],
                },
                {
                    "id": "replay",
                    "program": "brain/replay.py",
                    "argv": [],
                    "needs": ["prepare"],
                    "outputs": [{"path": "artifacts", "kind": "tree"}],
                },
            ],
            "inputs": [
                {
                    "id": "curated",
                    "root": "repo",
                    "path": "catalog/curated.json",
                    "cardinality": "one",
                    "requirement": "required",
                    "class": "curated_git_input",
                    "consumers": ["brain/replay.py"],
                    "purpose": "reviewed fixture",
                },
                {
                    "id": "optional_external",
                    "root": "external",
                    "path_pattern": "*_pages.jsonl",
                    "cardinality": "many",
                    "requirement": "optional",
                    "class": "immutable_source_object",
                    "consumers": ["brain/replay.py"],
                    "purpose": "explicitly absent fixture set",
                },
                {
                    "id": "source",
                    "root": "external",
                    "path": "input.json",
                    "cardinality": "one",
                    "requirement": "required",
                    "class": "immutable_source_object",
                    "consumers": ["brain/helper.py", "brain/replay.py"],
                    "purpose": "normalized fixture input",
                },
            ],
            "forbidden_ambient": [
                {
                    "name": "network access",
                    "consumers": ["*"],
                    "replacement": "sealed source objects",
                }
            ],
        }
        inventory["inventory_id"] = contracts.reducer_input_inventory_identity(inventory)
        write_canonical(self.root / "inventory.json", inventory)
        return inventory, {
            **file_ref(self.root, "inventory.json", "application/json"),
            "inventory_id": inventory["inventory_id"],
        }

    def make_source_manifest(
        self,
        *,
        source: str,
        source_kind: str,
        normalized: bytes,
        identity_normalization: bool = False,
    ) -> tuple[dict[str, object], dict[str, object]]:
        raw = normalized if identity_normalization else b"raw:" + normalized
        raw_ref = self.write_object(raw, "application/json")
        normalized_ref = self.write_object(normalized, "application/json")
        if identity_normalization:
            objects = [{
                "name": "identity",
                "roles": ["normalized", "raw"],
                "redistribution": "allowed",
                **normalized_ref,
            }]
            inputs = outputs = ["identity"]
        else:
            objects = [
                {
                    "name": "normalized",
                    "roles": ["normalized"],
                    "redistribution": "allowed",
                    **normalized_ref,
                },
                {
                    "name": "raw",
                    "roles": ["raw"],
                    "redistribution": "allowed",
                    **raw_ref,
                },
            ]
            inputs, outputs = ["raw"], ["normalized"]
        pin = (
            {"type": "git_commit", "value": GIT_COMMIT, "tree": "b" * 40}
            if source_kind == "curated_git_tree"
            else {"type": "content_sha256", "value": raw_ref["sha256"]}
        )
        manifest: dict[str, object] = {
            "schema": contracts.SOURCE_SCHEMA_V2,
            "source_manifest_id": ZERO_HASH,
            "source": source,
            "source_kind": source_kind,
            "pin": pin,
            "objects": objects,
            "license": {"expression": "CC0-1.0", "redistribution": "allowed"},
            "acquisition": {"name": "fixture-fetch", "version": "1", "sha256": ZERO_DIGEST},
            "normalization": {
                "schema": "fixture/v1",
                "tool": {"name": "fixture-normalize", "version": "1", "sha256": ZERO_DIGEST},
                "inputs": inputs,
                "outputs": outputs,
            },
            "audit": {"acquired_at": "2030-01-01T00:00:00Z"},
        }
        manifest["source_manifest_id"] = contracts.source_manifest_identity(manifest)
        relative = f"manifests/{source}.json"
        write_canonical(self.root / relative, manifest)
        return manifest, {
            **file_ref(self.root, relative, "application/json"),
            "source_manifest_id": manifest["source_manifest_id"],
        }

    def make_pack(self) -> tuple[dict[str, object], Path]:
        inventory, inventory_ref = self.make_inventory()
        curated, curated_ref = self.make_source_manifest(
            source="curated-fixture",
            source_kind="curated_git_tree",
            normalized=b'{"curated":true}',
            identity_normalization=True,
        )
        source, source_ref = self.make_source_manifest(
            source="external-fixture",
            source_kind="acquired_dataset",
            normalized=b'{"rows":[1,2]}',
        )
        manifests = sorted(
            [(curated, curated_ref), (source, source_ref)],
            key=lambda item: item[0]["source_manifest_id"],
        )
        packed_objects: dict[str, dict[str, object]] = {}
        for manifest, _ in manifests:
            for item in manifest["objects"]:
                packed_objects[item["path"]] = {
                    key: item[key] for key in ("path", "sha256", "bytes", "media_type")
                }
        curated_object = curated["objects"][0]["name"]
        source_object = next(
            item["name"] for item in source["objects"] if "normalized" in item["roles"]
        )
        bindings = [
            {
                "input_id": "curated",
                "state": "present",
                "members": [{
                    "path": "catalog/curated.json",
                    "source_manifest_id": curated["source_manifest_id"],
                    "object": curated_object,
                }],
            },
            {"input_id": "optional_external", "state": "absent", "members": []},
            {
                "input_id": "source",
                "state": "present",
                "members": [{
                    "path": "input.json",
                    "source_manifest_id": source["source_manifest_id"],
                    "object": source_object,
                }],
            },
        ]
        reducer_files = [
            {"logical_path": "brain/helper.py", **file_ref(self.root, "reducer/brain/helper.py", "text/x-python")},
            {"logical_path": "brain/replay.py", **file_ref(self.root, "reducer/brain/replay.py", "text/x-python")},
        ]
        pack: dict[str, object] = {
            "schema": contracts.PACK_SCHEMA_V2,
            "offline_pack_id": ZERO_HASH,
            "source_set_root": contracts.source_set_root_v2(
                inventory["inventory_id"],
                [manifest["source_manifest_id"] for manifest, _ in manifests],
                bindings,
            ),
            "inventory": inventory_ref,
            "source_manifests": [ref for _, ref in manifests],
            "objects": [packed_objects[path] for path in sorted(packed_objects)],
            "input_bindings": bindings,
            "reducer": {
                "entrypoint": "brain/replay.py",
                "files": reducer_files,
                "git_commit": GIT_COMMIT,
            },
            "configuration": file_ref(self.root, "config/reducer.json", "application/json"),
            "environment": file_ref(self.root, "environment/python.json", "application/json"),
            "schemas": [file_ref(self.root, "schemas/input.json", "application/schema+json")],
            "audit": {"created_at": "2030-01-01T00:00:00Z"},
        }
        pack["offline_pack_id"] = contracts.offline_pack_identity(pack)
        path = self.root / "pack.json"
        write_canonical(path, pack)
        return pack, path

    def test_v2_verifies_inventory_sources_pack_and_identity_normalization(self) -> None:
        pack, pack_path = self.make_pack()
        result = contracts.verify_offline_pack_files(
            contracts.validate_offline_pack(pack), self.root, manifest_path=pack_path
        )
        self.assertEqual(result["source_manifests"], 2)
        self.assertEqual(result["input_bindings"], 3)
        curated_path = next(
            ref["path"] for ref in pack["source_manifests"]
            if json.loads((self.root / ref["path"]).read_text())["source"] == "curated-fixture"
        )
        curated = json.loads((self.root / curated_path).read_text())
        self.assertEqual(curated["objects"][0]["roles"], ["normalized", "raw"])

        changed = copy.deepcopy(pack)
        changed["audit"]["created_at"] = "2040-01-01T00:00:00Z"
        self.assertEqual(
            contracts.offline_pack_identity(pack),
            contracts.offline_pack_identity(changed),
        )
        self.assertEqual(
            contracts.source_set_root_v2(
                pack["inventory"]["inventory_id"],
                [ref["source_manifest_id"] for ref in reversed(pack["source_manifests"])],
                list(reversed(pack["input_bindings"])),
            ),
            pack["source_set_root"],
        )
        changed_bindings = copy.deepcopy(pack["input_bindings"])
        source_binding = next(
            item for item in changed_bindings if item["input_id"] == "source"
        )
        source_binding["members"][0]["object"] = "different-object"
        self.assertNotEqual(
            contracts.source_set_root_v2(
                pack["inventory"]["inventory_id"],
                [ref["source_manifest_id"] for ref in pack["source_manifests"]],
                changed_bindings,
            ),
            pack["source_set_root"],
        )
        self.assertNotEqual(
            contracts.source_set_root_v2(
                "sha256:" + "f" * 64,
                [ref["source_manifest_id"] for ref in pack["source_manifests"]],
                pack["input_bindings"],
            ),
            pack["source_set_root"],
        )

        process = subprocess.run(
            [sys.executable, str(TOOLS / "verify_source_set.py"), "--manifest", str(pack_path), "--root", str(self.root)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["offline_pack_id"], pack["offline_pack_id"])

    def test_v2_rejects_missing_required_wrong_role_and_bad_inventory_pattern(self) -> None:
        pack, pack_path = self.make_pack()
        required_absent = copy.deepcopy(pack)
        binding = next(item for item in required_absent["input_bindings"] if item["input_id"] == "source")
        binding.update({"state": "absent", "members": []})
        required_absent["source_set_root"] = contracts.source_set_root_v2(
            required_absent["inventory"]["inventory_id"],
            [ref["source_manifest_id"] for ref in required_absent["source_manifests"]],
            required_absent["input_bindings"],
        )
        required_absent["offline_pack_id"] = contracts.offline_pack_identity(required_absent)
        with self.assertRaisesRegex(contracts.VerificationError, "required inputs must be present"):
            contracts.verify_offline_pack_files(required_absent, self.root, manifest_path=pack_path)

        wrong_role = copy.deepcopy(pack)
        source_ref = next(
            ref for ref in wrong_role["source_manifests"]
            if json.loads((self.root / ref["path"]).read_text())["source"] == "external-fixture"
        )
        source_manifest = json.loads((self.root / source_ref["path"]).read_text())
        raw_name = next(item["name"] for item in source_manifest["objects"] if item["roles"] == ["raw"])
        binding = next(item for item in wrong_role["input_bindings"] if item["input_id"] == "source")
        binding["members"][0]["object"] = raw_name
        wrong_role["source_set_root"] = contracts.source_set_root_v2(
            wrong_role["inventory"]["inventory_id"],
            [ref["source_manifest_id"] for ref in wrong_role["source_manifests"]],
            wrong_role["input_bindings"],
        )
        wrong_role["offline_pack_id"] = contracts.offline_pack_identity(wrong_role)
        with self.assertRaisesRegex(contracts.VerificationError, "normalized source objects"):
            contracts.verify_offline_pack_files(wrong_role, self.root, manifest_path=pack_path)

        inventory, _ = contracts.load_canonical_json(self.root / "inventory.json")
        bad_pattern = copy.deepcopy(inventory)
        item = next(entry for entry in bad_pattern["inputs"] if entry["id"] == "optional_external")
        item["path_pattern"] = "*_{pages,links}.jsonl"
        bad_pattern["inventory_id"] = contracts.reducer_input_inventory_identity(bad_pattern)
        with self.assertRaisesRegex(contracts.VerificationError, "brace expansion"):
            contracts.validate_reducer_input_inventory(bad_pattern)

        wildcard_scope = copy.deepcopy(inventory)
        wildcard_scope["scope"][0] = "brain/*.py"
        wildcard_scope["inventory_id"] = contracts.reducer_input_inventory_identity(
            wildcard_scope
        )
        with self.assertRaisesRegex(contracts.VerificationError, "glob metacharacters"):
            contracts.validate_reducer_input_inventory(wildcard_scope)

        wildcard_member = copy.deepcopy(pack)
        binding = next(
            item for item in wildcard_member["input_bindings"]
            if item["input_id"] == "source"
        )
        binding["members"][0]["path"] = "*.json"
        with self.assertRaisesRegex(contracts.VerificationError, "glob metacharacters"):
            contracts.validate_offline_pack(wildcard_member)

        long_pin, _ = self.make_source_manifest(
            source="long-pin-fixture",
            source_kind="acquired_dataset",
            normalized=b'{}',
        )
        long_pin["pin"] = {"type": "dataset_revision", "value": "x" * 513}
        long_pin["source_manifest_id"] = contracts.source_manifest_identity(long_pin)
        with self.assertRaisesRegex(contracts.VerificationError, "at most 512"):
            contracts.validate_source_manifest(long_pin)

        wrong_media = copy.deepcopy(pack)
        wrong_media["source_manifests"][0]["media_type"] = "text/plain"
        wrong_media["offline_pack_id"] = contracts.offline_pack_identity(wrong_media)
        with self.assertRaisesRegex(contracts.VerificationError, "application/json"):
            contracts.validate_offline_pack(wrong_media)

        bad_reducer_commit = copy.deepcopy(pack)
        bad_reducer_commit["reducer"]["git_commit"] = "a" * 39
        with self.assertRaisesRegex(contracts.VerificationError, "full lowercase Git commit"):
            contracts.validate_offline_pack(bad_reducer_commit)

        configuration_array = copy.deepcopy(pack)
        configuration_array["configuration"] = [configuration_array["configuration"]]
        with self.assertRaisesRegex(
            contracts.VerificationError, r"\$\.configuration: expected an object"
        ):
            contracts.validate_offline_pack(configuration_array)

        wrong_config_media = copy.deepcopy(pack)
        wrong_config_media["configuration"]["media_type"] = "text/plain"
        with self.assertRaisesRegex(contracts.VerificationError, "application/json"):
            contracts.validate_offline_pack(wrong_config_media)

    def test_v2_rejects_logical_path_ancestry_collisions(self) -> None:
        pack, pack_path = self.make_pack()

        reducer_overlap = copy.deepcopy(pack)
        reducer_overlap["reducer"]["files"][0]["logical_path"] = "brain"
        reducer_overlap["offline_pack_id"] = contracts.offline_pack_identity(reducer_overlap)
        with self.assertRaisesRegex(contracts.VerificationError, "overlaps by ancestry"):
            contracts.validate_offline_pack(reducer_overlap)

        inventory, _ = contracts.load_canonical_json(self.root / "inventory.json")
        source_input = next(
            item for item in inventory["inputs"] if item["id"] == "source"
        )
        source_input["path"] = "input.json/child"
        inventory["inputs"].append(
            {
                "id": "source-parent",
                "root": "external",
                "path": "input.json",
                "cardinality": "one",
                "requirement": "required",
                "class": "immutable_source_object",
                "consumers": ["brain/replay.py"],
                "purpose": "invalid ancestor fixture input",
            }
        )
        inventory["inventory_id"] = contracts.reducer_input_inventory_identity(inventory)
        write_canonical(self.root / "inventory.json", inventory)
        pack["inventory"] = {
            **file_ref(self.root, "inventory.json", "application/json"),
            "inventory_id": inventory["inventory_id"],
        }
        source_binding = next(
            item for item in pack["input_bindings"] if item["input_id"] == "source"
        )
        source_binding["members"][0]["path"] = "input.json/child"
        pack["input_bindings"].append(
            {
                "input_id": "source-parent",
                "state": "present",
                "members": [
                    {
                        **source_binding["members"][0],
                        "path": "input.json",
                    }
                ],
            }
        )
        pack["source_set_root"] = contracts.source_set_root_v2(
            inventory["inventory_id"],
            [ref["source_manifest_id"] for ref in pack["source_manifests"]],
            pack["input_bindings"],
        )
        pack["offline_pack_id"] = contracts.offline_pack_identity(pack)
        contracts.validate_offline_pack(pack)
        with self.assertRaisesRegex(contracts.VerificationError, "overlaps by ancestry"):
            contracts.verify_offline_pack_files(pack, self.root, manifest_path=pack_path)

    def test_v2_rejects_incomplete_and_undeclared_physical_closure(self) -> None:
        pack, pack_path = self.make_pack()
        missing_object = copy.deepcopy(pack)
        missing_object["objects"].pop(0)
        missing_object["offline_pack_id"] = contracts.offline_pack_identity(
            missing_object
        )
        contracts.validate_offline_pack(missing_object)
        with self.assertRaisesRegex(
            contracts.VerificationError, "source object closure mismatch"
        ):
            contracts.verify_offline_pack_files(
                missing_object, self.root, manifest_path=pack_path
            )

        extra = self.root / "undeclared.txt"
        extra.write_text("not in the pack")
        with self.assertRaisesRegex(contracts.VerificationError, "undeclared files"):
            contracts.verify_offline_pack_files(
                contracts.validate_offline_pack(pack),
                self.root,
                manifest_path=pack_path,
            )

    def test_v2_build_attestation_requires_pack_and_offline_replay(self) -> None:
        attestation: dict[str, object] = {
            "schema": contracts.BUILD_ATTESTATION_SCHEMA_V2,
            "attestation_id": ZERO_HASH,
            "release_id": "sha256:" + "1" * 64,
            "build_kind": "full-offline-replay",
            "builder": {
                "name": "fixture-replay",
                "version": "1",
                "git_commit": GIT_COMMIT,
                "configuration_sha256": "2" * 64,
                "environment_sha256": "3" * 64,
                "network": "disabled",
            },
            "inputs": {
                "authority_root": "sha256:" + "4" * 64,
                "source_set_root": "sha256:" + "5" * 64,
                "offline_pack_id": "sha256:" + "6" * 64,
                "reducer_inventory_id": "sha256:" + "7" * 64,
                "prior_state_root": None,
            },
            "output_root": "sha256:" + "8" * 64,
            "artifacts": [
                {"logical_name": "nodes", "sha256": "9" * 64, "bytes": 4, "logical_root": "sha256:" + "a" * 64}
            ],
            "metrics": {"artifact_count": 1},
            "recorded_at": "2030-01-01T00:00:00Z",
        }
        attestation["attestation_id"] = contracts.attestation_identity(attestation)
        contracts.validate_build_attestation(attestation)
        changed = copy.deepcopy(attestation)
        changed["recorded_at"] = "2040-01-01T00:00:00Z"
        self.assertEqual(
            contracts.attestation_identity(attestation),
            contracts.attestation_identity(changed),
        )
        for mutation, expected in (
            (("builder", "network", "enabled"), "network='disabled'"),
            ((None, "build_kind", "compatibility-freeze"), "full-offline-replay"),
        ):
            bad = copy.deepcopy(attestation)
            parent, key, value = mutation
            if parent is None:
                bad[key] = value
            else:
                bad[parent][key] = value
            bad["attestation_id"] = contracts.attestation_identity(bad)
            with self.assertRaisesRegex(contracts.VerificationError, expected):
                contracts.validate_build_attestation(bad)
        missing_pack = copy.deepcopy(attestation)
        del missing_pack["inputs"]["offline_pack_id"]
        with self.assertRaisesRegex(contracts.VerificationError, "offline_pack_id"):
            contracts.validate_build_attestation(missing_pack)

    def test_v2_offline_runner_requires_explicit_replay_authority(self) -> None:
        _pack, pack_path = self.make_pack()
        process = subprocess.run(
            [sys.executable, "-I", str(TOOLS / "run_offline.py"), "--manifest", str(pack_path), "--root", str(self.root)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("offline-pack/v2 requires --workspace", process.stderr)


class ReleaseVerificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_release_artifacts(self) -> list[dict[str, object]]:
        base_generation = "2030-01-01T00:00:00Z"
        cell_generation = "2030-01-01T00:01:00Z"
        snapshot_id = "f" * 64
        base_meta = f'{{"generated_at":"{base_generation}","snapshot_id":"{snapshot_id}"}}'
        cell_meta = (
            f'{{"base_generated_at":"{base_generation}","base_snapshot_id":"{snapshot_id}",'
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
        semantic_root = contracts.compatibility_semantic_state_root(
            "brain-v3",
            "f" * 64,
            {
                path: next(
                    artifact["logical_root"]
                    for artifact in artifacts
                    if artifact["path"] == path
                )
                for path in contracts.COMPATIBILITY_SEMANTIC_PATHS
            },
        )
        release: dict[str, object] = {
            "schema": contracts.RELEASE_SCHEMA,
            "profile": contracts.RELEASE_PROFILE,
            "release_id": ZERO_HASH,
            "authority": {"git_commit": GIT_COMMIT, "semantic_state_root": semantic_root},
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
                "authority": semantic_root,
                "source_set": "sha256:" + "1" * 64,
                "prior_state": None,
            },
            "output_root": semantic_root,
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

    def refresh_sqlite_ref(self, release: dict[str, object]) -> None:
        sqlite_path = self.root / "brain/data/brain.sqlite3"
        for artifact in release["artifacts"]:
            if artifact["path"] == "brain/data/brain.sqlite3":
                artifact["sha256"], artifact["bytes"] = contracts.digest_file(
                    sqlite_path
                )
        release["release_id"] = contracts.release_identity(release)

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

    def test_rejects_non_null_authority_changeset_without_replay(self) -> None:
        release, _ = self.make_release()
        release["authority"]["through_changeset"] = "accepted-change-1"
        release["release_id"] = contracts.release_identity(release)
        with self.assertRaisesRegex(
            contracts.VerificationError,
            "accepted changeset replay verification is not implemented",
        ):
            contracts.verify_release_files(
                contracts.validate_release_manifest(release), self.root
            )

    def test_sqlite_payload_must_match_its_ordinal_and_index_row(self) -> None:
        source = self.root / "nodes.jsonl"
        metadata = {"generated_at": "2030-01-01T00:00:00Z"}
        rows = [
            {"id": "Q1", "type": "concept", "label": "One"},
            {"id": "Q2", "type": "concept", "label": "Two"},
        ]
        source.write_text(
            "\n".join(
                json.dumps(value, sort_keys=True, separators=(",", ":"))
                for value in ({"_meta": metadata}, *rows)
            )
            + "\n",
            encoding="utf-8",
        )
        artifact = {
            "path": "nodes.jsonl",
            "sha256": contracts.digest_file(source)[0],
            "bytes": source.stat().st_size,
        }
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute(
                "CREATE TABLE nodes ("
                "ordinal INTEGER, id TEXT, type TEXT, label TEXT, payload_json TEXT)"
            )
            connection.executemany(
                "INSERT INTO nodes VALUES (?, ?, ?, ?, ?)",
                [
                    (0, "Q1", "concept", "One", json.dumps(rows[1], sort_keys=True)),
                    (1, "Q2", "concept", "Two", json.dumps(rows[0], sort_keys=True)),
                ],
            )
            with self.assertRaisesRegex(
                contracts.VerificationError,
                "paired with the wrong ordinal/index row",
            ):
                contracts._verify_sqlite_artifact_rows(
                    connection,
                    self.root,
                    artifact,
                    "nodes",
                    "SELECT id, type, label, payload_json FROM nodes ORDER BY ordinal",
                    json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                )
        finally:
            connection.close()

    def test_sqlite_payload_pairing_is_json_type_strict(self) -> None:
        source = self.root / "nodes.jsonl"
        metadata = {"generated_at": "2030-01-01T00:00:00Z"}
        rows = [
            {"id": "Q1", "type": "concept", "label": "One", "flag": True},
            {"id": "Q1", "type": "concept", "label": "One", "flag": 1},
        ]
        source.write_text(
            "\n".join(
                json.dumps(value, sort_keys=True, separators=(",", ":"))
                for value in ({"_meta": metadata}, *rows)
            )
            + "\n",
            encoding="utf-8",
        )
        artifact = {
            "path": "nodes.jsonl",
            "sha256": contracts.digest_file(source)[0],
            "bytes": source.stat().st_size,
        }
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute(
                "CREATE TABLE nodes ("
                "ordinal INTEGER, id TEXT, type TEXT, label TEXT, payload_json TEXT)"
            )
            connection.executemany(
                "INSERT INTO nodes VALUES (?, ?, ?, ?, ?)",
                [
                    (0, "Q1", "concept", "One", json.dumps(rows[1], sort_keys=True)),
                    (1, "Q1", "concept", "One", json.dumps(rows[0], sort_keys=True)),
                ],
            )
            with self.assertRaisesRegex(
                contracts.VerificationError,
                "paired with the wrong ordinal/index row",
            ):
                contracts._verify_sqlite_artifact_rows(
                    connection,
                    self.root,
                    artifact,
                    "nodes",
                    "SELECT id, type, label, payload_json FROM nodes ORDER BY ordinal",
                    json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                )
        finally:
            connection.close()

    def test_rejects_sqlite_inventory_metadata_drift(self) -> None:
        mutations = (
            (
                "row_count",
                "UPDATE artifacts SET row_count = row_count + 1 WHERE name = 'nodes'",
                "row_count disagrees",
            ),
            (
                "generated_at",
                "UPDATE artifacts SET generated_at = '2040-01-01T00:00:00Z' "
                "WHERE name = 'nodes'",
                "generated_at disagrees",
            ),
            (
                "snapshot_metadata",
                "UPDATE snapshot SET metadata_json = '{\"generated_at\":\"2040-01-01T00:00:00Z\"}'",
                "snapshot metadata disagrees",
            ),
        )
        for name, statement, expected in mutations:
            with self.subTest(name=name):
                release, _ = self.make_release()
                sqlite_path = self.root / "brain/data/brain.sqlite3"
                with sqlite3.connect(sqlite_path) as connection:
                    connection.execute(statement)
                self.refresh_sqlite_ref(release)
                with self.assertRaisesRegex(contracts.VerificationError, expected):
                    contracts.verify_release_files(
                        contracts.validate_release_manifest(release), self.root
                    )

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
        if connection.execute("PRAGMA user_version").fetchone()[0] == 1:
            connection.execute(
                "UPDATE artifacts SET source_digest = ? WHERE name = 'nodes'",
                (ZERO_DIGEST,),
            )
        else:
            connection.execute(
                "UPDATE artifacts SET source_digest = ?, raw_digest = ? WHERE name = 'nodes'",
                (ZERO_DIGEST, ZERO_DIGEST),
            )
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

    def test_current_release_profile_does_not_claim_v2_offline_replay(self) -> None:
        release, _ = self.make_release()
        build_path = self.root / "attestations/build.json"
        build, _ = contracts.load_canonical_json(build_path)
        build["schema"] = contracts.BUILD_ATTESTATION_SCHEMA_V2
        build["attestation_id"] = contracts.attestation_identity(build)
        write_canonical(build_path, build)
        for ref in release["attestations"]:
            if ref["kind"] == "build":
                ref["sha256"], ref["bytes"] = contracts.digest_file(build_path)
        with self.assertRaisesRegex(
            contracts.VerificationError,
            "offline replay attestations are not integrated yet",
        ):
            contracts.verify_release_files(
                contracts.validate_release_manifest(release), self.root
            )

    def test_rejects_structurally_corrupt_sqlite(self) -> None:
        release, _ = self.make_release()
        sqlite_path = self.root / "brain/data/brain.sqlite3"
        connection = sqlite3.connect(sqlite_path)
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        index_page = connection.execute(
            "SELECT rootpage FROM sqlite_schema "
            "WHERE type = 'index' AND rootpage > 0 ORDER BY rootpage LIMIT 1"
        ).fetchone()[0]
        connection.close()

        # Destroy an index b-tree page without touching the schema/header. The
        # database still opens, but a full integrity_check must reject it.
        with sqlite_path.open("r+b") as handle:
            handle.seek((index_page - 1) * page_size)
            self.assertIn(handle.read(1), {b"\x02", b"\x05", b"\x0a", b"\x0d"})
            handle.seek((index_page - 1) * page_size)
            handle.write(b"\x00")

        corrupt = copy.deepcopy(release)
        for artifact in corrupt["artifacts"]:
            if artifact["path"] == "brain/data/brain.sqlite3":
                artifact["sha256"], artifact["bytes"] = contracts.digest_file(sqlite_path)
        corrupt["release_id"] = contracts.release_identity(corrupt)
        with self.assertRaisesRegex(contracts.VerificationError, "integrity check failed"):
            contracts.verify_release_files(
                contracts.validate_release_manifest(corrupt), self.root
            )

    def test_rejects_v2_sqlite_identity_contract_mismatches(self) -> None:
        release, _ = self.make_release()
        sqlite_path = self.root / "brain/data/brain.sqlite3"
        connection = sqlite3.connect(sqlite_path)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
        connection.execute("PRAGMA application_id = 0")
        connection.close()
        wrong_application = copy.deepcopy(release)
        for artifact in wrong_application["artifacts"]:
            if artifact["path"] == "brain/data/brain.sqlite3":
                artifact["sha256"], artifact["bytes"] = contracts.digest_file(sqlite_path)
        wrong_application["release_id"] = contracts.release_identity(wrong_application)
        with self.assertRaisesRegex(contracts.VerificationError, "application_id"):
            contracts.verify_release_files(
                contracts.validate_release_manifest(wrong_application), self.root
            )

        release, _ = self.make_release()
        connection = sqlite3.connect(sqlite_path)
        connection.execute(
            "UPDATE snapshot SET projection_id = ?",
            (ZERO_DIGEST,),
        )
        connection.commit()
        connection.close()
        wrong_projection = copy.deepcopy(release)
        for artifact in wrong_projection["artifacts"]:
            if artifact["path"] == "brain/data/brain.sqlite3":
                artifact["sha256"], artifact["bytes"] = contracts.digest_file(sqlite_path)
        wrong_projection["release_id"] = contracts.release_identity(wrong_projection)
        with self.assertRaisesRegex(contracts.VerificationError, "projection_id"):
            contracts.verify_release_files(
                contracts.validate_release_manifest(wrong_projection), self.root
            )

    def test_current_release_profile_requires_sqlite_schema_v2(self) -> None:
        release, _ = self.make_release()
        sqlite_path = self.root / "brain/data/brain.sqlite3"
        with sqlite3.connect(sqlite_path) as connection:
            connection.execute("PRAGMA user_version = 1")
        self.refresh_sqlite_ref(release)
        with self.assertRaisesRegex(
            contracts.VerificationError,
            "requires Brain SQLite schema 2",
        ):
            contracts.verify_release_files(
                contracts.validate_release_manifest(release), self.root
            )

    def test_current_release_profile_requires_sqlite_v2_endpoint_indexes(self) -> None:
        for index in ("edges_src_kind_idx", "edges_dst_kind_idx"):
            with self.subTest(index=index):
                release, _ = self.make_release()
                sqlite_path = self.root / "brain/data/brain.sqlite3"
                with sqlite3.connect(sqlite_path) as connection:
                    connection.execute(f'DROP INDEX "{index}"')
                self.refresh_sqlite_ref(release)
                with self.assertRaisesRegex(
                    contracts.VerificationError,
                    f"missing required index '{index}'",
                ):
                    contracts.verify_release_files(
                        contracts.validate_release_manifest(release), self.root
                    )

    def test_release_profile_binds_artifact_format_to_path(self) -> None:
        release, _ = self.make_release()
        for field, value, expected in (
            ("media_type", "application/json", "application/vnd.sqlite3"),
            ("logical_format", "json", "opaque"),
        ):
            with self.subTest(field=field):
                mutated = copy.deepcopy(release)
                artifact = next(
                    item
                    for item in mutated["artifacts"]
                    if item["path"] == "brain/data/brain.sqlite3"
                )
                artifact[field] = value
                if field == "logical_format":
                    artifact["logical_root"] = "sha256:" + "0" * 64
                mutated["release_id"] = contracts.release_identity(mutated)
                with self.assertRaisesRegex(contracts.VerificationError, expected):
                    contracts.validate_release_manifest(mutated)

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
