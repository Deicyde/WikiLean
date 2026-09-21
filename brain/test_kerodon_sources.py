"""Complete Kerodon request closure and explicit retry evidence tests."""
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
import kerodon_source_evidence as core
import kerodon_sources as cli

WHEN = "2026-09-08T12:00:00Z"


class KerodonSourcesTest(unittest.TestCase):
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
        shared = {"tag": "0002", "type": "theorem", "reference": "1.0.1", "name": "Cafe\u0301"}
        roots = [{"tag": "0000", "type": "part", "reference": "1", "name": "Foundations", "children": [
            {"tag": "0001", "type": "chapter", "reference": "1", "children": [shared]}, shared]},
            {"tag": "02GZ", "type": "part", "reference": "2", "children": [{"tag": "02H0", "type": "definition", "reference": "6.0.1"}]}]
        self.tags = ["0000", "0001", "0002", "02GZ", "02H0"]
        self.plan = {"schema": core.PLAN_SCHEMA, "source": core.SOURCE, "uri": core.BASE, "minimum_pages": 5, "minimum_links": 5}
        self.pages = {"structure_" + tag: json.dumps(value, ensure_ascii=False).encode() for tag, value in zip(core.ROOT_TAGS, roots)}
        self.pages.update({"content_" + tag: self.html(tag, [self.tags[(i+1) % len(self.tags)], tag, "ZZZZ", "ZZZZ"]) for i, tag in enumerate(self.tags)})
        self.names = ["structure_0000", "structure_02GZ", *("content_" + tag for tag in self.tags)]
        self.records = [self.record(name, self.pages[name]) for name in self.names]

    @staticmethod
    def html(tag, destinations):
        return ('<article id="' + tag + '"><span data-tag="' + tag + '">Identity</span>' +
            ''.join('<a href="/tag/' + dst + '">link</a>' for dst in destinations) + '</article>').encode()

    @staticmethod
    def metadata(raw, **changes):
        return {"curl_exit_code": 0, "http_status": 200, "content_type": "text/html", "sha256": core.sha(raw), "bytes": len(raw), "retry_after": None, **changes}

    def record(self, name, raw, *, attempt=1, outcome="succeeded", delay=0, **metadata):
        return {"request": core.parameters(name), "response": self.metadata(raw, **metadata), "body_base64": base64.b64encode(raw).decode(),
            "attempt": attempt, "outcome": outcome, "retry_delay_seconds": delay}

    def capture(self, records=None):
        return core.capture_files(self.plan, self.records if records is None else records, self.tool, self.programs, WHEN)

    def build(self):
        capture = {name: raw for name, raw in self.capture().items() if name != "manifest.json"}
        return core.build_export(capture, self.profile, self.programs, WHEN)

    def test_both_roots_determine_every_unique_tag_and_out_of_scope_links_survive(self):
        facts, pending = core.replay(self.plan, self.records, self.programs)
        self.assertIsNone(pending)
        self.assertEqual((facts["requests"], facts["pages"], facts["contents_acquired"], facts["links"]), (7, 5, 5, 10))
        state = core.WalkState(self.plan, self.programs['brain/ingest/kerodon.py'])
        for row in self.records: state.accept(row)
        self.assertEqual(state.pages['0002']['title'], 'Cafe\u0301')
        self.assertIn(('0002', 'ZZZZ'), state.links)
        self.assertNotIn(('0002', '0002'), state.links)
        self.assertEqual(state.names, self.names)
        capture = core.archive.publish(self.capture(), self.root / 'captures', core.CAPTURE_SCHEMA)
        core.verify_capture(capture)
        target = core.archive.publish(self.build(), self.root / 'exports', core.EXPORT_SCHEMA)
        self.assertEqual(core.verify_export(target)['facts'], facts)

    def test_missing_reordered_duplicate_and_extra_content_never_certifies(self):
        for records in (self.records[:-1], self.records[:1]+self.records[2:], self.records[:3]+self.records[4:],
                self.records[:2]+[self.records[3],self.records[2]]+self.records[4:], self.records+[self.records[-1]]):
            with self.assertRaises(core.EvidenceError): self.capture(records)

    def test_exact_repeated_structure_identity_allowed_but_conflicts_rejected(self):
        tree = json.loads(self.pages['structure_0000'])
        tree['children'][1]['name'] = 'conflicting name'
        records = copy.deepcopy(self.records); records[0] = self.record('structure_0000', json.dumps(tree).encode())
        with self.assertRaisesRegex(core.EvidenceError, 'conflicting duplicate'): self.capture(records)
        tree = json.loads(self.pages['structure_02GZ']); tree['children'].append({'tag':'0002','type':'definition','reference':'wrong'})
        records = copy.deepcopy(self.records); records[1] = self.record('structure_02GZ', json.dumps(tree).encode())
        with self.assertRaisesRegex(core.EvidenceError, 'conflicting duplicate'): self.capture(records)

    def test_structure_types_depth_visits_and_tag_bounds(self):
        for replacement in (False, '', 0, [None]):
            tree = json.loads(self.pages['structure_0000']); tree['children'] = replacement
            with self.assertRaises(core.EvidenceError): core.structure_document(json.dumps(tree).encode(), '0000')
        with mock.patch.dict(core.POLICY, {'maximum_structure_nodes':1}), self.assertRaises(core.EvidenceError):
            core.structure_document(self.pages['structure_0000'], '0000')
        with mock.patch.dict(core.POLICY, {'maximum_structure_depth':0}), self.assertRaises(core.EvidenceError):
            core.structure_document(self.pages['structure_0000'], '0000')
        with self.assertRaises(core.EvidenceError): core.structure_document(self.pages['structure_0000'], '02GZ')

    def test_content_identity_closing_boundary_and_http_success_required(self):
        for raw, metadata in [(b'', {}),(b'<html>error</html>',{}),(b'<article id="0002">truncated',{}),
                (self.pages['content_0002'],{'http_status':301}), (self.pages['content_0002'],{'curl_exit_code':18})]:
            records=copy.deepcopy(self.records); records[4]=self.record('content_0002',raw,**metadata)
            with self.assertRaises(core.EvidenceError): self.capture(records)

    def test_recorded_retry_receipt_counts_real_attempts_and_retains_failed_bytes(self):
        failed=self.record('content_0000',b'upstream busy',outcome='failed',delay=15,curl_exit_code=22,http_status=503,
            retry_after={'kind':'delay-seconds','seconds':15})
        success=copy.deepcopy(self.records[2]); success['attempt']=2
        records=self.records[:2]+[failed,success]+self.records[3:]
        capture=self.capture(records); receipt=json.loads(capture['receipt.json'])
        self.assertEqual(receipt['schema'],core.contracts.ACQUISITION_RECEIPT_SCHEMA_V2)
        self.assertEqual([receipt['batch'][k] for k in ('requests_total','requests_succeeded','requests_failed')],[8,7,1])
        self.assertEqual(receipt['attempts'][2]['response_sha256'],core.sha(b'upstream busy'))
        self.assertEqual(json.loads(capture['raw/transcript.json'])['records'][2],failed)
        core.verify_capture_files({k:v for k,v in capture.items() if k!='manifest.json'})

    def test_failed_retry_sequence_counter_delay_or_removed_attempt_fails(self):
        fail=self.record('structure_0000',b'busy',outcome='failed',delay=5,curl_exit_code=22,http_status=502)
        success=copy.deepcopy(self.records[0]);success['attempt']=2
        good=[fail,success,*self.records[1:]]
        for change in ({'attempt':3},{'retry_delay_seconds':0},{'outcome':'succeeded'}):
            records=copy.deepcopy(good);records[0].update(change)
            with self.assertRaises(core.EvidenceError): self.capture(records)
        with self.assertRaises(core.EvidenceError): self.capture(good[1:])
        with self.assertRaises(core.EvidenceError): self.capture(good[:1])

    def test_nontransient_partial200_or_exhausted_failure_is_never_retried(self):
        for status,code in [(200,18),(200,28),(404,22),(500,22),(0,60),(0,-9)]:
            self.assertIsNone(core.retry_delay(self.metadata(b'',http_status=status,curl_exit_code=code),1))
        self.assertIsNone(core.retry_delay(self.metadata(b'',http_status=503,curl_exit_code=22),5))
        self.assertEqual(core.retry_delay(self.metadata(b'',http_status=0,curl_exit_code=6),1),5)
        for after in ({'kind':'unsupported-or-excessive'},{'kind':'delay-seconds','seconds':121}):
            self.assertIsNone(core.retry_delay(self.metadata(b'',http_status=503,curl_exit_code=22,retry_after=after),1))

    def test_retry_after_parser_never_treats_present_invalid_values_as_absent(self):
        self.assertIsNone(cli.parsed_retry_after(b''))
        self.assertEqual(cli.parsed_retry_after(b'20'),{'kind':'delay-seconds','seconds':20})
        for raw in (b'9'*16,b'9'*129,b'-1',b'bad',b'Tue, 08 Sep 2026 12:00:00 GMT',b' '):
            self.assertEqual(cli.parsed_retry_after(raw),{'kind':'unsupported-or-excessive'})

    def test_response_and_transcript_budgets_include_failed_attempts(self):
        for key in ('maximum_total_response_bytes','maximum_transcript_bytes'):
            state=core.WalkState(self.plan,self.programs['brain/ingest/kerodon.py'])
            with mock.patch.dict(core.POLICY,{key:1}),self.assertRaisesRegex(core.EvidenceError,'budget'):state.accept(self.records[0])
        with mock.patch.dict(core.POLICY,{'maximum_response_bytes':1}):
            state=core.WalkState(self.plan,self.programs['brain/ingest/kerodon.py'])
            with self.assertRaisesRegex(core.EvidenceError,'bound'):state.accept(self.record('structure_0000',self.pages['structure_0000']))
        state=core.WalkState(self.plan,self.programs['brain/ingest/kerodon.py'])
        bad=self.record('structure_0000',b'busy',outcome='failed',delay=5,curl_exit_code=22,http_status=502)
        with mock.patch.dict(core.POLICY,{'maximum_total_response_bytes':3}),self.assertRaisesRegex(core.EvidenceError,'budget'):state.accept(bad)

    def test_acquirer_records_retry_and_uses_only_exact_complete_derived_scope(self):
        plan=self.root/'plan.json';plan.write_bytes(core.canonical(self.plan))
        failed=self.record('content_0000',b'busy',outcome='failed',delay=5,curl_exit_code=22,http_status=503)
        responses=iter((base64.b64decode(r['body_base64']),r['response']) for r in [*self.records[:2],failed,*self.records[2:]])
        urls=[]
        def fetch(params,_curl):urls.append(params['uri']);return next(responses)
        with mock.patch.object(cli,'require_startup'),mock.patch.object(cli,'runtime_identity',return_value=(self.tool,self.programs)), \
                mock.patch.object(cli,'transport',side_effect=fetch),mock.patch.object(cli.time,'sleep') as sleep,mock.patch.object(sys,'stderr',io.StringIO()):
            target=cli.acquire(plan,self.root/'captures',Path(sys.executable).resolve())
        files,_=core.verify_capture(target)
        self.assertEqual(json.loads(files['facts.json'])['failed_attempts'],1)
        self.assertEqual(urls,[r['request']['uri'] for r in [*self.records[:2],failed,*self.records[2:]]])
        self.assertIn(mock.call(5),sleep.call_args_list)

    def test_failed_html_stops_before_next_request_and_retains_no_receipt(self):
        plan=self.root/'plan.json';plan.write_bytes(core.canonical(self.plan))
        with mock.patch.object(cli,'require_startup'),mock.patch.object(cli,'runtime_identity',return_value=(self.tool,self.programs)), \
                mock.patch.object(cli,'transport',return_value=(b'error',self.metadata(b'error'))) as fetch,mock.patch.object(cli.time,'sleep'),self.assertRaises(core.EvidenceError):
            cli.acquire(plan,self.root/'captures',Path(sys.executable).resolve())
        self.assertEqual(fetch.call_count,1)
        self.assertFalse((self.root/'captures').exists())
        target=next((self.root/'captures-incomplete').iterdir())
        files,_=core.archive.read_bundle(target,'wikilean.kerodon-incomplete-attempt/v1')
        self.assertFalse(any('receipt' in name for name in files))
        self.assertEqual(base64.b64decode(json.loads(files['transcript.json'])['failed_request']['body_base64']),b'error')

    def test_profile_preimages_and_exact_endpoint_policy_fail_closed(self):
        for name in ('../0000','structure_0001','content_0000?x=1','content_abcd','content_00000'):
            with self.assertRaises(core.EvidenceError):core.parameters(name)
        for path in ('brain/ingest/kerodon.py','brain/mathlib_source_evidence.py'):
            changed=dict(self.programs);changed[path]+=b'\n# changed'
            with self.assertRaises(core.EvidenceError):core.verify_programs(self.profile,changed)
        with self.assertRaisesRegex(core.EvidenceError,'selector'):
            core.parser(self.programs['brain/ingest/kerodon.py'].replace(b'def flatten(',b'def changed('))

    def test_rehashed_export_still_fails_independent_replay(self):
        files=self.build();files['acquisition/requests/'+core.request(self.records[0]['request'])['parameters_sha256']+'.json']+=b' '
        files.pop('manifest.json')
        path=core.archive.publish(core.archive.manifest_files(files,core.EXPORT_SCHEMA),self.root/'changed',core.EXPORT_SCHEMA)
        with self.assertRaises(core.EvidenceError):core.verify_export(path)

    def test_actual_pipe_transport_uses_explicit_environment_and_retains_retry_header(self):
        raw=b'busy';script=self.root/'curl';invocation=self.root/'invocation.json'
        script.write_text('#!'+str(Path(sys.executable).resolve())+'\nimport json,os,sys\nfrom pathlib import Path\n'+
            'Path('+repr(str(invocation))+').write_text(json.dumps({"argv":sys.argv,"env":dict(os.environ)}))\n'+
            'sys.stdout.buffer.write('+repr(raw)+')\nsys.stderr.buffer.write('+repr(cli.TRAILER+b'503\ttext/html; charset=utf-8\t15\n')+')\nsys.exit(22)\n')
        script.chmod(0o700)
        with mock.patch.dict(os.environ,{'PRIVATE_TOKEN':'fixture','HTTP_PROXY':'http://invalid.example'}), \
                mock.patch.object(cli.subprocess,'Popen',wraps=subprocess.Popen) as launch:
            actual,metadata=cli.transport(core.parameters('structure_0000'),script)
        self.assertEqual(actual,raw);self.assertEqual(metadata['retry_after'],{'kind':'delay-seconds','seconds':15})
        self.assertEqual(launch.call_args.kwargs['env'],cli.ENVIRONMENT)
        args=json.loads(invocation.read_bytes())['argv'];self.assertEqual(args[1],'-q')
        for forbidden in ('--location','--retry','--netrc','--insecure','--user','--compressed'):self.assertNotIn(forbidden,args)
        self.assertEqual(args[args.index('--proxy')+1],'')
        self.assertIn('%header{retry-after}',args[args.index('--write-out')+1])

    def test_eof_then_lingering_transport_preserves_partial_received_bytes(self):
        script=self.root/'linger';script.write_text('#!'+str(Path(sys.executable).resolve())+'\nimport os,time\nos.write(1,b"partial")\nos.close(1);os.close(2)\ntime.sleep(30)\n');script.chmod(0o700)
        with mock.patch.object(cli,'PROCESS_EXIT_TIMEOUT_SECONDS',0.05),self.assertRaises(cli.TransportFailure) as failure:
            cli.transport(core.parameters('structure_0000'),script)
        self.assertEqual(failure.exception.raw,b'partial')
        self.assertLess(failure.exception.response['curl_exit_code'],0)

    def test_completeness_floors_fail_before_certification(self):
        with self.assertRaisesRegex(core.EvidenceError,'enumerated tags'):
            core.replay({**self.plan,'minimum_pages':6},self.records,self.programs)
        with self.assertRaisesRegex(core.EvidenceError,'completeness floors'):
            core.replay({**self.plan,'minimum_links':100},self.records,self.programs)

    def test_actual_v3_compiler_accepts_raw_identity_parent(self):
        import test_compile_offline_pack_v2 as fixture_module
        target = core.archive.publish(self.build(), self.root / "exports", core.EXPORT_SCHEMA)
        fragment = json.loads((target / "source-fragment.json").read_bytes())
        fixture = fixture_module.OfflinePackCompilerTest(); fixture.setUp(); self.addCleanup(fixture.tearDown)
        fixture._upgrade_plan_v3()
        source = fragment["sources"][0]
        raw = next(o for o in source["objects"] if o["name"] == "page_transcript")
        (fixture.external / "kerodon-transcript.json").write_bytes((target / raw["path"]).read_bytes())
        fixture.inventory["inputs"].append({"id": "kerodon-transcript", "class": "immutable_source_object", "cardinality": "one", "root": "external",
            "path": "kerodon-transcript.json", "consumers": ["brain/replay.py"], "purpose": "complete Kerodon raw evidence fixture", "requirement": "required"})
        fixture.inventory["inputs"].sort(key=lambda x: x["id"])
        fixture.inventory["inventory_id"] = core.contracts.reducer_input_inventory_identity(fixture.inventory)
        fixture.plan["inventory_id"] = fixture.inventory["inventory_id"]
        fixture.plan["sources"] = sorted([*fixture.plan["sources"], source], key=lambda x: x["source"])
        fixture.plan["input_bindings"].append({"input_id": "kerodon-transcript", "state": "present", "sources": [core.SOURCE],
            "members": [{"path": "kerodon-transcript.json", "source": core.SOURCE, "object": "page_transcript"}]})
        fixture.plan["input_bindings"].sort(key=lambda x: x["input_id"])
        fixture.inventory_path.write_bytes(core.canonical(fixture.inventory)); fixture.plan_path.write_bytes(core.canonical(fixture.plan))
        packed = fixture_module.compiler.compile_offline_pack_v2(fixture.plan_path.resolve(), fixture.inventory_path.resolve(),
            (fixture.base / "kerodon-pack").resolve(), roots={"repo": fixture.repo.resolve(), "external": fixture.external.resolve(), fragment["physical_root"]: target}, git_executable="/usr/bin/git")
        manifest, _ = core.contracts.load_canonical_json(packed.manifest_path)
        core.contracts.verify_offline_pack_files(manifest, packed.root, manifest_path=packed.manifest_path)


if __name__ == "__main__": unittest.main()
