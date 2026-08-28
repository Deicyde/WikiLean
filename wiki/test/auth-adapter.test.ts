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

  it("persists a first GitHub login through the OAuth callback", async () => {
    const { env, db } = oauthFixture();
    try {
      const signInResponse = await app.request(
        "https://wikilean.example/api/auth/sign-in/social",
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ provider: "github", callbackURL: "/" }),
        },
        env,
      );
      expect(signInResponse.status).toBe(200);

      const signInBody = (await signInResponse.json()) as { url: string };
      const state = new URL(signInBody.url).searchParams.get("state");
      expect(state).toMatch(/^[A-Za-z0-9_-]{32}$/);

      const stateCookie = parseSetCookieHeader(signInResponse.headers.get("set-cookie") ?? "").get(STATE_COOKIE);
      expect(stateCookie).toBeDefined();
      const verificationRow = db
        .prepare("SELECT value FROM verifications WHERE identifier = ?")
        .get(state) as { value: string } | undefined;
      expect(verificationRow).toBeDefined();
      const verification = JSON.parse(verificationRow!.value) as { codeVerifier: string };

      const githubCode = "github-authorization-code";
      const accessToken = "gho_test_access_token";
      const refreshToken = "ghr_test_refresh_token";
      const requestURLs: string[] = [];
      const blockedFetch = globalThis.fetch;
      const callbackStartedAt = Math.floor(Date.now() / 1000);

      globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = input instanceof Request ? input : new Request(input, init);
        requestURLs.push(request.url);

        if (request.url === "https://github.com/login/oauth/access_token") {
          expect(request.method).toBe("POST");
          expect(request.headers.get("accept")).toBe("application/json");
          expect(request.headers.get("content-type")).toBe("application/x-www-form-urlencoded");
          const body = new URLSearchParams(await request.clone().text());
          expect(Object.fromEntries(body)).toEqual({
            grant_type: "authorization_code",
            code: githubCode,
            code_verifier: verification.codeVerifier,
            redirect_uri: "https://wikilean.example/api/auth/callback/github",
            client_id: "Iv1.test",
            client_secret: "test-secret",
          });
          return Response.json({
            access_token: accessToken,
            refresh_token: refreshToken,
            token_type: "bearer",
            scope: "read:user user:email",
            expires_in: 3600,
            refresh_token_expires_in: 7200,
          });
        }

        if (request.url === "https://api.github.com/user") {
          expect(request.method).toBe("GET");
          expect(request.headers.get("authorization")).toBe(`Bearer ${accessToken}`);
          expect(request.headers.get("user-agent")).toBe("better-auth");
          expect(await request.clone().text()).toBe("");
          return Response.json({
            id: 1234567,
            login: "octo-tester",
            name: "Octo Tester",
            email: null,
            avatar_url: "https://avatars.example/octo-tester.png",
          });
        }

        if (request.url === "https://api.github.com/user/emails") {
          expect(request.method).toBe("GET");
          expect(request.headers.get("authorization")).toBe(`Bearer ${accessToken}`);
          expect(request.headers.get("user-agent")).toBe("better-auth");
          expect(await request.clone().text()).toBe("");
          return Response.json([
            { email: "secondary@example.org", primary: false, verified: true, visibility: null },
            { email: "octo@example.org", primary: true, verified: true, visibility: "private" },
          ]);
        }

        throw new Error(`unexpected network fetch in OAuth callback test: ${request.method} ${request.url}`);
      }) as typeof fetch;

      let callbackResponse: Response;
      try {
        callbackResponse = await app.request(
          `https://wikilean.example/api/auth/callback/github?code=${githubCode}&state=${state}`,
          {
            headers: {
              cookie: `${STATE_COOKIE}=${encodeURIComponent(stateCookie!.value)}`,
              "user-agent": "oauth-callback-test",
            },
          },
          env,
        );
      } finally {
        globalThis.fetch = blockedFetch;
      }
      const callbackFinishedAt = Math.floor(Date.now() / 1000);

      expect(callbackResponse.status).toBe(302);
      expect(callbackResponse.headers.get("location")).toBe("/");
      expect(requestURLs).toEqual([
        "https://github.com/login/oauth/access_token",
        "https://api.github.com/user",
        "https://api.github.com/user/emails",
      ]);
      expect(
        db.prepare("SELECT COUNT(*) AS count FROM verifications WHERE identifier = ?").get(state),
      ).toEqual({ count: 0 });

      const user = db
        .prepare(
          "SELECT id, name, email, email_verified, image, role, created_at, updated_at FROM users WHERE email = ?",
        )
        .get("octo@example.org") as
        | {
            id: string;
            name: string;
            email: string;
            email_verified: number;
            image: string;
            role: string;
            created_at: number;
            updated_at: number;
          }
        | undefined;
      expect(user).toBeDefined();
      expect(user).toMatchObject({
        name: "Octo Tester",
        email: "octo@example.org",
        email_verified: 1,
        image: "https://avatars.example/octo-tester.png",
        role: "user",
      });
      expect(user!.id).toMatch(/^[A-Za-z0-9_-]{32}$/);
      expect(user!.created_at).toBeGreaterThanOrEqual(callbackStartedAt);
      expect(user!.created_at).toBeLessThanOrEqual(callbackFinishedAt);
      expect(user!.updated_at).toBe(user!.created_at);

      const account = db
        .prepare(
          "SELECT id, user_id, account_id, provider_id, access_token, refresh_token, access_token_expires_at, refresh_token_expires_at, scope, created_at, updated_at FROM accounts WHERE provider_id = ?",
        )
        .get("github") as
        | {
            id: string;
            user_id: string;
            account_id: string;
            provider_id: string;
            access_token: string;
            refresh_token: string;
            access_token_expires_at: number;
            refresh_token_expires_at: number;
            scope: string;
            created_at: number;
            updated_at: number;
          }
        | undefined;
      expect(account).toBeDefined();
      expect(account).toMatchObject({
        user_id: user!.id,
        account_id: "1234567",
        provider_id: "github",
        access_token: accessToken,
        refresh_token: refreshToken,
        scope: "read:user,user:email",
      });
      expect(account!.id).toMatch(/^[A-Za-z0-9_-]{32}$/);
      expect(account!.created_at).toBeGreaterThanOrEqual(callbackStartedAt);
      expect(account!.created_at).toBeLessThanOrEqual(callbackFinishedAt);
      expect(account!.updated_at).toBe(account!.created_at);
      expect(account!.access_token_expires_at).toBeGreaterThanOrEqual(callbackStartedAt + 3600);
      expect(account!.access_token_expires_at).toBeLessThanOrEqual(callbackFinishedAt + 3600);
      expect(account!.refresh_token_expires_at).toBeGreaterThanOrEqual(callbackStartedAt + 7200);
      expect(account!.refresh_token_expires_at).toBeLessThanOrEqual(callbackFinishedAt + 7200);

      const session = db
        .prepare(
          "SELECT id, user_id, token, expires_at, user_agent, created_at, updated_at FROM sessions WHERE user_id = ?",
        )
        .get(user!.id) as
        | {
            id: string;
            user_id: string;
            token: string;
            expires_at: number;
            user_agent: string;
            created_at: number;
            updated_at: number;
          }
        | undefined;
      expect(session).toBeDefined();
      expect(session).toMatchObject({ user_id: user!.id, user_agent: "oauth-callback-test" });
      expect(session!.id).toMatch(/^[A-Za-z0-9_-]{32}$/);
      expect(session!.token).toMatch(/^[A-Za-z0-9_-]{32}$/);
      expect(session!.created_at).toBeGreaterThanOrEqual(callbackStartedAt);
      expect(session!.created_at).toBeLessThanOrEqual(callbackFinishedAt);
      expect(session!.updated_at).toBe(session!.created_at);
      const sevenDays = 7 * 24 * 60 * 60;
      expect(session!.expires_at).toBeGreaterThanOrEqual(callbackStartedAt + sevenDays);
      expect(session!.expires_at).toBeLessThanOrEqual(callbackFinishedAt + sevenDays);

      const callbackCookies = parseSetCookieHeader(callbackResponse.headers.get("set-cookie") ?? "");
      const sessionCookie = callbackCookies.get(SESSION_COOKIE);
      expect(sessionCookie).toMatchObject({
        secure: true,
        httponly: true,
        samesite: "lax",
        path: "/",
      });
      expect(sessionCookie!.value).toBe(`${session!.token}.${await makeSignature(session!.token, AUTH_SECRET)}`);
    } finally {
      db.close();
    }
  });
});
