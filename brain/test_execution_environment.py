#!/usr/bin/env python3
"""Focused tests for the execution-environment/v1 identity contract."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


HERE = Path(__file__).resolve().parent
TOOLS = HERE / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import execution_environment as environment  # noqa: E402
import probe_execution_environment as probe_program  # noqa: E402


ZERO_HASH = "sha256:" + "0" * 64
SCHEMA_PATH = HERE / "authority" / "schemas" / "execution-environment" / "v1.json"


def valid_environment(
    profile: str = environment.DEVELOPMENT_HOST_PROFILE,
) -> dict[str, object]:
    if profile == environment.AUTHORITATIVE_OCI_PROFILE:
        runtime: dict[str, object] = {
            "kind": "oci-image",
            "os": "linux",
            "architecture": "x86_64",
            "manifest_digest": "sha256:" + "1" * 64,
        }
    else:
        runtime = {
            "kind": "development-host",
            "os": "darwin",
            "architecture": "arm64",
            "host_fingerprint": "sha256:" + "1" * 64,
        }
    value: dict[str, object] = {
        "schema": environment.EXECUTION_ENVIRONMENT_SCHEMA,
        "environment_id": ZERO_HASH,
        "profile": profile,
        "runtime": runtime,
        "runner": {
            "name": "wikilean-replay",
            "version": "2.0.0",
            "git_commit": "a" * 40,
            "files_root": "sha256:" + "2" * 64,
        },
        "python": {
            "implementation": "CPython",
            "version": "3.12.11",
            "cache_tag": "cpython-312",
            "soabi": "cpython-312-darwin",
            "executable_file_sha256": "3" * 64,
        },
        "dependency_lock": {
            "schema": environment.DEPENDENCY_LOCK_SCHEMA,
            "packages": [
                {
                    "name": "numpy",
                    "version": "2.3.2",
                    "locked_artifact_sha256": "4" * 64,
                    "installed_tree_root": "sha256:" + "5" * 64,
                }
            ],
        },
        "sqlite": {
            "version": "3.50.4",
            "source_id": "2030-01-02 03:04:05 " + "6" * 64,
            "extension_file_sha256": "7" * 64,
            "compile_options": ["ENABLE_FTS5", "THREADSAFE=1"],
        },
        "locale": {
            "lang": "C.UTF-8",
            "lc_all": "C.UTF-8",
            "timezone": "UTC",
            "preferred_encoding": "utf-8",
            "filesystem_encoding": "utf-8",
            "utf8_mode": 1,
            "python_hash_seed": "0",
            "hash_sentinel": "123456789",
        },
        "sandbox": {
            "backend": (
                "linux-bubblewrap"
                if profile == environment.AUTHORITATIVE_OCI_PROFILE
                else "darwin-sandbox-exec"
            ),
            "reported_version": (
                "0.11.0"
                if profile == environment.AUTHORITATIVE_OCI_PROFILE
                else None
            ),
            "executable_sha256": "8" * 64,
            "policy_id": "brain-replay-v1",
            "policy_root": "sha256:" + "9" * 64,
            "network": "disabled",
        },
    }
    return environment.seal_execution_environment(value)


def resign(value: dict[str, object]) -> dict[str, object]:
    value["environment_id"] = environment.execution_environment_identity(value)
    return value


class ExecutionEnvironmentTest(unittest.TestCase):
    def assert_rejected(
        self,
        value: dict[str, object],
        pattern: str,
    ) -> None:
        resign(value)
        with self.assertRaisesRegex(environment.ExecutionEnvironmentError, pattern):
            environment.validate_execution_environment(value)

    def test_development_and_authoritative_profiles_are_distinct_and_valid(self) -> None:
        development = valid_environment()
        authoritative = valid_environment(environment.AUTHORITATIVE_OCI_PROFILE)
        self.assertIs(
            environment.validate_execution_environment(development), development
        )
        self.assertIs(
            environment.validate_execution_environment(authoritative), authoritative
        )
        self.assertNotEqual(
            development["environment_id"], authoritative["environment_id"]
        )
        self.assertEqual(authoritative["runtime"]["kind"], "oci-image")
        self.assertEqual(authoritative["sandbox"]["backend"], "linux-bubblewrap")

    def test_identity_is_canonical_self_derived_and_relocation_independent(self) -> None:
        value = valid_environment()
        first = value["environment_id"]
        value["environment_id"] = "sha256:" + "f" * 64
        self.assertEqual(environment.execution_environment_identity(value), first)
        value["environment_id"] = first

        reordered = json.loads(
            json.dumps(value, sort_keys=False), object_pairs_hook=lambda pairs: dict(reversed(pairs))
        )
        self.assertEqual(environment.execution_environment_identity(reordered), first)
        self.assertEqual(
            environment.canonical_json_bytes(value),
            environment.canonical_json_bytes(reordered),
        )

        changed = copy.deepcopy(value)
        changed["runner"]["version"] = "2.0.1"
        self.assertNotEqual(environment.execution_environment_identity(changed), first)

        with_path = copy.deepcopy(value)
        with_path["python"]["executable_path"] = "/host/bin/python"
        self.assert_rejected(with_path, "unknown keys: executable_path")

        wrong_id = copy.deepcopy(value)
        wrong_id["environment_id"] = ZERO_HASH
        with self.assertRaisesRegex(
            environment.ExecutionEnvironmentError, "environment_id: expected sha256:"
        ):
            environment.validate_execution_environment(wrong_id)

    def test_every_version_is_exact(self) -> None:
        mutations = (
            ("runner", lambda value: value["runner"].update(version=">=2.0")),
            ("python", lambda value: value["python"].update(version="3.12.*")),
            (
                "numpy",
                lambda value: value["dependency_lock"]["packages"][0].update(
                    version="latest"
                ),
            ),
            ("sqlite", lambda value: value["sqlite"].update(version="3.x")),
            (
                "sandbox",
                lambda value: value["sandbox"].update(reported_version="main"),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                value = valid_environment()
                mutate(value)
                self.assert_rejected(value, "exact version label")

    def test_dependency_lock_is_sorted_unique_and_exactly_numpy(self) -> None:
        numpy = valid_environment()["dependency_lock"]["packages"][0]
        scipy = {
            "name": "scipy",
            "version": "1.16.1",
            "locked_artifact_sha256": "a" * 64,
            "installed_tree_root": "sha256:" + "b" * 64,
        }

        duplicate = valid_environment()
        duplicate["dependency_lock"]["packages"] = [numpy, copy.deepcopy(numpy)]
        self.assert_rejected(duplicate, "package names must be unique")

        unsorted = valid_environment()
        unsorted["dependency_lock"]["packages"] = [scipy, numpy]
        self.assert_rejected(unsorted, "sorted by name")

        extra = valid_environment()
        extra["dependency_lock"]["packages"] = [numpy, scipy]
        self.assert_rejected(extra, "exactly the numpy runtime dependency")

        missing = valid_environment()
        missing["dependency_lock"]["packages"] = []
        self.assert_rejected(missing, "non-empty array")

    def test_sqlite_compile_options_are_sorted_unique_and_exact(self) -> None:
        unsorted = valid_environment()
        unsorted["sqlite"]["compile_options"] = ["THREADSAFE=1", "ENABLE_FTS5"]
        self.assert_rejected(unsorted, "entries must be sorted")

        duplicate = valid_environment()
        duplicate["sqlite"]["compile_options"] = ["ENABLE_FTS5", "ENABLE_FTS5"]
        self.assert_rejected(duplicate, "entries must be unique")

        control = valid_environment()
        control["sqlite"]["compile_options"] = ["ENABLE_FTS5\n"]
        self.assert_rejected(control, "printable ASCII")

    def test_logical_file_roots_bind_paths_lengths_and_bytes_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            (first / "alpha.py").write_bytes(b"alpha\n")
            (first / "data.bin").write_bytes(b"\x00\x01")
            (second / "alpha.py").write_bytes(b"alpha\n")
            (second / "data.bin").write_bytes(b"\x00\x01")
            os.utime(first / "alpha.py", (1, 1))
            os.utime(second / "alpha.py", (2, 2))

            first_files = {
                "pkg/data.bin": first / "data.bin",
                "pkg/alpha.py": first / "alpha.py",
            }
            second_files = {
                "pkg/alpha.py": second / "alpha.py",
                "pkg/data.bin": second / "data.bin",
            }
            first_root = environment.runner_files_root(first_files)
            self.assertEqual(first_root, environment.runner_files_root(second_files))
            self.assertNotEqual(
                first_root,
                environment.numpy_installed_tree_root(first_files),
            )

            entries = environment.logical_file_set_entries(first_files)
            self.assertEqual(
                [entry["path"] for entry in entries],
                ["pkg/alpha.py", "pkg/data.bin"],
            )
            self.assertEqual(entries[0]["bytes"], 6)
            self.assertEqual(
                entries[0]["sha256"], hashlib.sha256(b"alpha\n").hexdigest()
            )

            (second / "alpha.py").write_bytes(b"bravo\n")
            self.assertNotEqual(first_root, environment.runner_files_root(second_files))
            self.assertNotEqual(
                first_root,
                environment.runner_files_root(
                    {"pkg/alpha.py": first / "alpha.py"}
                ),
            )

    def test_logical_file_set_rejects_symlinks_aliases_and_file_ancestors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            regular = root / "value"
            regular.write_bytes(b"value")
            link = root / "link"
            link.symlink_to(regular)
            with self.assertRaisesRegex(
                environment.ExecutionEnvironmentError, "securely open"
            ):
                environment.runner_files_root({"value": link})
            directory_link = root / "directory-link"
            directory_link.symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(
                environment.ExecutionEnvironmentError, "securely open"
            ):
                environment.runner_files_root(
                    {"value": directory_link / regular.name}
                )

            with self.assertRaisesRegex(
                environment.ExecutionEnvironmentError, "portable path alias"
            ):
                environment.runner_files_root(
                    {"Pkg/value": regular, "pkg/VALUE": regular}
                )
            with self.assertRaisesRegex(
                environment.ExecutionEnvironmentError, "file ancestor"
            ):
                environment.runner_files_root(
                    {"pkg": regular, "pkg/value": regular}
                )
            with self.assertRaisesRegex(
                environment.ExecutionEnvironmentError, "relative POSIX"
            ):
                environment.runner_files_root({"../value": regular})

    def test_secure_file_digest_streams_exact_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / "payload"
            payload = b"payload\x00bytes"
            path.write_bytes(payload)
            digest, byte_length = environment.secure_file_digest(path)
            self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
            self.assertEqual(byte_length, len(payload))

    def test_logical_file_root_recipe_has_a_golden_value(self) -> None:
        root = environment.runner_files_root(
            {"runner.py": "/unused"},
            file_digest=lambda _path: ("a" * 64, 7),
        )
        self.assertEqual(
            root,
            "sha256:9c3a02e4970b5b674c5a9b47d826292025e7372d2099563f48922236e6dd1bcd",
        )

    def test_python_and_numpy_projections_are_injectable(self) -> None:
        calls: list[object] = []

        def fake_digest(path: object) -> tuple[str, int]:
            calls.append(path)
            return "a" * 64, 17

        python = environment.probe_python_runtime(
            implementation="CPython",
            version="3.12.11",
            cache_tag="cpython-312",
            soabi="cpython-312-darwin",
            executable_path="/logical/python",
            file_digest=fake_digest,
        )
        self.assertEqual(python["executable_file_sha256"], "a" * 64)
        self.assertEqual(calls, ["/logical/python"])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            (first / "__init__.py").write_bytes(b"__version__ = '2.3.2'\n")
            (second / "__init__.py").write_bytes(b"__version__ = '2.3.2'\n")
            first_facts = environment.numpy_runtime_facts(
                version="2.3.2",
                installed_files={"numpy/__init__.py": first / "__init__.py"},
            )
            second_facts = environment.numpy_runtime_facts(
                version="2.3.2",
                installed_files={"numpy/__init__.py": second / "__init__.py"},
            )
            self.assertEqual(first_facts, second_facts)
            self.assertEqual(first_facts["name"], "numpy")
            self.assertTrue(first_facts["installed_tree_root"].startswith("sha256:"))

    def test_numpy_probe_scans_owned_trees_beyond_distribution_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            site = root / "site-packages"
            scripts = root / "bin"
            headers = root / "include"
            package = site / "numpy"
            libraries = site / "numpy.libs"
            dynamic_libraries = site / "numpy.dylibs"
            metadata = site / "numpy-2.3.2.dist-info"
            package.mkdir(parents=True)
            libraries.mkdir()
            dynamic_libraries.mkdir()
            metadata.mkdir()
            scripts.mkdir()
            headers.mkdir()
            init = package / "__init__.py"
            unrecorded = package / "unrecorded.py"
            library = libraries / "libnumpy.so"
            dynamic_library = dynamic_libraries / "libnumpy.dylib"
            metadata_file = metadata / "METADATA"
            record = metadata / "RECORD"
            script = scripts / "f2py"
            header = headers / "numpy.h"
            init.write_bytes(b"__version__ = '2.3.2'\n")
            unrecorded.write_bytes(b"first\n")
            library.write_bytes(b"shared library\n")
            dynamic_library.write_bytes(b"dynamic library\n")
            metadata_file.write_bytes(
                b"Metadata-Version: 2.1\nName: numpy\nVersion: 2.3.2\n"
            )
            record.write_bytes(b"fixture record\n")
            script.write_bytes(b"#!/bin/sh\n")
            header.write_bytes(b"fixture header\n")
            locations = {
                "numpy/__init__.py": init,
                "numpy-2.3.2.dist-info/METADATA": metadata_file,
                "numpy-2.3.2.dist-info/RECORD": record,
                "../../../bin/f2py": script,
                "../../../include/numpy.h": header,
            }

            class Distribution:
                version = "2.3.2"
                metadata = {"Name": "NumPy"}
                files = tuple(locations)

                @staticmethod
                def locate_file(path: object) -> Path:
                    return locations[str(path)]

            module = SimpleNamespace(__version__="2.3.2", __file__=str(init))
            scheme = {
                "purelib": str(site),
                "platlib": str(site),
                "scripts": str(scripts),
                "data": str(root),
            }
            discovered = environment.discover_numpy_installed_files(
                distribution=Distribution(),
                numpy_module=module,
                scheme_paths=scheme,
            )
            self.assertIn("site-packages/numpy/unrecorded.py", discovered)
            self.assertIn("site-packages/numpy.libs/libnumpy.so", discovered)
            self.assertIn(
                "site-packages/numpy.dylibs/libnumpy.dylib", discovered
            )
            self.assertIn(
                "site-packages/numpy-2.3.2.dist-info/METADATA", discovered
            )
            self.assertNotIn("scripts/f2py", discovered)
            self.assertNotIn("headers/numpy.h", discovered)
            first = environment.probe_numpy_runtime(
                distribution=Distribution(),
                numpy_module=module,
                scheme_paths=scheme,
            )
            script.write_bytes(b"#!/bin/sh\necho changed\n")
            header.write_bytes(b"changed header\n")
            outside_changed = environment.probe_numpy_runtime(
                distribution=Distribution(),
                numpy_module=module,
                scheme_paths=scheme,
            )
            self.assertEqual(
                first["installed_tree_root"],
                outside_changed["installed_tree_root"],
            )
            unrecorded.write_bytes(b"other\n")
            second = environment.probe_numpy_runtime(
                distribution=Distribution(),
                numpy_module=module,
                scheme_paths=scheme,
            )
            self.assertNotEqual(
                first["installed_tree_root"], second["installed_tree_root"]
            )

            mismatched_module = SimpleNamespace(
                __version__="2.3.1", __file__=str(init)
            )
            with self.assertRaisesRegex(
                environment.ExecutionEnvironmentError, "disagrees"
            ):
                environment.probe_numpy_runtime(
                    distribution=Distribution(),
                    numpy_module=mismatched_module,
                    scheme_paths=scheme,
                )

            original_walk = environment.os.walk

            def unreadable_walk(path: object, *, followlinks: bool, onerror: object):
                if Path(path) == package:
                    onerror(PermissionError("fixture denied"))
                    return iter(())
                return original_walk(path, followlinks=followlinks, onerror=onerror)

            with mock.patch.object(environment.os, "walk", unreadable_walk):
                with self.assertRaisesRegex(
                    environment.ExecutionEnvironmentError,
                    "cannot traverse NumPy installed tree",
                ):
                    environment.discover_numpy_installed_files(
                        distribution=Distribution(),
                        numpy_module=module,
                        scheme_paths=scheme,
                    )

    def test_sqlite_projection_uses_live_engine_and_extension_file(self) -> None:
        class Result:
            def __init__(self, one: object = None, many: object = None) -> None:
                self.one = one
                self.many = many

            def fetchone(self) -> object:
                return self.one

            def fetchall(self) -> object:
                return self.many

        class Connection:
            closed = False

            def execute(self, statement: str) -> Result:
                if statement.startswith("SELECT"):
                    return Result(("3.50.4", "2030-01-02 source-id"))
                return Result(many=[("THREADSAFE=1",), ("ENABLE_FTS5",)])

            def close(self) -> None:
                self.closed = True

        connection = Connection()
        sqlite = environment.probe_sqlite_runtime(
            sqlite_module=SimpleNamespace(sqlite_version="3.50.4"),
            extension_module=SimpleNamespace(__file__="/logical/_sqlite3.so"),
            connect=lambda _location: connection,
            file_digest=lambda _path: ("b" * 64, 123),
        )
        self.assertTrue(connection.closed)
        self.assertEqual(sqlite["extension_file_sha256"], "b" * 64)
        self.assertEqual(
            sqlite["compile_options"], ["ENABLE_FTS5", "THREADSAFE=1"]
        )

        with self.assertRaisesRegex(
            environment.ExecutionEnvironmentError, "disagrees"
        ):
            environment.probe_sqlite_runtime(
                sqlite_module=SimpleNamespace(sqlite_version="3.49.0"),
                extension_module=SimpleNamespace(__file__="/logical/_sqlite3.so"),
                connect=lambda _location: Connection(),
                file_digest=lambda _path: ("b" * 64, 123),
            )

    def test_host_policy_and_locale_projections_are_deterministic(self) -> None:
        first = environment.development_host_fingerprint(
            operating_system="darwin",
            architecture="arm64",
            kernel_release="24.6.0",
            libc_name="",
            libc_version="",
        )
        second = environment.development_host_fingerprint(
            operating_system="darwin",
            architecture="arm64",
            kernel_release="24.6.0",
            libc_name="",
            libc_version="",
        )
        changed = environment.development_host_fingerprint(
            operating_system="darwin",
            architecture="arm64",
            kernel_release="24.6.1",
            libc_name="",
            libc_version="",
        )
        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)
        self.assertEqual(
            environment.sandbox_policy_root(
                {"network": "disabled", "mounts": ["runtime", "workspace"]}
            ),
            environment.sandbox_policy_root(
                {"mounts": ["runtime", "workspace"], "network": "disabled"}
            ),
        )
        self.assertEqual(
            environment.probe_locale_runtime(
                environ={
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "TZ": "UTC",
                    "PYTHONHASHSEED": "0",
                },
                preferred_encoding="UTF-8",
                filesystem_encoding="utf-8",
                utf8_mode=1,
                hash_sentinel="123456789",
            ),
            {
                "lang": "C.UTF-8",
                "lc_all": "C.UTF-8",
                "timezone": "UTC",
                "preferred_encoding": "utf-8",
                "filesystem_encoding": "utf-8",
                "utf8_mode": 1,
                "python_hash_seed": "0",
                "hash_sentinel": "123456789",
            },
        )
        observed = environment.probe_locale_runtime(
            environ={
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "TZ": "UTC",
                "PYTHONHASHSEED": "0",
            },
            preferred_encoding="utf-8",
            filesystem_encoding="utf-8",
            utf8_mode=1,
        )
        self.assertEqual(
            observed["hash_sentinel"],
            str(hash(environment.HASH_SENTINEL_TEXT)),
        )

    def test_live_projection_excludes_unobservable_locked_artifact(self) -> None:
        first = valid_environment()
        second = copy.deepcopy(first)
        second["dependency_lock"]["packages"][0]["locked_artifact_sha256"] = (
            "f" * 64
        )
        resign(second)
        self.assertNotEqual(first["environment_id"], second["environment_id"])
        self.assertEqual(
            environment.live_environment_projection(first),
            environment.live_environment_projection(second),
        )
        projection = environment.live_environment_projection(first)
        self.assertNotIn("locked_artifact_sha256", projection["numpy"])
        calls: list[str] = []

        def injected(name: str, value: object):
            def probe() -> object:
                calls.append(name)
                return copy.deepcopy(value)

            return probe

        live = environment.probe_live_environment_projection(
            profile=first["profile"],
            runtime_probe=injected("runtime", projection["runtime"]),
            runner_files_probe=injected(
                "runner", projection["runner"]["files_root"]
            ),
            python_probe=injected("python", projection["python"]),
            numpy_probe=injected("numpy", projection["numpy"]),
            sqlite_probe=injected("sqlite", projection["sqlite"]),
            locale_probe=injected("locale", projection["locale"]),
            sandbox_probe=injected("sandbox", projection["sandbox"]),
        )
        self.assertEqual(live, projection)
        self.assertEqual(
            calls,
            ["runtime", "runner", "python", "numpy", "sqlite", "locale", "sandbox"],
        )

    def test_probe_document_and_trusted_runtime_evidence_are_strict(self) -> None:
        value = valid_environment()
        projection = environment.live_environment_projection(value)
        probe = {
            "schema": environment.LIVE_PROBE_SCHEMA,
            "python": projection["python"],
            "numpy": projection["numpy"],
            "sqlite": projection["sqlite"],
            "locale": projection["locale"],
        }
        self.assertIs(environment.validate_live_probe_document(probe), probe)
        with self.assertRaisesRegex(
            environment.ExecutionEnvironmentError, "unknown keys: extra"
        ):
            environment.validate_live_probe_document({**probe, "extra": True})

        authoritative = valid_environment(environment.AUTHORITATIVE_OCI_PROFILE)
        evidence = {
            "schema": environment.TRUSTED_RUNTIME_EVIDENCE_SCHEMA,
            "profile": environment.AUTHORITATIVE_OCI_PROFILE,
            "runtime": copy.deepcopy(authoritative["runtime"]),
        }
        self.assertIs(
            environment.validate_trusted_runtime_evidence(evidence), evidence
        )
        with self.assertRaisesRegex(
            environment.ExecutionEnvironmentError, "must be authoritative-oci"
        ):
            environment.validate_trusted_runtime_evidence(
                {**evidence, "profile": environment.DEVELOPMENT_HOST_PROFILE}
            )

    def test_probe_program_collects_only_the_strict_child_projection(self) -> None:
        value = valid_environment()
        projection = environment.live_environment_projection(value)
        document = probe_program.collect_probe_document(
            python_probe=lambda: projection["python"],
            numpy_probe=lambda: projection["numpy"],
            sqlite_probe=lambda: projection["sqlite"],
            locale_probe=lambda: projection["locale"],
        )
        self.assertEqual(document["schema"], environment.LIVE_PROBE_SCHEMA)
        self.assertEqual(set(document), {"schema", "python", "numpy", "sqlite", "locale"})

    def test_sandbox_reported_version_is_nullable_only_when_unavailable(self) -> None:
        development = valid_environment()
        self.assertIsNone(development["sandbox"]["reported_version"])
        environment.validate_execution_environment(development)

        authoritative = valid_environment(environment.AUTHORITATIVE_OCI_PROFILE)
        authoritative["sandbox"]["reported_version"] = None
        self.assert_rejected(authoritative, "may be null only")

    def test_schema_uses_the_live_binding_field_names(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertIn(
            "host_fingerprint", schema["$defs"]["developmentRuntime"]["required"]
        )
        self.assertIn(
            "manifest_digest", schema["$defs"]["ociRuntime"]["required"]
        )
        self.assertIn(
            "executable_file_sha256", schema["$defs"]["python"]["required"]
        )
        self.assertIn(
            "locked_artifact_sha256", schema["$defs"]["package"]["required"]
        )
        self.assertIn(
            "installed_tree_root", schema["$defs"]["package"]["required"]
        )
        self.assertIn(
            "extension_file_sha256", schema["$defs"]["sqlite"]["required"]
        )
        self.assertIn("reported_version", schema["$defs"]["sandbox"]["required"])
        self.assertIn("policy_root", schema["$defs"]["sandbox"]["required"])
        self.assertEqual(
            schema["$defs"]["locale"]["properties"]["utf8_mode"],
            {"const": 1},
        )
        self.assertIn("hash_sentinel", schema["$defs"]["locale"]["required"])

    def test_digests_profiles_locale_and_network_fail_closed(self) -> None:
        cases = (
            (
                "python digest",
                lambda value: value["python"].update(
                    executable_file_sha256="A" * 64
                ),
                "lowercase 64-hex",
            ),
            (
                "runner root",
                lambda value: value["runner"].update(files_root="2" * 64),
                "sha256:<64-hex>",
            ),
            (
                "sqlite digest",
                lambda value: value["sqlite"].update(
                    extension_file_sha256="short"
                ),
                "lowercase 64-hex",
            ),
            (
                "sandbox policy",
                lambda value: value["sandbox"].update(policy_root="9" * 64),
                "sha256:<64-hex>",
            ),
            (
                "locale",
                lambda value: value["locale"].update(lang="C"),
                "expected 'C.UTF-8'",
            ),
            (
                "utf8 mode",
                lambda value: value["locale"].update(utf8_mode=0),
                "expected integer 1",
            ),
            (
                "hash sentinel",
                lambda value: value["locale"].update(hash_sentinel="+1"),
                "canonical signed integer string",
            ),
            (
                "network",
                lambda value: value["sandbox"].update(network="enabled"),
                "network must be 'disabled'",
            ),
            (
                "backend",
                lambda value: value["sandbox"].update(backend="linux-bubblewrap"),
                "darwin runtime requires",
            ),
        )
        for label, mutate, pattern in cases:
            with self.subTest(label=label):
                value = valid_environment()
                mutate(value)
                self.assert_rejected(value, pattern)

        authoritative = valid_environment(environment.AUTHORITATIVE_OCI_PROFILE)
        authoritative["runtime"] = {
            "kind": "oci-image",
            "os": "darwin",
            "architecture": "arm64",
            "manifest_digest": "sha256:" + "1" * 64,
        }
        authoritative["sandbox"]["backend"] = "darwin-sandbox-exec"
        self.assert_rejected(authoritative, "currently requires Linux")

        unknown = valid_environment()
        unknown["created_at"] = "2030-01-01T00:00:00Z"
        self.assert_rejected(unknown, "unknown keys: created_at")

    def test_canonical_encoder_rejects_floats_and_non_nfc(self) -> None:
        with self.assertRaisesRegex(
            environment.ExecutionEnvironmentError, "floating-point"
        ):
            environment.canonical_json_bytes({"value": 1.0})
        with self.assertRaisesRegex(
            environment.ExecutionEnvironmentError, "Unicode NFC"
        ):
            environment.canonical_json_bytes({"value": "e\u0301"})


if __name__ == "__main__":
    unittest.main()
