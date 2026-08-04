#!/usr/bin/env python3
"""Generate WikiLean's static chrome: a 404 page, robots.txt, and a sitemap
covering every rendered page.

(The About page is no longer built here — it is served dynamically by the
Worker at GET /about with live D1 counts; see wiki/src/home.ts aboutPage().)

Run LAST, after render.py / build_index.py / export_wikidata_rdf.py, so the
sitemap picks up index.html, concepts.html, and every article in out/.

Outputs:
    out/404.html
    out/robots.txt
    out/sitemap.xml
"""
from __future__ import annotations

import datetime
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "out"
BASE_URL = "https://wikilean.jackmccarthy.org"

# Pages that are not articles get friendlier sitemap priorities / no per-article
# treatment. Everything else in out/*.html is an annotated article.
NON_ARTICLE = {"index", "concepts", "graph", "article-graph", "about", "404"}
# Hyphenated stems like "article-graph" survive Path.stem unchanged.


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
    write_404()
    write_robots()
    write_sitemap()
    print("Wrote out/404.html, out/robots.txt")


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
    <a class="wl-navlink" href="/brain">Brain</a>
    <a class="wl-navlink" href="/about">About &amp; method</a>
    %TOGGLE_BTN%
  </nav>
</header>
<div class="wrap">
  <div class="nf">
    <p class="code">404</p>
    <p>That page isn't here. It may not be one of the annotated articles yet.</p>
    <p><a href="/">Browse all articles</a> &middot; <a href="/concepts">Concepts</a> &middot; <a href="/brain">Brain</a></p>
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
