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
  html_url: string;
  created_at: string;
}

function keyOf(path: string, line: number): string {
  return `${path}:${line}`;
}

// ---- the assembled review payload returned to the browser ----

export interface ReviewPayload {
  repo: string;
  pr: number;
  head_sha: string;
  title: string;
  decls: Array<DeclTag & { comments: ReviewComment[] }>;
}

async function buildReviewPayload(
  owner: string,
  repo: string,
  pr: number,
  token?: string,
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
      html_url: c.html_url,
      created_at: c.created_at,
    });
  }

  return {
    repo: full,
    pr,
    head_sha: meta.head.sha,
    title: meta.title,
    decls: tags.map((t) => ({ ...t, comments: byLine.get(keyOf(t.file, t.line)) ?? [] })),
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
      const payload = await buildReviewPayload(owner, repo, pr, token);
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
.none{color:var(--muted);font-size:.85rem;font-style:italic;padding:.2rem .9rem .5rem}
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
          '</div><div class="body">' + esc(c.body) + '</div></div>').join("")
      : '<div class="none">No existing comments on this line.</div>';
    el.innerHTML =
      '<header><span class="qid"><a href="https://www.wikidata.org/wiki/' + d.qid +
        '" target="_blank">' + d.qid + '</a></span>' +
        '<span class="loc">' + esc(d.file) + ':' + d.line + '</span></header>' +
      '<pre class="lean">' + esc(d.hunk.join("\n")) + '</pre>' +
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
