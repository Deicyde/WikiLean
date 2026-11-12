// Community brain edges (docs/BRAIN-EDITS-ROADMAP.md): POST/GET/DELETE
// /api/brain/edge(s). Exercises auth (login required; OAuth forces human, bearer
// must declare actor_type), origin/rate-limit guards, oracle + kind + xref
// validation, dedupe, the added-by/human-AI provenance, and the soft-delete
// gravestone. Node existence is oracle'd against the STRICT v3 resolver
// (aliases.json organs ∪ supercells.json + the cell shards), served from the
// shared v3 fixture (helpers/brain-fixture.ts) — the v2 per-node shards are
// retired. cell:<anchor> endpoints normalize to their anchor before storage,
// and prose labels / unanchored xref:/lit: ids must NEVER validate.

import { describe, it, expect, beforeEach } from "vitest";
import { setup, post, get, blockNetwork, PIPELINE_TOKEN, type Harness } from "./helpers/harness.js";
import { app } from "../src/index.js";
import { _brainEditCacheReleaseIds, _resetBrainEditCaches } from "../src/brain-edits.js";
import { CC0_AGREEMENT_TEXT, CC0_AGREEMENT_VERSION } from "../src/articles-overlay.js";
import { _resetBrainAssetMemo } from "../src/brain.js";
import {
  installBrainFixture,
  DEFAULT_ALIASES,
  MODULE_CELL,
  MODULE_Q,
  LINALG_SUPER,
} from "./helpers/brain-fixture.js";
import type { Env } from "../src/env.js";

blockNetwork();
beforeEach(() => {
  _resetBrainEditCaches(); // isolate-lifetime xref index (+ its inversion)
  _resetBrainAssetMemo(); // isolate-lifetime aliases/supercells/manifest memo
});

const CONCEPT = "Q181296"; // abelian group (organ of cell:Q181296 in the fixture)
const DECL = "decl:Mathlib:CommGroup"; // organ of the same cell
const XREF_DST = "xref:lmfdb_knowl:group.abelian";
const UNKNOWN = "Q999999999";

// Serve the shared v3 fixture, optionally extending aliases.json `organs`
// (extra node ids the oracle should accept) and overriding the static
// xref_index.json (page → nodes; brain-edits inverts it for a node's own
// static pages — the replacement for the v2 shard entries' xref edge lists).
function installBrainAssets(
  env: Env,
  extraOrgans: Record<string, string> = {},
  xrefIndex: Record<string, string[]> = {},
): void {
  installBrainFixture(env, {
    aliases: {
      ...DEFAULT_ALIASES,
      organs: { ...DEFAULT_ALIASES.organs, ...extraOrgans },
    },
    xrefIndex,
  });
}

// An edge carries the contributor's own evidence note, so a write needs the
// CC0 grant on record (the same gate as the reader's overlay). Record it the
// way an earlier edit would have, so the ordinary cases below are not about
// the first-edit flow — "the CC0 grant" describes it on its own.
function agreed(h: Harness, userId = "u-human", version = CC0_AGREEMENT_VERSION): void {
  h.db.prepare("INSERT OR REPLACE INTO user_agreements (user_id, cc0_version, accepted_at, verified_at) VALUES (?,?,?,NULL)")
    .run(userId, version, Date.now());
}

function harness(opts: Parameters<typeof setup>[0] = {}): Harness {
  const h = setup(opts);
  installBrainAssets(h.env);
  agreed(h);
  return h;
}

function edgeRows(h: Harness): Array<Record<string, unknown>> {
  return h.db.prepare("SELECT * FROM brain_edges ORDER BY created_at").all() as Array<Record<string, unknown>>;
}

function postEdge(h: Harness, body: Record<string, unknown>, opts = {}): Promise<Response> {
  return post(h.env, "/api/brain/edge", body, opts);
}

const REL = { src: CONCEPT, dst: DECL, kind: "formalizes", evidence: { note: "CommGroup formalizes abelian group" } };

describe("POST /api/brain/edge", () => {
  it("a logged-in user adds a live edge, attributed + actor_type=human", async () => {
    const h = harness();
    const res = await postEdge(h, REL, { user: "u-human" });
    expect(res.status).toBe(201);
    const j = (await res.json()) as Record<string, unknown>;
    expect(j).toMatchObject({ ok: true, actor_type: "human", added_by: "u-human" });
    const rows = edgeRows(h);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      src: CONCEPT, dst: DECL, kind: "formalizes",
      added_by: "u-human", actor_type: "human", status: "live", deleted_by: null,
    });
    expect(JSON.parse(rows[0].evidence as string)).toMatchObject({ note: "CommGroup formalizes abelian group" });
  });

  it("requires login (401 when anonymous)", async () => {
    const h = harness();
    const res = await postEdge(h, REL, {});
    expect(res.status).toBe(401);
    expect(edgeRows(h)).toHaveLength(0);
  });

  it("rejects a cross-origin request (403)", async () => {
    const h = harness();
    const res = await postEdge(h, REL, { user: "u-human", origin: "http://evil.example" });
    expect(res.status).toBe(403);
  });

  it("a bearer/API call MUST declare actor_type", async () => {
    const h = harness();
    const noType = await postEdge(h, REL, { bearer: PIPELINE_TOKEN, origin: null });
    expect(noType.status).toBe(400);
    const asAi = await postEdge(h, { ...REL, actor_type: "ai" }, { bearer: PIPELINE_TOKEN, origin: null });
    expect(asAi.status).toBe(201);
    expect((await asAi.json() as Record<string, unknown>).actor_type).toBe("ai");
    expect(edgeRows(h)[0]).toMatchObject({ added_by: "pipeline", actor_type: "ai" });
  });

  it("a bearer call cannot forge actor_type on a browser session path (session forces human)", async () => {
    const h = harness();
    // a session user passing actor_type:'ai' is ignored — forced to human
    const res = await postEdge(h, { ...REL, actor_type: "ai" }, { user: "u-human" });
    expect(res.status).toBe(201);
    expect(edgeRows(h)[0].actor_type).toBe("human");
  });

  it("accepts an xref cross-database link and stores {db, value}", async () => {
    const h = harness();
    const res = await postEdge(h, { src: CONCEPT, dst: XREF_DST, kind: "xref", evidence: { note: "same object in LMFDB" } }, { user: "u-human" });
    expect(res.status).toBe(201);
    const ev = JSON.parse(edgeRows(h)[0].evidence as string) as Record<string, unknown>;
    expect(ev).toMatchObject({ db: "lmfdb_knowl", value: "group.abelian" });
  });

  it("rejects an unknown xref db", async () => {
    const h = harness();
    const res = await postEdge(h, { src: CONCEPT, dst: "xref:notadb:x", kind: "xref", evidence: { note: "n" } }, { user: "u-human" });
    expect(res.status).toBe(400);
  });

  it("caps an oversized xref dst/value (no unbounded storage)", async () => {
    const h = harness();
    const huge = "xref:lmfdb_knowl:" + "a".repeat(1000);
    const res = await postEdge(h, { src: CONCEPT, dst: huge, kind: "xref", evidence: { note: "n" } }, { user: "u-human" });
    expect(res.status).toBe(400);
    expect(edgeRows(h)).toHaveLength(0);
  });

  it("rejects a non-community kind (depends/contains are machine-only)", async () => {
    const h = harness();
    expect((await postEdge(h, { ...REL, kind: "depends" }, { user: "u-human" })).status).toBe(400);
    expect((await postEdge(h, { ...REL, kind: "contains" }, { user: "u-human" })).status).toBe(400);
  });

  it("requires a real src node and a real dst node", async () => {
    const h = harness();
    expect((await postEdge(h, { ...REL, src: UNKNOWN }, { user: "u-human" })).status).toBe(400);
    expect((await postEdge(h, { ...REL, dst: UNKNOWN }, { user: "u-human" })).status).toBe(400);
  });

  it("allows a missing evidence note (optional) but rejects a self-loop", async () => {
    const h = harness();
    // note is optional → a note-less edge succeeds
    const noNote = await postEdge(h, { src: CONCEPT, dst: DECL, kind: "formalizes", evidence: {} }, { user: "u-human" });
    expect(noNote.status).toBe(201);
    expect((await postEdge(h, { src: CONCEPT, dst: CONCEPT, kind: "relates", evidence: { note: "x" } }, { user: "u-human" })).status).toBe(400);
  });

  it("dedupes an identical live edge (idempotent)", async () => {
    const h = harness();
    const a = await postEdge(h, REL, { user: "u-human" });
    const b = await postEdge(h, REL, { user: "u-human" });
    expect(a.status).toBe(201);
    expect(b.status).toBe(200);
    expect((await b.json() as Record<string, unknown>).duplicate).toBe(true);
    expect(edgeRows(h)).toHaveLength(1);
  });

  it("429s when the rate limiter denies", async () => {
    const h = harness({ limiterAllows: false });
    installBrainAssets(h.env);
    expect((await postEdge(h, REL, { user: "u-human" })).status).toBe(429);
  });

  // ---- the v3 oracle: strict ids in, v2 grammar stored -----------------------

  it("accepts a supercell path: endpoint (validated via supercells.json)", async () => {
    const h = harness();
    const res = await postEdge(
      h,
      { src: CONCEPT, dst: LINALG_SUPER, kind: "relates", evidence: { note: "field link" } },
      { user: "u-human" },
    );
    expect(res.status).toBe(201);
    expect(edgeRows(h)[0]).toMatchObject({ src: CONCEPT, dst: LINALG_SUPER, kind: "relates" });
  });

  it("accepts a cell:<anchor> endpoint AND stores it normalized to the anchor", async () => {
    const h = harness();
    // the /brain search picker submits cell ids — the v2 oracle 400'd these
    // (the live bug this port fixes); they must now validate AND store in the
    // v2 id grammar the overlay string-matches on.
    const res = await postEdge(
      h,
      { src: `cell:${CONCEPT}`, dst: MODULE_CELL, kind: "relates", evidence: { note: "picker" } },
      { user: "u-human" },
    );
    expect(res.status).toBe(201);
    expect(edgeRows(h)[0]).toMatchObject({ src: CONCEPT, dst: MODULE_Q, kind: "relates" });
    // the normalized endpoints are what the overlay serves back
    const j = (await (await get(h.env, `/api/brain/edges?id=${encodeURIComponent(MODULE_Q)}`)).json()) as {
      edges: Array<{ src: string; dst: string }>;
    };
    expect(j.edges).toHaveLength(1);
    expect(j.edges[0]).toMatchObject({ src: CONCEPT, dst: MODULE_Q });
  });

  it("strips a double-wrapped cell:cell: endpoint to fixpoint (no atom-grammar rows in D1)", async () => {
    const h = harness();
    // A double-wrapped id (plausible agent bug: prefixing an already-prefixed
    // id) must land on the SAME stored row as the bare anchor — a single-strip
    // normalize stored "cell:Q…" in the atom grammar, defeating (src,dst,kind)
    // dedupe, hiding the row from the overlay string-match, and letting a
    // disguised self-loop (cell:cell:X → X) past the src===dst guard.
    const dup = await postEdge(
      h,
      { src: `cell:cell:${CONCEPT}`, dst: MODULE_CELL, kind: "relates", evidence: { note: "wrapped" } },
      { user: "u-human" },
    );
    expect(dup.status).toBe(201);
    expect(edgeRows(h)[0]).toMatchObject({ src: CONCEPT, dst: MODULE_Q, kind: "relates" });
    // disguised self-loop: both endpoints normalize to the same anchor → 400
    const loop = await postEdge(
      h,
      { src: `cell:cell:${CONCEPT}`, dst: CONCEPT, kind: "relates", evidence: { note: "loop" } },
      { user: "u-human" },
    );
    expect(loop.status).toBe(400);
    expect(edgeRows(h)).toHaveLength(1);
  });

  it("rejects a prose label as an endpoint (no label/aka/slug resolution)", async () => {
    const h = harness();
    // "Vector space" is an `aka` of the Module atom — the READ api resolves it,
    // the WRITE oracle must not: an accepted label would store a junk id.
    const res = await postEdge(
      h,
      { src: CONCEPT, dst: "Vector space", kind: "relates", evidence: { note: "x" } },
      { user: "u-human" },
    );
    expect(res.status).toBe(400);
    expect(String((await res.json() as Record<string, unknown>).error)).toContain("not a known brain node");
    expect(edgeRows(h)).toHaveLength(0);
  });

  it("fails closed when the mandatory alias oracle is unavailable", async () => {
    const h = setup();
    installBrainFixture(h.env, { aliases: null });
    const res = await postEdge(h, REL, { user: "u-human" });
    expect(res.status).toBe(503);
    expect(await res.json()).toMatchObject({ ok: false, error: "brain release unavailable" });
    expect(edgeRows(h)).toHaveLength(0);
    expect(h.db.prepare("SELECT * FROM brain_nodes").all()).toHaveLength(0);
  });

  it("rejects an unanchored xref:/lit: endpoint with the named v3-drop reason", async () => {
    const h = harness();
    const res = await postEdge(
      h,
      { src: CONCEPT, dst: "xref:dlmf:1.1", kind: "relates", evidence: { note: "x" } },
      { user: "u-human" },
    );
    expect(res.status).toBe(400);
    expect(String((await res.json() as Record<string, unknown>).error)).toContain(
      "unanchored external id — dropped in v3; link the page to a cell first",
    );
    const lit = await postEdge(
      h,
      { src: CONCEPT, dst: "lit:9999.00001#thm1", kind: "cites", evidence: { note: "x" } },
      { user: "u-human" },
    );
    expect(lit.status).toBe(400);
    expect(String((await lit.json() as Record<string, unknown>).error)).toContain("dropped in v3");
    expect(edgeRows(h)).toHaveLength(0);
  });
});

describe("GET /api/brain/node — deleted v2 route", () => {
  it("answers a plain 404 (the tombstone was deleted 2026-08-04), not a 500", async () => {
    const h = harness();
    const res = await get(h.env, `/api/brain/node?id=${encodeURIComponent(CONCEPT)}`);
    expect(res.status).toBe(404);
  });
});

describe("GET /api/brain/edges", () => {
  it("returns live community edges touching the node, both directions", async () => {
    const h = harness();
    await postEdge(h, REL, { user: "u-human" });
    const byConcept = await get(h.env, `/api/brain/edges?id=${encodeURIComponent(CONCEPT)}`);
    const byDecl = await get(h.env, `/api/brain/edges?id=${encodeURIComponent(DECL)}`);
    expect(((await byConcept.json()) as { edges: unknown[] }).edges).toHaveLength(1);
    expect(((await byDecl.json()) as { edges: unknown[] }).edges).toHaveLength(1);
    expect(byConcept.headers.get("Cache-Control")).toBe("no-store");
  });
});

// Stub the Wikidata wbgetentities call (harness blockNetwork() throws otherwise).
// `entities` maps QID → entity JSON; an absent QID responds as "missing".
async function withWikidata(
  entities: Record<string, unknown>,
  fn: () => Promise<Response>,
): Promise<Response> {
  const prev = globalThis.fetch;
  globalThis.fetch = (async (url: RequestInfo | URL) => {
    const m = String(url).match(/ids=(Q\d+)/);
    const qid = m ? m[1] : "";
    const ent = qid ? (entities[qid] ?? { missing: "" }) : {};
    return new Response(JSON.stringify({ entities: qid ? { [qid]: ent } : {} }), { status: 200 });
  }) as typeof fetch;
  try {
    return await fn();
  } finally {
    globalThis.fetch = prev;
  }
}

const QUICK_ITEM = {
  lmfdb_id: "group.abelian",
  qid: CONCEPT,
  decl: "CommGroup",
  file: "Mathlib/Algebra/Group/Defs.lean",
  note: "tentative LMFDB tag",
};

function postQuick(h: Harness, body: Record<string, unknown>, opts = {}): Promise<Response> {
  return post(h.env, "/api/brain/quickstatements", body, opts);
}

async function lmfdbQueue(h: Harness): Promise<{ items: Array<Record<string, unknown>>; updated: string }> {
  const raw = await h.renderCache.get("crossref:lmfdb:queue");
  return raw ? JSON.parse(raw) as { items: Array<Record<string, unknown>>; updated: string } : { items: [], updated: "" };
}

describe("POST /api/brain/quickstatements", () => {
  it("accepts pasted LMFDB TSV, creates Brain edges, and upserts the review queue", async () => {
    const h = harness();
    const text = [
      "lmfdb_id\tqid\tdecl\tfile\tnote",
      "group.abelian\tQ181296\tCommGroup\tMathlib/Algebra/Group/Defs.lean\tTentative LMFDB tag",
    ].join("\n");
    const res = await postQuick(h, { db: "lmfdb", text }, { user: "u-human" });
    expect(res.status).toBe(200);
    const j = await res.json() as { accepted: number; failed: number; rows: Array<Record<string, unknown>> };
    expect(j).toMatchObject({ accepted: 1, failed: 0 });
    expect(j.rows[0]).toMatchObject({
      ok: true,
      ids: { lmfdb: "group.abelian", wikidata: CONCEPT, mathlib: "CommGroup" },
      queued: true,
      queue_id: "group.abelian",
      queue_decl: "CommGroup",
    });

    const rows = edgeRows(h);
    expect(rows).toHaveLength(3);
    expect(rows.find((r) => r.kind === "xref" && r.src === CONCEPT)).toMatchObject({
      src: CONCEPT,
      dst: XREF_DST,
      added_by: "u-human",
      actor_type: "human",
    });
    expect(rows.find((r) => r.kind === "xref" && r.src === DECL)).toMatchObject({
      src: DECL,
      dst: XREF_DST,
      added_by: "u-human",
      actor_type: "human",
    });
    expect(rows.find((r) => r.kind === "formalizes")).toMatchObject({
      src: CONCEPT,
      dst: DECL,
      added_by: "u-human",
      actor_type: "human",
    });

    const queue = await lmfdbQueue(h);
    expect(queue.updated).toBeTruthy();
    expect(queue.items).toHaveLength(1);
    expect(queue.items[0]).toMatchObject({
      db: "lmfdb",
      id: "group.abelian",
      concept_qid: CONCEPT,
      decl: "CommGroup",
      file: "Mathlib/Algebra/Group/Defs.lean",
      status: "brain",
      source: "quickstatements",
      priority_source: "community-bulk",
      provenance_tier: "community-human",
      actor_type: "human",
      added_by: "u-human",
    });
  });

  it("is idempotent on duplicate submissions", async () => {
    const h = harness();
    expect((await postQuick(h, { db: "lmfdb", items: [QUICK_ITEM] }, { user: "u-human" })).status).toBe(200);
    const second = await postQuick(h, { db: "lmfdb", items: [QUICK_ITEM] }, { user: "u-human" });
    expect(second.status).toBe(200);
    const j = await second.json() as { rows: Array<Record<string, unknown>> };
    const edges = j.rows[0].edges as Array<Record<string, unknown>>;
    expect(edges).toHaveLength(3);
    expect(edges.every((e) => e.duplicate === true)).toBe(true);
    expect(edgeRows(h)).toHaveLength(3);
    expect((await lmfdbQueue(h)).items).toHaveLength(1);
  });

  it("accepts a generic Mathlib-to-nLab connection without touching the LMFDB queue", async () => {
    const h = harness();
    const res = await postQuick(
      h,
      {
        databases: ["mathlib", "nlab"],
        items: [{ mathlib: "CommGroup", nlab: "abelian_group", file: "Mathlib/Algebra/Group/Defs.lean" }],
      },
      { user: "u-human" },
    );
    expect(res.status).toBe(200);
    const j = await res.json() as { accepted: number; queue_count: number; rows: Array<Record<string, unknown>> };
    expect(j.accepted).toBe(1);
    expect(j.queue_count).toBe(0);
    expect(j.rows[0]).toMatchObject({ ok: true, ids: { mathlib: "CommGroup", nlab: "abelian_group" }, queued: false });
    expect(edgeRows(h)).toHaveLength(1);
    expect(edgeRows(h)[0]).toMatchObject({
      src: DECL,
      dst: "xref:nlab:abelian_group",
      kind: "xref",
      actor_type: "human",
    });
    expect((await lmfdbQueue(h)).items).toHaveLength(0);
  });

  it("rejects selections with no Brain-node database", async () => {
    const h = harness();
    const res = await postQuick(
      h,
      { databases: ["lmfdb", "nlab"], items: [{ lmfdb: "group.abelian", nlab: "abelian_group" }] },
      { user: "u-human" },
    );
    expect(res.status).toBe(400);
    expect((await res.json() as Record<string, unknown>).error).toBe("select at least one database with Brain nodes");
    expect(edgeRows(h)).toHaveLength(0);
  });

  it("also accepts direct LMFDB-to-Mathlib rows without a Wikidata QID", async () => {
    const h = harness();
    const res = await postQuick(
      h,
      { db: "lmfdb", items: [{ lmfdb_id: "group.abelian", decl: "CommGroup", file: "Mathlib/Algebra/Group/Defs.lean" }] },
      { user: "u-human" },
    );
    expect(res.status).toBe(200);
    const rows = edgeRows(h);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      src: DECL,
      dst: XREF_DST,
      kind: "xref",
      added_by: "u-human",
      actor_type: "human",
    });
    const queue = await lmfdbQueue(h);
    expect(queue.items).toHaveLength(1);
    expect(queue.items[0]).toMatchObject({
      id: "group.abelian",
      decl: "CommGroup",
      brain_node: DECL,
      decl_node: DECL,
      status: "brain",
    });
    expect(queue.items[0].concept_qid).toBeUndefined();
  });

  it("requires bearer callers to declare human vs AI provenance", async () => {
    const h = harness();
    const noType = await postQuick(h, { db: "lmfdb", items: [QUICK_ITEM] }, { bearer: PIPELINE_TOKEN, origin: null });
    expect(noType.status).toBe(400);
    expect(edgeRows(h)).toHaveLength(0);

    const asAi = await postQuick(
      h,
      { db: "lmfdb", actor_type: "ai", items: [QUICK_ITEM] },
      { bearer: PIPELINE_TOKEN, origin: null },
    );
    expect(asAi.status).toBe(200);
    expect(edgeRows(h).every((r) => r.added_by === "pipeline" && r.actor_type === "ai")).toBe(true);
    expect((await lmfdbQueue(h)).items[0]).toMatchObject({
      actor_type: "ai",
      added_by: "pipeline",
      provenance_tier: "community-ai",
    });
  });

  it("lets a logged-in browser user mark a bulk submission as AI-generated", async () => {
    const h = harness();
    const res = await postQuick(h, { db: "lmfdb", actor_type: "ai", items: [QUICK_ITEM] }, { user: "u-human" });
    expect(res.status).toBe(200);
    expect(edgeRows(h).every((r) => r.added_by === "u-human" && r.actor_type === "ai")).toBe(true);
    expect((await lmfdbQueue(h)).items[0]).toMatchObject({
      actor_type: "ai",
      added_by: "u-human",
      provenance_tier: "community-ai",
    });
  });

  it("mints a QID the Brain does not have yet, in the same batch as the row's edges", async () => {
    const h = harness();
    const QID = "Q5530428";
    const WD = { [QID]: { labels: { en: { value: "GNS construction" } }, descriptions: { en: { value: "a construction" } } } };
    const res = await withWikidata(WD, () => postQuick(
      h,
      { databases: ["wikidata", "nlab"], items: [{ qid: QID, nlab: "gns_construction", note: "same object" }] },
      { user: "u-human" },
    ));
    expect(res.status).toBe(200);
    const nodes = h.db.prepare("SELECT * FROM brain_nodes").all() as Array<Record<string, unknown>>;
    expect(nodes).toHaveLength(1);
    expect(nodes[0]).toMatchObject({
      id: QID, label: "GNS construction", description: "a construction", node_type: "concept",
      added_by: "u-human", actor_type: "human", status: "live", version: 1,
    });
    expect(edgeRows(h)).toHaveLength(1);
    expect(edgeRows(h)[0]).toMatchObject({ src: QID, dst: "xref:nlab:gns_construction", kind: "xref" });
  });

  it("keeps good rows when another row is invalid", async () => {
    const h = harness();
    const res = await postQuick(
      h,
      { db: "lmfdb", items: [QUICK_ITEM, { ...QUICK_ITEM, lmfdb_id: "bad id with spaces" }] },
      { user: "u-human" },
    );
    expect(res.status).toBe(200);
    const j = await res.json() as { accepted: number; failed: number; rows: Array<Record<string, unknown>> };
    expect(j.accepted).toBe(1);
    expect(j.failed).toBe(1);
    expect(j.rows.some((r) => r.ok === false && String(r.error).includes("LMFDB"))).toBe(true);
    expect(edgeRows(h)).toHaveLength(3);
    expect((await lmfdbQueue(h)).items).toHaveLength(1);
  });
});

// D1 allows 1,000 queries per Worker invocation and counts one batch() as one
// query. A row's writes therefore go out as a single batch: a 100-row submit
// over every database costs about 100 D1 calls, not the ~6,000 statements it
// would cost one at a time, and a row that fails writes nothing at all.
describe("POST /api/brain/quickstatements — the D1 per-invocation budget", () => {
  const ALL_DBS = [
    "mathlib", "wikidata", "brain", "lmfdb", "nlab", "mathworld",
    "proofwiki", "oeis", "stacks", "kerodon", "dlmf", "metamath",
  ];

  // Count what the Worker spends against the per-invocation cap: a prepared
  // statement run on its own is one query, and a batch is one query however
  // many statements it carries. Mirrors the D1 surface helpers/d1shim.ts
  // implements, so the real app code runs unchanged underneath.
  interface D1Spend { calls: () => number; statements: () => number }
  interface Stmt {
    bind: (...args: unknown[]) => Stmt;
    run: () => unknown;
    all: () => unknown;
    raw: () => unknown;
    first: () => unknown;
    _batchExec: () => unknown;
  }
  interface Db { prepare: (sql: string) => Stmt; batch: (stmts: Stmt[]) => unknown }

  function countD1(env: Env): D1Spend {
    let calls = 0;
    let statements = 0;
    const real = env.DB as unknown as Db;
    const wrap = (st: Stmt): Stmt => ({
      bind: (...args: unknown[]) => wrap(st.bind(...args)),
      run: () => { calls++; statements++; return st.run(); },
      all: () => { calls++; statements++; return st.all(); },
      raw: () => { calls++; statements++; return st.raw(); },
      first: () => { calls++; statements++; return st.first(); },
      _batchExec: () => st._batchExec(),
    });
    const counting: Db = {
      prepare: (sql) => wrap(real.prepare(sql)),
      batch: (stmts) => { calls++; statements += stmts.length; return real.batch(stmts); },
    };
    (env as unknown as { DB: unknown }).DB = counting;
    return { calls: () => calls, statements: () => statements };
  }

  // Every row names its own nodes, so no two rows write the same edge. The
  // extra organs make each one resolvable against the oracle without a
  // Wikidata round trip (the network is blocked in these tests).
  function bulkHarness(rows: number): Harness {
    const organs: Record<string, string> = {};
    for (let i = 0; i < rows; i++) {
      organs[`decl:Mathlib:Bulk${i}`] = MODULE_CELL;
      organs[`Q90${i}`] = MODULE_CELL;
      organs[`decl:Mathlib:Node${i}`] = MODULE_CELL;
    }
    const h = setup();
    installBrainAssets(h.env, organs);
    agreed(h);
    return h;
  }

  function bulkRow(i: number): Record<string, string> {
    return {
      mathlib: `Bulk${i}`,
      file: "Mathlib/Algebra/Group/Defs.lean",
      wikidata: `Q90${i}`,
      brain: `decl:Mathlib:Node${i}`,
      lmfdb: `group.bulk${i}`,
      nlab: `nlab_${i}`,
      mathworld: `MathWorld${i}`,
      proofwiki: `Definition:Bulk_${i}`,
      oeis: `A00000${i}`,
      stacks: `0A0${i}`,
      kerodon: `tag/00${i}`,
      dlmf: `1.2.E${i}`,
      metamath: `df-bulk${i}`,
      note: "bulk connection",
    };
  }

  it("spends about one D1 call a row on a 100-row all-databases submit", async () => {
    const h = bulkHarness(100);
    const spend = countD1(h.env);
    const items = Array.from({ length: 100 }, (_, i) => bulkRow(i));
    const res = await postQuick(h, { databases: ALL_DBS, items }, { user: "u-human" });
    expect(res.status).toBe(200);
    const j = await res.json() as { accepted: number; failed: number; rows: Array<Record<string, unknown>> };
    expect(j).toMatchObject({ accepted: 100, failed: 0 });
    // 3 node databases and 9 external ones: 3×9 xref edges + 3 node pairs.
    expect((j.rows[0].edges as unknown[])).toHaveLength(30);
    expect(edgeRows(h)).toHaveLength(3000);
    // One batch a row, plus the handful of setup reads (the session lookup).
    expect(spend.calls()).toBeLessThanOrEqual(110);
    expect(spend.calls()).toBeGreaterThanOrEqual(100);
    // …carrying far more statements than the 1,000 the old one-at-a-time path
    // would have spent as separate queries.
    expect(spend.statements()).toBeGreaterThan(5000);
  });

  it("reports edges that already exist as duplicates, and new ones as new", async () => {
    const h = harness();
    const first = await postQuick(h, { databases: ["mathlib", "lmfdb"], items: [QUICK_ITEM] }, { user: "u-human" });
    expect(first.status).toBe(200);
    expect(edgeRows(h)).toHaveLength(1);

    // The same Mathlib↔LMFDB pair plus a Wikidata column: that one edge is
    // already live, the other two are new.
    const second = await postQuick(h, { databases: ["mathlib", "wikidata", "lmfdb"], items: [QUICK_ITEM] }, { user: "u-human" });
    expect(second.status).toBe(200);
    const edges = (await second.json() as { rows: Array<{ edges: Array<Record<string, unknown>> }> }).rows[0].edges;
    expect(edges).toHaveLength(3);
    const repeated = edges.filter((e) => e.duplicate === true);
    expect(repeated).toHaveLength(1);
    expect(repeated[0]).toMatchObject({ src: DECL, dst: XREF_DST, kind: "xref" });
    expect(edgeRows(h)).toHaveLength(3);
    // The duplicate keeps the id the first submit minted.
    expect(repeated[0].id).toBe(edgeRows(h).find((r) => r.src === DECL && r.kind === "xref")!.id);
  });

  it("writes nothing for a row whose batch fails, and says why", async () => {
    const h = harness();
    // Refuse one of the row's edges inside the transaction, the way a
    // constraint or a D1 limit would.
    h.db.exec(
      "CREATE TRIGGER refuse_nlab BEFORE INSERT ON brain_edges WHEN NEW.dst LIKE 'xref:nlab:%' " +
      "BEGIN SELECT RAISE(ABORT, 'refused by the test trigger'); END",
    );
    const res = await postQuick(
      h,
      {
        databases: ["mathlib", "wikidata", "nlab"],
        items: [{ mathlib: "CommGroup", file: "Mathlib/Algebra/Group/Defs.lean", qid: CONCEPT, nlab: "abelian_group" }],
      },
      { user: "u-human" },
    );
    expect(res.status).toBe(400);
    const j = await res.json() as { accepted: number; failed: number; rows: Array<Record<string, unknown>> };
    expect(j).toMatchObject({ accepted: 0, failed: 1 });
    expect(String(j.rows[0].error)).toContain("could not save row");
    expect(String(j.rows[0].error)).toContain("refused by the test trigger");
    // The formalizes edge and the Wikidata xref were in the same batch: the
    // row left no partial write behind.
    expect(edgeRows(h)).toHaveLength(0);
  });
});

// The routes that store a contributor's own prose take the CC0 grant once, at
// their first edit, exactly as the formalization reader's overlay does: the
// evidence note is published through GET /api/brain/edges under a CC0 claim.
describe("the CC0 grant on Brain writes", () => {
  function ungreed(): Harness {
    const h = setup();
    installBrainAssets(h.env);
    return h;
  }

  const agreementRow = (h: Harness) =>
    h.db.prepare("SELECT * FROM user_agreements WHERE user_id = 'u-human'").get() as Record<string, unknown> | undefined;

  it("asks a person for it at their first edge, then records it", async () => {
    const h = ungreed();
    const refused = await postEdge(h, REL, { user: "u-human" });
    expect(refused.status).toBe(403);
    const body = await refused.json() as Record<string, unknown>;
    expect(body).toMatchObject({ cc0_required: true, cc0_version: CC0_AGREEMENT_VERSION });
    expect(body.cc0_text).toBe(CC0_AGREEMENT_TEXT);
    expect(String(body.cc0_text)).toContain("Brain connections");
    expect(edgeRows(h)).toHaveLength(0);
    expect(agreementRow(h)).toBeUndefined();

    const accepted = await postEdge(h, { ...REL, cc0: true }, { user: "u-human" });
    expect(accepted.status).toBe(201);
    expect(edgeRows(h)).toHaveLength(1);
    // The Brain's form carries no Turnstile widget, so the acceptance is
    // recorded unverified rather than refused.
    expect(agreementRow(h)).toMatchObject({ cc0_version: CC0_AGREEMENT_VERSION, verified_at: null });

    // Asked once: the next edge needs no checkbox.
    const other = { ...REL, dst: XREF_DST, kind: "xref" };
    expect((await postEdge(h, other, { user: "u-human" })).status).toBe(201);
  });

  it("asks for it on a bulk submit too, and writes no row until it is given", async () => {
    const h = ungreed();
    const refused = await postQuick(h, { db: "lmfdb", items: [QUICK_ITEM] }, { user: "u-human" });
    expect(refused.status).toBe(403);
    expect(await refused.json()).toMatchObject({ cc0_required: true });
    expect(edgeRows(h)).toHaveLength(0);
    expect((await lmfdbQueue(h)).items).toHaveLength(0);

    const accepted = await postQuick(h, { db: "lmfdb", cc0: true, items: [QUICK_ITEM] }, { user: "u-human" });
    expect(accepted.status).toBe(200);
    expect(edgeRows(h)).toHaveLength(3);
    expect(agreementRow(h)).toMatchObject({ cc0_version: CC0_AGREEMENT_VERSION });
  });

  // `cc0-1.0/2026-11` released "the mapping data I add here — the declaration
  // names, relations and notes": the overlay's passage tags, not an edge's
  // note. It is therefore no grant for a Brain connection, and a contributor
  // still on it is asked once for the wording that names them.
  it("asks a person on the superseded wording for the one that names Brain connections", async () => {
    const h = ungreed();
    const acceptedAt = Date.now() - 86_400_000;
    h.db.prepare("INSERT INTO user_agreements (user_id, cc0_version, accepted_at, verified_at) VALUES (?,?,?,?)")
      .run("u-human", "cc0-1.0/2026-11", acceptedAt, acceptedAt);

    const refused = await postEdge(h, REL, { user: "u-human" });
    expect(refused.status).toBe(403);
    const body = await refused.json() as Record<string, unknown>;
    expect(body).toMatchObject({ cc0_required: true, cc0_version: CC0_AGREEMENT_VERSION });
    expect(String(body.error)).toContain("updated");
    expect(String(body.cc0_text)).toContain("Brain connections");
    expect(edgeRows(h)).toHaveLength(0);
    expect(agreementRow(h)).toMatchObject({ cc0_version: "cc0-1.0/2026-11" });

    // Accepting is the same one-checkbox flow, and updates the row in place:
    // the new version, the ORIGINAL acceptance time, the verification kept.
    const accepted = await postEdge(h, { ...REL, cc0: true }, { user: "u-human" });
    expect(accepted.status).toBe(201);
    expect(agreementRow(h)).toMatchObject({
      cc0_version: CC0_AGREEMENT_VERSION, accepted_at: acceptedAt, verified_at: acceptedAt,
    });
    // Asked once: the next edge goes straight through.
    expect((await postEdge(h, { ...REL, dst: XREF_DST, kind: "xref" }, { user: "u-human" })).status).toBe(201);
  });


  it("does not gate a new concept node or a delete, which store no text of the contributor's", async () => {
    const h = ungreed();
    const node = await post(h.env, "/api/brain/node", { qid: CONCEPT }, { user: "u-human" });
    expect(node.status).toBe(200); // already a static node: a no-op, not a 403
    expect(agreementRow(h)).toBeUndefined();

    agreed(h);
    const edge = (await (await postEdge(h, REL, { user: "u-human" })).json()) as { id: string };
    h.db.prepare("DELETE FROM user_agreements WHERE user_id = 'u-human'").run();
    expect((await post(h.env, `/api/brain/edge/${edge.id}/delete`, {}, { user: "u-human" })).status).toBe(200);
  });

  it("does not gate the pipeline bearer, which writes under the operator's own terms", async () => {
    const h = ungreed();
    const res = await postEdge(h, { ...REL, actor_type: "ai" }, { bearer: PIPELINE_TOKEN, origin: null });
    expect(res.status).toBe(201);
    expect(h.db.prepare("SELECT COUNT(*) AS n FROM user_agreements").get()).toMatchObject({ n: 0 });
  });
});

describe("POST /api/brain/edge — new Wikidata concept nodes", () => {
  const QID = "Q5530428";
  const WD = { [QID]: { labels: { en: { value: "Gelfand–Naimark–Segal construction" } }, descriptions: { en: { value: "a construction" } } } };
  const nodeRows = (h: Harness) => h.db.prepare("SELECT * FROM brain_nodes").all() as Array<Record<string, unknown>>;

  it("mints a validated Wikidata QID as a community node and links to it", async () => {
    const h = harness();
    const res = await withWikidata(WD, () =>
      postEdge(h, { src: DECL, dst: QID, kind: "formalizes", evidence: { note: "gns" } }, { user: "u-human" }));
    expect(res.status).toBe(201);
    expect(edgeRows(h)[0]).toMatchObject({ src: DECL, dst: QID, kind: "formalizes" });
    const nodes = nodeRows(h);
    expect(nodes).toHaveLength(1);
    expect(nodes[0]).toMatchObject({ id: QID, label: "Gelfand–Naimark–Segal construction", added_by: "u-human", status: "live" });
  });

  it("rejects a QID Wikidata doesn't know (400, no node minted)", async () => {
    const h = harness();
    const res = await withWikidata({}, () =>
      postEdge(h, { src: DECL, dst: "Q999999999", kind: "formalizes", evidence: { note: "x" } }, { user: "u-human" }));
    expect(res.status).toBe(400);
    expect(nodeRows(h)).toHaveLength(0);
  });

  it("the overlay returns node_labels for a minted QID so it renders with its name", async () => {
    const h = harness();
    await withWikidata(WD, () =>
      postEdge(h, { src: DECL, dst: QID, kind: "formalizes", evidence: { note: "gns" } }, { user: "u-human" }));
    const j = (await (await get(h.env, `/api/brain/edges?id=${encodeURIComponent(DECL)}`)).json()) as { node_labels: Record<string, string> };
    expect(j.node_labels[QID]).toBe("Gelfand–Naimark–Segal construction");
  });

  it("a QID that IS already a static node links directly (no mint)", async () => {
    const h = harness();
    const res = await withWikidata(WD, () =>
      postEdge(h, { src: DECL, dst: CONCEPT, kind: "formalizes", evidence: { note: "x" } }, { user: "u-human" }));
    expect(res.status).toBe(201);
    expect(nodeRows(h)).toHaveLength(0); // CONCEPT is already a node
  });
});

describe("POST /api/brain/node — introduce a concept (no edge)", () => {
  const QID = "Q5530428";
  const WD = { [QID]: { labels: { en: { value: "Gelfand–Naimark–Segal construction" } }, descriptions: { en: { value: "a construction" } } } };
  const nodeRows = (h: Harness) => h.db.prepare("SELECT * FROM brain_nodes").all() as Array<Record<string, unknown>>;
  const addNode = (h: Harness, body: Record<string, unknown>, opts = {}) => post(h.env, "/api/brain/node", body, opts);

  it("mints a validated Wikidata concept standalone (no edge created)", async () => {
    const h = harness();
    const res = await withWikidata(WD, () => addNode(h, { qid: QID }, { user: "u-human" }));
    expect(res.status).toBe(201);
    expect((await res.json() as Record<string, unknown>).label).toBe("Gelfand–Naimark–Segal construction");
    expect(nodeRows(h)).toHaveLength(1);
    expect(edgeRows(h)).toHaveLength(0); // it's a NODE, not an edge
  });

  it("rejects a QID Wikidata doesn't know, and a non-QID", async () => {
    const h = harness();
    expect((await withWikidata({}, () => addNode(h, { qid: "Q999999999" }, { user: "u-human" }))).status).toBe(400);
    expect((await addNode(h, { qid: "not-a-qid" }, { user: "u-human" })).status).toBe(400);
    expect((await addNode(h, { qid: QID }, {})).status).toBe(401); // login required
  });

  it("is a no-op when the QID is already a static node", async () => {
    const h = harness();
    const res = await withWikidata(WD, () => addNode(h, { qid: CONCEPT }, { user: "u-human" }));
    expect(res.status).toBe(200);
    expect((await res.json() as Record<string, unknown>).existing).toBe(true);
    expect(nodeRows(h)).toHaveLength(0);
  });

  it("does not mint a static QID when the mandatory alias oracle is unavailable", async () => {
    const h = setup();
    installBrainFixture(h.env, { aliases: null });
    const res = await withWikidata(WD, () => addNode(h, { qid: CONCEPT }, { user: "u-human" }));
    expect(res.status).toBe(503);
    expect(nodeRows(h)).toHaveLength(0);
  });

  it("the overlay returns `self` for a community node, cleared on delete", async () => {
    const h = harness();
    await withWikidata(WD, () => addNode(h, { qid: QID }, { user: "u-human" }));
    const j1 = (await (await get(h.env, `/api/brain/edges?id=${QID}`)).json()) as { self: Record<string, unknown> | null };
    expect(j1.self).toMatchObject({ id: QID, label: "Gelfand–Naimark–Segal construction", added_by: "u-human" });
    // any logged-in user can soft-delete it (gravestone)
    const del = await post(h.env, `/api/brain/node/${QID}/delete`, {}, { user: "u-admin" });
    expect(del.status).toBe(200);
    expect(nodeRows(h)[0]).toMatchObject({ status: "deleted", deleted_by: "u-admin" });
    const j2 = (await (await get(h.env, `/api/brain/edges?id=${QID}`)).json()) as { self: unknown };
    expect(j2.self).toBeNull();
  });
});

describe("GET /api/brain/edges — xref-shared cross-pollination", () => {
  const PAGE = "xref:lmfdb_knowl:group.abelian";
  const NODE_B = "Q11650"; // a second node

  function sharedOf(j: unknown): Array<{ node: string; via: string; source: string }> {
    return ((j as { shared?: Array<{ node: string; via: string; source: string }> }).shared) || [];
  }

  it("community↔community: two nodes both community-xref'd to one page infer each other", async () => {
    const h = setup();
    installBrainAssets(h.env);
    agreed(h);
    await postEdge(h, { src: CONCEPT, dst: PAGE, kind: "xref", evidence: { note: "a" } }, { user: "u-human" });
    await postEdge(h, { src: DECL, dst: PAGE, kind: "xref", evidence: { note: "b" } }, { user: "u-human" });
    const shared = sharedOf(await (await get(h.env, `/api/brain/edges?id=${encodeURIComponent(CONCEPT)}`)).json());
    expect(shared.some((s) => s.node === DECL && s.source === "community" && s.via === PAGE)).toBe(true);
  });

  it("community→static: a community xref onto a page a STATIC node already holds, bridges both ways", async () => {
    const h = setup();
    // NODE_B carries a STATIC xref to PAGE: it appears in the reverse index
    // (page → nodes), whose inversion is also how the overlay finds NODE_B's
    // own static pages — the v2 shard-entry edge lists are gone.
    installBrainAssets(h.env, { [NODE_B]: "cell:Q11650" }, { [PAGE]: [NODE_B] });
    agreed(h);
    await postEdge(h, { src: CONCEPT, dst: PAGE, kind: "xref", evidence: { note: "same object" } }, { user: "u-human" });
    // viewing CONCEPT surfaces the static NODE_B
    const sA = sharedOf(await (await get(h.env, `/api/brain/edges?id=${encodeURIComponent(CONCEPT)}`)).json());
    expect(sA.some((s) => s.node === NODE_B && s.source === "static" && s.via === PAGE)).toBe(true);
    // viewing NODE_B surfaces the community CONCEPT
    const sB = sharedOf(await (await get(h.env, `/api/brain/edges?id=${encodeURIComponent(NODE_B)}`)).json());
    expect(sB.some((s) => s.node === CONCEPT && s.source === "community")).toBe(true);
  });

  it("does not reuse an xref inversion after selector rollover", async () => {
    const h = setup();
    installBrainAssets(h.env, { [NODE_B]: "cell:Q11650" }, { [PAGE]: [NODE_B] });
    agreed(h);
    const first = sharedOf(await (await get(h.env, `/api/brain/edges?id=${encodeURIComponent(NODE_B)}`)).json());
    expect(first).toEqual([]);

    const releaseB = installBrainFixture(h.env, {
      releaseVariant: "xref-rollover",
      aliases: { ...DEFAULT_ALIASES, organs: { ...DEFAULT_ALIASES.organs, [NODE_B]: "cell:Q11650" } },
      xrefIndex: { [PAGE]: [CONCEPT] },
    });
    await postEdge(h, { src: NODE_B, dst: PAGE, kind: "xref", evidence: { note: "release B" } }, { user: "u-human" });
    const response = await get(h.env, `/api/brain/edges?id=${encodeURIComponent(NODE_B)}`);
    const body = await response.json() as Record<string, unknown>;
    expect(body.release_id).toBe(releaseB.releaseId);
    expect(sharedOf(body).some((s) => s.node === CONCEPT)).toBe(true);
  });

  it("evicts the oldest xref index and inversion after a third release", async () => {
    const h = setup();
    const paths: string[] = [];
    const observed = (path: string) => paths.push(path);
    const install = (releaseVariant: string) => installBrainFixture(h.env, {
      releaseVariant,
      xrefIndex: { [PAGE]: [CONCEPT] },
      onAssetPath: observed,
    });

    const releaseA = install("xref-lru-a");
    expect((await get(h.env, `/api/brain/edges?id=${encodeURIComponent(CONCEPT)}`)).status).toBe(200);
    const releaseB = install("xref-lru-b");
    expect((await get(h.env, `/api/brain/edges?id=${encodeURIComponent(CONCEPT)}`)).status).toBe(200);
    install("xref-lru-a");
    expect((await get(h.env, `/api/brain/edges?id=${encodeURIComponent(CONCEPT)}`)).status).toBe(200);
    const releaseC = install("xref-lru-c");
    expect((await get(h.env, `/api/brain/edges?id=${encodeURIComponent(CONCEPT)}`)).status).toBe(200);

    expect(_brainEditCacheReleaseIds()).toEqual({
      xrefIndex: [releaseA.releaseId, releaseC.releaseId],
      xrefPagesByNode: [releaseA.releaseId, releaseC.releaseId],
    });

    install("xref-lru-b");
    expect((await get(h.env, `/api/brain/edges?id=${encodeURIComponent(CONCEPT)}`)).status).toBe(200);
    const releaseBXref = `/assets/brain/releases/${releaseB.manifestSha256}/xref_index.json`;
    expect(paths.filter((path) => path === releaseBXref)).toHaveLength(2);
  });

  it("returns 503 for a missing xref index and retries it on the next request", async () => {
    const h = setup();
    const paths: string[] = [];
    installBrainFixture(h.env, {
      xrefIndex: { [PAGE]: [NODE_B, CONCEPT] },
      xrefIndexFailures: 1,
      onAssetPath: (path) => paths.push(path),
    });

    const first = await get(h.env, `/api/brain/edges?id=${encodeURIComponent(NODE_B)}`);
    expect(first.status).toBe(503);
    expect(await first.json()).toMatchObject({ ok: false, error: "brain release unavailable" });

    const second = await get(h.env, `/api/brain/edges?id=${encodeURIComponent(NODE_B)}`);
    expect(second.status).toBe(200);
    expect(sharedOf(await second.json()).some((s) => s.node === CONCEPT)).toBe(true);
    expect(paths.filter((path) => path.endsWith("/xref_index.json"))).toHaveLength(2);
  });

  it("no false partners: a node whose page is unique has no shared", async () => {
    const h = setup();
    installBrainAssets(h.env);
    await postEdge(h, { src: CONCEPT, dst: "xref:nlab:unique_thing", kind: "xref", evidence: { note: "x" } }, { user: "u-human" });
    expect(sharedOf(await (await get(h.env, `/api/brain/edges?id=${encodeURIComponent(CONCEPT)}`)).json())).toHaveLength(0);
  });
});

describe("DELETE /api/brain/edge/:id (soft-delete gravestone)", () => {
  it("any logged-in user can delete; the row becomes a gravestone and drops from the overlay", async () => {
    const h = harness();
    const created = (await (await postEdge(h, REL, { user: "u-human" })).json()) as { id: string };
    // a DIFFERENT user deletes it (decision (a): anyone logged in)
    const del = await app.request(
      `/api/brain/edge/${created.id}`,
      { method: "DELETE", headers: { Origin: "http://localhost", Cookie: "wl_dev_user=u-admin" } },
      h.env,
    );
    expect(del.status).toBe(200);
    expect((await del.json() as Record<string, unknown>).deleted_by).toBe("u-admin");
    const row = edgeRows(h)[0];
    expect(row).toMatchObject({ status: "deleted", deleted_by: "u-admin" });
    expect(row.deleted_at).not.toBeNull();
    // overlay no longer serves it
    const overlay = await get(h.env, `/api/brain/edges?id=${encodeURIComponent(CONCEPT)}`);
    expect(((await overlay.json()) as { edges: unknown[] }).edges).toHaveLength(0);
  });

  it("re-adding after delete makes a NEW row (gravestone preserved)", async () => {
    const h = harness();
    const created = (await (await postEdge(h, REL, { user: "u-human" })).json()) as { id: string };
    await post(h.env, `/api/brain/edge/${created.id}/delete`, {}, { user: "u-human" });
    const readd = await postEdge(h, REL, { user: "u-human" });
    expect(readd.status).toBe(201);
    expect(edgeRows(h)).toHaveLength(2); // gravestone + new live
  });

  it("404s an unknown edge id and 400s a malformed id", async () => {
    const h = harness();
    expect((await post(h.env, `/api/brain/edge/aaaaaaaaaaaa/delete`, {}, { user: "u-human" })).status).toBe(404);
    expect((await post(h.env, `/api/brain/edge/nothex/delete`, {}, { user: "u-human" })).status).toBe(400);
  });

  it("requires login to delete", async () => {
    const h = harness();
    const created = (await (await postEdge(h, REL, { user: "u-human" })).json()) as { id: string };
    expect((await post(h.env, `/api/brain/edge/${created.id}/delete`, {}, {})).status).toBe(401);
  });
});
