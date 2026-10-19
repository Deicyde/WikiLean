// The formalization atlas plane (src/atlas.ts, src/atlas-render.ts,
// src/libraries.ts, migration 0015; design docs/ATLAS.md). Pins: every atlas
// route answers 503 while ATLAS is unbound; with the golden fixture store bound
// the pages and APIs serve it (404 for unknown ids); feedback is session-only,
// same-origin, capped, rate limited, validated against the store, and exported
// to the pipeline bearer under pseudonyms; the article page shows the summary
// box per request without touching the cached base page.

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { describe, it, expect } from "vitest";
import {
  setup, get, post, blockNetwork, PIPELINE_TOKEN, REVID, SLUG, ORIGIN, type Harness,
} from "./helpers/harness.js";
import {
  ATLAS_STORE_ROOT, atlasFixtureFiles, bindAtlas, fixtureArticle, FIXTURE_CAMPAIGN, FIXTURE_UNIVERSE_SHA, GADGET_PAGEID,
  WIDGET_PAGEID,
} from "./helpers/atlas.js";
import {
  ATLAS_PSEUDONYM_DOMAIN, CAMPAIGN_RE, INDEX_CACHE_MS, atlasHref, kvReader, loadArticle, loadIndexShard, lookupTitle,
  memoryReader, parsePageid, staticReader, titleKey, titleShard,
} from "../src/atlas.js";
import { atlasSummaryBox, campaignLabel, locationStatus } from "../src/atlas-render.js";
import { libraryInfo, libraryInfos, repoHref, sourceUrl, treeUrl } from "../src/libraries.js";
import { injectAtlasSummary } from "../src/pages.js";
import { app } from "../src/index.js";
import { CSP_REPORT_ONLY, MAX_JSON_BODY_BYTES, SECURITY_HEADERS } from "../src/security.js";

blockNetwork();

const WIDGET_PATH = `/atlas/${WIDGET_PAGEID}/Widget_lemma`;
// Response.json() is `unknown` under workers-types.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const jsonOf = async (res: Response): Promise<any> => res.json();
const MATHLIB_KEY = fixtureArticle().passages[0].locations[0].location_key as string;

function bound(opts: Parameters<typeof setup>[0] = {}): Harness & { gets: string[] } {
  const h = setup(opts);
  const bucket = bindAtlas(h.env);
  return { ...h, gets: bucket.gets };
}

function feedback(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return { location_key: MATHLIB_KEY, pageid: WIDGET_PAGEID, campaign: FIXTURE_CAMPAIGN, verdict: "confirm", ...overrides };
}

function feedbackRows(h: Harness): Array<Record<string, unknown>> {
  return h.db.prepare("SELECT * FROM atlas_feedback ORDER BY id").all() as Array<Record<string, unknown>>;
}

describe("atlas routes without a store", () => {
  const PAGE_ROUTES = ["/atlas", `/atlas/${WIDGET_PAGEID}`, WIDGET_PATH, `/atlas/universe/${FIXTURE_UNIVERSE_SHA}`];
  const API_ROUTES = [
    "/api/atlas/current", `/api/atlas/article/${WIDGET_PAGEID}`, `/api/atlas/records/${WIDGET_PAGEID}`,
    `/api/atlas/universe/${FIXTURE_UNIVERSE_SHA}`, "/api/atlas/index/1",
  ];

  it("every page and API route answers 503 'atlas store not configured', never a crash", async () => {
    const h = setup();
    for (const path of PAGE_ROUTES) {
      const res = await get(h.env, path);
      expect(res.status, path).toBe(503);
      expect(res.headers.get("Content-Type"), path).toContain("text/html");
      expect(await res.text(), path).toContain("atlas store not configured");
    }
    for (const path of API_ROUTES) {
      const res = await get(h.env, path);
      expect(res.status, path).toBe(503);
      expect(await jsonOf(res), path).toEqual({ ok: false, error: "atlas store not configured" });
      expect(res.headers.get("Cache-Control"), path).toBe("no-store");
    }
    const write = await post(h.env, "/api/atlas/feedback", feedback(), { user: "u-human" });
    expect(write.status).toBe(503);
    expect(feedbackRows(h)).toEqual([]);
  });

  it("an article page has no atlas box and is unchanged", async () => {
    const h = setup();
    const html = await (await get(h.env, `/${SLUG}`)).text();
    expect(html).not.toContain("wl-atlas-box");
    expect(html).not.toContain("atlas.css");
  });

  it("a bound but empty store says it has no published build", async () => {
    const h = setup();
    bindAtlas(h.env, new Map());
    const page = await get(h.env, "/atlas");
    expect(page.status).toBe(503);
    expect(await page.text()).toContain("atlas store has no published build");
    expect((await get(h.env, "/api/atlas/current")).status).toBe(404);
  });
});

describe("atlas APIs with the fixture store", () => {
  it("serve the store's bytes with JSON / NDJSON types and cache headers", async () => {
    const h = bound();
    const files = atlasFixtureFiles();
    const article = await get(h.env, `/api/atlas/article/${WIDGET_PAGEID}`);
    expect(article.status).toBe(200);
    expect(article.headers.get("Content-Type")).toBe("application/json; charset=utf-8");
    expect(article.headers.get("Cache-Control")).toBe("public, max-age=300");
    expect(await article.text()).toBe(files.get(`atlas/v1/articles/${WIDGET_PAGEID}.json`));

    const records = await get(h.env, `/api/atlas/records/${WIDGET_PAGEID}`);
    expect(records.status).toBe(200);
    expect(records.headers.get("Content-Type")).toBe("application/x-ndjson; charset=utf-8");
    const body = await records.text();
    expect(body).toBe(files.get(`atlas/v1/records/${WIDGET_PAGEID}.jsonl`));
    expect(body.trim().split("\n").map((line) => JSON.parse(line).relation)).toEqual(
      ["exactMatch", "closeMatch", "exactMatch", "noMatch", "relatedMatch", undefined]);

    // Not immutable: the key is the producer's universe sha, which does not
    // cover the display metadata stored beside the libraries.
    const universe = await get(h.env, `/api/atlas/universe/${FIXTURE_UNIVERSE_SHA}`);
    expect(universe.status).toBe(200);
    expect(universe.headers.get("Cache-Control")).toBe("public, max-age=300");
    expect((await jsonOf(universe)).libraries).toHaveLength(5);

    const current = await get(h.env, "/api/atlas/current");
    expect(current.status).toBe(200);
    expect((await jsonOf(current)).campaign).toBe(FIXTURE_CAMPAIGN);

    const shard = await get(h.env, "/api/atlas/index/1");
    expect((await jsonOf(shard)).articles[0][0]).toBe("Widget lemma");
  });

  it("answer 404 for unknown or malformed ids", async () => {
    const h = bound();
    for (const path of [
      "/api/atlas/article/9999", "/api/atlas/article/0", "/api/atlas/article/abc", "/api/atlas/article/01001",
      "/api/atlas/records/9999", "/api/atlas/universe/" + "f".repeat(64), "/api/atlas/universe/not-a-sha",
      "/api/atlas/index/z", "/api/atlas/index/0",
    ]) {
      const res = await get(h.env, path);
      expect(res.status, path).toBe(404);
      expect((await jsonOf(res)).ok, path).toBe(false);
    }
  });
});

describe("atlas pages with the fixture store", () => {
  it("the dashboard shows totals by status, by library and by importance, and lists articles", async () => {
    const h = bound();
    const res = await get(h.env, "/atlas");
    expect(res.status).toBe(200);
    const html = await res.text();
    expect(html).toContain("<title>WikiLean · Formalization atlas</title>");
    expect(html).toContain("2 articles · 7 passages · 7 Lean locations · 1 related declaration");
    expect(html).toContain('aria-label="7 passages: 4 formalized, 1 statement only, 1 not formalized, 1 undecided"');
    // By library, in universe order (by id). Seeded repositories keep the
    // registry's name (the universe's "Lean 4 core (Init, Std, Lean)" is not used).
    const names = ["Batteries", "FormalFrontier · WidgetTheory", "Lean core", "Mathlib", "TauCeti"];
    const at = names.map((name) => html.indexOf(`<th scope="row">${name}</th>`));
    expect(at.every((i) => i > 0)).toBe(true);
    expect([...at].sort((a, b) => a - b)).toEqual(at);
    expect(html).not.toContain("Lean 4 core");
    // Complete and incomplete locations are separate columns: TauCeti only
    // states its result, so it is never counted like a proved Mathlib row.
    expect(html).toContain('<th scope="col" class="num">Complete</th><th scope="col" class="num">Incomplete</th>');
    expect(html).toMatch(/<th scope="row">TauCeti<\/th><td>.*?<\/td><td class="num">1<\/td><td class="num">1<\/td><td class="num">0<\/td><td class="num">1<\/td><td class="num">0<\/td><\/tr>/);
    expect(html).toMatch(/<th scope="row">Mathlib<\/th><td>.*?<\/td><td class="num">2<\/td><td class="num">4<\/td><td class="num">5<\/td><td class="num">0<\/td><td class="num">0<\/td><\/tr>/);
    expect(html).toContain('<code class="atlas-sha">@bcb35bd</code>');
    expect(html).toContain("By WikiProject importance");
    expect(html).toContain('<th scope="row">High</th>');
    expect(html).toContain(`href="${WIDGET_PATH}"`);
    expect(html).toContain(`href="/atlas/${GADGET_PAGEID}/Gadget_(mathematics)"`);
    expect(html).toContain('data-shards="1 8"');
    // Where each article is formalized: a Libraries column from the index.
    expect(html).toContain('<th scope="col">Libraries</th>');
    expect(html).toContain('title="FormalFrontier · WidgetTheory · https://github.com/FormalFrontier/WidgetTheory">FormalFrontier · WidgetTheory</span> ' +
      '<span class="atlas-libtag" title="Mathlib · https://github.com/leanprover-community/mathlib4">Mathlib</span> ' +
      '<span class="atlas-libtag" title="TauCeti · https://github.com/TauCetiProject/TauCeti">TauCeti</span>');
    expect(html).toContain("data-library-names=\"{&quot;batteries&quot;:&quot;Batteries&quot;,&quot;ff-widgets&quot;");
    expect(html).toContain("&quot;mathlib&quot;:&quot;Mathlib&quot;");
    expect(html).toContain('<p class="atlas-meta">Campaign fixture-2026-10 · <a href="/atlas/universe/');
    // The bar's legend: each totals chip carries its fill's swatch.
    expect(html).toContain('<span class="atlas-swatch" data-status="unresolved" aria-hidden="true"></span>');
    // Every table that can scroll sideways is a labelled, focusable region.
    for (const name of ["By library", "By WikiProject importance", "Articles"]) {
      expect(html).toContain(`<div class="atlas-table-wrap" role="region" tabindex="0" aria-label="Table: ${name}">`);
    }
    expect(html).toContain('<a class="wl-navlink" href="/atlas" aria-current="page">Atlas</a>');
    expect(html).toContain('<script src="/assets/atlas.js?v=2"></script>');
    expect(html).toContain('<link rel="stylesheet" href="/assets/atlas.css?v=4">');
    expect(html).not.toMatch(/\son[a-z]+=/i); // no inline event handlers
  });

  it("the dashboard reads each index shard once a minute, not once per view", async () => {
    const h = bound();
    await get(h.env, "/atlas");
    await get(h.env, "/atlas?q=widget");
    await get(h.env, `/${SLUG}`);
    expect(h.gets.filter((key) => key === "atlas/v1/index/1.json")).toHaveLength(1);
    expect(h.gets.filter((key) => key === "atlas/v1/index/8.json")).toHaveLength(1);
    // A cached shard expires after INDEX_CACHE_MS.
    const files = atlasFixtureFiles();
    const reader = memoryReader(files);
    const first = await loadIndexShard(reader, "1", 1_000);
    files.set("atlas/v1/index/1.json", files.get("atlas/v1/index/8.json")!.replace('"shard":"8"', '"shard":"1"'));
    expect(await loadIndexShard(reader, "1", 1_000 + INDEX_CACHE_MS - 1)).toBe(first);
    expect((await loadIndexShard(reader, "1", 1_000 + INDEX_CACHE_MS))?.[0].title).toBe("Gadget (mathematics)");
  });

  it("a dashboard search takes the per-IP anonymous read budget; the plain list does not", async () => {
    const h = setup({ brainApiLimiterAllows: false });
    bindAtlas(h.env);
    const limited = await get(h.env, "/atlas?q=gadg", { ip: "198.51.100.4" });
    expect(limited.status).toBe(429);
    expect(limited.headers.get("Retry-After")).toBe("60");
    expect(await limited.text()).toContain("Too many atlas searches from this address in a minute.");
    expect((await get(h.env, "/atlas")).status).toBe(200);
  });

  it("the dashboard search filters server-side through the index shards", async () => {
    const h = bound();
    const html = await (await get(h.env, "/atlas?q=gadg")).text();
    expect(html).toContain("1 of 2 articles match");
    expect(html).toContain("Gadget (mathematics)</a>");
    expect(html).not.toContain(`href="${WIDGET_PATH}"`);
    const none = await (await get(h.env, "/atlas?q=" + encodeURIComponent("<script>"))).text();
    expect(none).toContain("0 of 2 articles match “&lt;script&gt;”");
  });

  it("an article coverage page renders every outcome", async () => {
    const h = bound();
    const res = await get(h.env, WIDGET_PATH);
    expect(res.status).toBe(200);
    const html = await res.text();
    expect(html).toContain('<h1 class="atlas-h1">Widget lemma</h1>');
    // Headings to navigate by: Coverage, Passages, one h3 per passage, and an
    // h4 over related declarations.
    expect(html).toContain('<section class="atlas-strip" aria-labelledby="atlas-h-coverage">\n<h2 class="atlas-h2" id="atlas-h-coverage">Coverage</h2>');
    expect(html).toContain('<h2 class="atlas-h2" id="atlas-h-passages">Passages</h2>');
    expect(html.match(/<h3 class="atlas-p-h">/g)).toHaveLength(4);
    // The article's subject passage is marked: a tag in its heading, a class on
    // its block, and a "Subject" row in the strip linking it. Other passages
    // keep their prominence in the eyebrow.
    expect(html).toContain('<h3 class="atlas-p-h"><span class="atlas-eyebrow">Theorem</span>' +
      '<span class="atlas-subject">Subject of the article</span><span class="atlas-section">Statement</span></h3>');
    expect(html).toContain('<li class="atlas-passage is-subject" id="p-3fa91c07" data-status="formalized" data-prominence="subject"');
    expect(html.match(/class="atlas-passage is-subject"/g)).toHaveLength(1);
    expect(html.match(/class="atlas-subject"/g)).toHaveLength(1);
    expect(html).toContain('<span class="atlas-eyebrow">Definition · central</span><span class="atlas-section">Definitions</span>');
    expect(html).toContain('<li class="atlas-passage" id="p-5b0e44a1" data-status="statement_only" data-prominence="supporting"');
    expect(html).toContain('<div class="atlas-strip-row"><span class="atlas-strip-h" id="atlas-h-subject">Subject</span>' +
      '<ul class="atlas-subject-list" aria-labelledby="atlas-h-subject"><li><a href="#p-3fa91c07">Theorem: “every finite widget admits a spanning gadget”</a> ' +
      '<span class="atlas-pill" data-status="formalized">');
    expect(html).toContain("Searched <a href=\"/atlas/universe/" + FIXTURE_UNIVERSE_SHA + "\">universe u-fixture</a>: 5 libraries, frozen 2026-10-17, 1 excluded · campaign fixture-2026-10</p>");
    expect(html).toContain('<h4 class="atlas-related-h">Related, but states something else</h4>');
    // Only the dashboard is the Atlas nav link's own page.
    expect(html).toContain('<a class="wl-navlink" href="/atlas">Atlas</a>');
    expect(html).not.toContain('aria-current="page"');
    // The footer claims only what the page shows.
    expect(html).toContain("some are re-checked by a second AI pass, and each location says whether it was. Signed-in readers can confirm or report a location.");
    // Strip: counts by status and by library, as filter chips.
    expect(html).toContain('data-filter="status:formalized"');
    expect(html).toContain('data-filter="library:mathlib"');
    expect(html).toContain('data-filter="library:ff-widgets"');
    // A zero count is shown but is not a filter.
    expect(html).not.toContain('data-filter="status:partial"');
    expect(html).toContain('<span class="atlas-chip is-static is-zero" data-status="partial">');
    expect(html).toContain("4 passages checked, 3 Lean locations (and 1 related declaration).");
    expect(html).toContain("Batteries <span class=\"atlas-n\">related only</span>");
    // The status in words is visible text beside the pill, not a hover-only
    // title, and the pill is not read twice.
    expect(html).toContain('<p class="atlas-why">Formalized: Mathlib states this exactly (proved).</p>');
    expect(html).toContain('<p class="atlas-why">Statement only: TauCeti states this exactly (statement only: the proof is not finished).</p>');
    expect(html).toContain('<span class="atlas-pill" data-status="formalized"><span class="atlas-ico" aria-hidden="true">✓</span>Formalized</span>');
    expect(html).not.toContain('class="atlas-pill" data-status="formalized" title=');
    expect(html.match(/class="atlas-why"/g)).toHaveLength(2); // the absence and the undecided passage have their own boxes
    // The producer's item id is markup, not reader-facing text.
    expect(html).toContain('data-item="In-text #3fa91c07"');
    expect(html).not.toMatch(/>In-text #/);
    // The status chips carry the coverage bar's fills as their legend.
    expect(html).toContain('<span class="atlas-swatch" data-status="statement_only" aria-hidden="true"></span>');
    // Location card: library chip, short commit, relation, proof status, name, pinned source link.
    const mathlib = fixtureArticle().passages[0].locations[0];
    const sha7 = mathlib.commit.slice(0, 7);
    expect(html).toContain(`<code class="atlas-commit" title="commit ${mathlib.commit}">@${sha7}</code>`);
    expect(html).toContain('<span class="atlas-rel" title="states this exactly">exact match</span>');
    expect(html).toContain('<code class="atlas-decl"><span class="atlas-ns">Widget.<wbr></span><b>exists_<wbr>spanning_<wbr>gadget</b></code>');
    expect(html).toContain(`href="https://github.com/leanprover-community/mathlib4/blob/${mathlib.commit}/Mathlib/Combinatorics/Widget/Basic.lean#L612-L640"`);
    expect(html).toContain(`Source @${sha7}`);
    // Proof status shown prominently when it is not "proved".
    expect(html).toContain('<span class="atlas-ps is-alert" data-proof="statement_only"');
    expect(html).toContain("close match");
    expect(html).toContain("Counts gadgets from 1, where the article counts from 0.");
    // A lean end with no end_line (its index records only line): "line N", and
    // the source link names that one line.
    const ff = fixtureArticle().passages[0].locations[1];
    expect(ff.library).toBe("ff-widgets");
    expect(ff).not.toHaveProperty("end_line");
    expect(html).toContain('<p class="atlas-module">WidgetTheory.Spanning · line 88</p>');
    expect(html).toContain(`href="https://github.com/FormalFrontier/WidgetTheory/blob/${ff.commit}/WidgetTheory/Spanning.lean#L88"`);
    expect(html).toContain(`(opens FormalFrontier · WidgetTheory at commit ${ff.commit.slice(0, 7)}, line 88)`);
    expect(html).toContain("a challenger downgraded it");
    expect(html).toContain('<p class="atlas-module">Mathlib.Combinatorics.Widget.Basic · lines 612–640</p>');
    // Absence and undecided.
    expect(html).toContain(`Searched 5 libraries in universe <a href="/atlas/universe/${FIXTURE_UNIVERSE_SHA}">u-fixture</a> (2026-10-17) — not found.`);
    expect(html).toContain("Related, but states something else");
    expect(html).toContain("Undecided — the pipeline could not settle");
    // Quote, in context at the pinned revision.
    expect(html).toContain("<mark>every finite widget admits a spanning gadget</mark>");
    expect(html).toContain("https://en.wikipedia.org/w/index.php?title=Widget_lemma&amp;oldid=2002#:~:text=every%20finite%20widget");
    // Anonymous: no feedback buttons, a sign-in link back to this page.
    expect(html).not.toContain('class="atlas-fb"');
    expect(html).toContain(`href="/login?returnTo=${encodeURIComponent(WIDGET_PATH)}"`);
    expect(html).not.toMatch(/\son[a-z]+=/i);
  });

  it("the bare page id serves the same page, and the WikiLean article is linked when one exists", async () => {
    const h = bound();
    h.db.prepare("UPDATE articles SET wikipedia_title = 'Widget lemma' WHERE slug = ?").run(SLUG);
    const html = await (await get(h.env, `/atlas/${WIDGET_PAGEID}`)).text();
    expect(html).toContain(`<link rel="canonical" href="https://wikilean.jackmccarthy.org${WIDGET_PATH}">`);
    expect(html).toContain(`<a href="/${SLUG}">Annotated article on WikiLean</a>`);
  });

  it("a lead passage (section []) that is the article's subject renders without a section", async () => {
    const h = bound();
    const html = await (await get(h.env, `/atlas/${GADGET_PAGEID}/Gadget_(mathematics)`)).text();
    expect(html).toContain('<span class="atlas-eyebrow">Definition</span><span class="atlas-subject">Subject of the article</span></h3>');
    const lead = html.slice(html.indexOf('id="p-7d2c9e10"'), html.indexOf("</li>", html.indexOf('id="p-7d2c9e10"')));
    expect(lead.length).toBeGreaterThan(0);
    expect(lead).not.toContain("atlas-section");
    expect(html).toContain('<a href="#p-7d2c9e10">Definition: “a gadget is a widget together with a chosen basepoint”</a>');
    expect(html).toContain('<p class="atlas-why">Formalized: Mathlib states this exactly (a definition).</p>');
    // Its prominence was overturned to subject after the challenge ran: challenge_pending,
    // which reads as unchallenged.
    expect(html).toContain("high confidence · not challenged yet (a challenge is pending)</span>");
    expect(html).not.toContain("challenge_pending");
    expect(html).toContain('aria-label="3 passages: 3 formalized"');
    expect(html).toContain("3 passages checked, 4 Lean locations.");
    // One passage is "1 passage", in the strip and in the bar's name.
    const files = atlasFixtureFiles();
    const article = JSON.parse(files.get(`atlas/v1/articles/${GADGET_PAGEID}.json`)!);
    article.passages = article.passages.slice(0, 1);
    Object.assign(article.summary, { passages: 1, locations: 1 });
    article.summary.by_status.formalized = 1;
    files.set(`atlas/v1/articles/${GADGET_PAGEID}.json`, JSON.stringify(article));
    const one = setup();
    bindAtlas(one.env, files);
    const single = await (await get(one.env, `/atlas/${GADGET_PAGEID}`)).text();
    expect(single).toContain('aria-label="1 passage: 1 formalized"');
    expect(single).toContain("1 passage checked, 1 Lean location.");
  });

  it("the status sentence names the location that justifies the status, not rank 1", async () => {
    // Rank 1 (Mathlib) only states the theorem; rank 2 (FormalFrontier) proves
    // it, so the passage is formalized because of rank 2 (build_store basis).
    for (const withBasis of [true, false]) {
      const h = setup();
      const files = atlasFixtureFiles();
      const article = JSON.parse(files.get(`atlas/v1/articles/${WIDGET_PAGEID}.json`)!);
      const passage = article.passages[0];
      passage.locations[0].proof_status = "statement_only";
      passage.basis = withBasis ? passage.locations[1].location_key : undefined;
      files.set(`atlas/v1/articles/${WIDGET_PAGEID}.json`, JSON.stringify(article));
      bindAtlas(h.env, files);
      const html = await (await get(h.env, WIDGET_PATH)).text();
      expect(html, `basis ${withBasis}`).toContain(
        '<p class="atlas-why">Formalized: FormalFrontier · WidgetTheory states this up to a convention difference (proved).</p>');
      expect(html, `basis ${withBasis}`).not.toContain("Formalized: Mathlib");
    }
  });

  it("store numbers reach the HTML only as integers", async () => {
    const h = setup();
    const files = atlasFixtureFiles();
    const article = JSON.parse(files.get(`atlas/v1/articles/${WIDGET_PAGEID}.json`)!);
    article.revision = "<svg onload=alert(2)>";
    article.pageid = "<i>p</i>";
    article.summary.passages = "<b>n</b>";
    article.summary.by_status.formalized = "<u>f</u>";
    article.universe.libraries = "<s>l</s>";
    article.passages[0].locations[0].rank = "<img src=x onerror=alert(1)>";
    article.passages[0].locations[0].line = "<q>1</q>";
    article.passages[2].absence.libraries = "<em>5</em>";
    article.passages[2].absence.receipt.queries = "<kbd>q</kbd>";
    files.set(`atlas/v1/articles/${WIDGET_PAGEID}.json`, JSON.stringify(article));
    const current = JSON.parse(files.get("atlas/v1/current.json")!);
    current.counts.by_status.formalized = "<script>alert(3)</script>";
    current.counts.by_library.mathlib.passages = "<sub>m</sub>";
    current.counts.articles = "<sup>a</sup>";
    current.universe.libraries = "<del>u</del>";
    files.set("atlas/v1/current.json", JSON.stringify(current));
    bindAtlas(h.env, files);
    const page = await (await get(h.env, `/atlas/${WIDGET_PAGEID}`)).text();
    const dashboard = await (await get(h.env, "/atlas")).text();
    for (const html of [page, dashboard]) {
      for (const tag of ["<svg", "<img", "<i>", "<b>n", "<u>", "<s>", "<q>", "<em>", "<kbd>", "<script>alert", "<sub>", "<sup>", "<del>"]) {
        expect(html).not.toContain(tag);
      }
    }
    expect(page).toContain("AI pipeline · rank 0 ·");
    expect(page).toContain('data-pageid="0"');
    expect(dashboard).toContain("0 articles · 7 passages");
  });

  it("a universe list that is not an array never reaches the HTML and never breaks a page", async () => {
    const universePath = `atlas/v1/universes/${FIXTURE_UNIVERSE_SHA}.json`;
    const cases: Array<[string, (u: Record<string, unknown>) => void]> = [
      ["excluded with a hostile length", (u) => { u.excluded = { length: "<img src=x onerror=alert(document.domain)>" }; }],
      ["no excluded key", (u) => { delete u.excluded; }],
      ["libraries with a hostile length", (u) => { u.libraries = { length: "<b>9</b>" }; }],
      ["non-object entries", (u) => { u.libraries = [null, 5, { id: "x" }]; u.excluded = [null, "x"]; }],
    ];
    for (const [label, edit] of cases) {
      const h = setup();
      const files = atlasFixtureFiles();
      const universe = JSON.parse(files.get(universePath)!);
      edit(universe);
      files.set(universePath, JSON.stringify(universe));
      bindAtlas(h.env, files);
      const page = await get(h.env, `/atlas/${WIDGET_PAGEID}`);
      const universePage = await get(h.env, `/atlas/universe/${FIXTURE_UNIVERSE_SHA}`);
      expect(page.status, label).toBe(200);
      expect(universePage.status, label).toBe(200);
      for (const html of [await page.text(), await universePage.text()]) {
        expect(html, label).not.toContain("<img src=x");
        expect(html, label).not.toContain("<b>9</b>");
        expect(html, label).not.toContain(" excluded<");
      }
    }
    const h = bound();
    expect(await (await get(h.env, `/atlas/${WIDGET_PAGEID}`)).text()).toContain("5 libraries, frozen 2026-10-17, 1 excluded");
  });

  it("a passage whose only locations are related is undecided and keeps its related declarations", async () => {
    // build_store's reading of proposal §2.6 "(none)": records without a status.
    const h = setup();
    const files = atlasFixtureFiles();
    const article = JSON.parse(files.get(`atlas/v1/articles/${WIDGET_PAGEID}.json`)!);
    const passage = article.passages[0];
    Object.assign(passage, { outcome: "unresolved", status: null, basis: null, unresolved: { challenge: "downgraded" } });
    for (const loc of passage.locations) loc.relation = "relatedMatch";
    files.set(`atlas/v1/articles/${WIDGET_PAGEID}.json`, JSON.stringify(article));
    bindAtlas(h.env, files);
    const html = await (await get(h.env, WIDGET_PATH)).text();
    const block = html.slice(html.indexOf('<li class="atlas-passage is-subject" id="p-3fa91c07"'), html.indexOf("</li>", html.indexOf('id="p-3fa91c07"')));
    expect(block.length).toBeGreaterThan(0);
    expect(block).toContain('data-status="unresolved"');
    expect(block).toContain("Related, but states something else");
    expect(block).toContain("Widget.<wbr>");
    expect(block).toContain("Undecided — the pipeline could not settle");
    expect(block).not.toContain('class="atlas-why"');
    expect(block).not.toContain('<div class="atlas-cards">');
  });

  it("a survived coverage claim says 'Formalized by N parts together' and lists its parts (decision D1)", async () => {
    const h = bound();
    const html = await (await get(h.env, `/atlas/${GADGET_PAGEID}/Gadget_(mathematics)`)).text();
    const at = html.indexOf('id="p-a41f6c2e"');
    expect(at).toBeGreaterThan(0);
    const block = html.slice(at, html.indexOf('id="p-c93b0d58"', at));
    expect(block).toContain('data-status="formalized"');
    // The claim is the basis, so its line is the status sentence, with the parts listed in its order.
    expect(block).toContain('<div class="atlas-coverage" data-coverage-status="formalized"><p class="atlas-why">Formalized by 2 parts together:</p><ol class="atlas-coverage-parts">');
    expect(block.match(/class="atlas-why"/g)?.length).toBe(1);
    expect(block.indexOf("Gadget.<wbr></span><b>connected</b>")).toBeLessThan(block.indexOf("<b>basepoint_<wbr>degree_<wbr>finite</b>"));
    expect(block).toContain("<span class=\"atlas-muted\">(proved)</span></li>");
    // Each part is still a card that states part of it, and reads as checked: confirming the
    // claim confirms its parts (round three; build_store.confirmed_parts).
    expect(block.match(/data-relation="hasPart"/g)?.length).toBe(2);
    expect(block.match(/ · a challenger checked it<\/span>/g)?.length).toBe(2);
    // The theorem an equivalence states is formalized by that one data declaration.
    expect(html).toContain('<p class="atlas-why">Formalized: Mathlib states this exactly (a definition).</p>');
    expect(html.match(/parts together/g)?.length).toBe(1);
  });

  it("a coverage claim that did not survive shows no parts line and upgrades nothing", async () => {
    for (const challenge of ["challenge_pending", "not_required", "sampled_out", "downgraded"]) {
      const h = setup();
      const files = atlasFixtureFiles();
      const article = JSON.parse(files.get(`atlas/v1/articles/${GADGET_PAGEID}.json`)!);
      const passage = article.passages[1];
      Object.assign(passage, { status: "partial", basis: passage.locations[0].location_key });
      Object.assign(passage.coverage, { challenge, status: null, basis: false });
      files.set(`atlas/v1/articles/${GADGET_PAGEID}.json`, JSON.stringify(article));
      bindAtlas(h.env, files);
      const html = await (await get(h.env, `/atlas/${GADGET_PAGEID}`)).text();
      expect(html, challenge).not.toContain("parts together");
      expect(html, challenge).not.toContain("atlas-coverage");
      expect(html, challenge).toContain('<p class="atlas-why">Partial: Mathlib states part of this (proved).</p>');
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
      }, ['<p class="atlas-why">Stated by 2 parts together, not all of them finished:</p>']],
      // The builder gives every survived claim a status (round three); a store built
      // before that could leave one null, and the page still says what the parts are.
      ["a survived claim the store gives no status", (p) => {
        Object.assign(p, { status: "partial", basis: p.locations[0].location_key });
        Object.assign(p.coverage, { status: null, basis: false });
      }, ['<p class="atlas-why">Partial: Mathlib states part of this (proved).</p>',
        '<p class="atlas-why">Covered by 2 parts together, not all of which count toward the status:</p>']],
    ];
    for (const [label, edit, expected] of cases) {
      const h = setup();
      const files = atlasFixtureFiles();
      const article = JSON.parse(files.get(`atlas/v1/articles/${GADGET_PAGEID}.json`)!);
      edit(article.passages[1]);
      files.set(`atlas/v1/articles/${GADGET_PAGEID}.json`, JSON.stringify(article));
      bindAtlas(h.env, files);
      const html = await (await get(h.env, `/atlas/${GADGET_PAGEID}`)).text();
      for (const fragment of expected) expect(html, label).toContain(fragment);
      if (label === "a location reaches the same status") {
        // The exact match is named, and the claim's line still lists the parts.
        expect(html, label).toContain('<p class="atlas-why">Formalized: Mathlib states this exactly (proved).</p>');
        expect(html, label).toContain('<p class="atlas-why">Formalized by 2 parts together:</p>');
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
      const files = atlasFixtureFiles();
      const article = JSON.parse(files.get(`atlas/v1/articles/${GADGET_PAGEID}.json`)!);
      article.passages[1].coverage = claim;
      files.set(`atlas/v1/articles/${GADGET_PAGEID}.json`, JSON.stringify(article));
      bindAtlas(h.env, files);
      const res = await get(h.env, `/atlas/${GADGET_PAGEID}`);
      expect(res.status).toBe(200);
      const html = await res.text();
      for (const tag of ["<b>x", "<i>p", "<u>k", "<s>k"]) expect(html).not.toContain(tag);
      expect(html).not.toContain("parts together");
      // Without a claim to name, the sentence falls back to the store's status alone.
      expect(html).toContain('<p class="atlas-why">Formalized.</p>');
    }
  });

  it("a Prop-valued definition (the statement flag) is named as one", async () => {
    const h = setup();
    const files = atlasFixtureFiles();
    const article = JSON.parse(files.get(`atlas/v1/articles/${WIDGET_PAGEID}.json`)!);
    const loc = article.passages[1].locations[0];
    Object.assign(loc, { proof_status: "definition", statement: true });
    article.passages[1].basis = undefined;  // the fallback reads the flag too
    files.set(`atlas/v1/articles/${WIDGET_PAGEID}.json`, JSON.stringify(article));
    bindAtlas(h.env, files);
    const html = await (await get(h.env, WIDGET_PATH)).text();
    expect(html).toContain('<p class="atlas-why">Statement only: TauCeti states this exactly (a Prop-valued definition: it states the claim without proving it).</p>');
    expect(html).toContain(">Prop definition</span>");
  });

  it("unknown page ids are a 404 page; non-numeric ids fall through to the plain 404", async () => {
    const h = bound();
    const missing = await get(h.env, "/atlas/9999");
    expect(missing.status).toBe(404);
    expect(await missing.text()).toContain("The atlas has no entry for Wikipedia page id 9999.");
    expect((await get(h.env, "/atlas/0123")).status).toBe(404);
    expect((await get(h.env, "/atlas/abc")).status).toBe(404);
    expect((await get(h.env, "/atlas/universe/" + "a".repeat(64))).status).toBe(404);
  });

  it("the universe page lists libraries, commits and exclusions", async () => {
    const h = bound();
    const html = await (await get(h.env, `/atlas/universe/${FIXTURE_UNIVERSE_SHA}`)).text();
    expect(html).toContain("Universe u-fixture");
    expect(html).toContain("<b>atlas-lean</b>: licence forbids ML evaluation use");
    expect(html).toContain("github.com/leanprover/lean4");
    expect(html).toContain('<div class="atlas-table-wrap" role="region" tabindex="0" aria-label="Table: Libraries of universe u-fixture">');
    expect(html).toContain('<a href="https://github.com/leanprover-community/mathlib4" rel="noopener">github.com/leanprover-community/mathlib4</a>');
    expect(html).not.toContain('aria-current="page"');
  });

  it("the universe page links GitHub repositories only and names any other host as text", async () => {
    const h = setup();
    const files = atlasFixtureFiles();
    const universePath = `atlas/v1/universes/${FIXTURE_UNIVERSE_SHA}.json`;
    const universe = JSON.parse(files.get(universePath)!);
    universe.libraries[4].repo = "https://evil.example.com/phish";
    files.set(universePath, JSON.stringify(universe));
    bindAtlas(h.env, files);
    const html = await (await get(h.env, `/atlas/universe/${FIXTURE_UNIVERSE_SHA}`)).text();
    expect(html).toContain("<td>https://evil.example.com/phish</td>");
    expect(html).not.toContain('href="https://evil.example.com');
    expect(html).toContain('<a href="https://github.com/leanprover/lean4" rel="noopener">');
  });

  it("libraries split from one repository are shown per library id, named apart (Lean core · Init, · Std)", async () => {
    // docs/ATLAS.md section 3 and contract note 13: proposal §2.3 splits lean-core
    // into Init and Std at one lean4 commit. The store keys libraries by id; the
    // pages must not show two "Lean core"s.
    const LEAN4 = "https://github.com/leanprover/lean4";
    const universePath = `atlas/v1/universes/${FIXTURE_UNIVERSE_SHA}.json`;
    const articlePath = `atlas/v1/articles/${WIDGET_PAGEID}.json`;
    const files = atlasFixtureFiles();
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
    const current = JSON.parse(files.get("atlas/v1/current.json")!);
    current.counts.by_library = rename(current.counts.by_library);
    files.set("atlas/v1/current.json", JSON.stringify(current));
    const shard = JSON.parse(files.get("atlas/v1/index/1.json")!);
    shard.articles[0][shard.fields.indexOf("libraries")] = ["ff-widgets", "mathlib", "std"];
    files.set("atlas/v1/index/1.json", JSON.stringify(shard));
    const h = setup();
    bindAtlas(h.env, files);

    const universePage = await (await get(h.env, `/atlas/universe/${FIXTURE_UNIVERSE_SHA}`)).text();
    expect(universePage).toContain('<th scope="row">Lean core · Init <span class="atlas-muted">init</span></th>');
    expect(universePage).toContain('<th scope="row">Lean core · Std <span class="atlas-muted">std</span></th>');
    expect(universePage).toContain(`<a href="${LEAN4}/tree/${core.commit}" rel="noopener"><code>${core.commit}</code></a>`);
    expect(universePage).toContain("6 libraries, frozen 2026-10-17");
    expect(universePage).not.toMatch(/>Lean core <span/);

    const page = await (await get(h.env, WIDGET_PATH)).text();
    const std = `<span class="atlas-lib" title="Lean core · Std · ${LEAN4}">Lean core · Std</span>`;
    expect(page).toContain(std);
    expect(page).toContain(`<span class="atlas-lib" title="Lean core · Init · ${LEAN4}">Lean core · Init</span>`);
    expect(page).toContain('data-filter="library:std"');
    expect(page).toContain("Lean core · Init <span class=\"atlas-n\">related only</span>");
    expect(page).toContain("Statement only: Lean core · Std states this exactly");
    expect(page).toContain(`href="${LEAN4}/blob/${core.commit}/src/Std/Data/Widget.lean#L41-L57"`);
    expect(page).not.toMatch(/>Lean core</);

    const dashboard = await (await get(h.env, "/atlas")).text();
    expect(dashboard).toContain('<th scope="row">Lean core · Std</th>');
    expect(dashboard).toContain('<th scope="row">Lean core · Init</th>');
    expect(dashboard).toContain("&quot;init&quot;:&quot;Lean core · Init&quot;");
    expect(dashboard).toContain("&quot;std&quot;:&quot;Lean core · Std&quot;");
    expect(dashboard).toContain(`<span class="atlas-libtag" title="Lean core · Std · ${LEAN4}">Lean core · Std</span>`);
    expect(dashboard).not.toMatch(/>Lean core</);
  });

  it("signed-in viewers get Confirm / Report controls showing their own verdict", async () => {
    const h = bound();
    expect((await post(h.env, "/api/atlas/feedback", feedback({ verdict: "report" }), { user: "u-human" })).status).toBe(200);
    const html = await (await get(h.env, WIDGET_PATH, { user: "u-human" })).text();
    expect(html).toContain(`<div class="atlas-fb" data-location-key="${MATHLIB_KEY}">`);
    expect(html).toContain('data-verdict="report" aria-pressed="true"');
    expect(html).toContain("You reported this link.");
    expect(html).toContain(`data-campaign="${FIXTURE_CAMPAIGN}"`);
    // The pipeline bearer is not a person: no controls.
    const bot = await (await get(h.env, WIDGET_PATH, { bearer: PIPELINE_TOKEN })).text();
    expect(bot).not.toContain('class="atlas-fb"');
  });

  it("escapes hostile store text and drops non-https links", async () => {
    const h = setup();
    const files = atlasFixtureFiles();
    const article = JSON.parse(files.get(`atlas/v1/articles/${WIDGET_PAGEID}.json`)!);
    article.title = '<script>alert("t")</script>';
    article.passages[0].quote.exact = '<img src=x onerror="alert(1)">';
    article.passages[0].locations[0].declaration = 'Evil.<b onmouseover="x">';
    article.passages[0].locations[0].path = "../../etc/passwd";
    article.passages[0].subject_uri = "javascript:alert(1)";
    files.set(`atlas/v1/articles/${WIDGET_PAGEID}.json`, JSON.stringify(article));
    bindAtlas(h.env, files);
    const html = await (await get(h.env, `/atlas/${WIDGET_PAGEID}`)).text();
    expect(html).not.toContain("<script>alert");
    expect(html).not.toContain("<img src=x");
    expect(html).not.toContain('<b onmouseover');
    expect(html).toContain("&lt;script&gt;alert(&quot;t&quot;)&lt;/script&gt;");
    expect(html).not.toContain("javascript:");
    expect(html).not.toContain("etc/passwd\"");
    expect(html).toContain("No source link");
  });
});

describe("atlas feedback", () => {
  it("stores one verdict per (location, user); a second verdict replaces the first", async () => {
    const h = bound();
    const first = await post(h.env, "/api/atlas/feedback", feedback(), { user: "u-human" });
    expect(first.status).toBe(200);
    expect(await jsonOf(first)).toEqual({ ok: true, verdict: "confirm" });
    const second = await post(h.env, "/api/atlas/feedback", feedback({ verdict: "report", note: "  wrong lemma  " }), { user: "u-human" });
    expect(second.status).toBe(200);
    const rows = feedbackRows(h);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      location_key: MATHLIB_KEY, pageid: WIDGET_PAGEID, revision: 2002, campaign: FIXTURE_CAMPAIGN,
      verdict: "report", note: "wrong lemma", user_id: "u-human",
    });
    expect(rows[0].updated_at as number).toBeGreaterThanOrEqual(rows[0].created_at as number);
    await post(h.env, "/api/atlas/feedback", feedback(), { user: "u-patroller" });
    expect(feedbackRows(h)).toHaveLength(2);
  });

  it("refuses anonymous, bearer and cross-origin writes", async () => {
    const h = bound();
    expect((await post(h.env, "/api/atlas/feedback", feedback())).status).toBe(401);
    expect((await post(h.env, "/api/atlas/feedback", feedback(), { user: "u-blocked" })).status).toBe(401);
    const bot = await post(h.env, "/api/atlas/feedback", feedback(), { bearer: PIPELINE_TOKEN, origin: null });
    expect(bot.status).toBe(403);
    expect((await jsonOf(bot)).error).toBe("session required (no bots)");
    const cross = await post(h.env, "/api/atlas/feedback", feedback(), { user: "u-human", origin: "https://evil.example" });
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
      [feedback({ campaign: "older-campaign" }), 409, "stale: the atlas changed since this page loaded"],
    ];
    for (const [body, status, error] of cases) {
      const res = await post(h.env, "/api/atlas/feedback", body, { user: "u-human" });
      expect(res.status, JSON.stringify(body)).toBe(status);
      expect((await jsonOf(res)).error, JSON.stringify(body)).toBe(error);
    }
    const bad = await app.request("/api/atlas/feedback", {
      method: "POST", headers: { "Content-Type": "application/json", Cookie: "wl_dev_user=u-human", Origin: ORIGIN }, body: "{not json",
    }, h.env);
    expect(bad.status).toBe(400);
    const array = await post(h.env, "/api/atlas/feedback", [feedback()], { user: "u-human" });
    expect(array.status).toBe(400);
    expect(feedbackRows(h)).toEqual([]);
  });

  // The shared JSON body cap (security.ts readJsonCapped), with the route's
  // own MAX_JSON_BODY_BYTES entry, read after the origin, auth and rate-limit
  // checks.
  it("caps the body at MAX_JSON_BODY_BYTES.atlasFeedback before parsing", async () => {
    const h = bound();
    const max = MAX_JSON_BODY_BYTES.atlasFeedback;
    expect(max).toBe(4096);
    const tooLarge = { ok: false, error: `request body too large (max ${max} bytes)` };
    const send = (body: string, headers: Record<string, string> = {}) => app.request("/api/atlas/feedback", {
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

    const res = await post(h.env, "/api/atlas/feedback", feedback({ note: "x".repeat(5000) }), { user: "u-human" });
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
    const anonymous = await app.request("/api/atlas/feedback", {
      method: "POST", headers: { "Content-Type": "application/json", "Content-Length": "999999" }, body: "{}",
    }, h.env);
    expect(anonymous.status).toBe(401);
    expect((await send(padded(max + 1), { Origin: "https://evil.example" })).status).toBe(403);
  });

  it("is rate limited per user", async () => {
    const h = bound({ limiterAllows: false });
    const res = await post(h.env, "/api/atlas/feedback", feedback(), { user: "u-human" });
    expect(res.status).toBe(429);
    expect(feedbackRows(h)).toEqual([]);
  });

  it("answers 503 (not 500) while migration 0015 is not applied", async () => {
    const h = bound();
    h.db.exec("DROP TABLE atlas_feedback");
    const res = await post(h.env, "/api/atlas/feedback", feedback(), { user: "u-human" });
    expect(res.status).toBe(503);
    expect((await jsonOf(res)).error).toBe("atlas feedback not configured (migration 0015)");
    // The coverage page still renders for a signed-in viewer.
    expect((await get(h.env, WIDGET_PATH, { user: "u-human" })).status).toBe(200);
    expect((await get(h.env, "/api/atlas/feedback/export", { bearer: PIPELINE_TOKEN })).status).toBe(503);
  });
});

// SEAL.json's campaign is always a token: the campaign id, or for a run outside
// any campaign "run:<run-id>[+<challenge-run-id>]" (build_store.CAMPAIGN_RE).
describe("a record set from a run outside any campaign", () => {
  const RUN = "run:loc-smoke-2+chal-smoke-2";

  function runStore(): Map<string, string> {
    const files = atlasFixtureFiles();
    for (const [key, value] of files) {
      if (key === "atlas/v1/current.json" || key.startsWith("atlas/v1/articles/")) {
        const doc = JSON.parse(value);
        doc.campaign = RUN;
        files.set(key, JSON.stringify(doc));
      }
    }
    return files;
  }

  it("names the runs on the pages, and takes feedback under the run token", async () => {
    const h = setup();
    bindAtlas(h.env, runStore());
    const page = await (await get(h.env, WIDGET_PATH, { user: "u-human" })).text();
    expect(page).toContain(" · run loc-smoke-2 with challenge run chal-smoke-2, outside any campaign</p>");
    expect(page).toContain(`data-campaign="${RUN}"`);
    const dashboard = await (await get(h.env, "/atlas")).text();
    expect(dashboard).toContain('<p class="atlas-meta">Run loc-smoke-2 with challenge run chal-smoke-2, outside any campaign · ');
    const res = await post(h.env, "/api/atlas/feedback", feedback({ campaign: RUN }), { user: "u-human" });
    expect(res.status).toBe(200);
    expect(feedbackRows(h)[0]).toMatchObject({ campaign: RUN, verdict: "confirm" });
    // The fixture's campaign id is now stale for this store.
    expect((await post(h.env, "/api/atlas/feedback", feedback(), { user: "u-human" })).status).toBe(409);
    for (const campaign of ["run:loc-smoke-2+chal-smoke-2+x", "run:", "run:a+", "run:a:b", ""]) {
      const bad = await post(h.env, "/api/atlas/feedback", feedback({ campaign }), { user: "u-human" });
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

describe("atlas feedback export", () => {
  it("is pipeline-bearer only", async () => {
    const h = bound();
    expect((await get(h.env, "/api/atlas/feedback/export")).status).toBe(403);
    expect((await get(h.env, "/api/atlas/feedback/export", { user: "u-admin" })).status).toBe(403);
    expect((await get(h.env, "/api/atlas/feedback/export", { user: "u-human" })).status).toBe(403);
  });

  it("streams every row with pseudonyms and no user ids, and works with ATLAS unbound", async () => {
    const h = bound();
    await post(h.env, "/api/atlas/feedback", feedback(), { user: "u-human" });
    await post(h.env, "/api/atlas/feedback", feedback({ verdict: "report", note: "special case only" }), { user: "u-patroller" });
    delete (h.env as { ATLAS?: unknown }).ATLAS;
    const res = await get(h.env, "/api/atlas/feedback/export", { bearer: PIPELINE_TOKEN });
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("application/x-ndjson; charset=utf-8");
    expect(res.headers.get("Cache-Control")).toBe("no-store");
    const lines = (await res.text()).trim().split("\n").map((line) => JSON.parse(line));
    expect(lines).toHaveLength(2);
    // Domain-separated from the research export's sha256(user_id + salt), so a
    // note that names its author cannot be joined to that export.
    const pseudonym = (id: string) => createHash("sha256").update("atlas-feedback\0" + id + PIPELINE_TOKEN).digest("hex").slice(0, 12);
    const research = (id: string) => createHash("sha256").update(id + PIPELINE_TOKEN).digest("hex").slice(0, 12);
    expect(ATLAS_PSEUDONYM_DOMAIN).toBe("atlas-feedback\0");
    expect(pseudonym("u-human")).not.toBe(research("u-human"));
    expect(lines[0]).toEqual({
      feedback_id: 1, location_key: MATHLIB_KEY, pageid: WIDGET_PAGEID, revision: 2002, campaign: FIXTURE_CAMPAIGN,
      verdict: "confirm", note: null, pseudonym: pseudonym("u-human"),
      created_at: lines[0].created_at, updated_at: lines[0].updated_at,
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
    await post(h.env, "/api/atlas/feedback", feedback(), { user: "u-human" });
    const pseudonymOf = async () => {
      const res = await get(h.env, "/api/atlas/feedback/export", { bearer: PIPELINE_TOKEN });
      expect(res.status).toBe(200);
      return JSON.parse((await res.text()).trim()).pseudonym as string;
    };
    const atlas = (salt: string) => createHash("sha256").update("atlas-feedback\0" + "u-human" + salt).digest("hex").slice(0, 12);
    const research = (salt: string) => createHash("sha256").update("u-human" + salt).digest("hex").slice(0, 12);
    expect(await pseudonymOf()).toBe(atlas(PIPELINE_TOKEN));

    h.env.RESEARCH_EXPORT_SALT = "dedicated-research-salt";
    const salted = await pseudonymOf();
    expect(salted).toBe(atlas("dedicated-research-salt"));
    expect(salted).not.toBe(atlas(PIPELINE_TOKEN));
    // Still domain-separated from the research export under the shared salt.
    expect(salted).not.toBe(research("dedicated-research-salt"));

    // An empty value is "unset", not an empty salt.
    h.env.RESEARCH_EXPORT_SALT = "";
    expect(await pseudonymOf()).toBe(atlas(PIPELINE_TOKEN));
  });
});

// The atlas under the Worker's security layer (src/security.ts): every atlas
// response, whatever its status or how it is built, carries SECURITY_HEADERS,
// and the pages, the article box and the atlas assets load nothing the
// Report-Only policy would report, so enforcing that policy cannot break them
// (e2e/atlas.spec.ts runs the flows with it enforced).
describe("atlas security headers and CSP", () => {
  function expectSecurityHeaders(res: Response, label: string): void {
    for (const [name, value] of Object.entries(SECURITY_HEADERS)) expect(res.headers.get(name), `${label}: ${name}`).toBe(value);
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

  it("are on every atlas page, API answer, error, write answer and export", async () => {
    const unbound = setup();
    for (const path of ["/atlas", WIDGET_PATH, "/api/atlas/current", `/api/atlas/article/${WIDGET_PAGEID}`]) {
      const res = await get(unbound.env, path);
      expect(res.status, path).toBe(503);
      expectSecurityHeaders(res, `unbound ${path}`);
    }

    const h = bound();
    const reads: Array<[string, number]> = [
      ["/atlas", 200], ["/atlas?q=wid", 200], [`/atlas/${WIDGET_PAGEID}`, 200], [WIDGET_PATH, 200],
      [`/atlas/universe/${FIXTURE_UNIVERSE_SHA}`, 200], ["/atlas/9999", 404],
      ["/api/atlas/current", 200], [`/api/atlas/article/${WIDGET_PAGEID}`, 200], [`/api/atlas/records/${WIDGET_PAGEID}`, 200],
      [`/api/atlas/universe/${FIXTURE_UNIVERSE_SHA}`, 200], ["/api/atlas/index/1", 200], ["/api/atlas/article/9999", 404],
    ];
    for (const [path, status] of reads) {
      const res = await get(h.env, path);
      expect(res.status, path).toBe(status);
      expectSecurityHeaders(res, path);
    }

    const writes: Array<[() => Promise<Response>, number]> = [
      [() => post(h.env, "/api/atlas/feedback", feedback(), { user: "u-human" }), 200],
      [() => post(h.env, "/api/atlas/feedback", feedback()), 401],
      [() => post(h.env, "/api/atlas/feedback", feedback(), { user: "u-human", origin: "https://evil.example" }), 403],
      [() => post(h.env, "/api/atlas/feedback", feedback({ note: "x".repeat(5000) }), { user: "u-human" }), 413],
      [() => post(h.env, "/api/atlas/feedback", feedback({ verdict: "maybe" }), { user: "u-human" }), 400],
    ];
    for (const [send, status] of writes) {
      const res = await send();
      expect(res.status).toBe(status);
      expectSecurityHeaders(res, `feedback ${status}`);
    }

    // The export is a hand-built streaming Response, not a c.json/c.html one.
    const exported = await get(h.env, "/api/atlas/feedback/export", { bearer: PIPELINE_TOKEN });
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
      ["/atlas", undefined], ["/atlas?q=gadg", undefined], [WIDGET_PATH, undefined], [WIDGET_PATH, "u-human"],
      [`/atlas/universe/${FIXTURE_UNIVERSE_SHA}`, undefined], ["/atlas/9999", undefined],
    ];
    for (const [path, user] of pages) {
      const html = await (await get(h.env, path, user ? { user } : {})).text();
      const { urls, embeds, handlers } = loads(html);
      expect(urls.length, path).toBeGreaterThan(0);
      expect(urls.filter((url) => !sameOrigin(url)), path).toEqual([]);
      expect(embeds, path).toEqual([]);
      expect(handlers, path).toEqual([]);
      expect(html, path).toContain('<script src="/assets/atlas.js?v=2"></script>');
    }
    const article = await (await get(h.env, `/${SLUG}`)).text();
    const start = article.indexOf('<link rel="stylesheet" href="/assets/atlas.css');
    const end = article.indexOf("</aside>", start);
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
    const box = loads(article.slice(start, end));
    expect(box.urls).toEqual(["/assets/atlas.css?v=4"]);
    expect(box.embeds).toEqual([]);
    expect(box.handlers).toEqual([]);
  });

  it("atlas.js fetches only same-origin paths and never evaluates strings; atlas.css loads nothing", () => {
    const js = readFileSync(new URL("../../site/assets/atlas.js", import.meta.url), "utf8");
    const css = readFileSync(new URL("../../site/assets/atlas.css", import.meta.url), "utf8");
    const targets = [...js.matchAll(/\bfetch\(\s*(["'`])([^"'`]*)/g)].map((m) => m[2]);
    expect(targets).toEqual(["/api/atlas/feedback", "/api/atlas/index/"]);
    expect(js.match(/\bfetch\(/g)).toHaveLength(targets.length);
    expect(js).not.toMatch(/https?:\/\/|["'`]\/\//);
    expect(js).not.toMatch(/\beval\s*\(|\bnew\s+Function\b|\bset(Timeout|Interval)\(\s*["'`]/);
    expect(css).not.toMatch(/url\(|@import/);
  });
});

describe("article page summary box", () => {
  it("appears per request for an article whose Wikipedia title is in the atlas, outside the cached base", async () => {
    const h = bound();
    h.db.prepare("UPDATE articles SET wikipedia_title = 'Widget_lemma' WHERE slug = ?").run(SLUG);
    const res = await get(h.env, `/${SLUG}`);
    expect(res.status).toBe(200);
    const html = await res.text();
    expect(html).toContain('<aside class="wl-atlas-box" aria-label="Formalization atlas">');
    expect(html).toContain(`<a class="wl-atlas-box-link" href="${WIDGET_PATH}">See where`);
    // Its counts are the atlas's, not this page's annotations (the badges
    // above), and it names the page's revision when the atlas checked another.
    expect(html).toContain("Separately from this page&#x27;s annotations, the atlas checked 4 passages of Wikipedia revision 2002 against Lean libraries:");
    expect(html).toContain(`<span class="wl-atlas-box-rev">This page shows revision ${REVID}.</span>`);
    expect(html).toContain("1 formalized");
    expect(html).toContain("1 undecided");
    expect(html.indexOf("wl-atlas-box")).toBeLessThan(html.indexOf('<main class="wl-article-body">'));
    // One index-shard read, nothing else from the store.
    expect(h.gets).toEqual(["atlas/v1/index/1.json"]);
    // The cached base page (render:v18) does not carry the box.
    const cached = [...h.renderCache.store.entries()].filter(([key]) => key.startsWith("render:v18:"));
    expect(cached).toHaveLength(1);
    expect(String(cached[0][1])).not.toContain("wl-atlas-box");
  });

  it("is absent for a title the atlas does not cover, and a broken store never breaks the page", async () => {
    const h = bound();
    expect(await (await get(h.env, `/${SLUG}`)).text()).not.toContain("wl-atlas-box");
    h.db.prepare("UPDATE articles SET wikipedia_title = 'Widget lemma' WHERE slug = ?").run(SLUG);
    (h.env as unknown as { ATLAS: unknown }).ATLAS = { get: async () => { throw new Error("R2 down"); } };
    const res = await get(h.env, `/${SLUG}`);
    expect(res.status).toBe(200);
    expect(await res.text()).not.toContain("wl-atlas-box");
  });

  it("names the page's revision only when it differs from the atlas's", async () => {
    const entry = (await lookupTitle(memoryReader(atlasFixtureFiles()), "Widget lemma"))!;
    expect(atlasSummaryBox(entry, WIDGET_PATH, 2002)).not.toContain("wl-atlas-box-rev");
    expect(atlasSummaryBox(entry, WIDGET_PATH, null)).not.toContain("wl-atlas-box-rev");
    expect(atlasSummaryBox(entry, WIDGET_PATH, 2003)).toContain("This page shows revision 2003.");
    const one = { ...entry, passages: 1, formalized: 1, statement_only: 0, not_formalized: 0, unresolved: 0 };
    expect(atlasSummaryBox(one, WIDGET_PATH, 2002)).toContain("checked 1 passage of Wikipedia revision 2002");
  });

  it("injectAtlasSummary returns the page byte-for-byte when there is no box", () => {
    const page = '<html><body><main class="wl-article-body">x</main></body></html>';
    expect(injectAtlasSummary(page, null)).toBe(page);
    expect(injectAtlasSummary(page, "<aside>B</aside>")).toBe('<html><body><aside>B</aside><main class="wl-article-body">x</main></body></html>');
    expect(injectAtlasSummary("<html><body><p>y</p></body></html>", "<aside>B</aside>")).toBe("<html><body>\n<aside>B</aside><p>y</p></body></html>");
  });
});

describe("atlas store reader", () => {
  it("title keys and shards match scripts/atlas/build_store.py", async () => {
    expect(titleKey("Widget_lemma")).toBe("Widget lemma");
    expect(titleKey("  Gadget (mathematics) ")).toBe("Gadget (mathematics)");
    expect(titleKey("A B__C")).toBe("A B C");
    // Same values scripts/atlas/test_build_store.py pins.
    expect(await titleShard("Widget lemma")).toBe("1");
    expect(await titleShard("Gadget (mathematics)")).toBe("8");
    expect(atlasHref(1003, "Gadget (mathematics)")).toBe("/atlas/1003/Gadget_(mathematics)");
  });

  it("looks titles up through one shard, in any of the three backends", async () => {
    const files = atlasFixtureFiles();
    const memory = memoryReader(files);
    expect((await lookupTitle(memory, "Widget_lemma"))?.pageid).toBe(WIDGET_PAGEID);
    expect(await lookupTitle(memory, "Sylow theorems")).toBeNull();
    expect(await lookupTitle(memory, "   ")).toBeNull();
    const kv = kvReader({ get: (async (key: string) => files.get(key) ?? null) as KVNamespace["get"] });
    expect((await loadArticle(kv, GADGET_PAGEID))?.title).toBe("Gadget (mathematics)");
    const assets = staticReader({
      fetch: (async (req: Request) => {
        const path = new URL(req.url).pathname.replace(/^\/atlas-store\//, "");
        const body = files.get(path);
        return body === undefined ? new Response("nope", { status: 404 }) : new Response(body);
      }) as unknown as Fetcher["fetch"],
    });
    expect((await loadArticle(assets, WIDGET_PAGEID))?.campaign).toBe(FIXTURE_CAMPAIGN);
    expect(await loadArticle(assets, 9999)).toBeNull();
  });

  it("refuses a store object of the wrong schema", async () => {
    const files = atlasFixtureFiles();
    files.set(`atlas/v1/articles/${WIDGET_PAGEID}.json`, JSON.stringify({ schema: "something/else" }));
    await expect(loadArticle(memoryReader(files), WIDGET_PAGEID)).rejects.toThrow("is not wikilean.atlas.article/1");
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
    // An unseeded repository may carry a display name, unless it names a seeded library.
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
  // scripts/atlas/fixtures/producer_derive_status.json: formal-companion's own
  // cflib.vocab.derive_status answers (make_producer_table.py). The page's
  // fallback for a store without `basis` must read the table the same way.
  const table = JSON.parse(readFileSync(`${ATLAS_STORE_ROOT}/../producer_derive_status.json`, "utf8")) as {
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
    // scripts/atlas/fixtures/status_parity.json: the hand-written parity table both sides run
    // (formal-companion's tests/data/locate/status-parity.json, byte for byte). As above, only a
    // located case without a coverage claim reaches the page's per-location fallback.
    const shared = JSON.parse(readFileSync(`${ATLAS_STORE_ROOT}/../status_parity.json`, "utf8")) as {
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
