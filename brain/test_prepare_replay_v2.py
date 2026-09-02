#!/usr/bin/env python3
"""Focused tests for sealed offline-pack/v2 workspace preparation."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import shutil
import stat
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
import build_context  # noqa: E402
import prepare_replay_v2 as prepare  # noqa: E402
import test_authority_contracts as authority_tests  # noqa: E402


AUTHORITY_GIT = "c" * 40
AUTHORITY_ROOT = "sha256:" + "d" * 64
PRIOR_ROOT = "sha256:" + "f" * 64
SEMANTIC_EPOCH = "brain-reducer-v2"
CONFIGURATION = {
    "schema": build_context.REDUCER_CONFIGURATION_SCHEMA,
    "external_node_cap": 8,
    "cell_attach_kinds": ["generalization", "related"],
    "layout": {"enabled": True, "iterations": 12},
}


class ReplayPackFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.case = authority_tests.V2SourceSetVerificationTest()
        self.case.root = root

    def make(self) -> tuple[dict[str, object], Path]:
        pack, manifest = self.case.make_pack()
        authority_tests.write_canonical(
            self.root / "config/reducer.json", CONFIGURATION
        )
        pack["configuration"] = authority_tests.file_ref(
            self.root, "config/reducer.json", "application/json"
        )
        self.rewrite(pack, manifest)
        return pack, manifest

    def rewrite(self, pack: dict[str, object], manifest: Path) -> None:
        pack["offline_pack_id"] = contracts.offline_pack_identity(pack)
        authority_tests.write_canonical(manifest, pack)

    def add_reducer_ancestry_collision(
        self, pack: dict[str, object], manifest: Path
    ) -> None:
        self.add_reducer_files(
            pack,
            manifest,
            {"brain/helper.py/child.py": b"COLLISION = True\n"},
        )

    def add_reducer_files(
        self,
        pack: dict[str, object],
        manifest: Path,
        additions: dict[str, bytes],
    ) -> None:
        inventory_path = self.root / pack["inventory"]["path"]
        inventory = json.loads(inventory_path.read_text())
        inventory["scope"].extend(additions)
        inventory["scope"].sort()
        inventory["inventory_id"] = contracts.reducer_input_inventory_identity(
            inventory
        )
        authority_tests.write_canonical(inventory_path, inventory)
        pack["inventory"] = {
            **authority_tests.file_ref(
                self.root, "inventory.json", "application/json"
            ),
            "inventory_id": inventory["inventory_id"],
        }
        for index, (logical_path, data) in enumerate(sorted(additions.items())):
            relative = f"reducer/additional-{index}.py"
            physical = self.root / relative
            physical.write_bytes(data)
            pack["reducer"]["files"].append(
                {
                    "logical_path": logical_path,
                    **authority_tests.file_ref(
                        self.root, relative, "text/x-python"
                    ),
                }
            )
        pack["reducer"]["files"].sort(key=lambda item: item["logical_path"])
        pack["source_set_root"] = contracts.source_set_root_v2(
            inventory["inventory_id"],
            [item["source_manifest_id"] for item in pack["source_manifests"]],
            pack["input_bindings"],
        )
        self.rewrite(pack, manifest)


class PrepareReplayV2Test(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pack_root = self.root / "pack"
        self.pack_root.mkdir()
        self.fixture = ReplayPackFixture(self.pack_root)
        self.pack, self.manifest = self.fixture.make()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def prepare(self, workspace: Path, **overrides: object) -> prepare.PreparedReplay:
        arguments: dict[str, object] = {
            "authority_git_commit": AUTHORITY_GIT,
            "authority_root": AUTHORITY_ROOT,
            "semantic_epoch": SEMANTIC_EPOCH,
            "prior_state_root": PRIOR_ROOT,
            "pack_root": self.pack_root,
        }
        arguments.update(overrides)
        return prepare.prepare_replay_v2(self.manifest, workspace, **arguments)

    def bound_source_object(self) -> tuple[Path, dict[str, object]]:
        binding = next(
            item
            for item in self.pack["input_bindings"]
            if item["input_id"] == "source"
        )
        member = binding["members"][0]
        manifest_ref = next(
            ref
            for ref in self.pack["source_manifests"]
            if ref["source_manifest_id"] == member["source_manifest_id"]
        )
        source_manifest = json.loads(
            (self.pack_root / manifest_ref["path"]).read_text()
        )
        source_object = next(
            item
            for item in source_manifest["objects"]
            if item["name"] == member["object"]
        )
        return self.pack_root / source_object["path"], source_object

    def test_materializes_only_declared_copies_and_canonical_context(self) -> None:
        workspace = self.root / "workspace"
        result = self.prepare(workspace)
        self.assertEqual(result.offline_pack_id, self.pack["offline_pack_id"])
        self.assertEqual(result.source_set_root, self.pack["source_set_root"])
        self.assertEqual(
            result.reducer_inventory_id, self.pack["inventory"]["inventory_id"]
        )

        raw_context = result.context_path.read_bytes()
        context_document = json.loads(raw_context)
        self.assertEqual(
            raw_context, build_context.canonical_json_bytes(context_document)
        )
        context = build_context.BuildContext.load(result.context_path)
        self.assertEqual(context.generation_id, result.generation_id)
        self.assertEqual(
            context.replay.reducer_git_commit, self.pack["reducer"]["git_commit"]
        )
        self.assertEqual(context.require_one("source").read_bytes(), b'{"rows":[1,2]}')
        self.assertEqual(
            context.require_one("curated").read_bytes(), b'{"curated":true}'
        )
        self.assertEqual(context.members("optional_external"), ())

        input_files = {
            path.relative_to(workspace / "input").as_posix()
            for path in (workspace / "input").rglob("*")
            if path.is_file()
        }
        code_files = {
            path.relative_to(workspace / "code").as_posix()
            for path in (workspace / "code").rglob("*")
            if path.is_file()
        }
        self.assertEqual(
            input_files,
            {"external/input.json", "repo/catalog/curated.json"},
        )
        self.assertEqual(code_files, {"brain/helper.py", "brain/replay.py"})
        self.assertFalse(any(path.is_symlink() for path in workspace.rglob("*")))

        source_member = next(
            member
            for binding in self.pack["input_bindings"]
            if binding["input_id"] == "source"
            for member in binding["members"]
        )
        source_manifest_ref = next(
            ref
            for ref in self.pack["source_manifests"]
            if ref["source_manifest_id"] == source_member["source_manifest_id"]
        )
        source_manifest = json.loads(
            (self.pack_root / source_manifest_ref["path"]).read_text()
        )
        source_object = next(
            item
            for item in source_manifest["objects"]
            if item["name"] == source_member["object"]
        )
        self.assertNotEqual(
            os.stat(self.pack_root / source_object["path"]).st_ino,
            os.stat(context.require_one("source")).st_ino,
        )

        inventory = json.loads(
            (self.pack_root / self.pack["inventory"]["path"]).read_text()
        )
        self.assertEqual(context_document["stages"], inventory["stages"])
        binding_projection = [
            {
                "input_id": binding["input_id"],
                "members": [
                    {
                        "object": member["object"],
                        "path": member["path"],
                        "source_manifest_id": member["source_manifest_id"],
                    }
                    for member in binding["members"]
                ],
                "state": binding["state"],
            }
            for binding in context_document["bindings"]
        ]
        self.assertEqual(binding_projection, self.pack["input_bindings"])

    def test_code_and_input_are_read_only_while_output_and_scratch_are_writable(self) -> None:
        workspace = self.root / "permissions"
        self.prepare(workspace)
        for tree in (workspace / "code", workspace / "input"):
            for path in [tree, *tree.rglob("*")]:
                mode = stat.S_IMODE(path.lstat().st_mode)
                self.assertEqual(mode & 0o222, 0, path)
                if path.is_dir():
                    self.assertNotEqual(mode & 0o111, 0, path)
        for tree in (workspace / "output", workspace / "scratch"):
            self.assertNotEqual(stat.S_IMODE(tree.stat().st_mode) & 0o200, 0)

    def test_modes_are_independent_of_hostile_umask(self) -> None:
        workspace = self.root / "umask"
        previous = os.umask(0o777)
        try:
            self.prepare(workspace)
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE(workspace.stat().st_mode), 0o700)
        for path in (workspace / "output", workspace / "scratch"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        for root in (workspace / "code", workspace / "input"):
            for path in (root, *root.rglob("*")):
                expected = 0o555 if path.is_dir() else 0o444
                self.assertEqual(stat.S_IMODE(path.lstat().st_mode), expected, path)
        self.assertEqual(
            stat.S_IMODE((workspace / "build-context.json").stat().st_mode),
            0o444,
        )

    def test_relocation_and_hostile_brain_environment_preserve_generation(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "BRAIN_EXTERNAL_DIR": "/hostile/external",
                "BRAIN_MATHLIB_CHECKOUT": "/hostile/mathlib",
                "BRAIN_DECLARATION_DATA": "/hostile/oracle.json",
                "BRAIN_LAYOUT": "0",
            },
        ):
            first = self.prepare(self.root / "first")
        with mock.patch.dict(
            os.environ,
            {
                "BRAIN_EXTERNAL_DIR": "/different/external",
                "BRAIN_MATHLIB_CHECKOUT": "/different/mathlib",
                "BRAIN_DECLARATION_DATA": "/different/oracle.json",
                "BRAIN_LAYOUT": "1",
            },
        ):
            second = self.prepare(self.root / "second")
        self.assertEqual(first.generation_id, second.generation_id)
        self.assertEqual(
            build_context.BuildContext.load(first.context_path).logical_document(),
            build_context.BuildContext.load(second.context_path).logical_document(),
        )

    def test_existing_or_overlapping_workspace_is_rejected_without_mutation(self) -> None:
        existing = self.root / "existing"
        existing.mkdir()
        marker = existing / "keep.txt"
        marker.write_text("keep")
        with self.assertRaisesRegex(prepare.ReplayPreparationError, "already exists"):
            self.prepare(existing)
        self.assertEqual(marker.read_text(), "keep")

        nested_in_pack = self.pack_root / "workspace"
        with self.assertRaisesRegex(prepare.ReplayPreparationError, "disjoint"):
            self.prepare(nested_in_pack)
        self.assertFalse(nested_in_pack.exists())

    def test_tampered_object_fails_before_workspace_creation(self) -> None:
        object_ref = self.pack["objects"][0]
        object_path = self.pack_root / object_ref["path"]
        object_path.write_bytes(b"x" * object_ref["bytes"])
        workspace = self.root / "tampered"
        with self.assertRaises(contracts.VerificationError):
            self.prepare(workspace)
        self.assertFalse(workspace.exists())

    def test_destination_ancestry_collision_fails_before_workspace_creation(self) -> None:
        self.fixture.add_reducer_ancestry_collision(self.pack, self.manifest)
        workspace = self.root / "collision"
        with self.assertRaises(
            (prepare.ReplayPreparationError, contracts.VerificationError)
        ):
            self.prepare(workspace)
        self.assertFalse(workspace.exists())

    def test_portable_directory_alias_is_rejected_on_every_filesystem(self) -> None:
        self.fixture.add_reducer_files(
            self.pack,
            self.manifest,
            {
                "brain/Case/A.py": b"A = 1\n",
                "brain/case/B.py": b"B = 1\n",
            },
        )
        workspace = self.root / "case-alias"
        with self.assertRaisesRegex(prepare.ReplayPreparationError, "aliases"):
            self.prepare(workspace)
        self.assertFalse(workspace.exists())

    def test_portable_file_alias_is_rejected_before_staging(self) -> None:
        self.fixture.add_reducer_files(
            self.pack,
            self.manifest,
            {
                "brain/Alias.py": b"UPPER = 1\n",
                "brain/alias.py": b"LOWER = 1\n",
            },
        )
        workspace = self.root / "file-alias"
        with self.assertRaisesRegex(prepare.ReplayPreparationError, "aliases"):
            self.prepare(workspace)
        self.assertFalse(workspace.exists())
        self.assertEqual(list(self.root.glob(".file-alias.prepare-*")), [])

    def test_post_verification_reducer_tamper_cleans_private_staging(self) -> None:
        original = contracts.verify_offline_pack_files

        def verify_then_tamper(*args: object, **kwargs: object) -> dict[str, int]:
            result = original(*args, **kwargs)
            reducer_ref = self.pack["reducer"]["files"][0]
            reducer_path = self.pack_root / reducer_ref["path"]
            reducer_path.write_bytes(b"x" * reducer_ref["bytes"])
            return result

        workspace = self.root / "post-verify-tamper"
        with mock.patch.object(
            contracts,
            "verify_offline_pack_files",
            side_effect=verify_then_tamper,
        ):
            with self.assertRaisesRegex(
                prepare.ReplayPreparationError, "source changed"
            ):
                self.prepare(workspace)
        self.assertFalse(workspace.exists())
        self.assertEqual(list(self.root.glob(".post-verify-tamper.prepare-*")), [])

    def test_post_verification_source_tamper_is_reverified(self) -> None:
        original = contracts.verify_offline_pack_files
        source_path, source_ref = self.bound_source_object()

        def verify_then_tamper(*args: object, **kwargs: object) -> dict[str, int]:
            result = original(*args, **kwargs)
            source_path.write_bytes(b"x" * source_ref["bytes"])
            return result

        workspace = self.root / "post-verify-source-tamper"
        with mock.patch.object(
            contracts,
            "verify_offline_pack_files",
            side_effect=verify_then_tamper,
        ):
            with self.assertRaisesRegex(
                prepare.ReplayPreparationError, "source changed"
            ):
                self.prepare(workspace)
        self.assertFalse(workspace.exists())
        self.assertEqual(
            list(self.root.glob(".post-verify-source-tamper.prepare-*")), []
        )

    def test_post_verification_symlink_swap_is_rejected(self) -> None:
        original = contracts.verify_offline_pack_files
        reducer_ref = self.pack["reducer"]["files"][0]
        reducer_path = self.pack_root / reducer_ref["path"]
        replacement = self.root / "replacement.py"
        replacement.write_bytes(reducer_path.read_bytes())

        def verify_then_swap(*args: object, **kwargs: object) -> dict[str, int]:
            result = original(*args, **kwargs)
            reducer_path.unlink()
            reducer_path.symlink_to(replacement)
            return result

        workspace = self.root / "post-verify-symlink"
        with mock.patch.object(
            contracts,
            "verify_offline_pack_files",
            side_effect=verify_then_swap,
        ):
            with self.assertRaises(contracts.VerificationError):
                self.prepare(workspace)
        self.assertFalse(workspace.exists())
        self.assertEqual(list(self.root.glob(".post-verify-symlink.prepare-*")), [])

    def test_publication_race_preserves_competing_workspace(self) -> None:
        original = prepare._publish_no_replace
        workspace = self.root / "publication-race"

        def race(staging: Path, target: Path) -> None:
            target.mkdir()
            (target / "keep.txt").write_text("competitor")
            original(staging, target)

        with mock.patch.object(prepare, "_publish_no_replace", side_effect=race):
            with self.assertRaisesRegex(
                prepare.ReplayPreparationError, "appeared during publication"
            ):
                self.prepare(workspace)
        self.assertEqual((workspace / "keep.txt").read_text(), "competitor")
        self.assertEqual(list(self.root.glob(".publication-race.prepare-*")), [])

    def test_cleanup_refuses_replaced_staging_directory(self) -> None:
        target = self.root / "owned"
        ownership = prepare._create_staging_directory(target)
        moved = self.root / "moved-owned"
        ownership.path.rename(moved)
        ownership.path.mkdir()
        marker = ownership.path / "keep.txt"
        marker.write_text("competitor")
        with self.assertRaisesRegex(prepare.ReplayPreparationError, "replaced"):
            prepare._remove_created_workspace(ownership)
        self.assertEqual(marker.read_text(), "competitor")
        shutil.rmtree(ownership.path)
        shutil.rmtree(moved)

    def test_noncanonical_configuration_is_rejected(self) -> None:
        noncanonical = json.dumps(CONFIGURATION, indent=2).encode("utf-8") + b"\n"
        config_path = self.pack_root / "config/reducer.json"
        config_path.write_bytes(noncanonical)
        self.pack["configuration"] = authority_tests.file_ref(
            self.pack_root, "config/reducer.json", "application/json"
        )
        self.fixture.rewrite(self.pack, self.manifest)
        workspace = self.root / "noncanonical"
        with self.assertRaisesRegex(
            prepare.ReplayPreparationError, "canonical-json-v1"
        ):
            self.prepare(workspace)
        self.assertFalse(workspace.exists())

    def test_failure_after_creation_removes_only_created_workspace(self) -> None:
        workspace = self.root / "cleanup"
        original = prepare._make_read_only_tree
        calls = 0

        def fail_after_one_tree(root: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                original(root)
                return
            raise prepare.ReplayPreparationError("injected failure")

        with mock.patch.object(
            prepare,
            "_make_read_only_tree",
            side_effect=fail_after_one_tree,
        ):
            with self.assertRaisesRegex(prepare.ReplayPreparationError, "injected"):
                self.prepare(workspace)
        self.assertFalse(workspace.exists())

    def test_prepublication_fsync_failure_cleans_staging(self) -> None:
        workspace = self.root / "fsync-failure"
        with mock.patch.object(
            prepare,
            "_fsync_tree",
            side_effect=OSError("injected fsync failure"),
        ):
            with self.assertRaisesRegex(OSError, "injected fsync failure"):
                self.prepare(workspace)
        self.assertFalse(workspace.exists())
        self.assertEqual(list(self.root.glob(".fsync-failure.prepare-*")), [])

    def test_postpublication_parent_fsync_failure_is_explicit(self) -> None:
        workspace = self.root / "published-fsync-failure"
        original = prepare._fsync_directory

        def fail_final_parent(path: Path) -> None:
            if path.resolve() == workspace.parent.resolve():
                raise OSError("injected parent fsync failure")
            original(path)

        with mock.patch.object(
            prepare,
            "_fsync_directory",
            side_effect=fail_final_parent,
        ):
            with self.assertRaisesRegex(
                prepare.ReplayPreparationError, "workspace was published"
            ):
                self.prepare(workspace)
        self.assertTrue((workspace / "build-context.json").is_file())

    def test_cleanup_failure_preserves_primary_exception_and_reports_note(self) -> None:
        workspace = self.root / "cleanup-note"
        with mock.patch.object(
            prepare,
            "_make_read_only_tree",
            side_effect=prepare.ReplayPreparationError("primary failure"),
        ), mock.patch.object(
            prepare,
            "_remove_created_workspace",
            side_effect=prepare.ReplayPreparationError("cleanup leak"),
        ):
            with self.assertRaisesRegex(
                prepare.ReplayPreparationError, "primary failure"
            ) as caught:
                self.prepare(workspace)
        self.assertIn(
            "staging cleanup also failed: cleanup leak",
            getattr(caught.exception, "__notes__", []),
        )
        leaked = list(self.root.glob(".cleanup-note.prepare-*"))
        self.assertEqual(len(leaked), 1)
        shutil.rmtree(leaked[0])

    def test_cli_serializes_exception_notes(self) -> None:
        failure = prepare.ReplayPreparationError("primary failure")
        failure.add_note("staging cleanup also failed: cleanup leak")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(
            prepare, "prepare_replay_v2", side_effect=failure
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            status = prepare.main(
                [
                    "--manifest",
                    "manifest.json",
                    "--workspace",
                    "workspace",
                    "--authority-git-commit",
                    AUTHORITY_GIT,
                    "--authority-root",
                    AUTHORITY_ROOT,
                    "--semantic-epoch",
                    SEMANTIC_EPOCH,
                ]
            )
        self.assertEqual(status, 1)
        self.assertEqual(stdout.getvalue(), "")
        error = json.loads(stderr.getvalue())
        self.assertEqual(
            error["error"]["notes"],
            ["staging cleanup also failed: cleanup leak"],
        )

    def test_cli_prints_verified_identity_document(self) -> None:
        workspace = self.root / "cli"
        completed = subprocess.run(
            [
                sys.executable,
                str(TOOLS / "prepare_replay_v2.py"),
                "--manifest",
                str(self.manifest),
                "--root",
                str(self.pack_root),
                "--workspace",
                str(workspace),
                "--authority-git-commit",
                AUTHORITY_GIT,
                "--authority-root",
                AUTHORITY_ROOT,
                "--semantic-epoch",
                SEMANTIC_EPOCH,
                "--prior-state-root",
                PRIOR_ROOT,
            ],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "BRAIN_EXTERNAL_DIR": "/ignored"},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result["ok"])
        self.assertEqual(result["offline_pack_id"], self.pack["offline_pack_id"])
        self.assertEqual(
            result["generation_id"],
            build_context.BuildContext.load(
                workspace / "build-context.json"
            ).generation_id,
        )

        failed = subprocess.run(
            [
                sys.executable,
                str(TOOLS / "prepare_replay_v2.py"),
                "--manifest",
                str(self.manifest),
                "--workspace",
                str(workspace),
                "--authority-git-commit",
                AUTHORITY_GIT,
                "--authority-root",
                AUTHORITY_ROOT,
                "--semantic-epoch",
                SEMANTIC_EPOCH,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(failed.stdout, "")
        error = json.loads(failed.stderr)
        self.assertFalse(error["ok"])
        self.assertIn("already exists", error["error"]["message"])


if __name__ == "__main__":
    unittest.main()
