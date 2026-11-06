#!/usr/bin/env python3
"""Stage 9a: the ``web-html/3`` renderer — one body into the views every stage reads.

docs/PIPELINE.md section 3 (stage 9a). Ported from formal-companion's
``scripts/sources/render_html.py``, with the one change the ruling asks for:

**The text layer is not computed here.** It is ``site/anchor_resolve.text_layer``'s —
the Literature Links layer WikiLean already implements for the editor's anchors — and
this module is a *use* of it. :class:`_Layer` takes the tree's text and that function's
layer and aligns them: a layer character that is not ``" "`` is a non-``White_Space``
text character, in order, and every ``" "`` in the layer is one anchor_resolve put
there for a collapsed run. So the offset map is derived from the layer rather than from
a second copy of the collapse rule, and a disagreement between the two readers of the
body is a refusal (:class:`RenderError`) rather than a quietly different layer. The
gate's own text rule (``gate/locate_text.normalize_text``) goes through the same module,
so this repository has exactly one definition of the layer's whitespace.

**The renderer id is frozen as** ``web-html/3`` (:data:`vocab.RENDERER_WEB_HTML`): a
source's ``layer_sha256`` is pinned by every manifest entry, plan, gate configuration
and sealed record, so any change to the rule below that can move one character of any
view — the layout of a view's file included, and the paragraph-label rule the finders
read (:func:`vocab.is_bold_label` and its two label sets) — is a new renderer id, never
an edit of this one. The rule, in full:

* **The canonical body** (:func:`canonical_body`): the bytes are decoded as UTF-8 with
  one leading byte-order mark dropped; a ``formatversion=2`` ``action=parse`` JSON
  response gives its ``parse.text`` (``parse.text["*"]`` of a ``formatversion=1`` one),
  and anything that is no such response is the body itself. Every HTML comment is then
  removed exactly as the HTML tokenizer delimits one in the data state
  (:func:`strip_comments`): from ``<!--`` to the first ``-->`` or ``--!>``, an abrupt
  ``<!-->`` or ``<!--->`` being a whole (empty) comment and an unterminated one running
  to the end, while a ``<!--`` inside a tag or inside the raw text of
  ``script style xmp iframe noembed noframes textarea title`` is text and stays. A body
  holding a lone surrogate is refused. Its sha256 is ``html_sha256``.
* **The tree.** Tags are lower-cased by the parser; character references are decoded
  except inside the six raw-text elements, and ``textarea`` and ``title`` are escapable
  raw text. These sets are the renderer's own, never the interpreter's (CPython changed
  its tuples within 3.14), and an ``html.parser`` with no escapable raw-text mode is
  refused. The void elements never hold content. A self-closing flag is honoured on a
  MathML or SVG element and ignored on an HTML one, as the HTML5 tree builder does. An
  end tag closes the nearest open element of its name and everything opened inside it.
  A ``p`` is closed by the start of a block when no table cell, caption, button or
  object lies between; an ``li`` by the next ``li`` of its list, a ``dt``/``dd`` by the
  next of its ``dl``, a ``tr`` by the next ``tr``, a cell by the next cell or row, an
  ``option`` by the next ``option``. One newline right after a ``pre``, ``listing`` or
  ``textarea`` start tag is not text. A body nested more than :data:`MAX_DEPTH` deep is
  refused.
* **The layer** (``layer.txt``) is ``anchor_resolve.text_layer`` of the canonical body:
  the DOM ``textContent`` without ``script``, ``style`` and ``template``, each maximal
  run of ``White_Space`` made one U+0020 and the ends stripped. No space is added at
  element boundaries, and there is no other Unicode normalisation.
* **An element's layer span** is half-open, from its first non-whitespace text character
  to just after its last. Its **kept span** is the same over the text outside the dropped
  regions listed under the reading copy, so a citation marker at the start of a paragraph
  is never the start of a passage.
* **Formulas** (``formulas.json``): every element whose class tokens include
  ``mwe-math-element``, and every ``math`` outside one; its alttext is the first
  descendant ``math``'s ``alttext``, else the text of an ``annotation`` whose
  ``encoding`` is ``application/x-tex``, else the ``alt`` of an ``img`` of class
  ``mwe-math-fallback-image``. A formula with no alttext is ordinary text; one with no
  span contributes nothing to the layer and is written to the reading copy as added
  characters. Formulas never nest.
* **The head text** (``head.txt``) is ``gate.locate_text.HeadMap`` over the layer and the
  formulas: each formula span replaced by ``⟦normalised alttext⟧``.
* **Sections** (``sections.json``): every ``h1``–``h6`` starts one, at the layer position
  of its first text; the lead (path ``[]``) comes first and the spans tile the layer.
* **Blocks** (``paragraphs.json``): every ``p``, ``li``, ``dd``, ``dt`` and
  ``blockquote``, every theorem box and every display formula with a kept span outside
  the dropped regions. Blocks may nest. ``math`` is what gate rule 8's coverage floor
  reads.
* **The deterministic finders** (``finders.json``, :data:`vocab.DETERMINISTIC_FINDERS`):
  ``theorem_box``, ``bold_definition`` and ``lead``. A passage starts at its element's
  kept span start and must be a word start of the head text; one that is not is dropped
  and counted.
* **The reading copy** (``text.txt``) walks the same tree without the dropped regions —
  ``style``, ``script``, ``template``, ``sup.reference``, ``ol.references``, ``.reflist``,
  ``.mw-references-wrap``, ``.mw-editsection`` — writing each formula as
  ``$<normalised alttext>$``, each heading as ``#``-marks and its title, ``- `` before a
  list item, `` | `` between table cells, a line break for ``br`` and a blank line
  between blocks. ``layer_map.json`` maps it back, segment by segment.
* **The record** (``render.json``): the renderer, four shas, the counts, and ``views`` —
  the sha256 of every other file a rendering writes.

Offsets everywhere are code-point offsets. Nothing here fetches anything;
:mod:`wikinet` is the one module that may.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from bisect import bisect_right
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
for _candidate in (REPO / "site", HERE / "gate", HERE):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import anchor_resolve  # noqa: E402 — site/anchor_resolve.py, the one layer definition
import locate_text  # noqa: E402 — scripts/pipeline/gate/locate_text.py
import vocab  # noqa: E402

RENDERER = vocab.RENDERER_WEB_HTML
#: the lead finder skips a lead paragraph shorter than this (a stub's empty line)
LEAD_MIN_WORDS = 5
#: a bold run with fewer letters than this is a symbol (a bold R, a 1'), not a term
MIN_TERM_LETTERS = 2

#: the files a rendering writes into a source's directory, in the order written
RENDER_FILES = ("body.html", "layer.txt", "formulas.json", "head.txt", "text.txt",
                "layer_map.json", "sections.json", "paragraphs.json", "finders.json",
                "render.json")
#: the views whose shas ``render.json`` records under ``views`` (every other file: the
#: body's sha is ``html_sha256``, and the record cannot hold its own)
VIEW_FILES = tuple(name for name in RENDER_FILES if name not in ("body.html", "render.json"))

#: the HTML tokenizer's whitespace (where a tag ends, a tag name stops)
_HTML_WS = "\t\n\f\r "
#: the 25 code points with the Unicode ``White_Space`` property — anchor_resolve's set,
#: not a second copy: the layer this module uses is the one that collapses them
WHITE_SPACE = anchor_resolve.WHITE_SPACE
_WS = WHITE_SPACE
#: elements whose text is no part of the layer (the Literature Links rule)
_SKIPPED = frozenset({"script", "style", "template"})
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
                   "meta", "param", "source", "track", "wbr", "keygen"})
_CLOSES_P = frozenset({"address", "article", "aside", "blockquote", "center", "details",
                       "dir", "div", "dl", "fieldset", "figcaption", "figure", "footer",
                       "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "main",
                       "menu", "nav", "ol", "p", "pre", "section", "table", "ul"})
_P_SCOPE_BOUNDARY = frozenset({"table", "td", "th", "caption", "button", "object", "html",
                               "template", "marquee", "applet"})
_HEADINGS = {f"h{n}": n for n in range(1, 7)}
_COPY_BLOCKS = frozenset({"p", "div", "li", "dd", "dt", "dl", "ul", "ol", "table", "tr",
                          "tbody", "thead", "tfoot", "blockquote", "pre", "figure",
                          "figcaption", "caption", "section", "center", "hr", "h1", "h2",
                          "h3", "h4", "h5", "h6", "address", "article", "aside", "details",
                          "footer", "header", "main", "nav", "form", "fieldset"})
_BLOCK_TAGS = frozenset({"p", "li", "dd", "dt", "blockquote"})
_THEOREM_CLASSES = frozenset({"math_theorem", "math-theorem"})
#: elements whose content :func:`strip_comments` reads as raw text, so a ``<!--`` there
#: is text and stays. This is the canonical body's rule, and the canonical body is what
#: ``html_sha256`` is of, so the set is frozen with the renderer id.
_RAW_TEXT = frozenset({"script", "style", "xmp", "iframe", "noembed", "noframes",
                       "textarea", "title"})
#: elements whose content the **tree** reads as raw text. It is the set
#: ``site/anchor_resolve.text_layer`` skips, and no other: that function is the layer's
#: one author here, and it reads the content of a ``title``, a ``textarea`` or an
#: ``xmp`` as ordinary markup. A tree that read them as raw text would hold characters
#: the layer does not, which :class:`_Layer` refuses. Pinning the set also makes the
#: tree independent of the interpreter: CPython's own tuples changed within 3.14, which
#: added ``xmp iframe noembed noframes`` and an escapable mode for ``textarea`` and
#: ``title``.
_TREE_RAW_TEXT = frozenset({"script", "style"})
#: the HTML5 tree builder drops one newline right after these start tags
_EAT_LF = frozenset({"pre", "listing", "textarea"})
#: deeper nesting than this is refused, deterministically and well inside Python's
#: recursion limit (the walk takes two frames a level)
MAX_DEPTH = 256
#: the layer map's schema, and its segment kinds
LAYER_MAP_SCHEMA = "cf-layer-map/2"
SEGMENT_TEXT = 0
SEGMENT_FORMULA = 1
_TAG_NAME = re.compile(r"[^\t\n\f\r />]*")


class RenderError(ValueError):
    """A body or a response the renderer cannot turn into a source."""


# --------------------------------------------------------------------------
# the canonical body
# --------------------------------------------------------------------------

def _tag_end(html: str, i: int) -> int:
    """Just past the ``>`` closing the tag that starts at ``i`` (or the end of the text).

    The tokenizer's attribute states, reduced to what decides where a tag ends: a ``"``
    or ``'`` opens a quoted value only right after an ``=`` (and the whitespace after
    it); inside one, ``>`` is text.
    """
    n = len(html)
    j = i + 1
    expecting_value = False
    while j < n:
        ch = html[j]
        if ch == ">":
            return j + 1
        if ch == "=":
            expecting_value = True
            j += 1
            continue
        if ch in _HTML_WS:
            j += 1
            continue
        if expecting_value and ch in "\"'":
            close = html.find(ch, j + 1)
            if close < 0:
                return n
            j = close + 1
            expecting_value = False
            continue
        expecting_value = False
        j += 1
    return n


def strip_comments(html: str) -> str:
    """Every HTML comment removed, delimited as the tokenizer delimits it.

    Scans the data state: a ``<!--`` opens a comment closed by the first ``-->`` or
    ``--!>`` (``<!-->`` and ``<!--->`` close at once, and an unterminated one runs to the
    end); a tag is skipped whole, attribute values included; the content of a raw-text
    element runs to its end tag. So a ``<!--`` in an attribute or a stylesheet stays.
    """
    out: list[str] = []
    n = len(html)
    i = 0
    while i < n:
        j = html.find("<", i)
        if j < 0:
            out.append(html[i:])
            break
        out.append(html[i:j])
        if html.startswith("<!--", j):
            k = j + 4
            if html.startswith(">", k):
                i = k + 1
                continue
            if html.startswith("->", k):
                i = k + 2
                continue
            ends = [e + len(m) for m in ("-->", "--!>") for e in [html.find(m, k)] if e >= 0]
            i = min(ends) if ends else n
            continue
        nxt = html[j + 1:j + 2]
        if nxt.isascii() and nxt.isalpha():
            k = _tag_end(html, j)
            out.append(html[j:k])
            name = _TAG_NAME.match(html, j + 1).group(0).lower()
            if name in _RAW_TEXT and not html[j:k].endswith("/>"):
                close = re.compile(r"</" + re.escape(name) + r"(?=[\t\n\f\r />])", re.I)
                m = close.search(html, k)
                end = m.start() if m else n
                out.append(html[k:end])
                i = end
            else:
                i = k
            continue
        if nxt == "/" and html[j + 2:j + 3].isascii() and html[j + 2:j + 3].isalpha():
            k = _tag_end(html, j)
            out.append(html[j:k])
            i = k
            continue
        out.append("<")
        i = j + 1
    return "".join(out)


def decode(raw: bytes) -> str:
    """UTF-8, one leading byte-order mark dropped (the WHATWG UTF-8 decode); else refused."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RenderError(f"the bytes are not UTF-8 ({exc})") from None
    return text[1:] if text.startswith("﻿") else text


def response_json(raw: bytes):
    """The JSON value the bytes hold (byte-order mark tolerated), or None when they hold none."""
    try:
        return json.loads(decode(raw))
    except (RenderError, ValueError):
        return None


def parse_response_text(raw: bytes) -> tuple[str | None, dict]:
    """``(parse.text, parse)`` of an ``action=parse`` JSON response, or ``(None, {})``."""
    data = response_json(raw)
    if not isinstance(data, dict) or not isinstance(data.get("parse"), dict):
        return None, {}
    parse = data["parse"]
    text = parse.get("text")
    if isinstance(text, dict):
        text = text.get("*")
    return (text, parse) if isinstance(text, str) else (None, {})


def canonical_body(raw: bytes) -> str:
    """The canonical body of fetched or adopted bytes (see the module docstring)."""
    if isinstance(raw, str):
        raw = _encodable(raw).encode("utf-8")
    text, _parse = parse_response_text(raw)
    if text is None:
        try:
            text = decode(raw)
        except RenderError as exc:
            raise RenderError(f"the bytes are neither a parse response nor UTF-8 HTML "
                              f"({exc})") from None
    _encodable(text)
    return strip_comments(text)


def _encodable(text: str) -> str:
    """``text``, refused when it holds a lone surrogate (it would have no UTF-8 bytes)."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RenderError(f"the body holds a lone surrogate ({exc.reason} at {exc.start})")
    return text


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# the tree
# --------------------------------------------------------------------------

#: an element's namespace, as far as the tree needs it (where a self-closing flag counts)
_NS_HTML, _NS_MATHML, _NS_SVG = "html", "mathml", "svg"
#: MathML's text integration points: a start tag inside one is HTML, but these two
_MATHML_TEXT_POINTS = frozenset({"mi", "mo", "mn", "ms", "mtext"})
_MATHML_TEXT_POINT_FOREIGN = frozenset({"mglyph", "malignmark"})
#: SVG's HTML integration points (html.parser lower-cases ``foreignObject``)
_SVG_HTML_POINTS = frozenset({"foreignobject", "desc", "title"})
#: the HTML start tags that break out of foreign content (the HTML5 tree builder)
_BREAKOUT = frozenset({"b", "big", "blockquote", "body", "br", "center", "code", "dd", "div",
                       "dl", "dt", "em", "embed", "h1", "h2", "h3", "h4", "h5", "h6", "head",
                       "hr", "i", "img", "li", "listing", "menu", "meta", "nobr", "ol", "p",
                       "pre", "ruby", "s", "small", "span", "strong", "strike", "sub", "sup",
                       "table", "tt", "u", "ul", "var"})


def _namespace(tag: str, attrs: dict, parent: "_Node") -> str:
    """The namespace a start tag of ``tag`` gets under ``parent``."""
    pns = parent.ns
    if pns == _NS_MATHML:
        if parent.tag in _MATHML_TEXT_POINTS and tag not in _MATHML_TEXT_POINT_FOREIGN:
            pns = _NS_HTML
        elif parent.tag == "annotation-xml":
            encoding = parent.attrs.get("encoding", "").lower()
            if tag == "svg":
                return _NS_SVG
            if encoding in ("text/html", "application/xhtml+xml"):
                pns = _NS_HTML
    elif pns == _NS_SVG and parent.tag in _SVG_HTML_POINTS:
        pns = _NS_HTML
    if pns == _NS_HTML:
        return {"math": _NS_MATHML, "svg": _NS_SVG}.get(tag, _NS_HTML)
    if tag in _BREAKOUT or (tag == "font" and set(attrs) & {"color", "face", "size"}):
        return _NS_HTML
    return pns


class _Node:
    __slots__ = ("tag", "attrs", "classes", "children", "parent", "start", "end", "ns")

    def __init__(self, tag: str, attrs: dict, parent: "_Node | None", start: int,
                 ns: str = _NS_HTML):
        self.tag = tag
        self.attrs = attrs
        self.classes = frozenset((attrs.get("class") or "").split())
        self.children: list = []          # _Node or (start, end) text ranges
        self.parent = parent
        self.start = start
        self.end = start
        self.ns = ns

    def ancestors(self):
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def iter(self):
        """This node and every descendant element, in document order."""
        stack = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(c for c in reversed(node.children) if isinstance(c, _Node))


class _TreeBuilder(HTMLParser):
    """``html.parser`` events into a :class:`_Node` tree plus the document's text.

    The raw-text set is pinned to :data:`_TREE_RAW_TEXT` rather than inherited, for two
    reasons that are one reason: CPython's own tuples changed within 3.14 (3.14.0 reads
    only ``script`` and ``style``, 3.14.5 adds ``xmp iframe noembed noframes`` and an
    escapable mode for ``textarea`` and ``title``), and
    ``site/anchor_resolve.text_layer`` — this renderer's layer — reads all of those as
    ordinary markup. The tree must read the body as the layer's author reads it, on
    every interpreter.
    """

    #: raw text: no tag and no reference inside is read
    CDATA_CONTENT_ELEMENTS = tuple(sorted(_TREE_RAW_TEXT))
    #: escapable raw text: none, so ``textarea`` and ``title`` are ordinary elements
    RCDATA_CONTENT_ELEMENTS = ()

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pieces: list[str] = []
        self.length = 0
        self.root = _Node("#root", {}, None, 0)
        self.stack = [self.root]
        self.eat_lf = False
        #: how many ``script``/``style``/``template`` elements are open: their text is
        #: no part of the layer, so it never enters the document's text
        self.skipping = 0

    def set_cdata_mode(self, elem, **options):
        """Enter raw text only for an element of :data:`_TREE_RAW_TEXT`.

        ``**options`` takes the ``escapable`` keyword newer interpreters pass and older
        ones do not, so one renderer runs on both.
        """
        if elem.lower() in _TREE_RAW_TEXT:
            super().set_cdata_mode(elem, **options)

    # -- helpers -------------------------------------------------------
    def _close_to(self, index: int) -> None:
        """Pop the stack down to (and including) ``self.stack[index]``."""
        while len(self.stack) > index:
            node = self.stack.pop()
            node.end = self.length
            if node.tag in _SKIPPED:
                self.skipping -= 1

    def _find(self, names, boundary) -> int:
        """Index of the nearest open element named in ``names`` above ``boundary``."""
        for i in range(len(self.stack) - 1, 0, -1):
            tag = self.stack[i].tag
            if tag in names:
                return i
            if tag in boundary:
                return -1
        return -1

    def _implied_closes(self, tag: str) -> None:
        if tag in _CLOSES_P:
            i = self._find({"p"}, _P_SCOPE_BOUNDARY)
            if i > 0:
                self._close_to(i)
        if tag == "li":
            i = self._find({"li"}, {"ul", "ol", "menu", "table", "td", "th"})
            if i > 0:
                self._close_to(i)
        elif tag in ("dt", "dd"):
            i = self._find({"dt", "dd"}, {"dl", "table", "td", "th"})
            if i > 0:
                self._close_to(i)
        elif tag == "tr":
            i = self._find({"tr"}, {"table", "tbody", "thead", "tfoot"})
            if i > 0:
                self._close_to(i)
        elif tag in ("td", "th"):
            i = self._find({"td", "th"}, {"tr", "table"})
            if i > 0:
                self._close_to(i)
        elif tag in ("tbody", "thead", "tfoot"):
            i = self._find({"tr", "td", "th", "tbody", "thead", "tfoot"}, {"table"})
            if i > 0:
                self._close_to(i)
        elif tag == "option":
            i = self._find({"option"}, {"select", "datalist"})
            if i > 0:
                self._close_to(i)

    # -- events --------------------------------------------------------
    def handle_starttag(self, tag, attrs, *, self_closing: bool = False):
        self.eat_lf = False
        self._implied_closes(tag)
        parent = self.stack[-1]
        attrs = {k: (v if v is not None else "") for k, v in attrs}
        node = _Node(tag, attrs, parent, self.length, _namespace(tag, attrs, parent))
        parent.children.append(node)
        if tag in _VOID or (self_closing and node.ns != _NS_HTML):
            node.end = self.length
        else:
            if len(self.stack) > MAX_DEPTH:
                raise RenderError(f"the body nests elements more than {MAX_DEPTH} deep "
                                  f"(at a {tag}); repair it before rendering")
            self.stack.append(node)
            self.eat_lf = tag in _EAT_LF
            if tag in _SKIPPED:
                self.skipping += 1

    def handle_startendtag(self, tag, attrs):
        # HTML5 honours the self-closing flag in MathML and SVG, ignores it on HTML
        self.handle_starttag(tag, attrs, self_closing=True)

    def handle_endtag(self, tag):
        self.eat_lf = False
        if tag in _VOID:
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                self._close_to(i)
                return

    def handle_comment(self, data):
        self.eat_lf = False

    def handle_decl(self, decl):
        self.eat_lf = False

    def handle_pi(self, data):
        self.eat_lf = False

    def handle_data(self, data):
        if self.eat_lf:
            self.eat_lf = False
            for newline in ("\r\n", "\r", "\n"):
                if data.startswith(newline):
                    data = data[len(newline):]
                    break
        if not data or self.skipping:
            return
        start = self.length
        self.pieces.append(data)
        self.length += len(data)
        self.stack[-1].children.append((start, self.length))

    def finish(self) -> tuple[_Node, str]:
        self.close()
        self._close_to(1)
        self.root.end = self.length
        return self.root, "".join(self.pieces)


# --------------------------------------------------------------------------
# the layer, from site/anchor_resolve.text_layer
# --------------------------------------------------------------------------

class _Layer:
    """``anchor_resolve``'s layer, with the tree's text aligned onto it.

    The alignment is the whole of the relation between the two readers of one body: a
    layer character that is not ``" "`` is a non-``White_Space`` text character, in
    document order, and every ``" "`` in the layer stands for a collapsed run. Walking
    both in step gives ``emit[i]`` — the layer offset of text character ``i``, or ``-1``
    for one the collapse dropped — without restating the collapse rule here. A body the
    two read differently (a tokenizer disagreement, not a rendering choice) is refused.
    """

    def __init__(self, text: str, layer: str):
        self.raw = text
        self.layer = layer
        emit = [-1] * len(text)
        at = 0
        size = len(layer)
        for i, ch in enumerate(text):
            if ch in _WS:
                continue
            while at < size and layer[at] == " ":
                at += 1
            if at >= size or layer[at] != ch:
                seen = layer[at] if at < size else "<the end of the layer>"
                raise RenderError(
                    f"the body's text and its layer disagree at text offset {i}: the tree "
                    f"reads {ch!r} and site/anchor_resolve.text_layer reads {seen!r}; the "
                    f"two readers of this body do not agree, so it has no {RENDERER} "
                    f"rendering")
            emit[i] = at
            at += 1
        while at < size and layer[at] == " ":
            at += 1
        if at != size:
            raise RenderError(f"the layer holds {size - at} character(s) the body's tree "
                              f"does not: the two readers of this body do not agree")
        self.emit = emit
        n = len(text)
        nxt = [n] * (n + 1)
        for i in range(n - 1, -1, -1):
            nxt[i] = i if emit[i] >= 0 else nxt[i + 1]
        prv = [-1] * (n + 1)
        last = -1
        for i in range(n + 1):
            prv[i] = last
            if i < n and emit[i] >= 0:
                last = i
        self.nxt = nxt
        self.prv = prv

    def span(self, start: int, end: int) -> tuple[int, int] | None:
        """The layer span of text ``[start, end)``: first to last non-whitespace character."""
        j = self.nxt[start]
        if j >= end:
            return None
        k = self.prv[end]
        return self.emit[j], self.emit[k] + 1

    def position(self, offset: int) -> int:
        """The layer offset of the first text at or after a text offset."""
        j = self.nxt[offset]
        return self.emit[j] if j < len(self.raw) else len(self.layer)


def _alttext(node: _Node, raw: str) -> str | None:
    """A formula's alttext: math@alttext, else the x-tex annotation, else the image alt."""
    for el in node.iter():
        if el.tag == "math" and el.attrs.get("alttext"):
            return el.attrs["alttext"]
    for el in node.iter():
        if el.tag == "annotation" and el.attrs.get("encoding", "").lower() == "application/x-tex":
            text = "".join(raw[a:b] for a, b in _texts(el))
            if text.strip():
                return text
    for el in node.iter():
        if el.tag == "img" and any("mwe-math-fallback-image" in c for c in el.classes) \
                and el.attrs.get("alt"):
            return el.attrs["alt"]
    return None


def _texts(node: _Node, *, kept: bool = False) -> Iterable[tuple[int, int]]:
    """Every text range under ``node`` in document order (``kept``: outside dropped regions)."""
    stack = [iter(node.children)]
    while stack:
        child = next(stack[-1], None)
        if child is None:
            stack.pop()
        elif isinstance(child, _Node):
            if not (kept and _dropped(child)):
                stack.append(iter(child.children))
        else:
            yield child


def _kept_span(node: _Node, layer: "_Layer") -> tuple[int, int] | None:
    """The layer span of the node's text outside the dropped regions, or None."""
    first = last = None
    for a, b in _texts(node, kept=True):
        j = layer.nxt[a]
        if j < b:
            if first is None:
                first = j
            last = layer.prv[b]
    if first is None:
        return None
    return layer.emit[first], layer.emit[last] + 1


def _kept_text(node: _Node, raw: str) -> str:
    return anchor_resolve.collapse("".join(raw[a:b] for a, b in _texts(node, kept=True)))


def _is_formula_root(node: _Node, inside: bool) -> bool:
    if inside:
        return False
    return "mwe-math-element" in node.classes or node.tag == "math"


def _is_display(node: _Node) -> bool:
    return ("mwe-math-element-block" in node.classes or node.tag == "div"
            or (node.tag == "math" and node.attrs.get("display", "").lower() == "block"))


def _dropped(node: _Node) -> bool:
    """Is this element a region the reading copy and the block list leave out?"""
    if node.tag in _SKIPPED:
        return True
    classes = node.classes
    if node.tag == "sup" and "reference" in classes:
        return True
    if node.tag == "ol" and "references" in classes:
        return True
    return bool(classes & {"reflist", "mw-references-wrap", "mw-editsection"})


def _is_theorem_box(node: _Node) -> bool:
    return bool(node.classes & _THEOREM_CLASSES)


# --------------------------------------------------------------------------
# the rendering
# --------------------------------------------------------------------------

@dataclass
class Rendering:
    """Every view of one canonical body."""

    body: str
    layer: str
    formulas: list[dict]
    head: str
    text: str
    segments: list[list[int]]
    sections: list[dict]
    paragraphs: list[dict]
    finders: list[dict]
    counts: dict = field(default_factory=dict)

    @property
    def html_sha256(self) -> str:
        return sha256_text(self.body)

    @property
    def layer_sha256(self) -> str:
        return sha256_text(self.layer)

    @property
    def head_sha256(self) -> str:
        return sha256_text(self.head)

    @property
    def text_sha256(self) -> str:
        return sha256_text(self.text)

    def record(self) -> dict:
        """``render.json``: the shas, the counts, and every view's sha (``views``)."""
        views = self._views()
        return {"renderer": RENDERER, "html_sha256": self.html_sha256,
                "layer_sha256": self.layer_sha256, "head_sha256": self.head_sha256,
                "text_sha256": self.text_sha256, "counts": dict(self.counts),
                "views": {name: sha256_text(views[name]) for name in VIEW_FILES}}

    def layer_map(self) -> dict:
        return {"schema": LAYER_MAP_SCHEMA, "renderer": RENDERER,
                "segments": [list(s) for s in self.segments]}

    def head_map(self) -> "locate_text.HeadMap":
        return locate_text.HeadMap(self.layer, self.formulas)

    def text_to_layer(self, offset: int) -> int | None:
        return text_to_layer(self.segments, offset)

    def layer_to_text(self, offset: int) -> int | None:
        return layer_to_text(self.segments, offset)

    def _views(self) -> dict[str, str]:
        return {
            "body.html": self.body,
            "layer.txt": self.layer,
            "formulas.json": _dump(self.formulas),
            "head.txt": self.head,
            "text.txt": self.text,
            "layer_map.json": _dump(self.layer_map()),
            "sections.json": _dump(self.sections),
            "paragraphs.json": _dump(self.paragraphs),
            "finders.json": _dump(self.finders),
        }

    def files(self) -> dict[str, str]:
        """``{file name: content}`` for every file :func:`write_rendering` writes."""
        return {**self._views(), "render.json": _dump(self.record())}


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=1) + "\n"


class _Copy:
    """The reading copy and its map, built in document order."""

    def __init__(self, layer: _Layer):
        self.c = layer
        self.parts: list[str] = []
        self.length = 0
        self.tmap: list[int | None] = []      # per text character: its layer offset
        self.atomic: list[list[int]] = []
        self.pending_space = False
        self.last_layer = -10                 # layer offset of the last mapped character
        self.line_has_text = False

    def _append(self, text: str, mapped=None) -> None:
        self.parts.append(text)
        self.length += len(text)
        if mapped is None:
            self.tmap.extend([None] * len(text))
        else:
            self.tmap.extend(mapped)

    def _tail(self, n: int) -> str:
        out = ""
        for part in reversed(self.parts):
            out = part + out
            if len(out) >= n:
                break
        return out[-n:]

    def _flush_space(self, next_layer: int | None) -> None:
        if self.pending_space and self.line_has_text and not self._tail(1).isspace():
            layer = self.c.layer
            prev = self.last_layer
            if (next_layer is not None and next_layer == prev + 2
                    and 0 <= prev + 1 < len(layer) and layer[prev + 1] == " "):
                self._append(" ", [prev + 1])
            else:
                self._append(" ")
        self.pending_space = False

    def text(self, start: int, end: int) -> None:
        raw, emit = self.c.raw, self.c.emit
        for i in range(start, end):
            ch = raw[i]
            if ch in _WS:
                self.pending_space = True
                continue
            at = emit[i]
            self._flush_space(at)
            self._append(ch, [at])
            self.last_layer = at
            self.line_has_text = True

    def literal(self, text: str) -> None:
        """Unmapped characters the copy adds (bullets, separators)."""
        self._flush_space(None)
        self._append(text)
        self.line_has_text = True

    def atom(self, text: str, span: tuple[int, int]) -> None:
        self._flush_space(span[0])
        t0 = self.length
        self._append(text)
        self.atomic.append([t0, self.length, span[0], span[1], SEGMENT_FORMULA])
        self.last_layer = span[1] - 1
        self.line_has_text = True

    def line_break(self) -> None:
        self.pending_space = False
        if self.line_has_text:
            self._append("\n")
            self.line_has_text = False

    def block_break(self) -> None:
        self.pending_space = False
        if self.length == 0:
            return
        tail = self._tail(2)
        if tail == "\n\n":
            return
        self._append("\n" if tail.endswith("\n") else "\n\n")
        self.line_has_text = False

    def finish(self) -> tuple[str, list[list[int]]]:
        text = "".join(self.parts)
        cut = len(text.rstrip("\n"))
        text = text[:cut] + ("\n" if cut else "")
        tmap = self.tmap[:cut] + ([None] if cut else [])
        segments: list[list[int]] = []
        run: list[int] | None = None
        for t, at in enumerate(tmap):
            if at is not None and run is not None and at == run[3] and t == run[1]:
                run[1] += 1
                run[3] += 1
                continue
            if run is not None:
                segments.append(run)
                run = None
            if at is not None:
                run = [t, t + 1, at, at + 1, SEGMENT_TEXT]
        if run is not None:
            segments.append(run)
        segments.extend(s for s in self.atomic if s[1] <= cut)
        segments.sort()
        return text, segments


class LayerMap:
    """``layer_map.json``'s segments with their search keys built once, for many lookups."""

    def __init__(self, segments: list[list[int]]):
        self.segments = [list(s) for s in segments]
        self._t = [s[0] for s in self.segments]
        self._l = [s[2] for s in self.segments]

    @classmethod
    def read(cls, directory: str | Path) -> "LayerMap":
        """A source directory's map, refused unless THIS renderer wrote it and the views.

        A map's segments are offsets into one renderer's ``text.txt`` and ``layer.txt``;
        read against another renderer's views they point at the wrong characters.
        """
        directory = Path(directory)
        path = directory / "layer_map.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        hint = (f"re-render the source under {RENDERER} (a recorded layer never changes: "
                f"seed a new sourceset from the old one)")
        if not isinstance(data, dict) or data.get("schema") != LAYER_MAP_SCHEMA:
            raise RenderError(f"{path} is not a {LAYER_MAP_SCHEMA} layer map; {hint}")
        if data.get("renderer") != RENDERER:
            raise RenderError(f"{path} was written by renderer {data.get('renderer')!r}, and "
                              f"this one is {RENDERER}; {hint}")
        record = directory / "render.json"
        if record.is_file():
            written_by = json.loads(record.read_text(encoding="utf-8")).get("renderer")
            if written_by != RENDERER:
                raise RenderError(f"{directory}: render.json names renderer {written_by!r}, "
                                  f"the layer map {RENDERER}; {hint}")
        return cls(data["segments"])

    def text_to_layer(self, offset: int) -> int | None:
        i = bisect_right(self._t, offset) - 1
        return _lookup(self.segments, i, offset, 0)

    def layer_to_text(self, offset: int) -> int | None:
        i = bisect_right(self._l, offset) - 1
        return _lookup(self.segments, i, offset, 2)


def _lookup(segments, i: int, offset: int, side: int) -> int | None:
    """Map ``offset`` through segment ``i`` from ``side`` (0: text, 2: layer) to the other."""
    if i < 0:
        return None
    seg = segments[i]
    a, b = seg[side], seg[side + 1]
    other = 2 - side
    if not a <= offset < b:
        return None
    if seg[4] == SEGMENT_TEXT:
        return seg[other] + (offset - a)
    return seg[other] if offset == a else None       # a formula maps only as a whole


def text_to_layer(segments: list[list[int]], offset: int) -> int | None:
    """The layer offset of a reading-copy offset, or None where the copy added the character."""
    return _lookup(segments, bisect_right([s[0] for s in segments], offset) - 1, offset, 0)


def layer_to_text(segments: list[list[int]], offset: int) -> int | None:
    """The reading-copy offset of a layer offset, or None where the copy has no character."""
    return _lookup(segments, bisect_right([s[2] for s in segments], offset) - 1, offset, 2)


def text_span(segments: list[list[int]], span) -> list[int] | None:
    """A layer span's reading-copy span (``text.txt`` offsets), through the layer map.

    The first segment ending after the start and the last starting before the end bound
    it; a formula maps as a whole, since the copy writes it as ``$TeX$`` and its two
    lengths have nothing to do with each other. None when the span maps to nothing —
    a span wholly inside a dropped region, which the copy does not hold.
    """
    if not span:
        return None
    a, b = int(span[0]), int(span[1])
    start = end = None
    for t0, t1, l0, l1, kind in segments:
        if l1 <= a or l0 >= b:
            continue
        if start is None:
            start = t0 + max(0, a - l0) if kind == SEGMENT_TEXT else t0
        end = t0 + (min(b, l1) - l0) if kind == SEGMENT_TEXT else t1
    return [start, end] if start is not None and end is not None else None


class _Walker:
    """One pass over the tree: formulas, sections, blocks, finders, the reading copy."""

    def __init__(self, root: _Node, layer: _Layer):
        self.root = root
        self.c = layer
        self.copy = _Copy(layer)
        self.formulas: list[dict] = []
        self.no_text_formulas = 0
        self.headings: list[tuple[_Node, int]] = []
        self.blocks: list[tuple[_Node, str, tuple[int, int], bool]] = []
        self.boxes: list[tuple[_Node, tuple[int, int]]] = []
        #: layer starts of the formulas written as HTML (``.texhtml``), outermost only
        self.texhtml: list[int] = []
        #: node, span, in table, in heading
        self.paras: list[tuple[_Node, tuple[int, int], bool, bool]] = []

    def run(self) -> None:
        self._visit(self.root, in_formula=False, dropped=False, in_table=False,
                    in_heading=False, in_cell=False)

    def _visit(self, node: _Node, *, in_formula: bool, dropped: bool, in_table: bool,
               in_heading: bool, in_cell: bool) -> None:
        for child in node.children:
            if isinstance(child, tuple):
                if not dropped:
                    self.copy.text(*child)
                continue
            self._element(child, in_formula=in_formula, dropped=dropped, in_table=in_table,
                          in_heading=in_heading, in_cell=in_cell)

    def _formula_break(self, display: bool, in_cell: bool) -> None:
        """Around a display formula: a block break, or inside a table cell a space."""
        if not display:
            return
        if in_cell:
            self.copy.pending_space = True
        else:
            self.copy.block_break()

    def _element(self, node: _Node, *, in_formula: bool, dropped: bool, in_table: bool,
                 in_heading: bool, in_cell: bool) -> None:
        c = self.c
        tag = node.tag
        if tag in _SKIPPED:
            return          # no text, no formula, no heading, no block: nothing is read
        span = c.span(node.start, node.end)
        drop_here = dropped or _dropped(node)
        # formulas: always in the layer; the copy gets the TeX when not dropped
        if _is_formula_root(node, in_formula):
            alt = _alttext(node, c.raw)
            if alt is not None:
                display = _is_display(node)
                tex = "$" + locate_text.normalize_alttext(alt) + "$"
                if span is None:
                    self.no_text_formulas += 1
                else:
                    self.formulas.append({"span": [span[0], span[1]], "alttext": alt})
                if not drop_here:
                    if display and span is not None:
                        self.blocks.append((node, "math", span, False))
                    self._formula_break(display, in_cell)
                    if span is None:
                        self.copy.literal(tex)      # an image only: added characters
                    else:
                        self.copy.atom(tex, span)
                    self._formula_break(display, in_cell)
                return
        heading = _HEADINGS.get(tag)
        if heading is not None and not in_formula:
            self.headings.append((node, heading))
        if ("texhtml" in node.classes and not in_formula and not drop_here
                and not any("texhtml" in a.classes for a in node.ancestors())):
            kept_formula = _kept_span(node, c)
            if kept_formula is not None:
                self.texhtml.append(kept_formula[0])
        kept = (_kept_span(node, c)
                if not drop_here and (tag in _BLOCK_TAGS or _is_theorem_box(node)) else None)
        if kept is not None:
            if tag in _BLOCK_TAGS:
                self.blocks.append((node, tag, kept, in_table))
            if _is_theorem_box(node):
                self.blocks.append((node, "theorem_box", kept, in_table))
                self.boxes.append((node, kept))
            if tag == "p":
                self.paras.append((node, kept, in_table, in_heading))
        copy = self.copy
        # inside a table cell a block is only a word break: a cell is one line
        breaks = (tag in _COPY_BLOCKS or _is_theorem_box(node)) and not in_cell
        if not drop_here:
            if tag == "br":
                copy.line_break()
            elif tag in ("td", "th"):
                if copy.line_has_text:
                    copy.pending_space = False     # the separator brings its own spaces
                    copy.literal(" | ")
            elif breaks:
                copy.block_break()
            elif tag in _COPY_BLOCKS and in_cell:
                copy.pending_space = True
            if heading is not None and not in_cell:
                copy.literal("#" * heading + " ")
            if tag == "li" and not in_cell:
                copy.literal("- ")
        self._visit(node, in_formula=in_formula or _is_formula_root(node, in_formula),
                    dropped=drop_here,
                    in_table=in_table or tag in ("table", "caption"),
                    in_heading=in_heading or heading is not None,
                    in_cell=in_cell or tag in ("td", "th", "caption"))
        if not drop_here and breaks:
            copy.block_break()
        elif not drop_here and tag in _COPY_BLOCKS and in_cell:
            copy.pending_space = True


def _heading_fragment(node: _Node) -> str | None:
    if node.attrs.get("id"):
        return node.attrs["id"]
    for el in node.iter():
        if el is not node and "mw-headline" in el.classes and el.attrs.get("id"):
            return el.attrs["id"]
    return None


def _iter_kept(node: _Node):
    """Every descendant element outside a dropped region, in document order."""
    stack = [iter(node.children)]
    while stack:
        child = next(stack[-1], None)
        if child is None:
            stack.pop()
        elif isinstance(child, _Node) and not _dropped(child):
            yield child
            stack.append(iter(child.children))


def _first_text(node: _Node, raw: str, predicate) -> str | None:
    for el in _iter_kept(node):
        if predicate(el):
            text = _kept_text(el, raw)
            if text:
                return text
    return None


def is_term(text: str) -> bool:
    """Does a bold run's text name a term: enough letters, and not only a label?"""
    return (sum(1 for ch in text if ch.isalpha()) >= MIN_TERM_LETTERS
            and not vocab.is_bold_label(text))


def _defined_term(node: _Node, raw: str) -> tuple[str | None, bool]:
    """``(the kept text of the first b of a paragraph that names a term, labelled)``.

    ``labelled`` is True when there is no such ``b`` but one with enough letters to be a
    term was refused only because it is a label.
    """
    labelled = False
    for el in _iter_kept(node):
        if el.tag == "b":
            text = _kept_text(el, raw)
            if text and is_term(text):
                return text, False
            if text and sum(1 for ch in text if ch.isalpha()) >= MIN_TERM_LETTERS:
                labelled = True
    return None, labelled


def render(body: str) -> Rendering:
    """Every view of one canonical body. Deterministic: the same body, the same bytes."""
    if not isinstance(body, str):
        raise RenderError("the body is text: decode it (canonical_body) first")
    _encodable(body)
    builder = _TreeBuilder()
    try:
        builder.feed(body)
        root, raw = builder.finish()
    except AssertionError as exc:  # html.parser's own refusals
        raise RenderError(f"the body does not parse as HTML ({exc})") from None
    layer_text, _starts, _ends = anchor_resolve.text_layer(body)
    mapping = _Layer(raw, layer_text)
    layer = mapping.layer
    walker = _Walker(root, mapping)
    walker.run()
    formulas = sorted(walker.formulas, key=lambda f: f["span"])
    head_map = locate_text.HeadMap(layer, formulas)
    head = head_map.head

    # sections: the lead, then one per heading, tiling the layer
    sections: list[dict] = []
    open_titles: list[tuple[int, str]] = []
    heading_entries = []
    for node, level in walker.headings:
        title = _kept_text(node, raw)
        while open_titles and open_titles[-1][0] >= level:
            open_titles.pop()
        open_titles.append((level, title))
        heading_entries.append({"path": [t for _l, t in open_titles],
                                "fragment": _heading_fragment(node), "level": level,
                                "start": mapping.position(node.start)})
    bounds = [h["start"] for h in heading_entries] + [len(layer)]
    sections.append({"path": [], "fragment": None, "level": 0,
                     "span": [0, bounds[0] if heading_entries else len(layer)]})
    for i, entry in enumerate(heading_entries):
        sections.append({"path": entry["path"], "fragment": entry["fragment"],
                         "level": entry["level"], "span": [entry["start"], bounds[i + 1]]})
    starts = [s["span"][0] for s in sections]

    def section_of(offset: int) -> list[str]:
        i = bisect_right(starts, offset) - 1
        # an empty section shares its start with the next one; take the last
        return list(sections[max(i, 0)]["path"])

    lead_end = sections[0]["span"][1]

    # deterministic finders
    finder_rows: list[dict] = []
    dropped = 0
    labels_dropped = 0

    def propose(finder: str, span: tuple[int, int], term: str | None) -> None:
        nonlocal dropped
        start = span[0]
        try:
            locate_text.check_start(head, head_map.to_head(start))
        except locate_text.HeadError:
            dropped += 1
            return
        finder_rows.append({"finder": finder, "start": start, "span": [span[0], span[1]],
                            "section": section_of(start), "term": term})

    for node, span in walker.boxes:
        term = (_first_text(node, raw,
                            lambda el: bool(el.classes & {"theorem-name",
                                                          "math_theorem_name"}))
                or _first_text(node, raw, lambda el: el.tag in ("b", "strong")))
        propose(vocab.FINDER_THEOREM_BOX, span, term)
    for node, span, in_table, in_heading in walker.paras:
        if in_table or in_heading:
            continue
        term, labelled = _defined_term(node, raw)
        if term:
            propose(vocab.FINDER_BOLD_DEFINITION, span, term)
        labels_dropped += labelled
    for node, span, in_table, _in_heading in walker.paras:
        if in_table or span[0] >= lead_end:
            continue
        if len(_kept_text(node, raw).split(" ")) >= LEAD_MIN_WORDS:
            propose(vocab.FINDER_LEAD, span, None)
            break
    seen = set()
    finders = []
    for row in sorted(finder_rows, key=lambda r: (r["start"], r["finder"])):
        key = (row["finder"], row["start"])
        if key not in seen:
            seen.add(key)
            finders.append(row)
    finder_starts = sorted(r["start"] for r in finders)

    # blocks
    formula_starts = [f["span"][0] for f in formulas]
    texhtml_starts = sorted(walker.texhtml)

    def inside(points: list[int], a: int, b: int) -> int:
        return bisect_right(points, b - 1) - bisect_right(points, a - 1)

    paragraphs = []
    for node, kind, span, _in_table in walker.blocks:
        a, b = span
        n = inside(formula_starts, a, b)
        html_n = inside(texhtml_starts, a, b)
        hit = inside(finder_starts, a, b)
        paragraphs.append({"span": [a, b], "section": section_of(a), "block": kind,
                           "formulas": n, "texhtml": html_n, "math": bool(n or html_n or hit)})
    paragraphs.sort(key=lambda p: (p["span"][0], -p["span"][1], p["block"]))

    text, segments = walker.copy.finish()
    counts = {
        "layer_chars": len(layer), "text_chars": len(text), "formulas": len(formulas),
        "formulas_without_text": walker.no_text_formulas, "texhtml": len(texhtml_starts),
        "sections": len(sections),
        "paragraphs": len(paragraphs),
        "math_paragraphs": sum(1 for p in paragraphs if p["math"]),
        "finders": {name: sum(1 for r in finders if r["finder"] == name)
                    for name in ("theorem_box", "bold_definition", "lead")},
        "finders_dropped": dropped,
        "bold_labels_dropped": labels_dropped,
    }
    return Rendering(body=body, layer=layer, formulas=formulas, head=head, text=text,
                     segments=segments, sections=sections, paragraphs=paragraphs,
                     finders=finders, counts=counts)


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------

def view_differences(directory: str | Path, rendering: Rendering) -> tuple[list[str], list[str]]:
    """``(missing, differing)``: the files of ``rendering`` absent from ``directory``, and
    those present with other bytes than this rendering writes, in :data:`RENDER_FILES` order.

    Under one renderer id the same body renders to the same bytes, so a file that differs
    was written by another rule under this id — what a render refuses to rewrite.
    """
    directory = Path(directory)
    missing, differing = [], []
    for name, content in rendering.files().items():
        try:
            data = (directory / name).read_bytes()
        except FileNotFoundError:
            missing.append(name)
            continue
        if data != content.encode("utf-8"):
            differing.append(name)
    return missing, differing


def write_rendering(directory: str | Path, rendering: Rendering) -> dict:
    """Write every file of :data:`RENDER_FILES` into ``directory``; returns ``render.json``.

    Each file is written atomically as the exact UTF-8 bytes of its content (no newline
    translation, so a body holding a CR keeps the sha it was recorded with); an unchanged
    file is left alone, so a re-render of the same body changes nothing on disk.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in rendering.files().items():
        path = directory / name
        data = content.encode("utf-8")
        try:
            if path.read_bytes() == data:
                continue
        except OSError:
            pass
        write_bytes(path, data)
    return rendering.record()


def write_bytes(path: str | Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically (temp file in the same directory, then replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_body(path: str | Path) -> str:
    """A ``body.html`` as its exact text: UTF-8, no newline translation."""
    try:
        return Path(path).read_bytes().decode("utf-8")
    except FileNotFoundError:
        raise RenderError(f"{path} is missing: fetch or adopt the source first") from None
    except UnicodeDecodeError as exc:
        raise RenderError(f"{path} is not UTF-8 ({exc})") from None


def render_directory(directory: str | Path, *, write: bool = True) -> Rendering:
    """Render the ``body.html`` of a source directory, and write every view beside it."""
    directory = Path(directory)
    rendering = render(read_body(directory / "body.html"))
    if write:
        write_rendering(directory, rendering)
    return rendering


def read_layer(directory: str | Path) -> tuple[str, list[dict]]:
    """``(layer, formulas)`` of a rendered source directory."""
    directory = Path(directory)
    layer = (directory / "layer.txt").read_text(encoding="utf-8")
    formulas = json.loads((directory / "formulas.json").read_text(encoding="utf-8"))
    return layer, formulas


def head_map(directory: str | Path) -> "locate_text.HeadMap":
    """The :class:`gate.locate_text.HeadMap` of a rendered source directory."""
    layer, formulas = read_layer(directory)
    return locate_text.HeadMap(layer, formulas)
