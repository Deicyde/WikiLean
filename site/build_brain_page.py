#!/usr/bin/env python3
"""Generate /brain — the bubble+graph explorer over the BRAIN dataset.

One zoomable canvas, zero baked-in data: everything is fetched on demand from
the prefix shards in /assets/brain/ (manifest.json → one shard fetch per node),
so the client never loads the whole graph — brain/SCHEMA.md's locality law as UX.

  · Bubbles — one circle-pack level per focus container (library → module → …
              → file → decl), children sized by decl count, concepts packed
              beside the containers that anchor them. Click a bubble to zoom
              in (d3.interpolateZoom, the /map bubbles feel); click the
              background or breadcrumb to zoom out.
  · Graph   — real database connections drawn in space: sibling `depends`
              edges from the typed rollups (sig-weighted), and the selected
              node's ontology edges (formalizes / xref / cites / relates)
              overlaid by kind.
  · Panel   — the selected node: breadcrumb, altitude evidence, slogan, and
              every edge with its provenance one tap away (anti-slop drawer).
  · Layers  — per-source-kind toggles overlay or hide edge families.
  · Search  — label search over concepts + areas (labels.json, lazy).

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
<meta name="description" content="Explore the BRAIN: a zoomable bubble map of mathematics joining Wikipedia/Wikidata concepts, Lean formalizations across 39 libraries, cross-database identities (LMFDB, nLab, MathWorld, …) and arXiv literature — real dependency edges between bubbles, machine-checkable provenance on every link.">
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
#crumbbar { background:#fff; border-bottom:1px solid #eaeef2; padding:6px 20px;
  font-size:.82rem; color:#57606a; display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
#crumbbar a { cursor:pointer; }
#crumbbar .sep { color:#8c959f; }
.main { display:flex; height:calc(100vh - 132px); }
#stage { flex:1 1 62%; position:relative; background:#fff; overflow:hidden; }
#stage svg { display:block; width:100%; height:100%; }
#stage .hint { position:absolute; left:12px; bottom:10px; font-size:.72rem; color:#8c959f;
  pointer-events:none; }
circle.bubble { cursor:pointer; transition: stroke .12s; stroke:#fff0; }
circle.bubble:hover { stroke:#0969da; stroke-width:2px; }
circle.preview { pointer-events:none; }
circle.dot { cursor:pointer; stroke:#fff0; }
circle.dot:hover { stroke:#0969da; stroke-width:2px; }
circle.selring { fill:none; stroke:#0969da; stroke-width:2.5px; pointer-events:none; }
text.blabel { pointer-events:none; text-anchor:middle;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; fill:#1f2328; }
text.bcount { pointer-events:none; text-anchor:middle; fill:#57606a; }
path.link { pointer-events:none; }
path.ov { fill:none; pointer-events:none; stroke-dasharray:4 3; }
#panel { flex:1 1 38%; overflow-y:auto; padding:18px 22px; background:#fafbfc;
  border-left:1px solid #d0d7de; }
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
.codeblock { margin:8px 0; border:1px solid #d0d7de; border-radius:6px; background:#f6f8fa; }
.codeblock pre { margin:0; padding:8px 10px; overflow-x:auto; font-size:.76rem;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace; line-height:1.45; }
.codeblock .src { display:block; color:#8c959f; font-size:.7rem; padding:4px 10px 6px;
  border-top:1px solid #d8dee4; }
html[data-theme="dark"] .codeblock { background:#0d1117; border-color:#30363d; }
html[data-theme="dark"] .codeblock .src { border-color:#30363d; }
.lit-ref { color:#8c959f; font-size:.74rem; }
.note { color:#57606a; font-size:.8rem; }
.more { font-size:.78rem; color:#57606a; padding:4px 10px; }
.extlink { font-size:.8rem; }
@media (max-width: 900px) { .main { flex-direction:column; height:auto; }
  #stage { min-height:52vh; border-left:none; }
  #panel { border-left:none; border-top:1px solid #d0d7de; max-height:none; } }
html[data-theme="dark"] body { background:#0d1117; color:#e6edf3; }
html[data-theme="dark"] .wl-header, html[data-theme="dark"] .toolbar,
html[data-theme="dark"] #crumbbar, html[data-theme="dark"] #stage { background:#161b22;
  border-color:#30363d; }
html[data-theme="dark"] #panel { background:#0d1117; border-color:#30363d; }
html[data-theme="dark"] .edge, html[data-theme="dark"] .badge, html[data-theme="dark"] .chip,
html[data-theme="dark"] .slogan { background:#161b22; border-color:#30363d; }
html[data-theme="dark"] .edge .row:hover { background:#21262d; }
html[data-theme="dark"] .edge .drawer { background:#0d1117; border-color:#30363d; }
html[data-theme="dark"] #search input { background:#0d1117; border-color:#30363d; color:#e6edf3; }
html[data-theme="dark"] #hits { background:#161b22; border-color:#30363d; }
html[data-theme="dark"] #hits .hit:hover { background:#21262d; }
html[data-theme="dark"] text.blabel { fill:#e6edf3; }
html[data-theme="dark"] text.bcount { fill:#8b949e; }
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
    <a class="wl-navlink" id="srcbtn" style="cursor:pointer" title="every external database the brain links to — layer, provenance, license">Sources</a>
    <a class="wl-navlink" href="/map">Map</a>
    <a class="wl-navlink" href="/stats">Stats</a>
    <a class="wl-navlink" href="https://github.com/Deicyde/WikiLean" rel="noopener">GitHub</a>
  </nav>
</header>
<div class="toolbar">
  <span class="grp"><b>Layers</b>
    <label><input type="checkbox" data-k="depends" checked> formal deps</label>
    <label><input type="checkbox" data-k="formalizes" checked> formalizations</label>
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
  <span class="grp"><b>Structure</b>
    <label title="weight edges by observed/expected flow (null model) instead of raw volume — corrects the hub bias found by arXiv 2604.24797"><input type="checkbox" id="dehub" checked> de-hub (lift)</label>
    <label title="color bubble outlines by dependency-flow communities — where logical structure diverges from the folder tree (arXiv 2604.24797: NMI 0.34)"><input type="checkbox" id="commColor" checked> logical communities</label>
  </span>
  <span class="note" id="status">loading manifest…</span>
</div>
<div id="crumbbar"></div>
<div class="main">
  <div id="stage"><svg id="svg"></svg>
    <div class="hint">click a bubble to zoom in · background to zoom out · click any edge
      for its evidence · <span style="color:#8250df">formal deps</span> ·
      <span style="color:#0969da">formalizes</span> ·
      <span style="color:#d4a72c">wikidata relations</span> ·
      <span style="color:#bf5af2">same external page</span> ·
      dots = concepts (blue) / decls (green) · bubble outlines = logical communities</div>
  </div>
  <div id="panel"><p class="note">The Brain as bubbles: areas nest by containment
    (Mathlib → Algebra → Group → …), concepts float beside the code that formalizes
    them, and the links between bubbles are real, provenance-carrying dependency
    edges. Click anything — every edge opens its evidence drawer here.</p></div>
</div>
<script src="https://cdn.jsdelivr.net/npm/d3@7"></script>
<script>
"use strict";
const BASE = "/assets/brain/";
const LIBS_ID = "__libs__";           // pseudo-focus: the 39 library roots
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
    shardCache.set(key, fetch(BASE + key + ".json")
      .then(r => r.ok ? r.json() : {})
      .catch(() => { shardCache.delete(key); return {}; }));
  }
  const shard = await shardCache.get(key);
  const e = shard[id] || null;
  entryCache.set(id, e);
  return e;
}

const $ = s => document.querySelector(s);
const stageEl = $("#stage"), panelEl = $("#panel"), statusEl = $("#status");
const crumbEl = $("#crumbbar");
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

// ============================ canvas state ==================================
let focusId = null;        // container id (or LIBS_ID) whose children fill the stage
let selectedId = null;     // node the panel shows / ring highlights
let layout = null;         // {items: Map(id -> {x,y,r,item}), root}
const svg = d3.select("#svg");
const gEdges = svg.append("g");
const gBubbles = svg.append("g");
const gOverlay = svg.append("g");
const gLabels = svg.append("g");

const CONCEPT_COLOR = {formalized: "#0969da", partial: "#d4a72c", not_formalized: "#cf222e"};
function fillFor(item, depthShade) {
  if (item.type === "concept") return CONCEPT_COLOR[item.status] || "#0969da";
  if (item.type === "decl") return "#1a7f37";
  if (item.type === "strays") return "#8c959f";
  return depthShade;   // container
}

// children of the current focus, as pack-able items
async function focusItems(id) {
  if (id === LIBS_ID) {
    const kinds = activeLibKinds();
    return manifest.roots
      .filter(r => kinds.has(r.library_kind || "math"))
      .map(r => ({id: r.id, label: r.label, type: "container",
                  n_decls: r.n_decls || 1, n_concepts: 0}));
  }
  const e = await getEntry(id);
  if (!e) return [];
  const kids = (e.children && e.children.first || []).map(c => ({...c}));
  const conts = kids.filter(c => c.type === "container");
  let decls = kids.filter(c => c.type !== "container");
  // At levels that have sub-containers, loose decls (placed here because the
  // depth-capped tree has no deeper node) would flood the pack — collapse
  // them into one small bubble; the panel's children list still has them all.
  if (conts.length && decls.length > 12) {
    decls = [{id: "__strays__", type: "strays",
              label: decls.length + " loose decls", n: decls.length}];
  }
  // concepts anchored to THIS container (field-of-study altitude links)
  const anchored = (e.edges && e.edges.in || [])
    .filter(x => x.kind === "formalizes")
    .map(x => ({id: x.id, label: x.id, type: "concept",
                mk: x.evidence && x.evidence.match_kind}));
  // file level: concepts formalized by the decls here float beside them
  // (decls of one file share shard prefixes, so this is a handful of fetches)
  if (!conts.length && decls.length) {
    const seen = new Set(anchored.map(a => a.id));
    await Promise.all(decls.map(async d => {
      const de = await getEntry(d.id);
      for (const x of (de && de.edges && de.edges.in) || []) {
        if (x.kind !== "formalizes" || seen.has(x.id)) continue;
        seen.add(x.id);
        anchored.push({id: x.id, label: x.id, type: "concept",
                       mk: x.evidence && x.evidence.match_kind});
      }
    }));
  }
  return conts.concat(decls, anchored);
}

// pack values: container area ~ decl-count^0.6 (compresses Algebra=52k vs a
// 100-decl module into a ~6:1 radius ratio); concepts/decls small fixed dots
function packValue(item) {
  if (item.type === "container") return Math.pow(Math.max(item.n_decls || 1, 1), 0.6);
  if (item.type === "strays") return 30;
  return item.type === "concept" ? 6 : 2.5;
}

let renderSeq = 0;   // guards against out-of-order async renders
async function renderFocus(anim) {
  const seq = ++renderSeq;
  const items = await focusItems(focusId);
  if (seq !== renderSeq) return;
  const W = stageEl.clientWidth || 800, H = stageEl.clientHeight || 600;
  const root = d3.hierarchy({children: items}).sum(d => d.children ? 0 : packValue(d));
  d3.pack().size([W, H]).padding(items.length > 150 ? 1.5 : 4)(root);
  const leaves = root.leaves().filter(l => l.data.id);

  layout = {items: new Map(leaves.map(l => [l.data.id, l]))};
  gEdges.selectAll("*").remove();
  gOverlay.selectAll("*").remove();
  gBubbles.selectAll("circle.preview").remove();

  const shade = document.documentElement.dataset.theme === "dark" ? "#20304a" : "#dbeafe";
  const bubbles = gBubbles.selectAll("circle.node")
    .data(leaves, l => l.data.id);
  bubbles.exit().remove();
  const entered = bubbles.enter().append("circle")
    .attr("class", l => l.data.type === "container" ? "bubble node" : "dot node");
  entered.append("title");
  const all = entered.merge(bubbles)
    .attr("cx", l => l.x).attr("cy", l => l.y)
    .attr("r", l => Math.max(l.r, 2.5))
    .attr("fill", l => fillFor(l.data, shade))
    .attr("fill-opacity", l => l.data.type === "container" ? 0.55 : 0.9)
    .on("click", (ev, l) => { ev.stopPropagation(); nodeClick(l.data); });
  all.select("title").text(l => l.data.label + (l.data.n_decls
    ? ` — ${l.data.n_decls.toLocaleString()} decls` : ""));

  gLabels.selectAll("*").remove();
  for (const l of leaves) {
    if (l.data.type !== "container" || l.r < 24) continue;
    const fs = Math.max(10, Math.min(16, l.r / 4.5));
    gLabels.append("text").attr("class", "blabel")
      .attr("x", l.x).attr("y", l.y - (l.r > 40 ? 4 : -4)).attr("font-size", fs)
      .text(l.data.label);
    if (l.r > 40) {
      gLabels.append("text").attr("class", "bcount")
        .attr("x", l.x).attr("y", l.y + fs - 2).attr("font-size", fs * 0.72)
        .text(`${(l.data.n_decls || 0).toLocaleString()}${
          l.data.n_concepts ? " · " + l.data.n_concepts + "★" : ""}`);
    }
  }

  // concept dots boot with their QID as label; resolve real labels lazily
  for (const l of leaves) {
    if (l.data.type !== "concept") continue;
    getEntry(l.data.id).then(ce => {
      if (seq !== renderSeq || !ce || !ce.node.label) return;
      l.data.label = ce.node.label;
      all.filter(d => d.data.id === l.data.id).select("title").text(ce.node.label);
      if (l.r >= 11) {
        gLabels.append("text").attr("class", "blabel")
          .attr("x", l.x).attr("y", l.y + l.r + 10).attr("font-size", 10)
          .text(ce.node.label.length > 26
            ? ce.node.label.slice(0, 24) + "…" : ce.node.label);
      }
    });
  }

  drawSelRing();
  renderCrumb();
  if (anim) {
    const g = [gEdges, gBubbles, gOverlay, gLabels];
    for (const gr of g) gr.attr("opacity", 0).transition().duration(260).attr("opacity", 1);
    // rAF-driven transitions pause in background tabs — never leave the
    // canvas stuck invisible
    setTimeout(() => g.forEach(gr => { gr.interrupt(); gr.attr("opacity", 1); }), 600);
  }

  // background enrichment: children shards → inner previews + the edge web
  enrich(seq, leaves);
}

// EVERY drawn edge is a real brain edge (or a pair of xref edges to the same
// external page) and carries its payload for the click-to-inspect panel card.
const EDGE_STYLE = {
  depends:      {color: "#8250df", dash: null,   label: "formal dependency"},
  formalizes:   {color: "#0969da", dash: null,   label: "formalizes (formal↔informal join)"},
  relates:      {color: "#d4a72c", dash: "5 3",  label: "Wikidata relation (informal)"},
  mentions:     {color: "#8c959f", dash: "2 3",  label: "article mention (informal)"},
  "xref-shared":{color: "#bf5af2", dash: "5 3",  label: "same external-database page"},
};
let edgeStore = [];

// prefetch visible shards: grandchild previews, container rollup edges, and
// the ontology web among visible concepts/decls (formal + informal)
async function enrich(seq, leaves) {
  const visible = new Set(leaves.map(l => l.data.id));
  const containers = leaves.filter(l => l.data.type === "container");
  const store = new Map();   // kind|a|b -> edge
  const put = (kind, a, b, w, payload) => {
    const key = kind + "|" + (a < b ? a + "|" + b : b + "|" + a);
    const prev = store.get(key);
    if (!prev || w > prev.w) store.set(key, {kind, a, b, w, payload});
  };

  await Promise.all(containers.map(async l => {
    const e = await getEntry(l.data.id);
    if (seq !== renderSeq || !e) return;
    // grandchild preview: faint inner circles (top 24 by size)
    const kids = (e.children && e.children.first || [])
      .filter(c => c.type === "container").slice(0, 24);
    if (kids.length > 1 && l.r > 26) {
      const inner = d3.hierarchy({children: kids})
        .sum(d => d.children ? 0 : Math.pow(Math.max(d.n_decls || 1, 1), 0.6));
      d3.pack().size([l.r * 1.7, l.r * 1.7]).padding(2)(inner);
      for (const k of inner.leaves()) {
        gBubbles.append("circle").attr("class", "preview")
          .attr("cx", l.x - l.r * 0.85 + k.x).attr("cy", l.y - l.r * 0.85 + k.y)
          .attr("r", k.r).attr("fill", "none")
          .attr("stroke", "currentColor").attr("stroke-opacity", 0.14);
      }
    }
    // container↔container depends from the typed rollups (sig weights) —
    // both directions: a sibling link crowded out of A's top-N by global
    // hubs often survives in B's
    for (const grain of ["tree", "module", "dir"]) {
      const b = e.rollup && e.rollup[grain];
      if (!b) continue;
      for (const dir of ["out", "in"]) {
        for (const row of b[dir] || []) {
          if (!visible.has(row.id)) continue;
          const sig = row.evidence && row.evidence.w_types ? row.evidence.w_types.sig : 0;
          if (sig) put("depends", l.data.id, row.id, sig, row);
        }
      }
    }
  }));

  // the ontology web: every visible concept's edges to other visible nodes,
  // plus same-external-page pairs (two concepts both xref-ing one nLab/
  // MathWorld/LMFDB/… page — the cross-database fabric made visible)
  const concepts = leaves.filter(l => l.data.type === "concept");
  const xrefPages = new Map();   // external page id -> [concept ids]
  await Promise.all(concepts.map(async l => {
    const e = await getEntry(l.data.id);
    if (seq !== renderSeq || !e) return;
    for (const dir of ["out", "in"]) {
      for (const x of (e.edges && e.edges[dir]) || []) {
        if (x.kind === "xref") {
          const arr = xrefPages.get(x.id) || [];
          arr.push([l.data.id, x]);
          xrefPages.set(x.id, arr);
          continue;
        }
        if (!visible.has(x.id) || !EDGE_STYLE[x.kind]) continue;
        const w = x.kind === "depends" && x.evidence && x.evidence.w_types
          ? x.evidence.w_types.sig || 1 : 1;
        put(x.kind, l.data.id, x.id, w, x);
      }
    }
  }));
  for (const [page, arr] of xrefPages) {
    if (arr.length < 2) continue;
    for (let i = 0; i < arr.length; i++)
      for (let j = i + 1; j < arr.length; j++)
        put("xref-shared", arr[i][0], arr[j][0], 1,
            {evidence: {shared_page: page, via: [arr[i][1], arr[j][1]]},
             confidence: "high"});
  }

  if (seq !== renderSeq) return;
  edgeStore = [...store.values()];
  renderEdges();
}

function liftOf(e) {
  return (e.payload && e.payload.evidence && e.payload.evidence.lift) || null;
}
function renderEdges() {
  gEdges.selectAll("*").remove();
  if (!layout) return;
  const kinds = activeKinds();
  const dehub = $("#dehub").checked;
  const show = edgeStore.filter(e =>
    e.kind === "xref-shared" ? kinds.has("xref") : kinds.has(e.kind));
  // depends dominates by count — cap it, keep every join/informal edge.
  // De-hub mode ranks by lift×√sig (affinity × volume) instead of raw volume,
  // per arXiv 2604.24797: raw weights measure infrastructure, not relevance.
  const rank = e => dehub ? (liftOf(e) || 0.05) * Math.sqrt(e.w) : e.w;
  const dep = show.filter(e => e.kind === "depends")
    .sort((x, y) => rank(y) - rank(x)).slice(0, 250);
  const rest = show.filter(e => e.kind !== "depends");
  const maxSig = dep.reduce((m, e) => Math.max(m, e.w), 1);
  const depOpacity = e => {
    const lf = liftOf(e);
    return dehub && lf !== null
      ? Math.min(0.9, Math.max(0.06, 0.06 + 0.17 * Math.log2(1 + lf)))
      : 0.16 + 0.3 * (e.w / maxSig);
  };
  for (const e of [...dep, ...rest]) {
    const A = layout.items.get(e.a), B = layout.items.get(e.b);
    if (!A || !B) continue;
    const mx = (A.x + B.x) / 2, my = (A.y + B.y) / 2;
    const dx = B.x - A.x, dy = B.y - A.y;
    const d = `M${A.x},${A.y} Q${mx - dy * 0.18},${my + dx * 0.18} ${B.x},${B.y}`;
    const st = EDGE_STYLE[e.kind];
    const isDep = e.kind === "depends";
    const baseOp = isDep ? depOpacity(e) : 0.5;
    const p = gEdges.append("path").attr("class", "link")
      .attr("d", d).attr("fill", "none")
      .attr("stroke", st.color)
      .attr("stroke-width", isDep ? 0.6 + 2.6 * Math.sqrt(e.w / maxSig) : 1.3)
      .attr("stroke-opacity", baseOp);
    if (st.dash) p.attr("stroke-dasharray", st.dash);
    // invisible fat twin = the click/hover target
    gEdges.append("path").attr("class", "hit")
      .attr("d", d).attr("fill", "none")
      .attr("stroke", "transparent").attr("stroke-width", 9)
      .style("cursor", "pointer")
      .on("mouseenter", () => p.attr("stroke-opacity", 0.95))
      .on("mouseleave", () => p.attr("stroke-opacity", baseOp))
      .on("click", ev => { ev.stopPropagation(); showEdgePanel(e); });
  }
  paintCommunities();
}

// Logical-community coloring of the visible level: greedy modularity merging
// over the sibling depends graph (lift-corrected weights). Makes the paper's
// Finding 1 visible — where dependency communities cut across the folder tree.
const COMM_PALETTE = ["#e6550d", "#1a7f37", "#0969da", "#bf5af2", "#d4a72c",
                      "#cf222e", "#0e7490", "#6e7781", "#9a6700", "#8250df"];
function communitiesOf(ids, links) {
  const m = links.reduce((s, l) => s + l.w, 0);
  if (!m || ids.length < 3) return null;
  const deg = new Map(ids.map(i => [i, 0]));
  for (const l of links) { deg.set(l.a, deg.get(l.a) + l.w); deg.set(l.b, deg.get(l.b) + l.w); }
  const comm = new Map(ids.map((i, k) => [i, k]));
  const tot = new Map(ids.map((i, k) => [k, deg.get(i)]));
  for (let round = 0; round < 40; round++) {
    const cw = new Map();
    for (const l of links) {
      const ca = comm.get(l.a), cb = comm.get(l.b);
      if (ca === cb) continue;
      const key = ca < cb ? ca + ":" + cb : cb + ":" + ca;
      cw.set(key, (cw.get(key) || 0) + l.w);
    }
    let best = null, bestDq = 1e-9;
    for (const [key, w] of cw) {
      const [ca, cb] = key.split(":").map(Number);
      const dq = w / m - (tot.get(ca) * tot.get(cb)) / (2 * m * m);
      if (dq > bestDq) { bestDq = dq; best = [ca, cb]; }
    }
    if (!best) break;
    const [ca, cb] = best;
    for (const [i, c] of comm) if (c === cb) comm.set(i, ca);
    tot.set(ca, tot.get(ca) + tot.get(cb));
    tot.delete(cb);
  }
  return comm;
}
function paintCommunities() {
  const conts = [...layout.items.values()].filter(l => l.data.type === "container");
  gBubbles.selectAll("circle.bubble").attr("stroke", null).attr("stroke-width", null)
    .attr("stroke-opacity", null);
  if (!$("#commColor").checked || !activeKinds().has("depends")) return;
  const ids = conts.map(l => l.data.id);
  const idset = new Set(ids);
  const links = edgeStore.filter(e => e.kind === "depends"
      && idset.has(e.a) && idset.has(e.b))
    .map(e => ({a: e.a, b: e.b, w: e.w * (liftOf(e) || 1)}));
  const comm = communitiesOf(ids, links);
  if (!comm) return;
  const sizes = new Map();
  for (const c of comm.values()) sizes.set(c, (sizes.get(c) || 0) + 1);
  const colorOf = new Map();
  let ci = 0;
  gBubbles.selectAll("circle.bubble").each(function(l) {
    const c = comm.get(l.data.id);
    if (c === undefined || sizes.get(c) < 2) return;
    if (!colorOf.has(c)) colorOf.set(c, COMM_PALETTE[ci++ % COMM_PALETTE.length]);
    d3.select(this).attr("stroke", colorOf.get(c))
      .attr("stroke-width", 2.4).attr("stroke-opacity", 0.9);
  });
}

// click-to-inspect: the edge's provenance card in the panel
function showEdgePanel(e) {
  const st = EDGE_STYLE[e.kind];
  const prov = (e.payload && e.payload.prov !== undefined && manifest.prov[e.payload.prov]) || null;
  const ev = (e.payload && e.payload.evidence) || {};
  const name = id => {
    const L = layout.items.get(id);
    return (L && L.data.label) || id;
  };
  panelEl.innerHTML = `
    <h2 style="font-size:1.05rem">${esc(st.label)}</h2>
    <div class="sub"><span style="color:${st.color}">●</span> ${esc(e.kind)}${
      e.payload && e.payload.confidence ? ` · confidence ${esc(e.payload.confidence)}` : ""}${
      e.kind === "depends" ? ` · sig weight ${e.w}` : ""}${
      liftOf(e) !== null ? ` · lift ${liftOf(e)}× vs null model` : ""}</div>
    <div class="chips">
      <span class="chip"><a data-nav="${esc(e.a)}">${esc(name(e.a))}</a></span>
      <span class="chip">↔</span>
      <span class="chip"><a data-nav="${esc(e.b)}">${esc(name(e.b))}</a></span>
    </div>
    <section class="kind"><h3>Evidence</h3>
      <div class="edge open"><div class="drawer" style="display:block">${
        prov ? `provenance: <b>${esc(prov.source)}</b> · ${esc(prov.method)} · pin ${esc(prov.pin)}` : ""}
        <pre>${esc(JSON.stringify(ev, null, 1))}</pre></div></div>
    </section>
    <p class="note">Every line on the canvas is a stored brain edge (or, for
    "same external-database page", the pair of xref edges shown above). Click
    the endpoints to inspect the nodes.</p>`;
  panelEl.querySelectorAll("[data-nav]").forEach(a =>
    a.addEventListener("click", () => navigate(a.dataset.nav)));
}

// overlay: the selected node's ontology edges to visible endpoints
const OV_COLOR = {formalizes: "#0969da", xref: "#bf5af2", cites: "#d4a72c",
                  matches: "#d4a72c", relates: "#57606a", mentions: "#8c959f",
                  depends: "#8250df"};
async function drawOverlay() {
  gOverlay.selectAll("path.ov").remove();
  if (!selectedId || !layout) return;
  const S = layout.items.get(selectedId);
  const e = await getEntry(selectedId);
  if (!e || !S) return;
  const kinds = activeKinds();
  for (const dir of ["out", "in"]) {
    for (const x of (e.edges && e.edges[dir]) || []) {
      if (!kinds.has(x.kind)) continue;
      const T = layout.items.get(x.id);
      if (!T) continue;
      gOverlay.append("path").attr("class", "ov")
        .attr("d", `M${S.x},${S.y} L${T.x},${T.y}`)
        .attr("stroke", OV_COLOR[x.kind] || "#57606a")
        .attr("stroke-width", 1.6).attr("stroke-opacity", 0.8);
    }
  }
}
function drawSelRing() {
  gOverlay.selectAll("circle.selring").remove();
  const S = selectedId && layout && layout.items.get(selectedId);
  if (S) gOverlay.append("circle").attr("class", "selring")
    .attr("cx", S.x).attr("cy", S.y).attr("r", Math.max(S.r, 3) + 3);
  drawOverlay();
}

// ============================ zoom navigation ================================
async function zoomInto(id) {
  // slick part: scale the clicked bubble up to fill the stage, then swap levels
  const L = layout && layout.items.get(id);
  if (L) {
    const W = stageEl.clientWidth, H = stageEl.clientHeight;
    const k = Math.min(W, H) / (L.r * 2.2);
    const t = d3.zoomIdentity.translate(W / 2 - L.x * k, H / 2 - L.y * k).scale(k);
    const groups = [gEdges, gBubbles, gOverlay, gLabels];
    // race the transition against a timer: rAF pauses in background tabs and
    // the reset below must ALWAYS run
    await Promise.race([
      Promise.all(groups.map(g =>
        g.transition().duration(420).ease(d3.easeCubicInOut)
          .attr("transform", t.toString()).attr("opacity", g === gBubbles ? 0.35 : 0)
          .end().catch(() => {}))),
      new Promise(r => setTimeout(r, 700)),
    ]);
    groups.forEach(g => { g.interrupt(); g.attr("transform", null).attr("opacity", 1); });
  }
  focusId = id;
  history.replaceState(null, "", "#" + encodeURIComponent(id));
  await renderFocus(true);
}
async function zoomOut() {
  if (focusId === LIBS_ID) return;
  const e = await getEntry(focusId);
  const bc = (e && e.breadcrumb) || [];
  const parent = bc.length > 1 ? bc[bc.length - 2].id : LIBS_ID;
  focusId = parent;
  selectedId = null;
  history.replaceState(null, "", "#" + encodeURIComponent(
    parent === LIBS_ID ? "" : parent));
  await renderFocus(true);
}
svg.on("click", () => { zoomOut(); });

async function nodeClick(item) {
  if (item.type === "strays") {   // the collapsed loose-decl bubble: list them
    renderPanel(focusId);
    return;
  }
  if (item.type === "container") {
    selectedId = null;
    renderPanel(item.id);
    await zoomInto(item.id);
  } else {
    selectedId = item.id;
    history.replaceState(null, "", "#" + encodeURIComponent(item.id));
    renderPanel(item.id);
    drawSelRing();
  }
}

// land the canvas on any node id: containers focus themselves; leaves focus
// their parent container and select themselves
async function navigate(id) {
  const e = await getEntry(id);
  if (!e) { renderPanel(id); return; }
  if (e.node.type === "container") {
    focusId = id; selectedId = null;
  } else if (e.breadcrumb && e.breadcrumb.length > 1) {
    focusId = e.breadcrumb[e.breadcrumb.length - 2].id;
    selectedId = id;
  } else if (e.node.type === "concept") {
    const f = ((e.edges || {}).out || []).find(x => x.kind === "formalizes");
    const fe = f && await getEntry(f.id);
    if (fe && fe.node.type === "container") focusId = f.id;
    else if (fe && fe.breadcrumb && fe.breadcrumb.length > 1)
      focusId = fe.breadcrumb[fe.breadcrumb.length - 2].id;
    else focusId = "path:Mathlib";
    selectedId = id;
  } else { focusId = "path:Mathlib"; selectedId = id; }
  history.replaceState(null, "", "#" + encodeURIComponent(id));
  renderPanel(id);
  await renderFocus(true);
}

async function renderCrumb() {
  let html = `<a data-nav="${LIBS_ID}">all libraries</a>`;
  if (focusId !== LIBS_ID) {
    const e = await getEntry(focusId);
    for (const b of (e && e.breadcrumb) || []) {
      html += ` <span class="sep">/</span> ` + (b.id === focusId
        ? `<b>${esc(b.label)}</b>` : `<a data-nav="${esc(b.id)}">${esc(b.label)}</a>`);
    }
  }
  crumbEl.innerHTML = html;
  crumbEl.querySelectorAll("[data-nav]").forEach(a =>
    a.addEventListener("click", () => {
      if (a.dataset.nav === LIBS_ID) { focusId = LIBS_ID; selectedId = null;
        history.replaceState(null, "", "#"); renderFocus(true); }
      else navigate(a.dataset.nav);
    }));
}

// ============================ panel ==========================================
const KIND_LABEL = {
  formalizes: "Formalizations", mentions: "Article mentions", depends: "Formal dependencies",
  matches: "Formal ↔ literature matches", xref: "Cross-database identity",
  relates: "Wikidata relations", cites: "Stated in the literature (TheoremGraph)",
  contains: "Contains",
};
const XREF_NAME = {mathworld: "MathWorld", nlab: "nLab", proofwiki: "ProofWiki",
  eom: "Encyclopedia of Math", planetmath: "PlanetMath", metamath: "Metamath",
  lmfdb_knowl: "LMFDB", oeis: "OEIS", dlmf: "DLMF", msc: "MSC"};
const XREF_URL = {
  mathworld: v => `https://mathworld.wolfram.com/${v}.html`,
  nlab: v => `https://ncatlab.org/nlab/show/${encodeURIComponent(v)}`,
  proofwiki: v => `https://proofwiki.org/wiki/${encodeURIComponent(v)}`,
  eom: v => `https://encyclopediaofmath.org/wiki/${/%[0-9A-Fa-f]{2}/.test(v) ? v : encodeURIComponent(v)}`,
  planetmath: v => `https://planetmath.org/${encodeURIComponent(v)}`,
  metamath: v => `https://us.metamath.org/mpeuni/${encodeURIComponent(v)}.html`,
  lmfdb_knowl: v => `https://www.lmfdb.org/knowledge/show/${encodeURIComponent(v)}`,
  oeis: v => `https://oeis.org/${encodeURIComponent(v)}`,
  dlmf: v => `https://dlmf.nist.gov/${encodeURIComponent(v)}`,
  msc: () => null,
};
function nodeUrl(id) {
  if (id.startsWith("decl:")) return "/decl/" + encodeURIComponent(id.slice(id.indexOf(":", 5) + 1));
  if (id.startsWith("lit:")) {
    const ax = id.slice(4).split("#")[0];
    if (/^[A-Za-z.-]+\/\d{7}(v\d+)?$/.test(ax) || !ax.includes("/"))
      return `https://arxiv.org/abs/${ax}`;
    return `https://github.com/${ax}`;
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
  const n = e.node, prov = manifest.prov;
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
  // "Also in" — every external identity of this concept as one chip strip
  // (the /map concept-panel affordance): article, Wikidata, Google KG, and
  // each cross-referenced database, deep-linked
  if (n.type === "concept") {
    const chips = [];
    if (n.slug) chips.push(`<a href="/${esc(n.slug)}">WikiLean article</a>`);
    chips.push(`<a href="https://www.wikidata.org/wiki/${esc(n.id)}" rel="noopener" target="_blank">Wikidata</a>`);
    if (n.kgmid) chips.push(`<a href="https://www.google.com/search?kgmid=${encodeURIComponent(n.kgmid)}" rel="noopener" target="_blank">Google Knowledge Graph</a>`);
    for (const x of (e.edges && e.edges.out) || []) {
      if (x.kind !== "xref") continue;
      const key = x.id.split(":")[1];
      const mkUrl = XREF_URL[key];
      const url = mkUrl && x.evidence && mkUrl(x.evidence.value);
      const nm = XREF_NAME[key] || key;
      chips.push(url ? `<a href="${esc(url)}" rel="noopener" target="_blank">${esc(nm)}</a>` : esc(nm));
    }
    if (chips.length)
      html += `<div class="chips"><span class="note">Also in:</span> ` +
              chips.map(c => `<span class="chip">${c}</span>`).join("") + `</div>`;
  }
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
  if (n.code) {
    html += `<div class="codeblock"><pre>${esc(n.code)}</pre><span class="src">${
      esc(n.decl_kind || "decl")} — mathlib4 source (Apache-2.0) · ` +
      `<a href="${esc(nodeUrl(n.id))}" rel="noopener" target="_blank">Mathlib docs ↗</a></span></div>`;
  }
  if (n.docstring) html += `<p class="note">${esc(n.docstring)}</p>`;
  if (n.arxiv_id) html += `<p class="note">appears as <b>${esc(n.ref || "?")}</b> of
    <a href="${esc(nodeUrl(n.id))}" rel="noopener" target="_blank">${esc(n.arxiv_id)}</a>
    ${n.license_open ? "" : " (text not redistributable — link only)"}</p>`;

  if (e.children && e.children.count) {
    html += `<section class="kind"><h3>Children <span class="cnt">(${e.children.count})</span></h3><div class="chips">`;
    for (const c of e.children.first.slice(0, 60))
      html += `<span class="chip"><a data-nav="${esc(c.id)}">${esc(c.label || c.id)}</a>${c.n_decls ? ` <small>${c.n_decls.toLocaleString()}</small>` : ""}</span>`;
    if (e.children.count > 60)
      html += `<span class="chip">… +${e.children.count - 60} more</span>`;
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
    for (const grain of ["tree", "module", "dir"]) {
      const b = e.rollup[grain];
      if (!b || !kinds.has("depends")) continue;
      html += `<section class="kind"><h3>Strongest ${esc(grain === 'tree' ? 'sibling' : grain)}-level dependencies
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
  // literature rows boot as raw lit: ids — resolve paper titles lazily (their
  // entries share arXiv-prefix shards, so this is a handful of fetches)
  [...panelEl.querySelectorAll('[data-nav^="lit:"]')].slice(0, 20).forEach(async el => {
    const lid = el.dataset.nav;
    const le = await getEntry(lid);
    if (!le || !le.node.label || le.node.label === lid) return;
    const t = le.node.label.length > 90 ? le.node.label.slice(0, 88) + "…" : le.node.label;
    el.innerHTML = `${esc(t)} <span class="lit-ref">${esc(le.node.ref || "")} · ${
      esc(le.node.arxiv_id || "")}</span>`;
  });
}

// ---- the transparency legend: /map's Sources view, rendered in the panel ----
let sourcesData = null;
async function showSourcesPanel() {
  if (!sourcesData) {
    const r = await fetch(BASE + "sources.json");
    if (!r.ok) { panelEl.innerHTML = `<p class="note">sources.json unavailable</p>`; return; }
    sourcesData = await r.json();
  }
  const GROUP_LABEL = {spine: "The join spine", node_sources: "Node sources",
    edge_sources: "Edge sources", crossref_sources: "Cross-reference databases",
    literature_sources: "Literature", frontier_sources: "Research frontier",
    brain_sources: "Brain pipeline"};
  let html = `<h2>Sources</h2>
    <div class="sub">every external database the brain links to — its layer in the
    formal↔informal stack, how WE obtained each link, and the target's own license</div>`;
  html += `<div class="chips">` + Object.entries(sourcesData.layers).map(([k, v]) =>
    `<span class="chip" title="${esc(v)}">${esc(k)}</span>`).join("") + `</div>`;
  html += `<p class="note">WikiLean's own annotation + graph data: ${
    esc(sourcesData.our_data_license.annotations)} / ${
    esc(sourcesData.our_data_license.concept_graph)}. ${
    esc(sourcesData.our_data_license.note || "")}</p>`;
  const byGroup = new Map();
  for (const s of sourcesData.sources) {
    if (!byGroup.has(s.group)) byGroup.set(s.group, []);
    byGroup.get(s.group).push(s);
  }
  for (const [grp, rows] of byGroup) {
    html += `<section class="kind"><h3>${esc(GROUP_LABEL[grp] || grp)} <span class="cnt">(${rows.length})</span></h3>`;
    for (const s of rows) {
      html += `<div class="edge"><div class="row">${
        s.homepage ? `<a href="${esc(s.homepage)}" rel="noopener" target="_blank"><b>${esc(s.name || s.key)}</b></a>` : `<b>${esc(s.name || s.key)}</b>`}
        <span class="mk">${esc(s.layer)}</span>${
        s.wikidata_property ? ` <span class="lit-ref">${esc(s.wikidata_property)}</span>` : ""}
        <span class="conf">${esc(s.target_license || "—")}</span></div>
        <div class="drawer">${esc(s.kind || "")}${s.kind ? "<br>" : ""}<i>${esc(s.our_provenance || "")}</i>${
        s.note ? `<br>${esc(s.note)}` : ""}</div></div>`;
    }
    html += `</section>`;
  }
  html += `<p class="note">Identifiers are read from Wikidata (CC0) or derived locally —
    linked-target content keeps each project's own license.</p>`;
  panelEl.innerHTML = html;
  panelEl.querySelectorAll(".edge .row").forEach(r =>
    r.addEventListener("click", () => r.parentElement.classList.toggle("open")));
}
$("#srcbtn").addEventListener("click", showSourcesPanel);

// ============================ search =========================================
async function ensureLabels() {
  if (!labels) {
    const r = await fetch(BASE + "labels.json");
    labels = r.ok ? await r.json() : [];
  }
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

// ============================ toolbar + boot =================================
document.querySelectorAll(".toolbar input").forEach(el =>
  el.addEventListener("change", () => {
    if (el.dataset.lk && focusId === LIBS_ID) { renderFocus(false); return; }
    renderEdges();
    drawSelRing();
    if (selectedId) renderPanel(selectedId);
    else if (focusId !== LIBS_ID) renderPanel(focusId);
  }));

window.addEventListener("hashchange", () => {
  const id = decodeURIComponent(location.hash.slice(1));
  if (id) navigate(id);
});
window.addEventListener("resize", () => { renderFocus(false); });

(async function boot() {
  try {
    const r = await fetch(BASE + "manifest.json");
    if (!r.ok) throw new Error("HTTP " + r.status);
    manifest = await r.json();
  } catch (e) {
    statusEl.textContent = "brain data unavailable (" + e.message +
      ") — run brain/build_shards.py + build-public";
    return;
  }
  statusEl.textContent = `${manifest._meta.counts.entries.toLocaleString()} nodes · ` +
    `${manifest._meta.counts.ontology_edges.toLocaleString()} edges · ` +
    `data ${manifest._meta.generated_at.slice(0, 10)}`;
  const target = decodeURIComponent(location.hash.slice(1));
  if (target) { await navigate(target); }
  else { focusId = "path:Mathlib"; await renderFocus(false); renderPanel(focusId); }
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
