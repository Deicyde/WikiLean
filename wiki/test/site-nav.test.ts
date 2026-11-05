// The site navigation links the formalization atlas (docs/ATLAS.md section 14, item 8).
// Pins: the header of every Worker page that carries the site's navigation links /atlas
// once, labelled "Atlas", after Articles and before the Brain where those are listed (the
// order the atlas pages' own header has); every cached page whose bytes that changed moved
// to a new key and carries the link in its cached bytes; the JSON and XML answers, whose
// bytes did not change, keep their keys, and the sitemap leaves out the atlas, which asks
// not to be indexed.

import { describe, expect, it } from "vitest";
import { blockNetwork, get, setup, SLUG } from "./helpers/harness.js";

blockNetwork();

// One page from each shell the header comes from, with a user who may see it.
const PAGES: Array<{ path: string; user?: string }> = [
  { path: `/${SLUG}` }, // engine/page.ts
  { path: "/" }, // home.ts brainLanding
  { path: "/articles" }, // home.ts homePage
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

const ATLAS_LINK = /<a (?:class="wl-navlink" )?href="\/atlas">Atlas<\/a>/g;

function header(html: string): string {
  const start = html.indexOf('<header class="wl-header"');
  expect(start, "the page has the site header").toBeGreaterThanOrEqual(0);
  const end = html.indexOf("</header>", start);
  expect(end).toBeGreaterThan(start);
  return html.slice(start, end);
}

describe("the site navigation", () => {
  for (const { path, user } of PAGES) {
    it(`links the atlas from the header of ${path}`, async () => {
      const h = setup();
      const res = await get(h.env, path, user ? { user } : {});
      expect(res.status).toBe(200);
      const head = header(await res.text());
      expect(head.match(ATLAS_LINK)).toHaveLength(1);
      const atlas = head.search(ATLAS_LINK);
      const articles = head.indexOf('href="/articles"');
      const brain = head.indexOf('href="/brain"');
      if (articles >= 0) expect(articles).toBeLessThan(atlas);
      if (brain >= 0) expect(atlas).toBeLessThan(brain);
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
      ["/", "page:home:v11", ["page:home:v10"]],
      ["/articles", "page:articles:v4", ["page:articles:v3"]],
      ["/about", "page:about:v3", ["page:about:v2"]],
      ["/stats", "page:stats:v7", ["page:stats:v6"]],
      ["/wikifunctions", "page:wikifunctions:v4", ["page:wikifunctions:v3"]],
      ["/wikifunctions/verify", "page:wikifunctions-verify:v4", ["page:wikifunctions-verify:v3"]],
    ];
    for (const [path, key, before] of moved) {
      expect((await get(h.env, path)).status, path).toBe(200);
      const cached = h.renderCache.store.get(key);
      expect(cached, key).toBeDefined();
      expect(header(cached!).match(ATLAS_LINK), key).toHaveLength(1);
      const keys = [...h.renderCache.store.keys()];
      for (const prefix of before) expect(keys.some((k) => k.startsWith(prefix)), prefix).toBe(false);
    }
  });

  it("the article index and the sitemap keep their keys, and the sitemap leaves out the atlas", async () => {
    const h = setup();
    expect((await get(h.env, "/api/articles")).status).toBe(200);
    const sitemap = await get(h.env, "/sitemap.xml");
    expect(sitemap.status).toBe(200);
    expect(h.renderCache.store.has("page:articles-index:v1")).toBe(true);
    expect(h.renderCache.store.has("page:sitemap:v4")).toBe(true);
    expect(await sitemap.text()).not.toContain("/atlas");
  });
});
