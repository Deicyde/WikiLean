import { describe, it, expect } from 'vitest';
import { migrateLocalDatabase } from '../scripts/migrate-native.js';
import { setup, post, articleRow, storedAnnotations, revisionCount, SLUG, REVID, PIPELINE_TOKEN, blockNetwork, type Harness } from './helpers/harness.js';

const path = `/api/admin/article/${SLUG}/native-migration`;
async function preview(h: Harness) {
  const response = await post(h.env, path, {}, { user: 'u-admin' });
  expect(response.status).toBe(200);
  const result = await response.json() as { base_version: number; source_revid: number; snapshot: string };
  return { base_version: result.base_version, source_revid: result.source_revid, snapshot: result.snapshot };
}
async function apply(h: Harness, options?: Awaited<ReturnType<typeof preview>>) {
  const plan = options ?? await preview(h);
  return post(h.env, path, { ...plan, apply: true }, { user: 'u-admin' });
}
function archives(h: Harness) {
  return h.db.prepare('SELECT * FROM companion_migrations').all();
}
describe('admin structural native migration', () => {
  blockNetwork();
  it('previews without writes even while ordinary article editing is off', async () => {
    const h = setup({ slugEditing: false });
    const before = articleRow(h.db); const n = revisionCount(h.db);
    const result = await preview(h);
    expect(result).toMatchObject({ base_version: 1, source_revid: REVID });
    expect(result.snapshot).toMatch(/^[0-9a-f]{64}$/);
    expect(articleRow(h.db)).toEqual(before);
    expect(archives(h)).toEqual([]); expect(revisionCount(h.db)).toBe(n);
    expect((await apply(h, result)).status).toBe(409); // maintenance lock required
    expect(articleRow(h.db)).toEqual(before);
  });
  it('matches the offline rehearsal, preserving human bytes, vetoes, proposals, source pin and history', async () => {
    const live = setup({ slugEditing: false, siteWriteMode: 'locked' }); const copy = setup();
    const old = storedAnnotations(live.db);
    old[0].provenance = 'human'; delete old[1].id;
    old.push({ id: 'cccccccccccc', status: 'rejected', provenance: 'human', anchor: { snippet: 'veto' }, extra: 'keep' });
    const bytes = JSON.stringify(old, null, 3) + '\n';
    const proposal = ' [ {"annotationId":"aaaaaaaaaaaa","fields":{"note":"pending"}} ] ';
    for (const h of [live, copy]) {
      h.db.prepare('UPDATE articles SET annotations=? WHERE slug=?').run(bytes, SLUG);
      h.db.prepare('INSERT INTO moderation_state (slug,proposal,rejected_proposals,last_reviewed_version) VALUES (?,?,?,1)').run(SLUG, proposal, ' [ ] ');
    }
    const offline = migrateLocalDatabase(copy.db);
    const response = await apply(live);
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ applied: true, report: offline.articles[0] });
    expect(storedAnnotations(live.db)).toEqual(storedAnnotations(copy.db));
    expect(articleRow(live.db)).toMatchObject({ version: 2, revid: REVID, schema_version: 4, n_formalized: 1, n_partial: 1, n_not_formalized: 0 });
    expect(archives(live)[0]).toMatchObject({ annotations: bytes, pending_proposals: proposal, rejected_proposals: ' [ ] ' });
    expect(live.db.prepare('SELECT proposal,last_reviewed_version FROM moderation_state').get()).toMatchObject({ proposal, last_reviewed_version: 2 });
    expect(live.db.prepare('SELECT source_revid,schema_version FROM revisions ORDER BY id').all()).toEqual([
      { source_revid: null, schema_version: null }, { source_revid: REVID, schema_version: 3 }, { source_revid: REVID, schema_version: 4 },
    ]);
    expect(live.db.prepare('SELECT user_id,kind FROM revisions ORDER BY id DESC LIMIT 1').get()).toMatchObject({ user_id: 'u-admin', kind: 'native-migration' });
    expect(revisionCount(live.db)).toBe(3);
    expect((await apply(live)).status).toBe(200);
    expect(revisionCount(live.db)).toBe(3); expect(archives(live)).toHaveLength(1);
    expect(live.db.prepare("SELECT value FROM site_settings WHERE key='slug_editing'").get()).toMatchObject({ value: 'off' });
  });
  it.each([
    [{}, 401], [{ user: 'u-human' }, 403], [{ user: 'u-patroller' }, 403],
    [{ user: 'u-blocked' }, 401], [{ bearer: PIPELINE_TOKEN }, 403],
    [{ user: 'u-admin', origin: 'https://evil.example' }, 403], [{ user: 'u-admin', origin: null }, 403],
  ] as const)('refuses unauthorized requests %j', async (options, status) => {
    const h = setup();
    expect((await post(h.env, path, {}, options)).status).toBe(status);
    expect(archives(h)).toEqual([]); expect(articleRow(h.db)?.version).toBe(1);
  });
  it('requires exact preview evidence and refuses supplied replacement data', async () => {
    const h = setup({ siteWriteMode: 'locked' });
    for (const request of [{ apply: true }, { ...await preview(h), apply: true, snapshot: '0'.repeat(64) }]) {
      expect((await post(h.env, path, request, { user: 'u-admin' })).status).toBe(409);
    }
    for (const request of [{ annotations: [] }, { apply: 'yes' }, null, []]) {
      expect((await post(h.env, path, request, { user: 'u-admin' })).status).toBe(400);
    }
    expect(archives(h)).toEqual([]);
    expect((await post(h.env, path, { padding: 'x'.repeat(4097) }, { user: 'u-admin' })).status).toBe(413);
    expect((await post(setup({ limiterAllows: false }).env, path, {}, { user: 'u-admin' })).status).toBe(429);
  });
  it('refuses invalid legacy statuses and never changes them heuristically', async () => {
    const h = setup({ siteWriteMode: 'locked' });
    h.db.prepare('UPDATE articles SET annotations=?').run('[{"status":"not_applicable"}]');
    const old = articleRow(h.db);
    expect((await post(h.env, path, {}, { user: 'u-admin' })).status).toBe(422);
    expect(articleRow(h.db)).toEqual(old); expect(archives(h)).toEqual([]);
  });
  it.each(['version', 'annotations', 'source', 'proposal', 'rejected', 'review', 'history', 'unlock', 'delete'])(
    'rolls back all migration side effects on a %s race inside the batch', async mutation => {
      const h = setup({ siteWriteMode: 'locked' });
      h.db.prepare('INSERT INTO moderation_state (slug,proposal,rejected_proposals,last_reviewed_version) VALUES (?,?,?,1)').run(SLUG, '[]', '[]');
      const plan = await preview(h);
      const realBatch = h.env.DB.batch.bind(h.env.DB);
      let changesBefore = 0; let revisionsBefore = 0;
      h.env.DB.batch = (async (statements: D1PreparedStatement[]) => {
        if (mutation === 'version') h.db.prepare('UPDATE articles SET version=version+1').run();
        if (mutation === 'annotations') h.db.prepare("UPDATE articles SET annotations=annotations || ' '").run();
        if (mutation === 'source') h.db.prepare('UPDATE articles SET revid=revid+1').run();
        if (mutation === 'proposal') h.db.prepare("UPDATE moderation_state SET proposal='[{}]'").run();
        if (mutation === 'rejected') h.db.prepare("UPDATE moderation_state SET rejected_proposals='[{}]'").run();
        if (mutation === 'review') h.db.prepare('UPDATE moderation_state SET last_reviewed_version=0').run();
        if (mutation === 'history') h.db.prepare("INSERT INTO revisions (slug,annotations,kind,created_at) VALUES (?,'[]','other',1)").run(SLUG);
        if (mutation === 'unlock') h.db.prepare("UPDATE site_settings SET value='open' WHERE key='site_write_mode'").run();
        if (mutation === 'delete') {
          h.db.prepare('DELETE FROM moderation_state WHERE slug=?').run(SLUG);
          h.db.prepare('DELETE FROM revisions WHERE slug=?').run(SLUG);
          h.db.prepare('DELETE FROM articles WHERE slug=?').run(SLUG);
        }
        changesBefore = h.db.prepare('SELECT total_changes() AS n').get()!.n as number;
        revisionsBefore = revisionCount(h.db);
        return realBatch(statements);
      }) as typeof h.env.DB.batch;
      const response = await apply(h, plan);
      expect(response.status).toBe(409);
      expect(archives(h)).toEqual([]); expect(revisionCount(h.db)).toBe(revisionsBefore);
      expect(h.db.prepare('SELECT total_changes() AS n').get()!.n).toBe(changesBefore);
      if (mutation !== 'delete') expect(articleRow(h.db)?.schema_version).toBe(3);
    });
  it('previews and restores unchanged legacy bytes under a new version, preserving both histories', async () => {
    const h = setup({ slugEditing: false, siteWriteMode: 'locked' });
    const before = articleRow(h.db)!;
    const exact = JSON.stringify(JSON.parse(before.annotations), null, 3) + '\n';
    h.db.prepare('UPDATE articles SET annotations=?').run(exact);
    h.db.prepare('INSERT INTO moderation_state (slug,proposal,last_reviewed_version) VALUES (?,?,1)').run(SLUG, ' [ ] ');
    expect((await apply(h)).status).toBe(200);
    const native = articleRow(h.db)!;
    const response = await post(h.env, path, { rollback: true }, { user: 'u-admin' });
    expect(response.status).toBe(200);
    const plan = await response.json() as { base_version: number; source_revid: number; snapshot: string };
    expect(articleRow(h.db)).toEqual(native); expect(revisionCount(h.db)).toBe(3);
    const options = { rollback: true, apply: true, base_version: plan.base_version, source_revid: plan.source_revid, snapshot: plan.snapshot };
    const restored = await post(h.env, path, options, { user: 'u-admin' });
    expect(restored.status).toBe(200);
    expect(await restored.json()).toMatchObject({ operation: 'restore-legacy', schema_version: 3, applied: true });
    expect(articleRow(h.db)).toMatchObject({ schema_version: 3, version: 3, annotations: exact, revid: REVID });
    expect(revisionCount(h.db)).toBe(5);
    expect(archives(h)).toHaveLength(2);
    expect(archives(h)[1]).toMatchObject({ schema_version: 4, annotations: native.annotations });
    expect(h.db.prepare('SELECT proposal,last_reviewed_version FROM moderation_state').get()).toMatchObject({ proposal: ' [ ] ', last_reviewed_version: 3 });
    expect((await post(h.env, path, options, { user: 'u-admin' })).status).toBe(409);
    expect(revisionCount(h.db)).toBe(5);
    // Restoring is not a reset of the content version or a ban on another reviewed migration.
    expect((await apply(h)).status).toBe(200);
    expect(articleRow(h.db)).toMatchObject({ schema_version: 4, version: 4, annotations: native.annotations });
  });
  it('keeps generic history restore from silently crossing the schema boundary after rollback', async () => {
    const h = setup({ siteWriteMode: 'locked' });
    h.db.prepare("UPDATE revisions SET kind='pipeline',meta=? WHERE id=1").run(JSON.stringify({ run_id: 'before-migration' }));
    expect((await apply(h)).status).toBe(200);
    const response = await post(h.env, path, { rollback: true }, { user: 'u-admin' });
    const plan = await response.json() as { base_version: number; source_revid: number; snapshot: string };
    expect((await post(h.env, path, { rollback: true, apply: true, base_version: plan.base_version, source_revid: plan.source_revid, snapshot: plan.snapshot }, { user: 'u-admin' })).status).toBe(200);
    const current = articleRow(h.db);
    expect((await post(h.env, `/api/article/${SLUG}/revert/3`, {}, { user: 'u-admin' })).status).toBe(409);
    const run = await post(h.env, '/api/admin/revert-run/before-migration', {}, { user: 'u-admin' });
    expect(await run.json()).toMatchObject({ reverted: [], skipped: [{ slug: SLUG, reason: 'human-edited-since' }] });
    expect(articleRow(h.db)).toEqual(current); expect(revisionCount(h.db)).toBe(5);
  });
  it.each(['edit', 'history', 'proposal', 'archive'])('refuses restoration after a later %s', async mutation => {
    const h = setup({ siteWriteMode: 'locked' });
    expect((await apply(h)).status).toBe(200);
    if (mutation === 'edit') h.db.prepare('UPDATE articles SET version=version+1').run();
    if (mutation === 'history') h.db.prepare("INSERT INTO revisions (slug,annotations,kind,created_at) VALUES (?,'[]','edit',1)").run(SLUG);
    if (mutation === 'proposal') h.db.prepare("INSERT INTO moderation_state (slug,proposal) VALUES (?,'[{}]')").run(SLUG);
    if (mutation === 'archive') h.db.prepare("UPDATE companion_migrations SET annotations='[]'").run();
    const current = articleRow(h.db); const n = revisionCount(h.db);
    expect((await post(h.env, path, { rollback: true }, { user: 'u-admin' })).status).toBe(409);
    expect(articleRow(h.db)).toEqual(current); expect(revisionCount(h.db)).toBe(n); expect(archives(h)).toHaveLength(1);
  });
  it.each(['article', 'archive'])('guards a %s race after the restore preview', async mutation => {
    const h = setup({ siteWriteMode: 'locked' });
    expect((await apply(h)).status).toBe(200);
    const response = await post(h.env, path, { rollback: true }, { user: 'u-admin' });
    const plan = await response.json() as { base_version: number; source_revid: number; snapshot: string };
    const realBatch = h.env.DB.batch.bind(h.env.DB);
    h.env.DB.batch = (async (statements: D1PreparedStatement[]) => {
      if (mutation === 'article') h.db.prepare('UPDATE articles SET version=version+1').run();
      else h.db.prepare("UPDATE companion_migrations SET annotations='[]'").run();
      return realBatch(statements);
    }) as typeof h.env.DB.batch;
    const restored = await post(h.env, path, { rollback: true, apply: true, base_version: plan.base_version, source_revid: plan.source_revid, snapshot: plan.snapshot }, { user: 'u-admin' });
    expect(restored.status).toBe(409);
    expect(articleRow(h.db)?.schema_version).toBe(4); expect(revisionCount(h.db)).toBe(3);
    expect(archives(h)).toHaveLength(1);
  });
  it('rolls back the archive and checkpoint if a later statement fails', async () => {
    const h = setup({ siteWriteMode: 'locked' });
    const old = articleRow(h.db);
    h.db.exec("CREATE TRIGGER refuse_update BEFORE UPDATE ON articles BEGIN SELECT RAISE(ABORT,'simulated storage failure'); END");
    expect((await apply(h)).status).toBe(500);
    expect(articleRow(h.db)).toEqual(old); expect(archives(h)).toEqual([]); expect(revisionCount(h.db)).toBe(1);
  });
});
