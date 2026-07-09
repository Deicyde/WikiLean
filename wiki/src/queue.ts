// WikiLean cross-reference tag QUEUE.
//
// A public read view of the pipeline's pending tags: "unreviewed" LLM-generated
// candidates, Brain-suggested formalizes edges, plus tags that were recycled
// after a reject/revise (carrying the reviewer notes + the LLM triage's retarget
// suggestion). The daily bot publishes Wikidata via POST /api/queue and other
// databases via POST /api/queue/:db (PIPELINE_TOKEN bearer / bot role).
// GET /queue renders Wikidata; GET /queue/:db renders a database-specific queue.
//
// Storage: a single JSON blob in KV (no TTL — durable state, not cache).

import type { Context, Hono } from "hono";
import type { Env } from "./env.js";
import { requireRole } from "./auth.js";
import { htmlEscape } from "./engine/html.js";
import { type CrossRefSpec, crossRefSpec, crossRefUrl } from "./crossref.js";

type Ctx = Context<{ Bindings: Env }>;

export interface QueueItem {
  db?: string;
  id?: string;
  qid?: string; // legacy Wikidata alias
  label?: string;
  decl: string; // target Mathlib declaration
  file?: string;
  status: "unreviewed" | "recycled" | "brain";
  orig_id?: string; // recycled item before retarget
  orig_qid?: string; // legacy Wikidata recycled item before retarget
  article_qid?: string; // source article concept when the tag QID was narrowed
  concept_qid?: string; // source concept for non-Wikidata databases
  centrality_pct?: number;
  brain_rank?: number;
  wikilink_rank?: number;
  rank_delta?: number;
  provenance_tier?: string;
  source_file?: string;
  priority_source?: string;
  review_reason?: string;
  brain_node?: string;
  decl_node?: string;
  source?: string;
  actor_type?: string;
  added_by?: string;
  brain_edge_id?: string;
  confidence?: string;
  notes?: Array<{ login: string; status: string; text: string }>;
  retarget?: string; // LLM triage fix hint (recycled items)
  added?: string; // ISO
}
export interface QueueBlob {
  db?: string;
  updated: string;
  items: QueueItem[];
}

async function readQueue(env: Env, spec: CrossRefSpec): Promise<QueueBlob> {
  const raw = await env.RENDER_CACHE.get(spec.queueKey);
  if (!raw) return { db: spec.db, updated: "", items: [] };
  try {
    const blob = JSON.parse(raw) as QueueBlob;
    return { db: spec.db, updated: blob.updated || "", items: blob.items || [] };
  } catch {
    return { db: spec.db, updated: "", items: [] };
  }
}

const STATUS_BADGE: Record<string, string> = {
  unreviewed: '<span class="qb un">◷ unreviewed</span>',
  recycled: '<span class="qb re">↻ recycled</span>',
  brain: '<span class="qb br">◇ Brain</span>',
};
const EMO: Record<string, string> = { approve: "🟢", revise: "🟡", reject: "🔴", flag: "⚠️", "(note)": "💬" };

function declDocsUrl(file: string | undefined, decl: string): string | null {
  if (!file || !/^Mathlib\//.test(file)) return null;
  return (
    "https://leanprover-community.github.io/mathlib4_docs/" +
    file.replace(/\.lean$/, ".html") +
    "#" +
    encodeURIComponent(decl)
  );
}

function crossrefLink(db: string, id: string): string {
  const s = htmlEscape(id);
  return `<a href="${crossRefUrl(db, id)}" target="_blank" rel="noopener">${s}</a>`;
}

function wikidataLink(qid: string): string {
  const q = htmlEscape(qid);
  return `<a href="${crossRefUrl("wikidata", qid)}" target="_blank" rel="noopener">${q}</a>`;
}

function chip(label: string, value: string): string {
  return `<span class="qchip"><b>${htmlEscape(label)}</b> ${value}</span>`;
}

function itemHtml(it: QueueItem, spec: CrossRefSpec): string {
  const esc = (s: string) => htmlEscape(s);
  const db = it.db || spec.db;
  const id = it.id || it.qid || "";
  const docs = declDocsUrl(it.file, it.decl);
  const declHtml = docs
    ? `<a href="${docs}" target="_blank" rel="noopener"><code>${esc(it.decl)}</code></a>`
    : `<code>${esc(it.decl)}</code>`;
  const notes =
    it.notes && it.notes.length
      ? `<ul class="qnotes">${it.notes
          .map(
            (n) =>
              `<li>${EMO[n.status] || ""} <span class="who">@${esc(n.login)}</span>: ${esc(n.text)}</li>`,
          )
          .join("")}</ul>`
      : "";
  const retarget = it.retarget ? `<div class="qfix">↳ retarget: ${esc(it.retarget)}</div>` : "";
  const meta: string[] = [];
  if (it.orig_id && it.orig_id !== id) meta.push(chip("from", crossrefLink(db, it.orig_id)));
  else if (it.orig_qid && it.orig_qid !== id) meta.push(chip("from", wikidataLink(it.orig_qid)));
  if (it.article_qid && it.article_qid !== id) meta.push(chip("article", wikidataLink(it.article_qid)));
  if (it.concept_qid && it.concept_qid !== id) meta.push(chip("concept", wikidataLink(it.concept_qid)));
  if (typeof it.centrality_pct === "number") meta.push(chip("centrality", esc(it.centrality_pct.toFixed(2))));
  if (typeof it.brain_rank === "number") meta.push(chip("brain rank", esc(String(it.brain_rank + 1))));
  if (typeof it.rank_delta === "number") meta.push(chip("delta", esc(String(it.rank_delta))));
  if (it.priority_source) meta.push(chip("priority", esc(it.priority_source)));
  if (it.provenance_tier) meta.push(chip("provenance", esc(it.provenance_tier)));
  if (it.source) meta.push(chip("source", esc(it.source)));
  if (it.confidence) meta.push(chip("confidence", esc(it.confidence)));
  if (it.brain_node) {
    meta.push(
      chip("Brain", `<a href="/brain#${encodeURIComponent(it.brain_node)}">${esc(it.brain_node)}</a>`),
    );
  }
  const metaHtml = meta.length ? `<div class="qmeta">${meta.join("")}</div>` : "";
  const reason = it.review_reason ? `<div class="qreason">${esc(it.review_reason)}</div>` : "";
  return (
    `<article class="qitem ${it.status}">` +
    `<div class="qhead">${STATUS_BADGE[it.status] || ""} ` +
    `<span class="qid">${crossrefLink(db, id)}</span>` +
    (it.label ? ` <span class="qlabel">${esc(it.label)}</span>` : "") +
    ` → ${declHtml}</div>` +
    metaHtml +
    notes +
    retarget +
    reason +
    `</article>`
  );
}

function queuePageHtml(blob: QueueBlob, spec: CrossRefSpec): string {
  const items = blob.items || [];
  const recycled = items.filter((i) => i.status === "recycled");
  const brain = items.filter((i) => i.status === "brain");
  const unreviewed = items.filter((i) => i.status === "unreviewed");
  const section = (title: string, list: QueueItem[]) =>
    list.length
      ? `<h2>${title} <span class="ct">${list.length}</span></h2>` + list.map((i) => itemHtml(i, spec)).join("")
      : "";
  const body =
    items.length === 0
      ? `<p class="empty">The queue is empty. The daily bot publishes pending tags here.</p>`
      : section("Recycled — needs a retarget", recycled) +
        section("Brain-suggested formalizes edges", brain) +
        section("Unreviewed candidates", unreviewed);
  const when = blob.updated ? `updated ${htmlEscape(blob.updated)}` : "";
  const tools =
    spec.db === "lmfdb"
      ? `<p class="qtools"><a href="/quickstatements">Bulk add LMFDB tags</a></p>`
      : "";
  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WikiLean · ${htmlEscape(spec.label)} tag queue</title>
<style>
:root{color-scheme:dark;--bg:#0b0e14;--card:#10141d;--card2:#151b28;--rule:#262c3a;--rule2:#33405c;--ink:#e6e4de;--muted:#9aa3b2;--accent:#7cb3ff;--accent2:#38bdf8;--code:#363333;--g:#8fe388;--y:#f0a202}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--ink);margin:0;padding:2rem 1rem;line-height:1.5}
main{max-width:820px;margin:0 auto}
h1{font-size:1.5rem;margin:0 0 .2rem}.sub{color:var(--muted);margin:0 0 1.5rem;font-size:.9rem}
.qtools{margin:-.8rem 0 1.4rem}.qtools a{display:inline-flex;border:1px solid var(--rule2);border-radius:8px;padding:.35rem .6rem;background:rgba(124,179,255,.1);text-decoration:none;font-size:.88rem}.qtools a:hover{text-decoration:none;background:rgba(124,179,255,.18)}
h2{font-size:1rem;margin:1.6rem 0 .6rem;color:var(--muted);text-transform:uppercase;letter-spacing:.03em}
.ct{background:rgba(124,179,255,.14);color:#c8ddff;border-radius:10px;padding:0 .5rem;font-size:.8rem}
.qitem{background:var(--card);border:1px solid var(--rule);border-radius:8px;padding:.7rem .9rem;margin:.5rem 0}
.qitem.recycled{border-left:3px solid var(--y)}
.qitem.brain{border-left:3px solid var(--g)}
.qhead{font-size:.95rem}
.qid,.qid a{font-family:"SF Mono",Menlo,monospace;color:var(--accent);text-decoration:none;font-weight:600}
.qid a:hover{text-decoration:underline}
.qlabel{color:var(--muted)}
code{font-family:"SF Mono",Menlo,monospace;font-size:.85em;background:var(--code);color:#eee;padding:.05rem .3rem;border-radius:4px}
.qb{font-size:.75rem;border-radius:10px;padding:.05rem .45rem;margin-right:.3rem}
.qb.un{background:rgba(154,163,178,.14);color:#c3cad6}.qb.re{background:rgba(240,162,2,.16);color:#f6cb72}.qb.br{background:rgba(143,227,136,.14);color:#b8f5b3}
.qmeta{display:flex;flex-wrap:wrap;gap:.25rem;margin:.45rem 0 0}
.qchip{display:inline-flex;gap:.25rem;align-items:center;border:1px solid var(--rule);border-radius:8px;padding:.08rem .4rem;font-size:.76rem;color:var(--muted);background:var(--card2)}
.qchip b{color:var(--ink);font-weight:600}.qchip a{text-decoration:none}.qchip a:hover{text-decoration:underline}
.qnotes{margin:.5rem 0 0;padding-left:1.1rem;font-size:.88rem;color:var(--ink)}
.qnotes .who{color:var(--accent)}
.qfix{margin-top:.35rem;font-size:.85rem;color:var(--g)}
.qreason{margin-top:.35rem;font-size:.84rem;color:var(--muted)}
.empty{color:var(--muted)}
a{color:var(--accent)}
a:hover{color:var(--accent2)}
</style></head><body><main>
<h1>${htmlEscape(spec.label)} tag queue</h1>
<p class="sub">Pending <code>@[${htmlEscape(spec.attr)}]</code> tags for the next Mathlib batch. ${when}</p>
${tools}
${body}
</main></body></html>`;
}

export function registerQueueRoutes(app: Hono<{ Bindings: Env }>): void {
  const routeSpec = (db: string | undefined) => crossRefSpec(db || "wikidata");
  app.get("/api/queue", async (c) => {
    const sp = routeSpec(undefined)!;
    return c.json(await readQueue(c.env, sp));
  });
  app.get("/api/queue/:db", async (c) => {
    const sp = routeSpec(c.req.param("db"));
    if (!sp) return c.json({ ok: false, error: "unknown crossref database" }, 404);
    return c.json(await readQueue(c.env, sp));
  });

  // The bot publishes the assembled queue here (PIPELINE_TOKEN bearer → 'bot').
  const postQueue = async (c: Ctx, sp: CrossRefSpec) => {
    const user = await requireRole(c, ["bot", "admin"]);
    if (!user) return c.json({ ok: false, error: "bot/admin only" }, 403);
    let items: QueueItem[];
    try {
      const body = (await c.req.json()) as { items?: QueueItem[] };
      items = Array.isArray(body.items) ? body.items : [];
    } catch {
      return c.json({ ok: false, error: "bad json (expect {items:[…]})" }, 400);
    }
    const qidField = (v: unknown) => (typeof v === "string" && /^Q\d+$/.test(v) ? v : undefined);
    const idField = (v: unknown) => (typeof v === "string" && sp.idPattern.test(v) ? v : undefined);
    const textField = (v: unknown, n: number) => (typeof v === "string" ? v.slice(0, n) : undefined);
    const numField = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : undefined);
    // keep only the fields we render; validate id-shaped fields
    const clean = items
      .map((i) => {
        const db = textField(i.db, 40) || sp.db;
        if (db !== sp.db) return null;
        const id = idField(i.id) || (sp.db === "wikidata" ? qidField(i.qid) : undefined);
        if (!id) return null;
        return { raw: i, id };
      })
      .filter((x): x is { raw: QueueItem; id: string } => !!x)
      .map((i) => ({
        db: sp.db,
        id: i.id,
        qid: sp.db === "wikidata" ? i.id : undefined,
        label: textField(i.raw.label, 200),
        decl: textField(i.raw.decl, 200) || "",
        file: textField(i.raw.file, 300),
        status: i.raw.status === "recycled" ? "recycled" : i.raw.status === "brain" ? "brain" : "unreviewed",
        orig_id: idField(i.raw.orig_id),
        orig_qid: qidField(i.raw.orig_qid),
        article_qid: qidField(i.raw.article_qid),
        concept_qid: qidField(i.raw.concept_qid),
        centrality_pct: numField(i.raw.centrality_pct),
        brain_rank: numField(i.raw.brain_rank),
        wikilink_rank: numField(i.raw.wikilink_rank),
        rank_delta: numField(i.raw.rank_delta),
        provenance_tier: textField(i.raw.provenance_tier, 80),
        source_file: textField(i.raw.source_file, 120),
        priority_source: textField(i.raw.priority_source, 80),
        review_reason: textField(i.raw.review_reason, 500),
        brain_node: textField(i.raw.brain_node, 120),
        decl_node: textField(i.raw.decl_node, 240),
        source: textField(i.raw.source, 80),
        actor_type: textField(i.raw.actor_type, 40),
        added_by: textField(i.raw.added_by, 80),
        brain_edge_id: textField(i.raw.brain_edge_id, 120),
        confidence: textField(i.raw.confidence, 40),
        notes: Array.isArray(i.raw.notes)
          ? i.raw.notes.slice(0, 10).map((n) => ({
              login: String(n.login || "").slice(0, 60),
              status: String(n.status || "").slice(0, 20),
              text: String(n.text || "").slice(0, 1000),
            }))
          : undefined,
        retarget: textField(i.raw.retarget, 300),
        added: textField(i.raw.added, 80),
      })) as QueueItem[];
    const blob: QueueBlob = { db: sp.db, updated: new Date().toISOString(), items: clean };
    await c.env.RENDER_CACHE.put(sp.queueKey, JSON.stringify(blob)); // no TTL → durable
    return c.json({ ok: true, db: sp.db, count: clean.length });
  };
  app.post("/api/queue", async (c) => postQueue(c, routeSpec(undefined)!));
  app.post("/api/queue/:db", async (c) => {
    const sp = routeSpec(c.req.param("db"));
    if (!sp) return c.json({ ok: false, error: "unknown crossref database" }, 404);
    return postQueue(c, sp);
  });

  app.get("/queue", async (c) => {
    c.header("Cache-Control", "no-cache");
    const sp = routeSpec(undefined)!;
    return c.html(queuePageHtml(await readQueue(c.env, sp), sp));
  });
  app.get("/queue/:db", async (c) => {
    c.header("Cache-Control", "no-cache");
    const sp = routeSpec(c.req.param("db"));
    if (!sp) return c.text("Unknown crossref database.", 404);
    return c.html(queuePageHtml(await readQueue(c.env, sp), sp));
  });
}
