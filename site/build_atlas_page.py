#!/usr/bin/env python3
"""Generate /atlas — the zoomable bubble atlas (Phase A of the multilayer map).

Reads nothing itself: the page fetches /atlas_data.json (KV-first in the
Worker, static fallback). Zoomable circle packing (continents → subfields →
super-nodes/concepts); click zooms in, click outside zooms out. Detail panel
shows a bubble's aggregated connections (subfield/continent rollups) or a
concept's identity + links. Run: python3 site/build_atlas_page.py
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "out"

HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WikiLean — Atlas of mathematics</title>
<meta name="description" content="A multilayer bubble atlas of mathematics: domains, subfields, and concepts — each carrying its Mathlib formalization status, conjecture frontier, and cross-database identity.">
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
  border-radius:50%; width:28px; height:28px; padding:0; font-size:14px; cursor:pointer; }
#app { display:grid; grid-template-columns: 1fr 340px; height: calc(100vh - 53px); min-height:0; }
#stage { position:relative; min-width:0; }
svg { display:block; width:100%; height:100%; cursor:pointer; }
#info { border-left:1px solid #d0d7de; background:#fff; overflow-y:auto; padding:18px; font-size:.92rem; }
#info h3 { font-size:1.05rem; margin:0 0 4px; }
#info .crumb { color:#57606a; font-size:.8rem; margin-bottom:10px; }
#info .field { margin:5px 0; font-size:.9rem; }
#info .field b { color:#57606a; font-weight:600; }
#info a { color:#0969da; text-decoration:none; }
#info a:hover { text-decoration:underline; }
#info code { background:#f0f0f0; padding:1px 5px; border-radius:3px; font-size:.85rem; }
#search { position:absolute; top:14px; left:14px; width:260px; padding:7px 10px;
  border:1px solid #d0d7de; border-radius:6px; font:inherit; background:#fff; color:#1f2328; }
#search:focus { outline:none; border-color:#0969da; box-shadow:0 0 0 2px rgba(9,105,218,.18); }
.hint { font-size:.78rem; color:#57606a; line-height:1.55; }
.empty { color:#8c959f; font-style:italic; }
circle.bubble { stroke:#fff0; transition: stroke .15s; }
circle.bubble:hover { stroke:#0969da; stroke-width:1.5px; }
text.blabel { pointer-events:none; text-anchor:middle;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; fill:#1f2328; }
[data-theme="dark"] body { background:#1a1816; color:#ebe5d8; }
[data-theme="dark"] .wl-header { background:#232020; border-bottom-color:#4d4742; }
[data-theme="dark"] .wl-brand, [data-theme="dark"] .wl-navlink { color:#6e9adf; }
[data-theme="dark"] .wl-navlink.active { color:#ebe5d8; }
[data-theme="dark"] #info { background:#232020; border-left-color:#4d4742; color:#ebe5d8; }
[data-theme="dark"] #info code { background:#2c2926; }
[data-theme="dark"] #info a { color:#6e9adf; }
[data-theme="dark"] #search { background:#1a1816; color:#ebe5d8; border-color:#4d4742; }
[data-theme="dark"] text.blabel { fill:#ebe5d8; }
</style>
</head>
<body>
<header class="wl-header">
  <a class="wl-brand" href="/">WikiLean</a>
  <nav class="wl-nav">
    <a class="wl-navlink" href="/concepts">Concepts</a>
    <a class="wl-navlink" href="/graph">Concept graph</a>
    <a class="wl-navlink active" href="/atlas">Atlas</a>
    <a class="wl-navlink" href="/about">About &amp; method</a>
    <button id="wl-theme-toggle" class="wl-theme-toggle" type="button" aria-label="Toggle dark mode" title="Toggle dark mode">🌓</button>
  </nav>
</header>
<div id="app">
  <div id="stage">
    <input id="search" type="text" placeholder="Find a concept… (e.g. Module)" autocomplete="off">
    <svg id="svg"></svg>
  </div>
  <aside id="info">
    <h3>Atlas of mathematics</h3>
    <p class="hint">Domains → subfields → concepts, each bubble sized by its contents.
    Click a bubble to zoom in; click the background to zoom out. Leaf colours:
    <span style="color:#2da44e">formalized</span> ·
    <span style="color:#8c959f">not yet</span> ·
    dashed <span style="color:#c78420">amber</span> = conjecture frontier ·
    <span style="color:#8250df">purple ring</span> = human-reviewed mapping.</p>
    <p class="hint">Assignment is deterministic and provenance-tagged (Mathlib module root →
    MSC chip → AMS code → neighbour vote → unsorted) — every concept shows which rule placed it.</p>
  </aside>
</div>
<script src="https://cdn.jsdelivr.net/npm/d3@7"></script>
<script>
(async function () {
  const data = await (await fetch('/atlas_data.json')).json();
  const info = document.getElementById('info');
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  // ---- build the d3 hierarchy: root → continents → subfields → (supernode|concept)
  const superBySub = {};
  for (const sn of data.supernodes) (superBySub[sn.subfield] ||= []).push(sn);
  const inSuper = new Set(data.supernodes.flatMap(sn => sn.members));
  const rootData = { name: 'mathematics', kind: 'root', children: data.continents.map(c => ({
    name: c.label, kind: 'continent', key: c.key, color: c.color, n: c.n_concepts,
    children: c.subfields.map(sk => {
      const sf = data.subfields[sk];
      const kids = [];
      for (const sn of (superBySub[sk] || []))
        kids.push({ name: sn.decl, kind: 'supernode', decl: sn.decl,
                    children: sn.members.map(q => leaf(q)) });
      for (const q of sf.qids) if (!inSuper.has(q)) kids.push(leaf(q));
      return { name: sf.label, kind: 'subfield', key: sk, children: kids };
    }).filter(s => s.children.length),
  })).filter(c => c.children.length) };
  function leaf(q) { return { name: data.nodes[q].label || q, kind: 'concept', qid: q, value: 1 }; }

  const root = d3.hierarchy(rootData).sum(d => d.value || 0).sort((a, b) => b.value - a.value);
  const svg = d3.select('#svg');
  const stage = document.getElementById('stage');
  let W = stage.clientWidth, H = stage.clientHeight;
  d3.pack().size([W, H]).padding(3)(root);

  const dark = () => document.documentElement.dataset.theme === 'dark';
  const statusFill = d => {
    const n = data.nodes[d.data.qid];
    if (!n) return '#0000';
    if (n.status === 'formalized') return '#2da44e';
    return dark() ? '#57534d' : '#c9d1d9';
  };
  const bubbleFill = d =>
    d.data.kind === 'continent' ? d.data.color + (dark() ? '33' : '22')
    : d.data.kind === 'subfield' ? (dark() ? '#ffffff10' : '#ffffff88')
    : d.data.kind === 'supernode' ? (dark() ? '#6e9adf22' : '#0969da14')
    : statusFill(d);

  const g = svg.append('g');
  let focus = root, view;
  const node = g.selectAll('circle').data(root.descendants().slice(1)).join('circle')
    .attr('class', 'bubble')
    .attr('fill', bubbleFill)
    .attr('stroke', d => {
      const n = data.nodes[d.data.qid];
      if (n?.verified) return '#8250df';
      if (n?.frontier || n?.n_conjectures) return '#c78420';
      return null;
    })
    .attr('stroke-dasharray', d => (data.nodes[d.data.qid]?.frontier || data.nodes[d.data.qid]?.n_conjectures) ? '3,2' : null)
    .attr('stroke-width', 1.2)
    .on('click', (event, d) => {
      event.stopPropagation();
      showInfo(d);
      if (d.children && focus !== d) zoom(d);
      else if (!d.children && focus !== d.parent) zoom(d.parent);
    });
  const label = g.selectAll('text').data(root.descendants().slice(1)).join('text')
    .attr('class', 'blabel')
    .style('display', 'none')
    .text(d => d.data.name);

  svg.on('click', () => { if (focus.parent) { zoom(focus.parent); showInfo(focus.parent); } });

  function zoomTo(v) {
    const k = Math.min(W, H) / v[2];
    view = v;
    node.attr('transform', d => `translate(${(d.x - v[0]) * k + W / 2},${(d.y - v[1]) * k + H / 2})`)
        .attr('r', d => d.r * k);
    label.attr('transform', d => `translate(${(d.x - v[0]) * k + W / 2},${(d.y - v[1]) * k + H / 2})`);
    relabel(k);
  }
  function relabel(k) {
    label.style('display', d =>
      (d.parent === focus && d.r * k > 14) || (d === focus && !d.children) ? null : 'none')
      .style('font-size', d => Math.max(9, Math.min(15, d.r * k / 3.2)) + 'px');
  }
  function zoom(d) {
    focus = d;
    const t = svg.transition().duration(500)
      .tween('zoom', () => {
        const i = d3.interpolateZoom(view, [focus.x, focus.y, focus.r * 2.05]);
        return tt => zoomTo(i(tt));
      });
  }
  zoomTo([root.x, root.y, root.r * 2.05]);
  showInfo(root);

  function crumb(d) {
    return d.ancestors().reverse().slice(1).map(a => esc(a.data.name)).join(' › ');
  }
  function showInfo(d) {
    if (d === root || !d.data) { return; }
    const k = d.data.kind;
    if (k === 'concept') {
      const n = data.nodes[d.data.qid];
      const rows = [
        `<h3>${esc(n.label)}</h3>`,
        `<div class="crumb">${crumb(d)}</div>`,
        `<div class="field"><b>Status</b> · ${esc(n.status)}${n.coverage != null ? ` · ${Math.round(n.coverage * 100)}% coverage` : ''}</div>`,
        n.n_conjectures ? `<div class="field" style="color:#c78420"><b>Conjecture frontier</b> · ${n.n_conjectures} statement${n.n_conjectures === 1 ? '' : 's'}</div>` : '',
        n.verified ? `<div class="field" style="color:#8250df"><b>✓ Human-reviewed</b> mapping</div>` : '',
        `<div class="field"><b>Placed by</b> · <code>${esc(n.assign_rule)}</code></div>`,
        `<div class="field" style="margin-top:12px">` +
          `<a href="/${encodeURIComponent(n.slug)}">WikiLean article →</a><br>` +
          `<a href="https://www.wikidata.org/wiki/${esc(d.data.qid)}" target="_blank" rel="noopener">Wikidata ${esc(d.data.qid)} →</a><br>` +
          `<a href="/graph">Open in concept graph →</a></div>`,
      ];
      info.innerHTML = rows.join('');
    } else if (k === 'supernode') {
      info.innerHTML = `<h3><code>${esc(d.data.decl)}</code></h3>` +
        `<div class="crumb">${crumb(d)}</div>` +
        `<div class="field">One Mathlib declaration realizing <b>${d.children.length}</b> distinct concepts:</div>` +
        d.children.map(c => `<div class="field">· ${esc(c.data.name)}</div>`).join('') +
        `<div class="field" style="margin-top:10px"><a href="/decl/${encodeURIComponent(d.data.decl)}" target="_blank" rel="noopener">View declaration →</a></div>`;
    } else if (k === 'subfield') {
      const pairs = data.edges.subfield_pairs.filter(p => p.a === d.data.key || p.b === d.data.key).slice(0, 8);
      info.innerHTML = `<h3>${esc(d.data.name)}</h3>` +
        `<div class="crumb">${crumb(d)}</div>` +
        `<div class="field"><b>Concepts</b> · ${d.leaves().length}</div>` +
        `<div class="field" style="margin-top:8px"><b>Most connected to</b></div>` +
        (pairs.length ? pairs.map(p => {
          const other = p.a === d.data.key ? p.b : p.a;
          return `<div class="field">· ${esc(data.subfields[other]?.label || other)} <span class="hint">(${p.count} dependencies)</span></div>`;
        }).join('') : '<div class="empty">no cross-subfield edges</div>');
    } else if (k === 'continent') {
      const pairs = data.edges.continent_pairs.filter(p => p.a === d.data.key || p.b === d.data.key).slice(0, 6);
      const contLabel = x => (data.continents.find(c => c.key === x) || {}).label || x;
      info.innerHTML = `<h3>${esc(d.data.name)}</h3>` +
        `<div class="field"><b>Concepts</b> · ${d.leaves().length} in ${d.children.length} subfields</div>` +
        `<div class="field" style="margin-top:8px"><b>Most connected to</b></div>` +
        (pairs.length ? pairs.map(p => {
          const other = p.a === d.data.key ? p.b : p.a;
          return `<div class="field">· ${esc(contLabel(other))} <span class="hint">(${p.count} dependencies)</span></div>`;
        }).join('') : '<div class="empty">no cross-continent edges</div>');
    }
  }

  // search → zoom to the concept's parent bubble and select it
  const idx = root.leaves();
  document.getElementById('search').addEventListener('input', e => {
    const q = e.target.value.trim().toLowerCase();
    if (q.length < 3) return;
    const hit = idx.find(l => (l.data.name || '').toLowerCase().startsWith(q)) ||
                idx.find(l => (l.data.name || '').toLowerCase().includes(q));
    if (hit) { zoom(hit.parent); showInfo(hit); }
  });

  window.addEventListener('resize', () => {
    W = stage.clientWidth; H = stage.clientHeight;
    d3.pack().size([W, H]).padding(3)(root);
    zoomTo([focus.x, focus.y, focus.r * 2.05]);
  });
  const tbtn = document.getElementById('wl-theme-toggle');
  tbtn.addEventListener('click', () => {
    const r = document.documentElement;
    const n = r.dataset.theme === 'dark' ? 'light' : 'dark';
    r.dataset.theme = n;
    try { localStorage.setItem('wl-theme', n); } catch (e) {}
    node.attr('fill', bubbleFill);
  });
})();
</script>
</body>
</html>
"""


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "atlas.html").write_text(HTML)
    print("Wrote out/atlas.html")


if __name__ == "__main__":
    main()
