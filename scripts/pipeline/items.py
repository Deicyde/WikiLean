#!/usr/bin/env python3
"""The passage item grammar: the one normaliser pair the pipeline keys on.

docs/PIPELINE.md section 3 (stage 8). A WikiLean passage is named ``In-text #<hex>``
and nothing else: the hex is the head rule's hash of the passage's first words, so the
label already *is* the identity. Two normalisers sit on it, and they are not
interchangeable:

* :func:`clean` is the **ID transformation**. It folds whitespace and the dash
  spellings, strips wrapping backticks and trailing punctuation, and is what
  ``location_key`` and ``finding_id`` hash. It is frozen: changing it re-keys every
  location and every vote in ``atlas_feedback``.
* :func:`join_key` is the **key two generations join on**. For a passage it is the
  canonical spelling, ``In-text #<hex>``, so ``In-text#3fa91c07`` and
  ``In-text  #3FA91C07`` join as one passage and hash as two.

Anything that MATCHES on an item matches on :func:`join_key`; anything that WRITES or
HASHES a label leaves it as the document wrote it. A label the grammar refuses keeps
itself as its own key and is reported — never guessed onto another passage.

Ported from ``cflib/items.py``'s two normalisers and its ``In-text #<hex>`` grammar,
narrowed to the one kind of item a WikiLean record set carries. The numbered-item
grammar (``Exercise 1.3.19``), which belongs to a book, does not move.
"""
from __future__ import annotations

import re
import unicodedata

#: The canonical prefix of a passage label.
PASSAGE_PREFIX = "In-text #"
#: How many hex digits a passage hash has.
PASSAGE_HEX_MIN = 8
PASSAGE_HEX_MAX = 16

#: The dash spellings a source may use, folded to ASCII before anything is hashed.
_DASHES = {"‐": "-", "‑": "-", "‒": "-", "–": "-",
           "—": "-", "―": "-", "−": "-"}
#: ``In-text``, ``in text``, ``Intext`` — the prefix spellings the grammar accepts.
_IN_TEXT = re.compile(r"^in[-\s]?text\s*#\s*([0-9A-Fa-f]+)\Z", re.I)
_HEX = re.compile(r"^[0-9a-f]+\Z")


class ItemProblem(ValueError):
    """A label the passage grammar refuses rather than guess onto an item."""


def clean(raw: str) -> str:
    """NFKC, dash and whitespace folding; strips wrapping backticks and trailing marks.

    The ID transformation, frozen. ``In-text\\t#3fa91c07`` and ``In-text #3fa91c07.``
    both become ``In-text #3fa91c07``; the case of the hex is left exactly as written,
    because this hashes and does not join.
    """
    text = unicodedata.normalize("NFKC", raw or "")
    for bad, good in _DASHES.items():
        text = text.replace(bad, good)
    text = text.replace(" ", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip("`").strip()
    return text.rstrip(" .,;:").strip()


def passage_hex(raw: str) -> str:
    """The lowercase hex of an ``In-text #<hex>`` label; raises on anything else."""
    match = _IN_TEXT.match(clean(raw))
    if match is None:
        raise ItemProblem(f"{raw!r} is not a passage label: a passage is "
                          f"{PASSAGE_PREFIX}<hex>")
    digits = match.group(1).lower()
    if not PASSAGE_HEX_MIN <= len(digits) <= PASSAGE_HEX_MAX:
        raise ItemProblem(f"{raw!r}: a passage hash is {PASSAGE_HEX_MIN} to "
                          f"{PASSAGE_HEX_MAX} hex digits, not {len(digits)}")
    return digits


def passage_item(hex_digits: str) -> str:
    """``"In-text #<hex>"`` for 8 to 16 lowercase hex digits; raises on anything else."""
    digits = str(hex_digits or "")
    if not _HEX.match(digits) or not PASSAGE_HEX_MIN <= len(digits) <= PASSAGE_HEX_MAX:
        raise ItemProblem(f"a passage hash is {PASSAGE_HEX_MIN} to {PASSAGE_HEX_MAX} "
                          f"lowercase hex digits, not {digits!r}")
    return PASSAGE_PREFIX + digits


def is_passage_item(raw: str) -> bool:
    """Whether ``raw`` parses as an ``In-text #<hex>`` passage label."""
    try:
        passage_hex(raw)
    except ItemProblem:
        return False
    return True


def join_key(raw: str) -> str:
    """The key two generations of one passage join on: its canonical spelling.

    A label the grammar refuses keeps :func:`clean` of itself as its own key, so a
    join never silently merges it onto another passage; the caller reports it.
    """
    try:
        return passage_item(passage_hex(raw))
    except ItemProblem:
        return clean(raw)


def qualified(source_id: str, item: str) -> str:
    """``"<source_id>::In-text #<hex>"`` — an item is the pair, never the bare label."""
    return f"{source_id}::{item}"
