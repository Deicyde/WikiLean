// Dynamic homepage + sitemap from D1 (Wave D, contract D-C7). Replaces the
// static site/out/index.html + sitemap.xml that build-public.ts used to copy
// into wiki/public/ — articles created via PUT /api/article/:slug now appear
// without a static rebuild. index.ts serves homePage() cached in RENDER_CACHE
// ('page:home:v2', TTL 300s) and sitemapXml() ('page:sitemap:v1', TTL 3600s).
//
// Visual language: warm academic-minimalist — paper background, serif display
// headings (system stacks only, no external fonts), one deep-blue accent, and
// the three semantic status colors retuned to harmonize (green=formalized,
// amber=partial, red=not formalized). The directory is a scannable list with
// inline stacked coverage bars, filtered/sorted client-side.
// Counts come from articles.n_formalized/n_partial/n_not_formalized (D-C5);
// null = not yet backfilled and renders as a muted "pending".
//
// Self-contained by design: no imports from pages.ts / index.ts / engine.

export interface HomeRow {
  slug: string;
  displayTitle: string;
  nFormalized: number | null;
  nPartial: number | null;
  nNotFormalized: number | null;
  updatedAt: number;
}

export interface SitemapRow {
  slug: string;
  updatedAt: number;
}

const SITE_ORIGIN = "https://wikilean.jackmccarthy.org";

// Tiny local twin of engine/html.ts htmlEscape (copied to keep this module
// import-free). Numeric refs are valid in both HTML and XML.
function esc(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#x27;");
}

function fmtInt(n: number): string {
  return n.toLocaleString("en-US");
}

// Compact relative age for the "Recently updated" strip. The page is KV-cached
// for 300s, so these can be up to ~5 minutes stale — acceptable at this
// granularity. Clamped at 0 for clock skew / future timestamps.
function fmtRel(ms: number): string {
  const s = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  const d = Math.floor(s / 86400);
  if (d < 60) return d + "d ago";
  if (d < 365) return Math.floor(d / 30) + "mo ago";
  return Math.floor(d / 365) + "y ago";
}

function hasCounts(r: HomeRow): r is HomeRow & {
  nFormalized: number;
  nPartial: number;
  nNotFormalized: number;
} {
  return r.nFormalized !== null && r.nPartial !== null && r.nNotFormalized !== null;
}

// One directory entry: title | stacked coverage bar | formalized/total.
// data-* attributes drive the client-side filter + sort (data-title is the
// lowercased filter key; cov/f/n are sort keys; untagged feeds the hide
// checkbox — null-count rows behave as untagged with zeroed sort keys).
function dirRow(r: HomeRow): string {
  const t = esc(r.displayTitle);
  let bar: string;
  let meta: string;
  let cov = 0;
  let f = 0;
  let n = 0;
  let untagged = true;
  if (!hasCounts(r)) {
    // Pre-backfill row: counts are unknown, not zero (D-C5: null = pending).
    bar = '<span class="bar bar-empty"></span>';
    meta = '<span class="row-meta pending">pending</span>';
  } else {
    f = r.nFormalized;
    n = r.nFormalized + r.nPartial + r.nNotFormalized;
    if (n === 0) {
      bar = '<span class="bar bar-empty"></span>';
      meta = '<span class="row-meta untagged">untagged</span>';
    } else {
      untagged = false;
      cov = f / n;
      const wf = (100 * r.nFormalized) / n;
      const wp = (100 * r.nPartial) / n;
      const wn = (100 * r.nNotFormalized) / n;
      bar =
        `<span class="bar" title="${r.nFormalized} formalized · ${r.nPartial} partial · ${r.nNotFormalized} not formalized">` +
        `<i class="f" style="width:${wf.toFixed(1)}%"></i>` +
        `<i class="p" style="width:${wp.toFixed(1)}%"></i>` +
        `<i class="n" style="width:${wn.toFixed(1)}%"></i></span>`;
      meta = `<span class="row-meta">${r.nFormalized}<span class="of">/</span>${n}</span>`;
    }
  }
  return (
    // Slugs are URL-encoded for the href (then HTML-escaped, like every sink) —
    // raw "?"/"#"/"%" in a slug would otherwise break the link (W3 fix #10).
    `<a class="row" href="/${esc(encodeURIComponent(r.slug))}" ` +
    `data-title="${esc(r.displayTitle.toLowerCase())}" data-cov="${cov.toFixed(4)}" ` +
    `data-f="${f}" data-n="${n}" data-untagged="${untagged ? 1 : 0}">` +
    `<span class="row-title">${t}</span>${bar}${meta}</a>`
  );
}

function recentItem(r: HomeRow): string {
  return (
    // URL-encoded + HTML-escaped, same as dirRow (W3 fix #10).
    `<a class="recent-item" href="/${esc(encodeURIComponent(r.slug))}">` +
    `<span class="recent-title">${esc(r.displayTitle)}</span>` +
    `<span class="recent-when">${fmtRel(r.updatedAt)}</span></a>`
  );
}

export function homePage(rows: HomeRow[]): string {
  const sorted = [...rows].sort((a, b) =>
    a.displayTitle.toLowerCase().localeCompare(b.displayTitle.toLowerCase()),
  );
  const counted = sorted.filter(hasCounts);
  const tf = counted.reduce((s, r) => s + r.nFormalized, 0);
  const tp = counted.reduce((s, r) => s + r.nPartial, 0);
  const tn = counted.reduce((s, r) => s + r.nNotFormalized, 0);
  const grand = tf + tp + tn;
  const pctF = grand ? Math.round((100 * tf) / grand) : 0;
  const pctP = grand ? Math.round((100 * tp) / grand) : 0;
  const nUntagged = counted.filter((r) => r.nFormalized + r.nPartial + r.nNotFormalized === 0).length;
  const nPending = sorted.length - counted.length;
  const noteParts: string[] = [];
  if (nUntagged) noteParts.push(`<b>${fmtInt(nUntagged)}</b> not yet tagged`);
  if (nPending) noteParts.push(`<b>${fmtInt(nPending)}</b> awaiting count backfill`);
  const statsNote = noteParts.length
    ? `\n    <p class="stats-note">${noteParts.join(" &middot; ")}</p>`
    : "";
  const recent = [...sorted].sort((a, b) => b.updatedAt - a.updatedAt).slice(0, 8);
  const recentSection = recent.length
    ? `
  <section class="recent" aria-labelledby="recent-h">
    <div class="sect-head"><h2 id="recent-h">Recently updated</h2></div>
    <div class="recent-grid">
${recent.map(recentItem).join("\n")}
    </div>
  </section>`
    : "";

  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WikiLean — Wikipedia&#x27;s mathematics, annotated with its formalization status in Mathlib</title>
<meta name="description" content="WikiLean is a mirror of WikiProject Mathematics articles annotated with links into Mathlib4, color-coded by formalization coverage in Lean.">
<link rel="canonical" href="https://wikilean.jackmccarthy.org/">
<meta name="robots" content="index, follow, max-image-preview:large">
<meta property="og:type" content="website">
<meta property="og:site_name" content="WikiLean">
<meta property="og:title" content="WikiLean — Wikipedia mathematics, mapped to Lean">
<meta property="og:description" content="WikiLean is a mirror of WikiProject Mathematics articles annotated with links into Mathlib4, color-coded by formalization coverage in Lean.">
<meta property="og:url" content="https://wikilean.jackmccarthy.org/">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="WikiLean — Wikipedia mathematics, mapped to Lean">
<meta name="twitter:description" content="A mirror of WikiProject Mathematics articles annotated with links into Mathlib4, color-coded by formalization coverage in Lean.">
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@graph": [
    {
      "@type": "WebSite",
      "@id": "https://wikilean.jackmccarthy.org/#website",
      "url": "https://wikilean.jackmccarthy.org/",
      "name": "WikiLean",
      "alternateName": "WikiLean — Wikipedia mathematics, mapped to Lean",
      "description": "WikiLean is a mirror of WikiProject Mathematics articles annotated inline with links into Mathlib4, color-coded by whether each definition, theorem, and proof has been formalized in Lean.",
      "inLanguage": "en",
      "sameAs": ["https://github.com/Deicyde/WikiLean"],
      "publisher": {"@id": "https://wikilean.jackmccarthy.org/#person"}
    },
    {
      "@type": "Person",
      "@id": "https://wikilean.jackmccarthy.org/#person",
      "name": "Jack McCarthy",
      "url": "https://jackmccarthy.org"
    }
  ]
}
</script>
<script>
/* Set the theme attr BEFORE the stylesheet parses so dark mode applies on first
   paint (no flash). Priority: localStorage > OS preference > light default. The
   toggle in the header (script at the bottom of body) flips dark/light. */
(function(){try{var s=localStorage.getItem("wl-theme");
var t=s==="dark"||s==="light"?s:(window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light");
document.documentElement.dataset.theme=t;}catch(e){}})();
</script>
<style>
:root {
  --paper:#f7f4ee; --surface:#fffdf9; --ink:#1f1d1a; --muted:#5f594e;
  --line:#e6e0d2; --line-strong:#d8d0bd;
  --accent:#1a4b8c; --accent-dark:#163e74;
  --green:#2f7d4f; --yellow:#b08020; --red:#b3372f;
  --serif:Charter,'Bitstream Charter','Iowan Old Style',Georgia,'Times New Roman',serif;
}
/* Dark mode — remap the warm-light token palette to the shared dark scheme
   (matches pages.ts / style.css). One override block recolors everything that
   uses the vars; the few hardcoded colors below get explicit dark overrides. */
[data-theme="dark"] :root {
  --paper:#1a1816; --surface:#232020; --ink:#ebe5d8; --muted:#9a9081;
  --line:#3a3530; --line-strong:#4d4742; --accent:#6e9adf; --accent-dark:#8fb4e8;
  --green:#8fd4ad; --yellow:#e2bf78; --red:#f08e85;
}
/* Hardcoded-color overrides for dark mode (search placeholder, row-hover tint,
   coverage-bar track, the "/" separator) — kept legible on the dark surface. */
[data-theme="dark"] .search::placeholder { color:#8a8278; }
[data-theme="dark"] .row:hover { background:rgba(110,154,223,.08); }
[data-theme="dark"] .bar { background:#2e2a2f; }
[data-theme="dark"] .row-meta .of { color:#6e675a; }
/* Theme-toggle button (matches the article-page version in style.css). */
.wl-theme-toggle { background:transparent; border:1px solid var(--line-strong); color:var(--muted);
  border-radius:50%; width:28px; height:28px; padding:0; line-height:1; font-size:14px; cursor:pointer;
  display:inline-flex; align-items:center; justify-content:center; }
.wl-theme-toggle:hover { color:var(--ink); border-color:var(--accent); }
* { box-sizing:border-box; }
html { scroll-behavior:smooth; }
body { margin:0; background:var(--paper); color:var(--ink); line-height:1.55;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
.sr { position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden;
  clip:rect(0 0 0 0); white-space:nowrap; border:0; }
.wl-header { display:flex; align-items:baseline; justify-content:space-between;
  gap:8px 20px; flex-wrap:wrap; max-width:880px; margin:0 auto; padding:22px 20px 0; }
.wl-brand { font-family:var(--serif); font-weight:700; font-size:1.15rem;
  color:var(--ink); text-decoration:none; }
.wl-brand:hover { color:var(--accent); }
.wl-nav { display:flex; gap:6px 18px; flex-wrap:wrap; }
.wl-nav a { color:var(--accent); text-decoration:none; font-size:.88rem; }
.wl-nav a:hover { text-decoration:underline; }
.wrap { max-width:880px; margin:0 auto; padding:0 20px 56px; }
.hero { padding:42px 0 6px; }
h1 { font-family:var(--serif); font-size:2.5rem; line-height:1.1; letter-spacing:-.01em; margin:0 0 14px; }
.tagline { font-family:var(--serif); font-size:1.28rem; line-height:1.45; margin:0 0 10px; max-width:34em; }
.tagline a { color:inherit; text-decoration:underline; text-decoration-color:var(--line-strong); text-underline-offset:3px; }
.tagline a:hover { color:var(--accent); text-decoration-color:var(--accent); }
.sub { color:var(--muted); font-size:.95rem; max-width:44em; margin:0; }
.sub a { color:var(--accent); text-decoration:none; }
.sub a:hover { text-decoration:underline; }
.stats { display:flex; gap:16px 40px; flex-wrap:wrap; margin:30px 0 6px; }
.stat-num { display:block; font-family:var(--serif); font-weight:700; font-size:1.8rem;
  line-height:1.15; font-variant-numeric:tabular-nums; }
.stat-label { display:block; font-size:.72rem; letter-spacing:.08em; text-transform:uppercase;
  color:var(--muted); margin-top:2px; }
.stats-note { color:var(--muted); font-size:.82rem; margin:4px 0 0; }
.stats-note b { color:var(--ink); font-weight:600; }
.ctas { display:flex; gap:10px; flex-wrap:wrap; margin:26px 0 0; }
.btn { display:inline-block; padding:9px 16px; border-radius:7px; font-size:.92rem;
  text-decoration:none; border:1px solid var(--line-strong); color:var(--ink); background:var(--surface); }
.btn:hover { border-color:var(--accent); color:var(--accent); }
.btn-primary { background:var(--accent); border-color:var(--accent); color:#fff; }
.btn-primary:hover { background:var(--accent-dark); border-color:var(--accent-dark); color:#fff; }
.searchbar { margin:34px 0 0; }
.search { width:100%; padding:13px 16px; font-size:1.05rem; font-family:inherit;
  color:var(--ink); background:var(--surface); border:1px solid var(--line-strong); border-radius:9px; }
.search::placeholder { color:#6e675a; }
.search:focus { outline:2px solid var(--accent); outline-offset:0; border-color:var(--accent); }
.sect-head { display:flex; align-items:baseline; justify-content:space-between;
  gap:8px 18px; flex-wrap:wrap; margin:36px 0 12px; }
h2 { font-family:var(--serif); font-size:1.25rem; margin:0; }
.recent-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); gap:8px; }
.recent-item { display:flex; justify-content:space-between; align-items:baseline; gap:10px;
  min-width:0; background:var(--surface); border:1px solid var(--line); border-radius:7px;
  padding:9px 12px; text-decoration:none; color:var(--ink); font-size:.9rem; }
.recent-item:hover { border-color:var(--accent); }
.recent-item:hover .recent-title { color:var(--accent); }
.recent-title { font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.recent-when { color:var(--muted); font-size:.78rem; flex:none; }
.explore ul { list-style:none; margin:0; padding:0; }
.explore li { color:var(--muted); font-size:.9rem; line-height:1.5; margin:0 0 8px; }
.explore li a { color:var(--accent); font-weight:600; text-decoration:none; }
.explore li a:hover { text-decoration:underline; }
.dir-controls { display:flex; gap:14px; align-items:center; flex-wrap:wrap; }
.sort { padding:7px 10px; font-size:.85rem; font-family:inherit; color:var(--ink);
  background:var(--surface); border:1px solid var(--line-strong); border-radius:6px; }
.chk { display:flex; gap:6px; align-items:center; font-size:.85rem; color:var(--muted);
  cursor:pointer; white-space:nowrap; }
.chk input { accent-color:var(--accent); }
.legend { display:flex; gap:14px; flex-wrap:wrap; font-size:.78rem; color:var(--muted); margin:0 0 10px; }
.legend i { display:inline-block; width:10px; height:10px; border-radius:2px;
  margin-right:5px; vertical-align:-1px; }
.legend i.f { background:var(--green); }
.legend i.p { background:var(--yellow); }
.legend i.n { background:var(--red); }
.dir { border-top:1px solid var(--line-strong); }
.row { display:grid; grid-template-columns:minmax(0,1fr) 130px 64px; gap:14px; align-items:center;
  padding:9px 2px; border-bottom:1px solid var(--line); text-decoration:none; color:var(--ink); }
.row:hover { background:rgba(26,75,140,.05); }
.row:hover .row-title { color:var(--accent); }
.row-title { font-size:.95rem; font-weight:500; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.bar { display:flex; height:6px; border-radius:3px; overflow:hidden; background:#e9e3d6; }
.bar i { display:block; height:100%; }
.bar i.f { background:var(--green); }
.bar i.p { background:var(--yellow); }
.bar i.n { background:var(--red); }
.row-meta { font-size:.8rem; color:var(--muted); text-align:right;
  font-variant-numeric:tabular-nums; white-space:nowrap; }
.row-meta .of { color:#9a917f; padding:0 1px; }
.row-meta.untagged, .row-meta.pending { font-style:italic; }
.empty { color:var(--muted); padding:22px 0; font-style:italic; }
.searching .recent, .searching .explore { display:none; }
footer { margin-top:56px; padding-top:18px; border-top:1px solid var(--line-strong);
  color:var(--muted); font-size:.82rem; line-height:1.6; }
footer p { margin:4px 0; }
footer a { color:var(--accent); text-decoration:none; }
footer a:hover { text-decoration:underline; }
@media (max-width:540px) {
  h1 { font-size:2rem; }
  .tagline { font-size:1.1rem; }
  .stat-num { font-size:1.5rem; }
  .stats { gap:14px 26px; margin-top:24px; }
  .hero { padding-top:30px; }
  .row { grid-template-columns:minmax(0,1fr) 72px 56px; gap:10px; }
}
</style>
</head>
<body>
<header class="wl-header">
  <a class="wl-brand" href="/">WikiLean</a>
  <nav class="wl-nav" aria-label="Site">
    <a href="/concepts">Concepts</a>
    <a href="/wikifunctions">Wikifunctions</a>
    <a href="/article-graph">Article graph</a>
    <a href="/map">Map</a>
    <a href="/about">About &amp; method</a>
    <button id="wl-theme-toggle" class="wl-theme-toggle" type="button" aria-label="Toggle dark mode" title="Toggle dark mode">🌓</button>
  </nav>
</header>
<main class="wrap">
  <section class="hero">
    <h1>WikiLean</h1>
    <p class="tagline">Wikipedia&#x27;s mathematics, annotated with its formalization status in
      <a href="https://leanprover-community.github.io/mathlib4_docs/">Mathlib</a>.</p>
    <p class="sub">
      A mirror of <a href="https://en.wikipedia.org/wiki/Wikipedia:WikiProject_Mathematics">WikiProject
      Mathematics</a> articles with every definition, theorem, and proof linked to its Lean
      formalization where one exists. A project by <a href="https://jackmccarthy.org">Jack McCarthy</a>.
    </p>
    <div class="stats">
      <div class="stat"><span class="stat-num">${fmtInt(sorted.length)}</span><span class="stat-label">articles</span></div>
      <div class="stat"><span class="stat-num">${fmtInt(grand)}</span><span class="stat-label">annotated results</span></div>
      <div class="stat"><span class="stat-num">${pctF}%</span><span class="stat-label">formalized</span></div>
      <div class="stat"><span class="stat-num">${pctP}%</span><span class="stat-label">partial</span></div>
    </div>${statsNote}
    <div class="ctas">
      <a class="btn btn-primary" href="#directory">Browse articles</a>
      <a class="btn" href="/about">How to contribute</a>
      <a class="btn" href="/recent-changes">Recent changes</a>
    </div>
  </section>
  <div class="searchbar">
    <label class="sr" for="q">Filter articles</label>
    <input class="search" id="q" type="search" placeholder="Filter ${fmtInt(sorted.length)} articles&hellip;" autocomplete="off">
  </div>${recentSection}
  <section class="explore" aria-labelledby="explore-h">
    <div class="sect-head"><h2 id="explore-h">Datasets &amp; graphs</h2></div>
    <ul>
      <li><a href="/concepts">Wikidata concept links</a> &mdash; every formalized concept keyed
        to its Wikidata item, as an open RDF dataset (the basis for a proposed
        <em>&ldquo;formalized as (Lean/Mathlib)&rdquo;</em> Wikidata property).</li>
      <li><a href="/article-graph">Article graph</a> &mdash; articles clustered by shared Mathlib
        formalizations, colored by their dominant Mathlib area.</li>
      <li><a href="/map">Map</a> &mdash; Mathlib&#x27;s declaration-level dependency
        edges overlaid on Wikidata&#x27;s typed statements, on a shared node set.</li>
    </ul>
  </section>
  <section class="directory" id="directory" aria-labelledby="directory-h">
    <div class="sect-head">
      <h2 id="directory-h">All articles</h2>
      <div class="dir-controls">
        <select class="sort" id="sort" aria-label="Sort articles">
          <option value="title">Sort: A&ndash;Z</option>
          <option value="coverage">Sort: coverage</option>
          <option value="formalized">Sort: most formalized</option>
          <option value="count">Sort: most annotated</option>
        </select>
        <label class="chk"><input type="checkbox" id="hideUntagged"> Hide untagged</label>
      </div>
    </div>
    <div class="legend">
      <span><i class="f"></i>formalized</span>
      <span><i class="p"></i>partial</span>
      <span><i class="n"></i>not formalized</span>
    </div>
    <div class="dir" id="dir">
${sorted.map(dirRow).join("\n")}
    </div>
    <p class="empty" id="empty" style="display:none">No articles match.</p>
  </section>
  <footer>
    <p>Article text from <a href="https://en.wikipedia.org/wiki/Wikipedia:WikiProject_Mathematics">Wikipedia</a>,
      available under <a href="https://creativecommons.org/licenses/by-sa/4.0/">CC BY-SA 4.0</a>.
      WikiLean annotations are released under
      <a href="https://creativecommons.org/publicdomain/zero/1.0/">CC0</a>.</p>
    <p><a href="https://github.com/Deicyde/WikiLean">Source on GitHub</a> &middot;
      a project by <a href="https://jackmccarthy.org">Jack McCarthy</a></p>
  </footer>
</main>
<script>
(function(){
  var q=document.getElementById('q'), dir=document.getElementById('dir'),
      empty=document.getElementById('empty'), sortSel=document.getElementById('sort'),
      hideU=document.getElementById('hideUntagged'),
      rows=[].slice.call(dir.querySelectorAll('.row'));
  function num(c,a){ return parseFloat(c.getAttribute(a))||0; }
  function sortRows(){
    var mode=sortSel.value, arr=rows.slice();
    arr.sort(function(a,b){
      if(mode==='coverage') return num(b,'data-cov')-num(a,'data-cov');
      if(mode==='formalized') return num(b,'data-f')-num(a,'data-f');
      if(mode==='count') return num(b,'data-n')-num(a,'data-n');
      return a.getAttribute('data-title').localeCompare(b.getAttribute('data-title'));
    });
    arr.forEach(function(c){ dir.appendChild(c); });
  }
  function apply(){
    var t=q.value.trim().toLowerCase(), hu=hideU.checked, shown=0;
    rows.forEach(function(c){
      var hit=c.getAttribute('data-title').indexOf(t)!==-1
              && !(hu && c.getAttribute('data-untagged')==='1');
      c.style.display=hit?'':'none'; if(hit)shown++;
    });
    empty.style.display=shown?'none':'';
    // While a query is active, collapse the strips so matches sit right
    // under the search box.
    document.body.classList.toggle('searching', t.length>0);
  }
  q.addEventListener('input',apply);
  hideU.addEventListener('change',apply);
  sortSel.addEventListener('change',sortRows);
})();
</script>
<script>
/* Theme toggle — flips dark/light and persists in localStorage. */
(function(){var b=document.getElementById("wl-theme-toggle");if(!b)return;
b.addEventListener("click",function(){var r=document.documentElement;
var n=r.dataset.theme==="dark"?"light":"dark";r.dataset.theme=n;
try{localStorage.setItem("wl-theme",n);}catch(e){}});})();
</script>
</body>
</html>
`;
}

export function sitemapXml(rows: SitemapRow[]): string {
  // Homepage root first, at top priority — it's the page that owns the brand
  // query. lastmod tracks the most recently updated article (the homepage
  // re-renders whenever any article changes).
  const newest = rows.reduce((m, r) => (r.updatedAt > m ? r.updatedAt : m), 0);
  const rootMod = newest ? `<lastmod>${new Date(newest).toISOString().slice(0, 10)}</lastmod>` : "";
  const rootUrl = `  <url><loc>${SITE_ORIGIN}/</loc>${rootMod}<priority>1.0</priority></url>`;
  // Flagship static pages — were missing, so crawlers never discovered the map.
  const staticUrls = ["map", "concepts", "about"].map(
    (p) => `  <url><loc>${SITE_ORIGIN}/${p}</loc><priority>0.8</priority></url>`,
  );
  const urls = [
    rootUrl,
    ...staticUrls,
    ...[...rows]
      .sort((a, b) => (a.slug < b.slug ? -1 : a.slug > b.slug ? 1 : 0))
      .map(
        (r) =>
          // <loc> must be a valid URL: percent-encode the slug, then XML-escape
          // (W3 fix #10).
          `  <url><loc>${SITE_ORIGIN}/${esc(encodeURIComponent(r.slug))}</loc>` +
          `<lastmod>${new Date(r.updatedAt).toISOString().slice(0, 10)}</lastmod>` +
          `<priority>0.6</priority></url>`,
      ),
  ];
  return (
    `<?xml version="1.0" encoding="UTF-8"?>\n` +
    `<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n` +
    urls.join("\n") +
    (urls.length ? "\n" : "") +
    `</urlset>\n`
  );
}
