// Dynamic homepage + sitemap from D1 (Wave D, contract D-C7). Replaces the
// static site/out/index.html + sitemap.xml that build-public.ts used to copy
// into wiki/public/ — articles created via PUT /api/article/:slug now appear
// without a static rebuild. index.ts serves homePage() cached in RENDER_CACHE
// ('page:home:v1', TTL 300s) and sitemapXml() ('page:sitemap:v1', TTL 3600s).
//
// Visual language mirrors site/build_index.py's landing page (header, stat
// badges, legend, card grid with coverage bars, client-side filter/sort).
// Counts come from articles.n_formalized/n_partial/n_not_formalized (D-C5);
// null = not yet backfilled and renders as an em-dash.
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

function hasCounts(r: HomeRow): r is HomeRow & {
  nFormalized: number;
  nPartial: number;
  nNotFormalized: number;
} {
  return r.nFormalized !== null && r.nPartial !== null && r.nNotFormalized !== null;
}

function card(r: HomeRow): string {
  const t = esc(r.displayTitle);
  let body: string;
  let cov = 0;
  let f = 0;
  let n = 0;
  let untagged = true;
  if (!hasCounts(r)) {
    // Pre-backfill row: counts are unknown, not zero (D-C5: null = pending).
    body =
      '<span class="bar bar-empty"></span>' +
      '<span class="counts pending">&mdash; counts pending</span>';
  } else {
    f = r.nFormalized;
    n = r.nFormalized + r.nPartial + r.nNotFormalized;
    if (n === 0) {
      body =
        '<span class="bar bar-empty"></span>' +
        '<span class="counts untagged">not yet tagged</span>';
    } else {
      untagged = false;
      cov = f / n;
      const wf = (100 * r.nFormalized) / n;
      const wp = (100 * r.nPartial) / n;
      const wn = (100 * r.nNotFormalized) / n;
      body =
        `<span class="bar" title="${r.nFormalized} formalized · ${r.nPartial} partial · ${r.nNotFormalized} not formalized">` +
        `<i class="f" style="width:${wf.toFixed(1)}%"></i>` +
        `<i class="p" style="width:${wp.toFixed(1)}%"></i>` +
        `<i class="n" style="width:${wn.toFixed(1)}%"></i></span>` +
        `<span class="counts"><i class="dot df"></i>${r.nFormalized} ` +
        `<i class="dot dp"></i>${r.nPartial} ` +
        `<i class="dot dn"></i>${r.nNotFormalized} &middot; ${n} total</span>`;
    }
  }
  return (
    `<a class="card" href="/${esc(r.slug)}" ` +
    `data-title="${esc(r.displayTitle.toLowerCase())}" data-cov="${cov.toFixed(4)}" ` +
    `data-f="${f}" data-n="${n}" data-untagged="${untagged ? 1 : 0}">` +
    `<span class="card-title">${t}</span>${body}</a>`
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
  const pendingStat = nPending
    ? `\n    <span><b>${fmtInt(nPending)}</b> awaiting count backfill</span>`
    : "";

  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WikiLean — Wikipedia mathematics, mapped to Lean</title>
<meta name="description" content="A mirror of WikiProject Mathematics articles annotated with links into Mathlib, color-coded by formalization coverage.">
<style>
:root {
  --green:#2da44e; --yellow:#d29922; --red:#cf222e;
}
* { box-sizing:border-box; }
body { margin:0; background:#fafbfc; color:#1f2328;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
.wl-header { background:#fff; border-bottom:1px solid #d0d7de; padding:14px 28px;
  display:flex; align-items:center; justify-content:space-between; }
.wl-brand { font-weight:700; color:#0969da; font-size:18px; text-decoration:none; }
.wl-nav { display:flex; gap:18px; }
.wl-navlink { color:#0969da; text-decoration:none; font-size:.9rem; }
.wl-navlink:hover { text-decoration:underline; }
.wrap { max-width:920px; margin:0 auto; padding:32px 28px 64px; }
h1 { font-size:1.7rem; margin:0 0 .5rem; }
.lead { color:#57606a; font-size:1.02rem; line-height:1.6; max-width:680px; }
.lead a { color:#0969da; text-decoration:none; }
.lead a:hover { text-decoration:underline; }
.stats { display:flex; gap:24px; margin:24px 0 8px; flex-wrap:wrap; font-size:.9rem; color:#57606a; }
.stats b { color:#1f2328; }
.legend { display:flex; gap:16px; font-size:.82rem; color:#57606a; margin-bottom:20px; flex-wrap:wrap; }
.legend i { display:inline-block; width:11px; height:11px; border-radius:2px; margin-right:5px; vertical-align:middle; }
.concepts-link { background:#fff; border:1px solid #d0d7de; border-radius:8px; padding:12px 16px;
  font-size:.9rem; color:#57606a; line-height:1.5; margin:0 0 20px; }
.concepts-link a { color:#0969da; text-decoration:none; font-weight:600; }
.concepts-link a:hover { text-decoration:underline; }
.controls { display:flex; gap:12px; align-items:center; margin-bottom:20px; flex-wrap:wrap; }
.search { flex:1 1 240px; padding:10px 14px; font-size:1rem; border:1px solid #d0d7de;
  border-radius:8px; font-family:inherit; }
.sort { padding:9px 12px; font-size:.9rem; border:1px solid #d0d7de; border-radius:8px;
  background:#fff; color:#1f2328; font-family:inherit; }
.chk { display:flex; gap:6px; align-items:center; font-size:.9rem; color:#57606a;
  cursor:pointer; white-space:nowrap; }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:12px; }
.card { display:block; text-decoration:none; color:inherit; border:1px solid #d0d7de;
  border-radius:8px; padding:13px 15px; background:#fff; transition:border-color .12s, box-shadow .12s; }
.card:hover { border-color:#0969da; box-shadow:0 1px 6px rgba(9,105,218,.12); }
.card-title { display:block; font-weight:600; font-size:.98rem; margin-bottom:9px; }
.bar { display:flex; height:7px; border-radius:4px; overflow:hidden; background:#eaeef2; }
.bar i { display:block; height:100%; }
.bar i.f { background:var(--green); }
.bar i.p { background:var(--yellow); }
.bar i.n { background:var(--red); }
.bar-empty { background:#eaeef2; }
.counts { display:block; font-size:.78rem; color:#57606a; margin-top:7px; }
.counts .dot { display:inline-block; width:8px; height:8px; border-radius:2px; margin:0 3px 0 6px; vertical-align:baseline; }
.counts .dot:first-child { margin-left:0; }
.counts .dot.df { background:var(--green); }
.counts .dot.dp { background:var(--yellow); }
.counts .dot.dn { background:var(--red); }
.counts.untagged { color:#8c959f; font-style:italic; }
.counts.pending { color:#8c959f; }
.empty { color:#57606a; padding:30px 0; }
footer { margin-top:48px; padding-top:20px; border-top:1px solid #d0d7de; font-size:.82rem; color:#57606a; }
footer a { color:#0969da; text-decoration:none; }
</style>
</head>
<body>
<header class="wl-header">
  <a class="wl-brand" href="/">WikiLean</a>
  <nav class="wl-nav">
    <a class="wl-navlink" href="/concepts">Concepts</a>
    <a class="wl-navlink" href="/article-graph">Article graph</a>
    <a class="wl-navlink" href="/graph">Concept graph</a>
    <a class="wl-navlink" href="/about">About &amp; method</a>
  </nav>
</header>
<div class="wrap">
  <h1>Wikipedia mathematics, mapped to Lean</h1>
  <p class="lead">
    A mirror of <a href="https://en.wikipedia.org/wiki/Wikipedia:WikiProject_Mathematics">WikiProject
    Mathematics</a> articles, annotated inline with links into
    <a href="https://leanprover-community.github.io/mathlib4_docs/">Mathlib4</a> and color-coded by
    whether each definition, theorem, and proof has been formalized in Lean. Built by
    <a href="https://jackmccarthy.org">Jack McCarthy</a>.
  </p>
  <div class="stats">
    <span><b>${fmtInt(sorted.length)}</b> articles</span>
    <span><b>${fmtInt(grand)}</b> annotated results</span>
    <span><b>${pctF}%</b> formalized &middot; <b>${pctP}%</b> partial</span>
    <span><b>${fmtInt(nUntagged)}</b> not yet tagged</span>${pendingStat}
  </div>
  <div class="legend">
    <span><i style="background:var(--green)"></i>formalized</span>
    <span><i style="background:var(--yellow)"></i>partial</span>
    <span><i style="background:var(--red)"></i>not formalized</span>
  </div>
  <p class="concepts-link">
    <a href="/concepts">&rarr; Wikidata concept links</a> &mdash; every formalized
    concept keyed to its Wikidata item, as an open RDF dataset (the basis for a proposed
    <em>&ldquo;formalized as (Lean/Mathlib)&rdquo;</em> Wikidata property).
  </p>
  <p class="concepts-link">
    <a href="/article-graph">&rarr; Article graph</a> &mdash; WikiLean articles
    clustered by shared Mathlib formalizations: edges connect articles that annotate
    the same declarations, colored by their dominant Mathlib area.
  </p>
  <p class="concepts-link">
    <a href="/graph">&rarr; Concept graph</a> &mdash; Mathlib's declaration-level
    dependency edges overlaid on Wikidata's typed item-to-item statements, on a shared
    Wikidata node set. Drag, zoom, click; consensus edges (in both) highlighted.
  </p>
  <div class="controls">
    <input class="search" id="q" type="search" placeholder="Filter articles&hellip;" autocomplete="off">
    <select class="sort" id="sort" aria-label="Sort articles">
      <option value="title">Sort: A&ndash;Z</option>
      <option value="coverage">Sort: coverage</option>
      <option value="formalized">Sort: most formalized</option>
      <option value="count">Sort: most annotated</option>
    </select>
    <label class="chk"><input type="checkbox" id="hideUntagged"> Hide untagged</label>
  </div>
  <div class="grid" id="grid">
${sorted.map(card).join("\n")}
  </div>
  <p class="empty" id="empty" style="display:none">No articles match.</p>
  <footer>
    WikiLean &middot; <a href="https://github.com/Deicyde/WikiLean">source on GitHub</a> &middot;
    a project by <a href="https://jackmccarthy.org">Jack McCarthy</a>
  </footer>
</div>
<script>
(function(){
  var q=document.getElementById('q'), grid=document.getElementById('grid'),
      empty=document.getElementById('empty'), sortSel=document.getElementById('sort'),
      hideU=document.getElementById('hideUntagged'),
      cards=[].slice.call(grid.querySelectorAll('.card'));
  function num(c,a){ return parseFloat(c.getAttribute(a))||0; }
  function sortCards(){
    var mode=sortSel.value, arr=cards.slice();
    arr.sort(function(a,b){
      if(mode==='coverage') return num(b,'data-cov')-num(a,'data-cov');
      if(mode==='formalized') return num(b,'data-f')-num(a,'data-f');
      if(mode==='count') return num(b,'data-n')-num(a,'data-n');
      return a.getAttribute('data-title').localeCompare(b.getAttribute('data-title'));
    });
    arr.forEach(function(c){ grid.appendChild(c); });
  }
  function apply(){
    var t=q.value.trim().toLowerCase(), hu=hideU.checked, shown=0;
    cards.forEach(function(c){
      var hit=c.getAttribute('data-title').indexOf(t)!==-1
              && !(hu && c.getAttribute('data-untagged')==='1');
      c.style.display=hit?'':'none'; if(hit)shown++;
    });
    empty.style.display=shown?'none':'';
  }
  q.addEventListener('input',apply);
  hideU.addEventListener('change',apply);
  sortSel.addEventListener('change',sortCards);
})();
</script>
</body>
</html>
`;
}

export function sitemapXml(rows: SitemapRow[]): string {
  const urls = [...rows]
    .sort((a, b) => (a.slug < b.slug ? -1 : a.slug > b.slug ? 1 : 0))
    .map(
      (r) =>
        `  <url><loc>${SITE_ORIGIN}/${esc(r.slug)}</loc>` +
        `<lastmod>${new Date(r.updatedAt).toISOString().slice(0, 10)}</lastmod>` +
        `<priority>0.6</priority></url>`,
    );
  return (
    `<?xml version="1.0" encoding="UTF-8"?>\n` +
    `<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n` +
    urls.join("\n") +
    (urls.length ? "\n" : "") +
    `</urlset>\n`
  );
}
