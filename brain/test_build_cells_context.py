#!/usr/bin/env python3
"""Hermetic routing and publication tests for the sealed cells stage."""
from __future__ import annotations

import copy
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

import build_cells  # noqa: E402
import build_context  # noqa: E402
from test_build_context import _document  # noqa: E402


COMMIT_A = "1" * 40
COMMIT_B = "2" * 40
SOURCE_MANIFEST_ID = "sha256:" + "d" * 64

CELL_BINDINGS = {
    "bot-brain-queue": "bot/state/brain_queue.json",
    "bot-cut-log": "bot/state/cut_log.json",
    "bot-pool-candidates": "bot/state/pool_candidates.json",
    "bot-recycle-queue": "bot/state/recycle_queue.json",
    "bot-seed-queue": "bot/state/seed_queue.json",
    "brain-discovery-rejected": "brain/data/discovery_rejected.jsonl",
    "grounding-overrides": "catalog/data/grounding_overrides.jsonl",
}


class CellsContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve() / "replay"
        for root in ("code", "input", "output", "scratch"):
            path = self.base / root
            path.mkdir(parents=True)
            path.chmod(0o700)

        self.output_root = self.base / "output"
        self.scratch_root = self.base / "scratch"
        self.input_root = self.base / "input/repo"
        self.input_root.mkdir(parents=True)
        self.base_outputs = {
            name: self.output_root / f"brain/data/{name}.jsonl"
            for name in ("nodes", "edges", "edges_links")
        }
        for path in self.base_outputs.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.parent.chmod(0o700)
            path.write_text(
                json.dumps(
                    {"_meta": {"generated_at": "sha256:" + "9" * 64}}
                )
                + "\n",
                encoding="utf-8",
            )

        self.host = Path(self.temp.name).resolve() / "host"
        host_data = self.host / "brain/data"
        host_data.mkdir(parents=True)
        for name in ("nodes", "edges", "edges_links"):
            (host_data / f"{name}.jsonl").write_text(
                "not context data\n", encoding="utf-8"
            )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _binding(self, input_id: str, *, present: bool) -> dict:
        logical_path = CELL_BINDINGS[input_id]
        path = self.input_root.joinpath(*logical_path.split("/"))
        members = []
        if present:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "[]\n" if logical_path.endswith(".json") else "",
                encoding="utf-8",
            )
            raw = path.read_bytes()
            members.append(
                {
                    "bytes": len(raw),
                    "materialized_path": str(path),
                    "media_type": (
                        "application/json"
                        if logical_path.endswith(".json")
                        else "application/x-ndjson"
                    ),
                    "object": input_id,
                    "path": logical_path,
                    "pin": {
                        "tree": COMMIT_B,
                        "type": "git_commit",
                        "value": COMMIT_A,
                    },
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "source_manifest_id": SOURCE_MANIFEST_ID,
                }
            )
        return {
            "cardinality": "one",
            "class": "curated_git_input",
            "input_id": input_id,
            "members": members,
            "path": logical_path,
            "requirement": "optional",
            "root": "repo",
            "state": "present" if present else "absent",
        }

    def context(
        self,
        *,
        present: set[str] = frozenset(),
        stage_changes: dict | None = None,
    ) -> build_context.BuildContext:
        document = copy.deepcopy(_document(self.base))
        document["bindings"] = [
            self._binding(input_id, present=input_id in present)
            for input_id in sorted(CELL_BINDINGS)
        ]
        document["configuration"] = {
            "cell_attach_kinds": ["invocation", "related"],
            "external_node_cap": 123,
            "layout": {"enabled": False, "iterations": 37},
            "schema": "wikilean.brain-reducer-config/v1",
        }
        document["replay"]["reducer"]["configuration_sha256"] = hashlib.sha256(
            build_context.canonical_json_bytes(document["configuration"])
        ).hexdigest()
        if stage_changes:
            stage = next(item for item in document["stages"] if item["id"] == "cells")
            stage.update(stage_changes)
        document["generation_id"] = build_context.generation_identity(document)
        return build_context.BuildContext.from_document(document)

    def _patch_host_paths(self):
        return mock.patch.multiple(
            build_cells,
            ROOT=self.host,
            BRAIN_DATA=self.host / "brain/data",
            CATALOG_DATA=self.host / "catalog/data",
            BOT_STATE=self.host / "bot/state",
            NODES_IN=self.host / "brain/data/nodes.jsonl",
            EDGES_IN=self.host / "brain/data/edges.jsonl",
            EDGES_LINKS_IN=self.host / "brain/data/edges_links.jsonl",
            REJECTED_IN=self.host / "brain/data/discovery_rejected.jsonl",
            OVERRIDES_IN=self.host / "catalog/data/grounding_overrides.jsonl",
        )

    def _run_context_cli(self, context: build_context.BuildContext) -> None:
        context_path = self.base / "build-context.json"
        context_path.write_bytes(
            build_context.canonical_json_bytes(context.to_document())
        )
        context_path.chmod(0o444)
        build_cells.main(
            [
                "--build-context",
                str(context_path),
                "--stage-id",
                "cells",
            ]
        )

    def test_absent_optional_inputs_never_discover_host_paths(self) -> None:
        context = self.context()
        previous_umask = os.umask(0o777)
        try:
            with self._patch_host_paths():
                self._run_context_cli(context)
        finally:
            os.umask(previous_umask)

        outputs = tuple(
            self.output_root / relative
            for relative in (
                "brain/data/cell_review.jsonl",
                "brain/data/cells.jsonl",
                "brain/data/synapses.jsonl",
            )
        )
        self.assertEqual(
            tuple(path.relative_to(self.output_root).as_posix() for path in outputs),
            (
                "brain/data/cell_review.jsonl",
                "brain/data/cells.jsonl",
                "brain/data/synapses.jsonl",
            ),
        )
        for path in outputs:
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            meta = json.loads(path.read_text(encoding="utf-8").splitlines()[0])["_meta"]
            self.assertEqual(meta["generated_at"], context.generation_id)
            self.assertEqual(meta["generation_id"], context.generation_id)
        self.assertEqual(outputs[0].parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(
            sorted(
                path.relative_to(self.output_root).as_posix()
                for path in self.output_root.rglob("*")
                if path.is_file()
            ),
            [
                "brain/data/cell_review.jsonl",
                "brain/data/cells.jsonl",
                "brain/data/edges.jsonl",
                "brain/data/edges_links.jsonl",
                "brain/data/nodes.jsonl",
                "brain/data/synapses.jsonl",
            ],
        )
        self.assertFalse((self.scratch_root / "cells/publish").exists())
        self.assertFalse((self.host / "brain/data/cells.jsonl").exists())

    def test_context_routes_every_input_and_configuration_value(self) -> None:
        context = self.context(present=set(CELL_BINDINGS))
        fake_meta = {
            "schema": "brain/SCHEMA.md#v3",
            "generated_at": context.generation_id,
            "generation_id": context.generation_id,
            "base_generated_at": "base",
            "prov": [],
            "counts": {
                "cells": 0,
                "synapses": 0,
                "organs": 0,
                "multi_organ_cells": 0,
                "largest_cell": 0,
            },
            "stats": {},
        }
        with self._patch_host_paths(), mock.patch.object(
            build_cells,
            "build",
            return_value=([], [], fake_meta, []),
        ) as reducer:
            build_cells.build_cells_from_context(context)

        kwargs = reducer.call_args.kwargs
        self.assertFalse(kwargs["do_layout"])
        self.assertEqual(kwargs["layout_iterations"], 37)
        self.assertEqual(kwargs["attach_kinds"], ("invocation", "related"))
        self.assertTrue(kwargs["strict_inputs"])
        self.assertEqual(kwargs["generated_at"], context.generation_id)
        self.assertEqual(kwargs["generation_id"], context.generation_id)
        paths = kwargs["input_paths"]
        self.assertEqual(paths.nodes, self.base_outputs["nodes"])
        self.assertEqual(paths.edges, self.base_outputs["edges"])
        self.assertEqual(paths.edges_links, self.base_outputs["edges_links"])
        for input_id, logical_path in CELL_BINDINGS.items():
            field = input_id.replace("-", "_")
            if field == "brain_discovery_rejected":
                field = "discovery_rejected"
            self.assertEqual(
                getattr(paths, field),
                self.input_root.joinpath(*logical_path.split("/")),
            )

    def test_context_refuses_existing_output_before_reduction(self) -> None:
        context = self.context()
        output = self.output_root / "brain/data/cells.jsonl"
        output.write_bytes(b"competitor")
        with mock.patch.object(build_cells, "build") as reducer:
            with self.assertRaisesRegex(FileExistsError, "already exists"):
                build_cells.build_cells_from_context(context)
        reducer.assert_not_called()
        self.assertEqual(output.read_bytes(), b"competitor")

    def test_strict_bound_inputs_cannot_disappear_silently(self) -> None:
        missing = self.base / "missing.json"
        with self.assertRaises(FileNotFoundError):
            list(build_cells._iter_jsonl(missing, required=True))
        with self.assertRaisesRegex(ValueError, "invalid sealed cut log"):
            build_cells._load_rejected(
                cut_log_path=missing,
                discovery_rejected_path=None,
                strict=True,
            )
        with self.assertRaisesRegex(ValueError, "invalid sealed tag queue"):
            build_cells._load_tag_queue(
                (("seed_queue.json", missing),),
                strict=True,
            )

    def test_context_logging_failure_happens_before_publication(self) -> None:
        class FailingStderr:
            def write(self, value: str) -> int:
                if value.startswith("writing brain/data/"):
                    raise OSError("injected summary failure")
                return len(value)

            def flush(self) -> None:
                return None

        context = self.context()
        with mock.patch.object(
            build_cells.sys, "stderr", FailingStderr()
        ), self.assertRaisesRegex(OSError, "injected summary"):
            build_cells.build_cells_from_context(context)
        self.assertFalse(
            (self.output_root / "brain/data/cells.jsonl").exists()
        )

    def test_context_rejects_stage_contract_drift(self) -> None:
        cases = (
            ({"program": "brain/not-cells.py"}, "program is"),
            ({"argv": ["--stats"]}, "argv is"),
            ({"needs": []}, "needs are"),
            (
                {
                    "outputs": [
                        {"kind": "file", "path": "brain/data/cell_audit.jsonl"},
                        {"kind": "file", "path": "brain/data/cells.jsonl"},
                        {"kind": "file", "path": "brain/data/synapses.jsonl"},
                    ]
                },
                "outputs are",
            ),
        )
        for changes, message in cases:
            with self.subTest(changes=changes):
                context = self.context(stage_changes=changes)
                with self.assertRaisesRegex(build_context.BuildContextError, message):
                    build_cells.build_cells_from_context(context)

    def test_legacy_cli_still_controls_layout_attach_and_stats(self) -> None:
        fake_meta = {"counts": {"cells": 0}, "stats": {}}
        with mock.patch.object(
            build_cells, "build", return_value=([], [], fake_meta, [])
        ) as reducer, mock.patch.object(build_cells, "write_jsonl") as writer:
            self.assertIsNone(
                build_cells.main(
                    ["--no-layout", "--attach", "invocation", "--stats"]
                )
            )
        reducer.assert_called_once_with(
            do_layout=False,
            attach_kinds=("invocation",),
        )
        writer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
