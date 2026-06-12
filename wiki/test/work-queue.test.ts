// GET /api/work — the moderation pipeline's priority queue (binding decision:
// ONE ORDER BY implements flagged > drifted > human-edited-since-review >
// never-reviewed > oldest-reviewed). api.test.ts pins the basic ladder; this
// suite goes deeper: tie-breaks, the limit clamp, the wp-update filter's
// NULL edges, and the two full lifecycle loops driven entirely through the
// API (review cycle, flag→resolve cycle).
//
// F2: the flagged tier is recency-aware — it is driven by real OPEN flag rows
// created after the last review, not by moderation_state.flag_count (which
// remains the patrol-pressure counter) — so the fixtures below seed actual
// flags rows alongside the counter.

import { describe, it, expect } from "vitest";
import {
  setup,
  get,
  post,
  save,
  botSave,
  insertArticle,
  insertModState,
  flagRows,
  moderationRow,
  revisionCount,
  blockNetwork,
  echo,
  SLUG,
  PIPELINE_TOKEN,
  type Harness,
} from "./helpers/harness.js";

blockNetwork();

interface Job {
  slug: string;
  version: number;
  revid: number | null;
  latest_revid: number | null;
  last_reviewed_at: number | null;
  last_reviewed_version: number | null;
  reason: string;
}

async function work(h: Harness, qs = ""): Promise<Job[]> {
  const res = await get(h.env, `/api/work${qs}`, { bearer: PIPELINE_TOKEN });
  expect(res.status).toBe(200);
  return ((await res.json()) as { jobs: Job[] }).jobs;
}

// F2: seed a real flags row (the queue tier reads flags, not flag_count).
function insertFlag(h: Harness, slug: string, createdAt: number, status = "open"): void {
  h.db
    .prepare("INSERT INTO flags (slug, annotation_id, reason, status, created_at) VALUES (?,NULL,'other',?,?)")
    .run(slug, status, createdAt);
}

// F4/F11: park/unpark an article's update-flow state directly (the cron and
// stage-0 are the production writers).
function setState(h: Harness, slug: string, state: string | null): void {
  h.db.prepare("UPDATE moderation_state SET state = ? WHERE slug = ?").run(state, slug);
}

describe("priority ladder", () => {
  it("every tier in one queue: flagged > drifted > human-edited > never-reviewed > stale-review", async () => {
    const h = setup();
    // Reviewed timestamps deliberately anti-correlated with priority so the
    // test fails if ORDER BY degrades to recency/rowid.
    insertArticle(h.db, "T_Flagged");
    insertModState(h.db, "T_Flagged", { flagCount: 1, lastReviewedAt: 9000, lastReviewedVersion: 1 });
    insertFlag(h, "T_Flagged", 9500); // F2: open flag NEWER than the review
    insertArticle(h.db, "T_Drifted");
    insertModState(h.db, "T_Drifted", { wpDrifted: 1, lastReviewedAt: 8000, lastReviewedVersion: 1 });
    insertArticle(h.db, "T_HumanEdited", { version: 5 });
    insertModState(h.db, "T_HumanEdited", { lastReviewedAt: 7000, lastReviewedVersion: 4 });
    insertArticle(h.db, "T_Stale");
    insertModState(h.db, "T_Stale", { lastReviewedAt: 1000, lastReviewedVersion: 1 });
    // Seed article (Test_Article) has no moderation row → never-reviewed.

    const jobs = await work(h);
    expect(jobs.map((j) => [j.slug, j.reason])).toEqual([
      ["T_Flagged", "flagged"],
      ["T_Drifted", "drifted"],
      ["T_HumanEdited", "human-edited"],
      [SLUG, "never-reviewed"],
      ["T_Stale", "stale-review"],
    ]);
  });

  it("a flagged article that is also drifted and human-edited reports reason 'flagged' (first rule wins)", async () => {
    const h = setup();
    insertArticle(h.db, "T_Everything", { version: 9, revid: 100, latestRevid: 200 });
    insertModState(h.db, "T_Everything", { flagCount: 1, wpDrifted: 1, lastReviewedAt: 1, lastReviewedVersion: 2 });
    insertFlag(h, "T_Everything", 100); // F2: newer than last_reviewed_at 1
    const jobs = await work(h, "?limit=1");
    expect(jobs[0]).toMatchObject({ slug: "T_Everything", reason: "flagged" });
  });

  it("ties: more recent open flags first; equal counts break on wp_drifted, then human-edited, then oldest review", async () => {
    // F2: tie-breaking within the flagged tier now counts open flags created
    // after the last review (COUNT, not the flag_count column).
    const h = setup();
    insertArticle(h.db, "T_Flag3");
    insertModState(h.db, "T_Flag3", { flagCount: 3, lastReviewedAt: 1000, lastReviewedVersion: 1 });
    for (let i = 0; i < 3; i++) insertFlag(h, "T_Flag3", 1500);
    insertArticle(h.db, "T_Flag1_Drifted");
    insertModState(h.db, "T_Flag1_Drifted", { flagCount: 1, wpDrifted: 1, lastReviewedAt: 1000, lastReviewedVersion: 1 });
    insertFlag(h, "T_Flag1_Drifted", 1500);
    insertArticle(h.db, "T_Flag1_HumanEdited", { version: 2 });
    insertModState(h.db, "T_Flag1_HumanEdited", { flagCount: 1, lastReviewedAt: 1000, lastReviewedVersion: 1 });
    insertFlag(h, "T_Flag1_HumanEdited", 1500);
    insertArticle(h.db, "T_Flag1_Old");
    insertModState(h.db, "T_Flag1_Old", { flagCount: 1, lastReviewedAt: 500, lastReviewedVersion: 1 });
    insertFlag(h, "T_Flag1_Old", 1500);
    insertArticle(h.db, "T_Flag1_New");
    insertModState(h.db, "T_Flag1_New", { flagCount: 1, lastReviewedAt: 2000, lastReviewedVersion: 1 });
    insertFlag(h, "T_Flag1_New", 2500);

    const jobs = await work(h);
    expect(jobs.map((j) => j.slug)).toEqual([
      "T_Flag3", // 3 recent open flags beat every 1-flag article
      "T_Flag1_Drifted", // tie on count → wp_drifted DESC
      "T_Flag1_HumanEdited", // then human-edited DESC
      "T_Flag1_Old", // then last_reviewed_at ASC (oldest first)
      "T_Flag1_New",
      SLUG, // unflagged seed article trails the whole flagged cohort
    ]);
  });

  it("never-reviewed (NULL last_reviewed_at) sorts before every reviewed article in the bottom tier", async () => {
    const h = setup();
    insertArticle(h.db, "T_ReviewedAncient");
    insertModState(h.db, "T_ReviewedAncient", { lastReviewedAt: 1, lastReviewedVersion: 1 });
    const jobs = await work(h);
    // The seed article has no moderation row at all → NULLS FIRST.
    expect(jobs.map((j) => [j.slug, j.reason])).toEqual([
      [SLUG, "never-reviewed"],
      ["T_ReviewedAncient", "stale-review"],
    ]);
  });
});

describe("limit clamp", () => {
  function seedMany(h: Harness, n: number): void {
    // Zero-padded so the never-reviewed cohort has no surprising order deps.
    for (let i = 0; i < n; i++) insertArticle(h.db, `Bulk_${String(i).padStart(3, "0")}`);
  }

  it("defaults to 50; caps at 100; floors at 1; non-numeric falls back to 50", async () => {
    const h = setup();
    seedMany(h, 120); // + the seed article = 121 candidates
    expect((await work(h)).length).toBe(50);
    expect((await work(h, "?limit=100")).length).toBe(100);
    expect((await work(h, "?limit=999")).length).toBe(100); // cap
    expect((await work(h, "?limit=0")).length).toBe(1); // floor
    expect((await work(h, "?limit=-5")).length).toBe(1);
    expect((await work(h, "?limit=abc")).length).toBe(50); // NaN → default
    expect((await work(h, "?limit=2")).length).toBe(2);
  });

  it("limit truncates from the back: the top of the ladder always survives", async () => {
    const h = setup();
    seedMany(h, 5);
    insertArticle(h.db, "T_Flagged");
    insertModState(h.db, "T_Flagged", { flagCount: 1 });
    insertFlag(h, "T_Flagged", 1000); // F2: never reviewed → any open flag is recent
    insertArticle(h.db, "T_Drifted");
    insertModState(h.db, "T_Drifted", { wpDrifted: 1 });
    const jobs = await work(h, "?limit=2");
    expect(jobs.map((j) => j.slug)).toEqual(["T_Flagged", "T_Drifted"]);
  });
});

describe("mode=wp-update filter", () => {
  it("selects wp_drifted OR latest_revid > revid; NULL revids and up-to-date pins are excluded", async () => {
    const h = setup();
    insertArticle(h.db, "U_FlaggedDrift", { revid: 100, latestRevid: 100 });
    insertModState(h.db, "U_FlaggedDrift", { wpDrifted: 1 }); // included via the flag column
    insertArticle(h.db, "U_Trailing", { revid: 100, latestRevid: 200 }); // included via revid comparison
    insertArticle(h.db, "U_UpToDate", { revid: 100, latestRevid: 100 }); // excluded
    insertArticle(h.db, "U_NeverPinned", { revid: null, latestRevid: 500 }); // excluded: NULL revid comparison is not true
    insertArticle(h.db, "U_NoUpstream", { revid: 100, latestRevid: null }); // excluded
    // Seed article: revid set, latest_revid null → excluded too.

    const jobs = await work(h, "?mode=wp-update");
    expect(jobs.map((j) => j.slug).sort()).toEqual(["U_FlaggedDrift", "U_Trailing"]);
    expect(jobs.every((j) => j.reason === "drifted")).toBe(true);
  });

  it("within wp-update, a flagged drifting article still sorts first", async () => {
    const h = setup();
    insertArticle(h.db, "U_Plain", { revid: 100, latestRevid: 200 });
    insertArticle(h.db, "U_AlsoFlagged", { revid: 100, latestRevid: 200 });
    insertModState(h.db, "U_AlsoFlagged", { flagCount: 2 });
    insertFlag(h, "U_AlsoFlagged", 1000); // F2: real open flags drive the tier
    insertFlag(h, "U_AlsoFlagged", 1000);
    const jobs = await work(h, "?mode=wp-update");
    expect(jobs.map((j) => j.slug)).toEqual(["U_AlsoFlagged", "U_Plain"]);
    // reason still reports the selecting rule for THIS queue position.
    expect(jobs[0].reason).toBe("flagged");
  });
});

describe("full review cycle through the API", () => {
  it("bot review stamps moderation_state and the article leaves the queue front; a human edit brings it back", async () => {
    const h = setup();
    insertArticle(h.db, "Cycle_Competitor");
    insertModState(h.db, "Cycle_Competitor", { lastReviewedAt: 1000, lastReviewedVersion: 1 });

    // 1. Never-reviewed seed article heads the queue.
    let jobs = await work(h);
    expect(jobs[0]).toMatchObject({ slug: SLUG, reason: "never-reviewed" });

    // 2. The pipeline does what moderate.py does: read, echo, push.
    //    TESTAGENT#2: a verbatim echo is a NO-OP save — the version no longer
    //    bumps and no revision is written, but the bot's moderation_state
    //    stamp still lands (that's the no-op exception).
    const read = (await (await get(h.env, `/api/article/${SLUG}.json`)).json()) as {
      version: number;
      annotations: Array<Record<string, unknown>>;
    };
    const echoRes = await botSave(h.env, { annotations: echo(read.annotations), base_version: read.version });
    expect(echoRes.status).toBe(200);
    expect(await echoRes.json()).toEqual({ ok: true, noop: true, version: 1 });

    // 3. Reviewed-just-now → the article drops behind the older review.
    jobs = await work(h);
    expect(jobs.map((j) => [j.slug, j.reason])).toEqual([
      ["Cycle_Competitor", "stale-review"],
      [SLUG, "stale-review"],
    ]);
    expect(jobs[1].last_reviewed_version).toBe(1); // TESTAGENT#2: no-op kept version 1

    // 4. A human session edit bumps version past last_reviewed_version →
    //    the article jumps the queue with reason human-edited.
    const cur = (await (await get(h.env, `/api/article/${SLUG}.json`)).json()) as {
      version: number;
      annotations: Array<Record<string, unknown>>;
    };
    const edited = echo(cur.annotations);
    edited[0] = { ...edited[0], note: "human touch-up" };
    expect((await save(h.env, { annotations: edited, base_version: cur.version }, { user: "u-human" })).status).toBe(200);

    jobs = await work(h);
    expect(jobs[0]).toMatchObject({ slug: SLUG, reason: "human-edited", version: 2, last_reviewed_version: 1 });

    // 5. The bot re-reviews (another no-op echo) → back to the rear again.
    const reread = (await (await get(h.env, `/api/article/${SLUG}.json`)).json()) as {
      version: number;
      annotations: Array<Record<string, unknown>>;
    };
    expect(
      (await botSave(h.env, { annotations: echo(reread.annotations), base_version: reread.version })).status,
    ).toBe(200);
    jobs = await work(h);
    expect(jobs[0].slug).toBe("Cycle_Competitor");
    expect(jobs[1]).toMatchObject({ slug: SLUG, reason: "stale-review", last_reviewed_version: 2 });
  });

  it("TESTAGENT#2: a bot no-op echo stamps moderation_state but writes nothing else; session no-ops skip the stamp", async () => {
    const h = setup();
    const read = (await (await get(h.env, `/api/article/${SLUG}.json`)).json()) as {
      version: number;
      annotations: Array<Record<string, unknown>>;
    };

    // Session no-op: 200 {noop}, NO moderation row, no version/revision churn.
    const sess = await save(h.env, { annotations: echo(read.annotations), base_version: read.version }, { user: "u-human" });
    expect(sess.status).toBe(200);
    expect(await sess.json()).toEqual({ ok: true, noop: true, version: 1 });
    expect(moderationRow(h.db)).toBeUndefined();
    expect(revisionCount(h.db)).toBe(1);

    // Bot no-op: 200 {noop} + the review stamp (last_reviewed_at/version).
    const bot = await botSave(h.env, { annotations: echo(read.annotations), base_version: read.version });
    expect(bot.status).toBe(200);
    expect(await bot.json()).toEqual({ ok: true, noop: true, version: 1 });
    const mod = moderationRow(h.db)!;
    expect(mod.last_reviewed_version).toBe(1);
    expect(mod.last_reviewed_at).toBeGreaterThan(0);
    expect(revisionCount(h.db)).toBe(1); // still only the seed revision
  });

  it("a bot re-pin to latest_revid clears the drifted tier through the API", async () => {
    const h = setup();
    const NEW_REVID = 67890; // pre-seeded in WP_HTML by the harness
    h.db.prepare("UPDATE articles SET latest_revid = ? WHERE slug = ?").run(NEW_REVID, SLUG);
    insertModState(h.db, SLUG, { wpDrifted: 1, lastReviewedAt: 1000, lastReviewedVersion: 1 });
    insertArticle(h.db, "Cycle_Other");
    insertModState(h.db, "Cycle_Other", { lastReviewedAt: 2000, lastReviewedVersion: 1 });

    let jobs = await work(h, "?mode=wp-update");
    expect(jobs.map((j) => j.slug)).toEqual([SLUG]);

    const read = (await (await get(h.env, `/api/article/${SLUG}.json`)).json()) as {
      version: number;
      annotations: Array<Record<string, unknown>>;
    };
    expect(
      (
        await botSave(h.env, {
          annotations: echo(read.annotations),
          base_version: read.version,
          revid: NEW_REVID,
        })
      ).status,
    ).toBe(200);

    // Drift cleared: the wp-update queue empties; review mode ranks it last.
    jobs = await work(h, "?mode=wp-update");
    expect(jobs).toEqual([]);
    jobs = await work(h);
    expect(jobs.map((j) => j.slug)).toEqual(["Cycle_Other", SLUG]);
    expect(jobs[1].revid).toBe(NEW_REVID);
  });
});

describe("flag → resolve cycle through the API", () => {
  it("a reader flag puts the article at the queue front; resolving drops it back down", async () => {
    const h = setup();
    insertArticle(h.db, "Flag_Competitor");
    insertModState(h.db, "Flag_Competitor", { wpDrifted: 1, lastReviewedAt: 1000, lastReviewedVersion: 1 });

    // Anonymous reader reports a wrong decl.
    expect(
      (await post(h.env, `/api/flag/${SLUG}`, { annotation_id: "aaaaaaaaaaaa", reason: "wrong_decl" }, { ip: "203.0.113.9" }))
        .status,
    ).toBe(200);
    expect(moderationRow(h.db)!.flag_count).toBe(1);

    let jobs = await work(h);
    expect(jobs.map((j) => [j.slug, j.reason])).toEqual([
      [SLUG, "flagged"],
      ["Flag_Competitor", "drifted"],
    ]);

    // A patroller resolves it → flag_count decrements → priority drops.
    const flagId = flagRows(h.db)[0].id;
    expect(
      (await post(h.env, `/api/flag/${flagId}/resolve`, { resolution: "fixed" }, { user: "u-patroller" })).status,
    ).toBe(200);
    expect(moderationRow(h.db)!.flag_count).toBe(0);

    jobs = await work(h);
    expect(jobs.map((j) => [j.slug, j.reason])).toEqual([
      ["Flag_Competitor", "drifted"],
      // The flag created the moderation row with NULL last_reviewed_at →
      // the article is back to plain never-reviewed.
      [SLUG, "never-reviewed"],
    ]);
  });

  it("two open flags need two resolutions before the article leaves the flagged tier", async () => {
    const h = setup();
    insertArticle(h.db, "Flag_Competitor2");
    insertModState(h.db, "Flag_Competitor2", { wpDrifted: 1 });
    expect((await post(h.env, `/api/flag/${SLUG}`, { reason: "other" }, { ip: "203.0.113.9" })).status).toBe(200);
    expect(
      (await post(h.env, `/api/flag/${SLUG}`, { annotation_id: "bbbbbbbbbbbb", reason: "wrong_status" }, { ip: "203.0.113.9" }))
        .status,
    ).toBe(200);
    expect(moderationRow(h.db)!.flag_count).toBe(2);

    const [f1, f2] = flagRows(h.db);
    expect((await post(h.env, `/api/flag/${f1.id}/resolve`, { resolution: "dismissed" }, { user: "u-admin" })).status).toBe(200);
    // Still flagged (one open flag left) → still first.
    let jobs = await work(h, "?limit=1");
    expect(jobs[0]).toMatchObject({ slug: SLUG, reason: "flagged" });

    expect((await post(h.env, `/api/flag/${f2.id}/resolve`, { resolution: "fixed" }, { user: "u-admin" })).status).toBe(200);
    jobs = await work(h, "?limit=1");
    expect(jobs[0].slug).toBe("Flag_Competitor2");
  });
});

describe("recency-aware flagged tier (F2)", () => {
  it("a bot review releases the flagged tier even while the flag stays open (no livelock)", async () => {
    const h = setup();
    insertArticle(h.db, "F2_Competitor");
    insertModState(h.db, "F2_Competitor", { lastReviewedAt: 1000, lastReviewedVersion: 1 });

    // Reader flags the seed article → it heads the queue.
    expect((await post(h.env, `/api/flag/${SLUG}`, { reason: "other" }, { ip: "203.0.113.9" })).status).toBe(200);
    let jobs = await work(h);
    expect(jobs[0]).toMatchObject({ slug: SLUG, reason: "flagged" });

    // The bot reviews it (verbatim echo). The flag REMAINS OPEN — only a
    // patroller resolves flags — but the review is now at least as new as the
    // flag, so the article leaves the flagged tier instead of livelocking.
    const read = (await (await get(h.env, `/api/article/${SLUG}.json`)).json()) as {
      version: number;
      annotations: Array<Record<string, unknown>>;
    };
    expect((await botSave(h.env, { annotations: echo(read.annotations), base_version: read.version })).status).toBe(200);
    expect(flagRows(h.db)[0].status).toBe("open"); // untouched
    expect(moderationRow(h.db)!.flag_count).toBe(1); // patrol pressure stands

    jobs = await work(h);
    expect(jobs.map((j) => [j.slug, j.reason])).toEqual([
      ["F2_Competitor", "stale-review"],
      [SLUG, "stale-review"],
    ]);
  });

  it("a flag created AFTER the last review re-enters the flagged tier", async () => {
    const h = setup();
    insertModState(h.db, SLUG, { lastReviewedAt: 5000, lastReviewedVersion: 1, flagCount: 1 });
    insertFlag(h, SLUG, 4000); // open, but OLDER than the review → not flagged
    let jobs = await work(h);
    expect(jobs[0]).toMatchObject({ slug: SLUG, reason: "stale-review" });

    insertFlag(h, SLUG, 6000); // newer than the review → flagged again
    jobs = await work(h);
    expect(jobs[0]).toMatchObject({ slug: SLUG, reason: "flagged" });
  });
});

describe("moved/deleted/needs_human exclusion (F4/F11)", () => {
  it("parked articles drop out of BOTH modes; clearing the state readmits them", async () => {
    const h = setup();
    // All three would otherwise qualify for wp-update (latest_revid > revid)
    // and for review (never reviewed).
    for (const [slug, state] of [
      ["Park_Moved", "moved"],
      ["Park_Deleted", "deleted"],
      ["Park_NeedsHuman", "needs_human"],
    ] as const) {
      insertArticle(h.db, slug, { revid: 100, latestRevid: 200 });
      insertModState(h.db, slug, { wpDrifted: 1 });
      setState(h, slug, state);
    }
    insertArticle(h.db, "Park_Control", { revid: 100, latestRevid: 200 });
    insertModState(h.db, "Park_Control", { wpDrifted: 1 });

    // review mode: only the un-parked articles appear.
    let jobs = await work(h);
    expect(jobs.map((j) => j.slug).sort()).toEqual(["Park_Control", SLUG]);

    // wp-update mode (F11: notably needs_human, or stage-0 re-probes wedged
    // articles forever): only the control article appears.
    jobs = await work(h, "?mode=wp-update");
    expect(jobs.map((j) => j.slug)).toEqual(["Park_Control"]);

    // The drift cron clearing the state (page back to normal) readmits it.
    setState(h, "Park_Moved", null);
    jobs = await work(h, "?mode=wp-update");
    expect(jobs.map((j) => j.slug).sort()).toEqual(["Park_Control", "Park_Moved"]);
  });

  it("a parked article stays excluded even when flagged (parking wins)", async () => {
    const h = setup();
    insertArticle(h.db, "Park_Flagged");
    insertModState(h.db, "Park_Flagged", {});
    setState(h, "Park_Flagged", "needs_human");
    insertFlag(h, "Park_Flagged", 1000);
    const jobs = await work(h);
    expect(jobs.map((j) => j.slug)).toEqual([SLUG]);
  });
});
