#!/usr/bin/env python3
"""Hermetic tests for the sealed-replay cell-shards stage."""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import build_cell_shards  # noqa: E402
import build_context  # noqa: E402
import stage_io  # noqa: E402
from test_build_context import _document  # noqa: E402
from test_store import make_fixture  # noqa: E402


class CellShardContextTest(unittest.TestCase):
    def make_case(self, root: Path) -> tuple[build_context.BuildContext, Path]:
        document = _document(root)
        context = build_context.BuildContext.from_document(document)
        for path in context.roots.to_document().values():
            Path(path).mkdir(parents=True)
        data_dir = context.roots.output / "brain/data"
        make_fixture(data_dir)
        frontier_meta = {
            "generated_at": context.generation_id,
            "generation_id": context.generation_id,
            "method": "fixture",
            "counts": {"homeless": 0, "assigned": 0, "unsorted": 0},
            "suitability": {
                "method": "fixture",
                "counts": {"candidate": 0, "deprioritized": 0, "reasons": {}},
            },
            "proximity": {
                "method": "fixture min(weight, direct)",
                "lambda": 0.25,
                "counts": {"direct": 0, "bridged": 0, "zero": 0},
            },
        }
        (data_dir / "frontier.jsonl").write_text(
            json.dumps(
                {"_meta": frontier_meta},
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        graph = {
            "_meta": {
                "counts": {"cells": 0, "edges": 0, "formal": 0, "libs": {}},
                "generated_at": context.generation_id,
                "generation_id": context.generation_id,
            },
            "cells": [],
            "edges": [],
            "formal": {},
        }
        graph_path = data_dir / "frontier_graph.json"
        graph_path.write_text(
            json.dumps(graph, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        return context, graph_path

    def make_homeless_frontier(
        self,
        context: build_context.BuildContext,
        graph_path: Path,
    ) -> tuple[Path, str]:
        """Turn cell:Q2 into a one-cell, valid sealed Frontier fixture."""
        data_dir = context.roots.output / "brain/data"
        cells_path = data_dir / "cells.jsonl"
        lines = cells_path.read_text(encoding="utf-8").splitlines()
        cell = json.loads(lines[2])
        self.assertEqual(cell["id"], "cell:Q2")
        cell["organs"] = [
            organ for organ in cell["organs"] if organ.get("kind") != "decl"
        ]
        cells_path.write_text(
            lines[0]
            + "\n"
            + lines[1]
            + "\n"
            + json.dumps(cell, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )

        frontier_path = data_dir / "frontier.jsonl"
        frontier_meta = {
            "generated_at": context.generation_id,
            "generation_id": context.generation_id,
            "method": "fixture",
            "counts": {"homeless": 1, "assigned": 1, "unsorted": 0},
            "suitability": {
                "method": "fixture",
                "counts": {"candidate": 1, "deprioritized": 0, "reasons": {}},
            },
            "proximity": {
                "method": "fixture min(weight, direct)",
                "lambda": 0.25,
                "counts": {"direct": 1, "bridged": 0, "zero": 0},
            },
        }
        row = {
            "id": "frontier:Fixture",
            "label": "Fixture frontier",
            "cells": [cell["id"]],
            "n": 1,
            "prox": {
                "db": [1], "dw": [2], "ib": [0], "iw": [0],
                "s": [2], "r": [0.5],
            },
            "suitability": {"candidate": [True], "reason": [None]},
            "near": "path:Mathlib",
            "mean_stateability": None,
            "top": [{"cell": cell["id"], "label": cell["label"], "score": 4}],
        }
        frontier_path.write_text(
            json.dumps({"_meta": frontier_meta}, separators=(",", ":"))
            + "\n"
            + json.dumps(row, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        graph = {
            "_meta": {
                "generated_at": context.generation_id,
                "generation_id": context.generation_id,
                "method": "fixture",
                "counts": {
                    "cells": 1,
                    "formal": 1,
                    "edges": 0,
                    "libs": {"Mathlib": 1},
                },
            },
            "cells": [cell["id"]],
            "formal": {cell["id"]: {"Mathlib": 2}},
            "edges": [],
        }
        graph_path.write_text(
            json.dumps(graph, separators=(",", ":")), encoding="utf-8"
        )
        return frontier_path, cell["id"]

    def test_context_builds_only_owned_tree_with_stable_modes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            context, graph_path = self.make_case(base)
            output = context.roots.output / "site/assets/brain/cells"
            context_path = base / "build-context.json"
            context_path.write_bytes(
                build_context.canonical_json_bytes(context.to_document())
            )
            context_path.chmod(0o444)

            previous_umask = os.umask(0o777)
            try:
                self.assertEqual(
                    build_cell_shards._cli(
                        [
                            "--build-context",
                            str(context_path),
                            "--stage-id",
                            "cell-shards",
                        ]
                    ),
                    0,
                )
            finally:
                os.umask(previous_umask)

            self.assertEqual(
                (output / "frontier_graph.json").read_bytes(),
                graph_path.read_bytes(),
            )
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)
            for path in output.rglob("*"):
                expected = 0o700 if path.is_dir() else 0o644
                self.assertEqual(path.stat().st_mode & 0o777, expected, path)
            self.assertFalse(
                (context.roots.scratch / "cell-shards/cells").exists()
            )

    def test_context_requires_frontier_outputs(self) -> None:
        for missing in ("frontier.jsonl", "frontier_graph.json"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as raw:
                context, _graph_path = self.make_case(Path(raw))
                (context.roots.output / "brain/data" / missing).unlink()
                with self.assertRaisesRegex(SystemExit, "missing required replay input"):
                    build_cell_shards.build_cell_shards_from_context(context)
                self.assertFalse(
                    (context.roots.output / "site/assets/brain/cells").exists()
                )

    def test_context_rejects_stale_frontier_partition_and_graph(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            context, graph_path = self.make_case(Path(raw))
            frontier_path, cell_id = self.make_homeless_frontier(context, graph_path)
            meta_line = frontier_path.read_text(encoding="utf-8").splitlines()[0]
            frontier_path.write_text(meta_line + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "sealed frontier partition"):
                build_cell_shards.build_cell_shards_from_context(context)
            self.assertFalse(
                (context.roots.output / "site/assets/brain/cells").exists()
            )

            frontier_path, cell_id = self.make_homeless_frontier(context, graph_path)
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
            graph["cells"] = []
            graph_path.write_text(
                json.dumps(graph, separators=(",", ":")), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "STALE frontier_graph"):
                build_cell_shards.build_cell_shards_from_context(context)
            self.assertFalse(
                (context.roots.output / "site/assets/brain/cells").exists()
            )

            frontier_path, cell_id = self.make_homeless_frontier(context, graph_path)
            row = json.loads(frontier_path.read_text(encoding="utf-8").splitlines()[1])
            row["id"] = "path:Mathlib"
            frontier_path.write_text(
                frontier_path.read_text(encoding="utf-8").splitlines()[0]
                + "\n"
                + json.dumps(row, separators=(",", ":"))
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "malformed area id"):
                build_cell_shards.build_cell_shards_from_context(context)
            self.assertFalse(
                (context.roots.output / "site/assets/brain/cells").exists()
            )

    def test_context_rejects_frontier_generation_mismatch(self) -> None:
        for artifact in ("frontier", "graph"):
            with self.subTest(artifact=artifact), tempfile.TemporaryDirectory() as raw:
                context, graph_path = self.make_case(Path(raw))
                frontier_path = context.roots.output / "brain/data/frontier.jsonl"
                if artifact == "frontier":
                    lines = frontier_path.read_text(encoding="utf-8").splitlines()
                    header = json.loads(lines[0])
                    header["_meta"]["generation_id"] = "sha256:" + "f" * 64
                    frontier_path.write_text(
                        json.dumps(header, separators=(",", ":")) + "\n",
                        encoding="utf-8",
                    )
                else:
                    graph = json.loads(graph_path.read_text(encoding="utf-8"))
                    graph["_meta"]["generated_at"] = "sha256:" + "f" * 64
                    graph_path.write_text(
                        json.dumps(graph, separators=(",", ":")),
                        encoding="utf-8",
                    )
                with self.assertRaisesRegex(ValueError, "generation mismatch"):
                    build_cell_shards.build_cell_shards_from_context(context)
                self.assertFalse(
                    (context.roots.output / "site/assets/brain/cells").exists()
                )

    def test_context_rejects_malformed_frontier_row_contract(self) -> None:
        mutations = {
            "n": lambda row: row.__setitem__("n", 2),
            "prox": lambda row: row["prox"].__setitem__("s", [3]),
            "suitability": lambda row: row["suitability"].__setitem__(
                "reason", ["broad_scope"]
            ),
        }
        messages = {
            "n": "n must equal",
            "prox": "incoherent score",
            "suitability": "invalid suitability",
        }
        for label, mutate in mutations.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as raw:
                context, graph_path = self.make_case(Path(raw))
                frontier_path, _cell_id = self.make_homeless_frontier(
                    context, graph_path
                )
                lines = frontier_path.read_text(encoding="utf-8").splitlines()
                row = json.loads(lines[1])
                mutate(row)
                frontier_path.write_text(
                    lines[0]
                    + "\n"
                    + json.dumps(row, separators=(",", ":"))
                    + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, messages[label]):
                    build_cell_shards.build_cell_shards_from_context(context)
                self.assertFalse(
                    (context.roots.output / "site/assets/brain/cells").exists()
                )

    def test_context_rejects_malformed_frontier_graph_contract(self) -> None:
        def bad_edge(graph: dict, _cell_id: str) -> None:
            graph["edges"] = [[0, 0, 1]]
            graph["_meta"]["counts"]["edges"] = 1

        def bad_formal(graph: dict, cell_id: str) -> None:
            graph["formal"][cell_id]["Mathlib"] = 0

        def bad_counts(graph: dict, _cell_id: str) -> None:
            graph["_meta"]["counts"]["formal"] = 2

        mutations = {
            "edges": bad_edge,
            "formal": bad_formal,
            "counts": bad_counts,
        }
        messages = {
            "edges": "edge metadata",
            "formal": "formal metadata",
            "counts": "_meta.counts",
        }
        for label, mutate in mutations.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as raw:
                context, graph_path = self.make_case(Path(raw))
                _frontier_path, cell_id = self.make_homeless_frontier(
                    context, graph_path
                )
                graph = json.loads(graph_path.read_text(encoding="utf-8"))
                mutate(graph, cell_id)
                graph_path.write_text(
                    json.dumps(graph, separators=(",", ":")), encoding="utf-8"
                )
                with self.assertRaisesRegex(ValueError, messages[label]):
                    build_cell_shards.build_cell_shards_from_context(context)
                self.assertFalse(
                    (context.roots.output / "site/assets/brain/cells").exists()
                )

    def test_context_refuses_preexisting_or_racing_output_tree(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            context, _graph_path = self.make_case(Path(raw))
            output = context.roots.output / "site/assets/brain/cells"
            output.mkdir(parents=True)
            (output / "competitor").write_bytes(b"first")
            with self.assertRaisesRegex(FileExistsError, "already exists"):
                build_cell_shards.build_cell_shards_from_context(context)
            self.assertEqual((output / "competitor").read_bytes(), b"first")

        with tempfile.TemporaryDirectory() as raw:
            context, _graph_path = self.make_case(Path(raw))
            output = context.roots.output / "site/assets/brain/cells"

            def race(_source: Path, destination: Path) -> None:
                destination.mkdir()
                (destination / "competitor").write_bytes(b"raced")
                raise FileExistsError("injected directory publication race")

            with mock.patch.object(
                stage_io, "_rename_no_replace", side_effect=race
            ), self.assertRaisesRegex(FileExistsError, "injected directory"):
                build_cell_shards.build_cell_shards_from_context(context)

            self.assertEqual((output / "competitor").read_bytes(), b"raced")
            self.assertFalse(
                (context.roots.scratch / "cell-shards/cells").exists()
            )

    def test_context_rolls_back_tree_when_parent_sync_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            context, _graph_path = self.make_case(Path(raw))
            output = context.roots.output / "site/assets/brain/cells"
            real_fsync_directory = stage_io.fsync_directory
            failed = False
            calls: list[Path] = []

            def fail_after_publish(path: Path) -> None:
                nonlocal failed
                calls.append(Path(path).resolve())
                if (
                    not failed
                    and Path(path).resolve() == output.parent.resolve()
                    and output.exists()
                ):
                    failed = True
                    raise OSError("injected tree directory fsync failure")
                real_fsync_directory(path)

            with mock.patch.object(
                stage_io, "fsync_directory", side_effect=fail_after_publish
            ), self.assertRaisesRegex(OSError, "injected tree"):
                build_cell_shards.build_cell_shards_from_context(context)

            self.assertTrue(failed)
            self.assertFalse(output.exists())
            self.assertFalse(
                (context.roots.scratch / "cell-shards/cells").exists()
            )
            self.assertIn(context.roots.scratch.resolve(), calls)

    def test_directory_publisher_never_publishes_replaced_scratch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            scratch_root = base / "scratch"
            output_root = base / "output"
            scratch_root.mkdir(mode=0o700)
            output_root.mkdir(mode=0o700)
            source = scratch_root / "owned"
            ownership = stage_io.create_owned_directory(scratch_root, source)
            (source / "owned.json").write_bytes(b"owned")
            orphan = scratch_root / "orphan"
            source.rename(orphan)
            source.mkdir(mode=0o700)
            (source / "competitor.json").write_bytes(b"competitor")

            with self.assertRaisesRegex(RuntimeError, "replaced stage directory"):
                stage_io.publish_directory_no_replace(
                    ownership,
                    output_root / "published",
                )

            self.assertEqual((source / "competitor.json").read_bytes(), b"competitor")
            self.assertEqual((orphan / "owned.json").read_bytes(), b"owned")
            self.assertFalse((output_root / "published").exists())

    def test_directory_publisher_rejects_symlinks_inside_owned_tree(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            scratch_root = base / "scratch"
            output_root = base / "output"
            scratch_root.mkdir(mode=0o700)
            output_root.mkdir(mode=0o700)
            source = scratch_root / "owned"
            ownership = stage_io.create_owned_directory(scratch_root, source)
            outside = base / "outside"
            outside.mkdir()
            (source / "escape").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(OSError, "non-directory"):
                stage_io.publish_directory_no_replace(
                    ownership,
                    output_root / "published",
                )

            self.assertTrue((source / "escape").is_symlink())
            self.assertFalse((output_root / "published").exists())

    def test_directory_rollback_never_deletes_a_racing_competitor(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            scratch_root = base / "scratch"
            output_root = base / "output"
            scratch_root.mkdir(mode=0o700)
            output_root.mkdir(mode=0o700)
            source = scratch_root / "owned"
            ownership = stage_io.create_owned_directory(scratch_root, source)
            (source / "owned.json").write_bytes(b"owned")
            output = output_root / "published"
            aside = output_root / "owned-aside"
            real_rename = stage_io._rename_no_replace
            rename_calls = 0

            def swap_on_rollback(first: Path, second: Path) -> None:
                nonlocal rename_calls
                rename_calls += 1
                if rename_calls == 2:
                    real_rename(first, aside)
                    first.mkdir(mode=0o700)
                    (first / "competitor.json").write_bytes(b"competitor")
                real_rename(first, second)

            real_fsync = stage_io.fsync_directory
            failed = False

            def fail_publish_sync(path: Path) -> None:
                nonlocal failed
                if not failed and Path(path) == output_root and output.exists():
                    failed = True
                    raise OSError("injected publish sync failure")
                real_fsync(path)

            with mock.patch.object(
                stage_io, "_rename_no_replace", side_effect=swap_on_rollback
            ), mock.patch.object(
                stage_io, "fsync_directory", side_effect=fail_publish_sync
            ), self.assertRaisesRegex(OSError, "injected publish sync"):
                stage_io.publish_directory_no_replace(ownership, output)

            self.assertFalse(output.exists())
            self.assertEqual(
                (source / "competitor.json").read_bytes(), b"competitor"
            )
            self.assertEqual((aside / "owned.json").read_bytes(), b"owned")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO requires POSIX")
    def test_directory_publisher_rejects_fifo_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            scratch_root = base / "scratch"
            output_root = base / "output"
            scratch_root.mkdir(mode=0o700)
            output_root.mkdir(mode=0o700)
            source = scratch_root / "owned"
            ownership = stage_io.create_owned_directory(scratch_root, source)
            os.mkfifo(source / "fifo", mode=0o600)

            with self.assertRaisesRegex(OSError, "not a regular file"):
                stage_io.publish_directory_no_replace(
                    ownership,
                    output_root / "published",
                )
            self.assertFalse((output_root / "published").exists())

    def test_directory_publisher_rejects_nondeterministic_modes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            scratch_root = base / "scratch"
            output_root = base / "output"
            scratch_root.mkdir(mode=0o700)
            output_root.mkdir(mode=0o700)
            source = scratch_root / "owned"
            ownership = stage_io.create_owned_directory(scratch_root, source)
            child = source / "private.json"
            child.write_bytes(b"{}")
            child.chmod(0o600)

            with self.assertRaisesRegex(OSError, "mode 0o644"):
                stage_io.publish_directory_no_replace(
                    ownership,
                    output_root / "published",
                )
            self.assertFalse((output_root / "published").exists())

    def test_context_logging_failure_happens_before_publication(self) -> None:
        class FailingStderr:
            def write(self, value: str) -> int:
                if value.startswith("shards:"):
                    raise OSError("injected summary failure")
                return len(value)

            def flush(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as raw:
            context, _graph_path = self.make_case(Path(raw))
            output = context.roots.output / "site/assets/brain/cells"
            with mock.patch.object(
                build_cell_shards.sys, "stderr", FailingStderr()
            ), self.assertRaisesRegex(OSError, "injected summary"):
                build_cell_shards.build_cell_shards_from_context(context)

            self.assertFalse(output.exists())
            self.assertFalse(
                (context.roots.scratch / "cell-shards/cells").exists()
            )

    def test_context_rejects_stage_contract_drift(self) -> None:
        cases = (
            ("program", "program", "brain/not-cell-shards.py", "program is"),
            ("argv", "argv", ["--unexpected"], "argv is"),
            ("needs", "needs", ["cells", "frontier"], "needs are"),
            (
                "outputs",
                "outputs",
                [{"kind": "tree", "path": "site/assets/brain/not-cells"}],
                "outputs are",
            ),
        )
        for label, field, value, message in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as raw:
                base = Path(raw)
                document = copy.deepcopy(_document(base))
                stage = next(
                    item for item in document["stages"] if item["id"] == "cell-shards"
                )
                stage[field] = value
                document["generation_id"] = build_context.generation_identity(document)
                context = build_context.BuildContext.from_document(document)
                for path in context.roots.to_document().values():
                    Path(path).mkdir(parents=True)
                with self.assertRaisesRegex(build_context.BuildContextError, message):
                    build_cell_shards.build_cell_shards_from_context(context)


if __name__ == "__main__":
    unittest.main()
