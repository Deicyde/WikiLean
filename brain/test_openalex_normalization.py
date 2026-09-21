"""Pure citation rows over an exact reviewed theoremgraph selector and transcript."""
import copy
import json
import os
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parent))
import openalex_normalization as core
import export_openalex_normalization as producer
import test_openalex_sources as fixture

io=core.io
WHEN=fixture.WHEN


class OpenalexNormalizationTest(unittest.TestCase):
    def setUp(self):
        self.raw=fixture.OpenalexSourcesTest();self.raw.setUp();self.addCleanup(self.raw.doCleanups);self.root=self.raw.root
        self.parent_root=self.root/'parents';self.parent_root.mkdir(mode=0o700)
        self.sources=[];self.manifests={};self.counter=0
        seed=self.source('fixture-theoremgraph-origin','seed',self.raw.selector)
        scope=self.source(core.observation.SCOPE_SOURCE,core.observation.SCOPE_OBJECT,self.raw.selector,parent=seed)
        self.raw.plan['scope']['source_manifest_id']=scope['source_manifest_id']
        self.raw_export=core.observation.archive.publish(self.raw.build(),self.root/'raw-export',core.observation.EXPORT_SCHEMA)
        fragment=json.loads((self.raw_export/'source-fragment.json').read_bytes());self.sources+=fragment['sources']
        self.roots={'fixture_parents':self.parent_root,fragment['physical_root']:self.raw_export}
        self.plan={'schema':core.PLAN_SCHEMA,'parents':sorted(self.sources,key=lambda s:s['source']),
            'reviewed_parent_manifest_ids':{s['source']:io.source_plan_contracts._source_manifest_from_plan(s,'parent')['source_manifest_id'] for s in self.sources}}
        self.programs=producer.implementation();self.profile={'files':[{'path':n,'sha256':io.sha(raw)} for n,raw in sorted(self.programs.items())]};self.profile['profile_id']=core.profile_id(self.profile)
        registry=self.root/'normalizer-profiles.json';registry.write_bytes(io.canonical({'schema':core.PROFILE_SCHEMA,'current_profile':self.profile['profile_id'],'profiles':[self.profile]}))
        patch=mock.patch.object(core,'REGISTRY',registry);patch.start();self.addCleanup(patch.stop)
        self.store=self.root/'normalized';self.store.mkdir(mode=0o700)

    def source(self,name,objname,raw,parent=None):
        root=self.parent_root
        def ref(path,data,**kw):
            full=root/path;full.parent.mkdir(parents=True,exist_ok=True,mode=0o700);full.write_bytes(data)
            return {'root':'fixture_parents','path':path,'sha256':io.sha(data),'bytes':len(data),'media_type':'application/json',**kw}
        output={**ref(name+'/output.json',raw),'name':objname,'roles':['normalized'],'redistribution':'restricted'}
        tool={'name':'fixture-normalizer','version':'1','sha256':'b'*64}
        receipt=None
        if parent is None:
            request={'kind':'http_get','uri':'https://example.invalid/selector','parameters_sha256':'c'*64}
            params=b'{}';request['parameters_sha256']=io.sha(params)
            receipt={'schema':core.contracts.ACQUISITION_RECEIPT_SCHEMA_V1,'source':name,'pin':{'type':'content_sha256','value':io.sha(raw)},'upstream_uri':request['uri'],'tool':tool,'requests':[request],
                'batch':{'status':'complete','requests_total':1,'requests_succeeded':1,'requests_failed':0,'request_set_root':core.contracts.acquisition_request_set_root([request])},'outputs':[io.object_ref(output)],'audit':{'acquired_at':WHEN}}
            receipt['acquisition_receipt_id']=core.contracts.acquisition_receipt_identity(receipt)
            origin={'kind':'acquisition_receipt','id':receipt['acquisition_receipt_id']};inputobj={**output,'roles':['normalized','raw']};objects=[inputobj];inputs=[objname]
        else:
            prior=next(o for o in parent['objects'] if 'normalized' in o['roles'])
            inputobj={**ref(name+'/input.json',raw),'name':prior['name'],'roles':['raw'],'redistribution':'restricted'}
            origin={'kind':'source_manifest','id':parent['source_manifest_id']};objects=[inputobj,output];inputs=[prior['name']]
        lineage={'schema':core.contracts.NORMALIZATION_LINEAGE_SCHEMA_V1,'source':name,'mode':'identity' if parent is None else 'transform',
            'normalization_schema':'wikilean.fixture-selector/v1','configuration_sha256':'a'*64,'tool':tool,'acquisition_receipt_ids':[origin['id']] if parent is None else [],
            'parent_source_manifest_ids':[origin['id']] if parent is not None else [],'inputs':[{**io.object_ref(inputobj),'origin':origin}],
            'outputs':[io.object_ref(output)],'result':'complete','audit':{'normalized_at':WHEN}}
        lineage['normalization_lineage_id']=core.contracts.normalization_lineage_identity(lineage)
        evidence={'acquisition_receipts':[],'request_parameter_preimages':[],
            'normalization_lineage':ref(name+'/lineage.json',io.canonical(lineage),normalization_lineage_id=lineage['normalization_lineage_id'])}
        if receipt:
            evidence['acquisition_receipts']=[ref(name+'/receipt.json',io.canonical(receipt),acquisition_receipt_id=receipt['acquisition_receipt_id'])]
            evidence['request_parameter_preimages']=[ref(name+'/parameters.json',params,parameters_sha256=request['parameters_sha256'])]
        source={'source':name,'source_kind':'acquired_dataset' if receipt else 'sealed_snapshot','pin':receipt['pin'] if receipt else {'type':'dataset_revision','value':lineage['normalization_lineage_id']},
            'objects':sorted(objects,key=lambda o:o['name']),'license':{'expression':'CC0-1.0','redistribution':'restricted','notice':'fixture'},'acquisition':tool,
            'normalization':{'schema':'wikilean.fixture-selector/v1','tool':tool,'inputs':inputs,'outputs':[objname]},'evidence':evidence}
        manifest=io.source_plan_contracts._source_manifest_from_plan(source,'fixture')
        core.contracts.validate_source_manifest_evidence_documents(manifest,receipts={receipt['acquisition_receipt_id']:receipt} if receipt else {},lineage=lineage,
            request_parameter_preimages={r['parameters_sha256']:{k:r[k] for k in ('parameters_sha256','bytes','media_type')} for r in evidence['request_parameter_preimages']},parent_source_manifests={parent['source_manifest_id']:parent} if parent else {})
        self.sources.append(source);self.manifests[name]=manifest;return manifest

    def export(self):return producer.export(self.plan,self.roots,self.store,normalized_at=WHEN)
    def rows(self,path):return [json.loads(line) for line in path.read_bytes().splitlines()]

    def test_exact_citation_rows_and_normalized_metadata(self):
        path=self.export();producer.verify(path,self.roots)
        rows=self.rows(path/'normalized/arxiv_citations.jsonl')
        self.assertEqual(rows[1:],self.raw.rows)
        self.assertEqual(rows[0]['_meta']['n_arxiv_ids'],3);self.assertEqual(rows[0]['_meta']['n_skipped_non_arxiv'],1)
        self.assertNotIn('timestamp',rows[0]['_meta'])
        derivation=json.loads((path/'normalized/arxiv_citations_derivation.json').read_bytes())
        self.assertEqual(derivation['excluded_non_arxiv_ids'],['teorth/pfr'])
        fragment=json.loads((path/'source-fragment.json').read_bytes());self.assertEqual(fragment['input_bindings'][0]['input_id'],'external-arxiv-citations')

    def test_retain_entire_parent_ancestry_and_fail_on_missing_or_unrelated_parent(self):
        plan=copy.deepcopy(self.plan);plan['parents']=[s for s in plan['parents'] if s['source']!='fixture-theoremgraph-origin'];plan['reviewed_parent_manifest_ids'].pop('fixture-theoremgraph-origin')
        with self.assertRaisesRegex(io.ExportError,'ancestor'):core.capture_parents(plan,self.roots)
        unrelated=self.source('unrelated','unrelated_selector',b'{}');plan=copy.deepcopy(self.plan);plan['parents'].append(self.sources[-1]);plan['parents'].sort(key=lambda s:s['source']);plan['reviewed_parent_manifest_ids']['unrelated']=unrelated['source_manifest_id']
        with self.assertRaisesRegex(io.ExportError,'unrelated'):core.capture_parents(plan,self.roots)

    def test_original_scope_identity_bytes_and_bound_plan_are_all_required(self):
        for kind in ('id','selector','plan'):
            data=core.capture_parents(self.plan,self.roots);sources,manifests,objects,captured,lineages=data
            if kind=='id':manifests[core.observation.SCOPE_SOURCE]['source_manifest_id']='sha256:'+'f'*64
            elif kind=='selector':captured[(core.observation.SOURCE,'theoremgraph_selector')]+=b' '
            else:captured[(core.observation.SOURCE,'normalization_plan')]=io.canonical({**self.raw.plan,'minimum_links':0})
            with self.assertRaises(io.ExportError):core.reduce_transcript(manifests,objects,captured,lineages,self.programs)

    def test_scope_classification_and_transcript_replayed_not_trusted_as_rows(self):
        data=core.capture_parents(self.plan,self.roots);sources,manifests,objects,captured,lineages=data
        key=(core.observation.SOURCE,'citation_transcript');transcript=json.loads(captured[key]);transcript['records'].pop()
        captured[key]=io.canonical(transcript);manifests[core.observation.SOURCE]['pin']['value']=io.sha(captured[key])
        with self.assertRaises(core.observation.EvidenceError):core.reduce_transcript(manifests,objects,captured,lineages,self.programs)

    def test_all_rows_match_cache_free_legacy_main(self):
        import openalex_citations as legacy
        pending=list(self.raw.records[4:]);storage={};legacy_output={}
        # Legacy curl automatically follows the one redirect. Feed only its
        # canonical terminal response; complete new evidence keeps both hops.
        pending=[r for r in pending if r['response']['http_status']!=301]
        def fetch(url):
            row=pending.pop(0)
            if row['response']['http_status']==404:raise RuntimeError('curl: (22) 404')
            return core.observation.response_bytes(row)
        def cp(*parts):return '/'.join(parts)
        def read(p):return storage.get(p)
        def write(p,rec):storage[p]=rec
        def emit(_path,meta,rows):legacy_output.update(meta=meta,rows=rows)
        with mock.patch.object(legacy,'collect_arxiv_ids',return_value=(fixture.AIDS,1)),mock.patch.object(legacy.common,'cache_path',side_effect=cp),mock.patch.object(legacy,'cache_read',side_effect=read),mock.patch.object(legacy,'cache_write',side_effect=write),mock.patch.object(legacy.common,'curl_fetch',side_effect=fetch),mock.patch.object(legacy.common,'write_jsonl',side_effect=emit),mock.patch.object(legacy.common,'_volume_guard'),mock.patch.object(legacy.time,'sleep'),mock.patch('socket.socket',side_effect=AssertionError('network')):
            self.assertEqual(legacy.main(),0)
        self.assertFalse(pending);path=self.export();rows=self.rows(path/'normalized/arxiv_citations.jsonl')
        self.assertEqual(rows[1:],legacy_output['rows'])
        self.assertEqual({k:v for k,v in rows[0]['_meta'].items() if k!='source_pin'},{k:v for k,v in legacy_output['meta'].items() if k!='source_pin'})

    def test_curated_ancestor_reads_exact_commit_native_paths_not_worktree(self):
        repo=self.root/'git';repo.mkdir();env={'PATH':'/usr/bin:/bin','HOME':str(self.root),'GIT_CONFIG_NOSYSTEM':'1'}
        def git(*args):return subprocess.check_output(['/usr/bin/git','-C',str(repo),*args],env=env,stderr=subprocess.DEVNULL)
        git('init','-q');(repo/'selector.json').write_bytes(b'{}');git('add','selector.json');git('-c','user.name=fixture','-c','user.email=x@example.invalid','commit','-qm','seed')
        commit=git('rev-parse','HEAD').decode().strip();tree=git('rev-parse','HEAD^{tree}').decode().strip()
        item={'root':'git','path':'selector.json','name':'selection','sha256':io.sha(b'{}'),'bytes':2}
        source={'pin':{'value':commit,'tree':tree},'objects':[item]};(repo/'selector.json').write_bytes(b'changed ambient')
        self.assertEqual(core.curated_objects(source,{'git':repo}),{'selection':b'{}'})
        source['pin']['tree']='f'*40
        with self.assertRaises(io.ExportError):core.curated_objects(source,{'git':repo})

    def test_every_normalization_program_preimage_is_bound(self):
        data=core.capture_parents(self.plan,self.roots)
        for name in self.programs:
            programs=dict(self.programs);programs[name]+=b'\n'
            with self.assertRaises(io.ExportError):core.build_documents(self.plan,*data,self.profile,programs,WHEN)

    def test_export_tampering_is_detected_by_full_replay(self):
        path=self.export();target=path/'normalized/arxiv_citations.jsonl';target.write_bytes(target.read_bytes()+b'{}\n')
        with self.assertRaises(io.ExportError):producer.verify(path,self.roots)

    def test_failure_after_rename_cleans_only_owned_output(self):
        real=io.stage_io.publish_directory_no_replace
        def fail(owned,target):real(owned,target);raise OSError('uncertain rename')
        with mock.patch.object(io.stage_io,'publish_directory_no_replace',side_effect=fail),self.assertRaises(OSError):self.export()
        self.assertFalse(list(self.store.iterdir()))

    def test_actual_v3_compiler_accepts_scope_and_citation_dependency_closure(self):
        import test_compile_offline_pack_v2 as module
        path=self.export();fragment=json.loads((path/'source-fragment.json').read_bytes())
        f=module.OfflinePackCompilerTest();f.setUp();self.addCleanup(f.tearDown);f._upgrade_plan_v3()
        (f.external/'arxiv_citations.jsonl').write_bytes((path/'normalized/arxiv_citations.jsonl').read_bytes())
        f.inventory['inputs'].append({'id':'external-arxiv-citations','class':'immutable_source_object','cardinality':'one','root':'external','path':'arxiv_citations.jsonl','consumers':['brain/replay.py'],'purpose':'citation closure fixture','requirement':'required'})
        f.inventory['inputs'].sort(key=lambda i:i['id']);f.inventory['inventory_id']=core.contracts.reducer_input_inventory_identity(f.inventory);f.plan['inventory_id']=f.inventory['inventory_id']
        f.plan['sources']=sorted([*f.plan['sources'],*fragment['sources']],key=lambda s:s['source']);f.plan['input_bindings']+=fragment['input_bindings'];f.plan['input_bindings'].sort(key=lambda i:i['input_id'])
        f.inventory_path.write_bytes(io.canonical(f.inventory));f.plan_path.write_bytes(io.canonical(f.plan))
        packed=module.compiler.compile_offline_pack_v2(f.plan_path.resolve(),f.inventory_path.resolve(),(f.base/'citation-pack').resolve(),roots={'repo':f.repo.resolve(),'external':f.external.resolve(),**self.roots,core.PHYSICAL_ROOT:path},git_executable='/usr/bin/git')
        manifest,_=core.contracts.load_canonical_json(packed.manifest_path);core.contracts.verify_offline_pack_files(manifest,packed.root,manifest_path=packed.manifest_path)


if __name__=='__main__':unittest.main()
