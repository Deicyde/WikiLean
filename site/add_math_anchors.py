#!/usr/bin/env python3
"""Augment v3 annotations with `math_alttext` anchors for every display-math
element contained in (or absorbed-by) the annotation's prose-block wrap.

Why: the v3 pipeline emits only section+snippet anchors. The engine extends
those wraps over adjacent display math at render time, but the data itself
doesn't *say* which equations belong to which annotation. This script makes the
annotations self-describing — each annotation's `anchors[]` array gains a
`math_alttext` entry per equation that the engine would visually highlight.

Effect on rendering: the math element gets its OWN span.anno wrapper (background
color) nested inside the existing div.anno (border-left). So display math shows
the annotation's color instead of being just "inside an annotated block."

Idempotent: re-running adds no new anchors. Only touches v3 annotations whose
canonical file (annotations/<slug>.json) exists alongside cache/<slug>.html.

Usage:
    python site/add_math_anchors.py --dry-run
    python site/add_math_anchors.py
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Reuse the engine's matching helpers — this script must place anchors EXACTLY
# where the live engine will resolve them.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import render as r  # noqa: E402

ANNOT = HERE / "annotations"
CACHE = HERE / "cache"

# Capture the alttext value of each display-math element. The class string
# matches what render.py's _DISPLAY_MATH_OPEN_RE recognizes. DOTALL because the
# alttext sits inside a nested <math> element a few lines below the span open.
_MATH_ELEMENT_BLOCK_RE = re.compile(
    r'<span class="mwe-math-element mwe-math-element-block">.*?alttext="([^"]+)"',
    re.IGNORECASE | re.DOTALL,
)


def display_math_alttexts_in(src: str, start: int, end: int) -> list[str]:
    """Alttext values of every display-math element within src[start:end],
    deduplicated, preserving document order. Stored as the raw HTML-attribute
    string (find_math_span tries both raw and html-escape variants)."""
    seen: dict[str, None] = {}
    for m in _MATH_ELEMENT_BLOCK_RE.finditer(src, start, end):
        seen.setdefault(m.group(1), None)
    return list(seen)


def process_file(annot_path: Path, dry_run: bool) -> tuple[int, int]:
    """Returns (annotations_touched, anchors_added)."""
    if ".agent1" in annot_path.name:
        return (0, 0)
    slug = annot_path.stem
    cache_path = CACHE / f"{slug}.html"
    if not cache_path.exists():
        return (0, 0)
    try:
        model = json.loads(annot_path.read_text())
    except (json.JSONDecodeError, OSError):
        return (0, 0)
    if model.get("schema_version") != 3:
        return (0, 0)

    src = r.absolutize_wikipedia_urls(cache_path.read_text())

    annos_touched = 0
    anchors_added = 0

    for a in model.get("annotations", []) or []:
        anchor = a.get("anchor") or {}
        # Only enhance section+snippet anchors; math_alttext/theorem_box/
        # prose_range already point at specific elements.
        if anchor.get("type"):
            continue
        if "section" not in anchor or "snippet" not in anchor:
            continue

        loc = r.find_prose_block(src, anchor["section"], anchor["snippet"])
        if loc is None:
            continue
        wrap_start, wrap_end, _wrapper = loc

        math_alttexts = display_math_alttexts_in(src, wrap_start, wrap_end)
        if not math_alttexts:
            continue

        # Preserve any existing anchors[] (and ensure the original `anchor` is
        # in there) before appending the math ones.
        anchors = a.get("anchors")
        if anchors is None:
            anchors = [a["anchor"]]
        already = {x.get("value") for x in anchors if x.get("type") == "math_alttext"}

        new_here = 0
        for v in math_alttexts:
            if v in already:
                continue
            anchors.append({"type": "math_alttext", "value": v})
            already.add(v)
            new_here += 1

        if new_here:
            a["anchors"] = anchors
            annos_touched += 1
            anchors_added += new_here

    if anchors_added and not dry_run:
        annot_path.write_text(json.dumps(model, ensure_ascii=False, indent=2))
    return (annos_touched, anchors_added)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    files = sorted(ANNOT.glob("*.json"))
    if args.limit:
        files = files[: args.limit]

    files_touched = 0
    total_annos = 0
    total_anchors = 0
    for f in files:
        annos, anchors = process_file(f, args.dry_run)
        if anchors:
            files_touched += 1
            total_annos += annos
            total_anchors += anchors

    label = "[DRY-RUN] would update" if args.dry_run else "updated"
    print(
        f"{label} {files_touched} annotation files / "
        f"{total_annos} annotations / +{total_anchors} math_alttext anchors"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
