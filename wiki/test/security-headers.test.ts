// Browser security headers (src/security.ts). Pins: every Worker response
// (HTML page, KV-cache hit, JSON API, 404, redirect) carries HSTS, nosniff,
// the enforced framing ban, Referrer-Policy, and the Report-Only CSP; the CSP
// allows what the pages actually load; a route's own enforced policy (the
// reader's articlesCsp, on the reader and every other page of the articles plane) is kept with the
// framing ban listed beside it, never in place of it; the headers never enter RENDER_CACHE
// bytes (so no render-cache key bump); the /api/csp-report sink logs both
// report shapes and caps its input and its per-IP request rate; and the static-asset `_headers` file that
// build-public generates applies the same set to assets the Worker never sees.

import { afterEach, describe, it, expect, vi } from "vitest";
import { setup, get, blockNetwork, ORIGIN } from "./helpers/harness.js";
import { Hono } from "hono";
import { app } from "../src/index.js";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import {
  ARTICLES_FORM_PATH,
  ARTICLES_INLINE_SCRIPT_HASHES,
  ARTICLES_MEDIA_HOSTS,
  ARTICLES_SCRIPT_PATHS,
  TURNSTILE_ORIGIN,
  articlesCsp,
  BRAIN_CSP_EXTRA_SOURCES,
  BRAIN_CSP_REPORT_ONLY,
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
import { articlesReaderFiles, bindArticles, WIDGET_PAGEID } from "./helpers/articles.js";

blockNetwork();

// The articles pages' policy for the origin the harness serves (app.request's).
const ARTICLES_CSP = articlesCsp(ORIGIN);

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

function directive(name: string, policy = CSP_REPORT_ONLY): string[] {
  const d = policy.split("; ").find((part) => part.startsWith(name + " "));
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

  it("the landing page links to the Brain without embedding it or widening frame access", async () => {
    const h = setup();
    const html = await (await get(h.env, "/")).text();
    expect(html).toContain('href="/brain"');
    expect(html).not.toContain("<iframe");
    expect(directive("frame-src")).toEqual(["'self'"]);
  });

  it("keep a route's own enforced policy and list the framing ban beside it, never in place of it", async () => {
    const ban = SECURITY_HEADERS["Content-Security-Policy"];
    const mini = new Hono<{ Bindings: Env }>();
    registerSecurityHeaders(mini);
    mini.get("/own", (c) => c.text("x", 200, { "Content-Security-Policy": "default-src 'none'" }));
    mini.get("/ban", (c) => c.text("x", 200, { "Content-Security-Policy": ban }));
    mini.get("/listed", (c) => c.text("x", 200, { "Content-Security-Policy": `default-src 'none', ${ban}` }));
    // A route that tries to allow framing still gets the ban as a policy of its own.
    mini.get("/framed", (c) => c.text("x", 200, { "Content-Security-Policy": "frame-ancestors *" }));
    mini.get("/raw", () => new Response("x", { headers: { "Content-Security-Policy": "default-src 'none'" } }));
    const csp = async (path: string) => (await mini.request(path)).headers.get("Content-Security-Policy");
    expect(await csp("/own")).toBe(`default-src 'none', ${ban}`);
    expect(await csp("/ban")).toBe(ban);
    expect(await csp("/listed")).toBe(`default-src 'none', ${ban}`);
    expect(await csp("/framed")).toBe(`frame-ancestors *, ${ban}`);
    expect(await csp("/raw")).toBe(`default-src 'none', ${ban}`);
    const res = await mini.request("/own");
    expect(res.headers.get("Content-Security-Policy-Report-Only")).toBe(CSP_REPORT_ONLY);
    expect(res.headers.get("X-Frame-Options")).toBe("DENY");
  });
});

// The formalization reader's pages enforce articlesCsp (src/articles.ts articlesHtml), listed beside the
// framing ban; every other header is the shared set. test/articles.test.ts checks the policy against
// what the pages hold, and e2e/articles.spec.ts runs the reader under it in Chromium.
describe("the articles pages' enforced policy", () => {
  function policy(text: string): Map<string, string[]> {
    return new Map(text.split("; ").map((part) => {
      const [name, ...sources] = part.split(" ");
      return [name, sources] as [string, string[]];
    }));
  }

  it("is served on the reader and every other articles page, beside the framing ban and with every shared header", async () => {
    const h = setup();
    bindArticles(h.env, articlesReaderFiles());
    const pages = [
      `/articles/${WIDGET_PAGEID}/Widget_lemma`, `/articles/${WIDGET_PAGEID}`, `/articles/${WIDGET_PAGEID}/Widget_lemma?view=cards`,
      "/articles", "/articles?q=wid", "/articles/9999",
    ];
    for (const path of pages) {
      const res = await get(h.env, path);
      expect(res.headers.get("Content-Type"), path).toMatch(/^text\/html/);
      expect(res.headers.get("Content-Security-Policy"), path).toBe(`${ARTICLES_CSP}, ${SECURITY_HEADERS["Content-Security-Policy"]}`);
      for (const [name, value] of Object.entries(SECURITY_HEADERS)) {
        if (name !== "Content-Security-Policy") expect(res.headers.get(name), `${path}: ${name}`).toBe(value);
      }
    }
    // An unbound store's 503 pages and an over-budget 429 page are articles pages too.
    const unbound = setup();
    for (const path of [`/articles/${WIDGET_PAGEID}/Widget_lemma`, "/articles", "/articles/universe/" + "0".repeat(64)]) {
      const res = await get(unbound.env, path);
      expect(res.status, path).toBe(503);
      expect(res.headers.get("Content-Security-Policy"), path).toBe(`${ARTICLES_CSP}, ${SECURITY_HEADERS["Content-Security-Policy"]}`);
    }
    const limited = setup({ brainApiLimiterAllows: false });
    bindArticles(limited.env, articlesReaderFiles());
    const over = await get(limited.env, `/articles/${WIDGET_PAGEID}/Widget_lemma`);
    expect(over.status).toBe(429);
    expect(over.headers.get("Content-Security-Policy")).toBe(`${ARTICLES_CSP}, ${SECURITY_HEADERS["Content-Security-Policy"]}`);
    // The plane's JSON and every page outside it keep the ban alone.
    for (const path of [`/api/articles/article/${WIDGET_PAGEID}`, "/api/articles/current"]) {
      expectSecurityHeaders(await get(h.env, path));
    }
    expectSecurityHeaders(await get(h.env, "/Test_Article"));
    expectSecurityHeaders(await get(h.env, "/about"));
  });

  it("allows no script but the plane's two files and the shell's two inline ones, nothing from another host but the beacon and Wikimedia media", () => {
    const p = policy(ARTICLES_CSP);
    expect(p.get("default-src")).toEqual(["'none'"]);
    // The plane's script files by URL on the page's own origin, never 'self' (every same-origin script).
    expect(ARTICLES_SCRIPT_PATHS).toEqual(["/assets/articles.js", "/assets/articles-reader.js"]);
    expect(p.get("script-src")).toEqual([
      ...ARTICLES_SCRIPT_PATHS.map((path) => ORIGIN + path),
      ARTICLES_INLINE_SCRIPT_HASHES.theme, ARTICLES_INLINE_SCRIPT_HASHES.search, "https://static.cloudflareinsights.com",
    ]);
    for (const hash of Object.values(ARTICLES_INLINE_SCRIPT_HASHES)) expect(hash).toMatch(/^'sha256-[A-Za-z0-9+/]{43}='$/);
    expect(p.get("connect-src")).toEqual(["'self'", "https://cloudflareinsights.com"]);
    expect(p.get("img-src")).toEqual(["'self'", ...ARTICLES_MEDIA_HOSTS]);
    expect(p.get("media-src")).toEqual(["'self'", ...ARTICLES_MEDIA_HOSTS]);
    expect(ARTICLES_MEDIA_HOSTS).toEqual(["https://upload.wikimedia.org", "https://thumb.wikimedia.org"]);
    // The revision's TemplateStyles and style attributes, and the coverage bar's widths.
    expect(p.get("style-src")).toEqual(["'self'", "'unsafe-inline'"]);
    expect(p.get("font-src")).toEqual(["'self'"]);
    // The dashboard's search, the one form an articles page has.
    expect(p.get("form-action")).toEqual([ORIGIN + ARTICLES_FORM_PATH]);
    expect(p.get("base-uri")).toEqual(["'none'"]);
    expect(p.get("object-src")).toEqual(["'none'"]);
    expect(p.get("report-uri")).toEqual(["/api/csp-report"]);
    expect(p.get("report-to")).toEqual(["csp"]);
    // Nothing that would reopen inline script or eval, no wildcard or data: source, no framing, and
    // one policy (the ban is the second).
    expect(p.get("script-src")).not.toContain("'unsafe-inline'");
    expect(p.get("script-src")).not.toContain("'self'");
    expect(ARTICLES_CSP).not.toMatch(/'unsafe-eval'|'unsafe-hashes'|'wasm-unsafe-eval'|'strict-dynamic'|\*|data:|blob:/);
    expect([...p.keys()].filter((name) => /^(?:frame-src|child-src|worker-src|frame-ancestors|sandbox)$/.test(name))).toEqual([]);
    expect(ARTICLES_CSP).not.toContain(",");
  });

  // A page that renders the Turnstile widget — a new account's first community
  // tag, and only with a site key configured — widens the policy by exactly one
  // origin, in exactly three directives. Every other page keeps the policy
  // above, which frames nothing at all.
  it("the Turnstile variant adds one origin and nothing else", () => {
    const base = policy(ARTICLES_CSP);
    const widened = policy(articlesCsp(ORIGIN, true));
    expect([...widened.keys()].sort()).toEqual([...base.keys(), "frame-src"].sort());
    expect(widened.get("frame-src")).toEqual([TURNSTILE_ORIGIN]);
    expect(widened.get("script-src")).toEqual([...base.get("script-src")!, TURNSTILE_ORIGIN]);
    expect(widened.get("connect-src")).toEqual([...base.get("connect-src")!, TURNSTILE_ORIGIN]);
    for (const name of ["default-src", "style-src", "img-src", "media-src", "font-src", "form-action", "base-uri", "object-src"]) {
      expect(widened.get(name), name).toEqual(base.get(name));
    }
    expect(TURNSTILE_ORIGIN).toBe("https://challenges.cloudflare.com");
    expect(articlesCsp(ORIGIN, true)).not.toContain(",");
  });
});

describe("POST /api/csp-report", () => {
  it("caps each IP under its own csp:<ip> key before reading the body", async () => {
    const h = setup();
    const keys: string[] = [];
    (h.env as { FLAG_LIMITER: unknown }).FLAG_LIMITER = {
      limit: async ({ key }: { key: string }) => {
        keys.push(key);
        return { success: false };
      },
    };
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const res = await app.request(
      "/api/csp-report",
      { method: "POST", headers: { "Content-Type": "application/csp-report", "CF-Connecting-IP": "203.0.113.7" }, body: "{not json" },
      h.env,
    );
    // Refused before the body is parsed: 429, not the 400 that malformed JSON gets.
    expect(res.status).toBe(429);
    expectSecurityHeaders(res);
    expect(keys).toEqual(["csp:203.0.113.7"]);
    expect(log).not.toHaveBeenCalled();
  });

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
    for (const path of [
      "/", "/concepts", "/robots.txt", "/404.html", "/assets/brain/current.json", "/assets/style.css",
      // The formalization reader's assets (build-public copies them from site/assets).
      "/assets/articles.css", "/assets/articles.js", "/assets/articles-reader.css", "/assets/articles-reader.js",
    ]) {
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
      // The Brain's own Report-Only policy replaces (never appends to) the shared one.
      expect(headers.get("Content-Security-Policy-Report-Only")).toBe(BRAIN_CSP_REPORT_ONLY);
      expect(headers.get("Strict-Transport-Security")).toBe(SECURITY_HEADERS["Strict-Transport-Security"]);
      expect(headers.get("X-Content-Type-Options")).toBe("nosniff");
      expect(headers.get("Reporting-Endpoints")).toBe(SECURITY_HEADERS["Reporting-Endpoints"]);
    }
    for (const path of ["/brain/", "/brainstorm", "/assets/brain/brain.html"]) {
      const headers = applyStaticHeaders(file, path);
      expect(headers.get("X-Frame-Options")).toBe("DENY");
      expect(headers.get("Content-Security-Policy-Report-Only")).toBe(CSP_REPORT_ONLY);
    }
  });

  it("gives the Brain page a Report-Only policy that allows exactly what it loads", () => {
    // site/build_brain_page.py writes brain.html: an external d3 <script src>
    // and fetch() calls to Wikipedia and Wikidata. Under the shared policy each
    // was a violation on every landing view (home.ts frames /brain?embed=1).
    const builder = readFileSync(fileURLToPath(new URL("../../site/build_brain_page.py", import.meta.url)), "utf8");
    const origins = (re: RegExp) => [...new Set([...builder.matchAll(re)].map((m) => new URL(m[1]).origin))].sort();
    const scripts = origins(/<script[^>]*\ssrc="(https:\/\/[^"]+)"/g);
    const fetches = origins(/fetch\(\s*["'`](https:\/\/[^"'`]+)/g);
    expect(scripts).toEqual(["https://cdn.jsdelivr.net"]);
    expect(fetches).toEqual(["https://en.wikipedia.org", "https://www.wikidata.org"]);
    expect([...BRAIN_CSP_EXTRA_SOURCES["script-src"]].sort()).toEqual(scripts);
    expect([...BRAIN_CSP_EXTRA_SOURCES["connect-src"]].sort()).toEqual(fetches);
    for (const origin of scripts) {
      expect(directive("script-src", BRAIN_CSP_REPORT_ONLY)).toContain(origin);
      expect(directive("script-src")).not.toContain(origin);
    }
    for (const origin of fetches) {
      expect(directive("connect-src", BRAIN_CSP_REPORT_ONLY)).toContain(origin);
      expect(directive("connect-src")).not.toContain(origin);
    }
    // Otherwise identical to the shared policy.
    const strip = (policy: string) =>
      policy
        .split("; ")
        .map((d) => d.split(" ").filter((src) => !scripts.includes(src) && !fetches.includes(src)).join(" "));
    expect(strip(BRAIN_CSP_REPORT_ONLY)).toEqual(strip(CSP_REPORT_ONLY));
  });

  it("stays within the static-assets parser limits and never changes the dynamic CSP", () => {
    const file = staticAssetHeadersFile();
    expect(file.endsWith("\n")).toBe(true);
    const lines = file.split("\n").filter((line) => line.trim() && !line.trim().startsWith("#"));
    const paths = lines.filter((line) => line.startsWith("/"));
    expect(paths.length).toBeLessThanOrEqual(STATIC_HEADERS_MAX_RULES);
    for (const line of file.split("\n")) expect(line.length).toBeLessThanOrEqual(STATIC_HEADERS_MAX_LINE_LENGTH);
    // Same Report-Only policy as Worker responses on every path but the Brain
    // page, whose rule replaces it with BRAIN_CSP_REPORT_ONLY.
    expect(file).toContain(`/*\n`);
    expect(file).toContain(`  Content-Security-Policy-Report-Only: ${CSP_REPORT_ONLY}\n`);
    expect(file.match(/Content-Security-Policy-Report-Only: /g)).toHaveLength(1 + SAME_ORIGIN_FRAMED_ASSET_PATHS.length);
    expect(file.match(/! Content-Security-Policy-Report-Only\n/g)).toHaveLength(SAME_ORIGIN_FRAMED_ASSET_PATHS.length);
  });
});
