// Browser security headers (src/security.ts). Pins: every Worker response
// (HTML page, KV-cache hit, JSON API, 404, redirect) carries HSTS, nosniff,
// the enforced framing ban, Referrer-Policy, and the Report-Only CSP; the CSP
// allows what the pages actually load; the headers never enter RENDER_CACHE
// bytes (so no render-cache key bump); and the /api/csp-report sink logs both
// report shapes and caps its input.

import { afterEach, describe, it, expect, vi } from "vitest";
import { setup, get, blockNetwork, ORIGIN } from "./helpers/harness.js";
import { Hono } from "hono";
import { app } from "../src/index.js";
import { CSP_REPORT_ONLY, SECURITY_HEADERS, parseCspReports, registerSecurityHeaders } from "../src/security.js";
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
    expect(h.renderCache.store.has("page:about:v1")).toBe(true);
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
    // frame-ancestors is ignored in report-only policies; it is enforced
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
        disposition: "report",
        directive: "script-src-elem",
        blocked: "https://evil.example/x.js",
        document: `${ORIGIN}/Test_Article`,
        source: `${ORIGIN}/Test_Article`,
        line: "12",
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
    expect(lines[0]).toMatchObject({
      event: "csp-report",
      directive: "img-src",
      blocked: "https://images.example/a.png",
      document: `${ORIGIN}/stats`,
    });
  });

  it("rejects oversized and malformed bodies, and caps fields and line count", async () => {
    const h = setup();
    vi.spyOn(console, "log").mockImplementation(() => {});
    const big = await postReport(h.env, "x".repeat(17 * 1024), "application/csp-report");
    expect(big.status).toBe(413);
    const bad = await postReport(h.env, "{not json", "application/csp-report");
    expect(bad.status).toBe(400);

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
