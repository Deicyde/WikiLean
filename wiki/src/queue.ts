// WikiLean @[wikidata] tag QUEUE.
//
// A public read view of the pipeline's pending tags: "unreviewed" LLM-generated
// candidates, plus tags that were recycled after a reject/revise (carrying the
// reviewer notes + the LLM triage's retarget suggestion). The daily bot
// publishes the queue via POST /api/queue (PIPELINE_TOKEN bearer / bot role);
// GET /queue renders it; GET /api/queue returns the JSON.
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
  status: "unreviewed" | "recycled";
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
  return (
    `<article class="qitem ${it.status}">` +
    `<div class="qhead">${STATUS_BADGE[it.status] || ""} ` +
    `<a class="qid" href="https://www.wikidata.org/wiki/${esc(it.qid)}" target="_blank" rel="noopener">${esc(it.qid)}</a>` +
    (it.label ? ` <span class="qlabel">${esc(it.label)}</span>` : "") +
    ` → ${declHtml}</div>` +
    notes +
    retarget +
    `</article>`
  );
}

function queuePageHtml(blob: QueueBlob): string {
  const items = blob.items || [];
  const recycled = items.filter((i) => i.status === "recycled");
  const unreviewed = items.filter((i) => i.status !== "recycled");
  const section = (title: string, list: QueueItem[]) =>
    list.length
      ? `<h2>${title} <span class="ct">${list.length}</span></h2>` + list.map(itemHtml).join("")
      : "";
  const body =
    items.length === 0
      ? `<p class="empty">The queue is empty. The daily bot publishes pending tags here.</p>`
      : section("Recycled — needs a retarget", recycled) + section("Unreviewed candidates", unreviewed);
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
.qhead{font-size:.95rem}
.qid{font-family:"SF Mono",Menlo,monospace;color:var(--accent);text-decoration:none;font-weight:600}
.qid:hover{text-decoration:underline}
.qlabel{color:var(--muted)}
code{font-family:"SF Mono",Menlo,monospace;font-size:.85em;background:#f3efe6;padding:.05rem .3rem;border-radius:4px}
.qb{font-size:.75rem;border-radius:10px;padding:.05rem .45rem;margin-right:.3rem}
.qb.un{background:#eef2f6;color:#54606b}.qb.re{background:#fbf0db;color:#8a5a14}
.qnotes{margin:.5rem 0 0;padding-left:1.1rem;font-size:.88rem;color:#403a30}
.qnotes .who{color:var(--accent)}
.qfix{margin-top:.35rem;font-size:.85rem;color:#1a6b3a}
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
    // keep only the fields we render; validate qid shape
    const clean = items
      .filter((i) => i && typeof i.qid === "string" && /^Q\d+$/.test(i.qid))
      .map((i) => ({
        qid: i.qid,
        label: typeof i.label === "string" ? i.label.slice(0, 200) : undefined,
        decl: typeof i.decl === "string" ? i.decl.slice(0, 200) : "",
        file: typeof i.file === "string" ? i.file.slice(0, 300) : undefined,
        status: i.status === "recycled" ? "recycled" : "unreviewed",
        notes: Array.isArray(i.notes)
          ? i.notes.slice(0, 10).map((n) => ({
              login: String(n.login || "").slice(0, 60),
              status: String(n.status || "").slice(0, 20),
              text: String(n.text || "").slice(0, 1000),
            }))
          : undefined,
        retarget: typeof i.retarget === "string" ? i.retarget.slice(0, 300) : undefined,
        added: typeof i.added === "string" ? i.added : undefined,
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
