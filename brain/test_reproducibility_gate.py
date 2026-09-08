#!/usr/bin/env python3
"""Hermetic adversarial gate tests; fixture results never imply real OCI evidence."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE / "tools")]
import reproducibility_gate as gate
import store
import test_semantic_diff as semantic_fixtures
import test_authority_contracts as authority_fixtures
import test_oci_runtime as oci_fixtures

H = "sha256:" + "a" * 64
D = "a" * 64


def seal_approval(value=None):
    value = value or {"schema": gate.APPROVAL_SCHEMA, "approval_id": "", "scope": "fixture",
        "offline_pack_id": H, "source_set_root": H, "reducer_inventory_id": H, "environment_id": H,
        "authority_git_commit": "a" * 40, "authority_root": H, "semantic_epoch": "brain-v3-current", "prior_state_root": None,
        "baseline": {"release_id": H, "manifest_sha256": D},
        "provenance": {"mode": "exact", "expected_report_sha256": None}}
    value["approval_id"] = gate.identity("wikilean.reproducibility-approval.v1", value, "approval_id")
    return value


def complete_output(root: Path):
    root.mkdir(mode=0o700)
    fixture = authority_fixtures.ReleaseVerificationTest(methodName="runTest")
    fixture.root = root
    fixture.write_release_artifacts()
    old = b"f" * 64
    hashes = []
    for relative in gate.BASE_PATHS:
        header, rest = (root / relative).read_bytes().split(b"\n", 1)
        parsed = json.loads(header)
        del parsed["_meta"]["snapshot_id"]
        preimage = (json.dumps(parsed, ensure_ascii=False, separators=(",", ":")) + "\n").encode() + rest
        hashes.append(hashlib.sha256(preimage).hexdigest())
    snapshot = gate.sha("".join(hashes).encode())
    for path in root.rglob("*"):
        if path.is_file() and path.suffix in {".json", ".jsonl"}:
            path.write_bytes(path.read_bytes().replace(old, snapshot.encode()))
    database = root / gate.SQLITE_PATH
    database.unlink()
    store.write_sqlite_from_jsonl(database, root / "brain/data")
    for path in root.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o444 if path == database else 0o644)
    return snapshot


class OutputVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.output = self.root / "first"
        self.snapshot = complete_output(self.output)

    def measure(self):
        return gate.measure_output(self.output, semantic_epoch="brain-v3-current")

    def test_actual_sqlite_rows_static_closure_base_and_projection_are_verified(self):
        measured = self.measure()
        self.assertEqual(measured["base_snapshot_id"], self.snapshot)
        self.assertEqual(len(measured["projection_id"]), 64)
        self.assertTrue(measured["semantic_state_root"].startswith("sha256:"))
        other = self.root / "different-long-absolute-directory"
        shutil.copytree(self.output, other)
        self.assertEqual(measured, gate.measure_output(other, semantic_epoch="brain-v3-current"))

    def test_shared_but_fabricated_base_snapshot_header_is_not_accepted(self):
        for relative in gate.BASE_PATHS:
            path = self.output / relative
            path.write_bytes(path.read_bytes().replace(self.snapshot.encode(), b"e" * 64))
        with self.assertRaisesRegex(gate.GateError, "actual three-file preimage"):
            self.measure()

    def test_changed_sqlite_content_or_projection_cannot_hide_behind_matching_metadata(self):
        for statement in ("UPDATE nodes SET id='Q999'", "UPDATE snapshot SET projection_id='" + "e" * 64 + "'"):
            with self.subTest(statement=statement):
                database = self.output / gate.SQLITE_PATH
                original = database.read_bytes()
                database.chmod(0o644)
                with sqlite3.connect(database) as connection:
                    connection.execute(statement)
                database.chmod(0o444)
                with self.assertRaises(gate.contracts.VerificationError):
                    self.measure()
                database.chmod(0o644)
                database.write_bytes(original)
                database.chmod(0o444)

    def test_static_shard_tampering_fails_independent_projection_verification(self):
        path = self.output / "site/assets/brain/cells/ce.json"
        path.write_bytes(b"{}")
        with self.assertRaisesRegex(gate.contracts.VerificationError, "manifest declares"):
            self.measure()

    def test_complete_closure_captures_unrelated_output_bytes_and_empty_directories(self):
        before = self.measure()
        (self.output / "empty").mkdir(mode=0o700)
        after = self.measure()
        self.assertNotEqual(before["output_root"], after["output_root"])
        self.assertIn({"path": "empty", "kind": "directory", "mode": 0o700}, after["entries"])
        path = self.output / "extra.json"
        path.write_bytes(b"{}")
        self.assertNotEqual(after["output_root"], self.measure()["output_root"])

    def test_symlink_and_hardlink_outputs_are_rejected(self):
        source = self.output / "brain/data/nodes.jsonl"
        target = self.output / "injected.jsonl"
        for make_link in (lambda: target.symlink_to(source), lambda: os.link(source, target)):
            make_link()
            with self.assertRaises(gate.GateError):
                gate.capture_tree(self.output)
            target.unlink()

    def test_mutation_during_identity_verification_is_detected(self):
        original = gate.base_snapshot_identity
        def modify(root):
            result = original(root)
            (root / "site/out/brain.html").write_bytes(b"changed while checked")
            return result
        with mock.patch.object(gate, "base_snapshot_identity", side_effect=modify):
            with self.assertRaisesRegex(gate.GateError, "output changed"):
                self.measure()


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.before = self.root / "before"
        self.after = self.root / "after"
        self.fixture = semantic_fixtures.base_fixture()
        semantic_fixtures.write_fixture(self.before, self.fixture)
        semantic_fixtures.write_fixture(self.after, self.fixture)

    def report(self):
        return gate.comparison_report(self.before, self.after)

    def test_exact_baseline_compares_all_families_without_runtime_paths(self):
        report = self.report()
        self.assertNotIn("from", report)
        self.assertNotIn("to", report)
        result = gate.verify_compatibility(report, {"mode": "exact", "expected_report_sha256": None})
        self.assertEqual(result["provenance"], "equal")

    def test_lost_mathlib_external_or_concept_snippets_fail_even_under_provenance_policy(self):
        for row_index, key in ((0, "code"), (0, "docstring"), (1, "unit"), (2, "snippet")):
            with self.subTest(key=key):
                changed = copy.deepcopy(self.fixture)
                del changed["nodes"][row_index][key]
                semantic_fixtures.write_fixture(self.after, changed)
                report = self.report()
                policy = {"mode": "reviewed-provenance-only", "expected_report_sha256": gate.sha(gate.report_bytes(report))}
                with self.assertRaisesRegex(gate.GateError, "cannot waive"):
                    gate.verify_compatibility(report, policy)

    def test_fold_or_source_mismatch_changing_edges_is_not_provenance_only(self):
        changed = copy.deepcopy(self.fixture)
        changed["edges"][0]["dst"] = "decl:Mathlib:WrongFoldSource"
        semantic_fixtures.write_fixture(self.after, changed)
        report = self.report()
        with self.assertRaisesRegex(gate.GateError, "cannot waive"):
            gate.verify_compatibility(report, {"mode": "reviewed-provenance-only", "expected_report_sha256": gate.sha(gate.report_bytes(report))})

    def test_only_exact_reviewed_provenance_report_can_pass(self):
        changed = copy.deepcopy(self.fixture)
        changed["edges"][0]["provenance"]["pin"] = "content-sha256:new-pin"
        semantic_fixtures.write_fixture(self.after, changed)
        report = self.report()
        with self.assertRaises(gate.GateError):
            gate.verify_compatibility(report, {"mode": "exact", "expected_report_sha256": None})
        policy = {"mode": "reviewed-provenance-only", "expected_report_sha256": gate.sha(gate.report_bytes(report))}
        self.assertEqual(gate.verify_compatibility(report, policy)["provenance"], "reviewed-only")
        policy["expected_report_sha256"] = "0" * 64
        with self.assertRaisesRegex(gate.GateError, "exact reviewed report"):
            gate.verify_compatibility(report, policy)

    def test_missing_family_and_forged_summary_never_pass(self):
        (self.after / "synapses.jsonl").unlink()
        report = self.report()
        with self.assertRaisesRegex(gate.GateError, "complete artifact coverage"):
            gate.verify_compatibility(report, {"mode": "exact", "expected_report_sha256": None})
        semantic_fixtures.write_fixture(self.after, self.fixture)
        report = self.report()
        report["semantic"]["summary"]["nodes"]["removed"] = 1
        with self.assertRaisesRegex(gate.GateError, "detailed evidence"):
            gate.verify_compatibility(report, {"mode": "exact", "expected_report_sha256": None})

    def test_independent_content_roots_preserve_high_precision_when_a_summary_misses_changes(self):
        path = self.after / "edges.jsonl"
        before = self.before / "edges.jsonl"
        for destination, value in ((before, b"0.123456789012345678901"), (path, b"0.123456789012345678902")):
            raw = destination.read_bytes().replace(b'"confidence":"high"', b'"confidence":' + value)
            destination.write_bytes(raw)
        report = self.report()
        self.assertEqual(report["semantic"]["summary"]["edges"]["changed"], 1)
        # Even a summary parser that accidentally rounds both numbers to one
        # binary float cannot satisfy the independent exact-content roots.
        report["semantic"]["edges"]["changed"] = []
        report["semantic"]["summary"] = gate.semantic_diff.summarize_report(report["semantic"])
        report["semantic"]["different"] = False
        for policy in ({"mode": "exact", "expected_report_sha256": None},
                       {"mode": "reviewed-provenance-only", "expected_report_sha256": gate.sha(gate.report_bytes(report))}):
            with self.assertRaisesRegex(gate.GateError, "roots differ|content differs"):
                gate.verify_compatibility(report, policy)

    def test_diagnostic_cli_preserves_exact_source_decimals_without_claiming_authority(self):
        raw = (self.after / "edges.jsonl").read_bytes().replace(
            b'"confidence":"high"', b'"confidence":0.123456789012345678901')
        (self.after / "edges.jsonl").write_bytes(raw)
        with mock.patch("builtins.print") as output:
            status = gate.main(["diagnose", "--before", str(self.before), "--after", str(self.after)])
        self.assertEqual(status, 0)
        rendered = output.call_args.args[0]
        self.assertIn('"authority":"none"', rendered)
        self.assertIn("0.123456789012345678901", rendered)


class LaunchRecordTests(unittest.TestCase):
    """Inspect-only fixtures test validation, never issue executable evidence."""

    def setUp(self):
        fixture = oci_fixtures.OCILaunchBoundaryTests(methodName="runTest")
        fixture.setUp()
        self.image, self.policy = fixture.image, fixture.policy
        self.context = SimpleNamespace(generation_id=H,
            roots=SimpleNamespace(output=Path("/private/workspace/output")))
        self.pack = {"offline_pack_id": H}
        self.descriptor = {"environment_id": H}
        self.runtime = {"engine_id": "fixture-engine", "engine_version": "fixture-engine-version",
            "docker_sha256": D, "memory_bytes": 1024**3, "manifest": "/private/pack/pack.json",
            "pack_root": "/private/pack", "stage_timeout_seconds": "1200.0"}
        self.name = "wikilean-replay-" + "9" * 32
        _arguments, child, mounts = gate.launcher.create_arguments(self.image, Path("/private/workspace"),
            Path("/private/pack"), self.policy, uid=501, gid=20, name=self.name, memory_bytes=1024**3)
        before = copy.deepcopy(fixture.container)
        before["Name"] = "/" + self.name
        before["Config"].update(User="501:20", Cmd=child, Labels={gate.launcher.LAUNCH_LABEL: self.name})
        before["Mounts"] = [{**mount, "Type": "bind", "Propagation": "rprivate"} for mount in mounts]
        after = copy.deepcopy(before)
        after["State"].update(Status="exited", ExitCode=0)
        payload = {"schema": gate.launcher.CHANNEL_SCHEMA, "nonce": "9" * 32,
            "runtime": self.image.runtime(), "policy": self.policy,
            "runner_arguments": ["--manifest", self.runtime["manifest"], "--root", self.runtime["pack_root"],
                "--context", "/private/workspace/build-context.json", "--expected-generation-id", H,
                "--python", self.policy["python"], "--stage-timeout-seconds", "1200.0"]}
        self.payload = payload
        self.record = {"schema": gate.launcher.LAUNCH_SCHEMA, "profile": "trusted-local-engine", "ok": True,
            "runtime": self.image.runtime(), "config_digest": self.image.config_digest, "environment_id": H,
            "offline_pack_id": H, "generation_id": H, "policy_sha256": self.image.policy_sha256,
            **{key: self.runtime[key] for key in ("engine_id", "engine_version", "docker_sha256")},
            "container_id": before["Id"], "observations": {"created": before, "exited": after},
            "request_sha256": gate.sha(gate.canonical(payload)),
            "stdout_sha256": gate.sha(b"fixture stdout"), "stderr_sha256": gate.sha(b"")}
        self.rehash(self.record)

    @staticmethod
    def rehash(record):
        for key, stage in (("create_observation_sha256", "created"), ("exit_observation_sha256", "exited")):
            record[key] = gate.sha(gate.canonical(record["observations"][stage]))

    def verify(self, record=None):
        with mock.patch.object(gate.os, "getuid", return_value=501), mock.patch.object(gate.os, "getgid", return_value=20):
            gate.verify_launch_record(record or self.record, context=self.context, pack=self.pack,
                descriptor=self.descriptor, image=self.image, policy=self.policy, runtime=self.runtime)

    def test_complete_inspection_fixture_satisfies_exact_launch_record_shape(self):
        self.verify()

    def test_rehashed_host_network_writable_input_or_failed_exit_is_rejected(self):
        for mutate in (
            lambda value: value["observations"]["created"]["HostConfig"].update(NetworkMode="host"),
            lambda value: value["observations"]["created"]["Mounts"][0].update(RW=True),
            lambda value: value["observations"]["exited"]["State"].update(ExitCode=1),
            lambda value: value["observations"]["exited"].update(Name="/different-container"),
            lambda value: value["observations"]["created"]["Config"].update(Labels={}),
        ):
            changed = copy.deepcopy(self.record)
            mutate(changed)
            self.rehash(changed)
            with self.assertRaises((gate.GateError, gate.launcher.OCILaunchError)):
                self.verify(changed)

    def test_replayed_observations_must_bind_exact_request_context_and_runtime(self):
        for key in ("offline_pack_id", "generation_id", "environment_id", "config_digest", "engine_id", "docker_sha256", "request_sha256"):
            changed = copy.deepcopy(self.record)
            changed[key] = "other"
            with self.subTest(key=key), self.assertRaises(gate.GateError):
                self.verify(changed)
        self.context = SimpleNamespace(generation_id=H,
            roots=SimpleNamespace(output=Path("/private/different-workspace/output")))
        with self.assertRaisesRegex(gate.launcher.OCILaunchError, "command differs|mount closure"):
            self.verify()

    def test_v2_launch_binds_exact_loaded_apparmor_and_system_path_configuration(self):
        fixture = oci_fixtures.AppArmorPolicyTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.temporary.cleanup)
        self.policy.update(schema=gate.oci_runtime.POLICY_SCHEMA_V2, apparmor=fixture.policy)
        self.record.update(schema=gate.launcher.LAUNCH_SCHEMA_V2,
            apparmor={key: fixture.policy[key] for key in
                ("name", "binary_sha256", "kernel_abi", "text_sha256", "parser_sha256")} |
                {"mode": "enforce", "kernel_profile_sha256": D})
        for item in self.record["observations"].values():
            item["AppArmorProfile"] = fixture.policy["name"]
            item["HostConfig"]["SecurityOpt"] += ["apparmor=" + fixture.policy["name"]]
            item["HostConfig"].update(MaskedPaths=[], ReadonlyPaths=[])
        self.record["request_sha256"] = gate.sha(gate.canonical(self.payload))
        self.rehash(self.record)
        self.verify()
        for mutate in (lambda item: item["apparmor"].update(mode="complain"),
                       lambda item: item["apparmor"].update(binary_sha256="b" * 64),
                       lambda item: item["observations"]["created"].update(AppArmorProfile="unconfined"),
                       lambda item: item["observations"]["created"]["HostConfig"].update(MaskedPaths=["/proc/kcore"])):
            changed = copy.deepcopy(self.record)
            mutate(changed)
            self.rehash(changed)
            with self.assertRaises((gate.GateError, gate.launcher.OCILaunchError)):
                self.verify(changed)


class AttestationContractTests(unittest.TestCase):
    def fixture(self):
        build = {"generation_id": H, "container_id": D, "launch_sha256": D, "measurement_sha256": D,
                 "release_id": H, "output_root": H, "base_snapshot_id": D, "projection_id": D, "semantic_state_root": H}
        value = {"schema": gate.ATTESTATION_SCHEMA, "attestation_id": "", "result": "pass", "scope": "fixture",
                 "authority": "diagnostic-fixture", "session_id": H, "approval_id": H, "offline_pack_id": H,
                 "source_set_root": H, "environment_id": H, "baseline": {"release_id": H, "manifest_sha256": D},
                 "compatibility": {"mode": "exact", "report_sha256": D, "graph_topology_content": "equal", "provenance": "equal"},
                 "builds": [build, {**build, "container_id": "b" * 64, "launch_sha256": "b" * 64}]}
        return self.seal(value)

    def seal(self, value):
        value["attestation_id"] = gate.identity("wikilean.reproducibility-attestation.v1", value, "attestation_id")
        return value

    def test_schema_and_validator_preserve_fixture_and_failure_boundary(self):
        import jsonschema
        schema = json.loads((HERE / "authority/schemas/attestation/reproducibility-v1.json").read_text())
        validator = jsonschema.Draft202012Validator(schema)
        value = self.fixture()
        gate.validate_attestation(value)
        validator.validate(value)
        for scope, result, authority in (("full-corpus", "pass", "full-corpus"), ("full-corpus", "fail", "none")):
            value.update(scope=scope, result=result, authority=authority)
            self.seal(value)
            gate.validate_attestation(value)
            validator.validate(value)
        value.update(scope="fixture", result="pass", authority="full-corpus")
        self.seal(value)
        with self.assertRaises(gate.GateError):
            gate.validate_attestation(value)
        with self.assertRaises(jsonschema.ValidationError):
            validator.validate(value)

    def test_passing_documents_require_all_equal_build_identities_and_two_actual_containers(self):
        for field in ("generation_id", "release_id", "output_root", "semantic_state_root", "base_snapshot_id", "projection_id", "measurement_sha256"):
            value = self.fixture()
            value["builds"][1][field] = "sha256:" + "c" * 64 if value["builds"][1][field].startswith("sha256:") else "c" * 64
            self.seal(value)
            with self.assertRaisesRegex(gate.GateError, "identities differ"):
                gate.validate_attestation(value)
        value = self.fixture()
        value["builds"][1]["container_id"] = value["builds"][0]["container_id"]
        self.seal(value)
        with self.assertRaisesRegex(gate.GateError, "distinct observed containers"):
            gate.validate_attestation(value)


class BoundaryTests(unittest.TestCase):
    def test_no_cli_flags_accept_launch_receipts_or_success_summaries(self):
        parser = gate.parser()
        with mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                parser.parse_args(["run", "--launch-receipt", "fake.json"])

    def test_missing_native_runtime_fails_before_creating_artifacts(self):
        with mock.patch.object(gate.runner, "require_isolated_startup"), mock.patch.object(gate.sys, "platform", "darwin"):
            with self.assertRaisesRegex(gate.GateError, "prerequisite missing: native Linux"):
                gate.run_gate(argparse.Namespace())

    def test_fake_success_record_cannot_satisfy_observed_launch_contract(self):
        with self.assertRaisesRegex(gate.GateError, "unexpected fields"):
            gate.verify_launch_record({"ok": True, "generation_id": H}, context=None, pack={}, descriptor={}, image=None, policy={}, runtime={})

    def test_approval_is_exact_and_cannot_be_boolean_baseline_authority(self):
        value = seal_approval()
        gate.validate_approval(value)
        value["baseline"] = {"approved": True}
        seal_approval(value)
        with self.assertRaisesRegex(gate.GateError, "unexpected fields"):
            gate.validate_approval(value)

    def test_timestamp_and_environment_challenges_are_distinct_and_reverified(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            measurements = []
            environments = []
            for index in (0, 1):
                private = root / str(index)
                workspace = private / "workspace"
                (workspace / "input").mkdir(parents=True)
                (workspace / "input/source.json").write_bytes(b"{}")
                (workspace / "output").mkdir()
                before = (workspace / "input/source.json").read_bytes()
                measured = gate.randomize_mtimes(workspace, 77 + index)
                self.assertEqual(measured, gate.randomize_mtimes(workspace, 77 + index, apply=False))
                self.assertEqual(before, (workspace / "input/source.json").read_bytes())
                measurements.append(measured)
                environments.append(gate.hostile_environment(private, index))
            self.assertNotEqual(measurements[0]["schedule_sha256"], measurements[1]["schedule_sha256"])
            self.assertNotEqual(environments[0], environments[1])
            os.utime(workspace / "input/source.json", ns=(1, 1))
            with self.assertRaisesRegex(gate.GateError, "recorded adversarial schedule"):
                gate.randomize_mtimes(workspace, 78, apply=False)

    def test_unreviewed_session_id_fails_before_any_release_use(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / "session.json"
            session = {"schema": gate.SESSION_SCHEMA, "session_id": "", "status": "pending-attestation"}
            session["session_id"] = gate.identity("wikilean.reproducibility-session.v1", session, "session_id")
            path.write_bytes(gate.canonical(session))
            with self.assertRaisesRegex(gate.GateError, "explicitly reviewed exact"):
                gate.finalize(argparse.Namespace(session=path, expected_session_id=H))

    def test_legacy_release_cannot_satisfy_real_pack_finalize(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fixture = authority_fixtures.ReleaseVerificationTest(methodName="runTest")
            fixture.root = root
            release, _ = fixture.make_release()
            manifest_path = root / "release.json"
            measured = {"semantic_state_root": release["authority"]["semantic_state_root"],
                        "entries": [{"kind": "file", **item} for item in release["artifacts"]]}
            pack = {"source_set_root": release["source_set_root"], "reducer": {"git_commit": release["reducer"]["git_commit"]},
                    "configuration": {"sha256": release["reducer"]["configuration_sha256"]},
                    "environment": {"sha256": release["reducer"]["environment_sha256"]}}
            approval = {"authority_git_commit": release["authority"]["git_commit"], "semantic_epoch": release["semantic_epoch"]}
            with self.assertRaisesRegex(gate.GateError, "legacy compatibility freezer"):
                gate.verified_pack_release(manifest_path, approval=approval, pack=pack, context=None, measured=measured)


if __name__ == "__main__":
    unittest.main()
