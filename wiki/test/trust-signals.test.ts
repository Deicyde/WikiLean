// Trust signals (P2): the N/M human-reviewed header badge + the "?" legend
// popover on article pages (engine/page.ts, render:v17), and the landing
// page's "Least recently reviewed" strip (home.ts brainLanding via GET /,
// page:home:v10). Badge/legend math is unit-tested on renderArticlePage
// directly; the strip's query (NULLS FIRST ordering, parked-state exclusion,
// XSS at the title sink) is exercised end-to-end through the app.

import { describe, it, expect } from "vitest";
import { renderArticlePage } from "../src/engine/page.js";
import type { Annotation } from "../src/engine/types.js";
import {
  setup,
  get,
  SLUG,
  blockNetwork,
  insertArticle,
  insertModState,
} from "./helpers/harness.js";

function page(annotations: Annotation[]): string {
  return renderArticlePage({
    slug: "Badge_test",
    displayTitle: "Badge test",
    wikipediaTitle: "Badge test",
    body: "",
    annotations,
    matched: annotations.map(() => true),
    wpHtml: "",
  });
}

const ann = (status: string, provenance?: string): Annotation =>
  ({ status, ...(provenance ? { provenance } : {}) }) as Annotation;

describe("__WL_MATCHED__ client emission", () => {
  it("emits the wrap engine's matched[] index-aligned, verbatim", () => {
    // The editor needs the real per-annotation match results to tell anchor
    // rot (matched=false → Re-anchor) from an overlap-suppressed highlight
    // (matched=true but unwrapped → NO Re-anchor: the anchor is healthy and
    // re-saving would only launder provenance to human).
    const html = renderArticlePage({
      slug: "Matched_test",
      displayTitle: "Matched test",
      wikipediaTitle: "Matched test",
      body: "",
      annotations: [ann("formalized", "human"), ann("partial"), ann("rejected", "human")],
      matched: [true, false, true], // tombstone reports true per engine contract
      wpHtml: "",
    });
    expect(html).toContain("window.__WL_MATCHED__ = [true,false,true];");
  });
});

describe("human-reviewed header badge (P2)", () => {
  it("counts N = provenance 'human' over M = non-rejected annotations", () => {
    const html = page([
      ann("formalized", "human"),
      ann("partial", "ai"),
      ann("not_formalized", "ai-moderated"),
    ]);
    expect(html).toContain(">1/3 human-reviewed<");
  });

  it("excludes rejected tombstones from BOTH sides of the fraction", () => {
    const html = page([
      ann("formalized", "human"),
      ann("partial", "ai"),
      // Human tombstone: its 'human' provenance must not inflate N, and the
      // tombstone itself must not inflate M.
      ann("rejected", "human"),
    ]);
    expect(html).toContain(">1/2 human-reviewed<");
  });

  it("renders 0/M when no annotation is human-reviewed (honesty case)", () => {
    const html = page([ann("formalized", "ai"), ann("partial", "ai")]);
    expect(html).toContain(">0/2 human-reviewed<");
    // Hover explanation rides on the badge itself.
    expect(html).toMatch(/wl-human-reviewed" title="0 of 2 annotations/);
  });

  it("matches provenance by exact string — 'ai-moderated' is not 'human'", () => {
    const html = page([ann("formalized", "ai-moderated"), ann("partial", "Human" as string)]);
    expect(html).toContain(">0/2 human-reviewed<");
  });
});

describe("legend popover (P2)", () => {
  it("renders a keyboard-accessible button wired to the popover", () => {
    const html = page([]);
    expect(html).toContain(
      '<button id="wl-legend-btn" class="wl-legend-btn" type="button" aria-expanded="false" aria-controls="wl-legend-pop"',
    );
    // Popover starts hidden and is closable via Escape (inline script).
    expect(html).toContain('<div id="wl-legend-pop" class="wl-legend-pop" role="region"');
    expect(html).toMatch(/id="wl-legend-pop"[^>]*hidden/);
    expect(html).toContain('e.key==="Escape"');
    expect(html).toContain('b.setAttribute("aria-expanded"');
  });

  it("explains the three statuses, dimming, the badge, and the flag affordance", () => {
    const html = page([]);
    const pop = html.slice(html.indexOf('id="wl-legend-pop"'), html.indexOf("</ul>"));
    expect(pop).toContain("<b>Formalized</b>");
    expect(pop).toContain("<b>Partial</b>");
    expect(pop).toContain("<b>Not formalized</b>");
    expect(pop).toContain("Dim unannotated");
    expect(pop).toContain("human-reviewed");
    expect(pop).toContain("⚑");
  });
});

describe("trust signals through the app", () => {
  blockNetwork();

  it("article pages carry the badge and cache under render:v17", async () => {
    const h = setup();
    const res = await get(h.env, `/${SLUG}`);
    expect(res.status).toBe(200);
    const html = await res.text();
    // Seed = two ai-provenance annotations → the honest 0/2.
    expect(html).toContain(">0/2 human-reviewed<");
    expect(html).toContain('id="wl-legend-btn"');
    const keys = [...h.renderCache.store.keys()];
    expect(keys.some((k) => k.startsWith("render:v17:"))).toBe(true);
    expect(keys.some((k) => k.startsWith("render:v16:"))).toBe(false);
  });

  it("landing strip orders NULLS FIRST, excludes parked states, caches under page:home:v10", async () => {
    const h = setup();
    const now = Date.now();
    // Seed Test_Article has no moderation row → never reviewed.
    insertArticle(h.db, "Old_Review");
    insertModState(h.db, "Old_Review", { lastReviewedAt: now - 10 * 86400_000 });
    insertArticle(h.db, "Fresh_Review");
    insertModState(h.db, "Fresh_Review", { lastReviewedAt: now - 3600_000 });
    // Parked states are excluded even though they'd sort first (never reviewed).
    const parkIns = h.db.prepare(
      "INSERT INTO moderation_state (slug, state, updated_at) VALUES (?,?,?)",
    );
    for (const [slug, state] of [
      ["Parked_Moved", "moved"],
      ["Parked_Deleted", "deleted"],
      ["Parked_Needs", "needs_human"],
    ] as const) {
      insertArticle(h.db, slug);
      parkIns.run(slug, state, now);
    }

    const res = await get(h.env, "/");
    expect(res.status).toBe(200);
    const html = await res.text();
    expect(h.renderCache.store.has("page:home:v10")).toBe(true);
    expect(h.renderCache.store.has("page:home:v9")).toBe(false);

    // Never-reviewed rows (slug-ordered) precede reviewed rows, oldest review
    // first; parked slugs never appear.
    const iSeed = html.indexOf('class="rv-item" href="/Test_Article"');
    const iOld = html.indexOf('class="rv-item" href="/Old_Review"');
    const iFresh = html.indexOf('class="rv-item" href="/Fresh_Review"');
    expect(iSeed).toBeGreaterThan(-1);
    expect(iOld).toBeGreaterThan(iSeed);
    expect(iFresh).toBeGreaterThan(iOld);
    expect(html).not.toContain('href="/Parked_Moved"');
    expect(html).not.toContain('href="/Parked_Deleted"');
    expect(html).not.toContain('href="/Parked_Needs"');
    // Row labels: never reviewed vs relative review age.
    expect(html).toContain("<em>never reviewed</em>");
    expect(html).toContain("<em>reviewed 10d ago</em>");
    expect(html).toContain("<em>reviewed 1h ago</em>");
  });

  it("caps the strip at 8 rows", async () => {
    const h = setup();
    for (let i = 0; i < 10; i++) insertArticle(h.db, `Never_${i}`); // + seed = 11 never-reviewed
    const html = await (await get(h.env, "/")).text();
    expect(html.match(/class="rv-item"/g)).toHaveLength(8);
  });

  it("escapes a hostile display_title in the strip (renders as text)", async () => {
    const h = setup();
    const now = Date.now();
    h.db
      .prepare(
        "INSERT INTO articles (slug, wikipedia_title, display_title, annotations, version, created_at, updated_at) VALUES (?,?,?,?,1,?,?)",
      )
      .run("Hostile", "Hostile", '<img src=x onerror=alert(1)>"><script>alert(2)</script>', "[]", now, now);
    const html = await (await get(h.env, "/")).text();
    expect(html).not.toContain("<img src=x");
    expect(html).not.toContain("<script>alert(2)</script>");
    expect(html).toContain(
      "&lt;img src=x onerror=alert(1)&gt;&quot;&gt;&lt;script&gt;alert(2)&lt;/script&gt;",
    );
  });
});
