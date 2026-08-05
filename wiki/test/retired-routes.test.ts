// The old graph stack's tombstones (301→/brain redirects for /map, /map-v2,
// /graph, /atlas, /article-graph; 410s for /graph_data.json, /atlas_data.json,
// /api/atlas) were DELETED outright 2026-08-04 ("we don't have enough users to
// worry about deprecating"). The single-segment paths now fall through to the
// /:slug article catch-all, where the RESERVED set squats the names; the
// multi-segment /api/atlas paths fall through to app.notFound. Either way the
// contract is the same: the normal clean article-404 page — never a redirect,
// never a 500, and never article creation under an old URL.

import { describe, it, expect } from "vitest";
import { setup, get, botCreate, articleRow, blockNetwork } from "./helpers/harness.js";

blockNetwork();

// Every path the deleted tombstone handlers used to answer.
const DELETED_PAGE_PATHS = ["/map", "/map-v2", "/graph", "/atlas", "/article-graph"];
const DELETED_DATA_PATHS = ["/graph_data.json", "/atlas_data.json", "/api/atlas", "/api/atlas/algebra"];

describe("deleted old-graph-stack routes", () => {
  it("every deleted path answers the plain 404 page — no redirect, no 410, no 500", async () => {
    const { env } = setup();
    for (const path of [...DELETED_PAGE_PATHS, ...DELETED_DATA_PATHS]) {
      const res = await get(env, path);
      expect(res.status, path).toBe(404);
      // Not the old tombstones: no 301 Location, and the body is the 404
      // asset page (whatever ASSETS serves for /404.html), not the 410 JSON.
      expect(res.headers.get("Location"), path).toBeNull();
      expect((res.headers.get("Content-Type") || "").includes("application/json"), path).toBe(false);
    }
  });

  it("the fallthrough never creates or serves an article under an old URL", async () => {
    const { db, env } = setup();
    for (const path of DELETED_PAGE_PATHS) {
      const slug = path.slice(1);
      await get(env, path);
      expect(articleRow(db, slug), slug).toBeUndefined();
      // RESERVED still squats the name: creation is rejected outright.
      const res = await botCreate(env, slug, { wikipedia_title: "X", annotations: [] });
      expect(res.status, slug).toBe(400);
      expect(((await res.json()) as { error: string }).error).toBe("reserved slug");
      expect(articleRow(db, slug), slug).toBeUndefined();
    }
  });
});
