#!/usr/bin/env python3
"""Focused tests for immutable Brain release assembly."""
from __future__ import annotations

import hashlib
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
import store  # noqa: E402
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
        self.assertEqual(root.name, first["manifest_sha256"])
        self.assertEqual(first["release_id"], f"sha256:{first['release']}")
        self.assertEqual(first["manifest"], str(root / "release.json"))
        self.assertFalse(first["reused"])

        manifest, raw = contracts.load_canonical_json(root / "release.json")
        self.assertEqual(raw, contracts.canonical_json_bytes(manifest))
        self.assertEqual(first["manifest_sha256"], hashlib.sha256(raw).hexdigest())
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

    def test_distinct_manifests_for_one_logical_release_coexist(self) -> None:
        first = build_release.build_release(
            self.config(recorded_at="2030-01-01T00:00:00Z")
        )
        second = build_release.build_release(
            self.config(recorded_at="2030-01-02T00:00:00Z")
        )
        self.assertEqual(first["release_id"], second["release_id"])
        self.assertNotEqual(first["manifest_sha256"], second["manifest_sha256"])
        self.assertNotEqual(first["root"], second["root"])
        self.assertTrue(Path(first["manifest"]).is_file())
        self.assertTrue(Path(second["manifest"]).is_file())

    def test_manifest_mutation_before_publication_cannot_claim_the_old_digest(self) -> None:
        def mutate_manifest() -> None:
            candidate = next(self.output.glob(".brain-release-*"))
            manifest = candidate / "release.json"
            manifest.write_bytes(manifest.read_bytes() + b" ")

        with self.assertRaisesRegex(contracts.VerificationError, "manifest digest"):
            build_release.build_release(
                self.config(),
                _before_publish=mutate_manifest,
            )
        self.assertEqual(list(self.output.iterdir()), [])

    def offline_inputs(self) -> build_release._VerifiedReplayInputs:
        generation = "sha256:" + "a" * 64
        for path in self.repo.rglob("*"):
            if path.suffix in {".json", ".jsonl"}:
                raw = path.read_bytes()
                raw = raw.replace(b"2030-01-01T00:00:00Z", generation.encode())
                raw = raw.replace(b"2030-01-01T00:01:00Z", generation.encode())
                path.write_bytes(raw)
        sqlite = self.repo / "brain/data/brain.sqlite3"
        sqlite.unlink()
        store.write_sqlite_from_jsonl(sqlite, self.repo / "brain/data")
        inputs = Path(self.temp.name) / "sealed-provenance"
        inputs.mkdir()
        sources = {}
        for relative in ("catalog/data/source_registry.json", "brain/data/community_edges.jsonl"):
            original = self.repo / relative
            source = inputs / original.name
            source.write_bytes(original.read_bytes())
            sources[relative] = (source, *contracts.digest_file(source))
            original.unlink()
        for relative in ("brain/data/cell_review.jsonl", "brain/data/frontier_review.jsonl"):
            (self.repo / relative).write_bytes(b'{}\n')
        output_paths = tuple(sorted(path.relative_to(self.repo).as_posix() for path in self.repo.rglob("*")
                                    if path.is_file() and "authority" not in path.parts))
        return build_release._VerifiedReplayInputs(
            source_set_root="sha256:" + "b" * 64,
            replay={"authority_root": "sha256:" + "c" * 64, "offline_pack_id": "sha256:" + "d" * 64,
                    "reducer_inventory_id": "sha256:" + "e" * 64, "prior_state_root": None,
                    "generation_id": generation},
            sources=sources, input_count=41, output_paths=output_paths)

    def test_offline_profile_freezes_all_outputs_and_exact_sealed_provenance_inputs(self) -> None:
        inputs = self.offline_inputs()
        result = build_release.build_release(self.config(), _verified_replay=inputs)
        root = Path(result["root"])
        manifest, _ = contracts.load_canonical_json(root / "release.json")
        self.assertEqual(manifest["profile"], contracts.OFFLINE_REPLAY_RELEASE_PROFILE)
        self.assertEqual(manifest["source_set_root"], inputs.source_set_root)
        self.assertEqual(manifest["replay"], inputs.replay)
        self.assertEqual({item["path"] for item in manifest["artifacts"]}, set(inputs.output_paths) | set(inputs.sources))
        build, _ = contracts.load_canonical_json(root / "attestations/build.json")
        self.assertEqual(build["schema"], contracts.BUILD_ATTESTATION_SCHEMA_V2)
        self.assertEqual(build["inputs"]["authority_root"], inputs.replay["authority_root"])
        self.assertNotIn("generation_id", build["inputs"])
        contracts.verify_release_files(contracts.validate_release_manifest(manifest), root)
        self.assertFalse((self.repo / "catalog/data/source_registry.json").exists())

    def test_offline_profile_rejects_forged_attestation_pack_binding(self) -> None:
        result = build_release.build_release(self.config(), _verified_replay=self.offline_inputs())
        root = Path(result["root"])
        manifest, _ = contracts.load_canonical_json(root / "release.json")
        path = root / "attestations/build.json"
        build, _ = contracts.load_canonical_json(path)
        build["inputs"]["offline_pack_id"] = "sha256:" + "f" * 64
        build["attestation_id"] = contracts.attestation_identity(build)
        path.write_bytes(contracts.canonical_json_bytes(build))
        ref = next(item for item in manifest["attestations"] if item["kind"] == "build")
        ref["sha256"], ref["bytes"] = contracts.digest_file(path)
        with self.assertRaisesRegex(contracts.VerificationError, "release replay binding"):
            contracts.verify_release_files(contracts.validate_release_manifest(manifest), root)

    def test_offline_profile_and_generation_are_part_of_release_identity(self) -> None:
        result = build_release.build_release(self.config(), _verified_replay=self.offline_inputs())
        root = Path(result["root"])
        manifest, _ = contracts.load_canonical_json(root / "release.json")
        old_id = manifest["release_id"]
        manifest["replay"]["generation_id"] = "sha256:" + "f" * 64
        manifest["release_id"] = contracts.release_identity(manifest)
        self.assertNotEqual(old_id, manifest["release_id"])
        with self.assertRaisesRegex(contracts.VerificationError, "artifact generation"):
            contracts.verify_release_files(contracts.validate_release_manifest(manifest), root)
        manifest["profile"] = contracts.RELEASE_PROFILE
        manifest["release_id"] = contracts.release_identity(manifest)
        with self.assertRaises(contracts.VerificationError):
            contracts.validate_release_manifest(manifest)

    def test_offline_provenance_input_mutation_cannot_publish(self) -> None:
        inputs = self.offline_inputs()
        source = inputs.sources["brain/data/community_edges.jsonl"][0]
        def mutate(relative):
            if relative == "site/out/brain.html":
                source.write_bytes(b'{}\n')
        with self.assertRaisesRegex(contracts.VerificationError, "provenance input changed"):
            build_release.build_release(self.config(), _verified_replay=inputs, after_copy=mutate)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_offline_final_source_check_runs_before_publication(self) -> None:
        def changed():
            raise contracts.VerificationError("retained replay changed")
        with self.assertRaisesRegex(contracts.VerificationError, "retained replay changed"):
            build_release.build_release(self.config(), _verified_replay=self.offline_inputs(), _before_publish=changed)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_empty_target_appearing_during_publication_is_never_replaced(self) -> None:
        original = build_release._publish_no_replace
        appeared = []
        def collision(staging, target):
            target.mkdir()
            appeared.append((target, target.stat().st_ino))
            original(staging, target)
        with mock.patch.object(build_release, "_publish_no_replace", side_effect=collision):
            with self.assertRaises((OSError, contracts.VerificationError)):
                build_release.build_release(self.config())
        self.assertEqual(len(appeared), 1)
        target, inode = appeared[0]
        self.assertEqual(target.stat().st_ino, inode)
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(list(self.output.iterdir()), [target])

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
        self.assertEqual(
            {path.name for path in self.output.iterdir()},
            {first["manifest_sha256"]},
        )

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
        self.assertEqual(
            result["manifest_sha256"],
            hashlib.sha256(Path(result["manifest"]).read_bytes()).hexdigest(),
        )
        self.assertTrue(Path(result["manifest"]).is_file())


class ReducerInputInventoryRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo = HERE.parent
        cls.v1_path = HERE / "authority/reducer-inputs-v1.json"
        cls.v2_path = HERE / "authority/reducer-inputs-v2.json"
        cls.v1 = json.loads(cls.v1_path.read_text(encoding="utf-8"))
        cls.v2 = json.loads(cls.v2_path.read_text(encoding="utf-8"))

    def test_v1_compatibility_inventory_closes_known_consumer_gaps(self) -> None:
        self.assertIn("brain/layout.py", self.v1["scope"])
        self.assertIn("brain/build_context.py", self.v1["scope"])
        self.assertIn("brain/stage_io.py", self.v1["scope"])
        exact_paths = {entry["path"] for entry in self.v1["inputs"] if "path" in entry}
        self.assertIn("brain/data/discovery_rejected.jsonl", exact_paths)
        self.assertIn("catalog/data/tauceti_links.jsonl", exact_paths)
        self.assertNotIn("manage/data/halo.json", exact_paths)

        patterns = {
            entry["path_pattern"]
            for entry in self.v1["inputs"]
            if "path_pattern" in entry
        }
        self.assertIn("catalog/data/external/*_pages.jsonl", patterns)
        self.assertIn("catalog/data/external/*_links.jsonl", patterns)
        self.assertNotIn("catalog/data/external/*_{pages,links}.jsonl", patterns)
        self.assertTrue(all("{" not in pattern and "}" not in pattern for pattern in patterns))

    def test_v1_external_patterns_enumerate_page_and_link_members(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inventory = root / "brain/authority/reducer-inputs-v1.json"
            inventory.parent.mkdir(parents=True)
            inventory.write_bytes(self.v1_path.read_bytes())
            external = root / "catalog/data/external"
            external.mkdir(parents=True)
            (external / "fixture_pages.jsonl").write_text("{}\n", encoding="utf-8")
            (external / "fixture_links.jsonl").write_text("{}\n", encoding="utf-8")

            _digest, declared = build_release._inventory_paths(
                root, "brain/authority/reducer-inputs-v1.json"
            )
            external_members = {
                item["path"]
                for item in declared
                if item["present"]
                and str(item["path"]).startswith("catalog/data/external/")
            }
            self.assertEqual(
                external_members,
                {
                    "catalog/data/external/fixture_links.jsonl",
                    "catalog/data/external/fixture_pages.jsonl",
                },
            )

    def test_v2_inventory_validates_and_names_full_replay_roots(self) -> None:
        validated = contracts.validate_reducer_input_inventory(self.v2)
        self.assertIs(validated, self.v2)
        self.assertEqual(
            self.v2["inventory_id"],
            contracts.reducer_input_inventory_identity(self.v2),
        )
        self.assertEqual(self.v2["boundary"], "post-acquisition-fold")
        self.assertEqual(
            {root["id"]: root["kind"] for root in self.v2["roots"]},
            {
                "decl_oracle": "external_file",
                "external": "external_tree",
                "mathlib": "external_tree",
                "repo": "repository",
            },
        )

        by_id = {entry["id"]: entry for entry in self.v2["inputs"]}
        self.assertNotIn("halo", by_id)
        expected = {
            "brain-community-edges": (
                "repo",
                "brain/data/community_edges.jsonl",
                "optional",
            ),
            "brain-discovery-rejected": (
                "repo",
                "brain/data/discovery_rejected.jsonl",
                "optional",
            ),
            "tauceti-links": (
                "repo",
                "catalog/data/tauceti_links.jsonl",
                "optional",
            ),
            "external-pages": ("external", "*_pages.jsonl", "optional"),
            "external-links": ("external", "*_links.jsonl", "optional"),
            "mathlib-source-tree": ("mathlib", "Mathlib/**/*.lean", "required"),
            "mathlib-ilean-tree": (
                "mathlib",
                ".lake/build/lib/lean/**/*.ilean",
                "optional",
            ),
            "declaration-oracle": (
                "decl_oracle",
                "declaration-data.json",
                "optional",
            ),
        }
        for input_id, (root, logical_path, requirement) in expected.items():
            with self.subTest(input_id=input_id):
                entry = by_id[input_id]
                self.assertEqual(entry["root"], root)
                self.assertEqual(
                    entry.get("path", entry.get("path_pattern")), logical_path
                )
                self.assertEqual(entry["requirement"], requirement)

        # The source-tree requirement deliberately strengthens the legacy
        # builder's warn-and-continue behavior: a replay without Mathlib source
        # would silently lose declaration snippets.  Every other required input
        # below is opened/stat'ed unconditionally by the reducer.
        self.assertEqual(
            {
                entry["id"]
                for entry in self.v2["inputs"]
                if entry["requirement"] == "required"
            },
            {
                "brain-frontier-suitability-overrides",
                "concept-graph",
                "decl-qid-roles",
                "decl-to-qid",
                "hierarchy",
                "mathlib-source-tree",
                "rebuild-grounding",
                "source-registry",
                "statement-formal",
                "theorem-matching",
                "theoremgraph-links",
                "universe-extension",
                "wikidata-crossrefs",
                "wikidata-edges",
                "wikidata-universe",
            },
        )

        self.assertEqual(
            self.v2["scope"],
            [
                "brain/build_cell_shards.py",
                "brain/build_cells.py",
                "brain/build_common.py",
                "brain/build_context.py",
                "brain/build_frontier.py",
                "brain/build_shards.py",
                "brain/build_snapshot.py",
                "brain/frontier_suitability.py",
                "brain/layout.py",
                "brain/stage_io.py",
                "brain/store.py",
                "site/build_brain_page.py",
            ],
        )
        self.assertEqual(
            [
                (
                    stage["id"],
                    stage["program"],
                    stage["argv"],
                    stage["needs"],
                )
                for stage in self.v2["stages"]
            ],
            [
                (
                    "base-graph",
                    "brain/build_snapshot.py",
                    ["--jsonl-only"],
                    [],
                ),
                (
                    "top-level-shards",
                    "brain/build_shards.py",
                    [],
                    ["base-graph"],
                ),
                ("cells", "brain/build_cells.py", [], ["base-graph"]),
                (
                    "sqlite-with-cells",
                    "brain/build_snapshot.py",
                    ["--from-jsonl"],
                    ["base-graph", "cells"],
                ),
                (
                    "frontier",
                    "brain/build_frontier.py",
                    [],
                    ["base-graph", "cells"],
                ),
                (
                    "cell-shards",
                    "brain/build_cell_shards.py",
                    [],
                    ["base-graph", "cells", "frontier"],
                ),
                (
                    "brain-page",
                    "site/build_brain_page.py",
                    [],
                    [],
                ),
            ],
        )
        self.assertEqual(
            {
                stage["id"]: [
                    (output["path"], output["kind"])
                    for output in stage["outputs"]
                ]
                for stage in self.v2["stages"]
            },
            {
                "base-graph": [
                    ("brain/data/edges.jsonl", "file"),
                    ("brain/data/edges_links.jsonl", "file"),
                    ("brain/data/nodes.jsonl", "file"),
                ],
                "top-level-shards": [
                    ("site/assets/brain/sources.json", "file"),
                    ("site/assets/brain/xref_index.json", "file"),
                ],
                "cells": [
                    ("brain/data/cell_review.jsonl", "file"),
                    ("brain/data/cells.jsonl", "file"),
                    ("brain/data/synapses.jsonl", "file"),
                ],
                "sqlite-with-cells": [("brain/data/brain.sqlite3", "file")],
                "frontier": [
                    ("brain/data/frontier.jsonl", "file"),
                    ("brain/data/frontier_graph.json", "file"),
                    ("brain/data/frontier_review.jsonl", "file"),
                ],
                "cell-shards": [("site/assets/brain/cells", "tree")],
                "brain-page": [("site/out/brain.html", "file")],
            },
        )


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    )
    raise SystemExit(0 if result.wasSuccessful() else 1)
