// Unit tests for the dynamic homepage + sitemap (src/home.ts, contract D-C7):
// escaping at every sink, null-count rows rendering as a muted "pending"
// (never NaN/fake zeros), alphabetical ordering, corpus-total stats, the
// recently-updated strip, and the sitemap urlset shape. Pure functions — no
// D1 shim needed.

import { describe, it, expect } from "vitest";
import { homePage, sitemapXml, type HomeRow } from "../src/home.js";
import { computeCounts } from "../scripts/backfill-counts.js";

function row(over: Partial<HomeRow> = {}): HomeRow {
  return {
    slug: "Group_theory",
    displayTitle: "Group theory",
    nFormalized: 10,
    nPartial: 3,
    nNotFormalized: 2,
    updatedAt: Date.UTC(2026, 5, 11, 12, 0, 0),
    ...over,
  };
}

describe("homePage", () => {
  it("escapes display titles and slugs at every sink", () => {
    const html = homePage([
      row({
        slug: 'Weird"slug',
        displayTitle: `<script>alert(1)</script> & "quotes" 'apos'`,
      }),
    ]);
    expect(html).not.toContain("<script>alert(1)</script>");
    expect(html).toContain("&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;quotes&quot; &#x27;apos&#x27;");
    expect(html).toContain('href="/Weird&quot;slug"');
    // data-title (lowercased filter key) is escaped too.
    expect(html).toContain('data-title="&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;quotes&quot; &#x27;apos&#x27;"');
  });

  it("renders null counts as a muted pending row (no NaN, no fake zeros)", () => {
    const html = homePage([
      row({ nFormalized: null, nPartial: null, nNotFormalized: null }),
    ]);
    expect(html).toContain('class="row-meta pending">pending<');
    expect(html).toContain('class="bar bar-empty"');
    expect(html).not.toContain("NaN");
    expect(html).not.toContain('class="row-meta untagged"'); // null ≠ counted-as-zero
    // Pending rows behave as untagged for the hide filter, with zeroed sort keys.
    expect(html).toContain('data-untagged="1"');
    expect(html).toContain('data-cov="0.0000"');
  });

  it("distinguishes counted-zero (untagged) from null (pending)", () => {
    const html = homePage([
      row({ nFormalized: 0, nPartial: 0, nNotFormalized: 0 }),
    ]);
    expect(html).toContain('class="row-meta untagged">untagged<');
    expect(html).not.toContain('class="row-meta pending"');
  });

  it("sorts directory rows alphabetically by display title, case-insensitively", () => {
    const html = homePage([
      row({ slug: "Zeta", displayTitle: "zeta function" }),
      row({ slug: "Algebra", displayTitle: "Algebra" }),
      row({ slug: "Monoid", displayTitle: "Monoid" }),
    ]);
    const dir = html.slice(html.indexOf('id="dir"'));
    const iA = dir.indexOf('href="/Algebra"');
    const iM = dir.indexOf('href="/Monoid"');
    const iZ = dir.indexOf('href="/Zeta"');
    expect(iA).toBeGreaterThan(-1);
    expect(iA).toBeLessThan(iM);
    expect(iM).toBeLessThan(iZ);
  });

  it("hero stats carry corpus totals, percentages, and the pending/untagged note", () => {
    const html = homePage([
      row({ slug: "A", displayTitle: "A", nFormalized: 6, nPartial: 2, nNotFormalized: 2 }),
      row({ slug: "B", displayTitle: "B", nFormalized: 0, nPartial: 0, nNotFormalized: 0 }),
      row({ slug: "C", displayTitle: "C", nFormalized: null, nPartial: null, nNotFormalized: null }),
    ]);
    expect(html).toContain('<span class="stat-num">3</span><span class="stat-label">articles</span>');
    expect(html).toContain('<span class="stat-num">10</span><span class="stat-label">annotated results</span>');
    expect(html).toContain('<span class="stat-num">60%</span><span class="stat-label">formalized</span>');
    expect(html).toContain('<span class="stat-num">20%</span><span class="stat-label">partial</span>');
    expect(html).toContain("<b>1</b> not yet tagged");
    expect(html).toContain("<b>1</b> awaiting count backfill");
  });

  it("omits the stats note once every row is counted and tagged", () => {
    const html = homePage([row()]);
    expect(html).not.toContain("awaiting count backfill");
    expect(html).not.toContain("not yet tagged");
    // Counted directory row shows formalized/total with the breakdown on the bar.
    expect(html).toContain('>10<span class="of">/</span>15</span>');
    expect(html).toContain('title="10 formalized · 3 partial · 2 not formalized"');
  });

  it("recently-updated strip lists newest first, capped at 8, with relative dates", () => {
    const now = Date.now();
    const rows = Array.from({ length: 10 }, (_, i) =>
      row({
        slug: `R${i}`,
        displayTitle: `R${i}`,
        updatedAt: now - (i + 1) * 3600_000, // R0 newest (1h ago) … R9 oldest
      }),
    );
    const html = homePage(rows);
    expect(html.match(/class="recent-item"/g)).toHaveLength(8);
    const i0 = html.indexOf('class="recent-item" href="/R0"');
    const i1 = html.indexOf('class="recent-item" href="/R1"');
    expect(i0).toBeGreaterThan(-1);
    expect(i0).toBeLessThan(i1);
    // R8/R9 (9th/10th newest) fall off the strip.
    expect(html).not.toContain('class="recent-item" href="/R8"');
    expect(html).toContain(">2h ago<"); // R1's relative date
  });

  it("omits the recently-updated strip for an empty corpus", () => {
    expect(homePage([])).not.toContain("Recently updated");
  });

  it("footer mirrors the article-page CC notices and links the repo", () => {
    const html = homePage([row()]);
    expect(html).toContain("https://creativecommons.org/licenses/by-sa/4.0/");
    expect(html).toContain("https://creativecommons.org/publicdomain/zero/1.0/");
    expect(html).toContain("https://github.com/Deicyde/WikiLean");
  });

  it("labels the search input for assistive tech", () => {
    const html = homePage([row()]);
    expect(html).toContain('<label class="sr" for="q">');
    expect(html).toContain('id="q" type="search"');
  });

  it("handles an empty corpus without dividing by zero", () => {
    const html = homePage([]);
    expect(html).toContain('<span class="stat-num">0</span><span class="stat-label">articles</span>');
    expect(html).toContain('<span class="stat-num">0%</span><span class="stat-label">formalized</span>');
    expect(html).not.toContain("NaN");
  });
});

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
  });

  it("escapes slugs in <loc>", () => {
    const xml = sitemapXml([{ slug: "A&B", updatedAt: Date.UTC(2026, 5, 11) }]);
    expect(xml).toContain("<loc>https://wikilean.jackmccarthy.org/A&amp;B</loc>");
    expect(xml).not.toContain("/A&B<");
  });

  it("renders an empty urlset for zero rows", () => {
    const xml = sitemapXml([]);
    expect(xml).toContain('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n</urlset>');
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
