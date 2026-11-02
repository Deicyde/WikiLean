// POSTs to /api/auth/* with an empty body, against a real session row.
//
// A browser sends fetch(url, { method: "POST" }) with Content-Length: 0, and both
// workerd and @hono/node-server hand the Worker a non-null empty body for it. A
// Request built in a test as new Request(url, { method: "POST" }) has a null body
// and takes a different branch of the route, so every request here is built with
// an empty stream. e2e/sign-out.spec.ts runs the same path from the /logout page
// in Chromium.

import { makeSignature } from "better-auth/crypto";
import { describe, expect, it, vi } from "vitest";
import { MAX_AUTH_BODY_BYTES, makeAuth } from "../src/auth.js";
import { app } from "../src/index.js";
import { blockNetwork, setup, type Harness } from "./helpers/harness.js";

// better-auth turns its origin check off when it sees a test environment, and
// reads NODE_ENV as it loads. This file asserts that check, so it hides the
// signs Vitest sets, before the imports above run.
vi.hoisted(() => {
  process.env.NODE_ENV = "production";
  process.env.TEST = "false";
});

blockNetwork();

const SITE = "https://wikilean.example";
const SECRET = "0123456789abcdef0123456789abcdef";
const SESSION_COOKIE = "__Secure-better-auth.session_token";

function oauthHarness(): Harness {
  const h = setup();
  const bindings = h.env as unknown as Record<string, unknown>;
  bindings.AUTH_MODE = "oauth";
  bindings.BETTER_AUTH_SECRET = SECRET;
  bindings.BETTER_AUTH_URL = SITE;
  bindings.GITHUB_CLIENT_ID = "Iv1.test";
  bindings.GITHUB_CLIENT_SECRET = "test-secret";
  return h;
}

// A session row and the cookie that names it, returned as a Cookie header value.
let sessions = 0;
async function signIn(h: Harness): Promise<string> {
  sessions += 1;
  const token = `session-token-${sessions}`;
  const now = Math.floor(Date.now() / 1000);
  h.db
    .prepare(
      "INSERT INTO sessions (id, user_id, token, expires_at, ip_address, user_agent, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
    )
    .run(`session-${sessions}`, "u-human", token, now + 3600, "203.0.113.7", "vitest", now, now);
  return `${SESSION_COOKIE}=${encodeURIComponent(`${token}.${await makeSignature(token, SECRET)}`)}`;
}

function sessionRows(h: Harness): number {
  return (h.db.prepare("SELECT COUNT(*) AS n FROM sessions").get() as { n: number }).n;
}

async function currentUser(h: Harness, cookie: string): Promise<{ id: string } | null> {
  const res = await app.request(`${SITE}/api/auth/me`, { headers: { cookie } }, h.env);
  return ((await res.json()) as { user: { id: string } | null }).user;
}

function emptyStream(): ReadableStream<Uint8Array> {
  return new ReadableStream<Uint8Array>({ start: (controller) => controller.close() });
}

// The empty-bodied POST a browser's fetch delivers: same origin, Content-Length: 0.
function browserPost(h: Harness, path: string, headers: Record<string, string>): Promise<Response> {
  return Promise.resolve(
    app.request(
      SITE + path,
      {
        method: "POST",
        headers: { origin: SITE, "content-length": "0", ...headers },
        body: emptyStream(),
        duplex: "half",
      } as RequestInit,
      h.env,
    ),
  );
}

function postText(h: Harness, path: string, body: string, headers: Record<string, string>): Promise<Response> {
  return Promise.resolve(
    app.request(
      SITE + path,
      { method: "POST", headers: { origin: SITE, ...headers }, body: new TextEncoder().encode(body) },
      h.env,
    ),
  );
}

describe("sign-out with an empty body", () => {
  it("is what the /logout page sends", async () => {
    const page = await (await app.request(`${SITE}/logout`, {}, oauthHarness().env)).text();
    expect(page).toContain(`fetch("/api/auth/sign-out",{method:"POST"})`);
  });

  it("deletes the session row and ends the session", async () => {
    const h = oauthHarness();
    const cookie = await signIn(h);
    expect(await currentUser(h, cookie)).toMatchObject({ id: "u-human" });
    const before = sessionRows(h);

    const res = await browserPost(h, "/api/auth/sign-out", { cookie });

    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ success: true });
    expect(sessionRows(h)).toBe(before - 1);
    // The old cookie is replayed: this passes only if the session itself is gone.
    expect(await currentUser(h, cookie)).toBeNull();
    const cleared = res.headers.getSetCookie().find((c) => c.startsWith(`${SESSION_COOKIE}=`));
    expect(cleared).toMatch(/Max-Age=0/i);
  });

  it("does the same with a declared JSON type and no body", async () => {
    const h = oauthHarness();
    const cookie = await signIn(h);
    const res = await browserPost(h, "/api/auth/sign-out", { cookie, "content-type": "application/json" });
    expect(res.status).toBe(200);
    expect(sessionRows(h)).toBe(0);
  });

  it.each([
    ["a foreign origin", { origin: "https://evil.example" }, "INVALID_ORIGIN"],
    ["a null origin", { origin: "null" }, "MISSING_OR_NULL_ORIGIN"],
  ])("still refuses %s with the session cookie", async (_name, headers, code) => {
    const h = oauthHarness();
    const cookie = await signIn(h);
    const res = await browserPost(h, "/api/auth/sign-out", { cookie, ...headers });
    expect(res.status).toBe(403);
    expect(await res.json()).toMatchObject({ code });
    expect(sessionRows(h)).toBe(1);
    expect(await currentUser(h, cookie)).toMatchObject({ id: "u-human" });
  });

  it("still refuses a foreign origin on the routes that revoke every session", async () => {
    const h = oauthHarness();
    const cookie = await signIn(h);
    for (const path of ["/api/auth/revoke-sessions", "/api/auth/revoke-other-sessions"]) {
      const res = await browserPost(h, path, { cookie, origin: "https://evil.example" });
      expect(res.status).toBe(403);
    }
    expect(sessionRows(h)).toBe(1);
  });
});

describe("every POST under /api/auth/* with an empty body", () => {
  const h = oauthHarness();
  const posts = Object.values(makeAuth(h.env).api as unknown as Record<string, { path?: string; options?: { method?: string | string[] } }>)
    .filter((endpoint) => endpoint.path && [endpoint.options?.method].flat().includes("POST"))
    .map((endpoint) => endpoint.path!.replace(":id", "github"));

  it("finds the routes it is checking", () => {
    expect(posts).toContain("/sign-out");
    expect(posts).toContain("/sign-in/social");
    expect(posts.length).toBeGreaterThan(10);
  });

  it.each(posts)("%s is judged on its own terms, not refused for a type the Worker stamped", async (path) => {
    const res = await browserPost(h, `/api/auth${path}`, { cookie: await signIn(h) });
    expect(res.status).not.toBe(415);
    expect(res.status).toBeLessThan(500);
  });
});

describe("the body cap and content types around the empty case", () => {
  it("counts a streamed body even when it declares Content-Length: 0", async () => {
    const h = oauthHarness();
    const cookie = await signIn(h);
    const over = new TextEncoder().encode("x".repeat(MAX_AUTH_BODY_BYTES + 1));
    const res = await app.request(
      `${SITE}/api/auth/sign-out`,
      {
        method: "POST",
        headers: { origin: SITE, cookie, "content-length": "0" },
        body: new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(over);
            controller.close();
          },
        }),
        duplex: "half",
      } as RequestInit,
      h.env,
    );
    expect(res.status).toBe(413);
    expect(sessionRows(h)).toBe(1);
  });

  it("refuses a declared body past the cap", async () => {
    const h = oauthHarness();
    const cookie = await signIn(h);
    const res = await browserPost(h, "/api/auth/sign-out", { cookie, "content-length": String(MAX_AUTH_BODY_BYTES + 1) });
    expect(res.status).toBe(413);
    expect(sessionRows(h)).toBe(1);
  });

  it("forwards a non-empty JSON body under its own type", async () => {
    const h = oauthHarness();
    const cookie = await signIn(h);
    const res = await postText(h, "/api/auth/sign-out", "{}", { cookie, "content-type": "application/json" });
    expect(res.status).toBe(200);
    expect(sessionRows(h)).toBe(0);
  });

  it("does not give a non-empty body of the wrong type a pass", async () => {
    const h = oauthHarness();
    const cookie = await signIn(h);
    const res = await postText(h, "/api/auth/sign-out", "{}", { cookie, "content-type": "text/plain" });
    expect(res.status).toBe(415);
    expect(((await res.json()) as { message: string }).message).toContain('"text/plain"');
    expect(sessionRows(h)).toBe(1);
  });

  it("does not give a non-empty body with no type a pass", async () => {
    const h = oauthHarness();
    const cookie = await signIn(h);
    const res = await postText(h, "/api/auth/sign-out", "{}", { cookie });
    expect(res.status).toBe(415);
    expect(sessionRows(h)).toBe(1);
  });
});
