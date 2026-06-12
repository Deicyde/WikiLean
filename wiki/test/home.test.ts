// Unit tests for the dynamic homepage + sitemap (src/home.ts, contract D-C7):
// escaping at every sink, null-count rows rendering as em-dashes (pending
// backfill, never NaN/0), alphabetical ordering, corpus-total stats, and the
// sitemap urlset shape. Pure functions — no D1 shim needed.

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

  it("renders null counts as an em-dash pending backfill (no NaN, no fake zeros)", () => {
    const html = homePage([
      row({ nFormalized: null, nPartial: null, nNotFormalized: null }),
    ]);
    expect(html).toContain("&mdash; counts pending");
    expect(html).toContain('class="bar bar-empty"');
    expect(html).not.toContain("NaN");
    expect(html).not.toContain('class="counts untagged"'); // null ≠ counted-as-zero
    // Pending rows behave as untagged for the hide filter, with zeroed sort keys.
    expect(html).toContain('data-untagged="1"');
    expect(html).toContain('data-cov="0.0000"');
  });

  it("distinguishes counted-zero (not yet tagged) from null (pending)", () => {
    const html = homePage([
      row({ nFormalized: 0, nPartial: 0, nNotFormalized: 0 }),
    ]);
    expect(html).toContain("not yet tagged");
    expect(html).not.toContain("counts pending");
  });

  it("sorts cards alphabetically by display title, case-insensitively", () => {
    const html = homePage([
      row({ slug: "Zeta", displayTitle: "zeta function" }),
      row({ slug: "Algebra", displayTitle: "Algebra" }),
      row({ slug: "Monoid", displayTitle: "Monoid" }),
    ]);
    const iA = html.indexOf('href="/Algebra"');
    const iM = html.indexOf('href="/Monoid"');
    const iZ = html.indexOf('href="/Zeta"');
    expect(iA).toBeGreaterThan(-1);
    expect(iA).toBeLessThan(iM);
    expect(iM).toBeLessThan(iZ);
  });

  it("header stats carry corpus totals, percentages, and the pending count", () => {
    const html = homePage([
      row({ slug: "A", displayTitle: "A", nFormalized: 6, nPartial: 2, nNotFormalized: 2 }),
      row({ slug: "B", displayTitle: "B", nFormalized: 0, nPartial: 0, nNotFormalized: 0 }),
      row({ slug: "C", displayTitle: "C", nFormalized: null, nPartial: null, nNotFormalized: null }),
    ]);
    expect(html).toContain("<b>3</b> articles");
    expect(html).toContain("<b>10</b> annotated results");
    expect(html).toContain("<b>60%</b> formalized");
    expect(html).toContain("<b>1</b> not yet tagged");
    expect(html).toContain("<b>1</b> awaiting count backfill");
  });

  it("omits the backfill stat once every row has counts", () => {
    const html = homePage([row()]);
    expect(html).not.toContain("awaiting count backfill");
    // Counted card shows the three badges + total.
    expect(html).toContain("&middot; 15 total");
  });

  it("handles an empty corpus without dividing by zero", () => {
    const html = homePage([]);
    expect(html).toContain("<b>0</b> articles");
    expect(html).toContain("<b>0%</b> formalized");
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
