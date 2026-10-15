#!/usr/bin/env python3
"""Offline native Literature Links storage, producer, rendering, moderation and drift checks."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault('WIKILEAN_SEARCH_TOOLS', '0')
import native_annotations as n
import render
import moderate
from migrate_native import migrate_file, _publish_new

TITLE = 'Synthetic Article'
URI = n.wikipedia_source_uri(TITLE, 42)
HTML = '<h2><span id="Statement">Statement</span></h2><p>A finite group has an identity.</p>'


def occurrence(snippet='A finite group', **extras):
    return {'source': {'type': 'generic', 'uri': URI},
            'location': {'profile': n.ANCHOR_PROFILE, 'anchor': {'section': 'Statement', 'snippet': snippet},
                         'resolution': 'not_attempted'}, **extras}


def item(**extras):
    return {'id': 'aabbccddeeff', 'status': 'formalized', 'provenance': 'ai', 'label': 'Fixture',
            'links': [{'id': 'association:1', 'annotation': {**occurrence(), 'target': 'https://formal.example/Groups.html#identity'}}],
            'unlinked': [], **extras}


def old_item(**extras):
    return {'id': 'aabbccddeeff', 'status': 'formalized', 'provenance': 'ai',
            'anchor': {'section': 'Statement', 'snippet': 'A finite group'},
            'mathlib': {'module': 'Mathlib.Group', 'decl': 'Group.identity', 'match_kind': 'exact'},
            'note': 'review', **extras}


class NativeContract(unittest.TestCase):
    def test_strict_native_json_rejects_duplicate_keys_and_nonfinite_numbers(self):
        import batch_annotate as ba
        for payload in ('{"annotations":[],"annotations":[]}',
                        '{"annotations":[{"links":[{"id":"a","id":"b"}]}]}',
                        '{"value":1e999}', '{"value":NaN}'):
            with self.subTest(payload=payload):
                with self.assertRaises(n.NativeAnnotationError): n.strict_loads(payload)
                self.assertIsNone(ba.parse_json_object(payload))
        self.assertEqual(n.strict_loads('{"left":{"id":"a"},"right":{"id":"b"}}'),
                         {'left':{'id':'a'},'right':{'id':'b'}})

    def test_native_all_modes_and_unknown_values_survive(self):
        locations = {'pdf': {'page': 1, 'rect': [0, 0, 1, 1]}, 'latex': {'lines': [1, 2]},
                     'html': {'fragment': 'α'}, 'generic': {'unknown': {'list': [None, True, 1.25]}}}
        work = item()
        work['links'] = [{'id': mode, 'base_uri': 'file:///tmp/shards/one.json',
                          'annotation': {'source': {'type': mode, 'uri': '../source', 'extensions': {'owner': {'unknown': 5}}},
                                         'target': '../formal#target', 'id': 'same-record-id', 'location': loc,
                                         'extensions': {'custom': {'z': [3, 2, 1]}}}}
                         for mode, loc in locations.items()]
        before = deepcopy(work)
        n.validate_items([work])
        self.assertEqual(n.preserve_native_items([work], [deepcopy(work)]), [before])
        self.assertEqual(work, before)

    def test_core_version_null_locations_and_numeric_invariants_rejected(self):
        for patcher in (lambda a: a.update(version=2), lambda a: a.update(location=None),
                        lambda a: a.update(source={'type': 'latex', 'uri': URI}, location={'lines': [9, 2]}),
                        lambda a: a.update(source={'type': 'pdf', 'uri': URI}, location={'page': 1, 'rect': [.9, 0, .2, 1]})):
            work = item(); patcher(work['links'][0]['annotation'])
            with self.subTest(work=work), self.assertRaises(n.NativeAnnotationError):
                n.validate_items([work])

    def test_profile_locations_and_double_sum_rectangles(self):
        record = lambda mode, location: {'source': {'type': mode, 'uri': URI}, 'target': 'urn:x', 'location': location}
        # The bound is a double sum, so a width computed as 1 - x fits.
        n.validate_link_record(record('pdf', {'page': 1, 'rect': [.9, 0, .10000000000000002, 1]}))
        # A profile makes the location application-defined in every mode.
        n.validate_link_record(record('pdf', {'profile': 'urn:x', 'rect': [.9, 0, .2, 1]}))
        n.validate_link_record(record('latex', {'profile': 'urn:x', 'lines': [9, 2]}))
        n.validate_link_record(record('pdf', {'page': 1, 'quote': {'exact': 'a group'}}))
        with self.assertRaises(n.NativeAnnotationError):
            n.validate_link_record(record('pdf', {'page': 1, 'rect': [0, 0, 1, 1], 'quote': {'exact': 'a group'}}))

    def test_normalize_gives_one_shape(self):
        value = {'source': {'type': 'html', 'uri': URI}, 'target': 'urn:x', 'status': 'partial'}
        self.assertEqual(n.normalize(value), {'source': value['source'], 'target': {'uri': 'urn:x'}, 'status': {'value': 'partial'}})
        self.assertEqual(n.normalize(n.normalize(value)), n.normalize(value))
        try:
            from literature_links.companion import normalize
        except ImportError:
            return
        self.assertEqual(n.normalize(value), normalize(value))

    def test_native_shape_internal_identity_and_base_are_checked(self):
        broken = []
        for key in ('links', 'unlinked'):
            work = item(); del work[key]; broken.append(work)
        work=item(anchor={}); broken.append(work)
        work=item(); work['links'].append(deepcopy(work['links'][0])); broken.append(work)
        work=item(); work['links'][0]['annotation']['target']='../formal'; broken.append(work)
        work=item(); work['links'][0]['annotation']['target']={'uri': '../formal'}; broken.append(work)
        work=item(); work['links'][0]['annotation']['target']={}; broken.append(work)
        work=item(); del work['links'][0]['annotation']['target']; broken.append(work)
        work=item(); work['links'][0]['annotation']['extensions']={'exact': 2**53}; broken.append(work)
        for work in broken:
            with self.subTest(work=work), self.assertRaises(n.NativeAnnotationError): n.validate_items([work])

    def test_endpoint_targets_pins_and_facts_validate_and_project(self):
        work = item(kind='theorem', note='why', match_kind='exact')
        pinned = {'uri': 'https://formal.example/Groups.html#identity',
                  'pin': {'type': 'repository', 'url': n.MATHLIB_REPOSITORY, 'module': 'Mathlib.Group', 'declaration': 'Group.identity'}}
        work['links'][0]['annotation']['target'] = pinned
        foreign = {'id': 'their-id', 'label': 'Their label', 'source': {'type': 'pdf', 'uri': '../paper.pdf'},
                   'target': {'uri': 'urn:formal:b'}, 'status': {'pin': {'type': 'repository', 'url': n.MATHLIB_REPOSITORY}},
                   'extensions': {'org.example': {'kept': True}, 'org.wikilean': {'imported': 1}}}
        work['links'].append({'id': 'foreign', 'base_uri': 'https://example.net/shards/a.json', 'annotation': foreign})
        before = deepcopy(work)
        n.validate_items([work], title=TITLE, revid=42)
        first = n.companion_annotation(work, work['links'][0], title=TITLE, revid=42, wikidata_qid='Q1')
        self.assertEqual(first, {'id': 'aabbccddeeff', 'kind': 'theorem', 'label': 'Fixture', 'status': 'formalized',
            'source': {'type': 'generic', 'uri': URI, 'pin': {'type': 'website', 'title': TITLE, 'owner': 'Wikipedia', 'url': URI,
                                                              'revision': '42', 'identifiers': {'wikidata': 'Q1'}}},
            'location': occurrence()['location'], 'target': pinned, 'note': 'why',
            'extensions': {'org.wikilean': {'provenance': 'ai', 'match_kind': 'exact'}}})
        # A foreign source gets no article pin; stored id, label, extensions and the status pin survive.
        second = n.companion_annotation(work, work['links'][1], title=TITLE, revid=42)
        self.assertEqual(second, {'id': 'their-id', 'kind': 'theorem', 'label': 'Their label',
            'status': {'value': 'formalized', 'pin': {'type': 'repository', 'url': n.MATHLIB_REPOSITORY}},
            'source': foreign['source'], 'target': foreign['target'], 'note': 'why',
            'extensions': {'org.example': {'kept': True}, 'org.wikilean': {'imported': 1, 'provenance': 'ai', 'match_kind': 'exact'}}})
        for record in (first, second):
            n.validate_link_record(record)
        self.assertEqual(work, before)
        self.assertEqual(n.formal_links(work)[0]['label'], 'Group.identity')
        self.assertEqual(n.first_target(work), pinned['uri'])
        self.assertNotIn('pin', n.companion_annotation({'status': 'partial', 'links': [], 'unlinked': []}, work['links'][0])['source'])
        n.validate_link_record({'source': {'type': 'html', 'uri': URI}, 'status': 'not_formalized'})

    def test_opaque_extension_fields_survive_agent_partial_updates(self):
        original = item(private={'audit': [1, 2]})
        original['links'][0]['annotation']['extensions'] = {'custom': {'opaque': 1}}
        original['links'][0]['annotation']['source']['extensions'] = {'source': ['untouched']}
        produced = item(note='edited')
        restored = n.preserve_native_items([original], [produced])[0]
        self.assertEqual(restored['private'], original['private'])
        self.assertEqual(restored['links'], original['links'])
        self.assertEqual(restored['note'], 'edited')

    def test_native_to_legacy_or_dropped_link_is_never_a_roundtrip(self):
        for output in ([old_item()], [item(links=[])], []):
            with self.subTest(output=output), self.assertRaises(n.NativeAnnotationError):
                n.preserve_native_items([item()], output)

    def test_unlinked_selection_and_idless_items_cannot_silently_disappear(self):
        old = item(links=[], unlinked=[occurrence()]); del old['id']
        with self.assertRaises(n.NativeAnnotationError): n.preserve_native_items([old], [])
        self.assertEqual(n.preserve_native_items([old], [item(links=[],unlinked=[occurrence()])])[0]['id'],'aabbccddeeff')
        old = item(links=[], unlinked=[occurrence()])
        with self.assertRaises(n.NativeAnnotationError): n.preserve_native_items([old], [item(links=[])])
        promoted = item()
        self.assertEqual(n.preserve_native_items([old], [promoted])[0]['links'], promoted['links'])
        old['unlinked'][0]['location']['opaque'] = {'keep': 1}
        with self.assertRaises(n.NativeAnnotationError): n.preserve_native_items([old], [promoted])

    def test_native_signature_number_codec_and_key_order(self):
        self.assertEqual(n.stable_native([1, 1.0, -0.0, 1e-7]),
                         '[#3ff0000000000000,#3ff0000000000000,#0000000000000000,#3e7ad7f29abcaf48]')
        self.assertEqual(n.stable_native({'😀': 1, '\uffff': 2}), '{"\uffff":#4000000000000000,"😀":#3ff0000000000000}')

    def test_relative_source_pin_and_schema_hint_application_constraints(self):
        work=item(); link=work['links'][0]
        link['base_uri']='https://en.wikipedia.org/w/records.json'
        link['annotation']['source']['uri']='index.php?title=Synthetic_Article&oldid=41'
        with self.assertRaises(n.NativeAnnotationError): n.validate_items([work],title=TITLE,revid=42)
        work['status']='rejected'
        n.validate_items([work],title=TITLE,revid=42)
        work=item(); work['links'][0]['annotation']['$schema']='./schema.json'
        with self.assertRaises(n.NativeAnnotationError): n.validate_items([work])
        work=item(private={'integer':2**53})
        with self.assertRaises(n.NativeAnnotationError): n.validate_items([work])

    def test_human_native_tombstones_restored_verbatim(self):
        human = item(provenance='human', status='rejected', legacy={'secret': 'private'})
        posted, _ = moderate.finalize_for_post([human], [])
        self.assertEqual(posted, [human])
        changed = deepcopy(human); changed['links'][0]['annotation']['target']='urn:changed'
        posted, _ = moderate.finalize_for_post([human], [changed])
        self.assertEqual(posted, [human])

    def test_veto_adjacency_checks_every_native_occurrence_and_its_source(self):
        tomb=item(status='rejected',provenance='human',links=[],unlinked=[occurrence('Different premise'),occurrence()])
        proposal=item(id='001122334455',links=[],unlinked=[occurrence('Irrelevant prose'),occurrence('A finite group has identity')])
        self.assertTrue(moderate._veto_adjacent(proposal,[tomb]))
        for span in proposal['unlinked']: span['source']['uri']='https://other.example/book'
        self.assertFalse(moderate._veto_adjacent(proposal,[tomb]))

    def test_native_anchor_signature_uses_raw_source_location_and_base(self):
        first = item(); second = deepcopy(first)
        second['links'][0]['annotation']['extensions']={'unrelated': 1}
        self.assertEqual(n.anchor_sig(first), n.anchor_sig(second))
        second['links'][0]['base_uri']='file:///different/base'
        self.assertNotEqual(n.anchor_sig(first), n.anchor_sig(second))
        self.assertEqual(n.anchor_sig(item(links=[])), '["native",null,null,null]')
        self.assertNotEqual(n.anchor_sig(first), n.anchor_sig(old_item()))


class Migration(unittest.TestCase):
    def test_one_link_per_anchor_and_exact_private_archive(self):
        old=old_item(anchors=[{'section':'Statement','snippet':'A finite group'}, {'type':'math_alttext','value':'x'}],
                     unknown={'nested': [1, False]}, proof_note='private')
        before=deepcopy(old)
        result, diagnostics=n.migrate_items([old], title=TITLE, revid=42)
        converted=result[0]
        self.assertEqual(diagnostics, [])
        self.assertEqual(converted['legacy'], before)
        self.assertEqual(old, before)
        self.assertFalse(n.LEGACY_FIELDS & converted.keys())
        self.assertEqual(converted['match_kind'], 'exact')
        self.assertEqual({link['annotation']['source']['type'] for link in converted['links']}, {'html'})
        # Records written before the profile form used generic sources; both read the same.
        generic = deepcopy(converted); generic['links'][0]['annotation']['source']['type'] = 'generic'
        self.assertEqual(n.anchors(generic), n.anchors(converted))
        self.assertEqual([link['id'] for link in converted['links']], ['aabbccddeeff:link:0','aabbccddeeff:link:1'])
        self.assertEqual(converted['links'][0]['annotation']['target'], {
            'uri': 'https://leanprover-community.github.io/mathlib4_docs/Mathlib/Group.html#Group.identity',
            'pin': {'type': 'repository', 'url': n.MATHLIB_REPOSITORY, 'module': 'Mathlib.Group', 'declaration': 'Group.identity'}})
        again, diagnostics=n.migrate_items(result, title=TITLE, revid=42)
        self.assertEqual(again, result); self.assertEqual(diagnostics, [])

    def test_only_absent_anchor_is_missing_and_explicit_null_is_invalid(self):
        for value in ({},None,[]):
            with self.subTest(anchor=value):
                converted,diagnostics=n.migrate_items([old_item(anchor=value)],title=TITLE,revid=42)
                self.assertNotIn('MISSING_ANCHOR',[entry['code'] for entry in diagnostics])
                self.assertEqual(len(converted[0]['links']),int(isinstance(value,dict)))
                self.assertEqual('INVALID_ANCHOR' in [entry['code'] for entry in diagnostics],not isinstance(value,dict))

    def test_targetless_conflicting_and_invalid_anchors_are_preserved(self):
        for old, links, code in ((old_item(mathlib=None), 0, 'NO_FORMAL_TARGET'),
                                  (old_item(status='not_formalized'), 1, 'STATUS_TARGET_CONFLICT'),
                                  (old_item(anchor={'snippet': 9}), 0, 'INVALID_ANCHOR')):
            with self.subTest(old=old):
                converted, diagnostics=n.migrate_items([old], title=TITLE, revid=42)
                self.assertEqual(len(converted[0]['links']), links)
                self.assertIn(code, [entry['code'] for entry in diagnostics])
                self.assertEqual(converted[0]['legacy'], old)

    def test_unknown_anchor_fields_stay_private_and_missing_is_not_whole_source(self):
        old=old_item(anchor={'section':'Statement','snippet':'A finite group','private':'secret'})
        converted,_=n.migrate_items([old],title=TITLE,revid=42)
        self.assertNotIn('private', converted[0]['links'][0]['annotation']['location']['anchor'])
        old=old_item(); del old['anchor']
        converted, diagnostics=n.migrate_items([old],title=TITLE,revid=42)
        self.assertEqual(converted[0]['links'][0]['annotation']['location']['anchor'], {})
        self.assertIn('MISSING_ANCHOR',[entry['code'] for entry in diagnostics])

    def test_explicit_json_bundle_archives_bytes_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'old.json'; output=Path(tmp)/'converted'
            original=json.dumps({'wikipedia_title':TITLE,'annotations':[old_item()]},indent=3).encode()
            source.write_bytes(original)
            with self.assertRaises(n.NativeAnnotationError): migrate_file(source, output)
            report=migrate_file(source,output,revid=42)
            self.assertEqual((output/'original.json').read_bytes(),original)
            self.assertEqual(json.loads((output/'article.json').read_text())['schema_version'],4)
            self.assertFalse(report['d1_updated'])
            with self.assertRaises(n.NativeAnnotationError): migrate_file(source,output,revid=42)

    def test_bundle_publication_refuses_even_a_racing_empty_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'staged'; destination=Path(tmp)/'racing'
            source.mkdir(); destination.mkdir()
            (source/'original.json').write_text('kept')
            with self.assertRaises(OSError): _publish_new(source,destination)
            self.assertEqual(list(destination.iterdir()),[])
            self.assertEqual((source/'original.json').read_text(),'kept')


class Rendering(unittest.TestCase):
    def test_only_the_matching_source_renders(self):
        for uri, expected in ((None,False),('https://other.example/',False),(URI,True)):
            wrapped, matched=render.wrap_annotations(HTML,[item()],source_uri=uri)
            self.assertEqual(matched,[expected]); self.assertEqual('data-anno-indices' in wrapped,expected)

    def test_every_native_occurrence_must_match_and_duplicates_wrap_once(self):
        work=item(); duplicate=deepcopy(work['links'][0]); duplicate['id']='association:2'
        work['links'].append(duplicate)
        wrapped, matched=render.wrap_annotations(HTML,[work],source_uri=URI)
        self.assertEqual(matched,[True]); self.assertIn('data-anno-indices="0"',wrapped)
        work['unlinked']=[occurrence('missing phrase')]
        self.assertEqual(render.wrap_annotations(HTML,[work],source_uri=URI)[1],[False])

    def test_absent_location_selects_whole_resource_but_empty_does_not(self):
        work=item(); del work['links'][0]['annotation']['location']
        self.assertEqual(render.wrap_annotations(HTML,[work],source_uri=URI)[1],[True])
        work['links'][0]['annotation']['location']={}
        self.assertEqual(render.wrap_annotations(HTML,[work],source_uri=URI)[1],[False])

    def test_unknown_modes_and_malformed_generic_locations_remain_unresolved(self):
        for mode,loc in (('pdf',{'page':1}),('latex',{'label':'x'}),('html',{'css':'#x'}),
                         ('generic',{'profile':n.ANCHOR_PROFILE,'anchor':{'type':'math_alttext','value':7}})):
            work=item(); work['links'][0]['annotation'].update(source={'type':mode,'uri':URI},location=loc)
            before=deepcopy(work)
            self.assertEqual(render.wrap_annotations(HTML,[work],source_uri=URI)[1],[False])
            self.assertEqual(work,before)

    def test_public_links_are_safe_and_private_review_data_is_absent(self):
        work=item(note='a public note',proof_note='secret',legacy={'secret':True},private='secret')
        work['links'] += [{'id':'bad','annotation':{**occurrence(),'target':'javascript:alert(1)'}},
                          {'id':'credentials','annotation':{**occurrence(),'target':'https://user:pass@example.org/'}},
                          {'id':'relative','annotation':{**occurrence(),'target':'../formal#α'},'base_uri':'https://example.org/shards/one.json'}]
        public=render.build_client_data([work,item(status='rejected')])
        self.assertIsNone(public[1]); self.assertNotIn('secret',json.dumps(public))
        self.assertEqual(public[0]['note'],'a public note')
        self.assertEqual(public[0]['formal_links'][0]['label'],'identity')
        self.assertNotIn('url',public[0]['formal_links'][1]); self.assertNotIn('url',public[0]['formal_links'][2])
        self.assertEqual(public[0]['formal_links'][3]['url'],'https://example.org/formal#%CE%B1')


class Pipeline(unittest.TestCase):
    def test_native_mocked_agent_pipeline_never_roundtrips_legacy(self):
        import batch_annotate as ba
        first=item(status='not_formalized',links=[],unlinked=[occurrence()],private={'opaque':True})
        final=item(private={'opaque':True})
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)
            (directory/'Synthetic_Article.42.sections.json').write_text(json.dumps({'sections':[]}))
            (directory/'Synthetic_Article.42.html').write_text(HTML)
            fake=AsyncMock(side_effect=[({'annotations':[first]},{}),({'annotations':[final]}, {})])
            with patch.object(ba,'CACHE',directory),patch.object(ba,'ANNOT',directory),patch.object(ba,'OUT',directory), \
                 patch.object(ba,'fetch_html',return_value=True),patch.object(ba,'run_script',return_value=(0,'')),patch.object(ba,'run_agent',fake):
                result=asyncio.run(ba.annotate_one({'title':TITLE},asyncio.Semaphore(1),{},target_revid=42))
            self.assertNotIn('error',result)
            article=json.loads((directory/'Synthetic_Article.json').read_text())
            self.assertEqual(article['schema_version'],4); self.assertEqual(article['revid'],42)
            n.validate_items(article['annotations'])
            self.assertEqual(article['annotations'][0]['private'],{'opaque':True})
            self.assertIn('native review-item format',fake.call_args_list[0].args[0])

    def test_native_repin_updates_only_ai_owned_sources_with_audit(self):
        import update_from_upstream as drift
        original=item()
        article={'annotations':[original],'wikipedia_title':TITLE,'revid':42,'latest_revid':43,'version':7}
        with patch.object(drift,'get_article',return_value=article),patch.object(drift.render,'fetch_article_html',return_value=HTML), \
             patch.object(drift,'post_repin',return_value=(200,{})) as posted:
            result=drift.process_slug(None,'http://unused','Synthetic_Article',SimpleNamespace(force_revid=None,dry_run=False),'run',lambda:'test')
        self.assertEqual(result['outcome'],'repinned')
        payload=posted.call_args.args[-1]
        self.assertEqual(payload['annotations'][0]['links'][0]['annotation']['source']['uri'],n.wikipedia_source_uri(TITLE,43))
        self.assertEqual(payload['meta']['native_source_repin'][0]['from'],URI)
        self.assertEqual(original['links'][0]['annotation']['source']['uri'],URI)

    def test_relative_article_source_repins_with_its_original_value_in_audit(self):
        work=item(); link=work['links'][0]
        link['annotation']['source']['uri']='index.php?title=Synthetic_Article&oldid=42'
        link['base_uri']='https://en.wikipedia.org:443/w/records.json'
        self.assertEqual(render.wrap_annotations(HTML,[work],source_uri=URI)[1],[True])
        result,audit=n.repin_items([work],title=TITLE,old_revid=42,new_revid=43)
        self.assertEqual(audit[0]['from'],link['annotation']['source']['uri'])
        self.assertEqual(result[0]['links'][0]['base_uri'],link['base_uri'])
        self.assertEqual(result[0]['links'][0]['annotation']['source']['uri'],n.wikipedia_source_uri(TITLE,43))

    def test_native_human_tombstone_keeps_historical_pin(self):
        tombstone=item(provenance='human',status='rejected')
        result,audit=n.repin_items([tombstone],title=TITLE,old_revid=42,new_revid=43)
        self.assertEqual(result,[tombstone]); self.assertEqual(audit,[])
        n.validate_items(result,title=TITLE,revid=43)

    def test_native_human_sources_move_with_the_pin_and_keep_attribution(self):
        import update_from_upstream as drift
        article={'annotations':[item(provenance='human')],'wikipedia_title':TITLE,'revid':42,'latest_revid':43,'version':7}
        with patch.object(drift,'get_article',return_value=article),patch.object(drift.render,'fetch_article_html',return_value=HTML),patch.object(drift,'post_repin',return_value=(200,{})) as posted:
            result=drift.process_slug(None,'http://unused','Synthetic_Article',SimpleNamespace(force_revid=None,dry_run=False),'run',lambda:'test')
        self.assertEqual(result['outcome'],'repinned')
        payload=posted.call_args.args[-1]
        self.assertEqual(payload['annotations'][0]['provenance'],'human')
        self.assertEqual(payload['annotations'][0]['links'][0]['annotation']['source']['uri'],n.wikipedia_source_uri(TITLE,43))
        self.assertEqual(payload['meta']['native_source_repin'][0]['to'],n.wikipedia_source_uri(TITLE,43))

    def test_veto_guard_ignores_the_article_pin(self):
        tombstone=item(provenance='human',status='rejected')
        fresh,_=n.repin_items([item()],title=TITLE,old_revid=42,new_revid=43)
        self.assertTrue(moderate._veto_adjacent(fresh[0],[tombstone]))

    def test_only_article_local_occurrences_gate_a_repin(self):
        import update_from_upstream as drift
        work=item(); work['links'].append({'id':'pdf','annotation':{'source':{'type':'pdf','uri':'https://example.net/paper.pdf'},'target':'urn:formal:pdf','location':{'page':2}}})
        article={'annotations':[work],'wikipedia_title':TITLE,'revid':42,'latest_revid':43,'version':7}
        with patch.object(drift,'get_article',return_value=article),patch.object(drift.render,'fetch_article_html',return_value=HTML),patch.object(drift,'post_repin',return_value=(200,{})) as posted:
            result=drift.process_slug(None,'http://unused','Synthetic_Article',SimpleNamespace(force_revid=None,dry_run=False),'run',lambda:'test')
        self.assertEqual(result['outcome'],'repinned'); posted.assert_called_once()

    def test_lost_second_legacy_anchor_prevents_repin(self):
        import update_from_upstream as drift
        legacy=old_item(anchors=[{'section':'Statement','snippet':'A finite group'}, {'section':'Statement','snippet':'missing'}])
        article={'annotations':[legacy],'wikipedia_title':TITLE,'revid':42,'latest_revid':43,'version':7}
        with tempfile.TemporaryDirectory() as tmp,patch.object(drift,'REPORT_PATH',Path(tmp)/'report.jsonl'), \
             patch.object(drift,'get_article',return_value=article),patch.object(drift.render,'fetch_article_html',return_value=HTML),patch.object(drift,'post_repin') as posted:
            result=drift.process_slug(None,'http://unused','Synthetic_Article',SimpleNamespace(force_revid=None,dry_run=False),'run',lambda:'test')
        self.assertEqual(result['outcome'],'needs-work'); posted.assert_not_called()


if __name__ == '__main__':
    unittest.main()
