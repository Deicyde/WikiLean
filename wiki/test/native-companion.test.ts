import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import {
  validateLinkRecord, validateNativeItems, migrateLegacyItems, nativeAnchorSig, legacyAnchorOccurrences, wikipediaSourceUri,
  strictJsonParse, targetUri, targetPin, normalize, companionAnnotation, storedCompanionRecords, MATHLIB_REPOSITORY,
  type NativeItem, type LinkRecord,
} from '../src/native-companion.js';

const context = { wikipediaTitle: 'Test Article', revid: 12345 };
const core = (mode: LinkRecord['source']['type'] = 'generic'): LinkRecord => ({ source: { type: mode, uri: 'urn:source' }, target: 'urn:target' });
const item = (annotation = core()): NativeItem => ({ id: 'aaaaaaaaaaaa', status: 'partial', links: [{ id: 'record-1', annotation }], unlinked: [] });

describe('canonical native companion records', () => {
  it('decodes strict JSON before object keys or integer precision can be lost', () => {
    for (const text of ['{"x":1,"\\u0078":2}', '{"ext":{"same":1,"same":2}}', '[9007199254740993]', '[1e400]']) expect(() => strictJsonParse(text)).toThrow();
    expect(strictJsonParse('{"a":[{"x":"\\\""},true,null,-2.5e-1]}')).toEqual({ a: [{ x: '"' }, true, null, -0.25] });
  });
  it('vendors exactly the pinned reference schema', () => {
    const bytes = readFileSync(new URL('../src/vendor/literature-links/companion.schema.json', import.meta.url));
    const provenance = JSON.parse(readFileSync(new URL('../src/vendor/literature-links/provenance.json', import.meta.url), 'utf8'));
    expect(createHash('sha256').update(bytes).digest('hex')).toBe(provenance.sha256);
    const license = readFileSync(new URL('../src/vendor/literature-links/LICENSE', import.meta.url));
    expect(createHash('sha256').update(license).digest('hex')).toBe(provenance.license_sha256);
  });
  it.each(['generic', 'pdf', 'html', 'latex'] as const)('accepts whole-resource %s annotations with opaque extensions', mode => {
    const annotation = { ...core(mode), id: 'duplicate is okay', extensions: { 'urn:test': [null, { unknown: '🧮' }] } };
    expect(validateLinkRecord(annotation)).toEqual([]);
    const a = item(annotation); a.links.push({ id: 'record-2', annotation: structuredClone(annotation) });
    expect(validateNativeItems([a])).toEqual([]);
  });
  it('enforces the reference semantic checks and preserves lexical references', () => {
    expect(validateLinkRecord({ ...core('pdf'), location: { page: 1, rect: [0.9, 0, 0.2, 1] } })).not.toEqual([]);
    expect(validateLinkRecord({ ...core('pdf'), location: { page: 1, rect: [0.9, 0, 0.1, 1] } })).toEqual([]);
    // The bound is a double sum, so a width computed as 1 - x fits.
    expect(validateLinkRecord({ ...core('pdf'), location: { page: 1, rect: [0.9, 0, 0.10000000000000002, 1] } })).toEqual([]);
    expect(validateLinkRecord({ ...core('latex'), location: { lines: [3, 1] } })).not.toEqual([]);
    // A profile makes the location application-defined in every mode.
    expect(validateLinkRecord({ ...core('pdf'), location: { profile: 'urn:x', rect: [0.9, 0, 0.2, 1] } })).toEqual([]);
    expect(validateLinkRecord({ ...core('latex'), location: { profile: 'urn:x', lines: [3, 1] } })).toEqual([]);
    expect(validateLinkRecord({ ...core('pdf'), location: { page: 1, quote: { exact: 'a group' } } })).toEqual([]);
    expect(validateLinkRecord({ ...core('pdf'), location: { page: 1, rect: [0, 0, 1, 1], quote: { exact: 'a group' } } })).not.toEqual([]);
    for (const uri of ['https://[oops', 'file:foo', 'urn:opaque:α%2f#']) {
      expect(validateNativeItems([item({ source: { type: 'generic', uri }, target: uri })])).toEqual([]);
    }
  });
  it('rejects invalid core shapes without discarding arbitrary valid generic location data', () => {
    for (const bad of [{ ...core(), version: 2 }, { ...core(), location: null }, { ...core('html'), location: { css: 'x', fragment: 'y' } }, { ...core(), target: '' },
      { ...core(), target: {} }, { ...core(), target: { uri: 'urn:x', pin: { type: 'repository', bogus: 1 } } }, { ...core(), status: '' }, { ...core(), kind: 1 }]) {
      expect(validateLinkRecord(bad)).not.toEqual([]);
    }
    expect(validateLinkRecord({ ...core(), location: { profile: 'unknown', arbitrary: [1, null] } })).toEqual([]);
  });
  it('accepts endpoint targets, typed pins and facts; a link still needs a formal destination', () => {
    const pinned: LinkRecord = { ...core(), kind: 'theorem', label: 'Lagrange', note: 'checked by hand',
      status: { value: 'partial', pin: { type: 'repository', url: MATHLIB_REPOSITORY, commit: 'a'.repeat(40) } },
      target: { uri: 'urn:x', pin: { type: 'repository', url: MATHLIB_REPOSITORY, module: 'Mathlib.X', declaration: 'x' } } };
    expect(validateLinkRecord(pinned)).toEqual([]);
    expect(targetUri(pinned)).toBe('urn:x'); expect(targetPin(pinned)?.declaration).toBe('x');
    expect(normalize({ ...core(), status: 'partial' })).toEqual({ source: core().source, target: { uri: 'urn:target' }, status: { value: 'partial' } });
    expect(normalize(pinned)).toEqual(pinned);
    expect(validateNativeItems([item(pinned)])).toEqual([]);
    const absent: LinkRecord = { source: { type: 'html', uri: 'urn:source' }, status: 'not_formalized' };
    expect(validateLinkRecord(absent)).toEqual([]);
    expect(targetUri(absent)).toBeUndefined();
    expect(validateNativeItems([item(absent)]).join()).toContain('requires a formal target');
    const relative = item({ ...core(), target: { uri: '../formal.lean#x' } });
    expect(validateNativeItems([relative]).join()).toContain('base_uri');
    relative.links[0].base_uri = 'file:///shards/a.json';
    expect(validateNativeItems([relative])).toEqual([]);
  });
  it('projects the review item onto each stored link without changing what is stored', () => {
    const local = { source: { type: 'generic' as const, uri: wikipediaSourceUri(context) }, target: 'urn:formal:a', location: { profile: 'urn:wikilean:anchor:v3', anchor: { snippet: 'x' } } };
    const foreign: LinkRecord = { id: 'their-id', label: 'Their label', source: { type: 'pdf', uri: '../paper.pdf' }, target: { uri: 'urn:formal:b' },
      status: { pin: { type: 'repository', url: MATHLIB_REPOSITORY } }, extensions: { 'org.example': { kept: true }, 'org.wikilean': { imported: 1 } } };
    const work: NativeItem = { id: 'aaaaaaaaaaaa', status: 'partial', kind: 'theorem', label: 'Ours', note: 'why partial', provenance: 'ai-moderated', match_kind: 'generalization',
      links: [{ id: 'one', annotation: local }, { id: 'two', annotation: foreign, base_uri: 'https://example.net/shards/a.json' }], unlinked: [] };
    const before = structuredClone(work);
    const [first, second] = storedCompanionRecords([work, { ...work, id: 'bbbbbbbbbbbb', status: 'rejected' }], { ...context, wikidataQid: 'Q1' });
    expect(storedCompanionRecords([work], context)).toHaveLength(2);
    expect(first.annotation).toEqual({ id: 'aaaaaaaaaaaa', kind: 'theorem', label: 'Ours', status: 'partial',
      source: { ...local.source, pin: { type: 'website', title: 'Test Article', owner: 'Wikipedia', url: local.source.uri, revision: '12345', identifiers: { wikidata: 'Q1' } } },
      location: local.location, target: 'urn:formal:a', note: 'why partial',
      extensions: { 'org.wikilean': { provenance: 'ai-moderated', match_kind: 'generalization' } } });
    // A foreign source gets no article pin; stored id, label, extensions and the status pin survive.
    expect(second.annotation).toEqual({ id: 'their-id', kind: 'theorem', label: 'Their label', status: { value: 'partial', pin: { type: 'repository', url: MATHLIB_REPOSITORY } },
      source: foreign.source, target: foreign.target, note: 'why partial',
      extensions: { 'org.example': { kept: true }, 'org.wikilean': { imported: 1, provenance: 'ai-moderated', match_kind: 'generalization' } } });
    expect(second.item_id).toBe('aaaaaaaaaaaa'); expect(second.base_uri).toBe('https://example.net/shards/a.json');
    expect(work).toEqual(before);
    for (const record of [first.annotation, second.annotation]) expect(validateLinkRecord(record)).toEqual([]);
    expect(companionAnnotation({ status: 'partial', links: [], unlinked: [] }, { id: 'x', annotation: local }).source).not.toHaveProperty('pin');
  });
  it('requires carrier bases for relative references and schema hints', () => {
    const a = item({ ...core(), target: '../formal.lean#x', $schema: '../schema.json' });
    expect(validateNativeItems([a])).not.toEqual([]);
    a.links[0].base_uri = 'file:///original/shards/a.json';
    expect(validateNativeItems([a])).toEqual([]);
  });
  it('checks every relative article occurrence against its pinned revision', () => {
    const a = item({ source: { type: 'generic', uri: '?title=Test_Article&oldid=1' }, target: 'urn:x' });
    a.links[0].base_uri = 'https://en.wikipedia.org/w/index.php';
    expect(validateNativeItems([a], context).join()).toContain('source revision');
    a.status = 'rejected';
    expect(validateNativeItems([a], context)).toEqual([]);
  });
  it('rejects unsafe numeric storage and unpaired surrogates, including opaque values', () => {
    const a = item({ ...core(), extensions: { 'urn:test': Number.MAX_SAFE_INTEGER + 1 } });
    expect(validateNativeItems([a]).join()).toContain('lossless');
    expect(validateLinkRecord({ ...core(), extensions: { 'urn:test': '\ud800' } })).not.toEqual([]);
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
    expect(targetUri(result.items[0].links[0].annotation)).toContain('Mathlib/%C3%9C%25X.html#x');
    expect(targetPin(result.items[0].links[0].annotation)).toEqual({ type: 'repository', url: MATHLIB_REPOSITORY, module: 'Mathlib.Ü%X', declaration: 'x' });
    expect(result.items[0].legacy).toEqual(legacy[0]);
    expect(result.items[0].provenance).toBe('human');
    expect(result.items[0]).not.toHaveProperty('mathlib');
    expect(result.items[0].match_kind).toBe('exact');
    // The anchor profile marks an application-defined location on the article's HTML.
    expect(result.items[0].links[0].annotation.source.type).toBe('html');
    expect(result.items[1].unlinked).toHaveLength(1);
    expect(result.items[2].unlinked).toHaveLength(1);
    expect(result.diagnostics.map(d => d.code)).toEqual(expect.arrayContaining(['STATUS_TARGET_CONFLICT', 'MISSING_ANCHOR', 'INVALID_ANCHOR']));
  });
  it('distinguishes absent, explicitly empty and malformed singular anchors during migration', () => {
    const original = { status: 'partial', mathlib: { module: 'Mathlib.X', decl: 'x' } };
    const result = migrateLegacyItems([original, { ...original, anchor: {} }, { ...original, anchor: null }, { ...original, anchor: [] }], context);
    expect(result.items.map(item => item.links.length)).toEqual([1, 1, 0, 0]);
    expect(result.diagnostics).toEqual([{ item: 0, code: 'MISSING_ANCHOR' }, { item: 2, anchor: 0, code: 'INVALID_ANCHOR' }, { item: 3, anchor: 0, code: 'INVALID_ANCHOR' }]);
  });
  it('does not resolve unsupported modes, wrong sources, malformed selectors, or missing context', () => {
    const a = item({ source: { type: 'html', uri: wikipediaSourceUri(context) }, target: 'urn:x' });
    expect(legacyAnchorOccurrences(a)).toEqual([null]);
    expect(legacyAnchorOccurrences(a, context)).toEqual([{ type: 'whole_resource' }]);
    a.links[0].annotation.location = { css: '#x' };
    expect(legacyAnchorOccurrences(a, context)).toEqual([null]);
    a.links[0].annotation = { source: { type: 'generic', uri: wikipediaSourceUri(context) }, target: 'urn:x', location: { profile: 'urn:wikilean:anchor:v3', anchor: { snippet: 1 } } };
    expect(legacyAnchorOccurrences(a, context)).toEqual([null]);
    // Records written before the profile form used generic sources; both read the same.
    for (const type of ['html', 'generic'] as const) {
      a.links[0].annotation = { source: { type, uri: wikipediaSourceUri(context) }, target: 'urn:x', location: { profile: 'urn:wikilean:anchor:v3', anchor: { snippet: 'x' } } };
      expect(legacyAnchorOccurrences(a, context)).toEqual([{ snippet: 'x' }]);
    }
    a.links[0].annotation.source.type = 'pdf';
    expect(legacyAnchorOccurrences(a, context)).toEqual([null]);
  });
  it('uses a cross-language exact numeric signature without number/string collisions', () => {
    const a = item({ ...core('pdf'), location: { page: 1, rect: [0, 0.1, 0.2, 0.3] } });
    expect(nativeAnchorSig(a)).toContain('#3fb999999999999a');
    const before = nativeAnchorSig(a); a.links[0].annotation.location = { page: '1' };
    expect(nativeAnchorSig(a)).not.toBe(before);
  });
});
