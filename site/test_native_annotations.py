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
import native_review as nr
import render
import moderate

TITLE = 'Synthetic Article'
URI = n.wikipedia_source_uri(TITLE, 42)
HTML = '<h2><span id="Statement">Statement</span></h2><p>A finite group has an identity.</p>'


def occurrence(snippet='A finite group', **extras):
    return {'type': 'html', 'uri': URI,
            'location': {'profile': n.ANCHOR_PROFILE, 'anchor': {'section': 'Statement', 'snippet': snippet},
                         'resolution': 'not_attempted'}, **extras}


def formal(uri):
    return [{'type': 'generic', 'uri': uri}]


def item(**extras):
    return {'id': 'aabbccddeeff', 'status': 'formalized', 'provenance': 'ai', 'label': 'Fixture',
            'links': [{'id': 'association:1', 'annotation': {'subject': [occurrence()], 'object': formal('https://formal.example/Groups.html#identity')}}],
            'unlinked': [], **extras}


def old_item(**extras):
    return {'id': 'aabbccddeeff', 'status': 'formalized', 'provenance': 'ai',
            'anchor': {'section': 'Statement', 'snippet': 'A finite group'},
            'mathlib': {'module': 'Mathlib.Group', 'decl': 'Group.identity', 'match_kind': 'exact'},
            'note': 'review', **extras}


class NativeContract(unittest.TestCase):
    def test_native_all_modes_and_unknown_values_survive(self):
        locations = {'pdf': {'page': 1, 'rect': [0, 0, 1, 1]}, 'latex': {'lines': [1, 2]},
                     'html': {'fragment': 'α'}, 'generic': {'unknown': {'list': [None, True, 1.25]}},
                     'lean': {'module': 'Mathlib.Group', 'lines': [4]}}
        work = item()
        work['links'] = [{'id': mode, 'base_uri': 'file:///tmp/shards/one.json',
                          'annotation': {'subject': [{'type': mode, 'uri': '../source', 'location': loc,
                                                      'extensions': {'owner': {'unknown': 5}}}],
                                         'object': formal('../formal#target'), 'id': 'same-record-id',
                                         'extensions': {'custom': {'z': [3, 2, 1]}}}}
                         for mode, loc in locations.items()]
        before = deepcopy(work)
        n.validate_items([work])
        self.assertEqual(n.preserve_native_items([work], [deepcopy(work)]), [before])
        self.assertEqual(work, before)

    def test_core_version_null_locations_and_numeric_invariants_rejected(self):
        for patcher in (lambda a: a.update(version=2), lambda a: a['subject'][0].update(location=None),
                        lambda a: a['subject'][0].update(type='latex', location={'lines': [9, 2]}),
                        lambda a: a['subject'][0].update(type='pdf', location={'page': 1, 'rect': [.9, 0, .2, 1]}),
                        lambda a: a.update(object=[{'type': 'lean', 'uri': n.MATHLIB_REPOSITORY, 'location': {'module': 'M', 'lines': [9, 2]}}]),
                        lambda a: a.update(object=[{'type': 'lean', 'uri': n.MATHLIB_REPOSITORY, 'location': {'declaration': 'x', 'lines': [2]}}]),
                        lambda a: a.update(status={'value': 'partial'}),
                        # Junk values belong to an end; on the record they are an unknown field.
                        lambda a: a.update(junk=['x / 0 = 0']), lambda a: a['object'][0].update(junk=[]),
                        lambda a: a['object'][0].update(junk=['']), lambda a: a['subject'][0].update(junk='x / 0 = 0')):
            work = item(); patcher(work['links'][0]['annotation'])
            with self.subTest(work=work), self.assertRaises(n.NativeAnnotationError):
                n.validate_items([work])

    def test_profile_locations_and_double_sum_rectangles(self):
        record = lambda mode, location: {'subject': [{'type': mode, 'uri': URI, 'location': location}], 'object': formal('urn:x')}
        # The bound is a double sum, so a width computed as 1 - x fits.
        n.validate_link_record(record('pdf', {'page': 1, 'rect': [.9, 0, .10000000000000002, 1]}))
        # A profile makes the location application-defined in every mode.
        n.validate_link_record(record('pdf', {'profile': 'urn:x', 'rect': [.9, 0, .2, 1]}))
        n.validate_link_record(record('latex', {'profile': 'urn:x', 'lines': [9, 2]}))
        n.validate_link_record(record('pdf', {'page': 1, 'quote': {'exact': 'a group'}}))
        with self.assertRaises(n.NativeAnnotationError):
            n.validate_link_record(record('pdf', {'page': 1, 'rect': [0, 0, 1, 1], 'quote': {'exact': 'a group'}}))

    def test_vendored_schema_is_the_pinned_upstream_copy(self):
        import hashlib
        provenance = json.loads((n.SCHEMA_PATH.parent / 'provenance.json').read_text(encoding='utf-8'))
        self.assertEqual(hashlib.sha256(n.SCHEMA_PATH.read_bytes()).hexdigest(), provenance['sha256'])
        self.assertEqual(provenance['version'], '1.0.2')

    def test_native_shape_internal_identity_and_base_are_checked(self):
        broken = []
        for key in ('links', 'unlinked'):
            work = item(); del work[key]; broken.append(work)
        work=item(anchor={}); broken.append(work)
        work=item(); work['links'].append(deepcopy(work['links'][0])); broken.append(work)
        work=item(); work['links'][0]['annotation']['object']=formal('../formal'); broken.append(work)
        work=item(); work['links'][0]['annotation']['object']=[{'uri': 'urn:formal'}]; broken.append(work)
        work=item(); work['links'][0]['annotation']['object']=[]; broken.append(work)
        work=item(); del work['links'][0]['annotation']['object']; broken.append(work)
        work=item(); work['links'][0]['annotation'].update(object=[{'type': 'lean', 'uri': n.MATHLIB_REPOSITORY}], relation='noMatch'); broken.append(work)
        work=item(); work['unlinked']=[occurrence(unknown=1)]; broken.append(work)
        work=item(); work['unlinked']=[{**occurrence(), 'uri': 'relative'}]; broken.append(work)
        work=item(); work['links'][0]['annotation']['extensions']={'exact': 2**53}; broken.append(work)
        for work in broken:
            with self.subTest(work=work), self.assertRaises(n.NativeAnnotationError): n.validate_items([work])

    def test_lean_objects_relations_and_foreign_records_validate(self):
        work = item(kind='theorem', note='why', match_kind='exact')
        work['links'][0]['annotation']['object'] = [{**n.mathlib_end('Mathlib.Group', 'Group.identity'), 'version': 'a' * 40}]
        foreign = {'id': 'their-id', 'relation': 'closeMatch', 'status': 'partial',
                   'subject': [{'type': 'pdf', 'uri': '../paper.pdf', 'title': 'Their title'}],
                   'object': [{'type': 'generic', 'uri': 'urn:formal:b', 'junk': ['x / 0 = 0']}],
                   'extensions': {'org.example': {'kept': True}, 'org.wikilean': {'imported': 1}}}
        absence = {'subject': [occurrence()], 'object': [{'type': 'lean', 'uri': n.MATHLIB_REPOSITORY, 'version': 'a' * 40}],
                   'relation': 'noMatch'}
        work['links'] += [{'id': 'foreign', 'base_uri': 'https://example.net/shards/a.json', 'annotation': foreign},
                          {'id': 'absence', 'annotation': absence}]
        n.validate_items([work], title=TITLE, revid=42)
        self.assertEqual(n.formal_links(work), [
            {'target': 'https://leanprover-community.github.io/mathlib4_docs/Mathlib/Group.html#Group.identity',
             'label': 'Group.identity', 'url': 'https://leanprover-community.github.io/mathlib4_docs/Mathlib/Group.html#Group.identity'},
            {'target': 'urn:formal:b', 'label': 'urn:formal:b'}])
        n.validate_link_record({'subject': [occurrence()], 'object': [], 'status': 'not_formalized'})

    def test_opaque_extension_fields_survive_agent_partial_updates(self):
        original = item(private={'audit': [1, 2]})
        original['links'][0]['annotation']['extensions'] = {'custom': {'opaque': 1}}
        original['links'][0]['annotation']['subject'][0]['extensions'] = {'org.wikilean': {'anchor': {'snippet': 'key'}}}
        original['unlinked'] = [occurrence('Other', extensions={'source': ['untouched']}, base_uri='https://example.net/a.json')]
        produced = item(note='edited', unlinked=[occurrence('Other', base_uri='https://example.net/a.json')])
        restored = n.preserve_native_items([original], [produced])[0]
        self.assertEqual(restored['private'], original['private'])
        self.assertEqual(restored['links'], original['links'])
        self.assertEqual(restored['unlinked'], original['unlinked'])
        self.assertEqual(restored['note'], 'edited')
        # A selection the agent promotes into a link keeps what its unlinked end carried.
        work = item(links=[], unlinked=[occurrence(extensions={'org.wikilean': {'anchor': {'snippet': 'key'}}})])
        promoted = n.preserve_native_items([work], [item()])[0]
        self.assertEqual(promoted['links'][0]['annotation']['subject'], work['unlinked'])

    def test_end_junk_travels_with_its_end_through_agent_edits(self):
        original = item()
        original['links'][0]['annotation']['subject'][0]['junk'] = ['the order of an infinite group is 0']
        original['links'][0]['annotation']['object'][0]['junk'] = ['Nat.card of an infinite type is 0']
        original['unlinked'] = [occurrence('Other', junk=['0 / 0 = 0'])]
        n.validate_items([original], title=TITLE, revid=42)
        # An echoed end keeps the junk values the agent left out.
        restored = n.preserve_native_items([original], [item(unlinked=[occurrence('Other')])])[0]
        self.assertEqual(restored['links'], original['links'])
        self.assertEqual(restored['unlinked'], original['unlinked'])
        # An end that selects something else is a different end, and the old junk stays behind.
        produced = item(unlinked=[occurrence('Other')])
        produced['links'][0]['annotation']['subject'] = [occurrence('A finite group has')]
        produced['links'][0]['annotation']['object'] = formal('https://formal.example/Groups.html#other')
        changed = n.preserve_native_items([original], [produced])[0]['links'][0]['annotation']
        self.assertNotIn('junk', changed['subject'][0])
        self.assertNotIn('junk', changed['object'][0])
        self.assertNotIn('junk', changed)

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
        link['annotation']['subject'][0]['uri']='index.php?title=Synthetic_Article&oldid=41'
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
        changed = deepcopy(human); changed['links'][0]['annotation']['object']=formal('urn:changed')
        posted, _ = moderate.finalize_for_post([human], [changed])
        self.assertEqual(posted, [human])

    def test_veto_adjacency_checks_every_native_occurrence_and_its_source(self):
        tomb=item(status='rejected',provenance='human',links=[],unlinked=[occurrence('Different premise'),occurrence()])
        proposal=item(id='001122334455',links=[],unlinked=[occurrence('Irrelevant prose'),occurrence('A finite group has identity')])
        self.assertTrue(moderate._veto_adjacent(proposal,[tomb]))
        for span in proposal['unlinked']: span['uri']='https://other.example/book'
        self.assertFalse(moderate._veto_adjacent(proposal,[tomb]))

    def test_native_anchor_signature_uses_raw_source_location_and_base(self):
        first = item(); second = deepcopy(first)
        second['links'][0]['annotation']['extensions']={'unrelated': 1}
        second['links'][0]['annotation']['subject'][0]['extensions']={'org.wikilean': {'anchor': {'snippet': 'x'}}}
        self.assertEqual(n.anchor_sig(first), n.anchor_sig(second))
        second['links'][0]['base_uri']='file:///different/base'
        self.assertNotEqual(n.anchor_sig(first), n.anchor_sig(second))
        self.assertEqual(n.anchor_sig(item(links=[])), '["native",null,null,null]')
        self.assertNotEqual(n.anchor_sig(first), n.anchor_sig(old_item()))


class ReviewMerge(unittest.TestCase):
    """The agent names stored selections; the merge carries them forward itself."""

    def test_resolving_a_stored_anchor_is_not_an_agent_edit(self):
        # The 2026-11-18 nightly: every article failed because the pipeline's own
        # resolution pass rewrote the stored profile locations it was handed.
        for snippet, expected in (('A finite group', 'quote'), ('absent from the article', 'profile')):
            stored = item(links=[], unlinked=[occurrence(snippet)])
            produced = [deepcopy(stored)]
            n.resolve_locations(produced, HTML)
            location = produced[0]['unlinked'][0]['location']
            self.assertEqual(expected, 'profile' if 'profile' in location else 'quote')
            self.assertNotEqual(location, stored['unlinked'][0]['location'])
            self.assertEqual(nr.selection_identity(produced[0]['unlinked'][0]),
                             nr.selection_identity(stored['unlinked'][0]))
            posted, _ = moderate.finalize_for_post([stored], produced)
            self.assertEqual(posted, produced)

    def test_a_lost_or_altered_selection_still_fails_the_write(self):
        stored = item(links=[], unlinked=[occurrence(), occurrence('Other')])
        for produced in ([item(links=[], unlinked=[occurrence()])],
                         [item(links=[], unlinked=[occurrence(), occurrence('Other words')])]):
            with self.subTest(produced=produced), self.assertRaises(n.NativeAnnotationError):
                moderate.finalize_for_post([stored], produced)
        altered = deepcopy(stored)
        altered['unlinked'][1]['location']['opaque'] = {'keep': 1}
        with self.assertRaises(n.NativeAnnotationError):
            moderate.finalize_for_post([altered], [stored])

    def test_the_agent_view_names_every_selection_it_may_not_retype(self):
        work = item(unlinked=[occurrence('Other', junk=['0 / 0 = 0'])])
        work['links'].append({'id': 'pdf', 'annotation': {'subject': [{'type': 'pdf', 'uri': 'https://example.net/p.pdf',
                              'location': {'page': 2}}], 'object': formal('urn:formal:pdf')}})
        view = nr.review_view([work])[0]
        self.assertEqual([span['ref'] for span in view['unlinked']], ['u2'])
        self.assertEqual(view['links'][0]['annotation']['subject'],
                         [{'ref': 'u0', 'type': 'html', 'selects': {'section': 'Statement', 'snippet': 'A finite group'}}])
        self.assertEqual(view['links'][1]['annotation']['subject'],
                         [{'ref': 'u1', 'type': 'pdf', 'uri': 'https://example.net/p.pdf', 'opaque': True}])
        self.assertNotIn('junk', json.dumps(view))
        self.assertEqual(view['links'][0]['annotation']['object'], work['links'][0]['annotation']['object'])
        # Echoing the view back changes nothing at all, and human items are shown as stored.
        merged, report = nr.merge_native_review([work], [view])
        self.assertEqual(merged, [work])
        self.assertEqual(report['rejections'], []) and self.assertEqual(report['skipped_items'], [])
        human = item(provenance='human', unlinked=[occurrence('Other')])
        self.assertEqual(nr.review_view([human, old_item()]), [human, old_item()])

    def test_a_reference_promotes_the_stored_selection_not_the_agents_copy(self):
        stored = item(links=[], unlinked=[occurrence(extensions={'org.wikilean': {'anchor': {'snippet': 'key'}}},
                                                     junk=['0 / 0 = 0']), occurrence('Other')])
        reply = {'id': 'aabbccddeeff', 'status': 'formalized', 'note': 'linked',
                 'links': [{'id': 'new:1', 'annotation': {'subject': [{'ref': 'u0'}],
                                                          'object': formal('urn:formal:a')}}],
                 'unlinked': [{'ref': 'u1'}]}
        merged, report = nr.merge_native_review([stored], [reply])
        self.assertEqual(merged[0]['links'][0]['annotation']['subject'], [stored['unlinked'][0]])
        self.assertEqual(merged[0]['unlinked'], [stored['unlinked'][1]])
        self.assertEqual(merged[0]['note'], 'linked')
        self.assertEqual((report['promoted'], report['new_selections']), (1, 0))

    def test_an_agents_own_copy_can_never_change_a_stored_selection(self):
        stored = item(links=[], unlinked=[occurrence(junk=['0 / 0 = 0'])])
        rewritten = occurrence('A finite group has an identity')
        merged, report = nr.merge_native_review([stored], [item(links=[], unlinked=[rewritten])])
        self.assertEqual(merged[0]['unlinked'], [stored['unlinked'][0], rewritten])
        self.assertEqual(report['new_selections'], 1)
        # An exact copy is recognised as the selection it copies, not a second one.
        merged, _ = nr.merge_native_review([stored], [item(links=[], unlinked=[occurrence()])])
        self.assertEqual(merged[0]['unlinked'], stored['unlinked'])

    def test_a_disputed_selection_is_kept_and_reported_to_the_editor(self):
        stored = item(links=[], unlinked=[occurrence()])
        reply = item(links=[], unlinked=[{'ref': 'u0', 'reject': 'the article no longer states this  '}])
        merged, report = nr.merge_native_review([stored], [reply])
        self.assertEqual(merged[0]['unlinked'], stored['unlinked'])
        self.assertEqual(report['rejections'], [{'item': 'aabbccddeeff', 'ref': 'u0',
                                                 'reason': 'the article no longer states this'}])

    def test_an_omitted_item_or_link_is_carried_forward_not_deleted(self):
        # 2026-11-18 Algebraic_number_field: one omitted item failed the article.
        stored = [item(), item(id='001122334455', links=[], unlinked=[occurrence('Other')])]
        merged, report = nr.merge_native_review(stored, [item(note='reviewed')])
        self.assertEqual(merged[1], stored[1])
        self.assertEqual(merged[0]['note'], 'reviewed')
        self.assertEqual(report['carried_items'], 1)
        merged, report = nr.merge_native_review([item()], [{'id': 'aabbccddeeff', 'status': 'rejected',
                                                           'links': [], 'unlinked': []}])
        self.assertEqual(merged[0]['links'], item()['links'])
        self.assertEqual((merged[0]['status'], report['carried_links']), ('rejected', 1))

    def test_one_mangled_item_is_kept_unchanged_instead_of_failing_the_article(self):
        stored = [item(), item(id='001122334455', links=[], unlinked=[occurrence('Other')])]
        for broken in ({'id': '001122334455', 'links': [{'id': 'x', 'annotation': {'subject': [{'ref': 'u7'}],
                                                                                   'object': formal('urn:x')}}], 'unlinked': []},
                       {'id': '001122334455', 'links': [{'id': 'x', 'annotation': {'object': formal('urn:x')}}], 'unlinked': []},
                       {'id': '001122334455', 'links': 'nonsense', 'unlinked': []},
                       {'id': '001122334455', 'anchor': {'snippet': 'legacy'}}):
            with self.subTest(broken=broken):
                merged, report = nr.merge_native_review(stored, [item(note='fine'), broken])
                self.assertEqual(merged[1], stored[1])
                self.assertEqual([entry['item'] for entry in report['skipped_items']], ['001122334455'])
                self.assertEqual(merged[0]['note'], 'fine')
        # A new item cannot borrow another item's references, and nothing is lost by refusing it.
        merged, report = nr.merge_native_review(stored, [item(), item(id='ffeeddccbbaa', links=[],
                                                                     unlinked=[{'ref': 'u0'}])])
        self.assertEqual(len(merged), 2)
        self.assertEqual(report['skipped_items'][0]['item'], 'ffeeddccbbaa')

    def test_the_pipeline_never_resolves_a_human_items_selection(self):
        # A human annotation is posted byte-identical to the stored one, so the
        # pipeline must not rewrite its anchor on the way past.
        import batch_annotate as ba
        stored = [item(provenance='human', links=[], unlinked=[occurrence()]),
                  item(id='001122334455', links=[], unlinked=[occurrence()])]
        produced, _ = ba._native_agent_output(deepcopy(stored), stored, title=TITLE, revid=42, html=HTML)
        self.assertEqual(produced[0], stored[0])
        self.assertNotEqual(produced[1]['unlinked'][0]['location'], stored[1]['unlinked'][0]['location'])
        posted, stats = moderate.finalize_for_post(stored, produced)
        self.assertEqual(posted[0], stored[0])
        self.assertEqual((stats['human_restored_wire'], stats['human_reinserted_wire']), (0, 0))


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
        work=item(); del work['links'][0]['annotation']['subject'][0]['location']
        self.assertEqual(render.wrap_annotations(HTML,[work],source_uri=URI)[1],[True])
        work['links'][0]['annotation']['subject'][0]['location']={}
        self.assertEqual(render.wrap_annotations(HTML,[work],source_uri=URI)[1],[False])

    def test_unknown_modes_and_malformed_generic_locations_remain_unresolved(self):
        for mode,loc in (('pdf',{'page':1}),('latex',{'label':'x'}),('html',{'css':'#x'}),
                         ('generic',{'profile':n.ANCHOR_PROFILE,'anchor':{'type':'math_alttext','value':7}})):
            work=item(); work['links'][0]['annotation']['subject'][0].update(type=mode,location=loc)
            before=deepcopy(work)
            self.assertEqual(render.wrap_annotations(HTML,[work],source_uri=URI)[1],[False])
            self.assertEqual(work,before)

    def test_public_links_are_safe_and_private_review_data_is_absent(self):
        work=item(note='a public note',proof_note='secret',legacy={'secret':True},private='secret')
        work['links'] += [{'id':'bad','annotation':{'subject':[occurrence()],'object':formal('javascript:alert(1)')}},
                          {'id':'credentials','annotation':{'subject':[occurrence()],'object':formal('https://user:pass@example.org/')}},
                          {'id':'relative','annotation':{'subject':[occurrence()],'object':formal('../formal#α')},'base_uri':'https://example.org/shards/one.json'}]
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

    def test_native_moderation_of_a_stored_article_reaches_the_write(self):
        # End to end over the nightly review path: stored item → references in
        # both prompts → merge → resolution → the POST payload moderate.py builds.
        import batch_annotate as ba
        existing = [item(links=[], unlinked=[occurrence(junk=['0 / 0 = 0'])], note='old')]
        replies = [{'annotations': [{'id': 'aabbccddeeff', 'status': 'not_formalized',
                                     'links': [], 'unlinked': [{'ref': 'u0'}], 'note': 'still stated'}]},
                   {'annotations': [{'id': 'aabbccddeeff', 'status': 'formalized', 'unlinked': [],
                                     'links': [{'id': 'aabbccddeeff:link:1', 'annotation': {
                                         'subject': [{'ref': 'u0'}],
                                         'object': [n.mathlib_end('Mathlib.Group', 'Group.identity')]}}]}]}]
        prompts = []
        async def agent(system, prompt, *args, **kwargs):
            prompts.append(prompt)
            return replies[len(prompts) - 1], {}
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / 'Synthetic_Article.42.sections.json').write_text(json.dumps({'sections': []}))
            (directory / 'Synthetic_Article.42.html').write_text(HTML)
            with patch.object(ba, 'CACHE', directory), patch.object(ba, 'ANNOT', directory), patch.object(ba, 'OUT', directory), \
                 patch.object(ba, 'fetch_html', return_value=True), patch.object(ba, 'run_script', return_value=(0, '')), \
                 patch.object(ba, 'run_agent', agent):
                result = asyncio.run(ba.annotate_one({'title': TITLE}, asyncio.Semaphore(1), {}, moderate=True,
                                                     existing_override=deepcopy(existing), target_revid=42))
            final = json.loads((directory / 'Synthetic_Article.json').read_text())['annotations']
        self.assertNotIn('error', result)
        for prompt in prompts:  # neither agent is shown a location it would have to retype
            self.assertIn('"ref": "u0"', prompt)
            self.assertNotIn('not_attempted', prompt)
        link = final[0]['links'][0]['annotation']
        self.assertEqual(link['subject'][0]['junk'], ['0 / 0 = 0'])          # the stored bytes rode through
        self.assertEqual(link['subject'][0]['location']['quote']['exact'], 'A finite group')
        self.assertEqual(link['subject'][0]['extensions']['org.wikilean']['anchor'],
                         occurrence()['location']['anchor'])
        self.assertEqual(final[0]['unlinked'], [])
        posted, stats = moderate.finalize_for_post(existing, final)
        self.assertEqual([a['id'] for a in posted], ['aabbccddeeff'])
        self.assertEqual((stats['ids_echoed'], stats['ids_fresh']), (1, 0))

    def test_native_repin_updates_only_ai_owned_sources_with_audit(self):
        import update_from_upstream as drift
        original=item()
        article={'annotations':[original],'wikipedia_title':TITLE,'revid':42,'latest_revid':43,'version':7}
        with patch.object(drift,'get_article',return_value=article),patch.object(drift.render,'fetch_article_html',return_value=HTML), \
             patch.object(drift,'post_repin',return_value=(200,{})) as posted:
            result=drift.process_slug(None,'http://unused','Synthetic_Article',SimpleNamespace(force_revid=None,dry_run=False),'run',lambda:'test')
        self.assertEqual(result['outcome'],'repinned')
        payload=posted.call_args.args[-1]
        self.assertEqual(payload['annotations'][0]['links'][0]['annotation']['subject'][0]['uri'],n.wikipedia_source_uri(TITLE,43))
        self.assertEqual(payload['meta']['native_source_repin'][0]['from'],URI)
        self.assertEqual(original['links'][0]['annotation']['subject'][0]['uri'],URI)

    def test_relative_article_source_repins_with_its_original_value_in_audit(self):
        work=item(); link=work['links'][0]
        link['annotation']['subject'][0]['uri']='index.php?title=Synthetic_Article&oldid=42'
        link['base_uri']='https://en.wikipedia.org:443/w/records.json'
        self.assertEqual(render.wrap_annotations(HTML,[work],source_uri=URI)[1],[True])
        result,audit=n.repin_items([work],title=TITLE,old_revid=42,new_revid=43)
        self.assertEqual(audit[0]['from'],link['annotation']['subject'][0]['uri'])
        self.assertEqual(result[0]['links'][0]['base_uri'],link['base_uri'])
        self.assertEqual(result[0]['links'][0]['annotation']['subject'][0]['uri'],n.wikipedia_source_uri(TITLE,43))

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
        self.assertEqual(payload['annotations'][0]['links'][0]['annotation']['subject'][0]['uri'],n.wikipedia_source_uri(TITLE,43))
        self.assertEqual(payload['meta']['native_source_repin'][0]['to'],n.wikipedia_source_uri(TITLE,43))

    def test_veto_guard_ignores_the_article_pin(self):
        tombstone=item(provenance='human',status='rejected')
        fresh,_=n.repin_items([item()],title=TITLE,old_revid=42,new_revid=43)
        self.assertTrue(moderate._veto_adjacent(fresh[0],[tombstone]))

    def test_repinned_formula_and_theorem_anchors_cannot_resurrect_a_veto(self):
        def end(anchor, revid, css=None):
            span={'type':'html','uri':n.wikipedia_source_uri(TITLE,revid),'location':{'profile':n.ANCHOR_PROFILE,'anchor':anchor}}
            if css: span.update(location={'css':css},extensions={'org.wikilean':{'anchor':anchor}})
            return span
        cases=[({'type':'theorem_box','value':'Theorem 1'},None),({'type':'math_alttext','value':'x^2+1'},None),
               ({'type':'math_alttext','value':'x^2+1'},'img[alt="x^2+1"]')]
        for anchor,css in cases:
            tomb=item(provenance='human',status='rejected',links=[],unlinked=[end(anchor,42,css)])
            for revid in (42,43):
                fresh=item(links=[],unlinked=[end(anchor,revid,css)]); del fresh['id']
                posted,_=moderate.finalize_for_post([tomb],[fresh])
                self.assertEqual(posted,[tomb],(anchor,css,revid))
            other=item(links=[],unlinked=[end({**anchor,'value':'Other'},43)]); del other['id']
            posted,stats=moderate.finalize_for_post([tomb],[other])
            self.assertEqual((len(posted),stats['veto_adjacent_dropped']),(2,0),anchor)

    def test_only_article_local_occurrences_gate_a_repin(self):
        import update_from_upstream as drift
        work=item(); work['links'].append({'id':'pdf','annotation':{'subject':[{'type':'pdf','uri':'https://example.net/paper.pdf','location':{'page':2}}],'object':formal('urn:formal:pdf')}})
        # A link whose subject runs over the article and a PDF is gated by its article end only.
        work['links'].append({'id':'joint','annotation':{'subject':[occurrence(),{'type':'pdf','uri':'https://example.net/paper.pdf','location':{'page':3}}],'object':formal('urn:formal:joint')}})
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
