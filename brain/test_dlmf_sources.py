"""DLMF derived request closure, bounded transport and complete evidence tests."""
import base64
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dlmf_source_evidence as core
import dlmf_sources as cli

WHEN = "2026-09-08T12:00:00Z"


class DlmfSourcesTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.programs = {name: (core.ROOT / name).read_bytes() for name in core.TOOL_FILES}
        self.profile = {"files": [{"path": name, "sha256": core.sha(raw)} for name, raw in sorted(self.programs.items())], "policy": copy.deepcopy(core.POLICY)}
        self.profile["profile_id"] = core.profile_id(self.profile)
        self.registry = self.root / "profiles.json"
        self.registry.write_bytes(core.canonical({"schema": core.PROFILE_SCHEMA, "current_profile": self.profile["profile_id"], "profiles": [self.profile]}))
        patch = mock.patch.object(core, "REGISTRY", self.registry); patch.start(); self.addCleanup(patch.stop)
        self.tool = {"schema": core.TOOL_SCHEMA, "profile_id": self.profile["profile_id"], "files": self.profile["files"],
            "python": {"version": "CPython 3.12.13 -I -S", "sha256": "a" * 64}, "curl": {"version": "curl fixture", "sha256": "b" * 64}}
        self.sections = sorted({*(str(ch) + ".1" for ch in core.POLICY["chapters"]), "1.2", "1.10"}, key=lambda s: tuple(map(int, s.split("."))))
        self.plan = {"schema": core.PLAN_SCHEMA, "source": core.SOURCE, "uri": core.BASE, "minimum_pages": len(self.sections), "minimum_links": len(self.sections)}
        self.pages = {"idx": self.html("Index", ["../1.2", "../1.10", "../99.1"])}
        for chapter in core.POLICY["chapters"]:
            self.pages["toc_" + str(chapter)] = self.html("Chapter " + str(chapter) + " Topic", ["./" + str(chapter) + ".1", "./99.1"])
        for index, section in enumerate(self.sections):
            self.pages["section_" + section] = self.html("§" + section + " Fixture Cafe\u0301", ["./" + self.sections[(index + 1) % len(self.sections)] + "#E1", "./99.1", "./" + section])
        self.names = ["idx", *("toc_" + str(ch) for ch in core.POLICY["chapters"]), *("section_" + s for s in self.sections)]
        self.records = [self.record(name, self.pages[name]) for name in self.names]

    @staticmethod
    def html(title, hrefs):
        return ('<html><head><title>DLMF: ' + title + '</title></head><body>' + ''.join('<a href="' + href + '">link</a>' for href in hrefs) + '</body></html>').encode()

    @staticmethod
    def metadata(raw, **changes):
        return {"curl_exit_code": 0, "http_status": 200, "content_type": "text/html", "sha256": core.sha(raw), "bytes": len(raw), **changes}

    def record(self, name, raw):
        return {"request": core.parameters(name), "response": self.metadata(raw), "body_base64": base64.b64encode(raw).decode()}

    def capture(self):
        return core.capture_files(self.plan, self.records, self.tool, self.programs, WHEN)

    def build(self):
        capture = {name: raw for name, raw in self.capture().items() if name != "manifest.json"}
        return core.build_export(capture, self.profile, self.programs, WHEN)

    def test_complete_index_tocs_and_every_derived_section_are_replayed(self):
        facts, pending = core.replay(self.plan, self.records, self.programs)
        self.assertIsNone(pending)
        self.assertEqual((facts["requests"], facts["pages"], facts["links"]), (75, 38, 38))
        self.assertEqual(self.records[0]["request"]["uri"], "https://dlmf.nist.gov/idx/")
        self.assertEqual([record["request"]["uri"] for record in self.records[37:40]], [core.BASE + name for name in ("1.1", "1.2", "1.10")])
        capture = core.archive.publish(self.capture(), self.root / "captures", core.CAPTURE_SCHEMA)
        core.verify_capture(capture)
        target = core.archive.publish(self.build(), self.root / "exports", core.EXPORT_SCHEMA)
        result = core.verify_export(target)
        self.assertEqual(result["facts"], facts)
        source = json.loads((target / "source-manifest.json").read_bytes())
        body = [item for item in source["objects"] if "normalized" in item["roles"]]
        self.assertEqual(len(body), 1); self.assertEqual(body[0]["roles"], ["normalized", "raw"])
        self.assertEqual(source["pin"], {"type": "content_sha256", "value": body[0]["sha256"]})

    def test_missing_toc_section_reordered_duplicate_and_extra_requests_fail(self):
        for records in (self.records[:-1], self.records[:4] + self.records[5:], self.records[:38] + self.records[39:],
                self.records[:37] + [self.records[38], self.records[37]] + self.records[39:], self.records + [self.records[-1]]):
            with self.assertRaises(core.EvidenceError): core.replay(self.plan, records, self.programs)

    def test_cross_chapter_navigation_does_not_invent_sections(self):
        state = core.WalkState(self.plan, self.programs["brain/ingest/dlmf.py"])
        for record in self.records[:37]: state.accept(record)
        self.assertEqual(state.sections, set(self.sections))
        self.assertNotIn("section_99.1", state.names)
        record = self.record("toc_2", self.html("Chapter 2 Topic", ["./1.1"]))
        state = core.WalkState(self.plan, self.programs["brain/ingest/dlmf.py"])
        for prior in self.records[:2]: state.accept(prior)
        with self.assertRaisesRegex(core.EvidenceError, "own-chapter"): state.accept(record)

    def test_title_html_status_and_early_floor_validation_fail_closed(self):
        bad = [(b"<html>oops</html>", {}), (self.html("Wrong Site", ["./1.1"]), {}),
            (self.pages["idx"][:-7], {}), (self.pages["idx"], {"http_status": 301}), (self.pages["idx"], {"curl_exit_code": 18})]
        for raw, changes in bad:
            records = copy.deepcopy(self.records); records[0] = self.record("idx", raw); records[0]["response"].update(changes)
            with self.assertRaises(core.EvidenceError): core.replay(self.plan, records, self.programs)
        state = core.WalkState({**self.plan, "minimum_pages": 100}, self.programs["brain/ingest/dlmf.py"])
        with self.assertRaisesRegex(core.EvidenceError, "enumerated sections"):
            for record in self.records[:37]: state.accept(record)
        with self.assertRaisesRegex(core.EvidenceError, "completeness floors"):
            core.replay({**self.plan, "minimum_links": 100}, self.records, self.programs)

    def test_unapproved_endpoint_policy_and_parser_preimages_fail(self):
        for name in ("idx/", "../idx", "toc_37", "section_0.1", "section_1.2?x=y", "section_1.2#E1"):
            with self.assertRaises(core.EvidenceError): core.parameters(name)
        for path in ("brain/ingest/dlmf.py", "brain/mathlib_source_evidence.py"):
            changed = dict(self.programs); changed[path] += b"\n# changed helper"
            with self.assertRaises(core.EvidenceError): core.verify_programs(self.profile, changed)
        with self.assertRaisesRegex(core.EvidenceError, "selector differs"):
            core.parser(self.programs["brain/ingest/dlmf.py"].replace(b"def clean_title(", b"def changed_title("))

    def test_rehashed_changed_source_export_still_fails_independent_replay(self):
        files = self.build(); files["acquisition/requests/" + core.request(self.records[0]["request"])["parameters_sha256"] + ".json"] += b" "
        files.pop("manifest.json")
        path = core.archive.publish(core.archive.manifest_files(files, core.EXPORT_SCHEMA), self.root / "changed", core.EXPORT_SCHEMA)
        with self.assertRaises(core.EvidenceError): core.verify_export(path)

    def test_acquirer_uses_derived_exact_list_and_never_loads_cache(self):
        plan = self.root / "plan.json"; plan.write_bytes(core.canonical(self.plan))
        urls = []
        responses = iter((base64.b64decode(record["body_base64"]), record["response"]) for record in self.records)
        def fetch(parameters, _curl): urls.append(parameters["uri"]); return next(responses)
        with mock.patch.object(cli, "require_startup"), mock.patch.object(cli, "runtime_identity", return_value=(self.tool, self.programs)), \
                mock.patch.object(cli, "transport", side_effect=fetch), mock.patch.object(cli.time, "sleep"), mock.patch.object(sys, "stderr", io.StringIO()):
            target = cli.acquire(plan, self.root / "captures", Path(sys.executable).resolve())
        self.assertEqual(urls, [record["request"]["uri"] for record in self.records])
        core.verify_capture(target)

    def test_failed_html_stops_before_next_request_and_retains_no_receipt(self):
        plan = self.root / "plan.json"; plan.write_bytes(core.canonical(self.plan))
        with mock.patch.object(cli, "require_startup"), mock.patch.object(cli, "runtime_identity", return_value=(self.tool, self.programs)), \
                mock.patch.object(cli, "transport", return_value=(b"error", self.metadata(b"error"))) as fetch, mock.patch.object(cli.time, "sleep"), \
                self.assertRaises(core.EvidenceError):
            cli.acquire(plan, self.root / "captures", Path(sys.executable).resolve())
        self.assertEqual(fetch.call_count, 1)
        self.assertFalse((self.root / "captures").exists())
        target = next((self.root / "captures-incomplete").iterdir())
        files, _ = core.archive.read_bundle(target, "wikilean.dlmf-incomplete-attempt/v1")
        self.assertFalse(any("receipt" in name for name in files))
        record = json.loads(files["transcript.json"])["failed_request"]
        self.assertEqual(base64.b64decode(record["body_base64"]), b"error")

    def test_eof_then_lingering_process_retains_received_bytes(self):
        script = self.root / "linger"
        script.write_text("#!" + str(Path(sys.executable).resolve()) + "\nimport os,time\nos.write(1,b'partial-body')\nos.close(1);os.close(2)\ntime.sleep(30)\n")
        script.chmod(0o700)
        with mock.patch.object(cli, "PROCESS_EXIT_TIMEOUT_SECONDS", 0.05), self.assertRaises(cli.TransportFailure) as failure:
            cli.transport(core.parameters("idx"), script)
        self.assertEqual(failure.exception.raw, b"partial-body")
        self.assertLess(failure.exception.response["curl_exit_code"], 0)

    def test_response_and_total_byte_budgets_stop_before_extra_requests(self):
        state = core.WalkState(self.plan, self.programs["brain/ingest/dlmf.py"])
        with mock.patch.dict(core.POLICY, {"maximum_total_response_bytes": 1}), self.assertRaisesRegex(core.EvidenceError, "response budget"):
            state.accept(self.records[0])
        state = core.WalkState(self.plan, self.programs["brain/ingest/dlmf.py"])
        with mock.patch.dict(core.POLICY, {"maximum_response_bytes": 1}), self.assertRaisesRegex(core.EvidenceError, "exceed bound"):
            state.accept(self.record("idx", self.pages["idx"]))


    def test_actual_pipe_transport_uses_only_explicit_request_environment_and_controls(self):
        raw = base64.b64decode(self.records[0]["body_base64"])
        script = self.root / "curl"
        invocation = self.root / "invocation.json"
        script.write_text("#!" + str(Path(sys.executable).resolve()) + "\nimport json, os, sys\nfrom pathlib import Path\n" +
            "Path(" + repr(str(invocation)) + ").write_text(json.dumps({'argv':sys.argv,'env':dict(os.environ)}))\n" +
            "sys.stdout.buffer.write(" + repr(raw) + ")\nsys.stderr.buffer.write(" + repr(cli.TRAILER + b"200\ttext/html; charset=utf-8\n") + ")\n")
        script.chmod(0o700)
        with mock.patch.dict(os.environ, {"PRIVATE_TOKEN": "fixture", "HTTP_PROXY": "http://invalid.example"}), \
                mock.patch.object(cli.subprocess, "Popen", wraps=subprocess.Popen) as launch:
            actual, metadata = cli.transport(self.records[0]["request"], script)
        self.assertEqual(actual, raw); self.assertEqual(metadata, self.records[0]["response"])
        self.assertEqual(launch.call_args.kwargs["env"], cli.ENVIRONMENT)
        args = json.loads(invocation.read_bytes())["argv"]
        self.assertEqual(args[1], "-q")
        for forbidden in ("--location", "--retry", "--netrc", "--insecure", "--user", "--compressed"):
            self.assertNotIn(forbidden, args)
        self.assertEqual(args[args.index("--proxy") + 1], "")
        self.assertIn(self.records[0]["request"]["uri"], args)

    def test_actual_v3_compiler_accepts_raw_identity_parent(self):
        import test_compile_offline_pack_v2 as fixture_module
        target = core.archive.publish(self.build(), self.root / "exports", core.EXPORT_SCHEMA)
        fragment = json.loads((target / "source-fragment.json").read_bytes())
        fixture = fixture_module.OfflinePackCompilerTest(); fixture.setUp(); self.addCleanup(fixture.tearDown)
        fixture._upgrade_plan_v3()
        source = fragment["sources"][0]
        raw = next(o for o in source["objects"] if o["name"] == "page_transcript")
        (fixture.external / "dlmf-transcript.json").write_bytes((target / raw["path"]).read_bytes())
        fixture.inventory["inputs"].append({"id": "dlmf-transcript", "class": "immutable_source_object", "cardinality": "one", "root": "external",
            "path": "dlmf-transcript.json", "consumers": ["brain/replay.py"], "purpose": "complete DLMF raw evidence fixture", "requirement": "required"})
        fixture.inventory["inputs"].sort(key=lambda x: x["id"])
        fixture.inventory["inventory_id"] = core.contracts.reducer_input_inventory_identity(fixture.inventory)
        fixture.plan["inventory_id"] = fixture.inventory["inventory_id"]
        fixture.plan["sources"] = sorted([*fixture.plan["sources"], source], key=lambda x: x["source"])
        fixture.plan["input_bindings"].append({"input_id": "dlmf-transcript", "state": "present", "sources": [core.SOURCE],
            "members": [{"path": "dlmf-transcript.json", "source": core.SOURCE, "object": "page_transcript"}]})
        fixture.plan["input_bindings"].sort(key=lambda x: x["input_id"])
        fixture.inventory_path.write_bytes(core.canonical(fixture.inventory)); fixture.plan_path.write_bytes(core.canonical(fixture.plan))
        packed = fixture_module.compiler.compile_offline_pack_v2(fixture.plan_path.resolve(), fixture.inventory_path.resolve(),
            (fixture.base / "dlmf-pack").resolve(), roots={"repo": fixture.repo.resolve(), "external": fixture.external.resolve(), fragment["physical_root"]: target}, git_executable="/usr/bin/git")
        manifest, _ = core.contracts.load_canonical_json(packed.manifest_path)
        core.contracts.verify_offline_pack_files(manifest, packed.root, manifest_path=packed.manifest_path)


if __name__ == "__main__": unittest.main()
