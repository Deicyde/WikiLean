// Queue rotation via per-mode attempt stamps on moderation_state (migration
// 0014). POST /api/runs `attempted` stamps the slugs a run worked on (review →
// last_review_attempt_at, wp-update → last_update_attempt_at, all → both;
// first report only, atomically with the run row; scheduling metadata, never
// articles.version). GET /api/work's last tier orders by the later of
// last_reviewed_at and the mode's own stamp, never-touched first. Without it,
// a review that fails before saving, or a stage-0 re-pin whose anchors fail,
// never stamps anything and the same articles head the queue every night.

import { describe, it, expect } from "vitest";
import {
  setup,
  get,
  post,
  insertArticle,
  insertModState,
  blockNetwork,
  PIPELINE_TOKEN,
  type Harness,
} from "./helpers/harness.js";

blockNetwork();

const DAY = 24 * 3600 * 1000;

interface Job {
  slug: string;
  last_reviewed_at: number | null;
  last_attempted_at: number | null;
  reason: string;
}

async function work(h: Harness, qs = ""): Promise<Job[]> {
  const res = await get(h.env, `/api/work${qs}`, { bearer: PIPELINE_TOKEN });
  expect(res.status).toBe(200);
  return ((await res.json()) as { jobs: Job[] }).jobs;
}

let runSeq = 0;
function reportRun(h: Harness, extra: Record<string, unknown>, runId?: string): Promise<Response> {
  const id = runId ?? (0xa0000000 + runSeq++).toString(16);
  return post(
    h.env,
    "/api/runs",
    { run_id: id, kind: "review", started_at: 1, finished_at: 2, articles_processed: 0, errors: 0, tokens: 0, ...extra },
    { bearer: PIPELINE_TOKEN, origin: null },
  );
}

function attemptedAt(h: Harness, slug: string, column = "last_review_attempt_at"): number | null {
  const r = h.db.prepare(`SELECT ${column} AS v FROM moderation_state WHERE slug = ?`).get(slug) as
    | { v: number | null }
    | undefined;
  return r?.v ?? null;
}

function versions(h: Harness): Record<string, number> {
  const rows = h.db.prepare("SELECT slug, version FROM articles ORDER BY slug").all() as Array<{ slug: string; version: number }>;
  return Object.fromEntries(rows.map((r) => [r.slug, r.version]));
}

// Three reviewed articles, oldest review first, plus the never-reviewed
// harness seed article.
function seedReviewed(h: Harness): void {
  const now = Date.now();
  insertArticle(h.db, "Old_A");
  insertModState(h.db, "Old_A", { lastReviewedAt: now - 30 * DAY });
  insertArticle(h.db, "Mid_B");
  insertModState(h.db, "Mid_B", { lastReviewedAt: now - 20 * DAY });
  insertArticle(h.db, "New_C");
  insertModState(h.db, "New_C", { lastReviewedAt: now - 10 * DAY });
}

describe("POST /api/runs attempted", () => {
  it("stamps the review attempt for known slugs, creating moderation rows, without touching versions", async () => {
    const h = setup();
    seedReviewed(h);
    const before = versions(h);
    const t0 = Date.now();
    const res = await reportRun(h, { attempted: ["Old_A", "Test_Article", "No_Such_Article", "Old_A"] });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true, stamped: 2 }); // deduped; unknown slug ignored
    expect(attemptedAt(h, "Old_A")).toBeGreaterThanOrEqual(t0);
    expect(attemptedAt(h, "Test_Article")).toBeGreaterThanOrEqual(t0); // row created
    expect(attemptedAt(h, "Mid_B")).toBeNull();
    // Reviews are not faked: last_reviewed_at is untouched by an attempt.
    const r = h.db.prepare("SELECT last_reviewed_at FROM moderation_state WHERE slug = 'Test_Article'").get() as {
      last_reviewed_at: number | null;
    };
    expect(r.last_reviewed_at).toBeNull();
    expect(versions(h)).toEqual(before);
    expect(h.db.prepare("SELECT COUNT(*) AS n FROM moderation_state WHERE slug = 'No_Such_Article'").get()).toEqual({ n: 0 });
  });

  it("a duplicate report does not re-stamp", async () => {
    const h = setup();
    seedReviewed(h);
    expect((await reportRun(h, { attempted: ["Old_A"] }, "0badf00d")).status).toBe(200);
    h.db.prepare("UPDATE moderation_state SET last_review_attempt_at = 1 WHERE slug = 'Old_A'").run();
    const dup = await reportRun(h, { attempted: ["Old_A", "Mid_B"] }, "0badf00d");
    expect(await dup.json()).toEqual({ ok: true, duplicate: true });
    expect(attemptedAt(h, "Old_A")).toBe(1);
    expect(attemptedAt(h, "Mid_B")).toBeNull();
  });

  it("400s on malformed attempted (nothing stored); omitted/null keeps the old response", async () => {
    const h = setup();
    seedReviewed(h);
    const bad: unknown[] = ["Old_A", [1], [""], ["a/b"], ["x\u0000y"], ["x".repeat(301)], { slug: "Old_A" }];
    for (const attempted of bad) {
      const res = await reportRun(h, { attempted });
      expect(res.status, JSON.stringify(attempted).slice(0, 40)).toBe(400);
    }
    expect(h.db.prepare("SELECT COUNT(*) AS n FROM pipeline_runs").get()).toEqual({ n: 0 });
    expect(await (await reportRun(h, {})).json()).toEqual({ ok: true });
    expect(await (await reportRun(h, { attempted: null })).json()).toEqual({ ok: true });
  });

  it("stamps in chunks (D1's 100-parameter cap) and never loses the run to an oversized list", async () => {
    const h = setup();
    for (let i = 0; i < 120; i++) insertArticle(h.db, `Bulk_${i}`);
    const many = Array.from({ length: 120 }, (_, i) => `Bulk_${i}`);
    expect(await (await reportRun(h, { attempted: many })).json()).toEqual({ ok: true, stamped: 120 });
    expect(attemptedAt(h, "Bulk_119")).not.toBeNull();
    // Over the cap: the run row is still stored, nothing is stamped.
    const tooMany = Array.from({ length: 301 }, (_, i) => `X${i}`);
    const res = await reportRun(h, { attempted: tooMany }, "0000feed");
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true, stamped: 0, attempted_error: "more than 300 attempted slugs; none stamped" });
    expect(h.db.prepare("SELECT COUNT(*) AS n FROM pipeline_runs WHERE run_id = '0000feed'").get()).toEqual({ n: 1 });
  });

  it("stamps the column for the run's kind: review, wp-update, all (both), new (none)", async () => {
    const h = setup();
    seedReviewed(h);
    expect((await reportRun(h, { kind: "review", attempted: ["Old_A"] })).status).toBe(200);
    expect((await reportRun(h, { kind: "wp-update", attempted: ["Mid_B"] })).status).toBe(200);
    expect((await reportRun(h, { kind: "all", attempted: ["New_C"] })).status).toBe(200);
    expect(await (await reportRun(h, { kind: "new", attempted: ["Test_Article"] })).json()).toEqual({ ok: true, stamped: 0 });
    expect([attemptedAt(h, "Old_A"), attemptedAt(h, "Old_A", "last_update_attempt_at")].map((v) => v !== null)).toEqual([true, false]);
    expect([attemptedAt(h, "Mid_B"), attemptedAt(h, "Mid_B", "last_update_attempt_at")].map((v) => v !== null)).toEqual([false, true]);
    expect([attemptedAt(h, "New_C"), attemptedAt(h, "New_C", "last_update_attempt_at")].map((v) => v !== null)).toEqual([true, true]);
    expect(attemptedAt(h, "Test_Article")).toBeNull();
  });
});

describe("GET /api/work rotation", () => {
  it("an attempted article moves behind articles touched less recently (review mode)", async () => {
    const h = setup();
    seedReviewed(h);
    expect((await work(h)).map((j) => j.slug)).toEqual(["Test_Article", "Old_A", "Mid_B", "New_C"]);
    // The nightly attempted the two at the head and both failed (no save).
    expect((await reportRun(h, { attempted: ["Test_Article", "Old_A"] })).status).toBe(200);
    const jobs = await work(h);
    // Same-run stamps tie; slug breaks the tie.
    expect(jobs.map((j) => j.slug)).toEqual(["Mid_B", "New_C", "Old_A", "Test_Article"]);
    expect(jobs[3].last_attempted_at).not.toBeNull();
    expect(jobs[3].reason).toBe("never-reviewed"); // reason still describes reviews
  });

  it("never-reviewed-and-never-attempted stays first; an old attempt counts like an old review", async () => {
    const h = setup();
    const now = Date.now();
    insertArticle(h.db, "Reviewed_Old");
    insertModState(h.db, "Reviewed_Old", { lastReviewedAt: now - 5 * DAY });
    insertArticle(h.db, "Attempted_Older");
    insertModState(h.db, "Attempted_Older");
    h.db.prepare("UPDATE moderation_state SET last_review_attempt_at = ? WHERE slug = 'Attempted_Older'").run(now - 9 * DAY);
    expect((await work(h)).map((j) => j.slug)).toEqual(["Test_Article", "Attempted_Older", "Reviewed_Old"]);
  });

  it("rotates the wp-update queue on its own stamp, leaving the review queue order alone", async () => {
    const h = setup();
    const now = Date.now();
    for (const [slug, age] of [["Drift_1", 50], ["Drift_2", 40], ["Drift_3", 30]] as const) {
      insertArticle(h.db, slug);
      insertModState(h.db, slug, { lastReviewedAt: now - age * DAY, wpDrifted: 1 });
    }
    expect((await work(h, "?mode=wp-update")).map((j) => j.slug)).toEqual(["Drift_1", "Drift_2", "Drift_3"]);
    expect((await reportRun(h, { kind: "wp-update", attempted: ["Drift_1", "Drift_2"] })).status).toBe(200);
    const update = await work(h, "?mode=wp-update");
    expect(update.map((j) => j.slug)).toEqual(["Drift_3", "Drift_1", "Drift_2"]);
    expect(update[1].last_attempted_at).not.toBeNull();
    // A failed re-pin (needs work) must still reach the review queue next.
    const review = await work(h);
    expect(review.map((j) => j.slug).slice(0, 3)).toEqual(["Drift_1", "Drift_2", "Drift_3"]);
    expect(review[0].last_attempted_at).toBeNull();
  });

  it("a review newer than the attempt stamp still counts (the later of the two orders)", async () => {
    const h = setup();
    const now = Date.now();
    insertArticle(h.db, "Reviewed_Recently");
    insertModState(h.db, "Reviewed_Recently", { lastReviewedAt: now - DAY });
    h.db.prepare("UPDATE moderation_state SET last_review_attempt_at = ? WHERE slug = 'Reviewed_Recently'").run(now - 20 * DAY);
    insertArticle(h.db, "Reviewed_Long_Ago");
    insertModState(h.db, "Reviewed_Long_Ago", { lastReviewedAt: now - 10 * DAY });
    expect((await work(h)).map((j) => j.slug)).toEqual(["Test_Article", "Reviewed_Long_Ago", "Reviewed_Recently"]);
  });

  it("an attempt stamp does not release an article from the flagged tier", async () => {
    const h = setup();
    const now = Date.now();
    insertArticle(h.db, "Flagged");
    insertModState(h.db, "Flagged", { lastReviewedAt: now - 2 * DAY });
    h.db.prepare("INSERT INTO flags (slug, annotation_id, reason, status, created_at) VALUES ('Flagged',NULL,'other','open',?)").run(now - DAY);
    expect((await reportRun(h, { attempted: ["Flagged"] })).status).toBe(200);
    const jobs = await work(h);
    expect(jobs[0]).toMatchObject({ slug: "Flagged", reason: "flagged" });
  });

  it("higher tiers still win over rotation (drifted before stale in review mode)", async () => {
    const h = setup();
    const now = Date.now();
    insertArticle(h.db, "Drifted");
    insertModState(h.db, "Drifted", { lastReviewedAt: now - DAY, wpDrifted: 1 });
    insertArticle(h.db, "Stale");
    insertModState(h.db, "Stale", { lastReviewedAt: now - 60 * DAY });
    expect((await reportRun(h, { attempted: ["Drifted"] })).status).toBe(200);
    expect((await work(h)).map((j) => j.slug).slice(0, 2)).toEqual(["Drifted", "Test_Article"]);
  });
});
