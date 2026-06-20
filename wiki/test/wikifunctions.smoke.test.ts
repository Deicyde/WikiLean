import { describe, it, expect } from "vitest";
import { wikifunctionsPage } from "../src/wikifunctions.js";
import { WF_FUNCTIONS } from "../src/wikifunctions-data.js";

describe("wikifunctions tracker page", () => {
  const html = wikifunctionsPage();
  it("is a complete HTML document", () => {
    expect(html.startsWith("<!doctype html>")).toBe(true);
    expect(html.trimEnd().endsWith("</html>")).toBe(true);
  });
  it("renders all 25 corpus rows + 3 header rows", () => {
    expect(WF_FUNCTIONS.length).toBe(25);
    expect((html.match(/<tr>/g) || []).length).toBe(28);
  });
  it("links every ZID and QID outward", () => {
    for (const f of WF_FUNCTIONS) {
      expect(html).toContain(`https://www.wikifunctions.org/wiki/${f.zid}`);
      expect(html).toContain(`https://www.wikidata.org/wiki/${f.qid}`);
    }
  });
  it("deep-links named Mathlib decls and shows headline stats", () => {
    expect(html).toContain("mathlib4_docs/find/?pattern=Nat.Prime");
    expect(html).toContain(">14/25<");
    expect(html).toContain("builds green against Mathlib");
    expect(html).toContain('<a href="/wikifunctions">Wikifunctions</a>');
  });
});
