#!/usr/bin/env python3
"""Focused tests for the runtime-only sealed Brain build context."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tools"))

import authority_contracts as contracts  # noqa: E402
from build_context import (  # noqa: E402
    BUILD_CONTEXT_SCHEMA,
    GENERATION_DOMAIN,
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
DIGEST_A = "3" * 64
DIGEST_B = "4" * 64


def _member(
    root: Path,
    logical_path: str,
    *,
    digest: str = DIGEST_A,
    object_name: str = "normalized",
) -> dict:
    return {
        "bytes": 7,
        "materialized_path": str(root.joinpath(*logical_path.split("/"))),
        "media_type": "application/json",
        "object": object_name,
        "path": logical_path,
        "pin": {"tree": COMMIT_B, "type": "git_commit", "value": COMMIT_A},
        "sha256": digest,
        "source_manifest_id": HASH_D,
    }


def _stages() -> list[dict]:
    return [
        {
            "argv": ["--jsonl-only"],
            "id": "base-graph",
            "needs": [],
            "outputs": [
                {"kind": "file", "path": "brain/data/edges.jsonl"},
                {"kind": "file", "path": "brain/data/edges_links.jsonl"},
                {"kind": "file", "path": "brain/data/nodes.jsonl"},
            ],
            "program": "brain/build_snapshot.py",
        },
        {
            "argv": [],
            "id": "top-level-shards",
            "needs": ["base-graph"],
            "outputs": [
                {"kind": "file", "path": "site/assets/brain/sources.json"},
                {"kind": "file", "path": "site/assets/brain/xref_index.json"},
            ],
            "program": "brain/build_shards.py",
        },
        {
            "argv": [],
            "id": "cells",
            "needs": ["base-graph"],
            "outputs": [
                {"kind": "file", "path": "brain/data/cell_review.jsonl"},
                {"kind": "file", "path": "brain/data/cells.jsonl"},
                {"kind": "file", "path": "brain/data/synapses.jsonl"},
            ],
            "program": "brain/build_cells.py",
        },
        {
            "argv": ["--from-jsonl"],
            "id": "sqlite-with-cells",
            "needs": ["base-graph", "cells"],
            "outputs": [{"kind": "file", "path": "brain/data/brain.sqlite3"}],
            "program": "brain/build_snapshot.py",
        },
        {
            "argv": [],
            "id": "frontier",
            "needs": ["base-graph", "cells"],
            "outputs": [
                {"kind": "file", "path": "brain/data/frontier.jsonl"},
                {"kind": "file", "path": "brain/data/frontier_graph.json"},
                {"kind": "file", "path": "brain/data/frontier_review.jsonl"},
            ],
            "program": "brain/build_frontier.py",
        },
        {
            "argv": [],
            "id": "cell-shards",
            "needs": ["base-graph", "cells", "frontier"],
            "outputs": [{"kind": "tree", "path": "site/assets/brain/cells"}],
            "program": "brain/build_cell_shards.py",
        },
        {
            "argv": [],
            "id": "brain-page",
            "needs": [],
            "outputs": [{"kind": "file", "path": "site/out/brain.html"}],
            "program": "site/build_brain_page.py",
        },
    ]


def _document(base: Path) -> dict:
    roots = {
        "code": str(base / "code"),
        "input": str(base / "input"),
        "output": str(base / "output"),
        "scratch": str(base / "scratch"),
    }
    configuration = {
        "cell_attach_kinds": ["generalization", "special_case"],
        "external_node_cap": 8000,
        "layout": {"enabled": True, "iterations": 200},
        "schema": "wikilean.brain-reducer-config/v1",
    }
    config_digest = hashlib.sha256(canonical_json_bytes(configuration)).hexdigest()
    input_root = Path(roots["input"])
    document = {
        "audit": {"created_at": "2026-09-02T12:00:00Z", "note": "fixture"},
        "bindings": [
            {
                "cardinality": "one",
                "class": "curated_git_input",
                "input_id": "concept-graph",
                "members": [
                    _member(
                        input_root / "repo",
                        "catalog/data/concept_graph_v2.json",
                    )
                ],
                "path": "catalog/data/concept_graph_v2.json",
                "requirement": "required",
                "root": "repo",
                "state": "present",
            },
            {
                "cardinality": "one",
                "class": "immutable_source_object",
                "input_id": "declaration-oracle",
                "members": [],
                "path": "declaration-data.json",
                "requirement": "optional",
                "root": "decl_oracle",
                "state": "absent",
            },
            {
                "cardinality": "many",
                "class": "immutable_source_object",
                "input_id": "mathlib-source-tree",
                "members": [
                    _member(
                        input_root / "mathlib",
                        "Mathlib/A.lean",
                        object_name="mathlib-a",
                    ),
                    _member(
                        input_root / "mathlib",
                        "Mathlib/Algebra/B.lean",
                        digest=DIGEST_B,
                        object_name="mathlib-b",
                    ),
                ],
                "path_pattern": "Mathlib/**/*.lean",
                "requirement": "required",
                "root": "mathlib",
                "state": "present",
            },
        ],
        "configuration": configuration,
        "replay": {
            "authority": {
                "authority_root": HASH_A,
                "git_commit": COMMIT_A,
            },
            "offline_pack_id": HASH_B,
            "prior_state_root": None,
            "reducer": {
                "configuration_sha256": config_digest,
                "environment_sha256": DIGEST_B,
                "git_commit": COMMIT_B,
            },
            "reducer_inventory_id": HASH_C,
            "semantic_epoch": "brain-v3-current",
            "source_set_root": HASH_D,
        },
        "roots": roots,
        "schema": BUILD_CONTEXT_SCHEMA,
        "stages": _stages(),
    }
    document["generation_id"] = generation_identity(document)
    return document


class BuildContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.document = _document(self.base)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def context(self, document: dict | None = None) -> BuildContext:
        return BuildContext.from_document(document or self.document)

    def test_valid_context_exposes_exact_paths_and_stage_outputs(self) -> None:
        context = self.context()
        self.assertEqual(
            context.require_one("concept-graph"),
            (self.base / "input/repo/catalog/data/concept_graph_v2.json").resolve(),
        )
        self.assertIsNone(context.optional_one("declaration-oracle"))
        self.assertEqual(
            context.members("mathlib-source-tree"),
            (
                (self.base / "input/mathlib/Mathlib/A.lean").resolve(),
                (self.base / "input/mathlib/Mathlib/Algebra/B.lean").resolve(),
            ),
        )
        self.assertEqual(
            context.code("brain/build_snapshot.py"),
            (self.base / "code/brain/build_snapshot.py").resolve(),
        )
        self.assertEqual(
            context.output("brain/data/nodes.jsonl"),
            (self.base / "output/brain/data/nodes.jsonl").resolve(),
        )
        self.assertEqual(
            context.output("site/assets/brain/cells/traces/a.json"),
            (self.base / "output/site/assets/brain/cells/traces/a.json").resolve(),
        )
        self.assertEqual(context.stage("cells").program, "brain/build_cells.py")
        self.assertEqual(
            context.require_stage(
                "base-graph",
                program="brain/build_snapshot.py",
                argv=["--jsonl-only"],
                needs=[],
                outputs=[
                    ("file", "brain/data/edges.jsonl"),
                    ("file", "brain/data/edges_links.jsonl"),
                    ("file", "brain/data/nodes.jsonl"),
                ],
            ).id,
            "base-graph",
        )
        self.assertEqual(
            context.output_for("cells", "brain/data/cells.jsonl"),
            (self.base / "output/brain/data/cells.jsonl").resolve(),
        )
        self.assertEqual(
            context.dependency_output_for(
                "cells", "base-graph", "brain/data/nodes.jsonl"
            ),
            (self.base / "output/brain/data/nodes.jsonl").resolve(),
        )
        self.assertEqual(
            context.output_for("cell-shards", "site/assets/brain/cells/a.json"),
            (self.base / "output/site/assets/brain/cells/a.json").resolve(),
        )
        self.assertEqual(
            context.scratch_for("cells", "publish.tmp/cells.jsonl"),
            (self.base / "scratch/cells/publish.tmp/cells.jsonl").resolve(),
        )

    def test_runtime_schedule_matches_the_checked_in_inventory(self) -> None:
        inventory = json.loads(
            (HERE / "authority/reducer-inputs-v2.json").read_text(encoding="utf-8")
        )
        self.assertEqual(_stages(), inventory["stages"])

    def test_context_is_deeply_immutable(self) -> None:
        context = self.context()
        with self.assertRaises(FrozenInstanceError):
            context.generation_id = HASH_A  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            context.configuration.external_node_cap = 1  # type: ignore[misc]
        with self.assertRaises(TypeError):
            context.audit["note"] = "changed"  # type: ignore[index]
        self.assertIsInstance(context.configuration.cell_attach_kinds, tuple)

    def test_generation_identity_excludes_all_physical_paths_and_audit(self) -> None:
        first = self.context()
        relocated = copy.deepcopy(self.document)
        relocated["roots"] = {
            "code": str(self.base / "relocated-code"),
            "input": str(self.base / "relocated-input"),
            "output": str(self.base / "relocated-output"),
            "scratch": str(self.base / "relocated-scratch"),
        }
        for binding in relocated["bindings"]:
            for member in binding["members"]:
                member["materialized_path"] = str(
                    Path(relocated["roots"]["input"])
                    / binding["root"]
                    / member["path"]
                )
        relocated["audit"] = {"created_at": "2099-01-01T00:00:00Z", "note": "moved"}
        second = self.context(relocated)
        self.assertEqual(first.generation_id, second.generation_id)
        self.assertEqual(
            first.generation_id,
            contracts.domain_hash(GENERATION_DOMAIN, first.logical_document()),
        )

    def test_logical_binding_stage_and_configuration_changes_change_identity(self) -> None:
        for mutate in (
            lambda doc: doc["bindings"][0]["members"][0].update({"sha256": DIGEST_B}),
            lambda doc: doc["stages"][0].update({"argv": ["--fixture"]}),
            lambda doc: doc["configuration"].update({"external_node_cap": 17}),
        ):
            changed = copy.deepcopy(self.document)
            mutate(changed)
            self.assertNotEqual(
                generation_identity(changed),
                self.document["generation_id"],
            )

    def test_rejects_wrong_generation_and_configuration_digest(self) -> None:
        wrong_generation = copy.deepcopy(self.document)
        wrong_generation["generation_id"] = HASH_A
        with self.assertRaisesRegex(BuildContextError, r"generation_id.*expected"):
            self.context(wrong_generation)

        wrong_config = copy.deepcopy(self.document)
        wrong_config["configuration"]["external_node_cap"] = 17
        wrong_config["generation_id"] = generation_identity(wrong_config)
        with self.assertRaisesRegex(BuildContextError, "configuration_sha256"):
            self.context(wrong_config)

    def test_rejects_equal_and_ancestrally_overlapping_roots(self) -> None:
        for root_name, value in (
            ("scratch", self.document["roots"]["output"]),
            ("scratch", str(Path(self.document["roots"]["output"]) / "scratch")),
            ("output", "relative/output"),
        ):
            document = copy.deepcopy(self.document)
            document["roots"][root_name] = value
            document["generation_id"] = generation_identity(document)
            with self.subTest(root=root_name, value=value):
                with self.assertRaises(BuildContextError):
                    self.context(document)

    def test_rejects_materialized_member_outside_or_not_exactly_at_root_path(self) -> None:
        for materialized in (
            str(self.base / "elsewhere/concept_graph_v2.json"),
            str(self.base / "input/repo/catalog/data/other.json"),
        ):
            document = copy.deepcopy(self.document)
            document["bindings"][0]["members"][0]["materialized_path"] = materialized
            document["generation_id"] = generation_identity(document)
            with self.subTest(materialized=materialized):
                with self.assertRaisesRegex(BuildContextError, "materialized_path"):
                    self.context(document)

    def test_rejects_symlink_escape_beneath_an_input_root(self) -> None:
        repo_root = self.base / "input/repo"
        outside = self.base / "outside"
        repo_root.mkdir(parents=True)
        outside.mkdir()
        try:
            (repo_root / "catalog").symlink_to(outside, target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")
        document = copy.deepcopy(self.document)
        document["generation_id"] = generation_identity(document)
        with self.assertRaisesRegex(BuildContextError, "symlink components are forbidden"):
            self.context(document)

    def test_rejects_in_root_symlink_alias_across_output_owners(self) -> None:
        context = self.context()
        cells = self.base / "output/site/assets/brain/cells"
        cells.mkdir(parents=True)
        try:
            (cells / "alias").symlink_to("../xref_index.json")
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaisesRegex(BuildContextError, "symlink components are forbidden"):
            context.output_for(
                "cell-shards", "site/assets/brain/cells/alias"
            )

    def test_rejects_member_that_does_not_match_declared_path_or_pattern(self) -> None:
        exact = copy.deepcopy(self.document)
        exact["bindings"][0]["members"][0]["path"] = "catalog/data/other.json"
        exact["bindings"][0]["members"][0]["materialized_path"] = str(
            self.base / "input/repo/catalog/data/other.json"
        )
        exact["generation_id"] = generation_identity(exact)
        with self.assertRaisesRegex(BuildContextError, "does not match"):
            self.context(exact)

        pattern = copy.deepcopy(self.document)
        pattern["bindings"][2]["members"][0]["path"] = "Archive/A.lean"
        pattern["bindings"][2]["members"][0]["materialized_path"] = str(
            self.base / "input/mathlib/Archive/A.lean"
        )
        pattern["generation_id"] = generation_identity(pattern)
        with self.assertRaisesRegex(BuildContextError, "does not match"):
            self.context(pattern)

    def test_rejects_unsorted_duplicate_bindings_and_members(self) -> None:
        unsorted_bindings = copy.deepcopy(self.document)
        unsorted_bindings["bindings"][0], unsorted_bindings["bindings"][1] = (
            unsorted_bindings["bindings"][1],
            unsorted_bindings["bindings"][0],
        )
        unsorted_bindings["generation_id"] = generation_identity(unsorted_bindings)
        with self.assertRaisesRegex(BuildContextError, "sorted by input_id"):
            self.context(unsorted_bindings)

        unsorted_members = copy.deepcopy(self.document)
        unsorted_members["bindings"][2]["members"].reverse()
        unsorted_members["generation_id"] = generation_identity(unsorted_members)
        with self.assertRaisesRegex(BuildContextError, "sorted by path"):
            self.context(unsorted_members)

        duplicate = copy.deepcopy(self.document)
        repeated = copy.deepcopy(duplicate["bindings"][0])
        repeated["input_id"] = "concept-graph-copy"
        duplicate["bindings"].insert(1, repeated)
        duplicate["generation_id"] = generation_identity(duplicate)
        with self.assertRaisesRegex(BuildContextError, "bound more than once"):
            self.context(duplicate)

    def test_rejects_input_member_ancestry_collision_within_a_root(self) -> None:
        bad = copy.deepcopy(self.document)
        binding = next(
            item
            for item in bad["bindings"]
            if item["input_id"] == "mathlib-source-tree"
        )
        binding["members"][1]["path"] = "Mathlib/A.lean/Child.lean"
        binding["members"][1]["materialized_path"] = str(
            self.base / "input/mathlib/Mathlib/A.lean/Child.lean"
        )
        bad["generation_id"] = generation_identity(bad)
        with self.assertRaisesRegex(BuildContextError, "overlaps by ancestry"):
            self.context(bad)

    def test_rejects_invalid_presence_and_cardinality(self) -> None:
        required_absent = copy.deepcopy(self.document)
        required_absent["bindings"][0]["state"] = "absent"
        required_absent["bindings"][0]["members"] = []
        required_absent["generation_id"] = generation_identity(required_absent)
        with self.assertRaisesRegex(BuildContextError, "required inputs must be present"):
            self.context(required_absent)

        absent_with_member = copy.deepcopy(self.document)
        absent_with_member["bindings"][1]["state"] = "absent"
        absent_with_member["bindings"][1]["members"] = [
            _member(
                self.base / "input/decl_oracle",
                "declaration-data.json",
            )
        ]
        absent_with_member["generation_id"] = generation_identity(absent_with_member)
        with self.assertRaisesRegex(BuildContextError, "absent bindings"):
            self.context(absent_with_member)

        too_many = copy.deepcopy(self.document)
        too_many["bindings"][0]["members"].append(
            _member(
                self.base / "input/repo",
                "catalog/data/concept_graph_v2.json",
                object_name="second",
            )
        )
        too_many["generation_id"] = generation_identity(too_many)
        with self.assertRaisesRegex(BuildContextError, "cardinality one"):
            self.context(too_many)

    def test_helpers_reject_wrong_cardinality_requirement_and_unknown_ids(self) -> None:
        context = self.context()
        with self.assertRaisesRegex(BuildContextError, "cardinality one"):
            context.require_one("mathlib-source-tree")
        with self.assertRaisesRegex(BuildContextError, "not optional"):
            context.optional_one("concept-graph")
        with self.assertRaisesRegex(BuildContextError, "unknown input binding"):
            context.members("not-declared")
        with self.assertRaisesRegex(BuildContextError, "unknown stage"):
            context.stage("not-a-stage")
        with self.assertRaisesRegex(BuildContextError, "program is"):
            context.require_stage(
                "cells", program="brain/build_frontier.py", argv=[]
            )
        with self.assertRaisesRegex(BuildContextError, "argv is"):
            context.require_stage(
                "base-graph", program="brain/build_snapshot.py", argv=[]
            )
        with self.assertRaisesRegex(BuildContextError, "needs are"):
            context.require_stage(
                "cells",
                program="brain/build_cells.py",
                argv=[],
                needs=[],
            )
        with self.assertRaisesRegex(BuildContextError, "outputs are"):
            context.require_stage(
                "brain-page",
                program="site/build_brain_page.py",
                argv=[],
                outputs=[("file", "site/out/not-brain.html")],
            )
        with self.assertRaisesRegex(BuildContextError, "does not directly depend"):
            context.dependency_output_for(
                "brain-page", "base-graph", "brain/data/nodes.jsonl"
            )

    def test_stage_schedule_rejects_forward_needs_and_output_overlap(self) -> None:
        forward = copy.deepcopy(self.document)
        forward["stages"][0]["needs"] = ["cells"]
        forward["generation_id"] = generation_identity(forward)
        with self.assertRaisesRegex(BuildContextError, "earlier stages"):
            self.context(forward)

        overlap = copy.deepcopy(self.document)
        overlap["stages"][-1]["outputs"] = [
            {"kind": "file", "path": "site/assets/brain/cells/inside.json"}
        ]
        overlap["generation_id"] = generation_identity(overlap)
        with self.assertRaisesRegex(BuildContextError, "overlaps tree"):
            self.context(overlap)

    def test_stage_outputs_must_be_nonempty_sorted_and_unique(self) -> None:
        for outputs in (
            [],
            [
                {"kind": "file", "path": "z.json"},
                {"kind": "file", "path": "a.json"},
            ],
            [
                {"kind": "file", "path": "same.json"},
                {"kind": "file", "path": "same.json"},
            ],
        ):
            document = copy.deepcopy(self.document)
            document["stages"][0]["outputs"] = outputs
            document["generation_id"] = generation_identity(document)
            with self.subTest(outputs=outputs):
                with self.assertRaises(BuildContextError):
                    self.context(document)

    def test_code_and_output_helpers_reject_escape_and_undeclared_paths(self) -> None:
        context = self.context()
        for path in ("../outside.py", "/absolute.py", "brain/./build_snapshot.py"):
            with self.subTest(code=path):
                with self.assertRaises(BuildContextError):
                    context.code(path)
        with self.assertRaisesRegex(BuildContextError, "not owned"):
            context.output("brain/data/undeclared.json")
        with self.assertRaisesRegex(BuildContextError, "does not own"):
            context.output_for("cells", "brain/data/nodes.jsonl")
        with self.assertRaises(BuildContextError):
            context.scratch_for("cells", "../outside.tmp")
        with self.assertRaises(BuildContextError):
            context.output("../outside.json")

    def test_curated_inputs_require_a_git_commit_and_tree_pin(self) -> None:
        for pin in (
            {"type": "dataset_revision", "value": "2026-09-02"},
            {"type": "git_commit", "value": COMMIT_A},
        ):
            document = copy.deepcopy(self.document)
            document["bindings"][0]["members"][0]["pin"] = pin
            document["generation_id"] = generation_identity(document)
            with self.subTest(pin=pin):
                with self.assertRaisesRegex(BuildContextError, "git_commit pin with tree"):
                    self.context(document)

    def test_configuration_is_strict_and_closes_every_runtime_knob(self) -> None:
        invalid_configurations = []
        wrong_schema = copy.deepcopy(self.document["configuration"])
        wrong_schema["schema"] = "wikilean.brain-reducer-config/v2"
        invalid_configurations.append(wrong_schema)
        unknown_key = copy.deepcopy(self.document["configuration"])
        unknown_key["extra"] = True
        invalid_configurations.append(unknown_key)
        negative_cap = copy.deepcopy(self.document["configuration"])
        negative_cap["external_node_cap"] = -1
        invalid_configurations.append(negative_cap)
        unsorted_kinds = copy.deepcopy(self.document["configuration"])
        unsorted_kinds["cell_attach_kinds"] = ["special_case", "generalization"]
        invalid_configurations.append(unsorted_kinds)
        duplicate_kinds = copy.deepcopy(self.document["configuration"])
        duplicate_kinds["cell_attach_kinds"] = ["generalization", "generalization"]
        invalid_configurations.append(duplicate_kinds)
        unknown_kind = copy.deepcopy(self.document["configuration"])
        unknown_kind["cell_attach_kinds"] = ["exact"]
        invalid_configurations.append(unknown_kind)
        bad_layout_shape = copy.deepcopy(self.document["configuration"])
        bad_layout_shape["layout"]["seed"] = 1
        invalid_configurations.append(bad_layout_shape)
        bad_layout_enabled = copy.deepcopy(self.document["configuration"])
        bad_layout_enabled["layout"]["enabled"] = 1
        invalid_configurations.append(bad_layout_enabled)
        negative_iterations = copy.deepcopy(self.document["configuration"])
        negative_iterations["layout"]["iterations"] = -1
        invalid_configurations.append(negative_iterations)

        for configuration in invalid_configurations:
            document = copy.deepcopy(self.document)
            document["configuration"] = configuration
            document["replay"]["reducer"]["configuration_sha256"] = hashlib.sha256(
                canonical_json_bytes(configuration)
            ).hexdigest()
            document["generation_id"] = generation_identity(document)
            with self.subTest(configuration=configuration):
                with self.assertRaises(BuildContextError):
                    self.context(document)

    def test_hostile_environment_does_not_affect_context_or_identity(self) -> None:
        hostile = {
            "BRAIN_DECL_ORACLE": "/hostile/oracle",
            "BRAIN_EXTERNAL_DIR": "/hostile/external",
            "BRAIN_EXT_NODE_CAP": "1",
            "BRAIN_MATHLIB_CHECKOUT": "/hostile/mathlib",
            "WIKILEAN_BRAIN_BACKEND": "hostile",
        }
        baseline = self.context()
        with mock.patch.dict(os.environ, hostile, clear=False):
            under_attack = self.context()
        self.assertEqual(baseline, under_attack)
        self.assertEqual(baseline.generation_id, under_attack.generation_id)
        self.assertNotIn("/hostile/", str(under_attack.to_document()))

    def test_validation_and_helpers_do_not_create_any_roots(self) -> None:
        roots = [Path(value) for value in self.document["roots"].values()]
        self.assertTrue(all(not root.exists() for root in roots))
        context = self.context()
        context.require_one("concept-graph")
        context.code("brain/build_snapshot.py")
        context.output("site/out/brain.html")
        self.assertTrue(all(not root.exists() for root in roots))

    def test_load_rejects_duplicate_keys_and_floating_point_configuration(self) -> None:
        duplicate = self.base / "duplicate.json"
        duplicate.write_text('{"schema":"a","schema":"b"}', encoding="utf-8")
        with self.assertRaisesRegex(BuildContextError, "duplicate object key"):
            BuildContext.load(duplicate)

        floating = self.base / "floating.json"
        floating.write_text('{"value":1.5}', encoding="utf-8")
        with self.assertRaisesRegex(BuildContextError, "floating-point JSON"):
            BuildContext.load(floating)

        noncanonical = self.base / "noncanonical.json"
        noncanonical.write_text(json.dumps(self.document, indent=2), encoding="utf-8")
        with self.assertRaisesRegex(BuildContextError, "not canonical-json-v1"):
            BuildContext.load(noncanonical)

        canonical = self.base / "canonical.json"
        canonical.write_bytes(canonical_json_bytes(self.document))
        self.assertEqual(BuildContext.load(canonical), self.context())


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    )
    raise SystemExit(0 if result.wasSuccessful() else 1)
