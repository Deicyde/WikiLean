// Propose-then-approve (docs/propose-then-approve.md): a bot review may PROPOSE
// an update to a human annotation (findLostHuman still preserves it verbatim);
// Jack approves or rejects. Proposals live inert in moderation_state.proposal
// and never touch articles.annotations until approved.

import { describe, it, expect } from "vitest";
import {
  setup,
  save,
  botSave,
  get,
  articleRow,
  storedAnnotations,
  latestRevision,
  eventRows,
  blockNetwork,
  echo,
  SLUG,
} from "./helpers/harness.js";
import {
  mergeProposals,
  applyProposalFields,
  fieldsSig,
  parsePending,
  type PendingProposal,
} from "../src/proposals.js";

blockNetwork();

// moderationRow() in the shared harness does not SELECT the proposal column;
// read it directly here.
function pending(db: import("node:sqlite").DatabaseSync) {
  const r = db.prepare("SELECT proposal FROM moderation_state WHERE slug = ?").get(SLUG) as { proposal: string | null } | undefined;
  return parsePending(r?.proposal);
}

const HUMAN_ID = "bbbbbbbbbbbb"; // SEED_ANNOTATIONS[1], status 'partial'
const PROP_FIELDS = {
  status: "formalized",
  mathlib: { decl: "Foo.bar", module: "Mathlib.Foo", match_kind: "exact" },
};

// Make HUMAN_ID a human annotation, then have the bot store a proposal against
// it. Returns the pending proposal + the current version.
async function seedProposal(env: import("../src/env.js").Env) {
  const e1 = await save(env, { action: "endorse", annotation_id: HUMAN_ID, base_version: 1 }, { user: "u-human" });
  expect(e1.status).toBe(200); // now version 2, HUMAN_ID provenance 'human'
  return e1;
}

async function storeProposal(env: import("../src/env.js").Env, db: import("node:sqlite").DatabaseSync, baseVersion: number) {
  const current = storedAnnotations(db).map(echo);
  const res = await botSave(env, {
    annotations: current, // echoed verbatim → findLostHuman happy, no-op save
    base_version: baseVersion,
    meta: {
      run_id: "run-abc",
      model: "test-model",
      ladder: { proposals: [{ annotationId: HUMAN_ID, fields: PROP_FIELDS, reason: "Mathlib now has Foo.bar" }] },
    },
  });
  expect(res.status).toBe(200);
}

describe("proposals — pure logic", () => {
  it("mergeProposals dedups vs pending + rejected and skips non-live ids", () => {
    const inc = [{ annotationId: HUMAN_ID, fields: PROP_FIELDS, reason: "r" }];
    const validIds = new Set([HUMAN_ID]);
    const first = mergeProposals([], [], inc, { now: 1, validIds }).merged;
    expect(first).toHaveLength(1);
    expect(first[0].proposalId).toMatch(/^[0-9a-f]{12}$/);
    // same delta again → deduped (already pending)
    expect(mergeProposals(first, [], inc, { now: 2, validIds }).merged).toHaveLength(1);
    // previously rejected → suppressed
    const rejected = [{ annotationId: HUMAN_ID, fieldsSig: fieldsSig(PROP_FIELDS) }];
    expect(mergeProposals([], rejected, inc, { now: 3, validIds }).merged).toHaveLength(0);
    // target id not live → skipped
    expect(mergeProposals([], [], inc, { now: 4, validIds: new Set(["zzzzzzzzzzzz"]) }).merged).toHaveLength(0);
  });

  it("mergeProposals with liveById drops incoming no-delta proposals and counts them", () => {
    const validIds = new Set([HUMAN_ID]);
    // Live target ALREADY has every proposed value → empty delta → dropped.
    const liveMatching = new Map([[HUMAN_ID, { id: HUMAN_ID, status: "formalized", mathlib: PROP_FIELDS.mathlib }]]);
    const inc = [{ annotationId: HUMAN_ID, fields: PROP_FIELDS, reason: "confirmation" }];
    const dropped = mergeProposals([], [], inc, { now: 1, validIds, liveById: liveMatching });
    expect(dropped.merged).toHaveLength(0);
    expect(dropped.droppedNoDelta).toBe(1);
    // A real delta against the same live target still merges.
    const liveDiffering = new Map([[HUMAN_ID, { id: HUMAN_ID, status: "partial", mathlib: null }]]);
    const kept = mergeProposals([], [], inc, { now: 2, validIds, liveById: liveDiffering });
    expect(kept.merged).toHaveLength(1);
    expect(kept.droppedNoDelta).toBe(0);
    // Without liveById the old behavior is unchanged (no no-delta filtering).
    expect(mergeProposals([], [], inc, { now: 3, validIds }).merged).toHaveLength(1);
  });

  it("applyProposalFields overwrites only whitelisted fields and reports the delta", () => {
    const ann = { id: HUMAN_ID, status: "partial", provenance: "human", label: "L", note: "n" };
    const { next, changed } = applyProposalFields(ann, { ...PROP_FIELDS, provenance: "ai", id: "hack" });
    expect(next.status).toBe("formalized");
    expect(next.mathlib).toEqual(PROP_FIELDS.mathlib);
    expect(next.provenance).toBe("human"); // provenance/id never overwritten here
    expect(next.id).toBe(HUMAN_ID);
    expect(changed.map((c) => c.field).sort()).toEqual(["mathlib", "status"]);
  });
});

describe("proposals — inline banner injection", () => {
  it("injectAuthAndEditor emits __WL_PROPOSALS__ for a logged-in user, not for anon", async () => {
    const { injectAuthAndEditor } = await import("../src/pages.js");
    const proposals = [{ proposalId: "abc123abc123", annotationId: HUMAN_ID, fields: { status: "formalized" }, reason: "r", createdAt: 1 }];
    const html = injectAuthAndEditor("<main></main>", {
      slug: "Foo",
      user: { id: "u", name: "U", role: "user" } as never,
      annotations: [],
      version: 3,
      proposals,
    });
    expect(html).toContain("window.__WL_PROPOSALS__=");
    expect(html).toContain("abc123abc123");
    expect(html).toContain("editor.js?v=16");

    const anon = injectAuthAndEditor("<main></main>", { slug: "Foo", user: null, annotations: [], proposals });
    expect(anon).not.toContain("__WL_PROPOSALS__");
  });
});

describe("POST /api/article/:slug (proposals)", () => {
  it("stores a bot proposal inert (no annotation change), then approve applies it and keeps provenance human", async () => {
    const { db, env } = setup();
    await seedProposal(env); // version 2, HUMAN_ID human
    await storeProposal(env, db, 2); // stores the proposal; still version 2 (no-op)

    expect(articleRow(db)!.version).toBe(2);
    // Inert: the annotation is unchanged, still 'partial'.
    expect(storedAnnotations(db).find((a) => a.id === HUMAN_ID)!.status).toBe("partial");
    const p = pending(db);
    expect(p).toHaveLength(1);
    expect(p[0]).toMatchObject({ annotationId: HUMAN_ID, fields: PROP_FIELDS, runId: "run-abc", model: "test-model" });

    // Approve.
    const res = await save(env, { action: "approve_proposal", proposal_id: p[0].proposalId, base_version: 2 }, { user: "u-patroller" });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true, version: 3 });

    const ann = storedAnnotations(db).find((a) => a.id === HUMAN_ID)!;
    expect(ann.status).toBe("formalized");
    expect(ann.mathlib).toEqual(PROP_FIELDS.mathlib);
    expect(ann.provenance).toBe("human"); // Jack owns it → still bot-protected
    expect(articleRow(db)!.version).toBe(3);
    expect(articleRow(db)!.n_formalized).toBe(2); // counts recomputed

    const rev = latestRevision(db);
    expect(rev.kind).toBe("proposal-approved");
    expect(rev.comment).toMatch(/^proposal-approved:[0-9a-f]{12}$/);
    const modifyEvents = eventRows(db).filter((e) => e.event_type === "modify" && e.annotation_id === HUMAN_ID);
    expect(modifyEvents.length).toBe(1);
    expect(modifyEvents[0].actor_type).toBe("human");

    // Proposal consumed.
    expect(pending(db)).toHaveLength(0);
  });

  it("dual-writes the lifecycle table: pending on store, approved/rejected(+reason) on decision", async () => {
    const { db, env } = setup();
    await seedProposal(env);
    await storeProposal(env, db, 2);
    const prow = () =>
      db.prepare("SELECT * FROM proposals WHERE annotation_id = ?").get(HUMAN_ID) as Record<string, unknown>;
    expect(prow()).toMatchObject({ slug: SLUG, status: "pending", run_id: "run-abc", model: "test-model" });
    expect(prow()).toHaveProperty("created_at");

    // Reject with a reason enum → rejected + reason recorded.
    const p = pending(db)[0];
    const rej = await save(
      env,
      { action: "reject_proposal", proposal_id: p.proposalId, reject_reason: "incorrect" },
      { user: "u-patroller" },
    );
    expect(rej.status).toBe(200);
    expect(prow()).toMatchObject({ status: "rejected", reject_reason: "incorrect", decided_by: "u-patroller" });

    // A different delta → new pending row; approve it → approved, no reason.
    const current = storedAnnotations(db).map(echo);
    const res = await botSave(env, {
      annotations: current,
      base_version: 2,
      meta: { ladder: { proposals: [{ annotationId: HUMAN_ID, fields: { note: "better note" }, reason: "r2" }] } },
    });
    expect(res.status).toBe(200);
    const p2 = pending(db)[0];
    const app = await save(env, { action: "approve_proposal", proposal_id: p2.proposalId, base_version: 2 }, { user: "u-patroller" });
    expect(app.status).toBe(200);
    const row2 = db.prepare("SELECT * FROM proposals WHERE id = ?").get(p2.proposalId) as Record<string, unknown>;
    expect(row2).toMatchObject({ status: "approved", reject_reason: null, decided_by: "u-patroller" });
    // An invalid reject_reason would have been nulled: enum-only (validRejectReason).
  });

  it("marks a pending proposal stale when a later bot save shows its target gone", async () => {
    const { db, env } = setup();
    await seedProposal(env);
    await storeProposal(env, db, 2); // pending targets HUMAN_ID
    // Human tombstones the target (version 3).
    const anns = storedAnnotations(db).map(echo);
    const i = anns.findIndex((a) => a.id === HUMAN_ID);
    anns[i] = { ...anns[i], status: "rejected" };
    const t = await save(env, { annotations: anns, base_version: 2 }, { user: "u-human" });
    expect(t.status).toBe(200);
    // Next bot save (echo, no new proposals) sweeps the dead-target pending.
    const res = await botSave(env, {
      annotations: storedAnnotations(db).map(echo),
      base_version: 3,
      meta: { ladder: { proposals: [] } },
    });
    expect(res.status).toBe(200);
    expect(pending(db)).toHaveLength(0); // blob cleared
    const row = db.prepare("SELECT status, decided_at FROM proposals WHERE annotation_id = ?").get(HUMAN_ID) as {
      status: string;
      decided_at: number | null;
    };
    expect(row.status).toBe("stale");
    expect(row.decided_at).not.toBeNull();
  });

  it("approving a proposal whose target was tombstoned AFTER storage cannot resurrect the veto", async () => {
    const { db, env } = setup();
    await seedProposal(env);
    await storeProposal(env, db, 2); // pending proposal on HUMAN_ID (v2)
    const pid = pending(db)[0].proposalId;
    // Human tombstones the target via a session save (no bot sweep runs).
    const anns = storedAnnotations(db).map(echo);
    const i = anns.findIndex((a) => a.id === HUMAN_ID);
    anns[i] = { ...anns[i], status: "rejected" };
    expect((await save(env, { annotations: anns, base_version: 2 }, { user: "u-human" })).status).toBe(200);
    // Approve with the CURRENT version (what /proposals would hand out) — must
    // NOT flip the veto back; proposal is dropped as stale instead.
    const res = await save(env, { action: "approve_proposal", proposal_id: pid, base_version: 3 }, { user: "u-patroller" });
    expect(res.status).toBe(409);
    expect(storedAnnotations(db).find((a) => a.id === HUMAN_ID)!.status).toBe("rejected"); // veto intact
    const row = db.prepare("SELECT status FROM proposals WHERE id = ?").get(pid) as { status: string };
    expect(row.status).toBe("stale");
    expect(pending(db)).toHaveLength(0);
  });

  it("role 'user' cannot decide (403) and gets the forbidden queue page", async () => {
    const { db, env } = setup();
    await seedProposal(env);
    await storeProposal(env, db, 2);
    const pid = pending(db)[0].proposalId;
    expect((await save(env, { action: "approve_proposal", proposal_id: pid, base_version: 2 }, { user: "u-human" })).status).toBe(403);
    expect((await save(env, { action: "reject_proposal", proposal_id: pid }, { user: "u-human" })).status).toBe(403);
    const page = await get(env, "/proposals", { user: "u-human" });
    expect(page.status).toBe(403);
    expect(await page.text()).toContain("patroller/admin");
    // still pending — nothing recorded
    expect(pending(db)).toHaveLength(1);
  });

  it("GET /proposals: anon redirects to login; logged-in sees the pending queue", async () => {
    const { db, env } = setup();
    await seedProposal(env);
    await storeProposal(env, db, 2);
    const anon = await get(env, "/proposals");
    expect(anon.status).toBe(302);
    expect(anon.headers.get("Location")).toContain("/login");
    const page = await get(env, "/proposals", { user: "u-patroller" });
    expect(page.status).toBe(200);
    const html = await page.text();
    expect(html).toContain("AI proposals");
    expect(html).toContain(pending(db)[0].proposalId);
    expect(html).toContain("wl-prop-approve");
    expect(html).toContain("reject: why?");
    expect(html).toContain('data-ver="2"'); // base_version for the approve POST
  });

  it("drops a proposal targeting a tombstoned (rejected) human annotation", async () => {
    const { db, env } = setup();
    // Tombstone HUMAN_ID via a human save (status rejected → provenance human).
    const anns = storedAnnotations(db).map(echo);
    const i = anns.findIndex((a) => a.id === HUMAN_ID);
    anns[i] = { ...anns[i], status: "rejected" };
    const t = await save(env, { annotations: anns, base_version: 1 }, { user: "u-human" });
    expect(t.status).toBe(200);
    expect(storedAnnotations(db).find((a) => a.id === HUMAN_ID)!.status).toBe("rejected");
    // A bot proposal against the now-tombstoned id must be dropped — a human veto
    // is never a proposal target (else approve could resurrect it).
    await storeProposal(env, db, articleRow(db)!.version);
    expect(pending(db)).toHaveLength(0);
  });

  it("reject drops the proposal, remembers the delta, and suppresses a re-proposal", async () => {
    const { db, env } = setup();
    await seedProposal(env);
    await storeProposal(env, db, 2);
    const pid = pending(db)[0].proposalId;

    const rej = await save(env, { action: "reject_proposal", proposal_id: pid }, { user: "u-patroller" });
    expect(rej.status).toBe(200);
    expect(await rej.json()).toEqual({ ok: true, rejected: true });
    expect(pending(db)).toHaveLength(0);
    expect(articleRow(db)!.version).toBe(2); // reject does not touch annotations

    // The bot re-proposing the identical delta is suppressed.
    await storeProposal(env, db, 2);
    expect(pending(db)).toHaveLength(0);
  });

  it("drops an incoming no-delta proposal at merge (nothing pending, no lifecycle row)", async () => {
    const { db, env } = setup();
    await seedProposal(env); // v2, HUMAN_ID human, status 'partial'
    // "Confirmation" filed as a proposal: fields equal the live values.
    const res = await botSave(env, {
      annotations: storedAnnotations(db).map(echo),
      base_version: 2,
      meta: { ladder: { proposals: [{ annotationId: HUMAN_ID, fields: { status: "partial" }, reason: "confirmed correct" }] } },
    });
    expect(res.status).toBe(200);
    expect(pending(db)).toHaveLength(0);
    const n = db.prepare("SELECT COUNT(*) AS n FROM proposals").get() as { n: number };
    expect(n.n).toBe(0); // never entered the lifecycle log either
  });

  it("anonymous 401; stale/unknown handled; bot endorse still 403", async () => {
    const { db, env } = setup();
    await seedProposal(env);
    await storeProposal(env, db, 2);
    const pid = pending(db)[0].proposalId;

    expect((await save(env, { action: "approve_proposal", proposal_id: pid, base_version: 2 })).status).toBe(401);
    // endorse mints 'human' — the bearer may NEVER do that (attribution hard line)
    expect((await botSave(env, { action: "endorse", annotation_id: HUMAN_ID, base_version: 2 })).status).toBe(403);
    // unknown proposal id → 404
    expect((await save(env, { action: "approve_proposal", proposal_id: "ffffffffffff", base_version: 2 }, { user: "u-patroller" })).status).toBe(404);
    // stale base_version → 409, no write
    const stale = await save(env, { action: "approve_proposal", proposal_id: pid, base_version: 99 }, { user: "u-patroller" });
    expect(stale.status).toBe(409);
    expect(articleRow(db)!.version).toBe(2);
    expect(pending(db)).toHaveLength(1); // untouched
  });
});

describe("bearer decide path (Human-at-boundaries, ratified 2026-08-06)", () => {
  it("bearer approve applies the delta, re-labels human → ai-moderated, kind proposal-decided-ai, actorType pipeline", async () => {
    const { db, env } = setup();
    await seedProposal(env);
    await storeProposal(env, db, 2);
    const pid = pending(db)[0].proposalId;

    const res = await botSave(env, { action: "approve_proposal", proposal_id: pid, base_version: 2 });
    expect(res.status).toBe(200);
    expect(((await res.json()) as { version: number }).version).toBe(3);

    const anns = storedAnnotations(db);
    const target = anns.find((a) => a.id === HUMAN_ID)!;
    // the delta landed…
    expect(target.status).toBe("formalized");
    // …and attribution says the AI changed the bytes: NEVER left 'human'
    expect(target.provenance).toBe("ai-moderated");

    const rev = latestRevision(db)!;
    expect(rev.kind).toBe("proposal-decided-ai");
    expect(rev.user_id).toBe("pipeline");

    const events = eventRows(db);
    const decide = events.find((e) => e.annotation_id === HUMAN_ID && e.event_type === "modify" && e.actor_type === "pipeline");
    expect(decide).toBeTruthy();

    const life = db.prepare("SELECT status, decided_by FROM proposals WHERE id = ?").get(pid) as { status: string; decided_by: string };
    expect(life.status).toBe("approved");
    expect(life.decided_by).toBe("pipeline");
    expect(pending(db)).toHaveLength(0);
  });

  it("bearer approve preserves non-human provenance (no 'ai-moderated' mint on ai-owned bytes)", async () => {
    const { db, env } = setup();
    // NO endorse: the target keeps its seeded (non-human) provenance
    await storeProposal(env, db, 1);
    const pid = pending(db)[0].proposalId;
    const before = storedAnnotations(db).find((a) => a.id === HUMAN_ID)!.provenance;
    expect(before).not.toBe("human");

    const res = await botSave(env, { action: "approve_proposal", proposal_id: pid, base_version: 1 });
    expect(res.status).toBe(200);
    const after = storedAnnotations(db).find((a) => a.id === HUMAN_ID)!;
    expect(after.provenance).toBe(before); // passthrough, no relabel, no mint
  });

  it("bearer reject records the reason + decided_by pipeline and writes no revision", async () => {
    const { db, env } = setup();
    await seedProposal(env);
    await storeProposal(env, db, 2);
    const pid = pending(db)[0].proposalId;
    const revBefore = latestRevision(db)?.id ?? null;

    const res = await botSave(env, { action: "reject_proposal", proposal_id: pid, reject_reason: "not_better" });
    expect(res.status).toBe(200);
    const life = db.prepare("SELECT status, reject_reason, decided_by FROM proposals WHERE id = ?").get(pid) as {
      status: string; reject_reason: string | null; decided_by: string };
    expect(life.status).toBe("rejected");
    expect(life.reject_reason).toBe("not_better");
    expect(life.decided_by).toBe("pipeline");
    expect(latestRevision(db)?.id ?? null).toBe(revBefore); // no content revision
    // the human annotation is untouched — reject never writes bytes
    expect(storedAnnotations(db).find((a) => a.id === HUMAN_ID)!.provenance).toBe("human");
  });

  it("session approve is byte-identical to before: provenance human, kind proposal-approved, actorType human", async () => {
    const { db, env } = setup();
    await seedProposal(env);
    await storeProposal(env, db, 2);
    const pid = pending(db)[0].proposalId;

    const res = await save(env, { action: "approve_proposal", proposal_id: pid, base_version: 2 }, { user: "u-patroller" });
    expect(res.status).toBe(200);
    const target = storedAnnotations(db).find((a) => a.id === HUMAN_ID)!;
    expect(target.provenance).toBe("human");
    expect(latestRevision(db)!.kind).toBe("proposal-approved");
    const decide = eventRows(db).find((e) => e.annotation_id === HUMAN_ID && e.event_type === "modify");
    expect(decide!.actor_type).toBe("human");
  });
});

describe("GET /api/proposals (bearer machine twin)", () => {
  it("401 anon, 403 session, 200 bearer with the page's own row semantics", async () => {
    const { db, env } = setup();
    await seedProposal(env);
    await storeProposal(env, db, 2);

    expect((await get(env, "/api/proposals")).status).toBe(401);
    expect((await get(env, "/api/proposals", { user: "u-patroller" })).status).toBe(403);

    const res = await get(env, "/api/proposals", { bearer: "test-pipeline-token", origin: null });
    expect(res.status).toBe(200);
    const body = (await res.json()) as { ok: boolean; total: number; rows: Array<Record<string, unknown>> };
    expect(body.ok).toBe(true);
    expect(body.total).toBe(1);
    expect(body.rows).toHaveLength(1);
    const row = body.rows[0];
    expect(row.annotationId).toBe(HUMAN_ID);
    expect(row.noChange).toBe(false);
    expect(Array.isArray(row.changed)).toBe(true);
    expect(row.provenance).toBe("human");
    expect(typeof row.version).toBe("number");
  });
});

describe("GET /proposals — evidence cards", () => {
  it("renders the quote, the human-owned badge, and real before/after chips", async () => {
    const { db, env } = setup();
    await seedProposal(env); // HUMAN_ID → provenance 'human' (v2)
    await storeProposal(env, db, 2); // partial → formalized + mathlib Foo.bar
    const html = await (await get(env, "/proposals", { user: "u-patroller" })).text();
    // The annotation's quoted article text (its anchor snippet).
    expect(html).toContain("fundamental theorem of finite abelian groups");
    // Approving edits Jack's own annotation — the loud badge.
    expect(html).toContain("human-owned");
    // Real delta chips computed server-side (applyProposalFields semantics).
    expect(html).toContain(">partial</span>");
    expect(html).toContain(">formalized</span>");
    // Proposed decl links to mathlib4_docs via its module, with the
    // client-side existence-tick placeholder.
    expect(html).toContain("https://leanprover-community.github.io/mathlib4_docs/Mathlib/Foo.html#Foo.bar");
    expect(html).toContain('data-decl="Foo.bar"');
    // No no-change banner: this proposal is a real delta.
    expect(html).not.toContain("changes nothing");
  });

  it("mathlib object replace: BOTH decls render as docs links; reason evidence auto-links", async () => {
    const { db, env } = setup();
    // Target aaaaaaaaaaaa currently cites AddCommGroup @ Mathlib.Algebra.Group.Defs.
    const res = await botSave(env, {
      annotations: storedAnnotations(db).map(echo),
      base_version: 1,
      meta: {
        ladder: {
          proposals: [
            {
              annotationId: "aaaaaaaaaaaa",
              fields: { mathlib: { decl: "CommGroup", module: "Mathlib.Algebra.Group.Defs", match_kind: "exact" } },
              reason: "Tighter match — see Mathlib/Algebra/Group/Defs.lean:120 and GroupTheory/Sylow.lean",
            },
          ],
        },
      },
    });
    expect(res.status).toBe(200);
    const html = await (await get(env, "/proposals", { user: "u-patroller" })).text();
    // Both sides of the decl change are mathlib4_docs links + existence ticks.
    expect(html).toContain("https://leanprover-community.github.io/mathlib4_docs/Mathlib/Algebra/Group/Defs.html#AddCommGroup");
    expect(html).toContain("https://leanprover-community.github.io/mathlib4_docs/Mathlib/Algebra/Group/Defs.html#CommGroup");
    expect(html).toContain('data-decl="AddCommGroup"');
    expect(html).toContain('data-decl="CommGroup"');
    // Mathlib source refs in the reason become GitHub links (Mathlib/ prefix
    // added when missing; line number optional).
    expect(html).toContain("https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Algebra/Group/Defs.lean#L120");
    expect(html).toContain('https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/GroupTheory/Sylow.lean"');
    // Non-human target still shows its provenance.
    expect(html).toContain(">ai</span>");
  });

  it("evidence links: full URLs are not re-prefixed; Archive/ paths keep their real root", async () => {
    const { db, env } = setup();
    const res = await botSave(env, {
      annotations: storedAnnotations(db).map(echo),
      base_version: 1,
      meta: {
        ladder: {
          proposals: [
            {
              annotationId: "aaaaaaaaaaaa",
              fields: { status: "partial" },
              // A reason may quote a full GitHub URL (must not be re-matched
              // from its "com/…" tail) or cite mathlib4's non-Mathlib roots
              // (Archive/, Counterexamples/ — prefixing them 404s).
              reason:
                "see https://github.com/leanprover-community/mathlib4/blob/master/Mathlib/Algebra/Group/Defs.lean#L120 " +
                "and Archive/Imo/Imo1959Q1.lean:12 and Counterexamples/Phillips.lean",
            },
          ],
        },
      },
    });
    expect(res.status).toBe(200);
    const html = await (await get(env, "/proposals", { user: "u-patroller" })).text();
    expect(html).not.toContain("master/Mathlib/com/");
    expect(html).toContain('https://github.com/leanprover-community/mathlib4/blob/master/Archive/Imo/Imo1959Q1.lean#L12');
    expect(html).toContain('https://github.com/leanprover-community/mathlib4/blob/master/Counterexamples/Phillips.lean"');
  });

  it("tombstoned target renders the dead-target fallback, never a live delta card", async () => {
    const { db, env } = setup();
    // File a proposal against the human annotation, then tombstone it via a
    // session save (human veto) with NO bot sweep in between.
    const seed = await botSave(env, {
      annotations: storedAnnotations(db).map(echo),
      base_version: 1,
      meta: { ladder: { proposals: [{ annotationId: HUMAN_ID, fields: { status: "formalized" }, reason: "x" }] } },
    });
    expect(seed.status).toBe(200);
    const anns = storedAnnotations(db).map(echo);
    const human = anns.find((a) => a.id === HUMAN_ID)!;
    human.status = "rejected";
    // The seed save changed no annotation bytes (proposal-blob writes never
    // bump version), so the article is still at version 1.
    const veto = await save(env, { annotations: anns, base_version: 1 }, { user: "u-human" });
    expect(veto.status).toBe(200);
    const html = await (await get(env, "/proposals", { user: "u-patroller" })).text();
    // Approve on this card would 409 "annotation gone" — the card must render
    // the dead-target fallback ("? →" chips + warning), never a live delta
    // computed FROM the tombstoned annotation: a "rejected → formalized" card
    // would read as the AI proposing to overturn the human veto.
    expect(html).toContain("Target annotation not found");
    expect(html).not.toContain(">rejected<");
  });

  it("no-change row: banner + not_better pre-selected; next bot save sweeps it stale", async () => {
    const { db, env } = setup();
    await seedProposal(env); // v2
    // Proposal with a REAL delta at filing time…
    const r1 = await botSave(env, {
      annotations: storedAnnotations(db).map(echo),
      base_version: 2,
      meta: { ladder: { proposals: [{ annotationId: HUMAN_ID, fields: { note: "better note" }, reason: "clearer" }] } },
    });
    expect(r1.status).toBe(200);
    expect(pending(db)).toHaveLength(1);
    const pid = pending(db)[0].proposalId;
    // …then a human applies the same change by hand (v3) → delta is now empty.
    const anns = storedAnnotations(db).map(echo);
    const i = anns.findIndex((a) => a.id === HUMAN_ID);
    anns[i] = { ...anns[i], note: "better note" };
    expect((await save(env, { annotations: anns, base_version: 2 }, { user: "u-human" })).status).toBe(200);
    // The queue computes the delta live: banner + reject reason pre-selected.
    const html = await (await get(env, "/proposals", { user: "u-patroller" })).text();
    expect(html).toContain("changes nothing");
    expect(html).toContain('value="not_better" selected');
    // Self-heal: the next bot save (a plain echo, no new proposals) drops the
    // no-delta pending row and records the expiry as 'stale'.
    expect((await botSave(env, { annotations: storedAnnotations(db).map(echo), base_version: 3 })).status).toBe(200);
    expect(pending(db)).toHaveLength(0);
    const row = db.prepare("SELECT status, decided_at FROM proposals WHERE id = ?").get(pid) as {
      status: string;
      decided_at: number | null;
    };
    expect(row.status).toBe("stale");
    expect(row.decided_at).not.toBeNull();
  });

  it("XSS: hostile quote/section/reason/decl strings render inert", async () => {
    const { db, env } = setup();
    // Hostile anchor via a human edit (v2; also makes the target human-owned).
    const anns = storedAnnotations(db).map(echo);
    const i = anns.findIndex((a) => a.id === HUMAN_ID);
    anns[i] = { ...anns[i], anchor: { section: "<svg onload=alert(4)>", snippet: '<img src=x onerror="alert(1)">' } };
    expect((await save(env, { annotations: anns, base_version: 1 }, { user: "u-human" })).status).toBe(200);
    // Hostile proposal: decl/module/reason all carry markup.
    const res = await botSave(env, {
      annotations: storedAnnotations(db).map(echo),
      base_version: 2,
      meta: {
        ladder: {
          proposals: [
            {
              annotationId: HUMAN_ID,
              fields: {
                status: "formalized",
                mathlib: { decl: '"><img src=x onerror=alert(3)>', module: 'x"><script>alert(5)</script>', match_kind: "exact" },
              },
              reason: '<img src=x onerror=alert(2)> trust me — also Evil/Path.lean:1',
            },
          ],
        },
      },
    });
    expect(res.status).toBe(200);
    const html = await (await get(env, "/proposals", { user: "u-patroller" })).text();
    expect(html).not.toContain("<img");
    expect(html).not.toContain("<svg");
    expect(html).not.toContain("<script>alert");
    expect(html).toContain("&lt;img"); // escaped, still visible to the reviewer
    expect(html).toContain("&lt;svg");
  });
});
