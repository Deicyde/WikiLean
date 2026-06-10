import { describe, it, expect } from "vitest";
import { wrapAnnotations } from "../src/engine/wrap.js";
import { buildClientData, renderArticlePage } from "../src/engine/page.js";
import type { Annotation } from "../src/engine/types.js";

// Human-deletion tombstones (status="rejected", C8): the wrap engine must
// never emit a wrap for them, must not report them as anchor failures
// (matched[i] === true means "wrapped OR deliberately excluded"), and they
// must not reach anonymous readers via badge counts or __WL_ANNOTATIONS__.
// The current corpus has zero rejected annotations, so the golden parity
// suite can't exercise this path — this synthetic fixture does.

const HTML = [
  "<h2>Definitions</h2>",
  "<p>A prime ideal is a proper ideal whose complement is multiplicatively closed.</p>",
  "<p>A maximal ideal is a proper ideal not contained in any larger proper ideal.</p>",
].join("\n");

const live: Annotation = {
  status: "formalized",
  provenance: "ai",
  anchor: { section: "Definitions", snippet: "A prime ideal is a proper ideal" },
};

const tombstone: Annotation = {
  status: "rejected",
  provenance: "human",
  anchor: { section: "Definitions", snippet: "A maximal ideal is a proper ideal" },
};

describe("tombstones (status=rejected)", () => {
  it("skips rejected annotations while still wrapping the others", () => {
    const { html, matched } = wrapAnnotations(HTML, [live, tombstone]);
    // The live annotation is wrapped; the tombstoned one is not.
    expect(html).toContain('class="anno anno-formalized"');
    expect(html).toContain('data-anno-indices="0"');
    expect((html.match(/class="anno /g) ?? []).length).toBe(1);
    expect(html).not.toContain("rejected");
    expect(html).toContain("<p>A maximal ideal is a proper ideal not contained");
    // matched semantics: true-or-excluded — a veto is not an anchor failure,
    // so save responses / telemetry never report it as anchor rot.
    expect(matched).toEqual([true, true]);
  });

  it("emits no edits at all when every annotation is rejected", () => {
    const { html, matched } = wrapAnnotations(HTML, [tombstone]);
    expect(html).toBe(HTML);
    expect(matched).toEqual([true]);
  });

  it("does not let a rejected human annotation promote a shared wrap to human provenance", () => {
    const tombSameAnchor: Annotation = {
      status: "rejected",
      provenance: "human",
      anchor: { ...live.anchor },
    };
    const { html } = wrapAnnotations(HTML, [live, tombSameAnchor]);
    expect(html).toContain('data-provenance="ai"');
    expect(html).toContain('data-anno-indices="0"');
    expect(html).not.toContain('data-anno-indices="0,1"');
  });

  it("replaces tombstones with null in the anonymous client data, preserving index alignment", () => {
    const data = buildClientData([live, tombstone]);
    expect(data.length).toBe(2);
    expect(data[0]?.status).toBe("formalized");
    expect(data[1]).toBeNull();
  });

  it("excludes tombstones from header badge counts and ships none of their content", () => {
    const page = renderArticlePage({
      slug: "Tombstone_test",
      displayTitle: "Tombstone test",
      wikipediaTitle: "Tombstone test",
      body: "",
      annotations: [tombstone],
      matched: [true],
      wpHtml: "",
    });
    expect(page).toContain(">0 formalized<");
    expect(page).toContain(">0 partial<");
    expect(page).toContain(">0 not formalized<");
    expect(page).toContain("window.__WL_ANNOTATIONS__ = [null]");
  });
});
