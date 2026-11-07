/** Admin-only, preview-first, per-article structural migration. The copied
 * snapshot rehearsal and this route use the same pure conversion planner. */
import type { Hono } from 'hono';
import type { Env } from './env.js';
import { getUser } from './auth.js';
import { siteWriteMode } from './site-settings.js';
import { checkOrigin, readJsonCapped, MAX_JSON_BODY_BYTES } from './security.js';
import { planNativeMigration, missingMigrationIdInput, type MigrationArticle } from './native-migration.js';

interface Snapshot extends MigrationArticle {
  proposal: string | null; rejected_proposals: string | null;
  last_reviewed_version: number | null; prior_revision: number | null;
}
interface RollbackArchive {
  id: number; slug: string; article_version: number; source_revid: number; schema_version: number;
  annotations: string; pending_proposals: string | null; rejected_proposals: string | null;
}
interface MigrationRevision {
  annotations: string; source_revid: number | null; schema_version: number | null;
  kind: string; meta: string | null; parent_id: number | null;
}
async function preparePlan(row: MigrationArticle) {
  const original: unknown = JSON.parse(row.annotations);
  if (!Array.isArray(original)) throw new Error('annotations must be an array');
  const ids = await Promise.all(original.map((item, index) => sha256(missingMigrationIdInput(row.slug, index, item))));
  return planNativeMigration(row, (_item, index) => ids[index].slice(0, 12));
}
async function sha256(text: string): Promise<string> {
  const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(bytes)].map(byte => byte.toString(16).padStart(2, '0')).join('');
}
function stale(error: unknown): boolean {
  for (let current: unknown = error, n = 0; current && n < 6; n++) {
    if (current instanceof Error && current.message.includes('NOT NULL constraint failed: companion_migrations.source_revid')) return true;
    current = typeof current === 'object' && 'cause' in current ? current.cause : null;
  }
  return false;
}
export function registerNativeMigrationRoutes(app: Hono<{ Bindings: Env }>): void {
  app.post('/api/admin/article/:slug/native-migration', async c => {
    c.header('Cache-Control', 'no-store');
    const badOrigin = checkOrigin(c);
    if (badOrigin) return badOrigin;
    if (!c.req.header('Origin')) return c.json({ ok: false, error: 'same-origin request required' }, 403);
    const user = await getUser(c);
    if (!user) return c.json({ ok: false, error: 'login required' }, 401);
    if (user.role !== 'admin') return c.json({ ok: false, error: 'admin session required' }, 403);
    if (!(await c.env.EDIT_LIMITER.limit({ key: `native-migration:${user.id}` })).success) return c.json({ ok: false, error: 'rate limited' }, 429);
    const body = await readJsonCapped(c, MAX_JSON_BODY_BYTES.nativeMigration);
    if (!body.ok) return body.response;
    const request = body.value as Record<string, unknown>;
    if (!request || typeof request !== 'object' || Array.isArray(request)
      || Object.keys(request).some(key => !['apply', 'base_version', 'source_revid', 'snapshot', 'rollback'].includes(key))
      || (request.apply !== undefined && typeof request.apply !== 'boolean')
      || (request.rollback !== undefined && typeof request.rollback !== 'boolean')) {
      return c.json({ ok: false, error: 'expected migration options only' }, 400);
    }
    const slug = c.req.param('slug');
    // One read captures annotations, source context, pending decisions and history.
    const row = (await c.env.DB.prepare(`SELECT a.slug,a.wikipedia_title,a.revid,a.annotations,a.version,a.schema_version,
      m.proposal,m.rejected_proposals,m.last_reviewed_version,
      (SELECT MAX(id) FROM revisions WHERE slug=a.slug) AS prior_revision
      FROM articles a LEFT JOIN moderation_state m ON m.slug=a.slug WHERE a.slug=?`).bind(slug).all<Snapshot>()).results[0];
    if (!row) return c.json({ ok: false, error: 'unknown slug' }, 404);
    let plan: ReturnType<typeof planNativeMigration>;
    try {
      plan = await preparePlan(row);
    } catch (error) {
      return c.json({ ok: false, error: 'migration requires review', detail: String(error) }, 422);
    }
    const rollback = request.rollback === true;
    let restore: RollbackArchive | undefined;
    if (rollback) {
      const history = (await c.env.DB.prepare(
        'SELECT annotations,source_revid,schema_version,kind,meta,parent_id FROM revisions WHERE slug=? AND id=?'
      ).bind(slug, row.prior_revision).all<MigrationRevision>()).results[0];
      restore = (await c.env.DB.prepare(`SELECT id,slug,article_version,source_revid,schema_version,
        annotations,pending_proposals,rejected_proposals FROM companion_migrations
        WHERE slug=? AND article_version=? AND schema_version=3`).bind(slug, row.version - 1).all<RollbackArchive>()).results[0];
      const checkpoint = history?.parent_id == null ? undefined : (await c.env.DB.prepare(
        'SELECT annotations,source_revid,schema_version,kind,meta,parent_id FROM revisions WHERE slug=? AND id=?'
      ).bind(slug, history.parent_id).all<MigrationRevision>()).results[0];
      if (row.schema_version !== 4 || !restore || !history || !checkpoint
        || history.kind !== 'native-migration' || history.annotations !== row.annotations
        || history.schema_version !== 4 || history.source_revid !== row.revid
        || checkpoint.kind !== 'native-migration-checkpoint' || checkpoint.schema_version !== 3
        || checkpoint.annotations !== restore.annotations || checkpoint.source_revid !== restore.source_revid
        || restore.source_revid !== row.revid || restore.pending_proposals !== row.proposal
        || restore.rejected_proposals !== row.rejected_proposals) {
        return c.json({ ok: false, error: 'migration rollback requires unchanged migrated state; review later edits explicitly' }, 409);
      }
      let priorPlan: ReturnType<typeof planNativeMigration>;
      try {
        priorPlan = await preparePlan({ ...row, version: restore.article_version, revid: restore.source_revid,
          schema_version: restore.schema_version, annotations: restore.annotations });
      } catch (error) {
        return c.json({ ok: false, error: 'migration archive requires review', detail: String(error) }, 422);
      }
      if (priorPlan.annotations !== row.annotations) {
        return c.json({ ok: false, error: 'migration no longer matches its archived conversion' }, 409);
      }
      plan = { ...priorPlan, annotations: restore.annotations,
        report: { ...priorPlan.report, version: row.version + 1 } };
    }
    const operation = rollback ? 'restore-legacy' : 'migrate-native';
    const snapshot = await sha256(JSON.stringify({ schema: 'wikilean.native-migration-plan/v1', operation, row, restore, result: plan.annotations, report: plan.report }));

    const preview = { ok: true, operation, schema_version: rollback ? 3 : 4, base_version: row.version, source_revid: row.revid, snapshot, report: plan.report };
    if (request.apply !== true) return c.json({ ...preview, applied: false });
    if (request.base_version !== row.version || request.source_revid !== row.revid || request.snapshot !== snapshot) {
      return c.json({ ok: false, error: 'stale migration preview; preview again' }, 409);
    }
    if (!plan.report.migrated) return c.json({ ...preview, applied: false });
    if (await siteWriteMode(c.env) !== 'locked') return c.json({ ok: false, error: 'lock community writes before migration' }, 409);
    const now = Date.now();
    const db = c.env.DB;
    // The first statement deliberately violates a NOT NULL constraint on ANY
    // lost snapshot (including deletion). D1 rolls the whole batch back. Using
    // a zero-row UPDATE would allow unrelated archive/history inserts to commit.
    const archive = db.prepare(`INSERT INTO companion_migrations
      (slug,article_version,source_revid,schema_version,annotations,pending_proposals,rejected_proposals,diagnostics,migrated_at)
      VALUES (?,?,CASE WHEN
        EXISTS (SELECT 1 FROM articles WHERE slug=? AND version=? AND revid IS ?
          AND wikipedia_title=? AND schema_version=? AND annotations=?)
        AND (SELECT proposal FROM moderation_state WHERE slug=?) IS ?
        AND (SELECT rejected_proposals FROM moderation_state WHERE slug=?) IS ?
        AND (SELECT last_reviewed_version FROM moderation_state WHERE slug=?) IS ?
        AND (SELECT MAX(id) FROM revisions WHERE slug=?) IS ?
        AND (SELECT value FROM site_settings WHERE key='site_write_mode') = 'locked'
        AND (? IS NULL OR EXISTS (SELECT 1 FROM companion_migrations WHERE id=? AND slug=?
          AND article_version=? AND source_revid=? AND schema_version=3 AND annotations=?
          AND pending_proposals IS ? AND rejected_proposals IS ?))
        THEN ? ELSE NULL END,?,?,?,?,?,?)`).bind(
          slug, row.version, slug, row.version, row.revid, row.wikipedia_title, row.schema_version, row.annotations,
          slug, row.proposal, slug, row.rejected_proposals, slug, row.last_reviewed_version,
          slug, row.prior_revision,
          restore?.id ?? null, restore?.id ?? null, slug, restore?.article_version ?? null,
          restore?.source_revid ?? null, restore?.annotations ?? null,
          restore?.pending_proposals ?? null, restore?.rejected_proposals ?? null,
          row.revid, row.schema_version, row.annotations,
          row.proposal, row.rejected_proposals, JSON.stringify(plan.report.diagnostics), now);
    const nextSchema = rollback ? 3 : 4;
    const revisionKind = rollback ? 'native-migration-rollback' : 'native-migration';
    try {
      await db.batch([
        archive,
        db.prepare(`INSERT INTO revisions
          (slug,user_id,annotations,source_revid,schema_version,comment,kind,parent_id,created_at)
          VALUES (?,?,?,?,?,?,'native-migration-checkpoint',?,?)`).bind(
            slug, user.id, row.annotations, row.revid, row.schema_version,
            rollback ? 'observed pre-rollback native state' : 'observed pre-native state', row.prior_revision, now),
        db.prepare(`UPDATE articles SET annotations=?,schema_version=?,version=version+1,
          n_formalized=?,n_partial=?,n_not_formalized=?,updated_at=? WHERE slug=?`).bind(
            plan.annotations, nextSchema, plan.counts.formalized, plan.counts.partial, plan.counts.not_formalized, now, slug),
        db.prepare(`INSERT INTO revisions
          (slug,user_id,annotations,source_revid,schema_version,comment,kind,meta,parent_id,created_at)
          VALUES (?,?,?,?,?,?,?,?,(SELECT MAX(id) FROM revisions WHERE slug=?),?)`).bind(
            slug, user.id, plan.annotations, row.revid, nextSchema,
            rollback ? 'restore unchanged pre-native state' : 'native companion migration', revisionKind,
            JSON.stringify({ previous_version: row.version, snapshot, operation, diagnostics: plan.report.diagnostics }), slug, now),
        db.prepare('UPDATE moderation_state SET last_reviewed_version=? WHERE slug=? AND last_reviewed_version=?')
          .bind(row.version + 1, slug, row.version),
      ]);
    } catch (error) {
      if (stale(error)) return c.json({ ok: false, error: 'snapshot changed during migration; preview again' }, 409);
      throw error;
    }
    return c.json({ ...preview, applied: true });
  });
}
