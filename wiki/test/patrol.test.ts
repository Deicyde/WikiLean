// P2a patrol polish: the /recent-changes ?kind= filter + unpatrolled
// highlighting, and the POST /api/revision/:id/patrol lifecycle (set-once
// CAS, duplicate semantics, kind gating). Authz cells live in authz.test.ts.

import { describe, it, expect } from "vitest";
import {
  setup,
  post,
  get,
  insertRevision,
  revisionById,
  blockNetwork,
  SLUG,
  type Harness,
} from "./helpers/harness.js";

blockNetwork();

function patrol(h: Harness, id: number, user = "u-patroller"): Promise<Response> {
  return post(h.env, `/api/revision/${id}/patrol`, {}, { user });
}

// The harness seed revision (#1) is kind='edit'. Add one of each other kind
// with distinguishable comments.
function seedKinds(h: Harness): { pipelineId: number; revertId: number } {
  const pipelineId = insertRevision(h.db, SLUG, {
    userId: "pipeline",
    kind: "pipeline",
    comment: "pipeline-only-comment",
  });
  const revertId = insertRevision(h.db, SLUG, {
    userId: "u-patroller",
    kind: "revert",
    comment: "revert-only-comment",
  });
  insertRevision(h.db, SLUG, { userId: null, kind: "seed", comment: "seed-only-comment" });
  return { pipelineId, revertId };
}

describe("GET /recent-changes?kind=", () => {
  it("filters to one revisions.kind and renders the filter chrome", async () => {
    const h = setup();
    seedKinds(h);
    const all = await (await get(h.env, "/recent-changes")).text();
    for (const c of ["pipeline-only-comment", "revert-only-comment", "seed-only-comment"]) {
      expect(all).toContain(c);
    }
    // Filter links for every kind, in the page chrome.
    for (const k of ["edit", "revert", "seed", "pipeline", "contribution"]) {
      expect(all).toContain(`href="/recent-changes?kind=${k}"`);
    }

    const pipelineOnly = await (await get(h.env, "/recent-changes?kind=pipeline")).text();
    expect(pipelineOnly).toContain("pipeline-only-comment");
    expect(pipelineOnly).not.toContain("revert-only-comment");
    expect(pipelineOnly).not.toContain("seed-only-comment");
    // The active filter is marked in the chrome.
    expect(pipelineOnly).toContain(`href="/recent-changes?kind=pipeline" class="active"`);

    const editOnly = await (await get(h.env, "/recent-changes?kind=edit")).text();
    expect(editOnly).not.toContain("pipeline-only-comment");
    expect(editOnly).toContain('wl-kind-edit"'); // the seed edit revision
  });

  it("ignores unknown kind values (reads as all)", async () => {
    const h = setup();
    seedKinds(h);
    const html = await (await get(h.env, "/recent-changes?kind=bogus")).text();
    expect(html).toContain("pipeline-only-comment");
    expect(html).toContain("revert-only-comment");
  });

  it("marks unpatrolled human edits — marker for everyone, button only for patroller/admin", async () => {
    const h = setup();
    seedKinds(h);
    const anon = await (await get(h.env, "/recent-changes")).text();
    expect(anon).toContain('class="wl-unpatrolled"'); // the seed edit, unpatrolled
    expect(anon).not.toContain('class="revert wl-patrol"'); // no button for anonymous…

    const plain = await (await get(h.env, "/recent-changes", { user: "u-human" })).text();
    expect(plain).not.toContain('class="revert wl-patrol"'); // …or plain users (UI#3)

    const patroller = await (await get(h.env, "/recent-changes", { user: "u-patroller" })).text();
    expect(patroller).toContain('class="revert wl-patrol" data-rev="1"');
    const admin = await (await get(h.env, "/recent-changes", { user: "u-admin" })).text();
    expect(admin).toContain('class="revert wl-patrol"');
  });

  it("non-edit revisions get no patrol affordance at all", async () => {
    const h = setup();
    const { pipelineId } = seedKinds(h);
    const html = await (await get(h.env, "/recent-changes?kind=pipeline", { user: "u-patroller" })).text();
    expect(html).not.toContain('class="wl-unpatrolled"'); // (bare substring would hit the shell CSS)
    expect(html).not.toContain(`data-rev="${pipelineId}"`);
  });
});

describe("POST /api/revision/:id/patrol", () => {
  it("sets patrolled_by/at once; the page then shows who/when on hover", async () => {
    const h = setup();
    const before = Date.now();
    const res = await patrol(h, 1);
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true });

    const rev = revisionById(h.db, 1)!;
    expect(rev.patrolled_by).toBe("u-patroller");
    expect(rev.patrolled_at).toBeGreaterThanOrEqual(before);

    const html = await (await get(h.env, "/recent-changes")).text();
    expect(html).not.toContain('class="wl-unpatrolled"'); // (bare substring would hit the shell CSS)
    // Hover title carries the patroller's name and the timestamp.
    expect(html).toMatch(/<span class="wl-patrolled" title="patrolled by Pat Roller · [^"]+">/);
  });

  it("repeat patrol → 200 {ok, duplicate}; the first mark is never overwritten", async () => {
    const h = setup();
    expect((await patrol(h, 1)).status).toBe(200);
    const first = revisionById(h.db, 1)!;

    const res = await patrol(h, 1, "u-admin"); // a second patroller re-marks
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true, duplicate: true });

    const after = revisionById(h.db, 1)!;
    expect(after.patrolled_by).toBe("u-patroller"); // still the first patroller
    expect(after.patrolled_at).toBe(first.patrolled_at);
  });

  it("admin may patrol too", async () => {
    const h = setup();
    const res = await patrol(h, 1, "u-admin");
    expect(res.status).toBe(200);
    expect(revisionById(h.db, 1)!.patrolled_by).toBe("u-admin");
  });

  it("only kind='edit' revisions are patrol targets (400 otherwise)", async () => {
    const h = setup();
    const { pipelineId, revertId } = seedKinds(h);
    for (const id of [pipelineId, revertId]) {
      const res = await patrol(h, id);
      expect(res.status).toBe(400);
      expect(revisionById(h.db, id)!.patrolled_by).toBeNull();
    }
  });

  it("404s an unknown revision id and 400s a malformed one", async () => {
    const h = setup();
    expect((await patrol(h, 999)).status).toBe(404);
    const res = await post(h.env, "/api/revision/abc/patrol", {}, { user: "u-patroller" });
    expect(res.status).toBe(400);
  });

  it("blocks cross-origin browser POSTs (403)", async () => {
    const h = setup();
    const res = await post(
      h.env,
      "/api/revision/1/patrol",
      {},
      { user: "u-patroller", origin: "https://evil.example" },
    );
    expect(res.status).toBe(403);
    expect(revisionById(h.db, 1)!.patrolled_by).toBeNull();
  });
});
