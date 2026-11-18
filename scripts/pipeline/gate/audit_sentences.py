#!/usr/bin/env python3
"""Measure an exported record set against the gate's sentence check. Read-only.

docs/PIPELINE.md section 3 (stage 7). Statement-cut rule 4 refuses a passage that starts
inside a sentence where no rule of the cut starts one (issue #172), but a record set
exported before that check carries the starts it would have refused. This runs the same
check — ``locate_text.Blocks.cuts_a_sentence``, the one the gate calls — over a sealed
set and says how many passages, and which, start there. It re-runs nothing, writes
nothing, and asks for no model: measuring a published store is what it is for, and
whether to re-cut and re-locate those articles is the operator's call.

A span is an offset into one text layer, so the check needs the **rendered sources** the
set was cut from (``<sources>/<source-id>/{layer.txt,paragraphs.json,formulas.json}``,
stage 9a's output) beside the set. A source whose layer is missing, unreadable or not
the one the set pins is reported as unchecked rather than quietly passed over, because
an audit that counts a source it never read is worse than one that says so.

**It prints keys, ids, offsets and counts, and never a character of the article.**
WikiLean is public and an audit's output goes into tickets and reports; the layer is
read, and nothing of it is written out.

    python3 scripts/pipeline/gate/audit_sentences.py --record-set <dir> --sources <dir>
    python3 scripts/pipeline/gate/audit_sentences.py --record-set <dir> --sources <dir> --json

Exit codes: ``0`` every source checked and no passage starts where no rule allows, ``1``
some passage does, ``2`` the set was refused or no source could be checked at all, ``3``
the audit itself failed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _candidate in (HERE, HERE.parent, HERE.parents[2] / "site"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import keys  # noqa: E402 — scripts/pipeline/keys.py
import locate_text as lt  # noqa: E402 — scripts/pipeline/gate/locate_text.py
import readback  # noqa: E402 — scripts/pipeline/readback.py

EXIT_CLEAN, EXIT_FOUND, EXIT_REFUSED, EXIT_BROKEN = 0, 1, 2, 3
#: What one source's rendered view is, under ``<sources>/<source-id>/``.
LAYER_FILE, PARAGRAPH_FILE, FORMULA_FILE = "layer.txt", "paragraphs.json", "formulas.json"


class AuditProblem(ValueError):
    """A set or a source directory this audit cannot read."""


def _view(directory: Path, layer_sha256: str) -> lt.Blocks:
    """One source's blocks, held to the layer the set pins."""
    try:
        layer = (directory / LAYER_FILE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise AuditProblem(f"{LAYER_FILE} cannot be read ({exc.__class__.__name__})") from None
    got = keys.sha256_text(layer)
    if layer_sha256 and got != layer_sha256:
        raise AuditProblem(f"{LAYER_FILE} hashes to {got[:12]} and the set pins "
                           f"{layer_sha256[:12]}; a span is an offset into one layer")
    views = []
    for name in (PARAGRAPH_FILE, FORMULA_FILE):
        try:
            value = json.loads((directory / name).read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise AuditProblem(f"{name} cannot be read ({exc.__class__.__name__})") from None
        if not isinstance(value, list):
            raise AuditProblem(f"{name} is not a list")
        views.append(value)
    return lt.Blocks(layer, views[0], views[1])


def _passage_key(row: dict, pageid: int) -> str:
    """The row's ``passage_key``, computed from the cut where the set does not carry one."""
    carried = row.get("passage_key")
    if isinstance(carried, str) and carried:
        return carried
    return keys.passage_key(pageid, (row.get("cut") or {}).get("span_sha256") or "")


def audit(set_dir: Path, sources_dir: Path) -> dict:
    """``{"sources": [...], "unchecked": [...], "totals": {...}}`` of one sealed set.

    One entry per source with a start no rule of the cut puts there, each listing its
    offending passages in span order; one ``unchecked`` entry per source whose rendered
    view is missing or not the one the set pins.
    """
    read = readback.read_set(Path(set_dir))
    by_source: dict[str, list[dict]] = {}
    for row in read.rows:
        by_source.setdefault(str(row["source_id"]), []).append(row)

    found: list[dict] = []
    unchecked: list[dict] = []
    checked_passages = 0
    for source_id in sorted(by_source):
        source = read.source(source_id)
        try:
            # a source id names one directory under --sources and never a path
            if source_id in ("", ".", "..") or source_id != Path(source_id).name:
                raise AuditProblem(f"{source_id!r} is no directory name")
            blocks = _view(Path(sources_dir) / source_id, str(source.get("layer_sha256") or ""))
        except AuditProblem as exc:
            unchecked.append({"source_id": source_id, "pageid": source.get("pageid"),
                              "passages": len(by_source[source_id]), "reason": str(exc)})
            continue
        rows = sorted(by_source[source_id], key=lambda row: (row["span"][0], row["span"][1]))
        checked_passages += len(rows)
        passages: list[dict] = []
        for number, row in enumerate(rows):
            start, end = int(row["span"][0]), int(row["span"][1])
            if not blocks.cuts_a_sentence(start):
                continue
            entry = {"passage_key": _passage_key(row, int(source["pageid"])),
                     "item": row["item"], "span": [start, end],
                     "outcome": row.get("outcome"), "status": row.get("status"),
                     "locations": len(row.get("locations") or []), "truncates": None}
            sentence = blocks.sentence_at(start)
            if number and sentence is not None:
                earlier = rows[number - 1]
                if int(earlier["span"][1]) <= start and \
                        blocks.sentence_at(int(earlier["span"][0])) == sentence:
                    entry["truncates"] = earlier["item"]
            passages.append(entry)
        if passages:
            found.append({"source_id": source_id, "pageid": source.get("pageid"),
                          "passages": passages})

    offending = [entry for source in found for entry in source["passages"]]
    totals = {
        "sources": len(by_source),
        "sources_checked": len(by_source) - len(unchecked),
        "sources_with_a_cut_no_rule_allows": len(found),
        "passages_checked": checked_passages,
        "passages": len(offending),
        "located": sum(1 for entry in offending if entry["outcome"] == "located"),
        "truncated_as_well": len({entry["truncates"] for entry in offending
                                  if entry["truncates"]}),
    }
    return {"set": str(Path(set_dir)), "sources": found, "unchecked": unchecked,
            "totals": totals}


def report(result: dict) -> list[str]:
    """The audit as lines: keys, ids, offsets and counts, and no article text."""
    lines = []
    for source in result["sources"]:
        lines.append(f"{source['source_id']} (pageid {source['pageid']}): "
                     f"{len(source['passages'])} passage(s) start where no rule cuts")
        for entry in source["passages"]:
            truncates = f", truncating {entry['truncates']}" if entry["truncates"] else ""
            lines.append(f"    {entry['passage_key']}  {entry['item']}  "
                         f"[{entry['span'][0]}, {entry['span'][1]}]  {entry['outcome']}"
                         f"/{entry['status']}  {entry['locations']} location(s){truncates}")
    for source in result["unchecked"]:
        lines.append(f"{source['source_id']} (pageid {source['pageid']}): not checked, "
                     f"{source['passages']} passage(s) — {source['reason']}")
    totals = result["totals"]
    lines.append(f"{totals['sources_checked']} of {totals['sources']} source(s) checked, "
                 f"{totals['passages_checked']} passage(s); "
                 f"{totals['passages']} start inside a sentence at no cut of the rules "
                 f"({totals['located']} located) in {totals['sources_with_a_cut_no_rule_allows']} "
                 f"source(s), truncating {totals['truncated_as_well']} passage(s) before them")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Count the passages of a sealed record set that start inside a "
                    "sentence where no rule of the cut starts one (gate rule 10, "
                    "statement-cut rule 4). Reads; changes nothing.")
    parser.add_argument("--record-set", required=True, type=Path,
                        help="the sealed set's directory")
    parser.add_argument("--sources", required=True, type=Path,
                        help="the rendered sources the set was cut from, one directory per "
                             "source id")
    parser.add_argument("--json", action="store_true",
                        help="write the result as one JSON object instead of the report")
    args = parser.parse_args(argv)
    try:
        result = audit(args.record_set, args.sources)
    except readback.SetProblem as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_REFUSED
    except (AuditProblem, OSError, ValueError, KeyError) as exc:
        print(f"the audit could not run: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return EXIT_BROKEN
    if args.json:
        print(json.dumps(result, indent=1, sort_keys=True))
    else:
        print("\n".join(report(result)))
    if not result["totals"]["sources_checked"]:
        print("no source could be checked: --sources holds none of the set's rendered "
              "sources, so this measured nothing", file=sys.stderr)
        return EXIT_REFUSED
    return EXIT_FOUND if result["totals"]["passages"] else EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
