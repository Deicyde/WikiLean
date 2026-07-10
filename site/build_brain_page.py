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
  · v2      — unit cards (concept ∘ article ∘ decls ∘ xrefs as one identity),
              a Sources accordion (Wikidata / Wikipedia lead / external-DB
              snippets), first-class ext nodes (xref:<db>:<id>, db-ringed
              bubbles), the `links` edge kind, facet-chip node filters over
              the f bitmask (state in the URL hash), and the cross-ref
              Explorer view over views/xref_explorer.json. All of it
              feature-detects: pre-v2 shard data still renders.

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
html, body { height:100%; overflow:hidden; }   /* app canvas — no page scrollbar; the wheel zooms */
body { margin:0; height:100vh; display:flex; flex-direction:column;
  background:#0b0e14; color:#e6e4de;
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
#structstat { color:#7f8a9c; font-size:.78rem; font-style:italic; white-space:nowrap; }
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
.main { display:flex; flex:1 1 auto; min-height:0; }   /* fills the space the chrome leaves — no magic numbers */
#stage { flex:1 1 62%; position:relative; background:#0b0e14; overflow:hidden;
  cursor:grab; touch-action:none; }
#stage.grabbing { cursor:grabbing; }
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
/* evidence, rendered as prose instead of a JSON dump */
.ev { font-size:.82rem; line-height:1.5; color:#2b2822; }
.ev .lead { margin:0 0 4px; }
.ev .lead b { color:#0d0c0a; }
.ev code { background:#efe8d6; padding:0 3px; border-radius:3px;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.85em; }
.ev-list { list-style:none; margin:5px 0; padding:0; }
.ev-list li { padding:2px 0 2px 14px; position:relative; }
.ev-list li::before { content:"–"; position:absolute; left:1px; color:#a99f86; }
.ev-sub { margin:5px 0; color:#4a463d; font-size:.8rem; }
.ev .stat { font-weight:600; }
.ev .stat.formalized { color:#116329; }
.ev .stat.partial { color:#7d5e00; }
.ev .stat.not_formalized { color:#a12621; }
.ev .attrib { margin-top:8px; border-top:1px solid #e3dac4; padding-top:6px;
  color:#4a463d; font-size:.76rem; }
.ev .attrib .prov { border:none; padding:0; margin:0 4px 0 0; font-weight:700;
  font-family:-apple-system,sans-serif; }
.ev .pin { color:#8a8272; }
.rawtoggle { font-size:.7rem; color:#8a8272; cursor:pointer; margin-top:6px;
  font-family:-apple-system,sans-serif; user-select:none; }
.rawtoggle:hover { color:#5a544a; }
.rawjson { margin:4px 0 0 !important; }
.dirarrow { color:#8a8272; font-weight:600; }
/* community connections — user/API-submitted edges (Project 2) */
section.kind.community h3 { border-bottom-color:#c9b98a; }
.cedge { border:1px solid #ddd4bd; border-radius:6px; margin-bottom:6px; padding:6px 10px;
  background:#fdfbf4; font-size:.84rem; display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
.cedge .ctarget { font-weight:600; color:#0d0c0a; }
.cedge .mk { color:#6d28d9; font-size:.72rem; font-style:italic; }
.cprov { font-size:.66rem; border-radius:8px; padding:0 6px; border:1px solid #c8bfa8;
  color:#5a544a; margin-left:auto; white-space:nowrap; font-family:-apple-system,sans-serif; }
.cprov.human { border-color:#1a7f37; color:#116329; }
.cprov.ai { border-color:#c2540a; color:#9a3f00; }
.cprov.machine { border-color:#6d28d9; color:#5b21b6; }
.cshared { margin-top:8px; border-top:1px dashed #d8cfb8; padding-top:8px; }
.cshared h4 { margin:0 0 6px; font-size:.86rem; color:#0d0c0a; font-weight:700; }
.cshared h4 .cnt { color:#8a8272; font-weight:400; font-size:.78rem; }
.cedge.cinferred { background:#f6f1e5; border-style:dashed; }
.cdel { border:none; background:none; color:#a12621; cursor:pointer; font-size:1.05rem;
  line-height:1; padding:0 2px; font-family:-apple-system,sans-serif; }
.cdel:hover { color:#7d1a16; }
.cnote { flex-basis:100%; color:#4a463d; font-size:.78rem; font-style:italic; }
.caddform { margin-top:8px; }
.caddform summary { cursor:pointer; color:#1a4b8f; font-size:.85rem; user-select:none; }
.cform { display:flex; flex-direction:column; gap:7px; margin-top:8px; padding:9px;
  border:1px solid #ddd4bd; border-radius:6px; background:#fbf8ef; }
.cform label { font-size:.76rem; color:#4a463d; display:flex; flex-direction:column; gap:2px;
  font-family:-apple-system,sans-serif; }
.cf-opt { color:#8a8272; font-weight:400; }
.cform input, .cform select { padding:4px 6px; border:1px solid #c8bfa8; border-radius:4px;
  font-size:.82rem; background:#fff; color:#151310; font-family:inherit; }
.cf-hits { max-height:150px; overflow:auto; }
.cf-hit { padding:3px 6px; cursor:pointer; font-size:.8rem; border-radius:4px; }
.cf-hit:hover { background:#efe8d6; }
.cf-hit .t { color:#8a8272; font-size:.7rem; margin-left:4px; }
#cf-submit { align-self:flex-start; padding:4px 13px; background:#1a4b8f; color:#fff; border:none;
  border-radius:5px; cursor:pointer; font-size:.82rem; font-family:-apple-system,sans-serif; }
#cf-submit:disabled { opacity:.5; cursor:default; }
#cf-msg { font-size:.76rem; }
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
/* facet-filter chips ("Show only") + the Explorer view toggle */
.fchip { padding:2px 10px; border:1px solid #33405c; border-radius:12px; background:#0b0e14;
  color:#9aa3b2; font-size:.78rem; cursor:pointer; font-family:inherit; }
.fchip:hover { border-color:#38bdf8; color:#e6e4de; }
.fchip.on { background:#173753; border-color:#38bdf8; color:#cdeafe; }
#filterstat { color:#7f8a9c; font-size:.78rem; font-style:italic; }
/* the atomic-unit card: one identity strip for a concept */
.unitcard { border:1px solid #d8cfb8; border-radius:8px; background:#fdfbf4;
  padding:12px 14px 8px; margin-bottom:12px; }
.unitcard h2 { margin:0 0 4px; }
.uc-desc { color:#3d382e; font-size:.9rem; margin:0 0 8px; }
.uc-src { color:#8a8272; font-size:.7rem; margin-left:6px; font-style:italic; }
.uc-primary { font-size:.66rem; color:#116329; border:1px solid #1a7f37; border-radius:8px;
  padding:0 5px; font-family:-apple-system,sans-serif; }
/* Sources accordion + snippet rows (TeX stays raw — no math renderer ships) */
.srcacc summary { cursor:pointer; color:#1a4b8f; font-size:.85rem; user-select:none; }
.srcrow { border:1px solid #ddd4bd; border-radius:6px; background:#fbf8ef; margin:8px 0;
  padding:8px 10px; }
.srchead { font-size:.84rem; margin-bottom:4px; }
.snip { font-size:.86rem; line-height:1.5; color:#2b2822; }
.srclic { margin-top:6px; border-top:1px solid #e3dac4; padding-top:4px; color:#8a8272;
  font-size:.7rem; font-family:-apple-system,sans-serif; }
.snipblock { margin:8px 0; border:1px solid #ddd4bd; border-radius:6px; background:#fbf8ef;
  padding:8px 10px; }
.snipblock .src { display:block; color:#8a8272; font-size:.7rem; margin-top:6px; }
body.embed .wl-header, body.embed #crumbbar { display:none; }   /* flex column fills the rest */
/* On a phone the stage + panel stack and the PAGE scrolls again (no fixed
   viewport to pan within), so restore normal document overflow there. */
@media (max-width: 900px) {
  html, body { overflow:auto; height:auto; }
  body { height:auto; }
  .main { flex-direction:column; }
  #stage { min-height:52vh; border-left:none; touch-action:auto; }
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
    <a class="wl-navlink" href="/stats">Stats</a>
    <a class="wl-navlink" href="https://github.com/Deicyde/WikiLean" rel="noopener">GitHub</a>
    <span class="wl-navlink" id="wl-auth"><a href="/login?returnTo=/brain">Log in</a></span>
  </nav>
</header>
<div class="toolbar">
  <span class="grp"><b>View</b>
    <button id="explorerbtn" class="fchip" title="the cross-ref explorer — every tagged &amp; cross-referenced node and the edges among them, one force-directed graph">Explorer</button>
    <button id="flattenbtn" class="fchip" title="flatten this folder: with a facet chip active, show EVERY matching declaration in the whole subtree as one flat layer; without one, show the next layer down">Flatten</button>
  </span>
  <span class="grp"><b>Layers</b>
    <label><input type="checkbox" data-k="depends" checked> formal deps</label>
    <label><input type="checkbox" data-k="formalizes" checked> formalizations</label>
    <label><input type="checkbox" data-k="xref,links" checked> cross-refs</label>
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
    <label title="Rank dependency edges by affinity (lift = observed ÷ expected flow) instead of raw volume, so genuine mathematical links survive the cut and shared infrastructure (everything-uses-Mathlib) recedes. Corrects the hub bias of arXiv 2604.24797. Bites hardest inside a dense library, where it changes which edges are shown."><input type="checkbox" id="dehub" checked> de-hub (lift)</label>
    <label title="Tint each bubble by its dependency-flow community — clusters of areas that lean on each other, regardless of where the folder tree files them (arXiv 2604.24797's Finding 1). Dive into a library to watch its modules regroup."><input type="checkbox" id="commColor" checked> logical communities</label>
    <span class="note" id="structstat" title="what the Structure toggles are doing at this level"></span>
  </span>
  <span class="grp"><b>Provenance</b>
    <label title="community/human-curated: Wikidata properties &amp; claims, @[wikidata]/@[stacks]/@[kerodon] attributes written in Mathlib source"><input type="checkbox" data-p="human" checked> human</label>
    <label title="machine-verified: kernel-extracted dependencies and the file tree — checked by the Lean compiler, no judgment involved"><input type="checkbox" data-p="machine" checked> machine</label>
    <label title="AI-generated: agent-proposed concept matches (skeptic-reviewed), LLM-judged paper matches (TheoremGraph), pipeline annotations"><input type="checkbox" data-p="ai" checked> AI</label>
  </span>
  <span class="grp"><b>Show only</b>
    <button class="fchip" data-fbit="1" title="declarations carrying a gold @[wikidata] tag in the Mathlib source">@[wikidata]</button>
    <button class="fchip" data-fbit="2" title="declarations carrying an @[stacks] tag">@[stacks]</button>
    <button class="fchip" data-fbit="4" title="declarations carrying an @[kerodon] tag">@[kerodon]</button>
    <button class="fchip" data-fbit="8" title="nodes with at least one cross-database reference">cross-refs</button>
    <button class="fchip" data-fbit="16" title="formalized concepts">formalized</button>
    <button class="fchip" data-fbit="64" title="concepts with a WikiLean article">article</button>
    <button class="fchip" data-fbit="512" title="cross-referenced in LMFDB">LMFDB</button>
    <button class="fchip" data-fbit="1024" title="cross-referenced in nLab">nLab</button>
    <button class="fchip" data-fbit="2048" title="cross-referenced in MathWorld">MathWorld</button>
    <button class="fchip" data-fbit="4096" title="cross-referenced in ProofWiki">ProofWiki</button>
    <button class="fchip" data-fbit="8192" title="cross-referenced by a Stacks Project tag">Stacks</button>
    <button class="fchip" data-fbit="16384" title="cross-referenced in the OEIS">OEIS</button>
    <span class="note" id="filterstat"></span>
  </span>
  <span class="grp"><a id="srcbtn2" style="cursor:pointer"
    title="every external database the brain links to — layer, provenance, license">Sources</a></span>
  <span class="note" id="status">loading manifest…</span>
</div>
<div id="crumbbar"></div>
<div class="main">
  <div id="stage"><svg id="svg"></svg>
    <div class="hint">scroll to zoom · drag to pan · click a bubble to dive in ·
      background to go up · arrows point along dependencies · click any edge
      for its evidence · <span style="color:#a78bfa">formal deps</span> ·
      <span style="color:#38bdf8">formalizes</span> ·
      <span style="color:#fbbf24">wikidata relations</span> ·
      <span style="color:#f472b6">cross-database</span> ·
      <span style="color:#84cc16">page links</span> ·
      <span style="color:#fb923c">literature</span> ·
      <span style="color:#2dd4bf">matches</span> ·
      dots = concepts (blue) / decls (green) · tinted regions = logical communities ·
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
  // `links` = a hyperlink mechanically extracted from the source database's
  // own pages (and its CC0-anchored concept projection) — no judgment involved
  if (kind === "depends" || kind === "contains" || kind === "links") return "machine";
  if (((prov && prov.method) || "").includes("@[")) return "human";
  if (ev && ev.source_tagged) return "human";   // gold pair reached via another path
  if (kind === "xref" || kind === "xref-shared" || kind === "relates") return "human";
  return "ai";
}
const PROV_TITLE = {
  human: "human-curated (Wikidata property/claim or a source attribute in Mathlib)",
  machine: "machine-verified (Lean kernel / file tree / mechanically-extracted page links)",
  ai: "AI-generated (agent-proposed or LLM-judged), verified by oracle + skeptic",
};

// ============================ canvas state ==================================
let focusId = null;        // container id (or LIBS_ID) whose children fill the stage
let selectedId = null;     // node the panel shows / ring highlights
let layout = null;         // {items: Map(id -> {x,y,r,item}), root}
let explorerOn = false;    // the cross-ref Explorer view (views/xref_explorer.json)
let flattenOn = false;     // flatten: one flat layer of the focus subtree
let flatNote = "";         // status-line text for the flattened level
let filterMask = 0;        // facet-filter bitmask over node `f` (0 = no filter)
let currentUser = null;    // {id, name, role} once /api/auth/me resolves (community edits)
const svg = d3.select("#svg");
// One <g> holds the whole scene so free pan/zoom is a single transform on it,
// layered UNDER the semantic click-to-descend. Everything drawn (edges,
// bubbles, overlays, labels) lives inside it and therefore pans/zooms together.
const gViewport = svg.append("g").attr("class", "viewport");
const gEdges = gViewport.append("g");
const gBubbles = gViewport.append("g");
const gOverlay = gViewport.append("g");
const gLabels = gViewport.append("g");
const defs = svg.append("defs");

// directed edge kinds get an arrowhead pointing at the dependency/target end
const DIRECTED = new Set(["depends", "contains", "cites", "mentions", "formalizes", "links"]);
function ensureMarker(color) {
  const id = "arw_" + color.replace(/[^a-z0-9]/gi, "");
  if (defs.select("#" + id).empty()) {
    defs.append("marker").attr("id", id).attr("viewBox", "0 0 10 10")
      .attr("refX", 8.5).attr("refY", 5).attr("markerWidth", 5.5).attr("markerHeight", 5.5)
      .attr("orient", "auto-start-reverse")
      .append("path").attr("d", "M0.6,1 L9,5 L0.6,9 Z").attr("fill", color);
  }
  return "url(#" + id + ")";
}

// Free pan/zoom over the canvas: scroll wheel zooms, drag pans (the /map feel).
// A pan must not read as a background click (which zooms out to the parent), so
// we swallow the click that follows a real drag.
let panMoved = false;
const isPhone = () => window.matchMedia && window.matchMedia("(max-width: 900px)").matches;
const zoomBehav = d3.zoom().scaleExtent([0.35, 16])
  // On a phone the page scrolls (the stack layout); d3-zoom's touch handlers
  // call preventDefault, which would trap a vertical swipe started over the
  // >50vh stage. Reject every gesture below 900px so native scroll wins there.
  // Desktop keeps the default gating (allow wheel, ignore ctrl/secondary-button).
  .filter(ev => !isPhone() && (!ev.ctrlKey || ev.type === "wheel") && !ev.button)
  .on("start", ev => { panMoved = false;
    if (ev.sourceEvent && ev.sourceEvent.type === "mousedown") stageEl.classList.add("grabbing"); })
  .on("zoom", ev => { if (ev.sourceEvent && ev.sourceEvent.type === "mousemove") panMoved = true;
    gViewport.attr("transform", ev.transform); })
  .on("end", () => stageEl.classList.remove("grabbing"));
svg.call(zoomBehav).on("dblclick.zoom", null);
// every fresh level fits the viewport — discard any lingering pan/zoom
function resetZoom() { svg.call(zoomBehav.transform, d3.zoomIdentity); }

const CONCEPT_COLOR = {formalized: "#3b82f6", partial: "#eab308", not_formalized: "#ef4444"};
// ext nodes (external-database pages) wear their db's ring color + short badge
const DB_COLOR = {lmfdb_knowl: "#facc15", nlab: "#4ade80", mathworld: "#f87171",
  proofwiki: "#60a5fa", stacks: "#f97316", kerodon: "#22d3ee", oeis: "#a3e635",
  dlmf: "#c084fc", eom: "#fb7185", planetmath: "#34d399", metamath: "#94a3b8",
  msc: "#eab308"};
const DB_ABBR = {lmfdb_knowl: "LMF", nlab: "nLab", mathworld: "MW", proofwiki: "PW",
  stacks: "St", kerodon: "Ker", oeis: "OEIS", dlmf: "DLMF", eom: "EoM",
  planetmath: "PM", metamath: "MM", msc: "MSC"};
const extDbOf = id => id.split(":")[1] || "";
const extValueOf = id => id.split(":").slice(2).join(":");
function fillFor(item, depthShade) {
  if (item.type === "concept") return CONCEPT_COLOR[item.status] || "#0969da";
  if (item.type === "decl") return "#22c55e";
  if (item.type === "strays") return "#8c959f";
  if (item.type === "external") return "#f472b6";
  if (item.type === "ext") return "#1b2436";   // dark fill; the db ring carries color
  if (item.type === "literature") return "#d4a72c";
  return depthShade;   // container
}

// ---- flatten: one flat layer of the focus subtree ---------------------------
// With a facet filter active: EVERY matching declaration in the subtree, from
// labels.json rows (facet-bearing decls carry f + containment path p). Without
// a filter: the next layer down (children of child containers), capped.
// Returns null when flatten does not apply (falls through to the normal level).
async function flattenItems(id) {
  flatNote = "";
  const isRoot = id === LIBS_ID;
  const inSubtree = pp => isRoot || pp === id || (pp || "").startsWith(id + "/");
  if (filterMask) {
    const rows = (await ensureLabels()).filter(r =>
      r.type === "decl" && ((r.f || 0) & filterMask) !== 0 && inSubtree(r.p));
    flatNote = rows.length
      ? "flattened: " + rows.length + " matching declaration" + (rows.length === 1 ? "" : "s") +
        (isRoot ? " across all libraries" : " under " + id.slice(5))
      : "flattened: no matching declarations here — the tag facets (@[wikidata]/@[stacks]/@[kerodon]) live on declarations";
    return rows.slice(0, 800).map(r => ({id: r.id, label: r.label, type: "decl", f: r.f, n_decls: 1}));
  }
  if (isRoot) { flatNote = "flatten needs a library focus or an active facet chip"; return null; }
  const e = await getEntry(id);
  if (!e) return null;
  const kids = (e.children && e.children.first || []);
  const out = kids.filter(c => c.type !== "container").map(c => ({...c}));
  const conts = kids.filter(c => c.type === "container").slice(0, 48);
  await Promise.all(conts.map(async c => {
    const ce = await getEntry(c.id);
    for (const g of (ce && ce.children && ce.children.first) || []) out.push({...g});
  }));
  const cap = 500;
  flatNote = "flattened one level: " + Math.min(out.length, cap) + " nodes" +
    (out.length > cap ? " of " + out.length + " — add a facet chip to flatten all the way to declarations" : "");
  return out.slice(0, cap);
}

// children of the current focus, as pack-able items
async function focusItems(id) {
  if (flattenOn) {
    const flat = await flattenItems(id);
    if (flat) return flat;
  } else flatNote = "";
  if (id === LIBS_ID) {
    const kinds = activeLibKinds();
    return manifest.roots
      .filter(r => kinds.has(r.library_kind || "math"))
      .map(r => ({id: r.id, label: r.label, type: "container",
                  n_decls: r.n_decls || 1, n_concepts: 0,
                  ...(r.f !== undefined ? {f: r.f} : {}),
                  ...(r.fa !== undefined ? {fa: r.fa} : {})}));
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
    // an active facet filter must still see matching loose decls — only the
    // non-matching remainder collapses into the strays bubble
    const keep = filterMask ? decls.filter(d => ((d.f || 0) & filterMask) !== 0) : [];
    const rest = decls.length - keep.length;
    decls = keep.concat(rest > 0
      ? [{id: "__strays__", type: "strays",
          label: rest + " loose decls", n: rest}] : []);
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

const SHADE = "#22304d";   // container fill — the canvas is always dark
function drawNodes() {
  const leaves = layout.leaves;
  const bubbles = gBubbles.selectAll("circle.node").data(leaves, l => l.data.id);
  bubbles.exit().remove();
  const entered = bubbles.enter().append("circle")
    .attr("class", l => l.data.type === "container" ? "bubble node" : "dot node");
  entered.append("title");
  entered.merge(bubbles)
    .attr("cx", l => l.x).attr("cy", l => l.y)
    .attr("r", l => Math.max(l.r, 2.5))
    .attr("fill", l => fillFor(l.data, SHADE))
    .attr("fill-opacity", l => l.data.dim ? 0.15
      : l.data.type === "container" ? 0.55 : l.data.ghost ? 0.35 : 0.9)
    // ext bubbles wear their database's ring — inline style, because the
    // .dot/.bubble CSS stroke would override a presentation attribute
    .style("stroke", l => l.data.type === "ext"
      ? (DB_COLOR[l.data.db || extDbOf(l.data.id)] || "#f472b6") : null)
    .style("stroke-width", l => l.data.type === "ext" ? "2px" : null)
    .on("click", (ev, l) => { ev.stopPropagation(); nodeClick(l.data); })
    .select("title").text(l => l.data.label
      + (l.data.type === "ext"
        ? ` — ${XREF_NAME[l.data.db || extDbOf(l.data.id)] || "external"} page` : "")
      + (l.data.n_decls ? ` — ${l.data.n_decls.toLocaleString()} decls` : "")
      + (l.data.ghost ? " — in Mathlib, not yet linked in the brain" : ""));
}

function drawLabels() {
  gLabels.selectAll("*").remove();
  // ego view lays labels flat under the node (edge-first); level views set them
  // inside/over the bubble (containment-first)
  const flat = layout && layout.ego;
  const ego = layout && layout.ego;
  for (const l of layout.leaves) {
    if (ego && l.data.type !== "container" && l.data.type !== "concept") {
      if (l.data.type === "ext") {   // db badge inside the ringed bubble
        const db = l.data.db || extDbOf(l.data.id);
        gLabels.append("text").attr("class", "blabel")
          .attr("x", l.x).attr("y", l.y + 2.5).attr("font-size", 6.5)
          .style("fill", DB_COLOR[db] || "#f472b6")
          .text(DB_ABBR[db] || db.slice(0, 3));
      }
      const raw = l.data.label || l.data.id;
      const short = l.data.type === "decl" && raw.includes(":")
        ? raw.split(":").pop().split(".").slice(-2).join(".") : raw;
      gLabels.append("text").attr("class", "blabel")
        .attr("x", l.x).attr("y", l.y + l.r + 10).attr("font-size", 9)
        .text(short.length > 28 ? short.slice(0, 26) + "…" : short);
      continue;
    }
    if (!ego && flattenOn && l.data.type === "decl" && layout.leaves.length <= 220) {
      // flattened declarations are the POINT of the view — label them like
      // ego neighbors (below the dot); past ~220 the text would shingle,
      // tooltips + the panel take over
      const raw = l.data.label || l.data.id;
      const short = raw.includes(":")
        ? raw.split(":").pop().split(".").slice(-2).join(".") : raw;
      gLabels.append("text").attr("class", "blabel")
        .attr("x", l.x).attr("y", l.y + l.r + 9).attr("font-size", 8.5)
        .text(short.length > 28 ? short.slice(0, 26) + "…" : short);
      continue;
    }
    if (l.data.type === "container") {
      if (!flat && l.r < 24) continue;
      const fs = flat ? 10 : Math.max(10, Math.min(16, l.r / 4.5));
      gLabels.append("text").attr("class", "blabel")
        .attr("x", l.x).attr("y", flat ? l.y + l.r + 11 : l.y - (l.r > 40 ? 4 : -4))
        .attr("font-size", fs).attr("opacity", l.data.dim ? 0.35 : null)
        .text(l.data.label);
      if (!flat && l.r > 40) {
        gLabels.append("text").attr("class", "bcount")
          .attr("x", l.x).attr("y", l.y + fs - 2).attr("font-size", fs * 0.72)
          .attr("opacity", l.data.dim ? 0.35 : null)
          .text(`${(l.data.n_decls || 0).toLocaleString()}${
            l.data.n_concepts ? " · " + l.data.n_concepts + "★" : ""}`);
      }
    } else if (l.data.type === "concept" && l.data.label && !/^Q\d+$/.test(l.data.label)) {
      if (!flat && l.r < 11) continue;
      gLabels.append("text").attr("class", "blabel")
        .attr("x", l.x).attr("y", l.y + Math.max(l.r, 3) + 10).attr("font-size", 9)
        .text(l.data.label.length > 26 ? l.data.label.slice(0, 24) + "…" : l.data.label);
    }
  }
}

// Ego view: a concept/decl/paper becomes the focus — it sits centered and
// EVERYTHING it links to expands around it (formalizing decls, related
// concepts, external database pages, papers, its home folders), laid out by a
// static force pass. Clicking a neighbor re-centers on it, so you can walk
// the Wikidata relation graph and the formal graph in one continuous motion.
// Same edgeStore pipeline as the level views: the Layers + Provenance toggles
// and edge evidence cards all apply.
const EGO_DIST = {formalizes: 95, relates: 135, links: 150, xref: 160, depends: 175,
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
      if (!kinds.has(x.kind) || !provs.has(pc)) continue;
      if (neigh.has(x.id)) continue;
      const t = idType(x.id);
      const nd = {id: x.id, type: t, label: x.id, dir, edge: x,
                  rank: SAT_RANK[x.kind] ?? 9};
      if (t === "ext") {
        // a REAL external-page node (shard-resolvable in v2 data) rendered as
        // a db-ringed bubble; pre-v2 xref edges still resolve db/value/url
        // from the id + evidence, so old data keeps working
        const db = extDbOf(x.id);
        nd.db = db;
        nd.label = x.evidence && x.evidence.value !== undefined
          ? `${XREF_NAME[db] || db}: ${x.evidence.value}` : extValueOf(x.id);
        nd.url = nodeUrl(x.id);
        if (x.kind === "xref") nd.rank = SAT_RANK.relates + 0.5;
      }
      neigh.set(x.id, nd);
    }
  }
  if (entry.node.article_annotations && entry.node.slug) {
    const aa = entry.node.article_annotations;
    neigh.set("article:" + entry.node.slug, {
      id: "article:" + entry.node.slug, type: "external", dir: "out",
      label: `WikiLean article — ${aa.total} annotations`,
      url: "/" + entry.node.slug,
      edge: {kind: "mentions", prov: 0,
             evidence: {role: "article", ...aa,
                        note: "the concept's annotated Wikipedia mirror"}},
      rank: -1});
  }
  let nodesArr = [...neigh.values()];
  let fstat = null;
  if (filterMask) {   // facet filter — resolve f lazily, then OR-match
    await Promise.all(nodesArr.map(async nd => {
      if (nd.f !== undefined) return;
      const ne2 = await getEntry(nd.id);
      if (ne2 && ne2.node && ne2.node.f !== undefined) nd.f = ne2.node.f;
    }));
    if (seq !== renderSeq) return;
    const total = nodesArr.length;
    const hasF = nodesArr.some(nd => nd.f !== undefined);
    if (hasF) nodesArr = nodesArr.filter(nd => ((nd.f || 0) & filterMask) !== 0);
    fstat = {active: true, shown: nodesArr.length, total, noF: !hasF};
  }
  updateFilterStat(fstat);
  nodesArr.sort((a, b) => a.rank - b.rank);
  if (nodesArr.length > 72) { skipped = nodesArr.length - 72; nodesArr = nodesArr.slice(0, 72); }

  const W = stageEl.clientWidth || 800, H = stageEl.clientHeight || 600;
  const R_BY_TYPE = {concept: 11, decl: 8, container: 13, literature: 7, external: 7, ext: 9};
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
    payload: nd.edge,
    from: nd.dir === "in" ? nd.id : id,   // arrow points the true way (in = neighbor → focus)
    to: nd.dir === "in" ? id : nd.id}));
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

// ---- facet filter (the f bitmask; SCHEMA.md v2) -----------------------------
// Chips OR together; a node shows iff (f & mask) != 0. Non-matching leaf nodes
// (and every edge touching them) drop out of the level; containers stay as
// dimmed context. Feature-detect: pre-v2 shard data carries no f — an active
// filter must then leave the canvas alone rather than blank it.
async function applyFacetFilter(items, seq) {
  for (const it of items) delete it.dim;
  if (!filterMask)
    return {items, shown: items.length, total: items.length, active: false};
  // children rows carry f when the data is v2-built; anchored concepts come
  // from edges and resolve theirs from the node payload (cached shards)
  await Promise.all(items.map(async it => {
    if (it.f !== undefined || it.type !== "concept") return;
    const ce = await getEntry(it.id);
    if (ce && ce.node && ce.node.f !== undefined) it.f = ce.node.f;
  }));
  const total = items.length;
  if (seq !== renderSeq) return {items, shown: total, total, active: true};
  if (!items.some(it => it.f !== undefined || it.fa !== undefined))
    return {items, shown: total, total, active: true, noF: true};
  const match = it => ((it.f || 0) & filterMask) !== 0;
  // a container also matches when its SUBTREE does (fa = aggregate facet
  // bits) — it stays bright and navigable so the user can descend to the
  // matching decls/concepts instead of staring at a fully-dimmed level
  const matchAgg = it => it.type === "container" &&
    (((it.f || 0) | (it.fa || 0)) & filterMask) !== 0;
  const kept = [];
  let shown = 0;
  for (const it of items) {
    if (it.type === "container") {
      it.dim = !(match(it) || matchAgg(it));
      kept.push(it);
      if (!it.dim) shown++;
    }
    else if (match(it)) { kept.push(it); shown++; }
  }
  return {items: kept, shown, total, active: true};
}
function updateFilterStat(fv) {
  const el = $("#filterstat");
  if (!el) return;
  if (flattenOn && flatNote) { el.textContent = flatNote; return; }
  el.textContent = !fv || !fv.active ? ""
    : fv.noF ? "facet data not in this build yet"
    : `showing ${fv.shown} of ${fv.total} nodes`;
}

async function renderFocus(anim) {
  if (explorerOn) return renderExplorer(anim);
  const seq = ++renderSeq;
  resetZoom();   // a fresh level is laid out to fit the stage — drop any pan/zoom
  if (focusId !== LIBS_ID) {
    const fe = await getEntry(focusId);
    if (seq !== renderSeq) return;
    if (fe && fe.node.type !== "container") return renderEgo(seq, fe, anim);
  }
  const items = await focusItems(focusId);
  if (seq !== renderSeq) return;
  const fv = await applyFacetFilter(items, seq);
  if (seq !== renderSeq) return;
  updateFilterStat(fv);
  const shownItems = fv.items;
  const W = stageEl.clientWidth || 800, H = stageEl.clientHeight || 600;
  const root = d3.hierarchy({children: shownItems}).sum(d => d.children ? 0 : packValue(d));
  d3.pack().size([W, H]).padding(shownItems.length > 150 ? 1.5 : 4)(root);
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
  links:        {color: "#84cc16", dash: "2 2",  label: "page link (external database)"},
};
let edgeStore = [];

// prefetch visible shards: grandchild previews, container rollup edges, and
// the ontology web among visible concepts/decls (formal + informal)
async function enrich(seq, leaves) {
  const visible = new Set(leaves.map(l => l.data.id));
  const containers = leaves.filter(l => l.data.type === "container");
  const store = new Map();   // kind|a|b -> edge
  // from/to carry the true dependency direction (a/b are only the dedup key
  // order); the heavier of the two directions wins and its arrow is drawn.
  const put = (kind, a, b, w, payload, from, to) => {
    const key = kind + "|" + (a < b ? a + "|" + b : b + "|" + a);
    const prev = store.get(key);
    if (!prev || w > prev.w) store.set(key, {kind, a, b, w, payload, from: from ?? a, to: to ?? b});
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
    // informal rollups: relates (Wikidata, human) + mentions (articles, AI)
    // aggregated between concept homes — the human/AI synapses between bubbles
    const inf = e.rollup && e.rollup.informal;
    if (inf) {
      for (const row of inf) {
        if (!visible.has(row.id)) continue;
        put(row.kind, l.data.id, row.id, row.count,
            {prov: row.prov, confidence: "high",
             evidence: {aggregated: true, count: row.count, sample_pairs: row.samples,
                        note: "concept-level " + row.kind + " flows between these areas"}},
            l.data.id, row.id);
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
          if (sig) put("depends", l.data.id, row.id, sig, row,
                        dir === "out" ? l.data.id : row.id,
                        dir === "out" ? row.id : l.data.id);
        }
      }
    }
  }));

  // the ontology web: every visible concept's edges to other visible nodes,
  // plus same-external-page pairs (two concepts both xref-ing one nLab/
  // MathWorld/LMFDB/… page — the cross-database fabric made visible)
  const concepts = leaves.filter(l => l.data.type === "concept" ||
    (flattenOn && l.data.type === "decl" && leaves.length <= 300));
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
        put(x.kind, l.data.id, x.id, w, x,
            dir === "out" ? l.data.id : x.id,
            dir === "out" ? x.id : l.data.id);
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
  const depAll = show.filter(e => e.kind === "depends");
  const dep = depAll.slice().sort((x, y) => rank(y) - rank(x)).slice(0, 250);
  const rest = show.filter(e => e.kind !== "depends");
  depCap = {shown: dep.length, total: depAll.length, dehub};
  const maxSig = dep.reduce((m, e) => Math.max(m, e.w), 1);
  const depOpacity = e => {
    const lf = liftOf(e);
    return dehub && lf !== null
      ? Math.min(0.9, Math.max(0.06, 0.06 + 0.17 * Math.log2(1 + lf)))
      : 0.16 + 0.3 * (e.w / maxSig);
  };
  // the Explorer view can carry thousands of edges — skip the fat hit twins
  // there so the DOM stays half the size (nodes remain clickable)
  const noHit = !!(layout && layout.explorer) && show.length > 1200;
  for (const e of [...dep, ...rest]) {
    const directed = DIRECTED.has(e.kind);
    // undirected kinds use a/b; directed kinds draw from source → target so the
    // arrowhead lands on the dependency/target end
    const A = layout.items.get(directed ? (e.from || e.a) : e.a);
    const B = layout.items.get(directed ? (e.to || e.b) : e.b);
    if (!A || !B) continue;
    if (A.data.dim || B.data.dim) continue;   // filtered-out context containers
    const mx = (A.x + B.x) / 2, my = (A.y + B.y) / 2;
    const dx = B.x - A.x, dy = B.y - A.y;
    // deterministic per-pair bend so parallel routes fan out instead of piling
    let h = 0;
    const hk = e.a + "|" + e.b + e.kind;
    for (let i = 0; i < hk.length; i++) h = (h * 31 + hk.charCodeAt(i)) >>> 0;
    const bend = (0.08 + (h % 1000) / 1000 * 0.22) * ((h & 1) ? 1 : -1);
    const cpx = mx - dy * bend, cpy = my + dx * bend;   // quadratic control point
    // trim the arrow end back to the node's rim so the head isn't buried
    let ex = B.x, ey = B.y;
    if (directed) {
      let tx = B.x - cpx, ty = B.y - cpy;               // tangent at the end ≈ B - control
      const tl = Math.hypot(tx, ty) || 1;
      const back = Math.max(B.r || 3, 3) + 3.5;
      ex = B.x - (tx / tl) * back; ey = B.y - (ty / tl) * back;
    }
    const d = `M${A.x},${A.y} Q${cpx},${cpy} ${ex},${ey}`;
    const hitD = `M${A.x},${A.y} Q${cpx},${cpy} ${B.x},${B.y}`;
    const st = EDGE_STYLE[e.kind];
    const isDep = e.kind === "depends";
    const baseOp = isDep ? depOpacity(e) : 0.5;
    const p = gEdges.append("path").attr("class", "link")
      .attr("d", d).attr("fill", "none")
      .attr("stroke", st.color)
      .attr("stroke-width", isDep ? 0.6 + 2.6 * Math.sqrt(e.w / maxSig)
            : 1 + Math.min(2.2, Math.log2(1 + e.w) * 0.5))
      .attr("stroke-opacity", baseOp);
    if (st.dash) p.attr("stroke-dasharray", st.dash);
    if (directed) p.attr("marker-end", ensureMarker(st.color));
    // invisible fat twin = the click/hover target
    if (!noHit) gEdges.append("path").attr("class", "hit")
      .attr("d", hitD).attr("fill", "none")
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
  updateStructStat();
}

// Live readout of what the two Structure toggles are doing at this level, so the
// controls visibly earn their place: de-hub reports whether the depends-cap is
// biting (it only changes which edges show once there are >250 candidates —
// i.e. inside a dense library), and communities reports the cluster count.
let depCap = {shown: 0, total: 0, dehub: true};
let commState = {n: 0, reason: ""};
function updateStructStat() {
  const el = $("#structstat");
  if (!el) return;
  const parts = [];
  const capped = depCap.total > depCap.shown;
  if (depCap.dehub)
    parts.push(capped ? `de-hub: top ${depCap.shown} of ${depCap.total} deps by affinity`
                      : "de-hub: weighting deps by affinity");
  else
    parts.push(capped ? `raw volume: top ${depCap.shown} of ${depCap.total} deps`
                      : "raw volume");
  if (commState.reason === "off") parts.push("communities off");
  else if (commState.reason === "nodeps") parts.push("communities need formal deps");
  else if (commState.reason === "sparse") parts.push("one community here");
  else if (commState.reason === "ok")
    parts.push(`${commState.n} logical ${commState.n === 1 ? "community" : "communities"}`);
  el.textContent = parts.join(" · ");
}

// blend two #rrggbb colors, t=0 → a, t=1 → b
function mix(a, b, t) {
  const ch = (h, i) => parseInt(h.slice(1 + 2 * i, 3 + 2 * i), 16);
  const hx = x => Math.round(x).toString(16).padStart(2, "0");
  return "#" + [0, 1, 2].map(i => hx(ch(a, i) + (ch(b, i) - ch(a, i)) * t)).join("");
}

// Logical-community coloring of the visible level: greedy modularity merging
// over the sibling depends graph (lift-corrected weights). Makes the paper's
// Finding 1 visible — where dependency communities cut across the folder tree.
const COMM_PALETTE = ["#f2711c", "#3fb950", "#58a6ff", "#d2a8ff", "#e3b341",
                      "#ff7b72", "#39c5cf", "#a5d6ff", "#ffa657", "#bc8cff"];
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
  // clear pass: restore the base dark fill + drop any prior community ring
  gBubbles.selectAll("circle.bubble")
    .attr("stroke", null).attr("stroke-width", null).attr("stroke-opacity", null)
    .attr("fill", l => fillFor(l.data, SHADE));
  if (!$("#commColor").checked) { commState = {n: 0, reason: "off"}; return; }
  if (!activeKinds().has("depends")) { commState = {n: 0, reason: "nodeps"}; return; }
  const ids = conts.map(l => l.data.id);
  const idset = new Set(ids);
  const links = edgeStore.filter(e => e.kind === "depends"
      && idset.has(e.a) && idset.has(e.b))
    .map(e => ({a: e.a, b: e.b, w: e.w * (liftOf(e) || 1)}));
  const comm = communitiesOf(ids, links);
  if (!comm) { commState = {n: 0, reason: "sparse"}; return; }
  const sizes = new Map();
  for (const c of comm.values()) sizes.set(c, (sizes.get(c) || 0) + 1);
  const colorOf = new Map();
  let ci = 0;
  gBubbles.selectAll("circle.bubble").each(function(l) {
    if (l.data.dim) return;   // filtered-out context stays dim, not tinted
    const c = comm.get(l.data.id);
    if (c === undefined || sizes.get(c) < 2) return;
    if (!colorOf.has(c)) colorOf.set(c, COMM_PALETTE[ci++ % COMM_PALETTE.length]);
    const col = colorOf.get(c);
    // a colored region (fill wash) + a bright ring, both scaled so the grouping
    // reads against the dark bubbles instead of vanishing into a thin outline
    d3.select(this).attr("fill", mix(SHADE, col, 0.34))
      .attr("stroke", col)
      .attr("stroke-width", Math.max(2, Math.min(4.5, (l.r || 6) * 0.07)))
      .attr("stroke-opacity", 0.95);
  });
  commState = {n: colorOf.size, reason: colorOf.size ? "ok" : "sparse"};
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
  // directed kinds read source → target; undirected read A ↔ B
  const directed = DIRECTED.has(e.kind);
  const fromId = directed ? (e.from || e.a) : e.a;
  const toId = directed ? (e.to || e.b) : e.b;
  panelEl.innerHTML = `
    <h2 style="font-size:1.05rem">${esc(st.label)}</h2>
    <div class="sub"><span style="color:${st.color}">●</span> ${esc(e.kind)}
      · <span class="prov ${epc}" style="margin-left:0" title="${esc(PROV_TITLE[epc])}">${epc}</span>${
      liftOf(e) !== null ? ` · lift ${liftOf(e)}× vs null model` : ""}</div>
    <div class="chips">
      <span class="chip"><a data-nav="${esc(fromId)}">${esc(name(fromId))}</a></span>
      <span class="chip dirarrow">${directed ? "→" : "↔"}</span>
      <span class="chip"><a data-nav="${esc(toId)}">${esc(name(toId))}</a></span>
    </div>
    <section class="kind"><h3>Evidence</h3>
      <div class="edge open"><div class="drawer" style="display:block">${
        evidenceProse(e.kind, ev, eprov, null, e.b)}</div></div>
    </section>
    <p class="note">Every line on the canvas is a stored brain edge${
      directed ? " — the arrowhead points from the source to what it depends on / joins to" : ""}.
    Click either endpoint to inspect that node.</p>`;
  panelEl.querySelectorAll("[data-nav]").forEach(a =>
    a.addEventListener("click", () => navigate(a.dataset.nav)));
  bindRawToggles();
}

// overlay: the selected node's ontology edges to visible endpoints
const OV_COLOR = {formalizes: "#38bdf8", xref: "#f472b6", cites: "#fb923c",
                  matches: "#2dd4bf", relates: "#fbbf24", mentions: "#94a3b8",
                  depends: "#a78bfa", links: "#84cc16"};
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
      if (!T || T.data.dim) continue;
      // point the arrow the true way: an incoming edge is neighbour → selection
      const directed = DIRECTED.has(x.kind);
      const F = dir === "in" && directed ? T : S;
      const G = dir === "in" && directed ? S : T;
      let gx = G.x, gy = G.y;
      if (directed) {
        let tx = G.x - F.x, ty = G.y - F.y; const tl = Math.hypot(tx, ty) || 1;
        const back = Math.max(G.r || 3, 3) + 3;
        gx = G.x - (tx / tl) * back; gy = G.y - (ty / tl) * back;
      }
      const color = OV_COLOR[x.kind] || "#57606a";
      const ov = gOverlay.append("path").attr("class", "ov")
        .attr("d", `M${F.x},${F.y} L${gx},${gy}`)
        .attr("stroke", color)
        .attr("stroke-width", 1.6).attr("stroke-opacity", 0.8);
      if (directed) ov.attr("marker-end", ensureMarker(color));
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
const SAT_RANK = {relates: 0, formalizes: 1, matches: 2, xref: 2.5, depends: 3,
                  cites: 4, mentions: 5, links: 6};
function idType(id) {
  if (/^Q\d+$/.test(id)) return "concept";
  if (id.startsWith("decl:")) return "decl";
  if (id.startsWith("lit:")) return "literature";
  if (id.startsWith("path:")) return "container";
  if (id.startsWith("xref:")) return "ext";   // external-DB page — a real node (v2)
  return "external";
}
async function drawSatellites() {
  gOverlay.selectAll("g.sat").remove();
  if (!selectedId || !layout || layout.ego || layout.explorer) return;
  const S = layout.items.get(selectedId);
  const e = await getEntry(selectedId);
  if (!S || !e) return;
  const kinds = activeKinds(), provs = activeProv();
  const seen = new Set();
  const cand = [];
  for (const dir of ["out", "in"]) {
    for (const x of (e.edges && e.edges[dir]) || []) {
      if (!kinds.has(x.kind)) continue;   // xref satellites = ext nodes (v2)
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
    const t = idType(s.x.id);
    const db = t === "ext" ? extDbOf(s.x.id) : null;
    g.append("circle").attr("cx", sx).attr("cy", sy).attr("r", 7)
      .attr("fill", t === "concept" ? "#0969da" : t === "decl" ? "#1a7f37"
            : t === "literature" ? "#bf5af2" : t === "ext" ? "#151b28" : "#8250df")
      .attr("fill-opacity", 0.92)
      .attr("stroke", t === "ext" ? (DB_COLOR[db] || "#f472b6") : "#fff")
      .attr("stroke-width", t === "ext" ? 2 : 1.2);
    if (t === "ext") g.append("text").attr("class", "blabel")
      .attr("x", sx).attr("y", sy + 2.5).attr("font-size", 6)
      .style("fill", DB_COLOR[db] || "#f472b6")
      .text(DB_ABBR[db] || db.slice(0, 3));
    const rawLab = t === "ext"
      ? `${XREF_NAME[db] || db}: ${extValueOf(s.x.id)}` : s.x.id;
    const label = g.append("text").attr("class", "blabel")
      .attr("x", sx).attr("y", sy + 17).attr("font-size", 9)
      .text(rawLab.length > 22 ? rawLab.slice(0, 20) + "…" : rawLab);
    getEntry(s.x.id).then(se => {
      if (se && se.node.label && se.node.label !== s.x.id)
        label.text(se.node.label.length > 26 ? se.node.label.slice(0, 24) + "…" : se.node.label);
    });
    g.append("title").text(`${s.x.kind} · ${s.x.id} — click to open`);
  });
}

// ============================ zoom navigation ================================
// The URL hash carries the whole shareable view state:
//   #<node-id>&f=<facet mask>&view=explorer
// The id segment is fully URI-encoded (any raw "&" became %26), so splitting
// on "&" is safe and pre-v2 "#<id>" hashes parse unchanged.
function setHash(id) {
  let h = "#" + (id && id !== LIBS_ID ? encodeURIComponent(id) : "");
  if (filterMask) h += "&f=" + filterMask;
  if (explorerOn) h += "&view=explorer";
  if (flattenOn) h += "&flat=1";
  history.replaceState(null, "", h);
}
function parseHash() {
  const parts = location.hash.slice(1).split("&");
  let id = parts[0] || "";
  try { id = decodeURIComponent(id); } catch (e) { /* malformed — keep raw */ }
  const out = {id, f: 0, view: "", flat: false};
  for (const kv of parts.slice(1)) {
    const i = kv.indexOf("=");
    const k = i < 0 ? kv : kv.slice(0, i), v = i < 0 ? "" : kv.slice(i + 1);
    if (k === "f") out.f = (parseInt(v, 10) || 0) & 0xffff;
    else if (k === "view") out.view = v;
    else if (k === "flat") out.flat = v !== "0";
  }
  return out;
}
function setExplorer(on) {
  explorerOn = on;
  const b = $("#explorerbtn");
  if (b) b.classList.toggle("on", on);
}
function setFlatten(on) {
  flattenOn = on;
  const b = $("#flattenbtn");
  if (b) b.classList.toggle("on", on);
}

async function zoomInto(id) {
  // slick part: scale the clicked bubble up to fill the stage, then swap levels.
  // Drive it through the pan/zoom transform so it composes with (and replaces)
  // any manual pan the user has applied — L.x/L.y are always identity-space.
  const L = layout && layout.items.get(id);
  if (L) {
    const W = stageEl.clientWidth, H = stageEl.clientHeight;
    const k = Math.min(W, H) / (L.r * 2.2);
    const t = d3.zoomIdentity.translate(W / 2 - L.x * k, H / 2 - L.y * k).scale(k);
    const groups = [gEdges, gBubbles, gOverlay, gLabels];
    // race the transition against a timer: rAF pauses in background tabs and
    // the cleanup below must ALWAYS run
    await Promise.race([
      Promise.all([
        svg.transition().duration(420).ease(d3.easeCubicInOut)
          .call(zoomBehav.transform, t).end().catch(() => {}),
        ...groups.map(g =>
          g.transition().duration(g === gBubbles ? 420 : 300)
            .attr("opacity", g === gBubbles ? 0.35 : 0).end().catch(() => {})),
      ]),
      new Promise(r => setTimeout(r, 700)),
    ]);
    // hold the fading scene invisible across the async re-layout so no stale
    // frame flashes when renderFocus snaps the viewport back to identity
    groups.forEach(g => { g.interrupt(); g.attr("opacity", g === gBubbles ? 0.35 : 0); });
  }
  focusId = id;
  setHash(id);
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
    setHash(home);
    await renderFocus(true);
    return;
  }
  const bc = (e && e.breadcrumb) || [];
  const parent = bc.length > 1 ? bc[bc.length - 2].id : LIBS_ID;
  focusId = parent;
  selectedId = null;
  setHash(parent);
  await renderFocus(true);
}
svg.on("click", () => {
  if (panMoved) { panMoved = false; return; }
  if (layout && layout.explorer) return;   // explorer: background = nothing
  zoomOut();
});

async function nodeClick(item) {
  if (layout && layout.explorer) {   // explorer: select + panel, stay put
    selectedId = item.id;
    renderPanel(item.id);
    drawSelRing();
    return;
  }
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
  if (item.type === "ext") {               // external-page node: travel there
    const ee = await getEntry(item.id);
    if (ee) { await zoomInto(item.id); return; }
    selectedId = item.id;                  // pre-v2 shards: panel + deep link only
    renderPanel(item.id);
    return;
  }
  if (item.type === "external") {          // article pseudo-node → its page
    if (safeUrl(item.url)) window.open(safeUrl(item.url), "_blank", "noopener");
    return;
  }
  // concept/decl/paper: zoom in and expand its whole neighborhood (ego view)
  await zoomInto(item.id);
}

// land the canvas on any node id: containers focus themselves; leaves focus
// their parent container and select themselves
async function navigate(id) {
  if (explorerOn) setExplorer(false);   // navigation = travel to the node's home
  const e = await getEntry(id);
  if (!e) { renderPanel(id); return; }
  focusId = id;                    // containers → level view; others → ego view
  selectedId = e.node.type === "container" ? null : id;
  setHash(id);
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
        setHash(""); renderFocus(true); }
      else navigate(a.dataset.nav);
    }));
}

// ============================ panel ==========================================
const KIND_LABEL = {
  formalizes: "Formalizations", mentions: "Article mentions", depends: "Formal dependencies",
  matches: "Formal ↔ literature matches", xref: "Cross-database identity",
  relates: "Wikidata relations", cites: "Stated in the literature (TheoremGraph)",
  contains: "Contains", links: "Links to",
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
// external-data urls are template-built by the ingest adapters, but never
// trust a stored url into an href without a scheme check (javascript:/data:
// would ride through esc() untouched)
function safeUrl(u) { return u && /^https?:\/\//i.test(u) ? u : null; }
function nodeUrl(id) {
  if (id.startsWith("decl:")) return "/decl/" + encodeURIComponent(id.slice(id.indexOf(":", 5) + 1));
  if (id.startsWith("xref:")) {
    const mkUrl = XREF_URL[extDbOf(id)];
    return (mkUrl && mkUrl(extValueOf(id))) || null;
  }
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

// ---- evidence, in plain English --------------------------------------------
// The drawer used to dump raw JSON. Instead we say what the edge ASSERTS and
// where it came from — one sentence, plus the structured bits (annotation
// samples, dependency witnesses, judge verdicts) rendered legibly. The raw
// object stays one click away for anyone who wants it.
const STATUS_WORD = {formalized: "formalized", partial: "partially formalized",
  not_formalized: "not yet formalized"};
function statusChip(s) {
  return `<span class="stat ${esc(s || "")}">${esc(STATUS_WORD[s] || s || "unknown")}</span>`;
}
const evList = items => items.length
  ? `<ul class="ev-list"><li>${items.join("</li><li>")}</li></ul>` : "";
const shortDecl = s => String(s).split(".").slice(-2).join(".");
function pairText(p) {
  const a = Array.isArray(p) ? p[0] : (p && (p.a ?? p[0]));
  const b = Array.isArray(p) ? p[1] : (p && (p.b ?? p[1]));
  return b !== undefined ? `${esc(a)} <span class="dirarrow">↔</span> ${esc(b)}` : esc(p);
}
function judgeVerdict(ev) {
  const verd = [ev.gpt54, ev.deepseek].filter(Boolean);
  const agree = verd.length === 2 && verd[0] === verd[1];
  const label = agree ? verd[0] : (verd.includes("exact") ? "a partial" : (verd[0] || "a"));
  const sim = typeof ev.sim === "number" ? ` (cosine similarity ${ev.sim.toFixed(2)})` : "";
  return `two independent LLM judges rated it <b>${esc(label)}</b> match${sim}`;
}
// friendly names for the manifest provenance vocabulary (source/method)
const SRC_NICE = {
  annotations: "WikiLean article annotations",
  mathlib_deps: "the Lean kernel dependency graph",
  wikidata_props: "Wikidata properties &amp; claims",
  theoremgraph: "the TheoremGraph corpus (arXiv 2606.25363)",
  mathlib: "Mathlib source",
};
function provAttribHtml(kind, ev, prov) {
  // deterministic field-of-study altitude links aren't an AI proposal — label
  // them honestly even though the coarse provenance filter groups them as "ai"
  if (prov && prov.method === "container_links") {
    const pin = prov.pin ? `<span class="pin"> · snapshot ${esc(String(prov.pin).slice(0, 10))}</span>` : "";
    return `<div class="attrib"><span class="prov machine">Deterministic</span> (a field-of-study concept mapped to the Mathlib area that formalizes it) · from Wikidata field-of-study + the library tree${pin}</div>`;
  }
  const pc = provClass(kind, prov, ev);
  const who = {human: "Human-curated", machine: "Machine-verified", ai: "AI-generated"}[pc];
  const gloss = {
    human: "written by a person",
    machine: kind === "links"
      ? "mechanically extracted from the source's own pages, no judgment involved"
      : "certified by the Lean compiler, no human or AI judgment",
    ai: "proposed by an AI agent, checked against the Mathlib oracle + a skeptic",
  }[pc];
  let src = "";
  if (pc === "machine") {
    // machine edges come from the kernel / file tree regardless of which rollup
    // file happened to carry them — never mislabel a formal dep as "TheoremGraph"
    src = kind === "contains" ? "the library file tree"
      : kind === "links" ? "the external database's own hyperlinks"
      : "Mathlib's kernel dependency graph";
  } else if (prov) {
    src = SRC_NICE[prov.source] || String(prov.source || "").replace(/_/g, " ");
    if (prov.method === "wikidata-property" && XREF_NAME[prov.source])
      src = XREF_NAME[prov.source] + " (via a Wikidata external-ID property)";
    else if (prov.method === "wikidata-claims") src = "Wikidata claims";
    else if (String(prov.method || "").includes("@["))
      src = String(prov.method).replace(/\s*\(mathlib4 source\)/, "").trim() + " in Mathlib source";
  }
  const pin = prov && prov.pin ? `<span class="pin"> · snapshot ${esc(String(prov.pin).slice(0, 10))}</span>` : "";
  return `<div class="attrib"><span class="prov ${pc}">${who}</span> (${esc(gloss)})${
    src ? ` · from ${src}` : ""}${pin}</div>`;
}
// the sentence + structured detail for one edge; otherId (when known) names
// the far endpoint so ext-page edges can say WHICH database they live in
function evidenceProse(kind, ev, prov, dir, otherId) {
  ev = ev || {};
  const inbound = dir === "in";
  let lead = "", detail = "";

  if (kind === "depends") {
    lead = (ev.aggregated || ev.top_witnesses)
      ? `<b>Formal dependency.</b> The Lean proofs in one area reference declarations in the other — read straight off Mathlib's compiled kernel graph.`
      : (inbound
          ? `<b>Formal dependency.</b> Declarations elsewhere use the declaration here in their proofs.`
          : `<b>Formal dependency.</b> The proof here uses the declaration on the other side.`);
    const wt = ev.w_types || {}, bits = [];
    if (wt.sig) bits.push(`${wt.sig.toLocaleString()} statement-level references`);
    if (wt.proof) bits.push(`${wt.proof.toLocaleString()} uses inside proofs`);
    if (wt.def) bits.push(`${wt.def.toLocaleString()} uses in definitions`);
    if (typeof ev.lift === "number")
      bits.push(`${ev.lift}× the volume a random graph of the same shape predicts — ${ev.lift >= 1.5 ? "a genuine affinity" : ev.lift >= 0.8 ? "about as expected" : "mostly shared infrastructure"}`);
    detail += evList(bits);
    const wit = ev.top_witnesses || ev.witnesses;
    if (wit && wit.length)
      detail += `<div class="ev-sub">for example, <code>${esc(shortDecl(wit[0][0]))}</code> uses <code>${esc(shortDecl(wit[0][1]))}</code></div>`;
  } else if (kind === "formalizes") {
    if (ev.role) {                                   // annotation-sourced
      const n = ev.n_annotations || (ev.sample ? ev.sample.length : 1);
      lead = `<b>Formal ↔ informal join.</b> ${n} WikiLean article annotation${n > 1 ? "s" : ""} name this Lean declaration as the formalization of the concept.`;
      if (ev.sample && ev.sample.length)
        detail = evList(ev.sample.filter(s => s.label).map(s => `“${esc(s.label)}” — ${statusChip(s.status)}`));
    } else if ((prov && String(prov.method || "").includes("@[")) || ev.source_tagged) {
      lead = `<b>Formal ↔ informal join.</b> A person wrote this match into the Mathlib source as an <code>@[wikidata]</code> attribute — the declaration is asserted to formalize the concept.`;
      if (ev.match_kind) detail = `<div class="ev-sub">match type: <b>${esc(MK_LABEL[ev.match_kind] || ev.match_kind)}</b></div>`;
    } else if ((prov && prov.method === "container_links") || ev.match_kind === "field") {
      lead = `<b>Formal ↔ informal join.</b> This concept is a field of study, linked to the Mathlib area that formalizes it — a deterministic altitude link from Wikidata, not an AI guess.`;
    } else {
      // agent-grounded match: verified against the oracle; only claim skeptic
      // review when the provenance/evidence actually records it (never fabricate)
      const reviewed = (prov && String(prov.method || "").includes("verified")) ||
        (ev.skeptic && ev.skeptic !== "pending");
      lead = `<b>Formal ↔ informal join.</b> An AI agent proposed that this Lean declaration formalizes the concept, and the declaration was verified to exist in Mathlib${
        reviewed ? "; the match also passed skeptic review" : ""}.`;
      const d = [];
      if (ev.match_kind) d.push(`match type: <b>${esc(MK_LABEL[ev.match_kind] || ev.match_kind)}</b>`);
      if (ev.skeptic === "pending") d.push(`skeptic review: <b>pending</b>`);
      detail += evList(d);
      if (ev.grounding_note) detail += `<div class="ev-sub">“${esc(ev.grounding_note)}”</div>`;
    }
  } else if (kind === "relates") {
    if (ev.aggregated) {
      const c = ev.count || (ev.sample_pairs ? ev.sample_pairs.length : 0);
      lead = `<b>Wikidata relation.</b> Wikidata connects these two areas through ${c ? "<b>" + c + "</b> " : ""}concept-to-concept relation${c === 1 ? "" : "s"} (subclass-of, part-of, …).`;
      if (ev.sample_pairs && ev.sample_pairs.length)
        detail = evList(ev.sample_pairs.slice(0, 4).map(pairText));
    } else {
      lead = `<b>Wikidata relation.</b> Wikidata records a direct relationship between these two concepts.`;
      const props = ev.properties || [];
      if (props.length) detail = evList(props.map(p => `${esc(p.label || p.p)} <span class="pin">(${esc(p.p)})</span>`));
    }
  } else if (kind === "mentions") {
    if (ev.aggregated) {                              // area↔area rollup (concept homes)
      const c = ev.count || (ev.sample_pairs ? ev.sample_pairs.length : 0);
      lead = `<b>Article mentions.</b> WikiLean articles link these two areas through ${c ? "<b>" + c + "</b> " : ""}annotation-level mention${c === 1 ? "" : "s"} of declarations across the boundary.`;
      if (ev.sample_pairs && ev.sample_pairs.length)
        detail = evList(ev.sample_pairs.slice(0, 4).map(pairText));
    } else {
      const n = ev.n_annotations || ev.total || (ev.sample ? ev.sample.length : 1);
      lead = `<b>Article mention.</b> ${ev.role === "article"
        ? "This is the concept's annotated Wikipedia mirror on WikiLean, carrying"
        : "A WikiLean article cites this in"} <b>${n}</b> Lean annotation${n > 1 ? "s" : ""}.`;
      if (ev.sample && ev.sample.length)
        detail = evList(ev.sample.filter(s => s.label).slice(0, 4).map(s => `“${esc(s.label)}” — ${statusChip(s.status)}`));
      else if (ev.statuses)
        detail = evList(Object.entries(ev.statuses).map(([k, v]) => `${v} ${STATUS_WORD[k] || k}`));
    }
  } else if (kind === "xref-shared") {
    const key = ev.shared_page ? ev.shared_page.split(":")[1] : null;
    lead = `<b>Same object, two entries.</b> Both concepts point at the same page${key ? ` in <b>${esc(XREF_NAME[key] || key)}</b>` : ""}, so Wikidata treats them as the same object across databases.`;
  } else if (kind === "xref") {
    lead = `<b>Cross-database identity.</b> Wikidata records this concept in an external database${ev.value !== undefined ? ` as <code>${esc(ev.value)}</code>` : ""} — the same object, catalogued elsewhere.`;
    if (ev.property) detail = `<div class="ev-sub">via Wikidata property <span class="pin">${esc(ev.property)}</span></div>`;
  } else if (kind === "cites") {
    lead = `<b>Stated in the literature.</b> This result appears in the mathematical literature; ${judgeVerdict(ev)}.`;
    if (ev.via_decls && ev.via_decls.length)
      detail = `<div class="ev-sub">via ${ev.via_decls.slice(0, 3).map(d => `<code>${esc(shortDecl(d))}</code>`).join(", ")}</div>`;
  } else if (kind === "matches") {
    lead = `<b>Formal ↔ literature match.</b> A Lean declaration was matched to an informal statement in the literature; ${judgeVerdict(ev)}.`;
  } else if (kind === "links") {
    const db = ev.via || (otherId && otherId.startsWith && otherId.startsWith("xref:")
      ? extDbOf(otherId) : null);
    const dbName = db ? (XREF_NAME[db] || db) : "the external database";
    if (ev.projected) {
      lead = `<b>Projected link.</b> Two concepts joined through an internal link inside <b>${esc(dbName)}</b>'s own pages — the database's editors connected them.`;
      detail = `<div class="ev-sub">projected from ${esc(dbName)}: <code>${esc(ev.src_page ?? "?")}</code> <span class="dirarrow">→</span> <code>${esc(ev.dst_page ?? "?")}</code></div>`;
    } else {
      lead = `<b>Page link.</b> An internal link on ${esc(dbName)} — one page links to the other inside the database.`;
      if (ev.context) detail = `<div class="ev-sub">link context: <b>${esc(ev.context)}</b></div>`;
    }
  } else if (kind === "contains") {
    lead = `<b>Containment.</b> One directly contains the other in the library's folder tree.`;
  } else {
    lead = esc((EDGE_STYLE[kind] && EDGE_STYLE[kind].label) || kind);
  }

  const raw = `<div class="rawtoggle" data-raw>▸ source data</div><pre class="rawjson" style="display:none">${esc(JSON.stringify(ev, null, 1))}</pre>`;
  return `<div class="ev"><p class="lead">${lead}</p>${detail}${provAttribHtml(kind, ev, prov)}${raw}</div>`;
}
// wire the ▸ source-data disclosures inside a freshly-rendered panel
function bindRawToggles() {
  panelEl.querySelectorAll(".rawtoggle").forEach(t => t.addEventListener("click", () => {
    const pre = t.nextElementSibling;
    if (!pre) return;
    const open = pre.style.display !== "none";
    pre.style.display = open ? "none" : "block";
    t.textContent = (open ? "▸" : "▾") + " source data";
  }));
}

function edgeHtml(x, provTable, dir) {
  const ev = x.evidence || {};
  const mkv = ev.match_kind && (MK_LABEL[ev.match_kind] || ev.match_kind);
  let mk = mkv ? `<span class="mk">${esc(mkv)}</span>` : "";
  if (ev.n_annotations > 1) mk += ` <span class="lit-ref">×${ev.n_annotations} annotations</span>`;
  // arrow reflects real direction for directed kinds; undirected kinds get ↔
  const arrow = DIRECTED.has(x.kind) ? (dir === "in" ? "←" : "→") : "↔";
  let target = esc(x.id);
  if (x.id.startsWith("xref:")) {
    // ext endpoints are real nodes now — navigable in-brain, deep link beside
    const db = extDbOf(x.id);
    const val = ev.value !== undefined ? String(ev.value) : extValueOf(x.id);
    const url = nodeUrl(x.id)
      || (ev.value !== undefined && XREF_URL[db] && XREF_URL[db](ev.value)) || null;
    target = `<span class="nav" data-nav="${esc(x.id)}" style="color:#1a4b8f;cursor:pointer">${
      esc(`${XREF_NAME[db] || db}: ${val}`)}</span>`
           + (url ? ` <a class="extlink" href="${esc(url)}" rel="noopener" target="_blank">↗</a>` : "");
  } else {
    const u = nodeUrl(x.id);
    target = `<span class="nav" data-nav="${esc(x.id)}" style="color:#1a4b8f;cursor:pointer">${target}</span>`
           + (u ? ` <a class="extlink" href="${esc(u)}" rel="noopener" target="_blank">↗</a>` : "");
  }
  const prov = provTable[x.prov] || {};
  const pc = provClass(x.kind, prov, ev);
  return `<div class="edge"><div class="row"><span class="dirarrow">${arrow}</span> ${target} ${mk}
    <span class="prov ${pc}" title="${esc(PROV_TITLE[pc])}">${pc}${ev.skeptic === "pending" ? " · unreviewed" : ""}${ev.source_tagged ? " · @[wikidata]" : ""}</span></div>
    <div class="drawer">${evidenceProse(x.kind, ev, prov, dir, x.id)}</div></div>`;
}
let lastPanelId = null;
let lastLevelEdges = [];
async function renderPanel(id) {
  lastPanelId = id;
  const e = await getEntry(id);
  if (!e) {
    // not in the static shards — an ext page from pre-v2 data / an unminted
    // page, or a community-added Wikidata concept node
    if (id.startsWith("xref:")) return extFallbackPanel(id);
    if (/^Q\d+$/.test(id)) return renderCommunityNodePanel(id);
    panelEl.innerHTML = `<p class="note">Unknown node: ${esc(id)}</p>`;
    return;
  }
  const n = e.node, prov = manifest.prov;
  let html = "";
  if (e.breadcrumb) {
    html += `<div class="crumb">` + e.breadcrumb.map((b, i) =>
      i === e.breadcrumb.length - 1 ? esc(b.label)
        : `<a data-nav="${esc(b.id)}">${esc(b.label)}</a>`).join(" / ") + `</div>`;
  }
  // the atomic-unit card replaces the plain header when the build carries one
  const unit = n.type === "concept" && n.unit ? n.unit : null;
  if (unit) html += unitCardHtml(n, e);
  else html += `<h2>${esc(n.label || n.id)}</h2>`;
  const sub = [];
  if (n.type) sub.push(n.type);
  if (n.library_kind) sub.push(n.library_kind + " library");
  if (n.module) sub.push(esc(n.module));
  if (n.slug) sub.push(`<a href="/${esc(n.slug)}">WikiLean article</a>`);
  if (n.type === "concept") sub.push(`<a href="https://www.wikidata.org/wiki/${esc(n.id)}" rel="noopener" target="_blank">${esc(n.id)}</a>`);
  if (n.type === "container" && n.qid) sub.push(`<a href="https://www.wikidata.org/wiki/${esc(n.qid)}" rel="noopener" target="_blank">Wikidata ${esc(n.qid)}</a>`);
  if (n.type === "decl") sub.push(`<a href="${esc(nodeUrl(n.id))}" rel="noopener" target="_blank">${esc(n.library || "Mathlib")} docs ↗</a>`);
  if (n.type === "ext") {
    sub.push(`<span class="badge" style="border-color:${esc(DB_COLOR[n.db] || "#c8bfa8")}">${
      esc(XREF_NAME[n.db] || n.db || "external database")}</span>`);
    if (n.kind_hint) sub.push(esc(n.kind_hint));
    const xu = safeUrl(n.url) || nodeUrl(n.id);
    if (xu) sub.push(`<a href="${esc(xu)}" rel="noopener" target="_blank">page ↗</a>`);
  }
  if (!unit) html += `<div class="sub">${sub.join(" · ")}</div>`;
  // "Also in" — every external identity of this concept as one chip strip
  // (the /map concept-panel affordance): article, Wikidata, Google KG, and
  // each cross-referenced database, deep-linked. The unit card already covers
  // all of this, so it renders only without one.
  if (n.type === "concept" && !unit) {
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
  if (n.article_annotations && !unit) {
    const aa = n.article_annotations;
    html += `<div class="chips"><span class="chip"><a href="/${esc(n.slug)}">WikiLean article</a>:
      <b>${aa.total}</b> Lean annotations</span>
      <span class="badge f">${aa.formalized} formalized</span>
      <span class="badge p">${aa.partial} partial</span>
      <span class="badge n">${aa.not_formalized} not</span></div>`;
  }
  if (n.description && !unit) html += `<p style="font-size:.9rem">${esc(n.description)}</p>`;
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

  // ext node: the stored snippet (license permitting) or an honest deep link
  if (n.type === "ext") {
    const xu = safeUrl(n.url) || nodeUrl(n.id);
    const dbName = XREF_NAME[n.db] || n.db || "the source";
    if (n.snippet) {
      html += `<div class="snipblock"><div class="snip">${esc(n.snippet)}</div>
        <span class="src">${esc(n.snippet_license || "source license applies")}${
        xu ? ` · <a href="${esc(xu)}" rel="noopener" target="_blank">read at ${esc(dbName)} ↗</a>` : ""}</span></div>`;
    } else {
      html += `<p class="note">No content stored for ${esc(dbName)} pages (its
        license permits ids, titles and links only)${
        xu ? ` — <a href="${esc(xu)}" rel="noopener" target="_blank">read it at the source ↗</a>` : ""}.</p>`;
    }
    if (n.qid) html += `<div class="chips"><span class="chip"><a data-nav="${esc(n.qid)}">anchored concept ${esc(n.qid)}</a></span></div>`;
  }

  // concept: the Sources accordion — every stored content snippet in one place
  let srcAccIds = null;
  if (n.type === "concept") {
    srcAccIds = conceptSourceRefs(n, e);
    html += sourcesAccordionHtml(n, srcAccIds);
  }

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
  const order = ["formalizes:out", "formalizes:in", "xref:out", "xref:in",
                 "links:out", "links:in", "cites:out", "matches:out",
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
        && !layout.items.get(e2.a).data.dim && !layout.items.get(e2.b).data.dim
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
        const dd = DIRECTED.has(e2.kind);
        const l = dd ? (e2.from || e2.a) : e2.a, r = dd ? (e2.to || e2.b) : e2.b;
        html += `<div class="edge"><div class="row" data-lvledge="${i}">
          <span style="color:${st2.color}">●</span>
          <span>${esc(short(l))} <span class="dirarrow">${dd ? "→" : "↔"}</span> ${esc(short(r))}</span>
          <span class="mk">${esc(st2.label || e2.kind)}</span>${
          e2.kind === "depends" ? ` <span class="lit-ref">sig ${e2.w}</span>` : ""}</div></div>`;
      });
      html += `</section>`;
    }
  }
  html += `<div id="community-slot"></div>`;
  panelEl.innerHTML = html;
  panelEl.querySelectorAll("[data-nav]").forEach(a =>
    a.addEventListener("click", () => navigate(a.dataset.nav)));
  bindRawToggles();
  // Sources accordion loads its content on first open (Wikipedia lead +
  // ext-node snippets are on-demand fetches, never paid on panel render)
  const acc = panelEl.querySelector("#srcacc");
  if (acc) acc.addEventListener("toggle", () => {
    if (acc.open) populateSources(id, n, srcAccIds || []);
  });
  renderCommunity(id);   // async: overlay the live community edges + add-a-connection form
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

// ext id not in the shards (pre-v2 data, or an unminted frontier page):
// a minimal deep-link panel instead of "Unknown node"
function extFallbackPanel(id) {
  lastPanelId = id;
  const db = extDbOf(id), val = extValueOf(id);
  const url = nodeUrl(id);
  panelEl.innerHTML = `
    <h2 style="font-size:1.1rem">${esc(val || id)}</h2>
    <div class="sub">external page ·
      <span class="badge" style="border-color:${esc(DB_COLOR[db] || "#c8bfa8")}">${
      esc(XREF_NAME[db] || db || "external database")}</span></div>
    <p class="note">This external page isn't in the current shard build — ${
      url ? `<a href="${esc(url)}" rel="noopener" target="_blank">open it at the source ↗</a>` : "no deep link available"}.</p>`;
}

// ---- the atomic-unit card (SCHEMA.md v2 `unit`) -----------------------------
// One identity strip: label ∘ Wikidata description ∘ article ∘ Wikipedia ∘
// QID ∘ formalizing decls (primary first) ∘ containers ∘ external DB pages.
function unitCardHtml(n) {
  const u = n.unit;
  let h = `<div class="unitcard"><h2>${esc(u.label || n.label || n.id)}</h2>`;
  if (u.description)
    h += `<div class="uc-desc">${esc(u.description)}<span class="uc-src">— Wikidata (CC0)</span></div>`;
  const chips = [];
  const slug = (u.article && u.article.slug) || n.slug;
  const aa = (u.article && u.article.annotations) || n.article_annotations;
  if (slug) {
    let b = "";
    if (aa && typeof aa === "object")
      b = ` <span class="badge f">${aa.formalized ?? 0}</span><span class="badge p">${
        aa.partial ?? 0}</span><span class="badge n">${aa.not_formalized ?? 0}</span>`;
    else if (typeof aa === "number") b = ` <small>${aa} annotations</small>`;
    chips.push(`<span class="chip"><a href="/${esc(slug)}">WikiLean article</a>${b}</span>`);
  }
  const wslug = u.wikipedia_slug || slug;
  if (wslug) chips.push(`<span class="chip"><a href="https://en.wikipedia.org/wiki/${
    esc(wslug)}" rel="noopener" target="_blank">Wikipedia</a></span>`);
  chips.push(`<span class="chip"><a href="https://www.wikidata.org/wiki/${
    esc(u.qid || n.id)}" rel="noopener" target="_blank">${esc(u.qid || n.id)}</a></span>`);
  const primary = n.display && n.display.primary_decl;
  const decls = (u.decls || []).slice()
    .sort((a, b2) => (a.name === primary ? -1 : 0) - (b2.name === primary ? -1 : 0));
  for (const d of decls.slice(0, 12)) {
    const lib = (d.module || "Mathlib").split(".")[0] || "Mathlib";
    chips.push(`<span class="chip"><a data-nav="decl:${esc(lib)}:${esc(d.name)}">${
      esc(shortDecl(d.name))}</a>${
      d.match_kind ? ` <span class="mk">${esc(MK_LABEL[d.match_kind] || d.match_kind)}</span>` : ""}${
      d.name === primary ? ` <span class="uc-primary" title="display hint — never identity">primary</span>` : ""}</span>`);
  }
  if ((u.decls || []).length > 12)
    chips.push(`<span class="chip">+${u.decls.length - 12} more decls</span>`);
  for (const c of u.containers || [])
    chips.push(`<span class="chip"><a data-nav="${esc(c)}">${
      esc(c.startsWith("path:") ? c.slice(5) : c)}</a></span>`);
  for (const [db, arr] of Object.entries(u.xrefs || {})) {
    for (const x of (arr || []).slice(0, 4)) {
      const xid = x.id && String(x.id).startsWith("xref:") ? x.id : `xref:${db}:${x.id}`;
      const url = x.url || nodeUrl(xid);
      chips.push(`<span class="chip" style="border-color:${esc(DB_COLOR[db] || "#c8bfa8")}"><a data-nav="${
        esc(xid)}">${esc(XREF_NAME[db] || db)}: ${esc(x.label || extValueOf(xid))}</a>${
        url ? ` <a class="extlink" href="${esc(url)}" rel="noopener" target="_blank">↗</a>` : ""}</span>`);
    }
  }
  return h + `<div class="chips">${chips.join("")}</div></div>`;
}

// ---- Sources accordion: every content snippet attached to a concept ---------
// (a) Wikidata description (build-time), (b) Wikipedia lead (on-demand REST
// summary, CORS-safe, cached), (c) ext-node snippets from their shard entries
// (license-permitting dbs), plus plain deep-link rows for no-content dbs.
const wpLeadCache = new Map();   // slug -> Promise<extract|null>
function wikipediaLead(slug) {
  if (!wpLeadCache.has(slug)) {
    wpLeadCache.set(slug,
      fetch("https://en.wikipedia.org/api/rest_v1/page/summary/" + encodeURIComponent(slug))
        .then(r => (r.ok ? r.json() : null))
        .then(j => (j && j.extract) || null)
        .catch(() => null));
  }
  return wpLeadCache.get(slug);
}
function conceptSourceRefs(n, e) {
  const refs = [], seen = new Set();
  const u = n.unit;
  if (u && u.xrefs) {
    for (const [db, arr] of Object.entries(u.xrefs)) {
      for (const x of arr || []) {
        const xid = x.id && String(x.id).startsWith("xref:") ? x.id : `xref:${db}:${x.id}`;
        if (!seen.has(xid)) { seen.add(xid); refs.push(xid); }
      }
    }
  }
  for (const x of (e.edges && e.edges.out) || []) {
    if (x.kind !== "xref" || !x.id.startsWith("xref:") || seen.has(x.id)) continue;
    seen.add(x.id);
    refs.push(x.id);
  }
  return refs;
}
function sourcesAccordionHtml(n, extIds) {
  const slug = n.slug || (n.unit && n.unit.article && n.unit.article.slug);
  const desc = (n.unit && n.unit.description) || n.description;
  const count = (desc ? 1 : 0) + (slug ? 1 : 0) + extIds.length;
  if (!count) return "";
  return `<section class="kind"><h3>Sources <span class="cnt">(${count})</span></h3>
    <details class="srcacc" id="srcacc"><summary>what each database says about this concept</summary>
    <div id="srcacc-body"><p class="note">loading…</p></div></details></section>`;
}
// snippet text renders verbatim — inline $TeX$ stays raw (the page ships no
// math renderer); the serif .snip block keeps it readable
function srcRow(name, url, text, license, navId) {
  return `<div class="srcrow"><div class="srchead"><b>${esc(name)}</b>${
    navId ? ` <a data-nav="${esc(navId)}" style="cursor:pointer;font-size:.75rem">open node</a>` : ""}${
    url ? ` <a class="extlink" href="${esc(url)}" rel="noopener" target="_blank">↗</a>` : ""}</div>
    <div class="snip">${esc(text)}</div>${
    license ? `<div class="srclic">${esc(license)}</div>` : ""}</div>`;
}
function srcLinkRow(name, url, navId) {
  return `<div class="srcrow"><div class="srchead"><b>${esc(name)}</b>
    <a data-nav="${esc(navId)}" style="cursor:pointer;font-size:.75rem">open node</a>${
    url ? ` <a class="extlink" href="${esc(url)}" rel="noopener" target="_blank">↗</a>` : ""}</div>
    <div class="srclic">no stored content (license) — read it at the source</div></div>`;
}
async function populateSources(id, n, extIds) {
  const body = $("#srcacc-body");
  if (!body || body.dataset.loaded) return;
  body.dataset.loaded = "1";
  const rows = [];
  const desc = (n.unit && n.unit.description) || n.description;
  const qid = (n.unit && n.unit.qid) || n.id;
  if (desc) rows.push(srcRow("Wikidata", `https://www.wikidata.org/wiki/${qid}`, desc, "CC0"));
  body.innerHTML = rows.join("") || `<p class="note">loading…</p>`;
  const slug = n.slug || (n.unit && n.unit.article && n.unit.article.slug);
  if (slug) {
    const lead = await wikipediaLead(slug);
    if (lastPanelId !== id) return;
    if (lead) rows.push(srcRow("Wikipedia (lead)",
      `https://en.wikipedia.org/wiki/${slug}`, lead, "CC-BY-SA-4.0"));
  }
  for (const xid of extIds.slice(0, 12)) {
    const ee = await getEntry(xid);
    if (lastPanelId !== id) return;
    const db = extDbOf(xid);
    const nm = XREF_NAME[db] || db;
    const url = (ee && safeUrl(ee.node.url)) || nodeUrl(xid);
    if (ee && ee.node.snippet)
      rows.push(srcRow(nm, url, ee.node.snippet, ee.node.snippet_license || "", xid));
    else rows.push(srcLinkRow(nm, url, xid));   // no-content db → deep link out
  }
  if (extIds.length > 12)
    rows.push(`<p class="note">… +${extIds.length - 12} more cross-references (see the chips above)</p>`);
  if (lastPanelId !== id || !$("#srcacc-body")) return;
  $("#srcacc-body").innerHTML = rows.join("") || `<p class="note">no stored source content.</p>`;
  $("#srcacc-body").querySelectorAll("[data-nav]").forEach(a =>
    a.addEventListener("click", () => navigate(a.dataset.nav)));
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

// ============================ xref explorer ==================================
// The whole cross-referenced subgraph (views/xref_explorer.json — every
// tagged / cross-linked node + the edges among them) as ONE static-tick force
// layout: the cross-ref explorer for Mathlib. Facet chips, Layers and
// Provenance toggles all apply; clicking a node opens its panel; labels are
// capped by zoom so a few thousand nodes stay legible. Degrades to a status
// message when the view file hasn't been built yet.
let xdata = null;
async function fetchExplorerData() {
  if (xdata) return xdata;
  const get = () => fetch(BASE + "views/xref_explorer.json" + vq())
    .then(r => (r.ok ? r.json() : null)).catch(() => null);
  let j = await get();
  if (!j) {   // stale manifest → one re-sync + retry, same as getEntry
    try { await fetchManifest(); } catch (e) { return null; }
    j = await get();
  }
  xdata = j;
  return j;
}
async function renderExplorer(anim) {
  const seq = ++renderSeq;
  const j = await fetchExplorerData();
  if (seq !== renderSeq) return;
  if (!j || !(j.nodes || []).length) {
    setExplorer(false);
    setHash(focusId || "");   // drop the stale &view=explorer
    statusEl.textContent = "explorer data not built yet (views/xref_explorer.json)";
    return renderFocus(false);
  }
  resetZoom();
  selectedId = null;
  // tolerate {nodes, edges} or {nodes, links}; infer type/db/label from ids
  let nodesArr = (j.nodes || []).map(r => {
    const t = r.type || idType(r.id);
    return {id: r.id, type: t, f: r.f, status: r.status,
            db: r.db || (t === "ext" ? extDbOf(r.id) : undefined),
            label: r.label || (t === "ext" ? extValueOf(r.id) : r.id)};
  });
  const totalN = nodesArr.length;
  let noF = false;
  if (filterMask) {
    if (nodesArr.some(nd => nd.f !== undefined))
      nodesArr = nodesArr.filter(nd => ((nd.f || 0) & filterMask) !== 0);
    else noF = true;
  }
  updateFilterStat(filterMask
    ? {active: true, shown: nodesArr.length, total: totalN, noF} : null);
  const keep = new Set(nodesArr.map(nd => nd.id));
  const rawEdges = j.edges || j.links || [];
  let edges = [];
  for (const r of rawEdges) {
    const src = r.src ?? r.source ?? r.a, dst = r.dst ?? r.target ?? r.b;
    if (!keep.has(src) || !keep.has(dst)) continue;
    edges.push({src, dst, kind: r.kind || "links", prov: r.prov,
                evidence: r.evidence || {}, w: r.w || 1});
  }
  const totalE = edges.length;
  if (edges.length > 6000) edges = edges.slice(0, 6000);   // keep the DOM sane
  const deg = new Map();
  for (const ed of edges) {
    deg.set(ed.src, (deg.get(ed.src) || 0) + 1);
    deg.set(ed.dst, (deg.get(ed.dst) || 0) + 1);
  }
  const W = stageEl.clientWidth || 800, H = stageEl.clientHeight || 600;
  const R_T = {concept: 5, decl: 3.5, ext: 4, container: 6, literature: 4};
  const sims = nodesArr.map(nd => ({id: nd.id, nd,
    r: (R_T[nd.type] || 4) + Math.min(4, Math.sqrt(deg.get(nd.id) || 0) * 0.7)}));
  const links = edges.map(ed => ({source: ed.src, target: ed.dst}));
  const sim = d3.forceSimulation(sims)
    .force("link", d3.forceLink(links).id(d => d.id).distance(46).strength(0.3))
    .force("charge", d3.forceManyBody().strength(-26).distanceMax(260))
    .force("collide", d3.forceCollide(d => d.r + 2).iterations(1))
    .force("x", d3.forceX(W / 2).strength(0.05))
    .force("y", d3.forceY(H / 2).strength(0.07))
    .stop();
  const ticks = sims.length > 1500 ? 90 : 180;   // static ticks, renderEgo-style
  for (let i = 0; i < ticks; i++) sim.tick();
  if (seq !== renderSeq) return;
  const leaves = sims.map(sm => ({data: sm.nd, x: sm.x, y: sm.y, r: sm.r}));
  layout = {items: new Map(leaves.map(l => [l.data.id, l])), leaves, explorer: true};
  edgeStore = edges.map(ed => ({kind: ed.kind, a: ed.src, b: ed.dst, w: ed.w,
    payload: {kind: ed.kind, prov: ed.prov, evidence: ed.evidence},
    from: ed.src, to: ed.dst}));
  gEdges.selectAll("*").remove();
  gOverlay.selectAll("*").remove();
  gBubbles.selectAll("circle.preview").remove();
  drawNodes();
  drawExplorerLabels();
  renderEdges();
  crumbEl.innerHTML = `<a data-nav="${LIBS_ID}">all libraries</a>
    <span class="sep">/</span> <b>cross-ref explorer</b>`;
  crumbEl.querySelectorAll("[data-nav]").forEach(a =>
    a.addEventListener("click", () => {
      setExplorer(false); focusId = LIBS_ID; selectedId = null;
      setHash(""); renderFocus(true);
    }));
  statusEl.textContent = `explorer: ${nodesArr.length.toLocaleString()}${
    filterMask && !noF ? ` of ${totalN.toLocaleString()}` : ""} nodes · ${
    edges.length.toLocaleString()}${
    totalE > edges.length ? ` of ${totalE.toLocaleString()}` : ""} edges`;
  if (anim) {
    const g = [gEdges, gBubbles, gOverlay, gLabels];
    for (const gr of g) gr.attr("opacity", 0).transition().duration(260).attr("opacity", 1);
    setTimeout(() => g.forEach(gr => { gr.interrupt(); gr.attr("opacity", 1); }), 600);
  }
}
// labels capped by zoom: only the biggest nodes are labelled zoomed-out;
// zooming in reveals more (up to 250 text elements at any graph size)
function drawExplorerLabels() {
  gLabels.selectAll("*").remove();
  const ranked = layout.leaves.slice().sort((a, b) => b.r - a.r).slice(0, 250);
  ranked.forEach((l, i) => {
    const raw = l.data.label || l.data.id;
    gLabels.append("text").attr("class", "blabel xlab")
      .attr("x", l.x).attr("y", l.y + l.r + 8).attr("font-size", 8)
      .attr("data-rank", i)
      .text(raw.length > 24 ? raw.slice(0, 22) + "…" : raw);
  });
  updateExplorerLabels(d3.zoomTransform(svg.node()).k);
}
function updateExplorerLabels(k) {
  if (!layout || !layout.explorer) return;
  const lim = Math.min(250, Math.round(50 * k * k));
  gLabels.selectAll("text.xlab").attr("display", function () {
    return Number(this.dataset.rank) < lim ? null : "none";
  });
}
zoomBehav.on("zoom.xplabels", ev => {
  if (layout && layout.explorer) updateExplorerLabels(ev.transform.k);
});

// ============================ toolbar + boot =================================
document.querySelectorAll(".toolbar input").forEach(el =>
  el.addEventListener("change", () => {
    if (explorerOn) { renderExplorer(false); return; }
    if (el.dataset.lk && focusId === LIBS_ID) { renderFocus(false); return; }
    if (layout && layout.ego) { renderFocus(false); return; }
    renderEdges();
    drawSelRing();
    if (selectedId) renderPanel(selectedId);
    else if (focusId !== LIBS_ID) renderPanel(focusId);
  }));

// facet chips: OR together into filterMask; the state rides the URL hash
function syncChips() {
  document.querySelectorAll(".fchip[data-fbit]").forEach(b =>
    b.classList.toggle("on", (filterMask & Number(b.dataset.fbit)) !== 0));
}
document.querySelectorAll(".fchip[data-fbit]").forEach(b =>
  b.addEventListener("click", () => {
    filterMask ^= Number(b.dataset.fbit);
    syncChips();
    setHash(focusId || "");
    if (explorerOn) renderExplorer(false);
    else renderFocus(false);
  }));
$("#explorerbtn").addEventListener("click", () => {
  setExplorer(!explorerOn);
  setHash(focusId || "");
  if (explorerOn) renderExplorer(true);
  else renderFocus(true);
});
$("#flattenbtn").addEventListener("click", () => {
  setFlatten(!flattenOn);
  setHash(focusId || "");
  if (!explorerOn) renderFocus(true);
});

window.addEventListener("hashchange", () => {
  const h = parseHash();
  filterMask = h.f;
  syncChips();
  setFlatten(!!h.flat);
  if (h.view === "explorer") {
    setExplorer(true);
    renderExplorer(true).then(() => {
      if (h.id && explorerOn) { selectedId = h.id; renderPanel(h.id); drawSelRing(); }
    });
    return;
  }
  if (explorerOn) setExplorer(false);
  if (h.id) navigate(h.id);
  else renderFocus(false);
});
// Re-pack only on a real WIDTH change (the layout is width-driven), debounced.
// This skips the height-only resize storm a mobile URL bar fires on every
// scroll, and stops a stray resize from yanking a panned/zoomed desktop view.
let lastStageW = 0, resizeTimer = 0;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    const w = stageEl.clientWidth;
    if (Math.abs(w - lastStageW) < 2) return;
    lastStageW = w;
    renderFocus(false);
  }, 160);
});

// ======================= community connections (Project 2) ==================
// Live, user/API-submitted edges (docs/BRAIN-EDITS-ROADMAP.md). Fetched per-node
// from the D1 overlay and merged into the panel with an attribution + human/AI
// chip; logged-in users get an "add a connection" form and can delete any edge
// (soft-delete gravestone). All fetches degrade silently when the API is absent
// (e.g. the static preview), so the page still works read-only.
const COMMUNITY_KINDS_UI = [
  ["formalizes", "formalizes (concept ↔ Lean decl)"],
  ["relates", "relates (concept ↔ concept)"],
  ["xref", "cross-database link (LMFDB, nLab, …)"],
  ["mentions", "article mention"],
  ["matches", "formal ↔ literature match"],
  ["cites", "stated in the literature"],
];
const XREF_DB_OPTIONS = [
  ["lmfdb_knowl", "LMFDB"], ["nlab", "nLab"], ["mathworld", "MathWorld"],
  ["stacks", "Stacks Project"], ["kerodon", "Kerodon"], ["oeis", "OEIS"],
  ["dlmf", "DLMF"], ["proofwiki", "ProofWiki"], ["eom", "Encyclopedia of Math"],
  ["planetmath", "PlanetMath"], ["metamath", "Metamath"], ["msc", "MSC"],
  ["kgmid", "Google Knowledge Graph"],
];

async function fetchMe() {
  try {
    const r = await fetch("/api/auth/me", {headers: {Accept: "application/json"}});
    if (r.ok) currentUser = (await r.json()).user || null;
  } catch (e) { currentUser = null; }
  updateAuthNav();
}
// reflect login state in the header: "Log in" ↔ "<name> · Log out"
function updateAuthNav() {
  const el = $("#wl-auth");
  if (!el) return;
  el.innerHTML = currentUser
    ? `<span style="color:#9aa3b2">${esc(currentUser.name || "you")}</span> · ` +
      `<a href="/logout?returnTo=/brain">Log out</a>`
    : `<a href="/login?returnTo=/brain">Log in</a>`;
}
async function fetchCommunityEdges(id) {
  try {
    const r = await fetch("/api/brain/edges?id=" + encodeURIComponent(id));
    if (!r.ok) return {edges: [], shared: [], nodeLabels: {}, self: null};
    const j = await r.json();
    return {edges: j.edges || [], shared: j.shared || [], nodeLabels: j.node_labels || {}, self: j.self || null};
  } catch (e) { return {edges: [], shared: [], nodeLabels: {}, self: null}; }
}
// full-text autocomplete over ALL of Wikidata (not just the ingested nodes)
async function searchWikidata(q) {
  try {
    const r = await fetch("https://www.wikidata.org/w/api.php?action=wbsearchentities" +
      "&format=json&language=en&uselang=en&type=item&limit=8&origin=*&search=" + encodeURIComponent(q));
    return ((await r.json()).search || []).map(s =>
      ({id: s.id, label: s.label || s.id, desc: s.description || ""}));
  } catch (e) { return []; }
}
async function submitCommunityEdge(payload) {
  try {
    const r = await fetch("/api/brain/edge", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
    if (r.ok) return {ok: true};
    return {ok: false, error: ((await r.json().catch(() => ({}))).error) || ("HTTP " + r.status)};
  } catch (e) { return {ok: false, error: String(e)}; }
}
async function deleteCommunityEdge(edgeId) {
  try { await fetch("/api/brain/edge/" + encodeURIComponent(edgeId) + "/delete", {method: "POST"}); }
  catch (e) { /* ignore; the refresh will show the true state */ }
}
// HTML for a community-edge endpoint: an xref reads "<DB>: value"; a
// community-added Wikidata node (in nodeLabels) links OUT to Wikidata (it isn't
// in the static shards, so in-brain nav would 404); a static node navigates
// in-brain.
function communityTargetHtml(other, nodeLabels) {
  nodeLabels = nodeLabels || {};
  if (other.startsWith("xref:")) {
    const p = other.split(":");
    return esc((XREF_NAME[p[1]] || p[1]) + ": " + p.slice(2).join(":"));
  }
  if (/^Q\d+$/.test(other) && nodeLabels[other]) {
    return `<a href="https://www.wikidata.org/wiki/${esc(other)}" target="_blank" rel="noopener"
      title="community-added Wikidata concept">${esc(nodeLabels[other])} <span class="lit-ref">${esc(other)}</span></a>`;
  }
  const L = layout && layout.items.get(other);
  return `<a data-nav="${esc(other)}">${esc((L && L.data.label) || other)}</a>`;
}
// minimal panel for a community-added Wikidata concept (not in the static shards)
async function renderCommunityNodePanel(id) {
  const {self} = await fetchCommunityEdges(id);
  if (lastPanelId !== id) return;
  if (!self) { panelEl.innerHTML = `<p class="note">Unknown node: ${esc(id)}</p>`; return; }
  let html = `<h2>${esc(self.label)}</h2>
    <div class="sub">community concept ·
      <a href="https://www.wikidata.org/wiki/${esc(id)}" target="_blank" rel="noopener">${esc(id)}</a>
      · added by ${esc(self.added_by)}${
      currentUser ? ` · <a data-delnode="1" style="color:#a12621;cursor:pointer">delete</a>` : ""}</div>`;
  if (self.description) html += `<p style="font-size:.9rem">${esc(self.description)}</p>`;
  html += `<span class="badge">Wikidata concept</span><div id="community-slot"></div>`;
  panelEl.innerHTML = html;
  panelEl.querySelectorAll("[data-delnode]").forEach(a => a.addEventListener("click", async () => {
    if (!confirm("Delete this community concept? It stays as a gravestone recording who removed it.")) return;
    try { await fetch("/api/brain/node/" + encodeURIComponent(id) + "/delete", {method: "POST"}); } catch (e) {}
    navigate("path:Mathlib");
  }));
  renderCommunity(id);
}
async function renderCommunity(id) {
  const slot = $("#community-slot");
  if (!slot) return;
  const {edges, shared, nodeLabels} = await fetchCommunityEdges(id);
  if (lastPanelId !== id || !$("#community-slot")) return;   // panel moved on
  let html = `<section class="kind community"><h3>Community connections
    <span class="cnt">(${edges.length})</span></h3>`;
  for (const e of edges) {
    const out = e.src === id;
    const note = (e.evidence && e.evidence.note) || "";
    html += `<div class="cedge">
      <span class="dirarrow">${out ? "→" : "←"}</span>
      <span class="ctarget">${communityTargetHtml(out ? e.dst : e.src, nodeLabels)}</span>
      <span class="mk">${esc(e.kind)}</span>
      <span class="cprov ${e.actor_type === "ai" ? "ai" : "human"}">${
        e.actor_type === "ai" ? "AI" : "human"} · ${esc(e.added_by)}</span>${
      currentUser ? `<button class="cdel" data-del="${esc(e.id)}"
        title="delete this connection (kept as a gravestone recording who removed it)">×</button>` : ""}${
      note ? `<div class="cnote">${esc(note)}</div>` : ""}</div>`;
  }
  if (!edges.length) html += `<p class="note">No community connections yet.</p>`;
  // cross-pollination: nodes that share an external-database page with this one
  // (discovered, not drawn) — the same object catalogued in two places.
  if (shared && shared.length) {
    html += `<div class="cshared"><h4>Same object elsewhere
      <span class="cnt">(${shared.length} discovered)</span></h4>`;
    for (const s of shared.slice(0, 30)) {
      html += `<div class="cedge cinferred">
        <span class="dirarrow">↔</span>
        <span class="ctarget">${communityTargetHtml(s.node, nodeLabels)}</span>
        <span class="mk">same page in ${esc(XREF_NAME[s.db] || s.db)}</span>
        <span class="cprov ${s.source === "community" ? "human" : "machine"}">${
          s.source === "community" ? "community" : "database"}</span></div>`;
    }
    html += `<p class="note">Shared external-database pages ⇒ these are the same
      object. Add a cross-database link above to discover more.</p></div>`;
  }
  if (currentUser) {
    // ADD AN EDGE (a connection between this node and another)
    html += `<details class="caddform"><summary>＋ Add a connection (edge)</summary><div class="cform">
      <label>Type<select id="cf-kind">${
        COMMUNITY_KINDS_UI.map(([k, l]) => `<option value="${k}">${esc(l)}</option>`).join("")}</select></label>
      <div id="cf-target-node"><label>Connect to
        <input id="cf-target" type="text" autocomplete="off" placeholder="search a concept / decl / area (across all of Wikidata)…"></label>
        <div id="cf-hits" class="cf-hits"></div><input type="hidden" id="cf-target-id"></div>
      <div id="cf-target-xref" style="display:none">
        <label>Database<select id="cf-db">${
          XREF_DB_OPTIONS.map(([k, l]) => `<option value="${k}">${esc(l)}</option>`).join("")}</select></label>
        <label>Identifier<input id="cf-value" type="text" placeholder="e.g. group.abelian"></label></div>
      <label>Evidence note <span class="cf-opt">(optional)</span><input id="cf-note" type="text" placeholder="why is this connection valid?"></label>
      <button id="cf-submit">Add connection</button><span id="cf-msg" class="note"></span></div></details>`;
    // ADD A NODE (introduce a new Wikidata concept — no edge)
    html += `<details class="caddform"><summary>＋ Add a Wikidata concept (node)</summary><div class="cform">
      <p class="note" style="margin:0">Introduce a concept the brain doesn't have yet — search all of Wikidata.</p>
      <label>Wikidata concept
        <input id="cn-search" type="text" autocomplete="off" placeholder="search Wikidata by name…"></label>
      <div id="cn-hits" class="cf-hits"></div><input type="hidden" id="cn-id">
      <button id="cn-submit">Add concept</button><span id="cn-msg" class="note"></span></div></details>`;
  } else {
    html += `<p class="note"><a href="/login">Log in with GitHub</a> to add or remove connections.</p>`;
  }
  html += `</section>`;
  slot.innerHTML = html;
  wireCommunity(id);
}
function wireCommunity(id) {
  const slot = $("#community-slot");
  if (!slot) return;
  slot.querySelectorAll("[data-nav]").forEach(a =>
    a.addEventListener("click", () => navigate(a.dataset.nav)));
  slot.querySelectorAll("[data-del]").forEach(b => b.addEventListener("click", async () => {
    if (!confirm("Delete this connection? It stays as a gravestone that records who removed it.")) return;
    await deleteCommunityEdge(b.dataset.del);
    if (lastPanelId === id) renderCommunity(id);
  }));
  const kindSel = $("#cf-kind");
  if (!kindSel) return;
  const sync = () => {
    const isX = kindSel.value === "xref";
    $("#cf-target-node").style.display = isX ? "none" : "";
    $("#cf-target-xref").style.display = isX ? "" : "none";
  };
  kindSel.addEventListener("change", sync); sync();
  const tin = $("#cf-target"), hits = $("#cf-hits"), tid = $("#cf-target-id");
  let searchT;
  if (tin) tin.addEventListener("input", () => {
    clearTimeout(searchT);
    tid.value = "";
    const q = tin.value.trim();
    if (q.length < 2) { hits.innerHTML = ""; return; }
    searchT = setTimeout(async () => {
      // brain nodes (decls, containers, ingested concepts) AND all of Wikidata
      let brainHits = [];
      try { brainHits = (await (await fetch("/api/brain/search?limit=6&q=" + encodeURIComponent(q))).json()).hits || []; }
      catch (e) { /* no API */ }
      const wd = await searchWikidata(q);
      if (tin.value.trim() !== q) return;   // a newer keystroke won
      const seen = new Set(brainHits.map(h => (h.id || "").toUpperCase()));
      const merged = [
        ...brainHits.map(h => ({id: h.id, label: h.label, type: h.type})),
        ...wd.filter(w => !seen.has(w.id.toUpperCase())).map(w =>
          ({id: w.id, label: w.label,
            type: "Wikidata" + (w.desc ? " · " + (w.desc.length > 42 ? w.desc.slice(0, 40) + "…" : w.desc) : "")})),
      ];
      hits.innerHTML = merged.slice(0, 10).map(h =>
        `<div class="cf-hit" data-id="${esc(h.id)}" data-label="${esc(h.label)}">${
          esc(h.label)}<span class="t">${esc(h.type)}</span></div>`).join("");
      hits.querySelectorAll(".cf-hit").forEach(el => el.addEventListener("click", () => {
        tid.value = el.dataset.id; tin.value = el.dataset.label; hits.innerHTML = "";
      }));
    }, 220);
  });
  const submit = $("#cf-submit");
  if (submit) submit.addEventListener("click", async () => {
    const msg = $("#cf-msg"), kind = kindSel.value, note = $("#cf-note").value.trim();
    let dst;
    if (kind === "xref") {
      const value = $("#cf-value").value.trim();
      if (!value) { msg.textContent = "enter an identifier"; return; }
      dst = "xref:" + $("#cf-db").value + ":" + value;
    } else {
      dst = tid.value;
      if (!dst) { msg.textContent = "pick a target from the search results"; return; }
    }
    submit.disabled = true; msg.textContent = "saving…";
    const res = await submitCommunityEdge({src: id, dst, kind, evidence: {note}});
    submit.disabled = false;
    if (res.ok) { if (lastPanelId === id) renderCommunity(id); }
    else msg.textContent = res.error || "could not add";
  });

  // ---- "Add a Wikidata concept" (a NEW node, no edge) ----------------------
  const cnIn = $("#cn-search"), cnHits = $("#cn-hits"), cnId = $("#cn-id"), cnSubmit = $("#cn-submit");
  let cnT;
  if (cnIn) cnIn.addEventListener("input", () => {
    clearTimeout(cnT);
    cnId.value = "";
    const q = cnIn.value.trim();
    if (q.length < 2) { cnHits.innerHTML = ""; return; }
    cnT = setTimeout(async () => {
      const wd = await searchWikidata(q);
      if (cnIn.value.trim() !== q) return;
      cnHits.innerHTML = wd.map(w =>
        `<div class="cf-hit" data-id="${esc(w.id)}" data-label="${esc(w.label)}">${esc(w.label)}<span class="t">${
          esc(w.id + (w.desc ? " · " + (w.desc.length > 42 ? w.desc.slice(0, 40) + "…" : w.desc) : ""))}</span></div>`).join("");
      cnHits.querySelectorAll(".cf-hit").forEach(el => el.addEventListener("click", () => {
        cnId.value = el.dataset.id; cnIn.value = el.dataset.label; cnHits.innerHTML = "";
      }));
    }, 220);
  });
  if (cnSubmit) cnSubmit.addEventListener("click", async () => {
    const msg = $("#cn-msg"), qid = cnId.value;
    if (!qid) { msg.textContent = "pick a concept from the search results"; return; }
    cnSubmit.disabled = true; msg.textContent = "adding…";
    let ok = false, err = "could not add";
    try {
      const r = await fetch("/api/brain/node", {method: "POST",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify({qid})});
      ok = r.ok;
      if (!ok) err = ((await r.json().catch(() => ({}))).error) || ("HTTP " + r.status);
    } catch (e) { err = String(e); }
    cnSubmit.disabled = false;
    if (ok) { msg.innerHTML = `added ✓ — now searchable & linkable`; cnId.value = ""; cnIn.value = ""; }
    else msg.textContent = err;
  });
}

(async function boot() {
  // ?embed=1 → chrome-less mode for the landing-page iframe: hide the header +
  // crumb bar, article/external links escape the frame
  if (new URLSearchParams(location.search).has("embed")) {
    document.body.classList.add("embed");
    const base = document.createElement("base");
    base.target = "_parent";
    document.head.appendChild(base);
  }
  fetchMe();   // login state for the community-edit affordances (non-blocking)
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
  const h = parseHash();
  filterMask = h.f;
  syncChips();
  setFlatten(!!h.flat);
  if (h.view === "explorer") {
    setExplorer(true);
    focusId = h.id || "path:Mathlib";
    await renderExplorer(false);
    if (h.id && explorerOn) { selectedId = h.id; renderPanel(h.id); drawSelRing(); }
  } else if (h.id) { await navigate(h.id); }
  else { focusId = "path:Mathlib"; await renderFocus(false); renderPanel(focusId); }
  lastStageW = stageEl.clientWidth;   // baseline for the width-change resize guard
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
