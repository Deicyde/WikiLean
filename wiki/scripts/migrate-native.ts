/** Offline migration of a COPIED SQLite snapshot. No D1 binding, network, or
 * in-place CLI operation. Run with: npx tsx scripts/migrate-native.ts INPUT OUTPUT.
 * The library entry point is also used by hermetic SQLite integration tests. */
import { DatabaseSync, backup } from 'node:sqlite';
import { createHash, randomUUID } from 'node:crypto';
import { readFileSync, existsSync, linkSync, unlinkSync, mkdirSync, chmodSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { NATIVE_SCHEMA_VERSION } from '../src/native-companion.js';
import { planNativeMigration, missingMigrationIdInput, type MigrationArticle } from '../src/native-migration.js';

export function migrateLocalDatabase(db: DatabaseSync, now = Date.now()) {
  const report: Array<{ slug: string; version: number; migrated: boolean; items: number; links: number; unlinked: number; diagnostics: unknown[] }> = [];
  db.exec('BEGIN IMMEDIATE');
  try {
    const columns = db.prepare('PRAGMA table_info(revisions)').all() as Array<{ name: string }>;
    if (!columns.some(column => column.name === 'source_revid')) {
      db.exec(readFileSync(new URL('../migrations/0014_native_companions.sql', import.meta.url), 'utf8'));
    }
    const rows = db.prepare('SELECT slug, wikipedia_title, revid, annotations, version, schema_version FROM articles ORDER BY slug').all() as unknown as MigrationArticle[];
    for (const row of rows) {
      const plan = planNativeMigration(row, (item, index) =>
        createHash('sha256').update(missingMigrationIdInput(row.slug, index, item)).digest('hex').slice(0, 12));
      if (!plan.report.migrated) { report.push(plan.report); continue; }
      const { annotations, counts } = plan;
      const pending = db.prepare('SELECT proposal, rejected_proposals FROM moderation_state WHERE slug=?').get(row.slug) as { proposal: string | null; rejected_proposals: string | null } | undefined;
      db.prepare('INSERT INTO companion_migrations (slug,article_version,source_revid,schema_version,annotations,pending_proposals,rejected_proposals,diagnostics,migrated_at) VALUES (?,?,?,?,?,?,?,?,?)')
        .run(row.slug, row.version, row.revid, row.schema_version, row.annotations, pending?.proposal ?? null, pending?.rejected_proposals ?? null, JSON.stringify(plan.report.diagnostics), now);
      const prior = db.prepare('SELECT MAX(id) AS id FROM revisions WHERE slug=?').get(row.slug) as { id: number | null };
      // This checkpoint has observed source context. Older snapshots retain NULL
      // pins; equality with current bytes is not evidence about their old source.
      const checkpoint = db.prepare('INSERT INTO revisions (slug,annotations,source_revid,schema_version,comment,kind,parent_id,created_at) VALUES (?,?,?,?,?,?,?,?)')
        .run(row.slug, row.annotations, row.revid, row.schema_version, 'observed pre-native state', 'native-migration-checkpoint', prior.id, now);
      const changed = db.prepare('UPDATE articles SET annotations=?,schema_version=?,version=version+1,n_formalized=?,n_partial=?,n_not_formalized=?,updated_at=? WHERE slug=? AND version=? AND annotations=? AND revid=?')
        .run(annotations, NATIVE_SCHEMA_VERSION, counts.formalized, counts.partial, counts.not_formalized, now, row.slug, row.version, row.annotations, row.revid);
      if (changed.changes !== 1) throw new Error(`${row.slug}: snapshot changed during migration`);
      db.prepare('INSERT INTO revisions (slug,annotations,source_revid,schema_version,comment,kind,meta,parent_id,created_at) VALUES (?,?,?,?,?,?,?,?,?)')
        .run(row.slug, annotations, row.revid, NATIVE_SCHEMA_VERSION, 'native companion migration', 'native-migration',
          JSON.stringify({ previous_version: row.version, diagnostics: plan.report.diagnostics }), Number(checkpoint.lastInsertRowid), now);
      // Structural conversion is not another review, nor an unreviewed edit.
      db.prepare('UPDATE moderation_state SET last_reviewed_version=? WHERE slug=? AND last_reviewed_version=?').run(row.version + 1, row.slug, row.version);
      report.push(plan.report);
    }
    db.exec('COMMIT');
  } catch (error) {
    db.exec('ROLLBACK');
    throw error;
  }
  return { schema: 'wikilean.native-migration-report/v1', articles: report };
}

export async function migrateDatabaseCopy(input: string, output: string) {
  const sourcePath = resolve(input); const outputPath = resolve(output);
  if (sourcePath === outputPath || existsSync(outputPath)) throw new Error('output must be a new file distinct from the input');
  mkdirSync(dirname(outputPath), { recursive: true });
  const temporary = `${outputPath}.${randomUUID()}.tmp`;
  const source = new DatabaseSync(sourcePath, { readOnly: true });
  let destination: DatabaseSync | undefined;
  try {
    await backup(source, temporary);
    chmodSync(temporary, 0o600);
    destination = new DatabaseSync(temporary);
    const report = migrateLocalDatabase(destination);
    const check = destination.prepare('PRAGMA integrity_check').get() as Record<string, unknown>;
    if (Object.values(check)[0] !== 'ok') throw new Error('migrated database failed SQLite integrity check');
    destination.close(); destination = undefined;
    // Same-directory hard link publishes atomically and cannot replace an
    // existing file or directory even if another process wins the race.
    linkSync(temporary, outputPath);
    return report;
  } finally {
    destination?.close(); source.close();
    if (existsSync(temporary)) unlinkSync(temporary);
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  if (process.argv.length !== 4) {
    console.error('Usage: tsx scripts/migrate-native.ts INPUT.sqlite OUTPUT.sqlite');
    process.exitCode = 2;
  } else {
    migrateDatabaseCopy(process.argv[2], process.argv[3]).then(report => console.log(JSON.stringify(report, null, 2))).catch(error => {
      console.error(String(error)); process.exitCode = 1;
    });
  }
}
