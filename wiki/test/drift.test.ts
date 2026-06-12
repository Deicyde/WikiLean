// Unit tests for the drift cron's pure logic (src/drift.ts): sweep planning
// (rotating cursor, wrap-around, batch chunking), the MediaWiki info-query
// URL contract, and response→slug classification (normalized titles,
// missing/redirect flags, drift comparison). Plus the staleness banner that
// pages.ts renders from the cron's latest_revid bookkeeping (C10).
//
// The scheduled() handler itself is not driven here — the d1shim has no
// batch() — but everything it composes is.

import { describe, it, expect } from "vitest";
import {
  planSweep,
  infoQueryUrl,
  classifyBatch,
  type ArticleLite,
  type InfoQueryResponse,
} from "../src/drift.js";
import { injectAuthAndEditor } from "../src/pages.js";

function art(slug: string, revid: number | null = 100, latestRevid: number | null = null): ArticleLite {
  return { slug, wikipediaTitle: slug.replaceAll("_", " "), revid, latestRevid };
}

describe("planSweep", () => {
  const rows = [art("Cc"), art("Aa"), art("Ee"), art("Bb"), art("Dd")]; // deliberately unsorted

  it("empty corpus → no batches, null cursor", () => {
    expect(planSweep([], null)).toEqual({ batches: [], nextCursor: null });
  });

  it("no cursor: starts at the first sorted slug, chunks by batchSize, caps at maxBatches", () => {
    const { batches, nextCursor } = planSweep(rows, null, 2, 2);
    expect(batches.map((b) => b.map((a) => a.slug))).toEqual([
      ["Aa", "Bb"],
      ["Cc", "Dd"],
    ]);
    expect(nextCursor).toBe("Dd");
  });

  it("resumes strictly after the cursor and wraps around at the end", () => {
    const { batches, nextCursor } = planSweep(rows, "Dd", 2, 2);
    expect(batches.map((b) => b.map((a) => a.slug))).toEqual([
      ["Ee", "Aa"],
      ["Bb", "Cc"],
    ]);
    expect(nextCursor).toBe("Cc");
  });

  it("cursor at/after the last slug wraps to the start", () => {
    const { batches } = planSweep(rows, "Ee", 1, 2);
    expect(batches[0].map((a) => a.slug)).toEqual(["Aa", "Bb"]);
  });

  it("a deleted cursor slug resumes at the next greater slug", () => {
    const { batches } = planSweep(rows, "Bb_gone", 1, 2);
    expect(batches[0].map((a) => a.slug)).toEqual(["Cc", "Dd"]);
  });

  it("never repeats a slug when the corpus is smaller than the cap", () => {
    const { batches, nextCursor } = planSweep(rows, "Bb", 8, 50);
    const slugs = batches.flat().map((a) => a.slug);
    expect(slugs).toEqual(["Cc", "Dd", "Ee", "Aa", "Bb"]);
    expect(new Set(slugs).size).toBe(5);
    expect(nextCursor).toBe("Bb"); // full sweep ends where it started
  });
});

describe("infoQueryUrl", () => {
  it("pipe-joins titles into one prop=info query (formatversion=2)", () => {
    const url = infoQueryUrl(["Abelian group", "Group (mathematics)"]);
    expect(url).toContain("action=query");
    expect(url).toContain("prop=info");
    expect(url).toContain("formatversion=2");
    // URLSearchParams encodes the pipe and spaces.
    expect(url).toContain("titles=Abelian+group%7CGroup+%28mathematics%29");
  });

  it("never sends a `redirects` param (MediaWiki treats any present value — even 0 — as true)", () => {
    expect(infoQueryUrl(["Foo"])).not.toContain("redirects");
  });
});

describe("classifyBatch", () => {
  it("classifies drifted / unchanged / missing / moved and maps normalized titles back to slugs", () => {
    const batch = [
      art("Abelian_group", 100), // will drift (lastrevid 200)
      art("Boolean_ring", 100), // unchanged (lastrevid 100)
      art("Cut_locus", 100), // deleted upstream
      art("Derived_set", 100), // now a redirect
    ];
    const resp: InfoQueryResponse = {
      query: {
        // MediaWiki echoes requested→normalized; pages carry the normalized title.
        normalized: [
          { from: "Abelian group", to: "Abelian Group (norm)" },
        ],
        pages: [
          { title: "Abelian Group (norm)", lastrevid: 200 },
          { title: "Boolean ring", lastrevid: 100 },
          { title: "Cut locus", missing: true },
          { title: "Derived set", redirect: true, lastrevid: 150 },
        ],
      },
    };
    const { results, unmatched } = classifyBatch(batch, resp);
    expect(unmatched).toEqual([]);
    const bySlug = Object.fromEntries(results.map((r) => [r.slug, r]));
    expect(bySlug["Abelian_group"]).toEqual({ slug: "Abelian_group", outcome: "drifted", lastrevid: 200 });
    expect(bySlug["Boolean_ring"]).toEqual({ slug: "Boolean_ring", outcome: "unchanged", lastrevid: 100 });
    expect(bySlug["Cut_locus"]).toEqual({ slug: "Cut_locus", outcome: "missing", lastrevid: null });
    expect(bySlug["Derived_set"]).toEqual({ slug: "Derived_set", outcome: "moved", lastrevid: 150 });
  });

  it("a never-pinned article (revid null) is 'unchanged', not 'drifted'", () => {
    const batch = [art("Unpinned", null)];
    const resp: InfoQueryResponse = { query: { pages: [{ title: "Unpinned", lastrevid: 999 }] } };
    const { results } = classifyBatch(batch, resp);
    expect(results).toEqual([{ slug: "Unpinned", outcome: "unchanged", lastrevid: 999 }]);
  });

  it("reports unmatched in both directions: alien response titles and unanswered slugs", () => {
    const batch = [art("Asked_for", 100)];
    const resp: InfoQueryResponse = { query: { pages: [{ title: "Something else", lastrevid: 5 }] } };
    const { results, unmatched } = classifyBatch(batch, resp);
    expect(results).toEqual([]);
    expect(unmatched).toContain("Something else"); // response title with no slug
    expect(unmatched).toContain("Asked_for"); // batch slug with no response page
  });
});

describe("staleness banner (injectAuthAndEditor)", () => {
  const PAGE = `<html><head></head><body class="show-all"><main>article</main></body></html>`;
  const base = { slug: "Test_Article", user: null, annotations: [] };

  it("latestRevid > revid → banner above the article with the diff link", () => {
    const html = injectAuthAndEditor(PAGE, { ...base, revid: 12345, latestRevid: 12399 });
    expect(html).toContain("the article has changed upstream");
    expect(html).toContain("https://en.wikipedia.org/w/index.php?diff=cur&amp;oldid=12345");
    // Injected right after <body>, i.e. above the article content.
    expect(html.indexOf("wl-stale-banner")).toBeGreaterThan(html.indexOf("<body"));
    expect(html.indexOf("wl-stale-banner")).toBeLessThan(html.indexOf("<main>"));
  });

  it("no banner when latestRevid is unknown, equal, or behind", () => {
    for (const opts of [
      { ...base, revid: 12345 },
      { ...base, revid: 12345, latestRevid: null },
      { ...base, revid: 12345, latestRevid: 12345 },
      { ...base, revid: 12345, latestRevid: 12000 },
      { ...base, latestRevid: 12399 }, // pinned revid unknown → can't compare
    ]) {
      expect(injectAuthAndEditor(PAGE, opts)).not.toContain("wl-stale-banner");
    }
  });

  it("logged-in viewers get the banner alongside the editor (with the current asset bump)", () => {
    const user = { id: "u1", name: "U", role: "user" } as never;
    const html = injectAuthAndEditor(PAGE, { ...base, user, revid: 1, latestRevid: 2, version: 7 });
    expect(html).toContain("wl-stale-banner");
    // v=8: editor.js endorse action (D-C9). NB: this assertion was stale at
    // v=6 when the editor shipped v=7 — keep it in lockstep with pages.ts.
    expect(html).toContain("/assets/editor.js?v=8");
    expect(html).not.toContain("editor.js?v=7");
  });
});
