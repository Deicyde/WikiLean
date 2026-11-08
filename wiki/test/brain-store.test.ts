// The BrainStore seam (src/brain-store.ts, docs/BRAIN-D1.md step 3a): every
// Brain data read goes through the release's store. A substituted store must
// answer the routes, no module but brain.ts and the static store may touch the
// release's files, and the static store must keep the old load order.

import { readdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { _resetBrainAssetMemo, BrainReleaseUnavailableError, type BrainReleaseContext } from "../src/brain.js";
import {
  _resetXrefCaches,
  _setBrainStoreForTests,
  type AtomHead,
  type BrainStore,
  type CellEntry,
} from "../src/brain-store.js";
import { StaticStore } from "../src/brain-store-static.js";
import { setup, get, post, blockNetwork, type Harness } from "./helpers/harness.js";
import {
  installBrainFixture,
  ABELIAN,
  COMM_DECL,
  LINALG_SUPER,
  MODULE_CELL,
  MODULE_Q,
} from "./helpers/brain-fixture.js";

blockNetwork();

beforeEach(() => {
  _resetBrainAssetMemo();
  _resetXrefCaches();
});
afterEach(() => _setBrainStoreForTests(null));

async function getJson(env: Parameters<typeof get>[0], path: string): Promise<{ status: number; j: Record<string, unknown> }> {
  const res = await get(env, path);
  return { status: res.status, j: (await res.json()) as Record<string, unknown> };
}

const enc = encodeURIComponent;

describe("routes read through the release's store", () => {
  it("answers /api/brain/cell from a substituted store's cellPage", async () => {
    const h = setup();
    installBrainFixture(h.env);
    class FakeCellStore extends StaticStore {
      async cellPage(id: string): Promise<CellEntry | null> {
        return id === "cell:Fake" ? { cell: { id: "cell:Fake", anchor: "Q1", label: "Fake atom" } } : null;
      }
    }
    _setBrainStoreForTests((c, release) => new FakeCellStore(c, release));
    const { status, j } = await getJson(h.env, "/api/brain/cell?key=cell:Fake");
    expect(status).toBe(200);
    expect(j).toMatchObject({ ok: true, resolved_from: "cell", id: "cell:Fake", kind: "cell", label: "Fake atom" });
  });

  it("answers /api/brain/search from a substituted store's searchNames", async () => {
    const h = setup();
    installBrainFixture(h.env);
    class FakeSearchStore extends StaticStore {
      async searchNames(): Promise<AtomHead[]> {
        return [{ kind: "cell", row: { id: "cell:Fake", label: "Fake atom" } }];
      }
    }
    _setBrainStoreForTests((c, release) => new FakeSearchStore(c, release));
    const { status, j } = await getJson(h.env, "/api/brain/search?q=zzzzqq");
    expect(status).toBe(200);
    expect(j.hits).toEqual([{ id: "cell:Fake", label: "Fake atom" }]);
  });
});

// Every store method, failing alone, must fail the routes that read through it.
// A route that bypassed the store (reading the release's files itself, or
// building its own StaticStore) would still answer 200 and fail here.
type MakeStore = NonNullable<Parameters<typeof _setBrainStoreForTests>[0]>;
type Method = keyof BrainStore;

function failingAt(method: Method, fail: (release: BrainReleaseContext) => Error): MakeStore {
  return (c, release) => {
    const inner = new StaticStore(c, release);
    return new Proxy(inner, {
      get(target, prop) {
        if (prop === method) {
          return async () => {
            throw fail(release);
          };
        }
        const value: unknown = Reflect.get(target, prop, target);
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
  };
}

const unavailable = (release: BrainReleaseContext) =>
  new BrainReleaseUnavailableError(release.releaseId, "BRAIN_DB", "test");

const XREF_PAGE = "xref:lmfdb_knowl:group.abelian";

// Each call answers its HTTP status; the MCP call answers 503 for a 503 tool
// result.
type Call = [string, (h: Harness) => Promise<number>];
const getStatus = (path: string): Call => [`GET ${path}`, async (h) => (await get(h.env, path)).status];
const postEdge: Call = [
  "POST /api/brain/edge",
  async (h) =>
    (await post(h.env, "/api/brain/edge", {
      src: ABELIAN,
      dst: COMM_DECL,
      kind: "formalizes",
      evidence: { note: "store seam" },
    }, { user: "u-store" })).status,
];
const postNode: Call = [
  "POST /api/brain/node",
  async (h) => (await post(h.env, "/api/brain/node", { qid: ABELIAN }, { user: "u-store" })).status,
];
const mcpCell: Call = [
  "MCP brain_cell",
  async (h) => {
    const res = await post(h.env, "/mcp", {
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: { name: "brain_cell", arguments: { key: MODULE_Q } },
    });
    const j = (await res.json()) as { result?: { isError?: boolean; content?: Array<{ text: string }> } };
    const data = JSON.parse(j.result?.content?.[0]?.text ?? "{}") as { error?: string };
    return j.result?.isError && data.error === "brain release unavailable" ? 503 : res.status;
  },
];
const bridgeAbelian = getStatus("/api/brain/bridge?q=abelian%20group");

const CALLS: Record<Method, Call[]> = {
  snapshotSource: [getStatus(`/api/brain/cell?key=${enc(MODULE_CELL)}`), mcpCell],
  cellPage: [getStatus(`/api/brain/cell?key=${enc(MODULE_CELL)}`), mcpCell],
  supercell: [
    getStatus(`/api/brain/cell?key=${enc(LINALG_SUPER)}`),
    getStatus(`/api/brain/neighborhood?id=${enc(LINALG_SUPER)}`),
  ],
  keyHits: [
    getStatus(`/api/brain/transfer?q=${MODULE_Q}&direction=informal_to_formal`),
    getStatus("/api/brain/transfer?q=Module&direction=formal_to_informal"),
    getStatus("/api/brain/decl?name=Basis"), // a dead name: suggestRename's decl lookup
    postEdge, // atomIdForOrgan, the community-edit endpoint oracle
    postNode, // brainNodeExists
    mcpCell,
  ],
  searchNames: [getStatus("/api/brain/search?q=vector")],
  searchCellNames: [
    getStatus("/api/brain/transfer?q=Vector%20spa&direction=informal_to_formal"),
    bridgeAbelian,
  ],
  statementAtoms: [bridgeAbelian],
  labelRows: [getStatus(`/api/brain/search?q=${MODULE_Q}`), bridgeAbelian],
  supercellTraces: [getStatus(`/api/brain/neighborhood?id=${enc(LINALG_SUPER)}`)],
  filterCells: [getStatus("/api/brain/filter?f=1")],
  filterSupercells: [getStatus("/api/brain/filter?f=1&type=supercell")],
  xrefPages: [getStatus(`/api/brain/edges?id=${ABELIAN}`)],
  xrefNodes: [getStatus(`/api/brain/edges?id=${ABELIAN}`)],
};

function storeHarness(): Harness {
  const h = setup();
  installBrainFixture(h.env, { xrefIndex: { [XREF_PAGE]: [ABELIAN, "Q11650"] } });
  return h;
}

describe("every Brain route reads through the store", () => {
  it("lists every store method", () => {
    const methods = Object.getOwnPropertyNames(StaticStore.prototype).filter((name) => name !== "constructor");
    expect(Object.keys(CALLS).sort()).toEqual(methods.sort());
  });

  const cases = Object.entries(CALLS).flatMap(([method, calls]) =>
    calls.map(([label, run]) => ({ method: method as Method, label, run })),
  );

  it.each(cases)("$label answers when the store works ($method)", async ({ run }) => {
    expect(await run(storeHarness())).not.toBe(503);
  });

  it.each(cases)("$label answers 503 when $method is unavailable", async ({ method, run }) => {
    _setBrainStoreForTests(failingAt(method, unavailable));
    expect(await run(storeHarness())).toBe(503);
  });

  it("/api/brain/edges maps an outage after xrefPages succeeded to 503, and a bug to 500", async () => {
    _setBrainStoreForTests(failingAt("xrefNodes", unavailable));
    const outage = await getJson(storeHarness().env, `/api/brain/edges?id=${ABELIAN}`);
    expect(outage.status).toBe(503);
    expect(outage.j).toMatchObject({ ok: false, error: "brain release unavailable" });

    _resetBrainAssetMemo();
    _resetXrefCaches();
    _setBrainStoreForTests(failingAt("xrefNodes", () => new Error("store bug")));
    const bug = await get(storeHarness().env, `/api/brain/edges?id=${ABELIAN}`);
    expect(bug.status).toBe(500);
  });
});

describe("the static store keeps the old reads", () => {
  // Loaders are awaited one after another: when labels.json fails, search must
  // answer 503 without ever requesting supercells.json.
  it("never requests supercells.json for a search once labels.json failed", async () => {
    const paths: string[] = [];
    const h = setup();
    installBrainFixture(h.env, { missingAssets: ["cells/labels.json"], onAssetPath: (path) => paths.push(path) });
    const { status } = await getJson(h.env, "/api/brain/search?q=vector");
    expect(status).toBe(503);
    expect(paths.some((path) => path.endsWith("/cells/labels.json"))).toBe(true);
    expect(paths.some((path) => path.endsWith("/cells/supercells.json"))).toBe(false);
  });

  // The free-text transfer fallback searches case-insensitively.
  it("resolves mixed-case free text through the transfer fallback", async () => {
    const h = setup();
    installBrainFixture(h.env);
    const { status, j } = await getJson(h.env, "/api/brain/transfer?q=Vector%20spa&direction=informal_to_formal");
    expect(status).toBe(200);
    expect(j).toMatchObject({ ok: true, resolved_from: "search", id: MODULE_CELL });
  });
});

// The store, not its callers, lowercases q: a store that hands the static
// implementation an upper-cased q must still find the same atoms.
describe("the store lowercases q", () => {
  class UpperCaseQStore extends StaticStore {
    searchNames(q: string, kind: "" | "cell" | "supercell", limit: number) {
      return super.searchNames(q.toUpperCase(), kind, limit);
    }
    searchCellNames(q: string, limit: number) {
      return super.searchCellNames(q.toUpperCase(), limit);
    }
    statementAtoms(q: string, limit: number) {
      return super.statementAtoms(q.toUpperCase(), limit);
    }
  }

  function upperHarness(): Harness {
    const h = setup();
    installBrainFixture(h.env);
    _setBrainStoreForTests((c, release) => new UpperCaseQStore(c, release));
    return h;
  }

  it("in searchCellNames (the transfer fallback)", async () => {
    const { status, j } = await getJson(upperHarness().env, "/api/brain/transfer?q=Vector%20spa&direction=informal_to_formal");
    expect(status).toBe(200);
    expect(j).toMatchObject({ ok: true, resolved_from: "search", id: MODULE_CELL });
  });

  it("in searchNames, for cells and supercells", async () => {
    const cells = await getJson(upperHarness().env, "/api/brain/search?q=vector%20spa");
    expect(cells.status).toBe(200);
    expect(cells.j.hits).toContainEqual(expect.objectContaining({ id: MODULE_CELL }));
    const supers = await getJson(upperHarness().env, "/api/brain/search?q=linear%20alg&type=supercell");
    expect(supers.status).toBe(200);
    expect(supers.j.hits).toContainEqual(expect.objectContaining({ id: LINALG_SUPER }));
  });

  it("in statementAtoms (the bridge's statement pass)", async () => {
    const { status, j } = await getJson(upperHarness().env, "/api/brain/bridge?q=Every%20vector%20space%20has%20a%20basis");
    expect(status).toBe(200);
    expect(j.atoms).toContainEqual(expect.objectContaining({ id: MODULE_CELL, resolved_from: "statement" }));
  });
});

describe("only the static store reads Brain release files", () => {
  // brain.ts owns the loaders and the selector; brain-store-static.ts is the one
  // reader. A D1 store module joins this list in step 3b if it reads the slim
  // static manifest.
  const ALLOWED = new Set(["brain.ts", "brain-store-static.ts"]);
  const srcDir = fileURLToPath(new URL("../src/", import.meta.url));
  const files = readdirSync(srcDir, { recursive: true, encoding: "utf8" })
    .filter((f) => f.endsWith(".ts"));
  const LOADER = /\b(?:requiredBrainAssetJson|brainAssetJson|brainAssetPath|assetBase|artifactsByPath)\b/;
  // a release path in a string or template, also after `/` or `${...}`
  const RELEASE_PATH = /["'`/}](?:cells\/|xref_index\.json)|["'`]\/assets\/brain\//;

  it("finds the source files", () => {
    expect(files).toContain("brain-api.ts");
    expect(files).toContain("brain-store-static.ts");
  });

  it("keeps Brain asset loaders and release paths out of every other module", () => {
    const offenders: string[] = [];
    for (const f of files) {
      if (ALLOWED.has(f)) continue;
      const source = readFileSync(srcDir + f, "utf8");
      if (LOADER.test(source)) offenders.push(`${f}: asset loader`);
      if (RELEASE_PATH.test(source)) offenders.push(`${f}: release path`);
    }
    expect(offenders).toEqual([]);
  });

  // storeFor is the one way to a store: a route that built its own StaticStore
  // would keep serving static data on a D1 release.
  it("builds the static store only in brain-store.ts", () => {
    const offenders: string[] = [];
    for (const f of files) {
      if (f === "brain-store.ts" || f === "brain-store-static.ts") continue;
      const source = readFileSync(srcDir + f, "utf8");
      if (/["'](?:\.{1,2}\/)+brain-store-static(?:\.[jt]s)?["']/.test(source)) offenders.push(`${f}: imports brain-store-static`);
      const code = source.split("\n").filter((line) => !/^\s*(?:\/\/|\*|\/\*)/.test(line)).join("\n");
      if (/\bStaticStore\b/.test(code)) offenders.push(`${f}: StaticStore`);
    }
    expect(offenders).toEqual([]);
  });
});
