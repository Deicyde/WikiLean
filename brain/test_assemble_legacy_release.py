"""Private compatibility assembly: complete fixture parity and fail-closed inputs."""
import copy
import json
import os
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "tools"))
import assemble_legacy_release as assembly
import test_legacy_sqlite_projection as projector_fixture


class LegacyAssemblyTest(unittest.TestCase):
    def setUp(self):
        self.fixture = projector_fixture.LegacySQLiteProjectionTest(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root
        self.legacy = self.fixture.source
        self.projected = self.fixture.destination
        self.fixture.run_projection()
        self.projection_plan = self.root / "projection-plan.json"
        self.projection_plan.write_bytes(assembly.canonical(self.fixture.plan))
        self.evidence = self.root / "run-evidence"; self.evidence.mkdir()
        self.sealed = self.root / "sealed"; self.sealed.mkdir()
        self.destination = self.root / "assembled"
        for path in assembly.PROVENANCE:
            target = self.sealed / path; target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((self.legacy / path).read_bytes())
        halo = self.legacy / assembly.HALO; halo.parent.mkdir(parents=True); halo.write_bytes(b'{"items":[]}')
        original = self.legacy / "catalog/data/original.json"; original.write_bytes(b'{"original":"must remain present"}')
        old_programs = []
        for path in sorted(assembly.LEGACY_PROGRAM_PATHS):
            target = self.legacy / path; target.parent.mkdir(parents=True, exist_ok=True)
            # Synthetic reviewed record fixture, not a legacy execution claim.
            target.write_bytes(("# synthetic stage fixture: " + path + "\n").encode())
            old_programs.append(self.file(target, self.legacy))
        inputs = []
        for path in sorted(assembly.PROVENANCE | {"catalog/data/original.json"}):
            inputs.append({**self.file(self.legacy / path, self.legacy), "input_id": "fixture",
                "source_manifest_id": "sha256:" + "1" * 64, "object": "fixture",
                "logical_root": "fixture", "member_path": path})
        stages = []
        for index, program in enumerate(assembly.STAGES):
            logs = {}
            for kind in ("stdout", "stderr"):
                path = self.evidence / f"stage-{index}-{kind}.log"; path.write_bytes(b"")
                logs[kind] = self.file(path, self.evidence)
            stages.append({"program": program, "argv": ["python3", program], "exit": 0, **logs})
        configuration = self.evidence / "configuration.json"; configuration.write_bytes(b'{"fixture":true}')
        runtime = self.evidence / "runtime.json"; runtime.write_bytes(b'{"scope":"fixture-only"}')
        outputs = [self.file(self.legacy / path, self.legacy) for path in sorted(
            assembly.contracts.REQUIRED_RELEASE_PATHS - assembly.PROVENANCE | assembly.freezer._static_closure(self.legacy))]
        self.record = {"schema": "wikilean.legacy-baseline-execution/v1", "scope": "baseline-diagnostic",
            "authority": False, "baseline_approved": False,
            "pack": {name: "sha256:" + "2" * 64 for name in ("offline_pack_id", "source_set_root", "reducer_inventory_id")},
            "legacy": {"git_commit": assembly.LEGACY_COMMIT, "git_tree": "3" * 40, "program_files": old_programs},
            "inputs": inputs, "outputs": outputs, "absences": [{"input_id": "absent", "logical_root": "fixture", "path": "catalog/data/absent.json"}],
            "stages": stages, "halo": {"output": self.file(halo, self.legacy)},
            "configuration": {"preimage": self.file(configuration, self.evidence)},
            "runtime": {"preimage": self.file(runtime, self.evidence)}}
        self.record_path = self.evidence / "execution.json"
        self.record_path.write_bytes(assembly.canonical(self.record))
        self.plan = {"schema": assembly.SCHEMA, "plan_id": "", "execution": assembly.measure(self.record_path),
            "projection_plan": assembly.measure(self.projection_plan),
            "projection_report": assembly.measure(self.projected / "projection.json"),
            "provenance": {path: assembly.measure(self.sealed / path) for path in assembly.PROVENANCE},
            "programs": dict(assembly.LOADED_PROGRAMS), "semantic_epoch": "brain-v3-fixture",
            "assembly_git_commit": "4" * 40, "curated_authority_git_commit": "5" * 40}
        self.plan["plan_id"] = assembly.plan_id(self.plan)

    def file(self, path, root):
        return {"path": path.relative_to(root).as_posix(), **assembly.measure(path)}

    def refresh_record(self):
        self.record_path.write_bytes(assembly.canonical(self.record))
        self.plan["execution"] = assembly.measure(self.record_path)
        self.plan["plan_id"] = assembly.plan_id(self.plan)

    def run_assembly(self):
        return assembly.assemble(self.legacy, self.projected, self.sealed, self.evidence, self.destination,
            self.plan, self.plan["plan_id"], self.record_path, self.projection_plan)

    def test_complete_release_independently_verifies_and_preserves_all_seven_semantic_files(self):
        before = {path: (self.legacy / path).read_bytes() for path in assembly.SEMANTIC}
        result = self.run_assembly(); release = Path(result["release"]["root"])
        manifest, _ = assembly.contracts.load_canonical_json(release / "release.json")
        assembly.contracts.verify_release_files(assembly.contracts.validate_release_manifest(manifest), release)
        self.assertEqual(manifest["profile"], assembly.contracts.RELEASE_PROFILE)
        self.assertNotIn("replay", manifest)
        build = json.loads((release / "attestations/build.json").read_bytes())
        self.assertEqual(build["schema"], assembly.contracts.BUILD_ATTESTATION_SCHEMA_V1)
        self.assertEqual(build["builder"]["git_commit"], self.plan["assembly_git_commit"])
        self.assertEqual(manifest["reducer"]["git_commit"], self.plan["assembly_git_commit"])
        self.assertEqual(result["legacy_graph_git_commit"], assembly.LEGACY_COMMIT)
        for path, raw in before.items():
            self.assertEqual((release / path).read_bytes(), raw)
            self.assertEqual((self.legacy / path).read_bytes(), raw)
        for key in ("authority", "baseline_approved", "legacy_execution_verified", "offline_replay_verified"):
            self.assertIs(result[key], False)
        self.assertIs(result["static_release_verified"], True)
        self.assertFalse((release / "projection.json").exists())
        self.assertFalse((release / "retained").exists())
        self.assertEqual((self.destination / "evidence/legacy-output/brain/data/brain.sqlite3").read_bytes(),
                         (self.legacy / "brain/data/brain.sqlite3").read_bytes())
        inventory = json.loads((self.destination / "assembly" / (assembly.PREFIX + "input-inventory.json")).read_bytes())
        actual_paths = {row["path"] for row in inventory["inputs"] if "path" in row}
        self.assertIn(assembly.HALO, actual_paths)
        self.assertIn("catalog/data/original.json", actual_paths)
        self.assertEqual((self.destination / "assembly/catalog/data/original.json").read_bytes(), b'{"original":"must remain present"}')
        self.assertEqual(json.loads((self.destination / "assembly.json").read_bytes()), result)

    def test_missing_original_input_cannot_become_a_false_inventory_absence(self):
        (self.legacy / "catalog/data/original.json").unlink()
        with self.assertRaises(OSError): self.run_assembly()
        self.assertFalse(self.destination.exists())

    def test_changed_or_missing_static_and_sealed_provenance_fail(self):
        paths = [self.legacy / "site/assets/brain/cells/ce.json", self.legacy / "site/out/brain.html",
                 self.sealed / "catalog/data/source_registry.json", self.sealed / "brain/data/community_edges.jsonl"]
        for path in paths:
            raw = path.read_bytes()
            for attack in ("missing", "changed"):
                with self.subTest(path=path, attack=attack):
                    if attack == "missing": path.unlink()
                    else: path.write_bytes(raw + b" ")
                    with self.assertRaises((ValueError, OSError)): self.run_assembly()
                    self.assertFalse(self.destination.exists())
                    path.write_bytes(raw)

    def test_incomplete_stage_or_missing_halo_is_rejected(self):
        self.record["stages"][2]["exit"] = 1; self.refresh_record()
        with self.assertRaisesRegex(ValueError, "did not complete"): self.run_assembly()
        self.record["stages"][2]["exit"] = 0; self.refresh_record()
        (self.legacy / assembly.HALO).unlink()
        with self.assertRaises(OSError): self.run_assembly()
        self.assertFalse(self.destination.exists())

    def test_execution_record_cannot_omit_an_imported_legacy_helper(self):
        for path in sorted(assembly.LEGACY_PROGRAM_PATHS - set(assembly.STAGES)):
            with self.subTest(path=path):
                programs = self.record["legacy"]["program_files"]
                self.record["legacy"]["program_files"] = [row for row in programs if row["path"] != path]
                self.refresh_record()
                with self.assertRaisesRegex(ValueError, "exactly the ten ebac reducer programs"):
                    self.run_assembly()
                self.assertFalse(self.destination.exists())
                self.record["legacy"]["program_files"] = programs

    def test_present_recorded_absence_and_symlink_are_rejected(self):
        path = self.legacy / "catalog/data/absent.json"; path.write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "absent legacy input is present"): self.run_assembly()
        path.unlink(); path.symlink_to(self.legacy / "catalog/data/original.json")
        with self.assertRaisesRegex(ValueError, "absent legacy input is present"): self.run_assembly()
        self.assertFalse(self.destination.exists())

    def test_rehashed_incorrect_projected_semantics_and_fake_report_are_rejected(self):
        report_path = self.projected / "projection.json"
        report = json.loads(report_path.read_bytes()); report["artifacts"]["brain/data/nodes.jsonl"]["logical_root"] = "sha256:" + "0" * 64
        report_path.write_bytes(assembly.canonical(report)); self.plan["projection_report"] = assembly.measure(report_path)
        self.plan["plan_id"] = assembly.plan_id(self.plan)
        with self.assertRaisesRegex(ValueError, "semantic artifact report differs"): self.run_assembly()
        self.assertFalse((self.destination / "assembly.json").exists())

    def test_publication_recheck_rejects_changed_static_before_a_release_is_published(self):
        original = assembly.freezer._copy_source_file
        def change(root, relative, destination, **kwargs):
            result = original(root, relative, destination, **kwargs)
            if relative == "site/out/brain.html":
                path = self.legacy / relative; path.write_bytes(path.read_bytes() + b" ")
            return result
        with mock.patch.object(assembly.freezer, "_copy_source_file", side_effect=change):
            with self.assertRaises(ValueError): self.run_assembly()
        self.assertFalse((self.destination / "assembly.json").exists())
        self.assertEqual(list((self.destination / "releases").iterdir()), [])

    def test_rehashed_projected_sqlite_with_wrong_index_columns_fails_independent_parity(self):
        database = self.projected / "brain/data/brain.sqlite3"
        os.chmod(database, 0o600)
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE nodes SET label='Incorrect indexed label'")
        report_path = self.projected / "projection.json"
        report = json.loads(report_path.read_bytes())
        report["new_sqlite"].update(assembly.measure(database))
        report_path.write_bytes(assembly.canonical(report))
        self.plan["projection_report"] = assembly.measure(report_path)
        self.plan["plan_id"] = assembly.plan_id(self.plan)
        with self.assertRaises(assembly.contracts.VerificationError): self.run_assembly()
        self.assertFalse((self.destination / "assembly.json").exists())

    def test_retained_projector_evidence_mutation_prevents_publication(self):
        original = assembly.freezer._copy_source_file
        def change(root, relative, destination, **kwargs):
            result = original(root, relative, destination, **kwargs)
            if relative == "site/out/brain.html":
                path = self.destination / "evidence/projection.json"
                path.write_bytes(path.read_bytes() + b" ")
            return result
        with mock.patch.object(assembly.freezer, "_copy_source_file", side_effect=change):
            with self.assertRaises(ValueError): self.run_assembly()
        self.assertFalse((self.destination / "assembly.json").exists())
        self.assertEqual(list((self.destination / "releases").iterdir()), [])

    def test_old_commit_cannot_misidentify_new_freezer_and_changed_loaded_program_is_rejected(self):
        self.plan["assembly_git_commit"] = assembly.LEGACY_COMMIT
        self.plan["plan_id"] = assembly.plan_id(self.plan)
        with self.assertRaisesRegex(ValueError, "old graph commit"): self.run_assembly()
        self.plan["assembly_git_commit"] = "4" * 40
        self.plan["programs"]["freezer"] = {"sha256": "0" * 64, "bytes": 1}
        self.plan["plan_id"] = assembly.plan_id(self.plan)
        with self.assertRaisesRegex(ValueError, "loaded complete generation"): self.run_assembly()

    def test_actual_isolated_cli_and_canonical_plan_requirement(self):
        plan = self.root / "assembly-plan.json"; plan.write_bytes(assembly.canonical(self.plan))
        argv = [sys.executable, "-I", str(Path(assembly.__file__)), "--plan", str(plan),
            "--expected-plan-id", self.plan["plan_id"], "--legacy-root", str(self.legacy),
            "--projection-root", str(self.projected), "--sealed-root", str(self.sealed), "--evidence-root", str(self.evidence),
            "--destination", str(self.destination), "--execution-record", str(self.record_path), "--projection-plan", str(self.projection_plan)]
        process = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stdout, (self.destination / "assembly.json").read_bytes())
        plan.write_bytes(json.dumps(self.plan, indent=2).encode())
        failure = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(failure.returncode, 1)
        self.assertIn(b"not canonical", failure.stderr)


if __name__ == "__main__":
    unittest.main()
