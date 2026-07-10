// Brain-shard ASSETS shim shared by brain-api.test.ts and mcp.test.ts: a small
// but complete v2 fixture graph (concepts, decls, ext nodes with snippets,
// labels with facet bits, aliases, decl-index shards) served through a fake
// ASSETS fetcher, keyed with the REAL declShardKey so shard resolution matches
// production (same approach as brain-edges.test.ts installBrainAssets).

import { declShardKey } from "../../src/decl.js";
import type { Env } from "../../src/env.js";

export const ABELIAN = "Q181296"; // concept with edge-assembled unit (no node.unit)
export const MODULE_Q = "Q18848"; // concept WITH a prebuilt node.unit (passthrough)
export const VSPACE_Q = "Q125977"; // second concept formalized by the same decl
export const EMPTY_Q = "Q555000"; // concept with no formalizes edges
export const COMM_DECL = "decl:Mathlib:CommGroup";
export const MODULE_DECL = "decl:Mathlib:Module";
export const LMFDB_EXT = "xref:lmfdb_knowl:group.abelian"; // ext WITH qid + snippet
export const NLAB_EXT = "xref:nlab:abelian+group"; // ext with NO qid (in-edge resolution)
export const MW_XREF = "xref:mathworld:AbelianGroup"; // xref target with NO ext node minted

// The aliases fixture deliberately puts VSPACE_Q first for "Module" so tests
// can tell the alias path (→ VSPACE_Q) from the in-edge fallback (→ MODULE_Q,
// which wins on confidence).
export const DEFAULT_ALIASES = {
  decls: { CommGroup: [ABELIAN], Module: [VSPACE_Q, MODULE_Q] },
  slugs: { Abelian_group: ABELIAN },
};

export const LABELS = [
  { id: ABELIAN, type: "concept", label: "Abelian group", slug: "Abelian_group", status: "formalized", f: 5 },
  { id: MODULE_Q, type: "concept", label: "Module", slug: "Module_(mathematics)", status: "formalized", f: 7 },
  { id: VSPACE_Q, type: "concept", label: "Vector space", slug: "Vector_space", status: "partial", f: 1 },
  { id: EMPTY_Q, type: "concept", label: "Parity conjecture", slug: "Parity_conjecture", status: "not_formalized" },
  { id: "path:Mathlib/Algebra", type: "container", label: "Algebra", n_decls: 120 },
  { id: LMFDB_EXT, type: "ext", label: "group.abelian (LMFDB)", f: 768 },
];

const DECL_MANIFEST = {
  scheme: { min_len: 2, max_len: 2, pad: "_" },
  shards: { co: 1, mo: 1 } as Record<string, number>,
};
const DECL_SHARDS: Record<string, Array<[string, string]>> = {
  co: [["CommGroup", "Mathlib.Algebra.Group.Defs"]],
  mo: [["Module", "Mathlib.Algebra.Module.Defs"]],
};

type Edge = { id: string; kind: string; confidence?: string; evidence?: Record<string, unknown> };
interface NodeSpec {
  node: Record<string, unknown>;
  out?: Edge[];
  in?: Edge[];
  breadcrumb?: Array<Record<string, unknown>>;
}

function fixtureNodes(): Record<string, NodeSpec> {
  return {
    [ABELIAN]: {
      node: {
        id: ABELIAN, type: "concept", label: "Abelian group", slug: "Abelian_group",
        article_annotations: { total: 60, formalized: 39 },
        display: { primary_decl: "CommGroup", status: "formalized", importance: "Top" },
      },
      out: [
        { id: COMM_DECL, kind: "formalizes", confidence: "high",
          evidence: { match_kind: "exact", module: "Mathlib.Algebra.Group.Defs" } },
        { id: LMFDB_EXT, kind: "xref", confidence: "high", evidence: { property: "P12987" } },
        { id: NLAB_EXT, kind: "xref", confidence: "high", evidence: { property: "P4215" } },
        { id: MW_XREF, kind: "xref", confidence: "high", evidence: { property: "P2812" } },
        { id: MODULE_Q, kind: "relates", confidence: "medium", evidence: { property: "P279" } },
      ],
      in: [{ id: MODULE_Q, kind: "relates", confidence: "medium", evidence: { property: "P279" } }],
    },
    [MODULE_Q]: {
      node: {
        id: MODULE_Q, type: "concept", label: "Module", slug: "Module_(mathematics)",
        unit: {
          qid: MODULE_Q, label: "Module", description: "algebraic structure over a ring",
          article: { slug: "Module_(mathematics)", annotations: { total: 10 } },
          decls: [{ name: "Module", module: "Mathlib.Algebra.Module.Defs", match_kind: "exact", confidence: "high" }],
          containers: [], xrefs: {},
        },
        display: { primary_decl: "Module", status: "formalized" },
      },
      out: [
        { id: MODULE_DECL, kind: "formalizes", confidence: "high",
          evidence: { match_kind: "exact", module: "Mathlib.Algebra.Module.Defs" } },
      ],
    },
    [VSPACE_Q]: {
      node: { id: VSPACE_Q, type: "concept", label: "Vector space", slug: "Vector_space" },
      out: [
        { id: MODULE_DECL, kind: "formalizes", confidence: "medium",
          evidence: { match_kind: "related", module: "Mathlib.Algebra.Module.Defs" } },
      ],
    },
    [EMPTY_Q]: {
      node: { id: EMPTY_Q, type: "concept", label: "Parity conjecture", slug: "Parity_conjecture" },
    },
    [COMM_DECL]: {
      node: {
        id: COMM_DECL, type: "decl", label: "CommGroup", library: "Mathlib",
        module: "Mathlib.Algebra.Group.Defs",
      },
      breadcrumb: [
        { id: "path:Mathlib", label: "Mathlib", type: "container" },
        { id: "path:Mathlib/Algebra", label: "Algebra", type: "container" },
      ],
      in: [{ id: ABELIAN, kind: "formalizes", confidence: "high", evidence: { match_kind: "exact" } }],
    },
    [MODULE_DECL]: {
      node: {
        id: MODULE_DECL, type: "decl", label: "Module", library: "Mathlib",
        module: "Mathlib.Algebra.Module.Defs",
      },
      // medium-confidence edge listed FIRST: formalizingQids must re-rank by
      // confidence, so the owning concept is MODULE_Q (high), not VSPACE_Q
      in: [
        { id: VSPACE_Q, kind: "formalizes", confidence: "medium", evidence: { match_kind: "related" } },
        { id: MODULE_Q, kind: "formalizes", confidence: "high", evidence: { match_kind: "exact" } },
      ],
    },
    [LMFDB_EXT]: {
      node: {
        id: LMFDB_EXT, type: "ext", db: "lmfdb_knowl", label: "group.abelian",
        url: "https://www.lmfdb.org/knowledge/show/group.abelian",
        snippet: "An abelian group is a group whose operation is commutative.",
        snippet_license: "CC-BY-SA-4.0 (LMFDB)", qid: ABELIAN, f: 768,
      },
      in: [{ id: ABELIAN, kind: "xref", confidence: "high" }],
    },
    [NLAB_EXT]: {
      node: {
        id: NLAB_EXT, type: "ext", db: "nlab", label: "abelian+group",
        url: "https://ncatlab.org/nlab/show/abelian+group",
      },
      in: [{ id: ABELIAN, kind: "xref", confidence: "high" }],
    },
  };
}

const KEY_LEN = 6; // every fixture id is ≥6 chars, so fixed-length keys suffice

export interface BrainFixtureOpts {
  // undefined → serve DEFAULT_ALIASES; null → 404 aliases.json (fallback paths)
  aliases?: object | null;
}

export function installBrainFixture(env: Env, opts: BrainFixtureOpts = {}): void {
  const nodes = fixtureNodes();
  const shards: Record<string, number> = {};
  const data: Record<string, Record<string, unknown>> = {};
  for (const [id, spec] of Object.entries(nodes)) {
    const key = declShardKey(id, KEY_LEN);
    shards[key] = (shards[key] ?? 0) + 1;
    const out = spec.out ?? [];
    const inn = spec.in ?? [];
    (data[key] ??= {})[id] = {
      node: spec.node,
      ...(spec.breadcrumb ? { breadcrumb: spec.breadcrumb } : {}),
      edges: {
        out,
        in: inn,
        counts: { out: out.length, in: inn.length },
        truncated: { out: false, in: false },
      },
    };
  }
  const manifest = {
    scheme: { min_len: KEY_LEN, max_len: KEY_LEN, pad: "_" },
    shards,
    prov: [{ source: "test", method: "fixture", pin: "2026-07-10" }],
    roots: [],
    _meta: { generated_at: "2026-07-10" },
  };
  const aliases = opts.aliases === undefined ? DEFAULT_ALIASES : opts.aliases;
  (env as unknown as { ASSETS: { fetch: (r: Request) => Promise<Response> } }).ASSETS = {
    fetch: async (req: Request) => {
      const path = new URL(req.url).pathname;
      const json = (o: unknown) => new Response(JSON.stringify(o), { status: 200 });
      if (path === "/assets/brain/manifest.json") return json(manifest);
      if (path === "/assets/brain/labels.json") return json(LABELS);
      if (path === "/assets/brain/aliases.json")
        return aliases ? json(aliases) : new Response("not found", { status: 404 });
      if (path === "/assets/decl-index/manifest.json") return json(DECL_MANIFEST);
      const dm = /^\/assets\/decl-index\/([a-z0-9_]+)\.json$/.exec(path);
      if (dm && DECL_SHARDS[dm[1]]) return json(DECL_SHARDS[dm[1]]);
      const m = /^\/assets\/brain\/([a-z0-9_]+)\.json$/.exec(path);
      if (m && data[m[1]]) return json(data[m[1]]);
      return new Response("not found", { status: 404 });
    },
  };
}
