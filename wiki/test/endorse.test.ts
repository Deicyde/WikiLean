// POST /api/article/:slug {action:'endorse'} — the human-agreement signal
// (Wave D, contract D-C2). Replaces the editor's old provenance-flip-and-save,
// which stampProvenance now reverts by design: an endorse flips ONE stored
// annotation's provenance to 'human' server-side, content untouched, and logs
// the cleanest agreement event we have.

import { describe, it, expect } from "vitest";
import {
  setup,
  save,
  botSave,
  articleRow,
  storedAnnotations,
  revisionCount,
  latestRevision,
  moderationRow,
  eventRows,
  blockNetwork,
  echo,
  SEED_ANNOTATIONS,
} from "./helpers/harness.js";

blockNetwork();

const TARGET_ID = "aaaaaaaaaaaa"; // SEED_ANNOTATIONS[0], provenance 'ai'

describe("POST /api/article/:slug (endorse)", () => {
  it("happy path: provenance flips to human, content untouched, version bumps, event emitted", async () => {
    const { db, env } = setup();
    const res = await save(
      env,
      { action: "endorse", annotation_id: TARGET_ID, base_version: 1 },
      { user: "u-human" },
    );
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true, version: 2 });

    const row = articleRow(db)!;
    expect(row.version).toBe(2);
    const stored = storedAnnotations(db);
    // Only provenance changed; every other byte of the annotation is intact.
    expect(stored[0]).toEqual({ ...SEED_ANNOTATIONS[0], provenance: "human" });
    expect(stored[1]).toEqual(SEED_ANNOTATIONS[1]);
    // D-C5: counts written in the same UPDATE.
    expect(row.n_formalized).toBe(1);
    expect(row.n_partial).toBe(1);
    expect(row.n_not_formalized).toBe(0);

    const rev = latestRevision(db);
    expect(rev.user_id).toBe("u-human");
    expect(rev.kind).toBe("edit");
    expect(rev.comment).toBe(`endorse:${TARGET_ID}`);
    expect(rev.annotations).toBe(row.annotations);
    expect(rev.parent_id).toBe(rev.id - 1);

    const events = eventRows(db);
    expect(events.length).toBe(1);
    expect(events[0]).toMatchObject({
      revision_id: rev.id,
      annotation_id: TARGET_ID,
      event_type: "endorse",
      actor_type: "human",
      user_id: "u-human",
    });
    expect(JSON.parse(events[0].field_changes ?? "null")).toEqual({ provenance: ["ai", "human"] });

    // Session write: no moderation_state bookkeeping (that's bot-save only).
    expect(moderationRow(db)).toBeUndefined();
  });

  it("the endorsed annotation is then protected from bot writes (provenance human)", async () => {
    const { db, env } = setup();
    expect(
      (await save(env, { action: "endorse", annotation_id: TARGET_ID, base_version: 1 }, { user: "u-human" }))
        .status,
    ).toBe(200);
    // A bot pass that drops or rewords the endorsed annotation now 422s.
    const stored = storedAnnotations(db);
    const dropping = await botSave(env, { annotations: [echo(stored[1])], base_version: 2 });
    expect(dropping.status).toBe(422);
    expect(((await dropping.json()) as { missing: string[] }).missing).toEqual(["Abelian group"]);
  });

  it("stale base_version → 409 with current state, no write", async () => {
    const { db, env } = setup();
    const res = await save(
      env,
      { action: "endorse", annotation_id: TARGET_ID, base_version: 99 },
      { user: "u-human" },
    );
    expect(res.status).toBe(409);
    const body = (await res.json()) as { error: string; version: number; annotations: unknown };
    expect(body.error).toBe("stale");
    expect(body.version).toBe(1);
    expect(body.annotations).toEqual(SEED_ANNOTATIONS);
    expect(articleRow(db)!.version).toBe(1);
    expect(revisionCount(db)).toBe(1);
    expect(eventRows(db).length).toBe(0);
  });

  it("unknown annotation id → 404 {error:'annotation not found'}, no write", async () => {
    const { db, env } = setup();
    const res = await save(
      env,
      { action: "endorse", annotation_id: "ffffffffffff", base_version: 1 },
      { user: "u-human" },
    );
    expect(res.status).toBe(404);
    expect(await res.json()).toEqual({ error: "annotation not found" });
    expect(articleRow(db)!.version).toBe(1);
    expect(revisionCount(db)).toBe(1);
  });

  it("bots may not endorse (403); anonymous gets 401", async () => {
    const { db, env } = setup();
    const bot = await botSave(env, { action: "endorse", annotation_id: TARGET_ID, base_version: 1 });
    expect(bot.status).toBe(403);

    const anon = await save(env, { action: "endorse", annotation_id: TARGET_ID, base_version: 1 });
    expect(anon.status).toBe(401);
    expect(articleRow(db)!.version).toBe(1);
  });

  it("validation: malformed annotation_id → 400; missing base_version → 400; unknown action → 400", async () => {
    const { db, env } = setup();
    const badId = await save(
      env,
      { action: "endorse", annotation_id: "not-hex!", base_version: 1 },
      { user: "u-human" },
    );
    expect(badId.status).toBe(400);

    const noBase = await save(env, { action: "endorse", annotation_id: TARGET_ID }, { user: "u-human" });
    expect(noBase.status).toBe(400);

    const badAction = await save(
      env,
      { action: "bless", annotation_id: TARGET_ID, base_version: 1 },
      { user: "u-human" },
    );
    expect(badAction.status).toBe(400);
    expect(articleRow(db)!.version).toBe(1);
    expect(eventRows(db).length).toBe(0);
  });
});
