import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import {
  validateFCA, validateNativeItems, migrateLegacyItems, nativeAnchorSig,
  legacyAnchorOccurrences, wikipediaSourceUri, strictJsonParse, type NativeItem, type FCARecord,
} from '../src/native-companion.js';

const context = { wikipediaTitle: 'Test Article', revid: 12345 };
const core = (mode: FCARecord['source']['type'] = 'generic'): FCARecord => ({ source: { type: mode, uri: 'urn:source' }, target: 'urn:target' });
const item = (annotation = core()): NativeItem => ({ id: 'aaaaaaaaaaaa', status: 'partial', links: [{ id: 'record-1', annotation }], unlinked: [] });

describe('canonical native companion records', () => {
  it('decodes strict JSON before object keys or integer precision can be lost', () => {
    for (const text of ['{"x":1,"\\u0078":2}', '{"ext":{"same":1,"same":2}}', '[9007199254740993]', '[1e400]']) expect(() => strictJsonParse(text)).toThrow();
    expect(strictJsonParse('{"a":[{"x":"\\\""},true,null,-2.5e-1]}')).toEqual({ a: [{ x: '"' }, true, null, -0.25] });
  });
  it('vendors exactly the pinned reference schema', () => {
    const bytes = readFileSync(new URL('../src/vendor/fca/companion.schema.json', import.meta.url));
    const provenance = JSON.parse(readFileSync(new URL('../src/vendor/fca/provenance.json', import.meta.url), 'utf8'));
    expect(createHash('sha256').update(bytes).digest('hex')).toBe(provenance.sha256);
  });
  it.each(['generic', 'pdf', 'html', 'latex'] as const)('accepts whole-resource %s annotations with opaque extensions', mode => {
    const annotation = { ...core(mode), id: 'duplicate is okay', extensions: { 'urn:test': [null, { unknown: '🧮' }] } };
    expect(validateFCA(annotation)).toEqual([]);
    const a = item(annotation); a.links.push({ id: 'record-2', annotation: structuredClone(annotation) });
    expect(validateNativeItems([a])).toEqual([]);
  });
  it('enforces the reference semantic checks and preserves lexical references', () => {
    expect(validateFCA({ ...core('pdf'), location: { page: 1, rect: [0.9, 0, 0.10000000000000002, 1] } })).not.toEqual([]);
    expect(validateFCA({ ...core('pdf'), location: { page: 1, rect: [0.9, 0, 0.1, 1] } })).toEqual([]);
    expect(validateFCA({ ...core('latex'), location: { lines: [3, 1] } })).not.toEqual([]);
    for (const uri of ['https://[oops', 'file:foo', 'urn:opaque:α%2f#']) {
      expect(validateNativeItems([item({ source: { type: 'generic', uri }, target: uri })])).toEqual([]);
    }
  });
  it('rejects invalid core shapes without discarding arbitrary valid generic location data', () => {
    for (const bad of [{ ...core(), version: 2 }, { ...core(), location: null }, { ...core('html'), location: { css: 'x', fragment: 'y' } }, { ...core(), target: '' }]) {
      expect(validateFCA(bad)).not.toEqual([]);
    }
    expect(validateFCA({ ...core(), location: { profile: 'unknown', arbitrary: [1, null] } })).toEqual([]);
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
    expect(validateFCA({ ...core(), extensions: { 'urn:test': '\ud800' } })).not.toEqual([]);
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
    expect(result.items[0].links[0].annotation.target).toContain('Mathlib/%C3%9C%25X.html#x');
    expect(result.items[0].legacy).toEqual(legacy[0]);
    expect(result.items[0].provenance).toBe('human');
    expect(result.items[0]).not.toHaveProperty('mathlib');
    expect(result.items[0].match_kind).toBe('exact');
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
  });
  it('uses a cross-language exact numeric signature without number/string collisions', () => {
    const a = item({ ...core('pdf'), location: { page: 1, rect: [0, 0.1, 0.2, 0.3] } });
    expect(nativeAnchorSig(a)).toContain('#3fb999999999999a');
    const before = nativeAnchorSig(a); a.links[0].annotation.location = { page: '1' };
    expect(nativeAnchorSig(a)).not.toBe(before);
  });
});
