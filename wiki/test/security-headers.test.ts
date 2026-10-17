// Browser security headers (src/security.ts). Pins: every Worker response
// (HTML page, KV-cache hit, JSON API, 404, redirect) carries HSTS, nosniff,
// the enforced framing ban, Referrer-Policy, and the Report-Only CSP; the CSP
// allows what the pages actually load; the headers never enter RENDER_CACHE
// bytes (so no render-cache key bump); the /api/csp-report sink logs both
// report shapes and caps its input; and the static-asset `_headers` file that
// build-public generates applies the same set to assets the Worker never sees.

import { afterEach, describe, it, expect, vi } from "vitest";
import { setup, get, blockNetwork, ORIGIN } from "./helpers/harness.js";
import { Hono } from "hono";
import { app } from "../src/index.js";
import {
  CSP_REPORT_ONLY,
  SAME_ORIGIN_FRAMED_ASSET_PATHS,
  SECURITY_HEADERS,
  STATIC_HEADERS_MAX_LINE_LENGTH,
  STATIC_HEADERS_MAX_RULES,
  parseCspReports,
  registerSecurityHeaders,
  staticAssetHeadersFile,
} from "../src/security.js";
import type { Env } from "../src/env.js";

blockNetwork();

afterEach(() => {
  vi.restoreAllMocks();
});

function expectSecurityHeaders(res: Response): void {
  expect(res.headers.get("Strict-Transport-Security")).toBe("max-age=31536000; includeSubDomains");
  expect(res.headers.get("X-Content-Type-Options")).toBe("nosniff");
  expect(res.headers.get("X-Frame-Options")).toBe("DENY");
  expect(res.headers.get("Content-Security-Policy")).toBe("frame-ancestors 'none'");
  expect(res.headers.get("Content-Security-Policy-Report-Only")).toBe(CSP_REPORT_ONLY);
  expect(res.headers.get("Reporting-Endpoints")).toBe('csp="/api/csp-report"');
  expect(res.headers.get("Referrer-Policy")).toBe("strict-origin-when-cross-origin");
}

function directive(name: string): string[] {
  const d = CSP_REPORT_ONLY.split("; ").find((part) => part.startsWith(name + " "));
  expect(d, `CSP lacks ${name}`).toBeDefined();
  return d!.split(" ").slice(1);
}

function postReport(env: Env, body: string, contentType: string): Promise<Response> {
  return Promise.resolve(
    app.request("/api/csp-report", { method: "POST", headers: { "Content-Type": contentType }, body }, env),
  );
}

describe("security headers", () => {
  it("are set on rendered HTML pages and on KV-cache hits, but never stored in the cache", async () => {
    const h = setup();
    const first = await get(h.env, "/about");
    expect(first.status).toBe(200);
    expectSecurityHeaders(first);
    expect([...h.renderCache.store.keys()].some((k) => k.startsWith("page:about:"))).toBe(true);
    const second = await get(h.env, "/about"); // served from RENDER_CACHE
    expectSecurityHeaders(second);
    for (const value of h.renderCache.store.values()) {
      expect(String(value)).not.toContain("Content-Security-Policy");
      expect(String(value)).not.toContain("frame-ancestors");
    }
  });

  it("are set on article pages, JSON APIs, 404s, and redirects", async () => {
    const h = setup();
    const article = await get(h.env, "/Test_Article");
    expect(article.status).toBe(200);
    expectSecurityHeaders(article);

    const api = await get(h.env, "/api/articles");
    expect(api.status).toBe(200);
    expectSecurityHeaders(api);

    const missing = await get(h.env, "/No_Such_Article");
    expect(missing.status).toBe(404);
    expectSecurityHeaders(missing);

    const redirect = await get(h.env, "/proposals"); // anonymous → login redirect
    expect(redirect.status).toBe(302);
    expectSecurityHeaders(redirect);
  });

  it("re-wraps a response whose headers are immutable", async () => {
    const mini = new Hono<{ Bindings: Env }>();
    registerSecurityHeaders(mini);
    mini.get("/raw", () => Response.redirect("https://example.org/elsewhere", 302));
    const res = await mini.request("/raw");
    expect(res.status).toBe(302);
    expect(res.headers.get("Location")).toBe("https://example.org/elsewhere");
    expectSecurityHeaders(res);
  });

  it("CSP report-only policy allows what the pages load", () => {
    expect(directive("script-src")).toEqual(
      expect.arrayContaining(["'self'", "'unsafe-inline'", "https://static.cloudflareinsights.com"]),
    );
    expect(directive("style-src")).toEqual(expect.arrayContaining(["'self'", "'unsafe-inline'"]));
    expect(directive("connect-src")).toEqual(expect.arrayContaining(["'self'", "https://cloudflareinsights.com"]));
    expect(directive("img-src")).toEqual(
      expect.arrayContaining(["'self'", "data:", "https://*.wikimedia.org", "https://wikimedia.org"]),
    );
    expect(directive("object-src")).toEqual(["'none'"]);
    // /review: the JuliaMono webfont (jsDelivr) and GitHub-rendered comment media.
    expect(directive("font-src")).toEqual(["'self'", "https://cdn.jsdelivr.net"]);
    expect(directive("img-src")).toContain("https://github.com");
    expect(directive("media-src")).toContain("https://github.com");
    // Uploaded videos render with a private-user-images.githubusercontent.com src.
    expect(directive("media-src")).toContain("https://*.githubusercontent.com");
    // A report-only frame-ancestors would only report; the ban is enforced
    // through the separate Content-Security-Policy header instead.
    expect(CSP_REPORT_ONLY).not.toContain("frame-ancestors");
    expect(SECURITY_HEADERS["Content-Security-Policy"]).toBe("frame-ancestors 'none'");
  });

  it("frame-src 'self' covers the landing page's same-origin /brain iframe", async () => {
    const h = setup();
    const html = await (await get(h.env, "/")).text();
    expect(html).toContain('src="/brain?embed=1"');
    expect(directive("frame-src")).toEqual(["'self'"]);
  });
});

describe("POST /api/csp-report", () => {
  it("logs a legacy report-uri body and answers 204", async () => {
    const h = setup();
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const body = JSON.stringify({
      "csp-report": {
        "document-uri": `${ORIGIN}/Test_Article`,
        "effective-directive": "script-src-elem",
        "blocked-uri": "https://evil.example/x.js",
        "source-file": `${ORIGIN}/Test_Article`,
        "line-number": 12,
        disposition: "report",
      },
    });
    const res = await postReport(h.env, body, "application/csp-report");
    expect(res.status).toBe(204);
    expectSecurityHeaders(res);
    const lines = log.mock.calls.map((call) => JSON.parse(String(call[0])));
    expect(lines).toEqual([
      {
        event: "csp-report",
        violations: [
          {
            disposition: "report",
            directive: "script-src-elem",
            blocked: "https://evil.example/x.js",
            document: `${ORIGIN}/Test_Article`,
            source: `${ORIGIN}/Test_Article`,
            line: "12",
          },
        ],
      },
    ]);
  });

  it("logs Reporting API batches, skipping non-CSP entries", async () => {
    const h = setup();
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const body = JSON.stringify([
      { type: "deprecation", url: `${ORIGIN}/`, body: { id: "x" } },
      {
        type: "csp-violation",
        url: `${ORIGIN}/stats`,
        body: {
          documentURL: `${ORIGIN}/stats`,
          effectiveDirective: "img-src",
          blockedURL: "https://images.example/a.png",
          disposition: "report",
        },
      },
    ]);
    const res = await postReport(h.env, body, "application/reports+json");
    expect(res.status).toBe(204);
    const lines = log.mock.calls.map((call) => JSON.parse(String(call[0])));
    expect(lines).toHaveLength(1);
    expect(lines[0].event).toBe("csp-report");
    expect(lines[0].violations).toHaveLength(1);
    expect(lines[0].violations[0]).toMatchObject({
      directive: "img-src",
      blocked: "https://images.example/a.png",
      document: `${ORIGIN}/stats`,
    });
  });

  it("rejects oversized and malformed bodies, and caps fields and line count", async () => {
    const h = setup();
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const big = await postReport(h.env, "x".repeat(65 * 1024), "application/csp-report");
    expect(big.status).toBe(413);
    const bad = await postReport(h.env, "{not json", "application/csp-report");
    expect(bad.status).toBe(400);
    // A chunked body with no Content-Length is still byte-capped while streaming.
    const chunk = new TextEncoder().encode("x".repeat(16 * 1024));
    let sent = 0;
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        if (sent++ < 8) controller.enqueue(chunk);
        else controller.close();
      },
    });
    const chunked = await app.request(
      "/api/csp-report",
      { method: "POST", headers: { "Content-Type": "application/csp-report" }, body: stream, duplex: "half" } as RequestInit,
      h.env,
    );
    expect(chunked.status).toBe(413);
    expect(sent).toBeLessThan(8); // the reader stopped early
    // A batch of 50 violations is one log line holding the first 20.
    const batch = Array.from({ length: 50 }, () => ({
      type: "csp-violation",
      body: { effectiveDirective: "img-src", blockedURL: "https://x.example/a.png" },
    }));
    expect((await postReport(h.env, JSON.stringify(batch), "application/reports+json")).status).toBe(204);
    expect(log).toHaveBeenCalledTimes(1);
    expect(JSON.parse(String(log.mock.calls[0][0])).violations).toHaveLength(20);

    const many = Array.from({ length: 50 }, () => ({
      type: "csp-violation",
      body: { effectiveDirective: "img-src", blockedURL: "https://x.example/" + "a".repeat(1000) },
    }));
    const parsed = parseCspReports(many);
    expect(parsed).toHaveLength(20);
    expect(parsed[0].blocked!.length).toBe(300);
    expect(parseCspReports({ unrelated: true })).toEqual([]);
  });
});

// The static-asset `_headers` file (build-public writes it). This mirrors the
// Workers Static Assets parser and applier (workers-shared parseHeaders and
// attachCustomHeaders, as bundled in miniflare): path lines start with "/",
// "# " lines are comments, "! Name" detaches a header an earlier matching rule
// set, and a header set again by a later matching rule is appended to.
function applyStaticHeaders(file: string, pathname: string): Headers {
  const rules: Array<{ pattern: RegExp; set: Array<[string, string]>; unset: string[] }> = [];
  for (const raw of file.split("\n")) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    if (/^([^\s]+:\/\/|\/)/.test(line)) {
      const pattern = new RegExp(
        "^" + line.split("*").map((part) => part.replace(/[.+?^${}()|[\]\\]/g, "\\$&")).join(".*") + "$",
      );
      rules.push({ pattern, set: [], unset: [] });
      continue;
    }
    const rule = rules[rules.length - 1];
    expect(rule, `header line before any path: ${line}`).toBeDefined();
    if (!line.includes(":")) {
      expect(line.startsWith("! "), `not a header or unset line: ${line}`).toBe(true);
      rule.unset.push(line.slice(2));
      continue;
    }
    const at = line.indexOf(":");
    const name = line.slice(0, at).trim();
    expect(name).not.toContain(" ");
    rule.set.push([name.toLowerCase(), line.slice(at + 1).trim()]);
  }
  const headers = new Headers();
  const seen = new Set<string>();
  for (const rule of rules.filter((r) => r.pattern.test(pathname))) {
    for (const name of rule.unset) headers.delete(name);
    for (const [name, value] of rule.set) {
      if (seen.has(name)) headers.append(name, value);
      else {
        headers.set(name, value);
        seen.add(name);
      }
    }
  }
  return headers;
}

describe("static asset _headers", () => {
  it("applies the Worker's header set to every static asset path", () => {
    const file = staticAssetHeadersFile();
    for (const path of ["/", "/concepts", "/robots.txt", "/404.html", "/assets/brain/current.json", "/assets/style.css"]) {
      const headers = applyStaticHeaders(file, path);
      expectSecurityHeaders(new Response(null, { headers }));
      expect([...headers.keys()].sort()).toEqual(Object.keys(SECURITY_HEADERS).map((k) => k.toLowerCase()).sort());
    }
  });

  it("lets only the Brain page be framed, and only by this origin", () => {
    const file = staticAssetHeadersFile();
    expect(SAME_ORIGIN_FRAMED_ASSET_PATHS).toEqual(["/brain", "/brain.html"]);
    for (const path of SAME_ORIGIN_FRAMED_ASSET_PATHS) {
      const headers = applyStaticHeaders(file, path);
      expect(headers.get("X-Frame-Options")).toBe("SAMEORIGIN");
      expect(headers.get("Content-Security-Policy")).toBe("frame-ancestors 'self'");
      // Everything else is the shared set, with no appended duplicates.
      expect(headers.get("Content-Security-Policy-Report-Only")).toBe(CSP_REPORT_ONLY);
      expect(headers.get("Strict-Transport-Security")).toBe(SECURITY_HEADERS["Strict-Transport-Security"]);
      expect(headers.get("X-Content-Type-Options")).toBe("nosniff");
    }
    for (const path of ["/brain/", "/brainstorm", "/assets/brain/brain.html"]) {
      expect(applyStaticHeaders(file, path).get("X-Frame-Options")).toBe("DENY");
    }
  });

  it("stays within the static-assets parser limits and never changes the dynamic CSP", () => {
    const file = staticAssetHeadersFile();
    expect(file.endsWith("\n")).toBe(true);
    const lines = file.split("\n").filter((line) => line.trim() && !line.trim().startsWith("#"));
    const paths = lines.filter((line) => line.startsWith("/"));
    expect(paths.length).toBeLessThanOrEqual(STATIC_HEADERS_MAX_RULES);
    for (const line of file.split("\n")) expect(line.length).toBeLessThanOrEqual(STATIC_HEADERS_MAX_LINE_LENGTH);
    // Same Report-Only policy as Worker responses: not a stricter invented one.
    expect(file).toContain(`  Content-Security-Policy-Report-Only: ${CSP_REPORT_ONLY}\n`);
    expect(file.match(/Content-Security-Policy-Report-Only:/g)).toHaveLength(1);
  });
});
