#!/usr/bin/env python3
"""Export WikiLean annotations as Wikidata-aligned RDF + a human concept index.

For each annotated article that has a Wikidata QID, emit triples linking the
Wikidata item to its Mathlib declaration(s) via a custom predicate, plus a
styled, deployable concept index. This is the data backing the Wikidata property
proposal in docs/wikidata_property_proposal.md — the long-term goal is a real
Wikidata property "formalized as (Lean/Mathlib)" so these links live in
Wikidata itself and are queryable via SPARQL.

Outputs:
    out/concepts.html         # styled, deployed concept index (QID + decl links)
    out/wikilean.ttl          # downloadable Turtle dump
    out_w3c/wikilean.ttl      # canonical RDF copy

Usage:
    python export_wikidata_rdf.py     # run after render.py / build_index.py
"""
from __future__ import annotations

import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ANNOT = ROOT / "annotations"
OUT = ROOT / "out"
OUT_W3C = ROOT / "out_w3c"
CATALOG = ROOT.parent / "catalog" / "data" / "articles.jsonl"

WIKILEAN_NS = "https://wikilean.jackmccarthy.org/ns#"
WD = "http://www.wikidata.org/entity/"
MATHLIB_DOCS = "https://leanprover-community.github.io/mathlib4_docs"


def load_qid_map() -> dict:
    """Map article title -> Wikidata QID from the catalog JSONL."""
    qmap = {}
    if not CATALOG.exists():
        return qmap
    with CATALOG.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            title = rec.get("title")
            qid = rec.get("wikidata_qid")
            if title and qid:
                qmap[title] = qid
    return qmap


def slug_to_title(slug: str) -> str:
    return slug.replace("_", " ")


def decl_url(module: str | None, decl: str) -> str | None:
    if not module:
        return None
    return f"{MATHLIB_DOCS}/{module.replace('.', '/')}.html#{decl}"


def main() -> None:
    qmap = load_qid_map()
    anns = sorted(ANNOT.glob("*.json"))
    OUT.mkdir(exist_ok=True)
    OUT_W3C.mkdir(exist_ok=True)

    concepts = []  # (title, slug, qid, [(decl, module)], has_article)
    ttl_lines = [
        f"@prefix wd: <{WD}> .",
        f"@prefix wl: <{WIKILEAN_NS}> .",
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
        "",
    ]

    n_links = 0
    for jf in anns:
        slug = jf.stem
        try:
            d = json.loads(jf.read_text())
        except Exception:
            continue
        title = d.get("wikipedia_title") or slug_to_title(slug)
        qid = qmap.get(title)
        if not qid:
            continue
        decls = []
        seen = set()
        for a in d.get("annotations", []):
            ml = a.get("mathlib") or {}
            decl = ml.get("decl")
            module = ml.get("module")
            if decl and a.get("status") == "formalized" and decl not in seen:
                seen.add(decl)
                decls.append((decl, module))
        if not decls:
            continue
        n_links += 1
        has_article = (OUT / f"{slug}.html").exists()
        concepts.append((title, slug, qid, decls, has_article))
        subj = f"wd:{qid}"
        for decl, module in decls:
            ttl_lines.append(f'{subj} wl:formalizedAs "{html.escape(decl)}" .')
            url = decl_url(module, decl)
            if url:
                ttl_lines.append(f'{subj} rdfs:seeAlso <{url}> .')

    ttl = "\n".join(ttl_lines) + "\n"
    (OUT / "wikilean.ttl").write_text(ttl)
    (OUT_W3C / "wikilean.ttl").write_text(ttl)

    n_decls = sum(len(c[3]) for c in concepts)

    rows = []
    for title, slug, qid, decls, has_article in sorted(concepts, key=lambda c: c[0].lower()):
        t = html.escape(title)
        concept_cell = f'<a href="{slug}.html">{t}</a>' if has_article else t
        decl_cell = ", ".join(
            (f'<a href="{decl_url(module, decl)}"><code>{html.escape(decl)}</code></a>'
             if decl_url(module, decl) else f'<code>{html.escape(decl)}</code>')
            for decl, module in decls
        )
        rows.append(
            f'<tr data-q="{t.lower()} {qid.lower()}">'
            f'<td><a href="https://www.wikidata.org/wiki/{qid}">{qid}</a></td>'
            f'<td>{concept_cell}</td><td>{decl_cell}</td></tr>'
        )

    page = TEMPLATE.format(
        n_links=n_links,
        n_decls=n_decls,
        rows="\n".join(rows),
    )
    (OUT / "concepts.html").write_text(page)
    print(f"Wrote out/concepts.html + out/wikilean.ttl — "
          f"{n_links} concepts, {n_decls} formalized declarations")


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WikiLean — Wikidata concept links</title>
<meta name="description" content="Wikipedia mathematics concepts linked to their Lean/Mathlib formalizations via Wikidata QIDs — the dataset behind a proposed Wikidata property.">
<style>
* {{ box-sizing:border-box; }}
body {{ margin:0; background:#fafbfc; color:#1f2328;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
.wl-header {{ background:#fff; border-bottom:1px solid #d0d7de; padding:14px 28px;
  display:flex; align-items:center; justify-content:space-between; }}
.wl-brand {{ font-weight:700; color:#0969da; font-size:18px; text-decoration:none; }}
.wl-nav {{ display:flex; gap:18px; }}
.wl-navlink {{ color:#0969da; text-decoration:none; font-size:.9rem; }}
.wl-navlink:hover {{ text-decoration:underline; }}
.wrap {{ max-width:920px; margin:0 auto; padding:32px 28px 64px; }}
h1 {{ font-size:1.7rem; margin:0 0 .5rem; }}
.lead {{ color:#57606a; font-size:1.02rem; line-height:1.6; max-width:720px; }}
.lead a {{ color:#0969da; text-decoration:none; }}
.lead a:hover {{ text-decoration:underline; }}
.stats {{ display:flex; gap:24px; margin:22px 0 6px; flex-wrap:wrap; font-size:.9rem; color:#57606a; }}
.stats b {{ color:#1f2328; }}
.dataset {{ background:#fff; border:1px solid #d0d7de; border-radius:8px; padding:14px 16px;
  margin:18px 0 8px; font-size:.9rem; color:#57606a; }}
.dataset a {{ color:#0969da; text-decoration:none; }}
.dataset code {{ background:#f0f0f0; padding:1px 5px; border-radius:3px; }}
.search {{ width:100%; padding:10px 14px; font-size:1rem; border:1px solid #d0d7de; border-radius:8px;
  margin:18px 0; font-family:inherit; }}
table {{ border-collapse:collapse; width:100%; background:#fff; border:1px solid #d0d7de; border-radius:8px; overflow:hidden; }}
th,td {{ border-bottom:1px solid #eaeef2; padding:8px 12px; text-align:left; font-size:.92rem; vertical-align:top; }}
th {{ background:#f6f8fa; font-size:.8rem; text-transform:uppercase; letter-spacing:.04em; color:#57606a; }}
tr:last-child td {{ border-bottom:none; }}
td a {{ color:#0969da; text-decoration:none; }}
td a:hover {{ text-decoration:underline; }}
code {{ background:#f0f0f0; padding:1px 5px; border-radius:3px; font-size:.85em; }}
.empty {{ color:#57606a; padding:24px 0; }}
footer {{ margin-top:40px; padding-top:18px; border-top:1px solid #d0d7de; font-size:.82rem; color:#57606a; }}
footer a {{ color:#0969da; text-decoration:none; }}
</style>
</head>
<body>
<header class="wl-header">
  <a class="wl-brand" href="/">WikiLean</a>
  <nav class="wl-nav">
    <a class="wl-navlink active" href="/concepts">Concepts</a>
    <a class="wl-navlink" href="/article-graph">Article graph</a>
    <a class="wl-navlink" href="/graph">Concept graph</a>
    <a class="wl-navlink" href="/about">About &amp; method</a>
  </nav>
</header>
<div class="wrap">
  <h1>Wikidata concept links</h1>
  <p class="lead">
    Every Wikipedia mathematics concept WikiLean has matched to a
    <em>formalized</em> declaration in
    <a href="https://leanprover-community.github.io/mathlib4_docs/">Mathlib4</a>,
    keyed by its <a href="https://www.wikidata.org/">Wikidata</a> item. This is the
    dataset behind a proposed Wikidata property, <em>&ldquo;formalized as
    (Lean/Mathlib)&rdquo;</em> — the goal is for these links to live in Wikidata
    itself, queryable via SPARQL and maintainable by the community.
    &larr; <a href="/">back to all articles</a>
  </p>
  <div class="stats">
    <span><b>{n_links}</b> concepts</span>
    <span><b>{n_decls}</b> formalized declarations</span>
  </div>
  <div class="dataset">
    Download the dataset as RDF Turtle: <a href="/wikilean.ttl"><code>wikilean.ttl</code></a>
    (predicate <code>wl:formalizedAs</code> + <code>rdfs:seeAlso</code> into the Mathlib docs).
  </div>
  <input class="search" id="q" type="search" placeholder="Filter by concept or QID…" autocomplete="off">
  <table>
    <thead><tr><th>Wikidata</th><th>Concept</th><th>Mathlib declaration(s)</th></tr></thead>
    <tbody id="rows">
{rows}
    </tbody>
  </table>
  <p class="empty" id="empty" style="display:none">No concepts match.</p>
  <footer>
    WikiLean &middot; <a href="https://github.com/Deicyde/WikiLean">source</a> &middot;
    <a href="https://jackmccarthy.org">Jack McCarthy</a>
  </footer>
</div>
<script>
(function(){{
  var q=document.getElementById('q'),
      rows=[].slice.call(document.querySelectorAll('#rows tr')),
      empty=document.getElementById('empty');
  q.addEventListener('input',function(){{
    var t=q.value.trim().toLowerCase(), shown=0;
    rows.forEach(function(r){{
      var hit=r.getAttribute('data-q').indexOf(t)!==-1;
      r.style.display=hit?'':'none'; if(hit)shown++;
    }});
    empty.style.display=shown?'none':'';
  }});
}})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
