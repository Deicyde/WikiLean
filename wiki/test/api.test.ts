// Integration tests for the Worker write path (P1 Wave A harness).
//
// Drives the real Hono app (src/index.ts) end-to-end via app.request() with
// shimmed bindings (test/helpers/d1shim.ts): node:sqlite behind a D1 shim,
// Map-backed KV namespaces, a controllable rate limiter, and dev-cookie auth.
// WP_HTML is pre-seeded with a fixture keyed exactly as wikipedia.ts kvKey()
// ('wp:<slug>:<revid>') so the save handler's re-render never touches the
// network — a throwing global fetch enforces that.
//
// These tests pin CURRENT behavior. The ~10 upcoming P1 changes to
// POST /api/article/:slug must keep them green (or change them consciously).

import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { describe, it, expect, beforeAll, afterAll } from "vitest";
import app from "../src/index.js";
import type { Env } from "../src/env.js";
import { makeD1, makeKV } from "./helpers/d1shim.js";

const MIGRATIONS_DIR = resolve(process.cwd(), "migrations");
const MIGRATIONS = readdirSync(MIGRATIONS_DIR)
  .filter((f) => f.endsWith(".sql"))
  .sort();

const SLUG = "Test_Article";
const REVID = 12345;
const ORIGIN = "http://localhost"; // app.request() URLs resolve against this

// Fake cached Wikipedia HTML. The lead paragraph's <b> anchors the seed
// annotation; the Properties section anchors the theorem via sentence match.
const WP_FIXTURE = [
  '<p>In mathematics, an <b>abelian group</b> is a <a href="/wiki/Group_(mathematics)">group</a> whose operation is commutative.</p>',
  "<h2>Properties</h2>",
  "<p>Every subgroup of an abelian group is normal. The fundamental theorem of finite abelian groups classifies them completely.</p>",
].join("\n");

const SEED_ANNOTATIONS = [
  {
    status: "formalized",
    kind: "definition",
    label: "Abelian group",
    provenance: "ai",
    anchor: { section: "(lead)", snippet: "abelian group" },
    mathlib: { decl: "AddCommGroup", module: "Mathlib.Algebra.Group.Defs", match_kind: "exact" },
  },
];

// A human edit: endorses the seed annotation (provenance flip + note) and adds
// a second one. Both anchors resolve in WP_FIXTURE → matched "2/2".
const HUMAN_EDIT = [
  { ...SEED_ANNOTATIONS[0], provenance: "human", note: "checked against Mathlib by a human" },
  {
    status: "partial",
    kind: "theorem",
    label: "Fundamental theorem of finite abelian groups",
    provenance: "human",
    anchor: { section: "Properties", snippet: "fundamental theorem of finite abelian groups" },
    mathlib: { decl: null, module: null, match_kind: null },
  },
];

// No test may hit the network: the only fetch sites are wikipedia.ts (defeated
// by the WP_HTML pre-seed) and better-auth (never reached in dev auth mode).
const realFetch = globalThis.fetch;
beforeAll(() => {
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    throw new Error(`unexpected network fetch in test: ${String(input)}`);
  }) as typeof fetch;
});
afterAll(() => {
  globalThis.fetch = realFetch;
});

function setup(opts: { limiterAllows?: boolean } = {}) {
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
  insUser.run("u-blocked", "Block Ed", "blocked@example.org", "blocked", nowSec, nowSec);

  const env = {
    DB: makeD1(db),
    RENDER_CACHE: makeKV(),
    WP_HTML: makeKV({ [`wp:${SLUG}:${REVID}`]: WP_FIXTURE }), // exact wikipedia.ts kvKey()
    ASSETS: { fetch: async () => new Response("not found", { status: 404 }) },
    EDIT_LIMITER: { limit: async () => ({ success: opts.limiterAllows ?? true }) },
    AUTH_MODE: "dev",
  } as unknown as Env;
  return { db, env };
}

interface ReqOpts {
  user?: string; // users.id → wl_dev_user cookie
  origin?: string | null; // null = omit Origin header entirely
}

function post(env: Env, path: string, body: unknown, opts: ReqOpts = {}): Promise<Response> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (opts.user) headers["Cookie"] = `wl_dev_user=${opts.user}`;
  if (opts.origin !== null) headers["Origin"] = opts.origin ?? ORIGIN;
  // app.request is typed Response | Promise<Response>; normalize.
  return Promise.resolve(app.request(path, { method: "POST", headers, body: JSON.stringify(body) }, env));
}

function save(env: Env, body: Record<string, unknown>, opts: ReqOpts = {}): Promise<Response> {
  return post(env, `/api/article/${SLUG}`, body, opts);
}

function articleRow(db: DatabaseSync) {
  return db.prepare("SELECT version, annotations, revid FROM articles WHERE slug = ?").get(SLUG) as {
    version: number;
    annotations: string;
    revid: number;
  };
}

function revisionCount(db: DatabaseSync): number {
  return (db.prepare("SELECT COUNT(*) AS n FROM revisions WHERE slug = ?").get(SLUG) as { n: number }).n;
}

function latestRevision(db: DatabaseSync) {
  return db.prepare("SELECT id, user_id, annotations, comment FROM revisions WHERE slug = ? ORDER BY id DESC LIMIT 1").get(SLUG) as {
    id: number;
    user_id: string | null;
    annotations: string;
    comment: string | null;
  };
}

describe("POST /api/article/:slug (save)", () => {
  it("rejects anonymous saves with 401 and writes nothing", async () => {
    const { db, env } = setup();
    const res = await save(env, { annotations: HUMAN_EDIT, base_version: 1 });
    expect(res.status).toBe(401);
    expect(await res.json()).toEqual({ ok: false, error: "login required" });
    expect(articleRow(db).version).toBe(1);
    expect(revisionCount(db)).toBe(1);
  });

  it("treats a blocked user as anonymous (401)", async () => {
    const { db, env } = setup();
    const res = await save(env, { annotations: HUMAN_EDIT, base_version: 1 }, { user: "u-blocked" });
    expect(res.status).toBe(401);
    expect(articleRow(db).version).toBe(1);
    expect(revisionCount(db)).toBe(1);
  });

  it("happy path: bumps version, stores annotations verbatim, logs a revision", async () => {
    const { db, env } = setup();
    const res = await save(env, { annotations: HUMAN_EDIT, comment: "human pass", base_version: 1 }, { user: "u-human" });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true, matched: "2/2", version: 2 });

    const row = articleRow(db);
    expect(row.version).toBe(2);
    expect(row.annotations).toBe(JSON.stringify(HUMAN_EDIT)); // verbatim
    expect(row.revid).toBe(REVID);

    expect(revisionCount(db)).toBe(2);
    const rev = latestRevision(db);
    expect(rev.user_id).toBe("u-human");
    expect(rev.annotations).toBe(JSON.stringify(HUMAN_EDIT));
    expect(rev.comment).toBe("human pass");
  });

  it("rate-limited save → 429, no write", async () => {
    const { db, env } = setup({ limiterAllows: false });
    const res = await save(env, { annotations: HUMAN_EDIT, base_version: 1 }, { user: "u-human" });
    expect(res.status).toBe(429);
    expect(articleRow(db).version).toBe(1);
    expect(revisionCount(db)).toBe(1);
  });

  it("unknown slug → 404", async () => {
    const { env } = setup();
    const res = await post(env, "/api/article/No_Such_Article", { annotations: [] }, { user: "u-human" });
    expect(res.status).toBe(404);
  });

  it("stale base_version → 409 with current {version, annotations}, no write", async () => {
    const { db, env } = setup();
    const res = await save(env, { annotations: HUMAN_EDIT, base_version: 99 }, { user: "u-human" });
    expect(res.status).toBe(409);
    const body = (await res.json()) as { error: string; version: number; annotations: unknown };
    expect(body.error).toBe("stale");
    expect(body.version).toBe(1);
    expect(body.annotations).toEqual(SEED_ANNOTATIONS);
    expect(articleRow(db).version).toBe(1);
    expect(revisionCount(db)).toBe(1);
  });

  it("current base_version succeeds; re-save against the old version → 409", async () => {
    const { db, env } = setup();
    const first = await save(env, { annotations: HUMAN_EDIT, base_version: 1 }, { user: "u-human" });
    expect(first.status).toBe(200);
    expect(((await first.json()) as { version: number }).version).toBe(2);

    const second = await save(env, { annotations: SEED_ANNOTATIONS, base_version: 1 }, { user: "u-human" });
    expect(second.status).toBe(409);
    const body = (await second.json()) as { error: string; version: number; annotations: unknown };
    expect(body.error).toBe("stale");
    expect(body.version).toBe(2);
    expect(body.annotations).toEqual(HUMAN_EDIT);
    expect(articleRow(db).version).toBe(2);
    expect(revisionCount(db)).toBe(2);
  });

  it("back-compat: absent base_version writes unconditionally", async () => {
    // Current behavior (pre-P1): clients that don't send base_version bypass
    // optimistic concurrency entirely. The P1 pipeline work is expected to
    // tighten this for bearer writes — update this test consciously then.
    const { db, env } = setup();
    const res = await save(env, { annotations: HUMAN_EDIT }, { user: "u-human" });
    expect(res.status).toBe(200);
    expect(articleRow(db).version).toBe(2);
  });

  it("validation: unknown status → 400", async () => {
    const { env } = setup();
    const res = await save(env, { annotations: [{ status: "bogus" }], base_version: 1 }, { user: "u-human" });
    expect(res.status).toBe(400);
    expect(((await res.json()) as { error: string }).error).toBe("invalid status");
  });

  it("validation: >2000 annotations → 413", async () => {
    const { env } = setup();
    const annotations = Array.from({ length: 2001 }, () => ({ status: "formalized" }));
    const res = await save(env, { annotations, base_version: 1 }, { user: "u-human" });
    expect(res.status).toBe(413);
  });

  it("validation: >256KB payload → 413", async () => {
    const { env } = setup();
    const annotations = [{ status: "formalized", note: "x".repeat(257 * 1024) }];
    const res = await save(env, { annotations, base_version: 1 }, { user: "u-human" });
    expect(res.status).toBe(413);
  });

  it("validation: free-text field over cap → 400", async () => {
    const { env } = setup();
    const annotations = [{ status: "formalized", note: "x".repeat(2001) }];
    const res = await save(env, { annotations, base_version: 1 }, { user: "u-human" });
    expect(res.status).toBe(400);
    expect(((await res.json()) as { error: string }).error).toBe("field note too long");
  });

  it("validation: non-object annotation → 400", async () => {
    const { env } = setup();
    const res = await save(env, { annotations: [42], base_version: 1 }, { user: "u-human" });
    expect(res.status).toBe(400);
    expect(((await res.json()) as { error: string }).error).toBe("annotation must be an object");
  });

  it("validation: missing annotations array → 400", async () => {
    const { env } = setup();
    const res = await save(env, { base_version: 1 }, { user: "u-human" });
    expect(res.status).toBe(400);
    expect(((await res.json()) as { error: string }).error).toBe("missing annotations");
  });

  it("cross-origin POST → 403, no write; same-origin passes", async () => {
    const { db, env } = setup();
    const evil = await save(
      env,
      { annotations: HUMAN_EDIT, base_version: 1 },
      { user: "u-human", origin: "https://evil.example" },
    );
    expect(evil.status).toBe(403);
    expect(articleRow(db).version).toBe(1);

    const ok = await save(env, { annotations: HUMAN_EDIT, base_version: 1 }, { user: "u-human", origin: ORIGIN });
    expect(ok.status).toBe(200);
  });
});

describe("POST /api/article/:slug/revert/:revid", () => {
  it("plain 'user' role → 403; patroller revert restores, bumps version, logs revision", async () => {
    const { db, env } = setup();
    const seedRevId = latestRevision(db).id;

    // Human save first so there is something to revert away from.
    const saved = await save(env, { annotations: HUMAN_EDIT, base_version: 1 }, { user: "u-human" });
    expect(saved.status).toBe(200);

    const denied = await post(env, `/api/article/${SLUG}/revert/${seedRevId}`, {}, { user: "u-human" });
    expect(denied.status).toBe(403);
    expect(articleRow(db).version).toBe(2);

    const reverted = await post(env, `/api/article/${SLUG}/revert/${seedRevId}`, {}, { user: "u-patroller" });
    expect(reverted.status).toBe(200);
    expect(await reverted.json()).toEqual({ ok: true, version: 3 });

    const row = articleRow(db);
    expect(row.version).toBe(3);
    expect(row.annotations).toBe(JSON.stringify(SEED_ANNOTATIONS)); // restored

    expect(revisionCount(db)).toBe(3);
    const rev = latestRevision(db);
    expect(rev.user_id).toBe("u-patroller");
    expect(rev.comment).toBe(`revert to #${seedRevId}`);
  });

  it("unknown revision id → 404; non-numeric → 400", async () => {
    const { env } = setup();
    expect((await post(env, `/api/article/${SLUG}/revert/99999`, {}, { user: "u-patroller" })).status).toBe(404);
    expect((await post(env, `/api/article/${SLUG}/revert/abc`, {}, { user: "u-patroller" })).status).toBe(400);
  });
});

describe("edit-safety invariant", () => {
  // The roadmap's named test, current-behavior version: a human save must
  // survive — verbatim in D1 and visible on a direct read.
  // TODO(Wave B): extend to 'survives a full pipeline write cycle' — seed →
  // human save → moderate (bearer write) → push → assert the
  // provenance:'human' annotation is intact (server-side _preserve_human twin).
  it("a human-provenance annotation survives save and is served on read", async () => {
    const { db, env } = setup();

    const res = await save(env, { annotations: HUMAN_EDIT, comment: "human edit", base_version: 1 }, { user: "u-human" });
    expect(res.status).toBe(200);

    // Stored verbatim in D1, human provenance intact.
    const stored = JSON.parse(articleRow(db).annotations) as Array<{ provenance?: string; note?: string }>;
    expect(stored).toEqual(HUMAN_EDIT);
    expect(stored.some((a) => a.provenance === "human")).toBe(true);

    // Direct read: the rendered page wraps the annotation as human-curated and
    // the logged-in editor payload carries the annotation verbatim.
    const page = await app.request(`/${SLUG}`, { headers: { Cookie: "wl_dev_user=u-human" } }, env);
    expect(page.status).toBe(200);
    const html = await page.text();
    expect(html).toContain('data-provenance="human"');
    expect(html).toContain("checked against Mathlib by a human");
    expect(html).toContain("window.__WL_FULL_ANNOS__");
  });
});
