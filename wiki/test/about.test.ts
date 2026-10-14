// GET /about — the dynamic methodology page (home.ts aboutPage), which
// replaced the static wiki/public/about.html asset. Pins: 200 with live D1
// counts (COUNT(*) + summed count columns, SUM skipping pre-backfill NULL
// rows), the unified nav + Concepts + MCP links, the rewritten contribution
// copy, the moderation copy (Human-at-boundaries, ROADMAP 2026-08-06), the
// Wikidata property name (P14534), licensing notices, and the KV cache
// (page:about:v2, TTL 300, TTL-only invalidation). The harness ASSETS binding
// always 404s, so a 200 here also proves the route no longer depends on the
// retired asset.

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
    // The Wikidata property exists (P14534); the old proposal-era name is gone.
    expect(html).toContain('href="https://www.wikidata.org/wiki/Property:P14534"');
    expect(html.replace(/\s+/g, " ")).toContain("<em>&ldquo;Mathlib Declaration ID&rdquo;</em> (P14534)");
    expect(html).not.toContain("formalized as (Lean/Mathlib)");
    expect(html).not.toContain("basis for a proposed");
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
    expect(text).not.toContain("growing sample");
    // What actually happens.
    expect(html).toContain("<h2>How edits are moderated</h2>");
    expect(text).toContain("Annotation edits by people take effect immediately and are shown as human-curated");
    expect(text).toContain('in <a href="/recent-changes?kind=edit">Recent changes</a> with an awaiting-patrol marker');
    expect(text).toContain('<a href="/stats">Stats</a> page shows how many have been patrolled');
    // Revert restores annotations only; the Wikipedia pin is not rolled back.
    expect(text).toContain("restore an article&rsquo;s annotations to any earlier version in that history");
    expect(text).toContain("a pipeline save that drops or alters one is refused");
    expect(text).toContain("those proposals no longer need a person&rsquo;s approval");
    expect(text).toContain("deterministic resolver, which uses no language model");
    expect(text).toContain("the rest stay pending until a patroller or a later nightly run decides them");
    expect(text).toContain("lapse when the pipeline next saves the article");
    // index.ts approve path: a bot approval re-labels human → ai-moderated,
    // a patroller approval stamps human. The copy states both directions and
    // what the relabel means: script.js shows every non-'human' provenance as
    // "AI-generated · not yet reviewed", and the anti-clobber guards
    // (findLostHuman, _preserve_human) protect only provenance 'human'.
    expect(text).toContain("When the pipeline approves a change to a human-curated annotation, the annotation is relabeled: it is then shown as &ldquo;AI-generated &middot; not yet reviewed&rdquo; and loses the human-curated protection, so later pipeline saves can change it directly");
    expect(text).not.toContain("stops being shown as human-curated");
    expect(text).toContain("When a patroller approves a change, the result is shown as human-curated");
    // The bot opens Mathlib PRs unattended; the human gate is Mathlib review.
    expect(text).toContain("the tagging bot opens pull requests to Mathlib on its own, but they merge only after Mathlib reviewers approve them");
    expect(text).toContain("statements are added to Wikidata only for mappings people have reviewed");
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
