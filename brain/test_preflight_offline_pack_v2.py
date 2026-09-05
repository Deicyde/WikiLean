#!/usr/bin/env python3
"""Tiny, network-free tests for the offline-pack/v2 source-plan preflight."""
from __future__ import annotations

import contextlib
import copy
import datetime as dt
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock


HERE = Path(__file__).resolve().parent
TOOLS = HERE / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import authority_contracts as contracts  # noqa: E402
import build_context  # noqa: E402
import compile_offline_pack_v2 as compiler  # noqa: E402
import execution_environment as environment  # noqa: E402
import preflight_offline_pack_v2 as preflight  # noqa: E402


ZERO_HASH = "sha256:" + "0" * 64
ZERO_DIGEST = "0" * 64
CURATED = b'{"curated":true}\n'
NORMALIZED = b'{"rows":[1]}\n'
RAW = b'{"raw":[1]}\n'
RECEIPT = b'{"complete":true}\n'


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


class OfflinePackPreflightTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.repo = self.base / "repo"
        self.external = self.base / "external"
        self.repo.mkdir()
        self.external.mkdir()
        for relative, data in {
            "brain/replay.py": b"print('fixture')\n",
            "data/curated.json": CURATED,
        }.items():
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.name", "WikiLean Test")
        _git(self.repo, "config", "user.email", "test@wikilean.invalid")
        _git(self.repo, "add", "--", "brain/replay.py", "data/curated.json")
        env = dict(os.environ)
        env.update(
            {
                "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+0000",
                "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+0000",
            }
        )
        _git(self.repo, "commit", "-q", "-m", "fixture", env=env)
        self.commit = _git(self.repo, "rev-parse", "HEAD")
        self.tree = _git(self.repo, "rev-parse", "HEAD^{tree}")

        (self.external / "input.json").write_bytes(NORMALIZED)
        (self.external / "raw.json").write_bytes(RAW)
        (self.external / "receipt.json").write_bytes(RECEIPT)
        self.configuration = {
            "cell_attach_kinds": ["generalization", "related"],
            "external_node_cap": 8,
            "layout": {"enabled": True, "iterations": 12},
            "schema": build_context.REDUCER_CONFIGURATION_SCHEMA,
        }
        self.environment = self._environment(self.commit)
        self.input_schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
        }
        _write_canonical(self.external / "configuration.json", self.configuration)
        _write_canonical(self.external / "environment.json", self.environment)
        _write_canonical(self.external / "schema.json", self.input_schema)

        self.inventory = self._inventory()
        self.inventory_path = self.base / "inventory.json"
        _write_canonical(self.inventory_path, self.inventory)
        self.plan = self._plan()
        self.plan_path = self.base / "plan.json"
        _write_canonical(self.plan_path, self.plan)
        self.store = self.base / "store"

    def tearDown(self) -> None:
        _make_writable(self.base)
        self.temporary.cleanup()

    @staticmethod
    def _environment(commit: str) -> dict[str, object]:
        value: dict[str, object] = {
            "dependency_lock": {
                "packages": [
                    {
                        "locked_artifact_sha256": "4" * 64,
                        "installed_tree_root": "sha256:" + "5" * 64,
                        "name": "numpy",
                        "version": "2.5.2",
                    }
                ],
                "schema": environment.DEPENDENCY_LOCK_SCHEMA,
            },
            "environment_id": ZERO_HASH,
            "locale": {
                "filesystem_encoding": "utf-8",
                "hash_sentinel": "123456789",
                "lang": "C.UTF-8",
                "lc_all": "C.UTF-8",
                "preferred_encoding": "utf-8",
                "python_hash_seed": "0",
                "timezone": "UTC",
                "utf8_mode": 1,
            },
            "profile": environment.AUTHORITATIVE_OCI_PROFILE,
            "python": {
                "cache_tag": "cpython-312",
                "executable_file_sha256": "3" * 64,
                "implementation": "CPython",
                "soabi": "cpython-312-linux-gnu",
                "version": "3.12.14",
            },
            "runner": {
                "files_root": "sha256:" + "2" * 64,
                "git_commit": commit,
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
                "compile_options": ["ENABLE_FTS5", "THREADSAFE=1"],
                "extension_file_sha256": "7" * 64,
                "source_id": "2030-01-02 03:04:05 " + "6" * 64,
                "version": "3.51.0",
            },
        }
        return environment.seal_execution_environment(value)

    def _inventory(self) -> dict[str, object]:
        value: dict[str, object] = {
            "boundary": "post-acquisition-fold",
            "forbidden_ambient": [
                {
                    "consumers": ["*"],
                    "name": "network access",
                    "replacement": "sealed inputs",
                }
            ],
            "inputs": [
                {
                    "cardinality": "one",
                    "class": "curated_git_input",
                    "consumers": ["brain/replay.py"],
                    "id": "curated",
                    "path": "data/curated.json",
                    "purpose": "curated fixture",
                    "requirement": "required",
                    "root": "repo",
                },
                {
                    "cardinality": "many",
                    "class": "immutable_source_object",
                    "consumers": ["brain/replay.py"],
                    "id": "optional-pages",
                    "path_pattern": "*_pages.jsonl",
                    "purpose": "absent fixture",
                    "requirement": "optional",
                    "root": "external",
                },
                {
                    "cardinality": "one",
                    "class": "immutable_source_object",
                    "consumers": ["brain/replay.py"],
                    "id": "source",
                    "path": "input.json",
                    "purpose": "normalized fixture",
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
            "scope": ["brain/replay.py"],
            "stages": [
                {
                    "argv": [],
                    "id": "replay",
                    "needs": [],
                    "outputs": [{"kind": "file", "path": "out/result.json"}],
                    "program": "brain/replay.py",
                }
            ],
        }
        value["inventory_id"] = contracts.reducer_input_inventory_identity(value)
        contracts.validate_reducer_input_inventory(value)
        return value

    @staticmethod
    def _ref(root: str, path: str, raw: bytes) -> dict[str, object]:
        return {
            "bytes": len(raw),
            "path": path,
            "root": root,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    def _plan(self) -> dict[str, object]:
        tool = {"name": "fixture", "sha256": ZERO_DIGEST, "version": "1"}
        configuration_raw = contracts.canonical_json_bytes(self.configuration)
        environment_raw = contracts.canonical_json_bytes(self.environment)
        schema_raw = contracts.canonical_json_bytes(self.input_schema)
        return {
            "configuration": self._ref(
                "external", "configuration.json", configuration_raw
            ),
            "environment": self._ref("external", "environment.json", environment_raw),
            "input_bindings": [
                {
                    "input_id": "curated",
                    "members": [
                        {
                            "object": "identity",
                            "path": "data/curated.json",
                            "source": "curated-fixture",
                        }
                    ],
                    "sources": ["curated-fixture"],
                    "state": "present",
                },
                {
                    "input_id": "optional-pages",
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
                    **self._ref("external", "schema.json", schema_raw),
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
                            "bytes": len(CURATED),
                            "media_type": "application/json",
                            "name": "identity",
                            "path": "data/curated.json",
                            "redistribution": "allowed",
                            "roles": ["normalized", "raw"],
                            "root": "repo",
                            "sha256": hashlib.sha256(CURATED).hexdigest(),
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
                    "audit": {
                        "acquired_at": "2030-01-01T00:00:00Z",
                        "upstream_uri": "https://example.invalid/fixture",
                    },
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
                            "bytes": len(NORMALIZED),
                            "media_type": "application/json",
                            "name": "normalized",
                            "path": "input.json",
                            "redistribution": "allowed",
                            "roles": ["normalized"],
                            "root": "external",
                            "sha256": hashlib.sha256(NORMALIZED).hexdigest(),
                        },
                        {
                            "bytes": len(RAW),
                            "media_type": "application/json",
                            "name": "raw",
                            "path": "raw.json",
                            "redistribution": "allowed",
                            "roles": ["raw"],
                            "root": "external",
                            "sha256": hashlib.sha256(RAW).hexdigest(),
                        },
                        {
                            "bytes": len(RECEIPT),
                            "media_type": "application/json",
                            "name": "receipt",
                            "path": "receipt.json",
                            "redistribution": "allowed",
                            "roles": ["receipt"],
                            "root": "external",
                            "sha256": hashlib.sha256(RECEIPT).hexdigest(),
                        },
                    ],
                    "pin": {
                        "type": "content_sha256",
                        "value": hashlib.sha256(RAW).hexdigest(),
                    },
                    "review": {
                        "expected_semantic_effects": [],
                        "summary": "fixture review",
                    },
                    "source": "external-fixture",
                    "source_kind": "acquired_dataset",
                },
            ],
        }

    def _write_plan(self) -> None:
        _write_canonical(self.plan_path, self.plan)

    def _run(self, **kwargs: object) -> dict[str, object]:
        return preflight.preflight_offline_pack_v2(
            self.plan_path,
            self.inventory_path,
            self.store,
            roots={"external": self.external, "repo": self.repo},
            as_of=dt.datetime(2030, 1, 2, tzinfo=dt.timezone.utc),
            **kwargs,
        )

    def test_reports_exact_members_sizes_and_capacity_without_hashing(self) -> None:
        expected_plan_sha = hashlib.sha256(self.plan_path.read_bytes()).hexdigest()
        original_read_bytes = Path.read_bytes
        read_paths: list[Path] = []

        def guarded_read_bytes(path: Path) -> bytes:
            resolved = path.resolve()
            read_paths.append(resolved)
            if resolved not in {self.plan_path.resolve(), self.inventory_path.resolve()}:
                raise AssertionError(f"unexpected Path.read_bytes corpus read: {resolved}")
            return original_read_bytes(path)

        real_run = subprocess.run
        with (
            mock.patch.object(Path, "read_bytes", new=guarded_read_bytes),
            mock.patch.object(
                contracts,
                "open_regular_file",
                wraps=contracts.open_regular_file,
            ) as open_regular,
            mock.patch.object(
                compiler,
                "_read_stable",
                wraps=compiler._read_stable,
            ) as read_stable,
            mock.patch.object(
                compiler,
                "_fingerprint_regular",
                side_effect=AssertionError("corpus fingerprinted"),
            ),
            mock.patch.object(contracts, "digest_file", side_effect=AssertionError("hashed")),
            mock.patch.object(preflight.subprocess, "run", wraps=real_run) as run,
        ):
            result = self._run()
        self.assertTrue(result["ok"])
        self.assertTrue(result["compile_ready"])
        self.assertFalse(result["source_authority_ready"])
        self.assertFalse(result["source_publishable"])
        self.assertEqual(result["readiness_scope"], "source-plan-only")
        self.assertFalse(result["runtime_environment_checked"])
        self.assertEqual(result["source_plan_sha256"], expected_plan_sha)
        self.assertEqual(result["summary"]["inputs_total"], 3)
        self.assertEqual(result["summary"]["inputs_present"], 2)
        self.assertEqual(result["summary"]["inputs_absent"], 1)
        self.assertEqual(result["summary"]["required_present"], 2)
        self.assertEqual(result["summary"]["members"], 2)
        self.assertEqual(result["redistribution"]["allowed"]["objects"], 4)
        self.assertGreater(result["space"]["recommended_free_bytes"], 0)
        self.assertEqual(
            set(read_paths),
            {self.plan_path.resolve(), self.inventory_path.resolve()},
        )
        self.assertEqual(
            [call.args[1] for call in read_stable.call_args_list],
            ["configuration.json", "environment.json", "schema.json"],
        )
        self.assertEqual(
            [call.args[1] for call in open_regular.call_args_list],
            ["configuration.json", "environment.json", "schema.json"],
        )
        cat_file_calls = [
            call.args[0]
            for call in run.call_args_list
            if "cat-file" in call.args[0]
        ]
        self.assertTrue(cat_file_calls)
        self.assertTrue(
            all(
                any(argument.startswith("--batch-check=") for argument in command)
                for command in cat_file_calls
            )
        )

    def test_compile_ready_fixture_succeeds_in_tiny_compiler(self) -> None:
        report = self._run()
        self.assertTrue(report["compile_ready"])
        compiled = compiler.compile_offline_pack_v2(
            self.plan_path,
            self.inventory_path,
            self.base / "compile-store",
            roots={"external": self.external, "repo": self.repo},
        )
        self.assertTrue(compiled.manifest_path.is_file())

    def test_rejects_mutable_declared_size_mismatch(self) -> None:
        self.plan["sources"][1]["objects"][0]["bytes"] += 1
        self._write_plan()
        with self.assertRaisesRegex(preflight.PreflightError, "declared byte size"):
            self._run()

    def test_rejects_curated_git_declared_size_mismatch(self) -> None:
        self.plan["sources"][0]["objects"][0]["bytes"] += 1
        self._write_plan()
        with self.assertRaisesRegex(preflight.PreflightError, "declared byte size"):
            self._run()

    def test_rejects_environment_bound_to_another_runner_commit(self) -> None:
        other = self._environment("1" * 40)
        raw = contracts.canonical_json_bytes(other)
        (self.external / "environment.json").write_bytes(raw)
        self.plan["environment"] = self._ref("external", "environment.json", raw)
        self._write_plan()
        with self.assertRaisesRegex(
            preflight.PreflightError, "must equal the reducer Git commit"
        ):
            self._run()

    def test_hashes_small_control_files(self) -> None:
        changed = copy.deepcopy(self.configuration)
        changed["external_node_cap"] = 9
        raw = contracts.canonical_json_bytes(changed)
        self.assertEqual(len(raw), self.plan["configuration"]["bytes"])
        (self.external / "configuration.json").write_bytes(raw)
        with self.assertRaisesRegex(preflight.PreflightError, "bytes do not match"):
            self._run()

    def test_rejects_noncanonical_or_nonobject_schema_control(self) -> None:
        raw = b'{ "type": "object" }\n'
        (self.external / "schema.json").write_bytes(raw)
        self.plan["schemas"][0].update(self._ref("external", "schema.json", raw))
        self._write_plan()
        with self.assertRaisesRegex(preflight.PreflightError, "canonical-json-v1"):
            self._run()

        raw = b"[]"
        (self.external / "schema.json").write_bytes(raw)
        self.plan["schemas"][0].update(self._ref("external", "schema.json", raw))
        self._write_plan()
        with self.assertRaisesRegex(preflight.PreflightError, "JSON object"):
            self._run()

        raw = contracts.canonical_json_bytes({"type": 17})
        (self.external / "schema.json").write_bytes(raw)
        self.plan["schemas"][0].update(self._ref("external", "schema.json", raw))
        self._write_plan()
        with self.assertRaisesRegex(
            preflight.PreflightError, "invalid Draft 2020-12 JSON Schema"
        ):
            self._run()

    def test_native_pin_warning_blocks_authority_not_compilation(self) -> None:
        self.plan["sources"][1]["pin"] = {
            "type": "dataset_revision",
            "value": "fixture-r1",
        }
        self._write_plan()
        result = self._run()
        concern = next(
            item
            for item in result["concerns"]
            if item["code"] == "native-pin-not-locally-verifiable"
        )
        self.assertEqual(concern["blocks"], ["authority", "publication"])
        self.assertTrue(result["compile_ready"])
        self.assertFalse(result["source_authority_ready"])
        self.assertFalse(result["source_publishable"])

    def test_rejects_member_set_drift(self) -> None:
        (self.external / "surprise_pages.jsonl").write_bytes(b"{}\n")
        with self.assertRaisesRegex(preflight.PreflightError, "member set"):
            self._run()

    def test_wildcard_binding_accepts_members_from_distinct_sources(self) -> None:
        tool = {"name": "fixture", "sha256": ZERO_DIGEST, "version": "1"}
        members = []
        names = []
        for name in ("alpha", "beta"):
            page = f'{{"page":"{name}"}}\n'.encode()
            receipt = f'{{"source":"{name}"}}\n'.encode()
            page_path = f"{name}_pages.jsonl"
            receipt_path = f"{name}.receipt.json"
            (self.external / page_path).write_bytes(page)
            (self.external / receipt_path).write_bytes(receipt)
            source_name = f"{name}-pages"
            names.append(source_name)
            members.append(
                {"object": "page", "path": page_path, "source": source_name}
            )
            self.plan["sources"].append(
                {
                    "acquisition": copy.deepcopy(tool),
                    "audit": {
                        "acquired_at": "2030-01-01T00:00:00Z",
                        "upstream_uri": f"https://example.invalid/{name}",
                    },
                    "license": {
                        "expression": "CC0-1.0",
                        "redistribution": "allowed",
                    },
                    "normalization": {
                        "inputs": ["page"],
                        "outputs": ["page"],
                        "schema": "fixture/identity-v1",
                        "tool": copy.deepcopy(tool),
                    },
                    "objects": [
                        {
                            "bytes": len(page),
                            "media_type": "application/x-ndjson",
                            "name": "page",
                            "path": page_path,
                            "redistribution": "allowed",
                            "roles": ["normalized", "raw"],
                            "root": "external",
                            "sha256": hashlib.sha256(page).hexdigest(),
                        },
                        {
                            "bytes": len(receipt),
                            "media_type": "application/json",
                            "name": "receipt",
                            "path": receipt_path,
                            "redistribution": "allowed",
                            "roles": ["receipt"],
                            "root": "external",
                            "sha256": hashlib.sha256(receipt).hexdigest(),
                        },
                    ],
                    "pin": {
                        "type": "content_sha256",
                        "value": hashlib.sha256(page).hexdigest(),
                    },
                    "review": {
                        "expected_semantic_effects": [],
                        "summary": "fixture review",
                    },
                    "source": source_name,
                    "source_kind": "acquired_dataset",
                }
            )
        self.plan["sources"].sort(key=lambda source: source["source"])
        wildcard = self.plan["input_bindings"][1]
        wildcard["members"] = members
        wildcard["sources"] = names
        wildcard["state"] = "present"
        self._write_plan()

        result = self._run()
        report = next(
            item for item in result["inputs"] if item["input_id"] == "optional-pages"
        )
        self.assertEqual(report["members"], 2)
        self.assertEqual(report["state"], "present")

    def test_rejects_required_absence(self) -> None:
        (self.external / "input.json").rename(self.external / "normalized-source.json")
        self.plan["sources"][1]["objects"][0]["path"] = "normalized-source.json"
        binding = self.plan["input_bindings"][2]
        binding["members"] = []
        binding["sources"] = ["external-fixture"]
        binding["state"] = "absent"
        self._write_plan()
        with self.assertRaisesRegex(preflight.PreflightError, "required input is absent"):
            self._run()

    def test_reports_stale_source_as_a_warning(self) -> None:
        self.plan["sources"][1]["audit"]["acquired_at"] = "2029-01-01T00:00:00Z"
        self._write_plan()
        result = self._run(max_age_days=30)
        stale = [item for item in result["concerns"] if item["code"] == "stale-source"]
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["severity"], "warning")
        self.assertEqual(stale[0]["blocks"], ["authority", "publication"])
        self.assertTrue(result["compile_ready"])
        self.assertFalse(result["source_authority_ready"])
        self.assertFalse(result["source_publishable"])

    def test_space_estimate_includes_largest_duplicate_object_temporary(self) -> None:
        duplicate = copy.deepcopy(self.plan["sources"][1])
        duplicate["source"] = "duplicate-fixture"
        duplicate["objects"] = [
            item for item in duplicate["objects"] if item["name"] == "normalized"
        ]
        duplicate["objects"][0]["name"] = "identity"
        duplicate["objects"][0]["roles"] = ["normalized", "raw"]
        duplicate["normalization"] = {
            "inputs": ["identity"],
            "outputs": ["identity"],
            "schema": "fixture/identity-v1",
            "tool": copy.deepcopy(duplicate["normalization"]["tool"]),
        }
        duplicate["pin"] = {
            "type": "content_sha256",
            "value": hashlib.sha256(NORMALIZED).hexdigest(),
        }
        self.plan["sources"].append(duplicate)
        self.plan["sources"].sort(key=lambda source: source["source"])
        self._write_plan()
        result = self._run()
        self.assertEqual(
            result["space"]["largest_duplicate_temp_bytes"], len(NORMALIZED)
        )

    def test_reports_provenance_license_freshness_and_space_blockers(self) -> None:
        source = self.plan["sources"][1]
        del source["audit"]
        source["pin"] = {"type": "http_etag", "value": "opaque-etag"}
        source["license"] = {
            "expression": "unknown - verify",
            "redistribution": "unknown",
        }
        source["objects"] = [
            item for item in source["objects"] if "receipt" not in item["roles"]
        ]
        for item in source["objects"]:
            item["redistribution"] = "unknown"
        self._write_plan()
        usage = namedtuple("usage", "total used free")(100, 100, 0)
        with mock.patch.object(preflight.shutil, "disk_usage", return_value=usage):
            result = self._run()
        codes = {item["code"] for item in result["concerns"]}
        self.assertFalse(result["compile_ready"])
        self.assertFalse(result["source_authority_ready"])
        self.assertFalse(result["source_publishable"])
        self.assertTrue(
            {
                "insufficient-output-space",
                "license-expression-needs-review",
                "missing-acquisition-receipt",
                "missing-normalization-lineage",
                "missing-source-audit",
                "unknown-object-license",
                "unknown-source-license",
                "weak-http-etag-pin",
            }.issubset(codes)
        )

    def test_cli_emits_canonical_json_and_structural_errors_exit_nonzero(self) -> None:
        args = [
            "--plan",
            str(self.plan_path),
            "--inventory",
            str(self.inventory_path),
            "--output-store",
            str(self.store),
            "--root",
            f"external={self.external}",
            "--root",
            f"repo={self.repo}",
            "--as-of",
            "2030-01-02T00:00:00Z",
        ]
        stdout = io.BytesIO()
        with mock.patch.object(sys, "stdout", mock.Mock(buffer=stdout)):
            self.assertEqual(preflight.main(args), 2)
        raw = stdout.getvalue().removesuffix(b"\n")
        document = json.loads(raw)
        self.assertEqual(raw, contracts.canonical_json_bytes(document))

        warning_only = copy.deepcopy(document)
        warning_only["concerns"] = [
            {
                "blocks": ["authority", "publication"],
                "code": "fixture-warning",
                "location": "$.sources[0]",
                "message": "source evidence requires review",
                "severity": "warning",
            }
        ]
        warning_only["source_authority_ready"] = False
        warning_only["source_publishable"] = False
        warning_only["summary"]["blockers"] = 0
        warning_only["summary"]["warnings"] = 1
        warning_stdout = io.BytesIO()
        with (
            mock.patch.object(
                preflight,
                "preflight_offline_pack_v2",
                return_value=warning_only,
            ),
            mock.patch.object(sys, "stdout", mock.Mock(buffer=warning_stdout)),
        ):
            self.assertEqual(preflight.main(args), 2)
        warning_raw = warning_stdout.getvalue().removesuffix(b"\n")
        self.assertEqual(
            warning_raw,
            contracts.canonical_json_bytes(json.loads(warning_raw)),
        )

        stderr = io.BytesIO()
        bad_args = [
            "--plan",
            str(self.plan_path),
            "--inventory",
            str(self.inventory_path),
            "--output-store",
            str(self.store),
            "--root",
            f"external={self.external}",
            "--as-of",
            "2030-01-02T00:00:00Z",
        ]
        with mock.patch.object(sys, "stderr", mock.Mock(buffer=stderr)):
            self.assertEqual(preflight.main(bad_args), 1)
        error_raw = stderr.getvalue().removesuffix(b"\n")
        self.assertEqual(error_raw, contracts.canonical_json_bytes(json.loads(error_raw)))

        argument_stderr = io.BytesIO()
        with mock.patch.object(sys, "stderr", mock.Mock(buffer=argument_stderr)):
            self.assertEqual(preflight.main(["--plan"]), 1)
        argument_raw = argument_stderr.getvalue().removesuffix(b"\n")
        self.assertEqual(
            argument_raw,
            contracts.canonical_json_bytes(json.loads(argument_raw)),
        )


if __name__ == "__main__":
    unittest.main()
