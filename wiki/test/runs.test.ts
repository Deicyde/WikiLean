// POST /api/runs (P2a; cross-agent contract RUNS-API): bot-bearer-only run
// reporting with idempotent retries on run_id. Authz cells live in
// authz.test.ts; this suite pins the body contract and the duplicate
// semantics.

import { describe, it, expect } from "vitest";
import {
  setup,
  post,
  pipelineRunRows,
  blockNetwork,
  PIPELINE_TOKEN,
  type Harness,
} from "./helpers/harness.js";

blockNetwork();

// A full, valid report (every contract field present).
function runBody(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    run_id: "9abcf468",
    kind: "review",
    model: "claude-fable-5",
    prompt_sha: "c0ffee00",
    started_at: 1_780_000_000_000,
    finished_at: 1_780_000_600_000,
    articles_processed: 12,
    errors: 1,
    tokens: 480_000,
    cost_usd_equiv: 3.21,
    notes: "first wave-E verification run",
    ...over,
  };
}

function report(h: Harness, body: Record<string, unknown>): Promise<Response> {
  // As the runner sends it: bearer auth, no Origin header.
  return post(h.env, "/api/runs", body, { bearer: PIPELINE_TOKEN, origin: null });
}

describe("POST /api/runs", () => {
  it("inserts a pipeline_runs row with every contract field", async () => {
    const h = setup();
    const res = await report(h, runBody());
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true });

    const rows = pipelineRunRows(h.db);
    expect(rows.length).toBe(1);
    expect(rows[0]).toMatchObject({
      run_id: "9abcf468",
      kind: "review",
      model: "claude-fable-5",
      prompt_sha: "c0ffee00",
      started_at: 1_780_000_000_000,
      finished_at: 1_780_000_600_000,
      articles_processed: 12,
      errors: 1,
      tokens: 480_000,
      cost_usd_equiv: 3.21,
      notes: "first wave-E verification run",
    });
    expect(rows[0].created_at).toBeGreaterThan(0);
  });

  it("optional fields may be omitted (or null) — stored as NULL", async () => {
    const h = setup();
    const res = await report(h, {
      run_id: "0000aaaa",
      kind: "wp-update",
      started_at: 1,
      finished_at: 2,
      articles_processed: 0,
      errors: 0,
      tokens: 0,
      cost_usd_equiv: null,
    });
    expect(res.status).toBe(200);
    const row = pipelineRunRows(h.db)[0];
    expect(row.model).toBeNull();
    expect(row.prompt_sha).toBeNull();
    expect(row.cost_usd_equiv).toBeNull();
    expect(row.notes).toBeNull();
  });

  it("duplicate run_id → 200 {ok, duplicate} and the first report is never overwritten", async () => {
    const h = setup();
    expect((await report(h, runBody())).status).toBe(200);
    // Retry with DIFFERENT numbers (a runner re-send after a dropped response).
    const res = await report(h, runBody({ tokens: 999, articles_processed: 99 }));
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true, duplicate: true });

    const rows = pipelineRunRows(h.db);
    expect(rows.length).toBe(1);
    expect(rows[0].tokens).toBe(480_000); // first write wins
    expect(rows[0].articles_processed).toBe(12);
  });

  it("accepts every runner kind", async () => {
    const h = setup();
    const kinds = ["review", "wp-update", "new", "all"];
    for (let i = 0; i < kinds.length; i++) {
      const res = await report(h, runBody({ run_id: `0000000${i}`, kind: kinds[i] }));
      expect(res.status).toBe(200);
    }
    expect(pipelineRunRows(h.db).length).toBe(4);
  });

  it("rejects malformed run_ids (contract: exactly 8 lowercase hex)", async () => {
    const h = setup();
    for (const bad of ["DEADBEEF", "abc1234", "abc123456", "g0000000", 12345678, null, undefined]) {
      const res = await report(h, runBody({ run_id: bad }));
      expect(res.status).toBe(400);
    }
    expect(pipelineRunRows(h.db).length).toBe(0);
  });

  it("rejects unknown kinds", async () => {
    const h = setup();
    for (const bad of ["moderate", "", null, 7]) {
      expect((await report(h, runBody({ kind: bad }))).status).toBe(400);
    }
  });

  it("rejects missing/non-integer/negative timestamps and counters", async () => {
    const h = setup();
    for (const field of ["started_at", "finished_at", "articles_processed", "errors", "tokens"]) {
      expect((await report(h, runBody({ [field]: undefined }))).status).toBe(400);
      expect((await report(h, runBody({ [field]: 1.5 }))).status).toBe(400);
      expect((await report(h, runBody({ [field]: -1 }))).status).toBe(400);
      expect((await report(h, runBody({ [field]: "12" }))).status).toBe(400);
    }
    expect(pipelineRunRows(h.db).length).toBe(0);
  });

  it("rejects a non-numeric or negative cost", async () => {
    const h = setup();
    expect((await report(h, runBody({ cost_usd_equiv: "3.21" }))).status).toBe(400);
    expect((await report(h, runBody({ cost_usd_equiv: -0.5 }))).status).toBe(400);
    // (Infinity/NaN can't be tested through JSON — JSON.stringify maps them
    // to null, which is the valid "unknown cost"; the isFinite guard stays as
    // server-side defense for non-JSON clients.)
  });

  it("size-caps notes (413) and rejects non-string notes", async () => {
    const h = setup();
    expect((await report(h, runBody({ notes: "x".repeat(2001) }))).status).toBe(413);
    expect((await report(h, runBody({ notes: ["a"] }))).status).toBe(400);
    expect((await report(h, runBody({ notes: "x".repeat(2000) }))).status).toBe(200);
  });

  it("rejects bad json bodies", async () => {
    const h = setup();
    // post() JSON.stringify(undefined) → no request body → c.req.json() throws.
    const res = await post(h.env, "/api/runs", undefined, { bearer: PIPELINE_TOKEN, origin: null });
    expect(res.status).toBe(400);
  });

  it("blocks cross-origin browser POSTs (403) even with the bearer", async () => {
    const h = setup();
    const res = await post(h.env, "/api/runs", runBody(), {
      bearer: PIPELINE_TOKEN,
      origin: "https://evil.example",
    });
    expect(res.status).toBe(403);
    expect(pipelineRunRows(h.db).length).toBe(0);
  });

  it("denied non-bot reports leave no row", async () => {
    const h = setup();
    const res = await post(h.env, "/api/runs", runBody(), { user: "u-admin" });
    expect(res.status).toBe(403);
    expect(pipelineRunRows(h.db).length).toBe(0);
  });
});
