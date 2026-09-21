#!/usr/bin/env python3
"""Generate /brain — the cell map over the BRAIN v3 dataset.

One zoomable canvas, zero baked-in data: at startup the page selects one
immutable release through /assets/brain/current.json, then everything is fetched
on demand from that release's prefix shards (manifest.json → one shard fetch per
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
  · Frontier — ONE view (#__frontier__): every homeless cell on a polar canvas
               whose angular sectors are the frontier areas and whose RADIUS is
               the build-time bond-weighted formal proximity (frontier rows'
               `prox`, PROXIMITY CONTRACT in brain/SCHEMA.md) — a deterministic
               client-computed layout, no simulation. The old hop-shell halo
               was DESTROYED 2026-08-04: "1 jump away" said nothing about
               whether the jump rode 200 bonds or one thread. Fail-soft: a
               build whose frontier rows ship no prox renders area bubbles.
  · Card     — the selected cell's organs grouped by kind, each with its bond,
               its provenance (a merged @[wikidata] tag never reads like an
               AI-queued candidate — C7) and its embedded payload: Lean code,
               the Wikidata description, licensed DB snippets, arXiv refs. ONE
               fetch renders the whole card.
  · Drawer   — a synapse's weight, its kind histogram and EVERY trace, in prose
               that names the actual database and page.
  · Search   — label + `aka` (every organ label) over labels.json, so searching
               "Vector space" surfaces the Module atom.

Run: python3 site/build_brain_page.py   (writes the release-neutral
site/out/brain.html; build-public stages it and an explicitly verified release)
"""
import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT_DIR = HERE / "out"

BRAIN_DIR = ROOT / "brain"
if str(BRAIN_DIR) not in sys.path:
    sys.path.insert(0, str(BRAIN_DIR))

from build_context import BuildContext  # noqa: E402
from stage_io import (  # noqa: E402
    assert_outputs_absent,
    ensure_private_directory,
    owned_directory,
    publish_files_no_replace,
    require_same_filesystem,
    write_bytes_exclusive,
)

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
  border-right:1px solid #262c3a; flex-wrap:wrap; row-gap:4px; }
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
#crumbpath { min-width:0; }
#crumbside { margin-left:auto; display:flex; gap:14px; align-items:center;
  white-space:nowrap; overflow:hidden; min-width:0; flex:0 1 auto; }
#crumbside .note { overflow:hidden; text-overflow:ellipsis; }
#release-id { color:#7f8a9c; font-size:.72rem; }
#crumbbar .sep { color:#556074; }
#crumbbar b { color:#e6e4de; }
.main { display:flex; flex:1 1 auto; min-height:0; }   /* fills the space the chrome leaves — no magic numbers */
#stage { flex:1 1 62%; position:relative; background:#0b0e14; overflow:hidden;
  cursor:grab; touch-action:none; }
#stage.grabbing { cursor:grabbing; }
/* Absolutely positioned against #stage (position:relative): under the
   <=900px media query #stage's height comes from min-height alone, where a
   child's height:100% resolves to auto and an SVG defaults to 150px — the
   wheel painted only its top band on phones. inset:0 tracks the real box. */
#stage svg { display:block; position:absolute; inset:0; width:100%; height:100%; }
/* the Explorer's canvas: sits over the (then-empty) SVG, never eats events —
   the SVG keeps the zoom/click surface and the canvas only paints */
#xcanvas { position:absolute; inset:0; pointer-events:none; display:none;
  transition:opacity .26s; }
#stage .hint { position:absolute; left:12px; bottom:10px; font-size:.72rem;
  pointer-events:none; color:#77808f;
  /* readable when SVG labels (frontier sector names at 6 o'clock) run beneath it */
  background:color-mix(in srgb, #0b0e14 82%, transparent);
  border-radius:6px; padding:2px 6px; max-width:72%; }
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
/* frontier: sector rim labels are CLICKABLE (the .blabel default is pointer-events:none) */
text.rimlab { pointer-events:auto; cursor:pointer; }
text.rimlab:hover { fill:#38bdf8; }
/* the frontier QUEUE: a ranked list filling the stage (#__frontier__ is the
   DEFAULT; the polar map moved to #__frontier__:map). Rows are WINDOWED — the
   DOM holds only the viewport slice; the scrollbar + the "N concepts" total
   prove the full set (no-silent-filter rule). */
#flist { position:absolute; inset:0; z-index:3; display:none; overflow-y:auto;
  background:#0b0e14; cursor:default; touch-action:pan-y; outline:none;
  overscroll-behavior:contain;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
#flhead { position:sticky; top:0; z-index:2; background:#0d1119;
  border-bottom:1px solid #1c2230; padding:8px 12px 0; }
#flctl { display:flex; gap:8px; align-items:center; flex-wrap:wrap;
  padding-right:118px; }   /* room for the list|map toggle overlay at top-right */
#fltotal { color:#e6e4de; font-size:.88rem; font-weight:600; }
#flq { padding:3px 9px; border:1px solid #33405c; border-radius:6px; background:#0b0e14;
  color:#e6e4de; font-size:.8rem; width:170px; }
#flq:focus { outline:2px solid #38bdf855; }
#flareas { display:flex; gap:6px; overflow-x:auto; padding:7px 0 6px;
  scrollbar-width:thin; }   /* the 46 area chips, size-desc, as a scrollable strip */
#flareas .fchip { flex:0 0 auto; }
.flcols, .flrow { display:grid; gap:8px; align-items:center;
  grid-template-columns:42px minmax(150px,1.5fr) 118px 96px 110px 132px minmax(100px,0.8fr); }
.flcols { padding:5px 0; color:#6b7488; font-size:.68rem; text-transform:uppercase;
  letter-spacing:.06em; }
#flbody { position:relative; margin:0 12px; }
.flrow { position:absolute; left:0; right:0; height:34px; cursor:pointer;
  border-bottom:1px solid #131826; font-size:.82rem; color:#c8cdd8; }
.flrow:hover { background:#141a28; }
.flrow.sel { background:#173753; }
.flrow.act { box-shadow:inset 0 0 0 1px #38bdf8; }
.flrank { color:#556074; font-size:.72rem; text-align:right; }
.fllabel { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:#e6e4de; }
.flareabtn { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:100%;
  text-align:left; }
.flprox { position:relative; height:8px; background:#1a2233; border-radius:4px;
  overflow:hidden; }
.flprox i { position:absolute; left:0; top:0; bottom:0; border-radius:4px;
  background:linear-gradient(90deg,#3b82f6,#38bdf8); }
.flev { display:flex; gap:3px; align-items:center; }
.flev i { width:9px; height:9px; border-radius:50%; display:inline-block; flex:0 0 auto; }
.flsuit { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:.7rem; }
.flsuit.candidate { color:#4ade80; }
.flsuit.deprioritized { color:#fbbf24; }
.flrow.deprioritized { color:#8d96a7; }
.flrow.deprioritized .fllabel { color:#aeb5c2; }
.flnear { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:.74rem; }
.flnear a { color:#7cb3ff; cursor:pointer; }
/* the list|map toggle at the frontier level (the areas|halo fchip precedent:
   a stage overlay of two .fchip buttons, shown only on the frontier views) */
#fviewtoggle { position:absolute; top:10px; right:16px; z-index:5; display:none; gap:6px; }
/* the Libraries control — ONE component, rendered by the root panel AND the frontier panel */
.libctl { display:flex; flex-direction:column; gap:4px; margin:6px 0; }
.librow { display:flex; gap:7px; align-items:center; font-size:.84rem; cursor:pointer;
  font-family:-apple-system,sans-serif; }
.librow small { color:#8a8272; margin-left:auto; }
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
  #panel { border-left:none; border-top:1px solid #262c3a; max-height:none; }
  .flcols, .flrow { grid-template-columns:34px minmax(130px,1.5fr) 100px 82px 118px; }
  .flcols > :nth-child(5), .flrow > :nth-child(5),
  .flcols > :nth-child(7), .flrow > :nth-child(7) { display:none; }
}
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
    <a class="wl-navlink" href="/quickstatements"
      title="paste TSV/CSV rows to add database connections to the Brain">Bulk add connections</a>
    <a class="wl-navlink" id="srcbtn" style="cursor:pointer" title="every external database the brain links to — layer, provenance, license">Sources</a>
    <a class="wl-navlink" href="/stats">Stats</a>
    <a class="wl-navlink" href="https://github.com/Deicyde/WikiLean" rel="noopener">GitHub</a>
    <span class="wl-navlink" id="wl-auth"><a href="/login?returnTo=/brain">Log in</a></span>
  </nav>
</header>
<div class="toolbar">
  <span class="grp"><b>View</b>
    <button id="explorerbtn" class="fchip" title="flatten the current area's subtree into the complete cell graph — every cell at its build-time position and every synapse among them. The library + facet filters narrow it; at the top level it covers every cell in the current build.">Explorer</button>
    <button id="hiddenchip" class="fchip" style="display:none"
      title="items the current library/facet filters removed from this view — click to open the Libraries panel"></button>
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
</div>
<div id="crumbbar"><span id="crumbpath"></span><span id="crumbside"><a id="srcbtn2" style="cursor:pointer"
  title="every external database the brain links to — layer, provenance, license">Sources</a><span
  id="release-id" title="selected immutable Brain release"></span><span
  class="note" id="status">loading manifest…</span></span></div>
<div class="main">
  <div id="stage"><svg id="svg"></svg>
    <canvas id="xcanvas"></canvas>
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
    <div id="flist" tabindex="0" aria-label="frontier queue"></div>
    <div id="fviewtoggle">
      <button id="fv-list" class="fchip" title="the frontier as a ranked queue — every homeless concept ordered by its formal proximity">list</button>
      <button id="fv-map" class="fchip" title="the frontier as a polar map — areas as angular sectors, radius = formal-proximity percentile">map</button>
    </div>
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
// The page is release-neutral on disk. boot() resolves the selector once, checks
// the immutable release manifest identity, and pins every Brain data read in this
// tab to that namespace. It never falls back to mutable compatibility aliases.
const RELEASE_SELECTOR_URL = "/assets/brain/current.json";
const RELEASE_ID_RE = /^sha256:([0-9a-f]{64})$/;
let RELEASE_ID = "", RELEASE_HEX = "", RELEASE_BASE = "", BASE = "", SOURCES_URL = "";
function canonicalIdentityJson(value) {
  if (value === null || typeof value === "boolean" || typeof value === "string")
    return JSON.stringify(value);
  if (typeof value === "number" && Number.isSafeInteger(value)) return String(value);
  if (Array.isArray(value)) return "[" + value.map(canonicalIdentityJson).join(",") + "]";
  if (value && typeof value === "object") {
    return "{" + Object.keys(value).sort().map(key =>
      JSON.stringify(key) + ":" + canonicalIdentityJson(value[key])).join(",") + "}";
  }
  throw new Error("unsupported release identity value");
}
async function domainIdentity(domain, value, excluded) {
  const identityValue = {...value};
  excluded.forEach(key => delete identityValue[key]);
  const bytes = new TextEncoder().encode(
    "wikilean\0" + domain + "\0canonical-json-v1\0" + canonicalIdentityJson(identityValue));
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
  return "sha256:" + Array.from(digest, byte => byte.toString(16).padStart(2, "0")).join("");
}
async function selectRelease() {
  const response = await fetch(RELEASE_SELECTOR_URL, {cache: "no-cache"});
  if (!response.ok) throw new Error("release selector HTTP " + response.status);
  const selector = await response.json();
  const required = ["schema", "release_id", "release", "manifest"];
  const previousKeys = ["previous_release_id", "previous_release", "previous_manifest"];
  const allowed = new Set([...required, ...previousKeys, "audited_at"]);
  if (!selector || typeof selector !== "object" || Array.isArray(selector) ||
      required.some(key => !(key in selector)) || Object.keys(selector).some(key => !allowed.has(key)))
    throw new Error("invalid release selector shape");
  const presentPrevious = previousKeys.filter(key => key in selector);
  if (presentPrevious.length !== 0 && presentPrevious.length !== previousKeys.length)
    throw new Error("invalid previous release selector");
  if (presentPrevious.length) {
    const previousMatch = typeof selector.previous_release_id === "string"
      ? RELEASE_ID_RE.exec(selector.previous_release_id) : null;
    if (!previousMatch || selector.previous_release !== previousMatch[1] ||
        selector.previous_manifest !== "/assets/brain/releases/" + previousMatch[1] + "/release.json" ||
        selector.previous_release_id === selector.release_id)
      throw new Error("invalid previous release selector");
  }
  if ("audited_at" in selector &&
      (typeof selector.audited_at !== "string" || !selector.audited_at))
    throw new Error("invalid release selector audit timestamp");
  const match = typeof selector.release_id === "string"
    ? RELEASE_ID_RE.exec(selector.release_id) : null;
  if (!match || selector.schema !== "wikilean.release-selector/v1" || selector.release !== match[1])
    throw new Error("invalid release selector");
  const releaseBase = "/assets/brain/releases/" + match[1] + "/";
  if (selector.manifest !== releaseBase + "release.json")
    throw new Error("release selector manifest mismatch");
  const manifestResponse = await fetch(selector.manifest);
  if (!manifestResponse.ok) throw new Error("release manifest HTTP " + manifestResponse.status);
  const releaseManifest = await manifestResponse.json();
  if (!releaseManifest || releaseManifest.schema !== "wikilean.release/v1" ||
      releaseManifest.release_id !== selector.release_id ||
      releaseManifest.release_id !== await domainIdentity(
        "wikilean.release.v1", releaseManifest, ["release_id", "attestations", "created_at"]))
    throw new Error("release manifest identity mismatch");
  RELEASE_ID = selector.release_id;
  RELEASE_HEX = match[1];
  RELEASE_BASE = releaseBase;
  BASE = releaseBase + "cells/";
  SOURCES_URL = releaseBase + "sources.json";
}
const ROOTS_ID = "__libs__";          // pseudo-focus: the library roots
const UNPLACED_ID = "__unplaced__";   // pseudo-focus: cells neither the tree nor
                                      // the frontier partition places — with the
                                      // frontier shipped, only the residue whose
                                      // decls have no recorded module (~5 cells);
                                      // without it (fail-soft), the whole
                                      // homeless population, as before
const STRAYS_PREFIX = "__strays__:";  // pseudo-focus: "__strays__:<path>" — the
                                      // cells filed at that level, collapsed into
                                      // one bubble but dive-able like a folder
                                      // bubble (see focusItems)
const FRONTIER_ID = "__frontier__";   // pseudo-focus: the Frontier group — the
                                      // frontier:<Area> partition of the homeless
                                      // cells (brain/build_frontier.py), which
                                      // replaced the old undifferentiated
                                      // "no formal home" blob
const isFrontierId = id => typeof id === "string" && id.startsWith("frontier:");
// "#__frontier__:<token>" — a frontier sub-view. The token is either "map"
// (the polar map), "map:<Area>" (the map with one sector focused) or a bare
// frontier <Area> id segment (hash-safe by the frontier id grammar
// ^[A-Za-z][A-Za-z0-9_]{0,63}$, NOT its display label) — the QUEUE filtered to
// that area, which is where every pre-queue sector deep link now lands.
const isSectorId = id => typeof id === "string" && id.startsWith(FRONTIER_ID + ":");
// The frontier's TWO surfaces share one grammar (the queue is the DEFAULT):
//   __frontier__            → the ranked LIST (the queue)
//   __frontier__:<Area>     → the queue filtered to that area (old sector links)
//   __frontier__:map        → the polar map, full circle
//   __frontier__:map:<Area> → the polar map, one sector focused
// "map" is matched AHEAD of the area grammar: no frontier area is named "map"
// today, and reserving the token keeps the routing unambiguous if one ever is.
const FRONTIER_MAP_ID = FRONTIER_ID + ":map";
function frontierViewOf(id) {
  if (id === FRONTIER_ID) return {mode: "list", area: null};
  if (!isSectorId(id)) return null;
  const tok = id.slice(FRONTIER_ID.length + 1);
  if (tok === "map") return {mode: "map", area: null};
  if (tok.startsWith("map:")) return {mode: "map", area: "frontier:" + tok.slice(4)};
  return {mode: "list", area: "frontier:" + tok};
}
// the ONE frontier surface pair (queue or map, full or focused)
const isFrontierViewId = id => id === FRONTIER_ID || isSectorId(id);
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
// Every data fetch is pinned to one immutable release namespace. dataV remains
// a useful cache/debug key, but a failed shard never re-resolves the selector or
// crosses into a newer release during this page session.
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
async function getEntry(id) {
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
  if (!res.ok) {
    // Immutable namespace means a retry must stay on this release, but a
    // transient asset failure must not poison the entry cache for the session.
    shardCache.delete(key);
    return null;
  }
  entryCache.set(id, e);
  return e;
}

const $ = s => document.querySelector(s);
const stageEl = $("#stage"), panelEl = $("#panel"), statusEl = $("#status");
const releaseEl = $("#release-id");
const crumbEl = $("#crumbpath");   // the bar also holds Sources + the status (right side)
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
  if (!j) { tree = {roots: [], frontier: [], frontierFa: 0, frontierN: 0,
                    cellArea: new Map(), sc: {}, unplaced: [], unplacedFa: 0,
                    prox: false, proxDrift: 0, cellProx: new Map(),
                    suitability: false, suitabilityDrift: 0,
                    cellSuitability: new Map(), count: () => 0}; return tree; }
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
  // a cell no supercell OR frontier area claims. With the frontier layer shipped
  // this is a tiny residue (decls with no `contains` parent, e.g. Mathlib-Archive
  // names), NOT the old 1.6k homeless blob — but it stays browsable either way,
  // because a bucket that silently vanishes is the 'extreme minority' bug.
  const unplaced = (labels || []).map(r => r.id).filter(id => !placed.has(id));
  let unplacedFa = 0;
  for (const id of unplaced) unplacedFa |= (labelById.get(id) || {}).f || 0;
  // the FRONTIER partition (brain/build_frontier.py): frontier:<Area> rows are
  // parentless like library roots, but they are not libraries — split them out so
  // the root level shows ONE dive-able "Frontier" group beside the library roots
  // instead of 46 loose area bubbles (or, before the frontier, one grey blob).
  const frontier = (j.roots || []).filter(p => (sc[p] || {}).frontier)
    .sort((a, b) => ((sc[b].cells || []).length - (sc[a].cells || []).length)
                    || (a < b ? -1 : 1));
  const roots = (j.roots || []).filter(p => !(sc[p] || {}).frontier);
  let frontierFa = 0, frontierN = 0;
  const cellArea = new Map();   // homeless cell -> its area (breadcrumb/zoom-out)
  for (const p of frontier) {
    frontierFa |= sc[p].fa || 0;
    frontierN += (sc[p].cells || []).length;
    for (const c of sc[p].cells || []) cellArea.set(c, p);
  }
  // the PROXIMITY layer: frontier rows carry `prox` — six arrays PARALLEL to
  // that row's `cells` (PROXIMITY CONTRACT, brain/SCHEMA.md "Formal
  // proximity"): db/dw = the cell's direct bonds/summed RAW trace weight into
  // formalized cells, ib/iw = bridging frontier neighbors/bridged weight,
  // s = score (dw + iw/4), r = rank percentile of s over ALL frontier cells
  // (0 ≈ most proximal, ties share). cellProx powers the frontier view's
  // radial placement, the cell cards' provenance line and the best-evidenced-
  // first dive sort. RE-CHECKED here on the shipped bytes: a row whose array
  // lengths disagree with its cells is counted and DROPPED loudly — misaligned
  // arrays never feed a render.
  let prox = false, proxDrift = 0;
  const cellProx = new Map();   // homeless cell -> {db, dw, ib, iw, s, r}
  const PROX_KEYS = ["db", "dw", "ib", "iw", "s", "r"];
  for (const p of frontier) {
    const px = sc[p].prox;
    if (!px) continue;
    const cs = sc[p].cells || [];
    if (!PROX_KEYS.every(k => Array.isArray(px[k]) && px[k].length === cs.length)) {
      proxDrift += cs.length;
      continue;
    }
    prox = true;
    cs.forEach((c, i) => cellProx.set(c, {db: px.db[i], dw: px.dw[i],
      ib: px.ib[i], iw: px.iw[i], s: px.s[i], r: px.r[i]}));
  }
  if (proxDrift)
    console.warn(`[brain frontier] prox drift: ${proxDrift} cell(s) in area(s) whose prox arrays and cells disagree — dropped, not trusted`);
  let suitability = false, suitabilityDrift = 0;
  const cellSuitability = new Map();
  for (const p of frontier) {
    const su = sc[p].suitability;
    if (!su) continue;
    const cs = sc[p].cells || [];
    if (!Array.isArray(su.candidate) || !Array.isArray(su.reason) ||
        su.candidate.length !== cs.length || su.reason.length !== cs.length) {
      suitabilityDrift += cs.length;
      continue;
    }
    suitability = true;
    cs.forEach((c, i) => cellSuitability.set(c, {
      candidate: su.candidate[i] === true,
      reason: su.reason[i] || null,
    }));
  }
  if (suitabilityDrift)
    console.warn(`[brain frontier] suitability drift: ${suitabilityDrift} cell(s) in area(s) whose suitability arrays and cells disagree — treated as review-needed`);
  if (prox) {
    const noProx = frontier.filter(p => !sc[p].prox);
    if (noProx.length)
      console.warn(`[brain frontier] ${noProx.length} frontier area(s) ship no prox: ${noProx.join(", ")}`);
  }
  tree = {roots, frontier, frontierFa, frontierN, cellArea, sc,
          unplaced, unplacedFa, prox, proxDrift, cellProx,
          suitability, suitabilityDrift, cellSuitability, count};
  buildCellLibs();   // the (V) predicate's cell → owning-libraries table
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
  if (id === ROOTS_ID || id === UNPLACED_ID || id === FRONTIER_ID || isCellId(id)) return id;
  await ensureTree();
  // legacy "#__halo__" hashes land on the ONE frontier view (the hop-shell
  // halo was destroyed 2026-08-04 — old links must not dead-end)
  if (id === "__halo__" || id.startsWith("__halo__:"))
    id = id === "__halo__" ? FRONTIER_ID
      : FRONTIER_ID + ":" + id.slice("__halo__:".length);
  if (id === FRONTIER_ID) return id;
  // a frontier sub-view whose area vanished from this build resolves to the
  // FULL view of the same surface (queue or map), never to a dead canvas
  if (isSectorId(id)) {
    const v = frontierViewOf(id);
    if (v.mode === "map")
      return !v.area || (tree.sc[v.area] || {}).frontier ? id : FRONTIER_MAP_ID;
    return (tree.sc[v.area] || {}).frontier ? id : FRONTIER_ID;
  }
  if (isFrontierId(id)) return tree.sc[id] ? id : null;
  if (id.startsWith(STRAYS_PREFIX))
    return tree.sc[id.slice(STRAYS_PREFIX.length)] ? id : null;
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
// ---- the Libraries config: which formal libraries count as "formal code" ----
// State = the DISABLED set of library root names ("Mathlib", "TauCeti", …).
// Default all-on; persists in localStorage "wl-brain-libs" (the ENABLED list;
// key absent = all on) AND in the hash (&libs=Mathlib,TauCeti when not-all).
// A disabled library: its root bubble and every cell it places are REMOVED
// from the map/panels/search (the ONE visibility predicate below; the
// '+N hidden' chip restores discoverability), its declarations stop counting
// as formal evidence in the frontier re-score, and the frontier core disc
// label lists what is left on.
let disabledLibs = new Set();
const libsFiltered = () => disabledLibs.size > 0;
function libRoots() {   // every library root WITH cells (8 today) — from the tree
  return tree ? tree.roots.filter(p => tree.count(p) > 0).map(p => p.slice(5)) : [];
}
const enabledLibs = () => libRoots().filter(n => !disabledLibs.has(n));
function persistLibs(writeHash = true) {
  try {
    if (libsFiltered()) localStorage.setItem("wl-brain-libs", enabledLibs().join(","));
    else localStorage.removeItem("wl-brain-libs");
  } catch (e) { /* storage unavailable — the hash still carries the state */ }
  if (writeHash) setHash(focusId === ROOTS_ID ? "" : focusId || "");
}
// enabled-list → state. A stored list naming NO current root is stale data,
// not "all off" — reset to the all-on default. An EMPTY list is a deliberate
// all-off (every frontier cell scores zero — honest, if bleak).
function applyLibsList(en) {
  const known = libRoots();
  const keep = new Set(en.filter(n => known.includes(n)));
  if (!keep.size && en.length) { disabledLibs = new Set(); return; }
  disabledLibs = new Set(known.filter(n => !keep.has(n)));
}
function initLibs(hashLibs) {   // boot: the hash wins over localStorage
  let en = hashLibs;
  if (en === null) {
    try { const s = localStorage.getItem("wl-brain-libs");
          en = s === null ? null : s.split(",").filter(Boolean); }
    catch (e) { en = null; }
  }
  if (en !== null) applyLibsList(en);
}
function syncLibCheckboxes() {
  document.querySelectorAll(".libcb").forEach(cb => {
    cb.checked = !disabledLibs.has(cb.dataset.lib);
  });
}
// ---- (V) THE visibility predicate ------------------------------------------
// ONE predicate decides what every surface shows:
//   visible(cell) = libEnabled(the cell's owning library) AND facet-mask match.
// Owning library = the library root(s) whose subtree places the cell (built
// once from the containment tree; a cell placed under several roots stays
// visible while ANY of them is enabled). Homeless/frontier cells have no
// owning library: they are library-independent everywhere EXCEPT the frontier
// view, where the library set governs them through the re-SCORE (their bonded
// evidence moves them outward) — never through removal. Bubbles, panels,
// search badges, the frontier membership and the explorer all ask THIS
// function; no surface re-implements the test (the reshape contract's
// single-predicate law).
let cellLibs = null;   // cell id -> [library root names], from tree containment
function buildCellLibs() {
  cellLibs = new Map();
  for (const rootP of tree.roots) {
    const lib = rootP.slice(5);
    const stack = [rootP];
    while (stack.length) {
      const p = stack.pop();
      const v = tree.sc[p];
      if (!v) continue;
      for (const c of v.cells || []) {
        const a = cellLibs.get(c);
        if (a) { if (!a.includes(lib)) a.push(lib); }
        else cellLibs.set(c, [lib]);
      }
      for (const ch of v.children || []) stack.push(ch);
    }
  }
}
const facetOk = f => !filterMask || (((f || 0) & filterMask) !== 0);
function libOkById(id) {
  if (!disabledLibs.size) return true;
  const libs = cellLibs && cellLibs.get(id);
  if (!libs) return true;   // homeless/frontier/unfiled: library-independent
  return libs.some(L => !disabledLibs.has(L));
}
function cellVisible(id, f) {
  if (f === undefined) {
    const r = labelById && labelById.get(id);
    f = (r && r.f) || 0;
  }
  return libOkById(id) && facetOk(f);
}
const filtersActive = () => !!filterMask || libsFiltered();
// ---- filtered subtree counts, memoized per predicate epoch ------------------
// tree.count under the predicate (measured ~5ms for ALL 9,363 supercell rows in
// the browser); the memo keys on the (libs, facet) state so one render pass
// never recomputes a row, and the unfiltered fast path IS tree.count exactly.
let visMemo = new Map(), visMemoKey = null;
function visEpoch() { return [...disabledLibs].sort().join(",") + "|" + filterMask; }
function countVisible(p) {
  if (!filtersActive()) return tree.count(p);
  const key = visEpoch();
  if (key !== visMemoKey) { visMemo = new Map(); visMemoKey = key; }
  if (visMemo.has(p)) return visMemo.get(p);
  const v = tree.sc[p];
  if (!v) return 0;
  visMemo.set(p, 0);   // cycle guard, same as tree.count
  let n = 0;
  for (const c of v.cells || []) if (cellVisible(c)) n++;
  for (const ch of v.children || []) n += countVisible(ch);
  visMemo.set(p, n);
  return n;
}
function unplacedVisibleN() {
  if (!filtersActive()) return tree.unplaced.length;
  let n = 0;
  for (const id of tree.unplaced) if (cellVisible(id)) n++;
  return n;
}
function frontierVisibleN() {
  if (!filtersActive()) return tree.frontierN;
  let n = 0;
  for (const p of tree.frontier) n += countVisible(p);
  return n;
}
// the '+N hidden' chip: how many items the predicate removed from the CURRENT
// view — the discoverability affordance that keeps true removal honest
// (no-silent-filter rule). Click → the Libraries panel.
function updateHiddenChip(hidden) {
  const el = $("#hiddenchip");
  if (!el) return;
  el.style.display = hidden > 0 ? "" : "none";
  if (hidden > 0) el.textContent = `+${hidden.toLocaleString()} hidden`;
}
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
    // in the explorer the scene lives on the canvas and gViewport is EMPTY —
    // skip the per-event DOM write (the rAF draw reads the transform itself);
    // explorerOn (not layout.explorer) so the leave-path resetZoom still writes
    if (!explorerOn) gViewport.attr("transform", ev.transform); })
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
const CELL_INFORMAL = "#8c959f";      // atom with no attached declaration organ
const GOLD = "#eab308";               // a hand-written @[wikidata] tag rides in this atom
function fillFor(item) {
  // a frontier area's tint IS its mean stateability, on the SAME grey→blue
  // formalization axis the cells use (CELL_INFORMAL → CELL_FORMAL); an unscored
  // area (`s` null — no halo-joined member) keeps the plain folder shade rather
  // than faking a zero
  if (item.type === "folder")
    return item.s != null
      ? mix(CELL_INFORMAL, CELL_FORMAL, Math.max(0, Math.min(1, item.s)))
      : SHADE;
  if (item.type === "strays") return "#8c959f";
  return item.p ? CELL_FORMAL : CELL_INFORMAL;
}

// ---- level items: a supercell's sub-folders + the CELLS it holds ------------
function folderItem(p) {
  const sc = tree.sc[p] || {};
  // n = the count under the CURRENT predicate (drives pack size, labels and
  // survival); nAll = the unfiltered truth, so every count surface can say
  // "n of nAll shown" instead of silently shrinking
  return {id: p, type: "folder", label: sc.label || p, n: countVisible(p),
          nAll: tree.count(p), f: 0, fa: sc.fa || 0};
}
// a frontier area's display name: "Analysis frontier" → "Analysis" (the group's
// breadcrumb already says Frontier; repeating it 46 times is noise)
function frontierName(p) {
  const sc = (tree.sc || {})[p] || {};
  return (sc.label || p.slice(9)).replace(/\s+frontier$/i, "");
}
function frontierItem(p) {
  const sc = tree.sc[p] || {};
  return {id: p, type: "folder", label: frontierName(p),
          n: countVisible(p), nAll: (sc.cells || []).length, f: 0, fa: sc.fa || 0,
          s: sc.stateability != null ? sc.stateability : null};
}
// an area's cells for a DIVE, best-evidenced first: (formal-proximity
// percentile r ascending — the same radius the frontier view renders — then
// the row's own order). The sort is STABLE, so a build without prox — or any
// cell the prox arrays miss — keeps exactly today's order.
function frontierProxRank(cid) {
  // unknown ranks after every scored cell (r is always <= 1)
  const px = tree.cellProx && tree.cellProx.get(cid);
  return px === undefined ? 2 : px.r;
}
function frontierCells(p) {
  const ids = ((tree.sc || {})[p] || {}).cells || [];
  if (!tree.prox) return ids;
  return ids.map((c, i) => [frontierProxRank(c), i, c])
    .sort((a, b) => a[0] - b[0] || a[1] - b[1]).map(t => t[2]);
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
    // (V)/(B): a library the Libraries control turned off is ABSENT — n forced
    // to 0 so applyVisibility removes the bubble (true removal; the '+N hidden'
    // chip restores discoverability). No dimming.
    for (const it of items) if (disabledLibs.has(it.id.slice(5))) it.n = 0;
    // the FRONTIER: one root-level group holding the frontier:<Area> partition of
    // the homeless cells (+ the tiny unfiled residue) — replaces the old
    // undifferentiated "no formal home" blob. When the build shipped no frontier
    // rows (fail-soft path), the old blob stays, honestly labelled.
    if (tree.frontier.length)
      items.push({id: FRONTIER_ID, type: "folder", label: "Frontier",
                  n: frontierVisibleN() + unplacedVisibleN(),
                  nAll: tree.frontierN + tree.unplaced.length, f: 0,
                  fa: tree.frontierFa | tree.unplacedFa});
    else if (tree.unplaced.length)
      items.push({id: UNPLACED_ID, type: "folder", label: "no formal home",
                  n: unplacedVisibleN(), nAll: tree.unplaced.length,
                  f: 0, fa: tree.unplacedFa});
    return items;
  }
  if (id === FRONTIER_ID) {
    const items = tree.frontier.map(frontierItem);
    if (tree.unplaced.length)
      items.push({id: UNPLACED_ID, type: "folder", label: "unfiled",
                  n: unplacedVisibleN(), nAll: tree.unplaced.length,
                  f: 0, fa: tree.unplacedFa});
    return items;
  }
  // an area's cells dive exactly like a supercell's: flat dots, cellItem each —
  // sorted best-evidenced first (prox r asc; stable without prox)
  if (isFrontierId(id)) return frontierCells(id).map(cellItem);
  if (id === UNPLACED_ID) return tree.unplaced.map(cellItem);
  // diving into a strays bubble shows the cells filed at its level as dots —
  // the (V) predicate then REMOVES any the current filters hide (applyVisibility)
  if (id.startsWith(STRAYS_PREFIX))
    return ((tree.sc[id.slice(STRAYS_PREFIX.length)] || {}).cells || []).map(cellItem);
  const sc = tree.sc[id];
  if (!sc) return [];
  const folders = (sc.children || []).map(folderItem);
  // a cell that spans several modules is listed by EACH of them, so it renders
  // inside each — exactly what SCHEMA's `supercells` array asks for
  let cells = (sc.cells || []).map(cellItem);
  // At a level that HAS sub-areas, the cells filed directly here would flood the
  // pack and shrink Algebra to a dot (Mathlib once held 567) — collapse
  // them into one DIVE-ABLE bubble. Under an active facet filter the matches
  // stay out as dots and the non-matches are REMOVED by applyVisibility (true
  // removal — no dimmed remainder bubble). At a library ROOT the honest label
  // is different: nothing is legitimately "filed" at path:Mathlib — a decl lands
  // there only because it has NO recorded module (mostly stale renames and
  // hallucinated citations in annotations; see straysPanel).
  if (folders.length && !filterMask) {
    const vis = filtersActive() ? cells.filter(c => cellVisible(c.id, c.f)) : cells;
    if (vis.length > 12) {
      const atRoot = !id.slice(5).includes("/");
      // library-hidden cells swallowed by the collapse are still HIDDEN
      // content — hiddenInside feeds the '+N hidden' total (no-silent-filter)
      cells = [{id: STRAYS_PREFIX + id, type: "strays", n: vis.length,
                nAll: cells.length, hiddenInside: cells.length - vis.length,
                label: vis.length +
                  (vis.length < cells.length ? ` of ${cells.length}` : "") +
                  (atRoot ? " cells · no module recorded" : " cells filed here")}];
    }
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

// The explorer draws on canvas: every input (wheel, drag, filter, resize)
// funnels into scheduleXDraw and at most ONE frame is painted per rAF. The old
// SVG version ran three full-selection passes over 20,880 circles per wheel
// EVENT (measured ~53ms of main thread per pan frame before rasterising) —
// never re-grow a per-event DOM write here.
function applyExplorerScale(k) {
  if (!layout || !layout.explorer) return;
  lastK = k || 1;
  scheduleXDraw();
}

function drawNodes(reshape) {
  const leaves = layout.leaves;
  // 8.9k <title> children is real DOM weight on the flat map and the labels
  // already name the big ones — hover text is a level-view affordance
  const withTitles = !(layout.explorer && leaves.length > 2000);
  const bubbles = gBubbles.selectAll("circle.node").data(leaves, l => l.data.id);
  bubbles.exit().remove();
  const entered = bubbles.enter().append("circle")
    .attr("class", l => l.data.type === "folder" ? "bubble node" : "dot node");
  const all = entered.merge(bubbles);
  // <title> presence syncs on the MERGED selection: the data join reuses a
  // circle across views (same cell id), so an enter-only append left every
  // frontier dot TITLELESS when the reader arrived from the explorer (and handed
  // the explorer stale level-view titles). A circle's only legal child is its
  // <title>, so firstElementChild is the exact test.
  all.each(function () {
    const t = this.firstElementChild;
    if (withTitles) {
      if (!t) this.appendChild(document.createElementNS("http://www.w3.org/2000/svg", "title"));
    } else if (t) t.remove();
  });
  // cells are dots, never discs: a near-empty focus level would otherwise
  // pack its lone cell to fill the stage (a 568px "plain blue dot")
  const rOf = l => l.data.type === "cell" ? Math.min(Math.max(l.r, 2), 42)
                                          : Math.max(l.r, 2);
  all
    .attr("fill", l => fillFor(l.data))
    .attr("fill-opacity", l => l.data.type === "folder" ? 0.55 : 0.9)
    // the gold ring marks an atom carrying a hand-written @[wikidata] tag —
    // inline style, because the .dot/.bubble CSS stroke overrides an attribute
    .style("stroke", l => l.data.type === "cell" && ((l.data.f || 0) & 1) ? GOLD : null)
    .style("stroke-width", l => l.data.type === "cell" && ((l.data.f || 0) & 1) ? "1.6px" : null)
    .on("click", (ev, l) => { ev.stopPropagation(); nodeClick(l.data); });
  if (reshape) {
    // (B) filters-changed reshape: surviving circles GLIDE to their repacked
    // position/size (the transition starts on THIS frame — first reshaped
    // frame well inside the 300ms gate); entering circles fade in at their
    // final spot. interrupt() first so a rapid double-toggle never leaves a
    // circle mid-flight toward a stale layout.
    entered.attr("cx", l => l.x).attr("cy", l => l.y).attr("r", rOf)
      .attr("opacity", 0)
      .transition("reshape").duration(260).attr("opacity", 1);
    bubbles.interrupt("reshape").attr("opacity", null)
      .transition("reshape").duration(260).ease(d3.easeCubicInOut)
      .attr("cx", l => l.x).attr("cy", l => l.y).attr("r", rOf);
    // rAF-driven transitions pause in background tabs — snap everything to its
    // exact final geometry once the window has passed (restore stays honest:
    // the end state is the pack output, bit-identical to a cold render)
    setTimeout(() => {
      gBubbles.selectAll("circle.node").interrupt("reshape").attr("opacity", null)
        .attr("cx", l => l.x).attr("cy", l => l.y).attr("r", rOf);
    }, 600);
  } else {
    all.interrupt("reshape").attr("opacity", null)
      .attr("cx", l => l.x).attr("cy", l => l.y).attr("r", rOf);
  }
  if (withTitles) all.select("title").text(l => l.data.label
    + (l.data.type === "folder"
        ? (l.data.nAll != null && l.data.nAll !== (l.data.n || 0)
            ? ` — ${(l.data.n || 0).toLocaleString()} of ${l.data.nAll.toLocaleString()} cells shown (filtered)`
            : ` — ${(l.data.n || 0).toLocaleString()} cells`)
          + (l.data.s != null ? ` · mean stateability ${l.data.s.toFixed(2)}` : "")
      : l.data.type === "strays" ? " — click to open them as dots, with the story of why they sit here"
      : (((l.data.f || 0) & 1 ? " — carries a hand-written @[wikidata] tag" : "")
         + (l.data.ptip === undefined ? ""    // frontier dots wear area + bond summary:
           : (l.data.area ? ` · ${l.data.area}` : "")   // "label · area · N bonds …"
             + ` · ${l.data.ptip}`))));
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
        .attr("font-size", fs)
        .text(l.data.label);
      if (!flat && l.r > 40)
        gLabels.append("text").attr("class", "bcount")
          .attr("x", l.x).attr("y", l.y + fs - 2).attr("font-size", fs * 0.72)
          // the no-silent-filter rule: a filtered count never poses as the total
          .text(l.data.nAll != null && l.data.nAll !== (l.data.n || 0)
            ? `${(l.data.n || 0).toLocaleString()} of ${l.data.nAll.toLocaleString()} cells`
            : `${(l.data.n || 0).toLocaleString()} cells`);
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
// The level views' twin of the explorer's zoom^2 label budget — same budget
// shape, same screen-space font size, for the same reason.
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

// ---- (V) applying the predicate to a level ---------------------------------
// True REMOVAL, never dimming: a cell failing cellVisible is not in the
// layout; a container (folder / strays / the Frontier group) whose visible
// count n is 0 is not in the layout. The '+N hidden' chip and the "N of M
// shown" labels keep the removal honest (no-silent-filter rule). Folder n is
// recomputed under the predicate by focusItems (countVisible), so survival and
// pack size agree with the counts every panel shows.
function applyVisibility(items) {
  const active = filtersActive();
  if (!active)
    return {items, shown: items.length, total: items.length, hidden: 0,
            hiddenCells: 0, active};
  const kept = [];
  // hiddenCells counts CELLS uniformly across views (a removed folder weighs
  // its nAll, a strays collapse carries hiddenInside) so the '+N hidden' chip
  // never flips units between the roots level and the explorer.
  let hiddenCells = 0;
  for (const it of items) {
    if (it.type === "folder" || it.type === "strays") {
      hiddenCells += it.hiddenInside || 0;
      if ((it.n || 0) > 0) kept.push(it);
      else hiddenCells += it.nAll ?? it.n ?? 0;
    } else if (cellVisible(it.id, it.f)) kept.push(it);
    else hiddenCells += 1;
  }
  return {items: kept, shown: kept.length, total: items.length,
          hidden: items.length - kept.length, hiddenCells, active};
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
  // the frontier view's core disc + radial guide live in gEdges — never wipe
  // them (its dots carry no level-view synapse web to draw anyway)
  if (layout && layout.frontier) return;
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
    if (!A || !B) continue;   // an endpoint the (V) predicate removed has no layout item
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
    // the Frontier group's "children" are its areas — same faint preview
    const kids = (l.data.id === FRONTIER_ID ? tree.frontier.map(frontierItem)
      : (tree.sc[l.data.id] && tree.sc[l.data.id].children || []).map(folderItem))
      .sort((a, b) => b.n - a.n).slice(0, 24);
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

async function renderFocus(anim, opts) {
  opts = opts || {};
  if (explorerOn) return renderExplorer(anim);
  xcanvasShow(false);   // every non-explorer view: the canvas is gone, SVG owns the stage
  const seq = ++renderSeq;
  if (!opts.keepZoom) resetZoom();   // a filters-changed reshape happens IN PLACE
  await ensureTree();
  if (seq !== renderSeq) return;
  if (isFrontierViewId(focusId)) {
    if (tree.prox) {
      const fv = frontierViewOf(focusId);
      if (fv.mode === "list") return renderFrontierList(seq, anim);
      flistShow(false);
      return renderFrontier(seq, anim);
    }
    focusId = FRONTIER_ID;   // fail-soft: a prox-less build renders area bubbles
  }
  flistShow(false);   // every non-queue view: the list overlay is gone
  if (isCellId(focusId)) {
    const fe = await getEntry(focusId);
    if (seq !== renderSeq) return;
    if (fe) return renderCellEgo(seq, fe, anim);
    focusId = ROOTS_ID;   // unknown atom → don't strand the canvas
  }
  const items = await focusItems(focusId);
  if (seq !== renderSeq) return;
  const fv = applyVisibility(items);
  updateFilterStat(fv);
  updateHiddenChip(fv.hiddenCells ?? fv.hidden);   // cell units, every view
  // chrome FIRST, measure AFTER (the renderFrontier lesson): the status line
  // and crumb can re-wrap the toolbar and change the stage height with no
  // resize event — packing against the pre-write stage shifted every bubble
  // ~3.3% on the first toggle after a cold boot
  renderCrumb();
  const foldersShown = fv.items.filter(i => i.type === "folder").length;
  const foldersTotal = items.filter(i => i.type === "folder").length;
  const cellsShown = fv.items.length - foldersShown;
  const cellsTotal = items.length - foldersTotal;
  // the no-silent-filter rule: under an active predicate every count reads
  // "N of M … shown" — a shrunken number never poses as the universe
  statusEl.textContent = (fv.active
      ? `${cellsShown.toLocaleString()} of ${cellsTotal.toLocaleString()} cells · ` +
        `${foldersShown} of ${foldersTotal} areas shown · `
      : `${cellsShown.toLocaleString()} cells · ${foldersShown} areas · `) +
    `${focusId === ROOTS_ID ? "all libraries"
      : focusId === FRONTIER_ID ? "the Frontier"
      : isFrontierId(focusId) ? frontierName(focusId) + " frontier"
      : focusId === UNPLACED_ID ? (tree.frontier.length ? "unfiled" : "no formal home")
      : focusId.startsWith(STRAYS_PREFIX)
        ? focusId.slice(STRAYS_PREFIX.length + 5)
          + (focusId.slice(STRAYS_PREFIX.length + 5).includes("/")
             ? " · filed here" : " · no module recorded")
      : focusId.slice(5)}`;
  const W = stageEl.clientWidth || 800, H = stageEl.clientHeight || 600;
  const root = d3.hierarchy({children: fv.items}).sum(d => d.children ? 0 : packValue(d));
  d3.pack().size([W, H]).padding(fv.items.length > 150 ? 1.5 : 4)(root);
  const leaves = root.leaves().filter(l => l.data.id);

  layout = {items: new Map(leaves.map(l => [l.data.id, l])), leaves};
  edgeStore = [];
  gEdges.selectAll("*").remove();
  gOverlay.selectAll("*").remove();
  gBubbles.selectAll("circle.preview").remove();
  drawNodes(!!opts.reshape);
  drawLabels();
  if (opts.reshape) fadeLabelsIn();   // labels land at final positions, softened
  drawSelRing();
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
// the reshape path's label treatment: gLabels is wiped + redrawn at the new
// pack, so fade the fresh labels in while the circles glide (same 260ms clock,
// same background-tab snap guard as fadeIn)
function fadeLabelsIn() {
  gLabels.attr("opacity", 0).transition("reshape").duration(260).attr("opacity", 1);
  setTimeout(() => { gLabels.interrupt("reshape"); gLabels.attr("opacity", 1); }, 600);
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
  updateHiddenChip(0);   // the ego view shows one atom's synapses — nothing is filtered out
  if (anim) fadeIn();
  renderPanel(id);
}
// ---- the frontier view: territories × formal proximity ----------------------
// ONE view for the frontier (the hop-shell halo was DESTROYED 2026-08-04:
// "1 jump away" said nothing about whether the jump rode 200 synapse bonds or
// one thread to an isolated node). A deterministic, client-computed polar
// layout — pure arithmetic over the frontier rows' `prox` arrays and the area
// sizes. No simulation, no RNG: the same build renders the same view every
// visit. The frontier AREAS stay the organizational skeleton, as angular
// sectors (order = area size desc, stable); WITHIN a sector each cell's
// RADIAL position is its build-time bond-weighted formal-proximity percentile
// `r` — rank-robust by construction (the robust-fit rule: the radius map is a
// percentile, never a min/max fit over raw scores, so one 900-weight hub
// cannot stretch it). Heavily-evidenced cells hug the central formal disc, a
// single thread to an isolated neighbor sits far out, zero-signal cells are
// outermost. Dots go through the same cellItem pathway as an area dive (same
// tint, same click-through to the ego view).
const FRONTIER_RIM_LABELS = 12;   // the largest sectors get rim labels
let frontierBootTries = 0;        // boot guard: re-solve attempts against an unsettled stage
// ---- the frontier graph: client-side re-SCORING -----------------------------
// frontier_graph.json ships the WHOLE frontier synapse graph once (~152 KB):
// `cells` (every frontier cell id, sorted), `formal` (per cell, {"|"-joined
// EXACT owning-root set → summed RAW synapse weight} over its decl-holding
// neighbors — exact-set keys, so a library subset never double-counts a
// multi-root neighbor) and `edges` (every frontier↔frontier synapse as
// [i, j, w] weight triples into `cells`). The view fetches it LAZILY, once,
// and re-scores CLIENT-SIDE per the PROXIMITY CONTRACT (brain/SCHEMA.md):
//   direct_L(c) = Σ formal[c][K] over key sets K with K ∩ enabled ≠ ∅
//   score_L(c)  = direct_L(c) + Σ over edges (c,u,w) of min(w, direct_L(u))/4
//   r_L(c)      = (#cells with strictly higher score_L + #equal/2) / N, 4dp
//                 — the builder's own midrank formula, re-ranked per subset
//
// PARITY LAW: with every library enabled score_L must equal the shipped `s`
// EXACTLY (exact float equality — quarter-floats are lossless), asserted the
// moment the graph loads (console.error + a visible ⚠ in the status line on
// any mismatch). The all-on view renders the SHIPPED r verbatim, so it is
// identical to the build-time placement by construction.
let fgraph = null, fgraphP = null, fgraphFail = false;
let fgAdjOff = null, fgAdj = null, fgAdjW = null, fgFormal = null;
let parity = {ran: false, ok: null, bad: 0, missing: 0, extra: 0};
let clientProx = null, clientProxKey = null;   // cell -> {dw,ib,iw,s,r} for the CURRENT lib set
function prepFrontierGraph() {
  const cells = fgraph.cells || [], edges = fgraph.edges || [];
  const n = cells.length;
  const deg = new Uint32Array(n);
  for (const [i, j] of edges) { deg[i]++; deg[j]++; }
  fgAdjOff = new Uint32Array(n + 1);
  for (let i = 0; i < n; i++) fgAdjOff[i + 1] = fgAdjOff[i] + deg[i];
  fgAdj = new Uint32Array(fgAdjOff[n]);
  fgAdjW = new Float64Array(fgAdjOff[n]);
  const cur = fgAdjOff.slice(0, n);
  for (const [i, j, w] of edges) {
    fgAdj[cur[i]] = j; fgAdjW[cur[i]++] = w;
    fgAdj[cur[j]] = i; fgAdjW[cur[j]++] = w;
  }
  // per cell: [[root set (split once), weight], …]
  fgFormal = cells.map(c => Object.entries((fgraph.formal || {})[c] || {})
    .map(([k, w]) => [k.split("|"), w]));
}
// Re-score with the disabled libraries removed from the FORMAL evidence. A
// root the Libraries control does not list can never be toggled off, so it
// always conducts — never a silent drop.
function scoreCells(disabled) {
  const cells = fgraph.cells, n = cells.length;
  const direct = new Float64Array(n);   // integer trace weights — exact
  for (let i = 0; i < n; i++) {
    let d = 0;
    for (const [roots, w] of fgFormal[i])
      if (roots.some(L => !disabled.has(L))) d += w;
    direct[i] = d;
  }
  const out = new Map();
  for (let i = 0; i < n; i++) {
    let iw = 0, ib = 0;
    for (let e = fgAdjOff[i]; e < fgAdjOff[i + 1]; e++) {
      const du = direct[fgAdj[e]];
      if (du > 0) { iw += Math.min(fgAdjW[e], du); ib++; }   // the bottleneck rule
    }
    // iw accumulates INTEGERS; one /4 at the end — bit-for-bit the builder's
    // `direct + bridge * 0.25`, which is what the parity law demands
    out.set(cells[i], {dw: direct[i], ib, iw, s: direct[i] + iw / 4});
  }
  // r: midrank percentile of s — (#strictly higher + #equal/2)/N, 4dp,
  // ties share (the builder's formula, verbatim)
  const sorted = [...out.values()].map(p => p.s).sort((a, b) => b - a);
  const higher = new Map();   // score -> #cells strictly above it
  const count = new Map();    // score -> #cells at it
  for (let i = 0; i < sorted.length; i++) {
    const v = sorted[i];
    if (!count.has(v)) { higher.set(v, i); count.set(v, 0); }
    count.set(v, count.get(v) + 1);
  }
  for (const p of out.values())
    p.r = Math.round(((higher.get(p.s) + count.get(p.s) / 2) / n) * 1e4) / 1e4;
  return out;
}
function runParityCheck() {
  if (!fgraph || !tree || !tree.prox) return;
  const all = scoreCells(new Set());   // ALL libraries enabled
  let bad = 0, missing = 0, extra = 0;
  for (const [c, p] of all) {
    const ship = tree.cellProx.get(c);
    if (ship === undefined) missing++;   // in the graph, in no shipped prox row
    else if (ship.s !== p.s) bad++;      // EXACT equality — the parity law
  }
  for (const c of tree.cellProx.keys()) if (!all.has(c)) extra++;
  parity = {ran: true, ok: !(bad || missing || extra), bad, missing, extra};
  if (!parity.ok) {
    console.error(`[brain frontier] PARITY FAILURE: client re-score (all libraries) != shipped prox — ` +
      `${bad} cell(s) at a different score, ${missing} in the graph but in no shipped prox row, ` +
      `${extra} in shipped prox but missing from the graph`);
    // the render that is already on screen printed no warning — say it now
    if (layout && layout.frontier && isFrontierViewId(focusId))
      statusEl.textContent += " · ⚠ client scores disagree with the build (see console)";
  } else {
    console.info(`[brain frontier] parity OK: the client re-score reproduces the shipped scores for all ${
      all.size} frontier cells`);
  }
}
function fetchFrontierGraph() {
  if (!fgraphP) {
    fgraphP = fetch(BASE + "frontier_graph.json" + vq())
      .then(r => (r.ok ? r.json() : null)).catch(() => null)
      .then(j => {
        fgraph = j;
        fgraphFail = !j;
        if (j) { prepFrontierGraph(); runParityCheck(); }
        else console.warn("[brain frontier] frontier_graph.json unavailable — the library filter cannot re-score");
        return j;
      });
  }
  return fgraphP;
}
function ensureClientProx() {
  if (!fgraph) { clientProx = null; return; }
  const key = [...disabledLibs].sort().join(",");
  if (clientProxKey === key && clientProx) return;
  clientProx = scoreCells(disabledLibs);
  clientProxKey = key;
}
// The prox feeding the CURRENT render: shipped VERBATIM when every library is
// on (byte-identical to the build, and the parity assert proves the client
// re-score agrees anyway); client-computed when the set is filtered. A client
// row carries no `db` (the graph aggregates direct weight by root set, not by
// neighbor), so db is undefined there — the copy degrades honestly.
function activeProxFor(cid) {
  if (!libsFiltered() || !fgraph) return tree.cellProx.get(cid);
  ensureClientProx();   // self-syncs to the CURRENT set (cheap when unchanged) —
                        // a cell card opened after a root-level toggle must
                        // never read scores for a stale library set
  return clientProx ? clientProx.get(cid) : tree.cellProx.get(cid);
}
// the human-readable bond summary for ONE cell — the tooltip/panel provenance
// (evidence-mass wording; weights are RAW trace counts, per the contract)
function proxSummary(px) {
  if (!px) return "no proximity data in this build";
  if (px.dw > 0) {
    const direct = px.db !== undefined
      ? `${px.dw.toLocaleString()} trace${px.dw === 1 ? "" : "s"} across ${
          px.db} direct bond${px.db === 1 ? "" : "s"} into formalized cells`
      : `direct evidence weight ${px.dw.toLocaleString()} into formalized cells`;
    return direct + (px.iw > 0
      ? `, +${px.iw.toLocaleString()} bridged via ${px.ib} frontier neighbor${
          px.ib === 1 ? "" : "s"}` : "");
  }
  if (px.iw > 0)
    return `no direct bonds — evidence weight ${px.iw.toLocaleString()} bridged via ${
      px.ib} frontier neighbor${px.ib === 1 ? "" : "s"} (¼-damped, bottleneck-capped)`;
  return "no formal signal — no bonds into formalized cells and nothing to bridge through";
}
async function renderFrontier(seq, anim, moveAnim) {
  // The frontier graph is fetched lazily by THIS view, once. A filtered library
  // set needs it BEFORE scoring; all-on renders the shipped prox immediately
  // while the fetch + the parity assert run in the background.
  const graphP = fetchFrontierGraph();
  if (libsFiltered() && !fgraph && !fgraphFail) {
    await graphP;
    if (seq !== renderSeq) return;
  }
  if (libsFiltered()) ensureClientProx();   // no-op when the graph is missing
  // sector focus (#__frontier__:map:<Area>): that area's cells alone, spread
  // over the full circle. An area this build doesn't carry falls back to the
  // full map view.
  let sector = (frontierViewOf(focusId) || {}).area || null;
  if (sector && !((tree.sc[sector] || {}).frontier)) { sector = null; focusId = FRONTIER_MAP_ID; }
  // a re-score ANIMATES: remember where every dot sits now, move it after
  const oldPos = moveAnim && layout && layout.frontier
    ? new Map([...layout.items.values()].map(l => [l.data.id, [l.x, l.y]])) : null;
  // ---- data pass FIRST (geometry-free): sectors, prox counts, the predicate --
  // sectors: tree.frontier is already (size desc, id) — a stable order
  const areas = sector ? [sector] : tree.frontier;
  const perArea = [];
  let totalCells = 0, totalRaw = 0, facetHidden = 0,
      nDirect = 0, nBridged = 0, nZero = 0, skipped = 0;
  for (const p of areas) {
    const members = [];   // [{cid, px}]
    for (const cid of (tree.sc[p] || {}).cells || []) {
      const px = activeProxFor(cid);
      if (!px) { skipped++; continue; }   // no prox data — COUNTED below, never silent
      totalRaw++;
      // (H) the shared (V) predicate governs shell MEMBERSHIP: a cell failing
      // it is REMOVED from its shell and from every ring count. For homeless
      // cells the predicate's library half is always true — the library set
      // governs them through the re-SCORE above (activeProxFor), never removal.
      if (!cellVisible(cid)) { facetHidden++; continue; }
      members.push({cid, px});
      if (px.dw > 0) nDirect++; else if (px.iw > 0) nBridged++; else nZero++;
    }
    // radius ascending, then id — deterministic; equal-r runs form tie groups
    // (the 300-odd zero-signal cells all share ONE percentile, and a tie group
    // is laid out in stacked rows instead of a 1px overplotted arc)
    members.sort((a, b) => (a.px.r - b.px.r) || (a.cid < b.cid ? -1 : 1));
    if (members.length) {
      const groups = [];
      let g0 = 0;
      for (let i = 1; i <= members.length; i++)
        if (i === members.length || members[i].px.r !== members[g0].px.r) {
          groups.push({rv: members[g0].px.r, from: g0, m: i - g0});
          g0 = i;
        }
      perArea.push({p, members, groups, n: members.length});
    }
    totalCells += members.length;
  }
  const placed = [];   // [{it, A, G, j}] — items now, coordinates after measuring
  for (const A of perArea)
    for (const G of A.groups)
      for (let j = 0; j < G.m; j++) {
        const {cid, px} = A.members[G.from + j];
        const it = cellItem(cid);
        it.area = frontierName(A.p);   // tooltip: label · area · bond summary
        it.ptip = proxSummary(px);     // hover + card plumbing
        it.px = px;
        placed.push({it, A, G, j});
      }
  // ---- chrome pass: write EVERY toolbar/crumb line, THEN measure the stage --
  // The long status line and the filter stat can re-wrap the flex toolbar, and
  // the crumb bar grows from empty at boot — each changes the stage height with
  // NO window `resize` event (and ResizeObserver is undeliverable in embedded
  // panes, measured). Solving the radial layout before those writes left the
  // view drawn for a stage 16px taller than the one on screen. Write first; the
  // clientWidth/Height reads below then force (and measure) the settled layout.
  updateFilterStat({active: !!filterMask, shown: totalCells, total: totalRaw});
  updateHiddenChip(facetHidden);
  renderCrumb();   // the frontier branch writes synchronously (no await on its path)
  const en = enabledLibs();
  const libNote = !libsFiltered() ? ""
    : clientProx
      ? ` · libraries: ${en.length === 0 ? "none"
          : en.length <= 3 ? en.join(" + ")
          : `${en.length} of ${libRoots().length}`}`
      : " · ⚠ library filter inactive (frontier_graph.json unavailable)";
  const parityNote = parity.ran && !parity.ok
    ? " · ⚠ client scores disagree with the build (see console)" : "";
  statusEl.textContent = `${totalCells.toLocaleString()}${filterMask
      ? ` of ${totalRaw.toLocaleString()} cells shown` : " cells"} · frontier map` +
    (sector ? ` · ${frontierName(sector)} sector` : "") +
    ` · ${nDirect.toLocaleString()} bond formal code directly · ` +
    `${nBridged.toLocaleString()} bridged only · ` +
    `${nZero.toLocaleString()} no formal signal` +
    (skipped > 0 ? ` · ${skipped} cells lack proximity data (drift — see console)` : "") +
    libNote + parityNote;
  if (skipped > 0)
    console.warn(`[brain frontier] ${skipped} frontier cell(s) missing from prox — ` +
      `the view draws only what this build's prox arrays cover`);
  const stat = $("#structstat");
  if (stat) stat.textContent =
    "deterministic polar layout — no simulation; radius = build-time formal-proximity percentile";
  // ---- geometry pass: solve the polar layout for the settled stage ----------
  const W = stageEl.clientWidth || 800, H = stageEl.clientHeight || 600;
  // Boot guard: a mid-layout stage (flex not settled on a direct #__frontier__
  // load) can measure 0×150 here — the || fallbacks then solve a 800×150
  // wheel (39px radius). The stage ResizeObserver self-heals that where it
  // delivers, but not in every embedded pane (measured — see the observer
  // note below), so poll the real size briefly and re-solve.
  if ((stageEl.clientWidth < 60 || stageEl.clientHeight < 60) && frontierBootTries < 60) {
    frontierBootTries++;
    setTimeout(() => { if (layout && layout.frontier) renderFocus(false); }, 100);
  } else if (stageEl.clientWidth >= 60 && stageEl.clientHeight >= 60) {
    frontierBootTries = 0;
  }
  const cx = W / 2, cy = H / 2;
  const maxR = Math.min(W, H) / 2 - 36;   // rim-label margin: nothing clips the stage
  const coreR = Math.max(26, maxR * 0.17);
  // The radial band the proximity percentile maps onto: r=0 (best-evidenced)
  // lands just off the core disc, r=1 would land at the outer edge. LINEAR IN
  // r — r is already the rank statistic (robust-fit rule: percentiles, never
  // a min/max fit over raw scores, so a 900-weight hub cannot stretch the map).
  const rInner = coreR + Math.max(10, maxR * 0.06);
  const rOuter = maxR - 4;
  const radiusOf = r => rInner + (rOuter - rInner) * Math.max(0, Math.min(1, r));
  // a thin top wedge stays clear of dots so the radial guide sits on empty sky
  const TOP_GUTTER = 0.14;
  const start = -Math.PI / 2 + TOP_GUTTER / 2;
  const avail = 2 * Math.PI - TOP_GUTTER;
  const SMOOTH = 2;   // a 1-cell area still gets a visible wedge
  const wSum = perArea.reduce((s, A) => s + A.n + SMOOTH, 0) || 1;
  let a0 = start;
  for (const A of perArea) { A.a0 = a0; A.span = avail * (A.n + SMOOTH) / wSum; a0 += A.span; }
  // dot geometry adapts to the viewport. A tie group (cells sharing one exact
  // percentile — the zero-signal population shares ONE) stacks rows around its
  // shared radius, gap clamped small so the band stays a thin annulus.
  const ARC_STEP = Math.max(5.5, Math.min(8, maxR / 42));
  const DOT = maxR < 220 ? 2.2 : 2.6;
  const hash01 = s => {   // deterministic per-(area, radius) rotation — no RNG
    let h = 0;
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return (h % 9973) / 9973;
  };
  const leaves = placed.map(({it, A, G, j}) => {
    const R = radiusOf(G.rv);
    const perRow = Math.max(1, Math.floor((A.span * R) / ARC_STEP));
    const rows = Math.ceil(G.m / perRow);
    const gap = rows > 1 ? Math.min(4, (rOuter - rInner) * 0.02) : 0;
    const row = Math.floor(j / perRow);
    const inRow = row === rows - 1 ? G.m - row * perRow : perRow;
    // the hash rotation de-spokes single-dot groups: without it every area's
    // groups would open at the sector's start edge and draw a radial seam
    const frac = (((j % perRow) + 0.5) / inRow + hash01(A.p + "|" + G.rv)) % 1;
    const ang = A.a0 + frac * A.span;
    const rr = R + (row - (rows - 1) / 2) * gap;
    // arc length per dot in this row — the honest "does a label fit" test the
    // sector view renders labels by
    const fit = (A.span * rr) / Math.max(1, inRow) >= FRONTIER_LABEL_FIT_PX;
    return {data: it, x: cx + rr * Math.cos(ang), y: cy + rr * Math.sin(ang), r: DOT, fit};
  });
  // fvW/H: the stage this layout was solved FOR — the deferred self-check
  // below (and the stage ResizeObserver, where it delivers) re-render on drift
  layout = {items: new Map(leaves.map(l => [l.data.id, l])), leaves, frontier: true,
            fvW: W, fvH: H, fvCore: {cx, cy, coreR}, sector};
  edgeStore = [];
  gEdges.selectAll("*").remove();
  gOverlay.selectAll("*").remove();
  gBubbles.selectAll("circle.preview").remove();
  gBubbles.selectAll("circle.node").interrupt("rescore");   // stale moves must not fight this render
  gLabels.selectAll("*").remove();
  // the proximity axis renders UNDER the dots (gEdges is the bottom layer;
  // renderEdges() early-returns in the frontier view so nothing wipes it):
  // ONE faint radial guide up the top gutter — an axis, NOT shell rings
  gEdges.append("line")
    .attr("x1", cx).attr("y1", cy - rInner).attr("x2", cx).attr("y2", cy - rOuter)
    .attr("stroke", "#33405c").attr("stroke-opacity", 0.8)
    .attr("stroke-width", 1).attr("stroke-dasharray", "2 4");
  // a fat transparent twin makes the thin guide hoverable; its title is the
  // one-sentence proximity explanation (the SCHEMA contract's own wording)
  gEdges.append("line")
    .attr("x1", cx).attr("y1", cy - rInner).attr("x2", cx).attr("y2", cy - rOuter)
    .attr("stroke", "transparent").attr("stroke-width", 14)
    .append("title").text(
      "radius = formal proximity: the trace weight of a cell's bonds straight into " +
      "formalized cells, plus ¼ of what its frontier neighbors can bridge (each " +
      "bridge capped by both the bond and the neighbor's own direct evidence), " +
      "rank-mapped over all frontier cells — closer in = more formal evidence");
  const core = gEdges.append("g").style("cursor", "pointer")
    .on("click", ev => { ev.stopPropagation(); focusId = ROOTS_ID; selectedId = null;
      setHash(""); renderFocus(true); renderPanel(ROOTS_ID); });
  core.append("circle").attr("cx", cx).attr("cy", cy).attr("r", coreR)
    .attr("fill", CELL_FORMAL).attr("fill-opacity", 0.3)
    .attr("stroke", CELL_FORMAL).attr("stroke-opacity", 0.9).attr("stroke-width", 1.5);
  core.append("title").text(
    `the formalized interior — every cell with a Lean declaration in: ${
      en.length ? en.join(", ") : "(no library enabled)"}; click to open the library roots`);
  drawNodes();
  // a re-score ANIMATES the dots from their previous radii to the new ones
  // (drawNodes reuses each circle by id, so only cx/cy need to travel)
  if (oldPos) {
    gBubbles.selectAll("circle.node").each(function (l) {
      const o = oldPos.get(l.data.id);
      if (!o || (Math.abs(o[0] - l.x) < 0.5 && Math.abs(o[1] - l.y) < 0.5)) return;
      d3.select(this).attr("cx", o[0]).attr("cy", o[1])
        .transition("rescore").duration(650).ease(d3.easeCubicInOut)
        .attr("cx", l.x).attr("cy", l.y);
    });
    // rAF-driven transitions pause in hidden/background panes (the fadeIn /
    // zoomInto guard) — a dot must NEVER stay stranded at its old radius, so
    // snap everything to its final position once the window has passed
    setTimeout(() => {
      if (seq !== renderSeq || !layout || !layout.frontier) return;
      gBubbles.selectAll("circle.node").interrupt("rescore")
        .attr("cx", l => l.x).attr("cy", l => l.y);
    }, 800);
  }
  // core label + axis pole labels + sector rim labels — inked with a dark
  // outline so they stay readable wherever they land over dots
  const inked = t => t.attr("stroke", "#0b0e14").attr("stroke-width", 3)
    .attr("paint-order", "stroke").attr("stroke-linejoin", "round");
  // the core disc names what "formal" currently MEANS: the one enabled library,
  // both of two, or the count — and how many the reader switched off
  const coreLbl = en.length === 1 ? en[0]
    : en.length === 2 ? `${en[0]} + ${en[1]}`
    : `${en.length} libraries`;
  const coreFs = Math.max(8.5, Math.min(Math.max(12, coreR * 0.3),
    (2 * coreR - 6) / (0.6 * Math.max(1, coreLbl.length))));
  inked(gLabels.append("text").attr("class", "blabel")
    .attr("x", cx).attr("y", cy - 3).attr("font-size", coreFs)
    .text(coreLbl));
  inked(gLabels.append("text").attr("class", "bcount")
    .attr("x", cx).attr("y", cy + Math.max(11, coreR * 0.26))
    .attr("font-size", Math.max(9, coreR * 0.2))
    .text(libsFiltered() ? `${disabledLibs.size} off` : "formal interior"));
  // the axis poles: what closest and outermost MEAN, in the gutter's empty sky
  inked(gLabels.append("text").attr("class", "bcount")
    .attr("x", cx).attr("y", cy - rInner - 5).attr("font-size", 9.5)
    .text(`strongest formal evidence · ${nDirect.toLocaleString()} bonded`));
  inked(gLabels.append("text").attr("class", "bcount")
    .attr("x", cx).attr("y", cy - rOuter + 12).attr("font-size", 9.5)
    .text(`no formal signal · ${nZero.toLocaleString()}`));
  // rim labels: full view only (a sector IS the whole circle) — clicking one
  // focuses that sector (#__frontier__:<Area>); background-click returns
  if (!sector) perArea.slice(0, FRONTIER_RIM_LABELS).forEach(A => {
    const mid = A.a0 + A.span / 2;
    const t = gLabels.append("text").attr("class", "blabel rimlab")
      .attr("x", cx + (maxR + 12) * Math.cos(mid))
      .attr("y", cy + (maxR + 12) * Math.sin(mid) + 3)
      .attr("font-size", 10)
      .style("text-anchor", Math.cos(mid) > 0.25 ? "start"
        : Math.cos(mid) < -0.25 ? "end" : "middle")
      .text(frontierName(A.p))
      // map mode stays in map mode: a rim label focuses the SECTOR on the map
      // (#__frontier__:map:<Area>); the queue's area filter is the list's own
      .on("click", ev => { ev.stopPropagation();
        gotoFrontierView(FRONTIER_MAP_ID + ":" + A.p.slice(9)); });
    inked(t);
    const nRaw = ((tree.sc[A.p] || {}).cells || []).length;
    t.append("title").text(`focus this sector — ${frontierName(A.p)}'s cells alone, ` +
      `spread over the full circle (${A.n === nRaw ? `${A.n.toLocaleString()} cells`
        : `${A.n.toLocaleString()} of ${nRaw.toLocaleString()} cells shown`})`);
  });
  drawFrontierDotLabels(leaves, sector);
  drawSelRing();
  webState = {shown: 0, cells: totalCells, capped: false};
  applyFrontierScale(d3.zoomTransform(svg.node()).k || 1);   // re-scores keep the reader's zoom
  if (anim) fadeIn();
  // Safety net for any OTHER late reflow (scrollbars, panel content, chrome):
  // if the stage the layout was solved for is no longer the stage on screen,
  // re-solve once against the settled one. Converges — the second pass writes
  // identical chrome, so the stage cannot move again.
  setTimeout(() => {
    if (!layout || !layout.frontier || !isFrontierViewId(focusId)) return;
    if (Math.abs(stageEl.clientWidth - layout.fvW) < 2 &&
        Math.abs(stageEl.clientHeight - layout.fvH) < 2) return;
    renderFocus(false);
  }, 120);
}
// ---- frontier dot labels + screen-space zoom scaling ------------------------
// Dot labels are ANNOTATION (the radius is the geometry): font pinned to the
// screen, visibility budgeted by zoom — exactly the updateLevelLabels / explorer
// zoom^2-budget design. The sector view additionally renders every label
// whose row spacing FITS at rest; the full view reveals labels as you zoom.
const FRONTIER_LABEL_FIT_PX = 46;   // arc per dot at which a label fits at k=1
const FRONTIER_LABEL_BUDGET = 14;   // ranked labels at k=1 (sector: 36); grows with k²
const FRONTIER_SECTOR_BUDGET = 36;
function drawFrontierDotLabels(leaves, sector) {
  // rank: fitting labels first (the sector view shows them immediately), then
  // best-evidenced (proximity r asc), then label — deterministic, so the map
  // can be learned
  const fitRank = l => sector && l.fit ? 0 : 1;
  const rk = l => (l.data.px && l.data.px.r !== undefined) ? l.data.px.r : 2;
  const ranked = leaves.slice().sort((a, b) =>
    (fitRank(a) - fitRank(b)) ||
    (rk(a) - rk(b)) ||
    String(a.data.label || a.data.id).localeCompare(String(b.data.label || b.data.id)));
  const ink = t => t.attr("stroke", "#0b0e14").attr("stroke-width", 3)
    .attr("paint-order", "stroke").attr("stroke-linejoin", "round");
  // the best-evidenced dots hug the core disc, and a top-of-ring dot's label
  // descends INTO it — over the core's own name. Skip any label whose anchor
  // lands on the disc (the dot keeps its hover title; the panel lists the top
  // cells) — measured: without this the top-14 budget stacks 4 labels over
  // "N libraries" at rest zoom.
  const cv = layout.fvCore;
  let i = 0;
  for (const l of ranked) {
    const raw = l.data.label || l.data.id;
    const shown = raw.length > 26 ? raw.slice(0, 24) + "…" : raw;
    const ly = l.y + (l.r || 2.2) + 8;
    // the label is centre-anchored: test the point of its approximate text box
    // nearest the disc, not just the anchor (≈2.7px half-width per char at 9.5px)
    const nx = Math.max(0, Math.abs(l.x - cv.cx) - shown.length * 2.7);
    if (Math.hypot(nx, ly - cv.cy) < cv.coreR + 12) continue;
    ink(gLabels.append("text").attr("class", "blabel hlab")
      .attr("x", l.x).attr("y", ly).attr("data-rank", i++)
      .text(shown));
  }
  layout.fvFitN = sector ? leaves.filter(l => l.fit).length : 0;
}
// Dots grow with √k (visibly bigger as you dive, but the k-growing spacing
// still separates overlaps); labels hold a constant screen size while their
// budget grows with k² — zooming DE-CLUTTERS, as everywhere else in v3.
function applyFrontierScale(k) {
  if (!layout || !layout.frontier) return;
  lastK = k || 1;
  const rr = 1 / Math.sqrt(lastK);
  gBubbles.selectAll("circle.dot.node").attr("r", l => (l.r || 2.2) * rr);
  updateFrontierLabels(lastK);
}
function updateFrontierLabels(k) {
  const sel = gLabels.selectAll("text.hlab");
  const n = sel.size();
  if (!n) return;
  const budget = layout.sector ? FRONTIER_SECTOR_BUDGET : FRONTIER_LABEL_BUDGET;
  const lim = Math.min(n, Math.max(layout.fvFitN || 0, Math.round(budget * k * k)));
  sel.attr("display", function () { return Number(this.dataset.rank) < lim ? null : "none"; })
     .attr("font-size", 9.5 / (k || 1))
     .attr("stroke-width", 3 / (k || 1));
}
zoomBehav.on("zoom.frontier", ev => {
  if (layout && layout.frontier) applyFrontierScale(ev.transform.k);
});
// ---- the frontier QUEUE (#__frontier__, the DEFAULT frontier surface) -------
// A virtualized ranked table of EVERY frontier cell passing the shared (V)
// predicate — an HTML overlay in the stage (the explorer's xcanvasShow
// precedent: a non-SVG surface swaps in over the empty SVG). Scoring is
// activeProxFor — the SAME path the map and the cell cards read (shipped prox
// all-on, scoreCells re-score under a library subset; the parity law rides
// along untouched). Rows are WINDOWED: the DOM holds only the viewport slice,
// the scrollbar + the "N concepts" total prove the full set, and nothing is
// ever dropped silently (every narrowing is counted in the header + status).
const FL_ROW_H = 34;      // fixed row height — the windowing arithmetic's anchor
const FL_OVERSCAN = 8;    // rows rendered beyond each viewport edge
// evidence badges, one per f-bit the labels rows carry (contract order; the
// toolbar chips' own bit table — there is NO Wikipedia-sitelink bit, so the
// article badge is the annotated WIKILEAN article organ, named honestly)
const EVIDENCE_BADGES = [
  [64,    "WikiLean article", "#7cb3ff", "an annotated WikiLean article organ"],
  [1024,  "nLab",             "#4ade80", "an nLab page organ (Wikidata P4215)"],
  [2048,  "MathWorld",        "#f87171", "a MathWorld page organ (Wikidata P2812)"],
  [4096,  "ProofWiki",        "#60a5fa", "a ProofWiki page organ (Wikidata P6781)"],
  [512,   "LMFDB",            "#facc15", "an LMFDB knowl organ (Wikidata P12987)"],
  [16384, "OEIS",             "#a3e635", "an OEIS sequence organ (Wikidata P829)"],
  [128,   "literature",       "#fb923c", "cited in / matched to the literature (\u22651 cites or matches bond \u2014 TheoremGraph)"],
];
function evidenceKindCount(f) {
  let n = 0;
  for (const [b] of EVIDENCE_BADGES) if (f & b) n++;
  return n;
}
const SUITABILITY_LABELS = {
  existing_formal_coverage: "coverage exists",
  not_formalization_target: "not a formal target",
  broad_scope: "scope too broad",
  ambiguous_scope: "scope unclear",
  too_elementary: "too elementary",
  review_needed: "needs review",
  no_concept_target: "no concept target",
};
function suitabilityFor(cid) {
  return tree.cellSuitability.get(cid) || {candidate: false, reason: "review_needed"};
}
// queue state — session-local (sort/query/multi-area); a SINGLE selected area
// also rides the hash as #__frontier__:<Area> so old sector links deep-link in
let flSort = "readiness";   // "readiness" | "evidence" | "az"
let flQuery = "";
let flAreas = new Set();    // selected frontier:<Area> ids; empty = all areas
let flRows = [];            // the CURRENT sorted+filtered contract rows
let flUniverseN = 0;        // every prox-scored frontier cell (the honest M)
let flActive = -1;          // keyboard cursor (index into flRows)
let flWindowLo = -1, flWindowHi = -1, flWindowPending = false, flQT = 0;
window.__flstats = {rows: 0, universe: 0, assembleMs: 0, sortMs: 0, windowMs: 0,
                    rowsInDom: 0, firstPaintMs: 0};
function flistShow(on) {
  const el = $("#flist");
  if (!el) return;
  el.style.display = on ? "block" : "none";
  if (!on && el.innerHTML) {
    el.innerHTML = "";
    el.scrollTop = 0;
    flRows = []; flActive = -1; flWindowLo = flWindowHi = -1;
  }
}
// the list|map toggle (the destroyed areas|halo #viewtoggle precedent):
// visible only on the frontier surfaces, state = the hash id
function updateFrontierToggle() {
  const el = $("#fviewtoggle");
  if (!el) return;
  const on = tree && tree.prox && isFrontierViewId(focusId) && !explorerOn;
  el.style.display = on ? "flex" : "none";
  if (!on) return;
  const v = frontierViewOf(focusId) || {mode: "list"};
  $("#fv-list").classList.toggle("on", v.mode === "list");
  $("#fv-map").classList.toggle("on", v.mode === "map");
}
async function renderFrontierList(seq, anim) {
  const t0 = performance.now();
  // same lazy-graph discipline as the map: a filtered library set needs the
  // frontier graph BEFORE scoring; all-on renders the shipped prox immediately
  const graphP = fetchFrontierGraph();
  if (libsFiltered() && !fgraph && !fgraphFail) {
    await graphP;
    if (seq !== renderSeq) return;
  }
  if (libsFiltered()) ensureClientProx();
  // a hash-carried area (#__frontier__:<Area> — every old sector link) seeds
  // the chip selection; multi-select beyond one area is session state
  const v = frontierViewOf(focusId) || {mode: "list", area: null};
  if (v.area && !flAreas.has(v.area)) flAreas = new Set([v.area]);
  for (const a of [...flAreas]) if (!((tree.sc[a] || {}).frontier)) flAreas.delete(a);
  // the queue owns the stage: no SVG scene behind it, no stale selection ring
  layout = {items: new Map(), leaves: [], frontierList: true};
  edgeStore = [];
  gEdges.selectAll("*").remove();
  gOverlay.selectAll("*").remove();
  gBubbles.selectAll("*").remove();
  gLabels.selectAll("*").remove();
  const el = $("#flist");
  // an in-place re-render (re-score, resize, facet reshape) keeps the reader's
  // scroll; a fresh entry starts at the top (flistShow(false) reset it to 0)
  const prevScroll = el.style.display !== "none" ? el.scrollTop : 0;
  el.innerHTML = flHeaderHtml();
  flistShow(true);
  wireFlHeader();
  flRebuildRows(false);
  el.scrollTop = prevScroll;
  renderCrumb();   // crumb + the list|map toggle (synchronous on this path)
  const stat = $("#structstat");
  if (stat) stat.textContent =
    "actionable candidates first — proximity client-scored within each tier";
  webState = {shown: 0, cells: flRows.length, capped: false};
  // keyboard: the stage focus ring is the list itself (arrows + enter)
  if (document.activeElement === document.body) el.focus({preventScroll: true});
  window.__flstats.firstPaintMs = performance.now() - t0;
}
function flHeaderHtml() {
  const sorts = [["readiness", "readiness",
      "actionable candidates first, then formal proximity (score desc, ties by id)"],
    ["evidence", "evidence",
      "actionable candidates first, then distinct evidence kinds and proximity"],
    ["az", "A–Z", "actionable candidates first, then label alphabetically"]];
  let h = `<div id="flhead"><div id="flctl"><span id="fltotal"></span>
    <span class="fgrouplabel">sort:</span>`;
  for (const [k, lbl, why] of sorts)
    h += `<button class="fchip flsort${flSort === k ? " on" : ""}" data-sort="${k}"
      title="${esc(why)}">${esc(lbl)}</button>`;
  h += `<input id="flq" type="search" placeholder="filter by name or aka…"
    value="${esc(flQuery)}" autocomplete="off"></div><div id="flareas"></div>
    <div class="flcols"><span style="text-align:right">#</span><span>concept</span>
    <span>area</span>
    <span title="bar = formal-proximity percentile over the whole frontier (client-scored under the current library set); hover a row's bar for the direct + bridged breakdown">proximity</span>
    <span title="which evidence organs the cell carries — hover a dot to name it">evidence</span>
    <span title="candidate or the reason this row needs review before it is treated as a formalization task">assessment</span>
    <span title="the area's nearest formal home — the library module a formalization would most likely land in">nearest formal home</span></div></div>
    <div id="flbody"></div>`;
  return h;
}
function flAreaChipsHtml() {
  // tree.frontier is already (size desc, id) — the contract's chip order
  let h = flAreas.size
    ? `<button class="fchip on" id="flareaclear" title="clear the area filter">× clear (${flAreas.size} area${flAreas.size > 1 ? "s" : ""})</button>`
    : "";
  for (const p of tree.frontier) {
    const nAll = ((tree.sc[p] || {}).cells || []).length;
    const nv = filtersActive() ? countVisible(p) : nAll;
    const on = flAreas.has(p);
    h += `<button class="fchip flareachip${on ? " on" : ""}" data-area="${esc(p)}"
      title="${esc(frontierName(p))} — ${nv !== nAll
        ? `${nv.toLocaleString()} of ${nAll.toLocaleString()} cells shown`
        : `${nAll.toLocaleString()} cells`} · click to ${on ? "remove from" : "add to"} the area filter">${
      esc(frontierName(p))} <small>${nv !== nAll ? `${nv}/${nAll}` : nAll}</small></button>`;
  }
  return h;
}
function wireFlHeader() {
  const el = $("#flist");
  el.querySelectorAll(".flsort").forEach(b => b.addEventListener("click", () => {
    if (flSort === b.dataset.sort) return;
    flSort = b.dataset.sort;
    el.querySelectorAll(".flsort").forEach(x =>
      x.classList.toggle("on", x.dataset.sort === flSort));
    flRebuildRows(true);
  }));
  const qi = $("#flq");
  if (qi) qi.addEventListener("input", () => {
    clearTimeout(flQT);
    flQT = setTimeout(() => { flQuery = qi.value; flRebuildRows(true); }, 120);
  });
  flSyncAreaChips();
}
function flSyncAreaChips() {
  const strip = $("#flareas");
  if (!strip) return;
  strip.innerHTML = flAreaChipsHtml();
  strip.querySelectorAll(".flareachip").forEach(b =>
    b.addEventListener("click", () => flToggleArea(b.dataset.area)));
  const clr = $("#flareaclear");
  if (clr) clr.addEventListener("click", () => {
    flAreas = new Set();
    flSyncFocusHash();
    flRebuildRows(true);
    flSyncAreaChips();
  });
}
function flToggleArea(p) {
  if (flAreas.has(p)) flAreas.delete(p); else flAreas.add(p);
  flSyncFocusHash();
  flRebuildRows(true);
  flSyncAreaChips();
}
// ONE selected area is shareable state (#__frontier__:<Area>); zero or many
// collapse the hash to the full queue — the chips carry the rest visibly
function flSyncFocusHash() {
  focusId = flAreas.size === 1
    ? FRONTIER_ID + ":" + [...flAreas][0].slice(9) : FRONTIER_ID;
  setHash(focusId);
  renderCrumb();
  // the panel tracks the view like every other travel path — unless a cell
  // card is open (then the reader is mid-inspection; leave it alone)
  if (isFrontierViewId(lastPanelId)) renderPanel(focusId);
}
// assemble + sort + window the contract rows under the CURRENT state. Every
// narrowing is COUNTED (skipped prox drift, predicate-hidden, area filter,
// quick filter) — the header + status always name N of M.
function flRebuildRows(resetScroll) {
  const el = $("#flist"), body = $("#flbody");
  if (!el || !body) return;
  const tA = performance.now();
  const q = flQuery.trim().toLowerCase();
  let universe = 0, skipped = 0, facetHidden = 0;
  const rows = [];
  for (const p of tree.frontier) {
    const sc = tree.sc[p] || {};
    const near = sc.near || null;
    const aname = frontierName(p);
    const areaOff = flAreas.size > 0 && !flAreas.has(p);
    for (const cid of sc.cells || []) {
      const px = activeProxFor(cid);
      if (!px) { skipped++; continue; }   // no prox data — counted, never silent
      universe++;
      // (H) the shared (V) predicate governs membership — the library set acts
      // on homeless cells through the re-SCORE (activeProxFor), never removal
      if (!cellVisible(cid)) { facetHidden++; continue; }
      if (areaOff) continue;   // counted via universe vs rows.length below
      const r = labelById.get(cid) || {};
      if (q) {
        const lbl = r.label || cid;
        if (!lbl.toLowerCase().includes(q) &&
            !(r.aka || []).some(a => a.toLowerCase().includes(q))) continue;
      }
      const suitability = suitabilityFor(cid);
      rows.push({cid, label: r.label || cid, f: r.f || 0, area: p, aname, near,
                 px, suitability, ek: evidenceKindCount(r.f || 0)});
    }
  }
  const tS = performance.now();
  // deterministic ties everywhere: score desc, then id (contract H)
  const byId = (a, b) => (a.cid < b.cid ? -1 : a.cid > b.cid ? 1 : 0);
  const bySuitability = (a, b) =>
    Number(b.suitability.candidate) - Number(a.suitability.candidate);
  if (flSort === "az")
    rows.sort((a, b) => bySuitability(a, b) || a.label.localeCompare(b.label) || byId(a, b));
  else if (flSort === "evidence")
    rows.sort((a, b) => bySuitability(a, b) || (b.ek - a.ek) ||
      (b.px.s - a.px.s) || byId(a, b));
  else
    rows.sort((a, b) => bySuitability(a, b) || (b.px.s - a.px.s) || byId(a, b));
  const tW = performance.now();
  flRows = rows;
  flUniverseN = universe;
  if (flActive >= rows.length) flActive = -1;
  body.style.height = (rows.length * FL_ROW_H) + "px";
  const tot = $("#fltotal");
  const candidateN = rows.filter(row => row.suitability.candidate).length;
  const reviewN = rows.length - candidateN;
  if (tot) tot.textContent = `${rows.length === universe
    ? universe.toLocaleString()
    : `${rows.length.toLocaleString()} of ${universe.toLocaleString()}`} concepts · ` +
    `${candidateN.toLocaleString()} candidates · ${reviewN.toLocaleString()} review needed`;
  // chrome: the same honesty surfaces every frontier render writes
  updateFilterStat({active: !!filterMask, shown: universe - facetHidden, total: universe});
  updateHiddenChip(facetHidden);
  const en = enabledLibs();
  const libNote = !libsFiltered() ? ""
    : clientProx
      ? ` · libraries: ${en.length === 0 ? "none"
          : en.length <= 3 ? en.join(" + ")
          : `${en.length} of ${libRoots().length}`}`
      : " · ⚠ library filter inactive (frontier_graph.json unavailable)";
  const parityNote = parity.ran && !parity.ok
    ? " · ⚠ client scores disagree with the build (see console)" : "";
  statusEl.textContent = `${rows.length.toLocaleString()}${rows.length !== universe
      ? ` of ${universe.toLocaleString()} cells shown` : " cells"} · frontier queue · ` +
    `${candidateN.toLocaleString()} actionable first · sorted within tiers by ${
      flSort === "az" ? "name"
      : flSort === "evidence" ? "evidence breadth, then proximity"
      : "formal proximity"}` +
    (skipped > 0 ? ` · ${skipped} cells lack proximity data (drift — see console)` : "") +
    libNote + parityNote;
  if (skipped > 0)
    console.warn(`[brain frontier] ${skipped} frontier cell(s) missing from prox — ` +
      `the queue lists only what this build's prox arrays cover`);
  if (resetScroll) el.scrollTop = 0;
  flWindowLo = flWindowHi = -1;   // force a fresh window over the new rows
  flWindow(true);
  const st = window.__flstats;
  st.rows = rows.length; st.universe = universe;
  st.assembleMs = tS - tA; st.sortMs = tW - tS;
}
// ---- windowed rendering: the viewport slice only, absolute-positioned -------
function flWindow(force) {
  const el = $("#flist"), body = $("#flbody"), head = $("#flhead");
  if (!el || !body) return;
  const t0 = performance.now();
  const headH = head ? head.offsetHeight : 0;
  const st = el.scrollTop, vh = el.clientHeight || 600;
  const lo = Math.max(0, Math.floor((st - headH) / FL_ROW_H) - FL_OVERSCAN);
  const hi = Math.min(flRows.length, Math.ceil((st - headH + vh) / FL_ROW_H) + FL_OVERSCAN);
  if (!force && lo === flWindowLo && hi === flWindowHi) return;
  flWindowLo = lo; flWindowHi = hi;
  let h = "";
  for (let i = lo; i < hi; i++) h += flRowHtml(flRows[i], i);
  body.innerHTML = h;
  body.querySelectorAll(".flrow").forEach(rEl => rEl.addEventListener("click", ev => {
    const areaBtn = ev.target.closest(".flareabtn");
    if (areaBtn) { flToggleArea(areaBtn.dataset.area); return; }
    const nav = ev.target.closest("[data-nav]");
    if (nav) { navigate(nav.dataset.nav); return; }
    flOpenRow(Number(rEl.dataset.i));
  }));
  const stx = window.__flstats;
  stx.rowsInDom = hi - lo;
  stx.windowMs = performance.now() - t0;
}
let flWindowTimer = 0;
function scheduleFlWindow() {
  if (flWindowPending) return;
  flWindowPending = true;
  const run = () => {
    if (!flWindowPending) return;
    flWindowPending = false;
    clearTimeout(flWindowTimer);
    flWindow(false);
  };
  requestAnimationFrame(run);
  // rAF pauses in hidden tabs/panes (the scheduleXDraw trap) — a timer keeps a
  // programmatic scroll from leaving the window stale; still one coalesced pass
  flWindowTimer = setTimeout(run, 120);
}
function flRowHtml(row, i) {
  const px = row.px;
  const pct = px.r !== undefined ? Math.max(0, Math.min(1, 1 - px.r)) : 0;
  const badges = EVIDENCE_BADGES.filter(([b]) => row.f & b).map(([b, name, col, why]) =>
    `<i style="background:${col}" title="${esc(name + " — " + why)}"></i>`).join("");
  const near = row.near
    ? `<a data-nav="${esc(row.near)}" title="${esc(`the ${row.aname} frontier's nearest formal home — the library area its cells' synapse neighborhoods vote for`)}">${esc(row.near.slice(5))}</a>`
    : `<span style="color:#556074" title="no formal home votes for this area (DeepFrontier/Unsorted)">—</span>`;
  const assessment = row.suitability.candidate
    ? {label: "candidate", detail: "a bounded gap according to current Brain metadata"}
    : {label: SUITABILITY_LABELS[row.suitability.reason] || "needs review",
       detail: "deprioritized: " + (SUITABILITY_LABELS[row.suitability.reason] || row.suitability.reason)};
  const ptitle = `formal proximity ${(+px.s).toLocaleString()} — ${proxSummary(px)}` +
    (px.r !== undefined
      ? ` · closer to formal code than ${Math.floor((1 - px.r) * 100)}% of the frontier` : "");
  return `<div class="flrow${row.cid === selectedId ? " sel" : ""}${i === flActive ? " act" : ""}${row.suitability.candidate ? "" : " deprioritized"}"
      style="top:${i * FL_ROW_H}px" data-i="${i}" data-cid="${esc(row.cid)}">
    <span class="flrank">${(i + 1).toLocaleString()}</span>
    <span class="fllabel" title="${esc(row.label)} — click for the cell's card">${esc(row.label)}</span>
    <button class="fchip flareabtn${flAreas.has(row.area) ? " on" : ""}" data-area="${esc(row.area)}"
      title="area: ${esc(row.aname)} — click to toggle the area filter">${esc(row.aname)}</button>
    <span class="flprox" title="${esc(ptitle)}"><i style="width:${(pct * 100).toFixed(1)}%"></i></span>
    <span class="flev">${badges || `<small style="color:#556074">none</small>`}</span>
    <span class="flsuit ${row.suitability.candidate ? "candidate" : "deprioritized"}"
      title="${esc(assessment.detail)}">${esc(assessment.label)}</span>
    <span class="flnear">${near}</span></div>`;
}
// a row opens the cell's EXISTING side-panel card (renderPanel → the cell card
// with its organs, snippets, synapses — and the Build-on-these section)
function flOpenRow(i) {
  const row = flRows[i];
  if (!row) return;
  flActive = i;
  selectedId = row.cid;
  renderPanel(row.cid);
  flWindow(true);   // refresh the .sel/.act row highlights
}
function flEnsureVisible(i) {
  const el = $("#flist"), head = $("#flhead");
  if (!el) return;
  const headH = head ? head.offsetHeight : 0;
  const top = headH + i * FL_ROW_H, bot = top + FL_ROW_H;
  if (top < el.scrollTop + headH) el.scrollTop = top - headH;
  else if (bot > el.scrollTop + el.clientHeight) el.scrollTop = bot - el.clientHeight;
}
$("#flist").addEventListener("scroll", () => {
  if (layout && layout.frontierList) scheduleFlWindow();
});
$("#flist").addEventListener("keydown", ev => {
  if (!layout || !layout.frontierList || !flRows.length) return;
  if (ev.target && ev.target.id === "flq" && ev.key !== "Escape") return;
  if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
    ev.preventDefault();
    const d = ev.key === "ArrowDown" ? 1 : -1;
    flActive = flActive < 0 ? (d > 0 ? 0 : flRows.length - 1)
      : Math.max(0, Math.min(flRows.length - 1, flActive + d));
    flEnsureVisible(flActive);
    flWindow(true);
  } else if (ev.key === "Enter" && flActive >= 0) {
    ev.preventDefault();
    flOpenRow(flActive);
  }
});
// searching while in the frontier view keeps the view for hits that are ON it:
// spotlight-pulse the dot + open its card. Anything not on the current view
// (areas, formalized cells, another sector's cells) navigates exactly as today.
function spotlightDot(id) {
  const L = layout && layout.items.get(id);
  if (!L) return;
  const t = d3.zoomTransform(svg.node()), k = t.k || 1;
  // a pan/zoom may have left the dot off screen — bring it back at the same zoom
  const sx = t.applyX(L.x), sy = t.applyY(L.y);
  const W = stageEl.clientWidth, H = stageEl.clientHeight;
  if (sx < 20 || sx > W - 20 || sy < 20 || sy > H - 20)
    svg.transition().duration(420).call(zoomBehav.transform,
      d3.zoomIdentity.translate(W / 2 - k * L.x, H / 2 - k * L.y).scale(k));
  for (let i = 0; i < 3; i++)
    gOverlay.append("circle")
      .attr("cx", L.x).attr("cy", L.y).attr("r", 4 / k)
      .attr("fill", "none").attr("stroke", "#38bdf8").attr("stroke-width", 2.4 / k)
      .transition().delay(140 + i * 300).duration(950).ease(d3.easeCubicOut)
      .attr("r", 40 / k).attr("stroke-opacity", 0)
      .remove();
}
async function searchGo(rawId) {
  if (layout && layout.frontier) {
    const id = await resolveId(rawId);
    if (layout && layout.frontier && isCellId(id) && layout.items.has(id)) {
      selectedId = id;
      drawSelRing();
      spotlightDot(id);
      renderPanel(id);
      return;
    }
  }
  navigate(rawId);
}
// ---- the Libraries control (root panel + frontier panel, ONE component) -----
// ONE re-score path for BOTH frontier surfaces (never fork scoring): the queue
// re-assembles + re-ranks its rows through the same activeProxFor the map's
// dots read; the map animates its dots to the new radii.
async function reScoreFrontier() {
  const seq = ++renderSeq;
  if (layout && layout.frontierList) {
    await renderFrontierList(seq, false);   // in place: rows re-rank, scroll kept
    return;
  }
  await renderFrontier(seq, false, true);   // instant: no refetch, no zoom reset; dots animate
}
async function setLibEnabled(name, on) {
  if (on) disabledLibs.delete(name); else disabledLibs.add(name);
  await filtersChanged();   // the ONE path — every filter mutation reshapes in place
}
function librariesSectionHtml() {
  const all = libRoots();
  let html = `<section class="kind" id="libsec"><h3>Library filter <span class="cnt">(${
    all.length - disabledLibs.size} of ${all.length} on)</span></h3>
    <p class="note">Which libraries count as <b>formal code</b>. Turning one off removes
    its root bubble and every cell it places from the map, panels and search (the
    <b>+N hidden</b> chip in the toolbar brings you back here), and re-scores the
    frontier without it: a cell's formal proximity counts only bonds and bridges
    reaching declarations in the libraries still on, re-ranked with the build's own
    formula (with every library on, the view is exactly the shipped
    placement).</p><div class="libctl">`;
  for (const n of all)
    html += `<label class="librow"><input type="checkbox" class="libcb" data-lib="${esc(n)}"${
      disabledLibs.has(n) ? "" : " checked"}> ${esc(n)}
      <small>${tree.count("path:" + n).toLocaleString()} cells</small></label>`;
  html += `</div></section>`;
  return html;
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
  const nodes = [...layout.items.values()];   // the layout holds only (V)-visible items
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
  // the explorer's ring is painted by the canvas frame (screen-space, so it is
  // visible at ANY zoom — the old SVG ring drew sub-pixel at the resting k)
  if (layout && layout.explorer) { scheduleXDraw(); return; }
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
  const core = id && id !== ROOTS_ID ? encodeURIComponent(id) : "";
  let extra = "";
  if (filterMask) extra += "&f=" + filterMask;
  if (explorerOn) extra += "&view=explorer";
  // lib names match ^[A-Za-z][A-Za-z0-9_]*$ — raw in the hash by construction
  if (tree && libsFiltered()) extra += "&libs=" + enabledLibs().join(",");
  // (S) an empty id segment beside params ("#&f=16") cold-loads to the DEFAULT
  // dive (path:Mathlib), losing the roots view the params were set on — pin the
  // roots id explicitly whenever there is state that must survive a reload
  history.replaceState(null, "", "#" + (core || (extra ? ROOTS_ID : "")) + extra);
}
function parseHash() {
  const parts = location.hash.slice(1).split("&");
  let id = parts[0] || "";
  try { id = decodeURIComponent(id); } catch (e) { /* malformed — keep raw */ }
  const out = {id, f: 0, view: "", flat: false, libs: null};
  for (const kv of parts.slice(1)) {
    const i = kv.indexOf("=");
    const k = i < 0 ? kv : kv.slice(0, i), v = i < 0 ? "" : kv.slice(i + 1);
    if (k === "f") out.f = (parseInt(v, 10) || 0) & 0xffff;
    else if (k === "view") out.view = v;
    else if (k === "flat" && v !== "0") out.flat = true;
    else if (k === "libs") {   // the ENABLED list; "&libs=" (empty) = all off
      try { out.libs = v === "" ? [] : decodeURIComponent(v).split(",").filter(Boolean); }
      catch (e) { out.libs = null; }
    }
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

// Explicit travel to the FULL frontier drops the queue's session-local area
// multi-select (the chips' state): the hash is the state that survives travel,
// and it can carry at most ONE area — landing on #__frontier__ and silently
// keeping a stale area filter would show a filtered list under a full-view
// hash. Re-renders (re-score, resize, facet reshape) never pass through these
// entry points, so live narrowing keeps its chips.
function flClearAreasOnFullTravel(id) {
  if (id === FRONTIER_ID) flAreas = new Set();
}

async function zoomInto(id) {
  flClearAreasOnFullTravel(id);
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
// no decl organ has no breadcrumb at all — its home is its frontier AREA (the
// partition claims every homeless cell), or the unfiled residue bucket.
function homeOf(entry) {
  const bc = entry.breadcrumb || [];
  if (bc.length) return bc[bc.length - 1].id;
  const area = tree && tree.cellArea && tree.cellArea.get(entry.cell.id);
  return area || UNPLACED_ID;
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
  const fvUp = isSectorId(focusId) ? frontierViewOf(focusId) : null;
  const parent = fvUp   // map sector → full map; map → roots; area queue → full queue
    ? (fvUp.mode === "map" ? (fvUp.area ? FRONTIER_MAP_ID : ROOTS_ID) : FRONTIER_ID)
    : focusId === FRONTIER_ID ? ROOTS_ID
    : isFrontierId(focusId) ? FRONTIER_ID
    : focusId === UNPLACED_ID ? (tree.frontier.length ? FRONTIER_ID : ROOTS_ID)
    : focusId.startsWith(STRAYS_PREFIX) ? focusId.slice(STRAYS_PREFIX.length)
    : ((tree.sc[focusId] || {}).parent || ROOTS_ID);
  flClearAreasOnFullTravel(parent);   // zoom-out is full travel: stale area chips clear
  focusId = parent;
  selectedId = null;
  setHash(parent);
  await renderFocus(true);
  renderPanel(parent);   // panel follows the zoom — no stale supercell at the root
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
  if (item.type === "strays" || item.type === "folder") {
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
  flClearAreasOnFullTravel(id);
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
  if (isSectorId(focusId)) {
    const v = frontierViewOf(focusId);
    html += ` <span class="sep">/</span> <a data-nav="${FRONTIER_ID}">Frontier</a>`;
    if (v.mode === "map")
      html += v.area
        ? ` <span class="sep">/</span> <a data-nav="${FRONTIER_MAP_ID}">map</a>` +
          ` <span class="sep">/</span> <b>${esc(frontierName(v.area))} sector</b>`
        : ` <span class="sep">/</span> <b>map</b>`;
    else
      html += ` <span class="sep">/</span> <b>${esc(frontierName(v.area))} · queue</b>`;
  } else if (focusId === FRONTIER_ID) {
    html += ` <span class="sep">/</span> <b>Frontier</b>`;
  } else if (isFrontierId(focusId)) {
    html += ` <span class="sep">/</span> <a data-nav="${FRONTIER_ID}">Frontier</a>` +
      ` <span class="sep">/</span> <b>${esc(frontierName(focusId))}</b>`;
  } else if (focusId === UNPLACED_ID) {
    html += tree.frontier.length
      ? ` <span class="sep">/</span> <a data-nav="${FRONTIER_ID}">Frontier</a>` +
        ` <span class="sep">/</span> <b>unfiled</b>`
      : ` <span class="sep">/</span> <b>no formal home</b>`;
  } else if (focusId.startsWith(STRAYS_PREFIX)) {
    const parent = focusId.slice(STRAYS_PREFIX.length);
    for (const b of pathChain(parent))
      html += ` <span class="sep">/</span> <a data-nav="${esc(b.id)}">${esc(b.label)}</a>`;
    html += ` <span class="sep">/</span> <b>${parent.slice(5).includes("/")
      ? "filed here" : "no module recorded"}</b>`;
  } else if (isCellId(focusId)) {
    const e = await getEntry(focusId);
    for (const b of (e && e.breadcrumb) || [])
      html += ` <span class="sep">/</span> <a data-nav="${esc(b.id)}">${esc(b.label)}</a>`;
    if (e && !(e.breadcrumb || []).length) {
      // a homeless cell's home is its frontier area; the residue keeps the bucket
      const area = tree.cellArea.get(focusId);
      html += area
        ? ` <span class="sep">/</span> <a data-nav="${FRONTIER_ID}">Frontier</a>` +
          ` <span class="sep">/</span> <a data-nav="${esc(area)}">${esc(frontierName(area))}</a>`
        : tree.frontier.length
        ? ` <span class="sep">/</span> <a data-nav="${FRONTIER_ID}">Frontier</a>` +
          ` <span class="sep">/</span> <a data-nav="${UNPLACED_ID}">unfiled</a>`
        : ` <span class="sep">/</span> <a data-nav="${UNPLACED_ID}">no formal home</a>`;
    }
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
  updateFrontierToggle();   // every view renders the crumb — the toggle rides it
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
  xref: "an external-database page about this atom — no other cell cites it, so it belongs to this atom outright",
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
  // a cited name that Mathlib has since renamed: say so, never present the dead
  // name as current (the code shown below is the CURRENT declaration's)
  if (o.kind === "decl" && o.renamed_to)
    head += ` <span class="mk" style="color:#d97706" title="the annotation cites a name Mathlib has since renamed — the code shown is the current declaration's">now ${esc(o.renamed_to)}</span>`;
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
    if (cl.length) {
      // Say it in English, with the claimants named and clickable: one external
      // page cited by several cells cannot belong to any of them (an organ in
      // two cells would MERGE them into one atom — SCHEMA rule 4), so it is
      // filed with the area they share and the claimants keep a co-page synapse.
      const names = cl.map(id => {
        const row = labelById && labelById.get(id.replace(/^cell:/, ""));
        const lbl = (row && row.label) || id.replace(/^cell:/, "");
        return `<span class="nav" data-nav="${esc(id)}" data-lbl="${esc(lbl)}">${esc(lbl)}</span>`;
      }).join(", ");
      const dbName = esc(XREF_NAME[o.db] || o.db || "external");
      body += cl.length === 1
        ? `<p class="osub">an area-level ${dbName} page — cross-referenced by ${names},
           but broader than that one cell, so it is filed here with the area
           it describes.</p>`
        : `<p class="osub">a shared reference: this one ${dbName} page is cited by
           <b>${cl.length}</b> different cells — ${names}. A page can belong to only
           one cell (sharing an organ would merge its claimants into a single atom),
           so it is filed here at their common area instead, and the claimants keep
           a “same page” synapse recording the relation.</p>`;
    }
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
  // a frontier cell wears its formal proximity (prox → cellProx, the same
  // plumbing cellArea rides): the bond-weighted evidence tying it to formal
  // code, in the same evidence-mass prose the synapse trace drawer uses
  const px = tree && tree.cellProx ? activeProxFor(c.id) : undefined;
  if (px !== undefined)
    html += `<div class="sub" title="formal proximity: score = trace weight of its bonds straight into formalized cells, plus ¼ of what its frontier neighbors can bridge (each bridge capped by both the bond and the neighbor's own direct evidence) — rank-mapped over all frontier cells">formal proximity <b>${
      (+px.s).toLocaleString()}</b> — ${esc(proxSummary(px))}${
      px.r !== undefined
        ? ` · closer to formal code than ${Math.floor((1 - px.r) * 100)}% of the frontier`
        : ""}${
      libsFiltered() && clientProx
        ? ` · libraries: ${esc(enabledLibs().join(" + ") || "none")}` : ""}</div>`;

  // (D) 'Build on these': a FRONTIER cell's formalized neighbor cells — the
  // concrete formal anchors a formalization of this concept could build on.
  // Neighbors come from the cell's own synapses; "formalized" is the labels
  // row's `p` (placed in the containment tree ⇔ has a decl organ — the same
  // test the dots' blue/grey fill uses), restricted to the enabled libraries
  // (the (V) table's library half — facets never gate a formal anchor). The
  // concrete decl name + module need each neighbor's shard entry, filled in
  // asynchronously below; the partner label, bond kinds and weight are free.
  const isFrontierCell = tree && tree.cellArea && tree.cellArea.has(c.id);
  let buildNbs = [], bodRowRef = null;
  if (isFrontierCell) {
    buildNbs = (e.syn || []).filter(s => isCellId(s.id) &&
      ((labelById && labelById.get(s.id)) || {}).p && libOkById(s.id));
    if (buildNbs.length) {   // syn ships weight-sorted — heaviest anchors first
      const shown = buildNbs.slice(0, 8);
      // px.db (shipped, all-libraries) is the build's own neighbor count; a
      // client re-score carries no db, and the shard's synapse cap can hide
      // lighter neighbors — name the honest total whenever it exceeds the list
      const totalFormal = !libsFiltered() && px && px.db !== undefined
        ? Math.max(px.db, buildNbs.length) : buildNbs.length;
      html += `<section class="kind" id="buildon"><h3 title="formalized neighbor cells — the declarations this concept's synapses bond to, i.e. the formal anchors a formalization could build on">Build on these
        <span class="cnt">(${shown.length}${totalFormal > shown.length
          ? ` of ${totalFormal} formalized neighbors — this card shows the heaviest-bonded`
          : ""})</span></h3>`;
      const bodRow = bodRowRef = s => {
        const kinds = Object.keys(s.kinds || {});
        return `<div class="edge"><div class="row" style="cursor:default">
          <span class="nav" data-nav="${esc(s.id)}"
            style="color:#1a4b8f;cursor:pointer;font-weight:600">${esc(synLabel(s.id))}</span>
          ${kinds.map(k => {
            const st = EDGE_STYLE[k] || {color: SYN_COLOR, label: k};
            return `<span class="mk" title="${esc(st.label)}"><span style="color:${
              st.color}">●</span> ${esc(k)}</span>`;
          }).join(" ")}
          <span class="prov" title="the number of constituent bonds">weight ${s.w}</span>
          <span class="lit-ref" data-bod="${esc(s.id)}" style="flex-basis:100%">resolving its declaration…</span>
        </div></div>`;
      };
      html += shown.map(bodRow).join("");
      if (buildNbs.length > shown.length)
        html += `<div class="edge" id="bod-more-row"><a id="bod-more" style="cursor:pointer"
          title="the shard ships this cell's heaviest ${buildNbs.length} synapses — render every bonded formalized neighbor">show
          all ${buildNbs.length} bonded</a></div>`;
      html += `</section>`;
    } else {
      const area = tree.cellArea.get(c.id);
      const near = area ? (tree.sc[area] || {}).near : null;
      html += `<section class="kind" id="buildon"><h3>Build on these</h3>
        <p class="note">deep frontier — no formal anchor yet: none of this cell's
        synapses reach a formalized cell${libsFiltered() ? " in the enabled libraries" : ""}${
        near
          ? `; nearest area home: <a data-nav="${esc(near)}">${esc(near.slice(5))}</a>`
          : area
            ? `, and its area (${esc(frontierName(area))}) has no formal home either`
            : ""}.</p></section>`;
    }
  }

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
  // Build-on-these, 'show all N bonded': swap the link row for the remaining
  // neighbors, re-wire nav, re-run the decl fill for the fresh [data-bod]s
  const moreBtn = panelEl.querySelector("#bod-more");
  if (moreBtn) moreBtn.addEventListener("click", () => {
    const row = panelEl.querySelector("#bod-more-row");
    if (!row || !bodRowRef) return;
    row.outerHTML = buildNbs.slice(8).map(bodRowRef).join("");
    wirePanel();
    fillBuildonDecls();
  });
  // Build-on-these, phase 2: each anchor's concrete DECL NAME + module lives in
  // the neighbor's own shard entry — parallel cached fetches, filled in
  // place. A miss is said out loud, never left as a spinner.
  function fillBuildonDecls() {
  panelEl.querySelectorAll("[data-bod]").forEach(async elx => {
    const nid = elx.dataset.bod;
    const ne = await getEntry(nid);
    if (lastPanelId !== id || !panelEl.contains(elx)) return;
    elx.removeAttribute("data-bod");   // one-shot, like enrichEvidence's data-lbl
    const decls = ne ? (ne.organs || []).filter(o => o.kind === "decl") : [];
    if (decls.length) {
      const d = decls[0];
      elx.innerHTML = `<code>${esc(d.label || d.id)}</code>${
        d.module ? ` · <code>${esc(d.module)}</code>` : ""}${
        decls.length > 1 ? ` · +${decls.length - 1} more decl${
          decls.length > 2 ? "s" : ""} on its card` : ""}`;
    } else {
      elx.textContent = ne
        ? "no decl organ in its shard (drift — the labels index says it has one)"
        : "neighbor entry unavailable";
    }
  });
  }
  fillBuildonDecls();
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
// ---- the lazy trace sidecar ------------------------------------------------
// A synapse with a SUPERCELL endpoint (cell↔path / path↔path — SCHEMA rule 5)
// ships TRACELESS in the eagerly-fetched supercells.json (carrying evidence
// nobody has clicked yet would treble it); the traces live in sidecar bucket
// files beside the cell shards. Buckets are keyed by the synapse pair key
// "<src>|<dst>" (stored order — lexicographic, the same key enrich() derives),
// normalized + longest-prefix-probed with the SAME scheme shardFor uses, so
// opening a drawer costs exactly ONE bucket fetch, cached for the page's life.
// `prov` indexes in sidecar traces resolve against the sidecar's own _meta.prov
// table when it ships one, else manifest.prov (build-verified identical).
let traceSidecarIdx = null;            // cached against the manifest that built it
const traceBucketCache = new Map();    // bucket key -> Promise<{ok, j}>
const synPairKey = (a, b) => (a < b ? a + "|" + b : b + "|" + a);
// The sidecar's _meta is `manifest.traces` — the manifest is the one file the
// client always holds, so no extra index fetch. Memoized on the meta object so
// a mid-session manifest re-sync (getEntry's retry path) rebuilds the index.
function traceSidecarIndex() {
  const meta = manifest && manifest.traces;
  if (!meta || !meta.files) return null;
  if (traceSidecarIdx && traceSidecarIdx.meta === meta) return traceSidecarIdx;
  const keys = Object.keys(meta.files);
  if (!keys.length) return null;
  const sch = meta.scheme || {};
  let lo = sch.min_len, hi = sch.max_len;
  if (!lo || !hi) {   // scheme missing: derive the probe bounds from the keys
    lo = Infinity; hi = 0;
    for (const k of keys) { lo = Math.min(lo, k.length); hi = Math.max(hi, k.length); }
  }
  traceSidecarIdx = {meta, keys: new Set(keys), lo, hi,
                     dir: (meta.dir || "traces") + "/",
                     caps: meta.caps || {}, prov: meta.prov || null};
  return traceSidecarIdx;
}
// the longest declared bucket key that prefixes the normalized pair key —
// shardFor's probe, over the sidecar's own key set (its keys are prefix-free,
// but a fixed length cannot work: all pair keys start "cell:"/"path:")
function sidecarBucketFor(idx, key) {
  for (let l = Math.min(idx.hi, Math.max(key.length, idx.lo)); l >= idx.lo; l--) {
    const k = shardKey(key, l);
    if (idx.keys.has(k)) return k;
  }
  for (let l = Math.max(key.length, idx.lo) + 1; l <= idx.hi; l++) {
    const k = shardKey(key, l);
    if (idx.keys.has(k)) return k;
  }
  return null;
}
async function fetchSidecarTraces(a, b) {
  const idx = traceSidecarIndex();
  if (!idx) return {ok: false, why: "no sidecar in this build"};
  const key = synPairKey(a, b);
  const bk = sidecarBucketFor(idx, key);
  if (bk === null) return {ok: false, why: "no bucket covers this synapse"};
  if (!traceBucketCache.has(bk)) {
    traceBucketCache.set(bk, fetch(BASE + idx.dir + bk + ".json" + vq())
      .then(r => (r.ok ? r.json().then(j => ({ok: true, j})) : {ok: false, j: {}}))
      .catch(() => ({ok: false, j: {}})));
  }
  const res = await traceBucketCache.get(bk);
  if (!res.ok) { traceBucketCache.delete(bk); return {ok: false, why: "fetch failed"}; }
  const row = res.j[key];
  if (!row) return {ok: false, why: "not in this build's sidecar"};
  const traces = row.traces || [];
  return {ok: true, traces, tt: row.tt || traces.length,
          prov: idx.prov || manifest.prov};
}
// ONE trace row — the same rendering whether the trace arrived inline in a cell
// shard or from the lazy sidecar: kind dot, clickable src → dst endpoints
// (data-nav → navigate → resolveId, so a decl lands on its owning cell and a
// path on its supercell), provenance class chip, evidence prose.
function traceRowHtml(t, provTable) {
  const st = EDGE_STYLE[t.kind] || {color: SYN_COLOR, label: t.kind};
  const prov = t.prov !== undefined ? (provTable || manifest.prov || [])[t.prov] : null;
  const pc = provClass(t.kind, prov, t.evidence);
  const ctx = {fromId: t.src, fromLabel: null, toId: t.dst, toLabel: null};
  return `<div class="edge open"><div class="row">
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
let synOpenSeq = 0;   // two rapid opens both set lastPanelId = "__syn__" — the
                      // seq keeps a stale open's async fills off the newer panel
async function showSynapsePanel(a, b, syn) {
  lastPanelId = "__syn__";
  const mySeq = ++synOpenSeq;
  const live = () => lastPanelId === "__syn__" && mySeq === synOpenSeq;
  // the flat map ships [i, j, w] only — fetch the kinds + traces on demand
  if (!syn || !syn.kinds || !Object.keys(syn.kinds).length) {
    const got = await synBetween(a, b);
    syn = got || {a, b, w: (syn && syn.w) || 0, kinds: {}, traces: []};
  }
  if (!live()) return;
  const [la, lb] = await Promise.all([labelOf(a), labelOf(b)]);
  if (!live()) return;
  const kinds = Object.entries(syn.kinds || {}).sort((x, y) => y[1] - x[1]);
  const dom = EDGE_STYLE[dominantKind(syn.kinds)] || {color: SYN_COLOR, label: "synapse"};
  let head = `<h2 style="font-size:1.05rem">Synapse</h2>
    <div class="sub"><span style="color:${dom.color}">●</span> weight <b>${syn.w}</b> —
      every weak bond between these two atoms, collapsed into one edge. A synapse is
      <b>undirected</b>: direction lives on each trace below.</div>
    <div class="chips">
      <span class="chip"><a data-nav="${esc(a)}">${esc(la)}</a></span>
      <span class="chip dirarrow">↔</span>
      <span class="chip"><a data-nav="${esc(b)}">${esc(lb)}</a></span>
    </div>`;
  if (kinds.length)
    head += `<section class="kind"><h3>Bonds <span class="cnt">(${kinds.length} kind${
      kinds.length === 1 ? "" : "s"})</span></h3><div class="chips">` +
      kinds.map(([k, v]) => {
        const st = EDGE_STYLE[k] || {color: SYN_COLOR, label: k};
        return `<span class="chip" title="${esc(st.label)}"><span style="color:${
          st.color}">●</span> ${esc(k)} <b>×${v}</b></span>`;
      }).join("") + `</div></section>`;
  // everything above the Traces section is identical across the loading /
  // loaded / failed states, so swapping them never moves the layout above
  const render = tracesHtml => {
    panelEl.innerHTML = head + tracesHtml + `<p class="note">Every line on the canvas
      is a stored synapse. Click either atom to inspect it.</p>`;
    wirePanel();
  };
  // ONE Traces-section builder for both sources, so a sidecar trace (cell↔path,
  // path↔path) renders EXACTLY like an inline cell↔cell one
  const tracesSection = (traces, tt, provTable, viaSidecar) => {
    let h = `<section class="kind"><h3>Traces <span class="cnt">(${traces.length}${
      tt > traces.length ? ` of ${tt}` : ""})</span></h3>`;
    for (const t of traces) h += traceRowHtml(t, provTable);
    if (tt > traces.length)
      h += viaSidecar
        ? `<div class="more" title="brain/query.py --full serves the complete set">showing ${
            traces.length} of ${tt} bonds</div>`
        : `<div class="more">${tt - traces.length} further trace${
            tt - traces.length === 1 ? " is" : "s are"} not shipped in this shard (cap:
            ${manifest._meta.caps.traces_per_synapse}/synapse) — the full set is at
            <code>/api/brain/*</code> or <code>brain/query.py</code>.</div>`;
    return h + `</section>`;
  };
  const inline = syn.traces || [];
  const pathInvolved = isPathId(a) || isPathId(b);
  if (inline.length) {
    render(tracesSection(inline, syn.tt || inline.length, manifest.prov, false));
    // A cell↔path synapse opened from the cell card carries only the inline
    // cap-6 set; the sidecar holds up to 24 — upgrade in place so the evidence
    // depth doesn't depend on which panel the user came from.
    if (pathInvolved && (syn.tt || 0) > inline.length) {
      const got = await fetchSidecarTraces(a, b);
      if (!live()) return;
      if (got.ok && got.traces.length > inline.length)
        render(tracesSection(got.traces, got.tt, got.prov, true));
    }
    return;
  }
  if (pathInvolved) {
    // A supercell-level synapse ships traceless in supercells.json (byte budget);
    // its traces are exactly ONE lazy sidecar-bucket fetch away.
    render(`<section class="kind"><h3>Traces${
      syn.tt ? ` <span class="cnt">(${syn.tt})</span>` : ""}</h3>
      <div class="edge"><div class="row"><span class="mk">loading traces…</span></div></div></section>`);
    const got = await fetchSidecarTraces(a, b);
    if (!live()) return;
    if (got.ok) { render(tracesSection(got.traces, got.tt, got.prov, true)); return; }
    // a failed fetch is VISIBLE, never a silent empty (project bug-class rule)
    render(`<section class="kind"><h3>Traces</h3>
      <div class="edge"><div class="row"><span class="mk">traces unavailable (${
        esc(got.why)})</span></div></div>
      <p class="note">The full set is at <code>/api/brain/*</code> or
        <code>brain/query.py --full</code>.</p></section>`);
    return;
  }
  // a cell↔cell synapse neither endpoint's shard kept (both hit synapses_per_cell)
  render(`<section class="kind"><h3>Traces</h3>
    <p class="note">This synapse's traces aren't shipped in the static view. The full
      set is at <code>/api/brain/*</code> or <code>brain/query.py</code>.</p></section>`);
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
  // the Libraries control (rendered by the root panel + the frontier panel)
  panelEl.querySelectorAll(".libcb").forEach(cb =>
    cb.addEventListener("change", () => setLibEnabled(cb.dataset.lib, cb.checked)));
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
  // frontier ids are TREE rows, not shard entries — they must never reach getEntry
  if (id === FRONTIER_ID || isSectorId(id)) return frontierPanel(id);
  if (isFrontierId(id)) return frontierAreaPanel(id);
  if (id.startsWith(STRAYS_PREFIX)) return straysPanel(id.slice(STRAYS_PREFIX.length));
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
  for (const [n, p] of rows) {
    const off = disabledLibs.has(p.slice(5));
    const nv = off ? 0 : countVisible(p);
    // "N of M shown" whenever the predicate shrank a count (no-silent-filter)
    html += `<span class="chip"><a data-nav="${esc(p)}">${esc(p.slice(5))}</a>
      <small>${off ? "off — hidden"
        : filtersActive() && nv !== n
          ? `${nv.toLocaleString()} of ${n.toLocaleString()} shown`
          : n.toLocaleString()}</small></span>`;
  }
  html += `</div><p class="note">${
    tree.roots.length - rows.length} further library roots hold no cells yet.</p></section>`;
  html += librariesSectionHtml();
  const frAll = tree.frontierN + tree.unplaced.length;
  const frVis = frontierVisibleN() + unplacedVisibleN();
  if (tree.frontier.length)
    html += `<section class="kind"><h3>The Frontier <span class="cnt">(${
      filtersActive() && frVis !== frAll
        ? `${frVis.toLocaleString()} of ${frAll.toLocaleString()} shown`
        : frAll.toLocaleString()})</span></h3>
      <p class="note">Atoms with no attached Lean declaration have no module to nest in;
      that structural fact does not by itself mean Mathlib lacks the concept. ${tree.prox
        ? `<a data-nav="${FRONTIER_ID}">Browse the queue</a>: every frontier concept
        ranked by its <b>formal proximity</b> — the bond-weighted evidence tying it to
        formalized code, so a concept riding hundreds of bonds ranks above one with no
        formal signal — with its evidence, its area and the formal anchors to build
        on. Or open the <a data-nav="${FRONTIER_MAP_ID}">polar map</a>: the
        ${tree.frontier.length} areas as sectors around the formal core, every cell
        placed by that same proximity; the queue separates actionable candidates from
        cells that need scope or coverage review`
        : `<a data-nav="${FRONTIER_ID}">Open the frontier</a>: the
        ${tree.frontier.length} frontier areas (this build ships no proximity data,
        so they render as dive-able bubbles)`}.</p></section>`;
  else if (tree.unplaced.length)
    html += `<section class="kind"><h3>No formal home <span class="cnt">(${
      filtersActive() && unplacedVisibleN() !== tree.unplaced.length
        ? `${unplacedVisibleN().toLocaleString()} of ${tree.unplaced.length.toLocaleString()} shown`
        : tree.unplaced.length.toLocaleString()})</span></h3>
      <p class="note">Atoms with no Lean declaration have no module to nest in — nothing
      formalizes them yet. <a data-nav="${UNPLACED_ID}">Browse them</a>, or find them in the
      Explorer, which places every atom.</p></section>`;
  panelEl.innerHTML = html;
  wirePanel();
}
// ---- the Frontier group + its areas (frontier:<Area> rows on the tree) ------
// ONE panel for BOTH frontier surfaces: the queue (#__frontier__, optionally
// #__frontier__:<Area>) and the map (#__frontier__:map[:<Area>]). Counts
// reflect the prox ON SCREEN — the client re-score when the library set is
// filtered, the shipped arrays otherwise.
async function frontierPanel(id) {
  id = id || FRONTIER_ID;
  await ensureTree();
  if (lastPanelId !== id) return;
  const fv = frontierViewOf(id) || {mode: "list", area: null};
  const base = fv.mode === "map" ? FRONTIER_MAP_ID : FRONTIER_ID;
  const sector = fv.area && (tree.sc[fv.area] || {}).frontier ? fv.area : null;
  const areas = sector ? [sector] : tree.frontier;
  // counts run under the SAME (V) predicate the canvas renders: membership =
  // prox'd AND cellVisible; the library set acts through the re-score
  let total = 0, totalRaw = 0, nDirect = 0, nBridged = 0, nZero = 0;
  for (const p of areas)
    for (const cid of (tree.sc[p] || {}).cells || []) {
      if (!tree.prox) { totalRaw++; if (cellVisible(cid)) total++; continue; }
      const px = activeProxFor(cid);
      if (!px) continue;
      totalRaw++;
      if (!cellVisible(cid)) continue;
      total++;
      if (px.dw > 0) nDirect++; else if (px.iw > 0) nBridged++; else nZero++;
    }
  let html = `<div class="crumb"><a data-nav="${ROOTS_ID}">all libraries</a> /
      ${fv.mode === "map"
        ? `<a data-nav="${FRONTIER_ID}">Frontier</a> / ${sector
            ? `<a data-nav="${FRONTIER_MAP_ID}">map</a> / ${esc(frontierName(sector))} sector`
            : "map"}`
        : sector
          ? `<a data-nav="${FRONTIER_ID}">Frontier</a> / ${esc(frontierName(sector))}`
          : "Frontier"}</div>
    <h2>${sector
      ? `${esc(frontierName(sector))} — frontier ${fv.mode === "map" ? "sector" : "queue"}`
      : "The Frontier"}</h2>
    <div class="sub">${filterMask && total !== totalRaw
      ? `${total.toLocaleString()} of ${totalRaw.toLocaleString()} cells shown`
      : `${total.toLocaleString()} cells`} · ${sector ? "one area"
      : `${tree.frontier.length} areas`} · atoms with no Lean declaration${
      libsFiltered() && clientProx
        ? ` · libraries: ${esc(enabledLibs().join(" + ") || "none")}` : ""}</div>`;
  if (fv.mode === "map") html += sector
    ? `<p class="note">One frontier area's cells, spread over the full circle — dot
       labels render where they fit, and zooming reveals more. Each dot's distance
       from the center is its <b>formal proximity</b>: the trace weight of its bonds
       straight into formalized cells, plus ¼ of what its frontier neighbors can
       bridge (each bridge capped by both the bond and the neighbor's own direct
       evidence), rank-mapped over the whole frontier. Click the canvas background
       (or <a data-nav="${FRONTIER_MAP_ID}">here</a>) to return to the full map,
       browse <a data-nav="${FRONTIER_ID + ":" + sector.slice(9)}">this area in the
       queue</a>, or open <a data-nav="${esc(sector)}">${esc(frontierName(sector))}</a>
       as dive-able dots.</p>`
    : `<p class="note">These atoms have no attached Lean declaration, so the
       containment tree cannot place them. Each is filed under the <b>library area its synapse
       neighborhood points at</b> — a weighted vote of its formalized neighbors
       (deterministic, no LLM; <code>brain/build_frontier.py</code>) — and the areas
       are the angular sectors. Each dot's distance from the central formal disc is
       its <b>formal proximity</b>: the trace weight of its bonds straight into
       formalized cells, plus ¼ of what its frontier neighbors can bridge (each
       bridge capped by both the bond and the neighbor's own direct evidence),
       rank-mapped over the whole frontier — a concept riding hundreds of bonds hugs
       the core; one thread to an isolated node sits far out. A missing declaration
       organ does not by itself prove that Mathlib lacks the concept. Click a sector's rim
       label to focus it, or <a data-nav="${FRONTIER_ID}">browse the queue</a> — the
       same cells as a ranked list.</p>`;
  else html += `<p class="note">${sector
      ? `One frontier area's cells as a ranked queue — the rest of the frontier is
         one chip-click away. Within this area, `
      : `These atoms have no attached Lean declaration. The queue puts
         <b>actionable candidates first</b>, then rows that need coverage or scope review;
         within each tier, `}<b>formal proximity</b>${sector ? "" : " orders the rows"}: the trace
       weight of ${sector ? "each cell's" : "its"} bonds straight into formalized
       cells, plus ¼ of what its frontier neighbors can bridge (each bridge capped
       by both the bond and the neighbor's own direct evidence). Sort by readiness,
       evidence breadth or name; the chips filter by area; click a row for the
       cell's full card; the queue row keeps its assessment reason visible beside the
       formal anchors. Every structural frontier cell remains searchable. Or open the
       <a data-nav="${sector ? FRONTIER_MAP_ID + ":" + sector.slice(9) : FRONTIER_MAP_ID}">polar
       map</a> — the same proximity as radius.</p>`;
  if (tree.prox)
    html += `<section class="kind"><h3>Formal proximity</h3><div class="chips">
      <span class="chip" title="cells with at least one synapse straight into a formalized cell">bond formal code directly <b>${nDirect.toLocaleString()}</b></span>
      <span class="chip" title="cells whose only formal evidence bridges through a frontier neighbor (¼-damped, capped by the neighbor's own direct evidence)">bridged only <b>${nBridged.toLocaleString()}</b></span>
      <span class="chip" title="no bonds into formalized cells and no bridging neighbor with any — zero formal evidence">no formal signal <b>${nZero.toLocaleString()}</b></span></div>
      <p class="note">${fv.mode === "map"
        ? `The central disc is the enabled formal libraries; click it to open them.
           The outermost dots carry no formal signal at all — the deepest frontier.`
        : `The readiness sort places actionable candidates first and orders each
           tier by formal proximity; review-needed rows stay visible below them.`}</p></section>`;
  if (!sector) {
    html += `<section class="kind"><h3>Areas <span class="cnt">(${
      tree.frontier.length})</span></h3><div class="chips">`;
    for (const p of tree.frontier) {
      const na = ((tree.sc[p] || {}).cells || []).length;
      const nva = countVisible(p);
      html += `<span class="chip"><a data-nav="${esc(p)}">${esc(frontierName(p))}</a>
        <small>${filtersActive() && nva !== na
          ? `${nva.toLocaleString()} of ${na.toLocaleString()} shown`
          : na.toLocaleString()}</small></span>`;
    }
    html += `</div></section>`;
    if (tree.unplaced.length)
      html += `<section class="kind"><h3>Unfiled <span class="cnt">(${
        filtersActive() && unplacedVisibleN() !== tree.unplaced.length
          ? `${unplacedVisibleN().toLocaleString()} of ${tree.unplaced.length.toLocaleString()} shown`
          : tree.unplaced.length.toLocaleString()})</span></h3>
        <p class="note">A different residue: these cells DO hold Lean declarations, but
        no module is recorded for them (Mathlib-Archive names with no
        <code>contains</code> parent), so neither the tree nor the frontier partition —
        which claims only declaration-less cells — can file them.
        <a data-nav="${UNPLACED_ID}">Browse them</a>.</p></section>`;
  }
  html += librariesSectionHtml();
  panelEl.innerHTML = html;
  wirePanel();
}
async function frontierAreaPanel(id) {
  await ensureTree();
  const sc = tree.sc[id];
  if (lastPanelId !== id) return;
  if (!sc) { panelEl.innerHTML = `<p class="note">Unknown frontier area: ${esc(id)}</p>`; return; }
  const cells = frontierCells(id);   // best-evidenced first (prox r asc, stable)
  // the (V) predicate: chips, counts and the proximity summary below all
  // describe the VISIBLE membership, labeled "N of M shown" when it shrank
  const visCells = filtersActive() ? cells.filter(c => cellVisible(c)) : cells;
  const near = sc.near || null;
  const st = sc.stateability;
  let html = `<div class="crumb"><a data-nav="${ROOTS_ID}">all libraries</a> /
      <a data-nav="${FRONTIER_ID}">Frontier</a> / ${esc(frontierName(id))}</div>
    <h2>${esc(sc.label || id)}</h2>
    <div class="sub">frontier area · <code>${esc(id)}</code> ·
      ${visCells.length !== cells.length
        ? `${visCells.length.toLocaleString()} of ${cells.length.toLocaleString()} cells shown`
        : `${cells.length.toLocaleString()} cells`}, none with a declaration organ</div>`;
  html += `<section class="kind"><h3>Nearest formal home</h3>` + (near
    ? `<div class="chips"><span class="chip"><a data-nav="${esc(near)}">${esc(near.slice(5))}</a>
        <small>${esc(((tree.sc[near] || {}).label) || "")}</small></span></div>
      <p class="note">These atoms hold no Lean declaration; their synapse neighborhoods
      vote them next to <code>${esc(near)}</code> — the library area a formalization
      would most likely land in.</p>`
    : `<p class="note">none — ${id === "frontier:Unsorted"
        ? `these cells' neighborhoods reach no formalized cell at all (most have no
           synapses), so no library area can claim them; this is the honest remainder
           of the partition`
        : "no formalized neighbor votes for a library area here"}.</p>`) + `</section>`;
  html += `<section class="kind"><h3>Stateability</h3>` + (st != null
    ? `<p class="note">mean <b>${st.toFixed(2)}</b> — the fraction of each cell's
       neighborhood that is already formalized, averaged over the area's halo-scored
       cells (0 = isolated from formal code, 1 = surrounded by it). The area bubble's
       grey→blue tint carries this number.</p>`
    : `<p class="note">not yet scored — none of this area's cells appear in the
       stateability halo.</p>`) + `</section>`;
  if (tree.prox && sc.prox) {
    // activeProxFor, NOT the shipped arrays: under a filtered library set the
    // canvas renders the client re-score, and this panel must agree with it —
    // and the counts run over the (V)-visible membership only
    let nDirect = 0, nBridged = 0, nZero = 0;
    for (const cid of visCells) {
      const px = activeProxFor(cid);
      if (!px) continue;
      if (px.dw > 0) nDirect++;
      else if (px.iw > 0) nBridged++;
      else nZero++;
    }
    html += `<section class="kind"><h3>Formal proximity</h3><div class="chips">${
      nDirect ? `<span class="chip" title="cells with at least one synapse straight into a formalized cell">bond formal code directly <b>${nDirect.toLocaleString()}</b></span>` : ""}${
      nBridged ? `<span class="chip" title="cells whose only formal evidence bridges through a frontier neighbor (¼-damped, capped by the neighbor's own direct evidence)">bridged only <b>${nBridged.toLocaleString()}</b></span>` : ""}${
      nZero ? `<span class="chip" title="no bonds into formalized cells and no bridging neighbor with any — zero formal evidence">no formal signal <b>${nZero.toLocaleString()}</b></span>` : ""}</div>
      <p class="note">bond-weighted evidence into formalized cells (score = direct
      trace weight + ¼ of what frontier neighbors can bridge, each bridge capped by
      both the bond and the neighbor's own direct evidence) — the dive and the list
      below run best-evidenced first.
      <a data-nav="${FRONTIER_ID + ":" + id.slice(9)}">See this area in the frontier
      queue</a> · <a data-nav="${FRONTIER_MAP_ID + ":" + id.slice(9)}">as a sector on
      the polar map</a>.</p></section>`;
  }
  if ((sc.top || []).length) {
    const topVis = filtersActive() ? sc.top.filter(t => cellVisible(t.cell)) : sc.top;
    html += `<section class="kind"><h3>Top cells <span class="cnt">(${
      topVis.length !== sc.top.length
        ? `${topVis.length} of ${sc.top.length} shown` : sc.top.length})</span></h3>
      <p class="note">the area's most-connected cells (total synapse weight; formal
      bonds count 3×) — the natural first formalization targets.</p><div class="chips">`;
    for (const t of topVis)
      html += `<span class="chip"><a data-nav="${esc(t.cell)}">${esc(t.label || t.cell)}</a>
        <small title="weighted synapse degree">${Number(t.score || 0).toLocaleString()}</small></span>`;
    html += `</div></section>`;
  }
  if (cells.length) {
    html += `<section class="kind"><h3>Cells <span class="cnt">(${
      visCells.length !== cells.length
        ? `${visCells.length} of ${cells.length} shown` : cells.length})</span></h3><div class="chips">`;
    for (const cid of visCells.slice(0, 80))
      html += `<span class="chip"><a data-nav="${esc(cid)}">${
        esc(((labelById && labelById.get(cid)) || {}).label || cid)}</a></span>`;
    if (visCells.length > 80) html += `<span class="chip">… +${visCells.length - 80} more</span>`;
    html += `</div></section>`;
  }
  panelEl.innerHTML = html;
  wirePanel();
}
async function straysPanel(parent) {
  await ensureTree();
  const sc = tree.sc[parent] || {};
  const cells = sc.cells || [];
  const atRoot = !parent.slice(5).includes("/");
  const chain = pathChain(parent);
  let html = `<div class="crumb">` + chain.map(b =>
    `<a data-nav="${esc(b.id)}">${esc(b.label)}</a>`).join(" / ") + `</div>`;
  const visStr = filtersActive() ? cells.filter(c => cellVisible(c)) : cells;
  html += `<h2>${atRoot ? "No module recorded" : "Filed at this level"}</h2>
    <div class="sub">${visStr.length !== cells.length
      ? `${visStr.length.toLocaleString()} of ${cells.length.toLocaleString()} cells shown`
      : `${cells.length.toLocaleString()} cells`} · directly under
      <code>${esc(parent)}</code></div>`;
  // The honest story differs by altitude. Deeper down, a cell filed at the level
  // itself is ordinary (its decl lives in <path>.lean rather than a sub-folder).
  // At a library ROOT nothing legitimately files — a decl lands here only because
  // it has NO recorded module, and after the doc-gen4/.ilean oracle pass the ones
  // that remain are names that do not exist in current Mathlib at all.
  html += atRoot
    ? `<p class="note">A declaration files at the library root only when <b>no module is
        recorded for it anywhere</b> — and every placement oracle (TheoremGraph, the
        snapshot CSVs, the tag rows, doc-gen4, the checkout's own indexes) has been
        asked. What remains are declaration names that <b>do not exist in current
        Mathlib</b>: stale renames (<code>Basis</code> is now <code>Module.Basis</code>),
        citations of names that never existed, and namespaces mistaken for
        declarations. They come from WikiLean's own annotations, so this bubble is an
        honest inventory of annotation debt — kept here deliberately, because filing
        them into a guessed folder would be worse than leaving them unfiled. The
        cleanup lives in the decl-existence sweep, not the map.</p>`
    : `<p class="note">These cells' declarations live in <code>${esc(parent.slice(5))}.lean</code>
        itself (or files at this level) rather than in a sub-folder — ordinary filing,
        collapsed into one bubble so the sub-areas stay readable.</p>`;
  if (cells.length) {
    html += `<section class="kind"><h3>Cells <span class="cnt">(${
      visStr.length !== cells.length
        ? `${visStr.length} of ${cells.length} shown` : cells.length})</span></h3><div class="chips">`;
    for (const cid of visStr.slice(0, 120))
      html += `<span class="chip"><a data-nav="${esc(cid)}">${
        esc(((labelById && labelById.get(cid)) || {}).label || cid)}</a></span>`;
    if (visStr.length > 120) html += `<span class="chip">… +${visStr.length - 120} more</span>`;
    html += `</div></section>`;
  }
  panelEl.innerHTML = html;
  wirePanel();
}
function unplacedPanel() {
  // two different buckets share this id: with the frontier shipped it holds only
  // the residue (cells whose decls have no recorded module); without it (fail-soft
  // build) it is the whole homeless population, and the copy must not lie
  const crumb = tree.frontier.length
    ? `<div class="crumb"><a data-nav="${ROOTS_ID}">all libraries</a> /
        <a data-nav="${FRONTIER_ID}">Frontier</a> / unfiled</div>` : "";
  panelEl.innerHTML = crumb + `<h2>${tree.frontier.length ? "Unfiled" : "No formal home"}</h2>
    <div class="sub">${filtersActive() && unplacedVisibleN() !== tree.unplaced.length
      ? `${unplacedVisibleN().toLocaleString()} of ${tree.unplaced.length.toLocaleString()} cells shown`
      : `${tree.unplaced.length.toLocaleString()} cells`}</div>
    <p class="note">${tree.frontier.length
      ? `These atoms hold Lean declarations, but no module is recorded for them
         (Mathlib-Archive names with no <code>contains</code> parent) — so the
         containment tree cannot place them, and the frontier partition, which claims
         only declaration-less atoms, cannot either. Real atoms with real synapses:
         the Explorer places every one of them, and search finds them by any of their
         organs' names.`
      : `These atoms hold no Lean declaration, so they have no module to nest
         inside — the containment tree can't place them. They are real atoms with real
         synapses: the Explorer places every one of them, and search finds them by any of
         their organs' names.`}</p>`;
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
  const nSub = tree.count(p), nSubVis = countVisible(p);
  const hereAll = (sc.cells || []);
  const hereVis = filtersActive() ? hereAll.filter(c => cellVisible(c)) : hereAll;
  html += `<h2>${esc(sc.label || p)}</h2>
    <div class="sub">supercell · <code>${esc(p)}</code> · ${
      filtersActive() && nSubVis !== nSub
        ? `${nSubVis.toLocaleString()} of ${nSub.toLocaleString()} cells shown in the subtree`
        : `${nSub.toLocaleString()} cells in the subtree`}${
      hereAll.length ? ` · ${hereVis.length !== hereAll.length
        ? `${hereVis.length} of ${hereAll.length} shown` : hereAll.length} here` : ""}</div>`;
  // rule-5 organs: field-of-study concepts and area pages belong to the FOLDER,
  // never to a cell — "Linear algebra" is this module, not the Module atom.
  // Rule-4 PARKED pages are a different fact (a page shared by several cells
  // below, displaced up here) — mixing them with the field organs read as
  // "Parseval's Theorem is an area page about Analysis". Two sections.
  if ((sc.organs || []).length) {
    const about = sc.organs.filter(o => o.bond !== "area-page");
    const parked = sc.organs.filter(o => o.bond === "area-page");
    if (about.length) {
      html += `<section class="kind"><h3 title="a field-of-study concept or an area-level page belongs to the module, never to a cell (SCHEMA rule 5)">This area <em>is</em>
        <span class="cnt">(${about.length})</span></h3>`;
      for (const o of about) html += organHtml(o, null);
      html += `</section>`;
    }
    if (parked.length) {
      html += `<section class="kind"><h3 title="an external page cited by several cells below — it describes specific results, not this area, and is parked here because attaching it to any one claimant would merge them into one atom (SCHEMA rule 4)">Shared references parked here
        <span class="cnt">(${parked.length})</span></h3>
        <p class="note">Pages cited by <b>several cells below</b> — they describe specific
        results, not this area; each sits here only because no single cell can own it.</p>`;
      for (const o of parked) html += organHtml(o, null);
      html += `</section>`;
    }
  }
  if ((sc.children || []).length) {
    html += `<section class="kind"><h3>Areas <span class="cnt">(${sc.children.length})</span></h3><div class="chips">`;
    for (const ch of sc.children.slice(0, 60)) {
      const na = tree.count(ch), nv = countVisible(ch);
      html += `<span class="chip"><a data-nav="${esc(ch)}">${esc((tree.sc[ch] || {}).label || ch)}</a>
        <small>${filtersActive() && nv !== na
          ? `${nv.toLocaleString()} of ${na.toLocaleString()} shown`
          : na.toLocaleString()}</small></span>`;
    }
    if (sc.children.length > 60) html += `<span class="chip">… +${sc.children.length - 60} more</span>`;
    html += `</div></section>`;
  }
  if (hereAll.length) {
    html += `<section class="kind"><h3>Cells here <span class="cnt">(${
      hereVis.length !== hereAll.length
        ? `${hereVis.length} of ${hereAll.length} shown` : hereAll.length})</span></h3><div class="chips">`;
    for (const cid of hereVis.slice(0, 80))
      html += `<span class="chip"><a data-nav="${esc(cid)}">${
        esc(((labelById && labelById.get(cid)) || {}).label || cid)}</a></span>`;
    if (hereVis.length > 80) html += `<span class="chip">… +${hereVis.length - 80} more</span>`;
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
    // a frontier row's id prefix is "frontier:" (9 chars), not "path:" (5) — a
    // blind slice(5) would index "ier:Analysis" and never match "analysis"
    const fr = isFrontierId(p);
    rows.push({id: p, label: sc.label || p, type: fr ? "frontier" : "area", n,
               hay: [(sc.label || "").toLowerCase(),
                     (fr ? p.slice(9) : p.slice(5)).toLowerCase()]});
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
      // (V) search badges ride the SAME predicate as the canvas: a hit the
      // current filters hide says so, and an area count says "n of N shown"
      const hidden = r.type === "cell" && filtersActive() && !cellVisible(r.id);
      const nv = r.n && filtersActive() ? countVisible(r.id) : r.n;
      return `<div class="hit" data-id="${esc(r.id)}"><span class="t">${esc(r.type)}</span> ${
        esc(r.label)}${via ? ` <span class="aka">— its organ “${esc(via)}”</span>` : ""}${
        hidden ? ` <span class="aka">· hidden by the current filters</span>` : ""}${
        r.n ? ` <small style="color:#8c959f">${nv !== r.n
          ? `${nv.toLocaleString()} of ${r.n.toLocaleString()} shown`
          : r.n.toLocaleString()}</small>` : ""}</div>`;
    }).join("") || `<div class="hit"><span class="t">no hits</span> try /decl/&lt;name&gt; for declarations</div>`;
    box.style.display = "block";
    box.querySelectorAll("[data-id]").forEach(h =>
      h.addEventListener("click", () => { box.style.display = "none"; $("#q").value = ""; searchGo(h.dataset.id); }));
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
// ONE deliberate exception (reshape contract E): a FILTERED subgraph. The
// predicate cuts holes the build layout never planned for, so when the visible
// remainder is small enough (<= RELAX_CAP cells) the client runs a BOUNDED,
// fully deterministic relaxation seeded from the build xy — see the solver
// below drawXFrame. With no filter active the build xy is used verbatim, so
// clearing every filter restores positions bit-identical to explorer.json.
//
// explorer.json ships edges as index triples [i, j, w] into `nodes` (ids average
// ~11 chars and repeat twice per edge, so objects cost ~4×) — which is what buys
// shipping all 76,083 synapses in 2.3 MB with no draw cap.
let xdata = null;
async function fetchExplorerData() {
  if (xdata) return xdata;
  const get = () => fetch(BASE + "explorer.json" + vq())
    .then(r => (r.ok ? r.json() : null)).catch(() => null);
  const j = await get();
  xdata = j;
  return j;
}
// 76k SVG <path> segments — and 20.9k SVG <circle>s — are not a thing a browser
// repaints at 60fps: measured on the shipped graph, every wheel/mousemove ran
// ~53ms of main thread BEFORE rasterising. The explorer therefore paints on a
// <canvas> (the SVG stays the empty event surface on top):
//   · a uniform spatial grid (CSR) over the shipped xy, built ONCE per scope —
//     it culls the nodes each frame and answers every pointer hit test, so no
//     mousemove ever scans all n cells
//   · zoom-tiered LOD:  far (k < XK_EDGE_MIN)  dots only — no edges, no labels
//                       mid                    + edges from the heaviest weight
//                         tier down, stopping at the tier that would push the
//                         drawn count past XE_BUDGET (a weight threshold)
//                       near                   all visible edges + labels for
//                         the largest cells on screen (XL_CAP hard cap).
//                         Near = every visible edge fits the budget (a small
//                         scope is "near" at rest — full detail) OR k >=
//                         XK_NEAR: the dense core's long heavy edges cross any
//                         zoomed-in viewport, so a count test alone would keep
//                         max-zoom label-less and capped forever
//   · every input coalesces into ONE requestAnimationFrame draw — never a draw
//     per wheel event
// Nothing is dropped from the DATA: xEdges keeps every synapse for the click
// hit test, and zooming in always reaches full detail — the budget caps what
// one FRAME draws, not what the view can reach.
const SYN_TIERS = [
  {min: 1, max: 1, w: 0.5, op: 0.10},
  {min: 2, max: 3, w: 0.7, op: 0.18},
  {min: 4, max: 7, w: 1.1, op: 0.30},
  {min: 8, max: Infinity, w: 1.8, op: 0.50},
];
const XK_EDGE_MIN = 0.05;   // below this zoom the view is "far": dots only
const XK_NEAR = 0.8;        // at/above this zoom the view is "near" regardless
const XE_BUDGET = 8000;     // max synapse segments a MID frame draws
const XL_CAP = 250;         // max labels on screen (same cap the SVG version had)
// ---- (E) deterministic relaxation of FILTERED subgraphs ---------------------
// DETERMINISM CONTRACT: a FIXED iteration count, no Math.random anywhere, every
// loop in array order, and forces derived only from the visible subset — the
// same subset always settles to the same layout, bit for bit (the completion
// telemetry in __xrelax hashes the final positions so two runs can be diffed).
// The solve runs in rAF slices (~RELAX_CHUNK_MS of solver work per frame) so
// long main-thread blocks are bounded in the common case (adaptive pacing
// halves the slice after an overshoot) and the pull-in IS the animation. Above
// RELAX_CAP the build xy is kept and the status line says so — never a silent
// skip (no-silent-filter rule).
const RELAX_CAP = 4000;     // relax only when the visible subgraph is <= this
const RELAX_ITERS = 96;     // FIXED total iterations — the determinism anchor
const RELAX_CHUNK_MS = 10;  // solver budget per rAF slice (perf: no long block)
const RELAX_ITERS_PER_FRAME = 4;  // pacing cap: a small graph solves in <1ms an
                            // iteration, and without this cap the whole pull-in
                            // lands inside ~3 frames — a snap, not an animation.
                            // 96/4 = 24 frames ≈ 0.4s at 60fps; chunking never
                            // changes the math, only when it is shown
const RELAX_KK = 26;        // ideal spacing, world units (≈ the build core's own
                            // median cell spacing on the shipped explorer.json)
const RELAX_NEIGH_MAX = 64; // repulsion pairs per node per pass — an algorithmic
                            // bound, DETERMINISTIC (grid traversal order) and
                            // counted in __xrelax.neighCapHits, never a silent
                            // data drop (nothing is hidden — only physics)
let xEdges = [];   // [{a, b, w, ax, ay, bx, by, ai, bi}] click hit test + solver
                   // (ai/bi index layout.leaves, so the relaxation can refresh
                   //  the endpoint coords the tiers and the edge click read)
let xr = null;     // canvas render state (typed arrays + grid), per scope
let xRelax = null; // in-flight (E) relaxation state (null = none)
let xLastGestureT = 0;   // last USER pan/zoom — guards the settle-refit snap
let xDrawPending = false, xDrawTimer = 0, xInputT = 0, xHover = -1;
const xcv = document.getElementById("xcanvas");
const xctx = xcv.getContext("2d");
// live draw telemetry (console-inspectable debug hook; ~40 bytes, no hot-path cost)
window.__xstats = {lod: "", nodes: 0, edges: 0, perTier: [0, 0, 0, 0],
                   labels: 0, drawMs: 0, i2f: []};

function xcanvasShow(on) {
  xcv.style.display = on ? "block" : "none";
  if (!on) {
    cancelXRelax();   // leaving the explorer orphans any in-flight relaxation
    xr = null; xHover = -1; xDrawPending = false;
    clearTimeout(xDrawTimer);
    stageEl.title = "";
    svg.node().style.cursor = "";
  }
}
function scheduleXDraw() {
  if (!xr || xDrawPending) return;
  xDrawPending = true;
  requestAnimationFrame(xDrawNow);
  // rAF pauses in hidden tabs (fadeIn's own trap) — a safety timer keeps a
  // background tab from sticking blank; it still coalesces (one timer, one draw)
  xDrawTimer = setTimeout(xDrawNow, 120);
}
function xDrawNow() {
  if (!xDrawPending) return;
  xDrawPending = false;
  clearTimeout(xDrawTimer);
  drawXFrame();
}
// typed-array scene + CSR grid, built once per renderExplorer (scope/filter)
function buildXState(leaves) {
  const n = leaves.length;
  const X = new Float32Array(n), Y = new Float32Array(n), R = new Float32Array(n);
  const flags = new Uint8Array(n);   // bit0 formal (blue), bit1 gold @[wikidata]
  const labs = new Array(n);
  let maxR = 0;
  for (let i = 0; i < n; i++) {
    const l = leaves[i];
    X[i] = l.x; Y[i] = l.y; R[i] = l.r;
    if (l.r > maxR) maxR = l.r;
    flags[i] = (l.data.p ? 1 : 0) | (((l.data.f || 0) & 1) ? 2 : 0);
    const raw = l.data.label || l.data.id;
    labs[i] = raw.length > 24 ? raw.slice(0, 22) + "…" : raw;
  }
  // label rank = the SVG version's ordering: biggest dot (≈ degree) first
  const rank = Uint32Array.from(
    Array.from({length: n}, (_, i) => i).sort((a, b) => R[b] - R[a]));
  // ---- the uniform spatial grid (CSR: starts + items) -----------------------
  let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
  for (let i = 0; i < n; i++) {
    if (X[i] < minx) minx = X[i]; if (X[i] > maxx) maxx = X[i];
    if (Y[i] < miny) miny = Y[i]; if (Y[i] > maxy) maxy = Y[i];
  }
  if (!(minx <= maxx)) { minx = miny = 0; maxx = maxy = 1; }
  const gn = Math.max(1, Math.min(160, Math.ceil(Math.sqrt(n || 1))));
  const cw = Math.max((maxx - minx) / gn, 1e-6);
  const ch = Math.max((maxy - miny) / gn, 1e-6);
  const bxOf = i => Math.min(gn - 1, Math.max(0, Math.floor((X[i] - minx) / cw)));
  const byOf = i => Math.min(gn - 1, Math.max(0, Math.floor((Y[i] - miny) / ch)));
  const starts = new Uint32Array(gn * gn + 1);
  for (let i = 0; i < n; i++) starts[byOf(i) * gn + bxOf(i) + 1]++;
  for (let b = 0; b < gn * gn; b++) starts[b + 1] += starts[b];
  const items = new Uint32Array(n);
  const cursor = starts.slice(0, gn * gn);
  for (let i = 0; i < n; i++) items[cursor[byOf(i) * gn + bxOf(i)]++] = i;
  // ---- synapse tiers as flat arrays (screen-space stroke + bbox culling) ----
  const tierOf = w => w >= 8 ? 3 : w >= 4 ? 2 : w >= 2 ? 1 : 0;
  const tiers = SYN_TIERS.map(t => ({min: t.min, w: t.w, op: t.op, n: 0, fill: 0}));
  for (const e of xEdges) tiers[tierOf(e.w)].n++;
  for (const t of tiers) {
    t.ax = new Float32Array(t.n); t.ay = new Float32Array(t.n);
    t.bx = new Float32Array(t.n); t.by = new Float32Array(t.n);
  }
  for (const e of xEdges) {
    const t = tiers[tierOf(e.w)], j = t.fill++;
    t.ax[j] = e.ax; t.ay[j] = e.ay; t.bx[j] = e.bx; t.by[j] = e.by;
  }
  xHover = -1;
  xr = {n, X, Y, R, flags, labs, rank, maxR, tiers,
        grid: {minx, miny, cw, ch, gn, starts, items},
        vis: new Uint32Array(n), stamp: new Int32Array(n), frame: 0,
        minClickW: Infinity};
}
// visible-node gather through the grid; stamps this frame's visibility set
function xCollectVisible(x0, y0, x1, y1) {
  const g = xr.grid, gn = g.gn, X = xr.X, Y = xr.Y;
  const cl = (v, w) => Math.min(gn - 1, Math.max(0, Math.floor((v) / w)));
  const bx0 = cl(x0 - g.minx, g.cw), bx1 = cl(x1 - g.minx, g.cw);
  const by0 = cl(y0 - g.miny, g.ch), by1 = cl(y1 - g.miny, g.ch);
  const vis = xr.vis, stamp = xr.stamp, f = ++xr.frame;
  let m = 0;
  for (let by = by0; by <= by1; by++)
    for (let bx = bx0; bx <= bx1; bx++) {
      const b = by * gn + bx;
      for (let s = g.starts[b], e = g.starts[b + 1]; s < e; s++) {
        const i = g.items[s];
        if (X[i] >= x0 && X[i] <= x1 && Y[i] >= y0 && Y[i] <= y1) {
          vis[m++] = i; stamp[i] = f;
        }
      }
    }
  return m;
}
// nearest dot under the pointer (world coords), through the grid — the hit
// radius is the dot's own rendered radius + a 4px grace ring
function xHitNode(wx, wy, k) {
  if (!xr) return -1;
  const g = xr.grid, gn = g.gn, X = xr.X, Y = xr.Y, R = xr.R;
  const grace = 4 / k, floor = DOT_PX / k;
  const reach = xr.maxR + Math.max(floor, DOT_PX) + grace;
  const cl = (v, w) => Math.min(gn - 1, Math.max(0, Math.floor((v) / w)));
  const bx0 = cl(wx - reach - g.minx, g.cw), bx1 = cl(wx + reach - g.minx, g.cw);
  const by0 = cl(wy - reach - g.miny, g.ch), by1 = cl(wy + reach - g.miny, g.ch);
  let best = -1, bd = Infinity;
  for (let by = by0; by <= by1; by++)
    for (let bx = bx0; bx <= bx1; bx++) {
      const b = by * gn + bx;
      for (let s = g.starts[b], e = g.starts[b + 1]; s < e; s++) {
        const i = g.items[s];
        const dx = X[i] - wx, dy = Y[i] - wy, d2 = dx * dx + dy * dy;
        const rr = Math.max(R[i], floor) + grace;
        if (d2 <= rr * rr && d2 < bd) { bd = d2; best = i; }
      }
    }
  return best;
}
function drawXFrame() {
  if (!xr || !layout || !layout.explorer) return;
  const t0 = performance.now();
  const t = d3.zoomTransform(svg.node());
  const k = t.k || 1, tx = t.x, ty = t.y;
  const W = stageEl.clientWidth || 800, H = stageEl.clientHeight || 600;
  const dpr = window.devicePixelRatio || 1;
  if (xcv.width !== Math.round(W * dpr) || xcv.height !== Math.round(H * dpr)) {
    xcv.width = Math.round(W * dpr); xcv.height = Math.round(H * dpr);
    xcv.style.width = W + "px"; xcv.style.height = H + "px";
  }
  xctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  xctx.clearRect(0, 0, W, H);
  const X = xr.X, Y = xr.Y, R = xr.R, flags = xr.flags, vis = xr.vis;
  // world-space viewport, padded by the biggest dot + label room at this zoom
  const pad = xr.maxR + (DOT_PX + 12) / k;
  const x0 = -tx / k - pad, x1 = (W - tx) / k + pad;
  const y0 = -ty / k - pad, y1 = (H - ty) / k + pad;
  const m = xCollectVisible(x0, y0, x1, y1);
  // ---- edges: count visible per tier, pick the LOD, then draw ---------------
  let edges = 0, lod = "far";
  const perTier = [0, 0, 0, 0];
  xr.minClickW = Infinity;
  if (k >= XK_EDGE_MIN) {
    const cnt = [0, 0, 0, 0];
    let total = 0;
    for (let ti = 0; ti < 4; ti++) {
      const T = xr.tiers[ti];
      let c = 0;
      for (let e = 0; e < T.n; e++) {
        const ax = T.ax[e], ay = T.ay[e], bx = T.bx[e], by = T.by[e];
        if ((ax < x0 && bx < x0) || (ax > x1 && bx > x1) ||
            (ay < y0 && by < y0) || (ay > y1 && by > y1)) continue;
        c++;
      }
      cnt[ti] = c; total += c;
    }
    // near draws EVERYTHING visible (XE_BUDGET only chooses the mid threshold)
    const near = k >= XK_NEAR || total <= XE_BUDGET;
    lod = near ? "near" : "mid";
    let loTier = 0;   // lightest tier this frame draws
    if (!near) {
      let acc = 0;
      loTier = 4;
      for (let ti = 3; ti >= 0; ti--) {
        if (acc + cnt[ti] > XE_BUDGET) break;   // lighter tiers stay off too
        acc += cnt[ti]; loTier = ti;
      }
    }
    for (let ti = 3; ti >= loTier; ti--) {
      const T = xr.tiers[ti];
      if (!cnt[ti]) { xr.minClickW = Math.min(xr.minClickW, T.min); continue; }
      xctx.beginPath();
      for (let e = 0; e < T.n; e++) {
        const ax = T.ax[e], ay = T.ay[e], bx = T.bx[e], by = T.by[e];
        if ((ax < x0 && bx < x0) || (ax > x1 && bx > x1) ||
            (ay < y0 && by < y0) || (ay > y1 && by > y1)) continue;
        xctx.moveTo(ax * k + tx, ay * k + ty);
        xctx.lineTo(bx * k + tx, by * k + ty);
      }
      xctx.globalAlpha = T.op; xctx.strokeStyle = SYN_COLOR;
      xctx.lineWidth = T.w; xctx.stroke();
      perTier[ti] = cnt[ti]; edges += cnt[ti];
      xr.minClickW = T.min;   // only what this frame draws is click-inspectable
    }
  }
  // ---- dots: one fill pass per colour, gold rings after ---------------------
  xctx.globalAlpha = 0.9;
  const TAU = Math.PI * 2;
  for (let pass = 0; pass < 2; pass++) {
    xctx.beginPath();
    for (let v = 0; v < m; v++) {
      const i = vis[v];
      if ((flags[i] & 1) !== pass) continue;
      const sx = X[i] * k + tx, sy = Y[i] * k + ty;
      const r = Math.max(R[i] * k, DOT_PX);
      xctx.moveTo(sx + r, sy); xctx.arc(sx, sy, r, 0, TAU);
    }
    xctx.fillStyle = pass ? CELL_FORMAL : CELL_INFORMAL;
    xctx.fill();
  }
  xctx.globalAlpha = 1;
  xctx.beginPath();
  for (let v = 0; v < m; v++) {
    const i = vis[v];
    if (!(flags[i] & 2)) continue;
    const sx = X[i] * k + tx, sy = Y[i] * k + ty;
    const r = Math.max(R[i] * k, DOT_PX);
    xctx.moveTo(sx + r, sy); xctx.arc(sx, sy, r, 0, TAU);
  }
  xctx.strokeStyle = GOLD; xctx.lineWidth = RING_PX; xctx.stroke();
  // hover + selection rings (screen-space, like circle.dot:hover / .selring)
  if (xHover >= 0 && xr.stamp[xHover] === xr.frame) {
    const sx = X[xHover] * k + tx, sy = Y[xHover] * k + ty;
    xctx.beginPath();
    xctx.arc(sx, sy, Math.max(R[xHover] * k, DOT_PX) + 1, 0, TAU);
    xctx.strokeStyle = "#38bdf8"; xctx.lineWidth = 2; xctx.stroke();
  }
  const S = selectedId && layout.items.get(selectedId);
  if (S) {
    xctx.beginPath();
    xctx.arc(S.x * k + tx, S.y * k + ty, Math.max(S.r * k, DOT_PX) + 4, 0, TAU);
    xctx.strokeStyle = "#38bdf8"; xctx.lineWidth = 2.5; xctx.stroke();
  }
  // ---- labels: near tier only — the largest visible cells, budget by zoom ---
  let labels = 0;
  if (lod === "near") {
    const lim = Math.min(XL_CAP, Math.max(12, Math.round(600 * k * k)));
    xctx.fillStyle = "#e8e6e1"; xctx.textAlign = "center";
    xctx.font = LABEL_PX + 'px Georgia,"Iowan Old Style","Times New Roman",serif';
    // screen-space declutter: one label per ~110×15px bucket (a label claims
    // its own bucket and both horizontal neighbours). The relaxation compacts
    // a filtered graph into one clump, and without this the rank pass painted
    // the settled core as a solid white smear of 250 overlapping strings.
    // Deterministic: rank order in, first claim wins.
    const LB_W = 110, LB_H = 15;
    const cols = Math.max(1, Math.ceil(W / LB_W)), rows = Math.max(1, Math.ceil(H / LB_H));
    const taken = new Uint8Array(cols * rows);
    const rank = xr.rank;
    for (let q = 0; q < rank.length && labels < lim; q++) {
      const i = rank[q];
      if (xr.stamp[i] !== xr.frame) continue;
      const sx = X[i] * k + tx, sy = Y[i] * k + ty + Math.max(R[i] * k, DOT_PX) + 10;
      const row = Math.floor(sy / LB_H);
      if (row < 0 || row >= rows) continue;
      const c0 = Math.max(0, Math.floor((sx - LB_W / 2) / LB_W));
      const c1 = Math.min(cols - 1, Math.floor((sx + LB_W / 2) / LB_W));
      let free = true;
      for (let c = c0; c <= c1; c++) if (taken[row * cols + c]) { free = false; break; }
      if (!free) continue;
      for (let c = c0; c <= c1; c++) taken[row * cols + c] = 1;
      xctx.fillText(xr.labs[i], sx, sy);
      labels++;
    }
  }
  const st = window.__xstats;
  st.lod = lod; st.nodes = m; st.edges = edges; st.perTier = perTier;
  st.labels = labels; st.drawMs = performance.now() - t0;
  if (xInputT) {   // interaction → this frame's completion, for the perf gate
    if (st.i2f.length < 400) st.i2f.push(performance.now() - xInputT);
    xInputT = 0;
  }
}
window.__xdraw = drawXFrame;   // debug hook: force one synchronous frame
// ---- (E) the relaxation solver ---------------------------------------------
// A bounded Fruchterman–Reingold pass over the FILTERED subgraph, seeded from
// the build xy. Repulsion is SHORT-RANGE only (cutoff 3·RELAX_KK through a
// per-iteration uniform grid) — the long-range kind is exactly what
// brain/layout.py exists to avoid (see the layout doctrine above fetchExplorerData).
// Attraction runs along every induced synapse, weight-scaled, so the survivors
// pull together across the holes the predicate cut. Everything is deterministic:
// fixed RELAX_ITERS, array-order loops, a golden-angle (index-derived) push for
// coincident dots instead of random jitter.
window.__xrelax = null;   // completion telemetry {n, edges, iters, ms, hash, px, py, …}
function cancelXRelax() {
  xRelax = null;
  // the 300ms settle camera refit must not leak into the next view — an
  // interrupted transition would otherwise keep writing the zoom transform
  svg.interrupt("relaxfit");
}
// FNV-1a over the raw Float64 bytes of the positions — the determinism receipt
function xPosHash(px, py) {
  const dv = new DataView(new ArrayBuffer(8));
  let h = 0x811c9dc5 >>> 0;
  const mix = v => { dv.setFloat64(0, v);
    for (let b = 0; b < 8; b++) { h = (h ^ dv.getUint8(b)) >>> 0; h = Math.imul(h, 0x01000193) >>> 0; } };
  for (let i = 0; i < px.length; i++) { mix(px[i]); mix(py[i]); }
  return ("0000000" + h.toString(16)).slice(-8);
}
function startXRelax(leaves, statusBase) {
  const n = leaves.length;
  const px = new Float64Array(n), py = new Float64Array(n);
  let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
  for (let i = 0; i < n; i++) {
    px[i] = leaves[i].x; py[i] = leaves[i].y;
    if (px[i] < minx) minx = px[i]; if (px[i] > maxx) maxx = px[i];
    if (py[i] < miny) miny = py[i]; if (py[i] > maxy) maxy = py[i];
  }
  const spread = Math.max(maxx - minx, maxy - miny, 1);
  const S = {
    it: 0, n, px, py, leaves, statusBase, t0: performance.now(),
    edges: xEdges,   // the induced synapses (ai/bi index `leaves`)
    dx: new Float64Array(n), dy: new Float64Array(n),
    // travel budget: enough temperature to cross the seed footprint, cooling
    // linearly to zero so the layout SETTLES (never oscillates forever)
    tmax: Math.max(RELAX_KK * 4, spread * 0.05),
    neighCapHits: 0, chunks: 0, maxChunkMs: 0, workMs: 0,
  };
  xRelax = S;
  requestAnimationFrame(() => xRelaxFrame(S));
  // the FIRST chunk needs the hidden-tab fallback too (a background deep link
  // never gets an rAF at all — measured: it sat at "relaxing…", it=0, forever)
  S.timer = setTimeout(() => xRelaxFrame(S), 120);
}
function xRelaxIter(S) {
  const n = S.n, px = S.px, py = S.py, dx = S.dx, dy = S.dy;
  const kk = RELAX_KK, rc = RELAX_KK * 3, rc2 = rc * rc;
  dx.fill(0); dy.fill(0);
  // short-range repulsion through a fresh uniform grid (CSR, cell ≈ the cutoff;
  // O(n) to build, and its traversal order is a pure function of the positions
  // — the pass stays deterministic)
  let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
  for (let i = 0; i < n; i++) {
    if (px[i] < minx) minx = px[i]; if (px[i] > maxx) maxx = px[i];
    if (py[i] < miny) miny = py[i]; if (py[i] > maxy) maxy = py[i];
  }
  const gn = Math.max(1, Math.min(256,
    Math.ceil(Math.max(maxx - minx, maxy - miny, 1) / rc)));
  const cw = Math.max((maxx - minx) / gn, 1e-6);
  const ch = Math.max((maxy - miny) / gn, 1e-6);
  const bxOf = i => Math.min(gn - 1, Math.max(0, Math.floor((px[i] - minx) / cw)));
  const byOf = i => Math.min(gn - 1, Math.max(0, Math.floor((py[i] - miny) / ch)));
  const starts = new Uint32Array(gn * gn + 1);
  for (let i = 0; i < n; i++) starts[byOf(i) * gn + bxOf(i) + 1]++;
  for (let b = 0; b < gn * gn; b++) starts[b + 1] += starts[b];
  const items = new Uint32Array(n), cursor = starts.slice(0, gn * gn);
  for (let i = 0; i < n; i++) items[cursor[byOf(i) * gn + bxOf(i)]++] = i;
  for (let i = 0; i < n; i++) {
    const bx = bxOf(i), by = byOf(i);
    const cy0 = Math.max(0, by - 1), cy1 = Math.min(gn - 1, by + 1);
    const cx0 = Math.max(0, bx - 1), cx1 = Math.min(gn - 1, bx + 1);
    let seen = 0;
    for (let cy = cy0; cy <= cy1 && seen < RELAX_NEIGH_MAX; cy++)
      for (let cx = cx0; cx <= cx1 && seen < RELAX_NEIGH_MAX; cx++) {
        const b = cy * gn + cx;
        for (let s = starts[b], e = starts[b + 1];
             s < e && seen < RELAX_NEIGH_MAX; s++) {
          const j = items[s];
          if (j === i) continue;
          let ddx = px[i] - px[j], ddy = py[i] - py[j];
          const d2 = ddx * ddx + ddy * ddy;
          if (d2 >= rc2) continue;
          seen++;
          let d = Math.sqrt(d2);
          if (d < 1e-6) {   // coincident: deterministic per-index golden-angle push
            const a = i * 2.39996322972865332;
            ddx = Math.cos(a); ddy = Math.sin(a); d = 1;
          }
          const f = (kk * kk) / Math.max(d, 0.5) * (1 - d / rc);
          dx[i] += (ddx / d) * f; dy[i] += (ddy / d) * f;
        }
      }
    if (seen >= RELAX_NEIGH_MAX) S.neighCapHits++;
  }
  // attraction along every induced synapse (heavier bonds pull a little harder)
  for (const e of S.edges) {
    const ai = e.ai, bi = e.bi;
    const ddx = px[bi] - px[ai], ddy = py[bi] - py[ai];
    const d = Math.sqrt(ddx * ddx + ddy * ddy);
    if (d < 1e-6) continue;
    // FR attraction d²/kk along the unit vector = delta · (d/kk), weight-scaled
    const m = (d / kk) * (1 + 0.25 * Math.log2(1 + Math.min(e.w || 1, 8)));
    dx[ai] += ddx * m; dy[ai] += ddy * m;
    dx[bi] -= ddx * m; dy[bi] -= ddy * m;
  }
  // apply, per-node displacement capped by the cooling temperature
  const temp = S.tmax * (1 - S.it / RELAX_ITERS);
  for (let i = 0; i < n; i++) {
    const len = Math.sqrt(dx[i] * dx[i] + dy[i] * dy[i]);
    if (len < 1e-9) continue;
    const s = Math.min(len, temp) / len;
    px[i] += dx[i] * s; py[i] += dy[i] * s;
  }
}
// positions → leaves + edge endpoints + a refreshed render state, so culling,
// labels, the edge tiers and every hit test track the moved dots mid-animation
// (never a stale grid under a live pointer)
function xRelaxSync(S) {
  for (let i = 0; i < S.n; i++) { S.leaves[i].x = S.px[i]; S.leaves[i].y = S.py[i]; }
  for (const e of S.edges) {
    e.ax = S.px[e.ai]; e.ay = S.py[e.ai];
    e.bx = S.px[e.bi]; e.by = S.py[e.bi];
  }
  xRefreshGeom(S);
}
// geometry-only refresh: during a relax only POSITIONS move — labels, radii,
// flags, rank and the tier allocations are all invariant, so refill X/Y, the
// CSR grid and the tier endpoints IN PLACE instead of rebuilding the whole
// render state per chunk (measured: buildXState-per-chunk spent ~3ms of every
// ~7ms chunk re-deriving invariants, and reallocating typed arrays each frame)
function xRefreshGeom(S) {
  if (!xr || xr.n !== S.n) { buildXState(S.leaves); return; }   // defensive: scope changed
  const n = S.n, X = xr.X, Y = xr.Y;
  for (let i = 0; i < n; i++) { X[i] = S.px[i]; Y[i] = S.py[i]; }
  let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
  for (let i = 0; i < n; i++) {
    if (X[i] < minx) minx = X[i]; if (X[i] > maxx) maxx = X[i];
    if (Y[i] < miny) miny = Y[i]; if (Y[i] > maxy) maxy = Y[i];
  }
  if (!(minx <= maxx)) { minx = miny = 0; maxx = maxy = 1; }
  const g = xr.grid, gn = g.gn;
  g.minx = minx; g.miny = miny;
  g.cw = Math.max((maxx - minx) / gn, 1e-6);
  g.ch = Math.max((maxy - miny) / gn, 1e-6);
  const bxOf = i => Math.min(gn - 1, Math.max(0, Math.floor((X[i] - g.minx) / g.cw)));
  const byOf = i => Math.min(gn - 1, Math.max(0, Math.floor((Y[i] - g.miny) / g.ch)));
  g.starts.fill(0);
  for (let i = 0; i < n; i++) g.starts[byOf(i) * gn + bxOf(i) + 1]++;
  for (let b = 0; b < gn * gn; b++) g.starts[b + 1] += g.starts[b];
  const cursor = g.starts.slice(0, gn * gn);
  for (let i = 0; i < n; i++) g.items[cursor[byOf(i) * gn + bxOf(i)]++] = i;
  const tierOf = w => w >= 8 ? 3 : w >= 4 ? 2 : w >= 2 ? 1 : 0;   // == buildXState's
  for (const t of xr.tiers) t.fill = 0;
  for (const e of xEdges) {
    const t = xr.tiers[tierOf(e.w)], j = t.fill++;
    t.ax[j] = e.ax; t.ay[j] = e.ay; t.bx[j] = e.bx; t.by[j] = e.by;
  }
}
function xRelaxFrame(S) {
  // the solver dies silently when superseded: a newer render pass called
  // cancelXRelax / started its own solve, or the reader left the explorer
  if (xRelax !== S || !layout || !layout.explorer) return;
  clearTimeout(S.timer);   // whichever driver fired, the other stands down
  const t0 = performance.now();
  // the FIRST chunk runs a single iteration: the solver's code is cold (JIT +
  // the render pass's own GC debris) and a full chunk measured 105ms on boot —
  // one warm-up iteration bounds that window. Chunking never changes the math.
  // adaptive: after a chunk overshoots the budget (cold JIT / GC), halve the
  // per-frame iterations so the 10ms budget is a bound in practice, not advice
  const cap = S.chunks === 0 ? 1
    : (S.maxChunkMs || 0) > RELAX_CHUNK_MS * 1.6 ? Math.max(2, RELAX_ITERS_PER_FRAME >> 1)
    : RELAX_ITERS_PER_FRAME;
  let ran = 0;
  while (S.it < RELAX_ITERS && ran < cap &&
         performance.now() - t0 < RELAX_CHUNK_MS) {
    xRelaxIter(S);
    S.it++; ran++;
  }
  xRelaxSync(S);
  const ms = performance.now() - t0;
  S.chunks++; S.workMs += ms; if (ms > S.maxChunkMs) S.maxChunkMs = ms;
  scheduleXDraw();
  if (S.it < RELAX_ITERS) {
    requestAnimationFrame(() => xRelaxFrame(S));
    // rAF pauses in hidden tabs (scheduleXDraw's own trap): without a fallback
    // a background /brain deep link would sit at "relaxing…" until focused.
    // The slow timer keeps the solve finishing unwatched; when the tab is
    // visible the rAF always fires first and clears it. Either driver runs the
    // SAME iterations in the same order — determinism is untouched.
    S.timer = setTimeout(() => xRelaxFrame(S), 120);
    return;
  }
  // settled — publish the determinism receipt + say what happened, honestly.
  // wallMs counts idle time too (a background tab throttles both drivers to
  // ~1s ticks — measured 142s of wall for 88ms of work), so the reader-facing
  // line reports nothing time-based, only what the layout now IS.
  window.__xrelax = {n: S.n, edges: S.edges.length, iters: S.it,
    wallMs: performance.now() - S.t0, workMs: S.workMs,
    chunks: S.chunks, maxChunkMs: S.maxChunkMs, neighCapHits: S.neighCapHits,
    hash: xPosHash(S.px, S.py), px: S.px.slice(), py: S.py.slice()};
  xRelax = null;
  statusEl.textContent = S.statusBase +
    `relaxed layout — deterministic (${RELAX_ITERS} iterations)`;
  // the pull-in contracts the graph, so the camera fitted to the SEED extent
  // now frames mostly void (measured: the settled 791-cell subgraph drew as a
  // ~60px blob in a corner of a 990px stage — the dot-in-a-void failure class
  // again, moved into the camera). Refit to the SETTLED mass — unless the
  // reader panned/zoomed during the relax: their view is theirs, never yanked.
  if (!S.userMoved) {
    const t = explorerCameraTransform(S.leaves);
    if (t) {
      const settleT = performance.now(), layAtSettle = layout;
      svg.transition("relaxfit").duration(300).ease(d3.easeCubicInOut)
        .call(zoomBehav.transform, t);
      // d3 transitions pause in background tabs (fadeIn's own trap) — snap to
      // the exact final camera once the window passed. Guarded three ways: the
      // reader gestured (their view), a newer solve started, or the scene was
      // re-rendered (layout identity) — in each case this camera is stale.
      setTimeout(() => {
        if (layout === layAtSettle && !xRelax && xLastGestureT < settleT)
          svg.interrupt("relaxfit").call(zoomBehav.transform, t);
      }, 700);
    }
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
  if (!xr) return;
  // gViewport no longer tracks the explorer's zoom (the canvas reads the
  // transform itself), so invert the SVG-space pointer through the transform
  const t = d3.zoomTransform(svg.node());
  const k = t.k || 1;
  const p = t.invert(d3.pointer(ev, svg.node()));
  // dots first: the canvas has no per-circle handlers, so the stage click IS
  // the node click — resolved through the grid, exactly like hover
  const i = xHitNode(p[0], p[1], k);
  if (i >= 0) { nodeClick(layout.leaves[i].data); return; }
  if (!xEdges.length) return;
  const tol = 7 / k;                    // a constant on-screen grab radius
  let best = null, bd = tol * tol;
  for (const e of xEdges) {             // click-only — never runs per move
    if (e.w < xr.minClickW) continue;   // only edges the frame DRAWS are clickable
    const d2 = segDist2(p[0], p[1], e.ax, e.ay, e.bx, e.by);
    if (d2 < bd) { bd = d2; best = e; }
  }
  if (best) showSynapsePanel(best.a, best.b, {a: best.a, b: best.b, w: best.w});
}
// hover: grid hit test per move (never an O(n) scan), native tooltip on the
// stage — the level views' <title> affordance, now at EVERY explorer scope
svg.on("mousemove.xhover", ev => {
  if (!xr || !layout || !layout.explorer) return;
  const t = d3.zoomTransform(svg.node());
  const p = t.invert(d3.pointer(ev, svg.node()));
  const i = xHitNode(p[0], p[1], t.k || 1);
  if (i === xHover) return;
  xHover = i;
  svg.node().style.cursor = i >= 0 ? "pointer" : "";
  const d = i >= 0 ? layout.leaves[i].data : null;
  stageEl.title = d ? (d.label || d.id) +
    (((d.f || 0) & 1) ? " — carries a hand-written @[wikidata] tag" : "") : "";
  scheduleXDraw();   // repaint the hover ring (coalesced)
});
svg.on("mouseleave.xhover", () => {
  if (xHover === -1) return;
  xHover = -1;
  if (xr) { svg.node().style.cursor = ""; stageEl.title = ""; scheduleXDraw(); }
});
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
// the fit-to-mass camera transform for a set of leaves (render + relax-settle)
function explorerCameraTransform(leaves) {
  if (!leaves.length) return null;
  const W = stageEl.clientWidth || 800, H = stageEl.clientHeight || 600;
  const mid = a => a.length ? a.slice().sort((p, q) => p - q)[a.length >> 1] : 0;
  const cx = mid(leaves.map(l => l.x)), cy = mid(leaves.map(l => l.y));
  const rad = leaves.map(l => Math.hypot(l.x - cx, l.y - cy)).sort((a, b) => a - b);
  const rFit = Math.max(fitRadius(rad), 1);
  const pad = leaves[0] ? leaves[0].r * 2 : 0;
  const bw = (rFit + pad) * 2, bh = bw;
  const k = Math.max(0.02, Math.min(2, Math.min((W - 70) / bw, (H - 70) / bh)));
  return d3.zoomIdentity.translate(W / 2 - k * cx, H / 2 - k * cy).scale(k);
}
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
  cancelXRelax();   // a superseded solve must not keep mutating the old scene
  await ensureTree();
  // the explorer is a view the library filter reshapes — the panel must offer
  // the Libraries control even on a cold deep link with nothing selected
  if (!selectedId) renderPanel(focusId || ROOTS_ID);
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
  // (V) the SHARED visibility predicate, routed behind this one boundary: the
  // explorer's keep[] asks cellVisible (libraries + facets) — never a local
  // mask re-implementation. With no filters active it keeps everything, so the
  // restore path re-reads the build xy verbatim, as before.
  const visRow = i => cellVisible(nodes[i].id, nodes[i].f || 0);
  const keep = new Uint8Array(totalN);
  let universeN = totalN;   // the scope's honest universe, for "N of M" counts
  if (!scope) {
    for (let i = 0; i < totalN; i++) keep[i] = visRow(i) ? 1 : 0;
  } else {
    const core = new Uint8Array(totalN);
    for (let i = 0; i < totalN; i++) if (nodes[i].p && inSub(nodes[i].p)) core[i] = 1;
    const touch = core.slice();
    for (const [i, k2] of j.edges) {   // one ripple: homeless atoms hanging off the core
      if (core[i]) touch[k2] = 1;
      if (core[k2]) touch[i] = 1;
    }
    universeN = 0;
    for (let i = 0; i < totalN; i++) {
      if (touch[i]) universeN++;
      keep[i] = (touch[i] && visRow(i)) ? 1 : 0;
    }
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
    xEdges.push({a: A.data.id, b: B.data.id, w, ax: A.x, ay: A.y, bx: B.x, by: B.y,
                 ai: idxOf[i], bi: idxOf[k2]});   // leaf indices, for the solver
  }
  layout = {items: new Map(leaves.map(l => [l.data.id, l])), leaves, explorer: true};
  edgeStore = [];
  // the canvas engine owns the whole scene — every SVG group empties, so the
  // event surface on top paints nothing and costs nothing per frame
  gEdges.selectAll("*").remove();
  gOverlay.selectAll("*").remove();
  gBubbles.selectAll("*").remove();
  gLabels.selectAll("*").remove();
  buildXState(leaves);
  flistShow(false);   // the explorer's canvas owns the stage — the queue is gone
  xcanvasShow(true);
  scheduleXDraw();   // even a 0-leaf scope paints (clears) a frame — never stale pixels
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
  const t = explorerCameraTransform(leaves);
  if (t) {
    svg.call(zoomBehav.transform, t);
    applyExplorerScale(t.k);
  }
  const scopeLabel = scope ? scope.slice(5) : "all libraries";
  const filtered = filtersActive();
  // (E) a FILTERED subgraph small enough pulls into its own shape; everything
  // else keeps the build xy verbatim — and the status line says WHICH, always
  // (no-silent-filter rule: an above-cap skip is announced, never implied)
  const relaxing = filtered && leaves.length > 1 && leaves.length <= RELAX_CAP;
  updateFilterStat({active: filtered, shown: leaves.length, total: universeN,
    text: filtered ? `${leaves.length.toLocaleString()} of ${universeN.toLocaleString()} cells match` : ""});
  updateHiddenChip(universeN - leaves.length);
  crumbEl.innerHTML = `<a data-nav="${ROOTS_ID}">all libraries</a>
    <span class="sep">/</span> <b>${esc(scopeLabel)} · explorer</b>`;
  crumbEl.querySelectorAll("[data-nav]").forEach(a =>
    a.addEventListener("click", () => {
      setExplorer(false); focusId = ROOTS_ID; selectedId = null;
      setHash(""); renderFocus(true); renderPanel(ROOTS_ID);
    }));
  updateFrontierToggle();   // the explorer writes its own crumb — hide the toggle here too
  const statusBase = `explorer: ${leaves.length.toLocaleString()}${filtered
      ? ` of ${universeN.toLocaleString()}` : ""} cells${filtered ? " shown" : ""} · ${
    xEdges.length.toLocaleString()} synapses · ${scopeLabel} · `;
  statusEl.textContent = statusBase + (relaxing ? "relaxing…"
    : filtered && leaves.length > RELAX_CAP
      ? `build-time layout (relaxation under ${RELAX_CAP.toLocaleString()} nodes)`
      : "build-time layout");
  const el = $("#structstat");
  if (el) el.textContent = relaxing
    ? `deterministic relaxation — seeded from the build layout, ${RELAX_ITERS} fixed iterations`
    : "no client simulation — positions are solved at build time";
  if (relaxing) startXRelax(leaves, statusBase);
  // the canvas fade-in mirrors fadeIn(), background-tab guard included
  if (anim) {
    xcv.style.opacity = "0";
    requestAnimationFrame(() => { xcv.style.opacity = "1"; });
    setTimeout(() => { xcv.style.opacity = "1"; }, 600);
  } else xcv.style.opacity = "1";
}
zoomBehav.on("zoom.xplabels", ev => {
  // dots, strokes AND labels are all sized in screen space by the canvas frame;
  // this handler only timestamps the input and schedules ONE coalesced draw
  if (layout && layout.explorer) {
    if (ev.sourceEvent) {
      xInputT = performance.now();
      xLastGestureT = xInputT;
      if (xRelax) xRelax.userMoved = true;   // the settle-refit stands down
    }
    applyExplorerScale(ev.transform.k);
  }
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
// ---- the ONE filters-changed path (reshape contract V) ----------------------
// EVERY filter mutation — a facet chip, a library checkbox — funnels here:
// sync the controls + persistence, then re-render the CURRENT view in place.
// No reload, no zoom reset; the bubbles repack with a transition, the frontier
// re-shells with its dot animation, the explorer rebuilds its kept subgraph,
// and the visible panel re-reads its counts under the new predicate.
async function filtersChanged() {
  syncChips();
  syncLibCheckboxes();
  persistLibs(false);   // storage only — setHash below writes the URL state once
  setHash(focusId === ROOTS_ID ? "" : focusId || "");
  if (explorerOn) {
    xInputT = performance.now();   // (P) filter-toggle → first-frame telemetry (i2f)
    await renderExplorer(false);
  } else if (layout && (layout.frontier || layout.frontierList) && isFrontierViewId(focusId)) {
    await reScoreFrontier();   // in place: membership + radii/ranks; surviving dots glide
  } else if (layout && layout.ego) {
    await renderFocus(false, {keepZoom: true});
  } else {
    await renderFocus(false, {keepZoom: true, reshape: true});
  }
  if (lastPanelId && lastPanelId !== "__sources__") {
    const st = panelEl.scrollTop;
    await renderPanel(lastPanelId);
    panelEl.scrollTop = st;
  }
}
document.querySelectorAll(".fchip[data-fbit]").forEach(b =>
  b.addEventListener("click", () => {
    filterMask ^= Number(b.dataset.fbit);
    filtersChanged();
  }));
// '+N hidden' → the Libraries panel (the restore surface the contract names)
$("#hiddenchip").addEventListener("click", () => {
  lastPanelId = ROOTS_ID;
  rootsPanel();
  const sec = panelEl.querySelector("#libsec");
  if (sec) sec.scrollIntoView({block: "start"});
});
$("#explorerbtn").addEventListener("click", () => {
  setExplorer(!explorerOn);
  setHash(focusId === ROOTS_ID ? "" : focusId || "");
  if (explorerOn) renderExplorer(true);
  else renderFocus(true);
});
// the list|map toggle: travel within the ONE frontier surface pair; a focused
// area carries across (map sector ↔ queue filtered to that area)
$("#fv-list").addEventListener("click", () => {
  const v = frontierViewOf(focusId);
  if (!v || v.mode === "list") return;
  gotoFrontierView(v.area ? FRONTIER_ID + ":" + v.area.slice(9) : FRONTIER_ID);
});
$("#fv-map").addEventListener("click", () => {
  const v = frontierViewOf(focusId);
  if (!v || v.mode === "map") return;
  const one = flAreas.size === 1 ? [...flAreas][0] : v.area;
  gotoFrontierView(one ? FRONTIER_MAP_ID + ":" + one.slice(9) : FRONTIER_MAP_ID);
});

// travel within the ONE frontier view (full circle ↔ a sector focus) — the
// state IS the hash id (#__frontier__ / #__frontier__:<Area>), so both deep-link
function gotoFrontierView(id) {
  if (focusId === id) return;
  if (explorerOn) setExplorer(false);
  flClearAreasOnFullTravel(id);
  focusId = id;
  selectedId = null;
  setHash(id);
  renderPanel(id);
  renderFocus(true);
}

window.addEventListener("hashchange", async () => {
  const h = parseHash();
  filterMask = h.f;
  syncChips();
  // a &libs= in an incoming hash wins (a shared link renders what it says);
  // an absent param keeps the session's current library set
  if (h.libs !== null) {
    applyLibsList(h.libs);
    persistLibs(false);   // storage only — the hash already says it
    syncLibCheckboxes();
  }
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
// The FRONTIER view additionally watches the STAGE ELEMENT itself: its radial
// layout is keyed to min(W, H), and the stage can change height with NO window
// resize at all — the toolbar re-wraps when webfonts land and the stage flexes
// to fill. Measured at boot: the view stayed drawn for a stage ~40px taller
// than the one on screen, shifting the radial bands by ~3px. Scoped to the
// frontier view (the pannable level views keep the width-only guard above on
// purpose), and gated on the geometry the layout was actually solved for.
// The EXPLORER piggybacks here for its canvas: a height-only stage change never
// fires the width-gated resize handler, but the canvas backing store is sized
// in pixels — one coalesced redraw re-syncs it (drawXFrame reads the live size).
if (typeof ResizeObserver !== "undefined") {
  let stageRT = 0;
  new ResizeObserver(() => {
    clearTimeout(stageRT);
    stageRT = setTimeout(() => {
      if (layout && layout.explorer) { scheduleXDraw(); return; }
      if (layout && layout.frontierList) { flWindow(true); return; }   // re-window only
      if (!layout || !layout.frontier) return;
      const w = stageEl.clientWidth, h = stageEl.clientHeight;
      if (Math.abs(w - (layout.fvW || 0)) < 2 &&
          Math.abs(h - (layout.fvH || 0)) < 2) return;
      renderFocus(false);
    }, 120);
  }).observe(stageEl);
}

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
    await selectRelease();
    await fetchManifest();
  } catch (e) {
    statusEl.textContent = "brain release unavailable (" + e.message + ")";
    panelEl.innerHTML = `<p class="note">This page could not verify one consistent Brain release. Reload after publication completes.</p>`;
    return;
  }
  const c = manifest._meta.counts || {};
  statusEl.textContent = `${(c.cells || 0).toLocaleString()} cells · ` +
    `${(c.organs || 0).toLocaleString()} organs · ` +
    `${(c.synapses || 0).toLocaleString()} synapses · ` +
    `data ${manifest._meta.generated_at.slice(0, 10)}`;
  releaseEl.textContent = `release ${RELEASE_HEX.slice(0, 12)}`;
  releaseEl.title = RELEASE_ID;
  await ensureTree();
  const h = parseHash();
  filterMask = h.f;
  syncChips();
  initLibs(h.libs);   // the hash wins over localStorage; default = every library on
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


def write_page(output: Path) -> Path:
    """Write the historical repository-local page output."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(HTML, encoding="utf-8", newline="\n")
    return output


def build_brain_page_from_context(context: BuildContext) -> Path:
    """Publish the exact page output owned by the sealed replay stage."""
    context.require_stage(
        "brain-page",
        program="site/build_brain_page.py",
        argv=[],
        needs=[],
        outputs=[("file", "site/out/brain.html")],
    )
    output = context.output_for("brain-page", "site/out/brain.html")
    scratch = context.scratch_for("brain-page", "publish")
    scratch_file = scratch / "brain.html"
    assert_outputs_absent([output])
    ensure_private_directory(context.roots.output, output.parent)
    with owned_directory(context.roots.scratch, scratch) as ownership:
        write_bytes_exclusive(scratch_file, HTML.encode("utf-8"), mode=0o644)
        require_same_filesystem(scratch, output.parent)
        publish_files_no_replace([(scratch_file, output)], scratch=ownership)
    return output


def main() -> None:
    output = write_page(OUT_DIR / "brain.html")
    print(f"wrote {output} ({len(HTML) / 1024:.0f} KB)")


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-context", type=Path)
    parser.add_argument("--stage-id")
    args = parser.parse_args(argv)

    if args.build_context is None:
        if args.stage_id is not None:
            parser.error("--stage-id requires --build-context")
        main()
        return 0
    if args.stage_id != "brain-page":
        parser.error("--stage-id must be 'brain-page' with --build-context")
    context = BuildContext.load(args.build_context)
    output = build_brain_page_from_context(context)
    print(f"wrote {output} ({len(HTML) / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
