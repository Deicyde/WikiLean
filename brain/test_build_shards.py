#!/usr/bin/env python3
"""Hermetic ownership and rollback tests for the top-level Brain assets."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import build_shards  # noqa: E402
import stage_io  # noqa: E402
from build_context import (  # noqa: E402
    BUILD_CONTEXT_SCHEMA,
    BuildContext,
    BuildContextError,
    canonical_json_bytes,
    generation_identity,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
HASH_D = "sha256:" + "d" * 64
COMMIT_A = "1" * 40
COMMIT_B = "2" * 40


class TopLevelShardPublicationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.data = self.repo / "brain/data"
        self.out = self.repo / "site/assets/brain"
        self.registry = self.repo / "catalog/data/source_registry.json"
        self.data.mkdir(parents=True)
        self.out.mkdir(parents=True)
        self.registry.parent.mkdir(parents=True)

        (self.data / "edges.jsonl").write_text(
            "\n".join(
                (
                    json.dumps({"_meta": {"generated_at": "2030-01-01T00:00:00Z"}}),
                    json.dumps({"src": "Q1", "dst": "xref:nlab:a", "kind": "xref"}),
                    json.dumps({"src": "Q1", "dst": "Q2", "kind": "depends"}),
                )
            )
            + "\n",
            encoding="utf-8",
        )
        (self.data / "community_edges.jsonl").write_text(
            json.dumps({"src": "Q2", "dst": "xref:nlab:a", "kind": "xref"})
            + "\n",
            encoding="utf-8",
        )
        self.registry.write_text(
            json.dumps(
                {
                    "brain_sources": {},
                    "crossref_sources": {},
                    "edge_sources": {},
                    "frontier_sources": {},
                    "layers": {"spine": "fixture"},
                    "literature_sources": {},
                    "node_sources": {
                        "fixture": {"name": "Fixture", "layer": "source"}
                    },
                    "our_data_license": "CC0-1.0",
                    "spine": {"key": "wikidata", "name": "Wikidata"},
                }
            ),
            encoding="utf-8",
        )

        self.cells = self.out / "cells"
        self.cells.mkdir()
        (self.cells / "manifest.json").write_bytes(b"cell tree sentinel")
        (self.out / "unrelated.json").write_bytes(b"unrelated sentinel")
        (self.out / "stale-v2-shard.json").write_bytes(b"legacy sentinel")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_builder(self) -> int:
        with mock.patch.multiple(
            build_shards,
            ROOT=self.repo,
            BRAIN_DATA=self.data,
            OUT_DIR=self.out,
        ):
            return build_shards.main()

    def assert_non_owned_assets_unchanged(self, parent_inode: int) -> None:
        self.assertEqual(self.out.stat().st_ino, parent_inode)
        self.assertEqual(
            (self.cells / "manifest.json").read_bytes(), b"cell tree sentinel"
        )
        self.assertEqual((self.out / "unrelated.json").read_bytes(), b"unrelated sentinel")
        self.assertEqual(
            (self.out / "stale-v2-shard.json").read_bytes(), b"legacy sentinel"
        )

    def test_publishes_only_two_owned_files_and_preserves_parent_tree(self) -> None:
        parent_inode = self.out.stat().st_ino
        (self.out / "xref_index.json").write_bytes(b"old xref")
        (self.out / "sources.json").write_bytes(b"old sources")

        self.assertEqual(self.run_builder(), 0)

        self.assert_non_owned_assets_unchanged(parent_inode)
        self.assertEqual(
            (self.out / "xref_index.json").read_text(encoding="utf-8"),
            '{"xref:nlab:a":["Q1","Q2"]}',
        )
        sources_bytes = (self.out / "sources.json").read_text(encoding="utf-8")
        sources = json.loads(sources_bytes)
        self.assertEqual(sources["layers"], {"spine": "fixture"})
        self.assertEqual(
            [(row["key"], row["group"]) for row in sources["sources"]],
            [("wikidata", "spine"), ("fixture", "node_sources")],
        )
        self.assertEqual(
            sources_bytes,
            json.dumps(sources, ensure_ascii=False, separators=(",", ":")),
        )
        self.assertEqual(list(self.out.parent.glob(".brain-shards.*")), [])

    def test_second_replace_failure_rolls_back_both_owned_files(self) -> None:
        old_xref = b"old xref bytes"
        old_sources = b"old sources bytes"
        (self.out / "xref_index.json").write_bytes(old_xref)
        (self.out / "sources.json").write_bytes(old_sources)
        parent_inode = self.out.stat().st_ino

        real_replace = os.replace
        failed = False

        def fail_second_publish(source: str | os.PathLike[str], destination) -> None:
            nonlocal failed
            source_path = Path(source)
            if (
                not failed
                and source_path.name == "sources.json"
                and source_path.parent.name.startswith(".brain-shards.")
            ):
                failed = True
                raise OSError("injected second-file publication failure")
            real_replace(source, destination)

        with (
            mock.patch.multiple(
                build_shards,
                ROOT=self.repo,
                BRAIN_DATA=self.data,
                OUT_DIR=self.out,
            ),
            mock.patch.object(build_shards.os, "replace", side_effect=fail_second_publish),
        ):
            with self.assertRaisesRegex(OSError, "injected second-file"):
                build_shards.main()

        self.assertTrue(failed)
        self.assertEqual((self.out / "xref_index.json").read_bytes(), old_xref)
        self.assertEqual((self.out / "sources.json").read_bytes(), old_sources)
        self.assert_non_owned_assets_unchanged(parent_inode)
        self.assertEqual(list(self.out.parent.glob(".brain-shards.*")), [])


class StageIOOwnershipTest(unittest.TestCase):
    def test_scratch_creation_race_never_deletes_competitor(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve() / "scratch"
            root.mkdir()
            parent = root / "stage"
            parent.mkdir()
            destination = parent / "publish"
            real_mkdir = Path.mkdir
            injected = False

            def race(path, mode=0o777, parents=False, exist_ok=False):
                nonlocal injected
                if Path(path) == destination and not injected:
                    injected = True
                    real_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)
                    raise FileExistsError(f"injected scratch race: {path}")
                return real_mkdir(
                    path,
                    mode=mode,
                    parents=parents,
                    exist_ok=exist_ok,
                )

            with mock.patch.object(Path, "mkdir", new=race):
                with self.assertRaisesRegex(FileExistsError, "injected scratch race"):
                    stage_io.create_owned_directory(root, destination)

            self.assertTrue(injected)
            self.assertTrue(destination.is_dir())

    def test_post_link_replacement_is_never_deleted_by_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            scratch_root = base / "scratch"
            output_root = base / "output"
            scratch_root.mkdir()
            output_root.mkdir()
            output = output_root / "result.json"
            real_link = stage_io.os.link
            replaced = False

            with stage_io.owned_directory(
                scratch_root, scratch_root / "stage/publish"
            ) as ownership:
                source = ownership.path / "result.json"
                stage_io.write_bytes_exclusive(source, b"ours", mode=0o644)

                def replace_after_link(src, destination, *, follow_symlinks=True):
                    nonlocal replaced
                    real_link(src, destination, follow_symlinks=follow_symlinks)
                    Path(destination).unlink()
                    Path(destination).write_bytes(b"competitor")
                    replaced = True

                with mock.patch.object(
                    stage_io.os, "link", side_effect=replace_after_link
                ), self.assertRaisesRegex(RuntimeError, "does not match"):
                    stage_io.publish_files_no_replace(
                        [(source, output)], scratch=ownership
                    )

            self.assertTrue(replaced)
            self.assertEqual(output.read_bytes(), b"competitor")

    def test_consumed_scratch_token_ignores_path_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            scratch_root = base / "scratch"
            output_root = base / "output"
            scratch_root.mkdir()
            output_root.mkdir()
            output = output_root / "result.json"

            with stage_io.owned_directory(
                scratch_root, scratch_root / "stage/publish"
            ) as ownership:
                source = ownership.path / "result.json"
                stage_io.write_bytes_exclusive(source, b"ours", mode=0o644)
                stage_io.publish_files_no_replace(
                    [(source, output)], scratch=ownership
                )
                ownership.path.mkdir()
                replacement = ownership.path / "replacement.json"
                replacement.write_bytes(b"competitor")
                with self.assertRaisesRegex(
                    RuntimeError, "replaced stage directory"
                ):
                    stage_io.publish_files_no_replace(
                        [(replacement, output_root / "second.json")],
                        scratch=ownership,
                    )

            self.assertEqual(output.read_bytes(), b"ours")
            self.assertTrue(ownership.path.is_dir())
            self.assertFalse((output_root / "second.json").exists())


class ContextTopLevelShardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name) / "replay"
        self.input_root = self.base / "input/repo"
        self.output_root = self.base / "output"
        self.scratch_root = self.base / "scratch"
        self.code_root = self.base / "code"
        for root in (
            self.input_root,
            self.output_root,
            self.scratch_root,
            self.code_root,
        ):
            root.mkdir(parents=True)

        self.edges = self.output_root / "brain/data/edges.jsonl"
        self.edges.parent.mkdir(parents=True)
        self.edges.write_text(
            "\n".join(
                (
                    json.dumps({"_meta": {"generated_at": "2030-01-01T00:00:00Z"}}),
                    json.dumps({"src": "Q1", "dst": "xref:nlab:a", "kind": "xref"}),
                    json.dumps({"src": "Q1", "dst": "Q2", "kind": "depends"}),
                )
            )
            + "\n",
            encoding="utf-8",
        )
        self.registry = self.input_root / "catalog/data/source_registry.json"
        self.registry.parent.mkdir(parents=True)
        self.registry.write_text(
            json.dumps(
                {
                    "brain_sources": {},
                    "crossref_sources": {},
                    "edge_sources": {},
                    "frontier_sources": {},
                    "layers": {"spine": "context fixture"},
                    "literature_sources": {},
                    "node_sources": {
                        "context": {"name": "Context", "layer": "source"}
                    },
                    "our_data_license": "CC0-1.0",
                    "spine": {"key": "wikidata", "name": "Wikidata"},
                }
            ),
            encoding="utf-8",
        )
        self.community = self.input_root / "brain/data/community_edges.jsonl"
        self.community.parent.mkdir(parents=True)

        # These repository-shaped files are deliberate decoys. Context mode must
        # never discover them through build_shards' legacy globals.
        self.host = Path(self.temp.name) / "host"
        host_data = self.host / "brain/data"
        host_registry = self.host / "catalog/data/source_registry.json"
        host_data.mkdir(parents=True)
        host_registry.parent.mkdir(parents=True)
        (host_data / "edges.jsonl").write_text(
            json.dumps({"_meta": {}}) + "\n"
            + json.dumps({"src": "Q9", "dst": "xref:host:wrong", "kind": "xref"})
            + "\n",
            encoding="utf-8",
        )
        (host_data / "community_edges.jsonl").write_text(
            json.dumps({"src": "Q9", "dst": "xref:host:wrong", "kind": "xref"})
            + "\n",
            encoding="utf-8",
        )
        host_registry.write_text("{}", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _member(path: Path, logical_path: str, object_name: str) -> dict:
        raw = path.read_bytes()
        return {
            "bytes": len(raw),
            "materialized_path": str(path.resolve()),
            "media_type": "application/json",
            "object": object_name,
            "path": logical_path,
            "pin": {"tree": COMMIT_B, "type": "git_commit", "value": COMMIT_A},
            "sha256": hashlib.sha256(raw).hexdigest(),
            "source_manifest_id": HASH_D,
        }

    def context(
        self,
        *,
        community_present: bool,
        stage_program: str = "brain/build_shards.py",
        stage_argv: list[str] | None = None,
        stage_needs: list[str] | None = None,
        stage_outputs: list[dict[str, str]] | None = None,
    ) -> BuildContext:
        if community_present:
            self.community.write_text(
                json.dumps({"src": "Q2", "dst": "xref:nlab:a", "kind": "xref"})
                + "\n",
                encoding="utf-8",
            )
            community_members = [
                self._member(
                    self.community,
                    "brain/data/community_edges.jsonl",
                    "community-edges",
                )
            ]
        else:
            self.community.unlink(missing_ok=True)
            community_members = []

        configuration = {
            "cell_attach_kinds": ["generalization", "special_case"],
            "external_node_cap": 8000,
            "layout": {"enabled": True, "iterations": 200},
            "schema": "wikilean.brain-reducer-config/v1",
        }
        configuration_sha256 = hashlib.sha256(
            canonical_json_bytes(configuration)
        ).hexdigest()
        document = {
            "bindings": [
                {
                    "cardinality": "one",
                    "class": "curated_git_input",
                    "input_id": "brain-community-edges",
                    "members": community_members,
                    "path": "brain/data/community_edges.jsonl",
                    "requirement": "optional",
                    "root": "repo",
                    "source_manifest_ids": [HASH_D],
                    "state": "present" if community_present else "absent",
                },
                {
                    "cardinality": "one",
                    "class": "curated_git_input",
                    "input_id": "source-registry",
                    "members": [
                        self._member(
                            self.registry,
                            "catalog/data/source_registry.json",
                            "source-registry",
                        )
                    ],
                    "path": "catalog/data/source_registry.json",
                    "requirement": "required",
                    "root": "repo",
                    "source_manifest_ids": [HASH_D],
                    "state": "present",
                },
            ],
            "configuration": configuration,
            "replay": {
                "authority": {"authority_root": HASH_A, "git_commit": COMMIT_A},
                "offline_pack_id": HASH_B,
                "prior_state_root": None,
                "reducer": {
                    "configuration_sha256": configuration_sha256,
                    "environment_sha256": "3" * 64,
                    "git_commit": COMMIT_B,
                },
                "reducer_inventory_id": HASH_C,
                "semantic_epoch": "brain-v3-context-test",
                "source_set_root": HASH_D,
            },
            "roots": {
                "code": str(self.code_root.resolve()),
                "input": str((self.base / "input").resolve()),
                "output": str(self.output_root.resolve()),
                "scratch": str(self.scratch_root.resolve()),
            },
            "schema": BUILD_CONTEXT_SCHEMA,
            "stages": [
                {
                    "argv": ["--jsonl-only"],
                    "id": "base-graph",
                    "needs": [],
                    "outputs": [
                        {"kind": "file", "path": "brain/data/edges.jsonl"}
                    ],
                    "program": "brain/build_snapshot.py",
                },
                {
                    "argv": stage_argv or [],
                    "id": "top-level-shards",
                    "needs": stage_needs if stage_needs is not None else ["base-graph"],
                    "outputs": stage_outputs if stage_outputs is not None else [
                        {"kind": "file", "path": "site/assets/brain/sources.json"},
                        {"kind": "file", "path": "site/assets/brain/xref_index.json"},
                    ],
                    "program": stage_program,
                },
            ],
        }
        document["generation_id"] = generation_identity(document)
        return BuildContext.from_document(document)

    def run_context(self, context: BuildContext) -> int:
        with mock.patch.multiple(
            build_shards,
            ROOT=self.host,
            BRAIN_DATA=self.host / "brain/data",
            OUT_DIR=self.host / "site/assets/brain",
        ):
            return build_shards.build_top_level_shards_from_context(context)

    def run_context_cli(self, context: BuildContext) -> int:
        context_path = self.base / "build-context.json"
        context_path.write_bytes(canonical_json_bytes(context.to_document()))
        with mock.patch.multiple(
            build_shards,
            ROOT=self.host,
            BRAIN_DATA=self.host / "brain/data",
            OUT_DIR=self.host / "site/assets/brain",
        ):
            return build_shards._cli(
                [
                    "--build-context",
                    str(context_path),
                    "--stage-id",
                    "top-level-shards",
                ]
            )

    def test_context_uses_exact_bound_inputs_and_outputs_only(self) -> None:
        context = self.context(community_present=True)

        previous_umask = os.umask(0o777)
        try:
            self.assertEqual(self.run_context(context), 0)
        finally:
            os.umask(previous_umask)

        out = self.output_root / "site/assets/brain"
        self.assertEqual(
            (out / "xref_index.json").read_text(encoding="utf-8"),
            '{"xref:nlab:a":["Q1","Q2"]}',
        )
        sources = json.loads((out / "sources.json").read_text(encoding="utf-8"))
        self.assertEqual(sources["layers"], {"spine": "context fixture"})
        self.assertEqual(
            sorted(path.relative_to(self.output_root).as_posix()
                   for path in self.output_root.rglob("*") if path.is_file()),
            [
                "brain/data/edges.jsonl",
                "site/assets/brain/sources.json",
                "site/assets/brain/xref_index.json",
            ],
        )
        self.assertFalse((self.host / "site/assets/brain/xref_index.json").exists())
        self.assertFalse((self.host / "site/assets/brain/sources.json").exists())
        self.assertEqual((out / "xref_index.json").stat().st_mode & 0o777, 0o644)
        self.assertEqual((out / "sources.json").stat().st_mode & 0o777, 0o644)
        self.assertEqual(out.stat().st_mode & 0o777, 0o700)
        self.assertEqual(list(self.scratch_root.rglob("*")), [self.scratch_root / "top-level-shards"])

    def test_absent_optional_community_input_does_not_discover_host_decoy(self) -> None:
        context = self.context(community_present=False)

        self.assertEqual(self.run_context_cli(context), 0)

        xref = json.loads(
            (self.output_root / "site/assets/brain/xref_index.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(xref, {"xref:nlab:a": ["Q1"]})
        self.assertNotIn("xref:host:wrong", xref)

    def test_context_refuses_preexisting_owned_output(self) -> None:
        context = self.context(community_present=False)
        output = self.output_root / "site/assets/brain/xref_index.json"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"sentinel")

        with self.assertRaisesRegex(FileExistsError, "context-owned output already exists"):
            self.run_context(context)

        self.assertEqual(output.read_bytes(), b"sentinel")
        self.assertFalse((output.parent / "sources.json").exists())
        self.assertEqual(list(self.scratch_root.rglob("*")), [])

    def test_context_rejects_stage_program_and_argv_mismatches(self) -> None:
        wrong_program = self.context(
            community_present=False,
            stage_program="brain/not-build-shards.py",
        )
        with self.assertRaisesRegex(BuildContextError, "program is"):
            self.run_context(wrong_program)

        wrong_argv = self.context(
            community_present=False,
            stage_argv=["--unexpected"],
        )
        with self.assertRaisesRegex(BuildContextError, "argv is"):
            self.run_context(wrong_argv)

        wrong_needs = self.context(
            community_present=False,
            stage_needs=[],
        )
        with self.assertRaisesRegex(BuildContextError, "needs are"):
            self.run_context(wrong_needs)

        wrong_outputs = self.context(
            community_present=False,
            stage_outputs=[
                {"kind": "file", "path": "site/assets/brain/not-sources.json"},
                {"kind": "file", "path": "site/assets/brain/xref_index.json"},
            ],
        )
        with self.assertRaisesRegex(BuildContextError, "outputs are"):
            self.run_context(wrong_outputs)

        self.assertFalse((self.output_root / "site/assets/brain").exists())

    def test_context_second_output_race_rolls_back_first_output(self) -> None:
        context = self.context(community_present=False)
        xref_output = self.output_root / "site/assets/brain/xref_index.json"
        sources_output = self.output_root / "site/assets/brain/sources.json"
        real_link = stage_io.os.link
        injected = False

        def race(source, destination, *, follow_symlinks=True):
            nonlocal injected
            destination = Path(destination)
            if destination.resolve(strict=False) == sources_output.resolve() and not injected:
                injected = True
                destination.write_bytes(b"competitor")
            return real_link(
                source,
                destination,
                follow_symlinks=follow_symlinks,
            )

        with mock.patch.object(stage_io.os, "link", side_effect=race):
            with self.assertRaises(FileExistsError):
                self.run_context(context)

        self.assertTrue(injected)
        self.assertFalse(xref_output.exists())
        self.assertEqual(sources_output.read_bytes(), b"competitor")
        self.assertFalse(
            (self.scratch_root / "top-level-shards/publish").exists()
        )


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    )
    raise SystemExit(0 if result.wasSuccessful() else 1)
