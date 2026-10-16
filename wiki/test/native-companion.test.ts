import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import {
  validateLinkRecord, validateNativeItems, migrateLegacyItems, nativeAnchorSig, legacyAnchorOccurrences, parseMathSelector, wikipediaSourceUri,
  companionAnnotation, storedCompanionRecords, unlinkedAnnotation, endReference, mathlibEnd, MATHLIB_REPOSITORY,
  type NativeItem, type LinkRecord, type LinkEnd,
} from '../src/native-companion.js';

const context = { wikipediaTitle: 'Test Article', revid: 12345 };
const end = (type: LinkEnd['type'] = 'generic', extra: Partial<LinkEnd> = {}): LinkEnd => ({ type, uri: 'urn:source', ...extra });
const core = (type: LinkEnd['type'] = 'generic', extra: Partial<LinkEnd> = {}): LinkRecord => ({ subject: [end(type, extra)], object: [{ type: 'generic', uri: 'urn:target' }] });
const item = (annotation = core()): NativeItem => ({ id: 'aaaaaaaaaaaa', status: 'partial', links: [{ id: 'record-1', annotation }], unlinked: [] });
const commit = 'a'.repeat(40);

describe('canonical native companion records', () => {
  it('vendors exactly the pinned reference schema', () => {
    const bytes = readFileSync(new URL('../src/vendor/literature-links/companion.schema.json', import.meta.url));
    const provenance = JSON.parse(readFileSync(new URL('../src/vendor/literature-links/provenance.json', import.meta.url), 'utf8'));
    expect(createHash('sha256').update(bytes).digest('hex')).toBe(provenance.sha256);
    expect(provenance).toMatchObject({ repository: 'https://github.com/mathlib-initiative/literature-links', branch: 'format-2.0', version: '2.0.0' });
    expect(provenance.commit).toMatch(/^[0-9a-f]{40}$/);
    const license = readFileSync(new URL('../src/vendor/literature-links/LICENSE', import.meta.url));
    expect(createHash('sha256').update(license).digest('hex')).toBe(provenance.license_sha256);
  });
  it.each(['generic', 'pdf', 'html', 'latex', 'lean'] as const)('accepts whole-resource %s ends with opaque extensions', type => {
    const annotation = { ...core(type, { extensions: { 'urn:end': [1] } }), id: 'duplicate is okay', extensions: { 'urn:test': [null, { unknown: '🧮' }] } };
    expect(validateLinkRecord(annotation)).toEqual([]);
    const a = item(annotation); a.links.push({ id: 'record-2', annotation: structuredClone(annotation) });
    expect(validateNativeItems([a])).toEqual([]);
  });
  it('enforces the reference semantic checks in every end and preserves lexical references', () => {
    expect(validateLinkRecord(core('pdf', { location: { page: 1, rect: [0.9, 0, 0.2, 1] } }))).not.toEqual([]);
    expect(validateLinkRecord(core('pdf', { location: { page: 1, rect: [0.9, 0, 0.1, 1] } }))).toEqual([]);
    // The bound is a double sum, so a width computed as 1 - x fits.
    expect(validateLinkRecord(core('pdf', { location: { page: 1, rect: [0.9, 0, 0.10000000000000002, 1] } }))).toEqual([]);
    expect(validateLinkRecord(core('latex', { location: { lines: [3, 1] } }))).not.toEqual([]);
    expect(validateLinkRecord(core('latex', { location: { lines: [3] } }))).toEqual([]);
    // Object ends are checked too: Lean lines need a module and must be ordered.
    const lean = (location: Record<string, unknown>): LinkRecord => ({ subject: [end()], object: [{ type: 'lean', uri: MATHLIB_REPOSITORY, location }] });
    expect(validateLinkRecord(lean({ module: 'Mathlib.X', lines: [9, 2] })).join()).toContain('/object/0/location/lines');
    expect(validateLinkRecord(lean({ declaration: 'x', lines: [2, 9] }))).not.toEqual([]);
    expect(validateLinkRecord(lean({ declaration: 'x', module: 'Mathlib.X', lines: [2, 9] }))).toEqual([]);
    // A profile makes the location application-defined in every type.
    expect(validateLinkRecord(core('pdf', { location: { profile: 'urn:x', rect: [0.9, 0, 0.2, 1] } }))).toEqual([]);
    expect(validateLinkRecord(core('latex', { location: { profile: 'urn:x', lines: [3, 1] } }))).toEqual([]);
    expect(validateLinkRecord(core('pdf', { location: { page: 1, quote: { exact: 'a group' } } }))).toEqual([]);
    expect(validateLinkRecord(core('pdf', { location: { page: 1, rect: [0, 0, 1, 1], quote: { exact: 'a group' } } }))).not.toEqual([]);
    for (const uri of ['https://[oops', 'file:foo', 'urn:opaque:α%2f#']) {
      expect(validateNativeItems([item({ subject: [{ type: 'generic', uri }], object: [{ type: 'generic', uri }] })])).toEqual([]);
    }
  });
  it('rejects invalid core shapes without discarding arbitrary valid generic location data', () => {
    for (const bad of [{ ...core(), version: 2 }, core('generic', { location: null as never }), core('html', { location: { css: 'x', fragment: 'y' } }),
      { ...core(), subject: [] }, { ...core(), object: [{ uri: 'urn:x' }] }, { source: { type: 'generic', uri: 'urn:x' }, target: 'urn:y' },
      { ...core(), status: { value: 'partial' } }, { ...core(), status: '' }, { ...core(), kind: 1 }]) {
      expect(validateLinkRecord(bad)).not.toEqual([]);
    }
    // Junk values belong to an end; on the record they are an unknown field.
    expect(validateLinkRecord({ ...core(), junk: ['x / 0 = 0'] })).toEqual(['/: must NOT have additional properties']);
    for (const junk of [[], [''], 'x / 0 = 0', [1]]) {
      expect(validateLinkRecord({ ...core(), object: [{ type: 'generic', uri: 'urn:target', junk }] }).join()).toContain('/object/0/junk');
    }
    expect(validateLinkRecord(core('generic', { location: { profile: 'unknown', arbitrary: [1, null] } }))).toEqual([]);
  });
  it('accepts relations, versions and facts; a link still needs an object end', () => {
    const facts: LinkRecord = { ...core(), relation: 'broadMatch', status: 'partial', kind: 'theorem', id: 'subject-1', note: 'checked by hand',
      object: [{ ...mathlibEnd('Mathlib.X', 'x'), version: commit, junk: ['x / 0 = 0'] }] };
    expect(validateLinkRecord(facts)).toEqual([]);
    expect(validateNativeItems([item(facts)])).toEqual([]);
    const unlinked: LinkRecord = { subject: [end('html')], object: [], status: 'not_formalized' };
    expect(validateLinkRecord(unlinked)).toEqual([]);
    expect(validateNativeItems([item(unlinked)]).join()).toContain('requires an object end');
    const relative = item({ ...core(), object: [{ type: 'generic', uri: '../formal.lean#x' }] });
    expect(validateNativeItems([relative]).join()).toContain('base_uri');
    relative.links[0].base_uri = 'file:///shards/a.json';
    expect(validateNativeItems([relative])).toEqual([]);
  });
  it('stores a checked absence only with the version that was searched', () => {
    const absence: LinkRecord = { subject: [end('html')], object: [{ type: 'lean', uri: MATHLIB_REPOSITORY }], relation: 'noMatch' };
    expect(validateLinkRecord(absence)).toEqual([]);
    expect(validateNativeItems([item(absence)]).join()).toContain('noMatch');
    absence.object[0].version = commit;
    expect(validateNativeItems([item(absence)])).toEqual([]);
  });
  it('projects the review item onto each stored link without changing what is stored', () => {
    const local: LinkRecord = { subject: [{ type: 'html', uri: wikipediaSourceUri(context), location: { profile: 'urn:wikilean:anchor:v3', anchor: { snippet: 'x' } } }],
      object: [mathlibEnd('Mathlib.Algebra.Group.Defs', 'AddCommGroup')] };
    const foreign: LinkRecord = { id: 'their-id', relation: 'closeMatch', subject: [{ type: 'pdf', uri: '../paper.pdf', title: 'Their title' }],
      object: [{ type: 'generic', uri: 'urn:formal:b' }], extensions: { 'org.example': { kept: true }, 'org.wikilean': { imported: 1 } } };
    const work: NativeItem = { id: 'aaaaaaaaaaaa', status: 'partial', kind: 'theorem', label: 'Ours', note: 'why partial', provenance: 'ai-moderated', match_kind: 'generalization',
      links: [{ id: 'one', annotation: local }, { id: 'two', annotation: foreign, base_uri: 'https://example.net/shards/a.json' }], unlinked: [] };
    const before = structuredClone(work);
    const [first, second] = storedCompanionRecords([work, { ...work, id: 'bbbbbbbbbbbb', status: 'rejected' }], { ...context, wikidataQid: 'Q1' });
    expect(storedCompanionRecords([work], context)).toHaveLength(2);
    expect(first.annotation).toEqual({
      subject: [{ type: 'html', uri: 'https://en.wikipedia.org/wiki/Test_Article', version: '12345', location: local.subject[0].location, title: 'Ours',
        citation: { id: 'wikipedia-Test_Article', type: 'webpage', title: 'Test Article', 'container-title': 'Wikipedia', URL: wikipediaSourceUri(context) },
        identifiers: { wikidata: 'Q1' } }],
      object: local.object, relation: 'broadMatch', status: 'partial', kind: 'theorem', id: 'aaaaaaaaaaaa', note: 'why partial',
      extensions: { 'org.wikilean': { provenance: 'ai-moderated' } } });
    // A foreign end gets no article revision; stored id, title and extensions survive, and the item's match_kind decides the relation.
    expect(second.annotation).toEqual({ id: 'their-id', relation: 'broadMatch', status: 'partial', kind: 'theorem', note: 'why partial',
      subject: foreign.subject, object: foreign.object,
      extensions: { 'org.example': { kept: true }, 'org.wikilean': { imported: 1, provenance: 'ai-moderated' } } });
    expect(second).toMatchObject({ item_id: 'aaaaaaaaaaaa', id: 'two', base_uri: 'https://example.net/shards/a.json' });
    expect(work).toEqual(before);
    for (const record of [first.annotation, second.annotation]) expect(validateLinkRecord(record)).toEqual([]);
    expect(companionAnnotation({ status: 'partial', links: [], unlinked: [] }, { id: 'x', annotation: local }).subject[0].uri).toBe(wikipediaSourceUri(context));
  });
  it('exports each end with its own junk values and none on the record', () => {
    const subject: LinkEnd = { type: 'html', uri: wikipediaSourceUri(context), location: { fragment: 'Statement' }, junk: ['the order of an infinite group is 0'] };
    const lagrange = { ...mathlibEnd('Mathlib.GroupTheory.Coset.Card', 'Subgroup.card_subgroup_dvd_card'), junk: ['Nat.card of an infinite type is 0'] };
    const index = { ...mathlibEnd('Mathlib.GroupTheory.Index', 'Subgroup.index_mul_card'), junk: ['Subgroup.index is 0 for an infinite index'] };
    const work: NativeItem = { id: 'aaaaaaaaaaaa', status: 'formalized', match_kind: 'exact',
      links: [{ id: 'one', annotation: { subject: [subject], object: [lagrange, index] } }], unlinked: [{ ...subject, location: { fragment: 'Proof' } }] };
    const before = structuredClone(work);
    const [linked, unlinked] = storedCompanionRecords([work], context);
    expect(linked.annotation).not.toHaveProperty('junk'); expect(unlinked.annotation).not.toHaveProperty('junk');
    expect(linked.annotation.object).toEqual([lagrange, index]);
    expect(linked.annotation.subject[0]).toMatchObject({ uri: 'https://en.wikipedia.org/wiki/Test_Article', version: '12345', junk: subject.junk });
    expect(unlinked.annotation.subject[0].junk).toEqual(subject.junk);
    for (const record of [linked, unlinked]) expect(validateLinkRecord(record.annotation)).toEqual([]);
    expect(validateNativeItems([work], context)).toEqual([]);
    expect(work).toEqual(before);
    linked.annotation.object[0].junk!.push('changed'); linked.annotation.subject[0].junk!.push('changed');
    expect(work).toEqual(before);
  });
  it('maps match_kind to a relation and keeps the tokens a relation does not name alone', () => {
    const link = { id: 'one', annotation: core() };
    const relation = (match_kind?: string) => companionAnnotation({ status: 'formalized', match_kind, links: [link], unlinked: [] }, link);
    expect(relation('exact')).toMatchObject({ relation: 'exactMatch' }); expect(relation('exact').extensions).toBeUndefined();
    expect(relation('special_case')).not.toHaveProperty('relation'); expect(relation('special_case').extensions).toEqual({ 'org.wikilean': { match_kind: 'special_case' } });
    expect(relation('close').relation).toBe('closeMatch');
    expect(relation('invocation')).toMatchObject({ relation: 'relatedMatch', extensions: { 'org.wikilean': { match_kind: 'invocation' } } });
    expect(relation('partial')).not.toHaveProperty('relation'); expect(relation('partial').extensions).toEqual({ 'org.wikilean': { match_kind: 'partial' } });
    expect(relation()).not.toHaveProperty('relation');
    const unstated = (match_kind: string) => companionAnnotation({ status: 'not_formalized', match_kind, links: [link], unlinked: [] }, link);
    expect(unstated('exact')).toMatchObject({ status: 'not_formalized', extensions: { 'org.wikilean': { match_kind: 'exact' } } });
    expect(unstated('exact')).not.toHaveProperty('relation'); expect(unstated('generalization')).not.toHaveProperty('relation');
    expect(unstated('related')).toMatchObject({ relation: 'relatedMatch', status: 'not_formalized' });
    const absence = { id: 'two', annotation: { subject: [end()], object: [{ type: 'lean' as const, uri: MATHLIB_REPOSITORY, version: commit }], relation: 'noMatch' } };
    expect(companionAnnotation({ status: 'not_formalized', match_kind: 'exact', links: [absence], unlinked: [] }, absence))
      .toMatchObject({ relation: 'noMatch', status: 'not_formalized', extensions: { 'org.wikilean': { match_kind: 'exact' } } });
  });
  it('writes no relation but relatedMatch or noMatch for an unformalized item, stored on the link or not', () => {
    const stored = (relation: string, status = 'not_formalized', match_kind?: string) => {
      const link = { id: 'one', annotation: { ...core(), relation } };
      const work: NativeItem = { id: 'aaaaaaaaaaaa', status, ...(match_kind ? { match_kind } : {}), links: [link], unlinked: [] };
      const [record] = storedCompanionRecords([work]);
      expect(validateLinkRecord(record.annotation)).toEqual([]);
      expect(work.links[0].annotation.relation).toBe(relation);
      return record.annotation;
    };
    for (const relation of ['exactMatch', 'broadMatch', 'closeMatch', 'narrowMatch', 'hasPart', 'urn:example:states']) {
      expect(stored(relation)).not.toHaveProperty('relation');
      expect(stored(relation, 'formalized').relation).toBe(relation);
      expect(stored(relation, 'partial').relation).toBe(relation);
    }
    expect(stored('relatedMatch').relation).toBe('relatedMatch');
    // The token still decides when it may; withheld, it stays under org.wikilean.
    expect(stored('exactMatch', 'not_formalized', 'invocation')).toMatchObject({ relation: 'relatedMatch', extensions: { 'org.wikilean': { match_kind: 'invocation' } } });
    expect(stored('relatedMatch', 'not_formalized', 'exact')).toMatchObject({ relation: 'relatedMatch', extensions: { 'org.wikilean': { match_kind: 'exact' } } });
    const withheld = stored('closeMatch', 'not_formalized', 'exact');
    expect(withheld).not.toHaveProperty('relation'); expect(withheld.extensions).toEqual({ 'org.wikilean': { match_kind: 'exact' } });
    expect(stored('closeMatch', 'not_formalized', 'special_case')).not.toHaveProperty('relation');
    // The record's status is the item's, or the stored one when the item has none.
    const link = { id: 'one', annotation: { ...core(), relation: 'exactMatch', status: 'not_formalized' } };
    expect(companionAnnotation({ links: [link], unlinked: [] }, link)).toEqual({ ...core(), status: 'not_formalized' });
    expect(companionAnnotation({ status: 'formalized', links: [link], unlinked: [] }, link)).toMatchObject({ relation: 'exactMatch', status: 'formalized' });
  });
  it('exports unlinked selections as records with an empty object and no relation', () => {
    const selection = { type: 'html' as const, uri: wikipediaSourceUri(context), location: { quote: { exact: 'finite group' } } };
    const work: NativeItem = { id: 'cccccccccccc', status: 'not_formalized', label: 'Groups', match_kind: 'exact', provenance: 'ai', links: [],
      unlinked: [selection, { type: 'pdf', uri: 'paper.pdf', base_uri: 'https://example.net/a.json' }] };
    const records = storedCompanionRecords([work], context);
    expect(records.map(record => [record.item_id, record.unlinked, record.base_uri])).toEqual([['cccccccccccc', 0, undefined], ['cccccccccccc', 1, 'https://example.net/a.json']]);
    expect(records[0].annotation).toEqual({ subject: [{ type: 'html', uri: 'https://en.wikipedia.org/wiki/Test_Article', version: '12345', location: selection.location, title: 'Groups',
      citation: { id: 'wikipedia-Test_Article', type: 'webpage', title: 'Test Article', 'container-title': 'Wikipedia', URL: selection.uri } }],
      object: [], status: 'not_formalized', id: 'cccccccccccc', extensions: { 'org.wikilean': { provenance: 'ai', match_kind: 'exact' } } });
    expect(unlinkedAnnotation(work, work.unlinked[1], context).subject).toEqual([{ type: 'pdf', uri: 'paper.pdf', title: 'Groups' }]);
    for (const record of records) expect(validateLinkRecord(record.annotation)).toEqual([]);
  });
  it('reads built-in locations: the editing key in the end, else the location itself', () => {
    const uri = wikipediaSourceUri(context);
    const at = (location: Record<string, unknown>, extensions?: Record<string, unknown>) => item({ subject: [{ type: 'html', uri, location, ...(extensions ? { extensions } : {}) }], object: [end()] });
    expect(legacyAnchorOccurrences(at({ quote: { exact: 'finite group' } }, { 'org.wikilean': { anchor: { section: '(lead)', snippet: 'finite group' } } }), context))
      .toEqual([{ section: '(lead)', snippet: 'finite group' }]);
    expect(legacyAnchorOccurrences(at({ quote: { exact: 'finite group', prefix: 'A ' } }), context)).toEqual([{ type: 'quote', exact: 'finite group', prefix: 'A ' }]);
    expect(legacyAnchorOccurrences(at({ css: 'img[alt="{\\\\displaystyle |G|}"]' }), context)).toEqual([{ type: 'math_alttext', value: '{\\displaystyle |G|}' }]);
    expect(parseMathSelector('article > p')).toBeNull();
    expect(parseMathSelector('img[alt="x\\\\a y"]')).toBe('x\\a y');
    expect(legacyAnchorOccurrences(at({ fragment: 'Statement' }), context)).toEqual([{ type: 'fragment', value: 'Statement' }]);
    expect(legacyAnchorOccurrences(at({ css: 'article > p' }), context)).toEqual([null]);
    // Every subject end of a link is an occurrence, and an unlinked selection may carry the editing key too.
    const joint = at({ fragment: 'Statement' }); joint.links[0].annotation.subject.push({ type: 'pdf', uri: 'paper.pdf', location: { page: 2 } });
    expect(legacyAnchorOccurrences(joint, context)).toEqual([{ type: 'fragment', value: 'Statement' }, null]);
    const work: NativeItem = { status: 'not_formalized', links: [], unlinked: [{ type: 'html', uri, location: { quote: { exact: 'finite group' } }, extensions: { 'org.wikilean': { anchor: { snippet: 'finite group' } } } }] };
    expect(validateNativeItems([work], context)).toEqual([]);
    expect(legacyAnchorOccurrences(work, context)).toEqual([{ snippet: 'finite group' }]);
  });
  it('requires carrier bases for relative references and schema hints', () => {
    const a = item({ ...core(), object: [{ type: 'generic', uri: '../formal.lean#x' }], $schema: '../schema.json' });
    expect(validateNativeItems([a])).not.toEqual([]);
    a.links[0].base_uri = 'file:///original/shards/a.json';
    expect(validateNativeItems([a])).toEqual([]);
    const unlinked: NativeItem = { status: 'partial', links: [], unlinked: [{ type: 'pdf', uri: 'paper.pdf' }] };
    expect(validateNativeItems([unlinked]).join()).toContain('base_uri');
    unlinked.unlinked[0].base_uri = 'https://example.net/a.json';
    expect(validateNativeItems([unlinked])).toEqual([]);
    unlinked.unlinked[0].base_uri = 'relative/a.json';
    expect(validateNativeItems([unlinked]).join()).toContain('absolute');
  });
  it('checks every relative article occurrence against its pinned revision', () => {
    const a = item({ subject: [{ type: 'generic', uri: '?title=Test_Article&oldid=1' }], object: [end()] });
    a.links[0].base_uri = 'https://en.wikipedia.org/w/index.php';
    expect(validateNativeItems([a], context).join()).toContain('source revision');
    a.status = 'rejected';
    expect(validateNativeItems([a], context)).toEqual([]);
  });
  it('rejects unsafe numeric storage and unpaired surrogates, including opaque values', () => {
    const a = item({ ...core(), extensions: { 'urn:test': Number.MAX_SAFE_INTEGER + 1 } });
    expect(validateNativeItems([a]).join()).toContain('lossless');
    expect(validateLinkRecord({ ...core(), extensions: { 'urn:test': String.fromCharCode(0xd800) } })).not.toEqual([]);
    a.links[0].annotation.extensions = { 'urn:test': Number.MAX_SAFE_INTEGER };
    expect(validateNativeItems([a])).toEqual([]);
  });
  it('keeps duplicate occurrences but requires independent internal identities', () => {
    const a = item(); a.links.push(structuredClone(a.links[0]));
    expect(validateNativeItems([a]).join()).toContain('unique');
    a.links[1].id = 'record-2';
    expect(validateNativeItems([a])).toEqual([]);
  });
  it('migrates all work items, targetless spans, tombstones and private originals', () => {
    const legacy = [
      { id: 'aaaaaaaaaaaa', status: 'not_formalized', provenance: 'human', anchors: [{ snippet: 'x' }, {}], mathlib: { module: 'Mathlib.Ü%X', decl: 'x', match_kind: 'exact' }, secret: { x: 1 } },
      { id: 'bbbbbbbbbbbb', status: 'rejected', provenance: 'human', label: 'veto' },
      { id: 'cccccccccccc', status: 'partial', anchor: { snippet: 2 }, mathlib: { module: 'Mathlib.X', decl: 'x' } },
    ];
    const result = migrateLegacyItems(legacy, context);
    expect(result.items).toHaveLength(3);
    expect(result.items[0].links).toHaveLength(2);
    const annotation = result.items[0].links[0].annotation;
    expect(annotation.object).toEqual([{ type: 'lean', uri: MATHLIB_REPOSITORY, location: { declaration: 'x', module: 'Mathlib.Ü%X' } }]);
    expect(endReference(annotation.object[0])).toContain('Mathlib/%C3%9C%25X.html#x');
    expect(result.items[0].legacy).toEqual(legacy[0]);
    expect(result.items[0].provenance).toBe('human');
    expect(result.items[0]).not.toHaveProperty('mathlib');
    expect(result.items[0].match_kind).toBe('exact');
    // The anchor profile marks an application-defined location on the article's HTML.
    expect(annotation.subject).toEqual([{ type: 'html', uri: wikipediaSourceUri(context), location: { profile: 'urn:wikilean:anchor:v3', anchor: { snippet: 'x' }, resolution: 'not_attempted' } }]);
    expect(result.items[1].unlinked).toHaveLength(1);
    expect(result.items[1].unlinked[0]).toMatchObject({ type: 'html', uri: wikipediaSourceUri(context) });
    expect(result.items[2].unlinked).toHaveLength(1);
    expect(result.diagnostics.map(d => d.code)).toEqual(expect.arrayContaining(['STATUS_TARGET_CONFLICT', 'MISSING_ANCHOR', 'INVALID_ANCHOR']));
  });
  it('distinguishes absent, explicitly empty and malformed singular anchors during migration', () => {
    const original = { status: 'partial', mathlib: { module: 'Mathlib.X', decl: 'x' } };
    const result = migrateLegacyItems([original, { ...original, anchor: {} }, { ...original, anchor: null }, { ...original, anchor: [] }], context);
    expect(result.items.map(item => item.links.length)).toEqual([1, 1, 0, 0]);
    expect(result.diagnostics).toEqual([{ item: 0, code: 'MISSING_ANCHOR' }, { item: 2, anchor: 0, code: 'INVALID_ANCHOR' }, { item: 3, anchor: 0, code: 'INVALID_ANCHOR' }]);
  });
  it('does not resolve unsupported types, wrong sources, malformed selectors, or missing context', () => {
    const a = item({ subject: [{ type: 'html', uri: wikipediaSourceUri(context) }], object: [end()] });
    const subject = () => a.links[0].annotation.subject[0];
    expect(legacyAnchorOccurrences(a)).toEqual([null]);
    expect(legacyAnchorOccurrences(a, context)).toEqual([{ type: 'whole_resource' }]);
    subject().location = { css: '#x' };
    expect(legacyAnchorOccurrences(a, context)).toEqual([null]);
    subject().location = { profile: 'urn:wikilean:anchor:v3', anchor: { snippet: 1 } };
    expect(legacyAnchorOccurrences(a, context)).toEqual([null]);
    subject().location = { profile: 'urn:wikilean:anchor:v3', anchor: { snippet: 'x' } };
    expect(legacyAnchorOccurrences(a, context)).toEqual([{ snippet: 'x' }]);
    // Only html ends carry Wikipedia text; another type is left to its own reader.
    subject().type = 'generic';
    expect(legacyAnchorOccurrences(a, context)).toEqual([null]);
    subject().type = 'pdf';
    expect(legacyAnchorOccurrences(a, context)).toEqual([null]);
  });
  it('uses a cross-language exact numeric signature without number/string collisions', () => {
    const a = item(core('pdf', { location: { page: 1, rect: [0, 0.1, 0.2, 0.3] } }));
    expect(nativeAnchorSig(a)).toContain('#3fb999999999999a');
    const before = nativeAnchorSig(a); a.links[0].annotation.subject[0].location = { page: '1' };
    expect(nativeAnchorSig(a)).not.toBe(before);
    // The end's extensions, such as the editing key, are not part of the selection.
    const keyed = structuredClone(a); keyed.links[0].annotation.subject[0].extensions = { 'org.wikilean': { anchor: { snippet: 'x' } } };
    expect(nativeAnchorSig(keyed)).toBe(nativeAnchorSig(a));
    expect(nativeAnchorSig({ status: 'partial', links: [], unlinked: [] })).toBe('["native",null,null,null]');
  });
});
