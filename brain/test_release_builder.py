#!/usr/bin/env python3
"""Focused tests for immutable Brain release assembly."""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
TOOLS = HERE / "tools"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(HERE))

import authority_contracts as contracts  # noqa: E402
import build_release  # noqa: E402
from test_authority_contracts import GIT_COMMIT, ReleaseVerificationTest  # noqa: E402


class ReleaseBuilderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        fixture = ReleaseVerificationTest(methodName="runTest")
        fixture.temp = None
        fixture.root = self.repo
        fixture.write_release_artifacts()
        inventory = {
            "schema": "wikilean.reducer-input-inventory/v1",
            "scope": ["fixture"],
            "classes": {},
            "inputs": [
                {
                    "path": "catalog/data/source_registry.json",
                    "class": "curated_git_input",
                    "consumers": ["fixture"],
                    "purpose": "fixture",
                },
                {
                    "path": "missing-optional.json",
                    "class": "immutable_source_object",
                    "consumers": ["fixture"],
                    "purpose": "fixture absence",
                },
                {
                    "path_pattern": "site/assets/brain/cells/traces/*.json",
                    "class": "immutable_source_object",
                    "consumers": ["fixture"],
                    "purpose": "fixture glob",
                },
                {
                    "name": "network access",
                    "class": "forbidden_ambient_state",
                    "consumers": ["fixture"],
                    "replacement": "offline fixture",
                },
            ],
        }
        inventory_path = self.repo / "brain/authority/reducer-inputs-v1.json"
        inventory_path.parent.mkdir(parents=True)
        inventory_path.write_bytes(contracts.canonical_json_bytes(inventory))
        self.output = Path(self.temp.name) / "releases"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def config(self, **changes: object) -> build_release.BuildConfig:
        values: dict[str, object] = {
            "repo_root": self.repo,
            "output_store": self.output,
            "semantic_epoch": "brain-v3-current",
            "schedule": "brain-v3-current",
            "reducer_version": "1",
            "authority_git_commit": GIT_COMMIT,
            "reducer_git_commit": GIT_COMMIT,
            "configuration_sha256": "2" * 64,
            "environment_sha256": "3" * 64,
            "compatible_overlay_generation_ids": ("overlay-fixture",),
        }
        values.update(changes)
        return build_release.BuildConfig(**values)  # type: ignore[arg-type]

    def test_builds_verified_frozen_release_and_reuses_identical_bytes(self) -> None:
        config = self.config(reducer_version="reducer-fixture-v7")
        first = build_release.build_release(config)
        root = Path(first["root"])
        self.assertEqual(root.name, first["release"])
        self.assertEqual(first["release_id"], f"sha256:{first['release']}")
        self.assertEqual(first["manifest"], str(root / "release.json"))
        self.assertFalse(first["reused"])

        manifest, raw = contracts.load_canonical_json(root / "release.json")
        self.assertEqual(raw, contracts.canonical_json_bytes(manifest))
        self.assertIsNone(manifest["authority"]["through_changeset"])
        build_attestation, _ = contracts.load_canonical_json(root / "attestations/build.json")
        self.assertEqual(build_attestation["builder"]["name"], build_release.BUILDER_NAME)
        self.assertEqual(build_attestation["builder"]["version"], build_release.BUILDER_VERSION)
        self.assertEqual(manifest["reducer"]["version"], "reducer-fixture-v7")
        self.assertEqual(
            contracts.verify_release_files(contracts.validate_release_manifest(manifest), root),
            {"artifacts": first["artifact_count"], "attestations": 2},
        )
        before = {path.relative_to(root).as_posix(): path.stat().st_mtime_ns for path in root.rglob("*") if path.is_file()}
        second = build_release.build_release(config)
        after = {path.relative_to(root).as_posix(): path.stat().st_mtime_ns for path in root.rglob("*") if path.is_file()}
        self.assertTrue(second["reused"])
        self.assertEqual(first["release_id"], second["release_id"])
        self.assertEqual(before, after)

    def test_fsyncs_every_release_directory_before_publish(self) -> None:
        synced_directories: set[tuple[int, int]] = set()
        real_fsync = os.fsync

        def observed_fsync(descriptor: int) -> None:
            descriptor_stat = os.fstat(descriptor)
            if stat.S_ISDIR(descriptor_stat.st_mode):
                synced_directories.add(
                    (descriptor_stat.st_dev, descriptor_stat.st_ino)
                )
            real_fsync(descriptor)

        with mock.patch.object(
            build_release.os,
            "fsync",
            side_effect=observed_fsync,
        ):
            result = build_release.build_release(self.config())

        root = Path(result["root"])
        release_directories = [root, *(path for path in root.rglob("*") if path.is_dir())]
        missing = [
            path.relative_to(root).as_posix() or "."
            for path in release_directories
            if (path.stat().st_dev, path.stat().st_ino) not in synced_directories
        ]
        self.assertEqual(missing, [])

    def test_hashes_frozen_bytes_not_later_source_changes(self) -> None:
        first = build_release.build_release(self.config())
        source = self.repo / "site/out/brain.html"
        source.write_bytes(b"changed after release")
        frozen = Path(first["root"]) / "site/out/brain.html"
        self.assertNotEqual(source.read_bytes(), frozen.read_bytes())
        manifest, _ = contracts.load_canonical_json(Path(first["manifest"]))
        contracts.verify_release_files(contracts.validate_release_manifest(manifest), Path(first["root"]))

    def test_rejects_source_mutation_during_copy_without_publishing(self) -> None:
        mutated = False

        def mutate(relative: str) -> None:
            nonlocal mutated
            if not mutated and relative == "site/out/brain.html":
                path = self.repo / relative
                path.write_bytes(path.read_bytes() + b" mutation")
                mutated = True

        with self.assertRaisesRegex(contracts.VerificationError, "changed while freezing"):
            build_release.build_release(self.config(), after_copy=mutate)
        self.assertTrue(mutated)
        if self.output.exists():
            self.assertEqual(list(self.output.iterdir()), [])

    def test_rejects_declared_input_mutation_across_freeze(self) -> None:
        mutated = False

        def mutate(relative: str) -> None:
            nonlocal mutated
            if not mutated and relative == "site/out/brain.html":
                source = self.repo / "catalog/data/source_registry.json"
                source.write_bytes(source.read_bytes() + b" ")
                mutated = True

        with self.assertRaisesRegex(
            contracts.VerificationError,
            "declared reducer inputs changed while freezing",
        ):
            build_release.build_release(self.config(), after_copy=mutate)
        self.assertTrue(mutated)
        if self.output.exists():
            self.assertEqual(list(self.output.iterdir()), [])

    def test_rejects_absolute_inventory_and_unsorted_or_duplicate_overlays(self) -> None:
        with self.assertRaisesRegex(contracts.VerificationError, "normalized, relative"):
            build_release.build_release(
                self.config(input_inventory=str(self.repo / "brain/authority/reducer-inputs-v1.json"))
            )
        for overlays in (("z", "a"), ("same", "same")):
            with self.subTest(overlays=overlays):
                with self.assertRaisesRegex(contracts.VerificationError, "unique and sorted"):
                    build_release.build_release(
                        self.config(compatible_overlay_generation_ids=overlays)
                    )

    def test_rejects_symlink_and_unreferenced_static_file(self) -> None:
        target = self.repo / "site/out/brain-real.html"
        page = self.repo / "site/out/brain.html"
        page.rename(target)
        try:
            os.symlink(target, page)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable")
        with self.assertRaisesRegex(contracts.VerificationError, "cannot safely open source"):
            build_release.build_release(self.config())
        page.unlink()
        target.rename(page)

        stale = self.repo / "site/assets/brain/cells/stale.json"
        stale.write_text("{}")
        with self.assertRaisesRegex(contracts.VerificationError, "unreferenced"):
            build_release.build_release(self.config())

    def test_corrupt_existing_release_is_never_overwritten(self) -> None:
        first = build_release.build_release(self.config())
        release_root = Path(first["root"])
        page = release_root / "site/out/brain.html"
        page.write_bytes(b"corrupt")
        corrupt = page.read_bytes()
        with self.assertRaises(contracts.VerificationError):
            build_release.build_release(self.config())
        self.assertEqual(page.read_bytes(), corrupt)
        self.assertEqual({path.name for path in self.output.iterdir()}, {first["release"]})

    def test_failure_preserves_an_existing_other_release(self) -> None:
        self.output.mkdir()
        prior = self.output / ("e" * 64)
        prior.mkdir()
        marker = prior / "marker"
        marker.write_bytes(b"prior")
        (self.repo / "brain/data/nodes.jsonl").unlink()
        with self.assertRaises(contracts.VerificationError):
            build_release.build_release(self.config())
        self.assertEqual(marker.read_bytes(), b"prior")
        self.assertEqual({path.name for path in self.output.iterdir()}, {prior.name})

    def test_cli_emits_one_machine_readable_result(self) -> None:
        arguments = [
            "--repo-root", str(self.repo),
            "--output-store", str(self.output),
            "--semantic-epoch", "brain-v3-current",
            "--schedule", "brain-v3-current",
            "--reducer-version", "1",
            "--authority-git-commit", GIT_COMMIT,
            "--reducer-git-commit", GIT_COMMIT,
            "--configuration-sha256", "2" * 64,
            "--environment-sha256", "3" * 64,
            "--compatible-overlay-generation-id", "overlay-fixture",
        ]
        original_stdout = sys.stdout
        from io import StringIO
        stdout = StringIO()
        try:
            sys.stdout = stdout
            code = build_release.main(arguments)
        finally:
            sys.stdout = original_stdout
        self.assertEqual(code, 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["release_id"], f"sha256:{result['release']}")
        self.assertTrue(Path(result["manifest"]).is_file())


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    )
    raise SystemExit(0 if result.wasSuccessful() else 1)
