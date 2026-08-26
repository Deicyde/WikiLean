// Covers Better Auth's Drizzle adapter against the real D1 shim and migrations.
// The shared fixtures default to dev auth, so production OAuth adapter queries
// need an explicit fixture to remain part of the hermetic CI suite.

import { parseSetCookieHeader } from "better-auth/cookies";
import { makeSignature } from "better-auth/crypto";
import { describe, expect, it } from "vitest";
import { makeAuth } from "../src/auth.js";
import type { Env } from "../src/env.js";
import { app } from "../src/index.js";
import { blockNetwork } from "./helpers/harness.js";
import { setup } from "./helpers/fixture.js";

const AUTH_SECRET = "0123456789abcdef0123456789abcdef";
const SESSION_COOKIE = "__Secure-better-auth.session_token";
const STATE_COOKIE = "__Secure-better-auth.state";

async function signedCookieValue(value: string): Promise<string> {
  return encodeURIComponent(`${value}.${await makeSignature(value, AUTH_SECRET)}`);
}

function oauthFixture() {
  const harness = setup();
  const bindings = harness.env as unknown as Record<string, unknown>;
  bindings.AUTH_MODE = "oauth";
  bindings.BETTER_AUTH_SECRET = AUTH_SECRET;
  bindings.BETTER_AUTH_URL = "https://wikilean.example";
  bindings.GITHUB_CLIENT_ID = "Iv1.test";
  bindings.GITHUB_CLIENT_SECRET = "test-secret";
  return harness;
}

describe("Better Auth Drizzle adapter", () => {
  blockNetwork();

  it("instantiates against the D1 schema", () => {
    const { env, db } = oauthFixture();
    try {
      expect(typeof makeAuth(env).handler).toBe("function");
    } finally {
      db.close();
    }
  });

  it("reads unauthenticated, bogus, and valid sessions through the adapter", async () => {
    const { env, db } = oauthFixture();
    try {
      const auth = makeAuth(env);
      expect(await auth.api.getSession({ headers: new Headers() })).toBeNull();

      const bogusHeaders = new Headers({
        cookie: `${SESSION_COOKIE}=deadbeef.sig`,
      });
      expect(await auth.api.getSession({ headers: bogusHeaders })).toBeNull();

      const token = "session-token-u-human";
      const now = Math.floor(Date.now() / 1000);
      const expiresAt = now + 7 * 24 * 60 * 60;
      db.prepare(
        "INSERT INTO sessions (id, user_id, token, expires_at, ip_address, user_agent, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
      ).run("session-u-human", "u-human", token, expiresAt, "203.0.113.7", "vitest", now, now);

      const headers = new Headers({
        cookie: `${SESSION_COOKIE}=${await signedCookieValue(token)}`,
      });
      const result = await auth.api.getSession({ headers });
      expect(result).not.toBeNull();
      expect(result!.session).toMatchObject({
        id: "session-u-human",
        userId: "u-human",
        token,
        ipAddress: "203.0.113.7",
        userAgent: "vitest",
      });
      expect(result!.session.expiresAt.getTime()).toBe(expiresAt * 1000);
      expect(result!.user).toMatchObject({
        id: "u-human",
        name: "Human Tester",
        email: "human@example.org",
        emailVerified: false,
        role: "user",
      });
    } finally {
      db.close();
    }
  });

  it("serves the Better Auth session route in OAuth mode", async () => {
    const { env, db } = oauthFixture();
    try {
      const response = await app.request(
        "https://wikilean.example/api/auth/get-session",
        { headers: new Headers() },
        env,
      );
      expect(response.status).toBe(200);
      expect(response.headers.get("content-type")).toBe("application/json");
      expect(await response.json()).toBeNull();
    } finally {
      db.close();
    }
  });

  it("persists social sign-in PKCE state through Drizzle", async () => {
    const { env, db } = oauthFixture();
    try {
      const response = await app.request(
        "https://wikilean.example/api/auth/sign-in/social",
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ provider: "github", callbackURL: "/" }),
        },
        env,
      );

      expect(response.status).toBe(200);
      expect(response.headers.get("content-type")).toBe("application/json");
      const body = (await response.json()) as { redirect: boolean; url: string };
      expect(body.redirect).toBe(true);
      expect(response.headers.get("location")).toBe(body.url);

      const authorizationURL = new URL(body.url);
      expect(authorizationURL.origin).toBe("https://github.com");
      expect(authorizationURL.pathname).toBe("/login/oauth/authorize");
      const state = authorizationURL.searchParams.get("state");
      expect(state).toMatch(/^[A-Za-z0-9_-]{32}$/);

      const cookies = parseSetCookieHeader(response.headers.get("set-cookie") ?? "");
      const stateCookie = cookies.get(STATE_COOKIE);
      expect(stateCookie).toMatchObject({
        secure: true,
        httponly: true,
        samesite: "lax",
        path: "/",
      });
      const signedState = stateCookie!.value;
      const signatureStart = signedState.lastIndexOf(".");
      const cookieState = signedState.slice(0, signatureStart);
      const signature = signedState.slice(signatureStart + 1);
      expect(cookieState).toBe(state);
      expect(signature).toBe(await makeSignature(cookieState, AUTH_SECRET));

      const row = db
        .prepare("SELECT identifier, value FROM verifications WHERE identifier = ?")
        .get(state) as { identifier: string; value: string } | undefined;
      expect(row).toBeDefined();
      expect(row!.identifier).toBe(state);
      const verification = JSON.parse(row!.value) as {
        callbackURL: string;
        codeVerifier: string;
        oauthState: string;
      };
      expect(verification).toMatchObject({ callbackURL: "/", oauthState: state });
      expect(verification.codeVerifier).toMatch(/^[A-Za-z0-9_-]{128}$/);

      const challengeBytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verification.codeVerifier));
      expect(authorizationURL.searchParams.get("code_challenge")).toBe(Buffer.from(challengeBytes).toString("base64url"));
      expect(authorizationURL.searchParams.get("code_challenge_method")).toBe("S256");
    } finally {
      db.close();
    }
  });
});
