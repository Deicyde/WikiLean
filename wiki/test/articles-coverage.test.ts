// "These together state the passage": the community coverage bundle (issue
// #173; migration 0021, src/articles.ts's two coverage routes, the merge in
// src/articles-overlay.ts and the stand-in in src/articles-render.ts).
//
// The problem it fixes: a community tag is one declaration with one relation.
// When two declarations each state one clause and together state all of it, the
// honest per-link relation is `hasPart`, and `hasPart` alone leaves the passage
// partial. So an editor who corrected the overstated relations made the status
// worse, and the overlay quietly discouraged accurate review.
//
// Pinned here:
//   the row      one per bundle_key — the passage key and the SORTED part pair
//                keys — so the same parts in either order are the same row;
//   the rules    2 to 5 parts, each a pair key of that passage; 2 to 5 clauses,
//                c1..cN, each naming a part, every part stating one; one of the
//                three strong relations;
//   the gates    the tag routes' own: account, same-origin, the brake, the
//                burst and daily caps, CC0, and an agent may not touch a
//                person's row;
//   the merge    a bundle counts only when every part is a live merged location
//                whose relation, after any override, is hasPart — removing a
//                part or relabelling one away from hasPart withdraws it; a
//                community-added part is statement_only, so a bundle holding
//                one never mints "formalized";
//   the invariant the sealed store is never written, and the cacheable view
//                model never carries a bundle.

import { createHash } from "node:crypto";
import { describe, it, expect } from "vitest";
import { setup, get, post, blockNetwork, PIPELINE_TOKEN, type Harness, type ReqOpts } from "./helpers/harness.js";
import {
  articlesBundleFiles, articlesPairedFiles, bindArticles, bundlePassage, COVERED_PAGEID, FIXTURE_CAMPAIGN,
  MATHLIB_REPO, pairedPassage, WIDGET_PAGEID,
} from "./helpers/articles.js";
import { bundleKey, CC0_AGREEMENT_VERSION, pairKey } from "../src/articles-overlay.js";
import { BRAKE_MESSAGE } from "../src/site-settings.js";

blockNetwork();

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const jsonOf = async (res: Response): Promise<any> => res.json();

const COVERAGE = "/api/articles/coverage";
const TAG = "/api/articles/tag";
const AGENT_TOKEN = "wla_" + "c".repeat(43);
const AGENT: ReqOpts = { bearer: AGENT_TOKEN, origin: null };

// The Gadget's covered theorem with its sealed claim stripped: two proved
// hasPart locations, published partial (helpers/articles.ts).
const P = bundlePassage();
const PART_A = P.locations[0].pair_key as string;
const PART_B = P.locations[1].pair_key as string;
const GADGET_PATH = `/articles/${COVERED_PAGEID}/Gadget_(mathematics)`;

function bound(opts: Parameters<typeof setup>[0] = {}): Harness {
  const h = setup(opts);
  bindArticles(h.env, articlesBundleFiles());
  return h;
}

function agreed(h: Harness, userId = "u-human", at = Date.now()): void {
  h.db.prepare("INSERT OR REPLACE INTO user_agreements (user_id, cc0_version, accepted_at, verified_at) VALUES (?,?,?,NULL)")
    .run(userId, CC0_AGREEMENT_VERSION, at);
}

function mintAgent(h: Harness, ownerId = "u-human", id = "agent-000000000002"): void {
  const now = Date.now();
  h.db.prepare("INSERT INTO users (id, name, role, created_at, updated_at) VALUES (?,?,'agent',?,?)")
    .run(id, "Bundler (agent)", Math.floor(now / 1000), Math.floor(now / 1000));
  h.db.prepare("INSERT INTO agent_tokens (agent_id, owner_id, token_hash, created_at) VALUES (?,?,?,?)")
    .run(id, ownerId, createHash("sha256").update(AGENT_TOKEN).digest("hex"), now);
}

function body(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    pageid: COVERED_PAGEID,
    campaign: FIXTURE_CAMPAIGN,
    passage_key: P.passage_key,
    parts: [PART_A, PART_B],
    conjuncts: [
      { id: "c1", text: "a gadget is connected", part: PART_A },
      { id: "c2", text: "its basepoint has finite degree", part: PART_B },
    ],
    relation: "exactMatch",
    ...overrides,
  };
}

function bundleRows(h: Harness): Array<Record<string, unknown>> {
  return h.db.prepare("SELECT * FROM article_coverage ORDER BY created_at, bundle_key").all() as Array<Record<string, unknown>>;
}

const KEY = () => bundleKey(P.passage_key as string, [PART_A, PART_B]);

// The status of one passage on the rendered page.
function statusOf(html: string, item: string): string {
  const hex = /#([0-9a-f]{8,16})$/.exec(item)![1];
  const at = html.indexOf(`id="p-${hex}"`);
  return /data-status="([a-z_]+)"/.exec(html.slice(at, at + 200))![1];
}

// ---- writing one -------------------------------------------------------------

describe("POST /api/articles/coverage", () => {
  it("writes one live row, keyed by the passage key and the sorted part keys", async () => {
    const h = bound();
    agreed(h);
    const res = await post(h.env, COVERAGE, body(), { user: "u-human" });
    expect(res.status).toBe(200);
    const answer = await jsonOf(res);
    expect(answer.bundle_key).toBe(await KEY());
    expect(answer.version).toBe(1);
    expect(answer.state).toBe("live");
    expect(answer.counts).toBe(true);

    const rows = bundleRows(h);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      bundle_key: await KEY(),
      pageid: COVERED_PAGEID,
      passage_key: P.passage_key,
      relation: "exactMatch",
      state: "live",
      origin: "community",
      added_by: "u-human",
      actor_type: "human",
      via: "web",
      campaign: FIXTURE_CAMPAIGN,
      version: 1,
    });
    expect(JSON.parse(String(rows[0].parts))).toEqual([PART_A, PART_B]);
    expect(JSON.parse(String(rows[0].conjuncts))).toEqual([
      { id: "c1", text: "a gadget is connected", part: PART_A },
      { id: "c2", text: "its basepoint has finite degree", part: PART_B },
    ]);
    // Copied from the store, never from the client.
    expect(String(rows[0].record_set)).toHaveLength(64);
    expect(String(rows[0].universe_sha256)).toHaveLength(64);
  });

  it("the same parts in the other order are the same row, not a second bundle", async () => {
    const h = bound();
    agreed(h);
    agreed(h, "u-patroller");
    const first = await post(h.env, COVERAGE, body(), { user: "u-human" });
    expect(first.status).toBe(200);
    const second = await post(h.env, COVERAGE, body({
      parts: [PART_B, PART_A],
      conjuncts: [
        { id: "c1", text: "its basepoint has finite degree", part: PART_B },
        { id: "c2", text: "a gadget is connected", part: PART_A },
      ],
      relation: "closeMatch",
      base_version: 1,
    }), { user: "u-patroller" });
    expect(second.status).toBe(200);
    expect((await jsonOf(second)).version).toBe(2);
    const rows = bundleRows(h);
    expect(rows).toHaveLength(1);
    expect(rows[0].relation).toBe("closeMatch");
    expect(rows[0].added_by).toBe("u-patroller");
    // The row keeps the editor's own reading order for its parts.
    expect(JSON.parse(String(rows[0].parts))).toEqual([PART_B, PART_A]);
  });

  it("the sealed store is never written and the cacheable view model never carries a bundle", async () => {
    const h = bound();
    agreed(h);
    const before = articlesBundleFiles();
    await post(h.env, COVERAGE, body(), { user: "u-human" });
    const records = await get(h.env, `/api/articles/records/${COVERED_PAGEID}`);
    expect(records.status).toBe(200);
    expect(await records.text()).toBe(before.get(`articles/v1/records/${COVERED_PAGEID}.jsonl`));
    const view = await get(h.env, `/api/articles/article/${COVERED_PAGEID}`);
    expect(view.headers.get("Cache-Control")).toBe("public, max-age=300");
    const model = await jsonOf(view);
    expect(model.passages[1].coverage ?? null).toBeNull();
    expect(model.passages[1].status).toBe("partial");
  });

  it("an agent writes its own row, labelled, and must say what it read", async () => {
    const h = bound();
    mintAgent(h);
    agreed(h);
    const missing = await post(h.env, COVERAGE, body(), AGENT);
    expect(missing.status).toBe(400);
    expect((await jsonOf(missing)).error).toBe("base_version required");
    const res = await post(h.env, COVERAGE, body({ base_version: 0, model: "claude-x", run_id: "r1" }), AGENT);
    expect(res.status).toBe(200);
    expect(bundleRows(h)[0]).toMatchObject({
      added_by: "agent-000000000002", actor_type: "agent", via: "api", model: "claude-x", run_id: "r1",
    });
  });
});

// ---- refusals ------------------------------------------------------------------

describe("POST /api/articles/coverage refusals", () => {
  const cases: Array<[string, Record<string, unknown>, number]> = [
    ["a bad page id", { pageid: 0 }, 400],
    ["a bad passage key", { passage_key: "nope" }, 400],
    ["one part", { parts: [PART_A], conjuncts: [{ id: "c1", text: "one", part: PART_A }, { id: "c2", text: "two", part: PART_A }] }, 400],
    ["six parts", { parts: [PART_A, PART_B, "a".repeat(64), "b".repeat(64), "c".repeat(64), "d".repeat(64)] }, 400],
    ["a part named twice", { parts: [PART_A, PART_A] }, 400],
    ["a part that is not a key", { parts: [PART_A, "nope"] }, 400],
    ["a weak bundle relation", { relation: "hasPart" }, 400],
    ["a relation outside the vocabulary", { relation: "sortOfMatch" }, 400],
    ["one clause", { conjuncts: [{ id: "c1", text: "only one", part: PART_A }] }, 400],
    ["clauses out of order", {
      conjuncts: [{ id: "c2", text: "a", part: PART_A }, { id: "c1", text: "b", part: PART_B }],
    }, 400],
    ["a clause naming no part", {
      conjuncts: [{ id: "c1", text: "a", part: PART_A }, { id: "c2", text: "b", part: "f".repeat(64) }],
    }, 400],
    ["a part stating no clause", {
      conjuncts: [{ id: "c1", text: "a", part: PART_A }, { id: "c2", text: "b", part: PART_A }],
    }, 400],
    ["an empty clause", {
      conjuncts: [{ id: "c1", text: "   ", part: PART_A }, { id: "c2", text: "b", part: PART_B }],
    }, 400],
    ["a clause over 500 characters", {
      conjuncts: [{ id: "c1", text: "x".repeat(501), part: PART_A }, { id: "c2", text: "b", part: PART_B }],
    }, 400],
    ["a clause with a line break", {
      conjuncts: [{ id: "c1", text: "a\nb", part: PART_A }, { id: "c2", text: "b", part: PART_B }],
    }, 400],
    ["a campaign the store does not carry", { campaign: "other-campaign" }, 409],
    ["a passage key not on the article", { passage_key: "a".repeat(64) }, 404],
    ["a pair key that is no location of this passage", {
      parts: [PART_A, "e".repeat(64)],
      conjuncts: [{ id: "c1", text: "a", part: PART_A }, { id: "c2", text: "b", part: "e".repeat(64) }],
    }, 404],
    ["an unknown page id", { pageid: 424242 }, 404],
  ];
  for (const [label, override, status] of cases) {
    it(`${label} → ${status}`, async () => {
      const h = bound();
      agreed(h);
      const res = await post(h.env, COVERAGE, body(override), { user: "u-human" });
      expect(res.status).toBe(status);
      expect(bundleRows(h)).toHaveLength(0);
    });
  }

  it("anonymous → 401, blocked → 401, cross-origin → 403, the pipeline bearer → 403", async () => {
    const h = bound();
    agreed(h);
    expect((await post(h.env, COVERAGE, body())).status).toBe(401);
    expect((await post(h.env, COVERAGE, body(), { user: "u-blocked" })).status).toBe(401);
    expect((await post(h.env, COVERAGE, body(), { user: "u-human", origin: "https://evil.example" })).status).toBe(403);
    expect((await post(h.env, COVERAGE, body(), { bearer: PIPELINE_TOKEN, origin: null })).status).toBe(403);
    expect(bundleRows(h)).toHaveLength(0);
  });

  it("asks for the CC0 agreement at a first edit, and records it with the write", async () => {
    const h = bound();
    const refused = await post(h.env, COVERAGE, body(), { user: "u-human" });
    expect(refused.status).toBe(403);
    expect((await jsonOf(refused)).cc0_required).toBe(true);
    expect(bundleRows(h)).toHaveLength(0);
    expect((await post(h.env, COVERAGE, body({ cc0: true }), { user: "u-human" })).status).toBe(200);
    const row = h.db.prepare("SELECT * FROM user_agreements WHERE user_id = 'u-human'").get() as Record<string, unknown>;
    expect(row.cc0_version).toBe(CC0_AGREEMENT_VERSION);
  });

  it("an agent whose owner has not accepted is refused", async () => {
    const h = bound();
    mintAgent(h);
    const res = await post(h.env, COVERAGE, body({ base_version: 0, cc0: true }), AGENT);
    expect(res.status).toBe(403);
    expect((await jsonOf(res)).error).toMatch(/accept the CC0 contribution agreement at \/agents/);
    expect(bundleRows(h)).toHaveLength(0);
  });

  it("the site-wide write brake refuses it, for a person and for an agent", async () => {
    const h = setup({ siteWriteMode: "locked" });
    bindArticles(h.env, articlesBundleFiles());
    agreed(h);
    mintAgent(h);
    for (const opts of [{ user: "u-human" } as ReqOpts, AGENT]) {
      const res = await post(h.env, COVERAGE, body({ base_version: 0 }), opts);
      expect(res.status).toBe(503);
      expect((await jsonOf(res)).error).toBe(BRAKE_MESSAGE);
    }
    expect(bundleRows(h)).toHaveLength(0);
  });

  it("the burst budget and the daily cap hold it, counting tags and bundles together", async () => {
    const burst = setup({ tagLimiterAllows: false });
    bindArticles(burst.env, articlesBundleFiles());
    agreed(burst);
    expect((await post(burst.env, COVERAGE, body(), { user: "u-human" })).status).toBe(429);

    const h = bound();
    agreed(h);
    const now = Date.now();
    const ins = h.db.prepare(
      "INSERT INTO article_tags (pair_key, pageid, passage_key, library, repository, declaration, relation, " +
        "state, origin, added_by, actor_type, via, campaign, revision, record_set, universe_sha256, version, created_at, updated_at) " +
        "VALUES (?, 1001, ?, 'mathlib', ?, ?, 'exactMatch', 'live', 'community', 'u-human', 'human', 'web', ?, 2002, ?, ?, 1, ?, ?)",
    );
    for (let i = 0; i < 50; i += 1) {
      ins.run(createHash("sha256").update(`cap-${i}`).digest("hex"), P.passage_key, MATHLIB_REPO,
        `Filler.decl${i}`, FIXTURE_CAMPAIGN, "a".repeat(64), "b".repeat(64), now, now);
    }
    const capped = await post(h.env, COVERAGE, body(), { user: "u-human" });
    expect(capped.status).toBe(429);
    expect((await jsonOf(capped)).cap).toBe(50);
    expect(bundleRows(h)).toHaveLength(0);
  });

  it("bundles fill the same daily cap a tag spends, not a second one", async () => {
    const h = bound();
    agreed(h);
    const now = Date.now();
    const ins = h.db.prepare(
      "INSERT INTO article_coverage (bundle_key, pageid, passage_key, parts, conjuncts, relation, state, origin, " +
        "added_by, actor_type, via, campaign, revision, record_set, universe_sha256, version, created_at, updated_at) " +
        "VALUES (?, ?, ?, ?, ?, 'exactMatch', 'live', 'community', 'u-human', 'human', 'web', ?, 2002, ?, ?, 1, ?, ?)",
    );
    const parts = JSON.stringify([PART_A, PART_B]);
    const conjuncts = JSON.stringify([
      { id: "c1", text: "a", part: PART_A }, { id: "c2", text: "b", part: PART_B },
    ]);
    for (let i = 0; i < 50; i += 1) {
      ins.run(createHash("sha256").update(`bundle-${i}`).digest("hex"), COVERED_PAGEID, P.passage_key,
        parts, conjuncts, FIXTURE_CAMPAIGN, "a".repeat(64), "b".repeat(64), now, now);
    }
    // A day's worth of bundles spends the cap a tag would have spent.
    const tag = await post(h.env, TAG, {
      pageid: COVERED_PAGEID, campaign: FIXTURE_CAMPAIGN, passage_key: P.passage_key,
      library: "mathlib", declaration: "Gadget.another", relation: "relatedMatch",
    }, { user: "u-human" });
    expect(tag.status).toBe(429);
    expect((await jsonOf(tag)).cap).toBe(50);
  });

  it("counting both surfaces reads each by its index, never the whole overlay", async () => {
    // The allowance runs on EVERY community write, so it must stay an indexed
    // read of one account's rows: counting the two tables through one UNION
    // would make SQLite materialise it and scan both, which grows with the
    // overlay and is charged per row read.
    const h = bound();
    agreed(h);
    const seen: string[] = [];
    const prepare = h.env.DB.prepare.bind(h.env.DB);
    Object.assign(h.env.DB, { prepare: (query: string) => { seen.push(query); return prepare(query); } });
    expect((await post(h.env, COVERAGE, body(), { user: "u-human" })).status).toBe(200);
    const allowance = seen.filter((query) => query.includes("FROM agent_tokens WHERE owner_id = ?"));
    expect(allowance).toHaveLength(1);
    // The account three times, then one bound time per window clause.
    const args = [...allowance[0]].filter((c) => c === "?").map((_, i) => (i < 3 ? "u-human" : 0));
    const plan = (h.db.prepare(`EXPLAIN QUERY PLAN ${allowance[0]}`)
      .all(...args) as Array<{ detail: string }>).map((row) => row.detail);
    expect(plan.filter((step) => /\bSCAN (article_tags|article_coverage)\b/.test(step))).toEqual([]);
    for (const table of ["article_tags", "article_coverage"]) {
      expect(plan.filter((step) => step.includes(`SEARCH ${table} USING`)).length, table).toBeGreaterThan(0);
    }
  });

  it("a part an editor has removed is no part to build on", async () => {
    const h = bound();
    agreed(h);
    const gone = await post(h.env, `${TAG}/${PART_A}/state`, {
      pageid: COVERED_PAGEID, campaign: FIXTURE_CAMPAIGN, state: "removed", reason: "about widgets",
    }, { user: "u-human" });
    expect(gone.status).toBe(200);
    const res = await post(h.env, COVERAGE, body(), { user: "u-human" });
    expect(res.status).toBe(404);
    expect((await jsonOf(res)).error).toMatch(/no live location of this passage/);
    expect(bundleRows(h)).toHaveLength(0);
  });

  it("a /1 record set → 422, an unbound store → 503, and a stale version → 409", async () => {
    const one = setup();
    bindArticles(one.env); // the committed /1 fixture: no passage keys
    agreed(one);
    const refused = await post(one.env, COVERAGE, body({ passage_key: "a".repeat(64) }), { user: "u-human" });
    expect(refused.status).toBe(422);

    const unbound = setup();
    expect((await post(unbound.env, COVERAGE, body(), { user: "u-human" })).status).toBe(503);

    const h = bound();
    agreed(h);
    await post(h.env, COVERAGE, body(), { user: "u-human" });
    const stale = await post(h.env, COVERAGE, body({ relation: "closeMatch", base_version: 0 }), { user: "u-human" });
    expect(stale.status).toBe(409);
    expect((await jsonOf(stale)).version).toBe(1);
    expect(bundleRows(h)[0].relation).toBe("exactMatch");
  });

  it("a body over the cap → 413", async () => {
    const h = bound();
    agreed(h);
    const res = await post(h.env, COVERAGE, body({
      conjuncts: [
        { id: "c1", text: "a", part: PART_A, filler: "x".repeat(20000) },
        { id: "c2", text: "b", part: PART_B },
      ],
    }), { user: "u-human" });
    expect(res.status).toBe(413);
    expect(bundleRows(h)).toHaveLength(0);
  });

  it("without migration 0021 the route answers 503 and the rest of the overlay works", async () => {
    const h = bound();
    agreed(h);
    h.db.exec("DROP TABLE article_coverage");
    const res = await post(h.env, COVERAGE, body(), { user: "u-human" });
    expect(res.status).toBe(503);
    expect((await jsonOf(res)).error).toMatch(/migration 0021/);
    // A tag still saves, and the article page still renders.
    const tag = await post(h.env, TAG, {
      pageid: COVERED_PAGEID, campaign: FIXTURE_CAMPAIGN, passage_key: P.passage_key,
      library: "mathlib", declaration: "Gadget.other", relation: "relatedMatch",
    }, { user: "u-human" });
    expect(tag.status).toBe(200);
    expect((await get(h.env, GADGET_PATH)).status).toBe(200);
  });
});

// ---- withdrawing and restoring --------------------------------------------------

describe("POST /api/articles/coverage/:bundle_key/state", () => {
  const stateUrl = (key: string) => `${COVERAGE}/${key}/state`;

  async function withBundle(): Promise<{ h: Harness; key: string }> {
    const h = bound();
    agreed(h);
    agreed(h, "u-patroller");
    const res = await post(h.env, COVERAGE, body(), { user: "u-human" });
    expect(res.status).toBe(200);
    return { h, key: (await jsonOf(res)).bundle_key as string };
  }

  it("withdrawing leaves a gravestone and takes the bundle out of the status", async () => {
    const { h, key } = await withBundle();
    expect(statusOf(await (await get(h.env, GADGET_PATH)).text(), P.item)).toBe("formalized");
    const res = await post(h.env, stateUrl(key), {
      pageid: COVERED_PAGEID, campaign: FIXTURE_CAMPAIGN, state: "removed", reason: "they leave a clause out",
    }, { user: "u-patroller" });
    expect(res.status).toBe(200);
    expect((await jsonOf(res)).status).toBe("partial");
    const row = bundleRows(h)[0];
    expect(row).toMatchObject({ state: "removed", removed_by: "u-patroller", removed_reason: "they leave a clause out" });
    expect(typeof row.removed_at).toBe("number");
    expect(statusOf(await (await get(h.env, GADGET_PATH)).text(), P.item)).toBe("partial");
  });

  it("a withdrawal needs a reason; restoring clears the gravestone", async () => {
    const { h, key } = await withBundle();
    const noReason = await post(h.env, stateUrl(key), {
      pageid: COVERED_PAGEID, campaign: FIXTURE_CAMPAIGN, state: "removed",
    }, { user: "u-human" });
    expect(noReason.status).toBe(400);
    await post(h.env, stateUrl(key), {
      pageid: COVERED_PAGEID, campaign: FIXTURE_CAMPAIGN, state: "removed", reason: "wrong",
    }, { user: "u-human" });
    const back = await post(h.env, stateUrl(key), {
      pageid: COVERED_PAGEID, campaign: FIXTURE_CAMPAIGN, state: "live", base_version: 2,
    }, { user: "u-patroller" });
    expect(back.status).toBe(200);
    expect(bundleRows(h)[0]).toMatchObject({ state: "live", removed_by: null, removed_at: null, removed_reason: null, version: 3 });
  });

  it("an unknown key → 404, a bad one → 400, another article's page id → 404", async () => {
    const { h, key } = await withBundle();
    const payload = { pageid: COVERED_PAGEID, campaign: FIXTURE_CAMPAIGN, state: "removed", reason: "x" };
    expect((await post(h.env, stateUrl("f".repeat(64)), payload, { user: "u-human" })).status).toBe(404);
    expect((await post(h.env, stateUrl("short"), payload, { user: "u-human" })).status).toBe(400);
    const other = await post(h.env, stateUrl(key), { ...payload, pageid: WIDGET_PAGEID }, { user: "u-human" });
    expect([404, 409]).toContain(other.status);
    expect(bundleRows(h)[0].state).toBe("live");
  });
});

// ---- agents and people ----------------------------------------------------------

describe("an agent and a person's bundle", () => {
  it("an agent may not change or withdraw a bundle a person wrote; a person may correct an agent's", async () => {
    const h = bound();
    mintAgent(h);
    agreed(h);
    const made = await post(h.env, COVERAGE, body(), { user: "u-human" });
    const key = (await jsonOf(made)).bundle_key as string;
    const change = await post(h.env, COVERAGE, body({ relation: "closeMatch", base_version: 1 }), AGENT);
    expect(change.status).toBe(403);
    const withdraw = await post(h.env, `${COVERAGE}/${key}/state`, {
      pageid: COVERED_PAGEID, campaign: FIXTURE_CAMPAIGN, state: "removed", reason: "no", base_version: 1,
    }, AGENT);
    expect(withdraw.status).toBe(403);
    expect(bundleRows(h)[0]).toMatchObject({ relation: "exactMatch", state: "live", actor_type: "human" });

    // The other way round: a person corrects an agent's bundle freely.
    const fresh = bound();
    mintAgent(fresh);
    agreed(fresh);
    expect((await post(fresh.env, COVERAGE, body({ base_version: 0 }), AGENT)).status).toBe(200);
    const corrected = await post(fresh.env, COVERAGE, body({ relation: "closeMatch", base_version: 1 }), { user: "u-human" });
    expect(corrected.status).toBe(200);
    expect(bundleRows(fresh)[0]).toMatchObject({ relation: "closeMatch", actor_type: "human" });
  });
});

// ---- the merge ------------------------------------------------------------------

describe("the merge counts a bundle as one more location", () => {
  it("two hasPart parts that together state the passage put it back to formalized", async () => {
    const h = bound();
    agreed(h);
    // As published, the parts alone leave it partial.
    const sealed = await (await get(h.env, `${GADGET_PATH}?overlay=off`)).text();
    expect(statusOf(sealed, P.item)).toBe("partial");
    const res = await post(h.env, COVERAGE, body(), { user: "u-human" });
    expect((await jsonOf(res)).status).toBe("formalized");
    const merged = await (await get(h.env, GADGET_PATH)).text();
    expect(statusOf(merged, P.item)).toBe("formalized");
    expect(statusOf(await (await get(h.env, `${GADGET_PATH}?overlay=off`)).text(), P.item)).toBe("partial");
  });

  it("removing one part withdraws it, and restoring the part brings it back", async () => {
    const h = bound();
    agreed(h);
    await post(h.env, COVERAGE, body(), { user: "u-human" });
    const gone = await post(h.env, `${TAG}/${PART_A}/state`, {
      pageid: COVERED_PAGEID, campaign: FIXTURE_CAMPAIGN, state: "removed", reason: "that lemma is about widgets",
    }, { user: "u-human" });
    expect(gone.status).toBe(200);
    expect((await jsonOf(gone)).status).toBe("partial");
    const html = await (await get(h.env, GADGET_PATH)).text();
    expect(html).toContain("no longer state the passage together");

    const back = await post(h.env, `${TAG}/${PART_A}/state`, {
      pageid: COVERED_PAGEID, campaign: FIXTURE_CAMPAIGN, state: "live", base_version: 1,
    }, { user: "u-human" });
    expect((await jsonOf(back)).status).toBe("formalized");
  });

  it("relabelling a part away from hasPart withdraws it too", async () => {
    const h = bound();
    agreed(h);
    await post(h.env, COVERAGE, body(), { user: "u-human" });
    const relabelled = await post(h.env, TAG, {
      pageid: COVERED_PAGEID, campaign: FIXTURE_CAMPAIGN, passage_key: P.passage_key,
      library: P.locations[0].library, declaration: P.locations[0].declaration, relation: "relatedMatch",
    }, { user: "u-human" });
    expect(relabelled.status).toBe(200);
    // The part is no longer a part, so the bundle no longer counts: what is
    // left is one hasPart location, which is partial.
    expect((await jsonOf(relabelled)).status).toBe("partial");
    const merged = await (await get(h.env, GADGET_PATH)).text();
    expect(merged).toContain("no longer state the passage together");
  });

  it("a bundle holding a community-added part never mints a formalization claim", async () => {
    const h = bound();
    agreed(h);
    // A third part nobody located: a community row, which the merge reads as
    // statement_only whatever the editor says (COMMUNITY_PROOF_STATUS).
    const added = await post(h.env, TAG, {
      pageid: COVERED_PAGEID, campaign: FIXTURE_CAMPAIGN, passage_key: P.passage_key,
      library: "mathlib", declaration: "Gadget.basepoint_unique", relation: "hasPart",
    }, { user: "u-human" });
    expect(added.status).toBe(200);
    const third = await pairKey(P.passage_key as string, MATHLIB_REPO, "Gadget.basepoint_unique");
    const res = await post(h.env, COVERAGE, body({
      parts: [PART_A, PART_B, third],
      conjuncts: [
        { id: "c1", text: "a gadget is connected", part: PART_A },
        { id: "c2", text: "its basepoint has finite degree", part: PART_B },
        { id: "c3", text: "its basepoint is unique", part: third },
      ],
    }), { user: "u-human" });
    expect(res.status).toBe(200);
    const answer = await jsonOf(res);
    expect(answer.counts).toBe(true);
    expect(answer.status).toBe("statement_only");
  });

  it("a bundle is live at once for every reader, and shows its parts and clauses", async () => {
    const h = bound();
    agreed(h);
    await post(h.env, COVERAGE, body(), { user: "u-human" });
    const html = await (await get(h.env, GADGET_PATH)).text();
    expect(html).toContain("articles-bundle");
    expect(html).toContain("Formalized by 2 parts together");
    expect(html).toContain("a gadget is connected");
    expect(html).toContain("its basepoint has finite degree");
    expect(html).toContain("/u/u-human");
    // "As published" shows neither the bundle nor its effect.
    const sealed = await (await get(h.env, `${GADGET_PATH}?overlay=off`)).text();
    expect(sealed).not.toContain("articles-bundle");
  });

  it("a bundle on a passage with no community tags still counts as an edit on the page", async () => {
    const h = bound();
    agreed(h);
    expect(await (await get(h.env, GADGET_PATH)).text()).not.toContain("articles-overlay-switch");
    await post(h.env, COVERAGE, body(), { user: "u-human" });
    const html = await (await get(h.env, GADGET_PATH)).text();
    expect(html).toContain("articles-overlay-switch");
    expect(html).toContain("1 community edit");
  });
});

// ---- the form and the overlay read -----------------------------------------------

describe("the reader's bundle form", () => {
  it("is offered to a signed-in editor where two links each state part of the passage", async () => {
    const h = bound();
    agreed(h);
    const html = await (await get(h.env, GADGET_PATH, { user: "u-human" })).text();
    expect(html).toContain("These together state the passage");
    expect(html).toContain('name="part"');
    expect(html).toContain('<option value="broadMatch"');
    // The strong three only: a bundle's relation is its weakest part's.
    const form = html.slice(html.indexOf("articles-bundle-form"));
    expect(form.slice(0, form.indexOf("</form>"))).not.toContain('value="hasPart"');
  });

  it("is not offered to a signed-out reader, nor on a passage with one part", async () => {
    const h = bound();
    expect(await (await get(h.env, GADGET_PATH)).text()).not.toContain("These together state the passage");
    const widget = setup();
    bindArticles(widget.env, articlesPairedFiles());
    const html = await (await get(widget.env, `/articles/${WIDGET_PAGEID}/Widget_lemma`, { user: "u-human" })).text();
    expect(html).not.toContain("These together state the passage");
  });

  it("GET /api/articles/overlay/:pageid lists the bundles, parsed, never cached", async () => {
    const h = bound();
    agreed(h);
    await post(h.env, COVERAGE, body(), { user: "u-human" });
    const res = await get(h.env, `/api/articles/overlay/${COVERED_PAGEID}`);
    expect(res.status).toBe(200);
    expect(res.headers.get("Cache-Control")).toBe("no-store");
    const answer = await jsonOf(res);
    expect(answer.bundles).toHaveLength(1);
    expect(answer.bundles[0]).toMatchObject({
      bundle_key: await KEY(), passage_key: P.passage_key, relation: "exactMatch", state: "live", version: 1,
    });
    expect(answer.bundles[0].parts).toEqual([PART_A, PART_B]);
    expect(answer.bundles[0].conjuncts[0]).toEqual({ id: "c1", text: "a gadget is connected", part: PART_A });
    expect(answer.bundles[0].added_by).toMatchObject({ id: "u-human", name: "Human Tester" });
    // An article with none answers an empty list rather than nothing.
    expect((await jsonOf(await get(h.env, `/api/articles/overlay/${WIDGET_PAGEID}`))).bundles).toEqual([]);
  });
});

// ---- migration 0021 ---------------------------------------------------------------

describe("migration 0021", () => {
  it("is applied by the harness, with the columns and indexes the overlay reads", () => {
    const h = setup();
    const columns = (h.db.prepare("SELECT name FROM pragma_table_info('article_coverage')").all() as Array<{ name: string }>)
      .map((r) => r.name);
    expect(columns).toEqual([
      "bundle_key", "pageid", "passage_key", "parts", "conjuncts", "relation", "state", "origin",
      "added_by", "actor_type", "via", "token_id", "model", "run_id", "campaign", "revision",
      "record_set", "universe_sha256", "version", "created_at", "updated_at",
      "removed_by", "removed_at", "removed_reason",
    ]);
    const indexes = (h.db.prepare(
      "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'article_coverage' AND name LIKE 'idx_%' ORDER BY name",
    ).all() as Array<{ name: string }>).map((r) => r.name);
    expect(indexes).toEqual([
      "idx_article_coverage_author", "idx_article_coverage_author_day", "idx_article_coverage_page",
      "idx_article_coverage_recent", "idx_article_coverage_remover_day",
    ]);
  });

  it("its CHECKs refuse a row outside the vocabulary", () => {
    const h = setup();
    const insert = (relation: string, state: string, parts: string, conjuncts: string) =>
      h.db.prepare(
        "INSERT INTO article_coverage (bundle_key, pageid, passage_key, parts, conjuncts, relation, state, origin, " +
          "added_by, actor_type, via, campaign, revision, record_set, universe_sha256, version, created_at, updated_at) " +
          "VALUES (?, 1, ?, ?, ?, ?, ?, 'community', 'u-human', 'human', 'web', 'c', 1, ?, ?, 1, 1, 1)",
      ).run("a".repeat(64), "b".repeat(64), parts, conjuncts, relation, state, "c".repeat(64), "d".repeat(64));
    const parts = JSON.stringify(["x", "y"]);
    const conjuncts = JSON.stringify([{ id: "c1" }, { id: "c2" }]);
    expect(() => insert("hasPart", "live", parts, conjuncts)).toThrow();
    expect(() => insert("exactMatch", "gone", parts, conjuncts)).toThrow();
    expect(() => insert("exactMatch", "live", JSON.stringify(["x"]), conjuncts)).toThrow();
    expect(() => insert("exactMatch", "live", "not json", conjuncts)).toThrow();
    expect(() => insert("exactMatch", "live", parts, conjuncts)).not.toThrow();
  });
});
