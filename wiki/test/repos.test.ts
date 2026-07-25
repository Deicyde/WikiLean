// Per-user GitHub repo registration (src/repos.ts): /api/repos CRUD, the
// server-side GitHub listing (stubbed fetch + per-user KV cache), the
// validation rules (owner/repo/lib regexes, per-user cap), and the PINNED
// unauthenticated GET /api/repos/enabled nightly contract (shape, enabled-only,
// cross-user dedup, stable order).

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { setup, post, get, PIPELINE_TOKEN, botCreate, type Harness, type ReqOpts } from "./helpers/harness.js";
import { app } from "../src/index.js";
import { defaultLibName, REPO_CAP } from "../src/repos.js";
import type { Env } from "../src/env.js";

// ---- GitHub API stub (this file does NOT use blockNetwork — it installs its
// own fetch that serves the two GitHub endpoints repos.ts calls and fails
// loudly on anything else; ghCalls lets the cache test count upstream hits).
const GH_REPOS = [
  {
    name: "my-lean-lib",
    owner: { login: "octocat" },
    language: "Lean",
    description: "a Lean library",
    updated_at: "2026-07-01T00:00:00Z",
  },
  { name: "dotfiles", owner: { login: "octocat" }, language: "Shell", description: null, updated_at: "2026-06-01T00:00:00Z" },
];
const ghCalls: string[] = [];
const realFetch = globalThis.fetch;
beforeAll(() => {
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = String(input instanceof Request ? input.url : input);
    ghCalls.push(url);
    if (url.startsWith("https://api.github.com/user/777")) {
      return new Response(JSON.stringify({ login: "octocat" }), { status: 200 });
    }
    if (url.startsWith("https://api.github.com/users/octocat/repos")) {
      return new Response(JSON.stringify(GH_REPOS), { status: 200 });
    }
    throw new Error(`unexpected network fetch in test: ${url}`);
  }) as typeof fetch;
});
afterAll(() => {
  globalThis.fetch = realFetch;
});

// Link a GitHub account row (better-auth accounts table) for a harness user.
function linkGithub(h: Harness, userId: string, accountId = "777"): void {
  const nowSec = Math.floor(Date.now() / 1000);
  h.db
    .prepare(
      "INSERT INTO accounts (id, user_id, account_id, provider_id, created_at, updated_at) VALUES (?,?,?,?,?,?)",
    )
    .run(`acc-${userId}`, userId, accountId, "github", nowSec, nowSec);
}

function repoRows(h: Harness, userId?: string): Array<Record<string, unknown>> {
  return (
    userId
      ? h.db.prepare("SELECT * FROM user_repos WHERE user_id = ? ORDER BY owner, repo").all(userId)
      : h.db.prepare("SELECT * FROM user_repos ORDER BY user_id, owner, repo").all()
  ) as Array<Record<string, unknown>>;
}

// harness has post/put/get but no DELETE-with-body; same Origin conventions.
function del(env: Env, path: string, body: unknown, opts: ReqOpts = {}): Promise<Response> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (opts.user) headers["Cookie"] = `wl_dev_user=${opts.user}`;
  if (opts.bearer) headers["Authorization"] = `Bearer ${opts.bearer}`;
  if (opts.origin !== null) headers["Origin"] = opts.origin ?? "http://localhost";
  return Promise.resolve(app.request(path, { method: "DELETE", headers, body: JSON.stringify(body) }, env));
}

const ITEM = { owner: "octocat", repo: "my-lean-lib" };

describe("auth gates", () => {
  it("GET/POST/DELETE /api/repos: 401 anonymous, 401 blocked (treated as anonymous), 403 bot bearer", async () => {
    const h = setup();
    expect((await get(h.env, "/api/repos")).status).toBe(401);
    expect((await get(h.env, "/api/repos", { user: "u-blocked" })).status).toBe(401);
    expect((await get(h.env, "/api/repos", { bearer: PIPELINE_TOKEN })).status).toBe(403);
    expect((await post(h.env, "/api/repos", { repos: [ITEM] })).status).toBe(401);
    expect((await post(h.env, "/api/repos", { repos: [ITEM] }, { user: "u-blocked" })).status).toBe(401);
    expect(
      (await post(h.env, "/api/repos", { repos: [ITEM] }, { bearer: PIPELINE_TOKEN, origin: null })).status,
    ).toBe(403);
    expect((await del(h.env, "/api/repos", { repos: [ITEM] })).status).toBe(401);
    expect((await del(h.env, "/api/repos", { repos: [ITEM] }, { bearer: PIPELINE_TOKEN, origin: null })).status).toBe(403);
    expect((await get(h.env, "/api/repos/github")).status).toBe(401);
    expect(repoRows(h)).toHaveLength(0);
  });

  it("POST/DELETE reject cross-origin browser requests (403)", async () => {
    const h = setup();
    const p = await post(h.env, "/api/repos", { repos: [ITEM] }, { user: "u-human", origin: "http://evil.example" });
    expect(p.status).toBe(403);
    const d = await del(h.env, "/api/repos", { repos: [ITEM] }, { user: "u-human", origin: "http://evil.example" });
    expect(d.status).toBe(403);
  });

  it("POST is rate limited via REPO_LIMITER keyed by user id (429)", async () => {
    const h = setup();
    const keys: string[] = [];
    (h.env as { REPO_LIMITER?: unknown }).REPO_LIMITER = {
      limit: async ({ key }: { key: string }) => {
        keys.push(key);
        return { success: false };
      },
    };
    const res = await post(h.env, "/api/repos", { repos: [ITEM] }, { user: "u-human" });
    expect(res.status).toBe(429);
    expect(keys).toEqual(["repos:u-human"]);
    expect(repoRows(h)).toHaveLength(0);
  });

  it("GET /repos redirects anonymous readers to login; renders for a session", async () => {
    const h = setup();
    const anon = await get(h.env, "/repos");
    expect(anon.status).toBe(302);
    expect(anon.headers.get("Location")).toBe("/login?returnTo=%2Frepos");
    const page = await get(h.env, "/repos", { user: "u-human" });
    expect(page.status).toBe(200);
    const html = await page.text();
    expect(html).toContain("Repo sources");
    // fixed global sources are always on
    expect(html).toContain("leanprover-community/mathlib4");
    expect(html).toContain("FormalConjectures");
    expect(html).toContain("TauCeti");
  });

  it("'repos' is RESERVED — an article cannot claim the slug", async () => {
    const h = setup();
    const res = await botCreate(h.env, "repos", { wikipedia_title: "Repos", annotations: [] });
    expect(res.status).toBe(400);
    expect(((await res.json()) as { error: string }).error).toMatch(/reserved/);
  });
});

describe("defaultLibName", () => {
  it("CamelCases the repo name with invalid chars stripped", () => {
    expect(defaultLibName("my-lean-lib")).toBe("MyLeanLib");
    expect(defaultLibName("lean4_tauceti-2")).toBe("Lean4Tauceti2");
    expect(defaultLibName("dotfiles")).toBe("Dotfiles");
    expect(defaultLibName("123abc")).toBe("Abc"); // leading digits stripped
    expect(defaultLibName("1234")).toBeNull(); // nothing lib-shaped survives
    expect(defaultLibName("a".repeat(200))!.length).toBe(64); // clamped to the regex cap
  });
});

describe("POST /api/repos validation", () => {
  it("rejects bad owner/repo/lib shapes and oversized calls (400)", async () => {
    const h = setup();
    const cases: Array<Record<string, unknown>> = [
      { owner: ".dot", repo: "ok" }, // owner must not start with '.'
      { owner: "bad/name", repo: "ok" },
      { owner: "a".repeat(101), repo: "ok" },
      { owner: "octocat", repo: "bad name" },
      { owner: "octocat", repo: "" },
      { owner: "octocat", repo: "ok", lib: "lowercase" },
      { owner: "octocat", repo: "ok", lib: "Bad-Char" },
      { owner: "octocat", repo: "1234" }, // underivable default lib
    ];
    for (const bad of cases) {
      const res = await post(h.env, "/api/repos", { repos: [bad] }, { user: "u-human" });
      expect(res.status, JSON.stringify(bad)).toBe(400);
    }
    expect((await post(h.env, "/api/repos", { nope: 1 }, { user: "u-human" })).status).toBe(400);
    expect((await post(h.env, "/api/repos", { repos: [] }, { user: "u-human" })).status).toBe(400);
    const tooMany = Array.from({ length: 51 }, (_, i) => ({ owner: "octocat", repo: `r${i}` }));
    expect((await post(h.env, "/api/repos", { repos: tooMany }, { user: "u-human" })).status).toBe(400);
    expect(repoRows(h)).toHaveLength(0); // nothing was applied
  });

  it("repo MAY start with '.' (only owner is restricted)", async () => {
    const h = setup();
    const res = await post(
      h.env,
      "/api/repos",
      { repos: [{ owner: "octocat", repo: ".github", lib: "DotGithub" }] },
      { user: "u-human" },
    );
    expect(res.status).toBe(200);
    expect(repoRows(h, "u-human")).toHaveLength(1);
  });

  it("enforces the per-user cap with 422 and applies nothing beyond it", async () => {
    const h = setup();
    const twenty = Array.from({ length: REPO_CAP }, (_, i) => ({
      owner: "octocat",
      repo: `r${String(i).padStart(2, "0")}`,
    }));
    const okRes = await post(h.env, "/api/repos", { repos: twenty }, { user: "u-human" });
    expect(okRes.status).toBe(200);
    expect(repoRows(h, "u-human")).toHaveLength(REPO_CAP);

    // one more fresh repo → 422, nothing written
    const over = await post(h.env, "/api/repos", { repos: [{ owner: "octocat", repo: "extra" }] }, { user: "u-human" });
    expect(over.status).toBe(422);
    // mixing existing rows with a fresh one still trips the cap atomically
    const mixed = await post(
      h.env,
      "/api/repos",
      { repos: [twenty[0], { owner: "octocat", repo: "extra" }] },
      { user: "u-human" },
    );
    expect(mixed.status).toBe(422);
    expect(repoRows(h, "u-human")).toHaveLength(REPO_CAP);
    expect(repoRows(h, "u-human").some((r) => r.repo === "extra")).toBe(false);

    // re-upserting existing rows at the cap is fine (0 fresh)
    const idem = await post(h.env, "/api/repos", { repos: [twenty[0]] }, { user: "u-human" });
    expect(idem.status).toBe(200);

    // the cap is per-user — another user still has room
    const other = await post(h.env, "/api/repos", { repos: [ITEM] }, { user: "u-patroller" });
    expect(other.status).toBe(200);
  });
});

describe("upsert + delete", () => {
  it("upserts idempotently, defaults lib from the repo name, toggles enabled", async () => {
    const h = setup();
    const first = await post(h.env, "/api/repos", { repos: [ITEM] }, { user: "u-human" });
    expect(first.status).toBe(200);
    const j = (await first.json()) as { repos: Array<Record<string, unknown>> };
    expect(j.repos).toEqual([{ owner: "octocat", repo: "my-lean-lib", lib: "MyLeanLib", enabled: true }]);

    // same item again → still one row
    await post(h.env, "/api/repos", { repos: [ITEM] }, { user: "u-human" });
    expect(repoRows(h, "u-human")).toHaveLength(1);

    // toggle enabled off via upsert
    await post(h.env, "/api/repos", { repos: [{ ...ITEM, enabled: false }] }, { user: "u-human" });
    expect(repoRows(h, "u-human")[0].enabled).toBe(0);

    // an explicit lib overwrites; a later lib-less upsert must NOT clobber it
    await post(h.env, "/api/repos", { repos: [{ ...ITEM, lib: "CustomLib" }] }, { user: "u-human" });
    expect(repoRows(h, "u-human")[0].lib).toBe("CustomLib");
    await post(h.env, "/api/repos", { repos: [{ ...ITEM, enabled: true }] }, { user: "u-human" });
    const row = repoRows(h, "u-human")[0];
    expect(row.lib).toBe("CustomLib");
    expect(row.enabled).toBe(1);
  });

  it("GET /api/repos returns only the caller's rows; DELETE bulk-removes them", async () => {
    const h = setup();
    await post(
      h.env,
      "/api/repos",
      { repos: [ITEM, { owner: "octocat", repo: "dotfiles" }] },
      { user: "u-human" },
    );
    await post(h.env, "/api/repos", { repos: [{ owner: "other", repo: "theirs" }] }, { user: "u-patroller" });

    const mine = (await (await get(h.env, "/api/repos", { user: "u-human" })).json()) as {
      repos: Array<{ owner: string; repo: string }>;
    };
    expect(mine.repos.map((r) => r.repo).sort()).toEqual(["dotfiles", "my-lean-lib"]);

    const d = await del(h.env, "/api/repos", { repos: [{ owner: "octocat", repo: "dotfiles" }] }, { user: "u-human" });
    expect(d.status).toBe(200);
    expect(((await d.json()) as { deleted: number }).deleted).toBe(1);
    expect(repoRows(h, "u-human")).toHaveLength(1);
    expect(repoRows(h, "u-patroller")).toHaveLength(1); // untouched
  });
});

describe("GET /api/repos/enabled (PINNED nightly contract)", () => {
  it("is unauthenticated, enabled-only, distinct across users, capped shape", async () => {
    const h = setup();
    // same repo enabled by two users with different libs → once, MIN(lib) wins
    await post(h.env, "/api/repos", { repos: [{ ...ITEM, lib: "ZebraLib" }] }, { user: "u-human" });
    await post(h.env, "/api/repos", { repos: [{ ...ITEM, lib: "AlphaLib" }] }, { user: "u-patroller" });
    // a disabled row must not leak
    await post(
      h.env,
      "/api/repos",
      { repos: [{ owner: "octocat", repo: "dotfiles", enabled: false }] },
      { user: "u-human" },
    );
    await post(h.env, "/api/repos", { repos: [{ owner: "aaa", repo: "zzz" }] }, { user: "u-human" });

    const res = await get(h.env, "/api/repos/enabled"); // NO auth
    expect(res.status).toBe(200);
    const body = (await res.json()) as Record<string, unknown>;
    expect(Object.keys(body)).toEqual(["repos"]); // exact contract shape
    expect(body.repos).toEqual([
      { owner: "aaa", repo: "zzz", lib: "Zzz" },
      { owner: "octocat", repo: "my-lean-lib", lib: "AlphaLib" },
    ]);
    for (const r of body.repos as Array<{ owner: string; repo: string; lib: string }>) {
      expect(r.owner).toMatch(/^[A-Za-z0-9_.-]{1,100}$/);
      expect(r.owner.startsWith(".")).toBe(false);
      expect(r.repo).toMatch(/^[A-Za-z0-9_.-]{1,100}$/);
      expect(r.lib).toMatch(/^[A-Z][A-Za-z0-9_]{0,63}$/);
    }
  });
});

describe("GET /api/repos/github", () => {
  it("resolves the linked account id → login → public repos, and caches per user", async () => {
    const h = setup();
    linkGithub(h, "u-human");
    (h.env as { GITHUB_API_TOKEN?: string }).GITHUB_API_TOKEN = "test-token";

    const before = ghCalls.length;
    const res = await get(h.env, "/api/repos/github", { user: "u-human" });
    expect(res.status).toBe(200);
    const j = (await res.json()) as { ok: boolean; login: string; repos: Array<Record<string, unknown>> };
    expect(j.ok).toBe(true);
    expect(j.login).toBe("octocat");
    expect(j.repos).toEqual([
      {
        name: "my-lean-lib",
        owner: "octocat",
        language: "Lean",
        description: "a Lean library",
        updated_at: "2026-07-01T00:00:00Z",
      },
      { name: "dotfiles", owner: "octocat", language: "Shell", description: null, updated_at: "2026-06-01T00:00:00Z" },
    ]);
    expect(ghCalls.length).toBe(before + 2); // /user/<id> + /users/<login>/repos

    // second call: served from the per-user KV cache, zero upstream hits
    const again = await get(h.env, "/api/repos/github", { user: "u-human" });
    expect(again.status).toBe(200);
    expect(await again.json()).toEqual(j);
    expect(ghCalls.length).toBe(before + 2);
    expect(h.renderCache.store.has("repos:gh:u-human")).toBe(true);
  });

  it("404 when no GitHub account is linked; 503 without a server token", async () => {
    const h = setup();
    (h.env as { GITHUB_API_TOKEN?: string }).GITHUB_API_TOKEN = "test-token";
    expect((await get(h.env, "/api/repos/github", { user: "u-patroller" })).status).toBe(404);

    const h2 = setup(); // no GITHUB_API_TOKEN in the harness env
    linkGithub(h2, "u-human");
    expect((await get(h2.env, "/api/repos/github", { user: "u-human" })).status).toBe(503);
  });
});
