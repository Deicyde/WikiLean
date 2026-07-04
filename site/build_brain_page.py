#!/usr/bin/env python3
"""Generate /brain — the locality-scoped explorer over the BRAIN dataset.

One page, zero baked-in data: everything is fetched on demand from the prefix
shards in /assets/brain/ (manifest.json → one shard fetch per node), so the
client never loads the whole graph — brain/SCHEMA.md's locality law as UX.

  · Explore  — Miller-column drill-down through the containment tree
               (library → module → … → file → decl), concepts floating beside
               the container they anchor to.
  · Panel    — the selected node: breadcrumb, altitude evidence, slogan, and
               every ontology edge grouped by kind, each with its provenance
               and evidence one tap away (the anti-slop drawer).
  · Layers   — per-source-kind toggles (formal deps / cross-refs / literature /
               wikidata relations / mentions) overlay or hide edge families.
  · Search   — label search over concepts + containers (labels.json, lazy);
               decls resolve through the existing /decl route.

Run: python3 site/build_brain_page.py   (writes site/out/brain.html; build-public
copies it + site/assets/brain/ into the Worker's static assets)
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "out"

HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WikiLean — The Brain</title>
<meta name="description" content="Explore the BRAIN: a locality-scoped concept and dependency graph of mathematics joining Wikipedia/Wikidata concepts, Lean formalizations across 39 libraries, cross-database identities (LMFDB, nLab, MathWorld, …) and arXiv literature — with machine-checkable provenance on every edge.">
<script>(function(){try{var s=localStorage.getItem("wl-theme");var t=s==="dark"||s==="light"?s:(window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light");document.documentElement.dataset.theme=t;}catch(e){}})();</script>
<style>
* { box-sizing:border-box; }
html, body { height:100%; }
body { margin:0; background:#fafbfc; color:#1f2328;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
a { color:#0969da; text-decoration:none; }
a:hover { text-decoration:underline; }
.wl-header { background:#fff; border-bottom:1px solid #d0d7de; padding:10px 20px;
  display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; }
.wl-brand { font-weight:700; color:#0969da; font-size:18px; }
.wl-nav { display:flex; gap:14px; align-items:center; flex-wrap:wrap; }
.wl-navlink { font-size:.9rem; }
.toolbar { background:#fff; border-bottom:1px solid #d0d7de; padding:8px 20px;
  display:flex; gap:14px; align-items:center; flex-wrap:wrap; font-size:.85rem; }
.toolbar label { display:inline-flex; align-items:center; gap:4px; cursor:pointer;
  color:#57606a; user-select:none; }
.toolbar .grp { display:inline-flex; gap:10px; align-items:center; padding-right:14px;
  border-right:1px solid #d8dee4; }
.toolbar .grp:last-child { border-right:none; }
#search { position:relative; }
#search input { width:290px; padding:5px 9px; border:1px solid #d0d7de; border-radius:6px;
  font-size:.88rem; background:#f6f8fa; }
#search input:focus { outline:2px solid #0969da33; background:#fff; }
#hits { position:absolute; top:32px; left:0; z-index:30; width:420px; max-height:380px;
  overflow:auto; background:#fff; border:1px solid #d0d7de; border-radius:8px;
  box-shadow:0 8px 24px rgba(31,35,40,.15); display:none; }
#hits .hit { padding:6px 10px; cursor:pointer; display:flex; gap:8px; align-items:baseline; }
#hits .hit:hover { background:#f6f8fa; }
#hits .hit .t { font-size:.72rem; color:#57606a; min-width:64px; }
.main { display:flex; height:calc(100vh - 96px); }
#columns { flex:1 1 58%; display:flex; overflow-x:auto; border-right:1px solid #d0d7de;
  background:#fff; }
.col { min-width:230px; max-width:290px; border-right:1px solid #eaeef2; overflow-y:auto;
  padding:4px 0; flex:0 0 auto; }
.col h4 { margin:6px 10px 4px; font-size:.72rem; color:#57606a; text-transform:uppercase;
  letter-spacing:.03em; font-weight:600; }
.item { padding:5px 10px; cursor:pointer; display:flex; align-items:center; gap:7px;
  font-size:.86rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.item:hover { background:#f6f8fa; }
.item.sel { background:#ddf4ff; }
.item .n { color:#8c959f; font-size:.72rem; margin-left:auto; flex:0 0 auto; }
.item .chev { color:#8c959f; flex:0 0 auto; font-size:.7rem; }
.dot { width:8px; height:8px; border-radius:50%; flex:0 0 auto; }
.dot.container { background:#8250df; border-radius:2px; }
.dot.decl { background:#1a7f37; }
.dot.concept { background:#0969da; }
.dot.concept.partial { background:#d4a72c; }
.dot.concept.not_formalized { background:#cf222e; }
.dot.literature { background:#bf5af2; }
#panel { flex:1 1 42%; overflow-y:auto; padding:18px 22px; background:#fafbfc; }
#panel h2 { margin:0 0 2px; font-size:1.25rem; }
#panel .sub { color:#57606a; font-size:.85rem; margin-bottom:10px; }
.crumb { font-size:.8rem; color:#57606a; margin-bottom:8px; }
.crumb a { cursor:pointer; }
.badge { display:inline-block; padding:1px 8px; border-radius:10px; font-size:.72rem;
  border:1px solid #d0d7de; color:#57606a; margin:0 4px 4px 0; background:#fff; }
.badge.f { border-color:#1a7f37; color:#1a7f37; }
.badge.p { border-color:#d4a72c; color:#9a6700; }
.badge.n { border-color:#cf222e; color:#cf222e; }
.chips { margin:8px 0; }
.chip { display:inline-block; margin:0 6px 6px 0; padding:2px 9px; border:1px solid #d0d7de;
  border-radius:12px; font-size:.76rem; background:#fff; }
section.kind { margin-top:14px; }
section.kind h3 { font-size:.85rem; margin:0 0 6px; color:#1f2328; }
section.kind h3 .cnt { color:#8c959f; font-weight:400; }
.edge { border:1px solid #eaeef2; border-radius:6px; margin-bottom:6px; background:#fff; }
.edge .row { padding:6px 10px; display:flex; gap:8px; align-items:baseline; cursor:pointer;
  font-size:.85rem; flex-wrap:wrap; }
.edge .row:hover { background:#f6f8fa; }
.edge .mk { color:#8250df; font-size:.74rem; }
.edge .conf { font-size:.7rem; border-radius:8px; padding:0 6px; border:1px solid #d0d7de;
  color:#57606a; margin-left:auto; }
.edge .conf.high { border-color:#1a7f37; color:#1a7f37; }
.edge .conf.low { border-color:#cf222e; color:#cf222e; }
.edge .drawer { display:none; border-top:1px solid #eaeef2; padding:8px 10px; font-size:.76rem;
  background:#f6f8fa; border-radius:0 0 6px 6px; }
.edge .drawer pre { margin:4px 0 0; white-space:pre-wrap; word-break:break-word;
  font-size:.72rem; color:#1f2328; }
.edge.open .drawer { display:block; }
.slogan { border-left:3px solid #8250df; padding:6px 10px; background:#fff; margin:8px 0;
  font-size:.86rem; border-radius:0 6px 6px 0; }
.slogan .src { display:block; color:#8c959f; font-size:.7rem; margin-top:3px; }
.note { color:#57606a; font-size:.8rem; }
.more { font-size:.78rem; color:#57606a; padding:4px 10px; }
.extlink { font-size:.8rem; }
@media (max-width: 900px) { .main { flex-direction:column; height:auto; }
  #columns { max-height:45vh; border-right:none; border-bottom:1px solid #d0d7de; }
  #panel { max-height:none; } }
html[data-theme="dark"] body { background:#0d1117; color:#e6edf3; }
html[data-theme="dark"] .wl-header, html[data-theme="dark"] .toolbar,
html[data-theme="dark"] #columns { background:#161b22; border-color:#30363d; }
html[data-theme="dark"] .col { border-color:#21262d; }
html[data-theme="dark"] .item:hover { background:#21262d; }
html[data-theme="dark"] .item.sel { background:#0c2d6b; }
html[data-theme="dark"] #panel { background:#0d1117; }
html[data-theme="dark"] .edge, html[data-theme="dark"] .badge, html[data-theme="dark"] .chip,
html[data-theme="dark"] .slogan { background:#161b22; border-color:#30363d; }
html[data-theme="dark"] .edge .row:hover { background:#21262d; }
html[data-theme="dark"] .edge .drawer { background:#0d1117; border-color:#30363d; }
html[data-theme="dark"] #search input { background:#0d1117; border-color:#30363d; color:#e6edf3; }
html[data-theme="dark"] #hits { background:#161b22; border-color:#30363d; }
html[data-theme="dark"] #hits .hit:hover { background:#21262d; }
</style>
</head>
<body>
<header class="wl-header">
  <span><a class="wl-brand" href="/">WikiLean</a> <span style="color:#57606a">/ brain</span></span>
  <nav class="wl-nav">
    <div id="search">
      <input id="q" type="search" placeholder="Search concepts &amp; areas… (e.g. abelian group)" autocomplete="off">
      <div id="hits"></div>
    </div>
    <a class="wl-navlink" href="/map">Map</a>
    <a class="wl-navlink" href="/stats">Stats</a>
    <a class="wl-navlink" href="https://github.com/Deicyde/WikiLean" rel="noopener">GitHub</a>
  </nav>
</header>
<div class="toolbar">
  <span class="grp"><b>Layers</b>
    <label><input type="checkbox" data-k="formalizes" checked> formalizations</label>
    <label><input type="checkbox" data-k="depends" checked> formal deps</label>
    <label><input type="checkbox" data-k="xref" checked> cross-refs</label>
    <label><input type="checkbox" data-k="cites,matches" checked> literature</label>
    <label><input type="checkbox" data-k="relates"> wikidata relations</label>
    <label><input type="checkbox" data-k="mentions"> article mentions</label>
  </span>
  <span class="grp"><b>Libraries</b>
    <label><input type="checkbox" data-lk="math" checked> math</label>
    <label><input type="checkbox" data-lk="cs" checked> CS</label>
    <label><input type="checkbox" data-lk="physics" checked> physics</label>
    <label><input type="checkbox" data-lk="tooling"> tooling</label>
  </span>
  <span class="grp"><b>Sort</b>
    <label><select id="sort">
      <option value="size">by size</option>
      <option value="name">by name</option>
    </select></label>
  </span>
  <span class="note" id="status">loading manifest…</span>
</div>
<div class="main">
  <div id="columns"></div>
  <div id="panel"><p class="note">Pick a library on the left, drill into its modules, or search.
    Every edge you see carries its provenance — click any row to open the evidence drawer.</p></div>
</div>
<script>
"use strict";
const BASE = "/assets/brain/";
let manifest = null, labels = null;
const shardCache = new Map(), entryCache = new Map();

function shardKey(id, len) {
  let k = "";
  for (let i = 0; i < len; i++) {
    if (i < id.length) { const c = id[i].toLowerCase();
      k += /[a-z0-9]/.test(c) ? c : "_"; } else k += "_";
  }
  return k;
}
function shardFor(id) {
  const lo = manifest.scheme.min_len, hi = manifest.scheme.max_len;
  for (let l = Math.min(hi, Math.max(id.length, lo)); l >= lo; l--) {
    const k = shardKey(id, l); if (manifest.shards[k] !== undefined) return k;
  }
  for (let l = Math.max(id.length, lo) + 1; l <= hi; l++) {
    const k = shardKey(id, l); if (manifest.shards[k] !== undefined) return k;
  }
  return null;
}
async function getEntry(id) {
  if (entryCache.has(id)) return entryCache.get(id);
  const key = shardFor(id);
  if (key === null) return null;
  if (!shardCache.has(key)) {
    shardCache.set(key, fetch(BASE + key + ".json").then(r => r.ok ? r.json() : {}));
  }
  const shard = await shardCache.get(key);
  const e = shard[id] || null;
  entryCache.set(id, e);
  return e;
}

const $ = s => document.querySelector(s);
const columnsEl = $("#columns"), panelEl = $("#panel"), statusEl = $("#status");
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

function activeKinds() {
  const ks = new Set();
  document.querySelectorAll(".toolbar input[data-k]").forEach(cb => {
    if (cb.checked) cb.dataset.k.split(",").forEach(k => ks.add(k));
  });
  return ks;
}
function activeLibKinds() {
  const ks = new Set();
  document.querySelectorAll(".toolbar input[data-lk]").forEach(cb => {
    if (cb.checked) ks.add(cb.dataset.lk);
  });
  return ks;
}

// ---- columns (Miller drill-down) -------------------------------------------
let colPath = [];   // selected ids, one per column depth

function statusClass(n) {
  const st = n.status || (n.display && n.display.status) || "";
  return st === "formalized" ? "" : st ? " " + st : "";
}
function itemHtml(n, hasKids) {
  const dot = `<span class="dot ${n.type}${n.type === "concept" ? statusClass(n) : ""}"></span>`;
  const count = n.n_decls ? `<span class="n">${n.n_decls.toLocaleString()}</span>` : "";
  const chev = hasKids ? `<span class="chev">›</span>` : "";
  return `${dot}<span style="overflow:hidden;text-overflow:ellipsis">${esc(n.label || n.id)}</span>${count}${chev}`;
}
function sortRows(rows) {
  const mode = $("#sort").value;
  const arr = rows.slice();
  if (mode === "name") arr.sort((a, b) => (a.label || a.id).localeCompare(b.label || b.id));
  else arr.sort((a, b) => (b.n_decls || 0) - (a.n_decls || 0) ||
                          (a.label || a.id).localeCompare(b.label || b.id));
  return arr;
}
function rootRows() {
  const kinds = activeLibKinds();
  return manifest.roots.filter(r => kinds.has(r.library_kind || "math"))
    .map(r => ({...r, type: "container"}));
}
async function childRows(id) {
  const e = await getEntry(id);
  if (!e) return {rows: [], total: 0};
  const kids = (e.children && e.children.first || []).map(c => ({...c}));
  // concepts anchored to this container (inbound formalizes), shown beside kids
  const anchored = (e.edges && e.edges.in || [])
    .filter(x => x.kind === "formalizes")
    .map(x => ({id: x.id, label: x.id, type: "concept",
                mk: x.evidence && x.evidence.match_kind}));
  return {rows: kids, anchored, total: e.children ? e.children.count : 0};
}
function renderColumn(title, rows, anchored, total, depth) {
  const col = document.createElement("div");
  col.className = "col";
  col.dataset.depth = depth;
  let html = `<h4>${esc(title)}</h4>`;
  for (const n of sortRows(rows)) {
    const sel = colPath[depth] === n.id ? " sel" : "";
    const hasKids = n.type === "container";
    html += `<div class="item${sel}" data-id="${esc(n.id)}" data-kids="${hasKids ? 1 : 0}">
      ${itemHtml(n, hasKids)}</div>`;
  }
  if (total > rows.length)
    html += `<div class="more">+ ${total - rows.length} more — open the panel list</div>`;
  if (anchored && anchored.length) {
    html += `<h4>concepts here</h4>`;
    for (const c of anchored.slice(0, 30)) {
      const sel = colPath[depth] === c.id ? " sel" : "";
      html += `<div class="item${sel}" data-id="${esc(c.id)}" data-kids="0" data-concept="1">
        <span class="dot concept"></span><span class="cl" data-cl="${esc(c.id)}">${esc(c.label)}</span>
        ${c.mk ? `<span class="n">${esc(c.mk)}</span>` : ""}</div>`;
    }
  }
  col.innerHTML = html;
  col.addEventListener("click", ev => {
    const it = ev.target.closest(".item");
    if (it) navigate(it.dataset.id, depth);
  });
  return col;
}
// Concept labels aren't in children summaries; resolve them lazily.
async function fillConceptLabels(col) {
  for (const el of col.querySelectorAll("[data-cl]")) {
    const e = await getEntry(el.dataset.cl);
    if (e && e.node.label) el.textContent = e.node.label;
  }
}
async function renderColumns() {
  columnsEl.innerHTML = "";
  columnsEl.appendChild(renderColumn("libraries", rootRows(), null, 0, 0));
  for (let d = 0; d < colPath.length; d++) {
    const id = colPath[d];
    const e = await getEntry(id);
    if (!e || e.node.type !== "container") break;
    const {rows, anchored, total} = await childRows(id);
    if (!rows.length && !(anchored || []).length) break;
    const col = renderColumn(e.node.label || id, rows, anchored, total, d + 1);
    columnsEl.appendChild(col);
    fillConceptLabels(col);
  }
  // reflect selection in the root column too
  columnsEl.querySelectorAll(".col").forEach((col, d) => {
    col.querySelectorAll(".item").forEach(it => {
      it.classList.toggle("sel", colPath[d] === it.dataset.id);
    });
  });
  columnsEl.scrollLeft = columnsEl.scrollWidth;
}

// ---- panel ------------------------------------------------------------------
const KIND_LABEL = {
  formalizes: "Formalizations", mentions: "Article mentions", depends: "Formal dependencies",
  matches: "Formal ↔ literature matches", xref: "Cross-database identity",
  relates: "Wikidata relations", cites: "Literature", contains: "Contains",
};
const XREF_URL = {
  mathworld: v => `https://mathworld.wolfram.com/${v}.html`,
  nlab: v => `https://ncatlab.org/nlab/show/${encodeURIComponent(v)}`,
  proofwiki: v => `https://proofwiki.org/wiki/${encodeURIComponent(v)}`,
  eom: v => `https://encyclopediaofmath.org/wiki/${encodeURIComponent(v)}`,
  planetmath: v => `https://planetmath.org/${encodeURIComponent(v)}`,
  metamath: v => `https://us.metamath.org/mpeuni/${encodeURIComponent(v)}.html`,
  lmfdb_knowl: v => `https://www.lmfdb.org/knowledge/show/${encodeURIComponent(v)}`,
  oeis: v => `https://oeis.org/${encodeURIComponent(v)}`,
  dlmf: v => `https://dlmf.nist.gov/${encodeURIComponent(v)}`,
  msc: () => null,
};
function nodeUrl(id) {
  if (id.startsWith("decl:")) return "/decl/" + encodeURIComponent(id.split(":", 3)[2]);
  if (id.startsWith("lit:")) {
    const ax = id.slice(4).split("#")[0];
    return /^[A-Za-z-]+\//.test(ax) ? `https://github.com/${ax.split("#")[0]}`
                                    : `https://arxiv.org/abs/${ax}`;
  }
  return null;
}
function edgeHtml(x, provTable, dir) {
  const ev = x.evidence || {};
  const mk = ev.match_kind ? `<span class="mk">${esc(ev.match_kind)}</span>` : "";
  const arrow = dir === "in" ? "←" : "→";
  let target = esc(x.id);
  if (x.kind === "xref" && ev.value !== undefined) {
    const mkUrl = XREF_URL[x.id.split(":")[1]];
    const url = mkUrl && mkUrl(ev.value);
    const lbl = `${esc(x.id.split(":")[1])}: ${esc(ev.value)}`;
    target = url ? `<a href="${esc(url)}" rel="noopener" target="_blank">${lbl}</a>` : lbl;
  } else {
    const u = nodeUrl(x.id);
    target = `<span class="nav" data-nav="${esc(x.id)}" style="color:#0969da;cursor:pointer">${target}</span>`
           + (u ? ` <a class="extlink" href="${esc(u)}" rel="noopener" target="_blank">↗</a>` : "");
  }
  const prov = provTable[x.prov] || {};
  const drawer = `provenance: <b>${esc(prov.source)}</b> · ${esc(prov.method)} · pin ${esc(prov.pin)}
<pre>${esc(JSON.stringify(ev, null, 1))}</pre>`;
  return `<div class="edge"><div class="row">${arrow} ${target} ${mk}
    <span class="conf ${esc(x.confidence)}">${esc(x.confidence)}${ev.skeptic === "pending" ? " · unreviewed" : ""}</span></div>
    <div class="drawer">${drawer}</div></div>`;
}
async function renderPanel(id) {
  const e = await getEntry(id);
  if (!e) { panelEl.innerHTML = `<p class="note">Unknown node: ${esc(id)}</p>`; return; }
  const n = e.node, prov = e._provTable || manifest.prov;
  let html = "";
  if (e.breadcrumb) {
    html += `<div class="crumb">` + e.breadcrumb.map((b, i) =>
      i === e.breadcrumb.length - 1 ? esc(b.label)
        : `<a data-nav="${esc(b.id)}">${esc(b.label)}</a>`).join(" / ") + `</div>`;
  }
  html += `<h2>${esc(n.label || n.id)}</h2>`;
  const sub = [];
  if (n.type) sub.push(n.type);
  if (n.library_kind) sub.push(n.library_kind + " library");
  if (n.module) sub.push(esc(n.module));
  if (n.slug) sub.push(`<a href="/${esc(n.slug)}">WikiLean article</a>`);
  if (n.type === "concept") sub.push(`<a href="https://www.wikidata.org/wiki/${esc(n.id)}" rel="noopener" target="_blank">${esc(n.id)}</a>`);
  if (n.type === "decl") sub.push(`<a href="${esc(nodeUrl(n.id))}" rel="noopener" target="_blank">docs ↗</a>`);
  html += `<div class="sub">${sub.join(" · ")}</div>`;
  if (n.description) html += `<p style="font-size:.9rem">${esc(n.description)}</p>`;
  const st = n.display && n.display.status;
  if (st) html += `<span class="badge ${st === "formalized" ? "f" : st === "partial" ? "p" : "n"}">${esc(st.replace("_", " "))}</span>`;
  const ae = n.altitude_evidence;
  if (ae) {
    if ((ae.p31 || []).includes("Q1936384")) html += `<span class="badge">field of study</span>`;
    (ae.match_kinds || []).forEach(k => { html += `<span class="badge">${esc(k)}</span>`; });
    (ae.module_span || []).forEach(m => { html += `<span class="badge">${esc(m)}</span>`; });
  }
  if (n.n_decls) html += `<span class="badge">${n.n_decls.toLocaleString()} decls</span>`;
  if (n.superseded) html += `<span class="badge n">superseded snapshot module</span>`;
  if (n.slogan) html += `<div class="slogan">${esc(n.slogan)}<span class="src">slogan — TheoremGraph (CC-BY-SA-4.0)</span></div>`;
  if (n.arxiv_id) html += `<p class="note">appears as <b>${esc(n.ref || "?")}</b> of
    <a href="${esc(nodeUrl(n.id))}" rel="noopener" target="_blank">${esc(n.arxiv_id)}</a>
    ${n.license_open ? "" : " (text not redistributable — link only)"}</p>`;

  if (e.children && e.children.count) {
    html += `<section class="kind"><h3>Children <span class="cnt">(${e.children.count})</span></h3><div class="chips">`;
    for (const c of e.children.first)
      html += `<span class="chip"><a data-nav="${esc(c.id)}">${esc(c.label || c.id)}</a>${c.n_decls ? ` <small>${c.n_decls.toLocaleString()}</small>` : ""}</span>`;
    if (e.children.count > e.children.first.length)
      html += `<span class="chip">… +${e.children.count - e.children.first.length} more</span>`;
    html += `</div></section>`;
  }

  const kinds = activeKinds();
  const groups = new Map();
  for (const dir of ["out", "in"]) {
    for (const x of (e.edges && e.edges[dir]) || []) {
      if (!kinds.has(x.kind)) continue;
      const key = x.kind + ":" + dir;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push([x, dir]);
    }
  }
  const order = ["formalizes:out", "formalizes:in", "xref:out", "cites:out", "matches:out",
                 "matches:in", "depends:out", "depends:in", "relates:out", "relates:in",
                 "mentions:out", "mentions:in", "cites:in"];
  for (const key of [...order, ...[...groups.keys()].filter(k => !order.includes(k))]) {
    const rows = groups.get(key);
    if (!rows) continue;
    const [kind, dir] = key.split(":");
    const label = KIND_LABEL[kind] + (dir === "in" ?
      (kind === "formalizes" ? " (concepts this formalizes)" : " (incoming)") : "");
    html += `<section class="kind"><h3>${esc(label)} <span class="cnt">(${rows.length}${
      e.edges.truncated && e.edges.truncated[dir] ? "+, truncated" : ""})</span></h3>`;
    for (const [x, d] of rows.slice(0, 40)) html += edgeHtml(x, prov, d);
    if (rows.length > 40) html += `<div class="more">… ${rows.length - 40} more (use brain/query.py or the API)</div>`;
    html += `</section>`;
  }

  if (e.rollup) {
    for (const grain of ["module", "dir"]) {
      const b = e.rollup[grain];
      if (!b || !kinds.has("depends")) continue;
      html += `<section class="kind"><h3>Strongest ${esc(grain)}-level dependencies
        <span class="cnt">(${b.counts.out} out / ${b.counts.in} in)</span></h3>`;
      for (const x of b.out.slice(0, 12)) html += edgeHtml(x, prov, "out");
      html += `</section>`;
      break;
    }
  }
  panelEl.innerHTML = html;
  panelEl.querySelectorAll("[data-nav]").forEach(a =>
    a.addEventListener("click", () => navigate(a.dataset.nav)));
  panelEl.querySelectorAll(".edge .row").forEach(r =>
    r.addEventListener("click", ev => {
      if (ev.target.closest("a") || ev.target.closest("[data-nav]")) return;
      r.parentElement.classList.toggle("open");
    }));
}

// ---- navigation --------------------------------------------------------------
async function navigate(id, depth) {
  if (depth !== undefined) {
    colPath = colPath.slice(0, depth); colPath[depth] = id;
  } else {
    const e = await getEntry(id);
    if (e && e.breadcrumb) colPath = e.breadcrumb.map(b => b.id);
    else if (e && e.node.type === "concept") {
      // land the columns on the concept's first formalization container
      const f = ((e.edges || {}).out || []).find(x => x.kind === "formalizes");
      if (f) {
        const fe = await getEntry(f.id);
        if (fe && fe.breadcrumb) colPath = fe.breadcrumb.map(b => b.id);
      }
    }
  }
  history.replaceState(null, "", "#" + encodeURIComponent(id));
  statusEl.textContent = id;
  await Promise.all([renderColumns(), renderPanel(id)]);
}

// ---- search -------------------------------------------------------------------
async function ensureLabels() {
  if (!labels) labels = await fetch(BASE + "labels.json").then(r => r.json());
  return labels;
}
let searchT = null;
$("#q").addEventListener("input", () => {
  clearTimeout(searchT);
  searchT = setTimeout(async () => {
    const q = $("#q").value.trim().toLowerCase();
    const box = $("#hits");
    if (q.length < 2) { box.style.display = "none"; return; }
    const L = await ensureLabels();
    const starts = [], contains = [];
    for (const r of L) {
      const l = r.label.toLowerCase();
      if (l.startsWith(q)) starts.push(r);
      else if (l.includes(q)) contains.push(r);
      if (starts.length >= 20) break;
    }
    const hits = [...starts, ...contains].slice(0, 20);
    box.innerHTML = hits.map(r =>
      `<div class="hit" data-id="${esc(r.id)}"><span class="t">${esc(r.type)}${
        r.status ? " · " + esc(r.status).replace("_", " ") : ""}</span> ${esc(r.label)}${
        r.n_decls ? ` <small style="color:#8c959f">${r.n_decls.toLocaleString()}</small>` : ""}</div>`).join("")
      || `<div class="hit"><span class="t">no hits</span> try /decl/&lt;name&gt; for declarations</div>`;
    box.style.display = "block";
    box.querySelectorAll("[data-id]").forEach(h =>
      h.addEventListener("click", () => { box.style.display = "none"; $("#q").value = ""; navigate(h.dataset.id); }));
  }, 150);
});
document.addEventListener("click", ev => {
  if (!ev.target.closest("#search")) $("#hits").style.display = "none";
});

// ---- toolbar re-renders --------------------------------------------------------
document.querySelectorAll(".toolbar input, #sort").forEach(el =>
  el.addEventListener("change", () => {
    const cur = decodeURIComponent(location.hash.slice(1));
    renderColumns();
    if (cur) renderPanel(cur);
  }));

// back/forward + externally-set hashes navigate too (navigate() uses
// replaceState, so its own updates never re-trigger this)
window.addEventListener("hashchange", () => {
  const id = decodeURIComponent(location.hash.slice(1));
  if (id) navigate(id);
});

// ---- boot -----------------------------------------------------------------------
(async function boot() {
  manifest = await fetch(BASE + "manifest.json").then(r => r.json());
  statusEl.textContent = `${manifest._meta.counts.entries.toLocaleString()} nodes · ` +
    `${manifest._meta.counts.ontology_edges.toLocaleString()} edges · ` +
    `data ${manifest._meta.generated_at.slice(0, 10)}`;
  await renderColumns();
  const target = decodeURIComponent(location.hash.slice(1));
  if (target) navigate(target);
})();
</script>
</body>
</html>
"""


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "brain.html").write_text(HTML)
    print(f"wrote {OUT_DIR / 'brain.html'} ({len(HTML) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
