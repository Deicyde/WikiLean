// Reviewer GitHub tokens (/review posting). They are live credentials, so they
// go to the dedicated REVIEW_TOKENS namespace when it is bound and fall back to
// RENDER_CACHE when it is not. Binding it signs nobody out: a token stashed in
// RENDER_CACHE earlier is still read, and is moved out on its next write.
//
// A namespace is not access isolation (Cloudflare grants KV access per
// account), so with REVIEW_TOKEN_KEY set the record is sealed: AES-GCM
// ciphertext bound to the cookie id, under a KV key that is a digest of the
// cookie id. A KV reader then holds neither a GitHub token nor a live cookie.

import { afterEach, describe, expect, it, vi } from "vitest";
import { app } from "../src/index.js";
import { reviewTokenStore } from "../src/review.js";
import type { Env } from "../src/env.js";
import { makeKV, type KVShim } from "./helpers/d1shim.js";
import { setup } from "./helpers/harness.js";

afterEach(() => {
  vi.restoreAllMocks();
});

const SEAL_KEY = "test-review-token-key-0123456789abcdef";

function harness(
  withDedicated: boolean,
  sealKey?: string,
): { env: Env; renderCache: KVShim; tokens: KVShim | null } {
  const h = setup();
  h.env.REVIEW_GITHUB_CLIENT_ID = "Iv1.test";
  h.env.REVIEW_GITHUB_CLIENT_SECRET = "secret";
  h.env.BETTER_AUTH_URL = "https://wikilean.example";
  if (sealKey !== undefined) h.env.REVIEW_TOKEN_KEY = sealKey;
  const tokens = withDedicated ? makeKV() : null;
  if (tokens) h.env.REVIEW_TOKENS = tokens as unknown as KVNamespace;
  return { env: h.env, renderCache: h.renderCache, tokens };
}

async function sealedKeyFor(id: string): Promise<string> {
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(id)));
  return `reviewtok:s1:${[...digest].map((b) => b.toString(16).padStart(2, "0")).join("")}`;
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

  it("leaves only ciphertext, under a digest of the cookie, when REVIEW_TOKEN_KEY is set", async () => {
    for (const dedicated of [true, false]) {
      const h = harness(dedicated, SEAL_KEY);
      const store = h.tokens ?? h.renderCache;
      const id = await connect(h.env);
      const key = await sealedKeyFor(id);
      expect(reviewKeys(store)).toEqual([key]);
      expect(key).not.toContain(id);
      const value = store.store.get(key)!;
      expect(value.startsWith("rt1.")).toBe(true);
      for (const secret of ["gho_user_token", "reviewer", id]) expect(value).not.toContain(secret);
      if (dedicated) expect(reviewKeys(h.renderCache)).toEqual([]);
      expect(await connected(h.env, id)).toMatchObject({ connected: true, login: "reviewer" });
      await request(h.env, "/review/auth/logout", `wl_review_gh=${id}`);
      expect(reviewKeys(store)).toEqual([]);
    }
  });

  it("opens a sealed token only with its own key and its own cookie", async () => {
    const h = harness(true, SEAL_KEY);
    const id = await connect(h.env);
    const sealed = h.tokens!.store.get(await sealedKeyFor(id))!;
    // Copied to another session's key, the ciphertext does not open (the
    // cookie id is the AES-GCM additional data).
    h.tokens!.store.set(await sealedKeyFor("other-session"), sealed);
    expect(await connected(h.env, "other-session")).toMatchObject({ connected: false });
    // Tampered, or under a rotated key: signed out, never a crash.
    const flipped = sealed.slice(0, -2) + (sealed.at(-2) === "A" ? "B" : "A") + sealed.slice(-1);
    expect(flipped).not.toBe(sealed);
    h.tokens!.store.set(await sealedKeyFor("tampered"), flipped);
    expect(await connected(h.env, "tampered")).toMatchObject({ connected: false });
    h.env.REVIEW_TOKEN_KEY = SEAL_KEY.replace("0123", "9999");
    expect(await connected(h.env, id)).toMatchObject({ connected: false });
    h.env.REVIEW_TOKEN_KEY = SEAL_KEY;
    expect(await connected(h.env, id)).toMatchObject({ connected: true, login: "reviewer" });
  });

  it("seals a plaintext token on first read once the key is set, signing nobody out", async () => {
    const h = harness(true, SEAL_KEY);
    h.renderCache.store.set("reviewtok:legacy", JSON.stringify({ token: "gho_old", login: "reviewer" }));
    h.tokens!.store.set("reviewtok:plain", JSON.stringify({ token: "gho_plain", login: "other" }));
    expect(await connected(h.env, "legacy")).toMatchObject({ connected: true, login: "reviewer" });
    expect(await connected(h.env, "plain")).toMatchObject({ connected: true, login: "other" });
    expect(reviewKeys(h.renderCache)).toEqual([]);
    expect(reviewKeys(h.tokens!).sort()).toEqual([await sealedKeyFor("legacy"), await sealedKeyFor("plain")].sort());
    for (const value of h.tokens!.store.values()) expect(value).not.toContain("gho_");
    expect(await connected(h.env, "legacy")).toMatchObject({ connected: true, login: "reviewer" });
  });

  it("refuses a REVIEW_TOKEN_KEY shorter than 32 characters instead of storing a token", async () => {
    const h = harness(true, "too-short");
    vi.spyOn(console, "log").mockImplementation(() => {});
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({ access_token: "gho_user_token" }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ login: "reviewer" }), { status: 200 }));
    const res = await request(h.env, "/review/auth/callback?code=code&state=state", "wl_review_oauth=state%7C%2Freview");
    expect(res.status).toBe(500);
    expect(reviewKeys(h.tokens!)).toEqual([]);
    expect(reviewKeys(h.renderCache)).toEqual([]);
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
