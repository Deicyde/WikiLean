// GET /about — the dynamic methodology page (home.ts aboutPage), which
// replaced the static wiki/public/about.html asset. Pins: 200 with live D1
// counts (COUNT(*) + summed count columns, SUM skipping pre-backfill NULL
// rows), the unified nav + Concepts + MCP links, the rewritten contribution
// copy + licensing notices, and the KV cache (page:about:v1, TTL 300,
// TTL-only invalidation). The harness ASSETS binding always 404s, so a 200
// here also proves the route no longer depends on the retired asset.

import { describe, it, expect } from "vitest";
import { setup, get, insertArticle, blockNetwork, type Harness } from "./helpers/harness.js";

blockNetwork();

function setCounts(h: Harness, slug: string, f: number, p: number, n: number): void {
  h.db
    .prepare("UPDATE articles SET n_formalized=?, n_partial=?, n_not_formalized=? WHERE slug=?")
    .run(f, p, n, slug);
}

describe("GET /about", () => {
  it("renders live counts from D1 (no static asset involved)", async () => {
    const h = setup();
    // Harness seed row "Test_Article" keeps NULL counts (skipped by SUM,
    // still counted as an article); Alpha carries 6/2/2.
    insertArticle(h.db, "Alpha");
    setCounts(h, "Alpha", 6, 2, 2);
    const res = await get(h.env, "/about");
    expect(res.status).toBe(200);
    const html = await res.text();
    expect(html).toContain("<b>2</b> articles annotated");
    expect(html).toContain("<b>10</b> tagged statements");
    expect(html).toContain("<b>60%</b> formalized");
    expect(html).toContain("<b>20%</b> partial");
    expect(html).not.toContain("NaN");
  });

  it("carries the unified nav plus Concepts + MCP, contribution copy, and licenses", async () => {
    const h = setup();
    const html = await (await get(h.env, "/about")).text();
    for (const href of ["/articles", "/brain", "/recent-changes", "/stats", "/concepts", "/mcp"]) {
      expect(html).toContain(`href="${href}"`);
    }
    // The stale "corrections via the source repository" limitation copy is
    // replaced by the live contribution loop.
    expect(html).not.toContain("Corrections are welcome via the source repository");
    expect(html).toContain("sign in");
    expect(html).toContain("GitHub or Google");
    expect(html).toContain("⚑");
    expect(html).toContain("human approval");
    // Licensing: article text CC BY-SA, annotations CC0.
    expect(html).toContain("https://creativecommons.org/licenses/by-sa/4.0/");
    expect(html).toContain("https://creativecommons.org/publicdomain/zero/1.0/");
  });

  it("handles an empty corpus without dividing by zero", async () => {
    const h = setup();
    h.db.prepare("DELETE FROM revisions").run(); // FK before articles
    h.db.prepare("DELETE FROM articles").run();
    const html = await (await get(h.env, "/about")).text();
    expect(html).toContain("<b>0</b> articles annotated");
    expect(html).toContain("<b>0%</b> formalized");
    expect(html).not.toContain("NaN");
  });

  it("is KV-cached under page:about:v1 for 300s (TTL-only invalidation)", async () => {
    const h = setup();
    const first = await (await get(h.env, "/about")).text();
    expect(h.renderCache.store.has("page:about:v1")).toBe(true);
    insertArticle(h.db, "After_Cache");
    const second = await (await get(h.env, "/about")).text();
    expect(second).toBe(first);
    expect(second).toContain("<b>1</b> articles annotated");
  });
});
