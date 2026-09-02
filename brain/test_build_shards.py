#!/usr/bin/env python3
"""Hermetic ownership and rollback tests for the top-level Brain assets."""
from __future__ import annotations

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


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    )
    raise SystemExit(0 if result.wasSuccessful() else 1)
