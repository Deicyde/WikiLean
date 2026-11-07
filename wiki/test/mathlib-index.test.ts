import { afterEach, expect, it } from 'vitest';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { buildMathlibIndex } from '../scripts/build-mathlib-index.js';
import { MATHLIB_REPOSITORY, mathlibTargets, type JsonObject } from '../src/native-companion.js';

const roots: string[] = [];
afterEach(() => { for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true }); });
const end = (decl: string, uri = MATHLIB_REPOSITORY) => ({
  type: 'lean', uri, location: { declaration: decl, module: 'Example' },
});
const item = (): JsonObject => ({ status: 'formalized', links: [
  { id: 'first', annotation: { subject: [], object: [end('Example.first'), end('Example.second')] } },
  { id: 'foreign', annotation: { subject: [], object: [end('Foreign.name', 'https://example.org/library')] } },
  { id: 'absence', annotation: { relation: 'noMatch', subject: [], object: [end('Absent.name')] } },
], unlinked: [], legacy: { mathlib: { decl: 'Archived.name', module: 'Archived' } } });

it('indexes every native Mathlib target and retains legacy article compatibility', () => {
  const root = mkdtempSync(join(tmpdir(), 'wl-index-')); roots.push(root);
  mkdirSync(join(root, 'annotations'));
  writeFileSync(join(root, 'annotations', 'example.json'), JSON.stringify({ annotations: [
    item(), { status: 'formalized', mathlib: { decl: 'Legacy.name', module: 'Legacy' } },
    { ...item(), status: 'rejected' },
  ] }));
  const out = join(root, 'index.json');
  expect(buildMathlibIndex(root, out)).toBe(3);
  expect(JSON.parse(readFileSync(out, 'utf8'))).toEqual([
    ['Example.first', 'Example'], ['Example.second', 'Example'], ['Legacy.name', 'Legacy'],
  ]);
});

it('resolves explicit carrier bases and retains the object version', () => {
  const record = item();
  record.links = [{ id: 'relative', base_uri: MATHLIB_REPOSITORY,
    annotation: { subject: [], object: [{ ...end('Example.pinned', ''), version: 'v4.33.0' }] } }];
  expect(mathlibTargets(record)).toEqual([{ decl: 'Example.pinned', module: 'Example', version: 'v4.33.0' }]);
});
