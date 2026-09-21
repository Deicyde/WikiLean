"""Closed OpenAlex/arXiv request phases, negative evidence, budgets and receipts."""
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
import openalex_source_evidence as core
import openalex_sources as cli

WHEN = '2026-09-08T12:00:00Z'
AIDS = ['0704.1309','0704.3749','0705.3356']
W = lambda n: 'https://openalex.org/W'+str(n)


class OpenalexSourcesTest(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name).resolve()
        self.programs={n:(core.ROOT/n).read_bytes() for n in core.TOOL_FILES}
        self.profile={'files':[{'path':n,'sha256':core.sha(raw)} for n,raw in sorted(self.programs.items())],'policy':copy.deepcopy(core.POLICY)}
        self.profile['profile_id']=core.profile_id(self.profile)
        self.registry=self.root/'profiles.json';self.registry.write_bytes(core.canonical({'schema':core.PROFILE_SCHEMA,'current_profile':self.profile['profile_id'],'profiles':[self.profile]}))
        patch=mock.patch.object(core,'REGISTRY',self.registry);patch.start();self.addCleanup(patch.stop)
        self.tool={'schema':core.TOOL_SCHEMA,'profile_id':self.profile['profile_id'],'files':self.profile['files'],
            'python':{'sha256':'a'*64,'version':'CPython 3.12.13 -I -S'},'curl':{'sha256':'b'*64,'version':'curl fixture'}}
        self.selector=json.dumps({'links':{'Cafe\u0301':[{'arxiv_id':a} for a in [*AIDS,'teorth/pfr']]}},ensure_ascii=False).encode()
        self.plan={'schema':core.PLAN_SCHEMA,'source':core.SOURCE,'uri':core.API,'mailto':None,
            'scope':{'source':core.SCOPE_SOURCE,'source_manifest_id':'sha256:'+'c'*64,'object':core.SCOPE_OBJECT,'sha256':core.sha(self.selector),'bytes':len(self.selector)},
            'arxiv_ids':AIDS,'excluded_non_arxiv_ids':['teorth/pfr'],'minimum_links':3}
        self.records=[];state=core.WalkState(self.plan,self.programs['brain/ingest/openalex_citations.py'])
        while state.next_name is not None:
            row=self.fixture_record(state.next_name);self.records.append(row);state.accept(row)
        self.facts=state.facts();self.rows=state.result['rows']

    @staticmethod
    def metadata(raw,**kwargs):
        return {'curl_exit_code':0,'http_status':200,'content_type':'application/json','sha256':core.sha(raw),'bytes':len(raw),'retry_after':None,'location':None,
            'quota':{'limit_usd':'0.1','prepaid_remaining_usd':'0','cost_usd':'0.0001','remaining_usd':'0.0999'},**kwargs}

    def record(self,target,raw,*,attempt=1,outcome='succeeded',delay=0,**kwargs):
        return {'request':core.parameters(target),'response':self.metadata(raw,**kwargs),'body_base64':base64.b64encode(raw).decode(),
            'attempt':attempt,'outcome':outcome,'retry_delay_seconds':delay}

    def fixture_record(self,t):
        phase=t['phase'].split(':')[0];changes={};syntax=core.parser(self.programs['brain/ingest/openalex_citations.py'])
        if phase=='docs':
            raw={'access':b'<html>No key at all keyless 429</html>','costs':b'<html>Without a key $0.10</html>',
                'openalex_license':b'<html>All metadata CC0</html>','arxiv_license':b'<html>descriptive CC0 1.0 one request every three seconds</html>'}[t['values'][0]]
            changes={'content_type':'text/html'}
        elif phase=='a':
            raw=self.batch([{'id':W(1),'doi':'https://doi.org/'+syntax.doi_of(AIDS[0]),'referenced_works':[W(4),W(99)]}])
        elif phase=='direct':
            if t['values']==[AIDS[1]]:raw=b'{"error":"not found"}';changes={'curl_exit_code':22,'http_status':404}
            elif '/https://doi.org/' in t['uri']:raw=b'<html>merged</html>';changes={'http_status':301,'location':core.API+'/W3','content_type':'text/html'}
            else:raw=core.canonical({'id':W(3),'doi':'https://doi.org/'+syntax.doi_of(AIDS[2]),'referenced_works':[W(1)]})
        elif phase=='arxiv':
            raw=('<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">'+''.join(
                '<entry><id>http://arxiv.org/abs/'+a+'v2</id>'+('<arxiv:doi>10.1234/twin,</arxiv:doi>' if a==AIDS[1] else '')+'</entry>' for a in AIDS)+'</feed>').encode()
            changes={'content_type':'application/atom+xml'}
        elif phase=='journal':raw=self.batch([{'id':W(2),'doi':'https://doi.org/10.1234/twin'}])
        elif phase=='identify':raw=self.batch([{'id':W(4),'doi':None,'locations':[{'landing_page_url':'https://arxiv.org/abs/'+AIDS[2]+'v4','pdf_url':None}]}])
        elif phase=='twins':raw=self.batch([{'id':W(2),'referenced_works':[W(3),W(5)]},{'id':W(4),'referenced_works':[W(1)]}])
        elif phase=='identify-final':raw=self.batch([{'id':W(5),'doi':'https://doi.org/'+syntax.doi_of(AIDS[1]),'locations':[]}])
        else:raise AssertionError(t)
        return self.record(t,raw,**changes)

    @staticmethod
    def batch(results):return core.canonical({'meta':{'count':len(results)},'results':results})
    def capture(self,records=None):return core.capture_files(self.plan,self.records if records is None else records,self.tool,self.programs,WHEN,self.selector)
    def build(self):return core.build_export({k:v for k,v in self.capture().items() if k!='manifest.json'},self.profile,self.programs,WHEN)

    def enable_v2(self):
        previous=copy.deepcopy(self.profile)
        self.plan['schema']=core.PLAN_SCHEMA_V2
        self.profile={**previous,'policy':copy.deepcopy(core.POLICY_V2)}
        self.profile['profile_id']=core.profile_id(self.profile)
        self.registry.write_bytes(core.canonical({'schema':core.PROFILE_SCHEMA,'current_profile':self.profile['profile_id'],
            'profiles':sorted([previous,self.profile],key=lambda p:p['profile_id'])}))
        self.tool.update(profile_id=self.profile['profile_id'],files=self.profile['files'])
        for row in self.records:row['request']=core.parameters(row['request']['selection'],core.POLICY_V2)

    def v2_retry_rows(self):
        index=next(i for i,r in enumerate(self.records) if r['request']['selection']['phase'].startswith('arxiv:'))
        failure=self.record(self.records[index]['request']['selection'],b'rate limited',outcome='failed',delay=60,
            http_status=429,curl_exit_code=22,content_type='text/html')
        failure['request']=core.parameters(failure['request']['selection'],core.POLICY_V2)
        success=copy.deepcopy(self.records[index]);success['attempt']=2
        return [*self.records[:index],failure,success,*self.records[index+1:]],index

    def test_v2_arxiv_429_retains_true_attempts_and_old_generation_stays_identical(self):
        old=self.capture();self.enable_v2()
        core.verify_capture_files({k:v for k,v in old.items() if k!='manifest.json'})
        rows,index=self.v2_retry_rows();capture=self.capture(rows)
        core.verify_capture_files({k:v for k,v in capture.items() if k!='manifest.json'})
        receipt=json.loads(capture['receipt.json'])
        self.assertEqual(receipt['batch']['requests_failed'],1)
        self.assertEqual(receipt['attempts'][index]['response_sha256'],core.sha(b'rate limited'))
        self.assertEqual(receipt['attempts'][index]['outcome'],'failed')
        for invalid in (rows[:index]+rows[index+1:],rows[:index+1]):
            with self.assertRaises(core.EvidenceError):self.capture(invalid)
        changed=copy.deepcopy(rows);changed[index]['retry_delay_seconds']=59
        with self.assertRaises(core.EvidenceError):self.capture(changed)

    def test_v2_rate_limit_authority_is_arxiv_only_and_bounded(self):
        meta=self.metadata(b'limited',http_status=429,curl_exit_code=22)
        self.assertIsNone(core.retry_delay(meta,1,phase='arxiv'))
        for phase in ('a','direct','journal','identify','twins','docs',None):
            self.assertIsNone(core.retry_delay(meta,1,phase=phase,policy=core.POLICY_V2))
        self.assertEqual([core.retry_delay(meta,i,phase='arxiv',policy=core.POLICY_V2) for i in range(1,6)],[60,120,240,480,None])
        for bad in (0,-1,True,1.0):self.assertIsNone(core.retry_delay(meta,bad,phase='arxiv',policy=core.POLICY_V2))
        for status in (400,401,403):
            self.assertIsNone(core.retry_delay({**meta,'http_status':status},1,phase='arxiv',policy=core.POLICY_V2))
        for after,expected in (({'kind':'delay-seconds','seconds':0},60),({'kind':'delay-seconds','seconds':90},90),
            ({'kind':'delay-seconds','seconds':900},900),({'kind':'delay-seconds','seconds':901},None),
            ({'kind':'unsupported-or-excessive'},None)):
            self.assertEqual(core.retry_delay({**meta,'retry_after':after},1,phase='arxiv',policy=core.POLICY_V2),expected)

    def test_plan_profile_mismatch_refuses_before_any_network_or_capture(self):
        old_tool=copy.deepcopy(self.tool);self.enable_v2()
        with self.assertRaisesRegex(core.EvidenceError,'different policies'):
            core.capture_files(self.plan,self.records,old_tool,self.programs,WHEN,self.selector)
        plan=self.root/'plan.json';plan.write_bytes(core.canonical({**self.plan,'schema':core.PLAN_SCHEMA}))
        selector=self.root/'selector.json';selector.write_bytes(self.selector)
        with mock.patch.object(cli,'require_startup'),mock.patch.object(cli,'runtime_identity',return_value=(self.tool,self.programs)),mock.patch.object(cli,'transport') as network:
            with self.assertRaisesRegex(core.EvidenceError,'different policies'):
                cli.acquire(plan,self.root/'captures',Path(sys.executable).resolve(),selector)
        network.assert_not_called();self.assertFalse((self.root/'captures').exists())

    def test_v2_acquirer_waits_longer_and_preserves_rate_limit_failure(self):
        self.enable_v2();rows,_index=self.v2_retry_rows();responses=iter(rows)
        plan=self.root/'plan.json';plan.write_bytes(core.canonical(self.plan));selector=self.root/'selector.json';selector.write_bytes(self.selector)
        def fetch(params,_curl):
            row=next(responses);self.assertEqual(params,row['request']);return core.response_bytes(row),row['response']
        with mock.patch.object(cli,'require_startup'),mock.patch.object(cli,'runtime_identity',return_value=(self.tool,self.programs)),mock.patch.object(cli,'transport',side_effect=fetch),mock.patch.object(cli.time,'sleep') as sleep,mock.patch.object(sys,'stderr',io.StringIO()):
            result=cli.acquire(plan,self.root/'captures',Path(sys.executable).resolve(),selector)
        core.verify_capture(result)
        self.assertIn(mock.call(10),sleep.call_args_list);self.assertIn(mock.call(60),sleep.call_args_list)
        self.assertEqual(json.loads((result/'facts.json').read_bytes())['failed_attempts'],1)

    def test_v2_exhausted_arxiv_rate_limit_keeps_all_five_failed_responses(self):
        self.enable_v2();rows,index=self.v2_retry_rows();failures=[]
        for ordinal,delay in enumerate((60,120,240,480,0),1):
            row=copy.deepcopy(rows[index]);row.update(attempt=ordinal,retry_delay_seconds=delay);failures.append(row)
        responses=iter([*rows[:index],*failures]);plan=self.root/'plan.json';plan.write_bytes(core.canonical(self.plan))
        selector=self.root/'selector.json';selector.write_bytes(self.selector)
        def fetch(params,_curl):
            row=next(responses);self.assertEqual(params,row['request']);return core.response_bytes(row),row['response']
        with mock.patch.object(cli,'require_startup'),mock.patch.object(cli,'runtime_identity',return_value=(self.tool,self.programs)),mock.patch.object(cli,'transport',side_effect=fetch) as network,mock.patch.object(cli.time,'sleep'),mock.patch.object(sys,'stderr',io.StringIO()):
            with self.assertRaisesRegex(core.EvidenceError,'nonretryable or retries exhausted'):
                cli.acquire(plan,self.root/'captures',Path(sys.executable).resolve(),selector)
        self.assertEqual(network.call_count,index+5);self.assertFalse((self.root/'captures').exists())
        retained=next((self.root/'captures-incomplete').iterdir());doc=json.loads((retained/'transcript.json').read_bytes())
        failed=[r for r in doc['completed_requests'] if r['outcome']=='failed']+[doc['failed_request']]
        self.assertEqual([r['attempt'] for r in failed],[1,2,3,4,5])
        self.assertTrue(all(core.response_bytes(r)==b'rate limited' for r in failed))

    def test_exact_phases_scope_direct_404_redirect_and_one_twin_round(self):
        self.assertEqual(self.rows,[{'src':AIDS[0],'dst':AIDS[2]},{'src':AIDS[1],'dst':AIDS[2]},{'src':AIDS[2],'dst':AIDS[0]}])
        self.assertEqual(self.facts['journal_twins'],1);self.assertEqual(self.facts['identified_twins'],1)
        self.assertEqual(self.facts['final_reference_identifications'],1)
        self.assertEqual([r['request']['selection']['phase'].split(':')[0] for r in self.records],['docs']*4+['a','direct','direct','direct','arxiv','journal','identify','twins','identify-final'])
        target=core.archive.publish(self.capture(),self.root/'captures',core.CAPTURE_SCHEMA);core.verify_capture(target)
        export=core.archive.publish(self.build(),self.root/'exports',core.EXPORT_SCHEMA);self.assertEqual(core.verify_export(export)['facts'],self.facts)

    def test_selector_exact_bytes_and_explicit_exclusions_required(self):
        core.validate_selector(self.plan,self.selector,self.programs['brain/ingest/openalex_citations.py'])
        for changed in ({'arxiv_ids':AIDS[:-1]},{'excluded_non_arxiv_ids':[]}):
            with self.assertRaises(core.EvidenceError):core.validate_selector({**self.plan,**changed},self.selector,self.programs['brain/ingest/openalex_citations.py'])
        with self.assertRaises(core.EvidenceError):core.validate_selector(self.plan,self.selector+b' ',self.programs['brain/ingest/openalex_citations.py'])
        with self.assertRaises(core.EvidenceError):core.validate_plan({**self.plan,'mailto':'invented@example.invalid'})
        with self.assertRaisesRegex(core.EvidenceError,'versioned'):core.validate_plan({**self.plan,'arxiv_ids':[AIDS[0]+'v2']})

    def test_omitted_reordered_extra_requests_or_query_preimage_rejected(self):
        for rows in (self.records[:-1],self.records[1:],self.records+[self.records[-1]],self.records[:5]+self.records[6:]):
            with self.assertRaises(core.EvidenceError):self.capture(rows)
        rows=copy.deepcopy(self.records);rows[4]['request']['uri']+='&api_key=fake'
        with self.assertRaises(core.EvidenceError):self.capture(rows)

    def test_only404_is_missing_401_403_429_and_partial200_abort(self):
        for code,status in [(22,401),(22,403),(22,429),(22,400),(18,200),(28,200)]:
            rows=copy.deepcopy(self.records);rows[5]['response'].update(curl_exit_code=code,http_status=status)
            with self.assertRaises(core.EvidenceError):self.capture(rows)
            self.assertIsNone(core.retry_delay(rows[5]['response'],1))

    def test_redirect_requires_explicit_same_authority_and_bounded_chain(self):
        for destination in ('https://other.invalid/W3',core.API+'/W3?api_key=secret',core.API+'/W3#fragment'):
            rows=copy.deepcopy(self.records);rows[6]['response']['location']=destination
            with self.assertRaisesRegex(core.EvidenceError,'redirect authority'):self.capture(rows)
        with mock.patch.dict(core.POLICY,{'maximum_redirects':0}),self.assertRaisesRegex(core.EvidenceError,'redirect limit'):core.replay(self.plan,self.records,self.programs)

    def test_explicit_failed_attempts_are_retained_and_counted(self):
        failure=self.record(self.records[4]['request']['selection'],b'busy',outcome='failed',delay=5,http_status=503,curl_exit_code=22)
        success=copy.deepcopy(self.records[4]);success['attempt']=2
        rows=[*self.records[:4],failure,success,*self.records[5:]]
        capture=self.capture(rows);receipt=json.loads(capture['receipt.json'])
        self.assertEqual(receipt['batch']['requests_failed'],1);self.assertEqual(receipt['batch']['requests_total'],14)
        self.assertEqual(receipt['attempts'][4]['response_sha256'],core.sha(b'busy'))
        with self.assertRaises(core.EvidenceError):self.capture([*self.records[:4],success,*self.records[5:]])
        failure['retry_delay_seconds']=4
        with self.assertRaises(core.EvidenceError):self.capture(rows)

    def test_quota_changes_and_filtered_budget_fail_closed(self):
        for changes in ({'limit_usd':'1'},{'cost_usd':'0.001'},{'remaining_usd':'0'},{'prepaid_remaining_usd':'10'}):
            rows=copy.deepcopy(self.records);rows[4]['response']['quota'].update(changes)
            with self.assertRaises(core.EvidenceError):self.capture(rows)
        with mock.patch.dict(core.POLICY,{'maximum_filtered_attempts':1}),self.assertRaisesRegex(core.EvidenceError,'budget'):core.replay(self.plan,self.records,self.programs)

    def test_batch_overflow_malformed_fields_outside_selection_rejected(self):
        for raw in (b'{"meta":{"count":2},"results":[]}',b'{"error":"bad"}',self.batch([{'id':W(1),'doi':None}])):
            with self.assertRaises(core.EvidenceError):core.batch_document(raw,'id,doi,referenced_works')
        rows=copy.deepcopy(self.records);rows[4]=self.record(rows[4]['request']['selection'],self.batch([{'id':W(9),'doi':'https://doi.org/other','referenced_works':[]}]))
        with self.assertRaisesRegex(core.EvidenceError,'out-of-scope'):self.capture(rows)

    def test_arxiv_exact_entry_scope_xml_bounds_and_absent_doi_semantics(self):
        row=next(r for r in self.records if r['request']['selection']['phase'].startswith('arxiv:'));raw=core.response_bytes(row)
        self.assertEqual(core.arxiv_document(raw,AIDS),{AIDS[0]:None,AIDS[1]:'10.1234/twin',AIDS[2]:None})
        for bad in (raw.replace(AIDS[0].encode(),b'9999.1234'),raw.replace(b'</feed>',b''),b'<!DOCTYPE x>'+raw,raw.replace(b'<entry>',b'<ignored>',1)):
            with self.assertRaises(core.EvidenceError):core.arxiv_document(bad,AIDS)

    def test_legacy_doi_and_location_identifier_projection_is_exact(self):
        syntax=core.parser(self.programs['brain/ingest/openalex_citations.py'])
        self.assertEqual(syntax.arxiv_of_work({'doi':'https://doi.org/10.48550/arXiv.math.GT/0211159','locations':[]}), 'math.gt/0211159')
        self.assertEqual(syntax.arxiv_of_work({'doi':None,'locations':[{'pdf_url':'https://arxiv.org/pdf/math.GT/0211159v2'}]}),'math.GT/0211159')
        self.assertEqual(syntax.doi_of('0704.1309v3'),'10.48550/arXiv.0704.1309')

    def test_response_and_transcript_limits_include_failed_bytes(self):
        for key in ('maximum_total_response_bytes','maximum_transcript_bytes','maximum_response_bytes'):
            with mock.patch.dict(core.POLICY,{key:1}),self.assertRaises(core.EvidenceError):core.replay(self.plan,self.records,self.programs)

    def test_every_tool_preimage_and_scope_support_is_replayed(self):
        capture={k:v for k,v in self.capture().items() if k!='manifest.json'}
        for name in self.programs:
            changed=dict(capture);changed['implementation/'+name]+=b'\n'
            with self.assertRaises(core.EvidenceError):core.verify_capture_files(changed)
        changed=dict(capture);changed['selector.json']+=b' '
        with self.assertRaises(core.EvidenceError):core.verify_capture_files(changed)

    def test_transport_has_no_implicit_authority_and_safe_header_capture(self):
        args=cli.command(self.records[4]['request'],Path('/usr/bin/curl'))
        self.assertNotIn('--location',args);self.assertNotIn('--retry',args);self.assertIn('--proxy',args);self.assertEqual(cli.ENVIRONMENT,{'PATH':'/usr/bin:/bin','LANG':'C','LC_ALL':'C'})
        tail=cli.TRAILER+b'301\ttext/html\t\thttps://api.openalex.org/works/W3?api_key=secret\t0.1\t0\t0\t0.09\n'
        self.assertEqual(cli.response_metadata(b'ok',0,tail)['location'],'unsupported')
        self.assertNotIn('secret',json.dumps(cli.response_metadata(b'ok',0,tail)))

    def test_acquisition_runs_complete_query_sequence_and_polite_arxiv_delay(self):
        plan=self.root/'plan.json';plan.write_bytes(core.canonical(self.plan));selector=self.root/'selector.json';selector.write_bytes(self.selector)
        rows=iter(self.records)
        def fetch(params,_curl):
            row=next(rows);self.assertEqual(params,row['request']);return core.response_bytes(row),row['response']
        with mock.patch.object(cli,'require_startup'),mock.patch.object(cli,'runtime_identity',return_value=(self.tool,self.programs)),mock.patch.object(cli,'transport',side_effect=fetch),mock.patch.object(cli.time,'sleep') as sleep,mock.patch.object(sys,'stderr',io.StringIO()):
            path=cli.acquire(plan,self.root/'captures',Path(sys.executable).resolve(),selector)
        core.verify_capture(path);self.assertIn(mock.call(3.1),sleep.call_args_list)

    def test_failed_quota_keeps_diagnostics_without_receipt_or_followup(self):
        plan=self.root/'plan.json';plan.write_bytes(core.canonical(self.plan));selector=self.root/'selector.json';selector.write_bytes(self.selector)
        rows=iter(self.records[:4]+[self.record(self.records[4]['request']['selection'],b'quota',http_status=429,curl_exit_code=22)])
        def fetch(params,_curl):row=next(rows);return core.response_bytes(row),row['response']
        with mock.patch.object(cli,'require_startup'),mock.patch.object(cli,'runtime_identity',return_value=(self.tool,self.programs)),mock.patch.object(cli,'transport',side_effect=fetch) as network,mock.patch.object(cli.time,'sleep'),self.assertRaises(core.EvidenceError):
            cli.acquire(plan,self.root/'captures',Path(sys.executable).resolve(),selector)
        self.assertEqual(network.call_count,5);self.assertFalse((self.root/'captures').exists())
        child=next((self.root/'captures-incomplete').iterdir());files,_=core.archive.read_bundle(child,'wikilean.openalex-incomplete-attempt/v1')
        self.assertFalse(any('receipt' in key for key in files));self.assertIn(b'quota',base64.b64decode(json.loads(files['transcript.json'])['failed_request']['body_base64']))


    def test_actual_pipe_transport_uses_explicit_environment_and_retains_retry_header(self):
        raw=b'busy';script=self.root/'curl';invocation=self.root/'invocation.json'
        script.write_text('#!'+str(Path(sys.executable).resolve())+'\nimport json,os,sys\nfrom pathlib import Path\n'+
            'Path('+repr(str(invocation))+').write_text(json.dumps({"argv":sys.argv,"env":dict(os.environ)}))\n'+
            'sys.stdout.buffer.write('+repr(raw)+')\nsys.stderr.buffer.write('+repr(cli.TRAILER+b'503\ttext/html; charset=utf-8\t15\t\t0.1\t0\t0.0001\t0.0999\n')+')\nsys.exit(22)\n')
        script.chmod(0o700)
        with mock.patch.dict(os.environ,{'PRIVATE_TOKEN':'fixture','HTTP_PROXY':'http://invalid.example'}), \
                mock.patch.object(cli.subprocess,'Popen',wraps=subprocess.Popen) as launch:
            actual,metadata=cli.transport(self.records[4]['request'],script)
        self.assertEqual(actual,raw);self.assertEqual(metadata['retry_after'],{'kind':'delay-seconds','seconds':15})
        self.assertEqual(launch.call_args.kwargs['env'],cli.ENVIRONMENT)
        args=json.loads(invocation.read_bytes())['argv'];self.assertEqual(args[1],'-q')
        for forbidden in ('--location','--retry','--netrc','--insecure','--user','--compressed'):self.assertNotIn(forbidden,args)
        self.assertEqual(args[args.index('--proxy')+1],'')
        self.assertIn('%header{retry-after}',args[args.index('--write-out')+1])

    def test_eof_then_lingering_transport_preserves_partial_received_bytes(self):
        script=self.root/'linger';script.write_text('#!'+str(Path(sys.executable).resolve())+'\nimport os,time\nos.write(1,b"partial")\nos.close(1);os.close(2)\ntime.sleep(30)\n');script.chmod(0o700)
        with mock.patch.object(cli,'PROCESS_EXIT_TIMEOUT_SECONDS',0.05),self.assertRaises(cli.TransportFailure) as failure:
            cli.transport(self.records[4]['request'],script)
        self.assertEqual(failure.exception.raw,b'partial')
        self.assertLess(failure.exception.response['curl_exit_code'],0)

    def test_actual_v3_compiler_accepts_raw_identity_parent(self):
        import test_compile_offline_pack_v2 as fixture_module
        target = core.archive.publish(self.build(), self.root / "exports", core.EXPORT_SCHEMA)
        fragment = json.loads((target / "source-fragment.json").read_bytes())
        fixture = fixture_module.OfflinePackCompilerTest(); fixture.setUp(); self.addCleanup(fixture.tearDown)
        fixture._upgrade_plan_v3()
        source = fragment["sources"][0]
        raw = next(o for o in source["objects"] if o["name"] == "citation_transcript")
        (fixture.external / "openalex-transcript.json").write_bytes((target / raw["path"]).read_bytes())
        fixture.inventory["inputs"].append({"id": "openalex-transcript", "class": "immutable_source_object", "cardinality": "one", "root": "external",
            "path": "openalex-transcript.json", "consumers": ["brain/replay.py"], "purpose": "complete OpenAlex raw evidence fixture", "requirement": "required"})
        fixture.inventory["inputs"].sort(key=lambda x: x["id"])
        fixture.inventory["inventory_id"] = core.contracts.reducer_input_inventory_identity(fixture.inventory)
        fixture.plan["inventory_id"] = fixture.inventory["inventory_id"]
        fixture.plan["sources"] = sorted([*fixture.plan["sources"], source], key=lambda x: x["source"])
        fixture.plan["input_bindings"].append({"input_id": "openalex-transcript", "state": "present", "sources": [core.SOURCE],
            "members": [{"path": "openalex-transcript.json", "source": core.SOURCE, "object": "citation_transcript"}]})
        fixture.plan["input_bindings"].sort(key=lambda x: x["input_id"])
        fixture.inventory_path.write_bytes(core.canonical(fixture.inventory)); fixture.plan_path.write_bytes(core.canonical(fixture.plan))
        packed = fixture_module.compiler.compile_offline_pack_v2(fixture.plan_path.resolve(), fixture.inventory_path.resolve(),
            (fixture.base / "openalex-pack").resolve(), roots={"repo": fixture.repo.resolve(), "external": fixture.external.resolve(), fragment["physical_root"]: target}, git_executable="/usr/bin/git")
        manifest, _ = core.contracts.load_canonical_json(packed.manifest_path)
        core.contracts.verify_offline_pack_files(manifest, packed.root, manifest_path=packed.manifest_path)


if __name__=='__main__':unittest.main()
