// Auth fail-closed and bearer comparison.
//
// The dev cookie stub trusts a client-set user id (and user ids are public on
// /u/:id), so it must run only when AUTH_MODE is exactly "dev": an unset or
// misspelled mode is no login, never "set a cookie to become any user". The
// PIPELINE_TOKEN bearer is compared in constant time.

import { afterEach, describe, expect, it, vi } from "vitest";
import { bearerMatches, devAuthEnabled } from "../src/auth.js";
import type { Env } from "../src/env.js";
import { blockNetwork, get, post, setup, PIPELINE_TOKEN, SLUG } from "./helpers/harness.js";

blockNetwork();

afterEach(() => {
  vi.restoreAllMocks();
});

const NOT_DEV_MODES: Array<string | undefined> = [undefined, "", "Dev", "DEV", "dev ", "development", "oaut", "none"];

function harnessWithMode(mode: string | undefined) {
  const h = setup();
  (h.env as { AUTH_MODE?: string }).AUTH_MODE = mode;
  return h;
}

async function me(env: Env, opts: Parameters<typeof get>[2] = {}): Promise<unknown> {
  const res = await get(env, "/api/auth/me", opts);
  expect(res.status).toBe(200);
  return ((await res.json()) as { user: unknown }).user;
}

describe("dev cookie stub", () => {
  it("is enabled only by AUTH_MODE === 'dev'", () => {
    expect(devAuthEnabled({ AUTH_MODE: "dev" })).toBe(true);
    for (const mode of [...NOT_DEV_MODES, "oauth"]) {
      expect(devAuthEnabled({ AUTH_MODE: mode as string })).toBe(false);
    }
  });

  it("resolves the cookie user in dev mode (control)", async () => {
    const h = harnessWithMode("dev");
    expect(await me(h.env, { user: "u-admin" })).toMatchObject({ id: "u-admin", role: "admin" });
  });

  it.each(NOT_DEV_MODES)("ignores the dev cookie when AUTH_MODE is %j", async (mode) => {
    const h = harnessWithMode(mode);
    expect(await me(h.env, { user: "u-admin" })).toBeNull();

    const dev = await get(h.env, "/api/auth/dev-login?name=Mallory");
    expect(dev.status).toBe(403);

    // A cookie-only write is anonymous, not the admin the cookie names.
    const watch = await post(h.env, `/api/watch/${SLUG}`, {}, { user: "u-admin" });
    expect(watch.status).toBe(401);
    const runs = await get(h.env, "/api/flags", { user: "u-admin" });
    expect(runs.status).toBe(403);
  });

  it.each(NOT_DEV_MODES)("does not send /login to the dev stub when AUTH_MODE is %j", async (mode) => {
    const h = harnessWithMode(mode);
    h.env.GITHUB_CLIENT_ID = "id";
    h.env.GITHUB_CLIENT_SECRET = "secret";
    const res = await get(h.env, "/login?returnTo=%2Fflags");
    expect(res.status).toBe(200);
    const html = await res.text();
    expect(html).not.toContain("dev-login");
    expect(html).not.toContain('data-provider="github"');
    expect(html).toContain("No login providers are configured.");
  });

  it("still offers configured providers in oauth mode", async () => {
    const h = harnessWithMode("oauth");
    h.env.GITHUB_CLIENT_ID = "id";
    h.env.GITHUB_CLIENT_SECRET = "secret";
    const html = await (await get(h.env, "/login")).text();
    expect(html).toContain('data-provider="github"');
  });

  it("keeps the bearer working whatever AUTH_MODE says", async () => {
    for (const mode of [undefined, "oauth", "typo"]) {
      const h = harnessWithMode(mode);
      expect(await me(h.env, { bearer: PIPELINE_TOKEN })).toMatchObject({ id: "pipeline", role: "bot" });
    }
  });
});

describe("PIPELINE_TOKEN bearer comparison", () => {
  it("accepts exactly the configured token", async () => {
    expect(await bearerMatches(PIPELINE_TOKEN, PIPELINE_TOKEN)).toBe(true);
    for (const wrong of ["", "x", PIPELINE_TOKEN.slice(0, -1), PIPELINE_TOKEN + "x", PIPELINE_TOKEN.toUpperCase(), ` ${PIPELINE_TOKEN}`]) {
      expect(await bearerMatches(wrong, PIPELINE_TOKEN)).toBe(false);
    }
  });

  it("uses the runtime's timingSafeEqual over equal-length digests when present", async () => {
    const subtle = crypto.subtle as SubtleCrypto & { timingSafeEqual?: unknown };
    const calls: Array<[number, number]> = [];
    const fake = vi.fn((a: ArrayBufferView, b: ArrayBufferView) => {
      calls.push([a.byteLength, b.byteLength]);
      return true;
    });
    const had = Object.prototype.hasOwnProperty.call(subtle, "timingSafeEqual");
    const previous = subtle.timingSafeEqual;
    Object.defineProperty(subtle, "timingSafeEqual", { value: fake, configurable: true, writable: true });
    try {
      expect(await bearerMatches("short", "a-much-longer-expected-token")).toBe(true);
    } finally {
      if (had) Object.defineProperty(subtle, "timingSafeEqual", { value: previous, configurable: true, writable: true });
      else delete (subtle as { timingSafeEqual?: unknown }).timingSafeEqual;
    }
    expect(calls).toEqual([[32, 32]]);
  });

  it("compares a request's bearer through timingSafeEqual, not ===", async () => {
    // The unit test above calls bearerMatches directly; this pins getUser's
    // use of it, so reverting the request path to a plain === fails here.
    const subtle = crypto.subtle as SubtleCrypto & { timingSafeEqual?: unknown };
    const calls: Array<[number, number]> = [];
    const fake = vi.fn((a: ArrayBufferView, b: ArrayBufferView) => {
      calls.push([a.byteLength, b.byteLength]);
      const x = new Uint8Array(a.buffer, a.byteOffset, a.byteLength);
      const y = new Uint8Array(b.buffer, b.byteOffset, b.byteLength);
      return x.length === y.length && x.every((v, i) => v === y[i]);
    });
    const had = Object.prototype.hasOwnProperty.call(subtle, "timingSafeEqual");
    const previous = subtle.timingSafeEqual;
    Object.defineProperty(subtle, "timingSafeEqual", { value: fake, configurable: true, writable: true });
    let right: unknown;
    let wrong: unknown;
    try {
      const h = setup();
      right = await me(h.env, { bearer: PIPELINE_TOKEN });
      expect(fake).toHaveBeenCalledTimes(1);
      wrong = await me(h.env, { bearer: PIPELINE_TOKEN + "0" });
    } finally {
      if (had) Object.defineProperty(subtle, "timingSafeEqual", { value: previous, configurable: true, writable: true });
      else delete (subtle as { timingSafeEqual?: unknown }).timingSafeEqual;
    }
    expect(right).toMatchObject({ id: "pipeline" });
    expect(wrong).toBeNull();
    expect(calls).toEqual([
      [32, 32],
      [32, 32],
    ]);
  });

  it("treats near-miss bearers as anonymous on the API", async () => {
    const h = setup();
    for (const wrong of [PIPELINE_TOKEN.slice(0, -1), PIPELINE_TOKEN + "0", PIPELINE_TOKEN.replace(/.$/, "X")]) {
      expect(await me(h.env, { bearer: wrong })).toBeNull();
      expect((await get(h.env, "/api/work", { bearer: wrong })).status).toBe(403);
    }
    expect((await get(h.env, "/api/work", { bearer: PIPELINE_TOKEN })).status).toBe(200);
  });
});
