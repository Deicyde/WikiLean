// CSRF guards and request-size caps on the writes that lacked them:
//   /api/watch/:slug (POST, DELETE): session writes → checkOrigin, like every
//     other session-authenticated write;
//   /api/queue[/:db] (POST): checkOrigin, a raw-body byte cap (413) and an item
//     cap (400) before anything reaches KV;
//   /mcp (POST): a raw-body byte cap (413, JSON-RPC error) before parsing.

import { describe, expect, it } from "vitest";
import { app } from "../src/index.js";
import { MAX_MCP_BODY_BYTES } from "../src/mcp.js";
import { MAX_QUEUE_BODY_BYTES, MAX_QUEUE_ITEMS } from "../src/queue.js";
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

function rawPost(h: Harness, path: string, body: BodyInit, headers: Record<string, string> = {}): Promise<Response> {
  return Promise.resolve(
    app.request(
      path,
      { method: "POST", headers: { "Content-Type": "application/json", ...headers }, body, duplex: "half" } as RequestInit,
      h.env,
    ),
  );
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

  it("refuses an oversized body with a 413, by declared length and while streaming", async () => {
    const h = setup();
    const bearer = { Authorization: `Bearer ${PIPELINE_TOKEN}` };
    const big = JSON.stringify({ items: [{ ...queueItem(1), label: "x".repeat(MAX_QUEUE_BODY_BYTES) }] });
    const declared = await rawPost(h, "/api/queue", big, { ...bearer, "Content-Length": String(big.length) });
    expect(declared.status).toBe(413);
    expect(await declared.json()).toEqual({
      ok: false,
      error: `request body too large (max ${MAX_QUEUE_BODY_BYTES} bytes)`,
    });

    const { stream, sent } = streamOf(MAX_QUEUE_BODY_BYTES * 4);
    const chunked = await rawPost(h, "/api/queue/lmfdb", stream, bearer);
    expect(chunked.status).toBe(413);
    expect(sent() * 64 * 1024).toBeLessThan(MAX_QUEUE_BODY_BYTES * 4); // stopped reading early
    expect(await h.renderCache.get("wikidata:queue")).toBeNull();
    expect(await h.renderCache.get("crossref:lmfdb:queue")).toBeNull();
  });

  it("checks authorization before reading the body", async () => {
    const h = setup();
    const res = await post(h.env, "/api/queue", { items: [queueItem(1)] }, { user: "u-human" });
    expect(res.status).toBe(403);
  });

  it("keeps the bad-json answer for malformed bodies", async () => {
    const h = setup();
    const res = await rawPost(h, "/api/queue", "{nope", { Authorization: `Bearer ${PIPELINE_TOKEN}` });
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ ok: false, error: "bad json (expect {items:[…]})" });
    const empty = await rawPost(h, "/api/queue", "null", { Authorization: `Bearer ${PIPELINE_TOKEN}` });
    expect(empty.status).toBe(200);
    expect(await empty.json()).toMatchObject({ count: 0 });
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
});
