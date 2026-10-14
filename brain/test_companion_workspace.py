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
    import literature_links
except ImportError:
    autoform_cli = literature_links = None


class WorkspaceConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / "workspace"
        self.tools = self.root / "tools"
        self.tools.mkdir()

    def init(self, **kwargs):
        return workspace.initialize(self.path, autoform_root=self.tools, literature_links_root=self.tools, **kwargs)

    def test_existing_workspace_is_not_replaced(self):
        self.init()
        before = (self.path / "workspace.json").read_bytes()
        with self.assertRaisesRegex(workspace.WorkspaceError, "already exists"):
            self.init()
        self.assertEqual(before, (self.path / "workspace.json").read_bytes())

    def test_consumer_needs_no_export_coordinates(self):
        result = self.init(consumer=self.root / "consumer")
        self.assertEqual(result["consumer"]["path"], str(self.root / "consumer"))

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

    def test_pre_release_configuration_has_actionable_error(self):
        self.init()
        config = json.loads((self.path / "workspace.json").read_text())
        config["fca_root"] = config.pop("literature_links_root")
        (self.path / "workspace.json").write_text(json.dumps(config))
        with self.assertRaisesRegex(workspace.WorkspaceError, "--literature-links-root"):
            workspace._configuration(self.path)

    def test_missing_or_changed_setup_receipt_has_actionable_error(self):
        self.init()
        with self.assertRaisesRegex(workspace.WorkspaceError, "run setup"):
            workspace.doctor(self.path)
        config = workspace._configuration(self.path)
        (self.path / "setup.json").write_text(json.dumps({"workspace_id": config["workspace_id"], "dependencies": {}}))
        with patch.object(workspace, "_dependency_inputs", return_value={"lock": "changed"}):
            with self.assertRaisesRegex(workspace.WorkspaceError, "run setup"):
                workspace.doctor(self.path)


@unittest.skipUnless(autoform_cli and literature_links, "shared Literature Links/Autoform environment required")
class WorkspaceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / "workspace"
        self.shards = self.root / "annotations"
        self.shards.mkdir()
        self.record = {"source": {"type": "generic", "uri": "../source.md"},
                       "target": "urn:lean:Fixture.result", "location": {"opaque": [1, 2]},
                       "extensions": {"test": {"large": 2**100}}}
        (self.shards / "record.json").write_text(json.dumps(self.record))
        workspace.initialize(self.path, autoform_root=Path(autoform_cli.__file__).parents[1],
                             literature_links_root=Path(literature_links.__file__).parents[2], shards=[self.shards])
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        tools = {name: {"commit": "fixture", "dirty": False} for name in ("autoform", "literature_links", "wikilean")}
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
        # A record without a target is valid (a checked absence); one without a source is not.
        (self.shards / "bad.json").write_text('{"target":"urn:missing-source"}')
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
        from autoform_cli import companions
        actual = companions.write_retained_annotations

        def change_source(collection, **kwargs):
            (self.shards / "record.json").write_text("invalid after capture")
            return actual(collection, **kwargs)

        with patch.object(companions, "write_retained_annotations", side_effect=change_source):
            report = workspace.synchronize(self.path)
        self.assertEqual(report["annotations"], 1)
        archive = Path(report["catalog_path"]) / "records.jsonl"
        self.assertEqual(json.loads(archive.read_text())["annotation"], self.record)

    def test_record_mismatch_cannot_become_current(self):
        from autoform_cli import companions
        actual = companions.write_retained_annotations

        def change_archive(collection, **kwargs):
            result = actual(collection, **kwargs)
            (Path(kwargs["output"]) / "records.jsonl").write_text("")
            return result

        with patch.object(companions, "write_retained_annotations", side_effect=change_archive):
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

    def test_failure_before_selection_preserves_native_config_and_current(self):
        consumer = self._attach_consumer()
        workspace.synchronize(self.path)
        previous = workspace.current_generation(self.path)
        config = consumer / ".autoform-companions.json"
        before = config.read_bytes()
        with patch.object(workspace, "_select", side_effect=RuntimeError("before selection")):
            with self.assertRaisesRegex(RuntimeError, "before selection"):
                workspace.synchronize(self.path)
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual(workspace.current_generation(self.path), previous)
        self.assertFalse((consumer / "blueprint/sources").exists())

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
        self.assertEqual(Path(result["catalog_path"]).name, "autoform-native")
        self.assertTrue((consumer / ".autoform-companions.json").is_file())

    def test_invalid_attachment_config_does_not_select_new_generation(self):
        consumer = self._attach_consumer()
        workspace.synchronize(self.path)
        previous = workspace.current_generation(self.path)
        config = consumer / ".autoform-companions.json"
        config.write_text("{broken json")
        with self.assertRaises(ValueError):
            workspace.synchronize(self.path)
        self.assertEqual(workspace.current_generation(self.path), previous)
        self.assertEqual(config.read_text(), "{broken json")

    def test_native_attachment_preserves_authored_roots_bindings_and_updates_once(self):
        consumer = self._attach_consumer()
        config_path = consumer / ".autoform-companions.json"
        authored = {"roots": [str(self.shards)], "bindings": [{"source": "urn:article", "article_id": "a", "sha256": "b"*64}]}
        config_path.write_text(json.dumps(authored))
        for _ in range(2):
            workspace.synchronize(self.path)
        result = json.loads(config_path.read_text())
        for key, value in authored.items():
            self.assertEqual(result[key], value)
        self.assertEqual(result["archives"], [str(self.path / "current/autoform-native/records.jsonl")])
        self.assertFalse((consumer / "blueprint/sources").exists())

    def test_native_article_records_are_read_without_conversion_and_keep_http_bases(self):
        article = self.root / "article.json"
        record = {"source": {"type": "latex", "uri": "../paper.tex"}, "target": "../formal.html#x", "location": {"label": "theorem:1"}, "extensions": {"opaque": [99]}}
        article.write_text(json.dumps({"schema_version": 4, "wikipedia_title":"Test", "revid":12345, "annotations": [{"status":"partial", "links":[{"id":"x","annotation":record,"base_uri":"https://example.net/shards/article.json"}], "unlinked": [], "legacy": {"private": "do not publish"}}]}))
        config = workspace._configuration(self.path)
        config["articles"] = [str(article)]
        (self.path / "workspace.json").write_text(json.dumps(config))
        result = workspace.synchronize(self.path)
        from brain.ingest.companion import iter_annotations
        rows = list(iter_annotations(Path(result["index"]["output"])))
        retained = next(row for row in rows if row["annotation"].get("extensions") == {"opaque": [99]})
        # The stored record is kept; the item's review status is projected onto it.
        self.assertEqual(retained["annotation"], {**record, "status": "partial"})
        self.assertEqual(retained["origin"]["base_uri"], "https://example.net/shards/article.json")
        self.assertEqual(retained["source"], "https://example.net/paper.tex")
        self.assertEqual(retained["target"], "https://example.net/formal.html#x")
        self.assertNotIn("do not publish", json.dumps(rows))

    def test_legacy_articles_require_explicit_migration_and_do_not_replace_current(self):
        workspace.synchronize(self.path)
        previous = workspace.current_generation(self.path)
        article = self.root / "old.json"
        article.write_text(json.dumps({"annotations": [{"status":"partial", "mathlib":{"decl":"X"}}]}))
        config = workspace._configuration(self.path); config["articles"] = [str(article)]
        (self.path / "workspace.json").write_text(json.dumps(config))
        with self.assertRaisesRegex(workspace.WorkspaceError, "explicit native migration"):
            workspace.synchronize(self.path)
        self.assertEqual(workspace.current_generation(self.path), previous)

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
        if not (autoform_cli and literature_links):
            raise SystemExit("Use the shared companion workspace environment")
    unittest.main()
