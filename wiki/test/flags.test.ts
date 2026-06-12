// Anonymous flag pipeline (Wave D, contract D-C4): POST /api/flag/:slug
// (no auth, IP-keyed limiter, silent per-target cap), the patroller/admin
// resolve endpoint with flag_count decrement, the /api/flags list, and the
// /flags patrol page gating.

import { createHash } from "node:crypto";
import { describe, it, expect } from "vitest";
import {
  setup,
  post,
  get,
  flagRows,
  moderationRow,
  blockNetwork,
  SLUG,
  TEST_IP,
  type Harness,
  type ReqOpts,
} from "./helpers/harness.js";

blockNetwork();

const ANNO_ID = "aaaaaaaaaaaa"; // seed annotation
const TEST_IP_HASH = createHash("sha256").update(TEST_IP).digest("hex");

function flag(
  h: Harness,
  body: Record<string, unknown>,
  opts: ReqOpts = { ip: TEST_IP },
  slug = SLUG,
): Promise<Response> {
  return post(h.env, `/api/flag/${slug}`, body, opts);
}

function resolve(h: Harness, id: number, resolution: string, user = "u-patroller"): Promise<Response> {
  return post(h.env, `/api/flag/${id}/resolve`, { resolution }, { user });
}

describe("POST /api/flag/:slug", () => {
  it("anonymous flag inserts a row, hashes the IP, and bumps moderation_state.flag_count", async () => {
    const h = setup();
    const res = await flag(h, { annotation_id: ANNO_ID, reason: "wrong_decl", comment: "decl points elsewhere" });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true });

    const rows = flagRows(h.db);
    expect(rows.length).toBe(1);
    expect(rows[0]).toMatchObject({
      slug: SLUG,
      annotation_id: ANNO_ID,
      reason: "wrong_decl",
      comment: "decl points elsewhere",
      user_id: null, // anonymous
      ip_hash: TEST_IP_HASH, // pseudonymous, never the raw IP
      status: "open",
      resolved_by: null,
      resolved_at: null,
    });
    expect(moderationRow(h.db)!.flag_count).toBe(1);

    // A second report (different reason) stacks the count.
    expect((await flag(h, { annotation_id: ANNO_ID, reason: "wrong_status" })).status).toBe(200);
    expect(flagRows(h.db).length).toBe(2);
    expect(moderationRow(h.db)!.flag_count).toBe(2);
  });

  it("article-level flag (no annotation_id) works; logged-in reporters get user_id recorded", async () => {
    const h = setup();
    const res = await flag(h, { reason: "other", comment: "whole page is stale" }, { ip: TEST_IP, user: "u-human" });
    expect(res.status).toBe(200);
    const rows = flagRows(h.db);
    expect(rows[0].annotation_id).toBeNull();
    expect(rows[0].user_id).toBe("u-human");
  });

  it("silent cap: the 6th open flag on the same target reports ok but does not insert", async () => {
    const h = setup();
    for (let i = 0; i < 5; i++) {
      expect((await flag(h, { annotation_id: ANNO_ID, reason: "other" })).status).toBe(200);
    }
    expect(flagRows(h.db).length).toBe(5);
    expect(moderationRow(h.db)!.flag_count).toBe(5);

    const sixth = await flag(h, { annotation_id: ANNO_ID, reason: "other" });
    expect(sixth.status).toBe(200);
    expect(await sixth.json()).toEqual({ ok: true }); // indistinguishable from success
    expect(flagRows(h.db).length).toBe(5);
    expect(moderationRow(h.db)!.flag_count).toBe(5);

    // The cap is per (slug, annotation_id-or-null): an article-level flag still lands.
    expect((await flag(h, { reason: "other" })).status).toBe(200);
    expect(flagRows(h.db).length).toBe(6);
    expect(moderationRow(h.db)!.flag_count).toBe(6);
  });

  it("rate limiter (FLAG_LIMITER) → 429, nothing inserted", async () => {
    const h = setup({ flagLimiterAllows: false });
    const res = await flag(h, { annotation_id: ANNO_ID, reason: "other" });
    expect(res.status).toBe(429);
    expect(flagRows(h.db).length).toBe(0);
  });

  it("validation: unknown slug 404, bad reason/annotation_id/oversized comment 400, cross-origin 403", async () => {
    const h = setup();
    expect((await flag(h, { reason: "other" }, { ip: TEST_IP }, "No_Such_Article")).status).toBe(404);
    expect((await flag(h, { reason: "rude" })).status).toBe(400);
    expect((await flag(h, {})).status).toBe(400);
    expect((await flag(h, { reason: "other", annotation_id: "nope" })).status).toBe(400);
    expect((await flag(h, { reason: "other", comment: "x".repeat(501) })).status).toBe(400);
    expect((await flag(h, { reason: "other" }, { ip: TEST_IP, origin: "https://evil.example" })).status).toBe(403);
    expect(flagRows(h.db).length).toBe(0);
    expect(moderationRow(h.db)).toBeUndefined();
  });
});

describe("POST /api/flag/:id/resolve", () => {
  async function seedFlag(h: Harness): Promise<number> {
    expect((await flag(h, { annotation_id: ANNO_ID, reason: "wrong_decl" })).status).toBe(200);
    return flagRows(h.db)[0].id;
  }

  it("role gate: anonymous and plain users 403; patroller resolves and decrements flag_count", async () => {
    const h = setup();
    const id = await seedFlag(h);
    expect(moderationRow(h.db)!.flag_count).toBe(1);

    expect((await post(h.env, `/api/flag/${id}/resolve`, { resolution: "fixed" })).status).toBe(403);
    expect((await resolve(h, id, "fixed", "u-human")).status).toBe(403);
    expect(flagRows(h.db)[0].status).toBe("open");

    const res = await resolve(h, id, "fixed");
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true });
    const row = flagRows(h.db)[0];
    expect(row.status).toBe("fixed");
    expect(row.resolved_by).toBe("u-patroller");
    expect(row.resolved_at).toBeGreaterThan(0);
    expect(moderationRow(h.db)!.flag_count).toBe(0);
  });

  it("admin may resolve too; 'dismissed' is the other valid resolution", async () => {
    const h = setup();
    const id = await seedFlag(h);
    expect((await resolve(h, id, "dismissed", "u-admin")).status).toBe(200);
    expect(flagRows(h.db)[0].status).toBe("dismissed");
  });

  it("re-resolving 404s and does not double-decrement; flag_count floors at 0", async () => {
    const h = setup();
    const id = await seedFlag(h);
    // Simulate a count already drained (e.g. manual SQL) — floor still holds.
    h.db.prepare("UPDATE moderation_state SET flag_count = 0 WHERE slug = ?").run(SLUG);
    expect((await resolve(h, id, "fixed")).status).toBe(200);
    expect(moderationRow(h.db)!.flag_count).toBe(0); // MAX(0-1, 0)

    expect((await resolve(h, id, "fixed")).status).toBe(404);
    expect(moderationRow(h.db)!.flag_count).toBe(0);
  });

  it("validation: bad resolution 400, unknown/non-numeric id 404/400", async () => {
    const h = setup();
    const id = await seedFlag(h);
    expect((await resolve(h, id, "wontfix")).status).toBe(400);
    expect((await resolve(h, 99999, "fixed")).status).toBe(404);
    expect((await post(h.env, "/api/flag/abc/resolve", { resolution: "fixed" }, { user: "u-patroller" })).status).toBe(400);
  });
});

describe("GET /api/flags + GET /flags (patrol surfaces)", () => {
  it("/api/flags is patroller/admin-only and lists open flags newest first with display_title", async () => {
    const h = setup();
    await flag(h, { annotation_id: ANNO_ID, reason: "wrong_decl" });
    await flag(h, { reason: "other", comment: "second" });

    expect((await get(h.env, "/api/flags")).status).toBe(403);
    expect((await get(h.env, "/api/flags", { user: "u-human" })).status).toBe(403);

    const res = await get(h.env, "/api/flags", { user: "u-patroller" });
    expect(res.status).toBe(200);
    const { flags: list } = (await res.json()) as { flags: Array<Record<string, unknown>> };
    expect(list.length).toBe(2);
    expect(list[0]).toMatchObject({ slug: SLUG, display_title: "Test Article", reason: "other", comment: "second", status: "open" });
    expect(list[1]).toMatchObject({ annotation_id: ANNO_ID, reason: "wrong_decl" });
    // Resolved flags drop out of the default (open) view.
    await resolve(h, list[0].id as number, "dismissed");
    const after = (await (await get(h.env, "/api/flags", { user: "u-patroller" })).json()) as {
      flags: unknown[];
    };
    expect(after.flags.length).toBe(1);
  });

  it("/flags page: anonymous → login redirect; any session views; resolve buttons only for patroller/admin", async () => {
    const h = setup();
    await flag(h, { annotation_id: ANNO_ID, reason: "wrong_decl", comment: "visible-comment-marker" });

    const anon = await get(h.env, "/flags");
    expect(anon.status).toBe(302);
    expect(anon.headers.get("Location")).toBe("/login?returnTo=%2Fflags");

    const viewer = await get(h.env, "/flags", { user: "u-human" });
    expect(viewer.status).toBe(200);
    const viewerHtml = await viewer.text();
    expect(viewerHtml).toContain("visible-comment-marker");
    expect(viewerHtml).toContain("Test Article");
    expect(viewerHtml).not.toContain("wl-resolve"); // canResolve=false

    const patrol = await get(h.env, "/flags", { user: "u-patroller" });
    expect(patrol.status).toBe(200);
    expect(await patrol.text()).toContain("wl-resolve");
  });
});
