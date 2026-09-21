"""Closed EOM pagination-to-pair normalization and legacy projection parity."""
import base64
import copy
import json
import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eom_normalization as core
import export_eom_normalization as producer
import test_eom_sources as raw_fixture
import test_wikidata_crossref_sources as cross_fixture

io = core.io
WHEN = cross_fixture.WHEN


class EomNormalizationTest(unittest.TestCase):
    def setUp(self):
        self.xref = cross_fixture.CrossrefExportTest(); self.xref.setUp(); self.addCleanup(self.xref.doCleanups)
        self.root = self.xref.root
        self.xref.registry = cross_fixture.registry(eom="P7554")
        (self.xref.repository / io.REGISTRY_PATH).write_bytes(self.xref.registry)
        self.xref.git("add", io.REGISTRY_PATH)
        self.xref.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "EOM property")
        self.xref.commit = self.xref.git("rev-parse", "HEAD").decode().strip()
        capture = cross_fixture.fixture
        first, second = capture.entity("Q1"), capture.entity("Q2")
        first["claims"]["P7554"] = [cross_fixture.statement("Alpha_(group)", pid="P7554")]
        second["claims"]["P7554"] = [cross_fixture.statement("Beta", pid="P7554"), cross_fixture.statement("Alpha_(group)", pid="P7554")]
        toolchain = capture.fake_toolchain()
        self.xref.bundle = capture.acquire.publish_transcript(capture.plan_bytes(["Q1", "Q2"]),
            [capture.response({"entities": {"Q1": first, "Q2": second}})], store=self.root / "eom-entities",
            acquisition_tool=capture.fake_tool(toolchain), acquisition_toolchain=toolchain, audit_time=WHEN)
        xref_export = self.xref.export()
        self.sources = json.loads((xref_export / "source-fragment.json").read_bytes())["sources"]
        self.raw = raw_fixture.EomSourcesTest(); self.raw.setUp(); self.addCleanup(self.raw.doCleanups)
        self.raw.first["query"]["pages"]["1"] = self.raw.page(1, "Alpha (group)", ["Beta", "Missing page", "Alpha (group)"])
        self.raw.last["query"]["pages"]["1"] = self.raw.page(1, "Alpha (group)", ["Beta", "Gamma/É"])
        self.raw.records = [self.raw.record(self.raw.first), self.raw.record(self.raw.last, self.raw.cursor)]
        self.raw_export = raw_fixture.core.archive.publish(self.raw.export(), self.root / "raw-source", raw_fixture.core.EXPORT_SCHEMA)
        fragment = json.loads((self.raw_export / "source-fragment.json").read_bytes())
        self.sources += fragment["sources"]
        self.roots = {io.PHYSICAL_ROOT: xref_export, io.GIT_ROOT: self.xref.repository, fragment["physical_root"]: self.raw_export}
        self.manifests = {s["source"]: io.source_plan_contracts._source_manifest_from_plan(s, "fixture") for s in self.sources}
        self.plan = {"schema": core.PLAN_SCHEMA, "parents": sorted(self.sources, key=lambda s:s["source"]),
            "reviewed_parent_manifest_ids": {name:m["source_manifest_id"] for name,m in self.manifests.items()}}
        programs = producer.implementation()
        self.profile = {"files": [{"path": p, "sha256": io.sha(raw)} for p,raw in sorted(programs.items())]}
        self.profile["profile_id"] = core.profile_id(self.profile)
        registry = self.root / "normalizer-profiles.json"
        registry.write_bytes(io.canonical({"schema":core.PROFILE_SCHEMA,"current_profile":self.profile["profile_id"],"profiles":[self.profile]}))
        patch=mock.patch.object(core,"REGISTRY",registry);patch.start();self.addCleanup(patch.stop)
        self.store=self.root/"eom-exports";self.store.mkdir(mode=0o700)

    def export(self):
        return producer.export(self.plan,self.roots,self.store,normalized_at=WHEN)

    @staticmethod
    def rows(path):
        return [json.loads(line) for line in path.read_bytes().splitlines()]

    def test_complete_pair_preserves_title_links_qid_join_and_no_snippets(self):
        path=self.export();producer.verify(path,self.roots)
        pages,links=[self.rows(path/f"normalized/eom_{kind}.jsonl") for kind in ("pages","links")]
        self.assertEqual([p["id"] for p in pages[1:]],["Alpha_(group)","Beta"])
        self.assertEqual(pages[1]["qid"],"Q1")
        self.assertEqual(pages[1]["url"],"https://encyclopediaofmath.org/wiki/Alpha_(group)")
        self.assertEqual({e["dst"] for e in links[1:]},{"Beta","Gamma/É","Missing_page"})
        self.assertTrue(all("snippet" not in p and "snippet_license" not in p for p in pages[1:]))
        core.build_context.validate_external_pair("eom",pages[0]["_meta"],pages[1:],links[0]["_meta"],links[1:])
        self.assertEqual(pages[0]["_meta"]["n_links_resolved"],1)
        self.assertEqual(pages[0]["_meta"]["n_with_qid"],2)
        self.assertTrue(pages[0]["_meta"]["source_pin"].startswith("sha256:"))
        facts=json.loads((path/"normalized/eom_derivation.json").read_bytes())
        self.assertEqual(facts["observation"],"independent-live-requests/no-snapshot")
        self.assertEqual(facts["facts"]["requests"],2)

    def test_row_projection_matches_exact_legacy_main(self):
        import eom
        responses=iter(self.raw.records)
        def fetch(url):
            record=next(responses)
            self.assertEqual(dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)),dict(urllib.parse.parse_qsl(record["request"]["query_urlencoded"])))
            return base64.b64decode(record["body_base64"])
        qmap=core.eom_qids(core.capture_parents(self.plan,self.roots)[3])
        legacy={}
        def emit(db,pages,links,extra_meta):
            _meta,legacy["pages"],legacy["links"]=core.pairs.normalize_pair(db,pages,links,extra_meta,io.read(core.ROOT/"brain/ingest/common.py"))
        with mock.patch.object(core.common,"curl_fetch",side_effect=fetch),mock.patch.object(core.common,"cache_path",return_value=mock.Mock()),\
             mock.patch.object(core.common,"qid_map",return_value=qmap),mock.patch.object(core.common,"emit",side_effect=emit),\
             mock.patch.object(eom.time,"sleep"),mock.patch("socket.socket",side_effect=AssertionError("network")):
            eom.main()
        self.assertEqual(list(responses),[])
        path=self.export()
        for kind in ("pages","links"):
            self.assertEqual(self.rows(path/f"normalized/eom_{kind}.jsonl")[1:],legacy[kind])

    def test_truncated_reordered_and_duplicated_walks_fail(self):
        program=io.read(core.ROOT/"brain/ingest/eom.py")
        for records in (self.raw.records[:1],list(reversed(self.raw.records)),self.raw.records+self.raw.records[-1:]):
            raw=io.canonical({"schema":core.observation.TRANSCRIPT_SCHEMA,"records":records})
            with self.subTest(records=len(records)),self.assertRaises(core.observation.EvidenceError):
                core.project_transcript(raw,self.raw.plan,{},program)

    def test_walk_request_mutation_or_bad_response_fails(self):
        for key in ("request","body_base64"):
            records=copy.deepcopy(self.raw.records)
            if key=="request":records[0][key]["query_urlencoded"]+="&prop=extracts"
            else:records[0][key]=base64.b64encode(b"{}").decode()
            with self.subTest(key=key),self.assertRaises(core.observation.EvidenceError):
                core.project_transcript(io.canonical({"schema":core.observation.TRANSCRIPT_SCHEMA,"records":records}),self.raw.plan,{},io.read(core.ROOT/"brain/ingest/eom.py"))

    def test_original_plan_support_must_match_parent_lineage(self):
        sources,manifests,objects,captured,lineages=core.capture_parents(self.plan,self.roots)
        captured[("eom-api-walk","normalization_plan")]=io.canonical({**self.raw.plan,"minimum_links":1})
        with self.assertRaisesRegex(io.ExportError,"not bound"):
            core.reduce_transcript(manifests,objects,captured,lineages,producer.implementation())

    def test_source_and_scoped_property_ids_are_required(self):
        captured=core.capture_parents(self.plan,self.roots)[3]
        captured[("wikidata-crossrefs","requested_qid_scope")]=io.canonical({"schema":io.SCOPE_SCHEMA,"qids":[]})
        with self.assertRaisesRegex(io.ExportError,"scope"):core.eom_qids(captured)
        captured=core.capture_parents(self.plan,self.roots)[3]
        captured[(io.CURATED_SOURCE,"source_registry")]=cross_fixture.registry(eom="P1")
        with self.assertRaisesRegex(io.ExportError,"P7554"):core.eom_qids(captured)
        self.plan["reviewed_parent_manifest_ids"]["eom-api-walk"]="sha256:"+"f"*64
        with self.assertRaisesRegex(io.ExportError,"explicitly reviewed"):self.export()

    def test_no_cache_network_mtime_or_legacy_publication(self):
        with mock.patch.object(core.common,"cache_path",side_effect=AssertionError("cache")),\
             mock.patch.object(core.common,"qid_map",side_effect=AssertionError("loose crossrefs")),\
             mock.patch.object(core.common,"external_pair_lock",side_effect=AssertionError("publish")),\
             mock.patch("socket.socket",side_effect=AssertionError("network")):
            path=self.export();producer.verify(path,self.roots)

    def test_exact_program_profile_preimages_and_support_bound(self):
        path=self.export();lineage=json.loads((path/"evidence/external-eom.json").read_bytes())
        self.assertEqual(lineage["tool"]["sha256"],io.sha(io.canonical(self.profile)))
        self.assertEqual({i["object"] for i in lineage["inputs"]},{"api_transcript","wikidata_crossrefs","requested_qid_scope","source_registry"})
        self.assertEqual((path/"normalization/upstream_walk_plan.json").read_bytes(),io.canonical(self.raw.plan))
        programs=producer.implementation();programs["brain/eom_source_evidence.py"]+=b"\n# changed"
        with self.assertRaisesRegex(io.ExportError,"preimages"):
            core.build_documents(self.plan,*core.capture_parents(self.plan,self.roots),self.profile,programs,WHEN)

    def test_parser_selector_and_noncanonical_transcript_fail(self):
        program=io.read(core.ROOT/"brain/ingest/eom.py")
        with self.assertRaisesRegex(io.ExportError,"selector differs"):core.selected_parser(program.replace(b"def page_id(",b"def other_id("))
        raw=json.dumps({"schema":core.observation.TRANSCRIPT_SCHEMA,"records":self.raw.records}).encode()
        with self.assertRaisesRegex(io.ExportError,"canonical EOM"):
            core.project_transcript(raw,self.raw.plan,{},program)

    def test_uncertain_rename_cleanup_preserves_unrelated_stage(self):
        original=io.stage_io.publish_directory_no_replace;replacements=[]
        def renamed_failed(owned,target):
            original(owned,target);owned.path.mkdir(mode=0o700);(owned.path/"unrelated").write_bytes(b"preserve")
            replacements.append(owned.path);raise RuntimeError("uncertain rename")
        with mock.patch.object(io.stage_io,"publish_directory_no_replace",side_effect=renamed_failed),self.assertRaisesRegex(RuntimeError,"uncertain rename"):
            self.export()
        self.assertEqual(list(self.store.iterdir()),replacements)
        self.assertEqual((replacements[0]/"unrelated").read_bytes(),b"preserve")

    def test_repeated_export_and_rehashed_output_tamper(self):
        path=self.export();self.assertEqual(self.export(),path)
        (path/"normalized/eom_pages.jsonl").write_bytes(b"{}\n")
        files=io.capture_tree(path);doc=io.parse(files.pop("export.json"),"export")
        doc["files"]={name:{"sha256":io.sha(raw),"bytes":len(raw)} for name,raw in sorted(files.items())}
        doc["export_id"]=core.contracts.domain_hash(core.EXPORT_SCHEMA,{k:v for k,v in doc.items() if k!="export_id"})
        (path/"export.json").write_bytes(io.canonical(doc))
        with self.assertRaisesRegex(io.ExportError,"independent reduction"):producer.verify(path,self.roots)

    def test_actual_v3_compiler_accepts_both_pairs_and_full_evidence(self):
        import test_compile_offline_pack_v2 as compiler_fixture
        path=self.export();fragment=json.loads((path/"source-fragment.json").read_bytes())
        fixture=compiler_fixture.OfflinePackCompilerTest();fixture.setUp();self.addCleanup(fixture.tearDown);fixture._upgrade_plan_v3()
        inventory,plan=fixture.inventory,fixture.plan
        inventory["inputs"]=[i for i in inventory["inputs"] if i["id"]!="optional_external"]
        plan["input_bindings"]=[i for i in plan["input_bindings"] if i["input_id"]!="optional_external"]
        for binding in fragment["input_bindings"]:
            member=binding["members"][0]
            inventory["inputs"].append({"id":binding["input_id"],"class":"immutable_source_object","root":"external","path":member["path"],"cardinality":"one","requirement":"required","consumers":["brain/replay.py"],"purpose":"EOM fixture"})
            (fixture.external/member["path"]).write_bytes((path/("normalized/"+member["object"]+".jsonl")).read_bytes())
        inventory["inputs"].sort(key=lambda i:i["id"]);inventory["inventory_id"]=core.contracts.reducer_input_inventory_identity(inventory)
        fixture.inventory_path.write_bytes(io.canonical(inventory));plan["inventory_id"]=inventory["inventory_id"]
        plan["sources"]=sorted([*plan["sources"],*fragment["sources"]],key=lambda s:s["source"])
        plan["input_bindings"]=sorted([*plan["input_bindings"],*fragment["input_bindings"]],key=lambda b:b["input_id"])
        fixture.plan_path.write_bytes(io.canonical(plan))
        packed=compiler_fixture.compiler.compile_offline_pack_v2(fixture.plan_path.resolve(),fixture.inventory_path.resolve(),(fixture.base/"eom-pack").resolve(),
            roots={"repo":fixture.repo.resolve(),"external":fixture.external.resolve(),**self.roots,core.PHYSICAL_ROOT:path},git_executable="/usr/bin/git")
        manifest,_=core.contracts.load_canonical_json(packed.manifest_path)
        core.contracts.verify_offline_pack_files(manifest,packed.root,manifest_path=packed.manifest_path)


if __name__=="__main__":unittest.main()
