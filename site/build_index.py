#!/usr/bin/env python3
"""Generate out/index.html: the WikiLean landing page.

Lists every rendered article with a formalization-coverage bar, plus a live
search filter. Reads annotations/*.json and the rendered out/*.html, so run it
after render.py.
"""
from __future__ import annotations

import html
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ANNO_DIR = HERE / "annotations"
OUT_DIR = HERE / "out"


def main() -> None:
    # A slug can have several annotation files (draft/agent variants). Keep the
    # richest one per slug — the file with the most status-tagged statements —
    # so each rendered article appears exactly once.
    best: dict[str, dict] = {}
    for jf in sorted(ANNO_DIR.glob("*.json")):
        try:
            d = json.loads(jf.read_text())
        except Exception:
            continue
        slug = d.get("slug")
        # Articles render dynamically via the Worker, so we no longer require a
        # local out/<slug>.html to exist — the index links resolve at request time.
        if not slug:
            continue
        c = {"formalized": 0, "partial": 0, "not_formalized": 0}
        for a in d.get("annotations", []) or []:
            s = a.get("status")
            if s in c:
                c[s] += 1
        n = c["formalized"] + c["partial"] + c["not_formalized"]
        if slug not in best or n > best[slug]["n"]:
            best[slug] = {"slug": slug,
                          "title": d.get("display_title") or d.get("wikipedia_title") or slug,
                          "n": n, "f": c["formalized"], "p": c["partial"], "nf": c["not_formalized"],
                          "untagged": n == 0}

    rows = sorted(best.values(), key=lambda r: r["title"].lower())
    totals = {"formalized": sum(r["f"] for r in rows),
              "partial": sum(r["p"] for r in rows),
              "not_formalized": sum(r["nf"] for r in rows)}
    grand = sum(totals.values()) or 1
    pct_f = round(100 * totals["formalized"] / grand)
    pct_p = round(100 * totals["partial"] / grand)
    n_untagged = sum(1 for r in rows if r["untagged"])

    cards = []
    for r in rows:
        n = r["n"]
        t = html.escape(r["title"])
        cov = (r["f"] / n) if n else 0.0
        if r["untagged"]:
            body = ('<span class="bar bar-empty"></span>'
                    '<span class="counts untagged">not yet tagged</span>')
        else:
            wf = 100 * r["f"] / n
            wp = 100 * r["p"] / n
            wn = 100 * r["nf"] / n
            body = (
                f'<span class="bar" title="{r["f"]} formalized · {r["p"]} partial · {r["nf"]} not formalized">'
                f'<i class="f" style="width:{wf:.1f}%"></i>'
                f'<i class="p" style="width:{wp:.1f}%"></i>'
                f'<i class="n" style="width:{wn:.1f}%"></i></span>'
                f'<span class="counts">{r["f"]}/{n} formalized</span>'
            )
        cards.append(
            f'<a class="card" href="{r["slug"]}.html" '
            f'data-title="{t.lower()}" data-cov="{cov:.4f}" '
            f'data-f="{r["f"]}" data-n="{n}" data-untagged="{1 if r["untagged"] else 0}">'
            f'<span class="card-title">{t}</span>'
            f'{body}</a>'
        )

    page = TEMPLATE.format(
        n_articles=len(rows),
        n_results=grand,
        pct_f=pct_f,
        pct_p=pct_p,
        n_untagged=n_untagged,
        cards="\n".join(cards),
    )
    (OUT_DIR / "index.html").write_text(page)
    print(f"Wrote out/index.html — {len(rows)} articles ({n_untagged} untagged), "
          f"{grand} results ({pct_f}% formalized, {pct_p}% partial)")


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WikiLean — Wikipedia mathematics, mapped to Lean</title>
<meta name="description" content="A mirror of WikiProject Mathematics articles annotated with links into Mathlib, color-coded by formalization coverage.">
<style>
:root {{
  --green:#2da44e; --yellow:#d29922; --red:#cf222e;
}}
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
.lead {{ color:#57606a; font-size:1.02rem; line-height:1.6; max-width:680px; }}
.lead a {{ color:#0969da; text-decoration:none; }}
.lead a:hover {{ text-decoration:underline; }}
.stats {{ display:flex; gap:24px; margin:24px 0 8px; flex-wrap:wrap; font-size:.9rem; color:#57606a; }}
.stats b {{ color:#1f2328; }}
.legend {{ display:flex; gap:16px; font-size:.82rem; color:#57606a; margin-bottom:20px; flex-wrap:wrap; }}
.legend i {{ display:inline-block; width:11px; height:11px; border-radius:2px; margin-right:5px; vertical-align:middle; }}
.concepts-link {{ background:#fff; border:1px solid #d0d7de; border-radius:8px; padding:12px 16px;
  font-size:.9rem; color:#57606a; line-height:1.5; margin:0 0 20px; }}
.concepts-link a {{ color:#0969da; text-decoration:none; font-weight:600; }}
.concepts-link a:hover {{ text-decoration:underline; }}
.controls {{ display:flex; gap:12px; align-items:center; margin-bottom:20px; flex-wrap:wrap; }}
.search {{ flex:1 1 240px; padding:10px 14px; font-size:1rem; border:1px solid #d0d7de;
  border-radius:8px; font-family:inherit; }}
.sort {{ padding:9px 12px; font-size:.9rem; border:1px solid #d0d7de; border-radius:8px;
  background:#fff; color:#1f2328; font-family:inherit; }}
.chk {{ display:flex; gap:6px; align-items:center; font-size:.9rem; color:#57606a;
  cursor:pointer; white-space:nowrap; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:12px; }}
.card {{ display:block; text-decoration:none; color:inherit; border:1px solid #d0d7de;
  border-radius:8px; padding:13px 15px; background:#fff; transition:border-color .12s, box-shadow .12s; }}
.card:hover {{ border-color:#0969da; box-shadow:0 1px 6px rgba(9,105,218,.12); }}
.card-title {{ display:block; font-weight:600; font-size:.98rem; margin-bottom:9px; }}
.bar {{ display:flex; height:7px; border-radius:4px; overflow:hidden; background:#eaeef2; }}
.bar i {{ display:block; height:100%; }}
.bar i.f {{ background:var(--green); }}
.bar i.p {{ background:var(--yellow); }}
.bar i.n {{ background:var(--red); }}
.bar-empty {{ background:#eaeef2; }}
.counts {{ display:block; font-size:.78rem; color:#57606a; margin-top:7px; }}
.counts.untagged {{ color:#8c959f; font-style:italic; }}
.empty {{ color:#57606a; padding:30px 0; }}
footer {{ margin-top:48px; padding-top:20px; border-top:1px solid #d0d7de; font-size:.82rem; color:#57606a; }}
footer a {{ color:#0969da; text-decoration:none; }}
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
    <span><b>{n_articles}</b> articles</span>
    <span><b>{n_results}</b> annotated results</span>
    <span><b>{pct_f}%</b> formalized · <b>{pct_p}%</b> partial</span>
    <span><b>{n_untagged}</b> not yet tagged</span>
  </div>
  <div class="legend">
    <span><i style="background:var(--green)"></i>formalized</span>
    <span><i style="background:var(--yellow)"></i>partial</span>
    <span><i style="background:var(--red)"></i>not formalized</span>
  </div>
  <p class="concepts-link">
    <a href="concepts.html">&rarr; Wikidata concept links</a> &mdash; every formalized
    concept keyed to its Wikidata item, as an open RDF dataset (the basis for a proposed
    <em>&ldquo;formalized as (Lean/Mathlib)&rdquo;</em> Wikidata property).
  </p>
  <p class="concepts-link">
    <a href="article-graph">&rarr; Article graph</a> &mdash; WikiLean articles
    clustered by shared Mathlib formalizations: edges connect articles that annotate
    the same declarations, colored by their dominant Mathlib area.
  </p>
  <p class="concepts-link">
    <a href="graph.html">&rarr; Concept graph</a> &mdash; Mathlib's declaration-level
    dependency edges overlaid on Wikidata's typed item-to-item statements, on a shared
    Wikidata node set. Drag, zoom, click; consensus edges (in both) highlighted.
  </p>
  <div class="controls">
    <input class="search" id="q" type="search" placeholder="Filter articles…" autocomplete="off">
    <select class="sort" id="sort" aria-label="Sort articles">
      <option value="title">Sort: A–Z</option>
      <option value="coverage">Sort: coverage</option>
      <option value="formalized">Sort: most formalized</option>
      <option value="count">Sort: most annotated</option>
    </select>
    <label class="chk"><input type="checkbox" id="hideUntagged"> Hide untagged</label>
  </div>
  <div class="grid" id="grid">
{cards}
  </div>
  <p class="empty" id="empty" style="display:none">No articles match.</p>
  <footer>
    WikiLean &middot; <a href="https://github.com/Deicyde/WikiLean">source on GitHub</a> &middot;
    a project by <a href="https://jackmccarthy.org">Jack McCarthy</a>
  </footer>
</div>
<script>
(function(){{
  var q=document.getElementById('q'), grid=document.getElementById('grid'),
      empty=document.getElementById('empty'), sortSel=document.getElementById('sort'),
      hideU=document.getElementById('hideUntagged'),
      cards=[].slice.call(grid.querySelectorAll('.card'));
  function num(c,a){{ return parseFloat(c.getAttribute(a))||0; }}
  function sortCards(){{
    var mode=sortSel.value, arr=cards.slice();
    arr.sort(function(a,b){{
      if(mode==='coverage') return num(b,'data-cov')-num(a,'data-cov');
      if(mode==='formalized') return num(b,'data-f')-num(a,'data-f');
      if(mode==='count') return num(b,'data-n')-num(a,'data-n');
      return a.getAttribute('data-title').localeCompare(b.getAttribute('data-title'));
    }});
    arr.forEach(function(c){{ grid.appendChild(c); }});
  }}
  function apply(){{
    var t=q.value.trim().toLowerCase(), hu=hideU.checked, shown=0;
    cards.forEach(function(c){{
      var hit=c.getAttribute('data-title').indexOf(t)!==-1
              && !(hu && c.getAttribute('data-untagged')==='1');
      c.style.display=hit?'':'none'; if(hit)shown++;
    }});
    empty.style.display=shown?'none':'';
  }}
  q.addEventListener('input',apply);
  hideU.addEventListener('change',apply);
  sortSel.addEventListener('change',sortCards);
}})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
