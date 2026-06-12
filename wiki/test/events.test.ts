// annotation_events — the experiment dataset's integrity (Wave D, contract
// D-C3). Every row asserted here is produced by a REAL API call against the
// real app; the suite pins the exact rows each write path emits, that every
// row keys the right revision, and — critically for dataset cleanliness —
// that failed writes (400/403/404/409/413/422/429) emit ZERO rows.

import { describe, it, expect } from "vitest";
import {
  setup,
  save,
  botSave,
  botCreate,
  post,
  get,
  articleRow,
  storedAnnotations,
  revisionCount,
  latestRevision,
  eventRows,
  statusRecount,
  blockNetwork,
  echo,
  SLUG,
  ID_RE,
  SEED_ANNOTATIONS,
  EXTRA_ANNOTATION,
  type Harness,
  type EventRow,
} from "./helpers/harness.js";

blockNetwork();

const ID0 = "aaaaaaaaaaaa"; // SEED_ANNOTATIONS[0] (formalized, ai, mathlib.decl AddCommGroup)
const ID1 = "bbbbbbbbbbbb"; // SEED_ANNOTATIONS[1] (partial, ai)

const parseChanges = (e: EventRow): Record<string, unknown> | null =>
  e.field_changes === null ? null : (JSON.parse(e.field_changes) as Record<string, unknown>);

describe("session save events", () => {
  it("adding an annotation emits exactly one 'add' (actor human, no field_changes)", async () => {
    const { db, env } = setup();
    const posted = [...echo(SEED_ANNOTATIONS), echo(EXTRA_ANNOTATION)];
    expect((await save(env, { annotations: posted, base_version: 1 }, { user: "u-human" })).status).toBe(200);

    const events = eventRows(db);
    expect(events.length).toBe(1);
    const stored = storedAnnotations(db);
    expect(stored[2].id).toMatch(ID_RE);
    expect(events[0]).toMatchObject({
      slug: SLUG,
      annotation_id: stored[2].id,
      event_type: "add",
      actor_type: "human",
      user_id: "u-human",
      field_changes: null,
      revision_id: latestRevision(db).id,
    });
  });

  it("a modify carries field-level old/new values, nested mathlib.decl as a dotted path", async () => {
    const { db, env } = setup();
    const posted = echo(SEED_ANNOTATIONS) as Array<Record<string, unknown>>;
    (posted[0].mathlib as Record<string, unknown>).decl = "CommGroup";
    posted[0].note = "prefer the multiplicative formulation";
    expect((await save(env, { annotations: posted, base_version: 1 }, { user: "u-human" })).status).toBe(200);

    const events = eventRows(db);
    expect(events.length).toBe(1);
    expect(events[0]).toMatchObject({
      annotation_id: ID0,
      event_type: "modify",
      actor_type: "human",
      user_id: "u-human",
    });
    // Exact field_changes: the nested decl under its dotted path, the new
    // note with null as the absent-side, and the server-side provenance stamp
    // (ai → human, because the annotation changed).
    expect(parseChanges(events[0])).toEqual({
      "mathlib.decl": ["AddCommGroup", "CommGroup"],
      note: [null, "prefer the multiplicative formulation"],
      provenance: ["ai", "human"],
    });
  });

  it("a tombstone (status → rejected) is one 'reject' event — never modify+delete", async () => {
    const { db, env } = setup();
    const posted = echo(SEED_ANNOTATIONS) as Array<Record<string, unknown>>;
    posted[1].status = "rejected";
    expect((await save(env, { annotations: posted, base_version: 1 }, { user: "u-human" })).status).toBe(200);

    const events = eventRows(db);
    expect(events.length).toBe(1);
    expect(events[0]).toMatchObject({ annotation_id: ID1, event_type: "reject", actor_type: "human" });
    expect(parseChanges(events[0])).toEqual({
      provenance: ["ai", "human"],
      status: ["partial", "rejected"],
    });
    // The annotation is tombstoned in place, not deleted.
    expect(storedAnnotations(db).length).toBe(2);
  });

  it("a byte-identical re-post is short-circuited: zero events, no version bump, no revision (TESTAGENT#2 no-op saves)", async () => {
    const { db, env } = setup();
    // Echo exactly what is stored (the seed already carries ids).
    const posted = echo(storedAnnotations(db));
    const res = await save(env, { annotations: posted, base_version: 1 }, { user: "u-human" });
    expect(res.status).toBe(200);
    // TESTAGENT#2 conscious update (was pinned as version-bumping): the no-op
    // save now returns {noop:true} and writes NOTHING — no article UPDATE, no
    // revision, no events. (Bot no-ops still stamp moderation_state; covered
    // in work-queue.test.ts.)
    expect(await res.json()).toEqual({ ok: true, noop: true, version: 1 });
    expect(eventRows(db).length).toBe(0);
    expect(articleRow(db)!.version).toBe(1);
    expect(revisionCount(db)).toBe(1);
    expect(storedAnnotations(db)).toEqual(SEED_ANNOTATIONS);
  });
});

describe("endorse events", () => {
  it("an endorse is a single 'endorse' event (no modify), with the provenance flip as field_changes", async () => {
    const { db, env } = setup();
    expect(
      (await save(env, { action: "endorse", annotation_id: ID0, base_version: 1 }, { user: "u-human" })).status,
    ).toBe(200);
    const events = eventRows(db);
    expect(events.length).toBe(1);
    expect(events[0]).toMatchObject({
      annotation_id: ID0,
      event_type: "endorse",
      actor_type: "human",
      user_id: "u-human",
      revision_id: latestRevision(db).id,
    });
    expect(parseChanges(events[0])).toEqual({ provenance: ["ai", "human"] });
    expect(events.filter((e) => e.event_type === "modify").length).toBe(0);
  });
});

describe("pipeline (bot) events", () => {
  it("a bot dropping an ai annotation emits one 'delete' with actor pipeline", async () => {
    const { db, env } = setup();
    // Drop SEED_ANNOTATIONS[1] (provenance ai → allowed for bots).
    expect((await botSave(env, { annotations: [echo(SEED_ANNOTATIONS[0])], base_version: 1 })).status).toBe(200);

    const events = eventRows(db);
    expect(events.length).toBe(1);
    expect(events[0]).toMatchObject({
      annotation_id: ID1,
      event_type: "delete",
      actor_type: "pipeline",
      user_id: "pipeline",
      field_changes: null,
      revision_id: latestRevision(db).id,
    });
  });

  it("a bot modify is attributed to the auth seam (actor pipeline), never to client-claimed provenance", async () => {
    const { db, env } = setup();
    const posted = echo(SEED_ANNOTATIONS) as Array<Record<string, unknown>>;
    posted[0].note = "re-anchored against the new revision";
    expect((await botSave(env, { annotations: posted, base_version: 1 })).status).toBe(200);

    const events = eventRows(db);
    expect(events.length).toBe(1);
    expect(events[0]).toMatchObject({ annotation_id: ID0, event_type: "modify", actor_type: "pipeline" });
    // Bot provenance passes through verbatim → no provenance entry in the diff.
    expect(parseChanges(events[0])).toEqual({ note: [null, "re-anchored against the new revision"] });
  });
});

describe("create events", () => {
  it("create emits one 'add' per annotation, all keyed to the create revision", async () => {
    const { db, env } = setup();
    const res = await botCreate(env, "Events_Created", {
      wikipedia_title: "Events Created",
      revid: 999, // F16: revid required for pipeline creates
      annotations: [
        { status: "formalized", label: "one", provenance: "ai" },
        { status: "partial", label: "two", provenance: "ai" },
      ],
    });
    expect(res.status).toBe(201);

    const events = eventRows(db, "Events_Created");
    const rev = latestRevision(db, "Events_Created");
    const stored = storedAnnotations(db, "Events_Created");
    expect(events.length).toBe(2);
    expect(new Set(events.map((e) => e.annotation_id))).toEqual(new Set(stored.map((a) => a.id as string)));
    for (const e of events) {
      expect(e).toMatchObject({
        event_type: "add",
        actor_type: "pipeline",
        user_id: "pipeline",
        field_changes: null,
        revision_id: rev.id,
      });
    }
  });
});

describe("revert events", () => {
  it("revert emits 'revert_restore' for changed annotations ONLY, with field detail where the differ has it", async () => {
    const { db, env } = setup();
    const seedRevId = latestRevision(db).id;

    // One modify (note on ID0) + one add (EXTRA), ID1 untouched.
    const posted = [
      { ...echo(SEED_ANNOTATIONS[0]), note: "human note" },
      echo(SEED_ANNOTATIONS[1]),
      echo(EXTRA_ANNOTATION),
    ];
    expect((await save(env, { annotations: posted, base_version: 1 }, { user: "u-human" })).status).toBe(200);
    const addedId = storedAnnotations(db)[2].id as string;
    const eventsBefore = eventRows(db).length;

    const res = await post(env, `/api/article/${SLUG}/revert/${seedRevId}`, {}, { user: "u-patroller" });
    expect(res.status).toBe(200);

    const revertRev = latestRevision(db);
    const revertEvents = eventRows(db).slice(eventsBefore);
    // ID1 never changed → no event for it. ID0 (un-modified back) and the
    // added annotation (deleted by the revert) each get one revert_restore.
    expect(revertEvents.length).toBe(2);
    for (const e of revertEvents) {
      expect(e).toMatchObject({ event_type: "revert_restore", actor_type: "human", user_id: "u-patroller", revision_id: revertRev.id });
    }
    const byId = new Map(revertEvents.map((e) => [e.annotation_id, e]));
    expect(new Set(byId.keys())).toEqual(new Set([ID0, addedId]));
    // The restored-modify carries the field detail (note removed, stamp undone)…
    expect(parseChanges(byId.get(ID0)!)).toEqual({
      note: ["human note", null],
      provenance: ["human", "ai"],
    });
    // …the underlying delete has none.
    expect(byId.get(addedId)!.field_changes).toBeNull();
  });
});

describe("field_changes 4KB truncation", () => {
  it("over-cap diffs keep early fields, drop the rest, and carry the _truncated marker", async () => {
    const { db, env } = setup();
    // Establish 2000-char old values (at MAX_TEXT_LEN, the largest legal field).
    const first = echo(SEED_ANNOTATIONS) as Array<Record<string, unknown>>;
    first[0] = { ...first[0], label: "c".repeat(2000), note: "n".repeat(2000), proof_note: "p".repeat(2000) };
    expect((await save(env, { annotations: first, base_version: 1 }, { user: "u-human" })).status).toBe(200);
    const eventsBefore = eventRows(db).length;

    // Change all three → three ~4KB field entries; only the first fits.
    const second = echo(storedAnnotations(db)) as Array<Record<string, unknown>>;
    second[0] = { ...second[0], label: "C".repeat(2000), note: "N".repeat(2000), proof_note: "P".repeat(2000) };
    expect((await save(env, { annotations: second, base_version: 2 }, { user: "u-human" })).status).toBe(200);

    const events = eventRows(db).slice(eventsBefore);
    expect(events.length).toBe(1);
    expect(events[0].event_type).toBe("modify");
    const raw = events[0].field_changes!;
    expect(raw.length).toBeLessThanOrEqual(4 * 1024);
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    expect(parsed._truncated).toBe(true);
    // Fields are dropped in (sorted) field order until the JSON fits: the
    // first entry (label) survives, note/proof_note are cut.
    expect(parsed.label).toEqual(["c".repeat(2000), "C".repeat(2000)]);
    expect("note" in parsed).toBe(false);
    expect("proof_note" in parsed).toBe(false);
  });
});

describe("revision linkage", () => {
  it("after a mixed write sequence, every event references a real revision of the same slug and timestamp", async () => {
    const { db, env } = setup();
    // save (add+modify) → endorse → bot save (add) → revert.
    const v1 = [{ ...echo(SEED_ANNOTATIONS[0]), note: "x" }, echo(SEED_ANNOTATIONS[1]), echo(EXTRA_ANNOTATION)];
    expect((await save(env, { annotations: v1, base_version: 1 }, { user: "u-human" })).status).toBe(200);
    expect(
      (await save(env, { action: "endorse", annotation_id: ID1, base_version: 2 }, { user: "u-human" })).status,
    ).toBe(200);
    const cur = storedAnnotations(db);
    expect(
      (
        await botSave(env, {
          annotations: [...echo(cur), { status: "partial", label: "bot addition", provenance: "ai" }],
          base_version: 3,
        })
      ).status,
    ).toBe(200);
    expect((await post(env, `/api/article/${SLUG}/revert/1`, {}, { user: "u-patroller" })).status).toBe(200);

    const events = eventRows(db);
    expect(events.length).toBeGreaterThanOrEqual(6);
    const revs = db
      .prepare("SELECT id, slug, created_at FROM revisions")
      .all() as Array<{ id: number; slug: string; created_at: number }>;
    const revById = new Map(revs.map((r) => [r.id, r]));
    for (const e of events) {
      const rev = revById.get(e.revision_id);
      expect(rev, `event ${e.id} (${e.event_type}) must reference a real revision`).toBeDefined();
      expect(rev!.slug).toBe(SLUG);
      // The event is emitted in the same write as its revision.
      expect(e.created_at).toBe(rev!.created_at);
    }
  });
});

describe("failed writes emit ZERO events (dataset cleanliness)", () => {
  async function expectNoNewEvents(h: Harness, fn: () => Promise<Response>, status: number): Promise<void> {
    const before = eventRows(h.db).length;
    const res = await fn();
    expect(res.status).toBe(status);
    expect(eventRows(h.db).length).toBe(before);
  }

  it("409 stale base_version (session and bot)", async () => {
    const h = setup();
    await expectNoNewEvents(
      h,
      () => save(h.env, { annotations: [echo(EXTRA_ANNOTATION)], base_version: 99 }, { user: "u-human" }),
      409,
    );
    await expectNoNewEvents(h, () => botSave(h.env, { annotations: [], base_version: 99 }), 409);
  });

  it("422 human-annotation-lost bot write", async () => {
    const h = setup();
    // Make ID0 human first (this DOES emit one endorse event — baseline moves).
    expect(
      (await save(h.env, { action: "endorse", annotation_id: ID0, base_version: 1 }, { user: "u-human" })).status,
    ).toBe(200);
    await expectNoNewEvents(
      h,
      () => botSave(h.env, { annotations: [echo(storedAnnotations(h.db)[1])], base_version: 2 }),
      422,
    );
  });

  it("400 validation failures: bad status, oversized field, duplicate ids, bad annotation_id", async () => {
    const h = setup();
    await expectNoNewEvents(
      h,
      () => save(h.env, { annotations: [{ status: "bogus" }], base_version: 1 }, { user: "u-human" }),
      400,
    );
    await expectNoNewEvents(
      h,
      () => save(h.env, { annotations: [{ status: "formalized", note: "x".repeat(2001) }], base_version: 1 }, { user: "u-human" }),
      400,
    );
    const dup = [echo(SEED_ANNOTATIONS[0]), { ...echo(EXTRA_ANNOTATION), id: ID0 }];
    await expectNoNewEvents(
      h,
      () => save(h.env, { annotations: dup, base_version: 1 }, { user: "u-human" }),
      400,
    );
    await expectNoNewEvents(
      h,
      () => save(h.env, { action: "endorse", annotation_id: "nope", base_version: 1 }, { user: "u-human" }),
      400,
    );
  });

  it("413 payload too large", async () => {
    const h = setup();
    const huge = Array.from({ length: 2001 }, () => ({ status: "formalized" as const }));
    await expectNoNewEvents(
      h,
      () => save(h.env, { annotations: huge, base_version: 1 }, { user: "u-human" }),
      413,
    );
  });

  it("404 paths: unknown slug, unknown endorse target, unknown revert revision", async () => {
    const h = setup();
    await expectNoNewEvents(
      h,
      () => post(h.env, "/api/article/No_Such_Article", { annotations: [], base_version: 1 }, { user: "u-human" }),
      404,
    );
    await expectNoNewEvents(
      h,
      () => save(h.env, { action: "endorse", annotation_id: "ffffffffffff", base_version: 1 }, { user: "u-human" }),
      404,
    );
    await expectNoNewEvents(h, () => post(h.env, `/api/article/${SLUG}/revert/99999`, {}, { user: "u-patroller" }), 404);
  });

  it("409 create-on-existing-slug and 403 cross-origin", async () => {
    const h = setup();
    // revid present (F16) so the request reaches the existence check (409).
    await expectNoNewEvents(h, () => botCreate(h.env, SLUG, { wikipedia_title: "X", revid: 999, annotations: [] }), 409);
    await expectNoNewEvents(
      h,
      () =>
        save(
          h.env,
          { annotations: [echo(EXTRA_ANNOTATION)], base_version: 1 },
          { user: "u-human", origin: "https://evil.example" },
        ),
      403,
    );
  });

  it("429 rate-limited", async () => {
    const h = setup({ limiterAllows: false });
    await expectNoNewEvents(
      h,
      () => save(h.env, { annotations: [echo(EXTRA_ANNOTATION)], base_version: 1 }, { user: "u-human" }),
      429,
    );
  });
});

describe("per-status counts stay consistent (D-C5 recount)", () => {
  function expectCountsMatch(h: Harness, slug = SLUG): void {
    const row = articleRow(h.db, slug)!;
    const recount = statusRecount(h.db, slug);
    expect({
      n_formalized: row.n_formalized,
      n_partial: row.n_partial,
      n_not_formalized: row.n_not_formalized,
    }).toEqual(recount);
  }

  it("columns match an independent recount after every mutation kind", async () => {
    const h = setup();

    // Session save: mixed statuses + a tombstone.
    const v1 = [
      { ...echo(SEED_ANNOTATIONS[0]), status: "not_formalized" },
      { ...echo(SEED_ANNOTATIONS[1]), status: "rejected" }, // excluded from counts
      echo(EXTRA_ANNOTATION), // formalized
    ];
    expect((await save(h.env, { annotations: v1, base_version: 1 }, { user: "u-human" })).status).toBe(200);
    expectCountsMatch(h);
    expect(articleRow(h.db)!.n_formalized).toBe(1);
    expect(articleRow(h.db)!.n_partial).toBe(0);
    expect(articleRow(h.db)!.n_not_formalized).toBe(1);

    // Endorse (no status change, counts re-written all the same).
    const endorseTarget = storedAnnotations(h.db)[2].id as string;
    expect(
      (await save(h.env, { action: "endorse", annotation_id: endorseTarget, base_version: 2 }, { user: "u-human" })).status,
    ).toBe(200);
    expectCountsMatch(h);

    // Bot save: echo humans, add one partial annotation.
    const cur = storedAnnotations(h.db);
    expect(
      (
        await botSave(h.env, {
          annotations: [...echo(cur), { status: "partial", label: "bot addition", provenance: "ai" }],
          base_version: 3,
        })
      ).status,
    ).toBe(200);
    expectCountsMatch(h);
    expect(articleRow(h.db)!.n_partial).toBe(1);

    // Revert to the seed snapshot.
    expect((await post(h.env, `/api/article/${SLUG}/revert/1`, {}, { user: "u-patroller" })).status).toBe(200);
    expectCountsMatch(h);
    expect(articleRow(h.db)!.n_formalized).toBe(1);
    expect(articleRow(h.db)!.n_partial).toBe(1);

    // Create.
    expect(
      (
        await botCreate(h.env, "Counts_Created", {
          wikipedia_title: "Counts Created",
          revid: 999, // F16: revid required for pipeline creates
          annotations: [
            { status: "formalized", provenance: "ai" },
            { status: "rejected", provenance: "human" },
          ],
        })
      ).status,
    ).toBe(201);
    expectCountsMatch(h, "Counts_Created");
  });
});
