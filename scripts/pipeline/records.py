#!/usr/bin/env python3
"""Stage 5: the Literature Links records of one passage outcome.

docs/PIPELINE.md section 3 (stage 5); the shapes are docs/ARTICLES.md section 3, which
is the format both producers implement. Three record shapes, all with
``id = <source_id>::<item>`` and the same subject end:

* a **location** record — one ``lean`` end at the declaration, ``role: key``, the
  relation and the passage's derived status; a ``closeMatch`` names its convention
  difference in ``note``;
* a checked **absence** — ``relation: noMatch``, ``status: not_formalized``, one minimal
  ``{type, uri, version}`` lean end per distinct ``(repo, commit)`` of the universe, and
  its search receipt referenced by sha256 and count, never inline;
* an **unresolved** record — ``object: []`` and no status.

Under status rules 2 and 3 every record also carries its ``passage_key``, a location
record its ``pair_key``, and a coverage claim rides as a bundle with its ``relation``,
its ``conjuncts`` and its ``bundle_key``. All three keys are **recomputed here**, never
copied from the row: a row that states a different one is refused by name.

## The cut

The one departure from the producer this was ported from. There, the record builder is
handed the article's text layer and cuts the quote out of it. Here the cut is a stage of
its own: :func:`cut_passage` makes a :class:`Cut` (the capped quote and the span's
sha256) from a layer, and :func:`passage_records` takes the ``Cut``.

The reason is that the layer is the article's text. It is not in this repository, it is
not in a sealed record set, and it never travels to a contributor's machine — so a
record can be rebuilt from a record only if the layer's two outputs travel with the row.
With the cut explicit, every other value a record carries is derived here and can be
checked against a real set byte for byte (docs/PIPELINE.md section 8, R5).
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import keys  # noqa: E402
import vocab  # noqa: E402

_SPACE = re.compile(r"\s")


class RecordProblem(ValueError):
    """A passage that cannot be written as the record format requires."""


# ---- the cut --------------------------------------------------------------------------


@dataclass(frozen=True)
class Cut:
    """What the text layer gives a passage: its capped quote and its span's sha256.

    ``quote`` is ``{exact, prefix, suffix}`` (the affixes empty when the quote is already
    unique on the layer), and ``span_sha256`` is the sha256 of the span's characters as
    they stand on the layer, trailing whitespace included.
    """

    quote: dict
    span_sha256: str

    def checked(self, item: str) -> "Cut":
        quote = self.quote
        if not isinstance(quote, Mapping) or not isinstance(quote.get("exact"), str) or not quote["exact"]:
            raise RecordProblem(f"{item}: the cut has no exact quote")
        for name in ("prefix", "suffix"):
            if not isinstance(quote.get(name, ""), str):
                raise RecordProblem(f"{item}: the cut's {name} is not a string")
        if len(quote["exact"].split()) > vocab.QUOTE_MAX_WORDS:
            raise RecordProblem(f"{item}: the quote is {len(quote['exact'].split())} words, over the "
                                f"cap of {vocab.QUOTE_MAX_WORDS}")
        for name in ("prefix", "suffix"):
            if len(quote.get(name) or "") > vocab.AFFIX_MAX_CHARS:
                raise RecordProblem(f"{item}: the quote's {name} is {len(quote[name])} characters, over "
                                    f"the cap of {vocab.AFFIX_MAX_CHARS}")
        if not isinstance(self.span_sha256, str) or not vocab.HEX64.match(self.span_sha256):
            raise RecordProblem(f"{item}: the cut's span_sha256 is not 64 lowercase hex")
        return self


def find_quote(layer: str, exact: str, prefix: str = "", suffix: str = "") -> list[int]:
    """Every layer offset at which ``exact`` starts with ``prefix``/``suffix`` around it.

    Exact character matching on the layer, overlapping occurrences included; an empty
    ``exact`` matches nowhere. The layer is the one view a quote is cut from, so nothing
    is normalised. A record's quote must occur exactly once.
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


def _words_end(layer: str, start: int, words: int) -> int:
    """The offset after the first ``words`` whitespace-separated words from ``start``."""
    i, n, count = start, len(layer), 0
    while i < n and count < words:
        while i < n and _SPACE.match(layer[i]):
            i += 1
        if i >= n:
            break
        while i < n and not _SPACE.match(layer[i]):
            i += 1
        count += 1
    return i


def _clip_left(text: str, chars: int) -> str:
    if len(text) <= chars:
        return text
    clipped = text[-chars:]
    cut = clipped.find(" ")
    return clipped[cut + 1:] if 0 <= cut < len(clipped) - 1 else clipped


def _clip_right(text: str, chars: int) -> str:
    if len(text) <= chars:
        return text
    clipped = text[:chars]
    cut = clipped.rfind(" ")
    return clipped[:cut] if cut > 0 else clipped


def cut_passage(layer: str, span: Iterable[int], *, words: int = vocab.QUOTE_MAX_WORDS,
                context: int = vocab.AFFIX_MAX_CHARS) -> Cut:
    """The :class:`Cut` of the passage at ``span`` on ``layer``.

    ``exact`` is the passage's first ``words`` words, never past the span's end,
    right-stripped; ``prefix`` and ``suffix`` are empty when ``exact`` is already unique
    on the layer and otherwise widened a character at a time, up to ``context`` each,
    until it is. A passage whose capped quote cannot be made unique is refused — never
    exported ambiguous, because a consumer locates the quote by matching it.

    ``span_sha256`` hashes the span's characters as they stand, the trailing whitespace
    the quote was stripped of included: that is how a consumer tells a capped head from a
    whole passage.
    """
    start, end = _span(span, "the cut")
    if not 0 <= start < len(layer):
        raise RecordProblem(f"the passage span [{start}, {end}] starts outside the layer "
                            f"(0..{len(layer)})")
    if end > len(layer):
        raise RecordProblem(f"the passage span [{start}, {end}] leaves the layer (0..{len(layer)})")
    span_sha = keys.sha256_text(layer[start:end])
    stop = min(_words_end(layer, start, words), max(end, start + 1))
    exact = layer[start:stop].rstrip()
    if not exact:
        raise RecordProblem(f"the passage at {start} has no text to quote")
    if find_quote(layer, exact) == [start]:
        return Cut({"exact": exact, "prefix": "", "suffix": ""}, span_sha)
    after = start + len(exact)
    for width in range(1, context + 1):
        prefix = _clip_left(layer[max(0, start - width):start], width)
        suffix = _clip_right(layer[after:after + width], width)
        for pre, suf in ((prefix, ""), ("", suffix), (prefix, suffix)):
            if find_quote(layer, exact, pre, suf) == [start]:
                return Cut({"exact": exact, "prefix": pre, "suffix": suf}, span_sha)
    raise RecordProblem(f"the quote at {start} ({exact[:40]!r}) occurs more than once on the layer "
                        f"even with {context} characters of context each side")


# ---- free text ------------------------------------------------------------------------


def verbatim_run(text: str, layer: str, words: int = vocab.QUOTE_MAX_WORDS) -> str | None:
    """The first run of more than ``words`` consecutive words of ``text`` that the layer
    holds, whitespace collapsed — source text a free-text field would carry past the
    quote cap — or None."""
    tokens = str(text or "").split()
    if len(tokens) <= words:
        return None
    flat = " ".join(layer.split())
    for i in range(len(tokens) - words):
        run = " ".join(tokens[i:i + words + 1])
        if run in flat:
            return run
    return None


def check_free_text(item: str, what: str, text: Any, layer: str) -> None:
    """Refuse a note, a query or a conjunct that carries the source past the quote cap.

    A record quotes its passage to the cap and no further; a free-text field that holds
    more of the article than the quote does is the cap gone round the back. Checked only
    when the caller has the layer: a re-export from a sealed set does not.
    """
    if not layer:
        return
    run = verbatim_run(str(text or ""), layer)
    if run is not None:
        raise RecordProblem(f"{item}: {what} carries {len(run.split())}+ words of the source "
                            f"verbatim ({run[:40]!r}...), past the {vocab.QUOTE_MAX_WORDS}-word quote cap")


# ---- the note a downgrade leaves --------------------------------------------------------

#: How much of a witness one sentence of a downgrade note may carry.
NOTE_FROM_WITNESS_CHARS = 300
_SENTENCE_END = re.compile(r"(?<=[.!?])\s")
#: Abbreviations whose full stop ends no sentence of a witness ("i.e. F(x) = …").
_ABBREVIATIONS = frozenset({"i.e.", "e.g.", "cf.", "resp.", "etc.", "viz.", "vs.", "w.r.t."})
#: The mark of a challenger's recall miss: the last sentence of its witness on a general
#: card names a declaration the passage lacks, which is never the convention a record
#: differs by, so it is never taken as a note.
SPECIFIC_MISS_MARK = "R-SPECIFIC miss:"


def is_specific_miss(sentence: str) -> bool:
    """Does a witness sentence carry the recall-miss mark, in any case?"""
    return SPECIFIC_MISS_MARK.casefold() in str(sentence).casefold()


def witness_sentences(witness: str) -> list[str]:
    """A witness's sentences, whitespace collapsed, each cut at a word at the cap.

    A sentence ends at ``.``, ``!`` or ``?`` before whitespace, except after an
    abbreviation: "— i.e. F(x) = …" is one sentence, not two.
    """
    text = " ".join(str(witness or "").split())
    sentences: list[str] = []
    start = 0
    for brk in _SENTENCE_END.finditer(text):
        last = text[start:brk.start()].rsplit(" ", 1)[-1].lstrip("([{'\"")
        if last.lower() in _ABBREVIATIONS:
            continue
        sentences.append(text[start:brk.start()])
        start = brk.end()
    if text[start:]:
        sentences.append(text[start:])
    out = []
    for sentence in sentences:
        if len(sentence) > NOTE_FROM_WITNESS_CHARS:
            cut = sentence.rfind(" ", 0, NOTE_FROM_WITNESS_CHARS)
            sentence = (sentence[:cut if cut > 0 else NOTE_FROM_WITNESS_CHARS].rstrip(" ,;:")
                        + "…")
        out.append(sentence)
    return out


def downgrade_note(verdict: Mapping[str, Any], layer: str | None = None) -> str:
    """The note of a location a challenge downgraded to ``closeMatch``.

    A ``closeMatch`` names its convention difference in ``note``, and a downgrade from
    a stronger relation is exactly the challenger saying there is one: the verdict's
    own ``note`` when it gives one, else a sentence of its ``witness`` — the first that
    carries no more of the source verbatim than the quote cap allows
    (:func:`verbatim_run` against the passage's layer). A witness opens by quoting the
    passage as often as not, and a note is exported, so such a sentence is passed over.
    Without a layer, or when every sentence left quotes past the cap, the first of them,
    which :func:`check_free_text` then refuses by name.

    It lives here, beside the free-text check, so the gate computes the very note the
    export would write and refuses the same one.
    """
    note = " ".join(str(verdict.get("note") or "").split())
    if note:
        return note
    sentences = [s for s in witness_sentences(str(verdict.get("witness") or ""))
                 if not is_specific_miss(s)]
    if not sentences:
        return ""
    if layer:
        for sentence in sentences:
            if verbatim_run(sentence, layer) is None:
                return sentence
    return sentences[0]


# ---- the ends ---------------------------------------------------------------------------


def _span(value: Any, item: str) -> list[int]:
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or not all(type(offset) is int for offset in value)):
        raise RecordProblem(f"{item}: no [start, end] layer span ({value!r})")
    start, end = value
    if not 0 <= start < end:
        raise RecordProblem(f"{item}: the span [{start}, {end}] is not 0 <= start < end")
    return [start, end]


def article_qid(source: Mapping[str, Any], row: Mapping[str, Any], *, challenge: str) -> str | None:
    """The article's QID one record of a passage carries, or None (decision B5).

    Only a ``subject`` passage — its prominence after the challenge — carries its
    source's own QID as the subject end's ``identifiers.wikidata``: the article's QID on
    every passage would weld the wrong concepts, since a theorem the article merely
    mentions would join the article's concept.

    A ``challenge_pending`` record carries none. It is a record a prominence overturn to
    ``subject`` reopened, which no challenger judged under that prominence's rule, so the
    QID would rest on a location nobody checked. The rule is per record, not per passage:
    the passage's other records carry it. A record of a run sealed with no challenge at
    all (``tally.py --unchallenged``, every record ``not_required``) does carry it: the
    locate worker's subject call stands without a challenger (Jack, 2026-11-13, "Carry
    the QID"). A source with no QID gives None; one whose ``wikidata`` is not a QID is
    refused, never guessed.
    """
    if row.get("prominence") != vocab.PROMINENCE_SUBJECT:
        return None
    qid = source.get("wikidata")
    if qid is None:
        return None
    if not isinstance(qid, str) or not vocab.QID_RE.match(qid):
        raise RecordProblem(f"{source.get('source_id')}: wikidata {qid!r} is not a Wikidata QID, "
                            "and a subject passage's records carry it")
    return None if challenge == vocab.CHALLENGE_PENDING else qid


def subject_end(source: Mapping[str, Any], cut: Cut, span: list[int], *,
                wikidata: str | None = None) -> dict:
    """The ``html`` end: the revision's permalink, the capped quote, the citation, the licence.

    Its locate extension carries the passage's ``span`` on the layer whose sha is the
    source's ``layer_sha256`` — offsets only, never the text — beside ``span_sha256``,
    the sha of those characters, so a consumer holding that layer can highlight the whole
    passage and not only its quote. A span is offsets on one layer, so a source with no
    layer sha is refused.
    """
    layer_sha = str(source.get("layer_sha256") or "")
    if not vocab.HEX64.match(layer_sha):
        raise RecordProblem(f"{source.get('source_id')}: no layer_sha256 for the subject end's span")
    uri = str(source["uri"])
    citation = {"id": str(source["source_id"]), "type": "webpage",
                "title": str(source.get("title") or source["source_id"]), "URL": uri}
    if source.get("container_title"):
        citation["container-title"] = str(source["container_title"])
    quote = {"exact": cut.quote["exact"]}
    for name in ("prefix", "suffix"):
        if cut.quote.get(name):
            quote[name] = cut.quote[name]
    ext = {"pageid": int(source["pageid"]), "html_sha256": str(source["html_sha256"]),
           "layer_sha256": layer_sha, "renderer": str(source["renderer"]),
           "span": [int(span[0]), int(span[1])], "span_sha256": cut.span_sha256,
           "licence": str(source.get("licence") or "")}
    end = {"type": "html", "uri": uri, "version": str(source["revision"]),
           "location": {"quote": quote}, "citation": citation,
           "extensions": {vocab.LOCATE: ext}}
    if wikidata is not None:
        end["identifiers"] = {"wikidata": wikidata}
    return end


def _commit(library: Mapping[str, Any]) -> str:
    commit = str(library.get("commit") or "")
    if not vocab.HEX40.match(commit):
        raise RecordProblem(f"library {library.get('id')!r} has no 40-hex commit")
    return commit


def lean_end(library: Mapping[str, Any], location: Mapping[str, Any] | None = None) -> dict:
    """A ``lean`` end: at a declaration (``role: key``), or the bare library (an absence).

    An absence's end is ``{type, uri, version}`` and nothing else — the record names each
    repository the universe searched, and the universe file already says which libraries
    each repository is.
    """
    end = {"type": "lean", "uri": str(library["repo"]), "version": _commit(library)}
    if location is None:
        return end
    loc = {"declaration": str(location["declaration"])}
    if location.get("module"):
        loc["module"] = str(location["module"])
    ext = {"library": str(library["id"]), "proof_status": str(location["proof_status"])}
    for key in ("path", "line", "end_line"):
        if location.get(key) is not None:
            ext[key] = location[key]
    if location.get("statement"):
        ext["statement"] = True
    end.update(role="key", location=loc, extensions={vocab.LOCATE: ext})
    return end


def absence_ends(universe: Mapping[str, Any]) -> list[dict]:
    """The lean ends of a checked absence: one per distinct ``(repo, commit)``, by library id.

    An absence names what was searched, and two universe libraries split from one
    repository at one commit (lean core's ``Init`` and ``Std``, one root each) are one
    search target, so they are one end. The first library by id of each pair places it.
    """
    ends: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for library in sorted(universe["libraries"], key=lambda lib: str(lib["id"])):
        end = lean_end(library)
        pair = (end["uri"], end["version"])
        if pair not in seen:
            seen.add(pair)
            ends.append(end)
    return ends


# ---- the receipt ------------------------------------------------------------------------


def receipt_document(source_id: str, item: str, receipt: Mapping[str, Any],
                     universe: Mapping[str, Any], layer: str = "") -> tuple[str, str, dict]:
    """``(sha256, the sidecar's text, the record's reference)`` for an absence's receipt.

    The sidecar keeps each candidate's documented keys only, refuses a query or a reason
    that carries the source past the quote cap, and its sha256 is the sha of the exact
    bytes written — the file's name, the record's reference and the seal's entry are one
    value. The reference's counts are the lengths of the sidecar's two lists, which a
    consumer checks: a query repeated exactly is listed once, in its first place.
    """
    queries = list(dict.fromkeys(str(q) for q in receipt.get("queries") or []))
    candidates = [{k: c[k] for k in vocab.RECEIPT_CANDIDATE_KEYS if k in c}
                  for c in receipt.get("candidates") or [] if isinstance(c, dict)]
    for query in queries:
        check_free_text(item, "a receipt query", query, layer)
    for candidate in candidates:
        check_free_text(item, "a candidate's reason", candidate.get("reason"), layer)
    body = {"source_id": source_id, "item": item, "universe": str(universe["id"]),
            "universe_sha256": str(universe["sha256"]), "queries": queries,
            "candidates": candidates}
    text = keys.canonical_line(body)
    return keys.sha256_text(text), text, {"sha256": keys.sha256_text(text),
                                          "queries": len(queries), "candidates": len(candidates)}


# ---- the records ------------------------------------------------------------------------


def record_id(source_id: str, item: str) -> str:
    return f"{source_id}::{item}"


def row_challenge(row: Mapping[str, Any]) -> str:
    value = row.get("challenge")
    if not value:
        raise RecordProblem(f"{row.get('source_id')} {row.get('item')}: no challenge value "
                            "(nothing leaves the pipeline without one)")
    return str(value)


def row_problem(row: Mapping[str, Any]) -> str | None:
    """Why a row cannot be exported as the record set requires, or None.

    Every record names its passage's ``section`` (the heading path, ``[]`` in the lead)
    and ``prominence``, so a consumer can single out the article's subject passages
    without a passage table.
    """
    section = row.get("section")
    if not isinstance(section, (list, tuple)) or not all(isinstance(h, str) and h.strip()
                                                         for h in section):
        return f"{row.get('item')}: no section heading path to export ({section!r})"
    if not vocab.member(row.get("prominence"), vocab.PROMINENCES):
        return (f"{row.get('item')}: prominence {row.get('prominence')!r} is not one of "
                f"{', '.join(sorted(vocab.PROMINENCES))}")
    return None


def location_problem(location: Mapping[str, Any]) -> str | None:
    """Why a location's joined index row cannot be a lean end, or None.

    A lean end names its declaration's ``module``, its ``path`` (a safe relative ``.lean``
    path from the repository root) and its ``line`` — a consumer links the source from
    them. Only ``end_line`` is optional: a published index may record ``line`` alone, and
    a guessed end line would be a claim about code nobody read.
    """
    missing = []
    if not isinstance(location.get("module"), str) or not location["module"].strip():
        missing.append("module")
    path = location.get("path")
    parts = str(path).split("/") if isinstance(path, str) else []
    if (not isinstance(path, str) or not path.endswith(".lean") or path.startswith("/")
            or "\\" in path or any(part in ("", ".", "..") for part in parts)):
        missing.append("path (a relative .lean path)")
    line = location.get("line")
    if isinstance(line, bool) or not isinstance(line, int) or line < 1:
        missing.append("line")
    end_line = location.get("end_line")
    if end_line is not None and (isinstance(end_line, bool) or not isinstance(end_line, int)
                                 or not isinstance(line, int) or isinstance(line, bool)
                                 or end_line < line):
        missing.append("end_line (an integer at least line, or absent)")
    if not missing:
        return None
    return (f"{location.get('library')}:{location.get('declaration')} has no "
            f"{', '.join(missing)} in the index, and a lean end names its source by them")


def _record_ext(row: Mapping[str, Any], universe: Mapping[str, Any],
                coverage: Mapping[str, Any] | None = None, **extra) -> dict:
    ext = {"item": str(row["item"]), "universe": str(universe["id"]),
           "universe_sha256": str(universe["sha256"]), "challenge": row_challenge(row)}
    if isinstance(row.get("section"), (list, tuple)):
        ext["section"] = [str(heading) for heading in row["section"]]
    if row.get("prominence"):
        ext["prominence"] = str(row["prominence"])
    if coverage is not None:
        ext["coverage"] = dict(coverage)
    ext.update({k: v for k, v in extra.items() if v is not None})
    return ext


def coverage_ext(claim: Mapping[str, Any], *, status_rule: int = vocab.STATUS_RULE_D1,
                 passage: str | None = None, pairs: Mapping[str, str] | None = None,
                 item: str = "", layer: str = "") -> dict:
    """A coverage claim as a record carries it, with its keys recomputed.

    Under status rule 1: ``{complete, parts, challenge}`` — the claim's own note is never
    exported. Under rules 2 and 3 it is a bundle, which adds its ``relation`` (its
    weakest part's, which the tally computed), its ``conjuncts`` beside the parts that
    state them, and its ``bundle_key``, **recomputed** from the passage key and the
    parts' pair keys; a row that states a different one is refused.
    """
    out = {"complete": True, "parts": [str(k) for k in claim["parts"]],
           "challenge": str(claim.get("challenge"))}
    if status_rule < vocab.STATUS_RULE_BUNDLE:
        return out
    if not vocab.member(claim.get("relation"), vocab.BUNDLE_RELATIONS):
        raise RecordProblem(f"{item}: a bundle's relation is one of "
                            f"{', '.join(sorted(vocab.BUNDLE_RELATIONS))}, not {claim.get('relation')!r}")
    out["relation"] = claim["relation"]
    conjuncts = claim.get("conjuncts")
    if not isinstance(conjuncts, (list, tuple)) or not conjuncts:
        raise RecordProblem(f"{item}: a bundle on a passage with no conjuncts (a coverage claim "
                            "stands only on a compound statement)")
    out["conjuncts"] = [{"id": str(c.get("id")), "text": str(c.get("text") or ""),
                         "part": str(c.get("part"))} for c in conjuncts]
    for conjunct in out["conjuncts"]:
        check_free_text(item, f"conjunct {conjunct['id']}'s text", conjunct["text"], layer)
    computed = keys.bundle_key(passage, [pairs[part] for part in out["parts"]])
    stated = claim.get("bundle_key")
    if stated is not None and stated != computed:
        raise RecordProblem(f"{item}: the bundle states bundle_key {str(stated)[:12]}, but the "
                            "passage key and its parts' pair keys give another")
    out["bundle_key"] = computed
    return out


def location_record(source: Mapping[str, Any], row: Mapping[str, Any], location: Mapping[str, Any],
                    library: Mapping[str, Any], universe: Mapping[str, Any], cut: Cut,
                    span: list[int], *, passage_key: str | None = None,
                    pair_key: str | None = None,
                    coverage: Mapping[str, Any] | None = None) -> dict:
    """One location's record. ``passage_key``, ``pair_key`` and the bundle are rules 2 and 3's."""
    challenge = location.get("challenge")
    if not challenge:
        raise RecordProblem(f"{row.get('item')}: location {location.get('declaration')} has no "
                            "challenge value")
    record = {
        "id": record_id(str(source["source_id"]), str(row["item"])),
        "subject": [subject_end(source, cut, span,
                                wikidata=article_qid(source, row, challenge=str(challenge)))],
        "object": [lean_end(library, location)],
        "relation": str(location["relation"]),
        "kind": str(row["passage_kind"]),
        "extensions": {vocab.LOCATE: _record_ext(
            row, universe, coverage, location_key=str(location["location_key"]),
            rank=int(location["rank"]) if location.get("rank") is not None else None,
            confidence=location.get("confidence") or None, passage_key=passage_key,
            pair_key=pair_key)},
    }
    # A location's own challenge, not its passage's: a challenge judges records.
    record["extensions"][vocab.LOCATE]["challenge"] = str(challenge)
    if row.get("status"):
        record["status"] = str(row["status"])
    if location.get("note"):
        record["note"] = str(location["note"])
    return record


def absence_record(source: Mapping[str, Any], row: Mapping[str, Any],
                   universe: Mapping[str, Any], cut: Cut, span: list[int], *,
                   receipt: Mapping[str, Any], passage_key: str | None = None) -> dict:
    """The checked absence; ``receipt`` is the ``{sha256, queries, candidates}`` reference."""
    ends = absence_ends(universe)
    if not ends:
        raise RecordProblem("an absence names the universe's libraries, and it has none")
    absence = row.get("absence") or {}
    challenge = absence.get("challenge") or row.get("challenge")
    if not challenge:
        raise RecordProblem(f"{row.get('item')}: the absence has no challenge value")
    record = {
        "id": record_id(str(source["source_id"]), str(row["item"])),
        "subject": [subject_end(source, cut, span,
                                wikidata=article_qid(source, row, challenge=str(challenge)))],
        "object": ends,
        "relation": vocab.NO_MATCH,
        "status": vocab.NOT_FORMALIZED,
        "kind": str(row["passage_kind"]),
        "extensions": {vocab.LOCATE: _record_ext(row, universe, None, receipt=dict(receipt),
                                                 passage_key=passage_key)},
    }
    record["extensions"][vocab.LOCATE]["challenge"] = str(challenge)
    return record


def unresolved_record(source: Mapping[str, Any], row: Mapping[str, Any],
                      universe: Mapping[str, Any], cut: Cut, span: list[int], *,
                      passage_key: str | None = None) -> dict:
    """An unresolved outcome: ``object: []`` and no status; counted, never not formalized."""
    return {
        "id": record_id(str(source["source_id"]), str(row["item"])),
        "subject": [subject_end(source, cut, span,
                                wikidata=article_qid(source, row, challenge=row_challenge(row)))],
        "object": [],
        "kind": str(row["passage_kind"]),
        "extensions": {vocab.LOCATE: _record_ext(row, universe, None, passage_key=passage_key)},
    }


def sorted_locations(row: Mapping[str, Any]) -> list[dict]:
    """The row's locations in the order a record set lists them: rank, library, declaration."""
    return sorted(row.get("locations") or [],
                  key=lambda loc: (loc.get("rank") or 99, str(loc.get("library")),
                                   str(loc.get("declaration"))))


def passage_records(source: Mapping[str, Any], row: Mapping[str, Any], cut: Cut,
                    universe: Mapping[str, Any], *, status_rule: int = vocab.STATUS_RULE_D1,
                    layer: str = "") -> tuple[list[dict], list[tuple[str, str]]]:
    """Every record of one passage outcome, and the receipt sidecars they reference.

    ``source`` is the sourceset entry with its ``uri``, ``container_title`` and
    ``licence``; ``row`` is the tally's outcome row; ``cut`` is what the layer gave the
    passage (:func:`cut_passage`); ``universe`` is ``{id, sha256, libraries: [...]}``.
    ``layer``, when the caller has it, holds the free-text fields to the quote cap.

    Returns ``(records, [(sha256, sidecar text), ...])``.
    """
    problem = vocab.status_rule_problem(status_rule)
    if problem:
        raise RecordProblem(problem)
    item = str(row.get("item"))
    span = _span(row.get("span"), item)
    cut = cut.checked(item)
    libraries = {str(lib["id"]): lib for lib in universe["libraries"]}
    locations = sorted_locations(row)
    if len(locations) > vocab.MAX_LOCATIONS:
        raise RecordProblem(f"{item}: {len(locations)} locations, over the cap of {vocab.MAX_LOCATIONS}")
    ranks = [loc.get("rank") for loc in locations]
    if len(set(ranks)) != len(ranks):
        raise RecordProblem(f"{item}: two locations share a rank")
    keys_seen = {str(loc.get("location_key")) for loc in locations}
    if len(keys_seen) != len(locations):
        raise RecordProblem(f"{item}: two locations share a location_key")
    row_challenge(row)
    why = row_problem(row)
    if why:
        raise RecordProblem(why)
    for loc in locations:
        if str(loc.get("library")) not in libraries:
            raise RecordProblem(f"{item}: library {loc.get('library')} is not in the universe")
        if loc.get("relation") == vocab.CLOSE_MATCH and not str(loc.get("note") or "").strip():
            raise RecordProblem(f"{item}: the closeMatch {loc.get('library')}:"
                                f"{loc.get('declaration')} names no convention difference in its note")
        check_free_text(item, "a location's note", loc.get("note"), layer)
        where = location_problem(loc)
        if where:
            raise RecordProblem(f"{item}: {where}")

    # The revision-free keys, recomputed: each pair key needs its library's repository.
    passage: str | None = None
    pairs: dict[str, str] = {}
    coverage: dict | None = None
    claim = row.get("coverage")
    if status_rule >= vocab.STATUS_RULE_BUNDLE:
        passage = keys.passage_key(source.get("pageid"), cut.span_sha256)
        pairs = {str(loc["location_key"]): keys.pair_key(
            passage, str(libraries[str(loc["library"])]["repo"]), str(loc["declaration"]))
            for loc in locations}
        stated = row.get("passage_key")
        if stated is not None and stated != passage:
            raise RecordProblem(f"{item}: the row states passage_key {str(stated)[:12]}, but the "
                                "page id and the span's sha256 give another")
    if claim:
        _coverage_problem(row, claim, locations, status_rule)
        coverage = coverage_ext(claim, status_rule=status_rule, passage=passage, pairs=pairs,
                                item=item, layer=layer)

    records: list[dict] = []
    sidecars: list[tuple[str, str]] = []
    for loc in locations:
        records.append(location_record(source, row, loc, libraries[str(loc["library"])], universe,
                                       cut, span, passage_key=passage,
                                       pair_key=pairs.get(str(loc["location_key"])),
                                       coverage=coverage))
    if row.get("outcome") == "absent":
        receipt = (row.get("absence") or {}).get("receipt") or row.get("receipt") or {}
        digest, text, reference = receipt_document(str(source["source_id"]), item, receipt,
                                                   universe, layer)
        sidecars.append((digest, text))
        records.append(absence_record(source, row, universe, cut, span, receipt=reference,
                                      passage_key=passage))
    elif not locations:
        records.append(unresolved_record(source, row, universe, cut, span, passage_key=passage))
    return records, sidecars


def _coverage_problem(row: Mapping[str, Any], claim: Any, locations: list[dict],
                      status_rule: int) -> None:
    """Refuse a coverage claim that cannot ride on this passage's records."""
    item = str(row.get("item"))
    if not isinstance(claim, Mapping):
        raise RecordProblem(f"{item}: a coverage claim is an object")
    parts = claim.get("parts")
    if (not isinstance(parts, (list, tuple)) or len(parts) < vocab.MIN_COVERAGE_PARTS
            or len({str(p) for p in parts}) != len(parts)):
        raise RecordProblem(f"{item}: a coverage claim names at least {vocab.MIN_COVERAGE_PARTS} "
                            f"distinct parts, not {parts!r}")
    has_part = {str(loc.get("location_key")) for loc in locations
                if loc.get("relation") == vocab.HAS_PART}
    stray = [str(p) for p in parts if str(p) not in has_part]
    if stray:
        raise RecordProblem(f"{item}: coverage part(s) {', '.join(k[:12] for k in stray)} are not "
                            f"{vocab.HAS_PART} locations of the passage")
    challenge = claim.get("challenge")
    if not challenge:
        raise RecordProblem(f"{item}: the coverage claim has no challenge value (nothing leaves "
                            "the pipeline without one)")
    if not vocab.member(challenge, vocab.CHALLENGES):
        raise RecordProblem(f"{item}: the coverage claim's challenge {challenge!r} is not one of "
                            f"{', '.join(sorted(vocab.CHALLENGES))}")
    if status_rule < vocab.STATUS_RULE_BUNDLE and set(claim) - set(vocab.COVERAGE_KEYS):
        raise RecordProblem(f"{item}: under status rule 1 a coverage claim is exactly "
                            f"{{{', '.join(vocab.COVERAGE_KEYS)}}}")
