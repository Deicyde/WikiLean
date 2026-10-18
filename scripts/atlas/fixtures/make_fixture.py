#!/usr/bin/env python3
"""Write the hand-built sealed locate record set the atlas tests run on.

New code (docs/ATLAS.md, "Input contract"). Every value below is synthetic: two
made-up articles ("Widget lemma", page id 1001; "Gadget (mathematics)", page id
1003), made-up declarations and made-up commits. The repositories are real
library repositories so the library registry (wiki/src/libraries.ts) resolves
them; `FormalFrontier/WidgetTheory` is a made-up repository in a real
organisation, standing in for any of its ~50 repositories. Nothing here is a
claim about Wikipedia or about any library.

The set is in exactly the record-set format formal-companion and WikiLean
agreed (docs/ATLAS.md section 3): SEAL.json with set_id, a campaign token and
byte_budget 0; universe.json with its libraries as a list sorted by id, each with
brain_lib, roots, toolchain and licence {spdx}; every record with its passage's
section and prominence; minimal {type, uri, version} lean ends on the absence;
and one location (FormalFrontier's) whose index records only `line`, so its lean
end has no end_line, as atlas's published index gives none. Its closeMatch was
produced by a challenge downgrade, so its note is the challenger's witness
sentence. The Gadget definition's location is `challenge_pending`: its passage's
prominence was overturned to subject after the challenge ran, so a challenge is owed and
none has judged it (read as unchallenged). Two Gadget theorems show the maintainer's
decision D1 (docs/ATLAS.md, section 3, check 6): a conjunction formalized by two hasPart
locations under a survived coverage claim, and a theorem an equivalence (data, like CRT's
RingEquiv) states exactly. One difference from proposal §2.7 remains, recorded in docs/ATLAS.md
("Contract notes"): every CSL `citation` carries an `id`, which the vendored
Literature Links schema requires and the proposal's example omits.

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
# A campaign id. A run outside any campaign would be "run:<run-id>[+<challenge-run-id>]".
CAMPAIGN = "fixture-2026-10"
SET_ID = "fixture-set"


def fake_hex(label: str, length: int) -> str:
    """A stable fake digest or commit for a label (fixture values only)."""
    return hashlib.sha256(("wikilean atlas fixture: " + label).encode()).hexdigest()[:length]


def dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# Sorted by id, as the agreed universe.json lists them.
LIBRARIES = [
    {"id": "batteries", "brain_lib": "Batteries", "display_name": "Batteries",
     "repo": "https://github.com/leanprover-community/batteries", "roots": ["Batteries"],
     "licence": {"spdx": "Apache-2.0"}},
    # No display_name: the Worker's registry names it from the repository pattern.
    {"id": "ff-widgets", "brain_lib": "WidgetTheory",
     "repo": "https://github.com/FormalFrontier/WidgetTheory", "roots": ["WidgetTheory"],
     "licence": {"spdx": "Apache-2.0"}},
    # display_name differs from the registry's "Lean core": for a seeded repository
    # the registry's name wins, so a universe cannot relabel a library.
    {"id": "lean-core", "brain_lib": "Lean", "display_name": "Lean 4 core (Init, Std, Lean)",
     "repo": "https://github.com/leanprover/lean4", "roots": ["Init", "Std", "Lean"],
     "licence": {"spdx": "Apache-2.0"}},
    {"id": "mathlib", "brain_lib": "Mathlib", "display_name": "Mathlib",
     "repo": "https://github.com/leanprover-community/mathlib4", "roots": ["Mathlib"],
     "licence": {"spdx": "Apache-2.0"}},
    {"id": "tauceti", "brain_lib": "TauCeti", "display_name": "TauCeti",
     "repo": "https://github.com/TauCetiProject/TauCeti", "roots": ["TauCeti"],
     "licence": {"spdx": "Apache-2.0"}},
]
assert [lib["id"] for lib in LIBRARIES] == sorted(lib["id"] for lib in LIBRARIES)
for _lib in LIBRARIES:
    _lib["commit"] = fake_hex("commit " + _lib["id"], 40)
    _lib["toolchain"] = "leanprover/lean4:v4.24.0"
LIB = {lib["id"]: lib for lib in LIBRARIES}
# Each library index's sha. The universe sha covers it, but universe.json does not
# carry it (the agreed format has no index field).
INDEX_SHA = {lib["id"]: fake_hex("index " + lib["id"], 64) for lib in LIBRARIES}

# The fixture's own reading of "sha over sorted (id, commit, index sha)"
# (proposal §2.3). The consumer treats the value as opaque and checks only that
# SEAL.json, universe.json and every record agree on it.
UNIVERSE_SHA = hashlib.sha256("".join(
    f"{lib['id']}\t{lib['commit']}\t{INDEX_SHA[lib['id']]}\n"
    for lib in sorted(LIBRARIES, key=lambda lib: lib["id"])).encode()).hexdigest()

UNIVERSE = {
    "id": UNIVERSE_ID,
    "universe_sha256": UNIVERSE_SHA,
    "frozen": "2026-10-17",
    "libraries": LIBRARIES,
    "excluded": [{"id": "atlas-lean", "reason": "licence forbids ML evaluation use"}],
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


def passage_ext(item: str, extra: dict, layout: dict) -> dict:
    """The record's locate extension: every record carries its passage's section and prominence."""
    return {"item": item, "universe": UNIVERSE_ID, "universe_sha256": UNIVERSE_SHA,
            "section": layout["section"], "prominence": layout["prominence"], **extra}


def location(source_id: str, item: str, kind: str, quote: dict, status: str, loc: dict,
             layout: dict, coverage: dict | None = None) -> dict:
    lib = LIB[loc["library"]]
    end_ext = {"library": loc["library"], "path": loc["path"], "line": loc["line"],
               "proof_status": loc["proof_status"]}
    if "end_line" in loc:  # absent when the library index records only line
        end_ext["end_line"] = loc["end_line"]
    record = {
        "id": f"{source_id}::{item}",
        "subject": [subject(source_id, quote)],
        "object": [{
            "type": "lean", "uri": lib["repo"], "version": lib["commit"], "role": "key",
            "location": {"declaration": loc["declaration"], "module": loc["module"]},
            "extensions": {LOCATE: end_ext},
        }],
        "relation": loc["relation"], "status": status, "kind": kind,
        "extensions": {LOCATE: passage_ext(item, {
            "location_key": location_key(source_id, item, loc["library"], loc["declaration"]),
            "rank": loc["rank"], "confidence": loc["confidence"], "challenge": loc["challenge"],
            # The passage's coverage claim (decision D1), on every record of the passage.
            **({"coverage": coverage} if coverage is not None else {})}, layout)},
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


def absence(source_id: str, item: str, kind: str, quote: dict, layout: dict) -> dict:
    return {
        "id": f"{source_id}::{item}",
        "subject": [subject(source_id, quote)],
        # One minimal lean end per universe library, in universe order: no role, no extensions.
        "object": [{"type": "lean", "uri": lib["repo"], "version": lib["commit"]} for lib in LIBRARIES],
        "relation": "noMatch", "status": "not_formalized", "kind": kind,
        "extensions": {LOCATE: passage_ext(item, {
            "receipt": {"sha256": RECEIPT_SHA, "queries": len(RECEIPT["queries"]),
                        "candidates": len(RECEIPT["candidates"])},
            "challenge": "survived"}, layout)},
    }


def unresolved(source_id: str, item: str, kind: str, quote: dict, layout: dict) -> dict:
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
    # The article's subject: the theorem the article is named for.
    p1_layout = {"section": ["Statement"], "prominence": "subject"}
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
            # Line only, as atlas's published index gives it: no end_line.
            "line": 88, "proof_status": "proved", "relation": "closeMatch",
            # A challenge downgraded the worker's exactMatch: the note is the challenger's witness.
            "note": "Counts gadgets from 1, where the article counts from 0.",
            "rank": 2, "confidence": "medium", "challenge": "downgraded"}, p1_layout),
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
    # The lead's defining sentence: no heading above it, and the article's subject.
    layout = {"section": [], "prominence": "subject"}
    module, path = "Mathlib.Combinatorics.Widget.Gadget", "Mathlib/Combinatorics/Widget/Gadget.lean"
    # A conjunction no one declaration states: two hasPart locations, each proved, which
    # together state both clauses. The challenger confirmed the coverage claim, so by decision
    # D1 the passage is formalized (each part alone gives only partial).
    both = {"exact": "every gadget is connected and its basepoint has finite degree",
            "prefix": "By construction, ", "suffix": "."}
    both_item = "In-text #a41f6c2e"
    both_layout = {"section": ["Properties"], "prominence": "central"}
    both_parts = [("Gadget.connected", 44, 51, 1, "high"), ("Gadget.basepoint_degree_finite", 53, 58, 2, "medium")]
    claim = {"complete": True, "challenge": "survived",
             "parts": [location_key(sid, both_item, "mathlib", name) for name, *_ in both_parts]}
    # A theorem that a data declaration states: an equivalence, as CRT's RingEquiv states the
    # Chinese remainder theorem. Its proof_status is definition and it carries no statement flag;
    # by decision D1 an exact match to it formalizes the theorem.
    equiv = {"exact": "the gadgets on a widget correspond bijectively to its vertices",
             "prefix": "Moreover, ", "suffix": "."}
    equiv_layout = {"section": ["Properties"], "prominence": "supporting"}
    return [location(sid, "In-text #7d2c9e10", "definition", quote, "formalized", {
        "library": "mathlib", "declaration": "Gadget",
        "module": module, "path": path,
        "line": 15, "end_line": 28, "proof_status": "definition", "relation": "exactMatch",
        # Planned under `supporting` and overturned to `subject` after the challenge ran: a
        # rule now requires a challenge nobody has run, so the record is challenge_pending.
        "rank": 1, "confidence": "high", "challenge": "challenge_pending"}, layout),
        *[location(sid, both_item, "theorem", both, "formalized", {
            "library": "mathlib", "declaration": name, "module": module, "path": path,
            "line": line, "end_line": end_line, "proof_status": "proved", "relation": "hasPart",
            "rank": rank, "confidence": confidence, "challenge": "survived"}, both_layout, coverage=claim)
          for name, line, end_line, rank, confidence in both_parts],
        location(sid, "In-text #c93b0d58", "theorem", equiv, "formalized", {
            "library": "mathlib", "declaration": "Gadget.equivVertex", "module": module, "path": path,
            "line": 60, "end_line": 74, "proof_status": "definition", "relation": "exactMatch",
            "rank": 1, "confidence": "high", "challenge": "survived"}, equiv_layout),
    ]


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
        "set_id": SET_ID,
        "campaign": CAMPAIGN,
        "universe": {"id": UNIVERSE_ID, "universe_sha256": UNIVERSE_SHA},
        "sourceset": {"id": "ss-fixture", "manifest_sha256": fake_hex("sourceset manifest", 64)},
        "skill_commit": fake_hex("skill commit", 40),
        "created": "2026-10-17T12:00:00Z",
        # The export's default for a sealed set: no per-source byte budget.
        "byte_budget": 0,
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
