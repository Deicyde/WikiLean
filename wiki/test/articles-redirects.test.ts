// The reader's old /atlas URLs (index.ts atlasRedirect, docs/ARTICLES.md "The rename").
// Pins: every path the reader answered under /atlas and /api/atlas/<sub> 301s to the same
// path under /articles and /api/articles/, query string and all; the old graph stack's own
// names (the bare /api/atlas, /atlas_data.json) are untouched; /api/articles, the header
// search box's index of WikiLean's own articles, keeps answering exactly as it did, with no
// X-Robots-Tag; and a redirect needs no store, so it answers with ARTICLES unbound.

import { describe, expect, it } from "vitest";
import { blockNetwork, get, setup } from "./helpers/harness.js";
import { bindArticles, FIXTURE_UNIVERSE_SHA, WIDGET_PAGEID } from "./helpers/articles.js";

blockNetwork();

const MOVED: Array<[from: string, to: string]> = [
  ["/atlas", "/articles"],
  ["/atlas?q=widget", "/articles?q=widget"],
  [`/atlas/${WIDGET_PAGEID}`, `/articles/${WIDGET_PAGEID}`],
  [`/atlas/${WIDGET_PAGEID}/Widget_lemma`, `/articles/${WIDGET_PAGEID}/Widget_lemma`],
  [`/atlas/${WIDGET_PAGEID}/Widget_lemma?view=cards`, `/articles/${WIDGET_PAGEID}/Widget_lemma?view=cards`],
  [`/atlas/universe/${FIXTURE_UNIVERSE_SHA}`, `/articles/universe/${FIXTURE_UNIVERSE_SHA}`],
  ["/api/atlas/current", "/api/articles/current"],
  [`/api/atlas/article/${WIDGET_PAGEID}`, `/api/articles/article/${WIDGET_PAGEID}`],
  [`/api/atlas/records/${WIDGET_PAGEID}`, `/api/articles/records/${WIDGET_PAGEID}`],
  [`/api/atlas/universe/${FIXTURE_UNIVERSE_SHA}`, `/api/articles/universe/${FIXTURE_UNIVERSE_SHA}`],
  ["/api/atlas/index/1", "/api/articles/index/1"],
  ["/api/atlas/feedback", "/api/articles/feedback"],
  ["/api/atlas/feedback/export", "/api/articles/feedback/export"],
  // A path the reader never routed still moves: the new path answers for it.
  ["/atlas/nope", "/articles/nope"],
];

describe("the reader's old /atlas URLs", () => {
  for (const [from, to] of MOVED) {
    it(`301s ${from} to ${to}`, async () => {
      const h = setup();
      bindArticles(h.env);
      const res = await get(h.env, from);
      expect(res.status, from).toBe(301);
      expect(res.headers.get("Location"), from).toBe(to);
    });
  }

  it("redirects with no store bound, and reads nothing from one that is", async () => {
    const absent = setup();
    expect((await get(absent.env, "/atlas")).status).toBe(301);
    const present = setup();
    const bucket = bindArticles(present.env);
    expect((await get(present.env, `/atlas/${WIDGET_PAGEID}`)).status).toBe(301);
    expect(bucket.gets).toEqual([]);
  });

  it("leaves the old graph stack's own paths as the plain 404 they are", async () => {
    const h = setup();
    for (const path of ["/api/atlas", "/atlas_data.json"]) {
      const res = await get(h.env, path);
      expect(res.status, path).toBe(404);
      expect(res.headers.get("Location"), path).toBeNull();
    }
  });

  it("leaves /api/articles, the header search index, exactly as it was", async () => {
    const h = setup();
    const res = await get(h.env, "/api/articles");
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("application/json");
    expect(res.headers.get("X-Robots-Tag")).toBeNull();
    expect(res.headers.get("Cache-Control")).toBe("public, max-age=300");
    expect(await res.json()).toEqual({ v: 1, articles: expect.any(Array) });
  });
});
