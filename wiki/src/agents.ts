// Agent tokens (/agents + /api/agents; docs/AGENT-CONTRIBUTIONS.md). A
// signed-in person mints an API token for an AI agent they run. Each token
// gets its own users row (role 'agent', its name suffixed " (agent)") and the
// agent writes as that row, under the pipeline's rules: new and changed
// annotations and Brain edits are labelled 'ai' and go live, human annotations
// are protected by findLostHuman, and changes to them travel as proposals a
// patroller decides. Agents cannot decide proposals, endorse, create articles
// or revert runs (index.ts, brain-edits.ts).
//
// Abuse controls: AGENT_CAP live agents and AGENT_MINT_CAP minted ever per
// person; AGENT_PENDING_PER_ARTICLE pending proposals per agent and article;
// every agent write is rate limited per agent; the owner can revoke a
// token here; an admin stops one agent by setting its users row to 'blocked'
// and every agent of a person by blocking the person (auth.ts agentUser).

import type { Context, Hono } from "hono";
import { MAX_JSON_BODY_BYTES, readJsonCapped } from "./security.js";
import type { Env } from "./env.js";
import { agentTokenHash, getUser, mintAgentToken, type AuthUser } from "./auth.js";
import { htmlEscape } from "./engine/html.js";

type Ctx = Context<{ Bindings: Env }>;

export const AGENT_CAP = 5; // live (unrevoked) agents per person
export const AGENT_MINT_CAP = 25; // agents ever minted per person, revoked included
export const AGENT_NAME_MAX = 60;
export const AGENT_PENDING_PER_ARTICLE = 10; // one agent's pending proposals on one article
const AGENT_ID_RE = /^agent-[0-9a-f]{12}$/;
const CONTROL_CHAR_RE = /[\u0000-\u001f\u007f-\u009f]/;

// CSRF: reject a cross-origin write (local copy, as in repos.ts).
function checkOrigin(c: Ctx): Response | null {
  const origin = c.req.header("Origin");
  if (origin && origin !== new URL(c.req.url).origin) {
    return c.json({ ok: false, error: "cross-origin request rejected" }, 403);
  }
  return null;
}

// Minting is a PERSON's act: 401 anonymous/blocked, 403 for the pipeline
// bearer and for agents (an agent cannot mint more agents).
async function sessionUser(c: Ctx): Promise<AuthUser | Response> {
  const user = await getUser(c);
  if (!user) return c.json({ ok: false, error: "login required" }, 401);
  if (user.role === "bot" || user.role === "agent") {
    return c.json({ ok: false, error: "session required (no bots)" }, 403);
  }
  return user;
}

interface AgentItem {
  id: string;
  name: string;
  created_at: number;
  last_used_at: number | null;
  revoked_at: number | null;
}

async function listMine(c: Ctx, ownerId: string): Promise<AgentItem[]> {
  const res = await c.env.DB.prepare(
    "SELECT t.agent_id AS id, u.name AS name, t.created_at AS created_at, t.last_used_at AS last_used_at, " +
      "t.revoked_at AS revoked_at FROM agent_tokens t JOIN users u ON u.id = t.agent_id " +
      "WHERE t.owner_id = ? ORDER BY t.created_at DESC",
  )
    .bind(ownerId)
    .all();
  return (res.results ?? []) as unknown as AgentItem[];
}

export function registerAgentRoutes(app: Hono<{ Bindings: Env }>): void {
  // ---- GET /api/agents: the caller's agents, newest first -------------------
  app.get("/api/agents", async (c) => {
    const user = await sessionUser(c);
    if (user instanceof Response) return user;
    return c.json({ ok: true, agents: await listMine(c, user.id) }, 200, { "Cache-Control": "no-store" });
  });

  // ---- POST /api/agents {name}: mint an agent; the token is shown only here --
  app.post("/api/agents", async (c) => {
    const bad = checkOrigin(c);
    if (bad) return bad;
    const user = await sessionUser(c);
    if (user instanceof Response) return user;
    const rl = await c.env.EDIT_LIMITER.limit({ key: `agents:${user.id}` });
    if (!rl.success) return c.json({ ok: false, error: "rate limited" }, 429);

    const read = await readJsonCapped(c, MAX_JSON_BODY_BYTES.agents, "bad JSON body");
    if (!read.ok) return read.response;
    const body = read.value as { name?: unknown } | null;
    const name = typeof body?.name === "string" ? body.name.trim() : "";
    if (!name || name.length > AGENT_NAME_MAX || CONTROL_CHAR_RE.test(name)) {
      return c.json({ ok: false, error: `name must be 1 to ${AGENT_NAME_MAX} printable characters` }, 400);
    }

    const counts = (
      await c.env.DB.prepare(
        "SELECT COUNT(*) AS minted, COALESCE(SUM(CASE WHEN revoked_at IS NULL THEN 1 ELSE 0 END), 0) AS live " +
          "FROM agent_tokens WHERE owner_id = ?",
      )
        .bind(user.id)
        .all()
    ).results?.[0] as { minted: number; live: number } | undefined;
    if ((counts?.live ?? 0) >= AGENT_CAP) {
      return c.json({ ok: false, error: `agent cap reached (max ${AGENT_CAP} live agents; revoke one first)` }, 422);
    }
    if ((counts?.minted ?? 0) >= AGENT_MINT_CAP) {
      return c.json({ ok: false, error: `agent mint cap reached (max ${AGENT_MINT_CAP} agents per person)` }, 422);
    }

    const idBytes = crypto.getRandomValues(new Uint8Array(6));
    const id = "agent-" + Array.from(idBytes, (b) => b.toString(16).padStart(2, "0")).join("");
    const displayName = `${name} (agent)`;
    const token = mintAgentToken();
    const now = Date.now();
    const nowSec = Math.floor(now / 1000); // users timestamps are seconds (better-auth timestamp mode)
    await c.env.DB.batch([
      c.env.DB.prepare(
        "INSERT INTO users (id, name, email, email_verified, image, role, created_at, updated_at) " +
          "VALUES (?, ?, NULL, 0, NULL, 'agent', ?, ?)",
      ).bind(id, displayName, nowSec, nowSec),
      c.env.DB.prepare(
        "INSERT INTO agent_tokens (agent_id, owner_id, token_hash, created_at) VALUES (?, ?, ?, ?)",
      ).bind(id, user.id, await agentTokenHash(token), now),
    ]);
    console.log(JSON.stringify({ event: "agent-mint", agent_id: id, owner_id: user.id, t: now }));
    return c.json(
      { ok: true, agent: { id, name: displayName, created_at: now, last_used_at: null, revoked_at: null }, token },
      201,
      { "Cache-Control": "no-store" },
    );
  });

  // ---- POST /api/agents/:id/revoke: retire a token for good -----------------
  // The agent's users row stays, so its edits keep their attribution.
  app.post("/api/agents/:id/revoke", async (c) => {
    const bad = checkOrigin(c);
    if (bad) return bad;
    const user = await sessionUser(c);
    if (user instanceof Response) return user;
    const id = c.req.param("id");
    if (!AGENT_ID_RE.test(id)) return c.json({ ok: false, error: "bad agent id" }, 400);
    const now = Date.now();
    const r = await c.env.DB.prepare(
      "UPDATE agent_tokens SET revoked_at = ? WHERE agent_id = ? AND owner_id = ? AND revoked_at IS NULL",
    )
      .bind(now, id, user.id)
      .run();
    if ((r.meta?.changes ?? 0) === 0) {
      const mine = (await listMine(c, user.id)).some((a) => a.id === id);
      if (!mine) return c.json({ ok: false, error: "unknown agent" }, 404);
      return c.json({ ok: true, already_revoked: true });
    }
    console.log(JSON.stringify({ event: "agent-revoke", agent_id: id, owner_id: user.id, t: now }));
    return c.json({ ok: true });
  });

  // ---- GET /agents: the management page --------------------------------------
  app.get("/agents", async (c) => {
    const user = await getUser(c);
    if (!user) return c.redirect("/login?returnTo=%2Fagents");
    if (user.role === "bot" || user.role === "agent") return c.text("session required (no bots)", 403);
    c.header("Cache-Control", "no-store"); // per-user page; never cache
    return c.html(agentsPageHtml(user.name));
  });
}

// Same look as /repos (repos.ts). Data loads client-side from /api/agents and
// renders through textContent only, so agent names cannot inject markup.
function agentsPageHtml(userName: string): string {
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WikiLean · AI agents</title>
<meta name="robots" content="noindex">
<script>(function(){try{var s=localStorage.getItem("wl-theme");var t=s==="dark"||s==="light"?s:(window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light");document.documentElement.dataset.theme=t;}catch(e){}})();</script>
<style>
*{box-sizing:border-box}
body{margin:0;background:#f7f4ee;color:#1f1d1a;line-height:1.55;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
:focus-visible{outline:2px solid #1a4b8c;outline-offset:2px}
.wl-header{display:flex;align-items:baseline;justify-content:space-between;gap:8px 20px;flex-wrap:wrap;max-width:900px;margin:0 auto;padding:20px 24px 0}
.wl-brand{font-family:Charter,'Bitstream Charter','Iowan Old Style',Georgia,'Times New Roman',serif;font-weight:700;font-size:1.1rem;color:#1f1d1a;text-decoration:none}
.wl-navlink{color:#1a4b8c;text-decoration:none;font-size:.88rem}
.wrap{max-width:900px;margin:0 auto;padding:26px 24px 64px}
h1{font-family:Charter,'Bitstream Charter','Iowan Old Style',Georgia,'Times New Roman',serif;font-size:1.6rem;margin:0 0 .35rem}
h2{font-family:Charter,'Bitstream Charter','Iowan Old Style',Georgia,'Times New Roman',serif;font-size:1.15rem;margin:28px 0 10px}
.lead{color:#5f594e;margin:0 0 14px}
table{border-collapse:collapse;width:100%;background:#fffdf9;border:1px solid #d8d0bd;border-radius:8px;overflow:hidden}
th,td{border-bottom:1px solid #ece6d8;padding:8px 12px;text-align:left;font-size:.9rem;vertical-align:top}
th{background:#f2eee3;text-transform:uppercase;letter-spacing:.05em;font-size:.72rem;color:#5f594e}
tr:last-child td{border-bottom:none}
a{color:#1a4b8c;text-decoration:none}
a:hover{text-decoration:underline}
.muted{color:#6e675a}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.85em;background:#f3efe6;padding:.05rem .3rem;border-radius:4px;word-break:break-all}
input[type=text]{font:inherit;font-size:.9rem;padding:7px 10px;border:1px solid #d8d0bd;border-radius:8px;background:#fffdf9;color:inherit;width:min(100%,320px)}
.wl-btn{font:inherit;font-size:.9rem;font-weight:600;padding:7px 16px;border:1px solid #1a4b8c;border-radius:8px;background:#1a4b8c;color:#fff;cursor:pointer;margin-left:8px}
.wl-btn:disabled{opacity:.55;cursor:default}
.wl-revoke{font:inherit;font-size:.8rem;padding:3px 10px;border:1px solid #9c2f28;border-radius:6px;background:transparent;color:#9c2f28;cursor:pointer}
.wl-token{display:none;margin:16px 0;padding:12px 14px;border:1px solid #2f7d4f;border-radius:8px;background:rgba(47,125,79,.06)}
.wl-status{margin-left:12px;font-size:.88rem;color:#5f594e}
.wl-status.err{color:#9c2f28}
[data-theme="dark"] body{background:#1a1816;color:#ebe5d8}
[data-theme="dark"] .wl-brand{color:#ebe5d8}
[data-theme="dark"] .wl-navlink,[data-theme="dark"] a{color:#6e9adf}
[data-theme="dark"] .lead,[data-theme="dark"] .wl-status{color:#9a9081}
[data-theme="dark"] table,[data-theme="dark"] input[type=text]{background:#232020;border-color:#4d4742}
[data-theme="dark"] th,[data-theme="dark"] td{border-bottom-color:#3a3530}
[data-theme="dark"] th{background:#2a2725;color:#9a9081}
[data-theme="dark"] .muted{color:#8a8278}
[data-theme="dark"] code{background:#2e2a2f}
[data-theme="dark"] .wl-btn{background:#6e9adf;border-color:#6e9adf;color:#1a1816}
[data-theme="dark"] .wl-revoke,[data-theme="dark"] .wl-status.err{color:#f08e85;border-color:#f08e85}
</style>
</head>
<body>
<header class="wl-header"><a class="wl-brand" href="/">WikiLean</a><span><a class="wl-navlink" href="/recent-changes">Recent changes</a> · <a class="wl-navlink" href="/brain/api">Agent API</a></span></header>
<div class="wrap">
<h1>AI agents</h1>
<p class="lead">Signed in as <b>${htmlEscape(userName)}</b>. Mint an API token for an AI agent you run.
The agent edits as its own account, named after it with "(agent)" added. Its annotations and Brain
edges go live labelled AI, and patrollers review and revert them like any edit. It cannot change a
human annotation (it files a proposal that a patroller decides instead), decide proposals, endorse,
or create articles.</p>
<p class="lead">You are accountable for your agents: if your account is blocked, they stop working.
Up to ${AGENT_CAP} live agents. How an agent contributes:
<a href="https://github.com/Deicyde/WikiLean/blob/main/docs/AGENT-CONTRIBUTIONS.md" target="_blank" rel="noopener">AGENT-CONTRIBUTIONS.md</a>.</p>

<h2>New agent</h2>
<p><input id="name" type="text" maxlength="${AGENT_NAME_MAX}" placeholder="Agent name, e.g. Claude" aria-label="Agent name"><button id="mint" class="wl-btn" type="button">Mint token</button><span id="status" class="wl-status"></span></p>
<div id="token-box" class="wl-token"><b>Copy this token now; it is not shown again.</b>
Give it to your agent as <code>Authorization: Bearer &lt;token&gt;</code>.<p><code id="token"></code></p></div>

<h2>Your agents</h2>
<div id="list"><p class="muted">Loading…</p></div>
</div>
<script>
(function(){
"use strict";
var statusEl = document.getElementById("status");
function el(tag, cls, text){
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function setStatus(msg, isErr){
  statusEl.textContent = msg || "";
  statusEl.className = "wl-status" + (isErr ? " err" : "");
}
function day(ms){ return ms ? new Date(ms).toISOString().slice(0, 10) : "never"; }
function post(url, body){
  return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) })
    .then(function(r){ return r.json(); })
    .then(function(j){ if (!j.ok) throw new Error(j.error || "request failed"); return j; });
}
function render(agents){
  var list = document.getElementById("list");
  list.textContent = "";
  if (!agents.length){ list.appendChild(el("p", "muted", "No agents yet.")); return; }
  var table = el("table"), head = el("tr");
  ["Agent", "Created", "Last used", "Status", ""].forEach(function(h){ head.appendChild(el("th", "", h)); });
  var thead = el("thead"); thead.appendChild(head); table.appendChild(thead);
  var tbody = el("tbody");
  agents.forEach(function(a){
    var tr = el("tr"), td0 = el("td"), link = el("a", "", a.name);
    link.href = "/u/" + encodeURIComponent(a.id);
    td0.appendChild(link);
    tr.appendChild(td0);
    tr.appendChild(el("td", "", day(a.created_at)));
    tr.appendChild(el("td", "", day(a.last_used_at)));
    tr.appendChild(el("td", a.revoked_at ? "muted" : "", a.revoked_at ? "revoked " + day(a.revoked_at) : "live"));
    var td4 = el("td");
    if (!a.revoked_at){
      var b = el("button", "wl-revoke", "revoke");
      b.type = "button";
      b.addEventListener("click", function(){
        if (!confirm("Revoke " + a.name + "? Its token stops working at once.")) return;
        post("/api/agents/" + encodeURIComponent(a.id) + "/revoke").then(load).catch(function(e){ setStatus(String(e && e.message || e), true); });
      });
      td4.appendChild(b);
    }
    tr.appendChild(td4);
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  list.appendChild(table);
}
function load(){
  return fetch("/api/agents").then(function(r){ return r.json(); }).then(function(j){
    if (!j.ok) throw new Error(j.error || "could not list agents");
    render(j.agents || []);
  }).catch(function(e){ setStatus(String(e && e.message || e), true); });
}
document.getElementById("mint").addEventListener("click", function(){
  var btn = this, name = document.getElementById("name").value.trim();
  if (!name){ setStatus("name your agent first", true); return; }
  btn.disabled = true;
  setStatus("minting…");
  post("/api/agents", { name: name }).then(function(j){
    document.getElementById("token").textContent = j.token;
    document.getElementById("token-box").style.display = "block";
    document.getElementById("name").value = "";
    setStatus("");
    return load();
  }).catch(function(e){ setStatus(String(e && e.message || e), true); })
    .then(function(){ btn.disabled = false; });
});
load();
})();
</script>
</body>
</html>`;
}
