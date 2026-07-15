#!/usr/bin/env python3
"""Generate /brain — the cell map over the BRAIN v3 dataset.

One zoomable canvas, zero baked-in data: everything is fetched on demand from
the prefix shards in /assets/brain/cells/ (manifest.json → one shard fetch per
cell), so the client never loads the whole graph — brain/SCHEMA.md's locality
law as UX.

v3 (brain/SCHEMA.md#v3, docs/BRAIN-V3.md): the node is the **cell** — an atom of
**organs** (a Wikidata concept, a Lean decl, an external-DB page, a WikiLean
article, an arXiv statement). External pages are NOT nodes any more; they are
organs inside cells. Cells nest in **supercells** (module folders). All weak
bonds between two cells collapse to ONE **synapse** carrying every trace.

  · Bubbles  — one circle-pack level per supercell (library → module → … → file),
               with the cells it holds as the leaves. supercells.json IS the
               tree; a cell spanning several modules renders inside each.
  · Explorer — the complete flat cell graph (explorer.json: 8.9k cells, 76k
               synapses), drawn at its BUILD-TIME `xy`. The client runs no
               physics at all — SCHEMA "Layout is BUILD-TIME" — which is what
               killed the freeze and the ring-around-a-clump artefact.
  · Card     — the selected cell's organs grouped by kind, each with its bond,
               its provenance (a merged @[wikidata] tag never reads like an
               AI-queued candidate — C7) and its embedded payload: Lean code,
               the Wikidata description, licensed DB snippets, arXiv refs. ONE
               fetch renders the whole card.
  · Drawer   — a synapse's weight, its kind histogram and EVERY trace, in prose
               that names the actual database and page.
  · Search   — label + `aka` (every organ label) over labels.json, so searching
               "Vector space" surfaces the Module atom.

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
<meta name="description" content="Explore the BRAIN: a zoomable map of mathematics as cells — atoms that fuse a Wikidata concept, its Lean formalization, its external-database entries (LMFDB, nLab, MathWorld, …), its WikiLean article and its arXiv statements into one object, joined by synapses with machine-checkable provenance on every trace.">
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
/* a group whose data the current view doesn't carry: visibly inert, never a
   silent no-op (the flat map ships weights only — no per-kind/per-trace data) */
.toolbar .grp.inert { opacity:.4; }
.toolbar .grp.inert label { cursor:not-allowed; }
#structstat { color:#7f8a9c; font-size:.78rem; font-style:italic; white-space:nowrap; }
#search { position:relative; }
#search input { width:290px; padding:5px 9px; border:1px solid #33405c; border-radius:6px;
  font-size:.88rem; background:#0b0e14; color:#e6e4de; }
#search input:focus { outline:2px solid #38bdf855; }
#hits { position:absolute; top:32px; left:0; z-index:30; width:460px; max-height:380px;
  overflow:auto; background:#151b28; border:1px solid #33405c; border-radius:8px;
  box-shadow:0 8px 24px rgba(0,0,0,.5); display:none; }
#hits .hit { padding:6px 10px; cursor:pointer; display:flex; gap:8px; align-items:baseline; }
#hits .hit:hover { background:#1e2635; }
#hits .hit .t { font-size:.72rem; color:#9aa3b2; min-width:64px; }
#hits .hit .aka { font-size:.72rem; color:#7f8a9c; font-style:italic; }
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
path.synbatch { pointer-events:none; fill:none; }   /* the flat map's batched synapses */

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
.prov { font-size:.7rem; border-radius:8px; padding:0 6px; border:1px solid #c8bfa8;
  color:#5a544a; margin-left:auto; white-space:nowrap; font-family:-apple-system,sans-serif; }
.prov.human { border-color:#1a7f37; color:#116329; }
.prov.machine { border-color:#6d28d9; color:#5b21b6; }
.prov.ai { border-color:#c2540a; color:#9a3f00; }
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
/* synapse-evidence trace: the step-by-step chain that connects two cells */
.ev-trace { margin:6px 0 2px; border-left:2px solid #d8cfb6; padding-left:9px; }
.ev-step { display:flex; align-items:baseline; gap:6px; padding:1px 0; }
.ev-step .role { color:#8a8272; font-size:.72rem; min-width:14px; }
.ev-step .who { color:#2b2822; }
.ev-step .who a, .ev-step .who .nav { color:#1a4b8c; cursor:pointer; }
.ev-step .who .nav:hover { text-decoration:underline; }
.ev-step .tag { color:#8a8272; font-size:.72rem; }
.ev-step .extlink { color:#1a4b8c; text-decoration:none; margin-left:2px; }
.ev-conn { color:#8a8272; font-size:.72rem; margin:1px 0 1px 2px; font-style:italic; }
.ev-snip { margin:6px 0 2px; background:#efe8d6; border-radius:5px;
  padding:6px 9px; color:#3a362e; font-size:.79rem; line-height:1.45; }
.ev-snip .cite { display:block; margin-top:4px; color:#8a8272; font-size:.71rem; }
.ev-snip .cite a { color:#1a4b8c; text-decoration:none; }
.ev-snip.loading { color:#8a8272; font-style:italic; background:none; padding:2px 0; }
[data-theme="dark"] .ev-trace { border-left-color:#4d4742; }
[data-theme="dark"] .ev-step .who { color:#ebe5d8; }
[data-theme="dark"] .ev-step .who a, [data-theme="dark"] .ev-step .who .nav,
[data-theme="dark"] .ev-snip .cite a { color:#6e9adf; }
[data-theme="dark"] .ev-snip { background:#2c2926; color:#d8d2c4; }
[data-theme="dark"] .ev-step .role, [data-theme="dark"] .ev-step .tag,
[data-theme="dark"] .ev-conn, [data-theme="dark"] .ev-snip .cite { color:#9a9081; }
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
.fgrouplabel { color:#6b7488; font-size:.7rem; margin:0 2px 0 8px; white-space:nowrap;
  border-left:1px solid #2a3244; padding-left:9px; cursor:help; }
.fgrouplabel:first-of-type { border-left:none; padding-left:0; }
#filterstat { color:#7f8a9c; font-size:.78rem; font-style:italic; }
/* the cell card: one identity strip for the atom */
.unitcard { border:1px solid #d8cfb8; border-radius:8px; background:#fdfbf4;
  padding:12px 14px 8px; margin-bottom:12px; }
.unitcard h2 { margin:0 0 4px; }
.uc-desc { color:#3d382e; font-size:.9rem; margin:0 0 8px; }
.uc-src { color:#8a8272; font-size:.7rem; margin-left:6px; font-style:italic; }
.uc-anchor { font-size:.66rem; color:#116329; border:1px solid #1a7f37; border-radius:8px;
  padding:0 5px; font-family:-apple-system,sans-serif; }
/* one organ row per particle (Sources-accordion styling; TeX stays raw — no
   math renderer ships) */
.srcacc summary { cursor:pointer; color:#1a4b8f; font-size:.85rem; user-select:none; }
.srcrow { border:1px solid #ddd4bd; border-radius:6px; background:#fbf8ef; margin:8px 0;
  padding:8px 10px; }
.srchead { font-size:.84rem; margin-bottom:4px; display:flex; gap:6px; align-items:baseline;
  flex-wrap:wrap; }
.srchead .oname { font-weight:700; color:#0d0c0a; }
.snip { font-size:.86rem; line-height:1.5; color:#2b2822; }
.srclic { margin-top:6px; border-top:1px solid #e3dac4; padding-top:4px; color:#8a8272;
  font-size:.7rem; font-family:-apple-system,sans-serif; }
.snipblock { margin:8px 0; border:1px solid #ddd4bd; border-radius:6px; background:#fbf8ef;
  padding:8px 10px; }
.snipblock .src { display:block; color:#8a8272; font-size:.7rem; margin-top:6px; }
/* the bond that pulled this organ into the atom (SCHEMA v3 "Strong bonds") */
.bond { font-size:.7rem; border:1px solid #c8bfa8; border-radius:8px; padding:0 6px;
  color:#5a544a; font-family:-apple-system,sans-serif; }
.bond.exact { border-color:#1a7f37; color:#116329; }
.osub { color:#5a544a; font-size:.76rem; margin:4px 0 0;
  font-family:-apple-system,sans-serif; }
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
      <input id="q" type="search" placeholder="Search cells &amp; areas… (e.g. vector space)" autocomplete="off">
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
    <button id="explorerbtn" class="fchip" title="flatten the current area's subtree into the complete cell graph — every cell at its build-time position and every synapse among them. Facet chips narrow it; at the top level it covers all 8.9k cells.">Explorer</button>
  </span>
  <span class="grp" id="grp-layers"><b>Layers</b>
    <label><input type="checkbox" data-k="depends" checked> formal deps</label>
    <label title="concept→declaration claims that did NOT fuse the two into one atom: invocation/related never merge (SCHEMA rule 3), and a generalization/special_case claim past the concept's single best target stays a synapse."><input type="checkbox" data-k="generalization,special_case,invocation,related" checked> loose formalization claims</label>
    <label><input type="checkbox" data-k="links,co-page" checked> cross-refs</label>
    <label><input type="checkbox" data-k="cites,co-statement" checked> literature</label>
    <label><input type="checkbox" data-k="relates" checked> wikidata relations</label>
    <label><input type="checkbox" data-k="mentions" checked> article mentions</label>
  </span>
  <span class="grp"><b>Structure</b>
    <label title="Tint each cell by its dependency-flow community — clusters of atoms that lean on each other, regardless of which folder the tree files them under (arXiv 2604.24797's Finding 1). Needs the level's synapse web, so it works where the cells are few enough to fetch."><input type="checkbox" id="commColor" checked> logical communities</label>
    <span class="note" id="structstat" title="what this level's synapse web is doing"></span>
  </span>
  <span class="grp" id="grp-prov"><b>Provenance</b>
    <label title="community/human-curated: Wikidata properties &amp; claims, @[wikidata]/@[stacks]/@[kerodon] attributes written in Mathlib source"><input type="checkbox" data-p="human" checked> human</label>
    <label title="machine-verified: kernel-extracted dependencies and mechanically-scraped page links — no judgment involved"><input type="checkbox" data-p="machine" checked> machine</label>
    <label title="AI-generated: agent-proposed concept matches (skeptic-reviewed), LLM-judged paper matches (TheoremGraph), pipeline annotations"><input type="checkbox" data-p="ai" checked> AI</label>
  </span>
  <span class="grp"><b>Show only</b>
    <span class="fgrouplabel" title="Cross-reference ATTRIBUTES hand-written into the mathlib4 source. Each links a Lean declaration to an external catalog, and rides up to the cell that declaration is an organ of. These three are literally the @[…] attributes in Mathlib.">Mathlib tags:</span>
    <button class="fchip" data-fbit="1" title="cells holding a declaration that carries an @[wikidata] attribute in mathlib4 — the gold, human-written link from a Lean declaration to its Wikidata concept">@[wikidata]</button>
    <button class="fchip" data-fbit="2" title="cells holding a declaration that carries an @[stacks] attribute in mathlib4 — a human-written link to a Stacks Project tag">@[stacks]</button>
    <button class="fchip" data-fbit="4" title="cells holding a declaration that carries an @[kerodon] attribute in mathlib4 — a human-written link to a Kerodon tag">@[kerodon]</button>
    <span class="fgrouplabel" title="External-database identities that WIKIDATA records for a math concept, independent of Mathlib. Each becomes a `page` organ inside the cell.">Wikidata cross-refs:</span>
    <button class="fchip" data-fbit="1024" title="cells with an nLab page organ (Wikidata property P4215)">nLab</button>
    <button class="fchip" data-fbit="2048" title="cells with a MathWorld page organ (Wikidata property P2812)">MathWorld</button>
    <button class="fchip" data-fbit="512" title="cells with an LMFDB knowl organ (Wikidata property P12987)">LMFDB</button>
    <button class="fchip" data-fbit="4096" title="cells with a ProofWiki page organ (Wikidata property P6781)">ProofWiki</button>
    <button class="fchip" data-fbit="16384" title="cells with an OEIS sequence organ (Wikidata property P829)">OEIS</button>
    <button class="fchip" data-fbit="8" title="cells with ANY external-database page organ — the union of every database above PLUS the @[stacks]/@[kerodon] Mathlib tags">any</button>
    <span class="fgrouplabel">Status:</span>
    <button class="fchip" data-fbit="16" title="cells whose concept is formalized — a Mathlib declaration formalizes it">formalized</button>
    <button class="fchip" data-fbit="64" title="cells holding an annotated WikiLean article organ">article</button>
    <button class="fchip" data-fbit="128" title="cells holding an arXiv statement organ (a TheoremGraph match)">literature</button>
    <span class="note" id="filterstat"></span>
  </span>
  <span class="grp"><a id="srcbtn2" style="cursor:pointer"
    title="every external database the brain links to — layer, provenance, license">Sources</a></span>
  <span class="note" id="status">loading manifest…</span>
</div>
<div id="crumbbar"></div>
<div class="main">
  <div id="stage"><svg id="svg"></svg>
    <div class="hint">scroll to zoom · drag to pan · click an area to dive in ·
      background to go up · click any synapse for its evidence ·
      dots = <b>cells</b> (atoms of organs) ·
      <span style="color:#3b82f6">blue</span> = has a Lean formalization ·
      <span style="color:#8c959f">grey</span> = no formal home yet ·
      gold ring = a hand-written <span style="color:#eab308">@[wikidata]</span> tag ·
      lines = <b>synapses</b> (thicker = more bonds):
      <span style="color:#a78bfa">formal deps</span> ·
      <span style="color:#38bdf8">loose formalization claims</span> ·
      <span style="color:#fbbf24">wikidata relations</span> ·
      <span style="color:#f472b6">shared DB page</span> ·
      <span style="color:#84cc16">page links</span> ·
      <span style="color:#fb923c">literature</span> ·
      <span style="color:#2dd4bf">shared statement</span> ·
      tinted cells = logical communities</div>
  </div>
  <div id="panel"><p class="note">The Brain as cells: every atom fuses a Wikidata
    concept, the Lean declaration that formalizes it, its entries in nLab / LMFDB /
    Stacks / MathWorld / …, its WikiLean article and its arXiv statements into ONE
    object. Atoms nest inside the Mathlib folders that hold their code, and the
    lines between them are synapses — every weak bond between two atoms, collapsed
    into one edge that keeps every trace. Click anything.</p></div>
</div>
<script src="https://cdn.jsdelivr.net/npm/d3@7"></script>
<script>
"use strict";
// ============================ data layer ====================================
// v3 lives in its OWN namespace: cells/ ships the atoms, the containment tree,
// the flat map, the organ→atom alias table and the search index.
const BASE = "/assets/brain/cells/";
const SOURCES_URL = "/assets/brain/sources.json";   // the legend is v-agnostic
const ROOTS_ID = "__libs__";          // pseudo-focus: the library roots
const UNPLACED_ID = "__unplaced__";   // pseudo-focus: cells with no decl organ,
                                      // so no supercell to nest in (1.5k of 8.9k)
const STRAYS_ID = "__strays__";       // the collapsed "cells filed at this level"
                                      // bubble (see focusItems)
let manifest = null, labels = null, labelById = null, tree = null, aliases = null;
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
// silently 404s — the "Unknown cell" ghost bug. The manifest revalidates
// (no-cache) and a missing shard triggers one manifest re-sync + retry.
let dataV = "";
const vq = () => (dataV ? "?v=" + dataV : "");
async function fetchManifest() {
  const r = await fetch(BASE + "manifest.json", {cache: "no-cache"});
  if (!r.ok) throw new Error("HTTP " + r.status);
  manifest = await r.json();
  dataV = encodeURIComponent(manifest._meta.generated_at || "");
}
// ONE fetch renders a whole card: the shard entry embeds every organ payload
// (Lean code, the Wikidata description, licensed DB snippets) + the synapses.
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

const isCellId = id => typeof id === "string" && id.startsWith("cell:");
const isPathId = id => typeof id === "string" && id.startsWith("path:");

async function ensureLabels() {
  if (!labels) {
    const r = await fetch(BASE + "labels.json" + vq());
    labels = r.ok ? await r.json() : [];
    labelById = new Map(labels.map(r2 => [r2.id, r2]));
  }
  return labels;
}
// The containment tree. supercells.json IS the bubble view's data source — its
// leaves are cells, so no shard fetch is needed to lay out a level; labels.json
// supplies each cell's label + facet bits. Subtree cell counts and the
// no-supercell bucket are derived once, here.
async function ensureTree() {
  if (tree) return tree;
  const [j] = await Promise.all([
    fetch(BASE + "supercells.json" + vq()).then(r => (r.ok ? r.json() : null)).catch(() => null),
    ensureLabels(),
  ]);
  if (!j) { tree = {roots: [], sc: {}, unplaced: [], unplacedFa: 0, count: () => 0}; return tree; }
  const sc = j.supercells || {};
  const memo = new Map();
  const count = p => {
    if (memo.has(p)) return memo.get(p);
    const v = sc[p];
    if (!v) return 0;
    memo.set(p, 0);   // cycle guard: the tree is acyclic, but never hang on bad data
    let n = (v.cells || []).length;
    for (const ch of v.children || []) n += count(ch);
    memo.set(p, n);
    return n;
  };
  const placed = new Set();
  for (const p of Object.keys(sc)) for (const c of sc[p].cells || []) placed.add(c);
  // a cell with no decl organ has no module to nest in — it would otherwise be
  // browsable only through search and the Explorer
  const unplaced = (labels || []).map(r => r.id).filter(id => !placed.has(id));
  let unplacedFa = 0;
  for (const id of unplaced) unplacedFa |= (labelById.get(id) || {}).f || 0;
  tree = {roots: j.roots || [], sc, unplaced, unplacedFa, count};
  return tree;
}
async function ensureAliases() {
  if (!aliases) {
    const r = await fetch(BASE + "aliases.json" + vq());
    aliases = r.ok ? await r.json() : {organs: {}, decls: {}, slugs: {}};
  }
  return aliases;
}
// The v2→v3 compat layer: /brain#Q181296, #Vector_space and #decl:Mathlib:Module
// must all land on the atom that OWNS that organ (SCHEMA C4 — aliases is a
// function). A rule-5 organ (a field-of-study concept) resolves to its folder.
async function resolveId(id) {
  if (!id) return null;
  if (id === ROOTS_ID || id === UNPLACED_ID || isCellId(id)) return id;
  await ensureTree();
  if (isPathId(id)) return tree.sc[id] ? id : null;
  const a = await ensureAliases();
  return a.organs[id] || a.decls[id] || a.slugs[id] || null;
}

function activeKinds() {
  const ks = new Set();
  document.querySelectorAll(".toolbar input[data-k]").forEach(cb => {
    if (cb.checked) cb.dataset.k.split(",").forEach(k => ks.add(k));
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
// attributes), did the Lean kernel certify it (dependencies, page scrapes), or
// did an AI propose it (agent grounding, LLM-judged paper matches)?
function provClass(kind, prov, ev) {
  // `links` = a hyperlink mechanically extracted from the source database's
  // own pages (and its CC0-anchored concept projection) — no judgment involved
  if (kind === "depends" || kind === "contains" || kind === "links") return "machine";
  if (((prov && prov.method) || "").includes("@[")) return "human";
  if (ev && ev.source_tagged) return "human";   // gold pair reached via another path
  // co-page = two cells cross-referencing one page; the cross-refs themselves
  // are Wikidata properties / Mathlib attributes, i.e. human-written
  if (kind === "xref" || kind === "co-page" || kind === "relates") return "human";
  return "ai";
}
const PROV_TITLE = {
  human: "human-curated (Wikidata property/claim or a source attribute in Mathlib)",
  machine: "machine-verified (Lean kernel / mechanically-extracted page links)",
  ai: "AI-generated (agent-proposed or LLM-judged), verified by oracle + skeptic",
};
// ============================ canvas state ==================================
let focusId = null;        // supercell path / ROOTS_ID / UNPLACED_ID / a cell id
let selectedId = null;     // node the panel shows / ring highlights
let layout = null;         // {items: Map(id -> {x,y,r,data}), leaves, ego?, explorer?}
let explorerOn = false;    // the Explorer: the flat cell graph at its build-time xy
let filterMask = 0;        // facet-filter bitmask over `f` (0 = no filter)
let currentUser = null;    // {id, name, role} once /api/auth/me resolves (community edits)
let renderSeq = 0;         // guards against out-of-order async renders
const svg = d3.select("#svg");
// One <g> holds the whole scene so free pan/zoom is a single transform on it,
// layered UNDER the semantic click-to-descend. Everything drawn (edges,
// bubbles, overlays, labels) lives inside it and therefore pans/zooms together.
const gViewport = svg.append("g").attr("class", "viewport");
const gEdges = gViewport.append("g");
const gBubbles = gViewport.append("g");
const gOverlay = gViewport.append("g");
const gLabels = gViewport.append("g");

// Free pan/zoom over the canvas: scroll wheel zooms, drag pans (the /map feel).
// A pan must not read as a background click (which zooms out to the parent), so
// we swallow the click that follows a real drag.
let panMoved = false;
const isPhone = () => window.matchMedia && window.matchMedia("(max-width: 900px)").matches;
const zoomBehav = d3.zoom().scaleExtent([0.02, 16])
  // On a phone the page scrolls (the stack layout); d3-zoom's touch handlers
  // call preventDefault, which would trap a vertical swipe started over the
  // >50vh stage. Reject every gesture below 900px so native scroll wins there.
  .filter(ev => !isPhone() && (!ev.ctrlKey || ev.type === "wheel") && !ev.button)
  .on("start", ev => { panMoved = false;
    if (ev.sourceEvent && ev.sourceEvent.type === "mousedown") stageEl.classList.add("grabbing"); })
  .on("zoom", ev => { if (ev.sourceEvent && ev.sourceEvent.type === "mousemove") panMoved = true;
    lastK = ev.transform.k;
    gViewport.attr("transform", ev.transform); })
  .on("end", () => stageEl.classList.remove("grabbing"));
svg.call(zoomBehav).on("dblclick.zoom", null);
// every fresh level fits the viewport — discard any lingering pan/zoom
function resetZoom() { svg.call(zoomBehav.transform, d3.zoomIdentity); }

const DB_COLOR = {lmfdb_knowl: "#facc15", nlab: "#4ade80", mathworld: "#f87171",
  proofwiki: "#60a5fa", stacks: "#f97316", kerodon: "#22d3ee", oeis: "#a3e635",
  dlmf: "#c084fc", eom: "#fb7185", planetmath: "#34d399", metamath: "#94a3b8",
  msc: "#eab308"};
const extDbOf = id => id.split(":")[1] || "";
const extValueOf = id => id.split(":").slice(2).join(":");

const SHADE = "#22304d";              // supercell fill — the canvas is always dark
const CELL_FORMAL = "#3b82f6";        // the atom has a decl organ (a formal home)
const CELL_INFORMAL = "#8c959f";      // concept-only atom — nothing formalizes it yet
const GOLD = "#eab308";               // a hand-written @[wikidata] tag rides in this atom
function fillFor(item) {
  if (item.type === "folder") return SHADE;
  if (item.type === "strays") return "#8c959f";
  return item.p ? CELL_FORMAL : CELL_INFORMAL;
}

// ---- level items: a supercell's sub-folders + the CELLS it holds ------------
function folderItem(p) {
  const sc = tree.sc[p] || {};
  return {id: p, type: "folder", label: sc.label || p, n: tree.count(p),
          f: 0, fa: sc.fa || 0};
}
function cellItem(cid) {
  // a synapse endpoint may legitimately be a SUPERCELL: a field concept's bonds
  // hang off the module that holds it (SCHEMA rule 5), so it reads as its folder
  if (isPathId(cid)) {
    const sc = (tree.sc || {})[cid] || {};
    return {id: cid, type: "folder", label: sc.label || cid.slice(5),
            n: tree.count ? tree.count(cid) : 0, f: 0, fa: sc.fa || 0};
  }
  const r = (labelById && labelById.get(cid)) || null;
  return {id: cid, type: "cell", label: (r && r.label) || cid,
          f: (r && r.f) || 0, p: (r && r.p) || null, aka: (r && r.aka) || null};
}
async function focusItems(id) {
  await ensureTree();
  if (id === ROOTS_ID) {
    // a root with no cells has nothing to dive into — v3 ships no library_kind,
    // so emptiness (not a taxonomy toggle) is what prunes the 39 roots to 6
    const items = tree.roots.filter(p => tree.count(p) > 0).map(folderItem);
    if (tree.unplaced.length)
      items.push({id: UNPLACED_ID, type: "folder", label: "no formal home",
                  n: tree.unplaced.length, f: 0, fa: tree.unplacedFa});
    return items;
  }
  if (id === UNPLACED_ID) return tree.unplaced.map(cellItem);
  const sc = tree.sc[id];
  if (!sc) return [];
  const folders = (sc.children || []).map(folderItem);
  // a cell that spans several modules is listed by EACH of them, so it renders
  // inside each — exactly what SCHEMA's `supercells` array asks for
  let cells = (sc.cells || []).map(cellItem);
  // At a level that HAS sub-areas, the cells filed directly here (Mathlib holds
  // 567 of them) would flood the pack and shrink Algebra to a dot — collapse
  // them into one bubble; the card still lists every one. An active facet filter
  // must still see its matches, so only the remainder collapses.
  if (folders.length && cells.length > 12) {
    const keep = filterMask ? cells.filter(c => ((c.f || 0) & filterMask) !== 0) : [];
    const rest = cells.length - keep.length;
    cells = keep.concat(rest > 0
      ? [{id: STRAYS_ID, type: "strays", label: rest + " cells filed here", n: rest}] : []);
  }
  return folders.concat(cells);
}
// pack values: folder area ~ cell-count^0.6 (compresses Mathlib=7.3k vs a
// 2-cell module into a ~10:1 radius ratio); cells are small fixed dots
function packValue(item) {
  if (item.type === "folder") return Math.pow(Math.max(item.n || 1, 1), 0.6);
  return item.type === "strays" ? 30 : 6;
}

// ---- screen-space sizing for the flat map -----------------------------------
// Everything inside gViewport is multiplied by the zoom k, so ANY size given in
// layout units renders at size*k. The build-time layout spans ±3,000 units, so the
// explorer fits at k≈0.13 — and at that zoom its r=2.2 dots drew at 0.29px, its
// 1.6 gold rings at 0.21px and its labels at 1.1px. The map was sub-pixel dust at
// its own resting zoom, which is a large part of why it read as unreadable however
// the layout was tuned. Dividing by k pins a size to the SCREEN instead.
//
// Applies to the explorer only: the bubble view's radii are meaningful geometry
// (a folder's area is its cell count) and must keep scaling with the zoom.
const LABEL_PX = 11;    // rendered label height at any zoom
const DOT_PX = 3.0;     // rendered dot radius floor at any zoom
const RING_PX = 1.6;    // rendered @[wikidata] gold ring width at any zoom
let lastK = 1;          // live zoom, tracked by the zoom handler

function applyExplorerScale(k) {
  if (!layout || !layout.explorer) return;
  lastK = k || 1;
  const s = 1 / lastK;
  gBubbles.selectAll("circle.node")
    .attr("r", l => Math.max(l.r, DOT_PX * s))
    .style("stroke-width", l => ((l.data.f || 0) & 1) ? (RING_PX * s) + "px" : null);
  gEdges.selectAll("path.synbatch").attr("stroke-width", function () {
    return Number(this.dataset.w) * s;
  });
  updateExplorerLabels(lastK);
}

function drawNodes() {
  const leaves = layout.leaves;
  // 8.9k <title> children is real DOM weight on the flat map and the labels
  // already name the big ones — hover text is a level-view affordance
  const withTitles = !(layout.explorer && leaves.length > 2000);
  const bubbles = gBubbles.selectAll("circle.node").data(leaves, l => l.data.id);
  bubbles.exit().remove();
  const entered = bubbles.enter().append("circle")
    .attr("class", l => l.data.type === "folder" ? "bubble node" : "dot node");
  if (withTitles) entered.append("title");
  const all = entered.merge(bubbles);
  all
    .attr("cx", l => l.x).attr("cy", l => l.y)
    .attr("r", l => Math.max(l.r, 2))
    .attr("fill", l => fillFor(l.data))
    .attr("fill-opacity", l => l.data.dim ? 0.15 : l.data.type === "folder" ? 0.55 : 0.9)
    // the gold ring marks an atom carrying a hand-written @[wikidata] tag —
    // inline style, because the .dot/.bubble CSS stroke overrides an attribute
    .style("stroke", l => l.data.type === "cell" && !l.data.dim && ((l.data.f || 0) & 1)
      ? GOLD : null)
    .style("stroke-width", l => l.data.type === "cell" && ((l.data.f || 0) & 1) ? "1.6px" : null)
    .on("click", (ev, l) => { ev.stopPropagation(); nodeClick(l.data); });
  if (withTitles) all.select("title").text(l => l.data.label
    + (l.data.type === "folder" ? ` — ${(l.data.n || 0).toLocaleString()} cells`
      : l.data.type === "strays" ? " — filed at this level; the card lists them all"
      : ((l.data.f || 0) & 1 ? " — carries a hand-written @[wikidata] tag" : "")));
}

// Cell labels in the level views get the SAME treatment the explorer's do:
// ranked once, then budgeted by zoom (updateLevelLabels). Without it a level that
// has no sub-folders draws every label at once — the `__unplaced__` bucket packs
// 1,516 equal-value cells into the stage, so every one clears the r>=5 gate and
// ~94% of the labels overlap another (measured: 1,516 shown, 100% overlapping at
// a 794x676 stage). The STRAYS collapse that protects every other level needs
// `folders.length`, and that bucket has none, so it never fires there — and it is
// the only tree-browsable surface for the 17% of atoms with no formal home, the
// one rootsPanel advertises as "Browse them".
function drawLabels() {
  gLabels.selectAll("*").remove();
  // ego lays labels flat under the node (edge-first); level views set them
  // inside/over the bubble (containment-first)
  const flat = layout && layout.ego;
  const cells = [];
  for (const l of layout.leaves) {
    if (l.data.type === "folder") {
      if (!flat && l.r < 24) continue;
      const fs = flat ? 10 : Math.max(10, Math.min(16, l.r / 4.5));
      gLabels.append("text").attr("class", "blabel")
        .attr("x", l.x).attr("y", flat ? l.y + l.r + 11 : l.y - (l.r > 40 ? 4 : -4))
        .attr("font-size", fs).attr("opacity", l.data.dim ? 0.35 : null)
        .text(l.data.label);
      if (!flat && l.r > 40)
        gLabels.append("text").attr("class", "bcount")
          .attr("x", l.x).attr("y", l.y + fs - 2).attr("font-size", fs * 0.72)
          .attr("opacity", l.data.dim ? 0.35 : null)
          .text(`${(l.data.n || 0).toLocaleString()} cells`);
      continue;
    }
    if (!flat && l.r < 5) continue;
    cells.push(l);
  }
  // Rank: biggest first, then gold @[wikidata] atoms, then label. The size tiebreak
  // matters — the unplaced pack gives every cell an identical radius, so radius
  // alone would rank arbitrarily; this puts the hand-tagged atoms in the budget
  // first and is deterministic (the map can be learned), like the rest of v3.
  cells.sort((a, b) => (b.r - a.r) ||
    (((b.data.f || 0) & 1) - ((a.data.f || 0) & 1)) ||
    String(a.data.label || a.data.id).localeCompare(String(b.data.label || b.data.id)));
  cells.forEach((l, i) => {
    const raw = l.data.label || l.data.id;
    gLabels.append("text").attr("class", "blabel clab")
      .attr("x", l.x).attr("y", l.y + Math.max(l.r, 3) + 10)
      .attr("data-rank", i)
      .text(raw.length > 26 ? raw.slice(0, 24) + "…" : raw);
  });
  // the pack's median cell radius, cached HERE and not recomputed per zoom tick:
  // updateLevelLabels runs on every frame of a zoom gesture, and re-sorting 1,516
  // leaves at 60fps to learn a number that only changes when the pack does is
  // exactly the kind of per-frame work this view has no reason to pay for
  layout.cellR = cells.length ? cells.map(l => l.r).sort((a, b) => a - b)[cells.length >> 1] : 0;
  updateLevelLabels(d3.zoomTransform(svg.node()).k);
}
// The level views' twin of updateExplorerLabels — same budget shape, same
// screen-space font size, for the same reason.
//
// FOLDER labels stay in layout units: a folder's radius IS its cell count and its
// label is sized to fit inside it, so that text is geometry and must keep scaling
// with the zoom. A CELL label is annotation, not geometry. Pinning it to the screen
// is what makes zooming DE-CLUTTER: a level view is otherwise scale-invariant (dots,
// gaps and labels all multiply by k together), so magnifying it never separates two
// overlapping labels — you just get bigger overlapping labels. Pin the text and the
// gaps grow while it doesn't, which is exactly the room the budget then spends.
const CELL_LABEL_PX = 9;      // == the previous literal, so k=1 renders unchanged
// Budget at k=1, tuned by MEASUREMENT on the pathological level (`__unplaced__`,
// 1,516 cells packed to r=7.5 in a 794x676 stage), counting pairwise
// getBoundingClientRect intersections of the RENDERED labels:
//   all 1,516 (before) 100% overlap · 250 -> 73% · 90 -> 53% · 40 -> 28% · 24 -> 8%
// so 24 is the knee. It scales with k^2 exactly as the explorer's 600 does.
const LEVEL_LABEL_BUDGET = 24;
// ...but ONLY where the pack is too dense to label honestly, which is why this is
// gated rather than global. A pack that fills the stage with n cells is a regular
// lattice: its labels are evenly spaced and mostly clear each other. Subsampling a
// DENSE pack instead picks spatially random points, so the survivors clump. Measured
// (labels shown / % overlapping / legible = shown-overlapping):
//   Group/Defs (49 cells, spacing 73px)  all 49 -> 18% -> 40 legible
//                                        budget 24 -> 17% ->  20 legible   <- a REGRESSION
//   __unplaced__ (1,516, spacing 15px)   all 1,516 -> 100% -> ~95 buried in a text wall
//                                        budget 24 ->  8% ->  22 legible   <- the fix
// So an ungated budget would hide 25 perfectly readable labels on Group/Defs to fix a
// level it doesn't share a problem with. Gate on the pack's own on-screen spacing:
// above the threshold the lattice carries its labels, so show them all and change
// nothing; below it, subsampling is the only way to be legible at all.
const SPACING_OK_PX = 55;     // 2*r*k at which a lattice's labels stop colliding
function updateLevelLabels(k) {
  if (!layout || layout.explorer) return;
  const sel = gLabels.selectAll("text.clab");
  const n = sel.size();
  if (!n) return;
  const spacing = 2 * (layout.cellR || 0) * (k || 1);
  const lim = spacing >= SPACING_OK_PX ? n
    : Math.max(12, Math.min(n, Math.round(LEVEL_LABEL_BUDGET * k * k)));
  sel.attr("display", function () { return Number(this.dataset.rank) < lim ? null : "none"; })
     .attr("font-size", CELL_LABEL_PX / (k || 1));
}
zoomBehav.on("zoom.lvlabels", ev => {
  if (layout && !layout.explorer) updateLevelLabels(ev.transform.k);
});

// ---- facet filter (the f bitmask; SCHEMA v2 `f` / v3 `fa`) ------------------
// Chips OR together; a CELL shows iff (f & mask) != 0. A FOLDER survives on its
// subtree-aggregate bits `fa` — testing a folder's own `f` (always 0 in v3) is
// what greys the whole canvas to "showing 0 of 77".
function applyFacetFilter(items) {
  for (const it of items) delete it.dim;
  if (!filterMask)
    return {items, shown: items.length, total: items.length, active: false};
  const match = it => ((it.f || 0) & filterMask) !== 0;
  const folderMatch = it => (((it.f || 0) | (it.fa || 0)) & filterMask) !== 0;
  const kept = [];
  let shown = 0;
  for (const it of items) {
    if (it.type === "folder") {
      // a folder stays bright and navigable whenever its SUBTREE matches, so
      // the user can descend to the matching cells instead of a dead level
      it.dim = !folderMatch(it);
      kept.push(it);
      if (!it.dim) shown++;
    } else if (match(it)) { kept.push(it); shown++; }
  }
  return {items: kept, shown, total: items.length, active: true};
}
function updateFilterStat(fv) {
  const el = $("#filterstat");
  if (!el) return;
  el.textContent = !fv || !fv.active ? ""
    : fv.text ? fv.text
    : `showing ${fv.shown} of ${fv.total}`;
}
// ============================ synapses =======================================
// A synapse is ONE undirected aggregate of every weak bond between two cells
// (SCHEMA: "src/dst are ordered lexicographically, not directionally") — so no
// arrowheads on the canvas; direction lives on each trace, in the drawer.
const EDGE_STYLE = {
  depends:         {color: "#a78bfa", dash: null,  label: "formal dependency"},
  generalization:  {color: "#38bdf8", dash: "4 3", label: "formalization claim (generalization)"},
  special_case:    {color: "#38bdf8", dash: "4 3", label: "formalization claim (special case)"},
  invocation:      {color: "#38bdf8", dash: "4 3", label: "formalization claim (invocation)"},
  related:         {color: "#38bdf8", dash: "4 3", label: "formalization claim (related)"},
  relates:         {color: "#fbbf24", dash: "5 3", label: "Wikidata relation (informal)"},
  mentions:        {color: "#94a3b8", dash: "2 3", label: "article mention (informal)"},
  "co-page":       {color: "#f472b6", dash: "5 3", label: "same external-database page"},
  "co-statement":  {color: "#2dd4bf", dash: "5 3", label: "same arXiv statement"},
  cites:           {color: "#fb923c", dash: "2 4", label: "stated in the literature (TheoremGraph)"},
  links:           {color: "#84cc16", dash: "2 2", label: "page link (external database)"},
};
const SYN_COLOR = "#7c8db5";   // the flat map ships weights only — no kind to colour by
// concept→decl claims that did not fuse the two into one atom (rules 2/3)
const FORM_FAMILY = new Set(["generalization", "special_case", "invocation", "related"]);
// the kind that gives a synapse its colour: the heaviest constituent
function dominantKind(kinds) {
  let best = null, bw = -1;
  for (const [k, v] of Object.entries(kinds || {})) if (v > bw) { bw = v; best = k; }
  return best;
}
// a synapse survives the Layers filter if ANY constituent bond does, and the
// Provenance filter if ANY trace does (an area-level synapse ships no traces —
// it can't be judged, so it is never silently dropped)
function synVisible(e, kinds, provs) {
  const ks = Object.keys(e.kinds || {});
  if (ks.length && !ks.some(k => kinds.has(k))) return false;
  const tr = e.traces || [];
  if (!tr.length) return true;
  return tr.some(t => provs.has(provClass(t.kind, manifest.prov[t.prov], t.evidence)));
}
let edgeStore = [];   // [{a, b, w, kinds, traces, tt}] for the level/ego views

function renderEdges() {
  gEdges.selectAll("*").remove();
  if (!layout || layout.explorer) return;
  const kinds = activeKinds(), provs = activeProv();
  const show = edgeStore
    .filter(e => synVisible(e, kinds, provs))
    .sort((x, y) => y.w - x.w).slice(0, 400);
  const maxW = show.reduce((m, e) => Math.max(m, e.w), 1);
  const widthOf = e => 0.7 + 2.4 * Math.sqrt(e.w / maxW);
  for (const e of show) {
    const A = layout.items.get(e.a), B = layout.items.get(e.b);
    if (!A || !B) continue;
    if (A.data.dim || B.data.dim) continue;   // filtered-out context folders
    const mx = (A.x + B.x) / 2, my = (A.y + B.y) / 2;
    const dx = B.x - A.x, dy = B.y - A.y;
    // deterministic per-pair bend so parallel routes fan out instead of piling
    let h = 0;
    const hk = e.a + "|" + e.b;
    for (let i = 0; i < hk.length; i++) h = (h * 31 + hk.charCodeAt(i)) >>> 0;
    const bend = (0.08 + (h % 1000) / 1000 * 0.22) * ((h & 1) ? 1 : -1);
    const cpx = mx - dy * bend, cpy = my + dx * bend;   // quadratic control point
    const d = `M${A.x},${A.y} Q${cpx},${cpy} ${B.x},${B.y}`;
    const st = EDGE_STYLE[dominantKind(e.kinds)] || {color: SYN_COLOR, dash: null};
    const baseOp = 0.3 + 0.45 * (e.w / maxW);
    const p = gEdges.append("path").attr("class", "link")
      .attr("d", d).attr("fill", "none")
      .attr("stroke", st.color).attr("stroke-width", widthOf(e))
      .attr("stroke-opacity", baseOp);
    if (st.dash) p.attr("stroke-dasharray", st.dash);
    // every drawn edge keeps its fat hit twin — an uninspectable edge reads as
    // a bug
    gEdges.append("path").attr("class", "hit")
      .attr("d", d).attr("fill", "none")
      .attr("stroke", "transparent").attr("stroke-width", 14)
      .style("cursor", "pointer")
      .on("mouseenter", () => p.attr("stroke-opacity", 0.95).attr("stroke-width", widthOf(e) + 1.4))
      .on("mouseleave", () => p.attr("stroke-opacity", baseOp).attr("stroke-width", widthOf(e)))
      .on("click", ev => { ev.stopPropagation(); showSynapsePanel(e.a, e.b, e); });
  }
  paintCommunities();
  updateStructStat();
}

// ---- level view ------------------------------------------------------------
// A folder level is laid out straight from the tree — no fetch. The synapse web
// among its CELLS costs one shard fetch per cell, so it only runs where that
// fan-out stays sane; a big folder's web is the Explorer's job (locality law).
const CELL_WEB_CAP = 60;
let webState = {shown: 0, cells: 0, capped: false};
async function enrich(seq, leaves) {
  const visible = new Set(leaves.map(l => l.data.id));
  const store = new Map();
  const put = (a, b, s) => {
    const key = a < b ? a + "|" + b : b + "|" + a;   // the SAME synapse is listed
    if (store.has(key)) return;                      // by both of its endpoints
    store.set(key, {a, b, w: s.w, kinds: s.kinds || {}, traces: s.traces || [], tt: s.tt});
  };
  // grandchild preview: faint inner circles (top 24 by size) — free, from the tree
  for (const l of leaves) {
    if (l.data.type !== "folder" || l.r <= 26) continue;
    const kids = (tree.sc[l.data.id] && tree.sc[l.data.id].children || [])
      .map(folderItem).sort((a, b) => b.n - a.n).slice(0, 24);
    if (kids.length < 2) continue;
    const inner = d3.hierarchy({children: kids})
      .sum(d => d.children ? 0 : Math.pow(Math.max(d.n || 1, 1), 0.6));
    d3.pack().size([l.r * 1.7, l.r * 1.7]).padding(2)(inner);
    for (const k of inner.leaves()) {
      gBubbles.append("circle").attr("class", "preview")
        .attr("cx", l.x - l.r * 0.85 + k.x).attr("cy", l.y - l.r * 0.85 + k.y)
        .attr("r", k.r).attr("fill", "none")
        .attr("stroke", "currentColor").attr("stroke-opacity", 0.14);
    }
  }
  // rule-5 synapses: a field concept's bonds hang off the FOLDER that holds it,
  // so a synapse endpoint may legitimately be a supercell. They ship on the
  // tree — free, and they carry no traces (the API has the full set).
  for (const l of leaves) {
    if (l.data.type !== "folder") continue;
    for (const s of (tree.sc[l.data.id] || {}).syn || [])
      if (visible.has(s.id)) put(l.data.id, s.id, s);
  }
  const cells = leaves.filter(l => l.data.type === "cell");
  webState = {shown: 0, cells: cells.length, capped: cells.length > CELL_WEB_CAP};
  if (cells.length && !webState.capped) {
    await Promise.all(cells.map(async l => {
      const e = await getEntry(l.data.id);
      if (seq !== renderSeq || !e) return;
      for (const s of e.syn || []) if (visible.has(s.id)) put(l.data.id, s.id, s);
    }));
  }
  if (seq !== renderSeq) return;
  edgeStore = [...store.values()];
  webState.shown = edgeStore.length;
  renderEdges();
  if (lastPanelId === focusId && !selectedId) renderPanel(focusId);
}

async function renderFocus(anim) {
  if (explorerOn) return renderExplorer(anim);
  const seq = ++renderSeq;
  resetZoom();
  await ensureTree();
  if (seq !== renderSeq) return;
  if (isCellId(focusId)) {
    const fe = await getEntry(focusId);
    if (seq !== renderSeq) return;
    if (fe) return renderCellEgo(seq, fe, anim);
    focusId = ROOTS_ID;   // unknown atom → don't strand the canvas
  }
  const items = await focusItems(focusId);
  if (seq !== renderSeq) return;
  const fv = applyFacetFilter(items);
  updateFilterStat(fv);
  const W = stageEl.clientWidth || 800, H = stageEl.clientHeight || 600;
  const root = d3.hierarchy({children: fv.items}).sum(d => d.children ? 0 : packValue(d));
  d3.pack().size([W, H]).padding(fv.items.length > 150 ? 1.5 : 4)(root);
  const leaves = root.leaves().filter(l => l.data.id);

  layout = {items: new Map(leaves.map(l => [l.data.id, l])), leaves};
  edgeStore = [];
  gEdges.selectAll("*").remove();
  gOverlay.selectAll("*").remove();
  gBubbles.selectAll("circle.preview").remove();
  drawNodes();
  drawLabels();
  drawSelRing();
  renderCrumb();
  const folders = fv.items.filter(i => i.type === "folder").length;
  statusEl.textContent = `${(fv.items.length - folders).toLocaleString()} cells · ` +
    `${folders} areas · ${focusId === ROOTS_ID ? "all libraries"
      : focusId === UNPLACED_ID ? "no formal home" : focusId.slice(5)}`;
  if (anim) fadeIn();
  enrich(seq, leaves);   // background: previews + the synapse web
}
function fadeIn() {
  const g = [gEdges, gBubbles, gOverlay, gLabels];
  for (const gr of g) gr.attr("opacity", 0).transition().duration(260).attr("opacity", 1);
  // rAF-driven transitions pause in background tabs — never leave the canvas
  // stuck invisible
  setTimeout(() => g.forEach(gr => { gr.interrupt(); gr.attr("opacity", 1); }), 600);
}

// ---- ego view: one atom and its synapses ------------------------------------
// The cell sits centered and its heaviest synapses fan around it on rings,
// ranked by weight. Deterministic placement, NOT a simulation — SCHEMA "Layout
// is BUILD-TIME: the client renders and never simulates". Labels come from
// labels.json, so the whole view costs the one shard fetch already made.
const EGO_CAP = 60;
async function renderCellEgo(seq, entry, anim) {
  const id = entry.cell.id;
  selectedId = id;
  const kinds = activeKinds(), provs = activeProv();
  const all = (entry.syn || []).map(s =>
    ({a: id, b: s.id, w: s.w, kinds: s.kinds || {}, traces: s.traces || [], tt: s.tt}));
  let shown = all.filter(e => synVisible(e, kinds, provs));
  const skipped = Math.max(0, shown.length - EGO_CAP);
  shown = shown.slice(0, EGO_CAP);          // syn ships sorted by weight
  const W = stageEl.clientWidth || 800, H = stageEl.clientHeight || 600;
  const cx = W / 2, cy = H / 2;
  const center = {data: {id, label: entry.cell.label || id, type: "cell",
                         f: entry.cell.f || 0, p: (entry.cell.supercells || [])[0] || null},
                  x: cx, y: cy, r: 22};
  const leaves = [center];
  // concentric rings, capacity growing with circumference: heaviest synapses
  // land closest, and no two neighbours share a point
  let i = 0, ring = 0;
  while (i < shown.length) {
    const rr = 105 + ring * 78;
    const cap = Math.max(8, Math.floor((2 * Math.PI * rr) / 58));
    const count = Math.min(cap, shown.length - i);
    for (let j = 0; j < count; j++, i++) {
      const a = -Math.PI / 2 + (j / count) * 2 * Math.PI + ring * 0.21;
      const it = cellItem(shown[i].b);
      leaves.push({data: it, x: cx + rr * Math.cos(a), y: cy + rr * Math.sin(a), r: 8});
    }
    ring++;
  }
  layout = {items: new Map(leaves.map(l => [l.data.id, l])), leaves, ego: true};
  edgeStore = shown.filter(e => layout.items.has(e.b));
  gEdges.selectAll("*").remove();
  gOverlay.selectAll("*").remove();
  gBubbles.selectAll("circle.preview").remove();
  drawNodes();
  drawLabels();
  renderEdges();
  drawSelRing();
  renderCrumb();
  statusEl.textContent = `${shown.length} synapse${shown.length === 1 ? "" : "s"}` +
    `${skipped ? ` (+${skipped} more in the card)` : ""} · ` +
    `${(entry.counts && entry.counts.organs) || (entry.organs || []).length} organs · cell view`;
  updateFilterStat(null);
  if (anim) fadeIn();
  renderPanel(id);
}
// ---- logical communities ---------------------------------------------------
// Greedy modularity merging over the level's `depends` synapses. Makes arXiv
// 2604.24797's Finding 1 visible — where dependency communities cut across the
// folder tree. Inside a folder every cell is formal, so the blue/grey fill
// carries no information there and the community tint costs nothing.
function mix(a, b, t) {
  const ch = (h, i) => parseInt(h.slice(1 + 2 * i, 3 + 2 * i), 16);
  const hx = x => Math.round(x).toString(16).padStart(2, "0");
  return "#" + [0, 1, 2].map(i => hx(ch(a, i) + (ch(b, i) - ch(a, i)) * t)).join("");
}
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
let commState = {n: 0, reason: ""};
function paintCommunities() {
  // clear pass: restore the base fills + drop any prior community ring
  gBubbles.selectAll("circle.node")
    .attr("stroke", null).attr("stroke-width", null).attr("stroke-opacity", null)
    .attr("fill", l => fillFor(l.data));
  if (!$("#commColor").checked) { commState = {n: 0, reason: "off"}; return; }
  if (!activeKinds().has("depends")) { commState = {n: 0, reason: "nodeps"}; return; }
  const nodes = [...layout.items.values()].filter(l => !l.data.dim);
  const idset = new Set(nodes.map(l => l.data.id));
  const links = edgeStore
    .filter(e => (e.kinds || {}).depends && idset.has(e.a) && idset.has(e.b))
    .map(e => ({a: e.a, b: e.b, w: e.kinds.depends}));
  const comm = communitiesOf(nodes.map(l => l.data.id), links);
  if (!comm) { commState = {n: 0, reason: "sparse"}; return; }
  const sizes = new Map();
  for (const c of comm.values()) sizes.set(c, (sizes.get(c) || 0) + 1);
  const colorOf = new Map();
  let ci = 0;
  gBubbles.selectAll("circle.node").each(function (l) {
    if (l.data.dim) return;
    const c = comm.get(l.data.id);
    if (c === undefined || sizes.get(c) < 2) return;
    if (!colorOf.has(c)) colorOf.set(c, COMM_PALETTE[ci++ % COMM_PALETTE.length]);
    const col = colorOf.get(c);
    const isFolder = l.data.type === "folder";
    d3.select(this).attr("fill", mix(fillFor(l.data), col, isFolder ? 0.34 : 0.62));
    if (isFolder) d3.select(this).attr("stroke", col)
      .attr("stroke-width", Math.max(2, Math.min(4.5, (l.r || 6) * 0.07)))
      .attr("stroke-opacity", 0.95);
  });
  commState = {n: colorOf.size, reason: colorOf.size ? "ok" : "sparse"};
}
// Live readout of what this level's web is doing, so the control visibly earns
// its place: the web is capped by fetch fan-out, and communities need deps.
function updateStructStat() {
  const el = $("#structstat");
  if (!el) return;
  const parts = [];
  if (webState.capped)
    parts.push(`${webState.cells.toLocaleString()} cells — too many to fetch each web; use the Explorer`);
  else if (webState.cells)
    parts.push(`${webState.shown} synapse${webState.shown === 1 ? "" : "s"} among ${webState.cells} cells`);
  if (commState.reason === "off") parts.push("communities off");
  else if (commState.reason === "nodeps") parts.push("communities need formal deps");
  else if (commState.reason === "sparse") parts.push("one community here");
  else if (commState.reason === "ok")
    parts.push(`${commState.n} logical ${commState.n === 1 ? "community" : "communities"}`);
  el.textContent = parts.join(" · ");
}
function drawSelRing() {
  gOverlay.selectAll("circle.selring").remove();
  const S = selectedId && layout && layout.items.get(selectedId);
  if (S) gOverlay.append("circle").attr("class", "selring")
    .attr("cx", S.x).attr("cy", S.y).attr("r", Math.max(S.r, 3) + 3);
}

// ============================ zoom navigation ================================
// The URL hash carries the whole shareable view state:
//   #<id>&f=<facet mask>&view=explorer
// The id segment is fully URI-encoded (any raw "&" became %26), so splitting on
// "&" is safe and a v2 "#Q181296" hash still resolves — through aliases.json.
function setHash(id) {
  let h = "#" + (id && id !== ROOTS_ID ? encodeURIComponent(id) : "");
  if (filterMask) h += "&f=" + filterMask;
  if (explorerOn) h += "&view=explorer";
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
    else if (k === "flat" && v !== "0") out.flat = true;
  }
  if (out.flat && !out.view) out.view = "explorer";   // pre-merge flatten links
  return out;
}
function setExplorer(on) {
  explorerOn = on;
  const b = $("#explorerbtn");
  if (b) b.classList.toggle("on", on);
  // the flat map ships weights only (explorer.json: [i, j, w]) — there is no
  // per-kind or per-trace data to filter on, so say so instead of no-op-ing
  for (const g of [$("#grp-layers"), $("#grp-prov")]) {
    if (!g) continue;
    g.classList.toggle("inert", on);
    g.title = on ? "the flat map ships synapse weights only — open a cell or an area to filter by kind/provenance" : "";
    g.querySelectorAll("input").forEach(cb => { cb.disabled = on; });
  }
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
// the supercell an atom calls home (for zoom-out + the breadcrumb). A cell with
// no decl organ has no breadcrumb at all — its home is the unplaced bucket.
function homeOf(entry) {
  const bc = entry.breadcrumb || [];
  return bc.length ? bc[bc.length - 1].id : UNPLACED_ID;
}
async function zoomOut() {
  if (focusId === ROOTS_ID) return;
  if (isCellId(focusId)) {
    const e = await getEntry(focusId);
    const home = e ? homeOf(e) : ROOTS_ID;
    const cell = focusId;
    focusId = home;
    selectedId = cell;                        // keep it ringed at its home level
    setHash(home);
    await renderFocus(true);
    return;
  }
  const parent = focusId === UNPLACED_ID ? ROOTS_ID
    : ((tree.sc[focusId] || {}).parent || ROOTS_ID);
  focusId = parent;
  selectedId = null;
  setHash(parent);
  await renderFocus(true);
}
svg.on("click", ev => {
  if (panMoved) { panMoved = false; return; }
  if (layout && layout.explorer) { explorerClick(ev); return; }
  zoomOut();
});

async function nodeClick(item) {
  if (layout && layout.explorer) {   // explorer: select + card, stay put
    selectedId = item.id;
    renderPanel(item.id);
    drawSelRing();
    return;
  }
  if (item.type === "strays") { renderPanel(focusId); return; }   // the card lists them
  if (item.type === "folder") {
    selectedId = null;
    renderPanel(item.id);
    await zoomInto(item.id);
    return;
  }
  await zoomInto(item.id);   // a cell → its ego view + card
}

// land the canvas on ANY id — a cell, an area, or any organ id (which resolves
// through aliases.json to the atom that owns it)
async function navigate(rawId) {
  if (explorerOn) setExplorer(false);   // navigation = travel to the atom's home
  const id = await resolveId(rawId);
  if (!id) { renderPanel(rawId); return; }
  focusId = id;
  selectedId = isCellId(id) ? id : null;
  setHash(id);
  renderPanel(id);
  await renderFocus(true);
}

function pathChain(p) {
  const out = [];
  let cur = p;
  for (let i = 0; i < 24 && cur && tree.sc[cur]; i++) {
    out.unshift({id: cur, label: tree.sc[cur].label || cur});
    cur = tree.sc[cur].parent;
  }
  return out;
}
async function renderCrumb() {
  let html = `<a data-nav="${ROOTS_ID}">all libraries</a>`;
  if (focusId === UNPLACED_ID) {
    html += ` <span class="sep">/</span> <b>no formal home</b>`;
  } else if (isCellId(focusId)) {
    const e = await getEntry(focusId);
    for (const b of (e && e.breadcrumb) || [])
      html += ` <span class="sep">/</span> <a data-nav="${esc(b.id)}">${esc(b.label)}</a>`;
    if (e && !(e.breadcrumb || []).length)
      html += ` <span class="sep">/</span> <a data-nav="${UNPLACED_ID}">no formal home</a>`;
    html += ` <span class="sep">/</span> <b>● ${esc((e && e.cell.label) || focusId)}</b>`;
  } else if (focusId !== ROOTS_ID) {
    for (const b of pathChain(focusId))
      html += ` <span class="sep">/</span> ` + (b.id === focusId
        ? `<b>${esc(b.label)}</b>` : `<a data-nav="${esc(b.id)}">${esc(b.label)}</a>`);
  }
  crumbEl.innerHTML = html;
  crumbEl.querySelectorAll("[data-nav]").forEach(a =>
    a.addEventListener("click", () => {
      if (a.dataset.nav === ROOTS_ID) { focusId = ROOTS_ID; selectedId = null;
        setHash(""); renderFocus(true); renderPanel(ROOTS_ID); }
      else navigate(a.dataset.nav);
    }));
}
// ============================ panel ==========================================
const XREF_NAME = {mathworld: "MathWorld", nlab: "nLab", proofwiki: "ProofWiki",
  eom: "Encyclopedia of Math", planetmath: "PlanetMath", metamath: "Metamath",
  lmfdb_knowl: "LMFDB", oeis: "OEIS", dlmf: "DLMF", msc: "MSC",
  stacks: "Stacks Project", kerodon: "Kerodon"};
// Whether a source's LICENCE permits storing its text — mirrors
// catalog/data/source_registry.json `crossref_sources.<db>.ingest.snippets`, the
// single source of truth (and nodes.jsonl `_meta.licenses.external`, which names
// mathworld/dlmf/eom/kerodon as the no-content sources).
//
// This is a PER-SOURCE POLICY and it is NOT the same question as "did this organ
// ship with text". Conflating the two is a licensing LIE, and it fires constantly:
// all 296 supercell area-page organs are snippet-stripped for supercells.json's
// eager-fetch byte budget, and 160 of them come from sources that expressly permit
// their text (stacks 106, proofwiki 27, nlab 22, planetmath 5). Stacks is GFDL and
// the text is sitting in catalog/data/external/stacks_pages.jsonl — telling a
// reader Stacks' licence forbids quoting it defames the source, and licensing
// honesty is this project's whole point. Distinguish the two cases; never guess.
const DB_SNIPPETS = {
  nlab: true, proofwiki: true, lmfdb_knowl: true, oeis: true, planetmath: true,
  stacks: true,                                    // ingest.snippets: true
  mathworld: false, eom: false, dlmf: false, kerodon: false,   // ingest.snippets: false
};
// Why is there no text here? Two different facts, and they must never read alike.
// Returns null when we genuinely do not know the source's policy — in which case we
// say only what we can see (it isn't in this shard), never what the licence allows.
function snippetAbsence(db) {
  const name = XREF_NAME[db] || db || "this source";
  if (DB_SNIPPETS[db] === false)
    return {licensed: false, short: `no stored content — ${name}'s licence permits ids, titles and links only`,
            prose: `stores ids, titles and links only — ${name}'s licence permits no more`};
  if (DB_SNIPPETS[db] === true)
    return {licensed: true, short: `${name}'s licence permits its text, but this snippet wasn't carried into this shard`,
            prose: `permits its text under its own licence, but this snippet wasn't carried into this shard`};
  return {licensed: null, short: `no text in this shard`,
          prose: `has no text stored in this shard`};
}
// The pointer to the surface that DOES serve the text — only ever shown when the
// licence actually permits it (or when we don't know), never as a promise the
// source's terms forbid us to keep.
const SNIP_API = `fetch it from <code>/api/brain/snippets</code>`;
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
// external-data urls are template-built by the ingest adapters, but never trust
// a stored url into an href without a scheme check (javascript:/data: would ride
// through esc() untouched)
function safeUrl(u) { return u && /^https?:\/\//i.test(u) ? u : null; }
function organUrl(id) {
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
// The drawer used to dump raw JSON. Instead we say what the bond ASSERTS and
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
  wikilean: "the WikiLean annotation stack",
  "tag-queue": "the @[wikidata] tag queue (AI-generated, not yet in Mathlib)",
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
    // machine bonds come from the kernel / page scrapes regardless of which file
    // happened to carry them — never mislabel a formal dep as "TheoremGraph"
    src = kind === "links" ? "the external database's own hyperlinks"
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
// One organ's row in an evidence trace. `who` is clickable-navigable (through
// aliases, so it lands on the atom that owns the organ); ext pages also get a ↗
// deep link. Labels resolve lazily (data-lbl) via enrichEvidence when only an id
// is known at render time.
function traceStep(role, id, label, tag) {
  const isExt = id && id.startsWith("xref:");
  const shown = label || (isExt ? extValueOf(id) : id);
  const needsLbl = !label && !isExt;   // a bare QID / decl id → resolve async
  const url = isExt ? organUrl(id) : null;
  const who = `<span class="nav" data-nav="${esc(id)}"${
    needsLbl ? ` data-lbl="${esc(id)}"` : ""}>${esc(shown)}</span>${
    url ? ` <a class="extlink" href="${esc(url)}" rel="noopener" target="_blank" title="view on the source site">↗</a>` : ""}`;
  return `<div class="ev-step"><span class="role">${role}</span>` +
    `<span class="who">${who}</span>${tag ? ` <span class="tag">${esc(tag)}</span>` : ""}</div>`;
}
function connector(text) { return `<div class="ev-conn">↓ ${esc(text)}</div>`; }

// The step-by-step chain behind a `links` bond + a lazily-loaded snippet of the
// page whose text actually contains the link (data-snip-page). `ctx` carries the
// two endpoint {id,label} in from→to order.
function linkTraceHtml(ev, ctx) {
  ctx = ctx || {};
  const via = ev.via || (ctx.fromId && ctx.fromId.startsWith("xref:") ? extDbOf(ctx.fromId)
            : ctx.toId && ctx.toId.startsWith("xref:") ? extDbOf(ctx.toId) : null);
  const dbName = via ? (XREF_NAME[via] || via) : "the external database";
  let steps = "", snipPage = null;
  if (ev.projected) {
    // concept → its page → (link) → other page → other concept
    const srcPage = `xref:${via}:${ev.src_page}`, dstPage = `xref:${via}:${ev.dst_page}`;
    snipPage = srcPage;
    steps =
      traceStep("A", ctx.fromId, ctx.fromLabel, "concept") +
      connector(`cross-referenced in ${dbName}`) +
      traceStep("", srcPage, ev.src_page, `${dbName} page`) +
      connector(`internal link on ${dbName}`) +
      traceStep("", dstPage, ev.dst_page, `${dbName} page`) +
      connector(`cross-referenced in ${dbName}`) +
      traceStep("B", ctx.toId, ctx.toLabel, "concept");
  } else {
    // page → (link) → page (the endpoints ARE the pages)
    snipPage = ctx.fromId && ctx.fromId.startsWith("xref:") ? ctx.fromId : null;
    steps =
      traceStep("", ctx.fromId, ctx.fromLabel, `${dbName} page`) +
      connector(`links to it${ev.context ? ` (in the ${ev.context})` : ""}`) +
      traceStep("", ctx.toId, ctx.toLabel, `${dbName} page`);
  }
  const snip = snipPage
    ? `<div class="ev-snip loading" data-snip-page="${esc(snipPage)}">loading the linking page…</div>`
    : "";
  return `<div class="ev-trace">${steps}</div>${snip}`;
}

// The prose behind ONE trace. `kind` is always a TRACE kind (the single call site
// is the synapse drawer), and in v3 that set is closed and measured: over all
// 115,174 traces in brain/data/synapses.jsonl the kinds are depends, links,
// mentions, cites, relates, co-page, co-statement, invocation, related,
// special_case, generalization — and nothing else. The v2 organ-level bonds
// (`formalizes`, `matches`, `xref`) moved INSIDE the cell and are rendered by
// organHtml/bondChip, so their branches here were ~90 lines of stale carry-over
// that read as live contract: a future edit to how a cross-database identity is
// worded would plausibly have been made in the dead `xref` branch and silently had
// no effect. Deleted. Anything unforeseen falls through to the generic tail below.
function evidenceProse(kind, ev, prov, otherId, ctx) {
  ev = ev || {};
  let lead = "", detail = "";

  if (kind === "depends") {
    lead = `<b>Formal dependency.</b> The proofs on the left use the declaration on the right.`;
    const wt = ev.w_types || {}, bits = [];
    if (wt.sig) bits.push(`${wt.sig.toLocaleString()} statement-level references`);
    if (wt.proof) bits.push(`${wt.proof.toLocaleString()} uses inside proofs`);
    if (wt.def) bits.push(`${wt.def.toLocaleString()} uses in definitions`);
    detail += evList(bits);
    const wit = ev.witnesses;
    if (wit && wit.length)
      detail += `<div class="ev-sub">for example, <code>${esc(shortDecl(wit[0][0]))}</code> uses <code>${esc(shortDecl(wit[0][1]))}</code></div>`;
  } else if (FORM_FAMILY.has(kind)) {
    // A concept→decl claim that did NOT fuse the two into one atom: `exact`
    // fuses (rule 1), a home-less concept attaches to its single best
    // generalization/special_case target (rule 2), and invocation/related never
    // merge (rule 3). Everything left over is a real relationship, kept here.
    const mk = MK_LABEL[ev.match_kind || kind] || ev.match_kind || kind;
    const reviewed = (prov && String(prov.method || "").includes("verified")) ||
      (ev.skeptic && ev.skeptic !== "pending");
    lead = `<b>Formalization claim (unmerged).</b> This concept↔declaration claim is graded <b>${
      esc(mk)}</b>, which does not fuse the two into one atom — so it stays a synapse between them${
      ev.verified_by ? `. The declaration was verified to exist in Mathlib` : ""}${
      reviewed ? "; the match also passed skeptic review" : ""}.`;
    const d = [];
    if (ev.module) d.push(`declared in <code>${esc(ev.module)}</code>`);
    if (ev.skeptic === "pending") d.push(`skeptic review: <b>pending</b>`);
    if (ev.verified_by) d.push(`existence oracle: <b>${esc(ev.verified_by)}</b>`);
    detail += evList(d);
    if (ev.grounding_note) detail += `<div class="ev-sub">“${esc(ev.grounding_note)}”</div>`;
  } else if (kind === "relates") {
    lead = `<b>Wikidata relation.</b> Wikidata records a direct relationship between these two concepts.`;
    const props = ev.properties || [];
    if (props.length) detail = evList(props.map(p => `${esc(p.label || p.p)} <span class="pin">(${esc(p.p)})</span>`));
  } else if (kind === "mentions") {
    const n = ev.n_annotations || ev.total || (ev.sample ? ev.sample.length : 1);
    lead = `<b>Article mention.</b> ${ev.role === "article"
      ? "This is the concept's annotated Wikipedia mirror on WikiLean, carrying"
      : "A WikiLean article on one side cites the other's declaration in"} <b>${n}</b> Lean annotation${n > 1 ? "s" : ""}.`;
    if (ev.sample && ev.sample.length)
      detail = evList(ev.sample.filter(s => s.label).slice(0, 4).map(s => `“${esc(s.label)}” — ${statusChip(s.status)}`));
    else if (ev.statuses)
      detail = evList(Object.entries(ev.statuses).map(([k, v]) => `${v} ${STATUS_WORD[k] || k}`));
  } else if (kind === "co-page") {
    // SCHEMA rule 4: a page claimed by >1 cell is evidence the claimants are
    // RELATED, not that either owns it — so the page becomes an area-level organ
    // and the claimants get this weak synapse.
    const db = ev.db || (ev.page ? extDbOf(ev.page) : null);
    const dbName = db ? (XREF_NAME[db] || db) : null;
    lead = `<b>Same object, two entries.</b> Both atoms cross-reference the same page${
      dbName ? ` in <b>${esc(dbName)}</b>` : ""}${
      ev.label ? ` (<code>${esc(ev.label)}</code>)` : ""}. A page claimed by more than one
      atom never merges them — it is evidence they are related, so it hangs off their
      common area instead and leaves this synapse behind.`;
    if (ev.page) {
      const url = organUrl(ev.page);
      detail = `<div class="ev-sub">the shared page: <span class="nav" data-nav="${esc(ev.page)}" data-lbl="${esc(ev.page)}">${
        esc(extValueOf(ev.page))}</span>${
        url ? ` <a class="extlink" href="${esc(url)}" rel="noopener" target="_blank">↗</a>` : ""}</div>` +
        `<div class="ev-snip loading" data-snip-page="${esc(ev.page)}">loading the shared page…</div>`;
    }
  } else if (kind === "co-statement") {
    lead = `<b>Same statement, two atoms.</b> One arXiv statement was matched to declarations
      in both atoms${ev.label ? ` — “${esc(ev.label)}”` : ""}. Attaching it to either would put
      one organ in two cells, so it stays a synapse between them.`;
    if (ev.statement) {
      const url = organUrl(ev.statement);
      detail = `<div class="ev-sub">the shared statement: <code>${esc(ev.statement)}</code>${
        url ? ` <a class="extlink" href="${esc(url)}" rel="noopener" target="_blank">↗</a>` : ""}</div>`;
    }
  } else if (kind === "cites") {
    lead = `<b>Stated in the literature.</b> This result appears in the mathematical literature; ${judgeVerdict(ev)}.`;
    if (ev.via_decls && ev.via_decls.length)
      detail = `<div class="ev-sub">via ${ev.via_decls.slice(0, 3).map(d => `<code>${esc(shortDecl(d))}</code>`).join(", ")}</div>`;
  } else if (kind === "links") {
    const db = ev.via || (ctx && ctx.fromId && ctx.fromId.startsWith("xref:") ? extDbOf(ctx.fromId)
      : otherId && otherId.startsWith && otherId.startsWith("xref:") ? extDbOf(otherId) : null);
    const dbName = db ? (XREF_NAME[db] || db) : "the external database";
    lead = ev.projected
      ? `<b>Projected link.</b> These two atoms are joined because <b>${esc(dbName)}</b>'s own pages link to each other — the trace below shows exactly how:`
      : `<b>Page link.</b> One page hyperlinks the other inside <b>${esc(dbName)}</b>${
          ev.context ? `, in the ${esc(ev.context)}` : ""} — the trace below shows which and quotes the linking page:`;
    detail = linkTraceHtml(ev, ctx);
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

// An organ id → the label its owning atom gives it. v3 has no per-organ shard,
// so this goes through aliases (organ → atom) and reads the organ back off the
// atom's entry — one cached fetch, and it is the atom's own wording.
const organInfoCache = new Map();
function organInfo(id) {
  if (!organInfoCache.has(id)) {
    organInfoCache.set(id, (async () => {
      const owner = await resolveId(id);
      if (!owner) return null;
      if (isPathId(owner)) {
        const sc = (tree.sc || {})[owner];
        const o = ((sc || {}).organs || []).find(x => x.id === id);
        return o || (sc ? {label: sc.label, id} : null);
      }
      const e = await getEntry(owner);
      if (!e) return null;
      return (e.organs || []).find(x => x.id === id) || {label: e.cell.label, id};
    })().catch(() => null));
  }
  return organInfoCache.get(id);
}

// Post-render enrichment of evidence traces: resolve organ labels the
// synchronous render couldn't know, and quote the actual page whose text
// contains a `links`/`co-page` bond (its stored snippet, with its licence).
// Best-effort: any miss just leaves the placeholder text. Scoped to `root` so a
// newer panel render can't be clobbered by an older in-flight fetch.
async function enrichEvidence(root) {
  // skip work inside a collapsed drawer (display:none → no offsetParent); the
  // drawer-open handler re-runs enrichEvidence on expand
  const vis = el => el.offsetParent !== null;
  root.querySelectorAll("[data-lbl]").forEach(async el => {
    if (!vis(el)) return;
    const id = el.dataset.lbl;
    const o = await organInfo(id);
    if (o && o.label && root.contains(el)) {
      el.textContent = o.label;
      el.removeAttribute("data-lbl");
    }
  });
  root.querySelectorAll(".ev-snip[data-snip-page]").forEach(async box => {
    if (!vis(box)) return;
    const pid = box.dataset.snipPage;
    const db = extDbOf(pid), dbName = XREF_NAME[db] || db;
    const o = await organInfo(pid);
    if (!root.contains(box)) return;
    box.removeAttribute("data-snip-page");   // one-shot: re-opens don't refetch
    const url = (o && safeUrl(o.url)) || organUrl(pid);
    const title = (o && o.label) || extValueOf(pid);
    const link = url ? ` <a href="${esc(url)}" rel="noopener" target="_blank">read on ${esc(dbName)} ↗</a>` : "";
    box.classList.remove("loading");
    // a snippet NEVER renders without its licence — if the licence didn't ship,
    // neither does the text
    if (o && o.snippet && o.snippet_license) {
      box.innerHTML = `“${esc(o.snippet)}”<span class="cite">— from “${esc(title)}” on ${
        esc(dbName)} · ${esc(o.snippet_license)}${link}</span>`;
    } else {
      // No text. Which reason? EVERY co-page trace lands here (rule 4 routes a
      // multi-claimant page to a SUPERCELL, whose organs ship snippet-stripped),
      // so "the licence forbids it" would be a lie on most of them — see
      // DB_SNIPPETS. Say the true thing and point at the surface that has it.
      const a = snippetAbsence(db);
      box.innerHTML = `<span class="cite">“${esc(title)}” on ${esc(dbName)} ${
        esc(a.prose)}${a.licensed === false ? "." : ` — ${SNIP_API}.`}${link}</span>`;
    }
  });
}
// ---- organ provenance ------------------------------------------------------
// C7's whole point: a merged @[wikidata] tag and an AI-queued candidate make the
// SAME claim, so they must never read alike. `source: "tag-queue"` is the AI one.
function provChipHtml(prov) {
  if (!prov) return "";
  const src = String(prov.source || ""), meth = String(prov.method || "");
  let cls = "ai", text = src.replace(/_/g, " "), title = meth;
  if (src === "tag-queue") {
    cls = "ai";
    text = "AI-queued tag — not in Mathlib";
    title = meth + (prov.queue ? ` · queue: ${prov.queue}` : "") +
      " — an AI proposed this @[wikidata] tag; it has NOT been merged into mathlib4";
  } else if (meth.includes("@[")) {
    cls = "human";
    text = meth.split(" ")[0] + " · merged";
    title = "hand-written in the mathlib4 source and merged upstream";
  } else if (meth === "wikidata-property") {
    cls = "human"; text = (XREF_NAME[src] || src) + " · Wikidata";
    title = "a Wikidata external-ID property (CC0)";
  } else if (meth === "wikidata-claims") { cls = "human"; text = "Wikidata claims"; }
  else if (meth === "container_links") { cls = "machine"; text = "deterministic"; }
  else if (meth === "external-ingest page qid") {
    cls = "machine"; text = (XREF_NAME[src] || src) + " · page QID";
    title = "the source database's own page states the QID";
  } else if (src === "mathlib_deps") { cls = "machine"; text = "Lean kernel"; }
  else if (src === "wikilean") { cls = "ai"; text = "WikiLean article"; }
  else if (src === "annotations") { cls = "ai"; text = "annotations"; }
  else if (src === "theoremgraph") {
    cls = "ai"; text = "TheoremGraph"; title = meth + " (LLM-judged, CC-BY-SA-4.0)";
  } else if (meth.includes("agent")) { cls = "ai"; text = "AI agent + oracle"; }
  else if (meth.includes("discovery_proposals")) { cls = "ai"; text = "discovery (verified)"; }
  const pin = prov.pin ? ` · ${String(prov.pin).slice(0, 10)}` : "";
  return `<span class="prov ${cls}" title="${esc(title + pin)}">${esc(text)}</span>`;
}
const BOND_TITLE = {
  exact: "the concept IS this declaration's formalization — `exact` asserts identity, and identity fuses both ways (SCHEMA rule 1)",
  generalization: "this concept has no `exact` declaration of its own, so it attaches to its single best generalization target (SCHEMA rule 2)",
  special_case: "this concept has no `exact` declaration of its own, so it attaches to its single best special-case target (SCHEMA rule 2)",
  xref: "an external-database page this atom cross-references, claimed by this atom alone (SCHEMA rule 4)",
  article: "the WikiLean article about this object",
  matches: "a TheoremGraph match between an arXiv statement and a Lean declaration here",
  field: "a field-of-study concept: its formal home is this folder, never a cell (SCHEMA rule 5)",
};
function bondChip(bond) {
  if (!bond) return "";
  return `<span class="bond ${bond === "exact" ? "exact" : ""}" title="${
    esc(BOND_TITLE[bond] || bond)}">${esc(bond)}</span>`;
}
const ORGAN_ORDER = ["concept", "decl", "article", "page", "statement"];
const ORGAN_HEAD = {
  concept: ["Wikidata concepts", "the informal identity — an atom may hold several (Module holds both “Module” and “Vector space”)"],
  decl: ["Lean declarations", "the formal identity — the code that IS this object"],
  article: ["WikiLean articles", "the annotated Wikipedia mirror of this object"],
  page: ["External database pages", "the same object, catalogued elsewhere"],
  statement: ["arXiv statements", "where this object is stated in the literature"],
};

// One organ, with its payload rendered in full — the card is ONE fetch, so
// nothing here is a promise of data that lives elsewhere.
function organHtml(o, anchor) {
  const isAnchor = o.id === anchor;
  const url = safeUrl(o.url) || organUrl(o.id);
  let head = `<div class="srchead"><span class="oname">${esc(o.label || o.id)}</span>`;
  if (isAnchor) head += ` <span class="uc-anchor" title="the organ that NAMES this atom — the anchor (SCHEMA v3 “Identity”)">anchor</span>`;
  head += bondChip(o.bond);
  if (o.kind === "page" && o.db)
    head += ` <span class="badge" style="border-color:${esc(DB_COLOR[o.db] || "#c8bfa8")}">${
      esc(XREF_NAME[o.db] || o.db)}</span>`;
  if (o.kind === "decl" && o.decl_kind) head += ` <span class="mk">${esc(o.decl_kind)}</span>`;
  if (url) head += ` <a class="extlink" href="${esc(url)}" rel="noopener" target="_blank">↗</a>`;
  head += provChipHtml(o.prov !== undefined ? manifest.prov[o.prov] : null);
  head += `</div>`;
  let body = "";
  if (o.kind === "concept") {
    if (o.description)
      body += `<div class="snip">${esc(o.description)}</div>
        <div class="srclic">Wikidata (CC0) · <a href="https://www.wikidata.org/wiki/${
        esc(o.id)}" rel="noopener" target="_blank">${esc(o.id)}</a></div>`;
    const bits = [];
    if (o.status) bits.push(`<span class="badge ${o.status === "formalized" ? "f"
      : o.status === "partial" ? "p" : "n"}">${esc(String(o.status).replace("_", " "))}</span>`);
    const aa = o.article_annotations;
    if (aa) bits.push(`<span class="chip"><a href="/${esc(o.slug || "")}">article</a>:
      <b>${aa.total}</b> annotations</span>
      <span class="badge f">${aa.formalized} formalized</span>
      <span class="badge p">${aa.partial} partial</span>
      <span class="badge n">${aa.not_formalized} not</span>`);
    if (bits.length) body += `<div class="chips">${bits.join(" ")}</div>`;
    if (o.slug)
      body += `<details class="srcacc" data-wplead="${esc(o.slug)}"><summary>read the Wikipedia lead</summary>
        <div class="wplead"><p class="note">loading…</p></div></details>`;
  } else if (o.kind === "decl") {
    if (o.code)
      body += `<div class="codeblock"><pre>${esc(o.code)}</pre><span class="src">${
        esc(o.decl_kind || "decl")} — mathlib4 source (Apache-2.0)${
        o.module ? ` · <code>${esc(o.module)}</code>` : ""} · <a href="${
        esc(organUrl(o.id))}" rel="noopener" target="_blank">${esc(o.library || "Mathlib")} docs ↗</a></span></div>`;
    if (o.docstring) body += `<p class="osub">${esc(o.docstring)}</p>`;
    if (!o.code && o.module) body += `<p class="osub"><code>${esc(o.module)}</code></p>`;
  } else if (o.kind === "article") {
    const aa = o.annotations;
    body += `<div class="chips"><span class="chip"><a href="/${esc(o.id)}">WikiLean article</a>${
      aa ? `: <b>${aa.total}</b> Lean annotations` : ""}</span>${
      aa ? `<span class="badge f">${aa.formalized} formalized</span>
            <span class="badge p">${aa.partial} partial</span>
            <span class="badge n">${aa.not_formalized} not</span>` : ""}
      <span class="chip"><a href="https://en.wikipedia.org/wiki/${esc(o.id)}" rel="noopener" target="_blank">Wikipedia</a></span></div>`;
  } else if (o.kind === "page") {
    // A snippet NEVER renders without its licence. When there is no text, say
    // WHICH of the two reasons applies — see DB_SNIPPETS: a licence that permits
    // ids/titles/links only is a fact about the SOURCE; a missing snippet on an
    // area-page organ is a fact about THIS SHARD, and the text is a call away.
    const readLink = url
      ? ` · <a href="${esc(url)}" rel="noopener" target="_blank">read at ${
        esc(XREF_NAME[o.db] || o.db)} ↗</a>` : "";
    if (o.snippet && o.snippet_license)
      body += `<div class="snip">${esc(o.snippet)}</div>
        <div class="srclic">${esc(o.snippet_license)}${readLink}</div>`;
    else {
      const a = snippetAbsence(o.db);
      body += `<div class="srclic">${esc(a.short)}${
        a.licensed === false ? "" : ` — ${SNIP_API}`}${readLink}</div>`;
    }
    if (o.kind_hint) body += `<p class="osub">${esc(o.kind_hint)}</p>`;
    // `claimants` ships as an ARRAY of the claiming cell ids (SCHEMA rule 4 — 121
    // of the 296 supercell organs carry one), so interpolating it where a COUNT
    // belongs splices the raw comma-joined ids into the sentence. Count them, and
    // escape the ids into the tooltip — this was the one interpolation in organHtml
    // that bypassed esc().
    const cl = Array.isArray(o.claimants) ? o.claimants
      : (o.claimants ? [o.claimants] : []);
    if (cl.length)
      body += `<p class="osub" title="${esc(cl.join(", "))}">claimed by ${cl.length} atom${
        cl.length === 1 ? "" : "s"} — an area page (SCHEMA rule 4)</p>`;
  } else if (o.kind === "statement") {
    body += `<p class="osub">appears as <b>${esc(o.ref || "?")}</b> of
      <a href="${esc(organUrl(o.id))}" rel="noopener" target="_blank">${esc(o.arxiv_id || o.id)}</a>${
      o.license_open ? "" : " — text not redistributable, link only"}</p>`;
  }
  return `<div class="srcrow">${head}${body}</div>`;
}

// ---- the cell card: the atom, its organs, its synapses ---------------------
function cellHeaderHtml(entry) {
  const c = entry.cell;
  const organs = entry.organs || [];
  const concept = organs.find(o => o.kind === "concept" && o.description)
    || organs.find(o => o.kind === "concept");
  const qid = organs.find(o => o.kind === "concept");
  const article = organs.find(o => o.kind === "article");
  const decls = organs.filter(o => o.kind === "decl");
  let h = `<div class="unitcard"><h2>${esc(c.label || c.id)}</h2>`;
  if (concept && concept.description)
    h += `<div class="uc-desc">${esc(concept.description)}<span class="uc-src">— Wikidata (CC0)</span></div>`;
  const chips = [];
  const slug = (article && article.id) || (qid && qid.slug);
  if (slug) chips.push(`<span class="chip"><a href="/${esc(slug)}">WikiLean article</a></span>`);
  if (slug) chips.push(`<span class="chip"><a href="https://en.wikipedia.org/wiki/${
    esc(slug)}" rel="noopener" target="_blank">Wikipedia</a></span>`);
  if (qid) chips.push(`<span class="chip"><a href="https://www.wikidata.org/wiki/${
    esc(qid.id)}" rel="noopener" target="_blank">${esc(qid.id)}</a></span>`);
  for (const d of decls.slice(0, 8))
    chips.push(`<span class="chip"><a href="${esc(organUrl(d.id))}" rel="noopener" target="_blank">${
      esc(shortDecl(d.label || d.id))}</a></span>`);
  if (decls.length > 8) chips.push(`<span class="chip">+${decls.length - 8} more decls</span>`);
  for (const p of c.supercells || [])
    chips.push(`<span class="chip"><a data-nav="${esc(p)}">${esc(p.slice(5))}</a></span>`);
  return h + `<div class="chips">${chips.join("")}</div></div>`;
}
async function renderCellPanel(id, e) {
  const c = e.cell, organs = e.organs || [];
  let html = "";
  if (e.breadcrumb && e.breadcrumb.length)
    html += `<div class="crumb">` + e.breadcrumb.map(b =>
      `<a data-nav="${esc(b.id)}">${esc(b.label)}</a>`).join(" / ") + `</div>`;
  html += cellHeaderHtml(e);
  const nOrg = (e.counts && e.counts.organs) || organs.length;
  const nSyn = (e.counts && e.counts.syn) || (e.syn || []).length;
  html += `<div class="sub">cell · <code>${esc(c.id)}</code> · ${nOrg} organ${
    nOrg === 1 ? "" : "s"} · ${nSyn.toLocaleString()} synapse${nSyn === 1 ? "" : "s"}${
    (c.supercells || []).length > 1
      ? ` · spans ${c.supercells.length} modules — it renders inside each` : ""}</div>`;

  // organs, grouped by kind: the informal identity, the formal identity, the
  // article, the outside world, the literature — in that order
  const byKind = new Map();
  for (const o of organs) {
    if (!byKind.has(o.kind)) byKind.set(o.kind, []);
    byKind.get(o.kind).push(o);
  }
  const order = [...ORGAN_ORDER, ...[...byKind.keys()].filter(k => !ORGAN_ORDER.includes(k))];
  for (const k of order) {
    const rows = byKind.get(k);
    if (!rows) continue;
    const [head, why] = ORGAN_HEAD[k] || [k, ""];
    html += `<section class="kind"><h3 title="${esc(why)}">${esc(head)}
      <span class="cnt">(${rows.length})</span></h3>`;
    for (const o of rows) html += organHtml(o, c.anchor);
    html += `</section>`;
  }

  // synapses, heaviest first (the shard ships them sorted)
  const kinds = activeKinds(), provs = activeProv();
  const syn = (e.syn || []).filter(s =>
    synVisible({kinds: s.kinds, traces: s.traces}, kinds, provs));
  if (syn.length) {
    const trunc = (e.truncated && e.truncated.syn) || 0;
    html += `<section class="kind"><h3>Synapses <span class="cnt">(${syn.length}${
      trunc ? ` shown of ${nSyn.toLocaleString()}` : ""})</span></h3>`;
    syn.slice(0, 40).forEach((s, i) => {
      const st = EDGE_STYLE[dominantKind(s.kinds)] || {color: SYN_COLOR};
      html += `<div class="edge"><div class="row" data-syn="${i}">
        <span style="color:${st.color}">●</span>
        <span>${esc(synLabel(s.id))}</span>
        <span class="mk">${esc(Object.keys(s.kinds || {}).join(", "))}</span>
        <span class="prov" title="the number of constituent bonds">weight ${s.w}</span></div></div>`;
    });
    if (syn.length > 40)
      html += `<div class="more">… ${syn.length - 40} more shown here; the full set is at
        <code>/api/brain/*</code> or <code>brain/query.py</code></div>`;
    if (trunc)
      html += `<div class="more">${trunc.toLocaleString()} lighter synapses were trimmed from
        this shard (cap: ${manifest._meta.caps.synapses_per_cell}/cell) — the full set is at
        <code>/api/brain/*</code>.</div>`;
    html += `</section>`;
  }
  html += `<div id="community-slot"></div>`;
  panelEl.innerHTML = html;
  wirePanel();
  // clicking a synapse row opens its drawer in the panel
  panelEl.querySelectorAll("[data-syn]").forEach(r =>
    r.addEventListener("click", ev => {
      if (ev.target.closest("a")) return;
      const s = syn[Number(r.dataset.syn)];
      showSynapsePanel(id, s.id, {a: id, b: s.id, w: s.w, kinds: s.kinds,
                                  traces: s.traces || [], tt: s.tt});
    }));
  // the community overlay is keyed by v2 node ids — the anchor IS one
  renderCommunity(c.anchor, id);
}

// ---- the synapse drawer ----------------------------------------------------
// Weight, the kind histogram, and EVERY trace the shard carries — each one named
// down to the actual database and page, in the same prose the node drawers use.
async function synBetween(a, b) {
  for (const [x, y] of [[a, b], [b, a]]) {
    if (!isCellId(x)) continue;
    const e = await getEntry(x);
    const s = e && (e.syn || []).find(s2 => s2.id === y);
    if (s) return {a, b, w: s.w, kinds: s.kinds || {}, traces: s.traces || [], tt: s.tt};
  }
  return null;
}
// The label for a synapse's FAR ENDPOINT, which may legitimately be a supercell:
// a field concept's bonds hang off the module that holds it (SCHEMA rule 5), so
// 7,173 syn rows across 1,731 cells (19.4%) carry a `path:` id. labels.json holds
// cells only (all 8,914 ids are `cell:`-prefixed), so labelById can NEVER resolve
// one — the raw id would leak into a reading surface whose whole premise is prose,
// and the drawer one click away would then name the same endpoint differently.
// The synchronous twin of labelOf(); cellItem() and renderSupercellPanel() branch
// the same way.
function synLabel(id) {
  if (isPathId(id)) return ((tree.sc || {})[id] || {}).label || id.slice(5);
  const r = labelById && labelById.get(id);
  return (r && r.label) || id;
}
async function labelOf(id) {
  if (isPathId(id)) return ((tree.sc || {})[id] || {}).label || id;
  const r = labelById && labelById.get(id);
  if (r) return r.label;
  const e = await getEntry(id);
  return (e && e.cell.label) || id;
}
async function showSynapsePanel(a, b, syn) {
  lastPanelId = "__syn__";
  // the flat map ships [i, j, w] only — fetch the kinds + traces on demand
  if (!syn || !syn.kinds || !Object.keys(syn.kinds).length) {
    const got = await synBetween(a, b);
    syn = got || {a, b, w: (syn && syn.w) || 0, kinds: {}, traces: []};
  }
  if (lastPanelId !== "__syn__") return;
  const [la, lb] = await Promise.all([labelOf(a), labelOf(b)]);
  const traces = syn.traces || [];
  const kinds = Object.entries(syn.kinds || {}).sort((x, y) => y[1] - x[1]);
  const dom = EDGE_STYLE[dominantKind(syn.kinds)] || {color: SYN_COLOR, label: "synapse"};
  let html = `<h2 style="font-size:1.05rem">Synapse</h2>
    <div class="sub"><span style="color:${dom.color}">●</span> weight <b>${syn.w}</b> —
      every weak bond between these two atoms, collapsed into one edge. A synapse is
      <b>undirected</b>: direction lives on each trace below.</div>
    <div class="chips">
      <span class="chip"><a data-nav="${esc(a)}">${esc(la)}</a></span>
      <span class="chip dirarrow">↔</span>
      <span class="chip"><a data-nav="${esc(b)}">${esc(lb)}</a></span>
    </div>`;
  if (kinds.length)
    html += `<section class="kind"><h3>Bonds <span class="cnt">(${kinds.length} kind${
      kinds.length === 1 ? "" : "s"})</span></h3><div class="chips">` +
      kinds.map(([k, v]) => {
        const st = EDGE_STYLE[k] || {color: SYN_COLOR, label: k};
        return `<span class="chip" title="${esc(st.label)}"><span style="color:${
          st.color}">●</span> ${esc(k)} <b>×${v}</b></span>`;
      }).join("") + `</div></section>`;
  if (traces.length) {
    html += `<section class="kind"><h3>Traces <span class="cnt">(${traces.length}${
      syn.tt && syn.tt > traces.length ? ` of ${syn.tt}` : ""})</span></h3>`;
    for (const t of traces) {
      const st = EDGE_STYLE[t.kind] || {color: SYN_COLOR, label: t.kind};
      const prov = t.prov !== undefined ? manifest.prov[t.prov] : null;
      const pc = provClass(t.kind, prov, t.evidence);
      const ctx = {fromId: t.src, fromLabel: null, toId: t.dst, toLabel: null};
      html += `<div class="edge open"><div class="row">
        <span style="color:${st.color}">●</span>
        <span class="nav" data-nav="${esc(t.src)}" data-lbl="${esc(t.src)}"
          style="color:#1a4b8f;cursor:pointer">${esc(t.src)}</span>
        <span class="dirarrow">→</span>
        <span class="nav" data-nav="${esc(t.dst)}" data-lbl="${esc(t.dst)}"
          style="color:#1a4b8f;cursor:pointer">${esc(t.dst)}</span>
        <span class="mk">${esc(st.label)}</span>
        <span class="prov ${pc}" title="${esc(PROV_TITLE[pc])}">${pc}</span></div>
        <div class="drawer" style="display:block">${
          evidenceProse(t.kind, t.evidence, prov, t.dst, ctx)}</div></div>`;
    }
    if (syn.tt && syn.tt > traces.length)
      html += `<div class="more">${syn.tt - traces.length} further trace${
        syn.tt - traces.length === 1 ? " is" : "s are"} not shipped in this shard (cap:
        ${manifest._meta.caps.traces_per_synapse}/synapse) — the full set is at
        <code>/api/brain/*</code> or <code>brain/query.py</code>.</div>`;
    html += `</section>`;
  } else {
    html += `<section class="kind"><h3>Traces</h3>
      <p class="note">This synapse's traces aren't shipped in the static view${
      isPathId(a) || isPathId(b)
        ? " — area-level synapses (a field concept's bonds, hanging off the folder that holds it) carry their weight and kinds here only"
        : ""}. The full set is at <code>/api/brain/*</code> or <code>brain/query.py</code>.</p></section>`;
  }
  html += `<p class="note">Every line on the canvas is a stored synapse. Click either
    atom to inspect it.</p>`;
  panelEl.innerHTML = html;
  wirePanel();
}
// ---- panel dispatch --------------------------------------------------------
let lastPanelId = null;
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
// shared wiring for every freshly-rendered panel
function wirePanel() {
  panelEl.querySelectorAll("[data-nav]").forEach(a =>
    a.addEventListener("click", ev => {
      if (ev.target.closest("a[href]") && ev.target !== a) return;
      navigate(a.dataset.nav);
    }));
  bindRawToggles();
  enrichEvidence(panelEl);
  // the Wikipedia lead is an on-demand REST fetch — never paid on card render
  panelEl.querySelectorAll("details[data-wplead]").forEach(d =>
    d.addEventListener("toggle", async () => {
      const box = d.querySelector(".wplead");
      if (!d.open || !box || box.dataset.loaded) return;
      box.dataset.loaded = "1";
      const slug = d.dataset.wplead;
      const lead = await wikipediaLead(slug);
      if (!panelEl.contains(box)) return;
      box.innerHTML = lead
        ? `<div class="snip">${esc(lead)}</div><div class="srclic">Wikipedia (CC-BY-SA-4.0) ·
           <a href="https://en.wikipedia.org/wiki/${esc(slug)}" rel="noopener" target="_blank">read the article ↗</a></div>`
        : `<p class="note">no lead available.</p>`;
    }));
  // collapsed drawers load their snippets/labels on expand
  panelEl.querySelectorAll(".edge .row").forEach(r =>
    r.addEventListener("click", ev => {
      if (ev.target.closest("a") || ev.target.closest("[data-nav]")) return;
      if (r.dataset.syn !== undefined) return;   // synapse rows open the drawer panel
      const edge = r.parentElement;
      edge.classList.toggle("open");
      if (edge.classList.contains("open")) enrichEvidence(edge);
    }));
}
async function renderPanel(id) {
  lastPanelId = id;
  if (id === ROOTS_ID) return rootsPanel();
  if (id === UNPLACED_ID) return unplacedPanel();
  if (isPathId(id)) return renderSupercellPanel(id);
  const resolved = isCellId(id) ? id : await resolveId(id);
  if (lastPanelId !== id) return;
  if (resolved && resolved !== id) return renderPanel(resolved);
  const e = resolved ? await getEntry(resolved) : null;
  if (lastPanelId !== id) return;
  if (!e) {
    // not in the shards: an unminted external page, or a community-added
    // Wikidata concept that never entered a build
    if (id.startsWith("xref:")) return extFallbackPanel(id);
    if (/^Q\d+$/.test(id)) return renderCommunityNodePanel(id);
    panelEl.innerHTML = `<p class="note">Unknown id: ${esc(id)}. Every organ id
      (a QID, a <code>decl:</code> name, an <code>xref:</code> page, an article slug)
      resolves through <code>aliases.json</code> to the atom that owns it.</p>`;
    return;
  }
  return renderCellPanel(resolved, e);
}
function rootsPanel() {
  const rows = tree.roots.filter(p => tree.count(p) > 0)
    .map(p => [tree.count(p), p]).sort((a, b) => b[0] - a[0]);
  let html = `<h2>The Brain</h2>
    <div class="sub">${(manifest._meta.counts.cells || 0).toLocaleString()} cells ·
      ${(manifest._meta.counts.organs || 0).toLocaleString()} organs ·
      ${(manifest._meta.counts.synapses || 0).toLocaleString()} synapses ·
      data ${esc(manifest._meta.generated_at.slice(0, 10))}</div>
    <p class="note">A <b>cell</b> is an atom: one mathematical object, holding every
    particle that denotes it — its Wikidata concept(s), the Lean declaration(s) that
    formalize it, its pages in nLab / LMFDB / Stacks / MathWorld / …, its WikiLean
    article, its arXiv statements. Atoms nest inside the Mathlib folders their code
    lives in; every weak bond between two atoms collapses into one <b>synapse</b> that
    keeps its traces.</p>
    <section class="kind"><h3>Libraries <span class="cnt">(${rows.length} with cells)</span></h3>
    <div class="chips">`;
  for (const [n, p] of rows)
    html += `<span class="chip"><a data-nav="${esc(p)}">${esc(p.slice(5))}</a>
      <small>${n.toLocaleString()}</small></span>`;
  html += `</div><p class="note">${
    tree.roots.length - rows.length} further library roots hold no cells yet.</p></section>`;
  if (tree.unplaced.length)
    html += `<section class="kind"><h3>No formal home <span class="cnt">(${
      tree.unplaced.length.toLocaleString()})</span></h3>
      <p class="note">Atoms with no Lean declaration have no module to nest in — nothing
      formalizes them yet. <a data-nav="${UNPLACED_ID}">Browse them</a>, or find them in the
      Explorer, which places every atom.</p></section>`;
  panelEl.innerHTML = html;
  wirePanel();
}
function unplacedPanel() {
  panelEl.innerHTML = `<h2>No formal home</h2>
    <div class="sub">${tree.unplaced.length.toLocaleString()} cells</div>
    <p class="note">These atoms hold no Lean declaration, so they have no module to nest
    inside — the containment tree can't place them. They are real atoms with real
    synapses: the Explorer places every one of them, and search finds them by any of
    their organs' names.</p>`;
  wirePanel();
}
async function renderSupercellPanel(p) {
  await ensureTree();
  const sc = tree.sc[p];
  if (lastPanelId !== p) return;
  if (!sc) { panelEl.innerHTML = `<p class="note">Unknown area: ${esc(p)}</p>`; return; }
  const chain = pathChain(p);
  let html = `<div class="crumb">` + chain.map((b, i) =>
    i === chain.length - 1 ? esc(b.label) : `<a data-nav="${esc(b.id)}">${esc(b.label)}</a>`)
    .join(" / ") + `</div>`;
  html += `<h2>${esc(sc.label || p)}</h2>
    <div class="sub">supercell · <code>${esc(p)}</code> · ${tree.count(p).toLocaleString()}
      cells in the subtree${(sc.cells || []).length ? ` · ${sc.cells.length} here` : ""}</div>`;
  // rule-5 organs: field-of-study concepts and area pages belong to the FOLDER,
  // never to a cell — "Linear algebra" is this module, not the Module atom
  if ((sc.organs || []).length) {
    html += `<section class="kind"><h3 title="a field-of-study concept or an area-level page belongs to the module, never to a cell (SCHEMA rules 4 &amp; 5)">This area <em>is</em>
      <span class="cnt">(${sc.organs.length})</span></h3>`;
    for (const o of sc.organs) html += organHtml(o, null);
    html += `</section>`;
  }
  if ((sc.children || []).length) {
    html += `<section class="kind"><h3>Areas <span class="cnt">(${sc.children.length})</span></h3><div class="chips">`;
    for (const ch of sc.children.slice(0, 60))
      html += `<span class="chip"><a data-nav="${esc(ch)}">${esc((tree.sc[ch] || {}).label || ch)}</a>
        <small>${tree.count(ch).toLocaleString()}</small></span>`;
    if (sc.children.length > 60) html += `<span class="chip">… +${sc.children.length - 60} more</span>`;
    html += `</div></section>`;
  }
  if ((sc.cells || []).length) {
    html += `<section class="kind"><h3>Cells here <span class="cnt">(${sc.cells.length})</span></h3><div class="chips">`;
    for (const cid of sc.cells.slice(0, 80))
      html += `<span class="chip"><a data-nav="${esc(cid)}">${
        esc(((labelById && labelById.get(cid)) || {}).label || cid)}</a></span>`;
    if (sc.cells.length > 80) html += `<span class="chip">… +${sc.cells.length - 80} more</span>`;
    html += `</div></section>`;
  }
  const kinds = activeKinds(), provs = activeProv();
  const syn = (sc.syn || []).filter(s => synVisible({kinds: s.kinds, traces: s.traces}, kinds, provs));
  if (syn.length) {
    html += `<section class="kind"><h3>Synapses <span class="cnt">(${syn.length}${
      (sc.counts && sc.counts.syn && sc.counts.syn > syn.length) ? ` of ${sc.counts.syn.toLocaleString()}` : ""})</span></h3>`;
    syn.slice(0, 30).forEach((s, i) => {
      const st = EDGE_STYLE[dominantKind(s.kinds)] || {color: SYN_COLOR};
      html += `<div class="edge"><div class="row" data-scsyn="${i}">
        <span style="color:${st.color}">●</span><span>${esc(synLabel(s.id))}</span>
        <span class="mk">${esc(Object.keys(s.kinds || {}).join(", "))}</span>
        <span class="prov">weight ${s.w}</span></div></div>`;
    });
    html += `</section>`;
  }
  html += `<div id="community-slot"></div>`;
  panelEl.innerHTML = html;
  wirePanel();
  panelEl.querySelectorAll("[data-scsyn]").forEach(r =>
    r.addEventListener("click", ev => {
      if (ev.target.closest("a")) return;
      const s = syn[Number(r.dataset.scsyn)];
      showSynapsePanel(p, s.id, {a: p, b: s.id, w: s.w, kinds: s.kinds, traces: s.traces || [], tt: s.tt});
    }));
  renderCommunity(p, p);
}
// an external page id not in aliases (an unminted frontier page): a minimal
// deep-link panel instead of "Unknown id"
function extFallbackPanel(id) {
  lastPanelId = id;
  const db = extDbOf(id), val = extValueOf(id);
  const url = organUrl(id);
  panelEl.innerHTML = `
    <h2 style="font-size:1.1rem">${esc(val || id)}</h2>
    <div class="sub">external page ·
      <span class="badge" style="border-color:${esc(DB_COLOR[db] || "#c8bfa8")}">${
      esc(XREF_NAME[db] || db || "external database")}</span></div>
    <p class="note">No atom claims this page in the current build — external pages are
    organs inside cells now, and only anchored ones ship. ${
      url ? `<a href="${esc(url)}" rel="noopener" target="_blank">Open it at the source ↗</a>` : "No deep link available."}</p>`;
}

// ---- the transparency legend: /map's Sources view, rendered in the panel ----
let sourcesData = null;
async function showSourcesPanel() {
  lastPanelId = "__sources__";
  if (!sourcesData) {
    const r = await fetch(SOURCES_URL);
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
        <span class="prov">${esc(s.target_license || "—")}</span></div>
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
// Over labels + `aka` (EVERY organ label the atom holds), so "Vector space"
// surfaces the Module atom — the whole point of the cell model.
let searchIndex = null;
async function ensureSearchIndex() {
  if (searchIndex) return searchIndex;
  await ensureTree();
  const rows = (labels || []).map(r => ({
    id: r.id, label: r.label, type: "cell", aka: r.aka || null,
    hay: [r.label, ...(r.aka || [])].map(s => s.toLowerCase()),
  }));
  for (const [p, sc] of Object.entries(tree.sc || {})) {
    const n = tree.count(p);
    if (!n) continue;   // an empty folder is not a destination
    rows.push({id: p, label: sc.label || p, type: "area", n,
               hay: [(sc.label || "").toLowerCase(), p.slice(5).toLowerCase()]});
  }
  searchIndex = rows;
  return rows;
}
let searchT = null;
$("#q").addEventListener("input", () => {
  clearTimeout(searchT);
  searchT = setTimeout(async () => {
    const q = $("#q").value.trim().toLowerCase();
    const box = $("#hits");
    if (q.length < 2) { box.style.display = "none"; return; }
    const L = await ensureSearchIndex();
    const starts = [], contains = [];
    for (const r of L) {
      if (r.hay.some(h => h.startsWith(q))) starts.push(r);
      else if (r.hay.some(h => h.includes(q))) contains.push(r);
      if (starts.length >= 20) break;
    }
    const hits = [...starts, ...contains].slice(0, 20);
    box.innerHTML = hits.map(r => {
      // say WHY a hit matched when it matched on an organ name, not the label
      const via = r.aka && !r.label.toLowerCase().includes(q)
        ? r.aka.find(a => a.toLowerCase().includes(q)) : null;
      return `<div class="hit" data-id="${esc(r.id)}"><span class="t">${esc(r.type)}</span> ${
        esc(r.label)}${via ? ` <span class="aka">— its organ “${esc(via)}”</span>` : ""}${
        r.n ? ` <small style="color:#8c959f">${r.n.toLocaleString()}</small>` : ""}</div>`;
    }).join("") || `<div class="hit"><span class="t">no hits</span> try /decl/&lt;name&gt; for declarations</div>`;
    box.style.display = "block";
    box.querySelectorAll("[data-id]").forEach(h =>
      h.addEventListener("click", () => { box.style.display = "none"; $("#q").value = ""; navigate(h.dataset.id); }));
  }, 150);
});
document.addEventListener("click", ev => {
  if (!ev.target.closest("#search")) $("#hits").style.display = "none";
});
// ============================ the Explorer ===================================
// The COMPLETE flat cell graph, drawn at its BUILD-TIME positions.
//
// The client runs NO physics (SCHEMA "Layout is BUILD-TIME"). brain/layout.py
// solves the layout once, deterministically, with SHORT-RANGE repulsion and
// parks synapse-less cells near their supercell's centre of mass. Re-simulating
// here would resurrect exactly what that fixed: textbook long-range repulsion
// pushes weakly-attached cells out to r = √(n·k²/g) — measured 84,200 vs 1,985
// for the core — so fit-to-content zooms out ~42× and the graph renders as a ring
// around a clump. It also makes the map STABLE: the same shape every visit, so it
// can be learned.
//
// explorer.json ships edges as index triples [i, j, w] into `nodes` (ids average
// ~11 chars and repeat twice per edge, so objects cost ~4×) — which is what buys
// shipping all 76,083 synapses in 2.3 MB with no draw cap.
let xdata = null;
async function fetchExplorerData() {
  if (xdata) return xdata;
  const get = () => fetch(BASE + "explorer.json" + vq())
    .then(r => (r.ok ? r.json() : null)).catch(() => null);
  let j = await get();
  if (!j) {   // stale manifest → one re-sync + retry, same as getEntry
    try { await fetchManifest(); } catch (e) { return null; }
    j = await get();
  }
  xdata = j;
  return j;
}
// 76k <path> elements is not a thing a browser survives, so the synapses batch
// into one path per weight tier (a single `d` of M…L subpaths). Nothing is
// dropped — the click target is a nearest-segment hit test below, so every drawn
// synapse stays inspectable.
const SYN_TIERS = [
  {min: 1, max: 1, w: 0.5, op: 0.10},
  {min: 2, max: 3, w: 0.7, op: 0.18},
  {min: 4, max: 7, w: 1.1, op: 0.30},
  {min: 8, max: Infinity, w: 1.8, op: 0.50},
];
let xEdges = [];   // [{a, b, w, ax, ay, bx, by}] for the hit test
function drawExplorerEdges() {
  gEdges.selectAll("*").remove();
  for (const t of SYN_TIERS) {
    let d = "";
    for (const e of xEdges) {
      if (e.w < t.min || e.w > t.max) continue;
      d += `M${e.ax.toFixed(1)},${e.ay.toFixed(1)}L${e.bx.toFixed(1)},${e.by.toFixed(1)}`;
    }
    if (!d) continue;
    gEdges.append("path").attr("class", "synbatch").attr("d", d)
      .attr("stroke", SYN_COLOR).attr("stroke-opacity", t.op)
      // base width in PIXELS; applyExplorerScale divides by k so it renders at t.w
      .attr("data-w", t.w).attr("stroke-width", t.w);
  }
}
// distance from p to segment ab, squared
function segDist2(px, py, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay;
  const l2 = dx * dx + dy * dy;
  let t = l2 ? ((px - ax) * dx + (py - ay) * dy) / l2 : 0;
  t = t < 0 ? 0 : t > 1 ? 1 : t;
  const qx = ax + t * dx, qy = ay + t * dy;
  return (px - qx) * (px - qx) + (py - qy) * (py - qy);
}
function explorerClick(ev) {
  if (!xEdges.length) return;
  const [px, py] = d3.pointer(ev, gViewport.node());
  const k = d3.zoomTransform(svg.node()).k || 1;
  const tol = 7 / k;                    // a constant on-screen grab radius
  let best = null, bd = tol * tol;
  for (const e of xEdges) {
    const d2 = segDist2(px, py, e.ax, e.ay, e.bx, e.by);
    if (d2 < bd) { bd = d2; best = e; }
  }
  if (best) showSynapsePanel(best.a, best.b, {a: best.a, b: best.b, w: best.w});
}
// the explorer scopes by AREA: an area id scopes to its subtree, a cell id
// scopes to that cell's home area (and is selected), anything else = everything
async function explorerFocusFor(rawId) {
  const id = await resolveId(rawId);
  if (isPathId(id)) return id;
  if (isCellId(id)) {
    const e = await getEntry(id);
    const home = e ? homeOf(e) : null;
    return isPathId(home) ? home : ROOTS_ID;
  }
  return ROOTS_ID;
}
// The camera's extent: where does the MASS of the graph end? Takes the sorted
// radii about the median centre and returns the radius at which the cell density
// collapses — the edge of the core, not the edge of the data.
//
// Bins by a scale-free width (p50/4, so it works at any scope's size), finds the
// densest annulus, then walks outward and cuts at the first bin holding less than
// FIT_DROP of the peak. Guard rails on both sides: never frame less than FIT_FLOOR
// of the cells (a scope with no gap must not get clipped to its mode), and never
// chase a lone outlier past p99.
//
// Measured on the shipped explorer.json against the old FIT_PCTL=0.97, as
// (p90 / rFit) — "how much of the frame radius the readable 90% fills", where 1.0
// is a full stage:
//   all libraries   0.56 -> 1.01   (rFit 2,731 -> 1,514; 89.9% framed; 1.80x zoom)
//   NumberTheory    0.41 -> 0.98   (2,915 -> 1,209; 91.2%)      Combinatorics 0.50 -> 1.16
//   SetTheory       0.51 -> 1.23   (2,005 ->   833; 85.2%)      Topology      0.63 -> 0.92
//   LinearAlgebra   0.82 -> 0.92   (1,410 -> 1,262; 94.1%)      Data          0.88 -> 0.86
// Every scope frames >=85% of its cells, and the scopes that had no band (Data,
// Analysis) are left essentially where they were — which is the point: the rule
// reacts to the data instead of to a constant.
const FIT_DROP = 0.20;    // a bin below this share of the peak = the core has ended
const FIT_FLOOR = 0.85;   // always frame at least this fraction of the cells
const FIT_CAP = 0.99;     // never let a single outlier set the extent
function fitRadius(rad) {
  const n = rad.length;
  const at = q => rad[Math.min(n - 1, Math.floor(n * q))];
  const w = at(0.5) / 4;
  if (!(w > 0) || n < 8) return at(FIT_CAP);          // degenerate/tiny scope
  const nb = Math.floor(rad[n - 1] / w) + 1;
  const cnt = new Array(nb).fill(0);
  for (const r of rad) cnt[Math.min(nb - 1, Math.floor(r / w))]++;
  let peak = 0, pi = 0;
  for (let i = 0; i < nb; i++) if (cnt[i] > peak) { peak = cnt[i]; pi = i; }
  let cut = nb;
  for (let i = pi + 1; i < nb; i++) if (cnt[i] < FIT_DROP * peak) { cut = i; break; }
  return Math.min(Math.max(cut * w, at(FIT_FLOOR)), at(FIT_CAP));
}
async function renderExplorer(anim) {
  const seq = ++renderSeq;
  await ensureTree();
  const j = await fetchExplorerData();
  if (seq !== renderSeq) return;
  if (!j || !(j.nodes || []).length) {
    setExplorer(false);
    setHash(focusId || "");   // drop the stale &view=explorer
    statusEl.textContent = "explorer data not built yet (cells/explorer.json)";
    return renderFocus(false);
  }
  resetZoom();
  selectedId = null;
  const nodes = j.nodes;
  const totalN = nodes.length;
  // ---- scope: the explorer flattens the CURRENT focus subtree. A cell carries
  // `p` (its containment path) iff it has a decl organ; a concept-only atom has
  // no tree home, so it joins by induction — kept iff it synapses with an
  // in-scope atom. At the top level everything is in scope.
  const scope = isPathId(focusId) ? focusId : null;
  const inSub = pp => pp === scope || (pp || "").startsWith(scope + "/");
  const maskOk = i => ((nodes[i].f || 0) & filterMask) !== 0;
  const keep = new Uint8Array(totalN);
  if (!scope) {
    for (let i = 0; i < totalN; i++) keep[i] = (!filterMask || maskOk(i)) ? 1 : 0;
  } else {
    const core = new Uint8Array(totalN);
    for (let i = 0; i < totalN; i++) if (nodes[i].p && inSub(nodes[i].p)) core[i] = 1;
    const touch = core.slice();
    for (const [i, k2] of j.edges) {   // one ripple: homeless atoms hanging off the core
      if (core[i]) touch[k2] = 1;
      if (core[k2]) touch[i] = 1;
    }
    for (let i = 0; i < totalN; i++)
      keep[i] = (touch[i] && (!filterMask || maskOk(i))) ? 1 : 0;
  }
  const leaves = [];
  const idxOf = new Uint32Array(totalN);
  const deg = new Uint32Array(totalN);
  for (const [i, k2] of j.edges) if (keep[i] && keep[k2]) { deg[i]++; deg[k2]++; }
  for (let i = 0; i < totalN; i++) {
    if (!keep[i]) continue;
    const n = nodes[i];
    idxOf[i] = leaves.length;
    leaves.push({data: {id: n.id, type: "cell", label: n.label, f: n.f || 0, p: n.p || null},
                 x: n.xy[0], y: n.xy[1],           // BUILD-TIME layout, verbatim
                 r: 2.2 + Math.min(4.5, Math.sqrt(deg[i]) * 0.55)});
  }
  xEdges = [];
  for (const [i, k2, w] of j.edges) {
    if (!keep[i] || !keep[k2]) continue;
    const A = leaves[idxOf[i]], B = leaves[idxOf[k2]];
    xEdges.push({a: A.data.id, b: B.data.id, w, ax: A.x, ay: A.y, bx: B.x, by: B.y});
  }
  layout = {items: new Map(leaves.map(l => [l.data.id, l])), leaves, explorer: true};
  edgeStore = [];
  gOverlay.selectAll("*").remove();
  gBubbles.selectAll("circle.preview").remove();
  drawNodes();
  drawExplorerEdges();
  drawExplorerLabels();
  // Fit the camera to where the cells actually ARE, not to the bounding box.
  //
  // The build-time layout takes whatever area it needs, so a scope's extent is set
  // by its few most distant stragglers. Fitting to min/max hands the zoom to them:
  // measured on Mathlib/LinearAlgebra, 68 of 1,373 cells (5%) stretched the extent
  // to r=567 while the other 95% sat inside r=218 — so the part worth reading drew
  // 2.6x smaller than it should, which is a dot-in-a-void by another route. Same
  // failure as the layout halo (an extreme minority dictating the view), just moved
  // into the camera.
  //
  // A FIXED percentile does not fix that — it only moves the threshold, and it is
  // still a minority-sensitive statistic whenever the minority is BIGGER than
  // 1-FIT_PCTL. FIT_PCTL=0.97 was tuned on Mathlib/LinearAlgebra, where the
  // stragglers are 5% — but at "all libraries" 7.7% of cells sit in the layout's
  // tidy outer band (SCHEMA: synapse-less cells with no supercell are parked
  // there), which is more than the 3% the constant discards. Measured on the
  // shipped explorer.json: p90=1,524 but p97=2,731 — rFit lands INSIDE the band and
  // the band sets the zoom anyway, so the 90% worth reading filled 1,524/2,731 =
  // 56% of the frame radius and drew 1.8x smaller than it needed to.
  //
  // So detect the band instead of assuming its size. The radius histogram has a
  // real gap — 89.8% of cells at r<=1,500, then a near-empty shell, then the band —
  // so walk outward from the densest annulus and cut where the density COLLAPSES.
  // That adapts to each scope rather than hard-coding one scope's minority.
  const W = stageEl.clientWidth || 800, H = stageEl.clientHeight || 600;
  if (leaves.length) {
    const mid = a => a.length ? a.slice().sort((p, q) => p - q)[a.length >> 1] : 0;
    const cx = mid(leaves.map(l => l.x)), cy = mid(leaves.map(l => l.y));
    const rad = leaves.map(l => Math.hypot(l.x - cx, l.y - cy)).sort((a, b) => a - b);
    const rFit = Math.max(fitRadius(rad), 1);
    const pad = leaves[0] ? leaves[0].r * 2 : 0;
    const bw = (rFit + pad) * 2, bh = bw;
    const k = Math.max(0.02, Math.min(2, Math.min((W - 70) / bw, (H - 70) / bh)));
    const t = d3.zoomIdentity.translate(W / 2 - k * cx, H / 2 - k * cy).scale(k);
    svg.call(zoomBehav.transform, t);
    applyExplorerScale(k);
  }
  const scopeLabel = scope ? scope.slice(5) : "all libraries";
  updateFilterStat({active: !!filterMask, shown: leaves.length, total: totalN,
    text: filterMask ? `${leaves.length.toLocaleString()} of ${totalN.toLocaleString()} cells match` : ""});
  crumbEl.innerHTML = `<a data-nav="${ROOTS_ID}">all libraries</a>
    <span class="sep">/</span> <b>${esc(scopeLabel)} · explorer</b>`;
  crumbEl.querySelectorAll("[data-nav]").forEach(a =>
    a.addEventListener("click", () => {
      setExplorer(false); focusId = ROOTS_ID; selectedId = null;
      setHash(""); renderFocus(true); renderPanel(ROOTS_ID);
    }));
  statusEl.textContent = `explorer: ${leaves.length.toLocaleString()} cells · ${
    xEdges.length.toLocaleString()} synapses · ${scopeLabel} · build-time layout`;
  const el = $("#structstat");
  if (el) el.textContent = "no client simulation — positions are solved at build time";
  if (anim) fadeIn();
}
// labels capped by zoom: only the biggest atoms are labelled zoomed-out; zooming
// in reveals more (up to 250 text elements at any graph size)
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
  // Label budget scales with zoom^2 (i.e. with visible AREA per cell), because the
  // labels now render at a constant 11px: once they were legible, showing 250 of
  // them at the resting zoom piled them into an unreadable white mass in the dense
  // core. Fewer at rest, more as you zoom in — the map stays readable at every k.
  const lim = Math.max(12, Math.min(250, Math.round(600 * k * k)));
  gLabels.selectAll("text.xlab").attr("display", function () {
    return Number(this.dataset.rank) < lim ? null : "none";
  });
  // The labels live inside gViewport, so `font-size` is in USER units and renders
  // at font-size*k. To hold a constant on-screen size the divisor must therefore be
  // the size you want IN PIXELS: 11/k renders at 11px, at any k.
  //
  // This read `1.1 / k`, which renders at 1.1 PIXELS — identically, at every k below
  // the clamp. The whole flat map drew as sub-pixel dust at its own fitted zoom
  // (k≈0.13), which is a large part of why the explorer read as unreadable no matter
  // how the layout was tuned. Same trap as the dots below: never size in layout units.
  gLabels.selectAll("text.xlab").attr("font-size", LABEL_PX / (k || 1));
}
zoomBehav.on("zoom.xplabels", ev => {
  // labels AND dots AND strokes: every one of them is sized in screen space
  if (layout && layout.explorer) applyExplorerScale(ev.transform.k);
});
// ============================ toolbar + boot =================================
document.querySelectorAll(".toolbar input").forEach(el =>
  el.addEventListener("change", () => {
    if (explorerOn) return;   // the flat map carries no kind/provenance data
    if (layout && layout.ego) { renderFocus(false); return; }
    renderEdges();
    drawSelRing();
    if (selectedId) renderPanel(selectedId);
    else if (focusId) renderPanel(focusId);
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
    setHash(focusId === ROOTS_ID ? "" : focusId || "");
    if (explorerOn) renderExplorer(false);
    else renderFocus(false);
  }));
$("#explorerbtn").addEventListener("click", () => {
  setExplorer(!explorerOn);
  setHash(focusId === ROOTS_ID ? "" : focusId || "");
  if (explorerOn) renderExplorer(true);
  else renderFocus(true);
});

window.addEventListener("hashchange", async () => {
  const h = parseHash();
  filterMask = h.f;
  syncChips();
  if (h.view === "explorer") {
    setExplorer(true);
    // the explorer scopes by AREA, so the id segment picks the subtree (a cell
    // id selects instead) — without this the scope silently stays where it was
    focusId = await explorerFocusFor(h.id);
    await renderExplorer(true);
    if (h.id && explorerOn) {
      const sel = await resolveId(h.id);
      if (isCellId(sel)) { selectedId = sel; renderPanel(sel); drawSelRing(); }
    }
    return;
  }
  if (explorerOn) setExplorer(false);
  if (h.id) navigate(h.id);
  else { focusId = ROOTS_ID; renderFocus(false); renderPanel(ROOTS_ID); }
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
// Live, user/API-submitted edges (docs/BRAIN-EDITS-ROADMAP.md). The overlay is
// keyed by the v2 node ids the API stores, and an atom's ANCHOR is exactly one
// of those — so a cell asks about its anchor and any target navigates back
// through aliases.json. All fetches degrade silently when the API is absent
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
// full-text autocomplete over ALL of Wikidata (not just the ingested atoms)
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
// community-added Wikidata node (in nodeLabels) links OUT to Wikidata (no atom
// owns it, so in-brain nav would dead-end); anything else navigates in-brain,
// where aliases resolves it to its atom.
function communityTargetHtml(other, nodeLabels) {
  nodeLabels = nodeLabels || {};
  if (other.startsWith("xref:")) {
    const p = other.split(":");
    return `<a data-nav="${esc(other)}">${esc((XREF_NAME[p[1]] || p[1]) + ": " + p.slice(2).join(":"))}</a>`;
  }
  if (/^Q\d+$/.test(other) && nodeLabels[other]) {
    return `<a href="https://www.wikidata.org/wiki/${esc(other)}" target="_blank" rel="noopener"
      title="community-added Wikidata concept">${esc(nodeLabels[other])} <span class="lit-ref">${esc(other)}</span></a>`;
  }
  return `<a data-nav="${esc(other)}" data-lbl="${esc(other)}">${esc(other)}</a>`;
}
// minimal panel for a community-added Wikidata concept (no atom claims it)
async function renderCommunityNodePanel(id) {
  const {self} = await fetchCommunityEdges(id);
  if (lastPanelId !== id) return;
  if (!self) { panelEl.innerHTML = `<p class="note">Unknown id: ${esc(id)}</p>`; return; }
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
  renderCommunity(id, id);
}
// `apiId` is the v2 node id the API knows (an atom's anchor); `panelId` is what
// the panel is currently showing, so a stale fetch can't paint over a newer card
async function renderCommunity(apiId, panelId) {
  const slot = $("#community-slot");
  if (!slot) return;
  const {edges, shared, nodeLabels} = await fetchCommunityEdges(apiId);
  if (lastPanelId !== panelId || !$("#community-slot")) return;   // panel moved on
  let html = `<section class="kind community"><h3>Community connections
    <span class="cnt">(${edges.length})</span></h3>`;
  for (const e of edges) {
    const out = e.src === apiId;
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
  // cross-pollination: atoms that share an external-database page with this one
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
    // ADD AN EDGE (a connection between this atom and another)
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
  wireCommunity(apiId, panelId);
}
function wireCommunity(apiId, panelId) {
  const slot = $("#community-slot");
  if (!slot) return;
  slot.querySelectorAll("[data-nav]").forEach(a =>
    a.addEventListener("click", () => navigate(a.dataset.nav)));
  enrichEvidence(slot);   // resolve any bare organ ids the API handed back
  slot.querySelectorAll("[data-del]").forEach(b => b.addEventListener("click", async () => {
    if (!confirm("Delete this connection? It stays as a gravestone that records who removed it.")) return;
    await deleteCommunityEdge(b.dataset.del);
    if (lastPanelId === panelId) renderCommunity(apiId, panelId);
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
  let searchT2;
  if (tin) tin.addEventListener("input", () => {
    clearTimeout(searchT2);
    tid.value = "";
    const q = tin.value.trim();
    if (q.length < 2) { hits.innerHTML = ""; return; }
    searchT2 = setTimeout(async () => {
      // brain nodes (decls, areas, ingested concepts) AND all of Wikidata
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
    const res = await submitCommunityEdge({src: apiId, dst, kind, evidence: {note}});
    submit.disabled = false;
    if (res.ok) { if (lastPanelId === panelId) renderCommunity(apiId, panelId); }
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
    if (ok) { msg.innerHTML = `added ✓ — now searchable &amp; linkable`; cnId.value = ""; cnIn.value = ""; }
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
      ") — run brain/build_cell_shards.py + build-public";
    return;
  }
  const c = manifest._meta.counts || {};
  statusEl.textContent = `${(c.cells || 0).toLocaleString()} cells · ` +
    `${(c.organs || 0).toLocaleString()} organs · ` +
    `${(c.synapses || 0).toLocaleString()} synapses · ` +
    `data ${manifest._meta.generated_at.slice(0, 10)}`;
  await ensureTree();
  const h = parseHash();
  filterMask = h.f;
  syncChips();
  if (h.view === "explorer") {
    setExplorer(true);
    focusId = await explorerFocusFor(h.id);
    await renderExplorer(false);
    if (h.id && explorerOn) {
      const sel = await resolveId(h.id);
      if (isCellId(sel)) { selectedId = sel; renderPanel(sel); drawSelRing(); }
    }
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
