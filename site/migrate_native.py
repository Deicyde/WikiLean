#!/usr/bin/env python3
"""Explicit offline legacy-to-native article migration; never reads or writes D1.

The destination must be new. The original JSON bytes, converted article and
conversion report are published together as a reviewable local bundle.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from native_annotations import NativeAnnotationError, migrate_items, strict_loads


def _publish_new(source: Path, destination: Path) -> None:
    """Atomically publish without replacing a concurrently created directory."""
    library = ctypes.CDLL(None, use_errno=True)
    left, right = os.fsencode(source), os.fsencode(destination)
    try:
        if sys.platform.startswith("linux"):
            rename = library.renameat2
            rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            result = rename(-100, left, -100, right, 1)  # RENAME_NOREPLACE
        elif sys.platform == "darwin":
            rename = library.renamex_np
            rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            result = rename(left, right, 4)  # RENAME_EXCL
        else:
            raise AttributeError(sys.platform)
    except AttributeError as exc:
        raise NativeAnnotationError(f"atomic no-replace publication is unavailable on {sys.platform}") from exc
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def location_forms(items: list[dict]) -> dict[str, int]:
    """How the stored occurrences locate their passages: quote, css, profile, fragment, none."""
    counts: dict[str, int] = {}
    for item in items:
        records = [link.get("annotation", {}) for link in item.get("links", []) if isinstance(link, dict)]
        records += [span for span in item.get("unlinked", []) if isinstance(span, dict)]
        for record in records:
            location = record.get("location")
            form = ("none" if not isinstance(location, dict) else "profile" if "profile" in location
                    else next((key for key in ("quote", "css", "fragment") if key in location), "other"))
            counts[form] = counts.get(form, 0) + 1
    return counts


def migrate_file(source: Path, destination: Path, *, revid: int | None = None, html: str | None = None) -> dict:
    source, destination = Path(source), Path(destination)
    if destination.exists() or destination.is_symlink():
        raise NativeAnnotationError("migration output must not exist")
    original = source.read_bytes()
    article = strict_loads(original.decode("utf-8"))
    if not isinstance(article, dict) or not isinstance(article.get("annotations"), list):
        raise NativeAnnotationError("expected an article object with annotations")
    title = article.get("wikipedia_title", article.get("wikipediaTitle"))
    pin = revid if revid is not None else article.get("revid")
    items, diagnostics = migrate_items(article["annotations"], title=title, revid=pin, html=html)
    migrated = {**article, "annotations": items, "schema_version": 4, "revid": pin}
    report = {"source": str(source.resolve()), "items": len(items),
              "links": sum(len(item["links"]) for item in items),
              "unlinked": sum(len(item["unlinked"]) for item in items),
              "locations": location_forms(items), "resolved_against_html": html is not None,
              "diagnostics": diagnostics, "d1_updated": False}
    texts = {"article.json": json.dumps(migrated, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
             "report.json": json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n"}
    for text in texts.values():
        text.encode("utf-8")
    stage = Path(tempfile.mkdtemp(prefix=".native-migration-", dir=destination.parent))
    try:
        (stage / "original.json").write_bytes(original)
        for name, text in texts.items():
            (stage / name).write_text(text, encoding="utf-8")
        _publish_new(stage, destination)
    except BaseException:
        if stage.exists():
            shutil.rmtree(stage)
        raise
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--revid", type=int, help="explicit pinned revision for legacy sidecars without one")
    html_group = parser.add_mutually_exclusive_group()
    html_group.add_argument("--article-html", type=Path,
                            help="the pinned article HTML; anchors are resolved to Literature Links quotations and selectors")
    html_group.add_argument("--fetch-article", action="store_true",
                            help="read the pinned article HTML through render.py's cache (fetches from Wikipedia when absent)")
    args = parser.parse_args(argv)
    try:
        html = None
        if args.article_html is not None:
            html = args.article_html.read_text(encoding="utf-8")
        elif args.fetch_article:
            import render
            article = strict_loads(args.source.read_text(encoding="utf-8"))
            title = article.get("wikipedia_title", article.get("wikipediaTitle"))
            pin = args.revid if args.revid is not None else article.get("revid")
            html = render.fetch_article_html(article.get("slug") or args.source.stem, title, target_revid=pin)
        report = migrate_file(args.source, args.output, revid=args.revid, html=html)
    except (NativeAnnotationError, OSError, ValueError, UnicodeError) as exc:
        parser.exit(2, f"migration failed: {exc}\n")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
