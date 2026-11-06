#!/usr/bin/env python3
"""Write scripts/pipeline/fixtures/recordset/, the pipeline's own synthetic sealed set.

New code. Two synthetic articles that between them carry every outcome and shape the
record format has, exported by the pipeline itself:

* a lead passage (``section: []``) with two locations, one of them a ``closeMatch`` with
  its note;
* a ``subject`` passage carrying the article's QID, and one whose location is
  ``challenge_pending`` and so carries none;
* a checked absence with its receipt (three distinct queries, two candidates);
* an unresolved passage (``object: []``, no status);
* a coverage bundle over two ``hasPart`` parts, with its conjuncts;
* a location with no ``end_line`` (a published index that records only ``line``);
* a universe whose ``init`` and ``std`` libraries are one repository at one commit, so
  an absence names that repository once, beside a second repository.

**Everything here is invented.** WikiLean is public: no article text, no real page, no
real declaration. The "layer" below is prose written for this fixture.

The set it writes is committed, and ``scripts/pipeline/test_record_sets.py`` rebuilds it
and refuses a difference, so the fixture cannot drift from the writer.

Run: python3 scripts/pipeline/fixtures/make_fixture.py [--out <dir>]
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIPELINE = HERE.parent
sys.path.insert(0, str(PIPELINE))

import export_records  # noqa: E402
import records as records_mod  # noqa: E402
import status as status_mod  # noqa: E402

SET_ID = "pipeline-fixture"
CAMPAIGN = "fixture-2026-11"
SOURCESET = "ss-pipeline-fixture"
SOURCESET_MANIFEST = "11" * 32
UNIVERSE_SHA = "22" * 32
PRODUCER_COMMIT = "33" * 20
CREATED = "2026-11-01T00:00:00Z"
STATUS_RULE = 3

UNIVERSE = {
    "id": "u-fixture",
    "universe_sha256": UNIVERSE_SHA,
    "frozen": "2026-10-01",
    "libraries": [
        {"id": "widgetlib", "brain_lib": "WidgetLib",
         "repo": "https://github.com/example-org/widgetlib",
         "commit": "a" * 40, "toolchain": "leanprover/lean4:v4.34.0",
         "roots": ["WidgetLib"], "licence": {"spdx": "Apache-2.0"}},
        # One repository, two libraries, one root each: an absence names it once.
        {"id": "init", "brain_lib": "Init",
         "repo": "https://github.com/example-org/core",
         "commit": "b" * 40, "toolchain": "leanprover/lean4:v4.34.0",
         "roots": ["Init"], "licence": {"spdx": "Apache-2.0"}},
        {"id": "std", "brain_lib": "Std",
         "repo": "https://github.com/example-org/core",
         "commit": "b" * 40, "toolchain": "leanprover/lean4:v4.34.0",
         "roots": ["Std"], "licence": {"spdx": "Apache-2.0"}},
    ],
    "excluded": [{"id": "closedlib", "reason": "its licence forbids evaluation use"}],
}

SOURCES = [
    {"source_id": "enwiki-1001-2002", "pageid": 1001, "revision": 2002,
     "title": "Widget lemma", "wikidata": "Q99999001",
     "html_sha256": "44" * 32, "layer_sha256": None, "renderer": "web-html/3",
     "assessment": {"class": "B", "importance": "high"}},
    {"source_id": "enwiki-1003-2004", "pageid": 1003, "revision": 2004,
     "title": "Gadget (mathematics)", "wikidata": "Q99999003",
     "html_sha256": "55" * 32, "layer_sha256": None, "renderer": "web-html/3",
     "assessment": {"class": "Start", "importance": "mid"}},
]

# Each article's layer, as a list of passages. The layer is the passages joined by a
# single space, so a span is computable; every text is unique on its layer, so most
# quotes need no context.
LAYERS = {
    "enwiki-1001-2002": [
        "A widget is a gadget that carries a handle and a spanning frame around it.",
        "Every finite widget admits a spanning gadget, and two such gadgets of one finite widget are equal.",
        "The spanning gadget of a widget may be computed by repeatedly removing a loose handle from it.",
        "Worked example: the three-handled widget has exactly one spanning gadget, drawn below in grey.",
        "A reader who prefers frames to handles may skip the rest of this section without loss.",
    ],
    "enwiki-1003-2004": [
        "A gadget is a frame together with a chosen handle, subject to the usual compatibility rule.",
        "Gadgets over a fixed frame form a category whose morphisms preserve the chosen handle.",
        "No published source states the handle-preservation rule for infinite frames.",
    ],
}


def layer_of(source_id: str) -> tuple[str, list[list[int]]]:
    """The article's layer and each passage's half-open span on it."""
    spans: list[list[int]] = []
    at = 0
    for text in LAYERS[source_id]:
        spans.append([at, at + len(text)])
        at += len(text) + 1  # the single space that joins them
    return " ".join(LAYERS[source_id]), spans


def location(key_byte: str, **over) -> dict:
    base = {"location_key": key_byte * 64, "relation": "exactMatch", "library": "widgetlib",
            "declaration": "WidgetLib.Widget.spanning", "module": "WidgetLib.Widget",
            "path": "WidgetLib/Widget.lean", "line": 12, "end_line": 20,
            "proof_status": "proved", "rank": 1, "confidence": "high",
            "challenge": "survived"}
    base.update(over)
    return base


def rows() -> list[dict]:
    """The outcome rows the fixture exports, one per passage."""
    widget, widget_spans = layer_of("enwiki-1001-2002")
    gadget, gadget_spans = layer_of("enwiki-1003-2004")
    out: list[dict] = []

    # 1. the lead passage: two locations, the second a closeMatch with its note.
    out.append({
        "source_id": "enwiki-1001-2002", "item": "In-text #1a2b3c4d",
        "passage_kind": "definition", "span": widget_spans[0], "section": [],
        "prominence": "supporting", "challenge": "survived", "outcome": "located",
        "locations": [
            location("1", relation="exactMatch", proof_status="definition",
                     declaration="WidgetLib.Widget", module="WidgetLib.Widget",
                     path="WidgetLib/Widget.lean", line=4, end_line=9),
            location("2", relation="closeMatch", proof_status="definition", rank=2,
                     confidence="medium", declaration="WidgetLib.FramedWidget",
                     note="The library fixes the frame first, where the passage fixes the handle.",
                     line=30, end_line=36),
        ],
    })
    # 2. the subject passage: a coverage bundle over two hasPart parts, and the QID.
    out.append({
        "source_id": "enwiki-1001-2002", "item": "In-text #2b3c4d5e",
        "passage_kind": "theorem", "span": widget_spans[1], "section": ["Statement"],
        "prominence": "subject", "challenge": "survived", "outcome": "located",
        "locations": [
            location("3", relation="hasPart", declaration="WidgetLib.exists_spanning_gadget",
                     module="WidgetLib.Spanning", path="WidgetLib/Spanning.lean",
                     line=100, end_line=140),
            location("4", relation="hasPart", rank=2, confidence="medium",
                     declaration="WidgetLib.spanning_gadget_unique",
                     module="WidgetLib.Spanning", path="WidgetLib/Spanning.lean",
                     line=150, end_line=190),
        ],
        "coverage": {"complete": True, "parts": ["3" * 64, "4" * 64], "challenge": "survived",
                     "relation": "exactMatch",
                     "conjuncts": [{"id": "c1", "text": "a finite widget has a spanning gadget",
                                    "part": "3" * 64},
                                   {"id": "c2", "text": "two spanning gadgets of one finite "
                                                        "widget are equal", "part": "4" * 64}]},
    })
    # 3. a construction passage whose one location has no end_line, challenge_pending, so
    #    the record carries no QID although the passage is subject.
    out.append({
        "source_id": "enwiki-1001-2002", "item": "In-text #3c4d5e6f",
        "passage_kind": "construction", "span": widget_spans[2], "section": ["Construction"],
        "prominence": "subject", "challenge": "challenge_pending", "outcome": "located",
        "locations": [location("5", relation="exactMatch", proof_status="definition",
                               declaration="WidgetLib.pruneHandles", module="WidgetLib.Prune",
                               path="WidgetLib/Prune.lean", line=7, end_line=None,
                               challenge="challenge_pending", confidence="low")],
    })
    # 4. an example passage with only a relatedMatch and no absence: unresolved, no status.
    out.append({
        "source_id": "enwiki-1001-2002", "item": "In-text #4d5e6f70",
        "passage_kind": "example", "span": widget_spans[3], "section": ["Examples"],
        "prominence": "supporting", "challenge": "sampled_out", "outcome": "located",
        "locations": [location("6", relation="relatedMatch", proof_status="proved",
                               declaration="WidgetLib.three_handled",
                               module="WidgetLib.Examples",
                               path="WidgetLib/Examples.lean", line=3, end_line=5,
                               challenge="sampled_out", confidence="low")],
        "status": None,
    })
    # 5. a passage the run settled nothing for: object: [], no status.
    out.append({
        "source_id": "enwiki-1001-2002", "item": "In-text #5e6f7081",
        "passage_kind": "other", "span": widget_spans[4], "section": ["Examples"],
        "prominence": "supporting", "challenge": "not_required", "outcome": "unresolved",
        "locations": [], "status": None,
    })
    # 6. a definition passage matched to a Prop-valued definition: statement only.
    out.append({
        "source_id": "enwiki-1003-2004", "item": "In-text #6f708192",
        "passage_kind": "definition", "span": gadget_spans[0], "section": [],
        "prominence": "subject", "challenge": "survived", "outcome": "located",
        "locations": [location("7", relation="exactMatch", library="std",
                               proof_status="statement_only", declaration="Std.Gadget",
                               module="Std.Gadget", path="Std/Gadget.lean", line=11,
                               end_line=15)],
    })
    # 7. a notation passage with a broadMatch to data: formalized under status rule 3.
    out.append({
        "source_id": "enwiki-1003-2004", "item": "In-text #70819233",
        "passage_kind": "notation", "span": gadget_spans[1], "section": ["Category"],
        "prominence": "central", "challenge": "downgraded", "outcome": "located",
        "locations": [location("8", relation="broadMatch", library="init",
                               proof_status="definition", declaration="Init.Category",
                               module="Init.Category", path="Init/Category.lean", line=2,
                               end_line=40, challenge="downgraded")],
    })
    # 8. a checked absence beside a relatedMatch, with its receipt.
    out.append({
        "source_id": "enwiki-1003-2004", "item": "In-text #81923344",
        "passage_kind": "theorem", "span": gadget_spans[2], "section": ["Open questions"],
        "prominence": "central", "challenge": "survived", "outcome": "absent",
        "locations": [location("9", relation="relatedMatch", library="widgetlib",
                               proof_status="proved", declaration="WidgetLib.handle_rule",
                               module="WidgetLib.Handle", path="WidgetLib/Handle.lean",
                               line=60, end_line=64)],
        "absence": {"challenge": "survived",
                    "receipt": {"queries": ["handle preservation rule for infinite frames",
                                            "infinite frame gadget handle",
                                            "gadget morphism preserves handle infinite"],
                                "candidates": [
                                    {"library": "widgetlib",
                                     "declaration": "WidgetLib.handle_rule",
                                     "verdict": "rejected",
                                     "reason": "It states the rule for finite frames only."},
                                    {"library": "std", "declaration": "Std.Gadget.map",
                                     "verdict": "rejected",
                                     "reason": "It is the morphism, not the rule about it."}]}},
    })

    layers = {"enwiki-1001-2002": widget, "enwiki-1003-2004": gadget}
    for row in out:
        layer = layers[row["source_id"]]
        cut = records_mod.cut_passage(layer, row["span"])
        row["cut"] = {"quote": dict(cut.quote), "span_sha256": cut.span_sha256}
        if "status" not in row:
            row["status"] = status_mod.derive(row["passage_kind"], row["locations"],
                                              row["outcome"] == "absent", row.get("coverage"),
                                              STATUS_RULE)
    return out


def sources() -> list[dict]:
    """The sourceset entries, each with the sha256 of its own layer."""
    import hashlib

    out = []
    for entry in SOURCES:
        layer, _ = layer_of(entry["source_id"])
        out.append({**entry,
                    "layer_sha256": hashlib.sha256(layer.encode("utf-8")).hexdigest()})
    return out


def write(out_dir: Path) -> dict:
    layers = {sid: layer_of(sid)[0] for sid in LAYERS}
    if out_dir.exists():
        shutil.rmtree(out_dir)
    return export_records.build_set(
        rows=rows(), sources=sources(), universe=UNIVERSE, set_id=SET_ID, campaign=CAMPAIGN,
        sourceset=SOURCESET, sourceset_manifest=SOURCESET_MANIFEST,
        producer_commit=PRODUCER_COMMIT, created=CREATED, status_rule=STATUS_RULE,
        out=out_dir, layers=layers, in_checkout=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=HERE / "recordset")
    args = parser.parse_args(argv)
    report = write(args.out)
    print(f"{args.out}: {report['exported']}  seal {report['seal_sha256'][:12]}")
    for case in report["refused"]:
        print("  refused:", case)
    return 1 if report["refused"] else 0


if __name__ == "__main__":
    sys.exit(main())
