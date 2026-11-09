// The articles store's binding (wiki/wrangler.jsonc; docs/ARTICLES.md section 6). Pins: the
// config binds ARTICLES, and nothing else, to the private R2 bucket wikilean-articles; the Worker
// reads the store through the binding the config names; and without the binding every GET
// route the app registers answers as it does with it, except the plane's own, which answer
// 503 "articles store not configured", while the pages outside it keep their bytes.

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";
import { app } from "../src/index.js";
import { NOT_CONFIGURED, titleShard } from "../src/articles.js";
import { blockNetwork, get, setup, PIPELINE_TOKEN, SLUG } from "./helpers/harness.js";
import { articlesFixtureFiles, bindArticles, fakeBucket, FIXTURE_UNIVERSE_SHA, WIDGET_PAGEID } from "./helpers/articles.js";

blockNetwork();

const WRANGLER_CONFIG = resolve(dirname(fileURLToPath(import.meta.url)), "../wrangler.jsonc");

// The end of a JSON string literal that opens at `start`, escapes included.
function stringEnd(text: string, start: number): number {
  let i = start + 1;
  while (i < text.length && text[i] !== '"') i += text[i] === "\\" ? 2 : 1;
  return i + 1;
}

// JSONC as Wrangler reads it: comments, and a comma before a closing bracket, are dropped
// outside strings (the config's URLs keep their "//").
function parseJsonc(text: string): unknown {
  let bare = "";
  for (let i = 0; i < text.length; ) {
    if (text[i] === '"') {
      const end = stringEnd(text, i);
      bare += text.slice(i, end);
      i = end;
    } else if (text.startsWith("//", i)) {
      const end = text.indexOf("\n", i);
      i = end < 0 ? text.length : end;
    } else if (text.startsWith("/*", i)) {
      const end = text.indexOf("*/", i + 2);
      i = end < 0 ? text.length : end + 2;
    } else {
      bare += text[i];
      i += 1;
    }
  }
  let json = "";
  for (let i = 0; i < bare.length; ) {
    if (bare[i] === '"') {
      const end = stringEnd(bare, i);
      json += bare.slice(i, end);
      i = end;
    } else if (bare[i] === "," && /^\s*[}\]]/.test(bare.slice(i + 1))) {
      i += 1;
    } else {
      json += bare[i];
      i += 1;
    }
  }
  return JSON.parse(json);
}

interface WranglerConfig {
  vars: Record<string, string>;
  assets?: { binding?: string };
  version_metadata?: { binding?: string };
  d1_databases?: Array<{ binding: string }>;
  kv_namespaces?: Array<{ binding: string }>;
  r2_buckets?: Array<{ binding: string; bucket_name: string }>;
  unsafe?: { bindings?: Array<{ name: string }> };
}

const config = parseJsonc(readFileSync(WRANGLER_CONFIG, "utf8")) as WranglerConfig;

describe("wrangler.jsonc", () => {
  it("binds ARTICLES, and nothing else, to the private bucket wikilean-articles", () => {
    expect(config.r2_buckets).toEqual([{ binding: "ARTICLES", bucket_name: "wikilean-articles" }]);
    // The reader kept the config's strings whole: a URL's "//" is not a comment.
    expect(config.vars.BETTER_AUTH_URL).toBe("https://wikilean.jackmccarthy.org");
    // No other binding takes the name.
    const names = [
      config.assets?.binding,
      config.version_metadata?.binding,
      ...(config.d1_databases ?? []).map((d) => d.binding),
      ...(config.kv_namespaces ?? []).map((k) => k.binding),
      ...(config.r2_buckets ?? []).map((r) => r.binding),
      ...(config.unsafe?.bindings ?? []).map((b) => b.name),
    ];
    expect(names.filter((name) => name === "ARTICLES")).toHaveLength(1);
    expect(new Set(names).size).toBe(names.length);
  });

  it("names the binding the Worker reads the store through", async () => {
    const [{ binding }] = config.r2_buckets!;
    const h = setup();
    const files = articlesFixtureFiles();
    const bucket = fakeBucket(files);
    (h.env as unknown as Record<string, unknown>)[binding] = bucket;

    const dashboard = await get(h.env, "/articles");
    expect(dashboard.status).toBe(200);
    expect(await dashboard.text()).toMatch(/<h1\b[^>]*>Articles<\/h1>/);
    const current = await get(h.env, "/api/articles/current");
    expect(current.status).toBe(200);
    // The API serves the object as the bucket holds it.
    expect(await current.text()).toBe(files.get("articles/v1/current.json"));
    const article = await get(h.env, `/api/articles/article/${WIDGET_PAGEID}`);
    expect(article.status).toBe(200);

    // Every read went to that bucket, under the store's prefix.
    expect(bucket.gets).toContain("articles/v1/current.json");
    expect(bucket.gets).toContain(`articles/v1/articles/${WIDGET_PAGEID}.json`);
    expect(bucket.gets.every((key) => key.startsWith("articles/v1/"))).toBe(true);
  });
});

// One request path for each GET route pattern with parameters. A new route with
// parameters fails the sweep below until it has one here.
const SAMPLE_PATHS: Record<string, string> = {
  "/api/submissions/:id": "/api/submissions/sub-" + "a".repeat(64),
  "/api/submissions/:id/body": "/api/submissions/sub-" + "a".repeat(64) + "/body",
  "/:slug": `/${SLUG}`,
  "/:slug/history": `/${SLUG}/history`,
  "/:slug/diff/:fromId/:toId": `/${SLUG}/diff/1/1`,
  "/:slug{.+\\.html}": `/${SLUG}.html`,
  "/api/article/:slug{.+\\.json}": `/api/article/${SLUG}.json`,
  "/api/article/:slug/companion.jsonl": `/api/article/${SLUG}/companion.jsonl`,
  "/api/article/:slug/companion-records.json": `/api/article/${SLUG}/companion-records.json`,
  "/api/articles/article/:pageid": `/api/articles/article/${WIDGET_PAGEID}`,
  "/api/articles/overlay/:pageid": `/api/articles/overlay/${WIDGET_PAGEID}`,
  "/api/articles/records/:pageid": `/api/articles/records/${WIDGET_PAGEID}`,
  "/api/articles/universe/:sha": `/api/articles/universe/${FIXTURE_UNIVERSE_SHA}`,
  "/api/articles/index/:shard": "/api/articles/index/1",
  "/api/auth/*": "/api/auth/get-session",
  "/api/queue/:db": "/api/queue/lmfdb",
  "/api/review/:owner/:repo/:pr": "/api/review/octo/widgets/1",
  "/articles/:pageid{[0-9]+}": `/articles/${WIDGET_PAGEID}`,
  "/articles/:pageid{[0-9]+}/:slug": `/articles/${WIDGET_PAGEID}/Widget_lemma`,
  "/articles/universe/:sha": `/articles/universe/${FIXTURE_UNIVERSE_SHA}`,
  "/decl/:name": "/decl/Nat.Prime",
  "/queue/:db": "/queue/lmfdb",
  "/u/:id": "/u/u-human",
};

// The plane's own routes read the store; the feedback export and the community overlay
// (/api/articles/overlay/:pageid, migration 0019) read only D1, and
// /api/articles (the header search box's index of WikiLean's own articles, index.ts)
// is not the plane's at all.
const readsStore = (path: string) =>
  (path === "/articles" || path.startsWith("/articles/") || path.startsWith("/api/articles/")) &&
  path !== "/api/articles/feedback/export" &&
  !path.startsWith("/api/articles/overlay/");

function getPaths(): string[] {
  const patterns = [...new Set(app.routes.filter((route) => route.method === "GET").map((route) => route.path))];
  return patterns.map((pattern) => {
    if (!/[:*]/.test(pattern)) return pattern;
    const path = SAMPLE_PATHS[pattern];
    expect(path, `a sample path for the GET route ${pattern}`).toBeDefined();
    return path;
  });
}

// A page from each shell that carries the site's navigation (/flags and /proposals send an
// anonymous reader to sign in), and beside them the sitemap and the article index.
const SHELL_PATHS = [
  "/", "/about", "/stats", "/recent-changes", "/flags", "/proposals", `/${SLUG}`,
  `/${SLUG}/history`, "/u/u-human", "/wikifunctions", "/wikifunctions/verify", "/sitemap.xml",
  "/api/articles",
];

describe("without the ARTICLES binding", () => {
  it("every GET route answers as it does with the binding, except the plane's own: 503, not configured", async () => {
    const absent = setup();
    const present = setup();
    bindArticles(present.env);
    const paths = getPaths();
    expect(paths.length).toBeGreaterThan(60);
    // The dashboard, the reader (two routes), the universe page and the five JSON APIs at least.
    expect(paths.filter(readsStore).length).toBeGreaterThanOrEqual(9);
    for (const path of paths) {
      const without = await get(absent.env, path);
      const withIt = await get(present.env, path);
      if (readsStore(path)) {
        expect(without.status, path).toBe(503);
        expect(await without.text(), path).toContain(NOT_CONFIGURED);
        // Bound, the same route reads the store instead.
        expect(withIt.status, path).not.toBe(503);
      } else {
        expect(without.status, path).toBe(withIt.status);
        expect(without.headers.get("Content-Type"), path).toBe(withIt.headers.get("Content-Type"));
      }
    }
  });

  it("pages outside the plane are the same headers and bytes as with the binding, and only an article page reads it", async () => {
    // One clock for both harnesses, so their rows and every rendered time are the same.
    vi.useFakeTimers({ toFake: ["Date"], now: new Date("2026-11-02T12:00:00Z") });
    try {
      const absent = setup();
      const present = setup();
      const bucket = bindArticles(present.env);
      for (const path of SHELL_PATHS) {
        const without = await get(absent.env, path);
        const withIt = await get(present.env, path);
        expect(without.status, path).toBe(withIt.status);
        expect([...without.headers], path).toEqual([...withIt.headers]);
        expect(await without.text(), path).toBe(await withIt.text());
      }
      // The article page looked for its box (this title has none) in the publish switch and
      // its title's index shard; no other page touched the bucket.
      const shard = await titleShard("Test Article");
      expect(new Set(bucket.gets)).toEqual(new Set(["articles/v1/current.json", `articles/v1/index/${shard}.json`]));
    } finally {
      vi.useRealTimers();
    }
  });

  it("the feedback export, which reads only D1, still answers the pipeline bearer", async () => {
    const h = setup();
    const res = await get(h.env, "/api/articles/feedback/export", { bearer: PIPELINE_TOKEN });
    expect(res.status).toBe(200);
    expect(res.headers.get("Cache-Control")).toBe("no-store");
  });
});
