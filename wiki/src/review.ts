// WikiLean crossref review tool.
//
// A logged-in reviewer pastes a PR (owner/repo/number); the page fetches the
// PR's crossref tags + any existing inline review comments from GitHub
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
import { getCookie, setCookie, deleteCookie } from "hono/cookie";
import type { Env } from "./env.js";
import { getUser } from "./auth.js";
import {
  type CrossRefSpec,
  crossRefBotMarkerRegex,
  crossRefReviewMarker,
  crossRefReviewMarkerRegex,
  crossRefSpec,
  crossRefTagRegex,
  crossRefUrl,
} from "./crossref.js";

const GH_API = "https://api.github.com";
const UA = "WikiLean-review/0.1 (+https://wikilean.jackmccarthy.org)";

// ---- dedicated review-posting OAuth (separate app from the wiki login) ----
// A reviewer who wants in-app posting clicks "Connect GitHub"; we run a classic
// OAuth flow against the REVIEW_GITHUB_* app (scope public_repo) and stash the
// token server-side in KV keyed by an opaque cookie id. Kept entirely off the
// identity-only wiki login (auth.ts), so article editors never see repo-write.
const REVIEW_COOKIE = "wl_review_gh"; // opaque session id → KV token
const REVIEW_STATE_COOKIE = "wl_review_oauth"; // CSRF state for the OAuth round-trip
const REVIEW_TOK_TTL = 60 * 60 * 12; // 12h
const GH_OAUTH_AUTHORIZE = "https://github.com/login/oauth/authorize";
const GH_OAUTH_TOKEN = "https://github.com/login/oauth/access_token";

function reviewBaseUrl(c: Ctx): string {
  return (c.env.BETTER_AUTH_URL || new URL(c.req.url).origin).replace(/\/$/, "");
}

// A connected reviewer's stashed GitHub credentials. The review app is a GitHub
// App: user access tokens may expire (~8h) and carry a refresh token (~6mo). A
// plain OAuth app instead returns a long-lived token (refresh_token/expires_at
// absent) — both shapes are handled, so swapping the app type needs no migration.
type ReviewTok = { token?: string; login?: string; refresh_token?: string; expires_at?: number };

// Exchange a `code` (callback) or a `refresh_token` for a user access token.
// SAME endpoint for OAuth apps and GitHub Apps — a GitHub App additionally returns
// refresh_token + expires_in when "expire user authorization tokens" is on. {} on fail.
async function ghTokenExchange(c: Ctx, params: Record<string, string>): Promise<ReviewTok> {
  try {
    const r = await fetch(GH_OAUTH_TOKEN, {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json", "User-Agent": UA },
      body: JSON.stringify({
        client_id: c.env.REVIEW_GITHUB_CLIENT_ID,
        client_secret: c.env.REVIEW_GITHUB_CLIENT_SECRET,
        ...params,
      }),
    });
    const d = (await r.json()) as { access_token?: string; refresh_token?: string; expires_in?: number };
    if (!d.access_token) return {};
    return {
      token: d.access_token,
      refresh_token: d.refresh_token,
      expires_at: d.expires_in ? Date.now() + d.expires_in * 1000 : undefined,
    };
  } catch {
    return {};
  }
}

// The connected reviewer's GitHub token + login, from the cookie→KV mapping. If
// the (GitHub App) access token is at/near expiry and we hold a refresh token,
// transparently mint a fresh one and re-stash on a rolling TTL so an active
// reviewer never has to re-connect mid-session.
async function reviewToken(c: Ctx): Promise<ReviewTok> {
  const id = getCookie(c, REVIEW_COOKIE);
  if (!id) return {};
  const raw = await c.env.RENDER_CACHE.get(`reviewtok:${id}`);
  if (!raw) return {};
  let tok: ReviewTok;
  try {
    tok = JSON.parse(raw) as ReviewTok;
  } catch {
    return {};
  }
  if (tok.refresh_token && tok.expires_at && Date.now() > tok.expires_at - 60_000) {
    const fresh = await ghTokenExchange(c, { grant_type: "refresh_token", refresh_token: tok.refresh_token });
    if (fresh.token) {
      tok = { token: fresh.token, login: tok.login, refresh_token: fresh.refresh_token ?? tok.refresh_token, expires_at: fresh.expires_at };
      await c.env.RENDER_CACHE.put(`reviewtok:${id}`, JSON.stringify(tok), { expirationTtl: REVIEW_TOK_TTL });
    }
  }
  return tok;
}

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

// The authenticated user's GitHub login (for per-author idempotency). Null if
// unauthenticated or the call fails.
async function ghLogin(token: string | undefined): Promise<string | null> {
  if (!token) return null;
  try {
    const r = await fetch(`${GH_API}/user`, { headers: ghHeaders(token) });
    if (!r.ok) return null;
    const d = (await r.json()) as { login?: string };
    return d.login ?? null;
  } catch {
    return null;
  }
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

// ---- diff parsing: find every added crossref attr line + its hunk context ----

export interface DeclTag {
  db: string;
  id: string;
  qid: string; // legacy UI key alias: QID for Wikidata, id for other databases
  file: string;
  line: number; // 1-based new-file line of the attribute
  hunk: string[]; // a few source lines starting at the tag (for display)
}

// Parse a unified diff: track the current file (+++ b/...) and new-file line
// counter (from @@ -a,b +c,d @@), and for each added line containing
// the selected crossref attribute record (id, file, line) plus the next few added/context lines
// as a display hunk.
export function parseCrossRefTags(diff: string, db = "wikidata"): DeclTag[] {
  const spec = crossRefSpec(db) ?? crossRefSpec("wikidata")!;
  const tagRe = crossRefTagRegex(spec.db);
  const isAttrLine = (s: string) => {
    const t = s.trimStart();
    return t.startsWith("@[") || /^(?:local\s+|scoped\s+)?attribute\s*\[/.test(t);
  };
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
      const m = isAttrLine(content) ? content.match(tagRe) : null;
      if (m) {
        // Display hunk: this line + up to 7 following added/context lines.
        const hunk: string[] = [content];
        for (let j = i + 1; j < lines.length && hunk.length < 8; j++) {
          const nx = lines[j];
          if (nx.startsWith("+") && !nx.startsWith("++")) hunk.push(nx.slice(1));
          else if (nx.startsWith(" ")) hunk.push(nx.slice(1));
          else break;
        }
        tags.push({ db: spec.db, id: m[1], qid: m[1], file, line: newLine, hunk });
      }
      newLine++;
    } else if (ln.startsWith(" ")) {
      newLine++;
    }
    // "-" lines and others: no new-line advance.
  }
  return tags;
}

export function parseWikidataTags(diff: string): DeclTag[] {
  return parseCrossRefTags(diff, "wikidata");
}

// ---- existing inline review comments, grouped by path:line ----

interface GhReviewComment {
  id: number;
  path: string;
  line: number | null;
  original_line: number | null;
  start_line: number | null; // multi-line comments (e.g. crossref's start_line=tag, line=tag+1)
  original_start_line: number | null;
  body: string;
  user: { login: string } | null;
  html_url: string;
  created_at: string;
  reactions?: { "+1"?: number; "-1"?: number }; // free summary on the list payload
}

interface GhReaction {
  content: string; // "+1" | "-1" | laugh | confused | heart | hooray | rocket | eyes
  user: { login: string } | null;
  created_at: string;
}

// A 👍/👎 reaction on the crossref per-tag comment = approve/reject by the reactor.
export interface ReactionVerdict {
  user: string;
  verdict: "approve" | "reject";
  created_at: string;
}

export interface ReviewComment {
  id: number;
  user: string;
  body: string;
  bodyHtml: string | null; // GitHub-rendered (sanitized) markdown, or null on fallback
  html_url: string;
  created_at: string;
  reactionVerdicts?: ReactionVerdict[]; // 👍/👎 on this comment (crossref per-tag comments only)
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

// True for a wikilean-review comment that carries only a status (no note) — the
// client drops these (the status shows in the "Existing review" row), so we
// skip rendering their markdown.
function statusOnlyReviewComment(body: string): boolean {
  if (!/wikilean-review:/.test(body)) return false;
  const rest = body
    .split("\n")
    .filter((l) => !/^\s*\*\*/.test(l) && !/<sub>/.test(l) && l.trim() !== "")
    .map((l) => l.replace(/^>\s?/, ""))
    .join("\n")
    .trim();
  return rest === "" || rest === "_(no note)_";
}

// ---- Wikidata label/description/enwiki + Wikipedia lead (cached) ----

export interface WdInfo {
  label: string | null;
  description: string | null;
  enwikiUrl: string | null;
  enwikiTitle: string | null;
  lead: string | null;
}

export interface ExternalInfo {
  db: string;
  id: string;
  label: string | null;
  title: string | null;
  description: string | null;
  url: string;
  contextUrl: string | null;
  lead: string | null;
}

export interface LmfdbInfo {
  title: string | null;
  description: string | null;
}

function externalInfo(spec: CrossRefSpec, id: string, wd: WdInfo | null = null, lmfdb: LmfdbInfo | null = null): ExternalInfo {
  if (spec.db === "wikidata") {
    return {
      db: spec.db,
      id,
      label: wd?.label ?? id,
      title: wd?.enwikiTitle ?? wd?.label ?? id,
      description: wd?.description ?? null,
      url: crossRefUrl(spec.db, id),
      contextUrl: wd?.enwikiUrl ?? null,
      lead: wd?.lead ?? null,
    };
  }
  return {
    db: spec.db,
    id,
    label: id,
    title: lmfdb?.title ?? id,
    description: lmfdb?.description ?? null,
    url: crossRefUrl(spec.db, id),
    contextUrl: crossRefUrl(spec.db, id),
    lead: null,
  };
}

const WD_API = "https://www.wikidata.org/w/api.php";

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

// LMFDB knowl pages have one stable page-level title plus the actual knowl body
// in `.knowl-content`.  Extract a compact plaintext summary from that body.
export function parseLmfdbKnowlHtml(html: string): LmfdbInfo {
  const titleHtml = (html.match(/<div\s+id=["']title["'][^>]*>([\s\S]*?)<\/div>/i) || [])[1] ?? "";
  const rawTitle = cleanLead(htmlLeadToText(titleHtml || "")) || null;
  const title = rawTitle ? rawTitle.replace(/\s*\((?:reviewed|beta|needs review)\)\s*$/i, "") : null;
  const bodyHtml = (html.match(/<div\b[^>]*class=["'][^"']*\bknowl-content\b[^"']*["'][^>]*>([\s\S]*?)<\/div>/i) || [])[1] ?? "";
  const description = cleanLead(htmlLeadToText(bodyHtml || "")) || null;
  return { title, description };
}

async function fetchLmfdbKnowls(ids: string[], env: Env): Promise<Map<string, LmfdbInfo>> {
  const out = new Map<string, LmfdbInfo>();
  const missing: string[] = [];
  for (const id of [...new Set(ids)]) {
    const c = await env.RENDER_CACHE.get(`lmfdbk:v1:${id}`);
    if (c !== null) out.set(id, JSON.parse(c) as LmfdbInfo);
    else missing.push(id);
  }
  await runPool(missing, 4, async (id) => {
    try {
      const r = await fetch(crossRefUrl("lmfdb", id), { headers: { "User-Agent": UA } });
      if (!r.ok) return;
      const info = parseLmfdbKnowlHtml(await r.text());
      if (!info.title && !info.description) return;
      out.set(id, info);
      await env.RENDER_CACHE.put(`lmfdbk:v1:${id}`, JSON.stringify(info), { expirationTtl: 60 * 60 * 24 * 7 });
    } catch {
      // Keep the review page usable if LMFDB is slow/unreachable.
    }
  });
  return out;
}

// ---- MathML → Unicode (for rendering Wikipedia math leads cleanly) ----
//
// The Wikipedia REST summary `extract` leaks broken LaTeX for math articles
// (e.g. ":\mathbb {Z} \rightarrow \mathbb {C} }"). Instead we fetch the action
// API's HTML intro, where each formula is a self-contained <math> element with
// full presentation MathML (the glyphs are already Unicode), and render it to
// compact inline Unicode ("χ: ℤ → ℂ", "i² = −1", "∑ₙ₌₁^∞ 1/(n^s)").

const DS_UP: Record<string, string> = { C: "ℂ", H: "ℍ", N: "ℕ", P: "ℙ", Q: "ℚ", R: "ℝ", Z: "ℤ" };
// Map ASCII to blackboard-bold (double-struck), honoring the Letterlike-Symbols
// exceptions (ℂℍℕℙℚℝℤ live outside the Mathematical Alphanumeric block).
function doubleStruck(s: string): string {
  let o = "";
  for (const ch of s) {
    const c = ch.codePointAt(0)!;
    if (DS_UP[ch]) o += DS_UP[ch];
    else if (ch >= "A" && ch <= "Z") o += String.fromCodePoint(0x1d538 + (c - 65));
    else if (ch >= "a" && ch <= "z") o += String.fromCodePoint(0x1d552 + (c - 97));
    else if (ch >= "0" && ch <= "9") o += String.fromCodePoint(0x1d7d8 + (c - 48));
    else o += ch;
  }
  return o;
}
const SUP_MAP: Record<string, string> = { "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹", "+": "⁺", "-": "⁻", "−": "⁻", "=": "⁼", "(": "⁽", ")": "⁾", n: "ⁿ", i: "ⁱ" };
const SUB_MAP: Record<string, string> = { "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅", "6": "₆", "7": "₇", "8": "₈", "9": "₉", "+": "₊", "-": "₋", "−": "₋", "=": "₌", "(": "₍", ")": "₎", n: "ₙ", i: "ᵢ", k: "ₖ", a: "ₐ", x: "ₓ", j: "ⱼ", m: "ₘ", s: "ₛ", t: "ₜ", l: "ₗ", p: "ₚ", r: "ᵣ", u: "ᵤ", v: "ᵥ", o: "ₒ", e: "ₑ", h: "ₕ" };
function toScript(s: string, map: Record<string, string>): string | null {
  if (!s) return null;
  let o = "";
  for (const ch of s) {
    if (!map[ch]) return null;
    o += map[ch];
  }
  return o;
}
// Invisible/format chars: soft hyphen, ZWSP/ZWNJ/ZWJ, word joiner, the four
// invisible math operators (function application, invisible times/comma), BOM.
const INVIS = /[­​‌‍⁠⁡⁢⁣⁤﻿]/g;
function decodeEntities(s: string): string {
  return s
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#160;|&nbsp;/g, " ")
    .replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(+n))
    .replace(/&#x([0-9a-fA-F]+);/g, (_, n) => String.fromCodePoint(parseInt(n, 16)))
    .replace(/&amp;/g, "&");
}
// Index just past the '>' that ends the tag starting at `lt`, skipping quoted
// attribute values (so a '>' inside alttext="…>…" doesn't terminate the tag).
function tagEnd(s: string, lt: number): number {
  let q = "";
  for (let i = lt + 1; i < s.length; i++) {
    const c = s[i];
    if (q) {
      if (c === q) q = "";
    } else if (c === '"' || c === "'") q = c;
    else if (c === ">") return i + 1;
  }
  return s.length;
}

interface MNode {
  t: string;
  attrs?: string;
  kids?: MNode[];
  text?: string;
}
const SPACED = ["⟺", "⟹", "⟶", "↦", "→", "⇒", "⇔", "≡", "≅", "≈", "≤", "≥", "≠", "∈", "∉", "⊆", "⊂", "⊇", "⊃", "=", "×", "≪", "≫", "<", ">"];

// Render one <math>…</math> element (presentation MathML) to inline Unicode.
export function mathToUnicode(xml: string): string {
  xml = xml
    .replace(/<!--[\s\S]*?-->/g, "")
    .replace(/<annotation(-xml)?\b[\s\S]*?<\/annotation(-xml)?>/g, "");
  let i = 0;
  function children(): MNode[] {
    const kids: MNode[] = [];
    while (i < xml.length) {
      if (xml[i] !== "<") {
        const j = xml.indexOf("<", i);
        const stop = j < 0 ? xml.length : j;
        kids.push({ t: "#text", text: xml.slice(i, stop) });
        i = stop;
        continue;
      }
      const end = tagEnd(xml, i);
      const raw = xml.slice(i + 1, end - 1);
      i = end;
      if (raw[0] === "/") return kids;
      const self = raw.endsWith("/");
      const name = raw.replace(/^\s+/, "").split(/[\s/>]/)[0];
      if (self) {
        kids.push({ t: name, attrs: raw, kids: [] });
        continue;
      }
      kids.push({ t: name, attrs: raw, kids: children() });
    }
    return kids;
  }
  const root = children();
  const wrap = (s: string): string => ((s = s || ""), s.length > 1 ? "(" + s + ")" : s);
  const scr = (s: string, map: Record<string, string>, fb: string): string => {
    s = (s || "").trim();
    const m = toScript(s, map);
    return m !== null ? m : fb + wrap(s);
  };
  function rend(node: MNode): string {
    if (node.t === "#text") return decodeEntities(node.text ?? "").replace(INVIS, "");
    const all = node.kids ?? [];
    // Inter-element whitespace is insignificant in presentation MathML; drop it
    // (so "f⁡(x)" → "f(x)") except inside <mtext>, where spaces are literal.
    const keep = node.t === "mtext" ? all : all.filter((k) => !(k.t === "#text" && !(k.text ?? "").trim()));
    const r = keep.map(rend);
    const e = keep.filter((k) => k.t !== "#text").map((k) => rend(k).trim());
    switch (node.t) {
      case "mi":
      case "mn":
      case "mo":
      case "mtext": {
        let t = keep.map(rend).join("");
        const mv = (node.attrs?.match(/mathvariant="([^"]+)"/) || [])[1];
        if (mv === "double-struck") t = doubleStruck(t);
        return t;
      }
      case "mspace":
        return " ";
      case "msup": {
        const sup = toScript((e[1] || "").trim(), SUP_MAP);
        return (e[0] || "") + (sup !== null ? sup : "^" + wrap(e[1]));
      }
      case "msub": {
        const sub = toScript((e[1] || "").trim(), SUB_MAP);
        return (e[0] || "") + (sub !== null ? sub : "_" + wrap(e[1]));
      }
      case "msubsup":
        return (e[0] || "") + scr(e[1], SUB_MAP, "_") + scr(e[2], SUP_MAP, "^");
      case "munderover":
        return (e[0] || "") + scr(e[1], SUB_MAP, "_") + scr(e[2], SUP_MAP, "^");
      case "munder":
        return (e[0] || "") + "_" + wrap(e[1]);
      case "mover":
        return (e[0] || "") + "^" + wrap(e[1]);
      case "mfrac": {
        // A zero-thickness fraction bar is \binom{n}{k} — render as "n choose k"
        // rather than "(n/k)", which would misleadingly read as division.
        const bar = (node.attrs?.match(/linethickness="([^"]*)"/) || [])[1];
        if (bar !== undefined && /^0/.test(bar)) return wrap(e[0]) + " choose " + wrap(e[1]);
        return wrap(e[0]) + "/" + wrap(e[1]);
      }
      case "msqrt":
        return "√" + wrap(e.join(""));
      case "mroot":
        return (e[1] || "") + "√" + wrap(e[0]);
      default:
        return r.join("");
    }
  }
  let s = root.map(rend).join("").replace(/\s+/g, " ").trim();
  for (const op of SPACED) s = s.split(op).join(" " + op + " ");
  s = s.replace(/([,;:])(?=[^\s)\]])/g, "$1 "); // space after , ; : when crowded
  return s.replace(/\s+/g, " ").trim();
}

// Turn the action API's HTML intro into plain text: take the first paragraphs,
// render each <math> to Unicode, convert prose <sup>/<sub>, drop everything else.
export function htmlLeadToText(html: string): string {
  // Skip empty/structural paragraphs (Wikipedia leads can open with a couple of
  // `mw-empty-elt` <p>s) so "first 2 paragraphs" lands on real content. A
  // formula-only paragraph survives — its MathML glyphs aren't empty after the
  // tag strip.
  const paras = [...html.matchAll(/<p\b[^>]*>([\s\S]*?)<\/p>/g)]
    .map((m) => m[1])
    .filter((p) => decodeEntities(p.replace(/<[^>]+>/g, "")).trim() !== "");
  const body = paras.slice(0, 2).join(" ") || html;
  let out = "";
  let k = 0;
  for (;;) {
    const lt = body.indexOf("<math", k);
    if (lt < 0) {
      out += body.slice(k);
      break;
    }
    out += body.slice(k, lt);
    const close = body.indexOf("</math>", lt);
    if (close < 0) {
      out += body.slice(lt);
      break;
    }
    out += mathToUnicode(body.slice(lt, close + 7));
    k = close + 7;
  }
  const sb = (c: string, map: Record<string, string>): string => {
    const x = decodeEntities(c.replace(/<[^>]+>/g, "")).trim();
    if (/^\[.*\]$/.test(x)) return ""; // footnote/citation marker, e.g. [1] or [note 1]
    const u = toScript(x, map);
    return u !== null ? u : x ? (map === SUP_MAP ? "^" : "_") + (x.length > 1 ? "(" + x + ")" : x) : "";
  };
  out = out
    .replace(/<sup\b[^>]*class="[^"]*reference[^"]*"[^>]*>[\s\S]*?<\/sup>/g, "") // drop citation markers
    .replace(/<sup\b[^>]*>([\s\S]*?)<\/sup>/g, (_, c) => sb(c, SUP_MAP))
    .replace(/<sub\b[^>]*>([\s\S]*?)<\/sub>/g, (_, c) => sb(c, SUB_MAP))
    .replace(/<style\b[\s\S]*?<\/style>/g, "")
    .replace(/<(?:[^>"']|"[^"]*"|'[^']*')*>/g, ""); // quote-aware tag strip
  return decodeEntities(out);
}

// Final tidy + safety net: collapse whitespace and orphaned punctuation, and
// strip any TeX/brace residue that slipped past the MathML renderer.
export function cleanLead(t: string | null): string | null {
  if (!t) return t;
  for (let i = 0; i < 4; i++) {
    t = t.replace(/\{\\(?:display|text)style\s*([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}/g, "$1");
  }
  t = t.replace(/\\[a-zA-Z]+\s*/g, "").replace(/[{}]/g, "");
  return t
    .replace(INVIS, "")
    .replace(/[ \t\n]{2,}/g, " ")
    .replace(/\s+([,.;:)\]])/g, "$1")
    .replace(/([([])\s+/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
}

// Run async tasks with a bounded concurrency (a worker pool).
async function runPool<T>(items: T[], limit: number, fn: (item: T) => Promise<void>): Promise<void> {
  const queue = [...items];
  const worker = async () => {
    for (let it = queue.shift(); it !== undefined; it = queue.shift()) await fn(it);
  };
  await Promise.all(Array.from({ length: Math.min(limit, queue.length) }, worker));
}

// Wikipedia leads via the action API HTML intro, KV-cached. We render math from
// the MathML the action API embeds — far cleaner than the REST summary
// `extract`, which leaks broken LaTeX for math-heavy articles. Fetched through a
// small worker pool with one retry: firing all titles at once bursts Wikipedia's
// per-IP rate limit, so a cold load of a fresh PR was getting only ~half its
// leads (the rest filled in over later loads as the cache warmed).
async function fetchLeads(titles: string[], env: Env): Promise<Map<string, string>> {
  const out = new Map<string, string>();
  const fetchOne = async (t: string) => {
    const key = `wplead5:${t}`; // bumped from wplead4: — skip empty leading paragraphs
    const cached = await env.RENDER_CACHE.get(key);
    if (cached !== null) {
      out.set(t, cached);
      return;
    }
    const u =
      `https://en.wikipedia.org/w/api.php?action=query&prop=extracts&exintro=1` +
      `&redirects=1&format=json&titles=${encodeURIComponent(t)}`;
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const r = await fetch(u, { headers: { "User-Agent": UA } });
        if (!r.ok) {
          if (attempt === 0) {
            await new Promise((res) => setTimeout(res, 300));
            continue;
          }
          return;
        }
        const j = (await r.json()) as { query?: { pages?: Record<string, { extract?: string }> } };
        const page = Object.values(j.query?.pages ?? {})[0];
        const lead = (cleanLead(htmlLeadToText(page?.extract ?? "")) ?? "").slice(0, 1000);
        out.set(t, lead);
        await env.RENDER_CACHE.put(key, lead, { expirationTtl: 60 * 60 * 24 * 7 });
        return;
      } catch {
        if (attempt === 0) await new Promise((res) => setTimeout(res, 300));
      }
    }
  };
  await runPool(titles, 5, fetchOne);
  return out;
}

// ---- full decl-body extraction from the file at the PR head (cached) ----

const TOPLEVEL_PREFIXES = [
  "irreducible_def ",
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

// Given file lines and a crossref id, return the full declaration slice
// (docstring + attributes + modifiers + signature + body) around its tag.
export function extractCrossRefDeclBody(lines: string[], db: string, id: string): string | null {
  const spec = crossRefSpec(db) ?? crossRefSpec("wikidata")!;
  const pat = new RegExp(`${spec.attr}\\s+${id.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`);
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

export function extractDeclBody(lines: string[], qid: string): string | null {
  return extractCrossRefDeclBody(lines, "wikidata", qid);
}

const DECL_SIG_RE =
  /^(?:protected\s+|private\s+|noncomputable\s+|public\s+|nonrec\s+|unsafe\s+|partial\s+|scoped\s+|local\s+)*(?:irreducible_def|def|theorem|lemma|class|structure|inductive|abbrev|instance|opaque|axiom)\s+([^\s:({\[]+)/;

// Given file lines and a crossref id, return the fully-qualified declaration name around
// its tag (e.g. "Module.Projective"), by reading the signature
// line and prepending any enclosing `namespace`s. Used to anchor the Mathlib
// docs link. Null when there is no named signature (e.g. an anonymous instance).
export function extractCrossRefDeclName(lines: string[], db: string, id: string): string | null {
  const spec = crossRefSpec(db) ?? crossRefSpec("wikidata")!;
  const pat = new RegExp(`${spec.attr}\\s+${id.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`);
  const idx = lines.findIndex((l) => pat.test(l));
  if (idx < 0) return null;
  let sig = idx + 1;
  while (sig < lines.length && (lines[sig].trimStart().startsWith("@[") || BARE_MODIFIERS.has(lines[sig].trim()))) sig++;
  const m = (lines[sig] ?? "").match(DECL_SIG_RE);
  if (!m) return null;
  const short = m[1];
  // Namespace walk from the file top to the signature line (`end <name>` pops a
  // namespace; a bare `end` closing a section doesn't match and is ignored).
  const stack: string[] = [];
  for (let i = 0; i < sig; i++) {
    const t = lines[i].trim();
    const o = t.match(/^namespace\s+(\S+)/);
    const c = t.match(/^end\s+(\S+)/);
    if (o) stack.push(o[1]);
    else if (c && stack.length && stack[stack.length - 1] === c[1]) stack.pop();
  }
  if (stack.length) {
    const prefix = stack.join(".");
    if (!short.startsWith(prefix + ".") && short !== prefix) return prefix + "." + short;
  }
  return short;
}

export function extractDeclName(lines: string[], qid: string): string | null {
  return extractCrossRefDeclName(lines, "wikidata", qid);
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
  db: string;
  attr: string;
  dbLabel: string;
  head_sha: string;
  title: string;
  decls: Array<DeclTag & { comments: ReviewComment[]; source: string; wd: WdInfo | null; external: ExternalInfo; decl: string | null }>;
}

async function buildReviewPayload(
  owner: string,
  repo: string,
  pr: number,
  token?: string,
  env?: Env,
  renderMarkdown = false,
  db = "wikidata",
): Promise<ReviewPayload> {
  const spec = crossRefSpec(db) ?? crossRefSpec("wikidata")!;
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
  const tags = parseCrossRefTags(diff, spec.db);

  const comments = await ghPaginate<GhReviewComment>(
    `${GH_API}/repos/${full}/pulls/${pr}/comments`,
    token,
  );
  // Render bodies to HTML in parallel (cached). Only for the page read path —
  // and skip status-only review comments (the client drops them), saving a
  // GitHub /markdown call each.
  const htmlById = new Map<number, string>();
  if (renderMarkdown && env) {
    const rendered = await Promise.all(
      comments.map(async (c) =>
        statusOnlyReviewComment(c.body)
          ? ([c.id, null] as const)
          : ([c.id, await ghRenderMarkdown(c.body, full, token, env)] as const),
      ),
    );
    for (const [id, html] of rendered) if (html !== null) htmlById.set(id, html);
  }
  // 👍/👎 reactions on the crossref per-tag comments = approve/reject by the reactor.
  // Page mode only, and only for comments the free list-summary says actually have a
  // thumbs reaction — so we add at most one /reactions call per reacted crossref comment.
  const reactionsById = new Map<number, ReactionVerdict[]>();
  if (renderMarkdown) {
    const withThumbs = comments.filter(
      (c) =>
        crossRefBotMarkerRegex(spec.db).test(c.body || "") &&
        c.reactions &&
        ((c.reactions["+1"] ?? 0) + (c.reactions["-1"] ?? 0)) > 0,
    );
    await Promise.all(
      withThumbs.map(async (c) => {
        const rs = await ghPaginate<GhReaction>(
          `${GH_API}/repos/${full}/pulls/comments/${c.id}/reactions`,
          token,
        ).catch(() => [] as GhReaction[]);
        const v = rs
          .filter((r) => r.content === "+1" || r.content === "-1")
          .map((r) => ({
            user: r.user?.login ?? "",
            verdict: (r.content === "+1" ? "approve" : "reject") as "approve" | "reject",
            created_at: r.created_at,
          }))
          .filter((r) => r.user);
        if (v.length) reactionsById.set(c.id, v);
      }),
    );
  }
  const byLine = new Map<string, ReviewComment[]>();
  for (const c of comments) {
    // Index under every anchor line a comment carries. Multi-line comments (the
    // crossref tool posts start_line=tag, line=tag+1) would otherwise only key
    // on `line` and miss the decl, which is pinned to the tag (start) line.
    const anchors = new Set<number>();
    for (const v of [c.line, c.start_line, c.original_line, c.original_start_line]) {
      if (v != null) anchors.add(v);
    }
    if (!anchors.size) continue;
    const rc: ReviewComment = {
      id: c.id,
      user: c.user?.login ?? "unknown",
      body: c.body,
      bodyHtml: htmlById.get(c.id) ?? null,
      html_url: c.html_url,
      created_at: c.created_at,
      reactionVerdicts: reactionsById.get(c.id) ?? [],
    };
    for (const ln of anchors) {
      const k = keyOf(c.path, ln);
      if (!byLine.has(k)) byLine.set(k, []);
      byLine.get(k)!.push(rc);
    }
  }

  // Page mode: also pull top-level "## WikiLean review" comments (the pasted
  // copy-paste reviews) and fold their per-id entries into the matching cards,
  // in the same inline shape the client renders.
  const pastedById = new Map<string, ReviewComment[]>();
  if (renderMarkdown) {
    const issueComments = await ghPaginate<{
      id: number;
      body: string;
      user: { login: string } | null;
      html_url: string;
      created_at: string;
    }>(`${GH_API}/repos/${full}/issues/${pr}/comments`, token);
    for (const ic of issueComments) {
      if (!ic.body || !isWikiLeanReview(ic.body)) continue;
      for (const e of parseClipboardReview(ic.body, spec.db)) {
        const body = synthReviewBody(spec.db, e.qid, e.status, e.note);
        const bodyHtml = e.note && env ? await ghRenderMarkdown(body, full, token, env) : null;
        if (!pastedById.has(e.qid)) pastedById.set(e.qid, []);
        pastedById.get(e.qid)!.push({
          id: ic.id,
          user: ic.user?.login ?? "unknown",
          body,
          bodyHtml,
          html_url: ic.html_url,
          created_at: ic.created_at,
        });
      }
    }
  }

  // Page mode (renderMarkdown): also fetch the full decl body per tag and the
  // Wikidata description + Wikipedia lead per id. Skipped for the POST path.
  const sourceById = new Map<string, string>();
  const declById = new Map<string, string>();
  let wdById = new Map<string, WdInfo>();
  let lmfdbById = new Map<string, LmfdbInfo>();
  if (renderMarkdown && env) {
    // Full bodies: fetch each unique file once, in parallel, then slice.
    const fileCache = new Map<string, string[] | null>();
    const uniqueFiles = [...new Set(tags.map((t) => t.file))];
    await Promise.all(
      uniqueFiles.map(async (f) => {
        fileCache.set(f, await fetchFileLines(full, f, meta.head.sha, token, env));
      }),
    );
    for (const t of tags) {
      const lines = fileCache.get(t.file) ?? null;
      const body = lines ? extractCrossRefDeclBody(lines, spec.db, t.id) : null;
      if (body) sourceById.set(t.id, body);
      const name = lines ? extractCrossRefDeclName(lines, spec.db, t.id) : null;
      if (name) declById.set(t.id, name);
    }
    // Wikidata + Wikipedia leads.
    if (spec.db === "wikidata") {
      const ids = [...new Set(tags.map((t) => t.id))];
      wdById = await fetchWikidata(ids, env);
      const titles = [...wdById.values()].map((w) => w.enwikiTitle).filter((x): x is string => !!x);
      const leads = await fetchLeads(titles, env);
      for (const [, w] of wdById) if (w.enwikiTitle) w.lead = leads.get(w.enwikiTitle) ?? null;
    } else if (spec.db === "lmfdb") {
      lmfdbById = await fetchLmfdbKnowls([...new Set(tags.map((t) => t.id))], env);
    }
  }

  return {
    repo: full,
    pr,
    db: spec.db,
    attr: spec.attr,
    dbLabel: spec.label,
    head_sha: meta.head.sha,
    title: meta.title,
    decls: tags.map((t) => ({
      ...t,
      comments: [...(byLine.get(keyOf(t.file, t.line)) ?? []), ...(pastedById.get(t.id) ?? [])],
      source: sourceById.get(t.id) ?? t.hunk.join("\n"),
      wd: wdById.get(t.id) ?? null,
      external: externalInfo(spec, t.id, wdById.get(t.id) ?? null, lmfdbById.get(t.id) ?? null),
      decl: declById.get(t.id) ?? null,
    })),
  };
}

// ---- routes -------------------------------------------------------------

const OWNER_RE = /^[A-Za-z0-9_.-]+$/;

function reviewCacheKey(owner: string, repo: string, pr: number, db = "wikidata"): string {
  return `reviewpayload:v2:${db}:${owner}/${repo}:${pr}`;
}

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
    const spec = crossRefSpec(c.req.query("db") || "wikidata");
    if (!spec) return c.json({ ok: false, error: "unknown crossref database" }, 404);
    // Full-payload cache (60s) — a refresh within the window returns instantly
    // from KV with zero GitHub calls. Busted on POST so a just-submitted review
    // shows immediately; external comments are at most 60s stale.
    const pageKey = reviewCacheKey(owner, repo, pr, spec.db);
    const cached = await c.env.RENDER_CACHE.get(pageKey);
    if (cached) return c.body(cached, 200, { "Content-Type": "application/json" });

    // Reads use the logged-in user's token if present, else the server token
    // (5000/hr) — a single page load makes ~50 GitHub calls and would blow the
    // shared 60/hr unauthenticated limit otherwise.
    const { token } = await githubAccountFor(c);
    const readToken = token || c.env.GITHUB_API_TOKEN;
    try {
      const payload = await buildReviewPayload(owner, repo, pr, readToken, c.env, true, spec.db);
      const json = JSON.stringify({ ok: true, ...payload });
      await c.env.RENDER_CACHE.put(pageKey, json, { expirationTtl: 60 });
      return c.body(json, 200, { "Content-Type": "application/json" });
    } catch (e) {
      return c.json({ ok: false, error: String(e instanceof Error ? e.message : e) }, 502);
    }
  });

  // Post the reviewer's decisions/notes as inline PR comments, via the token
  // from the dedicated review OAuth app ("Connect GitHub" flow above) — NOT the
  // identity-only wiki login. Verbatim notes, never LLM-authored.
  app.post("/api/review/:owner/:repo/:pr", async (c) => {
    // CSRF defense-in-depth: reject cross-origin browser POSTs.
    const origin = c.req.header("Origin");
    if (origin && origin !== new URL(c.req.url).origin) {
      return c.json({ ok: false, error: "cross-origin request rejected" }, 403);
    }
    const owner = c.req.param("owner");
    const repo = c.req.param("repo");
    const pr = parseInt(c.req.param("pr"), 10);
    if (!OWNER_RE.test(owner) || !OWNER_RE.test(repo) || !Number.isInteger(pr) || pr <= 0) {
      return c.json({ ok: false, error: "bad owner/repo/pr" }, 400);
    }
    const spec = crossRefSpec(c.req.query("db") || "wikidata");
    if (!spec) return c.json({ ok: false, error: "unknown crossref database" }, 404);
    if (!c.env.REVIEW_GITHUB_CLIENT_ID || !c.env.REVIEW_GITHUB_CLIENT_SECRET) {
      return c.json(
        { ok: false, error: "in-app posting isn't configured — use Copy review.", needsConnect: false },
        503,
      );
    }
    const { token } = await reviewToken(c);
    if (!token) {
      return c.json(
        { ok: false, error: "Connect GitHub to post your review.", needsConnect: true },
        401,
      );
    }

    let body: { decisions?: Record<string, { status?: string; notes?: string; was?: string }> };
    try {
      body = await c.req.json();
    } catch {
      return c.json({ ok: false, error: "bad json" }, 400);
    }
    const decisions = body.decisions ?? {};

    // Resolve id -> (file, line) + head sha + already-posted markers from the PR.
    let payload: ReviewPayload;
    try {
      payload = await buildReviewPayload(owner, repo, pr, token, undefined, false, spec.db);
    } catch (e) {
      return c.json({ ok: false, error: String(e instanceof Error ? e.message : e) }, 502);
    }
    const tagById = new Map(payload.decls.map((d) => [d.id, d]));
    // Idempotency by GitHub login (comment author), tracking my LATEST status per
    // tag so a reviewer can still change their mind (revise→approve) or add a
    // note — we only skip a true no-op (same status, no new note).
    const myLogin = await ghLogin(token);
    // POSTING CREDENTIAL (option A). A configured personal PAT (REVIEW_POSTING_PAT)
    // is exempt from the org's OAuth-App restriction, so it posts to mathlib with no
    // App install — but ONLY for its OWNER: you must be connected AS the PAT account.
    // That gate is load-bearing (this endpoint is public) — it stops anyone else from
    // posting through your token. Everyone else keeps their own token (posts as
    // themselves; 403s cleanly until the App is installed for them). The comment
    // author is always myLogin, since the PAT is used only when owner == connectee.
    let postToken = token;
    if (c.env.REVIEW_POSTING_PAT && myLogin) {
      const patLogin = await ghLogin(c.env.REVIEW_POSTING_PAT);
      if (patLogin && patLogin.toLowerCase() === myLogin.toLowerCase()) postToken = c.env.REVIEW_POSTING_PAT;
    }
    const myLatest = new Map<string, { status: string; at: string }>();
    for (const d of payload.decls) {
      for (const cm of d.comments) {
        const m = cm.body.match(crossRefReviewMarkerRegex(spec.db));
        if (!m || !myLogin || cm.user !== myLogin) continue;
        let st = "";
        if (/Deletion candidate/.test(cm.body)) st = "flag";
        else {
          const sm = cm.body.match(/\((approve|revise|reject)\)/);
          if (sm) st = sm[1];
        }
        const prev = myLatest.get(m[1]);
        if (!prev || (cm.created_at || "") > prev.at) myLatest.set(m[1], { status: st, at: cm.created_at || "" });
      }
    }

    const results: Array<{ qid: string; posted: boolean; skipped?: string; error?: string }> = [];
    let changed = 0; // posted comments that set a status differing from the existing one
    for (const [qid, dec] of Object.entries(decisions)) {
      const status = (dec.status ?? "").trim();
      const notes = (dec.notes ?? "").trim();
      const was = (dec.was ?? "").trim();
      if (!status && !notes) continue; // nothing to post
      const mine = myLatest.get(qid);
      if (mine && mine.status === status && !notes) {
        results.push({ qid, posted: false, skipped: "no change from your last review" });
        continue;
      }
      const tag = tagById.get(qid);
      if (!tag) {
        // A stored decision for a tag that's since been TRIMMED from the PR (a
        // recycled/rejected tag). Not an error — nothing to post. Skip it so it
        // doesn't inflate the "failed" count with stale, unactionable noise.
        results.push({ qid, posted: false, skipped: "not in this PR (trimmed)" });
        continue;
      }
      // "changed from" should reflect MY own prior status when I've reviewed
      // this tag before; else fall back to the client-sent prior.
      const prior = mine ? mine.status : was;
      const commentBody = buildReviewCommentBody(qid, status, notes, prior, spec.db);
      const r = await fetch(`${GH_API}/repos/${owner}/${repo}/pulls/${pr}/comments`, {
        method: "POST",
        headers: { ...ghHeaders(postToken), "Content-Type": "application/json" },
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
        if (status && status !== prior) changed++;
      } else {
        results.push({ qid, posted: false, error: `GitHub ${r.status}: ${(await r.text()).slice(0, 300)}` });
      }
    }
    const posted = results.filter((x) => x.posted).length;
    // Invalidate the page cache so the submitter's refresh shows the new
    // comments immediately (rather than the up-to-60s-stale cached page).
    if (posted > 0) await c.env.RENDER_CACHE.delete(reviewCacheKey(owner, repo, pr, spec.db));
    // Top-level "Reviewed" summary comment (one per submission that posted
    // anything), so the PR thread logs each review pass.
    if (posted > 0) {
      const who = myLogin ? `@${myLogin}` : "a reviewer";
      const summary =
        `**Reviewed by ${who}.** Added ${posted} inline comment${posted === 1 ? "" : "s"}` +
        (changed ? `, including ${changed} status change${changed === 1 ? "" : "s"}` : "") +
        `. <!-- wikilean-review-summary -->`;
      await fetch(`${GH_API}/repos/${owner}/${repo}/issues/${pr}/comments`, {
        method: "POST",
        headers: { ...ghHeaders(postToken), "Content-Type": "application/json" },
        body: JSON.stringify({ body: summary }),
      }).catch(() => {});
    }
    // When a post is blocked because the GitHub App isn't installed on the org
    // ("Resource not accessible by integration"), the client points the reviewer
    // at the install page — the one owner action that unblocks in-app posting.
    const installUrl = c.env.REVIEW_GITHUB_APP_SLUG
      ? `https://github.com/apps/${c.env.REVIEW_GITHUB_APP_SLUG}/installations/new`
      : undefined;
    return c.json({ ok: true, posted, changed, results, installUrl });
  });

  // ---- dedicated review-posting OAuth flow (separate GitHub app) ----

  // Whether the current browser is connected for posting (+ the GitHub login), and —
  // when ?owner= is given — whether an in-app Submit to that org would ACTUALLY land.
  // canPost is true two ways: (1) the connected user IS the personal-PAT owner (a classic
  // PAT posts to any public repo), or (2) the WikiLean App is installed on `owner`'s org
  // (an org-owner action) so the user's user-to-server token can write there. Everyone
  // else is blocked by GitHub, so the client steers them to Copy review instead of a
  // Submit button that would 403. Per-request + uncached (it depends on this session).
  app.get("/api/review/connected", async (c) => {
    const cid = c.env.REVIEW_GITHUB_CLIENT_ID;
    const configured = !!(cid && c.env.REVIEW_GITHUB_CLIENT_SECRET);
    // GitHub App client IDs start with "Iv" (Iv1./Iv23…); a classic OAuth app's don't.
    // The two post very differently: an OAuth app's public_repo token can comment on ANY
    // public repo, while a GitHub-App user-to-server token can only write where the app
    // is installed. So canPost is derived differently per app type (below).
    const isGitHubApp = !!cid && cid.startsWith("Iv");
    const { token, login } = await reviewToken(c);
    const owner = (c.req.query("owner") || "").toLowerCase();
    let canPost = false;
    let postVia = "none";
    if (login && owner) {
      const patLogin = c.env.REVIEW_POSTING_PAT ? (await ghLogin(c.env.REVIEW_POSTING_PAT))?.toLowerCase() : null;
      if (patLogin && patLogin === login.toLowerCase()) {
        canPost = true;
        postVia = "pat";
      } else if (!isGitHubApp && token) {
        // Classic OAuth app: the reviewer's own public_repo token can comment on any
        // public PR (posting AS them, so settle.py's maintainer-by-author gate still
        // holds). Allow optimistically — an org that restricts third-party OAuth apps
        // returns a 403 on POST, which the client already collapses into the "use Copy
        // review" nudge. Copy review stays the guaranteed, no-approval path either way.
        canPost = true;
        postVia = "oauth";
      } else if (token) {
        // A GitHub-App user-to-server token lists only THIS app's installations the user
        // can see; an install on `owner`'s org means their token can write PR comments there.
        const r = await fetch(`${GH_API}/user/installations`, { headers: ghHeaders(token) });
        if (r.ok) {
          const j = (await r.json().catch(() => ({}))) as { installations?: Array<{ account?: { login?: string } }> };
          if ((j.installations || []).some((i) => (i.account?.login || "").toLowerCase() === owner)) {
            canPost = true;
            postVia = "app";
          }
        }
      }
    }
    const installUrl = c.env.REVIEW_GITHUB_APP_SLUG
      ? `https://github.com/apps/${c.env.REVIEW_GITHUB_APP_SLUG}/installations/new`
      : null;
    return c.json({ configured, connected: !!login, login: login ?? null, canPost, postVia, installUrl });
  });

  // Step 1: redirect to GitHub to authorize the review GitHub App (user-to-server).
  // MIGRATED from a public_repo OAuth app to a GitHub App (docs/ROADMAP.md P3): NO
  // `scope` param — a GitHub App derives permissions from its own config ("Pull
  // requests: write"), so authorizing grants ONLY PR-comment write, never the old
  // public_repo write-to-all. User-to-server tokens still post AS the reviewer, so
  // settle.py's maintainer-by-author gate survives. Posting to an org additionally
  // needs the App INSTALLED there (owner action — see installUrl in POST results);
  // until then Copy review is the no-approval fallback.
  app.get("/review/auth/start", (c) => {
    const cid = c.env.REVIEW_GITHUB_CLIENT_ID;
    if (!cid || !c.env.REVIEW_GITHUB_CLIENT_SECRET) {
      return c.text("Review OAuth app not configured (set REVIEW_GITHUB_CLIENT_ID/SECRET).", 503);
    }
    const returnTo = c.req.query("returnTo") || "/review";
    const safeReturn = returnTo.startsWith("/") ? returnTo : "/review"; // same-origin only
    const state = crypto.randomUUID();
    setCookie(c, REVIEW_STATE_COOKIE, state + "|" + safeReturn, {
      path: "/review/auth",
      httpOnly: true,
      secure: true,
      sameSite: "Lax",
      maxAge: 600,
    });
    const u = new URL(GH_OAUTH_AUTHORIZE);
    u.searchParams.set("client_id", cid);
    u.searchParams.set("redirect_uri", reviewBaseUrl(c) + "/review/auth/callback");
    u.searchParams.set("state", state);
    // GitHub App client IDs start with "Iv" (Iv1./Iv23…) and take NO scope — perms
    // come from the App config. A legacy/"Ov23…" OAuth-app ID still needs public_repo.
    // Sniffing the prefix keeps the migration zero-downtime: correct for whichever
    // app is configured, so swapping the secret to the GitHub App needs no code change.
    if (!cid.startsWith("Iv")) u.searchParams.set("scope", "public_repo");
    return c.redirect(u.toString());
  });

  // Step 2: exchange the code for a token, stash it in KV, set the session cookie.
  app.get("/review/auth/callback", async (c) => {
    const code = c.req.query("code");
    const state = c.req.query("state");
    const saved = getCookie(c, REVIEW_STATE_COOKIE) || "";
    deleteCookie(c, REVIEW_STATE_COOKIE, { path: "/review/auth" });
    const [savedState, returnTo] = saved.split("|");
    if (!code || !state || !savedState || state !== savedState) {
      return c.text("Invalid OAuth state — try connecting again.", 400);
    }
    // Same token endpoint for OAuth apps and GitHub Apps; ghTokenExchange also
    // captures refresh_token + expires_at when the App expires user tokens.
    const ex = await ghTokenExchange(c, { code, redirect_uri: reviewBaseUrl(c) + "/review/auth/callback" });
    if (!ex.token) return c.text("GitHub token exchange failed — try again.", 502);
    const login = await ghLogin(ex.token);
    const id = crypto.randomUUID();
    await c.env.RENDER_CACHE.put(
      `reviewtok:${id}`,
      JSON.stringify({ token: ex.token, login, refresh_token: ex.refresh_token, expires_at: ex.expires_at }),
      { expirationTtl: REVIEW_TOK_TTL },
    );
    setCookie(c, REVIEW_COOKIE, id, {
      path: "/",
      httpOnly: true,
      secure: true,
      sameSite: "Lax",
      maxAge: REVIEW_TOK_TTL,
    });
    return c.redirect((returnTo || "/review").startsWith("/") ? returnTo : "/review");
  });

  // Disconnect: drop the stored token + cookie.
  app.get("/review/auth/logout", async (c) => {
    const id = getCookie(c, REVIEW_COOKIE);
    if (id) await c.env.RENDER_CACHE.delete(`reviewtok:${id}`);
    deleteCookie(c, REVIEW_COOKIE, { path: "/" });
    return c.redirect(c.req.query("returnTo") || "/review");
  });

  // The review page (shell + client script). no-cache so a deploy's updated
  // client reaches reviewers immediately (must-revalidate, not stale browser copy).
  app.get("/review", (c) => {
    c.header("Cache-Control", "no-cache, must-revalidate");
    return c.html(reviewPageHtml());
  });
}

const EMOJI: Record<string, string> = { approve: "🟢", revise: "🟡", reject: "🔴" };

// Build the inline-comment body: traffic-light label + the reviewer's VERBATIM
// note (blockquoted), plus the idempotency marker. Identical shape to the CLI
// poster (post_review_comments.py), so both tools interoperate via the marker.
export function buildReviewCommentBody(
  qid: string,
  status: string,
  notes: string,
  wasStatus = "",
  db = "wikidata",
): string {
  const em = EMOJI[status] ?? "";
  let label = status ? `${em} WikiLean reviewer note (${status})`.trim() : "WikiLean reviewer note";
  if (status && wasStatus && wasStatus !== status) {
    const we = EMOJI[wasStatus] ?? "";
    label += ` — changed from ${we} ${wasStatus}`.replace("  ", " ");
  }
  const quoted = notes ? notes.split("\n").map((l) => "> " + l).join("\n") : "_(no note)_";
  return (
    `**${label}**\n\n${quoted}\n\n` +
    `<sub><a href="${crossRefUrl(db, qid)}">${qid}</a> ` +
    `<!-- ${crossRefReviewMarker(db, qid)} --></sub>`
  );
}

// ---- pasted top-level reviews ("Copy review for GitHub" output) ----
//
// The web tool's copy-paste flow assembles a "## WikiLean review" Markdown list
// (buildClipboardReview), which the reviewer pastes as a single top-level PR
// comment. Those live under /issues/:pr/comments (not the inline
// /pulls/:pr/comments the tool normally reads), so we fetch them separately,
// recognize them, split them back into per-qid entries, and re-emit each in the
// inline-comment shape so the existing per-card rendering picks them up.

function isWikiLeanReview(body: string): boolean {
  return /^\s*##\s*WikiLean review/m.test(body) || /wikilean\.jackmccarthy\.org\/review/.test(body);
}

export interface PastedEntry {
  qid: string;
  db: string;
  status: string; // approve | revise | reject | flag | ""
  note: string;
}

// Parse a pasted "## WikiLean review" comment back into per-qid {status, note}.
// Mirrors the client's buildClipboardReview format (entry header + indented
// `- status:` and note sub-bullets).
export function parseClipboardReview(body: string, db = "wikidata"): PastedEntry[] {
  const spec = crossRefSpec(db) ?? crossRefSpec("wikidata")!;
  const out: PastedEntry[] = [];
  let cur: PastedEntry | null = null;
  let note: string[] = [];
  const flush = () => {
    if (cur) {
      cur.note = note.join("\n").trim();
      out.push(cur);
    }
  };
  // Normalize CRLF→LF first: GitHub serves comment bodies with \r\n, and a
  // trailing \r breaks the note regex's `$` anchor (`.` doesn't match \r),
  // which would drop every note (and any note-only entry entirely).
  for (const ln of body.replace(/\r\n?/g, "\n").split("\n")) {
    const h = ln.match(new RegExp(`^-\\s*\\*\\*\\[(${spec.idSource})\\]`));
    if (h) {
      flush();
      cur = { qid: h[1], db: spec.db, status: "", note: "" };
      note = [];
      continue;
    }
    if (!cur) continue;
    const st = ln.match(/^\s*-\s*status:[^\n]*\*\*(approve|revise|reject|flag)\*\*/i);
    if (st) {
      cur.status = st[1].toLowerCase();
      note = [];
      continue;
    }
    const nb = ln.match(/^\s*-\s+(.*)$/);
    if (nb) {
      note = [nb[1]];
      continue;
    }
    if (note.length && /^\s+\S/.test(ln)) note.push(ln.trim());
  }
  flush();
  return out.filter((e) => e.status || e.note);
}

// Re-emit a parsed pasted entry in the inline-comment body shape so the client's
// parseStatus/reviewNote handle it identically to a real inline review comment.
function synthReviewBody(db: string, qid: string, status: string, note: string): string {
  if (status === "flag") {
    const quoted = note ? note.split("\n").map((l) => "> " + l).join("\n") : "_(no note)_";
    return (
      `**⚠️ WikiLean reviewer note (Deletion candidate)**\n\n${quoted}\n\n` +
      `<sub><a href="${crossRefUrl(db, qid)}">${qid}</a> <!-- ${crossRefReviewMarker(db, qid)} --></sub>`
    );
  }
  return buildReviewCommentBody(qid, status, note, "", db);
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
<title>WikiLean · crossref review</title>
<style>
/* JuliaMono — full Lean glyph coverage (subscript letters like ₗ in →ₗ, 𝕜, ↪, ⋀, …)
   that system monospace fonts lack. font-display:swap → text shows immediately
   in a fallback, then swaps in once loaded (cached after first visit). */
@font-face{font-family:"JuliaMono";font-style:normal;font-weight:400;font-display:swap;
  src:url("https://cdn.jsdelivr.net/gh/cormullion/juliamono@v0.058/webfonts/JuliaMono-Regular.woff2") format("woff2");}
:root{--bg:#faf7f1;--card:#fffdf9;--rule:#e3dccb;--ink:#1f1d1a;--muted:#6b6457;--accent:#7a3d2a;--code:#f3efe6;
      --g:#2d7a4a;--y:#b77a14;--r:#a02828;--gb:#e8f4ec;--yb:#fbf3e0;--rb:#fbe8e8;}
*{box-sizing:border-box}
body{font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;color:var(--ink);background:var(--bg);margin:0;padding:1.5rem 1rem 5rem}
.wrap{max-width:1100px;margin:0 auto}
h1{font-size:1.4rem;margin:0 0 .3rem}
.lede{color:var(--muted);margin:0 0 1.3rem}
form.load{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;margin:0 0 1.5rem;background:var(--card);border:1px solid var(--rule);border-radius:8px;padding:.8rem}
form.load input,form.load select{font:inherit;padding:.45rem .6rem;border:1px solid var(--rule);border-radius:6px;background:#fff}
form.load input#repo{width:230px}form.load input#pr{width:90px}
form.load button{font:inherit;font-weight:600;padding:.45rem 1rem;border:1px solid var(--accent);background:var(--accent);color:#fff;border-radius:6px;cursor:pointer}
#status{color:var(--muted);font-size:.9rem;margin:.5rem 0}
.entry{background:var(--card);border:1px solid var(--rule);border-left:5px solid var(--rule);border-radius:6px;margin:1rem 0;overflow:hidden}
.entry[data-status=approve]{border-left-color:var(--g)}.entry[data-status=revise]{border-left-color:var(--y)}.entry[data-status=reject]{border-left-color:var(--r)}
.entry header{display:flex;gap:.5rem;align-items:baseline;flex-wrap:wrap;padding:.6rem .9rem;background:#f6f2e9;border-bottom:1px solid var(--rule)}
.entry header .qid a{font-family:"SF Mono",Menlo,monospace;color:var(--accent);text-decoration:none;font-size:.85rem}
.entry header .loc{color:var(--muted);font-size:.82rem;font-family:"SF Mono",Menlo,monospace;text-decoration:none}
.entry header .loc:hover{color:var(--accent);text-decoration:underline}
.entry header .loc-src{color:var(--muted);font-size:.74rem;font-family:"SF Mono",Menlo,monospace;text-decoration:none;opacity:.7;margin-left:.4rem}
.entry header .loc-src:hover{color:var(--accent);opacity:1;text-decoration:underline}
pre.lean{font-family:"JuliaMono","JetBrains Mono","SF Mono",Menlo,Consolas,monospace;font-size:.82rem;background:var(--code);margin:0;padding:.7rem .9rem;overflow:auto;white-space:pre-wrap;border-bottom:1px solid var(--rule)}
.comments{padding:.5rem .9rem;display:flex;flex-direction:column;gap:.5rem}
.cmt{font-size:.88rem;background:#fbf9f3;border:1px solid var(--rule);border-radius:6px;padding:.45rem .6rem}
.cmt .who{font-weight:600;font-size:.8rem;color:var(--muted)}
.cmt .body{white-space:pre-wrap;margin-top:.2rem}
.cmt .body.md{white-space:normal}
.cmt .body.md p{margin:.2rem 0}
.cmt .body.md blockquote{margin:.3rem 0;padding:.1rem .7rem;border-left:3px solid var(--rule);color:#4a463c}
.cmt .body.md code{background:var(--code);padding:.05em .35em;border-radius:3px;font-family:"JuliaMono","SF Mono",Menlo,monospace;font-size:.92em}
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
/* Standard Lean palette — VS Code "Light+" token colors (how Lean looks in
   the default editor). */
pre.lean .sd{color:#008000;font-style:italic}
pre.lean .c1{color:#008000;font-style:italic}
pre.lean .kn{color:#0000ff}
pre.lean .kt{color:#267f99}
pre.lean .nf{color:#795e26}
pre.lean .o{color:#000000}
pre.lean .s{color:#a31515}
pre.lean .mi{color:#098658}
pre.lean .n{color:#1f1f1f}
@media (max-width:820px){.panes{grid-template-columns:1fr}.src{border-right:none;border-bottom:1px solid var(--rule)}}
#controls{margin:0 0 1rem;display:flex;gap:1rem;align-items:center;flex-wrap:wrap;font-size:.9rem}
#controls select{font:inherit;padding:.25rem .4rem;border:1px solid var(--rule);border-radius:6px;background:#fff}
#open-all{font:inherit;font-size:.83rem;padding:.3rem .7rem;border:1px solid var(--accent);background:#fff;color:var(--accent);border-radius:6px;cursor:pointer}
#open-all:hover{background:#fbf6ec}
.entry.pending{box-shadow:inset 3px 0 0 #1a4b8c}
.cur{font-size:.88rem;margin-bottom:.45rem}
.cur-label{font-weight:600;color:var(--muted);margin-right:.4rem;font-size:.82rem;text-transform:uppercase;letter-spacing:.03em}
.rev-item{margin:.25rem 0}
.rev-note{margin:.15rem 0 .15rem 1.1rem;font-size:.9rem}
.rev-note p{margin:.15rem 0}
.rev-note blockquote{margin:.2rem 0;padding:.05rem .6rem;border-left:3px solid var(--rule);color:#4a463c}
.rev-note code{background:var(--code);padding:.05em .35em;border-radius:3px;font-family:"JuliaMono","SF Mono",Menlo,monospace;font-size:.92em}
.rev-note a{color:var(--accent)}
.comments-label{font-weight:600;color:var(--muted);font-size:.82rem;text-transform:uppercase;letter-spacing:.03em}
.cur .badge{padding:.1rem .5rem;border-radius:12px;font-weight:600}
.cur .badge.approve{background:var(--gb);color:var(--g)}
.cur .badge.revise{background:var(--yb);color:var(--y)}
.cur .badge.reject{background:var(--rb);color:var(--r)}
.cur .badge.flag{background:#f3e8fb;color:#6b1f9c}
.cur .badge.none{background:#eee;color:var(--muted)}
.cur .by{color:var(--muted)}
.acts{display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:.4rem}
.acts button{font:inherit;font-size:.83rem;padding:.3rem .7rem;border:1px solid var(--accent);background:#fff;color:var(--accent);border-radius:6px;cursor:pointer}
.acts button:hover{background:#fbf6ec}
.acts button.on{background:var(--accent);color:#fff}
.acts button.act-clear{border-color:var(--rule);color:var(--muted)}
.acts button.act-clear:hover{background:#f4efe6;color:var(--ink)}
.form{padding:.6rem .9rem;border-top:1px dashed var(--rule);background:#fcfaf4}
.form .row{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;margin-bottom:.4rem}
.form .row[hidden]{display:none}
.form label{display:inline-flex;align-items:center;gap:.3rem;padding:.15rem .55rem;border:1px solid var(--rule);border-radius:14px;background:#fff;cursor:pointer;font-size:.85rem}
.form label:has(input[value=approve]:checked){background:var(--gb);border-color:var(--g);color:var(--g);font-weight:600}
.form label:has(input[value=revise]:checked){background:var(--yb);border-color:var(--y);color:var(--y);font-weight:600}
.form label:has(input[value=reject]:checked){background:var(--rb);border-color:var(--r);color:var(--r);font-weight:600}
.form textarea{width:100%;min-height:44px;font:inherit;font-size:.88rem;padding:.4rem .5rem;border:1px solid var(--rule);border-radius:6px;resize:vertical}
#bar{position:fixed;left:0;right:0;bottom:0;background:var(--card);border-top:1px solid var(--rule);padding:.6rem 1.3rem;display:flex;justify-content:space-between;align-items:center;gap:1rem;box-shadow:0 -2px 8px rgba(0,0,0,.06)}
#bar .counts span{padding:.1rem .5rem;border-radius:12px;font-size:.85rem;margin-right:.4rem}
#bar button{font:inherit;font-weight:600;padding:.4rem 1rem;border:1px solid var(--accent);background:var(--accent);color:#fff;border-radius:6px;cursor:pointer}
#bar #submit-gh{background:#fff;color:var(--accent)}
#bar button:disabled{opacity:.5;cursor:default}
.note{font-size:.8rem;color:var(--muted)}
</style></head>
<body><div class="wrap">
<h1>WikiLean · <code id="attr-title">crossref</code> review</h1>
<p class="lede">Paste a pull request; review each tagged declaration with its existing GitHub comments, then submit your decisions.</p>
<form class="load" onsubmit="return false">
  <select id="db" autocomplete="off">
    <option value="wikidata">Wikidata</option>
    <option value="lmfdb">LMFDB</option>
  </select>
  <input id="repo" placeholder="owner/repo" value="leanprover-community/mathlib4" autocomplete="off">
  <input id="pr" placeholder="PR #" inputmode="numeric" autocomplete="off">
  <button id="load">Load PR</button>
  <span class="note">e.g. <code>leanprover-community/mathlib4</code></span>
</form>
<div id="status"></div>
<div id="controls" hidden>
  <label>Filter by your review:
    <select id="filter">
      <option value="all">All</option>
      <option value="approve">🟢 approve</option>
      <option value="reject">🔴 reject</option>
      <option value="flag">⚠️ deletion-candidate</option>
      <option value="none">◯ no review yet</option>
    </select>
  </label>
  <button id="open-all" type="button">Open all reviews</button>
  <span id="dist" class="note"></span>
</div>
<div id="entries"></div>
</div>
<div id="bar" hidden>
  <div class="counts">
    <span id="c-pending" style="background:#e8edf7;color:#1a4b8c">0 changes pending</span>
  </div>
  <div>
    <span class="note" id="submit-note"></span>
    <button id="submit">📋 Copy review</button>
    <button id="submit-gh">✅ Submit to GitHub</button>
  </div>
</div>
<dialog id="copybox">
  <p style="margin:.2rem 0 .5rem">Paste this as a comment on <a id="pr-link" href="#" target="_blank" rel="noopener">the PR ↗</a>:</p>
  <textarea id="copytext" readonly style="width:min(80vw,640px);height:40vh;font:13px/1.5 'SF Mono',Menlo,monospace;border:1px solid #d8d0bd;border-radius:6px;padding:.6rem"></textarea>
  <div style="text-align:right;margin-top:.5rem"><button id="copybox-close" style="font:inherit;padding:.4rem 1rem;border:1px solid #7a3d2a;background:#7a3d2a;color:#fff;border-radius:6px;cursor:pointer">Done</button></div>
</dialog>
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
const reEsc = (s) => (s||"").replace(/[.*+?^$()|[\]\\]/g, "\\$&").replace(/[{}]/g, "\\$&");
const DB = {
  wikidata: {label:"Wikidata", attr:"wikidata", url:"https://www.wikidata.org/wiki/"},
  lmfdb: {label:"LMFDB", attr:"lmfdb", url:"https://www.lmfdb.org/knowledge/show/"}
};
let STATE = {};   // qid -> {status, notes}
let KEY = "";
let CUR = null;   // {owner, repo, pr} of the loaded PR
let DATA = null;  // last loaded payload (for re-render on filter change)

function storageKey(repo, pr, db){ return "wl-review:" + (db||"wikidata") + ":" + repo + "#" + pr; }
function load(){ try { return JSON.parse(localStorage.getItem(KEY) || "{}"); } catch(e){ return {}; } }
function save(){ localStorage.setItem(KEY, JSON.stringify(STATE)); }

async function loadPR(){
  const repo = $("#repo").value.trim();
  const pr = $("#pr").value.trim();
  const db = ($("#db") && $("#db").value) || "wikidata";
  const m = repo.match(/^([\w.-]+)\/([\w.-]+)$/);
  if(!m || !pr){ $("#status").textContent = "Enter owner/repo and a PR number."; return; }
  KEY = storageKey(repo, pr, db); STATE = load();
  CUR = { owner: m[1], repo: m[2], pr: pr, db: db };
  $("#status").textContent = "Loading " + repo + " #" + pr + "…";
  $("#entries").innerHTML = "";
  try {
    const r = await fetch("/api/review/" + m[1] + "/" + m[2] + "/" + pr + "?db=" + encodeURIComponent(db));
    const data = await r.json();
    if(!data.ok){ $("#status").textContent = "Error: " + data.error; return; }
    render(data);
    refreshConnected();   // re-check canPost now that the PR's org is known
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

const EMO = {approve:"🟢",revise:"🟡",reject:"🔴",flag:"⚠️"};

function selectedDb(){ return (DATA && DATA.db) || (($("#db") && $("#db").value) || "wikidata"); }
function reviewMarkerRe(){
  const db = selectedDb();
  return db === "wikidata" ? /wikilean-review:(?:wikidata:)?Q\d+/ : new RegExp("wikilean-review:"+reEsc(db)+":");
}
function crossrefBotRe(){
  const db = selectedDb();
  return db === "wikidata" ? /crossref-bot:(?:wikidata:)?Q\d+/ : new RegExp("crossref-bot:"+reEsc(db)+":");
}
function isReviewCommentBody(body){ return reviewMarkerRe().test(body||""); }

// Your existing review status for a decl, parsed from the latest
// wikilean-review GitHub comment (the one you posted via the CLI/web tool).
function parseStatus(comments){
  let best=null;
  (comments||[]).forEach(c=>{ if(isReviewCommentBody(c.body)){
    if(!best || (c.created_at||"") > (best.created_at||"")) best=c; } });
  if(!best) return {status:"", by:""};
  let status="";
  if(/Deletion candidate/.test(best.body)) status="flag";
  else { const m=best.body.match(/\((approve|revise|reject)\)/); if(m) status=m[1]; }
  return {status, by:best.user||""};
}
function statusBadge(s){
  if(!s) return '<span class="badge none">◯ no review yet</span>';
  const label = s==="flag" ? "deletion-candidate" : s;
  return '<span class="badge '+s+'">'+(EMO[s]||"")+' '+label+'</span>';
}

// For a wikilean-review comment, return just the NOTE html (the status label +
// marker are already shown by the "Existing review" row, so they'd be
// redundant). Returns "" for a status-only comment (nothing to add).
function reviewNote(c){
  let h = c.bodyHtml || "";
  if(!h){
    const lines=(c.body||"").split("\n").filter(l=>!/^\*\*/.test(l.trim())&&!/<sub>/.test(l)&&l.trim()!=="");
    const t=lines.map(l=>l.replace(/^>\s?/,"")).join("\n").trim();
    return (t && t!=="_(no note)_") ? "<p>"+esc(t)+"</p>" : "";
  }
  h = h.replace(/<p[^>]*>\s*<strong>[^<]*(WikiLean reviewer note|Deletion candidate)[^<]*<\/strong>[^<]*<\/p>/i,"");
  h = h.replace(/<p[^>]*>\s*<sub>[\s\S]*?<\/sub>\s*<\/p>/i,"");
  h = h.replace(/<sub>[\s\S]*?<\/sub>/i,"");
  h = h.trim();
  if(h==="" || /^<p[^>]*>\s*<em>\(no note\)<\/em>\s*<\/p>$/i.test(h)) return "";
  return h;
}
// Render one comment; review comments show note-only (or are dropped), others full.
function commentHtml(c){
  if(isReviewCommentBody(c.body)){
    const note = reviewNote(c);
    if(!note) return "";
    return '<div class="cmt"><div class="who">'+esc(c.user)+'</div><div class="body md">'+note+'</div></div>';
  }
  return '<div class="cmt"><div class="who">'+esc(c.user)+'</div>'+
    (c.bodyHtml ? '<div class="body md">'+c.bodyHtml+'</div>' : '<div class="body">'+esc(c.body)+'</div>')+'</div>';
}

// All verdicts for a decl, per reviewer — combining wikilean-review comments AND
// 👍/👎 reactions on the crossref per-tag comment, latest-wins per login (a reaction
// can overturn an earlier comment by the same person, and vice-versa).
function declVerdicts(d){
  const by = {}; // login -> {status, at, note, reaction}
  (d.comments||[]).forEach(c => {
    if(isReviewCommentBody(c.body)){
      let s=""; if(/Deletion candidate/.test(c.body)) s="flag";
      else { const m=c.body.match(/\((approve|revise|reject)\)/); if(m) s=m[1]; }
      const login=c.user||"", at=c.created_at||"";
      if(s && login){ const p=by[login]; if(!p||at>p.at) by[login]={status:s, at, note:reviewNote(c), reaction:false}; }
    }
    (c.reactionVerdicts||[]).forEach(rv => {
      const p=by[rv.user];
      if(rv.user && (!p || (rv.created_at||"")>(p.at||""))) by[rv.user]={status:rv.verdict, at:rv.created_at||"", note:"", reaction:true};
    });
  });
  return by;
}
// The decl's headline status = the most recent verdict across all reviewers/sources.
function declStatus(d){
  let best=null; const v=declVerdicts(d);
  Object.keys(v).forEach(login => { const x=v[login]; if(!best || (x.at||"")>(best.at||"")) best={status:x.status, by:login, at:x.at}; });
  return best ? {status:best.status, by:best.by} : {status:"", by:""};
}

function render(data){
  DATA = data;
  const dbInfo = DB[data.db] || {label:data.dbLabel||data.db||"Crossref", attr:data.attr||"crossref", url:""};
  const titleAttr = $("#attr-title"); if(titleAttr) titleAttr.textContent = "@[" + dbInfo.attr + "]";
  // Per-decl existing status (latest verdict — comment OR reaction).
  const cur = data.decls.map(declStatus);
  // Distribution across ALL decls.
  const dist = {approve:0,revise:0,reject:0,flag:0,none:0};
  cur.forEach(c => dist[c.status||"none"]++);
  $("#dist").textContent = "🟢 "+dist.approve+" · 🟡 "+dist.revise+" · 🔴 "+dist.reject+
    " · ⚠️ "+dist.flag+" · ◯ "+dist.none;
  $("#controls").hidden = false;
  $("#status").innerHTML = "<b>" + esc(data.title) + "</b> — " + data.decls.length +
    " <code>@[" + esc(dbInfo.attr) + "]</code> declarations · commit <code>" + data.head_sha.slice(0,10) + "</code>" +
    ' · <a href="' + prUrl() + '" target="_blank" rel="noopener">' + esc(data.repo) + ' #' + data.pr + ' ↗</a>';

  const filter = $("#filter").value;
  const root = $("#entries"); root.innerHTML = "";
  data.decls.forEach((d, i) => {
    const cs = cur[i].status;                       // your existing status
    if(filter!=="all" && (filter==="none" ? cs!=="" : cs!==filter)) return;
    const st = (STATE[d.qid] || {});                // reviewer's pending input
    const pending = !!(st.changeStatus || (st.note && st.note.trim()));
    const el = document.createElement("article");
    el.className = "entry" + (pending?" pending":""); el.dataset.status = cs || "";
    // Split existing comments: WikiLean reviews (folded into the "Reviews"
    // block, status + note together) vs. other discussion. Johan's crossref-bot
    // comments are dropped from "Other comments" — they just restate the
    // external database label/description already shown in the card pane.
    const otherCmts = d.comments.filter(c =>
      !isReviewCommentBody(c.body) && !crossrefBotRe().test(c.body||""));
    const otherHtml = otherCmts.map(commentHtml).filter(Boolean).join("");
    // Reviews block: one row per reviewer, written verdicts AND 👍/👎 reactions, newest first.
    const verds = declVerdicts(d);
    const verdLogins = Object.keys(verds).sort((a,b) => (verds[b].at||"").localeCompare(verds[a].at||""));
    let reviewBlock;
    if(verdLogins.length){
      reviewBlock = '<div class="cur"><span class="cur-label">Reviews</span>' +
        verdLogins.map(login => { const x = verds[login];
          return '<div class="rev-item">' + statusBadge(x.status) +
            ' <span class="by">@' + esc(login) + '</span>' +
            (x.reaction ? ' <span class="by" title="via 👍/👎 reaction">(reacted)</span>' : '') +
            (x.note ? '<div class="rev-note md">' + x.note + '</div>' : '') + '</div>';
        }).join("") + '</div>';
    } else {
      reviewBlock = '<div class="cur"><span class="cur-label">Reviews</span> ' + statusBadge("") + '</div>';
    }
    const ext = d.external || {};
    const extUrl = ext.url || (dbInfo.url ? dbInfo.url + encodeURIComponent(d.id || d.qid) : "#");
    const contextUrl = ext.contextUrl || extUrl;
    const wikiHead = '<p class="wd-head"><a href="' + contextUrl + '" target="_blank">' + esc(ext.title || ext.label || d.id || d.qid) + '</a></p>';
    const descHtml = ext.description
      ? '<p class="wd-desc">' + esc(ext.description) + '</p>'
      : '<p class="wd-desc empty">(no ' + esc(dbInfo.label) + ' description)</p>';
    const leadHtml = ext.lead
      ? '<details class="wd-lead"><summary>Wikipedia lead ↓</summary><p>' + esc(ext.lead.slice(0,900)) + '</p>' +
        (contextUrl ? '<p class="more"><a href="' + contextUrl + '" target="_blank">Read full article ↗</a></p>' : '') +
        '</details>'
      : (contextUrl ? '<p class="wd-lead"><a href="' + contextUrl + '" target="_blank">Open in ' + esc(dbInfo.label) + ' ↗</a></p>' : '');
    const showStatus = !!st.changeStatus;
    const showNote = !!(st.note && st.note.length);
    // file:line links to the Mathlib docs for the decl (module page + #name
    // anchor); a small "src ↗" links to the exact line of GitHub source. For a
    // non-Mathlib file (no docs page) the location itself links to source.
    // Link to the canonical decl on master (not the PR branch): the crossref
    // PR only ADDS the attribute line; the reviewer wants the merged declaration.
    const srcUrl = "https://github.com/" + data.repo + "/blob/master/" + d.file + "#L" + d.line;
    const docsUrl = /^Mathlib\//.test(d.file || "")
      ? "https://leanprover-community.github.io/mathlib4_docs/" + d.file.replace(/\.lean$/, ".html") +
        (d.decl ? "#" + encodeURIComponent(d.decl) : "")
      : null;
    const locHtml = docsUrl
      ? '<a class="loc" href="' + docsUrl + '" target="_blank" rel="noopener" title="Open in Mathlib docs">' + esc(d.file) + ':' + d.line + '</a>' +
        ' <a class="loc-src" href="' + srcUrl + '" target="_blank" rel="noopener" title="View source on GitHub">src&nbsp;↗</a>'
      : '<a class="loc" href="' + srcUrl + '" target="_blank" rel="noopener" title="View source on GitHub">' + esc(d.file) + ':' + d.line + '</a>';
    el.innerHTML =
      '<header><span class="qid"><a href="' + extUrl +
        '" target="_blank">' + esc(d.id || d.qid) + '</a></span>' +
        locHtml + '</header>' +
      '<div class="panes">' +
        '<section class="src"><pre class="lean">' + hl(d.source || "") + '</pre></section>' +
        '<section class="wiki-pane">' + wikiHead + descHtml + leadHtml + '</section>' +
      '</div>' +
      (otherHtml ? '<div class="comments"><div class="comments-label">Other comments</div>' + otherHtml + '</div>' : '') +
      '<div class="form">' +
        reviewBlock +
        '<div class="acts">' +
          '<button class="act-status' + (showStatus?" on":"") + '" data-qid="' + d.qid + '">Add Review</button>' +
          '<button class="act-note' + (showNote?" on":"") + '" data-qid="' + d.qid + '">Add note</button>' +
          '<button class="act-clear" data-qid="' + d.qid + '"' + (showStatus||showNote?"":" hidden") + '>Clear</button>' +
        '</div>' +
        '<div class="row status-ctrl" data-qid="' + d.qid + '"' + (showStatus?"":" hidden") + '>' +
          radio(d.qid,"approve","🟢 Approve",st.changeStatus) +
          radio(d.qid,"reject","🔴 Reject",st.changeStatus) +
        '</div>' +
        '<textarea class="note-ctrl" data-qid="' + d.qid + '"' + (showNote?"":" hidden") +
          ' placeholder="Your note (verbatim, posted to GitHub)…">' + esc(st.note||"") + '</textarea>' +
      '</div>';
    root.appendChild(el);
  });
  // Wire actions.
  root.querySelectorAll('.act-status').forEach(b => b.addEventListener("click", e => {
    const qid=e.target.dataset.qid, ctrl=root.querySelector('.status-ctrl[data-qid="'+qid+'"]');
    ctrl.hidden = !ctrl.hidden; e.target.classList.toggle("on", !ctrl.hidden); syncOpenAll(); }));
  root.querySelectorAll('.act-note').forEach(b => b.addEventListener("click", e => {
    const qid=e.target.dataset.qid, ta=root.querySelector('.note-ctrl[data-qid="'+qid+'"]');
    ta.hidden = !ta.hidden; e.target.classList.toggle("on", !ta.hidden); if(!ta.hidden) ta.focus(); }));
  // Click (not change) so clicking the already-selected option deselects it.
  root.querySelectorAll('.status-ctrl input[type=radio]').forEach(r =>
    r.addEventListener("click", e => {
      const qid=e.target.dataset.qid, val=e.target.value;
      if(((STATE[qid]||{}).changeStatus)===val){ e.target.checked=false; set(qid,"changeStatus",""); }
      else set(qid,"changeStatus",val);
    }));
  root.querySelectorAll('textarea.note-ctrl').forEach(t =>
    t.addEventListener("input", e => set(e.target.dataset.qid, "note", e.target.value)));
  // Clear: drop this decl's status + note entirely (it leaves the summary).
  root.querySelectorAll('.act-clear').forEach(b => b.addEventListener("click", e => {
    const qid=e.target.dataset.qid; delete STATE[qid]; save();
    const card=e.target.closest('.entry');
    card.querySelectorAll('.status-ctrl input[type=radio]').forEach(r => { r.checked=false; });
    const ta=card.querySelector('textarea.note-ctrl'); if(ta) ta.value="";
    card.classList.remove("pending"); e.target.hidden=true; counts();
  }));
  $("#bar").hidden = false; counts(); syncOpenAll();
}

// Open (or close) every "Add Review" form at once. Toggles based on whether any
// are currently closed: any closed → open all; all open → close all.
function toggleAllReviews(){
  const root = $("#entries");
  const ctrls = [...root.querySelectorAll('.status-ctrl')];
  if(!ctrls.length) return;
  const anyHidden = ctrls.some(c => c.hidden);
  ctrls.forEach(c => { c.hidden = !anyHidden; });
  root.querySelectorAll('.act-status').forEach(b => b.classList.toggle("on", anyHidden));
  syncOpenAll();
}
function syncOpenAll(){
  const oa = $("#open-all"); if(!oa) return;
  const ctrls = [...$("#entries").querySelectorAll('.status-ctrl')];
  const allOpen = ctrls.length > 0 && ctrls.every(c => !c.hidden);
  oa.textContent = allOpen ? "Close all reviews" : "Open all reviews";
}

function radio(qid, val, label, cur){
  return '<label><input type="radio" name="r-' + qid + '" value="' + val + '" data-qid="' + qid +
    '"' + (cur===val?" checked":"") + '>' + label + '</label>';
}
function set(qid, field, value){
  STATE[qid] = STATE[qid] || {}; STATE[qid][field] = value; save();
  const el = [...document.querySelectorAll(".entry")].find(e => {
    const b=e.querySelector('.act-status'); return b && b.dataset.qid===qid; });
  if(el){ const st=STATE[qid]; const pend=!!(st.changeStatus || (st.note&&st.note.trim()));
    el.classList.toggle("pending", pend);
    const cb=el.querySelector('.act-clear'); if(cb) cb.hidden=!pend; }
  counts();
}
function counts(){
  let pending=0;
  Object.keys(STATE).forEach(qid => { const s=STATE[qid]||{};
    if(s.changeStatus || (s.note && s.note.trim())) pending++; });
  $("#c-pending").textContent = pending + (pending===1?" change":" changes") + " pending";
}

// Assemble the reviewer's pending changes/notes into a Markdown review to paste
// as a PR comment. (Short-term, scope-free alternative to auto-posting — see
// the GitHub-App TODO in auth.ts.)
function buildClipboardReview(){
  if(!DATA) return "";
  const dbInfo = DB[DATA.db] || {label:DATA.dbLabel||DATA.db||"Crossref", attr:DATA.attr||"crossref", url:""};
  const meta = {};
  DATA.decls.forEach(d => { meta[d.qid] = {
    label: (d.external && (d.external.title || d.external.label)) || (d.wd && (d.wd.enwikiTitle || d.wd.label)) || "",
    url: (d.external && d.external.url) || (dbInfo.url ? dbInfo.url + encodeURIComponent(d.id || d.qid) : ""),
    loc: d.file + ":" + d.line,
    was: parseStatus(d.comments).status,
  };});
  const bt = String.fromCharCode(96); // backtick char, built indirectly to avoid breaking String.raw
  const items = [];
  Object.keys(STATE).forEach(qid => {
    const s = STATE[qid] || {}; const ch=(s.changeStatus||"").trim(); const note=(s.note||"").trim();
    if(!ch && !note) return;
    const m = meta[qid] || {label:"",url:"",loc:"",was:""};
    let line = "- **["+qid+"]("+(m.url||"#")+")**" +
      (m.label?(" "+m.label):"") + (m.loc?(" — "+bt+m.loc+bt):"");
    if(ch){ const e=EMO[ch]||""; line += "\n  - status: "+e+" **"+ch+"**" +
      (m.was && m.was!==ch ? (" _(was "+(EMO[m.was]||"")+" "+m.was+")_") : ""); }
    if(note){ line += "\n  - "+note.split("\n").join("\n    "); }
    items.push(line);
  });
  if(!items.length) return "";
  // PR-specific review-page link (Johan's request): a GitHub reader who doesn't
  // know the tool can click straight through to THIS PR's review page, which
  // reads ?repo=&pr= and auto-loads (see init below). Keeping the
  // "wikilean.jackmccarthy.org/review" substring also keeps isWikiLeanReview()
  // recognizing the pasted comment.
  const reviewUrl = "https://wikilean.jackmccarthy.org/review?repo=" +
    encodeURIComponent(DATA.repo) + "&pr=" + DATA.pr + (DATA.db && DATA.db!=="wikidata" ? "&db="+encodeURIComponent(DATA.db) : "");
  return "## WikiLean review\n\n" + items.join("\n") +
    "\n\n<sub>Generated by the WikiLean review tool — [review this PR](" + reviewUrl + ")</sub>";
}

function prUrl(){ return DATA ? "https://github.com/" + DATA.repo + "/pull/" + DATA.pr : "#"; }

async function copyReview(){
  const md = buildClipboardReview();
  if(!md){ $("#submit-note").textContent = "Nothing to copy — set a status change or add a note first."; return; }
  const link = $("#pr-link"); if(link) link.href = prUrl();
  try {
    await navigator.clipboard.writeText(md);
    $("#submit-note").innerHTML = '✓ Copied — paste it as a comment on <a href="' + prUrl() + '" target="_blank" rel="noopener">the PR ↗</a>.';
  } catch(e){
    // Clipboard blocked → show a dialog with the text to copy manually.
    const ta = $("#copytext"); ta.value = md; const dlg = $("#copybox");
    if(dlg.showModal) dlg.showModal(); ta.focus(); ta.select();
    $("#submit-note").textContent = "Select-all + copy from the box.";
  }
}

// --- dedicated review-posting connection state (separate from wiki login) ---
let GH_CONFIGURED=false, GH_CONNECTED=false, GH_LOGIN="", GH_CANPOST=false, GH_INSTALL_URL="";
async function refreshConnected(){
  try {
    const owner = DATA ? DATA.repo.split("/")[0] : "";   // canPost is per-org
    const r = await fetch("/api/review/connected" + (owner ? "?owner="+encodeURIComponent(owner) : ""));
    const j = await r.json();
    GH_CONFIGURED=!!j.configured; GH_CONNECTED=!!j.connected; GH_LOGIN=j.login||"";
    GH_CANPOST=!!j.canPost; GH_INSTALL_URL=j.installUrl||"";
  } catch(e){}
  updateSubmitBtn();
}
function updateSubmitBtn(){
  const b = $("#submit-gh"); if(!b) return;
  b.disabled=false; b.style.opacity=""; b.style.cursor="";
  const note = document.getElementById("submit-note");
  if(GH_CONFIGURED && GH_CONNECTED && GH_CANPOST){
    b.textContent = "✅ Submit as @"+GH_LOGIN; b.title="Post your review as inline PR comments";
    // Keep Copy review visible as the reliable path (a Submit can still 403 on an
    // org that restricts the app/OAuth). Only fill when idle — never stomp a
    // transient "Posting…/✓ Posted" message that submitToGitHub owns.
    if(note && !note.textContent.trim()) note.innerHTML = "Posts as <b>@"+esc(GH_LOGIN)+"</b> — or <b>📋 Copy review</b> to paste it yourself.";
  } else if(GH_CONFIGURED && GH_CONNECTED){
    // Connected, but GitHub will block a post here (not the configured poster AND the app
    // isn't installed on this org). Don't offer a Submit that 403s — disable it and steer
    // to Copy review, which posts as the reviewer in-browser with no install.
    const org = DATA ? DATA.repo.split("/")[0] : "this org";
    b.textContent = "🔒 Submit unavailable here";
    b.title = "GitHub blocks in-app posting for @"+GH_LOGIN+" on "+org+" — the WikiLean review app isn’t installed there. Use “📋 Copy review” (it posts as you).";
    b.disabled=true; b.style.opacity=".5"; b.style.cursor="not-allowed";
    if(note) note.innerHTML = "In-app Submit isn’t available for <b>@"+esc(GH_LOGIN)+"</b> on "+esc(org)+" — use <b>📋 Copy review</b> (posts as you, no install needed)."
      + (GH_INSTALL_URL ? " An org owner can enable it by <a href=\""+GH_INSTALL_URL+"\" target=\"_blank\" rel=\"noopener\">installing the app</a>." : "");
  } else if(GH_CONFIGURED){
    b.textContent = "🔗 Connect GitHub to post"; b.title="Authorize the review app to post on your behalf";
  } else {
    b.textContent = "✅ Submit to GitHub"; b.title="In-app posting isn’t set up yet — use Copy review";
  }
}
function connectGitHub(){
  // Carry the loaded PR in returnTo so the page re-loads it after the OAuth
  // round-trip; pending decisions persist in localStorage (keyed by repo#pr).
  const ret = DATA ? ("/review?repo="+encodeURIComponent(DATA.repo)+"&pr="+DATA.pr+
    (DATA.db && DATA.db!=="wikidata" ? "&db="+encodeURIComponent(DATA.db) : "")) : (location.pathname+location.search);
  location.href = "/review/auth/start?returnTo=" + encodeURIComponent(ret);
}

// Post pending decisions as inline PR comments via the dedicated review OAuth
// token. If not connected, kick off the Connect-GitHub flow first.
async function submitToGitHub(){
  if(!DATA) return;
  if(GH_CONFIGURED && !GH_CONNECTED){ connectGitHub(); return; }
  if(GH_CONFIGURED && GH_CONNECTED && !GH_CANPOST){   // GitHub would 403 — go straight to Copy review
    $("#submit-note").textContent = "In-app posting isn’t available for @"+GH_LOGIN+" on this org — copy the review and paste it (it posts as you).";
    copyReview(); return;
  }
  const parts = DATA.repo.split("/"); const owner=parts[0], repo=parts[1];
  const wasByQid = {}; DATA.decls.forEach(d => { wasByQid[d.qid] = parseStatus(d.comments).status; });
  const decisions = {};
  Object.keys(STATE).forEach(qid => {
    const s = STATE[qid] || {}; const status=(s.changeStatus||"").trim(); const notes=(s.note||"").trim();
    if(!status && !notes) return;
    decisions[qid] = { status, notes, was: wasByQid[qid]||"" };
  });
  const note = $("#submit-note");
  if(!Object.keys(decisions).length){ note.textContent = "Nothing to submit — set a status change or add a note first."; return; }
  note.textContent = "Posting to GitHub…";
  try {
    const r = await fetch("/api/review/"+owner+"/"+repo+"/"+DATA.pr+"?db="+encodeURIComponent(DATA.db||"wikidata"),
      {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({decisions})});
    let j = {}; try { j = await r.json(); } catch(e){}
    if(j.needsConnect){ note.textContent = "Connecting to GitHub…"; connectGitHub(); return; }
    if(!j.ok){ note.textContent = j.error || ("Could not post (HTTP "+r.status+") — use Copy review."); return; }
    const res = j.results || [];
    res.filter(x=>x.posted).forEach(x=>{ delete STATE[x.qid]; }); save();
    const skipped = res.filter(x=>x.skipped).length;
    const errs = res.filter(x=>x.error);
    // Org-side blocks are identical and unactionable per tag, so collapse them into
    // ONE nudge. Two shapes: the legacy OAuth-App restriction, and the GitHub App
    // not being installed on the org ("Resource not accessible by integration").
    // Both unblock via Copy review (pastes as YOU in-browser) — and, for the App,
    // by an org owner installing it (installUrl).
    const BLOCKED = /appear to have the correct authorization|OAuth App access restrictions|organization has enabled|Resource not accessible by integration/;
    const blocked = errs.filter(e=>BLOCKED.test(e.error||""));
    const other = errs.filter(e=>!BLOCKED.test(e.error||""));
    const parts = ["Posted "+j.posted+" comment"+(j.posted===1?"":"s")+
      (j.changed?(" ("+j.changed+" status change"+(j.changed===1?"":"s")+")"):"")];
    if(skipped) parts.push(skipped+" skipped (not in this PR)");
    if(other.length) parts.push(other.length+" failed — "+other.map(e=>e.qid+": "+e.error).join("; "));
    let msg = parts.join(" · ");
    if(blocked.length){
      msg += " · ⚠️ "+blocked.length+" couldn’t post: this org hasn’t enabled in-app posting. "
        + "Click “📋 Copy review” and paste it as a PR comment — it posts as you."
        + (j.installUrl ? " (Or an org owner can install the app: "+j.installUrl+")" : "");
    }
    if(j.posted>0){
      note.innerHTML = "✓ "+msg+' — see <a href="'+prUrl()+'" target="_blank" rel="noopener">the PR ↗</a>. Reloading…';
      loadPR();
    } else {
      note.textContent = (errs.length?"⚠️ ":"")+msg+(res.length?"":" — nothing to submit.");
    }
  } catch(e){ note.textContent = "Network error: "+e; }
}

$("#load").addEventListener("click", loadPR);
$("#pr").addEventListener("keydown", e => { if(e.key==="Enter") loadPR(); });
$("#submit").addEventListener("click", copyReview);
$("#submit-gh").addEventListener("click", submitToGitHub);
$("#filter").addEventListener("change", () => { if(DATA) render(DATA); });
$("#db").addEventListener("change", () => { if($("#pr").value.trim()) loadPR(); });
$("#open-all").addEventListener("click", toggleAllReviews);
const cbClose = document.getElementById("copybox-close");
if(cbClose) cbClose.addEventListener("click", () => { const d=$("#copybox"); if(d.close) d.close(); });
// Deep-link support: /review?repo=owner/name&pr=6
const qp = new URLSearchParams(location.search);
// Always default to upstream Mathlib on load (overriding any browser session
// restore); a ?repo= deep-link still wins.
$("#repo").value = qp.get("repo") || "leanprover-community/mathlib4";
if(qp.get("db") && DB[qp.get("db")]) $("#db").value = qp.get("db");
if(qp.get("pr")){ $("#pr").value = qp.get("pr"); loadPR(); }
refreshConnected();
`;
}
