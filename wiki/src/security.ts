// Browser security headers for every Worker-generated response, plus the
// sink for Content-Security-Policy violation reports.
//
// Scope: these headers reach only responses the Worker produces. Files under
// wiki/public (including /brain, /concepts and the Brain shards) are served
// asset-first by Cloudflare before the Worker runs, so they never pass through
// this middleware.
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
// responses. It allows inline scripts, so enforcing it as written would add
// little XSS protection; the reports show what a stricter policy, with the
// inline scripts moved out first, would break.
//
// A Report-Only frame-ancestors only reports, so the framing ban rides a
// separate, enforced Content-Security-Policy header (alongside the legacy
// X-Frame-Options for older browsers).

import type { Hono } from "hono";
import type { Env } from "./env.js";

export const CSP_REPORT_PATH = "/api/csp-report";

export const CSP_REPORT_ONLY = [
  "default-src 'self'",
  "script-src 'self' 'unsafe-inline' https://static.cloudflareinsights.com",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: https://*.wikimedia.org https://wikimedia.org https://*.githubusercontent.com https://*.googleusercontent.com https://github.com",
  "media-src 'self' https://*.wikimedia.org https://*.githubusercontent.com https://github.com",
  "font-src 'self' https://cdn.jsdelivr.net",
  "connect-src 'self' https://cloudflareinsights.com",
  "frame-src 'self'",
  "form-action 'self'",
  "base-uri 'self'",
  "object-src 'none'",
  `report-uri ${CSP_REPORT_PATH}`,
  "report-to csp",
].join("; ");

export const SECURITY_HEADERS: Readonly<Record<string, string>> = {
  "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "Content-Security-Policy": "frame-ancestors 'none'",
  "Content-Security-Policy-Report-Only": CSP_REPORT_ONLY,
  "Reporting-Endpoints": `csp="${CSP_REPORT_PATH}"`,
  "Referrer-Policy": "strict-origin-when-cross-origin",
};

function applyHeaders(headers: Headers): void {
  for (const [name, value] of Object.entries(SECURITY_HEADERS)) headers.set(name, value);
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
  // capped, and each IP gets 5 reports a minute: FLAG_LIMITER's budget under
  // its own csp:<ip> keys, checked before the body is read.
  app.post(CSP_REPORT_PATH, async (c) => {
    const ip = c.req.header("CF-Connecting-IP") ?? "";
    const { success: allowed } = await c.env.FLAG_LIMITER.limit({ key: `csp:${ip}` });
    if (!allowed) return c.json({ ok: false, error: "rate limited" }, 429);
    const declared = Number(c.req.header("Content-Length"));
    if (Number.isFinite(declared) && declared > MAX_REPORT_BYTES) {
      return c.json({ ok: false, error: "report too large" }, 413);
    }
    const text = await readCapped(c.req.raw, MAX_REPORT_BYTES);
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
