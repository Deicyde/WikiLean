// Community brain edges (docs/BRAIN-EDITS-ROADMAP.md): POST/GET/DELETE
// /api/brain/edge(s). Exercises auth (login required; OAuth forces human, bearer
// must declare actor_type), origin/rate-limit guards, shard + kind + xref
// validation, dedupe, the added-by/human-AI provenance, and the soft-delete
// gravestone. Node existence is oracle'd against a shim brain manifest built
// with the real declShardKey so the shard resolution matches production.

import { describe, it, expect, beforeEach } from "vitest";
import { setup, post, get, blockNetwork, PIPELINE_TOKEN, type Harness } from "./helpers/harness.js";
import { app } from "../src/index.js";
import { declShardKey } from "../src/decl.js";
import { _resetBrainEditCaches } from "../src/brain-edits.js";
import type { Env } from "../src/env.js";

blockNetwork();
beforeEach(() => _resetBrainEditCaches());   // clear the isolate-lifetime xref index

const CONCEPT = "Q181296"; // abelian group
const DECL = "decl:Mathlib:CommGroup";
const XREF_DST = "xref:lmfdb_knowl:group.abelian";
const UNKNOWN = "Q999999999";

// Serve a brain manifest + shards for `nodeIds` so brainNodeExists resolves them.
// `nodeXrefs` seeds each node's STATIC xref edges (node id → external pages) so
// the shard entry has them; `xrefIndex` is the reverse page → nodes index.
function installBrainAssets(
  env: Env,
  nodeIds: string[],
  nodeXrefs: Record<string, string[]> = {},
  xrefIndex: Record<string, string[]> = {},
): void {
  const scheme = { min_len: 2, max_len: 2, pad: "_" };
  const shards: Record<string, number> = {};
  const data: Record<string, Record<string, unknown>> = {};
  for (const id of nodeIds) {
    const key = declShardKey(id, 2);
    shards[key] = (shards[key] ?? 0) + 1;
    const out = (nodeXrefs[id] || []).map((pg) => ({ id: pg, kind: "xref" }));
    (data[key] ??= {})[id] = { node: { id, label: id }, edges: { out, in: [] } };
  }
  const manifest = { scheme, shards, prov: [], roots: [], _meta: { generated_at: "2026-07-05" } };
  (env as unknown as { ASSETS: { fetch: (r: Request) => Promise<Response> } }).ASSETS = {
    fetch: async (req: Request) => {
      const path = new URL(req.url).pathname;
      if (path === "/assets/brain/manifest.json")
        return new Response(JSON.stringify(manifest), { status: 200 });
      if (path === "/assets/brain/xref_index.json")
        return new Response(JSON.stringify(xrefIndex), { status: 200 });
      const m = /^\/assets\/brain\/([a-z0-9_]+)\.json$/.exec(path);
      if (m && data[m[1]]) return new Response(JSON.stringify(data[m[1]]), { status: 200 });
      return new Response("not found", { status: 404 });
    },
  };
}

function harness(opts: Parameters<typeof setup>[0] = {}): Harness {
  const h = setup(opts);
  installBrainAssets(h.env, [CONCEPT, DECL]);
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
    installBrainAssets(h.env, [CONCEPT, DECL]);
    expect((await postEdge(h, REL, { user: "u-human" })).status).toBe(429);
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

describe("GET /api/brain/edges — xref-shared cross-pollination", () => {
  const PAGE = "xref:lmfdb_knowl:group.abelian";
  const NODE_B = "Q11650"; // a second node

  function sharedOf(j: unknown): Array<{ node: string; via: string; source: string }> {
    return ((j as { shared?: Array<{ node: string; via: string; source: string }> }).shared) || [];
  }

  it("community↔community: two nodes both community-xref'd to one page infer each other", async () => {
    const h = setup();
    installBrainAssets(h.env, [CONCEPT, DECL]);
    await postEdge(h, { src: CONCEPT, dst: PAGE, kind: "xref", evidence: { note: "a" } }, { user: "u-human" });
    await postEdge(h, { src: DECL, dst: PAGE, kind: "xref", evidence: { note: "b" } }, { user: "u-human" });
    const shared = sharedOf(await (await get(h.env, `/api/brain/edges?id=${encodeURIComponent(CONCEPT)}`)).json());
    expect(shared.some((s) => s.node === DECL && s.source === "community" && s.via === PAGE)).toBe(true);
  });

  it("community→static: a community xref onto a page a STATIC node already holds, bridges both ways", async () => {
    const h = setup();
    // NODE_B carries a STATIC xref to PAGE (seeded in its shard + the reverse index)
    installBrainAssets(h.env, [CONCEPT, DECL, NODE_B], { [NODE_B]: [PAGE] }, { [PAGE]: [NODE_B] });
    await postEdge(h, { src: CONCEPT, dst: PAGE, kind: "xref", evidence: { note: "same object" } }, { user: "u-human" });
    // viewing CONCEPT surfaces the static NODE_B
    const sA = sharedOf(await (await get(h.env, `/api/brain/edges?id=${encodeURIComponent(CONCEPT)}`)).json());
    expect(sA.some((s) => s.node === NODE_B && s.source === "static" && s.via === PAGE)).toBe(true);
    // viewing NODE_B surfaces the community CONCEPT
    const sB = sharedOf(await (await get(h.env, `/api/brain/edges?id=${encodeURIComponent(NODE_B)}`)).json());
    expect(sB.some((s) => s.node === CONCEPT && s.source === "community")).toBe(true);
  });

  it("no false partners: a node whose page is unique has no shared", async () => {
    const h = setup();
    installBrainAssets(h.env, [CONCEPT]);
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
