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

// True for a wikilean-review comment that carries only a status (no note) — the
// client drops these (the status shows in the "Existing review" row), so we
// skip rendering their markdown.
function statusOnlyReviewComment(body: string): boolean {
  if (!/wikilean-review:Q/.test(body)) return false;
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
  const paras = [...html.matchAll(/<p\b[^>]*>([\s\S]*?)<\/p>/g)].map((m) => m[1]);
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

// Wikipedia leads via the action API HTML intro (one request/title, KV-cached).
// We render math from the MathML the action API embeds — far cleaner than the
// REST summary `extract`, which leaks broken LaTeX for math-heavy articles.
async function fetchLeads(titles: string[], env: Env): Promise<Map<string, string>> {
  const out = new Map<string, string>();
  await Promise.all(
    titles.map(async (t) => {
      const key = `wplead4:${t}`; // bumped from wplead3: — footnote-marker stripping
      const cached = await env.RENDER_CACHE.get(key);
      if (cached !== null) {
        out.set(t, cached);
        return;
      }
      try {
        const u =
          `https://en.wikipedia.org/w/api.php?action=query&prop=extracts&exintro=1` +
          `&redirects=1&format=json&titles=${encodeURIComponent(t)}`;
        const r = await fetch(u, { headers: { "User-Agent": UA } });
        if (!r.ok) return;
        const j = (await r.json()) as { query?: { pages?: Record<string, { extract?: string }> } };
        const page = Object.values(j.query?.pages ?? {})[0];
        const lead = (cleanLead(htmlLeadToText(page?.extract ?? "")) ?? "").slice(0, 1000);
        out.set(t, lead);
        await env.RENDER_CACHE.put(key, lead, { expirationTtl: 60 * 60 * 24 * 7 });
      } catch {
        /* leave this title without a lead */
      }
    }),
  );
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

const DECL_SIG_RE =
  /^(?:protected\s+|private\s+|noncomputable\s+|public\s+|nonrec\s+|unsafe\s+|partial\s+|scoped\s+|local\s+)*(?:def|theorem|lemma|class|structure|inductive|abbrev|instance|opaque|axiom)\s+([^\s:({\[]+)/;

// Given file lines and a qid, return the fully-qualified declaration name around
// its @[wikidata Qxxx] tag (e.g. "Module.Projective"), by reading the signature
// line and prepending any enclosing `namespace`s. Used to anchor the Mathlib
// docs link. Null when there is no named signature (e.g. an anonymous instance).
export function extractDeclName(lines: string[], qid: string): string | null {
  const pat = new RegExp(`wikidata\\s+${qid}\\b`);
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
  decls: Array<DeclTag & { comments: ReviewComment[]; source: string; wd: WdInfo | null; decl: string | null }>;
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

  // Page mode: also pull top-level "## WikiLean review" comments (the pasted
  // copy-paste reviews) and fold their per-qid entries into the matching cards,
  // in the same inline shape the client renders.
  const pastedByQid = new Map<string, ReviewComment[]>();
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
      for (const e of parseClipboardReview(ic.body)) {
        const body = synthReviewBody(e.qid, e.status, e.note);
        const bodyHtml = e.note && env ? await ghRenderMarkdown(body, full, token, env) : null;
        if (!pastedByQid.has(e.qid)) pastedByQid.set(e.qid, []);
        pastedByQid.get(e.qid)!.push({
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
  // Wikidata description + Wikipedia lead per qid. Skipped for the POST path.
  const sourceByQid = new Map<string, string>();
  const declByQid = new Map<string, string>();
  let wdByQid = new Map<string, WdInfo>();
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
      const body = lines ? extractDeclBody(lines, t.qid) : null;
      if (body) sourceByQid.set(t.qid, body);
      const name = lines ? extractDeclName(lines, t.qid) : null;
      if (name) declByQid.set(t.qid, name);
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
      comments: [...(byLine.get(keyOf(t.file, t.line)) ?? []), ...(pastedByQid.get(t.qid) ?? [])],
      source: sourceByQid.get(t.qid) ?? t.hunk.join("\n"),
      wd: wdByQid.get(t.qid) ?? null,
      decl: declByQid.get(t.qid) ?? null,
    })),
  };
}

// ---- routes -------------------------------------------------------------

const OWNER_RE = /^[A-Za-z0-9_.-]+$/;

function reviewCacheKey(owner: string, repo: string, pr: number): string {
  return `reviewpayload:${owner}/${repo}:${pr}`;
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
    // Full-payload cache (60s) — a refresh within the window returns instantly
    // from KV with zero GitHub calls. Busted on POST so a just-submitted review
    // shows immediately; external comments are at most 60s stale.
    const pageKey = reviewCacheKey(owner, repo, pr);
    const cached = await c.env.RENDER_CACHE.get(pageKey);
    if (cached) return c.body(cached, 200, { "Content-Type": "application/json" });

    // Reads use the logged-in user's token if present, else the server token
    // (5000/hr) — a single page load makes ~50 GitHub calls and would blow the
    // shared 60/hr unauthenticated limit otherwise.
    const { token } = await githubAccountFor(c);
    const readToken = token || c.env.GITHUB_API_TOKEN;
    try {
      const payload = await buildReviewPayload(owner, repo, pr, readToken, c.env, true);
      const json = JSON.stringify({ ok: true, ...payload });
      await c.env.RENDER_CACHE.put(pageKey, json, { expirationTtl: 60 });
      return c.body(json, 200, { "Content-Type": "application/json" });
    } catch (e) {
      return c.json({ ok: false, error: String(e instanceof Error ? e.message : e) }, 502);
    }
  });

  // Post the reviewer's decisions/notes as inline PR comments, as the logged-in
  // user, via their stored GitHub token. Verbatim notes, never LLM-authored.
  //
  // NOTE: currently UNUSED by the page — the client copies a Markdown review to
  // paste manually, so we don't request the broad `public_repo` write scope.
  // Kept for the GitHub-App migration (see TODO in auth.ts), after which the
  // page can call this again with a fine-grained token.
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

    let body: { decisions?: Record<string, { status?: string; notes?: string; was?: string }> };
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
    // Per-author idempotency: a reviewer can add their note even when someone
    // else already reviewed the tag, but won't double-post their own. Match by
    // GitHub login (the comment author), not the WikiLean account name.
    const myLogin = await ghLogin(token);
    const alreadyByMe = new Set<string>();
    for (const d of payload.decls) {
      for (const cm of d.comments) {
        const m = cm.body.match(/wikilean-review:(Q\d+)/);
        if (m && myLogin && cm.user === myLogin) alreadyByMe.add(m[1]);
      }
    }

    const results: Array<{ qid: string; posted: boolean; skipped?: string; error?: string }> = [];
    let changed = 0; // posted comments that set a status differing from the existing one
    for (const [qid, dec] of Object.entries(decisions)) {
      const status = (dec.status ?? "").trim();
      const notes = (dec.notes ?? "").trim();
      const was = (dec.was ?? "").trim();
      if (!status && !notes) continue; // nothing to post
      if (alreadyByMe.has(qid)) {
        results.push({ qid, posted: false, skipped: "you already commented on this tag" });
        continue;
      }
      const tag = tagByQid.get(qid);
      if (!tag) {
        results.push({ qid, posted: false, error: "tag not present in this PR" });
        continue;
      }
      const commentBody = buildReviewCommentBody(qid, status, notes, was);
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
        if (status && status !== was) changed++;
      } else {
        results.push({ qid, posted: false, error: `GitHub ${r.status}: ${(await r.text()).slice(0, 120)}` });
      }
    }
    const posted = results.filter((x) => x.posted).length;
    // Invalidate the page cache so the submitter's refresh shows the new
    // comments immediately (rather than the up-to-60s-stale cached page).
    if (posted > 0) await c.env.RENDER_CACHE.delete(reviewCacheKey(owner, repo, pr));
    // Top-level "Reviewed" summary comment (one per submission that posted
    // anything), so the PR thread logs each review pass.
    if (posted > 0) {
      const who = myLogin ? `@${myLogin}` : user.name || "a reviewer";
      const summary =
        `**Reviewed by ${who}.** Added ${posted} inline comment${posted === 1 ? "" : "s"}` +
        (changed ? `, including ${changed} status change${changed === 1 ? "" : "s"}` : "") +
        `. <!-- wikilean-review-summary -->`;
      await fetch(`${GH_API}/repos/${owner}/${repo}/issues/${pr}/comments`, {
        method: "POST",
        headers: { ...ghHeaders(token), "Content-Type": "application/json" },
        body: JSON.stringify({ body: summary }),
      }).catch(() => {});
    }
    return c.json({ ok: true, posted, changed, results });
  });

  // The review page (shell + client script).
  app.get("/review", (c) => c.html(reviewPageHtml()));
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
    `<sub><a href="https://www.wikidata.org/wiki/${qid}">${qid}</a> ` +
    `<!-- wikilean-review:${qid} --></sub>`
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
  status: string; // approve | revise | reject | flag | ""
  note: string;
}

// Parse a pasted "## WikiLean review" comment back into per-qid {status, note}.
// Mirrors the client's buildClipboardReview format (entry header + indented
// `- status:` and note sub-bullets).
export function parseClipboardReview(body: string): PastedEntry[] {
  const out: PastedEntry[] = [];
  let cur: PastedEntry | null = null;
  let note: string[] = [];
  const flush = () => {
    if (cur) {
      cur.note = note.join("\n").trim();
      out.push(cur);
    }
  };
  for (const ln of body.split("\n")) {
    const h = ln.match(/^-\s*\*\*\[(Q\d+)\]/);
    if (h) {
      flush();
      cur = { qid: h[1], status: "", note: "" };
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
function synthReviewBody(qid: string, status: string, note: string): string {
  if (status === "flag") {
    const quoted = note ? note.split("\n").map((l) => "> " + l).join("\n") : "_(no note)_";
    return (
      `**⚠️ WikiLean reviewer note (Deletion candidate)**\n\n${quoted}\n\n` +
      `<sub><a href="https://www.wikidata.org/wiki/${qid}">${qid}</a> <!-- wikilean-review:${qid} --></sub>`
    );
  }
  return buildReviewCommentBody(qid, status, note);
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
form.load input{font:inherit;padding:.45rem .6rem;border:1px solid var(--rule);border-radius:6px;background:#fff}
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
#bar button:disabled{opacity:.5;cursor:default}
.note{font-size:.8rem;color:var(--muted)}
</style></head>
<body><div class="wrap">
<h1>WikiLean · <code>@[wikidata]</code> review</h1>
<p class="lede">Paste a pull request; review each tagged declaration with its existing GitHub comments, then submit your decisions.</p>
<form class="load" onsubmit="return false">
  <input id="repo" placeholder="owner/repo" value="leanprover-community/mathlib4">
  <input id="pr" placeholder="PR #" inputmode="numeric">
  <button id="load">Load PR</button>
  <span class="note">e.g. <code>leanprover-community/mathlib4</code> · <code>Deicyde/mathlib4</code> (fork)</span>
</form>
<div id="status"></div>
<div id="controls" hidden>
  <label>Filter by your review:
    <select id="filter">
      <option value="all">All</option>
      <option value="approve">🟢 approve</option>
      <option value="revise">🟡 revise</option>
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
    <button id="submit">📋 Copy review for GitHub</button>
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
let STATE = {};   // qid -> {status, notes}
let KEY = "";
let CUR = null;   // {owner, repo, pr} of the loaded PR
let DATA = null;  // last loaded payload (for re-render on filter change)

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

const EMO = {approve:"🟢",revise:"🟡",reject:"🔴",flag:"⚠️"};

// Your existing review status for a decl, parsed from the latest
// wikilean-review GitHub comment (the one you posted via the CLI/web tool).
function parseStatus(comments){
  let best=null;
  (comments||[]).forEach(c=>{ if(/wikilean-review:Q/.test(c.body||"")){
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
  if(/wikilean-review:Q/.test(c.body||"")){
    const note = reviewNote(c);
    if(!note) return "";
    return '<div class="cmt"><div class="who">'+esc(c.user)+'</div><div class="body md">'+note+'</div></div>';
  }
  return '<div class="cmt"><div class="who">'+esc(c.user)+'</div>'+
    (c.bodyHtml ? '<div class="body md">'+c.bodyHtml+'</div>' : '<div class="body">'+esc(c.body)+'</div>')+'</div>';
}

function render(data){
  DATA = data;
  // Per-decl existing status.
  const cur = data.decls.map(d => parseStatus(d.comments));
  // Distribution across ALL decls.
  const dist = {approve:0,revise:0,reject:0,flag:0,none:0};
  cur.forEach(c => dist[c.status||"none"]++);
  $("#dist").textContent = "🟢 "+dist.approve+" · 🟡 "+dist.revise+" · 🔴 "+dist.reject+
    " · ⚠️ "+dist.flag+" · ◯ "+dist.none;
  $("#controls").hidden = false;
  $("#status").innerHTML = "<b>" + esc(data.title) + "</b> — " + data.decls.length +
    " tagged declarations · commit <code>" + data.head_sha.slice(0,10) + "</code>" +
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
    // Split existing comments: WikiLean reviews (folded into the "Existing
    // review" block, status + note together) vs. other discussion.
    const reviewCmts = d.comments.filter(c => /wikilean-review:Q/.test(c.body||""));
    const otherCmts = d.comments.filter(c => !/wikilean-review:Q/.test(c.body||""));
    const otherHtml = otherCmts.map(commentHtml).filter(Boolean).join("");
    let reviewBlock;
    if(reviewCmts.length){
      reviewBlock = '<div class="cur"><span class="cur-label">Reviews</span>' +
        reviewCmts.map(c => { const ps = parseStatus([c]); const note = reviewNote(c);
          return '<div class="rev-item">' + statusBadge(ps.status) +
            (ps.by ? ' <span class="by">@' + esc(ps.by) + '</span>' : '') +
            (note ? '<div class="rev-note md">' + note + '</div>' : '') + '</div>';
        }).join("") + '</div>';
    } else {
      reviewBlock = '<div class="cur"><span class="cur-label">Reviews</span> ' + statusBadge("") + '</div>';
    }
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
    const showStatus = !!st.changeStatus;
    const showNote = !!(st.note && st.note.length);
    // file:line links to the Mathlib docs for the decl (module page + #name
    // anchor); a small "src ↗" links to the exact line of GitHub source. For a
    // non-Mathlib file (no docs page) the location itself links to source.
    const srcUrl = "https://github.com/" + data.repo + "/blob/" + data.head_sha + "/" + d.file + "#L" + d.line;
    const docsUrl = /^Mathlib\//.test(d.file || "")
      ? "https://leanprover-community.github.io/mathlib4_docs/" + d.file.replace(/\.lean$/, ".html") +
        (d.decl ? "#" + encodeURIComponent(d.decl) : "")
      : null;
    const locHtml = docsUrl
      ? '<a class="loc" href="' + docsUrl + '" target="_blank" rel="noopener" title="Open in Mathlib docs">' + esc(d.file) + ':' + d.line + '</a>' +
        ' <a class="loc-src" href="' + srcUrl + '" target="_blank" rel="noopener" title="View source on GitHub">src&nbsp;↗</a>'
      : '<a class="loc" href="' + srcUrl + '" target="_blank" rel="noopener" title="View source on GitHub">' + esc(d.file) + ':' + d.line + '</a>';
    el.innerHTML =
      '<header><span class="qid"><a href="https://www.wikidata.org/wiki/' + d.qid +
        '" target="_blank">' + d.qid + '</a></span>' +
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
          radio(d.qid,"revise","🟡 Revise",st.changeStatus) +
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
  const meta = {};
  DATA.decls.forEach(d => { meta[d.qid] = {
    label: (d.wd && (d.wd.enwikiTitle || d.wd.label)) || "",
    loc: d.file + ":" + d.line,
    was: parseStatus(d.comments).status,
  };});
  const bt = String.fromCharCode(96); // backtick char, built indirectly to avoid breaking String.raw
  const items = [];
  Object.keys(STATE).forEach(qid => {
    const s = STATE[qid] || {}; const ch=(s.changeStatus||"").trim(); const note=(s.note||"").trim();
    if(!ch && !note) return;
    const m = meta[qid] || {label:"",loc:"",was:""};
    let line = "- **["+qid+"](https://www.wikidata.org/wiki/"+qid+")**" +
      (m.label?(" "+m.label):"") + (m.loc?(" — "+bt+m.loc+bt):"");
    if(ch){ const e=EMO[ch]||""; line += "\n  - status: "+e+" **"+ch+"**" +
      (m.was && m.was!==ch ? (" _(was "+(EMO[m.was]||"")+" "+m.was+")_") : ""); }
    if(note){ line += "\n  - "+note.split("\n").join("\n    "); }
    items.push(line);
  });
  if(!items.length) return "";
  return "## WikiLean review\n\n" + items.join("\n") +
    "\n\n<sub>Generated by the WikiLean review tool — wikilean.jackmccarthy.org/review</sub>";
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

$("#load").addEventListener("click", loadPR);
$("#pr").addEventListener("keydown", e => { if(e.key==="Enter") loadPR(); });
$("#submit").addEventListener("click", copyReview);
$("#filter").addEventListener("change", () => { if(DATA) render(DATA); });
$("#open-all").addEventListener("click", toggleAllReviews);
const cbClose = document.getElementById("copybox-close");
if(cbClose) cbClose.addEventListener("click", () => { const d=$("#copybox"); if(d.close) d.close(); });
// Deep-link support: /review?repo=owner/name&pr=6
const qp = new URLSearchParams(location.search);
if(qp.get("repo")) $("#repo").value = qp.get("repo");
if(qp.get("pr")){ $("#pr").value = qp.get("pr"); loadPR(); }
`;
}
