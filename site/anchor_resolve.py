"""Resolve WikiLean's editor anchors into Literature Links' built-in HTML locations.

The editor keeps anchoring by section heading plus verbatim snippet, by a
formula's alt text, or by a theorem box. What gets stored and exported is the
same selection as a reader of the pinned article finds it: an exact quotation of
the page's text layer, or a CSS selector on a formula's alt text. The profile
form stays only for what this module cannot resolve.

The text layer is the one Literature Links defines for HTML: the textContent of
the body without script, style and template elements, each run of White_Space
collapsed to one space, trimmed. MathML text counts, hidden or not, and no space
is inserted at element boundaries.
"""
from __future__ import annotations

import bisect
import html as htmlmod
import re

WHITE_SPACE = frozenset(chr(code) for code in (
    *range(0x09, 0x0E), 0x20, 0x85, 0xA0, 0x1680, *range(0x2000, 0x200B), 0x2028, 0x2029, 0x202F, 0x205F, 0x3000))
_WS_RE = re.compile("[" + "".join(re.escape(ch) for ch in sorted(WHITE_SPACE)) + "]+")
_SKIP_ELEMENTS = frozenset({"script", "style", "template"})
_TAG_RE = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9:-]*)")
_MATH_SELECTOR_RE = re.compile(r'^(img\[alt|math\[alttext)="((?:[^"\\]|\\.)*)"\]$')
_SHORT_QUOTE = 20
_CONTEXT_WIDTHS = (24, 48, 96, 192)


def collapse(text: str) -> str:
    """Literature Links' whitespace rule: runs of White_Space become one space; ends are trimmed."""
    return " ".join(part for part in _WS_RE.split(text) if part)


def text_layer(src: str) -> tuple[str, list[int], list[int]]:
    """Return (text, starts, ends): the text layer of an HTML fragment and, for
    each character, the HTML offsets it came from."""
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    pending: tuple[int, int] | None = None
    lower = src.lower()
    n = len(src)
    i = 0

    def push(char: str, start: int, end: int) -> None:
        nonlocal pending
        if char in WHITE_SPACE:
            if pending is None:
                pending = (start, end)
            return
        if pending is not None and chars:
            chars.append(" ")
            starts.append(pending[0])
            ends.append(pending[1])
        pending = None
        chars.append(char)
        starts.append(start)
        ends.append(end)

    while i < n:
        char = src[i]
        if char == "<":
            if src.startswith("<!--", i):
                close = src.find("-->", i + 4)
                i = n if close == -1 else close + 3
                continue
            tag = _TAG_RE.match(src, i)
            if tag:
                end = src.find(">", tag.end())
                end = n if end == -1 else end + 1
                name = tag.group(2).lower()
                if not tag.group(1) and name in _SKIP_ELEMENTS:
                    close = lower.find("</" + name, end)
                    if close == -1:
                        i = n
                    else:
                        gt = src.find(">", close)
                        i = n if gt == -1 else gt + 1
                    continue
                i = end
                continue
        if char == "&":
            semi = src.find(";", i, min(i + 16, n))
            if semi != -1:
                entity = src[i:semi + 1]
                decoded = htmlmod.unescape(entity)
                if decoded != entity:
                    for piece in decoded:
                        push(piece, i, semi + 1)
                    i = semi + 1
                    continue
        push(char, i, i + 1)
        i += 1
    return "".join(chars), starts, ends


def css_string(value: str) -> str:
    """Escape a value for use inside a double-quoted CSS string."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\a ").replace("\r", "")


def css_unstring(value: str) -> str:
    # One pass, so an escaped backslash before "a " stays a backslash.
    return re.sub(r"\\(a |.)", lambda m: "\n" if m.group(1) == "a " else m.group(1), value, flags=re.S)


def parse_math_selector(selector: str) -> str | None:
    """The alt text named by a selector this module writes, or None."""
    match = _MATH_SELECTOR_RE.match(selector)
    return css_unstring(match.group(2)) if match else None


def _find_all(text: str, needle: str) -> list[int]:
    hits: list[int] = []
    position = text.find(needle)
    while position != -1:
        hits.append(position)
        position = text.find(needle, position + 1)
    return hits


def _lowered(text: str) -> str:
    # Per-character lowering keeps indices aligned; a character whose lower
    # form has another length stays as it is.
    return "".join(ch.lower() if len(ch.lower()) == 1 else ch for ch in text)


def _slice_for_range(text: str, starts: list[int], ends: list[int], start: int, end: int) -> str:
    first = bisect.bisect_left(starts, start)
    last = bisect.bisect_left(starts, end)
    return text[first:last].strip(" ")


def _with_context(text: str, position: int, exact: str) -> dict | None:
    """A quote object whose prefix and suffix make the match unique, or None."""
    after = position + len(exact)
    for width in _CONTEXT_WIDTHS:
        prefix = text[max(0, position - width):position]
        if position - width > 0 and " " in prefix:
            prefix = prefix[prefix.index(" "):]  # start at a word boundary
        suffix = text[after:after + width]
        if after + width < len(text) and " " in suffix:
            suffix = suffix[:suffix.rindex(" ") + 1]  # end at a word boundary
        if text.count(prefix + exact + suffix) == 1:
            quote = {"exact": exact}
            if prefix:
                quote["prefix"] = prefix
            if suffix:
                quote["suffix"] = suffix
            return quote
    return None


def resolve_snippet(src: str, section: str | None, snippet: str, layer=None) -> tuple[dict | None, str]:
    """Resolve a section+snippet anchor to an HTML quote location.

    As the renderer, a named section bounds the search: the words elsewhere on
    the page never stand in for the passage the anchor names.
    """
    text, starts, ends = layer or text_layer(src)
    key = collapse(snippet)
    if not key:
        return None, "empty"
    bounds = None
    if section is not None:
        import render
        bounds = render._find_section_bounds(src, section)

    def in_section(hit: int) -> bool:
        return bounds is None or bounds[0] <= starts[hit] < bounds[1]

    hits = [hit for hit in _find_all(text, key) if in_section(hit)]
    if not hits:
        lowered = _lowered(text)
        hits = [hit for hit in _find_all(lowered, _lowered(key)) if in_section(hit)]
    if not hits and section is not None:
        # Wikitext-derived or formula-spanning snippets: take the element the
        # renderer finds and quote its text layer instead.
        import render
        located = render.find_prose_block(src, section, snippet)
        if located:
            key = _slice_for_range(text, starts, ends, located[0], located[1])
            hits = [hit for hit in (_find_all(text, key) if key else []) if in_section(hit)]
    if not hits:
        return None, "no_match"
    chosen = hits[0]
    exact = text[chosen:chosen + len(key)]
    if len(_find_all(text, exact)) == 1 and len(exact) >= _SHORT_QUOTE:
        return {"quote": {"exact": exact}}, "quote"
    quote = _with_context(text, chosen, exact)
    if quote is None:
        return None, "ambiguous"
    return {"quote": quote}, "quote_context"


def resolve_math(src: str, value: str) -> tuple[dict | None, str]:
    """Resolve a formula's alt text to a CSS attribute selector when it is unique.

    The selector names the decoded attribute value, which is what CSS compares,
    not its spelling in the HTML source (legacy values may hold `&amp;`)."""
    candidates = {value, htmlmod.escape(value, quote=False), htmlmod.escape(value, quote=True)}

    def matches(attribute: str) -> tuple[int, str | None]:
        counts = {candidate: src.count(f'{attribute}="{candidate}"') for candidate in candidates}
        found = next((candidate for candidate, count in counts.items() if count), None)
        return sum(counts.values()), None if found is None else htmlmod.unescape(found)

    images, image_alt = matches("alt")
    if images == 1:
        return {"css": f'img[alt="{css_string(image_alt)}"]'}, "css"
    mathml, math_alt = matches("alttext")
    if images == 0 and mathml == 1:
        return {"css": f'math[alttext="{css_string(math_alt)}"]'}, "css"
    return None, "ambiguous" if images or mathml else "no_match"


def resolve_theorem_box(src: str, value: str, layer=None) -> tuple[dict | None, str]:
    import render
    located = render.find_theorem_box(src, value)
    if located is None:
        return None, "no_match"
    text, starts, ends = layer or text_layer(src)
    exact = _slice_for_range(text, starts, ends, located[0], located[1])
    if not exact:
        return None, "no_match"
    if text.count(exact) == 1:
        return {"quote": {"exact": exact}}, "quote"
    quote = _with_context(text, text.find(exact), exact)
    return ({"quote": quote}, "quote_context") if quote else (None, "ambiguous")


def resolve(anchor: dict, src: str, layer=None) -> tuple[dict | None, str]:
    """Resolve one WikiLean anchor against the pinned article HTML.

    Returns (location, how). ``location`` is a built-in Literature Links HTML
    location or None; ``how`` is quote, quote_context, css, unsupported,
    ambiguous, no_match or empty.
    """
    kind = anchor.get("type")
    if kind == "math_alttext":
        return resolve_math(src, anchor.get("value") or "")
    if kind == "theorem_box":
        return resolve_theorem_box(src, anchor.get("value") or "", layer)
    if kind == "prose_range":
        return None, "unsupported"
    if kind in (None, "") and isinstance(anchor.get("snippet"), str):
        return resolve_snippet(src, anchor.get("section"), anchor["snippet"], layer)
    return None, "unsupported"
