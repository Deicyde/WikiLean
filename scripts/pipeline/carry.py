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

    python3 scripts/pipeline/carry.py --from-run inv-1 --to-run inv-2 \\
        --corpus <dir> --runs <dir> --sources <dir> [--out <file>] \\
        [--rows <file> --rows-out <file>]
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

CARRY_SCHEMA = "wikilean.pipeline.carry/1"


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

    def ref(self) -> dict:
        """A new passage as an entry's ``into`` names it, with the scope the new cut gave it."""
        return {"passage_key": self.key, "item": self.item, "span": list(self.span),
                "scope": self.scope}


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
        out.append(_Keyed(key, item, span, row.get("scope"), conjuncts_sha256(row)))
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
            "scope": passage.scope, "fate": fate, "into": [other.ref() for other in into],
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
    """One inventory run as the map reads it: its documents by page, and its layers."""

    run_id: str
    #: ``{pageid: [(source_id, document)]}``
    pages: dict[int, list[tuple[str, dict]]]
    #: ``{source_id: layer}``, read once
    layers: dict[str, str]


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
            "old": {"inventory_run": old.run_id},
            "new": {"inventory_run": new.run_id},
            "sources": named or None,
            "pages": pages, "refused": refused,
            "unpaired": {"old": sorted(considered & set(old.pages) - set(new.pages)),
                         "new": sorted(considered & set(new.pages) - set(old.pages))},
            "counts": {"pages": len(pages), **dict(sorted(totals.items()))}}


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from-run", required=True, help="the old inventory run")
    parser.add_argument("--to-run", required=True, help="the new inventory run")
    parser.add_argument("--corpus", required=True, type=Path, help="the corpus root")
    parser.add_argument("--sources", required=True, type=Path,
                        help="the sourceset's rendered sources (<dir>/<source-id>/layer.txt)")
    parser.add_argument("--manifest", required=True, type=Path,
                        help="the sourceset manifest, for each source's page id")
    parser.add_argument("--source", action="append", default=[],
                        help="map only this source's page (repeatable)")
    parser.add_argument("--out", type=Path, help="write the carry document here")
    parser.add_argument("--rows", type=Path, help="rows keyed by passage_key to carry")
    parser.add_argument("--rows-out", type=Path, help="write the carried rows here")
    args = parser.parse_args(argv)

    if bool(args.rows) != bool(args.rows_out):
        parser.error("--rows and --rows-out go together")
    pageid_of = _pageid_of(args.manifest)
    try:
        old = read_side(args.corpus, args.from_run, args.sources, pageid_of)
        new = read_side(args.corpus, args.to_run, args.sources, pageid_of)
        doc = build(old, new, source_ids=args.source)
    except (CarryRefused, corpus_mod.CorpusProblem, keys.KeyProblem) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"carry {args.from_run} -> {args.to_run}: " +
          ", ".join(f"{key} {value}" for key, value in doc["counts"].items()))
    for case in doc["refused"][:10]:
        print(f"  refused page {case['pageid']}: {case['reason']}")
    if args.rows:
        rows, counts = carry_rows(doc, read_rows(args.rows))
        args.rows_out.parent.mkdir(parents=True, exist_ok=True)
        args.rows_out.write_text("".join(keys.canonical_line(row) for row in rows),
                                 encoding="utf-8")
        print("  rows: " + ", ".join(f"{key} {value}" for key, value in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
