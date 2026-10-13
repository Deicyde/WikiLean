// GET /api/runs — public per-run reports (the read twin of POST /api/runs).
// Pins: anonymous access, response shape, newest-first order (started_at,
// then created_at), the kind filter ('all' is a run kind, not a wildcard),
// limit bounds (400, no clamping), notes re-redaction + cap, and the
// public 5-minute Cache-Control.

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
  it("is public and returns the full row shape, newest first, with a 5-minute public cache", async () => {
    const h = setup();
    expect((await report(h, { run_id: "0000aaa1", kind: "review", started_at: 100, finished_at: 200, articles_processed: 15, errors: 15, tokens: 0, model: "claude-x", prompt_sha: "abc123", cost_usd_equiv: 0, notes: "15/15 errors" })).status).toBe(200);
    expect((await report(h, { run_id: "0000aaa2", kind: "wp-update", started_at: 300 })).status).toBe(200);
    const res = await get(h.env, "/api/runs"); // no cookie, no bearer
    expect(res.status).toBe(200);
    expect(res.headers.get("Cache-Control")).toBe("public, max-age=300");
    const body = (await res.json()) as { runs: Array<Record<string, unknown>> };
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
      notes: "15/15 errors",
    });
    expect(body.runs[0]).toMatchObject({ kind: "wp-update", cost_usd_equiv: null, model: null, notes: null });
  });

  it("breaks started_at ties by server receipt time, newest first", async () => {
    const h = setup();
    expect((await report(h, { run_id: "0000bbb1", kind: "review", started_at: 500 })).status).toBe(200);
    h.db.prepare("UPDATE pipeline_runs SET created_at = 1 WHERE run_id = '0000bbb1'").run();
    expect((await report(h, { run_id: "0000bbb2", kind: "review", started_at: 500 })).status).toBe(200);
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
    // 'all' names the full-nightly run kind; it is not a wildcard.
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

  it("re-redacts tokens and emails, strips control bytes, and caps notes at 2000 chars", async () => {
    const h = setup();
    const notes =
      "agent_error: Authorization: Bearer abc.def-ghi sk-ant-api03-XYZ_123 ghp_" + "a".repeat(36) +
      " github_pat_11ABCDEF_xyz mail jack@example.org \u001b[31mred\u0007";
    expect((await report(h, { run_id: "0000ddd1", kind: "review", started_at: 10, notes })).status).toBe(200);
    const got = (await runs(h)).body.runs![0].notes as string;
    expect(got).toBe("agent_error: Authorization: [redacted] [redacted] [redacted] [redacted] mail [redacted] [31mred");
    // Rows written before this release could exceed the cap after redaction.
    h.db.prepare("UPDATE pipeline_runs SET notes = ? WHERE run_id = '0000ddd1'").run("x".repeat(3000));
    expect(((await runs(h)).body.runs![0].notes as string).length).toBe(2000);
  });

  it("POST /api/runs still requires the bot bearer", async () => {
    const h = setup();
    const res = await post(h.env, "/api/runs", { run_id: "0000eee1", kind: "review", started_at: 1, finished_at: 2, articles_processed: 0, errors: 0, tokens: 0 }, { origin: null });
    expect(res.status).toBe(403);
  });
});
