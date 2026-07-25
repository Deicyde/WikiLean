// Per-user GitHub repo registration (/repos + /api/repos): a signed-in user
// picks which of their public repos the ops nightly should index as Lean
// sources (alongside the always-on globals: Mathlib, FormalConjectures,
// TauCeti). The nightly consumes the distinct enabled set via the
// UNAUTHENTICATED GET /api/repos/enabled — a PINNED cross-agent contract:
//   {"repos":[{"owner":string,"repo":string,"lib":string}]}
// owner/repo match ^[A-Za-z0-9_.-]{1,100}$ (owner never starts with '.'),
// lib matches ^[A-Z][A-Za-z0-9_]{0,63}$; enabled-only, distinct, ≤500 rows,
// stable order.
//
// The GitHub listing is SERVER-side via GITHUB_API_TOKEN (read-only PAT):
// the shared wiki login is identity-only by design (auth.ts) — no repo
// scopes, and stored user OAuth tokens are never used here. We resolve the
// caller's numeric GitHub account id from the better-auth accounts table,
// then read their PUBLIC repos, which needs no user consent or scope.

import type { Context, Hono } from "hono";
import { drizzle } from "drizzle-orm/d1";
import { and, asc, eq } from "drizzle-orm";
import type { Env } from "./env.js";
import { getUser, type AuthUser } from "./auth.js";
import { accounts, userRepos } from "./db/schema.js";
import { htmlEscape } from "./engine/html.js";

type Ctx = Context<{ Bindings: Env }>;

const GH_API = "https://api.github.com";
const UA = "WikiLean-repos/0.1 (+https://wikilean.jackmccarthy.org)";

// owner/repo: GitHub's own charset, capped. Owner additionally must not start
// with '.' (no such GitHub login exists; blocks path-trick strings). repo MAY
// (".github" is real).
export const REPO_NAME_RE = /^[A-Za-z0-9_.-]{1,100}$/;
export const LIB_RE = /^[A-Z][A-Za-z0-9_]{0,63}$/;

export const REPO_CAP = 20; // registered repos per user
const MAX_ITEMS = 50; // items per bulk call
const GH_CACHE_TTL = 600; // 10 min per-user GitHub listing cache

// Fixed always-on sources the nightly indexes regardless of registrations —
// shown on /repos so users see the full picture. Not stored in user_repos.
const GLOBAL_SOURCES = [
  { owner: "leanprover-community", repo: "mathlib4", lib: "Mathlib" },
  { owner: "google-deepmind", repo: "formal-conjectures", lib: "FormalConjectures" },
  { owner: "TauCetiProject", repo: "TauCeti", lib: "TauCeti" },
];

// Default Lean library name: the repo name CamelCased with invalid chars
// stripped ("my-lean_lib2" → "MyLeanLib2"). Null when nothing lib-shaped
// survives (e.g. "1234"): the caller must then provide `lib` explicitly.
export function defaultLibName(repo: string): string | null {
  const joined = repo
    .split(/[^A-Za-z0-9]+/)
    .filter(Boolean)
    .map((p) => p[0].toUpperCase() + p.slice(1))
    .join("")
    .replace(/^[^A-Za-z]+/, ""); // must start with a letter
  if (!joined) return null;
  const lib = (joined[0].toUpperCase() + joined.slice(1)).slice(0, 64);
  return LIB_RE.test(lib) ? lib : null;
}

// CSRF: reject a cross-origin write (local copy — index.ts imports this module).
function checkOrigin(c: Ctx): Response | null {
  const origin = c.req.header("Origin");
  if (origin && origin !== new URL(c.req.url).origin) {
    return c.json({ ok: false, error: "cross-origin request rejected" }, 403);
  }
  return null;
}

// Repo registration is a PERSON's surface: 401 anonymous/blocked, 403 for the
// pipeline bearer (role 'bot' — it has no GitHub identity or repo prefs).
async function sessionUser(c: Ctx): Promise<AuthUser | Response> {
  const user = await getUser(c);
  if (!user) return c.json({ ok: false, error: "login required" }, 401);
  if (user.role === "bot") return c.json({ ok: false, error: "session required (no bots)" }, 403);
  return user;
}

function ghHeaders(token: string): HeadersInit {
  return { "User-Agent": UA, Accept: "application/vnd.github+json", Authorization: `Bearer ${token}` };
}

async function ghJson<T>(url: string, token: string): Promise<T> {
  const r = await fetch(url, { headers: ghHeaders(token) });
  if (!r.ok) throw new Error(`GitHub ${r.status} for ${url}`);
  return r.json() as Promise<T>;
}

interface GhRepoOut {
  name: string;
  owner: string;
  language: string | null;
  description: string | null;
  updated_at: string | null;
}

interface RepoItem {
  owner: string;
  repo: string;
  lib: string;
  enabled: boolean;
}

// Parse the bulk body: {repos:[…]} (or a bare array). Returns items or an
// error string. Shape-only — per-item field validation happens in the caller.
function parseBulk(body: unknown): Record<string, unknown>[] | string {
  const arr = Array.isArray(body)
    ? body
    : body && typeof body === "object" && Array.isArray((body as { repos?: unknown }).repos)
      ? ((body as { repos: unknown[] }).repos as unknown[])
      : null;
  if (!arr) return "expected {repos:[…]}";
  if (arr.length === 0) return "repos is empty";
  if (arr.length > MAX_ITEMS) return `too many items (max ${MAX_ITEMS} per call)`;
  if (!arr.every((x) => x && typeof x === "object")) return "repos items must be objects";
  return arr as Record<string, unknown>[];
}

async function listMine(c: Ctx, userId: string): Promise<RepoItem[]> {
  const db = drizzle(c.env.DB);
  const rows = await db
    .select()
    .from(userRepos)
    .where(eq(userRepos.userId, userId))
    .orderBy(asc(userRepos.owner), asc(userRepos.repo));
  return rows.map((r) => ({ owner: r.owner, repo: r.repo, lib: r.lib, enabled: r.enabled !== 0 }));
}

export function registerRepoRoutes(app: Hono<{ Bindings: Env }>): void {
  // ---- GET /api/repos — the caller's registered rows -----------------------
  app.get("/api/repos", async (c) => {
    const user = await sessionUser(c);
    if (user instanceof Response) return user;
    return c.json({ ok: true, repos: await listMine(c, user.id) });
  });

  // ---- GET /api/repos/github — the caller's PUBLIC GitHub repos ------------
  // Server-side reads with GITHUB_API_TOKEN (5000/hr); the client marks Lean
  // ones by `language`. Cached per user for 10 min in RENDER_CACHE.
  app.get("/api/repos/github", async (c) => {
    const user = await sessionUser(c);
    if (user instanceof Response) return user;

    const cacheKey = `repos:gh:${user.id}`;
    const cached = await c.env.RENDER_CACHE.get(cacheKey);
    if (cached) return c.body(cached, 200, { "Content-Type": "application/json" });

    const db = drizzle(c.env.DB);
    const acct = (
      await db
        .select({ accountId: accounts.accountId })
        .from(accounts)
        .where(and(eq(accounts.userId, user.id), eq(accounts.providerId, "github")))
        .limit(1)
    )[0];
    // Numeric-id check doubles as URL-injection safety on the fetch below.
    if (!acct || !/^\d+$/.test(acct.accountId)) {
      return c.json({ ok: false, error: "no linked GitHub account" }, 404);
    }
    const token = c.env.GITHUB_API_TOKEN;
    if (!token) return c.json({ ok: false, error: "GITHUB_API_TOKEN not configured" }, 503);

    try {
      // Numeric account id → current login (survives renames), then the
      // login's public repos (the /users listing only ever returns public).
      const gu = await ghJson<{ login?: string }>(`${GH_API}/user/${acct.accountId}`, token);
      if (!gu.login) throw new Error("GitHub account lookup returned no login");
      const repos: GhRepoOut[] = [];
      for (let page = 1; page <= 3; page++) {
        const batch = await ghJson<
          Array<{
            name?: string;
            owner?: { login?: string };
            language?: string | null;
            description?: string | null;
            updated_at?: string | null;
          }>
        >(
          `${GH_API}/users/${encodeURIComponent(gu.login)}/repos?per_page=100&page=${page}&sort=updated`,
          token,
        );
        for (const r of batch) {
          if (typeof r.name !== "string") continue;
          repos.push({
            name: r.name,
            owner: r.owner?.login ?? gu.login,
            language: r.language ?? null,
            description: r.description ?? null,
            updated_at: r.updated_at ?? null,
          });
        }
        if (batch.length < 100) break;
      }
      const json = JSON.stringify({ ok: true, login: gu.login, repos });
      await c.env.RENDER_CACHE.put(cacheKey, json, { expirationTtl: GH_CACHE_TTL });
      return c.body(json, 200, { "Content-Type": "application/json" });
    } catch (e) {
      return c.json({ ok: false, error: String(e instanceof Error ? e.message : e) }, 502);
    }
  });

  // ---- POST /api/repos — bulk upsert [{owner,repo,enabled,lib?}] -----------
  app.post("/api/repos", async (c) => {
    const bad = checkOrigin(c);
    if (bad) return bad;
    const user = await sessionUser(c);
    if (user instanceof Response) return user;
    const limiter = c.env.REPO_LIMITER ?? c.env.EDIT_LIMITER;
    const rl = await limiter.limit({ key: `repos:${user.id}` });
    if (!rl.success) return c.json({ ok: false, error: "rate limited" }, 429);

    let body: unknown;
    try {
      body = await c.req.json();
    } catch {
      return c.json({ ok: false, error: "bad JSON body" }, 400);
    }
    const raw = parseBulk(body);
    if (typeof raw === "string") return c.json({ ok: false, error: raw }, 400);

    // Validate everything before writing anything (no partial application);
    // duplicate (owner,repo) within one call: last wins.
    const items = new Map<string, RepoItem & { libExplicit: boolean }>();
    for (let i = 0; i < raw.length; i++) {
      const it = raw[i];
      const owner = typeof it.owner === "string" ? it.owner : "";
      const repo = typeof it.repo === "string" ? it.repo : "";
      if (!REPO_NAME_RE.test(owner) || owner.startsWith(".")) {
        return c.json({ ok: false, error: `repos[${i}]: bad owner` }, 400);
      }
      if (!REPO_NAME_RE.test(repo)) return c.json({ ok: false, error: `repos[${i}]: bad repo` }, 400);
      const enabled = it.enabled === undefined ? true : it.enabled === true || it.enabled === 1;
      let lib: string | null;
      let libExplicit = false;
      if (it.lib !== undefined) {
        if (typeof it.lib !== "string" || !LIB_RE.test(it.lib)) {
          return c.json({ ok: false, error: `repos[${i}]: lib must match ${LIB_RE.source}` }, 400);
        }
        lib = it.lib;
        libExplicit = true;
      } else {
        lib = defaultLibName(repo);
        if (lib === null) {
          return c.json(
            { ok: false, error: `repos[${i}]: cannot derive a lib name from '${repo}' — provide lib` },
            400,
          );
        }
      }
      items.set(`${owner}/${repo}`, { owner, repo, lib, enabled, libExplicit });
    }

    // Per-user cap: existing keys + genuinely-new keys must stay ≤ REPO_CAP.
    const db = drizzle(c.env.DB);
    const existing = await db
      .select({ owner: userRepos.owner, repo: userRepos.repo })
      .from(userRepos)
      .where(eq(userRepos.userId, user.id));
    const existingKeys = new Set(existing.map((r) => `${r.owner}/${r.repo}`));
    let fresh = 0;
    for (const k of items.keys()) if (!existingKeys.has(k)) fresh += 1;
    if (existingKeys.size + fresh > REPO_CAP) {
      return c.json({ ok: false, error: `repo cap exceeded (max ${REPO_CAP} registered repos)` }, 422);
    }

    // Upsert. An omitted lib must NOT clobber a customized one on an existing
    // row — the derived default applies only on first insert.
    const now = Date.now();
    const upsertWithLib = c.env.DB.prepare(
      "INSERT INTO user_repos (user_id, owner, repo, lib, enabled, created_at) VALUES (?,?,?,?,?,?) " +
        "ON CONFLICT(user_id, owner, repo) DO UPDATE SET enabled = excluded.enabled, lib = excluded.lib",
    );
    const upsertKeepLib = c.env.DB.prepare(
      "INSERT INTO user_repos (user_id, owner, repo, lib, enabled, created_at) VALUES (?,?,?,?,?,?) " +
        "ON CONFLICT(user_id, owner, repo) DO UPDATE SET enabled = excluded.enabled",
    );
    for (const it of items.values()) {
      const stmt = it.libExplicit ? upsertWithLib : upsertKeepLib;
      await stmt.bind(user.id, it.owner, it.repo, it.lib, it.enabled ? 1 : 0, now).run();
    }
    return c.json({ ok: true, repos: await listMine(c, user.id) });
  });

  // ---- DELETE /api/repos — bulk delete [{owner,repo}] ----------------------
  app.delete("/api/repos", async (c) => {
    const bad = checkOrigin(c);
    if (bad) return bad;
    const user = await sessionUser(c);
    if (user instanceof Response) return user;
    const limiter = c.env.REPO_LIMITER ?? c.env.EDIT_LIMITER;
    const rl = await limiter.limit({ key: `repos:${user.id}` });
    if (!rl.success) return c.json({ ok: false, error: "rate limited" }, 429);

    let body: unknown;
    try {
      body = await c.req.json();
    } catch {
      return c.json({ ok: false, error: "bad JSON body" }, 400);
    }
    const raw = parseBulk(body);
    if (typeof raw === "string") return c.json({ ok: false, error: raw }, 400);

    const db = drizzle(c.env.DB);
    let deleted = 0;
    for (let i = 0; i < raw.length; i++) {
      const owner = typeof raw[i].owner === "string" ? (raw[i].owner as string) : "";
      const repo = typeof raw[i].repo === "string" ? (raw[i].repo as string) : "";
      if (!REPO_NAME_RE.test(owner) || !REPO_NAME_RE.test(repo)) {
        return c.json({ ok: false, error: `repos[${i}]: bad owner/repo` }, 400);
      }
      const r = await db
        .delete(userRepos)
        .where(and(eq(userRepos.userId, user.id), eq(userRepos.owner, owner), eq(userRepos.repo, repo)));
      deleted += (r as unknown as { meta?: { changes?: number } }).meta?.changes ?? 0;
    }
    return c.json({ ok: true, deleted, repos: await listMine(c, user.id) });
  });

  // ---- GET /api/repos/enabled — the nightly's feed (UNAUTHENTICATED) -------
  // PINNED CONTRACT (see the module header): {"repos":[{owner,repo,lib}]},
  // enabled-only, distinct (a repo enabled by ≥2 users appears once — the
  // lexicographically-least lib wins deterministically), stable order, ≤500.
  app.get("/api/repos/enabled", async (c) => {
    const res = await c.env.DB.prepare(
      "SELECT owner, repo, MIN(lib) AS lib FROM user_repos WHERE enabled = 1 " +
        "GROUP BY owner, repo ORDER BY owner, repo LIMIT 500",
    ).all();
    const rows = (res.results ?? []) as Array<{ owner: string; repo: string; lib: string }>;
    // Defense in depth: re-validate on read-back so a row landing outside the
    // POST path can never reach the harvester.
    const valid = rows.filter(
      (r) =>
        REPO_NAME_RE.test(r.owner) && !r.owner.startsWith(".") &&
        REPO_NAME_RE.test(r.repo) && LIB_RE.test(r.lib),
    );
    return c.json(
      { repos: valid.map((r) => ({ owner: r.owner, repo: r.repo, lib: r.lib })) },
      200,
      { "Cache-Control": "no-store" },
    );
  });

  // ---- GET /repos — the registration page ----------------------------------
  app.get("/repos", async (c) => {
    const user = await getUser(c);
    if (!user) return c.redirect("/login?returnTo=%2Frepos");
    c.header("Cache-Control", "no-store"); // per-user page — never cache
    return c.html(reposPageHtml(user.name));
  });
}

// Warm academic-minimalist page matching the pages.ts shell (kept local — the
// shell helper isn't exported). Data loads client-side from /api/repos +
// /api/repos/github; rendering uses DOM builders (never innerHTML with GitHub
// strings) so repo descriptions can't inject markup.
function reposPageHtml(userName: string): string {
  const globals = GLOBAL_SOURCES.map(
    (g) =>
      `<tr><td><input type="checkbox" checked disabled aria-label="always on"></td>` +
      `<td><a href="https://github.com/${g.owner}/${g.repo}" target="_blank" rel="noopener">${g.owner}/${g.repo}</a></td>` +
      `<td><code>${g.lib}</code></td><td class="muted">always on</td></tr>`,
  ).join("");
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WikiLean · Repo sources</title>
<meta name="robots" content="noindex">
<script>(function(){try{var s=localStorage.getItem("wl-theme");var t=s==="dark"||s==="light"?s:(window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light");document.documentElement.dataset.theme=t;}catch(e){}})();</script>
<style>
*{box-sizing:border-box}
body{margin:0;background:#f7f4ee;color:#1f1d1a;line-height:1.55;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
:focus-visible{outline:2px solid #1a4b8c;outline-offset:2px}
.wl-header{display:flex;align-items:baseline;justify-content:space-between;gap:8px 20px;flex-wrap:wrap;max-width:900px;margin:0 auto;padding:20px 24px 0}
.wl-brand{font-family:Charter,'Bitstream Charter','Iowan Old Style',Georgia,'Times New Roman',serif;font-weight:700;font-size:1.1rem;color:#1f1d1a;text-decoration:none}
.wl-brand:hover{color:#1a4b8c}
.wl-navlink{color:#1a4b8c;text-decoration:none;font-size:.88rem}
.wl-navlink:hover{text-decoration:underline}
.wrap{max-width:900px;margin:0 auto;padding:26px 24px 64px}
h1{font-family:Charter,'Bitstream Charter','Iowan Old Style',Georgia,'Times New Roman',serif;font-size:1.6rem;margin:0 0 .35rem}
h2{font-family:Charter,'Bitstream Charter','Iowan Old Style',Georgia,'Times New Roman',serif;font-size:1.15rem;margin:28px 0 10px}
.lead{color:#5f594e;margin:0 0 20px}
table{border-collapse:collapse;width:100%;background:#fffdf9;border:1px solid #d8d0bd;border-radius:8px;overflow:hidden}
th,td{border-bottom:1px solid #ece6d8;padding:8px 12px;text-align:left;font-size:.9rem;vertical-align:top}
th{background:#f2eee3;text-transform:uppercase;letter-spacing:.05em;font-size:.72rem;color:#5f594e}
tr:last-child td{border-bottom:none}
a{color:#1a4b8c;text-decoration:none}
a:hover{text-decoration:underline}
.muted{color:#6e675a}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.85em;background:#f3efe6;padding:.05rem .3rem;border-radius:4px}
tr.lean td{background:rgba(47,125,79,.06)}
.wl-lang{display:inline-block;padding:1px 8px;border-radius:10px;font-size:.7rem;font-weight:600;background:#ece6d8;color:#5f594e;white-space:nowrap}
.wl-lang.lean{background:rgba(47,125,79,.12);color:#2f7d4f}
.wl-save{font:inherit;font-size:.9rem;font-weight:600;padding:8px 18px;border:1px solid #1a4b8c;border-radius:8px;background:#1a4b8c;color:#fff;cursor:pointer;margin:16px 0 0}
.wl-save:hover{background:#163e74;border-color:#163e74}
.wl-save:disabled{opacity:.55;cursor:default}
.wl-status{margin-left:12px;font-size:.88rem;color:#5f594e}
.wl-status.err{color:#9c2f28}
.wl-theme-toggle{background:transparent;border:1px solid #d8d0bd;color:#5f594e;border-radius:50%;width:28px;height:28px;padding:0;line-height:1;font-size:14px;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;margin-left:10px}
.wl-theme-toggle:hover{color:#1f1d1a;border-color:#1a4b8c}
[data-theme="dark"] body{background:#1a1816;color:#ebe5d8}
[data-theme="dark"] :focus-visible{outline-color:#6e9adf}
[data-theme="dark"] .wl-brand{color:#ebe5d8}
[data-theme="dark"] .wl-brand:hover{color:#6e9adf}
[data-theme="dark"] .wl-navlink{color:#6e9adf}
[data-theme="dark"] .lead{color:#9a9081}
[data-theme="dark"] table{background:#232020;border-color:#4d4742}
[data-theme="dark"] th,[data-theme="dark"] td{border-bottom-color:#3a3530}
[data-theme="dark"] th{background:#2a2725;color:#9a9081}
[data-theme="dark"] a{color:#6e9adf}
[data-theme="dark"] .muted{color:#8a8278}
[data-theme="dark"] code{background:#2e2a2f}
[data-theme="dark"] tr.lean td{background:rgba(76,169,122,.08)}
[data-theme="dark"] .wl-lang{background:#2e2a2f;color:#9a9081}
[data-theme="dark"] .wl-lang.lean{background:rgba(76,169,122,.18);color:#8fd4ad}
[data-theme="dark"] .wl-save{background:#6e9adf;border-color:#6e9adf;color:#1a1816}
[data-theme="dark"] .wl-save:hover{background:#8fb4e8;border-color:#8fb4e8}
[data-theme="dark"] .wl-status{color:#9a9081}
[data-theme="dark"] .wl-status.err{color:#f08e85}
[data-theme="dark"] .wl-theme-toggle{color:#9a9081;border-color:#4d4742}
[data-theme="dark"] .wl-theme-toggle:hover{color:#ebe5d8;border-color:#6e9adf}
</style>
</head>
<body>
<header class="wl-header"><a class="wl-brand" href="/">WikiLean</a><span><a class="wl-navlink" href="/recent-changes">Recent changes</a> · <a class="wl-navlink" href="/stats">Stats</a><button id="wl-theme-toggle" class="wl-theme-toggle" type="button" aria-label="Toggle dark mode" title="Toggle dark mode">🌓</button></span></header>
<div class="wrap">
<h1>Repo sources</h1>
<p class="lead">Signed in as <b>${htmlEscape(userName)}</b>. Pick which of your public GitHub
repos WikiLean's nightly indexer should read as Lean sources. Checked = indexed
(up to ${REPO_CAP}); Lean repos are highlighted.</p>

<h2>Global sources</h2>
<table><thead><tr><th></th><th>Repository</th><th>Lean library</th><th></th></tr></thead>
<tbody>${globals}</tbody></table>

<h2>Your GitHub repos</h2>
<div id="gh-area"><p class="muted">Loading your public repos…</p></div>

<div id="extra-area"></div>

<p><button id="save" class="wl-save" type="button" disabled>Save</button><span id="status" class="wl-status"></span></p>
</div>
<script>(function(){var b=document.getElementById("wl-theme-toggle");if(!b)return;b.addEventListener("click",function(){var r=document.documentElement;var n=r.dataset.theme==="dark"?"light":"dark";r.dataset.theme=n;try{localStorage.setItem("wl-theme",n);}catch(e){}});})();</script>
<script>
(function(){
"use strict";
var registered = {};        // "owner/repo" -> {owner,repo,lib,enabled}
var ghRepos = [];
var saveBtn = document.getElementById("save");
var statusEl = document.getElementById("status");

function key(o, r){ return o + "/" + r; }
function el(tag, cls, text){
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;  // textContent only — GitHub strings never hit innerHTML
  return e;
}
function setStatus(msg, isErr){
  statusEl.textContent = msg || "";
  statusEl.className = "wl-status" + (isErr ? " err" : "");
}

function checkboxRow(o, r, opts){
  var tr = el("tr", opts.lean ? "lean" : "");
  var td0 = el("td");
  var cb = el("input");
  cb.type = "checkbox";
  cb.checked = !!opts.checked;
  cb.dataset.owner = o;
  cb.dataset.repo = r;
  cb.setAttribute("aria-label", "index " + key(o, r));
  td0.appendChild(cb);
  tr.appendChild(td0);
  var td1 = el("td");
  var a = el("a", "", key(o, r));
  a.href = "https://github.com/" + encodeURIComponent(o) + "/" + encodeURIComponent(r);
  a.target = "_blank"; a.rel = "noopener";
  td1.appendChild(a);
  if (opts.description){ td1.appendChild(el("div", "muted", opts.description)); }
  tr.appendChild(td1);
  var td2 = el("td");
  if (opts.language){ td2.appendChild(el("span", "wl-lang" + (opts.lean ? " lean" : ""), opts.language)); }
  tr.appendChild(td2);
  var td3 = el("td");
  if (opts.lib){ td3.appendChild(el("code", "", opts.lib)); }
  tr.appendChild(td3);
  return tr;
}

function repoTable(rows){
  var table = el("table");
  var thead = el("thead"), htr = el("tr");
  ["", "Repository", "Language", "Lean library"].forEach(function(h){ htr.appendChild(el("th", "", h)); });
  thead.appendChild(htr); table.appendChild(thead);
  var tbody = el("tbody");
  rows.forEach(function(r){ tbody.appendChild(r); });
  table.appendChild(tbody);
  return table;
}

function render(){
  var ghArea = document.getElementById("gh-area");
  ghArea.textContent = "";
  var listed = {};
  if (ghRepos.length){
    // Lean repos first (pre-highlighted), then the rest in GitHub's
    // recently-updated order.
    var sorted = ghRepos.slice().sort(function(a, b){
      return (b.language === "Lean" ? 1 : 0) - (a.language === "Lean" ? 1 : 0);
    });
    var rows = sorted.map(function(r){
      listed[key(r.owner, r.name)] = true;
      var reg = registered[key(r.owner, r.name)];
      return checkboxRow(r.owner, r.name, {
        checked: !!(reg && reg.enabled),
        lean: r.language === "Lean",
        language: r.language,
        description: r.description,
        lib: reg ? reg.lib : null
      });
    });
    ghArea.appendChild(repoTable(rows));
  } else {
    ghArea.appendChild(el("p", "muted", "No public repos found."));
  }
  // Registered rows the GitHub listing doesn't show (e.g. added via the API).
  var extraArea = document.getElementById("extra-area");
  extraArea.textContent = "";
  var extras = Object.keys(registered).filter(function(k){ return !listed[k]; });
  if (extras.length){
    extraArea.appendChild(el("h2", "", "Other registered repos"));
    extraArea.appendChild(repoTable(extras.map(function(k){
      var reg = registered[k];
      return checkboxRow(reg.owner, reg.repo, { checked: reg.enabled, lib: reg.lib });
    })));
  }
  saveBtn.disabled = false;
}

function save(){
  saveBtn.disabled = true;
  setStatus("saving…");
  var checked = {}, toPost = [];
  document.querySelectorAll('input[type="checkbox"][data-owner]').forEach(function(cb){
    var k = key(cb.dataset.owner, cb.dataset.repo);
    if (cb.checked){
      checked[k] = true;
      toPost.push({ owner: cb.dataset.owner, repo: cb.dataset.repo, enabled: true });
    }
  });
  // Unchecking a registered repo DISABLES it (enabled:false) rather than
  // deleting the row — a custom lib set via the API survives the round-trip.
  Object.keys(registered).filter(function(k){ return !checked[k]; })
    .forEach(function(k){
      var reg = registered[k];
      toPost.push({ owner: reg.owner, repo: reg.repo, enabled: false });
    });
  var steps = Promise.resolve();
  if (toPost.length){
    steps = steps.then(function(){
      return fetch("/api/repos", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repos: toPost }) }).then(function(r){ return r.json(); })
        .then(function(j){ if (!j.ok) throw new Error(j.error || "save failed"); });
    });
  }
  steps.then(function(){
    return fetch("/api/repos").then(function(r){ return r.json(); }).then(function(j){
      registered = {};
      (j.repos || []).forEach(function(r){ registered[key(r.owner, r.repo)] = r; });
      render();
      setStatus("saved ✓");
    });
  }).catch(function(e){
    setStatus(String(e && e.message || e), true);
    saveBtn.disabled = false;
  });
}
saveBtn.addEventListener("click", save);

Promise.all([
  fetch("/api/repos").then(function(r){ return r.json(); }),
  fetch("/api/repos/github").then(function(r){ return r.json(); }).catch(function(){ return { ok: false, error: "network error" }; })
]).then(function(res){
  var mine = res[0], gh = res[1];
  (mine.repos || []).forEach(function(r){ registered[key(r.owner, r.repo)] = r; });
  if (gh.ok){ ghRepos = gh.repos || []; }
  render();
  if (!gh.ok){
    var ghArea = document.getElementById("gh-area");
    ghArea.textContent = "";
    ghArea.appendChild(el("p", "muted", "Could not list your GitHub repos: " + (gh.error || "unknown error")));
  }
}).catch(function(e){ setStatus(String(e && e.message || e), true); });
})();
</script>
</body>
</html>`;
}
