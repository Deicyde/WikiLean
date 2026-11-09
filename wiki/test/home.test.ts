import { describe, it, expect } from "vitest";
import { sitemapXml } from "../src/home.js";
import { computeCounts } from "../scripts/backfill-counts.js";

describe("sitemapXml", () => {
  it("emits a standard urlset with loc + ISO lastmod, sorted by slug", () => {
    const xml = sitemapXml([
      { slug: "Zorn_lemma", updatedAt: Date.UTC(2026, 5, 11) },
      { slug: "Abelian_group", updatedAt: Date.UTC(2026, 0, 2) },
    ]);
    expect(xml.startsWith('<?xml version="1.0" encoding="UTF-8"?>\n')).toBe(true);
    expect(xml).toContain('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">');
    expect(xml).toContain(
      "<url><loc>https://wikilean.jackmccarthy.org/Abelian_group</loc><lastmod>2026-01-02</lastmod><priority>0.6</priority></url>",
    );
    expect(xml).toContain(
      "<url><loc>https://wikilean.jackmccarthy.org/Zorn_lemma</loc><lastmod>2026-06-11</lastmod><priority>0.6</priority></url>",
    );
    expect(xml.indexOf("Abelian_group")).toBeLessThan(xml.indexOf("Zorn_lemma"));
    expect(xml.trimEnd().endsWith("</urlset>")).toBe(true);
    // Homepage root is listed first at top priority (brand-query target),
    // lastmod tracking the most recently updated article.
    expect(xml).toContain(
      "<url><loc>https://wikilean.jackmccarthy.org/</loc><lastmod>2026-06-11</lastmod><priority>1.0</priority></url>",
    );
    expect(xml.indexOf("<loc>https://wikilean.jackmccarthy.org/</loc>")).toBeLessThan(
      xml.indexOf("Abelian_group"),
    );
  });

  it("escapes slugs in <loc>", () => {
    // W3 fix #10: slugs are percent-encoded in <loc> (then XML-escaped).
    const xml = sitemapXml([{ slug: "A&B", updatedAt: Date.UTC(2026, 5, 11) }]);
    expect(xml).toContain("<loc>https://wikilean.jackmccarthy.org/A%26B</loc>");
    expect(xml).not.toContain("/A&B<");
  });

  it("still lists the homepage root when there are zero article rows", () => {
    const xml = sitemapXml([]);
    // No articles, but the homepage + flagship static pages always exist —
    // root first with no lastmod, then the flagships at 0.8.
    expect(xml).toContain(
      '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' +
        "  <url><loc>https://wikilean.jackmccarthy.org/</loc><priority>1.0</priority></url>\n" +
        "  <url><loc>https://wikilean.jackmccarthy.org/brain</loc><priority>0.8</priority></url>\n" +
        "  <url><loc>https://wikilean.jackmccarthy.org/mcp</loc><priority>0.8</priority></url>\n" +
        "  <url><loc>https://wikilean.jackmccarthy.org/concepts</loc><priority>0.8</priority></url>\n" +
        "  <url><loc>https://wikilean.jackmccarthy.org/about</loc><priority>0.8</priority></url>\n" +
        "</urlset>",
    );
  });
});

describe("computeCounts (backfill-counts, D-C5)", () => {
  it("counts by status and excludes rejected tombstones", () => {
    expect(
      computeCounts([
        { status: "formalized" },
        { status: "formalized" },
        { status: "partial" },
        { status: "not_formalized" },
        { status: "rejected" }, // human veto — excluded
        { status: "something_else" }, // unknown — excluded
        {},
      ]),
    ).toEqual({ n_formalized: 2, n_partial: 1, n_not_formalized: 1 });
  });

  it("returns zeros for an empty array", () => {
    expect(computeCounts([])).toEqual({ n_formalized: 0, n_partial: 0, n_not_formalized: 0 });
  });
});
