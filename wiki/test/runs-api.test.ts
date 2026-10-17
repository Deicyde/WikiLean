// GET /api/runs — public per-run reports (the read twin of POST /api/runs).
// Pins: anonymous access, the counts-only response shape (notes are never
// served), newest-first order by server receipt (created_at, then run_id; a
// runner-reported started_at/finished_at cannot pin the top row), the kind
// filter ('all' is a run kind, not a wildcard), limit bounds (400, no
// clamping), and the public 5-minute Cache-Control.

import { describe, it, expect } from "vitest";
import { setup, post, get, blockNetwork, PIPELINE_TOKEN, type Harness } from "./helpers/harness.js";

blockNetwork();

function report(h: Harness, body: Record<string, unknown>): Promise<Response> {
  return post(
    h.env,
    "/api/runs",
    { started_at: 1, finished_at: 2, articles_processed: 0, errors: 0, tokens: 0, ...body },
    { bearer: PIPELINE_TOKEN, origin: null },
  );
}

async function runs(h: Harness, query = ""): Promise<{ status: number; body: { runs?: Array<Record<string, unknown>> } }> {
  const res = await get(h.env, `/api/runs${query}`);
  return { status: res.status, body: (await res.json()) as { runs?: Array<Record<string, unknown>> } };
}

describe("GET /api/runs", () => {
  it("is public and returns the counts-only row shape, newest first, with a 5-minute public cache", async () => {
    const h = setup();
    expect((await report(h, { run_id: "0000aaa1", kind: "review", started_at: 100, finished_at: 200, articles_processed: 15, errors: 15, tokens: 0, model: "claude-x", prompt_sha: "abc123", cost_usd_equiv: 0, notes: "15/15 errors" })).status).toBe(200);
    expect((await report(h, { run_id: "0000aaa2", kind: "wp-update", started_at: 300 })).status).toBe(200);
    const res = await get(h.env, "/api/runs"); // no cookie, no bearer
    expect(res.status).toBe(200);
    expect(res.headers.get("Cache-Control")).toBe("public, max-age=300");
    const text = await res.text();
    // Notes stay in D1: neither the key nor the posted text is served.
    expect(text).not.toContain("notes");
    expect(text).not.toContain("15/15 errors");
    const body = JSON.parse(text) as { runs: Array<Record<string, unknown>> };
    expect(body.runs.map((r) => r.run_id)).toEqual(["0000aaa2", "0000aaa1"]);
    expect(body.runs[1]).toEqual({
      run_id: "0000aaa1",
      kind: "review",
      started_at: 100,
      finished_at: 200,
      articles_processed: 15,
      errors: 15,
      tokens: 0,
      cost_usd_equiv: 0,
      model: "claude-x",
      prompt_sha: "abc123",
    });
    expect(body.runs[0]).toMatchObject({ kind: "wp-update", cost_usd_equiv: null, model: null });
  });

  it("orders by server receipt time, newest first, then run_id", async () => {
    const h = setup();
    expect((await report(h, { run_id: "0000bbb1", kind: "review", started_at: 500 })).status).toBe(200);
    expect((await report(h, { run_id: "0000bbb2", kind: "review", started_at: 500 })).status).toBe(200);
    // Backdate the SECOND insert, so insertion order would give the wrong answer.
    h.db.prepare("UPDATE pipeline_runs SET created_at = 1 WHERE run_id = '0000bbb2'").run();
    expect((await runs(h)).body.runs!.map((r) => r.run_id)).toEqual(["0000bbb1", "0000bbb2"]);
    h.db.prepare("UPDATE pipeline_runs SET created_at = 1").run();
    expect((await runs(h)).body.runs!.map((r) => r.run_id)).toEqual(["0000bbb2", "0000bbb1"]);
  });

  it("filters by kind and honors limit (default 10, 1..50)", async () => {
    const h = setup();
    for (let i = 0; i < 12; i++) {
      const id = (0xc000 + i).toString(16).padStart(8, "0");
      expect((await report(h, { run_id: id, kind: i % 2 ? "review" : "all", started_at: 1000 + i })).status).toBe(200);
    }
    expect((await runs(h)).body.runs).toHaveLength(10);
    expect((await runs(h, "?limit=50")).body.runs).toHaveLength(12);
    expect((await runs(h, "?limit=1")).body.runs).toHaveLength(1);
    const reviews = (await runs(h, "?kind=review")).body.runs!;
    expect(reviews).toHaveLength(6);
    expect(reviews.every((r) => r.kind === "review")).toBe(true);
    // 'all' is the kind a combined `moderate.py all` run records; it is not a wildcard.
    const alls = (await runs(h, "?kind=all&limit=50")).body.runs!;
    expect(alls).toHaveLength(6);
    expect(alls.every((r) => r.kind === "all")).toBe(true);
  });

  it("400s on an unknown kind or a bad limit (no clamping)", async () => {
    const h = setup();
    for (const q of ["?kind=bogus", "?kind=", "?limit=0", "?limit=51", "?limit=-1", "?limit=abc", "?limit=5.5", "?limit=100"]) {
      expect((await runs(h, q)).status, q).toBe(400);
    }
  });

  it("a future-dated runner timestamp does not take the top row", async () => {
    const h = setup();
    const now = Date.now();
    // A healthy run whose runner reported microseconds (far future as ms),
    // then a failing real run. The bogus row has the higher run_id, so only
    // the receipt-time order puts the real run on top.
    expect((await report(h, { run_id: "0000fff2", kind: "review", started_at: now * 1000, finished_at: now * 1000, articles_processed: 15, errors: 0, tokens: 90000 })).status).toBe(200);
    expect((await report(h, { run_id: "0000fff1", kind: "review", started_at: now, finished_at: now + 1, articles_processed: 15, errors: 15, tokens: 0 })).status).toBe(200);
    h.db.prepare("UPDATE pipeline_runs SET created_at = ? WHERE run_id = '0000fff2'").run(now - 1000);
    h.db.prepare("UPDATE pipeline_runs SET created_at = ? WHERE run_id = '0000fff1'").run(now);
    for (const q of ["", "?kind=review"]) {
      const top = (await runs(h, q)).body.runs![0];
      expect(top, q).toMatchObject({ run_id: "0000fff1", errors: 15, tokens: 0 });
    }
  });

  it("POST /api/runs still requires the bot bearer", async () => {
    const h = setup();
    const res = await post(h.env, "/api/runs", { run_id: "0000eee1", kind: "review", started_at: 1, finished_at: 2, articles_processed: 0, errors: 0, tokens: 0 }, { origin: null });
    expect(res.status).toBe(403);
  });
});
