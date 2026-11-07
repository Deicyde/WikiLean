#!/usr/bin/env python3
"""Stage 7: place an inventory's quotes on the layer and key its passages.

docs/PIPELINE.md section 3 (stages 7 and 9b). A passage is named ``In-text #<hex>``,
where the hex is the head rule's hash of the passage's first words on the text layer,
and its ``span`` is a pair of code-point offsets into that layer. **Neither is anything
a model can write**: the id is a hash the gate recomputes, the span's end is the
span-end rule's answer given every other passage's start, and gate rule 2 refuses a
document whose ``item``, ``head_words`` or ``span`` disagrees by one character.

So the worker writes the quote — the words the passage starts at, copied off the layer
— and **the runner runs this** before the gate, filling the three mechanical fields in.
It is the same arithmetic rule 2 does, out of the same module
(:mod:`locate_text`), against the same pinned views (``verify_locate``'s
:class:`~verify_locate.SourceView`), so a document this places is a document the gate
agrees with; where it cannot place a quote it says so and changes nothing, and the gate
then refuses the document with the worker's own quote in the message.

This is a gate-directory tool: it is staged out of tree with the gate
(``scripts/pipeline/gate/stage.py``) and run by the runner against the pinned
``gate.json``, never by the worker, which has no shell and never sees it.

    python3 <gate dir>/passage_ids.py --gate-config <gate dir>/gate.json \\
        --document <file> [--document <file> ...] [--write]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import locate_text as lt  # noqa: E402
import verify_locate as gate  # noqa: E402
import vocab  # noqa: E402

#: What this writes into a passage, and nothing else.
FILLED = ("item", "head_words", "span")

EXIT_OK = 0
EXIT_UNPLACED = 1
EXIT_BROKEN = 3


class Unplaceable(ValueError):
    """A quote this cannot put on the layer, named rather than guessed at."""


def place(document: dict, view: "gate.SourceView", *, span_end_rule: int) -> dict:
    """Fill ``item``, ``head_words`` and ``span`` on every passage of an inventory.

    Returns ``{"placed": [item], "unplaced": [{"at": n, "quote": …, "why": …}]}``.
    The document is changed in place, and only in :data:`FILLED`: a passage this
    cannot place keeps exactly what the worker wrote, so the gate's refusal names the
    worker's own quote.
    """
    passages = [p for p in document.get("passages") or [] if isinstance(p, dict)]
    rows: list[tuple[int, dict, int, int]] = []       # (number, passage, start, head)
    unplaced: list[dict] = []
    for number, passage in enumerate(passages):
        quote = passage.get("quote") if isinstance(passage.get("quote"), dict) else None
        exact = (quote or {}).get("exact")
        if not isinstance(exact, str) or not exact.strip():
            unplaced.append({"at": number, "quote": None,
                             "why": "the passage carries no quote to place it at"})
            continue
        hits = lt.find_quote(view.layer, exact, (quote.get("prefix") or ""),
                             (quote.get("suffix") or ""))
        if len(hits) != 1:
            unplaced.append({"at": number, "quote": exact[:80],
                             "why": f"the quote occurs {len(hits)} times on the layer; "
                                    "it must occur exactly once with its prefix and "
                                    "suffix"})
            continue
        try:
            head_start = lt.check_start(view.head_map.head,
                                        view.head_map.to_head(hits[0]))
        except (lt.HeadError, ValueError) as exc:
            unplaced.append({"at": number, "quote": exact[:80], "why": str(exc)})
            continue
        rows.append((number, passage, hits[0], head_start))

    if not rows:
        return {"placed": [], "unplaced": unplaced}
    try:
        ids = lt.assign_ids(view.head_map.head, [head for _n, _p, _s, head in rows])
    except lt.HeadError as exc:
        raise Unplaceable(f"the head rule cannot key this document's passages: {exc}") \
            from None

    starts = sorted({start for _n, _p, start, _h in rows})
    placed: list[str] = []
    for (number, passage, start, _head), (_at, digest, words) in zip(rows, ids):
        exact = passage["quote"]["exact"]
        after = [s for s in starts if s > start]
        try:
            end = lt.passage_end(view.layer, view.blocks, start, exact,
                                 passage.get("until"), rule=span_end_rule,
                                 next_start=(None if passage.get("until")
                                             else (after[0] if after else None)),
                                 formulas=view.formulas)
        except (lt.HeadError, ValueError) as exc:
            unplaced.append({"at": number, "quote": exact[:80], "why": str(exc)})
            continue
        passage["item"] = lt_item(digest)
        passage["head_words"] = words
        passage["span"] = [start, end]
        placed.append(passage["item"])
    return {"placed": placed, "unplaced": unplaced}


def lt_item(digest: str) -> str:
    """``In-text #<hex>`` — the one spelling of a passage label."""
    return "In-text #" + str(digest)


def run(gate_config: pathlib.Path, documents: list[pathlib.Path], *, write: bool) -> dict:
    """Place every named document's passages; returns a report, writes when asked."""
    cfg = gate.GateConfig(gate.read_json(pathlib.Path(gate_config),
                                         "the gate configuration"),
                          pathlib.Path(gate_config))
    if cfg.mode != vocab.MODE_INVENTORY:
        # a locate or challenge document answers the frozen inventory's passages and
        # copies their ids from its task manifest: there is nothing mechanical to fill
        return {"mode": cfg.mode, "documents": [], "placed": 0, "unplaced": 0,
                "skipped": "only an inventory's ids are derived from its quotes"}
    report = {"mode": cfg.mode, "documents": [], "placed": 0, "unplaced": 0}
    for path in documents:
        document = gate.read_json(pathlib.Path(path), f"the document {path}")
        if not isinstance(document, dict):
            raise Unplaceable(f"{path} is not a document object")
        source_id = str(document.get("source_id") or "")
        if not source_id:
            raise Unplaceable(f"{path} names no source_id")
        got = place(document, gate.Context(cfg).view(source_id),
                    span_end_rule=cfg.span_end_rule)
        if write and got["placed"]:
            pathlib.Path(path).write_text(
                json.dumps(document, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                encoding="utf-8")
        report["documents"].append({"path": str(path), "source_id": source_id, **got})
        report["placed"] += len(got["placed"])
        report["unplaced"] += len(got["unplaced"])
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                     allow_abbrev=False)
    parser.add_argument("--gate-config", required=True)
    parser.add_argument("--document", action="append", default=[], required=True,
                        help="an inventory document to key (repeatable)")
    parser.add_argument("--write", action="store_true",
                        help="write the filled document back; without it, report only")
    args = parser.parse_args(argv)
    try:
        report = run(pathlib.Path(args.gate_config),
                     [pathlib.Path(p) for p in args.document], write=args.write)
    except (gate.GateBroken, Unplaceable) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return EXIT_BROKEN
    except Exception as exc:  # noqa: BLE001 — a crash here is never a verdict
        print(f"FAIL: passage_ids itself failed ({exc.__class__.__name__}: {exc})",
              file=sys.stderr)
        return EXIT_BROKEN
    if report.get("skipped"):
        print(f"OK: nothing to key in a {report['mode']} document "
              f"({report['skipped']})")
        return EXIT_OK
    print(f"{'WROTE' if args.write else 'READ'}: {report['placed']} passage(s) keyed, "
          f"{report['unplaced']} not placed")
    for document in report["documents"]:
        for row in document["unplaced"]:
            print(f"  {document['source_id']} passages[{row['at']}]: {row['why']}"
                  + (f" ({row['quote']!r})" if row["quote"] else ""), file=sys.stderr)
    return EXIT_UNPLACED if report["unplaced"] else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
