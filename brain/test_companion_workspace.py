#!/usr/bin/env python3
"""Workspace lifecycle checks; full adapter cases require the shared environment."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from brain import companion_workspace as workspace

try:
    import autoform_cli
    import formal_companion_annotations as fca
except ImportError:
    autoform_cli = fca = None


class WorkspaceConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / "workspace"
        self.tools = self.root / "tools"
        self.tools.mkdir()

    def init(self, **kwargs):
        return workspace.initialize(self.path, autoform_root=self.tools, fca_root=self.tools, **kwargs)

    def test_existing_workspace_is_not_replaced(self):
        self.init()
        before = (self.path / "workspace.json").read_bytes()
        with self.assertRaisesRegex(workspace.WorkspaceError, "already exists"):
            self.init()
        self.assertEqual(before, (self.path / "workspace.json").read_bytes())

    def test_consumer_coordinates_are_all_required(self):
        with self.assertRaisesRegex(workspace.WorkspaceError, "supplied together"):
            self.init(consumer=self.root / "consumer")
        self.assertFalse(self.path.exists())

    def test_workspace_cannot_be_inside_a_tool_checkout(self):
        self.path = self.tools / "generated"
        with self.assertRaisesRegex(workspace.WorkspaceError, "outside the three"):
            self.init()
        self.assertFalse(self.path.exists())

    def test_consumer_must_be_independent(self):
        with self.assertRaisesRegex(workspace.WorkspaceError, "independent consumer"):
            self.init(consumer=self.tools / "example", repo="https://github.com/example/repo", commit="a" * 40)

    def test_selection_cannot_include_workspace_history(self):
        with self.assertRaisesRegex(workspace.WorkspaceError, "generated snapshots"):
            self.init(shards=[self.root])

    def test_lock_serializes_setup_and_sync_without_stealing(self):
        self.init()
        with workspace._lock(self.path):
            with self.assertRaisesRegex(workspace.WorkspaceError, "locked"):
                with workspace._lock(self.path):
                    self.fail("concurrent lock acquired")
        self.assertFalse((self.path / ".workspace.lock").exists())

    def test_current_rejects_foreign_file_and_escaping_link(self):
        self.init()
        current = self.path / "current"
        current.write_text("keep")
        with self.assertRaisesRegex(workspace.WorkspaceError, "symlink"):
            workspace.current_generation(self.path)
        self.assertEqual(current.read_text(), "keep")
        current.unlink()
        current.symlink_to(self.root)
        with self.assertRaisesRegex(workspace.WorkspaceError, "escapes"):
            workspace.current_generation(self.path)

    def test_tool_fingerprint_frames_file_names_kinds_and_contents(self):
        def git(*args):
            return workspace._run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                                   "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                                   "-C", self.tools, *args])
        git("init", "-q", "--template=")
        git("commit", "--allow-empty", "-qm", "fixture")
        (self.tools / "a").write_text("bc")
        with patch.dict(os.environ, {"GIT_DIR": "/does-not-exist"}):
            first = workspace._revision(self.tools)
        (self.tools / "a").unlink()
        (self.tools / "ab").write_text("c")
        second = workspace._revision(self.tools)
        self.assertNotEqual(first["changes_sha256"], second["changes_sha256"])
        (self.tools / "ab").unlink()
        (self.tools / "ab").symlink_to("c")
        self.assertNotEqual(second["changes_sha256"], workspace._revision(self.tools)["changes_sha256"])

    def test_missing_or_changed_setup_receipt_has_actionable_error(self):
        self.init()
        with self.assertRaisesRegex(workspace.WorkspaceError, "run setup"):
            workspace.doctor(self.path)
        config = workspace._configuration(self.path)
        (self.path / "setup.json").write_text(json.dumps({"workspace_id": config["workspace_id"], "dependencies": {}}))
        with patch.object(workspace, "_dependency_inputs", return_value={"lock": "changed"}):
            with self.assertRaisesRegex(workspace.WorkspaceError, "run setup"):
                workspace.doctor(self.path)


@unittest.skipUnless(autoform_cli and fca, "shared FCA/Autoform environment required")
class WorkspaceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / "workspace"
        self.shards = self.root / "annotations"
        self.shards.mkdir()
        self.record = {"version": 2, "source": {"type": "generic", "uri": "../source.md"},
                       "target": "urn:lean:Fixture.result", "location": {"opaque": [1, 2]},
                       "extensions": {"test": {"large": 2**100}}}
        (self.shards / "record.json").write_text(json.dumps(self.record))
        workspace.initialize(self.path, autoform_root=Path(autoform_cli.__file__).parents[1],
                             fca_root=Path(fca.__file__).parents[2], shards=[self.shards])
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        tools = {name: {"commit": "fixture", "dirty": False} for name in ("autoform", "fca", "wikilean")}
        self.stack.enter_context(patch.object(workspace, "doctor", return_value={"tools": tools, "python": "3.12"}))
        self.stack.enter_context(patch.object(workspace, "_tool_revisions", return_value=tools))

    def test_sync_captures_both_consumers_and_final_origins(self):
        report = workspace.synchronize(self.path)
        selected = workspace.current_generation(self.path)
        self.assertEqual(selected.name, report["generation"])
        self.assertEqual(report["annotations"], 1)
        from brain.ingest.companion import iter_annotations
        row, = iter_annotations(selected / "index")
        self.assertEqual(row["annotation"], self.record)
        self.assertEqual(row["origin"]["base_uri"], (self.shards / "record.json").as_uri())
        self.assertEqual(row["source"], (self.root / "source.md").as_uri())
        self.assertTrue(Path(report["catalog_path"]).is_dir())

    def test_failed_refresh_retains_previous_complete_snapshot(self):
        workspace.synchronize(self.path)
        previous = workspace.current_generation(self.path)
        database = (previous / "index/brain.sqlite3").read_bytes()
        (self.shards / "bad.json").write_text('{"version":2}')
        with self.assertRaises(workspace.WorkspaceError):
            workspace.synchronize(self.path)
        self.assertEqual(workspace.current_generation(self.path), previous)
        self.assertEqual((previous / "index/brain.sqlite3").read_bytes(), database)
        self.assertFalse((self.path / ".workspace.lock").exists())

    def test_empty_refresh_fails_without_selecting_a_generation(self):
        (self.shards / "record.json").unlink()
        with self.assertRaisesRegex(workspace.WorkspaceError, "No annotations"):
            workspace.synchronize(self.path)
        self.assertIsNone(workspace.current_generation(self.path))

    def test_catalog_and_index_share_capture_even_if_source_changes(self):
        from autoform_cli import companion_sources
        actual = companion_sources.write_annotation_catalog

        def change_source(collection, **kwargs):
            (self.shards / "record.json").write_text("invalid after capture")
            return actual(collection, **kwargs)

        with patch.object(companion_sources, "write_annotation_catalog", side_effect=change_source):
            report = workspace.synchronize(self.path)
        self.assertEqual(report["annotations"], 1)
        archive = Path(report["catalog_path"]) / "records.jsonl"
        self.assertEqual(json.loads(archive.read_text())["annotation"], self.record)

    def test_record_mismatch_cannot_become_current(self):
        from autoform_cli import companion_sources
        actual = companion_sources.write_annotation_catalog

        def change_archive(collection, **kwargs):
            result = actual(collection, **kwargs)
            (Path(kwargs["output"]) / "records.jsonl").write_text("")
            return result

        with patch.object(companion_sources, "write_annotation_catalog", side_effect=change_archive):
            with self.assertRaisesRegex(workspace.WorkspaceError, "same captured occurrences"):
                workspace.synchronize(self.path)
        self.assertIsNone(workspace.current_generation(self.path))

    def _attach_consumer(self):
        from autoform_cli.annotations import AnnotationExport
        consumer = self.root / "consumer"
        (consumer / "blueprint/roadmap").mkdir(parents=True)
        (consumer / "blueprint/roadmap/README.md").write_text("# Roadmap\n")
        config = workspace._configuration(self.path)
        config["consumer"] = {"path": str(consumer), "repo": "https://github.com/example/fixture",
                              "commit": "a" * 40, "attach_sources": True}
        (self.path / "workspace.json").write_text(json.dumps(config))
        self.stack.enter_context(patch("autoform_cli.annotations.export_annotations", return_value=AnnotationExport(
            (), ({"code": "NO_ASSOCIATIONS"},), config["consumer"]["repo"], "a" * 40)))
        return consumer

    def test_failure_before_selection_removes_only_new_attached_catalog(self):
        consumer = self._attach_consumer()
        workspace.synchronize(self.path)
        previous = workspace.current_generation(self.path)
        old = set((consumer / "blueprint/sources").iterdir())
        with patch.object(workspace, "_select", side_effect=RuntimeError("before selection")):
            with self.assertRaisesRegex(RuntimeError, "before selection"):
                workspace.synchronize(self.path)
        self.assertEqual(set((consumer / "blueprint/sources").iterdir()), old)
        self.assertEqual(workspace.current_generation(self.path), previous)

    def test_interrupt_after_selection_preserves_active_catalog(self):
        self._attach_consumer()
        actual = workspace._select

        def interrupt_after_select(*args):
            actual(*args)
            raise KeyboardInterrupt()

        with patch.object(workspace, "_select", side_effect=interrupt_after_select):
            with self.assertRaises(KeyboardInterrupt):
                workspace.synchronize(self.path)
        selected = workspace.current_generation(self.path)
        report = json.loads((selected / "generation.json").read_text())
        self.assertTrue(Path(report["catalog_path"]).is_dir())
        self.assertTrue((selected / "index/brain.sqlite3").is_file())
        self.assertTrue((selected / "interruption.json").is_file())

    def test_blueprint_path_consumer_attaches_at_the_real_sources_directory(self):
        consumer = self._attach_consumer()
        config = workspace._configuration(self.path)
        config["consumer"]["path"] = str(consumer / "blueprint")
        (self.path / "workspace.json").write_text(json.dumps(config))
        result = workspace.synchronize(self.path)
        self.assertEqual(Path(result["catalog_path"]).parent, consumer / "blueprint/sources")

    def test_attached_catalogs_cannot_be_inside_an_input_root(self):
        consumer = self._attach_consumer()
        workspace.synchronize(self.path)
        previous = workspace.current_generation(self.path)
        existing = set((consumer / "blueprint/sources").iterdir())
        config = workspace._configuration(self.path)
        config["consumer"]["path"] = str(consumer / "blueprint")
        config["shards"] = [str(consumer / "blueprint/sources")]
        (self.path / "workspace.json").write_text(json.dumps(config))
        for _ in range(2):
            with self.assertRaisesRegex(workspace.WorkspaceError, "narrower input"):
                workspace.synchronize(self.path)
            self.assertEqual(workspace.current_generation(self.path), previous)
            self.assertEqual(set((consumer / "blueprint/sources").iterdir()), existing)

    def test_narrow_source_input_stays_refreshable_with_attached_catalogs(self):
        consumer = self._attach_consumer()
        external = consumer / "blueprint/sources/external"
        external.mkdir(parents=True)
        (external / "link.json").write_text(json.dumps(self.record))
        config = workspace._configuration(self.path)
        config["shards"] = [str(external)]
        (self.path / "workspace.json").write_text(json.dumps(config))
        for _ in range(2):
            result = workspace.synchronize(self.path)
            self.assertEqual(result["annotations"], 1)
            self.assertTrue(Path(result["catalog_path"]).is_dir())


if __name__ == "__main__":
    if "--require-integrations" in sys.argv:
        sys.argv.remove("--require-integrations")
        if not (autoform_cli and fca):
            raise SystemExit("Use the shared companion workspace environment")
    unittest.main()
