#!/usr/bin/env python3
"""Focused tests for the execution-environment/v1 identity contract."""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
TOOLS = HERE / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import execution_environment as environment  # noqa: E402


ZERO_HASH = "sha256:" + "0" * 64


def valid_environment(
    profile: str = environment.DEVELOPMENT_HOST_PROFILE,
) -> dict[str, object]:
    if profile == environment.AUTHORITATIVE_OCI_PROFILE:
        runtime: dict[str, object] = {
            "kind": "oci-image",
            "os": "linux",
            "architecture": "x86_64",
            "image_digest": "sha256:" + "1" * 64,
        }
    else:
        runtime = {
            "kind": "development-host",
            "os": "darwin",
            "architecture": "arm64",
            "runtime_root": "sha256:" + "1" * 64,
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
            "executable_sha256": "3" * 64,
        },
        "dependency_lock": {
            "schema": environment.DEPENDENCY_LOCK_SCHEMA,
            "packages": [
                {
                    "name": "numpy",
                    "version": "2.3.2",
                    "artifact_sha256": "4" * 64,
                    "installed_files_root": "sha256:" + "5" * 64,
                }
            ],
        },
        "sqlite": {
            "version": "3.50.4",
            "source_id": "2030-01-02 03:04:05 " + "6" * 64,
            "binary_sha256": "7" * 64,
            "compile_options": ["ENABLE_FTS5", "THREADSAFE=1"],
        },
        "locale": {
            "lang": "C.UTF-8",
            "lc_all": "C.UTF-8",
            "timezone": "UTC",
            "preferred_encoding": "utf-8",
            "filesystem_encoding": "utf-8",
            "utf8_mode": 0,
            "python_hash_seed": "0",
        },
        "sandbox": {
            "backend": (
                "linux-bubblewrap"
                if profile == environment.AUTHORITATIVE_OCI_PROFILE
                else "darwin-sandbox-exec"
            ),
            "version": "0.11.0",
            "executable_sha256": "8" * 64,
            "policy_id": "brain-replay-v1",
            "policy_sha256": "9" * 64,
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
            ("sandbox", lambda value: value["sandbox"].update(version="main")),
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
            "artifact_sha256": "a" * 64,
            "installed_files_root": "sha256:" + "b" * 64,
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

    def test_digests_profiles_locale_and_network_fail_closed(self) -> None:
        cases = (
            (
                "python digest",
                lambda value: value["python"].update(executable_sha256="A" * 64),
                "lowercase 64-hex",
            ),
            (
                "runner root",
                lambda value: value["runner"].update(files_root="2" * 64),
                "sha256:<64-hex>",
            ),
            (
                "sqlite digest",
                lambda value: value["sqlite"].update(binary_sha256="short"),
                "lowercase 64-hex",
            ),
            (
                "sandbox policy",
                lambda value: value["sandbox"].update(policy_sha256="9" * 63),
                "lowercase 64-hex",
            ),
            (
                "locale",
                lambda value: value["locale"].update(lang="C"),
                "expected 'C.UTF-8'",
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
            "image_digest": "sha256:" + "1" * 64,
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
