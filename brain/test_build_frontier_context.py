#!/usr/bin/env python3
"""Hermetic routing and publication tests for the sealed Frontier stage."""
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

import build_context  # noqa: E402
import build_frontier  # noqa: E402
from test_build_context import _document  # noqa: E402


COMMIT = "a" * 40
TREE = "b" * 40
SOURCE_MANIFEST_ID = "sha256:" + "d" * 64
INPUTS = {
    "brain-frontier-suitability-overrides": {
        "class": "curated_git_input",
        "path": "brain/data/frontier_suitability_overrides.jsonl",
        "requirement": "required",
    },
    "concept-layer": {
        "class": "immutable_source_object",
        "path": "catalog/data/concept_layer.jsonl",
        "requirement": "optional",
    },
}


class FrontierContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.base = self.root / "replay"
        for name in ("code", "input", "output", "scratch"):
            path = self.base / name
            path.mkdir(parents=True)
            path.chmod(0o700)
        self.output_data = self.base / "output/brain/data"
        self.output_data.mkdir(parents=True)
        self.output_data.chmod(0o700)
        self._write_dependencies()

        self.host = self.root / "host"
        (self.host / "brain/data").mkdir(parents=True)
        (self.host / "catalog/data").mkdir(parents=True)
        (self.host / "manage/data").mkdir(parents=True)
        self.host.joinpath("manage/data/halo.json").write_text(
            '{"items":[{"all_frac":0.25,"cell":"cell:Q1"}]}\n',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _jsonl(meta: dict, rows: list[dict]) -> str:
        return "\n".join(
            [json.dumps({"_meta": meta}, separators=(",", ":"))]
            + [json.dumps(row, separators=(",", ":")) for row in rows]
        ) + "\n"

    def _write_dependencies(self) -> None:
        self.output_data.joinpath("nodes.jsonl").write_text(
            self._jsonl(
                {"generated_at": "ambient-base-stamp"},
                [
                    {
                        "altitude_evidence": {"p31": []},
                        "display": {"status": "not_formalized"},
                        "id": "Q1",
                        "type": "concept",
                        "unit": {"decls": []},
                    }
                ],
            ),
            encoding="utf-8",
        )
        self.output_data.joinpath("edges.jsonl").write_text(
            self._jsonl({"generated_at": "ambient-base-stamp"}, []),
            encoding="utf-8",
        )
        cells = [
            {
                "id": "cell:Q1",
                "label": "Alpha",
                "organs": [
                    {"id": "Q1", "kind": "concept"},
                    {"db": "nlab", "id": "alpha", "kind": "page"},
                ],
                "supercells": [],
            },
            {
                "id": "cell:formal",
                "label": "Formal",
                "organs": [{"id": "decl:Mathlib:Fixture.foo", "kind": "decl"}],
                "supercells": ["path:Mathlib/Algebra"],
            },
        ]
        self.output_data.joinpath("cells.jsonl").write_text(
            self._jsonl({"generated_at": "ambient-cell-stamp"}, cells),
            encoding="utf-8",
        )
        self.output_data.joinpath("synapses.jsonl").write_text(
            self._jsonl(
                {"generated_at": "ambient-cell-stamp"},
                [
                    {
                        "dst": "cell:formal",
                        "kinds": {"depends": 2},
                        "src": "cell:Q1",
                        "weight": 2,
                    }
                ],
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _input_bytes(input_id: str) -> bytes:
        if input_id == "brain-frontier-suitability-overrides":
            return b'{"_meta":{"fixture":true}}\n'
        if input_id == "concept-layer":
            return (
                b'{"primary_decl":null,"qid":"Q1",'
                b'"secondary_decls":["Fixture.foo"]}\n'
            )
        raise AssertionError(input_id)

    def _binding(self, input_id: str, *, present: bool) -> dict:
        contract = INPUTS[input_id]
        path = self.base / "input/repo" / contract["path"]
        members = []
        if present:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self._input_bytes(input_id))
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            pin = (
                {"tree": TREE, "type": "git_commit", "value": COMMIT}
                if contract["class"] == "curated_git_input"
                else {"type": "content_sha256", "value": digest}
            )
            members.append(
                {
                    "bytes": path.stat().st_size,
                    "materialized_path": str(path),
                    "media_type": (
                        "application/json"
                        if path.suffix == ".json"
                        else "application/x-ndjson"
                    ),
                    "object": input_id,
                    "path": contract["path"],
                    "pin": pin,
                    "sha256": digest,
                    "source_manifest_id": SOURCE_MANIFEST_ID,
                }
            )
        return {
            "cardinality": "one",
            "class": contract["class"],
            "input_id": input_id,
            "members": members,
            "path": contract["path"],
            "requirement": contract["requirement"],
            "root": "repo",
            "source_manifest_ids": [SOURCE_MANIFEST_ID],
            "state": "present" if present else "absent",
        }

    def context(
        self,
        *,
        present_optional: set[str] = frozenset(),
        stage_changes: dict | None = None,
    ) -> build_context.BuildContext:
        document = copy.deepcopy(_document(self.base))
        document["bindings"] = [
            self._binding(
                input_id,
                present=(
                    contract["requirement"] == "required"
                    or input_id in present_optional
                ),
            )
            for input_id, contract in sorted(INPUTS.items())
        ]
        if stage_changes:
            stage = next(item for item in document["stages"] if item["id"] == "frontier")
            stage.update(stage_changes)
        document["generation_id"] = build_context.generation_identity(document)
        return build_context.BuildContext.from_document(document)

    def _patch_host_paths(self):
        host_data = self.host / "brain/data"
        return mock.patch.multiple(
            build_frontier,
            CELLS_IN=host_data / "cells.jsonl",
            NODES_IN=host_data / "nodes.jsonl",
            SYNAPSES_IN=host_data / "synapses.jsonl",
            EDGES_IN=host_data / "edges.jsonl",
            OUT=host_data / "frontier.jsonl",
            GRAPH_OUT=host_data / "frontier_graph.json",
            REVIEW_OUT=host_data / "frontier_review.jsonl",
            SUITABILITY_OVERRIDES=host_data / "frontier_suitability_overrides.jsonl",
            SECONDARY_ONLY=self.host / "catalog/data/concept_layer.jsonl",
        )

    def _run_cli(self, context: build_context.BuildContext) -> int:
        context_path = self.base / "build-context.json"
        context_path.write_bytes(
            build_context.canonical_json_bytes(context.to_document())
        )
        context_path.chmod(0o444)
        return build_frontier._cli(
            ["--build-context", str(context_path), "--stage-id", "frontier"]
        )

    def test_context_cli_ignores_ambient_paths_and_publishes_stable_files(self) -> None:
        context = self.context()
        previous_umask = os.umask(0o777)
        try:
            with self._patch_host_paths():
                self.assertEqual(self._run_cli(context), 0)
        finally:
            os.umask(previous_umask)

        outputs = tuple(
            self.base / "output" / relative
            for _kind, relative in build_frontier.STAGE_OUTPUTS
        )
        self.assertEqual({path.stat().st_mode & 0o777 for path in outputs}, {0o644})
        for path in outputs:
            meta = json.loads(path.read_text(encoding="utf-8").splitlines()[0])["_meta"]
            self.assertEqual(meta["generated_at"], context.generation_id)
            self.assertEqual(meta["generation_id"], context.generation_id)
        rows = [
            json.loads(line)
            for line in outputs[0].read_text(encoding="utf-8").splitlines()[1:]
        ]
        self.assertEqual([row["id"] for row in rows], ["frontier:Algebra"])
        self.assertEqual(rows[0]["mean_stateability"], 1.0)
        meta = json.loads(
            outputs[0].read_text(encoding="utf-8").splitlines()[0]
        )["_meta"]
        self.assertEqual(meta["inputs"]["stateability_joined"], 1)
        self.assertNotIn("halo_joined", meta["inputs"])
        review_meta = json.loads(outputs[2].read_text().splitlines()[0])["_meta"]
        self.assertEqual(review_meta["counts"]["candidates"], 0)
        self.assertFalse((self.base / "scratch/frontier/publish").exists())
        self.assertFalse((self.host / "brain/data/frontier.jsonl").exists())

    def test_context_routes_every_dependency_and_declared_input(self) -> None:
        context = self.context(present_optional={"concept-layer"})

        def fake_build(**kwargs) -> int:
            for key in ("frontier_output", "graph_output", "review_output"):
                build_frontier.write_bytes_exclusive(
                    kwargs[key], b'{"_meta":{}}\n', mode=0o644
                )
            return 0

        with self._patch_host_paths(), mock.patch.object(
            build_frontier, "build_frontier", side_effect=fake_build
        ) as reducer:
            outputs = build_frontier.build_frontier_from_context(context)

        kwargs = reducer.call_args.kwargs
        self.assertEqual(kwargs["cells_path"], self.output_data / "cells.jsonl")
        self.assertEqual(kwargs["synapses_path"], self.output_data / "synapses.jsonl")
        self.assertEqual(kwargs["nodes_path"], self.output_data / "nodes.jsonl")
        self.assertEqual(kwargs["edges_path"], self.output_data / "edges.jsonl")
        self.assertEqual(
            kwargs["suitability_overrides_path"],
            self.base / "input/repo/brain/data/frontier_suitability_overrides.jsonl",
        )
        self.assertEqual(
            kwargs["secondary_only_path"],
            self.base / "input/repo/catalog/data/concept_layer.jsonl",
        )
        self.assertNotIn("halo_path", kwargs)
        self.assertTrue(kwargs["strict_inputs"])
        self.assertTrue(kwargs["sealed_outputs"])
        self.assertEqual(kwargs["generated_at"], context.generation_id)
        self.assertEqual(kwargs["generation_id"], context.generation_id)
        self.assertEqual(
            outputs,
            tuple(
                self.base / "output" / relative
                for _kind, relative in build_frontier.STAGE_OUTPUTS
            ),
        )

    def test_present_optional_and_required_dependencies_cannot_disappear(self) -> None:
        for missing in ("concept-layer",):
            with self.subTest(missing=missing):
                context = self.context(present_optional={"concept-layer"})
                context.require_one(missing).unlink()
                with mock.patch.object(build_frontier, "build_frontier") as reducer:
                    with self.assertRaisesRegex(FileNotFoundError, missing):
                        build_frontier.build_frontier_from_context(context)
                reducer.assert_not_called()
                self._input_path(missing).write_bytes(self._input_bytes(missing))

        for filename in ("cells.jsonl", "synapses.jsonl", "nodes.jsonl", "edges.jsonl"):
            with self.subTest(missing=filename):
                path = self.output_data / filename
                original = path.read_bytes()
                path.unlink()
                context = self.context()
                with mock.patch.object(build_frontier, "build_frontier") as reducer:
                    with self.assertRaisesRegex(FileNotFoundError, filename.split(".")[0]):
                        build_frontier.build_frontier_from_context(context)
                reducer.assert_not_called()
                path.write_bytes(original)

    def _input_path(self, input_id: str) -> Path:
        return self.base / "input/repo" / INPUTS[input_id]["path"]

    def test_context_refuses_preexisting_and_racing_outputs(self) -> None:
        context = self.context()
        existing = self.output_data / "frontier.jsonl"
        existing.write_bytes(b"competitor")
        with mock.patch.object(build_frontier, "build_frontier") as reducer:
            with self.assertRaisesRegex(FileExistsError, "already exists"):
                build_frontier.build_frontier_from_context(context)
        reducer.assert_not_called()
        self.assertEqual(existing.read_bytes(), b"competitor")

        existing.unlink()

        def fake_build(**kwargs) -> int:
            for key in ("frontier_output", "graph_output", "review_output"):
                build_frontier.write_bytes_exclusive(kwargs[key], b"sealed", mode=0o644)
            return 0

        real_publish = build_frontier.publish_files_no_replace

        def racing_publish(publications, *, scratch):
            pairs = tuple(publications)
            pairs[0][1].write_bytes(b"raced")
            return real_publish(pairs, scratch=scratch)

        with mock.patch.object(
            build_frontier, "build_frontier", side_effect=fake_build
        ), mock.patch.object(
            build_frontier,
            "publish_files_no_replace",
            side_effect=racing_publish,
        ), self.assertRaisesRegex(FileExistsError, "already exists"):
            build_frontier.build_frontier_from_context(context)
        self.assertEqual(existing.read_bytes(), b"raced")
        self.assertFalse((self.base / "scratch/frontier/publish").exists())

    def test_context_rejects_stage_contract_drift(self) -> None:
        cases = (
            ({"program": "brain/not-frontier.py"}, "program is"),
            ({"argv": ["--unexpected"]}, "argv is"),
            ({"needs": ["cells"]}, "needs are"),
            (
                {
                    "outputs": [
                        {"kind": "file", "path": "brain/data/frontier.jsonl"},
                        {"kind": "file", "path": "brain/data/frontier_graph.json"},
                    ]
                },
                "outputs are",
            ),
        )
        for changes, message in cases:
            with self.subTest(changes=changes):
                context = self.context(stage_changes=changes)
                with self.assertRaisesRegex(build_context.BuildContextError, message):
                    build_frontier.build_frontier_from_context(context)


if __name__ == "__main__":
    unittest.main()
