// WikiLean @[wikidata] review tool.
//
// A logged-in reviewer pastes a PR (owner/repo/number); the page fetches the
// PR's `@[wikidata Q…]` tags + any existing inline review comments from GitHub
// (via the Worker, server-side) and renders each tagged declaration with the
// comments underneath and a decision/notes form. Submitting posts the decision
// as inline review comments on GitHub.
//
// This module is DETERMINISTIC end to end — plain fetch/parse/post, no LLM.
//
// Increment 1 (this file): read-only — `GET /review` page + `GET /api/review/...`
// JSON (diff tags + existing comments). Posting (`POST /api/review/...`) and the
// OAuth `public_repo` scope land in increment 2.

import type { Context, Hono } from "hono";
import type { Env } from "./env.js";
import { getUser } from "./auth.js";

const GH_API = "https://api.github.com";
const UA = "WikiLean-review/0.1 (+https://wikilean.jackmccarthy.org)";

type Ctx = Context<{ Bindings: Env }>;

// ---- GitHub fetch helpers (server-side; optional token for rate limits) ----

function ghHeaders(token?: string, accept = "application/vnd.github+json"): HeadersInit {
  const h: Record<string, string> = { "User-Agent": UA, Accept: accept };
  if (token) h.Authorization = `Bearer ${token}`;
  return h;
}

async function ghJson<T>(url: string, token?: string): Promise<T> {
  const r = await fetch(url, { headers: ghHeaders(token) });
  if (!r.ok) throw new Error(`GitHub ${r.status} for ${url}: ${(await r.text()).slice(0, 200)}`);
  return r.json() as Promise<T>;
}

// Paginate a GitHub list endpoint (follows rel="next" Link headers).
async function ghPaginate<T>(url: string, token?: string): Promise<T[]> {
  const out: T[] = [];
  let next: string | null = `${url}${url.includes("?") ? "&" : "?"}per_page=100`;
  while (next !== null) {
    const r: Response = await fetch(next, { headers: ghHeaders(token) });
    if (!r.ok) throw new Error(`GitHub ${r.status} for ${next}`);
    out.push(...((await r.json()) as T[]));
    const link: string = r.headers.get("Link") || "";
    const m: RegExpMatchArray | null = link.match(/<([^>]+)>;\s*rel="next"/);
    next = m ? m[1] : null;
  }
  return out;
}

// ---- diff parsing: find every +@[wikidata Q…] line + its hunk context ----

const WIKIDATA_RE = /wikidata\s+(Q\d+)/;

export interface DeclTag {
  qid: string;
  file: string;
  line: number; // 1-based new-file line of the attribute
  hunk: string[]; // a few source lines starting at the tag (for display)
}

// Parse a unified diff: track the current file (+++ b/…) and new-file line
// counter (from @@ -a,b +c,d @@), and for each added line containing
// `wikidata Q…` record (qid, file, line) plus the next few added/context lines
// as a display hunk.
export function parseWikidataTags(diff: string): DeclTag[] {
  const tags: DeclTag[] = [];
  let file = "";
  let newLine = 0;
  const lines = diff.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const ln = lines[i];
    if (ln.startsWith("+++ b/")) {
      file = ln.slice(6);
      continue;
    }
    if (ln.startsWith("+++ ")) {
      file = ln.slice(4).replace(/^b\//, "");
      continue;
    }
    if (ln.startsWith("@@")) {
      const m = ln.match(/\+(\d+)/);
      newLine = m ? parseInt(m[1], 10) : newLine;
      continue;
    }
    if (ln.startsWith("diff --git") || ln.startsWith("index ") || ln.startsWith("--- ")) continue;
    // Hunk body lines: "+" added, " " context (both advance the new-file line),
    // "-" removed (does NOT advance the new-file counter).
    if (ln.startsWith("+") && !ln.startsWith("++")) {
      const content = ln.slice(1);
      const m = content.match(WIKIDATA_RE);
      if (m) {
        // Display hunk: this line + up to 7 following added/context lines.
        const hunk: string[] = [content];
        for (let j = i + 1; j < lines.length && hunk.length < 8; j++) {
          const nx = lines[j];
          if (nx.startsWith("+") && !nx.startsWith("++")) hunk.push(nx.slice(1));
          else if (nx.startsWith(" ")) hunk.push(nx.slice(1));
          else break;
        }
        tags.push({ qid: m[1], file, line: newLine, hunk });
      }
      newLine++;
    } else if (ln.startsWith(" ")) {
      newLine++;
    }
    // "-" lines and others: no new-line advance.
  }
  return tags;
}

// ---- existing inline review comments, grouped by path:line ----

interface GhReviewComment {
  id: number;
  path: string;
  line: number | null;
  original_line: number | null;
  body: string;
  user: { login: string } | null;
  html_url: string;
  created_at: string;
}

export interface ReviewComment {
  id: number;
  user: string;
  body: string;
  bodyHtml: string | null; // GitHub-rendered (sanitized) markdown, or null on fallback
  html_url: string;
  created_at: string;
}

async function sha256Hex(s: string): Promise<string> {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return Array.from(new Uint8Array(d), (b) => b.toString(16).padStart(2, "0")).join("");
}

// Render a comment body to HTML via GitHub's own /markdown (GFM) endpoint, so
// it matches GitHub exactly AND is sanitized by GitHub (safe to inject — public
// PR comments are untrusted). Cached in KV by content hash; null on failure
// (caller falls back to escaped plaintext).
async function ghRenderMarkdown(
  text: string,
  repoFull: string,
  token: string | undefined,
  env: Env,
): Promise<string | null> {
  try {
    const key = "md:" + (await sha256Hex(repoFull + "\n" + text));
    const cached = await env.RENDER_CACHE.get(key);
    if (cached !== null) return cached;
    const r = await fetch(`${GH_API}/markdown`, {
      method: "POST",
      headers: { ...ghHeaders(token), "Content-Type": "application/json" },
      body: JSON.stringify({ text, mode: "gfm", context: repoFull }),
    });
    if (!r.ok) return null;
    const html = await r.text();
    await env.RENDER_CACHE.put(key, html, { expirationTtl: 60 * 60 * 24 * 7 });
    return html;
  } catch {
    return null;
  }
}

function keyOf(path: string, line: number): string {
  return `${path}:${line}`;
}

// ---- Wikidata label/description/enwiki + Wikipedia lead (cached) ----

export interface WdInfo {
  label: string | null;
  description: string | null;
  enwikiUrl: string | null;
  enwikiTitle: string | null;
  lead: string | null;
}

const WD_API = "https://www.wikidata.org/w/api.php";
const EN_API = "https://en.wikipedia.org/w/api.php";

// Batched wbgetentities (50 ids/call) → label, description, enwiki sitelink.
// Per-qid KV cache (7d).
async function fetchWikidata(qids: string[], env: Env): Promise<Map<string, WdInfo>> {
  const out = new Map<string, WdInfo>();
  const missing: string[] = [];
  for (const q of qids) {
    const c = await env.RENDER_CACHE.get(`wd:${q}`);
    if (c !== null) out.set(q, JSON.parse(c) as WdInfo);
    else missing.push(q);
  }
  for (let i = 0; i < missing.length; i += 50) {
    const chunk = missing.slice(i, i + 50);
    const url =
      `${WD_API}?action=wbgetentities&ids=${chunk.join("|")}` +
      `&props=labels|descriptions|sitelinks/urls&languages=en&sitefilter=enwiki&format=json&origin=*`;
    let data: any;
    try {
      const r = await fetch(url, { headers: { "User-Agent": UA } });
      if (!r.ok) continue;
      data = await r.json();
    } catch {
      continue;
    }
    for (const q of chunk) {
      const e = data?.entities?.[q] ?? {};
      const sl = e?.sitelinks?.enwiki ?? {};
      const info: WdInfo = {
        label: e?.labels?.en?.value ?? null,
        description: e?.descriptions?.en?.value ?? null,
        enwikiUrl: sl?.url ?? null,
        enwikiTitle: sl?.title ?? null,
        lead: null,
      };
      out.set(q, info);
      await env.RENDER_CACHE.put(`wd:${q}`, JSON.stringify(info), { expirationTtl: 60 * 60 * 24 * 7 });
    }
  }
  return out;
}

// Batched Wikipedia lead extracts (20 titles/call), per-title KV cache (7d).
async function fetchLeads(titles: string[], env: Env): Promise<Map<string, string>> {
  const out = new Map<string, string>();
  const missing: string[] = [];
  for (const t of titles) {
    const c = await env.RENDER_CACHE.get(`wplead:${t}`);
    if (c !== null) out.set(t, c);
    else missing.push(t);
  }
  for (let i = 0; i < missing.length; i += 20) {
    const chunk = missing.slice(i, i + 20);
    const url =
      `${EN_API}?action=query&prop=extracts&exintro=1&explaintext=1&redirects=1` +
      `&titles=${chunk.map(encodeURIComponent).join("|")}&format=json&formatversion=2&origin=*`;
    let data: any;
    try {
      const r = await fetch(url, { headers: { "User-Agent": UA } });
      if (!r.ok) continue;
      data = await r.json();
    } catch {
      continue;
    }
    const norm: Record<string, string> = {};
    for (const n of data?.query?.normalized ?? []) norm[n.from] = n.to;
    const redir: Record<string, string> = {};
    for (const n of data?.query?.redirects ?? []) redir[n.from] = n.to;
    const byTitle: Record<string, string> = {};
    for (const p of data?.query?.pages ?? []) byTitle[p.title] = p.extract ?? "";
    for (const t of chunk) {
      const canon = redir[norm[t] ?? t] ?? norm[t] ?? t;
      const lead = byTitle[canon] ?? "";
      out.set(t, lead);
      await env.RENDER_CACHE.put(`wplead:${t}`, lead, { expirationTtl: 60 * 60 * 24 * 7 });
    }
  }
  return out;
}

// ---- full decl-body extraction from the file at the PR head (cached) ----

const TOPLEVEL_PREFIXES = [
  "def ", "theorem ", "lemma ", "class ", "structure ", "inductive ", "abbrev ",
  "instance ", "instance:", "example ", "axiom ", "constant ", "opaque ",
  "noncomputable ", "protected ", "private ", "public ", "nonrec ", "mutual ",
  "@[", "attribute ", "/--", "/-!", "/-", "namespace ", "end ", "section",
  "variable ", "open ", "export ", "import ", "notation", "scoped ", "local ",
  "infix", "prefix", "postfix", "syntax", "macro", "elab", "add_decl_doc",
  "initialize ", "builtin_", "#", "--",
];
const BARE_MODIFIERS = new Set(["noncomputable", "private", "protected", "public", "nonrec", "unsafe", "partial"]);

function isTopLevel(line: string): boolean {
  if (!line || line[0] === " " || line[0] === "\t") return false;
  if (line.startsWith("deriving ")) return false;
  return TOPLEVEL_PREFIXES.some((p) => line.startsWith(p));
}

// Given file lines and a qid, return the full declaration slice (docstring +
// attributes + modifiers + signature + body) around its @[wikidata Qxxx] tag.
export function extractDeclBody(lines: string[], qid: string): string | null {
  const pat = new RegExp(`wikidata\\s+${qid}\\b`);
  const idx = lines.findIndex((l) => pat.test(l));
  if (idx < 0) return null;
  // Forward: skip the attribute block + bare-modifier lines to the signature,
  // then walk to the next top-level construct.
  let sig = idx + 1;
  while (sig < lines.length && (lines[sig].trimStart().startsWith("@[") || BARE_MODIFIERS.has(lines[sig].trim()))) sig++;
  let end = sig + 1;
  while (end < lines.length && !isTopLevel(lines[end])) end++;
  while (end > sig + 1 && lines[end - 1].trim() === "") end--;
  // Backward: include the attribute block, interleaved `--` comments, the
  // docstring, and a `variable … in` clause.
  let start = idx;
  while (start > 0) {
    const prev = lines[start - 1].trimStart();
    if (prev.startsWith("@[") || prev.startsWith("-- ") || prev === "--") start--;
    else break;
  }
  if (start > 0 && lines[start - 1].trimEnd().endsWith("-/")) {
    if (lines[start - 1].includes("/--")) start--;
    else {
      let j = start - 1;
      while (j >= 0 && !lines[j].includes("/--")) j--;
      if (j >= 0) start = j;
    }
  }
  if (start > 0 && /^variable\s.*\sin\s*$/.test(lines[start - 1].trimEnd())) start--;
  return lines.slice(start, end).join("\n");
}

// Fetch a file's text at a commit (raw contents API), KV-cached by sha+path.
async function fetchFileLines(
  repoFull: string,
  path: string,
  sha: string,
  token: string | undefined,
  env: Env,
): Promise<string[] | null> {
  const key = `ghfile:${sha}:${path}`;
  const cached = await env.RENDER_CACHE.get(key);
  if (cached !== null) return cached.split("\n");
  try {
    const r = await fetch(
      `${GH_API}/repos/${repoFull}/contents/${path.split("/").map(encodeURIComponent).join("/")}?ref=${sha}`,
      { headers: ghHeaders(token, "application/vnd.github.raw") },
    );
    if (!r.ok) return null;
    const text = await r.text();
    await env.RENDER_CACHE.put(key, text, { expirationTtl: 60 * 60 * 24 * 30 });
    return text.split("\n");
  } catch {
    return null;
  }
}

// ---- the assembled review payload returned to the browser ----

export interface ReviewPayload {
  repo: string;
  pr: number;
  head_sha: string;
  title: string;
  decls: Array<DeclTag & { comments: ReviewComment[]; source: string; wd: WdInfo | null }>;
}

async function buildReviewPayload(
  owner: string,
  repo: string,
  pr: number,
  token?: string,
  env?: Env,
  renderMarkdown = false,
): Promise<ReviewPayload> {
  const full = `${owner}/${repo}`;
  const meta = await ghJson<{ head: { sha: string }; title: string }>(
    `${GH_API}/repos/${full}/pulls/${pr}`,
    token,
  );
  const diffResp = await fetch(`${GH_API}/repos/${full}/pulls/${pr}`, {
    headers: ghHeaders(token, "application/vnd.github.diff"),
  });
  if (!diffResp.ok) throw new Error(`GitHub diff ${diffResp.status}`);
  const diff = await diffResp.text();
  const tags = parseWikidataTags(diff);

  const comments = await ghPaginate<GhReviewComment>(
    `${GH_API}/repos/${full}/pulls/${pr}/comments`,
    token,
  );
  // Render bodies to HTML in parallel (cached). Only for the page read path.
  const htmlById = new Map<number, string>();
  if (renderMarkdown && env) {
    const rendered = await Promise.all(
      comments.map(async (c) => [c.id, await ghRenderMarkdown(c.body, full, token, env)] as const),
    );
    for (const [id, html] of rendered) if (html !== null) htmlById.set(id, html);
  }
  const byLine = new Map<string, ReviewComment[]>();
  for (const c of comments) {
    const ln = c.line ?? c.original_line;
    if (ln == null) continue;
    const k = keyOf(c.path, ln);
    if (!byLine.has(k)) byLine.set(k, []);
    byLine.get(k)!.push({
      id: c.id,
      user: c.user?.login ?? "unknown",
      body: c.body,
      bodyHtml: htmlById.get(c.id) ?? null,
      html_url: c.html_url,
      created_at: c.created_at,
    });
  }

  // Page mode (renderMarkdown): also fetch the full decl body per tag and the
  // Wikidata description + Wikipedia lead per qid. Skipped for the POST path.
  const sourceByQid = new Map<string, string>();
  let wdByQid = new Map<string, WdInfo>();
  if (renderMarkdown && env) {
    // Full bodies: one file fetch per unique file (cached), reused across tags.
    const fileCache = new Map<string, string[] | null>();
    for (const t of tags) {
      let lines = fileCache.get(t.file);
      if (lines === undefined) {
        lines = await fetchFileLines(full, t.file, meta.head.sha, token, env);
        fileCache.set(t.file, lines);
      }
      const body = lines ? extractDeclBody(lines, t.qid) : null;
      if (body) sourceByQid.set(t.qid, body);
    }
    // Wikidata + Wikipedia leads.
    const qids = [...new Set(tags.map((t) => t.qid))];
    wdByQid = await fetchWikidata(qids, env);
    const titles = [...wdByQid.values()].map((w) => w.enwikiTitle).filter((x): x is string => !!x);
    const leads = await fetchLeads(titles, env);
    for (const [, w] of wdByQid) if (w.enwikiTitle) w.lead = leads.get(w.enwikiTitle) ?? null;
  }

  return {
    repo: full,
    pr,
    head_sha: meta.head.sha,
    title: meta.title,
    decls: tags.map((t) => ({
      ...t,
      comments: byLine.get(keyOf(t.file, t.line)) ?? [],
      source: sourceByQid.get(t.qid) ?? t.hunk.join("\n"),
      wd: wdByQid.get(t.qid) ?? null,
    })),
  };
}

// ---- routes -------------------------------------------------------------

const OWNER_RE = /^[A-Za-z0-9_.-]+$/;

export function registerReviewRoutes(app: Hono<{ Bindings: Env }>): void {
  // JSON: PR tags + existing comments. Public PRs need no auth; if the reviewer
  // is logged in with a stored GitHub token we use it (5000/hr vs 60/hr).
  app.get("/api/review/:owner/:repo/:pr", async (c) => {
    const owner = c.req.param("owner");
    const repo = c.req.param("repo");
    const pr = parseInt(c.req.param("pr"), 10);
    if (!OWNER_RE.test(owner) || !OWNER_RE.test(repo) || !Number.isInteger(pr) || pr <= 0) {
      return c.json({ ok: false, error: "bad owner/repo/pr" }, 400);
    }
    const { token } = await githubAccountFor(c);
    try {
      const payload = await buildReviewPayload(owner, repo, pr, token, c.env, true);
      return c.json({ ok: true, ...payload });
    } catch (e) {
      return c.json({ ok: false, error: String(e instanceof Error ? e.message : e) }, 502);
    }
  });

  // Post the reviewer's decisions/notes as inline PR comments, as the logged-in
  // user, via their stored GitHub token. Verbatim notes, never LLM-authored.
  app.post("/api/review/:owner/:repo/:pr", async (c) => {
    // CSRF defense-in-depth: reject cross-origin browser POSTs.
    const origin = c.req.header("Origin");
    if (origin && origin !== new URL(c.req.url).origin) {
      return c.json({ ok: false, error: "cross-origin request rejected" }, 403);
    }
    const user = await getUser(c);
    if (!user) return c.json({ ok: false, error: "login required" }, 401);
    const owner = c.req.param("owner");
    const repo = c.req.param("repo");
    const pr = parseInt(c.req.param("pr"), 10);
    if (!OWNER_RE.test(owner) || !OWNER_RE.test(repo) || !Number.isInteger(pr) || pr <= 0) {
      return c.json({ ok: false, error: "bad owner/repo/pr" }, 400);
    }
    const { token, scope } = await githubAccountFor(c);
    if (!token) {
      return c.json({ ok: false, error: "no linked GitHub account — sign in with GitHub" }, 403);
    }
    if (!/(^|[\s,])(public_repo|repo)([\s,]|$)/.test(scope ?? "")) {
      return c.json(
        { ok: false, error: "GitHub token lacks comment permission — sign out and back in to grant it" },
        403,
      );
    }

    let body: { decisions?: Record<string, { status?: string; notes?: string }> };
    try {
      body = await c.req.json();
    } catch {
      return c.json({ ok: false, error: "bad json" }, 400);
    }
    const decisions = body.decisions ?? {};

    // Resolve qid → (file, line) + head sha + already-posted markers from the PR.
    let payload: ReviewPayload;
    try {
      payload = await buildReviewPayload(owner, repo, pr, token);
    } catch (e) {
      return c.json({ ok: false, error: String(e instanceof Error ? e.message : e) }, 502);
    }
    const tagByQid = new Map(payload.decls.map((d) => [d.qid, d]));
    const alreadyPosted = new Set<string>();
    for (const d of payload.decls) {
      for (const cm of d.comments) {
        const m = cm.body.match(/wikilean-review:(Q\d+)/);
        if (m) alreadyPosted.add(m[1]);
      }
    }

    const results: Array<{ qid: string; posted: boolean; skipped?: string; error?: string }> = [];
    for (const [qid, dec] of Object.entries(decisions)) {
      const status = (dec.status ?? "").trim();
      const notes = (dec.notes ?? "").trim();
      if (!status && !notes) continue; // pending — nothing to post
      if (alreadyPosted.has(qid)) {
        results.push({ qid, posted: false, skipped: "already has a review comment" });
        continue;
      }
      const tag = tagByQid.get(qid);
      if (!tag) {
        results.push({ qid, posted: false, error: "tag not present in this PR" });
        continue;
      }
      const commentBody = buildReviewCommentBody(qid, status, notes);
      const r = await fetch(`${GH_API}/repos/${owner}/${repo}/pulls/${pr}/comments`, {
        method: "POST",
        headers: { ...ghHeaders(token), "Content-Type": "application/json" },
        body: JSON.stringify({
          body: commentBody,
          commit_id: payload.head_sha,
          path: tag.file,
          line: tag.line,
          side: "RIGHT",
        }),
      });
      if (r.ok) {
        results.push({ qid, posted: true });
      } else {
        results.push({ qid, posted: false, error: `GitHub ${r.status}: ${(await r.text()).slice(0, 120)}` });
      }
    }
    const posted = results.filter((x) => x.posted).length;
    return c.json({ ok: true, posted, results });
  });

  // The review page (shell + client script).
  app.get("/review", (c) => c.html(reviewPageHtml()));
}

const EMOJI: Record<string, string> = { approve: "🟢", revise: "🟡", reject: "🔴" };

// Build the inline-comment body: traffic-light label + the reviewer's VERBATIM
// note (blockquoted), plus the idempotency marker. Identical shape to the CLI
// poster (post_review_comments.py), so both tools interoperate via the marker.
export function buildReviewCommentBody(qid: string, status: string, notes: string): string {
  const em = EMOJI[status] ?? "";
  const label = status ? `${em} WikiLean reviewer note (${status})`.trim() : "WikiLean reviewer note";
  const quoted = notes ? notes.split("\n").map((l) => "> " + l).join("\n") : "_(no note)_";
  return (
    `**${label}**\n\n${quoted}\n\n` +
    `<sub><a href="https://www.wikidata.org/wiki/${qid}">${qid}</a> ` +
    `<!-- wikilean-review:${qid} --></sub>`
  );
}

// Read the logged-in user's stored GitHub OAuth token + granted scope
// (better-auth `accounts`). Empty object if not logged in / no github account.
async function githubAccountFor(c: Ctx): Promise<{ token?: string; scope?: string }> {
  const user = await getUser(c);
  if (!user) return {};
  // Lazy import to keep this module's top-level deps light.
  const { drizzle } = await import("drizzle-orm/d1");
  const { eq, and } = await import("drizzle-orm");
  const { accounts } = await import("./db/schema.js");
  const db = drizzle(c.env.DB);
  const row = (
    await db
      .select({ token: accounts.accessToken, scope: accounts.scope })
      .from(accounts)
      .where(and(eq(accounts.userId, user.id), eq(accounts.providerId, "github")))
      .limit(1)
  )[0];
  return { token: row?.token ?? undefined, scope: row?.scope ?? undefined };
}

// ---- page HTML ----------------------------------------------------------

function reviewPageHtml(): string {
  // Self-contained: warm palette to match the site; vanilla JS, no deps.
  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WikiLean · @[wikidata] review</title>
<style>
:root{--bg:#faf7f1;--card:#fffdf9;--rule:#e3dccb;--ink:#1f1d1a;--muted:#6b6457;--accent:#7a3d2a;--code:#f3efe6;
      --g:#2d7a4a;--y:#b77a14;--r:#a02828;--gb:#e8f4ec;--yb:#fbf3e0;--rb:#fbe8e8;}
*{box-sizing:border-box}
body{font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;color:var(--ink);background:var(--bg);margin:0;padding:1.5rem 1rem 5rem}
.wrap{max-width:1100px;margin:0 auto}
h1{font-size:1.4rem;margin:0 0 .3rem}
.lede{color:var(--muted);margin:0 0 1.3rem}
form.load{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;margin:0 0 1.5rem;background:var(--card);border:1px solid var(--rule);border-radius:8px;padding:.8rem}
form.load input{font:inherit;padding:.45rem .6rem;border:1px solid var(--rule);border-radius:6px;background:#fff}
form.load input#repo{width:230px}form.load input#pr{width:90px}
form.load button{font:inherit;font-weight:600;padding:.45rem 1rem;border:1px solid var(--accent);background:var(--accent);color:#fff;border-radius:6px;cursor:pointer}
#status{color:var(--muted);font-size:.9rem;margin:.5rem 0}
.entry{background:var(--card);border:1px solid var(--rule);border-left:5px solid var(--rule);border-radius:6px;margin:1rem 0;overflow:hidden}
.entry[data-status=approve]{border-left-color:var(--g)}.entry[data-status=revise]{border-left-color:var(--y)}.entry[data-status=reject]{border-left-color:var(--r)}
.entry header{display:flex;gap:.5rem;align-items:baseline;flex-wrap:wrap;padding:.6rem .9rem;background:#f6f2e9;border-bottom:1px solid var(--rule)}
.entry header .qid a{font-family:"SF Mono",Menlo,monospace;color:var(--accent);text-decoration:none;font-size:.85rem}
.entry header .loc{color:var(--muted);font-size:.82rem;font-family:"SF Mono",Menlo,monospace}
pre.lean{font-family:"JuliaMono","JetBrains Mono","SF Mono",Menlo,Consolas,monospace;font-size:.82rem;background:var(--code);margin:0;padding:.7rem .9rem;overflow:auto;white-space:pre-wrap;border-bottom:1px solid var(--rule)}
.comments{padding:.5rem .9rem;display:flex;flex-direction:column;gap:.5rem}
.cmt{font-size:.88rem;background:#fbf9f3;border:1px solid var(--rule);border-radius:6px;padding:.45rem .6rem}
.cmt .who{font-weight:600;font-size:.8rem;color:var(--muted)}
.cmt .body{white-space:pre-wrap;margin-top:.2rem}
.cmt .body.md{white-space:normal}
.cmt .body.md p{margin:.2rem 0}
.cmt .body.md blockquote{margin:.3rem 0;padding:.1rem .7rem;border-left:3px solid var(--rule);color:#4a463c}
.cmt .body.md code{background:var(--code);padding:.05em .35em;border-radius:3px;font-family:"SF Mono",Menlo,monospace;font-size:.92em}
.cmt .body.md pre{background:var(--code);padding:.5rem .7rem;border-radius:5px;overflow:auto}
.cmt .body.md a{color:var(--accent)}
.cmt .body.md sub{color:var(--muted)}
.cmt .body.md h1,.cmt .body.md h2,.cmt .body.md h3{font-size:1rem;margin:.3rem 0}
.none{color:var(--muted);font-size:.85rem;font-style:italic;padding:.2rem .9rem .5rem}
.panes{display:grid;grid-template-columns:1fr 1fr;border-bottom:1px solid var(--rule)}
.src{border-right:1px solid var(--rule);background:#fdfcf8;overflow:auto}
.src pre.lean{border-bottom:none;margin:0}
.wiki-pane{padding:.7rem .9rem}
.wd-desc{font-size:.95rem;line-height:1.45;margin:0 0 .7rem;padding:.5rem .7rem;background:#fbf6ec;border-left:3px solid var(--accent);border-radius:0 3px 3px 0;color:#3a2a20;font-style:italic}
.wd-desc.empty{background:none;border-left:none;padding:0;color:var(--muted);font-style:italic}
.wd-head{font-size:.9rem;margin:0 0 .3rem}.wd-head a{color:var(--ink);font-weight:600;text-decoration:none}.wd-head a:hover{text-decoration:underline}
.wd-lead summary{cursor:pointer;color:var(--accent);font-weight:500;font-size:.85rem}
.wd-lead p{font-size:.88rem;line-height:1.5;margin:.4rem 0 0}
.wd-lead .more{font-size:.8rem;margin-top:.3rem}.wd-lead .more a{color:var(--accent)}
/* Lean syntax palette (Mathlib-docs-derived) */
pre.lean .sd{color:#4a5e2a;font-style:italic}
pre.lean .c1{color:#5d6b50;font-style:italic}
pre.lean .kn{color:#1F497F;font-weight:700}
pre.lean .kt{color:#73461C;font-weight:700}
pre.lean .nf{color:#134E2D;font-weight:500}
pre.lean .o{color:#262626}
pre.lean .s{color:#6B1B1A}
pre.lean .mi{color:#6B1B1A;font-weight:500}
pre.lean .n{color:#0a0a0a}
@media (max-width:820px){.panes{grid-template-columns:1fr}.src{border-right:none;border-bottom:1px solid var(--rule)}}
.form{padding:.6rem .9rem;border-top:1px dashed var(--rule);background:#fcfaf4}
.form .row{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;margin-bottom:.4rem}
.form label{display:inline-flex;align-items:center;gap:.3rem;padding:.15rem .55rem;border:1px solid var(--rule);border-radius:14px;background:#fff;cursor:pointer;font-size:.85rem}
.form label:has(input[value=approve]:checked){background:var(--gb);border-color:var(--g);color:var(--g);font-weight:600}
.form label:has(input[value=revise]:checked){background:var(--yb);border-color:var(--y);color:var(--y);font-weight:600}
.form label:has(input[value=reject]:checked){background:var(--rb);border-color:var(--r);color:var(--r);font-weight:600}
.form textarea{width:100%;min-height:44px;font:inherit;font-size:.88rem;padding:.4rem .5rem;border:1px solid var(--rule);border-radius:6px;resize:vertical}
#bar{position:fixed;left:0;right:0;bottom:0;background:var(--card);border-top:1px solid var(--rule);padding:.6rem 1.3rem;display:flex;justify-content:space-between;align-items:center;gap:1rem;box-shadow:0 -2px 8px rgba(0,0,0,.06)}
#bar .counts span{padding:.1rem .5rem;border-radius:12px;font-size:.85rem;margin-right:.4rem}
#bar button{font:inherit;font-weight:600;padding:.4rem 1rem;border:1px solid var(--accent);background:var(--accent);color:#fff;border-radius:6px;cursor:pointer}
#bar button:disabled{opacity:.5;cursor:default}
.note{font-size:.8rem;color:var(--muted)}
</style></head>
<body><div class="wrap">
<h1>WikiLean · <code>@[wikidata]</code> review</h1>
<p class="lede">Paste a pull request; review each tagged declaration with its existing GitHub comments, then submit your decisions.</p>
<form class="load" onsubmit="return false">
  <input id="repo" placeholder="owner/repo" value="Deicyde/mathlib4">
  <input id="pr" placeholder="PR #" inputmode="numeric">
  <button id="load">Load PR</button>
  <span class="note">e.g. <code>leanprover-community/mathlib4</code> · <code>Deicyde/mathlib4</code></span>
</form>
<div id="status"></div>
<div id="entries"></div>
</div>
<div id="bar" hidden>
  <div class="counts">
    <span id="c-approve" style="background:var(--gb);color:var(--g)">🟢 0</span>
    <span id="c-revise"  style="background:var(--yb);color:var(--y)">🟡 0</span>
    <span id="c-reject"  style="background:var(--rb);color:var(--r)">🔴 0</span>
    <span id="c-pending" style="background:#eee;color:var(--muted)">· pending 0</span>
  </div>
  <div>
    <span class="note" id="submit-note">Checking sign-in…</span>
    <button id="submit" disabled>Submit review</button>
  </div>
</div>
<script>
${reviewClientScript()}
</script></body></html>`;
}

function reviewClientScript(): string {
  // Vanilla JS. Loads /api/review/:owner/:repo/:pr, renders decls + comments +
  // a decision/notes form. Decisions persist in localStorage per repo+pr.
  return String.raw`
const $ = (s) => document.querySelector(s);
const esc = (s) => (s||"").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let STATE = {};   // qid -> {status, notes}
let KEY = "";
let CUR = null;   // {owner, repo, pr} of the loaded PR
let ME = null;    // logged-in user or null

function storageKey(repo, pr){ return "wl-review:" + repo + "#" + pr; }
function load(){ try { return JSON.parse(localStorage.getItem(KEY) || "{}"); } catch(e){ return {}; } }
function save(){ localStorage.setItem(KEY, JSON.stringify(STATE)); }

async function loadPR(){
  const repo = $("#repo").value.trim();
  const pr = $("#pr").value.trim();
  const m = repo.match(/^([\w.-]+)\/([\w.-]+)$/);
  if(!m || !pr){ $("#status").textContent = "Enter owner/repo and a PR number."; return; }
  KEY = storageKey(repo, pr); STATE = load();
  CUR = { owner: m[1], repo: m[2], pr: pr };
  $("#status").textContent = "Loading " + repo + " #" + pr + "…";
  $("#entries").innerHTML = "";
  try {
    const r = await fetch("/api/review/" + m[1] + "/" + m[2] + "/" + pr);
    const data = await r.json();
    if(!data.ok){ $("#status").textContent = "Error: " + data.error; return; }
    render(data);
  } catch(e){ $("#status").textContent = "Fetch failed: " + e; }
}

// Minimal Lean 4 highlighter → spans with the Mathlib-palette classes.
function hl(src){
  const KW = new Set(["def","theorem","lemma","class","structure","inductive","abbrev","instance","where","extends","noncomputable","protected","private","partial","mutual","namespace","end","open","variable","variables","import","deriving","by","fun","let","in","do","match","with","if","then","else","from","attribute","scoped","local","nonrec","opaque","example","section","return","have","show","calc"]);
  const DECL = new Set(["def","theorem","lemma","class","structure","inductive","abbrev","instance","opaque"]);
  const TY = new Set(["Type","Prop","Sort"]);
  const re = /(\/-[-!]?[\s\S]*?-\/)|(--[^\n]*)|("(?:[^"\\]|\\.)*")|([A-Za-z_À-￿][A-Za-z0-9_'.À-￿]*)|(\d[\d.]*)|(\s+)|([^\s])/g;
  let out="", m, afterDecl=false;
  while((m = re.exec(src))){
    if(m[1]) out += '<span class="sd">'+esc(m[1])+'</span>';
    else if(m[2]) out += '<span class="c1">'+esc(m[2])+'</span>';
    else if(m[3]) out += '<span class="s">'+esc(m[3])+'</span>';
    else if(m[4]){ const w=m[4]; let cls;
      if(KW.has(w)){ cls="kn"; afterDecl = DECL.has(w); }
      else if(TY.has(w)){ cls="kt"; afterDecl=false; }
      else { cls = afterDecl ? "nf" : "n"; afterDecl=false; }
      out += '<span class="'+cls+'">'+esc(w)+'</span>'; }
    else if(m[5]) out += '<span class="mi">'+esc(m[5])+'</span>';
    else if(m[6]) out += esc(m[6]);
    else out += '<span class="o">'+esc(m[7])+'</span>';
  }
  return out;
}

function render(data){
  $("#status").innerHTML = "<b>" + esc(data.title) + "</b> — " + data.decls.length +
    " tagged declarations · commit <code>" + data.head_sha.slice(0,10) + "</code>";
  const root = $("#entries"); root.innerHTML = "";
  data.decls.forEach((d) => {
    const st = (STATE[d.qid] || {});
    const el = document.createElement("article");
    el.className = "entry"; el.dataset.status = st.status || "";
    const commentsHtml = d.comments.length
      ? d.comments.map(c => '<div class="cmt"><div class="who">' + esc(c.user) +
          '</div>' + (c.bodyHtml ? '<div class="body md">' + c.bodyHtml + '</div>'
                                 : '<div class="body">' + esc(c.body) + '</div>') + '</div>').join("")
      : '<div class="none">No existing comments on this line.</div>';
    const wd = d.wd || {};
    const wikiHead = wd.enwikiUrl
      ? '<p class="wd-head"><a href="' + wd.enwikiUrl + '" target="_blank">' + esc(wd.enwikiTitle || wd.label || d.qid) + '</a></p>'
      : (wd.label ? '<p class="wd-head">' + esc(wd.label) + '</p>' : '');
    const descHtml = wd.description
      ? '<p class="wd-desc">' + esc(wd.description) + '</p>'
      : '<p class="wd-desc empty">(no Wikidata description)</p>';
    const leadHtml = wd.lead
      ? '<details class="wd-lead"><summary>Wikipedia lead ↓</summary><p>' + esc(wd.lead.slice(0,900)) + '</p>' +
        (wd.enwikiUrl ? '<p class="more"><a href="' + wd.enwikiUrl + '" target="_blank">Read full article ↗</a></p>' : '') +
        '</details>'
      : (wd.enwikiUrl ? '<p class="wd-lead"><a href="' + wd.enwikiUrl + '" target="_blank">Read on Wikipedia ↗</a></p>' : '');
    el.innerHTML =
      '<header><span class="qid"><a href="https://www.wikidata.org/wiki/' + d.qid +
        '" target="_blank">' + d.qid + '</a></span>' +
        '<span class="loc">' + esc(d.file) + ':' + d.line + '</span></header>' +
      '<div class="panes">' +
        '<section class="src"><pre class="lean">' + hl(d.source || "") + '</pre></section>' +
        '<section class="wiki-pane">' + wikiHead + descHtml + leadHtml + '</section>' +
      '</div>' +
      '<div class="comments">' + commentsHtml + '</div>' +
      '<div class="form"><div class="row">' +
        radio(d.qid,"approve","🟢 Approve",st.status) +
        radio(d.qid,"revise","🟡 Revise",st.status) +
        radio(d.qid,"reject","🔴 Reject",st.status) +
      '</div><textarea data-qid="' + d.qid + '" placeholder="Your note (verbatim, posted to GitHub)…">' +
        esc(st.notes||"") + '</textarea></div>';
    root.appendChild(el);
  });
  root.querySelectorAll('input[type=radio]').forEach(r =>
    r.addEventListener("change", e => set(e.target.dataset.qid, "status", e.target.value)));
  root.querySelectorAll('textarea[data-qid]').forEach(t =>
    t.addEventListener("input", e => set(e.target.dataset.qid, "notes", e.target.value)));
  $("#bar").hidden = false; counts();
}

function radio(qid, val, label, cur){
  return '<label><input type="radio" name="r-' + qid + '" value="' + val + '" data-qid="' + qid +
    '"' + (cur===val?" checked":"") + '>' + label + '</label>';
}
function set(qid, field, value){
  STATE[qid] = STATE[qid] || {}; STATE[qid][field] = value; save();
  if(field==="status"){ const el = [...document.querySelectorAll(".entry")].find(e =>
    e.querySelector('input[name=r-'+qid+']')); if(el) el.dataset.status = value; }
  counts();
}
function counts(){
  const c = {approve:0,revise:0,reject:0,pending:0};
  document.querySelectorAll(".entry").forEach(e => {
    const q = e.querySelector('input[type=radio]'); const qid = q && q.dataset.qid;
    const s = qid && STATE[qid] && STATE[qid].status;
    if(s) c[s]++; else c.pending++;
  });
  $("#c-approve").textContent = "🟢 " + c.approve;
  $("#c-revise").textContent  = "🟡 " + c.revise;
  $("#c-reject").textContent  = "🔴 " + c.reject;
  $("#c-pending").textContent = "· pending " + c.pending;
}

async function checkAuth(){
  try { const r = await fetch("/api/auth/me"); const d = await r.json(); ME = d.user || null; }
  catch(e){ ME = null; }
  const note = $("#submit-note"); const btn = $("#submit");
  if(ME){ note.innerHTML = "Signed in as <b>" + esc(ME.name) + "</b>"; btn.disabled = false; }
  else { note.innerHTML = '<a href="/login?returnTo=' + encodeURIComponent(location.pathname + location.search) + '">Sign in with GitHub</a> to submit'; btn.disabled = true; }
}

async function submitReview(){
  if(!CUR){ return; }
  if(!ME){ $("#submit-note").textContent = "Sign in first."; return; }
  const decisions = {};
  Object.keys(STATE).forEach(qid => {
    const s = STATE[qid] || {};
    if((s.status && s.status.trim()) || (s.notes && s.notes.trim())) decisions[qid] = { status: s.status||"", notes: s.notes||"" };
  });
  const n = Object.keys(decisions).length;
  if(!n){ $("#submit-note").textContent = "No decisions to post yet."; return; }
  const btn = $("#submit"); btn.disabled = true; const note = $("#submit-note");
  note.textContent = "Posting " + n + " comment(s)…";
  try {
    const r = await fetch("/api/review/" + CUR.owner + "/" + CUR.repo + "/" + CUR.pr, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decisions: decisions })
    });
    const d = await r.json();
    if(!d.ok){ note.textContent = "Error: " + d.error; btn.disabled = false; return; }
    const skipped = (d.results||[]).filter(x => x.skipped).length;
    const errs = (d.results||[]).filter(x => x.error);
    note.innerHTML = "Posted <b>" + d.posted + "</b>" + (skipped?(" · skipped " + skipped + " (already commented)"):"") +
      (errs.length?(" · " + errs.length + " error(s)"):"");
    // Refresh so the new comments appear under each decl.
    setTimeout(loadPR, 700);
  } catch(e){ note.textContent = "Submit failed: " + e; btn.disabled = false; }
}

$("#load").addEventListener("click", loadPR);
$("#pr").addEventListener("keydown", e => { if(e.key==="Enter") loadPR(); });
$("#submit").addEventListener("click", submitReview);
checkAuth();
// Deep-link support: /review?repo=owner/name&pr=6
const qp = new URLSearchParams(location.search);
if(qp.get("repo")) $("#repo").value = qp.get("repo");
if(qp.get("pr")){ $("#pr").value = qp.get("pr"); loadPR(); }
`;
}
