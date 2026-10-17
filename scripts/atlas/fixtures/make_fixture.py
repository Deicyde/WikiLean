#!/usr/bin/env python3
"""Write the hand-built sealed locate record set the atlas tests run on.

New code (docs/ATLAS.md, "Input contract"). Every value below is synthetic: two
made-up articles ("Widget lemma", page id 1001; "Gadget (mathematics)", page id
1003), made-up declarations and made-up commits. The repositories are real
library repositories so the library registry (wiki/src/libraries.ts) resolves
them; `FormalFrontier/WidgetTheory` is a made-up repository in a real
organisation, standing in for any of its ~50 repositories. Nothing here is a
claim about Wikipedia or about any library.

The records follow docs/proposals/wikipedia-locate.md §2.7 of the
formal-companion skill (proposal v2, 2026-10-17), with two deliberate
differences, both recorded in docs/ATLAS.md ("Contract notes"):

* every CSL `citation` carries an `id`, which the vendored Literature Links
  schema requires and the proposal's example omits;
* the "Widget lemma" records carry the optional `section` and `prominence`
  fields in the record's locate extension, which WikiLean asks the producer to
  add; the "Gadget (mathematics)" records omit them, so both shapes are tested.

Run it after editing a value here; it rewrites recordset/ (SEAL.json included)
deterministically, and scripts/atlas/test_build_store.py fails while the
committed files differ from what this script writes:

    python3 scripts/atlas/fixtures/make_fixture.py
    python3 scripts/atlas/build_store.py --record-set scripts/atlas/fixtures/recordset \\
        --out scripts/atlas/fixtures/store --replace
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "recordset"
LOCATE = "org.mathlib-initiative.formal-companion.locate"
UNIVERSE_ID = "u-fixture"
CAMPAIGN = "fixture-2026-10"


def fake_hex(label: str, length: int) -> str:
    """A stable fake digest or commit for a label (fixture values only)."""
    return hashlib.sha256(("wikilean atlas fixture: " + label).encode()).hexdigest()[:length]


def dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


LIBRARIES = [
    {"id": "mathlib", "brain_lib": "Mathlib", "display_name": "Mathlib",
     "repo": "https://github.com/leanprover-community/mathlib4", "roots": ["Mathlib"],
     "licence": {"spdx": "Apache-2.0"}},
    # display_name differs from the registry's "Lean core": for a seeded repository
    # the registry's name wins, so a universe cannot relabel a library.
    {"id": "lean-core", "brain_lib": "Lean", "display_name": "Lean 4 core (Init, Std, Lean)",
     "repo": "https://github.com/leanprover/lean4", "roots": ["Init", "Std", "Lean"],
     "licence": {"spdx": "Apache-2.0"}, "src_dir": "src"},
    {"id": "batteries", "brain_lib": "Batteries", "display_name": "Batteries",
     "repo": "https://github.com/leanprover-community/batteries", "roots": ["Batteries"],
     "licence": {"spdx": "Apache-2.0"}},
    {"id": "tauceti", "brain_lib": "TauCeti", "display_name": "TauCeti",
     "repo": "https://github.com/TauCetiProject/TauCeti", "roots": ["TauCeti"],
     "licence": {"spdx": "Apache-2.0"}},
    # No display_name: the Worker's registry names it from the repository pattern.
    {"id": "ff-widgets", "brain_lib": "WidgetTheory",
     "repo": "https://github.com/FormalFrontier/WidgetTheory", "roots": ["WidgetTheory"],
     "licence": {"spdx": "Apache-2.0"}},
]
for _lib in LIBRARIES:
    _lib["commit"] = fake_hex("commit " + _lib["id"], 40)
    _lib["toolchain"] = "leanprover/lean4:v4.24.0"
    _lib["index"] = {"path": f"index/{_lib['id']}/", "sha256": fake_hex("index " + _lib["id"], 64),
                     "declarations": 1000 + len(_lib["id"])}
LIB = {lib["id"]: lib for lib in LIBRARIES}

# The fixture's own reading of "sha over sorted (id, commit, index sha)"
# (proposal §2.3). The consumer treats the value as opaque and checks only that
# SEAL.json, universe.json and every record agree on it.
UNIVERSE_SHA = hashlib.sha256("".join(
    f"{lib['id']}\t{lib['commit']}\t{lib['index']['sha256']}\n"
    for lib in sorted(LIBRARIES, key=lambda lib: lib["id"])).encode()).hexdigest()

UNIVERSE = {
    "id": UNIVERSE_ID,
    "frozen": "2026-10-17",
    "libraries": LIBRARIES,
    "excluded": [{"id": "atlas-lean", "repo": "https://github.com/facebookresearch/atlas-lean",
                  "reason": "licence forbids ML evaluation use"}],
    "universe_sha256": UNIVERSE_SHA,
}

SOURCES = [
    {"source_id": "enwiki-1001-2002", "pageid": 1001, "revision": 2002, "title": "Widget lemma",
     "wikidata": "Q99999001", "html_sha256": fake_hex("html 1001", 64),
     "layer_sha256": fake_hex("layer 1001", 64), "renderer": "web-html/1",
     "assessment": {"class": "B", "importance": "high"}},
    {"source_id": "enwiki-1003-2004", "pageid": 1003, "revision": 2004, "title": "Gadget (mathematics)",
     "wikidata": "Q99999003", "html_sha256": fake_hex("html 1003", 64),
     "layer_sha256": fake_hex("layer 1003", 64), "renderer": "web-html/1",
     "assessment": {"class": "Start", "importance": "mid"}},
]
SOURCE = {source["source_id"]: source for source in SOURCES}


def location_key(source_id: str, item: str, library: str, declaration: str) -> str:
    """The fixture's reading of proposal §2.7's run-free key (opaque to the consumer)."""
    parts = [source_id, item, library, LIB[library]["commit"], declaration]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def subject(source_id: str, quote: dict) -> dict:
    source = SOURCE[source_id]
    uri = ("https://en.wikipedia.org/w/index.php?title=" + source["title"].replace(" ", "_")
           .replace("(", "%28").replace(")", "%29") + f"&oldid={source['revision']}")
    return {
        "type": "html", "uri": uri, "version": str(source["revision"]),
        "location": {"quote": quote},
        "citation": {"id": source_id, "type": "webpage", "title": source["title"],
                     "container-title": "Wikipedia", "URL": uri},
        "extensions": {LOCATE: {
            "pageid": source["pageid"], "html_sha256": source["html_sha256"], "renderer": source["renderer"],
            "span_sha256": fake_hex("span " + source_id + quote["exact"], 64), "licence": "CC-BY-SA-4.0"}},
    }


def passage_ext(item: str, extra: dict, layout: dict | None) -> dict:
    ext = {"item": item, "universe": UNIVERSE_ID, "universe_sha256": UNIVERSE_SHA, **extra}
    if layout:
        ext.update(layout)
    return ext


def location(source_id: str, item: str, kind: str, quote: dict, status: str, loc: dict,
             layout: dict | None) -> dict:
    lib = LIB[loc["library"]]
    record = {
        "id": f"{source_id}::{item}",
        "subject": [subject(source_id, quote)],
        "object": [{
            "type": "lean", "uri": lib["repo"], "version": lib["commit"], "role": "key",
            "location": {"declaration": loc["declaration"], "module": loc["module"]},
            "extensions": {LOCATE: {"library": loc["library"], "path": loc["path"], "line": loc["line"],
                                    "end_line": loc["end_line"], "proof_status": loc["proof_status"]}},
        }],
        "relation": loc["relation"], "status": status, "kind": kind,
        "extensions": {LOCATE: passage_ext(item, {
            "location_key": location_key(source_id, item, loc["library"], loc["declaration"]),
            "rank": loc["rank"], "confidence": loc["confidence"], "challenge": loc["challenge"]}, layout)},
    }
    if "note" in loc:
        record["note"] = loc["note"]
    return record


RECEIPT = {
    "queries": ["balanced widget", "widget equal gadget weight", "Widget.IsBalanced", "gadget weight uniform"],
    "candidates": [
        {"library": "batteries", "declaration": "Batteries.Weighted.IsBalanced", "verdict": "rejected",
         "reason": "balances the weights of a list, not of a widget's gadgets"},
        {"library": "mathlib", "declaration": "Widget.weight", "verdict": "rejected",
         "reason": "defines the weight only, not the balance condition"},
    ],
}
RECEIPT_BYTES = (dumps(RECEIPT) + "\n").encode()
RECEIPT_SHA = hashlib.sha256(RECEIPT_BYTES).hexdigest()


def absence(source_id: str, item: str, kind: str, quote: dict, layout: dict | None) -> dict:
    return {
        "id": f"{source_id}::{item}",
        "subject": [subject(source_id, quote)],
        "object": [{"type": "lean", "uri": lib["repo"], "version": lib["commit"]} for lib in LIBRARIES],
        "relation": "noMatch", "status": "not_formalized", "kind": kind,
        "extensions": {LOCATE: passage_ext(item, {
            "receipt": {"sha256": RECEIPT_SHA, "queries": len(RECEIPT["queries"]),
                        "candidates": len(RECEIPT["candidates"])},
            "challenge": "survived"}, layout)},
    }


def unresolved(source_id: str, item: str, kind: str, quote: dict, layout: dict | None) -> dict:
    return {
        "id": f"{source_id}::{item}",
        "subject": [subject(source_id, quote)],
        "object": [],
        "kind": kind,
        "extensions": {LOCATE: passage_ext(item, {"challenge": "not_required"}, layout)},
    }


def widget_records() -> list[dict]:
    sid = "enwiki-1001-2002"
    p1 = {"exact": "every finite widget admits a spanning gadget",
          "prefix": "The widget lemma states that ", "suffix": " of minimal degree."}
    p1_layout = {"section": ["Statement"], "prominence": "central"}
    p2 = {"exact": "the number of spanning gadgets is at most the widget's rank",
          "prefix": "Moreover, ", "suffix": "."}
    p2_layout = {"section": ["Statement", "Bounds"], "prominence": "supporting"}
    p3 = {"exact": "a widget is balanced if every gadget has equal weight",
          "prefix": "In this article, ", "suffix": "."}
    p3_layout = {"section": ["Definitions"], "prominence": "central"}
    p4 = {"exact": "every balanced widget is the image of a regular one",
          "prefix": "It is conjectured that ", "suffix": "."}
    p4_layout = {"section": ["Open problems"], "prominence": "supporting"}
    return [
        location(sid, "In-text #3fa91c07", "theorem", p1, "formalized", {
            "library": "mathlib", "declaration": "Widget.exists_spanning_gadget",
            "module": "Mathlib.Combinatorics.Widget.Basic", "path": "Mathlib/Combinatorics/Widget/Basic.lean",
            "line": 612, "end_line": 640, "proof_status": "proved", "relation": "exactMatch",
            "rank": 1, "confidence": "high", "challenge": "survived"}, p1_layout),
        location(sid, "In-text #3fa91c07", "theorem", p1, "formalized", {
            "library": "ff-widgets", "declaration": "WidgetTheory.spanning_gadget_exists",
            "module": "WidgetTheory.Spanning", "path": "WidgetTheory/Spanning.lean",
            "line": 88, "end_line": 104, "proof_status": "proved", "relation": "closeMatch",
            "note": "Counts gadgets from 1, where the article counts from 0.",
            "rank": 2, "confidence": "medium", "challenge": "not_required"}, p1_layout),
        location(sid, "In-text #5b0e44a1", "theorem", p2, "statement_only", {
            "library": "tauceti", "declaration": "TauCeti.Widget.gadget_count_le_rank",
            "module": "TauCeti.Widget.Bounds", "path": "TauCeti/Widget/Bounds.lean",
            "line": 41, "end_line": 57, "proof_status": "statement_only", "relation": "exactMatch",
            "rank": 1, "confidence": "high", "challenge": "survived"}, p2_layout),
        absence(sid, "In-text #91aa02de", "definition", p3, p3_layout),
        location(sid, "In-text #91aa02de", "definition", p3, "not_formalized", {
            "library": "batteries", "declaration": "Batteries.Weighted.IsBalanced",
            "module": "Batteries.Data.Weighted", "path": "Batteries/Data/Weighted.lean",
            "line": 10, "end_line": 22, "proof_status": "definition", "relation": "relatedMatch",
            "rank": 1, "confidence": "low", "challenge": "sampled_out"}, p3_layout),
        unresolved(sid, "In-text #0c4d77b1", "conjecture", p4, p4_layout),
    ]


def gadget_records() -> list[dict]:
    sid = "enwiki-1003-2004"
    quote = {"exact": "a gadget is a widget together with a chosen basepoint",
             "prefix": "In combinatorics, ", "suffix": "."}
    return [location(sid, "In-text #7d2c9e10", "definition", quote, "formalized", {
        "library": "mathlib", "declaration": "Gadget",
        "module": "Mathlib.Combinatorics.Widget.Gadget", "path": "Mathlib/Combinatorics/Widget/Gadget.lean",
        "line": 15, "end_line": 28, "proof_status": "definition", "relation": "exactMatch",
        "rank": 1, "confidence": "high", "challenge": "survived"}, None)]


def files() -> dict[str, bytes]:
    out = {
        "universe.json": (dumps(UNIVERSE) + "\n").encode(),
        "sources.jsonl": "".join(dumps(source) + "\n" for source in SOURCES).encode(),
        "records/enwiki-1001-2002.jsonl": "".join(dumps(r) + "\n" for r in widget_records()).encode(),
        "records/enwiki-1003-2004.jsonl": "".join(dumps(r) + "\n" for r in gadget_records()).encode(),
        f"receipts/{RECEIPT_SHA}.json": RECEIPT_BYTES,
    }
    seal = {
        "schema": "cf-locate-recordset/1",
        "campaign": CAMPAIGN,
        "universe": {"id": UNIVERSE_ID, "sha256": UNIVERSE_SHA},
        "sourceset": {"id": "ss-fixture", "manifest_sha256": fake_hex("sourceset manifest", 64)},
        "skill_commit": fake_hex("skill commit", 40),
        "created": "2026-10-17T12:00:00Z",
        "files": {path: hashlib.sha256(data).hexdigest() for path, data in sorted(out.items())},
    }
    out["SEAL.json"] = (json.dumps(seal, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    return out


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    for path, data in files().items():
        target = OUT / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    print(f"wrote {len(files())} files under {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
