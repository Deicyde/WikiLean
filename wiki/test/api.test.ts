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
import { app } from "../src/index.js";
import type { Env } from "../src/env.js";
import { makeD1, makeKV } from "./helpers/d1shim.js";

const MIGRATIONS_DIR = resolve(process.cwd(), "migrations");
const MIGRATIONS = readdirSync(MIGRATIONS_DIR)
  .filter((f) => f.endsWith(".sql"))
  .sort();

const SLUG = "Test_Article";
const REVID = 12345;
const NEW_REVID = 67890; // a later upstream revision, pre-seeded in WP_HTML for re-pin tests
const PIPELINE_TOKEN = "test-pipeline-token";
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

// A pipeline-authored annotation (provenance must pass through verbatim on
// bearer writes). Anchors in WP_FIXTURE's Properties section.
const BOT_ANNOTATION = {
  status: "formalized",
  kind: "theorem",
  label: "Subgroups of abelian groups are normal",
  provenance: "ai",
  anchor: { section: "Properties", snippet: "Every subgroup of an abelian group is normal" },
  mathlib: { decl: "Subgroup.Normal", module: "Mathlib.GroupTheory.Subgroup.Basic", match_kind: "exact" },
};

// Deep clone via JSON round-trip: what a pipeline client would post after
// reading /api/article/:slug.json (same values, fresh object identity).
const echo = <T>(x: T): T => JSON.parse(JSON.stringify(x)) as T;

// C1: the save path now heals a 12-hex `id` onto every stored annotation, so
// fixtures posted without ids come back with one extra field. stripIds lets
// the pre-id fixtures above still be compared by content; the id contract
// itself is covered in ids.test.ts.
const ID_RE = /^[0-9a-f]{12}$/;
const stripIds = (arr: Array<Record<string, unknown>>) => arr.map(({ id: _id, ...rest }) => rest);

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
  insUser.run("pipeline", "WikiLean Pipeline", null, "bot", nowSec, nowSec);

  const env = {
    DB: makeD1(db),
    RENDER_CACHE: makeKV(),
    // Exact wikipedia.ts kvKey()s: the pinned revid + the re-pin target.
    WP_HTML: makeKV({ [`wp:${SLUG}:${REVID}`]: WP_FIXTURE, [`wp:${SLUG}:${NEW_REVID}`]: WP_FIXTURE }),
    ASSETS: { fetch: async () => new Response("not found", { status: 404 }) },
    EDIT_LIMITER: { limit: async () => ({ success: opts.limiterAllows ?? true }) },
    AUTH_MODE: "dev",
    PIPELINE_TOKEN,
  } as unknown as Env;
  return { db, env };
}

interface ReqOpts {
  user?: string; // users.id → wl_dev_user cookie
  bearer?: string; // Authorization: Bearer <token>
  origin?: string | null; // null = omit Origin header entirely
}

function reqHeaders(opts: ReqOpts, withBody: boolean): Record<string, string> {
  const headers: Record<string, string> = withBody ? { "Content-Type": "application/json" } : {};
  if (opts.user) headers["Cookie"] = `wl_dev_user=${opts.user}`;
  if (opts.bearer) headers["Authorization"] = `Bearer ${opts.bearer}`;
  return headers;
}

function post(env: Env, path: string, body: unknown, opts: ReqOpts = {}): Promise<Response> {
  const headers = reqHeaders(opts, true);
  if (opts.origin !== null) headers["Origin"] = opts.origin ?? ORIGIN;
  // app.request is typed Response | Promise<Response>; normalize.
  return Promise.resolve(app.request(path, { method: "POST", headers, body: JSON.stringify(body) }, env));
}

function get(env: Env, path: string, opts: ReqOpts = {}): Promise<Response> {
  return Promise.resolve(app.request(path, { headers: reqHeaders(opts, false) }, env));
}

function save(env: Env, body: Record<string, unknown>, opts: ReqOpts = {}): Promise<Response> {
  return post(env, `/api/article/${SLUG}`, body, opts);
}

// Bearer save, as the pipeline runner sends it: no Origin header (script
// client), no cookies.
function botSave(env: Env, body: Record<string, unknown>): Promise<Response> {
  return save(env, body, { bearer: PIPELINE_TOKEN, origin: null });
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
  return db
    .prepare(
      "SELECT id, user_id, annotations, comment, kind, meta, parent_id FROM revisions WHERE slug = ? ORDER BY id DESC LIMIT 1",
    )
    .get(SLUG) as {
    id: number;
    user_id: string | null;
    annotations: string;
    comment: string | null;
    kind: string;
    meta: string | null;
    parent_id: number | null;
  };
}

function moderationRow(db: DatabaseSync, slug = SLUG) {
  return db
    .prepare(
      "SELECT last_reviewed_at, last_reviewed_version, wp_drifted, flag_count, updated_at FROM moderation_state WHERE slug = ?",
    )
    .get(slug) as
    | {
        last_reviewed_at: number | null;
        last_reviewed_version: number | null;
        wp_drifted: number;
        flag_count: number;
        updated_at: number | null;
      }
    | undefined;
}

// Extra fixture rows for the /api/work priority tests.
function insertArticle(
  db: DatabaseSync,
  slug: string,
  opts: { version?: number; revid?: number | null; latestRevid?: number | null } = {},
): void {
  const now = Date.now();
  db.prepare(
    "INSERT INTO articles (slug, wikipedia_title, display_title, revid, latest_revid, annotations, version, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
  ).run(slug, slug, slug, opts.revid ?? null, opts.latestRevid ?? null, "[]", opts.version ?? 1, now, now);
}

function insertModState(
  db: DatabaseSync,
  slug: string,
  opts: { lastReviewedAt?: number | null; lastReviewedVersion?: number | null; wpDrifted?: number; flagCount?: number } = {},
): void {
  db.prepare(
    "INSERT INTO moderation_state (slug, last_reviewed_at, last_reviewed_version, wp_drifted, flag_count, updated_at) VALUES (?,?,?,?,?,?)",
  ).run(
    slug,
    opts.lastReviewedAt ?? null,
    opts.lastReviewedVersion ?? null,
    opts.wpDrifted ?? 0,
    opts.flagCount ?? 0,
    Date.now(),
  );
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

  it("happy path: bumps version, stores annotations (+healed ids), logs a revision", async () => {
    const { db, env } = setup();
    const res = await save(env, { annotations: HUMAN_EDIT, comment: "human pass", base_version: 1 }, { user: "u-human" });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true, matched: "2/2", version: 2 });

    const row = articleRow(db);
    expect(row.version).toBe(2);
    // C1 conscious update: posted without ids → stored = posted + healed ids,
    // otherwise verbatim.
    const storedArr = JSON.parse(row.annotations) as Array<Record<string, unknown>>;
    expect(stripIds(storedArr)).toEqual(HUMAN_EDIT);
    for (const a of storedArr) expect(a.id).toMatch(ID_RE);
    expect(row.revid).toBe(REVID);

    expect(revisionCount(db)).toBe(2);
    const rev = latestRevision(db);
    expect(rev.user_id).toBe("u-human");
    expect(rev.annotations).toBe(row.annotations); // snapshot = stored row
    expect(rev.comment).toBe("human pass");
    expect(rev.kind).toBe("edit");
    expect(rev.meta).toBeNull();
    expect(rev.parent_id).toBe(rev.id - 1); // based on the seed revision
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
    const body = (await second.json()) as {
      error: string;
      version: number;
      annotations: Array<Record<string, unknown>>;
    };
    expect(body.error).toBe("stale");
    expect(body.version).toBe(2);
    expect(stripIds(body.annotations)).toEqual(HUMAN_EDIT); // C1: stored carries healed ids
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

describe("pipeline bearer auth (getUser branch)", () => {
  it("a matching bearer token resolves to the pipeline user (role bot)", async () => {
    const { env } = setup();
    const res = await get(env, "/api/auth/me", { bearer: PIPELINE_TOKEN });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ user: { id: "pipeline", name: "WikiLean Pipeline", role: "bot" } });
  });

  it("a wrong bearer token is anonymous", async () => {
    const { env } = setup();
    const me = await get(env, "/api/auth/me", { bearer: "wrong-token" });
    expect(await me.json()).toEqual({ user: null });
    const res = await save(env, { annotations: [], base_version: 1 }, { bearer: "wrong-token", origin: null });
    expect(res.status).toBe(401);
  });

  it("bearer is disabled when PIPELINE_TOKEN is not configured", async () => {
    const { env } = setup();
    (env as { PIPELINE_TOKEN?: string }).PIPELINE_TOKEN = undefined;
    const res = await botSave(env, { annotations: [], base_version: 1 });
    expect(res.status).toBe(401);
  });

  it("a missing pipeline users row means unauthenticated", async () => {
    const { db, env } = setup();
    db.prepare("DELETE FROM users WHERE id = 'pipeline'").run();
    const res = await botSave(env, { annotations: [], base_version: 1 });
    expect(res.status).toBe(401);
  });

  it("a blocked pipeline row is anonymous (kill switch)", async () => {
    const { db, env } = setup();
    db.prepare("UPDATE users SET role = 'blocked' WHERE id = 'pipeline'").run();
    const res = await botSave(env, { annotations: [], base_version: 1 });
    expect(res.status).toBe(401);
  });
});

describe("POST /api/article/:slug (pipeline bearer writes)", () => {
  it("requires base_version (400), writes nothing", async () => {
    const { db, env } = setup();
    const res = await botSave(env, { annotations: echo(SEED_ANNOTATIONS) });
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ ok: false, error: "base_version required for pipeline writes" });
    expect(articleRow(db).version).toBe(1);
    expect(revisionCount(db)).toBe(1);
  });

  it("stale base_version → 409 with current state", async () => {
    const { db, env } = setup();
    const res = await botSave(env, { annotations: echo(SEED_ANNOTATIONS), base_version: 99 });
    expect(res.status).toBe(409);
    const body = (await res.json()) as { error: string; version: number };
    expect(body.error).toBe("stale");
    expect(body.version).toBe(1);
    expect(articleRow(db).version).toBe(1);
  });

  it("dropping a stored human annotation → 422 naming it, D1 unchanged", async () => {
    const { db, env } = setup();
    expect((await save(env, { annotations: HUMAN_EDIT, base_version: 1 }, { user: "u-human" })).status).toBe(200);
    // C1 conscious update: a pipeline echoes what it READ — the stored rows,
    // healed ids included — so the fixture echoes D1, not the pre-id constant.
    const before = articleRow(db);
    const storedArr = JSON.parse(before.annotations) as Array<Record<string, unknown>>;

    // The bot omits the human-added theorem annotation.
    const res = await botSave(env, { annotations: [echo(storedArr[0])], base_version: 2 });
    expect(res.status).toBe(422);
    expect(await res.json()).toEqual({
      ok: false,
      error: "human annotation lost",
      missing: ["Fundamental theorem of finite abelian groups"],
    });
    const row = articleRow(db);
    expect(row.version).toBe(2);
    expect(row.annotations).toBe(before.annotations);
    expect(revisionCount(db)).toBe(2);
    expect(moderationRow(db)).toBeUndefined();
  });

  it("altering a stored human annotation → 422 (deep-equality required)", async () => {
    const { db, env } = setup();
    expect((await save(env, { annotations: HUMAN_EDIT, base_version: 1 }, { user: "u-human" })).status).toBe(200);
    const before = articleRow(db).annotations;

    const tampered = JSON.parse(before) as Array<Record<string, unknown>>; // echo of stored, ids included (C1)
    tampered[0].note = "the bot reworded this human note";
    const res = await botSave(env, { annotations: tampered, base_version: 2 });
    expect(res.status).toBe(422);
    expect(((await res.json()) as { missing: string[] }).missing).toEqual(["Abelian group"]);
    expect(articleRow(db).annotations).toBe(before);
  });

  it("happy path: echoes humans verbatim → 200; kind=pipeline revision with meta + parent_id; moderation_state upserted", async () => {
    const { db, env } = setup();
    expect((await save(env, { annotations: HUMAN_EDIT, base_version: 1 }, { user: "u-human" })).status).toBe(200);
    const humanRevId = latestRevision(db).id;
    const storedHuman = JSON.parse(articleRow(db).annotations) as Array<Record<string, unknown>>; // C1: ids included

    const payload = [...echo(storedHuman), BOT_ANNOTATION];
    const meta = { run_id: "run-1", model: "test-model", tokens: 1234 };
    const res = await botSave(env, { annotations: payload, base_version: 2, comment: "moderation pass", meta });
    expect(res.status).toBe(200);
    expect(((await res.json()) as { version: number }).version).toBe(3);

    const row = articleRow(db);
    expect(row.version).toBe(3);
    // Bot provenance passes through; humans byte-identical (echoed ids pass
    // through untouched); the new bot annotation gets a healed id (C1).
    const arr = JSON.parse(row.annotations) as Array<Record<string, unknown>>;
    expect(arr.slice(0, 2)).toEqual(storedHuman);
    const { id: botId, ...botRest } = arr[2];
    expect(botRest).toEqual(BOT_ANNOTATION);
    expect(botId).toMatch(ID_RE);
    expect(row.revid).toBe(REVID); // no revid posted → pin unchanged

    const rev = latestRevision(db);
    expect(rev.user_id).toBe("pipeline");
    expect(rev.kind).toBe("pipeline");
    expect(rev.comment).toBe("moderation pass");
    expect(JSON.parse(rev.meta ?? "null")).toEqual(meta);
    expect(rev.parent_id).toBe(humanRevId);

    const mod = moderationRow(db);
    expect(mod).toBeDefined();
    expect(mod!.last_reviewed_version).toBe(3);
    expect(mod!.last_reviewed_at).toBeGreaterThan(0);
    expect(mod!.updated_at).toBeGreaterThan(0);
    expect(mod!.wp_drifted).toBe(0);
  });

  it("posted revid re-pins articles.revid atomically and clears wp_drifted", async () => {
    const { db, env } = setup();
    db.prepare("UPDATE articles SET latest_revid = ? WHERE slug = ?").run(NEW_REVID, SLUG);
    insertModState(db, SLUG, { wpDrifted: 1, lastReviewedAt: 1000, lastReviewedVersion: 1 });

    const res = await botSave(env, { annotations: echo(SEED_ANNOTATIONS), base_version: 1, revid: NEW_REVID });
    expect(res.status).toBe(200);

    const row = articleRow(db);
    expect(row.version).toBe(2);
    expect(row.revid).toBe(NEW_REVID);
    const mod = moderationRow(db)!;
    expect(mod.wp_drifted).toBe(0); // re-pinned to latest → drift cleared
    expect(mod.last_reviewed_version).toBe(2);
  });

  it("a bot save without revid leaves the pin and an existing drift flag alone", async () => {
    const { db, env } = setup();
    db.prepare("UPDATE articles SET latest_revid = ? WHERE slug = ?").run(NEW_REVID, SLUG);
    insertModState(db, SLUG, { wpDrifted: 1, lastReviewedAt: 1000, lastReviewedVersion: 1 });

    const res = await botSave(env, { annotations: echo(SEED_ANNOTATIONS), base_version: 1 });
    expect(res.status).toBe(200);

    const row = articleRow(db);
    expect(row.revid).toBe(REVID); // still pinned to the old revision
    const mod = moderationRow(db)!;
    expect(mod.wp_drifted).toBe(1); // still drifted — review happened, but no re-pin
    expect(mod.last_reviewed_version).toBe(2);
  });
});

describe("provenance stamping on session saves", () => {
  it("a changed annotation is stamped human server-side even if the client claims ai", async () => {
    const { db, env } = setup();
    const posted = [
      { ...SEED_ANNOTATIONS[0], note: "edited by a human, mislabeled by the client", provenance: "ai" },
      { ...HUMAN_EDIT[1], provenance: "ai" }, // brand-new annotation, also mislabeled
    ];
    const res = await save(env, { annotations: posted, base_version: 1 }, { user: "u-human" });
    expect(res.status).toBe(200);
    const stored = JSON.parse(articleRow(db).annotations) as Array<{ provenance?: string }>;
    expect(stored[0].provenance).toBe("human");
    expect(stored[1].provenance).toBe("human");
  });

  it("an unchanged annotation keeps its stored ai provenance even if the client claims human", async () => {
    const { db, env } = setup();
    const posted = [{ ...SEED_ANNOTATIONS[0], provenance: "human" }]; // bare provenance flip
    const res = await save(env, { annotations: posted, base_version: 1 }, { user: "u-human" });
    expect(res.status).toBe(200);
    const stored = JSON.parse(articleRow(db).annotations) as Array<{ provenance?: string }>;
    expect(stored[0].provenance).toBe("ai");
  });
});

describe("tombstones (status=rejected) are human vetoes", () => {
  it("a session-saved tombstone is stamped human and a bot write may not drop it", async () => {
    const { db, env } = setup();
    // Human rejects the seed annotation (editor delete → tombstone).
    const tombstone = { ...SEED_ANNOTATIONS[0], status: "rejected" };
    expect((await save(env, { annotations: [tombstone], base_version: 1 }, { user: "u-human" })).status).toBe(200);
    const stored = JSON.parse(articleRow(db).annotations) as Array<Record<string, unknown>>;
    expect(stored[0].status).toBe("rejected");
    expect(stored[0].provenance).toBe("human");

    // Bot "resurrects" the annotation by dropping the tombstone → 422.
    const res = await botSave(env, { annotations: [echo(SEED_ANNOTATIONS[0])], base_version: 2 });
    expect(res.status).toBe(422);
    expect(((await res.json()) as { missing: string[] }).missing).toEqual(["Abelian group"]);

    // Echoing the tombstone verbatim is fine.
    const ok = await botSave(env, { annotations: [echo(stored[0]), BOT_ANNOTATION], base_version: 2 });
    expect(ok.status).toBe(200);
  });
});

describe("GET /api/article/:slug.json (pipeline read path)", () => {
  it("returns the article JSON to anonymous readers", async () => {
    const { env } = setup();
    const res = await get(env, `/api/article/${SLUG}.json`);
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({
      slug: SLUG,
      wikipedia_title: "Test Article",
      display_title: "Test Article",
      version: 1,
      revid: REVID,
      latest_revid: null,
      schema_version: 3,
      annotations: SEED_ANNOTATIONS,
    });
  });

  it("reflects saves (version bump + latest annotations)", async () => {
    const { env } = setup();
    expect((await save(env, { annotations: HUMAN_EDIT, base_version: 1 }, { user: "u-human" })).status).toBe(200);
    const body = (await (await get(env, `/api/article/${SLUG}.json`)).json()) as {
      version: number;
      annotations: Array<Record<string, unknown>>;
    };
    expect(body.version).toBe(2);
    expect(stripIds(body.annotations)).toEqual(HUMAN_EDIT); // C1: stored carries healed ids
  });

  it("unknown slug → 404", async () => {
    const { env } = setup();
    const res = await get(env, "/api/article/No_Such_Article.json");
    expect(res.status).toBe(404);
  });
});

describe("GET /api/work (moderation queue)", () => {
  function seedWork(db: DatabaseSync) {
    // Base fixture article (never reviewed, no moderation row) plus one per
    // priority tier. Reviewed timestamps are deliberately out of priority
    // order so the test fails if ORDER BY falls back to recency or rowid.
    insertArticle(db, "Work_Flagged");
    insertModState(db, "Work_Flagged", { flagCount: 2, lastReviewedAt: 5000, lastReviewedVersion: 1 });
    insertArticle(db, "Work_Drifted");
    insertModState(db, "Work_Drifted", { wpDrifted: 1, lastReviewedAt: 6000, lastReviewedVersion: 1 });
    insertArticle(db, "Work_HumanEdited", { version: 3 });
    insertModState(db, "Work_HumanEdited", { lastReviewedAt: 7000, lastReviewedVersion: 2 });
    insertArticle(db, "Work_Stale", { revid: 100, latestRevid: 200 });
    insertModState(db, "Work_Stale", { lastReviewedAt: 1000, lastReviewedVersion: 1 });
    insertArticle(db, "Work_New");
  }

  it("requires the bot role (403 for sessions and anonymous)", async () => {
    const { env } = setup();
    expect((await get(env, "/api/work")).status).toBe(403);
    expect((await get(env, "/api/work", { user: "u-human" })).status).toBe(403);
    expect((await get(env, "/api/work", { user: "u-patroller" })).status).toBe(403);
  });

  it("review mode: flagged > drifted > human-edited > never-reviewed > oldest-reviewed, with reasons", async () => {
    const { db, env } = setup();
    seedWork(db);
    const res = await get(env, "/api/work", { bearer: PIPELINE_TOKEN });
    expect(res.status).toBe(200);
    const { jobs } = (await res.json()) as {
      jobs: Array<{ slug: string; reason: string; version: number; last_reviewed_version: number | null }>;
    };
    expect(jobs.length).toBe(6);
    expect(jobs[0]).toMatchObject({ slug: "Work_Flagged", reason: "flagged" });
    expect(jobs[1]).toMatchObject({ slug: "Work_Drifted", reason: "drifted" });
    expect(jobs[2]).toMatchObject({ slug: "Work_HumanEdited", reason: "human-edited", version: 3, last_reviewed_version: 2 });
    // Never-reviewed rows (NULL last_reviewed_at) sort before the oldest
    // reviewed one; their relative order is unspecified.
    expect(new Set([jobs[3].slug, jobs[4].slug])).toEqual(new Set([SLUG, "Work_New"]));
    expect(jobs[3].reason).toBe("never-reviewed");
    expect(jobs[4].reason).toBe("never-reviewed");
    expect(jobs[5]).toMatchObject({ slug: "Work_Stale", reason: "stale-review" });
  });

  it("respects limit (capped at 100)", async () => {
    const { db, env } = setup();
    seedWork(db);
    const res = await get(env, "/api/work?limit=2", { bearer: PIPELINE_TOKEN });
    const { jobs } = (await res.json()) as { jobs: Array<{ slug: string }> };
    expect(jobs.map((j) => j.slug)).toEqual(["Work_Flagged", "Work_Drifted"]);
  });

  it("wp-update mode filters to drifted or revid-trailing articles", async () => {
    const { db, env } = setup();
    seedWork(db);
    const res = await get(env, "/api/work?mode=wp-update", { bearer: PIPELINE_TOKEN });
    expect(res.status).toBe(200);
    const { jobs } = (await res.json()) as { jobs: Array<{ slug: string; reason: string }> };
    expect(jobs.map((j) => j.slug)).toEqual(["Work_Drifted", "Work_Stale"]);
    expect(jobs.map((j) => j.reason)).toEqual(["drifted", "drifted"]);
  });

  it("unknown mode → 400", async () => {
    const { env } = setup();
    expect((await get(env, "/api/work?mode=bogus", { bearer: PIPELINE_TOKEN })).status).toBe(400);
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
  // The roadmap's named test: a human save must survive — verbatim in D1,
  // visible on a direct read, and intact through a full pipeline write cycle
  // (the server-side _preserve_human twin).
  it("a human-provenance annotation survives save and is served on read", async () => {
    const { db, env } = setup();

    const res = await save(env, { annotations: HUMAN_EDIT, comment: "human edit", base_version: 1 }, { user: "u-human" });
    expect(res.status).toBe(200);

    // Stored verbatim in D1 (modulo healed ids, C1), human provenance intact.
    const stored = JSON.parse(articleRow(db).annotations) as Array<Record<string, unknown>>;
    expect(stripIds(stored)).toEqual(HUMAN_EDIT);
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

  it("a human annotation survives a full pipeline write cycle; a dropping bot write is rejected", async () => {
    const { db, env } = setup();

    // seed → human save (stamped provenance human).
    expect((await save(env, { annotations: HUMAN_EDIT, comment: "human edit", base_version: 1 }, { user: "u-human" })).status).toBe(200);

    // moderate: the pipeline reads back the article, echoes the human
    // annotations verbatim, adds its own work, and pushes.
    const read = (await (await get(env, `/api/article/${SLUG}.json`)).json()) as {
      version: number;
      annotations: Array<Record<string, unknown>>;
    };
    const pushed = await botSave(env, {
      annotations: [...read.annotations, BOT_ANNOTATION],
      base_version: read.version,
      comment: "moderation pass",
      meta: { run_id: "cycle-1" },
    });
    expect(pushed.status).toBe(200);

    // The human annotations are intact — deep-equal (modulo healed ids, C1),
    // provenance human.
    const stored = JSON.parse(articleRow(db).annotations) as Array<Record<string, unknown>>;
    expect(stripIds(stored.slice(0, 2))).toEqual(HUMAN_EDIT);
    expect(stored.filter((a) => a.provenance === "human").length).toBe(2);

    // A second bot pass that omits a human annotation is rejected and D1 is
    // untouched. (C1: the pass echoes stored rows — ids included — and drops
    // stored[1], the human theorem annotation.)
    const before = articleRow(db);
    const dropping = await botSave(env, {
      annotations: [echo(stored[0]), echo(stored[2])],
      base_version: before.version,
    });
    expect(dropping.status).toBe(422);
    expect(((await dropping.json()) as { missing: string[] }).missing).toEqual([
      "Fundamental theorem of finite abelian groups",
    ]);
    const after = articleRow(db);
    expect(after.version).toBe(before.version);
    expect(after.annotations).toBe(before.annotations);
  });
});
