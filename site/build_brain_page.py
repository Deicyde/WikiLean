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
body { margin:0; background:#0b0e14; color:#e6e4de;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
a { color:#7cb3ff; text-decoration:none; }
a:hover { text-decoration:underline; }
.wl-header { background:#10141d; border-bottom:1px solid #262c3a; padding:10px 20px;
  display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; }
.wl-brand { font-weight:700; color:#7cb3ff; font-size:18px; }
.wl-nav { display:flex; gap:14px; align-items:center; flex-wrap:wrap; }
.wl-navlink { font-size:.9rem; }
.toolbar { background:#10141d; border-bottom:1px solid #262c3a; padding:8px 20px;
  display:flex; gap:14px; align-items:center; flex-wrap:wrap; font-size:.85rem; }
.toolbar label { display:inline-flex; align-items:center; gap:4px; cursor:pointer;
  color:#9aa3b2; user-select:none; }
.toolbar .grp { display:inline-flex; gap:10px; align-items:center; padding-right:14px;
  border-right:1px solid #262c3a; }
.toolbar .grp:last-child { border-right:none; }
.toolbar b { color:#e6e4de; }
#search { position:relative; }
#search input { width:290px; padding:5px 9px; border:1px solid #33405c; border-radius:6px;
  font-size:.88rem; background:#0b0e14; color:#e6e4de; }
#search input:focus { outline:2px solid #38bdf855; }
#hits { position:absolute; top:32px; left:0; z-index:30; width:420px; max-height:380px;
  overflow:auto; background:#151b28; border:1px solid #33405c; border-radius:8px;
  box-shadow:0 8px 24px rgba(0,0,0,.5); display:none; }
#hits .hit { padding:6px 10px; cursor:pointer; display:flex; gap:8px; align-items:baseline; }
#hits .hit:hover { background:#1e2635; }
#hits .hit .t { font-size:.72rem; color:#9aa3b2; min-width:64px; }
#crumbbar { background:#10141d; border-bottom:1px solid #1c2230; padding:6px 20px;
  font-size:.82rem; color:#9aa3b2; display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
#crumbbar a { cursor:pointer; }
#crumbbar .sep { color:#556074; }
#crumbbar b { color:#e6e4de; }
.main { display:flex; height:calc(100vh - 132px); }
#stage { flex:1 1 62%; position:relative; background:#0b0e14; overflow:hidden; }
#stage svg { display:block; width:100%; height:100%; }
#stage .hint { position:absolute; left:12px; bottom:10px; font-size:.72rem;
  pointer-events:none; color:#77808f; }
circle.bubble { cursor:pointer; transition: stroke .12s; stroke:#fff0; }
circle.bubble:hover { stroke:#38bdf8; stroke-width:2px; }
circle.preview { pointer-events:none; }
circle.dot { cursor:pointer; stroke:#fff0; }
circle.dot:hover { stroke:#38bdf8; stroke-width:2px; }
circle.selring { fill:none; stroke:#38bdf8; stroke-width:2.5px; pointer-events:none; }
text.blabel { pointer-events:none; text-anchor:middle;
  font-family:Georgia,"Iowan Old Style","Times New Roman",serif; fill:#e8e6e1; }
text.bcount { pointer-events:none; text-anchor:middle; fill:#9aa3b2;
  font-family:Georgia,serif; }
path.link { pointer-events:none; }
path.ov { fill:none; pointer-events:none; stroke-dasharray:4 3; }

/* the reading surface: an encyclopedia page beside a star map */
#panel { flex:1 1 38%; overflow-y:auto; padding:20px 26px; background:#f6f1e5;
  border-left:1px solid #262c3a; color:#151310;
  font-family:Georgia,"Iowan Old Style","Times New Roman",serif; }
#panel a { color:#1a4b8f; }
#panel h2 { margin:0 0 2px; font-size:1.35rem; font-weight:700; color:#0d0c0a;
  letter-spacing:.01em; }
#panel .sub { margin-bottom:10px; color:#5a544a; font-size:.88rem; }
.crumb { font-size:.8rem; color:#5a544a; margin-bottom:8px; }
.crumb a { cursor:pointer; }
.badge { display:inline-block; padding:1px 8px; border-radius:10px; font-size:.72rem;
  border:1px solid #c8bfa8; color:#5a544a; margin:0 4px 4px 0; background:#fdfbf4; }
.badge.f { border-color:#1a7f37; color:#116329; }
.badge.p { border-color:#b58800; color:#7d5e00; }
.badge.n { border-color:#c93c37; color:#a12621; }
.chips { margin:8px 0; }
.chip { display:inline-block; margin:0 6px 6px 0; padding:2px 9px; border:1px solid #c8bfa8;
  border-radius:12px; font-size:.78rem; background:#fdfbf4; }
section.kind { margin-top:16px; }
section.kind h3 { font-size:.95rem; margin:0 0 6px; color:#0d0c0a; font-weight:700;
  border-bottom:1px solid #d8cfb8; padding-bottom:2px; }
section.kind h3 .cnt { color:#8a8272; font-weight:400; font-size:.8rem; }
.edge { border:1px solid #ddd4bd; border-radius:6px; margin-bottom:6px; background:#fdfbf4; }
.edge .row { padding:6px 10px; display:flex; gap:8px; align-items:baseline; cursor:pointer;
  font-size:.86rem; flex-wrap:wrap; }
.edge .row:hover { background:#f3ecda; }
.edge .mk { color:#6d28d9; font-size:.74rem; font-style:italic; }
.edge .prov { font-size:.7rem; border-radius:8px; padding:0 6px; border:1px solid #c8bfa8;
  color:#5a544a; margin-left:auto; white-space:nowrap; font-family:-apple-system,sans-serif; }
.edge .prov.human { border-color:#1a7f37; color:#116329; }
.edge .prov.machine { border-color:#6d28d9; color:#5b21b6; }
.edge .prov.ai { border-color:#c2540a; color:#9a3f00; }
.edge .drawer { display:none; border-top:1px solid #ddd4bd; padding:8px 10px; font-size:.78rem;
  background:#f3ecda; border-radius:0 0 6px 6px; }
.edge .drawer pre { margin:4px 0 0; white-space:pre-wrap; word-break:break-word;
  font-size:.72rem; color:#2b2822;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
.edge.open .drawer { display:block; }
.slogan { border-left:3px solid #6d28d9; padding:6px 10px; background:#fdfbf4; margin:8px 0;
  font-size:.88rem; border-radius:0 6px 6px 0; font-style:italic; }
.slogan .src { display:block; color:#8a8272; font-size:.7rem; margin-top:3px; font-style:normal; }
.codeblock { margin:8px 0; border:1px solid #ddd4bd; border-radius:6px; background:#fbf8ef; }
.codeblock pre { margin:0; padding:8px 10px; overflow-x:auto; font-size:.76rem;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace; line-height:1.45; color:#1f1d18; }
.codeblock .src { display:block; color:#8a8272; font-size:.7rem; padding:4px 10px 6px;
  border-top:1px solid #ddd4bd; }
.lit-ref { color:#8a8272; font-size:.74rem; }
.note { color:#5a544a; font-size:.82rem; }
.more { font-size:.78rem; color:#5a544a; padding:4px 10px; }
.extlink { font-size:.8rem; }
body.embed .wl-header, body.embed #crumbbar { display:none; }
body.embed .main { height:calc(100vh - 44px); }
@media (max-width: 900px) { .main { flex-direction:column; height:auto; }
  #stage { min-height:52vh; border-left:none; }
  #panel { border-left:none; border-top:1px solid #262c3a; max-height:none; } }
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
  <span class="grp"><b>View</b>
    <label><input type="radio" name="vm" value="bubbles" checked> bubbles</label>
    <label title="same level, force-directed — nodes spread apart so the connections dominate and every edge is easy to hit"><input type="radio" name="vm" value="web"> web</label>
  </span>
  <span class="grp"><b>Layers</b>
    <label><input type="checkbox" data-k="depends" checked> formal deps</label>
    <label><input type="checkbox" data-k="formalizes" checked> formalizations</label>
    <label><input type="checkbox" data-k="xref" checked> cross-refs</label>
    <label><input type="checkbox" data-k="cites,matches" checked> literature</label>
    <label><input type="checkbox" data-k="relates" checked> wikidata relations</label>
    <label><input type="checkbox" data-k="mentions" checked> article mentions</label>
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
  <span class="grp"><b>Provenance</b>
    <label title="community/human-curated: Wikidata properties &amp; claims, @[wikidata]/@[stacks]/@[kerodon] attributes written in Mathlib source"><input type="checkbox" data-p="human" checked> human</label>
    <label title="machine-verified: kernel-extracted dependencies and the file tree — checked by the Lean compiler, no judgment involved"><input type="checkbox" data-p="machine" checked> machine</label>
    <label title="AI-generated: agent-proposed concept matches (skeptic-reviewed), LLM-judged paper matches (TheoremGraph), pipeline annotations"><input type="checkbox" data-p="ai" checked> AI</label>
  </span>
  <span class="grp"><a id="srcbtn2" style="cursor:pointer"
    title="every external database the brain links to — layer, provenance, license">Sources</a></span>
  <span class="note" id="status">loading manifest…</span>
</div>
<div id="crumbbar"></div>
<div class="main">
  <div id="stage"><svg id="svg"></svg>
    <div class="hint">click a bubble to zoom in · background to zoom out · click any edge
      for its evidence · <span style="color:#a78bfa">formal deps</span> ·
      <span style="color:#38bdf8">formalizes</span> ·
      <span style="color:#fbbf24">wikidata relations</span> ·
      <span style="color:#f472b6">cross-database</span> ·
      <span style="color:#fb923c">literature</span> ·
      <span style="color:#2dd4bf">matches</span> ·
      dots = concepts (blue) / decls (green) · outlines = logical communities ·
      selecting a node orbits its neighbors (click to travel)</div>
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
// Every data fetch is pinned to the manifest's data version: shard KEY NAMES
// change across rebuilds, so a cached manifest + fresh shards (or vice versa)
// silently 404s — the "Unknown node" ghost bug. The manifest revalidates
// (no-cache) and a missing shard triggers one manifest re-sync + retry.
let dataV = "";
const vq = () => (dataV ? "?v=" + dataV : "");
async function fetchManifest() {
  const r = await fetch(BASE + "manifest.json", {cache: "no-cache"});
  if (!r.ok) throw new Error("HTTP " + r.status);
  manifest = await r.json();
  dataV = encodeURIComponent(manifest._meta.generated_at || "");
}
async function getEntry(id, canRetry = true) {
  if (entryCache.has(id)) return entryCache.get(id);
  const key = shardFor(id);
  if (key === null) return null;
  if (!shardCache.has(key)) {
    shardCache.set(key, fetch(BASE + key + ".json" + vq())
      .then(r => r.ok ? r.json().then(j => ({ok: true, j})) : {ok: false, j: {}})
      .catch(() => { shardCache.delete(key); return {ok: false, j: {}}; }));
  }
  const res = await shardCache.get(key);
  const e = res.j[id] || null;
  if (e === null && !res.ok && canRetry) {
    // the shard key vanished under us — the data version moved (nightly
    // rebuild / redeploy while this tab was open). Re-sync once and retry.
    try { await fetchManifest(); } catch { return null; }
    shardCache.clear();
    entryCache.clear();
    return getEntry(id, false);
  }
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
function activeProv() {
  const ks = new Set();
  document.querySelectorAll(".toolbar input[data-p]").forEach(cb => {
    if (cb.checked) ks.add(cb.dataset.p);
  });
  return ks;
}

// Provenance CLASS is what matters, not a vacuous high/medium/low: did a human
// write this link (Wikidata properties/claims, @[wikidata]/@[stacks] source
// attributes), did the Lean kernel certify it (dependencies, the file tree),
// or did an AI propose it (agent grounding, LLM-judged paper matches)?
function provClass(kind, prov, ev) {
  if (kind === "depends" || kind === "contains") return "machine";
  if (((prov && prov.method) || "").includes("@[")) return "human";
  if (ev && ev.source_tagged) return "human";   // gold pair reached via another path
  if (kind === "xref" || kind === "xref-shared" || kind === "relates") return "human";
  return "ai";
}
const PROV_TITLE = {
  human: "human-curated (Wikidata property/claim or a source attribute in Mathlib)",
  machine: "machine-verified (Lean kernel / file tree)",
  ai: "AI-generated (agent-proposed or LLM-judged), verified by oracle + skeptic",
};

// ============================ canvas state ==================================
let focusId = null;        // container id (or LIBS_ID) whose children fill the stage
let selectedId = null;     // node the panel shows / ring highlights
let layout = null;         // {items: Map(id -> {x,y,r,item}), root}
const svg = d3.select("#svg");
const gEdges = svg.append("g");
const gBubbles = svg.append("g");
const gOverlay = svg.append("g");
const gLabels = svg.append("g");

const CONCEPT_COLOR = {formalized: "#3b82f6", partial: "#eab308", not_formalized: "#ef4444"};
function fillFor(item, depthShade) {
  if (item.type === "concept") return CONCEPT_COLOR[item.status] || "#0969da";
  if (item.type === "decl") return "#22c55e";
  if (item.type === "strays") return "#8c959f";
  if (item.type === "external") return "#f472b6";
  if (item.type === "literature") return "#d4a72c";
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
  // leaf level: ghost decls — in the formal snapshot but not yet linked by any
  // brain edge. Rendered dimmer so a file's real contents are never invisible.
  if (!conts.length && e.ghosts && e.ghosts.first) {
    const lib = e.node.library || "Mathlib";
    for (const name of e.ghosts.first)
      decls.push({id: `decl:${lib}:${name}`, label: name, type: "decl", ghost: true});
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

// bubbles = containment-first (circle pack); web = edge-first (force layout of
// the SAME level, nodes shrunk and spread so the connections dominate and are
// easy to hit)
let viewMode = "bubbles";
try { viewMode = localStorage.getItem("wl-brain-view") || "bubbles"; } catch (e) {}

function drawNodes() {
  const leaves = layout.leaves;
  const shade = "#22304d";   // container fill — the canvas is always dark
  const bubbles = gBubbles.selectAll("circle.node").data(leaves, l => l.data.id);
  bubbles.exit().remove();
  const entered = bubbles.enter().append("circle")
    .attr("class", l => l.data.type === "container" ? "bubble node" : "dot node");
  entered.append("title");
  entered.merge(bubbles)
    .attr("cx", l => l.x).attr("cy", l => l.y)
    .attr("r", l => Math.max(l.r, 2.5))
    .attr("fill", l => fillFor(l.data, shade))
    .attr("fill-opacity", l => l.data.type === "container" ? 0.55 : l.data.ghost ? 0.35 : 0.9)
    .on("click", (ev, l) => { ev.stopPropagation(); nodeClick(l.data); })
    .select("title").text(l => l.data.label + (l.data.n_decls
      ? ` — ${l.data.n_decls.toLocaleString()} decls` : "")
      + (l.data.ghost ? " — in Mathlib, not yet linked in the brain" : ""));
}

function drawLabels() {
  gLabels.selectAll("*").remove();
  const web = viewMode === "web" || (layout && layout.ego);
  const ego = layout && layout.ego;
  for (const l of layout.leaves) {
    if (ego && l.data.type !== "container" && l.data.type !== "concept") {
      const raw = l.data.label || l.data.id;
      const short = l.data.type === "decl" && raw.includes(":")
        ? raw.split(":").pop().split(".").slice(-2).join(".") : raw;
      gLabels.append("text").attr("class", "blabel")
        .attr("x", l.x).attr("y", l.y + l.r + 10).attr("font-size", 9)
        .text(short.length > 28 ? short.slice(0, 26) + "…" : short);
      continue;
    }
    if (l.data.type === "container") {
      if (!web && l.r < 24) continue;
      const fs = web ? 10 : Math.max(10, Math.min(16, l.r / 4.5));
      gLabels.append("text").attr("class", "blabel")
        .attr("x", l.x).attr("y", web ? l.y + l.r + 11 : l.y - (l.r > 40 ? 4 : -4))
        .attr("font-size", fs).text(l.data.label);
      if (!web && l.r > 40) {
        gLabels.append("text").attr("class", "bcount")
          .attr("x", l.x).attr("y", l.y + fs - 2).attr("font-size", fs * 0.72)
          .text(`${(l.data.n_decls || 0).toLocaleString()}${
            l.data.n_concepts ? " · " + l.data.n_concepts + "★" : ""}`);
      }
    } else if (l.data.type === "concept" && l.data.label && !/^Q\d+$/.test(l.data.label)) {
      if (!web && l.r < 11) continue;
      gLabels.append("text").attr("class", "blabel")
        .attr("x", l.x).attr("y", l.y + Math.max(l.r, 3) + 10).attr("font-size", 9)
        .text(l.data.label.length > 26 ? l.data.label.slice(0, 24) + "…" : l.data.label);
    }
  }
}

// force layout over the CURRENT level using the just-built edge web; runs
// synchronously (200 ticks) once enrich() has the edges
function applyWebLayout() {
  if (viewMode !== "web" || !layout || !layout.leaves) return;
  const W = stageEl.clientWidth || 800, H = stageEl.clientHeight || 600;
  const leaves = layout.leaves;
  const sims = leaves.map(l => ({
    id: l.data.id, l,
    x: l.x, y: l.y,
    r: l.data.type === "container"
      ? 9 + 3.2 * Math.log2(1 + (l.data.n_decls || 1))
      : Math.min(Math.max(l.r, 3.5), 7),
  }));
  const byId = new Map(sims.map(s => [s.id, s]));
  const links = edgeStore
    .filter(e => byId.has(e.a) && byId.has(e.b))
    .map(e => ({source: e.a, target: e.b, w: e.w}));
  const sim = d3.forceSimulation(sims)
    .force("link", d3.forceLink(links).id(d => d.id)
      .distance(l => 70 + 50 / Math.sqrt(1 + l.w)).strength(0.4))
    .force("charge", d3.forceManyBody().strength(-140).distanceMax(420))
    .force("center", d3.forceCenter(W / 2, H / 2))
    .force("collide", d3.forceCollide(d => d.r + 8))
    .stop();
  for (let i = 0; i < 200; i++) sim.tick();
  for (const s of sims) {
    s.l.x = Math.max(s.r + 6, Math.min(W - s.r - 6, s.x));
    s.l.y = Math.max(s.r + 6, Math.min(H - s.r - 18, s.y));
    s.l.r = s.r;
  }
  drawNodes();
  drawLabels();
  renderEdges();
  drawSelRing();
}

// Ego view: a concept/decl/paper becomes the focus — it sits centered and
// EVERYTHING it links to expands around it (formalizing decls, related
// concepts, external database pages, papers, its home folders), laid out by a
// static force pass. Clicking a neighbor re-centers on it, so you can walk
// the Wikidata relation graph and the formal graph in one continuous motion.
// Same edgeStore pipeline as the level views: the Layers + Provenance toggles
// and edge evidence cards all apply.
const EGO_DIST = {formalizes: 95, relates: 135, xref: 160, depends: 175,
                  matches: 195, cites: 205, mentions: 215};
async function renderEgo(seq, entry, anim) {
  const id = entry.node.id;
  selectedId = id;
  const kinds = activeKinds(), provs = activeProv();
  const neigh = new Map();
  let skipped = 0;
  for (const dir of ["out", "in"]) {
    for (const x of (entry.edges && entry.edges[dir]) || []) {
      const pc = provClass(x.kind, manifest.prov[x.prov], x.evidence);
      const kindOk = x.kind === "xref" ? kinds.has("xref") : kinds.has(x.kind);
      if (!kindOk || !provs.has(pc)) continue;
      if (neigh.has(x.id)) continue;
      if (x.kind === "xref") {
        const key = x.id.split(":")[1];
        const mkUrl = XREF_URL[key];
        neigh.set(x.id, {id: x.id, type: "external",
          label: `${XREF_NAME[key] || key}: ${x.evidence ? x.evidence.value : ""}`,
          url: (mkUrl && x.evidence && mkUrl(x.evidence.value)) || null,
          edge: x, rank: SAT_RANK.relates + 0.5});
      } else {
        neigh.set(x.id, {id: x.id, type: idType(x.id), label: x.id,
          edge: x, rank: SAT_RANK[x.kind] ?? 9});
      }
    }
  }
  if (entry.node.article_annotations && entry.node.slug) {
    const aa = entry.node.article_annotations;
    neigh.set("article:" + entry.node.slug, {
      id: "article:" + entry.node.slug, type: "external",
      label: `WikiLean article — ${aa.total} annotations`,
      url: "/" + entry.node.slug,
      edge: {kind: "mentions", prov: 0,
             evidence: {role: "article", ...aa,
                        note: "the concept's annotated Wikipedia mirror"}},
      rank: -1});
  }
  let nodesArr = [...neigh.values()].sort((a, b) => a.rank - b.rank);
  if (nodesArr.length > 72) { skipped = nodesArr.length - 72; nodesArr = nodesArr.slice(0, 72); }

  const W = stageEl.clientWidth || 800, H = stageEl.clientHeight || 600;
  const R_BY_TYPE = {concept: 11, decl: 8, container: 13, literature: 7, external: 7};
  const center = {data: {id, label: entry.node.label || id, type: entry.node.type},
                  x: W / 2, y: H / 2, r: 24};
  const leaves = [center].concat(nodesArr.map(nd => ({
    data: nd, x: W / 2 + (Math.random() - 0.5), y: H / 2 + (Math.random() - 0.5),
    r: R_BY_TYPE[nd.type] || 8,
  })));
  const sims = leaves.map(l => ({id: l.data.id, l, x: l.x, y: l.y, r: l.r}));
  sims[0].fx = W / 2; sims[0].fy = H / 2;
  const links = nodesArr.map(nd => ({source: id, target: nd.id,
    dist: EGO_DIST[nd.edge.kind] || 180}));
  const sim = d3.forceSimulation(sims)
    .force("link", d3.forceLink(links).id(d => d.id).distance(l => l.dist).strength(0.6))
    .force("charge", d3.forceManyBody().strength(-170).distanceMax(420))
    .force("collide", d3.forceCollide(d => d.r + 11))
    .stop();
  for (let i = 0; i < 220; i++) sim.tick();
  for (const sm of sims) {
    sm.l.x = Math.max(sm.r + 6, Math.min(W - sm.r - 6, sm.x));
    sm.l.y = Math.max(sm.r + 6, Math.min(H - sm.r - 18, sm.y));
  }

  layout = {items: new Map(leaves.map(l => [l.data.id, l])), leaves, ego: true};
  edgeStore = nodesArr.map(nd => ({kind: nd.edge.kind, a: id, b: nd.id,
    w: (nd.edge.evidence && nd.edge.evidence.w_types && nd.edge.evidence.w_types.sig) || 1,
    payload: nd.edge}));
  gEdges.selectAll("*").remove();
  gOverlay.selectAll("*").remove();
  gBubbles.selectAll("circle.preview").remove();
  drawNodes();
  drawLabels();
  renderEdges();
  drawSelRing();
  renderCrumb();
  statusEl.textContent = `${nodesArr.length} linked nodes${
    skipped ? ` (+${skipped} more in the panel)` : ""} · ` +
    `${entry.node.type === "concept" ? id : entry.node.type} ego view`;
  if (anim) {
    const g = [gEdges, gBubbles, gOverlay, gLabels];
    for (const gr of g) gr.attr("opacity", 0).transition().duration(260).attr("opacity", 1);
    setTimeout(() => g.forEach(gr => { gr.interrupt(); gr.attr("opacity", 1); }), 600);
  }
  // resolve neighbor labels lazily (shared prefix shards — few fetches)
  let pending = 0;
  for (const nd of nodesArr) {
    if (nd.type === "external" || (nd.label && nd.label !== nd.id)) continue;
    pending++;
    getEntry(nd.id).then(ne => {
      if (seq !== renderSeq) return;
      if (ne && ne.node.label) nd.label = ne.node.label;
      else if (nd.type === "decl") nd.label = nd.id.split(":").pop().split(".").slice(-2).join(".");
      if (--pending <= 0) drawLabels();
    });
  }
  renderPanel(id);
}

let renderSeq = 0;   // guards against out-of-order async renders
async function renderFocus(anim) {
  const seq = ++renderSeq;
  if (focusId !== LIBS_ID) {
    const fe = await getEntry(focusId);
    if (seq !== renderSeq) return;
    if (fe && fe.node.type !== "container") return renderEgo(seq, fe, anim);
  }
  const items = await focusItems(focusId);
  if (seq !== renderSeq) return;
  const W = stageEl.clientWidth || 800, H = stageEl.clientHeight || 600;
  const root = d3.hierarchy({children: items}).sum(d => d.children ? 0 : packValue(d));
  d3.pack().size([W, H]).padding(items.length > 150 ? 1.5 : 4)(root);
  const leaves = root.leaves().filter(l => l.data.id);

  layout = {items: new Map(leaves.map(l => [l.data.id, l])), leaves: leaves};
  gEdges.selectAll("*").remove();
  gOverlay.selectAll("*").remove();
  gBubbles.selectAll("circle.preview").remove();
  drawNodes();
  drawLabels();

  // concept dots boot with their QID as label; resolve real labels lazily
  let pendingLabels = 0;
  for (const l of leaves) {
    if (l.data.type !== "concept") continue;
    pendingLabels++;
    getEntry(l.data.id).then(ce => {
      if (seq !== renderSeq || !ce || !ce.node.label) return;
      l.data.label = ce.node.label;
      gBubbles.selectAll("circle.node").filter(d => d.data.id === l.data.id)
        .select("title").text(ce.node.label);
      if (--pendingLabels <= 0) drawLabels();
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
  depends:      {color: "#a78bfa", dash: null,   label: "formal dependency"},
  formalizes:   {color: "#38bdf8", dash: null,   label: "formalizes (formal↔informal join)"},
  relates:      {color: "#fbbf24", dash: "5 3",  label: "Wikidata relation (informal)"},
  mentions:     {color: "#94a3b8", dash: "2 3",  label: "article mention (informal)"},
  "xref-shared":{color: "#f472b6", dash: "5 3",  label: "same external-database page"},
  xref:         {color: "#f472b6", dash: "3 3",  label: "cross-database identity"},
  cites:        {color: "#fb923c", dash: "2 4",  label: "stated in the literature (TheoremGraph)"},
  matches:      {color: "#2dd4bf", dash: null,   label: "formal ↔ literature match"},
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
    if (kids.length > 1 && l.r > 26 && viewMode !== "web") {
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
    // informal rollups: relates (Wikidata, human) + mentions (articles, AI)
    // aggregated between concept homes — the human/AI synapses between bubbles
    const inf = e.rollup && e.rollup.informal;
    if (inf) {
      for (const row of inf) {
        if (!visible.has(row.id)) continue;
        put(row.kind, l.data.id, row.id, row.count,
            {prov: row.prov, confidence: "high",
             evidence: {aggregated: true, count: row.count, sample_pairs: row.samples,
                        note: "concept-level " + row.kind + " flows between these areas"}});
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
  applyWebLayout();
  if (lastPanelId === focusId && !selectedId) renderPanel(focusId);
}

function liftOf(e) {
  return (e.payload && e.payload.evidence && e.payload.evidence.lift) || null;
}
function renderEdges() {
  gEdges.selectAll("*").remove();
  if (!layout) return;
  const kinds = activeKinds();
  const provs = activeProv();
  const dehub = $("#dehub").checked;
  const show = edgeStore.filter(e =>
    (e.kind === "xref-shared" ? kinds.has("xref") : kinds.has(e.kind))
    && provs.has(provClass(e.kind,
        e.payload && e.payload.prov !== undefined ? manifest.prov[e.payload.prov] : null,
        e.payload && e.payload.evidence)));
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
    // deterministic per-pair bend so parallel routes fan out instead of piling
    let h = 0;
    const hk = e.a + "|" + e.b + e.kind;
    for (let i = 0; i < hk.length; i++) h = (h * 31 + hk.charCodeAt(i)) >>> 0;
    const bend = (0.08 + (h % 1000) / 1000 * 0.22) * ((h & 1) ? 1 : -1);
    const d = `M${A.x},${A.y} Q${mx - dy * bend},${my + dx * bend} ${B.x},${B.y}`;
    const st = EDGE_STYLE[e.kind];
    const isDep = e.kind === "depends";
    const web = viewMode === "web";
    const baseOp = isDep ? Math.max(depOpacity(e), web ? 0.3 : 0) : web ? 0.65 : 0.5;
    const p = gEdges.append("path").attr("class", "link")
      .attr("d", d).attr("fill", "none")
      .attr("stroke", st.color)
      .attr("stroke-width", isDep ? 0.6 + 2.6 * Math.sqrt(e.w / maxSig)
            : 1 + Math.min(2.2, Math.log2(1 + e.w) * 0.5))
      .attr("stroke-opacity", baseOp);
    if (st.dash) p.attr("stroke-dasharray", st.dash);
    // invisible fat twin = the click/hover target
    gEdges.append("path").attr("class", "hit")
      .attr("d", d).attr("fill", "none")
      .attr("stroke", "transparent").attr("stroke-width", 14)
      .style("cursor", "pointer")
      .on("mouseenter", () => p.attr("stroke-opacity", 0.95)
        .attr("stroke-width", (isDep ? 0.6 + 2.6 * Math.sqrt(e.w / maxSig)
          : 1 + Math.min(2.2, Math.log2(1 + e.w) * 0.5)) + 1.4))
      .on("mouseleave", () => p.attr("stroke-opacity", baseOp)
        .attr("stroke-width", isDep ? 0.6 + 2.6 * Math.sqrt(e.w / maxSig)
          : 1 + Math.min(2.2, Math.log2(1 + e.w) * 0.5)))
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
  lastPanelId = "__edge__";
  const st = EDGE_STYLE[e.kind];
  const prov = (e.payload && e.payload.prov !== undefined && manifest.prov[e.payload.prov]) || null;
  const ev = (e.payload && e.payload.evidence) || {};
  const name = id => {
    const L = layout.items.get(id);
    return (L && L.data.label) || id;
  };
  const eprov = e.payload && e.payload.prov !== undefined ? manifest.prov[e.payload.prov] : null;
  const epc = provClass(e.kind, eprov, e.payload && e.payload.evidence);
  panelEl.innerHTML = `
    <h2 style="font-size:1.05rem">${esc(st.label)}</h2>
    <div class="sub"><span style="color:${st.color}">●</span> ${esc(e.kind)}
      · <span class="prov ${epc}" style="margin-left:0" title="${esc(PROV_TITLE[epc])}">${epc}</span>${
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
const OV_COLOR = {formalizes: "#38bdf8", xref: "#f472b6", cites: "#fb923c",
                  matches: "#2dd4bf", relates: "#fbbf24", mentions: "#94a3b8",
                  depends: "#a78bfa"};
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
  drawSatellites();
}

// Satellites: the selected node's OFF-CANVAS neighborhood — Wikidata relations,
// formal dependencies, formalizations and paper matches whose other endpoint
// lives elsewhere in the containment tree — orbiting the selection so the
// Wikidata graph, the Mathlib graph and the literature overlay in one view.
// Click a satellite to travel there. Filtered by the Layers + Provenance toggles.
const SAT_RANK = {relates: 0, formalizes: 1, matches: 2, depends: 3, cites: 4, mentions: 5};
function idType(id) {
  if (/^Q\d+$/.test(id)) return "concept";
  if (id.startsWith("decl:")) return "decl";
  if (id.startsWith("lit:")) return "literature";
  if (id.startsWith("path:")) return "container";
  return "external";
}
async function drawSatellites() {
  gOverlay.selectAll("g.sat").remove();
  if (!selectedId || !layout || layout.ego) return;
  const S = layout.items.get(selectedId);
  const e = await getEntry(selectedId);
  if (!S || !e) return;
  const kinds = activeKinds(), provs = activeProv();
  const seen = new Set();
  const cand = [];
  for (const dir of ["out", "in"]) {
    for (const x of (e.edges && e.edges[dir]) || []) {
      if (x.kind === "xref" || !kinds.has(x.kind)) continue;
      if (!provs.has(provClass(x.kind, manifest.prov[x.prov], x.evidence))) continue;
      if (layout.items.has(x.id) || seen.has(x.id)) continue;
      seen.add(x.id);
      cand.push({x, rank: SAT_RANK[x.kind] ?? 9});
    }
  }
  cand.sort((a, b) => a.rank - b.rank);
  const sats = cand.slice(0, 18);
  if (!sats.length) return;
  const W = stageEl.clientWidth || 800, H = stageEl.clientHeight || 600;
  const R = Math.max(S.r, 8) + 54;
  sats.forEach((s, i) => {
    const a = -Math.PI / 2 + i * 2 * Math.PI / sats.length;
    const sx = Math.max(16, Math.min(W - 16, S.x + R * Math.cos(a)));
    const sy = Math.max(16, Math.min(H - 22, S.y + R * Math.sin(a)));
    const st = EDGE_STYLE[s.x.kind] || {};
    const g = gOverlay.append("g").attr("class", "sat").style("cursor", "pointer")
      .on("click", ev => { ev.stopPropagation(); navigate(s.x.id); });
    const p = g.append("path").attr("fill", "none")
      .attr("d", `M${S.x},${S.y} L${sx},${sy}`)
      .attr("stroke", st.color || "#57606a")
      .attr("stroke-width", 1.4).attr("stroke-opacity", 0.75);
    if (st.dash) p.attr("stroke-dasharray", st.dash);
    const t = /^Q\d+$/.test(s.x.id) ? "concept"
      : s.x.id.startsWith("decl:") ? "decl"
      : s.x.id.startsWith("lit:") ? "literature" : "container";
    g.append("circle").attr("cx", sx).attr("cy", sy).attr("r", 7)
      .attr("fill", t === "concept" ? "#0969da" : t === "decl" ? "#1a7f37"
            : t === "literature" ? "#bf5af2" : "#8250df")
      .attr("fill-opacity", 0.92).attr("stroke", "#fff").attr("stroke-width", 1.2);
    const label = g.append("text").attr("class", "blabel")
      .attr("x", sx).attr("y", sy + 17).attr("font-size", 9)
      .text(s.x.id.length > 22 ? s.x.id.slice(0, 20) + "…" : s.x.id);
    getEntry(s.x.id).then(se => {
      if (se && se.node.label && se.node.label !== s.x.id)
        label.text(se.node.label.length > 26 ? se.node.label.slice(0, 24) + "…" : se.node.label);
    });
    g.append("title").text(`${s.x.kind} · ${s.x.id} — click to open`);
  });
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
  if (e && e.node.type !== "container") {     // leaving an ego view
    const home = await homeContainerOf(e);
    const ego = focusId;
    focusId = home;
    selectedId = ego;                         // keep it ringed at its home level
    history.replaceState(null, "", "#" + encodeURIComponent(home));
    await renderFocus(true);
    return;
  }
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
  if (item.ghost) {               // snapshot decl with no brain edges yet
    ghostPanel(item);
    return;
  }
  if (item.type === "container") {
    selectedId = null;
    renderPanel(item.id);
    await zoomInto(item.id);
    return;
  }
  if (item.type === "external") {          // ego xref pseudo-node → the DB page
    if (item.url) window.open(item.url, "_blank", "noopener");
    return;
  }
  // concept/decl/paper: zoom in and expand its whole neighborhood (ego view)
  await zoomInto(item.id);
}

// land the canvas on any node id: containers focus themselves; leaves focus
// their parent container and select themselves
async function navigate(id) {
  const e = await getEntry(id);
  if (!e) { renderPanel(id); return; }
  focusId = id;                    // containers → level view; others → ego view
  selectedId = e.node.type === "container" ? null : id;
  history.replaceState(null, "", "#" + encodeURIComponent(id));
  renderPanel(id);
  await renderFocus(true);
}

// the container an ego node calls home (for zoom-out + the breadcrumb)
async function homeContainerOf(entry) {
  if (entry.breadcrumb && entry.breadcrumb.length > 1)
    return entry.breadcrumb[entry.breadcrumb.length - 2].id;
  if (entry.node.type === "concept") {
    const f = ((entry.edges || {}).out || []).find(x => x.kind === "formalizes");
    const fe = f && await getEntry(f.id);
    if (fe && fe.node.type === "container") return f.id;
    if (fe && fe.breadcrumb && fe.breadcrumb.length > 1)
      return fe.breadcrumb[fe.breadcrumb.length - 2].id;
  }
  return "path:Mathlib";
}

async function renderCrumb() {
  let html = `<a data-nav="${LIBS_ID}">all libraries</a>`;
  if (focusId !== LIBS_ID) {
    const e = await getEntry(focusId);
    if (e && e.node.type !== "container") {   // ego view: home chain + ● node
      const home = await homeContainerOf(e);
      const he = await getEntry(home);
      for (const b of (he && he.breadcrumb) || [])
        html += ` <span class="sep">/</span> <a data-nav="${esc(b.id)}">${esc(b.label)}</a>`;
      html += ` <span class="sep">/</span> <b>● ${esc(e.node.label || focusId)}</b>`;
    } else {
      for (const b of (e && e.breadcrumb) || []) {
        html += ` <span class="sep">/</span> ` + (b.id === focusId
          ? `<b>${esc(b.label)}</b>` : `<a data-nav="${esc(b.id)}">${esc(b.label)}</a>`);
      }
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
  lmfdb_knowl: "LMFDB", oeis: "OEIS", dlmf: "DLMF", msc: "MSC",
  stacks: "Stacks Project", kerodon: "Kerodon"};
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
  stacks: v => `https://stacks.math.columbia.edu/tag/${encodeURIComponent(v)}`,
  kerodon: v => `https://kerodon.net/tag/${encodeURIComponent(v)}`,
  msc: () => null,
};
function nodeUrl(id) {
  if (id.startsWith("decl:")) return "/decl/" + encodeURIComponent(id.slice(id.indexOf(":", 5) + 1));
  if (/^Q\d+$/.test(id)) return `https://www.wikidata.org/wiki/${id}`;
  if (id.startsWith("lit:")) {
    const ax = id.slice(4).split("#")[0];
    if (/^[A-Za-z.-]+\/\d{7}(v\d+)?$/.test(ax) || !ax.includes("/"))
      return `https://arxiv.org/abs/${ax}`;
    return `https://github.com/${ax}`;
  }
  return null;
}
// "field" as a match_kind chip beside an algebra QID reads like the Field
// concept — spell it out
const MK_LABEL = {field: "field-of-study link"};
function edgeHtml(x, provTable, dir) {
  const ev = x.evidence || {};
  const mkv = ev.match_kind && (MK_LABEL[ev.match_kind] || ev.match_kind);
  let mk = mkv ? `<span class="mk">${esc(mkv)}</span>` : "";
  if (ev.n_annotations > 1) mk += ` <span class="lit-ref">×${ev.n_annotations} annotations</span>`;
  const arrow = dir === "in" ? "←" : "→";
  let target = esc(x.id);
  if (x.kind === "xref" && ev.value !== undefined) {
    const mkUrl = XREF_URL[x.id.split(":")[1]];
    const url = mkUrl && mkUrl(ev.value);
    const lbl = `${esc(XREF_NAME[x.id.split(":")[1]] || x.id.split(":")[1])}: ${esc(ev.value)}`;
    target = url ? `<a href="${esc(url)}" rel="noopener" target="_blank">${lbl}</a>` : lbl;
  } else {
    const u = nodeUrl(x.id);
    target = `<span class="nav" data-nav="${esc(x.id)}" style="color:#1a4b8f;cursor:pointer">${target}</span>`
           + (u ? ` <a class="extlink" href="${esc(u)}" rel="noopener" target="_blank">↗</a>` : "");
  }
  const prov = provTable[x.prov] || {};
  const pc = provClass(x.kind, prov, ev);
  const drawer = `provenance: <b>${esc(prov.source)}</b> · ${esc(prov.method)} · pin ${esc(prov.pin)}
<pre>${esc(JSON.stringify(ev, null, 1))}</pre>`;
  return `<div class="edge"><div class="row">${arrow} ${target} ${mk}
    <span class="prov ${pc}" title="${esc(PROV_TITLE[pc])}">${pc}${ev.skeptic === "pending" ? " · unreviewed" : ""}${ev.source_tagged ? " · @[wikidata]" : ""}</span></div>
    <div class="drawer">${drawer}</div></div>`;
}
let lastPanelId = null;
let lastLevelEdges = [];
async function renderPanel(id) {
  lastPanelId = id;
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
  if (n.type === "container" && n.qid) sub.push(`<a href="https://www.wikidata.org/wiki/${esc(n.qid)}" rel="noopener" target="_blank">Wikidata ${esc(n.qid)}</a>`);
  if (n.type === "decl") sub.push(`<a href="${esc(nodeUrl(n.id))}" rel="noopener" target="_blank">${esc(n.library || "Mathlib")} docs ↗</a>`);
  html += `<div class="sub">${sub.join(" · ")}</div>`;
  // "Also in" — every external identity of this concept as one chip strip
  // (the /map concept-panel affordance): article, Wikidata, Google KG, and
  // each cross-referenced database, deep-linked
  if (n.type === "concept") {
    const chips = [];
    if (n.slug) chips.push(`<a href="/${esc(n.slug)}">WikiLean article</a>`);
    if (n.slug) chips.push(`<a href="https://en.wikipedia.org/wiki/${esc(n.slug)}" rel="noopener" target="_blank">Wikipedia</a>`);
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
  if (n.article_annotations) {
    const aa = n.article_annotations;
    html += `<div class="chips"><span class="chip"><a href="/${esc(n.slug)}">WikiLean article</a>:
      <b>${aa.total}</b> Lean annotations</span>
      <span class="badge f">${aa.formalized} formalized</span>
      <span class="badge p">${aa.partial} partial</span>
      <span class="badge n">${aa.not_formalized} not</span></div>`;
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
  if (n.slogan) html += `<div class="slogan">${esc(n.slogan)}<span class="src">slogan — math-graph (CC-BY-4.0)</span></div>`;
  if (n.code) {
    html += `<div class="codeblock"><pre>${esc(n.code)}</pre><span class="src">${
      esc(n.decl_kind || "decl")} — mathlib4 source (Apache-2.0) · ` +
      `<a href="${esc(nodeUrl(n.id))}" rel="noopener" target="_blank">${esc(n.library || "Mathlib")} docs ↗</a></span></div>`;
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
  const provs = activeProv();
  const groups = new Map();
  for (const dir of ["out", "in"]) {
    for (const x of (e.edges && e.edges[dir]) || []) {
      if (!kinds.has(x.kind)) continue;
      if (!provs.has(provClass(x.kind, prov[x.prov], x.evidence))) continue;
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

  // the focused container also lists the strongest connections AMONG its
  // children — the easy way to pick an edge that is hard to hit on canvas
  if (n.type === "container" && id === focusId && layout) {
    const short = nid => {
      const L = layout.items.get(nid);
      if (L && L.data.label && !/^Q\d+$/.test(L.data.label)) return L.data.label;
      return nid.startsWith("path:") ? nid.split("/").slice(-1)[0] : nid;
    };
    lastLevelEdges = edgeStore
      .filter(e2 => layout.items.has(e2.a) && layout.items.has(e2.b)
        && (e2.kind === "xref-shared" ? kinds.has("xref") : kinds.has(e2.kind))
        && provs.has(provClass(e2.kind,
            e2.payload && e2.payload.prov !== undefined ? manifest.prov[e2.payload.prov] : null,
            e2.payload && e2.payload.evidence)))
      .sort((x, y) => y.w - x.w).slice(0, 15);
    if (lastLevelEdges.length) {
      html += `<section class="kind"><h3>Connections at this level
        <span class="cnt">(strongest ${lastLevelEdges.length})</span></h3>`;
      lastLevelEdges.forEach((e2, i) => {
        const st2 = EDGE_STYLE[e2.kind] || {};
        html += `<div class="edge"><div class="row" data-lvledge="${i}">
          <span style="color:${st2.color}">●</span>
          <span>${esc(short(e2.a))} ↔ ${esc(short(e2.b))}</span>
          <span class="mk">${esc(st2.label || e2.kind)}</span>${
          e2.kind === "depends" ? ` <span class="lit-ref">sig ${e2.w}</span>` : ""}</div></div>`;
      });
      html += `</section>`;
    }
  }
  panelEl.innerHTML = html;
  panelEl.querySelectorAll("[data-nav]").forEach(a =>
    a.addEventListener("click", () => navigate(a.dataset.nav)));
  panelEl.querySelectorAll(".edge .row").forEach(r =>
    r.addEventListener("click", ev => {
      if (ev.target.closest("a") || ev.target.closest("[data-nav]")) return;
      if (r.dataset.lvledge !== undefined) {
        showEdgePanel(lastLevelEdges[Number(r.dataset.lvledge)]);
        return;
      }
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
  // concept rows likewise boot as bare QIDs — show the human label
  // (a "← Q3968 field-of-study link" row must read "Algebra", not "field")
  [...panelEl.querySelectorAll('[data-nav]')].filter(el => /^Q\d+$/.test(el.dataset.nav))
    .slice(0, 40).forEach(async el => {
      const qe = await getEntry(el.dataset.nav);
      if (!qe || !qe.node.label) return;
      el.innerHTML = `${esc(qe.node.label)} <span class="lit-ref">${esc(el.dataset.nav)}</span>`;
    });
}

// ghost decls have no brain node — the panel explains and links out instead
// of erroring with "Unknown node"
function ghostPanel(item) {
  lastPanelId = "__ghost__";
  const name = item.label;
  const mod = focusId.startsWith("path:")
    ? focusId.slice(5).replaceAll("/", ".") : "";
  panelEl.innerHTML = `
    <h2 style="font-size:1.1rem">${esc(name)}</h2>
    <div class="sub">decl${mod ? " · " + esc(mod) : ""} ·
      <a href="/decl/${encodeURIComponent(name)}" rel="noopener" target="_blank">docs ↗</a></div>
    <span class="badge">not yet linked</span>
    <p class="note" style="margin-top:10px">This declaration exists in the formal
    snapshot, but no brain edge reaches it yet — no concept formalizes it, no
    article cites it, no judged paper match. It renders dimmed so the file's
    real contents stay visible. Links grow through the discovery pipeline
    (<code>brain/proposals/</code>) or a WikiLean annotation citing it.</p>`;
}

// ---- the transparency legend: /map's Sources view, rendered in the panel ----
let sourcesData = null;
async function showSourcesPanel() {
  lastPanelId = "__sources__";
  if (!sourcesData) {
    const r = await fetch(BASE + "sources.json" + vq());
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
$("#srcbtn2").addEventListener("click", showSourcesPanel);

// ============================ search =========================================
async function ensureLabels() {
  if (!labels) {
    const r = await fetch(BASE + "labels.json" + vq());
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
    if (layout && layout.ego) { renderFocus(false); return; }
    renderEdges();
    drawSelRing();
    if (selectedId) renderPanel(selectedId);
    else if (focusId !== LIBS_ID) renderPanel(focusId);
  }));

document.querySelectorAll('input[name="vm"]').forEach(r => {
  r.checked = r.value === viewMode;
  r.addEventListener("change", () => {
    viewMode = r.value;
    try { localStorage.setItem("wl-brain-view", viewMode); } catch (e) {}
    renderFocus(false);
  });
});

window.addEventListener("hashchange", () => {
  const id = decodeURIComponent(location.hash.slice(1));
  if (id) navigate(id);
});
window.addEventListener("resize", () => { renderFocus(false); });

(async function boot() {
  // ?embed=1 → chrome-less mode for the landing-page iframe: hide the header +
  // crumb bar, article/external links escape the frame
  if (new URLSearchParams(location.search).has("embed")) {
    document.body.classList.add("embed");
    const base = document.createElement("base");
    base.target = "_parent";
    document.head.appendChild(base);
  }
  try {
    await fetchManifest();
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
