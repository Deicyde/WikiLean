"""Exercise real old/new SQLite writers while preserving legacy semantic bytes."""
import copy
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "tools"))
import legacy_sqlite_projection as projection
import test_authority_contracts as fixtures

OLD_PROGRAM = HERE / "authority/fixtures/legacy-store-ebac34dc.py.txt"
OLD_SHA = "52a077570554f22eaaabd65683668f9014170a299f82a464241c9008a4bc9d84"


class LegacySQLiteProjectionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        fixture = fixtures.ReleaseVerificationTest(methodName="runTest")
        fixture.root = self.source
        fixture.write_release_artifacts()
        self.data = self.source / "brain/data"
        raw = OLD_PROGRAM.read_bytes()
        self.assertEqual(projection.sha(raw), OLD_SHA)
        old = types.ModuleType("old_store")
        old.__file__ = str(OLD_PROGRAM)
        exec(compile(raw, str(OLD_PROGRAM), "exec"), old.__dict__)
        (self.data / "brain.sqlite3").unlink()
        old.write_sqlite_from_jsonl(self.data / "brain.sqlite3", self.data)
        self.plan = {"schema": projection.SCHEMA, "plan_id": "",
            "inputs": {name: projection.measure(self.data / name) for name in projection.INPUT_FILES},
            "programs": {name: projection.measure(path) for name, path in projection.PROGRAMS.items()}}
        self.plan["plan_id"] = projection.plan_id(self.plan)
        self.destination = self.root / "projection"

    def tearDown(self):
        self.temp.cleanup()

    def run_projection(self, plan=None):
        chosen = plan or self.plan
        return projection.project(self.data, self.destination, chosen, chosen["plan_id"])

    def test_real_legacy_index_preserved_new_index_verifies_and_all_seven_files_unchanged(self):
        before = {name: (self.data / name).read_bytes() for name in projection.INPUT_FILES}
        report = self.run_projection()
        self.assertEqual(report["scope"], "projection-only")
        self.assertTrue(report["semantic_bytes_preserved"])
        for key in ("authority", "baseline_approved", "legacy_execution_verified", "static_release_verified"):
            self.assertIs(report[key], False)
        self.assertEqual(report["new_sqlite"]["schema_version"], 2)
        self.assertEqual(report["original_sqlite"]["schema_version"], 1)
        for name in projection.INPUT_FILES:
            self.assertEqual((self.data / name).read_bytes(), before[name])
            output = self.destination / ("retained/brain.sqlite3" if name == "brain.sqlite3" else "brain/data/" + name)
            self.assertEqual(output.read_bytes(), before[name])
        with closing(projection.store.open_store(data_dir=self.destination / "brain/data", backend="sqlite", require_derived=True)) as backend:
            self.assertEqual(list(backend.iter_nodes()), [{"id": "Q1", "label": "One", "type": "concept"}])
        self.assertEqual(json.loads((self.destination / "projection.json").read_bytes()), report)
        self.assertNotEqual(report["original_sqlite"]["sha256"], report["new_sqlite"]["sha256"])

    def test_plan_rejects_missing_semantic_input_wrong_id_bool_size_and_changed_program(self):
        for variant in ("missing", "id", "bool", "program"):
            with self.subTest(variant=variant):
                plan = copy.deepcopy(self.plan)
                if variant == "missing": del plan["inputs"]["frontier_graph.json"]
                if variant == "bool": plan["inputs"]["nodes.jsonl"]["bytes"] = True
                if variant == "program": plan["programs"]["store"]["sha256"] = "0" * 64
                plan["plan_id"] = projection.plan_id(plan)
                if variant == "id": plan["plan_id"] = "sha256:" + "0" * 64
                with self.assertRaises(ValueError): self.run_projection(plan)
                self.assertFalse(self.destination.exists())

    def test_changed_input_and_symlink_ancestor_fail_before_outputs(self):
        path = self.data / "nodes.jsonl"
        raw = path.read_bytes()
        path.write_bytes(raw.replace(b"One", b"Two"))
        with self.assertRaisesRegex(ValueError, "expected identity"): self.run_projection()
        self.assertFalse(self.destination.exists())
        path.write_bytes(raw)
        link = self.root / "linked"; link.symlink_to(self.data, target_is_directory=True)
        with self.assertRaises(OSError):
            projection.project(link, self.destination, self.plan, self.plan["plan_id"])
        self.assertFalse(self.destination.exists())

    def test_schema_two_original_is_rejected_without_overwriting_and_existing_destination_preserved(self):
        database = self.data / "brain.sqlite3"
        old = database.read_bytes()
        os.chmod(database, 0o600)
        with sqlite3.connect(database) as connection: connection.execute("PRAGMA user_version=2")
        plan = copy.deepcopy(self.plan)
        plan["inputs"]["brain.sqlite3"] = projection.measure(database)
        plan["plan_id"] = projection.plan_id(plan)
        with self.assertRaisesRegex(ValueError, "legacy schema 1"): self.run_projection(plan)
        self.assertFalse(self.destination.exists())
        database.write_bytes(old)
        self.destination.mkdir(); marker = self.destination / "keep"; marker.write_bytes(b"existing")
        with self.assertRaises(FileExistsError): self.run_projection()
        self.assertEqual(marker.read_bytes(), b"existing")
        self.assertEqual(list(self.destination.iterdir()), [marker])

    def test_independent_verifier_rejects_corrupt_sqlite_index_and_no_completion_record(self):
        original = projection.store.write_sqlite_from_jsonl
        def corrupt(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            os.chmod(path, 0o600)
            with sqlite3.connect(path) as connection:
                connection.execute("UPDATE nodes SET label='Wrong index column'")
            return result
        with mock.patch.object(projection.store, "write_sqlite_from_jsonl", side_effect=corrupt):
            with self.assertRaises(projection.contracts.VerificationError): self.run_projection()
        self.assertFalse((self.destination / "projection.json").exists())
        self.assertTrue((self.destination / "retained/brain.sqlite3").exists())

    def test_passthrough_semantic_mutation_fails_without_a_completion_record(self):
        original = projection.store.write_sqlite_from_jsonl
        def mutate(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            frontier = Path(path).parent / "frontier_graph.json"
            frontier.write_bytes(frontier.read_bytes() + b" ")
            return result
        with mock.patch.object(projection.store, "write_sqlite_from_jsonl", side_effect=mutate):
            with self.assertRaisesRegex(ValueError, "expected identity"): self.run_projection()
        self.assertFalse((self.destination / "projection.json").exists())

    def test_retained_valid_legacy_payload_mutation_fails_even_with_unchanged_snapshot_id(self):
        original = projection.store.write_sqlite_from_jsonl
        def mutate(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            retained = self.destination / "retained/brain.sqlite3"
            before = projection.legacy_index_identity(retained)
            with sqlite3.connect(retained) as connection:
                connection.execute("UPDATE nodes SET label='Two'")
            self.assertEqual(before, projection.legacy_index_identity(retained))
            return result
        with mock.patch.object(projection.store, "write_sqlite_from_jsonl", side_effect=mutate):
            with self.assertRaisesRegex(ValueError, "expected identity"): self.run_projection()
        self.assertFalse((self.destination / "projection.json").exists())

    def test_plan_cannot_adopt_program_bytes_edited_after_module_was_loaded(self):
        replacement = self.root / "store-copy.py"
        replacement.write_bytes(projection.PROGRAMS["store"].read_bytes() + b"\n# changed after import\n")
        plan = copy.deepcopy(self.plan)
        plan["programs"]["store"] = projection.measure(replacement)
        plan["plan_id"] = projection.plan_id(plan)
        with mock.patch.dict(projection.PROGRAMS, {"store": replacement}):
            with self.assertRaisesRegex(ValueError, "loaded implementation"): self.run_projection(plan)
        self.assertFalse(self.destination.exists())

    def test_actual_isolated_cli_requires_exact_canonical_plan(self):
        plan_path = self.root / "plan.json"
        plan_path.write_bytes(projection.canonical(self.plan))
        argv = [sys.executable, "-I", projection.__file__, "--plan", str(plan_path),
            "--expected-plan-id", self.plan["plan_id"], "--source-data", str(self.data),
            "--destination", str(self.destination)]
        run = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout, (self.destination / "projection.json").read_bytes())
        self.assertEqual(json.loads(run.stdout)["indexed_artifacts"], ["nodes", "edges", "edges_links", "cells", "synapses"])
        plan_path.write_bytes(json.dumps(self.plan, indent=2).encode())
        failed = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(failed.returncode, 1)
        self.assertIn(b"canonical JSON", failed.stderr)


if __name__ == "__main__":
    unittest.main()
