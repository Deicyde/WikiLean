// Auth. Two modes selected by env.AUTH_MODE:
//   "oauth" → better-auth (GitHub/Google), the production path.
//   "dev"   → a cookie stub for local development without OAuth credentials.
// getUser() is the single seam the rest of the app depends on.

import type { Context, Hono } from "hono";
import { getCookie, setCookie, deleteCookie } from "hono/cookie";
import { betterAuth } from "better-auth";
import { drizzleAdapter } from "better-auth/adapters/drizzle";
import { drizzle } from "drizzle-orm/d1";
import { eq } from "drizzle-orm";
import { users, sessions, accounts, verifications } from "./db/schema.js";
import { htmlEscape } from "./engine/html.js";
import type { Env } from "./env.js";

export interface AuthUser {
  id: string;
  name: string;
  role: string;
}

const DEV_COOKIE = "wl_dev_user";
// The users row a matching PIPELINE_TOKEN bearer resolves to. The row is the
// kill switch and role source of truth: delete it (→ unauthenticated) or set
// role='blocked' (→ anonymous) to cut pipeline access without rotating the
// secret.
const PIPELINE_USER_ID = "pipeline";
type Ctx = Context<{ Bindings: Env }>;

// Per-request better-auth instance (Workers bindings are only available per request).
export function makeAuth(env: Env) {
  const db = drizzle(env.DB);
  const socialProviders: Record<
    string,
    { clientId: string; clientSecret: string; scope?: string[] }
  > = {};
  if (env.GITHUB_CLIENT_ID && env.GITHUB_CLIENT_SECRET) {
    // Stopgap auto-post: `public_repo` lets a signed-in reviewer post their
    // review as inline PR comments in-app (POST /api/review). Caveat: this
    // classic scope grants read+WRITE to ALL of the user's public repos — an
    // over-broad grant we accept only as a stopgap. (Existing users must sign
    // out + back in once to grant the added scope.)
    // TODO(github-app): replace with a fine-grained GitHub App ("Pull requests:
    // write" on selected repos) so we can drop `public_repo` entirely.
    socialProviders.github = {
      clientId: env.GITHUB_CLIENT_ID,
      clientSecret: env.GITHUB_CLIENT_SECRET,
      scope: ["read:user", "user:email", "public_repo"],
    };
  }
  if (env.GOOGLE_CLIENT_ID && env.GOOGLE_CLIENT_SECRET) {
    socialProviders.google = { clientId: env.GOOGLE_CLIENT_ID, clientSecret: env.GOOGLE_CLIENT_SECRET };
  }
  return betterAuth({
    secret: env.BETTER_AUTH_SECRET,
    baseURL: env.BETTER_AUTH_URL,
    database: drizzleAdapter(db, {
      provider: "sqlite",
      schema: { user: users, session: sessions, account: accounts, verification: verifications },
    }),
    socialProviders,
    user: {
      additionalFields: {
        role: { type: "string", required: false, defaultValue: "user", input: false },
      },
    },
    // Explicit session-cookie hardening (CSRF/secure-transport defense-in-depth).
    // `__Secure-` prefix + Secure flag; SameSite=Lax allows top-level OAuth
    // redirects back to us while blocking cross-site cookie sends.
    advanced: {
      useSecureCookies: true,
      defaultCookieAttributes: {
        sameSite: "lax",
        secure: true,
      },
    },
  });
}

function availableProviders(env: Env): string[] {
  const p: string[] = [];
  if (env.GITHUB_CLIENT_ID && env.GITHUB_CLIENT_SECRET) p.push("github");
  if (env.GOOGLE_CLIENT_ID && env.GOOGLE_CLIENT_SECRET) p.push("google");
  return p;
}

export async function getUser(c: Ctx): Promise<AuthUser | null> {
  // Pipeline bearer branch — checked before the session paths so the runner
  // never touches cookie/better-auth machinery. Requires BOTH the header and
  // the PIPELINE_TOKEN secret (skip if either is missing); an exact match
  // resolves to the 'pipeline' users row (role 'bot'). Missing row → treated
  // as unauthenticated. A non-matching bearer falls through to session auth.
  const authz = c.req.header("Authorization");
  if (authz?.startsWith("Bearer ") && c.env.PIPELINE_TOKEN && authz.slice(7) === c.env.PIPELINE_TOKEN) {
    const db = drizzle(c.env.DB);
    const row = (await db.select().from(users).where(eq(users.id, PIPELINE_USER_ID)).limit(1))[0];
    if (!row) return null;
    // Blocked users are treated as anonymous everywhere — bearer included.
    if (row.role === "blocked") return null;
    return { id: row.id, name: row.name ?? row.id, role: row.role };
  }
  if (c.env.AUTH_MODE === "oauth") {
    try {
      const session = await makeAuth(c.env).api.getSession({ headers: c.req.raw.headers });
      const u = session?.user as { id: string; name?: string; email?: string; role?: string } | undefined;
      if (!u) return null;
      const role = u.role || "user";
      // Blocked users are treated as anonymous everywhere.
      if (role === "blocked") return null;
      return { id: u.id, name: u.name || u.email || u.id, role };
    } catch {
      return null;
    }
  }
  // dev cookie stub
  const id = getCookie(c, DEV_COOKIE);
  if (!id) return null;
  const db = drizzle(c.env.DB);
  const row = (await db.select().from(users).where(eq(users.id, id)).limit(1))[0];
  if (!row) return null;
  // Blocked users are treated as anonymous everywhere.
  if (row.role === "blocked") return null;
  return { id: row.id, name: row.name ?? row.id, role: row.role };
}

// Returns the current user if their role is in `roles`, else null (caller sends
// 403). Blocked/anonymous users resolve to null via getUser already.
export async function requireRole(c: Ctx, roles: string[]): Promise<AuthUser | null> {
  const user = await getUser(c);
  if (!user || !roles.includes(user.role)) return null;
  return user;
}

function loginPageHtml(returnTo: string, providers: string[]): string {
  const buttons = providers
    .map(
      (p) =>
        `<button class="prov" data-provider="${p}">Continue with ${p[0].toUpperCase() + p.slice(1)}</button>`,
    )
    .join("");
  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>WikiLean · Sign in</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f7f4ee;color:#1f1d1a;display:grid;place-items:center;min-height:100vh;margin:0}
.box{background:#fffdf9;border:1px solid #d8d0bd;border-radius:12px;padding:32px;max-width:340px;text-align:center}
h1{font-size:1.2rem;margin:0 0 6px}p{color:#5f594e;font-size:.9rem;margin:0 0 18px}
.prov{display:block;width:100%;margin:8px 0;padding:11px;border:1px solid #1a4b8c;border-radius:8px;background:#1a4b8c;color:#fff;font:inherit;font-weight:600;cursor:pointer}
.prov:hover{background:#163e74;border-color:#163e74}a{color:#1a4b8c}
:focus-visible{outline:2px solid #1a4b8c;outline-offset:2px}</style></head>
<body><div class="box"><h1>Sign in to edit WikiLean</h1>
<p>Editing annotations requires an account. Reading is open to everyone.</p>
${buttons || "<p>No login providers are configured.</p>"}
<p style="margin-top:16px"><a href="${htmlEscape(returnTo, true)}">← back</a></p></div>
<script>
var ret=${JSON.stringify(returnTo)};
document.querySelectorAll(".prov").forEach(function(b){b.addEventListener("click",function(){
  b.disabled=true;b.textContent="redirecting…";
  fetch("/api/auth/sign-in/social",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({provider:b.dataset.provider,callbackURL:ret})})
    .then(function(r){return r.json()}).then(function(res){
      if(res&&res.url){location.href=res.url}else{alert("sign-in failed");b.disabled=false;b.textContent="Continue";}})
    .catch(function(e){alert("sign-in failed: "+e);b.disabled=false;});
});});
</script></body></html>`;
}

export function registerAuthRoutes(app: Hono<{ Bindings: Env }>): void {
  // Specific /api/auth/* routes MUST be registered before the better-auth
  // wildcard below, since Hono runs the first matching handler.

  // Dev cookie login.
  app.get("/api/auth/dev-login", async (c) => {
    if (c.env.AUTH_MODE !== "dev") return c.text("dev auth disabled", 403);
    const name = (c.req.query("name") || "Dev User").slice(0, 60);
    const id = "dev:" + (name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "user");
    const db = drizzle(c.env.DB);
    const now = new Date();
    await db.insert(users).values({ id, name, role: "user", createdAt: now, updatedAt: now }).onConflictDoNothing();
    setCookie(c, DEV_COOKIE, id, { path: "/", httpOnly: true, sameSite: "Lax", maxAge: 60 * 60 * 24 * 30 });
    return c.redirect(c.req.query("returnTo") || "/");
  });

  app.get("/api/auth/me", async (c) => c.json({ user: await getUser(c) }));

  // better-auth owns the rest of /api/auth/* in oauth mode (sign-in, callback,
  // sign-out, session). Registered after the specific routes above.
  app.on(["GET", "POST"], "/api/auth/*", (c) => {
    if (c.env.AUTH_MODE !== "oauth") return c.notFound();
    return makeAuth(c.env).handler(c.req.raw);
  });

  // Sign-in page.
  app.get("/login", (c) => {
    const ret = c.req.query("returnTo") || "/";
    if (c.env.AUTH_MODE !== "oauth") {
      const u = new URL("/api/auth/dev-login", c.req.url);
      u.searchParams.set("returnTo", ret);
      return c.redirect(u.pathname + u.search);
    }
    return c.html(loginPageHtml(ret, availableProviders(c.env)));
  });

  // Sign-out (both modes).
  app.get("/logout", (c) => {
    const ret = c.req.query("returnTo") || "/";
    if (c.env.AUTH_MODE === "oauth") {
      // Styled to the warm palette so the flash page matches the site (W3 fix #6c).
      return c.html(
        `<!doctype html><meta charset="utf-8"><title>Signing out…</title>` +
          `<style>body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f7f4ee;color:#5f594e;display:grid;place-items:center;min-height:100vh;margin:0}</style>` +
          `<script>fetch("/api/auth/sign-out",{method:"POST"}).then(function(){location.href=${JSON.stringify(ret)}}).catch(function(){location.href=${JSON.stringify(ret)}});</script>` +
          `Signing out…`,
      );
    }
    deleteCookie(c, DEV_COOKIE, { path: "/" });
    return c.redirect(ret);
  });
}
