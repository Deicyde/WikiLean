#!/usr/bin/env python3
"""Validate Agent 1's annotation coverage against the extracted sections.

Compares the sections JSON (every paragraph the article has) against Agent 1's
output (which paragraphs got annotations) and reports uncovered paragraphs.
This runs BETWEEN Agent 1 and Agent 2 to catch misses before they ship.

Usage:
    python validate_coverage.py <slug>

Exit code 0 if all substantive paragraphs are covered, 1 if gaps found.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
ANNOT = ROOT / "annotations"

# Paragraphs shorter than this (after stripping [MATH]) are likely just
# isolated formulas or trivial fragments — don't flag them.
MIN_SUBSTANTIVE_LEN = 40


def _strip_math(text: str) -> str:
    return re.sub(r"\[MATH\]", "", text).strip()


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def main() -> int:
    slug = sys.argv[1] if len(sys.argv) > 1 else None
    if not slug:
        print("usage: python validate_coverage.py <slug>", file=sys.stderr)
        return 2

    sections_path = CACHE / f"{slug}.sections.json"
    agent1_path = ANNOT / f"{slug}.agent1.json"

    if not sections_path.exists():
        print(f"missing: {sections_path}", file=sys.stderr)
        return 2
    if not agent1_path.exists():
        print(f"missing: {agent1_path}", file=sys.stderr)
        return 2

    sections = json.loads(sections_path.read_text())["sections"]
    annotations = json.loads(agent1_path.read_text())["annotations"]

    # Collect all snippets from annotations (normalized).
    snippets = set()
    theorem_box_values = set()
    for a in annotations:
        anchors = a.get("anchors") or [a.get("anchor", {})]
        for anc in anchors:
            if anc.get("type") == "theorem_box":
                theorem_box_values.add(_normalize(anc.get("value", "")))
            snippet = anc.get("snippet", "")
            if snippet:
                snippets.add(_normalize(re.sub(r"\[MATH\]", " ", snippet)))

    # Check each paragraph for coverage.
    uncovered = []
    total_substantive = 0
    covered = 0

    for sec in sections:
        heading = sec["heading"]
        for para in sec["paragraphs"]:
            clean = _strip_math(para)
            if len(clean) < MIN_SUBSTANTIVE_LEN:
                continue

            # Theorem boxes are covered by theorem_box anchors.
            if para.startswith("[THEOREM BOX"):
                total_substantive += 1
                label_match = re.match(r'\[THEOREM BOX: "([^"]+)"\]', para)
                if label_match and _normalize(label_match.group(1)) in theorem_box_values:
                    covered += 1
                    continue
                uncovered.append((heading, para))
                continue

            total_substantive += 1
            para_norm = _normalize(re.sub(r"\[MATH\]", " ", para))

            # A paragraph is "covered" if ANY annotation snippet appears in it.
            if any(s in para_norm for s in snippets):
                covered += 1
            else:
                uncovered.append((heading, para))

    # Report.
    pct = (covered / total_substantive * 100) if total_substantive else 0
    print(f"Coverage: {covered}/{total_substantive} substantive paragraphs "
          f"({pct:.0f}%)")
    print(f"Annotations: {len(annotations)}")

    if uncovered:
        print(f"\n{len(uncovered)} UNCOVERED paragraph(s):\n")
        for heading, para in uncovered:
            preview = para[:150].replace("\n", " ")
            print(f"  § {heading}")
            print(f"    {preview}{'…' if len(para) > 150 else ''}")
            print()
        return 1
    else:
        print("\nAll substantive paragraphs have at least one annotation.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
