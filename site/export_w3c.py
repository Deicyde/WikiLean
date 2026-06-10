#!/usr/bin/env python3
"""Export WikiLean annotations as W3C Web Annotation Data Model (JSON-LD).

For each final annotations/<slug>.json, emit out_w3c/<slug>.anno.jsonld — an
AnnotationPage whose items are standards-compliant Web Annotations:

  target : the Wikipedia article URL + a TextQuoteSelector (our `snippet`,
           with computed prefix/suffix for robust re-anchoring)
  body   : the Mathlib docs link as a SpecificResource (when formalized/
           partial) + classifying TextualBodies (status, match_kind) +
           a describing TextualBody (the note)

Only articles with a final JSON are exported — the HTML-only recovered
articles carry no span anchor and so can't yield a valid TextQuoteSelector.

Usage:
    python export_w3c.py            # all final annotation files
    python export_w3c.py Prime_ideal  # one slug
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from render import mathlib_docs_url  # reuse the docs-URL builder

HERE = Path(__file__).resolve().parent
ANNOT = HERE / "annotations"
CACHE = HERE / "cache"
OUT_W3C = HERE / "out_w3c"

WIKI_BASE = "https://en.wikipedia.org/wiki/"
CONTEXT = "http://www.w3.org/ns/anno.jsonld"
GENERATOR = {
    "type": "Software",
    "name": "WikiLean 2-agent annotation pipeline",
    "homepage": "https://github.com/Deicyde/WikiLean",
}
QUOTE_CONTEXT = 32  # chars of prefix/suffix to capture around the quote


def _load_revid(slug: str) -> int | None:
    """The pinned Wikipedia revision for this article, if recorded."""
    meta = CACHE / f"{slug}.meta.json"
    if not meta.exists():
        return None
    try:
        return json.loads(meta.read_text()).get("revid")
    except (json.JSONDecodeError, OSError):
        return None


def _section_plaintext(slug: str) -> str | None:
    """Joined plain text of the article's sections (for prefix/suffix lookup),
    with [MATH] placeholders stripped so it lines up with our snippets."""
    path = CACHE / f"{slug}.sections.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    parts = []
    for sec in data.get("sections", []):
        parts.extend(sec.get("paragraphs", []))
    text = " ".join(parts)
    text = re.sub(r"\[THEOREM BOX:[^\]]*\]", " ", text)
    text = re.sub(r"\[MATH\]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _quote_selector(exact: str, haystack: str | None) -> dict:
    """A TextQuoteSelector; add prefix/suffix when we can locate the quote."""
    sel = {"type": "TextQuoteSelector", "exact": exact}
    if haystack:
        idx = haystack.find(exact)
        if idx != -1:
            pre = haystack[max(0, idx - QUOTE_CONTEXT):idx]
            suf = haystack[idx + len(exact): idx + len(exact) + QUOTE_CONTEXT]
            if pre:
                sel["prefix"] = pre
            if suf:
                sel["suffix"] = suf
    return sel


def _anchor_exact(anchor: dict) -> str | None:
    """The text a TextQuoteSelector should quote, per anchor type."""
    t = anchor.get("type")
    if t == "theorem_box":
        return anchor.get("value")
    if t == "prose_range":
        return anchor.get("from") or anchor.get("from_snippet")
    return anchor.get("snippet")


def _bodies(a: dict) -> list[dict]:
    m = a.get("mathlib") or {}
    decl = m.get("decl") or a.get("decl")
    module = m.get("module") or a.get("module")
    bodies: list[dict] = []

    url = mathlib_docs_url(module, decl)
    if url and decl:
        bodies.append({
            "type": "SpecificResource",
            "purpose": "linking",
            "source": url,
            "identifier": decl,
        })
    status = a.get("status")
    if status:
        bodies.append({"type": "TextualBody", "purpose": "classifying", "value": status})
    match_kind = m.get("match_kind") or a.get("match_kind")
    if match_kind:
        bodies.append({"type": "TextualBody", "purpose": "classifying",
                       "value": f"match_kind:{match_kind}"})
    kind = a.get("kind")
    if kind:
        bodies.append({"type": "TextualBody", "purpose": "tagging", "value": kind})
    note = a.get("note") or a.get("proof_note")
    if note:
        bodies.append({"type": "TextualBody", "purpose": "describing",
                       "value": note, "format": "text/plain"})
    return bodies


def convert(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    annos = data.get("annotations") or []
    if not annos:
        return None

    slug = data.get("slug") or path.stem
    title = data.get("wikipedia_title") or slug
    live_url = WIKI_BASE + urllib.parse.quote(title.replace(" ", "_"))
    # Pin the target to the immutable revision the annotation was made for.
    # Fall back to the live (mutable) URL only when no revid was recorded.
    revid = _load_revid(slug)
    target_url = f"{live_url}?oldid={revid}" if revid else live_url
    haystack = _section_plaintext(slug)
    created = datetime.fromtimestamp(
        path.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")

    items = []
    for i, a in enumerate(annos):
        anchor = a.get("anchor") or {}
        exact = _anchor_exact(anchor)
        if not exact:
            continue  # nothing to anchor to
        has_link = bool((a.get("mathlib") or {}).get("decl") or a.get("decl"))
        items.append({
            "id": f"urn:wikilean:{slug}:anno-{i}",
            "type": "Annotation",
            "motivation": "linking" if has_link else "classifying",
            "created": created,
            "generator": GENERATOR,
            "body": _bodies(a),
            "target": {
                "source": target_url,
                "selector": _quote_selector(exact, haystack),
            },
        })

    return {
        "@context": CONTEXT,
        "id": f"urn:wikilean:{slug}:page",
        "type": "AnnotationPage",
        "label": f"WikiLean annotations — {title}",
        # partOf points at the live article; each target.source is pinned to
        # the exact revision (?oldid) the annotation was made against.
        "partOf": {"source": live_url, "revid": revid},
        "items": items,
    }


def main() -> int:
    OUT_W3C.mkdir(parents=True, exist_ok=True)
    if len(sys.argv) > 1:
        paths = [ANNOT / f"{sys.argv[1]}.json"]
    else:
        paths = sorted(p for p in ANNOT.glob("*.json")
                       if not p.name.endswith(".agent1.json"))

    n_pages = n_annos = 0
    for path in paths:
        if not path.exists():
            print(f"missing: {path}", file=sys.stderr)
            continue
        page = convert(path)
        if page is None:
            continue
        out_path = OUT_W3C / f"{page['id'].split(':')[2]}.anno.jsonld"
        out_path.write_text(
            json.dumps(page, ensure_ascii=False, indent=2), encoding="utf-8")
        n_pages += 1
        n_annos += len(page["items"])

    print(f"wrote {n_pages} AnnotationPages ({n_annos} annotations) to out_w3c/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
