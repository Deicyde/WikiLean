// WikiLean @[wikidata] tag QUEUE.
//
// A public read view of the pipeline's pending tags: "unreviewed" LLM-generated
// candidates, Brain-suggested formalizes edges, plus tags that were recycled
// after a reject/revise (carrying the reviewer notes + the LLM triage's retarget
// suggestion). The daily bot publishes the queue via POST /api/queue
// (PIPELINE_TOKEN bearer / bot role); GET /queue renders it; GET /api/queue
// returns the JSON.
//
// Storage: a single JSON blob in KV (no TTL — durable state, not cache).

import type { Context, Hono } from "hono";
import type { Env } from "./env.js";
import { requireRole } from "./auth.js";
import { htmlEscape } from "./engine/html.js";

type Ctx = Context<{ Bindings: Env }>;
const QUEUE_KEY = "wikidata:queue";

export interface QueueItem {
  qid: string;
  label?: string; // Wikidata label
  decl: string; // target Mathlib declaration
  file?: string;
  status: "unreviewed" | "recycled" | "brain";
  orig_qid?: string; // recycled item before retarget
  article_qid?: string; // source article concept when the tag QID was narrowed
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
  updated: string;
  items: QueueItem[];
}

async function readQueue(env: Env): Promise<QueueBlob> {
  const raw = await env.RENDER_CACHE.get(QUEUE_KEY);
  if (!raw) return { updated: "", items: [] };
  try {
    return JSON.parse(raw) as QueueBlob;
  } catch {
    return { updated: "", items: [] };
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

function qidLink(qid: string): string {
  const q = htmlEscape(qid);
  return `<a href="https://www.wikidata.org/wiki/${q}" target="_blank" rel="noopener">${q}</a>`;
}

function chip(label: string, value: string): string {
  return `<span class="qchip"><b>${htmlEscape(label)}</b> ${value}</span>`;
}

function itemHtml(it: QueueItem): string {
  const esc = (s: string) => htmlEscape(s);
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
  if (it.orig_qid && it.orig_qid !== it.qid) meta.push(chip("from", qidLink(it.orig_qid)));
  if (it.article_qid && it.article_qid !== it.qid) meta.push(chip("article", qidLink(it.article_qid)));
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
    `<span class="qid">${qidLink(it.qid)}</span>` +
    (it.label ? ` <span class="qlabel">${esc(it.label)}</span>` : "") +
    ` → ${declHtml}</div>` +
    metaHtml +
    notes +
    retarget +
    reason +
    `</article>`
  );
}

function queuePageHtml(blob: QueueBlob): string {
  const items = blob.items || [];
  const recycled = items.filter((i) => i.status === "recycled");
  const brain = items.filter((i) => i.status === "brain");
  const unreviewed = items.filter((i) => i.status === "unreviewed");
  const section = (title: string, list: QueueItem[]) =>
    list.length
      ? `<h2>${title} <span class="ct">${list.length}</span></h2>` + list.map(itemHtml).join("")
      : "";
  const body =
    items.length === 0
      ? `<p class="empty">The queue is empty. The daily bot publishes pending tags here.</p>`
      : section("Recycled — needs a retarget", recycled) +
        section("Brain-suggested formalizes edges", brain) +
        section("Unreviewed candidates", unreviewed);
  const when = blob.updated ? `updated ${htmlEscape(blob.updated)}` : "";
  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WikiLean · tag queue</title>
<style>
:root{--bg:#faf7f1;--card:#fffdf9;--rule:#e3dccb;--ink:#1f1d1a;--muted:#6b6457;--accent:#7a3d2a}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--ink);margin:0;padding:2rem 1rem;line-height:1.5}
main{max-width:820px;margin:0 auto}
h1{font-size:1.5rem;margin:0 0 .2rem}.sub{color:var(--muted);margin:0 0 1.5rem;font-size:.9rem}
h2{font-size:1rem;margin:1.6rem 0 .6rem;color:var(--muted);text-transform:uppercase;letter-spacing:.03em}
.ct{background:#e8edf7;color:#1a4b8c;border-radius:10px;padding:0 .5rem;font-size:.8rem}
.qitem{background:var(--card);border:1px solid var(--rule);border-radius:10px;padding:.7rem .9rem;margin:.5rem 0}
.qitem.recycled{border-left:3px solid #c98a2b}
.qitem.brain{border-left:3px solid #4f7d64}
.qhead{font-size:.95rem}
.qid,.qid a{font-family:"SF Mono",Menlo,monospace;color:var(--accent);text-decoration:none;font-weight:600}
.qid a:hover{text-decoration:underline}
.qlabel{color:var(--muted)}
code{font-family:"SF Mono",Menlo,monospace;font-size:.85em;background:#f3efe6;padding:.05rem .3rem;border-radius:4px}
.qb{font-size:.75rem;border-radius:10px;padding:.05rem .45rem;margin-right:.3rem}
.qb.un{background:#eef2f6;color:#54606b}.qb.re{background:#fbf0db;color:#8a5a14}.qb.br{background:#e6f2eb;color:#276047}
.qmeta{display:flex;flex-wrap:wrap;gap:.25rem;margin:.45rem 0 0}
.qchip{display:inline-flex;gap:.25rem;align-items:center;border:1px solid var(--rule);border-radius:8px;padding:.08rem .4rem;font-size:.76rem;color:var(--muted);background:#fffaf1}
.qchip b{color:#403a30;font-weight:600}.qchip a{text-decoration:none}.qchip a:hover{text-decoration:underline}
.qnotes{margin:.5rem 0 0;padding-left:1.1rem;font-size:.88rem;color:#403a30}
.qnotes .who{color:var(--accent)}
.qfix{margin-top:.35rem;font-size:.85rem;color:#1a6b3a}
.qreason{margin-top:.35rem;font-size:.84rem;color:var(--muted)}
.empty{color:var(--muted)}
a{color:#1a4b8c}
</style></head><body><main>
<h1>Tag queue</h1>
<p class="sub">Pending <code>@[wikidata]</code> tags for the next Mathlib batch. ${when}</p>
${body}
</main></body></html>`;
}

export function registerQueueRoutes(app: Hono<{ Bindings: Env }>): void {
  app.get("/api/queue", async (c) => c.json(await readQueue(c.env)));

  // The bot publishes the assembled queue here (PIPELINE_TOKEN bearer → 'bot').
  app.post("/api/queue", async (c) => {
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
    const textField = (v: unknown, n: number) => (typeof v === "string" ? v.slice(0, n) : undefined);
    const numField = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : undefined);
    // keep only the fields we render; validate qid-shaped fields
    const clean = items
      .filter((i) => i && typeof i.qid === "string" && /^Q\d+$/.test(i.qid))
      .map((i) => ({
        qid: i.qid,
        label: textField(i.label, 200),
        decl: textField(i.decl, 200) || "",
        file: textField(i.file, 300),
        status: i.status === "recycled" ? "recycled" : i.status === "brain" ? "brain" : "unreviewed",
        orig_qid: qidField(i.orig_qid),
        article_qid: qidField(i.article_qid),
        centrality_pct: numField(i.centrality_pct),
        brain_rank: numField(i.brain_rank),
        wikilink_rank: numField(i.wikilink_rank),
        rank_delta: numField(i.rank_delta),
        provenance_tier: textField(i.provenance_tier, 80),
        source_file: textField(i.source_file, 120),
        priority_source: textField(i.priority_source, 80),
        review_reason: textField(i.review_reason, 500),
        brain_node: qidField(i.brain_node),
        decl_node: textField(i.decl_node, 240),
        source: textField(i.source, 80),
        actor_type: textField(i.actor_type, 40),
        added_by: textField(i.added_by, 80),
        brain_edge_id: textField(i.brain_edge_id, 120),
        confidence: textField(i.confidence, 40),
        notes: Array.isArray(i.notes)
          ? i.notes.slice(0, 10).map((n) => ({
              login: String(n.login || "").slice(0, 60),
              status: String(n.status || "").slice(0, 20),
              text: String(n.text || "").slice(0, 1000),
            }))
          : undefined,
        retarget: textField(i.retarget, 300),
        added: textField(i.added, 80),
      })) as QueueItem[];
    const blob: QueueBlob = { updated: new Date().toISOString(), items: clean };
    await c.env.RENDER_CACHE.put(QUEUE_KEY, JSON.stringify(blob)); // no TTL → durable
    return c.json({ ok: true, count: clean.length });
  });

  app.get("/queue", async (c) => {
    c.header("Cache-Control", "no-cache");
    return c.html(queuePageHtml(await readQueue(c.env)));
  });
}
