"""EOM continuation completeness, original evidence and real transport checks."""
import base64
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eom_source_evidence as core
import eom_sources as cli

WHEN = "2026-09-08T18:00:00Z"


class EomSourcesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.programs = {n: (core.ROOT / n).read_bytes() for n in core.TOOL_FILES}
        self.profile = {"files": [{"path": n, "sha256": core.sha(self.programs[n])} for n in core.TOOL_FILES], "policy": copy.deepcopy(core.POLICY)}
        self.profile["profile_id"] = core.profile_id(self.profile)
        self.registry = {"schema": core.PROFILE_SCHEMA, "current_profile": self.profile["profile_id"], "profiles": [self.profile]}
        registry_path = self.root / "profiles.json"
        registry_path.write_bytes(core.canonical(self.registry))
        patch = mock.patch.object(core, "REGISTRY", registry_path)
        patch.start(); self.addCleanup(patch.stop)
        self.plan = {"schema": core.PLAN_SCHEMA, "source": core.SOURCE, "uri": core.API, "minimum_pages": 2, "minimum_links": 2}
        self.tool = {"schema": core.TOOL_SCHEMA, "profile_id": self.profile["profile_id"], "files": self.profile["files"],
            "python": {"version": "CPython 3.12.13 -I -S", "sha256": "b" * 64}, "curl": {"version": "curl fixture", "sha256": "c" * 64}}
        self.cursor = {"continue": "gapcontinue||", "plcontinue": "1|0|Beta"}
        self.first = {"batchcomplete": "", "query": {"pages": {"1": self.page(1, "Alpha", ["Beta"]) }}, "continue": self.cursor}
        self.last = {"batchcomplete": "", "query": {"pages": {"1": self.page(1, "Alpha", ["Gamma"]), "2": self.page(2, "Beta", ["Beta"])}}}
        self.records = [self.record(self.first), self.record(self.last, self.cursor)]

    def page(self, pid, title, links):
        return {"pageid": pid, "ns": 0, "title": title, "links": [{"ns": 0, "title": x} for x in links]}

    def record(self, data, cursor=None, **metadata):
        raw = json.dumps(data, ensure_ascii=False).encode()
        response = {"curl_exit_code": 0, "http_status": 200, "content_type": "application/json", "bytes": len(raw), "sha256": core.sha(raw), **metadata}
        return {"request": core.parameters(cursor or {}), "response": response, "body_base64": base64.b64encode(raw).decode()}

    def capture(self):
        return core.capture_files(self.plan, self.records, self.tool, self.programs, WHEN)

    def export(self):
        return core.build_export({k: v for k, v in self.capture().items() if k != "manifest.json"}, self.profile, self.programs, WHEN)

    def test_full_chain_capture_export_retains_original_responses_and_requests(self):
        facts, cursor = core.replay(self.plan, self.records)
        self.assertEqual((facts["pages"], facts["links"], facts["requests"], cursor), (2, 2, 2, None))
        capture = core.archive.publish(self.capture(), self.root / "captures", core.CAPTURE_SCHEMA)
        core.verify_capture(capture)
        target = core.archive.publish(self.export(), self.root / "exports", core.EXPORT_SCHEMA)
        result = core.verify_export(target)
        self.assertEqual(result["facts"], facts)
        self.assertEqual((target / "acquisition/raw/transcript.json").read_bytes(), (capture / "raw/transcript.json").read_bytes())
        source = json.loads((target / "source-manifest.json").read_bytes())
        body = [o for o in source["objects"] if "raw" in o["roles"]]
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["roles"], ["normalized", "raw"])
        self.assertEqual(source["pin"]["value"], body[0]["sha256"])

    def test_batchcomplete_does_not_end_walk_and_truncation_order_repeat_fail(self):
        cases = [self.records[:1], list(reversed(self.records)), self.records + [self.records[0]]]
        repeated = copy.deepcopy(self.records)
        repeated[1] = self.record({**self.last, "continue": self.cursor}, self.cursor)
        cases.append(repeated + [self.record(self.last, self.cursor)])
        changed = copy.deepcopy(self.records); changed[1]["request"] = core.parameters({"continue": "-||", "gapcontinue": "Other"})
        cases.append(changed)
        for records in cases:
            with self.assertRaises(core.EvidenceError): core.replay(self.plan, records)

    def test_empty_unknown_or_query_overriding_continuation_fails(self):
        for cursor in ({}, None, {"continue": "-||"}, {"continue": "-||", "action": "edit"},
                {"continue": "-||", "gapcontinue": ""}, {"continue": "-||", "gapcontinue": 2}):
            with self.subTest(cursor=cursor), self.assertRaises(core.EvidenceError):
                core.replay(self.plan, [self.record({**self.first, "continue": cursor})])

    def test_error_malformed_status_and_partial_transport_never_end_successfully(self):
        bad = [{"error": {"code": "bad"}}, {"query": {}}, {"query": {"pages": []}},
            {**self.last, "warnings": {"query": "truncated"}}, {**self.last, "query-continue": {}},
            {"query": {"pages": {"1": {"pageid": 1, "ns": 0, "title": "A", "links": [{}]}}}}]
        for data in bad:
            with self.assertRaises(core.EvidenceError): core.replay(self.plan, [self.record(data)])
        for metadata in ({"http_status": 503}, {"curl_exit_code": 18}, {"content_type": "text/html"}, {"bytes": 0}):
            with self.assertRaises(core.EvidenceError): core.replay(self.plan, [self.record(self.last, **metadata)])

    def test_reviewed_floors_and_title_conflicts_fail(self):
        for key in ("minimum_pages", "minimum_links"):
            with self.assertRaises(core.EvidenceError): core.replay({**self.plan, key: 3}, self.records)
        changed = copy.deepcopy(self.last); changed["query"]["pages"]["1"]["title"] = "Renamed"
        with self.assertRaises(core.EvidenceError): core.replay(self.plan, [self.records[0], self.record(changed, self.cursor)])

    def test_different_upstream_ids_cannot_inflate_logical_page_floor(self):
        data = {"query": {"pages": {"1": self.page(1, "Same Title", ["B", "C"]),
            "2": self.page(2, "Same_Title", [])}}}
        with self.assertRaisesRegex(core.EvidenceError, "logical page identity"):
            core.replay(self.plan, [self.record(data)])

    def test_producer_stops_before_next_request_after_graph_bound_violation(self):
        first = copy.deepcopy(self.first)
        first["query"]["pages"]["2"] = self.page(2, "Beta", [])
        record = self.record(first)
        plan = self.root / "plan.json"; plan.write_bytes(core.canonical(self.plan))
        with mock.patch.object(cli, "require_startup"), mock.patch.object(cli, "runtime_identity", return_value=(self.tool, self.programs)), \
                mock.patch.object(cli, "transport", return_value=(base64.b64decode(record["body_base64"]), record["response"])) as transport, \
                mock.patch.object(cli.time, "sleep"), mock.patch.object(cli, "retain_incomplete") as retain, \
                mock.patch.dict(core.POLICY, {"maximum_pages": 1}), self.assertRaises(core.EvidenceError):
            # Keep the plan valid under this deliberately tiny execution bound.
            plan.write_bytes(core.canonical({**self.plan, "minimum_pages": 1}))
            cli.acquire(plan, self.root / "captures", Path(sys.executable).resolve())
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(retain.call_args.args[5], record)

    def test_eof_then_lingering_process_retains_received_body_on_timeout(self):
        script = self.root / "linger"
        script.write_text("#!" + str(Path(sys.executable).resolve()) + "\nimport os,time\n"
            "os.write(1,b'partial-body')\nos.close(1)\nos.close(2)\ntime.sleep(30)\n")
        script.chmod(0o700)
        with self.assertRaises(cli.TransportFailure) as caught:
            cli.transport(core.parameters({}), script)
        self.assertEqual(caught.exception.raw, b"partial-body")
        self.assertEqual(caught.exception.response["bytes"], 12)
        self.assertNotEqual(caught.exception.response["curl_exit_code"], 0)
        self.assertEqual(caught.exception.response["http_status"], 0)

    def test_original_non_nfc_titles_and_percent_encoded_cursor_survive(self):
        title = "Cafe\u0301"
        cursor = {"continue": "-||", "gapcontinue": title}
        first = copy.deepcopy(self.first); first["continue"] = cursor; first["query"]["pages"]["1"]["title"] = title
        last = copy.deepcopy(self.last); last["query"]["pages"]["1"]["title"] = title
        records = [self.record(first), self.record(last, cursor)]
        core.replay(self.plan, records)
        self.assertIn("Cafe%CC%81", records[1]["request"]["query_urlencoded"])
        self.assertIn(title, base64.b64decode(records[0]["body_base64"]).decode())

    def test_tool_program_preimages_and_rehashed_export_tamper_fail(self):
        files = self.capture(); capture = {k: v for k, v in files.items() if k != "manifest.json"}
        capture["implementation/brain/tools/source_plan_contracts.py"] += b"\n"
        with self.assertRaises(core.EvidenceError): core.verify_capture_files(capture)
        for version in ("CPython 3.13.1 -I -S", "CPython 3.12.13", "PyPy 3.12.13 -I -S"):
            tool = copy.deepcopy(self.tool); tool["python"]["version"] = version
            with self.assertRaises(core.EvidenceError): core.validate_tool(tool)
        files = self.export(); files.pop("manifest.json"); files["acquisition/facts.json"] += b" "
        target = core.archive.publish(core.archive.manifest_files(files, core.EXPORT_SCHEMA), self.root / "bad", core.EXPORT_SCHEMA)
        with self.assertRaises(core.EvidenceError): core.verify_export(target)

    def test_acquire_failure_retains_completed_and_failed_requests_without_receipt(self):
        responses = [(base64.b64decode(r["body_base64"]), r["response"]) for r in self.records]
        bad = b"upstream failure"
        responses[1] = (bad, {"curl_exit_code": 22, "http_status": 503, "content_type": "text/html", "bytes": len(bad), "sha256": core.sha(bad)})
        plan = self.root / "plan.json"; plan.write_bytes(core.canonical(self.plan))
        with mock.patch.object(cli, "require_startup"), mock.patch.object(cli, "runtime_identity", return_value=(self.tool, self.programs)), \
                mock.patch.object(cli, "transport", side_effect=responses), mock.patch.object(cli.time, "sleep"), self.assertRaises(core.EvidenceError):
            cli.acquire(plan, self.root / "captures", Path(sys.executable).resolve())
        self.assertFalse((self.root / "captures").exists())
        target = next((self.root / "captures-incomplete").iterdir())
        files, _ = core.archive.read_bundle(target, "wikilean.eom-incomplete-attempt/v1")
        transcript = json.loads(files["transcript.json"])
        self.assertEqual(transcript["completed_requests"], self.records[:1])
        self.assertEqual(base64.b64decode(transcript["failed_request"]["body_base64"]), bad)
        self.assertFalse(json.loads(files["failure.json"])["authority"])
        self.assertFalse(any("receipt" in name for name in files))

    def test_actual_pipe_transport_uses_only_explicit_request_environment_and_controls(self):
        raw = base64.b64decode(self.records[0]["body_base64"])
        script = self.root / "curl"
        invocation = self.root / "invocation.json"
        script.write_text("#!" + str(Path(sys.executable).resolve()) + "\nimport json, os, sys\nfrom pathlib import Path\n" +
            "Path(" + repr(str(invocation)) + ").write_text(json.dumps({'argv':sys.argv,'env':dict(os.environ)}))\n" +
            "sys.stdout.buffer.write(" + repr(raw) + ")\nsys.stderr.buffer.write(" + repr(cli.TRAILER + b"200\tapplication/json; charset=utf-8\n") + ")\n")
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
        self.assertIn(core.API + "?" + self.records[0]["request"]["query_urlencoded"], args)

    def test_actual_v3_compiler_accepts_raw_identity_parent(self):
        import test_compile_offline_pack_v2 as fixture_module
        target = core.archive.publish(self.export(), self.root / "exports", core.EXPORT_SCHEMA)
        fragment = json.loads((target / "source-fragment.json").read_bytes())
        fixture = fixture_module.OfflinePackCompilerTest(); fixture.setUp(); self.addCleanup(fixture.tearDown)
        fixture._upgrade_plan_v3()
        source = fragment["sources"][0]
        raw = next(o for o in source["objects"] if o["name"] == "api_transcript")
        (fixture.external / "eom-transcript.json").write_bytes((target / raw["path"]).read_bytes())
        fixture.inventory["inputs"].append({"id": "eom-transcript", "class": "immutable_source_object", "cardinality": "one", "root": "external",
            "path": "eom-transcript.json", "consumers": ["brain/replay.py"], "purpose": "complete EOM raw evidence fixture", "requirement": "required"})
        fixture.inventory["inputs"].sort(key=lambda x: x["id"])
        fixture.inventory["inventory_id"] = core.contracts.reducer_input_inventory_identity(fixture.inventory)
        fixture.plan["inventory_id"] = fixture.inventory["inventory_id"]
        fixture.plan["sources"] = sorted([*fixture.plan["sources"], source], key=lambda x: x["source"])
        fixture.plan["input_bindings"].append({"input_id": "eom-transcript", "state": "present", "sources": [core.SOURCE],
            "members": [{"path": "eom-transcript.json", "source": core.SOURCE, "object": "api_transcript"}]})
        fixture.plan["input_bindings"].sort(key=lambda x: x["input_id"])
        fixture.inventory_path.write_bytes(core.canonical(fixture.inventory)); fixture.plan_path.write_bytes(core.canonical(fixture.plan))
        packed = fixture_module.compiler.compile_offline_pack_v2(fixture.plan_path.resolve(), fixture.inventory_path.resolve(),
            (fixture.base / "eom-pack").resolve(), roots={"repo": fixture.repo.resolve(), "external": fixture.external.resolve(), fragment["physical_root"]: target}, git_executable="/usr/bin/git")
        manifest, _ = core.contracts.load_canonical_json(packed.manifest_path)
        core.contracts.verify_offline_pack_files(manifest, packed.root, manifest_path=packed.manifest_path)


if __name__ == "__main__":
    unittest.main()
