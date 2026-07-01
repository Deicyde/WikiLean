// Propose-then-approve (docs/propose-then-approve.md): a bot review may PROPOSE
// an update to a human annotation (findLostHuman still preserves it verbatim);
// Jack approves or rejects. Proposals live inert in moderation_state.proposal
// and never touch articles.annotations until approved.

import { describe, it, expect } from "vitest";
import {
  setup,
  save,
  botSave,
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
    const first = mergeProposals([], [], inc, { now: 1, validIds });
    expect(first).toHaveLength(1);
    expect(first[0].proposalId).toMatch(/^[0-9a-f]{12}$/);
    // same delta again → deduped (already pending)
    expect(mergeProposals(first, [], inc, { now: 2, validIds })).toHaveLength(1);
    // previously rejected → suppressed
    const rejected = [{ annotationId: HUMAN_ID, fieldsSig: fieldsSig(PROP_FIELDS) }];
    expect(mergeProposals([], rejected, inc, { now: 3, validIds })).toHaveLength(0);
    // target id not live → skipped
    expect(mergeProposals([], [], inc, { now: 4, validIds: new Set(["zzzzzzzzzzzz"]) })).toHaveLength(0);
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
    expect(html).toContain("editor.js?v=15");

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
    const res = await save(env, { action: "approve_proposal", proposal_id: p[0].proposalId, base_version: 2 }, { user: "u-human" });
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

  it("reject drops the proposal, remembers the delta, and suppresses a re-proposal", async () => {
    const { db, env } = setup();
    await seedProposal(env);
    await storeProposal(env, db, 2);
    const pid = pending(db)[0].proposalId;

    const rej = await save(env, { action: "reject_proposal", proposal_id: pid }, { user: "u-human" });
    expect(rej.status).toBe(200);
    expect(await rej.json()).toEqual({ ok: true, rejected: true });
    expect(pending(db)).toHaveLength(0);
    expect(articleRow(db)!.version).toBe(2); // reject does not touch annotations

    // The bot re-proposing the identical delta is suppressed.
    await storeProposal(env, db, 2);
    expect(pending(db)).toHaveLength(0);
  });

  it("bots cannot approve/reject (403); anonymous 401; stale/unknown handled", async () => {
    const { db, env } = setup();
    await seedProposal(env);
    await storeProposal(env, db, 2);
    const pid = pending(db)[0].proposalId;

    expect((await botSave(env, { action: "approve_proposal", proposal_id: pid, base_version: 2 })).status).toBe(403);
    expect((await save(env, { action: "approve_proposal", proposal_id: pid, base_version: 2 })).status).toBe(401);
    // unknown proposal id → 404
    expect((await save(env, { action: "approve_proposal", proposal_id: "ffffffffffff", base_version: 2 }, { user: "u-human" })).status).toBe(404);
    // stale base_version → 409, no write
    const stale = await save(env, { action: "approve_proposal", proposal_id: pid, base_version: 99 }, { user: "u-human" });
    expect(stale.status).toBe(409);
    expect(articleRow(db)!.version).toBe(2);
    expect(pending(db)).toHaveLength(1); // untouched
  });
});
