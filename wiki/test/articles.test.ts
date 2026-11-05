// The formalization reader, the articles plane (src/articles.ts, src/articles-render.ts,
// src/libraries.ts, migration 0015; design docs/ARTICLES.md). Pins: every articles
// route answers 503 while ARTICLES is unbound; with the golden fixture store bound
// the pages and APIs serve it (404 for unknown ids); feedback is session-only,
// same-origin, capped, rate limited, validated against the store, and exported
// to the pipeline bearer under pseudonyms; the article page shows the summary
// box per request without touching the cached base page.

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { describe, it, expect, vi } from "vitest";
import {
  setup, get, post, blockNetwork, PIPELINE_TOKEN, REVID, SLUG, ORIGIN, type Harness,
} from "./helpers/harness.js";
import {
  ARTICLES_STORE_ROOT, articlesFixtureFiles, articlesReaderFiles, bindArticles, fixtureArticle, FIXTURE_CAMPAIGN, FIXTURE_UNIVERSE_SHA,
  GADGET_PAGEID, READER_ORDER, readerHtml, WIDGET_PAGEID,
} from "./helpers/articles.js";
import {
  ARTICLES_PSEUDONYM_DOMAIN, CAMPAIGN_RE, INDEX_CACHE_MS, MAX_SLUG_CACHE, NOT_PUBLISHED, SLUG_CACHE_MS, STORE_UNREADABLE,
  articlesHref, kvReader, loadArticle, loadCurrent, loadIndexShard, lookupTitle, memoryReader, parsePageid, staticReader,
  titleKey, titleShard, wikileanSlugFor, type ArticlesLocation,
} from "../src/articles.js";
import {
  ARTICLES_JS, ARTICLES_NOTICE, ARTICLES_READER_CSS, ARTICLES_READER_JS, UNMEASURED, articlesSummaryBox, campaignLabel, locationStatus,
  plainHead,
} from "../src/articles-render.js";
import { libraryInfo, libraryInfos, repoHref, sourceUrl, treeUrl } from "../src/libraries.js";
import { injectArticlesSummary } from "../src/pages.js";
import { app } from "../src/index.js";
import {
  ARTICLES_FORM_PATH, ARTICLES_INLINE_SCRIPT_HASHES, ARTICLES_MEDIA_HOSTS, ARTICLES_SCRIPT_PATHS, CSP_REPORT_ONLY, MAX_JSON_BODY_BYTES,
  SECURITY_HEADERS, articlesCsp,
} from "../src/security.js";

blockNetwork();

// The policy every articles page enforces, for the origin the harness serves (app.request's).
const ARTICLES_CSP = articlesCsp(ORIGIN);

const WIDGET_PATH = `/articles/${WIDGET_PAGEID}/Widget_lemma`;
// Response.json() is `unknown` under workers-types.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const jsonOf = async (res: Response): Promise<any> => res.json();
const MATHLIB_KEY = fixtureArticle().passages[0].locations[0].location_key as string;

function bound(opts: Parameters<typeof setup>[0] = {}): Harness & { gets: string[] } {
  const h = setup(opts);
  const bucket = bindArticles(h.env);
  return { ...h, gets: bucket.gets };
}

function feedback(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return { location_key: MATHLIB_KEY, pageid: WIDGET_PAGEID, campaign: FIXTURE_CAMPAIGN, verdict: "confirm", ...overrides };
}

function feedbackRows(h: Harness): Array<Record<string, unknown>> {
  return h.db.prepare("SELECT * FROM atlas_feedback ORDER BY id").all() as Array<Record<string, unknown>>;
}

// The Confirm / Report controls of one location on a coverage page.
function feedbackBox(html: string, key = MATHLIB_KEY): string {
  const start = html.indexOf(`<div class="articles-fb" data-location-key="${key}">`);
  expect(start, "the location has feedback controls").toBeGreaterThanOrEqual(0);
  return html.slice(start, html.indexOf("</div>", start));
}

// The fixture store as a later build publishes it: another campaign and record
// set over the same article and the same location keys.
const NEXT_CAMPAIGN = "fixture-2026-11";
const NEXT_RECORD_SET = "b".repeat(64);
function nextCampaignStore(): Map<string, string> {
  const files = articlesFixtureFiles();
  for (const [key, value] of files) {
    if (key === "articles/v1/current.json" || key.startsWith("articles/v1/articles/")) {
      const doc = JSON.parse(value);
      doc.campaign = NEXT_CAMPAIGN;
      if (key.startsWith("articles/v1/articles/")) doc.record_set = NEXT_RECORD_SET;
      files.set(key, JSON.stringify(doc));
    }
  }
  return files;
}

describe("articles routes without a store", () => {
  const PAGE_ROUTES = ["/articles", `/articles/${WIDGET_PAGEID}`, WIDGET_PATH, `/articles/universe/${FIXTURE_UNIVERSE_SHA}`];
  const API_ROUTES = [
    "/api/articles/current", `/api/articles/article/${WIDGET_PAGEID}`, `/api/articles/records/${WIDGET_PAGEID}`,
    `/api/articles/universe/${FIXTURE_UNIVERSE_SHA}`, "/api/articles/index/1",
  ];

  it("every page and API route answers 503 'articles store not configured', never a crash", async () => {
    const h = setup();
    for (const path of PAGE_ROUTES) {
      const res = await get(h.env, path);
      expect(res.status, path).toBe(503);
      expect(res.headers.get("Content-Type"), path).toContain("text/html");
      expect(await res.text(), path).toContain("articles store not configured");
    }
    for (const path of API_ROUTES) {
      const res = await get(h.env, path);
      expect(res.status, path).toBe(503);
      expect(await jsonOf(res), path).toEqual({ ok: false, error: "articles store not configured" });
      expect(res.headers.get("Cache-Control"), path).toBe("no-store");
    }
    const write = await post(h.env, "/api/articles/feedback", feedback(), { user: "u-human" });
    expect(write.status).toBe(503);
    expect(feedbackRows(h)).toEqual([]);
  });

  it("an article page has no articles box and is unchanged", async () => {
    const h = setup();
    const html = await (await get(h.env, `/${SLUG}`)).text();
    expect(html).not.toContain("wl-articles-box");
    expect(html).not.toContain("articles.css");
  });

  it("a bound but empty store says it has no published build", async () => {
    const h = setup();
    bindArticles(h.env, new Map());
    const page = await get(h.env, "/articles");
    expect(page.status).toBe(503);
    expect(await page.text()).toContain("articles store has no published build");
    const current = await get(h.env, "/api/articles/current");
    expect(current.status).toBe(503);
    expect(await jsonOf(current)).toEqual({ ok: false, error: NOT_PUBLISHED });
  });

  it("without current.json, the store's publish switch, every articles route answers 503 and article pages show no box", async () => {
    // The data rollback deletes current.json alone (docs/ARTICLES.md, "Publishing"): the bucket still
    // holds every article, record set, marked revision and shard, and none of them is served.
    const files = articlesReaderFiles();
    files.delete("articles/v1/current.json");
    const h = setup();
    const bucket = bindArticles(h.env, files);
    h.db.prepare("UPDATE articles SET wikipedia_title = 'Widget lemma' WHERE slug = ?").run(SLUG);
    for (const path of [...PAGE_ROUTES, `${WIDGET_PATH}?view=cards`]) {
      const res = await get(h.env, path);
      expect(res.status, path).toBe(503);
      expect(res.headers.get("Cache-Control"), path).toBe("no-store");
      expect(res.headers.get("Content-Security-Policy"), path).toBe(`${ARTICLES_CSP}, ${SECURITY_HEADERS["Content-Security-Policy"]}`);
      const html = await res.text();
      expect(html, path).toContain(`The articles plane is unavailable: ${NOT_PUBLISHED}.`);
      expect(html, path).toContain(ARTICLES_NOTICE);
    }
    for (const path of API_ROUTES) {
      const res = await get(h.env, path);
      expect(res.status, path).toBe(503);
      expect(res.headers.get("Cache-Control"), path).toBe("no-store");
      expect(await jsonOf(res), path).toEqual({ ok: false, error: NOT_PUBLISHED });
    }
    const write = await post(h.env, "/api/articles/feedback", feedback(), { user: "u-human" });
    expect(write.status).toBe(503);
    expect(await jsonOf(write)).toEqual({ ok: false, error: NOT_PUBLISHED });
    expect(feedbackRows(h)).toEqual([]);
    expect(await (await get(h.env, `/${SLUG}`)).text()).not.toContain("wl-articles-box");
    // Nothing but the switch was read, once: its absence is kept a minute like any answer.
    expect(bucket.gets).toEqual(["articles/v1/current.json"]);
    // The feedback export reads only D1, and keeps working.
    expect((await get(h.env, "/api/articles/feedback/export", { bearer: PIPELINE_TOKEN })).status).toBe(200);
  });

  it("a small object that does not parse is kept as that failure for a minute, so the routes without a budget read it once", async () => {
    const files = articlesFixtureFiles();
    files.set("articles/v1/current.json", "{not json");
    const h = setup();
    const bucket = bindArticles(h.env, files);
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    try {
      for (let round = 0; round < 25; round += 1) {
        for (const path of ["/articles", `/articles/universe/${FIXTURE_UNIVERSE_SHA}`]) {
          expect((await get(h.env, path)).status, path).toBe(503);
        }
      }
    } finally {
      log.mockRestore();
    }
    expect(bucket.gets).toEqual(["articles/v1/current.json"]);
    // The failure lasts the minute any answer does: a repaired object shows after it.
    const reader = memoryReader(files);
    await expect(loadCurrent(reader, 1_000)).rejects.toThrow("is not JSON");
    files.set("articles/v1/current.json", articlesFixtureFiles().get("articles/v1/current.json")!);
    await expect(loadCurrent(reader, 1_000 + INDEX_CACHE_MS - 1)).rejects.toThrow("is not JSON");
    expect((await loadCurrent(reader, 1_000 + INDEX_CACHE_MS))?.campaign).toBe(FIXTURE_CAMPAIGN);
  });

  it("current.json is read once a minute per isolate, so a build published or withdrawn shows within INDEX_CACHE_MS", async () => {
    const files = articlesFixtureFiles();
    const reader = memoryReader(files);
    const published = await loadCurrent(reader, 1_000);
    expect(published?.campaign).toBe(FIXTURE_CAMPAIGN);
    files.delete("articles/v1/current.json");
    expect(await loadCurrent(reader, 1_000 + INDEX_CACHE_MS - 1)).toBe(published);
    expect(await loadCurrent(reader, 1_000 + INDEX_CACHE_MS)).toBeNull();
    files.set("articles/v1/current.json", JSON.stringify({ ...published, campaign: NEXT_CAMPAIGN }));
    expect(await loadCurrent(reader, 1_000 + 2 * INDEX_CACHE_MS - 1)).toBeNull();
    expect((await loadCurrent(reader, 1_000 + 2 * INDEX_CACHE_MS))?.campaign).toBe(NEXT_CAMPAIGN);
  });
});

describe("articles APIs with the fixture store", () => {
  it("serve the store's bytes with JSON / NDJSON types and cache headers", async () => {
    const h = bound();
    const files = articlesFixtureFiles();
    const article = await get(h.env, `/api/articles/article/${WIDGET_PAGEID}`);
    expect(article.status).toBe(200);
    expect(article.headers.get("Content-Type")).toBe("application/json; charset=utf-8");
    expect(article.headers.get("Cache-Control")).toBe("public, max-age=300");
    expect(await article.text()).toBe(files.get(`articles/v1/articles/${WIDGET_PAGEID}.json`));

    const records = await get(h.env, `/api/articles/records/${WIDGET_PAGEID}`);
    expect(records.status).toBe(200);
    expect(records.headers.get("Content-Type")).toBe("application/x-ndjson; charset=utf-8");
    const body = await records.text();
    expect(body).toBe(files.get(`articles/v1/records/${WIDGET_PAGEID}.jsonl`));
    expect(body.trim().split("\n").map((line) => JSON.parse(line).relation)).toEqual(
      ["exactMatch", "closeMatch", "exactMatch", "noMatch", "relatedMatch", undefined]);

    // Not immutable: the key is the producer's universe sha, which does not
    // cover the display metadata stored beside the libraries.
    const universe = await get(h.env, `/api/articles/universe/${FIXTURE_UNIVERSE_SHA}`);
    expect(universe.status).toBe(200);
    expect(universe.headers.get("Cache-Control")).toBe("public, max-age=300");
    expect((await jsonOf(universe)).libraries).toHaveLength(5);

    const current = await get(h.env, "/api/articles/current");
    expect(current.status).toBe(200);
    expect((await jsonOf(current)).campaign).toBe(FIXTURE_CAMPAIGN);

    const shard = await get(h.env, "/api/articles/index/1");
    expect((await jsonOf(shard)).articles[0][0]).toBe("Widget lemma");
  });

  it("answer 404 for unknown or malformed ids", async () => {
    const h = bound();
    for (const path of [
      "/api/articles/article/9999", "/api/articles/article/0", "/api/articles/article/abc", "/api/articles/article/01001",
      "/api/articles/records/9999", "/api/articles/universe/" + "f".repeat(64), "/api/articles/universe/not-a-sha",
      "/api/articles/index/z", "/api/articles/index/0",
    ]) {
      const res = await get(h.env, path);
      expect(res.status, path).toBe(404);
      expect((await jsonOf(res)).ok, path).toBe(false);
    }
  });
});

describe("articles pages with the fixture store", () => {
  it("the dashboard shows totals by status, by library and by importance, and lists articles", async () => {
    const h = bound();
    const res = await get(h.env, "/articles");
    expect(res.status).toBe(200);
    const html = await res.text();
    expect(html).toContain("<title>WikiLean · Articles</title>");
    // The passage with no status is in no count, status or bar: the totals say there is one more.
    expect(html).toContain("2 articles · 6 passages · 7 Lean locations · 1 related declaration " +
      '<span class="articles-muted" id="articles-unread">1 more passage has no status yet; it is left out until someone reads it.</span></p>');
    expect(html).toContain('aria-label="6 passages: 4 formalized, 1 statement only, 1 not formalized"');
    expect(html).not.toMatch(/Undecided|undecided|data-status="unresolved"/);
    // By importance and per article: the passages that have a status, and four status columns.
    expect(html).toContain('<tr><th scope="row">High</th><td class="num">1</td><td class="num">3</td><td class="num" data-status="formalized">1</td>' +
      '<td class="num" data-status="statement_only">1</td><td class="num" data-status="partial">0</td><td class="num" data-status="not_formalized">1</td></tr>');
    expect(html).toMatch(/<a href="\/articles\/1001\/Widget_lemma">Widget lemma<\/a><\/th><td class="num">3<\/td><td class="num" data-status="formalized">1<\/td>/);
    // By library: most passages first, then most complete locations, then by
    // name (FormalFrontier and TauCeti hold one passage each, and only
    // FormalFrontier's location is complete). Batteries holds only a related
    // declaration and keeps its row. Seeded repositories keep the registry's
    // name (the universe's "Lean 4 core (Init, Std, Lean)" is not used).
    const names = ["Mathlib", "FormalFrontier · WidgetTheory", "TauCeti", "Batteries"];
    const at = names.map((name) => html.indexOf(`<th scope="row">${name}</th>`));
    expect(at.every((i) => i > 0)).toBe(true);
    expect([...at].sort((a, b) => a - b)).toEqual(at);
    expect(html).not.toContain("Lean 4 core");
    // A library with no passage and no related declaration is not a row of
    // zeros: it is named, with its commit, below the table.
    expect(html).not.toContain('<th scope="row">Lean core</th>');
    const leanCore = "1465a20b72ff3532c8f25b2532812415f8fbb0e1";
    // The table's column notes follow it directly; the list comes after them.
    expect(html).toContain('</table></div>\n<p class="articles-muted">A passage counts toward every library that states it.');
    expect(html).toContain('as locations.</p>\n<details id="articles-libs-empty"><summary>1 more library searched, with no locations in this build</summary>' +
      `<ul><li>Lean core <a href="https://github.com/leanprover/lean4/tree/${leanCore}" rel="noopener"><code class="articles-sha">@1465a20</code></a></li></ul></details>\n</section>`);
    // After the bar over all passages and its legend: the central statements
    // (subject and central passages), what the rest are, and the central ones
    // by status. The comma between two counts is an element, so the summary
    // box's ".articles-mini + .articles-mini" margin does not apply.
    expect(html).toContain('</span></span></div>\n<p class="articles-strip-lead" id="articles-central"><b>Central statements:</b> 4 of the 6 passages, across 2 articles, ' +
      "state an article's subject or a named result or definition; the other 2 are supporting (examples, setup). Of these 4: " +
      '<span class="articles-mini" data-status="formalized"><span class="articles-ico" aria-hidden="true">✓</span>3 formalized</span><span>,</span> ' +
      '<span class="articles-mini" data-status="not_formalized"><span class="articles-ico" aria-hidden="true">✕</span>1 not formalized</span>.</p>\n</section>');
    // Complete and incomplete locations are separate columns: TauCeti only
    // states its result, so it is never counted like a proved Mathlib row.
    expect(html).toContain('<th scope="col" class="num">Complete</th><th scope="col" class="num">Incomplete</th>');
    expect(html).toMatch(/<th scope="row">TauCeti<\/th><td>.*?<\/td><td class="num">1<\/td><td class="num">1<\/td><td class="num">0<\/td><td class="num">1<\/td><td class="num">0<\/td><\/tr>/);
    expect(html).toMatch(/<th scope="row">Mathlib<\/th><td>.*?<\/td><td class="num">2<\/td><td class="num">4<\/td><td class="num">5<\/td><td class="num">0<\/td><td class="num">0<\/td><\/tr>/);
    expect(html).toContain('<code class="articles-sha">@bcb35bd</code>');
    expect(html).toContain("By WikiProject importance");
    expect(html).toContain('<th scope="row">High</th>');
    expect(html).toContain(`href="${WIDGET_PATH}"`);
    expect(html).toContain(`href="/articles/${GADGET_PAGEID}/Gadget_(mathematics)"`);
    expect(html).toContain('data-shards="1 8"');
    // Where each article is formalized: a Libraries column from the index.
    expect(html).toContain('<th scope="col">Libraries</th>');
    expect(html).toContain('title="FormalFrontier · WidgetTheory · https://github.com/FormalFrontier/WidgetTheory">FormalFrontier · WidgetTheory</span> ' +
      '<span class="articles-libtag" title="Mathlib · https://github.com/leanprover-community/mathlib4">Mathlib</span> ' +
      '<span class="articles-libtag" title="TauCeti · https://github.com/TauCetiProject/TauCeti">TauCeti</span>');
    expect(html).toContain("data-library-names=\"{&quot;batteries&quot;:&quot;Batteries&quot;,&quot;ff-widgets&quot;");
    expect(html).toContain("&quot;mathlib&quot;:&quot;Mathlib&quot;");
    // Provenance sits behind a closed fold (the display rule): the campaign, the build date
    // and the record set are under "About this build", never in the open meta line.
    expect(html).toContain('<p class="articles-meta"><a href="/articles/universe/');
    expect(html).toContain(`<details class="articles-about"><summary>About this build</summary><ul class="articles-prov-list"><li>${UNMEASURED}</li><li>Campaign fixture-2026-10, from record sets exported 17 Oct 2026.</li><li>Campaign <code>fixture-2026-10</code>.</li>` +
      '<li>Record set <code>fixture-set</code>, exported 17 Oct 2026 by formal-companion at commit <code title="3bceabcee0724ffdf50ce65dde3ab10048c72cba">3bceabc</code>.</li><li>Seal sha256 <code>');
    expect(html).toContain("<li>The record set does not record the model or the operator that ran these runs.</li></ul></details>");
    expect(html).not.toContain("<details class=\"articles-about\" open");
    // The bar's legend: each totals chip carries its fill's swatch, for the four statuses a passage can be given.
    expect(html).toContain('<span class="articles-swatch" data-status="not_formalized" aria-hidden="true"></span>');
    expect(html.match(/<span class="articles-chip is-static" data-status="/g)).toHaveLength(4);
    // Every table that can scroll sideways is a labelled, focusable region.
    for (const name of ["By library", "By WikiProject importance", "Articles"]) {
      expect(html).toContain(`<div class="articles-table-wrap" role="region" tabindex="0" aria-label="Table: ${name}">`);
    }
    expect(html).toContain('<a class="wl-navlink" href="/articles" aria-current="page">Articles</a>');
    expect(html).toContain('<script src="/assets/articles.js?v=5"></script>');
    expect(html).toContain('<link rel="stylesheet" href="/assets/articles.css?v=10">');
    expect(html).not.toMatch(/\son[a-z]+=/i); // no inline event handlers
    expect(html).not.toMatch(/\bAI\b|pipeline/); // no generic label anywhere a reader sees
  });

  it("the dashboard reads each index shard once a minute, not once per view", async () => {
    const h = bound();
    await get(h.env, "/articles");
    await get(h.env, "/articles?q=widget");
    await get(h.env, `/${SLUG}`);
    expect(h.gets.filter((key) => key === "articles/v1/index/1.json")).toHaveLength(1);
    expect(h.gets.filter((key) => key === "articles/v1/index/8.json")).toHaveLength(1);
    // A cached shard expires after INDEX_CACHE_MS.
    const files = articlesFixtureFiles();
    const reader = memoryReader(files);
    const first = await loadIndexShard(reader, "1", 1_000);
    files.set("articles/v1/index/1.json", files.get("articles/v1/index/8.json")!.replace('"shard":"8"', '"shard":"1"'));
    expect(await loadIndexShard(reader, "1", 1_000 + INDEX_CACHE_MS - 1)).toBe(first);
    expect((await loadIndexShard(reader, "1", 1_000 + INDEX_CACHE_MS))?.[0].title).toBe("Gadget (mathematics)");
  });

  it("a dashboard search keys an IPv6 client by its /64, so rotating inside it buys no new budget", async () => {
    const h = setup();
    bindArticles(h.env);
    const keys: string[] = [];
    const record = { limit: async ({ key }: { key: string }) => { keys.push(key); return { success: true }; } };
    Object.assign(h.env, { BRAIN_API_LIMITER: record });
    for (const ip of ["2001:db8:1:2::a", "2001:db8:1:2:ffff::b", "2001:db8:1:3::a", "198.51.100.4"]) {
      expect((await get(h.env, "/articles?q=gadg", { ip })).status).toBe(200);
    }
    expect((await get(h.env, "/articles?q=gadg")).status).toBe(200);
    expect(keys).toEqual([
      "articles-q:2001:db8:1:2::/64", "articles-q:2001:db8:1:2::/64", "articles-q:2001:db8:1:3::/64", "articles-q:198.51.100.4",
      "articles-q:unknown",
    ]);
  });

  it("a dashboard search takes the per-IP anonymous read budget; the plain list does not", async () => {
    const h = setup({ brainApiLimiterAllows: false });
    bindArticles(h.env);
    const limited = await get(h.env, "/articles?q=gadg", { ip: "198.51.100.4" });
    expect(limited.status).toBe(429);
    expect(limited.headers.get("Retry-After")).toBe("60");
    expect(limited.headers.get("Cache-Control")).toBe("no-store");
    expect(await limited.text()).toContain("Too many searches from this address in a minute.");
    expect((await get(h.env, "/articles")).status).toBe(200);
  });

  // The routes that read an article: its page (the reader or the card list) and its two JSON views.
  const ARTICLE_READS = [WIDGET_PATH, `/articles/${WIDGET_PAGEID}`, `/api/articles/article/${WIDGET_PAGEID}`, `/api/articles/records/${WIDGET_PAGEID}`];

  it("an article page and its two JSON views take one per-IP read budget, keyed apart from search", async () => {
    const h = setup();
    bindArticles(h.env, articlesReaderFiles());
    const keys: string[] = [];
    const record = { limit: async ({ key }: { key: string }) => { keys.push(key); return { success: true }; } };
    Object.assign(h.env, { BRAIN_API_LIMITER: record });
    for (const path of [...ARTICLE_READS, `${WIDGET_PATH}?view=cards`]) {
      expect((await get(h.env, path, { ip: "2001:db8:1:2::a" })).status, path).toBe(200);
    }
    expect((await get(h.env, "/articles?q=gadg", { ip: "2001:db8:1:2::b" })).status).toBe(200);
    // A malformed or unknown id is still a 404, after the budget.
    expect((await get(h.env, "/api/articles/article/0x1", { ip: "198.51.100.4" })).status).toBe(404);
    expect((await get(h.env, "/articles/9999")).status).toBe(404);
    expect(keys).toEqual([
      ...Array(ARTICLE_READS.length + 1).fill("articles-read:2001:db8:1:2::/64"), "articles-q:2001:db8:1:2::/64",
      "articles-read:198.51.100.4", "articles-read:unknown",
    ]);
    // The small JSON objects take a budget of their own, so the dashboard's live filter, which
    // fetches every shard, never spends an article's; the dashboard without a search and the
    // universe page take none.
    keys.length = 0;
    for (const path of ["/api/articles/current", `/api/articles/universe/${FIXTURE_UNIVERSE_SHA}`, "/api/articles/index/1"]) {
      expect((await get(h.env, path, { ip: "2001:db8:1:2::a" })).status, path).toBe(200);
    }
    expect(keys).toEqual(Array(3).fill("articles-meta:2001:db8:1:2::/64"));
    keys.length = 0;
    for (const path of ["/articles", `/articles/universe/${FIXTURE_UNIVERSE_SHA}`]) {
      expect((await get(h.env, path)).status, path).toBe(200);
    }
    expect(keys).toEqual([]);
    // MCP_LIMITER, when bound, keeps the budget instead.
    const mcp: string[] = [];
    Object.assign(h.env, { MCP_LIMITER: { limit: async ({ key }: { key: string }) => { mcp.push(key); return { success: true }; } } });
    expect((await get(h.env, WIDGET_PATH, { ip: "198.51.100.4" })).status).toBe(200);
    expect(mcp).toEqual(["articles-read:198.51.100.4"]);
    expect(keys).toEqual([]);
  });

  it("over the meta budget the small JSON objects answer 429; the dashboard and the universe page still answer", async () => {
    const h = setup({ brainApiLimiterAllows: false });
    const bucket = bindArticles(h.env);
    for (const path of ["/api/articles/current", `/api/articles/universe/${FIXTURE_UNIVERSE_SHA}`, "/api/articles/index/1"]) {
      const res = await get(h.env, path, { ip: "198.51.100.4" });
      expect(res.status, path).toBe(429);
      expect(res.headers.get("Retry-After"), path).toBe("60");
      expect(res.headers.get("Cache-Control"), path).toBe("no-store");
      expect(await jsonOf(res), path).toEqual({ ok: false, error: "too many requests from this address; try again in a minute" });
    }
    expect(bucket.gets).toEqual([]);
    expect((await get(h.env, "/articles")).status).toBe(200);
    expect((await get(h.env, `/articles/universe/${FIXTURE_UNIVERSE_SHA}`)).status).toBe(200);
  });

  it("the small store objects are read once a minute per isolate, whatever the route, and only those current.json names", async () => {
    const h = bound();
    const files = articlesFixtureFiles();
    const small = ["/articles", `/articles/universe/${FIXTURE_UNIVERSE_SHA}`, "/api/articles/current",
      `/api/articles/universe/${FIXTURE_UNIVERSE_SHA}`, "/api/articles/index/1", "/api/articles/index/8"];
    for (let round = 0; round < 5; round += 1) {
      for (const path of small) expect((await get(h.env, path)).status, path).toBe(200);
    }
    const once = ["articles/v1/current.json", "articles/v1/index/1.json", "articles/v1/index/8.json",
      `articles/v1/universes/${FIXTURE_UNIVERSE_SHA}.json`];
    expect([...h.gets].sort()).toEqual(once);
    // The JSON routes serve the stored bytes, from the cache.
    for (const [path, key] of [["/api/articles/current", "current.json"], [`/api/articles/universe/${FIXTURE_UNIVERSE_SHA}`,
      `universes/${FIXTURE_UNIVERSE_SHA}.json`], ["/api/articles/index/8", "index/8.json"]]) {
      const res = await get(h.env, path);
      expect(res.headers.get("Content-Type"), path).toBe("application/json; charset=utf-8");
      expect(await res.text(), path).toBe(files.get(`articles/v1/${key}`));
    }
    // A sha or shard that current.json does not name is a 404 that reads nothing (a store holds
    // the one universe its build names, and the shards it lists).
    for (const path of [`/api/articles/universe/${"f".repeat(64)}`, `/articles/universe/${"f".repeat(64)}`, "/api/articles/index/0",
      "/api/articles/index/ff"]) {
      expect((await get(h.env, path)).status, path).toBe(404);
    }
    expect([...h.gets].sort()).toEqual(once);
  });

  it("a store object that does not parse, or lacks the shape the pages read, is the plane's 503 page or JSON, never the site's 500", async () => {
    const article = `articles/v1/articles/${WIDGET_PAGEID}.json`;
    const edit = (key: string, change: (doc: Record<string, unknown>) => void) => (files: Map<string, string>) => {
      const doc = JSON.parse(files.get(key)!);
      change(doc);
      files.set(key, JSON.stringify(doc));
    };
    const records = `articles/v1/records/${WIDGET_PAGEID}.jsonl`;
    const shards = (change: (row: unknown[]) => void) => (files: Map<string, string>) => {
      for (const key of [...files.keys()].filter((name) => name.startsWith("articles/v1/index/"))) {
        edit(key, (doc) => { (doc.articles as unknown[][]).forEach(change); })(files);
      }
    };
    const cases: Array<[string, (files: Map<string, string>) => void, string[], string[]]> = [
      // The JSON views check what they serve as the pages do: the view must parse, and the records
      // must be the ones the view names.
      ["a truncated article", (files) => files.set(article, files.get(article)!.slice(0, 400)),
        [WIDGET_PATH, `${WIDGET_PATH}?view=cards`], [`/api/articles/article/${WIDGET_PAGEID}`, `/api/articles/records/${WIDGET_PAGEID}`]],
      ["records cut short", (files) => files.set(records, files.get(records)!.slice(0, 200)), [], [`/api/articles/records/${WIDGET_PAGEID}`]],
      ["records missing", (files) => files.delete(records), [], [`/api/articles/records/${WIDGET_PAGEID}`]],
      // JSON can hold a lone surrogate, which no URL can carry (encodeURIComponent throws URIError).
      ["an article title no URL can carry", edit(article, (doc) => { doc.title = `${doc.title}\ud800`; }),
        [WIDGET_PATH, `${WIDGET_PATH}?view=cards`], []],
      ["an index title no URL can carry", shards((row) => { row[0] = `${row[0]}\ud800`; }), ["/articles"], []],
      ["an article without its universe", edit(article, (doc) => { delete doc.universe; }), [WIDGET_PATH], []],
      ["a truncated current.json", (files) => files.set("articles/v1/current.json", '{"schema"'),
        ["/articles", WIDGET_PATH, `/articles/universe/${FIXTURE_UNIVERSE_SHA}`],
        ["/api/articles/current", `/api/articles/article/${WIDGET_PAGEID}`, "/api/articles/index/1"]],
      ["a current.json of another schema", (files) => files.set("articles/v1/current.json", JSON.stringify({ schema: "x" })),
        ["/articles"], ["/api/articles/current"]],
      ["a universe of another shape", (files) => files.set(`articles/v1/universes/${FIXTURE_UNIVERSE_SHA}.json`, "[]"),
        ["/articles", `/articles/universe/${FIXTURE_UNIVERSE_SHA}`], [`/api/articles/universe/${FIXTURE_UNIVERSE_SHA}`]],
    ];
    const logged: string[] = [];
    const log = vi.spyOn(console, "log").mockImplementation((line: unknown) => { logged.push(String(line)); });
    try {
      for (const [label, damage, pages, apis] of cases) {
        const files = articlesFixtureFiles();
        damage(files);
        const h = setup();
        bindArticles(h.env, files);
        for (const path of pages) {
          const res = await get(h.env, path);
          expect(res.status, `${label}: ${path}`).toBe(503);
          expect(res.headers.get("Content-Type"), `${label}: ${path}`).toMatch(/^text\/html/);
          expect(res.headers.get("Cache-Control"), `${label}: ${path}`).toBe("no-store");
          expect(res.headers.get("Content-Security-Policy"), `${label}: ${path}`).toBe(`${ARTICLES_CSP}, ${SECURITY_HEADERS["Content-Security-Policy"]}`);
          const html = await res.text();
          expect(html, `${label}: ${path}`).toContain(`The articles plane is unavailable: ${STORE_UNREADABLE}.`);
          expect(html, `${label}: ${path}`).toContain(ARTICLES_NOTICE);
          expect(html, `${label}: ${path}`).not.toMatch(/SyntaxError|TypeError|Unexpected|articles\/v1\//);
        }
        for (const path of apis) {
          const res = await get(h.env, path);
          expect(res.status, `${label}: ${path}`).toBe(503);
          expect(res.headers.get("Cache-Control"), `${label}: ${path}`).toBe("no-store");
          expect(await jsonOf(res), `${label}: ${path}`).toEqual({ ok: false, error: STORE_UNREADABLE });
        }
      }
      // A feedback write against a damaged article stores nothing and says so.
      const files = articlesFixtureFiles();
      files.set(article, files.get(article)!.slice(0, 400));
      const h = setup();
      bindArticles(h.env, files);
      const write = await post(h.env, "/api/articles/feedback", feedback(), { user: "u-human" });
      expect(write.status).toBe(503);
      expect(await jsonOf(write)).toEqual({ ok: false, error: STORE_UNREADABLE });
      expect(feedbackRows(h)).toEqual([]);
    } finally {
      log.mockRestore();
    }
    // Each one is logged with its route, never shown.
    const events = logged.map((line) => { try { return JSON.parse(line); } catch { return null; } })
      .filter((event) => event?.event === "articles-store-error");
    expect(events.length).toBeGreaterThanOrEqual(12);
    expect(events.find((event) => event.route === "article-page")).toMatchObject({ pageid: String(WIDGET_PAGEID) });
  });

  it("an article whose lists are not lists renders without them, never a 500", async () => {
    const files = articlesFixtureFiles();
    const key = `articles/v1/articles/${WIDGET_PAGEID}.json`;
    const doc = JSON.parse(files.get(key)!);
    doc.passages = "x";
    files.set(key, JSON.stringify(doc));
    const h = setup();
    bindArticles(h.env, files);
    const res = await get(h.env, WIDGET_PATH);
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toMatch(/^text\/html/);
    expect(res.headers.get("Content-Security-Policy")).toBe(`${ARTICLES_CSP}, ${SECURITY_HEADERS["Content-Security-Policy"]}`);
    const write = await post(h.env, "/api/articles/feedback", feedback(), { user: "u-human" });
    expect(write.status).toBe(404);
    expect(await jsonOf(write)).toEqual({ ok: false, error: "unknown location_key" });
  });

  it("over the read budget an article page or JSON view answers 429 with Retry-After, before any store or D1 read", async () => {
    const h = setup({ brainApiLimiterAllows: false });
    const bucket = bindArticles(h.env, articlesReaderFiles());
    const sql: string[] = [];
    const prepare = h.env.DB.prepare.bind(h.env.DB);
    Object.assign(h.env.DB, { prepare: (query: string) => { sql.push(query); return prepare(query); } });
    for (const path of ARTICLE_READS) {
      const res = await get(h.env, path, { ip: "198.51.100.4", user: "u-human" });
      expect(res.status, path).toBe(429);
      expect(res.headers.get("Retry-After"), path).toBe("60");
      expect(res.headers.get("Cache-Control"), path).toBe("no-store");
      const body = await res.text();
      if (path.startsWith("/api/")) {
        expect(JSON.parse(body)).toEqual({ ok: false, error: "too many requests from this address; try again in a minute" });
      } else {
        expect(res.headers.get("Content-Type")).toMatch(/^text\/html/);
        expect(body).toContain("<title>WikiLean · Too many requests</title>");
        expect(body).toContain("Too many requests from this address in a minute.");
      }
      // Nothing about how the budget is kept: no binding, key, address or store path.
      expect(body, path).not.toMatch(/LIMITER|articles-read|198\.51\.100\.4|articles\/v1|wikipedia_title/);
    }
    expect(bucket.gets).toEqual([]);
    expect(sql).toEqual([]);
    // Unbound, the pages are still "not configured" (503), and spend no budget.
    const unbound = setup();
    const keys: string[] = [];
    Object.assign(unbound.env, { BRAIN_API_LIMITER: { limit: async ({ key }: { key: string }) => { keys.push(key); return { success: false }; } } });
    for (const path of ARTICLE_READS) expect((await get(unbound.env, path)).status, path).toBe(503);
    expect(keys).toEqual([]);
  });

  it("the dashboard search filters server-side through the index shards", async () => {
    const h = bound();
    const html = await (await get(h.env, "/articles?q=gadg")).text();
    expect(html).toContain("1 of 2 articles match");
    expect(html).toContain("Gadget (mathematics)</a>");
    expect(html).not.toContain(`href="${WIDGET_PATH}"`);
    const none = await (await get(h.env, "/articles?q=" + encodeURIComponent("<script>"))).text();
    expect(none).toContain("0 of 2 articles match “&lt;script&gt;”");
  });

  it("an article coverage page renders every outcome", async () => {
    const h = bound();
    const res = await get(h.env, WIDGET_PATH);
    expect(res.status).toBe(200);
    const html = await res.text();
    expect(html).toContain('<h1 class="articles-h1">Widget lemma</h1>');
    // Headings to navigate by: Coverage, Passages, one h3 per passage, and an
    // h4 over related declarations.
    expect(html).toContain('<section class="articles-strip" aria-labelledby="articles-h-coverage">\n<h2 class="articles-h2" id="articles-h-coverage">Coverage</h2>');
    expect(html).toContain('<h2 class="articles-h2" id="articles-h-passages">Passages</h2>');
    // One h3 per passage with a status: the conjecture has none, so it is set aside until read.
    expect(html.match(/<h3 class="articles-p-h">/g)).toHaveLength(3);
    expect(html).not.toContain('id="p-0c4d77b1"');
    // The article's subject passage is marked: a tag in its heading, a class on
    // its block, and a "Subject" row in the strip linking it. Other passages
    // keep their prominence in the eyebrow.
    // Visually hidden separators keep the heading's accessible name apart:
    // "Theorem · Subject of the article · § Statement", not "…articleStatement".
    expect(html).toContain('<h3 class="articles-p-h"><span class="articles-eyebrow">Theorem</span><span class="articles-sr"> · </span>' +
      '<span class="articles-subject">Subject of the article</span><span class="articles-sr"> · </span><span class="articles-section">Statement</span></h3>');
    expect(html).toContain('<li class="articles-passage is-subject" id="p-3fa91c07" data-status="formalized" data-prominence="subject"');
    expect(html.match(/class="articles-passage is-subject"/g)).toHaveLength(1);
    expect(html.match(/class="articles-subject"/g)).toHaveLength(1);
    expect(html).toContain('<span class="articles-eyebrow">Definition · central</span><span class="articles-sr"> · </span><span class="articles-section">Definitions</span></h3>');
    expect(html).toContain('<li class="articles-passage" id="p-5b0e44a1" data-status="statement_only" data-prominence="supporting"');
    expect(html).toContain('<div class="articles-strip-row"><span class="articles-strip-h" id="articles-h-subject">Subject</span>' +
      '<ul class="articles-subject-list" aria-labelledby="articles-h-subject"><li><a href="#p-3fa91c07">Theorem: “every finite widget admits a spanning gadget”</a> ' +
      '<span class="articles-pill" data-status="formalized">');
    expect(html).toContain("Searched <a href=\"/articles/universe/" + FIXTURE_UNIVERSE_SHA + "\">universe u-fixture</a>: 5 libraries, frozen 2026-10-17, 1 excluded</p>");
    // The campaign and the record set are under a closed "About this data" fold.
    const fixtureRecordSet = fixtureArticle().record_set;
    expect(html).toContain(`<details class="articles-about"><summary>About this data</summary><ul class="articles-prov-list"><li>${UNMEASURED}</li>` +
      `<li>Campaign <code>fixture-2026-10</code>.</li><li>Seal sha256 <code>${fixtureRecordSet}</code>.</li>` +
      "<li>The record set does not record the model or the operator that ran these runs.</li></ul></details>");
    expect(html).not.toContain(`record set <code title="${fixtureRecordSet}">`);
    expect(html).toContain('<h4 class="articles-related-h">Related, but states something else</h4>');
    // Only the dashboard is the Articles nav link's own page.
    expect(html).toContain('<a class="wl-navlink" href="/articles">Articles</a>');
    expect(html).not.toContain('aria-current="page"');
    // The footer claims only what the page shows.
    expect(html).toContain("Each location&#x27;s Details name the locate run that found it and the challenge run that re-checked it, if one did. Signed-in readers can confirm or report a location.");
    // No generic label anywhere a reader sees, and no voting data: no counts of others' verdicts.
    expect(html).not.toMatch(/\bAI\b|pipeline|challenger/);
    expect(html).not.toMatch(/\d+ (confirm|report|vote)/i);
    // Strip: counts by status and by library, as filter chips.
    expect(html).toContain('data-filter="status:formalized"');
    expect(html).toContain('data-filter="library:mathlib"');
    expect(html).toContain('data-filter="library:ff-widgets"');
    // A zero count is shown but is not a filter.
    expect(html).not.toContain('data-filter="status:partial"');
    expect(html).toContain('<span class="articles-chip is-static is-zero" data-status="partial">');
    expect(html).toContain("3 passages checked, 3 Lean locations (and 1 related declaration). " +
      '<span class="articles-muted" id="articles-unread">1 more passage has no status yet; it is left out until someone reads it.</span>');
    expect(html).toContain("Batteries <span class=\"articles-n\">related only</span>");
    // The status in words is visible text beside the pill, not a hover-only
    // title, and the pill is not read twice.
    expect(html).toContain('<p class="articles-why">Formalized: Mathlib states this exactly (proved).</p>');
    expect(html).toContain('<p class="articles-why">Statement only: TauCeti states this exactly (statement only: the proof is not finished).</p>');
    expect(html).toContain('<span class="articles-pill" data-status="formalized"><span class="articles-ico" aria-hidden="true">✓</span>Formalized</span>');
    expect(html).not.toContain('class="articles-pill" data-status="formalized" title=');
    expect(html.match(/class="articles-why"/g)).toHaveLength(2); // the absence has its own box
    // The producer's item id is markup, not reader-facing text.
    expect(html).toContain('data-item="In-text #3fa91c07"');
    expect(html).not.toMatch(/>In-text #/);
    // The status chips carry the coverage bar's fills as their legend.
    expect(html).toContain('<span class="articles-swatch" data-status="statement_only" aria-hidden="true"></span>');
    // Location card: it leads with the Lean code (the declaration, then its module and lines),
    // then the library chip, short commit, relation, proof status and pinned source link.
    const mathlib = fixtureArticle().passages[0].locations[0];
    const sha7 = mathlib.commit.slice(0, 7);
    expect(html).toContain('<article class="articles-card" data-library="mathlib" data-relation="exactMatch" data-proof="proved">\n' +
      '<p class="articles-decl-line"><code class="articles-decl"><span class="articles-ns">Widget.<wbr></span><b>exists_<wbr>spanning_<wbr>gadget</b></code> ' +
      '<span class="articles-rel" data-relation="exactMatch" title="States this exactly (exactMatch)">exact match</span></p>\n' +
      '<p class="articles-module">Mathlib.Combinatorics.Widget.Basic · lines 612–640</p>\n<div class="articles-card-top">');
    // Its provenance is specific and behind a closed Details fold, after the Lean code.
    expect(html).toContain('<details class="articles-prov"><summary>Details</summary><ul class="articles-prov-list">' +
      "<li>Found in campaign <code>fixture-2026-10</code>: candidate 1 for this passage, high confidence.</li>" +
      "<li>Re-checked in challenge run (unnamed): it survived.</li>" +
      "<li>The record set does not record the model or the operator that ran these runs.</li>" +
      `<li>Location key <code title="${mathlib.location_key}">${mathlib.location_key.slice(0, 12)}</code>.</li></ul></details>`);
    expect(html).not.toContain("<details class=\"articles-prov\" open");
    expect(html).toContain(`<code class="articles-commit" title="commit ${mathlib.commit}">@${sha7}</code>`);
    // The relation is a badge beside the declaration, and the legend under Passages names each one.
    expect(html).toContain('<div class="articles-legend-rel"><p class="articles-muted">How each Lean declaration relates to its passage:</p><ul>' +
      '<li><span class="articles-rel" data-relation="exactMatch" title="States this exactly (exactMatch)">exact match</span> states the passage exactly</li>');
    expect(html).toContain('<span class="articles-rel" data-relation="relatedMatch" title="States something related (relatedMatch)">related</span> states something related but different; never counted</li>');
    expect(html).toContain('<code class="articles-decl"><span class="articles-ns">Widget.<wbr></span><b>exists_<wbr>spanning_<wbr>gadget</b></code>');
    expect(html).toContain(`href="https://github.com/leanprover-community/mathlib4/blob/${mathlib.commit}/Mathlib/Combinatorics/Widget/Basic.lean#L612-L640"`);
    expect(html).toContain(`Source @${sha7}`);
    // Proof status shown prominently when it is not "proved".
    expect(html).toContain('<span class="articles-ps is-alert" data-proof="statement_only"');
    expect(html).toContain("close match");
    expect(html).toContain("Counts gadgets from 1, where the article counts from 0.");
    // A lean end with no end_line (its index records only line): "line N", and
    // the source link names that one line.
    const ff = fixtureArticle().passages[0].locations[1];
    expect(ff.library).toBe("ff-widgets");
    expect(ff).not.toHaveProperty("end_line");
    expect(html).toContain('<p class="articles-module">WidgetTheory.Spanning · line 88</p>');
    expect(html).toContain(`href="https://github.com/FormalFrontier/WidgetTheory/blob/${ff.commit}/WidgetTheory/Spanning.lean#L88"`);
    expect(html).toContain(`(opens FormalFrontier · WidgetTheory at commit ${ff.commit.slice(0, 7)}, line 88)`);
    expect(html).toContain("<li>Re-checked in challenge run (unnamed), which downgraded it.</li>");
    expect(html).toContain('<p class="articles-module">Mathlib.Combinatorics.Widget.Basic · lines 612–640</p>');
    // An absence; no passage is labelled undecided, and no status chip offers it.
    expect(html).toContain(`Searched 5 libraries in universe <a href="/articles/universe/${FIXTURE_UNIVERSE_SHA}">u-fixture</a> (2026-10-17) — not found.`);
    expect(html).toContain("Related, but states something else");
    expect(html).not.toMatch(/Undecided|undecided|data-status="unresolved"/);
    // The absence's receipt and challenge are under its own Details fold.
    expect(html).toContain('<details class="articles-prov articles-absence-prov"><summary>Details</summary><ul class="articles-prov-list">' +
      "<li>Searched in campaign <code>fixture-2026-10</code>: the search receipt lists 4 queries and 2 candidates examined (receipt <code");
    // Quote, in context at the pinned revision.
    expect(html).toContain("<mark>every finite widget admits a spanning gadget</mark>");
    expect(html).toContain("https://en.wikipedia.org/w/index.php?title=Widget_lemma&amp;oldid=2002#:~:text=every%20finite%20widget");
    // Anonymous: no feedback buttons, a sign-in link back to this page.
    expect(html).not.toContain('class="articles-fb"');
    expect(html).toContain(`href="/login?returnTo=${encodeURIComponent(WIDGET_PATH)}"`);
    expect(html).not.toMatch(/\son[a-z]+=/i);
  });

  it("the bare page id serves the same page, and the WikiLean article is linked when one exists", async () => {
    const h = bound();
    h.db.prepare("UPDATE articles SET wikipedia_title = 'Widget lemma' WHERE slug = ?").run(SLUG);
    const html = await (await get(h.env, `/articles/${WIDGET_PAGEID}`)).text();
    expect(html).toContain(`<link rel="canonical" href="https://wikilean.jackmccarthy.org${WIDGET_PATH}">`);
    expect(html).toContain(`<a href="/${SLUG}">Annotated article on WikiLean</a>`);
  });

  it("a lead passage (section []) that is the article's subject renders without a section", async () => {
    const h = bound();
    const html = await (await get(h.env, `/articles/${GADGET_PAGEID}/Gadget_(mathematics)`)).text();
    expect(html).toContain('<span class="articles-eyebrow">Definition</span><span class="articles-sr"> · </span><span class="articles-subject">Subject of the article</span></h3>');
    const lead = html.slice(html.indexOf('id="p-7d2c9e10"'), html.indexOf('<li class="articles-passage', html.indexOf('id="p-7d2c9e10"')));
    expect(lead.length).toBeGreaterThan(0);
    expect(lead).not.toContain("articles-section");
    expect(html).toContain('<a href="#p-7d2c9e10">Definition: “a gadget is a widget together with a chosen basepoint”</a>');
    expect(html).toContain('<p class="articles-why">Formalized: Mathlib states this exactly (a definition).</p>');
    // Its prominence was overturned to subject after the challenge ran: challenge_pending,
    // which reads as unchallenged.
    expect(html).toContain("high confidence.</li><li>Not re-checked yet: a challenge is owed (the passage's prominence changed after the challenge ran).</li>");
    expect(html).not.toContain("challenge_pending");
    expect(html).toContain('aria-label="3 passages: 3 formalized"');
    expect(html).toContain("3 passages checked, 4 Lean locations.");
    // One passage is "1 passage", in the strip and in the bar's name.
    const files = articlesFixtureFiles();
    const article = JSON.parse(files.get(`articles/v1/articles/${GADGET_PAGEID}.json`)!);
    article.passages = article.passages.slice(0, 1);
    Object.assign(article.summary, { passages: 1, locations: 1 });
    article.summary.by_status.formalized = 1;
    files.set(`articles/v1/articles/${GADGET_PAGEID}.json`, JSON.stringify(article));
    const one = setup();
    bindArticles(one.env, files);
    const single = await (await get(one.env, `/articles/${GADGET_PAGEID}`)).text();
    expect(single).toContain('aria-label="1 passage: 1 formalized"');
    expect(single).toContain("1 passage checked, 1 Lean location.");
  });

  it("a partial status from an exact match says why: a definition passage needs a Lean definition", async () => {
    const h = setup();
    const files = articlesFixtureFiles();
    const article = JSON.parse(files.get(`articles/v1/articles/${GADGET_PAGEID}.json`)!);
    // The producer's table: a proved theorem stating a definition passage leaves it partial.
    Object.assign(article.passages[0].locations[0], { proof_status: "proved" });
    article.passages[0].status = "partial";
    files.set(`articles/v1/articles/${GADGET_PAGEID}.json`, JSON.stringify(article));
    bindArticles(h.env, files);
    const html = await (await get(h.env, `/articles/${GADGET_PAGEID}`)).text();
    expect(html).toContain('<p class="articles-why">Partial: Mathlib states this exactly, but as a proved theorem, not a definition; ' +
      "a definition passage is formalized only by a Lean definition.</p>");
    // A part relation needs no reason beyond its own words.
    expect(html).not.toMatch(/states part of this, but/);
  });

  it("a card's note is labelled by its relation, never 'Convention' for every one", async () => {
    const html = await (await get(bound().env, WIDGET_PATH)).text();
    expect(html).toContain('<p class="articles-note"><span class="articles-k">Why close, not exact:</span> Counts gadgets from 1, where the article counts from 0.</p>');
    expect(html).not.toContain("Convention:");
  });

  it("a quote's formula renders once, from its TeX, and the in-context link quotes its prose only", async () => {
    const h = setup();
    const files = articlesFixtureFiles();
    const article = JSON.parse(files.get(`articles/v1/articles/${WIDGET_PAGEID}.json`)!);
    // The layer holds a formula twice: its MathML tokens, then its TeX annotation.
    article.passages[0].quote = { exact: "every finite widget W {\\displaystyle W} admits a spanning gadget g : W → G {\\displaystyle g\\colon W\\to G}" };
    files.set(`articles/v1/articles/${WIDGET_PAGEID}.json`, JSON.stringify(article));
    bindArticles(h.env, files);
    const html = await (await get(h.env, WIDGET_PATH)).text();
    expect(html).toContain('<mark>every finite widget <span class="articles-math" title="TeX: W"><i>W</i></span> admits a spanning gadget ' +
      '<span class="articles-math" title="TeX: g\\colon W\\to G"><i>g</i>: <i>W</i> → <i>G</i></span></mark>');
    expect(html).not.toContain("displaystyle");
    expect(html).toContain("oldid=2002#:~:text=every%20finite%20widget,admits%20a%20spanning%20gadget\"");
  });

  it("the status sentence names the location that justifies the status, not rank 1", async () => {
    // Rank 1 (Mathlib) only states the theorem; rank 2 (FormalFrontier) proves
    // it, so the passage is formalized because of rank 2 (build_store basis).
    for (const withBasis of [true, false]) {
      const h = setup();
      const files = articlesFixtureFiles();
      const article = JSON.parse(files.get(`articles/v1/articles/${WIDGET_PAGEID}.json`)!);
      const passage = article.passages[0];
      passage.locations[0].proof_status = "statement_only";
      passage.basis = withBasis ? passage.locations[1].location_key : undefined;
      files.set(`articles/v1/articles/${WIDGET_PAGEID}.json`, JSON.stringify(article));
      bindArticles(h.env, files);
      const html = await (await get(h.env, WIDGET_PATH)).text();
      expect(html, `basis ${withBasis}`).toContain(
        '<p class="articles-why">Formalized: FormalFrontier · WidgetTheory states this up to a convention difference (proved).</p>');
      expect(html, `basis ${withBasis}`).not.toContain("Formalized: Mathlib");
    }
  });

  it("store numbers reach the HTML only as integers", async () => {
    const h = setup();
    const files = articlesFixtureFiles();
    const article = JSON.parse(files.get(`articles/v1/articles/${WIDGET_PAGEID}.json`)!);
    article.revision = "<svg onload=alert(2)>";
    article.pageid = "<i>p</i>";
    article.summary.passages = "<b>n</b>";
    article.summary.by_status.formalized = "<u>f</u>";
    article.universe.libraries = "<s>l</s>";
    article.passages[0].locations[0].rank = "<img src=x onerror=alert(1)>";
    article.passages[0].locations[0].line = "<q>1</q>";
    article.passages[2].absence.libraries = "<em>5</em>";
    article.passages[2].absence.receipt.queries = "<kbd>q</kbd>";
    files.set(`articles/v1/articles/${WIDGET_PAGEID}.json`, JSON.stringify(article));
    const current = JSON.parse(files.get("articles/v1/current.json")!);
    current.counts.by_status.formalized = "<script>alert(3)</script>";
    current.counts.by_library.mathlib.passages = "<sub>m</sub>";
    current.counts.articles = "<sup>a</sup>";
    current.universe.libraries = "<del>u</del>";
    files.set("articles/v1/current.json", JSON.stringify(current));
    bindArticles(h.env, files);
    const page = await (await get(h.env, `/articles/${WIDGET_PAGEID}`)).text();
    const dashboard = await (await get(h.env, "/articles")).text();
    for (const html of [page, dashboard]) {
      for (const tag of ["<svg", "<img", "<i>", "<b>n", "<u>", "<s>", "<q>", "<em>", "<kbd>", "<script>alert", "<sub>", "<sup>", "<del>"]) {
        expect(html).not.toContain(tag);
      }
    }
    expect(page).toContain(": candidate 0 for this passage,");
    expect(page).toContain('data-pageid="0"');
    // The fixture's one passage with no status is in no count (7 passages, 6 with a status).
    expect(dashboard).toContain("0 articles · 6 passages");
  });

  it("a universe list that is not an array never reaches the HTML and never breaks a page", async () => {
    const universePath = `articles/v1/universes/${FIXTURE_UNIVERSE_SHA}.json`;
    const cases: Array<[string, (u: Record<string, unknown>) => void]> = [
      ["excluded with a hostile length", (u) => { u.excluded = { length: "<img src=x onerror=alert(document.domain)>" }; }],
      ["no excluded key", (u) => { delete u.excluded; }],
      ["libraries with a hostile length", (u) => { u.libraries = { length: "<b>9</b>" }; }],
      ["non-object entries", (u) => { u.libraries = [null, 5, { id: "x" }]; u.excluded = [null, "x"]; }],
    ];
    for (const [label, edit] of cases) {
      const h = setup();
      const files = articlesFixtureFiles();
      const universe = JSON.parse(files.get(universePath)!);
      edit(universe);
      files.set(universePath, JSON.stringify(universe));
      bindArticles(h.env, files);
      const page = await get(h.env, `/articles/${WIDGET_PAGEID}`);
      const universePage = await get(h.env, `/articles/universe/${FIXTURE_UNIVERSE_SHA}`);
      expect(page.status, label).toBe(200);
      expect(universePage.status, label).toBe(200);
      for (const html of [await page.text(), await universePage.text()]) {
        expect(html, label).not.toContain("<img src=x");
        expect(html, label).not.toContain("<b>9</b>");
        // A hostile or missing exclusion list names no exclusions.
        if (label.startsWith("excluded") || label === "no excluded key") expect(html, label).not.toContain(" excluded");
      }
    }
    const h = bound();
    expect(await (await get(h.env, `/articles/${WIDGET_PAGEID}`)).text()).toContain("5 libraries, frozen 2026-10-17, 1 excluded");
  });

  it("a passage with no status is set aside, with its related declarations, until someone reads it", async () => {
    // build_store's reading of proposal §2.6 "(none)": records without a status, here the subject
    // theorem's two locations made related. Like the unresolved conjecture, it is set aside: no
    // block, no status, no count, no related declaration counted.
    const h = setup();
    const files = articlesFixtureFiles();
    const article = JSON.parse(files.get(`articles/v1/articles/${WIDGET_PAGEID}.json`)!);
    const passage = article.passages[0];
    Object.assign(passage, { outcome: "unresolved", status: null, basis: null, unresolved: { challenge: "downgraded" } });
    for (const loc of passage.locations) loc.relation = "relatedMatch";
    article.summary = { ...article.summary, locations: 1, related: 3,
      by_status: { formalized: 0, statement_only: 1, partial: 0, not_formalized: 1, unresolved: 2 } };
    article.summary.by_library = { ...article.summary.by_library,
      mathlib: { locations: 0, complete: 0, incomplete: 0, passages: 0, related: 1 },
      "ff-widgets": { locations: 0, complete: 0, incomplete: 0, passages: 0, related: 1 } };
    files.set(`articles/v1/articles/${WIDGET_PAGEID}.json`, JSON.stringify(article));
    bindArticles(h.env, files);
    for (const path of [WIDGET_PATH, `${WIDGET_PATH}?view=cards`]) {
      const html = await (await get(h.env, path)).text();
      expect(html, path).not.toContain('id="p-3fa91c07"');
      expect(html, path).not.toContain("exists_<wbr>spanning_<wbr>gadget");
      expect(html, path).not.toMatch(/Undecided|undecided|data-status="unresolved"/);
      expect(html, path).toContain("2 passages checked, 1 Lean location (and 1 related declaration). " +
        '<span class="articles-muted" id="articles-unread">2 more passages have no status yet; they are left out until someone reads them.</span>');
      expect(html, path).toContain('aria-label="2 passages: 1 statement only, 1 not formalized"');
      // A library whose only related declarations were the set-aside passage's has no chip.
      expect(html, path).not.toContain('data-filter="library:mathlib"');
      expect(html, path).not.toContain(">Mathlib <span class=\"articles-n\">related only</span>");
      expect(html, path).toContain("Batteries <span class=\"articles-n\">related only</span>");
    }
  });

  it("a survived coverage claim says 'Formalized by N parts together' and lists its parts (decision D1)", async () => {
    const h = bound();
    const html = await (await get(h.env, `/articles/${GADGET_PAGEID}/Gadget_(mathematics)`)).text();
    const at = html.indexOf('id="p-a41f6c2e"');
    expect(at).toBeGreaterThan(0);
    const block = html.slice(at, html.indexOf('id="p-c93b0d58"', at));
    expect(block).toContain('data-status="formalized"');
    // The claim is the basis, so its line is the status sentence, with the parts listed in its order.
    expect(block).toContain('<div class="articles-coverage" data-coverage-status="formalized"><p class="articles-why">Formalized by 2 parts together:</p><ol class="articles-coverage-parts">');
    expect(block.match(/class="articles-why"/g)?.length).toBe(1);
    expect(block.indexOf("Gadget.<wbr></span><b>connected</b>")).toBeLessThan(block.indexOf("<b>basepoint_<wbr>degree_<wbr>finite</b>"));
    expect(block).toContain("<span class=\"articles-muted\">(proved)</span></li>");
    // Each part is still a card that states part of it, and reads as checked: confirming the
    // claim confirms its parts (round three; build_store.confirmed_parts).
    expect(block.match(/<article class="articles-card"[^>]* data-relation="hasPart"/g)?.length).toBe(2);
    expect(block.match(/<li>Re-checked in challenge run \(unnamed\): it survived\.<\/li>/g)?.length).toBe(2);
    // The theorem an equivalence states is formalized by that one data declaration.
    expect(html).toContain('<p class="articles-why">Formalized: Mathlib states this exactly (a definition).</p>');
    expect(html.match(/parts together/g)?.length).toBe(1);
  });

  it("a coverage claim that did not survive shows no parts line and upgrades nothing", async () => {
    for (const challenge of ["challenge_pending", "not_required", "sampled_out", "downgraded"]) {
      const h = setup();
      const files = articlesFixtureFiles();
      const article = JSON.parse(files.get(`articles/v1/articles/${GADGET_PAGEID}.json`)!);
      const passage = article.passages[1];
      Object.assign(passage, { status: "partial", basis: passage.locations[0].location_key });
      Object.assign(passage.coverage, { challenge, status: null, basis: false });
      files.set(`articles/v1/articles/${GADGET_PAGEID}.json`, JSON.stringify(article));
      bindArticles(h.env, files);
      const html = await (await get(h.env, `/articles/${GADGET_PAGEID}`)).text();
      expect(html, challenge).not.toContain("parts together");
      expect(html, challenge).not.toContain("articles-coverage");
      expect(html, challenge).toContain('<p class="articles-why">Partial: Mathlib states part of this (proved).</p>');
    }
  });

  it("a survived claim beside a better or equal location follows the status sentence; a partial claim says so", async () => {
    const cases: Array<[string, (p: Record<string, any>) => void, string[]]> = [
      ["a location reaches the same status", (p) => {
        // A third location, a proved exact match: on a tie the one location is named.
        p.locations.push({ ...p.locations[1], relation: "exactMatch", rank: 3, location_key: "e".repeat(64) });
        Object.assign(p, { basis: "e".repeat(64) });
        p.coverage.basis = false;
      }, []],
      ["a statement-only claim", (p) => {
        p.locations[1].proof_status = "statement_only";
        Object.assign(p, { status: "statement_only", basis: null });
        Object.assign(p.coverage, { status: "statement_only", basis: true });
      }, ['<p class="articles-why">Stated by 2 parts together, not all of them finished:</p>']],
      // The builder gives every survived claim a status (round three); a store built
      // before that could leave one null, and the page still says what the parts are.
      ["a survived claim the store gives no status", (p) => {
        Object.assign(p, { status: "partial", basis: p.locations[0].location_key });
        Object.assign(p.coverage, { status: null, basis: false });
      }, ['<p class="articles-why">Partial: Mathlib states part of this (proved).</p>',
        '<p class="articles-why">Covered by 2 parts together, not all of which count toward the status:</p>']],
    ];
    for (const [label, edit, expected] of cases) {
      const h = setup();
      const files = articlesFixtureFiles();
      const article = JSON.parse(files.get(`articles/v1/articles/${GADGET_PAGEID}.json`)!);
      edit(article.passages[1]);
      files.set(`articles/v1/articles/${GADGET_PAGEID}.json`, JSON.stringify(article));
      bindArticles(h.env, files);
      const html = await (await get(h.env, `/articles/${GADGET_PAGEID}`)).text();
      for (const fragment of expected) expect(html, label).toContain(fragment);
      if (label === "a location reaches the same status") {
        // The exact match is named, and the claim's line still lists the parts.
        expect(html, label).toContain('<p class="articles-why">Formalized: Mathlib states this exactly (proved).</p>');
        expect(html, label).toContain('<p class="articles-why">Formalized by 2 parts together:</p>');
      }
    }
  });

  it("a malformed coverage claim never breaks the page or reaches the HTML", async () => {
    const claims: unknown[] = [
      "<b>x</b>", 5, [], { challenge: "survived", parts: "<i>p</i>", status: "formalized", basis: true },
      { challenge: "survived", parts: ["<u>k</u>", "<s>k</s>"], status: "formalized", basis: true },
      { challenge: "survived", parts: [], status: "formalized", basis: true },
    ];
    for (const claim of claims) {
      const h = setup();
      const files = articlesFixtureFiles();
      const article = JSON.parse(files.get(`articles/v1/articles/${GADGET_PAGEID}.json`)!);
      article.passages[1].coverage = claim;
      files.set(`articles/v1/articles/${GADGET_PAGEID}.json`, JSON.stringify(article));
      bindArticles(h.env, files);
      const res = await get(h.env, `/articles/${GADGET_PAGEID}`);
      expect(res.status).toBe(200);
      const html = await res.text();
      for (const tag of ["<b>x", "<i>p", "<u>k", "<s>k"]) expect(html).not.toContain(tag);
      expect(html).not.toContain("parts together");
      // Without a claim to name, the sentence falls back to the store's status alone.
      expect(html).toContain('<p class="articles-why">Formalized.</p>');
    }
  });

  it("a Prop-valued definition (the statement flag) is named as one", async () => {
    const h = setup();
    const files = articlesFixtureFiles();
    const article = JSON.parse(files.get(`articles/v1/articles/${WIDGET_PAGEID}.json`)!);
    const loc = article.passages[1].locations[0];
    Object.assign(loc, { proof_status: "definition", statement: true });
    article.passages[1].basis = undefined;  // the fallback reads the flag too
    files.set(`articles/v1/articles/${WIDGET_PAGEID}.json`, JSON.stringify(article));
    bindArticles(h.env, files);
    const html = await (await get(h.env, WIDGET_PATH)).text();
    expect(html).toContain('<p class="articles-why">Statement only: TauCeti states this exactly (a Prop-valued definition: it states the claim without proving it).</p>');
    expect(html).toContain(">Prop definition</span>");
  });

  it("unknown page ids are a 404 page; non-numeric ids fall through to the plain 404", async () => {
    const h = bound();
    const missing = await get(h.env, "/articles/9999");
    expect(missing.status).toBe(404);
    expect(await missing.text()).toContain("There is no formalization entry for Wikipedia page id 9999.");
    expect((await get(h.env, "/articles/0123")).status).toBe(404);
    expect((await get(h.env, "/articles/abc")).status).toBe(404);
    expect((await get(h.env, "/articles/universe/" + "a".repeat(64))).status).toBe(404);
  });

  it("the universe page lists libraries, commits and exclusions", async () => {
    const h = bound();
    const html = await (await get(h.env, `/articles/universe/${FIXTURE_UNIVERSE_SHA}`)).text();
    expect(html).toContain("Universe u-fixture");
    expect(html).toContain("<b>atlas-lean</b>: licence forbids ML evaluation use");
    expect(html).toContain("github.com/leanprover/lean4");
    expect(html).toContain('<div class="articles-table-wrap" role="region" tabindex="0" aria-label="Table: Libraries of universe u-fixture">');
    expect(html).toContain('<a href="https://github.com/leanprover-community/mathlib4" rel="noopener">github.com/leanprover-community/mathlib4</a>');
    expect(html).not.toContain('aria-current="page"');
  });

  it("the universe page links GitHub repositories only and names any other host as text", async () => {
    const h = setup();
    const files = articlesFixtureFiles();
    const universePath = `articles/v1/universes/${FIXTURE_UNIVERSE_SHA}.json`;
    const universe = JSON.parse(files.get(universePath)!);
    universe.libraries[4].repo = "https://evil.example.com/phish";
    files.set(universePath, JSON.stringify(universe));
    bindArticles(h.env, files);
    const html = await (await get(h.env, `/articles/universe/${FIXTURE_UNIVERSE_SHA}`)).text();
    expect(html).toContain("<td>https://evil.example.com/phish</td>");
    expect(html).not.toContain('href="https://evil.example.com');
    expect(html).toContain('<a href="https://github.com/leanprover/lean4" rel="noopener">');
  });

  it("libraries split from one repository are shown per library id, named apart (Lean core · Init, · Std)", async () => {
    // docs/ARTICLES.md section 3 and contract note 13: proposal §2.3 splits lean-core
    // into Init and Std at one lean4 commit. The store keys libraries by id; the
    // pages must not show two "Lean core"s.
    const LEAN4 = "https://github.com/leanprover/lean4";
    const universePath = `articles/v1/universes/${FIXTURE_UNIVERSE_SHA}.json`;
    const articlePath = `articles/v1/articles/${WIDGET_PAGEID}.json`;
    const files = articlesFixtureFiles();
    const universe = JSON.parse(files.get(universePath)!);
    const core = universe.libraries.find((lib: { id: string }) => lib.id === "lean-core");
    const { display_name: _unused, ...plain } = core;
    universe.libraries = [
      ...universe.libraries.filter((lib: { id: string }) => lib.id !== "lean-core"),
      { ...plain, id: "init", brain_lib: "Init", roots: ["Init"] },
      { ...plain, id: "std", brain_lib: "Std", roots: ["Std"] },
    ].sort((a, b) => (a.id < b.id ? -1 : 1));
    files.set(universePath, JSON.stringify(universe));
    // The TauCeti location moves into std, and Batteries' related declaration into init.
    const rename = (counts: Record<string, unknown>) => {
      const { tauceti, batteries, ...rest } = counts;
      return { ...rest, std: tauceti, init: batteries };
    };
    const article = JSON.parse(files.get(articlePath)!);
    Object.assign(article.passages[1].locations[0], {
      library: "std", repo: LEAN4, commit: core.commit, path: "src/Std/Data/Widget.lean", module: "Std.Data.Widget",
    });
    Object.assign(article.passages[2].locations[0], {
      library: "init", repo: LEAN4, commit: core.commit, path: "src/Init/Data/Widget.lean", module: "Init.Data.Widget",
    });
    article.summary.by_library = rename(article.summary.by_library);
    files.set(articlePath, JSON.stringify(article));
    const current = JSON.parse(files.get("articles/v1/current.json")!);
    current.counts.by_library = rename(current.counts.by_library);
    files.set("articles/v1/current.json", JSON.stringify(current));
    const shard = JSON.parse(files.get("articles/v1/index/1.json")!);
    shard.articles[0][shard.fields.indexOf("libraries")] = ["ff-widgets", "mathlib", "std"];
    files.set("articles/v1/index/1.json", JSON.stringify(shard));
    const h = setup();
    bindArticles(h.env, files);

    const universePage = await (await get(h.env, `/articles/universe/${FIXTURE_UNIVERSE_SHA}`)).text();
    expect(universePage).toContain('<th scope="row">Lean core · Init <span class="articles-muted">init</span></th>');
    expect(universePage).toContain('<th scope="row">Lean core · Std <span class="articles-muted">std</span></th>');
    expect(universePage).toContain(`<a href="${LEAN4}/tree/${core.commit}" rel="noopener"><code>${core.commit}</code></a>`);
    expect(universePage).toContain("6 libraries, frozen 2026-10-17");
    expect(universePage).not.toMatch(/>Lean core <span/);

    const page = await (await get(h.env, WIDGET_PATH)).text();
    const std = `<span class="articles-lib" title="Lean core · Std · ${LEAN4}">Lean core · Std</span>`;
    expect(page).toContain(std);
    expect(page).toContain(`<span class="articles-lib" title="Lean core · Init · ${LEAN4}">Lean core · Init</span>`);
    expect(page).toContain('data-filter="library:std"');
    expect(page).toContain("Lean core · Init <span class=\"articles-n\">related only</span>");
    expect(page).toContain("Statement only: Lean core · Std states this exactly");
    expect(page).toContain(`href="${LEAN4}/blob/${core.commit}/src/Std/Data/Widget.lean#L41-L57"`);
    expect(page).not.toMatch(/>Lean core</);

    const dashboard = await (await get(h.env, "/articles")).text();
    expect(dashboard).toContain('<th scope="row">Lean core · Std</th>');
    expect(dashboard).toContain('<th scope="row">Lean core · Init</th>');
    expect(dashboard).toContain("&quot;init&quot;:&quot;Lean core · Init&quot;");
    expect(dashboard).toContain("&quot;std&quot;:&quot;Lean core · Std&quot;");
    expect(dashboard).toContain(`<span class="articles-libtag" title="Lean core · Std · ${LEAN4}">Lean core · Std</span>`);
    expect(dashboard).not.toMatch(/>Lean core</);
  });

  it("signed-in viewers get Confirm / Report controls showing their own verdict", async () => {
    const h = bound();
    expect((await post(h.env, "/api/articles/feedback", feedback({ verdict: "report" }), { user: "u-human" })).status).toBe(200);
    const html = await (await get(h.env, WIDGET_PATH, { user: "u-human" })).text();
    expect(html).toContain(`<div class="articles-fb" data-location-key="${MATHLIB_KEY}">`);
    expect(html).toContain('data-verdict="report" aria-pressed="true"');
    expect(html).toContain("You reported this link.");
    expect(html).toContain(`data-campaign="${FIXTURE_CAMPAIGN}"`);
    // The pipeline bearer is not a person: no controls.
    const bot = await (await get(h.env, WIDGET_PATH, { bearer: PIPELINE_TOKEN })).text();
    expect(bot).not.toContain('class="articles-fb"');
  });

  it("escapes hostile store text and drops non-https links", async () => {
    const h = setup();
    const files = articlesFixtureFiles();
    const article = JSON.parse(files.get(`articles/v1/articles/${WIDGET_PAGEID}.json`)!);
    article.title = '<script>alert("t")</script>';
    article.passages[0].quote.exact = '<img src=x onerror="alert(1)">';
    article.passages[0].locations[0].declaration = 'Evil.<b onmouseover="x">';
    article.passages[0].locations[0].path = "../../etc/passwd";
    article.passages[0].subject_uri = "javascript:alert(1)";
    files.set(`articles/v1/articles/${WIDGET_PAGEID}.json`, JSON.stringify(article));
    bindArticles(h.env, files);
    const html = await (await get(h.env, `/articles/${WIDGET_PAGEID}`)).text();
    expect(html).not.toContain("<script>alert");
    expect(html).not.toContain("<img src=x");
    expect(html).not.toContain('<b onmouseover');
    expect(html).toContain("&lt;script&gt;alert(&quot;t&quot;)&lt;/script&gt;");
    expect(html).not.toContain("javascript:");
    expect(html).not.toContain("etc/passwd\"");
    expect(html).toContain("No source link");
  });

  // The export quotes at most a passage's first 12 words (build_store.QUOTE_MAX_WORDS), and
  // build_store.quote_capped tells such a head from a whole passage by the record's span_sha256.
  const CUT = '<span class="articles-affix" data-quote-cut=""><span aria-hidden="true">&#xa0;…</span>' +
    '<span class="articles-sr"> (the passage continues)</span></span>';

  it("a quote that is the capped head of a longer passage ends in an ellipsis outside the quotation", async () => {
    const h = bound();
    const widget = await (await get(h.env, WIDGET_PATH)).text();
    // The bound goes on past the cap: its quote stays verbatim, the ellipsis follows the
    // highlight, and a screen reader hears that the passage continues.
    expect(widget).toContain('<blockquote class="articles-quote" data-quote="head">' +
      `<mark>Moreover, the number of spanning gadgets of a widget is at most</mark>${CUT}</blockquote>`);
    expect(widget.match(/data-quote-cut=""/g)).toHaveLength(1);
    // Two whole quotes: the conjecture, the third, has no status and is set aside.
    expect(widget.match(/data-quote="whole"/g)).toHaveLength(2);
    expect(widget).toContain('<h2 class="articles-h2" id="articles-h-passages">Passages</h2>\n<p class="articles-muted" id="articles-quote-note">' +
      "Quotes show at most the first 12 words of a passage (the record format's quotation cap; a formula's MathML text counts toward it). " +
      "A … after the highlighted quote means the passage continues; “Read it in context on Wikipedia” shows the rest. Hover a formula for its TeX.</p>");
    // A passage of exactly 12 words is quoted whole: at the cap, but no ellipsis and no note.
    const gadget = await (await get(h.env, `/articles/${GADGET_PAGEID}`)).text();
    expect(gadget).toContain('<blockquote class="articles-quote" data-quote="whole">' +
      "<mark>Moreover, the gadgets on a widget correspond bijectively to all its vertices.</mark></blockquote>");
    expect(gadget).not.toContain("data-quote-cut");
    expect(gadget).not.toContain("articles-quote-note");
  });

  it("the Subject row marks a capped quote after its closing quotation mark; without the flag it counts words", async () => {
    const twelve = "one two three four five six seven eight nine ten eleven twelve";
    const cases: Array<[string, (p: Record<string, any>) => void, boolean]> = [
      ["flagged as a head", (p) => { p.quote_capped = true; }, true],
      ["flagged as whole, at the cap", (p) => { p.quote_capped = false; p.quote.exact = twelve; }, false],
      // A store built before the flag: a quote at the cap reads as a head, one under it as whole.
      ["no flag, under the cap", (p) => { delete p.quote_capped; }, false],
      ["no flag, at the cap", (p) => { delete p.quote_capped; p.quote.exact = twelve; }, true],
      ["a flag that is not a boolean", (p) => { p.quote_capped = "<b>yes</b>"; }, false],
    ];
    for (const [label, edit, capped] of cases) {
      const h = setup();
      const files = articlesFixtureFiles();
      const article = JSON.parse(files.get(`articles/v1/articles/${WIDGET_PAGEID}.json`)!);
      edit(article.passages[0]);
      files.set(`articles/v1/articles/${WIDGET_PAGEID}.json`, JSON.stringify(article));
      bindArticles(h.env, files);
      const html = await (await get(h.env, WIDGET_PATH)).text();
      const start = html.indexOf('<ul class="articles-subject-list"');
      const row = html.slice(start, html.indexOf("</ul>", start));
      const quoted = `Theorem: “${article.passages[0].quote.exact}”`;
      expect(row, label).toContain(`<a href="#p-3fa91c07">${quoted}${capped ? CUT : ""}</a>`);
      // The card: a suffix keeps its own words, and a head's one ellipsis comes after it.
      const block = html.slice(html.indexOf('id="p-3fa91c07"'), html.indexOf('<li class="articles-passage', html.indexOf('id="p-3fa91c07"')));
      expect(block, label).toContain(`<span class="articles-affix"> of minimal degree.</span>${capped ? CUT : ""}</blockquote>`);
      expect(html, label).not.toContain("<b>yes");
    }
    // An ellipsis after the quote means only that the passage continues: a suffix is context
    // and never trails off by its own, even mid-sentence. A head whose passage ends inside its
    // suffix (the export widens a suffix from the page, past the passage) gets no ellipsis on
    // its card, which shows that end, yet keeps it in the Subject row, which shows the quote alone.
    const cardCases: Array<[string, Record<string, unknown>, boolean, boolean]> = [
      ["whole", { quote_capped: false }, false, false],
      ["head", { quote_capped: true }, true, true],
      ["head ending in its suffix", { quote_capped: true, passage_ends_in_suffix: true }, false, true],
      // The flag only counts for a head with a suffix, and only as a boolean.
      ["head, the flag not a boolean", { quote_capped: true, passage_ends_in_suffix: "yes" }, true, true],
      ["whole, the flag set", { quote_capped: false, passage_ends_in_suffix: true }, false, false],
    ];
    for (const [label, flags, cardCut, rowCut] of cardCases) {
      const h = setup();
      const files = articlesFixtureFiles();
      const article = JSON.parse(files.get(`articles/v1/articles/${WIDGET_PAGEID}.json`)!);
      Object.assign(article.passages[0], flags);
      article.passages[0].quote.suffix = " of minimal";
      files.set(`articles/v1/articles/${WIDGET_PAGEID}.json`, JSON.stringify(article));
      bindArticles(h.env, files);
      const html = await (await get(h.env, WIDGET_PATH)).text();
      expect(html, label).toContain(`<span class="articles-affix"> of minimal</span>${cardCut ? CUT : ""}</blockquote>`);
      expect(html, label).toContain(`spanning gadget”${rowCut ? CUT : ""}</a>`);
      expect(html, label).not.toContain("minimal…");
    }
    // Without a suffix there is nothing for the passage to end in: a head is cut on its card.
    {
      const h = setup();
      const files = articlesFixtureFiles();
      const article = JSON.parse(files.get(`articles/v1/articles/${WIDGET_PAGEID}.json`)!);
      Object.assign(article.passages[0], { quote_capped: true, passage_ends_in_suffix: true });
      delete article.passages[0].quote.suffix;
      files.set(`articles/v1/articles/${WIDGET_PAGEID}.json`, JSON.stringify(article));
      bindArticles(h.env, files);
      const html = await (await get(h.env, WIDGET_PATH)).text();
      expect(html).toContain(`spanning gadget</mark>${CUT}</blockquote>`);
    }
  });

  it("the Coverage box counts the central statements, the subject and central passages, by status", async () => {
    const h = bound();
    const widget = await (await get(h.env, WIDGET_PATH)).text();
    // Next to the bar over all the passages it shows (the conjecture, with no status, is set aside), which stays.
    expect(widget).toContain('<div class="articles-bar" role="img" aria-label="3 passages: 1 formalized, 1 statement only, 1 not formalized">');
    // The bar is followed by its own legend (the Subject row, then the Status chips),
    // and only then by the line, so the first figures under the bar are the bar's.
    expect(widget).toContain('title="1 not formalized"></span></div>\n' +
      '<div class="articles-strip-row"><span class="articles-strip-h" id="articles-h-subject">Subject</span>');
    expect(widget).toContain('<div class="articles-chips" role="group" aria-labelledby="articles-h-status">');
    expect(widget).toMatch(/aria-labelledby="articles-h-status">.*?<\/div><\/div>\n<p class="articles-strip-lead" id="articles-central">.*?<\/p>\n<div class="articles-strip-row"><span class="articles-strip-h" id="articles-h-where">/);
    expect(widget).toContain('<p class="articles-strip-lead" id="articles-central"><b>Central statements:</b> 2 of the 3 passages ' +
      "state the article's subject or a named result or definition; the other one is supporting (examples, setup). Of these 2: " +
      '<span class="articles-mini" data-status="formalized"><span class="articles-ico" aria-hidden="true">✓</span>1 formalized</span><span>,</span> ' +
      '<span class="articles-mini" data-status="not_formalized"><span class="articles-ico" aria-hidden="true">✕</span>1 not formalized</span>.</p>\n');
    const gadget = await (await get(h.env, `/articles/${GADGET_PAGEID}`)).text();
    expect(gadget).toContain("<b>Central statements:</b> 2 of the 3 passages state the article's subject or a named result or definition; " +
      "the other one is supporting (examples, setup). Of these 2: " +
      '<span class="articles-mini" data-status="formalized"><span class="articles-ico" aria-hidden="true">✓</span>2 formalized</span>.</p>');
    // From the prominence of the passages shown (the fourth, with no status, is set aside):
    // every count and its grammar; a store whose passages carry no prominence (built before it
    // was required) gets no line.
    const lineCases: Array<[string, Array<string | null>, string]> = [
      ["one central", ["subject", "supporting", "supporting", "supporting"],
        "1 of the 3 passages states the article's subject or a named result or definition; the other 2 are supporting (examples, setup). Of this one: "],
      ["all central", ["subject", "central", "central", "subject"], "All 3 passages state the article's subject or a named result or definition. Of these 3: "],
      // A passage without a prominence is neither: the supporting count says only what it is.
      ["one without", ["subject", null, "supporting", "supporting"],
        "1 of the 3 passages states the article's subject or a named result or definition; 1 is supporting (examples, setup). Of this one: "],
    ];
    for (const [label, prominences, expected] of lineCases) {
      const one = setup();
      const files = articlesFixtureFiles();
      const article = JSON.parse(files.get(`articles/v1/articles/${WIDGET_PAGEID}.json`)!);
      article.passages.forEach((passage: Record<string, unknown>, i: number) => { passage.prominence = prominences[i]; });
      files.set(`articles/v1/articles/${WIDGET_PAGEID}.json`, JSON.stringify(article));
      bindArticles(one.env, files);
      const html = await (await get(one.env, WIDGET_PATH)).text();
      expect(html, label).toContain(`<b>Central statements:</b> ${expected}`);
    }
    for (const [label, prominence, expected] of [["all supporting", "supporting",
      "<b>Central statements:</b> No passage states the article's subject or a named result or definition.</p>"], ["no prominence", null, null]] as const) {
      const one = setup();
      const files = articlesFixtureFiles();
      const article = JSON.parse(files.get(`articles/v1/articles/${WIDGET_PAGEID}.json`)!);
      for (const passage of article.passages) passage.prominence = prominence;
      files.set(`articles/v1/articles/${WIDGET_PAGEID}.json`, JSON.stringify(article));
      bindArticles(one.env, files);
      const html = await (await get(one.env, WIDGET_PATH)).text();
      if (expected) expect(html, label).toContain(expected);
      else expect(html, label).not.toContain("Central statements");
      expect(html, label).toContain('aria-label="3 passages: ');
    }
  });

  it("the dashboard's central statements come from the store's counts by prominence", async () => {
    const cases: Array<[string, (counts: Record<string, any>) => void, string | null]> = [
      // A store built before by_prominence: no line.
      ["absent", (counts) => { delete counts.by_prominence; }, null],
      // No supporting bucket to count: the line leaves the rest unsaid.
      ["hostile", (counts) => {
        counts.by_prominence = { subject: { by_status: { formalized: "<b>9</b>", partial: 2 } }, central: "<i>x</i>" };
      }, "2 of the 6 passages, across 2 articles, state an article's subject or a named result or definition. Of these 2: " +
        '<span class="articles-mini" data-status="partial"><span class="articles-ico" aria-hidden="true">◔</span>2 partial</span>.</p>'],
      ["no central passages", (counts) => {
        for (const bucket of Object.values(counts.by_prominence) as Array<Record<string, any>>) {
          if (bucket !== counts.by_prominence.supporting) bucket.by_status = {};
        }
      }, "No passage states an article's subject or a named result or definition.</p>"],
      // No passages at all: no line.
      ["no passages", (counts) => { counts.passages = 0; }, null],
    ];
    for (const [label, edit, expected] of cases) {
      const h = setup();
      const files = articlesFixtureFiles();
      const current = JSON.parse(files.get("articles/v1/current.json")!);
      edit(current.counts);
      files.set("articles/v1/current.json", JSON.stringify(current));
      bindArticles(h.env, files);
      const html = await (await get(h.env, "/articles")).text();
      if (expected) expect(html, label).toContain(expected);
      else expect(html, label).not.toContain("Central statements");
      expect(html, label).not.toContain("<b>9");
      expect(html, label).not.toContain("<i>x");
    }
  });

  it("the by-library table sorts by passages, then complete locations, then name, and lists empty libraries apart", async () => {
    // The library rows (the importance rows' first cell is a number, the article rows carry data-title).
    const rowOrder = (html: string) => [...html.matchAll(/<tr><th scope="row">([^<]+)<\/th><td>/g)].map((m) => m[1]);
    const cases: Array<[string, (byLibrary: Record<string, any>) => void, string[], string | null]> = [
      // TauCeti now has more complete locations than FormalFrontier, at one passage each.
      ["complete breaks a tie", (b) => { b.tauceti.complete = 3; }, ["Mathlib", "TauCeti", "FormalFrontier · WidgetTheory", "Batteries"],
        "1 more library searched"],
      // Equal passages and complete locations: by name.
      ["name breaks a tie", (b) => { b.tauceti.complete = 1; }, ["Mathlib", "FormalFrontier · WidgetTheory", "TauCeti", "Batteries"],
        "1 more library searched"],
      // A library with only a related declaration keeps its row; one with nothing joins the list.
      ["related keeps a row", (b) => { b["lean-core"] = { articles: 0, passages: 0, locations: 0, complete: 0, incomplete: 0, related: 2 }; delete b.batteries; },
        ["Mathlib", "FormalFrontier · WidgetTheory", "TauCeti", "Lean core"], "1 more library searched"],
      // Nothing held anywhere: no empty table, every library named below.
      ["nothing held", (b) => { for (const key of Object.keys(b)) delete b[key]; }, [], "5 libraries searched, with no locations in this build"],
    ];
    for (const [label, edit, order, summary] of cases) {
      const h = setup();
      const files = articlesFixtureFiles();
      const current = JSON.parse(files.get("articles/v1/current.json")!);
      edit(current.counts.by_library);
      files.set("articles/v1/current.json", JSON.stringify(current));
      bindArticles(h.env, files);
      const html = await (await get(h.env, "/articles")).text();
      expect(rowOrder(html), label).toEqual(order);
      if (summary) expect(html, label).toContain(`<summary>${summary}`);
      if (!order.length) {
        expect(html, label).not.toContain('aria-label="Table: By library"');
        expect(html, label).toContain("<p>No library holds a location or a related declaration in this build.</p>");
        // Listed by name, each with its commit.
        const names = [...html.matchAll(/<li>([^<]+) <a href="https:\/\/github\.com\/[^"]+\/tree\/[0-9a-f]{40}" rel="noopener"><code class="articles-sha">@[0-9a-f]{7}<\/code><\/a><\/li>/g)].map((m) => m[1]);
        expect(names, label).toEqual(["Batteries", "FormalFrontier · WidgetTheory", "Lean core", "Mathlib", "TauCeti"]);
      }
    }
  });

  it("without the universe object the by-library rows still come from the counts", async () => {
    // The route passes no universe when its object cannot be read (in the publish window after
    // old objects are deleted, say): the rows are the counts' libraries under their ids, with no
    // commit, and the page never says that no library holds anything.
    const h = setup();
    const files = articlesFixtureFiles();
    const current = JSON.parse(files.get("articles/v1/current.json")!);
    files.delete(`articles/v1/universes/${current.universe.sha256}.json`);
    bindArticles(h.env, files);
    const html = await (await get(h.env, "/articles")).text();
    const rows = [...html.matchAll(/<tr><th scope="row">([^<]+)<\/th><td><\/td><td class="num">/g)].map((m) => m[1]);
    expect(rows).toEqual(["mathlib", "ff-widgets", "tauceti", "batteries"]);
    expect(html).toMatch(/<th scope="row">mathlib<\/th><td><\/td><td class="num">2<\/td><td class="num">4<\/td><td class="num">5<\/td><td class="num">0<\/td><td class="num">0<\/td><\/tr>/);
    expect(html).not.toContain("No library holds a location");
    // No universe names the libraries that hold nothing, so there is no list of them.
    expect(html).not.toContain("articles-libs-empty");
  });
});

describe("articles feedback", () => {
  it("appends one row per verdict, and the newest is the person's verdict", async () => {
    const h = bound();
    const first = await post(h.env, "/api/articles/feedback", feedback(), { user: "u-human" });
    expect(first.status).toBe(200);
    expect(await jsonOf(first)).toEqual({ ok: true, verdict: "confirm" });
    const second = await post(h.env, "/api/articles/feedback", feedback({ verdict: "report", note: "  wrong lemma  " }), { user: "u-human" });
    expect(second.status).toBe(200);
    const rows = feedbackRows(h);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toMatchObject({ verdict: "confirm", note: null, user_id: "u-human" });
    expect(rows[1]).toMatchObject({
      location_key: MATHLIB_KEY, pageid: WIDGET_PAGEID, revision: 2002, campaign: FIXTURE_CAMPAIGN,
      verdict: "report", note: "wrong lemma", user_id: "u-human",
    });
    expect(rows[1].created_at as number).toBeGreaterThanOrEqual(rows[0].created_at as number);
    expect(rows[1]).not.toHaveProperty("updated_at");
    await post(h.env, "/api/articles/feedback", feedback(), { user: "u-patroller" });
    expect(feedbackRows(h)).toHaveLength(3);
    const latest = h.db.prepare("SELECT user_id, verdict FROM atlas_feedback_latest ORDER BY user_id").all();
    expect(latest).toEqual([{ user_id: "u-human", verdict: "report" }, { user_id: "u-patroller", verdict: "confirm" }]);
  });

  it("copies the record set, the relation and the pair key from the store at write time, never from the client", async () => {
    const h = setup();
    const files = articlesFixtureFiles();
    const article = JSON.parse(files.get(`articles/v1/articles/${WIDGET_PAGEID}.json`)!);
    const location = article.passages[0].locations[0];
    bindArticles(h.env, files);
    await post(h.env, "/api/articles/feedback", feedback({ record_set: "c".repeat(64), relation: "hasPart", pair_key: "d".repeat(64) }), { user: "u-human" });
    expect(article.record_set).toMatch(/^[0-9a-f]{64}$/);
    expect(feedbackRows(h)[0]).toMatchObject({
      record_set: article.record_set, relation: location.relation, pair_key: null, provenance: "human",
    });

    // A store that carries the producer's pair key: the row keeps it.
    location.pair_key = "e".repeat(64);
    files.set(`articles/v1/articles/${WIDGET_PAGEID}.json`, JSON.stringify(article));
    bindArticles(h.env, files);
    await post(h.env, "/api/articles/feedback", feedback(), { user: "u-human" });
    expect(feedbackRows(h)[1]).toMatchObject({ pair_key: "e".repeat(64), relation: location.relation });
  });

  it("a withdrawal is a verdict: nothing shows as pressed, and the earlier row stays", async () => {
    const h = bound();
    await post(h.env, "/api/articles/feedback", feedback({ verdict: "report", note: "wrong lemma" }), { user: "u-human" });
    const res = await post(h.env, "/api/articles/feedback", feedback({ verdict: "withdraw" }), { user: "u-human" });
    expect(res.status).toBe(200);
    expect(await jsonOf(res)).toEqual({ ok: true, verdict: "withdraw" });
    expect(feedbackRows(h).map((row) => row.verdict)).toEqual(["report", "withdraw"]);
    expect(feedbackRows(h)[0].note).toBe("wrong lemma");
    const box = feedbackBox(await (await get(h.env, WIDGET_PATH, { user: "u-human" })).text());
    expect(box).toContain('data-verdict="confirm" aria-pressed="false"');
    expect(box).toContain('data-verdict="report" aria-pressed="false"');
    expect(box).not.toContain("this link.");
    // A later verdict counts again.
    await post(h.env, "/api/articles/feedback", feedback(), { user: "u-human" });
    expect(feedbackBox(await (await get(h.env, WIDGET_PATH, { user: "u-human" })).text()))
      .toContain('data-verdict="confirm" aria-pressed="true"');
  });

  // docs/ARTICLES.md section 10: a verdict belongs to the campaign the person saw.
  it("a verdict from one campaign is not pressed in the next", async () => {
    const h = bound();
    await post(h.env, "/api/articles/feedback", feedback({ verdict: "report", note: "wrong lemma" }), { user: "u-human" });
    const before = feedbackBox(await (await get(h.env, WIDGET_PATH, { user: "u-human" })).text());
    expect(before).toContain('data-verdict="report" aria-pressed="true"');
    expect(before).toContain("You reported this link.");

    bindArticles(h.env, nextCampaignStore());
    const after = feedbackBox(await (await get(h.env, WIDGET_PATH, { user: "u-human" })).text());
    expect(after).toContain('data-verdict="report" aria-pressed="false"');
    expect(after).toContain('data-verdict="confirm" aria-pressed="false"');
    expect(after).not.toContain("You reported this link.");
  });

  it("the next campaign's verdict does not overwrite the old campaign's row", async () => {
    const h = bound();
    await post(h.env, "/api/articles/feedback", feedback({ verdict: "report", note: "wrong lemma" }), { user: "u-human" });
    const [old] = feedbackRows(h);

    bindArticles(h.env, nextCampaignStore());
    const next = await post(h.env, "/api/articles/feedback", feedback({ campaign: NEXT_CAMPAIGN }), { user: "u-human" });
    expect(next.status).toBe(200);
    const rows = feedbackRows(h);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toEqual(old);
    expect(rows[0]).toMatchObject({ campaign: FIXTURE_CAMPAIGN, verdict: "report", note: "wrong lemma" });
    expect(rows[1]).toMatchObject({ campaign: NEXT_CAMPAIGN, record_set: NEXT_RECORD_SET, verdict: "confirm", note: null });
    expect(feedbackBox(await (await get(h.env, WIDGET_PATH, { user: "u-human" })).text()))
      .toContain('data-verdict="confirm" aria-pressed="true"');

    // Each campaign keeps its own current verdict.
    bindArticles(h.env);
    const back = feedbackBox(await (await get(h.env, WIDGET_PATH, { user: "u-human" })).text());
    expect(back).toContain('data-verdict="report" aria-pressed="true"');
    expect(back).toContain('data-verdict="confirm" aria-pressed="false"');
  });

  it("refuses anonymous, bearer and cross-origin writes", async () => {
    const h = bound();
    expect((await post(h.env, "/api/articles/feedback", feedback())).status).toBe(401);
    expect((await post(h.env, "/api/articles/feedback", feedback(), { user: "u-blocked" })).status).toBe(401);
    const bot = await post(h.env, "/api/articles/feedback", feedback(), { bearer: PIPELINE_TOKEN, origin: null });
    expect(bot.status).toBe(403);
    expect((await jsonOf(bot)).error).toBe("session required (no bots)");
    const cross = await post(h.env, "/api/articles/feedback", feedback(), { user: "u-human", origin: "https://evil.example" });
    expect(cross.status).toBe(403);
    expect((await jsonOf(cross)).error).toBe("cross-origin request rejected");
    expect(feedbackRows(h)).toEqual([]);
  });

  it("validates the body", async () => {
    const h = bound();
    const cases: Array<[Record<string, unknown>, number, string]> = [
      [feedback({ verdict: "maybe" }), 400, "bad verdict"],
      [feedback({ verdict: undefined }), 400, "bad verdict"],
      [feedback({ location_key: "abc" }), 400, "bad location_key"],
      [feedback({ pageid: "1001" }), 400, "bad pageid"],
      [feedback({ campaign: "<x>" }), 400, "bad campaign"],
      [feedback({ note: 7 }), 400, "note must be a string of at most 500 characters"],
      [feedback({ note: "x".repeat(501) }), 400, "note must be a string of at most 500 characters"],
      [feedback({ pageid: 9999 }), 404, "unknown pageid"],
      [feedback({ location_key: "0".repeat(64) }), 404, "unknown location_key"],
      [feedback({ campaign: "older-campaign" }), 409, "stale: these links changed since this page loaded"],
    ];
    for (const [body, status, error] of cases) {
      const res = await post(h.env, "/api/articles/feedback", body, { user: "u-human" });
      expect(res.status, JSON.stringify(body)).toBe(status);
      expect((await jsonOf(res)).error, JSON.stringify(body)).toBe(error);
    }
    const bad = await app.request("/api/articles/feedback", {
      method: "POST", headers: { "Content-Type": "application/json", Cookie: "wl_dev_user=u-human", Origin: ORIGIN }, body: "{not json",
    }, h.env);
    expect(bad.status).toBe(400);
    const array = await post(h.env, "/api/articles/feedback", [feedback()], { user: "u-human" });
    expect(array.status).toBe(400);
    expect(feedbackRows(h)).toEqual([]);
  });

  // The shared JSON body cap (security.ts readJsonCapped), with the route's
  // own MAX_JSON_BODY_BYTES entry, read after the origin, auth and rate-limit
  // checks.
  it("caps the body at MAX_JSON_BODY_BYTES.articlesFeedback before parsing", async () => {
    const h = bound();
    const max = MAX_JSON_BODY_BYTES.articlesFeedback;
    expect(max).toBe(4096);
    const tooLarge = { ok: false, error: `request body too large (max ${max} bytes)` };
    const send = (body: string, headers: Record<string, string> = {}) => app.request("/api/articles/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json", Cookie: "wl_dev_user=u-human", Origin: ORIGIN, ...headers },
      body,
    }, h.env);
    // A JSON object of exactly `size` bytes: a valid verdict plus an ignored pad.
    const padded = (size: number) => {
      const head = JSON.stringify({ ...feedback(), pad: "" });
      const text = head.slice(0, -2) + "x".repeat(size - head.length) + '"}';
      expect(new TextEncoder().encode(text).byteLength).toBe(size);
      return text;
    };

    const res = await post(h.env, "/api/articles/feedback", feedback({ note: "x".repeat(5000) }), { user: "u-human" });
    expect(res.status).toBe(413);
    expect(await jsonOf(res)).toEqual(tooLarge);
    const declared = await send(JSON.stringify(feedback()), { "Content-Length": "999999" });
    expect(declared.status).toBe(413);
    expect(await jsonOf(declared)).toEqual(tooLarge);
    expect(feedbackRows(h)).toEqual([]);

    expect((await send(padded(max + 1))).status).toBe(413);
    expect(feedbackRows(h)).toEqual([]);
    const atCap = await send(padded(max));
    expect(atCap.status).toBe(200);
    expect(await jsonOf(atCap)).toEqual({ ok: true, verdict: "confirm" });
    expect(feedbackRows(h)).toHaveLength(1);

    // An oversized body from a caller the route refuses is answered as refused
    // (401 / 403), never as too large.
    const anonymous = await app.request("/api/articles/feedback", {
      method: "POST", headers: { "Content-Type": "application/json", "Content-Length": "999999" }, body: "{}",
    }, h.env);
    expect(anonymous.status).toBe(401);
    expect((await send(padded(max + 1), { Origin: "https://evil.example" })).status).toBe(403);
  });

  it("is rate limited per user", async () => {
    const h = bound({ limiterAllows: false });
    const res = await post(h.env, "/api/articles/feedback", feedback(), { user: "u-human" });
    expect(res.status).toBe(429);
    expect(feedbackRows(h)).toEqual([]);
  });

  it("the table holds only human labels and only the three verdicts", () => {
    const h = bound();
    const insert = (verdict: string, provenance: string) => h.db.prepare(
      `INSERT INTO atlas_feedback (location_key, pageid, revision, campaign, record_set, relation, verdict, provenance, user_id, created_at)
       VALUES (?, 1, 1, 'c', ?, 'exactMatch', ?, ?, 'u-human', 1)`,
    ).run(MATHLIB_KEY, "a".repeat(64), verdict, provenance);
    expect(() => insert("confirm", "ai")).toThrow(/CHECK constraint failed/);
    expect(() => insert("maybe", "human")).toThrow(/CHECK constraint failed/);
    for (const verdict of ["confirm", "report", "withdraw"]) insert(verdict, "human");
    expect(feedbackRows(h)).toHaveLength(3);
  });

  it("answers 503 (not 500) while migration 0015 is not applied", async () => {
    const h = bound();
    h.db.exec("DROP VIEW atlas_feedback_latest; DROP TABLE atlas_feedback");
    const res = await post(h.env, "/api/articles/feedback", feedback(), { user: "u-human" });
    expect(res.status).toBe(503);
    expect((await jsonOf(res)).error).toBe("articles feedback not configured (migration 0015)");
    // The coverage page still renders for a signed-in viewer.
    expect((await get(h.env, WIDGET_PATH, { user: "u-human" })).status).toBe(200);
    expect((await get(h.env, "/api/articles/feedback/export", { bearer: PIPELINE_TOKEN })).status).toBe(503);
  });
});

// How the file behaves when an operator applies it (docs/ARTICLES.md section 10). It
// is applied by hand, so this is what stands between a database that still holds
// the superseded table and a half-migrated one.
describe("migration 0015", () => {
  const MIGRATION = readFileSync(new URL("../migrations/0015_atlas_feedback.sql", import.meta.url), "utf8");
  // The table shape this file replaced, as a database that applied it holds it.
  const SUPERSEDED = `
    CREATE TABLE atlas_feedback (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      location_key TEXT NOT NULL CHECK (length(location_key) = 64),
      pageid INTEGER NOT NULL,
      revision INTEGER NOT NULL,
      campaign TEXT NOT NULL,
      verdict TEXT NOT NULL CHECK (verdict IN ('confirm', 'report')),
      note TEXT CHECK (note IS NULL OR length(note) <= 500),
      user_id TEXT NOT NULL REFERENCES users(id),
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL,
      UNIQUE (location_key, user_id)
    );
    CREATE INDEX idx_atlas_feedback_user_page ON atlas_feedback (user_id, pageid);
    CREATE INDEX idx_atlas_feedback_location ON atlas_feedback (location_key);`;
  const database = (): DatabaseSync => {
    const db = new DatabaseSync(":memory:");
    db.exec("CREATE TABLE users (id TEXT PRIMARY KEY)");
    return db;
  };
  const schema = (db: DatabaseSync) =>
    db.prepare("SELECT type, name, sql FROM sqlite_master WHERE name LIKE '%atlas_feedback%' ORDER BY name").all() as
      Array<{ type: string; name: string; sql: string | null }>;

  it("applies to a database with no atlas_feedback, and a re-run changes nothing", () => {
    const db = database();
    db.exec(MIGRATION);
    const applied = schema(db);
    expect(applied.map((row) => `${row.type} ${row.name}`).sort()).toEqual([
      "index idx_atlas_feedback_judgment",
      "index idx_atlas_feedback_pair",
      "index idx_atlas_feedback_user_page",
      "table atlas_feedback",
      "view atlas_feedback_latest",
    ]);
    db.exec(MIGRATION);
    expect(schema(db)).toEqual(applied);
  });

  it("stops on a database that still holds the superseded table, and creates nothing", () => {
    const db = database();
    db.exec(SUPERSEDED);
    const before = schema(db);
    expect(() => db.exec(MIGRATION)).toThrow(/pair_key/);
    expect(schema(db)).toEqual(before);
  });

  it("the view reads the newest row per judgment, and only per judgment", () => {
    const db = database();
    db.exec(MIGRATION);
    db.exec("INSERT INTO users (id) VALUES ('a'), ('b')");
    const insert = db.prepare(
      `INSERT INTO atlas_feedback (location_key, pageid, revision, campaign, record_set, relation, verdict, user_id, created_at)
       VALUES (?, 1, 1, ?, ?, 'exactMatch', ?, ?, 1)`,
    );
    const key = "a".repeat(64), other = "b".repeat(64), set = "c".repeat(64);
    insert.run(key, "one", set, "report", "a");
    insert.run(key, "one", set, "withdraw", "a");
    insert.run(key, "two", set, "confirm", "a");
    insert.run(key, "one", set, "confirm", "b");
    insert.run(other, "one", set, "report", "a");
    const latest = db.prepare(
      "SELECT location_key, campaign, user_id, verdict FROM atlas_feedback_latest ORDER BY location_key, campaign, user_id",
    ).all().map((row) => ({ ...row }));
    expect(latest).toEqual([
      { location_key: key, campaign: "one", user_id: "a", verdict: "withdraw" },
      { location_key: key, campaign: "one", user_id: "b", verdict: "confirm" },
      { location_key: key, campaign: "two", user_id: "a", verdict: "confirm" },
      { location_key: other, campaign: "one", user_id: "a", verdict: "report" },
    ]);
  });
});

// A store built from a cf-locate-recordset/2 set (build_store): the article names
// its seal, each location its pair_key, and a bundle its relation and conjuncts.
describe("a store built from a cf-locate-recordset/2 set", () => {
  const SKILL = "d85385aa4eb2818373e9b6b8b41b765ffaf53750";
  const RUN = "run:loc-tierA-x+chal-tierA-x";

  function v2Store(): Map<string, string> {
    const files = articlesFixtureFiles();
    for (const [key, value] of files) {
      if (!key.startsWith("articles/v1/articles/")) continue;
      const doc = JSON.parse(value);
      doc.campaign = RUN;
      doc.seal = { set_id: "tierA-x", schema: "cf-locate-recordset/2", status_rule: 3, created: "2026-11-01T14:25:17Z", skill_commit: SKILL };
      for (const passage of doc.passages) {
        for (const loc of passage.locations) loc.pair_key = "a".repeat(63) + "b";
        if (passage.coverage) {
          passage.coverage.relation = "broadMatch";
          passage.coverage.conjuncts = passage.coverage.parts.map((part: string, i: number) => ({ id: `c${i + 1}`, text: `clause <${i + 1}>`, part }));
        }
      }
      files.set(key, JSON.stringify(doc));
    }
    return files;
  }

  it("cites the seal and the pair key under Details, and shows each bundle part beside its clauses", async () => {
    const h = setup();
    bindArticles(h.env, v2Store());
    const page = await (await get(h.env, `/articles/${GADGET_PAGEID}`)).text();
    expect(page).toContain("<li>Locate run <code>loc-tierA-x</code>, re-checked by challenge run <code>chal-tierA-x</code>.</li>" +
      "<li>Record set <code>tierA-x</code> (cf-locate-recordset/2, statuses by status rule 3), exported 1 Nov 2026 by formal-companion " +
      `at commit <code title="${SKILL}">d85385a</code>.</li>`);
    expect(page).toContain(`<li>Record set <code>tierA-x</code>, exported 1 Nov 2026 by formal-companion <code title="${SKILL}">d85385a</code>.</li>`);
    expect(page).toContain(` · pair key <code title="${"a".repeat(63)}b">aaaaaaaaaaaa</code>.</li>`);
    expect(page).toContain("Formalized by 2 parts together (more general):");
    expect(page).toContain('<ul class="articles-conjuncts" aria-label="Clauses this part states"><li>clause &lt;1&gt;</li></ul>');
    expect(page).toContain("<li>clause &lt;2&gt;</li>");
    expect(page).not.toMatch(/\bAI\b|pipeline|challenger/);
  });

  it("derives a store without a basis by the seal's status rule (R-BROAD under rule 3)", () => {
    const data = { relation: "broadMatch", proof_status: "definition", statement: false } as unknown as ArticlesLocation;
    expect(locationStatus("theorem", data)).toBe("partial");
    expect(locationStatus("theorem", data, 3)).toBe("formalized");
    expect(locationStatus("definition", data, 2)).toBe("partial");
    expect(locationStatus("definition", data, 3)).toBe("formalized");
    const proved = { ...data, proof_status: "proved" };
    expect(locationStatus("definition", proved, 3)).toBe("partial");
    expect(locationStatus("theorem", { ...data, proof_status: "wanted" }, 3)).toBe("statement_only");
  });
});

// The optional side files build_store merges (--passage-text, --signatures,
// --provenance): every field they add is optional within v1, and the golden
// store, built without them, renders as it always has. The values are synthetic.
describe("a store built with the optional side files", () => {
  const RUN = "run:loc-fx+chal-fx";
  const BOUND = "enwiki-1001-2002::In-text #5b0e44a1";
  const SIG = "theorem Widget.exists_spanning_gadget {W : Widget} :\n    W.Finite → ∃ g, W.Spans g";
  const LOCATE = {
    run_id: "loc-fx", mode: "locate", model: "model-x", model_name: "Model X", reviewer_model: "model-r",
    reviewer_model_name: "Model R", operator: "alice", started: "2026-10-30T23:50:00Z", harvested: "2026-10-31T04:48:00Z",
    skill_commit: "3bceabcee0724ffdf50ce65dde3ab10048c72cba",
  };
  const CHALLENGE = {
    ...LOCATE, run_id: "chal-fx", mode: "locate-challenge", started: "2026-11-01T03:19:32Z", harvested: "2026-11-01T14:25:17Z",
    sampling: { seed: 0, sample_rate: 0.2, strong_sample_rate: 0.5, absence_sample_rate: 0.25 },
  };
  const INVENTORY = { ...LOCATE, run_id: "inv-fx", mode: "locate-inventory", started: "2026-10-30T06:52:00Z", harvested: "2026-10-30T20:00:47Z" };
  const SUPPLEMENTS = {
    passage_text: { schema: "wikilean.articles.passage-text/1", sha256: "a".repeat(64) },
    signatures: { schema: "wikilean.articles.signatures/1", sha256: "b".repeat(64), corpus_sha256: "c".repeat(64),
      corpus_built: "2026-10-18T06:14:30Z", universe: "u-fixture" },
    provenance: { schema: "wikilean.articles.provenance/1", sha256: "d".repeat(64) },
  };

  // The golden store with the fields the side files add, edited in place (`edit` last).
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  function supplementedStore(edit: (article: any) => void = () => {}): Map<string, string> {
    const files = articlesFixtureFiles();
    for (const [key, value] of files) {
      if (key !== "articles/v1/current.json" && !key.startsWith("articles/v1/articles/")) continue;
      const doc = JSON.parse(value);
      if (key === "articles/v1/current.json") {
        doc.provenance = { runs: [CHALLENGE, LOCATE] };
        doc.supplements = { ...SUPPLEMENTS, passage_text: { ...SUPPLEMENTS.passage_text, passages: 1, shown: 1 } };
        files.set(key, JSON.stringify(doc));
        continue;
      }
      doc.campaign = RUN;
      doc.provenance = { locate: LOCATE, challenge: CHALLENGE };
      doc.supplements = SUPPLEMENTS;
      for (const passage of doc.passages) {
        if (passage.id === BOUND) {
          passage.text = { layer: "(the layer slice)", segments: [
            { text: "Moreover, the number of <spanning> gadgets of a widget is at most its rank" }, { ref: "[3]" },
            { text: " " }, { tex: "\\le r", display: true }, { text: " ." }] };
        }
        for (const loc of passage.locations) {
          if (loc.declaration === "Widget.exists_spanning_gadget") loc.signature = { text: SIG, elided: false };
          if (loc.declaration === "Gadget.equivVertex") {
            loc.signature = { text: "def Gadget.equivVertex {W : Widget} […] : W.Gadget ≃ W.Vertex", elided: true,
              type: "{W : Widget} → [inst : W.Finite] → W.Gadget ≃ W.Vertex", deprecated_to: "Widget.gadgetEquiv" };
          }
        }
      }
      edit(doc);
      files.set(key, JSON.stringify(doc));
    }
    return files;
  }

  async function page(path: string, files: Map<string, string>): Promise<string> {
    const h = setup();
    bindArticles(h.env, files);
    const res = await get(h.env, path);
    expect(res.status).toBe(200);
    return res.text();
  }

  // One location card's markup, by its declaration.
  function card(html: string, declaration: string): string {
    const leaf = declaration.slice(declaration.lastIndexOf(".") + 1).replace(/_/g, "_<wbr>");
    const at = html.indexOf(`<b>${leaf}</b>`);
    expect(at, declaration).toBeGreaterThanOrEqual(0);
    const start = html.lastIndexOf('<article class="articles-card"', at);
    return html.slice(start, html.indexOf("</article>", at));
  }

  it("each card leads with its declaration, then its signature as a Lean code block that breaks between binders", async () => {
    const html = await page(WIDGET_PATH, supplementedStore());
    const lead = card(html, "Widget.exists_spanning_gadget");
    // The store's line break is read as a space: the page flows the header, one piece per
    // binder and per hypothesis, the header's colon ending the last binder's piece.
    expect(lead).toContain('<span class="articles-rel" data-relation="exactMatch" title="States this exactly (exactMatch)">exact match</span></p>\n' +
      '<pre class="articles-sig"><code><span class="articles-sig-u">theorem Widget.exists_spanning_gadget</span> <span class="articles-sig-u">{W : Widget} :</span> ' +
      '<span class="articles-sig-u">W.Finite →</span> <span class="articles-sig-u">∃ g,</span> <span class="articles-sig-u">W.Spans g</span></code></pre>\n' +
      '<p class="articles-module">Mathlib.Combinatorics.Widget.Basic · lines 612–640</p>');
    expect(lead.indexOf("articles-decl-line")).toBeLessThan(lead.indexOf("articles-sig"));
    expect(lead.indexOf("articles-sig")).toBeLessThan(lead.indexOf("articles-card-top"));
    // Its Details say what the code is and where it is from; a card without one shows the name alone.
    expect(lead).toContain("<li>Lean code: the declaration&#x27;s type as Lean prints it, laid out as a header (the corpus keeps no " +
      "hypothesis names), from the declaration corpus of universe u-fixture at commit " +
      '<code title="bcb35bd682db2d92ab4e8a54f5e183b8a0b6b795">bcb35bd</code>.</li>');
    const bare = card(html, "WidgetTheory.spanning_gadget_exists");
    expect(bare).not.toContain("articles-sig");
    expect(bare).not.toContain("Lean code:");
    // The page says once what every card's code is, and that it is not the source text.
    expect(html).toContain('<p class="articles-muted" id="articles-sig-note">Each card&#x27;s Lean code is its declaration&#x27;s type as Lean ' +
      "prints it, from universe u-fixture&#x27;s declaration corpus at the card&#x27;s commit, laid out as a header: binders before the " +
      "colon, then the hypotheses as arrows (the corpus keeps no hypothesis names) and the conclusion, in Mathlib&#x27;s notation " +
      "(𝓝, π, R[X], Type*). It is not the source text; Source opens that.</p>");
    // A header shortened to fit keeps the whole type under a fold; a deprecated name says what replaced it.
    const gadget = card(await page(`/articles/${GADGET_PAGEID}`, supplementedStore()), "Gadget.equivVertex");
    expect(gadget).toContain('<pre class="articles-sig"><code><span class="articles-sig-u">def Gadget.equivVertex</span> ' +
      '<span class="articles-sig-u">{W : Widget}</span> <span class="articles-sig-u">[…] :</span> <span class="articles-sig-u">W.Gadget ≃ W.Vertex</span></code></pre>\n' +
      '<details class="articles-sig-full"><summary>Full type, as Lean prints it</summary><pre class="articles-sig"><code>' +
      '<span class="articles-sig-u">{W : Widget} →</span> <span class="articles-sig-u">[inst : W.Finite] →</span> <span class="articles-sig-u">W.Gadget ≃ W.Vertex</span>' +
      '</code></pre></details>\n' +
      '<p class="articles-deprecated">Deprecated at this commit, in favour of <code class="articles-decl"><span class="articles-ns">Widget.<wbr></span><b>gadgetEquiv</b></code>.</p>');
  });

  it("a passage with verified text is shown in full, formulas from their TeX; the others keep their quote", async () => {
    const html = await page(WIDGET_PATH, supplementedStore());
    expect(html).toContain('<blockquote class="articles-quote articles-text" data-quote="full">Moreover, the number of &lt;spanning&gt; ' +
      'gadgets of a widget is at most its rank<sup class="articles-ref">[3]</sup> <span class="articles-math-display">' +
      '<span class="articles-math" title="TeX: \\le r">≤ <i>r</i></span></span>.</blockquote>');
    // Its quote, the capped head, is not shown beside it, and the passage keeps its in-context link.
    const block = html.slice(html.indexOf('id="p-5b0e44a1"'), html.indexOf('id="p-91aa02de"'));
    expect(block).not.toContain("<mark>");
    expect(block).not.toContain("data-quote-cut");
    expect(block).toContain("Read it in context on Wikipedia");
    // Every other passage shows its quote as before, and the notes say which is which.
    expect(html).toContain('<blockquote class="articles-quote" data-quote="whole"><span class="articles-affix">…The widget lemma states that </span>' +
      "<mark>every finite widget admits a spanning gadget</mark>");
    expect(html).toContain('<p class="articles-muted" id="articles-text-note">1 passage is shown in full, as revision 2002 states it ' +
      "(checked against its record&#x27;s span hash when the store was built). Formulas are drawn from their TeX; hover one to see it.</p>");
    // The only capped quote is now shown in full, so the 12-word note goes.
    expect(html).not.toContain('id="articles-quote-note"');
    expect(html).toContain("The quote and text fields of the JSON download and the quote fields of the JSONL download are excerpts of the same text and stay under that licence.");
  });

  it("a passage that is only part of a sentence is highlighted, the rest of its sentence in grey around it", async () => {
    const html = await page(WIDGET_PATH, supplementedStore((article) => {
      for (const passage of article.passages) {
        if (passage.id !== BOUND) continue;
        passage.text.before = { layer: "(slice)", segments: [{ text: "Lemma 2 shows this: " }], lead_in: true };
        passage.text.after = { layer: "(slice)", segments: [{ text: ", and " }, { tex: "r \\ge 1" }, { text: " here" }], trimmed: true };
      }
    }));
    expect(html).toContain('<blockquote class="articles-quote articles-text" data-quote="full" data-context=""><span class="articles-affix">' +
      "Lemma 2 shows this: <br></span><mark>Moreover, the number of &lt;spanning&gt; gadgets of a widget is at most its rank" +
      '<sup class="articles-ref">[3]</sup> <span class="articles-math-display"><span class="articles-math" title="TeX: \\le r">≤ <i>r</i></span>' +
      '</span>.</mark><span class="articles-affix">, and <span class="articles-math" title="TeX: r \\ge 1"><i>r</i> ≥ 1</span> here…</span></blockquote>');
    expect(html).toContain("Where a passage is only part of a sentence, it is highlighted and the rest of the sentence is shown in grey " +
      "around it, from the same copy of the revision; a list item also shows the sentence that introduces it.");
    // A context that does not render (a corrupt store) is left out; the passage still shows.
    const broken = await page(WIDGET_PATH, supplementedStore((article) => {
      for (const passage of article.passages) if (passage.id === BOUND) passage.text.before = { segments: "<b>" };
    }));
    expect(broken).toContain('<blockquote class="articles-quote articles-text" data-quote="full">Moreover, the number');
    expect(broken).not.toContain("Where a passage is only part of a sentence");
  });

  it("Details cite the model, the operator and the dates of each run, and never call them unrecorded", async () => {
    const html = await page(WIDGET_PATH, supplementedStore());
    // A model's id never breaks at its hyphens.
    const M = 'Model X <span class="articles-nowrap">(model-x)</span>', R = 'Model R <span class="articles-nowrap">(model-r)</span>';
    expect(html).toContain(`<li>Found by ${M} on 30–31 Oct 2026 (UTC), locate run <code>loc-fx</code> by alice: ` +
      `candidate 1 for this passage, high confidence.</li><li>Re-checked by ${M} on 1 Nov 2026 (UTC), challenge run ` +
      "<code>chal-fx</code> by alice: it survived.</li>");
    expect(html).toContain(`<li>Re-checked by ${M} on 1 Nov 2026 (UTC), challenge run <code>chal-fx</code> by alice, which downgraded it.</li>`);
    expect(html).toContain(`<li>Not re-checked: sampled out of challenge run <code>chal-fx</code> by alice, in which ${M} ` +
      "re-checked a sample on 1 Nov 2026 (UTC).</li>");
    // The absence says who searched, and who re-checked that; the passage with no status is set aside.
    expect(html).toContain(`<li>Searched by ${M} on 30–31 Oct 2026 (UTC), locate run <code>loc-fx</code> by alice: the search receipt lists`);
    expect(html).toContain(`<li>Re-checked by ${M} on 1 Nov 2026 (UTC), challenge run <code>chal-fx</code> by alice: upheld.</li>`);
    expect(html).not.toContain("Left undecided");
    // About this data: each run once, to the minute, with both roles' models; the challenge
    // run says what it re-checked in this article, so its rates and the shares agree; and
    // what the store merged.
    const about = html.slice(html.indexOf('<details class="articles-about">'), html.indexOf("</details>", html.indexOf('<details class="articles-about">')));
    expect(about.split("<code>loc-fx</code>").length - 1).toBe(1);
    expect(about).toContain(`<li>Locate run <code>loc-fx</code>, run by alice, 30 Oct 2026, 23:50 – 31 Oct 2026, 04:48 UTC: ` +
      `worker ${M}, reviewer ${R}.</li><li>Challenge run <code>chal-fx</code>, run by alice, ` +
      `1 Nov 2026, 03:19–14:25 UTC: worker ${M}, reviewer ${R}. In this article it re-checked every Lean location and absence ` +
      "on a central or subject passage (2 locations and 1 absence); on supporting passages, a seeded sample (seed 0; drawn at 0.5 " +
      "for strong matches, 0.2 for weaker links and 0.25 for absences): 1 of 1 locations. In all, 3 of 3 Lean locations (100%) and " +
      "1 of 1 absences (100%) were re-checked.</li><li>Passage text: 1 of 3 passages shown in full");
    expect(html).toContain("<li>Lean code: each declaration&#x27;s type, from the declaration corpus of universe " +
      `u-fixture (built 18 Oct 2026, sha256 <code title="${"c".repeat(64)}">cccccccccccc</code>), at the commit its card links.</li>`);
    expect(html).not.toContain("does not record the model");
    expect(html).not.toMatch(/\bAI\b|pipeline|challenger/);
    // Provenance stays folded: every citation is inside a closed <details>.
    const open = html.replace(/<details[\s\S]*?<\/details>/g, "");
    expect(open).not.toContain("Model X");
    expect(open).not.toContain("alice");
  });

  it("the About folds cite the inventory run that chose the passages, when the store names one", async () => {
    const M = 'Model X <span class="articles-nowrap">(model-x)</span>', R = 'Model R <span class="articles-nowrap">(model-r)</span>';
    const line = `<li>Inventory run <code>inv-fx</code>, run by alice, 30 Oct 2026, 06:52–20:00 UTC: worker ${M}, reviewer ${R}. ` +
      "It listed the formal passages, each with its kind and prominence, that the locate run then searched.</li>";
    const files = supplementedStore((article) => { article.provenance.inventory = INVENTORY; });
    const current = JSON.parse(files.get("articles/v1/current.json")!);
    current.provenance.runs = [CHALLENGE, INVENTORY, LOCATE];
    files.set("articles/v1/current.json", JSON.stringify(current));
    // The article's fold names it first, before the runs that searched and re-checked its passages.
    const about = (await page(WIDGET_PATH, files)).match(/<details class="articles-about">.*?<\/details>/s)![0];
    expect(about).toContain(`${line}<li>Locate run <code>loc-fx</code>`);
    expect(await page("/articles", files)).toContain(line);
    // A store without it names none, as before.
    expect(await page(WIDGET_PATH, supplementedStore())).not.toContain("Inventory run");
  });

  it("the dashboard dates the build, or, when the build took the record sets' export as its time, the export", async () => {
    const files = articlesFixtureFiles();
    const current = JSON.parse(files.get("articles/v1/current.json")!);
    files.set("articles/v1/current.json", JSON.stringify({ ...current, built_at: "2026-11-03T09:00:00Z" }));
    expect(await page("/articles", files)).toContain("<li>Campaign fixture-2026-10, built 3 Nov 2026.</li>");
    expect(await page("/articles", articlesFixtureFiles())).toContain("<li>Campaign fixture-2026-10, from record sets exported 17 Oct 2026.</li>");
  });

  it("the dashboard's About this build lists each run and what the build merged", async () => {
    const M = 'Model X <span class="articles-nowrap">(model-x)</span>', R = 'Model R <span class="articles-nowrap">(model-r)</span>';
    const html = await page("/articles", supplementedStore());
    expect(html).toContain(`<li>Challenge run <code>chal-fx</code>, run by alice, 1 Nov 2026, 03:19–14:25 UTC: worker ${M}, ` +
      `reviewer ${R}. It sampled with seed 0, at 0.5 for strong matches, 0.2 for weaker links and 0.25 for absences.</li>` +
      `<li>Locate run <code>loc-fx</code>, run by alice, 30 Oct 2026, 23:50 – 31 Oct 2026, 04:48 UTC: worker ${M}, ` +
      `reviewer ${R}.</li><li>Passage text: 1 of 6 passages shown in full`);
    expect(html).not.toContain("does not record the model");
    // With the build's own tallies (build_store recheck_tallies), it says what the challenge re-checked across the build.
    const files = supplementedStore();
    const current = JSON.parse(files.get("articles/v1/current.json")!);
    current.provenance.runs[0].rechecked = { central: { locations: [2109, 2109], absences: [723, 723] },
      supporting: { locations: [1080, 1799], absences: [318, 1236] } };
    files.set("articles/v1/current.json", JSON.stringify(current));
    expect(await page("/articles", files)).toContain(`reviewer ${R}. Across the build it re-checked every Lean location and absence on a ` +
      "central or subject passage (2109 locations and 723 absences); on supporting passages, a seeded sample (seed 0; drawn at 0.5 " +
      "for strong matches, 0.2 for weaker links and 0.25 for absences): 1080 of 1799 locations and 318 of 1236 absences. " +
      "In all, 3189 of 3908 Lean locations (82%) and 1041 of 1959 absences (53%) were re-checked.</li>");
  });

  it("a malformed side-file field never breaks the page: it falls back to what the record set alone shows", async () => {
    const html = await page(WIDGET_PATH, supplementedStore((article) => {
      article.provenance = { locate: { run_id: "loc-fx", model: 5 }, challenge: "x" };
      for (const passage of article.passages) {
        passage.text = { layer: "", segments: passage.id === BOUND ? [{ text: 1 }] : "<b>" };
        for (const loc of passage.locations) loc.signature = { text: ["<script>"], elided: "yes" };
      }
    }));
    expect(html).not.toContain("articles-sig");
    expect(html).not.toContain("articles-text");
    expect(html).toContain("<li>Found in locate run <code>loc-fx</code>: candidate 1 for this passage, high confidence.</li>");
    expect(html).toContain("The record set does not record the model or the operator that ran these runs.");
    expect(html).toContain('id="articles-quote-note"');
    expect(html).not.toContain("<script>\"");
    // Store text in a signature or a passage is escaped, never markup.
    const hostile = await page(WIDGET_PATH, supplementedStore((article) => {
      article.passages[0].text = { layer: "", segments: [{ text: "<img src=x>" }, { tex: "</span><img>" }] };
      article.passages[0].locations[0].signature = { text: "theorem X </code><img src=x>", elided: false };
    }));
    expect(hostile).not.toContain("<img");
    expect(hostile).toContain("&lt;img src=x&gt;");
  });

  it("without the side files the page says what is unrecorded and shows the quotes, as it always has", async () => {
    const html = await page(WIDGET_PATH, articlesFixtureFiles());
    for (const absent of ["articles-sig", "articles-text", "articles-undecided-prov", "Signature from", "Passage text:", "(UTC)"]) {
      expect(html).not.toContain(absent);
    }
    expect(html).toContain('<li>Seal sha256 <code>');
    expect(html).toContain("<li>The record set does not record the model or the operator that ran these runs.</li></ul></details>");
    expect(html).toContain('id="articles-quote-note"');
    expect(html).toContain("The quote fields of the JSON and JSONL downloads are excerpts of the same text and stay under that licence.");
  });
});

// SEAL.json's campaign is always a token: the campaign id, or for a run outside
// any campaign "run:<run-id>[+<challenge-run-id>]" (build_store.CAMPAIGN_RE).
describe("a record set from a run outside any campaign", () => {
  const RUN = "run:loc-smoke-2+chal-smoke-2";

  function runStore(): Map<string, string> {
    const files = articlesFixtureFiles();
    for (const [key, value] of files) {
      if (key === "articles/v1/current.json" || key.startsWith("articles/v1/articles/")) {
        const doc = JSON.parse(value);
        doc.campaign = RUN;
        files.set(key, JSON.stringify(doc));
      }
    }
    return files;
  }

  it("names the runs on the pages, and takes feedback under the run token", async () => {
    const h = setup();
    bindArticles(h.env, runStore());
    const page = await (await get(h.env, WIDGET_PATH, { user: "u-human" })).text();
    // The runs are named, specifically, in the Details folds: the locate run that found a
    // location and the challenge run that re-checked it.
    expect(page).toContain("<li>Locate run <code>loc-smoke-2</code>, re-checked by challenge run <code>chal-smoke-2</code>.</li>");
    expect(page).toContain("<li>Found in locate run <code>loc-smoke-2</code>: candidate 1 for this passage, high confidence.</li>" +
      "<li>Re-checked in challenge run <code>chal-smoke-2</code>: it survived.</li>");
    expect(page).not.toContain("Undecided");
    expect(page).toContain("<li>Not re-checked: sampled out of challenge run <code>chal-smoke-2</code>.</li>");
    expect(page).toContain(`data-campaign="${RUN}"`);
    const dashboard = await (await get(h.env, "/articles")).text();
    expect(dashboard).toContain(`<summary>About this build</summary><ul class="articles-prov-list"><li>${UNMEASURED}</li><li>Run loc-smoke-2 with challenge run chal-smoke-2, outside any campaign, from record sets exported 17 Oct 2026.</li>`);
    const res = await post(h.env, "/api/articles/feedback", feedback({ campaign: RUN }), { user: "u-human" });
    expect(res.status).toBe(200);
    expect(feedbackRows(h)[0]).toMatchObject({ campaign: RUN, verdict: "confirm" });
    // The fixture's campaign id is now stale for this store.
    expect((await post(h.env, "/api/articles/feedback", feedback(), { user: "u-human" })).status).toBe(409);
    for (const campaign of ["run:loc-smoke-2+chal-smoke-2+x", "run:", "run:a+", "run:a:b", ""]) {
      const bad = await post(h.env, "/api/articles/feedback", feedback({ campaign }), { user: "u-human" });
      expect(bad.status, campaign).toBe(400);
    }
  });

  it("accepts exactly the tokens the builder accepts", () => {
    for (const token of ["run:loc-smoke-2", RUN, "tier-a:shard-03", FIXTURE_CAMPAIGN]) {
      expect(CAMPAIGN_RE.test(token), token).toBe(true);
    }
    for (const token of ["", "run:", "run:loc+", "run:+chal", "run:a+b+c", "run:a:b", "run:a b", "loc smoke", "-x",
      "x".repeat(129), "run:" + "x".repeat(65), "fixture\n"]) {
      expect(CAMPAIGN_RE.test(token), JSON.stringify(token)).toBe(false);
    }
    expect(campaignLabel("run:loc-1")).toBe("run loc-1, outside any campaign");
    expect(campaignLabel(FIXTURE_CAMPAIGN)).toBe("campaign fixture-2026-10");
  });
});

describe("articles feedback export", () => {
  it("is pipeline-bearer only", async () => {
    const h = bound();
    expect((await get(h.env, "/api/articles/feedback/export")).status).toBe(403);
    expect((await get(h.env, "/api/articles/feedback/export", { user: "u-admin" })).status).toBe(403);
    expect((await get(h.env, "/api/articles/feedback/export", { user: "u-human" })).status).toBe(403);
  });

  it("streams every verdict, not only the newest, for consumers to take the newest per judgment", async () => {
    const h = bound();
    await post(h.env, "/api/articles/feedback", feedback({ verdict: "report" }), { user: "u-human" });
    await post(h.env, "/api/articles/feedback", feedback({ verdict: "withdraw" }), { user: "u-human" });
    const res = await get(h.env, "/api/articles/feedback/export", { bearer: PIPELINE_TOKEN });
    const lines = (await res.text()).trim().split("\n").map((line) => JSON.parse(line));
    expect(lines.map((line) => line.verdict)).toEqual(["report", "withdraw"]);
    expect(lines.map((line) => line.feedback_id)).toEqual([1, 2]);
  });

  it("streams every row with pseudonyms and no user ids, and works with ARTICLES unbound", async () => {
    const h = bound();
    await post(h.env, "/api/articles/feedback", feedback(), { user: "u-human" });
    await post(h.env, "/api/articles/feedback", feedback({ verdict: "report", note: "special case only" }), { user: "u-patroller" });
    delete (h.env as { ARTICLES?: unknown }).ARTICLES;
    const res = await get(h.env, "/api/articles/feedback/export", { bearer: PIPELINE_TOKEN });
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("application/x-ndjson; charset=utf-8");
    expect(res.headers.get("Cache-Control")).toBe("no-store");
    const lines = (await res.text()).trim().split("\n").map((line) => JSON.parse(line));
    expect(lines).toHaveLength(2);
    // Domain-separated from the research export's sha256(user_id + salt), so a
    // note that names its author cannot be joined to that export.
    const pseudonym = (id: string) => createHash("sha256").update("atlas-feedback\0" + id + PIPELINE_TOKEN).digest("hex").slice(0, 12);
    const research = (id: string) => createHash("sha256").update(id + PIPELINE_TOKEN).digest("hex").slice(0, 12);
    // The domain names the D1 table, which keeps its name, so no pseudonym is re-keyed.
    expect(ARTICLES_PSEUDONYM_DOMAIN).toBe("atlas-feedback\0");
    expect(pseudonym("u-human")).not.toBe(research("u-human"));
    const article = fixtureArticle();
    expect(lines[0]).toEqual({
      feedback_id: 1, location_key: MATHLIB_KEY, pair_key: null, pageid: WIDGET_PAGEID, revision: 2002,
      campaign: FIXTURE_CAMPAIGN, record_set: article.record_set, relation: article.passages[0].locations[0].relation,
      verdict: "confirm", note: null, provenance: "human", pseudonym: pseudonym("u-human"),
      created_at: lines[0].created_at,
    });
    expect(lines[1]).toMatchObject({ verdict: "report", note: "special case only", pseudonym: pseudonym("u-patroller") });
    expect(JSON.stringify(lines)).not.toContain("u-human");
    expect(JSON.stringify(lines)).not.toContain(research("u-human"));
  });

  // The research export's salt (security.ts researchExportSalt): a bearer
  // holder must not be able to recompute these pseudonyms either, and rotating
  // the bearer must not re-key them once RESEARCH_EXPORT_SALT is set.
  it("salts with RESEARCH_EXPORT_SALT when set, and with PIPELINE_TOKEN only as the fallback", async () => {
    const h = bound();
    await post(h.env, "/api/articles/feedback", feedback(), { user: "u-human" });
    const pseudonymOf = async () => {
      const res = await get(h.env, "/api/articles/feedback/export", { bearer: PIPELINE_TOKEN });
      expect(res.status).toBe(200);
      return JSON.parse((await res.text()).trim()).pseudonym as string;
    };
    const articles = (salt: string) => createHash("sha256").update(ARTICLES_PSEUDONYM_DOMAIN + "u-human" + salt).digest("hex").slice(0, 12);
    const research = (salt: string) => createHash("sha256").update("u-human" + salt).digest("hex").slice(0, 12);
    expect(await pseudonymOf()).toBe(articles(PIPELINE_TOKEN));

    h.env.RESEARCH_EXPORT_SALT = "dedicated-research-salt";
    const salted = await pseudonymOf();
    expect(salted).toBe(articles("dedicated-research-salt"));
    expect(salted).not.toBe(articles(PIPELINE_TOKEN));
    // Still domain-separated from the research export under the shared salt.
    expect(salted).not.toBe(research("dedicated-research-salt"));

    // An empty value is "unset", not an empty salt.
    h.env.RESEARCH_EXPORT_SALT = "";
    expect(await pseudonymOf()).toBe(articles(PIPELINE_TOKEN));
  });
});

// The articles plane under the Worker's security layer (src/security.ts): every
// response of its own, whatever its status or how it is built, carries SECURITY_HEADERS,
// and the pages, the article box and the plane's assets load nothing the
// Report-Only policy would report, so enforcing that policy cannot break them
// (e2e/articles.spec.ts runs the flows with it enforced).
describe("articles security headers and CSP", () => {
  // Every HTML answer of the plane is one of its pages, which enforces articlesCsp beside the framing ban; the
  // JSON answers carry the shared set alone.
  function expectSecurityHeaders(res: Response, label: string): void {
    const page = (res.headers.get("Content-Type") ?? "").startsWith("text/html");
    for (const [name, value] of Object.entries(SECURITY_HEADERS)) {
      const want = name === "Content-Security-Policy" && page ? `${ARTICLES_CSP}, ${value}` : value;
      expect(res.headers.get(name), `${label}: ${name}`).toBe(want);
    }
  }

  function sources(directive: string): string[] {
    const part = CSP_REPORT_ONLY.split("; ").find((d) => d.startsWith(directive + " "));
    expect(part, `CSP lacks ${directive}`).toBeDefined();
    return part!.split(" ").slice(1);
  }

  const sameOrigin = (url: string) => new URL(url, ORIGIN).origin === new URL(ORIGIN).origin;

  // Everything an HTML fragment makes the browser fetch, submit to or run.
  function loads(html: string): { urls: string[]; embeds: string[]; handlers: string[] } {
    const urls = [
      ...[...html.matchAll(/<script\b[^>]*\ssrc="([^"]*)"/g)].map((m) => m[1]),
      ...[...html.matchAll(/<link\b[^>]*\srel="(?:stylesheet|preload|modulepreload|icon)"[^>]*\shref="([^"]*)"/g)].map((m) => m[1]),
      ...[...html.matchAll(/<form\b[^>]*\saction="([^"]*)"/g)].map((m) => m[1]),
    ];
    const embeds = [...html.matchAll(/<(img|iframe|frame|embed|object|video|audio|source)\b/g)].map((m) => m[1]);
    const handlers = [...html.matchAll(/<[a-z][^>]*\s(on[a-z]+)\s*=/gi)].map((m) => m[1]);
    return { urls, embeds, handlers };
  }

  it("are on every articles page, API answer, error, write answer and export", async () => {
    const unbound = setup();
    for (const path of ["/articles", WIDGET_PATH, "/api/articles/current", `/api/articles/article/${WIDGET_PAGEID}`]) {
      const res = await get(unbound.env, path);
      expect(res.status, path).toBe(503);
      expectSecurityHeaders(res, `unbound ${path}`);
    }

    const h = bound();
    const reads: Array<[string, number]> = [
      ["/articles", 200], ["/articles?q=wid", 200], [`/articles/${WIDGET_PAGEID}`, 200], [WIDGET_PATH, 200],
      [`/articles/universe/${FIXTURE_UNIVERSE_SHA}`, 200], ["/articles/9999", 404],
      ["/api/articles/current", 200], [`/api/articles/article/${WIDGET_PAGEID}`, 200], [`/api/articles/records/${WIDGET_PAGEID}`, 200],
      [`/api/articles/universe/${FIXTURE_UNIVERSE_SHA}`, 200], ["/api/articles/index/1", 200], ["/api/articles/article/9999", 404],
    ];
    for (const [path, status] of reads) {
      const res = await get(h.env, path);
      expect(res.status, path).toBe(status);
      expectSecurityHeaders(res, path);
    }

    // Over the read budget.
    const limited = bound({ brainApiLimiterAllows: false });
    for (const path of ["/articles?q=wid", WIDGET_PATH, `/api/articles/article/${WIDGET_PAGEID}`, `/api/articles/records/${WIDGET_PAGEID}`]) {
      const res = await get(limited.env, path);
      expect(res.status, path).toBe(429);
      expectSecurityHeaders(res, `over the budget ${path}`);
    }

    const writes: Array<[() => Promise<Response>, number]> = [
      [() => post(h.env, "/api/articles/feedback", feedback(), { user: "u-human" }), 200],
      [() => post(h.env, "/api/articles/feedback", feedback()), 401],
      [() => post(h.env, "/api/articles/feedback", feedback(), { user: "u-human", origin: "https://evil.example" }), 403],
      [() => post(h.env, "/api/articles/feedback", feedback({ note: "x".repeat(5000) }), { user: "u-human" }), 413],
      [() => post(h.env, "/api/articles/feedback", feedback({ verdict: "maybe" }), { user: "u-human" }), 400],
    ];
    for (const [send, status] of writes) {
      const res = await send();
      expect(res.status).toBe(status);
      expectSecurityHeaders(res, `feedback ${status}`);
    }

    // The export is a hand-built streaming Response, not a c.json/c.html one.
    const exported = await get(h.env, "/api/articles/feedback/export", { bearer: PIPELINE_TOKEN });
    expect(exported.status).toBe(200);
    expectSecurityHeaders(exported, "export");
  });

  it("the policy keeps inline scripts and styles working: 'unsafe-inline' with no nonce, hash or strict-dynamic", () => {
    // A nonce, a hash or 'strict-dynamic' in a source list makes browsers
    // ignore 'unsafe-inline' there, which would block the pages' inline theme
    // and search scripts, their <style> block and the coverage bar's style="".
    for (const directive of ["script-src", "style-src"]) {
      expect(sources(directive)).toContain("'self'");
      expect(sources(directive)).toContain("'unsafe-inline'");
      expect(sources(directive).filter((s) => /^'(nonce-|sha(256|384|512)-|strict-dynamic)/.test(s))).toEqual([]);
    }
    expect(CSP_REPORT_ONLY).not.toMatch(/(script|style)-src-(elem|attr) /);
    expect(sources("connect-src")).toContain("'self'");
    expect(sources("form-action")).toEqual(["'self'"]);
    expect(sources("script-src")).not.toContain("'unsafe-eval'");
  });

  it("the pages and the article box load only same-origin URLs, embed nothing, and have no inline handlers", async () => {
    const h = bound();
    h.db.prepare("UPDATE articles SET wikipedia_title = 'Widget_lemma' WHERE slug = ?").run(SLUG);
    const pages: Array<[string, string | undefined]> = [
      ["/articles", undefined], ["/articles?q=gadg", undefined], [WIDGET_PATH, undefined], [WIDGET_PATH, "u-human"],
      [`/articles/universe/${FIXTURE_UNIVERSE_SHA}`, undefined], ["/articles/9999", undefined],
    ];
    for (const [path, user] of pages) {
      const html = await (await get(h.env, path, user ? { user } : {})).text();
      const { urls, embeds, handlers } = loads(html);
      expect(urls.length, path).toBeGreaterThan(0);
      expect(urls.filter((url) => !sameOrigin(url)), path).toEqual([]);
      expect(embeds, path).toEqual([]);
      expect(handlers, path).toEqual([]);
      expect(html, path).toContain('<script src="/assets/articles.js?v=5"></script>');
    }
    const article = await (await get(h.env, `/${SLUG}`)).text();
    const start = article.indexOf('<link rel="stylesheet" href="/assets/articles.css');
    const end = article.indexOf("</aside>", start);
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
    const box = loads(article.slice(start, end));
    expect(box.urls).toEqual(["/assets/articles.css?v=10"]);
    expect(box.embeds).toEqual([]);
    expect(box.handlers).toEqual([]);
  });

  it("every inline script an articles page serves is one articlesCsp lists by hash, and it lists no other", async () => {
    // A browser runs an inline script under articlesCsp only when the sha256 of its text is listed. If
    // this fails, an inline script of the articles shell changed (articles-render.ts or
    // engine/searchbox.ts SEARCHBOX_SCRIPT): update ARTICLES_INLINE_SCRIPT_HASHES in src/security.ts.
    const reader = setup();
    bindArticles(reader.env, articlesReaderFiles());
    const limited = setup({ brainApiLimiterAllows: false });
    bindArticles(limited.env);
    const pages: Array<[Harness, string]> = [
      [reader, WIDGET_PATH], [reader, `${WIDGET_PATH}?view=cards`], [reader, "/articles"], [reader, "/articles?q=gadg"],
      [reader, `/articles/universe/${FIXTURE_UNIVERSE_SHA}`], [reader, "/articles/9999"], [reader, `/articles/universe/${"0".repeat(64)}`],
      [limited, WIDGET_PATH], [setup(), "/articles"],
    ];
    const listed = new Set(Object.values(ARTICLES_INLINE_SCRIPT_HASHES));
    const seen = new Set<string>();
    for (const [h, path] of pages) {
      const res = await get(h.env, path);
      expect(res.headers.get("Content-Security-Policy"), path).toBe(`${ARTICLES_CSP}, ${SECURITY_HEADERS["Content-Security-Policy"]}`);
      const html = await res.text();
      const inline = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)].filter((m) => !/\ssrc=/.test(m[1]));
      expect(inline.length, path).toBe(2);
      for (const m of inline) {
        expect(m[1], `${path}: an inline script carries no attribute`).toBe("");
        const hash = `'sha256-${createHash("sha256").update(m[2], "utf8").digest("base64")}'`;
        expect(listed.has(hash), `${path}: inline script ${JSON.stringify(m[2].slice(0, 40))} is ${hash}`).toBe(true);
        seen.add(hash);
      }
    }
    expect([...seen].sort()).toEqual([...listed].sort());
  });

  it("articlesCsp lets images and media load only from the hosts the builder lets a revision fetch from", () => {
    const builder = readFileSync(new URL("../../scripts/articles/build_store.py", import.meta.url), "utf8");
    const fetch = builder.match(/^READER_FETCH = \(([^)]*)\)$/m);
    expect(fetch, "build_store.READER_FETCH").not.toBeNull();
    const hosts = [...fetch![1].matchAll(/"(https:\/\/[^"]+)\/"/g)].map((m) => m[1]);
    expect(hosts).toEqual([...ARTICLES_MEDIA_HOSTS]);
    for (const directive of ["img-src", "media-src"]) {
      const part = ARTICLES_CSP.split("; ").find((d) => d.startsWith(directive + " "));
      expect(part!.split(" ").slice(1)).toEqual(["'self'", ...hosts]);
    }
  });

  it("every HTML answer of the plane goes through articlesHtml, so none leaves without the policy", () => {
    const src = readFileSync(new URL("../src/articles.ts", import.meta.url), "utf8");
    expect(src.match(/\bc\.html\(/g)).toHaveLength(1);
    expect(src).toContain(`return c.html(html, status, { ...headers, "Content-Security-Policy": articlesCsp(new URL(c.req.url).origin) });`);
    expect(src).not.toMatch(/new Response\([^)]*text\/html/);
  });

  it("the policy runs the plane's two scripts by URL, not every same-origin script, and lets forms post only to /articles", async () => {
    const policy = new Map(articlesCsp("https://wikilean.example").split("; ").map((part) => {
      const [name, ...values] = part.split(" ");
      return [name, values] as [string, string[]];
    }));
    expect(policy.get("script-src")).toEqual([
      "https://wikilean.example/assets/articles.js", "https://wikilean.example/assets/articles-reader.js",
      ARTICLES_INLINE_SCRIPT_HASHES.theme, ARTICLES_INLINE_SCRIPT_HASHES.search, "https://static.cloudflareinsights.com",
    ]);
    expect(policy.get("script-src")).not.toContain("'self'");
    expect(policy.get("form-action")).toEqual(["https://wikilean.example/articles"]);
    // The paths are the pages' script tags without their ?v= (a path source ignores the query).
    expect(ARTICLES_SCRIPT_PATHS).toEqual([ARTICLES_JS, ARTICLES_READER_JS].map((url) => url.split("?")[0]));
    // Every script file an articles page loads is one of them, and its one form posts to /articles.
    const h = setup();
    bindArticles(h.env, articlesReaderFiles());
    const forms: string[] = [];
    for (const path of [WIDGET_PATH, `${WIDGET_PATH}?view=cards`, "/articles", "/articles?q=gadg", `/articles/universe/${FIXTURE_UNIVERSE_SHA}`, "/articles/9999"]) {
      const html = await (await get(h.env, path)).text();
      for (const m of html.matchAll(/<script\b[^>]*\ssrc="([^"]+)"/g)) {
        expect(ARTICLES_SCRIPT_PATHS, `${path}: ${m[1]}`).toContain(new URL(m[1], ORIGIN).pathname);
      }
      for (const m of html.matchAll(/<form\b([^>]*)>/g)) forms.push(/\saction="([^"]*)"/.exec(m[1])?.[1] ?? "");
    }
    expect(forms).toEqual([ARTICLES_FORM_PATH, ARTICLES_FORM_PATH]);
  });

  it("a page's policy names the origin it was served from", async () => {
    const h = bound();
    const res = await app.request("https://wikilean.example/articles", {}, h.env);
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Security-Policy")).toBe(
      `${articlesCsp("https://wikilean.example")}, ${SECURITY_HEADERS["Content-Security-Policy"]}`);
  });

  it("articles pages load the site stylesheet at the version the article pages load it", async () => {
    // engine/page.ts and articles-render.ts STYLE_CSS name style.css's ?v= each: a change to style.css
    // bumps both, or a reader page keeps a stale cached copy.
    const h = bound();
    const article = await (await get(h.env, `/${SLUG}`)).text();
    const site = /<link rel="stylesheet" href="(\/assets\/style\.css\?v=[^"]+)">/.exec(article);
    expect(site, "the article page's stylesheet").not.toBeNull();
    const reader = setup();
    bindArticles(reader.env, articlesReaderFiles());
    for (const [env, path] of [[h.env, "/articles"], [h.env, WIDGET_PATH], [reader.env, WIDGET_PATH]] as const) {
      const html = await (await get(env, path)).text();
      expect(html.match(/\/assets\/style\.css\?v=[^"]+/g), path).toEqual([site![1]]);
    }
  });

  it("articles.js fetches only same-origin paths and never evaluates strings; articles.css loads nothing", () => {
    const js = readFileSync(new URL("../../site/assets/articles.js", import.meta.url), "utf8");
    const css = readFileSync(new URL("../../site/assets/articles.css", import.meta.url), "utf8");
    const targets = [...js.matchAll(/\bfetch\(\s*(["'`])([^"'`]*)/g)].map((m) => m[2]);
    expect(targets).toEqual(["/api/articles/feedback", "/api/articles/index/"]);
    expect(js.match(/\bfetch\(/g)).toHaveLength(targets.length);
    expect(js).not.toMatch(/https?:\/\/|["'`]\/\//);
    expect(js).not.toMatch(/\beval\s*\(|\bnew\s+Function\b|\bset(Timeout|Interval)\(\s*["'`]/);
    expect(css).not.toMatch(/url\(|@import/);
  });
});

describe("article page summary box", () => {
  it("appears per request for an article whose Wikipedia title the plane covers, outside the cached base", async () => {
    const h = bound();
    h.db.prepare("UPDATE articles SET wikipedia_title = 'Widget_lemma' WHERE slug = ?").run(SLUG);
    const res = await get(h.env, `/${SLUG}`);
    expect(res.status).toBe(200);
    const html = await res.text();
    expect(html).toContain('<aside class="wl-articles-box" aria-label="Formalization">');
    expect(html).toContain(`<a class="wl-articles-box-link" href="${WIDGET_PATH}">See where`);
    // Its counts are the plane's, not this page's annotations (the badges
    // above), and it names the page's revision when the plane checked another.
    // Its counts are the passages with a status, as the article's own page counts them, and it says
    // how many more there are; the one with none has no status here either.
    expect(html).toContain("Separately from this page&#x27;s annotations, WikiLean checked 3 passages of Wikipedia revision 2002 against Lean libraries:");
    expect(html).toContain(`<span class="wl-articles-box-rev">This page shows revision ${REVID}.</span>`);
    expect(html).toContain("1 formalized");
    expect(html).toContain("1 not formalized</span>. 1 more passage has no status yet. <span class=\"wl-articles-box-rev\">");
    expect(html).not.toMatch(/Undecided|undecided|data-status="unresolved"/);
    // The notice is the articles pages' line under their header (one line, never a badge); the box
    // carries no notice of its own.
    expect(html).not.toContain(ARTICLES_NOTICE);
    expect(html).not.toContain("wl-articles-box-note");
    expect(html.indexOf("wl-articles-box")).toBeLessThan(html.indexOf('<main class="wl-article-body">'));
    // The publish switch and one index shard, nothing else from the store (each kept a minute).
    expect(h.gets).toEqual(["articles/v1/current.json", "articles/v1/index/1.json"]);
    // The cached base page (render:v20) does not carry the box.
    const cached = [...h.renderCache.store.entries()].filter(([key]) => key.startsWith("render:v20:"));
    expect(cached).toHaveLength(1);
    expect(String(cached[0][1])).not.toContain("wl-articles-box");
  });

  it("is absent for a title the articles does not cover, and a broken store never breaks the page", async () => {
    const h = bound();
    expect(await (await get(h.env, `/${SLUG}`)).text()).not.toContain("wl-articles-box");
    h.db.prepare("UPDATE articles SET wikipedia_title = 'Widget lemma' WHERE slug = ?").run(SLUG);
    (h.env as unknown as { ARTICLES: unknown }).ARTICLES = { get: async () => { throw new Error("R2 down"); } };
    const res = await get(h.env, `/${SLUG}`);
    expect(res.status).toBe(200);
    expect(await res.text()).not.toContain("wl-articles-box");
  });

  it("names the page's revision only when it differs from the plane's", async () => {
    const entry = (await lookupTitle(memoryReader(articlesFixtureFiles()), "Widget lemma"))!;
    expect(articlesSummaryBox(entry, WIDGET_PATH, 2002)).not.toContain("wl-articles-box-rev");
    expect(articlesSummaryBox(entry, WIDGET_PATH, null)).not.toContain("wl-articles-box-rev");
    expect(articlesSummaryBox(entry, WIDGET_PATH, 2003)).toContain("This page shows revision 2003.");
    const one = { ...entry, passages: 1, formalized: 1, statement_only: 0, not_formalized: 0, unresolved: 0 };
    expect(articlesSummaryBox(one, WIDGET_PATH, 2002)).toContain("checked 1 passage of Wikipedia revision 2002 against Lean libraries: ");
    expect(articlesSummaryBox(one, WIDGET_PATH, 2002)).not.toContain("no status yet");
    // Passages that all have no status yet: found, none with a status, and no count by status.
    const unread = { ...entry, passages: 2, formalized: 0, statement_only: 0, not_formalized: 0, unresolved: 2 };
    expect(articlesSummaryBox(unread, WIDGET_PATH, 2002)).toContain(
      "Separately from this page&#x27;s annotations, WikiLean found 2 passages in Wikipedia revision 2002, none with a status yet.");
    expect(articlesSummaryBox(unread, WIDGET_PATH, 2002)).not.toMatch(/articles-mini|undecided/);
  });

  it("injectArticlesSummary returns the page byte-for-byte when there is no box", () => {
    const page = '<html><body><main class="wl-article-body">x</main></body></html>';
    expect(injectArticlesSummary(page, null)).toBe(page);
    expect(injectArticlesSummary(page, "<aside>B</aside>")).toBe('<html><body><aside>B</aside><main class="wl-article-body">x</main></body></html>');
    expect(injectArticlesSummary("<html><body><p>y</p></body></html>", "<aside>B</aside>")).toBe("<html><body>\n<aside>B</aside><p>y</p></body></html>");
  });
});

describe("the articles is kept out of search indexes", () => {
  it("every answer under /articles and /api/articles carries X-Robots-Tag: noindex, whatever its status", async () => {
    const h = setup();
    bindArticles(h.env, articlesReaderFiles());
    const limited = setup({ brainApiLimiterAllows: false });
    bindArticles(limited.env);
    const unbound = setup();
    const answers: Array<[string, () => Promise<Response>, number]> = [
      ...([
        [WIDGET_PATH, 200], [`${WIDGET_PATH}?view=cards`, 200], [`/articles/${WIDGET_PAGEID}`, 200], ["/articles", 200],
        ["/articles?q=gadg", 200], [`/articles/universe/${FIXTURE_UNIVERSE_SHA}`, 200], ["/articles/9999", 404],
        [`/articles/universe/${"0".repeat(64)}`, 404],
        // Paths no articles route takes answer the site's 404, and are kept out too.
        ["/articles/abc", 404], [`${WIDGET_PATH}/extra`, 404], ["/api/articles/nope", 404],
        ["/api/articles/current", 200], [`/api/articles/article/${WIDGET_PAGEID}`, 200], [`/api/articles/records/${WIDGET_PAGEID}`, 200],
        [`/api/articles/universe/${FIXTURE_UNIVERSE_SHA}`, 200], ["/api/articles/index/1", 200], ["/api/articles/article/9999", 404],
        ["/api/articles/feedback/export", 403],
      ] as Array<[string, number]>).map(([path, status]): [string, () => Promise<Response>, number] => [path, () => get(h.env, path), status]),
      ["export", () => get(h.env, "/api/articles/feedback/export", { bearer: PIPELINE_TOKEN }), 200],
      ["feedback", () => post(h.env, "/api/articles/feedback", feedback()), 401],
      ["429 page", () => get(limited.env, WIDGET_PATH), 429],
      ["429 view model", () => get(limited.env, `/api/articles/article/${WIDGET_PAGEID}`), 429],
      ["503 page", () => get(unbound.env, "/articles"), 503],
      ["503 reader", () => get(unbound.env, WIDGET_PATH), 503],
      ["503 current", () => get(unbound.env, "/api/articles/current"), 503],
    ];
    for (const [label, send, status] of answers) {
      const res = await send();
      expect(res.status, label).toBe(status);
      expect(res.headers.get("X-Robots-Tag"), label).toBe("noindex");
    }
    // Nothing outside the plane: WikiLean's article pages, the box on them included, stay indexable.
    h.db.prepare("UPDATE articles SET wikipedia_title = 'Widget lemma' WHERE slug = ?").run(SLUG);
    for (const path of [`/${SLUG}`, "/about", "/api/articles", "/articlesx", "/"]) {
      expect((await get(h.env, path)).headers.get("X-Robots-Tag"), path).toBeNull();
    }
    expect(await (await get(h.env, `/${SLUG}`)).text()).not.toContain("noindex");
  });

  it("every articles page says noindex in its head, once", async () => {
    const h = setup();
    bindArticles(h.env, articlesReaderFiles());
    const limited = setup({ brainApiLimiterAllows: false });
    bindArticles(limited.env);
    const pages: Array<[Harness, string]> = [
      [h, WIDGET_PATH], [h, `${WIDGET_PATH}?view=cards`], [h, "/articles"], [h, `/articles/universe/${FIXTURE_UNIVERSE_SHA}`],
      [h, "/articles/9999"], [limited, WIDGET_PATH], [setup(), "/articles"],
    ];
    for (const [env, path] of pages) {
      const html = await (await get(env.env, path)).text();
      const head = html.slice(0, html.indexOf("</head>"));
      expect(head.match(/<meta name="robots" content="noindex">/g), path).toHaveLength(1);
      expect(html.match(/<meta name="robots"/g), path).toHaveLength(1);
    }
  });
});

describe("the plane's licence statements", () => {
  const HISTORY = `https://en.wikipedia.org/w/index.php?curid=${WIDGET_PAGEID}&amp;action=history`;
  const REVISION = `https://en.wikipedia.org/w/index.php?curid=${WIDGET_PAGEID}&amp;oldid=2002`;
  const footer = (html: string) => html.slice(html.indexOf('<footer class="articles-foot'), html.indexOf("</footer>") + 9);

  it("the reader says it is an adaptation of the pinned revision under CC BY-SA 4.0, and what it changed", async () => {
    const h = setup();
    bindArticles(h.env, articlesReaderFiles());
    const foot = footer(await (await get(h.env, WIDGET_PATH)).text());
    expect(foot).toContain(`<p>This page is an adaptation of <a href="${REVISION}">revision 2002</a> of “Widget lemma” on ` +
      `<a href="https://en.wikipedia.org/">Wikipedia</a>, by <a href="${HISTORY}">its contributors</a>, and is shared under ` +
      `<a href="https://creativecommons.org/licenses/by-sa/4.0/" rel="license">CC BY-SA 4.0</a>, the revision&#x27;s licence.`);
    // The changes: the highlights, the cards and links added, the links pointed, the images removed,
    // and what the reader's stylesheet hides (articles-reader.css: .navbox, .mw-editsection).
    for (const change of ["highlights the revision&#x27;s formal passages", "adds a panel of Lean cards beside them with links to the Lean declarations",
      "points the revision&#x27;s links to Wikipedia", "removes the Wikimedia logos and any image that is not free",
      "hides the revision&#x27;s navigation boxes (rows of links to other articles) and any section edit links",
      "Images and media keep their own licences"]) {
      expect(foot).toContain(change);
    }
    const css = readFileSync(new URL("../../site/assets/articles-reader.css", import.meta.url), "utf8");
    expect(css).toMatch(/\.atlr-article \.navbox[^{]*\{ display: none; \}/);
    expect(css).toMatch(/\.atlr-article \.mw-editsection \{ display: none; \}/);
    // The mapping is CC0, apart from the text and the Lean code; only the page's own licence is rel="license".
    expect(foot).toContain('is released under <a href="https://creativecommons.org/publicdomain/zero/1.0/">CC0</a>');
    expect(foot).toContain("it is distinct from the article text, which keeps its own licence.");
    expect(foot).toContain("The Lean code on the cards is quoted from its libraries and keeps each library&#x27;s own licence, " +
      "which the universe page lists; the CC0 release does not cover it.");
    expect(foot.match(/rel="license"/g)).toHaveLength(1);
  });

  it("the card list credits the revision it quotes, and every other articles page the licence of its quotes", async () => {
    const h = setup();
    bindArticles(h.env, articlesReaderFiles());
    const cards = footer(await (await get(h.env, `${WIDGET_PATH}?view=cards`)).text());
    expect(cards).toContain(`<p>The passages shown are excerpts of <a href="${REVISION}">revision 2002</a> of “Widget lemma” on ` +
      `<a href="https://en.wikipedia.org/">Wikipedia</a>, by <a href="${HISTORY}">its contributors</a>, shared under ` +
      `<a href="https://creativecommons.org/licenses/by-sa/4.0/">CC BY-SA 4.0</a>.`);
    expect(cards).not.toContain('rel="license"');
    for (const path of ["/articles", `/articles/universe/${FIXTURE_UNIVERSE_SHA}`]) {
      const foot = footer(await (await get(h.env, path)).text());
      expect(foot, path).toContain('available under <a href="https://creativecommons.org/licenses/by-sa/4.0/">CC BY-SA 4.0</a>.');
      expect(foot, path).toContain("is released under <a href=\"https://creativecommons.org/publicdomain/zero/1.0/\">CC0</a>");
      expect(foot, path).toContain("The Lean code on the cards is quoted from its libraries and keeps each library&#x27;s own licence");
    }
    expect(cards).toContain("The Lean code on the cards is quoted from its libraries and keeps each library&#x27;s own licence");
  });

  it("the reader lists the images and media it shows, each linked to its file page with its author and licence", async () => {
    const h = setup();
    const files = articlesReaderFiles();
    const key = `articles/v1/articles/${WIDGET_PAGEID}.json`;
    const article = JSON.parse(files.get(key)!);
    article.reader.images = [
      { file: "Gadget_icon.svg", project: "en", licence: "Public domain", licence_url: null, author: null },
      { file: "Newton's_<b>telescope</b>.jpg", project: "commons", licence: "CC BY-SA 4.0",
        licence_url: "https://creativecommons.org/licenses/by-sa/4.0/", author: "A. <i>Widgetmaker</i>" },
      { file: "Spin.webm", project: "commons", licence: null },
      { file: "Lens.svg", project: "commons", licence: "CC BY-SA 3.0", licence_url: "http://creativecommons.org/licenses/by-sa/3.0/",
        author: "C. Lens" },
      { file: "Elsewhere.png", project: "de", licence: "PD" }, "x", null,
    ];
    files.set(key, JSON.stringify(article));
    bindArticles(h.env, files);
    const foot = footer(await (await get(h.env, WIDGET_PATH)).text());
    expect(foot).toContain("Images and media keep their own licences: each is listed below, linked to its file page, with its author where that page names one, and its licence.");
    // The author is text, the licence links its own page (https only), and a store without them names the file page.
    expect(foot).toContain('<details class="atlr-images"><summary>Images and media in this revision (4), each under its own licence</summary><ul>' +
      '<li><a href="https://en.wikipedia.org/wiki/File:Gadget_icon.svg">Gadget icon.svg</a> (English Wikipedia): Public domain</li>' +
      '<li><a href="https://commons.wikimedia.org/wiki/File:Newton&#x27;s_%3Cb%3Etelescope%3C%2Fb%3E.jpg">Newton\'s &lt;b&gt;telescope&lt;/b&gt;.jpg</a> by A. &lt;i&gt;Widgetmaker&lt;/i&gt; (Wikimedia Commons): ' +
      '<a href="https://creativecommons.org/licenses/by-sa/4.0/">CC BY-SA 4.0</a></li>' +
      '<li><a href="https://commons.wikimedia.org/wiki/File:Spin.webm">Spin.webm</a> (Wikimedia Commons): its licence on its file page</li>' +
      '<li><a href="https://commons.wikimedia.org/wiki/File:Lens.svg">Lens.svg</a> by C. Lens (Wikimedia Commons): CC BY-SA 3.0</li></ul></details>');
    expect(foot).not.toContain("Elsewhere");
    // Without the list the footer says where the licences are, and lists nothing.
    bindArticles(h.env, articlesReaderFiles());
    const plain = footer(await (await get(h.env, WIDGET_PATH)).text());
    expect(plain).toContain("Images and media keep their own licences, which their file pages on Wikimedia give.");
    expect(plain).not.toContain("atlr-images");
  });

  it("each article's JSON states its licences, and the pages name the one it gives the text", async () => {
    const h = setup();
    bindArticles(h.env, articlesReaderFiles());
    expect((await jsonOf(await get(h.env, `/api/articles/article/${WIDGET_PAGEID}`))).licence)
      .toEqual({ text: "CC-BY-SA-4.0", mapping: "CC0-1.0" });
    const withLicence = (licence: unknown): Map<string, string> => {
      const files = articlesReaderFiles();
      const key = `articles/v1/articles/${WIDGET_PAGEID}.json`;
      files.set(key, JSON.stringify({ ...JSON.parse(files.get(key)!), licence }));
      return files;
    };
    const cases: Array<[unknown, string]> = [
      [{ text: "CC-BY-SA-3.0", mapping: "CC0-1.0" }, '<a href="https://creativecommons.org/licenses/by-sa/3.0/" rel="license">CC BY-SA 3.0</a>'],
      [{ text: "GFDL-1.3<i>", mapping: "CC0-1.0" }, "shared under GFDL-1.3&lt;i&gt;, the revision"],
      [undefined, '<a href="https://creativecommons.org/licenses/by-sa/4.0/" rel="license">CC BY-SA 4.0</a>'],
      ["CC-BY-SA-3.0", '<a href="https://creativecommons.org/licenses/by-sa/4.0/" rel="license">CC BY-SA 4.0</a>'],
    ];
    for (const [licence, shown] of cases) {
      bindArticles(h.env, withLicence(licence));
      expect(footer(await (await get(h.env, WIDGET_PATH)).text()), JSON.stringify(licence)).toContain(shown);
    }
  });
});

describe("the plane says the links are preliminary, and what measured them", () => {
  const ABOUT = /<details class="articles-about"><summary>About this (?:data|build)<\/summary><ul class="articles-prov-list">(.*?)<\/ul><\/details>/s;
  // A made-up audit of the fixture build: its run, model, sample and two figures, one weighted.
  const AUDIT = {
    run_id: "audit-fx", operator: "alice", started: "2026-11-03T02:44:00Z", ended: "2026-11-03T05:07:00Z",
    models: [{ model: "model-z", model_name: "Model Z" }], blind: true, independent: false, items: 20,
    figures: [{ id: "strong", label: "Strong supporting cards judged right", share: 0.8, ci95: [0.62, 0.91],
      weights: "relation and library", n_eff: 12.5, drawn: 16, population: 400 },
    { id: "absences", label: "Absences judged right", share: 0.75, ci95: [0.301, 0.954], weights: null, n_eff: 4, drawn: 4,
      population: 300 }],
  };
  function audited(audit: unknown = AUDIT): Map<string, string> {
    const files = articlesReaderFiles();
    for (const key of ["articles/v1/current.json", `articles/v1/articles/${WIDGET_PAGEID}.json`]) {
      files.set(key, JSON.stringify({ ...JSON.parse(files.get(key)!), audit }));
    }
    return files;
  }

  it("every articles page says it once, in one line under its header, and never on a card", async () => {
    const h = setup();
    bindArticles(h.env, articlesReaderFiles());
    const limited = setup({ brainApiLimiterAllows: false });
    bindArticles(limited.env);
    const pages: Array<[Harness, string, number]> = [
      [h, WIDGET_PATH, 200], [h, `${WIDGET_PATH}?view=cards`, 200], [h, `/articles/${GADGET_PAGEID}`, 200], [h, "/articles", 200],
      [h, `/articles/universe/${FIXTURE_UNIVERSE_SHA}`, 200], [h, "/articles/9999", 404], [limited, WIDGET_PATH, 429],
      [setup(), "/articles", 503],
    ];
    for (const [env, path, status] of pages) {
      const res = await get(env.env, path);
      expect(res.status, path).toBe(status);
      const html = await res.text();
      // The reader heads its article column with it (beside the sticky panel, not above it);
      // every other page has it right after the header.
      const line = `<p class="articles-notice" id="articles-notice">${ARTICLES_NOTICE}</p>\n`;
      const reader = path === WIDGET_PATH && status === 200;
      expect(html, path).toContain(reader ? `</header>\n<div class="atlr-layout"` : `</header>\n${line}`);
      if (reader) expect(html, path).toContain(`<div class="atlr-head">\n${line}<nav class="articles-crumbs"`);
      expect(html.match(/Preliminary/g), path).toHaveLength(1);
      expect(html.match(/independent audit/g), path).toHaveLength(1);
    }
    expect(ARTICLES_NOTICE).toBe("Preliminary: these links have not yet had an independent audit.");
  });

  it("without an audit summary, About this data says plainly that the build carries no accuracy figure", async () => {
    const h = setup();
    bindArticles(h.env, articlesReaderFiles());
    for (const path of [WIDGET_PATH, `${WIDGET_PATH}?view=cards`, "/articles"]) {
      const about = ABOUT.exec(await (await get(h.env, path)).text());
      expect(about, path).not.toBeNull();
      expect(about![1], path).toContain(`<li>${UNMEASURED}</li>`);
      expect(about![1], path).not.toContain("Accuracy, from an audit");
    }
    // A statement about the build, which says nothing of whether an audit of its links exists.
    expect(UNMEASURED).toBe("This build carries no accuracy figure from an audit of its links.");
  });

  it("with an audit summary, About this data gives each figure and cites the run that measured it", async () => {
    const h = setup();
    bindArticles(h.env, audited());
    const lines = [
      "<li>Accuracy, from an audit of a sample of 20 items (blind, not independent): Strong supporting cards judged right: " +
        "80% (95% interval 62–91%), from 16 drawn of 400, weighted by relation and library, an effective sample of 12.5; " +
        "Absences judged right: 75% (95% interval 30–95%), from 4 drawn of 300.</li>",
      `<li>Audit run <code>audit-fx</code>, run by alice, 3 Nov 2026, 02:44–05:07 UTC: judged by Model Z <span class="articles-nowrap">(model-z)</span>.</li>`,
    ];
    for (const path of [WIDGET_PATH, `${WIDGET_PATH}?view=cards`, "/articles"]) {
      const html = await (await get(h.env, path)).text();
      const about = ABOUT.exec(html);
      expect(about, path).not.toBeNull();
      expect(about![1].startsWith(lines.join("")), path).toBe(true);
      expect(about![1], path).not.toContain(UNMEASURED);
      // The notice stays: the line under the header states the data's audit status, not the figure.
      expect(html, path).toContain(`<p class="articles-notice" id="articles-notice">${ARTICLES_NOTICE}</p>`);
      // The figures are in the fold only, never on a card or a passage.
      expect(html.match(/judged right/g), path).toHaveLength(2);
    }
    // A malformed audit reads as none, and its text never reaches the page.
    for (const bad of [{ ...AUDIT, items: "20" }, { ...AUDIT, models: "Model Z" }, { ...AUDIT, run_id: 7 }, "audit", null]) {
      bindArticles(h.env, audited(bad));
      const about = ABOUT.exec(await (await get(h.env, WIDGET_PATH)).text());
      expect(about![1]).toContain(`<li>${UNMEASURED}</li>`);
    }
    // A figure is printed escaped, and one that is no estimate (a share outside its interval, or none)
    // is left out; without both flags the run's standing is not said.
    const figure = { share: 0.5, ci95: [0.2, 0.8], weights: null, n_eff: 2, drawn: 2, population: 9 };
    bindArticles(h.env, audited({ ...AUDIT, blind: "yes", figures: [{ ...figure, id: "x", label: "<b>bold</b>" },
      { ...figure, id: "y", label: "Off", share: 0.9 }, { ...figure, id: "z", label: "Counted", share: undefined, right: 1, of: 2 }] }));
    const about = ABOUT.exec(await (await get(h.env, WIDGET_PATH)).text())![1];
    expect(about).toContain("from an audit of a sample of 20 items: &lt;b&gt;bold&lt;/b&gt;: 50% (95% interval 20–80%), from 2 drawn of 9.");
    expect(about).not.toContain("Off:");
    expect(about).not.toContain("Counted:");
  });
});

describe("the WikiLean slug lookup (migration 0016 indexes articles.wikipedia_title)", () => {
  it("with migration 0016 the lookup reads the matching rows through the index, not the table, and a re-run is a no-op", () => {
    const src = readFileSync(new URL("../src/articles.ts", import.meta.url), "utf8");
    const query = "SELECT slug FROM articles WHERE wikipedia_title IN (?, ?) ORDER BY slug LIMIT 1";
    expect(src).toContain(`"${query}"`);
    const migration = readFileSync(new URL("../migrations/0016_articles_wikipedia_title.sql", import.meta.url), "utf8");
    const plan = (db: DatabaseSync) => (db.prepare(`EXPLAIN QUERY PLAN ${query}`).all("a", "b") as Array<{ detail: string }>)
      .map((row) => row.detail).join("\n");
    // A database before it scans every article; with it (the harness applies every migration) the
    // lookup searches the index.
    const before = new DatabaseSync(":memory:");
    before.exec(readFileSync(new URL("../migrations/0001_init.sql", import.meta.url), "utf8"));
    expect(plan(before)).toMatch(/^SCAN articles/m);
    before.exec(migration);
    before.exec(migration);
    expect(plan(before)).toContain("SEARCH articles USING INDEX idx_articles_wikipedia_title (wikipedia_title=?)");
    before.close();
    const h = setup();
    expect(plan(h.db as unknown as DatabaseSync)).toContain("SEARCH articles USING INDEX idx_articles_wikipedia_title (wikipedia_title=?)");
  });

  // Every D1 statement the harness's database prepares, and the ones that look a title up.
  function watchSql(h: Harness): { all: string[]; slugs: () => number } {
    const all: string[] = [];
    const prepare = h.env.DB.prepare.bind(h.env.DB);
    Object.assign(h.env.DB, { prepare: (query: string) => { all.push(query); return prepare(query); } });
    return { all, slugs: () => all.filter((query) => query.includes("wikipedia_title")).length };
  }

  it("an article page read again within a minute costs no D1 read, with or without a WikiLean article", async () => {
    const h = bound();
    h.db.prepare("UPDATE articles SET wikipedia_title = 'Widget lemma' WHERE slug = ?").run(SLUG);
    const sql = watchSql(h);
    for (let i = 0; i < 3; i += 1) {
      const html = await (await get(h.env, WIDGET_PATH)).text();
      expect(html).toContain(`<a href="/${SLUG}">Annotated article on WikiLean</a>`);
    }
    expect(sql.slugs()).toBe(1);
    // An anonymous view reads nothing else from D1: the page read again costs none.
    expect(sql.all).toHaveLength(1);
    // The Gadget has no WikiLean article: that answer is kept too.
    for (let i = 0; i < 3; i += 1) {
      const html = await (await get(h.env, `/articles/${GADGET_PAGEID}/Gadget_(mathematics)`)).text();
      expect(html).not.toContain("Annotated article on WikiLean");
    }
    expect(sql.slugs()).toBe(2);
    expect(sql.all).toHaveLength(2);
    // The reader and the card list of one article share it.
    const reader = setup();
    bindArticles(reader.env, articlesReaderFiles());
    const watched = watchSql(reader);
    for (const path of [WIDGET_PATH, `${WIDGET_PATH}?view=cards`, `/articles/${WIDGET_PAGEID}`]) {
      expect((await get(reader.env, path)).status, path).toBe(200);
    }
    expect(watched.slugs()).toBe(1);
  });

  it("is read again after SLUG_CACHE_MS, so a new WikiLean article gets its link within a minute", async () => {
    const h = setup();
    const sql = watchSql(h);
    const t0 = 1_000_000;
    expect(SLUG_CACHE_MS).toBe(INDEX_CACHE_MS);
    expect(await wikileanSlugFor(h.env, "Widget lemma", t0)).toBeNull();
    h.db.prepare("UPDATE articles SET wikipedia_title = 'Widget_lemma' WHERE slug = ?").run(SLUG);
    // Within the minute the remembered answer stands; the title's spellings share one entry.
    expect(await wikileanSlugFor(h.env, "Widget_lemma", t0 + SLUG_CACHE_MS - 1)).toBeNull();
    expect(await wikileanSlugFor(h.env, " Widget lemma ", t0 + 1)).toBeNull();
    expect(sql.slugs()).toBe(1);
    expect(await wikileanSlugFor(h.env, "Widget lemma", t0 + SLUG_CACHE_MS)).toBe(SLUG);
    expect(sql.slugs()).toBe(2);
    // A clock that went back reads again rather than trusting the entry.
    expect(await wikileanSlugFor(h.env, "Widget lemma", t0)).toBe(SLUG);
    expect(sql.slugs()).toBe(3);
  });

  it("keeps at most MAX_SLUG_CACHE titles, dropping the one read longest ago", async () => {
    const h = setup();
    const sql = watchSql(h);
    const now = 5_000_000;
    for (let i = 0; i <= MAX_SLUG_CACHE; i += 1) await wikileanSlugFor(h.env, `Title ${i}`, now);
    expect(sql.slugs()).toBe(MAX_SLUG_CACHE + 1);
    // One more than it holds: the first title in was dropped. The others are kept, and a read
    // moves a title last ("Title 1", the oldest, becomes the newest).
    await wikileanSlugFor(h.env, `Title ${MAX_SLUG_CACHE}`, now);
    await wikileanSlugFor(h.env, "Title 1", now);
    expect(sql.slugs()).toBe(MAX_SLUG_CACHE + 1);
    // "Title 0" is read again, and in its place goes the title read longest ago: "Title 2", not
    // "Title 1", which was just read.
    await wikileanSlugFor(h.env, "Title 0", now);
    expect(sql.slugs()).toBe(MAX_SLUG_CACHE + 2);
    await wikileanSlugFor(h.env, "Title 1", now);
    expect(sql.slugs()).toBe(MAX_SLUG_CACHE + 2);
    await wikileanSlugFor(h.env, "Title 2", now);
    expect(sql.slugs()).toBe(MAX_SLUG_CACHE + 3);
  });

  it("is kept per D1 binding, and a failed lookup is not remembered", async () => {
    const a = setup();
    const b = setup();
    b.db.prepare("UPDATE articles SET wikipedia_title = 'Widget lemma' WHERE slug = ?").run(SLUG);
    expect(await wikileanSlugFor(a.env, "Widget lemma")).toBeNull();
    expect(await wikileanSlugFor(b.env, "Widget lemma")).toBe(SLUG);
    const failing = setup();
    const prepare = failing.env.DB.prepare.bind(failing.env.DB);
    let fail = true;
    Object.assign(failing.env.DB, {
      prepare: (query: string) => {
        if (fail) throw new Error("D1 unavailable");
        return prepare(query);
      },
    });
    await expect(wikileanSlugFor(failing.env, "Widget lemma", 7_000)).rejects.toThrow("D1 unavailable");
    fail = false;
    failing.db.prepare("UPDATE articles SET wikipedia_title = 'Widget lemma' WHERE slug = ?").run(SLUG);
    expect(await wikileanSlugFor(failing.env, "Widget lemma", 7_001)).toBe(SLUG);
  });
});

describe("articles store reader", () => {
  it("title keys and shards match scripts/articles/build_store.py", async () => {
    expect(titleKey("Widget_lemma")).toBe("Widget lemma");
    expect(titleKey("  Gadget (mathematics) ")).toBe("Gadget (mathematics)");
    expect(titleKey("A B__C")).toBe("A B C");
    // Same values scripts/articles/test_build_store.py pins.
    expect(await titleShard("Widget lemma")).toBe("1");
    expect(await titleShard("Gadget (mathematics)")).toBe("8");
    expect(articlesHref(1003, "Gadget (mathematics)")).toBe("/articles/1003/Gadget_(mathematics)");
  });

  it("looks titles up through one shard, in any of the three backends", async () => {
    const files = articlesFixtureFiles();
    const memory = memoryReader(files);
    expect((await lookupTitle(memory, "Widget_lemma"))?.pageid).toBe(WIDGET_PAGEID);
    expect(await lookupTitle(memory, "Sylow theorems")).toBeNull();
    expect(await lookupTitle(memory, "   ")).toBeNull();
    const kv = kvReader({ get: (async (key: string) => files.get(key) ?? null) as KVNamespace["get"] });
    expect((await loadArticle(kv, GADGET_PAGEID))?.title).toBe("Gadget (mathematics)");
    const assets = staticReader({
      fetch: (async (req: Request) => {
        const path = new URL(req.url).pathname.replace(/^\/articles-store\//, "");
        const body = files.get(path);
        return body === undefined ? new Response("nope", { status: 404 }) : new Response(body);
      }) as unknown as Fetcher["fetch"],
    });
    expect((await loadArticle(assets, WIDGET_PAGEID))?.campaign).toBe(FIXTURE_CAMPAIGN);
    expect(await loadArticle(assets, 9999)).toBeNull();
  });

  it("refuses a store object of the wrong schema", async () => {
    const files = articlesFixtureFiles();
    files.set(`articles/v1/articles/${WIDGET_PAGEID}.json`, JSON.stringify({ schema: "something/else" }));
    await expect(loadArticle(memoryReader(files), WIDGET_PAGEID)).rejects.toThrow("is not wikilean.articles.article/1");
  });

  it("parses page ids strictly", () => {
    expect(parsePageid("1001")).toBe(1001);
    for (const raw of ["0", "01", "-1", "1e3", "abc", "", "99999999999"]) expect(parsePageid(raw), raw).toBeNull();
  });
});

describe("library registry", () => {
  const COMMIT = "a".repeat(40);

  it("names seeded, FormalFrontier and other GitHub repositories; a universe never relabels a seeded library", () => {
    expect(libraryInfo("https://github.com/leanprover-community/mathlib4").name).toBe("Mathlib");
    expect(libraryInfo("https://github.com/leanprover-community/mathlib4.git/").name).toBe("Mathlib");
    expect(libraryInfo("https://github.com/TauCetiProject/TauCeti").name).toBe("TauCeti");
    expect(libraryInfo("https://github.com/leanprover-community/batteries").name).toBe("Batteries");
    expect(libraryInfo("https://github.com/google-deepmind/formal-conjectures").name).toBe("Formal Conjectures");
    expect(libraryInfo("https://github.com/facebookresearch/atlas-lean").name).toBe("ATLAS (atlas-lean)");
    expect(libraryInfo("https://github.com/facebookresearch/atlas-lean").short).toBe("ATLAS");
    expect(libraryInfo("https://github.com/FormalFrontier/WidgetTheory").name).toBe("FormalFrontier · WidgetTheory");
    expect(libraryInfo("https://github.com/someone/lean-thing").name).toBe("someone/lean-thing");
    // A seeded repository keeps the registry's name whatever the universe says.
    const core = { id: "lean-core", repo: "https://github.com/leanprover/lean4", commit: COMMIT, display_name: "Lean 4 core (Init, Std, Lean)" };
    expect(libraryInfo(core.repo).name).toBe("Lean core");
    expect(libraryInfo(core.repo, core).name).toBe("Lean core");
    // An unseeded repository may carry a display name, unless it names a seeded library
    // ("ATLAS" below is facebookresearch's seeded library, which keeps its name).
    const other = { id: "x", repo: "https://github.com/someone/lean-thing", commit: COMMIT };
    expect(libraryInfo(other.repo, { ...other, display_name: "  Lean   Thing (fork) " }).name).toBe("Lean Thing (fork)");
    expect(libraryInfo(other.repo, { ...other, display_name: "Lean Thing (fork)" }).short).toBe("Lean Thing");
    for (const display of ["Mathlib", "mathlib", " MATHLIB ", "Mathlib (official)", "Batteries", "ATLAS", "Lean core", "FormalFrontier · WidgetTheory"]) {
      expect(libraryInfo(other.repo, { ...other, display_name: display }).name, display).toBe("someone/lean-thing");
    }
  });

  it("libraries that would read alike are told apart by their roots, else by their ids", () => {
    const lean4 = "https://github.com/leanprover/lean4";
    const names = (entries: Parameters<typeof libraryInfos>[0]) =>
      Object.fromEntries([...libraryInfos(entries)].map(([id, info]) => [id, [info.name, info.short]]));
    // Lean core split into Init and Std at one commit (proposal §2.3), beside Mathlib.
    expect(names([
      { id: "init", repo: lean4, roots: ["Init"] },
      { id: "mathlib", repo: "https://github.com/leanprover-community/mathlib4", roots: ["Mathlib"] },
      { id: "std", repo: lean4, roots: ["Std"], display_name: "Std" },
    ])).toEqual({
      init: ["Lean core · Init", "Lean core · Init"], mathlib: ["Mathlib", "Mathlib"], std: ["Lean core · Std", "Lean core · Std"],
    });
    // A Lake glob is named without its suffix, and several roots together.
    expect(names([
      { id: "fc", repo: "https://github.com/google-deepmind/formal-conjectures", roots: ["FormalConjectures.+"] },
      { id: "fc-lib", repo: "https://github.com/google-deepmind/formal-conjectures", roots: ["FormalConjecturesForMathlib", "Util.*"] },
    ])).toEqual({
      fc: ["Formal Conjectures · FormalConjectures", "Formal Conjectures · FormalConjectures"],
      "fc-lib": ["Formal Conjectures · FormalConjecturesForMathlib, Util", "Formal Conjectures · FormalConjecturesForMathlib, Util"],
    });
    // Roots that are not plain module names, or that do not tell the libraries
    // apart, give way to the ids; a library with no roots (a location's, when the
    // universe could not be read) is named by its id too.
    for (const [a, b] of [[["<b>Init</b>"], ["Std"]], [["Init"], ["init"]], [["Init"], undefined], [[], ["Std"]]]) {
      expect(names([{ id: "core-a", repo: lean4, roots: a }, { id: "core-b", repo: lean4, roots: b }]), JSON.stringify([a, b]))
        .toEqual({ "core-a": ["Lean core · core-a", "Lean core · core-a"], "core-b": ["Lean core · core-b", "Lean core · core-b"] });
    }
    // One library of a repository keeps its plain name; the first of a repeated id wins.
    expect(names([{ id: "lean-core", repo: lean4, roots: ["Init", "Std"] }, { id: "lean-core", repo: lean4, roots: ["Lean"] }]))
      .toEqual({ "lean-core": ["Lean core", "Lean core"] });
    // The base name is the repository's: a root never relabels a library.
    expect(names([
      { id: "a", repo: "https://github.com/someone/lean-thing", roots: ["Mathlib"] },
      { id: "b", repo: "https://github.com/someone/lean-thing", roots: ["Thing"] },
    ]).a).toEqual(["someone/lean-thing · Mathlib", "someone/lean-thing · Mathlib"]);
  });

  it("a display_name cannot read as a seeded library through invisible or look-alike characters", () => {
    const repo = "https://github.com/evil-org/lean-stuff";
    const entry = { id: "x", repo, commit: COMMIT };
    // "Atlas" spoofs the seeded ATLAS (atlas-lean), facebookresearch's library, not this plane.
    const spoofs = [
      "Mathlib\u200b", "M\u0430thlib", "Mathlib\u00ad", "Batteries\u200b", "Lean core\u2060", "\ufeffMathlib",
      "Mathlib4", "Mathlib 4", "MathIib", "Math1ib", "Math-lib", "mathlib4 (leanprover-community)",
      "leanprover-community/mathlib4", "Tau Ceti", "Formal Conjectures fork", "Atlas", "Lean\tcore",
      "Formal Frontier tools", "Bat-teries (official)", "x".repeat(101),
    ];
    for (const display_name of spoofs) {
      const info = libraryInfo(repo, { ...entry, display_name });
      expect([info.name, info.short], JSON.stringify(display_name)).toEqual(["evil-org/lean-stuff", "evil-org/lean-stuff"]);
    }
    for (const display_name of ["Lean Thing (fork)", "Widget theory (FF · fork), v2", "Category theory: extras"]) {
      expect(libraryInfo(repo, { ...entry, display_name }).name, display_name).toBe(display_name);
    }
    // A FormalFrontier repository may say so; another may not.
    const ff = { id: "w", repo: "https://github.com/FormalFrontier/WidgetTheory", commit: COMMIT };
    expect(libraryInfo(ff.repo, { ...ff, display_name: "FormalFrontier · Widgets" }).name).toBe("FormalFrontier · Widgets");
    expect(libraryInfo(ff.repo, { ...ff, display_name: "Mathlib widgets" }).name).toBe("FormalFrontier · WidgetTheory");
  });

  it("builds pinned GitHub source links only, from repository-relative paths, and refuses unsafe parts", () => {
    const mathlib = libraryInfo("https://github.com/leanprover-community/mathlib4");
    expect(sourceUrl(mathlib, { commit: COMMIT, path: "Mathlib/A B.lean", line: 3, end_line: 9 }))
      .toBe(`https://github.com/leanprover-community/mathlib4/blob/${COMMIT}/Mathlib/A%20B.lean#L3-L9`);
    expect(sourceUrl(mathlib, { commit: COMMIT, path: "Mathlib/A.lean", line: 3, end_line: 3 }))
      .toBe(`https://github.com/leanprover-community/mathlib4/blob/${COMMIT}/Mathlib/A.lean#L3`);
    // end_line is optional: without it the link names the one line.
    expect(sourceUrl(mathlib, { commit: COMMIT, path: "Mathlib/A.lean", line: 81 }))
      .toBe(`https://github.com/leanprover-community/mathlib4/blob/${COMMIT}/Mathlib/A.lean#L81`);
    // A path is relative to the repository root, so Lean core's records say
    // src/Init/..., and nothing is prefixed (no src/src/...).
    const core = libraryInfo("https://github.com/leanprover/lean4");
    expect(sourceUrl(core, { commit: COMMIT, path: "src/Init/Core.lean", line: 1, end_line: 2 }))
      .toBe(`https://github.com/leanprover/lean4/blob/${COMMIT}/src/Init/Core.lean#L1-L2`);
    // A universe's blob_url_template is ignored: a non-GitHub library gets no
    // link, and a GitHub one keeps GitHub's blob scheme for its own repository.
    const custom = libraryInfo("https://git.example.org/lib", {
      id: "x", repo: "https://git.example.org/lib", commit: COMMIT,
      blob_url_template: "https://git.example.org/lib/-/blob/{commit}/{path}#L{line}",
    });
    expect(sourceUrl(custom, { commit: COMMIT, path: "X.lean", line: 4, end_line: 8 })).toBeNull();
    expect(sourceUrl(libraryInfo("https://git.example.org/lib"), { commit: COMMIT, path: "X.lean", line: 1, end_line: 1 })).toBeNull();
    const phish = libraryInfo("https://github.com/leanprover-community/mathlib4", {
      id: "mathlib", repo: "https://github.com/leanprover-community/mathlib4", commit: COMMIT,
      display_name: "Mathlib", blob_url_template: "https://phish.example/login?next={commit}{path}",
    });
    expect(sourceUrl(phish, { commit: COMMIT, path: "A.lean", line: 1, end_line: 1 }))
      .toBe(`https://github.com/leanprover-community/mathlib4/blob/${COMMIT}/A.lean#L1`);
    // "." and ".." are not owner or repository names.
    for (const repo of ["https://github.com/../..", "https://github.com/a/..", "https://github.com/./b"]) {
      const info = libraryInfo(repo);
      expect(info.blobTemplate, repo).toBeNull();
      expect(sourceUrl(info, { commit: COMMIT, path: "A.lean", line: 1, end_line: 1 }), repo).toBeNull();
      expect(treeUrl(info, COMMIT), repo).toBeNull();
      expect(repoHref(repo), repo).toBeNull();
    }
    // Only a github.com/<owner>/<repo> repository is a link (the universe page).
    expect(repoHref("https://git.example.org/lib")).toBeNull();
    expect(repoHref("https://evil.example.com/phish")).toBeNull();
    expect(repoHref("https://github.com/a/b/c")).toBeNull();
    expect(repoHref("https://github.com/a/b")).toBe("https://github.com/a/b");
    expect(repoHref("https://github.com/a/b?x=1")).toBeNull();
    expect(repoHref("http://github.com/a/b")).toBeNull();
    expect(treeUrl(libraryInfo("https://github.com/a/b"), COMMIT)).toBe(`https://github.com/a/b/tree/${COMMIT}`);
    for (const path of ["../x.lean", "/abs.lean", "a//b.lean", "a\\b.lean", "a/./b.lean", ""]) {
      expect(sourceUrl(mathlib, { commit: COMMIT, path, line: 1, end_line: 1 }), path).toBeNull();
    }
    expect(sourceUrl(mathlib, { commit: "HEAD", path: "Mathlib/A.lean", line: 1, end_line: 1 })).toBeNull();
    expect(libraryInfo("https://github.com/a/b", { id: "b", repo: "https://github.com/a/b", commit: COMMIT, blob_url_template: "javascript:{commit}{path}" }).blobTemplate)
      .toBe("{repo}/blob/{commit}/{path}#L{line}-L{end_line}");
    expect(libraryInfo("https://github.com/FormalFrontier/X").name).toBe("FormalFrontier · X");
    expect(libraryInfo("https://github.com/formalfrontier/X").name).toBe("FormalFrontier · X");
  });
});

describe("derived status rows", () => {
  // scripts/articles/fixtures/producer_derive_status.json: formal-companion's own
  // cflib.vocab.derive_status answers (make_producer_table.py). The page's
  // fallback for a store without `basis` must read the table the same way.
  const table = JSON.parse(readFileSync(`${ARTICLES_STORE_ROOT}/../producer_derive_status.json`, "utf8")) as {
    cases: Array<[string, Array<[string, string, boolean]>, string | null, unknown?]>;
  };
  const rank: Record<string, number> = { formalized: 0, statement_only: 1, partial: 2 };

  it("locationStatus gives the producer's status for every case it does not refuse", () => {
    const wrong: unknown[] = [];
    // A coverage case (a fourth element, decision D1) is decided by the claim, which the
    // builder folds into the store's status and basis; the page's per-location fallback
    // never sees one, so only the cases without a claim apply here.
    const plain = table.cases.filter((c) => c.length === 3);
    for (const [kind, ends, expected] of plain) {
      if (expected === "refused") continue;
      const statuses = ends
        .map(([relation, proof_status, statement]) => locationStatus(kind, { relation, proof_status, statement } as never))
        .filter((s): s is "formalized" | "statement_only" | "partial" => s !== null);
      const best = statuses.length ? statuses.reduce((a, b) => (rank[b] < rank[a] ? b : a)) : null;
      if (best !== expected) wrong.push([kind, ends, expected, best]);
    }
    expect(plain.length).toBeGreaterThan(700);
    // The table's producer implements D1: a theorem's exact match to data formalizes it.
    expect(plain).toContainEqual(["theorem", [["exactMatch", "definition", false]], "formalized"]);
    expect(wrong).toEqual([]);
  });

  it("locationStatus gives the shared table's status for every claim-free case it does not refuse", () => {
    // scripts/articles/fixtures/status_parity.json: the hand-written parity table both sides run
    // (formal-companion's tests/data/locate/status-parity.json, byte for byte). As above, only a
    // located case without a coverage claim reaches the page's per-location fallback.
    const shared = JSON.parse(readFileSync(`${ARTICLES_STORE_ROOT}/../status_parity.json`, "utf8")) as {
      cases: Array<{
        id: string; passage_kind: string; absent: boolean; coverage: unknown; refused?: boolean;
        expected?: string | null; locations: Array<{ relation: string; proof_status: string; statement: boolean }>;
      }>;
    };
    const plain = shared.cases.filter((c) => !c.refused && !c.absent && c.coverage === null);
    const wrong: unknown[] = [];
    for (const c of plain) {
      const statuses = c.locations
        .map((loc) => locationStatus(c.passage_kind, loc as never))
        .filter((s): s is "formalized" | "statement_only" | "partial" => s !== null);
      const best = statuses.length ? statuses.reduce((a, b) => (rank[b] < rank[a] ? b : a)) : null;
      if (best !== c.expected) wrong.push([c.id, c.expected, best]);
    }
    expect(plain.length).toBeGreaterThanOrEqual(30);
    expect(wrong).toEqual([]);
  });
});

// The article reader (build_store --reader-html; docs/ARTICLES.md, "Reader"): with the store's
// marked revision the article route serves the article itself, its passages highlighted, and a
// panel holding exactly the card list's cards; ?view=cards is the card list with a view switch;
// a store without the revision, or with bytes the view does not name, serves the card list as
// it always was.
describe("the article reader", () => {
  function withReader(opts: Parameters<typeof articlesReaderFiles>[0] = {}): Harness & { gets: string[] } {
    const h = setup();
    const bucket = bindArticles(h.env, articlesReaderFiles(opts));
    return { ...h, gets: bucket.gets };
  }
  // Every location card of a page, in order, as markup.
  const cards = (html: string) => [...html.matchAll(/<article class="articles-card"[\s\S]*?<\/article>/g)].map((m) => m[0]);
  // The panel section of one passage.
  function section(html: string, hex: string): string {
    const start = html.indexOf(`<section class="articles-passage atlr-pp`);
    const at = html.indexOf(`id="atlr-pp-${hex}"`, start);
    expect(at, `a panel section for ${hex}`).toBeGreaterThan(-1);
    const open = html.lastIndexOf("<section", at);
    return html.slice(open, html.indexOf("</section>", at) + "</section>".length);
  }

  it("serves the pinned revision as the page, verbatim, with the reader's stylesheet and script", async () => {
    const h = withReader();
    const res = await get(h.env, WIDGET_PATH);
    expect(res.status).toBe(200);
    const html = await res.text();
    expect(html).toContain(`<article class="wl-article-body atlr-article" id="atlr-article" lang="en"`);
    expect(html).toContain(readerHtml());
    expect(html).toContain(`<link rel="stylesheet" href="${ARTICLES_READER_CSS}">`);
    expect(html).toContain(`<script src="${ARTICLES_JS}"></script>\n<script src="${ARTICLES_READER_JS}"></script>`);
    expect(html).toContain(`<body class="articles-body atlr-body">\n<a class="atlr-skip" href="#atlr-panel">Skip to the passage panel</a>`);
    // The view switch, the article current; the card list one link away.
    expect(html).toContain(`<nav class="atlr-switch" aria-label="View"><a id="atlr-to-article" href="${WIDGET_PATH}" aria-current="page">Article</a>` +
      `<a id="atlr-to-cards" href="${WIDGET_PATH}?view=cards">Card list</a></nav>`);
    // The filters of the card list (status, library) and central only, in the header.
    expect(html).toContain('<button type="button" class="articles-chip" data-filter="status:formalized"');
    expect(html).toContain('<button type="button" class="articles-chip" data-filter="library:mathlib"');
    expect(html).toContain('data-filter="central"');
    // The footer credits the revision's text, by its history, under its licence.
    expect(html).toContain(`This page is an adaptation of <a href="https://en.wikipedia.org/w/index.php?curid=${WIDGET_PAGEID}&amp;oldid=2002">revision 2002</a> of “Widget lemma”`);
    expect(html).toContain(`https://en.wikipedia.org/w/index.php?curid=${WIDGET_PAGEID}&amp;action=history`);
    expect(html).toContain("CC BY-SA 4.0");
    expect(h.gets).toContain(`articles/v1/reader/${WIDGET_PAGEID}.html`);
  });

  it("gives each passage a hidden panel section with exactly the card list's cards, Lean code first and Details closed", async () => {
    const h = withReader();
    const reader = await (await get(h.env, WIDGET_PATH)).text();
    const list = await (await get(h.env, `${WIDGET_PATH}?view=cards`)).text();
    expect(cards(reader).length).toBeGreaterThan(0);
    expect(cards(reader).sort()).toEqual(cards(list).sort());
    for (const hex of READER_ORDER) {
      const s = section(reader, hex);
      expect(s).toMatch(new RegExp(`^<section class="articles-passage atlr-pp[^"]*" id="atlr-pp-${hex}" data-p="${hex}" data-status="[a-z_]+"`));
      expect(s).toContain(" hidden>");
      expect(s).toContain(`<h3 class="articles-p-h" id="atlr-pp-h-${hex}" tabindex="-1">`);
      expect(s).not.toContain("<details class=\"articles-prov\" open");
      expect(s).toContain(`<a href="${WIDGET_PATH}?view=cards#p-${hex}">Open in card list</a>`);
      expect(s).toContain(`<button type="button" class="atlr-copy" data-p="${hex}">Copy link</button>`);
    }
    // The subject theorem: its Mathlib card leads with the declaration, then the module, then the library.
    const subject = section(reader, "3fa91c07");
    expect(subject).toContain('data-lead="Widget.exists_spanning_gadget" data-lead-lib="Mathlib" data-more="1" data-rel="exact match"');
    expect(subject).toContain('data-first="every finite widget admits a spanning gadget"');
    const card = cards(subject)[0];
    expect(card.indexOf('<p class="articles-decl-line">')).toBeLessThan(card.indexOf('<p class="articles-module">'));
    expect(card.indexOf('<p class="articles-module">')).toBeLessThan(card.indexOf('<span class="articles-lib"'));
    expect(card).toContain('<details class="articles-prov"><summary>Details</summary>');
    // The absence says what was searched; the panel's text is clamped behind "Show all".
    expect(section(reader, "91aa02de")).toContain('data-searched="5"');
    expect(subject).toContain('<div class="atlr-clamp"><blockquote class="articles-quote"');
    expect(subject).toContain('<button type="button" class="atlr-more" aria-expanded="false" hidden>Show all</button>');
    // The slot is the Confirm / Report root articles.js binds (data-pageid, data-campaign).
    expect(reader).toContain(`<div class="atlr-slot" id="articles-article" data-pageid="${WIDGET_PAGEID}" data-campaign="${FIXTURE_CAMPAIGN}">`);
    // Related declarations are folded under their own summary.
    expect(section(reader, "91aa02de")).toContain('<details class="atlr-related"><summary class="articles-related-h">Related, but states something else (1)</summary>');
  });

  it("lists the passages in the article's reading order, under its section headings", async () => {
    const h = withReader();
    const html = await (await get(h.env, WIDGET_PATH)).text();
    const rows = [...html.matchAll(/<a class="atlr-row" href="#p-([0-9a-f]+)" data-p="\1"/g)].map((m) => m[1]);
    expect(rows).toEqual(READER_ORDER);
    const sections = [...html.matchAll(/<section class="articles-passage atlr-pp[^"]*" id="atlr-pp-([0-9a-f]+)"/g)].map((m) => m[1]);
    expect(sections).toEqual(READER_ORDER);
    // The conjecture, with no status, is set aside: no row, no section, no heading of its own.
    expect([...html.matchAll(/<h3 class="atlr-group-h">([^<]*)<\/h3>/g)].map((m) => m[1]))
      .toEqual(["Statement", "Definitions", "Statement › Bounds"]);
    expect(html).toContain('<a class="atlr-row" href="#p-91aa02de" data-p="91aa02de" data-status="not_formalized">');
    expect(html).toContain("not found</span></a>");
  });

  it("names a passage the revision leaves unmarked, and still gives it its panel", async () => {
    const h = withReader({ omit: ["5b0e44a1"] });
    const html = await (await get(h.env, WIDGET_PATH)).text();
    expect(html).not.toContain('data-p="5b0e44a1" data-status="statement_only" id="p-5b0e44a1"');
    expect(html).toContain('<span class="atlr-row-note">not marked in the text</span>');
    expect(html).toContain("(1 passage is listed in the panel only)");
    expect(section(html, "5b0e44a1")).toContain('data-status="statement_only"');
  });

  it("a passage with no status is neither highlighted, listed nor counted, and its legend has no such highlight", async () => {
    const h = withReader();
    const html = await (await get(h.env, WIDGET_PATH)).text();
    // The revision states it as plain text: no mark, no row, no panel section, no status anywhere.
    expect(html).toContain("<p>It is conjectured that every balanced widget is the image of a regular one.</p>");
    expect(html).not.toContain('data-p="0c4d77b1"');
    expect(html).not.toContain('id="atlr-pp-0c4d77b1"');
    expect(html).not.toMatch(/Undecided|undecided|data-status="unresolved"/);
    expect(html).toContain("3 passages checked, 3 Lean locations (and 1 related declaration). " +
      '<span class="articles-muted" id="articles-unread">1 more passage has no status yet; it is left out until someone reads it.</span>');
    expect(html).toContain('aria-label="3 passages: 1 formalized, 1 statement only, 1 not formalized"');
    expect(html).not.toContain("listed in the panel only");
    expect(html).toContain('data-passages="3"');
    expect(html.match(/<li><span class="atlr-sample"/g)).toHaveLength(4);
  });

  it("?view=cards is the card list, with the switch and a link from each passage to its place in the article", async () => {
    const h = withReader();
    const html = await (await get(h.env, `${WIDGET_PATH}?view=cards`)).text();
    expect(html).not.toContain('id="articles-reader"');
    expect(html).toContain('<main class="wl-articles" id="articles-article"');
    expect(html).toContain(`<a id="atlr-to-cards" href="${WIDGET_PATH}?view=cards" aria-current="page">Card list</a>`);
    for (const hex of READER_ORDER) expect(html).toContain(`<p class="articles-context"><a href="${WIDGET_PATH}#p-${hex}">Show in article</a>`);
    expect(html).toContain(`<link rel="stylesheet" href="${ARTICLES_READER_CSS}">`);
    expect(html).toContain(`<script src="${ARTICLES_READER_JS}"></script>`);
    // The card list reads no revision: the view's sha256 is enough to offer the reader, and the
    // reader checks the bytes before it serves them.
    expect(h.gets).not.toContain(`articles/v1/reader/${WIDGET_PAGEID}.html`);
    await get(h.env, WIDGET_PATH);
    expect(h.gets.filter((key) => key === `articles/v1/reader/${WIDGET_PAGEID}.html`)).toHaveLength(1);
  });

  it("?view=cards offers the reader only when the view names a revision; the reader then falls back to the cards", async () => {
    // A view that names a revision the store lost: the card list still offers the switch (it reads
    // nothing to know), and the reader route, which checks, serves the card list without one.
    const files = articlesReaderFiles();
    files.delete(`articles/v1/reader/${WIDGET_PAGEID}.html`);
    const lost = setup();
    bindArticles(lost.env, files);
    expect(await (await get(lost.env, `${WIDGET_PATH}?view=cards`)).text()).toContain('<nav class="atlr-switch" aria-label="View">');
    expect(await (await get(lost.env, WIDGET_PATH)).text()).not.toContain("atlr-switch");
    // A view that names none: no switch on either.
    const plain = bound();
    expect(await (await get(plain.env, `${WIDGET_PATH}?view=cards`)).text()).not.toContain("atlr-switch");
  });

  it("without the store's marked revision, or with other bytes than the view names, the page is the card list as it always was", async () => {
    const plain = bound();
    const before = await (await get(plain.env, WIDGET_PATH)).text();
    expect(before).not.toMatch(/atlr-|articles-reader/);
    expect(plain.gets).not.toContain(`articles/v1/reader/${WIDGET_PAGEID}.html`);
    // A view that names a revision the store does not hold, or holds other bytes of.
    const missing = setup();
    const files = articlesReaderFiles();
    files.delete(`articles/v1/reader/${WIDGET_PAGEID}.html`);
    bindArticles(missing.env, files);
    const altered = withReader({ html: readerHtml().replace("widget lemma", "gadget lemma") });
    for (const h of [missing, altered]) {
      const html = await (await get(h.env, WIDGET_PATH)).text();
      expect(html).not.toContain('id="articles-reader"');
      expect(html).toContain('<main class="wl-articles" id="articles-article"');
      expect(html).not.toContain("atlr-switch");
    }
  });

  it("loads only same-origin URLs around the revision, with no inline handlers, and its script parses nothing as HTML", async () => {
    const h = withReader();
    const html = await (await get(h.env, WIDGET_PATH)).text();
    const scripts = [...html.matchAll(/<script\b[^>]*\ssrc="([^"]*)"/g)].map((m) => m[1]);
    expect(scripts).toEqual([ARTICLES_JS, ARTICLES_READER_JS]);
    const sheets = [...html.matchAll(/<link\b[^>]*\srel="stylesheet"[^>]*\shref="([^"]*)"/g)].map((m) => m[1]);
    expect(sheets).toEqual(["/assets/style.css?v=10", "/assets/articles.css?v=10", ARTICLES_READER_CSS]);
    expect([...html.matchAll(/<[a-z][^>]*\s(on[a-z]+)\s*=/gi)]).toEqual([]);
    const js = readFileSync(new URL("../../site/assets/articles-reader.js", import.meta.url), "utf8");
    const css = readFileSync(new URL("../../site/assets/articles-reader.css", import.meta.url), "utf8");
    expect(js).not.toMatch(/\bfetch\(|XMLHttpRequest|innerHTML|outerHTML|insertAdjacentHTML|document\.write|DOMParser|createContextualFragment/);
    expect(js).not.toMatch(/\beval\s*\(|\bnew\s+Function\b|\bset(Timeout|Interval)\(\s*["'`]/);
    expect(js).not.toMatch(/https?:\/\/|["'`]\/\//);
    expect(js).toMatch(/^[\x00-\x7f]*$/);
    expect(css).not.toMatch(/url\(|@import/);
  });

  it("contains the revision: the article is a paint-contained box, so nothing in it paints over the page", () => {
    // An absolutely positioned element of the revision with any z-index would otherwise cover the
    // header, the panel and the footer (e2e/articles.spec.ts checks it in Chromium). The rule is the
    // article's own, at the top level, so it holds at every width.
    const css = readFileSync(new URL("../../site/assets/articles-reader.css", import.meta.url), "utf8");
    const rules = [...css.matchAll(/^\.wl-article-body\.atlr-article \{([^}]*)\}/gm)];
    expect(rules).toHaveLength(1);
    const declarations = rules[0][1].split(";").map((d) => d.trim()).filter(Boolean);
    expect(declarations).toEqual(expect.arrayContaining(["position: relative", "contain: paint"]));
    // No later rule for the article (a phone width's, say) sets another containment.
    expect(css).not.toMatch(/\.atlr-article\s*\{[^}]*\bcontain:\s*(?!paint\b)\S/);
  });

  it("carries the articles pages' security headers, with articlesCsp enforced beside the framing ban", async () => {
    const h = withReader();
    for (const path of [WIDGET_PATH, `${WIDGET_PATH}?view=cards`]) {
      const res = await get(h.env, path);
      expect(res.status, path).toBe(200);
      for (const [name, value] of Object.entries(SECURITY_HEADERS)) {
        const want = name === "Content-Security-Policy" ? `${ARTICLES_CSP}, ${value}` : value;
        expect(res.headers.get(name), `${path}: ${name}`).toBe(want);
      }
    }
  });

  it("plainHead gives a passage's first words with each formula as its TeX", () => {
    expect(plainHead({ quote: { exact: "every finite widget admits a spanning gadget" } })).toBe("every finite widget admits a spanning gadget");
    expect(plainHead({ quote: { exact: "the gadget weight g ( w ) = w 3 {\\displaystyle g(w)=w^{3}} is odd" } })).toBe("the gadget weight g(w)=w^{3} is odd");
    const long = plainHead({ quote: { exact: "word ".repeat(40).trim() } }, 30);
    expect(long.endsWith("…")).toBe(true);
    expect(long.length).toBeLessThanOrEqual(31);
  });
});
