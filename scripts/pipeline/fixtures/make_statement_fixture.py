#!/usr/bin/env python3
"""Write scripts/pipeline/fixtures/statementset/, the pipeline's own synthetic sealed cut.

New code. Two synthetic articles that between them carry every shape a
``wikilean.statement-set/1`` has, exported by the pipeline itself:

* a lead ``subject`` statement (``section: []``, ``fragment: null``) and a ``central``
  restatement in a section with its fragment;
* a compound statement with its ``conjuncts`` and ``shape`` (``existence_uniqueness``,
  which has exactly two), and a second with three (``cases``);
* an ``expository`` and a ``non_math`` passage, both ``supporting`` and both with an
  empty restatement: the cut keeps them, and no locate run will ever ask about them;
* two statements whose first twelve words are the same word for word, so their heads run
  past the twelve-word floor and their quotes are made unique by a suffix;
* each of the three deterministic finders and the model's own;
* an article with a Wikidata QID and one without.

Every ``item`` and ``head_words`` below comes from the **head rule itself**
(``gate/locate_text.assign_ids``) and every quote from ``records.cut_passage``, so the
fixture is keyed exactly as a real cut is and its ``passage_key`` is the one phase 2 will
recompute.

**Everything here is invented.** WikiLean is public: no article text, no real page, no
real declaration. The "layer" below is prose written for this fixture.

The set it writes is committed, and ``scripts/pipeline/test_statement_sets.py`` rebuilds
it and refuses a difference, so the fixture cannot drift from the writer.

Run: python3 scripts/pipeline/fixtures/make_statement_fixture.py [--out <dir>]
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIPELINE = HERE.parent
for _candidate in (PIPELINE / "gate", PIPELINE):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import export_statements  # noqa: E402
import locate_text as lt  # noqa: E402 — scripts/pipeline/gate/locate_text.py
import records as records_mod  # noqa: E402

SET_ID = "pipeline-statement-fixture"
CAMPAIGN = "fixture-2026-11"
SOURCESET = "ss-pipeline-statement-fixture"
SOURCESET_MANIFEST = "66" * 32
PRODUCER_COMMIT = "77" * 20
CREATED = "2026-11-12T00:00:00Z"
SPAN_END_RULE = 3
STATEMENT_CUT_RULE = 4

SOURCES = [
    {"source_id": "enwiki-1005-3006", "pageid": 1005, "revision": 3006,
     "title": "Sprocket bundle", "wikidata": "Q99999005",
     "html_sha256": "88" * 32, "layer_sha256": None, "renderer": "web-html/3",
     "assessment": {"class": "C", "importance": "mid"}},
    # No Wikidata item: a statement set never carries a QID, and the source may have none.
    {"source_id": "enwiki-1007-3008", "pageid": 1007, "revision": 3008,
     "title": "Gadget cover", "wikidata": None,
     "html_sha256": "99" * 32, "layer_sha256": None, "renderer": "web-html/3",
     "assessment": {"class": "Start", "importance": "low"}},
]

#: Each article's cut, in reading order. The layer is these texts joined by one space, so
#: a span is computable and no two passages share a character.
CUTS: dict[str, list[dict]] = {
    "enwiki-1005-3006": [
        {"text": "A sprocket bundle is a widget equipped with a chosen sprocket over each handle.",
         "section": [], "fragment": None, "passage_kind": "definition",
         "prominence": "subject", "scope": "formal_math", "finder": "lead",
         "statement": "A sprocket bundle is a widget with one chosen sprocket over every handle."},
        {"text": "Every finite sprocket bundle has a trivialising cover, and any two trivialising "
                 "covers of it agree.",
         "section": ["Trivialisation"], "fragment": "Trivialisation",
         "passage_kind": "theorem", "prominence": "central", "scope": "formal_math",
         "finder": "theorem_box",
         "statement": "For a finite sprocket bundle: a trivialising cover exists, and any two of "
                      "them agree.",
         "shape": "existence_uniqueness",
         "conjuncts": [{"id": "c1", "text": "a finite sprocket bundle has a trivialising cover"},
                       {"id": "c2", "text": "two trivialising covers of one bundle agree"}]},
        # Prose about the mathematics: kept, never located, and so never given a status.
        {"text": "The reader may picture this as a gadget glued along each of its handles.",
         "section": ["Trivialisation"], "fragment": "Trivialisation", "passage_kind": "other",
         "prominence": "supporting", "scope": "expository", "finder": "model", "statement": ""},
        # These two share their first twelve words, so the head rule runs past the floor and
        # the quote needs a suffix to be unique on the layer.
        {"text": "The bundle map of a trivialising cover is natural in the chosen handle it "
                 "refines.",
         "section": ["Notation"], "fragment": "Notation", "passage_kind": "notation",
         "prominence": "supporting", "scope": "formal_math", "finder": "bold_definition",
         "statement": "Naturality holds in the handle."},
        {"text": "The bundle map of a trivialising cover is natural in the chosen frame it "
                 "refines.",
         "section": ["Notation"], "fragment": "Notation", "passage_kind": "notation",
         "prominence": "supporting", "scope": "formal_math", "finder": "model",
         "statement": "Naturality holds in the frame."},
    ],
    "enwiki-1007-3008": [
        {"text": "A gadget cover of a frame is a family of gadgets that spans the frame.",
         "section": [], "fragment": None, "passage_kind": "definition",
         "prominence": "subject", "scope": "formal_math", "finder": "lead",
         "statement": "A gadget cover of a frame is a spanning family of gadgets."},
        # Not mathematics at all: the cut keeps it so the paragraph is covered.
        {"text": "Gadget covers were named after an engineer who never published anything about "
                 "them.",
         "section": ["History"], "fragment": "History", "passage_kind": "other",
         "prominence": "supporting", "scope": "non_math", "finder": "model", "statement": ""},
        {"text": "A gadget cover is refining, saturated, or neither of the two.",
         "section": ["Kinds"], "fragment": "Kinds", "passage_kind": "theorem",
         "prominence": "supporting", "scope": "formal_math", "finder": "model",
         "statement": "Each gadget cover is of exactly one of three kinds.",
         "shape": "cases",
         "conjuncts": [{"id": "c1", "text": "the cover is refining"},
                       {"id": "c2", "text": "the cover is saturated"},
                       {"id": "c3", "text": "the cover is of neither kind"}]},
    ],
}


def layer_of(source_id: str) -> tuple[str, list[list[int]]]:
    """The article's layer and each passage's half-open span on it."""
    spans: list[list[int]] = []
    at = 0
    for row in CUTS[source_id]:
        spans.append([at, at + len(row["text"])])
        at = spans[-1][1] + 1  # the single space that joins them
    return " ".join(row["text"] for row in CUTS[source_id]), spans


def passages(source_id: str) -> list[dict]:
    """The harvested cut's rows for one source, keyed by the head rule itself."""
    layer, spans = layer_of(source_id)
    ids = lt.assign_ids(layer, [span[0] for span in spans])
    out = []
    for row, span, (_at, digest, words) in zip(CUTS[source_id], spans, ids):
        cut = records_mod.cut_passage(layer, span)
        passage = {"item": "In-text #" + digest, "head_words": words, "span": span,
                   "quote": dict(cut.quote),
                   "section": list(row["section"]), "fragment": row["fragment"],
                   "passage_kind": row["passage_kind"], "prominence": row["prominence"],
                   "scope": row["scope"], "statement": row["statement"],
                   "finder": row["finder"],
                   # the cut's own bookkeeping, which a statement set does not seal
                   "concept": None}
        if "conjuncts" in row:
            passage["conjuncts"] = [dict(c) for c in row["conjuncts"]]
            passage["shape"] = row["shape"]
        out.append(passage)
    return out


def sources() -> list[dict]:
    """The sourceset entries, each with the sha256 of its own layer."""
    return [{**entry,
             "layer_sha256": hashlib.sha256(
                 layer_of(entry["source_id"])[0].encode("utf-8")).hexdigest()}
            for entry in SOURCES]


def write(out_dir: Path) -> dict:
    # Check the target BEFORE removing anything: --out pointed at a directory this may
    # not write (outside fixtures/, or holding something that is not an earlier set) must
    # come back refused with that directory untouched.
    export_statements.check_statement_target(out_dir, in_checkout=True)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    return export_statements.build_set(
        inventory={sid: passages(sid) for sid in CUTS}, sources=sources(),
        layers={sid: layer_of(sid)[0] for sid in CUTS},
        set_id=SET_ID, campaign=CAMPAIGN, sourceset=SOURCESET,
        sourceset_manifest=SOURCESET_MANIFEST, producer_commit=PRODUCER_COMMIT,
        created=CREATED, span_end_rule=SPAN_END_RULE,
        statement_cut_rule=STATEMENT_CUT_RULE, out=out_dir, in_checkout=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=HERE / "statementset")
    args = parser.parse_args(argv)
    report = write(args.out)
    print(f"{args.out}: {report['exported']}  seal {report['seal_sha256'][:12]}")
    for case in report["refused"]:
        print("  refused:", case)
    return 1 if report["refused"] else 0


if __name__ == "__main__":
    sys.exit(main())
