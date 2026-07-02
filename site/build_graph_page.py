#!/usr/bin/env python3
"""Generate the standalone concept-graph viewer page.

Reads catalog/data/concept_graph.json and writes:
    out/graph.html       — viewer in WikiLean's chrome (wl-header), GitHub palette
    out/graph_data.json  — copy of the merged graph data the viewer fetches

Run alongside build_index.py / export_wikidata_rdf.py / build_static_pages.py.
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "out"
DATA_SRC = HERE.parent / "catalog" / "data" / "concept_graph.json"
# QIDs whose Wikidata↔Mathlib mapping is a merged @[wikidata] tag in Mathlib
# master — the deterministic, human-reviewed ground truth. The bot keeps this
# list current as batches land.
BOT_TAGGED = HERE.parent / "bot" / "data" / "tagged_in_master.txt"
# Live per-article formalization coverage (the manage/ control plane computes it
# from the annotation layer; grows nightly as moderation formalizes). Optional —
# the graph still builds without it.
COVERAGE = HERE.parent / "manage" / "data" / "coverage.json"
# External-database crossrefs per QID (MathWorld, nLab, ProofWiki, Metamath,
# LMFDB knowl, OEIS, …) — the one-SPARQL backfill from the Wikidata hub
# (catalog/mathlib_deps/fetch_crossrefs.py, refreshed nightly). Optional.
CROSSREFS = HERE.parent / "catalog" / "data" / "wikidata_crossrefs.json"


HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WikiLean — Concept graph</title>
<meta name="description" content="Side-by-side dependency graph for Wikipedia mathematics concepts: edges from Mathlib's formal dependency graph overlaid on edges from Wikidata's typed relations.">
<script>(function(){try{var s=localStorage.getItem("wl-theme");var t=s==="dark"||s==="light"?s:(window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light");document.documentElement.dataset.theme=t;}catch(e){}})();</script>
<style>
* { box-sizing:border-box; }
html, body { height:100%; }
body { margin:0; background:#fafbfc; color:#1f2328;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
.wl-header { background:#fff; border-bottom:1px solid #d0d7de; padding:14px 28px;
  display:flex; align-items:center; justify-content:space-between; }
.wl-brand { font-weight:700; color:#0969da; font-size:18px; text-decoration:none; }
.wl-nav { display:flex; gap:18px; align-items:center; }
.wl-navlink { color:#0969da; text-decoration:none; font-size:.9rem; }
.wl-navlink:hover { text-decoration:underline; }
.wl-navlink.active { color:#1f2328; }
.wl-theme-toggle { background:transparent; border:1px solid #d0d7de; color:#57606a;
  border-radius:50%; width:28px; height:28px; padding:0; line-height:1; font-size:14px;
  cursor:pointer; display:inline-flex; align-items:center; justify-content:center; margin-left:10px; }
[data-theme="dark"] .wl-theme-toggle { color:#9a9081; border-color:#4d4742; }

#app {
  display:grid; grid-template-columns: 260px 1fr 320px;
  grid-template-rows: 1fr;
  height: calc(100vh - 53px); min-height: 0;
}
aside { padding:18px 18px; background:#fff; overflow-y:auto; min-height:0; font-size:.92rem; color:#1f2328; }
#side { border-right:1px solid #d0d7de; }
#info { border-left:1px solid #d0d7de; }
canvas { display:block; width:100%; height:100%; cursor:grab; background:#fafbfc; }
canvas.dragging { cursor:grabbing; }
h2 { font-size:.72rem; text-transform:uppercase; letter-spacing:.06em; margin:18px 0 6px;
  color:#57606a; font-weight:600; }
h2:first-child { margin-top:0; }
label.row { display:flex; align-items:center; gap:8px; padding:3px 0; cursor:pointer; user-select:none; }
label.row .swatch { display:inline-block; width:14px; height:4px; border-radius:2px; }
input[type="text"] { width:100%; padding:7px 10px; border:1px solid #d0d7de; border-radius:6px;
  font:inherit; color:#1f2328; background:#fff; }
input[type="text"]:focus { outline:none; border-color:#0969da; box-shadow:0 0 0 2px rgba(9,105,218,.18); }
.stat { display:flex; justify-content:space-between; padding:2px 0; }
.stat .v { font-variant-numeric:tabular-nums; color:#57606a; }
.hint { font-size:.78rem; color:#57606a; line-height:1.55; }
#info h3 { font-size:1.05rem; margin:0 0 4px; }
#info .qid { color:#57606a; font-size:.82rem; margin-bottom:10px; }
#info code { background:#f0f0f0; padding:1px 5px; border-radius:3px; font-size:.85rem; }
#info a { color:#0969da; text-decoration:none; }
#info a:hover { text-decoration:underline; }
#info .field { margin:4px 0; font-size:.9rem; }
#info .field b { color:#57606a; font-weight:600; }
#info .xref-chip { display:inline-block; padding:1px 8px; margin:1px 2px; border:1px solid #d0d7de;
  border-radius:10px; font-size:.75rem; text-decoration:none; color:#0969da; background:#f6f8fa; }
#info .xref-chip:hover { border-color:#0969da; }
#info .links { display:flex; flex-direction:column; gap:4px; margin-top:14px; padding-top:14px; border-top:1px solid #d0d7de; }
.empty { color:#8c959f; font-style:italic; }

/* Dark mode — shared palette (bg #1a1816, surface #232020, text #ebe5d8,
   muted #9a9081, accent #6e9adf, borders #4d4742). Canvas bg + node/label
   colors that read poorly on dark are also handled in JS via dataset.theme. */
[data-theme="dark"] body { background:#1a1816; color:#ebe5d8; }
[data-theme="dark"] .wl-header { background:#232020; border-bottom-color:#4d4742; }
[data-theme="dark"] .wl-brand { color:#6e9adf; }
[data-theme="dark"] .wl-navlink { color:#6e9adf; }
[data-theme="dark"] .wl-navlink.active { color:#ebe5d8; }
[data-theme="dark"] aside { background:#232020; color:#ebe5d8; }
[data-theme="dark"] #side { border-right-color:#4d4742; }
[data-theme="dark"] #info { border-left-color:#4d4742; }
[data-theme="dark"] canvas { background:#1a1816; }
[data-theme="dark"] h2 { color:#9a9081; }
[data-theme="dark"] input[type="text"] { background:#1a1816; color:#ebe5d8; border-color:#4d4742; }
[data-theme="dark"] input[type="text"]:focus { border-color:#6e9adf; box-shadow:0 0 0 2px rgba(110,154,223,.25); }
[data-theme="dark"] .stat .v { color:#9a9081; }
[data-theme="dark"] .hint { color:#9a9081; }
[data-theme="dark"] #info h3 { color:#ebe5d8; }
[data-theme="dark"] #info .qid { color:#9a9081; }
[data-theme="dark"] #info code { background:#2c2926; color:#ebe5d8; }
[data-theme="dark"] #info a { color:#6e9adf; }
[data-theme="dark"] #info .field b { color:#9a9081; }
[data-theme="dark"] #info .links { border-top-color:#4d4742; }
[data-theme="dark"] .empty { color:#9a9081; }
</style>
</head>
<body>
<header class="wl-header">
  <a class="wl-brand" href="/">WikiLean</a>
  <nav class="wl-nav">
    <a class="wl-navlink" href="/concepts">Concepts</a>
    <a class="wl-navlink" href="/article-graph">Article graph</a>
    <a class="wl-navlink active" href="/graph">Concept graph</a>
    <a class="wl-navlink" href="/about">About &amp; method</a>
    <button id="wl-theme-toggle" class="wl-theme-toggle" type="button" aria-label="Toggle dark mode" title="Toggle dark mode">\U0001f313</button>
  </nav>
</header>
<div id="app">
  <aside id="side">
    <h2>Edge sources</h2>
    <label class="row"><input type="checkbox" id="show-mathlib"><span class="swatch" style="background:#2e6fab"></span> Mathlib only <span class="hint" style="margin-left:auto">12k</span></label>
    <label class="row"><input type="checkbox" id="show-wikidata" checked><span class="swatch" style="background:#c78420"></span> Wikidata only</label>
    <label class="row"><input type="checkbox" id="show-both" checked><span class="swatch" style="background:#2da44e"></span> Both (overlap)</label>

    <h2>Human-reviewed</h2>
    <label class="row"><input type="checkbox" id="verified-only"><span class="swatch" style="width:11px;height:11px;border-radius:50%;background:transparent;border:2px solid #8250df"></span> Only merged <code>@[wikidata]</code> tags <span class="hint" id="verified-count" style="margin-left:auto"></span></label>
    <p class="hint">The deterministic subset — Wikidata↔Mathlib links a maintainer merged into Mathlib (ringed ○). Everything else is an AI-proposed mapping.</p>

    <h2>Node colour</h2>
    <label class="row"><input type="checkbox" id="color-coverage"> Colour by Mathlib coverage</label>
    <p class="hint">Nodes shade <span style="color:#d1242f">red</span>→<span style="color:#d4a72c">amber</span>→<span style="color:#2da44e">green</span> by the share of the concept's Wikipedia statements formalized in Mathlib. Refreshed nightly.</p>

    <h2>Search</h2>
    <input type="text" id="search" placeholder="Concept label…" autocomplete="off">

    <h2>Stats</h2>
    <div id="stats"></div>

    <h2>About this view</h2>
    <p class="hint">Each node is a Wikidata concept (QID) the catalog has linked to a Mathlib declaration. Mathlib edges = one decl's type/value directly references another (via <code>Expr.getUsedConstants</code>). Wikidata edges = a direct-claim statement (P279, P361, P31, …) between two of these QIDs. <a href="/about">More →</a></p>

    <p class="hint">Drag to pan · scroll to zoom · click a node for details.</p>
  </aside>
  <canvas id="canvas"></canvas>
  <aside id="info">
    <p class="empty">Click a node to inspect.</p>
  </aside>
</div>
<script src="https://cdn.jsdelivr.net/npm/d3@7"></script>
<script>
const isDark = () => document.documentElement.dataset.theme === 'dark';
const COLORS = {
  mathlib: '#2e6fab',
  wikidata: '#c78420',
  both: '#2da44e',
  highlight: '#cf222e',
  verified: '#8250df',
  nodeFormalized: '#2da44e',
  nodeUnformalized: '#d0d7de',
  nodeDefault: '#57606a',
};
// On dark, lift the muted node colors and the node-label ink so they stay legible.
function nodeUnformalizedColor() { return isDark() ? '#4d4742' : COLORS.nodeUnformalized; }
function nodeDefaultColor() { return isDark() ? '#9a9081' : COLORS.nodeDefault; }
function labelColor() { return isDark() ? '#ebe5d8' : '#1f2328'; }

(async () => {
  const data = await (await fetch('graph_data.json')).json();

  const edgeMap = new Map();
  for (const e of data.edges) {
    const k = e.from + '>' + e.to;
    let bucket = edgeMap.get(k);
    if (!bucket) { bucket = { source: e.from, target: e.to, sources: new Set() }; edgeMap.set(k, bucket); }
    bucket.sources.add(e.source);
  }
  const validEdges = [];
  for (const e of edgeMap.values()) {
    const inMl = e.sources.has('mathlib');
    const inWd = e.sources.has('wikidata');
    e.cat = (inMl && inWd) ? 'both' : (inMl ? 'mathlib' : 'wikidata');
    validEdges.push(e);
  }

  const allNodesById = new Map();
  for (const n of data.nodes) allNodesById.set(n.qid, { ...n, id: n.qid });
  const allNodes = [...allNodesById.values()];
  const allEdges = validEdges.filter(e => allNodesById.has(e.source) && allNodesById.has(e.target));
  const nVerified = allNodes.reduce((a, n) => a + (n.verified ? 1 : 0), 0);

  // Active set — swapped by the human-reviewed filter, so these are reassignable.
  let nodes = allNodes, edges = allEdges, nodeById = allNodesById;
  let edgesByCat = { mathlib: [], wikidata: [], both: [] };
  function bucketEdges() {
    edgesByCat = { mathlib: [], wikidata: [], both: [] };
    for (const e of edges) edgesByCat[e.cat].push(e);
  }
  bucketEdges();

  const canvas = document.getElementById('canvas');
  const ctx = canvas.getContext('2d');
  let cssW = 0, cssH = 0;
  const resize = () => {
    const r = canvas.getBoundingClientRect();
    cssW = r.width; cssH = r.height;
    canvas.width = cssW * devicePixelRatio;
    canvas.height = cssH * devicePixelRatio;
    ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
    scheduleDraw();
  };

  let scale = 0.9, tx = 0, ty = 0, centered = false;
  const screenToWorld = (sx, sy) => [(sx - tx) / scale, (sy - ty) / scale];

  const sim = d3.forceSimulation(nodes)
    .force('link', d3.forceLink(edges).id(d => d.id).distance(28).strength(0.35))
    .force('charge', d3.forceManyBody().strength(-22).distanceMax(380))
    .force('center', d3.forceCenter(0, 0))
    .force('collide', d3.forceCollide(4))
    .alpha(1).alphaDecay(0.025).velocityDecay(0.4)
    .on('tick', scheduleDraw);

  const show = { mathlib: false, wikidata: true, both: true };
  let highlighted = null;
  let searchTerm = '';
  let verifiedOnly = false;
  let colorByCoverage = false;

  const endpointId = x => (x && typeof x === 'object') ? x.id : x;
  // Swap the active node/edge set to the human-reviewed subgraph (or back) and
  // re-run the layout so the ~97 verified nodes cluster instead of scattering.
  function applyFilter() {
    if (verifiedOnly) {
      nodeById = new Map([...allNodesById].filter(([, n]) => n.verified));
      nodes = [...nodeById.values()];
      edges = allEdges.filter(e => nodeById.has(endpointId(e.source)) && nodeById.has(endpointId(e.target)));
      // The verified subgraph is mostly Mathlib edges (few, and meaningful here),
      // so surface them even though they're hidden on the full graph by default.
      if (!show.mathlib) { show.mathlib = true; document.getElementById('show-mathlib').checked = true; }
    } else {
      nodeById = allNodesById; nodes = allNodes; edges = allEdges;
    }
    bucketEdges();
    if (highlighted && !nodeById.has(highlighted.id)) { highlighted = null; renderInfo(); }
    sim.nodes(nodes);
    sim.force('link').links(edges);
    sim.alpha(0.9).restart();
    renderStats();
    scheduleDraw();
  }

  // External-database mirrors (from the Wikidata crossref backfill). Formatter
  // URLs match the Wikidata property formatters; 'mathlib' (P14534) routes
  // through our own resolver so reverse citations ride along.
  const XREF_DBS = {
    mathlib:     { label: 'Mathlib',    url: 'https://wikilean.jackmccarthy.org/decl/$1' },
    mathworld:   { label: 'MathWorld',  url: 'https://mathworld.wolfram.com/$1.html' },
    nlab:        { label: 'nLab',       url: 'https://ncatlab.org/nlab/show/$1' },
    proofwiki:   { label: 'ProofWiki',  url: 'https://proofwiki.org/wiki/$1' },
    metamath:    { label: 'Metamath',   url: 'https://us.metamath.org/mpeuni/$1.html' },
    lmfdb_knowl: { label: 'LMFDB',      url: 'https://www.lmfdb.org/knowledge/show/$1' },
    oeis:        { label: 'OEIS',       url: 'https://oeis.org/$1' },
    eom:         { label: 'EoM',        url: 'https://encyclopediaofmath.org/wiki/$1' },
    planetmath:  { label: 'PlanetMath', url: 'https://planetmath.org/$1' },
    dlmf:        { label: 'DLMF',       url: 'https://dlmf.nist.gov/$1' },
    msc:         { label: 'MSC',        url: 'https://zbmath.org/classification/?q=cc%3A$1' },
    kgmid:       { label: 'Google',     url: 'https://www.google.com/search?kgmid=$1' },
  };
  const covStops = [[209, 36, 47], [212, 167, 44], [45, 164, 78]]; // red → amber → green
  function coverageColor(c) {
    const x = Math.max(0, Math.min(1, c)) * 2, i = Math.min(1, Math.floor(x)), t = x - i;
    const A = covStops[i], B = covStops[i + 1], l = k => Math.round(A[k] + (B[k] - A[k]) * t);
    return `rgb(${l(0)},${l(1)},${l(2)})`;
  }
  function nodeColor(n, matched) {
    if (matched) return COLORS.highlight;
    if (colorByCoverage && n.coverage != null) return coverageColor(n.coverage);
    if (n.status === 'formalized') return COLORS.nodeFormalized;
    if (!n.primary_decl) return nodeUnformalizedColor();
    return nodeDefaultColor();
  }
  function matchesSearch(n) {
    return searchTerm && (n.label || '').toLowerCase().includes(searchTerm);
  }

  let needsDraw = false;
  function scheduleDraw() {
    if (needsDraw) return;
    needsDraw = true;
    requestAnimationFrame(() => { needsDraw = false; draw(); });
  }

  function draw() {
    if (!centered) { tx = cssW / 2; ty = cssH / 2; centered = true; }
    ctx.clearRect(0, 0, cssW, cssH);
    ctx.save();
    ctx.translate(tx, ty);
    ctx.scale(scale, scale);
    ctx.lineCap = 'round';
    const inv = 1 / scale;

    // Viewport bounds in world coords for cheap edge culling.
    const vL = -tx * inv, vT = -ty * inv;
    const vR = (cssW - tx) * inv, vB = (cssH - ty) * inv;

    const order = ['mathlib', 'wikidata', 'both'];
    for (const cat of order) {
      if (!show[cat]) continue;
      ctx.strokeStyle = COLORS[cat];
      ctx.globalAlpha = cat === 'both' ? 0.78 : 0.28;
      ctx.lineWidth = (cat === 'both' ? 1.0 : 0.55) * inv;
      ctx.beginPath();
      const bucket = edgesByCat[cat];
      for (let i = 0; i < bucket.length; i++) {
        const e = bucket[i];
        const x1 = e.source.x, y1 = e.source.y;
        const x2 = e.target.x, y2 = e.target.y;
        // Skip edges with bbox entirely off-screen on one side.
        if ((x1 < vL && x2 < vL) || (x1 > vR && x2 > vR) ||
            (y1 < vT && y2 < vT) || (y1 > vB && y2 > vB)) continue;
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
      }
      ctx.stroke();
    }
    ctx.globalAlpha = 1;

    if (highlighted) {
      ctx.strokeStyle = COLORS.highlight;
      ctx.lineWidth = 1.6 * inv;
      ctx.beginPath();
      for (const e of edges) {
        if (e.source.id === highlighted.id || e.target.id === highlighted.id) {
          ctx.moveTo(e.source.x, e.source.y);
          ctx.lineTo(e.target.x, e.target.y);
        }
      }
      ctx.stroke();
    }

    for (const n of nodes) {
      const matched = matchesSearch(n);
      const isHl = n === highlighted;
      const r = (isHl ? 6 : matched ? 4.5 : 2.4) * inv;
      ctx.fillStyle = nodeColor(n, matched);
      ctx.beginPath();
      ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
      ctx.fill();
      // Ring the human-reviewed nodes so they're identifiable even unfiltered.
      if (n.verified && !isHl && !matched) {
        ctx.strokeStyle = COLORS.verified;
        ctx.lineWidth = 1.2 * inv;
        ctx.beginPath();
        ctx.arc(n.x, n.y, r + 1.7 * inv, 0, Math.PI * 2);
        ctx.stroke();
      }
    }

    if (highlighted) {
      ctx.fillStyle = labelColor();
      ctx.font = (12 * inv) + 'px -apple-system, sans-serif';
      ctx.textBaseline = 'middle';
      ctx.fillText(' ' + (highlighted.label || highlighted.id), highlighted.x + 7 * inv, highlighted.y);
    }
    ctx.restore();
  }

  let panning = false, panStart = null;
  canvas.addEventListener('mousedown', ev => {
    panning = true; canvas.classList.add('dragging');
    panStart = [ev.clientX - tx, ev.clientY - ty];
  });
  window.addEventListener('mousemove', ev => {
    if (!panning) return;
    tx = ev.clientX - panStart[0];
    ty = ev.clientY - panStart[1];
    scheduleDraw();
  });
  window.addEventListener('mouseup', () => { panning = false; canvas.classList.remove('dragging'); });
  canvas.addEventListener('wheel', ev => {
    ev.preventDefault();
    const r = canvas.getBoundingClientRect();
    const mx = ev.clientX - r.left, my = ev.clientY - r.top;
    const [wx, wy] = screenToWorld(mx, my);
    const factor = Math.exp(-ev.deltaY * 0.0015);
    scale = Math.max(0.05, Math.min(20, scale * factor));
    tx = mx - wx * scale;
    ty = my - wy * scale;
    scheduleDraw();
  }, { passive:false });

  canvas.addEventListener('click', ev => {
    const r = canvas.getBoundingClientRect();
    const [wx, wy] = screenToWorld(ev.clientX - r.left, ev.clientY - r.top);
    const pickR = 8 / scale;
    let best = null, bestD2 = pickR * pickR;
    for (const n of nodes) {
      const d2 = (n.x - wx) ** 2 + (n.y - wy) ** 2;
      if (d2 < bestD2) { bestD2 = d2; best = n; }
    }
    highlighted = best;
    renderInfo();
    scheduleDraw();
  });

  const info = document.getElementById('info');
  function renderInfo() {
    if (!highlighted) {
      info.innerHTML = '<p class="empty">Click a node to inspect.</p>';
      return;
    }
    const n = highlighted;
    const counts = { mathlib: 0, wikidata: 0, both: 0 };
    for (const e of edges) {
      if (e.source.id === n.id || e.target.id === n.id) counts[e.cat]++;
    }
    const wikiUrl = n.slug ? `https://en.wikipedia.org/wiki/${encodeURIComponent(n.slug)}` : null;
    const wdUrl = `https://www.wikidata.org/wiki/${n.qid}`;
    const wlUrl = n.slug ? `/${encodeURIComponent(n.slug)}` : null;
    const docsUrl = n.primary_decl
      ? `https://leanprover-community.github.io/mathlib4_docs/find/?pattern=${encodeURIComponent(n.primary_decl)}`
      : null;

    const parts = [];
    parts.push(`<h3>${esc(n.label || n.qid)}</h3>`);
    parts.push(`<div class="qid">${n.qid}${n.importance ? ' · ' + esc(n.importance) : ''}${n.status ? ' · ' + esc(n.status) : ''}</div>`);
    if (n.verified) parts.push(`<div class="field" style="color:${COLORS.verified}"><b style="color:${COLORS.verified}">✓ Human-reviewed</b> · merged <code>@[wikidata ${esc(n.qid)}]</code> in Mathlib</div>`);
    if (n.primary_decl) parts.push(`<div class="field"><b>Decl</b> · <code>${esc(n.primary_decl)}</code></div>`);
    if (n.module) parts.push(`<div class="field"><b>Module</b> · <code>${esc(n.module)}</code></div>`);
    if (n.coverage != null) parts.push(`<div class="field"><b>Coverage</b> · ${Math.round(n.coverage * 100)}% formalized <span style="color:#57606a">(${n.n_formalized}/${n.n_status} statements)</span></div>`);
    if (n.xrefs) {
      const chips = Object.entries(XREF_DBS).flatMap(([key, db]) =>
        (n.xrefs[key] || []).slice(0, 3).map(id =>
          `<a class="xref-chip" href="${db.url.replace('$1', encodeURIComponent(id))}" target="_blank" rel="noopener" title="${db.label}: ${esc(id)}">${db.label}</a>`));
      if (chips.length) parts.push(`<div class="field"><b>Also in</b> · ${chips.join(' ')}</div>`);
    }
    parts.push(`<div class="field" style="margin-top:10px"><b>Edges</b> · <span style="color:${COLORS.mathlib}">${counts.mathlib} mathlib</span> · <span style="color:${COLORS.wikidata}">${counts.wikidata} wikidata</span> · <span style="color:${COLORS.both}">${counts.both} both</span></div>`);
    parts.push('<div class="links">');
    if (wlUrl) parts.push(`<a href="${wlUrl}">WikiLean article →</a>`);
    if (wikiUrl) parts.push(`<a href="${wikiUrl}" target="_blank" rel="noopener">Wikipedia →</a>`);
    parts.push(`<a href="${wdUrl}" target="_blank" rel="noopener">Wikidata →</a>`);
    if (docsUrl) parts.push(`<a href="${docsUrl}" target="_blank" rel="noopener">Mathlib docs →</a>`);
    parts.push('</div>');
    info.innerHTML = parts.join('');
  }
  function esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));
  }

  document.getElementById('show-mathlib').addEventListener('change', e => { show.mathlib = e.target.checked; scheduleDraw(); });
  document.getElementById('show-wikidata').addEventListener('change', e => { show.wikidata = e.target.checked; scheduleDraw(); });
  document.getElementById('show-both').addEventListener('change', e => { show.both = e.target.checked; scheduleDraw(); });
  document.getElementById('verified-only').addEventListener('change', e => { verifiedOnly = e.target.checked; applyFilter(); });
  document.getElementById('color-coverage').addEventListener('change', e => { colorByCoverage = e.target.checked; scheduleDraw(); });
  document.getElementById('search').addEventListener('input', e => {
    searchTerm = e.target.value.toLowerCase().trim();
    scheduleDraw();
  });

  const statsEl = document.getElementById('stats');
  function renderStats() {
    const counts = { mathlib: 0, wikidata: 0, both: 0 };
    for (const e of edges) counts[e.cat]++;
    statsEl.innerHTML = `
      <div class="stat"><span>Nodes</span><span class="v">${nodes.length.toLocaleString()}</span></div>
      <div class="stat"><span>Mathlib only</span><span class="v">${counts.mathlib.toLocaleString()}</span></div>
      <div class="stat"><span>Wikidata only</span><span class="v">${counts.wikidata.toLocaleString()}</span></div>
      <div class="stat"><span>Both</span><span class="v">${counts.both.toLocaleString()}</span></div>`;
  }
  renderStats();
  const vcEl = document.getElementById('verified-count');
  if (vcEl) vcEl.textContent = nVerified.toLocaleString();

  resize();
  window.addEventListener('resize', resize);

  // Theme toggle — flip dataset.theme, persist, and redraw the canvas so its
  // theme-dependent node/label colors update without a reload.
  const themeBtn = document.getElementById('wl-theme-toggle');
  if (themeBtn) themeBtn.addEventListener('click', () => {
    const r = document.documentElement;
    const n = r.dataset.theme === 'dark' ? 'light' : 'dark';
    r.dataset.theme = n;
    try { localStorage.setItem('wl-theme', n); } catch (e) {}
    scheduleDraw();
  });
})();
</script>
</body>
</html>
"""


def load_verified_qids() -> set[str]:
    if not BOT_TAGGED.exists():
        return set()
    return {l.strip() for l in BOT_TAGGED.read_text().splitlines() if l.strip().startswith("Q")}


def load_coverage() -> dict:
    # Coverage is optional enrichment — a missing/empty/corrupt file must degrade
    # to "no coverage", never crash the graph build (the verified subgraph and the
    # rest of the graph don't depend on it).
    if not COVERAGE.exists():
        return {}
    try:
        return json.loads(COVERAGE.read_text()).get("by_slug", {})
    except (ValueError, OSError):
        return {}


def load_crossrefs() -> dict:
    # Same optional-enrichment contract as coverage: absent/corrupt → no chips.
    if not CROSSREFS.exists():
        return {}
    try:
        return json.loads(CROSSREFS.read_text()).get("xrefs", {})
    except (ValueError, OSError):
        return {}


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "graph.html").write_text(HTML)
    # Stamp `verified` on nodes backed by a merged @[wikidata] tag, `coverage`
    # (live formalized share of the concept's article), and `xrefs` (external-
    # database ids from the Wikidata hub) — so the viewer can filter to the
    # human-reviewed subgraph, colour by coverage, and deep-link each concept's
    # mirrors across the math-database ecosystem.
    verified = load_verified_qids()
    coverage = load_coverage()
    crossrefs = load_crossrefs()
    data = json.loads(DATA_SRC.read_text())
    nv = ncov = nx = 0
    for node in data.get("nodes", []):
        if node.get("qid") in verified:
            node["verified"] = True
            nv += 1
        c = coverage.get(node.get("slug"))
        if c and c.get("n_status", 0) > 0:
            node["coverage"] = c["coverage"]
            node["n_status"] = c["n_status"]
            node["n_formalized"] = c["n_formalized"]
            ncov += 1
        x = crossrefs.get(node.get("qid"))
        if x:
            node["xrefs"] = x
            nx += 1
    (OUT_DIR / "graph_data.json").write_text(json.dumps(data, ensure_ascii=False))
    size = (OUT_DIR / "graph_data.json").stat().st_size
    print(f"Wrote out/graph.html and out/graph_data.json ({size / 1024 / 1024:.1f} MB); "
          f"{nv} human-reviewed nodes, {ncov} with live coverage, {nx} with crossrefs")


if __name__ == "__main__":
    main()
