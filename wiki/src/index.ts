import { Hono, type Context } from "hono";
import { drizzle, type DrizzleD1Database } from "drizzle-orm/d1";
import { eq, and, desc, sql } from "drizzle-orm";
import { articles, revisions, users, moderationState, type ArticleRow } from "./db/schema.js";
import { absolutizeWikipediaUrls, wrapAnnotations } from "./engine/wrap.js";
import { renderArticlePage } from "./engine/page.js";
import { getWikipediaHtml } from "./wikipedia.js";
import { getUser, requireRole, registerAuthRoutes } from "./auth.js";
import { scheduled } from "./drift.js";
import { injectAuthAndEditor, historyPage, recentChangesPage } from "./pages.js";
import type { Annotation } from "./engine/types.js";
import type { Env } from "./env.js";
import {
  ANNOTATION_STATUSES,
  MAX_ANNOTATIONS,
  MAX_ANNOTATIONS_BYTES,
  MAX_FIELD_LEN,
  MAX_META_BYTES,
  MAX_TEXT_LEN,
  findLostHuman,
  stampProvenance,
  type AnnRecord,
} from "./validation.js";

const RESERVED = new Set([
  "assets",
  "api",
  "favicon.ico",
  "robots.txt",
  "sitemap.xml",
  "recent-changes",
  "login",
  "logout",
  "graph",
  "graph_data.json",
  "article-graph",
  "article-graph-data.json",
]);

const app = new Hono<{ Bindings: Env }>();
registerAuthRoutes(app);

type DB = DrizzleD1Database;

// Renders (and KV-caches) the anonymous base page for an article. Takes the
// already-SELECTed row so the cached base and the caller's injected editor
// model come from one consistent read (no double-read race with concurrent
// saves); db is still needed for the revid patch-back write below.
async function renderArticleBase(db: DB, env: Env, row: ArticleRow): Promise<string> {
  const slug = row.slug;

  // Bump the prefix when the engine's wrap behavior changes — old keys would
  // serve pre-change HTML. v6: wraps now carry data-provenance="ai|human" so
  // human-curated annotations can be visually distinguished. v7: post-XSS-
  // hardening + status-validation — evicts any XSS-poisoned/stale cached pages.
  // v8: tombstone skip — status='rejected' annotations are no longer wrapped
  // and drop out of header badge counts / anonymous client data.
  const cacheKey = `render:v8:${slug}:${row.version}`;
  const cached = await env.RENDER_CACHE.get(cacheKey);
  if (cached) return cached;

  const wp = await getWikipediaHtml(env.WP_HTML, slug, row.wikipediaTitle, row.revid);
  if (row.revid === null && wp.revid !== null) {
    await db.update(articles).set({ revid: wp.revid }).where(eq(articles.slug, slug));
  }

  const annotations = JSON.parse(row.annotations) as Annotation[];
  const src = absolutizeWikipediaUrls(wp.html);
  const { html: body, matched } = wrapAnnotations(src, annotations);
  const page = renderArticlePage({
    slug,
    displayTitle: row.displayTitle,
    wikipediaTitle: row.wikipediaTitle,
    body,
    annotations,
    matched,
    wpHtml: src,
  });
  await env.RENDER_CACHE.put(cacheKey, page, { expirationTtl: 60 * 60 * 24 * 30 });
  return page;
}

function annCount(json: string): number {
  try {
    const a = JSON.parse(json);
    return Array.isArray(a) ? a.length : 0;
  } catch {
    return 0;
  }
}

const STATUS_SET = new Set<string>(ANNOTATION_STATUSES);

// Server-side annotation validation (P0). Returns null if valid, else an
// error response to send. `annJson` is the pre-serialized array so callers
// don't double-stringify for the payload-size check.
function validateAnnotations(
  c: Context<{ Bindings: Env }>,
  annotations: unknown[],
  annJson: string,
): Response | null {
  // Payload caps first (cheap, bounds the work below).
  if (annJson.length > MAX_ANNOTATIONS_BYTES || annotations.length > MAX_ANNOTATIONS) {
    return c.json({ ok: false, error: "payload too large" }, 413);
  }
  for (const a of annotations) {
    if (typeof a !== "object" || a === null || Array.isArray(a)) {
      return c.json({ ok: false, error: "annotation must be an object" }, 400);
    }
    const ann = a as Record<string, unknown>;
    if (ann.status !== undefined && (typeof ann.status !== "string" || !STATUS_SET.has(ann.status))) {
      return c.json({ ok: false, error: "invalid status" }, 400);
    }
    // Length-cap free-text fields (flat + nested mathlib.*).
    const mathlib = (typeof ann.mathlib === "object" && ann.mathlib !== null ? ann.mathlib : {}) as Record<
      string,
      unknown
    >;
    // [name, value, cap]. Identifier/enum fields use MAX_FIELD_LEN; free-text
    // fields (label, note, proof_note) use the generous MAX_TEXT_LEN.
    const fields: Array<[string, unknown, number]> = [
      ["kind", ann.kind, MAX_FIELD_LEN],
      ["label", ann.label, MAX_TEXT_LEN],
      ["note", ann.note, MAX_TEXT_LEN],
      ["proof_note", ann.proof_note, MAX_TEXT_LEN],
      ["match_kind", ann.match_kind, MAX_FIELD_LEN],
      ["decl", ann.decl, MAX_FIELD_LEN],
      ["module", ann.module, MAX_FIELD_LEN],
      ["mathlib.decl", mathlib.decl, MAX_FIELD_LEN],
      ["mathlib.module", mathlib.module, MAX_FIELD_LEN],
      ["mathlib.match_kind", mathlib.match_kind, MAX_FIELD_LEN],
    ];
    for (const [name, v, cap] of fields) {
      if (typeof v === "string" && v.length > cap) {
        return c.json({ ok: false, error: `field ${name} too long` }, 400);
      }
    }
  }
  return null;
}

// Cheap CSRF defense-in-depth: block cross-site browser POSTs. Requests with no
// Origin header (non-browser/script clients, same-origin GETs) pass. Returns a
// 403 response to send, or null to proceed.
function checkOrigin(c: Context<{ Bindings: Env }>): Response | null {
  const origin = c.req.header("Origin");
  if (origin && origin !== new URL(c.req.url).origin) {
    return c.json({ ok: false, error: "cross-origin request rejected" }, 403);
  }
  return null;
}

// ---- recent changes (global patrol feed) ----
app.get("/recent-changes", async (c) => {
  const db = drizzle(c.env.DB);
  const rows = await db
    .select({
      id: revisions.id,
      slug: revisions.slug,
      userId: revisions.userId,
      comment: revisions.comment,
      createdAt: revisions.createdAt,
      displayTitle: articles.displayTitle,
      userName: users.name,
    })
    .from(revisions)
    .leftJoin(articles, eq(articles.slug, revisions.slug))
    .leftJoin(users, eq(users.id, revisions.userId))
    .orderBy(desc(revisions.createdAt))
    .limit(100);
  const html = recentChangesPage(
    rows.map((r) => ({
      slug: r.slug,
      displayTitle: r.displayTitle ?? r.slug,
      id: r.id,
      userId: r.userId,
      userName: r.userName,
      comment: r.comment,
      createdAt: r.createdAt,
    })),
  );
  return c.html(html);
});

// ---- per-article revision history ----
app.get("/:slug/history", async (c) => {
  const slug = c.req.param("slug");
  const db = drizzle(c.env.DB);
  const art = (await db.select().from(articles).where(eq(articles.slug, slug)).limit(1))[0];
  if (!art) return c.notFound();
  const revs = await db
    .select({
      id: revisions.id,
      userId: revisions.userId,
      comment: revisions.comment,
      createdAt: revisions.createdAt,
      annotations: revisions.annotations,
      userName: users.name,
    })
    .from(revisions)
    .leftJoin(users, eq(users.id, revisions.userId))
    .where(eq(revisions.slug, slug))
    .orderBy(desc(revisions.createdAt))
    .limit(200);
  const user = await getUser(c);
  const html = historyPage(
    slug,
    art.displayTitle,
    revs.map((r) => ({
      id: r.id,
      userId: r.userId,
      userName: r.userName,
      comment: r.comment,
      createdAt: r.createdAt,
      count: annCount(r.annotations),
    })),
    user !== null,
  );
  return c.html(html);
});

// ---- machine-readable article JSON (the pipeline read path; public) ----
// Same data anonymous readers already get in-page. The {.+\.json} suffix
// pattern wins over the catch-all /:slug routes by registration order, so a
// slug literally named "x.json" can't shadow this route (it'd be served at
// /api/article/x.json.json).
app.get("/api/article/:slug{.+\\.json}", async (c) => {
  const slug = c.req.param("slug").replace(/\.json$/, "");
  const db = drizzle(c.env.DB);
  const row = (await db.select().from(articles).where(eq(articles.slug, slug)).limit(1))[0];
  if (!row) return c.json({ ok: false, error: "unknown slug" }, 404);
  return c.json({
    slug: row.slug,
    wikipedia_title: row.wikipediaTitle,
    display_title: row.displayTitle,
    version: row.version,
    revid: row.revid,
    latest_revid: row.latestRevid,
    schema_version: row.schemaVersion,
    annotations: JSON.parse(row.annotations),
  });
});

// ---- save an edit (session login, or pipeline bearer with role 'bot') ----
app.post("/api/article/:slug", async (c) => {
  const originErr = checkOrigin(c);
  if (originErr) return originErr;
  const user = await getUser(c);
  if (!user) return c.json({ ok: false, error: "login required" }, 401);
  const isBot = user.role === "bot";
  const { success: allowed } = await c.env.EDIT_LIMITER.limit({ key: `edit:${user.id}` });
  if (!allowed) return c.json({ ok: false, error: "rate limited — slow down" }, 429);
  const slug = c.req.param("slug");
  const db = drizzle(c.env.DB);
  const row = (await db.select().from(articles).where(eq(articles.slug, slug)).limit(1))[0];
  if (!row) return c.json({ ok: false, error: "unknown slug" }, 404);

  let posted: {
    annotations?: unknown;
    comment?: unknown;
    base_version?: unknown;
    revid?: unknown;
    meta?: unknown;
  };
  try {
    posted = await c.req.json();
  } catch {
    return c.json({ ok: false, error: "bad json" }, 400);
  }
  if (!Array.isArray(posted.annotations)) return c.json({ ok: false, error: "missing annotations" }, 400);

  const postedJson = JSON.stringify(posted.annotations);
  const validationErr = validateAnnotations(c, posted.annotations, postedJson);
  if (validationErr) return validationErr;
  const postedAnnotations = posted.annotations as AnnRecord[];

  // Pipeline-only request fields. Bearer saves must rebase explicitly (no
  // back-compat carve-out) and may re-pin the article's Wikipedia revid and
  // attach run metadata for the revision log.
  let postedRevid: number | null = null;
  let metaJson: string | null = null;
  if (isBot) {
    if (typeof posted.base_version !== "number") {
      return c.json({ ok: false, error: "base_version required for pipeline writes" }, 400);
    }
    if (posted.revid !== undefined) {
      if (typeof posted.revid !== "number" || !Number.isInteger(posted.revid) || posted.revid <= 0) {
        return c.json({ ok: false, error: "bad revid" }, 400);
      }
      postedRevid = posted.revid;
    }
    if (posted.meta !== undefined) {
      if (typeof posted.meta !== "object" || posted.meta === null || Array.isArray(posted.meta)) {
        return c.json({ ok: false, error: "meta must be an object" }, 400);
      }
      metaJson = JSON.stringify(posted.meta);
      if (metaJson.length > MAX_META_BYTES) return c.json({ ok: false, error: "meta too large" }, 413);
    }
  }

  // Optimistic concurrency: if the client sent the version it edited against and
  // it no longer matches the current row, reject without writing so the client
  // can rebase. Absent base_version → write unconditionally (back-compat,
  // session saves only — bearer writes were required to send it above).
  if (typeof posted.base_version === "number" && posted.base_version !== row.version) {
    return c.json(
      { error: "stale", version: row.version, annotations: JSON.parse(row.annotations) },
      409,
    );
  }

  const stored = JSON.parse(row.annotations) as AnnRecord[];
  let finalAnnotations: AnnRecord[];
  if (isBot) {
    // Human-preservation assertion (server-side _preserve_human twin): a bot
    // write may never lose or alter a stored provenance='human' annotation —
    // tombstones (status='rejected') included; they are human vetoes.
    const missing = findLostHuman(stored, postedAnnotations);
    if (missing.length > 0) {
      return c.json({ ok: false, error: "human annotation lost", missing }, 422);
    }
    // Bot provenance passes through verbatim — attribution lives at revision
    // level (kind/meta), never laundered into the annotation provenance enum.
    finalAnnotations = postedAnnotations;
  } else {
    // Session saves: provenance is stamped server-side from the actor — new or
    // changed annotations become 'human'; unchanged ones keep their stored
    // provenance. Clients can't launder provenance in either direction.
    finalAnnotations = stampProvenance(stored, postedAnnotations);
  }
  const annJson = JSON.stringify(finalAnnotations);
  const annotations = finalAnnotations as unknown as Annotation[];

  // Re-render to report how many annotations actually anchored (same UX as
  // serve_review). A bot re-pin anchors against the NEW revision's HTML.
  const wp = await getWikipediaHtml(c.env.WP_HTML, slug, row.wikipediaTitle, postedRevid ?? row.revid);
  const { matched } = wrapAnnotations(absolutizeWikipediaUrls(wp.html), annotations);
  const matchedCount = matched.filter(Boolean).length;

  const now = Date.now();
  const newVersion = row.version + 1;
  const comment = typeof posted.comment === "string" ? posted.comment.slice(0, 500) : "";
  // Revid policy: articles.revid advances only atomically with the annotations
  // payload — a bot-posted revid re-pins in this same UPDATE, never separately.
  const newRevid = postedRevid ?? wp.revid ?? row.revid;
  // Guard the UPDATE on the version we read (CAS) to close the TOCTOU between
  // the read above and this write. If a concurrent save bumped the version,
  // 0 rows change → treat as stale (same 409 contract).
  const updated = await db
    .update(articles)
    .set({ annotations: annJson, version: newVersion, updatedAt: now, revid: newRevid })
    .where(and(eq(articles.slug, slug), eq(articles.version, row.version)));
  if (updated.meta.changes === 0) {
    const fresh = (await db.select().from(articles).where(eq(articles.slug, slug)).limit(1))[0];
    return c.json(
      {
        error: "stale",
        version: fresh ? fresh.version : row.version,
        annotations: fresh ? JSON.parse(fresh.annotations) : [],
      },
      409,
    );
  }
  const prior = (
    await db.select({ id: revisions.id }).from(revisions).where(eq(revisions.slug, slug)).orderBy(desc(revisions.id)).limit(1)
  )[0];
  await db.insert(revisions).values({
    slug,
    userId: user.id,
    annotations: annJson,
    comment: comment || null,
    kind: isBot ? "pipeline" : "edit",
    meta: metaJson,
    parentId: prior?.id ?? null,
    createdAt: now,
  });
  if (isBot) {
    // Bookkeep the review in moderation_state (feeds /api/work priority).
    // wp_drifted resets only when this write re-pinned revid to latest_revid
    // (or no upstream revid is known); otherwise an existing drift flag stands.
    const stillDrifted = row.latestRevid !== null && (newRevid === null || newRevid < row.latestRevid);
    await db
      .insert(moderationState)
      .values({
        slug,
        lastReviewedAt: now,
        lastReviewedVersion: newVersion,
        wpDrifted: stillDrifted,
        updatedAt: now,
      })
      .onConflictDoUpdate({
        target: moderationState.slug,
        set: {
          lastReviewedAt: now,
          lastReviewedVersion: newVersion,
          updatedAt: now,
          ...(stillDrifted ? {} : { wpDrifted: false }),
        },
      });
  }
  console.log(
    JSON.stringify({
      event: "save",
      slug,
      user_id: user.id,
      kind: isBot ? "pipeline" : "edit",
      version: newVersion,
      matched: matchedCount,
      total: annotations.length,
      bytes: annJson.length,
      t: now,
    }),
  );

  return c.json({ ok: true, matched: `${matchedCount}/${annotations.length}`, version: newVersion });
});

// ---- revert to a prior revision (patroller/admin only) ----
app.post("/api/article/:slug/revert/:revid", async (c) => {
  const originErr = checkOrigin(c);
  if (originErr) return originErr;
  const user = await requireRole(c, ["patroller", "admin"]);
  if (!user) return c.json({ ok: false, error: "forbidden" }, 403);
  const { success: allowed } = await c.env.EDIT_LIMITER.limit({ key: `edit:${user.id}` });
  if (!allowed) return c.json({ ok: false, error: "rate limited — slow down" }, 429);
  const slug = c.req.param("slug");
  const revid = parseInt(c.req.param("revid"), 10);
  if (Number.isNaN(revid)) return c.json({ ok: false, error: "bad revision id" }, 400);
  const db = drizzle(c.env.DB);
  const rev = (
    await db.select().from(revisions).where(and(eq(revisions.id, revid), eq(revisions.slug, slug))).limit(1)
  )[0];
  if (!rev) return c.json({ ok: false, error: "unknown revision" }, 404);
  const row = (await db.select().from(articles).where(eq(articles.slug, slug)).limit(1))[0];
  if (!row) return c.json({ ok: false, error: "unknown slug" }, 404);

  // The snapshot is already-validated data, but validate defensively (cheap) so
  // a malformed historical row can't be re-promoted to current.
  let revAnnotations: unknown;
  try {
    revAnnotations = JSON.parse(rev.annotations);
  } catch {
    return c.json({ ok: false, error: "corrupt revision" }, 400);
  }
  if (!Array.isArray(revAnnotations)) return c.json({ ok: false, error: "corrupt revision" }, 400);
  const revValidationErr = validateAnnotations(c, revAnnotations, rev.annotations);
  if (revValidationErr) return revValidationErr;

  const now = Date.now();
  const newVersion = row.version + 1;
  await db
    .update(articles)
    .set({ annotations: rev.annotations, version: newVersion, updatedAt: now })
    .where(eq(articles.slug, slug));
  const prior = (
    await db.select({ id: revisions.id }).from(revisions).where(eq(revisions.slug, slug)).orderBy(desc(revisions.id)).limit(1)
  )[0];
  await db.insert(revisions).values({
    slug,
    userId: user.id,
    annotations: rev.annotations,
    comment: `revert to #${revid}`,
    kind: "revert",
    parentId: prior?.id ?? null,
    createdAt: now,
  });
  console.log(
    JSON.stringify({
      event: "revert",
      slug,
      user_id: user.id,
      from_revid: revid,
      new_version: newVersion,
      t: now,
    }),
  );
  return c.json({ ok: true, version: newVersion });
});

// ---- work queue for the moderation pipeline (bot role only) ----
// One ORDER BY implements the binding priority policy: flagged > drifted >
// human-edited-since-review > oldest-reviewed (NULL last_reviewed_at = never
// reviewed sorts first within that tier). mode=wp-update narrows to articles
// whose pinned revid trails upstream.
app.get("/api/work", async (c) => {
  const user = await requireRole(c, ["bot"]);
  if (!user) return c.json({ ok: false, error: "forbidden" }, 403);
  const mode = c.req.query("mode") ?? "review";
  if (mode !== "review" && mode !== "wp-update") {
    return c.json({ ok: false, error: "unknown mode" }, 400);
  }
  const limitRaw = parseInt(c.req.query("limit") ?? "50", 10);
  const limit = Math.min(Math.max(Number.isNaN(limitRaw) ? 50 : limitRaw, 1), 100);

  const db = drizzle(c.env.DB);
  const humanEdited = sql<number>`CASE WHEN ${moderationState.lastReviewedVersion} IS NOT NULL AND ${articles.version} > ${moderationState.lastReviewedVersion} THEN 1 ELSE 0 END`;
  const where =
    mode === "wp-update"
      ? sql`(COALESCE(${moderationState.wpDrifted}, 0) = 1 OR (${articles.latestRevid} IS NOT NULL AND ${articles.latestRevid} > ${articles.revid}))`
      : undefined;
  const rows = await db
    .select({
      slug: articles.slug,
      version: articles.version,
      revid: articles.revid,
      latestRevid: articles.latestRevid,
      lastReviewedAt: moderationState.lastReviewedAt,
      lastReviewedVersion: moderationState.lastReviewedVersion,
      flagCount: moderationState.flagCount,
      wpDrifted: moderationState.wpDrifted,
    })
    .from(articles)
    .leftJoin(moderationState, eq(moderationState.slug, articles.slug))
    .where(where)
    .orderBy(
      sql`COALESCE(${moderationState.flagCount}, 0) DESC`,
      sql`COALESCE(${moderationState.wpDrifted}, 0) DESC`,
      sql`${humanEdited} DESC`,
      sql`${moderationState.lastReviewedAt} ASC NULLS FIRST`,
    )
    .limit(limit);

  // `reason` names the rule that selected the row, aligned with the ORDER BY
  // tiers (and, in wp-update mode, with the filter — where latest_revid >
  // revid counts as drift even without the wp_drifted flag).
  const jobs = rows.map((r) => {
    let reason: string;
    if ((r.flagCount ?? 0) > 0) reason = "flagged";
    else if (
      r.wpDrifted ||
      (mode === "wp-update" && r.latestRevid !== null && r.revid !== null && r.latestRevid > r.revid)
    )
      reason = "drifted";
    else if (r.lastReviewedVersion !== null && r.version > r.lastReviewedVersion) reason = "human-edited";
    else if (r.lastReviewedAt === null) reason = "never-reviewed";
    else reason = "stale-review";
    return {
      slug: r.slug,
      version: r.version,
      revid: r.revid,
      latest_revid: r.latestRevid,
      last_reviewed_at: r.lastReviewedAt,
      last_reviewed_version: r.lastReviewedVersion,
      reason,
    };
  });
  return c.json({ jobs });
});

// ---- article pages (clean + legacy .html) ----
async function serveArticle(c: Context<{ Bindings: Env }>, slug: string) {
  if (RESERVED.has(slug)) return c.notFound();
  const db = drizzle(c.env.DB);
  const row = (await db.select().from(articles).where(eq(articles.slug, slug)).limit(1))[0];
  if (!row) return c.notFound();
  const base = await renderArticleBase(db, c.env, row);
  const user = await getUser(c);
  const annotations = JSON.parse(row.annotations) as Annotation[];
  // latestRevid/revid feed the per-request staleness banner (rendered by
  // injectAuthAndEditor — never baked into the cached base page).
  const page = injectAuthAndEditor(base, {
    slug,
    user,
    annotations,
    version: row.version,
    latestRevid: row.latestRevid,
    revid: row.revid,
  });
  return c.html(page);
}

app.get("/:slug{.+\\.html}", (c) => serveArticle(c, c.req.param("slug").replace(/\.html$/, "")));
app.get("/:slug", (c) => serveArticle(c, c.req.param("slug")));

app.notFound(async (c) => {
  const res = await c.env.ASSETS.fetch(new Request(new URL("/404.html", c.req.url)));
  return new Response(res.body, { status: 404, headers: res.headers });
});

// Global error handler — catch-all so uncaught throws return a generic 500
// (no stack to clients) while the stack is logged for observability.
app.onError((err, c) => {
  console.log(
    JSON.stringify({
      event: "error",
      path: c.req.path,
      method: c.req.method,
      msg: String((err && err.stack) || err),
    }),
  );
  return c.json({ ok: false, error: "internal" }, 500);
});

// Module-format Worker export: HTTP via the Hono app, plus the Wikipedia
// drift-detection cron (src/drift.ts `scheduled`, wired to wrangler.jsonc's
// `triggers.crons`).
export { app }; // direct Hono handle for tests (app.request)
export default { fetch: app.fetch, scheduled };
