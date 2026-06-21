#!/usr/bin/env python3
"""Generate WikiLean's static chrome: the About/methodology page, a 404 page,
robots.txt, and a sitemap covering every rendered page.

Run LAST, after render.py / build_index.py / export_wikidata_rdf.py, so the
sitemap picks up index.html, concepts.html, and every article in out/.

Outputs:
    out/about.html
    out/404.html
    out/robots.txt
    out/sitemap.xml
"""
from __future__ import annotations

import datetime
import json
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
ANNO_DIR = HERE / "annotations"
OUT_DIR = HERE / "out"
BASE_URL = "https://wikilean.jackmccarthy.org"

# Pages that are not articles get friendlier sitemap priorities / no per-article
# treatment. Everything else in out/*.html is an annotated article.
NON_ARTICLE = {"index", "concepts", "graph", "article-graph", "about", "404"}
# Hyphenated stems like "article-graph" survive Path.stem unchanged.


def aggregate_stats() -> dict:
    """Articles + status totals across annotations that have a rendered page."""
    totals = {"formalized": 0, "partial": 0, "not_formalized": 0}
    n_articles = 0
    for jf in ANNO_DIR.glob("*.json"):
        try:
            d = json.loads(jf.read_text())
        except Exception:
            continue
        slug = d.get("slug")
        if not slug or not (OUT_DIR / f"{slug}.html").exists():
            continue
        c = {"formalized": 0, "partial": 0, "not_formalized": 0}
        for a in d.get("annotations", []) or []:
            s = a.get("status")
            if s in c:
                c[s] += 1
        if sum(c.values()) == 0:
            continue
        n_articles += 1
        for k in totals:
            totals[k] += c[k]
    grand = sum(totals.values()) or 1
    return {
        "n_articles": n_articles,
        "n_results": grand,
        "pct_f": round(100 * totals["formalized"] / grand),
        "pct_p": round(100 * totals["partial"] / grand),
    }


def write_about(stats: dict) -> None:
    # The template embeds raw CSS (with { }), so substitute with .replace
    # rather than str.format.
    html_out = ABOUT_TEMPLATE
    for key, val in stats.items():
        html_out = html_out.replace("%" + key.upper() + "%", str(val))
    (OUT_DIR / "about.html").write_text(html_out)


def write_404() -> None:
    (OUT_DIR / "404.html").write_text(NOT_FOUND_TEMPLATE)


def write_robots() -> None:
    (OUT_DIR / "robots.txt").write_text(
        "User-agent: *\nAllow: /\n"
        f"Sitemap: {BASE_URL}/sitemap.xml\n"
    )


def write_sitemap() -> None:
    today = datetime.date.today().isoformat()
    urls = []
    for f in sorted(OUT_DIR.glob("*.html")):
        stem = f.stem
        if stem == "404":
            continue
        if stem == "index":
            loc, prio = f"{BASE_URL}/", "1.0"
        elif stem in NON_ARTICLE:
            loc, prio = f"{BASE_URL}/{stem}", "0.8"
        else:
            loc, prio = f"{BASE_URL}/{urllib.parse.quote(stem)}", "0.6"
        urls.append(
            f"  <url><loc>{loc}</loc><lastmod>{today}</lastmod>"
            f"<priority>{prio}</priority></url>"
        )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls)
        + "\n</urlset>\n"
    )
    (OUT_DIR / "sitemap.xml").write_text(body)
    print(f"Wrote out/sitemap.xml — {len(urls)} URLs")


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    stats = aggregate_stats()
    write_about(stats)
    write_404()
    write_robots()
    write_sitemap()
    print(f"Wrote out/about.html, out/404.html, out/robots.txt "
          f"({stats['n_articles']} articles, {stats['n_results']} results)")


# Warm academic-minimalist palette, matching style.css / home.ts (W3 fix #6e):
# paper #f7f4ee, surface #fffdf9, ink #1f1d1a, muted #5f594e, accent #1a4b8c,
# hairlines #e6e0d2/#d8d0bd, status trio #2f7d4f/#b08020/#b3372f.
SHARED_CSS = """
* { box-sizing:border-box; }
body { margin:0; background:#f7f4ee; color:#1f1d1a;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
:focus-visible { outline:2px solid #1a4b8c; outline-offset:2px; }
.wl-header { background:#fffdf9; border-bottom:1px solid #d8d0bd; padding:14px 28px;
  display:flex; align-items:center; justify-content:space-between; }
.wl-brand { font-family:Charter,'Bitstream Charter','Iowan Old Style',Georgia,'Times New Roman',serif;
  font-weight:700; color:#1f1d1a; font-size:18px; text-decoration:none; }
.wl-brand:hover { color:#1a4b8c; }
.wl-nav { display:flex; gap:18px; }
.wl-navlink { color:#1a4b8c; text-decoration:none; font-size:.9rem; }
.wl-navlink:hover { text-decoration:underline; }
.wrap { max-width:760px; margin:0 auto; padding:32px 28px 64px; }
h1, h2 { font-family:Charter,'Bitstream Charter','Iowan Old Style',Georgia,'Times New Roman',serif; }
h1 { font-size:1.7rem; margin:0 0 .5rem; }
h2 { font-size:1.15rem; margin:2rem 0 .6rem; }
p, li { color:#1f1d1a; font-size:1.0rem; line-height:1.65; }
a { color:#1a4b8c; text-decoration:none; }
a:hover { text-decoration:underline; }
.lead { color:#5f594e; font-size:1.05rem; }
.swatch { display:inline-block; width:11px; height:11px; border-radius:2px;
  margin-right:6px; vertical-align:middle; }
.s-f { background:#2f7d4f; } .s-p { background:#b08020; } .s-n { background:#b3372f; }
.stats { display:flex; gap:24px; margin:18px 0 8px; flex-wrap:wrap; font-size:.9rem; color:#5f594e; }
.stats b { color:#1f1d1a; }
footer { margin-top:48px; padding-top:20px; border-top:1px solid #d8d0bd;
  font-size:.82rem; color:#5f594e; }
.wl-theme-toggle { background:transparent; border:1px solid #d8d0bd; color:#5f594e;
  border-radius:50%; width:28px; height:28px; padding:0; line-height:1; font-size:14px;
  cursor:pointer; display:inline-flex; align-items:center; justify-content:center; margin-left:10px; }
[data-theme="dark"] .wl-theme-toggle { color:#9a9081; border-color:#4d4742; }

/* Dark mode — shared palette across the site (bg #1a1816, surface #232020,
   text #ebe5d8, muted #9a9081, accent #6e9adf, borders #4d4742). */
[data-theme="dark"] body { background:#1a1816; color:#ebe5d8; }
[data-theme="dark"] :focus-visible { outline-color:#6e9adf; }
[data-theme="dark"] .wl-header { background:#232020; border-bottom-color:#4d4742; }
[data-theme="dark"] .wl-brand { color:#ebe5d8; }
[data-theme="dark"] .wl-brand:hover { color:#8fb4e8; }
[data-theme="dark"] .wl-navlink { color:#6e9adf; }
[data-theme="dark"] .wl-navlink.active { color:#ebe5d8; }
[data-theme="dark"] h1, [data-theme="dark"] h2 { color:#ebe5d8; }
[data-theme="dark"] p, [data-theme="dark"] li { color:#ebe5d8; }
[data-theme="dark"] a { color:#6e9adf; }
[data-theme="dark"] a:hover { color:#8fb4e8; }
[data-theme="dark"] .lead { color:#9a9081; }
[data-theme="dark"] .stats { color:#9a9081; }
[data-theme="dark"] .stats b { color:#ebe5d8; }
[data-theme="dark"] code { background:#2c2926; color:#ebe5d8; }
[data-theme="dark"] footer { border-top-color:#4d4742; color:#9a9081; }
"""

# Run before any stylesheet so the theme is set before first paint (no flash).
NO_FOUC = (
    '<script>(function(){try{var s=localStorage.getItem("wl-theme");'
    'var t=s==="dark"||s==="light"?s:(window.matchMedia&&'
    'window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light");'
    'document.documentElement.dataset.theme=t;}catch(e){}})();</script>'
)

THEME_TOGGLE_BTN = (
    '<button id="wl-theme-toggle" class="wl-theme-toggle" type="button" '
    'aria-label="Toggle dark mode" title="Toggle dark mode">\U0001f313</button>'
)

THEME_TOGGLE_SCRIPT = (
    '<script>(function(){var b=document.getElementById("wl-theme-toggle");'
    'if(!b)return;b.addEventListener("click",function(){var r=document.documentElement;'
    'var n=r.dataset.theme==="dark"?"light":"dark";r.dataset.theme=n;'
    'try{localStorage.setItem("wl-theme",n);}catch(e){}});})();</script>'
)

ABOUT_TEMPLATE = (
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WikiLean — About &amp; method</title>
<meta name="description" content="How WikiLean maps Wikipedia mathematics to Lean/Mathlib4 formalizations: methodology, what the formalized / partial / not-formalized statuses mean, and limitations.">
<link rel="canonical" href="%BASE%/about">
%NOFOUC%
<style>%CSS%</style>
</head>
<body>
<header class="wl-header">
  <a class="wl-brand" href="/">WikiLean</a>
  <nav class="wl-nav">
    <a class="wl-navlink" href="/concepts">Concepts</a>
    <a class="wl-navlink" href="/article-graph">Article graph</a>
    <a class="wl-navlink" href="/graph">Concept graph</a>
    <a class="wl-navlink active" href="/about">About &amp; method</a>
    %TOGGLE_BTN%
  </nav>
</header>
<div class="wrap">
  <h1>About &amp; method</h1>
  <p class="lead">WikiLean is a mirror of <a href="https://en.wikipedia.org/wiki/Wikipedia:WikiProject_Mathematics">WikiProject
  Mathematics</a> articles, annotated inline with links into
  <a href="https://leanprover-community.github.io/mathlib4_docs/">Mathlib4</a> and
  color-coded by whether each definition, theorem, and proof has been formalized in
  the <a href="https://leanprover-community.github.io/">Lean</a> proof assistant.</p>
  <div class="stats">
    <span><b>%N_ARTICLES%</b> articles annotated</span>
    <span><b>%N_RESULTS%</b> tagged statements</span>
    <span><b>%PCT_F%%</b> formalized · <b>%PCT_P%%</b> partial</span>
  </div>

  <h2>How it is built</h2>
  <p>Three stages. <b>Catalog:</b> enumerate the WikiProject Mathematics article set
  with its metadata and Wikidata identifiers. <b>Annotate:</b> for each article, work
  through its definitions, theorems, and proofs and match each one to the Mathlib4
  declaration that formalizes it, recording a status and the declaration's module path.
  <b>Host:</b> fetch the article's rendered HTML from the MediaWiki API, wrap each
  matched statement in place, and serve the result as a standalone page. Hovering (or
  tapping) a highlighted statement shows its status and a direct link into the Mathlib
  documentation.</p>

  <h2>What the colors mean</h2>
  <ul>
    <li><span class="swatch s-f"></span><b>Formalized.</b> A Mathlib4 declaration
      captures the statement or definition as stated.</li>
    <li><span class="swatch s-p"></span><b>Partial.</b> Mathlib has a related, weaker,
      or special-case form — or only part of the statement is formalized. The tooltip
      notes where the formalization and the article diverge.</li>
    <li><span class="swatch s-n"></span><b>Not formalized.</b> No corresponding Mathlib4
      declaration was found at the time of annotation.</li>
  </ul>

  <h2>Provenance and pinning</h2>
  <p>Each article is annotated against a specific Wikipedia revision, and that revision
  id is recorded alongside the annotations, so a highlight always refers to the exact
  prose it was made against even after the live article changes. Mathlib links point at
  the public Mathlib4 documentation.</p>

  <h2>Wikidata concept links</h2>
  <p>Every concept matched to a <em>formalized</em> declaration is also keyed to its
  <a href="https://www.wikidata.org/">Wikidata</a> item and published as an open
  <a href="/concepts">RDF dataset</a>. This is the basis for a proposed Wikidata
  property, <em>&ldquo;formalized as (Lean/Mathlib)&rdquo;</em> — the long-term goal is
  for these links to live in Wikidata itself, maintainable by the community and queryable
  via SPARQL.</p>

  <h2>Article graph</h2>
  <p>The <a href="/article-graph">article graph</a> takes a different cut: nodes are
  Wikipedia articles, and there are two independent edge layers you can toggle.
  <b>Shared-decl edges</b> connect two articles that annotate the same Mathlib
  declarations — the result is a topical clustering, since articles that draw on the
  same parts of Mathlib pull together. A slider lets you raise the threshold for how
  many declarations two articles must share to be connected.
  <b>Wikipedia link edges</b> connect articles that link to each other through ordinary
  prose blue-links inside the cached enwiki HTML — the encyclopedia's own notion of
  related topics, independent of formalization. Nodes are colored by their dominant
  Mathlib namespace; only the shared-decl edges drive the force layout, so the
  Wikipedia-link layer is a true overlay.</p>

  <h2>Concept graph</h2>
  <p>The <a href="/graph">concept graph</a> overlays two independent reference graphs on
  the same Wikidata node set: Mathlib's declaration-level dependency edges (extracted via
  <code>Expr.getUsedConstants</code> over the built Mathlib environment — the same data
  that produces hyperlinks in the Mathlib docs) and Wikidata's typed item-to-item
  statements (P279 subclass-of, P361 part-of, etc., pulled from the Wikidata Query
  Service). Edges are colored by source so the consensus subgraph (where both systems
  independently assert a relation) is visible at a glance.</p>

  <h2>Limitations</h2>
  <p>Annotations are best-effort and cover a growing sample of WikiProject Mathematics,
  not the whole corpus. A match reflects a judgment that a Mathlib declaration formalizes
  a statement; it can be incomplete or wrong, and Mathlib itself evolves, so coverage is
  a snapshot rather than a guarantee. Corrections are welcome via the source repository.</p>

  <footer>
    WikiLean &middot; <a href="https://github.com/Deicyde/WikiLean">source on GitHub</a> &middot;
    a project by <a href="https://jackmccarthy.org">Jack McCarthy</a>
  </footer>
</div>
%TOGGLE_SCRIPT%
</body>
</html>
"""
    .replace("%CSS%", SHARED_CSS)
    .replace("%NOFOUC%", NO_FOUC)
    .replace("%TOGGLE_BTN%", THEME_TOGGLE_BTN)
    .replace("%TOGGLE_SCRIPT%", THEME_TOGGLE_SCRIPT)
    .replace("%BASE%", BASE_URL)
)

NOT_FOUND_TEMPLATE = (
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WikiLean — Page not found</title>
<meta name="robots" content="noindex">
%NOFOUC%
<style>%CSS%
.nf { text-align:center; padding:72px 0; }
.nf .code { font-size:3rem; font-weight:700; color:#1a4b8c; margin:0; }
.nf p { color:#5f594e; }
[data-theme="dark"] .nf .code { color:#6e9adf; }
[data-theme="dark"] .nf p { color:#9a9081; }
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
    %TOGGLE_BTN%
  </nav>
</header>
<div class="wrap">
  <div class="nf">
    <p class="code">404</p>
    <p>That page isn't here. It may not be one of the annotated articles yet.</p>
    <p><a href="/">Browse all articles</a> &middot; <a href="/concepts">Concepts</a> &middot; <a href="/article-graph">Article graph</a> &middot; <a href="/graph">Concept graph</a></p>
  </div>
</div>
%TOGGLE_SCRIPT%
</body>
</html>
"""
    .replace("%CSS%", SHARED_CSS)
    .replace("%NOFOUC%", NO_FOUC)
    .replace("%TOGGLE_BTN%", THEME_TOGGLE_BTN)
    .replace("%TOGGLE_SCRIPT%", THEME_TOGGLE_SCRIPT)
)


if __name__ == "__main__":
    main()
