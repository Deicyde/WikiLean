import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { serve } from "@hono/node-server";
import { app } from "../src/index.js";
import { setup } from "../test/helpers/fixture.js";

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

const harness = setup();
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
