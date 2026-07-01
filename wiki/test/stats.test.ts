// /stats (P2a): the public experiment-instrumentation dashboard. Pins the
// SQL aggregates against seeded fixtures (review states, count columns,
// event_type × actor_type cells, flags, revisions/patrol, pipeline_runs),
// the RQ labels, the KV cache (page:stats:v1, TTL 300), and the
// zero-means-broken footnote.

import { describe, it, expect } from "vitest";
import {
  setup,
  post,
  get,
  insertArticle,
  insertModState,
  insertRevision,
  blockNetwork,
  PIPELINE_TOKEN,
  type Harness,
} from "./helpers/harness.js";

blockNetwork();

const DAY = 24 * 3600 * 1000;

function insertEvent(
  h: Harness,
  eventType: string,
  actorType: string,
  createdAt: number,
  fieldChanges: string | null = null,
): void {
  h.db
    .prepare(
      "INSERT INTO annotation_events (revision_id, slug, annotation_id, event_type, actor_type, user_id, field_changes, created_at) VALUES (1,'Test_Article','aaaaaaaaaaaa',?,?,?,?,?)",
    )
    .run(eventType, actorType, actorType === "human" ? "u-human" : "pipeline", fieldChanges, createdAt);
}

function setCounts(h: Harness, slug: string, f: number, p: number, n: number): void {
  h.db
    .prepare("UPDATE articles SET n_formalized=?, n_partial=?, n_not_formalized=? WHERE slug=?")
    .run(f, p, n, slug);
}

// One fully-populated fixture world. Layout (5 articles incl. the harness
// seed): Test_Article never-reviewed/counts-pending; Fresh_A fresh; Stale_B
// stale; Drift_C fresh+drifted; Parked_D fresh+parked.
async function seedWorld(h: Harness): Promise<void> {
  const now = Date.now();
  insertArticle(h.db, "Fresh_A");
  insertModState(h.db, "Fresh_A", { lastReviewedAt: now - 1000 });
  setCounts(h, "Fresh_A", 2, 1, 1);
  insertArticle(h.db, "Stale_B");
  insertModState(h.db, "Stale_B", { lastReviewedAt: now - 40 * DAY });
  setCounts(h, "Stale_B", 3, 0, 0);
  insertArticle(h.db, "Drift_C");
  insertModState(h.db, "Drift_C", { lastReviewedAt: now - 1000, wpDrifted: 1 });
  setCounts(h, "Drift_C", 0, 0, 0);
  insertArticle(h.db, "Parked_D");
  insertModState(h.db, "Parked_D", { lastReviewedAt: now - 1000 });
  h.db.prepare("UPDATE moderation_state SET state='moved' WHERE slug='Parked_D'").run();
  setCounts(h, "Parked_D", 0, 0, 0);

  // Events: modify×human (one recent, one >30d old), endorse×human,
  // reject×human, add×pipeline.
  insertEvent(h, "modify", "human", now - 1000, '{"label":["a","b"]}');
  insertEvent(h, "modify", "human", now - 40 * DAY, '{"label":["x","y"]}');
  insertEvent(h, "endorse", "human", now - 1000);
  insertEvent(h, "reject", "human", now - 1000);
  insertEvent(h, "add", "pipeline", now - 1000);

  // Flags: 2 open, 1 fixed.
  const insFlag = h.db.prepare(
    "INSERT INTO flags (slug, annotation_id, reason, status, created_at) VALUES ('Test_Article',NULL,'other',?,?)",
  );
  insFlag.run("open", now);
  insFlag.run("open", now);
  insFlag.run("fixed", now);

  // Revisions: the harness seed row is kind='edit' (unpatrolled). Add one
  // more edit (then patrol it) and one pipeline revision.
  const editId = insertRevision(h.db, "Test_Article", { userId: "u-human", kind: "edit" });
  insertRevision(h.db, "Test_Article", { userId: "pipeline", kind: "pipeline" });
  const patrolRes = await post(h.env, `/api/revision/${editId}/patrol`, {}, { user: "u-patroller" });
  expect(patrolRes.status).toBe(200);

  // One pipeline run via the real RUNS-API endpoint.
  const runRes = await post(
    h.env,
    "/api/runs",
    {
      run_id: "9abcf468",
      kind: "review",
      started_at: now - 60_000,
      finished_at: now,
      articles_processed: 12,
      errors: 1,
      tokens: 4800,
      cost_usd_equiv: 3.21,
    },
    { bearer: PIPELINE_TOKEN, origin: null },
  );
  expect(runRes.status).toBe(200);
}

describe("GET /stats", () => {
  it("renders every aggregate from the seeded fixtures", async () => {
    const h = setup();
    await seedWorld(h);
    const res = await get(h.env, "/stats");
    expect(res.status).toBe(200);
    const html = await res.text();

    // Articles by review state.
    expect(html).toContain('<td>Articles</td><td class="wl-stat-num">5</td>');
    expect(html).toContain('<td>Never reviewed by the pipeline</td><td class="wl-stat-num">1</td>');
    expect(html).toContain('<td>Review fresh (≤30d)</td><td class="wl-stat-num">3</td>');
    expect(html).toContain('<td>Review stale (&gt;30d)</td><td class="wl-stat-num">1</td>');
    expect(html).toContain('<td>Drifted from pinned Wikipedia revision</td><td class="wl-stat-num">1</td>');
    expect(html).toContain('<td>Parked (moved / deleted / needs human)</td><td class="wl-stat-num">1</td>');

    // Annotations by status (count columns only — never a blob parse).
    expect(html).toContain('<td>Formalized</td><td class="wl-stat-num">5</td>');
    expect(html).toContain('<td>Partial</td><td class="wl-stat-num">1</td>');
    expect(html).toContain('<td>Not formalized</td><td class="wl-stat-num">1</td>');
    expect(html).toContain('<td>Articles awaiting count backfill</td><td class="wl-stat-num">1</td>');

    // Event cells: modify×human all-time 2 / last-30d 1; add×pipeline 1/1.
    expect(html).toContain(
      '<td><code>modify</code></td><td><code>human</code></td><td class="wl-stat-num">1</td><td class="wl-stat-num">2</td>',
    );
    expect(html).toContain(
      '<td><code>add</code></td><td><code>pipeline</code></td><td class="wl-stat-num">1</td><td class="wl-stat-num">1</td>',
    );

    // Human signals derived from the same cells.
    expect(html).toContain('<td>Endorsements (human agrees with AI)</td><td class="wl-stat-num">1</td>');
    expect(html).toContain('<td>Rejections (human veto / tombstone)</td><td class="wl-stat-num">1</td>');

    // Flags.
    expect(html).toContain('<td>Open flags</td><td class="wl-stat-num">2</td>');
    expect(html).toContain('<td>Resolved: fixed</td><td class="wl-stat-num">1</td>');
    expect(html).toContain('<td>Resolved: dismissed</td><td class="wl-stat-num">0</td>');

    // Revisions by kind + patrol split (seed edit unpatrolled, second edit patrolled).
    expect(html).toContain('<td>Revisions: edit</td><td class="wl-stat-num">2</td>');
    expect(html).toContain('<td>Revisions: pipeline</td><td class="wl-stat-num">1</td>');
    expect(html).toContain('<td>Human edits awaiting patrol</td><td class="wl-stat-num">1</td>');
    expect(html).toContain('<td>Human edits patrolled</td><td class="wl-stat-num">1</td>');

    // Pipeline runs: per-kind row + the all-runs total, cost rendered in $.
    expect(html).toContain(
      '<td>review</td><td class="wl-stat-num">1</td><td class="wl-stat-num">12</td><td class="wl-stat-num">1</td><td class="wl-stat-num">4,800</td><td class="wl-stat-num">$3.21</td>',
    );
    expect(html).toContain("<td>all runs</td>");

    // Every row names a research question (labels per docs/research-plan.md).
    expect(html).toContain('<span class="wl-rq">RQ1</span>');
    expect(html).toContain('<span class="wl-rq">RQ2</span>');
    expect(html).toContain('<span class="wl-rq">RQ4</span>');
    expect(html).toContain('<span class="wl-rq">RQ5</span>');
    expect(html).toContain('<span class="wl-rq">RQ8</span>');
  });

  it("always carries the zero-means-broken footnote (and the omitted-median note)", async () => {
    const h = setup();
    const res = await get(h.env, "/stats");
    expect(res.status).toBe(200);
    const html = await res.text();
    expect(html).toContain("Reading a zero:");
    expect(html).toContain("instrumentation is broken");
    expect(html).toContain("Median time-to-first-human-touch is omitted");
  });

  it("a runs table with no $ figures renders cost as — (unknown ≠ $0)", async () => {
    const h = setup();
    const res = await post(
      h.env,
      "/api/runs",
      {
        run_id: "0000bbbb",
        kind: "new",
        started_at: 1,
        finished_at: 2,
        articles_processed: 3,
        errors: 0,
        tokens: 100,
      },
      { bearer: PIPELINE_TOKEN, origin: null },
    );
    expect(res.status).toBe(200);
    const html = await (await get(h.env, "/stats")).text();
    expect(html).toContain('<td>new</td><td class="wl-stat-num">1</td>');
    expect(html).not.toContain("$0.00");
    expect(html).toContain('<span class="muted">—</span>');
  });

  it("is KV-cached under page:stats:v3 for 300s (TTL-only invalidation)", async () => {
    const h = setup();
    const first = await (await get(h.env, "/stats")).text();
    expect(h.renderCache.store.has("page:stats:v3")).toBe(true);
    // Mutate the DB; the cached page must still serve unchanged.
    insertArticle(h.db, "After_Cache");
    const second = await (await get(h.env, "/stats")).text();
    expect(second).toBe(first);
    expect(second).toContain('<td>Articles</td><td class="wl-stat-num">1</td>');
  });

  it("the public shell links to /stats", async () => {
    const h = setup();
    const html = await (await get(h.env, "/recent-changes")).text();
    expect(html).toContain('href="/stats"');
  });
});
