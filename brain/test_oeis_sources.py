"""OEIS exact-scope acquisition, legacy projection, evidence and compiler checks."""
import base64
import copy
import gzip
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import oeis_source_evidence as core
import oeis_sources as cli
import test_wikidata_crossref_sources as upstream

WHEN = "2026-09-08T20:00:00Z"


class OeisSourcesTest(unittest.TestCase):
    def setUp(self):
        self.upstream = upstream.CrossrefExportTest()
        self.upstream.setUp(); self.addCleanup(self.upstream.doCleanups)
        self.root = self.upstream.root
        (self.upstream.repository / core.io.REGISTRY_PATH).write_bytes(upstream.registry(oeis="P829"))
        self.upstream.git("add", core.io.REGISTRY_PATH)
        self.upstream.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "OEIS registry")
        self.upstream.commit = self.upstream.git("rev-parse", "HEAD").decode().strip()
        first, second = upstream.fixture.entity("Q1"), upstream.fixture.entity("Q2")
        first["claims"]["P829"] = [upstream.statement("A000001", pid="P829"), upstream.statement("A000002", pid="P829")]
        second["claims"]["P829"] = [upstream.statement("A000002", pid="P829")]
        toolchain = upstream.fixture.fake_toolchain()
        self.upstream.bundle = upstream.fixture.acquire.publish_transcript(upstream.fixture.plan_bytes(["Q1", "Q2"]),
            [upstream.fixture.response({"entities": {"Q1": first, "Q2": second}})], store=self.root / "oeis-entity-fixture",
            acquisition_tool=upstream.fixture.fake_tool(toolchain), acquisition_toolchain=toolchain, audit_time=WHEN)
        export = self.upstream.export()
        sources = json.loads((export / "source-fragment.json").read_bytes())["sources"]
        self.roots = {core.io.PHYSICAL_ROOT: export, core.io.GIT_ROOT: self.upstream.repository}
        self.plan = {"schema": core.PLAN_SCHEMA, "parents": sorted(sources, key=lambda s: s["source"]),
            "reviewed_parent_manifest_ids": {s["source"]: core.io.source_plan_contracts._source_manifest_from_plan(s, "fixture")["source_manifest_id"] for s in sources},
            "anchored_qids": {"A000001": "Q1", "A000002": "Q1"}, "minimum_names_inventory": 3, "minimum_links": 1}
        self.programs = {name: (core.ROOT / name).read_bytes() for name in core.TOOL_FILES}
        self.profile = {"files": [{"path": name, "sha256": core.sha(self.programs[name])} for name in core.TOOL_FILES], "policy": copy.deepcopy(core.POLICY)}
        self.profile["profile_id"] = core.profile_id(self.profile)
        registry = self.root / "oeis-profiles.json"
        registry.write_bytes(core.canonical({"schema": core.PROFILE_SCHEMA, "current_profile": self.profile["profile_id"], "profiles": [self.profile]}))
        patch = mock.patch.object(core, "REGISTRY", registry); patch.start(); self.addCleanup(patch.stop)
        self.tool = {"schema": core.TOOL_SCHEMA, "profile_id": self.profile["profile_id"], "files": self.profile["files"],
            "python": {"version": "CPython 3.12.13 -I -S", "sha256": "b" * 64}, "curl": {"version": "curl fixture", "sha256": "c" * 64}}
        self.raw = {"names_gz": gzip.compress("# Header\nA000001 First name.\r\nA000002 Cafe\u0301 name.\nA000003 Unanchored name.\n".encode(), mtime=0),
            "entry-a000001": core.artifact({"number": 1, "name": "Fallback name", "xref": ["Cf. A000001 A000002 A000003 A000002."]}),
            "entry-a000002": core.artifact({"number": 2, "name": "Other fallback", "xref": []})}
        self.responses = {spec[0]: self.response(spec, self.raw[spec[0]]) for spec in core.request_specs(self.plan)}
        self.parents = core.capture_parents(self.plan, self.roots)

    @staticmethod
    def response(spec, raw, **changes):
        return {"curl_exit_code": 0, "http_status": 200, "content_type": spec[2], "sha256": core.sha(raw), "bytes": len(raw), **changes}

    def capture(self, when=WHEN):
        return core.capture_files(self.plan, self.raw, self.responses, self.tool, self.programs, self.parents, when)

    def export(self, when=WHEN):
        return core.build_export({k: v for k, v in self.capture(when).items() if k != "manifest.json"}, self.profile, self.programs, self.roots, when)

    def publish(self):
        return core.archive.publish(self.export(), self.root / "oeis-exports", core.EXPORT_SCHEMA)

    def test_complete_capture_and_export_retains_exact_scope_and_source_evidence(self):
        capture = core.archive.publish(self.capture(), self.root / "oeis-captures", core.CAPTURE_SCHEMA)
        core.verify_capture(capture, self.roots)
        path = self.publish()
        result = core.verify_export(path, self.roots)
        self.assertEqual((result["facts"]["names_inventory"], result["facts"]["pages"], result["facts"]["links"], result["facts"]["requests"]), (3, 2, 1, 3))
        manifest = json.loads((path / "source-manifest.json").read_bytes())
        lineage = json.loads((path / "evidence/lineage.json").read_bytes())
        self.assertEqual(lineage["acquisition_receipt_ids"], [])
        receipt = json.loads((path / "acquisition/receipt.json").read_bytes())
        transcript_bytes = (path / "acquisition/observation.json").read_bytes()
        self.assertEqual(receipt["outputs"], [{"object": "observation", "sha256": core.sha(transcript_bytes), "bytes": len(transcript_bytes), "media_type": "application/json"}])
        transcript = json.loads(transcript_bytes)
        self.assertEqual({r["object"]: base64.b64decode(r["body_base64"]) for r in transcript["responses"]}, self.raw)
        self.assertEqual(set(lineage["parent_source_manifest_ids"]), {self.plan["reviewed_parent_manifest_ids"][n] for n in ("wikidata-crossrefs", core.io.CURATED_SOURCE)} | {json.loads((path / "observation-source-manifest.json").read_bytes())["source_manifest_id"]})
        self.assertEqual(manifest["license"]["redistribution"], "restricted")
        self.assertEqual((path / "acquisition/raw/names_gz").read_bytes(), self.raw["names_gz"])
        self.assertEqual([s[0] for s in core.request_specs(self.plan)], ["names_gz", "entry-a000001", "entry-a000002"])

    def test_normalization_matches_actual_legacy_main_and_pair_cleanup(self):
        import oeis
        names = core.names_inventory(self.raw["names_gz"], 3)
        entries = {aid: core.entry_document(aid, self.raw["entry-" + aid.lower()]) for aid in self.plan["anchored_qids"]}
        expected = core.normalize(self.plan, self.raw, self.programs)
        actual = []
        def emit(db, pages, links, extra_meta):
            actual.append(core.pair.normalize_pair(db, pages, links, extra_meta, self.programs["brain/ingest/common.py"]))
        with mock.patch.object(oeis, "load_names", return_value=names), mock.patch.object(oeis, "entry_json", side_effect=lambda aid, _count: entries[aid]), \
                mock.patch.object(oeis.common, "qid_map", return_value=self.plan["anchored_qids"]), mock.patch.object(oeis.common, "emit", side_effect=emit):
            self.assertEqual(oeis.main(), 0)
        self.assertEqual(actual, [expected])
        meta, pages, links = expected
        self.assertEqual([p["title"] for p in pages], ["First name.", "Cafe\u0301 name."])
        self.assertTrue(all(p["snippet_license"] == "CC-BY-SA-4.0 (OEIS)" for p in pages))
        self.assertEqual([(r["src"], r["dst"]) for r in links], [("A000001", "A000002")])
        self.assertNotIn("fetched_at", meta)

    def test_scope_cannot_be_subset_extra_qid_or_wrong_reviewed_parent(self):
        for changes in ({"anchored_qids": {"A000001": "Q1"}}, {"anchored_qids": {"A000001": "Q1", "A000002": "Q2"}},
                {"anchored_qids": {**self.plan["anchored_qids"], "A000003": "Q1"}},
                {"reviewed_parent_manifest_ids": {**self.plan["reviewed_parent_manifest_ids"], "wikidata-crossrefs": "sha256:" + "f" * 64}}):
            with self.subTest(changes=changes), self.assertRaises(core.io.ExportError):
                core.capture_parents({**self.plan, **changes, "minimum_links": 0}, self.roots)

    def test_requested_qid_scope_and_p829_registry_are_required(self):
        captured = copy.deepcopy(self.parents[3])
        captured[("wikidata-crossrefs", "requested_qid_scope")] = core.canonical({"schema": core.io.SCOPE_SCHEMA, "qids": ["Q2"]})
        with self.assertRaisesRegex(core.io.ExportError, "requested crossref scope"): core.anchor_map(captured)
        captured = copy.deepcopy(self.parents[3])
        captured[(core.io.CURATED_SOURCE, "source_registry")] = upstream.registry(oeis="P999")
        with self.assertRaisesRegex(core.io.ExportError, "P829"): core.anchor_map(captured)

    def test_entry_request_number_shape_and_original_unicode_are_checked(self):
        self.assertEqual(core.entry_document("A000001", core.artifact({"number": 1, "name": "Cafe\u0301", "xref": []}))["name"], "Cafe\u0301")
        for document in ({"number": 2}, {"number": True}, [{"number": 1}], {"number": 1, "name": []}, {"number": 1, "xref": "A000002"}):
            with self.subTest(document=document), self.assertRaises(core.io.ExportError): core.entry_document("A000001", core.artifact(document))

    def test_names_gzip_integrity_bounds_floors_and_legacy_newlines(self):
        self.assertEqual(core.names_inventory(gzip.compress(b"A000001 Old\rA000001 New\nA000002 Next\n", mtime=0), 2), {"A000001": "New", "A000002": "Next"})
        self.assertEqual(core.names_inventory(gzip.compress("A000001 Has\u2028separator\n".encode(), mtime=0), 1)["A000001"], "Has\u2028separator")
        for raw in (self.raw["names_gz"][:-4], b"not gzip"):
            with self.assertRaises((OSError, EOFError)): core.names_inventory(raw, 1)
        with self.assertRaisesRegex(core.io.ExportError, "completeness floor"): core.names_inventory(self.raw["names_gz"], 4)
        with mock.patch.dict(core.POLICY, {"maximum_names_uncompressed_bytes": 16}):
            with self.assertRaisesRegex(core.io.ExportError, "expanded names"): core.names_inventory(self.raw["names_gz"], 1)

    def test_failed_partial_wrong_media_and_missing_extra_responses_fail(self):
        spec = core.request_specs(self.plan)[1]
        for response in ({"curl_exit_code": 28}, {"http_status": 403}, {"content_type": "text/html"}, {"bytes": 0}):
            responses = {**self.responses, spec[0]: {**self.responses[spec[0]], **response}}
            with self.assertRaises(core.io.ExportError): core.capture_files(self.plan, self.raw, responses, self.tool, self.programs, self.parents, WHEN)
        for raw in ({k: v for k, v in self.raw.items() if k != "entry-a000001"}, {**self.raw, "extra": b"x"}):
            with self.assertRaisesRegex(core.io.ExportError, "closure"): core.capture_files(self.plan, raw, self.responses, self.tool, self.programs, self.parents, WHEN)

    def test_rehashed_output_or_request_forgery_fails_independent_replay(self):
        for key, value in (("normalized/oeis_pages.jsonl", b'{"forged":true}\n'), ("acquisition/request-results.json", b"[]"), ("acquisition/observation.json", b"{}")):
            with self.subTest(key=key):
                files = {k: v for k, v in self.export().items() if k != "manifest.json"}; files[key] = value
                target = core.archive.publish(core.archive.manifest_files(files, core.EXPORT_SCHEMA), self.root / ("tampered-" + core.sha(key.encode())[:8]), core.EXPORT_SCHEMA)
                with self.assertRaises(core.io.ExportError): core.verify_export(target, self.roots)

    def test_tool_preimages_and_audit_independence(self):
        for name in core.TOOL_FILES:
            with self.subTest(name=name), self.assertRaisesRegex(core.io.ExportError, "reviewed whole generation"):
                core.capture_files(self.plan, self.raw, self.responses, self.tool, {**self.programs, name: self.programs[name] + b"\n"}, self.parents, WHEN)
        first, second = self.export(), self.export("2026-09-09T00:00:00Z")
        self.assertEqual(json.loads(first["source-manifest.json"])["source_manifest_id"], json.loads(second["source-manifest.json"])["source_manifest_id"])
        self.assertEqual(first["normalized/oeis_links.jsonl"], second["normalized/oeis_links.jsonl"])

    def test_no_ambient_cache_or_network_is_used_by_normalizer(self):
        import oeis
        with mock.patch("socket.socket", side_effect=AssertionError("network")), mock.patch.object(oeis.common, "cache_path", side_effect=AssertionError("cache")), \
                mock.patch.object(oeis.common, "qid_map", side_effect=AssertionError("loose crossrefs")), \
                mock.patch.dict(os.environ, {"OEIS_DELAY": "invalid", "HTTPS_PROXY": "https://unused.invalid"}):
            core.normalize(self.plan, self.raw, self.programs)

    def test_acquisition_fails_after_first_bad_entry_and_retains_private_response(self):
        plan = self.root / "oeis-plan.json"; plan.write_bytes(core.canonical(self.plan))
        specs = core.request_specs(self.plan)
        outputs = [(self.raw["names_gz"], self.responses["names_gz"]), (b"denied", self.response(specs[1], b"denied", http_status=403, curl_exit_code=22, content_type="text/html"))]
        with mock.patch.object(cli, "require_startup"), mock.patch.object(cli, "runtime_identity", return_value=(self.tool, self.programs)), \
                mock.patch.object(cli.time, "sleep"), mock.patch.object(cli, "transport", side_effect=outputs) as transport:
            with self.assertRaises(core.io.ExportError): cli.acquire(plan, self.roots, self.root / "captures", Path("/usr/bin/curl"))
        self.assertEqual(transport.call_count, 2)
        incomplete = next((self.root / "captures-incomplete").iterdir())
        self.assertEqual((incomplete / "raw/entry-a000001").read_bytes(), b"denied")
        self.assertFalse((incomplete / "receipt.json").exists())
        self.assertFalse(json.loads((incomplete / "failure.json").read_bytes())["authority"])

    def test_real_transport_pipe_and_request_controls(self):
        spec = core.request_specs(self.plan)[1]
        command = cli.command(spec, Path("/usr/bin/curl"))
        self.assertEqual(command[:2], ["/usr/bin/curl", "-q"])
        self.assertEqual(command[-1], "https://oeis.org/A000001?fmt=json")
        self.assertNotIn("--location", command); self.assertNotIn("--retry", command)
        self.assertEqual(command[command.index("--proxy") + 1], "")
        script = "import os,sys;assert not any(k in os.environ for k in ['HOME','HTTPS_PROXY','CURL_HOME']);sys.stdout.buffer.write(b'{\"number\":1}');sys.stderr.buffer.write(b'\\nWIKILEAN_OEIS_RESULT\\t200\\tapplication/json; charset=UTF-8\\n')"
        with mock.patch.object(cli, "command", return_value=[sys.executable, "-I", "-S", "-c", script]):
            raw, response = cli.transport(spec, Path("/usr/bin/curl"))
        self.assertEqual(raw, b'{"number":1}'); core.validate_response(spec, raw, response)

    def test_real_transport_retains_partial_body_after_eof_then_linger(self):
        spec = core.request_specs(self.plan)[1]
        script = "import os,time;os.write(1,b'partial');os.close(1);os.close(2);time.sleep(20)"
        with mock.patch.object(cli, "command", return_value=[sys.executable, "-I", "-S", "-c", script]):
            with self.assertRaises(cli.TransportFailure) as caught: cli.transport(spec, Path("/usr/bin/curl"))
        self.assertEqual(caught.exception.raw, b"partial")
        self.assertNotEqual(caught.exception.response["curl_exit_code"], 0)

    def test_actual_v3_compiler_accepts_mixed_acquisition_and_parent_lineage(self):
        import test_compile_offline_pack_v2 as fixture
        target = self.publish()
        fragment = json.loads((target / "source-fragment.json").read_bytes())
        compiler = fixture.OfflinePackCompilerTest(); compiler.setUp(); self.addCleanup(compiler.tearDown)
        compiler._upgrade_plan_v3()
        plan, inventory = compiler.plan, compiler.inventory
        inventory["roots"].append({"id": "oeis_normalized", "kind": "external_tree"}); inventory["roots"].sort(key=lambda r: r["id"])
        for kind in ("pages", "links"):
            inventory["inputs"].append({"id": "external-" + kind, "class": "immutable_source_object", "cardinality": "many",
                "consumers": ["brain/replay.py"], "path_pattern": "*_" + kind + ".jsonl", "purpose": "OEIS source integration", "requirement": "required", "root": "oeis_normalized"})
        inventory["inputs"].sort(key=lambda r: r["id"])
        inventory["inventory_id"] = core.contracts.reducer_input_inventory_identity(inventory)
        plan["inventory_id"] = inventory["inventory_id"]
        plan["sources"].extend(fragment["sources"]); plan["sources"].sort(key=lambda r: r["source"])
        plan["input_bindings"].extend(fragment["input_bindings"]); plan["input_bindings"].sort(key=lambda r: r["input_id"])
        compiler.plan_path.write_bytes(core.canonical(plan)); compiler.inventory_path.write_bytes(core.canonical(inventory))
        packed = fixture.compiler.compile_offline_pack_v2(compiler.plan_path.resolve(), compiler.inventory_path.resolve(), (compiler.base / "oeis-pack").resolve(),
            roots={"repo": compiler.repo.resolve(), "external": compiler.external.resolve(), **self.roots, core.PHYSICAL_ROOT: target, "oeis_normalized": target / "normalized"}, git_executable="/usr/bin/git")
        manifest, _ = core.contracts.load_canonical_json(packed.manifest_path)
        core.contracts.verify_offline_pack_files(manifest, packed.root, manifest_path=packed.manifest_path)


if __name__ == "__main__": unittest.main()
