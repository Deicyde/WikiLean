#!/usr/bin/env python3
"""Render a WikiLean-annotated Wikipedia article to a self-contained HTML file.

Usage:
    python render.py <slug>          # e.g. Picard-Lindelof_theorem

Inputs:
    annotations/<slug>.json          # WikiLean annotation sidecar
    cache/<slug>.html                # cached MediaWiki action=parse output
                                     # (fetched on demand if missing)
    assets/style.css, assets/script.js

Output:
    out/<slug>.html                  # standalone HTML page; open in a browser
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
ANNOT = ROOT / "annotations"
OUT = ROOT / "out"
ASSETS = ROOT / "assets"

WIKI_API = "https://en.wikipedia.org/w/api.php"
UA = "WikiLean/0.1 (https://github.com/; jack.mccarthy.1@stonybrook.edu)"

MATHLIB_DOCS = "https://leanprover-community.github.io/mathlib4_docs"
BASE_URL = "https://wikilean.jackmccarthy.org"


def absolutize_wikipedia_urls(body: str) -> str:
    """Rewrite MediaWiki's relative URLs so the page works from a file:// URL.

    MediaWiki emits two relative shapes:
      - protocol-relative: src="//upload.wikimedia.org/..."   → https://
      - site-relative:     href="/wiki/...", src="/w/..."     → https://en.wikipedia.org/...
    Without these rewrites, image figures 404 and wiki-links land on file:///wiki/...
    """
    # Protocol-relative URLs in src/href/srcset attribute values
    body = re.sub(r'((?:src|href|srcset)=")//', r'\1https://', body)
    # Subsequent URLs inside a srcset list are comma-separated
    body = re.sub(r'(,\s*)//', r'\1https://', body)
    # Site-relative URLs → en.wikipedia.org
    body = re.sub(
        r'((?:src|href|srcset)=")(/(?:wiki|w)/)',
        r'\1https://en.wikipedia.org\2',
        body,
    )
    return body


def mathlib_docs_url(module: str | None, decl: str | None) -> str | None:
    """Build a link into the rendered Mathlib4 docs from a dotted module + decl name."""
    if not module:
        return None
    rel = module.replace(".", "/") + ".html"
    return f"{MATHLIB_DOCS}/{rel}#{decl}" if decl else f"{MATHLIB_DOCS}/{rel}"


def fetch_article_html(slug: str, wikipedia_title: str) -> str:
    """Return the rendered article HTML, fetching once and caching to disk.

    Captures the parsed revision's `revid` atomically with the HTML and writes
    a cache/<slug>.meta.json sidecar, so annotations can be pinned to the exact
    Wikipedia revision they were made against (immutable ?oldid permalink)."""
    from datetime import datetime, timezone

    CACHE.mkdir(exist_ok=True)
    cache_path = CACHE / f"{slug}.html"
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8")

    # prop=text|revid → HTML and the revision id in one call (kept consistent).
    qs = urllib.parse.urlencode({
        "action": "parse",
        "page": wikipedia_title,
        "prop": "text|revid",
        "format": "json",
        "formatversion": "2",
        "redirects": "1",
    })
    req = urllib.request.Request(f"{WIKI_API}?{qs}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    if "parse" not in data:
        raise RuntimeError(f"MediaWiki returned no parse block: {data}")
    text = data["parse"]["text"]
    cache_path.write_text(text, encoding="utf-8")
    revid = data["parse"].get("revid")
    if revid:
        meta = {
            "slug": slug,
            "wikipedia_title": wikipedia_title,
            "revid": revid,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "pinned_via": "fetch",
        }
        (CACHE / f"{slug}.meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return text


_THEOREM_BOX_OPEN = re.compile(r'<div class="math(?:_|&#95;)theorem"[^>]*>')


def find_theorem_box(src: str, needle: str) -> tuple[int, int] | None:
    """Locate a `<div class="math_theorem">…</div>` whose plain-text content
    contains `needle`. Returns (start, end) byte offsets, or None.
    `<div>` depth is tracked so the matcher survives nested divs."""
    for m in _THEOREM_BOX_OPEN.finditer(src):
        depth = 1
        pos = m.end()
        while depth > 0 and pos < len(src):
            nx_open = src.find("<div", pos)
            nx_close = src.find("</div>", pos)
            if nx_close == -1:
                break
            if nx_open != -1 and nx_open < nx_close:
                depth += 1
                pos = nx_open + 4
            else:
                depth -= 1
                pos = nx_close + len("</div>")
        if depth != 0:
            continue
        inner = src[m.end():pos - len("</div>")]
        text = re.sub(r"<[^>]+>", " ", inner)
        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        if needle in text:
            return (m.start(), pos)
    return None


def _strip_wikitext_markup(s: str) -> str:
    """Best-effort strip of MediaWiki markup so a wikitext-derived snippet
    can be matched against rendered HTML prose. Handles the common cases:
      [[X]]       → X
      [[X|Y]]     → Y
      '''X'''     → X
      ''X''       → X
      {{math|X}}  → X
      {{tag|...}} → '' (drop unknown templates entirely)
    """
    s = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", s)   # [[A|B]] → B
    s = re.sub(r"\[\[([^\]]+)\]\]", r"\1", s)               # [[X]]   → X
    s = re.sub(r"'''([^']+)'''", r"\1", s)                  # '''X''' → X
    s = re.sub(r"''([^']+)''", r"\1", s)                    # ''X''   → X
    s = re.sub(r"\{\{(?:math|mvar|nowrap)\|([^}]+)\}\}", r"\1", s, flags=re.IGNORECASE)
    s = re.sub(r"\{\{[^}]*\}\}", "", s)                     # drop other templates
    s = re.sub(r"&\w+;", " ", s)                            # &thinsp; etc.
    s = re.sub(r"\s+", " ", s).strip()
    return s


_H_HEADING_RE = re.compile(r"<(h[234])\b[^>]*>([\s\S]*?)</\1>", re.IGNORECASE)
_SECTION_DISAMBIG_RE = re.compile(r"^(.*?)\s*\((\d+)\)$")


def _find_section_bounds(src: str, section: str) -> tuple[int, int] | None:
    """Return (start, end) offsets for the content of a section named `section`.

    Supports disambiguated names: "Examples (2)" matches the 2nd heading
    whose text is "Examples"."""
    m_dis = _SECTION_DISAMBIG_RE.match(section.strip())
    if m_dis:
        base, occurrence = m_dis.group(1).strip().lower(), int(m_dis.group(2))
    else:
        base, occurrence = section.strip().lower(), 1

    # "(Lead)" means the content before the first heading.
    if base == "(lead)":
        headings = list(_H_HEADING_RE.finditer(src))
        first = headings[0].start() if headings else len(src)
        return (0, first)

    headings = list(_H_HEADING_RE.finditer(src))
    # Two passes: first try exact match, then substring match.
    # This prevents "examples" from matching "Non-examples".
    for exact_only in (True, False):
        seen = 0
        for idx, m in enumerate(headings):
            head_text = re.sub(r"<[^>]+>", " ", m.group(2))
            head_text = re.sub(r"\s+", " ", head_text).strip().lower()
            if exact_only:
                match = head_text == base
            else:
                match = base in head_text
            if match:
                seen += 1
                if seen == occurrence:
                    section_pos = m.end()
                    level = int(m.group(1)[1])
                    section_end = len(src)
                    for n in headings[idx + 1:]:
                        if int(n.group(1)[1]) <= level:
                            section_end = n.start()
                            break
                    return (section_pos, section_end)
    return None
_BLOCK_OPENERS = [
    # Order matters — try most-specific first, then fall back to <p>.
    ('<div class="math_theorem"', "</div>", True),
    ('<div class="math&#95;theorem"', "</div>", True),
    ("<blockquote", "</blockquote>", False),
    ("<dl>", "</dl>", False),
    ("<dd>", "</dd>", False),
    ("<li>", "</li>", False),
    ("<p>", "</p>", False),
]


def _close_block(src: str, open_pos: int, open_tag: str, close_tag: str,
                 depth_track: bool) -> int | None:
    """Find the offset of the END of the block element starting at open_pos."""
    # Move past the opening tag's '>'
    pos = src.find(">", open_pos)
    if pos == -1:
        return None
    pos += 1
    if not depth_track:
        end = src.find(close_tag, pos)
        return end + len(close_tag) if end != -1 else None
    # Depth-track for divs (which can nest)
    depth = 1
    while depth > 0 and pos < len(src):
        nx_open = src.find("<div", pos)
        nx_close = src.find(close_tag, pos)
        if nx_close == -1:
            return None
        if nx_open != -1 and nx_open < nx_close:
            depth += 1
            pos = nx_open + 4
        else:
            depth -= 1
            pos = nx_close + len(close_tag)
    return pos


_MATH_ELEMENT_RE = re.compile(r"<math\b[^>]*>[\s\S]*?</math>", re.IGNORECASE)


def _block_plain_text(src: str, start: int, end: int) -> str:
    """Plain-text content of an HTML region: tags stripped, entities decoded,
    whitespace collapsed. `<math>…</math>` elements are removed entirely
    first because the MathML annotation text inside them (e.g.
    `{\\displaystyle X}`) is invisible to readers but would otherwise pollute
    the plain-text projection and break prose-snippet matches."""
    body = _MATH_ELEMENT_RE.sub(" ", src[start:end])
    t = re.sub(r"<[^>]+>", " ", body)
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


_INLINE_TAGS = ("a", "b", "i", "em", "strong", "span", "sup", "q", "cite", "dfn", "code")


def find_prose_block(src: str, section: str, snippet: str) -> tuple[int, int, str] | None:
    """Locate the smallest HTML element in `section` whose plain-text content
    contains `snippet`.

    Returns (start, end, wrapper_tag), where `wrapper_tag` is `"span"` for
    inline elements (a, i, em, …) and `"div"` for block elements (p, dd, …).
    Inline elements are preferred when the snippet matches one: a tight
    snippet like `"Banach fixed-point theorem"` will wrap just the wikilink,
    not the whole paragraph it lives in.
    """
    # 1. Locate the section.
    bounds = _find_section_bounds(src, section)
    if bounds is None:
        return None
    section_pos, section_end = bounds

    # 2. Build search keys (raw + wikitext-stripped, both whitespace-collapsed).
    #    Also strip [MATH] placeholders (from extract_sections.py output) to
    #    whitespace, matching the renderer's math-stripping behavior.
    math_stripped = re.sub(r"\[MATH\]", " ", snippet)
    keys = [snippet, _strip_wikitext_markup(snippet), math_stripped]
    keys = list(dict.fromkeys(re.sub(r"\s+", " ", k).strip() for k in keys if k))
    keys_lower = [k.lower() for k in keys]

    # 3. Try inline elements first; collect every match and pick the
    #    smallest plain-text content. A "tight" snippet that matches an
    #    <a> tag's text will beat a "loose" snippet that matches a <p>.
    inline_matches: list[tuple[int, int, int]] = []  # (content_len, start, end)
    for tag in _INLINE_TAGS:
        open_re = re.compile(rf"<{tag}(?:\s[^>]*)?>", re.IGNORECASE)
        close_tag = f"</{tag}>"
        for m in open_re.finditer(src, section_pos, section_end):
            ep = src.find(close_tag, m.end(), section_end)
            if ep == -1:
                continue
            content = _block_plain_text(src, m.end(), ep)
            if any(k in content.lower() for k in keys_lower):
                inline_matches.append((len(content), m.start(), ep + len(close_tag)))
    if inline_matches:
        inline_matches.sort()
        _, s, e = inline_matches[0]
        return (s, e, "span")

    # 4. Fall back to block elements. When a match is found, first try to
    #    wrap just the snippet's plain-text range as a tight <span> inside
    #    the block. Only wrap the entire block as a <div> if the tight range
    #    can't be located — this prevents five annotations in the same <p>
    #    from each wrapping the whole paragraph.
    block_open_re = re.compile(
        r'<(p|blockquote|dl|dd|li|div(?=\s+class="math(?:_|&#95;)theorem"))\b[^>]*>',
        re.IGNORECASE,
    )
    for m in block_open_re.finditer(src, section_pos, section_end):
        tag = m.group(1).lower()
        if tag == "div":
            end = _close_block(src, m.start(), "<div", "</div>", True)
        else:
            close_tag = f"</{tag}>"
            ep = src.find(close_tag, m.end(), section_end)
            end = ep + len(close_tag) if ep != -1 else None
        if end is None:
            continue
        block_text = _block_plain_text(src, m.end(), end - len(f"</{tag}>"))
        if any(k in block_text.lower() for k in keys_lower):
            # Expand to sentence boundaries around the snippet.
            for k in keys:
                rng = _find_sentence_range(src, m.start(), end, k)
                if rng is not None:
                    # If the sentence ends with ':' AND a list immediately
                    # follows the block (e.g. "P is prime if: ...<ul><li>..."),
                    # extend the highlight to include that list — the colon
                    # signals a continuation. Crossing the <p>→<ul> boundary
                    # requires a block (<div>) wrapper around WHOLE blocks: a
                    # <span> starting mid-<p> would be auto-closed at </p>,
                    # leaving the list unhighlighted. We snap the start back to
                    # the block opening (m.start()).
                    if _matched_ends_with_colon(src, rng[0], rng[1]):
                        list_end = _find_following_list(src, end, section_end)
                        if list_end is not None:
                            return (m.start(), list_end, "div")
                    # If the matched paragraph contains display math anywhere
                    # (the sentence often *swallows* the equation because
                    # _find_sentence_range only stops at .!?, not ':'), or if
                    # display math follows the paragraph, promote the wrap to a
                    # block-level <div>. A <span> wrap can't visually highlight
                    # a child element with display:block (the bg doesn't extend
                    # past an inline parent), so the equation would render
                    # un-highlighted even when it's technically inside the wrap.
                    block_has_display_math = src.find(
                        "mwe-math-element-block", m.start(), end) != -1
                    math_after = _find_following_display_math(src, end, section_end)
                    if block_has_display_math or math_after is not None:
                        new_end = math_after if math_after is not None else end
                        return (m.start(), new_end, "div")
                    return (rng[0], rng[1], "span")
            # Fallback: wrap the entire block (extended over any display math
            # that follows it, for the same definition+equation pattern).
            math_end = _find_following_display_math(src, end, section_end)
            if math_end is not None:
                return (m.start(), math_end, "div")
            return (m.start(), end, "div")
    return None


def find_math_span(src: str, alttext_value: str) -> tuple[int, int] | None:
    """Locate the outer <span class="mwe-math-element mwe-math-element-block">..</span>
    that wraps a display-math element whose `alttext` attribute equals
    `alttext_value` (compared either raw or HTML-escaped).
    Returns (start, end) byte offsets, or None.
    """
    # MediaWiki escapes `&`, `<`, `>` inside alttext="…" but leaves `'` literal,
    # so try both quote-modes of html.escape.
    candidates = [
        alttext_value,
        html.escape(alttext_value, quote=False),
        html.escape(alttext_value, quote=True),
    ]
    needle_pos = -1
    for cand in candidates:
        needle = f'alttext="{cand}"'
        needle_pos = src.find(needle)
        if needle_pos != -1:
            break
    if needle_pos == -1:
        return None

    # The math may be either display-block or inline. Walk back to whichever
    # `mwe-math-element` (-block or -inline) wrapper is the IMMEDIATE
    # enclosure of this <math>, not some unrelated earlier one. Matching the
    # common class prefix `mwe-math-element` covers both shapes.
    open_pos = src.rfind('<span class="mwe-math-element', 0, needle_pos)
    if open_pos == -1:
        return None

    math_end = src.find("</math>", needle_pos)
    if math_end == -1:
        return None
    math_end += len("</math>")

    # After </math> the structure is: </span> [<img …>] </span>
    span1 = src.find("</span>", math_end)
    if span1 == -1:
        return None
    span2 = src.find("</span>", span1 + len("</span>"))
    if span2 == -1:
        return None
    return (open_pos, span2 + len("</span>"))


def _build_plain_text_map(src: str, start: int, end: int, *, lowercase: bool = True):
    """Build a character-level plain-text projection of src[start:end].

    Returns (text, starts, ends) where text is the whitespace-collapsed,
    tag-stripped, entity-decoded content and starts[i]/ends[i] are the HTML
    source offsets for text[i]."""
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    in_tag = False
    last_was_space = True

    def push(ch: str, s: int, e: int):
        nonlocal last_was_space
        if ch.isspace() or ord(ch) in (0x00A0, 0x2009, 0x200B):
            if last_was_space:
                return
            chars.append(" ")
            starts.append(s)
            ends.append(e)
            last_was_space = True
        else:
            chars.append(ch.lower() if lowercase else ch)
            starts.append(s)
            ends.append(e)
            last_was_space = False

    i = start
    while i < end:
        c = src[i]
        if c == "<":
            if src[i:i + 5].lower() == "<math" and (
                i + 5 < len(src) and (src[i + 5].isspace() or src[i + 5] == ">")
            ):
                close = src.find("</math>", i, end)
                if close == -1:
                    break
                i = close + len("</math>")
                continue
            in_tag = True
            i += 1
            continue
        if c == ">":
            in_tag = False
            i += 1
            continue
        if in_tag:
            i += 1
            continue
        if c == "&":
            semi = src.find(";", i, min(i + 16, end))
            if semi != -1:
                entity = src[i:semi + 1]
                import html as htmlmod
                decoded = htmlmod.unescape(entity)
                if decoded != entity:
                    for d in decoded:
                        push(d, i, semi + 1)
                    i = semi + 1
                    continue
        push(c, i, i + 1)
        i += 1

    return "".join(chars), starts, ends


def _find_plain_text_range(src: str, start: int, end: int, snippet: str) -> tuple[int, int] | None:
    """Return the (html_start, html_end) where `snippet` first appears as
    plain text in [start, end).  Case-insensitive, whitespace-collapsed."""
    snippet_norm = re.sub(r"\s+", " ", snippet).strip().lower()
    if not snippet_norm:
        return None
    plain, p_starts, p_ends = _build_plain_text_map(src, start, end)
    idx = plain.find(snippet_norm)
    if idx == -1:
        return None
    return (p_starts[idx], p_ends[idx + len(snippet_norm) - 1])


def _find_sentence_range(src: str, block_start: int, block_end: int,
                         snippet: str) -> tuple[int, int] | None:
    """Find `snippet` in the block's plain text, then expand outward to
    sentence boundaries (`. ` / `! ` / `? ` followed by uppercase, or
    start/end of block).

    Returns (html_start, html_end) covering the full sentence."""
    snippet_norm = re.sub(r"\s+", " ", snippet).strip().lower()
    if not snippet_norm:
        return None
    plain, p_starts, p_ends = _build_plain_text_map(
        src, block_start, block_end, lowercase=False,
    )
    idx = plain.lower().find(snippet_norm)
    if idx == -1:
        return None

    # Expand backward: find `. X` (period-space-uppercase) before snippet.
    sent_start = 0
    for i in range(idx - 1, 0, -1):
        if plain[i] == " " and i > 0 and plain[i - 1] in ".!?" \
                and i + 1 < len(plain) and plain[i + 1:i + 2].isupper():
            sent_start = i + 1
            break

    # Expand forward: find `.` followed by space+uppercase or end of text.
    snippet_end = idx + len(snippet_norm)
    sent_end = len(plain)
    for i in range(snippet_end, len(plain)):
        if plain[i] in ".!?":
            rest = plain[i + 1:]
            if not rest or (len(rest) >= 1 and rest[0] == " " and
                            (len(rest) < 2 or rest[1].isupper())):
                sent_end = i + 1
                break

    sent_end = min(sent_end, len(p_ends))
    return (p_starts[sent_start], p_ends[sent_end - 1])


def _matched_ends_with_colon(src: str, html_start: int, html_end: int) -> bool:
    """Does the plain text of src[html_start:html_end] end with a colon?"""
    plain, _, _ = _build_plain_text_map(src, html_start, html_end, lowercase=False)
    return plain.rstrip().endswith(":")


def _find_following_list(src: str, after_pos: int, limit: int) -> int | None:
    """If a <ul>, <ol>, or <dl> immediately follows after_pos (skipping
    whitespace and comments only), return the offset past its closing tag.
    Else return None."""
    p = after_pos
    while p < limit and src[p].isspace():
        p += 1
    if p >= limit:
        return None
    m = re.match(r"<(ul|ol|dl)\b", src[p:p + 6], re.IGNORECASE)
    if not m:
        return None
    tag = m.group(1).lower()
    close_tag = f"</{tag}>"
    close = src.find(close_tag, p, limit)
    if close == -1:
        return None
    return close + len(close_tag)


_DISPLAY_MATH_OPEN_RE = re.compile(
    r'<span class="mwe-math-element mwe-math-element-block">',
    re.IGNORECASE,
)
_DL_OPEN_RE = re.compile(r'<dl\b[^>]*>', re.IGNORECASE)


def _find_following_display_math(src: str, after_pos: int, limit: int) -> int | None:
    """If one or more display-math blocks immediately follow `after_pos`
    (whitespace skipped, no other prose intervening), return the offset past
    the LAST one. Else return None.

    Recognizes two shapes:
      - `<span class="mwe-math-element mwe-math-element-block">…</span>`
      - `<dl>…</dl>` whose content contains `mwe-math-element-block`
        (MediaWiki's `:`-indented equation form).

    The natural pattern this captures is "<prose definition>… <equation>",
    i.e. a paragraph whose meaning includes the equation that follows."""
    p = after_pos
    end_pos: int | None = None
    while p < limit:
        while p < limit and src[p].isspace():
            p += 1
        if p >= limit:
            break
        m = _DISPLAY_MATH_OPEN_RE.match(src, p)
        if m:
            close = src.find("</span>", m.end())
            if close == -1 or close >= limit:
                break
            end_pos = close + len("</span>")
            p = end_pos
            continue
        m = _DL_OPEN_RE.match(src, p)
        if m:
            close = src.find("</dl>", m.end())
            if close == -1 or close >= limit:
                break
            if "mwe-math-element-block" not in src[m.end():close]:
                break  # non-math <dl>, leave the wrap alone
            end_pos = close + len("</dl>")
            p = end_pos
            continue
        break
    return end_pos


_BLOCK_OPEN_RE = re.compile(
    r'<(p|div|dl|blockquote|ul|ol|table|pre|figure|h[1-6])\b[^>]*>',
    re.IGNORECASE,
)


def _iter_top_level_blocks(src: str, section_pos: int, section_end: int):
    """Yield (start, end) offsets for each top-level block element in
    [section_pos, section_end). Handles nesting via depth tracking."""
    pos = section_pos
    while pos < section_end:
        m = _BLOCK_OPEN_RE.search(src, pos, section_end)
        if not m:
            return
        tag = m.group(1).lower()
        open_re = re.compile(rf"<{tag}\b[^>]*>", re.IGNORECASE)
        close_tag = f"</{tag}>"
        depth = 1
        cursor = m.end()
        while depth > 0 and cursor < section_end:
            nx_close = src.find(close_tag, cursor, section_end)
            if nx_close == -1:
                break
            nx_open_m = open_re.search(src, cursor, nx_close)
            if nx_open_m is not None:
                depth += 1
                cursor = nx_open_m.end()
            else:
                depth -= 1
                cursor = nx_close + len(close_tag)
        if depth == 0:
            yield (m.start(), cursor)
            pos = cursor
        else:
            return


def find_prose_range(src: str, section: str, from_snippet: str,
                     to_math_alttext: str | None = None,
                     to_snippet: str | None = None) -> tuple[int, int, str] | None:
    """Wrap a character-level range starting at the plain-text occurrence of
    `from_snippet` and ending at:
      - the END of the math element with alttext = `to_math_alttext`, OR
      - the END of `to_snippet`'s first occurrence after `from_snippet`.

    Used for spans that cross multiple inline elements / display equations
    within one HTML <p>.
    """
    # 1. Locate the section bounds.
    bounds = _find_section_bounds(src, section)
    if bounds is None:
        return None
    section_pos, section_end = bounds

    # 2. Find from_snippet's HTML start offset.
    rng = _find_plain_text_range(src, section_pos, section_end, from_snippet)
    if rng is None:
        stripped = _strip_wikitext_markup(from_snippet)
        if stripped and stripped != from_snippet:
            rng = _find_plain_text_range(src, section_pos, section_end, stripped)
        if rng is None:
            return None
    from_pos = rng[0]

    # 3. Find end of range.
    end_pos: int | None = None
    if to_math_alttext:
        m = find_math_span(src, to_math_alttext)
        if m is not None and m[0] >= from_pos:
            end_pos = m[1]
    if end_pos is None and to_snippet:
        to_rng = _find_plain_text_range(src, from_pos, section_end, to_snippet)
        if to_rng is not None:
            end_pos = to_rng[1]
    if end_pos is None:
        return None

    # 4. If the range crosses block boundaries (e.g. from a <p> through a
    #    <dl>/<dd> containing a displayed equation), wrapping with a <span>
    #    would be invalid HTML — the parser would auto-close the span at
    #    </p>, severing the wrap. Snap the range to its enclosing top-level
    #    blocks and use a <div> wrapper. Same-block ranges stay tight.
    blocks = list(_iter_top_level_blocks(src, section_pos, section_end))
    first = next((b for b in blocks if b[0] <= from_pos < b[1]), None)
    last = next((b for b in reversed(blocks) if b[0] < end_pos <= b[1]), None)
    if first is not None and last is not None and first != last:
        return (first[0], last[1], "div")
    return (from_pos, end_pos, "span")


_STATUS_PRIORITY = {"formalized": 0, "partial": 1, "not_formalized": 2}


def _apply_nested_edits(src: str, edits: list[tuple[int, int, str, str]]) -> str:
    """Apply edits that may be nested (one fully inside another) but not
    partially overlapping. Each edit is (start, end, open_tag, close_tag).

    We build a forest of edits ordered by containment and render the source
    with each edit's open/close inserted around its content (which may itself
    contain nested wraps). This avoids the offset-drift bug that arises when
    inserting bytes into a string that other edits reference by body_html
    offset.
    """
    if not edits:
        return src
    # Sort by start ascending, then by end descending — so when two edits
    # share a start, the OUTER (larger end) comes first.
    sorted_edits = sorted(edits, key=lambda e: (e[0], -e[1]))

    root = {"start": 0, "end": len(src), "open": "", "close": "", "children": []}
    stack = [root]
    for start, end, open_tag, close_tag in sorted_edits:
        # Pop nodes whose range we've already passed.
        while stack[-1] is not root and stack[-1]["end"] <= start:
            stack.pop()
        parent = stack[-1]
        if end > parent["end"]:
            # Partial overlap — would produce invalid nesting. Drop with a warning.
            print(f"warning: dropping edit ({start}, {end}) — partially overlaps "
                  f"({parent['start']}, {parent['end']})")
            continue
        node = {"start": start, "end": end, "open": open_tag, "close": close_tag, "children": []}
        parent["children"].append(node)
        stack.append(node)

    def render(node):
        parts = [node["open"]]
        cursor = node["start"]
        for child in node["children"]:
            parts.append(src[cursor:child["start"]])
            parts.append(render(child))
            cursor = child["end"]
        parts.append(src[cursor:node["end"]])
        parts.append(node["close"])
        return "".join(parts)

    return render(root)


def wrap_annotations(body_html: str, annotations: list[dict]) -> tuple[str, list[bool]]:
    """Wrap each annotation's target span in a WikiLean wrapper.

    Multiple annotations can resolve to the same anchor range (e.g. several
    defs/props within a single paragraph). We group them by (start, end) and
    emit ONE wrapper per range carrying a comma-separated list of indices;
    the client-side tooltip then stacks all of them.

    Returns (new_html, matched_flags) where matched_flags[i] is True iff
    annotation i was located in the HTML.
    """
    matched = [False] * len(annotations)

    # group_key → list of (annotation_index, wrapper_tag)
    groups: dict[tuple[int, int], list[tuple[int, str]]] = {}

    for i, a in enumerate(annotations):
        # Human-deletion tombstone: status="rejected" is a human veto — never
        # wrapped. matched[i] is reported True (meaning "excluded, not an
        # anchor failure") so vetoes don't show up as anchor rot in the
        # matched-counters / unmatched warnings. Mirrors wrap.ts
        # wrapAnnotations (golden parity).
        if a.get("status") == "rejected":
            matched[i] = True
            continue
        # Support both `anchor` (single) and `anchors` (list) — a single
        # annotation can have multiple anchor targets that all share its
        # label, decl, note, etc.
        anchors = a.get("anchors") or [a.get("anchor", {})]
        for anchor in anchors:
            t = anchor.get("type")
            if t == "math_alttext":
                loc = find_math_span(body_html, anchor["value"])
                if loc is None:
                    continue
                start, end, wrapper = *loc, "span"
            elif t == "theorem_box":
                loc = find_theorem_box(body_html, anchor["value"])
                if loc is None:
                    continue
                start, end, wrapper = *loc, "div"
            elif t == "prose_range":
                loc = find_prose_range(
                    body_html,
                    anchor["section"],
                    anchor.get("from") or anchor.get("from_snippet"),
                    to_math_alttext=anchor.get("to_math") or anchor.get("to_alttext"),
                    to_snippet=anchor.get("to") or anchor.get("to_snippet"),
                )
                if loc is None:
                    continue
                start, end, wrapper = loc
            elif "section" in anchor and "snippet" in anchor:
                loc = find_prose_block(body_html, anchor["section"], anchor["snippet"])
                if loc is None:
                    continue
                start, end, wrapper = loc
            else:
                continue
            matched[i] = True
            groups.setdefault((start, end), []).append((i, wrapper))

    edits: list[tuple[int, int, str, str]] = []
    for (start, end), members in groups.items():
        indices = [i for i, _ in members]
        wrapper = "div" if any(w == "div" for _, w in members) else "span"
        statuses = [annotations[i]["status"] for i in indices]
        rep = sorted(statuses, key=lambda s: _STATUS_PRIORITY.get(s, 99))[0]
        decl = None
        for i in indices:
            a = annotations[i]
            d = (a.get("mathlib") or {}).get("decl") or a.get("decl")
            if d:
                decl = d
                break
        # Representative provenance: any human-authored annotation in the group
        # promotes the wrap to "human-curated" (a person has signed off here).
        # Treat anything other than the explicit string "human" as AI / default.
        provs = [annotations[i].get("provenance") for i in indices]
        rep_prov = "human" if "human" in provs else "ai"
        attrs = [
            f'class="anno anno-{rep}"',
            f'data-status="{rep}"',
            f'data-provenance="{rep_prov}"',
            f'data-anno-indices="{",".join(str(i) for i in indices)}"',
        ]
        if decl:
            attrs.append(f'data-decl="{html.escape(decl, quote=True)}"')
        open_tag = f"<{wrapper} {' '.join(attrs)}>"
        close_tag = f"</{wrapper}>"
        edits.append((start, end, open_tag, close_tag))

    out = _apply_nested_edits(body_html, edits)
    return out, matched


# Embed JSON inside <script> safely: any "</" sequence in JSON could end the
# script tag in a buggy parser. The standard fix is to escape '<' → '<'.
def _safe_json_for_script(obj) -> str:
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WikiLean · {title}</title>
<meta name="description" content="{desc}">
<link rel="canonical" href="{canonical}">
<meta property="og:type" content="article">
<meta property="og:site_name" content="WikiLean">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<meta property="og:url" content="{canonical}">
<meta name="twitter:card" content="summary">
<style>
{css}
</style>
</head>
<body class="show-all">
<header class="wl-header">
  <div class="wl-title">
    <a class="wl-brand" href="/">WikiLean</a>
    <span class="wl-sep">·</span>
    <span class="wl-article">{title}</span>
    <span class="wl-nav">
      <a class="wl-navlink" href="/about">About</a>
      <a class="wl-wikilink" href="https://en.wikipedia.org/wiki/{wp_link}" target="_blank" rel="noopener">view on Wikipedia ↗</a>
    </span>
  </div>
  <div class="wl-controls">
    <span class="wl-coverage">
      <span class="wl-badge wl-formalized">{n_formalized} formalized</span>
      <span class="wl-badge wl-partial">{n_partial} partial</span>
      <span class="wl-badge wl-not_formalized">{n_not} not formalized</span>
      <span class="wl-badge wl-untouched">{n_untouched} unannotated math</span>
    </span>
    <span class="wl-toggles">
      <button data-mode="all" class="active">All</button>
      <button data-mode="formalized">Formalized only</button>
      <button data-mode="not_formalized">Not-formalized only</button>
      <button data-mode="dim">Dim unannotated</button>
    </span>
  </div>
</header>
<main class="wl-article-body">
{body}
</main>
<div id="wl-tooltip" hidden></div>
<script>
window.__WL_ANNOTATIONS__ = {data};
</script>
<script>
{js}
</script>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("slug", help="article slug (matches annotations/<slug>.json)")
    args = ap.parse_args()
    slug = args.slug

    annot_path = ANNOT / f"{slug}.json"
    if not annot_path.exists():
        print(f"missing: {annot_path}", file=sys.stderr)
        return 1
    annot = json.loads(annot_path.read_text(encoding="utf-8"))

    src = fetch_article_html(slug, annot["wikipedia_title"])
    src = absolutize_wikipedia_urls(src)
    wrapped, matched_flags = wrap_annotations(src, annot["annotations"])

    n_total_displays = src.count('<span class="mwe-math-element mwe-math-element-block">')
    n_annotated = sum(matched_flags)
    # Only math_alttext annotations consume display-math elements; theorem_box
    # (and any future block-level anchor types) target their own elements.
    # Tombstones (status="rejected") report matched=True from the wrap engine
    # but emit no wrap, so any math element they once covered counts as
    # unannotated again (mirrors page.ts).
    n_display_annotated = sum(
        1
        for a, ok in zip(annot["annotations"], matched_flags)
        if ok
        and a.get("status") != "rejected"
        and a.get("anchor", {}).get("type") == "math_alttext"
    )
    n_untouched = max(0, n_total_displays - n_display_annotated)
    n_unmatched = len(matched_flags) - n_annotated

    # "rejected" is deliberately absent from `counts`, so tombstones never
    # reach the header badges or the meta description (mirrors page.ts).
    counts = {"formalized": 0, "partial": 0, "not_formalized": 0}
    for a in annot["annotations"]:
        if a["status"] in counts:
            counts[a["status"]] += 1

    if n_unmatched:
        print(f"warning: {n_unmatched} annotation(s) had no matching anchor in HTML:")
        for i, m in enumerate(matched_flags):
            if not m:
                anchor = annot["annotations"][i].get("anchor", {})
                if "snippet" in anchor:
                    label = f"section={anchor.get('section')!r} snippet={anchor.get('snippet')!r}"
                else:
                    label = anchor.get("value", "?")
                print(f"  [{i}] {label[:120]}{'…' if len(label) > 120 else ''}")

    # Build the lean per-annotation payload the client needs.
    # Handles both v2 schema (status/decl/module/note flat) and v3 schema
    # (status/label/kind/mathlib.decl/mathlib.module/match_kind/proof_note).
    client_data = []
    for a in annot["annotations"]:
        # Human-deletion tombstones must not ship to readers; a None
        # placeholder (not a filter) keeps the array index-aligned with
        # data-anno-indices in the wrapped HTML. Tombstones are never
        # wrapped, so no index references the null (mirrors page.ts
        # buildClientData).
        if a.get("status") == "rejected":
            client_data.append(None)
            continue
        m = a.get("mathlib") or {}
        decl = m.get("decl") or a.get("decl")
        module = m.get("module") or a.get("module")
        item = {"status": a["status"]}
        for k in ("label", "kind", "note", "proof_note", "provenance"):
            if a.get(k):
                item[k] = a[k]
        if m.get("match_kind"):
            item["match_kind"] = m["match_kind"]
        if decl:
            item["decl"] = decl
        if module:
            item["module"] = module
        url = mathlib_docs_url(module, decl)
        if url:
            item["mathlib_url"] = url
        client_data.append(item)

    css = (ASSETS / "style.css").read_text(encoding="utf-8")
    js = (ASSETS / "script.js").read_text(encoding="utf-8")

    wp_title = annot["wikipedia_title"].replace(" ", "_")
    n_total = counts.get("formalized", 0) + counts.get("partial", 0) + counts.get("not_formalized", 0)
    display_title = annot["display_title"]
    if n_total:
        desc = (f"{display_title} from Wikipedia, annotated with links into Mathlib4: "
                f"{counts.get('formalized', 0)} of {n_total} definitions, theorems, and "
                f"proofs formalized in Lean ({counts.get('partial', 0)} partial).")
    else:
        desc = f"{display_title} from Wikipedia, annotated with links into Mathlib4 / Lean."
    canonical = f"{BASE_URL}/{urllib.parse.quote(slug)}"
    page = PAGE_TEMPLATE.format(
        title=html.escape(display_title),
        desc=html.escape(desc, quote=True),
        canonical=html.escape(canonical, quote=True),
        wp_link=urllib.parse.quote(wp_title, safe=""),
        css=css,
        js=js,
        body=wrapped,
        n_formalized=counts.get("formalized", 0),
        n_partial=counts.get("partial", 0),
        n_not=counts.get("not_formalized", 0),
        n_untouched=n_untouched,
        data=_safe_json_for_script(client_data),
    )

    OUT.mkdir(exist_ok=True)
    out_path = OUT / f"{slug}.html"
    out_path.write_text(page, encoding="utf-8")
    print(
        f"wrote {out_path}  "
        f"(annotations: {n_annotated}/{len(annot['annotations'])} matched; "
        f"{n_untouched} display-math elements left unannotated)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
