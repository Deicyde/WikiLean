#!/usr/bin/env python3
"""Post-hoc analysis of produced annotations.

Crawls every final annotations/<slug>.json (skipping the *.agent1.json
intermediates) and builds a tidy per-article dataset plus corpus-wide
aggregates. Re-runnable at any point during the batch — it reflects whatever
has completed so far. The per-article CSV is the substrate for trend plots
(formalization rate over completion order, coverage by field, etc.), which are
best done in post (a notebook) rather than baked into the live run.

Reads:   annotations/*.json   (excluding *.agent1.json)
Writes:  data/annotation_summary.csv     (one row per article)
         data/module_frequency.csv       (Mathlib modules ranked by use)
         data/annotation_aggregates.json  (corpus-wide rollup)
Prints:  a human-readable corpus summary.

Usage:
    python analyze_annotations.py
"""
from __future__ import annotations

import csv
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ANNOT = HERE / "annotations"
OUT = HERE / "out"
DATA = HERE.parent / "catalog" / "data"

_EMBED_RE = re.compile(r"window\.__WL_ANNOTATIONS__ = (\[.*?\]);", re.DOTALL)


def load_embedded(html_path: Path) -> list[dict] | None:
    """Recover the annotation list embedded in a rendered HTML page (for
    articles whose final annotations/<slug>.json is no longer on disk).
    Older renders embed only status+note; newer ones include decl/module/kind."""
    try:
        m = _EMBED_RE.search(html_path.read_text())
    except OSError:
        return None
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None

STATUSES = ("formalized", "partial", "not_formalized")
KINDS = ("definition", "proposition", "theorem", "example")


def _decl_module(a: dict) -> tuple[str | None, str | None]:
    """Pull (decl, module) from either the v3 nested `mathlib` object or a
    flat schema."""
    m = a.get("mathlib") or {}
    return (m.get("decl") or a.get("decl"),
            m.get("module") or a.get("module"))


def analyze_annotations(slug: str, title: str, annos: list[dict],
                        mtime: str, source: str) -> dict | None:
    if not annos:
        return None

    status_counts = Counter(a.get("status") for a in annos)
    kind_counts = Counter(a.get("kind") for a in annos)
    modules = [m for _, m in (_decl_module(a) for a in annos) if m]
    decls = [d for d, _ in (_decl_module(a) for a in annos) if d]

    n = len(annos)
    n_form = status_counts.get("formalized", 0)
    n_part = status_counts.get("partial", 0)
    n_none = status_counts.get("not_formalized", 0)

    return {
        "slug": slug,
        "title": title,
        "source": source,
        "n_annotations": n,
        "n_formalized": n_form,
        "n_partial": n_part,
        "n_not_formalized": n_none,
        # Two rates: strict (formalized only) and lenient (formalized+partial).
        "formalization_rate": round(n_form / n, 3) if n else 0,
        "coverage_rate": round((n_form + n_part) / n, 3) if n else 0,
        "n_definition": kind_counts.get("definition", 0),
        "n_proposition": kind_counts.get("proposition", 0),
        "n_theorem": kind_counts.get("theorem", 0),
        "n_example": kind_counts.get("example", 0),
        "n_distinct_modules": len(set(modules)),
        "modules": modules,
        "decls": decls,
        "mtime": mtime,
    }


def main() -> int:
    # Ground truth for "done" is the rendered HTML set. For each, prefer the
    # final annotations/<slug>.json (full schema); fall back to the data
    # embedded in the rendered HTML when that JSON is gone.
    rows = []
    n_from_json = n_from_html = 0
    for html_path in sorted(OUT.glob("*.html")):
        slug = html_path.stem
        json_path = ANNOT / f"{slug}.json"
        mtime = datetime.fromtimestamp(
            html_path.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
        if json_path.exists():
            try:
                data = json.loads(json_path.read_text())
            except (json.JSONDecodeError, OSError):
                data = None
            if data and data.get("annotations"):
                title = data.get("display_title") or data.get("wikipedia_title") or slug
                r = analyze_annotations(slug, title, data["annotations"], mtime, "json")
                if r:
                    rows.append(r); n_from_json += 1
                continue
        embedded = load_embedded(html_path)
        if embedded:
            r = analyze_annotations(slug, slug, embedded, mtime, "html")
            if r:
                rows.append(r); n_from_html += 1
    if not rows:
        print("no annotation data found")
        return 0
    print(f"sources: {n_from_json} from final JSON, {n_from_html} from embedded HTML\n")

    DATA.mkdir(parents=True, exist_ok=True)

    # 1. Per-article CSV (ordered by completion time so trends are easy to plot).
    rows_by_time = sorted(rows, key=lambda r: r["mtime"])
    csv_cols = ["slug", "title", "mtime", "n_annotations", "n_formalized",
                "n_partial", "n_not_formalized", "formalization_rate",
                "coverage_rate", "n_definition", "n_proposition", "n_theorem",
                "n_example", "n_distinct_modules"]
    with (DATA / "annotation_summary.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=csv_cols, extrasaction="ignore")
        w.writeheader()
        for r in rows_by_time:
            w.writerow(r)

    # 2. Module frequency (which areas of Mathlib cover the most concepts).
    all_modules = Counter()
    all_decls = Counter()
    for r in rows:
        all_modules.update(r["modules"])
        all_decls.update(r["decls"])
    with (DATA / "module_frequency.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["module", "annotation_count"])
        for mod, cnt in all_modules.most_common():
            w.writerow([mod, cnt])

    # 3. Corpus aggregates.
    n_articles = len(rows)
    total_annos = sum(r["n_annotations"] for r in rows)
    total_form = sum(r["n_formalized"] for r in rows)
    total_part = sum(r["n_partial"] for r in rows)
    total_none = sum(r["n_not_formalized"] for r in rows)
    fully_formalized = sum(1 for r in rows if r["n_annotations"] and r["n_formalized"] == r["n_annotations"])
    zero_formalized = sum(1 for r in rows if r["n_formalized"] == 0)

    aggregates = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "articles_analyzed": n_articles,
        "total_annotations": total_annos,
        "annotations_per_article_avg": round(total_annos / n_articles, 1),
        "status_totals": {
            "formalized": total_form, "partial": total_part, "not_formalized": total_none},
        "status_pct": {
            "formalized": round(100 * total_form / total_annos, 1) if total_annos else 0,
            "partial": round(100 * total_part / total_annos, 1) if total_annos else 0,
            "not_formalized": round(100 * total_none / total_annos, 1) if total_annos else 0},
        "articles_fully_formalized": fully_formalized,
        "articles_zero_formalized": zero_formalized,
        "distinct_modules": len(all_modules),
        "distinct_decls": len(all_decls),
        "top_modules": all_modules.most_common(15),
        "top_decls": all_decls.most_common(15),
    }
    (DATA / "annotation_aggregates.json").write_text(
        json.dumps(aggregates, ensure_ascii=False, indent=2), encoding="utf-8")

    # Print summary.
    print(f"Analyzed {n_articles} articles, {total_annos} annotations "
          f"(avg {aggregates['annotations_per_article_avg']}/article)")
    print(f"  formalized:     {total_form:5d} ({aggregates['status_pct']['formalized']}%)")
    print(f"  partial:        {total_part:5d} ({aggregates['status_pct']['partial']}%)")
    print(f"  not_formalized: {total_none:5d} ({aggregates['status_pct']['not_formalized']}%)")
    print(f"  articles fully formalized: {fully_formalized}  |  zero formalized: {zero_formalized}")
    print(f"  distinct Mathlib modules referenced: {len(all_modules)}")
    print(f"\n  top Mathlib modules by annotation count:")
    for mod, cnt in all_modules.most_common(10):
        print(f"    {cnt:4d}  {mod}")
    print(f"\nwrote: data/annotation_summary.csv, data/module_frequency.csv, "
          f"data/annotation_aggregates.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
