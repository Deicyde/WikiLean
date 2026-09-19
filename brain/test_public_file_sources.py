"""Closed fresh public-file evidence, bounded transport and compiler fixtures."""
import copy
import gzip
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import public_file_source_evidence as core
import public_file_sources as cli

WHEN = "2026-09-08T12:00:00Z"


class PublicFileSourcesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = gzip.compress(random.Random(0).randbytes(1024 * 1024 + 1024), mtime=0)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.programs = {name: (core.ROOT / name).read_bytes() for name in core.TOOL_FILES}
        self.profile = {"files": [{"path": name, "sha256": core.sha(self.programs[name])} for name in core.TOOL_FILES],
            "source_policies": copy.deepcopy(core.SOURCE_POLICIES)}
        self.profile["profile_id"] = core.profile_id(self.profile)
        self.registry = {"schema": core.PROFILE_SCHEMA, "current_profile": self.profile["profile_id"], "profiles": [self.profile]}
        self.registry_path = self.root / "profiles.json"
        self.registry_path.write_bytes(core.canonical(self.registry))
        patch = mock.patch.object(core, "REGISTRY", self.registry_path)
        patch.start(); self.addCleanup(patch.stop)
        self.plan = {"schema": core.PLAN_SCHEMA, "source": "proofwiki-dump", "uri": core.SOURCE_POLICIES["proofwiki-dump"]["uri"]}
        self.tool = {"schema": core.TOOL_SCHEMA, "profile_id": self.profile["profile_id"], "files": self.profile["files"],
            "python": {"version": "CPython 3.12.13 -I -S", "sha256": "b" * 64},
            "curl": {"version": "curl 8.7.1 fixture", "sha256": "c" * 64}}
        self.response = self.response_for(self.raw)

    def response_for(self, raw, **changes):
        return {"curl_exit_code": 0, "http_status": 200, "content_type": "application/gzip",
            "sha256": core.sha(raw), "bytes": len(raw), **changes}

    def capture(self):
        return core.capture_files(self.plan, self.raw, self.response, self.tool, self.programs, WHEN)

    def build(self):
        capture = {name: raw for name, raw in self.capture().items() if name != "manifest.json"}
        return core.build_export(capture, self.profile, self.programs, WHEN)

    def test_complete_capture_export_binary_identity_and_original_evidence(self):
        capture = core.archive.publish(self.capture(), self.root / "captures", core.CAPTURE_SCHEMA)
        self.assertEqual(core.verify_capture(capture)[1], self.raw)
        target = core.archive.publish(self.build(), self.root / "exports", core.EXPORT_SCHEMA)
        result = core.verify_export(target)
        self.assertEqual(result["source"], "proofwiki-dump")
        source = json.loads((target / "source-manifest.json").read_bytes())
        raw_objects = [item for item in source["objects"] if "raw" in item["roles"]]
        normalized = [item for item in source["objects"] if "normalized" in item["roles"]]
        self.assertEqual(raw_objects, normalized)
        self.assertEqual(len(raw_objects), 1)
        self.assertEqual(source["pin"], {"type": "content_sha256", "value": core.sha(self.raw)})
        self.assertEqual((target / raw_objects[0]["path"]).read_bytes(), self.raw)
        lineage = json.loads((target / "evidence/lineage.json").read_bytes())
        receipt = json.loads((capture / "receipt.json").read_bytes())
        self.assertEqual(lineage["acquisition_receipt_ids"], [receipt["acquisition_receipt_id"]])
        self.assertEqual(lineage["tool"]["sha256"], core.sha(core.canonical(self.profile)))
        self.assertEqual((target / "acquisition/receipt.json").read_bytes(), (capture / "receipt.json").read_bytes())

    def test_status_media_size_and_gzip_integrity_fail_closed(self):
        cases = [(self.raw, self.response_for(self.raw, http_status=302)),
            (self.raw, self.response_for(self.raw, curl_exit_code=18)),
            (self.raw, self.response_for(self.raw, content_type="text/html")),
            (self.raw, self.response_for(self.raw, sha256="0" * 64)),
            (b"html" * 300000, self.response_for(b"html" * 300000)),
            (gzip.compress(b"small", mtime=0), self.response_for(gzip.compress(b"small", mtime=0))),
            (self.raw[:-1], self.response_for(self.raw[:-1])),
            (self.raw[:-8] + b"00000000", self.response_for(self.raw[:-8] + b"00000000"))]
        for raw, response in cases:
            with self.subTest(response=response), self.assertRaises(core.EvidenceError):
                core.normalize(self.plan, raw, response, self.profile)
        tiny_profile = copy.deepcopy(self.profile)
        tiny_profile["source_policies"]["proofwiki-dump"]["maximum_uncompressed_bytes"] = 32
        with self.assertRaisesRegex(core.EvidenceError, "expansion bound"):
            core.normalize(self.plan, self.raw, self.response, tiny_profile)

    def test_plan_exact_endpoint_no_alias_or_unreviewed_options(self):
        for changes in ({"uri": self.plan["uri"].replace("https:", "http:")}, {"uri": self.plan["uri"] + "?x=1"},
                {"source": "unknown"}, {"cached_file": "local.gz"}):
            with self.assertRaises(core.EvidenceError): core.validate_plan({**self.plan, **changes}, self.profile)

    def test_retained_program_closure_and_profile_are_required(self):
        files = self.capture()
        capture = {name: raw for name, raw in files.items() if name != "manifest.json"}
        for member in ("implementation/brain/mathlib_source_evidence.py", "implementation/brain/tools/source_plan_contracts.py", "profile.json"):
            changed = dict(capture); changed[member] += b" "
            with self.assertRaises(core.EvidenceError): core.verify_capture_files(changed)
        changed = dict(self.programs); changed["brain/tools/authority_contracts.py"] += b"\n# changed helper"
        with self.assertRaises(core.EvidenceError): core.verify_programs(self.profile, changed)
        for version in ("CPython 3.12.13", "CPython 3.13.0 -I -S", "PyPy 3.12.0 -I -S"):
            tool = copy.deepcopy(self.tool); tool["python"]["version"] = version
            with self.assertRaises(core.EvidenceError): core.validate_tool(tool)

    def test_historical_whole_profile_remains_verifiable_and_mixing_fails(self):
        capture = self.capture()
        new_programs = dict(self.programs)
        new_programs["brain/public_file_sources.py"] += b"\n# reviewed next generation\n"
        new_profile = copy.deepcopy(self.profile)
        new_profile["files"] = [{"path": name, "sha256": core.sha(new_programs[name])} for name in core.TOOL_FILES]
        new_profile["profile_id"] = core.profile_id(new_profile)
        self.registry["profiles"].append(new_profile)
        self.registry["profiles"].sort(key=lambda item: item["profile_id"])
        self.registry["current_profile"] = new_profile["profile_id"]
        self.registry_path.write_bytes(core.canonical(self.registry))
        self.assertEqual(core.verify_capture_files({name: raw for name, raw in capture.items() if name != "manifest.json"})[1], self.raw)
        self.assertNotEqual(core.sha(core.canonical(self.profile)), core.sha(core.canonical(new_profile)))
        with self.assertRaises(core.EvidenceError): core.verify_programs(new_profile, self.programs)

    def test_rehashed_changed_export_or_omitted_program_still_fails_reconstruction(self):
        for member in ("normalization/profile.json", "implementation/brain/mathlib_source_evidence.py", "acquisition/request.json"):
            files = self.build(); files[member] += b" "
            files.pop("manifest.json")
            target = core.archive.publish(core.archive.manifest_files(files, core.EXPORT_SCHEMA), self.root / core.sha(member.encode()), core.EXPORT_SCHEMA)
            with self.assertRaises(core.EvidenceError): core.verify_export(target)

    def fake_curl(self, raw, *, status=200, content_type="application/gzip", exit_code=0, trailing_stderr=b""):
        raw_path = self.root / "fake-body.bin"; raw_path.write_bytes(raw)
        argv_path = self.root / "argv.json"
        fake = self.root / "curl"
        trailer = cli.TRAILER + str(status).zfill(3).encode() + b"\t" + content_type.encode() + b"\n" + trailing_stderr
        # Disable interpreter site startup so the fake records the environment
        # passed at exec, not vendor-specific variables added by site hooks.
        fake.write_text("#!" + str(Path(sys.executable).resolve()) + " -S\n" +
            "import json, os, sys\nfrom pathlib import Path\n" +
            "Path(" + repr(str(argv_path)) + ").write_text(json.dumps({'argv':sys.argv,'env':dict(os.environ)}))\n" +
            "sys.stdout.buffer.write(Path(" + repr(str(raw_path)) + ").read_bytes())\n" +
            "sys.stderr.buffer.write(" + repr(trailer) + ")\nsys.exit(" + str(exit_code) + ")\n")
        fake.chmod(0o700)
        return fake, argv_path

    def test_real_pipe_transport_exact_credentials_free_get_and_numeric_metadata(self):
        fake, argv_path = self.fake_curl(self.raw, content_type="application/gzip; charset=binary")
        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://invalid.example", "CURL_HOME": "/nonexistent", "PRIVATE_TOKEN": "fixture-only"}), \
                mock.patch.object(cli.subprocess, "Popen", wraps=subprocess.Popen) as launch:
            raw, response = cli.transport(self.plan, self.profile, fake)
        self.assertEqual(launch.call_args.kwargs["env"], cli.ENVIRONMENT)
        self.assertEqual(raw, self.raw); self.assertEqual(response, self.response)
        invocation = json.loads(argv_path.read_bytes())
        # CoreFoundation may inject its per-user encoding variable after exec
        # on Darwin; the explicit process-launch environment is asserted above.
        child_environment = dict(invocation["env"])
        if sys.platform == "darwin": child_environment.pop("__CF_USER_TEXT_ENCODING", None)
        self.assertEqual(child_environment, cli.ENVIRONMENT)
        args = invocation["argv"][1:]
        self.assertEqual(args[0], "-q")
        self.assertEqual(args[args.index("--url") + 1], self.plan["uri"])
        for forbidden in ("--location", "--retry", "--netrc", "--insecure", "--compressed", "--user"):
            self.assertNotIn(forbidden, args)
        self.assertEqual(args[args.index("--proxy") + 1], "")

    def test_streaming_bound_aborts_and_missing_trailer_never_claims_success(self):
        fake, _ = self.fake_curl(self.raw)
        small = copy.deepcopy(self.profile)
        small["source_policies"]["proofwiki-dump"]["maximum_bytes"] = 1024
        with self.assertRaises(cli.TransportFailure) as failure: cli.transport(self.plan, small, fake)
        self.assertEqual(len(failure.exception.raw), 1024)
        fake, _ = self.fake_curl(self.raw, trailing_stderr=b"bad trailer\n")
        raw, response = cli.transport(self.plan, self.profile, fake)
        self.assertEqual(response["http_status"], 0)
        with self.assertRaises(core.EvidenceError): core.normalize(self.plan, raw, response, self.profile)

    def test_eof_then_lingering_child_retains_complete_received_body(self):
        fake, _ = self.fake_curl(self.raw)
        script = fake.read_text().replace("sys.exit(0)", "sys.stdout.flush(); sys.stderr.flush(); os.close(1); os.close(2)\nimport time; time.sleep(60)")
        fake.write_text(script)
        with mock.patch.object(cli, "PROCESS_EXIT_TIMEOUT_SECONDS", 0.05), self.assertRaises(cli.TransportFailure) as failure:
            cli.transport(self.plan, self.profile, fake)
        self.assertEqual(failure.exception.raw, self.raw)
        self.assertEqual(failure.exception.response["http_status"], 200)
        self.assertLess(failure.exception.response["curl_exit_code"], 0)
        self.assertEqual(failure.exception.response["sha256"], core.sha(self.raw))

    def test_reader_failure_retains_already_received_prefix(self):
        fake, _ = self.fake_curl(self.raw)
        original_read, calls = os.read, []
        def broken_reader(fd, amount):
            # subprocess reads its exec-error pipe at a different buffer size;
            # inject only after the transport has captured one stdout chunk.
            if amount == 1024 * 1024:
                if calls: raise OSError("fixture reader failure")
                chunk = original_read(fd, amount); calls.append(chunk); return chunk
            return original_read(fd, amount)
        with mock.patch.object(cli.os, "read", side_effect=broken_reader), self.assertRaises(cli.TransportFailure) as failure:
            cli.transport(self.plan, self.profile, fake)
        self.assertGreater(len(failure.exception.raw), 0)
        self.assertEqual(failure.exception.raw, self.raw[:len(failure.exception.raw)])
        self.assertEqual(failure.exception.response["sha256"], core.sha(failure.exception.raw))
        self.assertLess(failure.exception.response["curl_exit_code"], 0)

    def test_failed_or_invalid_200_attempt_preserves_only_private_diagnostics(self):
        for index, (raw, response) in enumerate(((b"broken gzip", self.response_for(b"broken gzip")),
                (b"temporary error", self.response_for(b"temporary error", http_status=503, curl_exit_code=22)))):
            plan_path = self.root / "plan.json"; plan_path.write_bytes(core.canonical(self.plan))
            store = self.root / ("captures" + str(index))
            with mock.patch.object(cli, "require_startup"), mock.patch.object(cli, "runtime_identity", return_value=(self.tool, self.programs)), \
                    mock.patch.object(cli, "transport", return_value=(raw, response)), self.assertRaises(core.EvidenceError):
                cli.acquire(plan_path, store, Path(sys.executable).resolve())
            self.assertFalse(store.exists())
            diagnostic = next((store.parent / (store.name + "-incomplete")).iterdir())
            files, _ = core.archive.read_bundle(diagnostic, "wikilean.public-file-incomplete-attempt/v1")
            self.assertFalse(any("receipt" in name for name in files))
            self.assertEqual(files["raw/response-prefix.bin"], raw)
            attempt = json.loads(files["attempt.json"])
            self.assertEqual(attempt["response"], response)
            self.assertNotIn("stderr", attempt)
            with self.assertRaises(core.EvidenceError): core.verify_capture(diagnostic)

    def test_private_diagnostics_bound_and_checksum(self):
        with mock.patch.object(cli, "DIAGNOSTIC_BODY_LIMIT", 4):
            path = cli.retain_incomplete(self.root / "captures", core.canonical(self.plan), self.tool, self.programs,
                self.profile, b"0123456789", self.response_for(b"0123456789"), core.EvidenceError("fixture"))
        files, _ = core.archive.read_bundle(path, "wikilean.public-file-incomplete-attempt/v1")
        attempt = json.loads(files["attempt.json"])
        self.assertEqual(files["raw/response-prefix.bin"], b"0123")
        self.assertTrue(attempt["body_truncated"])
        self.assertEqual(attempt["captured_sha256"], core.sha(b"0123456789"))

    def test_actual_pack_compiler_accepts_closed_compressed_identity_source(self):
        import test_compile_offline_pack_v2 as compiler_fixture
        target = core.archive.publish(self.build(), self.root / "exports", core.EXPORT_SCHEMA)
        fragment = json.loads((target / "source-fragment.json").read_bytes())
        fixture = compiler_fixture.OfflinePackCompilerTest(); fixture.setUp(); self.addCleanup(fixture.tearDown)
        fixture._upgrade_plan_v3()
        physical = self.root / "logical"; (physical / "proofwiki").mkdir(parents=True)
        (physical / "proofwiki/latest.xml.gz").write_bytes(self.raw)
        fixture.inventory["roots"].append({"id": "proofwiki_logical", "kind": "external_tree"})
        fixture.inventory["roots"].sort(key=lambda item: item["id"])
        fixture.inventory["inputs"].append({"id": "proofwiki-compressed-dump", "class": "immutable_source_object", "cardinality": "one",
            "consumers": ["brain/replay.py"], "path": "proofwiki/latest.xml.gz", "purpose": "compressed source evidence integration",
            "requirement": "required", "root": "proofwiki_logical"})
        fixture.inventory["inputs"].sort(key=lambda item: item["id"])
        fixture.inventory["inventory_id"] = core.contracts.reducer_input_inventory_identity(fixture.inventory)
        fixture.plan["inventory_id"] = fixture.inventory["inventory_id"]
        fixture.plan["sources"] = sorted([*fixture.plan["sources"], *fragment["sources"]], key=lambda item: item["source"])
        fixture.plan["input_bindings"] = sorted([*fixture.plan["input_bindings"], *fragment["input_bindings"]], key=lambda item: item["input_id"])
        fixture.inventory_path.write_bytes(core.canonical(fixture.inventory)); fixture.plan_path.write_bytes(core.canonical(fixture.plan))
        packed = compiler_fixture.compiler.compile_offline_pack_v2(fixture.plan_path.resolve(), fixture.inventory_path.resolve(),
            (fixture.base / "proofwiki-pack").resolve(), roots={"repo": fixture.repo.resolve(), "external": fixture.external.resolve(),
                fragment["physical_root"]: target, "proofwiki_logical": physical}, git_executable="/usr/bin/git")
        manifest, _ = core.contracts.load_canonical_json(packed.manifest_path)
        core.contracts.verify_offline_pack_files(manifest, packed.root, manifest_path=packed.manifest_path)


if __name__ == "__main__":
    unittest.main()
