"""Hermetic official-artifact lineage and archive-boundary regression tests."""
from __future__ import annotations

import copy
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import acquire_mathlib_sources as acquisition
import export_mathlib_sources as exporter
import mathlib_source_evidence as evidence

WHEN = "2026-09-08T16:00:00Z"
CURRENT_PROFILE = evidence.current_profile


def tar_bytes(entries, *, source=False):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz" if source else "w") as archive:
        if source:
            item = tarfile.TarInfo("mathlib-fixture")
            item.type = tarfile.DIRTYPE
            archive.addfile(item)
        for name, mode, raw in entries:
            item = tarfile.TarInfo(("mathlib-fixture/" if source else "./") + name)
            if mode == "120000":
                item.type = tarfile.SYMTYPE
                item.linkname = raw.decode()
            else:
                item.mode = 0o755 if mode == "100755" else 0o644
                item.size = len(raw)
            archive.addfile(item, io.BytesIO(raw) if item.isfile() else None)
    return output.getvalue()


def zip_bytes(raw):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("artifact.tar", raw)
    return output.getvalue()


def fixture():
    plan = {"schema": evidence.PLAN_SCHEMA, "run_id": 101, "job_id": 202, "artifact_id": 303,
            "mathlib_commit": "a" * 40, "docgen_commit": "b" * 40}
    files = {"LICENSE": ("100644", b"Apache License\nVersion 2.0, January 2004\n"),
             "Mathlib/Topology/Basic.lean": ("100644", b"-- Cafe\xcc\x81\nexample : True := trivial\n"),
             "Mathlib/Empty.lean": ("100644", b""), "tool.sh": ("100755", b"true\n"),
             "external-link": ("120000", b"../../never-follow-this-link")}
    docs = [(evidence.ORACLE_MEMBER, "100644", b'{"declarations":{"Fixture.foo":{"docstring":"Cafe\\u0301"}}}'),
            (evidence.HTML_MEMBER, "100644", ('<a href="https://github.com/' + evidence.MATHLIB_REPO +
                 '/blob/' + plan["mathlib_commit"] + '/Mathlib/Topology/Basic.lean">source</a>').encode())]
    archive = zip_bytes(tar_bytes(docs))
    raw = {"pages_zip": archive, "source_tar": tar_bytes([(p, m, b) for p, (m, b) in files.items()], source=True),
        "run": evidence.canonical({"id": 101, "head_sha": "c" * 40, "repository": {"full_name": evidence.DOCS_REPO},
                                   "status": "completed", "conclusion": "success"}),
        "artifact": evidence.canonical({"id": 303, "name": "github-pages", "workflow_run": {"id": 101, "head_sha": "c" * 40},
                                        "digest": "sha256:" + evidence.sha(archive), "size_in_bytes": len(archive)}),
        "job": evidence.canonical({"id": 202, "run_id": 101, "head_sha": "c" * 40, "status": "completed", "conclusion": "success"}),
        "commit": evidence.canonical({"sha": plan["mathlib_commit"], "tree": {"sha": evidence.tree_hash(files)}}),
        "job_log": ("T From https://github.com/" + evidence.MATHLIB_REPO + "\nT [command]/usr/bin/git log -1 --format=%H\nT " +
                    plan["mathlib_commit"] + "\nT info: leanprover/doc-gen4: checking out revision '" + plan["docgen_commit"] + "'\n").encode()}
    return plan, raw, files


class MathlibEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.plan, self.raw, self.source_files = fixture()
        policy = mock.patch.object(evidence, "REVIEWED_APACHE_LICENSE_DIGESTS", frozenset({evidence.sha(self.source_files["LICENSE"][1])}))
        policy.start()
        self.addCleanup(policy.stop)
        registry = evidence.profiles()
        profile = next(p for p in registry["profiles"] if p["profile_id"] == registry["current_profile"])
        # The offline fixtures record an approved identity, like retained captures.
        # They do not reacquire or pretend to measure the evolving checkout.
        measured_profile = mock.patch.object(evidence, "current_profile", return_value=profile)
        measured_profile.start()
        self.addCleanup(measured_profile.stop)
        self.tool = {"schema": "wikilean.mathlib-source-acquirer-tool/v1", "profile_id": profile["profile_id"], "files": profile["files"],
            "python": {"sha256": "1" * 64, "version": "CPython 3.12.13 -I -S"},
            "gh": {"sha256": "2" * 64, "version": "gh version fixture"}}

    def capture(self):
        return evidence.publish(evidence.capture_files(self.plan, self.raw, self.tool, WHEN),
                                self.root / "capture", evidence.CAPTURE_SCHEMA)

    def test_exact_fresh_capture_and_export_close_both_upstream_acquired_sources(self):
        capture = self.capture()
        plan, raw, tool, files = evidence.verify_capture(capture)
        self.assertEqual(raw, self.raw)
        self.assertEqual(len(json.loads(files["request-results.json"])), 7)
        target = exporter.export(capture, self.root / "exports")
        facts = exporter.verify_export(target)
        self.assertEqual(facts["source_members"], 5)
        self.assertEqual(facts["oracle_members"], 1)
        fragment = json.loads((target / "source-fragment.json").read_bytes())
        self.assertEqual({s["source_kind"] for s in fragment["sources"]}, {"acquired_dataset"})
        self.assertFalse(fragment["source_publishable"])
        binding = next(b for b in fragment["input_bindings"] if b["input_id"] == "mathlib-source-tree")
        self.assertEqual([m["path"] for m in binding["members"]], ["Mathlib/Empty.lean", "Mathlib/Topology/Basic.lean"])
        docs = next(s for s in fragment["sources"] if s["source"] == "mathlib-docs")
        lineage = json.loads((target / docs["evidence"]["normalization_lineage"]["path"]).read_bytes())
        self.assertEqual(len(lineage["parent_source_manifest_ids"]), 1)
        profile = json.loads((target / "normalization/tool-profile.json").read_bytes())
        self.assertEqual(lineage["tool"]["version"], "2")
        self.assertEqual(lineage["tool"]["sha256"], evidence.sha(evidence.canonical(profile)))
        support = next(o for o in docs["objects"] if o["name"] == "normalization_tool_profile")
        self.assertEqual(support["roles"], ["receipt"])
        self.assertEqual(support["sha256"], lineage["tool"]["sha256"])
        self.assertNotIn(support["name"], [i["object"] for i in lineage["inputs"]])

    def test_metadata_links_must_match_the_exact_run_job_artifact_and_commit(self):
        for file, key, value in (("run", "id", 999), ("job", "run_id", 999),
                                 ("artifact", "digest", "sha256:" + "f" * 64), ("commit", "sha", "f" * 40)):
            with self.subTest(file=file):
                raw = dict(self.raw)
                data = json.loads(raw[file]); data[key] = value
                raw[file] = evidence.canonical(data)
                with self.assertRaises(evidence.EvidenceError):
                    evidence.normalize(self.plan, raw)

    def test_arbitrary_commit_occurrence_does_not_replace_checkout_command_evidence(self):
        raw = dict(self.raw)
        raw["job_log"] = raw["job_log"].replace(b"git log -1 --format=%H", b"echo unrelated")
        with self.assertRaisesRegex(evidence.EvidenceError, "checkout log"):
            evidence.normalize(self.plan, raw)

    def test_docs_repository_prefix_does_not_select_its_checkout_as_mathlib(self):
        raw = dict(self.raw)
        prefix = ("T From https://github.com/" + evidence.DOCS_REPO +
                  "\nT [command]/usr/bin/git log -1 --format=%H\nT " + "c" * 40 + "\n").encode()
        raw["job_log"] = prefix + raw["job_log"]
        self.assertEqual(evidence.normalize(self.plan, raw)[2]["mathlib_commit"], self.plan["mathlib_commit"])

    def test_export_cannot_write_into_its_acquisition_generation(self):
        path = self.capture()
        with self.assertRaisesRegex(evidence.EvidenceError, "disjoint"):
            exporter.export(path, path / "exports")
        evidence.verify_capture(path)

    def test_source_content_modes_and_membership_reconstruct_full_git_tree(self):
        expected = json.loads(self.raw["commit"])["tree"]["sha"]
        self.assertEqual(evidence.extract_source(self.raw["source_tar"], expected), self.source_files)
        for change in ("bytes", "mode", "omitted"):
            rows = dict(self.source_files)
            if change == "bytes": rows["LICENSE"] = ("100644", b"substituted")
            if change == "mode": rows["tool.sh"] = ("100644", b"true\n")
            if change == "omitted": rows.pop("external-link")
            with self.subTest(change=change), self.assertRaisesRegex(evidence.EvidenceError, "Git root tree"):
                evidence.extract_source(tar_bytes([(p, m, b) for p, (m, b) in rows.items()], source=True), expected)

    def test_archive_paths_duplicates_and_special_entries_fail_closed(self):
        expected = json.loads(self.raw["commit"])["tree"]["sha"]
        for name in ("../outside", "/absolute"):
            with self.subTest(name=name), self.assertRaises((evidence.EvidenceError, evidence.contracts.VerificationError)):
                evidence.extract_source(tar_bytes([(name, "100644", b"bad")], source=True), expected)
        rows = [("LICENSE", "100644", b"first"), ("LICENSE", "100644", b"second")]
        with self.assertRaisesRegex(evidence.EvidenceError, "duplicate"):
            evidence.extract_source(tar_bytes(rows, source=True), expected)

    def test_docs_selected_members_must_be_regular_unique_and_complete(self):
        for rows in ([(evidence.ORACLE_MEMBER, "120000", b"/etc/passwd")],
                     [(evidence.ORACLE_MEMBER, "100644", b"{}")],
                     [(evidence.HTML_MEMBER, "100644", b"a"), (evidence.HTML_MEMBER, "100644", b"b")]):
            with self.assertRaises(evidence.EvidenceError):
                evidence.extract_docs(zip_bytes(tar_bytes(rows)))

    def test_oracle_and_lean_non_nfc_text_remain_exact_artifact_bytes(self):
        docs, files, _ = evidence.normalize(self.plan, self.raw)
        self.assertIn(b"Cafe\\u0301", docs[evidence.ORACLE_MEMBER])
        self.assertEqual(files, self.source_files)

    def test_receipt_audit_times_do_not_change_logical_acquisition_identity(self):
        first = evidence.receipt(self.plan, self.raw, self.tool, "docs", WHEN)
        later = evidence.receipt(self.plan, self.raw, self.tool, "docs", "2026-09-09T16:00:00Z")
        self.assertEqual(first["acquisition_receipt_id"], later["acquisition_receipt_id"])
        self.assertNotEqual(evidence.canonical(first), evidence.canonical(later))

    def test_unreviewed_helper_tuple_and_missing_requests_are_rejected(self):
        tool = copy.deepcopy(self.tool)
        tool["files"][0]["sha256"] = "f" * 64
        with self.assertRaisesRegex(evidence.EvidenceError, "unreviewed"):
            evidence.validate_tool(tool)
        raw = dict(self.raw); raw.pop("job")
        with self.assertRaises(evidence.EvidenceError):
            evidence.capture_files(self.plan, raw, self.tool, WHEN)

    def test_bundle_substitution_and_surplus_paths_are_rejected(self):
        path = self.capture()
        (path / "raw/job").write_bytes(b"changed")
        with self.assertRaisesRegex(evidence.EvidenceError, "content differs"):
            evidence.verify_capture(path)

    def test_empty_undeclared_directory_is_not_part_of_a_sealed_generation(self):
        path = self.capture()
        (path / "surplus").mkdir(mode=0o700)
        with self.assertRaisesRegex(evidence.EvidenceError, "undeclared directories"):
            evidence.verify_capture(path)

    def test_license_expression_requires_the_reviewed_upstream_license_bytes(self):
        with mock.patch.object(evidence, "REVIEWED_APACHE_LICENSE_DIGESTS", frozenset()):
            with self.assertRaisesRegex(evidence.EvidenceError, "LICENSE bytes"):
                evidence.normalize(self.plan, self.raw)

    def test_prior_capture_and_export_verify_after_current_implementation_advances(self):
        capture = self.capture()
        target = exporter.export(capture, self.root / "exports")
        registry = copy.deepcopy(evidence.profiles())
        current = copy.deepcopy(next(p for p in registry["profiles"] if p["profile_id"] == registry["current_profile"]))
        current["files"][0]["sha256"] = "f" * 64
        current["profile_id"] = evidence.profile_id(current)
        registry["profiles"].append(current)
        registry["profiles"].sort(key=lambda p: p["profile_id"])
        registry["current_profile"] = current["profile_id"]
        with mock.patch.object(evidence, "profiles", return_value=registry):
            evidence.verify_capture(capture)
            self.assertEqual(exporter.verify_export(target)["source_members"], 5)
            with self.assertRaisesRegex(evidence.EvidenceError, "current producer/exporter bytes"):
                CURRENT_PROFILE()

    def test_normalization_identity_changes_when_only_a_helper_changes(self):
        registry = copy.deepcopy(evidence.profiles())
        old = copy.deepcopy(evidence.current_profile())
        current = copy.deepcopy(old)
        helper = next(item for item in current["files"] if item["path"] == "brain/tools/source_plan_contracts.py")
        helper["sha256"] = "f" * 64
        current["profile_id"] = evidence.profile_id(current)
        registry["profiles"].append(current)
        registry["profiles"].sort(key=lambda p: p["profile_id"])
        registry["current_profile"] = current["profile_id"]
        original_tool, _ = evidence.normalization_implementation()
        with mock.patch.object(evidence, "profiles", return_value=registry), \
                mock.patch.object(evidence, "current_profile", return_value=current):
            new_tool, preimage = evidence.normalization_implementation()
            self.assertNotEqual(new_tool["sha256"], original_tool["sha256"])
            self.assertEqual(preimage, current)
            capture = self.capture()
            target = exporter.export(capture, self.root / "new-export")
            self.assertEqual(exporter.verify_export(target)["source_members"], 5)
            self.assertEqual(json.loads((target / "acquisition/tool.json").read_bytes())["profile_id"], old["profile_id"])
            self.assertEqual(json.loads((target / "normalization/tool-profile.json").read_bytes()), current)

    def test_legacy_core_digest_exports_remain_replayable_without_profile_preimage(self):
        capture = self.capture()
        plan, raw, tool, files = evidence.verify_capture(capture)
        legacy = {"name": "wikilean-official-mathlib-normalizer", "version": "1", "sha256": next(
            iter(evidence.LEGACY_NORMALIZER_CORE_DIGESTS))}
        generated = evidence.build_export(plan, raw, tool, files, WHEN, normalizer=legacy)
        self.assertNotIn("normalization/tool-profile.json", generated)
        target = evidence.publish(generated, self.root / "legacy", evidence.EXPORT_SCHEMA)
        self.assertEqual(exporter.verify_export(target)["source_members"], 5)

    def test_v2_only_reviewed_core_cannot_be_downgraded_to_legacy_normalization(self):
        registry = copy.deepcopy(evidence.profiles())
        current = copy.deepcopy(evidence.current_profile())
        core = next(item for item in current["files"] if item["path"] == "brain/mathlib_source_evidence.py")
        core["sha256"] = "e" * 64
        current["profile_id"] = evidence.profile_id(current)
        registry["profiles"].append(current)
        registry["profiles"].sort(key=lambda p: p["profile_id"])
        registry["current_profile"] = current["profile_id"]
        downgraded = {"name": "wikilean-official-mathlib-normalizer", "version": "1", "sha256": core["sha256"]}
        with mock.patch.object(evidence, "profiles", return_value=registry):
            with self.assertRaisesRegex(evidence.EvidenceError, "unreviewed"):
                evidence.normalization_implementation(downgraded)

    def test_missing_mismatched_and_unreviewed_normalizer_preimages_fail_closed(self):
        normalizer, profile = evidence.normalization_implementation()
        changed = copy.deepcopy(profile)
        changed["files"][0]["sha256"] = "f" * 64
        changed["profile_id"] = evidence.profile_id(changed)
        for candidate_tool, candidate_profile in (
                (normalizer, None), (normalizer, changed),
                ({**normalizer, "sha256": evidence.sha(evidence.canonical(changed))}, changed)):
            with self.subTest(profile=candidate_profile), self.assertRaisesRegex(evidence.EvidenceError, "unreviewed"):
                evidence.normalization_implementation(candidate_tool, candidate_profile)

    def test_rehashed_bundle_cannot_remove_the_normalization_profile(self):
        capture = self.capture()
        target = exporter.export(capture, self.root / "exports")
        files, _ = evidence.read_bundle(target, evidence.EXPORT_SCHEMA)
        files.pop("normalization/tool-profile.json")
        changed = evidence.publish(evidence.manifest_files(files, evidence.EXPORT_SCHEMA), self.root / "changed", evidence.EXPORT_SCHEMA)
        with self.assertRaisesRegex(evidence.EvidenceError, "unreviewed"):
            exporter.verify_export(changed)

    def test_loaded_helper_origins_cannot_be_substituted(self):
        for module in (evidence.contracts, evidence.contracts.execution_environment_contract,
                       evidence.source_plan_contracts, evidence.stage_io):
            with self.subTest(module=module.__name__), mock.patch.object(module, "__file__", str(self.root / "shadow.py")):
                with self.assertRaises((evidence.EvidenceError, FileNotFoundError)):
                    evidence.validate_module_origins()
        with mock.patch.object(evidence.source_plan_contracts, "contracts", object()):
            with self.assertRaisesRegex(evidence.EvidenceError, "different authority"):
                evidence.validate_module_origins()

    def test_failed_publication_preserves_prior_immutable_capture(self):
        path = self.capture()
        original = (path / "manifest.json").read_bytes()
        with mock.patch.object(evidence.stage_io, "publish_directory_no_replace", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                self.capture()
        self.assertEqual((path / "manifest.json").read_bytes(), original)
        self.assertFalse(list((self.root / "capture").glob(".mathlib-*")))

    def test_unisolated_acquisition_stops_before_any_upstream_request(self):
        with mock.patch.object(acquisition, "transport") as transport:
            with self.assertRaisesRegex(evidence.EvidenceError, "isolated"):
                acquisition.acquire(self.root / "plan", self.root / "capture", Path("/usr/bin/false"))
        transport.assert_not_called()

    def test_request_preimages_are_exact_query_free_official_routes(self):
        for spec in evidence.request_specs(self.plan):
            descriptor = evidence.request_descriptor(spec)
            self.assertTrue(descriptor["uri"].startswith("https://api.github.com/repos/leanprover-community/"))
            self.assertNotIn("?", descriptor["uri"])
            self.assertEqual(descriptor["parameters_sha256"], evidence.sha(evidence.canonical(evidence.parameters(spec))))
        with mock.patch.dict(os.environ, {"GH_DEBUG": "api", "HTTPS_PROXY": "https://unsafe.invalid", "GITHUB_TOKEN": "fixture-secret"}):
            env = acquisition.environment()
        self.assertNotIn("GH_DEBUG", env)
        self.assertNotIn("HTTPS_PROXY", env)
        self.assertEqual(env["GITHUB_TOKEN"], "fixture-secret")


if __name__ == "__main__":
    unittest.main()
