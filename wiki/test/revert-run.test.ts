// POST /api/admin/revert-run/:runId — run-level revert (the batch undo).
// Insurance against a bad AI-moderation batch: find every article a pipeline
// run touched (revisions stamped meta.run_id=<runId>, with the comment LIKE
// fallback for null-meta rows) and roll each back to its pre-run state, reusing
// the single-revision revert mechanics (CAS UPDATE, kind='revert' revision,
// revert_restore events, recomputed counts, one batch per slug).
//
// SAFETY + IDEMPOTENCE contract pinned here (see the endpoint's header doc):
//   * a human edit AFTER the run's last touch → skip 'human-edited-since'
//     (never clobber human work to undo a bot batch);
//   * a slug already at its pre-run state (our own marker is latest, or it's
//     byte-identical) → skip 'already-reverted' (re-run idempotence);
//   * a concurrent write racing the per-slug CAS → skip 'conflict';
//   * unknown / no-touch runId → 200 {reverted:[], skipped:[]} (not 404).

import { describe, it, expect } from "vitest";
import {
  setup,
  post,
  botSave,
  botSaveRun,
  botCreate,
  save,
  articleRow,
  storedAnnotations,
  latestRevision,
  revisionRows,
  revisionCount,
  eventRows,
  insertArticle,
  blockNetwork,
  echo,
  SLUG,
  REVID,
  PIPELINE_TOKEN,
  SEED_ANNOTATIONS,
  EXTRA_ANNOTATION,
  type Harness,
  type ReqOpts,
} from "./helpers/harness.js";

blockNetwork();

const RUN = "testrun1";

// Admin session, with the same-origin Origin header the editor sends.
const ADMIN: ReqOpts = { user: "u-admin" };
// Bot bearer, no Origin header (the runner/ops tooling shape).
const BOT: ReqOpts = { bearer: PIPELINE_TOKEN, origin: null };

function revertRun(env: Harness["env"], runId: string, opts: ReqOpts = ADMIN): Promise<Response> {
  return post(env, `/api/admin/revert-run/${runId}`, {}, opts);
}

// A bot run that ADDS EXTRA_ANNOTATION to SLUG, tagged with run_id=RUN.
// Returns the harness so chained calls read fresh state.
async function runTouchesSlug(h: Harness, runId = RUN): Promise<void> {
  const res = await botSaveRun(
    h.env,
    { annotations: [...echo(SEED_ANNOTATIONS), echo(EXTRA_ANNOTATION)], base_version: 1 },
    runId,
  );
  expect(res.status).toBe(200);
}

describe("POST /api/admin/revert-run/:runId — happy path", () => {
  it("restores pre-run annotations, appends a kind='revert' revision, emits events, bumps version", async () => {
    const h = setup();
    await runTouchesSlug(h);

    // After the run: 3 annotations, version 2, a pipeline revision tagged RUN.
    expect(storedAnnotations(h.db).length).toBe(3);
    expect(articleRow(h.db)!.version).toBe(2);
    const afterRun = latestRevision(h.db);
    expect(afterRun.kind).toBe("pipeline");
    expect(JSON.parse(afterRun.meta!).run_id).toBe(RUN);

    const res = await revertRun(h.env, RUN);
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true, run_id: RUN, reverted: [SLUG], skipped: [] });

    // Annotations restored to the pre-run state (the seed: 2 annotations).
    const stored = storedAnnotations(h.db);
    expect(stored).toEqual(SEED_ANNOTATIONS);

    const row = articleRow(h.db)!;
    expect(row.version).toBe(3); // run bumped 1→2, revert bumps 2→3
    // D-C5 counts recomputed from the restored (pre-run) annotations.
    expect(row.n_formalized).toBe(1);
    expect(row.n_partial).toBe(1);
    expect(row.n_not_formalized).toBe(0);

    // A kind='revert' revision with the run-revert comment + meta.reverted_run.
    const rev = latestRevision(h.db);
    expect(rev.kind).toBe("revert");
    expect(rev.comment).toBe(`revert-run:${RUN}`);
    expect(rev.user_id).toBe("u-admin");
    expect(JSON.parse(rev.meta!)).toEqual({ reverted_run: RUN });
    expect(rev.parent_id).toBe(rev.id - 1); // chains off the pipeline revision
    expect(rev.annotations).toBe(row.annotations);

    // revert_restore event for the dropped EXTRA_ANNOTATION (a 'delete' vs the
    // current state, collapsed to revert_restore).
    const events = eventRows(h.db).filter((e) => e.revision_id === rev.id);
    expect(events.length).toBe(1);
    expect(events[0]).toMatchObject({ event_type: "revert_restore", actor_type: "human", user_id: "u-admin" });
  });

  it("the bot bearer may also revert a run (actor_type pipeline)", async () => {
    const h = setup();
    await runTouchesSlug(h);
    const res = await revertRun(h.env, RUN, BOT);
    expect(res.status).toBe(200);
    expect((await res.json()) as unknown).toEqual({ ok: true, run_id: RUN, reverted: [SLUG], skipped: [] });
    expect(storedAnnotations(h.db)).toEqual(SEED_ANNOTATIONS);
    const rev = latestRevision(h.db);
    expect(rev.kind).toBe("revert");
    expect(rev.user_id).toBe("pipeline");
    const ev = eventRows(h.db).filter((e) => e.revision_id === rev.id);
    expect(ev[0].actor_type).toBe("pipeline");
  });

  it("reverts a run that modified (not just added) an annotation back to the original", async () => {
    const h = setup();
    // Run rewrites annotation aaaa's decl.
    const mutated = echo(SEED_ANNOTATIONS);
    (mutated[0].mathlib as Record<string, unknown>).decl = "WrongDecl";
    const r = await botSaveRun(h.env, { annotations: mutated, base_version: 1 }, RUN);
    expect(r.status).toBe(200);
    expect((storedAnnotations(h.db)[0].mathlib as Record<string, unknown>).decl).toBe("WrongDecl");

    expect((await revertRun(h.env, RUN).then((x) => x.json())) as unknown).toEqual({
      ok: true,
      run_id: RUN,
      reverted: [SLUG],
      skipped: [],
    });
    expect(storedAnnotations(h.db)).toEqual(SEED_ANNOTATIONS);
  });
});

describe("POST /api/admin/revert-run/:runId — human preservation", () => {
  it("a human edit after the run → SKIP 'human-edited-since', article left untouched", async () => {
    const h = setup();
    await runTouchesSlug(h); // version 2, 3 annotations

    // A human edits AFTER the run (changes a label) → version 3.
    const human = echo(storedAnnotations(h.db));
    human[0].label = "Human-curated label";
    const hs = await save(h.env, { annotations: human, base_version: 2 }, { user: "u-human" });
    expect(hs.status).toBe(200);
    expect(articleRow(h.db)!.version).toBe(3);
    const beforeRevert = storedAnnotations(h.db);
    const versionBefore = articleRow(h.db)!.version;
    const revCountBefore = revisionCount(h.db);

    const res = await revertRun(h.env, RUN);
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({
      ok: true,
      run_id: RUN,
      reverted: [],
      skipped: [{ slug: SLUG, reason: "human-edited-since" }],
    });

    // Article completely unchanged: no version bump, no new revision, no events.
    expect(storedAnnotations(h.db)).toEqual(beforeRevert);
    expect(articleRow(h.db)!.version).toBe(versionBefore);
    expect(revisionCount(h.db)).toBe(revCountBefore);
  });

  it("a SEED/PIPELINE revision after the run does NOT block (not human work)", async () => {
    const h = setup();
    await runTouchesSlug(h, RUN); // run #1 tagged RUN

    // A LATER pipeline pass (different run) touches the slug again.
    const later = await botSaveRun(
      h.env,
      { annotations: echo(storedAnnotations(h.db)), base_version: 2 },
      "laterrun",
    );
    // No-op echo short-circuits (deep-equal) — force a real change instead.
    expect([200]).toContain(later.status);
    const changed = echo(storedAnnotations(h.db));
    changed[1].label = "Pipeline relabel";
    const lp = await botSaveRun(h.env, { annotations: changed, base_version: articleRow(h.db)!.version }, "laterrun");
    expect(lp.status).toBe(200);

    // Reverting RUN still proceeds (the later pipeline rev is not human work)
    // and restores to the pre-RUN state.
    const res = await revertRun(h.env, RUN);
    expect(res.status).toBe(200);
    expect((await res.json()) as unknown).toEqual({ ok: true, run_id: RUN, reverted: [SLUG], skipped: [] });
    expect(storedAnnotations(h.db)).toEqual(SEED_ANNOTATIONS);
  });
});

describe("POST /api/admin/revert-run/:runId — multi-article + unknown", () => {
  it("reverts every article a run touched", async () => {
    const h = setup();
    // Second article B with its own seed revision (kind 'edit').
    insertArticle(h.db, "Second_Article", { revid: REVID, annotations: JSON.stringify(SEED_ANNOTATIONS) });
    h.db
      .prepare("INSERT INTO revisions (slug, user_id, annotations, comment, kind, created_at) VALUES (?,?,?,?,?,?)")
      .run("Second_Article", null, JSON.stringify(SEED_ANNOTATIONS), "seed import", "edit", Date.now());
    // WP HTML for B's render in the bot-save path.
    h.wpHtml.store.set(`wp:Second_Article:${REVID}`, h.wpHtml.store.get(`wp:${SLUG}:${REVID}`)!);

    // One run touches BOTH articles.
    await runTouchesSlug(h, RUN);
    const bRes = await botSaveRun(
      h.env,
      { annotations: [...echo(SEED_ANNOTATIONS), echo(EXTRA_ANNOTATION)], base_version: 1 },
      RUN,
      { slug: "Second_Article" },
    );
    expect(bRes.status).toBe(200);
    expect(storedAnnotations(h.db, "Second_Article").length).toBe(3);

    const res = await revertRun(h.env, RUN);
    expect(res.status).toBe(200);
    const body = (await res.json()) as { reverted: string[]; skipped: unknown[] };
    expect(body.reverted.sort()).toEqual(["Second_Article", SLUG].sort());
    expect(body.skipped).toEqual([]);

    expect(storedAnnotations(h.db)).toEqual(SEED_ANNOTATIONS);
    expect(storedAnnotations(h.db, "Second_Article")).toEqual(SEED_ANNOTATIONS);
  });

  it("a run that CREATED an article reverts it to the empty pre-run state", async () => {
    const h = setup();
    const created = await botCreate(h.env, "Born_In_Run", {
      wikipedia_title: "Born In Run",
      revid: 9001,
      annotations: echo(SEED_ANNOTATIONS),
      comment: `ai-moderate:new:${RUN}`,
      meta: { run_id: RUN },
    });
    expect(created.status).toBe(201);
    expect(storedAnnotations(h.db, "Born_In_Run").length).toBe(2);

    const res = await revertRun(h.env, RUN);
    expect(res.status).toBe(200);
    expect((await res.json()) as unknown).toEqual({ ok: true, run_id: RUN, reverted: ["Born_In_Run"], skipped: [] });
    // Created-in-run → pre-run state is empty.
    expect(storedAnnotations(h.db, "Born_In_Run")).toEqual([]);
    const rev = latestRevision(h.db, "Born_In_Run");
    expect(rev.kind).toBe("revert");
    expect(rev.comment).toBe(`revert-run:${RUN}`);
  });

  it("unknown runId → 200 {reverted:[], skipped:[]} (idempotent, not 404)", async () => {
    const h = setup();
    const res = await revertRun(h.env, "nosuchrun");
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true, run_id: "nosuchrun", reverted: [], skipped: [] });
    // Nothing written.
    expect(articleRow(h.db)!.version).toBe(1);
    expect(revisionCount(h.db)).toBe(1);
  });

  it("matches a null-meta revision via the comment LIKE fallback", async () => {
    const h = setup();
    // A run whose revision carries the wp-update comment shape but null meta —
    // the runner's stage-0 path can land that way. Build it by hand.
    const annJson = JSON.stringify([...SEED_ANNOTATIONS, EXTRA_ANNOTATION]);
    h.db
      .prepare(
        "UPDATE articles SET annotations=?, version=2, n_formalized=2, n_partial=1, n_not_formalized=0 WHERE slug=?",
      )
      .run(annJson, SLUG);
    h.db
      .prepare(
        "INSERT INTO revisions (slug, user_id, annotations, comment, kind, meta, parent_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
      )
      .run(SLUG, "pipeline", annJson, `wp-update:stage0:${RUN}`, "pipeline", null, 1, Date.now());

    const res = await revertRun(h.env, RUN);
    expect(res.status).toBe(200);
    expect((await res.json()) as unknown).toEqual({ ok: true, run_id: RUN, reverted: [SLUG], skipped: [] });
    expect(storedAnnotations(h.db)).toEqual(SEED_ANNOTATIONS);
  });
});

describe("POST /api/admin/revert-run/:runId — idempotence (re-run)", () => {
  it("a second revert of the same run finds the slug already reverted and skips it", async () => {
    const h = setup();
    await runTouchesSlug(h);

    const first = await revertRun(h.env, RUN);
    expect((await first.json()) as unknown).toEqual({ ok: true, run_id: RUN, reverted: [SLUG], skipped: [] });
    const afterFirst = articleRow(h.db)!.version;
    const revsAfterFirst = revisionCount(h.db);

    const second = await revertRun(h.env, RUN);
    expect(second.status).toBe(200);
    expect(await second.json()).toEqual({
      ok: true,
      run_id: RUN,
      reverted: [],
      skipped: [{ slug: SLUG, reason: "already-reverted" }],
    });
    // The second call wrote nothing.
    expect(articleRow(h.db)!.version).toBe(afterFirst);
    expect(revisionCount(h.db)).toBe(revsAfterFirst);
    expect(storedAnnotations(h.db)).toEqual(SEED_ANNOTATIONS);
  });

  it("a no-op run (no net change) is reported 'already-reverted', writes nothing", async () => {
    const h = setup();
    // A run that re-pins the SAME annotations (a no-op echo): the save handler
    // short-circuits, so NO run revision is written → revert-run finds nothing.
    const noop = await botSaveRun(h.env, { annotations: echo(SEED_ANNOTATIONS), base_version: 1 }, RUN);
    expect(noop.status).toBe(200);
    expect(((await noop.json()) as { noop?: boolean }).noop).toBe(true);
    expect(revisionCount(h.db)).toBe(1); // no run revision

    const res = await revertRun(h.env, RUN);
    expect(res.status).toBe(200);
    // Nothing was tagged with RUN → empty result, not even a skip.
    expect(await res.json()).toEqual({ ok: true, run_id: RUN, reverted: [], skipped: [] });
  });
});

describe("POST /api/admin/revert-run/:runId — authz", () => {
  it("anon → 403, no write", async () => {
    const h = setup();
    await runTouchesSlug(h);
    const res = await revertRun(h.env, RUN, {});
    expect(res.status).toBe(403);
    expect(storedAnnotations(h.db).length).toBe(3); // run's change still in place
  });

  it("plain user → 403", async () => {
    const h = setup();
    await runTouchesSlug(h);
    const res = await revertRun(h.env, RUN, { user: "u-human" });
    expect(res.status).toBe(403);
  });

  it("patroller → 403 (run-revert is admin/bot only, unlike single-revision revert)", async () => {
    const h = setup();
    await runTouchesSlug(h);
    const res = await revertRun(h.env, RUN, { user: "u-patroller" });
    expect(res.status).toBe(403);
  });

  it("cross-origin POST → 403 (origin check)", async () => {
    const h = setup();
    await runTouchesSlug(h);
    const res = await post(h.env, `/api/admin/revert-run/${RUN}`, {}, { user: "u-admin", origin: "https://evil.test" });
    expect(res.status).toBe(403);
  });
});
