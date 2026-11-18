// Phase 1, the two halves joined: the Worker over the store the PYTHON BUILDER really writes.
//
// Every other articles fixture in test/helpers/articles.ts is written by hand from the design,
// which proves the Worker reads the shape the design names but not that the builder writes it.
// This file binds the committed golden store of a mixed build —
// scripts/articles/fixtures/store-mixed, the record set beside the pipeline's synthetic sealed
// cut, with the cut's first article's marked revision — whose bytes
// scripts/articles/test_build_store.py rebuilds from its inputs byte for byte
// (StatementSetTests.test_the_golden_mixed_store_matches_a_fresh_build, on the statements/store
// branch, where the builder lives; the command is in test/helpers/articles.ts beside
// BUILT_STORE_ROOT). So neither half can drift from the other: the builder's test owns the
// bytes, and this file says what the Worker does with them.
//
// What is held here is the design's section 3 end to end, on real builder output: the list row,
// the article page and its marked revision, the counts that never fold not_checked into
// not_formalized, the dashboard and home totals, a community tag whose keys are the ones the
// pipeline sealed (and the ones phase 2's locate run will write), the 404 for records a cut has
// none of, and the located articles of the same build unchanged.

import { createHash } from "node:crypto";
import { describe, it, expect } from "vitest";
import { setup, get, post, type Harness } from "./helpers/harness.js";
import {
  articlesBuiltMixedFiles, articlesFixtureFiles, bindArticles, BUILT_CUT_CAMPAIGN, BUILT_CUT_PAGEID,
  BUILT_CUT_PATH, BUILT_LOCATED_PAGEID, MATHLIB_REPO,
} from "./helpers/articles.js";
import { CC0_AGREEMENT_VERSION } from "../src/articles-overlay.js";
import { NO_RECORDS } from "../src/articles.js";

const jsonOf = async (res: Response): Promise<any> => res.json();

// The cut's four published statements, as the builder writes them. These page ids, items and
// passage keys are the sealed set's own (scripts/pipeline/fixtures/statementset/statements/
// enwiki-1005-3006.jsonl): the builder recomputes every passage_key from the page id and the
// span's sha256 and refuses a statement whose sealed key disagrees, and the pipeline's own
// tests pin that key against the record a phase-2 locate run writes for the same passage
// (scripts/pipeline/test_statement_sets.py, "key parity"). So a tag written on this key today
// is the row that run will fill in.
const CUT_KEYS: Record<string, string> = {
  "In-text #4c9ae6f6": "26abe534b220479339ade73866cd5acdf1dfd6abd65f58ab92e8e3b3ef89f0d0",
  "In-text #785c06ff": "ad30aced41155c1aa877376c0f3b8621f07623f7601f1a344172006f1e675ee9",
  "In-text #70fb31b7": "b8a9d5369ab51ed8348f4c09918471cc5e1260262ad2a7de981ee2025838845b",
  "In-text #d814d243": "0da4c25f54a8c39f10ef968a064bc1297daac76104e07dcad6b093dfe0c59926",
};
// The one statement of that article the store does NOT publish: the cut kept it (it is prose
// about the mathematics), and no locate run will ever search it, so it is no passage of the
// article and its text runs through the marked revision unmarked.
const UNPUBLISHED_ITEM = "In-text #ed913c1a";

function built(): Harness {
  const h = setup();
  bindArticles(h.env, articlesBuiltMixedFiles());
  return h;
}

function cutArticle(): Record<string, any> {
  return JSON.parse(articlesBuiltMixedFiles().get(`articles/v1/articles/${BUILT_CUT_PAGEID}.json`)!);
}

describe("the Worker over the builder's own mixed store", () => {
  it("the builder's keys are the sealed cut's, and only its formal statements are published", () => {
    const article = cutArticle();
    expect(Object.fromEntries(article.passages.map((p: any) => [p.item, p.passage_key]))).toEqual(CUT_KEYS);
    expect(article.passages.map((p: any) => p.item)).not.toContain(UNPUBLISHED_ITEM);
    // The four store fields the design names, on the bytes the builder wrote.
    expect(article.checked).toBe(false);
    expect(article.records_sha256).toBeNull();
    expect(article.seal.schema).toBe("wikilean.statement-set/1");
    expect(article.seal.producer_commit).toMatch(/^[0-9a-f]{40}$/);
    expect(article.seal).not.toHaveProperty("skill_commit");
    for (const passage of article.passages) {
      expect([passage.outcome, passage.status, passage.locations]).toEqual(["unchecked", null, []]);
    }
    // P3's one addition beyond the design's field list: a compound's shape, which the pages
    // ignore (it is carried, like `statement` and `conjuncts`, and never displayed).
    expect(article.passages.map((p: any) => p.shape)).toEqual([null, "existence_uniqueness", null, null]);
    expect(article.summary.by_status.not_checked).toBe(4);
  });

  it("lists the cut article with none of its statements checked and an empty bar", async () => {
    const h = built();
    const html = await (await get(h.env, "/articles")).text();
    const row = html.slice(html.indexOf('data-title="sprocket bundle"'),
      html.indexOf("</tr>", html.indexOf('data-title="sprocket bundle"')));
    expect(row).toContain('<div class="articles-bar is-empty" role="img" aria-label="4 statements: none checked yet"></div>');
    expect(row).toContain('<td class="num">0 of 4 checked</td>');
    expect(row).toContain('data-passages="0"');
    expect(row).toContain('data-not-checked="4"');
    // The index shard the builder wrote puts not_checked LAST, after `libraries`; the Worker
    // reads a shard's columns by name, so the row above is right wherever the builder puts it.
    const shard = JSON.parse(articlesBuiltMixedFiles().get("articles/v1/index/8.json")!);
    expect(shard.fields.at(-1)).toBe("not_checked");
    expect(shard.fields.indexOf("not_checked")).toBeGreaterThan(shard.fields.indexOf("libraries"));
  });

  it("counts the build's statements apart from its located passages, and never as not formalized", async () => {
    const h = built();
    const html = await (await get(h.env, "/articles")).text();
    // 13 passages: 7 the record set located (one of them with no status) and 6 statements.
    // 12, not 13: a passage with no status is in no count or bar of the dashboard, as it has
    // never been, and the six statements are counted apart from the six checked passages.
    expect(html).toContain("<p>4 articles · 12 passages (6 checked, 6 not checked yet) · 7 Lean locations · 1 related declaration</p>");
    expect(html).toContain('<span class="articles-mini" data-status="not_checked"><span class="articles-ico" aria-hidden="true">○</span>6 not checked yet</span>');
    // The one not_formalized in the build is the record set's own, not a statement.
    expect(html).toContain('>Not formalized <span class="articles-n">1</span></span>');
    const current = JSON.parse(articlesBuiltMixedFiles().get("articles/v1/current.json")!);
    expect(current.counts.by_status.not_checked).toBe(6);
    expect(current.counts.by_status.not_formalized).toBe(1);
    // The importance table gains the column, with the statements under their own importance.
    expect(html).toContain('<th scope="col" class="num"><span class="articles-ico" aria-hidden="true">○</span>Not checked yet</th>');
    expect(html).toContain('<tr><th scope="row">Mid</th><td class="num">2</td><td class="num">7</td>' +
      '<td class="num" data-status="formalized">3</td><td class="num" data-status="statement_only">0</td>' +
      '<td class="num" data-status="partial">0</td><td class="num" data-status="not_formalized">0</td>' +
      '<td class="num" data-status="not_checked">4</td></tr>');
  });

  it("the home page's figures leave the statements out of the passages checked", async () => {
    const h = built();
    const html = await (await get(h.env, "/")).text();
    // 13 passages less the 6 unchecked and the 1 with no status.
    expect(html).toContain("4 articles · 6 passages checked · 4 formalized (67%).");
  });

  it("gives the cut article its counts line, the statements' noun and the About fold's cut", async () => {
    const h = built();
    const html = await (await get(h.env, BUILT_CUT_PATH)).text();
    expect(html).toContain('<p class="articles-counts" id="articles-counts"><b>4 statements</b>' +
      '<span class="articles-dot" aria-hidden="true">·</span>' +
      '<span class="articles-mini" data-status="not_checked"><span class="articles-ico" aria-hidden="true">○</span>none checked yet</span></p>');
    expect(html).toContain('data-noun="statements"');
    expect(html).not.toContain('class="articles-seg"');
    expect(html).not.toMatch(/Searched \d+ (Lean )?librar/);
    const about = html.slice(html.indexOf('id="articles-about"'), html.indexOf("</details>", html.indexOf('id="articles-about"')));
    // The seal's own words: WikiLean's pipeline cut it, and no locate run has run. P3 writes
    // producer_commit where a record set writes skill_commit, and the fold says so.
    expect(about).toContain("Statement set <code>pipeline-statement-fixture</code> (wikilean.statement-set/1), " +
      "cut 12 Nov 2026 by WikiLean at commit");
    expect(about).not.toContain("by formal-companion at commit");
    expect(about).toContain("No locate run has run over these statements");
    expect(about).toContain("No library has been searched for these statements.");
    expect(about).not.toContain("status rule");
  });

  it("serves the builder's marked revision with the neutral highlight on every statement", async () => {
    const h = built();
    const html = await (await get(h.env, BUILT_CUT_PATH)).text();
    const stored = articlesBuiltMixedFiles().get(`articles/v1/reader/${BUILT_CUT_PAGEID}.html`)!;
    // The body is served byte for byte, and every mark carries the passage's own outcome.
    expect(html).toContain(stored.slice(stored.indexOf("<p>"), stored.lastIndexOf("</p>") + 4));
    expect(html.match(/<mark class="atl" data-p="[0-9a-f]+" data-status="unchecked"/g)).toHaveLength(4);
    // No mark and no panel of this page carries a status a search would have given it; the
    // legend still names the vocabulary, each unused status with a zero (`is-zero`).
    expect(html).not.toMatch(/<mark[^>]*data-status="(?!unchecked)/);
    expect(html).toContain('<span class="articles-chip is-static is-zero" data-status="not_formalized">');
    // The statement the store does not publish is text, not a highlight.
    expect(html).toContain("The reader may picture this as a gadget glued along each of its handles.");
    expect(html.match(/<mark class="atl"/g)).toHaveLength(4);
    // Its panel is the card: the words, no verdict buttons, and the tag form for a signed-in
    // reader (a verdict is a judgment on a sealed location, and there is none).
    expect(html).toContain("Not checked yet — no library has been searched for this statement.");
    expect(html).not.toContain('class="articles-fb"');
    expect(html).not.toContain('class="articles-absence"');
    const signedIn = await (await get(h.env, BUILT_CUT_PATH, { user: "u-human" })).text();
    expect(signedIn.match(/class="articles-tag-open"/g)).toHaveLength(4);
    expect(signedIn).not.toContain('class="articles-fb-btn"');
  });

  it("offers the cut article's view model and 404s the records it has none of", async () => {
    const h = built();
    const res = await get(h.env, `/api/articles/records/${BUILT_CUT_PAGEID}`);
    expect(res.status).toBe(404);
    expect((await jsonOf(res)).error).toBe(NO_RECORDS);
    expect(articlesBuiltMixedFiles().has(`articles/v1/records/${BUILT_CUT_PAGEID}.jsonl`)).toBe(false);
    const view = await jsonOf(await get(h.env, `/api/articles/article/${BUILT_CUT_PAGEID}`));
    expect(view).toEqual(cutArticle());
    // The record set's own articles still serve their records out of the same build.
    expect((await get(h.env, `/api/articles/records/${BUILT_LOCATED_PAGEID}`)).status).toBe(200);
  });

  it("takes a community tag on a statement, on the key the pipeline sealed", async () => {
    const h = built();
    h.db.prepare("INSERT OR REPLACE INTO user_agreements (user_id, cc0_version, accepted_at, verified_at) VALUES (?,?,?,NULL)")
      .run("u-human", CC0_AGREEMENT_VERSION, Date.now());
    const passageKey = CUT_KEYS["In-text #4c9ae6f6"];
    const declaration = "Sprocket.bundle";
    const res = await post(h.env, "/api/articles/tag", {
      pageid: BUILT_CUT_PAGEID, campaign: BUILT_CUT_CAMPAIGN, passage_key: passageKey,
      library: "mathlib", declaration, relation: "exactMatch",
    }, { user: "u-human" });
    expect(res.status).toBe(200);
    const body = await jsonOf(res);
    // pair_key = sha256("<passage_key>\n<repository>\n<declaration>"), scripts/pipeline/keys.py:
    // the tagging universe's repo for mathlib is the one phase 2 will search, and pair_key has
    // no commit in it, so this row is the row the pipeline's location lands on.
    expect(body.pair_key).toBe(createHash("sha256")
      .update(`${passageKey}\n${MATHLIB_REPO}\n${declaration}`, "utf8").digest("hex"));
    expect(body.status).toBe("statement_only");
    const row = h.db.prepare("SELECT * FROM article_tags WHERE pair_key = ?").get(body.pair_key) as Record<string, unknown>;
    expect(row).toMatchObject({ pageid: BUILT_CUT_PAGEID, passage_key: passageKey, repository: MATHLIB_REPO,
      origin: "community", state: "live", revision: 3006 });

    // The sealed store is untouched, and the card still says no library was searched.
    const view = await jsonOf(await get(h.env, `/api/articles/article/${BUILT_CUT_PAGEID}`));
    expect(view).toEqual(cutArticle());
    const page = await (await get(h.env, BUILT_CUT_PATH, { user: "u-human" })).text();
    expect(page).toContain("With community edits: Statement only. As published it is Not checked yet.");
    expect(page).toContain("Not checked yet — no library has been searched for this statement.");

    // Withdrawing it leaves the statement not checked — never not formalized.
    const removed = await post(h.env, `/api/articles/tag/${body.pair_key}/state`, {
      pageid: BUILT_CUT_PAGEID, campaign: BUILT_CUT_CAMPAIGN, state: "removed", reason: "the wrong bundle",
      base_version: 1,
    }, { user: "u-human" });
    expect(removed.status).toBe(200);
    expect((await jsonOf(removed)).status).toBe("not_checked");
  });

  it("refuses a library the cut's tagging universe does not name", async () => {
    const h = built();
    h.db.prepare("INSERT OR REPLACE INTO user_agreements (user_id, cc0_version, accepted_at, verified_at) VALUES (?,?,?,NULL)")
      .run("u-human", CC0_AGREEMENT_VERSION, Date.now());
    const res = await post(h.env, "/api/articles/tag", {
      pageid: BUILT_CUT_PAGEID, campaign: BUILT_CUT_CAMPAIGN, passage_key: CUT_KEYS["In-text #785c06ff"],
      library: "not-a-library", declaration: "Sprocket.cover", relation: "exactMatch",
    }, { user: "u-human" });
    expect(res.status).toBe(400);
    expect((await jsonOf(res)).error).toBe("unknown library for this article's universe");
  });

  it("leaves the located articles of the same build exactly as they were", async () => {
    const located = setup();
    bindArticles(located.env, articlesFixtureFiles());
    const h = built();
    for (const path of [`/articles/${BUILT_LOCATED_PAGEID}/Widget_lemma`, "/articles/1003/Gadget_(mathematics)"]) {
      const before = await (await get(located.env, path)).text();
      const after = await (await get(h.env, path)).text();
      expect(after, path).toBe(before);
      expect(after, path).not.toContain("not_checked");
    }
    // Their view models gain `checked: true` and the sixth zero count, and nothing else.
    const plain = JSON.parse(articlesFixtureFiles().get(`articles/v1/articles/${BUILT_LOCATED_PAGEID}.json`)!);
    const mixed = JSON.parse(articlesBuiltMixedFiles().get(`articles/v1/articles/${BUILT_LOCATED_PAGEID}.json`)!);
    expect(mixed.checked).toBe(true);
    expect(mixed.summary.by_status.not_checked).toBe(0);
    delete mixed.checked;
    delete mixed.supplements;
    delete mixed.summary.by_status.not_checked;
    for (const bucket of Object.values(mixed.summary.by_prominence) as Array<Record<string, any>>) {
      delete bucket.by_status.not_checked;
    }
    expect(mixed).toEqual(plain);
  });
});
