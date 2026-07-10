// POST /mcp — the Wikibrain MCP server (BRAIN v2 axis 5, docs/BRAIN-API.md).
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
  declExistsFor,
  filterFor,
  neighborhoodFor,
  nodeFor,
  searchFor,
  snippetsFor,
  transferFor,
  unitFor,
  type ApiResult,
} from "./brain-api.js";

type Ctx = Context<{ Bindings: Env }>;

// Spec revisions this server implements (identical wire behavior for our
// subset: initialize / tools/list / tools/call / ping, single JSON responses).
const PROTOCOLS = new Set(["2025-06-18", "2025-03-26"]);
const DEFAULT_PROTOCOL = "2025-06-18";
const SERVER_INFO = { name: "wikibrain", version: "2.0.0" };

const INSTRUCTIONS =
  "Wikibrain: WikiLean's verified map of mathematics joining Wikipedia/Wikidata " +
  "concepts, Mathlib4 Lean declarations, and external math databases (LMFDB, nLab, " +
  "Stacks, ProofWiki, …). Node id grammar: Q<digits> = concept, " +
  "decl:<Lib>:<Name> = Lean declaration, path:<Lib>/<Dir> = Mathlib folder, " +
  "xref:<db>:<id> = external DB page, lit:<arxiv>#<ref> = literature statement. " +
  "Mid-proof workflow: brain_transfer jumps informal↔formal (concept text → ranked " +
  "Mathlib decls with docs URLs, or decl name → concepts/articles); decl_exists " +
  "verifies a decl name is real before citing it; brain_unit shows everything known " +
  "about one mathematical object; brain_search finds ids from fuzzy text. " +
  "match_kind semantics on formalizes evidence: 'exact' = the decl IS the concept's " +
  "formalization; 'related'/'partial' = nearby or partial; 'field' = the concept is " +
  "a whole area whose formal home is a Mathlib folder. Confidence is high|medium|low.";

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
    name: "brain_search",
    description:
      "Search the Brain's label index by text. Returns node ids (Q… concepts, " +
      "path:… Mathlib folders, xref:… external pages) usable with every other tool. " +
      "Start here when you only have informal text or an approximate name. A bare " +
      "QID query matches by id.",
    inputSchema: obj(
      {
        q: { type: "string", description: "search text, min 2 chars" },
        type: { type: "string", description: "optional node-type filter: concept | container | ext" },
        limit: { type: "number", description: "max hits (default 25, cap 100)" },
      },
      ["q"],
    ),
  },
  {
    name: "brain_node",
    description:
      "Fetch one node's full shard entry: payload, typed 1-hop edges in both " +
      "directions (formalizes / mentions / depends / matches / xref / relates / " +
      "links / cites, each with confidence + evidence + provenance via prov_table), " +
      "containment breadcrumb, and children for containers. The maximal-detail call; " +
      "prefer brain_unit or brain_neighborhood for focused answers.",
    inputSchema: obj(
      { id: { type: "string", description: "node id, e.g. Q181296 | decl:Mathlib:CommGroup | path:Mathlib/Algebra" } },
      ["id"],
    ),
  },
  {
    name: "brain_unit",
    description:
      "Resolve ANY member key of an atomic unit — QID, decl:<Lib>:<Name>, bare " +
      "fully-qualified Lean decl name, WikiLean article slug, xref:<db>:<id>, or an " +
      "exact concept label — to the owning concept's unit card: the one identity " +
      "joining Wikipedia article, Wikidata QID, formalizing Mathlib decls (with " +
      "match_kind + confidence), Mathlib folder homes, and external-DB cross-refs. " +
      "The best first call when you have any handle on a mathematical object.",
    inputSchema: obj({ key: { type: "string", description: "any member key" } }, ["key"]),
  },
  {
    name: "brain_transfer",
    description:
      "THE informal↔formal jump for proof work. direction=informal_to_formal: q is " +
      "a concept (QID / slug / label / free text) → ranked Mathlib declarations with " +
      "module, mathlib4_docs URL, match_kind ('exact' = is the formalization; " +
      "'related'/'partial' = nearby) and confidence. direction=formal_to_informal: " +
      "q is a Lean decl name → the concepts it formalizes, with Wikipedia-mirror " +
      "article URLs and available snippet sources. Empty results carry near-miss " +
      "suggestions — read them before concluding something is unformalized.",
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
      "A node's typed edges, filtered. kinds is a CSV subset of formalizes, " +
      "mentions, depends, matches, xref, relates, links, cites; dir is out|in|both. " +
      "Use it to walk the graph one hop at a time (e.g. kinds=depends on a decl for " +
      "its formal dependencies, kinds=formalizes&dir=in on a decl for its concepts).",
    inputSchema: obj(
      {
        id: { type: "string", description: "node id" },
        kinds: { type: "string", description: "CSV edge-kind filter (optional)" },
        dir: { type: "string", enum: ["out", "in", "both"], description: "default both" },
        limit: { type: "number", description: "max edges (default 50, cap 200)" },
      },
      ["id"],
    ),
  },
  {
    name: "brain_snippets",
    description:
      "Every stored content snippet for a concept (or one external page): Wikidata " +
      "description, WikiLean annotated-article pointer, and each cross-referenced " +
      "external database's stored snippet — one row per source with license and URL. " +
      "No-content sources (MathWorld/DLMF/EoM/Kerodon) return deep links only.",
    inputSchema: obj(
      { id: { type: "string", description: "concept QID or ext node id (xref:<db>:<id>)" } },
      ["id"],
    ),
  },
  {
    name: "brain_filter",
    description:
      "Enumerate nodes by facet bitmask: returns label rows where (node.f & f) == f. " +
      "Bits: 0 gold @[wikidata] tag · 1 @[stacks] · 2 @[kerodon] · 3 any xref · " +
      "4 formalized · 5 partial · 6 has WikiLean article · 7 has literature · " +
      "8 is ext · 9 lmfdb · 10 nlab · 11 mathworld · 12 proofwiki · 13 stacks-tag · " +
      "14 oeis · 15 has snippet. Bits 0-2 sit on the tagged decl AND propagate to the " +
      "concept(s) it formalizes — f=1 returns tagged decls + their concepts; f=17 " +
      "(bits 0+4) = formalized concept with a gold-tagged formalization. " +
      "Paginate with next_cursor.",
    inputSchema: obj(
      {
        f: { type: "number", description: "facet bitmask (required; 0 matches everything)" },
        type: { type: "string", description: "optional node-type filter: concept | container | ext" },
        limit: { type: "number", description: "max rows (default 100, cap 500)" },
        cursor: { type: "number", description: "resume cursor from a previous call's next_cursor" },
      },
      ["f"],
    ),
  },
  {
    name: "decl_exists",
    description:
      "Verify a fully-qualified Lean declaration name exists in Mathlib (exact " +
      "match against the doc-gen4 declaration index) and get its current module + " +
      "mathlib4_docs URL. Call this BEFORE citing any decl name in a proof or " +
      "annotation — hallucinated/renamed names are the #1 failure mode " +
      "(e.g. Basis → Module.Basis).",
    inputSchema: obj(
      { name: { type: "string", description: "fully-qualified decl name, e.g. CommGroup or Nat.Prime.two_le" } },
      ["name"],
    ),
  },
];

// ---- tool dispatch (Map — never an object index, no proto-pollution) -----------

function argStr(v: unknown): string {
  return typeof v === "string" ? v : typeof v === "number" ? String(v) : "";
}

const IMPLS = new Map<string, (c: Ctx, a: Record<string, unknown>) => Promise<ApiResult>>([
  ["brain_search", (c, a) => searchFor(c, argStr(a.q), argStr(a.type), a.limit)],
  ["brain_node", (c, a) => nodeFor(c, argStr(a.id))],
  ["brain_unit", (c, a) => unitFor(c, argStr(a.key))],
  ["brain_transfer", (c, a) => transferFor(c, argStr(a.q), argStr(a.direction), a.limit)],
  ["brain_neighborhood", (c, a) => neighborhoodFor(c, argStr(a.id), argStr(a.kinds) || undefined, argStr(a.dir) || undefined, a.limit)],
  ["brain_snippets", (c, a) => snippetsFor(c, argStr(a.id))],
  ["brain_filter", (c, a) => filterFor(c, a.f, argStr(a.type) || undefined, a.limit, a.cursor)],
  ["decl_exists", (c, a) => declExistsFor(c, argStr(a.name))],
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
          return c.json(rpcResult(id, toolResult(await impl(c, args))));
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
and match quality), or start from a Lean name and pull the surrounding informal context:
the Wikipedia article, the Wikidata identity, LMFDB knowl text, nLab and Stacks entries.
Every edge carries provenance, confidence, and machine-checkable evidence.</p>

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
<table>
<tr><th>tool</th><th>what it does</th></tr>
<tr><td><code>brain_transfer</code></td><td><b>The informal&harr;formal jump.</b>
  <code>informal_to_formal</code>: concept text/QID/slug &rarr; ranked Mathlib declarations
  with module, mathlib4_docs URL, <code>match_kind</code> (<code>exact</code> = IS the
  formalization) and confidence. <code>formal_to_informal</code>: a Lean decl name &rarr;
  the concepts it formalizes, with article URLs and snippet sources. Empty results carry
  near-miss suggestions.</td></tr>
<tr><td><code>brain_unit</code></td><td>Resolve ANY handle on a mathematical object —
  QID, decl name, article slug, <code>xref:&lt;db&gt;:&lt;id&gt;</code>, exact label — to its
  atomic unit card: article &compfn; QID &compfn; formalizing decls &compfn; Mathlib folders
  &compfn; cross-database identities. The best first call.</td></tr>
<tr><td><code>decl_exists</code></td><td>Verify a Mathlib declaration name is real before
  citing it (existence oracle over the decl index; returns module + docs URL).</td></tr>
<tr><td><code>brain_search</code></td><td>Fuzzy label search &rarr; node ids, when all you
  have is approximate text.</td></tr>
<tr><td><code>brain_node</code></td><td>One node's full record: payload, typed 1-hop edges
  with evidence + provenance, breadcrumb, children.</td></tr>
<tr><td><code>brain_neighborhood</code></td><td>Typed edge walk: filter by kind CSV
  (<code>formalizes,depends,xref,links,&hellip;</code>) and direction.</td></tr>
<tr><td><code>brain_snippets</code></td><td>Every stored content snippet for a concept:
  Wikidata description, article pointer, LMFDB/nLab/Stacks/ProofWiki/PlanetMath/OEIS text
  (each with license); link-only rows for no-content sources.</td></tr>
<tr><td><code>brain_filter</code></td><td>Enumerate nodes by facet bitmask — e.g.
  <code>f=1</code> every gold <code>@[wikidata]</code>-tagged declaration (+ their concepts),
  <code>f=17</code> formalized concepts with a gold-tagged formalization. Bit table on the
  <a href="/brain/api">API reference</a>.</td></tr>
</table>

<h2>Node id grammar</h2>
<table>
<tr><th>form</th><th>meaning</th><th>example</th></tr>
<tr><td><code>Q&lt;digits&gt;</code></td><td>concept (Wikidata identity)</td><td><code>Q181296</code></td></tr>
<tr><td><code>decl:&lt;Lib&gt;:&lt;Name&gt;</code></td><td>Lean declaration</td><td><code>decl:Mathlib:CommGroup</code></td></tr>
<tr><td><code>path:&lt;Lib&gt;/&lt;Dir&gt;</code></td><td>Mathlib folder</td><td><code>path:Mathlib/Algebra</code></td></tr>
<tr><td><code>xref:&lt;db&gt;:&lt;id&gt;</code></td><td>external DB page</td><td><code>xref:lmfdb_knowl:group.abelian</code></td></tr>
<tr><td><code>lit:&lt;arxiv&gt;#&lt;ref&gt;</code></td><td>literature statement</td><td><code>lit:1707.04448#thm1.2</code></td></tr>
</table>

<h2>A worked mid-proof exchange</h2>
<pre><code>&rarr; brain_transfer {"q": "Euler's totient function", "direction": "informal_to_formal"}
&larr; {"qid": "Q190026", "hits": [{"decl": "Nat.totient",
      "module": "Mathlib.Data.Nat.Totient", "match_kind": "exact",
      "docs_url": "https://leanprover-community.github.io/mathlib4_docs/..."}]}

&rarr; decl_exists {"name": "Nat.ModEq.pow_totient"}
&larr; {"exists": true, "module": "Mathlib.Data.Nat.Totient", "docs_url": "..."}

&rarr; brain_snippets {"id": "Q190026"}
&larr; rows from Wikidata, WikiLean, EoM, MathWorld, OEIS, PlanetMath, ProofWiki &hellip;</code></pre>

<p class="muted">Identical REST twins of every tool live under <code>/api/brain/*</code> —
full parameter-level documentation on the <a href="/brain/api">Wikibrain API reference</a>.
Source &amp; benchmark harness: <a href="https://github.com/Deicyde/WikiLean">Deicyde/WikiLean</a>
(<code>wiki/src/mcp.ts</code>, <code>bench/</code>). Data licensing: per-source attribution
on every snippet; the Brain's own node/edge data is CC0.</p>
</main>
</body>
</html>
`;
