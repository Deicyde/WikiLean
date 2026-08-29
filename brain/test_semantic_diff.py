#!/usr/bin/env python3
"""Hermetic tests for the Brain semantic snapshot diff command."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE / "tools"
SCRIPT = TOOLS / "semantic_diff.py"
sys.path.insert(0, str(TOOLS))

import authority_contracts as contracts  # noqa: E402

PROV_A = {"source": "source-a", "method": "fixture", "pin": "one"}
PROV_B = {"source": "source-b", "method": "fixture", "pin": "two"}
META = {
    "generated_at": "2030-01-01T00:00:00Z",
    "snapshot_id": "old",
    "prov": [PROV_A, PROV_B],
}
ZERO_DIGEST = "0" * 64
ZERO_HASH = "sha256:" + ZERO_DIGEST
GIT_COMMIT = "a" * 40


def write_jsonl(path: Path, rows: list[dict], meta: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"_meta": meta or META}, ensure_ascii=False, separators=(",", ":"))]
    lines.extend(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def base_fixture() -> dict[str, list[dict]]:
    return {
        "nodes": [
            {
                "id": "decl:Mathlib:Alpha",
                "type": "decl",
                "label": "Alpha",
                "code": "theorem Alpha : True",
                "docstring": "Alpha docs",
            },
            {
                "id": "Q1",
                "type": "concept",
                "label": "One",
                "unit": {"description": "Concept one"},
            },
            {
                "id": "xref:nlab:alpha",
                "type": "ext",
                "db": "nlab",
                "label": "Alpha page",
                "snippet": "External alpha",
                "snippet_license": "CC-BY-SA-4.0",
            },
        ],
        "edges": [
            {
                "src": "Q1",
                "dst": "decl:Mathlib:Alpha",
                "kind": "formalizes",
                "provenance": PROV_A,
                "confidence": "high",
                "evidence": {"match_kind": "exact"},
            }
        ],
        "edges_links": [],
        "cells": [
            {
                "id": "cell:Q1",
                "anchor": "Q1",
                "label": "One",
                "organs": [
                    {"kind": "concept", "id": "Q1", "bond": "exact", "prov": 0},
                    {
                        "kind": "decl",
                        "id": "decl:Mathlib:Alpha",
                        "bond": "exact",
                        "prov": 0,
                    },
                ],
                "supercells": ["path:Mathlib/Algebra"],
                "f": 1,
                "xy": [1.0, 2.0],
            }
        ],
        "frontier": [
            {
                "id": "frontier:Algebra",
                "label": "Algebra frontier",
                "cells": ["cell:Q2", "cell:Q3"],
                "n": 2,
                "prox": {
                    "db": [1, 2],
                    "dw": [3, 4],
                    "ib": [0, 1],
                    "iw": [0, 2],
                    "s": [3.0, 4.5],
                    "r": [0.5, 0.25],
                },
                "suitability": {
                    "candidate": [True, False],
                    "reason": [None, "broad_scope"],
                },
                "near": "path:Mathlib/Algebra",
            }
        ],
    }


def write_fixture(root: Path, fixture: dict[str, list[dict]], *, meta: dict | None = None) -> None:
    base_meta = meta or META
    cell_meta = dict(base_meta)
    cell_meta.setdefault("base_generated_at", base_meta.get("generated_at"))
    cell_meta.setdefault("base_snapshot_id", base_meta.get("snapshot_id"))
    for key in ("nodes", "edges"):
        write_jsonl(root / f"{key}.jsonl", fixture[key], base_meta)
    for key in ("cells", "frontier"):
        write_jsonl(root / f"{key}.jsonl", fixture[key], cell_meta)
    if "edges_links" in fixture:
        write_jsonl(root / "edges_links.jsonl", fixture["edges_links"], base_meta)


def run_diff(before: Path, after: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--from", str(before), "--to", str(after)],
        capture_output=True,
        text=True,
        check=False,
    )


class SemanticDiffTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.before = self.root / "before"
        self.after = self.root / "after"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def compare(self, before_fixture: dict, after_fixture: dict) -> tuple[dict, subprocess.CompletedProcess[str]]:
        write_fixture(self.before, before_fixture)
        write_fixture(self.after, after_fixture)
        process = run_diff(self.before, self.after)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, "")
        self.assertTrue(process.stdout.endswith("\n"))
        return json.loads(process.stdout), process

    def test_equal_is_deterministic_and_ignores_generated_metadata_and_order(self) -> None:
        fixture = base_fixture()
        write_fixture(
            self.before,
            fixture,
            meta={"generated_at": "one", "snapshot_id": "one", "prov": [PROV_A, PROV_B]},
        )
        reordered = copy.deepcopy(fixture)
        for rows in reordered.values():
            rows.reverse()
        write_fixture(
            self.after,
            reordered,
            meta={
                "generated_at": "two",
                "snapshot_id": "two",
                # Parent pins may differ between snapshots but must remain
                # internally consistent within each snapshot generation.
                "base_generated_at": "two",
                "base_snapshot_id": "two",
                "prov": [PROV_A, PROV_B],
            },
        )
        first = run_diff(self.before, self.after)
        second = run_diff(self.before, self.after)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        report = json.loads(first.stdout)
        self.assertFalse(report["different"])
        self.assertTrue(all(not any(counts.values()) for counts in report["summary"].values()))

    def test_generated_named_fields_inside_rows_remain_semantic(self) -> None:
        before = base_fixture()
        after = copy.deepcopy(before)
        before["edges"][0]["evidence"]["generated_at"] = "observed-one"
        after["edges"][0]["evidence"]["generated_at"] = "observed-two"
        before["nodes"][1]["snapshot_id"] = "source-object-one"
        after["nodes"][1]["snapshot_id"] = "source-object-two"
        report, _ = self.compare(before, after)
        self.assertEqual(report["summary"]["nodes"]["changed"], 1)
        self.assertEqual(report["summary"]["edges"]["changed"], 1)

    def test_node_and_all_snippet_classes(self) -> None:
        before = base_fixture()
        after = copy.deepcopy(before)
        after["nodes"][0].pop("code")
        after["nodes"][0]["docstring"] = "Changed docs"
        after["nodes"][0]["slogan"] = "New slogan"
        after["nodes"][1]["unit"]["description"] = "Changed concept"
        after["nodes"][2]["snippet_license"] = "CC-BY-SA-3.0"
        after["nodes"].append({"id": "Q2", "type": "concept", "description": "Fallback"})

        report, _ = self.compare(before, after)
        self.assertTrue(report["different"])
        self.assertEqual(report["summary"]["nodes"], {"added": 1, "removed": 0, "changed": 3})
        decl_change = next(
            row for row in report["nodes"]["changed"]
            if row["id"] == "decl:Mathlib:Alpha"
        )
        self.assertIn("code", decl_change["changed_fields"])
        snippets = report["snippets"]
        self.assertEqual(
            {(row["id"], row["field"]) for row in snippets["removed"]},
            {("decl:Mathlib:Alpha", "declaration.code")},
        )
        self.assertIn(("decl:Mathlib:Alpha", "declaration.slogan"), {
            (row["id"], row["field"]) for row in snippets["added"]
        })
        self.assertIn(("Q2", "concept.description"), {
            (row["id"], row["field"]) for row in snippets["added"]
        })
        self.assertEqual(len(snippets["changed"]), 3)
        code_group = next(
            row for row in snippets["grouped_by_field_source"]
            if row["field"] == "declaration.code"
        )
        self.assertEqual(code_group, {
            "field": "declaration.code",
            "source": "Mathlib",
            "added": 0,
            "removed": 1,
            "changed": 0,
        })

    def test_snippet_source_transition_attributes_both_sources(self) -> None:
        before = base_fixture()
        after = copy.deepcopy(before)
        after["nodes"][2]["db"] = "proofwiki"
        report, _ = self.compare(before, after)
        changed = report["snippets"]["changed"]
        self.assertTrue(changed)
        self.assertTrue(all(row["before_source"] == "nlab" for row in changed))
        self.assertTrue(all(row["after_source"] == "proofwiki" for row in changed))
        grouped = report["snippets"]["grouped_by_field_source"]
        self.assertTrue(any(row["source"] == "nlab" and row["removed"] for row in grouped))
        self.assertTrue(any(row["source"] == "proofwiki" and row["added"] for row in grouped))

    def test_edge_summary_counts_duplicate_rows(self) -> None:
        before = base_fixture()
        after = copy.deepcopy(before)
        duplicate = copy.deepcopy(after["edges"][0])
        duplicate["src"] = "Q2"
        duplicate["evidence"]["sim"] = 0.12345678901234567890123456789
        after["edges"].extend([duplicate, copy.deepcopy(duplicate)])
        report, _ = self.compare(before, after)
        self.assertEqual(report["summary"]["edges"]["added"], 2)
        group = next(
            row for row in report["edges"]["grouped_by_source_kind"]
            if row["source"] == "source-a" and row["kind"] == "formalizes"
        )
        self.assertEqual(group["added"], 2)

        multiplicity_before = base_fixture()
        multiplicity_before["edges"].append(
            copy.deepcopy(multiplicity_before["edges"][0])
        )
        multiplicity_after = copy.deepcopy(multiplicity_before)
        multiplicity_after["edges"].pop()
        multiplicity_report, _ = self.compare(
            multiplicity_before, multiplicity_after
        )
        self.assertEqual(multiplicity_report["summary"]["edges"], {
            "added": 0,
            "removed": 1,
            "changed": 0,
            "provenance_only": 0,
        })

    def test_mixed_edge_variants_prefer_same_source_semantic_pairing(self) -> None:
        before = base_fixture()
        before["edges"].append(copy.deepcopy(before["edges"][0]))
        before["edges"][1]["confidence"] = "medium"
        before["edges"][1]["provenance"] = PROV_B
        after = copy.deepcopy(before)
        after["edges"][0]["confidence"] = "low"
        after["edges"][1]["confidence"] = "high"
        report, _ = self.compare(before, after)
        self.assertEqual(report["edges"]["provenance_only"], [])
        self.assertEqual(len(report["edges"]["changed"]), 1)
        self.assertEqual(report["summary"]["edges"], {
            "added": 0,
            "removed": 0,
            "changed": 2,
            "provenance_only": 0,
        })
        self.assertEqual(report["edges"]["grouped_by_source_kind"], [
            {"source": "source-a", "kind": "formalizes", "added": 0, "removed": 0, "changed": 1},
            {"source": "source-b", "kind": "formalizes", "added": 0, "removed": 0, "changed": 1},
        ])

    def test_edges_preserve_duplicates_and_separate_provenance_only(self) -> None:
        before = base_fixture()
        before_edge = before["edges"][0]
        before["edges"].append(copy.deepcopy(before_edge))
        after = copy.deepcopy(before)
        after["edges"][0]["provenance"] = PROV_B
        after["edges"][1]["provenance"] = PROV_B
        added = {
            "src": "Q1",
            "dst": "xref:nlab:alpha",
            "kind": "xref",
            "provenance": PROV_A,
            "confidence": "high",
            "evidence": {"property": "P1"},
        }
        removed = {
            "src": "Q9",
            "dst": "Q1",
            "kind": "relates",
            "provenance": PROV_B,
            "confidence": "medium",
            "evidence": {},
        }
        before["edges_links"].append(removed)
        after["edges_links"].append(added)

        report, _ = self.compare(before, after)
        edges = report["edges"]
        self.assertEqual(len(edges["provenance_only"]), 1)
        provenance_change = edges["provenance_only"][0]
        self.assertEqual(provenance_change["before"][0]["count"], 2)
        self.assertEqual(provenance_change["after"][0]["count"], 2)
        self.assertEqual(len(edges["changed"]), 0)
        self.assertEqual(len(edges["added"]), 1)
        self.assertEqual(len(edges["removed"]), 1)
        self.assertEqual(edges["grouped_by_source_kind"], [
            {"source": "source-a", "kind": "formalizes", "added": 0, "removed": 2, "changed": 0},
            {"source": "source-a", "kind": "xref", "added": 1, "removed": 0, "changed": 0},
            {"source": "source-b", "kind": "formalizes", "added": 2, "removed": 0, "changed": 0},
            {"source": "source-b", "kind": "relates", "added": 0, "removed": 1, "changed": 0},
        ])

    def test_edge_semantic_and_duplicate_count_changes_are_not_provenance_only(self) -> None:
        before = base_fixture()
        before["edges"].append(copy.deepcopy(before["edges"][0]))
        after = copy.deepcopy(before)
        after["edges"].pop()
        after["edges"][0]["confidence"] = "medium"
        after["edges"][0]["provenance"] = PROV_B

        report, _ = self.compare(before, after)
        self.assertEqual(len(report["edges"]["changed"]), 1)
        self.assertEqual(report["edges"]["provenance_only"], [])
        self.assertEqual(report["edges"]["changed"][0]["before"][0]["count"], 1)
        self.assertEqual(report["edges"]["removed"][0]["variants"][0]["count"], 1)
        self.assertEqual(report["summary"]["edges"], {
            "added": 0,
            "removed": 1,
            "changed": 1,
            "provenance_only": 0,
        })
        self.assertEqual(report["edges"]["grouped_by_source_kind"], [
            {"source": "source-a", "kind": "formalizes", "added": 0, "removed": 2, "changed": 0},
            {"source": "source-b", "kind": "formalizes", "added": 1, "removed": 0, "changed": 0},
        ])

        same_source_before = base_fixture()
        same_source_after = copy.deepcopy(same_source_before)
        same_source_after["edges"][0]["confidence"] = "medium"
        same_source_report, _ = self.compare(same_source_before, same_source_after)
        self.assertEqual(same_source_report["edges"]["grouped_by_source_kind"], [
            {"source": "source-a", "kind": "formalizes", "added": 0, "removed": 0, "changed": 1},
        ])

    def test_cells_organs_split_merge_and_layout(self) -> None:
        before = base_fixture()
        before["cells"] = [
            {
                "id": "cell:A",
                "anchor": "A",
                "label": "A",
                "organs": [
                    {"id": "o1", "kind": "concept", "bond": "exact", "prov": 0},
                    {"id": "o2", "kind": "decl", "bond": "exact", "prov": 1},
                ],
                "supercells": [],
                "xy": [0, 0],
            },
            {
                "id": "cell:B",
                "anchor": "B",
                "label": "B",
                "organs": [{"id": "o3", "kind": "concept", "bond": "exact"}],
                "supercells": [],
                "xy": [1, 1],
            },
        ]
        after = copy.deepcopy(before)
        after["cells"] = [
            {
                "id": "cell:C",
                "anchor": "C",
                "label": "C",
                "organs": [
                    {"id": "o1", "kind": "concept", "bond": "exact", "prov": 0},
                    {"id": "o3", "kind": "concept", "bond": "related"},
                ],
                "supercells": [],
                "xy": [99, 99],
            },
            {
                "id": "cell:D",
                "anchor": "D",
                "label": "D",
                "organs": [
                    {"id": "o2", "kind": "decl", "bond": "exact"},
                    {"id": "o4", "kind": "page", "bond": "xref"},
                ],
                "supercells": [],
                "xy": [3, 3],
            },
        ]

        report, _ = self.compare(before, after)
        membership = report["organ_membership"]
        self.assertEqual({row["id"] for row in membership["moved"]}, {"o1", "o2", "o3"})
        self.assertEqual({row["id"] for row in membership["changed"]}, {"o3"})
        self.assertEqual({row["id"] for row in membership["added"]}, {"o4"})
        self.assertEqual(len(membership["splits"]), 1)
        self.assertEqual(len(membership["merges"]), 1)

        same_before = base_fixture()
        same_after = copy.deepcopy(same_before)
        same_after["cells"][0]["xy"] = [999, -999]
        layout_report, _ = self.compare(same_before, same_after)
        self.assertFalse(layout_report["different"])

    def test_organ_provenance_index_changes_are_reported(self) -> None:
        before = base_fixture()
        after = copy.deepcopy(before)
        after["cells"][0]["organs"][0]["prov"] = 1
        report, _ = self.compare(before, after)
        provenance = report["organ_membership"]["provenance_only"]
        self.assertEqual(len(provenance), 1)
        self.assertEqual(provenance[0]["id"], "Q1")
        self.assertEqual(
            provenance[0]["before"]["provenance"]["source"], "source-a"
        )
        self.assertEqual(
            provenance[0]["after"]["provenance"]["source"], "source-b"
        )
        self.assertTrue(report["different"])

    def test_frontier_parallel_arrays_compare_by_member_identity(self) -> None:
        before = base_fixture()
        after = copy.deepcopy(before)
        row = after["frontier"][0]
        row["cells"].reverse()
        for values in row["prox"].values():
            values.reverse()
        for values in row["suitability"].values():
            values.reverse()
        report, _ = self.compare(before, after)
        self.assertFalse(report["different"])

        after["frontier"][0]["prox"]["dw"][0] = 99
        report, _ = self.compare(before, after)
        self.assertEqual(report["summary"]["frontier"]["changed"], 1)
        changed = report["frontier"]["changed"][0]
        self.assertEqual(changed["id"], "frontier:Algebra")

    def test_optional_edges_links_absent_means_empty(self) -> None:
        fixture = base_fixture()
        fixture.pop("edges_links")
        report, _ = self.compare(fixture, fixture)
        self.assertFalse(report["different"])

    def test_manifest_addressed_release_and_digest_validation(self) -> None:
        fixture = base_fixture()
        write_fixture(self.before, fixture)
        release_root = self.root / "release"
        data = release_root / "brain" / "data"
        write_fixture(data, fixture)
        manifest_path = self._write_release_manifest(release_root)

        process = run_diff(self.before, manifest_path)
        self.assertEqual(process.returncode, 0, process.stderr)
        report = json.loads(process.stdout)
        self.assertFalse(report["different"])
        self.assertEqual(report["to"]["kind"], "release-manifest")
        self.assertEqual(report["to"]["release_id"], json.loads(manifest_path.read_text())["release_id"])

        edges = data / "edges.jsonl"
        edges.write_text(edges.read_text() + "\n")
        failed = run_diff(self.before, manifest_path)
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(failed.stdout, "")
        self.assertIn("bytes", failed.stderr)

    def _write_release_manifest(self, root: Path) -> Path:
        required_paths = set(contracts.REQUIRED_RELEASE_PATHS)
        required_paths.add("site/assets/brain/cells/fixture.json")
        artifacts = []
        for index, relative in enumerate(sorted(required_paths)):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(b"fixture")
            digest, size = contracts.digest_file(path)
            artifacts.append({
                "logical_name": f"artifact-{index:02d}",
                "path": relative,
                "media_type": "application/x-ndjson" if relative.endswith(".jsonl") else "application/octet-stream",
                "sha256": digest,
                "bytes": size,
                "logical_format": "opaque",
                "logical_root": None,
            })
        release = {
            "schema": contracts.RELEASE_SCHEMA,
            "profile": contracts.RELEASE_PROFILE,
            "release_id": ZERO_HASH,
            "authority": {"git_commit": GIT_COMMIT, "semantic_state_root": ZERO_HASH},
            "source_set_root": "sha256:" + "1" * 64,
            "semantic_epoch": "brain-v3",
            "reducer": {
                "schedule": "current",
                "version": "1",
                "git_commit": GIT_COMMIT,
                "configuration_sha256": "2" * 64,
                "environment_sha256": "3" * 64,
            },
            "artifacts": artifacts,
            "attestations": [
                {"kind": "build", "path": "attestations/build.json", "sha256": ZERO_DIGEST, "bytes": 0},
                {"kind": "validation", "path": "attestations/validation.json", "sha256": ZERO_DIGEST, "bytes": 0},
            ],
            "compatible_overlay_generation_ids": [],
            "created_at": "2030-01-01T00:00:00Z",
        }
        release["release_id"] = contracts.release_identity(release)
        manifest = root / "release.json"
        manifest.write_bytes(contracts.canonical_json_bytes(release))
        return manifest

    def test_incomplete_data_directory_names_missing_artifacts(self) -> None:
        fixture = base_fixture()
        write_fixture(self.before, fixture)
        self.after.mkdir()
        write_jsonl(self.after / "nodes.jsonl", fixture["nodes"])
        process = run_diff(self.before, self.after)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(process.stdout, "")
        self.assertIn("incomplete Brain data directory", process.stderr)
        self.assertIn("edges.jsonl", process.stderr)
        self.assertIn("cells.jsonl", process.stderr)
        self.assertIn("frontier.jsonl", process.stderr)

    def test_mixed_generations_and_invalid_utf8_fail_cleanly(self) -> None:
        fixture = base_fixture()
        write_fixture(self.before, fixture)
        write_fixture(self.after, fixture)
        write_jsonl(
            self.after / "edges.jsonl",
            fixture["edges"],
            {"generated_at": "other", "snapshot_id": "old"},
        )
        process = run_diff(self.before, self.after)
        self.assertEqual(process.returncode, 2)
        self.assertIn("mixed or missing generated_at", process.stderr)

        write_fixture(self.after, fixture)
        (self.after / "nodes.jsonl").write_bytes(b'{"_meta":{}}\n{"id":"Q1","label":"\xff"}\n')
        process = run_diff(self.before, self.after)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(process.stdout, "")
        self.assertIn("not valid UTF-8", process.stderr)
        self.assertNotIn("Traceback", process.stderr)

    def test_frontier_partition_rejects_cross_area_duplicates(self) -> None:
        fixture = base_fixture()
        duplicate = copy.deepcopy(fixture)
        duplicate["frontier"].append({
            "id": "frontier:Other",
            "label": "Other frontier",
            "cells": ["cell:Q2"],
            "n": 1,
            "prox": {"db": [0]},
        })
        write_fixture(self.before, fixture)
        write_fixture(self.after, duplicate)
        process = run_diff(self.before, self.after)
        self.assertEqual(process.returncode, 2)
        self.assertIn("belongs to both", process.stderr)

    def test_malformed_inputs_fail_without_partial_json(self) -> None:
        fixture = base_fixture()
        write_fixture(self.before, fixture)
        write_fixture(self.after, fixture)
        (self.after / "nodes.jsonl").write_text('{"_meta":{}}\n{"id":')
        process = run_diff(self.before, self.after)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(process.stdout, "")
        self.assertIn("nodes.jsonl:2: invalid JSON", process.stderr)

        write_fixture(self.after, fixture)
        fixture_with_bad_frontier = base_fixture()
        fixture_with_bad_frontier["frontier"][0]["prox"]["db"].pop()
        write_fixture(self.after, fixture_with_bad_frontier)
        process = run_diff(self.before, self.after)
        self.assertEqual(process.returncode, 2)
        self.assertIn("parallel to cells", process.stderr)

    def test_duplicate_id_and_organ_owner_fail_clearly(self) -> None:
        fixture = base_fixture()
        write_fixture(self.before, fixture)
        duplicate = copy.deepcopy(fixture)
        duplicate["nodes"].append(copy.deepcopy(duplicate["nodes"][0]))
        write_fixture(self.after, duplicate)
        process = run_diff(self.before, self.after)
        self.assertEqual(process.returncode, 2)
        self.assertIn("duplicate node identity", process.stderr)

        duplicate = copy.deepcopy(fixture)
        duplicate["cells"].append({
            "id": "cell:other",
            "anchor": "other",
            "label": "Other",
            "organs": [{"id": "Q1", "kind": "concept", "bond": "exact"}],
            "supercells": [],
        })
        write_fixture(self.after, duplicate)
        process = run_diff(self.before, self.after)
        self.assertEqual(process.returncode, 2)
        self.assertIn("has two owners", process.stderr)

    def test_release_root_ambiguity_and_path_escape_fail(self) -> None:
        fixture = base_fixture()
        write_fixture(self.before, fixture)
        incomplete = self.root / "incomplete"
        incomplete.mkdir()
        (incomplete / "manifest.json").write_text("{}")
        (incomplete / "release.json").write_text("{}")
        process = run_diff(self.before, incomplete)
        self.assertEqual(process.returncode, 2)
        self.assertIn("ambiguous local release manifests", process.stderr)

        release_root = self.root / "escape-release"
        data = release_root / "brain" / "data"
        write_fixture(data, fixture)
        manifest = self._write_release_manifest(release_root)
        document = json.loads(manifest.read_text())
        target = next(
            artifact for artifact in document["artifacts"]
            if artifact["path"] == "brain/data/nodes.jsonl"
        )
        target["path"] = "../nodes.jsonl"
        target["sha256"] = hashlib.sha256(b"missing").hexdigest()
        document["release_id"] = contracts.release_identity(document)
        manifest.write_bytes(contracts.canonical_json_bytes(document))
        process = run_diff(self.before, manifest)
        self.assertEqual(process.returncode, 2)
        self.assertRegex(process.stderr, "normalized, relative|missing required artifact")


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    )
    raise SystemExit(0 if result.wasSuccessful() else 1)
