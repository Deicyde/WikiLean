#!/usr/bin/env python3
"""Generate the concept-layer dashboard — a self-contained, interactive HTML
page surveying Mathlib coverage across the whole concept corpus.

Reads catalog/data/concept_layer.jsonl, embeds it as JSON, and emits
site/out/concepts.html: a filter/sort/search table (all client-side, no server)
with links out to Wikidata (QID), the Mathlib docs (decl), and the local
article page (<slug>.html). Self-contained → deploys to Cloudflare Pages as-is.

    python build_concepts_page.py
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from render import mathlib_docs_url

HERE = Path(__file__).resolve().parent
CONCEPT_LAYER = HERE.parent / "catalog" / "data" / "concept_layer.jsonl"
OUT = HERE / "out" / "concepts.html"

IMP_ORDER = {"Top": 0, "High": 1, "Mid": 2, "Low": 3, None: 4}


def load_rows() -> list[dict]:
    rows = []
    for line in CONCEPT_LAYER.open():
        c = json.loads(line)
        if not c.get("qid"):
            continue
        rows.append({
            "qid": c["qid"],
            "title": c.get("primary_title") or "?",
            "slug": c.get("article_slug"),
            "status": c.get("status"),
            "decl": c.get("primary_decl"),
            "docs": mathlib_docs_url(c.get("module"), c.get("primary_decl"))
                    if c.get("primary_decl") else None,
            "module": c.get("module"),
            "confidence": c.get("confidence"),
            "importance": c.get("importance"),
            "klass": c.get("class"),
            "reason": c.get("no_match_reason"),
        })
    rows.sort(key=lambda r: (IMP_ORDER.get(r["importance"], 4), r["title"].lower()))
    return rows


PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>WikiLean · Mathlib coverage</title>
<style>
:root {{ --green:#2da44e; --red:#cf222e; --amber:#d29922; }}
body {{ font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; margin:0;
  background:#fafbfc; color:#1f2328; }}
header {{ background:#fff; border-bottom:1px solid #d0d7de; padding:16px 28px; }}
h1 {{ font-size:18px; margin:0 0 10px; }}
.stats {{ display:flex; gap:18px; flex-wrap:wrap; font-size:13px; }}
.stat b {{ font-size:18px; display:block; }}
.stat.green b {{ color:var(--green); }} .stat.red b {{ color:var(--red); }}
.controls {{ display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin-top:12px; }}
.controls input, .controls select {{ padding:5px 8px; border:1px solid #ccc; border-radius:5px; font:inherit; }}
#q {{ flex:1; min-width:200px; }}
main {{ padding:0 28px 60px; }}
table {{ border-collapse:collapse; width:100%; font-size:13px; margin-top:14px; }}
th, td {{ text-align:left; padding:7px 10px; border-bottom:1px solid #eaeef2; vertical-align:top; }}
th {{ position:sticky; top:0; background:#f6f8fa; cursor:pointer; user-select:none; white-space:nowrap; }}
th:hover {{ background:#eaeef2; }}
tr:hover td {{ background:#f6f8fa; }}
.pill {{ display:inline-block; padding:1px 8px; border-radius:10px; font-size:11px; font-weight:600; }}
.pill.formalized {{ background:rgba(45,164,78,.12); color:var(--green); }}
.pill.not_formalized {{ background:rgba(207,34,46,.10); color:var(--red); }}
code {{ background:#eef1f4; padding:1px 5px; border-radius:3px; font-size:12px; }}
a {{ color:#0969da; text-decoration:none; }} a:hover {{ text-decoration:underline; }}
.muted {{ color:#888; }}
#count {{ margin-top:8px; font-size:12px; color:#57606a; }}
</style></head>
<body>
<header>
  <h1>WikiLean · Mathlib coverage of WikiProject Mathematics concepts</h1>
  <div class="stats">
    <div class="stat"><b>{total}</b>concepts</div>
    <div class="stat green"><b>{n_form}</b>formalized ({pct_form}%)</div>
    <div class="stat red"><b>{n_not}</b>not formalized</div>
    <div class="stat"><b>{n_top_high}</b>Top/High importance</div>
    <div class="stat"><b>{n_top_high_form}</b>Top/High formalized ({pct_th}%)</div>
  </div>
  <div class="controls">
    <input id="q" type="search" placeholder="search title or declaration…">
    <select id="f-status"><option value="">all status</option>
      <option value="formalized">formalized</option>
      <option value="not_formalized">not formalized</option></select>
    <select id="f-imp"><option value="">all importance</option>
      <option>Top</option><option>High</option><option>Mid</option><option>Low</option></select>
  </div>
  <div id="count"></div>
</header>
<main>
<table id="t"><thead><tr>
  <th data-k="title">Concept</th>
  <th data-k="importance">Imp.</th>
  <th data-k="status">Status</th>
  <th data-k="decl">Mathlib declaration</th>
  <th data-k="confidence">Conf.</th>
  <th data-k="qid">Wikidata</th>
</tr></thead><tbody id="tb"></tbody></table>
</main>
<script>
const ROWS = {data};
const tb = document.getElementById("tb"), countEl = document.getElementById("count");
let sortK = "importance", sortAsc = true;
const impRank = {{Top:0, High:1, Mid:2, Low:3}};

function declCell(r) {{
  if (r.status !== "formalized" || !r.decl)
    return '<span class="muted">' + (r.reason ? r.reason : "—") + '</span>';
  const code = "<code>" + esc(r.decl) + "</code>";
  return r.docs ? '<a href="' + r.docs + '" target="_blank" rel="noopener">' + code + "</a>" : code;
}}
function titleCell(r) {{
  // Link to the local/published article page when we have a slug.
  const t = esc(r.title);
  return r.slug ? '<a href="./' + encodeURIComponent(r.slug) + '.html">' + t + "</a>" : t;
}}
function esc(s) {{ return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }}

function render() {{
  const q = document.getElementById("q").value.toLowerCase().trim();
  const fs = document.getElementById("f-status").value;
  const fi = document.getElementById("f-imp").value;
  let rows = ROWS.filter(r =>
    (!fs || r.status === fs) &&
    (!fi || r.importance === fi) &&
    (!q || (r.title.toLowerCase().includes(q) || (r.decl||"").toLowerCase().includes(q))));
  rows.sort((a,b) => {{
    let x, y;
    if (sortK === "importance") {{ x = impRank[a.importance] ?? 9; y = impRank[b.importance] ?? 9; }}
    else {{ x = (a[sortK]||"").toString().toLowerCase(); y = (b[sortK]||"").toString().toLowerCase(); }}
    if (x < y) return sortAsc ? -1 : 1;
    if (x > y) return sortAsc ? 1 : -1;
    return a.title.toLowerCase() < b.title.toLowerCase() ? -1 : 1;
  }});
  tb.innerHTML = rows.map(r =>
    "<tr><td>" + titleCell(r) + "</td>" +
    "<td>" + esc(r.importance||"") + "</td>" +
    '<td><span class="pill ' + r.status + '">' + r.status.replace("_"," ") + "</span></td>" +
    "<td>" + declCell(r) + "</td>" +
    "<td>" + esc(r.confidence||"") + "</td>" +
    '<td><a href="https://www.wikidata.org/wiki/' + r.qid + '" target="_blank" rel="noopener">' + r.qid + "</a></td></tr>"
  ).join("");
  countEl.textContent = rows.length + " of " + ROWS.length + " concepts";
}}
document.querySelectorAll("th").forEach(th => th.addEventListener("click", () => {{
  const k = th.dataset.k;
  if (sortK === k) sortAsc = !sortAsc; else {{ sortK = k; sortAsc = true; }}
  render();
}}));
["q","f-status","f-imp"].forEach(id => document.getElementById(id).addEventListener("input", render));
render();
</script>
</body></html>
"""


def main() -> int:
    rows = load_rows()
    total = len(rows)
    n_form = sum(1 for r in rows if r["status"] == "formalized")
    th = [r for r in rows if r["importance"] in ("Top", "High")]
    n_th_form = sum(1 for r in th if r["status"] == "formalized")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(PAGE.format(
        total=total, n_form=n_form, n_not=total - n_form,
        pct_form=round(100 * n_form / total) if total else 0,
        n_top_high=len(th), n_top_high_form=n_th_form,
        pct_th=round(100 * n_th_form / len(th)) if th else 0,
        data=json.dumps(rows, ensure_ascii=False).replace("</", "<\\/"),
    ), encoding="utf-8")
    print(f"wrote {OUT}  ({total} concepts: {n_form} formalized, {total - n_form} not)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
