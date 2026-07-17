// POST /mcp — the Wikibrain MCP server (BRAIN v3, docs/BRAIN-API.md).
//
// A dependency-free, STATELESS streamable-HTTP MCP server: plain JSON-RPC 2.0
// over POST, application/json single-response mode (no SSE, no sessions, no
// Durable Objects, no SDK). Connect with:
//
//   claude mcp add --transport http wikibrain https://wikilean.jackmccarthy.org/mcp
//
// Every tool is an internal call into the SAME exported helpers the REST
// routes use (src/brain-api.ts) — never an HTTP self-fetch — so the two
// surfaces answer identically. Input-validation failures come back as tool
// results with isError:true (the model can read and correct them); only
// protocol-level problems (bad JSON-RPC, unknown method/tool) are JSON-RPC
// errors. Read-only by construction: no tool touches D1 or KV.
import type { Context, Hono } from "hono";
import type { Env } from "./env.js";
import {
  bridgeFor,
  cellFor,
  declExistsFor,
  filterFor,
  neighborhoodFor,
  searchFor,
  snapshotFor,
  snippetsFor,
  transferFor,
  SYNAPSE_KINDS,
  SYNAPSE_KINDS_CSV,
  type ApiResult,
} from "./brain-api.js";

type Ctx = Context<{ Bindings: Env }>;

// Spec revisions this server implements (identical wire behavior for our
// subset: initialize / tools/list / tools/call / ping, single JSON responses).
const PROTOCOLS = new Set(["2025-06-18", "2025-03-26"]);
const DEFAULT_PROTOCOL = "2025-06-18";
const SERVER_INFO = { name: "wikibrain", version: "3.0.0" };

const INSTRUCTIONS =
  "Wikibrain: WikiLean's verified map of mathematics joining Wikipedia/Wikidata " +
  "concepts, Mathlib4 Lean declarations, and external math databases (LMFDB, nLab, " +
  "Stacks, ProofWiki, …). " +
  "THE MODEL: the node is a CELL — an atom of mathematics, id cell:<anchor>. A Lean " +
  "decl, a Wikidata concept, an external-DB page, a WikiLean article and an arXiv " +
  "statement that denote ONE object are ORGANS of one cell (Module, Q18848 'module' " +
  "and Q125977 'vector space' are the same atom — Mathlib has no VectorSpace because " +
  "Module generalizes it). Organs are particles, never nodes, and their content is " +
  "embedded: one brain_cell call returns the Lean code, the Wikidata description and " +
  "the licensed DB snippets. Mathlib folders are SUPERCELLS (path:<Lib>/<Dir>) which " +
  "own field-of-study concepts (Q82571 'Linear algebra' IS path:Mathlib/LinearAlgebra, " +
  "not a cell). All weak bonds between two atoms aggregate into ONE SYNAPSE carrying " +
  "weight, a kinds histogram and every trace (each trace keeps its own direction, " +
  "provenance and evidence) — so synapses are undirected and there is no dir argument. " +
  "IDS: pass an organ id (Q<digits>, decl:<Lib>:<Name>, xref:<db>:<id>, an article " +
  "slug, lit:<arxiv>#<ref>) or an atom id (cell:…, path:…) to any tool and it resolves " +
  "to the owning atom. Every concept, declaration, folder and article slug resolves. Two " +
  "v2 populations deliberately have NO atom and 404 with a `reason`: external pages no " +
  "cell claims (v3 dropped ~46k unanchored frontier pages; an anchored xref: page does " +
  "resolve) and arXiv PAPER ids (lit:<arxiv> without #ref) — only statements a cell claims " +
  "are organs. A 404 there means 'no atom owns this', not 'unknown to the Brain'. " +
  "THE CANONICAL LOOP (autoformalization): brain_bridge (informal statement → " +
  "existence-verified decls with signatures, imports, bond quality, one-hop depends — " +
  "the FIRST call) then brain_cell (the full atom for the winner) then decl_exists (batch: " +
  "re-verify EVERY name you write) then brain_neighborhood kinds=depends (walk the formal " +
  "dependency chain across turns; it is cursored). brain_search/brain_transfer are the " +
  "lower-level jumps brain_bridge composes. " +
  "HONEST ABSTENTION: informal to formal answers (brain_bridge, brain_transfer) carry a " +
  "`match` and a `confidence_floor`; a fuzzy match that does not clear the floor returns " +
  "match:'none' with `nearest` candidates instead of a forced weak grounding (a forced " +
  "match is what creates hallucinated citations). When the best hit is a generalization or " +
  "special_case rather than exact, a top-level `note` says so. " +
  "SNAPSHOT ECHO: every response carries snapshot:{generated_at,pin} — the brain build " +
  "time and the Mathlib rev the decls were built against. " +
  "An organ's `bond` says why it is in the cell: 'exact' = it IS the atom (identity); " +
  "'generalization'/'special_case' = it has no formal home of its own and attaches to " +
  "its single best target; 'xref'/'field' = a cross-reference / an area concept.";

// ---- tool catalog -------------------------------------------------------------

interface ToolDef {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
}

const obj = (
  properties: Record<string, unknown>,
  required: string[],
): Record<string, unknown> => ({ type: "object", properties, required });

export const TOOLS: ToolDef[] = [
  {
    name: "brain_bridge",
    description:
      "THE FIRST call of an autoformalization loop. An informal statement in → the top " +
      "Mathlib decls out, each existence-VERIFIED against the decl index, with its Lean " +
      "signature (`code`), `module`, `import_line`, `bond` quality, containment breadcrumb, " +
      "and the atom's one-hop `depends` partners (ids+labels, capped+counted). Collapses the " +
      "search→cell→transfer chain into one turn. Honest abstention: if nothing clears the " +
      "stated `confidence_floor` it returns match:'none' with `nearest` candidates rather " +
      "than a forced weak grounding (which is what creates hallucinated citations); when the " +
      "best hit is a generalization/special_case not an exact formalization, a `note` says so. " +
      "Ends with `next_tools`. THEN: brain_cell for the full atom, decl_exists to re-verify " +
      "every name you write, brain_neighborhood for the dependency chain.",
    inputSchema: obj(
      {
        q: { type: "string", description: "an informal statement or concept, e.g. 'every finitely generated vector space has a basis'" },
        limit: { type: "number", description: "max decl hits across the top atoms (default 8, cap 16)" },
      },
      ["q"],
    ),
  },
  {
    name: "brain_search",
    description:
      "Label search over the atom index → cell ids (cell:<anchor>) and supercell paths " +
      "(path:…). Matches an atom's own label AND its `aka` (every organ's label), so " +
      "q='Vector space' returns the Module atom (ONE atom, named by its anchor). The " +
      "lower-level lookup brain_bridge composes — reach for brain_bridge first for proof " +
      "work; use brain_search when you only need ids. An exactly-resolving key (QID, decl " +
      "name, slug, xref id) is promoted to the top hit.",
    inputSchema: obj(
      {
        q: { type: "string", description: "search text, min 2 chars" },
        type: { type: "string", enum: ["cell", "supercell"], description: "optional kind filter" },
        limit: { type: "number", description: "max hits (default 25, cap 100)" },
      },
      ["q"],
    ),
  },
  {
    name: "brain_cell",
    description:
      "THE first call when you have any handle on a mathematical object. Resolve ANY " +
      "organ id — QID, decl:<Lib>:<Name>, bare fully-qualified Lean decl name, " +
      "WikiLean article slug, xref:<db>:<id>, lit:<arxiv>#<ref>, an exact label — or " +
      "an atom id (cell:… / path:…) to the owning ATOM's card, in one request: every " +
      "organ WITH its embedded content (Lean docstring + code, the Wikidata " +
      "description, licensed external-DB snippets, article annotation counts), each " +
      "organ's `bond` and provenance, the containment breadcrumb, a synapse summary " +
      "and the strongest partners. Because the atom fuses them, key='Q125977' " +
      "(vector space), 'decl:Mathlib:Module' and 'Vector_space' all return the SAME " +
      "cell — that is the answer, not a redirect. A field-of-study concept resolves " +
      "to its Mathlib folder (kind='supercell'). Use brain_neighborhood for the full " +
      "synapse list with traces.",
    inputSchema: obj(
      { key: { type: "string", description: "any organ id, atom id, or exact label" } },
      ["key"],
    ),
  },
  {
    name: "brain_transfer",
    description:
      "The one-atom informal↔formal jump (brain_bridge composes this across atoms). " +
      "direction=informal_to_formal: q is a concept (QID / slug / label / free text) → the " +
      "atom's ranked decl organs, each with module, `import_line`, `code`, mathlib4_docs URL " +
      "and `bond`. Carries `match` + `confidence_floor`: a fuzzy match under the floor returns " +
      "match:'none' + `nearest` (never a forced weak answer); a non-exact best hit adds a `note` " +
      "(e.g. 'nearest is a generalization'). direction=formal_to_informal: q is a Lean decl name " +
      "→ the same atom's concept organs (multi-to-multi: Module answers with BOTH module and " +
      "vector space). A field-of-study concept answers with its supercell (the Mathlib FOLDER " +
      "that is its formal home) — honest, not a miss. Empty results carry near-miss suggestions.",
    inputSchema: obj(
      {
        q: { type: "string", description: "concept text/QID/slug (informal_to_formal) or decl name (formal_to_informal)" },
        direction: { type: "string", enum: ["informal_to_formal", "formal_to_informal"] },
        limit: { type: "number", description: "max hits (default 10, cap 50)" },
      },
      ["q", "direction"],
    ),
  },
  {
    name: "brain_neighborhood",
    description:
      "An atom's SYNAPSES — one aggregated edge per partner atom, not raw edges. Each " +
      "row carries `w` (weight: every constituent bond), a `kinds` histogram, " +
      "`traces_total`, and the `traces` themselves: {kind, src, dst, prov, evidence}, " +
      "where src/dst are the ORGAN ids that witnessed the bond and each trace keeps " +
      "its own direction. A synapse is UNDIRECTED (A may depend on B while B links A), " +
      "so there is no dir argument — read the traces for direction. kinds is a CSV subset " +
      `of exactly these ${SYNAPSE_KINDS.length}: ${SYNAPSE_KINDS_CSV}. ` +
      "'formalizes'/'matches' are NOT synapse kinds — the merge function consumes them as " +
      "organ attachments (an exact formalizes fuses a concept and a decl into ONE cell), so " +
      "read them off an organ's `bond` via brain_cell; asking for them here matches nothing. " +
      "'co-page'/'co-statement' are the rule-4 shared-page / shared-arXiv-statement relations. " +
      "traces=false gives a compact partner list. A supercell's traces are hydrated from the " +
      "partner cells' shards; a row that names `traces_unavailable` has evidence this endpoint " +
      "cannot reach (brain/query.py --full serves it) — it is NOT an unwitnessed bond. " +
      "`withheld_by_shard` counts synapses the shard cap withheld, and `truncated` is true " +
      "whenever any synapse is missing. Stable order (-w, id): walk long chains across turns " +
      "with the opaque `cursor` (response returns `next_cursor` when more remain — the shard " +
      "cap is the only HARD stop and stays counted in withheld_by_shard). `min_w` floors " +
      "synapse weight. Accepts any organ id.",
    inputSchema: obj(
      {
        id: { type: "string", description: "atom id (cell:… / path:…) or any organ id" },
        kinds: { type: "string", description: "CSV synapse-kind filter (optional)" },
        traces: { type: "boolean", description: "include per-bond traces (default true)" },
        limit: { type: "number", description: "max synapses (default 50, cap 200)" },
        min_w: { type: "number", description: "only synapses with weight ≥ this" },
        cursor: { type: "string", description: "opaque cursor from a previous call's next_cursor" },
        min_conf: { type: "number", description: "drop traces whose evidence.confidence is below this (kept when unscored)" },
      },
      ["id"],
    ),
  },
  {
    name: "brain_snippets",
    description:
      "Every stored content snippet on an atom, one row per source, read straight " +
      "from its embedded organ payloads: the Wikidata description (CC0), the WikiLean " +
      "annotated-article pointer, each external page organ's stored snippet, the " +
      "Mathlib docstring + source code, and arXiv statement links. Every row carries " +
      "its license. No-content sources (MathWorld/DLMF/EoM/Kerodon) return deep links " +
      "only, and arXiv statement text is never redistributed. Accepts any organ id.",
    inputSchema: obj(
      { id: { type: "string", description: "atom id or any organ id (QID, decl:…, xref:<db>:<id>, slug)" } },
      ["id"],
    ),
  },
  {
    name: "brain_filter",
    description:
      "Enumerate atoms by facet bitmask: returns rows where (f_row & f) == f. " +
      "type='cell' (default) reads each cell's OWN mask; type='supercell' reads `fa`, " +
      "the subtree-AGGREGATE mask ('something under this folder matches'). " +
      "under='path:…' restricts to a containment subtree. " +
      "Bits: 0 gold @[wikidata] tag · 1 @[stacks] · 2 @[kerodon] · 3 any xref · " +
      "4 formalized · 5 partial · 6 has WikiLean article · 7 has literature · " +
      "8 (unused on cells — an external page is an organ, never an atom) · 9 lmfdb · " +
      "10 nlab · 11 mathworld · 12 proofwiki · 13 stacks-tag · 14 oeis · " +
      "15 has stored snippet. A cell's mask is the OR over its organs, so f=1 returns " +
      "every atom holding a gold-tagged declaration and f=17 (bits 0+4) every " +
      "formalized atom whose formalization carries a gold @[wikidata] tag. " +
      "Paginate with next_cursor.",
    inputSchema: obj(
      {
        f: { type: "number", description: "facet bitmask (required; 0 matches everything)" },
        type: { type: "string", enum: ["cell", "supercell"], description: "default cell" },
        under: { type: "string", description: "restrict to a subtree, e.g. path:Mathlib/Algebra" },
        limit: { type: "number", description: "max rows (default 100, cap 500)" },
        cursor: { type: "number", description: "resume cursor from a previous call's next_cursor" },
      },
      ["f"],
    ),
  },
  {
    name: "decl_exists",
    description:
      "Verify Lean decl names against the doc-gen4 declaration index BEFORE citing them — " +
      "hallucinated/renamed names are the #1 failure mode. Pass `name` (one) or `names` (up " +
      "to 16, so a drafted statement's 3–8 decls verify in ONE call). Each verdict returns " +
      "exists + module + `import_line` + docs URL; when a name is DEAD it returns a labelled " +
      "suggestion — `suggestion_basis:'verified-rename'` (the verified rename map, e.g. " +
      "Basis → Module.Basis) or `'unique-suffix-match'` (one indexed decl shares the last " +
      "segment) — never presented as fact. Batch adds a `counts` summary.",
    inputSchema: obj(
      {
        name: { type: "string", description: "one fully-qualified decl name, e.g. CommGroup or Nat.Prime.two_le" },
        names: { type: "array", items: { type: "string" }, description: "a batch of decl names (cap 16) — verify a drafted statement's citations in one call" },
      },
      [],
    ),
  },
];

// ---- tool dispatch (Map — never an object index, no proto-pollution) -----------

function argStr(v: unknown): string {
  return typeof v === "string" ? v : typeof v === "number" ? String(v) : "";
}

const IMPLS = new Map<string, (c: Ctx, a: Record<string, unknown>) => Promise<ApiResult>>([
  ["brain_bridge", (c, a) => bridgeFor(c, argStr(a.q), a.limit)],
  ["brain_search", (c, a) => searchFor(c, argStr(a.q), argStr(a.type) || undefined, a.limit)],
  ["brain_cell", (c, a) => cellFor(c, argStr(a.key))],
  ["brain_transfer", (c, a) => transferFor(c, argStr(a.q), argStr(a.direction), a.limit)],
  ["brain_neighborhood", (c, a) =>
    neighborhoodFor(c, argStr(a.id), argStr(a.kinds) || undefined, a.limit, a.traces, a.min_w, a.cursor, a.min_conf)],
  ["brain_snippets", (c, a) => snippetsFor(c, argStr(a.id))],
  ["brain_filter", (c, a) => filterFor(c, a.f, argStr(a.type) || undefined, a.limit, a.cursor, argStr(a.under) || undefined)],
  // decl_exists: `names` (array) OR `name` (single) — the batch verifies a drafted
  // statement's 3–8 citations in one round trip (BRIDGE item 1).
  ["decl_exists", (c, a) => declExistsFor(c, argStr(a.name), a.names)],

  // v2 aliases — dispatch-only, deliberately NOT advertised in TOOLS. An agent
  // session that connected before the cell cut holds the old catalog and will
  // keep calling these; they answer with the atom rather than hard-failing.
  // brain_unit: the unit card BECAME the cell card (a unit was QID ∘ article ∘
  // decls ∘ xrefs — exactly a cell's organs). brain_node: v3 has no particle
  // nodes, so a node id is an organ id and resolves to its atom. Both accept
  // either argument name, since the two tools disagreed on it.
  ["brain_unit", (c, a) => cellFor(c, argStr(a.key) || argStr(a.id))],
  ["brain_node", (c, a) => cellFor(c, argStr(a.id) || argStr(a.key))],
]);

// {status, body} → MCP tool result. Failures are isError tool results (the
// model reads the JSON error + hint and can retry) — not protocol errors.
function toolResult(r: ApiResult): Record<string, unknown> {
  return {
    content: [{ type: "text", text: JSON.stringify(r.body) }],
    ...(r.status >= 400 ? { isError: true } : {}),
  };
}

// ---- JSON-RPC plumbing ---------------------------------------------------------

interface RpcRequest {
  jsonrpc?: unknown;
  id?: unknown;
  method?: unknown;
  params?: unknown;
}

function rpcResult(id: unknown, result: unknown): Record<string, unknown> {
  return { jsonrpc: "2.0", id: id ?? null, result };
}

function rpcError(id: unknown, code: number, message: string): Record<string, unknown> {
  return { jsonrpc: "2.0", id: id ?? null, error: { code, message } };
}

type Limiter = { limit: (opts: { key: string }) => Promise<{ success: boolean }> };

export function registerMcpRoutes(app: Hono<{ Bindings: Env }>): void {
  // Browsers get the human documentation page; MCP clients probing with GET
  // (e.g. opening an SSE stream, which this stateless server does not offer)
  // keep the machine-readable 405 hint.
  app.get("/mcp", (c) => {
    // Vary: Accept — the same URL answers HTML to browsers and a JSON 405 to
    // MCP clients; without it a shared cache could poison one with the other
    if ((c.req.header("Accept") || "").includes("text/html")) {
      return c.html(MCP_DOCS_HTML, 200,
        { "Cache-Control": "public, max-age=3600", "Vary": "Accept" });
    }
    return c.json(
      {
        ok: false,
        error: "method not allowed — this is a streamable-HTTP MCP endpoint; POST JSON-RPC 2.0 messages",
        hint: "connect: claude mcp add --transport http wikibrain https://wikilean.jackmccarthy.org/mcp — human docs at /mcp (browser) or /brain/api",
      },
      405,
      { "Vary": "Accept" },
    );
  });

  app.post("/mcp", async (c) => {
    // Per-IP limiter (public unauthenticated endpoint). Uses the dedicated
    // MCP_LIMITER binding when configured (wrangler.jsonc), else falls back to
    // BRAIN_API_LIMITER — same 120/min budget; the mcp: key prefix keeps the
    // counters separate from brainapi:<user.id> writes.
    const limiter: Limiter =
      (c.env as Env & { MCP_LIMITER?: Limiter }).MCP_LIMITER ?? c.env.BRAIN_API_LIMITER;
    const ip = c.req.header("CF-Connecting-IP") || "unknown";
    const rl = await limiter.limit({ key: `mcp:${ip}` });
    if (!rl.success) {
      return c.json(rpcError(null, -32000, "rate limited (120 requests/min per IP) — slow down"), 429);
    }

    let msg: unknown;
    try {
      msg = await c.req.json();
    } catch {
      return c.json(rpcError(null, -32700, "parse error: request body is not valid JSON"), 400);
    }
    if (Array.isArray(msg)) {
      // 2025-06-18 removed batching; we never supported it
      return c.json(rpcError(null, -32600, "batch requests are not supported"), 400);
    }
    if (typeof msg !== "object" || msg === null) {
      return c.json(rpcError(null, -32600, "invalid request: expected a JSON-RPC 2.0 object"), 400);
    }
    const req = msg as RpcRequest;
    if (req.jsonrpc !== "2.0" || typeof req.method !== "string") {
      return c.json(
        rpcError("id" in req ? req.id : null, -32600, "invalid request: need jsonrpc:'2.0' and a string method"),
        400,
      );
    }

    // Notifications (no id) get 202 + empty body per streamable HTTP. A
    // stateless server has nothing to do for notifications/initialized.
    if (!("id" in req)) return c.body(null, 202);

    const id = req.id;
    const params = (typeof req.params === "object" && req.params !== null ? req.params : {}) as Record<
      string,
      unknown
    >;

    switch (req.method) {
      case "initialize": {
        const asked = typeof params.protocolVersion === "string" ? params.protocolVersion : "";
        return c.json(
          rpcResult(id, {
            protocolVersion: PROTOCOLS.has(asked) ? asked : DEFAULT_PROTOCOL,
            capabilities: { tools: {} },
            serverInfo: SERVER_INFO,
            instructions: INSTRUCTIONS,
          }),
        );
      }
      case "ping":
        return c.json(rpcResult(id, {}));
      case "tools/list":
        return c.json(rpcResult(id, { tools: TOOLS }));
      case "tools/call": {
        const name = typeof params.name === "string" ? params.name : "";
        const impl = IMPLS.get(name);
        if (!impl) return c.json(rpcError(id, -32602, `unknown tool: ${name || "(missing name)"}`));
        const args = (typeof params.arguments === "object" && params.arguments !== null
          ? params.arguments
          : {}) as Record<string, unknown>;
        try {
          const r = await impl(c, args);
          r.body.snapshot = await snapshotFor(c); // item 6: every response echoes the snapshot
          return c.json(rpcResult(id, toolResult(r)));
        } catch (err) {
          // an unexpected throw must not become a protocol error — the tool
          // "executed and failed", which the spec wants surfaced via isError.
          // Log it (Workers tail/observability) or internal failures are
          // undiagnosable; the client still gets only a generic message.
          console.error(`mcp tools/call ${name} failed:`, err);
          return c.json(
            rpcResult(id, {
              content: [{ type: "text", text: JSON.stringify({ ok: false, error: "internal error executing tool" }) }],
              isError: true,
            }),
          );
        }
      }
      default:
        return c.json(rpcError(id, -32601, `method not found: ${req.method}`));
    }
  });
}
// ---- GET /mcp — the human documentation page ---------------------------------
// Served to browsers (Accept: text/html); style matches /brain/api.

const MCP_DOCS_HTML = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wikibrain MCP — WikiLean</title>
<meta name="description" content="Wikibrain MCP: a remote Model Context Protocol server over WikiLean's Brain. AI agents jump between informal mathematics (Wikipedia/Wikidata) and formal Mathlib declarations mid-proof.">
<style>
* { box-sizing:border-box; }
body { margin:0; background:#0b0e14; color:#e6e4de; line-height:1.55;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
a { color:#7cb3ff; text-decoration:none; } a:hover { text-decoration:underline; }
.wl-header { background:#10141d; border-bottom:1px solid #262c3a; padding:10px 20px;
  display:flex; align-items:baseline; justify-content:space-between; gap:12px; flex-wrap:wrap; }
.wl-brand { font-weight:700; color:#7cb3ff; font-size:18px; }
.tag { color:#9aa3b2; font-size:.85rem; }
.wl-nav { display:flex; gap:14px; align-items:center; flex-wrap:wrap; font-size:.9rem; }
main { max-width:880px; margin:0 auto; padding:24px 20px 80px; }
h1 { font-size:1.5rem; margin:0 0 4px; } h2 { font-size:1.15rem; margin:2.2em 0 .5em;
  border-bottom:1px solid #262c3a; padding-bottom:6px; }
h3 { font-size:1rem; margin:1.6em 0 .4em; color:#c9d4e3; }
p, li { color:#c4c2bb; font-size:.95rem; }
code { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.85em;
  background:#131826; border:1px solid #262c3a; border-radius:4px; padding:1px 5px; }
pre { background:#131826; border:1px solid #262c3a; border-radius:8px; padding:12px 14px;
  overflow-x:auto; font-size:.82rem; line-height:1.5; }
pre code { background:none; border:0; padding:0; }
table { border-collapse:collapse; width:100%; font-size:.88rem; margin:.6em 0; }
th, td { text-align:left; border-bottom:1px solid #262c3a; padding:6px 10px 6px 0; vertical-align:top; }
th { color:#9aa3b2; font-weight:600; }
.muted { color:#9aa3b2; font-size:.85rem; }
</style>
</head>
<body>
<header class="wl-header">
  <span><span class="wl-brand">WikiLean</span>
    <span class="tag">— Wikibrain MCP: the Brain, as tools for AI agents.</span></span>
  <nav class="wl-nav" aria-label="Site">
    <a href="/">Home</a>
    <a href="/brain">Brain</a>
    <a href="/brain/api">Full API reference</a>
    <a href="https://github.com/Deicyde/WikiLean">GitHub</a>
  </nav>
</header>
<main>
<h1>Wikibrain MCP</h1>
<p class="muted">A remote <a href="https://modelcontextprotocol.io">Model Context Protocol</a>
server over the <a href="/brain">Brain</a> — WikiLean's verified map of mathematics joining
Wikipedia/Wikidata concepts, Mathlib4 Lean declarations, and ten external math databases
(LMFDB, nLab, Stacks, ProofWiki, PlanetMath, MathWorld, OEIS, EoM, Kerodon, DLMF).</p>

<p>The design premise: <b>reasoning about mathematics is faster informally, but only
formalization checks correctness</b>. Wikibrain lets an agent jump between the two
mid-proof — resolve an informal idea to the exact Mathlib declaration (with docs link
and bond quality), or start from a Lean name and pull the surrounding informal context:
the Wikipedia article, the Wikidata identity, LMFDB knowl text, nLab and Stacks entries.
Every bond carries provenance and machine-checkable evidence.</p>

<h2>The unit of the Brain is a <em>cell</em></h2>
<p>The node is an <b>atom</b> of mathematics, id <code>cell:&lt;anchor&gt;</code>. A Lean
declaration, a Wikidata concept, an external-database page, a WikiLean article and an arXiv
statement that all denote <em>one object</em> are <b>organs</b> of that one cell — particles,
never nodes. <code>Module</code>, <code>Q18848</code> (module) and <code>Q125977</code> (vector
space) are <em>the same atom</em>, because Mathlib has no <code>VectorSpace</code>:
<code>Module</code> fully generalizes it. So <code>brain_cell</code> answers identically
whichever of them you hold.</p>
<p>Organ content is <b>embedded</b>: one <code>brain_cell</code> call returns the Lean docstring
and source, the Wikidata description, and each licensed DB snippet — no fan-out. Mathlib folders
are <b>supercells</b> (<code>path:&lt;Lib&gt;/&lt;Dir&gt;</code>) that own <em>field-of-study</em>
concepts: <code>Q82571</code> "Linear algebra" <em>is</em> <code>path:Mathlib/LinearAlgebra</code>,
not a cell of its own. And every weak bond between two atoms aggregates into one <b>synapse</b>
carrying weight, a kinds histogram, and every trace — so synapses are undirected, and direction
lives on each trace.</p>
<p class="muted">Any organ id works anywhere: pass a QID, a decl name, an article slug or an
<code>xref:</code> page to any tool and it resolves to the owning atom.</p>

<h2>Connect</h2>
<h3>Claude Code</h3>
<pre><code>claude mcp add --transport http wikibrain https://wikilean.jackmccarthy.org/mcp</code></pre>
<h3>Claude Desktop / any MCP-JSON config</h3>
<pre><code>{
  "mcpServers": {
    "wikibrain": { "type": "http", "url": "https://wikilean.jackmccarthy.org/mcp" }
  }
}</code></pre>
<h3>Raw JSON-RPC (any language)</h3>
<p>Stateless streamable HTTP: every message is one <code>POST /mcp</code> with a JSON-RPC 2.0
body; responses are plain <code>application/json</code> (no SSE, no sessions, no auth).</p>
<pre><code>curl -s https://wikilean.jackmccarthy.org/mcp \\
  -H 'content-type: application/json' \\
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{
        "name":"brain_transfer",
        "arguments":{"q":"abelian group","direction":"informal_to_formal"}}}'</code></pre>
<p class="muted">Protocol revisions 2025-06-18 and 2025-03-26; methods
<code>initialize</code>, <code>tools/list</code>, <code>tools/call</code>, <code>ping</code>.
Rate limit: 120 calls/min per IP. Read-only by construction.</p>

<h2>The eight tools</h2>
<p>The canonical autoformalization loop: <code>brain_bridge</code> (informal statement &rarr;
existence-verified decls) &rarr; <code>brain_cell</code> (the full atom) &rarr;
<code>decl_exists</code> (batch: re-verify every name you write) &rarr;
<code>brain_neighborhood</code> (walk <code>depends</code> across turns; cursored). Every
response echoes <code>snapshot:{generated_at,pin}</code>.</p>
<table>
<tr><th>tool</th><th>what it does</th></tr>
<tr><td><code>brain_bridge</code></td><td><b>The FIRST call of a formalization loop.</b>
  An informal statement &rarr; the top Mathlib decls, each existence-VERIFIED, with Lean
  signature, <code>import_line</code>, <code>bond</code>, breadcrumb, and one-hop
  <code>depends</code>. Honest abstention: nothing clearing the <code>confidence_floor</code>
  &rarr; <code>match:"none"</code> + <code>nearest</code>. Ends with <code>next_tools</code>.</td></tr>
<tr><td><code>brain_transfer</code></td><td><b>The one-atom informal&harr;formal jump.</b>
  <code>informal_to_formal</code>: concept text/QID/slug &rarr; the atom's ranked Mathlib decl
  organs with module, mathlib4_docs URL and <code>bond</code> (<code>exact</code> = IS this
  atom's formalization). <code>formal_to_informal</code>: a Lean decl name &rarr; the same
  atom's concept organs, with article URLs and snippet sources. A field-of-study concept
  answers with its <em>supercell</em> — the Mathlib folder that is its formal home. Empty
  results carry near-miss suggestions.</td></tr>
<tr><td><code>brain_cell</code></td><td>Resolve ANY handle on a mathematical object —
  QID, decl name, article slug, <code>xref:&lt;db&gt;:&lt;id&gt;</code>, <code>lit:…</code>, exact
  label, or an atom id — to the owning atom's card: every organ with its embedded content
  (Lean code, Wikidata description, licensed DB snippets, article annotations), breadcrumb,
  synapse summary. <b>The best first call.</b></td></tr>
<tr><td><code>decl_exists</code></td><td>Verify decl names before citing them (existence oracle
  over the decl index). Pass <code>name</code> (one) or <code>names</code> (a batch, cap 16 — a
  drafted statement's 3&ndash;8 citations in one call); each verdict returns module +
  <code>import_line</code>, and a DEAD name gets a labelled <code>renamed_to</code> suggestion
  (<code>verified-rename</code> | <code>unique-suffix-match</code>), never a fact.</td></tr>
<tr><td><code>brain_search</code></td><td>Label search &rarr; atom ids, when all you
  have is approximate text. Matches organ labels too, so "Vector space" finds the
  <b>Module</b> atom.</td></tr>
<tr><td><code>brain_neighborhood</code></td><td>An atom's <b>synapses</b>: one row per partner
  with weight, a kinds histogram (<code>depends,links,relates,cites,&hellip;</code>) and every
  trace (each with its own direction, provenance and evidence). Stable <code>(-w, id)</code>
  order with an opaque <code>cursor</code> + <code>min_w</code> to walk long chains across turns.</td></tr>
<tr><td><code>brain_snippets</code></td><td>Every stored content snippet on an atom:
  Wikidata description, article pointer, LMFDB/nLab/Stacks/ProofWiki/PlanetMath/OEIS text,
  Mathlib docstring + code (each with license); link-only rows for no-content sources.</td></tr>
<tr><td><code>brain_filter</code></td><td>Enumerate atoms by facet bitmask — e.g.
  <code>f=1</code> every atom holding a gold <code>@[wikidata]</code>-tagged declaration,
  <code>f=17</code> formalized atoms with a gold-tagged formalization. Bit table on the
  <a href="/brain/api">API reference</a>.</td></tr>
</table>
<p class="muted"><code>brain_unit</code> and <code>brain_node</code> still answer, as aliases of
<code>brain_cell</code> — the v2 unit card <em>became</em> the cell card, and v3 has no particle
nodes.</p>

<h2>Id grammar</h2>
<table>
<tr><th>form</th><th>meaning</th><th>example</th></tr>
<tr><td><code>cell:&lt;anchor&gt;</code></td><td>an atom — the node</td><td><code>cell:Q18848</code></td></tr>
<tr><td><code>path:&lt;Lib&gt;/&lt;Dir&gt;</code></td><td>supercell (Mathlib folder)</td><td><code>path:Mathlib/LinearAlgebra</code></td></tr>
<tr><td><code>Q&lt;digits&gt;</code></td><td>concept organ (Wikidata identity)</td><td><code>Q181296</code></td></tr>
<tr><td><code>decl:&lt;Lib&gt;:&lt;Name&gt;</code></td><td>decl organ</td><td><code>decl:Mathlib:CommGroup</code></td></tr>
<tr><td><code>xref:&lt;db&gt;:&lt;id&gt;</code></td><td>page organ (external DB)</td><td><code>xref:lmfdb_knowl:group.abelian</code></td></tr>
<tr><td><code>lit:&lt;arxiv&gt;#&lt;ref&gt;</code></td><td>statement organ</td><td><code>lit:1707.04448#thm1.2</code></td></tr>
</table>
<p class="muted">Organ ids are accepted everywhere an atom id is — that is the compat layer,
and it is why every pre-v3 id still resolves.</p>

<h2>A worked mid-proof exchange</h2>
<pre><code>&rarr; brain_transfer {"q": "Euler's totient function", "direction": "informal_to_formal"}
&larr; {"id": "cell:Q190026", "kind": "cell", "hits": [{"decl": "Nat.totient",
      "module": "Mathlib.Data.Nat.Totient", "bond": "exact",
      "docs_url": "https://leanprover-community.github.io/mathlib4_docs/..."}]}

&rarr; decl_exists {"name": "Nat.ModEq.pow_totient"}
&larr; {"exists": true, "module": "Mathlib.Data.Nat.Totient", "docs_url": "..."}

&rarr; brain_snippets {"id": "Q190026"}
&larr; rows from Wikidata, WikiLean, Mathlib, EoM, MathWorld, OEIS, PlanetMath &hellip;

&rarr; brain_neighborhood {"id": "Q190026", "kinds": "depends"}
&larr; {"synapses": [{"id": "cell:Q11567", "w": 31, "kinds": {"depends": 31},
      "traces": [{"kind": "depends", "src": "decl:Mathlib:Nat.totient", "dst": "...",
                  "evidence": {"witnesses": [["Nat.totient_prime", "Nat.Prime"]]}}]}]}</code></pre>

<p class="muted">Identical REST twins of every tool live under <code>/api/brain/*</code> —
full parameter-level documentation on the <a href="/brain/api">Wikibrain API reference</a>.
Source &amp; benchmark harness: <a href="https://github.com/Deicyde/WikiLean">Deicyde/WikiLean</a>
(<code>wiki/src/mcp.ts</code>, <code>bench/</code>). Data licensing: per-source attribution
on every snippet; the Brain's own node/edge data is CC0.</p>
</main>
</body>
</html>
`;
