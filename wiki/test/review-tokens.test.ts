// Reviewer GitHub tokens (/review posting). They are live credentials, so they
// go to the dedicated REVIEW_TOKENS namespace when it is bound and fall back to
// RENDER_CACHE when it is not. Binding it signs nobody out: a token stashed in
// RENDER_CACHE earlier is still read, and is moved out on its next write.

import { afterEach, describe, expect, it, vi } from "vitest";
import { app } from "../src/index.js";
import { reviewTokenStore } from "../src/review.js";
import type { Env } from "../src/env.js";
import { makeKV, type KVShim } from "./helpers/d1shim.js";
import { setup } from "./helpers/harness.js";

afterEach(() => {
  vi.restoreAllMocks();
});

function harness(withDedicated: boolean): { env: Env; renderCache: KVShim; tokens: KVShim | null } {
  const h = setup();
  h.env.REVIEW_GITHUB_CLIENT_ID = "Iv1.test";
  h.env.REVIEW_GITHUB_CLIENT_SECRET = "secret";
  h.env.BETTER_AUTH_URL = "https://wikilean.example";
  const tokens = withDedicated ? makeKV() : null;
  if (tokens) h.env.REVIEW_TOKENS = tokens as unknown as KVNamespace;
  return { env: h.env, renderCache: h.renderCache, tokens };
}

function request(env: Env, path: string, cookie?: string): Promise<Response> {
  return Promise.resolve(app.request(path, { headers: cookie ? { Cookie: cookie } : {} }, env));
}

async function connect(env: Env): Promise<string> {
  vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(new Response(JSON.stringify({ access_token: "gho_user_token" }), { status: 200 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ login: "reviewer" }), { status: 200 }));
  const res = await request(env, "/review/auth/callback?code=code&state=state", "wl_review_oauth=state%7C%2Freview");
  expect(res.status).toBe(302);
  const id = /wl_review_gh=([^;]+)/.exec(res.headers.get("Set-Cookie") || "")?.[1];
  expect(id).toBeTruthy();
  return id as string;
}

async function connected(env: Env, id: string): Promise<{ connected: boolean; login: string | null }> {
  const res = await request(env, "/api/review/connected", `wl_review_gh=${id}`);
  expect(res.status).toBe(200);
  return (await res.json()) as { connected: boolean; login: string | null };
}

const reviewKeys = (kv: KVShim) => [...kv.store.keys()].filter((k) => k.startsWith("reviewtok:"));

describe("review token store", () => {
  it("selects REVIEW_TOKENS when bound, else RENDER_CACHE", () => {
    const renderCache = makeKV();
    const tokens = makeKV();
    expect(reviewTokenStore({ RENDER_CACHE: renderCache as unknown as KVNamespace })).toBe(renderCache);
    expect(
      reviewTokenStore({
        RENDER_CACHE: renderCache as unknown as KVNamespace,
        REVIEW_TOKENS: tokens as unknown as KVNamespace,
      }),
    ).toBe(tokens);
  });

  it("keeps using RENDER_CACHE when no dedicated namespace is bound", async () => {
    const h = harness(false);
    const id = await connect(h.env);
    expect(reviewKeys(h.renderCache)).toEqual([`reviewtok:${id}`]);
    expect(await connected(h.env, id)).toMatchObject({ connected: true, login: "reviewer" });
    await request(h.env, "/review/auth/logout", `wl_review_gh=${id}`);
    expect(reviewKeys(h.renderCache)).toEqual([]);
  });

  it("stores, reads and deletes tokens only in REVIEW_TOKENS when it is bound", async () => {
    const h = harness(true);
    const id = await connect(h.env);
    expect(reviewKeys(h.tokens!)).toEqual([`reviewtok:${id}`]);
    expect(JSON.parse(h.tokens!.store.get(`reviewtok:${id}`)!)).toMatchObject({
      token: "gho_user_token",
      login: "reviewer",
    });
    expect(reviewKeys(h.renderCache)).toEqual([]);
    expect(await connected(h.env, id)).toMatchObject({ connected: true, login: "reviewer" });
    await request(h.env, "/review/auth/logout", `wl_review_gh=${id}`);
    expect(reviewKeys(h.tokens!)).toEqual([]);
  });

  it("still reads a token stashed in RENDER_CACHE before the binding, and logout clears both", async () => {
    const h = harness(true);
    h.renderCache.store.set("reviewtok:legacy", JSON.stringify({ token: "gho_old", login: "reviewer" }));
    expect(await connected(h.env, "legacy")).toMatchObject({ connected: true, login: "reviewer" });
    await request(h.env, "/review/auth/logout", "wl_review_gh=legacy");
    expect(reviewKeys(h.renderCache)).toEqual([]);
    expect(reviewKeys(h.tokens!)).toEqual([]);
  });

  it("moves a legacy token into REVIEW_TOKENS when it is refreshed", async () => {
    const h = harness(true);
    h.renderCache.store.set(
      "reviewtok:legacy",
      JSON.stringify({ token: "ghu_old", login: "reviewer", refresh_token: "ghr_old", expires_at: Date.now() - 1 }),
    );
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify({ access_token: "ghu_new", refresh_token: "ghr_new", expires_in: 28800 }), {
        status: 200,
      }),
    );
    expect(await connected(h.env, "legacy")).toMatchObject({ connected: true, login: "reviewer" });
    expect(reviewKeys(h.renderCache)).toEqual([]);
    expect(JSON.parse(h.tokens!.store.get("reviewtok:legacy")!)).toMatchObject({
      token: "ghu_new",
      refresh_token: "ghr_new",
      login: "reviewer",
    });
  });
});
