// The one site header (src/site-shell.ts), which every Worker-rendered page now
// carries. Its `context.label` is the only part of it a page fills with a value that
// is not a constant — the annotated article page passes the article's own title,
// which comes out of D1 — so this pins that it is escaped, and escaped exactly once,
// whichever branch renders it.

import { describe, expect, it } from "vitest";
import { contextStrip, siteHeader } from "../src/site-shell.js";
import { renderArticlePage } from "../src/engine/page.js";
import { blockNetwork, get, setup, SLUG } from "./helpers/harness.js";

blockNetwork();

const HOSTILE = `Zeta & "Beta" <script>alert(1)</script> 'x'`;
const ESCAPED = `Zeta &amp; "Beta" &lt;script&gt;alert(1)&lt;/script&gt; 'x'`;

describe("the one site header", () => {
  // The link branch escaped the label and the plain branch did not, so the same
  // option meant two different things depending on whether a page passed an href.
  it("escapes context.label the same way with and without a link", () => {
    const plain = siteHeader({ context: { label: HOSTILE } });
    const linked = siteHeader({ context: { label: HOSTILE, href: "/articles" } });
    expect(plain).toContain(`<span class="wl-article">${ESCAPED}</span>`);
    expect(linked).toContain(`<a class="wl-article" href="/articles">${ESCAPED}</a>`);
    for (const html of [plain, linked]) {
      expect(html).not.toContain("<script>alert(1)</script>");
      expect(html).not.toContain("&amp;amp;"); // escaped once, never twice
    }
  });

  it("escapes the href and the class it is given, so neither can close its attribute", () => {
    const html = siteHeader({ context: { label: "x", href: `/a"onmouseover=alert(1)`, cls: `c" onclick="alert(1)` } });
    // The quotes are entities, so the handler text stays inside the attribute it was
    // given and never becomes an attribute of its own.
    expect(html).toContain(`<a class="wl-article c&quot; onclick=&quot;alert(1)" href="/a&quot;onmouseover=alert(1)">x</a>`);
    expect(html).not.toContain(`onclick="alert(1)"`);
  });

  // Every page gets the nav and the toggle; a page that asks for neither context nor
  // search still has both.
  it("always carries the shared nav and the theme toggle", () => {
    const html = siteHeader();
    for (const href of ["/articles", "/brain", "/contribute", "/about"]) {
      expect(html).toContain(`href="${href}"`);
    }
    expect(html).toContain(`id="wl-theme-toggle"`);
    expect(html).toContain(`<nav class="wl-nav" aria-label="Site">`);
    expect(html).not.toContain("<span class=\"wl-sep\"");
  });

  it("the context strip is one row, and leaves out the parts a page does not fill", () => {
    expect(contextStrip({ title: "T" })).toBe(`<div class="wl-ctx"><div class="wl-ctx-in"><span class="wl-ctx-h">T</span></div></div>`);
    expect(contextStrip({})).toBe(`<div class="wl-ctx"><div class="wl-ctx-in"></div></div>`);
  });
});

// The shared stylesheet is pinned by ?v= in four places (home.ts, pages.ts,
// articles-render.ts, engine/page.ts). A page left on an older query serves the OLD
// bytes from the edge and the browser cache, which is the whole point of the pin.
describe("the shared stylesheet's version", () => {
  const STYLE = "/assets/style.css?v=12";

  it("is the same on every page that links it", async () => {
    const h = setup();
    for (const path of ["/", "/about", "/contribute", "/stats", "/recent-changes", `/${SLUG}`]) {
      const html = await (await get(h.env, path)).text();
      const links = [...html.matchAll(/href="(\/assets\/style\.css[^"]*)"/g)].map((m) => m[1]);
      expect(new Set(links), path).toEqual(new Set([STYLE]));
    }
  });

  // These two keep their own <header> and their own inline palette, and link no
  // stylesheet at all. Their cache keys say so (index.ts); restyling one onto the shared
  // layer changes its bytes and needs its key bumped, which this test is here to catch.
  it("is not on the two Wikifunctions pages, which are not restyled", async () => {
    const h = setup();
    for (const path of ["/wikifunctions", "/wikifunctions/verify"]) {
      const html = await (await get(h.env, path)).text();
      expect(html, path).not.toContain('href="/assets/style.css');
      expect(html, path).toContain("--accent:");  // its own palette, stated inline
    }
  });
});

describe("the annotated article page's header", () => {
  // The page escapes the title for <title> itself; it must hand siteHeader the plain
  // text, or the header shows the entities through ("&amp;" where "&" belongs).
  it("shows an article title with markup in it once, escaped, in the header", () => {
    const html = renderArticlePage({
      slug: "Hostile_title",
      displayTitle: HOSTILE,
      wikipediaTitle: "Hostile title",
      body: "",
      annotations: [],
      matched: [],
      wpHtml: "",
    });
    expect(html).toContain(`<span class="wl-article">${ESCAPED}</span>`);
    expect(html).not.toContain("&amp;amp;");
    expect(html).not.toContain("<script>alert(1)</script>");
  });
});
