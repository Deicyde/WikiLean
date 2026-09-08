"""Real pack/release fixtures and adversarial standalone policy boundaries."""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tools"))
import source_policy_reviews as core
import build_release
import test_authority_contracts as authority_fixture
import test_release_builder as release_fixture
from test_authority_contracts import file_ref, write_canonical


class SourcePolicyReviewsTest(unittest.TestCase):
    def setUp(self):
        self.release_fixture = release_fixture.ReleaseBuilderTest(methodName="runTest")
        self.release_fixture.setUp()
        self.addCleanup(self.release_fixture.tearDown)
        replay = self.release_fixture.offline_inputs()
        community_bytes = replay.sources["brain/data/community_edges.jsonl"][0].read_bytes()
        self.fixture = authority_fixture.V3EvidenceClosureTest(methodName="runTest")
        self.fixture.setUp(); self.addCleanup(self.fixture.tearDown)
        original = self.fixture.v2.make_source_manifest

        def restricted(**kwargs):
            if kwargs["source"] == "curated-fixture":
                kwargs["normalized"] = community_bytes
            manifest, ref = original(**kwargs)
            manifest["license"]["redistribution"] = "restricted"
            for obj in manifest["objects"]: obj["redistribution"] = "restricted"
            manifest["source_manifest_id"] = core.contracts.source_manifest_identity(manifest)
            write_canonical(self.fixture.root / ref["path"], manifest)
            return manifest, {**file_ref(self.fixture.root, ref["path"], "application/json"),
                              "source_manifest_id": manifest["source_manifest_id"]}

        with mock.patch.object(self.fixture.v2, "make_source_manifest", side_effect=restricted):
            self.pack, self.pack_path, *_ = self.fixture.make_v3_pack()
        self.pack_path = self.pack_path.resolve()
        replay = dataclasses.replace(replay, source_set_root=self.pack["source_set_root"],
            replay={**replay.replay, "offline_pack_id": self.pack["offline_pack_id"],
                    "reducer_inventory_id": self.pack["inventory"]["inventory_id"]})
        config = self.release_fixture.config(reducer_git_commit=self.pack["reducer"]["git_commit"],
            configuration_sha256=self.pack["configuration"]["sha256"], environment_sha256=self.pack["environment"]["sha256"])
        result = build_release.build_release(config, _verified_replay=replay)
        self.release_path = Path(result["manifest"]).resolve()
        self.attachments = Path(self.release_fixture.temp.name).resolve() / "policy-attachments"
        self.attachments.mkdir()
        (self.attachments / "reviewed-license.txt").write_bytes(b"Fixture policy evidence: not a real license approval.\n")
        self.evidence = {"kind": "review-attachment", "path": "reviewed-license.txt", "media_type": "text/plain",
            "sha256": hashlib.sha256((self.attachments / "reviewed-license.txt").read_bytes()).hexdigest(),
            "bytes": (self.attachments / "reviewed-license.txt").stat().st_size,
            "origin": "Synthetic independently reviewed fixture policy; no real source permission claimed."}
        self.evidence["evidence_id"] = core.evidence_id(self.evidence)

    @staticmethod
    def rehash(document):
        document["review_id"] = core.identity(document)
        return document

    def approve(self, document):
        document = copy.deepcopy(document)
        document["state"] = "approved"
        document["reviewer"] = {"name": "Synthetic test operator", "role": "trusted-local-operator", "reviewed_at": "2026-09-08T20:00:00Z"}
        document["evidence"] = [copy.deepcopy(self.evidence)]
        for record in document["sources"] + document.get("artifacts", []) + document.get("support_files", []):
            record.update(decision="approved", basis="Synthetic fixture-only decision over these exact bytes.",
                          evidence_ids=[self.evidence["evidence_id"]], unresolved=[])
        return self.rehash(document)

    def private(self):
        return self.approve(core.draft_private(self.pack_path))

    def public(self, private):
        document = self.approve(core.draft_public(self.pack_path, self.release_path, private,
            expected_private_id=private["review_id"], attachment_root=self.attachments))
        direct_ids = {item["source_manifest_id"] for record in document["artifacts"] for item in record["direct_source_objects"]}
        for source in document["sources"]:
            source["public_role"] = "content-parent" if source["source_manifest_id"] in direct_ids else "evidence-only"
        for record in document["artifacts"]:
            source_ids = sorted({item["source_manifest_id"] for item in record["direct_source_objects"]}) or [None]
            record["content_rules"] = sorted([{"source_manifest_id": source_id,
                "content_kind": "facts" if source_id else "generated-program-output", "fields": ["fixture exact artifact fields"],
                "license_expression": "LicenseRef-Synthetic-Policy-Fixture", "interpretation": "Fixture review of every byte; not a real permission grant."}
                for source_id in source_ids], key=core.canonical)
        for record in document["support_files"]:
            record["publicly_visible_fields"] = ["all fixture manifest and attestation metadata"]
        return self.rehash(document)

    def validate_private(self, document, **kwargs):
        return core.validate_private(document, self.pack_path, expected_id=kwargs.pop("expected_id", document["review_id"]),
            attachment_root=self.attachments, **kwargs)

    def validate_public(self, document, private, **kwargs):
        return core.validate_public(document, self.pack_path, self.release_path, private,
            expected_id=kwargs.pop("expected_id", document["review_id"]), expected_private_id=private["review_id"],
            attachment_root=self.attachments, private_attachment_root=self.attachments, **kwargs)

    def test_drafts_are_pending_and_do_not_change_pack_or_old_source_policies(self):
        before = {path.relative_to(self.pack_path.parent).as_posix(): path.read_bytes()
                  for path in self.pack_path.parent.rglob("*") if path.is_file()}
        draft = core.draft_private(self.pack_path)
        self.assertEqual(draft["state"], "pending"); self.assertIsNone(draft["reviewer"])
        self.assertFalse(self.validate_private(draft)["private_replay_policy_ready"])
        approved = self.approve(draft)
        result = self.validate_private(approved)
        self.assertTrue(result["private_replay_policy_ready"])
        self.assertFalse(result["scope"]["accepted_authority"])
        self.assertTrue(all(record["original_license"]["redistribution"] == "restricted" for record in approved["sources"]))
        self.assertEqual(before, {path.relative_to(self.pack_path.parent).as_posix(): path.read_bytes()
                                 for path in self.pack_path.parent.rglob("*") if path.is_file()})
        self.assertNotIn("source_authority_ready", result)
        self.assertNotIn("source_publishable", result)

    def test_approved_flag_alone_and_wrong_expected_id_do_not_approve(self):
        draft = core.draft_private(self.pack_path)
        draft["state"] = "approved"; self.rehash(draft)
        with self.assertRaisesRegex(core.PolicyReviewError, "reviewer"):
            self.validate_private(draft)
        approved = self.private()
        with self.assertRaisesRegex(core.PolicyReviewError, "independently expected"):
            self.validate_private(approved, expected_id="sha256:" + "0" * 64)
        with self.assertRaises(TypeError):
            core.validate_private(approved, self.pack_path)

    def test_every_source_object_role_license_and_pack_identity_are_bound(self):
        for mutate in (lambda d: d["sources"].pop(),
                       lambda d: d["sources"].append(copy.deepcopy(d["sources"][0])),
                       lambda d: d["sources"][0].update(object_role_root="sha256:" + "0" * 64),
                       lambda d: next(s for s in d["sources"] if s["source_object_count"] == 1).update(source_object_count=True),
                       lambda d: d["sources"][0]["original_license"].update(redistribution="allowed"),
                       lambda d: d["binding"].update(offline_pack_id="sha256:" + "0" * 64)):
            document = self.private(); mutate(document); self.rehash(document)
            with self.assertRaises(core.PolicyReviewError): self.validate_private(document)

    def test_approval_cannot_leave_pending_rejected_or_unresolved_sources(self):
        for changes in ({"decision": "pending"}, {"decision": "rejected"}, {"unresolved": ["rights unresolved"]}, {"evidence_ids": []}):
            document = self.private(); document["sources"][0].update(changes); self.rehash(document)
            with self.assertRaises(core.PolicyReviewError): self.validate_private(document)

    def test_retained_evidence_is_hashed_complete_and_referenced(self):
        document = self.private()
        (self.attachments / "reviewed-license.txt").write_bytes(b"replaced evidence")
        with self.assertRaisesRegex(core.PolicyReviewError, "attachment bytes"):
            self.validate_private(document)
        document["evidence"] = []; self.rehash(document)
        with self.assertRaisesRegex(core.PolicyReviewError, "unknown policy evidence"):
            self.validate_private(document)

    def test_pack_object_evidence_requires_exact_role_generation_and_byte_identity(self):
        document = self.private(); _pack, _sources, objects = core.verified_pack(self.pack_path)
        (source_id, name), obj = next(iter(objects.items()))
        evidence = {"kind": "pack-object", "source_manifest_id": source_id, "object": name,
            **{key: obj[key] for key in ("sha256", "bytes", "media_type")}}
        evidence["evidence_id"] = core.evidence_id(evidence)
        document["evidence"] = [evidence]
        for source in document["sources"]: source["evidence_ids"] = [evidence["evidence_id"]]
        self.rehash(document); self.assertTrue(self.validate_private(document)["private_replay_policy_ready"])
        evidence["bytes"] = True; evidence["evidence_id"] = core.evidence_id(evidence)
        for source in document["sources"]: source["evidence_ids"] = [evidence["evidence_id"]]
        self.rehash(document)
        with self.assertRaisesRegex(core.PolicyReviewError, "differs from pack bytes"):
            self.validate_private(document)

    def test_obligations_need_actual_fulfillment_evidence(self):
        document = self.private()
        document["sources"][0]["obligations"] = [{"id": "private-access", "description": "Fixture access control requirement",
            "evidence_ids": [], "artifact_paths": []}]
        self.rehash(document)
        with self.assertRaisesRegex(core.PolicyReviewError, "fulfillment"):
            self.validate_private(document)
        document["sources"][0]["obligations"][0]["evidence_ids"] = [self.evidence["evidence_id"]]
        self.rehash(document)
        self.assertTrue(self.validate_private(document)["private_replay_policy_ready"])

    def test_public_draft_requires_private_approval_and_complete_real_release_verification(self):
        draft = core.draft_private(self.pack_path)
        with self.assertRaisesRegex(core.PolicyReviewError, "approved private"):
            core.draft_public(self.pack_path, self.release_path, draft, expected_private_id=draft["review_id"])
        private = self.private()
        public = core.draft_public(self.pack_path, self.release_path, private,
            expected_private_id=private["review_id"], attachment_root=self.attachments)
        self.assertEqual(public["state"], "pending")
        self.assertFalse(self.validate_public(public, private)["public_release_policy_ready"])
        self.assertEqual(len(public["support_files"]), 3)

    def test_complete_public_review_covers_direct_copy_private_remainder_and_support(self):
        private = self.private(); public = self.public(private)
        result = self.validate_public(public, private)
        self.assertTrue(result["public_release_policy_ready"])
        self.assertFalse(result["scope"]["legal_determination"])
        direct = next(record for record in public["artifacts"] if record["artifact"]["path"] == "brain/data/community_edges.jsonl")
        self.assertTrue(direct["direct_source_objects"])
        self.assertTrue(any(s["public_role"] == "evidence-only" for s in public["sources"]))

    def test_public_review_rejects_omitted_artifact_support_source_and_field_policy(self):
        private = self.private()
        for mutate in (lambda d: d["artifacts"].pop(), lambda d: d["support_files"].pop(), lambda d: d["sources"].pop(),
                       lambda d: d["artifacts"][0].update(content_rules=[]),
                       lambda d: d["support_files"][0].update(publicly_visible_fields=[])):
            public = self.public(private); mutate(public); self.rehash(public)
            with self.assertRaises(core.PolicyReviewError): self.validate_public(public, private)

    def test_public_review_rejects_hidden_raw_copy_and_false_evidence_only_classification(self):
        private = self.private(); public = self.public(private)
        direct = next(record for record in public["artifacts"] if record["direct_source_objects"])
        source_id = direct["direct_source_objects"][0]["source_manifest_id"]
        direct["direct_source_objects"] = []; self.rehash(public)
        with self.assertRaisesRegex(core.PolicyReviewError, "direct source-object"):
            self.validate_public(public, private)
        public = self.public(private)
        next(s for s in public["sources"] if s["source_manifest_id"] == source_id)["public_role"] = "evidence-only"
        self.rehash(public)
        with self.assertRaisesRegex(core.PolicyReviewError, "evidence-only"):
            self.validate_public(public, private)

    def test_changed_release_pack_or_review_requires_a_new_pinned_decision(self):
        private = self.private(); public = self.public(private)
        original_id = public["review_id"]
        public["artifacts"][0]["basis"] = "different reviewed rationale"
        with self.assertRaisesRegex(core.PolicyReviewError, "independently expected"):
            self.validate_public(public, private, expected_id=original_id)
        self.rehash(public)
        public["binding"]["release_id"] = "sha256:" + "0" * 64; self.rehash(public)
        with self.assertRaisesRegex(core.PolicyReviewError, "identity/closure"):
            self.validate_public(public, private)

    def test_changed_actual_pack_and_release_payloads_fail_full_verification(self):
        private = self.private(); public = self.public(private)
        source = self.pack_path.parent / self.pack["objects"][0]["path"]
        original = source.read_bytes(); source.write_bytes(b"altered")
        with self.assertRaises(core.contracts.VerificationError): self.validate_private(private)
        source.write_bytes(original)
        artifact = self.release_path.parent / public["artifacts"][0]["artifact"]["path"]
        artifact.chmod(0o600); artifact.write_bytes(b"altered")
        with self.assertRaises(core.contracts.VerificationError): self.validate_public(public, private)

    def test_old_pack_schema_is_outside_new_policy_scope(self):
        _pack, path = self.fixture.v2.make_pack()
        with self.assertRaisesRegex(core.PolicyReviewError, "v3 pack"):
            core.draft_private(path.resolve())

    def test_legacy_release_profile_verifies_and_is_outside_new_policy_scope(self):
        fixture = release_fixture.ReleaseBuilderTest(methodName="runTest")
        fixture.setUp(); self.addCleanup(fixture.tearDown)
        result = build_release.build_release(fixture.config())
        path = Path(result["manifest"]).resolve()
        manifest = core.load_document(path)
        core.contracts.verify_release_files(manifest, path.parent)
        self.assertEqual(manifest["profile"], core.contracts.RELEASE_PROFILE)
        with self.assertRaisesRegex(core.PolicyReviewError, "only the offline replay"):
            core.verified_release(path, self.pack)

    def test_reader_rejects_symlinks_fifo_duplicates_noncanonical_and_oversize(self):
        base = self.attachments
        target = base / "control.json"; target.write_bytes(b'{"a":1}')
        link = base / "link.json"; link.symlink_to(target)
        with self.assertRaises(OSError): core.load_document(link)
        directory_link = base / "redirect"; directory_link.symlink_to(base, target_is_directory=True)
        with self.assertRaises(OSError): core.load_document(directory_link / target.name)
        fifo = base / "pipe"; os.mkfifo(fifo)
        with self.assertRaisesRegex(core.PolicyReviewError, "regular"):
            core.load_document(fifo)
        for raw in (b'{"a":1,"a":2}', b'{"a": NaN}', b'{"a":1}\n'):
            target.write_bytes(raw)
            with self.assertRaises((core.PolicyReviewError, core.contracts.VerificationError)):
                core.load_document(target)
        target.write_bytes(b'12345')
        with self.assertRaisesRegex(core.PolicyReviewError, "bounded"):
            core.secure_read(target, limit=4)

    def test_cli_draft_round_trips_and_pending_validation_is_not_success(self):
        command = [sys.executable, "-I", "-S", str(HERE / "tools/review_source_policy.py")]
        result = subprocess.run(command + ["draft-private", "--pack", str(self.pack_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr)
        path = self.attachments / "draft.json"; path.write_bytes(result.stdout)
        draft = core.load_document(path)
        checked = subprocess.run(command + ["validate-private", "--pack", str(self.pack_path), "--private-review", str(path),
            "--expected-private-id", draft["review_id"]], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(checked.returncode, 2, checked.stderr)
        self.assertFalse(json.loads(checked.stdout)["private_replay_policy_ready"])


if __name__ == "__main__":
    unittest.main()
