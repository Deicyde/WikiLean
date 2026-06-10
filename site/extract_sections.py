#!/usr/bin/env python3
"""Extract plain-text sections from a cached Wikipedia article HTML.

Produces a structured JSON file that annotation agents read instead of raw
HTML. Each section's paragraphs are the EXACT plain text the renderer will
match snippets against — agents copy phrases from this text, eliminating
paraphrase/hallucination errors.

Usage:
    python extract_sections.py <slug>

Reads:  cache/<slug>.html
Writes: cache/<slug>.sections.json   (also prints a human-readable summary)
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"

_HEADING_RE = re.compile(r"<(h[2-4])\b([^>]*)>([\s\S]*?)</\1>", re.IGNORECASE)
_ID_RE = re.compile(r'id="([^"]*)"')
_MATH_RE = re.compile(r"<math\b[^>]*>[\s\S]*?</math>", re.IGNORECASE)
_STYLE_RE = re.compile(r"<style\b[^>]*>[\s\S]*?</style>", re.IGNORECASE)

SKIP_SECTIONS = {"See also", "Notes", "References", "External links", "Further reading"}


def _plain_text(fragment: str) -> str:
    """HTML fragment -> whitespace-collapsed, entity-decoded plain text.
    <math> blocks are replaced with [MATH] so agents know something was there
    but don't see noisy MathML annotation text."""
    t = _STYLE_RE.sub("", fragment)
    t = _MATH_RE.sub(" [MATH] ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def _heading_text(raw: str) -> str:
    t = re.sub(r"<[^>]+>", " ", raw)
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def _split_paragraphs(section_html: str) -> list[str]:
    """Split a section's HTML into plain-text paragraphs.
    Handles <p>, <dd>, <li>, <blockquote>, and <div class="math_theorem">."""
    block_re = re.compile(
        r"<(p|dd|li|blockquote)\b[^>]*>([\s\S]*?)</\1>",
        re.IGNORECASE,
    )
    # Also catch math_theorem boxes
    theorem_re = re.compile(
        r'<div class="math(?:_|&#95;)theorem"[^>]*>([\s\S]*?)</div>',
        re.IGNORECASE,
    )
    # First, find theorem-box ranges so we can skip <p> tags inside them.
    theorem_ranges: list[tuple[int, int]] = []
    for m in theorem_re.finditer(section_html):
        theorem_ranges.append((m.start(), m.end()))

    # Find <ul>/<ol> list ranges (for potential merging into colon-intros).
    list_re = re.compile(r"<(ul|ol)\b[^>]*>([\s\S]*?)</\1>", re.IGNORECASE)
    list_items_by_pos: dict[int, str] = {}
    list_ranges_by_pos: dict[int, tuple[int, int]] = {}
    for m in list_re.finditer(section_html):
        items = re.findall(r"<li\b[^>]*>([\s\S]*?)</li>", m.group(2), re.IGNORECASE)
        items_text = [_plain_text(it) for it in items]
        items_text = [t for t in items_text if t]
        if items_text:
            list_items_by_pos[m.start()] = " ".join(items_text)
            list_ranges_by_pos[m.start()] = (m.start(), m.end())

    def _inside_theorem(pos: int) -> bool:
        return any(s <= pos < e for s, e in theorem_ranges)

    # Track lists CONSUMED by merging into a preceding <p>. <li> items
    # inside consumed lists are skipped (they're already in the merged text).
    # <li> items in non-consumed lists are kept as standalone paragraphs.
    consumed_list_ranges: list[tuple[int, int]] = []

    def _inside_consumed_list(pos: int) -> bool:
        return any(s <= pos < e for s, e in consumed_list_ranges)

    paras = []
    for m in block_re.finditer(section_html):
        if _inside_theorem(m.start()) or _inside_consumed_list(m.start()):
            continue
        t = _plain_text(m.group(2))
        if not t or len(t) <= 5:
            continue
        if m.group(1).lower() == "p" and t.rstrip().endswith(":"):
            for list_pos, list_text in list_items_by_pos.items():
                if list_pos >= m.end() and list_pos - m.end() < 50:
                    t = t + " " + list_text
                    consumed_list_ranges.append(list_ranges_by_pos[list_pos])
                    break
        paras.append(t)
    for m in theorem_re.finditer(section_html):
        t = _plain_text(m.group(1))
        if t and len(t) > 5:
            label_match = re.match(r"((?:Theorem|Lemma|Corollary|Proposition)[^—–\-]*)", t)
            label = label_match.group(1).strip() if label_match else t[:40]
            paras.append(f"[THEOREM BOX: \"{label}\"] {t}")
    return paras


def extract(slug: str) -> dict:
    cache_path = CACHE / f"{slug}.html"
    if not cache_path.exists():
        print(f"missing: {cache_path}", file=sys.stderr)
        sys.exit(1)
    src = cache_path.read_text(encoding="utf-8")

    headings = list(_HEADING_RE.finditer(src))
    sections = []

    # Lead section (before first heading)
    if headings:
        lead_html = src[:headings[0].start()]
        lead_paras = _split_paragraphs(lead_html)
        if lead_paras:
            sections.append({
                "heading": "(Lead)",
                "level": 1,
                "paragraphs": lead_paras,
            })

    seen_headings: dict[str, int] = {}
    for idx, m in enumerate(headings):
        heading = _heading_text(m.group(3))
        level = int(m.group(1)[1])
        attrs = m.group(2)
        id_m = _ID_RE.search(attrs)
        heading_id = id_m.group(1) if id_m else None
        start = m.end()
        end = headings[idx + 1].start() if idx + 1 < len(headings) else len(src)

        if heading in SKIP_SECTIONS:
            continue

        section_html = src[start:end]
        paras = _split_paragraphs(section_html)
        if not paras:
            continue

        # Disambiguate duplicate headings so Agent 1 can tell them apart
        count = seen_headings.get(heading, 0) + 1
        seen_headings[heading] = count
        display_heading = heading if count == 1 else f"{heading} ({count})"

        entry: dict = {
            "heading": display_heading,
            "level": level,
            "paragraphs": paras,
        }
        if heading_id:
            entry["heading_id"] = heading_id
        sections.append(entry)

    return {
        "slug": slug,
        "total_sections": len(sections),
        "total_paragraphs": sum(len(s["paragraphs"]) for s in sections),
        "sections": sections,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("slug")
    args = ap.parse_args()

    data = extract(args.slug)
    out_path = CACHE / f"{args.slug}.sections.json"
    out_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # Human-readable summary to stdout
    print(f"Extracted {data['total_sections']} sections, "
          f"{data['total_paragraphs']} paragraphs -> {out_path}")
    for s in data["sections"]:
        indent = "  " * (s["level"] - 1)
        print(f"{indent}§ {s['heading']}  ({len(s['paragraphs'])} paragraphs)")
        for p in s["paragraphs"][:2]:
            print(f"{indent}    {p[:120]}{'…' if len(p) > 120 else ''}")
        if len(s["paragraphs"]) > 2:
            print(f"{indent}    ... +{len(s['paragraphs']) - 2} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
