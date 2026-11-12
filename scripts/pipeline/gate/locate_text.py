#!/usr/bin/env python3
"""The head text, the quote and the blocks of a web source — standard library only.

docs/PIPELINE.md section 3 (stage 7). Every reader of a rendered web source needs the
same five things, and one of them — the gate — runs on a run host or a contributor's
machine with nothing importable, so they live here, stdlib-only, beside the gate:

1. **The head text** of a source is its Literature Links text layer (``layer.txt``) with
   each formula's layer span replaced by its token ``⟦<normalized alttext>⟧``
   (``formulas.json`` lists the spans). :class:`HeadMap` builds it and converts offsets
   both ways; a layer offset inside a formula has no head offset and is refused.
2. **The passage id** of a head start: the frozen rule, here as :func:`hex_at` and
   :func:`assign_ids`. It is frozen in the strongest sense — it is the ``item`` of every
   record of every sealed set, so a change here renames every passage ever judged.
3. **The quote**: :func:`find_quote` gives every layer offset where ``exact`` occurs with
   ``prefix`` immediately before it and ``suffix`` immediately after. A gate accepts
   exactly one. Matching is on the layer's own characters — no normalisation at all —
   because the layer is the one view quotes are cut from.
4. **The end** of a passage: :func:`passage_end`, the rule the planner fills a span with
   and gate rule 2 recomputes, in three versions (a run's ``span_end_rule``).
5. **The blocks** the statement cut reads: :class:`Blocks` over ``paragraphs.json`` and
   ``formulas.json``, which the end rule and gate rules 8 and 10 rest on.

**The whitespace rule is WikiLean's own.** ``normalize_text`` is NFKC followed by
``site/anchor_resolve.collapse`` — the Literature Links whitespace rule this repository
already implements for the editor's anchors, the 25 ``White_Space`` code points and not
Python's ``\\s``. ``anchor_resolve`` is staged beside this file in a gate directory and
imported from ``site/`` in a checkout; there is one definition of the layer's whitespace
in WikiLean and this is a use of it, not a second copy.

Ported from formal-companion's ``gate/locate_text.py``, whose own renderer says it is a
port of ``site/anchor_resolve.text_layer``: the move returns the rule to where it came
from. The one substantive change is the import above — the vendored legacy annotation
package the original hashed through does not move here, and does not need to: its
``normalize_text`` is this whitespace rule and its passage id is the first eight hex of
the sha256 of the normalised head, which is what :func:`head_hex` computes.
"""
from __future__ import annotations

import hashlib
import re
import sys
import unicodedata
from bisect import bisect_right
from pathlib import Path
from typing import Iterable, Sequence

_HERE = Path(__file__).resolve().parent
for _candidate in (_HERE, _HERE.parents[2] / "site"):
    if (_candidate / "anchor_resolve.py").is_file():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break

import anchor_resolve  # noqa: E402

#: The formula token's delimiters (U+27E6, U+27E7); NFKC leaves them alone.
FORMULA_OPEN = "⟦"
FORMULA_CLOSE = "⟧"
#: The head starts at this many words and is extended up to this many.
HEAD_WORDS = 12
MAX_HEAD_WORDS = 40
_DISPLAYSTYLE = re.compile(r"\\displaystyle(?![A-Za-z])")
_WINDOW = 1024
#: The code points :func:`normalize_text` strips from the ends, as a string for ``strip``.
_WHITE = "".join(sorted(anchor_resolve.WHITE_SPACE))


class HeadError(ValueError):
    """An offset, a formula list or a passage table the head rule cannot key."""


def normalize_text(text: str) -> str:
    """NFKC, then each run of ``White_Space`` to one space, ends stripped.

    The passage-id normaliser. NFKC comes first because it can turn a character into
    whitespace (U+00A0) and it folds ligatures and the micro sign; case is kept. The
    whitespace rule is ``site/anchor_resolve.collapse``'s, which is Literature Links'.
    """
    return anchor_resolve.collapse(unicodedata.normalize("NFKC", str(text)))


# ---- formulas ------------------------------------------------------------------------


def _single_group(text: str) -> bool:
    if len(text) < 2 or text[0] != "{" or text[-1] != "}":
        return False
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i == len(text) - 1
        i += 1
    return False


def normalize_alttext(alttext: str) -> str:
    """Whitespace collapsed, ``\\displaystyle`` dropped, outer braces stripped, to a fixed point."""
    text = normalize_text(str(alttext or ""))
    while True:
        before = text
        text = normalize_text(_DISPLAYSTYLE.sub(" ", text))
        if _single_group(text):
            text = normalize_text(text[1:-1])
        if text == before:
            return text


def formula_token(alttext: str) -> str:
    """``⟦<normalized alttext>⟧``: what a formula is in the head text."""
    return FORMULA_OPEN + normalize_alttext(alttext) + FORMULA_CLOSE


# ---- the head text and its offset map --------------------------------------------------


class HeadMap:
    """The head text of one source and the offset map between it and the layer.

    ``formulas`` is ``formulas.json``: ``[{"span": [a, b], "alttext": ...}]``, half-open
    code-point spans of the layer, sorted and disjoint, each non-empty. Anything else is
    refused: a map built from overlapping spans would send two passages to one head
    offset.
    """

    def __init__(self, layer: str, formulas: Sequence[dict] = ()):
        self.layer = str(layer)
        spans: list[tuple[int, int, str]] = []
        previous_end = 0
        for index, formula in enumerate(formulas or ()):
            try:
                a, b = (int(x) for x in formula["span"])
            except (KeyError, TypeError, ValueError):
                raise HeadError(f"formula {index} has no [start, end] span") from None
            if not previous_end <= a < b <= len(self.layer):
                raise HeadError(f"formula {index} span [{a}, {b}] is empty, overlaps the one "
                                f"before it or leaves the layer (0..{len(self.layer)})")
            spans.append((a, b, formula_token(formula.get("alttext", ""))))
            previous_end = b
        pieces: list[str] = []
        #: (layer_start, head_start, layer_end, head_end) of each formula
        self._formulas: list[tuple[int, int, int, int]] = []
        cursor = 0
        head_len = 0
        for a, b, token in spans:
            pieces.append(self.layer[cursor:a])
            head_len += a - cursor
            self._formulas.append((a, head_len, b, head_len + len(token)))
            pieces.append(token)
            head_len += len(token)
            cursor = b
        pieces.append(self.layer[cursor:])
        self.head = "".join(pieces)
        self._layer_starts = [f[0] for f in self._formulas]
        self._head_starts = [f[1] for f in self._formulas]

    def to_head(self, offset: int) -> int:
        """The head offset of a layer offset; refused strictly inside a formula."""
        offset = int(offset)
        if not 0 <= offset <= len(self.layer):
            raise HeadError(f"layer offset {offset} is outside the layer (0..{len(self.layer)})")
        i = bisect_right(self._layer_starts, offset) - 1
        if i < 0:
            return offset
        a, h, b, he = self._formulas[i]
        if offset == a:
            return h
        if offset < b:
            raise HeadError(f"layer offset {offset} is inside the formula at [{a}, {b}]")
        return he + (offset - b)

    def to_layer(self, offset: int) -> int:
        """The layer offset of a head offset; refused strictly inside a formula token."""
        offset = int(offset)
        if not 0 <= offset <= len(self.head):
            raise HeadError(f"head offset {offset} is outside the head text "
                            f"(0..{len(self.head)})")
        i = bisect_right(self._head_starts, offset) - 1
        if i < 0:
            return offset
        a, h, b, he = self._formulas[i]
        if offset == h:
            return a
        if offset < he:
            raise HeadError(f"head offset {offset} is inside the formula token at [{h}, {he}]")
        return b + (offset - he)


# ---- passage ids -----------------------------------------------------------------------


def _is_white(ch: str) -> bool:
    return normalize_text(ch) == ""


def check_start(head: str, start: int) -> int:
    """``start`` if it is the first character of a word of ``head``; else :class:`HeadError`."""
    if isinstance(start, bool) or not isinstance(start, int):
        raise HeadError(f"a passage start must be a character offset, not {start!r}")
    if not 0 <= start < len(head):
        raise HeadError(f"passage start {start} is outside the head text (0..{len(head) - 1})")
    if _is_white(head[start]):
        raise HeadError(f"passage start {start} is whitespace, not the start of a word")
    if start > 0 and not _is_white(head[start - 1]):
        raise HeadError(f"passage start {start} is inside a word, not the start of one")
    return start


def head_at(head: str, start: int, limit: int = MAX_HEAD_WORDS) -> list[str]:
    """Up to ``limit`` normalised words of ``head`` from ``start``."""
    check_start(head, start)
    if limit < 1:
        return []
    size = _WINDOW
    while True:
        end = start + size
        words = normalize_text(head[start:end]).split(" ")
        if end >= len(head):
            return [word for word in words if word][:limit]
        if len(words) > limit:
            return words[:limit]
        size *= 2


def head_hex(words: Sequence[str]) -> str:
    """The 8-hex passage hash of a head: the sha256 of its normalised words."""
    text = normalize_text(" ".join(words))
    if not text:
        raise HeadError("an empty head cannot identify a passage")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def hex_at(head: str, start: int, head_words: int) -> str:
    """Re-hash the first ``head_words`` words from ``start``: the gate's re-check."""
    if isinstance(head_words, bool) or not isinstance(head_words, int) or head_words < 1:
        raise HeadError(f"head_words must be a positive word count, not {head_words!r}")
    if head_words > MAX_HEAD_WORDS:
        raise HeadError(f"head_words {head_words} is over the cap of {MAX_HEAD_WORDS}")
    return head_hex(head_at(head, start, head_words))


def assign_ids(head: str, starts: Iterable[int]) -> list[tuple[int, str, int]]:
    """``[(start, hex, head_words)]`` for ALL of one source's passages, in the order given.

    Each head is the shortest of 12..40 words no other passage of the source starts
    with, so an id depends on the other passages: the whole table is keyed at once. A
    start given twice, forty shared words, or two heads with one hash is a
    :class:`HeadError` naming both.
    """
    offsets = list(starts)
    seen: dict[int, int] = {}
    for index, start in enumerate(offsets):
        check_start(head, start)
        if start in seen:
            raise HeadError(f"passage start {start} is given twice (passages {seen[start]} "
                            f"and {index})")
        seen[start] = index
    words = [head_at(head, start) for start in offsets]
    chosen: list[int | None] = [None] * len(offsets)
    for count in range(HEAD_WORDS, MAX_HEAD_WORDS + 1):
        pending = [i for i, value in enumerate(chosen) if value is None]
        if not pending:
            break
        groups: dict[tuple[str, ...], int] = {}
        for word_list in words:
            key = tuple(word_list[:count])
            groups[key] = groups.get(key, 0) + 1
        for i in pending:
            if groups[tuple(words[i][:count])] == 1:
                chosen[i] = len(words[i][:count])
    out: list[tuple[int, str, int]] = []
    by_hex: dict[str, int] = {}
    for i, start in enumerate(offsets):
        count = chosen[i]
        if count is None:
            twin = next(j for j, word_list in enumerate(words)
                        if j != i and word_list[:MAX_HEAD_WORDS] == words[i][:MAX_HEAD_WORDS])
            raise HeadError(f"passages at offsets {offsets[i]} and {offsets[twin]} share their "
                            f"first {MAX_HEAD_WORDS} words; neither has an id")
        digest = head_hex(words[i][:count])
        if digest in by_hex:
            raise HeadError(f"passages at offsets {offsets[by_hex[digest]]} and {start} have "
                            f"different heads that hash to the same id {digest}")
        by_hex[digest] = i
        out.append((start, digest, count))
    return out


# ---- quotes on the layer ------------------------------------------------------------------


def find_quote(layer: str, exact: str, prefix: str = "", suffix: str = "") -> list[int]:
    """Every layer offset at which ``exact`` starts with ``prefix``/``suffix`` around it.

    Exact character matching on the layer, overlapping occurrences included; an empty
    ``exact`` matches nowhere. The caller decides what a count means: a gate accepts
    exactly one.
    """
    exact = str(exact or "")
    if not exact:
        return []
    prefix = str(prefix or "")
    needle = prefix + exact + str(suffix or "")
    found: list[int] = []
    at = layer.find(needle)
    while at >= 0:
        found.append(at + len(prefix))
        at = layer.find(needle, at + 1)
    return found


# ---- where a passage ends -------------------------------------------------------------


def paragraph_end(paragraphs: Sequence[dict], start: int) -> int | None:
    """The end of the first ``paragraphs.json`` block that holds the offset ``start``.

    The table is sorted by start with an outer block before the blocks it holds, so the
    first block holding ``start`` is the outermost one: a passage that opens a theorem
    box ends where the box does. An entry without a readable span is passed over.
    """
    for para in paragraphs or ():
        try:
            a, b = (int(x) for x in para["span"])
        except (KeyError, TypeError, ValueError):
            continue
        if a <= start < b:
            return b
    return None


def passage_end(layer: str, paragraphs: "Sequence[dict] | Blocks", start: int, exact: str,
                until: object = None, *, rule: int = 1, next_start: int | None = None,
                formulas: Sequence[dict] | None = None) -> int:
    """The layer offset a passage that starts at ``start`` with quote ``exact`` ends at.

    The one rule the planner writes and gate rule 2 recomputes, in the version ``rule``
    names (a run's ``span_end_rule``). With an ``until`` — the passage's last words —
    every version ends at the first occurrence of it at or after the start. Without one,

    * **rule 1** ends it at the end of the paragraph the passage starts in, else at the
      end of the quote;
    * **rule 2** ends it at the earlier of its extent (:meth:`Blocks.extent`: its block,
      and the displays its last sentence introduces) and ``next_start``, the start of the
      document's next passage — so two passages of one paragraph never nest. When the
      next start is the earlier, the separator before it is in neither passage
      (:meth:`Blocks.separator_start`), so a statement's span, and with it its
      ``passage_key``, is the same whether the next passage or an ``until`` at its last
      word ends it;
    * **rule 3** is rule 2 with the *continued* extent: the paragraph that finishes a
      sentence after its display is the sentence's, so "…such that [display] for all x."
      ends after "x.".

    None ever ends before the end of the quote. Offsets are code points of the layer,
    like every span.
    """
    if rule not in SPAN_END_RULES:
        raise ValueError(f"span-end rule {rule!r} is not one of {SPAN_END_RULES}")
    end = start + len(exact)
    if until is None or until == "":
        if rule == 1:
            table = paragraphs.paragraphs if _is_blocks(paragraphs) else paragraphs
            return max(end, paragraph_end(table, start) or end)
        if next_start is not None and (isinstance(next_start, bool)
                                       or not isinstance(next_start, int)
                                       or next_start <= start):
            raise ValueError(f"next_start {next_start!r} is not a passage start after {start}")
        blocks = Blocks.of(layer, paragraphs, formulas)
        stop = blocks.extent(start, compound=False, continued=rule >= SPAN_END_CONTINUED)
        stop = end if stop is None else stop
        if next_start is not None and next_start < stop:
            stop = blocks.separator_start(start, next_start)
        return max(end, stop)
    if not isinstance(until, str):
        raise HeadError(f"`until` is a {type(until).__name__}, not the passage's last words")
    at = layer.find(until, start)
    if at < 0:
        raise HeadError(f"`until` {until[:40]!r} is not on the layer after the passage's start")
    return max(end, at + len(until))


# ---- the statement cut's blocks -------------------------------------------------------

#: The span-end rules this file knows: 1 ends a passage at its paragraph's end, 2 at the
#: earlier of its extent and the next passage's start, and 3 the same with the continued
#: extent.
SPAN_END_STATEMENT_CUT = 2
SPAN_END_CONTINUED = 3
SPAN_END_RULES = (1, SPAN_END_STATEMENT_CUT, SPAN_END_CONTINUED)
#: ``paragraphs.json``'s ``block`` names the cut reads.
BLOCK_THEOREM_BOX = "theorem_box"
BLOCK_MATH = "math"
LIST_BLOCKS = ("li", "dd")
#: What ends a sentence, and the TeX spacing a formula's alttext may end with after its
#: last character (``\qquad`` before ``\quad``, so the longer one is read first);
#: ``\right.`` is a null delimiter, not a full stop; a line break ``\\``, an environment's
#: ``\end{…}`` and a closing brace (``{\text{.}}``) stand after the last character too.
SENTENCE_STOPS = frozenset(".!?")
#: The stops a formula's last character ends a sentence with: TeX writes a factorial with
#: ``!``, so a formula's ``!`` is none. Read as one it would end a chain early.
FORMULA_STOPS = SENTENCE_STOPS - {"!"}
TEX_SPACING = ("\\qquad", "\\quad", "\\,", "\\;", "\\:", "\\!", "~")
TEX_NULL_DELIMITER = "\\right."
TEX_LINE_BREAK = "\\\\"
_TEX_END_ENVIRONMENT = re.compile(r"\\end\s*\{[A-Za-z]+\*?\}\Z")
#: A closing bracket or quote a block may end with after its full stop: read through to
#: the stop when it stands after one.
CLOSING_MARKS = frozenset(")]\"'\u2019\u201d\u00bb")
#: Where the inventory cuts inside a sentence. The end rule's versions 2 and 3 leave the
#: one before the next passage's start out of both passages.
CUT_CONNECTIVES = (";", "since", "because", "so", "hence", "then", "moreover", ", and")
#: What else separates two pieces of one sentence.
SEPARATOR_MARKS = frozenset(",;")
#: A sentence that opens with one of these opens with its reason, and the fact it gives
#: the reason for starts after that clause's comma ("Since W is finite, W is sprocketed").
#: Each is a word of :data:`CUT_CONNECTIVES`: the same cut, read from the other end.
REASON_OPENERS = ("since", "because")
#: A sentence that opens with one of these states a hypothesis, and no cut of the rules
#: separates it from its conclusions or those conclusions from each other, "however many
#: it governs" — a ``then`` of :data:`CUT_CONNECTIVES` included, which is where an
#: "if … then …" would otherwise be cut in two.
HYPOTHESIS_OPENERS = ("if", "when", "whenever", "suppose", "assume", "let", "given")
#: What :meth:`Blocks.allowed_cut` answers with: the rule that puts a passage start
#: inside a sentence. Anything else there is a fragment of a sentence nobody cut.
CUT_FORMULA = "a display formula of its own (R-SPLIT)"
CUT_ITEM = "a list item"
CUT_REASON = "the conclusion after the reason its sentence opens with"
#: The one block a sentence continues into after its display: a plain paragraph, never a
#: list item, a ``dd``, a quotation or a theorem box.
CONTINUATION_BLOCK = "p"
#: The blocks a continuation may leave, beside a display block. A container (a
#: ``blockquote``, a theorem box, a list item) is a unit, and its sentence never runs out.
CONTINUED_FROM = frozenset({"p", "dd", BLOCK_MATH})
#: The opening brackets and quotes read past to a paragraph's first word; never ``[``,
#: which opens a footnote mark.
OPENING_MARKS = frozenset("(\"'\u2018\u201c")
#: A word that labels an item rather than going on with a sentence: lettered ("a)", its
#: opening bracket set aside by the readers below), roman ("iii)") or numbered ("2)").
#: A number labels an item as often as a letter does, and :meth:`Blocks.continues` and
#: :meth:`Blocks.opens_with_formula` read a word with no leading lower-case letter as a
#: continuation of nothing either way, so the number changes only
#: :meth:`Blocks.allowed_cut`, where it stops a numbered list item being refused as a
#: start a lettered one is allowed.
_ITEM_LABEL = re.compile(r"(?:[^\W\d_]{1,4}|[0-9]{1,3})\)")
_LETTERS = re.compile(r"[^\W\d_]*")
#: The words a full stop does not end a sentence after. ``scripts/pipeline/records.py``
#: carries the same list for the note a downgrade leaves; the two move together.
ABBREVIATIONS = frozenset({"i.e.", "e.g.", "cf.", "resp.", "etc.", "viz.", "vs.", "w.r.t."})
#: What a reference superscript leaves in the layer between two blocks. Only the wide
#: bound reads it, so ``[0, 1]``, ``[edit]`` and a heading's text are no mark.
FOOTNOTE_MARK = re.compile(r"\[(?:[0-9]{1,4}|[a-z]|(?:nb|note) [0-9]{1,4})\]")


def formula_stop(alttext: str) -> str:
    """The last character of a formula's normalised alttext, what TeX writes after it set
    aside; '' when nothing is left.

    Set aside, from the end and again until nothing more is: TeX spacing, a ``\\right.``
    null delimiter, a line break, an environment's closer and a closing brace that is not
    escaped. So a display ``a = b.\\,`` ends its sentence with a full stop, one ending
    ``,`` runs on, and ``x^{n!}`` ends with ``!``, a factorial, which ends no sentence.
    """
    text = normalize_alttext(alttext)
    while True:
        before = text
        text = text.rstrip()
        closer = _TEX_END_ENVIRONMENT.search(text)
        if closer is not None:
            text = text[:closer.start()]
        elif text.endswith("}") and not _escaped(text, len(text) - 1):
            text = text[:-1]
        else:
            for tail in (TEX_NULL_DELIMITER, TEX_LINE_BREAK, *TEX_SPACING):
                if text.endswith(tail) and not _escaped(text, len(text) - len(tail)):
                    text = text[:-len(tail)]
                    break
        if text == before:
            return text[-1:] if text else ""


def _escaped(text: str, at: int) -> bool:
    """Is the character at ``at`` escaped: an odd run of backslashes before it?"""
    run = 0
    while at - run - 1 >= 0 and text[at - run - 1] == "\\":
        run += 1
    return run % 2 == 1


class Block:
    """One span of ``paragraphs.json``: every entry with exactly that span, in table order.

    ``kinds`` holds each entry's ``block`` (a theorem box and its one paragraph may share
    a span, and so may a ``dd`` and the display it holds); ``kind`` is the first one's.
    """

    __slots__ = ("start", "end", "kind", "kinds")

    def __init__(self, start: int, end: int, kind: str):
        self.start, self.end, self.kind = start, end, kind
        self.kinds = [kind]

    @property
    def span(self) -> tuple[int, int]:
        return self.start, self.end

    @property
    def theorem_box(self) -> bool:
        return BLOCK_THEOREM_BOX in self.kinds

    @property
    def list_kind(self) -> str | None:
        """``li`` or ``dd`` when the block is a list item, else None."""
        return next((kind for kind in self.kinds if kind in LIST_BLOCKS), None)

    def __repr__(self) -> str:
        return f"Block({self.start}, {self.end}, {'/'.join(self.kinds)})"


class Blocks:
    """The blocks of one source, with its layer and formulas.

    ``paragraphs`` is ``paragraphs.json``: an entry without a readable non-empty span is
    passed over, as :func:`paragraph_end` passes it over. The table is read in its own
    order with an outer block before the ones it holds, so the first block holding an
    offset is the outermost one. ``formulas`` is ``formulas.json``.
    """

    #: What :func:`_is_blocks` reads: this file may be imported under two names, so a
    #: class check would tell one module's Blocks from the other's.
    _locate_text_blocks = True

    def __init__(self, layer: str, paragraphs: Sequence[dict], formulas: Sequence[dict]):
        self.layer = str(layer)
        #: the table as given: what span-end rule 1 reads
        self.paragraphs = list(paragraphs or ())
        rows: list[tuple[int, int, int, str]] = []
        for index, para in enumerate(self.paragraphs):
            try:
                a, b = (int(x) for x in para["span"])
            except (KeyError, TypeError, ValueError):
                continue
            if a < b:
                rows.append((a, -b, index, str(para.get("block") or "")))
        rows.sort()
        self.blocks: list[Block] = []
        for a, negative_b, _index, kind in rows:
            if self.blocks and self.blocks[-1].span == (a, -negative_b):
                self.blocks[-1].kinds.append(kind)
            else:
                self.blocks.append(Block(a, -negative_b, kind))
        #: the blocks no other block holds, in layer order: what next() returns
        self.top: list[Block] = []
        reach = -1
        for block in self.blocks:
            if block.end > reach:
                self.top.append(block)
                reach = block.end
        self._top_starts = [block.start for block in self.top]
        #: the display formulas (``math`` blocks), by start
        self._math = [block for block in self.blocks if BLOCK_MATH in block.kinds]
        self._math_starts = [block.start for block in self._math]
        spans: list[tuple[int, int, str]] = []
        for formula in formulas or ():
            try:
                fa, fb = (int(x) for x in formula["span"])
            except (KeyError, TypeError, ValueError):
                continue
            spans.append((fa, fb, str(formula.get("alttext") or "")))
        spans.sort()
        self.formulas = spans
        self._formula_starts = [f[0] for f in spans]
        #: :meth:`sentence_at`'s reading of one block, kept because the sentence check
        #: asks for it once per passage start and a block holds several
        self._sentences: dict[tuple[int, int], list[tuple[int, int]]] = {}

    @classmethod
    def of(cls, layer: str, paragraphs: "Sequence[dict] | Blocks",
           formulas: Sequence[dict] | None = None) -> "Blocks":
        """``paragraphs`` itself when it is already a :class:`Blocks`, else one built."""
        if _is_blocks(paragraphs):
            return paragraphs
        if formulas is None:
            raise TypeError("the statement cut reads the source's formulas: pass formulas.json")
        return cls(layer, paragraphs, formulas)

    # -- formulas ----------------------------------------------------------------------

    def formula_at(self, offset: int) -> tuple[int, int, str] | None:
        """The formula whose layer span holds ``offset``, else None."""
        i = bisect_right(self._formula_starts, offset) - 1
        if i >= 0 and self.formulas[i][0] <= offset < self.formulas[i][1]:
            return self.formulas[i]
        return None

    def formulas_in(self, start: int, end: int) -> list[tuple[int, int, str]]:
        """The formulas whose whole layer span lies in ``[start, end)``."""
        i = bisect_right(self._formula_starts, start - 1)
        out = []
        while i < len(self.formulas) and self.formulas[i][0] < end:
            if self.formulas[i][1] <= end:
                out.append(self.formulas[i])
            i += 1
        return out

    # -- the definitions ----------------------------------------------------------------

    def block_at(self, offset: int) -> Block | None:
        """block(x): the outermost block holding ``offset``, else None."""
        for block in self.top:
            if block.start > offset:
                break
            if offset < block.end:
                return block
        return None

    def next(self, block: Block, *, marks: bool = False) -> Block | None:
        """next(B): the first block no other holds that starts at or after B's end with
        only whitespace between, else None.

        With ``marks`` (the wide bound's), the text between may also hold footnote marks:
        with every ``FOOTNOTE_MARK`` removed only whitespace is left, and no formula has a
        character between the two blocks.
        """
        i = bisect_right(self._top_starts, block.end - 1)
        if i >= len(self.top):
            return None
        following = self.top[i]
        gap = self.layer[block.end:following.start]
        if not all(_is_white(ch) for ch in gap):
            if not marks or not all(_is_white(ch) for ch in FOOTNOTE_MARK.sub("", gap)) \
                    or self._formula_between(block.end, following.start):
                return None
        return following

    def _formula_between(self, start: int, end: int) -> bool:
        """Does a formula have a character in ``[start, end)``?"""
        i = max(bisect_right(self._formula_starts, start) - 1, 0)
        while i < len(self.formulas) and self.formulas[i][0] < end:
            if self.formulas[i][1] > start:
                return True
            i += 1
        return False

    def is_display(self, block: Block) -> bool:
        """A display block: it holds a formula, and its text outside its formulas holds no
        letter (a display, or a ``dd`` holding only one)."""
        inside = self.formulas_in(block.start, block.end)
        if not inside:
            return False
        cursor = block.start
        for fa, fb, _alt in inside:
            if any(ch.isalpha() for ch in self.layer[cursor:fa]):
                return False
            cursor = fb
        return not any(ch.isalpha() for ch in self.layer[cursor:block.end])

    def last_character(self, start: int, end: int) -> str:
        """The last non-space character of ``[start, end)``, a trailing formula read as
        :func:`formula_stop` of its alttext; '' when there is none."""
        return self._last(start, end)[0]

    def _last(self, start: int, end: int) -> tuple[str, bool]:
        """:meth:`last_character`, and whether it is a full stop."""
        i = end - 1
        closing = ""
        while i >= start:
            if _is_white(self.layer[i]):
                i -= 1
                continue
            formula = self.formula_at(i)
            if formula is None:
                ch = self.layer[i]
                if ch in CLOSING_MARKS:
                    closing = closing or ch
                    i -= 1
                    continue
                stops = ch in SENTENCE_STOPS
            else:
                ch = formula_stop(formula[2])
                if not ch:
                    i = formula[0] - 1
                    continue
                stops = ch in FORMULA_STOPS
            return (ch, True) if stops else (closing or ch, False)
        return closing, False

    def introduces(self, block: Block) -> bool:
        """introduces(B): B's last non-space character is no full stop, so its last
        sentence runs on into what follows."""
        last, stops = self._last(block.start, block.end)
        return bool(last) and not stops

    def ends_with_display(self, block: Block) -> bool:
        """ends_with_display(D): D is a display block, or a paragraph or ``dd`` whose last
        content is a display formula.

        A container (a ``blockquote``, a theorem box, a list item) that ends with a
        display does not, nor one that holds only a display: it is a unit, and its
        sentence never runs on out of it.
        """
        if not set(block.kinds) <= CONTINUED_FROM:
            return False
        if self.is_display(block):
            return True
        i = bisect_right(self._math_starts, block.end - 1) - 1
        if i < 0:
            return False
        shown = self._math[i]
        if shown.start < block.start or shown.end > block.end or shown.span == block.span:
            return False
        return all(_is_white(ch) for ch in self.layer[shown.end:block.end])

    def continues(self, block: Block) -> bool:
        """continues(N): N's first word goes on with a sentence rather than starting one.

        The first word is the one at N's first non-space character, read past the opening
        marks (never ``[``, a footnote mark's), with a trailing ``,``, ``;`` or ``:`` set
        aside. It continues when it is one of ``ABBREVIATIONS`` as written, lower-case, or
        when its leading run of letters starts with a lower-case letter and is two letters
        or more, or the word "a". A lone lower-case letter is a variable written as text,
        and a digit, an upper-case letter or a formula starts a new sentence; nor does an
        item label continue. A name particle written as text ("von Neumann") reads as a
        continuation: a known false positive.
        """
        layer = self.layer
        i = block.start
        while i < block.end and _is_white(layer[i]):
            i += 1
        while i < block.end and layer[i] in OPENING_MARKS and self.formula_at(i) is None:
            i += 1
        if i >= block.end or self.formula_at(i) is not None:
            return False
        j = i
        while j < block.end and not _is_white(layer[j]) and self.formula_at(j) is None:
            j += 1
        word = layer[i:j]
        if word.rstrip(",;:") in ABBREVIATIONS:
            return True
        if _ITEM_LABEL.match(word):
            return False
        letters = _LETTERS.match(word).group(0)
        return letters[:1].islower() and (len(letters) >= 2 or letters == "a")

    def continuation(self, block: Block) -> Block | None:
        """continuation(D): the paragraph that finishes D's last sentence after its
        display, else None."""
        if "li" in block.kinds or not self.ends_with_display(block) \
                or not self.introduces(block):
            return None
        following = self.next(block)
        if following is None or set(following.kinds) != {CONTINUATION_BLOCK}:
            return None
        return following if self.continues(following) else None

    def opens_with_formula(self, block: Block, before: Block) -> bool:
        """opens_with_formula(N, before): N goes on with the sentence ``before`` left open,
        with a formula as its first word. Only the wide bound reads it."""
        if self.is_display(block):
            return False
        layer = self.layer
        i = block.start
        while i < block.end and _is_white(layer[i]):
            i += 1
        if i >= block.end or self.formula_at(i) is None:
            return False
        while i < block.end:
            formula = self.formula_at(i)
            if formula is not None:
                i = formula[1]
            elif _is_white(layer[i]) or layer[i] in ",;:":
                i += 1
            else:
                break
        if i >= block.end:
            return False
        j = i
        while j < block.end and not _is_white(layer[j]) and self.formula_at(j) is None:
            j += 1
        word = layer[i:j]
        if _ITEM_LABEL.match(word):
            return False
        letters = _LETTERS.match(word).group(0)
        if not (letters[:1].islower() and (len(letters) >= 2 or letters == "a")):
            return False
        k = before.end - 1
        while k >= before.start and _is_white(layer[k]):
            k -= 1
        return k >= before.start and self.formula_at(k) is None

    def finishes(self, block: Block, before: Block) -> bool:
        """finishes(N, before): N's first word goes on with ``before``'s open sentence."""
        if self.continues(block):
            return True
        return set(before.kinds) <= CONTINUED_FROM and self.opens_with_formula(block, before)

    def runs_on(self, block: Block, *, wide: bool = False) -> list[Block]:
        """The looser continuation gate rule 10 reads: the blocks D's last sentence runs on
        over to the paragraph that finishes it, else ``[]``.

        D is no list item and no theorem box (each a unit) and introduces what follows;
        then any ``dd`` or display blocks, each introducing what follows in turn; and then
        a plain paragraph that :meth:`continues`. It needs no display the gate can see, so
        it holds every :meth:`continuation` and more: a bound that only ever admits a
        longer ``until``. The default end never reads it.

        With ``wide``, the walk reads :meth:`next` across footnote marks, a plain
        paragraph ends the run when it :meth:`finishes` the sentence, and a ``dd`` that is
        no display block also ends it, included, when the block before is a paragraph, a
        ``dd`` or a display and the ``dd`` finishes it.
        """
        out: list[Block] = []
        last = block
        head = set(block.kinds) <= CONTINUED_FROM
        while True:
            if "li" in last.kinds or last.theorem_box or not self.introduces(last):
                return []
            following = self.next(last, marks=wide)
            if following is None:
                return []
            if set(following.kinds) == {CONTINUATION_BLOCK}:
                done = self.continues(following) or (
                    wide and head and self.finishes(following, last))
                return out + [following] if done else []
            if "li" in following.kinds or not set(following.kinds) <= {"dd", BLOCK_MATH}:
                return []
            if wide and head and "dd" in following.kinds and not self.is_display(following) \
                    and set(last.kinds) <= CONTINUED_FROM and self.finishes(following, last):
                return out + [following]
            out.append(following)
            last = following

    def chain(self, block: Block, *, continued: bool = False,
              loose: bool = False, wide: bool = False) -> list[Block]:
        """chain(B): B, then the display blocks its last sentence introduces.

        A list item is a unit of its own, never a display a sentence introduces, so the
        chain stops at one. A ``dd`` is how an article indents a display, and joins.
        ``continued`` joins the paragraph that finishes a sentence after its display;
        ``loose`` joins what :meth:`runs_on` names instead, and implies ``continued``;
        ``wide`` implies ``loose`` and reads across footnote marks.
        """
        loose = loose or wide
        out = [block]
        while True:
            while self.introduces(out[-1]):
                following = self.next(out[-1], marks=wide)
                if following is None or "li" in following.kinds \
                        or not self.is_display(following):
                    break
                out.append(following)
            if not (continued or loose) or "li" in block.kinds:
                return out
            if loose:
                tail = self.runs_on(out[-1], wide=wide)
                if not tail:
                    return out
                out.extend(tail)
                continue
            tail = self.continuation(out[-1])
            if tail is None:
                return out
            out.append(tail)

    def list_run(self, block: Block) -> list[Block]:
        """list run(B): an ``li`` or ``dd`` and the next() blocks of its kind after it."""
        kind = block.list_kind
        out = [block]
        if kind is None:
            return out
        while True:
            following = self.next(out[-1])
            if following is None or kind not in following.kinds:
                return out
            out.append(following)

    def extent(self, start: int, *, compound: bool = False, continued: bool = False,
               loose: bool = False, wide: bool = False) -> int | None:
        """extent(s): the furthest a passage that starts at ``start`` may run.

        None when no block holds the start; a theorem box's end when the start's block is
        one; else the end of the last block of chain(L), L the last item of the start's
        list run when ``compound``, and the start's own block otherwise.
        """
        block = self.block_at(start)
        if block is None:
            return None
        if block.theorem_box:
            return block.end
        last = self.list_run(block)[-1] if compound and block.list_kind else block
        return self.chain(last, continued=continued, loose=loose, wide=wide)[-1].end

    def continuations(self) -> list[tuple[Block, Block]]:
        """``[(D, N)]``: every block no other holds whose :meth:`continuation` is N."""
        out = []
        for block in self.top:
            tail = self.continuation(block)
            if tail is not None:
                out.append((block, tail))
        return out

    def loose_tails(self) -> list[tuple[list[Block], Block]]:
        """``[(head, N)]``: every paragraph the loose bound alone reaches."""
        strict = {tail.span for _block, tail in self.continuations()}
        seen: set[tuple[int, int]] = set()
        out = []
        for block in self.top:
            run = self.runs_on(block)
            if not run or run[-1].span in strict or run[-1].span in seen:
                continue
            seen.add(run[-1].span)
            out.append(([block] + run[:-1], run[-1]))
        return out

    def wide_tails(self) -> list[tuple[list[Block], Block]]:
        """``[(head, N)]``: every block the wide chain reaches and the loose chain does not."""
        reached = {b.span for block in self.top for b in self.chain(block, loose=True)[1:]}
        seen: set[tuple[int, int]] = set()
        out = []
        for block in self.top:
            run = self.chain(block, wide=True)
            for k in range(1, len(run)):
                tail = run[k]
                if tail.span in reached or tail.span in seen:
                    continue
                seen.add(tail.span)
                out.append((run[:k], tail))
        return out

    def mid_sentence(self, start: int) -> bool:
        """Does a passage that starts at ``start`` start inside a sentence?

        What stands between ``start`` and the sentence before it — whitespace, a comma or
        semicolon, and the reference superscripts a cited sentence ends with
        ("…are composite.[3] A gadget…") — is set aside, so the character read is the
        sentence's own last one.
        """
        block = self.block_at(start)
        if block is None:
            return False
        layer = self.layer
        first = block.start
        while first < block.end and _is_white(layer[first]):
            first += 1
        if start <= first:
            return False
        i = start
        while True:
            at = i
            while i > block.start and self.formula_at(i - 1) is None and (
                    _is_white(layer[i - 1]) or layer[i - 1] in SEPARATOR_MARKS):
                i -= 1
            i = self._before_marks(block.start, i)
            if i == at:
                break
        last, stops = self._last(block.start, i)
        return bool(last) and not stops

    def _before_marks(self, start: int, at: int) -> int:
        """``at`` with the footnote marks that end there set aside.

        A ``[3]`` is no character of the sentence it follows: left in, the sentence's
        full stop is hidden behind it and every passage that starts at the next sentence
        reads as a start inside one.
        """
        layer = self.layer
        while at > start and layer[at - 1] == "]" and self.formula_at(at - 1) is None:
            opener = layer.rfind("[", start, at)
            if opener < 0 or self.formula_at(opener) is not None \
                    or not FOOTNOTE_MARK.fullmatch(layer[opener:at]):
                break
            at = opener
        return at

    def _after_marks(self, at: int, end: int) -> int:
        """``at`` with the footnote marks that start there set aside; :meth:`_before_marks`
        read forwards, for the sentence boundary :meth:`units` looks for."""
        layer = self.layer
        while at < end and layer[at] == "[" and self.formula_at(at) is None:
            found = FOOTNOTE_MARK.match(layer, at, end)
            if found is None:
                break
            at = found.end()
        return at

    def sentence_at(self, offset: int) -> tuple[int, int] | None:
        """The sentence of ``offset``'s block that holds it, as ``(start, end)``.

        :meth:`units` read over the whole block, so the cut's check and the receipt's
        counts have one definition of a sentence between them. None where ``offset`` is
        in no block, or in the whitespace between two sentences.
        """
        block = self.block_at(offset)
        if block is None:
            return None
        found = self._sentences.get(block.span)
        if found is None:
            found = [(int(a), int(b))
                     for a, b in self.units(list(block.span))["sentences"]]
            self._sentences[block.span] = found
        for a, b in found:
            if a <= offset < b:
                return a, b
        return None

    def allowed_cut(self, start: int) -> str | None:
        """Which rule of the cut starts a passage at ``start``, inside a sentence.

        The inventory's own rules, in the words of ``prompts/inventory-worker.md.tmpl``
        ("Inside a sentence", "Displays", "Lists"): a piece after one of
        :data:`CUT_CONNECTIVES`, the fact after the reason its sentence opens with, a
        display's second and later formulas (R-SPLIT; a formula inside a paragraph is no
        display and starts no passage), and a list item. None when no rule does — which
        is :meth:`cuts_a_sentence`.

        What it cannot read is which side of a cut is a clause of its own, so a
        ``", and"`` is taken at its word. What it refuses is a bare ``and`` or ``or``, a
        list comma, and the step from a hypothesis to its conclusion: none of the three
        is a cut of the rules, and each leaves a fragment that states nothing.
        """
        block = self.block_at(start)
        if block is None:
            return None
        if self.formula_starts_at(start) and self.is_display(block):
            return CUT_FORMULA
        if _ITEM_LABEL.match(self._word_from(start, block.end)):
            return CUT_ITEM
        if self._hypothesis_sentence(start):
            return None
        layer = self.layer
        j = start
        while j > block.start and _is_white(layer[j - 1]) and self.formula_at(j - 1) is None:
            j -= 1
        if self._after_reason(block, start, j):
            return CUT_REASON
        # A connective is often written with a comma after it ("; moreover, W is round"),
        # so the marks between it and the start are set aside one at a time, as
        # :meth:`mid_sentence` sets them aside. A list comma survives this: what stands
        # before it is the list's last word and no connective.
        k = j
        while True:
            if k > block.start and layer[k - 1] == ";" and self.formula_at(k - 1) is None:
                return "the connective ';'"
            found = self._connective_before(block.start, k)
            if found is not None:
                return f"the connective {found[1]!r}"
            if not (k > block.start and layer[k - 1] in SEPARATOR_MARKS
                    and self.formula_at(k - 1) is None):
                return None
            k -= 1
            while k > block.start and _is_white(layer[k - 1]) \
                    and self.formula_at(k - 1) is None:
                k -= 1

    def cuts_a_sentence(self, start: int) -> bool:
        """Does a passage that starts at ``start`` start inside a sentence at no cut of
        the rules? Gate rule 10's sentence check, and the audit command's."""
        return self.mid_sentence(start) and self.allowed_cut(start) is None

    def _hypothesis_sentence(self, start: int) -> bool:
        """Does ``start``'s sentence open with a hypothesis? Then no word of it is a cut:
        a conclusion is never cut from the hypothesis that governs it, nor from the
        other conclusions it governs.
        """
        sentence = self.sentence_at(start)
        if sentence is None:
            return False
        return self._word_from(sentence[0], start).rstrip(",;:").lower() \
            in HYPOTHESIS_OPENERS

    def _word_from(self, at: int, end: int) -> str:
        """The word at ``at``, its opening marks set aside; '' where a formula is."""
        layer = self.layer
        i = at
        while i < end and layer[i] in OPENING_MARKS and self.formula_at(i) is None:
            i += 1
        if i >= end or self.formula_at(i) is not None:
            return ""
        j = i
        while j < end and not _is_white(layer[j]) and self.formula_at(j) is None:
            j += 1
        return layer[i:j]

    def _after_reason(self, block: Block, start: int, j: int) -> bool:
        """Is ``start`` (whose words begin at ``j`` less its whitespace) the fact after
        the reason its sentence opens with?

        The reason is the sentence's **first** clause, so the comma ``start`` follows is
        the first one of the sentence: a later comma is a list's or an aside's, and no
        cut of the rules stands there.
        """
        layer = self.layer
        if not (j > block.start and layer[j - 1] == "," and self.formula_at(j - 1) is None):
            return False
        sentence = self.sentence_at(start)
        if sentence is None:
            return False
        a = sentence[0]
        if self._word_from(a, j).rstrip(",;:").lower() not in REASON_OPENERS:
            return False
        return not any(layer[k] == "," and self.formula_at(k) is None
                       for k in range(a, j - 1))

    def separator_start(self, start: int, at: int) -> int:
        """Where the separator before ``at`` (the next passage's start) begins.

        Back from ``at``, and again while anything is set aside: whitespace, one
        connective of ``CUT_CONNECTIVES`` as a whole word, and a comma or semicolon —
        never a character inside a formula and never back to ``start``. So the span of the
        passage before is what an ``until`` at its last word gives, whatever the next
        passage is.
        """
        layer = self.layer
        i = at
        while i > start:
            j = i
            while j > start and _is_white(layer[j - 1]) and self.formula_at(j - 1) is None:
                j -= 1
            j = self._before_connective(start, j)
            if j == i:
                break
            i = j
        return i

    def _before_connective(self, start: int, j: int) -> int:
        """``j`` less one connective word or one separating mark ending at it."""
        layer = self.layer
        if j - 1 > start and layer[j - 1] in SEPARATOR_MARKS and self.formula_at(j - 1) is None:
            return j - 1
        found = self._connective_before(start, j)
        return j if found is None else found[0]

    def _connective_before(self, start: int, j: int) -> tuple[int, str] | None:
        """``(offset, connective)`` of the one word of :data:`CUT_CONNECTIVES` that ends
        at ``j`` and stands after ``start``, else None. ``";"`` is no word: it is a
        separating mark, read as one wherever this is called.
        """
        layer = self.layer
        for connective in CUT_CONNECTIVES:
            word = connective.lstrip(",; ")
            if not word:
                continue
            k = j - len(word)
            if k <= start or layer[k:j].lower() != word or self.formula_at(k) is not None \
                    or self.formula_at(j - 1) is not None:
                continue
            before = layer[k - 1]
            if not (_is_white(before) or before in SEPARATOR_MARKS):
                continue
            if connective.startswith(","):
                # ", and": the word counts only after its comma
                m = k
                while m > start and _is_white(layer[m - 1]):
                    m -= 1
                if not (m > start and layer[m - 1] == ","):
                    continue
            return k, connective
        return None

    def cut_points(self, span: Sequence[int]) -> list[tuple[int, str]]:
        """``[(offset, connective)]``: where a connective stands strictly inside the span.

        Nothing inside a formula counts, nor one at the span's start, nor one after which
        no word of the span follows. It is an upper bound on where the cut rules cut.
        """
        a, b = int(span[0]), int(span[1])
        layer = self.layer
        words = [c for c in CUT_CONNECTIVES if c.lstrip(",; ") and not c.startswith(",")]
        points: list[tuple[int, str]] = []
        i = a + 1
        while i < b:
            formula = self.formula_at(i)
            if formula is not None:
                i = max(formula[1], i + 1)
                continue
            ch = layer[i]
            if ch == ";":
                if self._word_follows(i + 1, b):
                    points.append((i, ";"))
            elif ch == ",":
                m = i + 1
                while m < b and _is_white(layer[m]) and self.formula_at(m) is None:
                    m += 1
                if self._whole_word(m, "and", b) and self._word_follows(m + 3, b):
                    points.append((i, ", and"))
            elif _is_white(layer[i - 1]) or layer[i - 1] in SEPARATOR_MARKS:
                for word in words:
                    if self._whole_word(i, word, b) and self._word_follows(i + len(word), b):
                        points.append((i, word))
                        break
            i += 1
        return points

    def _whole_word(self, at: int, word: str, end: int) -> bool:
        """Is ``word`` at ``at`` (case aside), outside every formula, with whitespace or a
        comma or semicolon after it before ``end``?"""
        after = at + len(word)
        if after >= end or self.layer[at:after].lower() != word:
            return False
        if any(self.formula_at(k) is not None for k in range(at, after)):
            return False
        return _is_white(self.layer[after]) or self.layer[after] in SEPARATOR_MARKS

    def _word_follows(self, at: int, end: int) -> bool:
        """Does a word of ``[at, end)`` follow ``at``, across whitespace and separators?"""
        while at < end and self.formula_at(at) is None and (
                _is_white(self.layer[at]) or self.layer[at] in SEPARATOR_MARKS):
            at += 1
        return at < end

    # -- units: the receipt's counts ------------------------------------------------------

    def units(self, span: Sequence[int]) -> dict[str, list[list[int]]]:
        """The sentences and displays of the passage at ``span``: :func:`units`."""
        start, end = int(span[0]), int(span[1])
        layer = self.layer
        stops: list[int] = []          # offsets just past a sentence's last character
        i = start
        while i < end:
            formula = self.formula_at(i)
            if formula is not None:
                if formula_stop(formula[2]) in FORMULA_STOPS and formula[1] <= end:
                    stops.append(formula[1])
                i = max(formula[1], i + 1)
                continue
            if layer[i] in SENTENCE_STOPS and not _abbreviation(layer, start, i):
                stops.append(i + 1)
            i += 1
        sentences: list[list[int]] = []
        cursor = start
        block_starts = {block.start for block in self.blocks}
        for stop in stops:
            # a cited sentence ends before its reference superscripts, not after them
            j = self._after_marks(stop, end)
            if j >= end:
                continue
            if _is_white(layer[j]):
                while j < end and _is_white(layer[j]):
                    j += 1
                if j >= end:
                    continue
            elif j not in block_starts:
                # glued to the next word: no sentence ends here, unless a block does
                continue
            if not (layer[j].isupper() or self.formula_starts_at(j)):
                continue
            sentences.append([cursor, stop])
            cursor = j
        tail = end
        while tail > cursor and _is_white(layer[tail - 1]):
            tail -= 1
        if tail > cursor:
            sentences.append([cursor, tail])
        displays = [list(block.span) for block in self.blocks
                    if BLOCK_MATH in block.kinds and start <= block.start and block.end <= end]
        return {"sentences": sentences, "displays": displays}

    def formula_starts_at(self, offset: int) -> bool:
        i = bisect_right(self._formula_starts, offset) - 1
        return i >= 0 and self.formulas[i][0] == offset


def _is_blocks(value: object) -> bool:
    """Is ``value`` a :class:`Blocks`, of this module or of its twin under another name?"""
    return getattr(type(value), "_locate_text_blocks", False) is True


def _abbreviation(layer: str, start: int, stop: int) -> bool:
    """Is the full stop at ``stop`` the end of an abbreviation?"""
    i = stop
    while i > start and not _is_white(layer[i - 1]):
        i -= 1
    return layer[i:stop + 1].lstrip("([{'\"").lower() in ABBREVIATIONS


def extent(layer: str, paragraphs: "Sequence[dict] | Blocks", formulas: Sequence[dict] | None,
           start: int, *, compound: bool = False, continued: bool = False,
           loose: bool = False, wide: bool = False) -> int | None:
    """:meth:`Blocks.extent` of one source: what gate rule 10 holds an end to."""
    return Blocks.of(layer, paragraphs, formulas).extent(start, compound=compound,
                                                         continued=continued, loose=loose,
                                                         wide=wide)


def units(layer: str, paragraphs: "Sequence[dict] | Blocks", formulas: Sequence[dict] | None,
          span: Sequence[int]) -> dict[str, list[list[int]]]:
    """``{"sentences": [[a, b], …], "displays": [[a, b], …]}`` of the passage at ``span``."""
    return Blocks.of(layer, paragraphs, formulas).units(span)
