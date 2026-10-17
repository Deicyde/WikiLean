// The formalization atlas plane (src/atlas.ts, src/atlas-render.ts,
// src/libraries.ts, migration 0015; design docs/ATLAS.md). Pins: every atlas
// route answers 503 while ATLAS is unbound; with the golden fixture store bound
// the pages and APIs serve it (404 for unknown ids); feedback is session-only,
// same-origin, capped, rate limited, validated against the store, and exported
// to the pipeline bearer under pseudonyms; the article page shows the summary
// box per request without touching the cached base page.

import { createHash } from "node:crypto";
import { describe, it, expect } from "vitest";
import {
  setup, get, post, blockNetwork, PIPELINE_TOKEN, SLUG, ORIGIN, type Harness,
} from "./helpers/harness.js";
import {
  atlasFixtureFiles, bindAtlas, fixtureArticle, FIXTURE_CAMPAIGN, FIXTURE_UNIVERSE_SHA, GADGET_PAGEID, WIDGET_PAGEID,
} from "./helpers/atlas.js";
import {
  atlasHref, kvReader, loadArticle, lookupTitle, memoryReader, parsePageid, staticReader, titleKey, titleShard,
} from "../src/atlas.js";
import { libraryInfo, sourceUrl } from "../src/libraries.js";
import { injectAtlasSummary } from "../src/pages.js";
import { app } from "../src/index.js";

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

    const universe = await get(h.env, `/api/atlas/universe/${FIXTURE_UNIVERSE_SHA}`);
    expect(universe.status).toBe(200);
    expect(universe.headers.get("Cache-Control")).toBe("public, max-age=86400, immutable");
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
    expect(html).toContain("2 articles · 5 passages · 4 Lean locations · 1 related declaration");
    expect(html).toContain('aria-label="5 passages: 2 formalized, 1 statement only, 1 not formalized, 1 undecided"');
    // By library: universe order, display names from the universe, registry fallback for FormalFrontier.
    for (const name of ["Mathlib", "Lean 4 core (Init, Std, Lean)", "Batteries", "TauCeti", "FormalFrontier · WidgetTheory"]) {
      expect(html).toContain(`<th scope="row">${name}</th>`);
    }
    expect(html).toContain("By WikiProject importance");
    expect(html).toContain('<th scope="row">High</th>');
    expect(html).toContain(`href="${WIDGET_PATH}"`);
    expect(html).toContain(`href="/atlas/${GADGET_PAGEID}/Gadget_(mathematics)"`);
    expect(html).toContain('data-shards="1 8"');
    expect(html).toContain('<script src="/assets/atlas.js?v=1"></script>');
    expect(html).not.toMatch(/\son[a-z]+=/i); // no inline event handlers
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
    // Strip: counts by status and by library, as filter chips.
    expect(html).toContain('data-filter="status:formalized"');
    expect(html).toContain('data-filter="library:mathlib"');
    expect(html).toContain('data-filter="library:ff-widgets"');
    // A zero count is shown but is not a filter.
    expect(html).not.toContain('data-filter="status:partial"');
    expect(html).toContain('<span class="atlas-chip is-static is-zero" data-status="partial">');
    expect(html).toContain("4 passages checked, 3 Lean locations (and 1 related declaration).");
    expect(html).toContain("Batteries <span class=\"atlas-n\">related only</span>");
    // Pill with its full sentence, and the not-colour-only status data.
    expect(html).toContain('title="Formalized: Mathlib states this exactly (proved)."');
    expect(html).toContain('title="Statement only: TauCeti states this exactly (statement only: the proof is not finished)."');
    expect(html).toContain('title="Not formalized: not found in the 5 libraries of universe u-fixture (searched 2026-10-17)."');
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

  it("a small article that follows proposal §2.7 exactly (no section, no prominence) renders", async () => {
    const h = bound();
    const html = await (await get(h.env, `/atlas/${GADGET_PAGEID}/Gadget_(mathematics)`)).text();
    expect(html).toContain('<span class="atlas-eyebrow">Definition</span>');
    expect(html).not.toContain("atlas-section");
    expect(html).toContain('title="Formalized: Mathlib states this exactly (a definition)."');
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

  it("caps the body size before parsing", async () => {
    const h = bound();
    const res = await post(h.env, "/api/atlas/feedback", feedback({ note: "x".repeat(5000) }), { user: "u-human" });
    expect(res.status).toBe(413);
    const declared = await app.request("/api/atlas/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json", Cookie: "wl_dev_user=u-human", Origin: ORIGIN, "Content-Length": "999999" },
      body: JSON.stringify(feedback()),
    }, h.env);
    expect(declared.status).toBe(413);
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
    const pseudonym = (id: string) => createHash("sha256").update(id + PIPELINE_TOKEN).digest("hex").slice(0, 12);
    expect(lines[0]).toEqual({
      feedback_id: 1, location_key: MATHLIB_KEY, pageid: WIDGET_PAGEID, revision: 2002, campaign: FIXTURE_CAMPAIGN,
      verdict: "confirm", note: null, pseudonym: pseudonym("u-human"),
      created_at: lines[0].created_at, updated_at: lines[0].updated_at,
    });
    expect(lines[1]).toMatchObject({ verdict: "report", note: "special case only", pseudonym: pseudonym("u-patroller") });
    expect(JSON.stringify(lines)).not.toContain("u-human");
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
    expect(html).toContain("4 passages of Wikipedia revision 2002 checked against Lean libraries:");
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

  it("names seeded, FormalFrontier and other GitHub repositories, with the universe taking precedence", () => {
    expect(libraryInfo("https://github.com/leanprover-community/mathlib4").name).toBe("Mathlib");
    expect(libraryInfo("https://github.com/leanprover-community/mathlib4.git/").name).toBe("Mathlib");
    expect(libraryInfo("https://github.com/TauCetiProject/TauCeti").name).toBe("TauCeti");
    expect(libraryInfo("https://github.com/leanprover-community/batteries").name).toBe("Batteries");
    expect(libraryInfo("https://github.com/google-deepmind/formal-conjectures").name).toBe("Formal Conjectures");
    expect(libraryInfo("https://github.com/facebookresearch/atlas-lean").name).toBe("ATLAS (atlas-lean)");
    expect(libraryInfo("https://github.com/facebookresearch/atlas-lean").short).toBe("ATLAS");
    expect(libraryInfo("https://github.com/FormalFrontier/WidgetTheory").name).toBe("FormalFrontier · WidgetTheory");
    expect(libraryInfo("https://github.com/someone/lean-thing").name).toBe("someone/lean-thing");
    const core = { id: "lean-core", repo: "https://github.com/leanprover/lean4", commit: COMMIT, display_name: "Lean 4 core (Init, Std, Lean)" };
    expect(libraryInfo(core.repo).name).toBe("Lean core");
    expect(libraryInfo(core.repo, core).name).toBe("Lean 4 core (Init, Std, Lean)");
    expect(libraryInfo(core.repo, core).short).toBe("Lean 4 core");
  });

  it("builds pinned GitHub source links, honouring src_dir and templates, and refuses unsafe parts", () => {
    const mathlib = libraryInfo("https://github.com/leanprover-community/mathlib4");
    expect(sourceUrl(mathlib, { commit: COMMIT, path: "Mathlib/A B.lean", line: 3, end_line: 9 }))
      .toBe(`https://github.com/leanprover-community/mathlib4/blob/${COMMIT}/Mathlib/A%20B.lean#L3-L9`);
    expect(sourceUrl(mathlib, { commit: COMMIT, path: "Mathlib/A.lean", line: 3, end_line: 3 }))
      .toBe(`https://github.com/leanprover-community/mathlib4/blob/${COMMIT}/Mathlib/A.lean#L3`);
    const core = libraryInfo("https://github.com/leanprover/lean4");
    expect(sourceUrl(core, { commit: COMMIT, path: "Init/Core.lean", line: 1, end_line: 2 }))
      .toBe(`https://github.com/leanprover/lean4/blob/${COMMIT}/src/Init/Core.lean#L1-L2`);
    const custom = libraryInfo("https://git.example.org/lib", {
      id: "x", repo: "https://git.example.org/lib", commit: COMMIT,
      blob_url_template: "https://git.example.org/lib/-/blob/{commit}/{path}#L{line}",
    });
    expect(sourceUrl(custom, { commit: COMMIT, path: "X.lean", line: 4, end_line: 8 }))
      .toBe(`https://git.example.org/lib/-/blob/${COMMIT}/X.lean#L4`);
    expect(sourceUrl(libraryInfo("https://git.example.org/lib"), { commit: COMMIT, path: "X.lean", line: 1, end_line: 1 })).toBeNull();
    for (const path of ["../x.lean", "/abs.lean", "a//b.lean", "a\\b.lean", "a/./b.lean", ""]) {
      expect(sourceUrl(mathlib, { commit: COMMIT, path, line: 1, end_line: 1 }), path).toBeNull();
    }
    expect(sourceUrl(mathlib, { commit: "HEAD", path: "Mathlib/A.lean", line: 1, end_line: 1 })).toBeNull();
    expect(libraryInfo("https://github.com/a/b", { id: "b", repo: "https://github.com/a/b", commit: COMMIT, blob_url_template: "javascript:{commit}{path}" }).blobTemplate)
      .toBe("{repo}/blob/{commit}/{path}#L{line}-L{end_line}");
  });
});
