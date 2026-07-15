// POST /mcp — the Wikibrain MCP server (src/mcp.ts): initialize version
// negotiation, notifications, tools/list, tools/call happy paths (tools must
// answer exactly like the REST helpers they share), input-validation-as-
// isError, JSON-RPC protocol errors, the per-IP rate limit, and GET → 405.

import { beforeEach, describe, it, expect } from "vitest";
import { _resetBrainAssetMemo } from "../src/brain.js";
import { setup, post, get, blockNetwork, type Harness } from "./helpers/harness.js";
import { app } from "../src/index.js";
import {
  installBrainFixture,
  ABELIAN,
  ABELIAN_CELL,
  MODULE_CELL,
  MODULE_Q,
  VSPACE_Q,
  FIELD_Q,
  LINALG_SUPER,
  DECL_CELL,
  type BrainFixtureOpts,
} from "./helpers/brain-fixture.js";

blockNetwork();

function harness(
  fixture: BrainFixtureOpts = {},
  setupOpts: Parameters<typeof setup>[0] = {},
): Harness {
  const h = setup(setupOpts);
  installBrainFixture(h.env, fixture);
  return h;
}

let nextId = 1;
function rpc(
  h: Harness,
  method: string,
  params?: Record<string, unknown>,
  opts: { id?: unknown; omitId?: boolean } = {},
): Promise<Response> {
  const body: Record<string, unknown> = { jsonrpc: "2.0", method };
  if (!opts.omitId) body.id = opts.id ?? nextId++;
  if (params !== undefined) body.params = params;
  return post(h.env, "/mcp", body);
}

async function callTool(
  h: Harness,
  name: string,
  args: Record<string, unknown>,
): Promise<{ isError?: boolean; data: Record<string, unknown> }> {
  const res = await rpc(h, "tools/call", { name, arguments: args });
  expect(res.status).toBe(200);
  const j = (await res.json()) as {
    result: { isError?: boolean; content: Array<{ type: string; text: string }> };
  };
  expect(j.result.content[0].type).toBe("text");
  return { isError: j.result.isError, data: JSON.parse(j.result.content[0].text) as Record<string, unknown> };
}

// the isolate-lifetime asset memo must not leak fixtures across tests
beforeEach(() => _resetBrainAssetMemo());

describe("POST /mcp — lifecycle", () => {
  it("initialize echoes a supported protocolVersion and advertises tools", async () => {
    const h = harness();
    const res = await rpc(h, "initialize", {
      protocolVersion: "2025-03-26",
      capabilities: {},
      clientInfo: { name: "test", version: "0" },
    });
    expect(res.status).toBe(200);
    const j = (await res.json()) as { id: unknown; result: Record<string, unknown> };
    expect(j.result.protocolVersion).toBe("2025-03-26");
    expect(j.result.capabilities).toEqual({ tools: {} });
    expect(j.result.serverInfo).toEqual({ name: "wikibrain", version: "3.0.0" });
    // the instructions ARE the model contract — they must teach cells, not nodes
    expect(j.result.instructions).toContain("CELL");
    expect(j.result.instructions).toContain("SYNAPSE");
  });

  it("offers 2025-06-18 when the client asks for an unknown version", async () => {
    const h = harness();
    const res = await rpc(h, "initialize", { protocolVersion: "1999-01-01" });
    const j = (await res.json()) as { result: { protocolVersion: string } };
    expect(j.result.protocolVersion).toBe("2025-06-18");
  });

  it("notifications/initialized → 202 with an empty body", async () => {
    const h = harness();
    const res = await rpc(h, "notifications/initialized", undefined, { omitId: true });
    expect(res.status).toBe(202);
    expect(await res.text()).toBe("");
  });

  it("ping → empty result", async () => {
    const h = harness();
    const j = (await (await rpc(h, "ping")).json()) as { result: unknown };
    expect(j.result).toEqual({});
  });

  it("GET /mcp → 405 with a connect hint", async () => {
    const h = harness();
    const res = await get(h.env, "/mcp");
    expect(res.status).toBe(405);
    const j = (await res.json()) as { hint: string };
    expect(j.hint).toContain("claude mcp add --transport http wikibrain");
  });
});

describe("POST /mcp — tools/list", () => {
  it("lists all seven tools with schemas", async () => {
    const h = harness();
    const j = (await (await rpc(h, "tools/list")).json()) as {
      result: { tools: Array<{ name: string; description: string; inputSchema: { type: string } }> };
    };
    // brain_node + brain_unit collapsed into brain_cell: v3 has no particle
    // nodes, and the unit card IS the cell card
    expect(j.result.tools.map((t) => t.name)).toEqual([
      "brain_search",
      "brain_cell",
      "brain_transfer",
      "brain_neighborhood",
      "brain_snippets",
      "brain_filter",
      "decl_exists",
    ]);
    for (const t of j.result.tools) {
      expect(t.description.length).toBeGreaterThan(20);
      expect(t.inputSchema.type).toBe("object");
    }
  });
});

describe("POST /mcp — tools/call", () => {
  it("brain_search finds atoms by text, including through an organ's label", async () => {
    const h = harness();
    const { isError, data } = await callTool(h, "brain_search", { q: "abelian" });
    expect(isError).toBeUndefined();
    expect((data.hits as Array<{ id: string }>).some((r) => r.id === ABELIAN_CELL)).toBe(true);
    // "Vector space" is an organ label of the Module atom
    const aka = await callTool(h, "brain_search", { q: "vector space" });
    expect((aka.data.hits as Array<{ id: string }>)[0].id).toBe(MODULE_CELL);
  });

  it("brain_cell resolves a bare decl name to the owning atom's card", async () => {
    const h = harness();
    const { data } = await callTool(h, "brain_cell", { key: "CommGroup" });
    expect(data).toMatchObject({ ok: true, id: ABELIAN_CELL, kind: "cell", resolved_from: "decl" });
    expect(data.organs_by_kind).toMatchObject({ concept: 1, decl: 1 });
  });

  it("brain_transfer answers both directions off one atom", async () => {
    const h = harness();
    const fwd = await callTool(h, "brain_transfer", {
      q: "abelian group",
      direction: "informal_to_formal",
    });
    expect((fwd.data.hits as Array<{ decl: string }>)[0].decl).toBe("CommGroup");
    const back = await callTool(h, "brain_transfer", {
      q: "Module",
      direction: "formal_to_informal",
    });
    expect((back.data.hits as Array<{ qid: string }>).map((x) => x.qid)).toEqual([MODULE_Q, VSPACE_Q]);
    // a field-of-study concept's formal home is a folder, and it says so
    const field = await callTool(h, "brain_transfer", { q: FIELD_Q, direction: "informal_to_formal" });
    expect(field.data).toMatchObject({ kind: "supercell", container: LINALG_SUPER });
  });

  it("brain_neighborhood / brain_snippets / brain_filter round-trip", async () => {
    const h = harness();
    const nb = await callTool(h, "brain_neighborhood", { id: ABELIAN, kinds: "depends" });
    const syn = nb.data.synapses as Array<Record<string, unknown>>;
    expect(syn).toHaveLength(1);
    expect(syn[0]).toMatchObject({ id: MODULE_CELL, w: 15 });
    expect((syn[0].traces as Array<{ kind: string }>).every((t) => t.kind === "depends")).toBe(true);
    const sn = await callTool(h, "brain_snippets", { id: ABELIAN });
    expect((sn.data.rows as unknown[]).length).toBeGreaterThan(2);
    const fl = await callTool(h, "brain_filter", { f: 5, limit: 10 });
    expect((fl.data.hits as Array<{ id: string }>).map((r) => r.id)).toEqual([ABELIAN_CELL, MODULE_CELL]);
    const sup = await callTool(h, "brain_filter", { f: 0, type: "supercell", under: LINALG_SUPER });
    expect((sup.data.hits as Array<{ id: string }>).map((r) => r.id)).toEqual([LINALG_SUPER]);
  });

  // An agent session that connected before the cell cut holds the old catalog.
  // It must keep working, not hard-fail on the first call.
  it("the v2 brain_unit / brain_node aliases still answer, with the atom", async () => {
    const h = harness();
    const unit = await callTool(h, "brain_unit", { key: "CommGroup" });
    expect(unit.isError).toBeUndefined();
    expect(unit.data).toMatchObject({ ok: true, id: ABELIAN_CELL, kind: "cell" });
    // brain_node took `id`, brain_unit took `key` — both names are accepted
    const node = await callTool(h, "brain_node", { id: VSPACE_Q });
    expect(node.isError).toBeUndefined();
    expect(node.data).toMatchObject({ ok: true, id: MODULE_CELL });
    const byKey = await callTool(h, "brain_node", { key: DECL_CELL });
    expect(byKey.data).toMatchObject({ ok: true, id: DECL_CELL });
  });

  it("decl_exists verifies real decls and rejects fabrications", async () => {
    const h = harness();
    const yes = await callTool(h, "decl_exists", { name: "CommGroup" });
    expect(yes.data).toMatchObject({
      exists: true,
      module: "Mathlib.Algebra.Group.Defs",
      docs_url:
        "https://leanprover-community.github.io/mathlib4_docs/Mathlib/Algebra/Group/Defs.html#CommGroup",
    });
    const no = await callTool(h, "decl_exists", { name: "CommGroupoid.fake" });
    expect(no.isError).toBeUndefined(); // a clean miss is an answer, not an error
    expect(no.data.exists).toBe(false);
  });

  it("input-validation failures are isError tool results, not protocol errors", async () => {
    const h = harness();
    const shortQ = await callTool(h, "brain_search", { q: "a" });
    expect(shortQ.isError).toBe(true);
    expect(shortQ.data.ok).toBe(false);
    const noKey = await callTool(h, "brain_cell", {});
    expect(noKey.isError).toBe(true);
    const badMask = await callTool(h, "brain_filter", { f: -3 });
    expect(badMask.isError).toBe(true);
    const unknownCell = await callTool(h, "brain_cell", { key: "Q999999999" });
    expect(unknownCell.isError).toBe(true);
  });

  it("an unknown tool is a -32602 protocol error", async () => {
    const h = harness();
    const res = await rpc(h, "tools/call", { name: "brain_teleport", arguments: {} });
    expect(res.status).toBe(200);
    const j = (await res.json()) as { error: { code: number; message: string } };
    expect(j.error.code).toBe(-32602);
    expect(j.error.message).toContain("brain_teleport");
  });
});

describe("POST /mcp — protocol errors", () => {
  it("non-JSON body → -32700", async () => {
    const h = harness();
    const res = await app.request(
      "/mcp",
      { method: "POST", headers: { "Content-Type": "application/json" }, body: "{not json" },
      h.env,
    );
    expect(res.status).toBe(400);
    const j = (await res.json()) as { error: { code: number } };
    expect(j.error.code).toBe(-32700);
  });

  it("missing jsonrpc/method → -32600; batch arrays are rejected", async () => {
    const h = harness();
    const bad = await post(h.env, "/mcp", { id: 1, method: 5 });
    expect(bad.status).toBe(400);
    expect(((await bad.json()) as { error: { code: number } }).error.code).toBe(-32600);
    const batch = await post(h.env, "/mcp", [{ jsonrpc: "2.0", id: 1, method: "ping" }]);
    expect(batch.status).toBe(400);
    expect(((await batch.json()) as { error: { code: number } }).error.code).toBe(-32600);
  });

  it("unknown method → -32601", async () => {
    const h = harness();
    const res = await rpc(h, "resources/list");
    expect(res.status).toBe(200);
    const j = (await res.json()) as { error: { code: number } };
    expect(j.error.code).toBe(-32601);
  });

  it("429s when the per-IP limiter denies (BRAIN_API_LIMITER fallback path)", async () => {
    const h = harness({}, { brainApiLimiterAllows: false });
    const res = await rpc(h, "ping");
    expect(res.status).toBe(429);
    const j = (await res.json()) as { error: { code: number } };
    expect(j.error.code).toBe(-32000);
  });
});
