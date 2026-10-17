// CSRF guards and request-size caps on the writes that lacked them:
//   /api/watch/:slug (POST, DELETE): session writes → checkOrigin, like every
//     other session-authenticated write;
//   /api/queue[/:db] (POST): checkOrigin, a raw-body byte cap (413) and an item
//     cap (400) before anything reaches KV;
//   /mcp (POST): a raw-body byte cap (413, JSON-RPC error) before parsing;
//   every other JSON body (readJsonCapped, MAX_JSON_BODY_BYTES): a 413 past
//     its cap, the anonymous /api/flag route included.
// The caps refuse a declared Content-Length over the cap without reading the
// body, accept a body of exactly the cap, and run after the auth checks.

import { describe, expect, it } from "vitest";
import { app } from "../src/index.js";
import { MAX_MCP_BODY_BYTES } from "../src/mcp.js";
import { MAX_QUEUE_BODY_BYTES, MAX_QUEUE_ITEMS } from "../src/queue.js";
import { MAX_JSON_BODY_BYTES } from "../src/security.js";
import { blockNetwork, get, post, setup, PIPELINE_TOKEN, SLUG, type Harness } from "./helpers/harness.js";

blockNetwork();

const EVIL = "https://evil.example";

function watchRows(h: Harness): number {
  return (h.db.prepare("SELECT COUNT(*) AS n FROM watchlist").get() as { n: number }).n;
}

function queueItem(i: number) {
  return { qid: `Q${1000 + i}`, label: `Concept ${i}`, decl: `Foo.bar${i}`, status: "unreviewed" };
}

function streamOf(bytes: number, chunkSize = 64 * 1024): { stream: ReadableStream<Uint8Array>; sent: () => number } {
  const chunk = new TextEncoder().encode("x".repeat(chunkSize));
  let sent = 0;
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (sent * chunkSize < bytes) {
        sent += 1;
        controller.enqueue(chunk);
      } else controller.close();
    },
  });
  return { stream, sent: () => sent };
}

// A body of exactly `text`'s bytes, streamed (no Content-Length) in chunks.
function exactStream(text: string, chunkSize = 64 * 1024): ReadableStream<Uint8Array> {
  const bytes = new TextEncoder().encode(text);
  let offset = 0;
  return new ReadableStream<Uint8Array>({
    pull(controller) {
      if (offset >= bytes.byteLength) return controller.close();
      controller.enqueue(bytes.subarray(offset, offset + chunkSize));
      offset += chunkSize;
    },
  });
}

// A JSON object of exactly `size` bytes: `prefix` plus a "pad" string.
function paddedJson(prefix: Record<string, unknown>, size: number): string {
  const head = JSON.stringify({ ...prefix, pad: "" });
  const text = head.slice(0, -2) + "x".repeat(size - head.length) + '"}';
  expect(new TextEncoder().encode(text).byteLength).toBe(size);
  JSON.parse(text);
  return text;
}

function rawRequest(
  h: Harness,
  method: string,
  path: string,
  body: BodyInit,
  headers: Record<string, string> = {},
): Promise<Response> {
  return Promise.resolve(
    app.request(
      path,
      { method, headers: { "Content-Type": "application/json", ...headers }, body, duplex: "half" } as RequestInit,
      h.env,
    ),
  );
}

function rawPost(h: Harness, path: string, body: BodyInit, headers: Record<string, string> = {}): Promise<Response> {
  return rawRequest(h, "POST", path, body, headers);
}

describe("/api/watch CSRF guard", () => {
  it("refuses a cross-origin watch and unwatch from a signed-in browser", async () => {
    const h = setup();
    const cross = await post(h.env, `/api/watch/${SLUG}`, {}, { user: "u-human", origin: EVIL });
    expect(cross.status).toBe(403);
    expect(await cross.json()).toEqual({ ok: false, error: "cross-origin request rejected" });
    expect(watchRows(h)).toBe(0);

    expect((await post(h.env, `/api/watch/${SLUG}`, {}, { user: "u-human" })).status).toBe(200);
    expect(watchRows(h)).toBe(1);

    const unwatch = await app.request(
      `/api/watch/${SLUG}`,
      { method: "DELETE", headers: { Cookie: "wl_dev_user=u-human", Origin: EVIL } },
      h.env,
    );
    expect(unwatch.status).toBe(403);
    expect(watchRows(h)).toBe(1);

    const sameOrigin = await app.request(
      `/api/watch/${SLUG}`,
      { method: "DELETE", headers: { Cookie: "wl_dev_user=u-human", Origin: "http://localhost" } },
      h.env,
    );
    expect(sameOrigin.status).toBe(200);
    expect(watchRows(h)).toBe(0);
  });
});

describe("/api/queue guards", () => {
  it("refuses a cross-origin publish even from an admin session, and accepts the bot", async () => {
    const h = setup();
    const body = { items: [queueItem(1)] };
    const cross = await post(h.env, "/api/queue", body, { user: "u-admin", origin: EVIL });
    expect(cross.status).toBe(403);
    expect(await h.renderCache.get("wikidata:queue")).toBeNull();

    const bot = await post(h.env, "/api/queue", body, { bearer: PIPELINE_TOKEN, origin: null });
    expect(bot.status).toBe(200);
    expect(await bot.json()).toMatchObject({ ok: true, db: "wikidata", count: 1 });

    const sameOriginAdmin = await post(h.env, "/api/queue", body, { user: "u-admin" });
    expect(sameOriginAdmin.status).toBe(200);
  });

  it("caps the item count at MAX_QUEUE_ITEMS with a 400 and leaves the stored queue alone", async () => {
    const h = setup();
    const atCap = Array.from({ length: MAX_QUEUE_ITEMS }, (_, i) => queueItem(i));
    const ok = await post(h.env, "/api/queue", { items: atCap }, { bearer: PIPELINE_TOKEN, origin: null });
    expect(ok.status).toBe(200);
    expect(await ok.json()).toMatchObject({ count: MAX_QUEUE_ITEMS });
    const stored = await h.renderCache.get("wikidata:queue");

    const over = await post(
      h.env,
      "/api/queue",
      { items: [...atCap, queueItem(MAX_QUEUE_ITEMS)] },
      { bearer: PIPELINE_TOKEN, origin: null },
    );
    expect(over.status).toBe(400);
    expect(await over.json()).toEqual({ ok: false, error: `too many items (max ${MAX_QUEUE_ITEMS})` });
    expect(await h.renderCache.get("wikidata:queue")).toBe(stored);
  });

  it("refuses a declared oversize body without reading it", async () => {
    // A small body that only claims to be over the cap: the 413 must come from
    // the Content-Length precheck, not from streaming past the cap.
    const h = setup();
    const { stream, sent } = streamOf(1024);
    const declared = await rawPost(h, "/api/queue", stream, {
      Authorization: `Bearer ${PIPELINE_TOKEN}`,
      "Content-Length": String(MAX_QUEUE_BODY_BYTES + 1),
    });
    expect(declared.status).toBe(413);
    expect(await declared.json()).toEqual({
      ok: false,
      error: `request body too large (max ${MAX_QUEUE_BODY_BYTES} bytes)`,
    });
    expect(sent()).toBeLessThanOrEqual(1); // the stream pre-pulls one chunk
    expect(await h.renderCache.get("wikidata:queue")).toBeNull();
  });

  it("refuses an oversized streamed body with a 413 and stops reading", async () => {
    const h = setup();
    const bearer = { Authorization: `Bearer ${PIPELINE_TOKEN}` };
    const { stream, sent } = streamOf(MAX_QUEUE_BODY_BYTES * 4);
    const chunked = await rawPost(h, "/api/queue/lmfdb", stream, bearer);
    expect(chunked.status).toBe(413);
    expect(sent() * 64 * 1024).toBeLessThan(MAX_QUEUE_BODY_BYTES * 4); // stopped reading early
    expect(await h.renderCache.get("wikidata:queue")).toBeNull();
    expect(await h.renderCache.get("crossref:lmfdb:queue")).toBeNull();
  });

  it("accepts a body of exactly the cap and refuses one byte more", async () => {
    const h = setup();
    const bearer = { Authorization: `Bearer ${PIPELINE_TOKEN}` };
    const atCap = paddedJson({ items: [queueItem(1)] }, MAX_QUEUE_BODY_BYTES);
    const ok = await rawPost(h, "/api/queue", exactStream(atCap), bearer);
    expect(ok.status).toBe(200);
    expect(await ok.json()).toMatchObject({ ok: true, count: 1 });
    const over = paddedJson({ items: [queueItem(2)] }, MAX_QUEUE_BODY_BYTES + 1);
    expect((await rawPost(h, "/api/queue", exactStream(over), bearer)).status).toBe(413);
  });

  it("checks authorization before reading the body", async () => {
    const h = setup();
    const res = await post(h.env, "/api/queue", { items: [queueItem(1)] }, { user: "u-human" });
    expect(res.status).toBe(403);
    // An oversized body from a caller who may not publish is refused as
    // forbidden (403), not as too large (413), and is never read.
    for (const headers of [{}, { Cookie: "wl_dev_user=u-human" }]) {
      const { stream, sent } = streamOf(MAX_QUEUE_BODY_BYTES * 2);
      const refused = await rawPost(h, "/api/queue", stream, headers);
      expect(refused.status).toBe(403);
      expect(sent()).toBeLessThanOrEqual(1);
    }
  });

  it("keeps the bad-json answer for malformed bodies", async () => {
    const h = setup();
    const res = await rawPost(h, "/api/queue", "{nope", { Authorization: `Bearer ${PIPELINE_TOKEN}` });
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ ok: false, error: "bad json (expect {items:[…]})" });
    // JSON null is still "bad json" (it never reached the stored queue before either).
    const nil = await rawPost(h, "/api/queue", "null", { Authorization: `Bearer ${PIPELINE_TOKEN}` });
    expect(nil.status).toBe(400);
    expect(await h.renderCache.get("wikidata:queue")).toBeNull();
  });
});

describe("/mcp body cap", () => {
  it("answers an oversized JSON-RPC body with a 413 JSON-RPC error", async () => {
    const h = setup();
    const params = { name: "brain_search", arguments: { q: "x".repeat(MAX_MCP_BODY_BYTES) } };
    const res = await post(h.env, "/mcp", { jsonrpc: "2.0", id: 7, method: "tools/call", params });
    expect(res.status).toBe(413);
    const body = (await res.json()) as { jsonrpc: string; id: unknown; error: { code: number; message: string } };
    expect(body.jsonrpc).toBe("2.0");
    expect(body.error.code).toBe(-32600);
    expect(body.error.message).toContain("too large");

    const { stream } = streamOf(MAX_MCP_BODY_BYTES * 4, 8 * 1024);
    expect((await rawPost(h, "/mcp", stream)).status).toBe(413);
  });

  it("still serves ordinary requests", async () => {
    const h = setup();
    const res = await post(h.env, "/mcp", { jsonrpc: "2.0", id: 1, method: "ping" });
    expect(res.status).toBe(200);
    expect(await res.json()).toMatchObject({ jsonrpc: "2.0", id: 1 });
    expect((await get(h.env, "/api/queue")).status).toBe(200);
  });

  it("refuses a declared oversize body unread, and takes a body of exactly the cap", async () => {
    const h = setup();
    const { stream, sent } = streamOf(1024, 1024);
    const declared = await rawPost(h, "/mcp", stream, { "Content-Length": String(MAX_MCP_BODY_BYTES + 1) });
    expect(declared.status).toBe(413);
    expect(sent()).toBeLessThanOrEqual(1);
    const atCap = paddedJson({ jsonrpc: "2.0", id: 3, method: "ping" }, MAX_MCP_BODY_BYTES);
    const ok = await rawPost(h, "/mcp", exactStream(atCap, 8 * 1024));
    expect(ok.status).toBe(200);
    expect(await ok.json()).toMatchObject({ jsonrpc: "2.0", id: 3 });
    const over = paddedJson({ jsonrpc: "2.0", id: 4, method: "ping" }, MAX_MCP_BODY_BYTES + 1);
    expect((await rawPost(h, "/mcp", exactStream(over, 8 * 1024))).status).toBe(413);
  });
});

// Every other JSON body read goes through readJsonCapped. Each route is
// reached with credentials that pass its auth and rate-limit checks, so the
// 413 below can only come from the body cap.
describe("JSON body caps on the remaining write routes", () => {
  const bearer = { Authorization: `Bearer ${PIPELINE_TOKEN}` };
  const review = { Cookie: "wl_review_gh=review-session" };
  const routes: Array<{ name: string; method: string; path: string; headers: Record<string, string>; max: number }> = [
    { name: "anonymous flag", method: "POST", path: `/api/flag/${SLUG}`, headers: {}, max: MAX_JSON_BODY_BYTES.flag },
    {
      name: "flag resolve",
      method: "POST",
      path: "/api/flag/1/resolve",
      headers: { Cookie: "wl_dev_user=u-admin" },
      max: MAX_JSON_BODY_BYTES.flagResolve,
    },
    { name: "article create", method: "PUT", path: "/api/article/Brand_New", headers: bearer, max: MAX_JSON_BODY_BYTES.article },
    { name: "article save", method: "POST", path: `/api/article/${SLUG}`, headers: bearer, max: MAX_JSON_BODY_BYTES.article },
    { name: "run report", method: "POST", path: "/api/runs", headers: bearer, max: MAX_JSON_BODY_BYTES.runs },
    { name: "brain edge", method: "POST", path: "/api/brain/edge", headers: bearer, max: MAX_JSON_BODY_BYTES.brainEdge },
    { name: "brain node", method: "POST", path: "/api/brain/node", headers: bearer, max: MAX_JSON_BODY_BYTES.brainNode },
    {
      name: "quickstatements",
      method: "POST",
      path: "/api/brain/quickstatements",
      headers: bearer,
      max: MAX_JSON_BODY_BYTES.quickstatements,
    },
    { name: "repo registration", method: "POST", path: "/api/repos", headers: { Cookie: "wl_dev_user=u-human" }, max: MAX_JSON_BODY_BYTES.repos },
    { name: "repo removal", method: "DELETE", path: "/api/repos", headers: { Cookie: "wl_dev_user=u-human" }, max: MAX_JSON_BODY_BYTES.repos },
    { name: "review post", method: "POST", path: "/api/review/octo/repo/1", headers: review, max: MAX_JSON_BODY_BYTES.reviewPost },
  ];

  function harness(): Harness {
    const h = setup();
    h.env.REVIEW_GITHUB_CLIENT_ID = "Iv1.test";
    h.env.REVIEW_GITHUB_CLIENT_SECRET = "secret";
    h.renderCache.store.set("reviewtok:review-session", JSON.stringify({ token: "gho_test", login: "reviewer" }));
    return h;
  }

  it.each(routes)("$name: a chunked body past the cap is a 413 and is not drained", async (route) => {
    const h = harness();
    const chunk = Math.min(64 * 1024, route.max / 4);
    const { stream, sent } = streamOf(route.max * 4, chunk);
    const res = await rawRequest(h, route.method, route.path, stream, route.headers);
    expect(res.status).toBe(413);
    expect(await res.json()).toEqual({ ok: false, error: `request body too large (max ${route.max} bytes)` });
    expect(sent() * chunk).toBeLessThan(route.max * 4);
  });

  it.each(routes)("$name: a declared Content-Length past the cap is a 413 before reading", async (route) => {
    const h = harness();
    const { stream, sent } = streamOf(1024, 1024);
    const res = await rawRequest(h, route.method, route.path, stream, {
      ...route.headers,
      "Content-Length": String(route.max + 1),
    });
    expect(res.status).toBe(413);
    expect(sent()).toBeLessThanOrEqual(1);
  });

  it("keeps the anonymous flag route working, and its answers for bad JSON", async () => {
    const h = harness();
    const ok = await post(h.env, `/api/flag/${SLUG}`, { reason: "other", comment: "c".repeat(500) }, { origin: null });
    expect(ok.status).toBe(200);
    const bad = await rawPost(h, `/api/flag/${SLUG}`, "{nope");
    expect(bad.status).toBe(400);
    expect(await bad.json()).toEqual({ ok: false, error: "bad json" });
    const badEdge = await rawPost(h, "/api/brain/edge", "{nope", bearer);
    expect(badEdge.status).toBe(400);
    expect(await badEdge.json()).toEqual({ ok: false, error: "bad JSON body" });
  });
});
