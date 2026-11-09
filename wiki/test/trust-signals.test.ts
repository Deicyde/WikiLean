// Review badge and legend on canonical article pages.
import { describe, it, expect } from "vitest";
import { renderArticlePage } from "../src/engine/page.js";
import type { Annotation } from "../src/engine/types.js";
import {
  setup,
  get,
  SLUG,
  blockNetwork,
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

  it("article pages carry the badge and cache under render:v20", async () => {
    const h = setup();
    const res = await get(h.env, `/${SLUG}`);
    expect(res.status).toBe(200);
    const html = await res.text();
    // Seed = two ai-provenance annotations → the honest 0/2.
    expect(html).toContain(">0/2 human-reviewed<");
    expect(html).toContain('id="wl-legend-btn"');
    const keys = [...h.renderCache.store.keys()];
    expect(keys.some((k) => k.startsWith("render:v20:"))).toBe(true);
    expect(keys.some((k) => k.startsWith("render:v18:") || k.startsWith("render:v19:"))).toBe(false);
  });

});
