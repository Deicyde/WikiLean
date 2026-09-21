"""Hermetic streaming acquisition, exact byte lineage, and retained-profile tests."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import huggingface_source_evidence as evidence
import huggingface_sources as producer

WHEN = "2026-09-08T20:00:00Z"
CURRENT_PROFILE = evidence.current_profile


class HuggingFaceEvidenceTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.pins = json.loads(evidence.PINS.read_bytes())
        self.bodies = {}
        for dataset, row in self.pins["datasets"].items():
            siblings = []
            for filename, descriptor in row["files"].items():
                raw = ("declaration,docstring\nFixture.foo,Cafe\u0301 " + filename + "\n").encode()
                descriptor.update(sha256=evidence.sha(raw), size=len(raw))
                self.bodies[(dataset, evidence.object_name(filename))] = raw
                siblings.append({"rfilename": filename, "size": len(raw), "lfs": {"sha256": evidence.sha(raw), "size": len(raw)}})
            license_name = {"MathNetwork/MathlibGraph": "apache-2.0", "uw-math-ai/math-graph": "cc-by-4.0",
                            "uw-math-ai/theorem-matching": "cc-by-sa-4.0"}[dataset]
            readme = ("---\nlicense: " + license_name + "\n---\n# Fixture\n").encode()
            self.bodies[(dataset, "dataset_readme")] = readme
            siblings.append({"rfilename": "README.md", "size": len(readme),
                             "blobId": hashlib.sha1(b"blob " + str(len(readme)).encode() + b"\0" + readme).hexdigest()})
            self.bodies[(dataset, "revision_metadata")] = evidence.canonical({"id": dataset, "sha": row["revision"], "private": False,
                "siblings": siblings, "cardData": {"license": license_name}})
        self.pins_bytes = evidence.canonical(self.pins)
        self.pin_path = self.root / "reviewed-pins.json"
        self.pin_path.write_bytes(self.pins_bytes)
        self.profile = {"files": [{"path": p, "sha256": evidence.sha(self.pins_bytes) if p == "catalog/huggingface_pins.json" else "a" * 64}
                                  for p in evidence.TOOL_FILES]}
        self.profile["profile_id"] = evidence.profile_id(self.profile)
        self.registry = {"schema": evidence.PROFILE_SCHEMA, "current_profile": self.profile["profile_id"], "profiles": [self.profile]}
        for patcher in (mock.patch.object(evidence, "profiles", return_value=self.registry),
                        mock.patch.object(evidence, "current_profile", return_value=self.profile),
                        mock.patch.object(evidence, "PINS", self.pin_path)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tool = {"schema": "wikilean.huggingface-source-acquirer-tool/v1", "profile": self.profile,
                     "python": {"sha256": "b" * 64, "version": "CPython 3.12.13 -I -S"},
                     "curl": {"sha256": "c" * 64, "version": "curl fixture"}}

    def capture(self, *, raw_override=None):
        store = self.root / "captures"
        evidence.prepare_store(store)
        with evidence.stage_io.owned_directory(store, store / (".fixture-" + str(len(list(store.iterdir()))))) as owned:
            evidence.write(owned.path, "pins-preimage.json", self.pins_bytes)
            for spec in evidence.specs(self.pins):
                raw = self.bodies[(spec["dataset"], spec["object"])]
                if raw_override and (spec["dataset"], spec["object"]) in raw_override:
                    raw = raw_override[(spec["dataset"], spec["object"])]
                evidence.write(owned.path, evidence.raw_path(spec), raw)
            index = evidence.indexed(owned.path)
            for name, raw in evidence.controls(self.pins, self.tool, index, WHEN).items():
                evidence.write(owned.path, name, raw)
            return evidence.seal(owned, store, evidence.CAPTURE)

    def test_complete_three_dataset_capture_exports_six_identical_csv_objects(self):
        capture = self.capture()
        target = producer.export(capture, self.root / "exports")
        facts = producer.verify_export(target)
        self.assertEqual(facts["csv_files"], 6)
        self.assertFalse(facts["source_publishable"])
        fragment = json.loads((target / "source-fragment.json").read_bytes())
        self.assertEqual([b["input_id"] for b in fragment["input_bindings"]], ["slogan", "statement-formal", "theorem-matching"])
        self.assertEqual(len(fragment["sources"]), 3)
        for source in fragment["sources"]:
            self.assertEqual(source["source_kind"], "acquired_dataset")
            lineage = json.loads((target / source["evidence"]["normalization_lineage"]["path"]).read_bytes())
            self.assertEqual(lineage["mode"], "identity")
            self.assertEqual([{k: v for k, v in i.items() if k != "origin"} for i in lineage["inputs"]], lineage["outputs"])
            support = next(o for o in source["objects"] if o["name"] == "normalization_tool_profile")
            self.assertEqual(support["sha256"], lineage["tool"]["sha256"])
            for item in source["objects"]:
                if item["media_type"] == "text/csv":
                    self.assertEqual(item["roles"], ["normalized", "raw"])
                    self.assertIn(b"Cafe\xcc\x81", (target / item["path"]).read_bytes())

    def test_csv_byte_substitution_fails_even_with_reconstructed_receipts(self):
        key = ("MathNetwork/MathlibGraph", "nodes_csv")
        raw = self.bodies[key].replace(b"Fixture", b"Changed")
        capture = self.capture(raw_override={key: raw})
        with self.assertRaisesRegex(evidence.EvidenceError, "CSV differs"):
            evidence.verify_capture(capture)

    def test_revision_public_visibility_lfs_and_membership_are_verified(self):
        key = ("MathNetwork/MathlibGraph", "revision_metadata")
        for field in ("sha", "private", "lfs", "duplicate"):
            metadata = json.loads(self.bodies[key])
            if field == "sha": metadata["sha"] = "f" * 40
            if field == "private": metadata["private"] = True
            if field == "lfs": metadata["siblings"][0]["lfs"]["sha256"] = "f" * 64
            if field == "duplicate": metadata["siblings"].append(metadata["siblings"][0])
            with self.subTest(field=field):
                capture = self.capture(raw_override={key: evidence.canonical(metadata)})
                with self.assertRaises(evidence.EvidenceError):
                    evidence.verify_capture(capture)

    def test_readme_must_match_recorded_git_blob_not_only_license_text(self):
        key = ("MathNetwork/MathlibGraph", "dataset_readme")
        capture = self.capture(raw_override={key: self.bodies[key] + b"unreviewed claim"})
        with self.assertRaisesRegex(evidence.EvidenceError, "README"):
            evidence.verify_capture(capture)

    def test_missing_or_disagreeing_license_does_not_invent_public_permission(self):
        key = ("MathNetwork/MathlibGraph", "revision_metadata")
        metadata = json.loads(self.bodies[key])
        metadata["cardData"] = {"license": "unreviewed"}
        capture = self.capture(raw_override={key: evidence.canonical(metadata)})
        target = producer.export(capture, self.root / "exports")
        facts = producer.verify_export(target)
        self.assertEqual(facts["licenses"][key[0]], "LicenseRef-HuggingFace-Review")
        self.assertFalse(facts["source_publishable"])

    def test_reviewed_pin_preimage_cannot_be_replaced_by_caller_claims(self):
        capture = self.capture()
        altered = json.loads((capture / "pins-preimage.json").read_bytes())
        altered["datasets"]["MathNetwork/MathlibGraph"]["verified_at"] = "2026-09-09"
        (capture / "pins-preimage.json").write_bytes(evidence.canonical(altered))
        (capture / "manifest.json").write_bytes(evidence.canonical(evidence.manifest(evidence.indexed(capture), evidence.CAPTURE)))
        with self.assertRaisesRegex(evidence.EvidenceError, "pin preimage"):
            evidence.verify_capture(capture)

    def test_exact_twelve_get_request_preimages_exclude_credentials_and_redirect_urls(self):
        specs = evidence.specs(self.pins)
        self.assertEqual(len(specs), 12)
        for spec in specs:
            request = evidence.request(spec)
            self.assertNotIn("?", request["uri"])
            self.assertEqual(request["parameters_sha256"], evidence.sha(evidence.canonical(evidence.parameters(spec))))
            self.assertEqual(spec["query"], [["blobs", "true"]] if spec["object"] == "revision_metadata" else [])
        with mock.patch.dict(os.environ, {"HF_TOKEN": "private", "HTTPS_PROXY": "https://untrusted", "CURL_HOME": "/untrusted"}):
            self.assertEqual(producer.environment(), {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"})

    def test_unisolated_acquisition_fails_before_any_request(self):
        with mock.patch.object(producer, "transport") as transport:
            with self.assertRaisesRegex(evidence.EvidenceError, "isolated"):
                producer.acquire(self.root / "never", Path("/usr/bin/curl"))
        transport.assert_not_called()

    def test_failed_acquisition_is_retained_without_complete_receipts(self):
        store = self.root / "failed"
        with mock.patch.object(producer, "require_startup"), mock.patch.object(producer, "runtime_identity", return_value=self.tool), \
                mock.patch.object(producer, "transport", side_effect=evidence.EvidenceError("fixture network failure")):
            with self.assertRaisesRegex(evidence.EvidenceError, "fixture network failure"):
                producer.acquire(store, Path("/usr/bin/curl"))
        bundles = [p for p in store.iterdir() if not p.name.startswith(".")]
        self.assertEqual(len(bundles), 1)
        self.assertFalse((bundles[0] / "receipts").exists())
        with self.assertRaises(evidence.EvidenceError):
            evidence.verify_capture(bundles[0])

    def test_prior_exports_verify_after_current_helpers_change(self):
        capture = self.capture()
        first = producer.export(capture, self.root / "first")
        next_profile = copy.deepcopy(self.profile)
        next_profile["files"][0]["sha256"] = "f" * 64
        next_profile["profile_id"] = evidence.profile_id(next_profile)
        self.registry["profiles"].append(next_profile)
        self.registry["profiles"].sort(key=lambda p: p["profile_id"])
        self.registry["current_profile"] = next_profile["profile_id"]
        with mock.patch.object(evidence, "current_profile", return_value=next_profile):
            producer.verify_export(first)
            second = producer.export(capture, self.root / "second")
        self.assertNotEqual((first / "normalization/tool-profile.json").read_bytes(), (second / "normalization/tool-profile.json").read_bytes())
        self.assertEqual((first / "acquisition/tool.json").read_bytes(), (second / "acquisition/tool.json").read_bytes())
        with self.assertRaisesRegex(evidence.EvidenceError, "current implementation"):
            CURRENT_PROFILE()

    def test_unreviewed_normalization_tuple_is_rejected(self):
        changed = copy.deepcopy(self.profile)
        changed["files"][0]["sha256"] = "f" * 64
        changed["profile_id"] = evidence.profile_id(changed)
        with self.assertRaisesRegex(evidence.EvidenceError, "unreviewed"):
            evidence.approved_profile(changed)

    def test_private_bundle_permissions_symlinks_hardlinks_and_empty_dirs_fail_closed(self):
        for change in ("mode", "symlink", "hardlink", "empty"):
            with self.subTest(change=change):
                capture = self.capture()
                target = capture / "request-results.json"
                if change == "mode": target.chmod(0o666)
                if change == "symlink":
                    target.unlink(); target.symlink_to(self.pin_path)
                if change == "hardlink": os.link(target, self.root / "alias")
                if change == "empty": (capture / "surplus").mkdir(mode=0o700)
                with self.assertRaises(evidence.EvidenceError):
                    evidence.verify_capture(capture)
                # Keep the next fixture generation distinct after deliberate corruption.
                self.bodies[("MathNetwork/MathlibGraph", "revision_metadata")] += b" "

    def test_export_cannot_write_inside_its_capture(self):
        capture = self.capture()
        with self.assertRaisesRegex(evidence.EvidenceError, "disjoint"):
            producer.export(capture, capture / "export")
        evidence.verify_capture(capture)

    def test_stream_copy_rejects_changed_bytes_before_publication(self):
        source = self.root / "source"
        source.write_bytes(b"original")
        descriptor = evidence.file_ref(source)
        source.write_bytes(b"changed!")
        destination = self.root / "copy"
        evidence.prepare_store(destination)
        with self.assertRaisesRegex(evidence.EvidenceError, "copy source content differs"):
            evidence.copy_file(source, destination, "output", descriptor)

    def test_helper_origin_substitution_is_rejected(self):
        with mock.patch.object(evidence.stage_io, "__file__", str(self.root / "elsewhere.py")):
            with self.assertRaises((evidence.EvidenceError, FileNotFoundError)):
                evidence.origins()

    def test_stream_transport_enforces_bound_and_uses_only_public_https_policy(self):
        class Process:
            def __init__(self, raw):
                self.stdout = io.BytesIO(raw)
                self.killed = False
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def wait(self): return 0
            def kill(self): self.killed = True
        for oversized in (False, True):
            destination = self.root / ("oversized" if oversized else "bounded")
            evidence.prepare_store(destination)
            spec = {**evidence.specs(self.pins)[0], "limit": 4}
            process = Process(b"abcde" if oversized else b"abcd")
            with mock.patch.object(producer.subprocess, "Popen", return_value=process) as popen:
                if oversized:
                    with self.assertRaisesRegex(evidence.EvidenceError, "reviewed bound"):
                        producer.transport(spec, Path("/usr/bin/curl"), destination)
                    self.assertTrue(process.killed)
                else:
                    producer.transport(spec, Path("/usr/bin/curl"), destination)
                    self.assertEqual((destination / evidence.raw_path(spec)).read_bytes(), b"abcd")
            argv = popen.call_args.args[0]
            self.assertEqual(argv[:2], ["/usr/bin/curl", "--disable"])
            self.assertEqual(argv[-1], spec["uri"] + "?blobs=true")
            self.assertIn("--fail", argv)
            self.assertEqual(argv[argv.index("--proto-redir") + 1], "=https")
            self.assertEqual(popen.call_args.kwargs["stderr"], producer.subprocess.DEVNULL)
            self.assertEqual(popen.call_args.kwargs["env"], producer.environment())

    def test_rehashed_export_cannot_change_source_kind_or_drop_profile(self):
        capture = self.capture()
        for alteration in ("source-kind", "profile"):
            target = producer.export(capture, self.root / alteration)
            if alteration == "source-kind":
                path = target / "source-fragment.json"
                value = json.loads(path.read_bytes())
                value["sources"][0]["source_kind"] = "curated_git_tree"
            else:
                path = target / "normalization/tool-profile.json"
                value = json.loads(path.read_bytes())
                value["files"][0]["sha256"] = "f" * 64
                value["profile_id"] = evidence.profile_id(value)
            path.write_bytes(evidence.canonical(value))
            (target / "manifest.json").write_bytes(evidence.canonical(evidence.manifest(evidence.indexed(target), evidence.EXPORT)))
            with self.assertRaises(evidence.EvidenceError):
                producer.verify_export(target)

    def test_recorded_receipt_identity_excludes_only_audit_clock(self):
        capture = self.capture()
        _pins, _tool, index, _licenses = evidence.verify_capture(capture)
        dataset = "MathNetwork/MathlibGraph"
        first = evidence.receipt(self.pins, self.tool, index, dataset, WHEN)
        later = evidence.receipt(self.pins, self.tool, index, dataset, "2026-09-09T20:00:00Z")
        self.assertEqual(first["acquisition_receipt_id"], later["acquisition_receipt_id"])
        self.assertNotEqual(evidence.canonical(first), evidence.canonical(later))

    def test_producer_detects_changed_loaded_program_before_measurement_or_export(self):
        with mock.patch.object(producer, "LOADED_PROGRAM_SHA256", "f" * 64):
            with self.assertRaisesRegex(evidence.EvidenceError, "since import"):
                producer.require_loaded_program()

    def test_selection_cannot_expand_to_unreviewed_files_or_floating_revisions(self):
        for change in ("file", "revision"):
            pins = copy.deepcopy(self.pins)
            row = pins["datasets"]["MathNetwork/MathlibGraph"]
            if change == "file": row["files"]["unreviewed.csv"] = row["files"]["nodes.csv"]
            else: row["revision"] = "main"
            with self.assertRaises(evidence.EvidenceError):
                evidence.specs(pins)


if __name__ == "__main__":
    unittest.main()
