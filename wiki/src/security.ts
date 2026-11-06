// Browser security headers for every Worker-generated response, plus the
// sink for Content-Security-Policy violation reports.
//
// Scope: the middleware reaches only responses the Worker produces. Files under
// wiki/public (including /brain, /concepts and the Brain shards) are served
// asset-first by Cloudflare before the Worker runs, so they never pass through
// it. scripts/build-public.ts therefore writes the same header set into the
// asset tree's `_headers` file (staticAssetHeadersFile below), which Workers
// Static Assets applies to every asset response.
//
// Headers are applied at response time and are never part of the bytes stored
// in RENDER_CACHE, so changing them needs no render-cache key bump.
//
// The CSP ships in Report-Only mode and describes what the pages load today:
// inline <script>/<style> blocks and style attributes, same-origin assets and
// fetches, the same-origin iframe of /brain on the landing page, Wikipedia
// media inside article bodies, OAuth avatars on profiles, GitHub-rendered
// comment media and the JuliaMono webfont (jsDelivr) on /review, and the
// Cloudflare Web Analytics beacon that Cloudflare injects into text/html
// responses. The static Brain page (/brain, framed on every landing view)
// also loads d3 from jsDelivr and fetches Wikipedia page summaries and
// Wikidata entity search, so its `_headers` rule carries BRAIN_CSP_REPORT_ONLY,
// the same policy plus exactly those origins. It allows inline scripts, so
// enforcing it as written would add little XSS protection; the reports show
// what a stricter policy, with the inline scripts moved out first, would break.
//
// A Report-Only frame-ancestors only reports, so the framing ban rides a
// separate, enforced Content-Security-Policy header (alongside the legacy
// X-Frame-Options for older browsers).
//
// The articles plane's pages are the exception: they enforce a strict
// policy of their own, articlesCsp below, listed beside the framing ban.

import type { Context, Hono } from "hono";
import type { Env } from "./env.js";

export const CSP_REPORT_PATH = "/api/csp-report";

const CSP_DIRECTIVES: ReadonlyArray<readonly [string, string]> = [
  ["default-src", "'self'"],
  ["script-src", "'self' 'unsafe-inline' https://static.cloudflareinsights.com"],
  ["style-src", "'self' 'unsafe-inline'"],
  [
    "img-src",
    "'self' data: https://*.wikimedia.org https://wikimedia.org https://*.githubusercontent.com https://*.googleusercontent.com https://github.com",
  ],
  ["media-src", "'self' https://*.wikimedia.org https://*.githubusercontent.com https://github.com"],
  ["font-src", "'self' https://cdn.jsdelivr.net"],
  ["connect-src", "'self' https://cloudflareinsights.com"],
  ["frame-src", "'self'"],
  ["form-action", "'self'"],
  ["base-uri", "'self'"],
  ["object-src", "'none'"],
  ["report-uri", CSP_REPORT_PATH],
  ["report-to", "csp"],
];

// The shared policy with `extra` sources appended to the named directives.
function reportOnlyPolicy(extra: Readonly<Record<string, readonly string[]>> = {}): string {
  for (const name of Object.keys(extra)) {
    if (!CSP_DIRECTIVES.some(([d]) => d === name)) throw new Error(`unknown CSP directive ${name}`);
  }
  return CSP_DIRECTIVES.map(([name, value]) => [name, value, ...(extra[name] ?? [])].join(" ")).join("; ");
}

export const CSP_REPORT_ONLY = reportOnlyPolicy();

// The Brain page's additions (site/build_brain_page.py): the d3 script from
// jsDelivr, the Wikipedia REST page-summary fetch and the Wikidata
// wbsearchentities fetch. test/security-headers.test.ts checks this list
// against the page builder's external <script src> and fetch() origins.
export const BRAIN_CSP_EXTRA_SOURCES: Readonly<Record<string, readonly string[]>> = {
  "script-src": ["https://cdn.jsdelivr.net"],
  "connect-src": ["https://en.wikipedia.org", "https://www.wikidata.org"],
};
export const BRAIN_CSP_REPORT_ONLY = reportOnlyPolicy(BRAIN_CSP_EXTRA_SOURCES);

// The articles plane's pages (articles.ts articlesHtml) enforce a policy of their
// own, articlesCsp. A reader page serves a stored Wikipedia revision as the builder
// checked it (scripts/articles/build_store.py --reader-html), not re-sanitised; this
// policy is the second barrier, so that were the stored bytes ever hostile, no
// script but the plane's own would run and nothing would load from another host.
// Scripts: the plane's two script files by URL (ARTICLES_SCRIPT_PATHS on the page's
// own origin; a path source matches that one file, whatever its ?v= query), not
// 'self', which would admit every same-origin script (the article editor's, the
// review page's, the Brain's); the Cloudflare Web Analytics beacon; and the reader
// shell's two inline scripts by their sha256 (the theme bootstrap in
// articles-render.ts and the header search, engine/searchbox.ts SEARCHBOX_SCRIPT). No
// 'unsafe-inline', so no other inline script and no inline handler runs.
// test/articles.test.ts recomputes the hashes from the served pages: change either
// script and update its hash here. Forms may submit only to the dashboard's search
// (ARTICLES_FORM_PATH, a GET to /articles), the one form an articles page has. Images and
// media: same-origin and Wikimedia's upload hosts, the hosts the builder lets a
// revision fetch from (READER_FETCH). Styles keep 'unsafe-inline': the revision
// carries Wikipedia's TemplateStyles and style attributes, which the builder
// checks, and the coverage bar is sized by style="width". Nothing may frame the
// pages (the separate framing ban) or be framed by them, and violations are
// reported to CSP_REPORT_PATH like the site's Report-Only ones.
export const ARTICLES_INLINE_SCRIPT_HASHES: Readonly<Record<"theme" | "search", string>> = {
  theme: "'sha256-DMaJR+iV63dY3QeNP+nRCqzJfeb9K2eVniDB9Ze1MWU='",
  search: "'sha256-/tMIUG71SaA6Sj3wRXe56bcJ5WHvJEJvv63Q2nMvywM='",
};
export const ARTICLES_MEDIA_HOSTS: readonly string[] = ["https://upload.wikimedia.org", "https://thumb.wikimedia.org"];
// articles-render.ts ARTICLES_JS and ARTICLES_READER_JS without their queries (test/articles.test.ts pins it).
export const ARTICLES_SCRIPT_PATHS: readonly string[] = ["/assets/articles.js", "/assets/articles-reader.js"];
export const ARTICLES_FORM_PATH = "/articles";
// Cloudflare Turnstile, which gates a new account's first community tag
// (articles-overlay.ts). Its widget is a script and a cross-origin iframe, so a
// page that renders it — and ONLY such a page — widens script-src, frame-src and
// connect-src by this one origin. default-src is 'none', so without the widget
// the pages still frame nothing at all.
export const TURNSTILE_ORIGIN = "https://challenges.cloudflare.com";

// The policy for an articles page served from `origin` (the request URL's,
// "https://host[:port]"). `turnstile` adds the challenge widget's origin, and is
// set only on a page that actually renders the widget.
export function articlesCsp(origin: string, turnstile = false): string {
  const extra = turnstile ? ` ${TURNSTILE_ORIGIN}` : "";
  const directives: ReadonlyArray<readonly [string, string]> = [
    ["default-src", "'none'"],
    ["script-src", [...ARTICLES_SCRIPT_PATHS.map((path) => origin + path), ARTICLES_INLINE_SCRIPT_HASHES.theme,
      ARTICLES_INLINE_SCRIPT_HASHES.search, "https://static.cloudflareinsights.com"].join(" ") + extra],
    ["style-src", "'self' 'unsafe-inline'"],
    ["img-src", `'self' ${ARTICLES_MEDIA_HOSTS.join(" ")}`],
    ["media-src", `'self' ${ARTICLES_MEDIA_HOSTS.join(" ")}`],
    ["font-src", "'self'"],
    ["connect-src", `'self' https://cloudflareinsights.com${extra}`],
    ...(turnstile ? ([["frame-src", TURNSTILE_ORIGIN]] as ReadonlyArray<readonly [string, string]>) : []),
    ["form-action", origin + ARTICLES_FORM_PATH],
    ["base-uri", "'none'"],
    ["object-src", "'none'"],
    ["report-uri", CSP_REPORT_PATH],
    ["report-to", "csp"],
  ];
  return directives.map(([name, value]) => `${name} ${value}`).join("; ");
}

export const SECURITY_HEADERS: Readonly<Record<string, string>> = {
  "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "Content-Security-Policy": "frame-ancestors 'none'",
  "Content-Security-Policy-Report-Only": CSP_REPORT_ONLY,
  "Reporting-Endpoints": `csp="${CSP_REPORT_PATH}"`,
  "Referrer-Policy": "strict-origin-when-cross-origin",
};

// Static pages our own pages frame: the landing page embeds /brain?embed=1
// (home.ts). They keep every other header but may be framed same-origin.
export const SAME_ORIGIN_FRAMED_ASSET_PATHS: readonly string[] = ["/brain", "/brain.html"];

// Limits of the Workers Static Assets `_headers` parser.
export const STATIC_HEADERS_MAX_RULES = 100;
export const STATIC_HEADERS_MAX_LINE_LENGTH = 2000;

// The `_headers` file for the static asset tree: SECURITY_HEADERS on every
// path, and for SAME_ORIGIN_FRAMED_ASSET_PATHS (the Brain page) same-origin
// framing plus the Brain's Report-Only policy. A later matching rule appends
// to a header an earlier rule set, so the framed paths first detach
// (`! Name`) the inherited headers and then set them again.
export function staticAssetHeadersFile(): string {
  const lines = [
    "# Generated by wiki/scripts/build-public.ts from SECURITY_HEADERS in",
    "# wiki/src/security.ts. Do not edit; Workers Static Assets applies it to",
    "# every static asset response (Worker responses get the middleware).",
    "/*",
  ];
  for (const [name, value] of Object.entries(SECURITY_HEADERS)) lines.push(`  ${name}: ${value}`);
  for (const path of SAME_ORIGIN_FRAMED_ASSET_PATHS) {
    lines.push(
      path,
      "  ! X-Frame-Options",
      "  ! Content-Security-Policy",
      "  ! Content-Security-Policy-Report-Only",
      "  X-Frame-Options: SAMEORIGIN",
      "  Content-Security-Policy: frame-ancestors 'self'",
      `  Content-Security-Policy-Report-Only: ${BRAIN_CSP_REPORT_ONLY}`,
    );
  }
  for (const line of lines) {
    if (line.length > STATIC_HEADERS_MAX_LINE_LENGTH || /[\r\n]/.test(line)) {
      throw new Error(`_headers line is not representable: ${line.slice(0, 80)}`);
    }
  }
  return lines.join("\n") + "\n";
}

// A response may carry an enforced policy of its own (the articles pages'
// articlesCsp). It is kept, and the framing ban is listed beside it as a second
// policy ("A, B": a browser enforces every policy a response lists), so no route
// can lift the ban; any other response gets the ban alone.
function applyHeaders(headers: Headers): void {
  const own = headers.get("Content-Security-Policy");
  for (const [name, value] of Object.entries(SECURITY_HEADERS)) headers.set(name, value);
  const ban = SECURITY_HEADERS["Content-Security-Policy"];
  if (own && own !== ban) {
    const listed = own.split(",").map((policy) => policy.trim());
    headers.set("Content-Security-Policy", listed.includes(ban) ? own : `${own}, ${ban}`);
  }
}

// Violation reports arrive in two shapes: the legacy report-uri body
// ({"csp-report": {...}}, kebab-case keys) and the Reporting API batch
// ([{type: "csp-violation", body: {...}}], camelCase keys). A batch carries
// about 1 KB per report (each repeats the policy text), hence the 64 KB cap.
const MAX_REPORT_BYTES = 64 * 1024;
const MAX_REPORTS_LOGGED = 20;
const MAX_FIELD_CHARS = 300;

function field(v: unknown): string | null {
  if (typeof v === "string") return v.slice(0, MAX_FIELD_CHARS);
  if (typeof v === "number" && Number.isFinite(v)) return String(v);
  return null;
}

function obj(v: unknown): Record<string, unknown> | null {
  return typeof v === "object" && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
}

export interface CspViolation {
  disposition: string | null;
  directive: string | null;
  blocked: string | null;
  document: string | null;
  source: string | null;
  line: string | null;
}

export function parseCspReports(payload: unknown): CspViolation[] {
  const out: CspViolation[] = [];
  const legacy = obj(obj(payload)?.["csp-report"]);
  if (legacy) {
    out.push({
      disposition: field(legacy["disposition"]),
      directive: field(legacy["effective-directive"] ?? legacy["violated-directive"]),
      blocked: field(legacy["blocked-uri"]),
      document: field(legacy["document-uri"]),
      source: field(legacy["source-file"]),
      line: field(legacy["line-number"]),
    });
  } else if (Array.isArray(payload)) {
    for (const item of payload) {
      const report = obj(item);
      const body = obj(report?.body);
      if (!report || !body || report.type !== "csp-violation") continue;
      out.push({
        disposition: field(body.disposition),
        directive: field(body.effectiveDirective),
        blocked: field(body.blockedURL),
        document: field(body.documentURL ?? report.url),
        source: field(body.sourceFile),
        line: field(body.lineNumber),
      });
    }
  }
  return out.slice(0, MAX_REPORTS_LOGGED);
}

// The pseudonym salt of both pseudonymous exports: the research export
// (index.ts, sha256(user_id + salt)) and the articles feedback export (articles.ts,
// domain-separated). A dedicated secret keeps the salt out of the hands of
// every PIPELINE_TOKEN holder (who could otherwise recompute the pseudonyms
// for the public user ids) and lets the bearer rotate without re-keying them.
// Falls back to PIPELINE_TOKEN so existing pseudonyms stay stable until the
// secret is set.
export function researchExportSalt(env: Pick<Env, "RESEARCH_EXPORT_SALT" | "PIPELINE_TOKEN">): string {
  return env.RESEARCH_EXPORT_SALT || env.PIPELINE_TOKEN || "";
}

// CSRF guard for session-authenticated writes: a browser request whose Origin
// is another site is refused. Requests without an Origin (the pipeline's
// curl/requests clients) pass; a same-origin fetch always sends one.
export function checkOrigin(c: Context<{ Bindings: Env }>): Response | null {
  const origin = c.req.header("Origin");
  if (origin && origin !== new URL(c.req.url).origin) {
    return c.json({ ok: false, error: "cross-origin request rejected" }, 403);
  }
  return null;
}

// The client part of an anonymous rate-limit key, from CF-Connecting-IP. An IPv6
// address keys by its /64: one host usually holds a whole /64 and could otherwise
// rotate through it for a fresh budget per address. Everything else keys as given:
// IPv4, the ::/64 block (loopback and the IPv4-mapped forms, one client per
// address), and anything unparsable.
export function limiterIp(ip: string | undefined): string | undefined {
  if (!ip?.includes(":")) return ip;
  const halves = ip.split("::");
  if (halves.length > 2) return ip;
  const head = halves[0] ? halves[0].split(":") : [];
  const tail = halves[1] ? halves[1].split(":") : [];
  const zeros = 8 - head.length - tail.length;
  if (halves.length === 2 ? zeros < 1 : zeros !== 0) return ip;
  const groups = [...head, ...Array<string>(zeros).fill("0"), ...tail];
  if (!groups.every((g) => /^[0-9a-f]{1,4}$/i.test(g))) return ip;
  const prefix = groups.slice(0, 4).map((g) => parseInt(g, 16).toString(16)).join(":");
  return prefix === "0:0:0:0" ? ip : `${prefix}::/64`;
}

// Reads at most `max` bytes of the body, counting bytes as they stream, so a
// chunked request without Content-Length cannot buffer an unbounded body.
// null = over the cap.
async function readCapped(req: Request, max: number): Promise<string | null> {
  if (!req.body) return "";
  const reader = req.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > max) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  const buf = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    buf.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(buf);
}

// The request body as text, or null when it is over `max` bytes: refused
// up front on a declared Content-Length, else counted as it streams. Callers
// answer null with a 413.
export async function readBodyCapped(req: Request, max: number): Promise<string | null> {
  const declared = Number(req.headers.get("Content-Length"));
  if (Number.isFinite(declared) && declared > max) return null;
  return readCapped(req, max);
}

// Byte caps for every JSON request body the Worker reads (readJsonCapped).
// Each is sized well above its largest legitimate payload:
//   flag / flagResolve: a reason, an annotation id and a comment of at most
//     MAX_FLAG_COMMENT_LEN characters (the anonymous route; FLAG_LIMITER keys per IP);
//   article: annotations of at most MAX_ANNOTATIONS_BYTES serialized plus
//     MAX_META_BYTES of meta, with room for escaped non-ASCII and whitespace;
//   runs: a pipeline run report (notes up to MAX_TEXT_LEN);
//   brainEdge / brainNode: one edge or node with short bounded fields;
//   quickstatements: up to 200,000 characters of pasted TSV, or 100 rows;
//   repos: at most 50 repository rows;
//   reviewPost: one PR's reviewer decisions with free-text notes;
//   articlesFeedback: one location verdict (articles.ts): a 64-hex location key, a
//     page id, a campaign of at most 128 characters and a note of at most
//     MAX_FLAG_COMMENT_LEN characters (about 3.3 KB even if every note
//     character is \u-escaped);
//   agents: one agent name of at most AGENT_NAME_MAX characters (agents.ts);
//   articlesTag: one community tag (articles.ts): a 64-hex passage key, a page
//     id, a library id, a declaration of at most 240 characters, a relation, a
//     note of at most MAX_TAG_NOTE_CHARS, a campaign and a Turnstile token
//     (about 2 KB), all of which fits in 4 KiB even fully \u-escaped.
export const MAX_JSON_BODY_BYTES = {
  flag: 16 * 1024,
  flagResolve: 16 * 1024,
  article: 4 * 1024 * 1024,
  runs: 64 * 1024,
  brainEdge: 64 * 1024,
  brainNode: 64 * 1024,
  quickstatements: 1024 * 1024,
  repos: 256 * 1024,
  reviewPost: 1024 * 1024,
  articlesFeedback: 4 * 1024,
  articlesTag: 4 * 1024,
  agents: 4 * 1024,
} as const;

export type JsonBody = { ok: true; value: unknown } | { ok: false; response: Response };

// The request body parsed as JSON, read through readBodyCapped so neither a
// declared nor a streamed body past `max` bytes is buffered. Over the cap:
// 413 {ok:false, error:"request body too large (max N bytes)"}. Malformed
// JSON: 400 with the route's own error text, so each route keeps its answer.
// Call it after the route's auth and rate-limit checks, as c.req.json() was.
export async function readJsonCapped(
  c: Context<{ Bindings: Env }>,
  max: number,
  badJson = "bad json",
): Promise<JsonBody> {
  const text = await readBodyCapped(c.req.raw, max);
  if (text === null) {
    return { ok: false, response: c.json({ ok: false, error: `request body too large (max ${max} bytes)` }, 413) };
  }
  try {
    return { ok: true, value: JSON.parse(text) as unknown };
  } catch {
    return { ok: false, response: c.json({ ok: false, error: badJson }, 400) };
  }
}

// Register FIRST, before any route: Hono runs middleware in registration
// order, and a route registered earlier answers without reaching it.
export function registerSecurityHeaders(app: Hono<{ Bindings: Env }>): void {
  app.use("*", async (c, next) => {
    await next();
    try {
      applyHeaders(c.res.headers);
    } catch {
      // Immutable headers (a Response passed through from fetch): re-wrap.
      c.res = new Response(c.res.body, c.res);
      applyHeaders(c.res.headers);
    }
  });

  // Log-only sink: at most one structured line per request (Workers
  // observability), no storage. Unauthenticated by necessity (browsers send
  // the reports), so the body is byte-capped, the violations per line are
  // capped, and each IP (an IPv6 /64) gets 5 requests a minute: FLAG_LIMITER's
  // budget under its own csp:<ip> keys, checked before the body is read.
  app.post(CSP_REPORT_PATH, async (c) => {
    const ip = limiterIp(c.req.header("CF-Connecting-IP")) ?? "";
    const { success: allowed } = await c.env.FLAG_LIMITER.limit({ key: `csp:${ip}` });
    if (!allowed) return c.json({ ok: false, error: "rate limited" }, 429);
    const text = await readBodyCapped(c.req.raw, MAX_REPORT_BYTES);
    if (text === null) return c.json({ ok: false, error: "report too large" }, 413);
    let payload: unknown;
    try {
      payload = JSON.parse(text);
    } catch {
      return c.json({ ok: false, error: "bad json" }, 400);
    }
    const violations = parseCspReports(payload);
    if (violations.length > 0) console.log(JSON.stringify({ event: "csp-report", violations }));
    return c.body(null, 204);
  });
}
