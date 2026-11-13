// Public entry routes must stay usable without loading the graph or corpus.
import { describe, it, expect, vi } from "vitest";
import { setup, get, blockNetwork } from "./helpers/harness.js";

blockNetwork();

describe("public entry pages", () => {
  for (const path of ["/", "/about", "/contribute"]) {
    it(`${path} serves primary navigation and licensing without D1 reads`, async () => {
      const h = setup();
      const query = vi.spyOn(h.env.DB, "prepare");
      const response = await get(h.env, path);
      expect(response.status).toBe(200);
      const html = await response.text();
      for (const href of ["/articles", "/brain", "/contribute", "/about"]) {
        expect(html).toContain(`href="${href}"`);
      }
      expect(html).toContain("https://creativecommons.org/licenses/by-sa/4.0/");
      expect(html).toContain("https://creativecommons.org/publicdomain/zero/1.0/");
      expect(html).not.toContain("<iframe");
      expect(query).not.toHaveBeenCalled();
    });
  }

  it("replaces cached landing and About pages from the retired workflow", async () => {
    const h = setup();
    await h.renderCache.put("page:home:v12", "old landing without the example");
    await h.renderCache.put("page:about:v4", "old entry shell");
    for (const [path, key] of [["/", "page:home:v14"], ["/about", "page:about:v5"]]) {
      const first = await (await get(h.env, path)).text();
      expect(first).toContain("<!doctype html>");
      expect(h.renderCache.store.get(key)).toBe(first);
      expect(await (await get(h.env, path)).text()).toBe(first);
    }
  });

  it("distinguishes direct submission and validation from publication", async () => {
    const html = await (await get(setup().env, "/contribute")).text();
    for (const href of ["/login", "/agents", "/articles"]) expect(html).toContain(`href="${href}"`);
    expect(html).toContain("CC0 contribution agreement");
    expect(html).toContain("submit a sealed pipeline result directly");
    expect(html).toContain("accepted submissions await publication");
    expect(html).toContain("Full tag history, diffs, and revert are still planned");
    expect(html).toContain("CONTRIBUTING.md#data--research-notice");
  });
});
