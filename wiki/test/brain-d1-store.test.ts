// The D1 store (src/brain-store-d1.ts, docs/BRAIN-D1.md step 3b). One parity
// release (brain-fixture.ts installBrainD1Fixture) lists the serving sidecar
// and still carries every static file, so each route can be served twice:
// through the static store and through the D1 store over an in-memory
// wikilean-brain built from the same values. The responses must be identical,
// byte for byte, except where the doc says D1 answers differently on purpose
// (search tiers, 2-character queries, path matching, short filter pages, the
// bounded statement pass, supercell traces, parts mode); those are pinned here
// one by one.

import { DatabaseSync } from "node:sqlite";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { Context } from "hono";
import type { Env } from "../src/env.js";
import { _resetBrainAssetMemo, resolveBrainRelease, type BrainReleaseContext } from "../src/brain.js";
import { _resetXrefCaches, _setBrainStoreForTests, batchesOf, storeFor } from "../src/brain-store.js";
import { StaticStore } from "../src/brain-store-static.js";
import {
  _resetD1StoreMemo,
  _setFilterExaminedForTests,
  atomExistsStmt,
  breadcrumbsStmt,
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
  nameTiers,
  organPagesStmt,
  PrefetchBudgetError,
  rangeTierStmt,
  READINESS_SQL,
  statementChunkEnd,
  statementChunkStmt,
  supercellsStmt,
  supercellStmt,
  tracesStmt,
  xrefNodesStmt,
  xrefPagesStmt,
  type Stmt,
} from "../src/brain-store-d1.js";
import { blockNetwork, get, post, setup, type Harness } from "./helpers/harness.js";
import { namesFtsCreate } from "./helpers/brain-d1-rows.ts";
import {
  ABELIAN,
  ABELIAN_CELL,
  ALGEBRA_SUPER,
  BASIS_CELL,
  brainDatabase,
  brainMigration,
  COMM_DECL,
  DECL_CELL,
  EMPTY_CELL,
  EMPTY_Q,
  FIELD_Q,
  FOURIER_DEAD_CELL,
  HUB_CELL,
  installBrainD1Fixture,
  installBrainFixture,
  LINALG_SUPER,
  LMFDB_PAGE,
  MIGRATIONS_BRAIN,
  MODULE_CELL,
  MODULE_Q,
  ORDER_SUPER,
  TOPOLOGY_SUPER,
  VSPACE_Q,
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

// The order of BRAIN_DB batches ("brain") and writes of the community overlay
// ("write", brain_edges and brain_nodes) on an installed fixture.
function eventLog(h: Harness): string[] {
  const events: string[] = [];
  const brain = h.env.BRAIN_DB as unknown as { batch: (s: unknown[]) => Promise<unknown> };
  const batch = brain.batch.bind(brain);
  brain.batch = async (s: unknown[]) => {
    events.push("brain");
    return batch(s);
  };
  const db = h.env.DB as unknown as { prepare: (sql: string) => unknown };
  const prepare = db.prepare.bind(db);
  db.prepare = (sql: string) => {
    if (/^\s*(insert|update|delete|replace)\b[\s\S]*\b(brain_edges|brain_nodes)\b/i.test(sql)) events.push("write");
    return prepare(sql);
  };
  return events;
}

// The characters of page text (the json columns) each BRAIN_DB batch returns.
function jsonChars(h: Harness): number[] {
  const sent: number[] = [];
  type Results = Array<{ results?: Array<Record<string, unknown>> }>;
  const d1 = h.env.BRAIN_DB as unknown as { batch: (s: unknown[]) => Promise<Results> };
  const batch = d1.batch.bind(d1);
  d1.batch = async (s: unknown[]) => {
    const out = await batch(s);
    let n = 0;
    for (const r of out) for (const row of r.results ?? []) if (typeof row.json === "string") n += row.json.length;
    sent.push(n);
    return out;
  };
  return sent;
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
  ["cell by a depth-2 path, whose breadcrumb has two crumbs", getPath(`/api/brain/cell?key=${enc(ORDER_SUPER)}`)],
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
  ["neighborhood of a depth-2 supercell", getPath(`/api/brain/neighborhood?id=${enc(ORDER_SUPER)}`)],
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
    // a shared failure or a fallback to the static files is not parity
    expect(s.status).toBeLessThan(500);
    if (_label !== "MCP brain_premises") expect(d.batches).toBeGreaterThan(0);
  });

  it.each([["before"], ["after"]] as const)("holds with a decoy dataset seeded %s, which no response shows", async (decoy) => {
    for (const [label, call] of PARITY) {
      const [s, d] = await both(call, { decoy });
      expect([label, d.status]).toEqual([label, s.status]);
      expect(d.text).toBe(s.text);
      expect(d.text).not.toContain("DECOY");
      expect([label, s.status < 500]).toEqual([label, true]);
      if (label !== "MCP brain_premises") expect([label, d.batches > 0]).toEqual([label, true]);
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

  it("rejects a search q over 200 UTF-16 units on D1 (request bounds), where static still answers", async () => {
    const q = "vector ".repeat(29) + "spa";
    expect(q.length).toBe(206);
    const [s, d] = await both(getPath(`/api/brain/search?q=${enc(q)}`));
    expect(s.status).toBe(200);
    expect(d.status).toBe(400);
    expect(JSON.parse(d.text)).toMatchObject({ ok: false, error: "query too long (max 200 chars)" });
    expect(d.batches).toBe(0);
    expect((await serve("d1", getPath(`/api/brain/search?q=${"v".repeat(200)}`))).status).toBe(200);
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

  it("starts every batch of a write route with the readiness check", async () => {
    const r = await serve("d1", (h) =>
      post(h.env, "/api/brain/edge", { src: ABELIAN, dst: COMM_DECL, kind: "formalizes", evidence: { note: "x" } }, { user: "u-human" }));
    expect(r.status).toBeLessThan(300);
    expect(r.batches).toBeGreaterThan(1);
    expect(r.statements.filter((sql) => sql === READINESS_SQL).length).toBe(r.batches);
  });

  it("makes one batch for POST node, so its readiness check covers every read", async () => {
    // a second batch would need checkReadinessEveryBatch to carry the check: pin the count
    const r = await serve("d1", (h) => post(h.env, "/api/brain/node", { qid: ABELIAN }, { user: "u-human" }));
    expect(r.status).toBe(200);
    expect(r.batches).toBe(1);
    expect(r.statements.filter((sql) => sql === READINESS_SQL).length).toBe(1);
  });

  it("answers 503 when the dataset's constants changed since the isolate memoized them, then serves", async () => {
    // names_mode is the realistic case (a dataset pruned and imported again);
    // longest_name keeps the third request's FTS query valid
    resetMemos();
    const h = setup();
    const fixture = installBrainD1Fixture(h.env);
    expect((await get(h.env, "/api/brain/search?q=vector")).status).toBe(200);
    fixture.db.prepare("UPDATE datasets SET longest_name = longest_name + 1 WHERE dataset_id = ?").run(fixture.datasetId);
    expect((await get(h.env, "/api/brain/search?q=vector")).status).toBe(503);
    expect((await get(h.env, "/api/brain/search?q=vector")).status).toBe(200);
  });

  it("answers 503 on every label lookup of a database without migration 0002", async () => {
    const only: BrainD1FixtureOpts = { migrations: ["0001_brain.sql"] };
    expect((await serve("d1", getPath("/api/brain/search?q=vector"), only)).status).toBe(503);
    expect((await serve("d1", getPath(`/api/brain/cell?key=${enc("abelian GROUP")}`), only)).status).toBe(503);
    expect((await serve("d1", getPath(`/api/brain/cell?key=${enc(MODULE_CELL)}`), only)).status).toBe(200);
  });

  // The sidecar is changed before the release identity is computed, so only
  // the sidecar artifact check can refuse the release.
  it.each([
    ["media_type", "text/plain"],
    ["logical_format", "opaque"],
    ["logical_root", null],
  ] as const)("refuses a release whose serving sidecar artifact has %s %s", async (field, value) => {
    const r = await serve("d1", getPath(`/api/brain/cell?key=${enc(MODULE_CELL)}`), {
      mutateArtifacts: (artifacts) => {
        const sidecar = artifacts.find((a) => a.path === "brain/data/serving-tables.json");
        expect(sidecar).toBeDefined();
        sidecar![field] = value;
      },
    });
    expect(r.status).toBe(503);
    expect(r.batches).toBe(0);
  });

  it("resolves a release without the sidecar as static, with no dataset", async () => {
    resetMemos();
    const h = setup();
    installBrainFixture(h.env);
    const c = { env: h.env, req: { url: "https://wikilean.test/api/brain/cell" } } as unknown as Ctx;
    const release = (await resolveBrainRelease(c))!;
    expect({ backend: release.backend, datasetId: release.datasetId, datasetHex: release.datasetHex })
      .toEqual({ backend: "static", datasetId: null, datasetHex: null });
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
  async function storeOn(
    opts: BrainD1FixtureOpts = {},
  ): Promise<{ store: D1Store; batches: () => number; h: Harness; release: BrainReleaseContext; db: DatabaseSync }> {
    resetMemos();
    const h = setup();
    const { db } = installBrainD1Fixture(h.env, opts);
    const counter = instrument(h);
    const c = { env: h.env, req: { url: "https://wikilean.test/api/brain/cell" } } as unknown as Ctx;
    const release = (await resolveBrainRelease(c))!;
    expect(release.backend).toBe("d1");
    const store = storeFor(c, release);
    expect(store).toBeInstanceOf(D1Store);
    expect(batchesOf(store)).not.toBeNull();
    return { store: store as D1Store, batches: counter.count, h, release, db };
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

  it("prefetches 120 organ keys and 10 cell ids in 3 batches and remembers every one", async () => {
    const { store, batches } = await storeOn();
    const organs = Array.from({ length: 120 }, (_, i) => `decl:Mathlib:None.n${i}`);
    const cells = Array.from({ length: 10 }, (_, i) => `cell:none${i}`);
    await store.prefetchAtoms([...organs, ...cells, COMM_DECL]);
    expect(batches()).toBe(3);
    for (const key of organs) expect(await store.keyHits({ organ: key })).toEqual({});
    for (const id of cells) expect(await store.cellPage(id)).toBeNull();
    expect(await store.keyHits({ organ: COMM_DECL })).toEqual({ organ: ABELIAN_CELL });
    expect((await store.cellPage(ABELIAN_CELL))?.cell.id).toBe(ABELIAN_CELL);
    expect(batches()).toBe(3);
  });

  it("prefetches supercells, by path and as organ owners, with the breadcrumbs supercell() reads", async () => {
    const { store, batches, h, release } = await storeOn();
    await store.prefetchAtoms([FIELD_Q, ORDER_SUPER, "path:Mathlib/Nowhere"]);
    // the keys and paths in one batch, then the owner FIELD_Q named
    expect(batches()).toBe(2);
    expect(await store.keyHits({ organ: FIELD_Q })).toEqual({ organ: LINALG_SUPER });
    const order = await store.supercell(ORDER_SUPER);
    const linalg = await store.supercell(LINALG_SUPER);
    expect(await store.supercell("path:Mathlib/Nowhere")).toBeNull();
    expect(batches()).toBe(2);
    expect(order?.breadcrumb).toEqual([{ id: "path:Mathlib", label: "Mathlib" }, { id: TOPOLOGY_SUPER, label: "Topology" }]);
    expect(linalg?.breadcrumb).toEqual([{ id: "path:Mathlib", label: "Mathlib" }]);
    const fresh = new D1Store({ env: h.env, req: { url: "https://wikilean.test/api/brain/cell" } } as unknown as Ctx, release);
    expect(order).toEqual(await fresh.supercell(ORDER_SUPER));
    expect(linalg).toEqual(await fresh.supercell(LINALG_SUPER));
  });

  it("reads a page over the prefetch's size cap alone, before it returns", async () => {
    const { store, batches, db } = await storeOn();
    // a page over 128 K characters, as #150's hub pages can be
    db.prepare("UPDATE cell_pages SET json = json_set(json, '$.cell.padding', ?) WHERE id = ?").run("x".repeat(200_000), MODULE_CELL);
    await store.prefetchAtoms([MODULE_Q]);
    expect(batches()).toBe(2);
    expect(await store.keyHits({ organ: MODULE_Q })).toEqual({ organ: MODULE_CELL });
    expect((await store.cellPage(MODULE_CELL))?.cell.id).toBe(MODULE_CELL);
    expect(batches()).toBe(2);
  });

  // `count` (70) cell pages of their own, cell:pad00 up, of the given lengths
  // in characters (each page's JSON is 37 characters longer than its padding).
  function padPages(db: DatabaseSync, datasetId: string, pad: (i: number) => number, count = 70): string[] {
    const { ds } = db.prepare("SELECT ds FROM datasets WHERE dataset_id = ?").get(datasetId) as { ds: number };
    const insert = db.prepare("INSERT INTO cell_pages (ds, seq, id, json) VALUES (?, ?, ?, ?)");
    const list = Array.from({ length: count }, (_, i) => `cell:pad${String(i).padStart(2, "0")}`);
    list.forEach((id, i) => insert.run(ds, 900_000 + i, id, JSON.stringify({ cell: { id }, pad: "x".repeat(pad(i)) })));
    return list;
  }

  // `count` organ keys of their own, Pad.organ00 up, each owning a cell page
  // cell:org00 up of the given length in characters (37 plus the padding).
  function padOrgans(db: DatabaseSync, datasetId: string, pad: (i: number) => number, count: number): string[] {
    const { ds } = db.prepare("SELECT ds FROM datasets WHERE dataset_id = ?").get(datasetId) as { ds: number };
    const page = db.prepare("INSERT INTO cell_pages (ds, seq, id, json) VALUES (?, ?, ?, ?)");
    const alias = db.prepare("INSERT INTO aliases (ds, seq, kind, key, owner) VALUES (?, ?, 'organ', ?, ?)");
    return Array.from({ length: count }, (_, i) => {
      const n = String(i).padStart(2, "0");
      page.run(ds, 800_000 + i, `cell:org${n}`, JSON.stringify({ cell: { id: `cell:org${n}` }, pad: "x".repeat(pad(i)) }));
      alias.run(ds, 800_000 + i, `Pad.organ${n}`, `cell:org${n}`);
      return `Pad.organ${n}`;
    });
  }

  const pageChars = (db: DatabaseSync, datasetId: string, id: string): number =>
    (db.prepare("SELECT length(json) AS n FROM cell_pages WHERE id = ? AND ds = (SELECT ds FROM datasets WHERE dataset_id = ?)")
      .get(id, datasetId) as { n: number }).n;

  it("caps each prefetch batch at its share of the 8 M kept characters, and reads the rest alone", async () => {
    const { store, batches, db, h, release } = await storeOn();
    // 50 pages of 120,037 characters fill the first batch (cap 128 K); the
    // second batch's 20 keys share the 2,386,758 left, 119,337 each, so its
    // one page of 120,037 goes alone and its 19 small ones are kept
    const list = padPages(db, release.datasetId!, (i) => (i < 50 || i === 69 ? 120_000 : 10));
    const sent = jsonChars(h);
    await store.prefetchAtoms(list);
    expect(batches()).toBe(3);
    expect(sent.length).toBe(3);
    // every page once: 50 x 120,037 + 19 x 47 + 120,037, under the 8 M in all
    expect(sent.reduce((a, b) => a + b, 0)).toBe(6_122_780);
    for (const id of list) expect((await store.cellPage(id))?.cell.id).toBe(id);
    expect(batches()).toBe(3);
  });

  it("refuses a prefetch whose pages pass 8 M characters, after reads only", async () => {
    const { store, batches, db, h, release } = await storeOn();
    // the second batch's 20 pages of 120,037 each pass the 2,386,758 left
    const list = padPages(db, release.datasetId!, () => 120_000);
    const sent = jsonChars(h);
    await expect(store.prefetchAtoms(list)).rejects.toBeInstanceOf(PrefetchBudgetError);
    expect(batches()).toBe(2);
    // only the first batch's 50 pages came back with their text
    expect(sent.reduce((a, b) => a + b, 0)).toBe(6_001_850);
  });

  it("reads more than 64 pages over their batches' caps in one batch when they fit 8 M", async () => {
    const { store, batches, db, h, release } = await storeOn();
    // two batches of 50 organ keys and 50 cell ids: 100 keys share the budget,
    // a cap of 83,886 then 83,854, so the 33 organ pages of 90,037 in each go
    // alone, 66 in all (5,942,442 characters); the other 134 pages are kept
    const organs = padOrgans(db, release.datasetId!, (i) => (i % 50 < 33 ? 90_000 : 10), 100);
    const cells = padPages(db, release.datasetId!, () => 10, 100);
    const sent = jsonChars(h);
    await store.prefetchAtoms([...organs, ...cells]);
    expect(batches()).toBe(3);
    expect(sent.reduce((a, b) => a + b, 0)).toBe(66 * 90_037 + 134 * 47);
    expect(await store.keyHits({ organ: organs[0] })).toEqual({ organ: "cell:org00" });
    for (let i = 0; i < 100; i++) {
      const n = String(i).padStart(2, "0");
      expect((await store.cellPage(`cell:org${n}`))?.cell.id).toBe(`cell:org${n}`);
      expect((await store.cellPage(`cell:pad${n}`))?.cell.id).toBe(`cell:pad${n}`);
    }
    expect(batches()).toBe(3);
  });

  it("counts a page an organ key and a cell id in one batch both name once", async () => {
    const { store, batches, db, release } = await storeOn();
    // the organ key's owner row and the cell id's row carry the same page
    await store.prefetchAtoms([MODULE_Q, MODULE_CELL]);
    expect(batches()).toBe(1);
    expect(store._prefetchedCharsForTests()).toBe(pageChars(db, release.datasetId!, MODULE_CELL));
  });

  it("does not ask a later batch for a page an earlier batch's organ key loaded", async () => {
    const { store, batches, db, release } = await storeOn();
    // MODULE_Q loads MODULE_CELL in the first batch; the second would hold
    // only MODULE_CELL, so it is not sent
    const pads = padPages(db, release.datasetId!, () => 10).slice(0, 50);
    await store.prefetchAtoms([MODULE_Q, ...pads, MODULE_CELL]);
    expect(batches()).toBe(1);
    expect(store._prefetchedCharsForTests()).toBe(pageChars(db, release.datasetId!, MODULE_CELL) + 50 * 47);
  });

  it("cuts a statement chunk at 2,000 candidates or 512 KB of JSON", () => {
    const short = Array.from({ length: 2500 }, (_, i) => `cand ${i}`);
    expect(statementChunkEnd(short, 0)).toBe(2000);
    expect(statementChunkEnd(short, 2000)).toBe(2500);
    const long = Array.from({ length: 2000 }, (_, i) => `${"é".repeat(300)} ${i}`);
    const end = statementChunkEnd(long, 0);
    expect(end).toBeLessThan(2000);
    expect(end).toBeGreaterThan(0);
    const bytes = (list: string[]) => new TextEncoder().encode(JSON.stringify(list)).length;
    expect(bytes(long.slice(0, end))).toBeLessThanOrEqual(512 * 1024);
    expect(bytes(long.slice(0, end + 1))).toBeGreaterThan(512 * 1024);
    expect(statementChunkEnd(long, end)).toBeGreaterThan(end);
  });

  it("asks parts mode for a prefix only of a last token of at least 2 characters", () => {
    expect(fragmentMatch("Nat.su", "parts")).toBe('"Nat su"*');
    expect(fragmentMatch("fourierCoeff", "parts")).toBe('"fourier Coeff"*');
    // a 1-character last token has no prefix index (prefix='2 3'): it matches whole
    expect(fragmentMatch("Nat.s", "parts")).toBe('"Nat s"');
    // the prefix indexes count code points: U+1D55C is 2 UTF-16 units but 1 character
    expect(fragmentMatch("Nat.\u{1D55C}", "parts")).toBe('"Nat \u{1D55C}"');
    expect(fragmentMatch("Nat.\u{1D55C}a", "parts")).toBe('"Nat \u{1D55C}a"*');
    expect(fragmentMatch("isS", "parts")).toBe('"is S"');
    expect(fragmentMatch("Nat.s?", "parts")).toBe('"Nat s?"');
    expect(fragmentMatch("...", "parts")).toBeNull();
    expect(fragmentMatch("Nat.s", "trigram")).toBe('"Nat.s"');
    const db = new DatabaseSync(":memory:");
    const hex = "1".repeat(64);
    db.exec(namesFtsCreate("parts", hex));
    const insert = db.prepare(`INSERT INTO names_${hex} (rowid, parts) VALUES (?, ?)`);
    for (const [rowid, parts] of [[1, "Nat succ"], [2, "Nat s"], [3, "Nat add"]] as const) insert.run(rowid, parts);
    const hits = (q: string) =>
      db.prepare(`SELECT rowid AS id FROM names_${hex} WHERE names_${hex} MATCH ? ORDER BY rowid`).all(fragmentMatch(q, "parts")!)
        .map((r) => Number((r as { id: number }).id));
    expect(hits("Nat.su")).toEqual([1]);
    expect(hits("Nat.s")).toEqual([2]);
  });

  it("refuses an input past each statement's cap", () => {
    const many = (n: number, prefix: string) => Array.from({ length: n }, (_, i) => `${prefix}${i}`);
    expect(() => labelRowsStmts(many(501, "cell:x"))).toThrow(/at most 500/);
    expect(labelRowsStmts(many(500, "cell:x"))).toHaveLength(2);
    expect(() => tracesStmt(many(201, "p"))).toThrow(/at most 200/);
    expect(() => xrefNodesStmt(many(41, "xref:a:"))).toThrow(/at most 40/);
    expect(() => cellPagesStmt(many(51, "cell:x"))).toThrow(/at most 50/);
    expect(() => organPagesStmt(many(51, "Q"))).toThrow(/at most 50/);
    expect(() => supercellsStmt(many(51, "path:x"))).toThrow(/at most 50/);
    expect(() => breadcrumbsStmt(many(51, "path:x"))).toThrow(/at most 50/);
    expect(() => statementChunkStmt(many(2001, "c"))).toThrow(/at most 2000/);
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

  it("resolves 100 rows of distinct endpoints in the prefetch's batches alone", async () => {
    // Every row names CommGroup and a Brain endpoint: an unknown path, an
    // unknown decl or xref organ key, or a known one (QIDs owned by a cell or a
    // supercell, supercell paths). None of these needs Wikidata.
    const known = [FIELD_Q, ORDER_SUPER, ALGEBRA_SUPER, VSPACE_Q, EMPTY_Q];
    const items = Array.from({ length: 100 }, (_, i) => ({
      mathlib: "CommGroup",
      brain: [`path:Mathlib/Nowhere${i}`, `decl:Mathlib:Unknown.b${i}`, `xref:nlab:none${i}`, known[i % known.length]][i % 4],
    }));
    const keys = new Set(["decl:Mathlib:CommGroup", ...items.map((r) => r.brain)]);
    const paths = [...keys].filter((k) => k.startsWith("path:")).length;
    const organs = keys.size - paths;
    resetMemos();
    const h = setup();
    const statements: string[] = [];
    installBrainD1Fixture(h.env, { onStatement: (sql) => { statements.push(sql); } });
    const counter = instrument(h);
    const events = eventLog(h);
    const res = await post(h.env, "/api/brain/quickstatements", { databases: ["mathlib", "brain"], items }, { user: "u-human" });
    const body = (await res.json()) as { accepted: number; failed: number };
    expect(body.accepted).toBeGreaterThan(0);
    expect(body.accepted + body.failed).toBe(100);
    // ceil(keys / 50) batches, plus one for the supercell FIELD_Q's alias names
    expect(organs).toBeGreaterThan(50);
    expect(counter.count()).toBe(Math.ceil(Math.max(organs, paths) / 50) + 1);
    // every batch carries the readiness check (a write route), and none runs after the first write
    expect(statements.filter((sql) => sql === READINESS_SQL).length).toBe(counter.count());
    expect(events).toContain("write");
    expect(events.lastIndexOf("brain")).toBeLessThan(events.indexOf("write"));
    // no endpoint read its owner, page or supercell on its own
    expect(statements.filter((sql) => /FROM (aliases|cell_pages|supercells)\b/.test(sql) && !sql.includes("json_each"))).toEqual([]);
  });
});

describe("quickstatements reads before it writes", () => {
  // pages over 128 K characters, as #150's hub pages can be
  type Read = { sql: string; params: readonly unknown[] };
  function padded(pad: string, cells: string[], sups: string[]): { h: Harness; statements: string[]; reads: Read[] } {
    resetMemos();
    const h = setup();
    const statements: string[] = [];
    const reads: Read[] = [];
    const { db } = installBrainD1Fixture(h.env, {
      onStatement: (sql, params) => {
        statements.push(sql);
        reads.push({ sql, params });
      },
    });
    for (const id of cells) db.prepare("UPDATE cell_pages SET json = json_set(json, '$.cell.padding', ?) WHERE id = ?").run(pad, id);
    for (const path of sups) db.prepare("UPDATE supercells SET json = json_set(json, '$.padding', ?) WHERE path = ?").run(pad, path);
    return { h, statements, reads };
  }
  // The ids the over-cap reads list: the multi-row statement `stmt` with a
  // maxChars (after the dataset id and the list) past the batches' 128 K cap,
  // which only readAlone asks for.
  const aloneIds = (reads: Read[], stmt: Stmt): string[] =>
    reads
      .filter((r) => r.sql === stmt.sql && Number(r.params[2]) > 128 * 1024)
      .flatMap((r) => JSON.parse(String(r.params[1])) as string[])
      .sort();
  const submit = (h: Harness, items: Array<Record<string, string>>) =>
    post(h.env, "/api/brain/quickstatements", { databases: ["wikidata", "mathlib", "lmfdb"], items }, { user: "u-human" });

  it("reads every page, over-cap ones included, before the first write", async () => {
    const { h, statements, reads } = padded("x".repeat(200_000), [MODULE_CELL, ABELIAN_CELL], [LINALG_SUPER]);
    const counter = instrument(h);
    const events = eventLog(h);
    // MODULE_Q is MODULE_CELL's, CommGroup ABELIAN_CELL's, FIELD_Q the supercell LINALG_SUPER's
    const res = await submit(h, [
      { wikidata: MODULE_Q, mathlib: "CommGroup", lmfdb: "group.abelian.p1" },
      { wikidata: FIELD_Q, mathlib: "CommGroup", lmfdb: "group.abelian.p2" },
    ]);
    expect(res.status).toBe(200);
    expect(((await res.json()) as { accepted: number }).accepted).toBe(2);
    expect(events).toContain("write");
    expect(events.lastIndexOf("brain")).toBeLessThan(events.indexOf("write"));
    // the three pages were read after their batches, each once
    expect(aloneIds(reads, cellPagesStmt([]))).toEqual([MODULE_CELL, ABELIAN_CELL].sort());
    expect(aloneIds(reads, supercellsStmt([]))).toEqual([LINALG_SUPER]);
    expect(statements.filter((sql) => sql === READINESS_SQL).length).toBe(counter.count());
  });

  it("answers 413 and writes nothing when the pages pass the 8 M characters one submit keeps", async () => {
    const { h, reads } = padded("x".repeat(4_500_000), [MODULE_CELL, ABELIAN_CELL], []);
    const events = eventLog(h);
    const res = await submit(h, [{ wikidata: MODULE_Q, mathlib: "CommGroup", lmfdb: "group.abelian.p1" }]);
    expect(res.status).toBe(413);
    expect(await res.json()).toMatchObject({ ok: false, accepted: 0, failed: 1, rows: [] });
    expect(events).toContain("brain");
    expect(events).not.toContain("write");
    expect(h.db.prepare("SELECT count(*) AS n FROM brain_edges").get()).toEqual({ n: 0 });
    // neither page was sent: each came back as its length alone
    expect(aloneIds(reads, cellPagesStmt([]))).toEqual([]);
  });
});

describe("result bounds", () => {
  // A synthetic dataset far past the parity fixture's sizes.
  const HEX = "e".repeat(64);
  const ID = `sha256:${HEX}`;
  const CONSTS = { namesMode: "trigram" as const, longestName: 10, supNamesFrom: 1_000_000 };
  const MAX = Number.MAX_SAFE_INTEGER;

  function bigDataset(): DatabaseSync {
    const db = brainDatabase();
    db.prepare(
      "INSERT INTO datasets (dataset_id, schema_version, tables_json, longest_name, sup_names_from, names_mode, state, imported_at) " +
      "VALUES (?, 1, '{}', ?, ?, 'trigram', 'ready', '2026-11-09T00:00:00Z')",
    ).run(ID, CONSTS.longestName, CONSTS.supNamesFrom);
    const ds = (db.prepare("SELECT ds FROM datasets WHERE dataset_id = ?").get(ID) as { ds: number }).ds;
    const name = db.prepare("INSERT INTO name_keys (ds, seq, name_lc, kind, owner, len16) VALUES (?, ?, ?, 'label', ?, ?)");
    let seq = 0;
    for (let i = 0; i < 250; i++) name.run(ds, ++seq, "basic", `cell:basic${i}`, 5);
    // more than the range tier's LIMIT 1001, so the test below sees the LIMIT
    for (let i = 0; i < 1500; i++) name.run(ds, ++seq, `ab${String(i).padStart(4, "0")}`, `cell:ab${i}`, 6);
    db.exec(namesFtsCreate("trigram", HEX));
    const fts = db.prepare(`INSERT INTO names_${HEX} (rowid, name) VALUES (?, ?)`);
    for (let i = 0; i < 50; i++) {
      name.run(ds, ++seq, `zeta${i}`, `cell:zeta${i}`, 6);
      fts.run(seq, `zeta${i}`);
    }
    const xref = db.prepare("INSERT INTO xrefs (ds, seq, page_ord, node_ord, page, node) VALUES (?, ?, ?, ?, ?, ?)");
    let x = 0;
    for (let i = 0; i < 45; i++) xref.run(ds, ++x, i, 0, `xref:a:p${i}`, "Q1");
    for (let i = 0; i < 120; i++) xref.run(ds, ++x, 100, i, "xref:a:hub", `Q${1000 + i}`);
    return db;
  }

  const tiers = (q: string, limit: number) => nameTiers(q, 1, MAX, limit, CONSTS, HEX, (reason) => new Error(reason));

  it("reads at most 200 names in the exact tier, in seq order", () => {
    const rows = run(bigDataset(), ID, exactTierStmt("basic", 1, MAX));
    expect(rows.length).toBe(200);
    expect(rows.map((r) => Number(r.seq))).toEqual(Array.from({ length: 200 }, (_, i) => i + 1));
  });

  it("reads at most 1,001 names of a prefix range, and refuses the 1,001st (no hot row lists the prefix)", () => {
    const db = bigDataset();
    const t = tiers("ab", 25);
    const rows = t.stmts.map((stmt) => run(db, ID, stmt));
    // 1,500 names start with "ab": only the LIMIT stops the read at 1,001
    expect(rows[2].length).toBe(1001);
    expect(() => t.decode(rows)).toThrow("a prefix range holds more names than the builder allows");
  });

  it("reads at most 20 fragment rows per hit asked for", () => {
    const db = bigDataset();
    const t = tiers("zeta", 2);
    const fragment = t.stmts[t.stmts.length - 1];
    expect(fragment.sql).toContain(`names_${HEX}`);
    expect(run(db, ID, fragment).length).toBe(40);
  });

  it("reads at most 40 pages of a node and 101 nodes of a page", () => {
    const db = bigDataset();
    expect(run(db, ID, xrefPagesStmt("Q1")).length).toBe(40);
    expect(run(db, ID, xrefNodesStmt(["xref:a:hub"])).length).toBe(101);
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
    ["statement chunk", statementChunkStmt(["vector space", "basis"]), ["j", "t"], "GROUP BY and ORDER BY read only the rows matching at most 2,000 candidates"],
    ["traces", tracesStmt([`${MODULE_CELL}|${LINALG_SUPER}`]), ["json_each"], null],
    ["filter cells", filterCellsStmts(1, "", 0, 100, 10000)[0], [], null],
    ["filter cells probe", filterCellsStmts(1, "", 0, 100, 10000)[1], [], null],
    ["filter cells under", filterCellsStmts(1, "path:Mathlib", 0, 100, 10000)[0], ["u"], null],
    ["filter cells under probe", filterCellsStmts(1, "path:Mathlib", 0, 100, 10000)[1], [], null],
    ["filter supercells", filterSupercellsStmts(1, "path:Mathlib", 0, 100, 10000)[0], [], null],
    ["filter supercells probe", filterSupercellsStmts(1, "", 0, 100, 10000)[1], [], null],
    ["xrefPages", xrefPagesStmt(ABELIAN), [], null],
    ["xrefNodes", xrefNodesStmt([LMFDB_PAGE]), ["json_each"], null],
    ["organPages", organPagesStmt([ABELIAN]), ["json_each"], null],
    ["supercells", supercellsStmt([LINALG_SUPER, ALGEBRA_SUPER]), ["json_each"], null],
    ["breadcrumbs", breadcrumbsStmt([LINALG_SUPER, ALGEBRA_SUPER]), ["up", "json_each"], "50 walks that stop at depth 64"],
    ["atom exists (path)", atomExistsStmt(LINALG_SUPER), [], null],
    ["atom exists (cell)", atomExistsStmt(MODULE_CELL), [], null],
  ];

  // The name_keys index and probe columns of each case that reads name_keys
  // (every other case reads none). A search on the wrong index passes the
  // checks above, so it is pinned here: the sorgan branch on name_keys_exact
  // reads the name's rows of every kind.
  const NAME_KEYS: Record<string, string[]> = {
    keyHits: ["name_keys_exact (ds=? AND name_lc=? AND seq<?)", "sqlite_autoindex_name_keys_2 (ds=? AND name_lc=? AND kind=?)"],
    "exact tier": ["name_keys_exact (ds=? AND name_lc=? AND seq>? AND seq<?)"],
    "hot tier": ["sqlite_autoindex_name_keys_1 (ds=? AND seq=?)", "sqlite_autoindex_name_keys_1 (ds=? AND seq=?)"],
    "range tier": ["name_keys_exact (ds=? AND name_lc>? AND name_lc<?)"],
    "fragment tier": ["sqlite_autoindex_name_keys_1 (ds=? AND seq=?)"],
    "statement chunk": ["sqlite_autoindex_name_keys_2 (ds=? AND name_lc=? AND kind=?)"],
  };
  // No statistics (D1's state until someone runs ANALYZE or PRAGMA optimize),
  // then hand-written sqlite_stat1 rows for name_keys, under which an unpinned
  // IN form of the statement chunk moved to name_keys_exact (ds, name_lc).
  const STATS: Array<[string, Array<[string, string]>]> = [
    ["with no sqlite_stat1", []],
    ["under sqlite_stat1 rows of one dataset, 3 rows a name", [
      ["sqlite_autoindex_name_keys_1", "3000000 3000000 1"],
      ["sqlite_autoindex_name_keys_2", "3000000 3000000 3 2 1"],
      ["name_keys_exact", "3000000 3000000 3 1"],
    ]],
    ["under sqlite_stat1 rows of two datasets, 20 rows a name", [
      ["sqlite_autoindex_name_keys_1", "6000000 3000000 1"],
      ["sqlite_autoindex_name_keys_2", "6000000 3000000 20 10 1"],
      ["name_keys_exact", "6000000 3000000 20 1"],
    ]],
  ];

  describe.each(STATS)("%s", (_label, stats) => {
    it.each(CASES)("%s reads a bounded set", (name, stmt, scans, tempReason) => {
      const db = new DatabaseSync(":memory:");
      for (const migration of MIGRATIONS_BRAIN) db.exec(brainMigration(migration));
      db.exec(namesFtsCreate("trigram", hex));
      if (stats.length) {
        db.exec("ANALYZE");
        db.exec("DELETE FROM sqlite_stat1");
        const stat = db.prepare("INSERT INTO sqlite_stat1 (tbl, idx, stat) VALUES ('name_keys', ?, ?)");
        for (const [idx, value] of stats) stat.run(idx, value);
        db.exec("ANALYZE sqlite_schema"); // reload the statistics
      }
      const plan = db.prepare(`EXPLAIN QUERY PLAN ${stmt.sql}`).all(`sha256:${hex}`, ...(stmt.params as Array<string | number>)) as Array<{ detail: string }>;
      const bad: string[] = [];
      const nameKeys: string[] = [];
      for (const { detail } of plan) {
        const scan = /^SCAN (\S+)/.exec(detail);
        const free = scan && (scan[1] === "CONSTANT" || scan[1].startsWith("(subquery") || detail.includes("VIRTUAL TABLE"));
        if (scan && !free && !scans.includes(scan[1])) bad.push(detail);
        if (scan && SHARED.has(scan[1])) bad.push(detail);
        if (/\(ds=\?\)/.test(detail)) bad.push(detail);
        if (detail.includes("USE TEMP B-TREE") && tempReason === null) bad.push(detail);
        const index = /USING (?:COVERING )?INDEX (name_keys_exact|sqlite_autoindex_name_keys_\d+) (\([^)]*\))/.exec(detail);
        if (index) nameKeys.push(`${index[1]} ${index[2]}`);
      }
      expect(bad).toEqual([]);
      expect(nameKeys.sort()).toEqual([...(NAME_KEYS[name] ?? [])].sort());
    });
  });
});

describe("the shim", () => {
  it("has FTS5 with the trigram tokenizer, contentless_delete and json_each", () => {
    const db = new DatabaseSync(":memory:");
    const t = `names_${"0".repeat(64)}`;
    db.exec(namesFtsCreate("trigram", "0".repeat(64)));
    db.prepare(`INSERT INTO ${t} (rowid, name) VALUES (?, ?)`).run(1, "StronglyContinuousSemigroup");
    expect(db.prepare(`SELECT rowid AS id FROM ${t} WHERE ${t} MATCH ?`).all('"semigroup"')).toEqual([{ id: 1 }]);
    db.prepare(`DELETE FROM ${t} WHERE rowid = ?`).run(1);
    expect(db.prepare(`SELECT rowid AS id FROM ${t} WHERE ${t} MATCH ?`).all('"semigroup"')).toEqual([]);
    expect(db.prepare("SELECT value FROM json_each(?)").all('["a","b"]')).toEqual([{ value: "a" }, { value: "b" }]);
  });

  it("returns a CTE's rows inside a batch", async () => {
    const { makeD1 } = await import("./helpers/d1shim.js");
    const d1 = makeD1(new DatabaseSync(":memory:"));
    const [r] = await d1.batch([d1.prepare("WITH x(n) AS (SELECT 1 UNION ALL SELECT 2) SELECT n FROM x").bind()]);
    expect(r.results).toEqual([{ n: 1 }, { n: 2 }]);
  });
});

