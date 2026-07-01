import { Hono, type Context } from "hono";
import { drizzle } from "drizzle-orm/d1";
import { eq, and, desc, gt, sql, inArray, isNull, type SQL } from "drizzle-orm";
import { alias } from "drizzle-orm/sqlite-core";
import type { BatchItem } from "drizzle-orm/batch";
import {
  articles,
  revisions,
  users,
  moderationState,
  annotationEvents,
  flags,
  pipelineRuns,
  watchlist,
  type ArticleRow,
  type AnnotationEventInsert,
} from "./db/schema.js";
import { absolutizeWikipediaUrls, wrapAnnotations } from "./engine/wrap.js";
import { renderArticlePage } from "./engine/page.js";
import { getWikipediaHtml } from "./wikipedia.js";
import { getUser, requireRole, registerAuthRoutes } from "./auth.js";
import { scheduled } from "./drift.js";
import {
  injectAuthAndEditor,
  historyPage,
  recentChangesPage,
  flagsPage,
  diffPage,
  statsPage,
  userProfilePage,
  RECENT_KINDS,
  type StatsEventCell,
  type UserProfileRow,
} from "./pages.js";
import { homePage, sitemapXml } from "./home.js";
import { wikifunctionsPage } from "./wikifunctions.js";
import { wikifunctionsVerifyPage } from "./wikifunctions-verify.js";
import { registerReviewRoutes } from "./review.js";
import { registerQueueRoutes } from "./queue.js";
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
import {
  applyProposalFields,
  fieldsSig,
  isProposalId,
  mergeProposals,
  parsePending,
  parseRejected,
} from "./proposals.js";

const RESERVED = new Set([
  "assets",
  "api",
  "favicon.ico",
  "robots.txt",
  "sitemap.xml",
  "recent-changes",
  "flags",
  "stats",
  "wikifunctions",
  "wikifunctions/verify",
  "login",
  "logout",
  "graph",
  "graph_data.json",
  "article-graph",
  "article-graph-data.json",
  "review",
  "queue",
  "u",
]);

const app = new Hono<{ Bindings: Env }>();
registerAuthRoutes(app);
registerReviewRoutes(app);
registerQueueRoutes(app);

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
  // v12 (now v13): snippet-respect — engine no longer promotes prose-snippet
  // wraps to a whole-block <div> at all. Math nested inside the sentence used
  // to force a promotion that swept in sibling sentences in the same <p>; the
  // .anno-X .mwe-math-element-block CSS rule paints display math directly now,
  // so a span sentence-wrap is fine even when it contains a display equation.
  // Editing the snippet actually tightens the highlight box, and the next
  // sentence in the same <p> stays unannotated.
  // v14: dark mode — page template carries the no-FOUC theme script + 🌓
  // toggle button; cached HTML must reflect those + style.css?v=8.
  const cacheKey = `render:v14:${slug}:${row.version}`;
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
  const cacheKey = "page:home:v3";  // v3: dark-mode theme script + toggle
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

// ---- /graph_data.json — concept-graph data, KV-first so the nightly can
// refresh it (verified @[wikidata] tags + live Mathlib coverage) via
// `wrangler kv key put`, with NO Worker deploy. run_worker_first (wrangler.jsonc)
// routes this path to the Worker; ./public/graph_data.json is the fallback on a
// KV miss (never-seeded or evicted). ------------------------------------------
app.get("/graph_data.json", async (c) => {
  const headers = {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "public, max-age=600",
  };
  const kv = await c.env.RENDER_CACHE.get("graph:data:v1");
  if (kv) return c.body(kv, 200, headers);
  // ASSETS.fetch bypasses run_worker_first, so this serves the static file
  // (no recursion) — the last-deployed graph_data.json as a safety net.
  return c.env.ASSETS.fetch(new Request(new URL("/graph_data.json", c.req.url)));
});

// ---- /wikifunctions — Wikifunctions-formalization tracker (static; public) --
// A self-contained status page rendered from the embedded verified corpus
// (wikifunctions-data.ts) — no D1, no migrations. Pure-function output, so it
// is KV-cached with a long TTL; bump the cache key suffix when the page or the
// embedded data changes.
app.get("/wikifunctions", async (c) => {
  const cacheKey = "page:wikifunctions:v2";  // v2: dark-mode theme script + toggle
  const cached = await c.env.RENDER_CACHE.get(cacheKey);
  if (cached) return c.html(cached);
  const html = wikifunctionsPage();
  await c.env.RENDER_CACHE.put(cacheKey, html, { expirationTtl: 3600 });
  return c.html(html);
});

// ---- /wikifunctions/verify — verification-methodology explainer (static) ----
// Sibling of /wikifunctions: how the Wikifunctions↔Mathlib proofs work (three
// layers of assurance + two worked examples). Pure-function output, same KV
// cache pattern; bump the suffix when the page copy changes. Registered before
// the /:slug catch-all (which only matches a single path segment anyway).
app.get("/wikifunctions/verify", async (c) => {
  const cacheKey = "page:wikifunctions-verify:v2";  // v2: dark-mode theme script + toggle
  const cached = await c.env.RENDER_CACHE.get(cacheKey);
  if (cached) return c.html(cached);
  const html = wikifunctionsVerifyPage();
  await c.env.RENDER_CACHE.put(cacheKey, html, { expirationTtl: 3600 });
  return c.html(html);
});

// ---- /stats — live experiment instrumentation (P2a; public) ----
// Every number is a cheap SQL aggregate: the per-article count columns (D-C5)
// plus GROUP BYs over annotation_events / flags / revisions / pipeline_runs.
// Annotation blobs are never parsed here. KV-cached with TTL-only
// invalidation (a write can be up to 5 minutes stale — fine for a dashboard).
app.get("/stats", async (c) => {
  const cacheKey = "page:stats:v2";  // v2: dark-mode theme script + toggle
  const cached = await c.env.RENDER_CACHE.get(cacheKey);
  if (cached) return c.html(cached);
  const db = drizzle(c.env.DB);
  const cutoff = Date.now() - 30 * 24 * 3600 * 1000; // "fresh"/"last 30d" boundary

  // Articles by review state (never/fresh/stale, drifted, parked). LEFT JOIN:
  // no moderation row reads as never-reviewed/not-drifted/not-parked.
  const review = (
    await db
      .select({
        total: sql<number>`COUNT(*)`,
        neverReviewed: sql<number>`COALESCE(SUM(CASE WHEN ${moderationState.lastReviewedAt} IS NULL THEN 1 ELSE 0 END), 0)`,
        fresh: sql<number>`COALESCE(SUM(CASE WHEN ${moderationState.lastReviewedAt} >= ${cutoff} THEN 1 ELSE 0 END), 0)`,
        stale: sql<number>`COALESCE(SUM(CASE WHEN ${moderationState.lastReviewedAt} < ${cutoff} THEN 1 ELSE 0 END), 0)`,
        drifted: sql<number>`COALESCE(SUM(CASE WHEN COALESCE(${moderationState.wpDrifted}, 0) = 1 THEN 1 ELSE 0 END), 0)`,
        parked: sql<number>`COALESCE(SUM(CASE WHEN ${moderationState.state} IN ('moved','deleted','needs_human') THEN 1 ELSE 0 END), 0)`,
      })
      .from(articles)
      .leftJoin(moderationState, eq(moderationState.slug, articles.slug))
  )[0];

  // Annotation totals by status from the count columns (NOT a blob parse —
  // see the page's own note; provenance signals come from events).
  const ann = (
    await db
      .select({
        formalized: sql<number>`COALESCE(SUM(${articles.nFormalized}), 0)`,
        partial: sql<number>`COALESCE(SUM(${articles.nPartial}), 0)`,
        notFormalized: sql<number>`COALESCE(SUM(${articles.nNotFormalized}), 0)`,
        pendingCounts: sql<number>`COALESCE(SUM(CASE WHEN ${articles.nFormalized} IS NULL THEN 1 ELSE 0 END), 0)`,
      })
      .from(articles)
  )[0];

  const events: StatsEventCell[] = await db
    .select({
      eventType: annotationEvents.eventType,
      actorType: annotationEvents.actorType,
      allTime: sql<number>`COUNT(*)`,
      last30d: sql<number>`COALESCE(SUM(CASE WHEN ${annotationEvents.createdAt} >= ${cutoff} THEN 1 ELSE 0 END), 0)`,
    })
    .from(annotationEvents)
    .groupBy(annotationEvents.eventType, annotationEvents.actorType)
    .orderBy(annotationEvents.eventType, annotationEvents.actorType);

  const flagAgg = (
    await db
      .select({
        open: sql<number>`COALESCE(SUM(CASE WHEN ${flags.status} = 'open' THEN 1 ELSE 0 END), 0)`,
        fixed: sql<number>`COALESCE(SUM(CASE WHEN ${flags.status} = 'fixed' THEN 1 ELSE 0 END), 0)`,
        dismissed: sql<number>`COALESCE(SUM(CASE WHEN ${flags.status} = 'dismissed' THEN 1 ELSE 0 END), 0)`,
      })
      .from(flags)
  )[0];

  const revByKind = await db
    .select({ kind: revisions.kind, count: sql<number>`COUNT(*)` })
    .from(revisions)
    .groupBy(revisions.kind)
    .orderBy(revisions.kind);

  const patrol = (
    await db
      .select({
        unpatrolledEdits: sql<number>`COALESCE(SUM(CASE WHEN ${revisions.kind} = 'edit' AND ${revisions.patrolledBy} IS NULL THEN 1 ELSE 0 END), 0)`,
        patrolledEdits: sql<number>`COALESCE(SUM(CASE WHEN ${revisions.kind} = 'edit' AND ${revisions.patrolledBy} IS NOT NULL THEN 1 ELSE 0 END), 0)`,
      })
      .from(revisions)
  )[0];

  const runs = await db
    .select({
      kind: pipelineRuns.kind,
      runs: sql<number>`COUNT(*)`,
      articles: sql<number>`COALESCE(SUM(${pipelineRuns.articlesProcessed}), 0)`,
      errors: sql<number>`COALESCE(SUM(${pipelineRuns.errors}), 0)`,
      tokens: sql<number>`COALESCE(SUM(${pipelineRuns.tokens}), 0)`,
      // SUM over all-NULL costs is NULL → rendered as "—" (unknown ≠ $0).
      cost: sql<number | null>`SUM(${pipelineRuns.costUsdEquiv})`,
    })
    .from(pipelineRuns)
    .groupBy(pipelineRuns.kind)
    .orderBy(pipelineRuns.kind);

  const html = statsPage({
    articles: review,
    annotations: ann,
    events,
    flags: flagAgg,
    revisions: revByKind,
    patrol,
    runs,
  });
  await c.env.RENDER_CACHE.put(cacheKey, html, { expirationTtl: 300 });
  return c.html(html);
});

// ---- recent changes (global patrol feed) ----
// ?kind=edit|revert|seed|pipeline|contribution narrows to one revisions.kind
// (anything else reads as "all"). Patrol affordances: see pages.ts patrolCell.
app.get("/recent-changes", async (c) => {
  const kindParam = c.req.query("kind") ?? null;
  const kind =
    kindParam !== null && (RECENT_KINDS as readonly string[]).includes(kindParam) ? kindParam : null;
  // P3: ?watching=1 filters to articles in the viewer's watchlist. Requires
  // login — anonymous users with the param get the unfiltered feed, no error.
  const wantWatching = c.req.query("watching") === "1";
  const user = await getUser(c);
  const watching = wantWatching && user !== null;

  const db = drizzle(c.env.DB);
  const patrollers = alias(users, "patrollers");
  let watchedSlugs: Set<string> | null = null;
  if (watching) {
    const ws = await db
      .select({ slug: watchlist.slug })
      .from(watchlist)
      .where(eq(watchlist.userId, user!.id));
    watchedSlugs = new Set(ws.map((r) => r.slug));
    if (watchedSlugs.size === 0) {
      // No watched slugs → no rows, skip the query entirely.
      const html = recentChangesPage([], {
        kind,
        canPatrol: user.role === "patroller" || user.role === "admin",
        watching: true,
        loggedIn: true,
      });
      return c.html(html);
    }
  }

  const rows = await db
    .select({
      id: revisions.id,
      slug: revisions.slug,
      userId: revisions.userId,
      comment: revisions.comment,
      kind: revisions.kind,
      patrolledBy: revisions.patrolledBy,
      patrolledAt: revisions.patrolledAt,
      createdAt: revisions.createdAt,
      displayTitle: articles.displayTitle,
      userName: users.name,
      patrollerName: sql<string | null>`${patrollers.name}`.as("patroller_name"),
    })
    .from(revisions)
    .leftJoin(articles, eq(articles.slug, revisions.slug))
    .leftJoin(users, eq(users.id, revisions.userId))
    .leftJoin(patrollers, eq(patrollers.id, revisions.patrolledBy))
    .where(kind === null ? undefined : eq(revisions.kind, kind))
    .orderBy(desc(revisions.createdAt))
    .limit(watching ? 500 : 100); // wider pre-filter window when filtering watchlist
  const canPatrol = user !== null && (user.role === "patroller" || user.role === "admin");
  const filteredRows = watchedSlugs ? rows.filter((r) => watchedSlugs!.has(r.slug)).slice(0, 100) : rows;
  const html = recentChangesPage(
    filteredRows.map((r) => ({
      slug: r.slug,
      displayTitle: r.displayTitle ?? r.slug,
      id: r.id,
      userId: r.userId,
      userName: r.userName,
      comment: r.comment,
      createdAt: r.createdAt,
      kind: r.kind,
      patrolledBy: r.patrolledBy,
      patrolledAt: r.patrolledAt,
      patrollerName: r.patrollerName,
    })),
    { kind, canPatrol, watching, loggedIn: user !== null },
  );
  return c.html(html);
});

// ---- /u/:id — user profile (P3 contribution-loop) --------------------------
// Public read of any user's stats + recent revisions. When the viewer IS the
// profile owner, also includes their watchlist (private signal — not exposed
// to other users; we don't want to leak which articles someone follows).
app.get("/u/:id", async (c) => {
  const profileId = c.req.param("id");
  const db = drizzle(c.env.DB);
  const profile = (await db.select().from(users).where(eq(users.id, profileId)).limit(1))[0];
  if (!profile) return c.notFound();
  const viewer = await getUser(c);
  const isSelf = viewer !== null && viewer.id === profile.id;

  // Stats. One round-trip per metric is fine — these are cheap on the indexed
  // user_id column.
  const [totalEdits, articlesTouched, humanRevs] = await Promise.all([
    db.select({ n: sql<number>`count(*)` }).from(revisions).where(eq(revisions.userId, profile.id)),
    db
      .select({ n: sql<number>`count(distinct ${revisions.slug})` })
      .from(revisions)
      .where(eq(revisions.userId, profile.id)),
    db
      .select({ n: sql<number>`count(*)` })
      .from(revisions)
      .where(and(eq(revisions.userId, profile.id), eq(revisions.kind, "edit"))),
  ]);

  const recent = await db
    .select({
      id: revisions.id,
      slug: revisions.slug,
      comment: revisions.comment,
      kind: revisions.kind,
      createdAt: revisions.createdAt,
      displayTitle: articles.displayTitle,
    })
    .from(revisions)
    .leftJoin(articles, eq(articles.slug, revisions.slug))
    .where(eq(revisions.userId, profile.id))
    .orderBy(desc(revisions.createdAt))
    .limit(50);

  let watching: string[] = [];
  if (isSelf) {
    const ws = await db
      .select({ slug: watchlist.slug })
      .from(watchlist)
      .where(eq(watchlist.userId, profile.id))
      .orderBy(desc(watchlist.createdAt));
    watching = ws.map((r) => r.slug);
  }

  const html = userProfilePage(
    {
      id: profile.id,
      name: profile.name,
      image: profile.image,
      role: profile.role,
      // users.createdAt is stored as `timestamp` (Date object via Drizzle); the
      // page helper takes ms (so format/relative-time work). Fall back to null.
      createdAt: profile.createdAt instanceof Date ? profile.createdAt.getTime() : null,
    },
    {
      totalEdits: totalEdits[0]?.n ?? 0,
      articlesTouched: articlesTouched[0]?.n ?? 0,
      humanRevs: humanRevs[0]?.n ?? 0,
    },
    recent.map(
      (r): UserProfileRow => ({
        id: r.id,
        slug: r.slug,
        displayTitle: r.displayTitle ?? r.slug,
        comment: r.comment,
        kind: r.kind,
        createdAt: r.createdAt,
      }),
    ),
    watching,
    isSelf,
  );
  return c.html(html);
});

// ---- /api/watch/:slug — toggle watchlist entry (P3) ------------------------
// POST adds (idempotent — duplicate rows would 1555 in SQLite; OR IGNORE keeps
// the round-trip a single insert), DELETE removes.
app.post("/api/watch/:slug", async (c) => {
  const user = await getUser(c);
  if (!user) return c.json({ ok: false, error: "login required" }, 401);
  const slug = c.req.param("slug");
  const db = drizzle(c.env.DB);
  // Confirm slug exists — don't let clients spam arbitrary keys.
  const art = (await db.select({ slug: articles.slug }).from(articles).where(eq(articles.slug, slug)).limit(1))[0];
  if (!art) return c.json({ ok: false, error: "unknown slug" }, 404);
  await c.env.DB.prepare(
    "INSERT OR IGNORE INTO watchlist (user_id, slug, created_at) VALUES (?, ?, ?)",
  )
    .bind(user.id, slug, Date.now())
    .run();
  return c.json({ ok: true, watching: true });
});
app.delete("/api/watch/:slug", async (c) => {
  const user = await getUser(c);
  if (!user) return c.json({ ok: false, error: "login required" }, 401);
  const slug = c.req.param("slug");
  const db = drizzle(c.env.DB);
  await db
    .delete(watchlist)
    .where(and(eq(watchlist.userId, user.id), eq(watchlist.slug, slug)));
  return c.json({ ok: true, watching: false });
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
    proposal_id?: unknown;
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
    // endorse / approve / reject are session-only human actions — a bot may
    // never approve its own proposal (same 403 as endorse).
    if (isBot) return c.json({ ok: false, error: "forbidden" }, 403);

    // ---- approve/reject a pending AI proposal (docs/propose-then-approve.md) --
    // Proposals live inert in moderation_state. Approving applies the delta to
    // the target human annotation IN PLACE and keeps provenance:'human' (Jack
    // owns it now — findLostHuman keeps protecting it). Rejecting remembers the
    // delta so the agent won't re-propose it. The agent never mutates here.
    if (posted.action === "approve_proposal" || posted.action === "reject_proposal") {
      if (!isProposalId(posted.proposal_id)) return c.json({ ok: false, error: "bad proposal_id" }, 400);
      const proposalId = posted.proposal_id;
      const ms = (await db.select().from(moderationState).where(eq(moderationState.slug, slug)).limit(1))[0];
      const pending = parsePending(ms?.proposal);
      const prop = pending.find((p) => p.proposalId === proposalId);
      if (!prop) return c.json({ ok: false, error: "proposal not found" }, 404);
      const now = Date.now();
      const remaining = pending.filter((p) => p.proposalId !== proposalId);
      const remainingJson = remaining.length ? JSON.stringify(remaining) : null;

      if (posted.action === "reject_proposal") {
        const rejected = parseRejected(ms?.rejectedProposals);
        rejected.push({ annotationId: prop.annotationId, fieldsSig: fieldsSig(prop.fields) });
        await db
          .update(moderationState)
          .set({ proposal: remainingJson, rejectedProposals: JSON.stringify(rejected.slice(-500)), updatedAt: now })
          .where(eq(moderationState.slug, slug));
        console.log(JSON.stringify({ event: "proposal-reject", slug, user_id: user.id, proposal_id: proposalId, t: now }));
        return c.json({ ok: true, rejected: true });
      }

      // approve_proposal — mutates the annotation, so CAS on version.
      if (typeof posted.base_version !== "number") return c.json({ ok: false, error: "base_version required" }, 400);
      if (posted.base_version !== row.version) {
        return c.json({ error: "stale", version: row.version, annotations: JSON.parse(row.annotations) }, 409);
      }
      const stored = JSON.parse(row.annotations) as AnnRecord[];
      const idx = stored.findIndex((a) => a.id === prop.annotationId);
      if (idx === -1) {
        // Target annotation gone — drop the stale proposal, no content write.
        await db.update(moderationState).set({ proposal: remainingJson, updatedAt: now }).where(eq(moderationState.slug, slug));
        return c.json({ ok: false, error: "annotation gone", dropped: true }, 409);
      }
      const { next, changed } = applyProposalFields(stored[idx], prop.fields);
      if (changed.length === 0) {
        // Already matches (e.g. applied by an earlier edit) — just clear it.
        await db.update(moderationState).set({ proposal: remainingJson, updatedAt: now }).where(eq(moderationState.slug, slug));
        return c.json({ ok: true, noop: true, version: row.version });
      }
      next.provenance = "human"; // Jack approved it; keep it human-protected
      const finals = stored.slice();
      finals[idx] = next;
      const finalsJson = JSON.stringify(finals);
      const validationErr = validateAnnotations(c, finals, finalsJson);
      if (validationErr) return validationErr;
      const newVersion = row.version + 1;
      const updated = await db
        .update(articles)
        .set({ annotations: finalsJson, version: newVersion, updatedAt: now, ...statusCounts(finals) })
        .where(and(eq(articles.slug, slug), eq(articles.version, row.version)));
      if (updated.meta.changes === 0) {
        const fresh = (await db.select().from(articles).where(eq(articles.slug, slug)).limit(1))[0];
        return c.json({ error: "stale", version: fresh ? fresh.version : row.version, annotations: fresh ? JSON.parse(fresh.annotations) : [] }, 409);
      }
      const prior = (
        await db.select({ id: revisions.id }).from(revisions).where(eq(revisions.slug, slug)).orderBy(desc(revisions.id)).limit(1)
      )[0];
      await db.batch([
        db.insert(revisions).values({
          slug,
          userId: user.id,
          annotations: finalsJson,
          comment: `proposal-approved:${proposalId}`,
          kind: "proposal-approved",
          parentId: prior?.id ?? null,
          createdAt: now,
        }),
        ...eventInsertStatements(db, [
          {
            revisionId: newRevisionId(slug),
            slug,
            annotationId: prop.annotationId,
            eventType: "modify",
            actorType: "human",
            userId: user.id,
            fieldChanges: serializeFieldChanges(changed),
            createdAt: now,
          },
        ]),
        db.update(moderationState).set({ proposal: remainingJson, updatedAt: now }).where(eq(moderationState.slug, slug)),
      ]);
      console.log(JSON.stringify({ event: "proposal-approve", slug, user_id: user.id, proposal_id: proposalId, version: newVersion, t: now }));
      return c.json({ ok: true, version: newVersion });
    }

    if (posted.action !== "endorse") return c.json({ ok: false, error: "unknown action" }, 400);
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

  // Propose-then-approve: a bot review may carry proposals to update human
  // annotations it could NOT change (findLostHuman preserved them verbatim).
  // Merge any into moderation_state.proposal — inert advisory data until Jack
  // approves — deduped vs already-pending / previously-rejected deltas, and only
  // for proposals that target a currently-live annotation id. Runs before the
  // no-op short-circuit so a "reviewed, changed nothing but proposed X" pass is
  // captured. Never touches articles.annotations.
  const incomingProposals =
    isBot && posted.meta && typeof posted.meta === "object"
      ? (posted.meta as { ladder?: { proposals?: unknown } }).ladder?.proposals
      : undefined;
  if (isBot && Array.isArray(incomingProposals) && incomingProposals.length > 0) {
    const pnow = Date.now();
    const ms = (await db.select().from(moderationState).where(eq(moderationState.slug, slug)).limit(1))[0];
    const existingPending = parsePending(ms?.proposal);
    const merged = mergeProposals(existingPending, parseRejected(ms?.rejectedProposals), incomingProposals, {
      now: pnow,
      runId: (posted.meta as { run_id?: unknown }).run_id as string | undefined,
      model: (posted.meta as { model?: unknown }).model as string | undefined,
      validIds: new Set(
        finalAnnotations.map((a) => a.id).filter((x): x is string => typeof x === "string"),
      ),
    });
    if (merged.length > existingPending.length) {
      const proposalJson = JSON.stringify(merged);
      await db
        .insert(moderationState)
        .values({ slug, proposal: proposalJson, updatedAt: pnow })
        .onConflictDoUpdate({ target: moderationState.slug, set: { proposal: proposalJson, updatedAt: pnow } });
    }
  }

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

// ---- run-level revert: undo a whole AI-moderation batch (admin OR bot) ------
// POST /api/admin/revert-run/:runId — insurance against a bad pipeline batch.
// A pipeline run stamps every revision it writes with meta.run_id = <runId>
// (and a comment like `ai-moderate:review:<runId>` / `wp-update:stage0:<runId>`,
// the LIKE fallback for rows whose meta is null). This finds every article the
// run touched and rolls each back to the state JUST BEFORE the run first
// touched it — reusing the exact save/revert mechanics (CAS-guarded UPDATE,
// kind='revert' revision, revert_restore events, recomputed counts, one batch
// per slug).
//
// SAFETY + IDEMPOTENCE SEMANTICS (load-bearing — read before changing):
//   * HUMAN-PRESERVATION: if a human revision (kind 'edit'/'revert') landed
//     AFTER the run's last touch of a slug, that slug is SKIPPED
//     ('human-edited-since'). We never clobber human work to undo a bot batch;
//     the human can revert the offending revision themselves. Seed/pipeline
//     revisions after the run do NOT block (they aren't human work).
//   * ALREADY-REVERTED (the re-run guard): if this endpoint has already
//     reverted a slug for THIS run, its latest revision is our own marker
//     (kind='revert', comment `revert-run:<runId>`). A second call detects that
//     marker as the latest revision and skips the slug ('already-reverted') —
//     it does NOT re-revert. A no-op (the article is already byte-identical to
//     its pre-run state, e.g. the run changed nothing for this slug, or a human
//     already restored it) is likewise reported 'already-reverted' and writes
//     nothing. NB: the run's own revisions stay in history on a second call —
//     we key idempotence off "is the slug already at its pre-run state / behind
//     our marker", not off deleting the run's rows.
//   * CONFLICT: each slug reverts independently under its own CAS. A concurrent
//     write that bumps the version between our read and UPDATE leaves the slug
//     'conflict' (no error) — the rest of the batch still reverts.
//   * PRE-RUN STATE: restore the annotations of the revision immediately before
//     the run FIRST touched the slug (the earliest run revision's parent_id if
//     set, else the highest-id revision below it). If the run CREATED the
//     article (no prior revision), the pre-run state is the empty array.
//   * Unknown / no-touch runId → 200 {reverted:[], skipped:[]} (idempotent, not
//     404): "revert this run" is a safe no-op when there's nothing to undo.
app.post("/api/admin/revert-run/:runId", async (c) => {
  const originErr = checkOrigin(c);
  if (originErr) return originErr;
  // Admin session OR the pipeline bot (the insurance is run by either an
  // operator from the site or the runner/ops tooling holding the bearer).
  const user = await requireRole(c, ["admin", "bot"]);
  if (!user) return c.json({ ok: false, error: "forbidden" }, 403);
  const { success: allowed } = await c.env.EDIT_LIMITER.limit({ key: `edit:${user.id}` });
  if (!allowed) return c.json({ ok: false, error: "rate limited — slow down" }, 429);
  const runId = c.req.param("runId");
  if (typeof runId !== "string" || runId.length === 0 || runId.length > MAX_FIELD_LEN) {
    return c.json({ ok: false, error: "bad run id" }, 400);
  }
  const revertComment = `revert-run:${runId}`;

  const db = drizzle(c.env.DB);
  // Every revision the run stamped, keyed by run_id in meta (the LIKE fallback
  // catches rows whose meta is null — comment ends in `:<runId>`). Drizzle's
  // sql template parameterizes runId, so it's injection-safe.
  const runRevs = await db
    .select({ id: revisions.id, slug: revisions.slug })
    .from(revisions)
    .where(
      sql`json_extract(${revisions.meta}, '$.run_id') = ${runId} OR (${revisions.meta} IS NULL AND ${revisions.comment} LIKE ${"%:" + runId})`,
    );

  // Group the run's revisions by slug; track the earliest and latest run
  // revision id per slug (earliest = where to revert TO the state before;
  // latest = the watermark for the human-edited-since check).
  const bySlug = new Map<string, { earliest: number; latest: number }>();
  for (const r of runRevs) {
    const cur = bySlug.get(r.slug);
    if (!cur) bySlug.set(r.slug, { earliest: r.id, latest: r.id });
    else {
      if (r.id < cur.earliest) cur.earliest = r.id;
      if (r.id > cur.latest) cur.latest = r.id;
    }
  }

  const reverted: string[] = [];
  const skipped: Array<{ slug: string; reason: string }> = [];

  for (const [slug, { earliest, latest }] of bySlug) {
    // All revisions for the slug (id-ordered): the pre-run snapshot, the
    // human-edited-since watermark, and the already-reverted marker all read
    // off this one list.
    const revs = await db
      .select({
        id: revisions.id,
        kind: revisions.kind,
        comment: revisions.comment,
        parentId: revisions.parentId,
        annotations: revisions.annotations,
      })
      .from(revisions)
      .where(eq(revisions.slug, slug))
      .orderBy(revisions.id);
    const runIds = new Set(runRevs.filter((r) => r.slug === slug).map((r) => r.id));

    // ALREADY-REVERTED: our own marker is the slug's latest revision → this run
    // was already reverted here; do not re-revert.
    const top = revs[revs.length - 1];
    if (top && top.kind === "revert" && top.comment === revertComment) {
      skipped.push({ slug, reason: "already-reverted" });
      continue;
    }

    // HUMAN-PRESERVATION: a human edit/revert landed after the run's last touch.
    const humanAfter = revs.some(
      (r) => r.id > latest && !runIds.has(r.id) && (r.kind === "edit" || r.kind === "revert"),
    );
    if (humanAfter) {
      skipped.push({ slug, reason: "human-edited-since" });
      continue;
    }

    // PRE-RUN STATE: the revision immediately before the run's earliest touch —
    // parent_id if it points at a real revision, else the highest id below the
    // earliest run revision. No prior revision (run created the article) → [].
    const earliestRev = revs.find((r) => r.id === earliest);
    let preRev: { annotations: string } | undefined;
    if (earliestRev?.parentId != null) {
      preRev = revs.find((r) => r.id === earliestRev.parentId);
    }
    if (!preRev) {
      const below = revs.filter((r) => r.id < earliest);
      preRev = below.length > 0 ? below[below.length - 1] : undefined;
    }
    const preAnnotations: AnnRecord[] = preRev ? (JSON.parse(preRev.annotations) as AnnRecord[]) : [];
    const preAnnJson = JSON.stringify(preAnnotations);

    // Current article state (for CAS + the by-id event diff).
    const row = (await db.select().from(articles).where(eq(articles.slug, slug)).limit(1))[0];
    if (!row) {
      // The run touched a revision for a slug with no article row — nothing to
      // restore (shouldn't happen; revisions outlive deletes only via a path we
      // don't ship). Treat as a conflict-class skip rather than erroring.
      skipped.push({ slug, reason: "conflict" });
      continue;
    }
    const current = JSON.parse(row.annotations) as AnnRecord[];

    // No-op: already byte-identical to the pre-run state (run changed nothing
    // here, or a human already restored it). Report 'already-reverted', write
    // nothing — keeps the revert log clean and the operation idempotent.
    if (deepEqual(current, preAnnotations)) {
      skipped.push({ slug, reason: "already-reverted" });
      continue;
    }

    const now = Date.now();
    const newVersion = row.version + 1;
    // Same CAS contract as the single-revision revert: guard the UPDATE on the
    // version we read; 0 rows changed = a concurrent write raced us → 'conflict'.
    const updated = await db
      .update(articles)
      .set({
        annotations: preAnnJson,
        version: newVersion,
        updatedAt: now,
        ...statusCounts(preAnnotations),
      })
      .where(and(eq(articles.slug, slug), eq(articles.version, row.version)));
    if (updated.meta.changes === 0) {
      skipped.push({ slug, reason: "conflict" });
      continue;
    }
    const prior = (
      await db
        .select({ id: revisions.id })
        .from(revisions)
        .where(eq(revisions.slug, slug))
        .orderBy(desc(revisions.id))
        .limit(1)
    )[0];
    // F9: revision + revert_restore events land in one atomic batch (per slug).
    const revertBatch: WriteBatch = [
      db.insert(revisions).values({
        slug,
        userId: user.id,
        annotations: preAnnJson,
        comment: revertComment,
        kind: "revert",
        meta: JSON.stringify({ reverted_run: runId }),
        parentId: prior?.id ?? null,
        createdAt: now,
      }),
      ...eventInsertStatements(
        db,
        eventRowsFromChanges(diffAnnotations(current, preAnnotations), {
          revisionId: newRevisionId(slug),
          slug,
          actorType: user.role === "bot" ? "pipeline" : "human",
          userId: user.id,
          now,
          eventTypeOverride: "revert_restore",
        }),
      ),
    ];
    await db.batch(revertBatch);
    reverted.push(slug);
  }

  console.log(
    JSON.stringify({
      event: "revert-run",
      run_id: runId,
      user_id: user.id,
      reverted: reverted.length,
      skipped: skipped.length,
      t: Date.now(),
    }),
  );
  return c.json({ ok: true, run_id: runId, reverted, skipped });
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

// ---- pipeline-run registry (P2a; cross-agent contract RUNS-API) -------------
// POST /api/runs — bot bearer only. The runner reports each invocation once,
// after it finishes; retries are idempotent on run_id (duplicate → 200
// {ok:true, duplicate:true}, first-report row wins and is never overwritten).
const RUN_ID_RE = /^[0-9a-f]{8}$/;
const RUN_KINDS = new Set(["review", "wp-update", "new", "all"]);

app.post("/api/runs", async (c) => {
  const originErr = checkOrigin(c);
  if (originErr) return originErr;
  const user = await requireRole(c, ["bot"]);
  if (!user) return c.json({ ok: false, error: "forbidden" }, 403);
  let posted: Record<string, unknown>;
  try {
    posted = await c.req.json();
  } catch {
    return c.json({ ok: false, error: "bad json" }, 400);
  }
  if (typeof posted.run_id !== "string" || !RUN_ID_RE.test(posted.run_id)) {
    return c.json({ ok: false, error: "bad run_id" }, 400);
  }
  if (typeof posted.kind !== "string" || !RUN_KINDS.has(posted.kind)) {
    return c.json({ ok: false, error: "bad kind" }, 400);
  }
  // Optional identifier-sized strings.
  for (const f of ["model", "prompt_sha"] as const) {
    const v = posted[f];
    if (v !== undefined && v !== null && (typeof v !== "string" || v.length === 0 || v.length > MAX_FIELD_LEN)) {
      return c.json({ ok: false, error: `bad ${f}` }, 400);
    }
  }
  // Required non-negative integers (ms timestamps + counters).
  for (const f of ["started_at", "finished_at", "articles_processed", "errors", "tokens"] as const) {
    const v = posted[f];
    if (typeof v !== "number" || !Number.isInteger(v) || v < 0) {
      return c.json({ ok: false, error: `bad ${f}` }, 400);
    }
  }
  let cost: number | null = null;
  if (posted.cost_usd_equiv !== undefined && posted.cost_usd_equiv !== null) {
    if (
      typeof posted.cost_usd_equiv !== "number" ||
      !Number.isFinite(posted.cost_usd_equiv) ||
      posted.cost_usd_equiv < 0
    ) {
      return c.json({ ok: false, error: "bad cost_usd_equiv" }, 400);
    }
    cost = posted.cost_usd_equiv;
  }
  // notes is free text — size-capped like every other free-text field.
  let notes: string | null = null;
  if (posted.notes !== undefined && posted.notes !== null) {
    if (typeof posted.notes !== "string") return c.json({ ok: false, error: "bad notes" }, 400);
    if (posted.notes.length > MAX_TEXT_LEN) return c.json({ ok: false, error: "notes too long" }, 413);
    notes = posted.notes;
  }
  const now = Date.now();
  const db = drizzle(c.env.DB);
  // Idempotency without a pre-select race: INSERT OR IGNORE on the run_id PK;
  // 0 rows changed = this run was already reported.
  const inserted = await db
    .insert(pipelineRuns)
    .values({
      runId: posted.run_id,
      kind: posted.kind,
      model: typeof posted.model === "string" ? posted.model : null,
      promptSha: typeof posted.prompt_sha === "string" ? posted.prompt_sha : null,
      startedAt: posted.started_at as number,
      finishedAt: posted.finished_at as number,
      articlesProcessed: posted.articles_processed as number,
      errors: posted.errors as number,
      tokens: posted.tokens as number,
      costUsdEquiv: cost,
      notes,
      createdAt: now,
    })
    .onConflictDoNothing({ target: pipelineRuns.runId });
  if (inserted.meta.changes === 0) return c.json({ ok: true, duplicate: true });
  console.log(
    JSON.stringify({
      event: "run-report",
      run_id: posted.run_id,
      kind: posted.kind,
      articles: posted.articles_processed,
      errors: posted.errors,
      tokens: posted.tokens,
      t: now,
    }),
  );
  return c.json({ ok: true });
});

// ---- research export (P2a): pseudonymized annotation_events JSONL ----------
// GET /api/research/export.jsonl — bot bearer OR admin session. Streams one
// JSON object per annotation_events row (id-ascending, joined to its
// revision), paged through D1 so the response never materializes in memory.
// Privacy contract: NO emails, IPs, names, or free-text comments — the only
// user-linked field is `pseudonym`, sha256(user_id + PIPELINE_TOKEN-as-salt)
// truncated to 12 hex (stable across exports while the secret is stable;
// rotating PIPELINE_TOKEN rotates pseudonyms, which is acceptable — it
// unlinks, never re-identifies). Pipeline events carry pseudonym null.
app.get("/api/research/export.jsonl", async (c) => {
  const user = await requireRole(c, ["bot", "admin"]);
  if (!user) return c.json({ ok: false, error: "forbidden" }, 403);
  const db = drizzle(c.env.DB);
  const salt = c.env.PIPELINE_TOKEN ?? "";
  const pseudoCache = new Map<string, string>();
  const pseudonymFor = async (userId: string): Promise<string> => {
    let p = pseudoCache.get(userId);
    if (p === undefined) {
      p = (await sha256Hex(userId + salt)).slice(0, 12);
      pseudoCache.set(userId, p);
    }
    return p;
  };

  const PAGE = 500;
  let cursor = 0;
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    pull: async (controller) => {
      const rows = await db
        .select({
          id: annotationEvents.id,
          slug: annotationEvents.slug,
          annotationId: annotationEvents.annotationId,
          eventType: annotationEvents.eventType,
          actorType: annotationEvents.actorType,
          userId: annotationEvents.userId,
          fieldChanges: annotationEvents.fieldChanges,
          revisionId: annotationEvents.revisionId,
          createdAt: annotationEvents.createdAt,
          revisionKind: revisions.kind,
          revisionMeta: revisions.meta,
        })
        .from(annotationEvents)
        .leftJoin(revisions, eq(revisions.id, annotationEvents.revisionId))
        .where(gt(annotationEvents.id, cursor))
        .orderBy(annotationEvents.id)
        .limit(PAGE);
      if (rows.length === 0) {
        controller.close();
        return;
      }
      cursor = rows[rows.length - 1].id;
      let out = "";
      for (const r of rows) {
        // run_id lives in revisions.meta JSON (the runner's ID3 stamp).
        let runId: string | null = null;
        if (r.revisionMeta) {
          try {
            const m = JSON.parse(r.revisionMeta) as Record<string, unknown>;
            if (typeof m.run_id === "string") runId = m.run_id;
          } catch {
            // corrupt meta → run_id null
          }
        }
        let fieldChanges: unknown = null;
        if (r.fieldChanges) {
          try {
            fieldChanges = JSON.parse(r.fieldChanges);
          } catch {
            fieldChanges = null;
          }
        }
        out +=
          JSON.stringify({
            event_id: r.id,
            slug: r.slug,
            annotation_id: r.annotationId,
            event_type: r.eventType,
            actor_type: r.actorType,
            pseudonym:
              r.actorType === "pipeline" || r.userId === null ? null : await pseudonymFor(r.userId),
            field_changes: fieldChanges,
            revision_id: r.revisionId,
            revision_kind: r.revisionKind,
            run_id: runId,
            created_at: r.createdAt,
          }) + "\n";
      }
      controller.enqueue(encoder.encode(out));
    },
  });
  return new Response(body, {
    headers: {
      "Content-Type": "application/x-ndjson; charset=utf-8",
      "Cache-Control": "no-store",
    },
  });
});

// ---- mark a human revision patrolled (P2a patrol polish) --------------------
// patroller/admin only. Only kind='edit' revisions take a patrol mark (seed/
// pipeline/revert rows aren't patrol targets; 'contribution' joins when that
// flow ships). CAS-ish: the UPDATE is guarded on patrolled_by IS NULL, so a
// repeat (or a raced double-click) returns 200 {ok:true, duplicate:true} and
// never overwrites the first patroller's mark.
app.post("/api/revision/:id/patrol", async (c) => {
  const originErr = checkOrigin(c);
  if (originErr) return originErr;
  const user = await requireRole(c, ["patroller", "admin"]);
  if (!user) return c.json({ ok: false, error: "forbidden" }, 403);
  const id = parseInt(c.req.param("id"), 10);
  if (Number.isNaN(id)) return c.json({ ok: false, error: "bad revision id" }, 400);
  const db = drizzle(c.env.DB);
  const rev = (
    await db
      .select({ id: revisions.id, kind: revisions.kind, patrolledBy: revisions.patrolledBy })
      .from(revisions)
      .where(eq(revisions.id, id))
      .limit(1)
  )[0];
  if (!rev) return c.json({ ok: false, error: "unknown revision" }, 404);
  if (rev.kind !== "edit") return c.json({ ok: false, error: "only human edits are patrolled" }, 400);
  const now = Date.now();
  const updated = await db
    .update(revisions)
    .set({ patrolledBy: user.id, patrolledAt: now })
    .where(and(eq(revisions.id, id), isNull(revisions.patrolledBy)));
  if (updated.meta.changes === 0) return c.json({ ok: true, duplicate: true });
  console.log(JSON.stringify({ event: "patrol", revision_id: id, user_id: user.id, t: now }));
  return c.json({ ok: true });
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
  // P3: look up the viewer's watch state for this slug so the ★ Watch toggle
  // renders with the correct initial state (no client roundtrip on first paint).
  let isWatching = false;
  if (user) {
    const w = await db
      .select({ slug: watchlist.slug })
      .from(watchlist)
      .where(and(eq(watchlist.userId, user.id), eq(watchlist.slug, slug)))
      .limit(1);
    isWatching = w.length > 0;
  }
  // latestRevid/revid feed the per-request staleness banner (rendered by
  // injectAuthAndEditor — never baked into the cached base page).
  const page = injectAuthAndEditor(base, {
    slug,
    user,
    annotations,
    version: row.version,
    latestRevid: row.latestRevid,
    revid: row.revid,
    isWatching,
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
