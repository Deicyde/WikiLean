"""Pure knowl projection, retained query proof, and actual compiler integration."""
import copy
import datetime as dt
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lmfdb_normalization as core
import export_lmfdb_normalization as producer
import test_lmfdb_sources as raw_fixture
import test_wikidata_crossref_sources as cross_fixture

io = core.io
WHEN = cross_fixture.WHEN


class LmfdbNormalizationTest(unittest.TestCase):
    def setUp(self):
        self.xref=cross_fixture.CrossrefExportTest();self.xref.setUp();self.addCleanup(self.xref.doCleanups)
        self.root=self.xref.root
        self.xref.registry=cross_fixture.registry(lmfdb_knowl="P12987")
        (self.xref.repository/io.REGISTRY_PATH).write_bytes(self.xref.registry)
        self.xref.git("add",io.REGISTRY_PATH)
        self.xref.git("-c","user.name=Fixture","-c","user.email=fixture@example.invalid","commit","-qm","LMFDB property")
        self.xref.commit=self.xref.git("rev-parse","HEAD").decode().strip()
        fixture=cross_fixture.fixture
        first,second=fixture.entity("Q1"),fixture.entity("Q2")
        first["claims"]["P12987"]=[cross_fixture.statement(v,pid="P12987") for v in ("alpha","doc.example")]
        second["claims"]["P12987"]=[cross_fixture.statement(v,pid="P12987") for v in ("alpha","beta")]
        toolchain=fixture.fake_toolchain()
        self.xref.bundle=fixture.acquire.publish_transcript(fixture.plan_bytes(["Q1","Q2"]),
            [fixture.response({"entities":{"Q1":first,"Q2":second}})],store=self.root/"lmfdb-entities",
            acquisition_tool=fixture.fake_tool(toolchain),acquisition_toolchain=toolchain,audit_time=WHEN)
        xref_export=self.xref.export()
        self.sources=json.loads((xref_export/"source-fragment.json").read_bytes())["sources"]
        self.raw=raw_fixture.LmfdbSourcesTest();self.raw.setUp();self.addCleanup(self.raw.doCleanups)
        self.raw.response["rows"][0].update(content="{{ DEFINES('Alpha', wikidata='Q99') }} {{ KNOWL('beta', title='Beta label') }} {{ KNOWL_INC('hidden') }} {% if x %}math{% endif %}",links=["beta","missing","alpha"," beta ",None])
        self.raw.response["rows"][1].update(title="  ",content="Beta body",links=["alpha"])
        self.raw.response["rows"].append({"id":"doc.example","title":"Editing","content":"Example {{ DEFINES('not a concept', wikidata='Q999') }}","links":["beta"],"timestamp":None})
        self.raw.response["row_count"]=3
        self.raw_export=raw_fixture.core.archive.publish(self.raw.export(),self.root/"raw-source",raw_fixture.core.EXPORT_SCHEMA)
        fragment=json.loads((self.raw_export/"source-fragment.json").read_bytes());self.sources+=fragment["sources"]
        self.roots={io.PHYSICAL_ROOT:xref_export,io.GIT_ROOT:self.xref.repository,fragment["physical_root"]:self.raw_export}
        self.manifests={s["source"]:io.source_plan_contracts._source_manifest_from_plan(s,"fixture") for s in self.sources}
        self.plan={"schema":core.PLAN_SCHEMA,"parents":sorted(self.sources,key=lambda s:s["source"]),
            "reviewed_parent_manifest_ids":{n:m["source_manifest_id"] for n,m in self.manifests.items()}}
        programs=producer.implementation();self.profile={"files":[{"path":p,"sha256":io.sha(raw)} for p,raw in sorted(programs.items())]}
        self.profile["profile_id"]=core.profile_id(self.profile)
        registry=self.root/"normalizer-profiles.json";registry.write_bytes(io.canonical({"schema":core.PROFILE_SCHEMA,"current_profile":self.profile["profile_id"],"profiles":[self.profile]}))
        patch=mock.patch.object(core,"REGISTRY",registry);patch.start();self.addCleanup(patch.stop)
        self.store=self.root/"lmfdb-exports";self.store.mkdir(mode=0o700)

    def export(self):return producer.export(self.plan,self.roots,self.store,normalized_at=WHEN)

    @staticmethod
    def rows(path):return [json.loads(line) for line in path.read_bytes().splitlines()]

    def test_pair_keeps_templates_links_inline_qids_and_doc_fallback(self):
        path=self.export();producer.verify(path,self.roots)
        pages,links=[self.rows(path/f"normalized/lmfdb_knowl_{kind}.jsonl") for kind in ("pages","links")]
        by_id={p["id"]:p for p in pages[1:]}
        self.assertEqual({key:value["qid"] for key,value in by_id.items()},{"alpha":"Q99","beta":"Q2","doc.example":"Q1"})
        self.assertEqual(by_id["beta"]["title"],"beta")
        self.assertIn("Alpha Beta label",by_id["alpha"]["snippet"])
        self.assertNotIn("hidden",by_id["alpha"]["snippet"])
        self.assertEqual({(row["src"],row["dst"]) for row in links[1:]},{("alpha","beta"),("alpha","missing"),("beta","alpha"),("doc.example","beta")})
        self.assertEqual(pages[0]["_meta"]["n_links_resolved"],3)
        self.assertEqual(pages[0]["_meta"]["n_with_qid"],3)
        core.build_context.validate_external_pair("lmfdb_knowl",pages[0]["_meta"],pages[1:],links[0]["_meta"],links[1:])

    def test_full_content_regex_is_legacy_and_doc_pages_stay_without_inline_join(self):
        data=copy.deepcopy(self.raw.response);data["rows"][1]["content"]='outside a macro wikidata="Q77"'
        rows=[(r["id"],r["title"],r["content"],r["links"],dt.datetime.fromisoformat(r["timestamp"]) if r["timestamp"] else None) for r in data["rows"]]
        pages,_=core.selected_projection(producer.implementation()["brain/ingest/lmfdb.py"],rows,{})
        self.assertEqual(next(r for r in pages if r["id"]=="beta")["qid"],"Q77")
        self.assertNotIn("qid",next(r for r in pages if r["id"]=="doc.example"))

    def test_all_rows_match_exact_legacy_main_without_network(self):
        import lmfdb
        native=[(r["id"],r["title"],r["content"],r["links"],dt.datetime.fromisoformat(r["timestamp"]) if r["timestamp"] else None) for r in self.raw.response["rows"]]
        connection=mock.Mock();connection.run.side_effect=[[(name,) for name in ("id","title","content","links","status","timestamp","type")],native]
        qmap=core.lmfdb_qids(core.capture_parents(self.plan,self.roots)[3]);legacy={}
        def emit(db,pages,links,extra_meta):
            _meta,legacy["pages"],legacy["links"]=core.pairs.normalize_pair(db,pages,links,extra_meta,io.read(core.ROOT/"brain/ingest/common.py"))
        with mock.patch.object(lmfdb.pg8000.native,"Connection",return_value=connection),mock.patch.object(core.common,"qid_map",return_value=qmap),mock.patch.object(core.common,"emit",side_effect=emit),mock.patch("socket.socket",side_effect=AssertionError("network")):
            lmfdb.main()
        path=self.export()
        for kind in ("pages","links"):self.assertEqual(self.rows(path/f"normalized/lmfdb_knowl_{kind}.jsonl")[1:],legacy[kind])

    def test_query_certificate_snapshot_schema_and_row_count_replayed(self):
        program=producer.implementation();raw=self.raw.body()
        for changed in ({"read_only":"off"},{"isolation":"read committed"},{"ambiguous_latest_ids":1},{"rows":[]},{"columns":[]}):
            body=self.raw.body({**self.raw.response,**changed})
            with self.assertRaises(core.observation.EvidenceError):
                core.project_response(body,self.raw.plan,self.raw.transport(body),self.raw.certificate,{},program)
        with self.assertRaises(core.observation.EvidenceError):
            core.project_response(raw,self.raw.plan,self.raw.transport(raw),b"changed DER",{},program)

    def test_original_query_plan_support_must_match_parent_lineage(self):
        sources,manifests,objects,captured,lineages=core.capture_parents(self.plan,self.roots)
        captured[("lmfdb-knowl-observation","normalization_plan")]=io.canonical({**self.raw.plan,"minimum_rows":1})
        with self.assertRaisesRegex(io.ExportError,"not bound"):
            core.reduce_response(manifests,objects,captured,lineages,producer.implementation())

    def test_exact_reviewed_parent_scope_and_property_required(self):
        captured=core.capture_parents(self.plan,self.roots)[3]
        captured[("wikidata-crossrefs","requested_qid_scope")]=io.canonical({"schema":io.SCOPE_SCHEMA,"qids":[]})
        with self.assertRaisesRegex(io.ExportError,"scope"):core.lmfdb_qids(captured)
        captured=core.capture_parents(self.plan,self.roots)[3];captured[(io.CURATED_SOURCE,"source_registry")]=cross_fixture.registry(lmfdb_knowl="P1")
        with self.assertRaisesRegex(io.ExportError,"P12987"):core.lmfdb_qids(captured)
        self.plan["reviewed_parent_manifest_ids"]["lmfdb-knowl-observation"]="sha256:"+"f"*64
        with self.assertRaisesRegex(io.ExportError,"explicitly reviewed"):self.export()

    def test_no_cache_network_ambient_crossrefs_or_legacy_publish(self):
        with mock.patch.object(core.common,"cache_path",side_effect=AssertionError("cache")),mock.patch.object(core.common,"qid_map",side_effect=AssertionError("ambient")),mock.patch.object(core.common,"external_pair_lock",side_effect=AssertionError("publish")),mock.patch("socket.socket",side_effect=AssertionError("network")):
            path=self.export();producer.verify(path,self.roots)

    def test_exact_program_profile_and_query_proof_support(self):
        path=self.export();lineage=json.loads((path/"evidence/external-lmfdb-knowl.json").read_bytes())
        self.assertEqual(lineage["tool"]["sha256"],io.sha(io.canonical(self.profile)))
        self.assertEqual({i["object"] for i in lineage["inputs"]},{"knowl_query_response","wikidata_crossrefs","requested_qid_scope","source_registry"})
        self.assertEqual((path/"normalization/upstream_query_plan.json").read_bytes(),io.canonical(self.raw.plan))
        self.assertEqual((path/"normalization/upstream_peer_certificate.der").read_bytes(),self.raw.certificate)
        programs=producer.implementation();programs["brain/lmfdb_source_evidence.py"]+=b"\n# changed"
        with self.assertRaisesRegex(io.ExportError,"preimages"):
            core.build_documents(self.plan,*core.capture_parents(self.plan,self.roots),self.profile,programs,WHEN)

    def test_parser_selector_and_main_boundary_fail_closed(self):
        program=producer.implementation()["brain/ingest/lmfdb.py"]
        for before,after in ((b"def template_args(",b"def changed("),(b"qids = common.qid_map",b"other = common.qid_map"),(b"common.emit(",b"common.other(")):
            with self.assertRaisesRegex(io.ExportError,"selector"):
                core.selected_projection(program.replace(before,after),[],{})

    def test_uncertain_rename_preserves_unrelated_staging_inode(self):
        original=io.stage_io.publish_directory_no_replace;replacements=[]
        def changed(owned,target):
            original(owned,target);owned.path.mkdir(mode=0o700);(owned.path/"unrelated").write_bytes(b"keep");replacements.append(owned.path);raise RuntimeError("uncertain rename")
        with mock.patch.object(io.stage_io,"publish_directory_no_replace",side_effect=changed),self.assertRaisesRegex(RuntimeError,"uncertain rename"):self.export()
        self.assertEqual(list(self.store.iterdir()),replacements)
        self.assertEqual((replacements[0]/"unrelated").read_bytes(),b"keep")

    def test_repeated_export_and_rehashed_output_tamper(self):
        path=self.export();self.assertEqual(self.export(),path)
        (path/"normalized/lmfdb_knowl_pages.jsonl").write_bytes(b"{}\n")
        files=io.capture_tree(path);doc=io.parse(files.pop("export.json"),"export")
        doc["files"]={n:{"sha256":io.sha(raw),"bytes":len(raw)} for n,raw in sorted(files.items())};doc["export_id"]=core.contracts.domain_hash(core.EXPORT_SCHEMA,{k:v for k,v in doc.items() if k!="export_id"})
        (path/"export.json").write_bytes(io.canonical(doc))
        with self.assertRaisesRegex(io.ExportError,"independent reduction"):producer.verify(path,self.roots)

    def test_real_compiler_accepts_complete_pair_and_parent_closure(self):
        import test_compile_offline_pack_v2 as fixture_module
        target=self.export();fragment=json.loads((target/"source-fragment.json").read_bytes())
        fixture=fixture_module.OfflinePackCompilerTest();fixture.setUp();self.addCleanup(fixture.tearDown);fixture._upgrade_plan_v3()
        fixture.inventory["inputs"]=[i for i in fixture.inventory["inputs"] if i["id"]!="optional_external"]
        fixture.plan["input_bindings"]=[i for i in fixture.plan["input_bindings"] if i["input_id"]!="optional_external"]
        for kind in ("pages","links"):
            name="lmfdb_knowl_"+kind+".jsonl";(fixture.external/name).write_bytes((target/"normalized"/name).read_bytes())
            fixture.inventory["inputs"].append({"id":"lmfdb-"+kind,"class":"immutable_source_object","cardinality":"one","root":"external","path":name,"consumers":["brain/replay.py"],"purpose":"LMFDB complete pair fixture","requirement":"required"})
            fixture.plan["input_bindings"].append({"input_id":"lmfdb-"+kind,"state":"present","sources":list(core.CHILDREN),"members":[{"path":name,"source":core.CHILDREN[0],"object":"lmfdb_knowl_"+kind}]})
        fixture.inventory["inputs"].sort(key=lambda x:x["id"]);fixture.inventory["inventory_id"]=core.contracts.reducer_input_inventory_identity(fixture.inventory);fixture.plan["inventory_id"]=fixture.inventory["inventory_id"]
        fixture.plan["sources"]=sorted([*fixture.plan["sources"],*fragment["sources"]],key=lambda s:s["source"]);fixture.plan["input_bindings"].sort(key=lambda x:x["input_id"])
        fixture.inventory_path.write_bytes(io.canonical(fixture.inventory));fixture.plan_path.write_bytes(io.canonical(fixture.plan))
        packed=fixture_module.compiler.compile_offline_pack_v2(fixture.plan_path.resolve(),fixture.inventory_path.resolve(),(fixture.base/"lmfdb-pack").resolve(),roots={"repo":fixture.repo.resolve(),"external":fixture.external.resolve(),**self.roots,core.PHYSICAL_ROOT:target},git_executable="/usr/bin/git")
        manifest,_=core.contracts.load_canonical_json(packed.manifest_path);core.contracts.verify_offline_pack_files(manifest,packed.root,manifest_path=packed.manifest_path)


if __name__=="__main__":unittest.main()
