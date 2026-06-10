// Wikipedia drift detection (P1). A daily cron walks the article corpus in
// slug order — resuming from a rotating KV cursor — and asks MediaWiki for
// each page's current lastrevid (action=query&prop=info, 50 titles per
// request). Findings per article:
//   lastrevid > pinned revid → articles.latest_revid + last_upstream_check,
//       and moderation_state.wp_drifted=1 (feeds /api/work mode=wp-update)
//   unchanged                → articles.last_upstream_check only
//   page missing             → moderation_state.state='deleted'
//   page is now a redirect   → moderation_state.state='moved'
//
// CACHE INVARIANT: this module writes latest_revid / last_upstream_check and
// moderation_state ONLY — it must NEVER touch articles.version (or revid /
// annotations). `revid` advances only atomically with a re-anchored
// annotations payload (the wp-update bot flow); the staleness UI is injected
// per-request, never baked into the cached base page, so drift bookkeeping
// must not bust the render cache.

import { drizzle } from "drizzle-orm/d1";
import { eq, inArray } from "drizzle-orm";
import type { BatchItem } from "drizzle-orm/batch";
import { articles, moderationState } from "./db/schema.js";
import { UA } from "./wikipedia.js";
import type { Env } from "./env.js";

const API = "https://en.wikipedia.org/w/api.php";
export const TITLES_PER_BATCH = 50; // MediaWiki titles= cap for non-bot clients
// Free-plan subrequest safety: at most 8 MediaWiki fetches (~400 titles) per
// invocation; the KV cursor rotates so a 709-article corpus is fully swept in
// 2 daily runs, degrading gracefully as the corpus grows.
export const MAX_BATCHES_PER_RUN = 8;
const CURSOR_KEY = "drift:cursor"; // KV (RENDER_CACHE): last slug processed

export interface ArticleLite {
  slug: string;
  wikipediaTitle: string;
  revid: number | null;
  latestRevid: number | null;
}

// Picks this run's slice of the corpus: sort by slug, resume strictly after
// the cursor slug (which may since have been deleted — first greater slug is
// enough), wrap around at the end, cap at maxBatches × batchSize rows, and
// chunk into per-request batches. nextCursor = last slug taken.
export function planSweep(
  rows: ArticleLite[],
  cursor: string | null,
  maxBatches = MAX_BATCHES_PER_RUN,
  batchSize = TITLES_PER_BATCH,
): { batches: ArticleLite[][]; nextCursor: string | null } {
  if (rows.length === 0) return { batches: [], nextCursor: null };
  const sorted = [...rows].sort((a, b) => (a.slug < b.slug ? -1 : a.slug > b.slug ? 1 : 0));
  let start = 0;
  if (cursor !== null) {
    const i = sorted.findIndex((r) => r.slug > cursor);
    start = i === -1 ? 0 : i; // nothing after the cursor → wrap to the start
  }
  const take = Math.min(sorted.length, maxBatches * batchSize);
  const slice: ArticleLite[] = [];
  for (let k = 0; k < take; k++) slice.push(sorted[(start + k) % sorted.length]);
  const batches: ArticleLite[][] = [];
  for (let i = 0; i < slice.length; i += batchSize) batches.push(slice.slice(i, i + batchSize));
  return { batches, nextCursor: slice[slice.length - 1].slug };
}

// action=query&prop=info returns lastrevid plus redirect/missing flags in one
// call. Deliberately NO `redirects` param: MediaWiki treats boolean params as
// true whenever present (even `redirects=0`), and following redirects would
// swap a moved page's info for its target's — masking exactly the
// moved/deleted signal this cron exists to capture.
export function infoQueryUrl(titles: string[]): string {
  const params = new URLSearchParams({
    action: "query",
    prop: "info",
    titles: titles.join("|"),
    format: "json",
    formatversion: "2",
  });
  return `${API}?${params.toString()}`;
}

export interface InfoPage {
  title: string;
  lastrevid?: number;
  missing?: boolean;
  invalid?: boolean;
  redirect?: boolean;
}

export interface InfoQueryResponse {
  query?: {
    normalized?: Array<{ from: string; to: string }>;
    pages?: InfoPage[];
  };
}

export type DriftOutcome = "drifted" | "unchanged" | "missing" | "moved";

export interface DriftResult {
  slug: string;
  outcome: DriftOutcome;
  lastrevid: number | null;
}

// Maps an info-query response back to the batch's slugs. MediaWiki normalizes
// requested titles (underscores → spaces, first-letter case, ...) and reports
// the mapping in the `normalized` array; response pages carry the NORMALIZED
// title, so walk it backwards: page.title → requested title → slug. Anything
// unmatchable in either direction (response title with no slug, batch slug
// with no response page) lands in `unmatched` for the log line.
export function classifyBatch(
  batch: ArticleLite[],
  resp: InfoQueryResponse,
): { results: DriftResult[]; unmatched: string[] } {
  const titleToSlug = new Map<string, string>();
  const pinnedBySlug = new Map<string, number | null>();
  for (const a of batch) {
    titleToSlug.set(a.wikipediaTitle, a.slug);
    pinnedBySlug.set(a.slug, a.revid);
  }
  const denormalize = new Map<string, string>();
  for (const n of resp.query?.normalized ?? []) denormalize.set(n.to, n.from);

  const results: DriftResult[] = [];
  const unmatched: string[] = [];
  const seen = new Set<string>();
  for (const page of resp.query?.pages ?? []) {
    const requested = denormalize.get(page.title) ?? page.title;
    const slug = titleToSlug.get(requested);
    if (slug === undefined) {
      unmatched.push(page.title);
      continue;
    }
    seen.add(slug);
    const lastrevid = typeof page.lastrevid === "number" ? page.lastrevid : null;
    let outcome: DriftOutcome;
    if (page.missing || page.invalid) outcome = "missing";
    else if (page.redirect) outcome = "moved";
    else {
      const pinned = pinnedBySlug.get(slug) ?? null;
      // A never-pinned article (revid null) can't "drift"; its lastrevid is
      // still recorded as latest_revid by the write pass below.
      outcome = lastrevid !== null && pinned !== null && lastrevid > pinned ? "drifted" : "unchanged";
    }
    results.push({ slug, outcome, lastrevid });
  }
  for (const a of batch) if (!seen.has(a.slug)) unmatched.push(a.slug);
  return { results, unmatched };
}

// Cron entrypoint (wrangler.jsonc triggers.crons; wired into the Worker's
// default export by index.ts).
export async function scheduled(
  _controller: ScheduledController,
  env: Env,
  _ctx: ExecutionContext,
): Promise<void> {
  const db = drizzle(env.DB);
  const rows: ArticleLite[] = await db
    .select({
      slug: articles.slug,
      wikipediaTitle: articles.wikipediaTitle,
      revid: articles.revid,
      latestRevid: articles.latestRevid,
    })
    .from(articles);

  const cursor = await env.RENDER_CACHE.get(CURSOR_KEY);
  const { batches } = planSweep(rows, cursor);

  const results: DriftResult[] = [];
  const unmatched: string[] = [];
  let lastProcessed: string | null = null;
  for (const batch of batches) {
    try {
      const resp = await fetch(infoQueryUrl(batch.map((a) => a.wikipediaTitle)), {
        headers: { "User-Agent": UA },
      });
      if (!resp.ok) throw new Error(`MediaWiki HTTP ${resp.status}`);
      const data = (await resp.json()) as InfoQueryResponse;
      const c = classifyBatch(batch, data);
      results.push(...c.results);
      unmatched.push(...c.unmatched);
      lastProcessed = batch[batch.length - 1].slug;
    } catch (err) {
      // Stop the sweep but still persist what this run learned; the cursor
      // only advances past fully-processed batches, so the next run retries
      // the failed batch.
      console.log(JSON.stringify({ event: "drift_error", msg: String(err), cursor: lastProcessed }));
      break;
    }
  }

  const now = Date.now();
  const pinnedBySlug = new Map(rows.map((r) => [r.slug, r.revid] as const));
  const stmts: BatchItem<"sqlite">[] = [];
  const unchangedSlugs: string[] = [];
  let drifted = 0;
  let moved = 0;
  let deleted = 0;
  for (const r of results) {
    if (r.outcome === "drifted") {
      drifted++;
      stmts.push(
        // NEVER touches `version` — see the cache invariant at the top.
        db
          .update(articles)
          .set({ latestRevid: r.lastrevid, lastUpstreamCheck: now })
          .where(eq(articles.slug, r.slug)),
        db
          .insert(moderationState)
          .values({ slug: r.slug, wpDrifted: true, updatedAt: now })
          .onConflictDoUpdate({
            target: moderationState.slug,
            set: { wpDrifted: true, updatedAt: now },
          }),
      );
    } else if (r.outcome === "missing" || r.outcome === "moved") {
      const state = r.outcome === "missing" ? "deleted" : "moved";
      if (r.outcome === "missing") deleted++;
      else moved++;
      stmts.push(
        db.update(articles).set({ lastUpstreamCheck: now }).where(eq(articles.slug, r.slug)),
        db
          .insert(moderationState)
          .values({ slug: r.slug, state, updatedAt: now })
          .onConflictDoUpdate({ target: moderationState.slug, set: { state, updatedAt: now } }),
      );
    } else if (r.lastrevid !== null && pinnedBySlug.get(r.slug) === null) {
      // Never-pinned article: record the observed head revid (still not a
      // "drift" — there is no pinned revid to have drifted from).
      stmts.push(
        db
          .update(articles)
          .set({ latestRevid: r.lastrevid, lastUpstreamCheck: now })
          .where(eq(articles.slug, r.slug)),
      );
    } else {
      unchangedSlugs.push(r.slug);
    }
  }
  // Unchanged rows share one timestamp → group into IN-list updates (D1 caps
  // bound parameters at 100 per statement; 50 stays well under).
  for (let i = 0; i < unchangedSlugs.length; i += TITLES_PER_BATCH) {
    stmts.push(
      db
        .update(articles)
        .set({ lastUpstreamCheck: now })
        .where(inArray(articles.slug, unchangedSlugs.slice(i, i + TITLES_PER_BATCH))),
    );
  }
  // db.batch sends each chunk as ONE request to D1, keeping the cron far
  // under the free-plan subrequest cap even when every page drifted.
  for (let i = 0; i < stmts.length; i += 100) {
    const chunk = stmts.slice(i, i + 100);
    await db.batch(chunk as [BatchItem<"sqlite">, ...BatchItem<"sqlite">[]]);
  }

  if (lastProcessed !== null) await env.RENDER_CACHE.put(CURSOR_KEY, lastProcessed);
  console.log(
    JSON.stringify({
      event: "drift",
      checked: results.length,
      drifted,
      moved,
      deleted,
      cursor: lastProcessed,
      ...(unmatched.length > 0 ? { unmatched } : {}),
    }),
  );
}
