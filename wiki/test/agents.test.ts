// Agent tokens (src/agents.ts, docs/AGENT-CONTRIBUTIONS.md): a signed-in person
// mints a token for an AI agent, and the agent writes as its own users row
// (role 'agent') under the pipeline's rules without the pipeline's powers.
// Covers minting, listing, caps and revocation; the bearer (revoked token,
// blocked owner or agent, no session fallthrough); article saves (ai stamp,
// kind 'agent', patrol, findLostHuman 422, proposals carrying the proposer,
// the per-article proposal cap, the refused pipeline powers, revert-run
// blocking); Brain edits (actor forced to ai, deletes of AI rows only); and
// the person-only surfaces that refuse agents.

import { createHash } from "node:crypto";
import { describe, it, expect, beforeEach } from "vitest";
import {
  setup,
  save,
  botSaveRun,
  post,
  put,
  get,
  articleRow,
  storedAnnotations,
  latestRevision,
  eventRows,
  blockNetwork,
  echo,
  EXTRA_ANNOTATION,
  SEED_ANNOTATIONS,
  SLUG,
  type Harness,
} from "./helpers/harness.js";
import { _resetBrainEditCaches } from "../src/brain-edits.js";
import { _resetBrainAssetMemo } from "../src/brain.js";
import { installBrainFixture } from "./helpers/brain-fixture.js";
import { AGENT_CAP, AGENT_MODEL_MAX, AGENT_PENDING_PER_ARTICLE, AGENT_PENDING_TOTAL } from "../src/agents.js";
import { parsePending } from "../src/proposals.js";

blockNetwork();
beforeEach(() => {
  _resetBrainEditCaches();
  _resetBrainAssetMemo();
});

const AI_ID = "aaaaaaaaaaaa"; // SEED_ANNOTATIONS[0], provenance 'ai'
const HUMAN_ID = "bbbbbbbbbbbb"; // SEED_ANNOTATIONS[1], endorsed to 'human' where a test needs it
const TOKEN_RE = /^wla_[A-Za-z0-9_-]{43}$/;

async function mint(h: Harness, name = "Claude", user = "u-human"): Promise<{ id: string; token: string }> {
  const res = await post(h.env, "/api/agents", { name }, { user });
  expect(res.status).toBe(201);
  const j = (await res.json()) as { agent: { id: string }; token: string };
  return { id: j.agent.id, token: j.token };
}

const as = (token: string) => ({ bearer: token, origin: null });

async function me(h: Harness, token: string): Promise<Record<string, unknown> | null> {
  const j = (await (await get(h.env, "/api/auth/me", { bearer: token })).json()) as { user: Record<string, unknown> | null };
  return j.user;
}

async function editorView(h: Harness, token: string): Promise<Array<Record<string, unknown>>> {
  const res = await get(h.env, `/api/article/${SLUG}.json`, { bearer: token });
  expect(res.status).toBe(200);
  return ((await res.json()) as { annotations: Array<Record<string, unknown>> }).annotations;
}

function pendingProposals(h: Harness) {
  const r = h.db.prepare("SELECT proposal FROM moderation_state WHERE slug = ?").get(SLUG) as { proposal: string | null } | undefined;
  return parsePending(r?.proposal);
}

async function endorseHuman(h: Harness): Promise<void> {
  const res = await save(h.env, { action: "endorse", annotation_id: HUMAN_ID, base_version: 1 }, { user: "u-human" });
  expect(res.status).toBe(200); // version 2
}

describe("minting, listing and revoking agents", () => {
  it("mints an agent row and a token whose hash alone is stored", async () => {
    const h = setup();
    const res = await post(h.env, "/api/agents", { name: "  Claude  " }, { user: "u-human" });
    expect(res.status).toBe(201);
    expect(res.headers.get("Cache-Control")).toBe("no-store");
    const j = (await res.json()) as { agent: { id: string; name: string }; token: string };
    expect(j.token).toMatch(TOKEN_RE);
    expect(j.agent.id).toMatch(/^agent-[0-9a-f]{12}$/);
    expect(j.agent.name).toBe("Claude (agent)");
    expect(h.db.prepare("SELECT name, role FROM users WHERE id = ?").get(j.agent.id)).toEqual({
      name: "Claude (agent)",
      role: "agent",
    });
    const rows = h.db.prepare("SELECT agent_id, owner_id, token_hash, revoked_at FROM agent_tokens").all();
    expect(rows).toEqual([
      {
        agent_id: j.agent.id,
        owner_id: "u-human",
        token_hash: createHash("sha256").update(j.token).digest("hex"),
        revoked_at: null,
      },
    ]);
    expect(JSON.stringify(rows)).not.toContain(j.token.slice(4));
  });

  it("lists only the caller's own agents", async () => {
    const h = setup();
    const mine = await mint(h, "Mine");
    await mint(h, "Theirs", "u-patroller");
    const res = await get(h.env, "/api/agents", { user: "u-human" });
    const j = (await res.json()) as { agents: Array<{ id: string; name: string; revoked_at: number | null }> };
    expect(j.agents.map((a) => [a.id, a.name, a.revoked_at])).toEqual([[mine.id, "Mine (agent)", null]]);
  });

  it("refuses anonymous callers, the pipeline bearer, agents and cross-origin posts", async () => {
    const h = setup();
    const agent = await mint(h);
    expect((await post(h.env, "/api/agents", { name: "x" })).status).toBe(401);
    expect((await post(h.env, "/api/agents", { name: "x" }, { bearer: "test-pipeline-token", origin: null })).status).toBe(403);
    expect((await post(h.env, "/api/agents", { name: "x" }, as(agent.token))).status).toBe(403);
    expect((await get(h.env, "/api/agents", { bearer: agent.token })).status).toBe(403);
    expect((await get(h.env, "/agents", { bearer: agent.token })).status).toBe(403);
    expect((await post(h.env, "/api/agents", { name: "x" }, { user: "u-human", origin: "http://evil.example" })).status).toBe(403);
  });

  it("rejects an empty, overlong, control-character or bidi-override name", async () => {
    const h = setup();
    for (const name of ["", "   ", "x".repeat(61), "a\nb", 7, "Claude\u202e", "a\u200bb", "a\u2028b"]) {
      expect((await post(h.env, "/api/agents", { name }, { user: "u-human" })).status).toBe(400);
    }
    expect(h.db.prepare("SELECT COUNT(*) AS n FROM agent_tokens").get()).toEqual({ n: 0 });
  });

  it(`caps live agents at ${AGENT_CAP} per person; revoking one frees a slot`, async () => {
    const h = setup();
    const agents: Array<{ id: string; token: string }> = [];
    for (let i = 0; i < AGENT_CAP; i++) agents.push(await mint(h, `A${i}`));
    const over = await post(h.env, "/api/agents", { name: "one too many" }, { user: "u-human" });
    expect(over.status).toBe(422);
    expect((await post(h.env, `/api/agents/${agents[0].id}/revoke`, {}, { user: "u-human" })).status).toBe(200);
    await mint(h, "replacement");
  });

  it("revokes for the owner only, and a revoked token resolves to nobody", async () => {
    const h = setup();
    const agent = await mint(h);
    expect(await me(h, agent.token)).toMatchObject({ id: agent.id, role: "agent" });
    expect((await post(h.env, `/api/agents/${agent.id}/revoke`, {}, { user: "u-patroller" })).status).toBe(404);
    expect(await me(h, agent.token)).not.toBeNull();
    expect((await post(h.env, `/api/agents/${agent.id}/revoke`, {}, { user: "u-human" })).status).toBe(200);
    expect(await me(h, agent.token)).toBeNull();
    const again = await post(h.env, `/api/agents/${agent.id}/revoke`, {}, { user: "u-human" });
    expect(await again.json()).toEqual({ ok: true, already_revoked: true });
    expect((await post(h.env, "/api/agents/nope/revoke", {}, { user: "u-human" })).status).toBe(400);
  });

  it("revoking retires the agent's pending proposals", async () => {
    const h = setup();
    const agent = await mint(h);
    await endorseHuman(h);
    const items = await editorView(h, agent.token);
    const fields = { mathlib: { decl: "Foo.bar", module: "Mathlib.Foo", match_kind: "exact" } };
    await save(h.env, { annotations: items, base_version: 2, meta: { ladder: { proposals: [{ annotationId: HUMAN_ID, fields, reason: "r" }] } } }, as(agent.token));
    expect(h.db.prepare("SELECT status FROM proposals").all()).toEqual([{ status: "pending" }]);
    expect((await post(h.env, `/api/agents/${agent.id}/revoke`, {}, { user: "u-human" })).status).toBe(200);
    expect(h.db.prepare("SELECT status FROM proposals").all()).toEqual([{ status: "stale" }]);
    const queue = await (await get(h.env, "/api/proposals", { bearer: "test-pipeline-token" })).json();
    expect(queue).toMatchObject({ total: 0, rows: [] });
  });
});

describe("the agent bearer", () => {
  it("resolves to the agent and stamps last use", async () => {
    const h = setup();
    const agent = await mint(h);
    expect(await me(h, agent.token)).toEqual({ id: agent.id, name: "Claude (agent)", role: "agent", ownerId: "u-human" });
    const used = h.db.prepare("SELECT last_used_at FROM agent_tokens").get() as { last_used_at: number | null };
    expect(used.last_used_at).not.toBeNull();
  });

  it("stops working when the owner or the agent is blocked", async () => {
    const h = setup();
    const agent = await mint(h);
    h.db.prepare("UPDATE users SET role = 'blocked' WHERE id = 'u-human'").run();
    expect(await me(h, agent.token)).toBeNull();
    h.db.prepare("UPDATE users SET role = 'user' WHERE id = 'u-human'").run();
    expect(await me(h, agent.token)).not.toBeNull();
    h.db.prepare("UPDATE users SET role = 'blocked' WHERE id = ?").run(agent.id);
    expect(await me(h, agent.token)).toBeNull();
  });

  it("shares one rate-limit key among all of a person's agents", async () => {
    const h = setup();
    const keys: string[] = [];
    (h.env as unknown as { EDIT_LIMITER: { limit: (o: { key: string }) => Promise<{ success: boolean }> } }).EDIT_LIMITER = {
      limit: async ({ key }) => (keys.push(key), { success: true }),
    };
    const one = await mint(h, "One");
    const two = await mint(h, "Two");
    const annotations = await editorView(h, one.token);
    await save(h.env, { annotations, base_version: 1 }, as(one.token));
    await save(h.env, { annotations, base_version: 1 }, as(two.token));
    expect(keys.filter((k) => k.startsWith("agents-of:"))).toEqual(["agents-of:u-human", "agents-of:u-human"]);
  });

  it("an unknown agent-shaped token is nobody, even beside a session cookie", async () => {
    const h = setup();
    const res = await get(h.env, "/api/auth/me", { bearer: "wla_" + "A".repeat(43), user: "u-human" });
    expect(await res.json()).toEqual({ user: null });
  });
});

describe("agent article saves", () => {
  it("go live labelled ai, as kind 'agent', patrollable, without the pipeline's review stamp", async () => {
    const h = setup();
    const agent = await mint(h);
    const items = await editorView(h, agent.token);
    const changed = items.map((a) => (a.id === AI_ID ? { ...a, label: "Abelian group (agent edit)" } : a));
    const res = await save(h.env, { annotations: [...changed, { ...echo(EXTRA_ANNOTATION), provenance: "human" }], base_version: 1 }, as(agent.token));
    expect(res.status).toBe(200);
    const stored = storedAnnotations(h.db);
    expect(stored.find((a) => a.id === AI_ID)).toMatchObject({ label: "Abelian group (agent edit)", provenance: "ai" });
    expect(stored.find((a) => a.label === EXTRA_ANNOTATION.label)?.provenance).toBe("ai");
    const rev = latestRevision(h.db);
    expect(rev).toMatchObject({ kind: "agent", user_id: agent.id });
    expect(new Set(eventRows(h.db).map((e) => `${e.actor_type}:${e.user_id}`))).toEqual(new Set([`pipeline:${agent.id}`]));
    expect(h.db.prepare("SELECT last_reviewed_at FROM moderation_state WHERE slug = ?").get(SLUG)).toBeUndefined();
    expect((await post(h.env, `/api/revision/${rev.id}/patrol`, {}, { user: "u-patroller" })).status).toBe(200);
    const recent = await (await get(h.env, "/recent-changes?kind=agent")).text();
    expect(recent).toContain("Claude (agent)");
  });

  it("require base_version and refuse revid and meta.run_id", async () => {
    const h = setup();
    const agent = await mint(h);
    const annotations = await editorView(h, agent.token);
    for (const body of [
      { annotations },
      { annotations, base_version: 1, revid: 67890 },
      { annotations, base_version: 1, meta: { run_id: "run-x" } },
    ]) {
      expect((await save(h.env, body, as(agent.token))).status).toBe(400);
    }
    expect(articleRow(h.db)!.version).toBe(1);
  });

  it("cannot copy a human annotation into a new one marked human", async () => {
    const h = setup();
    const agent = await mint(h);
    await endorseHuman(h);
    const items = await editorView(h, agent.token);
    const copy = { ...items.find((a) => a.id === HUMAN_ID)! };
    delete copy.id;
    const res = await save(h.env, { annotations: [...items, copy], base_version: 2 }, as(agent.token));
    expect(res.status).toBe(200);
    const stored = storedAnnotations(h.db).filter((a) => a.label === copy.label);
    expect(stored.map((a) => [a.id === HUMAN_ID, a.provenance])).toEqual([
      [true, "human"],
      [false, "ai"],
    ]);
  });

  it("cannot annotate a statement a person vetoed", async () => {
    const h = setup();
    const agent = await mint(h);
    const veto = echo(SEED_ANNOTATIONS).map((a) => (a.id === AI_ID ? { ...a, status: "rejected" } : a));
    expect((await save(h.env, { annotations: veto, base_version: 1 }, { user: "u-human" })).status).toBe(200);
    const items = await editorView(h, agent.token);
    const again: Record<string, unknown> = { ...items.find((a) => a.id === AI_ID)!, status: "formalized", label: "Abelian group again" };
    delete again.id;
    const res = await save(h.env, { annotations: [...items, again], base_version: 2 }, as(agent.token));
    expect(res.status).toBe(422);
    expect(await res.json()).toMatchObject({ error: "statement vetoed by a person" });
    expect(articleRow(h.db)!.version).toBe(2);
  });

  it("cannot claim a pipeline run through a revision comment", async () => {
    const h = setup();
    const agent = await mint(h);
    expect((await botSaveRun(h.env, { annotations: [...echo(SEED_ANNOTATIONS), echo(EXTRA_ANNOTATION)], base_version: 1 }, "run-1")).status).toBe(200);
    const human = echo(storedAnnotations(h.db)).map((a) => (a.id === AI_ID ? { ...a, label: "Human label" } : a));
    expect((await save(h.env, { annotations: human, base_version: 2 }, { user: "u-human" })).status).toBe(200);
    const items = (await editorView(h, agent.token)).map((a) => (a.id === HUMAN_ID ? { ...a, label: "agent label" } : a));
    expect((await save(h.env, { annotations: items, base_version: 3, comment: "fix:run-1" }, as(agent.token))).status).toBe(200);
    const res = await post(h.env, "/api/admin/revert-run/run-1", {}, { user: "u-admin" });
    expect(await res.json()).toMatchObject({ reverted: [], skipped: [{ slug: SLUG, reason: "human-edited-since" }] });
    expect(storedAnnotations(h.db).find((a) => a.id === AI_ID)?.label).toBe("Human label");
  });

  it("cannot change a human annotation, and file a proposal that names the agent instead", async () => {
    const h = setup();
    const agent = await mint(h);
    await endorseHuman(h);
    const items = await editorView(h, agent.token);
    const clobber = items.map((a) => (a.id === HUMAN_ID ? { ...a, label: "rewritten" } : a));
    const lost = await save(h.env, { annotations: clobber, base_version: 2 }, as(agent.token));
    expect(lost.status).toBe(422);

    const fields = { status: "formalized", mathlib: { decl: "Foo.bar", module: "Mathlib.Foo", match_kind: "exact" } };
    const res = await save(
      h.env,
      {
        annotations: items,
        base_version: 2,
        meta: {
          model: "claude-x" + "y".repeat(300),
          ladder: { proposals: [{ annotationId: HUMAN_ID, fields, reason: "r" }, { annotationId: AI_ID, fields, reason: "not human" }] },
        },
      },
      as(agent.token),
    );
    expect(res.status).toBe(200);
    expect(await res.json()).toMatchObject({ noop: true });
    const model = ("claude-x" + "y".repeat(300)).slice(0, AGENT_MODEL_MAX);
    expect(pendingProposals(h)).toMatchObject([{ annotationId: HUMAN_ID, proposedBy: agent.id, model }]);
    expect(h.db.prepare("SELECT proposed_by, run_id, model FROM proposals").all()).toEqual([
      { proposed_by: agent.id, run_id: null, model },
    ]);
    expect(articleRow(h.db)!.version).toBe(2);
    const queue = await (await get(h.env, "/proposals", { user: "u-patroller" })).text();
    expect(queue).toContain(`proposed by <a href="/u/${agent.id}">Claude (agent)</a>`);
  });

  it(`keep at most ${AGENT_PENDING_PER_ARTICLE} of one agent's proposals pending per article`, async () => {
    const h = setup();
    const agent = await mint(h);
    await endorseHuman(h);
    const items = await editorView(h, agent.token);
    const proposals = (from: number, n: number) =>
      Array.from({ length: n }, (_, i) => ({
        annotationId: HUMAN_ID,
        fields: { mathlib: { decl: `Foo.bar${from + i}`, module: "Mathlib.Foo", match_kind: "exact" } },
        reason: "r",
      }));
    const first = await save(h.env, { annotations: items, base_version: 2, meta: { ladder: { proposals: proposals(0, 12) } } }, as(agent.token));
    expect(first.status).toBe(200);
    expect(pendingProposals(h)).toHaveLength(AGENT_PENDING_PER_ARTICLE);
    const more = await save(h.env, { annotations: items, base_version: 2, meta: { ladder: { proposals: proposals(12, 3) } } }, as(agent.token));
    expect(more.status).toBe(200);
    expect(pendingProposals(h)).toHaveLength(AGENT_PENDING_PER_ARTICLE);
  });

  it(`keep at most ${AGENT_PENDING_TOTAL} of one agent's proposals pending overall`, async () => {
    const h = setup();
    const agent = await mint(h);
    await endorseHuman(h);
    const ins = h.db.prepare(
      "INSERT INTO proposals (id, slug, annotation_id, fields, fields_sig, status, created_at, proposed_by) VALUES (?, 'Other', 'cccccccccccc', '{}', ?, 'pending', 1, ?)",
    );
    for (let i = 0; i < AGENT_PENDING_TOTAL; i++) ins.run(i.toString(16).padStart(12, "0"), `sig${i}`, agent.id);
    const items = await editorView(h, agent.token);
    const fields = { mathlib: { decl: "Foo.bar", module: "Mathlib.Foo", match_kind: "exact" } };
    const res = await save(h.env, { annotations: items, base_version: 2, meta: { ladder: { proposals: [{ annotationId: HUMAN_ID, fields, reason: "r" }] } } }, as(agent.token));
    expect(res.status).toBe(200);
    expect(pendingProposals(h)).toHaveLength(0);
  });

  it("cannot endorse, decide proposals, create articles or revert runs", async () => {
    const h = setup();
    const agent = await mint(h);
    const endorse = await save(h.env, { action: "endorse", annotation_id: AI_ID, base_version: 1 }, as(agent.token));
    expect(endorse.status).toBe(403);
    const decide = await save(h.env, { action: "approve_proposal", proposal_id: "aaaaaaaaaaaa", base_version: 1 }, as(agent.token));
    expect(decide.status).toBe(403);
    expect((await put(h.env, "/api/article/Brand_New", { wikipedia_title: "Brand New", annotations: [] }, as(agent.token))).status).toBe(403);
    expect((await post(h.env, "/api/admin/revert-run/run-1", {}, as(agent.token))).status).toBe(403);
    expect(storedAnnotations(h.db).find((a) => a.id === AI_ID)?.provenance).toBe("ai");
  });

  it("block a later revert-run of the article, like a human edit", async () => {
    const h = setup();
    const agent = await mint(h);
    const run = await botSaveRun(h.env, { annotations: [...echo(SEED_ANNOTATIONS), echo(EXTRA_ANNOTATION)], base_version: 1 }, "run-1");
    expect(run.status).toBe(200);
    const items = (await editorView(h, agent.token)).map((a) => (a.id === AI_ID ? { ...a, label: "agent label" } : a));
    expect((await save(h.env, { annotations: items, base_version: 2 }, as(agent.token))).status).toBe(200);
    const res = await post(h.env, "/api/admin/revert-run/run-1", {}, { user: "u-admin" });
    expect(await res.json()).toMatchObject({ reverted: [], skipped: [{ slug: SLUG, reason: "human-edited-since" }] });
  });
});

describe("agent Brain edits", () => {
  const REL = { src: "Q181296", dst: "decl:Mathlib:CommGroup", kind: "formalizes", evidence: { note: "n" } };

  it("are labelled ai whether or not the agent declares it; declaring human is refused", async () => {
    const h = setup();
    installBrainFixture(h.env);
    const agent = await mint(h);
    const human = await post(h.env, "/api/brain/edge", { ...REL, actor_type: "human" }, as(agent.token));
    expect(human.status).toBe(400);
    const res = await post(h.env, "/api/brain/edge", REL, as(agent.token));
    expect(res.status).toBe(201);
    expect(await res.json()).toMatchObject({ actor_type: "ai", added_by: agent.id });
  });

  it("may delete AI-made edges only", async () => {
    const h = setup();
    installBrainFixture(h.env);
    const agent = await mint(h);
    const byHuman = (await (await post(h.env, "/api/brain/edge", REL, { user: "u-human" })).json()) as { id: string };
    const refused = await post(h.env, `/api/brain/edge/${byHuman.id}/delete`, {}, as(agent.token));
    expect(refused.status).toBe(403);
    const other = { ...REL, dst: "xref:lmfdb_knowl:group.abelian", kind: "xref" };
    const byAgent = (await (await post(h.env, "/api/brain/edge", other, as(agent.token))).json()) as { id: string };
    expect((await post(h.env, `/api/brain/edge/${byAgent.id}/delete`, {}, as(agent.token))).status).toBe(200);
    const rows = h.db.prepare("SELECT id, status FROM brain_edges ORDER BY rowid").all();
    expect(rows).toEqual([
      { id: byHuman.id, status: "live" },
      { id: byAgent.id, status: "deleted" },
    ]);
  });
});

describe("person-only surfaces refuse agents", () => {
  it("repo registration answers 403", async () => {
    const h = setup();
    const agent = await mint(h);
    expect((await get(h.env, "/api/repos", { bearer: agent.token })).status).toBe(403);
  });

  it("an agent's profile names its operator, blocked or not", async () => {
    const h = setup();
    const agent = await mint(h);
    const owner = 'AI agent operated by <a href="/u/u-human">Human Tester</a>';
    expect(await (await get(h.env, `/u/${agent.id}`)).text()).toContain(owner);
    h.db.prepare("UPDATE users SET role = 'blocked' WHERE id = ?").run(agent.id);
    expect(await (await get(h.env, `/u/${agent.id}`)).text()).toContain(owner);
  });
});
