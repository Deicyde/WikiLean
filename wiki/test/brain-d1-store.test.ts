// The D1 store (src/brain-store-d1.ts, docs/BRAIN-D1.md step 3b). One parity
// release (brain-fixture.ts installBrainD1Fixture) lists the serving sidecar
// and still carries every static file, so each route can be served twice:
// through the static store and through the D1 store over an in-memory
// wikilean-brain built from the same values. The responses must be identical,
// byte for byte, except where the doc says D1 answers differently on purpose
// (search tiers, 2-character queries, path matching, short filter pages, the
// bounded statement pass, supercell traces, parts mode); those are pinned here
// one by one.

import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { Context } from "hono";
import type { Env } from "../src/env.js";
import { _resetBrainAssetMemo, resolveBrainRelease } from "../src/brain.js";
import { _resetXrefCaches, _setBrainStoreForTests, batchesOf, storeFor } from "../src/brain-store.js";
import { StaticStore } from "../src/brain-store-static.js";
import {
  _resetD1StoreMemo,
  _setFilterExaminedForTests,
  breadcrumbStmt,
  cellPageStmt,
  cellPagesStmt,
  D1Store,
  exactTierStmt,
  filterCellsStmts,
  filterSupercellsStmts,
  fragmentMatch,
  fragmentTierStmt,
  hotTierStmt,
  keyHitsStmt,
  labelRowsStmts,
  organPagesStmt,
  rangeTierStmt,
  READINESS_SQL,
  statementChunkStmt,
  supercellStmt,
  tracesStmt,
  xrefNodesStmt,
  xrefPagesStmt,
  type Stmt,
} from "../src/brain-store-d1.js";
import { blockNetwork, get, post, setup, type Harness } from "./helpers/harness.js";
import {
  ABELIAN,
  ABELIAN_CELL,
  ALGEBRA_SUPER,
  BASIS_CELL,
  COMM_DECL,
  DECL_CELL,
  EMPTY_CELL,
  FOURIER_DEAD_CELL,
  HUB_CELL,
  installBrainD1Fixture,
  LINALG_SUPER,
  LMFDB_PAGE,
  MODULE_CELL,
  MODULE_Q,
  type BrainD1FixtureOpts,
} from "./helpers/brain-fixture.js";

blockNetwork();

type Ctx = Context<{ Bindings: Env }>;
const enc = encodeURIComponent;

function resetMemos(): void {
  _resetBrainAssetMemo();
  _resetXrefCaches();
  _resetD1StoreMemo();
}

beforeEach(() => {
  resetMemos();
  _setFilterExaminedForTests(null);
});
afterEach(() => {
  _setBrainStoreForTests(null);
  _setFilterExaminedForTests(null);
});

interface Served {
  status: number;
  type: string | null;
  text: string;
  batches: number;
  statements: string[];
  h: Harness;
  db: DatabaseSync; // the fixture's wikilean-brain
  datasetId: string;
}

// Counts BRAIN_DB batches on an installed fixture.
function instrument(h: Harness): { count: () => number } {
  let batches = 0;
  const d1 = h.env.BRAIN_DB as unknown as { batch: (s: unknown[]) => Promise<unknown> };
  const batch = d1.batch.bind(d1);
  d1.batch = async (s: unknown[]) => {
    batches += 1;
    return batch(s);
  };
  return { count: () => batches };
}

// One route call on a fresh harness and fixture, through the static store
// (`static`) or through storeFor's own choice (`d1`).
async function serve(
  store: "static" | "d1",
  call: (h: Harness) => Promise<Response>,
  opts: BrainD1FixtureOpts = {},
): Promise<Served> {
  resetMemos();
  const statements: string[] = [];
  const h = setup();
  const fixture = installBrainD1Fixture(h.env, {
    ...opts,
    onStatement: (sql) => {
      statements.push(sql);
      opts.onStatement?.(sql);
    },
  });
  const counter = instrument(h);
  _setBrainStoreForTests(store === "static" ? (c, release) => new StaticStore(c, release) : null);
  try {
    const res = await call(h);
    return {
      status: res.status,
      type: res.headers.get("Content-Type"),
      text: await res.text(),
      batches: counter.count(),
      statements,
      h,
      db: fixture.db,
      datasetId: fixture.datasetId,
    };
  } finally {
    _setBrainStoreForTests(null);
  }
}

async function both(call: (h: Harness) => Promise<Response>, opts: BrainD1FixtureOpts = {}): Promise<[Served, Served]> {
  const s = await serve("static", call, opts);
  const d = await serve("d1", call, opts);
  return [s, d];
}

const getPath = (path: string) => (h: Harness) => get(h.env, path);
const mcp = (name: string, args: Record<string, unknown>) => (h: Harness) =>
  post(h.env, "/mcp", { jsonrpc: "2.0", id: 1, method: "tools/call", params: { name, arguments: args } });

// The neighborhood's second page, through the cursor the first page returned.
const secondPage = (first: string) => async (h: Harness) => {
  const one = (await (await get(h.env, first)).json()) as { next_cursor?: string };
  expect(one.next_cursor).toBeTruthy();
  return get(h.env, `${first}&cursor=${enc(one.next_cursor!)}`);
};

const PARITY: Array<[string, (h: Harness) => Promise<Response>]> = [
  ["cell by cell id", getPath(`/api/brain/cell?key=${enc(MODULE_CELL)}`)],
  ["cell by path", getPath(`/api/brain/cell?key=${enc(LINALG_SUPER)}`)],
  ["cell by QID", getPath(`/api/brain/cell?key=${MODULE_Q}`)],
  ["cell by decl id", getPath(`/api/brain/cell?key=${enc(COMM_DECL)}`)],
  ["cell by bare decl", getPath("/api/brain/cell?key=CommGroup")],
  ["cell by slug", getPath("/api/brain/cell?key=Vector_space")],
  ["cell by exact label", getPath(`/api/brain/cell?key=${enc("abelian GROUP")}`)],
  ["cell by a field concept's slug", getPath("/api/brain/cell?key=Linear_algebra")],
  ["cell by a non-ASCII aka", getPath(`/api/brain/cell?key=${enc("Größe der Gruppe")}`)],
  ["unknown key (404)", getPath("/api/brain/cell?key=Nothing_here")],
  ["dropped xref (404)", getPath(`/api/brain/cell?key=${enc("xref:mathworld:NotInTheBrain")}`)],
  ["unknown atom id (404)", getPath(`/api/brain/cell?key=${enc("cell:Q999")}`)],
  ["transfer informal to formal", getPath(`/api/brain/transfer?q=${MODULE_Q}&direction=informal_to_formal`)],
  ["transfer formal to informal", getPath("/api/brain/transfer?q=Module&direction=formal_to_informal")],
  ["transfer free-text fallback", getPath("/api/brain/transfer?q=Vector%20spa&direction=informal_to_formal")],
  ["neighborhood with kinds and min_w", getPath(`/api/brain/neighborhood?id=${enc(MODULE_CELL)}&kinds=depends,links&min_w=2`)],
  ["neighborhood cursor", secondPage(`/api/brain/neighborhood?id=${enc(MODULE_CELL)}&limit=1`)],
  ["neighborhood of a supercell without traces", getPath(`/api/brain/neighborhood?id=${enc(LINALG_SUPER)}&traces=0`)],
  ["neighborhood with a trimmed hydrated row", getPath(`/api/brain/neighborhood?id=${enc(ALGEBRA_SUPER)}`)],
  ["snippets", getPath(`/api/brain/snippets?id=${enc(ABELIAN_CELL)}`)],
  ["filter cells", getPath("/api/brain/filter?f=1")],
  ["filter cells under", getPath(`/api/brain/filter?f=1&under=${enc("path:Mathlib/Algebra")}`)],
  ["filter cells under a folder holding cells by p and by list", getPath(`/api/brain/filter?f=0&under=${enc(LINALG_SUPER)}`)],
  ["filter cells, paged", secondPage("/api/brain/filter?f=0&limit=2")],
  ["filter supercells under", getPath(`/api/brain/filter?f=1&type=supercell&under=${enc("path:Mathlib")}`)],
  ["search", getPath("/api/brain/search?q=vector%20spa")],
  ["search promotes an exact key", getPath(`/api/brain/search?q=${ABELIAN}`)],
  ["search supercells", getPath("/api/brain/search?q=linear%20alg&type=supercell")],
  ["search a non-ASCII name", getPath(`/api/brain/search?q=${enc("größe")}`)],
  ["search an atom id", getPath(`/api/brain/search?q=${enc(LINALG_SUPER)}`)],
  ["bridge", getPath("/api/brain/bridge?q=abelian%20group")],
  ["bridge statement pass", getPath(`/api/brain/bridge?q=${enc("every vector space has a basis")}`)],
  ["bridge by atom id", getPath(`/api/brain/bridge?q=${enc(MODULE_CELL)}`)],
  ["edges", getPath(`/api/brain/edges?id=${ABELIAN}`)],
  ["decl", getPath("/api/brain/decl?name=Basis")],
  ["POST node of a static node", (h) => post(h.env, "/api/brain/node", { qid: ABELIAN }, { user: "u-human" })],
  ["MCP brain_bridge", mcp("brain_bridge", { q: "abelian group" })],
  ["MCP brain_search", mcp("brain_search", { q: "vector" })],
  ["MCP brain_cell", mcp("brain_cell", { key: MODULE_Q })],
  ["MCP brain_transfer", mcp("brain_transfer", { q: MODULE_Q, direction: "informal_to_formal" })],
  ["MCP brain_neighborhood", mcp("brain_neighborhood", { id: MODULE_CELL })],
  ["MCP brain_snippets", mcp("brain_snippets", { id: ABELIAN_CELL })],
  ["MCP brain_filter", mcp("brain_filter", { f: 1 })],
  ["MCP decl_exists", mcp("decl_exists", { names: ["Basis", "CommGroup"] })],
  ["MCP brain_premises", mcp("brain_premises", { seeds: ["Module"] })],
];

describe("D1 parity: one release, two stores, identical responses", () => {
  it.each(PARITY)("%s", async (_label, call) => {
    const [s, d] = await both(call);
    expect(d.status).toBe(s.status);
    expect(d.type).toBe(s.type);
    expect(d.text).toBe(s.text);
    expect(s.batches).toBe(0);
    // brain_premises reads no Brain table: only the snapshot, a static file
    if (_label !== "MCP brain_premises") expect(d.batches).toBeGreaterThan(0);
  });

  it.each(PARITY)("%s, with the dataset's hot prefixes listed (threshold 2)", async (_label, call) => {
    const [s, d] = await both(call, { hotPrefixNames: 2, datasetVariant: "hot" });
    expect(d.status).toBe(s.status);
    expect(d.text).toBe(s.text);
  });

  it.each([["before"], ["after"]] as const)("holds with a decoy dataset seeded %s, which no response shows", async (decoy) => {
    for (const [, call] of PARITY) {
      const [s, d] = await both(call, { decoy });
      expect(d.text).toBe(s.text);
      expect(d.text).not.toContain("DECOY");
    }
  });

  // An edge's id is random, so compare everything else, and the rows written.
  it("POST /api/brain/edge writes the same edge", async () => {
    const call = (h: Harness) =>
      post(h.env, "/api/brain/edge", { src: ABELIAN, dst: COMM_DECL, kind: "formalizes", evidence: { note: "parity" } }, { user: "u-human" });
    const [s, d] = await both(call);
    expect(d.status).toBe(s.status);
    const strip = (text: string) => {
      const body = JSON.parse(text) as Record<string, unknown>;
      delete body.id;
      return body;
    };
    expect(strip(d.text)).toEqual(strip(s.text));
    const rows = (h: Harness) =>
      h.db.prepare("SELECT src, dst, kind, added_by, actor_type, status FROM brain_edges ORDER BY src, dst").all();
    expect(rows(d.h)).toEqual(rows(s.h));
    expect(rows(d.h).length).toBe(1);
  });
});

describe("D1 differences the design makes on purpose", () => {
  const ids = (text: string) => ((JSON.parse(text) as { hits: Array<{ id: string }> }).hits).map((hit) => hit.id);

  it("ranks the exact tier first, and a supercell above a cell fragment", async () => {
    const [s, d] = await both(getPath("/api/brain/search?q=algebra"));
    expect(ids(s.text)).toEqual([BASIS_CELL, ALGEBRA_SUPER, LINALG_SUPER]);
    expect(ids(d.text)).toEqual([ALGEBRA_SUPER, BASIS_CELL, LINALG_SUPER]);
  });

  it("gives 2-character queries no substring matches", async () => {
    const [s, d] = await both(getPath("/api/brain/search?q=ar"));
    expect(ids(s.text)).toContain(EMPTY_CELL);
    expect(ids(d.text)).toEqual([]);
  });

  it("matches a supercell by its whole path", async () => {
    const [s, d] = await both(getPath(`/api/brain/search?q=${enc("mathlib/lin")}`));
    expect(ids(s.text)).toEqual([]);
    expect(ids(d.text)).toEqual([LINALG_SUPER]);
  });

  it("can return a short filter page whose cursors give the static page's rows", async () => {
    for (const path of ["/api/brain/filter?f=0", `/api/brain/filter?f=0&under=${enc("path:Mathlib")}`]) {
      const s = await serve("static", getPath(path));
      const staticBody = JSON.parse(s.text) as { hits: Array<{ id: string }>; next_cursor: string | null };
      expect(staticBody.next_cursor).toBeNull();

      resetMemos();
      const h = setup();
      installBrainD1Fixture(h.env);
      _setFilterExaminedForTests(2);
      const got: string[] = [];
      let url = path;
      let pages = 0;
      for (;;) {
        const body = (await (await get(h.env, url)).json()) as { hits: Array<{ id: string }>; next_cursor: string | null };
        expect(body.hits.length).toBeLessThanOrEqual(2);
        got.push(...body.hits.map((hit) => hit.id));
        pages += 1;
        if (!body.next_cursor) break;
        url = `${path}&cursor=${enc(body.next_cursor)}`;
        expect(pages).toBeLessThan(20);
      }
      expect(pages).toBeGreaterThan(1);
      expect(got).toEqual(staticBody.hits.map((hit) => hit.id));
    }
  });

  it("scans only the first 2,000 characters of a statement and says so", async () => {
    const q = "x ".repeat(1000) + "vector space";
    const [s, d] = await both(getPath(`/api/brain/bridge?q=${enc(q)}`));
    expect(s.status).toBe(200);
    expect((JSON.parse(s.text) as { atoms: Array<{ id: string }> }).atoms.map((a) => a.id)).toContain(MODULE_CELL);
    expect(d.status).toBe(404);
    expect(JSON.parse(d.text)).toMatchObject({ ok: false, statement_scan: "truncated" });
    expect(s.text).not.toContain("statement_scan");
  });

  it("stops after 4 chunks of candidates and says so", async () => {
    // single-character words in a pseudo-random order: about 9,000 distinct
    // candidates of 5 to 21 units in 1,999 characters, none of them a name
    let x = 12345;
    const words: string[] = [];
    for (let i = 0; i < 1000; i++) {
      x = (x * 48271) % 2147483647;
      words.push("abcdefghijklmnopqrstuvwxyz0123456789"[x % 36]);
    }
    const q = words.join(" ");
    expect(q.length).toBe(1999);
    const [s, d] = await both(getPath(`/api/brain/bridge?q=${enc(q)}`));
    expect(s.status).toBe(404);
    expect(s.text).not.toContain("statement_scan");
    expect(JSON.parse(d.text)).toMatchObject({ statement_scan: "truncated" });
    expect(d.statements.filter((sql) => sql.includes("max(len16)")).length).toBe(4);
    expect(d.batches).toBeLessThanOrEqual(6);
  });

  it("never matches a word the 2,000-character cut splits", async () => {
    const q = "y ".repeat(994) + "vector spaces here";
    expect(q.slice(0, 2000).endsWith("vector space")).toBe(true);
    const [s, d] = await both(getPath(`/api/brain/bridge?q=${enc(q)}`));
    for (const r of [s, d]) expect(r.text).not.toContain(MODULE_CELL);
    expect(JSON.parse(d.text)).toMatchObject({ statement_scan: "truncated" });
  });

  it("fills in supercell traces from the trace table, with the shard's trim rule", async () => {
    const [s, d] = await both(getPath(`/api/brain/neighborhood?id=${enc(LINALG_SUPER)}`));
    type Row = Record<string, unknown> & { id: string };
    const sb = JSON.parse(s.text) as { synapses: Row[]; traces_hydrated: number } & Record<string, unknown>;
    const db = JSON.parse(d.text) as { synapses: Row[]; traces_hydrated: number } & Record<string, unknown>;
    const row = (b: { synapses: Row[] }, id: string) => b.synapses.find((r) => r.id === id)!;
    // rows the static store hydrates read the same
    expect(row(db, MODULE_CELL)).toEqual(row(sb, MODULE_CELL));
    expect(row(db, MODULE_CELL).traces_total).toBe(9);
    expect(row(db, EMPTY_CELL)).toEqual(row(sb, EMPTY_CELL));
    expect(row(db, EMPTY_CELL)).not.toHaveProperty("traces_total");
    // the two the static store cannot reach
    for (const [id, n] of [[ALGEBRA_SUPER, 2], [DECL_CELL, 1]] as const) {
      expect(row(sb, id)).toHaveProperty("traces_unavailable");
      expect(row(db, id)).not.toHaveProperty("traces_unavailable");
      expect(row(db, id)).not.toHaveProperty("traces_total");
      expect((row(db, id).traces as unknown[]).length).toBe(n);
    }
    expect(sb.traces_hydrated).toBe(2);
    expect(db.traces_hydrated).toBe(4);
    const rest = (b: Record<string, unknown>) => {
      const { synapses: _s, traces_hydrated: _t, ...others } = b;
      return others;
    };
    expect(rest(db)).toEqual(rest(sb));
  });

  it("matches word-start fragments only in parts mode", async () => {
    const search = async (q: string, namesMode: "trigram" | "parts") =>
      ids((await serve("d1", getPath(`/api/brain/search?q=${enc(q)}`), { namesMode, datasetVariant: namesMode })).text);
    expect(await search("fourierCoeff", "parts")).toEqual([FOURIER_DEAD_CELL]);
    expect(await search("fourierCoeff", "trigram")).toEqual([FOURIER_DEAD_CELL]);
    expect(await search("ourierCo", "parts")).toEqual([]);
    expect(await search("ourierCo", "trigram")).toEqual([FOURIER_DEAD_CELL]);
    expect(await search("...", "parts")).toEqual([]);
  });
});

describe("the hot-prefix path", () => {
  it("lists hot prefixes on the threshold-2 dataset and answers them like the range does", async () => {
    for (const q of ["abel", "mathlib"]) {
      const normal = await serve("d1", getPath(`/api/brain/search?q=${q}`));
      const hot = await serve("d1", getPath(`/api/brain/search?q=${q}`), { hotPrefixNames: 2, datasetVariant: "hot" });
      const body = (text: string) => {
        const { release_id: _r, ...rest } = JSON.parse(text) as Record<string, unknown>;
        return rest;
      };
      expect(body(hot.text)).toEqual(body(normal.text));
      expect((body(hot.text).hits as unknown[]).length).toBeGreaterThan(0);

      for (const [served, isHot] of [[hot, true], [normal, false]] as const) {
        const hotRows = run(served.db, served.datasetId, hotTierStmt(q, true, true));
        const rangeRows = run(served.db, served.datasetId, rangeTierStmt(q));
        if (isHot) {
          expect(hotRows.length).toBeGreaterThan(0);
          expect(rangeRows).toEqual([]);
        } else {
          expect(hotRows).toEqual([]);
          expect(rangeRows.length).toBeGreaterThan(0);
        }
      }
    }
  });
});

function run(db: DatabaseSync, datasetId: string, stmt: Stmt): Array<Record<string, unknown>> {
  return db.prepare(stmt.sql).all(datasetId, ...(stmt.params as Array<string | number | null>)) as Array<Record<string, unknown>>;
}

describe("readiness and faults", () => {
  const CALLS: Array<[string, (h: Harness) => Promise<Response>]> = [
    ["cell by id", getPath(`/api/brain/cell?key=${enc(MODULE_CELL)}`)],
    ["cell by QID", getPath(`/api/brain/cell?key=${MODULE_Q}`)],
    ["supercell neighborhood", getPath(`/api/brain/neighborhood?id=${enc(LINALG_SUPER)}`)],
    ["transfer fallback", getPath("/api/brain/transfer?q=Vector%20spa&direction=informal_to_formal")],
    ["search", getPath("/api/brain/search?q=vector")],
    ["search an atom id", getPath(`/api/brain/search?q=${enc(MODULE_CELL)}`)],
    ["filter cells", getPath("/api/brain/filter?f=1")],
    ["filter supercells", getPath("/api/brain/filter?f=1&type=supercell")],
    ["bridge", getPath("/api/brain/bridge?q=abelian%20group")],
    ["edges", getPath(`/api/brain/edges?id=${ABELIAN}`)],
    ["decl", getPath("/api/brain/decl?name=Basis")],
    ["POST edge", (h) => post(h.env, "/api/brain/edge", { src: ABELIAN, dst: COMM_DECL, kind: "formalizes", evidence: { note: "x" } }, { user: "u-human" })],
    ["POST node", (h) => post(h.env, "/api/brain/node", { qid: ABELIAN }, { user: "u-human" })],
    ["quickstatements", (h) => post(h.env, "/api/brain/quickstatements", {
      databases: ["wikidata", "mathlib", "lmfdb"],
      items: [{ wikidata: ABELIAN, mathlib: "CommGroup", lmfdb: "group.abelian" }],
    }, { user: "u-human" })],
  ];
  const mcpCell = mcp("brain_cell", { key: MODULE_Q });

  const FAULTS: Array<[string, BrainD1FixtureOpts]> = [
    ["a dataset still importing", { datasetState: "importing" }],
    ["a dataset being deleted", { datasetState: "deleting" }],
    ["an unknown schema_version", { schemaVersion: 99 }],
    ["a dataset id the database does not hold", { registerDataset: false }],
    ["a D1 exception on every statement", { onStatement: () => { throw new Error("D1_ERROR: injected"); } }],
  ];

  it.each(FAULTS)("answers 503 on every route for %s", async (_label, fault) => {
    for (const [label, call] of CALLS) {
      const r = await serve("d1", call, fault);
      expect([label, r.status]).toEqual([label, 503]);
      expect(r.h.db.prepare("SELECT count(*) AS n FROM brain_edges").get()).toEqual({ n: 0 });
      expect(r.h.db.prepare("SELECT count(*) AS n FROM brain_nodes").get()).toEqual({ n: 0 });
    }
    const r = await serve("d1", mcpCell, fault);
    const j = JSON.parse(r.text) as { result: { isError?: boolean; content: Array<{ text: string }> } };
    expect(j.result.isError).toBe(true);
    expect(JSON.parse(j.result.content[0].text)).toMatchObject({ error: "brain release unavailable" });
  });

  it("answers 503 when a statement on one table fails", async () => {
    const r = await serve("d1", getPath("/api/brain/search?q=vector"), {
      onStatement: (sql) => {
        if (sql.includes("FROM labels")) throw new Error("D1_ERROR: labels");
      },
    });
    expect(r.status).toBe(503);
  });

  it("checks readiness once per request and store", async () => {
    const r = await serve("d1", getPath(`/api/brain/cell?key=${MODULE_Q}`));
    expect(r.status).toBe(200);
    expect(r.batches).toBe(2);
    expect(r.statements.filter((sql) => sql === READINESS_SQL).length).toBe(1);
  });

  it("serves two releases that share one dataset", async () => {
    resetMemos();
    const h1 = setup();
    const a = installBrainD1Fixture(h1.env, { datasetVariant: "shared" });
    const h2 = setup();
    const b = installBrainD1Fixture(h2.env, { datasetVariant: "shared", releaseVariant: "second", db: a.db });
    expect(b.datasetId).toBe(a.datasetId);
    expect(b.releaseId).not.toBe(a.releaseId);
    expect((a.db.prepare("SELECT count(*) AS n FROM datasets").get() as { n: number }).n).toBe(1);
    for (const [h, release] of [[h1, a.releaseId], [h2, b.releaseId]] as const) {
      resetMemos();
      const res = await get(h.env, `/api/brain/cell?key=${MODULE_Q}`);
      expect(res.status).toBe(200);
      expect(await res.json()).toMatchObject({ ok: true, id: MODULE_CELL, release_id: release });
    }
  });

  it("serves a D1 release from its static files while BRAIN_DB is unbound", async () => {
    resetMemos();
    const h = setup();
    installBrainD1Fixture(h.env);
    delete (h.env as unknown as { BRAIN_DB?: unknown }).BRAIN_DB;
    const res = await get(h.env, `/api/brain/cell?key=${MODULE_Q}`);
    expect(res.status).toBe(200);
  });
});

describe("the D1 store's own methods", () => {
  async function storeOn(opts: BrainD1FixtureOpts = {}): Promise<{ store: D1Store; batches: () => number; h: Harness }> {
    resetMemos();
    const h = setup();
    installBrainD1Fixture(h.env, opts);
    const counter = instrument(h);
    const c = { env: h.env, req: { url: "https://wikilean.test/api/brain/cell" } } as unknown as Ctx;
    const release = (await resolveBrainRelease(c))!;
    expect(release.backend).toBe("d1");
    const store = storeFor(c, release);
    expect(store).toBeInstanceOf(D1Store);
    expect(batchesOf(store)).not.toBeNull();
    return { store: store as D1Store, batches: counter.count, h };
  }

  it("answers empty input without a batch", async () => {
    const { store, batches } = await storeOn();
    expect(await store.labelRows([])).toEqual(new Map());
    expect(await store.xrefNodes([])).toEqual(new Map());
    expect(await store.supercellTraces(LINALG_SUPER, [])).toEqual(new Map());
    expect(await store.keyHits({})).toEqual({});
    expect(batches()).toBe(0);
  });

  it("gives searchNames, searchCellNames and statementAtoms the composites' answers", async () => {
    const { store } = await storeOn();
    const page = await store.searchPage("algebra", "", 25, null);
    expect(await store.searchNames("algebra", "", 25)).toEqual(page.named);
    expect(page.named.map((h) => (h.kind === "cell" ? h.row.id : h.head.id))).toEqual([ALGEBRA_SUPER, BASIS_CELL, LINALG_SUPER]);
    expect((await store.searchCellNames("Vector spa", 5)).map((r) => r.id)).toEqual([MODULE_CELL]);
    expect(await store.statementAtoms("every vector space has a basis", 5)).toEqual([{ id: MODULE_CELL, len: 12 }]);
    const cand = await store.bridgeCandidates("every vector space has a basis", null);
    expect(cand.statement).toEqual([{ id: MODULE_CELL, len: 12 }]);
    expect(cand.statementTruncated).toBe(false);
  });

  it("rebuilds label rows and supercell heads as the static files have them", async () => {
    const { store } = await storeOn();
    const heads = await store.labelRows([ABELIAN_CELL, EMPTY_CELL, HUB_CELL, LINALG_SUPER, "cell:none"]);
    expect(heads.get(ABELIAN_CELL)).toEqual({
      kind: "cell",
      row: {
        id: ABELIAN_CELL, label: "Abelian group", f: 5,
        aka: ["CommGroup", "group.abelian", "abelian+group", "Abelian group"], p: "path:Mathlib/Algebra/Group/Defs",
      },
    });
    expect(JSON.stringify(heads.get(EMPTY_CELL))).toBe(JSON.stringify({ kind: "cell", row: { id: EMPTY_CELL, label: "Parity conjecture" } }));
    expect(heads.get(HUB_CELL)).toMatchObject({ row: { aka: ["Größe der Gruppe", "\u{1D53D}-widget"] } });
    expect(heads.get(LINALG_SUPER)).toEqual({
      kind: "supercell",
      head: { id: LINALG_SUPER, label: "LinearAlgebra", aka: ["Linear algebra", "linear algebra"], n_cells: 1 },
    });
    expect(heads.has("cell:none")).toBe(false);
  });

  it("reads each atom once per request", async () => {
    const { store, batches } = await storeOn();
    await store.cellPage(MODULE_CELL);
    await store.cellPage(MODULE_CELL);
    await store.keyHits({ organ: MODULE_Q });
    await store.keyHits({ organ: MODULE_Q });
    expect(batches()).toBe(2);
  });

  it("prefetches quickstatements endpoints in one batch per 50 keys", async () => {
    const { store, batches } = await storeOn();
    await store.prefetchAtoms([ABELIAN, COMM_DECL, "Q404404", MODULE_CELL, "cell:Q999"]);
    expect(batches()).toBe(1);
    expect(await store.keyHits({ organ: ABELIAN })).toEqual({ organ: ABELIAN_CELL });
    expect(await store.keyHits({ organ: "Q404404" })).toEqual({});
    expect((await store.cellPage(ABELIAN_CELL))?.cell.id).toBe(ABELIAN_CELL);
    expect((await store.cellPage(MODULE_CELL))?.cell.id).toBe(MODULE_CELL);
    expect(await store.cellPage("cell:Q999")).toBeNull();
    expect(batches()).toBe(1);
  });
});

describe("round trips", () => {
  // after one warm-up request, so the dataset's constants are memoized
  async function trips(path: string): Promise<number> {
    resetMemos();
    const h = setup();
    installBrainD1Fixture(h.env);
    await get(h.env, `/api/brain/cell?key=${enc(MODULE_CELL)}`);
    const counter = instrument(h);
    const res = await get(h.env, path);
    expect(res.status).toBeLessThan(500);
    return counter.count();
  }

  it.each([
    ["cell by atom id", `/api/brain/cell?key=${enc(MODULE_CELL)}`, 1],
    ["cell by organ key", `/api/brain/cell?key=${MODULE_Q}`, 2],
    ["search", "/api/brain/search?q=vector%20spa", 2],
    ["filter", "/api/brain/filter?f=1", 1],
    ["bridge", "/api/brain/bridge?q=abelian%20group", 3],
    ["neighborhood of a cell", `/api/brain/neighborhood?id=${enc(MODULE_CELL)}`, 1],
    ["neighborhood of a supercell", `/api/brain/neighborhood?id=${enc(LINALG_SUPER)}`, 2],
  ] as const)("%s: %i", async (_label, path, n) => {
    expect(await trips(path)).toBe(n);
  });

  it("keeps a 100-row quickstatements submit far under the 1,000-statement cap", async () => {
    resetMemos();
    const h = setup();
    let brain = 0;
    installBrainD1Fixture(h.env, { onStatement: () => { brain += 1; } });
    let wikilean = 0;
    const db = h.env.DB as unknown as { prepare: (sql: string) => unknown; batch: (s: unknown[]) => Promise<unknown> };
    const prepare = db.prepare.bind(db);
    db.prepare = (sql: string) => {
      wikilean += 1;
      return prepare(sql);
    };
    const items = Array.from({ length: 100 }, (_, i) => ({
      wikidata: i % 2 ? ABELIAN : MODULE_Q,
      mathlib: i % 2 ? "CommGroup" : "Module",
      lmfdb: `group.abelian.k${i}`,
    }));
    const res = await post(h.env, "/api/brain/quickstatements", { databases: ["wikidata", "mathlib", "lmfdb"], items }, { user: "u-human" });
    const body = (await res.json()) as { accepted: number; failed: number };
    expect(body.accepted).toBe(100);
    expect(brain).toBeLessThanOrEqual(10);
    expect(brain + wikilean).toBeLessThan(1000);
  });
});

describe("statement plans", () => {
  // Shared tables: a full SCAN of one, or a search bound on ds alone, reads
  // every row of every dataset.
  const SHARED = new Set([
    "datasets", "cell_pages", "aliases", "labels", "supercells", "name_keys", "hot_prefixes",
    "cell_under", "synapse_traces", "xrefs", "text_ids",
  ]);
  const hex = "0".repeat(64);
  // [name, statement, the non-shared names a SCAN may read, why a temp b-tree is bounded]
  const CASES: Array<[string, Stmt, string[], string | null]> = [
    ["readiness", { sql: READINESS_SQL, params: [] }, [], null],
    ["cellPage", cellPageStmt(MODULE_CELL), [], null],
    ["cellPages", cellPagesStmt([MODULE_CELL, ABELIAN_CELL]), ["json_each"], null],
    ["supercell", supercellStmt(LINALG_SUPER), [], null],
    ["breadcrumb", breadcrumbStmt(LINALG_SUPER), ["up"], "the walk stops at depth 64"],
    ["keyHits", keyHitsStmt({ organ: "x", decl: "y", slug: "z", name: { keyLc: "a", slugLc: "b" } })!, [], null],
    ["labelRows cells", labelRowsStmts([MODULE_CELL])[0], ["json_each"], null],
    ["labelRows supercells", labelRowsStmts([LINALG_SUPER])[1], ["json_each"], null],
    ["exact tier", exactTierStmt("algebra", 1, 2 ** 52), [], null],
    ["hot tier", hotTierStmt("ab", true, true), ["j", "json_each"], null],
    ["range tier", rangeTierStmt("ab"), ["g"], null],
    ["fragment tier", fragmentTierStmt(hex, fragmentMatch("algebra", "trigram")!, 1, 2 ** 52, 100), ["f"], null],
    ["statement chunk", statementChunkStmt(["vector space", "basis"]), ["json_each"], "GROUP BY and ORDER BY read only the rows matching at most 2,000 candidates"],
    ["traces", tracesStmt([`${MODULE_CELL}|${LINALG_SUPER}`]), ["json_each"], null],
    ["filter cells", filterCellsStmts(1, "", 0, 100, 10000)[0], [], null],
    ["filter cells probe", filterCellsStmts(1, "", 0, 100, 10000)[1], [], null],
    ["filter cells under", filterCellsStmts(1, "path:Mathlib", 0, 100, 10000)[0], ["u"], "at most the examination cap of rows"],
    ["filter cells under probe", filterCellsStmts(1, "path:Mathlib", 0, 100, 10000)[1], [], null],
    ["filter supercells", filterSupercellsStmts(1, "path:Mathlib", 0, 100, 10000)[0], [], null],
    ["filter supercells probe", filterSupercellsStmts(1, "", 0, 100, 10000)[1], [], null],
    ["xrefPages", xrefPagesStmt(ABELIAN), [], null],
    ["xrefNodes", xrefNodesStmt([LMFDB_PAGE]), ["json_each"], null],
    ["organPages", organPagesStmt([ABELIAN]), ["json_each"], null],
  ];

  it.each(CASES)("%s reads a bounded set", (_name, stmt, scans, tempReason) => {
    const db = new DatabaseSync(":memory:");
    for (const url of ["0001_brain.sql", "0002_name_keys_exact.sql"]) {
      db.exec(readMigration(url));
    }
    db.exec(`CREATE VIRTUAL TABLE names_${hex} USING fts5(name, content='', contentless_delete=1, tokenize='trigram')`);
    const plan = db.prepare(`EXPLAIN QUERY PLAN ${stmt.sql}`).all(`sha256:${hex}`, ...(stmt.params as Array<string | number>)) as Array<{ detail: string }>;
    const bad: string[] = [];
    for (const { detail } of plan) {
      const scan = /^SCAN (\S+)/.exec(detail);
      const free = scan && (scan[1] === "CONSTANT" || scan[1].startsWith("(subquery") || detail.includes("VIRTUAL TABLE"));
      if (scan && !free && !scans.includes(scan[1])) bad.push(detail);
      if (scan && SHARED.has(scan[1])) bad.push(detail);
      if (/\(ds=\?\)/.test(detail)) bad.push(detail);
      if (detail.includes("USE TEMP B-TREE") && tempReason === null) bad.push(detail);
    }
    expect(bad).toEqual([]);
  });
});

function readMigration(name: string): string {
  return readFileSync(new URL(`../migrations-brain/${name}`, import.meta.url), "utf8");
}

describe("the shim", () => {
  it("has FTS5 with the trigram tokenizer, contentless_delete and json_each", () => {
    const db = new DatabaseSync(":memory:");
    db.exec("CREATE VIRTUAL TABLE t USING fts5(name, content='', contentless_delete=1, tokenize='trigram')");
    db.prepare("INSERT INTO t (rowid, name) VALUES (?, ?)").run(1, "StronglyContinuousSemigroup");
    expect(db.prepare("SELECT rowid AS id FROM t WHERE t MATCH ?").all('"semigroup"')).toEqual([{ id: 1 }]);
    db.prepare("DELETE FROM t WHERE rowid = ?").run(1);
    expect(db.prepare("SELECT rowid AS id FROM t WHERE t MATCH ?").all('"semigroup"')).toEqual([]);
    expect(db.prepare("SELECT value FROM json_each(?)").all('["a","b"]')).toEqual([{ value: "a" }, { value: "b" }]);
  });

  it("returns a CTE's rows inside a batch", async () => {
    const { makeD1 } = await import("./helpers/d1shim.js");
    const d1 = makeD1(new DatabaseSync(":memory:"));
    const [r] = await d1.batch([d1.prepare("WITH x(n) AS (SELECT 1 UNION ALL SELECT 2) SELECT n FROM x").bind()]);
    expect(r.results).toEqual([{ n: 1 }, { n: 2 }]);
  });
});

