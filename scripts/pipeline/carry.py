#!/usr/bin/env python3
"""Stage 8: the carry-over map — what a re-cut does to each passage of the old cut.

docs/PIPELINE.md section 3 (stage 8). A re-cut of a page is a routine, versioned
operation: a verdict, a gold label or a known answer carries automatically wherever its
passage's **text** is unchanged, and only the changed passages are asked again. That is
what makes re-cutting affordable, and it is why the keys of stage 2 are revision-free.

**The map** is computed from spans and span text only — never from an item, a statement,
a model or a guess — between two inventory runs' cuts of one page, at one revision or
two:

1. every passage of both cuts is keyed (:func:`keys.passage_key` of the page and the
   sha256 of its span's text); two passages of one cut with one key refuse the page,
   since no carry could tell them apart;
2. an old passage whose key a new passage has is ``unchanged``;
3. any other is placed on the new layer: at its own span when both cuts read one layer
   (the same ``layer_sha256``), else at the one occurrence of its text in the new layer;
   a text the new layer does not hold exactly once is ``changed``;
4. with M the new passages that share a character with the placed span: none is
   ``removed``; two or more is ``split``; one that starts where it starts is
   ``end_moved``; one that starts before it and holds the whole of it is ``merged``; one
   otherwise is ``reshaped``;
5. a new passage no old passage went into is ``new``.

:func:`map_page` is that function over two :class:`Cut` values and is all a consumer's
port needs; :func:`build` is the whole document over two runs' documents and layers;
:func:`carry_rows` gives each row of a JSON-lines file keyed by ``passage_key`` its fate,
and :func:`carries` says whether what was judged on the old passage carries. The
document holds no time and no path, so a re-run writes the same bytes.

Ported from ``locate/carry.py``. What changes in the move: a cut is two values — a layer
and a document — rather than a workspace lookup, so the map can be computed by a
contributor who holds neither; and the reader takes paths, as the rest of this pipeline
does.

    python3 scripts/pipeline/carry.py (--from-run inv-1 | --from-set <dir>) \\
        (--to-run inv-2 | --to-set <dir>) --sources <dir> \\
        [--corpus <dir> --manifest <file>] [--out <file>] [--store-out <dir>] \\
        [--rows <file> --rows-out <file>]

A side is a harvested inventory run or a sealed ``wikilean.statement-set/1``
(docs/STATEMENTS.md): the map is computed from spans and span text, and neither is a
fact about where the cut was published. ``--store-out`` writes one document a page, the
shape the store publishes at ``articles/v1/carry/<pageid>.json`` so the reader can show
the tags of an earlier cut in a fold.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import corpus as corpus_mod  # noqa: E402
import keys  # noqa: E402
import vocab  # noqa: E402

CARRY_SCHEMA = vocab.CARRY_SCHEMA


class CarryRefused(Exception):
    """A page the map cannot be computed for; its reason goes into ``refused``."""


@dataclass
class Cut:
    """One inventory's cut of one page: its layer and its passages.

    ``passages`` are the inventory document's rows as written, every scope, each with at
    least ``item`` and ``span``; ``scope`` and ``conjuncts`` are read when present.
    """

    pageid: int
    source_id: str
    layer: str
    layer_sha256: str
    passages: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class _Keyed:
    key: str
    item: str
    span: tuple[int, int]
    scope: str | None
    conjuncts: str
    #: the capped quote of the passage's span, DERIVED from the layer the same way a
    #: record's and a sealed statement's are (``records.cut_passage``), so a published
    #: map shows an orphaned tag with the very words the reader saw. Taking a harvested
    #: document's own quote instead would show the words the worker was cut at, which
    #: nothing ever displayed. None when the span cannot be cut.
    quote: dict | None = None

    def ref(self) -> dict:
        """A new passage as an entry's ``into`` names it, with the scope the new cut gave it."""
        return {"passage_key": self.key, "item": self.item, "span": list(self.span),
                "scope": self.scope}


def span_quote(layer: str, span: tuple[int, int]) -> dict | None:
    """The span's capped quote on ``layer``, as a record and a sealed statement carry it.

    Derived, never copied from the document: the words the reader was shown are the
    format's capped head, and a harvested cut's own quote is whatever the worker wrote.
    None when the span cannot be cut uniquely, which is a fact about the layer and never
    a reason to refuse the map.
    """
    import records as records_mod  # noqa: PLC0415 — only the published map needs it

    try:
        return dict(records_mod.cut_passage(layer, list(span)).quote)
    except records_mod.RecordProblem:
        return None


def conjuncts_sha256(passage: dict) -> str:
    """The sha256 of a passage's canonical conjunct list (``[]`` for a simple statement)."""
    conjuncts = passage.get("conjuncts")
    return keys.sha256_json(conjuncts if isinstance(conjuncts, list) else [])


def _span(value: Any, layer: str) -> tuple[int, int] | None:
    if (isinstance(value, (list, tuple)) and len(value) == 2
            and all(type(offset) is int for offset in value)
            and 0 <= value[0] < value[1] <= len(layer)):
        return value[0], value[1]
    return None


def _keyed(cut: Cut, which: str) -> list[_Keyed]:
    """Every passage of a cut with its key, ordered by span; refuse what cannot be keyed."""
    out: list[_Keyed] = []
    seen: dict[str, str] = {}
    for row in cut.passages:
        item = str(row.get("item") or "")
        span = _span(row.get("span"), cut.layer)
        if not item or span is None:
            raise CarryRefused(f"passage {item or '(no item)'} of the {which} cut has no span "
                               f"inside its layer ({row.get('span')!r})")
        key = keys.passage_key(cut.pageid, keys.span_sha256(cut.layer, list(span)))
        if key in seen:
            raise CarryRefused(f"two passages of the {which} cut share one passage_key "
                               f"({seen[key]} and {item}: the same text)")
        seen[key] = item
        out.append(_Keyed(key, item, span, row.get("scope"), conjuncts_sha256(row),
                          span_quote(cut.layer, span)))
    return sorted(out, key=lambda passage: (passage.span, passage.item))


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def occurrences(layer: str, text: str, limit: int = 2) -> list[int]:
    """Where ``text`` starts in ``layer``, overlapping matches counted, up to ``limit``."""
    found: list[int] = []
    at = layer.find(text)
    while at != -1 and len(found) < limit:
        found.append(at)
        at = layer.find(text, at + 1)
    return found


def _fate(placed: tuple[int, int], into: list[_Keyed]) -> str:
    if not into:
        return vocab.CARRY_REMOVED
    if len(into) > 1:
        return vocab.CARRY_SPLIT
    start, end = into[0].span
    if start == placed[0]:
        return vocab.CARRY_END_MOVED
    if start < placed[0] and end >= placed[1]:
        return vocab.CARRY_MERGED
    return vocab.CARRY_RESHAPED


def _counts(fates: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for fate in fates:
        out[fate] = out.get(fate, 0) + 1
    return dict(sorted(out.items()))


def map_page(old: Cut, new: Cut) -> dict:
    """The map of one page between two cuts.

    Returns the page's entry of a carry document — its ``pageid``, each cut's
    ``source_id``, ``layer_sha256`` and ``cut_id``, ``same_layer``, the old passages'
    fates, the new passages and the counts; raises :class:`CarryRefused` when either cut
    cannot be keyed.
    """
    if old.pageid != new.pageid:
        raise CarryRefused(f"the old cut is of page {old.pageid} and the new one of page "
                           f"{new.pageid}")
    olds, news = _keyed(old, "old"), _keyed(new, "new")
    by_key = {passage.key: passage for passage in news}
    same_layer = old.layer_sha256 == new.layer_sha256
    entered: set[str] = set()
    entries = []
    for passage in olds:
        twin = by_key.get(passage.key)
        if twin is not None:
            fate, into = vocab.CARRY_UNCHANGED, [twin]
        else:
            if same_layer:
                placed: tuple[int, int] | None = passage.span
            else:
                text = old.layer[passage.span[0]:passage.span[1]]
                hits = occurrences(new.layer, text)
                placed = (hits[0], hits[0] + len(text)) if len(hits) == 1 else None
            if placed is None:
                fate, into = vocab.CARRY_CHANGED, []
            else:
                into = [other for other in news if _overlaps(other.span, placed)]
                fate = _fate(placed, into)
        entered.update(other.key for other in into)
        entries.append({
            "passage_key": passage.key, "item": passage.item, "span": list(passage.span),
            "scope": passage.scope, "quote": passage.quote,
            "fate": fate, "into": [other.ref() for other in into],
            # nothing went in: nothing to compare (a removed or changed passage)
            "scope_changed": any(other.scope != passage.scope for other in into)
            if into else None,
            "conjuncts_changed": any(other.conjuncts != passage.conjuncts for other in into)
            if into else None})
    fresh = [{**passage.ref(), "fate": vocab.CARRY_NEW}
             for passage in news if passage.key not in entered]
    return {
        "pageid": old.pageid,
        "old": {"source_id": old.source_id, "layer_sha256": old.layer_sha256,
                "cut_id": keys.cut_id(old.pageid, old.layer_sha256,
                                      [list(p.span) for p in olds])},
        "new": {"source_id": new.source_id, "layer_sha256": new.layer_sha256,
                "cut_id": keys.cut_id(new.pageid, new.layer_sha256,
                                      [list(p.span) for p in news])},
        "same_layer": same_layer, "passages": entries, "new_passages": fresh,
        "counts": _counts([entry["fate"] for entry in entries]
                          + [vocab.CARRY_NEW] * len(fresh))}


# ---- two harvested inventory runs -----------------------------------------------------


@dataclass
class Side:
    """One cut as the map reads it: its documents by page, and its layers.

    A side is a harvested inventory run (:func:`read_side`) or a sealed statement set
    (:func:`statement_side`): the map is computed from spans and span text, and neither
    is a fact about where the cut was published.
    """

    run_id: str
    #: ``{pageid: [(source_id, document)]}``
    pages: dict[int, list[tuple[str, dict]]]
    #: ``{source_id: layer}``, read once
    layers: dict[str, str]
    #: how the carry document names this side: ``{"inventory_run": <id>}``, or
    #: ``{"statement_set": {set_id, seal_sha256}}`` for a published cut
    origin: dict | None = None

    def named(self) -> dict:
        return dict(self.origin) if self.origin else {"inventory_run": self.run_id}


def statement_side(st, sources_dir: Path) -> Side:
    """A sealed statement set as a side of the map (docs/STATEMENTS.md).

    The published cut of a page and a harvested one are the same thing to :func:`map_page`
    — spans and span text on one layer — so a re-cut can be mapped against what was
    published, which is the case the reader's orphaned-tag fold is for.
    """
    pages: dict[int, list[tuple[str, dict]]] = {}
    layers: dict[str, str] = {}
    for source_id, rows in sorted(st.statements.items()):
        source = st.sources[source_id]
        pages.setdefault(int(source["pageid"]), []).append((source_id, {
            "mode": vocab.MODE_INVENTORY, "source_id": source_id,
            "layer_sha256": source["layer_sha256"],
            "passages": [dict(row) for row in rows]}))
        path = Path(sources_dir) / source_id / "layer.txt"
        if path.is_file():
            layer = path.read_text(encoding="utf-8")
            layers[source_id] = layer
            # The map's keys are recomputed from the layer (_keyed), never read off the
            # sealed bytes, so a set whose sealed span_sha256 is not the layer's would be
            # mapped under keys the store never published and the reader's fold would find
            # no tag of them. Refuse instead of substituting our own.
            for row in rows:
                span = list(row["span"])
                if keys.span_sha256(layer, span) != row["span_sha256"]:
                    raise CarryRefused(
                        f"{source_id} {row['item']}: the set seals span_sha256 "
                        f"{str(row['span_sha256'])[:12]}… at {span}, and the render's layer "
                        "hashes to another: the published keys are not this layer's")
    return Side(st.set_id, pages, layers,
                origin={"statement_set": {"set_id": st.set_id,
                                          "seal_sha256": st.seal_sha256}})


def read_side(corpus_dir: Path, run_id: str, sources_dir: Path,
              pageid_of: dict[str, int]) -> Side:
    """A harvested inventory run's documents, grouped by the page each source is of.

    ``pageid_of`` maps a source id to its page id — the sourceset manifest's, since a
    page is the thing a re-cut re-cuts and two revisions of it are two source ids.
    """
    pages: dict[int, list[tuple[str, dict]]] = {}
    seen: dict[str, str] = {}
    layers: dict[str, str] = {}
    for rel, doc in corpus_mod.documents(Path(corpus_dir) / run_id):
        if doc.get("mode") != vocab.MODE_INVENTORY:
            raise CarryRefused(f"{rel} in inventory run {run_id} is a {doc.get('mode')!r} "
                               "document; a carry-over map is between two cuts")
        sid = corpus_mod.source_of(rel, doc)
        if sid in seen:
            raise CarryRefused(f"inventory run {run_id} holds two documents for {sid}: "
                               f"{seen[sid]} and {rel}")
        seen[sid] = rel
        pageid = pageid_of.get(sid)
        if pageid is None:
            raise CarryRefused(f"{rel}: {sid} is in no manifest entry, so its page is unknown")
        pages.setdefault(int(pageid), []).append((sid, doc))
        path = Path(sources_dir) / sid / "layer.txt"
        if path.is_file():
            layers[sid] = path.read_text(encoding="utf-8")
    return Side(run_id, pages, layers)


def side_cut(side: Side, pageid: int, which: str) -> Cut:
    """One page's cut out of a run, refusing what cannot be read as one."""
    docs = side.pages[pageid]
    if len(docs) != 1:
        raise CarryRefused(f"the {which} inventory ({side.run_id}) holds {len(docs)} sources of "
                           f"the page: " + ", ".join(sorted(sid for sid, _ in docs)))
    sid, doc = docs[0]
    if isinstance(doc.get("unresolved"), dict):
        raise CarryRefused(f"the {which} inventory ({side.run_id}) left {sid} unresolved "
                           f"({doc['unresolved'].get('reason', '')})")
    layer = side.layers.get(sid)
    if layer is None:
        raise CarryRefused(f"the {which} inventory's sourceset holds no rendered layer for {sid}")
    sha = keys.sha256_text(layer)
    if doc.get("layer_sha256") != sha:
        # stamp the source: spans are offsets into ONE layer, so a document made against
        # another layer is never read against this one
        raise CarryRefused(f"the {which} inventory's document for {sid} was made against layer "
                           f"{str(doc.get('layer_sha256'))[:12]}…, and the sourceset's layer is "
                           f"{sha[:12]}…")
    rows = doc.get("passages")
    return Cut(pageid, sid, layer, sha,
               [dict(row) for row in rows or [] if isinstance(row, dict)])


def build(old: Side, new: Side, *, source_ids: Iterable[str] = ()) -> dict:
    """The carry document between two inventory runs; writes nothing.

    Every page both runs hold a document for, or only the pages of ``source_ids``; a page
    only one run holds is listed in ``unpaired``, and a page the map cannot be computed
    for in ``refused`` with the reason.
    """
    named = sorted(set(source_ids))
    if named:
        considered: set[int] = set()
        for sid in named:
            found = {pageid for side in (old, new) for pageid, docs in side.pages.items()
                     if any(other == sid for other, _ in docs)}
            if not found:
                raise CarryRefused(f"neither {old.run_id} nor {new.run_id} inventoried {sid}")
            considered |= found
    else:
        considered = set(old.pages) | set(new.pages)
    pages, refused = [], []
    totals: dict[str, int] = {}
    for pageid in sorted(considered & set(old.pages) & set(new.pages)):
        try:
            page = map_page(side_cut(old, pageid, "old"), side_cut(new, pageid, "new"))
        except CarryRefused as exc:
            refused.append({"pageid": pageid, "reason": str(exc)})
            continue
        pages.append(page)
        for fate, number in page["counts"].items():
            totals[fate] = totals.get(fate, 0) + number
    return {"schema": CARRY_SCHEMA,
            "old": old.named(),
            "new": new.named(),
            "sources": named or None,
            "pages": pages, "refused": refused,
            "unpaired": {"old": sorted(considered & set(old.pages) - set(new.pages)),
                         "new": sorted(considered & set(new.pages) - set(old.pages))},
            "counts": {"pages": len(pages), **dict(sorted(totals.items()))}}


# ---- the map, as the store publishes it -------------------------------------------------


def page_documents(doc: dict) -> dict[int, dict]:
    """``{pageid: the page's own carry document}`` — one file per page for the store.

    The reader publishes these at ``articles/v1/carry/<pageid>.json`` so it can show the
    tags of an earlier cut in a fold — "from an earlier cut of this article", with the
    fate and the old quote — and let an editor re-apply one in a click. **Nothing
    auto-moves:** a tag carries only on ``unchanged``, where the key is byte-identical
    and nothing has to move (:func:`carries`); on every other fate the text changed and
    no mechanical re-pairing is honest. The D1 row is not deleted, it simply stops
    appearing, because the overlay merge is driven by the store's passages.

    Each document is the page's entry of :func:`build`'s, with the schema and both sides'
    names folded in, so one file says everything about one page and holds no time and no
    path: a re-run writes the same bytes.
    """
    out: dict[int, dict] = {}
    for page in doc.get("pages") or []:
        pageid = int(page["pageid"])
        if pageid in out:
            raise CarryRefused(f"the carry document holds page {pageid} twice")
        out[pageid] = {"schema": CARRY_SCHEMA, "pageid": pageid,
                       "old": {**(doc.get("old") or {}), **page["old"]},
                       "new": {**(doc.get("new") or {}), **page["new"]},
                       "same_layer": page["same_layer"], "passages": page["passages"],
                       "new_passages": page["new_passages"], "counts": page["counts"]}
    return out


def write_page_documents(doc: dict, out_dir: Path) -> list[Path]:
    """Write :func:`page_documents` as ``<out_dir>/<pageid>.json``, canonically.

    The directory holds this map and nothing else: a store input is not a place to leave
    a document from the run before, where a page that dropped out of the map would go on
    being published. A non-empty directory that holds anything but these documents is
    refused, and the ones it holds are replaced.
    """
    out_dir = Path(out_dir)
    pages = page_documents(doc)
    if out_dir.exists():
        if not out_dir.is_dir() or out_dir.is_symlink():
            raise CarryRefused(f"{out_dir} is not a directory")
        stray = sorted(path.name for path in out_dir.iterdir()
                       if path.is_symlink() or not path.is_file()
                       or not path.name.endswith(".json")
                       or not (path.stem.isascii() and path.stem.isdecimal()))
        if stray:
            raise CarryRefused(f"{out_dir} holds {', '.join(stray[:5])}; --store-out writes "
                               "one <pageid>.json a page and the directory holds those alone")
        for path in sorted(out_dir.iterdir()):
            if int(path.stem) not in pages:
                path.unlink()
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for pageid, page in sorted(pages.items()):
        path = out_dir / f"{pageid}.json"
        path.write_text(keys.canonical_line(page), encoding="utf-8")
        written.append(path)
    return written


# ---- rows keyed by passage_key ------------------------------------------------------------


def carries(entry: dict, *, reads_conjuncts: bool = True) -> bool:
    """Does a verdict, a label or a known answer on this old passage carry?

    Three tests on one entry of the map: its passage is ``unchanged``, the new passage —
    its one ``into`` — is ``formal_math``, and, for anything that read the conjuncts,
    ``conjuncts_changed`` is false. The old passage's scope is **not** a test: the same
    text scoped ``formal_math`` by the new cut carries what was said of it. A bundle
    verdict read the conjuncts, and so did each of its parts' pair verdicts; a caller
    that cannot say takes the default, which is the strict one.
    """
    into = entry.get("into") or []
    return (entry.get("fate") == vocab.CARRY_UNCHANGED and len(into) == 1
            and into[0].get("scope") == vocab.SCOPE_FORMAL_MATH
            and (not reads_conjuncts or entry.get("conjuncts_changed") is False))


def by_passage_key(doc: dict) -> dict[str, dict]:
    """``{old passage_key: its map entry}`` over every mapped page."""
    return {entry["passage_key"]: entry
            for page in doc.get("pages") or [] for entry in page["passages"]}


def read_rows(path: Path) -> list[dict]:
    """A JSON-lines file of objects, each with a 64-hex ``passage_key``; refuse by line.

    Lines end at ``\\n`` only: JSON holds a U+2028 or U+2029 unescaped inside a string,
    which ``str.splitlines`` would cut, and the writer writes them so.
    """
    rows = []
    text = Path(path).read_text(encoding="utf-8")
    for number, line in enumerate(text.split("\n"), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise CarryRefused(f"{path} line {number} is not JSON ({exc})") from None
        key = row.get("passage_key") if isinstance(row, dict) else None
        if not (isinstance(key, str) and keys.HEX64.match(key)):
            raise CarryRefused(f"{path} line {number} has no passage_key (a 64-hex sha256)")
        if "reads_conjuncts" in row and not isinstance(row["reads_conjuncts"], bool):
            raise CarryRefused(f"{path} line {number}: reads_conjuncts is "
                               f"{row['reads_conjuncts']!r}, not true or false")
        rows.append(row)
    return rows


def carry_rows(doc: dict, rows: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """Each row with ``carry`` (its old passage's fate, or null) and ``carried``, in order."""
    index = by_passage_key(doc)
    out, counts = [], {"rows": 0, "carried": 0, "asked_again": 0, "unmatched": 0}
    for row in rows:
        entry = index.get(row["passage_key"])
        if entry is None:
            carry, carried = None, False
            counts["unmatched"] += 1
        else:
            carry = {key: entry[key]
                     for key in ("fate", "into", "scope_changed", "conjuncts_changed")}
            carried = carries(entry, reads_conjuncts=row.get("reads_conjuncts") is not False)
        counts["rows"] += 1
        counts["carried" if carried else "asked_again"] += 1
        out.append({**row, "carry": carry, "carried": carried})
    return out, counts


# ---- the command line -----------------------------------------------------------------------


def _pageid_of(manifest: Path | None) -> dict[str, int]:
    out: dict[str, int] = {}
    if manifest is None:
        return out
    for line in Path(manifest).read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("source_id") and entry.get("pageid"):
            out[str(entry["source_id"])] = int(entry["pageid"])
    return out


def _side(which: str, run: str | None, statement_set: Path | None, corpus: Path | None,
          sources: Path, pageid_of: dict[str, int]) -> Side:
    """One side of the map: a harvested run, or a sealed statement set."""
    if statement_set is not None:
        import statements as statements_mod  # noqa: PLC0415 — only a sealed side needs it

        try:
            return statement_side(statements_mod.read_set(statement_set), sources)
        except statements_mod.StatementSetProblem as exc:
            raise CarryRefused(f"the {which} statement set: {exc}") from None
    return read_side(corpus, run, sources, pageid_of)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from-run", help="the old inventory run")
    parser.add_argument("--to-run", help="the new inventory run")
    parser.add_argument("--from-set", type=Path,
                        help="a sealed wikilean.statement-set/1 as the old cut, instead of "
                             "--from-run (docs/STATEMENTS.md)")
    parser.add_argument("--to-set", type=Path, help="the same as the new cut")
    parser.add_argument("--corpus", type=Path,
                        help="the corpus root; required for a --from-run or --to-run side")
    parser.add_argument("--sources", required=True, type=Path,
                        help="the sourceset's rendered sources (<dir>/<source-id>/layer.txt)")
    parser.add_argument("--manifest", type=Path,
                        help="the sourceset manifest, for each source's page id; required "
                             "for a harvested side (a statement set names its own)")
    parser.add_argument("--source", action="append", default=[],
                        help="map only this source's page (repeatable)")
    parser.add_argument("--out", type=Path, help="write the carry document here")
    parser.add_argument("--store-out", type=Path,
                        help="write one <pageid>.json per page here, the shape the store "
                             "publishes at articles/v1/carry/<pageid>.json")
    parser.add_argument("--rows", type=Path, help="rows keyed by passage_key to carry")
    parser.add_argument("--rows-out", type=Path, help="write the carried rows here")
    args = parser.parse_args(argv)

    if bool(args.rows) != bool(args.rows_out):
        parser.error("--rows and --rows-out go together")
    for which, flag, run, sealed in (("old", "from", args.from_run, args.from_set),
                                     ("new", "to", args.to_run, args.to_set)):
        if (run is None) == (sealed is None):
            parser.error(f"name the {which} cut once: --{flag}-run (a harvested run) or "
                         f"--{flag}-set (a sealed statement set)")
    harvested = args.from_run is not None or args.to_run is not None
    if harvested and (args.corpus is None or args.manifest is None):
        parser.error("a harvested side needs --corpus and --manifest")
    if args.store_out and args.source:
        # --store-out writes the WHOLE map: the directory holds this map and nothing
        # else, so a page that dropped out of the re-cut stops being published. Under
        # --source the map is only the pages named, and publishing it would delete every
        # other page's document — a page nobody mapped would read as a page nothing moved
        # on. Map the corpus in one run, or write --out documents and assemble them.
        parser.error("--store-out publishes the whole map, and --source maps some of it: "
                     "the store directory would lose every page this run did not name")
    pageid_of = _pageid_of(args.manifest)
    try:
        old = _side("old", args.from_run, args.from_set, args.corpus, args.sources, pageid_of)
        new = _side("new", args.to_run, args.to_set, args.corpus, args.sources, pageid_of)
        doc = build(old, new, source_ids=args.source)
    except (CarryRefused, corpus_mod.CorpusProblem, keys.KeyProblem) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"carry {old.run_id} -> {new.run_id}: " +
          ", ".join(f"{key} {value}" for key, value in doc["counts"].items()))
    for case in doc["refused"][:10]:
        print(f"  refused page {case['pageid']}: {case['reason']}", file=sys.stderr)
    if args.store_out:
        # A refused page and a page that was never re-cut are indistinguishable in the
        # store, so a map with a refusal in it is never published: the store would say
        # "nothing moved here" of a page nobody could map.
        if doc["refused"]:
            print(f"--store-out refuses to publish a map with {len(doc['refused'])} refused "
                  "page(s): a page nobody could map must not read as a page nothing moved on",
                  file=sys.stderr)
            return 2
        try:
            written = write_page_documents(doc, args.store_out)
        except CarryRefused as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"  {len(written)} page document(s) -> {args.store_out}")
    if args.rows:
        rows, counts = carry_rows(doc, read_rows(args.rows))
        args.rows_out.parent.mkdir(parents=True, exist_ok=True)
        args.rows_out.write_text("".join(keys.canonical_line(row) for row in rows),
                                 encoding="utf-8")
        print("  rows: " + ", ".join(f"{key} {value}" for key, value in counts.items()))
    return 1 if doc["refused"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
