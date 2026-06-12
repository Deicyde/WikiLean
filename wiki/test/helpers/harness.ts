// Shared Wave-D test harness: the api.test.ts setup pattern (real Hono app
// over node:sqlite + KV shims, dev-cookie auth, glob-applied migrations)
// factored out so the create/endorse/flags/events/pages suites don't each
// re-declare it. api.test.ts keeps its own copy deliberately — it pins the
// pre-Wave-D contracts and must stay independently readable.

import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { beforeAll, afterAll } from "vitest";
import { app } from "../../src/index.js";
import type { Env } from "../../src/env.js";
import { makeD1, makeKV, type KVShim } from "./d1shim.js";

export const SLUG = "Test_Article";
export const REVID = 12345;
export const NEW_REVID = 67890;
export const PIPELINE_TOKEN = "test-pipeline-token";
export const ORIGIN = "http://localhost"; // app.request() URLs resolve against this
export const ID_RE = /^[0-9a-f]{12}$/;
export const TEST_IP = "203.0.113.7"; // RFC 5737 documentation range

const MIGRATIONS_DIR = resolve(process.cwd(), "migrations");
const MIGRATIONS = readdirSync(MIGRATIONS_DIR)
  .filter((f) => f.endsWith(".sql"))
  .sort();

export const WP_FIXTURE = [
  '<p>In mathematics, an <b>abelian group</b> is a <a href="/wiki/Group_(mathematics)">group</a> whose operation is commutative.</p>',
  "<h2>Properties</h2>",
  "<p>Every subgroup of an abelian group is normal. The fundamental theorem of finite abelian groups classifies them completely.</p>",
].join("\n");

// Unlike the api.test.ts fixtures, these carry explicit ids (the production
// state since the C1 backfill) so by-id event diffs are exercised directly —
// a modify reads as 'modify', not as a fresh-id 'add'.
export const SEED_ANNOTATIONS = [
  {
    id: "aaaaaaaaaaaa",
    status: "formalized",
    kind: "definition",
    label: "Abelian group",
    provenance: "ai",
    anchor: { section: "(lead)", snippet: "abelian group" },
    mathlib: { decl: "AddCommGroup", module: "Mathlib.Algebra.Group.Defs", match_kind: "exact" },
  },
  {
    id: "bbbbbbbbbbbb",
    status: "partial",
    kind: "theorem",
    label: "Fundamental theorem of finite abelian groups",
    provenance: "ai",
    anchor: { section: "Properties", snippet: "fundamental theorem of finite abelian groups" },
    mathlib: { decl: null, module: null, match_kind: null },
  },
];

// A third annotation a test can add (anchors in WP_FIXTURE's Properties section).
export const EXTRA_ANNOTATION = {
  status: "formalized",
  kind: "theorem",
  label: "Subgroups of abelian groups are normal",
  provenance: "ai",
  anchor: { section: "Properties", snippet: "Every subgroup of an abelian group is normal" },
  mathlib: { decl: "Subgroup.Normal", module: "Mathlib.GroupTheory.Subgroup.Basic", match_kind: "exact" },
};

// Deep clone via JSON round-trip: what a client posts after reading state.
export const echo = <T>(x: T): T => JSON.parse(JSON.stringify(x)) as T;

// No test may hit the network. Call once per test file at describe scope.
export function blockNetwork(): void {
  const realFetch = globalThis.fetch;
  beforeAll(() => {
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      throw new Error(`unexpected network fetch in test: ${String(input)}`);
    }) as typeof fetch;
  });
  afterAll(() => {
    globalThis.fetch = realFetch;
  });
}

export interface Harness {
  db: DatabaseSync;
  env: Env;
  renderCache: KVShim;
  wpHtml: KVShim;
}

export function setup(
  opts: { limiterAllows?: boolean; flagLimiterAllows?: boolean } = {},
): Harness {
  const db = new DatabaseSync(":memory:");
  for (const f of MIGRATIONS) db.exec(readFileSync(resolve(MIGRATIONS_DIR, f), "utf8"));

  const now = Date.now();
  db.prepare(
    "INSERT INTO articles (slug, wikipedia_title, display_title, wikidata_qid, revid, annotations, version, created_at, updated_at) VALUES (?,?,?,?,?,?,1,?,?)",
  ).run(SLUG, "Test Article", "Test Article", null, REVID, JSON.stringify(SEED_ANNOTATIONS), now, now);
  db.prepare("INSERT INTO revisions (slug, user_id, annotations, comment, created_at) VALUES (?,NULL,?,?,?)").run(
    SLUG,
    JSON.stringify(SEED_ANNOTATIONS),
    "seed import",
    now,
  );

  // users.created_at/updated_at are timestamp-mode (integer seconds).
  const nowSec = Math.floor(now / 1000);
  const insUser = db.prepare("INSERT INTO users (id, name, email, role, created_at, updated_at) VALUES (?,?,?,?,?,?)");
  insUser.run("u-human", "Human Tester", "human@example.org", "user", nowSec, nowSec);
  insUser.run("u-patroller", "Pat Roller", "pat@example.org", "patroller", nowSec, nowSec);
  insUser.run("u-admin", "Ad Min", "admin@example.org", "admin", nowSec, nowSec);
  insUser.run("u-blocked", "Block Ed", "blocked@example.org", "blocked", nowSec, nowSec);
  insUser.run("pipeline", "WikiLean Pipeline", null, "bot", nowSec, nowSec);

  const renderCache = makeKV();
  const wpHtml = makeKV({ [`wp:${SLUG}:${REVID}`]: WP_FIXTURE, [`wp:${SLUG}:${NEW_REVID}`]: WP_FIXTURE });
  const env = {
    DB: makeD1(db),
    RENDER_CACHE: renderCache,
    WP_HTML: wpHtml,
    ASSETS: { fetch: async () => new Response("not found", { status: 404 }) },
    EDIT_LIMITER: { limit: async () => ({ success: opts.limiterAllows ?? true }) },
    FLAG_LIMITER: { limit: async () => ({ success: opts.flagLimiterAllows ?? true }) },
    AUTH_MODE: "dev",
    PIPELINE_TOKEN,
  } as unknown as Env;
  return { db, env, renderCache, wpHtml };
}

export interface ReqOpts {
  user?: string; // users.id → wl_dev_user cookie
  bearer?: string; // Authorization: Bearer <token>
  origin?: string | null; // null = omit Origin header entirely
  ip?: string; // CF-Connecting-IP
}

function reqHeaders(opts: ReqOpts, withBody: boolean): Record<string, string> {
  const headers: Record<string, string> = withBody ? { "Content-Type": "application/json" } : {};
  if (opts.user) headers["Cookie"] = `wl_dev_user=${opts.user}`;
  if (opts.bearer) headers["Authorization"] = `Bearer ${opts.bearer}`;
  if (opts.ip) headers["CF-Connecting-IP"] = opts.ip;
  return headers;
}

function bodyRequest(
  env: Env,
  method: "POST" | "PUT",
  path: string,
  body: unknown,
  opts: ReqOpts,
): Promise<Response> {
  const headers = reqHeaders(opts, true);
  if (opts.origin !== null) headers["Origin"] = opts.origin ?? ORIGIN;
  return Promise.resolve(app.request(path, { method, headers, body: JSON.stringify(body) }, env));
}

export function post(env: Env, path: string, body: unknown, opts: ReqOpts = {}): Promise<Response> {
  return bodyRequest(env, "POST", path, body, opts);
}

export function put(env: Env, path: string, body: unknown, opts: ReqOpts = {}): Promise<Response> {
  return bodyRequest(env, "PUT", path, body, opts);
}

export function get(env: Env, path: string, opts: ReqOpts = {}): Promise<Response> {
  return Promise.resolve(app.request(path, { headers: reqHeaders(opts, false) }, env));
}

export function save(env: Env, body: Record<string, unknown>, opts: ReqOpts = {}): Promise<Response> {
  return post(env, `/api/article/${SLUG}`, body, opts);
}

// Bearer write, as the pipeline runner sends it: no Origin header, no cookies.
export function botSave(env: Env, body: Record<string, unknown>): Promise<Response> {
  return save(env, body, { bearer: PIPELINE_TOKEN, origin: null });
}

export function botCreate(env: Env, slug: string, body: Record<string, unknown>): Promise<Response> {
  return put(env, `/api/article/${slug}`, body, { bearer: PIPELINE_TOKEN, origin: null });
}

// ---- row accessors ----------------------------------------------------------

export interface ArticleRowLite {
  version: number;
  annotations: string;
  revid: number | null;
  wikipedia_title: string;
  display_title: string;
  wikidata_qid: string | null;
  schema_version: number;
  n_formalized: number | null;
  n_partial: number | null;
  n_not_formalized: number | null;
}

export function articleRow(db: DatabaseSync, slug = SLUG): ArticleRowLite | undefined {
  return db
    .prepare(
      "SELECT version, annotations, revid, wikipedia_title, display_title, wikidata_qid, schema_version, n_formalized, n_partial, n_not_formalized FROM articles WHERE slug = ?",
    )
    .get(slug) as ArticleRowLite | undefined;
}

export function storedAnnotations(db: DatabaseSync, slug = SLUG): Array<Record<string, unknown>> {
  return JSON.parse(articleRow(db, slug)!.annotations) as Array<Record<string, unknown>>;
}

export function revisionCount(db: DatabaseSync, slug = SLUG): number {
  return (db.prepare("SELECT COUNT(*) AS n FROM revisions WHERE slug = ?").get(slug) as { n: number }).n;
}

export interface RevisionRowLite {
  id: number;
  user_id: string | null;
  annotations: string;
  comment: string | null;
  kind: string;
  meta: string | null;
  parent_id: number | null;
}

export function latestRevision(db: DatabaseSync, slug = SLUG): RevisionRowLite {
  return db
    .prepare(
      "SELECT id, user_id, annotations, comment, kind, meta, parent_id FROM revisions WHERE slug = ? ORDER BY id DESC LIMIT 1",
    )
    .get(slug) as unknown as RevisionRowLite;
}

export interface ModerationRowLite {
  last_reviewed_at: number | null;
  last_reviewed_version: number | null;
  wp_drifted: number;
  flag_count: number;
  updated_at: number | null;
}

export function moderationRow(db: DatabaseSync, slug = SLUG): ModerationRowLite | undefined {
  return db
    .prepare(
      "SELECT last_reviewed_at, last_reviewed_version, wp_drifted, flag_count, updated_at FROM moderation_state WHERE slug = ?",
    )
    .get(slug) as ModerationRowLite | undefined;
}

export interface EventRow {
  id: number;
  revision_id: number;
  slug: string;
  annotation_id: string;
  event_type: string;
  actor_type: string;
  user_id: string | null;
  field_changes: string | null;
  created_at: number;
}

export function eventRows(db: DatabaseSync, slug = SLUG): EventRow[] {
  return db
    .prepare(
      "SELECT id, revision_id, slug, annotation_id, event_type, actor_type, user_id, field_changes, created_at FROM annotation_events WHERE slug = ? ORDER BY id",
    )
    .all(slug) as unknown as EventRow[];
}

export interface FlagRowLite {
  id: number;
  slug: string;
  annotation_id: string | null;
  reason: string;
  comment: string | null;
  user_id: string | null;
  ip_hash: string | null;
  status: string;
  resolved_by: string | null;
  resolved_at: number | null;
  created_at: number;
}

export function flagRows(db: DatabaseSync, slug = SLUG): FlagRowLite[] {
  return db
    .prepare(
      "SELECT id, slug, annotation_id, reason, comment, user_id, ip_hash, status, resolved_by, resolved_at, created_at FROM flags WHERE slug = ? ORDER BY id",
    )
    .all(slug) as unknown as FlagRowLite[];
}
