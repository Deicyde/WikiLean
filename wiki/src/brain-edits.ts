// Community brain edges — the write/read/delete surface (docs/BRAIN-EDITS-ROADMAP.md).
//
// Logged-in users and API/bearer callers add connections to the Brain: a link
// between two existing nodes, or an `xref` from a node to an external database
// (LMFDB, nLab, MathWorld, …). Everything is LIVE on create, attributed
// ("added by"), and labelled human/AI. Correction is by SOFT delete, which
// leaves a gravestone (deleted_by/at). Reuses the annotation write guards:
// getUser (identity, never client-claimed), checkOrigin (CSRF), a rate limiter,
// and the brain shard set as the node-existence oracle.
import type { Context, Hono } from "hono";
import { drizzle } from "drizzle-orm/d1";
import { and, eq, or, inArray } from "drizzle-orm";
import type { Env } from "./env.js";
import { getUser } from "./auth.js";
import { brainEdges, brainNodes } from "./db/schema.js";
import { brainNodeExists, resolveBrainEntry, BRAIN_ID_RE } from "./brain.js";

const QID_RE = /^Q[1-9][0-9]{0,11}$/;

// Semantic edge kinds a person/agent may contribute. Structural (`contains`) and
// kernel-derived (`depends`) kinds are machine-only and NOT user-addable.
const COMMUNITY_KINDS = new Set(["relates", "xref", "formalizes", "mentions", "matches", "cites"]);

// External-database keys valid as an xref dst `xref:<db>:<value>`. Mirror of
// build_brain_page.py XREF_NAME + catalog/data/source_registry.json
// crossref_sources (the provenance single-source-of-truth). Keep in sync.
const XREF_DBS = new Set([
  "mathworld", "nlab", "proofwiki", "eom", "planetmath", "metamath",
  "lmfdb_knowl", "oeis", "dlmf", "msc", "stacks", "kerodon", "kgmid",
]);

const MAX_NOTE = 2000;
// dst upper bound: a real node id is already ≤400 (BRAIN_ID_RE), but the xref
// branch's `xref:<db>:<value>` value is otherwise unbounded — cap the whole dst
// so an xref value can't store an oversized row (external-DB keys are short).
const MAX_DST = 512;
const EDGE_ID_RE = /^[0-9a-f]{12}$/;

function freshEdgeId(): string {
  const b = new Uint8Array(6);
  crypto.getRandomValues(b);
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}

// CSRF: reject a cross-origin write (reimplemented locally to avoid importing
// from index.ts, which imports this module).
function checkOrigin(c: Context<{ Bindings: Env }>): Response | null {
  const origin = c.req.header("Origin");
  if (origin && origin !== new URL(c.req.url).origin) {
    return c.json({ ok: false, error: "cross-origin request rejected" }, 403);
  }
  return null;
}

function str(v: unknown): string {
  return typeof v === "string" ? v.trim() : "";
}

function safeParse(s: string): unknown {
  try {
    return JSON.parse(s);
  } catch {
    return { note: s };
  }
}

// Static external-page → nodes reverse index (built by build_shards.py →
// /assets/brain/xref_index.json). Cached for the isolate lifetime: it changes
// only on nightly rebuilds, and community (D1) partners are queried live, so a
// few minutes of staleness on the STATIC side is harmless.
let _xrefIndex: Record<string, string[]> | null = null;
/** test-only: clear the isolate-lifetime static-index cache between cases */
export function _resetBrainEditCaches(): void {
  _xrefIndex = null;
}
async function getXrefIndex(c: Context<{ Bindings: Env }>): Promise<Record<string, string[]>> {
  if (_xrefIndex) return _xrefIndex;
  try {
    const res = await c.env.ASSETS.fetch(new Request(new URL("/assets/brain/xref_index.json", c.req.url)));
    _xrefIndex = res.ok ? ((await res.json()) as Record<string, string[]>) : {};
  } catch {
    _xrefIndex = {};
  }
  return _xrefIndex;
}

// user.role 'bot' is the shared PIPELINE_TOKEN bearer (site/moderate.py + scripts).
function isBearer(role: string): boolean {
  return role === "bot";
}

// Validate a Wikidata QID exists, returning its label/description, cached in KV.
// Returns null if the QID is MISSING (negative-cached 1h) or Wikidata is
// unreachable (NOT cached — retryable). Fail-closed: never mint an unvalidated
// node. Exported for the search route.
export async function validateWikidataQid(
  c: Context<{ Bindings: Env }>,
  qid: string,
): Promise<{ label: string; description: string } | null> {
  const key = "wd:ent:" + qid;
  const cached = await c.env.RENDER_CACHE.get(key);
  if (cached !== null) return cached === "0" ? null : (JSON.parse(cached) as { label: string; description: string });
  try {
    const url =
      "https://www.wikidata.org/w/api.php?action=wbgetentities&format=json" +
      "&props=labels%7Cdescriptions&languages=en&ids=" + encodeURIComponent(qid);
    const r = await fetch(url, { headers: { "User-Agent": "WikiLean/1.0 (+https://wikilean.jackmccarthy.org)" } });
    if (!r.ok) return null; // transient — do not cache, allow retry
    const j = (await r.json()) as {
      entities?: Record<string, { missing?: string; labels?: Record<string, { value: string }>; descriptions?: Record<string, { value: string }> }>;
    };
    const e = j.entities?.[qid];
    if (!e || e.missing !== undefined) {
      await c.env.RENDER_CACHE.put(key, "0", { expirationTtl: 3600 });
      return null;
    }
    const val = { label: e.labels?.en?.value || qid, description: e.descriptions?.en?.value || "" };
    await c.env.RENDER_CACHE.put(key, JSON.stringify(val), { expirationTtl: 2592000 }); // 30d
    return val;
  } catch {
    return null;
  }
}

type EndpointResult =
  | { node: true }
  | { node: false; mint: { id: string; label: string; description: string } }
  | { error: string };

// An edge endpoint is valid if it's an EXISTING brain node, OR a validated
// Wikidata QID (which the caller then mints as a community node). Anything else
// is rejected — the constrained "new nodes" rule: only real Wikidata items.
async function resolveNodeEndpoint(c: Context<{ Bindings: Env }>, id: string): Promise<EndpointResult> {
  if (await brainNodeExists(c, id)) return { node: true };
  if (QID_RE.test(id)) {
    const wd = await validateWikidataQid(c, id);
    if (wd) return { node: false, mint: { id, label: wd.label, description: wd.description } };
    return { error: `${id} is not a resolvable Wikidata item (retry if Wikidata was unreachable)` };
  }
  return { error: "endpoint is not a known brain node" };
}

export function registerBrainEditRoutes(app: Hono<{ Bindings: Env }>): void {
  // POST /api/brain/edge — add a connection. Scriptable (bearer) or browser (OAuth).
  app.post("/api/brain/edge", async (c) => {
    const bad = checkOrigin(c);
    if (bad) return bad;
    const user = await getUser(c);
    if (!user) return c.json({ ok: false, error: "login required" }, 401);
    const bearer = isBearer(user.role);

    // Looser limiter for API/bearer scripts; per-user browser limiter otherwise.
    const rl = bearer
      ? await c.env.BRAIN_API_LIMITER.limit({ key: `brainapi:${user.id}` })
      : await c.env.EDIT_LIMITER.limit({ key: `edit:${user.id}` });
    if (!rl.success) return c.json({ ok: false, error: "rate limited" }, 429);

    let body: Record<string, unknown>;
    try {
      body = (await c.req.json()) as Record<string, unknown>;
    } catch {
      return c.json({ ok: false, error: "bad JSON body" }, 400);
    }
    const src = str(body.src);
    const dst = str(body.dst);
    const kind = str(body.kind);
    const ev = (body.evidence ?? {}) as Record<string, unknown>;
    const note = str(ev.note) || str(body.note);

    // actor_type: the human-vs-AI switch. A browser/OAuth submission is a person,
    // so force 'human'. An API/bearer caller MUST declare — the server can't tell
    // a human-run script from an agent, so the token-holder asserts it.
    let actorType: "human" | "ai";
    if (bearer) {
      const declared = str(body.actor_type);
      if (declared !== "human" && declared !== "ai") {
        return c.json(
          { ok: false, error: "API calls must set actor_type to 'human' or 'ai'" },
          400,
        );
      }
      actorType = declared;
    } else {
      actorType = "human";
    }

    if (!COMMUNITY_KINDS.has(kind)) {
      return c.json({ ok: false, error: `kind must be one of: ${[...COMMUNITY_KINDS].join(", ")}` }, 400);
    }
    if (!note) return c.json({ ok: false, error: "an evidence note is required" }, 400);
    if (note.length > MAX_NOTE) return c.json({ ok: false, error: "evidence note too long" }, 400);
    if (!BRAIN_ID_RE.test(src)) return c.json({ ok: false, error: "bad src id" }, 400);

    // Endpoints must be an existing brain node OR a validated Wikidata QID (which
    // we mint as a community node). Collect any mints to persist after the edge.
    const mints: Array<{ id: string; label: string; description: string }> = [];
    const srcRes = await resolveNodeEndpoint(c, src);
    if ("error" in srcRes) return c.json({ ok: false, error: "src: " + srcRes.error, src }, 400);
    if (!srcRes.node) mints.push(srcRes.mint);

    if (dst.length > MAX_DST) return c.json({ ok: false, error: "dst too long" }, 400);

    // dst: for xref it's an external "xref:<db>:<value>"; otherwise a node/QID.
    let evidence: Record<string, unknown> = { note };
    if (kind === "xref") {
      const m = /^xref:([a-z0-9_]+):(.+)$/i.exec(dst);
      if (!m) return c.json({ ok: false, error: "xref dst must be 'xref:<db>:<value>'" }, 400);
      const xdb = m[1].toLowerCase();
      const value = m[2];
      if (!XREF_DBS.has(xdb)) return c.json({ ok: false, error: `unknown xref db '${xdb}'` }, 400);
      evidence = { note, db: xdb, value };
    } else {
      if (!BRAIN_ID_RE.test(dst)) return c.json({ ok: false, error: "bad dst id" }, 400);
      const dstRes = await resolveNodeEndpoint(c, dst);
      if ("error" in dstRes) return c.json({ ok: false, error: "dst: " + dstRes.error, dst }, 400);
      if (!dstRes.node) mints.push(dstRes.mint);
    }
    if (src === dst) return c.json({ ok: false, error: "src and dst are identical" }, 400);

    const db = drizzle(c.env.DB);
    // persist any newly-validated Wikidata concept nodes (idempotent on the QID)
    const nowMs = Date.now();
    for (const m of mints) {
      await db
        .insert(brainNodes)
        .values({
          id: m.id,
          label: m.label,
          description: m.description || null,
          nodeType: "concept",
          addedBy: user.id,
          actorType,
          status: "live",
          createdAt: nowMs,
          version: 1,
        })
        .onConflictDoNothing();
    }
    // dedupe: idempotent on an existing live (src,dst,kind).
    const dup = (
      await db
        .select({ id: brainEdges.id })
        .from(brainEdges)
        .where(
          and(
            eq(brainEdges.src, src),
            eq(brainEdges.dst, dst),
            eq(brainEdges.kind, kind),
            eq(brainEdges.status, "live"),
          ),
        )
        .limit(1)
    )[0];
    if (dup) return c.json({ ok: true, id: dup.id, duplicate: true }, 200);

    const id = freshEdgeId();
    try {
      await db.insert(brainEdges).values({
        id,
        src,
        dst,
        kind,
        evidence: JSON.stringify(evidence),
        addedBy: user.id, // server-derived identity; never client-claimed
        actorType,
        status: "live",
        createdAt: Date.now(),
        version: 1,
      });
    } catch {
      // the partial unique index caught a concurrent duplicate — return it
      const ex = (
        await db
          .select({ id: brainEdges.id })
          .from(brainEdges)
          .where(
            and(
              eq(brainEdges.src, src),
              eq(brainEdges.dst, dst),
              eq(brainEdges.kind, kind),
              eq(brainEdges.status, "live"),
            ),
          )
          .limit(1)
      )[0];
      if (ex) return c.json({ ok: true, id: ex.id, duplicate: true }, 200);
      return c.json({ ok: false, error: "could not save edge" }, 500);
    }
    return c.json({ ok: true, id, actor_type: actorType, added_by: user.id }, 201);
  });

  // GET /api/brain/edges?id=<node> — live community overlay touching a node.
  app.get("/api/brain/edges", async (c) => {
    const id = str(c.req.query("id"));
    if (!BRAIN_ID_RE.test(id)) return c.json({ ok: false, error: "bad node id" }, 400);
    const db = drizzle(c.env.DB);
    const rows = await db
      .select()
      .from(brainEdges)
      .where(and(or(eq(brainEdges.src, id), eq(brainEdges.dst, id)), eq(brainEdges.status, "live")))
      .limit(500);
    const edges = rows.map((r) => ({
      id: r.id,
      src: r.src,
      dst: r.dst,
      kind: r.kind,
      evidence: safeParse(r.evidence),
      added_by: r.addedBy,
      actor_type: r.actorType,
      created_at: r.createdAt,
    }));

    // ---- cross-pollination: inferred xref-shared partners --------------------
    // A's external pages = its community xrefs (src=id) ∪ its STATIC xrefs (from
    // the shard). For each page, every OTHER node pointing at it — from the
    // static reverse index and from live community xrefs — is the same object
    // as A across databases (an xref-shared link nobody drew explicitly).
    const pages = new Set<string>();
    for (const r of rows) if (r.kind === "xref" && r.src === id) pages.add(r.dst);
    const resolved = await resolveBrainEntry(c, id);
    if (resolved) {
      const entry = resolved.entry as {
        edges?: { out?: Array<{ id: string; kind: string }>; in?: Array<{ id: string; kind: string }> };
      };
      for (const dir of ["out", "in"] as const)
        for (const x of entry.edges?.[dir] || []) if (x.kind === "xref") pages.add(x.id);
    }
    const shared: Array<{ node: string; via: string; db: string; value: string; source: string }> = [];
    if (pages.size) {
      const pageArr = [...pages].slice(0, 40);
      const seen = new Set<string>();
      const addPartner = (node: string, page: string, source: string) => {
        if (node === id || shared.length >= 100) return;
        const k = node + " " + page;
        if (seen.has(k)) return;
        seen.add(k);
        const parts = page.split(":");
        shared.push({ node, via: page, db: parts[1] || "", value: parts.slice(2).join(":"), source });
      };
      const idx = await getXrefIndex(c);
      for (const p of pageArr) for (const n of idx[p] || []) addPartner(n, p, "static");
      const comm = await db
        .select({ src: brainEdges.src, dst: brainEdges.dst })
        .from(brainEdges)
        .where(
          and(inArray(brainEdges.dst, pageArr), eq(brainEdges.kind, "xref"), eq(brainEdges.status, "live")),
        )
        .limit(500);
      for (const r of comm) addPartner(r.src, r.dst, "community");
    }

    // labels for community-added (brain_nodes) endpoints, so QID nodes the
    // static shards don't know about still render with their Wikidata name
    const refIds = [
      ...new Set([...rows.flatMap((r) => [r.src, r.dst]), ...shared.map((s) => s.node)].filter((x) =>
        QID_RE.test(x),
      )),
    ];
    let nodeLabels: Record<string, string> = {};
    if (refIds.length) {
      const nrows = await db
        .select({ id: brainNodes.id, label: brainNodes.label })
        .from(brainNodes)
        .where(and(inArray(brainNodes.id, refIds), eq(brainNodes.status, "live")))
        .limit(500);
      nodeLabels = Object.fromEntries(nrows.map((n) => [n.id, n.label]));
    }

    // live tail — never cache
    return c.json({ ok: true, id, edges, shared, node_labels: nodeLabels }, 200, { "Cache-Control": "no-store" });
  });

  // DELETE /api/brain/edge/:id — soft-delete (gravestone). Any logged-in user
  // may delete any edge; the gravestone records who (Jack's decision (a)).
  app.delete("/api/brain/edge/:id", (c) => deleteEdge(c));
  // form/no-verb-friendly alias for the same action
  app.post("/api/brain/edge/:id/delete", (c) => deleteEdge(c));
}

async function deleteEdge(c: Context<{ Bindings: Env }>): Promise<Response> {
  const bad = checkOrigin(c);
  if (bad) return bad;
  const user = await getUser(c);
  if (!user) return c.json({ ok: false, error: "login required" }, 401);
  const rl = isBearer(user.role)
    ? await c.env.BRAIN_API_LIMITER.limit({ key: `brainapi:${user.id}` })
    : await c.env.EDIT_LIMITER.limit({ key: `edit:${user.id}` });
  if (!rl.success) return c.json({ ok: false, error: "rate limited" }, 429);

  const id = c.req.param("id") ?? "";
  if (!EDGE_ID_RE.test(id)) return c.json({ ok: false, error: "bad edge id" }, 400);
  const db = drizzle(c.env.DB);
  const row = (await db.select().from(brainEdges).where(eq(brainEdges.id, id)).limit(1))[0];
  if (!row) return c.json({ ok: false, error: "unknown edge id" }, 404);
  if (row.status === "deleted") return c.json({ ok: true, id, already_deleted: true }, 200);
  await db
    .update(brainEdges)
    .set({ status: "deleted", deletedBy: user.id, deletedAt: Date.now(), version: row.version + 1 })
    .where(and(eq(brainEdges.id, id), eq(brainEdges.status, "live")));
  return c.json({ ok: true, id, deleted_by: user.id }, 200);
}
