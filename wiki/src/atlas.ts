// The formalization atlas: a separate, read-only data plane that shows which
// WikiProject Mathematics passages are formalized in Lean, in which library, at
// which commit (design: docs/ATLAS.md). The store is a static tree that
// scripts/atlas/build_store.py builds from one sealed locate record set; the
// Worker reads it through the optional R2 binding ATLAS and never writes it.
//
// What this plane deliberately does not do: no D1 annotation writes, no item
// identity matching, no 256 KB annotation cap, no EDIT_LIMITER on reads, and no
// interaction with the nightly moderation pipeline. Its only D1 surface is the
// small atlas_feedback table (migration 0015), where signed-in people confirm or
// report single locations; the pipeline bearer exports those rows as benchmark
// seeds.
//
// Unbound ATLAS → every atlas page and API route answers 503 "atlas store not
// configured" (the feedback export, which reads only D1, keeps working).

import type { Context, Hono } from "hono";
import { getUser, requireRole } from "./auth.js";
import type { Env } from "./env.js";
import type { UniverseLibrary } from "./libraries.js";
import { checkOrigin, MAX_JSON_BODY_BYTES, readJsonCapped, researchExportSalt } from "./security.js";
import { MAX_FLAG_COMMENT_LEN } from "./validation.js";
import {
  atlasArticlePage,
  atlasDashboardPage,
  atlasMessagePage,
  atlasUniversePage,
  type DashboardRow,
} from "./atlas-render.js";

type Ctx = Context<{ Bindings: Env }>;

// ---- store contract (mirrors scripts/atlas/build_store.py) -----------------

export const ATLAS_PREFIX = "atlas/v1/";
export const ARTICLE_SCHEMA = "wikilean.atlas.article/1";
export const CURRENT_SCHEMA = "wikilean.atlas.current/1";
export const UNIVERSE_SCHEMA = "wikilean.atlas.universe/1";
export const INDEX_SCHEMA = "wikilean.atlas.index/1";
export const SUMMARY_STATUSES = ["formalized", "statement_only", "partial", "not_formalized", "unresolved"] as const;
export type SummaryStatus = (typeof SUMMARY_STATUSES)[number];
const MAX_INDEX_SHARDS = 256;

export interface AtlasLocation {
  library: string;
  repo: string;
  commit: string;
  declaration: string;
  module: string;
  // Relative to the repository root.
  path: string;
  line: number;
  // Optional: absent when the library index records only the first line (as
  // atlas's published index does); the page then says "line N".
  end_line?: number;
  relation: string;
  proof_status: string;
  // The library index's flag for a Prop-valued definition (proposal §2.4): it
  // states a theorem without proving it. Absent from stores built before it.
  statement?: boolean;
  rank: number;
  confidence: string;
  challenge: string;
  location_key: string;
  note: string | null;
}

export interface AtlasPassage {
  id: string;
  item: string;
  kind: string;
  // The passage's heading path ([] in the lead) and prominence
  // (subject | central | supporting): every record of the agreed format carries
  // both. Null only in a store built before they were required.
  section: string[] | null;
  prominence: string | null;
  quote: { exact: string; prefix?: string; suffix?: string };
  subject_uri: string;
  outcome: "located" | "absent" | "unresolved";
  status: string | null;
  // location_key of the location that justifies the status (build_store
  // derive_status), or null (also when the coverage claim justifies it);
  // absent from stores built before it existed.
  basis?: string | null;
  // Decision D1's coverage claim: hasPart locations (`parts`, location keys)
  // that together state every clause. It counts only once its challenge is
  // "survived". `status` is what the claim gives on its own (formalized or
  // statement_only once it survived, null otherwise) and `basis` says it, not one
  // location, justifies the passage's status. Null when the passage has none;
  // absent from stores built before D1.
  coverage?: AtlasCoverage | null;
  locations: AtlasLocation[];
  absence: {
    universe: string;
    universe_sha256: string;
    libraries: number;
    receipt: { sha256: string; queries: number; candidates: number };
    challenge: string;
  } | null;
  unresolved: { challenge: string } | null;
}

export interface AtlasCoverage {
  complete: boolean;
  parts: string[];
  challenge: string;
  status: string | null;
  basis: boolean;
}

export interface LibraryCounts {
  locations: number;
  // locations split by proof status: complete = proved or a definition,
  // incomplete = statement_only, wanted or axiom_dependent.
  complete?: number;
  incomplete?: number;
  passages: number;
  related: number;
}

export interface AtlasArticle {
  schema: string;
  pageid: number;
  title: string;
  revision: number;
  source_id: string;
  wikidata: string | null;
  assessment: { class?: string; importance?: string } | null;
  campaign: string;
  universe: { id: string; sha256: string; frozen: string; libraries: number };
  record_set: string;
  records_sha256: string;
  summary: {
    passages: number;
    // Locations exclude relatedMatch declarations, which are counted in `related`.
    locations: number;
    related: number;
    by_status: Record<SummaryStatus, number>;
    by_library: Record<string, LibraryCounts>;
    by_proof_status: Record<string, number>;
    by_relation: Record<string, number>;
  };
  passages: AtlasPassage[];
}

export interface AtlasUniverse {
  schema: string;
  id: string;
  sha256: string;
  frozen: string;
  // Sorted by id, as the record set's universe.json lists them.
  libraries: UniverseLibrary[];
  excluded: Array<{ id: string; reason: string }>;
}

export interface AtlasCurrent {
  schema: string;
  campaign: string;
  built_at: string;
  universe: { id: string; sha256: string; frozen: string; libraries: number };
  // set_id and byte_budget (0 = the export had none) come from SEAL.json.
  record_set: { seal_sha256: string; set_id?: string; created: string; skill_commit: string; byte_budget?: number };
  counts: {
    articles: number;
    passages: number;
    locations: number;
    related: number;
    by_status: Record<SummaryStatus, number>;
    by_library: Record<string, LibraryCounts & { articles: number }>;
    by_importance: Record<string, { articles: number; passages: number; by_status: Record<SummaryStatus, number> }>;
  };
  index: { hex: number; shards: string[]; entries: number };
}

export interface IndexEntry {
  title: string;
  pageid: number;
  revision: number;
  passages: number;
  formalized: number;
  statement_only: number;
  partial: number;
  not_formalized: number;
  unresolved: number;
  importance: string | null;
  // Ids of the libraries holding a location for the article, in universe order.
  libraries: string[];
}

// ---- the store reader ------------------------------------------------------

// One method, so R2, a static asset tree or an in-memory map can back it.
export interface AtlasReader {
  // `key` is relative to atlas/v1/; null = no such object.
  text(key: string): Promise<string | null>;
}

export function r2Reader(bucket: Pick<R2Bucket, "get">): AtlasReader {
  return {
    async text(key: string) {
      const object = await bucket.get(ATLAS_PREFIX + key);
      return object ? object.text() : null;
    },
  };
}

// Ready alternatives for the open backend decision (docs/ATLAS.md, "Store"):
// src/wikipedia.ts records that R2 was not enabled on the Cloudflare account.
// Neither is wired to a binding; adopting one is a one-line change in
// atlasReader plus its binding.
// KV: one value per store path, the same keys as the R2 objects.
export function kvReader(namespace: Pick<KVNamespace, "get">): AtlasReader {
  return { text: (key: string) => namespace.get(ATLAS_PREFIX + key) };
}

// Static assets: the tree copied under wiki/public/atlas-store/ and served by
// the ASSETS binding (release-coupled: it would ship only with a release).
export function staticReader(assets: Pick<Fetcher, "fetch">, origin = "https://assets.invalid"): AtlasReader {
  return {
    async text(key: string) {
      const res = await assets.fetch(new Request(`${origin}/atlas-store/${ATLAS_PREFIX}${key}`));
      return res.ok ? res.text() : null;
    },
  };
}

// Test and preview backend: keys are full store paths ("atlas/v1/...").
export function memoryReader(files: ReadonlyMap<string, string> | Record<string, string>): AtlasReader {
  const map = files instanceof Map ? files : new Map(Object.entries(files));
  return { text: async (key: string) => map.get(ATLAS_PREFIX + key) ?? null };
}

// One reader per bound bucket object, so the index-shard cache below survives
// across the requests an isolate serves with that binding. (Were the runtime to
// hand each request a fresh binding object, the cache would simply miss.)
const READERS = new WeakMap<object, AtlasReader>();

export function atlasReader(env: Env): AtlasReader | null {
  if (!env.ATLAS) return null;
  let reader = READERS.get(env.ATLAS);
  if (!reader) {
    reader = r2Reader(env.ATLAS);
    READERS.set(env.ATLAS, reader);
  }
  return reader;
}

class AtlasStoreError extends Error {}

async function readJson<T extends { schema: string }>(reader: AtlasReader, key: string, schema: string): Promise<T | null> {
  const text = await reader.text(key);
  if (text === null) return null;
  const value = JSON.parse(text) as T;
  if (!value || typeof value !== "object" || value.schema !== schema) {
    throw new AtlasStoreError(`atlas store object ${key} is not ${schema}`);
  }
  return value;
}

const PAGEID_RE = /^[1-9][0-9]{0,9}$/;
const SHA_RE = /^[0-9a-f]{64}$/;
const SHARD_RE = /^[0-9a-f]{1,2}$/;
const LIBRARY_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

export function parsePageid(raw: string | undefined): number | null {
  if (!raw || !PAGEID_RE.test(raw)) return null;
  const value = Number(raw);
  return value <= 2 ** 31 ? value : null;
}

export const loadCurrent = (reader: AtlasReader) => readJson<AtlasCurrent>(reader, "current.json", CURRENT_SCHEMA);
export const loadArticle = (reader: AtlasReader, pageid: number) =>
  readJson<AtlasArticle>(reader, `articles/${pageid}.json`, ARTICLE_SCHEMA);
export const loadUniverse = (reader: AtlasReader, sha: string) =>
  SHA_RE.test(sha) ? readJson<AtlasUniverse>(reader, `universes/${sha}.json`, UNIVERSE_SCHEMA) : Promise.resolve(null);

// Parsed index shards, per reader, for INDEX_CACHE_MS. The dashboard reads
// every shard and each article page one, so without this every anonymous view
// re-reads and re-parses them from R2 (review finding: about 18 Class-B reads
// and several MB of parsing per dashboard view). Only settled values are kept,
// never a pending promise, since a Worker must not await another request's
// I/O. A newly published store shows up within a minute, the staleness
// /api/atlas/current already allows (max-age=60).
export const INDEX_CACHE_MS = 60_000;
const SHARD_CACHE = new WeakMap<AtlasReader, Map<string, { at: number; entries: IndexEntry[] | null }>>();

export async function loadIndexShard(reader: AtlasReader, shard: string, now = Date.now()): Promise<IndexEntry[] | null> {
  if (!SHARD_RE.test(shard)) return null;
  // Created and registered before any await: concurrent shard loads of one
  // request must share one map, or the last to finish would drop the others.
  let cache = SHARD_CACHE.get(reader);
  if (!cache) {
    cache = new Map();
    SHARD_CACHE.set(reader, cache);
  }
  const hit = cache.get(shard);
  if (hit && now - hit.at >= 0 && now - hit.at < INDEX_CACHE_MS) return hit.entries;
  const entries = await parseIndexShard(reader, shard);
  cache.set(shard, { at: now, entries });
  return entries;
}

async function parseIndexShard(reader: AtlasReader, shard: string): Promise<IndexEntry[] | null> {
  const raw = await readJson<{ schema: string; fields: string[]; articles: unknown[][] }>(
    reader, `index/${shard}.json`, INDEX_SCHEMA,
  );
  if (!raw) return null;
  if (!Array.isArray(raw.fields) || !Array.isArray(raw.articles)) {
    throw new AtlasStoreError(`atlas index shard ${shard} lacks fields/articles`);
  }
  const at = (name: string) => raw.fields.indexOf(name);
  const pos = {
    title: at("title"), pageid: at("pageid"), revision: at("revision"), passages: at("passages"),
    formalized: at("formalized"), statement_only: at("statement_only"), partial: at("partial"),
    not_formalized: at("not_formalized"), unresolved: at("unresolved"), importance: at("importance"),
    libraries: at("libraries"),
  };
  if (pos.title < 0 || pos.pageid < 0) throw new AtlasStoreError(`atlas index shard ${shard} lacks title/pageid`);
  const num = (row: unknown[], i: number) => (i >= 0 && Number.isSafeInteger(row[i]) && (row[i] as number) >= 0 ? (row[i] as number) : 0);
  const libraries = (row: unknown[]): string[] => {
    const value = pos.libraries >= 0 ? row[pos.libraries] : null;
    return Array.isArray(value)
      ? value.filter((id): id is string => typeof id === "string" && LIBRARY_ID_RE.test(id)).slice(0, 64)
      : [];
  };
  return raw.articles.filter(Array.isArray).map((row) => ({
    title: String(row[pos.title]),
    pageid: num(row, pos.pageid),
    revision: num(row, pos.revision),
    passages: num(row, pos.passages),
    formalized: num(row, pos.formalized),
    statement_only: num(row, pos.statement_only),
    partial: num(row, pos.partial),
    not_formalized: num(row, pos.not_formalized),
    unresolved: num(row, pos.unresolved),
    importance: pos.importance >= 0 && typeof row[pos.importance] === "string" ? (row[pos.importance] as string) : null,
    libraries: libraries(row),
  }));
}

// Title comparison form, byte-identical to build_store.title_key: NFC,
// underscores and the listed spaces collapsed to one space, trimmed.
const TITLE_SPACE = /[ \t\n\r\f\v\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000_]+/g;
export function titleKey(title: string): string {
  return title.normalize("NFC").replace(TITLE_SPACE, " ").replace(/^ +| +$/g, "");
}

async function sha256Hex(s: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

// v1 stores shard on the first hex digit (build_store.INDEX_HEX).
export const INDEX_HEX = 1;
export async function titleShard(title: string, hex = INDEX_HEX): Promise<string> {
  return (await sha256Hex(titleKey(title))).slice(0, hex);
}

// One index-shard read: the atlas entry for a Wikipedia title, or null.
export async function lookupTitle(reader: AtlasReader, title: string): Promise<IndexEntry | null> {
  const key = titleKey(title);
  if (!key) return null;
  const entries = await loadIndexShard(reader, await titleShard(key));
  return entries?.find((entry) => titleKey(entry.title) === key) ?? null;
}

// URL slug for /atlas/:pageid/:slug (readability only; the page id is the key).
export function atlasSlug(title: string): string {
  return encodeURIComponent(titleKey(title).replaceAll(" ", "_"));
}

export function atlasHref(pageid: number, title: string): string {
  return `/atlas/${pageid}/${atlasSlug(title)}`;
}

// The article page's compact summary box (pages.ts injectAtlasSummary). Never
// throws: an unbound, empty or broken store simply means no box.
export async function atlasSummaryFor(env: Env, wikipediaTitle: string): Promise<IndexEntry | null> {
  const reader = atlasReader(env);
  if (!reader) return null;
  try {
    return await lookupTitle(reader, wikipediaTitle);
  } catch (error) {
    console.log(JSON.stringify({ event: "atlas-box-error", msg: String(error) }));
    return null;
  }
}

// ---- responses -------------------------------------------------------------

export const NOT_CONFIGURED = "atlas store not configured";
const JSON_HEADERS = { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "public, max-age=300" };

function notConfiguredJson(c: Ctx, error = NOT_CONFIGURED): Response {
  return c.json({ ok: false, error }, 503, { "Cache-Control": "no-store" });
}

function notConfiguredHtml(c: Ctx, error = NOT_CONFIGURED): Response {
  return c.html(atlasMessagePage("Atlas unavailable", `The formalization atlas is unavailable: ${error}.`,
    "This deployment has no atlas store bound (docs/ATLAS.md, “Store”)."), 503, { "Cache-Control": "no-store" });
}

// ---- feedback --------------------------------------------------------------

const FEEDBACK_VERDICTS = new Set(["confirm", "report"]);
export const ATLAS_PSEUDONYM_DOMAIN = "atlas-feedback\0";
const MAX_NOTE_CHARS = MAX_FLAG_COMMENT_LEN; // 500, the flag form's cap
// SEAL.json's campaign, as build_store.CAMPAIGN_RE: a campaign id, or for a run
// outside any campaign "run:<run-id>[+<challenge-run-id>]".
export const CAMPAIGN_RE = /^(?:run:[A-Za-z0-9][A-Za-z0-9._-]{0,63}(?:\+[A-Za-z0-9][A-Za-z0-9._-]{0,63})?|(?!run:)[A-Za-z0-9][A-Za-z0-9._:-]{0,127})$/;

function missingTable(error: unknown): boolean {
  let current: unknown = error;
  for (let depth = 0; depth < 5 && current; depth += 1) {
    const message = current instanceof Error ? current.message : String(current);
    if (message.includes("no such table: atlas_feedback")) return true;
    current = typeof current === "object" && current !== null && "cause" in current ? (current as { cause?: unknown }).cause : null;
  }
  return false;
}

// The viewer's own verdicts on one article (location_key → verdict). Empty
// when the table is missing (migration 0015 not yet applied).
async function viewerVerdicts(env: Env, userId: string, pageid: number): Promise<Map<string, string>> {
  try {
    const { results } = await env.DB.prepare(
      "SELECT location_key, verdict FROM atlas_feedback WHERE user_id = ? AND pageid = ?",
    ).bind(userId, pageid).all<{ location_key: string; verdict: string }>();
    return new Map(results.map((row) => [row.location_key, row.verdict]));
  } catch (error) {
    if (!missingTable(error)) throw error;
    return new Map();
  }
}

async function wikileanSlugFor(env: Env, title: string): Promise<string | null> {
  const key = titleKey(title);
  const { results } = await env.DB.prepare(
    "SELECT slug FROM articles WHERE wikipedia_title IN (?, ?) ORDER BY slug LIMIT 1",
  ).bind(key, key.replaceAll(" ", "_")).all<{ slug: string }>();
  return results[0]?.slug ?? null;
}

function foldForSearch(s: string): string {
  return s.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
}

// Sort and search keys, computed once per cached index entry.
const ENTRY_KEYS = new WeakMap<IndexEntry, { key: string; folded: string }>();
function entryKeys(entry: IndexEntry): { key: string; folded: string } {
  let keys = ENTRY_KEYS.get(entry);
  if (!keys) {
    keys = { key: titleKey(entry.title), folded: foldForSearch(entry.title) };
    ENTRY_KEYS.set(entry, keys);
  }
  return keys;
}

const MAX_DASHBOARD_ROWS = 200;

// A ?q= search scans every index entry, so it takes the anonymous read budget
// /api/brain/* and /mcp use: per IP, MCP_LIMITER when bound, else
// BRAIN_API_LIMITER (120/min), under its own key prefix.
async function searchAllowed(c: Ctx): Promise<boolean> {
  const limiter = c.env.MCP_LIMITER ?? c.env.BRAIN_API_LIMITER;
  const ip = c.req.header("CF-Connecting-IP") || "unknown";
  const { success } = await limiter.limit({ key: `atlas-q:${ip}` });
  return success;
}

// ---- routes ----------------------------------------------------------------

export function registerAtlasRoutes(app: Hono<{ Bindings: Env }>): void {
  // Store objects keep their builder's bytes; the API serves them as stored.
  const serveText = async (c: Ctx, key: string, contentType: string, cache: string, missing: string) => {
    const reader = atlasReader(c.env);
    if (!reader) return notConfiguredJson(c);
    const text = await reader.text(key);
    if (text === null) return c.json({ ok: false, error: missing }, 404);
    return c.body(text, 200, { "Content-Type": contentType, "Cache-Control": cache });
  };

  app.get("/api/atlas/current", (c) =>
    serveText(c, "current.json", JSON_HEADERS["Content-Type"], "public, max-age=60", "atlas store has no published build"));

  app.get("/api/atlas/article/:pageid", (c) => {
    const pageid = parsePageid(c.req.param("pageid"));
    if (!atlasReader(c.env)) return notConfiguredJson(c);
    if (pageid === null) return c.json({ ok: false, error: "unknown pageid" }, 404);
    return serveText(c, `articles/${pageid}.json`, JSON_HEADERS["Content-Type"], JSON_HEADERS["Cache-Control"], "unknown pageid");
  });

  app.get("/api/atlas/records/:pageid", (c) => {
    const pageid = parsePageid(c.req.param("pageid"));
    if (!atlasReader(c.env)) return notConfiguredJson(c);
    if (pageid === null) return c.json({ ok: false, error: "unknown pageid" }, 404);
    return serveText(c, `records/${pageid}.jsonl`, "application/x-ndjson; charset=utf-8", JSON_HEADERS["Cache-Control"], "unknown pageid");
  });

  // Not immutable: the key is the producer's universe_sha256, which covers the
  // libraries' ids, commits and index shas but not the metadata stored beside
  // them (display_name, roots, toolchain, licence, exclusions), so a later
  // campaign on the same frozen universe may rewrite the object.
  app.get("/api/atlas/universe/:sha", (c) => {
    const sha = c.req.param("sha");
    if (!atlasReader(c.env)) return notConfiguredJson(c);
    if (!SHA_RE.test(sha)) return c.json({ ok: false, error: "unknown universe" }, 404);
    return serveText(c, `universes/${sha}.json`, JSON_HEADERS["Content-Type"], JSON_HEADERS["Cache-Control"], "unknown universe");
  });

  app.get("/api/atlas/index/:shard", (c) => {
    const shard = c.req.param("shard");
    if (!atlasReader(c.env)) return notConfiguredJson(c);
    if (!SHARD_RE.test(shard)) return c.json({ ok: false, error: "unknown shard" }, 404);
    return serveText(c, `index/${shard}.json`, JSON_HEADERS["Content-Type"], JSON_HEADERS["Cache-Control"], "unknown shard");
  });

  // ---- pages ----

  app.get("/atlas", async (c) => {
    const reader = atlasReader(c.env);
    if (!reader) return notConfiguredHtml(c);
    const current = await loadCurrent(reader);
    if (!current) return notConfiguredHtml(c, "atlas store has no published build");
    const q = (c.req.query("q") ?? "").trim().slice(0, 200);
    if (q && !(await searchAllowed(c))) {
      return c.html(atlasMessagePage("Too many searches", "Too many atlas searches from this address in a minute.",
        "Try again shortly, or browse the list without a search."), 429, { "Retry-After": "60" });
    }
    const universe = await loadUniverse(reader, current.universe.sha256);
    const shards = (Array.isArray(current.index?.shards) ? current.index.shards : [])
      .filter((s) => typeof s === "string" && SHARD_RE.test(s)).slice(0, MAX_INDEX_SHARDS);
    const loaded = await Promise.all(shards.map((s) => loadIndexShard(reader, s)));
    let rows: IndexEntry[] = loaded.flatMap((entries) => entries ?? []);
    const total = rows.length;
    if (q) {
      const needle = foldForSearch(q);
      rows = rows.filter((row) => entryKeys(row).folded.includes(needle));
    }
    const keyed = rows.map((row) => ({ row, key: entryKeys(row).key }));
    keyed.sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : a.row.pageid - b.row.pageid));
    const shown: DashboardRow[] = keyed.slice(0, MAX_DASHBOARD_ROWS)
      .map(({ row }) => ({ ...row, href: atlasHref(row.pageid, row.title) }));
    return c.html(atlasDashboardPage({ current, universe, q, rows: shown, matched: keyed.length, total, shards }));
  });

  app.get("/atlas/universe/:sha", async (c) => {
    const reader = atlasReader(c.env);
    if (!reader) return notConfiguredHtml(c);
    const universe = await loadUniverse(reader, c.req.param("sha"));
    if (!universe) {
      return c.html(atlasMessagePage("Unknown universe", "No atlas universe has that sha256.", ""), 404);
    }
    return c.html(atlasUniversePage(universe));
  });

  const articlePage = async (c: Ctx) => {
    const reader = atlasReader(c.env);
    if (!reader) return notConfiguredHtml(c);
    const pageid = parsePageid(c.req.param("pageid"));
    const article = pageid === null ? null : await loadArticle(reader, pageid);
    if (!article) {
      return c.html(atlasMessagePage("No atlas entry",
        `The atlas has no entry for Wikipedia page id ${pageid === null ? "(invalid)" : pageid}.`,
        "Only articles the locate pipeline has processed appear here."), 404);
    }
    const universe = await loadUniverse(reader, article.universe.sha256);
    const viewer = await getUser(c);
    const user = viewer && viewer.role !== "bot" ? viewer : null;
    const verdicts = user ? await viewerVerdicts(c.env, user.id, article.pageid) : new Map<string, string>();
    const wikileanSlug = await wikileanSlugFor(c.env, article.title);
    return c.html(atlasArticlePage({
      article, universe, user, verdicts, wikileanSlug, selfHref: atlasHref(article.pageid, article.title),
    }));
  };
  app.get("/atlas/:pageid{[0-9]+}", articlePage);
  app.get("/atlas/:pageid{[0-9]+}/:slug", articlePage);

  // ---- feedback ----

  // Confirm or report one location. Session users only (the pipeline bearer is
  // refused: these rows are human labels), same-origin (checkOrigin), rate
  // limited per user, byte-capped after those checks (readJsonCapped,
  // MAX_JSON_BODY_BYTES.atlasFeedback: 413 past it), and checked against the
  // current store so a key that is not on the page cannot be stored. One row
  // per (location_key, user): a second verdict replaces the first.
  app.post("/api/atlas/feedback", async (c) => {
    const reader = atlasReader(c.env);
    if (!reader) return notConfiguredJson(c);
    const originErr = checkOrigin(c);
    if (originErr) return originErr;
    const user = await getUser(c);
    if (!user) return c.json({ ok: false, error: "login required" }, 401);
    if (user.role === "bot") return c.json({ ok: false, error: "session required (no bots)" }, 403);
    const { success } = await c.env.EDIT_LIMITER.limit({ key: `atlas:${user.id}` });
    if (!success) return c.json({ ok: false, error: "rate limited — slow down" }, 429);

    const read = await readJsonCapped(c, MAX_JSON_BODY_BYTES.atlasFeedback);
    if (!read.ok) return read.response;
    const parsed = read.value;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return c.json({ ok: false, error: "bad json" }, 400);
    const body = parsed as Record<string, unknown>;
    const { location_key: key, pageid, campaign, verdict } = body;
    if (typeof key !== "string" || !SHA_RE.test(key)) return c.json({ ok: false, error: "bad location_key" }, 400);
    if (typeof pageid !== "number" || !Number.isSafeInteger(pageid) || pageid < 1) {
      return c.json({ ok: false, error: "bad pageid" }, 400);
    }
    if (typeof campaign !== "string" || !CAMPAIGN_RE.test(campaign)) return c.json({ ok: false, error: "bad campaign" }, 400);
    if (typeof verdict !== "string" || !FEEDBACK_VERDICTS.has(verdict)) return c.json({ ok: false, error: "bad verdict" }, 400);
    let note: string | null = null;
    if (body.note !== undefined && body.note !== null) {
      if (typeof body.note !== "string" || body.note.length > MAX_NOTE_CHARS) {
        return c.json({ ok: false, error: `note must be a string of at most ${MAX_NOTE_CHARS} characters` }, 400);
      }
      note = body.note.trim() || null;
    }

    const article = await loadArticle(reader, pageid);
    if (!article) return c.json({ ok: false, error: "unknown pageid" }, 404);
    if (article.campaign !== campaign) {
      return c.json({ ok: false, error: "stale: the atlas changed since this page loaded", campaign: article.campaign }, 409);
    }
    const known = article.passages.some((p) => p.locations.some((loc) => loc.location_key === key));
    if (!known) return c.json({ ok: false, error: "unknown location_key" }, 404);

    const now = Date.now();
    try {
      await c.env.DB.prepare(
        `INSERT INTO atlas_feedback (location_key, pageid, revision, campaign, verdict, note, user_id, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT (location_key, user_id) DO UPDATE SET
           pageid = excluded.pageid, revision = excluded.revision, campaign = excluded.campaign,
           verdict = excluded.verdict, note = excluded.note, updated_at = excluded.updated_at`,
      ).bind(key, pageid, article.revision, article.campaign, verdict, note, user.id, now, now).run();
    } catch (error) {
      if (missingTable(error)) return notConfiguredJson(c, "atlas feedback not configured (migration 0015)");
      throw error;
    }
    console.log(JSON.stringify({ event: "atlas-feedback", pageid, location_key: key, verdict, user_id: user.id, t: now }));
    return c.json({ ok: true, verdict });
  });

  // Benchmark seeding (docs/ATLAS.md, "Feedback"): every row, id-ascending,
  // paged from D1 so the response never materializes in memory. Pipeline
  // bearer only. Users appear only as pseudonyms, sha256("atlas-feedback\0" +
  // user_id + salt) truncated to 12 hex, with the research export's salt
  // (security.ts researchExportSalt: RESEARCH_EXPORT_SALT, else PIPELINE_TOKEN).
  // The domain prefix keeps them apart from the research export's
  // sha256(user_id + salt): this export carries free-text notes, so a note
  // that names its author must not re-identify that person's pseudonymous
  // annotation history there. Reads only D1, so it works whether or not ATLAS
  // is bound.
  app.get("/api/atlas/feedback/export", async (c) => {
    const user = await requireRole(c, ["bot"]);
    if (!user) return c.json({ ok: false, error: "forbidden" }, 403);
    const salt = researchExportSalt(c.env);
    const pseudonyms = new Map<string, string>();
    const pseudonymFor = async (userId: string) => {
      let value = pseudonyms.get(userId);
      if (value === undefined) {
        value = (await sha256Hex(ATLAS_PSEUDONYM_DOMAIN + userId + salt)).slice(0, 12);
        pseudonyms.set(userId, value);
      }
      return value;
    };
    try {
      await c.env.DB.prepare("SELECT 1 FROM atlas_feedback LIMIT 1").all();
    } catch (error) {
      if (missingTable(error)) return notConfiguredJson(c, "atlas feedback not configured (migration 0015)");
      throw error;
    }
    const PAGE = 500;
    let cursor = 0;
    const encoder = new TextEncoder();
    const env = c.env;
    const body = new ReadableStream<Uint8Array>({
      async pull(controller) {
        const { results } = await env.DB.prepare(
          `SELECT id, location_key, pageid, revision, campaign, verdict, note, user_id, created_at, updated_at
           FROM atlas_feedback WHERE id > ? ORDER BY id LIMIT ?`,
        ).bind(cursor, PAGE).all<{
          id: number; location_key: string; pageid: number; revision: number; campaign: string;
          verdict: string; note: string | null; user_id: string; created_at: number; updated_at: number;
        }>();
        if (results.length === 0) {
          controller.close();
          return;
        }
        cursor = results[results.length - 1].id;
        let out = "";
        for (const row of results) {
          out += JSON.stringify({
            feedback_id: row.id,
            location_key: row.location_key,
            pageid: row.pageid,
            revision: row.revision,
            campaign: row.campaign,
            verdict: row.verdict,
            note: row.note,
            pseudonym: await pseudonymFor(row.user_id),
            created_at: row.created_at,
            updated_at: row.updated_at,
          }) + "\n";
        }
        controller.enqueue(encoder.encode(out));
      },
    });
    return new Response(body, {
      headers: { "Content-Type": "application/x-ndjson; charset=utf-8", "Cache-Control": "no-store" },
    });
  });
}
