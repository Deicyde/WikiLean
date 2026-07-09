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
import { getUser, type AuthUser } from "./auth.js";
import { brainEdges, brainNodes } from "./db/schema.js";
import { brainNodeExists, resolveBrainEntry, BRAIN_ID_RE } from "./brain.js";
import { crossRefSpec } from "./crossref.js";
import { htmlEscape } from "./engine/html.js";
import type { QueueBlob, QueueItem } from "./queue.js";

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
const MAX_QUICK_ROWS = 100;
const MAX_DECL = 240;
const MAX_FILE = 300;
const MAX_LABEL = 200;
// dst upper bound: a real node id is already ≤400 (BRAIN_ID_RE), but the xref
// branch's `xref:<db>:<value>` value is otherwise unbounded — cap the whole dst
// so an xref value can't store an oversized row (external-DB keys are short).
const MAX_DST = 512;
const EDGE_ID_RE = /^[0-9a-f]{12}$/;
type ActorType = "human" | "ai";

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

function actorTypeFor(bearer: boolean, body: Record<string, unknown>): ActorType | string {
  const declared = str(body.actor_type);
  if (!bearer && !declared) return "human";
  if (declared !== "human" && declared !== "ai") return "API calls must set actor_type to 'human' or 'ai'";
  return declared;
}

async function insertMintedNodes(
  c: Context<{ Bindings: Env }>,
  user: AuthUser,
  actorType: ActorType,
  mints: Array<{ id: string; label: string; description: string }>,
): Promise<void> {
  if (!mints.length) return;
  const db = drizzle(c.env.DB);
  const nowMs = Date.now();
  const seen = new Set<string>();
  for (const m of mints) {
    if (seen.has(m.id)) continue;
    seen.add(m.id);
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
}

async function insertCommunityEdge(
  c: Context<{ Bindings: Env }>,
  user: AuthUser,
  actorType: ActorType,
  src: string,
  dst: string,
  kind: string,
  evidence: Record<string, unknown>,
): Promise<{ id: string; duplicate: boolean }> {
  const db = drizzle(c.env.DB);
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
  if (dup) return { id: dup.id, duplicate: true };

  const id = freshEdgeId();
  try {
    await db.insert(brainEdges).values({
      id,
      src,
      dst,
      kind,
      evidence: JSON.stringify(evidence),
      addedBy: user.id,
      actorType,
      status: "live",
      createdAt: Date.now(),
      version: 1,
    });
    return { id, duplicate: false };
  } catch {
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
    if (ex) return { id: ex.id, duplicate: true };
    throw new Error("could not save edge");
  }
}

interface QuickInputRow {
  lmfdb_id?: unknown;
  lmfdb?: unknown;
  lmfdb_knowl?: unknown;
  knowl?: unknown;
  id?: unknown;
  qid?: unknown;
  wikidata?: unknown;
  wikidata_qid?: unknown;
  concept_qid?: unknown;
  decl?: unknown;
  mathlib?: unknown;
  mathlib_decl?: unknown;
  file?: unknown;
  mathlib_file?: unknown;
  module?: unknown;
  mathlib_module?: unknown;
  label?: unknown;
  note?: unknown;
  notes?: unknown;
  comment?: unknown;
  description?: unknown;
  confidence?: unknown;
  [key: string]: unknown;
}

type QuickDbKey =
  | "mathlib"
  | "wikidata"
  | "brain"
  | "lmfdb"
  | "nlab"
  | "mathworld"
  | "proofwiki"
  | "oeis"
  | "stacks"
  | "kerodon"
  | "dlmf"
  | "metamath";

interface QuickDbSpec {
  key: QuickDbKey;
  label: string;
  endpoint: "node" | "xref";
  xrefDb?: string;
  aliases: string[];
}

const QUICK_DB_SPECS: Record<QuickDbKey, QuickDbSpec> = {
  mathlib: {
    key: "mathlib",
    label: "Mathlib",
    endpoint: "node",
    aliases: ["mathlib", "mathlib_decl", "decl", "lean", "lean_decl"],
  },
  wikidata: {
    key: "wikidata",
    label: "Wikidata",
    endpoint: "node",
    aliases: ["wikidata", "wikidata_qid", "qid", "concept_qid"],
  },
  brain: {
    key: "brain",
    label: "Brain node",
    endpoint: "node",
    aliases: ["brain", "brain_node", "brain_id", "node", "node_id"],
  },
  lmfdb: {
    key: "lmfdb",
    label: "LMFDB",
    endpoint: "xref",
    xrefDb: "lmfdb_knowl",
    aliases: ["lmfdb", "lmfdb_id", "lmfdb_knowl", "knowl", "id"],
  },
  nlab: { key: "nlab", label: "nLab", endpoint: "xref", xrefDb: "nlab", aliases: ["nlab", "nlab_id", "nlab_page"] },
  mathworld: {
    key: "mathworld",
    label: "MathWorld",
    endpoint: "xref",
    xrefDb: "mathworld",
    aliases: ["mathworld", "mathworld_id", "mathworld_page"],
  },
  proofwiki: {
    key: "proofwiki",
    label: "ProofWiki",
    endpoint: "xref",
    xrefDb: "proofwiki",
    aliases: ["proofwiki", "proofwiki_id", "proofwiki_page"],
  },
  oeis: { key: "oeis", label: "OEIS", endpoint: "xref", xrefDb: "oeis", aliases: ["oeis", "oeis_id"] },
  stacks: {
    key: "stacks",
    label: "Stacks",
    endpoint: "xref",
    xrefDb: "stacks",
    aliases: ["stacks", "stacks_id", "stacks_tag"],
  },
  kerodon: { key: "kerodon", label: "Kerodon", endpoint: "xref", xrefDb: "kerodon", aliases: ["kerodon", "kerodon_id"] },
  dlmf: { key: "dlmf", label: "DLMF", endpoint: "xref", xrefDb: "dlmf", aliases: ["dlmf", "dlmf_id"] },
  metamath: { key: "metamath", label: "Metamath", endpoint: "xref", xrefDb: "metamath", aliases: ["metamath", "metamath_id"] },
};
const DEFAULT_QUICK_DBS: QuickDbKey[] = ["mathlib", "lmfdb", "wikidata"];

interface QuickEndpoint {
  db: QuickDbKey;
  label: string;
  value: string;
  nodeId?: string;
  xrefId?: string;
  xrefDb?: string;
  decl?: string;
  file?: string;
  qid?: string;
  displayLabel?: string;
  mint?: { id: string; label: string; description: string };
}

interface QuickEdgeResult {
  src: string;
  dst: string;
  kind: string;
  id: string;
  duplicate: boolean;
}

interface QuickRowResult {
  ok: boolean;
  line: number;
  ids?: Record<string, string>;
  edges?: QuickEdgeResult[];
  queued?: boolean;
  queue_id?: string;
  queue_decl?: string;
  error?: string;
}

function bounded(v: unknown, max: number): string {
  if (typeof v !== "string") return "";
  return v.trim().slice(0, max);
}

function firstText(row: QuickInputRow, keys: string[], max: number): string {
  for (const k of keys) {
    const s = bounded(row[k], max);
    if (s) return s;
  }
  return "";
}

function normalizeHeader(s: string): string {
  return s.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
}

function selectedQuickDbs(body: Record<string, unknown>): QuickDbSpec[] {
  const raw = Array.isArray(body.databases)
    ? body.databases
    : Array.isArray(body.dbs)
      ? body.dbs
      : DEFAULT_QUICK_DBS;
  const seen = new Set<QuickDbKey>();
  for (const v of raw) {
    const key = normalizeHeader(String(v || "")) as QuickDbKey;
    if (key in QUICK_DB_SPECS) seen.add(key);
  }
  return [...seen].map((k) => QUICK_DB_SPECS[k]);
}

function quickHeaderSet(): Set<string> {
  const out = new Set<string>([
    "file", "mathlib_file", "module", "mathlib_module", "label", "note", "notes",
    "comment", "description", "confidence",
  ]);
  for (const spec of Object.values(QUICK_DB_SPECS)) for (const alias of spec.aliases) out.add(alias);
  return out;
}

function defaultHeadersFor(specs: QuickDbSpec[]): string[] {
  const headers: string[] = [];
  for (const sp of specs) headers.push(sp.aliases[0]);
  if (specs.some((sp) => sp.key === "mathlib")) headers.push("file");
  headers.push("note");
  return headers;
}

function splitDelimitedLine(line: string, delim: string): string[] {
  const out: string[] = [];
  let cur = "";
  let quoted = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (ch === '"') {
      if (quoted && line[i + 1] === '"') {
        cur += '"';
        i++;
      } else {
        quoted = !quoted;
      }
    } else if (ch === delim && !quoted) {
      out.push(cur.trim());
      cur = "";
    } else {
      cur += ch;
    }
  }
  out.push(cur.trim());
  return out;
}

function parseQuickText(text: string, specs: QuickDbSpec[]): QuickInputRow[] {
  const lines = text
    .replace(/\r/g, "")
    .split("\n")
    .map((l) => l.trimEnd())
    .filter((l) => l.trim() && !l.trim().startsWith("#"));
  if (!lines.length) return [];
  const delim = lines[0].includes("\t") ? "\t" : ",";
  const rawHead = splitDelimitedLine(lines[0], delim).map(normalizeHeader);
  const known = quickHeaderSet();
  const hasHeader = rawHead.some((h) => known.has(h));
  const headers = hasHeader ? rawHead : defaultHeadersFor(specs);
  const rows: QuickInputRow[] = [];
  const dataLines = hasHeader ? lines.slice(1) : lines;
  for (let i = 0; i < dataLines.length; i++) {
    const cells = splitDelimitedLine(dataLines[i], delim);
    const row: QuickInputRow = { line: i + (hasHeader ? 2 : 1) };
    for (let j = 0; j < headers.length; j++) row[headers[j] || `col_${j}`] = cells[j] || "";
    rows.push(row);
  }
  return rows;
}

function moduleToFile(module: string): string {
  const m = module.trim();
  if (m.startsWith("Mathlib.")) return m.replace(/\./g, "/") + ".lean";
  if (m.startsWith("Mathlib/") && m.endsWith(".lean")) return m;
  return "";
}

function normalizeFile(row: QuickInputRow): string {
  const file = firstText(row, ["file", "mathlib_file"], MAX_FILE);
  if (file) return moduleToFile(file) || (/^Mathlib\/[A-Za-z0-9_./-]+\.lean$/.test(file) ? file : "");
  const mod = firstText(row, ["module", "mathlib_module"], MAX_FILE);
  return mod ? moduleToFile(mod) : "";
}

function normalizeDecl(raw: string): string {
  let decl = raw.trim().replace(/^`+|`+$/g, "");
  if (decl.startsWith("decl:Mathlib:")) decl = decl.slice("decl:Mathlib:".length);
  if (!decl || decl.length > MAX_DECL || /[\s\p{C}/:]/u.test(decl)) return "";
  return decl;
}

function normalizeQid(raw: string): string {
  const s = raw.trim();
  return /^q[1-9][0-9]{0,11}$/i.test(s) ? "Q" + s.slice(1) : "";
}

function quickRowsFromBody(body: Record<string, unknown>, specs: QuickDbSpec[]): QuickInputRow[] {
  if (Array.isArray(body.items)) return body.items.filter((x) => typeof x === "object" && x !== null) as QuickInputRow[];
  const text = bounded(body.text ?? body.tsv ?? body.csv, 200000);
  return text ? parseQuickText(text, specs) : [];
}

function nodeLabelFromEntry(entry: object | undefined, fallback: string): string {
  const n = (entry as { node?: { label?: unknown } } | undefined)?.node;
  return typeof n?.label === "string" && n.label ? n.label : fallback;
}

async function fileForDecl(c: Context<{ Bindings: Env }>, declNode: string, supplied?: string): Promise<string> {
  if (supplied) return supplied;
  const resolved = await resolveBrainEntry(c, declNode);
  const node = (resolved?.entry as { node?: { module?: unknown } } | undefined)?.node;
  return typeof node?.module === "string" ? moduleToFile(node.module) : "";
}

function externalValue(raw: string, sp: QuickDbSpec): string | { error: string } {
  const value = raw.trim();
  if (!value) return "";
  if (sp.key === "lmfdb") {
    const lmfdb = value.toLowerCase();
    const spec = crossRefSpec("lmfdb")!;
    if (!spec.idPattern.test(lmfdb)) return { error: "bad LMFDB knowl id" };
    return lmfdb;
  }
  if (/[\p{C}]/u.test(value) || value.length > 240) return { error: `bad ${sp.label} id` };
  return value;
}

async function endpointForDb(
  c: Context<{ Bindings: Env }>,
  row: QuickInputRow,
  sp: QuickDbSpec,
): Promise<QuickEndpoint | { error: string } | null> {
  const raw = firstText(row, sp.aliases, sp.key === "mathlib" ? MAX_DECL : 240);
  if (!raw) return null;
  if (sp.key === "mathlib") {
    const decl = normalizeDecl(raw);
    if (!decl) return { error: "bad Mathlib declaration name" };
    const nodeId = `decl:Mathlib:${decl}`;
    const resolved = await resolveBrainEntry(c, nodeId);
    if (!resolved) return { error: "Mathlib declaration is not in the Brain" };
    const file = await fileForDecl(c, nodeId, normalizeFile(row) || undefined);
    if (!file) return { error: "Mathlib file is required when the Brain node has no module" };
    return { db: sp.key, label: sp.label, value: decl, nodeId, decl, file, displayLabel: nodeLabelFromEntry(resolved.entry, decl) };
  }
  if (sp.key === "wikidata") {
    const qid = normalizeQid(raw);
    if (!qid || !QID_RE.test(qid)) return { error: "bad Wikidata QID" };
    const res = await resolveNodeEndpoint(c, qid);
    if ("error" in res) return { error: "qid: " + res.error };
    const displayLabel = res.node ? nodeLabelFromEntry((await resolveBrainEntry(c, qid))?.entry, qid) : res.mint.label;
    return { db: sp.key, label: sp.label, value: qid, nodeId: qid, qid, displayLabel, mint: res.node ? undefined : res.mint };
  }
  if (sp.key === "brain") {
    const nodeId = raw.trim();
    if (!BRAIN_ID_RE.test(nodeId)) return { error: "bad Brain node id" };
    const res = await resolveNodeEndpoint(c, nodeId);
    if ("error" in res) return { error: "brain node: " + res.error };
    const displayLabel = res.node ? nodeLabelFromEntry((await resolveBrainEntry(c, nodeId))?.entry, nodeId) : res.mint.label;
    return { db: sp.key, label: sp.label, value: nodeId, nodeId, displayLabel, mint: res.node ? undefined : res.mint };
  }
  const value = externalValue(raw, sp);
  if (typeof value !== "string") return value;
  const xrefDb = sp.xrefDb || sp.key;
  if (!XREF_DBS.has(xrefDb)) return { error: `unknown xref db '${xrefDb}'` };
  const xrefId = `xref:${xrefDb}:${value}`;
  if (xrefId.length > MAX_DST) return { error: `${sp.label} id is too long` };
  return { db: sp.key, label: sp.label, value, xrefDb, xrefId, displayLabel: value };
}

function idsForEndpoints(endpoints: QuickEndpoint[]): Record<string, string> {
  return Object.fromEntries(endpoints.map((e) => [e.db, e.value]));
}

function edgeEvidence(
  row: QuickInputRow,
  endpoints: QuickEndpoint[],
  actorType: ActorType,
  extra: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    note: firstText(row, ["note", "notes", "comment", "description"], MAX_NOTE),
    source: "quickstatements",
    submitted_at: new Date().toISOString(),
    actor_type: actorType,
    ids: idsForEndpoints(endpoints),
    ...extra,
  };
}

function nodePairKind(a: QuickEndpoint, b: QuickEndpoint): { kind: string; src: QuickEndpoint; dst: QuickEndpoint } {
  if (a.db === "wikidata" && b.db === "mathlib") return { kind: "formalizes", src: a, dst: b };
  if (b.db === "wikidata" && a.db === "mathlib") return { kind: "formalizes", src: b, dst: a };
  return { kind: "relates", src: a, dst: b };
}

async function processQuickRow(
  c: Context<{ Bindings: Env }>,
  user: AuthUser,
  actorType: ActorType,
  specs: QuickDbSpec[],
  row: QuickInputRow,
  fallbackLine: number,
): Promise<{ result: QuickRowResult; item?: QueueItem }> {
  const line = typeof row.line === "number" ? row.line : fallbackLine;
  const endpoints: QuickEndpoint[] = [];
  for (const sp of specs) {
    const ep = await endpointForDb(c, row, sp);
    if (!ep) continue;
    if ("error" in ep) return { result: { ok: false, line, error: ep.error } };
    endpoints.push(ep);
  }
  if (endpoints.length < 2) return { result: { ok: false, line, error: "row needs at least two database identifiers" } };
  const nodes = endpoints.filter((e) => e.nodeId);
  const xrefs = endpoints.filter((e) => e.xrefId);
  if (!nodes.length) return { result: { ok: false, line, ids: idsForEndpoints(endpoints), error: "row needs at least one selected database with Brain nodes" } };

  const mints = endpoints.flatMap((e) => e.mint ? [e.mint] : []);
  await insertMintedNodes(c, user, actorType, mints);

  const edges: QuickEdgeResult[] = [];
  for (const n of nodes) {
    for (const x of xrefs) {
      const saved = await insertCommunityEdge(c, user, actorType, n.nodeId!, x.xrefId!, "xref", edgeEvidence(row, endpoints, actorType, {
        db: x.xrefDb,
        value: x.value,
        assertion: `${n.db}-${x.db}-xref`,
      }));
      edges.push({ src: n.nodeId!, dst: x.xrefId!, kind: "xref", ...saved });
    }
  }
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const pair = nodePairKind(nodes[i], nodes[j]);
      const saved = await insertCommunityEdge(c, user, actorType, pair.src.nodeId!, pair.dst.nodeId!, pair.kind, edgeEvidence(row, endpoints, actorType));
      edges.push({ src: pair.src.nodeId!, dst: pair.dst.nodeId!, kind: pair.kind, ...saved });
    }
  }
  if (!edges.length) return { result: { ok: false, line, ids: idsForEndpoints(endpoints), error: "row did not generate any Brain edges" } };

  let item: QueueItem | undefined;
  const mathlib = endpoints.find((e) => e.db === "mathlib" && e.nodeId);
  const lmfdb = endpoints.find((e) => e.db === "lmfdb" && e.xrefId);
  if (mathlib?.decl && lmfdb) {
    const qid = endpoints.find((e) => e.db === "wikidata" && e.qid);
    const direct = edges.find((e) => e.src === mathlib.nodeId && e.dst === lmfdb.xrefId && e.kind === "xref");
    item = {
      db: "lmfdb",
      id: lmfdb.value,
      concept_qid: qid?.qid,
      label: firstText(row, ["label"], MAX_LABEL) || qid?.displayLabel || lmfdb.value,
      decl: mathlib.decl,
      file: mathlib.file,
      status: "brain",
      source: "quickstatements",
      priority_source: "community-bulk",
      provenance_tier: actorType === "human" ? "community-human" : "community-ai",
      brain_node: qid?.nodeId || mathlib.nodeId,
      decl_node: mathlib.nodeId,
      actor_type: actorType,
      added_by: user.id,
      brain_edge_id: direct?.id,
      confidence: firstText(row, ["confidence"], 40) || "medium",
      review_reason: "Bulk database connection submission linked LMFDB to a Mathlib declaration",
      added: new Date().toISOString(),
    };
  }
  return {
    item,
    result: {
      ok: true,
      line,
      ids: idsForEndpoints(endpoints),
      edges,
      queued: !!item,
      queue_id: item?.id,
      queue_decl: item?.decl,
    },
  };
}

function queueItemKey(item: QueueItem): string {
  return JSON.stringify([item.db || "lmfdb", item.id || item.qid || "", item.decl || ""]);
}

async function upsertQuickQueue(env: Env, items: QueueItem[]): Promise<number> {
  const spec = crossRefSpec("lmfdb")!;
  let existing: QueueBlob = { db: spec.db, updated: "", items: [] };
  const raw = await env.RENDER_CACHE.get(spec.queueKey);
  if (raw) {
    try {
      const parsed = JSON.parse(raw) as QueueBlob;
      existing = { db: spec.db, updated: parsed.updated || "", items: Array.isArray(parsed.items) ? parsed.items : [] };
    } catch {
      existing = { db: spec.db, updated: "", items: [] };
    }
  }
  const out = existing.items.slice();
  const index = new Map<string, number>();
  out.forEach((item, i) => index.set(queueItemKey(item), i));
  for (const item of items) {
    const key = queueItemKey(item);
    const at = index.get(key);
    if (at === undefined) {
      index.set(key, out.length);
      out.push(item);
    } else {
      const prev = out[at];
      out[at] = {
        ...prev,
        ...item,
        notes: prev.notes,
        added: prev.added || item.added,
      };
    }
  }
  const blob: QueueBlob = { db: spec.db, updated: new Date().toISOString(), items: out };
  await env.RENDER_CACHE.put(spec.queueKey, JSON.stringify(blob));
  return out.length;
}

function quickStatementsPageHtml(): string {
  const sample = "lmfdb_id\tqid\tdecl\tfile\tnote\n" +
    "group.abelian\tQ181296\tCommGroup\tMathlib/Algebra/Group/Defs.lean\tTentative LMFDB match";
  const sampleValues: Partial<Record<QuickDbKey, string>> = {
    mathlib: "CommGroup",
    wikidata: "Q181296",
    brain: "Q181296",
    lmfdb: "group.abelian",
    nlab: "abelian_group",
    mathworld: "AbelianGroup",
    proofwiki: "Definition:Abelian_Group",
    oeis: "A000045",
    stacks: "001A",
    kerodon: "tag/0001",
    dlmf: "1.2.E1",
    metamath: "df-grp",
  };
  const clientSpecs = Object.values(QUICK_DB_SPECS).map((sp) => ({
    key: sp.key,
    label: sp.label,
    node: sp.endpoint === "node",
    xrefDb: sp.xrefDb || "",
    aliases: sp.aliases,
    sample: sampleValues[sp.key] || sp.key + "_id",
  }));
  const dbChecks = clientSpecs
    .map(
      (sp) =>
        `<label class="check"><input class="db-check" data-db="${htmlEscape(sp.key)}" type="checkbox"` +
        (DEFAULT_QUICK_DBS.includes(sp.key as QuickDbKey) ? " checked" : "") +
        `>${htmlEscape(sp.label)} <small>${sp.node ? "Brain node" : "external"}</small></label>`,
    )
    .join("");
  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WikiLean · Bulk database connections</title>
<style>
:root{color-scheme:dark;--bg:#07111f;--panel:#0d1b2e;--panel2:#10233d;--line:#203653;--line2:#315174;--ink:#eaf2ff;--muted:#9bb1cc;--accent:#67b7ff;--good:#91d18b;--bad:#ff9b9b;--warn:#f2c86d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;line-height:1.5}
main{max-width:1040px;margin:0 auto;padding:28px 16px 40px}
header{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;margin-bottom:18px}
h1{margin:0;font-size:1.45rem;font-weight:680}.sub{margin:4px 0 0;color:var(--muted);font-size:.92rem}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.links{display:flex;gap:10px;flex-wrap:wrap;font-size:.9rem}.links a{border:1px solid var(--line);border-radius:8px;padding:6px 10px;background:rgba(255,255,255,.03)}
.grid{display:grid;grid-template-columns:minmax(0,1.05fr) minmax(300px,.95fr);gap:16px}@media(max-width:760px){.grid{grid-template-columns:1fr}header{align-items:flex-start;flex-direction:column}}
section{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px}
label,.label{display:block;color:var(--muted);font-size:.8rem;text-transform:uppercase;letter-spacing:.04em;margin-bottom:7px}
textarea{width:100%;min-height:332px;resize:vertical;border:1px solid var(--line2);border-radius:8px;background:#08172a;color:var(--ink);padding:12px;font:13px/1.45 "SF Mono",Menlo,Consolas,monospace}
textarea:focus,button:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.topbar{display:grid;grid-template-columns:minmax(210px,.9fr) minmax(230px,1.1fr);gap:10px;margin-bottom:12px}@media(max-width:760px){.topbar{grid-template-columns:1fr}}
.seg{display:grid;grid-template-columns:1fr 1fr;border:1px solid var(--line2);border-radius:8px;overflow:hidden;background:#08172a}
.seg input{position:absolute;opacity:0;pointer-events:none}.seg label{margin:0;padding:8px 10px;text-align:center;cursor:pointer;color:var(--muted);font-size:.9rem;text-transform:none;letter-spacing:0}
.seg input:checked+label{background:#1d6fb8;color:white}.seg label+input+label{border-left:1px solid var(--line2)}
.db-menu{position:relative}.db-menu summary{list-style:none;cursor:pointer;border:1px solid var(--line2);border-radius:8px;background:#08172a;color:var(--ink);padding:8px 10px;font-size:.92rem}.db-menu summary::-webkit-details-marker{display:none}
.db-menu summary:after{content:"v";float:right;color:var(--muted)}.db-menu[open] summary{border-bottom-left-radius:0;border-bottom-right-radius:0}
.db-panel{border:1px solid var(--line2);border-top:0;border-radius:0 0 8px 8px;background:#08172a;padding:8px 10px;display:grid;grid-template-columns:1fr 1fr;gap:7px}@media(max-width:540px){.db-panel{grid-template-columns:1fr}}
.check{display:flex;align-items:center;gap:8px;color:var(--ink);font-size:.92rem}.check input{accent-color:#1d6fb8}.check small{color:var(--muted)}
.controls{display:flex;align-items:center;gap:10px;margin-top:12px;flex-wrap:wrap}
button{border:1px solid #4694d8;border-radius:8px;background:#1d6fb8;color:white;font:inherit;font-weight:650;padding:9px 13px;cursor:pointer}
button:hover{background:#2380d2}button:disabled{opacity:.55;cursor:wait}
.hint{color:var(--muted);font-size:.86rem}.hint code{color:#d7e8ff}
.format-guide{margin-top:12px;border-top:1px solid var(--line);padding-top:10px;color:var(--muted);font-size:.86rem}.format-guide summary{cursor:pointer;color:var(--ink);font-weight:650}.format-guide p{margin:8px 0}.format-guide ul{margin:7px 0 0 18px;padding:0}.format-guide li{margin:3px 0}.format-guide pre{margin:8px 0 0;overflow:auto;border-radius:8px;background:#08172a;border:1px solid var(--line);padding:9px;color:#d7e8ff;font:12px/1.5 "SF Mono",Menlo,Consolas,monospace}
.summary{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-bottom:12px}
.metric{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:10px}.metric b{display:block;font-size:1.35rem}.metric span{color:var(--muted);font-size:.8rem}
.preview{margin-bottom:14px}.preview-head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:7px}.preview-head h2{font-size:.8rem;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;margin:0}.edge-count{font-size:.8rem;color:var(--muted)}
.edges{display:flex;flex-direction:column;gap:7px;max-height:240px;overflow:auto}.edge{border:1px solid var(--line);border-radius:8px;background:rgba(255,255,255,.025);padding:8px;font-size:.85rem}.edge .rel{color:var(--warn);font-family:"SF Mono",Menlo,Consolas,monospace;margin:0 6px}.edge .src,.edge .dst{word-break:break-word}.empty-preview{color:var(--muted);font-size:.9rem;border:1px dashed var(--line);border-radius:8px;padding:12px}
.results{display:flex;flex-direction:column;gap:8px}.row{border:1px solid var(--line);border-radius:8px;background:rgba(255,255,255,.025);padding:9px 10px;font-size:.9rem}
.row.ok{border-left:3px solid var(--good)}.row.bad{border-left:3px solid var(--bad)}.row .meta{color:var(--muted);font-size:.8rem;margin-top:2px}
.status{min-height:1.4em;color:var(--muted);font-size:.9rem}
code{font-family:"SF Mono",Menlo,Consolas,monospace}
</style></head><body><main>
<header><div><h1>Bulk database connections</h1><p class="sub">Select databases and paste rows to create Brain edges.</p></div>
<nav class="links"><a href="/queue/lmfdb">LMFDB queue</a><a href="/brain">Brain</a></nav></header>
<div class="grid">
<section>
<div class="topbar">
<div><div class="label">Provenance</div><div class="seg" role="radiogroup" aria-label="Tag provenance"><input id="actor-human" name="actor" type="radio" value="human" checked><label for="actor-human">Human-generated</label><input id="actor-ai" name="actor" type="radio" value="ai"><label for="actor-ai">AI-generated</label></div></div>
<div><div class="label">Database columns</div><details class="db-menu"><summary id="db-summary"></summary><div class="db-panel">${dbChecks}</div></details></div>
</div>
<label for="qs-rows">Rows</label><textarea id="qs-rows" spellcheck="false">${htmlEscape(sample)}</textarea>
<div class="controls"><button id="submit" type="button">Submit rows</button><span id="status" class="status"></span></div>
<p class="hint">Select at least two databases, including at least one Brain-node database. Up to ${MAX_QUICK_ROWS} rows per submit.</p>
<details class="format-guide" open><summary>Input format</summary>
<p>Use one row per connection. Tab-separated values are recommended; comma-separated CSV also works when the first line has no tabs. The example below uses <code>&lt;Tab&gt;</code> markers to show where real tab characters go.</p>
<ul><li>The first line may be headers such as <code>mathlib</code>, <code>wikidata</code>, <code>lmfdb</code>, <code>nlab</code>, <code>file</code>, and <code>note</code>.</li><li>If there is no header row, columns follow the selected databases in order, then <code>file</code> when Mathlib is selected, then <code>note</code>.</li><li>Blank lines and lines starting with <code>#</code> are ignored.</li></ul>
<pre>mathlib&lt;Tab&gt;wikidata&lt;Tab&gt;lmfdb&lt;Tab&gt;file&lt;Tab&gt;note
CommGroup&lt;Tab&gt;Q181296&lt;Tab&gt;group.abelian&lt;Tab&gt;Mathlib/Algebra/Group/Defs.lean&lt;Tab&gt;Tentative match</pre>
</details></section>
<section><div class="summary"><div class="metric"><b id="accepted">0</b><span>accepted</span></div><div class="metric"><b id="failed">0</b><span>failed</span></div><div class="metric"><b id="queued">0</b><span>queue size</span></div></div>
<div class="preview"><div class="preview-head"><h2>Generated Brain edges</h2><span id="edge-count" class="edge-count">0 edges</span></div><div id="edges" class="edges"></div></div>
<div id="results" class="results"></div></section>
</div></main>
<script>
const rows = document.getElementById("qs-rows");
const btn = document.getElementById("submit");
const statusEl = document.getElementById("status");
const results = document.getElementById("results");
const edgeList = document.getElementById("edges");
const edgeCount = document.getElementById("edge-count");
const dbSummary = document.getElementById("db-summary");
const dbChecks = Array.from(document.querySelectorAll(".db-check"));
const DB_SPECS = ${JSON.stringify(clientSpecs)};
const initialSample = ${JSON.stringify(sample)};
let dirty = false;
const setText = (id, value) => { document.getElementById(id).textContent = String(value); };
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, ch => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;" }[ch]));
function selectedSpecs() {
  const keys = new Set(dbChecks.filter(c => c.checked).map(c => c.dataset.db));
  return DB_SPECS.filter(sp => keys.has(sp.key));
}
function splitLine(line, delim) {
  const out = []; let cur = ""; let quoted = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (ch === '"') {
      if (quoted && line[i + 1] === '"') { cur += '"'; i++; } else { quoted = !quoted; }
    } else if (ch === delim && !quoted) { out.push(cur.trim()); cur = ""; }
    else { cur += ch; }
  }
  out.push(cur.trim()); return out;
}
function normHead(s) { return String(s || "").trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, ""); }
function parseRows() {
  const selected = selectedSpecs();
  const lines = rows.value.replace(/\\r/g, "").split("\\n").map(l => l.trimEnd()).filter(l => l.trim() && !l.trim().startsWith("#"));
  if (!lines.length) return [];
  const delim = lines[0].includes("\\t") ? "\\t" : ",";
  const first = splitLine(lines[0], delim).map(normHead);
  const known = new Set(["file","mathlib_file","module","mathlib_module","label","note","notes","comment","description","confidence"]);
  DB_SPECS.forEach(sp => sp.aliases.forEach(a => known.add(a)));
  const hasHeader = first.some(h => known.has(h));
  const headers = hasHeader ? first : defaultHeaders(selected);
  const data = hasHeader ? lines.slice(1) : lines;
  return data.map((line, idx) => {
    const cells = splitLine(line, delim);
    const row = { line: idx + (hasHeader ? 2 : 1) };
    headers.forEach((h, i) => row[h || ("col_" + i)] = cells[i] || "");
    return row;
  });
}
function defaultHeaders(selected) {
  const headers = selected.map(sp => sp.aliases[0]);
  if (selected.some(sp => sp.key === "mathlib")) headers.push("file");
  headers.push("note");
  return headers;
}
function first(row, names) {
  for (const n of names) if (row[n]) return String(row[n]).trim();
  return "";
}
function declNode(decl) {
  let d = String(decl || "").trim().replace(/^\\x60+|\\x60+$/g, "");
  if (d.startsWith("decl:Mathlib:")) d = d.slice("decl:Mathlib:".length);
  return d ? "decl:Mathlib:" + d : "";
}
function generatedEdges() {
  const edges = [];
  const selected = selectedSpecs();
  for (const row of parseRows()) {
    const nodes = [];
    const xrefs = [];
    for (const sp of selected) {
      const raw = first(row, sp.aliases);
      if (!raw) continue;
      if (sp.key === "mathlib") nodes.push({ db: sp.key, id: declNode(raw) });
      else if (sp.key === "wikidata") nodes.push({ db: sp.key, id: raw.toUpperCase() });
      else if (sp.node) nodes.push({ db: sp.key, id: raw });
      else xrefs.push({ db: sp.key, id: "xref:" + sp.xrefDb + ":" + raw });
    }
    for (const n of nodes) for (const x of xrefs) if (n.id && x.id) edges.push({ src: n.id, rel: "xref", dst: x.id });
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j];
        const formal = (a.db === "wikidata" && b.db === "mathlib") || (b.db === "wikidata" && a.db === "mathlib");
        const src = formal ? (a.db === "wikidata" ? a.id : b.id) : a.id;
        const dst = formal ? (a.db === "wikidata" ? b.id : a.id) : b.id;
        if (src && dst) edges.push({ src, rel: formal ? "formalizes" : "relates", dst });
      }
    }
  }
  return edges;
}
function refreshPreview() {
  const selected = selectedSpecs();
  dbSummary.textContent = selected.length ? selected.map(sp => sp.label).join(", ") : "Select databases";
  const edges = generatedEdges();
  edgeCount.textContent = edges.length + (edges.length === 1 ? " edge" : " edges");
  edgeList.innerHTML = edges.length
    ? edges.slice(0, 120).map(e => '<div class="edge"><span class="src"><code>'+esc(e.src)+'</code></span><span class="rel">'+esc(e.rel)+'</span><span class="dst"><code>'+esc(e.dst)+'</code></span></div>').join("")
    : '<div class="empty-preview">No complete rows yet.</div>';
}
function refreshSample() {
  if (dirty) return;
  const selected = selectedSpecs();
  const headers = defaultHeaders(selected);
  const values = headers.map(h => {
    if (h === "file") return "Mathlib/Algebra/Group/Defs.lean";
    if (h === "note") return "Tentative database connection";
    const spec = selected.find(sp => sp.aliases[0] === h);
    return spec ? spec.sample : "";
  });
  rows.value = headers.join("\\t") + "\\n" + values.join("\\t");
  refreshPreview();
}
rows.addEventListener("input", () => { dirty = true; refreshPreview(); });
dbChecks.forEach(c => c.addEventListener("change", refreshSample));
document.querySelectorAll("input[name=actor]").forEach(el => el.addEventListener("change", refreshPreview));
refreshPreview();
btn.addEventListener("click", async () => {
  btn.disabled = true; statusEl.textContent = "Submitting...";
  try {
    const actor = document.querySelector("input[name=actor]:checked").value;
    const selected = selectedSpecs();
    const res = await fetch("/api/brain/quickstatements", { method:"POST", headers:{ "Content-Type":"application/json" }, body:JSON.stringify({ databases: selected.map(sp => sp.key), actor_type: actor, items: parseRows() }) });
    const json = await res.json().catch(() => ({}));
    if (res.status === 401) { statusEl.innerHTML = '<a href="/login?returnTo=/quickstatements">Sign in to submit</a>'; return; }
    setText("accepted", json.accepted || 0); setText("failed", json.failed || 0); setText("queued", json.queue_count || 0);
    results.innerHTML = (json.rows || []).map((r) => {
      const ids = r.ids ? Object.entries(r.ids).map(([k,v]) => k + ":" + v).join(" / ") : "";
      const meta = r.error || [ids, r.edges ? (r.edges.length + " edge(s)") : "", r.queued ? "queued" : ""].filter(Boolean).join(" / ");
      return '<div class="row '+(r.ok?'ok':'bad')+'"><div>'+(r.ok?'accepted':'failed')+' line '+esc(r.line)+'</div><div class="meta">'+esc(meta)+'</div></div>';
    }).join("");
    statusEl.textContent = json.ok ? "Done" : (json.error || "No rows accepted");
  } catch (e) {
    statusEl.textContent = "Submit failed";
  } finally {
    btn.disabled = false;
  }
});
</script></body></html>`;
}

export function registerBrainEditRoutes(app: Hono<{ Bindings: Env }>): void {
  app.get("/quickstatements", (c) => c.html(quickStatementsPageHtml(), 200, { "Cache-Control": "no-store" }));
  app.get("/brain/quickstatements", (c) => c.html(quickStatementsPageHtml(), 200, { "Cache-Control": "no-store" }));

  // POST /api/brain/quickstatements — bulk-add database connections as Brain
  // edges. Rows that include both LMFDB and Mathlib also upsert the LMFDB queue.
  app.post("/api/brain/quickstatements", async (c) => {
    const bad = checkOrigin(c);
    if (bad) return bad;
    const user = await getUser(c);
    if (!user) return c.json({ ok: false, error: "login required" }, 401);
    const bearer = isBearer(user.role);
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
    const actor = actorTypeFor(bearer, body);
    if (actor !== "human" && actor !== "ai") return c.json({ ok: false, error: actor }, 400);
    const specs = selectedQuickDbs(body);
    if (specs.length < 2) return c.json({ ok: false, error: "select at least two databases" }, 400);
    if (!specs.some((sp) => sp.endpoint === "node")) {
      return c.json({ ok: false, error: "select at least one database with Brain nodes" }, 400);
    }
    const rows = quickRowsFromBody(body, specs);
    if (!rows.length) return c.json({ ok: false, error: "no rows supplied", accepted: 0, failed: 0, rows: [] }, 400);
    if (rows.length > MAX_QUICK_ROWS) {
      return c.json({ ok: false, error: `too many rows (max ${MAX_QUICK_ROWS})`, accepted: 0, failed: rows.length, rows: [] }, 400);
    }

    const results: QuickRowResult[] = [];
    const items: QueueItem[] = [];
    for (let i = 0; i < rows.length; i++) {
      try {
        const out = await processQuickRow(c, user, actor, specs, rows[i], i + 1);
        results.push(out.result);
        if (out.item) items.push(out.item);
      } catch {
        results.push({ ok: false, line: i + 1, error: "could not save row" });
      }
    }

    let queueCount = 0;
    if (items.length) {
      try {
        queueCount = await upsertQuickQueue(c.env, items);
      } catch {
        return c.json({ ok: false, error: "accepted rows but could not update the queue", accepted: items.length, failed: results.length - items.length, rows: results }, 500);
      }
    }
    const accepted = results.filter((r) => r.ok).length;
    const failed = results.length - accepted;
    return c.json(
      { ok: accepted > 0, databases: specs.map((sp) => sp.key), accepted, failed, queue_count: queueCount, rows: results },
      accepted > 0 ? 200 : 400,
    );
  });

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
    // the evidence note is optional; only cap its length when present
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

  // POST /api/brain/node — introduce a NEW concept node (a validated Wikidata
  // item), distinct from adding an edge. No connection is created.
  app.post("/api/brain/node", async (c) => {
    const bad = checkOrigin(c);
    if (bad) return bad;
    const user = await getUser(c);
    if (!user) return c.json({ ok: false, error: "login required" }, 401);
    const bearer = isBearer(user.role);
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
    const qid = str(body.qid) || str(body.id);
    let actorType: "human" | "ai";
    if (bearer) {
      const declared = str(body.actor_type);
      if (declared !== "human" && declared !== "ai")
        return c.json({ ok: false, error: "API calls must set actor_type to 'human' or 'ai'" }, 400);
      actorType = declared;
    } else {
      actorType = "human";
    }
    if (!QID_RE.test(qid)) return c.json({ ok: false, error: "node must be a Wikidata Q-id" }, 400);
    // already a static node → nothing to add
    if (await brainNodeExists(c, qid)) return c.json({ ok: true, id: qid, existing: true }, 200);
    const wd = await validateWikidataQid(c, qid);
    if (!wd) return c.json({ ok: false, error: `${qid} is not a resolvable Wikidata item` }, 400);
    const db = drizzle(c.env.DB);
    await db
      .insert(brainNodes)
      .values({
        id: qid,
        label: wd.label,
        description: wd.description || null,
        nodeType: "concept",
        addedBy: user.id,
        actorType,
        status: "live",
        createdAt: Date.now(),
        version: 1,
      })
      .onConflictDoNothing();
    return c.json({ ok: true, id: qid, label: wd.label, actor_type: actorType, added_by: user.id }, 201);
  });

  // DELETE a community node (soft-delete gravestone). Only community-added nodes
  // (brain_nodes) can be deleted; static nodes have no D1 row.
  app.delete("/api/brain/node/:id", (c) => deleteNode(c));
  app.post("/api/brain/node/:id/delete", (c) => deleteNode(c));

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
        const k = node + "\0" + page;
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

    // if the focus node ITSELF is a community-added node (not in the static
    // shards), return its record so the page can render a minimal panel for it
    let self: { id: string; label: string; description: string | null; added_by: string; actor_type: string } | null = null;
    if (QID_RE.test(id)) {
      const selfRow = (
        await db.select().from(brainNodes).where(and(eq(brainNodes.id, id), eq(brainNodes.status, "live"))).limit(1)
      )[0];
      if (selfRow)
        self = { id: selfRow.id, label: selfRow.label, description: selfRow.description, added_by: selfRow.addedBy, actor_type: selfRow.actorType };
    }

    // live tail — never cache
    return c.json({ ok: true, id, edges, shared, node_labels: nodeLabels, self }, 200, { "Cache-Control": "no-store" });
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

async function deleteNode(c: Context<{ Bindings: Env }>): Promise<Response> {
  const bad = checkOrigin(c);
  if (bad) return bad;
  const user = await getUser(c);
  if (!user) return c.json({ ok: false, error: "login required" }, 401);
  const rl = isBearer(user.role)
    ? await c.env.BRAIN_API_LIMITER.limit({ key: `brainapi:${user.id}` })
    : await c.env.EDIT_LIMITER.limit({ key: `edit:${user.id}` });
  if (!rl.success) return c.json({ ok: false, error: "rate limited" }, 429);

  const id = c.req.param("id") ?? "";
  if (!QID_RE.test(id)) return c.json({ ok: false, error: "bad node id" }, 400);
  const db = drizzle(c.env.DB);
  const row = (await db.select().from(brainNodes).where(eq(brainNodes.id, id)).limit(1))[0];
  if (!row) return c.json({ ok: false, error: "unknown community node" }, 404);
  if (row.status === "deleted") return c.json({ ok: true, id, already_deleted: true }, 200);
  await db
    .update(brainNodes)
    .set({ status: "deleted", deletedBy: user.id, deletedAt: Date.now(), version: row.version + 1 })
    .where(and(eq(brainNodes.id, id), eq(brainNodes.status, "live")));
  return c.json({ ok: true, id, deleted_by: user.id }, 200);
}
