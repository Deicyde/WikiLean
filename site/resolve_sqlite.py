#!/usr/bin/env python3
"""Resolve anchors in a MIGRATED SQLite copy of the WikiLean database.

Runs after ``wiki/scripts/migrate-native.ts`` has converted a copied database
to native work items. For every native article it fetches the pinned Wikipedia
revision through render.py's cache, resolves each profile-form anchor to a
Literature Links quotation or selector (native_annotations.resolve_locations),
and writes the resolved items back into ``articles.annotations`` and into the
``native companion migration`` revision row, which has not been served yet.
``version`` is unchanged. Never touches D1; the input is a local file. An article
whose resolved snapshot would exceed the Worker's save limits keeps its profile
locations and is reported as skipped.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import native_annotations as native  # noqa: E402
import render  # noqa: E402

# wiki/src/validation.ts MAX_ANNOTATIONS and MAX_ANNOTATIONS_BYTES: the Worker
# refuses every save above them, so a resolved snapshot must stay below.
MAX_ANNOTATIONS = 2000
MAX_ANNOTATIONS_BYTES = 256 * 1024


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, help="the migrated SQLite copy (modified in place)")
    parser.add_argument("--cache", type=Path, help="article HTML cache directory (default: render.py's)")
    parser.add_argument("--limit", type=int, help="stop after this many articles (rehearsal)")
    parser.add_argument("--pause", type=float, default=0.3, help="seconds between Wikipedia fetches")
    parser.add_argument("--report", type=Path, help="write the per-article report here as JSON")
    args = parser.parse_args(argv)
    if args.cache:
        render.CACHE = args.cache
        args.cache.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(args.database))
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT slug, wikipedia_title, revid, annotations, version FROM articles WHERE schema_version=4 ORDER BY slug").fetchall()
    if args.limit:
        rows = rows[: args.limit]
    totals: dict[str, int] = {}
    report = []
    for row in rows:
        items = json.loads(row["annotations"])
        if row["revid"] is None:
            report.append({"slug": row["slug"], "skipped": "no pinned revision"})
            continue
        try:
            html = render.fetch_article_html(row["slug"], row["wikipedia_title"], target_revid=row["revid"])
        except Exception as exc:  # noqa: BLE001 — recorded, the article keeps its profile locations
            report.append({"slug": row["slug"], "skipped": f"fetch failed: {exc}"})
            continue
        time.sleep(args.pause)
        counts, diagnostics = native.resolve_locations(items, html)
        native.validate_items(items, title=row["wikipedia_title"], revid=row["revid"])
        encoded = json.dumps(items, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        # The Worker measures JSON.stringify(...).length (UTF-16 units); a snapshot
        # above its caps could never be saved again, so keep the profile form instead.
        if len(items) > MAX_ANNOTATIONS or len(encoded.encode("utf-16-le")) // 2 > MAX_ANNOTATIONS_BYTES:
            report.append({"slug": row["slug"], "skipped": "resolved snapshot exceeds Worker save limits; profile locations kept"})
            continue
        with db:
            db.execute("UPDATE articles SET annotations=? WHERE slug=? AND version=?", (encoded, row["slug"], row["version"]))
            db.execute("""UPDATE revisions SET annotations=?, meta=json_set(COALESCE(meta,'{}'), '$.location_resolution', json(?))
                          WHERE slug=? AND kind='native-migration' AND id=(SELECT MAX(id) FROM revisions WHERE slug=? AND kind='native-migration')""",
                       (encoded, json.dumps(counts), row["slug"], row["slug"]))
        for key, value in counts.items():
            totals[key] = totals.get(key, 0) + value
        report.append({"slug": row["slug"], "counts": counts, "unresolved": len(diagnostics)})
    summary = {"schema": "wikilean.anchor-resolution-report/v1", "articles": len(rows), "totals": totals, "report": report}
    if args.report:
        args.report.write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"articles": len(rows), "totals": totals, "skipped": sum(1 for r in report if "skipped" in r)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
