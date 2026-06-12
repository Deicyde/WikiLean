import { Hono, type Context } from "hono";
import { drizzle } from "drizzle-orm/d1";
import { eq, and, desc, sql, inArray, isNull, type SQL } from "drizzle-orm";
import type { BatchItem } from "drizzle-orm/batch";
import {
  articles,
  revisions,
  users,
  moderationState,
  annotationEvents,
  flags,
  type ArticleRow,
  type AnnotationEventInsert,
} from "./db/schema.js";
import { absolutizeWikipediaUrls, wrapAnnotations } from "./engine/wrap.js";
import { renderArticlePage } from "./engine/page.js";
import { getWikipediaHtml } from "./wikipedia.js";
import { getUser, requireRole, registerAuthRoutes } from "./auth.js";
import { scheduled } from "./drift.js";
import { injectAuthAndEditor, historyPage, recentChangesPage, flagsPage, diffPage } from "./pages.js";
import { homePage, sitemapXml } from "./home.js";
import type { Annotation } from "./engine/types.js";
import type { Env } from "./env.js";
import {
  ANNOTATION_STATUSES,
  FLAG_REASONS,
  MAX_ANNOTATIONS,
  MAX_ANNOTATIONS_BYTES,
  MAX_FIELD_LEN,
  MAX_FLAG_COMMENT_LEN,
  MAX_META_BYTES,
  MAX_TEXT_LEN,
  deepEqual,
  diffAnnotations,
  findLostHuman,
  findMatch,
  serializeFieldChanges,
  stampProvenance,
  type AnnotationChange,
  type AnnRecord,
} from "./validation.js";

const RESERVED = new Set([
  "assets",
  "api",
  "favicon.ico",
  "robots.txt",
  "sitemap.xml",
  "recent-changes",
  "flags",
  "login",
  "logout",
  "graph",
  "graph_data.json",
  "article-graph",
  "article-graph-data.json",
]);

const app = new Hono<{ Bindings: Env }>();
registerAuthRoutes(app);

// Renders (and KV-caches) the anonymous base page for an article. Takes the
// already-SELECTed row so the cached base and the caller's injected editor
// model come from one consistent read (no double-read race with concurrent
// saves).
async function renderArticleBase(env: Env, row: ArticleRow): Promise<string> {
  const slug = row.slug;

  // Bump the prefix when the engine's wrap behavior changes — old keys would
  // serve pre-change HTML. v6: wraps now carry data-provenance="ai|human" so
  // human-curated annotations can be visually distinguished. v7: post-XSS-
  // hardening + status-validation — evicts any XSS-poisoned/stale cached pages.
  // v8: tombstone skip — status='rejected' annotations are no longer wrapped
  // and drop out of header badge counts / anonymous client data. v9: cached
  // pages embed /assets/script.js?v=5 (the ⚑ flag micro-form) — evict so all
  // readers refetch the new script. v10: warm-palette redesign — page template
  // links style.css?v=6 + retuned .wl-attribution. v11: page-template changes
  // shipping in the same review wave (frontend agent) — evict in lockstep.
  const cacheKey = `render:v11:${slug}:${row.version}`;
  const cached = await env.RENDER_CACHE.get(cacheKey);
  if (cached) return cached;

  const wp = await getWikipediaHtml(env.WP_HTML, slug, row.wikipediaTitle, row.revid);
  // Revid policy: articles.revid advances only atomically with the annotations
  // payload — the old patch-back write here (a write in a read path) is gone;
  // all 709 rows carry a non-null revid, so this log line firing means a
  // create/seed path regressed.
  if (row.revid === null) {
    console.log(JSON.stringify({ event: "render-null-revid", slug, resolved_revid: wp.revid }));
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

// Annotation count for the history page. Excludes tombstones
// (status='rejected') so the per-revision counts agree with the page-header
// badges, which already skip them (UI#12).
function annCount(json: string): number {
  try {
    const a = JSON.parse(json);
    if (!Array.isArray(a)) return 0;
    return a.filter((x) => !(typeof x === "object" && x !== null && (x as AnnRecord).status === "rejected"))
      .length;
  } catch {
    return 0;
  }
}

const STATUS_SET = new Set<string>(ANNOTATION_STATUSES);

// ---- stable annotation ids (C1) -------------------------------------------
// ID1 contract: 12 lowercase hex chars = 6 crypto-random bytes (twins:
// scripts/backfill-ids.ts randomHexId, Python secrets.token_hex(6)). Ids are
// immutable once assigned; posted ids are validated below and the save path
// lazily heals any annotation still lacking one, so every save converges the
// article on full id coverage.
const ANNOTATION_ID_RE = /^[0-9a-f]{12}$/;

function freshAnnotationId(): string {
  const bytes = new Uint8Array(6);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

// Lazy id-heal, applied AFTER findLostHuman/stampProvenance (which match a
// posted-without-id annotation to its stored twin by anchor signature) and
// BEFORE persisting. For each final annotation lacking an id:
//   1. If findMatch pairs it with a stored annotation that carries a valid id
//      (and no other posted annotation already claimed that id), ADOPT the
//      stored id — minting a fresh one here would break identity continuity
//      for an annotation that merely round-tripped through a client that
//      dropped the field.
//   2. Only truly-new annotations (no stored twin, or a twin without an id)
//      get a fresh id, collision-checked against every id in the article
//      (posted + stored, so a concurrently-dropped id can't be reissued).
// Annotations that already carry an id pass through untouched — posted ids
// were format- and uniqueness-validated in validateAnnotations.
function healAnnotationIds(stored: AnnRecord[], finals: AnnRecord[]): AnnRecord[] {
  const taken = new Set<string>();
  for (const a of finals) {
    if (typeof a.id === "string") taken.add(a.id);
  }
  const storedIds = new Set<string>();
  for (const s of stored) {
    if (typeof s.id === "string") storedIds.add(s.id);
  }
  return finals.map((a) => {
    if (typeof a.id === "string" && ANNOTATION_ID_RE.test(a.id)) return a;
    const match = findMatch(a, stored);
    let id: string | undefined =
      match && typeof match.id === "string" && ANNOTATION_ID_RE.test(match.id) && !taken.has(match.id)
        ? match.id
        : undefined;
    if (id === undefined) {
      do {
        id = freshAnnotationId();
      } while (taken.has(id) || storedIds.has(id));
    }
    taken.add(id);
    return { ...a, id };
  });
}

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
  const seenIds = new Set<string>();
  for (const a of annotations) {
    if (typeof a !== "object" || a === null || Array.isArray(a)) {
      return c.json({ ok: false, error: "annotation must be an object" }, 400);
    }
    const ann = a as Record<string, unknown>;
    if (ann.status !== undefined && (typeof ann.status !== "string" || !STATUS_SET.has(ann.status))) {
      return c.json({ ok: false, error: "invalid status" }, 400);
    }
    // C1: ids are optional (absent → lazily healed on save) but when present
    // must be canonical 12-hex strings and unique within the posted array.
    if (ann.id !== undefined) {
      if (typeof ann.id !== "string" || !ANNOTATION_ID_RE.test(ann.id)) {
        return c.json({ ok: false, error: "invalid annotation id" }, 400);
      }
      if (seenIds.has(ann.id)) {
        return c.json({ ok: false, error: "duplicate annotation id" }, 400);
      }
      seenIds.add(ann.id);
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

type DrizzleDB = ReturnType<typeof drizzle>;

// Per-status annotation counts (D-C5), tombstones excluded. Written alongside
// the annotations blob in EVERY write path (save/create/revert/endorse) so the
// homepage can render coverage without parsing 709 JSON blobs per request.
function statusCounts(annotations: AnnRecord[]): {
  nFormalized: number;
  nPartial: number;
  nNotFormalized: number;
} {
  const counts = { nFormalized: 0, nPartial: 0, nNotFormalized: 0 };
  for (const a of annotations) {
    if (a.status === "formalized") counts.nFormalized += 1;
    else if (a.status === "partial") counts.nPartial += 1;
    else if (a.status === "not_formalized") counts.nNotFormalized += 1;
  }
  return counts;
}

// Pseudonymous abuse handle for anonymous flags: sha256 hex of the client IP.
async function sha256Hex(s: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

// F9: each write path bundles its post-CAS writes (revision insert + events
// + moderation_state bookkeeping) into ONE db.batch() so a mid-write crash
// can't leave a revision without its events (or vice versa). D1 executes a
// batch sequentially in one transaction; the test shim (d1shim) executes it
// sequentially on one connection. The events' revision_id can't be bound
// before the batch runs, so event rows reference the batch's own revision
// insert via a per-slug MAX(id) subquery — valid because the revision insert
// precedes the event inserts in the same sequential batch.
function newRevisionId(slug: string): SQL<number> {
  return sql<number>`(SELECT MAX(${revisions.id}) FROM ${revisions} WHERE ${revisions.slug} = ${slug})`;
}

// AnnotationEventInsert with the batch-time revision_id expression allowed.
type EventRowInsert = Omit<AnnotationEventInsert, "revisionId"> & {
  revisionId: number | SQL<number>;
};

// Map by-id diff results (validation.ts diffAnnotations) to annotation_events
// rows. `eventTypeOverride` collapses every change kind to 'revert_restore'
// for the revert path ("one event per annotation that CHANGED vs the
// pre-revert state"); field_changes ride along whenever the differ produced
// field-level detail.
function eventRowsFromChanges(
  changes: AnnotationChange[],
  opts: {
    revisionId: number | SQL<number>;
    slug: string;
    actorType: "human" | "pipeline";
    userId: string;
    now: number;
    eventTypeOverride?: "revert_restore";
  },
): EventRowInsert[] {
  return changes.map((ch) => ({
    revisionId: opts.revisionId,
    slug: opts.slug,
    annotationId: ch.annotationId,
    eventType: opts.eventTypeOverride ?? ch.changeType,
    actorType: opts.actorType,
    userId: opts.userId,
    fieldChanges: serializeFieldChanges(ch.fields),
    createdAt: opts.now,
  }));
}

// Chunked multi-row inserts: ~10 bind params per row, so 10 rows stays well
// under D1's 100-parameter statement cap. Returned as statements so the
// caller can fold them into its write batch (F9).
const EVENT_INSERT_CHUNK = 10;

function eventInsertStatements(db: DrizzleDB, rows: EventRowInsert[]): BatchItem<"sqlite">[] {
  const stmts: BatchItem<"sqlite">[] = [];
  for (let i = 0; i < rows.length; i += EVENT_INSERT_CHUNK) {
    stmts.push(db.insert(annotationEvents).values(rows.slice(i, i + EVENT_INSERT_CHUNK)));
  }
  return stmts;
}

type WriteBatch = [BatchItem<"sqlite">, ...BatchItem<"sqlite">[]];

// ---- dynamic homepage + sitemap (D-C7) ----
// Both are D1-driven (new articles appear without a redeploy) and KV-cached
// with TTL-only invalidation. They route here only because build-public.ts no
// longer copies index.html/sitemap.xml into wiki/public/ — the asset layer
// runs BEFORE the Worker and would otherwise shadow these paths.
app.get("/", async (c) => {
  const cacheKey = "page:home:v2";
  const cached = await c.env.RENDER_CACHE.get(cacheKey);
  if (cached) return c.html(cached);
  const db = drizzle(c.env.DB);
  const rows = await db
    .select({
      slug: articles.slug,
      displayTitle: articles.displayTitle,
      nFormalized: articles.nFormalized,
      nPartial: articles.nPartial,
      nNotFormalized: articles.nNotFormalized,
      updatedAt: articles.updatedAt,
    })
    .from(articles)
    .orderBy(articles.displayTitle);
  const html = homePage(rows);
  await c.env.RENDER_CACHE.put(cacheKey, html, { expirationTtl: 300 });
  return c.html(html);
});

app.get("/sitemap.xml", async (c) => {
  const cacheKey = "page:sitemap:v1";
  const headers = { "Content-Type": "application/xml; charset=utf-8" };
  const cached = await c.env.RENDER_CACHE.get(cacheKey);
  if (cached) return c.body(cached, 200, headers);
  const db = drizzle(c.env.DB);
  const rows = await db
    .select({ slug: articles.slug, updatedAt: articles.updatedAt })
    .from(articles)
    .orderBy(articles.slug);
  const xml = sitemapXml(rows);
  await c.env.RENDER_CACHE.put(cacheKey, xml, { expirationTtl: 3600 });
  return c.body(xml, 200, headers);
});

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

// ---- /flags — open-flag patrol queue (D-C6) ----
// Any logged-in user may view (the queue is a contribution surface, not a
// secret), but resolve buttons render only for patroller/admin; the resolve
// endpoint enforces the same gate server-side.
// NB: the bot bearer also gets 200 here — intentional: a harmless read
// (getUser treats the bearer as logged-in), and resolving stays role-gated.
app.get("/flags", async (c) => {
  const user = await getUser(c);
  if (!user) return c.redirect("/login?returnTo=%2Fflags");
  const db = drizzle(c.env.DB);
  const rows = await db
    .select({
      id: flags.id,
      slug: flags.slug,
      displayTitle: articles.displayTitle,
      annotationId: flags.annotationId,
      reason: flags.reason,
      comment: flags.comment,
      status: flags.status,
      createdAt: flags.createdAt,
    })
    .from(flags)
    .leftJoin(articles, eq(articles.slug, flags.slug))
    .where(eq(flags.status, "open"))
    .orderBy(desc(flags.createdAt), desc(flags.id))
    .limit(200);
  const canResolve = user.role === "patroller" || user.role === "admin";
  return c.html(
    flagsPage(
      rows.map((r) => ({ ...r, displayTitle: r.displayTitle ?? r.slug })),
      canResolve,
    ),
  );
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
    // UI#3: revert is patroller/admin-only server-side (requireRole on the
    // endpoint) — don't render revert buttons that always 403 for plain users.
    user !== null && (user.role === "patroller" || user.role === "admin"),
  );
  return c.html(html);
});

// ---- field-level diff between two revisions (D-C6; public) ----
// A pure read over the public revision history: the backend computes the
// by-id diff (the same differ that feeds annotation_events) and pages.ts
// renders it. 'reject' folds into 'modify' for display — the status field
// change carries the signal.
app.get("/:slug/diff/:fromId/:toId", async (c) => {
  const slug = c.req.param("slug");
  const fromId = parseInt(c.req.param("fromId"), 10);
  const toId = parseInt(c.req.param("toId"), 10);
  if (Number.isNaN(fromId) || Number.isNaN(toId)) return c.notFound();
  const db = drizzle(c.env.DB);
  const art = (await db.select().from(articles).where(eq(articles.slug, slug)).limit(1))[0];
  if (!art) return c.notFound();
  // Both revisions must exist AND belong to this slug (no cross-article reads).
  const revs = await db
    .select({ id: revisions.id, annotations: revisions.annotations })
    .from(revisions)
    .where(and(eq(revisions.slug, slug), inArray(revisions.id, [fromId, toId])));
  const from = revs.find((r) => r.id === fromId);
  const to = revs.find((r) => r.id === toId);
  if (!from || !to) return c.notFound();
  let fromArr: unknown;
  let toArr: unknown;
  try {
    fromArr = JSON.parse(from.annotations);
    toArr = JSON.parse(to.annotations);
  } catch {
    return c.text("corrupt revision", 400);
  }
  if (!Array.isArray(fromArr) || !Array.isArray(toArr)) return c.text("corrupt revision", 400);
  const changes = diffAnnotations(fromArr as AnnRecord[], toArr as AnnRecord[]).map((ch) => ({
    annotationId: ch.annotationId,
    changeType: (ch.changeType === "reject" ? "modify" : ch.changeType) as "add" | "modify" | "delete",
    label: ch.label,
    fields: ch.fields,
  }));
  return c.html(diffPage(slug, art.displayTitle, fromId, toId, changes));
});

// ---- machine-readable article JSON (the pipeline read path; public) ----
// F15: tombstones (status='rejected') are replaced with null placeholders for
// everyone EXCEPT the bearer-authenticated pipeline — mirroring the rendered
// page (engine/page.ts buildClientData), so vetoed content can't be read back
// out through the API. The bearer bot gets the full array because the runner
// must echo tombstones verbatim or its next save 422s. The {.+\.json} suffix
// pattern wins over the catch-all /:slug routes by registration order, so a
// slug literally named "x.json" can't shadow this route (it'd be served at
// /api/article/x.json.json).
app.get("/api/article/:slug{.+\\.json}", async (c) => {
  const slug = c.req.param("slug").replace(/\.json$/, "");
  const db = drizzle(c.env.DB);
  const row = (await db.select().from(articles).where(eq(articles.slug, slug)).limit(1))[0];
  if (!row) return c.json({ ok: false, error: "unknown slug" }, 404);
  const user = await getUser(c);
  const annotations = JSON.parse(row.annotations) as unknown[];
  const full = user?.role === "bot";
  return c.json({
    slug: row.slug,
    wikipedia_title: row.wikipediaTitle,
    display_title: row.displayTitle,
    version: row.version,
    revid: row.revid,
    latest_revid: row.latestRevid,
    schema_version: row.schemaVersion,
    annotations: full
      ? annotations
      : annotations.map((a) =>
          typeof a === "object" && a !== null && (a as AnnRecord).status === "rejected" ? null : a,
        ),
  });
});

// ---- create an article (pipeline bearer only; D-C1) ----
// The moderate.py `new` mode's missing write path: bot-discovered articles
// enter D1 here (POST 404s on unknown slugs by design — creation is explicit).
app.put("/api/article/:slug", async (c) => {
  const originErr = checkOrigin(c);
  if (originErr) return originErr;
  const user = await requireRole(c, ["bot"]);
  if (!user) return c.json({ ok: false, error: "forbidden" }, 403);
  const { success: allowed } = await c.env.EDIT_LIMITER.limit({ key: `edit:${user.id}` });
  if (!allowed) return c.json({ ok: false, error: "rate limited — slow down" }, 429);
  const slug = c.req.param("slug");
  if (RESERVED.has(slug)) return c.json({ ok: false, error: "reserved slug" }, 400);

  let posted: {
    wikipedia_title?: unknown;
    display_title?: unknown;
    wikidata_qid?: unknown;
    revid?: unknown;
    annotations?: unknown;
    comment?: unknown;
    meta?: unknown;
  };
  try {
    posted = await c.req.json();
  } catch {
    return c.json({ ok: false, error: "bad json" }, 400);
  }
  if (
    typeof posted.wikipedia_title !== "string" ||
    posted.wikipedia_title.length === 0 ||
    posted.wikipedia_title.length > MAX_FIELD_LEN
  ) {
    return c.json({ ok: false, error: "bad wikipedia_title" }, 400);
  }
  if (
    posted.display_title !== undefined &&
    (typeof posted.display_title !== "string" ||
      posted.display_title.length === 0 ||
      posted.display_title.length > MAX_FIELD_LEN)
  ) {
    return c.json({ ok: false, error: "bad display_title" }, 400);
  }
  if (
    posted.wikidata_qid !== undefined &&
    (typeof posted.wikidata_qid !== "string" || posted.wikidata_qid.length > MAX_FIELD_LEN)
  ) {
    return c.json({ ok: false, error: "bad wikidata_qid" }, 400);
  }
  // F16: pipeline creates must pin the Wikipedia revision they annotated
  // against. All 709 production rows carry a non-null revid — this codifies
  // that reality (a null pin would also break drift detection for the row).
  if (typeof posted.revid !== "number" || !Number.isInteger(posted.revid) || posted.revid <= 0) {
    return c.json({ ok: false, error: "revid required for pipeline creates" }, 400);
  }
  const revid: number = posted.revid;
  let metaJson: string | null = null;
  if (posted.meta !== undefined) {
    if (typeof posted.meta !== "object" || posted.meta === null || Array.isArray(posted.meta)) {
      return c.json({ ok: false, error: "meta must be an object" }, 400);
    }
    metaJson = JSON.stringify(posted.meta);
    if (metaJson.length > MAX_META_BYTES) return c.json({ ok: false, error: "meta too large" }, 413);
  }
  if (!Array.isArray(posted.annotations)) return c.json({ ok: false, error: "missing annotations" }, 400);
  const postedJson = JSON.stringify(posted.annotations);
  const validationErr = validateAnnotations(c, posted.annotations, postedJson);
  if (validationErr) return validationErr;

  const db = drizzle(c.env.DB);
  const existing = (
    await db.select({ slug: articles.slug }).from(articles).where(eq(articles.slug, slug)).limit(1)
  )[0];
  if (existing) return c.json({ error: "exists" }, 409);

  // Same id discipline as saves: posted ids were validated above; annotations
  // without one get a fresh id (no stored pool to adopt from on create).
  const finalAnnotations = healAnnotationIds([], posted.annotations as AnnRecord[]);
  const annJson = JSON.stringify(finalAnnotations);
  const now = Date.now();
  const comment = typeof posted.comment === "string" ? posted.comment.slice(0, 500) : null;

  // F9: the whole create (article row + revision + moderation_state + 'add'
  // events, D-C3) lands in one atomic batch — a PK conflict on a racing
  // duplicate create aborts everything.
  const createBatch: WriteBatch = [
    db.insert(articles).values({
      slug,
      wikipediaTitle: posted.wikipedia_title,
      displayTitle: (posted.display_title as string | undefined) ?? posted.wikipedia_title,
      wikidataQid: (posted.wikidata_qid as string | undefined) ?? null,
      revid,
      annotations: annJson,
      schemaVersion: 3,
      version: 1,
      ...statusCounts(finalAnnotations),
      createdAt: now,
      updatedAt: now,
    }),
    db.insert(revisions).values({
      slug,
      userId: user.id,
      annotations: annJson,
      comment: comment ?? "create",
      kind: "pipeline",
      meta: metaJson,
      parentId: null,
      createdAt: now,
    }),
    db
      .insert(moderationState)
      .values({ slug, lastReviewedAt: now, lastReviewedVersion: 1, updatedAt: now })
      .onConflictDoUpdate({
        target: moderationState.slug,
        set: { lastReviewedAt: now, lastReviewedVersion: 1, updatedAt: now },
      }),
    ...eventInsertStatements(
      db,
      eventRowsFromChanges(diffAnnotations([], finalAnnotations), {
        revisionId: newRevisionId(slug),
        slug,
        actorType: "pipeline",
        userId: user.id,
        now,
      }),
    ),
  ];
  await db.batch(createBatch);
  console.log(
    JSON.stringify({
      event: "create",
      slug,
      user_id: user.id,
      annotations: finalAnnotations.length,
      revid,
      t: now,
    }),
  );
  return c.json({ ok: true, slug, version: 1 }, 201);
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
    action?: unknown;
    annotation_id?: unknown;
  };
  try {
    posted = await c.req.json();
  } catch {
    return c.json({ ok: false, error: "bad json" }, 400);
  }

  // ---- endorse (D-C2): {action:'endorse', annotation_id, base_version} ----
  // The human-agreement signal: flips one stored annotation's provenance to
  // 'human' server-side, content untouched. Session users only — a bot
  // endorsing itself would be meaningless (403). This replaces the editor's
  // old provenance-flip-and-save, which stampProvenance reverts by design.
  if (posted.action !== undefined) {
    if (posted.action !== "endorse") return c.json({ ok: false, error: "unknown action" }, 400);
    if (isBot) return c.json({ ok: false, error: "forbidden" }, 403);
    const annotationId = posted.annotation_id;
    if (typeof annotationId !== "string" || !ANNOTATION_ID_RE.test(annotationId)) {
      return c.json({ ok: false, error: "bad annotation_id" }, 400);
    }
    if (typeof posted.base_version !== "number") {
      return c.json({ ok: false, error: "base_version required" }, 400);
    }
    if (posted.base_version !== row.version) {
      return c.json(
        { error: "stale", version: row.version, annotations: JSON.parse(row.annotations) },
        409,
      );
    }
    const stored = JSON.parse(row.annotations) as AnnRecord[];
    const idx = stored.findIndex((a) => a.id === annotationId);
    if (idx === -1) return c.json({ error: "annotation not found" }, 404);
    const priorProvenance = stored[idx].provenance ?? null;
    const finals = stored.slice();
    finals[idx] = { ...stored[idx], provenance: "human" };
    const annJson = JSON.stringify(finals);
    const now = Date.now();
    const newVersion = row.version + 1;
    const updated = await db
      .update(articles)
      .set({ annotations: annJson, version: newVersion, updatedAt: now, ...statusCounts(finals) })
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
    // F9: revision + endorse event land atomically.
    const endorseBatch: WriteBatch = [
      db.insert(revisions).values({
        slug,
        userId: user.id,
        annotations: annJson,
        comment: `endorse:${annotationId}`,
        kind: "edit",
        parentId: prior?.id ?? null,
        createdAt: now,
      }),
      ...eventInsertStatements(db, [
        {
          revisionId: newRevisionId(slug),
          slug,
          annotationId,
          eventType: "endorse",
          actorType: "human",
          userId: user.id,
          fieldChanges: serializeFieldChanges([
            { field: "provenance", from: priorProvenance, to: "human" },
          ]),
          createdAt: now,
        },
      ]),
    ];
    await db.batch(endorseBatch);
    console.log(
      JSON.stringify({
        event: "endorse",
        slug,
        user_id: user.id,
        annotation_id: annotationId,
        version: newVersion,
        t: now,
      }),
    );
    return c.json({ ok: true, version: newVersion });
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
  // C1 lazy-heal (both branches): adopt the stored twin's id for annotations
  // posted without one, mint fresh ids for truly-new annotations. Runs after
  // the helpers above (they match posted-without-id to stored-by-signature)
  // and before persisting, so every save converges on full id coverage.
  finalAnnotations = healAnnotationIds(stored, finalAnnotations);

  // TESTAGENT#2: short-circuit no-op saves. If post-heal annotations are
  // deep-equal to what's stored and the save doesn't move the revid pin,
  // write NOTHING — no article UPDATE, no revision, no events (version-
  // bumping no-ops were churning the render cache and the revision log).
  // EXCEPTION: a bot no-op still stamps moderation_state (last_reviewed_at/
  // version), otherwise a reviewed-but-unchanged article never leaves the
  // review queue (and the F2 recency-aware flagged tier never releases it).
  // Session no-op saves skip the stamp.
  if (deepEqual(stored, finalAnnotations) && (postedRevid === null || postedRevid === row.revid)) {
    if (isBot) {
      const now = Date.now();
      await db
        .insert(moderationState)
        .values({ slug, lastReviewedAt: now, lastReviewedVersion: row.version, updatedAt: now })
        .onConflictDoUpdate({
          target: moderationState.slug,
          set: { lastReviewedAt: now, lastReviewedVersion: row.version, updatedAt: now },
        });
    }
    console.log(
      JSON.stringify({
        event: "save-noop",
        slug,
        user_id: user.id,
        kind: isBot ? "pipeline" : "edit",
        version: row.version,
      }),
    );
    return c.json({ ok: true, noop: true, version: row.version });
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
    .set({
      annotations: annJson,
      version: newVersion,
      updatedAt: now,
      revid: newRevid,
      ...statusCounts(finalAnnotations),
    })
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
  // D-C8b: a bot re-pin (old revid ≠ new) supersedes the old revision's cached
  // Wikipedia HTML — drop the stale WP_HTML key (fire-and-forget; the 90d TTL
  // is the backstop if the delete is lost).
  if (postedRevid !== null && row.revid !== null && postedRevid !== row.revid) {
    void c.env.WP_HTML.delete(`wp:${slug}:${row.revid}`).catch(() => {});
  }
  const prior = (
    await db.select({ id: revisions.id }).from(revisions).where(eq(revisions.slug, slug)).orderBy(desc(revisions.id)).limit(1)
  )[0];
  // F9: revision + events + moderation bookkeeping land in one atomic batch.
  // The article UPDATE above stays separate — its CAS result gates all this.
  const saveBatch: WriteBatch = [
    db.insert(revisions).values({
      slug,
      userId: user.id,
      annotations: annJson,
      comment: comment || null,
      kind: isBot ? "pipeline" : "edit",
      meta: metaJson,
      parentId: prior?.id ?? null,
      createdAt: now,
    }),
    // D-C3: per-annotation change log, diffed by id against the pre-save state.
    // actor_type comes from the auth seam, never from client-claimed provenance.
    ...eventInsertStatements(
      db,
      eventRowsFromChanges(diffAnnotations(stored, finalAnnotations), {
        revisionId: newRevisionId(slug),
        slug,
        actorType: isBot ? "pipeline" : "human",
        userId: user.id,
        now,
      }),
    ),
  ];
  if (isBot) {
    // Bookkeep the review in moderation_state (feeds /api/work priority).
    // wp_drifted resets only when this write re-pinned revid to latest_revid
    // (or no upstream revid is known); otherwise an existing drift flag stands.
    const stillDrifted = row.latestRevid !== null && (newRevid === null || newRevid < row.latestRevid);
    saveBatch.push(
      db
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
        }),
    );
  }
  await db.batch(saveBatch);
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
  // F5: same CAS contract as the save path — guard the UPDATE on the version
  // we read; a concurrent write between the read and this UPDATE leaves
  // 0 rows changed → 409 stale with the current state.
  const updated = await db
    .update(articles)
    .set({
      annotations: rev.annotations,
      version: newVersion,
      updatedAt: now,
      ...statusCounts(revAnnotations as AnnRecord[]),
    })
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
  // F9: revision + revert_restore events land atomically.
  const revertBatch: WriteBatch = [
    db.insert(revisions).values({
      slug,
      userId: user.id,
      annotations: rev.annotations,
      comment: `revert to #${revid}`,
      kind: "revert",
      parentId: prior?.id ?? null,
      createdAt: now,
    }),
    // D-C3: one 'revert_restore' per annotation that CHANGED vs the pre-revert
    // state (whatever the underlying change kind), field detail riding along.
    // Snapshot entries without ids (pre-backfill history) are skipped by the
    // differ — they can't key an event row.
    ...eventInsertStatements(
      db,
      eventRowsFromChanges(diffAnnotations(JSON.parse(row.annotations) as AnnRecord[], revAnnotations as AnnRecord[]), {
        revisionId: newRevisionId(slug),
        slug,
        actorType: "human",
        userId: user.id,
        now,
        eventTypeOverride: "revert_restore",
      }),
    ),
  ];
  await db.batch(revertBatch);
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

// ---- anonymous flag pipeline (D-C4) ----
const FLAG_REASON_SET = new Set<string>(FLAG_REASONS);

// Report a problem — NO auth required (the whole point: a reader on mobile can
// flag a wrong decl in two taps). Abuse posture: same-origin check, a
// dedicated per-IP rate limit, and a silent per-target open-flag cap.
app.post("/api/flag/:slug", async (c) => {
  const originErr = checkOrigin(c);
  if (originErr) return originErr;
  const ip = c.req.header("CF-Connecting-IP") ?? "";
  const { success: allowed } = await c.env.FLAG_LIMITER.limit({ key: `flag:${ip}` });
  if (!allowed) return c.json({ ok: false, error: "rate limited — slow down" }, 429);
  const slug = c.req.param("slug");

  let posted: { annotation_id?: unknown; reason?: unknown; comment?: unknown };
  try {
    posted = await c.req.json();
  } catch {
    return c.json({ ok: false, error: "bad json" }, 400);
  }
  if (typeof posted.reason !== "string" || !FLAG_REASON_SET.has(posted.reason)) {
    return c.json({ ok: false, error: "bad reason" }, 400);
  }
  let annotationId: string | null = null;
  if (posted.annotation_id !== undefined && posted.annotation_id !== null) {
    if (typeof posted.annotation_id !== "string" || !ANNOTATION_ID_RE.test(posted.annotation_id)) {
      return c.json({ ok: false, error: "bad annotation_id" }, 400);
    }
    annotationId = posted.annotation_id;
  }
  let comment: string | null = null;
  if (posted.comment !== undefined && posted.comment !== null) {
    if (typeof posted.comment !== "string" || posted.comment.length > MAX_FLAG_COMMENT_LEN) {
      return c.json({ ok: false, error: "bad comment" }, 400);
    }
    comment = posted.comment;
  }

  const db = drizzle(c.env.DB);
  const art = (
    await db.select({ slug: articles.slug }).from(articles).where(eq(articles.slug, slug)).limit(1)
  )[0];
  if (!art) return c.json({ ok: false, error: "unknown slug" }, 404);

  // Anti-spam cap: ≥5 open flags on the same target (slug + annotation-or-
  // article) → pretend success without inserting. Silent by design — a
  // flooder learns nothing, a genuine fifth reporter loses nothing (the
  // target is already maximally prioritized in /api/work).
  const openSame = await db
    .select({ id: flags.id })
    .from(flags)
    .where(
      and(
        eq(flags.slug, slug),
        eq(flags.status, "open"),
        annotationId === null ? isNull(flags.annotationId) : eq(flags.annotationId, annotationId),
      ),
    )
    .limit(5);
  if (openSame.length >= 5) return c.json({ ok: true });

  const user = await getUser(c);
  const now = Date.now();
  await db.insert(flags).values({
    slug,
    annotationId,
    reason: posted.reason,
    comment,
    userId: user?.id ?? null,
    ipHash: ip ? await sha256Hex(ip) : null,
    createdAt: now,
  });
  // flag_count feeds the /api/work priority ladder (flagged sorts first).
  await db
    .insert(moderationState)
    .values({ slug, flagCount: 1, updatedAt: now })
    .onConflictDoUpdate({
      target: moderationState.slug,
      set: { flagCount: sql`${moderationState.flagCount} + 1`, updatedAt: now },
    });
  console.log(
    JSON.stringify({
      event: "flag",
      slug,
      annotation_id: annotationId,
      reason: posted.reason,
      user_id: user?.id ?? null,
      t: now,
    }),
  );
  return c.json({ ok: true });
});

// Resolve a flag (patroller/admin). Only an OPEN flag resolves — a repeat
// resolve 404s instead of double-decrementing flag_count.
app.post("/api/flag/:id/resolve", async (c) => {
  const originErr = checkOrigin(c);
  if (originErr) return originErr;
  const user = await requireRole(c, ["patroller", "admin"]);
  if (!user) return c.json({ ok: false, error: "forbidden" }, 403);
  const id = parseInt(c.req.param("id"), 10);
  if (Number.isNaN(id)) return c.json({ ok: false, error: "bad flag id" }, 400);
  let posted: { resolution?: unknown };
  try {
    posted = await c.req.json();
  } catch {
    return c.json({ ok: false, error: "bad json" }, 400);
  }
  if (posted.resolution !== "fixed" && posted.resolution !== "dismissed") {
    return c.json({ ok: false, error: "bad resolution" }, 400);
  }
  const db = drizzle(c.env.DB);
  const flag = (await db.select().from(flags).where(eq(flags.id, id)).limit(1))[0];
  if (!flag || flag.status !== "open") return c.json({ ok: false, error: "unknown flag" }, 404);
  const now = Date.now();
  // Guard on status='open' (CAS) so two concurrent resolves can't both
  // decrement flag_count.
  const updated = await db
    .update(flags)
    .set({ status: posted.resolution, resolvedBy: user.id, resolvedAt: now })
    .where(and(eq(flags.id, id), eq(flags.status, "open")));
  if (updated.meta.changes === 0) return c.json({ ok: false, error: "unknown flag" }, 404);
  await db
    .update(moderationState)
    .set({ flagCount: sql`MAX(${moderationState.flagCount} - 1, 0)`, updatedAt: now })
    .where(eq(moderationState.slug, flag.slug));
  console.log(
    JSON.stringify({
      event: "flag-resolve",
      flag_id: id,
      slug: flag.slug,
      resolution: posted.resolution,
      user_id: user.id,
      t: now,
    }),
  );
  return c.json({ ok: true });
});

// Machine-readable flag list (patroller/admin) — the /flags page's data,
// for scripts and the moderation pipeline.
app.get("/api/flags", async (c) => {
  const user = await requireRole(c, ["patroller", "admin"]);
  if (!user) return c.json({ ok: false, error: "forbidden" }, 403);
  const status = c.req.query("status") ?? "open";
  const db = drizzle(c.env.DB);
  const rows = await db
    .select({
      id: flags.id,
      slug: flags.slug,
      displayTitle: articles.displayTitle,
      annotationId: flags.annotationId,
      reason: flags.reason,
      comment: flags.comment,
      status: flags.status,
      createdAt: flags.createdAt,
    })
    .from(flags)
    .leftJoin(articles, eq(articles.slug, flags.slug))
    .where(eq(flags.status, status))
    .orderBy(desc(flags.createdAt), desc(flags.id))
    .limit(200);
  return c.json({
    flags: rows.map((r) => ({
      id: r.id,
      slug: r.slug,
      display_title: r.displayTitle ?? r.slug,
      annotation_id: r.annotationId,
      reason: r.reason,
      comment: r.comment,
      status: r.status,
      created_at: r.createdAt,
    })),
  });
});

// ---- work queue for the moderation pipeline (bot role only) ----
// One ORDER BY implements the binding priority policy: flagged > drifted >
// human-edited-since-review > oldest-reviewed (NULL last_reviewed_at = never
// reviewed sorts first within that tier). mode=wp-update narrows to articles
// whose pinned revid trails upstream.
//
// F2: the flagged tier is RECENCY-AWARE — an article counts as flagged only
// while it has an open flag created AFTER its last review, so a bot review
// releases it from the tier even though the flag row stays open for human
// patrol (no flagged-livelock). moderation_state.flag_count keeps counting
// ALL open flags as the patrol-pressure metric; only the queue tier uses the
// recency predicate.
//
// F4/F11: articles parked by the update flow (state 'moved'/'deleted'/
// 'needs_human') are excluded from BOTH modes — they need a human (or the
// drift cron observing the page back to normal) before re-entering, and
// re-probing 'needs_human' rows would wedge stage-0 on the same articles.
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
  // F2: open flags newer than the last review (never reviewed → every open
  // flag counts). COUNT (not EXISTS) so heavier-flagged articles still sort
  // first within the tier.
  const recentOpenFlags = sql<number>`(SELECT COUNT(*) FROM ${flags} WHERE ${flags.slug} = ${articles.slug} AND ${flags.status} = 'open' AND ${flags.createdAt} > COALESCE(${moderationState.lastReviewedAt}, 0))`;
  // F4/F11: LEFT JOIN → no moderation row reads as state NULL (not parked).
  const notParked = sql`(${moderationState.state} IS NULL OR ${moderationState.state} NOT IN ('moved','deleted','needs_human'))`;
  const where =
    mode === "wp-update"
      ? sql`${notParked} AND (COALESCE(${moderationState.wpDrifted}, 0) = 1 OR (${articles.latestRevid} IS NOT NULL AND ${articles.latestRevid} > ${articles.revid}))`
      : notParked;
  const rows = await db
    .select({
      slug: articles.slug,
      version: articles.version,
      revid: articles.revid,
      latestRevid: articles.latestRevid,
      lastReviewedAt: moderationState.lastReviewedAt,
      lastReviewedVersion: moderationState.lastReviewedVersion,
      recentOpenFlags: recentOpenFlags.as("recent_open_flags"),
      wpDrifted: moderationState.wpDrifted,
    })
    .from(articles)
    .leftJoin(moderationState, eq(moderationState.slug, articles.slug))
    .where(where)
    .orderBy(
      sql`${recentOpenFlags} DESC`,
      sql`COALESCE(${moderationState.wpDrifted}, 0) DESC`,
      sql`${humanEdited} DESC`,
      // NULLS FIRST is deliberate (F13) and diverges from the roadmap's
      // original 'oldest-reviewed > new' wording: every article already has
      // one pipeline annotation pass, so first-moderation coverage of
      // never-reviewed articles beats re-reviewing old ones. (Roadmap text
      // update is on the lead.)
      sql`${moderationState.lastReviewedAt} ASC NULLS FIRST`,
    )
    .limit(limit);

  // `reason` names the rule that selected the row, aligned with the ORDER BY
  // tiers (and, in wp-update mode, with the filter — where latest_revid >
  // revid counts as drift even without the wp_drifted flag).
  const jobs = rows.map((r) => {
    let reason: string;
    if ((r.recentOpenFlags ?? 0) > 0) reason = "flagged";
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
  const base = await renderArticleBase(c.env, row);
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
