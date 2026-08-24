import { afterEach, describe, expect, it, vi } from "vitest";
import { internalReturnPath, scriptSafeJson } from "../src/auth.js";
import { app } from "../src/index.js";
import type { Env } from "../src/env.js";
import { get, setup } from "./helpers/harness.js";

const HOSTILE_RETURN_PATHS = [
  "javascript:alert(1)",
  "https://evil.example/path",
  "//evil.example/path",
  "/\\evil.example/path",
  "/safe\nLocation: https://evil.example",
];

function oauthHarness() {
  const h = setup();
  h.env.AUTH_MODE = "oauth";
  return h;
}

function reviewHarness() {
  const h = setup();
  h.env.REVIEW_GITHUB_CLIENT_ID = "Iv1.test";
  h.env.REVIEW_GITHUB_CLIENT_SECRET = "secret";
  h.env.BETTER_AUTH_URL = "https://wikilean.example";
  return h;
}

function request(env: Env, path: string, headers?: Record<string, string>): Promise<Response> {
  return Promise.resolve(app.request(path, { headers }, env));
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("internalReturnPath", () => {
  it("accepts a single-leading-slash local path with query and fragment", () => {
    expect(internalReturnPath("/review?repo=leanprover-community/mathlib4&pr=42#Q1", "/fallback")).toBe(
      "/review?repo=leanprover-community/mathlib4&pr=42#Q1",
    );
  });

  it.each(HOSTILE_RETURN_PATHS)("rejects hostile return target %j", (value) => {
    expect(internalReturnPath(value, "/fallback")).toBe("/fallback");
  });
});

describe("main auth returnTo handling", () => {
  it("preserves a valid internal path through login and dev-login", async () => {
    const h = setup();
    const login = await get(h.env, "/login?returnTo=%2Fflags%3Fstatus%3Dopen");
    expect(login.status).toBe(302);
    expect(login.headers.get("Location")).toBe("/api/auth/dev-login?returnTo=%2Fflags%3Fstatus%3Dopen");

    const devLogin = await get(h.env, "/api/auth/dev-login?returnTo=%2Fflags%3Fstatus%3Dopen");
    expect(devLogin.status).toBe(302);
    expect(devLogin.headers.get("Location")).toBe("/flags?status=open");
  });

  it.each(HOSTILE_RETURN_PATHS)("falls back for hostile login and dev-login target %j", async (value) => {
    const h = setup();
    const encoded = encodeURIComponent(value);
    const login = await get(h.env, `/login?returnTo=${encoded}`);
    expect(login.headers.get("Location")).toBe("/api/auth/dev-login?returnTo=%2F");

    const devLogin = await get(h.env, `/api/auth/dev-login?returnTo=${encoded}`);
    expect(devLogin.headers.get("Location")).toBe("/");
  });

  it.each(HOSTILE_RETURN_PATHS)("falls back for hostile logout target %j", async (value) => {
    const h = setup();
    const response = await get(h.env, `/logout?returnTo=${encodeURIComponent(value)}`);
    expect(response.status).toBe(302);
    expect(response.headers.get("Location")).toBe("/");
  });

  it("serializes login and logout destinations without allowing an inline-script breakout", async () => {
    const h = oauthHarness();
    const returnTo = "/</script><script>alert(1)</script>\u2028\u2029";
    const encoded = encodeURIComponent(returnTo);

    const loginHtml = await (await request(h.env, `/login?returnTo=${encoded}`)).text();
    expect(loginHtml).toContain(`var ret=${scriptSafeJson(returnTo)};`);
    expect(loginHtml).not.toContain(`var ret=${JSON.stringify(returnTo)};`);

    const logoutHtml = await (await request(h.env, `/logout?returnTo=${encoded}`)).text();
    expect(logoutHtml).toContain(`location.href=${scriptSafeJson(returnTo)}`);
    expect(logoutHtml).not.toContain(`location.href=${JSON.stringify(returnTo)}`);
    expect(scriptSafeJson(returnTo)).toContain("\\u003c/script>");
    expect(scriptSafeJson(returnTo)).toContain("\\u2028");
    expect(scriptSafeJson(returnTo)).toContain("\\u2029");
    h.db.close();
  });
});

describe("review auth returnTo handling", () => {
  it("preserves a valid internal path through OAuth start and logout", async () => {
    const h = reviewHarness();
    const returnTo = "/review?repo=owner/repo&pr=42";
    const start = await request(h.env, `/review/auth/start?returnTo=${encodeURIComponent(returnTo)}`);
    expect(start.status).toBe(302);
    expect(decodeURIComponent(start.headers.get("Set-Cookie") || "")).toContain(`|${returnTo}`);

    const logout = await request(h.env, `/review/auth/logout?returnTo=${encodeURIComponent(returnTo)}`);
    expect(logout.status).toBe(302);
    expect(logout.headers.get("Location")).toBe(returnTo);
    h.db.close();
  });

  it.each(HOSTILE_RETURN_PATHS)("falls back for hostile OAuth start and logout target %j", async (value) => {
    const h = reviewHarness();
    const encoded = encodeURIComponent(value);
    const start = await request(h.env, `/review/auth/start?returnTo=${encoded}`);
    expect(decodeURIComponent(start.headers.get("Set-Cookie") || "")).toContain("|/review");

    const logout = await request(h.env, `/review/auth/logout?returnTo=${encoded}`);
    expect(logout.headers.get("Location")).toBe("/review");
    h.db.close();
  });

  it("preserves a valid return target from the OAuth state cookie", async () => {
    const h = reviewHarness();
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({ access_token: "token" }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ login: "reviewer" }), { status: 200 }));

    const returnTo = "/review?repo=owner/repo&pr=42";
    const cookie = `wl_review_oauth=${encodeURIComponent(`state|${returnTo}`)}`;
    const callback = await request(h.env, "/review/auth/callback?code=code&state=state", { Cookie: cookie });
    expect(callback.status).toBe(302);
    expect(callback.headers.get("Location")).toBe(returnTo);
    h.db.close();
  });

  it.each([
    "javascript:alert(1)",
    "https://evil.example/path",
    "//evil.example/path",
    "/\\evil.example/path",
    "/safe\nLocation: https://evil.example",
  ])("revalidates a hostile return target from the OAuth state cookie: %j", async (value) => {
    const h = reviewHarness();
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({ access_token: "token" }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ login: "reviewer" }), { status: 200 }));

    const cookie = `wl_review_oauth=${encodeURIComponent(`state|${value}`)}`;
    const callback = await request(h.env, "/review/auth/callback?code=code&state=state", { Cookie: cookie });
    expect(callback.status).toBe(302);
    expect(callback.headers.get("Location")).toBe("/review");
    h.db.close();
  });
});
