#!/usr/bin/env python3
"""Generate the article-network viewer.

Reads site/annotations/*.json (skipping the .agent1.json variants), builds:
  - nodes: one per article with status counts + dominant Mathlib namespace
  - edges: article-pair co-citation, weighted by number of shared Mathlib decls
Writes:
  out/article_graph.html       — viewer in WikiLean's chrome
  out/article_graph_data.json  — {nodes, edges}
"""
from __future__ import annotations

import collections
import json
import re
import unicodedata
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
ANNO_DIR = HERE / "annotations"
CACHE_DIR = HERE / "cache"
OUT_DIR = HERE / "out"

# Match a Wikipedia internal link in cached article HTML.
_LINK_RE = re.compile(r'href="/wiki/([^"#?]+)"')
# Non-article namespaces to skip.
_BAD_NS_RE = re.compile(r"^(File|Image|Special|Help|Talk|User|Wikipedia|Portal|Category|Template|Module|MediaWiki):")


def _normalize_target(raw: str) -> str:
    """Decode a Wikipedia URL slug and reduce to WikiLean's ASCII slug form."""
    s = urllib.parse.unquote(raw)
    s = s.replace("–", "-").replace("—", "-")  # en/em dash
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9_.\-]", "", s)


def parse_outgoing_links(slug: str, slugs: set[str]) -> set[str]:
    """Outgoing slug-to-slug Wikipedia links from this article's cached HTML."""
    fp = CACHE_DIR / f"{slug}.html"
    if not fp.exists():
        return set()
    html = fp.read_text(errors="replace")
    out: set[str] = set()
    for m in _LINK_RE.finditer(html):
        raw = m.group(1)
        if _BAD_NS_RE.match(raw):
            continue
        target = _normalize_target(raw)
        if target == slug or target not in slugs:
            continue
        out.add(target)
    return out


def top_ns(module: str) -> str:
    """Mathlib namespace bucket: 'Mathlib.Algebra.X.Y' -> 'Algebra'."""
    if module.startswith("Mathlib."):
        rest = module[len("Mathlib.") :]
        return rest.split(".", 1)[0]
    return module.split(".", 1)[0]


def main() -> None:
    nodes: list[dict] = []
    article_decls: dict[str, set[str]] = {}
    decl_modules: dict[str, str] = {}

    for jf in sorted(ANNO_DIR.glob("*.json")):
        if ".agent1." in jf.name:
            continue
        d = json.loads(jf.read_text())
        slug = d.get("slug")
        if not slug or not (OUT_DIR / f"{slug}.html").exists():
            continue

        ns_counter: collections.Counter = collections.Counter()
        status = {"formalized": 0, "partial": 0, "not_formalized": 0}
        decls: set[str] = set()
        for a in d.get("annotations") or []:
            s = a.get("status")
            if s in status:
                status[s] += 1
            ml = a.get("mathlib") or {}
            decl = ml.get("decl")
            mod = ml.get("module")
            if decl and s in ("formalized", "partial"):
                decls.add(decl)
                if mod and decl not in decl_modules:
                    decl_modules[decl] = mod
            if mod:
                ns_counter[top_ns(mod)] += 1

        if sum(status.values()) == 0:
            continue
        if not decls:
            continue

        nodes.append({
            "slug": slug,
            "title": d.get("display_title") or d.get("wikipedia_title") or slug.replace("_", " "),
            "n_formalized": status["formalized"],
            "n_partial": status["partial"],
            "n_not_formalized": status["not_formalized"],
            "n_total": sum(status.values()),
            "n_decls": len(decls),
            "top_namespaces": [ns for ns, _ in ns_counter.most_common(3)],
            "dominant_ns": ns_counter.most_common(1)[0][0] if ns_counter else None,
            "decls": sorted(decls),
        })
        article_decls[slug] = decls

    # Article-pair edges by shared decls (both endpoints must have decls).
    slugs = list(article_decls)
    slug_set: set[str] = set(slugs)
    edges: list[dict] = []
    for i in range(len(slugs)):
        a = slugs[i]
        sa = article_decls[a]
        for j in range(i + 1, len(slugs)):
            b = slugs[j]
            shared = sa & article_decls[b]
            if not shared:
                continue
            edges.append({"a": a, "b": b, "shared": len(shared)})
    edges.sort(key=lambda e: -e["shared"])

    # Article-pair edges by Wikipedia internal links parsed from cached HTML.
    # Undirected; `mutual` = both A->B and B->A appear in the article text.
    outgoing: dict[str, set[str]] = {s: parse_outgoing_links(s, slug_set) for s in slugs}
    link_edges: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    for a, neigh in outgoing.items():
        for b in neigh:
            key = (a, b) if a < b else (b, a)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            mutual = (a in outgoing.get(b, set())) and (b in outgoing.get(a, set()))
            link_edges.append({"a": key[0], "b": key[1], "mutual": mutual})

    OUT_DIR.mkdir(exist_ok=True)
    data = {
        "nodes": nodes,
        "edges": edges,
        "link_edges": link_edges,
        "decl_modules": decl_modules,
    }
    (OUT_DIR / "article-graph-data.json").write_text(json.dumps(data, separators=(",", ":")))
    (OUT_DIR / "article-graph.html").write_text(HTML)

    edge_dist = collections.Counter(min(e["shared"], 10) for e in edges)
    by_ns = collections.Counter(n.get("dominant_ns") for n in nodes)
    print(f"nodes: {len(nodes)}, shared-decl edges: {len(edges)}, link edges: {len(link_edges)}")
    print(f"  shared-decl edges (>=k): "
          + " ".join(f"k>={k}:{sum(c for kk, c in edge_dist.items() if kk >= k)}" for k in (1, 3, 5, 10)))
    n_mutual = sum(1 for e in link_edges if e["mutual"])
    print(f"  link edges mutual: {n_mutual} / {len(link_edges)}")
    print(f"  dominant-ns: " + ", ".join(f"{n}={c}" for n, c in by_ns.most_common(10)))


HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WikiLean — Article graph</title>
<meta name="description" content="Wikipedia mathematics articles clustered by shared Mathlib formalizations: edges connect articles that annotate the same Lean declarations.">
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

#app { display:grid; grid-template-columns: 260px 1fr 320px; grid-template-rows: 1fr;
  height: calc(100vh - 53px); min-height:0; }
aside { padding:18px 18px; background:#fff; overflow-y:auto; min-height:0; font-size:.92rem; color:#1f2328; }
#side { border-right:1px solid #d0d7de; }
#info { border-left:1px solid #d0d7de; }
canvas { display:block; width:100%; height:100%; cursor:grab; background:#fafbfc; }
canvas.dragging { cursor:grabbing; }
h2 { font-size:.72rem; text-transform:uppercase; letter-spacing:.06em; margin:18px 0 6px;
  color:#57606a; font-weight:600; }
h2:first-child { margin-top:0; }
input[type="text"], input[type="range"] { width:100%; box-sizing:border-box; }
input[type="text"] { padding:7px 10px; border:1px solid #d0d7de; border-radius:6px;
  font:inherit; color:#1f2328; background:#fff; }
input[type="text"]:focus { outline:none; border-color:#0969da; box-shadow:0 0 0 2px rgba(9,105,218,.18); }
input[type="range"] { margin:6px 0 0; }
label.row { display:flex; align-items:center; gap:8px; padding:3px 0; cursor:pointer; user-select:none; }
label.row .swatch { display:inline-block; width:14px; height:4px; border-radius:2px; }
.hint-right { margin-left:auto; color:#57606a; }
.hint-right b { color:#1f2328; font-variant-numeric:tabular-nums; }
.slider-row { display:flex; justify-content:space-between; align-items:center; font-size:.85rem; color:#57606a; }
.slider-row b { color:#1f2328; font-variant-numeric:tabular-nums; }
.stat { display:flex; justify-content:space-between; padding:2px 0; font-size:.88rem; }
.stat .v { font-variant-numeric:tabular-nums; color:#57606a; }
.legend { display:grid; grid-template-columns: 14px auto auto; gap: 4px 8px; align-items:center; font-size:.82rem; }
.legend .sw { width:11px; height:11px; border-radius:50%; }
.legend .nm { color:#1f2328; }
.legend .ct { color:#57606a; font-variant-numeric:tabular-nums; text-align:right; }
.hint { font-size:.78rem; color:#57606a; line-height:1.55; }
#info h3 { font-size:1.05rem; margin:0 0 4px; }
#info h3 a { color:#1f2328; text-decoration:none; }
#info h3 a:hover { text-decoration:underline; }
#info .slug { color:#57606a; font-size:.82rem; margin-bottom:10px; }
#info .bar { display:flex; height:8px; border-radius:4px; overflow:hidden; background:#eaeef2; margin:8px 0 4px; }
#info .bar i.f { display:block; height:100%; background:#2da44e; }
#info .bar i.p { display:block; height:100%; background:#d29922; }
#info .bar i.n { display:block; height:100%; background:#cf222e; }
#info .field { margin:6px 0; font-size:.9rem; }
#info .field b { color:#57606a; font-weight:600; }
#info a { color:#0969da; text-decoration:none; }
#info a:hover { text-decoration:underline; }
#info .links { display:flex; flex-direction:column; gap:4px; margin-top:14px; padding-top:14px; border-top:1px solid #d0d7de; }
#info ul { padding-left:18px; margin:4px 0; font-size:.86rem; color:#1f2328; }
#info ul li { margin:2px 0; }
.empty { color:#8c959f; font-style:italic; }
.chips { display:flex; flex-wrap:wrap; gap:6px; margin:8px 0 14px; }
.chip { display:inline-flex; align-items:center; gap:2px; background:#f6f8fa; border:1px solid #d0d7de;
  border-radius:12px; padding:1px 4px 1px 10px; font-size:.82rem; }
.chip a { color:#1f2328; }
.chip a.rm { color:#8c959f; padding:0 5px; font-size:.78rem; }
.chip a.rm:hover { color:#cf222e; }
.declist { padding-left:18px; margin:6px 0 14px; font-size:.85rem; max-height:280px; overflow-y:auto; }
.declist code { background:#f0f0f0; padding:1px 5px; border-radius:3px; font-size:.88em; }
.declist a { color:#1f2328; }
.declist a:hover code { background:#e0e6ed; }
.declist .mod { font-size:.74rem; color:#57606a; margin-left:4px; }

/* Dark mode — shared palette (bg #1a1816, surface #232020, text #ebe5d8,
   muted #9a9081, accent #6e9adf, borders #4d4742). Namespace node colors and
   the canvas bg/labels read fine on dark or are handled in JS via dataset.theme. */
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
[data-theme="dark"] .hint-right { color:#9a9081; }
[data-theme="dark"] .hint-right b { color:#ebe5d8; }
[data-theme="dark"] .slider-row { color:#9a9081; }
[data-theme="dark"] .slider-row b { color:#ebe5d8; }
[data-theme="dark"] .stat .v { color:#9a9081; }
[data-theme="dark"] .legend .nm { color:#ebe5d8; }
[data-theme="dark"] .legend .ct { color:#9a9081; }
[data-theme="dark"] .hint { color:#9a9081; }
[data-theme="dark"] #info h3 a { color:#ebe5d8; }
[data-theme="dark"] #info .slug { color:#9a9081; }
[data-theme="dark"] #info .bar { background:#34302c; }
[data-theme="dark"] #info .field b { color:#9a9081; }
[data-theme="dark"] #info a { color:#6e9adf; }
[data-theme="dark"] #info ul { color:#ebe5d8; }
[data-theme="dark"] #info .links { border-top-color:#4d4742; }
[data-theme="dark"] .empty { color:#9a9081; }
[data-theme="dark"] .chip { background:#2c2926; border-color:#4d4742; }
[data-theme="dark"] .chip a { color:#ebe5d8; }
[data-theme="dark"] .chip a.rm { color:#9a9081; }
[data-theme="dark"] .declist code { background:#2c2926; color:#ebe5d8; }
[data-theme="dark"] .declist a { color:#ebe5d8; }
[data-theme="dark"] .declist a:hover code { background:#34302c; }
[data-theme="dark"] .declist .mod { color:#9a9081; }
</style>
</head>
<body>
<header class="wl-header">
  <a class="wl-brand" href="/">WikiLean</a>
  <nav class="wl-nav">
    <a class="wl-navlink" href="/concepts">Concepts</a>
    <a class="wl-navlink active" href="/article-graph">Article graph</a>
    <a class="wl-navlink" href="/map">Map</a>
    <a class="wl-navlink" href="/about">About &amp; method</a>
    <button id="wl-theme-toggle" class="wl-theme-toggle" type="button" aria-label="Toggle dark mode" title="Toggle dark mode">\U0001f313</button>
  </nav>
</header>
<div id="app">
  <aside id="side">
    <h2>Edges</h2>
    <label class="row"><input type="checkbox" id="show-decls" checked><span class="swatch" style="background:#9aa6b3"></span> Shared decls <span class="hint hint-right"><b id="visible-decls">0</b></span></label>
    <label class="row"><input type="checkbox" id="show-links"><span class="swatch" style="background:#4c8bf5"></span> Wikipedia links <span class="hint hint-right"><b id="visible-links">0</b></span></label>

    <h2>Min shared decls</h2>
    <div class="slider-row"><span>k &ge; <b id="k-val">1</b></span></div>
    <input type="range" id="k-slider" min="1" max="15" value="1">

    <h2>Search</h2>
    <input type="text" id="search" placeholder="Article title…" autocomplete="off">

    <h2>Mathlib namespace</h2>
    <div class="legend" id="legend"></div>

    <h2>Stats</h2>
    <div id="stats"></div>

    <h2>About this view</h2>
    <p class="hint">Each node is a WikiLean article. An edge connects two articles that annotate the same Mathlib declarations (status: formalized or partial) — the more they share, the heavier the edge. Color encodes the dominant Mathlib namespace cited by each article.</p>
    <p class="hint">Drag to pan · scroll to zoom · click a node for details · click an edge to see the decls two articles share · shift-click to add another article to the comparison.</p>
  </aside>
  <canvas id="canvas"></canvas>
  <aside id="info">
    <p class="empty">Click an article to inspect.</p>
  </aside>
</div>
<script src="https://cdn.jsdelivr.net/npm/d3@7"></script>
<script>
// Stable color per Mathlib namespace bucket.
const NS_COLORS = {
  Analysis:      '#1f77b4',
  Algebra:       '#d62728',
  LinearAlgebra: '#17becf',
  Topology:      '#9467bd',
  Data:          '#7f7f7f',
  RingTheory:    '#e377c2',
  NumberTheory:  '#bcbd22',
  MeasureTheory: '#2ca02c',
  Geometry:      '#ff7f0e',
  Probability:   '#8c564b',
  CategoryTheory:'#aec7e8',
  Order:         '#ffbb78',
  SetTheory:     '#98df8a',
  Combinatorics: '#c5b0d5',
  GroupTheory:   '#ff9896',
  Logic:         '#c49c94',
  FieldTheory:   '#f7b6d2',
  Computability: '#dbdb8d',
};
const NS_OTHER = '#8c959f';

(async () => {
  const data = await (await fetch('article-graph-data.json')).json();
  const nodes = data.nodes.map(n => ({ ...n, id: n.slug }));
  const nodeById = new Map(nodes.map(n => [n.id, n]));
  const allEdges = data.edges
    .filter(e => nodeById.has(e.a) && nodeById.has(e.b))
    .map(e => ({ source: e.a, target: e.b, shared: e.shared }));

  // Wikipedia link edges: parsed from cached enwiki HTML. Layout-independent
  // (only shared-decl edges drive the force simulation).
  const linkEdges = (data.link_edges || [])
    .filter(e => nodeById.has(e.a) && nodeById.has(e.b))
    .map(e => ({ source: nodeById.get(e.a), target: nodeById.get(e.b), mutual: !!e.mutual }));

  // Build adjacency for the info panel.
  const linksByNode = new Map();
  for (const e of linkEdges) {
    if (!linksByNode.has(e.source.id)) linksByNode.set(e.source.id, []);
    if (!linksByNode.has(e.target.id)) linksByNode.set(e.target.id, []);
    linksByNode.get(e.source.id).push({ other: e.target, mutual: e.mutual });
    linksByNode.get(e.target.id).push({ other: e.source, mutual: e.mutual });
  }

  // Initial k = 1 (all decl edges) — sim recomputes when k changes.
  let kMin = 1;
  let edges = allEdges.filter(e => e.shared >= kMin);
  const show = { decls: true, links: false };

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

  // Node radius from log-scaled total annotations.
  const maxTotal = Math.max(1, ...nodes.map(n => n.n_total));
  function nodeRadius(n) {
    return 2.5 + 6 * Math.sqrt(n.n_total / maxTotal);
  }

  function nodeColor(n) {
    return NS_COLORS[n.dominant_ns] || NS_OTHER;
  }

  const sim = d3.forceSimulation(nodes)
    .force('link', d3.forceLink(edges).id(d => d.id)
      .distance(d => 80 / Math.sqrt(d.shared))
      .strength(d => Math.min(1, 0.05 * d.shared)))
    .force('charge', d3.forceManyBody().strength(-40).distanceMax(500))
    .force('center', d3.forceCenter(0, 0))
    .force('collide', d3.forceCollide(d => nodeRadius(d) + 1.5))
    .alpha(1).alphaDecay(0.03).velocityDecay(0.4)
    .on('tick', scheduleDraw);

  const declModules = data.decl_modules || {};
  const selected = new Set();
  let primary = null; // last clicked node — used for label rendering
  let searchTerm = '';
  let nsFilter = null;

  function isSelected(n) { return selected.has(n); }
  function pointToSegmentDist2(px, py, x1, y1, x2, y2) {
    const dx = x2 - x1, dy = y2 - y1;
    const len2 = dx * dx + dy * dy;
    if (len2 === 0) { const a = px - x1, b = py - y1; return a*a + b*b; }
    let t = ((px - x1) * dx + (py - y1) * dy) / len2;
    if (t < 0) t = 0; else if (t > 1) t = 1;
    const cx = x1 + t * dx, cy = y1 + t * dy;
    const ex = px - cx, ey = py - cy;
    return ex * ex + ey * ey;
  }

  let needsDraw = false;
  function scheduleDraw() {
    if (needsDraw) return;
    needsDraw = true;
    requestAnimationFrame(() => { needsDraw = false; draw(); });
  }

  function matchesSearch(n) {
    return searchTerm && (n.title || n.slug).toLowerCase().includes(searchTerm);
  }

  function nodeVisible(n) {
    return !nsFilter || n.dominant_ns === nsFilter;
  }

  function draw() {
    if (!centered) { tx = cssW / 2; ty = cssH / 2; centered = true; }
    ctx.clearRect(0, 0, cssW, cssH);
    ctx.save();
    ctx.translate(tx, ty);
    ctx.scale(scale, scale);
    ctx.lineCap = 'round';
    const inv = 1 / scale;
    const vL = -tx * inv, vT = -ty * inv;
    const vR = (cssW - tx) * inv, vB = (cssH - ty) * inv;

    // Wikipedia link edges first (sit beneath shared-decl edges so the
    // formalization signal stays visible on top).
    if (show.links) {
      ctx.strokeStyle = '#4c8bf5';
      ctx.globalAlpha = 0.22;
      ctx.lineWidth = 0.5 * inv;
      ctx.beginPath();
      for (let i = 0; i < linkEdges.length; i++) {
        const e = linkEdges[i];
        const a = e.source, b = e.target;
        if (!nodeVisible(a) && !nodeVisible(b)) continue;
        const x1 = a.x, y1 = a.y, x2 = b.x, y2 = b.y;
        if ((x1 < vL && x2 < vL) || (x1 > vR && x2 > vR) ||
            (y1 < vT && y2 < vT) || (y1 > vB && y2 > vB)) continue;
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
      }
      ctx.stroke();
    }

    // Shared-decl edges: stroke width by shared count, gray.
    if (show.decls) {
      ctx.strokeStyle = '#5b6770';
      ctx.globalAlpha = 0.35;
      for (const e of edges) {
        const a = e.source, b = e.target;
        if (!nodeVisible(a) && !nodeVisible(b)) continue;
        const x1 = a.x, y1 = a.y, x2 = b.x, y2 = b.y;
        if ((x1 < vL && x2 < vL) || (x1 > vR && x2 > vR) ||
            (y1 < vT && y2 < vT) || (y1 > vB && y2 > vB)) continue;
        ctx.lineWidth = Math.min(3, 0.4 + e.shared * 0.25) * inv;
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        ctx.stroke();
      }
    }
    ctx.globalAlpha = 1;

    // Highlight: edges incident to any selected node, thicker + red.
    if (selected.size > 0) {
      ctx.strokeStyle = '#cf222e';
      ctx.lineWidth = 1.4 * inv;
      ctx.globalAlpha = 0.85;
      ctx.beginPath();
      if (show.decls) {
        for (const e of edges) {
          if (isSelected(e.source) || isSelected(e.target)) {
            ctx.moveTo(e.source.x, e.source.y);
            ctx.lineTo(e.target.x, e.target.y);
          }
        }
      }
      if (show.links) {
        for (const e of linkEdges) {
          if (isSelected(e.source) || isSelected(e.target)) {
            ctx.moveTo(e.source.x, e.source.y);
            ctx.lineTo(e.target.x, e.target.y);
          }
        }
      }
      ctx.stroke();
      ctx.globalAlpha = 1;
    }

    // Nodes
    for (const n of nodes) {
      if (!nodeVisible(n)) continue;
      const matched = matchesSearch(n);
      const isSel = isSelected(n);
      let r = nodeRadius(n);
      if (isSel) r = Math.max(r + 2, 8);
      else if (matched) r = Math.max(r + 1, 6);
      ctx.fillStyle = isSel || matched ? '#cf222e' : nodeColor(n);
      ctx.beginPath();
      ctx.arc(n.x, n.y, r * inv, 0, Math.PI * 2);
      ctx.fill();
    }

    // Label only for the primary (most recently picked) node.
    if (primary) {
      ctx.fillStyle = document.documentElement.dataset.theme === 'dark' ? '#ebe5d8' : '#1f2328';
      ctx.font = (12 * inv) + 'px -apple-system, sans-serif';
      ctx.textBaseline = 'middle';
      ctx.fillText(' ' + (primary.title || primary.slug),
        primary.x + (nodeRadius(primary) + 3) * inv, primary.y);
    }
    ctx.restore();
  }

  // Interaction
  let panning = false, panStart = null, panStartPx = null, panMoved = false;
  canvas.addEventListener('mousedown', ev => {
    panning = true; panMoved = false; canvas.classList.add('dragging');
    panStart = [ev.clientX - tx, ev.clientY - ty];
    panStartPx = [ev.clientX, ev.clientY];
  });
  window.addEventListener('mousemove', ev => {
    if (!panning) return;
    if (!panMoved && (Math.abs(ev.clientX - panStartPx[0]) > 3 || Math.abs(ev.clientY - panStartPx[1]) > 3)) {
      panMoved = true;
    }
    tx = ev.clientX - panStart[0]; ty = ev.clientY - panStart[1];
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
    tx = mx - wx * scale; ty = my - wy * scale;
    scheduleDraw();
  }, { passive:false });

  function selectNode(n, shift) {
    if (shift) {
      if (selected.has(n)) {
        selected.delete(n);
        if (primary === n) primary = selected.size ? [...selected][selected.size - 1] : null;
      } else {
        selected.add(n);
        primary = n;
      }
    } else {
      selected.clear();
      selected.add(n);
      primary = n;
    }
    renderInfo();
    scheduleDraw();
  }

  canvas.addEventListener('click', ev => {
    if (panMoved) return; // it was a drag, not a click
    const r = canvas.getBoundingClientRect();
    const [wx, wy] = screenToWorld(ev.clientX - r.left, ev.clientY - r.top);

    // 1. Try nodes (priority over edges).
    const pickRNode = 12 / scale;
    let pickedNode = null, bestNodeD2 = pickRNode * pickRNode;
    for (const n of nodes) {
      if (!nodeVisible(n)) continue;
      const d2 = (n.x - wx) ** 2 + (n.y - wy) ** 2;
      if (d2 < bestNodeD2) { bestNodeD2 = d2; pickedNode = n; }
    }
    if (pickedNode) { selectNode(pickedNode, ev.shiftKey); return; }

    // 2. Try edges (only currently-visible ones).
    const pickRedge2 = (6 / scale) ** 2;
    let pickedEdge = null, bestEdgeD2 = pickRedge2;
    if (show.decls) {
      for (const e of edges) {
        const d2 = pointToSegmentDist2(wx, wy, e.source.x, e.source.y, e.target.x, e.target.y);
        if (d2 < bestEdgeD2) { bestEdgeD2 = d2; pickedEdge = e; }
      }
    }
    if (show.links) {
      for (const e of linkEdges) {
        const d2 = pointToSegmentDist2(wx, wy, e.source.x, e.source.y, e.target.x, e.target.y);
        if (d2 < bestEdgeD2) { bestEdgeD2 = d2; pickedEdge = e; }
      }
    }
    if (pickedEdge) {
      selected.clear();
      selected.add(pickedEdge.source);
      selected.add(pickedEdge.target);
      primary = pickedEdge.source;
      renderInfo();
      scheduleDraw();
      return;
    }

    // 3. Empty click → clear.
    selected.clear();
    primary = null;
    renderInfo();
    scheduleDraw();
  });

  // Info panel
  const info = document.getElementById('info');
  function pct(num, den) { return den ? Math.round(100 * num / den) : 0; }
  function docUrl(decl) {
    return `https://leanprover-community.github.io/mathlib4_docs/find/?pattern=${encodeURIComponent(decl)}`;
  }
  function renderInfo() {
    const sel = [...selected];
    if (sel.length === 0) {
      info.innerHTML = '<p class="empty">Click an article, an edge, or shift-click multiple articles to compare what they share.</p>';
      return;
    }
    if (sel.length >= 2) {
      renderMulti(sel);
      return;
    }
    const n = sel[0];
    const f = n.n_formalized, p = n.n_partial, nf = n.n_not_formalized, tot = n.n_total;
    const wpUrl = `/${encodeURIComponent(n.slug)}`;
    const wikiUrl = `https://en.wikipedia.org/wiki/${encodeURIComponent(n.slug)}`;

    // Co-cited articles for this node from current edges, sorted by shared.
    const neigh = [];
    for (const e of edges) {
      if (e.source.id === n.id) neigh.push({ other: e.target, shared: e.shared });
      else if (e.target.id === n.id) neigh.push({ other: e.source, shared: e.shared });
    }
    neigh.sort((x, y) => y.shared - x.shared);

    const parts = [];
    parts.push(`<h3><a href="${wpUrl}">${esc(n.title)}</a></h3>`);
    parts.push(`<div class="slug">${esc(n.slug)} · ${n.dominant_ns ? esc(n.dominant_ns) : 'no namespace'}</div>`);
    parts.push(`<div class="bar"><i class="f" style="width:${pct(f,tot)}%"></i><i class="p" style="width:${pct(p,tot)}%"></i><i class="n" style="width:${pct(nf,tot)}%"></i></div>`);
    parts.push(`<div class="field"><b style="color:#2da44e">${f}</b> formalized · <b style="color:#d29922">${p}</b> partial · <b style="color:#cf222e">${nf}</b> not · <b>${tot}</b> total</div>`);
    parts.push(`<div class="field"><b>Decls referenced</b> · ${n.n_decls}</div>`);
    if (n.top_namespaces && n.top_namespaces.length) {
      parts.push(`<div class="field"><b>Top Mathlib areas</b><ul>${
        n.top_namespaces.map(ns => `<li>Mathlib.${esc(ns)}</li>`).join('')
      }</ul></div>`);
    }
    if (neigh.length) {
      const top = neigh.slice(0, 10);
      parts.push(`<div class="field"><b>Co-cited articles (≥k=${kMin})</b><ul>${
        top.map(({other, shared}) =>
          `<li><a href="#" data-slug="${esc(other.slug)}">${esc(other.title)}</a> · <span style="color:#57606a">${shared}</span></li>`
        ).join('')
      }${neigh.length > 10 ? `<li class="hint">+ ${neigh.length - 10} more</li>` : ''}</ul></div>`);
    }

    const wikiNeigh = (linksByNode.get(n.id) || []).slice();
    if (wikiNeigh.length) {
      // Sort: mutual links first, then alphabetical.
      wikiNeigh.sort((x, y) => (y.mutual - x.mutual) || x.other.title.localeCompare(y.other.title));
      const top = wikiNeigh.slice(0, 10);
      parts.push(`<div class="field"><b>Wikipedia-linked articles</b><ul>${
        top.map(({other, mutual}) =>
          `<li><a href="#" data-slug="${esc(other.slug)}">${esc(other.title)}</a>${mutual ? ' · <span style="color:#4c8bf5">↔</span>' : ''}</li>`
        ).join('')
      }${wikiNeigh.length > 10 ? `<li class="hint">+ ${wikiNeigh.length - 10} more</li>` : ''}</ul></div>`);
    }
    parts.push('<div class="links">');
    parts.push(`<a href="${wpUrl}">WikiLean article →</a>`);
    parts.push(`<a href="${wikiUrl}" target="_blank" rel="noopener">Wikipedia →</a>`);
    parts.push('</div>');
    info.innerHTML = parts.join('');

    info.querySelectorAll('a[data-slug]').forEach(el => {
      el.addEventListener('click', e => {
        e.preventDefault();
        const t = nodeById.get(el.dataset.slug);
        if (t) selectNode(t, e.shiftKey);
      });
    });
  }

  function renderMulti(sel) {
    const declSets = sel.map(n => new Set(n.decls || []));
    let inter = new Set(declSets[0]);
    for (let i = 1; i < declSets.length; i++) {
      inter = new Set([...inter].filter(d => declSets[i].has(d)));
    }
    const onlyCount = sel.map((n, i) => {
      const others = new Set();
      for (let j = 0; j < declSets.length; j++) if (j !== i) for (const d of declSets[j]) others.add(d);
      let c = 0; for (const d of declSets[i]) if (!others.has(d)) c++;
      return c;
    });

    const parts = [];
    parts.push(`<h3>Comparing ${sel.length} articles</h3>`);
    parts.push('<div class="chips">' + sel.map(n =>
      `<span class="chip"><a href="#" data-slug="${esc(n.slug)}">${esc(n.title)}</a><a href="#" data-rm="${esc(n.slug)}" class="rm" title="Remove from selection">✕</a></span>`
    ).join('') + '</div>');

    parts.push(`<div class="field"><b>Shared by all ${sel.length}:</b> ${inter.size} decl${inter.size === 1 ? '' : 's'}</div>`);
    if (inter.size > 0) {
      const sortedInter = [...inter].sort();
      parts.push('<ul class="declist">');
      for (let i = 0; i < Math.min(sortedInter.length, 80); i++) {
        const d = sortedInter[i];
        const mod = declModules[d] || '';
        parts.push(`<li><a href="${docUrl(d)}" target="_blank" rel="noopener"><code>${esc(d)}</code></a>${mod ? ` <span class="mod">${esc(mod)}</span>` : ''}</li>`);
      }
      if (sortedInter.length > 80) parts.push(`<li class="hint">+ ${sortedInter.length - 80} more</li>`);
      parts.push('</ul>');
    } else {
      parts.push('<p class="hint" style="margin:6px 0 14px">No decls common to all selected articles. Try removing one, or pick more closely-related articles.</p>');
    }

    parts.push('<div class="field"><b>Per-article counts</b><ul>');
    sel.forEach((n, i) => {
      parts.push(`<li>${esc(n.title)} · ${n.decls.length} total · <span style="color:#57606a">${onlyCount[i]} unique to it</span></li>`);
    });
    parts.push('</ul></div>');

    parts.push('<p class="hint" style="margin-top:12px">Shift-click another node to add · click a chip name to focus that one · ✕ to remove.</p>');

    info.innerHTML = parts.join('');
    info.querySelectorAll('a[data-slug]').forEach(el => {
      el.addEventListener('click', e => {
        e.preventDefault();
        const t = nodeById.get(el.dataset.slug);
        if (t) selectNode(t, e.shiftKey);
      });
    });
    info.querySelectorAll('a[data-rm]').forEach(el => {
      el.addEventListener('click', e => {
        e.preventDefault();
        const t = nodeById.get(el.dataset.rm);
        if (!t) return;
        selected.delete(t);
        if (primary === t) primary = selected.size ? [...selected][selected.size - 1] : null;
        renderInfo();
        scheduleDraw();
      });
    });
  }

  function esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));
  }

  // Edge-source toggles + slider
  const slider = document.getElementById('k-slider');
  const kVal = document.getElementById('k-val');
  const visDecls = document.getElementById('visible-decls');
  const visLinks = document.getElementById('visible-links');

  function applyK() {
    kMin = parseInt(slider.value, 10);
    kVal.textContent = kMin;
    edges = allEdges.filter(e => e.shared >= kMin);
    visDecls.textContent = edges.length.toLocaleString();
    sim.force('link').links(edges);
    sim.alpha(0.4).restart();
  }
  slider.addEventListener('input', applyK);
  document.getElementById('show-decls').addEventListener('change', e => {
    show.decls = e.target.checked;
    scheduleDraw();
  });
  document.getElementById('show-links').addEventListener('change', e => {
    show.links = e.target.checked;
    scheduleDraw();
  });
  visLinks.textContent = linkEdges.length.toLocaleString();

  // Search
  document.getElementById('search').addEventListener('input', e => {
    searchTerm = e.target.value.toLowerCase().trim();
    scheduleDraw();
  });

  // Legend (click to filter)
  const legend = document.getElementById('legend');
  const counts = {};
  for (const n of nodes) counts[n.dominant_ns] = (counts[n.dominant_ns] || 0) + 1;
  const ordered = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  for (const [ns, c] of ordered) {
    const color = NS_COLORS[ns] || NS_OTHER;
    const row = document.createElement('div');
    row.style.cursor = 'pointer';
    row.style.gridColumn = '1 / -1';
    row.style.display = 'grid';
    row.style.gridTemplateColumns = '14px auto auto';
    row.style.gap = '4px 8px';
    row.style.alignItems = 'center';
    row.style.padding = '1px 0';
    row.dataset.ns = ns || '';
    row.innerHTML = `<span class="sw" style="background:${color}"></span>
                     <span class="nm">${ns ? esc(ns) : 'other'}</span>
                     <span class="ct">${c}</span>`;
    row.addEventListener('click', () => {
      nsFilter = (nsFilter === ns) ? null : ns;
      [...legend.children].forEach(c => {
        c.style.opacity = (!nsFilter || c.dataset.ns === nsFilter) ? '1' : '0.4';
      });
      scheduleDraw();
    });
    legend.appendChild(row);
  }

  // Stats
  const totalAnn = nodes.reduce((s, n) => s + n.n_total, 0);
  const totalF = nodes.reduce((s, n) => s + n.n_formalized, 0);
  const totalP = nodes.reduce((s, n) => s + n.n_partial, 0);
  document.getElementById('stats').innerHTML = `
    <div class="stat"><span>Articles</span><span class="v">${nodes.length.toLocaleString()}</span></div>
    <div class="stat"><span>Annotations</span><span class="v">${totalAnn.toLocaleString()}</span></div>
    <div class="stat"><span>Formalized</span><span class="v">${totalF.toLocaleString()} (${Math.round(100*totalF/totalAnn)}%)</span></div>
    <div class="stat"><span>Partial</span><span class="v">${totalP.toLocaleString()} (${Math.round(100*totalP/totalAnn)}%)</span></div>
    <div class="stat"><span>Shared-decl edges</span><span class="v">${allEdges.length.toLocaleString()}</span></div>
    <div class="stat"><span>Wikipedia link edges</span><span class="v">${linkEdges.length.toLocaleString()}</span></div>`;

  visDecls.textContent = edges.length.toLocaleString();

  resize();
  window.addEventListener('resize', resize);

  // Theme toggle — flip dataset.theme, persist, and redraw so the canvas
  // node-label ink updates without a reload.
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


if __name__ == "__main__":
    main()
