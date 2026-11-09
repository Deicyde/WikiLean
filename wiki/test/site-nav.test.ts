// The site navigation and the formalization reader at /articles (docs/ARTICLES.md
// section 14, item 8, and "The rename"). Pins: the header of every Worker page that
// carries the site's navigation links /articles exactly once, labelled "Articles" and
// before the Brain where that is listed (the order the reader's own header has); the
// pages the rename moved to a new cache key carry the link in their cached bytes and
// left nothing under the old key; and /api/articles, whose bytes did not change, keeps
// its key, while the sitemap drops /articles, which now asks not to be indexed.

import { describe, expect, it } from "vitest";
import { blockNetwork, get, setup, SLUG } from "./helpers/harness.js";

blockNetwork();

// One page from each shell the header comes from, with a user who may see it.
// /articles is not here: it is the reader's own shell (test/articles.test.ts).
const PAGES: Array<{ path: string; user?: string }> = [
  { path: `/${SLUG}` }, // engine/page.ts
  { path: "/" }, // home.ts landingPage
  { path: "/contribute" },
  { path: "/about" }, // home.ts aboutPage
  { path: "/stats" }, // pages.ts shell, and the pages below
  { path: "/recent-changes" },
  { path: `/${SLUG}/history` },
  { path: `/${SLUG}/diff/1/1` },
  { path: "/u/u-human" },
  { path: "/flags", user: "u-patroller" },
  { path: "/proposals", user: "u-patroller" },
  { path: "/wikifunctions" }, // wikifunctions.ts
  { path: "/wikifunctions/verify" }, // wikifunctions-verify.ts
];

const ARTICLES_HREF = /<a (?:class="wl-navlink" )?href="\/articles">Articles<\/a>/g;

function header(html: string): string {
  const start = html.indexOf('<header class="wl-header"');
  expect(start, "the page has the site header").toBeGreaterThanOrEqual(0);
  const end = html.indexOf("</header>", start);
  expect(end).toBeGreaterThan(start);
  return html.slice(start, end);
}

describe("the site navigation", () => {
  for (const { path, user } of PAGES) {
    it(`links /articles once from the header of ${path}`, async () => {
      const h = setup();
      const res = await get(h.env, path, user ? { user } : {});
      expect(res.status).toBe(200);
      const head = header(await res.text());
      expect(head.match(ARTICLES_HREF)).toHaveLength(1);
      // Nothing links the reader under its old path any more.
      expect(head).not.toContain('href="/atlas"');
      const articles = head.search(ARTICLES_HREF);
      const brain = head.indexOf('href="/brain"');
      if (brain >= 0) expect(articles).toBeLessThan(brain);
      // A "·" between links keeps a space on each side ("Brain · Recent changes").
      expect(head.replace(/<[^>]*>/g, "")).not.toMatch(/\S·|·\S/);
    });
  }
});

describe("the cached pages", () => {
  it("each moved to a new key, and its cached bytes carry the link", async () => {
    const h = setup();
    const moved: Array<[path: string, key: string, before: string[]]> = [
      [`/${SLUG}`, `render:v20:${SLUG}:1`, ["render:v18:", "render:v19:"]],
      ["/", "page:home:v12", ["page:home:v11"]],
      ["/about", "page:about:v4", ["page:about:v3"]],
      ["/stats", "page:stats:v8", ["page:stats:v7"]],
      ["/wikifunctions", "page:wikifunctions:v4", ["page:wikifunctions:v3"]],
      ["/wikifunctions/verify", "page:wikifunctions-verify:v4", ["page:wikifunctions-verify:v3"]],
    ];
    for (const [path, key, before] of moved) {
      expect((await get(h.env, path)).status, path).toBe(200);
      const cached = h.renderCache.store.get(key);
      expect(cached, key).toBeDefined();
      expect(header(cached!).match(ARTICLES_HREF), key).toHaveLength(1);
      const keys = [...h.renderCache.store.keys()];
      for (const prefix of before) expect(keys.some((k) => k.startsWith(prefix)), prefix).toBe(false);
    }
  });

  it("the old /articles directory key is retired and nothing writes it", async () => {
    const h = setup();
    for (const path of ["/", "/about", "/stats", `/${SLUG}`]) await get(h.env, path);
    const keys = [...h.renderCache.store.keys()];
    expect(keys.some((k) => k.startsWith("page:articles:"))).toBe(false);
  });

  it("the article index keeps its key, and the sitemap leaves out /articles", async () => {
    const h = setup();
    expect((await get(h.env, "/api/articles")).status).toBe(200);
    const sitemap = await get(h.env, "/sitemap.xml");
    expect(sitemap.status).toBe(200);
    expect(h.renderCache.store.has("page:articles-index:v1")).toBe(true);
    expect(h.renderCache.store.has("page:sitemap:v5")).toBe(true);
    const xml = await sitemap.text();
    expect(xml).not.toContain("<loc>https://wikilean.jackmccarthy.org/articles</loc>");
    expect(xml).not.toContain("/atlas");
  });
});
