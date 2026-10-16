#!/usr/bin/env python3
"""Resolving WikiLean anchors into Literature Links quotations and selectors, and reading them back."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import anchor_resolve as ar
import native_annotations as n
import render

TITLE = 'Synthetic Article'
REVID = 42
URI = n.wikipedia_source_uri(TITLE, REVID)
HTML = (
    '<div class="mw-parser-output"><style data-mw-deduplicate="x">.a{color:red}</style>'
    '<p>A <b>finite</b> group has an identity element. Every subgroup of a finite group is finite.</p>'
    '<!-- a comment --><script>var hidden = 1;</script>'
    '<h2><span class="mw-headline" id="Statement">Statement</span></h2>'
    '<p>The order of a subgroup divides the order of the group. A finite group has an identity element.</p>'
    '<div class="math_theorem"><p><b>Theorem 1.</b> Every group of prime order is cyclic.</p></div>'
    '<p><span class="mwe-math-element"><span class="mwe-math-mathml-inline mwe-math-mathml-a11y" style="display: none;">'
    '<math alttext="{\\displaystyle |G|}"><semantics><mrow><mi>G</mi></mrow>'
    '<annotation encoding="application/x-tex">{\\displaystyle |G|}</annotation></semantics></math></span>'
    '<img src="g.svg" class="mwe-math-fallback-image-inline" alt="{\\displaystyle |G|}"></span> is the order.</p>'
    '<h2><span class="mw-headline" id="Examples">Examples</span></h2>'
    '<p>The order of a subgroup divides the order of the group. It really does.</p></div>'
)


def native(anchor, **extras):
    """A native item as the SQLite migration writes it: the profile form, one link per anchor."""
    item = {'id': 'aabbccddeeff', 'status': 'formalized', 'provenance': 'ai', 'links': [], 'unlinked': [], **extras}
    selection = {'type': 'html', 'uri': URI,
                 'location': {'profile': n.ANCHOR_PROFILE, 'anchor': anchor, 'resolution': 'not_attempted'}}
    if item['status'] == 'not_formalized':
        item['unlinked'].append(selection)
    else:
        item['links'].append({'id': item['id'] + ':link:0',
                              'annotation': {'subject': [selection], 'object': [n.mathlib_end('Mathlib.Group', 'Group.identity')]}})
    return item


class TextLayer(unittest.TestCase):
    def test_follows_the_specification(self):
        text, starts, ends = ar.text_layer('<p>  a  <b>natural</b><i>number</i>\n &amp; <style>x{}</style><!-- c -->z </p>')
        self.assertEqual(text, 'a naturalnumber & z')
        self.assertEqual(len(starts), len(text))
        self.assertEqual(len(ends), len(text))
        # Offsets point back into the HTML: the 'z' is the one before the closing tag.
        self.assertEqual('<p>  a  <b>natural</b><i>number</i>\n &amp; <style>x{}</style><!-- c -->z </p>'[starts[-1]], 'z')
        self.assertEqual(ar.collapse('  x\t\ty　'), 'x y')

    def test_mathml_counts_as_text(self):
        text = ar.text_layer(HTML)[0]
        self.assertIn('G{\\displaystyle |G|} is the order.', text)
        self.assertNotIn('color:red', text)
        self.assertNotIn('hidden', text)

    def test_css_strings_round_trip(self):
        value = 'a"b\\c'
        self.assertEqual(ar.css_string(value), 'a\\"b\\\\c')
        self.assertEqual(ar.parse_math_selector(f'img[alt="{ar.css_string(value)}"]'), value)
        self.assertEqual(ar.parse_math_selector('math[alttext="x"]'), 'x')
        self.assertIsNone(ar.parse_math_selector('article > p'))
        tricky = 'x\\a y'  # an escaped backslash before "a " is not a newline
        self.assertEqual(ar.parse_math_selector(f'img[alt="{ar.css_string(tricky)}"]'), tricky)


class Resolving(unittest.TestCase):
    def test_unique_snippet_becomes_a_plain_quotation(self):
        location, how = ar.resolve({'section': '(Lead)', 'snippet': 'Every subgroup of a finite group is finite.'}, HTML)
        self.assertEqual((location, how), ({'quote': {'exact': 'Every subgroup of a finite group is finite.'}}, 'quote'))

    def test_repeated_snippet_takes_context_from_its_section(self):
        location, how = ar.resolve({'section': 'Examples', 'snippet': 'The order of a subgroup divides the order of the group.'}, HTML)
        self.assertEqual(how, 'quote_context')
        quote = location['quote']
        self.assertEqual(quote['exact'], 'The order of a subgroup divides the order of the group.')
        self.assertIn('It really does', quote['suffix'])
        text = ar.text_layer(HTML)[0]
        self.assertEqual(text.count(quote.get('prefix', '') + quote['exact'] + quote['suffix']), 1)
        first, _ = ar.resolve({'section': 'Statement', 'snippet': 'The order of a subgroup divides the order of the group.'}, HTML)
        self.assertTrue(first['quote']['suffix'].startswith(' A finite group'), first['quote'])

    def test_snippet_outside_its_section_is_not_relocated(self):
        self.assertEqual(ar.resolve({'section': 'Examples', 'snippet': 'Every subgroup of a finite group is finite.'}, HTML), (None, 'no_match'))

    def test_stale_context_only_stands_in_for_a_unique_quotation(self):
        self.assertIsNone(render.find_quote(HTML, {'exact': 'The order of a subgroup', 'prefix': 'stale context '}))
        self.assertIsNotNone(render.find_quote(HTML, {'exact': 'Every subgroup of a finite group', 'prefix': 'stale context '}))

    def test_fragment_matches_the_decoded_id(self):
        page = '<p id="cite&#95;ref-1">x</p><h3 id="Planck\'s_constant">y</h3>'
        self.assertEqual(render.find_fragment(page, 'cite_ref-1')[2], 'div')
        self.assertIsNotNone(render.find_fragment(page, "Planck's_constant"))

    def test_case_and_whitespace_differences_quote_the_page_as_written(self):
        location, how = ar.resolve({'section': '(Lead)', 'snippet': 'every   SUBGROUP of a finite group is finite.'}, HTML)
        self.assertEqual(location['quote']['exact'], 'Every subgroup of a finite group is finite.')

    def test_short_snippets_carry_context(self):
        location, how = ar.resolve({'section': 'Examples', 'snippet': 'It really does.'}, HTML)
        self.assertEqual(how, 'quote_context')
        self.assertTrue(location['quote'].get('prefix') or location['quote'].get('suffix'))

    def test_wikitext_snippet_falls_back_to_the_rendered_block(self):
        location, how = ar.resolve({'section': '(Lead)', 'snippet': "A ''finite'' group has an identity element. Every subgroup of a finite group is finite."}, HTML)
        self.assertIsNotNone(location, how)
        self.assertTrue(location['quote']['exact'].startswith('A finite group has an identity element.'))

    def test_formula_alt_text_becomes_a_selector(self):
        location, how = ar.resolve({'type': 'math_alttext', 'value': '{\\displaystyle |G|}'}, HTML)
        self.assertEqual((location, how), ({'css': 'img[alt="{\\\\displaystyle |G|}"]'}, 'css'))
        self.assertEqual(ar.resolve({'type': 'math_alttext', 'value': 'missing'}, HTML), (None, 'no_match'))

    def test_theorem_box_quotes_its_text(self):
        location, how = ar.resolve({'type': 'theorem_box', 'value': 'prime order'}, HTML)
        self.assertEqual(location, {'quote': {'exact': 'Theorem 1. Every group of prime order is cyclic.'}})

    def test_unsupported_and_missing_anchors_stay_unresolved(self):
        self.assertEqual(ar.resolve({'type': 'prose_range', 'section': 'Statement', 'from': 'The'}, HTML), (None, 'unsupported'))
        self.assertEqual(ar.resolve({'section': 'Statement', 'snippet': 'not in the article'}, HTML), (None, 'no_match'))
        self.assertEqual(ar.resolve({'section': 'Statement', 'snippet': '   '}, HTML), (None, 'empty'))


class StoredRecords(unittest.TestCase):
    def test_resolution_replaces_profile_locations_and_keeps_the_editing_key(self):
        items = [native({'section': 'Statement', 'snippet': 'The order of a subgroup divides the order of the group.'}),
                 native({'type': 'math_alttext', 'value': '{\\displaystyle |G|}'}, id='bbccddeeff00'),
                 native({'type': 'prose_range', 'section': 'Statement', 'from': 'The'}, id='ccddeeff0011'),
                 native({'section': 'Statement', 'snippet': 'absent text'}, id='ddeeff001122')]
        _, diagnostics = n.resolve_locations(items, HTML)
        first = items[0]['links'][0]['annotation']['subject'][0]
        self.assertEqual({key: first[key] for key in ('type', 'uri')}, {'type': 'html', 'uri': URI})
        self.assertIn('quote', first['location'])
        self.assertEqual(first['extensions']['org.wikilean']['anchor'],
                         {'section': 'Statement', 'snippet': 'The order of a subgroup divides the order of the group.'})
        self.assertEqual(items[0]['links'][0]['annotation']['object'], [n.mathlib_end('Mathlib.Group', 'Group.identity')])
        self.assertEqual(items[1]['links'][0]['annotation']['subject'][0]['location'], {'css': 'img[alt="{\\\\displaystyle |G|}"]'})
        self.assertEqual(items[2]['links'][0]['annotation']['subject'][0]['location']['resolution'], 'unsupported')
        self.assertEqual(items[3]['links'][0]['annotation']['subject'][0]['location']['resolution'], 'unresolved')
        self.assertEqual([(d['item'], d.get('link'), d.get('end'), d['reason']) for d in diagnostics],
                         [(2, 0, 0, 'unsupported'), (3, 0, 0, 'no_match')])
        n.validate_items(items, title=TITLE, revid=REVID)

    def test_renderers_read_built_in_locations(self):
        items = [native({'section': 'Examples', 'snippet': 'It really does.'})]
        n.resolve_locations(items, HTML)
        # The editing key beside the quotation drives the renderer, unchanged.
        self.assertEqual(n.anchors(items[0]), [{'section': 'Examples', 'snippet': 'It really does.'}])
        wrapped, matched = render.wrap_annotations(HTML, items, source_uri=URI)
        self.assertEqual(matched, [True])
        self.assertIn('class="anno anno-formalized"', wrapped)
        # A record from elsewhere has no editing key; the quotation itself is read.
        foreign = {'status': 'formalized', 'provenance': 'ai', 'links': [{'id': 'f', 'annotation': {
            'subject': [{'type': 'html', 'uri': URI, 'location': {'quote': {
                'exact': 'The order of a subgroup divides the order of the group.', 'suffix': ' It really does.'}}}],
            'object': [{'type': 'generic', 'uri': 'urn:formal:x'}]}}], 'unlinked': []}
        at = lambda location: {**foreign, 'links': [{'id': 'm', 'annotation': {
            'subject': [{'type': 'html', 'uri': URI, 'location': location}], 'object': [{'type': 'generic', 'uri': 'urn:formal:x'}]}}]}
        self.assertEqual(n.anchors(foreign), [{'type': 'quote', 'exact': 'The order of a subgroup divides the order of the group.', 'suffix': ' It really does.'}])
        wrapped, matched = render.wrap_annotations(HTML, [foreign], source_uri=URI)
        self.assertEqual(matched, [True])
        self.assertIn('divides the order of the group.</span> It really does.', wrapped.replace('</span></span>', '</span>'))
        math = at({'css': 'img[alt="{\\\\displaystyle |G|}"]'})
        self.assertEqual(n.anchors(math), [{'type': 'math_alttext', 'value': '{\\displaystyle |G|}'}])
        self.assertEqual(render.wrap_annotations(HTML, [math], source_uri=URI)[1], [True])
        heading = at({'fragment': 'Statement'})
        self.assertEqual(n.anchors(heading), [{'type': 'fragment', 'value': 'Statement'}])
        self.assertEqual(render.wrap_annotations(HTML, [heading], source_uri=URI)[1], [True])
        self.assertEqual(render.find_fragment(HTML, 'nowhere'), None)

    def test_unlinked_selections_keep_their_editing_key(self):
        items = [native({'section': 'Statement', 'snippet': 'The order of a subgroup divides the order of the group.'}, status='not_formalized')]
        n.resolve_locations(items, HTML)
        span = items[0]['unlinked'][0]
        self.assertEqual(set(span), {'type', 'uri', 'location', 'extensions'})
        self.assertIn('quote', span['location'])
        n.validate_items(items, title=TITLE, revid=REVID)
        self.assertEqual(n.anchors(items[0])[0]['section'], 'Statement')


class ResolveSqlite(unittest.TestCase):
    def test_resolves_a_migrated_copy_in_place_without_changing_versions(self):
        import sqlite3
        from unittest.mock import patch
        import resolve_sqlite
        items = [native({'section': 'Statement', 'snippet': 'The order of a subgroup divides the order of the group.'})]
        encoded = json.dumps(items)
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / 'migrated.sqlite'
            db = sqlite3.connect(str(db_path))
            db.executescript("""
                CREATE TABLE articles (slug TEXT PRIMARY KEY, wikipedia_title TEXT, revid INTEGER, annotations TEXT, version INTEGER, schema_version INTEGER);
                CREATE TABLE revisions (id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT, annotations TEXT, kind TEXT, meta TEXT);
            """)
            db.execute("INSERT INTO articles VALUES ('synthetic', ?, ?, ?, 5, 4)", (TITLE, REVID, encoded))
            db.execute("INSERT INTO articles VALUES ('legacy', ?, ?, '[]', 1, 3)", (TITLE, REVID))
            db.execute("INSERT INTO revisions (slug, annotations, kind, meta) VALUES ('synthetic', ?, 'native-migration-checkpoint', NULL)", ('[]',))
            db.execute("INSERT INTO revisions (slug, annotations, kind, meta) VALUES ('synthetic', ?, 'native-migration', '{\"previous_version\": 4}')", (encoded,))
            db.commit(); db.close()
            with patch.object(render, 'fetch_article_html', return_value=HTML):
                resolve_sqlite.main([str(db_path), '--pause', '0', '--report', str(Path(tmp) / 'report.json')])
            db = sqlite3.connect(str(db_path))
            stored, version = db.execute("SELECT annotations, version FROM articles WHERE slug='synthetic'").fetchone()
            self.assertEqual(version, 5)
            location = json.loads(stored)[0]['links'][0]['annotation']['subject'][0]['location']
            self.assertIn('quote', location)
            revision, meta = db.execute("SELECT annotations, meta FROM revisions WHERE slug='synthetic' AND kind='native-migration'").fetchone()
            self.assertEqual(json.loads(revision), json.loads(stored))
            self.assertEqual(json.loads(meta)['location_resolution'], {'quote_context': 1})
            self.assertEqual(json.loads(meta)['previous_version'], 4)
            checkpoint = db.execute("SELECT annotations FROM revisions WHERE kind='native-migration-checkpoint'").fetchone()[0]
            self.assertEqual(checkpoint, '[]')
            legacy_row = db.execute("SELECT annotations FROM articles WHERE slug='legacy'").fetchone()[0]
            self.assertEqual(legacy_row, '[]')
            report = json.loads((Path(tmp) / 'report.json').read_text())
            self.assertEqual(report['totals'], {'quote_context': 1})


if __name__ == '__main__':
    unittest.main()
