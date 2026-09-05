#!/usr/bin/env python3
"""Synthetic, network-free tests for the offline-pack v2/v3 compiler."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


HERE = Path(__file__).resolve().parent
TOOLS = HERE / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import authority_contracts as contracts  # noqa: E402
import compile_offline_pack_v2 as compiler  # noqa: E402
import execution_environment as environment  # noqa: E402
import source_plan_contracts  # noqa: E402


ZERO_HASH = "sha256:" + "0" * 64
ZERO_DIGEST = "0" * 64
SHARED_BYTES = b'{"rows":[1]}\n'
RAW_BYTES = b'{"upstream":[1]}\n'
REPLAY_BYTES = b"print('fixture replay')\n"


def _write_canonical(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contracts.canonical_json_bytes(value))


def _git(repository: Path, *arguments: str, env: dict[str, str] | None = None) -> str:
    process = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
        text=True,
    )
    if process.returncode != 0:
        raise AssertionError(process.stderr)
    return process.stdout.strip()


def _make_writable(root: Path) -> None:
    if not root.exists() or root.is_symlink():
        return
    for directory, names, filenames in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        directory_path.chmod(0o700)
        for name in names:
            child = directory_path / name
            if not child.is_symlink():
                child.chmod(0o700)
        for name in filenames:
            child = directory_path / name
            if not child.is_symlink():
                child.chmod(0o600)


class OfflinePackCompilerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.repo = self.base / "repo"
        self.external = self.base / "external"
        self.repo.mkdir()
        self.external.mkdir()

        for relative, data in {
            "brain/helper.py": b"VALUE = 1\n",
            "brain/replay.py": REPLAY_BYTES,
            "catalog/curated.json": SHARED_BYTES,
        }.items():
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.name", "WikiLean Test")
        _git(self.repo, "config", "user.email", "test@wikilean.invalid")
        _git(
            self.repo,
            "add",
            "--",
            "brain/helper.py",
            "brain/replay.py",
            "catalog/curated.json",
        )
        git_env = dict(os.environ)
        git_env.update(
            {
                "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+0000",
                "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+0000",
            }
        )
        _git(self.repo, "commit", "-q", "-m", "fixture", env=git_env)
        self.commit = _git(self.repo, "rev-parse", "HEAD")
        self.tree = _git(self.repo, "rev-parse", "HEAD^{tree}")

        (self.external / "input.json").write_bytes(SHARED_BYTES)
        (self.external / "raw.json").write_bytes(RAW_BYTES)

        self.inventory = self._inventory()
        self.inventory_path = self.repo / "fixture/inventory.json"
        _write_canonical(self.inventory_path, self.inventory)

        _write_canonical(
            self.repo / "fixture/configuration.json",
            {
                "cell_attach_kinds": ["generalization", "related"],
                "external_node_cap": 8,
                "layout": {"enabled": True, "iterations": 12},
                "schema": "wikilean.brain-reducer-config/v1",
            },
        )
        _write_canonical(
            self.repo / "fixture/environment.json",
            self._environment(),
        )
        _write_canonical(
            self.repo / "fixture/input-schema.json",
            {"type": "object"},
        )

        self.plan = self._plan()
        self.plan_path = self.repo / "fixture/source-plan.json"
        _write_canonical(self.plan_path, self.plan)

    def tearDown(self) -> None:
        _make_writable(self.base)
        self.temporary.cleanup()

    def _inventory(self) -> dict[str, object]:
        value: dict[str, object] = {
            "boundary": "post-acquisition-fold",
            "forbidden_ambient": [
                {
                    "consumers": ["*"],
                    "name": "network access",
                    "replacement": "sealed source objects",
                }
            ],
            "inputs": [
                {
                    "cardinality": "one",
                    "class": "curated_git_input",
                    "consumers": ["brain/replay.py"],
                    "id": "curated",
                    "path": "catalog/curated.json",
                    "purpose": "reviewed fixture",
                    "requirement": "required",
                    "root": "repo",
                },
                {
                    "cardinality": "many",
                    "class": "immutable_source_object",
                    "consumers": ["brain/replay.py"],
                    "id": "optional_external",
                    "path_pattern": "*_pages.jsonl",
                    "purpose": "explicitly absent fixture set",
                    "requirement": "optional",
                    "root": "external",
                },
                {
                    "cardinality": "one",
                    "class": "immutable_source_object",
                    "consumers": ["brain/helper.py", "brain/replay.py"],
                    "id": "source",
                    "path": "input.json",
                    "purpose": "normalized fixture input",
                    "requirement": "required",
                    "root": "external",
                },
            ],
            "inventory_id": ZERO_HASH,
            "roots": [
                {"id": "external", "kind": "external_tree"},
                {"id": "repo", "kind": "repository"},
            ],
            "schema": contracts.REDUCER_INPUT_INVENTORY_SCHEMA_V2,
            "scope": ["brain/helper.py", "brain/replay.py"],
            "stages": [
                {
                    "argv": [],
                    "id": "prepare",
                    "needs": [],
                    "outputs": [
                        {"kind": "file", "path": "intermediate/prepared.json"}
                    ],
                    "program": "brain/helper.py",
                },
                {
                    "argv": [],
                    "id": "replay",
                    "needs": ["prepare"],
                    "outputs": [{"kind": "tree", "path": "artifacts"}],
                    "program": "brain/replay.py",
                },
            ],
        }
        value["inventory_id"] = contracts.reducer_input_inventory_identity(value)
        contracts.validate_reducer_input_inventory(value)
        return value

    def _environment(self) -> dict[str, object]:
        value: dict[str, object] = {
            "dependency_lock": {
                "packages": [
                    {
                        "locked_artifact_sha256": "4" * 64,
                        "installed_tree_root": "sha256:" + "5" * 64,
                        "name": "numpy",
                        "version": "2.3.2",
                    }
                ],
                "schema": environment.DEPENDENCY_LOCK_SCHEMA,
            },
            "environment_id": ZERO_HASH,
            "locale": {
                "filesystem_encoding": "utf-8",
                "lang": "C.UTF-8",
                "lc_all": "C.UTF-8",
                "preferred_encoding": "utf-8",
                "python_hash_seed": "0",
                "timezone": "UTC",
                "utf8_mode": 1,
                "hash_sentinel": "123456789",
            },
            "profile": environment.AUTHORITATIVE_OCI_PROFILE,
            "python": {
                "cache_tag": "cpython-312",
                "executable_file_sha256": "3" * 64,
                "implementation": "CPython",
                "soabi": "cpython-312-linux-gnu",
                "version": "3.12.11",
            },
            "runner": {
                "files_root": "sha256:" + "2" * 64,
                "git_commit": self.commit,
                "name": "wikilean-replay",
                "version": "2.0.0",
            },
            "runtime": {
                "architecture": "x86_64",
                "manifest_digest": "sha256:" + "1" * 64,
                "kind": "oci-image",
                "os": "linux",
            },
            "sandbox": {
                "backend": "linux-bubblewrap",
                "executable_sha256": "8" * 64,
                "network": "disabled",
                "policy_id": "brain-replay-v1",
                "policy_root": "sha256:" + "9" * 64,
                "reported_version": "0.11.0",
            },
            "schema": environment.EXECUTION_ENVIRONMENT_SCHEMA,
            "sqlite": {
                "extension_file_sha256": "7" * 64,
                "compile_options": ["ENABLE_FTS5", "THREADSAFE=1"],
                "source_id": "2030-01-02 03:04:05 " + "6" * 64,
                "version": "3.50.4",
            },
        }
        return environment.seal_execution_environment(value)

    def _plan(self) -> dict[str, object]:
        tool = {"name": "fixture", "sha256": ZERO_DIGEST, "version": "1.0.0"}
        def planned_repo_file(relative: str) -> dict[str, object]:
            raw = (self.repo / relative).read_bytes()
            return {
                "bytes": len(raw),
                "path": relative,
                "root": "repo",
                "sha256": hashlib.sha256(raw).hexdigest(),
            }

        return {
            "configuration": planned_repo_file("fixture/configuration.json"),
            "environment": planned_repo_file("fixture/environment.json"),
            "input_bindings": [
                {
                    "input_id": "curated",
                    "members": [
                        {
                            "object": "identity",
                            "path": "catalog/curated.json",
                            "source": "curated-fixture",
                        }
                    ],
                    "sources": ["curated-fixture"],
                    "state": "present",
                },
                {
                    "input_id": "optional_external",
                    "members": [],
                    "sources": ["external-fixture"],
                    "state": "absent",
                },
                {
                    "input_id": "source",
                    "members": [
                        {
                            "object": "normalized",
                            "path": "input.json",
                            "source": "external-fixture",
                        }
                    ],
                    "sources": ["external-fixture"],
                    "state": "present",
                },
            ],
            "inventory_id": self.inventory["inventory_id"],
            "reducer": {
                "entrypoint": "brain/replay.py",
                "git_commit": self.commit,
                "root": "repo",
            },
            "schema": compiler.SOURCE_PLAN_SCHEMA,
            "schemas": [
                {
                    "media_type": "application/schema+json",
                    "pack_path": "schemas/input.json",
                    **planned_repo_file("fixture/input-schema.json"),
                }
            ],
            "sources": [
                {
                    "acquisition": copy.deepcopy(tool),
                    "license": {
                        "expression": "CC0-1.0",
                        "redistribution": "allowed",
                    },
                    "normalization": {
                        "inputs": ["identity"],
                        "outputs": ["identity"],
                        "schema": "fixture/identity-v1",
                        "tool": copy.deepcopy(tool),
                    },
                    "objects": [
                        {
                            "media_type": "application/json",
                            "bytes": len(SHARED_BYTES),
                            "name": "identity",
                            "path": "catalog/curated.json",
                            "redistribution": "allowed",
                            "roles": ["normalized", "raw"],
                            "root": "repo",
                            "sha256": hashlib.sha256(SHARED_BYTES).hexdigest(),
                        }
                    ],
                    "pin": {
                        "tree": self.tree,
                        "type": "git_commit",
                        "value": self.commit,
                    },
                    "source": "curated-fixture",
                    "source_kind": "curated_git_tree",
                },
                {
                    "acquisition": copy.deepcopy(tool),
                    "license": {
                        "expression": "CC0-1.0",
                        "redistribution": "allowed",
                    },
                    "normalization": {
                        "inputs": ["raw"],
                        "outputs": ["normalized"],
                        "schema": "fixture/normalized-v1",
                        "tool": copy.deepcopy(tool),
                    },
                    "objects": [
                        {
                            "media_type": "application/json",
                            "bytes": len(SHARED_BYTES),
                            "name": "normalized",
                            "path": "input.json",
                            "redistribution": "allowed",
                            "roles": ["normalized"],
                            "root": "external",
                            "sha256": hashlib.sha256(SHARED_BYTES).hexdigest(),
                        },
                        {
                            "media_type": "application/json",
                            "bytes": len(RAW_BYTES),
                            "name": "raw",
                            "path": "raw.json",
                            "redistribution": "allowed",
                            "roles": ["raw"],
                            "root": "external",
                            "sha256": hashlib.sha256(RAW_BYTES).hexdigest(),
                        },
                    ],
                    "pin": {
                        "type": "content_sha256",
                        "value": hashlib.sha256(RAW_BYTES).hexdigest(),
                    },
                    "source": "external-fixture",
                    "source_kind": "acquired_dataset",
                },
            ],
        }

    def _upgrade_plan_v3(
        self,
    ) -> tuple[dict[str, object], dict[str, object], dict[str, object], bytes]:
        plan = copy.deepcopy(self.plan)
        plan["schema"] = compiler.SOURCE_PLAN_SCHEMA_V3
        source = next(
            item for item in plan["sources"] if item["source"] == "external-fixture"
        )
        raw_object = next(item for item in source["objects"] if "raw" in item["roles"])
        normalized_object = next(
            item for item in source["objects"] if "normalized" in item["roles"]
        )
        preimage = b'{"query":"fixture"}'
        preimage_digest = hashlib.sha256(preimage).hexdigest()
        (self.external / "request.json").write_bytes(preimage)

        def evidence_object(item: dict[str, object]) -> dict[str, object]:
            return {
                "object": item["name"],
                "sha256": item["sha256"],
                "bytes": item["bytes"],
                "media_type": item["media_type"],
            }

        requests = [
            {
                "kind": "http_get",
                "uri": "https://example.invalid/fixture",
                "parameters_sha256": preimage_digest,
            }
        ]
        receipt: dict[str, object] = {
            "schema": contracts.ACQUISITION_RECEIPT_SCHEMA_V1,
            "acquisition_receipt_id": ZERO_HASH,
            "source": source["source"],
            "upstream_uri": "https://example.invalid/fixture",
            "pin": copy.deepcopy(source["pin"]),
            "tool": copy.deepcopy(source["acquisition"]),
            "requests": requests,
            "batch": {
                "status": "complete",
                "request_set_root": contracts.acquisition_request_set_root(requests),
                "requests_total": 1,
                "requests_succeeded": 1,
                "requests_failed": 0,
            },
            "outputs": [evidence_object(raw_object)],
            "audit": {"acquired_at": "2030-01-01T00:00:00Z"},
        }
        receipt["acquisition_receipt_id"] = contracts.acquisition_receipt_identity(receipt)
        _write_canonical(self.external / "receipt.json", receipt)

        lineage: dict[str, object] = {
            "schema": contracts.NORMALIZATION_LINEAGE_SCHEMA_V1,
            "normalization_lineage_id": ZERO_HASH,
            "source": source["source"],
            "mode": "transform",
            "acquisition_receipt_ids": [receipt["acquisition_receipt_id"]],
            "parent_source_manifest_ids": [],
            "normalization_schema": source["normalization"]["schema"],
            "configuration_sha256": "5" * 64,
            "tool": copy.deepcopy(source["normalization"]["tool"]),
            "inputs": [
                {
                    **evidence_object(raw_object),
                    "origin": {
                        "kind": "acquisition_receipt",
                        "id": receipt["acquisition_receipt_id"],
                    },
                }
            ],
            "outputs": [evidence_object(normalized_object)],
            "result": "complete",
            "audit": {"normalized_at": "2030-01-01T00:01:00Z"},
        }
        lineage["normalization_lineage_id"] = contracts.normalization_lineage_identity(
            lineage
        )
        _write_canonical(self.external / "lineage.json", lineage)

        def planned_evidence_file(
            relative: str,
            media_type: str,
        ) -> dict[str, object]:
            raw = (self.external / relative).read_bytes()
            return {
                "root": "external",
                "path": relative,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
                "media_type": media_type,
            }

        source["evidence"] = {
            "acquisition_receipts": [
                {
                    **planned_evidence_file("receipt.json", "application/json"),
                    "acquisition_receipt_id": receipt["acquisition_receipt_id"],
                }
            ],
            "normalization_lineage": {
                **planned_evidence_file("lineage.json", "application/json"),
                "normalization_lineage_id": lineage["normalization_lineage_id"],
            },
            "request_parameter_preimages": [
                {
                    **planned_evidence_file("request.json", "application/json"),
                    "parameters_sha256": preimage_digest,
                }
            ],
        }
        self.plan = plan
        _write_canonical(self.plan_path, self.plan)
        return plan, receipt, lineage, preimage

    def _compile(self, store_name: str = "store", **kwargs: object) -> compiler.CompiledPack:
        return compiler.compile_offline_pack_v2(
            self.plan_path,
            self.inventory_path,
            self.base / store_name,
            roots={"external": self.external, "repo": self.repo},
            **kwargs,
        )

    def _published_directories(self, store_name: str = "store") -> list[Path]:
        store = self.base / store_name
        if not store.exists():
            return []
        return sorted(path for path in store.iterdir() if not path.name.startswith("."))

    def test_compiles_verified_deduplicated_read_only_pack_and_reuses_it(self) -> None:
        (self.repo / "brain/replay.py").write_bytes(b"uncommitted mutation\n")

        first = self._compile()
        self.assertFalse(first.reused)
        self.assertEqual(
            first.offline_pack_id,
            "sha256:7b39cf25ec766b0c5623345c54a7b8f006b711c05251530fc7632975db188fbe",
        )
        self.assertEqual(first.source_manifests, 2)
        self.assertEqual(first.source_objects, 2)
        self.assertEqual(
            (first.root / "reducer/brain/replay.py").read_bytes(),
            REPLAY_BYTES,
        )

        pack, _ = contracts.load_canonical_json(first.manifest_path)
        counts = contracts.verify_offline_pack_files(
            contracts.validate_offline_pack(pack),
            first.root,
            manifest_path=first.manifest_path,
        )
        self.assertEqual(counts["source_objects"], 2)
        for path in (first.root, *first.root.rglob("*")):
            self.assertFalse(path.lstat().st_mode & 0o222, path)

        second = self._compile()
        self.assertTrue(second.reused)
        self.assertEqual(second.offline_pack_id, first.offline_pack_id)
        self.assertEqual(second.root, first.root)

    def test_v3_compiles_verified_evidence_pack_and_reuses_it(self) -> None:
        _plan, receipt, lineage, preimage = self._upgrade_plan_v3()
        first = self._compile("store-v3")
        self.assertFalse(first.reused)
        pack, _ = contracts.load_canonical_json(first.manifest_path)
        self.assertEqual(pack["schema"], contracts.PACK_SCHEMA_V3)
        self.assertEqual(
            pack["source_set_root"],
            contracts.source_set_root_v3(
                pack["inventory"]["inventory_id"],
                [ref["source_manifest_id"] for ref in pack["source_manifests"]],
                pack["input_bindings"],
            ),
        )
        receipt_ref = pack["evidence"]["acquisition_receipts"][0]
        lineage_ref = pack["evidence"]["normalization_lineages"][0]
        preimage_ref = pack["evidence"]["request_parameter_preimages"][0]
        self.assertEqual(
            receipt_ref["path"],
            "evidence/acquisition-receipts/"
            + receipt["acquisition_receipt_id"].removeprefix("sha256:")
            + ".json",
        )
        self.assertEqual(
            lineage_ref["path"],
            "evidence/normalization-lineages/"
            + lineage["normalization_lineage_id"].removeprefix("sha256:")
            + ".json",
        )
        self.assertEqual(
            preimage_ref["path"],
            "evidence/request-parameters/sha256/"
            + hashlib.sha256(preimage).hexdigest(),
        )
        self.assertEqual((first.root / preimage_ref["path"]).read_bytes(), preimage)
        counts = contracts.verify_offline_pack_files(
            contracts.validate_offline_pack(pack),
            first.root,
            manifest_path=first.manifest_path,
        )
        self.assertEqual(counts["acquisition_receipts"], 1)
        self.assertEqual(counts["normalization_lineages"], 1)
        self.assertEqual(counts["request_parameter_preimages"], 1)
        manifests = [
            contracts.load_canonical_json(first.root / ref["path"])[0]
            for ref in pack["source_manifests"]
        ]
        acquired = next(
            manifest
            for manifest in manifests
            if manifest["source"] == "external-fixture"
        )
        curated = next(
            manifest
            for manifest in manifests
            if manifest["source"] == "curated-fixture"
        )
        self.assertEqual(acquired["schema"], contracts.SOURCE_SCHEMA_V3)
        self.assertEqual(
            acquired["evidence"]["acquisition_receipt_ids"],
            [receipt["acquisition_receipt_id"]],
        )
        self.assertNotIn("evidence", curated)

        second = self._compile("store-v3")
        self.assertTrue(second.reused)
        self.assertEqual(second.offline_pack_id, first.offline_pack_id)
        self.assertEqual(second.manifest_path.read_bytes(), first.manifest_path.read_bytes())

    def test_v3_compiles_evidence_only_parent_manifest(self) -> None:
        self._upgrade_plan_v3()
        prospective = source_plan_contracts.source_manifests_from_plan_v3(self.plan)
        parent = next(
            manifest
            for manifest in prospective.values()
            if manifest["source"] == "external-fixture"
        )
        identity_digest = hashlib.sha256(SHARED_BYTES).hexdigest()

        def evidence_object() -> dict[str, object]:
            return {
                "object": "identity",
                "sha256": identity_digest,
                "bytes": len(SHARED_BYTES),
                "media_type": "application/json",
            }

        lineage: dict[str, object] = {
            "schema": contracts.NORMALIZATION_LINEAGE_SCHEMA_V1,
            "normalization_lineage_id": ZERO_HASH,
            "source": "derived-fixture",
            "mode": "identity",
            "acquisition_receipt_ids": [],
            "parent_source_manifest_ids": [parent["source_manifest_id"]],
            "normalization_schema": "fixture/derived-v1",
            "configuration_sha256": "6" * 64,
            "tool": {"name": "fixture", "sha256": ZERO_DIGEST, "version": "1.0.0"},
            "inputs": [
                {
                    **evidence_object(),
                    "origin": {
                        "kind": "source_manifest",
                        "id": parent["source_manifest_id"],
                    },
                }
            ],
            "outputs": [evidence_object()],
            "result": "complete",
            "audit": {"normalized_at": "2030-01-01T00:02:00Z"},
        }
        # The parent normalized object is named "normalized", so use that name
        # consistently in the child identity lineage.
        lineage["inputs"][0]["object"] = "normalized"
        lineage["outputs"][0]["object"] = "normalized"
        lineage["normalization_lineage_id"] = contracts.normalization_lineage_identity(
            lineage
        )
        _write_canonical(self.external / "derived-lineage.json", lineage)
        lineage_raw = (self.external / "derived-lineage.json").read_bytes()
        child = {
            "source": "derived-fixture",
            "source_kind": "sealed_snapshot",
            "pin": {"type": "database_snapshot", "value": "derived-r1"},
            "objects": [
                {
                    "name": "normalized",
                    "roles": ["normalized", "raw"],
                    "root": "external",
                    "path": "input.json",
                    "sha256": identity_digest,
                    "bytes": len(SHARED_BYTES),
                    "media_type": "application/json",
                    "redistribution": "allowed",
                }
            ],
            "license": {"expression": "CC0-1.0", "redistribution": "allowed"},
            "acquisition": {"name": "fixture", "sha256": ZERO_DIGEST, "version": "1.0.0"},
            "normalization": {
                "schema": "fixture/derived-v1",
                "tool": {"name": "fixture", "sha256": ZERO_DIGEST, "version": "1.0.0"},
                "inputs": ["normalized"],
                "outputs": ["normalized"],
            },
            "evidence": {
                "acquisition_receipts": [],
                "normalization_lineage": {
                    "root": "external",
                    "path": "derived-lineage.json",
                    "sha256": hashlib.sha256(lineage_raw).hexdigest(),
                    "bytes": len(lineage_raw),
                    "media_type": "application/json",
                    "normalization_lineage_id": lineage[
                        "normalization_lineage_id"
                    ],
                },
                "request_parameter_preimages": [],
            },
        }
        self.plan["sources"].append(child)
        self.plan["sources"].sort(key=lambda item: item["source"])
        source_binding = next(
            item for item in self.plan["input_bindings"] if item["input_id"] == "source"
        )
        source_binding["sources"] = ["derived-fixture"]
        source_binding["members"][0]["source"] = "derived-fixture"
        optional_binding = next(
            item
            for item in self.plan["input_bindings"]
            if item["input_id"] == "optional_external"
        )
        optional_binding["sources"] = ["derived-fixture"]
        _write_canonical(self.plan_path, self.plan)

        result = self._compile("store-v3-parent")
        pack, _ = contracts.load_canonical_json(result.manifest_path)
        self.assertEqual(pack["schema"], contracts.PACK_SCHEMA_V3)
        self.assertEqual(result.source_manifests, 3)
        contracts.verify_offline_pack_files(
            contracts.validate_offline_pack(pack),
            result.root,
            manifest_path=result.manifest_path,
        )

    def test_v3_evidence_tampering_and_copy_races_fail_closed(self) -> None:
        self._upgrade_plan_v3()
        (self.external / "request.json").write_bytes(b"tampered-before-compile")
        with self.assertRaisesRegex(compiler.PackCompilationError, "source plan evidence"):
            self._compile("store-v3-tampered")
        self.assertEqual(self._published_directories("store-v3-tampered"), [])

        self._upgrade_plan_v3()
        mutated_during_copy = False

        def mutate_during_copy(location: str) -> None:
            nonlocal mutated_during_copy
            if not mutated_during_copy and "request_parameter_preimage" in location:
                mutated_during_copy = True
                (self.external / "request.json").write_bytes(b"changed-during-copy")

        with self.assertRaisesRegex(compiler.PackCompilationError, "changed while"):
            self._compile(
                "store-v3-copy-race",
                after_copy=mutate_during_copy,
            )
        self.assertTrue(mutated_during_copy)
        self.assertEqual(self._published_directories("store-v3-copy-race"), [])

        self._upgrade_plan_v3()

        def mutate_before_seal() -> None:
            (self.external / "receipt.json").write_bytes(b"changed-before-seal")

        with self.assertRaisesRegex(compiler.PackCompilationError, "changed after validation"):
            self._compile(
                "store-v3-seal-race",
                before_seal=mutate_before_seal,
            )
        self.assertEqual(self._published_directories("store-v3-seal-race"), [])

    def test_v3_reuse_reverifies_evidence_bytes(self) -> None:
        self._upgrade_plan_v3()
        first = self._compile("store-v3-reuse")
        pack, _ = contracts.load_canonical_json(first.manifest_path)
        receipt_path = first.root / pack["evidence"]["acquisition_receipts"][0]["path"]
        _make_writable(first.root)
        receipt_path.write_bytes(b"tampered evidence")
        compiler._make_read_only(first.root)
        with self.assertRaisesRegex(
            compiler.PackCompilationError,
            "existing pack verification failed",
        ):
            self._compile("store-v3-reuse")

    def test_identity_is_independent_of_mount_paths_and_mtimes(self) -> None:
        first = self._compile("store-a")
        relocated_repo = self.base / "relocated/repo"
        relocated_external = self.base / "relocated/external"
        shutil.copytree(self.repo, relocated_repo, symlinks=True)
        shutil.copytree(self.external, relocated_external, symlinks=True)
        self.assertNotEqual(self.repo.stat().st_ino, relocated_repo.stat().st_ino)
        self.assertNotEqual(self.external.stat().st_ino, relocated_external.stat().st_ino)
        for path in (
            relocated_external / "input.json",
            relocated_external / "raw.json",
            relocated_repo / "brain/helper.py",
            relocated_repo / "brain/replay.py",
            relocated_repo / "catalog/curated.json",
            relocated_repo / "fixture/configuration.json",
            relocated_repo / "fixture/environment.json",
            relocated_repo / "fixture/input-schema.json",
        ):
            os.utime(path, ns=(1_800_000_000_000_000_000,) * 2)

        second = compiler.compile_offline_pack_v2(
            relocated_repo / "fixture/source-plan.json",
            relocated_repo / "fixture/inventory.json",
            self.base / "store-b",
            roots={"external": relocated_external, "repo": relocated_repo},
        )
        self.assertEqual(second.offline_pack_id, first.offline_pack_id)
        self.assertEqual(second.source_set_root, first.source_set_root)
        self.assertEqual(second.manifest_path.read_bytes(), first.manifest_path.read_bytes())

    def test_explicit_absence_rejects_new_matching_input_before_staging(self) -> None:
        (self.external / "unexpected_pages.jsonl").write_bytes(b'{}\n')

        with self.assertRaisesRegex(compiler.PackCompilationError, "member set"):
            self._compile()
        self.assertEqual(self._published_directories(), [])
        self.assertEqual(list((self.base / "store").glob(".offline-pack-*")), [])

    def test_curated_membership_and_bytes_come_from_pinned_git_tree(self) -> None:
        curated = self.inventory["inputs"][0]
        del curated["path"]
        curated["path_pattern"] = "catalog/*.json"
        curated["cardinality"] = "many"
        optional = self.inventory["inputs"][1]
        optional["class"] = "curated_git_input"
        optional["path_pattern"] = "catalog/missing-*.json"
        optional["root"] = "repo"
        self.inventory["inventory_id"] = ZERO_HASH
        self.inventory["inventory_id"] = contracts.reducer_input_inventory_identity(
            self.inventory
        )
        _write_canonical(self.inventory_path, self.inventory)
        self.plan["inventory_id"] = self.inventory["inventory_id"]
        self.plan["input_bindings"][1]["sources"] = ["curated-fixture"]
        _write_canonical(self.plan_path, self.plan)

        (self.repo / "catalog/curated.json").unlink()
        (self.repo / "catalog/untracked.json").write_bytes(b"not in pinned tree\n")
        (self.repo / "catalog/missing-local.json").write_bytes(b"also not pinned\n")
        with mock.patch.object(
            compiler,
            "_git_output",
            wraps=compiler._git_output,
        ) as git_output:
            result = self._compile()

        self.assertEqual(result.source_objects, 2)
        recursive_indexes = [
            call
            for call in git_output.call_args_list
            if call.args[2][:3] == ["ls-tree", "-r", "-z"]
        ]
        self.assertEqual(len(recursive_indexes), 1)
        digest = hashlib.sha256(SHARED_BYTES).hexdigest()
        self.assertEqual(
            (result.root / f"objects/sha256/{digest}").read_bytes(),
            SHARED_BYTES,
        )

    def test_wildcard_binding_can_combine_distinct_source_manifests(self) -> None:
        alpha = b'{"page":"alpha"}\n'
        alpha_raw = b'{"raw":"alpha"}\n'
        beta = b'{"page":"beta"}\n'
        beta_raw = b'{"raw":"beta"}\n'
        for relative, raw in {
            "alpha_pages.jsonl": alpha,
            "alpha.raw": alpha_raw,
            "beta_pages.jsonl": beta,
            "beta.raw": beta_raw,
        }.items():
            (self.external / relative).write_bytes(raw)

        tool = {"name": "fixture", "sha256": ZERO_DIGEST, "version": "1.0.0"}

        def page_source(
            name: str,
            normalized_path: str,
            normalized: bytes,
            raw_path: str,
            raw: bytes,
        ) -> dict[str, object]:
            return {
                "acquisition": copy.deepcopy(tool),
                "license": {
                    "expression": "CC0-1.0",
                    "redistribution": "allowed",
                },
                "normalization": {
                    "inputs": ["raw"],
                    "outputs": ["normalized"],
                    "schema": "fixture/pages-v1",
                    "tool": copy.deepcopy(tool),
                },
                "objects": [
                    {
                        "bytes": len(normalized),
                        "media_type": "application/x-ndjson",
                        "name": "normalized",
                        "path": normalized_path,
                        "redistribution": "allowed",
                        "roles": ["normalized"],
                        "root": "external",
                        "sha256": hashlib.sha256(normalized).hexdigest(),
                    },
                    {
                        "bytes": len(raw),
                        "media_type": "application/x-ndjson",
                        "name": "raw",
                        "path": raw_path,
                        "redistribution": "allowed",
                        "roles": ["raw"],
                        "root": "external",
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    },
                ],
                "pin": {
                    "type": "content_sha256",
                    "value": hashlib.sha256(raw).hexdigest(),
                },
                "source": name,
                "source_kind": "acquired_dataset",
            }

        self.plan["sources"].extend(
            [
                page_source(
                    "alpha-pages",
                    "alpha_pages.jsonl",
                    alpha,
                    "alpha.raw",
                    alpha_raw,
                ),
                page_source(
                    "beta-pages",
                    "beta_pages.jsonl",
                    beta,
                    "beta.raw",
                    beta_raw,
                ),
            ]
        )
        self.plan["sources"].sort(key=lambda source: source["source"])
        wildcard = self.plan["input_bindings"][1]
        wildcard["sources"] = ["alpha-pages", "beta-pages"]
        wildcard["state"] = "present"
        wildcard["members"] = [
            {
                "object": "normalized",
                "path": "alpha_pages.jsonl",
                "source": "alpha-pages",
            },
            {
                "object": "normalized",
                "path": "beta_pages.jsonl",
                "source": "beta-pages",
            },
        ]
        _write_canonical(self.plan_path, self.plan)

        result = self._compile()
        pack, _ = contracts.load_canonical_json(result.manifest_path)
        binding = next(
            item
            for item in pack["input_bindings"]
            if item["input_id"] == "optional_external"
        )
        self.assertEqual(result.source_manifests, 4)
        self.assertEqual(len(binding["members"]), 2)
        self.assertEqual(
            len({member["source_manifest_id"] for member in binding["members"]}),
            2,
        )
        self.assertEqual(
            binding["source_manifest_ids"],
            sorted(
                {member["source_manifest_id"] for member in binding["members"]}
            ),
        )

    def test_absent_binding_source_provenance_affects_identity(self) -> None:
        first = self._compile("store-a")
        first_pack, _ = contracts.load_canonical_json(first.manifest_path)
        first_absent = next(
            binding
            for binding in first_pack["input_bindings"]
            if binding["input_id"] == "optional_external"
        )

        self.plan["input_bindings"][1]["sources"] = ["curated-fixture"]
        _write_canonical(self.plan_path, self.plan)
        second = self._compile("store-b")
        second_pack, _ = contracts.load_canonical_json(second.manifest_path)
        second_absent = next(
            binding
            for binding in second_pack["input_bindings"]
            if binding["input_id"] == "optional_external"
        )

        self.assertNotEqual(first.offline_pack_id, second.offline_pack_id)
        self.assertNotEqual(first.source_set_root, second.source_set_root)
        self.assertNotEqual(
            first_absent["source_manifest_ids"],
            second_absent["source_manifest_ids"],
        )

    def test_source_manifest_can_be_bound_only_to_explicit_absence(self) -> None:
        absence_source = copy.deepcopy(self.plan["sources"][1])
        absence_source["source"] = "absence-fixture"
        self.plan["sources"].append(absence_source)
        self.plan["sources"].sort(key=lambda source: source["source"])
        self.plan["input_bindings"][1]["sources"] = ["absence-fixture"]
        _write_canonical(self.plan_path, self.plan)

        result = self._compile()
        pack, _ = contracts.load_canonical_json(result.manifest_path)
        manifests: dict[str, str] = {}
        for reference in pack["source_manifests"]:
            manifest, _ = contracts.load_canonical_json(result.root / reference["path"])
            manifests[manifest["source"]] = manifest["source_manifest_id"]
        absent = next(
            binding
            for binding in pack["input_bindings"]
            if binding["input_id"] == "optional_external"
        )
        self.assertEqual(
            absent["source_manifest_ids"],
            [manifests["absence-fixture"]],
        )
        self.assertEqual(result.source_manifests, 3)

    def test_source_manifest_review_audit_lineage_and_receipt_pass_through(self) -> None:
        receipt = b'{"download":"fixture","status":200}\n'
        (self.external / "receipt.json").write_bytes(receipt)
        source = self.plan["sources"][1]
        source["audit"] = {
            "acquired_at": "2030-01-01T00:00:00Z",
            "upstream_uri": "https://example.invalid/fixture",
        }
        source["previous_source_manifest_id"] = None
        source["review"] = {
            "expected_semantic_effects": ["fixture-only"],
            "summary": "Synthetic acquisition review",
        }
        source["objects"].append(
            {
                "bytes": len(receipt),
                "media_type": "application/json",
                "name": "receipt",
                "path": "receipt.json",
                "redistribution": "allowed",
                "roles": ["receipt"],
                "root": "external",
                "sha256": hashlib.sha256(receipt).hexdigest(),
            }
        )
        _write_canonical(self.plan_path, self.plan)

        result = self._compile()
        pack, _ = contracts.load_canonical_json(result.manifest_path)
        manifests = []
        for ref in pack["source_manifests"]:
            document, _ = contracts.load_canonical_json(result.root / ref["path"])
            manifests.append(document)
        external = next(
            manifest
            for manifest in manifests
            if manifest["source"] == "external-fixture"
        )
        self.assertEqual(external["audit"], source["audit"])
        self.assertEqual(external["review"], source["review"])
        self.assertIsNone(external["previous_source_manifest_id"])
        self.assertIn(
            "receipt",
            next(item for item in external["objects"] if item["name"] == "receipt")[
                "roles"
            ],
        )

    def test_late_mutable_input_add_and_remove_are_rejected(self) -> None:
        def add_input() -> None:
            (self.external / "late_pages.jsonl").write_bytes(b'{}\n')

        with self.assertRaisesRegex(compiler.PackCompilationError, "changed during"):
            self._compile(before_seal=add_input)
        (self.external / "late_pages.jsonl").unlink()

        def remove_input() -> None:
            (self.external / "input.json").unlink()

        with self.assertRaisesRegex(compiler.PackCompilationError, "changed during"):
            self._compile(before_seal=remove_input)
        self.assertEqual(self._published_directories(), [])
        self.assertEqual(list((self.base / "store").glob(".offline-pack-*")), [])

    def test_late_root_replacement_is_rejected_by_inode(self) -> None:
        original = self.base / "external-original"

        def replace_root() -> None:
            self.external.rename(original)
            self.external.mkdir()
            (self.external / "input.json").write_bytes(SHARED_BYTES)
            (self.external / "raw.json").write_bytes(RAW_BYTES)

        with self.assertRaisesRegex(
            compiler.PackCompilationError,
            "roots.external: directory inode or ownership changed",
        ):
            self._compile(before_seal=replace_root)
        self.assertEqual(self._published_directories(), [])

    def test_candidate_mutation_after_initial_assembly_is_not_published(self) -> None:
        def corrupt_candidate() -> None:
            staging = next((self.base / "store").glob(".offline-pack-*"))
            manifest = staging / "offline-pack.json"
            manifest.chmod(0o600)
            manifest.write_bytes(b"{}\n")

        with self.assertRaisesRegex(
            compiler.PackCompilationError,
            "existing pack verification failed",
        ):
            self._compile(before_seal=corrupt_candidate)
        self.assertEqual(self._published_directories(), [])
        self.assertEqual(list((self.base / "store").glob(".offline-pack-*")), [])

    def test_publication_is_anchored_to_open_output_store(self) -> None:
        store = self.base / "store"
        displaced = self.base / "store-displaced"
        real_publish = compiler._publish_no_replace

        def replace_store_then_publish(
            store_descriptor: int,
            staging_name: str,
            target_name: str,
        ) -> None:
            store.rename(displaced)
            store.mkdir(mode=0o700)
            store.chmod(0o700)
            fake = store / staging_name
            fake.mkdir(mode=0o700)
            (fake / "marker").write_text("not the verified candidate", encoding="utf-8")
            real_publish(store_descriptor, staging_name, target_name)

        with mock.patch.object(
            compiler,
            "_publish_no_replace",
            side_effect=replace_store_then_publish,
        ):
            with self.assertRaisesRegex(
                compiler.PackCompilationError,
                "output store: directory inode or ownership changed",
            ):
                self._compile()

        self.assertEqual(
            [path for path in store.iterdir() if not path.name.startswith(".")],
            [],
        )
        published = [
            path for path in displaced.iterdir() if not path.name.startswith(".")
        ]
        self.assertEqual(len(published), 1)
        pack, _ = contracts.load_canonical_json(published[0] / "offline-pack.json")
        contracts.verify_offline_pack_files(
            contracts.validate_offline_pack(pack),
            published[0],
            manifest_path=published[0] / "offline-pack.json",
        )

    def test_same_id_reuse_rechecks_target_inode_after_verification(self) -> None:
        first = self._compile()
        displaced = first.root.with_name(first.root.name + "-displaced")
        real_verify = compiler._verify_existing_pack
        swapped = False

        def verify_then_replace(
            root: Path,
            expected_id: str,
            expected_fingerprints: object,
        ) -> dict[str, int]:
            nonlocal swapped
            counts = real_verify(root, expected_id, expected_fingerprints)
            if root == first.root and not swapped:
                swapped = True
                root.rename(displaced)
                root.mkdir(mode=0o700)
                (root / "offline-pack.json").write_bytes(b"{}\n")
                (root / "offline-pack.json").chmod(0o444)
                root.chmod(0o555)
            return counts

        with mock.patch.object(
            compiler,
            "_verify_existing_pack",
            side_effect=verify_then_replace,
        ):
            with self.assertRaisesRegex(
                compiler.PackCompilationError,
                "existing pack: directory inode or ownership changed",
            ):
                self._compile()
        self.assertTrue(swapped)
        self.assertEqual((first.root / "offline-pack.json").read_bytes(), b"{}\n")
        displaced_pack, _ = contracts.load_canonical_json(
            displaced / "offline-pack.json"
        )
        contracts.verify_offline_pack_files(
            contracts.validate_offline_pack(displaced_pack),
            displaced,
            manifest_path=displaced / "offline-pack.json",
        )

    def test_output_store_policy_and_cleanup_inode_guard(self) -> None:
        overlapping = self.repo / "new-store"
        with self.assertRaisesRegex(compiler.PackCompilationError, "overlaps"):
            compiler.compile_offline_pack_v2(
                self.plan_path,
                self.inventory_path,
                overlapping,
                roots={"external": self.external, "repo": self.repo},
            )
        self.assertFalse(overlapping.exists())

        public_store = self.base / "public-store"
        public_store.mkdir(mode=0o755)
        public_store.chmod(0o755)
        with self.assertRaisesRegex(compiler.PackCompilationError, "mode 0700"):
            compiler.compile_offline_pack_v2(
                self.plan_path,
                self.inventory_path,
                public_store,
                roots={"external": self.external, "repo": self.repo},
            )

        staging = self.base / "staging"
        staging.mkdir(mode=0o700)
        identity = compiler._validate_private_directory(staging, "staging")
        displaced = self.base / "displaced"
        staging.rename(displaced)
        staging.mkdir(mode=0o700)
        with self.assertRaisesRegex(compiler.PackCompilationError, "inode"):
            compiler._remove_tree(staging, identity)
        self.assertTrue(staging.is_dir())

        raced = self.base / "raced-staging"
        raced.mkdir(mode=0o700)
        (raced / "candidate").write_text("candidate", encoding="utf-8")
        raced_identity = compiler._validate_private_directory(raced, "staging")
        raced_displaced = self.base / "raced-staging-displaced"
        real_listdir = os.listdir
        did_swap = False

        def swap_during_cleanup(descriptor: int) -> list[str]:
            nonlocal did_swap
            if not did_swap:
                did_swap = True
                raced.rename(raced_displaced)
                raced.mkdir(mode=0o700)
                (raced / "keep").write_text("unrelated", encoding="utf-8")
            return real_listdir(descriptor)

        with mock.patch.object(compiler.os, "listdir", side_effect=swap_during_cleanup):
            with self.assertRaisesRegex(compiler.PackCompilationError, "inode"):
                compiler._remove_tree(raced, raced_identity)
        self.assertEqual((raced / "keep").read_text(encoding="utf-8"), "unrelated")

    def test_plan_roles_hashes_and_portable_paths_are_strict(self) -> None:
        invalid_role = copy.deepcopy(self.plan)
        invalid_role["sources"][0]["objects"][0]["roles"] = [["raw"]]
        with self.assertRaisesRegex(compiler.PackCompilationError, "expected a string"):
            compiler.validate_source_plan(invalid_role)

        ambiguous_pin = copy.deepcopy(self.plan)
        normalized = ambiguous_pin["sources"][1]["objects"][0]
        normalized["roles"] = ["normalized", "raw"]
        ambiguous_pin["sources"][1]["normalization"]["inputs"] = [
            "normalized",
            "raw",
        ]
        with self.assertRaisesRegex(compiler.PackCompilationError, "exactly one raw"):
            compiler.validate_source_plan(ambiguous_pin)

        mismatched_pin = copy.deepcopy(self.plan)
        mismatched_pin["sources"][1]["pin"]["value"] = "e" * 64
        with self.assertRaisesRegex(compiler.PackCompilationError, "sole raw"):
            compiler.validate_source_plan(mismatched_pin)

        unlisted_member_source = copy.deepcopy(self.plan)
        unlisted_member_source["input_bindings"][2]["sources"] = [
            "curated-fixture"
        ]
        with self.assertRaisesRegex(compiler.PackCompilationError, "binding sources"):
            compiler.validate_source_plan(unlisted_member_source)

        unused_binding_source = copy.deepcopy(self.plan)
        unused_binding_source["input_bindings"][2]["sources"] = [
            "curated-fixture",
            "external-fixture",
        ]
        with self.assertRaisesRegex(
            compiler.PackCompilationError, "exactly equal the member source set"
        ):
            compiler.validate_source_plan(unused_binding_source)

        mixed_curated = copy.deepcopy(self.plan)
        mixed_curated["input_bindings"][0]["sources"] = [
            "curated-fixture",
            "external-fixture",
        ]
        mixed_curated["input_bindings"][0]["members"].append(
            {
                "object": "normalized",
                "path": "catalog/external.json",
                "source": "external-fixture",
            }
        )
        _write_canonical(self.plan_path, mixed_curated)
        with self.assertRaisesRegex(compiler.PackCompilationError, "exactly one"):
            self._compile()

        bad_hash = copy.deepcopy(self.plan)
        bad_hash["sources"][0]["objects"][0]["sha256"] = "f" * 64
        _write_canonical(self.plan_path, bad_hash)
        with self.assertRaisesRegex(compiler.PackCompilationError, "approved source plan"):
            self._compile()

        aliases = copy.deepcopy(self.plan)
        aliases["schemas"] = [
            {
                **aliases["schemas"][0],
                "pack_path": "schemas/Input.json",
            },
            aliases["schemas"][0],
        ]
        with self.assertRaisesRegex(compiler.PackCompilationError, "aliases"):
            compiler.validate_source_plan(aliases)

    def test_traversal_errors_are_not_silently_ignored(self) -> None:
        def broken_walk(
            _root: Path,
            *,
            topdown: bool,
            followlinks: bool,
            onerror: object,
        ) -> object:
            del topdown, followlinks
            onerror(PermissionError("fixture traversal denial"))
            return iter(())

        with mock.patch.object(compiler.os, "walk", broken_walk):
            with self.assertRaisesRegex(
                compiler.PackCompilationError,
                "directory traversal failed",
            ):
                compiler._enumerate_pattern(
                    self.external,
                    "*_pages.jsonl",
                    "fixture traversal",
                )

    def test_cli_smoke_and_schema_surface_parity(self) -> None:
        process = subprocess.run(
            [
                sys.executable,
                str(TOOLS / "compile_offline_pack_v2.py"),
                "--plan",
                str(self.plan_path),
                "--inventory",
                str(self.inventory_path),
                "--output-store",
                str(self.base / "cli-store"),
                "--root",
                f"external={self.external}",
                "--root",
                f"repo={self.repo}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertTrue(result["ok"])
        self.assertFalse(result["reused"])

        schema = json.loads(
            (HERE / "authority/schemas/offline-pack-source-plan/v1.json").read_text()
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(self.plan))
        self.assertEqual(
            set(schema["$defs"]["sourceObject"]["required"]),
            set(self.plan["sources"][0]["objects"][0]),
        )
        self.assertEqual(
            set(schema["$defs"]["source"]["required"]),
            set(self.plan["sources"][0]),
        )
        self.assertEqual(
            set(schema["$defs"]["inputBinding"]["required"]),
            set(self.plan["input_bindings"][0]),
        )
        self.assertEqual(
            set(schema["$defs"]["rootFile"]["required"]),
            set(self.plan["configuration"]),
        )
        self.assertEqual(
            set(schema["$defs"]["schemaFile"]["required"]),
            set(self.plan["schemas"][0]),
        )
        content_pin_rule = schema["$defs"]["source"]["allOf"][1]["then"]
        self.assertEqual(
            content_pin_rule["properties"]["objects"]["maxContains"],
            1,
        )
        source_manifest_schema = json.loads(
            (HERE / "authority/schemas/source-manifest/v2.json").read_text()
        )
        for optional in ("previous_source_manifest_id", "review", "audit"):
            self.assertEqual(
                schema["$defs"]["source"]["properties"][optional],
                source_manifest_schema["properties"][optional],
            )

    def test_symlinked_source_is_rejected_and_candidate_is_removed(self) -> None:
        raw = self.external / "raw.json"
        raw.unlink()
        raw.symlink_to("input.json")

        with self.assertRaisesRegex(compiler.PackCompilationError, "safely open"):
            self._compile()
        self.assertEqual(self._published_directories(), [])
        self.assertEqual(list((self.base / "store").glob(".offline-pack-*")), [])

    def test_source_mutation_during_copy_is_rejected(self) -> None:
        mutated = False

        def mutate(location: str) -> None:
            nonlocal mutated
            if not mutated and location.endswith("objects[0]"):
                mutated = True
                (self.external / "input.json").write_bytes(b"changed\n")

        with self.assertRaisesRegex(compiler.PackCompilationError, "changed while"):
            self._compile(after_copy=mutate)
        self.assertTrue(mutated)
        self.assertEqual(self._published_directories(), [])
        self.assertEqual(list((self.base / "store").glob(".offline-pack-*")), [])

    def test_wrong_planned_sizes_fail_before_object_payload_creation(self) -> None:
        mutable_size = copy.deepcopy(self.plan)
        mutable_size["sources"][1]["objects"][0]["bytes"] = 1_000_000_000
        _write_canonical(self.plan_path, mutable_size)
        with mock.patch.object(
            compiler,
            "_copy_stream_to_temp",
            wraps=compiler._copy_stream_to_temp,
        ) as stream_copy:
            with self.assertRaisesRegex(
                compiler.PackCompilationError,
                "source size .* before copy",
            ):
                self._compile()
        stream_copy.assert_not_called()
        self.assertEqual(list((self.base / "store").glob(".object-*")), [])
        self.assertEqual(list((self.base / "store").glob(".offline-pack-*")), [])

        git_size = copy.deepcopy(self.plan)
        git_size["sources"][0]["objects"][0]["bytes"] = 1_000_000_000
        _write_canonical(self.plan_path, git_size)
        real_open = os.open
        object_payloads: list[str] = []

        def recording_open(path: object, *args: object, **kwargs: object) -> int:
            if isinstance(path, (str, bytes, os.PathLike)):
                name = Path(os.fsdecode(path)).name
                if name.startswith(".object-"):
                    object_payloads.append(name)
            return real_open(path, *args, **kwargs)

        with mock.patch.object(compiler.os, "open", side_effect=recording_open):
            with self.assertRaisesRegex(
                compiler.PackCompilationError,
                "Git blob size .* before copy",
            ):
                self._compile()
        self.assertEqual(object_payloads, [])
        self.assertEqual(list((self.base / "store").glob(".offline-pack-*")), [])

    def test_staging_setup_failure_removes_new_directory(self) -> None:
        store = self.base / "setup-store"
        store.mkdir(mode=0o700)
        store.chmod(0o700)
        store_identity = compiler._validate_private_store(store)
        staging_name = ".offline-pack-setup-failure"

        with mock.patch.object(
            compiler.os,
            "fchmod",
            side_effect=PermissionError("fixture chmod failure"),
        ):
            store_descriptor = compiler._open_private_store(store, store_identity)
            try:
                with self.assertRaisesRegex(PermissionError, "fixture chmod failure"):
                    compiler._create_private_staging(
                        store_descriptor,
                        store_identity,
                        staging_name,
                    )
            finally:
                os.close(store_descriptor)
        self.assertFalse((store / staging_name).exists())

    def test_noncanonical_plan_bad_tree_and_writable_reuse_are_rejected(self) -> None:
        self.plan_path.write_text(json.dumps(self.plan, indent=2), encoding="utf-8")
        with self.assertRaisesRegex(compiler.PackCompilationError, "canonical"):
            self._compile()

        bad_plan = copy.deepcopy(self.plan)
        bad_plan["sources"][0]["pin"]["tree"] = "f" * 40
        _write_canonical(self.plan_path, bad_plan)
        with self.assertRaisesRegex(compiler.PackCompilationError, "expected .* found"):
            self._compile()

        _write_canonical(self.plan_path, self.plan)
        result = self._compile()
        object_path = next((result.root / "objects/sha256").iterdir())
        object_path.chmod(0o644)
        with self.assertRaisesRegex(compiler.PackCompilationError, "writable entry"):
            self._compile()


if __name__ == "__main__":
    unittest.main()
