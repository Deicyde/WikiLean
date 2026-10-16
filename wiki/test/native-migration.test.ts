import { describe, it, expect } from 'vitest';
import { DatabaseSync, backup } from 'node:sqlite';
import { mkdtempSync, readFileSync, readdirSync, writeFileSync, mkdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { migrateLocalDatabase, migrateDatabaseCopy } from '../scripts/migrate-native.js';
import { setup, articleRow, storedAnnotations, latestRevision, revisionCount, SLUG, REVID } from './helpers/harness.js';
import { buildSeedRows } from '../src/seed/build.js';
import { validateNativeItems, type NativeItem } from '../src/native-companion.js';

describe('offline native migration', () => {
  it('archives exact original bytes and proposal state, adds observed source checkpoints, and is idempotent', () => {
    const h = setup();
    const old = storedAnnotations(h.db); old[0].provenance = 'human'; old.push({ id: 'cccccccccccc', status: 'rejected', provenance: 'human', anchor: { snippet: 'veto' } });
    const bytes = JSON.stringify(old, null, 3) + '\n';
    h.db.prepare('UPDATE articles SET annotations=? WHERE slug=?').run(bytes, SLUG);
    const proposal = '[ {"proposalId":"abc123abc123","annotationId":"aaaaaaaaaaaa","fields":{"mathlib":{"decl":"old-delta"}}} ]';
    h.db.prepare('INSERT INTO moderation_state (slug,proposal,rejected_proposals,last_reviewed_version) VALUES (?,?,?,1)').run(SLUG, proposal, '[]');
    const report = migrateLocalDatabase(h.db, 1000);
    expect(report.articles[0]).toMatchObject({ items: 3, links: 1, unlinked: 2, version: 2, migrated: true });
    const archive = h.db.prepare('SELECT annotations,pending_proposals FROM companion_migrations').get();
    expect(archive).toMatchObject({ annotations: bytes, pending_proposals: proposal });
    expect(h.db.prepare('SELECT proposal,last_reviewed_version FROM moderation_state WHERE slug=?').get(SLUG)).toMatchObject({ proposal, last_reviewed_version: 2 });
    expect(articleRow(h.db)).toMatchObject({ schema_version: 4, n_formalized: 1, n_partial: 1, n_not_formalized: 0 });
    const native = storedAnnotations(h.db);
    expect(native.map(item => item.id)).toEqual(old.map(item => item.id));
    expect(native[0].provenance).toBe('human'); expect(native[2].status).toBe('rejected');
    expect(native[0].legacy).toEqual(old[0]); expect(validateNativeItems(native)).toEqual([]);
    expect((native[0] as unknown as NativeItem).links[0].annotation.object).toEqual([{ type: 'lean', uri: 'https://github.com/leanprover-community/mathlib4',
      location: { declaration: 'AddCommGroup', module: 'Mathlib.Algebra.Group.Defs' } }]);
    expect(h.db.prepare('SELECT source_revid FROM revisions WHERE id=1').get()).toMatchObject({ source_revid: null });
    expect(h.db.prepare('SELECT source_revid,schema_version FROM revisions WHERE id=2').get()).toMatchObject({ source_revid: REVID, schema_version: 3 });
    expect(h.db.prepare('SELECT source_revid,schema_version FROM revisions WHERE id=3').get()).toMatchObject({ source_revid: REVID, schema_version: 4 });
    const final = articleRow(h.db); const rev = latestRevision(h.db);
    expect(migrateLocalDatabase(h.db).articles[0].migrated).toBe(false);
    expect(articleRow(h.db)).toEqual(final); expect(latestRevision(h.db)).toEqual(rev);
  });
  it('assigns missing application identities deterministically while archiving the pre-ID object', () => {
    const first = setup(); const second = setup();
    const annotations = [{ status: 'not_formalized', label: 'work', anchor: { snippet: 'x' } }];
    for (const h of [first, second]) {
      h.db.prepare('UPDATE articles SET annotations=? WHERE slug=?').run(JSON.stringify(annotations), SLUG);
      migrateLocalDatabase(h.db);
    }
    expect(storedAnnotations(first.db)[0].id).toBe(storedAnnotations(second.db)[0].id);
    expect(storedAnnotations(first.db)[0].legacy).toEqual(annotations[0]);
  });
  it('rolls the complete batch back on an invalid article, leaving originals and history intact', () => {
    const h = setup(); const before = articleRow(h.db); const revisions = revisionCount(h.db);
    h.db.prepare('INSERT INTO articles (slug,wikipedia_title,display_title,revid,annotations,created_at,updated_at) VALUES (?,?,?,?,?,?,?)')
      .run('Z_invalid', 'Z invalid', 'Z invalid', REVID, '[{"status":"unknown"}]', 1, 1);
    expect(() => migrateLocalDatabase(h.db)).toThrow('status');
    expect(articleRow(h.db)).toEqual(before); expect(revisionCount(h.db)).toBe(revisions);
    expect(h.db.prepare('SELECT COUNT(*) AS n FROM companion_migrations').get()).toMatchObject({ n: 0 });
  });
  it('refuses unknown application formats, current source pins and unsafe numeric metadata', () => {
    for (const mutation of [
      (db: DatabaseSync) => db.prepare('UPDATE articles SET schema_version=5').run(),
      (db: DatabaseSync) => db.prepare('UPDATE articles SET revid=NULL').run(),
      (db: DatabaseSync) => db.prepare('UPDATE articles SET annotations=?').run('[{"status":"partial","unknown":9007199254740993}]'),
    ]) {
      const h = setup(); mutation(h.db); const before = articleRow(h.db);
      expect(() => migrateLocalDatabase(h.db)).toThrow(); expect(articleRow(h.db)).toEqual(before);
    }
  });
  it('does not publish native expansion that cannot subsequently pass Worker save limits', () => {
    const h = setup();
    const original = Array.from({ length: 100 }, (_, index) => ({ id: index.toString(16).padStart(12, '0'), status: 'partial', note: 'x'.repeat(1500), anchor: { snippet: 'span' } }));
    const bytes = JSON.stringify(original); expect(bytes.length).toBeLessThan(256 * 1024);
    h.db.prepare('UPDATE articles SET annotations=?').run(bytes); const before = articleRow(h.db);
    expect(() => migrateLocalDatabase(h.db)).toThrow('Worker save limits');
    expect(articleRow(h.db)).toEqual(before);
    expect(h.db.prepare('SELECT COUNT(*) AS n FROM companion_migrations').get()).toMatchObject({ n: 0 });
  });
  it('publishes a separate database copy and never overwrites any existing destination', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'wikilean-native-'));
    try {
      const h = setup(); const input = join(dir, 'input.sqlite'); const output = join(dir, 'native.sqlite');
      await backup(h.db, input); const original = readFileSync(input);
      await migrateDatabaseCopy(input, output);
      expect(readFileSync(input)).toEqual(original);
      const migrated = new DatabaseSync(output, { readOnly: true });
      expect(articleRow(migrated)?.schema_version).toBe(4); migrated.close();
      const before = readFileSync(output);
      await expect(migrateDatabaseCopy(input, output)).rejects.toThrow('new file');
      expect(readFileSync(output)).toEqual(before);
      await expect(migrateDatabaseCopy(input, input)).rejects.toThrow('new file');
    } finally { rmSync(dir, { recursive: true, force: true }); }
  });
  it('leaves no published output after a failed copied migration', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'wikilean-native-'));
    try {
      const h = setup(); h.db.exec('UPDATE articles SET revid=NULL');
      const input = join(dir, 'input.sqlite'); await backup(h.db, input);
      await expect(migrateDatabaseCopy(input, join(dir, 'output.sqlite'))).rejects.toThrow();
      expect(readdirSync(dir)).toEqual(['input.sqlite']);
    } finally { rmSync(dir, { recursive: true, force: true }); }
  });
  it('prevents legacy seed tooling from relabelling native sidecars as old storage', () => {
    const dir = mkdtempSync(join(tmpdir(), 'wikilean-seed-'));
    try {
      mkdirSync(join(dir, 'annotations'));
      writeFileSync(join(dir, 'annotations', 'example.json'), JSON.stringify({ schema_version: 4, annotations: [] }));
      expect(() => buildSeedRows(dir)).toThrow('native companion');
    } finally { rmSync(dir, { recursive: true, force: true }); }
  });
});
