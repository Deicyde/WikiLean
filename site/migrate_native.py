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


def migrate_file(source: Path, destination: Path, *, revid: int | None = None) -> dict:
    source, destination = Path(source), Path(destination)
    if destination.exists() or destination.is_symlink():
        raise NativeAnnotationError("migration output must not exist")
    original = source.read_bytes()
    article = strict_loads(original.decode("utf-8"))
    if not isinstance(article, dict) or not isinstance(article.get("annotations"), list):
        raise NativeAnnotationError("expected an article object with annotations")
    title = article.get("wikipedia_title", article.get("wikipediaTitle"))
    pin = revid if revid is not None else article.get("revid")
    items, diagnostics = migrate_items(article["annotations"], title=title, revid=pin)
    migrated = {**article, "annotations": items, "schema_version": 4, "revid": pin}
    report = {"source": str(source.resolve()), "items": len(items),
              "links": sum(len(item["links"]) for item in items),
              "unlinked": sum(len(item["unlinked"]) for item in items),
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
    args = parser.parse_args(argv)
    try:
        report = migrate_file(args.source, args.output, revid=args.revid)
    except (NativeAnnotationError, OSError, ValueError, UnicodeError) as exc:
        parser.exit(2, f"migration failed: {exc}\n")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
