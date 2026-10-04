"""Standalone coverage: real verified pack/release plus adversarial pool/join cases."""
from __future__ import annotations
import copy
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE));sys.path.insert(0,str(HERE/'tools'))
import provenance_coverage as core
import source_policy_reviews as policy
import test_authority_contracts as af
import test_release_builder as rf
import build_release
from test_authority_contracts import file_ref,write_canonical


class CoverageIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.release_fixture=rf.ReleaseBuilderTest(methodName='runTest');self.release_fixture.setUp();self.addCleanup(self.release_fixture.tearDown)
        self.fixture=af.V3EvidenceClosureTest(methodName='runTest');self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        registry_path=self.release_fixture.repo/'catalog/data/source_registry.json'
        registry=json.loads(registry_path.read_bytes());registry['edge_sources']['wikidata_props']={'target_license':'CC0-1.0','name':'Fixture Wikidata claims'}
        registry_raw=core.canonical(registry);registry_path.write_bytes(registry_raw)
        static_path=self.release_fixture.repo/'site/assets/brain/sources.json';static=json.loads(static_path.read_bytes())
        static['sources'].append({**{field:registry['edge_sources']['wikidata_props'].get(field,'') for field in ('name','homepage','layer','kind','our_provenance','target_license','wikidata_property','note')},'key':'wikidata_props','group':'edge_sources'});static_path.write_bytes(core.canonical(static))
        claims=core.canonical({'s':'Q1','o':'Q2','p':'P279','p_label':'subclass of'})+b'\n'
        original=self.fixture.v2.make_source_manifest
        def manifest(**kw):
            kw['normalized']=registry_raw if kw['source']=='curated-fixture' else claims
            return original(**kw)
        with mock.patch.object(self.fixture.v2,'make_source_manifest',side_effect=manifest):
            self.pack,self.pack_path,sources,*_=self.fixture.make_v3_pack()
        self.pack_path=self.pack_path.resolve()
        inventory=json.loads((self.fixture.root/self.pack['inventory']['path']).read_bytes())
        curated=copy.deepcopy(next(b for b in self.pack['input_bindings'] if b['input_id']=='curated'));curated['input_id']='source-registry'
        source=copy.deepcopy(next(b for b in self.pack['input_bindings'] if b['input_id']=='source'));source['input_id']='wikidata-edges'
        absent=copy.deepcopy(next(b for b in self.pack['input_bindings'] if b['input_id']=='optional_external'))
        bindings=[curated,source]
        declarations=[]
        for name in ('source-registry','wikidata-edges','concept-graph','wikidata-universe'):
            prototype=copy.deepcopy(next(i for i in inventory['inputs'] if i['id']==('curated' if name=='source-registry' else 'source' if name=='wikidata-edges' else 'optional_external')))
            prototype['id']=name
            if name in {'concept-graph','wikidata-universe'}:
                prototype['path_pattern']=name+'/*.json';b=copy.deepcopy(absent);b['input_id']=name;bindings.append(b)
            declarations.append(prototype)
        inventory['inputs']=sorted(declarations,key=lambda d:d['id']);inventory['inventory_id']=core.contracts.reducer_input_inventory_identity(inventory)
        write_canonical(self.fixture.root/self.pack['inventory']['path'],inventory)
        self.pack['inventory']={**file_ref(self.fixture.root,self.pack['inventory']['path'],'application/json'),'inventory_id':inventory['inventory_id']}
        self.pack['input_bindings']=sorted(bindings,key=lambda b:b['input_id'])
        self.pack['source_set_root']=core.contracts.source_set_root_v3(inventory['inventory_id'],[s['source_manifest_id'] for s in self.pack['source_manifests']],self.pack['input_bindings'])
        self.pack['offline_pack_id']=core.contracts.offline_pack_identity(self.pack);write_canonical(self.pack_path,self.pack)
        self.prov={'source':'wikidata_props','method':'wikidata-claims','pin':sources['external-fixture']['pin']['value']}
        edge_path=self.release_fixture.repo/'brain/data/edges.jsonl';meta=json.loads(edge_path.read_bytes().splitlines()[0])
        edge={'src':'Q1','dst':'Q2','kind':'relates','provenance':self.prov,'evidence':{'properties':[{'p':'P279','label':'subclass of'}]}}
        edge_path.write_bytes(core.canonical(meta)+b'\n'+core.canonical(edge)+b'\n')
        cell_manifest=self.release_fixture.repo/'site/assets/brain/cells/manifest.json';document=json.loads(cell_manifest.read_bytes());document['prov']=[self.prov];cell_manifest.write_bytes(core.canonical(document))
        for relative in ('brain/data/cells.jsonl','brain/data/synapses.jsonl'):
            path=self.release_fixture.repo/relative;rows=[core.contracts.parse_artifact_json_bytes(line,location='fixture') for line in path.read_bytes().splitlines()];rows[0]['_meta']['prov']=[self.prov];path.write_bytes(b'\n'.join(core.artifact_bytes(row) for row in rows)+b'\n')
        replay=self.release_fixture.offline_inputs()
        replay=dataclasses.replace(replay,source_set_root=self.pack['source_set_root'],replay={**replay.replay,'offline_pack_id':self.pack['offline_pack_id'],'reducer_inventory_id':inventory['inventory_id']})
        config=self.release_fixture.config(reducer_git_commit=self.pack['reducer']['git_commit'],configuration_sha256=self.pack['configuration']['sha256'],environment_sha256=self.pack['environment']['sha256'])
        result=build_release.build_release(config,_verified_replay=replay);self.release_path=Path(result['manifest']).resolve()
        self.attachments=Path(self.release_fixture.temp.name).resolve()/'attachments';self.attachments.mkdir();body=b'Synthetic test fixture permission only.\n';(self.attachments/'fixture.txt').write_bytes(body)
        evidence={'kind':'review-attachment','path':'fixture.txt','media_type':'text/plain','sha256':hashlib.sha256(body).hexdigest(),'bytes':len(body),'origin':'Synthetic fixture; no actual source rights granted.'};evidence['evidence_id']=policy.evidence_id(evidence)
        self.private=policy.draft_private(self.pack_path);self.private.update(state='approved',reviewer={'name':'Fixture','role':'trusted-local-operator','reviewed_at':'2026-09-08T20:00:00Z'},evidence=[evidence])
        for source in self.private['sources']:source.update(decision='approved',basis='Synthetic fixture only.',evidence_ids=[evidence['evidence_id']],unresolved=[])
        self.private['review_id']=policy.identity(self.private)
        self.mapping=core.draft_mapping(self.pack_path,self.release_path);self.mapping.update(state='reviewed',reviewer={'name':'Fixture','reviewed_at':'2026-09-08T20:00:00Z'})
        for rule in self.mapping['rules']:rule['policy'].update(basis='Synthetic fixture only.',registry_entries=['/edge_sources/wikidata_props'],evidence_ids=[evidence['evidence_id']])
        self.mapping['mapping_id']=core.identity(self.mapping)
    def check(self,mapping=None,**kwargs):
        mapping=mapping or self.mapping
        return core.check(mapping,self.pack_path,self.release_path,self.private,expected_mapping_id=kwargs.pop('expected_mapping_id',mapping['mapping_id']),expected_private_id=self.private['review_id'],private_attachment_root=self.attachments,**kwargs)
    def rehash(self,mapping):mapping['mapping_id']=core.identity(mapping);return mapping
    def test_real_verified_pack_release_ready_counts_witness_and_policy(self):
        result=self.check();self.assertTrue(result['provenance_coverage_ready'],result['failures'])
        self.assertEqual(result['occurrences']['total'],4);self.assertEqual(result['witnesses'],{'checked':1,'declaration-only':3});self.assertEqual(core.validate_report(result),result)
    def test_expected_mapping_id_cannot_be_self_replaced(self):
        changed=copy.deepcopy(self.mapping);changed['reviewer']['name']='Other';self.rehash(changed)
        with self.assertRaisesRegex(core.CoverageError,'expected ID'):self.check(changed,expected_mapping_id=self.mapping['mapping_id'])
    def test_secondary_join_omission_and_member_rebinding_are_rejected(self):
        for mutate in (lambda d:d['rules'][0]['supporting_inputs'].pop(),lambda d:d['input_bindings'][0]['source_manifest_ids'].clear(),lambda d:d.update(implementation_root='sha256:'+'0'*64)):
            changed=copy.deepcopy(self.mapping);mutate(changed);self.rehash(changed)
            with self.assertRaises(core.CoverageError):self.check(changed)
    def test_duplicate_method_mapping_is_ambiguous(self):
        changed=copy.deepcopy(self.mapping);rule=copy.deepcopy(changed['rules'][0]);rule['id']+='z';changed['rules'].append(rule);self.rehash(changed)
        with self.assertRaisesRegex(core.CoverageError,'ambiguous'):self.check(changed)
    def test_pending_or_missing_policy_never_becomes_ready(self):
        for mutate in (lambda d:d.update(state='pending',reviewer=None),lambda d:d['rules'][0]['policy'].update(basis=''),lambda d:d['rules'][0]['policy'].update(evidence_ids=['not-retained'])):
            changed=copy.deepcopy(self.mapping);mutate(changed);self.rehash(changed);self.assertFalse(self.check(changed)['provenance_coverage_ready'])
    def test_unknown_family_pin_property_and_endpoint_failures_remain_diagnostics(self):
        original=core.occurrences
        for mutate in (lambda o:o['provenance'].update(pin='0'*64),lambda o:o['provenance'].update(method='not a reviewed producer'),lambda o:o['context']['evidence']['properties'][0].update(p='P31'),lambda o:o['context'].update(dst='xref:oeis:A2')):
            def changed(*args):
                for occurrence in original(*args):
                    if occurrence['type']=='direct':occurrence=copy.deepcopy(occurrence);mutate(occurrence)
                    yield occurrence
            with mock.patch.object(core,'occurrences',side_effect=changed):report=self.check()
            self.assertFalse(report['provenance_coverage_ready']);self.assertEqual(report['failures']['total'],1);self.assertEqual(report['occurrences']['total'],4)
    def test_changed_verified_artifact_cannot_be_described_by_old_mapping(self):
        path=self.release_path.parent/'brain/data/edges.jsonl';path.chmod(0o600);path.write_bytes(path.read_bytes()+b'{}\n')
        with self.assertRaises((core.CoverageError,core.contracts.VerificationError)):self.check()
    def test_rehashed_report_bool_counts_and_scope_inflation_are_rejected(self):
        report=self.check()
        for mutate in (lambda r:r['occurrences'].update(total=True),lambda r:r['scope'].update(accepted_authority=True),lambda r:r['limits'].update(semantic_reconstruction='complete')):
            changed=copy.deepcopy(report);mutate(changed);changed['report_id']=core.identity(changed)
            with self.assertRaises((core.CoverageError,core.contracts.VerificationError)):core.validate_report(changed)
    def test_cli_outputs_pending_canonical_mapping(self):
        result=subprocess.run([sys.executable,'-I','-S',str(HERE/'tools/check_provenance_coverage.py'),'draft','--pack',str(self.pack_path),'--release',str(self.release_path)],capture_output=True,check=True)
        value=json.loads(result.stdout);self.assertEqual(result.stdout,core.canonical(value));self.assertEqual(value['state'],'pending')


class CoverageEnumerationTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name).resolve()
        self.prov={'source':'oeis','method':'internal_link','pin':'a'*64}
    def release(self,extra,manifest=None):
        docs={'site/assets/brain/cells/manifest.json':manifest or {'prov':[self.prov]},**extra};artifacts=[]
        for path,value in docs.items():
            target=self.root/path;target.parent.mkdir(parents=True,exist_ok=True)
            raw=(b'\n'.join(core.artifact_bytes(r) for r in value)+b'\n') if path.endswith('.jsonl') else core.artifact_bytes(value);target.write_bytes(raw)
            artifacts.append({**file_ref(self.root,path,'application/json'),'logical_format':'jsonl-rowset' if path.endswith('.jsonl') else 'json'})
        return {'artifacts':sorted(artifacts,key=lambda r:r['path'])}
    def test_all_pool_entries_and_cell_trace_references_are_enumerated(self):
        unused={'source':'bad','method':'unknown'}
        trace={'src':'xref:oeis:A1','dst':'xref:oeis:A2','kind':'links','prov':0}
        release=self.release({'brain/data/synapses.jsonl':[{'_meta':{'prov':[self.prov,unused]}},{'traces':[trace]}],'site/assets/brain/cells/traces/ab.json':{'cell:a':[trace]}})
        rows=list(core.occurrences(release,self.root));self.assertEqual(len(rows),5);self.assertEqual(sum(r['type']=='reference' for r in rows),2);self.assertIn(unused,[r['provenance'] for r in rows])
    def test_bool_negative_out_of_bounds_null_and_nested_pool_are_rejected(self):
        for invalid in (True,-1,1,None,[self.prov]):
            release=self.release({'site/assets/brain/cells/ab.json':{'cell:a':{'organs':[{'kind':'decl','id':'decl:Mathlib:A','prov':invalid}]}}})
            with self.assertRaises(core.CoverageError):list(core.occurrences(release,self.root))
    def test_base_edge_without_provenance_fails(self):
        release=self.release({'brain/data/edges.jsonl':[{'src':'Q1','dst':'Q2','kind':'relates'}]})
        with self.assertRaisesRegex(core.CoverageError,'lacks provenance'):list(core.occurrences(release,self.root))
    def test_exact_source_method_context_dispatch_rejects_cross_database_alias(self):
        spec=core.families.classify(self.prov)
        core.validate_context(spec,self.prov,{'src':'xref:oeis:A1','dst':'xref:oeis:A2','kind':'links'},'direct')
        with self.assertRaisesRegex(core.CoverageError,'endpoint context'):core.validate_context(spec,self.prov,{'src':'xref:nlab:A1','dst':'xref:nlab:A2','kind':'links'},'direct')
        provenance={'source':'wikidata_props','method':'wikidata-claims'}
        with self.assertRaisesRegex(core.CoverageError,'unknown emitted edge kind'):core.validate_context(core.families.classify(provenance),provenance,{'src':'Q1','dst':'Q2','kind':'invented'},'direct')
        for p in ({'source':'wikilean_tags','method':'AI-queued @[wikidata] candidate (brain)'},{'source':'mathlib','method':'imaginary'}):self.assertIsNone(core.families.classify(p))
    def test_known_dual_input_join_and_synthetic_policy_gaps_are_explicit(self):
        spec=core.families.classify({'source':'oeis','method':'erdosproblems.com join (problems.yaml)'});self.assertEqual(spec['primary'],('formal-conjectures','erdos-joins'))
        self.assertIsNotNone(core.families.classify({'source':'tag-queue','method':'AI-queued @[wikidata] candidate (brain)','queue':'brain_queue'}))
        self.assertIsNone(core.families.classify({'source':'tag-queue','method':'AI-queued @[wikidata] candidate (brain)','queue':'unknown'}))
    def test_supporting_repositories_are_not_filtered_by_external_primary_pin(self):
        inputs=core.Inputs.__new__(core.Inputs)
        members=[{'path':'catalog/data/user_repos/a.jsonl','source_manifest_id':'a','object':'rows'},{'path':'catalog/data/user_repos/b.jsonl','source_manifest_id':'b','object':'rows'}]
        inputs.groups={'user-repos':{'members':members}};inputs.sources={'a':{'pin':{'value':'repo-pin'}},'b':{'pin':{'value':'repo-pin'}}}
        prov={'source':'oeis','method':'internal_link (projected)','pin':'external-link-pin'}
        self.assertIs(inputs.members('user-repos',prov,{}),members)
        direct={'source':'user_lean_repos','method':'file-tree (user_repos/a.jsonl)','pin':'repo-pin'}
        with mock.patch.object(inputs,'metadata',side_effect=lambda m:{'lib':'A' if m['source_manifest_id']=='a' else 'B','repo':'owner/'+m['source_manifest_id']}):
            self.assertEqual(inputs.members('user-repos',direct,{'src':'path:A','dst':'decl:A:T'}),members[:1])
            self.assertEqual(inputs.members('user-repos',direct,{'src':'path:B','dst':'decl:B:T'}),[])
            reference={'source':'oeis','method':'user-repo reference URL (owner/b)','pin':'repo-pin'}
            self.assertEqual(inputs.members('user-repos',reference,{'src':'decl:B:T','dst':'xref:oeis:A1'}),members[1:])
    def test_shared_page_statement_and_queued_concept_contexts_are_explicit(self):
        cases=[({'source':'oeis','method':'wikidata-property'}, {'src':'xref:oeis:A1','dst':'xref:oeis:A1','kind':'co-page','evidence':{'page':'xref:oeis:A1','db':'oeis'}}),
               ({'source':'theoremgraph','method':'theorem_matching dual-judge'},{'src':'lit:1234.0001:T1','dst':'lit:1234.0001:T1','kind':'co-statement','evidence':{'statement':'lit:1234.0001:T1'}}),
               ({'source':'tag-queue','method':'AI-queued @[wikidata] candidate (brain)','queue':'brain_queue'},{'kind':'concept','id':'Q1'})]
        for prov,context in cases:
            spec=core.families.classify(prov);core.validate_context(spec,prov,context,'reference')
            changed=copy.deepcopy(context);changed['kind']='article'
            with self.assertRaises(core.CoverageError):core.validate_context(spec,prov,changed,'reference')
        changed=copy.deepcopy(cases[0][1]);changed['dst']='xref:nlab:A1'
        with self.assertRaisesRegex(core.CoverageError,'shared-page'):core.validate_context(core.families.classify(cases[0][0]),cases[0][0],changed,'reference')
    def test_weak_bond_trace_is_read_as_formalizes_only_in_its_exact_projected_shape(self):
        prov={'source':'mathlib','method':'agent+oracle','pin':'a'*64};spec=core.families.classify(prov)
        for kind in sorted(core.WEAK_FORMALIZES_KINDS):
            context={'src':'Q1','dst':'decl:Mathlib:A','kind':kind,'evidence':{'match_kind':kind}}
            self.assertEqual(core.claim_kind(spec,context,'reference'),'formalizes');core.validate_context(spec,prov,context,'reference')
            # A base edge row, a trace naming another match kind, and a trace without one stay literal and are refused.
            for changed,occurrence in (({},'direct'),({'evidence':{'match_kind':'exact'}},'reference'),({'evidence':{}},'reference')):
                bad={**context,**changed};self.assertEqual(core.claim_kind(spec,bad,occurrence),kind)
                with self.assertRaisesRegex(core.CoverageError,'edge kind'):core.validate_context(spec,prov,bad,occurrence)
            with self.assertRaisesRegex(core.CoverageError,'endpoint context'):core.validate_context(spec,prov,{**context,'dst':'Q2'},'reference')
        # A family that cannot emit `formalizes`, or whose own vocabulary already has the kind, is never rewritten.
        depends={'source':'mathlib_deps','method':'lift_formal_edges (formal_dependency.csv)'}
        with self.assertRaisesRegex(core.CoverageError,'edge kind'):core.validate_context(core.families.classify(depends),depends,{'src':'Q1','dst':'Q2','kind':'related','evidence':{'match_kind':'related'}},'reference')
        frontier={'source':'tauceti','method':'fq-name-in-statement'}
        self.assertEqual(core.claim_kind(core.families.classify(frontier),{'kind':'invocation','evidence':{'match_kind':'invocation'}},'reference'),'invocation')
        self.assertEqual(core.claim_kind(spec,{'kind':'exact','evidence':{'match_kind':'exact'}},'reference'),'exact')
        self.assertEqual(core.WEAK_FORMALIZES_KINDS,frozenset({'invocation','related','special_case','generalization'}))
        self.assertEqual(core.claim_kind(spec,{'kind':'related'},'reference'),'related')
        self.assertEqual(core.claim_kind(spec,{'kind':'related','evidence':{'match_kind':'invocation'}},'reference'),'related')
        self.assertEqual(core.claim_kind(core.families.classify(depends),{'kind':'related','evidence':{'match_kind':'related'}},'reference'),'related')
        # Rule 3 skips `path:` targets and queue bonds never become traces: both stay literal and are refused.
        container={'source':'mathlib','method':'container_links'}
        with self.assertRaisesRegex(core.CoverageError,'edge kind'):core.validate_context(core.families.classify(container),container,{'src':'Q1','dst':'path:Mathlib/Algebra','kind':'related','evidence':{'match_kind':'related'}},'reference')
        queued={'source':'tag-queue','method':'AI-queued @[wikidata] candidate (unreviewed)','queue':'seed_queue'}
        with self.assertRaisesRegex(core.CoverageError,'unrelated organ/trace'):core.validate_context(core.families.classify(queued),queued,{'src':'Q1','dst':'decl:Mathlib:A','kind':'related','evidence':{'match_kind':'related'}},'reference')
    def test_weak_bond_discovery_witness_must_carry_the_trace_match_kind(self):
        prov={'source':'mathlib','method':'discovery_proposals (verified)','pin':'a'*64};spec=core.families.classify(prov)
        row={'src':'Q1','dst':'decl:Mathlib:A','kind':'formalizes','verified':True,'evidence':{'match_kind':'invocation'}}
        inputs=mock.Mock();inputs.indexed.side_effect=lambda member,fields,values:[row] if values==('Q1',) else []
        selected={'brain-discovery-proposals':[{'source_manifest_id':'m','object':'rows'}]}
        context={'src':'Q1','dst':'decl:Mathlib:A','kind':'invocation','evidence':{'match_kind':'invocation'}}
        self.assertEqual(core.witness(spec,prov,context,selected,inputs,'reference'),'checked')
        for changed in ({'kind':'related','evidence':{'match_kind':'related'}},{'evidence':{'match_kind':'exact'}}):
            with self.assertRaisesRegex(core.CoverageError,'discovery witness'):core.witness(spec,prov,{**context,**changed},selected,inputs,'reference')
        # The same shape on a base edge row is not a projected trace, and an ordinary `formalizes` claim is compared literally.
        with self.assertRaisesRegex(core.CoverageError,'discovery witness'):core.witness(spec,prov,context,selected,inputs,'direct')
        self.assertEqual(core.witness(spec,prov,{**context,'kind':'formalizes'},selected,inputs,'direct'),'checked')
        # A retained proposal that states no match kind cannot witness a weak trace.
        row.pop('evidence')
        with self.assertRaisesRegex(core.CoverageError,'discovery witness'):core.witness(spec,prov,context,selected,inputs,'reference')
    def test_article_organ_matches_its_annotation_file_under_the_reducer_en_dash_rule_only(self):
        inputs=core.Inputs.__new__(core.Inputs)
        members=[{'path':'site/annotations/Curry-Howard_correspondence.json','source_manifest_id':'a','object':'x'},{'path':'site/annotations/Other.json','source_manifest_id':'a','object':'y'}]
        inputs.groups={'annotations':{'members':members}};slugs={'x':'Curry-Howard_correspondence','y':'Other'}
        prov={'source':'wikilean','method':'annotated article (D1)'}
        with mock.patch.object(inputs,'read_json',side_effect=lambda member:{'slug':slugs[member['object']]}):
            for organ in ('Curry\u2013Howard_correspondence','Curry-Howard_correspondence'):
                self.assertEqual(inputs.members('annotations',prov,{'kind':'article','id':organ}),members[:1])
            for organ in ('Curry\u2014Howard_correspondence','Curry_Howard_correspondence','curry-howard_correspondence'):
                self.assertEqual(inputs.members('annotations',prov,{'kind':'article','id':organ}),[])
        # Every en dash of the organ id is folded, the exact slug still matches, and the file side is never folded.
        more=[{'path':'site/annotations/A-B-C.json','source_manifest_id':'a','object':'p'},{'path':'site/annotations/P.json','source_manifest_id':'a','object':'q'}]
        inputs.groups={'annotations':{'members':more}};slugs={'p':'A-B-C','q':'P\u2013Q'}
        with mock.patch.object(inputs,'read_json',side_effect=lambda member:{'slug':slugs[member['object']]}):
            self.assertEqual(inputs.members('annotations',prov,{'kind':'article','id':'A\u2013B\u2013C'}),more[:1])
            self.assertEqual(inputs.members('annotations',prov,{'kind':'article','id':'P\u2013Q'}),more[1:])
            self.assertEqual(inputs.members('annotations',prov,{'kind':'article','id':'P-Q'}),[])
    def test_import_captured_whole_local_closure_rejects_changed_helper_and_origin(self):
        records=core.implementation();self.assertEqual(len(records),5);self.assertIn('brain/tools/provenance_coverage_families.py',[r[0] for r in records])
        original=policy.secure_read
        def changed(path,**kw):
            raw=original(path,**kw);return raw+b'\n# changed' if str(path).endswith('provenance_coverage_families.py') else raw
        with mock.patch.object(policy,'secure_read',side_effect=changed),self.assertRaisesRegex(core.CoverageError,'changed after import'):core.implementation()
        with mock.patch.object(core.families,'__file__','/unrelated.py'),self.assertRaisesRegex(core.CoverageError,'origin'):core.implementation()
    def test_json_artifact_numbers_and_non_nfc_evidence_are_preserved(self):
        import decimal
        provenance={'source':'wikidata_props','method':'wikidata-claims','pin':'f'*64}
        release=self.release({'site/assets/brain/cells/traces/ab.json':{'cell:a':[{'src':'Q1','dst':'Q2','kind':'relates','prov':0,'evidence':{'score':decimal.Decimal('0.125'),'text':'e\u0301'}}]}},manifest={'prov':[provenance]})
        # The fixture serializer uses the artifact grammar for decimal values.
        rows=list(core.occurrences(release,self.root));trace=next(o for o in rows if o['type']=='reference');self.assertEqual(trace['context']['evidence']['score'],decimal.Decimal('0.125'));self.assertEqual(trace['context']['evidence']['text'],'e\u0301');core.artifact_bytes(trace)

if __name__=='__main__':unittest.main()
