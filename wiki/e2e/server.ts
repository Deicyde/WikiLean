import { execFileSync } from "node:child_process";
import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { serve } from "@hono/node-server";
import type { Env } from "../src/env.js";
import { app } from "../src/index.js";
import { migrateLegacyItems } from "../src/native-companion.js";
import { installBrainFixture } from "../test/helpers/brain-fixture.js";
import { SEED_ANNOTATIONS, WP_FIXTURE, REVID, setup } from "../test/helpers/fixture.js";

const HOST = "127.0.0.1";
const PORT = Number.parseInt(process.env.WIKILEAN_E2E_PORT ?? "4173", 10);
const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

const SOURCE_ASSETS = new Map<string, { path: string; contentType: string }>([
  ["/assets/style.css", { path: resolve(ROOT, "../site/assets/style.css"), contentType: "text/css; charset=utf-8" }],
  ["/assets/script.js", { path: resolve(ROOT, "../site/assets/script.js"), contentType: "text/javascript; charset=utf-8" }],
  ["/assets/review.css", { path: resolve(ROOT, "../site/assets/review.css"), contentType: "text/css; charset=utf-8" }],
  ["/assets/editor.js", { path: resolve(ROOT, "assets/editor.js"), contentType: "text/javascript; charset=utf-8" }],
]);

const GENERATED_ASSET_FIXTURES = new Map<string, { body: string; contentType: string }>([
  ["/favicon.ico", { body: "", contentType: "image/x-icon" }],
  ["/assets/mathlib-index.json", { body: "[]", contentType: "application/json" }],
  [
    "/assets/decl-index/manifest.json",
    { body: JSON.stringify({ v: 1, scheme: { max_len: 2 }, shards: {} }), contentType: "application/json" },
  ],
]);

// Production serves /brain asset-first: the release-neutral page that
// site/build_brain_page.py generates, which then selects an immutable release
// through /assets/brain/current.json. The harness serves those page bytes with
// one change, d3 from a loopback stand-in instead of the CDN (the harness never
// leaves loopback; drawing is not under test), plus the Worker tests' fixture
// release. The home page embeds /brain, so every spec that opens / loads it too.
const D3_CDN_TAG = '<script src="https://cdn.jsdelivr.net/npm/d3@7"></script>';
const INERT_D3_PATH = "/__e2e/inert-d3.js";
const generatedBrainPage = execFileSync(
  "python3",
  [
    "-I",
    "-B",
    "-c",
    "import sys; sys.path.insert(0, sys.argv[1]); import build_brain_page; sys.stdout.buffer.write(build_brain_page.HTML.encode('utf-8'))",
    resolve(ROOT, "../site"),
  ],
  { encoding: "utf8", maxBuffer: 16 * 1024 * 1024 },
);
if (generatedBrainPage.split(D3_CDN_TAG).length !== 2) {
  throw new Error(`site/build_brain_page.py no longer has exactly one ${D3_CDN_TAG}; update the browser harness`);
}
const BRAIN_PAGE = generatedBrainPage.replace(D3_CDN_TAG, `<script src="${INERT_D3_PATH}"></script>`);
// Every d3 call returns the same inert callable, so module-level setup runs and
// nothing draws. It is never a thenable, coerces to 0 or "", and iterates empty.
const INERT_D3 = `(() => {
  const inert = new Proxy(function () { "use strict"; }, {
    get: (_target, key) =>
      key === "then" ? undefined
        : key === Symbol.toPrimitive ? (hint) => (hint === "number" ? 0 : "")
        : key === Symbol.iterator ? function* () {}
        : inert,
    apply: () => inert,
    construct: () => inert,
  });
  globalThis.d3 = inert;
})();
`;
const brainRelease = {} as Env;
installBrainFixture(brainRelease);

const harness = setup();
const nativeSeed = migrateLegacyItems(SEED_ANNOTATIONS, { wikipediaTitle: "Native Article", revid: REVID }).items;
nativeSeed[0].links.push({ id: "pdf-companion", base_uri: "https://example.net/annotations/group.json", annotation: {
  subject: [{ type: "pdf", uri: "../group.pdf", location: { page: 2 }, extensions: { publisher: { retained: true } } }],
  object: [{ type: "generic", uri: "../lean.html#External" }], extensions: { custom: { nested: [1, "keep", null] } },
} });
nativeSeed[1].unlinked.push({ type: "html", uri: "https://example.net/narrative.html", location: { fragment: "result" } });
harness.db.prepare("INSERT INTO articles (slug,wikipedia_title,display_title,revid,annotations,schema_version,version,created_at,updated_at) VALUES (?,?,?,?,?,4,1,?,?)")
  .run("Native_Article", "Native Article", "Native Article", REVID, JSON.stringify(nativeSeed), Date.now(), Date.now());
await harness.wpHtml.put(`wp:Native_Article:${REVID}`, WP_FIXTURE);
const runtimeFetch = globalThis.fetch;
globalThis.fetch = (async (input: RequestInfo | URL) => {
  throw new Error(`browser harness attempted an external fetch: ${String(input)}`);
}) as typeof globalThis.fetch;

async function fetch(request: Request): Promise<Response> {
  const pathname = new URL(request.url).pathname;
  const asset = SOURCE_ASSETS.get(pathname);
  if (asset) {
    return new Response(await readFile(asset.path), {
      headers: {
        "Cache-Control": "no-store",
        "Content-Type": asset.contentType,
        "X-Content-Type-Options": "nosniff",
      },
    });
  }
  if (pathname === "/brain") {
    return new Response(BRAIN_PAGE, {
      headers: { "Cache-Control": "no-store", "Content-Type": "text/html; charset=utf-8" },
    });
  }
  if (pathname === INERT_D3_PATH) {
    return new Response(INERT_D3, {
      headers: { "Cache-Control": "no-store", "Content-Type": "text/javascript; charset=utf-8" },
    });
  }
  if (pathname.startsWith("/assets/brain/")) return brainRelease.ASSETS.fetch(request);
  const generated = GENERATED_ASSET_FIXTURES.get(pathname);
  if (generated) {
    return new Response(generated.body, {
      headers: { "Cache-Control": "no-store", "Content-Type": generated.contentType },
    });
  }
  return app.fetch(request, harness.env);
}

const server = serve({ fetch, hostname: HOST, port: PORT }, (info) => {
  console.log(`WikiLean browser harness listening on http://${HOST}:${info.port}`);
});

function shutdown(): void {
  server.close(() => {
    globalThis.fetch = runtimeFetch;
    harness.db.close();
    process.exit(0);
  });
}

process.once("SIGINT", shutdown);
process.once("SIGTERM", shutdown);
