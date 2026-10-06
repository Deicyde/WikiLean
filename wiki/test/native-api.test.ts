import { describe, it, expect } from 'vitest';
import {
  setup, get, put, post, save, botSave, botSaveRun, storedAnnotations, articleRow,
  latestRevision, revisionCount, eventRows, SLUG, REVID, NEW_REVID, PIPELINE_TOKEN, echo, blockNetwork,
} from './helpers/harness.js';
import { migrateLocalDatabase } from '../scripts/migrate-native.js';
import { app } from '../src/index.js';
import { editorItems, wikipediaSourceUri, type NativeItem, type FCARecord } from '../src/native-companion.js';

blockNetwork();
function nativeSetup() { const h = setup(); migrateLocalDatabase(h.db); return h; }
const nativeItems = (h: ReturnType<typeof setup>) => storedAnnotations(h.db) as NativeItem[];
function repin(items: NativeItem[], revid: number) {
  const output = echo(items);
  for (const item of output) {
    if (item.status === 'rejected') continue;
    for (const link of item.links) link.annotation.source.uri = wikipediaSourceUri({ wikipediaTitle: 'Test Article', revid });
    for (const occ of item.unlinked) occ.source.uri = wikipediaSourceUri({ wikipediaTitle: 'Test Article', revid });
  }
  return output;
}
const core = (type: FCARecord['source']['type']): FCARecord => ({ source: { type, uri: 'urn:source' }, target: 'urn:target', extensions: { 'urn:opaque': { deep: [null, 'α', 1] } } });

describe('native canonical API and lifecycle', () => {
  it('rejects duplicate members in the raw HTTP annotation instead of silently collapsing extensions', async () => {
    const h = nativeSetup(); const items = nativeItems(h);
    items[0].links[0].annotation.extensions = { 'urn:opaque': { duplicate: 'one' } };
    const body = JSON.stringify({ annotations: items, base_version: 2 }).replace('"duplicate":"one"', '"duplicate":"one","dupl\\u0069cate":"two"');
    const before = articleRow(h.db);
    const response = await app.request(`/api/article/${SLUG}`, { method: 'POST', headers: { Authorization: `Bearer ${PIPELINE_TOKEN}`, 'Content-Type': 'application/json' }, body }, h.env);
    expect(response.status).toBe(400); expect(articleRow(h.db)).toEqual(before);
  });
  it('defaults new articles to native storage, records source context, and cannot mint human provenance', async () => {
    const h = setup();
    const items = [{ id: 'aaaaaaaaaaaa', status: 'formalized', provenance: 'human', links: ['pdf', 'html', 'latex', 'generic'].map((type, index) => ({ id: `link-${index}`, annotation: core(type as FCARecord['source']['type']) })), unlinked: [] }];
    const result = await put(h.env, '/api/article/Native_New', { wikipedia_title: 'Native New', revid: REVID, annotations: items }, { bearer: PIPELINE_TOKEN });
    expect(result.status).toBe(201);
    expect(articleRow(h.db, 'Native_New')?.schema_version).toBe(4);
    const stored = storedAnnotations(h.db, 'Native_New') as NativeItem[];
    expect(stored[0].links).toEqual(items[0].links);
    expect(stored[0].provenance).toBe('ai');
    expect(h.db.prepare('SELECT source_revid,schema_version FROM revisions WHERE slug=?').get('Native_New')).toMatchObject({ source_revid: REVID, schema_version: 4 });
    const records = await get(h.env, '/api/article/Native_New/companion.json');
    expect(await records.json()).toEqual(items[0].links.map(link => link.annotation));
    const legacy = await put(h.env, '/api/article/Legacy_Implicit', { wikipedia_title: 'Legacy', revid: REVID, annotations: [{ status: 'partial', anchor: {} }] }, { bearer: PIPELINE_TOKEN });
    expect(legacy.status).toBe(400);
  });
  it('serves stored associations verbatim, preserves duplicate IDs and records with their bases', async () => {
    const h = nativeSetup(); const items = nativeItems(h);
    const value: FCARecord = { source: { type: 'pdf', uri: '../paper.pdf' }, target: '../formal.lean#x', id: 'shared', location: { page: 2 }, extensions: { 'urn:custom': ['keep', { a: true }] } };
    items[0].links = [{ id: 'one', annotation: value, base_uri: 'file:///papers/shards/a.json' }, { id: 'two', annotation: echo(value), base_uri: 'file:///other/shards/b.json' }];
    const saved = await botSave(h.env, { annotations: items, base_version: 2 });
    expect(saved.status).toBe(200);
    const response = await get(h.env, `/api/article/${SLUG}/companion-records.json`);
    expect(await response.json()).toEqual({ version: 3, records: items[0].links.map(link => ({ ...link, item_id: items[0].id })) });
    expect((await get(h.env, `/api/article/${SLUG}/companion.json`)).status).toBe(409);
  });
  it('retains relative schema hints in record reads and refuses to change their carrier silently', async () => {
    const h = nativeSetup(); const items = nativeItems(h);
    items[0].links[0].annotation.$schema = '../schema.json'; items[0].links[0].base_uri = 'https://example.org/shards/a.json';
    expect((await botSave(h.env, { annotations: items, base_version: 2 })).status).toBe(200);
    expect((await get(h.env, `/api/article/${SLUG}/companion.json`)).status).toBe(409);
  });
  it('keeps an explicit carrier for opaque profile references even when core references are absolute', async () => {
    const h = nativeSetup(); const items = nativeItems(h);
    items[0].links[0].annotation.location = { profile: 'urn:custom', relative_part: '../part.json' };
    items[0].links[0].base_uri = 'https://example.org/shards/original.json';
    expect((await botSave(h.env, { annotations: items, base_version: 2 })).status).toBe(200);
    expect((await get(h.env, `/api/article/${SLUG}/companion.json`)).status).toBe(409);
    const lossless = await (await get(h.env, `/api/article/${SLUG}/companion-records.json`)).json() as { records: unknown[] };
    expect(lossless.records[0]).toMatchObject(items[0].links[0]);
  });
  it('round-trips unsupported modes and extensions through an ordinary human save', async () => {
    const h = nativeSetup(); const items = nativeItems(h);
    items[0].links.push({ id: 'external', annotation: { ...core('html'), location: { css: '.arbitrary' } } });
    expect((await botSave(h.env, { annotations: items, base_version: 2 })).status).toBe(200);
    const posted = editorItems(nativeItems(h)); posted[0].label = 'Human label';
    expect((await save(h.env, { annotations: posted, base_version: 3 }, { user: 'u-human' })).status).toBe(200);
    expect(nativeItems(h)[0].links).toEqual(items[0].links);
    expect(nativeItems(h)[0].legacy).toEqual(items[0].legacy);
    expect(nativeItems(h)[0].provenance).toBe('human');
  });
  it('withholds private archives and workflow data from public API, editor state, and history', async () => {
    const h = nativeSetup(); const items = nativeItems(h);
    items[0].private_token = 'secret-native-value'; items[0].note = 'private-note';
    h.db.prepare('UPDATE articles SET annotations=? WHERE slug=?').run(JSON.stringify(items), SLUG);
    const initial = latestRevision(h.db).id;
    expect(await (await get(h.env, `/api/article/${SLUG}.json`)).text()).not.toMatch(/secret-native-value|private-note|"legacy"/);
    expect(await (await get(h.env, `/${SLUG}`, { user: 'u-human' })).text()).not.toContain('secret-native-value');
    const posted = editorItems(items); posted[0].label = 'edited';
    expect((await save(h.env, { annotations: posted, base_version: 2 }, { user: 'u-human' })).status).toBe(200);
    expect(nativeItems(h)[0].private_token).toBe('secret-native-value');
    const diff = await (await get(h.env, `/${SLUG}/diff/${initial}/${latestRevision(h.db).id}`)).text();
    expect(diff).not.toMatch(/secret-native-value|private-note|"legacy"/);
  });
  it('does not reveal the archived legacy side of a migration in a public cross-format diff', async () => {
    const h = setup(); const old = storedAnnotations(h.db); old[0].internal_review = 'legacy-private-secret';
    h.db.prepare('UPDATE articles SET annotations=?').run(JSON.stringify(old));
    h.db.prepare('UPDATE revisions SET annotations=?').run(JSON.stringify(old));
    migrateLocalDatabase(h.db);
    const diff = await (await get(h.env, `/${SLUG}/diff/1/${latestRevision(h.db).id}`)).text();
    expect(diff).not.toContain('legacy-private-secret');
  });
  it('rejects legacy writes to native articles, unsafe integers, and duplicate occurrence keys without writes', async () => {
    const h = nativeSetup(); const before = articleRow(h.db); const count = revisionCount(h.db);
    for (const annotations of [
      [{ status: 'partial', anchor: {} }],
      nativeItems(h).map((item, index) => index ? item : { ...item, links: [item.links[0], item.links[0]] }),
      nativeItems(h).map((item, index) => index ? item : { ...item, links: [{ ...item.links[0], annotation: { ...item.links[0].annotation, extensions: { 'urn:unsafe': 9007199254740992 } } }] }),
    ]) expect((await botSave(h.env, { annotations, base_version: 2 })).status).toBe(400);
    expect(articleRow(h.db)).toEqual(before); expect(revisionCount(h.db)).toBe(count);
  });
  it('preserves human links and tombstones against bot bulk changes', async () => {
    const h = nativeSetup(); const items = editorItems(nativeItems(h)); items[0].status = 'rejected';
    expect((await save(h.env, { annotations: items, base_version: 2 }, { user: 'u-human' })).status).toBe(200);
    const changed = nativeItems(h); changed[0].links[0].annotation.target = 'urn:changed';
    expect((await botSave(h.env, { annotations: changed, base_version: 3 })).status).toBe(422);
    expect(await (await get(h.env, `/api/article/${SLUG}/companion.json`, { bearer: PIPELINE_TOKEN })).json()).toEqual([]);
  });
  it('endorses native items and makes a stale edit fail without mutation', async () => {
    const h = nativeSetup();
    expect((await save(h.env, { action: 'endorse', annotation_id: 'aaaaaaaaaaaa', base_version: 2 }, { user: 'u-human' })).status).toBe(200);
    expect(nativeItems(h)[0].provenance).toBe('human');
    const before = articleRow(h.db);
    expect((await save(h.env, { annotations: editorItems(nativeItems(h)), base_version: 2 }, { user: 'u-human' })).status).toBe(409);
    expect(articleRow(h.db)).toEqual(before);
  });
  it('applies native proposals with authenticated provenance and retains old incompatible proposals for explicit rebase', async () => {
    const h = nativeSetup();
    expect((await save(h.env, { action: 'endorse', annotation_id: 'aaaaaaaaaaaa', base_version: 2 }, { user: 'u-human' })).status).toBe(200);
    const items = nativeItems(h); const links = echo(items[0].links); links[0].annotation.target = 'urn:replacement';
    expect((await botSave(h.env, { annotations: items, base_version: 3, meta: { ladder: { proposals: [{ annotationId: items[0].id, fields: { links }, reason: 'replace target' }] } } })).status).toBe(200);
    const pending = JSON.parse((h.db.prepare('SELECT proposal FROM moderation_state WHERE slug=?').get(SLUG) as { proposal: string }).proposal);
    expect((await botSave(h.env, { action: 'approve_proposal', proposal_id: pending[0].proposalId, base_version: 3 })).status).toBe(200);
    expect(nativeItems(h)[0].links).toEqual(links); expect(nativeItems(h)[0].provenance).toBe('ai-moderated');
    expect((await botSave(h.env, { annotations: nativeItems(h), base_version: 4, meta: { ladder: { proposals: [{ annotationId: items[0].id, fields: { mathlib: { decl: 'legacy' } }, reason: 'old dialect' }] } } })).status).toBe(200);
    const legacy = JSON.parse((h.db.prepare('SELECT proposal FROM moderation_state WHERE slug=?').get(SLUG) as { proposal: string }).proposal);
    expect((await botSave(h.env, { action: 'approve_proposal', proposal_id: legacy[0].proposalId, base_version: 4 })).status).toBe(409);
    expect((h.db.prepare('SELECT proposal FROM moderation_state WHERE slug=?').get(SLUG) as { proposal: string }).proposal).toContain(legacy[0].proposalId);
  });
  it('requires every active local span to resolve when advancing the pin', async () => {
    const h = nativeSetup(); const items = repin(nativeItems(h), NEW_REVID);
    const bad = echo(items[0].links[0]); bad.id = 'missing-span'; bad.annotation.location = { profile: 'urn:wikilean:anchor:v3', anchor: { section: '(lead)', snippet: 'absent text' } };
    items[0].links.push(bad);
    expect((await botSave(h.env, { annotations: items, revid: NEW_REVID, base_version: 2 })).status).toBe(422);
    expect(articleRow(h.db)?.revid).toBe(REVID);
    items[0].links.pop();
    expect((await botSave(h.env, { annotations: items, revid: NEW_REVID, base_version: 2 })).status).toBe(200);
    expect(articleRow(h.db)?.revid).toBe(NEW_REVID);
  });
  it('keeps rejected human source pins intact when active AI spans are reanchored', async () => {
    const h = nativeSetup(); const items = editorItems(nativeItems(h)); items[0].status = 'rejected';
    await save(h.env, { annotations: items, base_version: 2 }, { user: 'u-human' });
    const before = nativeItems(h)[0];
    expect((await botSave(h.env, { annotations: repin(nativeItems(h), NEW_REVID), revid: NEW_REVID, base_version: 3 })).status).toBe(200);
    expect(nativeItems(h)[0]).toEqual(before);
  });
  it('allows a moderator to reanchor a complete human snapshot atomically and truthfully', async () => {
    const h = nativeSetup();
    await save(h.env, { action: 'endorse', annotation_id: 'aaaaaaaaaaaa', base_version: 2 }, { user: 'u-human' });
    const changed = repin(nativeItems(h), NEW_REVID);
    expect((await botSave(h.env, { annotations: changed, base_version: 3, revid: NEW_REVID })).status).toBe(422);
    expect((await save(h.env, { annotations: editorItems(changed), base_version: 3, revid: NEW_REVID }, { user: 'u-human' })).status).toBe(403);
    expect((await save(h.env, { annotations: editorItems(changed), base_version: 3, revid: NEW_REVID }, { user: 'u-patroller' })).status).toBe(200);
    expect(nativeItems(h)[0].provenance).toBe('human');
    expect(articleRow(h.db)?.revid).toBe(NEW_REVID);
  });
  it('restores native source pins and rejects unresolved legacy history', async () => {
    const h = nativeSetup(); const before = nativeItems(h); const rev = latestRevision(h.db).id;
    expect((await botSave(h.env, { annotations: repin(before, NEW_REVID), revid: NEW_REVID, base_version: 2 })).status).toBe(200);
    expect((await post(h.env, `/api/article/${SLUG}/revert/${rev}`, {}, { user: 'u-patroller' })).status).toBe(200);
    expect(articleRow(h.db)?.revid).toBe(REVID); expect(nativeItems(h)).toEqual(before);
    const restored = articleRow(h.db);
    expect((await post(h.env, `/api/article/${SLUG}/revert/1`, {}, { user: 'u-patroller' })).status).toBe(409);
    expect(articleRow(h.db)).toEqual(restored);
  });
  it('restores source pins during run rollback and keeps writes atomic on failure', async () => {
    const h = nativeSetup(); const before = nativeItems(h);
    expect((await botSaveRun(h.env, { annotations: repin(before, NEW_REVID), revid: NEW_REVID, base_version: 2 }, 'native-run')).status).toBe(200);
    expect((await post(h.env, '/api/admin/revert-run/native-run', {}, { user: 'u-admin' })).status).toBe(200);
    expect(articleRow(h.db)?.revid).toBe(REVID); expect(nativeItems(h)).toEqual(before);
    const row = articleRow(h.db); const revisions = revisionCount(h.db); const events = eventRows(h.db);
    h.db.exec("CREATE TRIGGER reject_native_event BEFORE INSERT ON annotation_events BEGIN SELECT RAISE(ABORT,'injected failure'); END");
    const changed = editorItems(nativeItems(h)); changed[0].label = 'must roll back';
    expect((await save(h.env, { annotations: changed, base_version: row!.version }, { user: 'u-human' })).status).toBe(500);
    expect(articleRow(h.db)).toEqual(row); expect(revisionCount(h.db)).toBe(revisions); expect(eventRows(h.db)).toEqual(events);
  });
});
