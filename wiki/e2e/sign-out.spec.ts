import { expect, test, type APIRequestContext, type BrowserContext, type Page } from "@playwright/test";

// The oauth-mode listener in e2e/server.ts: the next port after the dev-mode one.
const PORT = Number.parseInt(process.env.WIKILEAN_E2E_PORT ?? "4173", 10) + 1;
const SITE = `http://127.0.0.1:${PORT}`;
const SESSION_COOKIE = "__Secure-better-auth.session_token";
const SIGN_OUT = "**/api/auth/sign-out";

test.use({ baseURL: SITE });

interface Session {
  token: string;
  cookie: string;
}

async function signIn(context: BrowserContext, request: APIRequestContext): Promise<Session> {
  const session = (await (await request.post(`${SITE}/__e2e/session`)).json()) as Session;
  await context.addCookies([
    { name: SESSION_COOKIE, value: session.cookie, domain: "127.0.0.1", path: "/", secure: true, httpOnly: true, sameSite: "Lax" },
  ]);
  return session;
}

async function sessionRows(request: APIRequestContext, session: Session): Promise<number> {
  const res = await request.get(`${SITE}/__e2e/session-rows?token=${encodeURIComponent(session.token)}`);
  return ((await res.json()) as { rows: number }).rows;
}

// /api/auth/me for a request holding the given cookie value, without the browser's jar.
async function userFor(request: APIRequestContext, session: Session): Promise<unknown> {
  const res = await request.get(`${SITE}/api/auth/me`, { headers: { Cookie: `${SESSION_COOKIE}=${session.cookie}` } });
  return ((await res.json()) as { user: unknown }).user;
}

async function expectFailureShown(page: Page, returnTo: string): Promise<void> {
  const alert = page.getByRole("alert");
  await expect(alert).toBeVisible();
  await expect(alert).toContainText("Sign-out failed");
  await expect(page.getByRole("status")).toBeHidden();
  await expect(page).toHaveTitle("Sign-out failed");
  expect(new URL(page.url()).pathname).toBe("/logout");
  await expect(alert.getByRole("link", { name: "Continue without signing out" })).toHaveAttribute("href", returnTo);
}

test("the /logout page ends the session, then goes to returnTo", async ({ context, page, request }) => {
  const session = await signIn(context, request);
  expect(await sessionRows(request, session)).toBe(1);
  expect(await userFor(request, session)).toMatchObject({ id: "u-human" });
  // cookies() with no URL: given an http URL, Playwright leaves out Secure cookies.
  expect((await context.cookies()).map((c) => c.name)).toContain(SESSION_COOKIE);

  const signOut = page.waitForRequest(SIGN_OUT);
  await page.goto("/logout?returnTo=/Test_Article");
  await page.waitForURL("**/Test_Article");

  // Sent as the browser sends it: same origin, no type, no body. That is the
  // shape this test exists to keep working, so it fails if the page changes it.
  const sent = await signOut;
  expect(sent.postData()).toBeNull();
  const headers = await sent.allHeaders();
  expect(headers["content-type"]).toBeUndefined();
  expect(headers.origin).toBe(SITE);
  expect((await sent.response())?.status()).toBe(200);

  expect(await sessionRows(request, session)).toBe(0);
  expect(await userFor(request, session)).toBeNull();
  const jar = await page.request.get("/api/auth/me");
  expect(((await jar.json()) as { user: unknown }).user).toBeNull();
  expect((await context.cookies()).map((c) => c.name)).not.toContain(SESSION_COOKIE);
});

test("a refused sign-out says so, stays put, and can be retried", async ({ context, page, request }) => {
  const session = await signIn(context, request);
  await page.route(SIGN_OUT, (route) =>
    route.fulfill({ status: 415, json: { message: "refused", code: "UNSUPPORTED_MEDIA_TYPE" } }),
  );
  await page.goto("/logout?returnTo=/Test_Article");
  await expectFailureShown(page, "/Test_Article");
  expect(await sessionRows(request, session)).toBe(1);
  expect(await userFor(request, session)).toMatchObject({ id: "u-human" });

  await page.unroute(SIGN_OUT);
  await page.getByRole("link", { name: "Try again" }).click();
  await page.waitForURL("**/Test_Article");
  expect(await sessionRows(request, session)).toBe(0);
});

test("a network error while signing out says so", async ({ context, page, request }) => {
  const session = await signIn(context, request);
  await page.route(SIGN_OUT, (route) => route.abort());
  await page.goto("/logout?returnTo=/Test_Article");
  await expectFailureShown(page, "/Test_Article");
  expect(await sessionRows(request, session)).toBe(1);
});

test("an OK answer that does not report success says so", async ({ context, page, request }) => {
  await signIn(context, request);
  await page.route(SIGN_OUT, (route) => route.fulfill({ status: 200, json: {} }));
  await page.goto("/logout?returnTo=/Test_Article");
  await expectFailureShown(page, "/Test_Article");
});

test("a hostile returnTo goes nowhere near its target", async ({ context, page, request }) => {
  await signIn(context, request);
  await page.goto("/logout?returnTo=" + encodeURIComponent("//evil.example/path"));
  await page.waitForURL(`${SITE}/`);
});

test("a markup returnTo runs nothing on the failure path", async ({ context, page, request }) => {
  await signIn(context, request);
  await page.route(SIGN_OUT, (route) => route.fulfill({ status: 500, body: "" }));
  const returnTo = `/x"><img src=y onerror="window.__xss=1"></script><script>window.__xss=2</script>`;
  await page.goto("/logout?returnTo=" + encodeURIComponent(returnTo));
  await expectFailureShown(page, returnTo);
  expect(await page.evaluate(() => Reflect.get(globalThis, "__xss"))).toBeUndefined();
});
