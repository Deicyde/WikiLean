// The full authorization matrix — every actor × every endpoint, exact status
// per cell. This is the announcement-readiness artifact: the table below IS
// the access-control policy, pinned. Each cell runs against a fresh harness
// (one assertion per cell), so a wrong status anywhere names the exact
// actor/endpoint pair that regressed.
//
// Actor semantics under test (auth.ts getUser/requireRole):
//   anon        no cookie, no bearer
//   user        session, role 'user'
//   patroller   session, role 'patroller'
//   admin       session, role 'admin'
//   blocked     session, role 'blocked' → treated as anonymous everywhere
//   bot         Authorization: Bearer <PIPELINE_TOKEN> → users row 'pipeline' (role 'bot')
//   wrongBearer bearer that does NOT match PIPELINE_TOKEN → falls through to anonymous
//   botNoToken  bearer sent but env.PIPELINE_TOKEN unset → bearer path disabled → anonymous

import { describe, it, expect } from "vitest";
import {
  setup,
  post,
  put,
  get,
  articleRow,
  flagRows,
  blockNetwork,
  echo,
  SLUG,
  PIPELINE_TOKEN,
  SEED_ANNOTATIONS,
  type Harness,
  type ReqOpts,
} from "./helpers/harness.js";

blockNetwork();

const ACTOR_NAMES = [
  "anon",
  "user",
  "patroller",
  "admin",
  "blocked",
  "bot",
  "wrongBearer",
  "botNoToken",
] as const;
type ActorName = (typeof ACTOR_NAMES)[number];

const ACTORS: Record<ActorName, { opts: ReqOpts; prep?: (h: Harness) => void }> = {
  anon: { opts: {} },
  user: { opts: { user: "u-human" } },
  patroller: { opts: { user: "u-patroller" } },
  admin: { opts: { user: "u-admin" } },
  blocked: { opts: { user: "u-blocked" } },
  // Bearer requests carry no Origin header (script clients), like the runner.
  bot: { opts: { bearer: PIPELINE_TOKEN, origin: null } },
  wrongBearer: { opts: { bearer: "wrong-token", origin: null } },
  botNoToken: {
    opts: { bearer: PIPELINE_TOKEN, origin: null },
    prep: (h) => {
      (h.env as { PIPELINE_TOKEN?: string }).PIPELINE_TOKEN = undefined;
    },
  },
};

const NEW_SLUG = "Authz_New_Article";

// Seed one open flag (id 1) for the resolve cells.
function seedFlag(h: Harness): void {
  h.db
    .prepare("INSERT INTO flags (slug, annotation_id, reason, status, created_at) VALUES (?,NULL,'other','open',?)")
    .run(SLUG, Date.now());
}

interface EndpointRow {
  name: string;
  prep?: (h: Harness) => void;
  request: (h: Harness, opts: ReqOpts) => Promise<Response>;
  expected: Record<ActorName, number>;
}

// The matrix. Expected-status sources, for the reader:
//   save     getUser (401 anon) — any session role and the bot may save
//   create   requireRole(['bot']) → 403 else, 201 bot
//   endorse  session-only (401 anon, 403 bot)
//   revert   requireRole(['patroller','admin'])
//   flag     deliberately public (the whole point of D-C4)
//   resolve  requireRole(['patroller','admin'])
//   /api/flags requireRole(['patroller','admin'])
//   /api/work  requireRole(['bot'])
//   reads    public (article JSON, home, sitemap, diff)
//   /flags page: logged-in only — 302 → /login otherwise (bot counts as logged in)
const ENDPOINTS: EndpointRow[] = [
  {
    name: "POST /api/article/:slug (save)",
    request: (h, opts) =>
      post(h.env, `/api/article/${SLUG}`, { annotations: echo(SEED_ANNOTATIONS), base_version: 1 }, opts),
    expected: { anon: 401, user: 200, patroller: 200, admin: 200, blocked: 401, bot: 200, wrongBearer: 401, botNoToken: 401 },
  },
  {
    name: "PUT /api/article/:slug (create)",
    request: (h, opts) =>
      // revid required since F16 (null-revid creates are rejected 400).
      put(h.env, `/api/article/${NEW_SLUG}`, { wikipedia_title: "Authz New Article", revid: 4242, annotations: [] }, opts),
    expected: { anon: 403, user: 403, patroller: 403, admin: 403, blocked: 403, bot: 201, wrongBearer: 403, botNoToken: 403 },
  },
  {
    name: "POST /api/article/:slug (action:endorse)",
    request: (h, opts) =>
      post(h.env, `/api/article/${SLUG}`, { action: "endorse", annotation_id: "aaaaaaaaaaaa", base_version: 1 }, opts),
    expected: { anon: 401, user: 200, patroller: 200, admin: 200, blocked: 401, bot: 403, wrongBearer: 401, botNoToken: 401 },
  },
  {
    name: "POST /api/article/:slug/revert/:revid",
    request: (h, opts) => post(h.env, `/api/article/${SLUG}/revert/1`, {}, opts), // rev 1 = the seed revision
    expected: { anon: 403, user: 403, patroller: 200, admin: 200, blocked: 403, bot: 403, wrongBearer: 403, botNoToken: 403 },
  },
  {
    name: "POST /api/flag/:slug",
    request: (h, opts) => post(h.env, `/api/flag/${SLUG}`, { reason: "other" }, opts),
    expected: { anon: 200, user: 200, patroller: 200, admin: 200, blocked: 200, bot: 200, wrongBearer: 200, botNoToken: 200 },
  },
  {
    name: "POST /api/flag/:id/resolve",
    prep: seedFlag,
    request: (h, opts) => post(h.env, "/api/flag/1/resolve", { resolution: "fixed" }, opts),
    expected: { anon: 403, user: 403, patroller: 200, admin: 200, blocked: 403, bot: 403, wrongBearer: 403, botNoToken: 403 },
  },
  {
    name: "GET /api/flags",
    request: (h, opts) => get(h.env, "/api/flags", opts),
    expected: { anon: 403, user: 403, patroller: 200, admin: 200, blocked: 403, bot: 403, wrongBearer: 403, botNoToken: 403 },
  },
  {
    name: "GET /api/work",
    request: (h, opts) => get(h.env, "/api/work", opts),
    expected: { anon: 403, user: 403, patroller: 403, admin: 403, blocked: 403, bot: 200, wrongBearer: 403, botNoToken: 403 },
  },
  {
    name: "GET /api/article/:slug.json (public)",
    request: (h, opts) => get(h.env, `/api/article/${SLUG}.json`, opts),
    expected: { anon: 200, user: 200, patroller: 200, admin: 200, blocked: 200, bot: 200, wrongBearer: 200, botNoToken: 200 },
  },
  {
    name: "GET /flags (patrol page; 302 = login redirect)",
    request: (h, opts) => get(h.env, "/flags", opts),
    expected: { anon: 302, user: 200, patroller: 200, admin: 200, blocked: 302, bot: 200, wrongBearer: 302, botNoToken: 302 },
  },
  {
    name: "GET / (home; public)",
    request: (h, opts) => get(h.env, "/", opts),
    expected: { anon: 200, user: 200, patroller: 200, admin: 200, blocked: 200, bot: 200, wrongBearer: 200, botNoToken: 200 },
  },
  {
    name: "GET /sitemap.xml (public)",
    request: (h, opts) => get(h.env, "/sitemap.xml", opts),
    expected: { anon: 200, user: 200, patroller: 200, admin: 200, blocked: 200, bot: 200, wrongBearer: 200, botNoToken: 200 },
  },
  {
    name: "GET /:slug/diff/:fromId/:toId (public)",
    request: (h, opts) => get(h.env, `/${SLUG}/diff/1/1`, opts),
    expected: { anon: 200, user: 200, patroller: 200, admin: 200, blocked: 200, bot: 200, wrongBearer: 200, botNoToken: 200 },
  },
];

describe("authorization matrix", () => {
  for (const ep of ENDPOINTS) {
    describe(ep.name, () => {
      for (const actor of ACTOR_NAMES) {
        it(`${actor} → ${ep.expected[actor]}`, async () => {
          const h = setup();
          ACTORS[actor].prep?.(h);
          ep.prep?.(h);
          const res = await ep.request(h, ACTORS[actor].opts);
          expect(res.status).toBe(ep.expected[actor]);
        });
      }
    });
  }
});

describe("denied writes leave no trace", () => {
  it("blocked-session save: version and revision count untouched", async () => {
    const h = setup();
    const res = await post(
      h.env,
      `/api/article/${SLUG}`,
      { annotations: echo(SEED_ANNOTATIONS), base_version: 1 },
      ACTORS.blocked.opts,
    );
    expect(res.status).toBe(401);
    expect(articleRow(h.db)!.version).toBe(1);
  });

  it("admin create (403): the article is never materialized", async () => {
    const h = setup();
    const res = await put(
      h.env,
      `/api/article/${NEW_SLUG}`,
      { wikipedia_title: "Authz New Article", revid: 4242, annotations: [] },
      ACTORS.admin.opts,
    );
    expect(res.status).toBe(403);
    expect(articleRow(h.db, NEW_SLUG)).toBeUndefined();
  });

  it("plain-user revert (403): article state untouched", async () => {
    const h = setup();
    const res = await post(h.env, `/api/article/${SLUG}/revert/1`, {}, ACTORS.user.opts);
    expect(res.status).toBe(403);
    expect(articleRow(h.db)!.version).toBe(1);
  });

  it("bot resolve (403): the flag stays open", async () => {
    const h = setup();
    seedFlag(h);
    const res = await post(h.env, "/api/flag/1/resolve", { resolution: "fixed" }, ACTORS.bot.opts);
    expect(res.status).toBe(403);
    expect(flagRows(h.db)[0].status).toBe("open");
  });
});

describe("history page revert affordance (UI#3)", () => {
  it("revert buttons render only for patroller/admin — the roles the endpoint accepts", async () => {
    const h = setup();
    // Public page, no buttons for anonymous…
    const anon = await get(h.env, `/${SLUG}/history`);
    expect(anon.status).toBe(200);
    expect(await anon.text()).not.toContain('class="revert"');
    // …none for a plain user either (their revert POST would always 403)…
    const userHtml = await (await get(h.env, `/${SLUG}/history`, { user: "u-human" })).text();
    expect(userHtml).not.toContain('class="revert"');
    // …but patroller and admin get the working affordance.
    const patrolHtml = await (await get(h.env, `/${SLUG}/history`, { user: "u-patroller" })).text();
    expect(patrolHtml).toContain('class="revert"');
    const adminHtml = await (await get(h.env, `/${SLUG}/history`, { user: "u-admin" })).text();
    expect(adminHtml).toContain('class="revert"');
  });
});
