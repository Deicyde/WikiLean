// "Tag this passage": the community overlay over the sealed formalization
// store (release R1 of the signed public-editing plan; src/articles.ts's three
// tag routes, src/articles-overlay.ts, src/site-settings.ts, migration 0019).
//
// Pins, in the order the plan states them:
//   §2.2  the three routes — add, change, remove, restore, read — live on save,
//         account required, same-origin, byte-capped, validated against the
//         store, and optimistically concurrent on the row's own version;
//         a community tag of the declaration the pipeline already located is
//         THE SAME ROW, by pair_key;
//   §2.5  the site-wide write brake refuses every write while it is on;
//   §2.6  the newcomer daily cap, the burst limiters and Turnstile on a new
//         account's first edit;
//   §2.7  PR #126's agent role reaches the two tag routes and nothing else of
//         this plane;
//   §2.8  the reader merges the two, labels both, recomputes the status, and
//         offers "As published";
//   §5.3  the CC0 agreement at a person's first edit, with its timestamp.
//
// And the invariant under all of it: the sealed store is never written.

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { describe, it, expect } from "vitest";
import {
  setup, get, post, blockNetwork, PIPELINE_TOKEN, type Harness, type ReqOpts,
} from "./helpers/harness.js";
import {
  articlesFixtureFiles, articlesPairedFiles, bindArticles, FIXTURE_CAMPAIGN, GADGET_PAGEID,
  MATHLIB_REPO, pairedPassage, WIDGET_PAGEID,
} from "./helpers/articles.js";
import {
  CC0_AGREEMENT_VERSION, DAILY_CAP, OVERLAY_STATUSES, TAG_RELATIONS, pairKey, validDeclaration,
} from "../src/articles-overlay.js";
import { SUMMARY_STATUSES } from "../src/articles.js";
import { BRAKE_MESSAGE } from "../src/site-settings.js";

blockNetwork();

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const jsonOf = async (res: Response): Promise<any> => res.json();

const WIDGET_PATH = `/articles/${WIDGET_PAGEID}/Widget_lemma`;
const TAG = "/api/articles/tag";
const AGENT_TOKEN = "wla_" + "b".repeat(43);

// The Widget lemma's first passage: located, formalized, one Mathlib location
// (Widget.exists_spanning_gadget, exactMatch) and one ff-widgets closeMatch.
const P0 = pairedPassage(WIDGET_PAGEID, 0);
// Its third: an absence — searched every library and not found.
const ABSENT = pairedPassage(WIDGET_PAGEID, 2);

function bound(opts: Parameters<typeof setup>[0] = {}): Harness {
  const h = setup(opts);
  bindArticles(h.env, articlesPairedFiles());
  return h;
}

// A person who has already agreed to CC0, so the ordinary cases are not about
// the first-edit gate. agreedAt is the timestamp the plan asks be recorded.
function agreed(h: Harness, userId = "u-human", at = Date.now()): void {
  h.db.prepare("INSERT OR REPLACE INTO user_agreements (user_id, cc0_version, accepted_at, verified_at) VALUES (?,?,?,NULL)")
    .run(userId, CC0_AGREEMENT_VERSION, at);
}

// PR #126's agent: its own users row, role 'agent', owned by u-human.
function mintAgent(h: Harness, ownerId = "u-human", id = "agent-000000000001"): void {
  const now = Date.now();
  h.db.prepare("INSERT INTO users (id, name, role, created_at, updated_at) VALUES (?,?,'agent',?,?)")
    .run(id, "Tagger (agent)", Math.floor(now / 1000), Math.floor(now / 1000));
  h.db.prepare("INSERT INTO agent_tokens (agent_id, owner_id, token_hash, created_at) VALUES (?,?,?,?)")
    .run(id, ownerId, createHash("sha256").update(AGENT_TOKEN).digest("hex"), now);
}

const AGENT: ReqOpts = { bearer: AGENT_TOKEN, origin: null };

function tagBody(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    pageid: WIDGET_PAGEID,
    campaign: FIXTURE_CAMPAIGN,
    passage_key: P0.passage_key,
    library: "mathlib",
    declaration: "Widget.spanning_gadget_unique",
    relation: "exactMatch",
    ...overrides,
  };
}

function tagRows(h: Harness): Array<Record<string, unknown>> {
  return h.db.prepare("SELECT * FROM article_tags ORDER BY created_at, pair_key").all() as Array<Record<string, unknown>>;
}

function tagRow(h: Harness, key: string): Record<string, unknown> | undefined {
  return h.db.prepare("SELECT * FROM article_tags WHERE pair_key = ?").get(key) as Record<string, unknown> | undefined;
}

// ---- the vocabularies this module keeps a copy of -----------------------------

describe("the overlay's vocabularies match the ones they mirror", () => {
  it("the status list is articles.ts's, in the same order", () => {
    // articles-overlay.ts keeps its own copy so it needs no runtime import from
    // articles.ts, which would make a cycle. This is the pin on that copy.
    expect([...OVERLAY_STATUSES]).toEqual([...SUMMARY_STATUSES]);
  });

  it("the relation list is the locate pipeline's, in the same order", () => {
    const vocab = readFileSync(new URL("../../scripts/pipeline/vocab.py", import.meta.url), "utf8");
    const line = /^LOCATION_RELATIONS = \(([^)]*)\)/m.exec(vocab);
    expect(line, "scripts/pipeline/vocab.py LOCATION_RELATIONS").not.toBeNull();
    const names = [...line![1].matchAll(/"([A-Za-z]+)"/g)].map((m) => m[1]);
    expect([...TAG_RELATIONS]).toEqual(names);
  });

  it("a declaration name is checked for shape, never for existence", () => {
    for (const ok of ["AddCommGroup", "Mathlib.Algebra.Group.Defs", "Nat.add_comm'", "a₁", "_private.Foo.bar"]) {
      expect(validDeclaration(ok), ok).toBe(true);
    }
    // Whitespace is out, which also rules out Lean's «guillemet» names: in a
    // formalization link a space is a typo far more often than it is a name.
    for (const bad of ["", "Foo bar", ".Foo", "Foo.", "Foo..bar", "«odd name»", "A".repeat(241), "Foo\u0000bar", "Foo\u200bbar"]) {
      expect(validDeclaration(bad), JSON.stringify(bad)).toBe(false);
    }
  });
});

// ---- adding a tag -----------------------------------------------------------

describe("POST /api/articles/tag (add)", () => {
  it("writes one live community row, keyed by the producer's own pair_key", async () => {
    const h = bound();
    agreed(h);
    const res = await post(h.env, TAG, tagBody(), { user: "u-human" });
    expect(res.status).toBe(200);
    const body = await jsonOf(res);
    const expected = await pairKey(P0.passage_key, MATHLIB_REPO, "Widget.spanning_gadget_unique");
    expect(body.pair_key).toBe(expected);
    expect(body.version).toBe(1);
    expect(body.state).toBe("live");

    const rows = tagRows(h);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      pair_key: expected,
      pageid: WIDGET_PAGEID,
      passage_key: P0.passage_key,
      library: "mathlib",
      // The repository is derived from the article's own universe, never sent.
      repository: MATHLIB_REPO,
      declaration: "Widget.spanning_gadget_unique",
      relation: "exactMatch",
      state: "live",
      origin: "community",
      base_relation: null,
      added_by: "u-human",
      actor_type: "human",
      via: "web",
      campaign: FIXTURE_CAMPAIGN,
      version: 1,
    });
    // Copied from the store at write time, never from the client.
    expect(rows[0].revision).toBe(2002);
    expect(String(rows[0].record_set)).toHaveLength(64);
    expect(String(rows[0].universe_sha256)).toHaveLength(64);
  });

  it("a tag of a declaration the pipeline already located is the SAME row", async () => {
    const h = bound();
    agreed(h);
    const pipeline = P0.locations[0];
    const res = await post(h.env, TAG, tagBody({
      library: pipeline.library, declaration: pipeline.declaration, relation: pipeline.relation,
    }), { user: "u-human" });
    expect(res.status).toBe(200);
    const body = await jsonOf(res);
    expect(body.pair_key).toBe(pipeline.pair_key);
    // Same relation and no note: this is agreement, not a change.
    expect(body.confirms).toBe(true);
    expect(tagRows(h)).toHaveLength(1);
    expect(tagRow(h, pipeline.pair_key)!.base_relation).toBe(pipeline.relation);
  });

  it("changing a pipeline location records what it overrides and bumps the version", async () => {
    const h = bound();
    agreed(h);
    agreed(h, "u-patroller");
    const pipeline = P0.locations[0]; // exactMatch
    const first = await post(h.env, TAG, tagBody({
      library: pipeline.library, declaration: pipeline.declaration, relation: "broadMatch", note: "more general",
    }), { user: "u-human" });
    expect(first.status).toBe(200);
    expect((await jsonOf(first)).confirms).toBe(false);
    const row = tagRow(h, pipeline.pair_key)!;
    expect(row.relation).toBe("broadMatch");
    expect(row.note).toBe("more general");
    expect(row.base_relation).toBe("exactMatch");
    expect(row.version).toBe(1);

    const second = await post(h.env, TAG, tagBody({
      library: pipeline.library, declaration: pipeline.declaration, relation: "closeMatch", base_version: 1,
    }), { user: "u-patroller" });
    expect(second.status).toBe(200);
    expect(tagRows(h)).toHaveLength(1); // last edit wins; one row per pair
    const after = tagRow(h, pipeline.pair_key)!;
    expect(after.relation).toBe("closeMatch");
    expect(after.version).toBe(2);
    expect(after.added_by).toBe("u-patroller");
  });

  it("the sealed store is never written", async () => {
    const h = bound();
    agreed(h);
    const before = articlesPairedFiles();
    await post(h.env, TAG, tagBody(), { user: "u-human" });
    const records = await get(h.env, `/api/articles/records/${WIDGET_PAGEID}`);
    expect(records.status).toBe(200);
    expect(await records.text()).toBe(before.get(`articles/v1/records/${WIDGET_PAGEID}.jsonl`));
    const view = await jsonOf(await get(h.env, `/api/articles/article/${WIDGET_PAGEID}`));
    // The cacheable view model stays the sealed one: no community location.
    const declarations = view.passages.flatMap((p: { locations: Array<{ declaration: string }> }) =>
      p.locations.map((l) => l.declaration));
    expect(declarations).not.toContain("Widget.spanning_gadget_unique");
  });
});

// ---- refusals ----------------------------------------------------------------

describe("POST /api/articles/tag refusals", () => {
  const cases: Array<[string, Record<string, unknown>, number]> = [
    ["a bad page id", { pageid: 0 }, 400],
    ["a bad passage key", { passage_key: "nope" }, 400],
    ["an unknown library", { library: "not-a-library" }, 400],
    ["a declaration with a space", { declaration: "Foo bar" }, 400],
    ["a declaration over 240 characters", { declaration: "A".repeat(241) }, 400],
    ["a relation outside the locate vocabulary", { relation: "sortOfMatch" }, 400],
    ["a note over 500 characters", { note: "x".repeat(501) }, 400],
    ["a campaign the store does not carry", { campaign: "other-campaign" }, 409],
    ["a passage key not on the article", { passage_key: "a".repeat(64) }, 404],
    ["an unknown page id", { pageid: 424242 }, 404],
  ];
  for (const [label, override, status] of cases) {
    it(`${label} → ${status}`, async () => {
      const h = bound();
      agreed(h);
      const res = await post(h.env, TAG, tagBody(override), { user: "u-human" });
      expect(res.status).toBe(status);
      expect(tagRows(h)).toHaveLength(0);
    });
  }

  it("anonymous → 401, blocked → 401, cross-origin → 403, the pipeline bearer → 403", async () => {
    const h = bound();
    agreed(h);
    expect((await post(h.env, TAG, tagBody())).status).toBe(401);
    expect((await post(h.env, TAG, tagBody(), { user: "u-blocked" })).status).toBe(401);
    expect((await post(h.env, TAG, tagBody(), { user: "u-human", origin: "https://evil.example" })).status).toBe(403);
    expect((await post(h.env, TAG, tagBody(), { bearer: PIPELINE_TOKEN, origin: null })).status).toBe(403);
    expect(tagRows(h)).toHaveLength(0);
  });

  it("a body over the 4 KiB cap → 413", async () => {
    const h = bound();
    agreed(h);
    const res = await post(h.env, TAG, tagBody({ note: "x".repeat(8000) }), { user: "u-human" });
    expect(res.status).toBe(413);
    expect(tagRows(h)).toHaveLength(0);
  });

  it("a /1 record set, which carries no passage keys → 422", async () => {
    const h = setup();
    bindArticles(h.env, articlesFixtureFiles()); // the committed /1 fixture
    agreed(h);
    const res = await post(h.env, TAG, tagBody({ passage_key: "a".repeat(64) }), { user: "u-human" });
    expect(res.status).toBe(422);
    expect((await jsonOf(res)).error).toMatch(/\/1 set/);
  });

  it("an unbound store → 503, and a store with no published build → 503", async () => {
    const unbound = setup();
    expect((await post(unbound.env, TAG, tagBody(), { user: "u-human" })).status).toBe(503);
    const files = articlesPairedFiles();
    files.delete("articles/v1/current.json");
    const unpublished = setup();
    bindArticles(unpublished.env, files);
    expect((await post(unpublished.env, TAG, tagBody(), { user: "u-human" })).status).toBe(503);
  });

  it("a stale base_version → 409 with the current row, and the row is untouched", async () => {
    const h = bound();
    agreed(h);
    await post(h.env, TAG, tagBody(), { user: "u-human" });
    const key = await pairKey(P0.passage_key, MATHLIB_REPO, "Widget.spanning_gadget_unique");
    const res = await post(h.env, TAG, tagBody({ relation: "closeMatch", base_version: 0 }), { user: "u-human" });
    expect(res.status).toBe(409);
    const body = await jsonOf(res);
    expect(body.error).toBe("stale");
    expect(body.version).toBe(1);
    expect(tagRow(h, key)!.relation).toBe("exactMatch");
  });
});

// ---- removing and restoring ---------------------------------------------------

describe("POST /api/articles/tag/:pair_key/state", () => {
  const stateUrl = (key: string) => `${TAG}/${key}/state`;

  it("removing a pipeline location writes a gravestone, never a deletion", async () => {
    const h = bound();
    agreed(h);
    const pipeline = P0.locations[0];
    const res = await post(h.env, stateUrl(pipeline.pair_key), {
      pageid: WIDGET_PAGEID, campaign: FIXTURE_CAMPAIGN, state: "removed", reason: "that lemma is about gadgets",
    }, { user: "u-human" });
    expect(res.status).toBe(200);
    const row = tagRow(h, pipeline.pair_key)!;
    expect(row.state).toBe("removed");
    expect(row.removed_by).toBe("u-human");
    expect(row.removed_reason).toBe("that lemma is about gadgets");
    expect(typeof row.removed_at).toBe("number");
    // The pipeline's own values are kept on the row, so restoring is exact.
    expect(row.declaration).toBe(pipeline.declaration);
    expect(row.relation).toBe(pipeline.relation);
    expect(row.base_relation).toBe(pipeline.relation);
  });

  it("a removal needs a reason", async () => {
    const h = bound();
    agreed(h);
    const res = await post(h.env, stateUrl(P0.locations[0].pair_key), {
      pageid: WIDGET_PAGEID, campaign: FIXTURE_CAMPAIGN, state: "removed",
    }, { user: "u-human" });
    expect(res.status).toBe(400);
    expect(tagRows(h)).toHaveLength(0);
  });

  it("restoring puts the link back and clears the gravestone", async () => {
    const h = bound();
    agreed(h);
    agreed(h, "u-admin");
    const key = P0.locations[0].pair_key;
    await post(h.env, stateUrl(key), {
      pageid: WIDGET_PAGEID, campaign: FIXTURE_CAMPAIGN, state: "removed", reason: "wrong",
    }, { user: "u-human" });
    const res = await post(h.env, stateUrl(key), {
      pageid: WIDGET_PAGEID, campaign: FIXTURE_CAMPAIGN, state: "live", base_version: 1,
    }, { user: "u-admin" });
    expect(res.status).toBe(200);
    const row = tagRow(h, key)!;
    expect(row.state).toBe("live");
    expect(row.removed_by).toBeNull();
    expect(row.removed_at).toBeNull();
    expect(row.removed_reason).toBeNull();
    expect(row.version).toBe(2);
  });

  it("an unknown pair key → 404, a bad one → 400, a stale version → 409", async () => {
    const h = bound();
    agreed(h);
    const body = { pageid: WIDGET_PAGEID, campaign: FIXTURE_CAMPAIGN, state: "removed", reason: "x" };
    expect((await post(h.env, stateUrl("f".repeat(64)), body, { user: "u-human" })).status).toBe(404);
    expect((await post(h.env, stateUrl("short"), body, { user: "u-human" })).status).toBe(400);
    const key = P0.locations[0].pair_key;
    await post(h.env, stateUrl(key), body, { user: "u-human" });
    const stale = await post(h.env, stateUrl(key), { ...body, state: "live", base_version: 0 }, { user: "u-human" });
    expect(stale.status).toBe(409);
  });
});

// ---- the overlay read ---------------------------------------------------------

describe("GET /api/articles/overlay/:pageid", () => {
  it("serves the article's rows, never cached, and names their authors", async () => {
    const h = bound();
    agreed(h);
    await post(h.env, TAG, tagBody(), { user: "u-human" });
    const res = await get(h.env, `/api/articles/overlay/${WIDGET_PAGEID}`);
    expect(res.status).toBe(200);
    expect(res.headers.get("Cache-Control")).toBe("no-store");
    const body = await jsonOf(res);
    expect(body.tags).toHaveLength(1);
    expect(body.tags[0].declaration).toBe("Widget.spanning_gadget_unique");
    expect(body.tags[0].added_by).toMatchObject({ id: "u-human", name: "Human Tester", owner_id: null });
  });

  it("an article with no overlay is an empty list, and an unknown page id is 404", async () => {
    const h = bound();
    expect((await jsonOf(await get(h.env, `/api/articles/overlay/${GADGET_PAGEID}`))).tags).toEqual([]);
    expect((await get(h.env, "/api/articles/overlay/0")).status).toBe(404);
  });

  it("the sealed view model keeps its cache header; the overlay never does", async () => {
    const h = bound();
    const sealed = await get(h.env, `/api/articles/article/${WIDGET_PAGEID}`);
    expect(sealed.headers.get("Cache-Control")).toBe("public, max-age=300");
  });
});

// ---- the write brake (plan section 2.5) ---------------------------------------

describe("the site-wide write brake", () => {
  function braked(): Harness {
    const h = setup({ siteWriteMode: "locked" });
    bindArticles(h.env, articlesPairedFiles());
    agreed(h);
    mintAgent(h);
    return h;
  }

  it("refuses every write surface of R1 while it is on", async () => {
    const h = braked();
    const writes: Array<[string, () => Promise<Response>]> = [
      ["tag", () => post(h.env, TAG, tagBody(), { user: "u-human" })],
      ["tag state", () => post(h.env, `${TAG}/${P0.locations[0].pair_key}/state`,
        { pageid: WIDGET_PAGEID, campaign: FIXTURE_CAMPAIGN, state: "removed", reason: "x" }, { user: "u-human" })],
      ["feedback", () => post(h.env, "/api/articles/feedback",
        { location_key: P0.locations[0].location_key, pageid: WIDGET_PAGEID, campaign: FIXTURE_CAMPAIGN, verdict: "confirm" },
        { user: "u-human" })],
      ["an agent's tag", () => post(h.env, TAG, tagBody({ base_version: 0 }), AGENT)],
    ];
    for (const [label, send] of writes) {
      const res = await send();
      expect(res.status, label).toBe(503);
      expect((await jsonOf(res)).error, label).toBe(BRAKE_MESSAGE);
    }
    expect(tagRows(h)).toHaveLength(0);
    expect(h.db.prepare("SELECT COUNT(*) AS n FROM atlas_feedback").get()).toMatchObject({ n: 0 });
  });

  it("refuses an agent's Brain writes too, and leaves a person's alone", async () => {
    const h = braked();
    const edge = { src: "Q1", dst: "Q2", kind: "relates" };
    expect((await post(h.env, "/api/brain/edge", edge, AGENT)).status).toBe(503);
    // A person's Brain write is not an R1 surface and is not braked: it fails
    // for its own reasons (no brain release in this harness), never with 503
    // "editing is paused".
    const person = await post(h.env, "/api/brain/edge", edge, { user: "u-human" });
    if (person.status === 503) expect((await jsonOf(person)).error).not.toBe(BRAKE_MESSAGE);
  });

  it("an unknown mode fails closed; 'open' lets the write through", async () => {
    const closed = setup({ siteWriteMode: "autoconfirmed" });
    bindArticles(closed.env, articlesPairedFiles());
    agreed(closed);
    expect((await post(closed.env, TAG, tagBody(), { user: "u-human" })).status).toBe(503);

    const open = bound();
    agreed(open);
    expect((await post(open.env, TAG, tagBody(), { user: "u-human" })).status).toBe(200);
  });
});

// ---- rate limits and the newcomer daily cap (plan section 2.6) -----------------

describe("limits", () => {
  it("over the burst budget → 429, nothing written", async () => {
    const h = setup({ tagLimiterAllows: false });
    bindArticles(h.env, articlesPairedFiles());
    agreed(h);
    const res = await post(h.env, TAG, tagBody(), { user: "u-human" });
    expect(res.status).toBe(429);
    expect(res.headers.get("Retry-After")).toBe("60");
    expect(tagRows(h)).toHaveLength(0);
  });

  it("a newcomer is capped at 50 overlay edits a day", async () => {
    const h = bound();
    agreed(h);
    // A fresh account is tier 'new' (under four days old), cap 50. Fill the
    // window with rows this account most recently wrote.
    const now = Date.now();
    const ins = h.db.prepare(
      "INSERT INTO article_tags (pair_key, pageid, passage_key, library, repository, declaration, relation, " +
        "state, origin, added_by, actor_type, via, campaign, revision, record_set, universe_sha256, version, created_at, updated_at) " +
        "VALUES (?, 1001, ?, 'mathlib', ?, ?, 'exactMatch', 'live', 'community', 'u-human', 'human', 'web', ?, 2002, ?, ?, 1, ?, ?)",
    );
    for (let i = 0; i < DAILY_CAP.new; i += 1) {
      ins.run(createHash("sha256").update(`filler-${i}`).digest("hex"), P0.passage_key, MATHLIB_REPO,
        `Filler.decl${i}`, FIXTURE_CAMPAIGN, "a".repeat(64), "b".repeat(64), now, now);
    }
    const res = await post(h.env, TAG, tagBody(), { user: "u-human" });
    expect(res.status).toBe(429);
    const body = await jsonOf(res);
    expect(body.tier).toBe("new");
    expect(body.cap).toBe(DAILY_CAP.new);
    expect(tagRows(h)).toHaveLength(DAILY_CAP.new);
  });

  it("an established account gets the larger cap", async () => {
    const h = bound();
    agreed(h);
    // Four days old and ten overlay edits → 'autoconfirmed'.
    const now = Date.now();
    const old = Math.floor((now - 10 * 24 * 60 * 60 * 1000) / 1000);
    h.db.prepare("UPDATE users SET created_at = ? WHERE id = 'u-human'").run(old);
    const ins = h.db.prepare(
      "INSERT INTO article_tags (pair_key, pageid, passage_key, library, repository, declaration, relation, " +
        "state, origin, added_by, actor_type, via, campaign, revision, record_set, universe_sha256, version, created_at, updated_at) " +
        "VALUES (?, 1001, ?, 'mathlib', ?, ?, 'exactMatch', 'live', 'community', 'u-human', 'human', 'web', ?, 2002, ?, ?, 1, ?, ?)",
    );
    // Old edits, outside today's window, so the daily count stays 0.
    const longAgo = now - 40 * 24 * 60 * 60 * 1000;
    for (let i = 0; i < 12; i += 1) {
      ins.run(createHash("sha256").update(`old-${i}`).digest("hex"), P0.passage_key, MATHLIB_REPO,
        `Old.decl${i}`, FIXTURE_CAMPAIGN, "a".repeat(64), "b".repeat(64), longAgo, longAgo);
    }
    const res = await post(h.env, TAG, tagBody(), { user: "u-human" });
    expect(res.status).toBe(200);
  });
});

// ---- the CC0 agreement and Turnstile (decision 11, plan section 2.6) ----------

describe("the first edit", () => {
  it("asks for the CC0 agreement, then records it with a timestamp", async () => {
    const h = bound();
    const refused = await post(h.env, TAG, tagBody(), { user: "u-human" });
    expect(refused.status).toBe(403);
    const body = await jsonOf(refused);
    expect(body.cc0_required).toBe(true);
    expect(body.cc0_version).toBe(CC0_AGREEMENT_VERSION);
    expect(body.cc0_text).toMatch(/CC0 1\.0/);
    expect(tagRows(h)).toHaveLength(0);

    const accepted = await post(h.env, TAG, tagBody({ cc0: true }), { user: "u-human" });
    expect(accepted.status).toBe(200);
    const row = h.db.prepare("SELECT * FROM user_agreements WHERE user_id = 'u-human'").get() as Record<string, unknown>;
    expect(row.cc0_version).toBe(CC0_AGREEMENT_VERSION);
    expect(typeof row.accepted_at).toBe("number");
    // Asked once, never again.
    const second = await post(h.env, TAG, tagBody({ declaration: "Widget.second" }), { user: "u-human" });
    expect(second.status).toBe(200);
  });

  it("a Turnstile secret makes the check compulsory on that first edit", async () => {
    const h = setup({ turnstileSecret: "test-secret" });
    bindArticles(h.env, articlesPairedFiles());
    // No token: refused without any network call (blockNetwork would throw).
    const refused = await post(h.env, TAG, tagBody({ cc0: true }), { user: "u-human" });
    expect(refused.status).toBe(403);
    expect((await jsonOf(refused)).turnstile_required).toBe(true);
    expect(tagRows(h)).toHaveLength(0);
  });

  it("without a secret the check is skipped, and the agreement records no verification", async () => {
    const h = bound();
    expect((await post(h.env, TAG, tagBody({ cc0: true }), { user: "u-human" })).status).toBe(200);
    const row = h.db.prepare("SELECT * FROM user_agreements WHERE user_id = 'u-human'").get() as Record<string, unknown>;
    expect(row.verified_at).toBeNull();
  });
});

// ---- agents on the overlay (decision 13, plan section 2.7) ---------------------

describe("agent writes", () => {
  it("an agent may tag, once its owner has accepted the licence", async () => {
    const h = bound();
    mintAgent(h);
    // The owner has not agreed yet: an agent cannot agree for them.
    const refused = await post(h.env, TAG, tagBody({ base_version: 0, cc0: true }), AGENT);
    expect(refused.status).toBe(403);
    expect((await jsonOf(refused)).error).toMatch(/has not accepted the CC0 terms/);

    agreed(h);
    const res = await post(h.env, TAG, tagBody({ base_version: 0, model: "claude-x", run_id: "r1" }), AGENT);
    expect(res.status).toBe(200);
    const row = tagRows(h)[0];
    expect(row.added_by).toBe("agent-000000000001");
    expect(row.actor_type).toBe("agent");
    expect(row.via).toBe("api");
    expect(row.model).toBe("claude-x");
    expect(row.run_id).toBe("r1");
  });

  it("a bearer write must say what it read", async () => {
    const h = bound();
    mintAgent(h);
    agreed(h);
    const res = await post(h.env, TAG, tagBody(), AGENT);
    expect(res.status).toBe(400);
    expect((await jsonOf(res)).error).toBe("base_version required");
  });

  it("an agent may not change or remove a tag a person wrote", async () => {
    const h = bound();
    mintAgent(h);
    agreed(h);
    await post(h.env, TAG, tagBody(), { user: "u-human" });
    const key = await pairKey(P0.passage_key, MATHLIB_REPO, "Widget.spanning_gadget_unique");
    const change = await post(h.env, TAG, tagBody({ relation: "closeMatch", base_version: 1 }), AGENT);
    expect(change.status).toBe(403);
    const remove = await post(h.env, `${TAG}/${key}/state`,
      { pageid: WIDGET_PAGEID, campaign: FIXTURE_CAMPAIGN, state: "removed", reason: "no", base_version: 1 }, AGENT);
    expect(remove.status).toBe(403);
    expect(tagRow(h, key)!.relation).toBe("exactMatch");
  });

  it("a person may correct an agent's tag", async () => {
    const h = bound();
    mintAgent(h);
    agreed(h);
    await post(h.env, TAG, tagBody({ base_version: 0 }), AGENT);
    const key = await pairKey(P0.passage_key, MATHLIB_REPO, "Widget.spanning_gadget_unique");
    const res = await post(h.env, TAG, tagBody({ relation: "closeMatch", base_version: 1 }), { user: "u-human" });
    expect(res.status).toBe(200);
    expect(tagRow(h, key)!.actor_type).toBe("human");
  });

  it("the atlas_feedback refusal stands", async () => {
    const h = bound();
    mintAgent(h);
    const res = await post(h.env, "/api/articles/feedback", {
      location_key: P0.locations[0].location_key, pageid: WIDGET_PAGEID, campaign: FIXTURE_CAMPAIGN, verdict: "confirm",
    }, AGENT);
    expect(res.status).toBe(403);
  });

  it("all of one person's agents share PR #126's one burst key", async () => {
    // EDIT_LIMITER is the agents' limiter (agents.ts editLimitKey); TAG_LIMITER
    // is the people's. Closing the agents' one must stop the agent and leave
    // the person's own budget alone.
    const h = setup({ limiterAllows: false });
    bindArticles(h.env, articlesPairedFiles());
    mintAgent(h);
    agreed(h);
    expect((await post(h.env, TAG, tagBody({ base_version: 0 }), AGENT)).status).toBe(429);
    expect((await post(h.env, TAG, tagBody(), { user: "u-human" })).status).toBe(200);
  });
});

// ---- the merged reader (plan section 2.8, decision 4) -------------------------

describe("the reader shows the merge", () => {
  it("a community tag is a card with a badge, an attribution and no verdict buttons", async () => {
    const h = bound();
    agreed(h);
    await post(h.env, TAG, tagBody({ declaration: "Widget.community_lemma" }), { user: "u-human" });
    const html = await (await get(h.env, WIDGET_PATH, { user: "u-human" })).text();
    // declName() inserts <wbr> at each dot and underscore.
    expect(html).toContain("community_<wbr>lemma");
    const start = html.indexOf('<article class="articles-card is-community"');
    expect(start).toBeGreaterThan(-1);
    const card = html.slice(start, html.indexOf("</article>", start));
    expect(card).toContain("community_<wbr>lemma");
    expect(card).toContain('class="articles-community"');
    expect(card).toContain("Added by");
    expect(card).toContain("/u/u-human");
    // Jack's display rule: the Lean code leads and no voting data is shown.
    expect(card.indexOf("articles-decl-line")).toBeLessThan(card.indexOf("articles-overlay-line"));
    expect(card).not.toContain("data-verdict=");
  });

  it("an agent's card names the agent and the person who owns it", async () => {
    const h = bound();
    mintAgent(h);
    agreed(h);
    await post(h.env, TAG, tagBody({ base_version: 0, declaration: "Widget.agent_lemma" }), AGENT);
    const html = await (await get(h.env, WIDGET_PATH)).text();
    expect(html).toContain("Tagger (agent)");
    expect(html).toContain("owned by");
    expect(html).toContain("/u/u-human");
  });

  it("a community exactMatch on an absence moves the derived status, and 'As published' does not", async () => {
    const h = bound();
    agreed(h);
    expect(ABSENT.status).toBe("not_formalized");
    const res = await post(h.env, TAG, tagBody({
      passage_key: ABSENT.passage_key, declaration: "Widget.balanced_iff", relation: "exactMatch",
    }), { user: "u-human" });
    expect(res.status).toBe(200);
    // Conservative by design: an editor names a declaration, not whether Lean's
    // proof is finished, so a community location never mints "formalized".
    expect((await jsonOf(res)).status).toBe("statement_only");

    const merged = await (await get(h.env, WIDGET_PATH)).text();
    const sealed = await (await get(h.env, `${WIDGET_PATH}?overlay=off`)).text();
    const hex = /#([0-9a-f]{8,16})$/.exec(ABSENT.item)![1];
    const statusOf = (html: string) => {
      const at = html.indexOf(`id="p-${hex}"`);
      return /data-status="([a-z_]+)"/.exec(html.slice(at, at + 200))![1];
    };
    expect(statusOf(merged)).toBe("statement_only");
    expect(statusOf(sealed)).toBe("not_formalized");
    // The sealed statement stays on the page whichever view is shown.
    expect(merged).toContain("not found");
  });

  it("the view switch appears only once an article has an edit, and counts them", async () => {
    const h = bound();
    agreed(h);
    expect(await (await get(h.env, WIDGET_PATH)).text()).not.toContain("articles-overlay-switch");
    await post(h.env, TAG, tagBody(), { user: "u-human" });
    const html = await (await get(h.env, WIDGET_PATH)).text();
    expect(html).toContain("articles-overlay-switch");
    expect(html).toContain("As published");
    expect(html).toContain("With community edits");
    expect(html).toContain("1 community edit");
  });

  it("a removed link is folded and struck, with its reason, and never deleted", async () => {
    const h = bound();
    agreed(h);
    const key = P0.locations[0].pair_key;
    await post(h.env, `${TAG}/${key}/state`, {
      pageid: WIDGET_PAGEID, campaign: FIXTURE_CAMPAIGN, state: "removed", reason: "about gadgets, not widgets",
    }, { user: "u-human" });
    const html = await (await get(h.env, WIDGET_PATH)).text();
    expect(html).toContain("Removed by an editor");
    expect(html).toContain("about gadgets, not widgets");
    expect(html).toContain("is-removed");
    // Still in the sealed records, and shown as published under the switch.
    const sealed = await (await get(h.env, `${WIDGET_PATH}?overlay=off`)).text();
    expect(sealed).not.toContain("Removed by an editor");
  });

  it("a signed-out reader is offered sign-in, not a form", async () => {
    const h = bound();
    const html = await (await get(h.env, WIDGET_PATH)).text();
    expect(html).toContain("Sign in to tag this passage");
    expect(html).not.toContain('class="articles-tag-form"');
  });

  it("a signed-in reader gets the tag form, with the universe's libraries", async () => {
    const h = bound();
    agreed(h);
    const html = await (await get(h.env, WIDGET_PATH, { user: "u-human" })).text();
    expect(html).toContain("Tag this passage");
    expect(html).toContain('<option value="mathlib">Mathlib</option>');
    expect(html).toContain('<option value="tauceti">TauCeti</option>');
    expect(html).toContain('name="relation"');
  });

  it("the CC0 block and the Turnstile widget appear only for a first edit, with a site key", async () => {
    const plain = bound();
    expect(await (await get(plain.env, WIDGET_PATH, { user: "u-human" })).text()).toContain('id="articles-cc0"');

    const withKey = setup({ turnstileSiteKey: "0xSITEKEY" });
    bindArticles(withKey.env, articlesPairedFiles());
    const res = await get(withKey.env, WIDGET_PATH, { user: "u-human" });
    const html = await res.text();
    expect(html).toContain('data-sitekey="0xSITEKEY"');
    expect(html).toContain("https://challenges.cloudflare.com/turnstile/v0/api.js");
    // The policy widens by exactly that origin, and only on this page state.
    const csp = res.headers.get("Content-Security-Policy") ?? "";
    expect(csp).toContain("frame-src https://challenges.cloudflare.com");

    // Once they have agreed, neither the block nor the script is there, and the
    // policy is the narrow one again.
    agreed(withKey);
    const after = await get(withKey.env, WIDGET_PATH, { user: "u-human" });
    expect(await after.text()).not.toContain("challenges.cloudflare.com");
    expect(after.headers.get("Content-Security-Policy") ?? "").not.toContain("challenges.cloudflare.com");
  });
});

// ---- migration 0019 -----------------------------------------------------------

describe("migration 0019", () => {
  it("is applied by the harness, seeds the brake open and editing off, and is idempotent", () => {
    const h = setup({ slugEditing: false });
    const settings = h.db.prepare("SELECT key, value FROM site_settings ORDER BY key").all() as Array<{ key: string; value: string }>;
    expect(settings).toEqual([
      { key: "site_write_mode", value: "open" },
      { key: "slug_editing", value: "off" },
    ]);
    const columns = (table: string) =>
      (h.db.prepare(`SELECT name FROM pragma_table_info('${table}')`).all() as Array<{ name: string }>).map((r) => r.name);
    expect(columns("article_tags")).toEqual([
      "pair_key", "pageid", "passage_key", "library", "repository", "declaration", "relation", "note",
      "state", "origin", "base_relation", "added_by", "actor_type", "via", "token_id", "model", "run_id",
      "campaign", "revision", "record_set", "universe_sha256", "version", "created_at", "updated_at",
      "removed_by", "removed_at", "removed_reason",
    ]);
    expect(columns("user_agreements")).toEqual(["user_id", "cc0_version", "accepted_at", "verified_at"]);
    const indexes = (h.db.prepare(
      "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'article_tags' AND name LIKE 'idx_%' ORDER BY name",
    ).all() as Array<{ name: string }>).map((r) => r.name);
    expect(indexes).toEqual([
      "idx_article_tags_author", "idx_article_tags_author_day", "idx_article_tags_page",
      "idx_article_tags_recent", "idx_article_tags_remover_day",
    ]);
  });

  it("its CHECKs refuse a row outside the vocabulary", () => {
    const h = setup();
    const insert = (relation: string, state: string) =>
      h.db.prepare(
        "INSERT INTO article_tags (pair_key, pageid, passage_key, library, repository, declaration, relation, " +
          "state, origin, added_by, actor_type, via, campaign, revision, record_set, universe_sha256, version, created_at, updated_at) " +
          "VALUES (?, 1, ?, 'mathlib', 'r', 'D', ?, ?, 'community', 'u-human', 'human', 'web', 'c', 1, ?, ?, 1, 1, 1)",
      ).run("a".repeat(64), "b".repeat(64), relation, state, "c".repeat(64), "d".repeat(64));
    expect(() => insert("sortOfMatch", "live")).toThrow();
    expect(() => insert("exactMatch", "gone")).toThrow();
    expect(() => insert("exactMatch", "live")).not.toThrow();
  });
});
