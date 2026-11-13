// Token-gated downloads of the declaration-index bundles (docs/ROADMAP.md, outside
// contributors: Jack, 2026-11-11, "Token-gated download (Recommended)"). An outside
// contributor's pipeline needs a frozen universe's declaration index and search corpus,
// about half a gigabyte, too big for git or a Worker asset, so the Worker streams it from
// the private bucket CONTRIB to the holder of an owned agent token.
//
//   GET  /api/index/bundles                       the live bundles (agent)
//   GET  /api/index/bundles/<sha>                 a bundle's MANIFEST.json (agent)
//   GET  /api/index/bundles/<sha>/files/<path>    one file of it, resumable with Range (agent)
//   POST /api/index/bundles                       register an uploaded bundle (pipeline)
//
// <sha> is the sha256 of the bundle's MANIFEST.json, which lists every other file with its
// sha256, so a client checks the manifest against the URL and each file against the
// manifest. The Worker never writes the bucket and never buffers a file:
// scripts/articles/upload_contrib.py puts a bundle under index/v1/<sha>/, the coordinator
// registers it here, and a download passes R2's body through.
//
// Controls that need no deploy: the index_downloads lever (migration 0022 seeds it
// 'closed'), a retire switch per bundle (index_bundles.state), revoking the token at
// /agents or blocking its owner, and a daily byte quota per owner, written by the same
// statement that re-checks access, which is also the download log (index_downloads; never
// an IP). Each bundle carries its libraries' licence texts (package_index.py
// --licence-texts), because a copy handed out cannot be recalled.
import type { Context, Hono } from "hono";
import type { Env } from "./env.js";
import { getUser, type AuthUser } from "./auth.js";
import { agreementStands, CC0_ACCEPTED_VERSIONS, loadAgreement } from "./articles-overlay.js";
import { checkOrigin, readJsonCapped, MAX_JSON_BODY_BYTES } from "./security.js";
import { leverOpen, writeBrake } from "./site-settings.js";
import { refuse, requirePipeline } from "./submissions.js";

type Ctx = Context<{ Bindings: Env }>;
export const INDEX_DOWNLOADS_LEVER = "index_downloads";
export const INDEX_PREFIX = "index/v1/";
export const BUNDLE_SCHEMA = "wikilean.pipeline.index-bundle/2";
export const MANIFEST_NAME = "MANIFEST.json";
// A day's downloads per owner, all of their agents together. A bundle is about half a
// gigabyte, so this is several whole downloads, and a bounded cost if a token leaks.
export const INDEX_DAILY_BYTES = 4 * 1024 ** 3;
// The Worker reads a MANIFEST.json whole when it registers the bundle.
export const MAX_MANIFEST_BYTES = 1024 * 1024;
const MAX_FILES = 10000;
const MAX_LIST_PAGES = 20;
const DAY_MS = 86400000;
const SHA = /^[0-9a-f]{64}$/;
// One segment of a file path a bundle may list: nothing that needs URL-encoding, so a path
// is the same string in the manifest, the bucket key and the download URL.
const SEGMENT = /^[A-Za-z0-9_][A-Za-z0-9._-]*$/;

interface BundleRow {
  manifest_sha256: string; bundle: string; universe_sha256: string; index_path: string;
  index_sha256: string; manifest_bytes: number; bytes: number; files_json: string;
  restricted_json: string; state: string; registered_at: number;
}
interface FileEntry { sha256: string; bytes: number }
interface Manifest {
  bundle: string; universe_sha256: string; index_path: string; index_sha256: string;
  files: Record<string, FileEntry>; restricted: string[]; bytes: number;
}

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
async function hash(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, "0")).join("");
}

export function bundlePath(rel: unknown): rel is string {
  return typeof rel === "string" && rel.length <= 300 && rel !== MANIFEST_NAME &&
    rel.split("/").every(segment => SEGMENT.test(segment));
}

// The fields of a MANIFEST.json the Worker relies on, or why it cannot rely on them.
export function parseManifest(value: unknown): Manifest | string {
  if (!object(value) || value.schema !== BUNDLE_SCHEMA) return `MANIFEST.json is not schema ${BUNDLE_SCHEMA}`;
  const files = value.files;
  if (!object(files) || !Object.keys(files).length || Object.keys(files).length > MAX_FILES) {
    return `MANIFEST.json must list between 1 and ${MAX_FILES} files`;
  }
  const entries: Record<string, FileEntry> = {};
  let total = 0;
  for (const [rel, entry] of Object.entries(files)) {
    if (!bundlePath(rel) || !object(entry) || typeof entry.sha256 !== "string" || !SHA.test(entry.sha256) ||
        !Number.isSafeInteger(entry.bytes) || (entry.bytes as number) < 0) {
      return `MANIFEST.json lists a file it gives no plain path, sha256 and byte count: ${rel.slice(0, 120)}`;
    }
    entries[rel] = { sha256: entry.sha256, bytes: entry.bytes as number };
    total += entry.bytes as number;
  }
  if (value.bytes !== total) return "MANIFEST.json's bytes are not the sum of its files";
  const index = value.index, universe = value.universe, licences = value.licences;
  if (!object(index) || typeof index.path !== "string" || !Object.prototype.hasOwnProperty.call(entries, index.path) ||
      entries[index.path].sha256 !== index.sha256) {
    return "MANIFEST.json's index is not one of its files with that file's sha256";
  }
  if (!object(universe) || typeof universe.universe_sha256 !== "string" || !SHA.test(universe.universe_sha256)) {
    return "MANIFEST.json names no universe_sha256";
  }
  if (typeof value.bundle !== "string" || !value.bundle || value.bundle.length > 300) {
    return "MANIFEST.json names no bundle";
  }
  // upload_contrib.py refuses a bundle without its licences; the Worker checks the shape
  // it can see without reading a second object.
  const texts = object(licences) ? licences.texts : undefined;
  if (!object(licences) || !Array.isArray(licences.no_licence_recorded) || licences.no_licence_recorded.length ||
      !Array.isArray(licences.use_restricted) || !licences.use_restricted.every(id => typeof id === "string") ||
      !object(texts) || !Object.keys(texts).length ||
      !Object.values(texts).every(paths => Array.isArray(paths) && paths.length &&
        paths.every(rel => typeof rel === "string" && Object.prototype.hasOwnProperty.call(entries, rel)))) {
    return "MANIFEST.json does not carry every library's licence: package it with --licence-texts";
  }
  return { bundle: value.bundle, universe_sha256: universe.universe_sha256, index_path: index.path,
    index_sha256: String(index.sha256), files: entries, restricted: licences.use_restricted as string[], bytes: total };
}

// A single byte range of a file of `size` bytes, read as RFC 9110 reads one: null for
// the whole file (no Range, an If-Range that names another version, or a header this
// ignores: several ranges, or one it cannot parse), "unsatisfiable" for one past the end.
export function byteRange(header: string | undefined, ifRange: string | undefined, etag: string, size: number):
    { offset: number; length: number } | null | "unsatisfiable" {
  if (!header || (ifRange !== undefined && ifRange !== etag)) return null;
  const m = /^bytes=([0-9]*)-([0-9]*)$/.exec(header.trim());
  if (!m || (m[1] === "" && m[2] === "")) return null;
  if (m[1] === "") {
    const last = Number(m[2]);
    if (!Number.isSafeInteger(last)) return null;
    if (last === 0 || size === 0) return "unsatisfiable";
    const length = Math.min(last, size);
    return { offset: size - length, length };
  }
  const start = Number(m[1]);
  const end = m[2] === "" ? size - 1 : Number(m[2]);
  if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || (m[2] !== "" && end < start)) return null;
  if (start >= size) return "unsatisfiable";
  return { offset: start, length: Math.min(end, size - 1) - start + 1 };
}

function describe(row: BundleRow) {
  return {
    manifest_sha256: row.manifest_sha256, bundle: row.bundle, universe_sha256: row.universe_sha256,
    index_path: row.index_path, index_sha256: row.index_sha256, bytes: row.bytes,
    restricted: JSON.parse(row.restricted_json) as string[], registered_at: row.registered_at,
    manifest_url: `/api/index/bundles/${row.manifest_sha256}`,
  };
}

async function bundleRow(c: Ctx, sha: string): Promise<BundleRow | null> {
  const { results } = await c.env.DB.prepare("SELECT * FROM index_bundles WHERE manifest_sha256 = ?")
    .bind(sha).all<BundleRow>();
  return results[0] ?? null;
}

// Who may read: an owned agent token, under its owner's read budget.
async function agentRead(c: Ctx): Promise<(AuthUser & { ownerId: string }) | Response> {
  const user = await getUser(c);
  if (!user) return refuse(c, "authentication", "an agent token is required", 401);
  if (user.role !== "agent" || !user.ownerId) return refuse(c, "agent_required", "download with an owned agent token", 403);
  if (!(await (c.env.MCP_LIMITER ?? c.env.EDIT_LIMITER).limit({ key: `index-read:${user.ownerId}` })).success) {
    c.header("Retry-After", "60");
    return refuse(c, "rate_limit", "download request limit reached", 429);
  }
  return { ...user, ownerId: user.ownerId };
}

// The manifest (rel null) or one listed file of a live bundle.
async function serve(c: Ctx, sha: string, rel: string | null): Promise<Response> {
  const user = await agentRead(c); if (user instanceof Response) return user;
  const bucket = c.env.CONTRIB;
  if (!bucket) return refuse(c, "not_configured", "index bundles are not configured on this deployment", 503);
  if (!await leverOpen(c.env, INDEX_DOWNLOADS_LEVER)) {
    return refuse(c, "downloads_closed", "index downloads are closed; a maintainer opens them without a deploy", 503);
  }
  const row = await bundleRow(c, sha);
  if (!row || row.state !== "live") return refuse(c, "not_found", "no live bundle has that manifest sha256", 404);
  if (!agreementStands(await loadAgreement(c.env, user.ownerId))) {
    return refuse(c, "agreement_required", "the agent owner must accept the CC0 contribution agreement at /agents first", 403);
  }
  let entry: FileEntry;
  if (rel === null) {
    entry = { sha256: row.manifest_sha256, bytes: row.manifest_bytes };
  } else {
    const files = JSON.parse(row.files_json) as Record<string, FileEntry>;
    if (!bundlePath(rel) || !Object.prototype.hasOwnProperty.call(files, rel)) {
      return refuse(c, "not_found", "the bundle's manifest lists no such file", 404);
    }
    entry = files[rel];
  }
  const path = rel ?? MANIFEST_NAME;
  const etag = `"${entry.sha256}"`;
  const headers = new Headers({
    "Content-Type": rel === null ? "application/json" : "application/octet-stream",
    "ETag": etag, "Accept-Ranges": "bytes", "Cache-Control": "private, no-store",
  });
  if (rel !== null) headers.set("Content-Disposition", `attachment; filename="${path.split("/").pop()}"`);
  if (c.req.header("If-None-Match") === etag) return new Response(null, { status: 304, headers });
  const range = byteRange(c.req.header("Range"), c.req.header("If-Range"), etag, entry.bytes);
  if (range === "unsatisfiable") {
    c.header("Content-Range", `bytes */${entry.bytes}`);
    return refuse(c, "range", `the range is past the end of a ${entry.bytes}-byte file`, 416);
  }
  const offset = range?.offset ?? 0, length = range?.length ?? entry.bytes;
  headers.set("Content-Length", String(length));
  if (range) headers.set("Content-Range", `bytes ${offset}-${offset + length - 1}/${entry.bytes}`);
  const status = range ? 206 : 200;
  // A HEAD request reaches this handler too, and is answered from the manifest: no bucket
  // read, nothing logged.
  if (c.req.method === "HEAD") return new Response(null, { status, headers });

  const now = Date.now();
  const day = Math.floor(now / DAY_MS) * DAY_MS;
  // Everything the reads above saw, re-checked inside the write, with the day's quota.
  const result = await c.env.DB.prepare(
    "INSERT INTO index_downloads (owner_id,agent_id,bundle_sha256,path,range_start,bytes,at) " +
    "SELECT ?,?,?,?,?,?,? WHERE " +
    "EXISTS (SELECT 1 FROM agent_tokens t JOIN users a ON a.id=t.agent_id JOIN users o ON o.id=t.owner_id " +
    "JOIN user_agreements g ON g.user_id=o.id WHERE t.agent_id=? AND t.owner_id=? AND t.revoked_at IS NULL " +
    `AND a.role='agent' AND o.role!='blocked' AND g.cc0_version IN (${CC0_ACCEPTED_VERSIONS.map(() => "?").join(",")})) ` +
    "AND (SELECT value FROM site_settings WHERE key=?)='open' " +
    "AND EXISTS (SELECT 1 FROM index_bundles WHERE manifest_sha256=? AND state='live') " +
    "AND (SELECT COALESCE(SUM(bytes),0) FROM index_downloads WHERE owner_id=? AND at>=?) + ? <= ?",
  ).bind(user.ownerId, user.id, sha, path, offset, length, now,
    user.id, user.ownerId, ...CC0_ACCEPTED_VERSIONS, INDEX_DOWNLOADS_LEVER, sha,
    user.ownerId, day, length, INDEX_DAILY_BYTES).run();
  if (!result.meta.changes) {
    if (!await leverOpen(c.env, INDEX_DOWNLOADS_LEVER)) {
      return refuse(c, "downloads_closed", "index downloads are closed; a maintainer opens them without a deploy", 503);
    }
    if ((await bundleRow(c, sha))?.state !== "live") return refuse(c, "not_found", "no live bundle has that manifest sha256", 404);
    if (!(await getUser(c)) || !agreementStands(await loadAgreement(c.env, user.ownerId))) {
      return refuse(c, "access_changed", "contributor access changed during the request", 403);
    }
    c.header("Retry-After", String(Math.max(60, Math.ceil((day + DAY_MS - now) / 1000))));
    return refuse(c, "quota_bytes", `today's downloads reached ${INDEX_DAILY_BYTES} bytes for this account; ` +
      "resume after the indicated delay", 429);
  }
  const object = await bucket.get(`${INDEX_PREFIX}${sha}/${path}`, range ? { range: { offset, length } } : undefined);
  if (!object || !("body" in object)) {
    console.log(JSON.stringify({ event: "index-download-missing", bundle: sha, path }));
    return refuse(c, "bundle_incomplete", "the bucket does not hold that file; a maintainer must re-upload the bundle", 503);
  }
  console.log(JSON.stringify({ event: "index-download", agent_id: user.id, owner_id: user.ownerId,
    bundle: sha, path, start: offset, bytes: length }));
  return new Response(object.body, { status, headers });
}

// Every object under a prefix, with its size (paged: a bundle is tens of files).
async function listPrefix(bucket: R2Bucket, prefix: string): Promise<Map<string, number> | null> {
  const found = new Map<string, number>();
  let cursor: string | undefined;
  for (let page = 0; page < MAX_LIST_PAGES; page += 1) {
    const listing = await bucket.list({ prefix, cursor, limit: 1000 });
    for (const item of listing.objects) found.set(item.key, item.size);
    if (!listing.truncated) return found;
    cursor = listing.cursor;
  }
  return null;
}

export function registerBundleRoutes(app: Hono<{ Bindings: Env }>): void {
  // JSON answers are never cached; a download sets its own `private, no-store`.
  app.use("/api/index/*", async (c, next) => {
    await next();
    if (!c.res.headers.has("Cache-Control")) c.res.headers.set("Cache-Control", "no-store");
  });

  app.get("/api/index/bundles", async c => {
    const user = await agentRead(c); if (user instanceof Response) return user;
    const { results } = await c.env.DB.prepare(
      "SELECT * FROM index_bundles WHERE state='live' ORDER BY registered_at DESC, manifest_sha256 LIMIT 50",
    ).all<BundleRow>();
    return c.json({ ok: true, configured: !!c.env.CONTRIB, downloads_open: await leverOpen(c.env, INDEX_DOWNLOADS_LEVER),
      daily_bytes: INDEX_DAILY_BYTES, bundles: results.map(describe) });
  });

  app.get("/api/index/bundles/:sha{[0-9a-f]{64}}", c => serve(c, c.req.param("sha"), null));

  app.get("/api/index/bundles/:sha{[0-9a-f]{64}}/files/:path{.+}", c => {
    // The path as the client sent it, never decoded: a listed path needs no encoding, so
    // one that arrives encoded names nothing the manifest lists.
    const sha = c.req.param("sha");
    const marker = `/api/index/bundles/${sha}/files/`;
    const raw = new URL(c.req.url).pathname;
    const rel = raw.startsWith(marker) ? raw.slice(marker.length) : "";
    if (rel.includes("%")) return refuse(c, "not_found", "the bundle's manifest lists no such file", 404);
    return serve(c, sha, rel);
  });

  app.post("/api/index/bundles", async c => {
    const origin = checkOrigin(c); if (origin) return origin;
    const user = await requirePipeline(c, "coordinator_required", "coordinator"); if (user instanceof Response) return user;
    const brake = await writeBrake(c); if (brake) return brake;
    const read = await readJsonCapped(c, MAX_JSON_BODY_BYTES.indexBundle, "send {\"manifest_sha256\": <64 hex>}");
    if (!read.ok) return read.response;
    const sha = object(read.value) ? read.value.manifest_sha256 : undefined;
    if (typeof sha !== "string" || !SHA.test(sha)) return refuse(c, "invalid", "send {\"manifest_sha256\": <64 lowercase hex>}", 422);
    const bucket = c.env.CONTRIB;
    if (!bucket) return refuse(c, "not_configured", "index bundles are not configured on this deployment", 503);
    const prefix = `${INDEX_PREFIX}${sha}/`;
    const stored = await bucket.get(prefix + MANIFEST_NAME);
    if (!stored) return refuse(c, "bundle_incomplete", "the bucket holds no MANIFEST.json under that sha; upload the bundle first", 422);
    if (stored.size > MAX_MANIFEST_BYTES) {
      await stored.body.cancel();
      return refuse(c, "manifest_shape", `MANIFEST.json is over ${MAX_MANIFEST_BYTES} bytes`, 422);
    }
    const raw = new Uint8Array(await stored.arrayBuffer());
    if (await hash(raw) !== sha) return refuse(c, "manifest_digest", "MANIFEST.json does not hash to the sha it is stored under", 422);
    let value: unknown;
    try { value = JSON.parse(new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(raw)); }
    catch { return refuse(c, "manifest_shape", "MANIFEST.json is not UTF-8 JSON", 422); }
    const manifest = parseManifest(value);
    if (typeof manifest === "string") return refuse(c, "manifest_shape", manifest, 422);
    const listed = await listPrefix(bucket, prefix);
    if (!listed) return refuse(c, "bundle_incomplete", "the bucket's listing of the bundle did not end", 422);
    const short = Object.entries(manifest.files).filter(([rel, file]) => listed.get(prefix + rel) !== file.bytes)
      .map(([rel]) => rel);
    if (short.length) {
      return refuse(c, "bundle_incomplete", `the bucket lacks ${short.length} listed file(s) at their size, ` +
        `starting with ${short.slice(0, 3).join(", ")}`, 422);
    }
    const existing = await bundleRow(c, sha);
    if (existing) return c.json({ ok: true, bundle: describe(existing) }, 200);
    await c.env.DB.prepare(
      "INSERT INTO index_bundles (manifest_sha256,bundle,universe_sha256,index_path,index_sha256,manifest_bytes," +
      "bytes,files_json,restricted_json,state,registered_at) VALUES (?,?,?,?,?,?,?,?,?,'live',?) " +
      "ON CONFLICT(manifest_sha256) DO NOTHING",
    ).bind(sha, manifest.bundle, manifest.universe_sha256, manifest.index_path, manifest.index_sha256, raw.length,
      manifest.bytes, JSON.stringify(manifest.files), JSON.stringify(manifest.restricted), Date.now()).run();
    const row = (await bundleRow(c, sha))!;
    console.log(JSON.stringify({ event: "index-bundle-register", manifest_sha256: sha, bundle: row.bundle, bytes: row.bytes }));
    return c.json({ ok: true, bundle: describe(row) }, 201);
  });
}
