// Stable annotation ids (P1, C1).
//
// Two layers under test:
//   1. The Worker save path's id contract — posted ids validated (format +
//      uniqueness), missing ids lazily healed: a posted annotation that
//      anchor-sig-matches a stored annotation ADOPTS the stored id (identity
//      continuity), only truly-new annotations get fresh ids — and bot writes
//      that echo stored ids verbatim still pass the 422 human-preservation
//      check.
//   2. The pure backfill transform (scripts/backfill-ids.ts
//      assignAnnotationIds): full coverage, idempotence, collision handling.
//
// Worker tests reuse the api.test.ts harness pattern: the real Hono app via
// app.request() over node:sqlite + KV shims, no network.

import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { app } from "../src/index.js";
import type { Env } from "../src/env.js";
import { makeD1, makeKV } from "./helpers/d1shim.js";
import { assignAnnotationIds, ANNOTATION_ID_RE } from "../scripts/backfill-ids.js";
import { anchorSig, type AnnRecord } from "../src/validation.js";

const MIGRATIONS_DIR = resolve(process.cwd(), "migrations");
const MIGRATIONS = readdirSync(MIGRATIONS_DIR)
  .filter((f) => f.endsWith(".sql"))
  .sort();

const SLUG = "Id_Test";
const REVID = 4242;
const PIPELINE_TOKEN = "test-pipeline-token";

const WP_FIXTURE = [
  "<p>The <b>centralizer</b> of a subset of a group is a subgroup.</p>",
  "<h2>Properties</h2>",
  "<p>The normalizer of a subgroup contains the centralizer. Every characteristic subgroup is normal.</p>",
].join("\n");

// One stored annotation already carrying an id, one still without (the
// pre-backfill straggler the lazy-heal exists for).
const STORED_WITH_ID = {
  id: "0123456789ab",
  status: "formalized",
  kind: "definition",
  label: "Centralizer",
  provenance: "ai",
  anchor: { section: "(lead)", snippet: "centralizer" },
  mathlib: { decl: "Subgroup.centralizer", module: "Mathlib.GroupTheory.Subgroup.Centralizer", match_kind: "exact" },
};
const STORED_NO_ID = {
  status: "partial",
  kind: "definition",
  label: "Normalizer",
  provenance: "ai",
  anchor: { section: "Properties", snippet: "normalizer of a subgroup" },
  mathlib: { decl: null, module: null, match_kind: null },
};
const SEED = [STORED_WITH_ID, STORED_NO_ID];

const NEW_ANNOTATION = {
  status: "formalized",
  kind: "theorem",
  label: "Characteristic implies normal",
  provenance: "human",
  anchor: { section: "Properties", snippet: "Every characteristic subgroup is normal" },
  mathlib: { decl: "Subgroup.Characteristic", module: "Mathlib.GroupTheory.Subgroup.Basic", match_kind: "exact" },
};

const echo = <T>(x: T): T => JSON.parse(JSON.stringify(x)) as T;
const stripId = ({ id: _id, ...rest }: Record<string, unknown>) => rest;

const realFetch = globalThis.fetch;
beforeAll(() => {
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    throw new Error(`unexpected network fetch in test: ${String(input)}`);
  }) as typeof fetch;
});
afterAll(() => {
  globalThis.fetch = realFetch;
});

function setup() {
  const db = new DatabaseSync(":memory:");
  for (const f of MIGRATIONS) db.exec(readFileSync(resolve(MIGRATIONS_DIR, f), "utf8"));

  const now = Date.now();
  db.prepare(
    "INSERT INTO articles (slug, wikipedia_title, display_title, wikidata_qid, revid, annotations, version, created_at, updated_at) VALUES (?,?,?,?,?,?,1,?,?)",
  ).run(SLUG, "Id Test", "Id Test", null, REVID, JSON.stringify(SEED), now, now);
  db.prepare("INSERT INTO revisions (slug, user_id, annotations, comment, created_at) VALUES (?,NULL,?,?,?)").run(
    SLUG,
    JSON.stringify(SEED),
    "seed import",
    now,
  );
  const nowSec = Math.floor(now / 1000);
  const insUser = db.prepare("INSERT INTO users (id, name, email, role, created_at, updated_at) VALUES (?,?,?,?,?,?)");
  insUser.run("u-human", "Human Tester", "human@example.org", "user", nowSec, nowSec);
  insUser.run("pipeline", "WikiLean Pipeline", null, "bot", nowSec, nowSec);

  const env = {
    DB: makeD1(db),
    RENDER_CACHE: makeKV(),
    WP_HTML: makeKV({ [`wp:${SLUG}:${REVID}`]: WP_FIXTURE }),
    ASSETS: { fetch: async () => new Response("not found", { status: 404 }) },
    EDIT_LIMITER: { limit: async () => ({ success: true }) },
    AUTH_MODE: "dev",
    PIPELINE_TOKEN,
  } as unknown as Env;
  return { db, env };
}

function save(env: Env, body: Record<string, unknown>, asBot = false): Promise<Response> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (asBot) headers["Authorization"] = `Bearer ${PIPELINE_TOKEN}`;
  else {
    headers["Cookie"] = "wl_dev_user=u-human";
    headers["Origin"] = "http://localhost";
  }
  return Promise.resolve(
    app.request(`/api/article/${SLUG}`, { method: "POST", headers, body: JSON.stringify(body) }, env),
  );
}

function storedAnnotations(db: DatabaseSync): Array<Record<string, unknown>> {
  const row = db.prepare("SELECT annotations FROM articles WHERE slug = ?").get(SLUG) as { annotations: string };
  return JSON.parse(row.annotations) as Array<Record<string, unknown>>;
}

describe("save-path id validation", () => {
  it("malformed ids → 400 'invalid annotation id', no write", async () => {
    const { db, env } = setup();
    for (const bad of ["not-hex-12ch", "ABCDEF123456", "0123456789", "0123456789abcd", 42, ""]) {
      const res = await save(env, { annotations: [{ ...echo(STORED_NO_ID), id: bad }], base_version: 1 });
      expect(res.status).toBe(400);
      expect(((await res.json()) as { error: string }).error).toBe("invalid annotation id");
    }
    expect(storedAnnotations(db)).toEqual(SEED);
  });

  it("duplicate ids within the posted array → 400 'duplicate annotation id'", async () => {
    const { db, env } = setup();
    const dup = [
      { ...echo(STORED_WITH_ID) },
      { ...echo(NEW_ANNOTATION), id: STORED_WITH_ID.id }, // same id, different annotation
    ];
    const res = await save(env, { annotations: dup, base_version: 1 });
    expect(res.status).toBe(400);
    expect(((await res.json()) as { error: string }).error).toBe("duplicate annotation id");
    expect(storedAnnotations(db)).toEqual(SEED);
  });
});

describe("save-path lazy heal", () => {
  it("a posted annotation missing its id adopts the stored twin's id (sig match)", async () => {
    const { db, env } = setup();
    // The client dropped both ids; [0] is edited (note), [1] is echoed as-is.
    const posted = [
      { ...stripId(echo(STORED_WITH_ID)), note: "human-checked" },
      echo(STORED_NO_ID),
    ];
    const res = await save(env, { annotations: posted, base_version: 1 });
    expect(res.status).toBe(200);

    const stored = storedAnnotations(db);
    // Identity continuity: the stored id survives the round-trip.
    expect(stored[0].id).toBe(STORED_WITH_ID.id);
    expect(stored[0].note).toBe("human-checked");
    expect(stored[0].provenance).toBe("human"); // changed → stamped human
    // The straggler had no stored id → a fresh one is minted; provenance
    // stamping (which ran before the heal) still sees it as unchanged.
    expect(stored[1].id).toMatch(ANNOTATION_ID_RE);
    expect(stored[1].id).not.toBe(STORED_WITH_ID.id);
    expect(stored[1].provenance).toBe("ai");
  });

  it("a truly-new annotation gets a fresh unique id; echoed ids pass through untouched", async () => {
    const { db, env } = setup();
    const posted = [echo(STORED_WITH_ID), echo(STORED_NO_ID), echo(NEW_ANNOTATION)];
    const res = await save(env, { annotations: posted, base_version: 1 });
    expect(res.status).toBe(200);

    const stored = storedAnnotations(db);
    expect(stored[0].id).toBe(STORED_WITH_ID.id);
    const ids = stored.map((a) => a.id as string);
    for (const id of ids) expect(id).toMatch(ANNOTATION_ID_RE);
    expect(new Set(ids).size).toBe(ids.length);
    expect(stripId(stored[2])).toEqual(NEW_ANNOTATION);
  });

  it("adoption never duplicates: a second sig-twin without an id gets a fresh id, not the stored one", async () => {
    const { db, env } = setup();
    // [0] echoes the stored annotation WITH its id; [1] is a same-anchor copy
    // without one. Adopting the stored id for [1] would collide with [0], so
    // the heal must mint a fresh id instead.
    const posted = [echo(STORED_WITH_ID), { ...stripId(echo(STORED_WITH_ID)), label: "Centralizer (dup)" }];
    const res = await save(env, { annotations: posted, base_version: 1 });
    expect(res.status).toBe(200);

    const stored = storedAnnotations(db);
    expect(stored[0].id).toBe(STORED_WITH_ID.id);
    expect(stored[1].id).toMatch(ANNOTATION_ID_RE);
    expect(stored[1].id).not.toBe(STORED_WITH_ID.id);
  });

  it("bot writes are healed too, and echoing stored ids verbatim passes the 422 human check", async () => {
    const { db, env } = setup();
    // Human session save first → stored[0] becomes provenance 'human' and
    // every stored annotation now carries an id.
    expect(
      (await save(env, { annotations: [{ ...echo(STORED_WITH_ID), note: "verified" }, echo(STORED_NO_ID)], base_version: 1 }))
        .status,
    ).toBe(200);
    const afterHuman = storedAnnotations(db);
    expect(afterHuman[0].provenance).toBe("human");
    expect(afterHuman.every((a) => typeof a.id === "string" && ANNOTATION_ID_RE.test(a.id as string))).toBe(true);

    // The pipeline reads the article back, echoes everything verbatim (ids
    // included) and adds its own id-less annotation.
    const pushed = await save(env, { annotations: [...echo(afterHuman), NEW_ANNOTATION], base_version: 2 }, true);
    expect(pushed.status).toBe(200);

    const stored = storedAnnotations(db);
    expect(stored.slice(0, 2)).toEqual(afterHuman); // humans byte-identical, ids intact
    expect(stored[2].id).toMatch(ANNOTATION_ID_RE);
    expect(stripId(stored[2])).toEqual(NEW_ANNOTATION);

    // Echo-verbatim is enforced: a bot that drops a stored human annotation's
    // id fails deep-equality → 422.
    const dropped = echo(stored) as Array<Record<string, unknown>>;
    delete dropped[0].id;
    const rejected = await save(env, { annotations: dropped, base_version: 3 }, true);
    expect(rejected.status).toBe(422);
    expect(((await rejected.json()) as { missing: string[] }).missing).toEqual(["Centralizer"]);
  });
});

describe("anchorSig + anchors[] (multi-anchor annotations)", () => {
  // The signature is exactly [type, section, snippet, value, from] of the
  // SINGULAR anchor (the batch_annotate.py _anchor_sig twin). Extra anchor
  // fields and key order must not perturb it — sig stability is what lets a
  // round-tripped id-less annotation re-find its stored twin.
  it("sig is stable across key order and ignores non-signature anchor fields", () => {
    const a: AnnRecord = {
      anchor: { section: "Properties", snippet: "x", from_snippet: "ignored", to: "ignored", to_math: "ignored" },
    };
    const b: AnnRecord = { anchor: { snippet: "x", section: "Properties" } };
    expect(anchorSig(a)).toBe(anchorSig(b));
    expect(anchorSig(a)).toBe(JSON.stringify([null, "Properties", "x", null, null]));
  });

  it("TESTAGENT#1: anchors[0] feeds the signature when the singular anchor is absent", () => {
    // Conscious update of the old 'anchors[] not read' pin (TESTAGENT#1
    // lockstep contract, twinned with site _anchor_sig): a multi-anchor
    // annotation now signs with anchors[0]'s [type, section, snippet, value,
    // from], so sig-based matching CAN distinguish anchors[]-only annotations.
    const a: AnnRecord = { label: "A", anchors: [{ section: "S1", snippet: "one" }] };
    const b: AnnRecord = { label: "B", anchors: [{ section: "S2", snippet: "two" }] };
    expect(anchorSig(a)).toBe(JSON.stringify([null, "S1", "one", null, null]));
    expect(anchorSig(b)).toBe(JSON.stringify([null, "S2", "two", null, null]));
    expect(anchorSig(a)).not.toBe(anchorSig(b));
    // A plain-object singular `anchor` still wins over anchors[] — even an
    // EMPTY one (it does not fall through to the array).
    expect(anchorSig({ anchor: { section: "S0", snippet: "zero" }, anchors: [{ section: "S1", snippet: "one" }] })).toBe(
      JSON.stringify([null, "S0", "zero", null, null]),
    );
    expect(anchorSig({ anchor: {}, anchors: [{ section: "S1", snippet: "one" }] })).toBe(
      JSON.stringify([null, null, null, null, null]),
    );
  });

  it("TESTAGENT#1: non-object anchor values never crash — they fall through to anchors[] then all-null", () => {
    const allNull = JSON.stringify([null, null, null, null, null]);
    // string / number / array anchors are malformed but must not throw
    // (the Python twin used to AttributeError on these).
    expect(anchorSig({ anchor: "lead" })).toBe(allNull);
    expect(anchorSig({ anchor: 42 })).toBe(allNull);
    expect(anchorSig({ anchor: ["a", "b"] })).toBe(allNull);
    // …and a usable anchors[] is still consulted behind a malformed anchor.
    expect(anchorSig({ anchor: 42, anchors: [{ section: "S1", snippet: "one" }] })).toBe(
      JSON.stringify([null, "S1", "one", null, null]),
    );
    // Empty/garbage anchors[] → all-null.
    expect(anchorSig({ anchors: [] })).toBe(allNull);
    expect(anchorSig({ anchors: ["not-an-object"] })).toBe(allNull);
    expect(anchorSig({})).toBe(allNull);
  });

  it("heal: an id-stripped anchors[]-only annotation re-adopts its stored id (sig from anchors[0])", async () => {
    const { db, env } = setup();
    // TESTAGENT#1: anchors[0] is the signature now, so it's chosen to be
    // distinct from STORED_NO_ID's singular anchor ('normalizer of a
    // subgroup') — same five fields would mean the same sig.
    const MULTI = {
      status: "formalized",
      kind: "theorem",
      label: "Multi-anchor result",
      provenance: "ai",
      anchors: [
        { section: "Properties", snippet: "characteristic subgroup" },
        { section: "Properties", snippet: "normalizer of a subgroup" },
      ],
    };
    expect(
      (await save(env, { annotations: [echo(STORED_WITH_ID), echo(STORED_NO_ID), MULTI], base_version: 1 })).status,
    ).toBe(200);
    const after1 = storedAnnotations(db);
    const multiId = after1[2].id as string;
    expect(multiId).toMatch(ANNOTATION_ID_RE);

    // Client round-trips with the multi-anchor annotation's id stripped.
    const reposted = echo(after1) as Array<Record<string, unknown>>;
    delete reposted[2].id;
    expect((await save(env, { annotations: reposted, base_version: 2 })).status).toBe(200);
    const after2 = storedAnnotations(db);
    expect(after2[2].id).toBe(multiId); // identity continuity held
    // And the no-op round-trip added nothing to the event log.
    const n = (
      db.prepare("SELECT COUNT(*) AS n FROM annotation_events WHERE annotation_id = ?").get(multiId) as { n: number }
    ).n;
    expect(n).toBe(1); // just the original 'add'
  });

  it("TESTAGENT#1 (was PINNED HAZARD): distinct anchors[] sigs prevent the id-theft identity swap", async () => {
    // Conscious update: this test previously PINNED the hazard that any two
    // anchors[]-only annotations shared the all-null sig, letting an id-less
    // imposter steal a stored id. With anchors[0] feeding the signature, the
    // imposter's sig no longer matches the original's — each keeps (or is
    // minted) its own identity.
    const { db, env } = setup();
    const MULTI = {
      status: "formalized",
      label: "Original multi-anchor",
      provenance: "ai",
      anchors: [{ section: "Properties", snippet: "characteristic subgroup" }],
    };
    expect(
      (await save(env, { annotations: [echo(STORED_WITH_ID), echo(STORED_NO_ID), MULTI], base_version: 1 })).status,
    ).toBe(200);
    const multiId = storedAnnotations(db)[2].id as string;

    const imposter = {
      status: "not_formalized",
      label: "Imposter",
      provenance: "ai",
      anchors: [{ section: "(lead)", snippet: "completely different text" }],
    };
    const original = echo(storedAnnotations(db)[2]) as Record<string, unknown>;
    delete original.id;
    expect(
      (
        await save(env, {
          annotations: [echo(STORED_WITH_ID), echo(STORED_NO_ID), imposter, original],
          base_version: 2,
        })
      ).status,
    ).toBe(200);

    const after = storedAnnotations(db);
    expect(after[2].label).toBe("Imposter");
    expect(after[2].id).toMatch(ANNOTATION_ID_RE);
    expect(after[2].id).not.toBe(multiId); // no theft: the imposter got a fresh id
    expect(after[3].label).toBe("Original multi-anchor");
    expect(after[3].id).toBe(multiId); // the id-stripped original re-adopted its own id
  });

  it("F8: an id-stripped but otherwise unchanged annotation keeps its stored provenance", async () => {
    // stampProvenance now judges "changed" with BOTH provenance and id
    // stripped — dropping the id field alone must not launder 'ai' → 'human'.
    const { db, env } = setup();
    const posted = [stripId(echo(STORED_WITH_ID)), echo(STORED_NO_ID)];
    expect((await save(env, { annotations: posted, base_version: 1 })).status).toBe(200);
    const stored = storedAnnotations(db);
    expect(stored[0].id).toBe(STORED_WITH_ID.id); // heal re-adopted the id
    expect(stored[0].provenance).toBe("ai"); // F8: NOT stamped 'human'
  });
});

describe("backfill transform (assignAnnotationIds)", () => {
  const corpus = (): AnnRecord[] => [
    echo(STORED_WITH_ID) as AnnRecord,
    echo(STORED_NO_ID) as AnnRecord,
    { ...echo(NEW_ANNOTATION), id: "NOT-VALID-12c" } as AnnRecord, // malformed → replaced
  ];

  it("assigns valid unique ids to every annotation lacking one, preserving content and order", () => {
    const input = corpus();
    const { annotations, changed } = assignAnnotationIds(input);
    expect(changed).toBe(true);
    expect(annotations.length).toBe(3);
    expect(annotations[0]).toBe(input[0]); // already-valid id → same reference
    expect(annotations[0].id).toBe(STORED_WITH_ID.id);
    const ids = annotations.map((a) => a.id as string);
    for (const id of ids) expect(id).toMatch(ANNOTATION_ID_RE);
    expect(new Set(ids).size).toBe(3);
    expect(stripId(annotations[1])).toEqual(stripId(echo(STORED_NO_ID)));
    expect(stripId(annotations[2])).toEqual(echo(NEW_ANNOTATION));
    expect(input[1].id).toBeUndefined(); // pure: the input objects are untouched
  });

  it("is idempotent: a second run changes nothing", () => {
    const first = assignAnnotationIds(corpus());
    const second = assignAnnotationIds(first.annotations);
    expect(second.changed).toBe(false);
    expect(second.annotations).toEqual(first.annotations);
    expect(JSON.stringify(second.annotations)).toBe(JSON.stringify(first.annotations));
  });

  it("retries on collision with existing and just-assigned ids", () => {
    const sequence = [STORED_WITH_ID.id, "aaaaaaaaaaaa", "aaaaaaaaaaaa", "bbbbbbbbbbbb"];
    const genId = () => {
      const next = sequence.shift();
      if (next === undefined) throw new Error("generator exhausted");
      return next;
    };
    const { annotations, changed } = assignAnnotationIds(
      [echo(STORED_WITH_ID) as AnnRecord, echo(STORED_NO_ID) as AnnRecord, echo(NEW_ANNOTATION) as AnnRecord],
      genId,
    );
    expect(changed).toBe(true);
    // First draw collided with the existing id → retried to 'aaaa…'; third
    // annotation's first draw collided with the just-assigned 'aaaa…' →
    // retried to 'bbbb…'.
    expect(annotations.map((a) => a.id)).toEqual([STORED_WITH_ID.id, "aaaaaaaaaaaa", "bbbbbbbbbbbb"]);
  });

  it("empty input → unchanged", () => {
    const { annotations, changed } = assignAnnotationIds([]);
    expect(changed).toBe(false);
    expect(annotations).toEqual([]);
  });
});
