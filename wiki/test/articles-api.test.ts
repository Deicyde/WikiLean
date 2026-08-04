// GET /api/articles — the compact article index behind the header search box
// (engine/searchbox.ts). Pins the response shape ({v:1, articles:[[slug,
// title, f, p, n], …]} ordered by display title), null count-columns passing
// through as JSON null (pre-backfill rows are "unknown", not zero), the JSON
// headers, and the KV cache (page:articles-index:v1, TTL 300, TTL-only
// invalidation).

import { describe, it, expect } from "vitest";
import { setup, get, insertArticle, blockNetwork, type Harness } from "./helpers/harness.js";

blockNetwork();

const KEY = "page:articles-index:v1";

interface IndexBody {
  v: number;
  articles: Array<[string, string, number | null, number | null, number | null]>;
}

function setCounts(h: Harness, slug: string, f: number, p: number, n: number): void {
  h.db
    .prepare("UPDATE articles SET n_formalized=?, n_partial=?, n_not_formalized=? WHERE slug=?")
    .run(f, p, n, slug);
}

describe("GET /api/articles", () => {
  it("returns {v:1, articles:[[slug,title,f,p,n]…]} sorted by display title", async () => {
    const h = setup();
    // Harness seed row "Test_Article" keeps its pre-backfill NULL counts.
    insertArticle(h.db, "Abelian_group");
    setCounts(h, "Abelian_group", 6, 2, 2);
    const res = await get(h.env, "/api/articles");
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toContain("application/json");
    expect(res.headers.get("Cache-Control")).toBe("public, max-age=300");
    const body = (await res.json()) as IndexBody;
    expect(body.v).toBe(1);
    expect(body.articles).toEqual([
      ["Abelian_group", "Abelian_group", 6, 2, 2],
      // NULL count columns pass through as null — unknown, not zero.
      ["Test_Article", "Test Article", null, null, null],
    ]);
  });

  it("carries hostile titles as plain JSON data (client renders via textContent)", async () => {
    const h = setup();
    const title = `<script>alert(1)</script> & "quotes"`;
    h.db.prepare("UPDATE articles SET display_title=? WHERE slug='Test_Article'").run(title);
    const body = (await (await get(h.env, "/api/articles")).json()) as IndexBody;
    expect(body.articles[0][1]).toBe(title); // JSON round-trip, no mangling
  });

  it("is KV-cached under page:articles-index:v1 (TTL-only invalidation)", async () => {
    const h = setup();
    const first = await (await get(h.env, "/api/articles")).text();
    expect(h.renderCache.store.has(KEY)).toBe(true);
    // Mutate the DB; the cached index must still serve unchanged.
    insertArticle(h.db, "After_Cache");
    const second = await (await get(h.env, "/api/articles")).text();
    expect(second).toBe(first);
    expect((JSON.parse(second) as IndexBody).articles).toHaveLength(1);
  });
});
