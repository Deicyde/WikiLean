"""Private qualification controls over real v3 restricted-source fixture packs."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import stat
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE));sys.path.insert(0,str(HERE/'tools'))
import private_replay_gate as core
import test_source_policy_reviews as policy_fixture
import test_reproducibility_gate as technical_fixture

H='sha256:'+'a'*64
D='a'*64


class PrivateReplayGateTest(unittest.TestCase):
    def setUp(self):
        self.fixture=policy_fixture.SourcePolicyReviewsTest(methodName='runTest')
        self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.addCleanup(self.fixture.doCleanups)
        self.pack_path=self.fixture.pack_path
        self.root=Path(self.fixture.release_fixture.temp.name).resolve()
        self.controls=self.root/'gate-controls';self.controls.mkdir(mode=0o700)
        self.review=self.fixture.private()
        self.review_path=self.controls/'review.json';self.review_path.write_bytes(core.canonical(self.review))
        self.approval=technical_fixture.seal_approval()
        self.approval.update(schema=core.APPROVAL_SCHEMA,profile=core.PROFILE,
            offline_pack_id=self.fixture.pack['offline_pack_id'],source_set_root=self.fixture.pack['source_set_root'],
            reducer_inventory_id=self.fixture.pack['inventory']['inventory_id'],
            private_policy=core.policy_binding(self.review,self.review_path.read_bytes()))
        self.seal()
        self.approval_path=self.controls/'approval.json';self.approval_path.write_bytes(core.canonical(self.approval))

    def seal(self):
        self.approval['approval_id']=core.identity(core.APPROVAL_DOMAIN,self.approval,'approval_id')

    def validate_policy(self,review_path=None,attachments=None):
        return core.verify_policy(review_path or self.review_path,attachments or self.fixture.attachments,
            self.pack_path,self.approval,self.review['review_id'])

    def retain(self):
        store=self.root/'session';store.mkdir(mode=0o700)
        core.retain_policy(store,self.review,self.review_path.read_bytes(),self.fixture.attachments)
        return store

    def check_retained(self,store):
        return core.verify_retained_policy(store,self.pack_path,self.approval,self.review['review_id'])

    def args(self):
        return SimpleNamespace(approval=self.approval_path,expected_approval_id=self.approval['approval_id'],
            private_review=self.review_path,private_attachments=self.fixture.attachments,
            expected_private_review_id=self.review['review_id'],manifest=self.pack_path,root=self.pack_path.parent,
            baseline=self.fixture.release_path,destination=self.controls/'session',policy=self.controls/'runtime-policy.json',
            oci_layout=self.root/'image',wheelhouse=self.root/'wheels',docker=Path('/usr/bin/docker'),socket=self.root/'socket',
            docker_sha256=D,engine_id='engine',engine_version='1',stage_timeout_seconds=1200.0,timeout_seconds=3600.0,
            memory_bytes=1024**3,seed=123)

    def test_exact_private_review_accepts_restricted_pack_without_relabeling(self):
        before=self.pack_path.read_bytes()
        core.validate_approval(self.approval,self.approval['approval_id'])
        self.validate_policy()
        self.assertEqual(before,self.pack_path.read_bytes())
        self.assertTrue(all(s['original_license']['redistribution']=='restricted' for s in self.review['sources']))

    def test_pending_review_cannot_start_any_launcher(self):
        pending=core.reviews.draft_private(self.pack_path)
        self.review_path.write_bytes(core.canonical(pending))
        self.approval['private_policy']=core.policy_binding(pending,self.review_path.read_bytes());self.seal()
        self.approval_path.write_bytes(core.canonical(self.approval))
        args=self.args();args.expected_private_review_id=pending['review_id']
        with mock.patch.object(core.runner,'require_isolated_startup'),mock.patch.object(core.sys,'platform','linux'),mock.patch.object(core.launcher,'_bounded_process') as launch:
            with self.assertRaisesRegex(ValueError,'pending or rejected'):core.run_gate(args)
        launch.assert_not_called();self.assertFalse(args.destination.exists())

    def test_rejected_review_fails_even_with_self_consistent_expected_id(self):
        self.review['state']='rejected';self.review['review_id']=core.reviews.identity(self.review)
        self.review_path.write_bytes(core.canonical(self.review))
        self.approval['private_policy']=core.policy_binding(self.review,self.review_path.read_bytes());self.seal()
        with self.assertRaisesRegex(ValueError,'pending or rejected'):self.validate_policy()

    def test_independent_approval_and_policy_ids_are_mandatory(self):
        with self.assertRaisesRegex(ValueError,'independently expected'):core.validate_approval(self.approval,H)
        with self.assertRaisesRegex(ValueError,'policy ID differs'):
            core.verify_policy(self.review_path,self.fixture.attachments,self.pack_path,self.approval,H)
        with self.assertRaises(SystemExit):core.parser().parse_args(['finalize','--session','/tmp/a','--destination','/tmp/b','--coverage-mapping','/tmp/c'])

    def test_source_omission_cannot_be_resealed_as_complete(self):
        self.review['sources'].pop();self.review['review_id']=core.reviews.identity(self.review)
        self.review_path.write_bytes(core.canonical(self.review))
        self.approval['private_policy']=core.policy_binding(self.review,self.review_path.read_bytes());self.seal()
        with self.assertRaisesRegex(ValueError,'every source'):self.validate_policy()

    def test_policy_pack_mismatch_is_rejected(self):
        self.approval['offline_pack_id']=H;self.seal()
        with self.assertRaisesRegex(ValueError,'policy pack'):self.validate_policy()

    def test_review_digest_is_not_replaced_by_its_self_claimed_id(self):
        self.approval['private_policy']['review_sha256']=D;self.seal()
        with self.assertRaisesRegex(ValueError,'review bytes'):self.validate_policy()

    def test_private_policy_retention_is_exact_and_independent_of_later_original_edit(self):
        store=self.retain();self.check_retained(store)
        (self.fixture.attachments/'reviewed-license.txt').write_bytes(b'original changed after copying')
        self.check_retained(store)
        self.assertEqual(stat.S_IMODE((store/'private-policy/review.json').stat().st_mode),0o400)

    def test_nested_attachment_paths_are_private_under_normal_umask(self):
        self.fixture.evidence['path']='a/b/license.txt'
        directory=self.fixture.attachments/'a/b';directory.mkdir(parents=True)
        (directory/'license.txt').write_bytes((self.fixture.attachments/'reviewed-license.txt').read_bytes())
        self.fixture.evidence['evidence_id']=core.reviews.evidence_id(self.fixture.evidence)
        self.review=self.fixture.private();self.review_path.write_bytes(core.canonical(self.review))
        self.approval['private_policy']=core.policy_binding(self.review,self.review_path.read_bytes());self.seal()
        previous=os.umask(0o022)
        try:store=self.retain()
        finally:os.umask(previous)
        self.check_retained(store)
        self.assertEqual(stat.S_IMODE((store/'private-policy/attachments/a').stat().st_mode),0o700)

    def test_retained_attachment_tampering_and_extra_empty_directory_fail(self):
        store=self.retain();extra=store/'private-policy/attachments/extra';extra.mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError,'extra entries'):self.check_retained(store)
        extra.rmdir()
        path=store/'private-policy/attachments/reviewed-license.txt'
        path.chmod(0o600);path.write_bytes(b'different');path.chmod(0o400)
        with self.assertRaisesRegex(ValueError,'attachment bytes differ'):self.check_retained(store)

    def test_retained_symlink_and_writable_mode_fail(self):
        store=self.retain();path=store/'private-policy/attachments/reviewed-license.txt'
        path.chmod(0o600)
        with self.assertRaisesRegex(ValueError,'mode0400'):self.check_retained(store)
        path.unlink();path.symlink_to(self.fixture.attachments/'reviewed-license.txt')
        with self.assertRaises((OSError,ValueError)):self.check_retained(store)

    def run_fixture(self,after_first=None,finalize=False,during_coverage=None):
        args=self.args();args.policy.write_bytes(core.canonical({}))
        for path in (args.oci_layout,args.wheelhouse):path.mkdir(mode=0o700)
        contexts={};calls=[]
        measured={'entries':[],'output_root':H,'base_snapshot_id':D,'projection_id':D,'semantic_state_root':H}
        compatibility={'mode':'exact','report_sha256':core.sha(b'{}'),'graph_topology_content':'equal','provenance':'equal'}
        def prepare(manifest,workspace,**kwargs):
            (workspace/'output').mkdir(mode=0o700,parents=True)
            context=SimpleNamespace(roots=SimpleNamespace(output=workspace/'output'),generation_id=H)
            path=workspace/'build-context.json';path.write_bytes(b'{}');contexts[str(path)]=context
            return SimpleNamespace(context_path=path,generation_id=H)
        def launch(command,**kwargs):
            path=Path(command[command.index('--receipt')+1]);record={'container_id':str(len(calls)+1)*64}
            data=core.canonical(record);core.write_new(path,data);calls.append(command)
            if len(calls)==1 and after_first:after_first(args)
            return 0,data+b'\n',b''
        def freeze(**kwargs):
            store=kwargs['output_store'];store.mkdir(mode=0o700)
            manifest=store/'release.json';manifest.write_bytes(core.canonical({'release_id':H}))
            return {'manifest':str(manifest)}
        with ExitStack() as stack:
            for target,name,value in ((core.runner,'require_isolated_startup',lambda:None),
                (core,'verified_baseline',lambda *a: {}),(core,'verified_pack',lambda *a:(self.fixture.pack,{'environment_id':H,'runtime':{'manifest_digest':H}})),
                (core.oci_runtime,'verify_image',lambda *a: object()),(core.preparation,'prepare_replay_v2',prepare),
                (core.runner.build_context.BuildContext,'load',lambda p:contexts[str(p)]),
                (core.build_replay_release,'verify_context',lambda *a:None),(core,'verify_context',lambda *a:None),
                (core,'randomize_mtimes',lambda w,s,**k:{'seed':s}),(core,'hostile_environment',lambda *a,**k:{}),
                (core.launcher,'_bounded_process',launch),(core,'verify_launch_record',lambda *a,**k:None),
                (core,'measure_output',lambda *a,**k:measured),(core.build_replay_release,'freeze_replayed_output',freeze),
                (core,'verified_pack_release',lambda *a,**k:{'release_id':H}),(core,'comparison_report',lambda *a:{}),
                (core,'report_bytes',lambda *a:b'{}'),(core,'verify_compatibility',lambda *a:compatibility),
                (core,'capture_tree',lambda *a:[]),(core.contracts,'verify_release_files',lambda *a:None)):
                stack.enter_context(mock.patch.object(target,name,side_effect=value))
            stack.enter_context(mock.patch.object(core.sys,'platform','linux'))
            # No accepted authority record is created: this exercises only the
            # outer ownership/control flow with synthetic technical witnesses.
            result=core.run_gate(args)
            if finalize:
                final_args=SimpleNamespace(session=args.destination/'session.json',expected_session_id=result['session_id'],
                    expected_private_review_id=self.review['review_id'],coverage_mapping=self.controls/'mapping.json',
                    expected_coverage_mapping_id=H,destination=self.controls/'qualified')
                def coverage(*a):
                    if during_coverage:during_coverage(args)
                    return b'{}',{'report_id':H,'provenance_coverage_ready':True},coverage_implementation
                coverage_files=[{'path':'brain/tools/'+name,'sha256':D,'bytes':123} for name in ('provenance_coverage.py','provenance_coverage_families.py')]
                coverage_implementation={'files':coverage_files,'root':core.contracts.domain_hash('private-replay-coverage-implementation/v1',coverage_files)}
                with mock.patch.object(core,'checked_coverage',side_effect=coverage),mock.patch.object(core,'coverage_implementation',return_value=(None,coverage_implementation)):
                    result=core.finalize(final_args)
        return result,calls,args

    def test_two_owned_launches_and_no_imported_success_interface(self):
        session,calls,args=self.run_fixture()
        self.assertEqual(len(calls),2)
        self.assertEqual(session['profile'],core.PROFILE)
        self.assertNotEqual(session['builds'][0]['container_id'],session['builds'][1]['container_id'])
        self.assertEqual(session['private_policy'],self.approval['private_policy'])
        self.assertFalse(session['limits']['accepted_authority'])
        self.assertEqual(core.identity(core.SESSION_DOMAIN,session,'session_id'),session['session_id'])
        self.assertTrue((args.destination/'private-policy/review.json').exists())
        with self.assertRaises(SystemExit):core.parser().parse_args(['import-success','/tmp/summary.json'])

    def test_changed_policy_after_first_owned_launch_stops_second(self):
        def mutate(args):
            path=args.destination/'private-policy/attachments/reviewed-license.txt'
            path.chmod(0o600);path.write_bytes(b'changed');path.chmod(0o400)
        with self.assertRaisesRegex(ValueError,'attachment bytes differ'):
            self.run_fixture(after_first=mutate)
        failed=json.loads((self.controls/'session/failure.json').read_bytes())
        self.assertEqual(failed['completed_builds'],1)
        self.assertFalse((self.controls/'session/second-with-a-different-path-length').exists())
        self.assertFalse((self.controls/'session/session.json').exists())

    def test_finalization_rechecks_owned_outputs_and_has_only_private_scope(self):
        result,calls,args=self.run_fixture(finalize=True)
        core.validate_attestation(result)
        self.assertEqual(len(calls),2)
        self.assertFalse(result['private_replay_qualified'])
        self.assertEqual(result['scope'],'fixture')
        self.assertTrue(all(value is False for value in result['limits'].values()))
        self.assertTrue((self.controls/'qualified/qualification.json').exists())
        with self.assertRaisesRegex(ValueError,'unexpected fields'):core.technical.validate_attestation(result)
        changed=copy.deepcopy(result);changed['limits']['public_release_policy_ready']=True
        changed['attestation_id']=core.identity(core.ATTESTATION_DOMAIN,changed,'attestation_id')
        with self.assertRaisesRegex(ValueError,'exceeds private'):core.validate_attestation(changed)

    def test_policy_mutation_during_coverage_prevents_final_attestation(self):
        def mutate(args):
            path=args.destination/'private-policy/review.json'
            path.chmod(0o600);path.write_bytes(b'{}');path.chmod(0o400)
        with self.assertRaises(ValueError):self.run_fixture(finalize=True,during_coverage=mutate)
        self.assertFalse((self.controls/'qualified').exists())

    def test_approval_mutation_during_coverage_prevents_final_attestation(self):
        def mutate(args):
            path=args.destination/'approval.json'
            path.chmod(0o600);path.write_bytes(b'{}');path.chmod(0o400)
        with self.assertRaisesRegex(ValueError,'retained approval or runtime'):
            self.run_fixture(finalize=True,during_coverage=mutate)
        self.assertFalse((self.controls/'qualified').exists())

    def test_whole_helper_mutation_after_first_launch_prevents_second(self):
        original=core.implementation;changed=[False]
        def fingerprint():
            result=original()
            if changed[0]:result[0]['sha256']='b'*64
            return result
        with mock.patch.object(core,'implementation',side_effect=fingerprint):
            with self.assertRaisesRegex(ValueError,'implementation changed before launch'):
                self.run_fixture(after_first=lambda args:changed.__setitem__(0,True))
        self.assertFalse((self.controls/'session/session.json').exists())

    def test_disk_generation_differing_from_loaded_tuple_fails_before_first_launch(self):
        original=core.environment.secure_file_digest
        target=HERE/'tools/source_policy_reviews.py'
        def changed(path):
            result=original(path)
            return ('b'*64,result[1]) if path==target else result
        with mock.patch.object(core.environment,'secure_file_digest',side_effect=changed),mock.patch.object(core.launcher,'_bounded_process') as launch:
            # Even a newly measured disk tuple and freshly valid approval cannot
            # relabel previously imported helper code as the new generation.
            self.assertNotEqual(tuple((i['path'],i['sha256']) for i in core.measured_implementation()),core.LOADED_IMPLEMENTATION)
            core.validate_approval(self.approval,self.approval['approval_id'])
            with self.assertRaisesRegex(ValueError,'loaded implementation'):core.run_gate(self.args())
        launch.assert_not_called()

    def test_coverage_closure_cannot_omit_the_family_helper(self):
        files=[{'path':'brain/tools/provenance_coverage.py','sha256':D,'bytes':123}]
        value={'files':files,'root':core.contracts.domain_hash('private-replay-coverage-implementation/v1',files)}
        with self.assertRaises(ValueError):core.validate_coverage_implementation(value)

    def test_missing_coverage_integration_never_becomes_a_pass(self):
        with mock.patch.object(core.importlib,'import_module',side_effect=ImportError('pending')):
            with self.assertRaisesRegex(ValueError,'remains pending'):
                core.checked_coverage(SimpleNamespace(),self.pack_path,self.fixture.release_path,self.review,self.fixture.attachments)

    def test_v1_contract_does_not_accept_new_profile_fields(self):
        old=technical_fixture.seal_approval();core.technical.validate_approval(old)
        old['private_policy']=self.approval['private_policy']
        with self.assertRaisesRegex(ValueError,'unexpected fields'):core.technical.validate_approval(old)
        with self.assertRaisesRegex(ValueError,'unsupported private'):
            wrong=copy.deepcopy(self.approval);wrong['profile']='brain-current-v1';core.validate_approval(wrong,H)


class RealCoverageHandshakeTest(unittest.TestCase):
    def setUp(self):
        import test_provenance_coverage as fixture
        self.fixture=fixture.CoverageIntegrationTest(methodName='runTest')
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.mapping_path=self.fixture.attachments.parent/'coverage-mapping.json'
        self.mapping_path.write_bytes(core.canonical(self.fixture.mapping))
        self.args=SimpleNamespace(coverage_mapping=self.mapping_path,
            expected_coverage_mapping_id=self.fixture.mapping['mapping_id'],
            expected_private_review_id=self.fixture.private['review_id'])

    def checked(self):
        return core.checked_coverage(self.args,self.fixture.pack_path,self.fixture.release_path,
            self.fixture.private,self.fixture.attachments)

    def test_real_loaded_closure_and_full_pack_release_report_handshake(self):
        coverage,implementation=core.coverage_implementation()
        self.assertNotEqual(list(coverage.IMPLEMENTATION_PATHS),sorted(coverage.IMPLEMENTATION_PATHS))
        self.assertEqual([item['path'] for item in implementation['files']],sorted(coverage.IMPLEMENTATION_PATHS))
        raw,report,measured=self.checked()
        self.assertEqual(raw,self.mapping_path.read_bytes());self.assertEqual(measured,implementation)
        self.assertEqual(report['implementation_root'],coverage.implementation_root())
        self.assertEqual(report['occurrences']['total'],4)
        self.assertTrue(report['provenance_coverage_ready'])

    def test_real_pending_mapping_cannot_qualify(self):
        coverage,_=core.coverage_implementation()
        pending=copy.deepcopy(self.fixture.mapping);pending.update(state='pending',reviewer=None)
        pending['mapping_id']=coverage.identity(pending)
        self.mapping_path.write_bytes(core.canonical(pending));self.args.expected_coverage_mapping_id=pending['mapping_id']
        with self.assertRaisesRegex(ValueError,'coverage remains unresolved'):self.checked()

    def test_real_family_loaded_origin_drift_is_rejected(self):
        coverage,_=core.coverage_implementation()
        with mock.patch.object(coverage.families.__spec__,'origin','/tmp/unreviewed-family.py'):
            with self.assertRaisesRegex(ValueError,'module origin differs'):self.checked()


if __name__=='__main__':unittest.main()
