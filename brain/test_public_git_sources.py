"""Adversarial fixtures for fresh public Git capture and normalization evidence."""
import copy
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import public_git_source_evidence as core
import public_git_sources as cli

WHEN = "2026-09-08T12:00:00Z"


class PublicGitSourcesTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.programs = {name: (core.ROOT / name).read_bytes() for name in core.TOOL_FILES}
        self.profile = {"files": [{"path": name, "sha256": core.sha(self.programs[name])} for name in core.TOOL_FILES]}
        self.profile["profile_id"] = core.profile_id(self.profile)
        self.registry = {"schema": core.PROFILE_SCHEMA, "current_profile": self.profile["profile_id"], "profiles": [self.profile]}
        registry = self.root / "profiles.json"
        registry.write_bytes(core.canonical(self.registry))
        patch = mock.patch.object(core, "REGISTRY", registry)
        patch.start(); self.addCleanup(patch.stop)
        self.plan = {"schema": core.PLAN_SCHEMA, "source": "formal-conjectures-source",
            "repository": "google-deepmind/formal-conjectures", "commit": "a" * 40}
        self.tool = {"schema": core.TOOL_SCHEMA, "profile_id": self.profile["profile_id"], "files": self.profile["files"],
            "python": {"version": "CPython 3.12.13 -I -S", "sha256": "b" * 64},
            "gh": {"version": "gh version fixture", "sha256": "c" * 64}}
        self.tree_files = {"LICENSE": ("100644", b"fixture license\n"), "src/A.lean": ("100644", b"theorem alpha : True := trivial\n"),
                           "run.sh": ("100755", b"#!/bin/sh\nexit 0\n"), "link": ("120000", b"src/A.lean")}
        repo = self.root / "git-oracle"; repo.mkdir()
        subprocess.run(["/usr/bin/git", "init", "-q", str(repo)], check=True)
        for path, (mode, raw) in self.tree_files.items():
            target = repo / path; target.parent.mkdir(parents=True, exist_ok=True)
            if mode == "120000": target.symlink_to(raw.decode())
            else:
                target.write_bytes(raw); target.chmod(0o755 if mode == "100755" else 0o644)
        subprocess.run(["/usr/bin/git", "-C", str(repo), "add", "--", *self.tree_files], check=True)
        self.tree = subprocess.check_output(["/usr/bin/git", "-C", str(repo), "write-tree"]).decode().strip()
        metadata = {"sha": self.plan["commit"], "url": "https://api.github.com/repos/" + self.plan["repository"] + "/git/commits/" + self.plan["commit"],
            "tree": {"sha": self.tree, "url": "https://api.github.com/repos/" + self.plan["repository"] + "/git/trees/" + self.tree}}
        self.raw = {"commit": core.canonical(metadata), "source_tar": self.tar(self.tree_files)}

    def tar(self, files):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            parent = tarfile.TarInfo("upstream-root"); parent.type = tarfile.DIRTYPE; archive.addfile(parent)
            for path, (mode, raw) in files.items():
                member = tarfile.TarInfo("upstream-root/" + path)
                if mode == "120000":
                    member.type = tarfile.SYMTYPE; member.linkname = raw.decode(); archive.addfile(member)
                else:
                    member.size = len(raw); member.mode = 0o755 if mode == "100755" else 0o644
                    archive.addfile(member, io.BytesIO(raw))
        return output.getvalue()

    def capture(self):
        return core.capture_files(self.plan, self.raw, self.tool, self.programs, WHEN)

    def export(self):
        return core.build_export({name: data for name, data in self.capture().items() if name != "manifest.json"},
                                self.profile, self.programs, WHEN)

    def test_complete_capture_and_export_rebuild_real_git_tree_and_never_extract_links(self):
        source, tree = core.normalize(self.plan, self.raw)
        self.assertEqual(source, self.tree_files)
        self.assertEqual(tree, self.tree)
        capture = core.archive.publish(self.capture(), self.root / "captures", core.CAPTURE_SCHEMA)
        self.assertEqual(core.verify_capture(capture)[0], self.plan)
        target = core.archive.publish(self.export(), self.root / "exports", core.EXPORT_SCHEMA)
        result = core.verify_export(target)
        self.assertEqual(result["source"], self.plan["source"])
        fragment = json.loads((target / "source-fragment.json").read_bytes())
        members = fragment["input_bindings"][0]["members"]
        self.assertEqual({item["path"] for item in members}, set(self.tree_files) - {"link"})
        self.assertFalse(any(path.is_symlink() for path in target.rglob("*")))

    def test_missing_changed_or_mode_changed_archive_content_fails_complete_tree(self):
        for changed in ({key: value for key, value in self.tree_files.items() if key != "LICENSE"},
                        {**self.tree_files, "src/A.lean": ("100644", b"changed")},
                        {**self.tree_files, "run.sh": ("100644", self.tree_files["run.sh"][1])},
                        {**self.tree_files, "link": ("120000", b"elsewhere")}):
            with self.assertRaisesRegex(core.EvidenceError, "root tree"):
                core.normalize(self.plan, {**self.raw, "source_tar": self.tar(changed)})

    def test_wrong_commit_repository_or_endpoint_is_not_the_reviewed_source(self):
        for change in ({"sha": "f" * 40}, {"url": "https://api.github.com/repos/attacker/fork/git/commits/" + self.plan["commit"]},
                       {"tree": {"sha": self.tree, "url": "https://example.invalid/tree"}}):
            metadata = json.loads(self.raw["commit"]); metadata.update(change)
            with self.assertRaises(core.EvidenceError):
                core.normalize(self.plan, {**self.raw, "commit": core.canonical(metadata)})
        for change in ({"commit": "main"}, {"repository": "attacker/fork"}, {"source": "unknown"}):
            with self.assertRaises(core.EvidenceError): core.validate_plan({**self.plan, **change})

    def test_upstream_unicode_metadata_is_retained_without_control_normalization(self):
        metadata = json.loads(self.raw["commit"])
        metadata.update(message="Cafe\u0301 update", author={"name": "Andre\u0301"})
        raw = json.dumps(metadata, ensure_ascii=False).encode()
        self.raw["commit"] = raw
        self.assertEqual(core.normalize(self.plan, self.raw)[1], self.tree)
        self.assertEqual(self.capture()["raw/commit"], raw)

    def test_complete_request_transcript_and_program_preimages_required(self):
        files = {name: data for name, data in self.capture().items() if name != "manifest.json"}
        for path in ("requests/commit.json", "raw/source_tar", "receipt.json", "implementation/brain/public_git_sources.py"):
            changed = dict(files); changed[path] += b" "
            with self.assertRaises((core.EvidenceError, core.contracts.VerificationError)):
                core.verify_capture_files(changed)

    def test_rehashed_normalized_output_cannot_pass_independent_replay(self):
        files = self.export()
        source = json.loads(files["source-manifest.json"])
        target_ref = next(item for item in source["objects"] if item["name"] == "file-" + core.sha(b"src/A.lean"))
        files[target_ref["path"]] = b"fake normalized content"
        files = core.archive.manifest_files({name: data for name, data in files.items() if name != "manifest.json"}, core.EXPORT_SCHEMA)
        target = core.archive.publish(files, self.root / "changed-export", core.EXPORT_SCHEMA)
        with self.assertRaisesRegex(core.EvidenceError, "independently reconstructed"):
            core.verify_export(target)

    def test_mixed_unreviewed_generation_and_module_origin_fail(self):
        changed = copy.deepcopy(self.tool); changed["files"][0]["sha256"] = "e" * 64
        with self.assertRaises(core.EvidenceError): core.validate_tool(changed)
        with mock.patch.object(core.github, "__file__", str(self.root / "foreign.py")):
            with self.assertRaises(core.EvidenceError): core.current_profile()

    def test_audit_times_do_not_change_source_manifest_identity(self):
        files = self.export()
        capture = {name: data for name, data in core.capture_files(self.plan, self.raw, self.tool, self.programs,
                    "2026-09-09T12:00:00Z").items() if name != "manifest.json"}
        later = core.build_export(capture, self.profile, self.programs, "2026-09-09T12:00:00Z")
        first_id = json.loads(files["source-manifest.json"])["source_manifest_id"]
        self.assertEqual(first_id, json.loads(later["source-manifest.json"])["source_manifest_id"])

    def test_failed_second_get_never_emits_a_complete_capture(self):
        plan = self.root / "plan.json"; plan.write_bytes(core.canonical(self.plan))
        gh = self.root / "gh"; gh.write_bytes(b"fixture gh")
        with mock.patch.object(core.github, "require_startup"), mock.patch.object(cli, "runtime_identity", return_value=(self.tool, self.programs)), \
             mock.patch.object(core.github, "transport", side_effect=[self.raw["commit"], core.EvidenceError("network failure")]) as transport:
            with self.assertRaisesRegex(core.EvidenceError, "network failure"):
                cli.acquire(plan, self.root / "failed-captures", gh)
        self.assertEqual(transport.call_count, 2)
        self.assertFalse((self.root / "failed-captures").exists())
        retained = list((self.root / "failed-captures-incomplete").iterdir())
        self.assertEqual(len(retained), 1)
        with self.assertRaises(core.EvidenceError): core.verify_capture(retained[0])

    def test_private_bundle_rejects_symlinks_loose_permissions_and_extra_files(self):
        target = core.archive.publish(self.capture(), self.root / "captures", core.CAPTURE_SCHEMA)
        (target / "extra").write_bytes(b"extra")
        with self.assertRaises(core.EvidenceError): core.verify_capture(target)
        (target / "extra").unlink()
        member = target / "raw/commit"; original = member.read_bytes()
        member.chmod(0o666)
        with self.assertRaises(core.EvidenceError): core.verify_capture(target)
        member.chmod(0o644); member.unlink()
        elsewhere = self.root / "foreign"; elsewhere.write_bytes(original); member.symlink_to(elsewhere)
        with self.assertRaises(core.EvidenceError): core.verify_capture(target)


if __name__ == "__main__": unittest.main()
