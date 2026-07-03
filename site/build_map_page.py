#!/usr/bin/env python3
"""Generate /map — the unified map of mathematics (bubbles + web + sources).

One page, one artifact (/map_data.json), three views over the SAME node
identities:
  · Bubbles — zoomable circle packing (continents → subfields → concepts),
              the containment view (formerly /atlas).
  · Web     — force-directed dependency graph coloured by continent, the
              relational view (formerly /graph).
  · Sources — the transparency legend: every external database WikiLean links
              to, its layer, our provenance, and its license.

A formal↔informal layer filter and a shared detail panel span all three, so a
concept selected in one view carries into the others. Replaces /graph and
/atlas (both now redirect here). Run: python3 site/build_map_page.py
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "out"

HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WikiLean — Map of mathematics</title>
<meta name="description" content="A unified map of mathematics: zoom through domains and subfields into concepts, follow the dependency web between them, and see every concept's Mathlib formalization status and cross-database identity — with transparent provenance for every link.">
<script>(function(){try{var s=localStorage.getItem("wl-theme");var t=s==="dark"||s==="light"?s:(window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light");document.documentElement.dataset.theme=t;}catch(e){}})();</script>
<style>
* { box-sizing:border-box; }
html, body { height:100%; }
body { margin:0; background:#fafbfc; color:#1f2328;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
.wl-header { background:#fff; border-bottom:1px solid #d0d7de; padding:12px 24px;
  display:flex; align-items:center; justify-content:space-between; gap:16px; flex-wrap:wrap; }
.wl-brand { font-weight:700; color:#0969da; font-size:18px; text-decoration:none; }
.wl-nav { display:flex; gap:16px; align-items:center; }
.wl-navlink { color:#0969da; text-decoration:none; font-size:.9rem; }
.wl-navlink:hover { text-decoration:underline; }
.wl-navlink.active { color:#1f2328; }
.wl-theme-toggle { background:transparent; border:1px solid #d0d7de; color:#57606a;
  border-radius:50%; width:28px; height:28px; padding:0; font-size:14px; cursor:pointer; }
/* segmented view switch */
.seg { display:inline-flex; border:1px solid #d0d7de; border-radius:8px; overflow:hidden; }
.seg button { border:0; background:#fff; color:#57606a; padding:6px 14px; font:inherit;
  font-size:.86rem; cursor:pointer; border-right:1px solid #d0d7de; }
.seg button:last-child { border-right:0; }
.seg button.on { background:#0969da; color:#fff; }
.lyr { display:inline-flex; gap:6px; align-items:center; font-size:.82rem; color:#57606a; }
.lyr select { font:inherit; font-size:.82rem; padding:4px 6px; border:1px solid #d0d7de;
  border-radius:6px; background:#fff; color:#1f2328; }
#app { display:grid; grid-template-columns: 1fr 340px; height: calc(100vh - 54px); min-height:0; }
#stage { position:relative; min-width:0; overflow:hidden; }
svg#svg { display:block; width:100%; height:100%; cursor:pointer; }
#canvas { display:block; width:100%; height:100%; }
.viewwrap { position:absolute; inset:0; }
.viewwrap[hidden] { display:none; }
#sourcesView { overflow-y:auto; padding:22px 26px; background:#fafbfc; }
#info { border-left:1px solid #d0d7de; background:#fff; overflow-y:auto; padding:18px; font-size:.92rem; }
#info h3 { font-size:1.05rem; margin:0 0 4px; }
#info .crumb { color:#57606a; font-size:.8rem; margin-bottom:10px; }
#info .field { margin:5px 0; font-size:.9rem; }
#info .field b { color:#57606a; font-weight:600; }
#info a { color:#0969da; text-decoration:none; }
#info a:hover { text-decoration:underline; }
#info code { background:#f0f0f0; padding:1px 5px; border-radius:3px; font-size:.85rem; }
#search { position:absolute; top:12px; left:12px; width:250px; padding:7px 10px; z-index:5;
  border:1px solid #d0d7de; border-radius:6px; font:inherit; background:#fff; color:#1f2328; }
#search:focus { outline:none; border-color:#0969da; box-shadow:0 0 0 2px rgba(9,105,218,.18); }
#weblegend { position:absolute; bottom:12px; left:12px; z-index:5; background:#ffffffcc;
  border:1px solid #d0d7de; border-radius:8px; padding:8px 11px; font-size:.76rem; color:#57606a;
  backdrop-filter:blur(3px); max-width:230px; }
#weblegend label { display:inline-flex; gap:5px; align-items:center; margin-right:9px; cursor:pointer; }
.hint { font-size:.78rem; color:#57606a; line-height:1.55; }
.empty { color:#8c959f; font-style:italic; }
circle.bubble { stroke:#fff0; transition: stroke .15s; }
circle.bubble:hover { stroke:#0969da; stroke-width:1.5px; }
text.blabel { pointer-events:none; text-anchor:middle;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; fill:#1f2328; }
.chip { display:inline-block; padding:1px 8px; margin:2px 3px 2px 0; border:1px solid #d0d7de;
  border-radius:999px; font-size:.78rem; text-decoration:none; color:#0969da; }
.chip:hover { border-color:#0969da; }
.chip .ly { font-size:.62rem; color:#8c959f; text-transform:uppercase; letter-spacing:.04em; margin-left:4px; }
.wl-desc { font-size:.9rem; color:#3d4450; line-height:1.5; margin:2px 0 10px; }
.rels { margin-top:5px; display:flex; flex-wrap:wrap; gap:5px; }
a.rel { display:inline-block; padding:2px 9px; border:1px solid #d0d7de; border-radius:999px;
  font-size:.8rem; text-decoration:none; color:#1f2328; background:#f6f8fa; cursor:pointer; }
a.rel:hover { border-color:#0969da; color:#0969da; }
[data-theme="dark"] .wl-desc { color:#c9c2b4; }
[data-theme="dark"] a.rel { background:#2c2926; border-color:#4d4742; color:#ebe5d8; }
.litrow { margin:6px 0; font-size:.86rem; }
.lithint { color:#57606a; font-size:.78rem; line-height:1.4; margin-top:1px; }
.lit-b { font-size:.62rem; text-transform:uppercase; letter-spacing:.04em; padding:1px 6px; border-radius:999px; margin-left:5px; }
.lit-exact { background:#2da44e22; color:#1a7f37; }
.lit-inexact { background:#9a670022; color:#9a6700; }
[data-theme="dark"] .lithint { color:#b8b0a2; }
[data-theme="dark"] .lit-exact { color:#4ac26b; }
[data-theme="dark"] .lit-inexact { color:#d4a72c; }
/* sources view */
.src-intro { max-width:760px; margin:0 0 20px; line-height:1.6; }
.src-intro h2 { margin:0 0 8px; font-size:1.3rem; }
.layer-group { margin:0 0 22px; }
.layer-group > h3 { font-size:1rem; margin:0 0 4px; display:flex; align-items:center; gap:8px; }
.layer-badge { font-size:.66rem; text-transform:uppercase; letter-spacing:.05em; color:#fff;
  padding:2px 8px; border-radius:999px; }
.layer-desc { color:#57606a; font-size:.84rem; margin:0 0 10px; }
.src-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:12px; }
.src-card { border:1px solid #d0d7de; border-radius:10px; padding:12px 14px; background:#fff; }
.src-card h4 { margin:0 0 6px; font-size:.98rem; }
.src-card h4 a { color:#0969da; text-decoration:none; }
.src-card .kv { font-size:.8rem; color:#57606a; margin:3px 0; line-height:1.45; }
.src-card .kv b { color:#1f2328; font-weight:600; }
.src-card .prov { font-size:.8rem; margin:6px 0 0; line-height:1.5; }
.src-note { font-size:.76rem; color:#9a6700; margin-top:6px; font-style:italic; }
[data-theme="dark"] body { background:#1a1816; color:#ebe5d8; }
[data-theme="dark"] .wl-header { background:#232020; border-bottom-color:#4d4742; }
[data-theme="dark"] .wl-brand, [data-theme="dark"] .wl-navlink { color:#6e9adf; }
[data-theme="dark"] .wl-navlink.active { color:#ebe5d8; }
[data-theme="dark"] .seg { border-color:#4d4742; }
[data-theme="dark"] .seg button { background:#232020; color:#b8b0a2; border-right-color:#4d4742; }
[data-theme="dark"] .seg button.on { background:#2f6bc0; color:#fff; }
[data-theme="dark"] .lyr select { background:#1a1816; color:#ebe5d8; border-color:#4d4742; }
[data-theme="dark"] #info { background:#232020; border-left-color:#4d4742; color:#ebe5d8; }
[data-theme="dark"] #info code { background:#2c2926; }
[data-theme="dark"] #info a, [data-theme="dark"] .src-card h4 a, [data-theme="dark"] .chip { color:#6e9adf; }
[data-theme="dark"] #search, [data-theme="dark"] .lyr select { background:#1a1816; color:#ebe5d8; border-color:#4d4742; }
[data-theme="dark"] #sourcesView { background:#1a1816; }
[data-theme="dark"] .src-card, [data-theme="dark"] #weblegend { background:#232020; border-color:#4d4742; }
[data-theme="dark"] #weblegend { background:#232020cc; }
[data-theme="dark"] .src-card .kv b { color:#ebe5d8; }
[data-theme="dark"] text.blabel { fill:#ebe5d8; }
</style>
</head>
<body>
<header class="wl-header">
  <a class="wl-brand" href="/">WikiLean</a>
  <div class="seg" id="viewswitch" role="tablist">
    <button data-view="bubbles" class="on">Bubbles</button>
    <button data-view="web">Web</button>
    <button data-view="sources">Sources</button>
  </div>
  <span class="lyr">Layer
    <select id="layerfilter">
      <option value="all">all</option>
      <option value="formal">formal</option>
      <option value="informal">informal</option>
    </select>
  </span>
  <nav class="wl-nav">
    <a class="wl-navlink" href="/concepts">Concepts</a>
    <a class="wl-navlink active" href="/map">Map</a>
    <a class="wl-navlink" href="/about">About &amp; method</a>
    <button id="wl-theme-toggle" class="wl-theme-toggle" type="button" aria-label="Toggle dark mode" title="Toggle dark mode">🌓</button>
  </nav>
</header>
<div id="app">
  <div id="stage">
    <input id="search" type="text" placeholder="Find a concept… (e.g. Module)" autocomplete="off">
    <div class="viewwrap" id="bubblesView"><svg id="svg"></svg></div>
    <div class="viewwrap" id="webView" hidden><canvas id="canvas"></canvas>
      <div id="weblegend"></div>
    </div>
    <div class="viewwrap" id="sourcesView" hidden></div>
  </div>
  <aside id="info"></aside>
</div>
<script src="https://cdn.jsdelivr.net/npm/d3@7"></script>
<script>
(async function () {
  const data = await (await fetch('/map_data.json')).json();
  const byQid = new Map(data.nodes.map(n => [n.qid, n]));
  const contColor = Object.fromEntries(data.continents.map(c => [c.key, c.color]));
  const contLabel = Object.fromEntries(data.continents.map(c => [c.key, c.label]));
  const srcByKey = Object.fromEntries(data.sources.sources.map(s => [s.key, s]));
  const edgeLayer = data.sources.edge_layer;               // {mathlib:'formal', wikidata:'bridge'}
  const info = document.getElementById('info');
  const stage = document.getElementById('stage');
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const dark = () => document.documentElement.dataset.theme === 'dark';
  let selectedQid = null, view = 'bubbles', layer = 'all';

  // ============================ shared detail panel ========================
  const XREF_URL = Object.fromEntries(data.sources.sources
    .filter(s => s.url_template).map(s => [s.key, s.url_template]));
  function xrefHref(key, id) {
    const t = XREF_URL[key]; if (!t) return null;
    return t.replace('{id}', encodeURIComponent(id).replace(/%2F/g, '/'));
  }
  function conceptPanel(qid) {
    const n = byQid.get(qid); if (!n) { info.innerHTML = ''; return; }
    const chips = [];
    for (const [db, ids] of Object.entries(n.xrefs || {})) {
      const s = srcByKey[db]; if (!s) continue;
      if (layer !== 'all' && s.layer !== layer) continue;   // dim chips outside the active layer
      for (const id of ids) {
        const href = xrefHref(db, id);
        chips.push(`<a class="chip" href="${esc(href)}" target="_blank" rel="noopener">${esc(s.name)}<span class="ly">${esc(s.layer)}</span></a>`);
      }
    }
    // arXiv literature layer (TheoremGraph). Hidden under the 'formal' filter
    // (these are informal/literature links). Primary-decl match first.
    const arx = (layer !== 'formal' && n.arxiv) ? n.arxiv : [];
    const arxHtml = arx.length ? (
      `<div class="field" style="margin-top:12px"><b>Stated in the literature</b> ` +
      `<span class="hint">via TheoremGraph</span>` +
      arx.slice(0, 8).map(a => {
        const badge = a.gpt54 === 'exact'
          ? `<span class="lit-b lit-exact">exact</span>`
          : `<span class="lit-b lit-inexact">inexact</span>`;
        return `<div class="litrow"><a href="https://arxiv.org/abs/${esc(a.arxiv_id)}" target="_blank" rel="noopener">arXiv:${esc(a.arxiv_id)}</a>` +
          `${a.ref ? ` · Thm ${esc(a.ref)}` : ''} ${badge}` +
          `<div class="lithint">${esc(a.title)}${a.primary ? '' : ` · <code>${esc(a.decl)}</code>`}</div></div>`;
      }).join('') +
      (arx.length > 8 ? `<div class="hint">+${arx.length - 8} more</div>` : '') +
      `</div>`) : '';
    const rel = (n.related || []).length
      ? `<div class="field" style="margin-top:12px"><b>Related concepts</b><div class="rels">` +
        n.related.map(r => `<a href="#" class="rel" data-qid="${esc(r.qid)}">${esc(r.label)}</a>`).join('') +
        `</div></div>`
      : '';
    const rows = [
      `<h3>${esc(n.label)}</h3>`,
      `<div class="crumb">${esc(contLabel[n.continent] || n.continent)} › ${esc((data.subfields[n.subfield]||{}).label || n.subfield)}</div>`,
      n.description ? `<div class="wl-desc">${esc(n.description)}</div>` : '',
      `<div class="field"><b>Status</b> · ${esc(n.status)}${n.coverage != null ? ` · ${Math.round(n.coverage * 100)}% coverage` : ''}</div>`,
      n.primary_decl ? `<div class="field"><b>Mathlib</b> · <code>${esc(n.primary_decl)}</code></div>` : '',
      n.n_conjectures ? `<div class="field" style="color:#c78420"><b>Conjecture frontier</b> · ${n.n_conjectures} statement${n.n_conjectures === 1 ? '' : 's'}</div>` : '',
      n.verified ? `<div class="field" style="color:#8250df"><b>✓ Human-reviewed</b> mapping</div>` : '',
      `<div class="field"><b>Placed by</b> · <code>${esc(n.assign_rule)}</code></div>`,
      chips.length ? `<div class="field" style="margin-top:10px"><b>Also in</b><br>${chips.join('')}</div>` : '',
      arxHtml,
      rel,
      `<div class="field" style="margin-top:12px">` +
        (n.wl_article
          ? `<a href="/${encodeURIComponent(n.slug)}">WikiLean article →</a><br>`
          : (n.slug ? `<a href="https://en.wikipedia.org/wiki/${encodeURIComponent(n.slug)}" target="_blank" rel="noopener">Wikipedia →</a><br>` : '')) +
        `<a href="https://www.wikidata.org/wiki/${esc(qid)}" target="_blank" rel="noopener">Wikidata ${esc(qid)} →</a><br>` +
        `<a href="#" id="crossview">${view === 'web' ? 'Show in bubbles →' : 'Show in web →'}</a></div>`,
    ];
    info.innerHTML = rows.join('');
    const cv = document.getElementById('crossview');
    if (cv) cv.onclick = (e) => { e.preventDefault(); setView(view === 'web' ? 'bubbles' : 'web'); focusConcept(qid); };
    info.querySelectorAll('a.rel').forEach(a => a.onclick = (e) => { e.preventDefault();
      if (view === 'sources') setView('bubbles');
      focusConcept(a.dataset.qid); });
  }
  function selectConcept(qid) { selectedQid = qid; conceptPanel(qid); }

  // ============================ BUBBLES (circle pack) ======================
  const svg = d3.select('#svg');
  const superBySub = {};
  for (const sn of data.supernodes) (superBySub[sn.subfield] ||= []).push(sn);
  const inSuper = new Set(data.supernodes.flatMap(sn => sn.members));
  const rootData = { name: 'mathematics', kind: 'root', children: data.continents.map(c => ({
    name: c.label, kind: 'continent', key: c.key, color: c.color,
    children: c.subfields.map(sk => {
      const sf = data.subfields[sk]; const kids = [];
      for (const sn of (superBySub[sk] || []))
        kids.push({ name: sn.decl, kind: 'supernode', decl: sn.decl, children: sn.members.map(leaf) });
      for (const q of sf.qids) if (!inSuper.has(q)) kids.push(leaf(q));
      return { name: sf.label, kind: 'subfield', key: sk, children: kids };
    }).filter(s => s.children.length),
  })).filter(c => c.children.length) };
  function leaf(q) { return { name: (byQid.get(q)||{}).label || q, kind: 'concept', qid: q, value: 1 }; }
  const root = d3.hierarchy(rootData).sum(d => d.value || 0).sort((a, b) => b.value - a.value);
  let W = stage.clientWidth || 800, H = stage.clientHeight || 600;   // guard 0-size containers (d3.pack NaNs on a zero dimension)
  d3.pack().size([W, H]).padding(3)(root);
  const statusFill = d => {
    const n = byQid.get(d.data.qid); if (!n) return '#0000';
    if (layer === 'formal' && n.status !== 'formalized') return dark() ? '#3a352f' : '#e6e0d6';
    if (n.status === 'formalized') return '#2da44e';
    return dark() ? '#57534d' : '#c9d1d9';
  };
  const bubbleFill = d =>
    d.data.kind === 'continent' ? d.data.color + (dark() ? '33' : '22')
    : d.data.kind === 'subfield' ? (dark() ? '#ffffff10' : '#ffffff88')
    : d.data.kind === 'supernode' ? (dark() ? '#6e9adf22' : '#0969da14')
    : statusFill(d);
  const g = svg.append('g');
  let focus = root, vview;
  const node = g.selectAll('circle').data(root.descendants().slice(1)).join('circle')
    .attr('class', 'bubble').attr('fill', bubbleFill)
    .attr('stroke', d => { const n = byQid.get(d.data.qid);
      if (n?.verified) return '#8250df'; if (n?.frontier || n?.n_conjectures) return '#c78420'; return null; })
    .attr('stroke-dasharray', d => { const n = byQid.get(d.data.qid); return (n?.frontier || n?.n_conjectures) ? '3,2' : null; })
    .attr('stroke-width', 1.2)
    .on('click', (event, d) => {
      event.stopPropagation();
      if (d.data.kind === 'concept') selectConcept(d.data.qid); else bubbleInfo(d);
      if (d.children && focus !== d) zoom(d);
      else if (!d.children && focus !== d.parent) zoom(d.parent);
    });
  const label = g.selectAll('text').data(root.descendants().slice(1)).join('text')
    .attr('class', 'blabel').style('display', 'none').text(d => d.data.name);
  svg.on('click', () => { if (focus.parent) { zoom(focus.parent); bubbleInfo(focus.parent); } });
  function zoomTo(v) {
    const k = Math.min(W, H) / v[2]; vview = v;
    node.attr('transform', d => `translate(${(d.x - v[0]) * k + W / 2},${(d.y - v[1]) * k + H / 2})`).attr('r', d => d.r * k);
    label.attr('transform', d => `translate(${(d.x - v[0]) * k + W / 2},${(d.y - v[1]) * k + H / 2})`);
    relabel(k);
  }
  function relabel(k) {
    label.style('display', d => (d.parent === focus && d.r * k > 14) || (d === focus && !d.children) ? null : 'none')
      .style('font-size', d => Math.max(9, Math.min(15, d.r * k / 3.2)) + 'px');
  }
  function zoom(d) {
    focus = d;
    svg.transition().duration(500).tween('zoom', () => {
      const i = d3.interpolateZoom(vview, [focus.x, focus.y, focus.r * 2.05]); return tt => zoomTo(i(tt));
    });
  }
  function bubbleInfo(d) {
    if (d === root || !d.data) return;
    const k = d.data.kind;
    const crumb = d.ancestors().reverse().slice(1).map(a => esc(a.data.name)).join(' › ');
    if (k === 'supernode') {
      info.innerHTML = `<h3><code>${esc(d.data.decl)}</code></h3><div class="crumb">${crumb}</div>` +
        `<div class="field">One Mathlib declaration realizing <b>${d.children.length}</b> distinct concepts:</div>` +
        d.children.map(c => `<div class="field">· ${esc(c.data.name)}</div>`).join('') +
        `<div class="field" style="margin-top:10px"><a href="/decl/${encodeURIComponent(d.data.decl)}" target="_blank" rel="noopener">View declaration →</a></div>`;
    } else if (k === 'subfield') {
      const pairs = data.bubble_edges.subfield_pairs.filter(p => p.a === d.data.key || p.b === d.data.key).slice(0, 8);
      info.innerHTML = `<h3>${esc(d.data.name)}</h3><div class="crumb">${crumb}</div>` +
        `<div class="field"><b>Concepts</b> · ${d.leaves().length}</div>` +
        `<div class="field" style="margin-top:8px"><b>Most connected to</b></div>` +
        (pairs.length ? pairs.map(p => { const o = p.a === d.data.key ? p.b : p.a;
          return `<div class="field">· ${esc((data.subfields[o]||{}).label || o)} <span class="hint">(${p.count} deps)</span></div>`; }).join('')
          : '<div class="empty">no cross-subfield edges</div>');
    } else if (k === 'continent') {
      const pairs = data.bubble_edges.continent_pairs.filter(p => p.a === d.data.key || p.b === d.data.key).slice(0, 6);
      info.innerHTML = `<h3>${esc(d.data.name)}</h3>` +
        `<div class="field"><b>Concepts</b> · ${d.leaves().length} in ${d.children.length} subfields</div>` +
        `<div class="field" style="margin-top:8px"><b>Most connected to</b></div>` +
        (pairs.length ? pairs.map(p => { const o = p.a === d.data.key ? p.b : p.a;
          return `<div class="field">· ${esc(contLabel[o] || o)} <span class="hint">(${p.count} deps)</span></div>`; }).join('')
          : '<div class="empty">no cross-continent edges</div>');
    }
  }
  function bubblesIntro() {
    info.innerHTML = `<h3>Map of mathematics</h3>` +
      `<p class="hint">Domains → subfields → concepts, each bubble sized by its contents. ` +
      `Click a bubble to zoom in; click the background to zoom out. Leaf colours: ` +
      `<span style="color:#2da44e">formalized</span> · <span style="color:#8c959f">not yet</span> · ` +
      `dashed <span style="color:#c78420">amber</span> = conjecture frontier · ` +
      `<span style="color:#8250df">purple ring</span> = human-reviewed mapping.</p>` +
      `<p class="hint">Assignment is deterministic and provenance-tagged (Mathlib module root → ` +
      `MSC → AMS code → neighbour vote → unsorted); each concept shows which rule placed it. ` +
      `Switch to <b>Web</b> for the dependency graph, or <b>Sources</b> for where every link comes from.</p>`;
  }
  function bubblesInit() {
    // Re-entering the view (e.g. after switching to Web/Sources and back) must not reset
    // `focus` to root — `focus` drives both label visibility (relabel) and the background-click
    // zoom-out target, and is otherwise only ever changed by zoom(d). Re-derive the view vector
    // from the current `focus`, mirroring the window-resize handler below.
    zoomTo([focus.x, focus.y, focus.r * 2.05]);
    if (selectedQid) conceptPanel(selectedQid); else bubblesIntro();
  }
  const bubbleLeaves = root.leaves();
  function focusBubble(qid) {
    const hit = bubbleLeaves.find(l => l.data.qid === qid);
    if (hit) { zoom(hit.parent); selectConcept(qid); }
  }

  // ============================ WEB (force graph) ==========================
  const COLORS = { both:'#8250df', mathlib:'#57606a', wikidata:'#0969da', highlight:'#e3116c',
    formalized:'#2da44e', unformalized:'#c9d1d9', deflt:'#8c959f' };
  const canvas = document.getElementById('canvas'); const ctx = canvas.getContext('2d');
  let webReady = false, sim = null, wcssW = 0, wcssH = 0, scale = 0.9, tx = 0, ty = 0, centered = false;
  let highlighted = null, wsearch = '';
  const show = { mathlib: false, wikidata: true, both: true };
  const webNodes = data.nodes.map(n => ({ ...n, id: n.qid }));
  const webById = new Map(webNodes.map(n => [n.id, n]));
  const edgeMap = new Map();
  for (const e of data.edges) {
    const k = e.from + '>' + e.to; let b = edgeMap.get(k);
    if (!b) { b = { source: e.from, target: e.to, sources: new Set() }; edgeMap.set(k, b); }
    b.sources.add(e.source);
  }
  const webEdges = [];
  for (const e of edgeMap.values()) {
    const inMl = e.sources.has('mathlib'), inWd = e.sources.has('wikidata');
    e.cat = (inMl && inWd) ? 'both' : (inMl ? 'mathlib' : 'wikidata');
    if (webById.has(e.source) && webById.has(e.target)) webEdges.push(e);
  }
  let edgesByCat = { mathlib: [], wikidata: [], both: [] };
  function bucketEdges() { edgesByCat = { mathlib: [], wikidata: [], both: [] }; for (const e of webEdges) edgesByCat[e.cat].push(e); }
  function nodeColor(n, matched) {
    if (matched) return COLORS.highlight;
    // colour by continent (the bubble structure, carried into the web)
    if (n.continent && n.continent !== 'unsorted' && contColor[n.continent]) {
      if (layer === 'formal' && n.status !== 'formalized') return dark() ? '#3a352f' : '#e6e0d6';
      return contColor[n.continent];
    }
    if (n.status === 'formalized') return COLORS.formalized;
    return dark() ? '#4d4742' : COLORS.unformalized;
  }
  let needsDraw = false;
  function scheduleDraw() { if (needsDraw) return; needsDraw = true; requestAnimationFrame(() => { needsDraw = false; draw(); }); }
  function wresize() {
    const r = canvas.getBoundingClientRect(); wcssW = r.width; wcssH = r.height;
    canvas.width = wcssW * devicePixelRatio; canvas.height = wcssH * devicePixelRatio;
    ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0); scheduleDraw();
  }
  function draw() {
    if (!centered) { tx = wcssW / 2; ty = wcssH / 2; centered = true; }
    ctx.clearRect(0, 0, wcssW, wcssH); ctx.save(); ctx.translate(tx, ty); ctx.scale(scale, scale); ctx.lineCap = 'round';
    const inv = 1 / scale;
    const vL = -tx * inv, vT = -ty * inv, vR = (wcssW - tx) * inv, vB = (wcssH - ty) * inv;
    for (const cat of ['mathlib', 'wikidata', 'both']) {
      if (!show[cat]) continue;
      // layer filter: formal = mathlib deps; informal/bridge = wikidata
      if (layer === 'formal' && cat === 'wikidata') continue;
      if (layer === 'informal' && cat === 'mathlib') continue;
      ctx.strokeStyle = COLORS[cat]; ctx.globalAlpha = cat === 'both' ? 0.7 : 0.24;
      ctx.lineWidth = (cat === 'both' ? 1.0 : 0.55) * inv; ctx.beginPath();
      for (const e of edgesByCat[cat]) {
        const x1 = e.source.x, y1 = e.source.y, x2 = e.target.x, y2 = e.target.y;
        if ((x1 < vL && x2 < vL) || (x1 > vR && x2 > vR) || (y1 < vT && y2 < vT) || (y1 > vB && y2 > vB)) continue;
        ctx.moveTo(x1, y1); ctx.lineTo(x2, y2);
      }
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
    if (highlighted) {
      ctx.strokeStyle = COLORS.highlight; ctx.lineWidth = 1.6 * inv; ctx.beginPath();
      for (const e of webEdges) if (e.source.id === highlighted.id || e.target.id === highlighted.id) { ctx.moveTo(e.source.x, e.source.y); ctx.lineTo(e.target.x, e.target.y); }
      ctx.stroke();
    }
    const r0 = 2.4 * inv;
    for (const n of webNodes) {
      const matched = (wsearch && (n.label || '').toLowerCase().includes(wsearch)) || (highlighted && n.id === highlighted.id);
      ctx.fillStyle = nodeColor(n, matched);
      ctx.beginPath(); ctx.arc(n.x, n.y, matched ? r0 * 2 : (n.verified ? r0 * 1.5 : r0), 0, 6.2832); ctx.fill();
      if (n.verified) { ctx.strokeStyle = '#8250df'; ctx.lineWidth = 0.7 * inv; ctx.stroke(); }
    }
    if (scale > 1.5) {
      ctx.fillStyle = dark() ? '#ebe5d8' : '#1f2328'; ctx.font = `${11 * inv}px -apple-system,sans-serif`; ctx.textAlign = 'center';
      for (const n of webNodes) { if (n.x < vL || n.x > vR || n.y < vT || n.y > vB) continue; ctx.fillText(n.label || '', n.x, n.y - 4 * inv); }
    }
    ctx.restore();
  }
  function webInit() {
    if (webReady) { wresize(); scheduleDraw(); return; }
    webReady = true; bucketEdges();
    sim = d3.forceSimulation(webNodes)
      .force('link', d3.forceLink(webEdges).id(d => d.id).distance(28).strength(0.35))
      .force('charge', d3.forceManyBody().strength(-22).distanceMax(380))
      .force('center', d3.forceCenter(0, 0)).force('collide', d3.forceCollide(4))
      .alpha(1).alphaDecay(0.025).velocityDecay(0.4).on('tick', scheduleDraw);
    wresize();
    canvas.addEventListener('mousedown', (ev) => {
      const [wx, wy] = [(ev.offsetX - tx) / scale, (ev.offsetY - ty) / scale];
      let best = null, bd = (8 / scale) ** 2;  // constant ~8px SCREEN hit radius at every zoom (world*scale=screen)
      for (const n of webNodes) { const dx = n.x - wx, dy = n.y - wy, d2 = dx * dx + dy * dy; if (d2 < bd) { bd = d2; best = n; } }
      if (best) { highlighted = best; selectConcept(best.id); scheduleDraw(); }
    });
    canvas.addEventListener('wheel', (ev) => {
      ev.preventDefault(); const f = Math.exp(-ev.deltaY * 0.0016);
      const [wx, wy] = [(ev.offsetX - tx) / scale, (ev.offsetY - ty) / scale];
      scale = Math.max(0.15, Math.min(9, scale * f)); tx = ev.offsetX - wx * scale; ty = ev.offsetY - wy * scale; scheduleDraw();
    }, { passive: false });
    let drag = null;
    canvas.addEventListener('mousemove', (ev) => { if (drag) { tx = ev.clientX - drag.x; ty = ev.clientY - drag.y; scheduleDraw(); } });
    canvas.addEventListener('mousedown', (ev) => { drag = { x: ev.clientX - tx, y: ev.clientY - ty }; });
    window.addEventListener('mouseup', () => { drag = null; });
    // legend
    document.getElementById('weblegend').innerHTML =
      `<div style="margin-bottom:4px"><b>Edges</b> <label><input type="checkbox" id="e-ml"> formal (Mathlib)</label> ` +
      `<label><input type="checkbox" id="e-wd" checked> relation (Wikidata)</label></div>` +
      `<div>Nodes coloured by domain · <span style="color:#8250df">purple ring</span> = human-reviewed</div>`;
    document.getElementById('e-ml').onchange = e => { show.mathlib = e.target.checked; scheduleDraw(); };
    document.getElementById('e-wd').onchange = e => { show.wikidata = e.target.checked; scheduleDraw(); };
  }
  function focusWeb(qid) {
    const n = webById.get(qid); if (!n) return;
    highlighted = n;
    if (n.x != null) { scale = 2.2; tx = wcssW / 2 - n.x * scale; ty = wcssH / 2 - n.y * scale; }
    selectConcept(qid); scheduleDraw();
  }
  function focusConcept(qid) { if (view === 'web') focusWeb(qid); else focusBubble(qid); }

  // ============================ SOURCES (legend) ===========================
  function renderSources() {
    const el = document.getElementById('sourcesView');
    const L = data.sources.layers;
    const order = ['formal', 'informal', 'literature', 'classification', 'bridge'];
    const badge = { formal:'#2da44e', informal:'#0969da', literature:'#9a6700', classification:'#8c959f', bridge:'#8250df' };
    let html = `<div class="src-intro"><h2>Where every link comes from</h2>` +
      `<p class="hint">WikiLean joins informal mathematics (Wikipedia) to its formal counterparts (Mathlib and other libraries) through the <b>Wikidata QID</b> — the only cross-database key in mathematics. Every external identifier below is read from <b>Wikidata (CC0)</b>, never scraped from the target site. Our own annotation + graph data is <b>${esc(data.sources.our_data_license.annotations)}</b>; linked content stays under each project's own license.</p></div>`;
    for (const ly of order) {
      const items = data.sources.sources.filter(s => s.layer === ly);
      if (!items.length) continue;
      html += `<div class="layer-group"><h3><span class="layer-badge" style="background:${badge[ly]}">${ly}</span> ${esc(L[ly] ? ly[0].toUpperCase()+ly.slice(1) : ly)}</h3>` +
        `<p class="layer-desc">${esc(L[ly] || '')}</p><div class="src-grid">`;
      for (const s of items) {
        html += `<div class="src-card"><h4>${s.homepage ? `<a href="${esc(s.homepage)}" target="_blank" rel="noopener">${esc(s.name)}</a>` : esc(s.name)}</h4>` +
          (s.wikidata_property ? `<div class="kv"><b>Wikidata property</b> · ${esc(s.wikidata_property)}</div>` : '') +
          `<div class="prov">${esc(s.our_provenance)}</div>` +
          (s.target_license ? `<div class="kv" style="margin-top:6px"><b>License</b> · ${esc(s.target_license)}</div>` : '') +
          (s.note ? `<div class="src-note">${esc(s.note)}</div>` : '') + `</div>`;
      }
      html += `</div></div>`;
    }
    el.innerHTML = html;
  }

  // ============================ view switching =============================
  const views = { bubbles: document.getElementById('bubblesView'), web: document.getElementById('webView'), sources: document.getElementById('sourcesView') };
  function setView(v) {
    view = v;
    for (const k in views) views[k].hidden = (k !== v);
    document.querySelectorAll('#viewswitch button').forEach(b => b.classList.toggle('on', b.dataset.view === v));
    document.getElementById('search').style.display = (v === 'sources') ? 'none' : '';
    try { history.replaceState(null, '', '?view=' + v); } catch (e) {}
    if (v === 'bubbles') bubblesInit();
    else if (v === 'web') { wsearch = document.getElementById('search').value.trim().toLowerCase(); webInit(); }
    else renderSources();
  }
  document.querySelectorAll('#viewswitch button').forEach(b => b.onclick = () => setView(b.dataset.view));

  // layer filter
  document.getElementById('layerfilter').addEventListener('change', e => {
    layer = e.target.value;
    node.attr('fill', bubbleFill);
    if (webReady) scheduleDraw();
    if (selectedQid) conceptPanel(selectedQid);
  });

  // search (works in both graph views)
  document.getElementById('search').addEventListener('input', e => {
    const q = e.target.value.trim().toLowerCase();
    if (view === 'web') { wsearch = q; if (webReady) scheduleDraw(); return; }
    if (q.length < 3) return;
    const hit = bubbleLeaves.find(l => (l.data.name || '').toLowerCase().startsWith(q)) ||
                bubbleLeaves.find(l => (l.data.name || '').toLowerCase().includes(q));
    if (hit) { zoom(hit.parent); selectConcept(hit.data.qid); }
  });

  window.addEventListener('resize', () => {
    W = stage.clientWidth || 800; H = stage.clientHeight || 600;
    d3.pack().size([W, H]).padding(3)(root); zoomTo([focus.x, focus.y, focus.r * 2.05]);
    if (webReady) wresize();
  });
  document.getElementById('wl-theme-toggle').addEventListener('click', () => {
    const r = document.documentElement, n = r.dataset.theme === 'dark' ? 'light' : 'dark';
    r.dataset.theme = n; try { localStorage.setItem('wl-theme', n); } catch (e) {}
    node.attr('fill', bubbleFill); if (webReady) scheduleDraw();
  });

  // initial view from ?view=
  const v0 = new URLSearchParams(location.search).get('view');
  setView(['bubbles', 'web', 'sources'].includes(v0) ? v0 : 'bubbles');
})();
</script>
</body>
</html>
"""


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "map.html").write_text(HTML)
    print("Wrote out/map.html")


if __name__ == "__main__":
    main()
