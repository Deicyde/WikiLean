// Local Wikibrain server for the Bridge Experiment's arm D.
//
// Production still serves the pre-v3 deployment and this branch must not deploy
// before Jack's review, so the benchmark talks to THIS: the real Hono app (the
// same `app` the wiki test-suite exercises) with the shipped shard bytes served
// from disk and allow-all rate limiters. `wrangler dev` is not an option in this
// environment (spawn EBADF), and a mock would invalidate the experiment — the
// arm must hit the code that would ship.
//
// Run:  node --experimental-strip-types bench/arms/local_worker.mts [port]
// Then: mcp-D.json points at http://localhost:<port>/mcp
import { createServer } from "node:http";
import { existsSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";

process.env.TZ = "UTC";
const { app } = await import("../../wiki/src/index.ts");

const ROOT = resolve(import.meta.dirname, "..", "..");
const ASSET_BASES = [join(ROOT, "site", "assets"), join(ROOT, "wiki", "public", "assets")];
const CT: Record<string, string> = { json: "application/json", html: "text/html" };

const env = {
  // brain/* and /mcp routes touch only ASSETS + limiters; anything else that a
  // stray route reaches gets a loud stub rather than a silent wrong answer
  ASSETS: {
    fetch: async (req: Request | string) => {
      const url = typeof req === "string" ? req : req.url;
      const rel = decodeURIComponent(new URL(url).pathname).replace(/^\/assets\//, "");
      for (const base of ASSET_BASES) {
        const f = join(base, rel);
        if (existsSync(f)) {
          const ext = f.split(".").pop() ?? "";
          return new Response(readFileSync(f), {
            status: 200,
            headers: { "Content-Type": CT[ext] ?? "application/octet-stream" },
          });
        }
      }
      return new Response("not found", { status: 404 });
    },
  },
  BRAIN_API_LIMITER: { limit: async () => ({ success: true }) },
  MCP_LIMITER: { limit: async () => ({ success: true }) },
  EDIT_LIMITER: { limit: async () => ({ success: true }) },
  FLAG_LIMITER: { limit: async () => ({ success: true }) },
  DB: new Proxy({}, { get() { throw new Error("local_worker: DB is not available — a bench route touched D1"); } }),
  RENDER_CACHE: { get: async () => null, put: async () => {}, delete: async () => {} },
  WP_HTML: { get: async () => null, put: async () => {}, delete: async () => {} },
  AUTH_MODE: "dev",
} as never;

const port = Number(process.argv[2] ?? 8790);
createServer(async (req, res) => {
  const chunks: Buffer[] = [];
  for await (const c of req) chunks.push(c as Buffer);
  const body = Buffer.concat(chunks);
  const request = new Request(`http://localhost:${port}${req.url}`, {
    method: req.method,
    headers: req.headers as Record<string, string>,
    body: body.length ? body : undefined,
  });
  try {
    const out = await app.fetch(request, env);
    res.writeHead(out.status, Object.fromEntries(out.headers.entries()));
    res.end(Buffer.from(await out.arrayBuffer()));
  } catch (e) {
    res.writeHead(500, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ ok: false, error: String(e) }));
  }
}).listen(port, () => console.log(`wikibrain local worker on http://localhost:${port} (mcp: /mcp)`));
