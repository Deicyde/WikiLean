// Anchor-rot telemetry (P1): the cache-miss branch of the article render path
// (renderArticleBase, index.ts) emits ONE structured console.log line —
// {event:'render', slug, version, revid, matched, total} — built from the wrap
// engine's real match results. Log-only: no D1 write rides along
// (articles.anchored_count is deferred). Cache hits emit nothing.

import { describe, it, expect, vi, afterEach } from "vitest";
import { setup, get, SLUG, REVID, blockNetwork } from "./helpers/harness.js";

function renderLines(spy: ReturnType<typeof vi.spyOn>): Array<Record<string, unknown>> {
  return spy.mock.calls
    .map((c) => String(c[0]))
    .filter((s) => s.includes('"event":"render"'))
    .map((s) => JSON.parse(s) as Record<string, unknown>);
}

describe("anchor-rot render telemetry", () => {
  blockNetwork();
  afterEach(() => vi.restoreAllMocks());

  it("cache-miss render logs one structured line with truthful matched/total", async () => {
    const h = setup();
    const spy = vi.spyOn(console, "log");
    const res = await get(h.env, `/${SLUG}`);
    expect(res.status).toBe(200);
    const lines = renderLines(spy);
    expect(lines).toHaveLength(1);
    // Both seed anchors resolve in WP_FIXTURE → 2/2 (matches the save path's
    // "2/2 anchored" contract pinned in api.test.ts).
    expect(lines[0]).toEqual({
      event: "render",
      slug: SLUG,
      version: 1,
      revid: REVID,
      matched: 2,
      total: 2,
    });
  });

  it("reports the real gap when an anchor no longer matches (anchor rot)", async () => {
    const h = setup();
    // Rot one anchor in place: its snippet no longer appears in WP_FIXTURE.
    const anns = JSON.parse(
      (h.db.prepare("SELECT annotations FROM articles WHERE slug = ?").get(SLUG) as { annotations: string })
        .annotations,
    ) as Array<{ anchor: { snippet: string } }>;
    anns[1].anchor.snippet = "text that was rewritten out of the article";
    h.db
      .prepare("UPDATE articles SET annotations = ? WHERE slug = ?")
      .run(JSON.stringify(anns), SLUG);

    const spy = vi.spyOn(console, "log");
    expect((await get(h.env, `/${SLUG}`)).status).toBe(200);
    const lines = renderLines(spy);
    expect(lines).toHaveLength(1);
    expect(lines[0].matched).toBe(1);
    expect(lines[0].total).toBe(2);
  });

  it("tombstones count as matched (engine contract: excluded ≠ anchor failure)", async () => {
    const h = setup();
    const anns = JSON.parse(
      (h.db.prepare("SELECT annotations FROM articles WHERE slug = ?").get(SLUG) as { annotations: string })
        .annotations,
    ) as Array<{ status: string; provenance: string }>;
    anns[1].status = "rejected";
    anns[1].provenance = "human";
    h.db
      .prepare("UPDATE articles SET annotations = ? WHERE slug = ?")
      .run(JSON.stringify(anns), SLUG);

    const spy = vi.spyOn(console, "log");
    expect((await get(h.env, `/${SLUG}`)).status).toBe(200);
    const lines = renderLines(spy);
    expect(lines).toHaveLength(1);
    // A veto is not rot: matched stays equal to total.
    expect(lines[0].matched).toBe(2);
    expect(lines[0].total).toBe(2);
  });

  it("cache hits emit no render line", async () => {
    const h = setup();
    expect((await get(h.env, `/${SLUG}`)).status).toBe(200); // warm the cache
    const spy = vi.spyOn(console, "log");
    expect((await get(h.env, `/${SLUG}`)).status).toBe(200);
    expect(renderLines(spy)).toHaveLength(0);
  });
});
