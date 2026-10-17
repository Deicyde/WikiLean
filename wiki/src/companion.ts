// Literature Links export of one native article snapshot, from a single D1 read.
// The schema is vendored from Literature Links under vendor/literature-links.
import { Hono } from "hono";
import { drizzle } from "drizzle-orm/d1";
import { eq } from "drizzle-orm";
import { articles } from "./db/schema.js";
import type { Env } from "./env.js";
import {
  NATIVE_SCHEMA_VERSION, storedCompanionRecords, validateNativeItems, isAbsoluteReference, type JsonObject,
} from "./native-companion.js";

/** Each annotation on its own line; an empty list is an empty document. */
export function toJsonl(annotations: unknown[]): string {
  return annotations.map((annotation) => JSON.stringify(annotation) + "\n").join("");
}

export function registerCompanionRoutes(app: Hono<{ Bindings: Env }>): void {
  // The stored records of a native article, projected; nothing is fetched.
  const nativeRecords = async (env: Env, slug: string) => {
    const db = drizzle(env.DB);
    const row = (await db.select().from(articles).where(eq(articles.slug, slug)).limit(1))[0];
    if (!row) return { status: 404 as const, body: { ok: false, error: "unknown slug" } };
    if (row.schemaVersion !== NATIVE_SCHEMA_VERSION) return { status: 409 as const, body: { ok: false, error: "explicit native migration required" } };
    const items = JSON.parse(row.annotations) as JsonObject[];
    if (validateNativeItems(items).length) return { status: 409 as const, body: { ok: false, error: "invalid native stored snapshot" } };
    return { row, records: storedCompanionRecords(items, { wikipediaTitle: row.wikipediaTitle, revid: row.revid, wikidataQid: row.wikidataQid }) };
  };
  // Lossless stored-record read: carrier bases accompany raw references.
  app.get("/api/article/:slug/companion-records.json", async (c) => {
    const result = await nativeRecords(c.env, c.req.param("slug"));
    if ("status" in result) return c.json(result.body, result.status);
    c.header("Cache-Control", "no-store");
    return c.json({ version: result.row.version, records: result.records });
  });
  // One annotation per line, as Literature Links reads a list of annotations
  // from a file or a web response.
  app.get("/api/article/:slug/companion.jsonl", async (c) => {
    const result = await nativeRecords(c.env, c.req.param("slug"));
    if ("status" in result) return c.json(result.body, result.status);
    if (result.records.some(({ base_uri, annotation }) => base_uri !== undefined
      || [...annotation.subject, ...annotation.object].some(end => !isAbsoluteReference(end.uri))
      || (annotation.$schema !== undefined && !isAbsoluteReference(annotation.$schema)))) {
      return c.json({ ok: false, error: "relative records require their original carrier bases",
        records_url: `/api/article/${encodeURIComponent(result.row.slug)}/companion-records.json` }, 409);
    }
    return new Response(toJsonl(result.records.map(record => record.annotation)), { headers: {
      "Content-Type": "application/x-ndjson; charset=utf-8", "Cache-Control": "no-store",
      "X-Companion-Annotations": String(result.records.length),
    } });
  });
}
