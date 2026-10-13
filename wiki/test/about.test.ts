// GET /about — the dynamic methodology page (home.ts aboutPage), which
// replaced the static wiki/public/about.html asset. Pins: 200 with live D1
// counts (COUNT(*) + summed count columns, SUM skipping pre-backfill NULL
// rows), the unified nav + Concepts + MCP links, the rewritten contribution
// copy, the moderation copy (Human-at-boundaries, ROADMAP 2026-08-06),
// licensing notices, and the KV cache (page:about:v2, TTL 300, TTL-only
// invalidation). The harness ASSETS binding always 404s, so a 200
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
    expect(html).toContain('<a href="/login">sign in</a>');
    // Providers are configured per deployment (auth.ts availableProviders);
    // the static copy must not promise one that may be absent.
    expect(html).not.toContain("GitHub or Google");
    expect(html).toContain("⚑");
    // Licensing: article text CC BY-SA, annotations CC0.
    expect(html).toContain("https://creativecommons.org/licenses/by-sa/4.0/");
    expect(html).toContain("https://creativecommons.org/publicdomain/zero/1.0/");
  });

  it("describes the Human-at-boundaries moderation policy, not a human gate on proposals", async () => {
    const h = setup();
    const html = await (await get(h.env, "/about")).text();
    const text = html.replace(/\s+/g, " ");
    // Copy that no longer matches the code or the ratified policy is gone.
    expect(text).not.toContain("queued for human approval");
    expect(text).not.toContain("patrolled by moderators");
    // What actually happens.
    expect(html).toContain("<h2>How edits are moderated</h2>");
    expect(text).toContain("Annotation edits by people take effect immediately and are shown as human-curated");
    expect(text).toContain('<a href="/stats">Stats</a> page shows how many have been patrolled');
    expect(text).toContain("a pipeline save that drops or alters one is refused");
    expect(text).toContain("those proposals do not wait for a person by default");
    expect(text).toContain("deterministic resolver, which uses no language model");
    expect(text).toContain("the rest stay pending until a patroller decides them");
    // index.ts approve path: a bot approval re-labels human → ai-moderated,
    // a patroller approval stamps human. The copy states both directions.
    expect(text).toContain("When the pipeline approves a change to a human-curated annotation, the annotation stops being shown as human-curated");
    expect(text).toContain("when a patroller approves a proposal, the result is shown as human-curated");
    expect(text).toContain("pull requests to Mathlib and statements added to Wikidata");
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

  it("is KV-cached under page:about:v2 for 300s (TTL-only invalidation)", async () => {
    const h = setup();
    const first = await (await get(h.env, "/about")).text();
    expect(h.renderCache.store.has("page:about:v2")).toBe(true);
    expect(h.renderCache.store.has("page:about:v1")).toBe(false);
    insertArticle(h.db, "After_Cache");
    const second = await (await get(h.env, "/about")).text();
    expect(second).toBe(first);
    expect(second).toContain("<b>1</b> articles annotated");
  });
});
