// Framework-neutral test fixture for exercising the real Hono application.
// Vitest request helpers and the Playwright loopback server both build their
// environment from this module, so browser tests use the same D1/KV state as
// the Worker request suites.

import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { Env } from "../../src/env.js";
import { makeD1, makeKV, type KVShim } from "./d1shim.js";

export const SLUG = "Test_Article";
export const REVID = 12345;
export const NEW_REVID = 67890;
export const PIPELINE_TOKEN = "test-pipeline-token";
export const ORIGIN = "http://localhost";
export const ID_RE = /^[0-9a-f]{12}$/;
export const TEST_IP = "203.0.113.7";

const MIGRATIONS_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "../../migrations");
const MIGRATIONS = readdirSync(MIGRATIONS_DIR)
  .filter((file) => file.endsWith(".sql"))
  .sort();

export const WP_FIXTURE = [
  '<p>In mathematics, an <b>abelian group</b> is a <a href="/wiki/Group_(mathematics)">group</a> whose operation is commutative.</p>',
  "<h2>Properties</h2>",
  "<p>Every subgroup of an abelian group is normal. The fundamental theorem of finite abelian groups classifies them completely.</p>",
].join("\n");

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

export const EXTRA_ANNOTATION = {
  status: "formalized",
  kind: "theorem",
  label: "Subgroups of abelian groups are normal",
  provenance: "ai",
  anchor: { section: "Properties", snippet: "Every subgroup of an abelian group is normal" },
  mathlib: { decl: "Subgroup.Normal", module: "Mathlib.GroupTheory.Subgroup.Basic", match_kind: "exact" },
};

export const echo = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;

export interface Harness {
  db: DatabaseSync;
  env: Env;
  renderCache: KVShim;
  wpHtml: KVShim;
}

export function setup(
  opts: { limiterAllows?: boolean; flagLimiterAllows?: boolean; brainApiLimiterAllows?: boolean } = {},
): Harness {
  const db = new DatabaseSync(":memory:");
  for (const file of MIGRATIONS) db.exec(readFileSync(resolve(MIGRATIONS_DIR, file), "utf8"));

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
    BRAIN_API_LIMITER: { limit: async () => ({ success: opts.brainApiLimiterAllows ?? true }) },
    AUTH_MODE: "dev",
    PIPELINE_TOKEN,
  } as unknown as Env;
  return { db, env, renderCache, wpHtml };
}
