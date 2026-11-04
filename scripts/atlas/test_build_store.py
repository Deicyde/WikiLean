#!/usr/bin/env python3
"""Tests for scripts/atlas/build_store.py, the atlas store builder (docs/ATLAS.md).

New code. Runs offline on the hand-built fixture under scripts/atlas/fixtures/:
the committed record set must equal what make_fixture.py writes, every record must
pass the vendored Literature Links schema, the committed golden store must equal a
fresh build, and each broken variant of the record set must be refused with a
message naming the rule it breaks. The Worker tests (wiki/test/atlas.test.ts) read
the same golden store, so the two languages share one contract fixture.

A cf-locate-recordset/2 set (status rules 2 and 3) is the same fixture converted in a scratch
copy (RecordSetTwoTests.to_v2): its revision-free keys, its coverage claim as a bundle and its
seal's status rule, each checked and refused by name; the producer's own answers per rule
(producer_derive_status-<rule>.json) and its parity tables (status_parity-<rule>.json) pin
the rules. CombinedSetTests builds the fixture's two pages from two sets into one store.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "fixtures"))

import build_store  # noqa: E402
import make_fixture  # noqa: E402

RECORDSET = HERE / "fixtures" / "recordset"
STORE = HERE / "fixtures" / "store"
PRODUCER_TABLE = HERE / "fixtures" / "producer_derive_status.json"
# The parity table both sides run: formal-companion's tests/data/locate/status-parity.json at
# the commit docs/ATLAS.md section 3, check 6 names, byte for byte.
SHARED_TABLE = HERE / "fixtures" / "status_parity.json"
SHARED_TABLE_SHA256 = "0e8d8adbabdb66e32eaa262e55dfa1e098beb6670b7324c59fae42d3b98edda4"
# Status rules 2 and 3 (cf-locate-recordset/2): the producer's truth table per rule
# (make_producer_table.py --status-rule) and its hand-written parity table per rule, copied from
# formal-companion tests/data/locate/status-parity-{2,3}.json at PRODUCER_COMMIT, the commit both
# Tier A batch 1 seals name as their skill_commit. Each copy is the producer's byte for byte but
# for its decision's text, which is shortened here: per rule, the copy's sha256, and the sha256 of
# the table with that text left out (parity_body_sha256), which the producer's file gives too.
PRODUCER_COMMIT = "d85385aa4eb2818373e9b6b8b41b765ffaf53750"
PRODUCER_TABLES = {rule: HERE / "fixtures" / f"producer_derive_status-{rule}.json" for rule in (2, 3)}
SHARED_TABLES = {
    2: (HERE / "fixtures" / "status_parity-2.json", "cd0cac59a9da22253ad024779a0a3ea00823ad891faa383c34f59de4818605bd",
        "497bd9b7a24aa638052ef7da379561f0d6070b2c46424b2852a4b5bb6add5e3c"),
    3: (HERE / "fixtures" / "status_parity-3.json", "675bc0402ee9355b4795d4e446c04aae6e7eebb3613131de07f5261c27aef207",
        "247de420c45eb3f294258c6c757117a53564796dc0045fb236b4e5b635ff91cd"),
}


def parity_body_sha256(doc: dict) -> str:
    """sha256 of a parity table with its decision's text left out, keys sorted, compact separators."""
    body = json.loads(json.dumps(doc))
    del body["decision"]["text"]
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                          .encode("utf-8")).hexdigest()
LOCATE = build_store.LOCATE
# How a stamped status that differs from the recomputed one is refused.
DERIVES = "the derived-status rules (proposal §2.6 and vocabulary.md §1.11) derive"


def reseal(set_dir: Path) -> None:
    """Recompute SEAL.json's file map after a test edits the copy (what a producer would do)."""
    seal_path = set_dir / "SEAL.json"
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    seal["files"] = {p.relative_to(set_dir).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in sorted(set_dir.rglob("*")) if p.is_file() and p.name != "SEAL.json"}
    seal_path.write_text(json.dumps(seal, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(make_fixture.dumps(row) + "\n" for row in rows), encoding="utf-8")


class FixtureTests(unittest.TestCase):
    def test_committed_record_set_is_what_make_fixture_writes(self):
        expected = make_fixture.files()
        on_disk = {p.relative_to(RECORDSET).as_posix(): p.read_bytes()
                   for p in RECORDSET.rglob("*") if p.is_file()}
        self.assertEqual(sorted(on_disk), sorted(expected))
        for path, data in expected.items():
            self.assertEqual(on_disk[path], data, f"{path} differs; rerun make_fixture.py")

    def test_every_record_passes_the_vendored_schema(self):
        from jsonschema import Draft202012Validator
        schema_path = ROOT / "wiki/src/vendor/literature-links/companion.schema.json"
        validator = Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8")))
        records = [r for p in sorted((RECORDSET / "records").glob("*.jsonl")) for r in read_jsonl(p)]
        self.assertEqual(len(records), 10)
        for record in records:
            errors = list(validator.iter_errors(record))
            self.assertEqual(errors, [], record["id"])

    def test_fixture_covers_the_required_outcomes(self):
        records = read_jsonl(RECORDSET / "records/enwiki-1001-2002.jsonl")
        shapes = {(r.get("relation"), r.get("status"),
                   r["object"][0].get("extensions", {}).get(LOCATE, {}).get("library") if r["object"] else None,
                   r["object"][0].get("extensions", {}).get(LOCATE, {}).get("proof_status") if r["object"] else None)
                  for r in records}
        self.assertIn(("exactMatch", "formalized", "mathlib", "proved"), shapes)
        self.assertIn(("closeMatch", "formalized", "ff-widgets", "proved"), shapes)
        self.assertIn(("exactMatch", "statement_only", "tauceti", "statement_only"), shapes)
        self.assertIn(("noMatch", "not_formalized", None, None), shapes)
        self.assertIn((None, None, None, None), shapes)  # unresolved: object [] and no status
        absence = next(r for r in records if r.get("relation") == "noMatch")
        self.assertEqual(len(absence["object"]), 5)
        self.assertTrue(all(set(end) == {"type", "uri", "version"} for end in absence["object"]))
        # Decision D1's two cases: a conjunction its hasPart locations cover together (a survived
        # coverage claim on every record of the passage), and a theorem a data declaration states.
        gadget = read_jsonl(RECORDSET / "records/enwiki-1003-2004.jsonl")
        covered = [r for r in gadget if "coverage" in r["extensions"][LOCATE]]
        self.assertEqual([(r["kind"], r["relation"], r["status"]) for r in covered],
                         [("theorem", "hasPart", "formalized")] * 2)
        claim = covered[0]["extensions"][LOCATE]["coverage"]
        self.assertEqual(claim, covered[1]["extensions"][LOCATE]["coverage"])
        self.assertEqual(claim["parts"], [r["extensions"][LOCATE]["location_key"] for r in covered])
        self.assertEqual((claim["complete"], claim["challenge"]), (True, "survived"))
        data = [r for r in gadget if r["kind"] == "theorem" and r["relation"] == "exactMatch"]
        self.assertEqual(len(data), 1)
        end = data[0]["object"][0]["extensions"][LOCATE]
        self.assertEqual((end["proof_status"], end.get("statement"), data[0]["status"]),
                         ("definition", None, "formalized"))

    def test_fixture_is_in_the_agreed_format(self):
        seal = json.loads((RECORDSET / "SEAL.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(seal), sorted(build_store.SEAL_KEYS))
        self.assertEqual(sorted(seal["universe"]), ["id", "universe_sha256"])
        self.assertEqual(seal["byte_budget"], 0)
        universe = json.loads((RECORDSET / "universe.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(universe), sorted(build_store.UNIVERSE_KEYS))
        ids = [lib["id"] for lib in universe["libraries"]]
        self.assertEqual(ids, sorted(ids))
        for lib in universe["libraries"]:
            self.assertLessEqual(set(build_store.LIBRARY_KEYS), set(lib))
        records = [r for p in sorted((RECORDSET / "records").glob("*.jsonl")) for r in read_jsonl(p)]
        for record in records:
            ext = record["extensions"][LOCATE]
            self.assertIsInstance(ext["section"], list, record["id"])
            self.assertIn(ext["prominence"], build_store.PROMINENCES, record["id"])
        # FormalFrontier's index, like atlas's published one, records only line.
        ends = {r["object"][0]["extensions"][LOCATE]["library"]: r["object"][0]["extensions"][LOCATE]
                for r in read_jsonl(RECORDSET / "records/enwiki-1001-2002.jsonl")
                if r.get("relation") not in (None, "noMatch")}
        self.assertNotIn("end_line", ends["ff-widgets"])
        self.assertEqual(ends["mathlib"]["end_line"], 640)


class BuildTests(unittest.TestCase):
    def test_golden_store_matches_a_fresh_build(self):
        tree = build_store.build(RECORDSET)
        self.assertEqual(build_store.check_tree(STORE, tree), [])

    def test_build_is_deterministic_and_built_at_only_touches_current(self):
        first = build_store.build(RECORDSET)
        self.assertEqual(first, build_store.build(RECORDSET))
        later = build_store.build(RECORDSET, built_at="2026-10-18T00:00:00Z")
        changed = sorted(p for p in first if first[p] != later[p])
        self.assertEqual(changed, ["atlas/v1/current.json", "atlas/v1/manifest.json"])
        with self.assertRaises(build_store.AtlasError):
            build_store.build(RECORDSET, built_at="yesterday")

    def test_manifest_lists_every_other_file(self):
        tree = build_store.build(RECORDSET)
        manifest = json.loads(tree["atlas/v1/manifest.json"])
        self.assertEqual(sorted(manifest["files"]), sorted(p for p in tree if p != "atlas/v1/manifest.json"))
        for path, entry in manifest["files"].items():
            self.assertEqual(entry["sha256"], hashlib.sha256(tree[path]).hexdigest())
            self.assertEqual(entry["bytes"], len(tree[path]))

    def test_records_are_copied_verbatim(self):
        tree = build_store.build(RECORDSET)
        self.assertEqual(tree["atlas/v1/records/1001.jsonl"],
                         (RECORDSET / "records/enwiki-1001-2002.jsonl").read_bytes())
        article = json.loads(tree["atlas/v1/articles/1001.json"])
        self.assertEqual(article["records_sha256"],
                         hashlib.sha256(tree["atlas/v1/records/1001.jsonl"]).hexdigest())

    def test_article_view_model(self):
        article = json.loads(build_store.build(RECORDSET)["atlas/v1/articles/1001.json"])
        self.assertEqual(article["schema"], "wikilean.atlas.article/1")
        self.assertEqual((article["pageid"], article["title"], article["revision"]), (1001, "Widget lemma", 2002))
        # The text it quotes is the source's, under the licence its records name; the mapping is CC0.
        self.assertEqual(article["licence"], {"text": "CC-BY-SA-4.0", "mapping": "CC0-1.0"})
        self.assertEqual(article["campaign"], "fixture-2026-10")
        self.assertEqual([p["status"] for p in article["passages"]],
                         ["formalized", "statement_only", "not_formalized", None])
        self.assertEqual([p["outcome"] for p in article["passages"]], ["located", "located", "absent", "unresolved"])
        first = article["passages"][0]
        self.assertEqual([loc["library"] for loc in first["locations"]], ["mathlib", "ff-widgets"])
        self.assertEqual((first["locations"][0]["line"], first["locations"][0]["end_line"]), (612, 640))
        # A lean end without end_line keeps line, and the view model omits end_line.
        self.assertEqual(first["locations"][1]["line"], 88)
        self.assertNotIn("end_line", first["locations"][1])
        self.assertEqual(first["locations"][1]["challenge"], "downgraded")
        self.assertEqual(first["locations"][1]["note"], "Counts gadgets from 1, where the article counts from 0.")
        self.assertEqual(first["section"], ["Statement"])
        self.assertEqual(first["prominence"], "subject")
        self.assertEqual([p["prominence"] for p in article["passages"]], ["subject", "supporting", "central", "supporting"])
        absent = article["passages"][2]
        self.assertEqual(absent["absence"]["libraries"], 5)
        self.assertEqual(absent["absence"]["universe"], "u-fixture")
        self.assertEqual(absent["locations"][0]["relation"], "relatedMatch")
        self.assertEqual(article["passages"][3]["unresolved"], {"challenge": "not_required"})
        # The basis is the location that justifies the status; absent and unresolved have none.
        self.assertEqual([p["basis"] for p in article["passages"]],
                         [first["locations"][0]["location_key"], article["passages"][1]["locations"][0]["location_key"],
                          None, None])
        summary = article["summary"]
        self.assertEqual(summary["by_status"], {"formalized": 1, "statement_only": 1, "partial": 0,
                                                "not_formalized": 1, "unresolved": 1})
        self.assertEqual(summary["by_library"]["batteries"],
                         {"locations": 0, "complete": 0, "incomplete": 0, "passages": 0, "related": 1})
        self.assertEqual(summary["by_library"]["mathlib"],
                         {"locations": 1, "complete": 1, "incomplete": 0, "passages": 1, "related": 0})
        # A statement-only location is a location, but not a complete one.
        self.assertEqual(summary["by_library"]["tauceti"],
                         {"locations": 1, "complete": 0, "incomplete": 1, "passages": 1, "related": 0})
        # The small article's one passage is its lead's defining sentence: no heading, the subject.
        gadget = json.loads(build_store.build(RECORDSET)["atlas/v1/articles/1003.json"])
        self.assertEqual(gadget["passages"][0]["section"], [])
        self.assertEqual(gadget["passages"][0]["prominence"], "subject")
        # Overturned to subject after the challenge ran: a challenge is owed, none has judged it.
        self.assertEqual(gadget["passages"][0]["locations"][0]["challenge"], "challenge_pending")
        self.assertEqual(gadget["passages"][0]["status"], "formalized")
        # No passage of the Widget article carries a coverage claim.
        self.assertEqual([p["coverage"] for p in article["passages"]], [None] * 4)
        # Decision D1. The conjunction: each hasPart part alone is partial, and the survived
        # claim over both formalizes it; the claim, not a location, is the basis.
        covered = gadget["passages"][1]
        self.assertEqual((covered["kind"], covered["status"], covered["basis"]), ("theorem", "formalized", None))
        self.assertEqual([loc["relation"] for loc in covered["locations"]], ["hasPart", "hasPart"])
        self.assertEqual(covered["coverage"], {
            "complete": True, "parts": [loc["location_key"] for loc in covered["locations"]],
            "challenge": "survived", "status": "formalized", "basis": True})
        # The theorem an equivalence states: an exact match to data, formalized, with that basis.
        equiv = gadget["passages"][2]
        self.assertEqual((equiv["kind"], equiv["status"], equiv["coverage"]), ("theorem", "formalized", None))
        self.assertEqual((equiv["locations"][0]["proof_status"], equiv["locations"][0]["statement"]),
                         ("definition", False))
        self.assertEqual(equiv["basis"], equiv["locations"][0]["location_key"])
        self.assertEqual(gadget["summary"]["by_status"], {"formalized": 3, "statement_only": 0, "partial": 0,
                                                          "not_formalized": 0, "unresolved": 0})
        self.assertEqual(gadget["summary"]["by_library"]["mathlib"],
                         {"locations": 4, "complete": 4, "incomplete": 0, "passages": 3, "related": 0})

    def test_a_quote_at_the_cap_is_marked_when_it_is_a_capped_head(self):
        # The export quotes a passage's first 12 words (proposal §2.7's verbatim cap) and stamps
        # span_sha256, the sha256 of the whole passage, so the builder tells a capped head from a
        # whole passage, and the page marks only the head with an ellipsis.
        tree = build_store.build(RECORDSET)
        widget = json.loads(tree["atlas/v1/articles/1001.json"])
        gadget = json.loads(tree["atlas/v1/articles/1003.json"])
        self.assertEqual([p["quote_capped"] for p in widget["passages"]], [False, True, False, False])
        self.assertEqual([p["quote_capped"] for p in gadget["passages"]], [False, False, False])
        # The head has no suffix, so the rest of its passage is never within one.
        for passage in widget["passages"] + gadget["passages"]:
            self.assertIs(passage["passage_ends_in_suffix"], False)
        head, whole = widget["passages"][1], gadget["passages"][2]
        # Both are at the cap; only the bound is longer than its quote. The quote stays verbatim.
        for passage in (head, whole):
            self.assertEqual(len(passage["quote"]["exact"].split()), build_store.QUOTE_MAX_WORDS)
        self.assertEqual(head["quote"], {"exact": "Moreover, the number of spanning gadgets of a widget is at most"})
        self.assertEqual(whole["quote"],
                         {"exact": "Moreover, the gadgets on a widget correspond bijectively to all its vertices."})

    def test_quote_capped_reads_the_cap_and_the_span_sha(self):
        twelve = {"exact": " ".join(f"w{i}" for i in range(12))}
        eleven = {"exact": " ".join(f"w{i}" for i in range(11))}
        own = hashlib.sha256(twelve["exact"].encode()).hexdigest()
        other = "0" * 64
        # Under the cap the passage ended first: whole, whatever the span sha says.
        for span in (own, other, None):
            self.assertFalse(build_store.quote_capped(eleven, span))
        self.assertFalse(build_store.quote_capped({"exact": ""}, other))
        # At the cap: whole exactly when the quote is the whole span.
        self.assertFalse(build_store.quote_capped(twelve, own))
        self.assertTrue(build_store.quote_capped(twelve, other))
        # No usable span sha (the builder refuses one, subject_problems): a quote at the cap reads as a head.
        for span in (None, 5, True, [], {}, "x", own.upper(), own + "\n"):
            self.assertTrue(build_store.quote_capped(twelve, span), repr(span))

    def test_a_whole_passage_may_end_in_whitespace(self):
        # The export right-strips the quote but hashes the span as it is, and a block span can
        # keep the space that ends its paragraph: a hair space, a no-break space, or a space and
        # a no-break space on the producer's real source set (docs/ATLAS.md section 4).
        twelve = {"exact": " ".join(f"w{i}" for i in range(12))}
        sha = lambda text: hashlib.sha256(text.encode()).hexdigest()
        for tail in ("\u200a", "\xa0", " \xa0", "\u2009", "\u3000", " ", "\n", "\t", " \n", "\xa0\xa0", "\r\n"):
            self.assertEqual(build_store.quote_extent(twelve, sha(twelve["exact"] + tail)),
                             {"quote_capped": False, "passage_ends_in_suffix": False}, repr(tail))
        # A longer run of trailing whitespace is not tried: the quote reads as a head (the safe side).
        self.assertTrue(build_store.quote_capped(twelve, sha(twelve["exact"] + "   ")))
        # Whitespace runs only: a word after the quote is a longer passage.
        self.assertTrue(build_store.quote_capped(twelve, sha(twelve["exact"] + " w12")))
        # The runs are every whitespace character the export's rstrip and \s word split know.
        everything = tuple(ch for ch in map(chr, range(0x110000)) if ch.isspace())
        self.assertEqual(build_store._SPACE_CHARS, everything)
        self.assertTrue(all(build_store.WORD_SPACE.fullmatch(ch) for ch in everything))
        self.assertEqual(len(build_store.TRAILING_SPACE), 1 + len(everything) + len(everything) ** 2)

    def test_a_capped_head_whose_passage_ends_in_its_suffix(self):
        # A quote that is not unique on the page carries a suffix, the text right after it, which
        # may hold the rest of the passage and run on past its end (formal-companion
        # records.cap_quote widens it from the layer, not from the passage).
        exact = "Every group of odd order is solvable, and so is every group"
        sha = lambda text: hashlib.sha256(text.encode()).hexdigest()
        ends = {"quote_capped": True, "passage_ends_in_suffix": True}
        head = {"quote_capped": True, "passage_ends_in_suffix": False}
        whole = {"quote_capped": False, "passage_ends_in_suffix": False}
        cases = [
            # The passage ends with the suffix, inside it, or with it and its trailing space.
            (" of order 2.", exact + " of order 2.", ends),
            (" of order 2. Later: Every", exact + " of order 2.", ends),
            (" of order 2. Later: Every", exact + " of", ends),
            (" of order 2.", exact + " of order 2.\xa0", ends),
            # The passage goes on past the suffix.
            (" of order 2", exact + " of order 2 or 3.", head),
            # Whitespace alone after the quote, in the suffix or past it, is a whole passage.
            ("\xa0Later:", exact + "\xa0", whole),
            ("   Later:", exact + "   ", whole),
            (" ", exact + " \n", whole),
        ]
        for suffix, span, expected in cases:
            self.assertEqual(build_store.quote_extent({"exact": exact, "suffix": suffix}, sha(span)), expected,
                             repr((suffix, span)))
        # Without a usable span sha, a head's passage goes on past its suffix (the safe side).
        self.assertEqual(build_store.quote_extent({"exact": exact, "suffix": " of order 2."}, None), head)

    def test_a_span_past_a_worded_suffix_is_never_whole(self) -> None:
        # Inconsistent producer data: the span runs one or two characters past a suffix with
        # words in it, while span_sha256 hashes the bare quote. The span proves the passage goes
        # on past the quote, so the hash may not make it whole.
        exact = "Every group of odd order is solvable, and so is every group"
        quote = {"exact": exact, "suffix": " of order 2."}
        sha = hashlib.sha256(exact.encode("utf-8")).hexdigest()
        for extra in (1, 2):
            span = [0, len(exact) + len(quote["suffix"]) + extra]
            got = build_store.quote_extent(quote, sha, span)
            self.assertTrue(got["quote_capped"], extra)
        # A suffix of whitespace alone proves nothing, so the hash still decides there.
        spaced = {"exact": exact, "suffix": "  "}
        self.assertFalse(build_store.quote_extent(spaced, sha, [0, len(exact) + 3])["quote_capped"])

    def test_the_span_is_a_shortcut_to_what_the_span_sha_gives(self):
        # F5's span gives the passage's length, so the text after exact is tail = (end - start)
        # - len(exact) characters. Where the suffix covers it, or the tail runs past the suffix by
        # more than any trailing-space run, the span alone decides; one or two characters past
        # the suffix, only span_sha256 says whether they are whitespace. Either way the answer
        # is the one span_sha256 gives without the span.
        exact = "Every group of odd order is solvable, and so is every group"
        sha = lambda text: hashlib.sha256(text.encode()).hexdigest()
        ends = {"quote_capped": True, "passage_ends_in_suffix": True}
        head = {"quote_capped": True, "passage_ends_in_suffix": False}
        whole = {"quote_capped": False, "passage_ends_in_suffix": False}
        self.assertEqual(build_store.MAX_TRAILING_SPACE, 2)
        cases = [
            # (suffix, the passage's text, what the quote shows, whether the span alone decides)
            ("", exact, whole, True),
            ("", exact + " ", whole, False),
            ("", exact + " \xa0", whole, False),
            ("", exact + "   ", head, True),
            ("", exact + " and more.", head, True),
            (" of order 2.", exact + " of order 2.", ends, True),
            (" of order 2. Later: Every", exact + " of", ends, True),
            (" of order 2.", exact + " of order 2.\xa0", ends, False),
            (" of order 2", exact + " of order 2 or 3.", head, True),
            ("\xa0Later:", exact + "\xa0", whole, True),
            ("   Later:", exact + "   ", whole, True),
            (" ", exact + " \n", whole, False),
        ]
        for suffix, text, expected, alone in cases:
            quote = {"exact": exact, "suffix": suffix}
            for start in (0, 7):
                span = [start, start + len(text)]
                with self.subTest(suffix=suffix, text=text[len(exact):], start=start):
                    self.assertEqual(build_store.quote_extent(quote, sha(text)), expected)
                    self.assertEqual(build_store.quote_extent(quote, sha(text), span), expected)
                    # With no usable span sha, the span decides where it can, and the rest is a head.
                    self.assertEqual(build_store.quote_extent(quote, None, span), expected if alone else head)
                    self.assertEqual(build_store.quote_capped(quote, sha(text), span), expected["quote_capped"])
        # Under the cap the passage ended first: whole, whatever the span.
        eleven = {"exact": " ".join(f"w{i}" for i in range(11))}
        self.assertEqual(build_store.quote_extent(eleven, None, [0, len(eleven["exact"]) + 50]), whole)

    def test_counts_by_prominence(self):
        # The subject and central passages are the article's statements: the pages count them
        # apart from the supporting ones, per article and in the totals.
        tree = build_store.build(RECORDSET)
        current = json.loads(tree["atlas/v1/current.json"])
        zero = dict.fromkeys(build_store.SUMMARY_STATUSES, 0)
        widget = json.loads(tree["atlas/v1/articles/1001.json"])["summary"]
        self.assertEqual(widget["by_prominence"], {
            "subject": {"passages": 1, "by_status": {**zero, "formalized": 1}},
            "central": {"passages": 1, "by_status": {**zero, "not_formalized": 1}},
            "supporting": {"passages": 2, "by_status": {**zero, "statement_only": 1, "unresolved": 1}}})
        gadget = json.loads(tree["atlas/v1/articles/1003.json"])["summary"]
        self.assertEqual(gadget["by_prominence"], {
            prominence: {"passages": 1, "by_status": {**zero, "formalized": 1}}
            for prominence in ("subject", "central", "supporting")})
        totals = current["counts"]
        self.assertEqual(totals["by_prominence"], {
            "subject": {"passages": 2, "by_status": {**zero, "formalized": 2}},
            "central": {"passages": 2, "by_status": {**zero, "formalized": 1, "not_formalized": 1}},
            "supporting": {"passages": 3, "by_status": {**zero, "formalized": 1, "statement_only": 1,
                                                        "unresolved": 1}}})
        # Every passage is counted once, under its own prominence.
        for summary in (widget, gadget, totals):
            buckets = summary["by_prominence"].values()
            self.assertEqual(sum(b["passages"] for b in buckets), summary["passages"])
            for status in build_store.SUMMARY_STATUSES:
                self.assertEqual(sum(b["by_status"][status] for b in buckets), summary["by_status"][status])

    def test_index_and_title_keys(self):
        self.assertEqual(build_store.title_key("Widget_lemma"), "Widget lemma")
        self.assertEqual(build_store.title_key("  Gadget\u00a0(mathematics) "), "Gadget (mathematics)")
        # wiki/test/atlas.test.ts pins the same shards for the same titles.
        self.assertEqual(build_store.title_shard("Widget lemma"), "1")
        self.assertEqual(build_store.title_shard("Gadget (mathematics)"), "8")
        tree = build_store.build(RECORDSET)
        current = json.loads(tree["atlas/v1/current.json"])
        self.assertEqual(current["index"], {"hex": 1, "shards": ["1", "8"], "entries": 2})
        shard = json.loads(tree["atlas/v1/index/1.json"])
        # The last field lists the libraries holding a location, in universe order, which
        # is by id (Batteries holds only a related declaration).
        self.assertEqual(shard["articles"], [["Widget lemma", 1001, 2002, 4, 1, 1, 0, 1, 1, "high",
                                              ["ff-widgets", "mathlib", "tauceti"]]])
        self.assertEqual((current["record_set"]["set_id"], current["record_set"]["byte_budget"]), ("fixture-set", 0))
        self.assertEqual(shard["fields"][0:3], ["title", "pageid", "revision"])
        self.assertEqual(shard["fields"][-1], "libraries")
        totals = current["counts"]["by_library"]
        self.assertEqual(totals["tauceti"], {"articles": 1, "passages": 1, "locations": 1, "complete": 0,
                                             "incomplete": 1, "related": 0})


class DeriveStatusParityTests(unittest.TestCase):
    """build_store.derive_status against the producer's own answers (make_producer_table.py)."""

    def setUp(self):
        self.table = json.loads(PRODUCER_TABLE.read_text(encoding="utf-8"))

    def test_table_is_the_producers(self):
        self.assertEqual(self.table["producer"]["repository"], "formal-companion")
        self.assertRegex(self.table["producer"]["commit"], r"^[0-9a-f]{40}$")
        cases = self.table["cases"]
        plain = [case for case in cases if len(case) == 3]
        singles = [case for case in plain if len(case[1]) == 1]
        # Every kind x relation x proof status x statement flag, once each.
        self.assertEqual(len(singles), 7 * 6 * 5 * 2)
        self.assertEqual(len({json.dumps(case[:2]) for case in singles}), len(singles))
        self.assertGreaterEqual(len(plain) - len(singles), 300)
        # Decision D1's coverage claims: every kind x every pair of parts under a survived claim.
        covered = [case for case in cases if len(case) == 4]
        survived_pairs = {(case[0], json.dumps(case[1])) for case in covered
                          if case[3] == {"parts": [0, 1], "challenge": "survived"} and len(case[1]) == 2}
        self.assertEqual(len(survived_pairs), 7 * 9 * 9)
        self.assertEqual({case[3]["challenge"] for case in covered}, build_store.CHALLENGES)
        # The producer the table came from implements D1: a theorem's exact match to data formalizes it.
        self.assertIn(["theorem", [["exactMatch", "definition", False]], "formalized"], singles)

    def test_builder_derives_exactly_the_producers_status(self):
        mismatches = producer_mismatches(self, self.table["cases"], 1)
        self.assertEqual(mismatches, [], f"{len(mismatches)} cases differ from the producer")


def producer_mismatches(test: unittest.TestCase, cases: list, status_rule: int) -> list:
    """build_store.derive_status (by `status_rule`) on each case of a producer table, against its answer."""
    mismatches = []
    for kind, ends, expected, *claimed in cases:
        problems = [build_store.statement_flag(statement, proof_status)[1] for _, proof_status, statement in ends]
        if expected == "refused":
            if not any(problems):
                mismatches.append((kind, ends, expected, "accepted"))
            continue
        if any(problems):
            mismatches.append((kind, ends, expected, f"refused: {problems}"))
            continue
        locations = [{"relation": relation, "proof_status": proof_status, "statement": statement,
                      "rank": rank, "library": "lib", "location_key": hashlib.sha256(b"key%d" % rank).hexdigest()}
                     for rank, (relation, proof_status, statement) in enumerate(ends, 1)]
        claim = None
        if claimed:  # a coverage case: {parts: [indexes into the locations], challenge[, relation]}
            claim = {"complete": True, "challenge": claimed[0]["challenge"],
                     "parts": [locations[i]["location_key"] for i in claimed[0]["parts"]]}
            if "relation" in claimed[0]:  # status rules 2 and 3: a bundle and its relation
                claim["relation"] = claimed[0]["relation"]
            test.assertEqual(build_store.coverage_part_problems(claim, locations), [], (kind, ends, claimed))
        status, basis = build_store.derive_status(kind, locations, claim, status_rule)
        if status != expected:
            mismatches.append((kind, ends, expected, status, claimed))
        elif status is None:
            test.assertIsNone(basis)
        elif basis == build_store.COVERAGE_BASIS:
            # The claim is the basis only when it does better than every location.
            test.assertEqual(build_store.coverage_status(kind, claim, locations, status_rule), status)
            test.assertFalse(any(build_store.location_status(kind, loc, status_rule) == status for loc in locations))
        else:
            # The basis is the highest-ranked location that reaches the passage's status.
            reaching = [loc for loc in locations if build_store.location_status(kind, loc, status_rule) == status]
            test.assertIs(basis, reaching[0], (kind, ends))
    return mismatches


def _hex_key(key):
    """A shared case's location key ("k1") as the 64-hex key a record carries, one to one.

    Anything that is not a string is left as it is, so a malformed part stays malformed.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest() if isinstance(key, str) else key


def _shared_case_status(case: dict, status_rule: int = 1):
    """build_store.derive_passage_status on one case of the shared table: a status, None, or "refused".

    A bundle's conjuncts name their parts by the case's keys too, so they are mapped alike.
    """
    locations = [{**loc, "location_key": _hex_key(loc.get("location_key"))} for loc in case["locations"]]
    coverage = case["coverage"]
    if isinstance(coverage, dict) and isinstance(coverage.get("parts"), list):
        coverage = {**coverage, "parts": [_hex_key(key) for key in coverage["parts"]]}
    if isinstance(coverage, dict) and isinstance(coverage.get("conjuncts"), list):
        coverage["conjuncts"] = [{**c, "part": _hex_key(c["part"])} if isinstance(c, dict) and "part" in c else c
                                 for c in coverage["conjuncts"]]
    try:
        return build_store.derive_passage_status(case["passage_kind"], locations, case["absent"], coverage,
                                                 status_rule)
    except build_store.StatusRefused:
        return "refused"


class SharedParityTableTests(unittest.TestCase):
    """The parity table both sides run (fixtures/status_parity.json), through the builder's checks.

    The table is formal-companion's hand-written tests/data/locate/status-parity.json, copied
    byte for byte: each case's expectation was written from decision D1's spec, and the
    producer's own test runs cflib.vocab.derive_status over the same cases. Here each case
    goes through build_store.derive_passage_status, the builder's own checks and status rules
    for one passage, with the case's keys ("k1", ...) as 64-hex keys.
    """

    def setUp(self):
        self.doc = json.loads(SHARED_TABLE.read_text(encoding="utf-8"))
        self.cases = {case["id"]: case for case in self.doc["cases"]}

    def test_the_table_is_the_producers_copy(self):
        self.assertEqual(hashlib.sha256(SHARED_TABLE.read_bytes()).hexdigest(), SHARED_TABLE_SHA256)
        self.assertEqual(self.doc["schema"], "cf-locate-status-parity/1")
        self.assertEqual(self.doc["function"], "derive_status(passage_kind, locations, absent, coverage) -> status | null")
        self.assertEqual((self.doc["decision"]["id"], self.doc["decision"]["date"]), ("D1", "2026-10-18"))
        self.assertIn("Formalized when the hasPart locations together cover every clause", self.doc["decision"]["text"])
        self.assertEqual(len(self.cases), len(self.doc["cases"]))
        for case in self.doc["cases"]:  # each case says one thing: a status (or null), or a refusal
            self.assertNotEqual("expected" in case, bool(case.get("refused")), case["id"])
        statuses = {case["expected"] for case in self.doc["cases"] if "expected" in case}
        self.assertEqual(statuses, {*build_store.STATUSES, None})
        # Round three: the lead's reading of the decision, which resolves Q1-Q3.
        self.assertEqual((self.doc["reading"]["round"], self.doc["reading"]["resolves"]), (3, ["Q1", "Q2", "Q3"]))
        self.assertIn("exactly like one more exactMatch location", self.doc["reading"]["text"])

    def test_the_builder_derives_every_case_as_the_table_says(self):
        mismatches = []
        for case in self.doc["cases"]:
            wanted = "refused" if case.get("refused") else case["expected"]
            got = _shared_case_status(case)
            if got != wanted:
                mismatches.append((case["id"], wanted, got, case["rule"]))
        self.assertEqual(mismatches, [], f"{len(mismatches)} cases differ from the shared table")

    def test_a_survived_claim_confirms_its_parts_as_the_table_says(self):
        # Round three: confirming a claim confirms its parts. Each case's expected_challenges is
        # the challenge value every location's exported record carries; the view model shows
        # exactly that, through build_store.confirmed_parts (what build_passages calls).
        cases = [case for case in self.doc["cases"] if "expected_challenges" in case]
        self.assertGreaterEqual(len(cases), 5)
        for case in cases:
            with self.subTest(case=case["id"]):
                self.assertNotEqual(_shared_case_status(case), "refused")
                locations = [{"location_key": _hex_key(loc["location_key"]), "challenge": loc["challenge"]}
                             for loc in case["locations"]]
                self.assertTrue(all(build_store.member(loc["challenge"], build_store.CHALLENGES) for loc in locations))
                coverage = case["coverage"]
                shown = locations if coverage is None else build_store.confirmed_parts(
                    {**coverage, "parts": [_hex_key(key) for key in coverage["parts"]]}, locations)
                self.assertEqual({loc["location_key"]: loc["challenge"] for loc in shown},
                                 {_hex_key(key): value for key, value in case["expected_challenges"].items()})
        # Review four: the reopen wins. A part a prominence overturn to subject or central reopened
        # in the claim's own run (challenge_pending) stays pending under a survived claim (P05).
        reopened = [case["id"] for case in cases
                    if (case["coverage"] or {}).get("challenge") == "survived"
                    and any(loc["challenge"] == "challenge_pending" and loc["location_key"] in case["coverage"]["parts"]
                            and case["expected_challenges"][loc["location_key"]] == "challenge_pending"
                            for loc in case["locations"])]
        self.assertIn("P05", reopened)

    def test_the_inputs_parity_round_one_found_apart_are_refused(self):
        # Round one (2026-10-18) found the two sides apart on three inputs no export carries: an
        # owed claim (challenge null), a claim with a note, and one location_key on two locations
        # (where the producer kept the last). Both sides now refuse each, in either order.
        def case(locations, coverage, absent=False, kind="theorem"):
            return _shared_case_status({"passage_kind": kind, "locations": locations, "absent": absent,
                                        "coverage": coverage})
        part = {"relation": "hasPart", "proof_status": "proved", "statement": False}
        related = {"relation": "relatedMatch", "proof_status": "proved", "statement": False}
        k1, k2 = {**part, "location_key": "k1"}, {**part, "location_key": "k2"}
        survived = {"complete": True, "parts": ["k1", "k2"], "challenge": "survived"}
        self.assertEqual(case([k1, k2], survived), "formalized")
        self.assertEqual(case([k1, k2], {**survived, "challenge": None}), "refused")
        self.assertEqual(case([k1, k2], {**survived, "note": "The two parts state both clauses."}), "refused")
        self.assertEqual(case([k1, k2], {**survived, "challenge": "refuted"}), "refused")
        self.assertEqual(case([k1, k2], None), "partial")  # a refuted claim is removed: its parts stay hasPart
        self.assertEqual(case([{**related, "location_key": "k2"}, k1, k2], survived), "refused")
        self.assertEqual(case([k1, k2, {**related, "location_key": "k2"}], survived), "refused")
        for message, coverage in (("coverage.challenge is null", {**survived, "challenge": None}),
                                  ("coverage: unexpected 'note'", {**survived, "note": "a note"})):
            with self.assertRaises(build_store.StatusRefused) as caught:
                build_store.derive_passage_status("theorem", [{**k1, "location_key": _hex_key("k1")},
                                                              {**k2, "location_key": _hex_key("k2")}], False,
                                                  {**coverage, "parts": [_hex_key("k1"), _hex_key("k2")]})
            self.assertIn(message, str(caught.exception))

    def test_it_is_the_builders_path(self):
        # On every passage of the fixture store, the one-passage form gives the status build() gave.
        for name, body in sorted(build_store.build(RECORDSET).items()):
            if not name.startswith("atlas/v1/articles/"):
                continue
            for passage in json.loads(body)["passages"]:
                with self.subTest(passage=passage["id"]):
                    coverage = passage["coverage"]
                    claim = None if coverage is None else {key: coverage[key] for key in build_store.COVERAGE_KEYS}
                    self.assertEqual(build_store.derive_passage_status(
                        passage["kind"], passage["locations"], passage["absence"] is not None, claim),
                        passage["status"])


class SetCopyTest(unittest.TestCase):
    """A test on a scratch copy of the fixture record set, edited and resealed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atlas-test-"))
        self.set = self.tmp / "set"
        shutil.copytree(RECORDSET, self.set)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def fresh(self) -> None:
        self.tearDown()
        self.setUp()

    def refuse(self, fragment: str):
        with self.assertRaises(build_store.AtlasError) as caught:
            build_store.build(self.set)
        self.assertIn(fragment, str(caught.exception))

    def article(self, pageid: int = 1001) -> dict:
        return json.loads(build_store.build(self.set)[f"atlas/v1/articles/{pageid}.json"])

    def edit_widget(self, edit) -> None:
        path = self.set / "records/enwiki-1001-2002.jsonl"
        rows = read_jsonl(path)
        edit(rows)
        write_jsonl(path, rows)
        reseal(self.set)

    def edit_universe(self, edit) -> None:
        path = self.set / "universe.json"
        universe = json.loads(path.read_text())
        edit(universe)
        path.write_text(make_fixture.dumps(universe) + "\n")
        reseal(self.set)

    def edit_seal(self, edit) -> None:
        path = self.set / "SEAL.json"
        seal = json.loads(path.read_text())
        edit(seal)
        path.write_text(json.dumps(seal, sort_keys=True, indent=2) + "\n")

    def replace_everywhere(self, old: str, new: str) -> None:
        """Replace a JSON string value in every file of the set (universe and records alike), then reseal."""
        needle, value = json.dumps(old).encode(), json.dumps(new).encode()
        for path in self.set.rglob("*"):
            if path.is_file() and path.name != "SEAL.json":
                path.write_bytes(path.read_bytes().replace(needle, value))
        reseal(self.set)

    def replace_receipt(self, body: dict, queries: int, candidates: int) -> str:
        """Swap the absence's receipt for `body`, referenced with the given counts; returns its path."""
        for old in (self.set / "receipts").glob("*.json"):
            old.unlink()
        data = (make_fixture.dumps(body) + "\n").encode()
        digest = hashlib.sha256(data).hexdigest()
        (self.set / "receipts" / f"{digest}.json").write_bytes(data)
        self.edit_widget(lambda rows: rows[3]["extensions"][LOCATE].update(
            receipt={"sha256": digest, "queries": queries, "candidates": candidates}))
        return f"receipts/{digest}.json"


class RefusalTests(SetCopyTest):

    def test_sha_mismatch_without_reseal(self):
        path = self.set / "sources.jsonl"
        path.write_bytes(path.read_bytes().replace(b"Widget lemma", b"Widget lemmb"))
        self.refuse("sha256 does not match SEAL.json")

    def test_unlisted_file(self):
        (self.set / "records" / "extra.txt").write_text("x\n")
        self.refuse("present but not listed in SEAL.json")

    def test_schema_invalid_record(self):
        self.edit_widget(lambda rows: rows[0].update(status={"value": "formalized"}))
        self.refuse("Literature Links $.status")

    def test_absence_missing_a_library(self):
        self.edit_widget(lambda rows: rows[3]["object"].pop())
        self.refuse("names every repository of the universe at its commit, once each")

    def test_unresolved_with_a_status(self):
        self.edit_widget(lambda rows: rows[5].update(status="not_formalized"))
        self.refuse("unresolved record has object [] and no status")

    def test_duplicate_location_key(self):
        def edit(rows):
            key = rows[0]["extensions"][LOCATE]["location_key"]
            rows[2]["extensions"][LOCATE]["location_key"] = key
        self.edit_widget(edit)
        self.refuse("appears twice")

    def test_quote_over_the_cap(self):
        def edit(rows):
            for row in rows[:2]:
                row["subject"][0]["location"]["quote"]["exact"] = " ".join(["word"] * 13)
        self.edit_widget(edit)
        self.refuse("exceeds 12 words")

    def test_records_of_one_passage_carry_the_same_span_sha(self):
        # The page's ellipsis reads span_sha256, so records that disagree on it are refused like
        # any other passage field, whichever record comes first; none is chosen by its order. (A
        # missing one is refused on its own as well, LocateExtensionTests.)
        twelve = " ".join(["word"] * 12)
        own = hashlib.sha256(twelve.encode()).hexdigest()
        for shas in ((own, "0" * 64), ("0" * 64, own), (own, None)):
            with self.subTest(shas=shas):
                self.fresh()

                def edit(rows, shas=shas):
                    for row, sha in zip(rows[:2], shas):
                        row["subject"][0]["location"]["quote"] = {"exact": twelve}
                        ext = row["subject"][0]["extensions"][LOCATE]
                        if sha is None:
                            del ext["span_sha256"]
                        else:
                            ext["span_sha256"] = sha
                self.edit_widget(edit)
                self.refuse("records of one passage disagree on span_sha256")

    def test_a_head_whose_passage_ends_in_its_suffix_is_marked(self):
        # The Widget bound with a suffix that holds the rest of its passage: still a capped head
        # (the Subject row shows only the quote), but the card's suffix reaches the passage's end.
        def edit(rows):
            quote = rows[2]["subject"][0]["location"]["quote"]
            quote["suffix"] = " its rank. Further"
            rows[2]["subject"][0]["extensions"][LOCATE]["span_sha256"] = hashlib.sha256(
                (quote["exact"] + " its rank.").encode()).hexdigest()
        self.edit_widget(edit)
        bound = self.article()["passages"][1]
        self.assertEqual((bound["quote_capped"], bound["passage_ends_in_suffix"]), (True, True))

    def test_commit_outside_the_universe(self):
        self.edit_widget(lambda rows: rows[0]["object"][0].update(version="0" * 40))
        self.refuse("uri and version must be its library's repo and commit")

    def test_record_names_another_universe(self):
        self.edit_widget(lambda rows: rows[0]["extensions"][LOCATE].update(universe_sha256="f" * 64))
        self.refuse("must name the record set's universe")

    def test_formalized_needs_a_proved_strong_match(self):
        def edit(rows):
            for row in rows[:2]:
                row["object"][0]["extensions"][LOCATE]["proof_status"] = "statement_only"
        self.edit_widget(edit)
        self.refuse(f"the records say formalized, but {DERIVES} statement_only for a theorem passage")

    def _first_passage(self, edit) -> None:
        """Edit the two records of the first Widget passage (Mathlib rank 1, FormalFrontier rank 2)."""
        self.edit_widget(lambda rows: edit(rows[0], rows[1]))

    def test_formalized_example_needs_a_complete_match(self):
        # Any kind, not only theorem and definition: a sorry-backed match never formalizes.
        def edit(a, b):
            for row in (a, b):
                row["kind"] = "example"
                row["object"][0]["extensions"][LOCATE]["proof_status"] = "statement_only"
        self._first_passage(edit)
        self.refuse(f"the records say formalized, but {DERIVES} statement_only for a example passage")

    def test_formalized_construction_via_a_wanted_declaration(self):
        # A construction is judged as a definition passage (vocabulary.md §1.11), whose
        # broadMatch is partial whatever the declaration is.
        def edit(a, b):
            for row in (a, b):
                row["kind"] = "construction"
                row["relation"] = "broadMatch"
                row.pop("note", None)
                row["object"][0]["extensions"][LOCATE]["proof_status"] = "wanted"
        self._first_passage(edit)
        self.refuse("derive partial for a construction passage with broadMatch to wanted")

    def test_example_with_a_proved_match_is_formalized(self):
        def edit(a, b):
            for row in (a, b):
                row["kind"] = "example"
        self._first_passage(edit)
        article = json.loads(build_store.build(self.set)["atlas/v1/articles/1001.json"])
        self.assertEqual(article["passages"][0]["status"], "formalized")

    def test_understated_status_is_refused(self):
        # A theorem with a proved exact match is formalized, not statement_only.
        self._first_passage(lambda a, b: [row.update(status="statement_only") for row in (a, b)])
        self.refuse(f"the records say statement_only, but {DERIVES} formalized")

    def test_partial_beside_a_proved_exact_match_is_refused(self):
        def edit(a, b):
            b["relation"] = "hasPart"
            b.pop("note", None)
            for row in (a, b):
                row["status"] = "partial"
        self._first_passage(edit)
        self.refuse(f"the records say partial, but {DERIVES} formalized")

    def _related_only(self, a, b, status=None):
        for row in (a, b):
            row["relation"] = "relatedMatch"
            row.pop("note", None)
            row["extensions"][LOCATE]["challenge"] = "downgraded"
            if status is None:
                row.pop("status", None)
            else:
                row["status"] = status

    def test_located_passage_with_only_related_matches_has_no_status(self):
        # Records that stamp a status on a related-only passage are refused: the table gives none.
        self._first_passage(lambda a, b: self._related_only(a, b, "partial"))
        self.refuse("gives a theorem passage with relatedMatch only no status: it is unresolved")

    def test_related_only_passage_without_a_status_is_unresolved(self):
        # Proposal §2.6 "(none)": what a challenge that downgrades a passage's locations leaves.
        # It counts as undecided, never as not formalized, and keeps its related declarations.
        self._first_passage(self._related_only)
        tree = build_store.build(self.set)
        article = json.loads(tree["atlas/v1/articles/1001.json"])
        passage = article["passages"][0]
        self.assertEqual((passage["outcome"], passage["status"], passage["basis"]), ("unresolved", None, None))
        self.assertEqual(passage["unresolved"], {"challenge": "downgraded"})
        self.assertEqual([loc["relation"] for loc in passage["locations"]], ["relatedMatch", "relatedMatch"])
        self.assertEqual(article["summary"]["by_status"]["unresolved"], 2)
        self.assertEqual(article["summary"]["by_status"]["formalized"], 0)
        self.assertEqual(article["summary"]["by_library"]["mathlib"],
                         {"locations": 0, "complete": 0, "incomplete": 0, "passages": 0, "related": 1})
        shard = json.loads(tree["atlas/v1/index/1.json"])
        self.assertEqual(shard["articles"][0][-1], ["tauceti"])  # Mathlib and FormalFrontier hold related only

    def test_status_less_related_match_beside_a_counted_location_is_refused(self):
        def edit(a, b):
            b["relation"] = "relatedMatch"
            b.pop("note", None)
            b.pop("status")
        self._first_passage(edit)
        self.refuse("every record of a passage carries the same derived status (only a passage whose")

    def test_status_less_record_that_counts_is_refused(self):
        self._first_passage(lambda a, b: [row.pop("status") for row in (a, b)])
        self.refuse("a location record carries a derived status")

    def test_status_less_related_match_beside_an_absence_is_refused(self):
        self.edit_widget(lambda rows: rows[4].pop("status"))
        self.refuse("every record of a passage carries the same derived status")

    def test_broad_match_to_a_definition_is_partial(self):
        # The table's "broadMatch to a definition" row: partial, and only partial. A
        # Prop-valued definition is marked by the statement flag, not by the relation.
        for status, refused in (("partial", False), ("statement_only", True)):
            with self.subTest(status=status):
                self.tearDown()
                self.setUp()

                def edit(a, b, status=status):
                    for row in (a, b):
                        row["relation"] = "broadMatch"
                        row.pop("note", None)
                        row["status"] = status
                        row["object"][0]["extensions"][LOCATE]["proof_status"] = "definition"
                self._first_passage(edit)
                if refused:
                    self.refuse(f"the records say statement_only, but {DERIVES} partial")
                else:
                    article = json.loads(build_store.build(self.set)["atlas/v1/articles/1001.json"])
                    self.assertEqual(article["passages"][0]["status"], "partial")

    def _one_location(self, kind, relation, proof_status, status, statement=None):
        """Make the first Widget passage one location (Mathlib, rank 1) of the given shape."""
        def edit(rows):
            del rows[1]
            row = rows[0]
            row["kind"], row["relation"], row["status"] = kind, relation, status
            if relation != "closeMatch":
                row.pop("note", None)
            else:  # a closeMatch always names its convention difference
                row.setdefault("note", "States it for finite widgets only.")
            end = row["object"][0]["extensions"][LOCATE]
            end["proof_status"] = proof_status
            if statement is not None:
                end["statement"] = statement
        self.edit_widget(edit)

    def test_theorem_matched_exactly_or_closely_to_data_is_formalized(self):
        # Decision D1: an exact or close match to an equivalence or data declaration that states
        # the theorem (CRT's RingEquiv) formalizes it; before D1 vocabulary.md §1.11 said partial.
        for kind in ("theorem", "conjecture"):
            for relation in ("exactMatch", "closeMatch"):
                with self.subTest(kind=kind, relation=relation):
                    self.fresh()
                    self._one_location(kind, relation, "definition", "formalized")
                    passage = self.article()["passages"][0]
                    self.assertEqual(passage["status"], "formalized")
                    self.assertEqual(passage["basis"], passage["locations"][0]["location_key"])
                    self.fresh()
                    self._one_location(kind, relation, "definition", "partial")
                    self.refuse(f"the records say partial, but {DERIVES} formalized for a {kind} passage "
                                f"with {relation} to definition")
        # A broadMatch to data stays partial (the table's row), and data resting on sorry has
        # proof_status statement_only, so it stays statement_only.
        self.fresh()
        self._one_location("theorem", "broadMatch", "definition", "formalized")
        self.refuse(f"the records say formalized, but {DERIVES} partial for a theorem passage "
                    "with broadMatch to definition")
        self.fresh()
        self._one_location("theorem", "exactMatch", "statement_only", "formalized")
        self.refuse(f"the records say formalized, but {DERIVES} statement_only")

    def test_prop_valued_definition_states_a_theorem(self):
        # The lean end's statement flag marks a Prop-valued definition: statement_only.
        self._one_location("theorem", "exactMatch", "definition", "statement_only", statement=True)
        article = json.loads(build_store.build(self.set)["atlas/v1/articles/1001.json"])
        passage = article["passages"][0]
        self.assertEqual(passage["status"], "statement_only")
        self.assertIs(passage["locations"][0]["statement"], True)
        self.assertEqual(passage["basis"], passage["locations"][0]["location_key"])
        self.tearDown()
        self.setUp()
        self._one_location("theorem", "exactMatch", "definition", "partial", statement=True)
        self.refuse("derive statement_only for a theorem passage with exactMatch to a Prop-valued definition")

    def test_statement_flag_must_be_a_bool_and_never_on_a_proved_declaration(self):
        self._one_location("theorem", "exactMatch", "definition", "statement_only", statement="yes")
        self.refuse("statement must be true or false")
        self.tearDown()
        self.setUp()
        self._one_location("theorem", "exactMatch", "proved", "formalized", statement=True)
        self.refuse("a proved declaration with the statement flag set")

    def test_definition_matched_to_a_proved_theorem_is_partial(self):
        for relation in ("exactMatch", "broadMatch"):
            with self.subTest(relation=relation):
                self.tearDown()
                self.setUp()
                self._one_location("definition", relation, "proved", "partial")
                self.assertEqual(json.loads(build_store.build(self.set)["atlas/v1/articles/1001.json"])
                                 ["passages"][0]["status"], "partial")

    def test_construction_and_notation_are_definition_passages(self):
        # A proved theorem never formalizes a construction or a notation (vocabulary.md §1.11).
        for kind in ("construction", "notation"):
            with self.subTest(kind=kind):
                self.tearDown()
                self.setUp()
                self._one_location(kind, "exactMatch", "proved", "formalized")
                self.refuse(f"the records say formalized, but {DERIVES} partial for a {kind} passage")

    def test_basis_is_the_location_that_justifies_the_status(self):
        # Rank 1 states it without proof, rank 2 proves a more general result:
        # the passage is formalized, and the basis is rank 2, not rank 1.
        def edit(a, b):
            a["object"][0]["extensions"][LOCATE]["proof_status"] = "statement_only"
            b["relation"] = "broadMatch"
            b.pop("note", None)
        self._first_passage(edit)
        article = json.loads(build_store.build(self.set)["atlas/v1/articles/1001.json"])
        passage = article["passages"][0]
        self.assertEqual(passage["status"], "formalized")
        self.assertEqual(passage["basis"], passage["locations"][1]["location_key"])
        self.assertEqual(passage["locations"][1]["library"], "ff-widgets")

    def test_unknown_status_is_a_refusal_not_a_crash(self):
        self.edit_widget(lambda rows: [row.update(status="disputed") for row in rows[:2]])
        self.refuse("a location record carries a derived status")

    def test_non_string_affix_is_a_refusal_not_a_crash(self):
        def edit(rows):
            for row in rows:
                row["subject"][0]["location"] = {"profile": "https://example.org/p",
                                                 "quote": {"exact": "a widget", "prefix": 5}}
        self.edit_widget(edit)
        self.refuse("quote.prefix must be a string")

    def test_subject_uri_must_name_exactly_the_revision(self):
        def edit(rows):
            for row in rows:
                row["subject"][0]["uri"] = row["subject"][0]["uri"].replace("oldid=2002", "oldid=20029")
        self.edit_widget(edit)
        self.refuse("oldid is exactly the revision")

    def test_two_pages_with_one_title_key(self):
        path = self.set / "sources.jsonl"
        rows = read_jsonl(path)
        rows[1]["title"] = "Widget_lemma"
        write_jsonl(path, rows)
        reseal(self.set)
        self.refuse("has the same title key as page 1001")

    def test_blob_url_template_is_refused(self):
        self.edit_universe(lambda u: u["libraries"][0].update(
            blob_url_template="https://phish.example/login?next={commit}{path}"))
        self.refuse("blob_url_template is not supported")

    def test_repo_with_dot_segments_is_refused(self):
        self.edit_universe(lambda u: u["libraries"][4].update(repo="https://github.com/../.."))
        self.refuse("repo must be an https repository URL")

    def test_receipt_with_too_few_queries(self):
        receipts = list((self.set / "receipts").glob("*.json"))
        body = json.loads(receipts[0].read_text())
        body["queries"] = body["queries"][:2]
        data = (make_fixture.dumps(body) + "\n").encode()
        digest = hashlib.sha256(data).hexdigest()
        receipts[0].unlink()
        (self.set / "receipts" / f"{digest}.json").write_bytes(data)

        def edit(rows):
            rows[3]["extensions"][LOCATE]["receipt"] = {"sha256": digest, "queries": 2, "candidates": 2}
        self.edit_widget(edit)
        self.refuse("at least 3 distinct queries")

    def test_universe_disagrees_with_seal(self):
        path = self.set / "universe.json"
        universe = json.loads(path.read_text())
        universe["universe_sha256"] = "e" * 64
        path.write_text(make_fixture.dumps(universe) + "\n")
        reseal(self.set)
        self.refuse("universe_sha256 differs from SEAL.json")

    def test_universe_yaml_is_refused(self):
        # The agreed format has universe.json only; a YAML universe, alone or beside it, is refused.
        (self.set / "universe.yaml").write_text("id: u-fixture\n")
        reseal(self.set)
        self.refuse("universe.yaml: not part of the agreed record-set format")
        (self.set / "universe.json").unlink()
        reseal(self.set)
        self.refuse("universe.json is missing from the record set")


class CoverageTests(SetCopyTest):
    """Decision D1's coverage claims, on the Gadget article's covered conjunction (rows 1 and 2).

    Row 0 is the Gadget definition, rows 1 and 2 the two hasPart parts (Mathlib, ranks 1 and 2,
    both proved) carrying the survived claim, row 3 the theorem an equivalence states.
    """

    GADGET = "records/enwiki-1003-2004.jsonl"

    def edit_gadget(self, edit) -> None:
        path = self.set / self.GADGET
        rows = read_jsonl(path)
        edit(rows)
        write_jsonl(path, rows)
        reseal(self.set)

    def edit_parts(self, edit) -> None:
        """Edit the two records of the covered passage."""
        self.edit_gadget(lambda rows: edit(rows[1], rows[2]))

    @staticmethod
    def key(row: dict) -> str:
        return row["extensions"][LOCATE]["location_key"]

    @staticmethod
    def claim_of(row: dict) -> dict:
        return row["extensions"][LOCATE]["coverage"]

    def covered(self) -> dict:
        return self.article(1003)["passages"][1]

    def shape(self, a_proof, b_proof, status, kind="theorem", a_statement=None, b_statement=None, challenge=None):
        """Give the covered passage's parts these proof statuses, its kind and its stamped status."""
        def edit(a, b):
            for row, proof, statement in ((a, a_proof, a_statement), (b, b_proof, b_statement)):
                row["kind"], row["status"] = kind, status
                end = row["object"][0]["extensions"][LOCATE]
                end["proof_status"] = proof
                if statement is not None:
                    end["statement"] = statement
                if challenge is not None:
                    self.claim_of(row)["challenge"] = challenge
        self.edit_parts(edit)

    def test_the_fixture_conjunction_is_formalized_by_its_survived_claim(self):
        passage = self.covered()
        self.assertEqual((passage["status"], passage["basis"]), ("formalized", None))
        self.assertEqual((passage["coverage"]["status"], passage["coverage"]["basis"]), ("formalized", True))
        # Without the claim (a refuted claim is removed) the parts stay hasPart: partial.
        self.fresh()
        self.edit_parts(lambda a, b: [row["extensions"][LOCATE].pop("coverage") for row in (a, b)])
        self.refuse(f"the records say formalized, but {DERIVES} partial for a theorem passage with hasPart to "
                    "proved, hasPart to proved")
        self.fresh()
        self.edit_parts(lambda a, b: [(row["extensions"][LOCATE].pop("coverage"), row.update(status="partial"))
                                      for row in (a, b)])
        passage = self.covered()
        self.assertEqual((passage["status"], passage["coverage"]), ("partial", None))
        self.assertEqual(passage["basis"], passage["locations"][0]["location_key"])

    def test_only_a_survived_claim_upgrades(self):
        # Pending, unjudged (not_required, sampled_out) or downgraded: the claim upgrades nothing.
        for challenge in ("challenge_pending", "not_required", "sampled_out", "downgraded"):
            with self.subTest(challenge=challenge):
                self.fresh()
                self.shape("proved", "proved", "formalized", challenge=challenge)
                self.refuse(f"{DERIVES} partial for a theorem passage with hasPart to proved, hasPart to proved, "
                            f"and a coverage claim over 2 parts (challenge {challenge})")
                self.fresh()
                self.shape("proved", "proved", "partial", challenge=challenge)
                passage = self.covered()
                self.assertEqual(passage["status"], "partial")
                self.assertEqual(passage["coverage"]["challenge"], challenge)
                self.assertEqual((passage["coverage"]["status"], passage["coverage"]["basis"]), (None, False))
                self.assertEqual(passage["basis"], passage["locations"][0]["location_key"])

    def _claim_cases(self, kinds, cases):
        """Each (part a, part b) -> status, under the survived claim, for every kind in `kinds`.

        The claim is the passage's basis in every case (each part alone gives only partial), and a
        stamp of the other upgraded status is refused, in either direction.
        """
        for kind in kinds:
            for (a, a_flag), (b, b_flag), expected in cases:
                with self.subTest(kind=kind, parts=(a, a_flag, b, b_flag)):
                    self.fresh()
                    self.shape(a, b, expected, kind=kind, a_statement=a_flag, b_statement=b_flag)
                    passage = self.covered()
                    self.assertEqual((passage["status"], passage["basis"]), (expected, None))
                    self.assertEqual((passage["coverage"]["status"], passage["coverage"]["basis"]), (expected, True))
                    self.fresh()
                    wrong = "statement_only" if expected == "formalized" else "formalized"
                    self.shape(a, b, wrong, kind=kind, a_statement=a_flag, b_statement=b_flag)
                    self.refuse(f"the records say {wrong}, but {DERIVES} {expected} for a {kind} passage")

    # The round-three reading of D1 (docs/ATLAS.md check 6, resolving Q1-Q3): a survived claim
    # counts as one more exactMatch location, a fixed stand-in rather than any one part. Finished
    # parts are proved ones and data (a definition without the statement flag); unfinished ones
    # are statement_only, wanted and axiom_dependent, and a Prop-valued definition (the flag set).
    # Every part finished: a proved declaration on the theorem row and a definition on the
    # definition row, so formalized on either. Any part unfinished: a statement_only declaration
    # on both rows, so statement only.
    def test_theorem_parts(self):
        # Q2: a data part counts as finished, as an exact match to data formalizes a theorem; a
        # Prop-valued definition is unfinished whatever its proof_status.
        self._claim_cases(("theorem", "conjecture"), [
            (("proved", None), ("proved", None), "formalized"),
            (("proved", None), ("definition", None), "formalized"),
            (("definition", None), ("definition", None), "formalized"),
            (("proved", None), ("statement_only", None), "statement_only"),
            (("wanted", None), ("proved", None), "statement_only"),
            (("axiom_dependent", None), ("statement_only", None), "statement_only"),
            (("proved", None), ("statement_only", True), "statement_only"),
            (("proved", None), ("definition", True), "statement_only"),
            (("definition", None), ("definition", True), "statement_only"),
            (("statement_only", None), ("definition", True), "statement_only"),
        ])

    def test_definition_parts(self):
        # Q3: an unfinished part gives the definition row the statement-only answer an exact match
        # to an unfinished declaration gets. A Prop-valued definition part is unfinished here too,
        # though an exact match to one alone formalizes a definition passage.
        self._claim_cases(("definition", "construction", "notation"), [
            (("definition", None), ("proved", None), "formalized"),
            (("definition", None), ("definition", None), "formalized"),
            (("proved", None), ("proved", None), "formalized"),
            (("definition", None), ("statement_only", None), "statement_only"),
            (("proved", None), ("wanted", None), "statement_only"),
            (("definition", None), ("definition", True), "statement_only"),
            (("axiom_dependent", None), ("definition", True), "statement_only"),
        ])

    def test_example_and_other_claims_take_both_rows(self):
        # Q1: an example's or `other` passage's claim is judged on both rows, the better winning,
        # exactly as an ordinary location is; both rows give the same answer for a claim.
        self._claim_cases(("example", "other"), [
            (("proved", None), ("proved", None), "formalized"),
            (("definition", None), ("proved", None), "formalized"),
            (("definition", None), ("definition", None), "formalized"),
            (("proved", None), ("statement_only", None), "statement_only"),
            (("definition", None), ("statement_only", None), "statement_only"),
            (("definition", None), ("definition", True), "statement_only"),
        ])

    def test_confirming_a_claim_confirms_its_parts(self):
        # A part no challenge judged on its own (not_required, sampled_out) reads survived once its
        # claim survived; the records are copied as they are.
        def own(a_challenge, b_challenge, claim_challenge="survived", status="formalized"):
            def edit(a, b):
                for row, challenge in ((a, a_challenge), (b, b_challenge)):
                    row["extensions"][LOCATE]["challenge"] = challenge
                    self.claim_of(row)["challenge"] = claim_challenge
                    row["status"] = status
            self.edit_parts(edit)
        own("not_required", "sampled_out")
        tree = build_store.build(self.set)
        passage = json.loads(tree["atlas/v1/articles/1003.json"])["passages"][1]
        self.assertEqual([loc["challenge"] for loc in passage["locations"]], ["survived", "survived"])
        self.assertEqual(passage["status"], "formalized")
        records = read_jsonl(self.set / self.GADGET)
        self.assertEqual(tree["atlas/v1/records/1003.jsonl"], (self.set / self.GADGET).read_bytes())
        self.assertEqual([row["extensions"][LOCATE]["challenge"] for row in records[1:3]],
                         ["not_required", "sampled_out"])
        # A part another verdict judged keeps it, and so does one still owed a challenge.
        for kept in ("downgraded", "challenge_pending"):
            with self.subTest(kept=kept):
                self.fresh()
                own(kept, "not_required")
                locations = self.covered()["locations"]
                self.assertEqual([loc["challenge"] for loc in locations], [kept, "survived"])
        # A claim that did not survive confirms nothing.
        for challenge in ("not_required", "sampled_out", "downgraded", "challenge_pending"):
            with self.subTest(claim=challenge):
                self.fresh()
                own("not_required", "sampled_out", claim_challenge=challenge, status="partial")
                passage = self.covered()
                self.assertEqual([loc["challenge"] for loc in passage["locations"]], ["not_required", "sampled_out"])
                self.assertEqual(passage["status"], "partial")
        # A location of the passage that is not a part is not confirmed by the claim.
        self.fresh()

        def third(rows):
            self._add_location(rows, "exactMatch", "statement_only")
            rows[3]["extensions"][LOCATE]["challenge"] = "not_required"
        self.edit_gadget(third)
        passage = self.covered()
        self.assertEqual([loc["challenge"] for loc in passage["locations"]], ["survived", "survived", "not_required"])

    def _add_location(self, rows, relation, proof_status, rank=3):
        """Give the covered passage a third location (Mathlib, a new declaration) of this shape."""
        row = json.loads(json.dumps(rows[1]))
        row["relation"] = relation
        row["object"][0]["location"]["declaration"] = "Gadget.connected_and_finite"
        row["object"][0]["extensions"][LOCATE]["proof_status"] = proof_status
        row["extensions"][LOCATE].update(rank=rank, location_key=hashlib.sha256(b"third location").hexdigest())
        rows.insert(3, row)

    def test_a_location_reaching_the_same_status_is_the_basis(self):
        # Best status over all evidence; on a tie the one location is named, the claim still shown.
        self.edit_gadget(lambda rows: self._add_location(rows, "exactMatch", "proved"))
        passage = self.covered()
        self.assertEqual(passage["status"], "formalized")
        self.assertEqual(passage["basis"], passage["locations"][2]["location_key"])
        self.assertEqual((passage["coverage"]["status"], passage["coverage"]["basis"]), ("formalized", False))
        # A claim that does better than every location is the basis.
        self.fresh()
        self.edit_gadget(lambda rows: self._add_location(rows, "exactMatch", "statement_only"))
        passage = self.covered()
        self.assertEqual((passage["status"], passage["basis"], passage["coverage"]["basis"]),
                         ("formalized", None, True))

    def test_parts_are_kept_in_the_records_order(self):
        def edit(a, b):
            for row in (a, b):
                self.claim_of(row)["parts"].reverse()
        self.edit_parts(edit)
        passage = self.covered()
        self.assertEqual(passage["coverage"]["parts"],
                         [passage["locations"][1]["location_key"], passage["locations"][0]["location_key"]])
        self.assertEqual(passage["status"], "formalized")

    def test_a_claim_names_at_least_two_hasPart_locations_of_its_passage(self):
        def set_parts(parts_of):
            def edit(rows):
                parts = parts_of(rows)
                for row in (rows[1], rows[2]):
                    self.claim_of(row)["parts"] = parts
            return edit
        cases = [
            (lambda rows: [self.key(rows[1])],
             "coverage.parts names 1 location(s): a coverage claim needs at least 2 hasPart locations"),
            (lambda rows: [], "coverage.parts names 0 location(s)"),
            (lambda rows: [self.key(rows[1]), self.key(rows[1])], "coverage.parts names one location twice"),
            (lambda rows: [self.key(rows[1]), "0" * 64],
             f"coverage names {'0' * 64}, which is no location of this passage: every part is one of its hasPart"),
            # a location of another passage
            (lambda rows: [self.key(rows[1]), self.key(rows[3])], "which is no location of this passage"),
            (lambda rows: [self.key(rows[1]), "XYZ"], "coverage.parts must be a list of location_keys (64 hex)"),
        ]
        for parts_of, fragment in cases:
            with self.subTest(fragment=fragment):
                self.fresh()
                self.edit_gadget(set_parts(parts_of))
                self.refuse(fragment)
        # Every part is a hasPart location: a narrowMatch part is refused, though it also states a part.
        self.fresh()
        self.edit_gadget(lambda rows: rows[2].update(relation="narrowMatch", status="formalized"))
        self.refuse("whose relation is narrowMatch: every part is a hasPart location of this passage")

    def test_the_claim_has_exactly_the_agreed_shape(self):
        cases = [
            (None, build_store.COVERAGE_RULE),
            ([], build_store.COVERAGE_RULE),
            ({"complete": True, "parts": None, "challenge": "survived"}, "coverage.parts must be a list"),
            ("extra", "coverage: unexpected 'note'"),
            ("missing", "coverage: missing 'challenge'"),
            ("incomplete", "coverage.complete must be true"),
            ("refuted", "coverage.challenge must be one of ['challenge_pending', 'downgraded', 'not_required', "
                        "'sampled_out', 'survived'] (only survived counts)"),
            # An owed claim (no challenge judged it yet) upgrades nothing and is never exported.
            ("owed", "coverage.challenge is null: a record's claim carries its challenge value"),
        ]
        for value, fragment in cases:
            with self.subTest(value=value):
                self.fresh()

                def edit(a, b, value=value):
                    for row in (a, b):
                        ext = row["extensions"][LOCATE]
                        if value == "extra":
                            ext["coverage"]["note"] = "Connectedness and the degree bound are its two clauses."
                        elif value == "missing":
                            del ext["coverage"]["challenge"]
                        elif value == "incomplete":
                            ext["coverage"]["complete"] = False
                        elif value == "refuted":
                            ext["coverage"]["challenge"] = "refuted"
                        elif value == "owed":
                            ext["coverage"]["challenge"] = None
                        else:
                            ext["coverage"] = value
                self.edit_parts(edit)
                self.refuse(fragment)

    def test_every_record_of_the_passage_carries_the_same_claim(self):
        self.edit_parts(lambda a, b: b["extensions"][LOCATE].pop("coverage"))
        self.refuse("records of one passage disagree on coverage (every record of the passage carries the same "
                    "claim, or none does)")
        self.fresh()
        self.edit_parts(lambda a, b: self.claim_of(b).update(challenge="challenge_pending"))
        self.refuse("records of one passage disagree on coverage")

    def test_a_claim_belongs_to_a_located_passage(self):
        # On the absence (beside a relatedMatch) and on the unresolved record of the Widget article.
        claim = {"complete": True, "challenge": "survived", "parts": ["a" * 64, "b" * 64]}

        def on_absence(rows):
            claim["parts"] = ["a" * 64, rows[4]["extensions"][LOCATE]["location_key"]]
            for row in (rows[3], rows[4]):
                row["extensions"][LOCATE]["coverage"] = claim
        self.edit_widget(on_absence)
        self.refuse("whose relation is relatedMatch: every part is a hasPart location of this passage")
        self.fresh()
        self.edit_widget(lambda rows: rows[5]["extensions"][LOCATE].update(coverage=claim))
        self.refuse("which is no location of this passage")


class AgreedFormatTests(SetCopyTest):
    """The record-set format formal-companion and WikiLean agreed (docs/ATLAS.md section 3):
    exactly that shape builds, and every other shape is refused by name."""

    # ---- SEAL.json ----

    def test_campaign_is_a_campaign_id_or_a_run_token(self):
        for token in ("run:loc-smoke-2", "run:loc-smoke-2+chal-smoke-2", "tier-a:shard-03", "fixture-2026-10"):
            with self.subTest(token=token):
                self.fresh()
                self.edit_seal(lambda seal, token=token: seal.update(campaign=token))
                tree = build_store.build(self.set)
                self.assertEqual(json.loads(tree["atlas/v1/current.json"])["campaign"], token)
                self.assertEqual(json.loads(tree["atlas/v1/articles/1001.json"])["campaign"], token)
        for token in (None, "", "run:", "run:loc+", "run:+chal", "run:a+b+c", "run:a:b", "run:a b",
                      "loc smoke", "-x", "x" * 129, "run:" + "x" * 65, "fixture\n"):
            with self.subTest(token=token):
                self.fresh()
                self.edit_seal(lambda seal, token=token: seal.update(campaign=token))
                self.refuse("campaign must be a non-empty token")

    def test_seal_keys_are_exactly_the_agreed_ones(self):
        self.edit_seal(lambda seal: seal.update(runs={"locate": "loc-1", "challenge": [], "inventory": "inv-1"}))
        self.refuse("SEAL.json: unexpected 'runs' (not in the agreed record-set format")
        self.fresh()
        self.edit_seal(lambda seal: seal.pop("set_id"))
        self.refuse("SEAL.json: missing 'set_id'")
        self.fresh()
        self.edit_seal(lambda seal: seal.pop("byte_budget"))
        self.refuse("SEAL.json: missing 'byte_budget'")
        # The pre-agreement shapes: universe.sha256, and a sourceset version.
        self.fresh()
        self.edit_seal(lambda seal: seal.update(universe={"id": "u-fixture", "sha256": make_fixture.UNIVERSE_SHA}))
        self.refuse("SEAL.json: universe: missing 'universe_sha256'")
        self.fresh()
        self.edit_seal(lambda seal: seal["sourceset"].update(version=5))
        self.refuse("SEAL.json: sourceset: unexpected 'version'")
        self.fresh()
        self.edit_seal(lambda seal: seal.update(set_id="smoke 2"))
        self.refuse("set_id must be a token")
        for key in ("universe", "sourceset", "campaign"):
            with self.subTest(missing=key):
                self.fresh()
                self.edit_seal(lambda seal, key=key: seal.pop(key))
                self.refuse(f"SEAL.json: missing {key!r}")

    def test_byte_budget_is_off_by_default_and_kept_when_set(self):
        for budget in (-1, True, 1.5, "0", None):
            with self.subTest(budget=budget):
                self.fresh()
                self.edit_seal(lambda seal, budget=budget: seal.update(byte_budget=budget))
                self.refuse("byte_budget must be the per-source byte budget the export used")
        records = (RECORDSET / "records/enwiki-1001-2002.jsonl").stat().st_size
        receipt = next((RECORDSET / "receipts").glob("*.json")).stat().st_size
        # The export measures a source as its records plus the receipts its absences reference.
        self.fresh()
        self.edit_seal(lambda seal: seal.update(byte_budget=records + receipt))
        tree = build_store.build(self.set)
        self.assertEqual(json.loads(tree["atlas/v1/current.json"])["record_set"]["byte_budget"], records + receipt)
        self.fresh()
        self.edit_seal(lambda seal: seal.update(byte_budget=records + receipt - 1))
        self.refuse(f"records/enwiki-1001-2002.jsonl: this source's records and receipts are {records + receipt} "
                    f"bytes, over the byte_budget of {records + receipt - 1}")

    # ---- universe.json ----

    def test_libraries_are_a_list_sorted_by_id(self):
        self.edit_universe(lambda u: u.update(libraries={lib["id"]: {k: v for k, v in lib.items() if k != "id"}
                                                         for lib in u["libraries"]}))
        self.refuse("libraries must be a list of library objects sorted by id, not an object keyed by library id")
        self.fresh()
        self.edit_universe(lambda u: u["libraries"].reverse())
        self.refuse("libraries must be sorted by id, each once ('mathlib' comes after 'tauceti')")
        self.fresh()
        self.edit_universe(lambda u: u["libraries"].insert(1, dict(u["libraries"][0])))
        self.refuse("universe.json: libraries[1]: library id 'batteries' appears twice: libraries are keyed by id")

    def test_universe_keys_are_exactly_the_agreed_ones(self):
        def old_shape(u):
            u["sha256"] = u.pop("universe_sha256")
            del u["frozen"]
        self.edit_universe(old_shape)
        self.refuse("universe.json: missing 'universe_sha256', 'frozen'")
        self.refuse("universe.json: the universe's sha is universe_sha256, not sha256")
        self.fresh()
        self.edit_universe(lambda u: u.pop("excluded"))
        self.refuse("universe.json: missing 'excluded'")
        self.fresh()
        self.edit_universe(lambda u: u["excluded"][0].update(repo="https://github.com/facebookresearch/atlas-lean"))
        self.refuse("universe.json: excluded[0]: unexpected 'repo'")

    def test_each_library_has_exactly_the_agreed_keys(self):
        for key in build_store.LIBRARY_KEYS[1:]:
            with self.subTest(missing=key):
                self.fresh()
                self.edit_universe(lambda u, key=key: u["libraries"][3].pop(key))
                self.refuse(f"universe.json: libraries[3]: missing {key!r}")
        for extra, fragment in ((("index", {"path": "index/mathlib/"}), "unexpected 'index'"),
                                (("conventions", None), "unexpected 'conventions'"),
                                (("src_dir", "src"), "src_dir is not supported: a lean end's path is relative "
                                                     "to the repository root")):
            with self.subTest(extra=extra[0]):
                self.fresh()
                self.edit_universe(lambda u, extra=extra: u["libraries"][2].update([extra]))
                self.refuse(fragment)
        for licence in ("Apache-2.0", {"spdx": ""}, {"spdx": "MIT", "paths": {}}, None):
            with self.subTest(licence=licence):
                self.fresh()
                self.edit_universe(lambda u, licence=licence: u["libraries"][0].update(licence=licence))
                self.refuse("licence must be {spdx: <SPDX id>}")
        for roots in ([], "Mathlib", [""], None):
            with self.subTest(roots=roots):
                self.fresh()
                self.edit_universe(lambda u, roots=roots: u["libraries"][3].update(roots=roots))
                self.refuse("roots must be a non-empty list of module roots")
        # display_name is the one optional key: absent or null builds.
        self.fresh()
        self.edit_universe(lambda u: u["libraries"][0].update(display_name=None))
        tree = build_store.build(self.set)
        universe = json.loads(tree[f"atlas/v1/universes/{make_fixture.UNIVERSE_SHA}.json"])
        self.assertEqual([lib["id"] for lib in universe["libraries"]],
                         ["batteries", "ff-widgets", "lean-core", "mathlib", "tauceti"])
        self.assertNotIn("display_name", universe["libraries"][0])
        self.assertEqual(universe["libraries"][3], {
            "id": "mathlib", "brain_lib": "Mathlib", "display_name": "Mathlib", "licence": "Apache-2.0",
            "repo": "https://github.com/leanprover-community/mathlib4", "commit": make_fixture.LIB["mathlib"]["commit"],
            "roots": ["Mathlib"], "toolchain": "leanprover/lean4:v4.24.0"})
        self.assertEqual(universe["excluded"], [{"id": "atlas-lean", "reason": "licence forbids ML evaluation use"}])

    # ---- records ----

    def test_every_record_carries_section_and_prominence(self):
        for key in ("section", "prominence"):
            for row in (0, 3, 5):  # a location, the absence, the unresolved record
                with self.subTest(key=key, row=row):
                    self.fresh()
                    self.edit_widget(lambda rows, key=key, row=row: rows[row]["extensions"][LOCATE].pop(key))
                    self.refuse(f"records/enwiki-1001-2002.jsonl:{row + 1}: "
                                + ("section must be its passage's heading path" if key == "section"
                                   else "prominence must be one of"))
        self.fresh()
        self.edit_widget(lambda rows: [row["extensions"][LOCATE].update(section=["Statement", ""]) for row in rows[:2]])
        self.refuse("section must be its passage's heading path")

    def test_end_line_is_optional_but_never_null_or_before_line(self):
        def drop(rows):
            del rows[0]["object"][0]["extensions"][LOCATE]["end_line"]
        self.edit_widget(drop)
        loc = self.article()["passages"][0]["locations"][0]
        self.assertEqual(loc["line"], 612)
        self.assertNotIn("end_line", loc)
        for end_line in (None, 611, "640", 640.0):
            with self.subTest(end_line=end_line):
                self.fresh()
                self.edit_widget(lambda rows, e=end_line: rows[0]["object"][0]["extensions"][LOCATE].update(end_line=e))
                self.refuse("end_line, when the index has one, must be an integer >= line (omit it, never null")
        for line in (None, 0, "612"):
            with self.subTest(line=line):
                self.fresh()
                self.edit_widget(lambda rows, v=line: rows[0]["object"][0]["extensions"][LOCATE].update(line=v))
                self.refuse("line must be an integer >= 1")
        self.fresh()
        self.edit_widget(lambda rows: rows[0]["object"][0]["extensions"][LOCATE].pop("line"))
        self.refuse("line must be an integer >= 1")

    def test_a_close_match_always_names_its_difference(self):
        # (An empty note is already refused by the Literature Links schema.)
        for note in (None, "   "):
            with self.subTest(note=note):
                self.fresh()
                self.edit_widget(lambda rows, note=note: rows[1].pop("note") if note is None else rows[1].update(note=note))
                self.refuse("a closeMatch names its convention difference in note")

    def test_absence_ends_are_minimal(self):
        for extra in ({"role": "key"}, {"extensions": {LOCATE: {"library": "batteries"}}},
                      {"location": {"declaration": "X", "module": "Y"}}):
            with self.subTest(extra=sorted(extra)):
                self.fresh()
                self.edit_widget(lambda rows, extra=extra: rows[3]["object"][0].update(extra))
                self.refuse('a checked absence names each repository it searched by a minimal lean end, '
                            '{"type": "lean", "uri", "version"} only')

    def test_every_challenge_verdict_builds_and_challenge_pending_is_one(self):
        # proposal §2.7: survived, downgraded, not_required, sampled_out, challenge_pending
        # (cflib.vocab CHALLENGE_VERDICTS); the last three are unjudged, read as unchallenged.
        self.assertEqual(build_store.CHALLENGES, {"survived", "downgraded", "not_required", "sampled_out",
                                                  "challenge_pending"})
        for row in (0, 3, 5):  # a location, the absence, the unresolved record
            with self.subTest(row=row):
                self.fresh()
                self.edit_widget(lambda rows, row=row: rows[row]["extensions"][LOCATE].update(
                    challenge="challenge_pending"))
                passages = self.article()["passages"]
                passage = passages[{0: 0, 3: 2, 5: 3}[row]]
                got = (passage["locations"][0]["challenge"] if row == 0 else
                       passage["absence"]["challenge"] if row == 3 else passage["unresolved"]["challenge"])
                self.assertEqual(got, "challenge_pending")
        self.fresh()
        self.edit_widget(lambda rows: rows[0]["extensions"][LOCATE].update(challenge="pending"))
        self.refuse("challenge must be one of ['challenge_pending', 'downgraded', 'not_required', "
                    "'sampled_out', 'survived']")

    def test_seal_ids_follow_the_token_grammar(self):
        for key, value in (("set_id", "smoke-2b"), ("set_id", "tier-a:shard.03"), ("sourceset", "wpm.math-2")):
            with self.subTest(key=key, value=value):
                self.fresh()
                self.edit_seal(lambda seal, key=key, value=value: seal["sourceset"].update(id=value)
                               if key == "sourceset" else seal.update(set_id=value))
                record_set = json.loads(build_store.build(self.set)["atlas/v1/current.json"])["record_set"]
                self.assertEqual(record_set["sourceset"]["id"] if key == "sourceset" else record_set["set_id"], value)
        rule = "must be a token: a letter or digit, then letters, digits and ._:- (at most 128 characters)"
        for value in ("smoke+2", "_draft", "smoke 2", "x" * 129, ""):
            with self.subTest(set_id=value):
                self.fresh()
                self.edit_seal(lambda seal, value=value: seal.update(set_id=value))
                self.refuse(f"SEAL.json: set_id {rule}")
        for value in ("wpm+math", ".hidden"):
            with self.subTest(sourceset=value):
                self.fresh()
                self.edit_seal(lambda seal, value=value: seal["sourceset"].update(id=value))
                self.refuse(f"SEAL.json: sourceset.id {rule}")

    def test_receipt_counts_are_the_lengths_of_its_lists(self):
        # A repeated query stays in the receipt file, so the count is the list's length, not
        # the number of distinct queries (gate rule 5's floor of 3 is on distinct ones).
        body = dict(make_fixture.RECEIPT)
        body["queries"] = [*body["queries"], body["queries"][0]]
        path = self.replace_receipt(body, queries=4, candidates=2)
        self.refuse(f"receipt.queries is 4, but {path} lists 5 queries (each count is the length of that list "
                    "in the receipt file, repeats included)")
        self.fresh()
        self.replace_receipt(body, queries=5, candidates=2)
        self.assertEqual(self.article()["passages"][2]["absence"]["receipt"]["queries"], 5)
        self.fresh()
        path = self.replace_receipt(make_fixture.RECEIPT, queries=4, candidates=3)
        self.refuse(f"receipt.candidates is 3, but {path} lists 2 candidates")

    def test_repository_grammar(self):
        ff = make_fixture.LIB["ff-widgets"]["repo"]
        for repo in ("https://gitlab.example.org/group/sub/lean/beta", "https://git.example.org/beta",
                     "https://github.com/FormalFrontier/Widget_Theory.v2~x"):
            with self.subTest(accepted=repo):
                self.fresh()
                self.replace_everywhere(ff, repo)
                self.assertEqual(self.article()["passages"][0]["locations"][1]["repo"], repo)
        rule = ("repo must be an https repository URL: a dotted host with no port, then one to four path segments "
                "of 1-100 letters, digits and _.~- each, none of them '.' or '..', and no trailing slash, query or "
                "fragment")
        for repo in (ff + "/", "https://gitlab.example.org/group/sub/team/lean/beta", "https://github.com",
                     "https://localhost/beta", "https://github.com:443/FormalFrontier/WidgetTheory",
                     "http://github.com/FormalFrontier/WidgetTheory", "https://github.com/FormalFrontier//WidgetTheory",
                     ff + "?tab=readme", ff + "#readme", "https://github.com/FormalFrontier/..",
                     "https://user@github.com/FormalFrontier/WidgetTheory"):
            with self.subTest(refused=repo):
                self.fresh()
                self.replace_everywhere(ff, repo)
                self.refuse(f"universe.json: libraries[1]: {rule}")

    def test_universe_string_caps(self):
        # An exclusion's reason is prose, at most 1000 characters; the real u1's longest is 215.
        reason = ("excluded because " + "its licence forbids use in evaluation " * 10).strip()
        self.assertGreater(len(reason), 300)
        self.edit_universe(lambda u: u["excluded"][0].update(reason=reason))
        tree = build_store.build(self.set)
        universe = json.loads(tree[f"atlas/v1/universes/{make_fixture.UNIVERSE_SHA}.json"])
        self.assertEqual(universe["excluded"][0]["reason"], reason)
        for value, fragment in (("r" * 1001, "reason is 1001 characters, longer than 1000 (at most 1000 characters)"),
                                ("  ", "reason must be a non-empty string"), (None, "reason must be a non-empty string")):
            with self.subTest(reason=value if value is None else len(value)):
                self.fresh()
                self.edit_universe(lambda u, value=value: u["excluded"][0].update(reason=value))
                self.refuse(f"universe.json: excluded[0]: {fragment}")
        self.fresh()
        self.edit_universe(lambda u: u["excluded"][0].update(id="atlas lean"))
        self.refuse("universe.json: excluded[0]: id must be a token: a letter or digit")
        # A library's brain_lib, toolchain and each root are names, at most 300 characters.
        for key in ("brain_lib", "toolchain"):
            with self.subTest(key=key):
                self.fresh()
                self.edit_universe(lambda u, key=key: u["libraries"][3].update({key: "M" * 301}))
                self.refuse(f"universe.json: libraries[3]: {key} is 301 characters, longer than 300")
                self.fresh()
                self.edit_universe(lambda u, key=key: u["libraries"][3].update({key: "M" * 300}))
                build_store.build(self.set)
        self.fresh()
        self.edit_universe(lambda u: u["libraries"][3].update(roots=["Mathlib", "M" * 301]))
        self.refuse("roots must be a non-empty list of module roots, each a non-empty string of at most 300")

    def test_licence_is_never_null(self):
        self.edit_universe(lambda u: u["libraries"][1].update(licence=None))
        self.refuse("universe.json: libraries[1]: licence must be {spdx: <SPDX id>}, never null")

    def test_a_location_end_requires_module_path_and_line(self):
        # Only end_line is optional (test_end_line_is_optional_but_never_null_or_before_line).
        for edit, fragment in (
                (lambda end: end["extensions"][LOCATE].pop("path"), "path is missing or null"),
                (lambda end: end["extensions"][LOCATE].update(path=None), "path is missing or null"),
                (lambda end: end["extensions"][LOCATE].pop("line"), "line is missing or null"),
                (lambda end: end["location"].pop("module"),
                 "the lean end names a declaration and its module (location.declaration and location.module "
                 "are required)")):
            with self.subTest(fragment=fragment):
                self.fresh()
                self.edit_widget(lambda rows, edit=edit: edit(rows[0]["object"][0]))
                self.refuse(f"records/enwiki-1001-2002.jsonl:1: {fragment}")


class SharedRepositoryTests(SetCopyTest):
    """Several libraries per repository (docs/ATLAS.md section 3 and contract note 13).

    Proposal §2.3 lets a universe split one repository into libraries, one root each
    (lean-core's Init and Std at one lean4 commit); WikiLean agreed on 2026-10-19 to key
    libraries by id and refuse only a true duplicate (an id twice, or one root of one
    repository in two libraries), and formal-companion 124a587 (records.absence_ends) writes
    an absence's ends one per distinct (repo, commit).
    """

    LEAN4 = "https://github.com/leanprover/lean4"
    CORE = make_fixture.LIB["lean-core"]["commit"]
    OTHER = "6" * 40
    MATHLIB = make_fixture.LIB["mathlib"]

    def split(self, std_commit: str | None = None, keep: tuple[str, ...] | None = None,
              std_roots: tuple[str, ...] = ("Std",)) -> None:
        """lean-core becomes init (roots Init) and std (roots Std) at its repository; with
        `keep`, the universe holds only those other libraries besides the two."""
        def edit(u):
            core = next(lib for lib in u["libraries"] if lib["id"] == "lean-core")
            libraries = [lib for lib in u["libraries"] if lib["id"] != "lean-core"
                         and (keep is None or lib["id"] in keep)]
            for lib_id, roots, commit in (("init", ("Init",), self.CORE), ("std", std_roots, std_commit or self.CORE)):
                lib = {**core, "id": lib_id, "brain_lib": lib_id.capitalize(), "roots": list(roots), "commit": commit}
                del lib["display_name"]
                libraries.append(lib)
            u["libraries"] = sorted(libraries, key=lambda lib: lib["id"])
        self.edit_universe(edit)

    def lean4_end(self, row: dict, library: str, root: str, commit: str | None = None) -> None:
        """Move a location's lean end into a library of the lean4 repository."""
        end = row["object"][0]
        end.update(uri=self.LEAN4, version=commit or self.CORE)
        end["location"]["module"] = f"{root}.Data.Widget"
        end["extensions"][LOCATE].update(library=library, path=f"src/{root}/Data/Widget.lean")

    @staticmethod
    def absence_ends(absence: dict, *pairs: tuple[str, str]) -> None:
        absence["object"] = [{"type": "lean", "uri": uri, "version": version} for uri, version in pairs]

    def test_init_and_std_at_one_lean4_commit_build_beside_mathlib(self):
        self.split(keep=("mathlib",))
        def edit(rows):
            del rows[1]                                  # the ff-widgets close match; mathlib still formalizes it
            self.lean4_end(rows[1], "std", "Std")        # tauceti's statement_only match
            self.lean4_end(rows[3], "init", "Init")      # batteries' related match, beside the absence
            # One end per distinct (repo, commit), placed by the first library by id.
            self.absence_ends(rows[2], (self.LEAN4, self.CORE), (self.MATHLIB["repo"], self.MATHLIB["commit"]))
        self.edit_widget(edit)
        tree = build_store.build(self.set)
        universe = json.loads(tree[f"atlas/v1/universes/{make_fixture.UNIVERSE_SHA}.json"])
        self.assertEqual([(lib["id"], lib["repo"], lib["commit"], lib["roots"]) for lib in universe["libraries"]], [
            ("init", self.LEAN4, self.CORE, ["Init"]),
            ("mathlib", self.MATHLIB["repo"], self.MATHLIB["commit"], ["Mathlib"]),
            ("std", self.LEAN4, self.CORE, ["Std"])])
        article = json.loads(tree["atlas/v1/articles/1001.json"])
        # Libraries are per id in the view model, though two share a repository.
        self.assertEqual(article["universe"]["libraries"], 3)
        self.assertEqual(sorted(article["summary"]["by_library"]), ["init", "mathlib", "std"])
        self.assertEqual(article["summary"]["by_library"]["std"],
                         {"locations": 1, "complete": 0, "incomplete": 1, "passages": 1, "related": 0})
        self.assertEqual(article["summary"]["by_library"]["init"],
                         {"locations": 0, "complete": 0, "incomplete": 0, "passages": 0, "related": 1})
        bounds, absent = article["passages"][1], article["passages"][2]
        self.assertEqual({key: bounds["locations"][0][key] for key in ("library", "repo", "commit", "path")},
                         {"library": "std", "repo": self.LEAN4, "commit": self.CORE,
                          "path": "src/Std/Data/Widget.lean"})
        self.assertEqual((bounds["status"], bounds["basis"]), ("statement_only", bounds["locations"][0]["location_key"]))
        # "Searched 3 libraries": the absence counts libraries; its records name two repositories.
        self.assertEqual((absent["outcome"], absent["absence"]["libraries"]), ("absent", 3))
        self.assertEqual([loc["library"] for loc in absent["locations"]], ["init"])
        records = [json.loads(line) for line in tree["atlas/v1/records/1001.jsonl"].decode().splitlines()]
        self.assertEqual([end["uri"] for end in records[2]["object"]], [self.LEAN4, self.MATHLIB["repo"]])
        current = json.loads(tree["atlas/v1/current.json"])
        self.assertEqual(sorted(current["counts"]["by_library"]), ["init", "mathlib", "std"])
        self.assertEqual(current["universe"]["libraries"], 3)
        index = json.loads(tree[f"atlas/v1/index/{build_store.title_shard('Widget lemma')}.json"])
        self.assertEqual(index["articles"][0][-1], ["mathlib", "std"])  # in universe order, locations only

    def test_an_absence_names_each_repository_at_its_commit_once(self):
        # The fixture's absence names lean4 once: with lean-core split at one commit it still builds.
        self.split()
        article = self.article()
        self.assertEqual(article["passages"][2]["absence"]["libraries"], 6)
        lean4 = {"type": "lean", "uri": self.LEAN4, "version": self.CORE}
        ends = [end for end in read_jsonl(self.set / "records/enwiki-1001-2002.jsonl")[3]["object"]]
        self.assertEqual(ends.count(lean4), 1)
        # One end per universe library (the export before 124a587) names lean4 twice: refused.
        self.edit_widget(lambda rows: rows[3]["object"].insert(2, dict(lean4)))
        self.refuse("a checked absence names every repository of the universe at its commit, once each: one end "
                    "per distinct (repo, commit) of its libraries")
        # One repository at two commits is two search targets, so two ends.
        self.fresh()
        self.split(std_commit=self.OTHER)
        self.refuse("names every repository of the universe at its commit, once each")
        self.edit_widget(lambda rows: rows[3]["object"].append({"type": "lean", "uri": self.LEAN4, "version": self.OTHER}))
        self.assertEqual(self.article()["passages"][2]["absence"]["libraries"], 6)

    def test_a_duplicate_library_id_is_refused(self):
        self.split()
        def twice(u):
            std = next(lib for lib in u["libraries"] if lib["id"] == "std")
            u["libraries"].insert(u["libraries"].index(std) + 1, {
                **std, "repo": "https://github.com/leanprover/std4", "roots": ["Std4"]})
        self.edit_universe(twice)
        self.refuse("universe.json: libraries[5]: library id 'std' appears twice: libraries are keyed by id, each id "
                    "once, and a repository may be several libraries, but each of its roots belongs to one library")

    def test_one_root_of_one_repository_belongs_to_one_library(self):
        for commit in (None, self.OTHER):
            with self.subTest(std_commit=commit):
                self.fresh()
                self.split(std_commit=commit, std_roots=("Std", "Init"))
                self.refuse("universe.json: libraries[4]: root 'Init' of https://github.com/leanprover/lean4 is also "
                            "library 'init''s: libraries are keyed by id")
        self.fresh()
        self.split(std_roots=("Std", "Std"))
        self.refuse("universe.json: libraries[4]: roots names one root twice")
        # A root of one name in two repositories is two roots: it builds.
        self.fresh()
        self.split()
        self.edit_universe(lambda u: next(lib for lib in u["libraries"] if lib["id"] == "batteries")["roots"].append("Std"))
        self.assertEqual(self.article()["universe"]["libraries"], 6)


class LocateExtensionTests(SetCopyTest):
    """The locate extension's agreed keys at its three places (docs/ATLAS.md section 3, "The locate
    extension's keys"), and the subject end's optional span and layer_sha256 (formal-companion
    722b30f): any other key is refused naming it, its place and the record id."""

    WIDGET_ID = "enwiki-1001-2002::In-text #3fa91c07"
    UNEXPECTED = "(not in the agreed record-set format, docs/ATLAS.md section 3)"
    SPAN_RULE = ("span must be the passage's half-open code-point offsets on the layer, [start, end] with "
                 "integers 0 <= start < end (formal-companion 722b30f)")

    @staticmethod
    def subject_ext(row: dict) -> dict:
        return row["subject"][0]["extensions"][LOCATE]

    def layer_sha(self, source_id: str = "enwiki-1001-2002") -> str:
        rows = read_jsonl(self.set / "sources.jsonl")
        return next(row["layer_sha256"] for row in rows if row["source_id"] == source_id)

    def add_spans(self, tail: int = 0, sha: bool = True) -> None:
        """Give every Widget record its passage's span, `tail` characters past its quote, on the layer.

        With tail 0 the span's text is the quote's exact, so span_sha256 becomes its sha256 (sha);
        records of one passage share one span, as the export writes them.
        """
        layer = self.layer_sha()

        def edit(rows):
            starts: dict[str, int] = {}
            for row in rows:
                exact = row["subject"][0]["location"]["quote"]["exact"]
                start = starts.setdefault(row["id"], 1000 * len(starts))
                ext = self.subject_ext(row)
                ext.update(span=[start, start + len(exact) + tail], layer_sha256=layer)
                if sha:
                    ext["span_sha256"] = hashlib.sha256(exact.encode()).hexdigest()
        self.edit_widget(edit)

    def test_an_unknown_key_at_each_level_is_refused_by_name(self):
        lean = lambda row: row["object"][0]["extensions"][LOCATE]  # noqa: E731
        cases = [
            # (row, the extension to edit, key, its place in the message)
            (0, lambda row: row["extensions"][LOCATE], "x_note", "a location record's locate extension"),
            (3, lambda row: row["extensions"][LOCATE], "x_note", "an absence record's locate extension"),
            (5, lambda row: row["extensions"][LOCATE], "x_note", "an unresolved record's locate extension"),
            (0, self.subject_ext, "spann", "the subject end's locate extension"),
            (0, lean, "endline", "the lean end's locate extension"),
            # A key of another shape is unknown to this one.
            (3, lambda row: row["extensions"][LOCATE], "rank", "an absence record's locate extension"),
            (5, lambda row: row["extensions"][LOCATE], "location_key", "an unresolved record's locate extension"),
            (0, lambda row: row["extensions"][LOCATE], "receipt", "a location record's locate extension"),
            (3, lambda row: row["extensions"][LOCATE]["receipt"], "sha", "the absence's receipt"),
        ]
        for row, place, key, level in cases:
            with self.subTest(row=row, key=key, level=level):
                self.fresh()
                self.edit_widget(lambda rows, row=row, place=place, key=key: place(rows[row]).update({key: 1}))
                rid = read_jsonl(self.set / "records/enwiki-1001-2002.jsonl")[row]["id"]
                self.refuse(f"records/enwiki-1001-2002.jsonl:{row + 1}: record {rid}: unexpected key {key!r} in "
                            f"{level} {self.UNEXPECTED}")
        # Every unknown key is named, in one message per place.
        self.fresh()
        self.edit_widget(lambda rows: self.subject_ext(rows[0]).update(b_key=1, a_key=None))
        self.refuse(f"record {self.WIDGET_ID}: unexpected keys 'a_key', 'b_key' in the subject end's locate extension")

    def test_only_the_locate_extension_rides_on_a_record_and_its_ends(self):
        for place, level in ((lambda row: row, "the record"), (lambda row: row["subject"][0], "the subject end"),
                             (lambda row: row["object"][0], "the lean end")):
            with self.subTest(level=level):
                self.fresh()
                self.edit_widget(lambda rows, place=place: place(rows[0])["extensions"].update(
                    {"org.example.other": {"note": "x"}}))
                self.refuse(f"records/enwiki-1001-2002.jsonl:1: record {self.WIDGET_ID}: unexpected extension "
                            f"'org.example.other' on {level}: the agreed record-set format carries only {LOCATE!r}")

    def test_the_subject_end_carries_renderer_licence_and_span_sha256(self):
        for key, value, fragment in (
                ("renderer", None, "the subject end's renderer must be its source's renderer token"),
                ("renderer", "web-html/2", "the subject end's renderer must be its source's renderer token"),
                ("renderer", "web html", "the subject end's renderer must be its source's renderer token"),
                ("licence", None, "the subject end's licence, the source's SPDX licence id, must be a non-empty"),
                ("licence", " ", "the subject end's licence, the source's SPDX licence id, must be a non-empty"),
                ("licence", "L" * 301, "the subject end's licence, the source's SPDX licence id, is 301 characters, "
                                       "longer than 300"),
                ("span_sha256", None, "the subject end's span_sha256, the sha256 of the passage's span, must be 64"),
                ("span_sha256", "A" * 64, "the subject end's span_sha256, the sha256 of the passage's span, must"),
                ("pageid", 1001.0, "subject extension pageid and html_sha256 must match sources.jsonl"),
                ("pageid", True, "subject extension pageid and html_sha256 must match sources.jsonl")):
            with self.subTest(key=key, value=value if not isinstance(value, str) else value[:20]):
                self.fresh()
                self.edit_widget(lambda rows, key=key, value=value: self.subject_ext(rows[0]).update({key: value}))
                self.refuse(f"records/enwiki-1001-2002.jsonl:1: {fragment}")
        for key in ("renderer", "licence", "span_sha256"):
            with self.subTest(missing=key):
                self.fresh()
                self.edit_widget(lambda rows, key=key: self.subject_ext(rows[0]).pop(key))
                self.refuse(f"records/enwiki-1001-2002.jsonl:1: the subject end's {key}")

    def test_the_records_of_an_article_name_one_licence(self):
        # The licence is the source's, so the article view states it once (`licence.text`).
        self.edit_widget(lambda rows: self.subject_ext(rows[2]).update(licence="CC-BY-SA-3.0"))
        self.refuse("records/enwiki-1001-2002.jsonl: every record of an article names one licence, its source's "
                    "(the subject end's licence): CC-BY-SA-3.0, CC-BY-SA-4.0")
        self.fresh()
        self.edit_widget(lambda rows: [self.subject_ext(row).update(licence="CC-BY-SA-3.0") for row in rows])
        self.assertEqual(self.article()["licence"], {"text": "CC-BY-SA-3.0", "mapping": "CC0-1.0"})

    def test_a_receipt_count_is_an_integer(self):
        # 2.0 == 2 in Python, so a float count once passed as the list's length.
        for value in (2.0, True, "2", None):
            with self.subTest(candidates=value):
                self.fresh()
                self.edit_widget(lambda rows, value=value: rows[3]["extensions"][LOCATE]["receipt"].update(
                    candidates=value))
                self.refuse("records/enwiki-1001-2002.jsonl:4: receipt.candidates must be an integer: the length of "
                            "the candidates list in receipts/")

    def without_spans(self) -> list[dict]:
        """The Widget passages the same set builds to with every span and layer_sha256 taken out."""
        self.edit_widget(lambda rows: [self.subject_ext(row).pop(key, None) for row in rows
                                       for key in ("span", "layer_sha256")])
        return self.article()["passages"]

    def test_a_valid_span_builds_to_the_same_view(self):
        # The span and layer_sha256 are optional, and quote_extent reads the span only as a
        # shortcut to what span_sha256 gives: a set with them builds to the same passages as the
        # same set without them, and the records, which the store keeps verbatim, keep them.
        before = self.article()["passages"]
        # The Widget bound is a quote at the cap: a span no longer than it (with span_sha256 the
        # quote's sha) is its whole passage, and one that runs 60 characters on is a capped head.
        for tail, sha, capped in ((0, True, False), (60, False, True)):
            with self.subTest(tail=tail):
                self.fresh()
                self.add_spans(tail=tail, sha=sha)
                tree = build_store.build(self.set)
                passages = json.loads(tree["atlas/v1/articles/1001.json"])["passages"]
                records = [json.loads(line) for line in tree["atlas/v1/records/1001.jsonl"].decode().splitlines()]
                self.assertEqual(self.subject_ext(records[0])["span"], [0, 44 + tail])
                self.assertEqual(self.subject_ext(records[0])["layer_sha256"], self.layer_sha())
                self.assertIs(passages[1]["quote_capped"], capped)
                self.assertEqual(passages, self.without_spans())
                if not sha:
                    self.assertEqual(passages, before)
        # A tail the suffix gives in full (one character): the span's text is exact and the
        # suffix's first character, and span_sha256 must be its sha256.
        self.fresh()
        self.add_spans(tail=1, sha=False)
        self.refuse("records/enwiki-1001-2002.jsonl:1: span_sha256 is not the sha256 of the span's text, which the "
                    "quote gives in full (exact and the first 1 characters of its suffix)")

        def space_sha(rows):
            for row in rows:
                quote = row["subject"][0]["location"]["quote"]
                text = quote["exact"] + quote.get("suffix", "")[:1]
                self.subject_ext(row)["span_sha256"] = hashlib.sha256(text.encode()).hexdigest()
        self.edit_widget(space_sha)
        passages = self.article()["passages"]
        self.assertEqual(passages, self.without_spans())

    def test_the_fixture_with_its_passages_spans_builds_its_golden_view(self):
        # The F5 shape on every record of the fixture. The two quotes at the cap get their
        # passages' own lengths: the Widget bound's passage runs on 10 characters past its quote
        # (" its rank."), past its empty suffix by more than any trailing-space run, so its span
        # alone makes it a capped head; the Gadget equivalence's passage is its quote, a tail of
        # 0, so its span alone makes it whole. The passages under the cap (stand-in
        # span_sha256 values) are whole whatever their span, which runs 40 characters past
        # their suffix. The view is the golden store's: the span and span_sha256 agree.
        tails = {"Moreover, the number of spanning gadgets of a widget is at most": 10,
                 "Moreover, the gadgets on a widget correspond bijectively to all its vertices.": 0}
        for name in ("enwiki-1001-2002", "enwiki-1003-2004"):
            path = self.set / "records" / f"{name}.jsonl"
            rows = read_jsonl(path)
            starts: dict[str, int] = {}
            for row in rows:
                quote = row["subject"][0]["location"]["quote"]
                start = starts.setdefault(row["id"], 1000 * len(starts))
                tail = tails.get(quote["exact"], len(quote.get("suffix", "")) + 40)
                self.subject_ext(row).update(span=[start, start + len(quote["exact"]) + tail],
                                             layer_sha256=self.layer_sha(name))
            write_jsonl(path, rows)
        reseal(self.set)
        tree = build_store.build(self.set)
        for pageid in (1001, 1003):
            built = json.loads(tree[f"atlas/v1/articles/{pageid}.json"])
            golden = json.loads((STORE / f"atlas/v1/articles/{pageid}.json").read_text())
            self.assertEqual((built["passages"], built["summary"]), (golden["passages"], golden["summary"]))
        self.assertEqual([p["quote_capped"] for p in json.loads(tree["atlas/v1/articles/1001.json"])["passages"]],
                         [False, True, False, False])
        self.assertEqual([p["quote_capped"] for p in json.loads(tree["atlas/v1/articles/1003.json"])["passages"]],
                         [False, False, False])

    def test_a_malformed_span_or_layer_hash_is_refused(self):
        for span in ([5, 5], [6, 5], [-1, 50], [0], [0, 50, 60], ["0", "50"], [0.0, 50], [True, 50], [0, 50.0],
                     "0-50", None, {}, {"start": 0, "end": 50}):
            with self.subTest(span=span):
                self.fresh()
                self.add_spans()
                self.edit_widget(lambda rows, span=span: self.subject_ext(rows[0]).update(span=span))
                self.refuse(f"records/enwiki-1001-2002.jsonl:1: {self.SPAN_RULE}")
        for layer, fragment in (("A" * 64, "layer_sha256 must be 64 lowercase hex"),
                                ("0" * 63, "layer_sha256 must be 64 lowercase hex"),
                                ("0" * 64 + "\n", "layer_sha256 must be 64 lowercase hex"),
                                (None, "layer_sha256 must be 64 lowercase hex"),
                                (["0" * 64], "layer_sha256 must be 64 lowercase hex"),
                                ("0" * 64, "layer_sha256 must be the source's layer_sha256 (sources.jsonl)")):
            with self.subTest(layer=layer):
                self.fresh()
                self.add_spans()
                self.edit_widget(lambda rows, layer=layer: self.subject_ext(rows[0]).update(layer_sha256=layer))
                self.refuse(f"records/enwiki-1001-2002.jsonl:1: {fragment}")
        for key in ("span", "layer_sha256"):
            with self.subTest(alone=key):
                self.fresh()
                self.add_spans()
                self.edit_widget(lambda rows, key=key: self.subject_ext(rows[0]).pop(
                    "layer_sha256" if key == "span" else "span"))
                self.refuse("records/enwiki-1001-2002.jsonl:1: span and layer_sha256 come together or not at all")
        # The quote is the head of its passage, so it fits in the span.
        self.fresh()
        self.add_spans(tail=-1, sha=False)
        self.refuse("records/enwiki-1001-2002.jsonl:1: the quote's exact is 44 characters, longer than its span "
                    "[0, 43] (43): the quote is the head of its passage")
        # A span that is not the quote's text, by the quote's own sha.
        self.fresh()
        self.add_spans()
        self.edit_widget(lambda rows: [self.subject_ext(row).update(span_sha256="5" * 64) for row in rows[:2]])
        self.refuse("records/enwiki-1001-2002.jsonl:1: span_sha256 is not the sha256 of the span's text, which the "
                    "quote gives in full (exact)")
        # A passage field: every record of the passage carries the same span.
        self.fresh()
        self.add_spans(tail=60, sha=False)
        self.edit_widget(lambda rows: self.subject_ext(rows[1]).update(span=[1, 105]))
        self.refuse(f"records/enwiki-1001-2002.jsonl: {self.WIDGET_ID}: records of one passage disagree on span")
        self.fresh()
        self.edit_widget(lambda rows: self.subject_ext(rows[1]).update(span_sha256="5" * 64))
        self.refuse(f"records/enwiki-1001-2002.jsonl: {self.WIDGET_ID}: records of one passage disagree on span_sha256")


class MalformedValueTests(unittest.TestCase):
    """A malformed value anywhere is a refusal (exit 2), never a crash (docs/ATLAS.md, section 5)."""

    HOSTILE = [None, True, 5, 1.5, "x", [], ["a"], {}, {"a": [1]}]
    WIDGET = "records/enwiki-1001-2002.jsonl"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atlas-malformed-"))
        self.set = self.tmp / "set"
        shutil.copytree(RECORDSET, self.set)
        self.original = {p: p.read_bytes() for p in self.set.rglob("*") if p.is_file()}

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def restore(self) -> None:
        for path, data in self.original.items():
            path.write_bytes(data)

    @staticmethod
    def put(value, path, new):
        for step in path[:-1]:
            value = value[step]
        value[path[-1]] = new

    def mutate(self, relative: str, row: int | None, path: list, new) -> None:
        target = self.set / relative
        if row is None:
            document = json.loads(target.read_text(encoding="utf-8"))
            self.put(document, path, new)
            target.write_text(make_fixture.dumps(document) + "\n", encoding="utf-8")
            if relative == "SEAL.json":
                return  # reseal() would rewrite the file map it reads from SEAL.json
        else:
            rows = read_jsonl(target)
            self.put(rows[row], path, new)
            write_jsonl(target, rows)
        reseal(self.set)

    def build_outcome(self) -> str:
        try:
            build_store.build(self.set)
        except build_store.AtlasError:
            return "refused"
        except Exception as exc:  # noqa: BLE001 - the point of the test
            return f"crashed: {type(exc).__name__}: {exc}"
        return "built"

    def test_no_malformed_value_crashes_the_builder(self):
        loc_end = ["object", 0, "extensions", LOCATE]
        targets = [(self.WIDGET, 0, path) for path in (
            ["kind"], ["relation"], ["status"], ["note"], ["id"],
            ["extensions", LOCATE, "challenge"], ["extensions", LOCATE, "confidence"],
            ["extensions", LOCATE, "rank"], ["extensions", LOCATE, "location_key"],
            ["extensions", LOCATE, "section"], ["extensions", LOCATE, "prominence"],
            ["extensions", LOCATE, "item"], ["extensions", LOCATE, "universe"],
            [*loc_end, "proof_status"], [*loc_end, "library"], [*loc_end, "path"], [*loc_end, "line"],
            [*loc_end, "end_line"], [*loc_end, "statement"], ["object", 0, "location"],
            ["object", 0, "location", "declaration"], ["object", 0, "uri"], ["object", 0, "version"],
            ["subject", 0, "uri"], ["subject", 0, "version"], ["subject", 0, "location", "quote"],
            ["subject", 0, "location", "quote", "exact"], ["subject", 0, "location", "quote", "prefix"],
            ["subject", 0, "extensions", LOCATE, "pageid"], ["subject", 0, "extensions", LOCATE, "renderer"],
            ["subject", 0, "extensions", LOCATE, "licence"], ["subject", 0, "extensions", LOCATE, "span_sha256"],
            ["subject", 0, "extensions", LOCATE, "span"], ["subject", 0, "extensions", LOCATE, "layer_sha256"],
            ["subject", 0, "extensions", LOCATE], ["subject", 0, "extensions"], ["object", 0, "extensions"],
            ["extensions", LOCATE, "x_unknown"], [*loc_end, "x_unknown"])]
        targets += [(self.WIDGET, 3, path) for path in (
            ["extensions", LOCATE, "receipt"], ["extensions", LOCATE, "receipt", "sha256"],
            ["extensions", LOCATE, "receipt", "queries"], ["extensions", LOCATE, "receipt", "candidates"],
            ["object", 0, "uri"], ["object", 0, "version"])]
        # A coverage claim's every field, on one record of the Gadget article's covered passage.
        targets += [("records/enwiki-1003-2004.jsonl", 1, ["extensions", LOCATE, "coverage", *path]) for path in (
            [], ["parts"], ["parts", 0], ["parts", 1], ["challenge"], ["complete"])]
        targets += [("universe.json", None, path) for path in (
            ["excluded"], ["excluded", 0], ["excluded", 0, "id"], ["excluded", 0, "reason"], ["excluded", 0, "repo"],
            ["libraries"], ["libraries", 0], ["libraries", 0, "id"], ["libraries", 0, "repo"],
            ["libraries", 0, "commit"], ["libraries", 0, "display_name"], ["libraries", 0, "roots"],
            ["libraries", 0, "licence"], ["libraries", 0, "licence", "spdx"], ["libraries", 0, "brain_lib"],
            ["libraries", 0, "toolchain"], ["libraries", 0, "src_dir"], ["frozen"], ["id"], ["universe_sha256"])]
        targets += [("SEAL.json", None, path) for path in (
            ["schema"], ["set_id"], ["campaign"], ["universe"], ["universe", "id"], ["universe", "universe_sha256"],
            ["sourceset"], ["sourceset", "id"], ["sourceset", "manifest_sha256"], ["skill_commit"], ["created"],
            ["byte_budget"])]
        targets += [("sources.jsonl", 0, path) for path in (
            ["renderer"], ["assessment"], ["assessment", "class"], ["title"], ["wikidata"], ["pageid"],
            ["html_sha256"], ["source_id"])]
        crashes = []
        for relative, row, path in targets:
            for value in self.HOSTILE:
                self.mutate(relative, row, path, value)
                outcome = self.build_outcome()
                if outcome.startswith("crashed"):
                    crashes.append((relative, row, path, value, outcome))
                self.restore()
        self.assertEqual(crashes, [])

    def run_main(self) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = build_store.main(["--record-set", str(self.set), "--out", str(self.tmp / "out")])
        return code, err.getvalue()

    def assert_exit_2(self, fragment: str) -> None:
        code, err = self.run_main()
        self.assertEqual(code, 2, err)
        self.assertIn(fragment, err)
        self.assertFalse((self.tmp / "out").exists())
        self.restore()

    def test_reported_crashes_are_refusals(self):
        # Each of these used to exit 3 ("internal error") instead of listing the problem.
        for value in (5, True, {"id": "x"}):
            self.mutate("universe.json", None, ["excluded"], value)
            self.assert_exit_2("excluded must be a list")
        for path, fragment in ((["extensions", LOCATE, "challenge"], "challenge must be one of"),
                               (["extensions", LOCATE, "confidence"], "confidence must be one of"),
                               (["extensions", LOCATE, "prominence"], "prominence must be one of"),
                               (["object", 0, "extensions", LOCATE, "proof_status"], "proof_status must be one of"),
                               (["extensions", LOCATE, "rank"], "rank must be an integer")):
            self.mutate(self.WIDGET, 0, path, ["a"])
            self.assert_exit_2(fragment)
        self.mutate("sources.jsonl", 0, ["renderer"], {"x": [1]})
        self.assert_exit_2("renderer must be a short token")
        # 1e400 is valid JSON that parses as infinity, which the store cannot write.
        path = self.set / "sources.jsonl"
        path.write_text(path.read_text().replace('"renderer":"web-html/1"', '"renderer":1e400', 1))
        reseal(self.set)
        self.assert_exit_2("non-finite JSON number 1e400")
        path = self.set / self.WIDGET
        path.write_text(path.read_text().replace('"rank":1', '"rank":1e400', 1))
        reseal(self.set)
        self.assert_exit_2("non-finite JSON number 1e400")
        # Nesting too deep for the parser is a refusal too.
        path.write_text("[" * 100000 + "]" * 100000 + "\n" + path.read_text())
        reseal(self.set)
        self.assert_exit_2("nested too deeply")

    def test_display_name_is_printable_ascii(self):
        # Invisible and look-alike characters could make a name read as "Mathlib".
        for name in ("Mathlib\u200b", "M\u0430thlib", "Mathlib\u00ad", "Lean core\u2060", "Tab\there"):
            self.mutate("universe.json", None, ["libraries", 4, "display_name"], name)
            self.assert_exit_2("display_name must be at most 100 printable ASCII")
        self.mutate("universe.json", None, ["libraries", 4, "display_name"], "Widget theory (FF \u00b7 fork), v2")
        self.assertEqual(self.build_outcome(), "built")


class BundleParityTests(unittest.TestCase):
    """Status rules 2 and 3 against the producer at PRODUCER_COMMIT: its truth tables and parity tables."""

    def test_the_tables_are_the_producers(self):
        for rule, (path, sha, body) in SHARED_TABLES.items():
            with self.subTest(rule=rule):
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), sha)
                doc = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(parity_body_sha256(doc), body)
                self.assertEqual((doc["schema"], doc["status_rule"]), (f"cf-locate-status-parity/{rule}", rule))
                self.assertEqual(doc["decision"]["id"], {2: "Q9", 3: "R-BROAD"}[rule])
                self.assertTrue(all(case["status_rule"] == rule for case in doc["cases"]))
                table = json.loads(PRODUCER_TABLES[rule].read_text(encoding="utf-8"))
                self.assertEqual((table["producer"]["commit"], table["status_rule"]), (PRODUCER_COMMIT, rule))
                covered = [case for case in table["cases"] if len(case) == 4]
                # Every kind x pair of part ends x bundle relation under a survived bundle.
                survived = {(case[0], json.dumps(case[1]), case[3]["relation"]) for case in covered
                            if case[3]["challenge"] == "survived" and case[3]["parts"] == [0, 1] and len(case[1]) == 2}
                self.assertEqual(len(survived), 7 * 9 * 9 * 3)
                self.assertTrue(all(case[3]["relation"] in build_store.BUNDLE_RELATIONS for case in covered))

    def test_the_builder_derives_exactly_the_producers_status_by_each_rule(self):
        for rule, path in PRODUCER_TABLES.items():
            with self.subTest(rule=rule):
                cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
                mismatches = producer_mismatches(self, cases, rule)
                self.assertEqual(mismatches, [], f"{len(mismatches)} cases differ from the producer's rule {rule}")

    def test_the_builder_derives_every_shared_case_by_each_rule(self):
        for rule, (path, _, _) in SHARED_TABLES.items():
            doc = json.loads(path.read_text(encoding="utf-8"))
            mismatches = []
            for case in doc["cases"]:
                wanted = "refused" if case.get("refused") else case["expected"]
                got = _shared_case_status(case, rule)
                if got != wanted:
                    mismatches.append((case["id"], wanted, got, case["rule"]))
            self.assertEqual(mismatches, [], f"rule {rule}: {len(mismatches)} cases differ from the shared table")
            # Each case also says what the rule before it derives: the builder agrees there too.
            before = "expected_status_rule_1" if rule == 2 else "expected_status_rule_2"
            for case in doc["cases"]:
                if before in case and rule == 3:
                    with self.subTest(rule=rule, case=case["id"]):
                        self.assertEqual(_shared_case_status(case, 2), case[before])

    def test_rule_3_changes_only_what_a_broad_match_gives(self):
        two, three = (json.loads(PRODUCER_TABLES[rule].read_text(encoding="utf-8"))["cases"] for rule in (2, 3))
        self.assertEqual([case[:2] for case in two], [case[:2] for case in three])
        changed = [(a, b) for a, b in zip(two, three) if a[2] != b[2]]
        self.assertGreater(len(changed), 0)
        for a, b in changed:
            broad = any(end[0] == "broadMatch" for end in a[1]) or (len(a) == 4 and a[3].get("relation") == "broadMatch")
            self.assertTrue(broad, a)
            self.assertEqual(b[2], "formalized", b)

    def test_a_status_rule_the_builder_does_not_know_is_refused(self):
        for rule in (0, 4, True, "3", None):
            with self.subTest(rule=rule), self.assertRaises(build_store.StatusRefused):
                build_store.derive_passage_status("theorem", [], False, None, rule)


class RecordSetTwoTests(SetCopyTest):
    """cf-locate-recordset/2: the fixture converted as the producer exports it under status rules 2 and 3."""

    WIDGET = "records/enwiki-1001-2002.jsonl"
    GADGET = "records/enwiki-1003-2004.jsonl"

    def to_v2(self, status_rule: int = 3, seal: bool = True) -> None:
        """Give the copy /2's shapes: the keys on every record, the claim as a bundle, the seal's rule."""
        for path in sorted((self.set / "records").glob("*.jsonl")):
            rows = read_jsonl(path)
            for row in rows:
                ext = row["extensions"][LOCATE]
                subject = row["subject"][0]["extensions"][LOCATE]
                ext["passage_key"] = build_store.passage_key(subject["pageid"], subject["span_sha256"])
                if row.get("relation") not in (None, "noMatch"):
                    end = row["object"][0]
                    ext["pair_key"] = build_store.pair_key(ext["passage_key"], end["uri"], end["location"]["declaration"])
            pairs = {row["extensions"][LOCATE].get("location_key"): row["extensions"][LOCATE].get("pair_key")
                     for row in rows}
            for row in rows:
                claim = row["extensions"][LOCATE].get("coverage")
                if claim is not None:
                    claim["relation"] = "exactMatch"
                    claim["conjuncts"] = [{"id": f"c{n}", "text": f"clause {n} of the conjunction", "part": key}
                                          for n, key in enumerate(claim["parts"], 1)]
                    claim["bundle_key"] = build_store.bundle_key(row["extensions"][LOCATE]["passage_key"],
                                                                 [pairs[key] for key in claim["parts"]])
            write_jsonl(path, rows)
        reseal(self.set)
        if seal:
            self.edit_seal(lambda seal: seal.update(schema="cf-locate-recordset/2", status_rule=status_rule))

    def edit_records(self, path: str, edit) -> None:
        rows = read_jsonl(self.set / path)
        edit(rows)
        write_jsonl(self.set / path, rows)
        reseal(self.set)
        self.edit_seal(lambda seal: None)  # reseal() rewrites files only; the seal keeps its schema

    def edit_v2(self, path: str, edit, status_rule: int = 3) -> None:
        """Edit the converted records, then reseal as /2 under `status_rule`."""
        rows = read_jsonl(self.set / path)
        edit(rows)
        write_jsonl(self.set / path, rows)
        reseal(self.set)
        self.edit_seal(lambda seal: seal.update(schema="cf-locate-recordset/2", status_rule=status_rule))

    @staticmethod
    def strip_v2(value):
        """A /2 view model without what /2 adds, to compare with the /1 view."""
        if isinstance(value, dict):
            return {k: RecordSetTwoTests.strip_v2(v) for k, v in value.items()
                    if k not in ("passage_key", "pair_key", "seal", "record_set", "records_sha256")}
        if isinstance(value, list):
            return [RecordSetTwoTests.strip_v2(v) for v in value]
        return value

    def test_a_v2_set_builds_the_v1_view_plus_its_keys(self):
        v1 = build_store.build(self.set)
        for rule in (2, 3):
            with self.subTest(rule=rule):
                self.fresh()
                self.to_v2(rule)
                tree = build_store.build(self.set)
                self.assertEqual(sorted(tree), sorted(v1))
                for pageid in (1001, 1003):
                    old = json.loads(v1[f"atlas/v1/articles/{pageid}.json"])
                    new = json.loads(tree[f"atlas/v1/articles/{pageid}.json"])
                    seal = json.loads((self.set / "SEAL.json").read_text())
                    self.assertEqual(new["seal"], {"set_id": "fixture-set", "schema": "cf-locate-recordset/2",
                                                   "status_rule": rule, "created": seal["created"],
                                                   "skill_commit": seal["skill_commit"]})
                    for passage in new["passages"]:
                        claim = passage["coverage"]
                        if claim is not None:
                            self.assertEqual(claim["relation"], "exactMatch")
                            self.assertEqual([c["id"] for c in claim["conjuncts"]], ["c1", "c2"])
                            self.assertRegex(claim["bundle_key"], r"^[0-9a-f]{64}$")
                            for key in ("relation", "conjuncts", "bundle_key"):
                                del claim[key]
                        self.assertRegex(passage["passage_key"], r"^[0-9a-f]{64}$")
                        for loc in passage["locations"]:
                            self.assertEqual(loc["pair_key"], build_store.pair_key(
                                passage["passage_key"], loc["repo"], loc["declaration"]))
                    self.assertEqual(self.strip_v2(new), self.strip_v2(old))
                    self.assertEqual(new["record_set"], hashlib.sha256((self.set / "SEAL.json").read_bytes()).hexdigest())
                current = json.loads(tree["atlas/v1/current.json"])
                self.assertEqual((current["record_set"]["schema"], current["record_set"]["status_rule"]),
                                 ("cf-locate-recordset/2", rule))
                self.assertNotIn("record_sets", current)
        # A /1 set's current.json names no schema or status rule: the golden store's bytes.
        self.assertNotIn("status_rule", json.loads(v1["atlas/v1/current.json"])["record_set"])

    def test_the_seal_names_its_status_rule(self):
        self.to_v2(seal=False)
        self.edit_seal(lambda seal: seal.update(schema="cf-locate-recordset/2"))
        self.refuse("SEAL.json: missing 'status_rule'")
        for rule in (1, 4, 0, True, "3", None, 2.0):
            with self.subTest(rule=rule):
                self.edit_seal(lambda seal, rule=rule: seal.update(status_rule=rule))
                self.refuse("status_rule must be 2 or 3 under 'cf-locate-recordset/2'")
        self.fresh()
        self.edit_seal(lambda seal: seal.update(status_rule=1))
        self.refuse("SEAL.json: unexpected 'status_rule'")
        self.fresh()
        self.edit_seal(lambda seal: seal.update(schema="cf-locate-recordset/3"))
        self.refuse("schema must be 'cf-locate-recordset/1'")

    def test_v2_records_under_a_v1_seal_are_refused_by_name(self):
        self.to_v2(seal=False)
        self.refuse("unexpected keys 'pair_key', 'passage_key' in a location record's locate extension")
        self.refuse("unexpected key 'passage_key' in an absence record's locate extension")

    def test_every_record_carries_its_passage_key(self):
        self.to_v2()
        self.edit_v2(self.WIDGET, lambda rows: rows[3]["extensions"][LOCATE].pop("passage_key"))
        self.refuse(build_store.PASSAGE_KEY_RULE)
        self.fresh()
        self.to_v2()
        self.edit_v2(self.WIDGET, lambda rows: rows[3]["extensions"][LOCATE].update(passage_key="0" * 64))
        self.refuse(f"{build_store.PASSAGE_KEY_RULE}; this one is not")

    def test_every_location_carries_its_pair_key_and_nothing_else_does(self):
        self.to_v2()
        self.edit_v2(self.WIDGET, lambda rows: rows[0]["extensions"][LOCATE].pop("pair_key"))
        self.refuse(build_store.PAIR_KEY_RULE)
        self.fresh()
        self.to_v2()
        self.edit_v2(self.WIDGET, lambda rows: rows[0]["extensions"][LOCATE].update(pair_key="f" * 64))
        self.refuse(f"{build_store.PAIR_KEY_RULE}; this one is not")
        # The absence (row 3) and the unresolved conjecture (row 5) name no declaration.
        for row, level in ((3, "an absence record's"), (5, "an unresolved record's")):
            with self.subTest(row=row):
                self.fresh()
                self.to_v2()
                self.edit_v2(self.WIDGET, lambda rows, row=row: rows[row]["extensions"][LOCATE].update(pair_key="a" * 64))
                self.refuse(f"unexpected key 'pair_key' in {level} locate extension")

    def test_the_claim_is_a_bundle(self):
        def bundle(edit):
            self.fresh()
            self.to_v2()
            self.edit_v2(self.GADGET, lambda rows: [edit(rows[i]["extensions"][LOCATE]["coverage"]) for i in (1, 2)])
        cases = [
            (lambda claim: claim.pop("conjuncts"), "coverage: missing 'conjuncts'"),
            (lambda claim: claim.pop("bundle_key"), "coverage: missing 'bundle_key'"),
            (lambda claim: claim.update(note="two parts"), "coverage: unexpected 'note'"),
            (lambda claim: claim.update(bundle_key="0" * 64), "coverage.bundle_key is not the sha256"),
            (lambda claim: claim.update(bundle_key="k1+k2"), "coverage.bundle_key must be 64 lowercase hex"),
            (lambda claim: claim.update(relation="hasPart"), "coverage.relation must be one of"),
            (lambda claim: claim.update(relation=None), "coverage.relation must be one of"),
            (lambda claim: claim["conjuncts"].reverse(), "coverage.conjuncts are c1..c2 in order"),
            (lambda claim: claim["conjuncts"].pop(), "coverage.conjuncts must be 2 to 5"),
            (lambda claim: claim["conjuncts"][1].update(part=claim["parts"][0]), "states no conjunct"),
            (lambda claim: claim["conjuncts"][1].update(part="e" * 64), "which is not one of the bundle's parts"),
            (lambda claim: claim["conjuncts"][0].update(text=" "), "text must be a non-empty string"),
            (lambda claim: claim["conjuncts"][0].update(relation="exactMatch"), "must be exactly {id, text, part}"),
        ]
        for edit, fragment in cases:
            with self.subTest(fragment=fragment):
                bundle(edit)
                self.refuse(fragment)
        # Under /1 the bundle's keys are refused by name, as before.
        self.fresh()
        self.to_v2(seal=False)
        for path in (self.WIDGET, self.GADGET):
            self.edit_records(path, lambda rows: [row["extensions"][LOCATE].pop(key, None)
                                                  for row in rows for key in ("passage_key", "pair_key")])
        self.refuse("coverage: unexpected 'bundle_key', 'conjuncts', 'relation'")

    def test_status_rule_3_lets_a_broad_match_formalize(self):
        # The Gadget theorem an equivalence states (row 3, data) and the Gadget definition (row 0,
        # a definition), each made a broadMatch: partial by status rules 1 and 2, formalized by 3.
        for row in (3, 0):
            def broad(rows, row=row):
                rows[row]["relation"] = "broadMatch"
            for rule, outcome in ((2, "derive partial"), (3, None)):
                with self.subTest(row=row, rule=rule):
                    self.fresh()
                    self.to_v2(rule)
                    self.edit_v2(self.GADGET, broad, rule)
                    if outcome:
                        self.refuse(f"status rule {rule}) {outcome}")
                    else:
                        passage = self.article(1003)["passages"][row if row == 0 else 2]
                        self.assertEqual((passage["status"], passage["locations"][0]["relation"]),
                                         ("formalized", "broadMatch"))

    def test_a_survived_bundle_is_one_more_location_of_its_relation(self):
        # The covered conjunction as a definition passage of two definition parts, its bundle broadMatch:
        # partial by status rule 2 (a broadMatch to a definition), formalized by 3 (R-BROAD); and an
        # exactMatch bundle formalizes it by either.
        def as_definition(relation, status):
            def edit(rows):
                for row in rows[1:3]:
                    row["kind"], row["status"] = "definition", status
                    row["object"][0]["extensions"][LOCATE]["proof_status"] = "definition"
                    row["extensions"][LOCATE]["coverage"]["relation"] = relation
            return edit
        for rule, relation, status in ((2, "broadMatch", "partial"), (3, "broadMatch", "formalized"),
                                       (2, "exactMatch", "formalized"), (3, "closeMatch", "formalized")):
            with self.subTest(rule=rule, relation=relation):
                self.fresh()
                self.to_v2(rule)
                self.edit_v2(self.GADGET, as_definition(relation, status), rule)
                passage = self.article(1003)["passages"][1]
                self.assertEqual((passage["status"], passage["coverage"]["status"], passage["coverage"]["relation"]),
                                 (status, status if status == "formalized" else "partial", relation))
                self.assertEqual(passage["coverage"]["basis"], status == "formalized")
        # A status the rule does not derive is refused, naming the rule.
        self.fresh()
        self.to_v2(2)
        self.edit_v2(self.GADGET, as_definition("broadMatch", "formalized"), 2)
        self.refuse("§1.11, status rule 2) derive partial for a definition passage with hasPart to definition, "
                    "hasPart to definition, and a broadMatch bundle over 2 parts (challenge survived)")

    def test_identifiers_carry_the_articles_qid_on_subject_passages_only(self):
        # Row 0 is the Widget subject passage; row 2 a supporting one.
        def qid(row, value):
            return lambda rows: rows[row]["subject"][0].update(identifiers=value)
        self.to_v2()
        self.edit_v2(self.WIDGET, qid(0, {"wikidata": "Q99999001"}))
        self.assertEqual(self.article(1001)["passages"][0]["prominence"], "subject")
        for edit, fragment in ((qid(0, {"wikidata": "Q99999003"}), "identifiers must be {\"wikidata\": <its source's QID>}"),
                               (qid(0, {"wikidata": "Q99999001", "viaf": "1"}), "identifiers must be"),
                               (qid(2, {"wikidata": "Q99999001"}), "only a subject passage's records carry")):
            with self.subTest(fragment=fragment):
                self.fresh()
                self.to_v2()
                self.edit_v2(self.WIDGET, edit)
                self.refuse(fragment)


class CombinedSetTests(unittest.TestCase):
    """Several sealed sets built into one store (build_store.combined_sets): the fixture's two pages as two sets."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atlas-combined-"))
        self.a, self.b = self.tmp / "a", self.tmp / "b"
        for target, keep, set_id, campaign in ((self.a, "enwiki-1001-2002", "fixture-a", "run:loc-a+chal-1"),
                                               (self.b, "enwiki-1003-2004", "fixture-b", "run:loc-b+chal-1")):
            shutil.copytree(RECORDSET, target)
            for path in (target / "records").glob("*.jsonl"):
                if path.stem != keep:
                    path.unlink()
            if keep != "enwiki-1001-2002":  # the receipt belongs to the Widget absence
                shutil.rmtree(target / "receipts")
            sources = [line for line in (target / "sources.jsonl").read_text().splitlines() if keep in line]
            (target / "sources.jsonl").write_text("\n".join(sources) + "\n")
            self.reseal(target, set_id=set_id, campaign=campaign)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    @staticmethod
    def reseal(target: Path, **fields) -> None:
        reseal(target)
        seal = json.loads((target / "SEAL.json").read_text())
        seal.update(fields)
        (target / "SEAL.json").write_text(json.dumps(seal, sort_keys=True, indent=2) + "\n")

    def test_two_sets_build_one_store_and_each_article_keeps_its_own_set(self):
        single = build_store.build(RECORDSET)
        tree = build_store.build([self.b, self.a], campaign="fixture-batch")
        self.assertEqual(sorted(tree), sorted(single))
        current = json.loads(tree["atlas/v1/current.json"])
        seals = {name: hashlib.sha256((path / "SEAL.json").read_bytes()).hexdigest()
                 for name, path in (("a", self.a), ("b", self.b))}
        self.assertEqual(current["campaign"], "fixture-batch")
        # record_set keeps its v1 shape and is the first set's by set_id; record_sets lists both.
        self.assertEqual(current["record_set"]["seal_sha256"], seals["a"])
        self.assertEqual(set(current["record_set"]), set(json.loads(single["atlas/v1/current.json"])["record_set"]))
        self.assertEqual([(r["set_id"], r["seal_sha256"], r["campaign"]) for r in current["record_sets"]],
                         [("fixture-a", seals["a"], "run:loc-a+chal-1"), ("fixture-b", seals["b"], "run:loc-b+chal-1")])
        self.assertEqual(current["counts"], json.loads(single["atlas/v1/current.json"])["counts"])
        manifest = json.loads(tree["atlas/v1/manifest.json"])
        self.assertEqual((manifest["record_set"], manifest["record_sets"]), (seals["a"], [seals["a"], seals["b"]]))
        for pageid, name, campaign in ((1001, "a", "run:loc-a+chal-1"), (1003, "b", "run:loc-b+chal-1")):
            article = json.loads(tree[f"atlas/v1/articles/{pageid}.json"])
            self.assertEqual((article["campaign"], article["record_set"]), (campaign, seals[name]))
            golden = json.loads(single[f"atlas/v1/articles/{pageid}.json"])
            for key in ("campaign", "record_set"):
                article.pop(key), golden.pop(key)
            self.assertEqual(article, golden)
        for path in single:
            if "/index/" in path or "/records/" in path or "/universes/" in path:
                self.assertEqual(tree[path], single[path], path)
        # The order the sets are given in does not matter.
        self.assertEqual(build_store.build([self.a, self.b], campaign="fixture-batch"), tree)

    def test_sets_that_disagree_or_overlap_are_refused(self):
        def refuse(fragment, sets=None, campaign="fixture-batch"):
            with self.assertRaises(build_store.AtlasError) as caught:
                build_store.build(sets or [self.a, self.b], campaign=campaign)
            self.assertIn(fragment, str(caught.exception))
        refuse("a build of several record sets needs --campaign", campaign=None)
        refuse("a build of several record sets needs --campaign", campaign="run:loc-a")
        with self.assertRaises(build_store.AtlasError) as caught:
            build_store.build(self.a, campaign="fixture-batch")
        self.assertIn("--campaign names a build of several record sets", str(caught.exception))
        refuse("set_id appears twice", [self.a, self.a])
        self.reseal(self.b, skill_commit="0" * 40)
        refuse("SEAL.json skill_commit differs from fixture-a's")
        self.reseal(self.b, skill_commit=json.loads((self.a / "SEAL.json").read_text())["skill_commit"],
                    sourceset={"id": "ss-other", "manifest_sha256": "0" * 64})
        refuse("SEAL.json sourceset differs from fixture-a's")
        overlap = self.tmp / "overlap"
        shutil.copytree(RECORDSET, overlap)
        self.reseal(overlap, set_id="fixture-c")
        refuse("page 1001 is also in fixture-a", [self.a, overlap])
        # A refusal inside one set names that set.
        (self.a / "sources.jsonl").write_text((self.a / "sources.jsonl").read_text().replace("Widget lemma", "X"))
        refuse(f"record set {self.a}:\nsources.jsonl: sha256 does not match SEAL.json", [self.a, overlap])

    def test_the_cli_builds_several_sets_into_one_store(self):
        out = self.tmp / "store"
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = build_store.main(["--record-set", str(self.a), "--record-set", str(self.b),
                                     "--campaign", "fixture-batch", "--out", str(out)])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(json.loads((out / "atlas/v1/current.json").read_text())["campaign"], "fixture-batch")
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = build_store.main(["--record-set", str(self.a), "--record-set", str(self.b), "--out", str(out),
                                     "--check"])
        self.assertEqual(code, 2)
        self.assertIn("needs --campaign", err.getvalue())


class SupplementTests(SetCopyTest):
    """The optional side files (build_store.apply_supplements): present, absent, and refused when they do not check out.

    Every side file here is synthetic and written from the fixture: the Widget bound's passage is
    the one whose span_sha256 hashes known text, the conjecture's span_sha256 is set to a text with a
    citation marker and a display formula, and the runs are invented (model-x, operator alice).
    """

    BOUND_ID = "enwiki-1001-2002::In-text #5b0e44a1"
    BOUND_TEXT = "Moreover, the number of spanning gadgets of a widget is at most its rank."
    CONJECTURE_ID = "enwiki-1001-2002::In-text #0c4d77b1"
    # The layer slice of a passage with a formula: prose, a citation marker, the formula's MathML
    # text and then its TeX annotation, as the web-html/3 layer holds every formula.
    CONJECTURE_LAYER = "every balanced widget is the image of a regular one,[3] x 2 {\\displaystyle x^{2}} ."
    CONJECTURE_SEGMENTS = [{"text": "every balanced widget is the image of a regular one,"}, {"ref": "[3]"},
                           {"text": " "}, {"tex": "x^{2}", "display": True}, {"text": " ."}]
    CAMPAIGN = "run:loc-fx+chal-fx"

    def setUp(self):
        super().setUp()
        self.universe = json.loads((self.set / "universe.json").read_text())
        self.commits = {lib["id"]: lib["commit"] for lib in self.universe["libraries"]}

    # ---- the side files -------------------------------------------------------------------

    def write(self, name: str, doc: dict) -> Path:
        path = self.tmp / name
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return path

    def supplements(self, **paths) -> build_store.Supplements:
        return build_store.Supplements.read(**paths)

    @staticmethod
    def text_entry(pid: str, pageid: int, layer: str, segments: list | None = None) -> tuple[str, dict]:
        span_sha = hashlib.sha256(layer.encode("utf-8")).hexdigest()
        return build_store.passage_key(pageid, span_sha), {
            "id": pid, "pageid": pageid, "span_sha256": span_sha, "layer": layer,
            "segments": segments if segments is not None else [{"text": layer}]}

    def passage_text_file(self, *entries: tuple[str, dict], schema: str = build_store.PASSAGE_TEXT_SCHEMA) -> Path:
        return self.write("passage-text.json", {"schema": schema, "passages": dict(entries)})

    def with_formula_passage(self) -> None:
        """Give the conjecture's records the span_sha256 of a layer slice holding a formula."""
        sha = hashlib.sha256(self.CONJECTURE_LAYER.encode("utf-8")).hexdigest()
        self.edit_widget(lambda rows: [self.subject_ext(row).update(span_sha256=sha)
                                       for row in rows if row["id"] == self.CONJECTURE_ID])

    @staticmethod
    def subject_ext(row: dict) -> dict:
        return row["subject"][0]["extensions"][LOCATE]

    def signature_file(self, table: dict, universe_sha: str | None = None) -> Path:
        return self.write("signatures.json", {
            "schema": build_store.SIGNATURES_SCHEMA,
            "source": {"universe": self.universe["id"], "universe_sha256": universe_sha or self.universe["universe_sha256"],
                       "corpus_sha256": "c" * 64, "corpus_built": "2026-10-18T06:14:30Z"},
            "signatures": table})

    def signature_table(self) -> dict:
        mathlib = self.commits["mathlib"]
        return {"mathlib": {mathlib: {
            "Widget.exists_spanning_gadget": {
                "signature": "theorem Widget.exists_spanning_gadget {W : Widget} :\n    W.Finite → ∃ g, W.Spans g",
                "elided": False, "kind": "theorem", "proof_status": "proved",
                "type": "∀ {W : Widget}, W.Finite → ∃ g, W.Spans g"},
            "Gadget.equivVertex": {
                "signature": "def Gadget.equivVertex {W : Widget} […] : W.Gadget ≃ W.Vertex",
                "elided": True, "kind": "def", "proof_status": "definition", "deprecated_to": "Widget.gadgetEquiv",
                "type": "{W : Widget} → [inst : W.Finite] → W.Gadget ≃ W.Vertex"},
            "Not.On.Any.Card": {"signature": "theorem Not.On.Any.Card : True", "elided": False, "kind": "theorem",
                                "proof_status": "proved", "type": "True"}}}}

    # The fixture's runs harvested before this, and its seal was exported then (provenance_doc).
    EXPORTED = "2026-11-01T14:25:17Z"

    def run_doc(self, run_id: str, mode: str, start: str, end: str, model: str = "model-x") -> dict:
        seal = json.loads((self.set / "SEAL.json").read_text())
        run = {"run_id": run_id, "mode": mode, "operator": "alice", "model": model, "model_name": "Model X",
               "roles": {"worker": {"model": model, "model_name": "Model X"},
                         "reviewer": {"model": "model-x", "model_name": "Model X"}},
               "model_evidence": {"observed": {"messages_by_model": {model: 12}}},
               "window_utc": {"start": start, "end": end}, "times": {"harvested_at": end},
               "skill": {"name": "formal-companion", "commit": seal["skill_commit"]},
               "universe": {"id": self.universe["id"], "sha256": self.universe["universe_sha256"]}}
        if mode == "locate-challenge":
            run.update(challenged_runs=["loc-fx"], sampling={"seed": 0, "sample_rate": 0.2, "strong_sample_rate": 0.5,
                                                              "absence_sample_rate": 0.25})
        return run

    def provenance_doc(self, inventory: bool = False) -> dict:
        """The runs, the set and one entry per record line, written from the records here (not the builder).
        With `inventory`, the locate run names the inventory run inv-fx, which the side file describes."""
        self.edit_seal(lambda seal: seal.update(campaign=self.CAMPAIGN, created=self.EXPORTED))
        records = {}
        for path in sorted((self.set / "records").glob("*.jsonl")):
            rows = []
            for line, row in enumerate(read_jsonl(path)):
                ext = row["extensions"][LOCATE]
                relation, verdict = row.get("relation"), ext["challenge"]
                checked = verdict in ("survived", "downgraded")
                entry = {"line": line, "locate_run": "loc-fx",
                         "shape": "unresolved" if relation is None else "absence" if relation == "noMatch" else "location",
                         "challenge": {"verdict": verdict, "rechecked": checked, "run": "chal-fx" if checked else None}}
                if verdict == "sampled_out":
                    entry["challenge"]["sampled_out_of"] = "chal-fx"
                if "location_key" in ext:
                    entry["location_key"] = ext["location_key"]
                rows.append(entry)
            records[path.stem.split("-")[1]] = rows
        seal_sha = hashlib.sha256((self.set / "SEAL.json").read_bytes()).hexdigest()
        runs = {"loc-fx": self.run_doc("loc-fx", "locate", "2026-10-30T23:50:00Z", "2026-10-31T04:48:00Z"),
                "chal-fx": self.run_doc("chal-fx", "locate-challenge", "2026-11-01T03:19:32Z", self.EXPORTED)}
        if inventory:
            runs["loc-fx"]["inventory_run"] = "inv-fx"
            runs["inv-fx"] = self.run_doc("inv-fx", "locate-inventory", "2026-10-30T06:52:00Z", "2026-10-30T20:00:47Z")
        return {"schema": build_store.PROVENANCE_SCHEMA, "runs": runs,
                "record_sets": {"fixture-set": {"seal_sha256": seal_sha, "campaign": self.CAMPAIGN,
                                                "locate_run": "loc-fx", "challenge_run": "chal-fx"}},
                "records": records}

    def refuse_with(self, fragment: str, **paths) -> None:
        with self.assertRaises(build_store.AtlasError) as caught:
            build_store.build(self.set, supplements=self.supplements(**paths))
        self.assertIn(fragment, str(caught.exception))

    @staticmethod
    def without(value, keys=("text", "signature", "provenance", "supplements")):
        """A view or current.json without what the side files add, to compare with the plain build
        (the article's own `licence`, whose `text` is the source's licence, is kept as it is)."""
        if isinstance(value, dict):
            return {k: v if k == "licence" else SupplementTests.without(v, keys) for k, v in value.items()
                    if k not in keys}
        if isinstance(value, list):
            return [SupplementTests.without(v, keys) for v in value]
        return value

    # ---- absent ---------------------------------------------------------------------------

    def test_without_side_files_the_store_is_byte_for_byte_the_plain_build(self):
        self.assertIsNone(build_store.Supplements.read())
        tree = build_store.build(RECORDSET, supplements=None)
        self.assertEqual(tree, build_store.build(RECORDSET))
        for path, data in tree.items():
            self.assertEqual(data, (STORE / path).read_bytes(), path)
            for key in (b'"text":{', b'"signature":', b'"provenance":', b'"supplements":'):
                self.assertNotIn(key, data, path)

    # ---- passage text ---------------------------------------------------------------------

    def test_passage_text_is_merged_where_it_checks_out_and_the_rest_keep_the_quote(self):
        self.with_formula_passage()
        plain = build_store.build(self.set)
        bound = self.text_entry(self.BOUND_ID, 1001, self.BOUND_TEXT)
        formula = self.text_entry(self.CONJECTURE_ID, 1001, self.CONJECTURE_LAYER, self.CONJECTURE_SEGMENTS)
        stray = self.text_entry("enwiki-9-9::In-text #00000000", 9, "a passage of no page here")
        path = self.passage_text_file(bound, formula, stray)
        tree = build_store.build(self.set, supplements=self.supplements(passage_text=path))
        self.assertEqual(sorted(tree), sorted(plain))
        article = json.loads(tree["atlas/v1/articles/1001.json"])
        texts = {p["id"]: p.get("text") for p in article["passages"]}
        self.assertEqual(texts[self.BOUND_ID], {"layer": self.BOUND_TEXT, "segments": [{"text": self.BOUND_TEXT}]})
        self.assertEqual(texts[self.CONJECTURE_ID], {"layer": self.CONJECTURE_LAYER, "segments": self.CONJECTURE_SEGMENTS})
        self.assertEqual([pid for pid, text in texts.items() if text is None],
                         ["enwiki-1001-2002::In-text #3fa91c07", "enwiki-1001-2002::In-text #91aa02de"])
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        identity = {"passage_text": {"schema": build_store.PASSAGE_TEXT_SCHEMA, "sha256": sha}}
        for pageid in (1001, 1003):
            view = json.loads(tree[f"atlas/v1/articles/{pageid}.json"])
            self.assertEqual(view["supplements"], identity)
            self.assertEqual(self.without(view), json.loads(plain[f"atlas/v1/articles/{pageid}.json"]))
        current = json.loads(tree["atlas/v1/current.json"])
        # The conjecture has no status, so of the two passages given their text one is shown: the
        # dashboard counts the text it shows from `shown`.
        self.assertEqual(current["supplements"], {"passage_text": {**identity["passage_text"], "passages": 2, "shown": 1}})
        self.assertNotIn("provenance", current)
        self.assertEqual(self.without(current), json.loads(plain["atlas/v1/current.json"]))
        # Records, universes and the index never change.
        for name in tree:
            if "/articles/" not in name and not name.endswith(("current.json", "manifest.json")):
                self.assertEqual(tree[name], plain[name], name)

    def test_passage_text_that_does_not_check_out_is_refused(self):
        self.with_formula_passage()
        key, good = self.text_entry(self.CONJECTURE_ID, 1001, self.CONJECTURE_LAYER, self.CONJECTURE_SEGMENTS)
        segment_rule = f"--passage-text: passages[{key}]: segment"
        cases = [
            ({"layer": self.CONJECTURE_LAYER.replace("regular", "irregular")}, "layer must be the passage's text"),
            ({"id": self.BOUND_ID}, f"id must be the passage's record id {self.CONJECTURE_ID!r}"),
            ({"span_sha256": "0" * 64}, "span_sha256 must be the record's span_sha256"),
            ({"pageid": 1003}, "pageid must be the record's"),
            # A word the slice does not say, a formula whose TeX is not its annotation's, a
            # segment of two shapes, and segments that stop short or run on.
            ({"segments": [{"text": "every balanced widget is the image of an ordinary one,"}, *self.CONJECTURE_SEGMENTS[1:]]},
             f"{segment_rule} 0: segments spell the passage's layer slice in order"),
            ({"segments": [*self.CONJECTURE_SEGMENTS[:3], {"tex": "y^{3}", "display": True}, {"text": " ."}]},
             f"{segment_rule} 3: segments spell"),
            ({"segments": [{"text": "every", "tex": "x"}]}, f"{segment_rule} 0: segments spell"),
            ({"segments": self.CONJECTURE_SEGMENTS[:-1]}, "the segments stop before the layer slice does"),
            ({"segments": [*self.CONJECTURE_SEGMENTS, {"text": "!"}]}, f"{segment_rule} 5: segments spell"),
            ({"segments": [*self.CONJECTURE_SEGMENTS[:3], {"tex": "x^{2}", "display": False}, {"text": " ."}]},
             f"{segment_rule} 3: display, when present, is true"),
            ({"segments": []}, "segments must be a list of 1 to"),
            ({"segments": "every balanced widget"}, "segments must be a list of 1 to"),
        ]
        for change, fragment in cases:
            with self.subTest(change=change):
                path = self.passage_text_file((key, {**good, **change}))
                self.refuse_with(fragment, passage_text=path)
        self.refuse_with("--passage-text: matches no passage of this build",
                         passage_text=self.passage_text_file(self.text_entry("enwiki-9-9::In-text #00", 9, "elsewhere")))
        with self.assertRaises(build_store.AtlasError) as caught:
            self.supplements(passage_text=self.passage_text_file((key, good), schema="wikilean.atlas.passage-text/0"))
        self.assertIn("schema must be 'wikilean.atlas.passage-text/1'", str(caught.exception))
        with self.assertRaises(build_store.AtlasError) as caught:
            self.supplements(passage_text=self.tmp / "missing.json")
        self.assertIn("cannot be read", str(caught.exception))

    def test_passage_context_is_merged_and_checked_as_far_as_the_record_allows(self):
        # The sentence around a passage: its own text, spelled by segments, flagged where it
        # was trimmed or leads into the passage from the block before.
        key, bound = self.text_entry(self.BOUND_ID, 1001, self.BOUND_TEXT)
        before = {"layer": "Lemma 2 is used as follows: ", "segments": [{"text": "Lemma 2 is used as follows: "}],
                  "lead_in": True}
        after = {"layer": " so the bound is sharp", "segments": [{"text": " so the bound is sharp"}], "trimmed": True}
        path = self.passage_text_file((key, {**bound, "before": before, "after": after}))
        tree = build_store.build(self.set, supplements=self.supplements(passage_text=path))
        text = {p["id"]: p.get("text") for p in json.loads(tree["atlas/v1/articles/1001.json"])["passages"]}[self.BOUND_ID]
        self.assertEqual(text, {"layer": self.BOUND_TEXT, "segments": [{"text": self.BOUND_TEXT}],
                                "before": before, "after": after})
        where = f"--passage-text: passages[{key}]: "
        cases = [({"before": {**before, "page": 3}}, "before: is an object of"),
                 ({"after": {**after, "lead_in": True}}, "after: is an object of"),
                 ({"after": {**after, "trimmed": False}}, "after: trimmed, when present, is true"),
                 ({"before": {**before, "segments": [{"text": "Lemma 3 is used as follows: "}]}},
                  "before: segment 0: segments spell"),
                 ({"before": {"layer": "x" * 2001, "segments": [{"text": "x" * 2001}]}}, "before: layer is the layer text")]
        for change, fragment in cases:
            with self.subTest(change=change):
                self.refuse_with(where + fragment, passage_text=self.passage_text_file((key, {**bound, **change})))
        # Where the record quotes the text before the passage, the context must end with it.
        self.edit_widget(lambda rows: [row["subject"][0]["location"]["quote"].update(prefix="is used as follows: ")
                                       for row in rows if row["id"] == self.BOUND_ID])
        path = self.passage_text_file((key, {**bound, "before": before, "after": after}))
        build_store.build(self.set, supplements=self.supplements(passage_text=path))
        self.refuse_with(where + "before: the record's quote prefix must be where this context meets the passage",
                         passage_text=self.passage_text_file((key, {**bound, "before": {
                             "layer": "Lemma 2 is used thus: ", "segments": [{"text": "Lemma 2 is used thus: "}]}})))

    # ---- signatures -----------------------------------------------------------------------

    def test_signatures_are_merged_by_library_commit_and_declaration(self):
        path = self.signature_file(self.signature_table())
        tree = build_store.build(self.set, supplements=self.supplements(signatures=path))
        found = {}
        for pageid in (1001, 1003):
            for passage in json.loads(tree[f"atlas/v1/articles/{pageid}.json"])["passages"]:
                for loc in passage["locations"]:
                    found[loc["declaration"]] = loc.get("signature")
        table = self.signature_table()["mathlib"][self.commits["mathlib"]]
        self.assertEqual(found.pop("Widget.exists_spanning_gadget"),
                         {"text": table["Widget.exists_spanning_gadget"]["signature"], "elided": False})
        # A shortened header keeps the whole type; a deprecated name says what replaced it.
        self.assertEqual(found.pop("Gadget.equivVertex"),
                         {"text": table["Gadget.equivVertex"]["signature"], "elided": True,
                          "type": table["Gadget.equivVertex"]["type"], "deprecated_to": "Widget.gadgetEquiv"})
        self.assertEqual(set(found.values()), {None})
        current = json.loads(tree["atlas/v1/current.json"])
        self.assertEqual(current["supplements"]["signatures"], {
            "schema": build_store.SIGNATURES_SCHEMA, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "corpus_sha256": "c" * 64, "corpus_built": "2026-10-18T06:14:30Z", "universe": "u-fixture",
            "locations": 2, "declarations": 2})

    def test_signatures_that_do_not_match_the_card_are_refused(self):
        mathlib = self.commits["mathlib"]
        where = f"--signatures: signatures[mathlib][{mathlib}][Widget.exists_spanning_gadget]: "

        def table(**change) -> dict:
            value = self.signature_table()
            value["mathlib"][mathlib]["Widget.exists_spanning_gadget"].update(change)
            return value
        cases = [({"proof_status": "statement_only"}, "proof_status 'statement_only' is not the card's 'proved'"),
                 ({"signature": "theorem Widget.exists_spanning_gadget' : True"},
                  "signature must start with its keyword and the card's declaration"),
                 ({"kind": "lemma"}, "kind must be one of"),
                 ({"elided": "no"}, "elided must be true or false"),
                 ({"elided": True, "type": ""}, "an elided signature keeps the whole type"),
                 ({"deprecated_to": "two words"}, "deprecated_to, when present, is a declaration name"),
                 ({"signature": "theorem Widget.exists_spanning_gadget " + "x" * 4000}, "signature must be a string")]
        for change, fragment in cases:
            with self.subTest(change=change):
                self.refuse_with(where + fragment, signatures=self.signature_file(table(**change)))
        self.refuse_with("--signatures: source.universe_sha256 must be the store's universe",
                         signatures=self.signature_file(self.signature_table(), universe_sha="0" * 64))
        self.refuse_with("--signatures: matches no location of this build",
                         signatures=self.signature_file({"mathlib": {"0" * 40: {}}}))

    # ---- provenance -----------------------------------------------------------------------

    def test_provenance_names_each_runs_models_operator_and_dates(self):
        path = self.write("provenance.json", self.provenance_doc())
        tree = build_store.build(self.set, supplements=self.supplements(provenance=path))
        seal = json.loads((self.set / "SEAL.json").read_text())
        locate = {"run_id": "loc-fx", "mode": "locate", "model": "model-x", "model_name": "Model X",
                  "reviewer_model": "model-x", "reviewer_model_name": "Model X", "operator": "alice",
                  "started": "2026-10-30T23:50:00Z", "harvested": "2026-10-31T04:48:00Z",
                  "skill_commit": seal["skill_commit"]}
        challenge = {**locate, "run_id": "chal-fx", "mode": "locate-challenge", "started": "2026-11-01T03:19:32Z",
                     "harvested": "2026-11-01T14:25:17Z",
                     "sampling": {"seed": 0, "sample_rate": 0.2, "strong_sample_rate": 0.5, "absence_sample_rate": 0.25}}
        for pageid in (1001, 1003):
            view = json.loads(tree[f"atlas/v1/articles/{pageid}.json"])
            self.assertEqual(view["provenance"], {"locate": locate, "challenge": challenge})
        current = json.loads(tree["atlas/v1/current.json"])
        # current.json's challenge run adds what it re-checked across the build, counted from
        # the articles' own verdicts, central and supporting passages apart.
        rechecked = {"central": {"locations": [0, 0], "absences": [0, 0]},
                     "supporting": {"locations": [0, 0], "absences": [0, 0]}}
        for pageid in (1001, 1003):
            for passage in json.loads(tree[f"atlas/v1/articles/{pageid}.json"])["passages"]:
                group = rechecked["central" if passage.get("prominence") in ("subject", "central") else "supporting"]
                for loc in passage["locations"]:
                    if loc["relation"] != "relatedMatch":
                        group["locations"][1] += 1
                        group["locations"][0] += loc["challenge"] in ("survived", "downgraded")
                if passage.get("absence"):
                    group["absences"][1] += 1
                    group["absences"][0] += passage["absence"]["challenge"] in ("survived", "downgraded")
        self.assertGreater(sum(n for g in rechecked.values() for pair in g.values() for n in pair), 0)
        self.assertEqual(current["provenance"], {"runs": [{**challenge, "rechecked": rechecked}, locate]})
        self.assertEqual(current["supplements"], {"provenance": {
            "schema": build_store.PROVENANCE_SCHEMA, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}})

    def test_provenance_that_does_not_describe_the_records_is_refused(self):
        def refuse(edit, fragment):
            self.fresh()
            doc = self.provenance_doc()
            edit(doc)
            self.refuse_with(fragment, provenance=self.write("provenance.json", doc))

        refuse(lambda d: d["record_sets"]["fixture-set"].update(seal_sha256="0" * 64),
               "--provenance: record_sets[fixture-set]: seal_sha256 and campaign must be the record set's own")
        refuse(lambda d: d["record_sets"]["fixture-set"].update(locate_run="loc-other"),
               "locate_run and challenge_run must be the runs the campaign token names")
        refuse(lambda d: d["record_sets"].clear(), "--provenance: record_sets[fixture-set]: is missing")
        refuse(lambda d: d["records"]["1001"][1]["challenge"].update(verdict="survived"),
               "--provenance: records[1001]: line 1 does not describe the record on that line")
        refuse(lambda d: d["records"]["1003"][0].update(location_key="0" * 64),
               "--provenance: records[1003]: line 0 does not describe the record on that line")
        refuse(lambda d: d["records"]["1003"].pop(), "--provenance: records[1003]: lists 3 records, not the 4 lines of the records file")
        # A model the run's own calls do not bear out is never cited.
        refuse(lambda d: d["runs"]["loc-fx"]["model_evidence"]["observed"]["messages_by_model"].update({"model-y": 1}),
               "--provenance: runs[loc-fx]: model_evidence.observed.messages_by_model must name only the models")
        refuse(lambda d: d["runs"]["loc-fx"]["roles"]["worker"].update(model="model-y"),
               "--provenance: runs[loc-fx]: roles.worker must be the run's model and model_name")
        refuse(lambda d: d["runs"]["chal-fx"].update(mode="locate"),
               "--provenance: runs[chal-fx]: mode must be 'locate-challenge'")
        refuse(lambda d: d["runs"]["loc-fx"]["times"].update(harvested_at="2026-10-31T05:00:00Z"),
               "--provenance: runs[loc-fx]: window_utc is {start, end}")
        refuse(lambda d: d["runs"]["loc-fx"].update(operator="two words"),
               "--provenance: runs[loc-fx]: operator must be the login that ran it")
        refuse(lambda d: d["runs"]["loc-fx"]["skill"].update(commit="0" * 40),
               "--provenance: runs[loc-fx]: skill.commit must be the seal's skill_commit")
        refuse(lambda d: d["runs"]["chal-fx"].update(challenged_runs=["loc-other"]),
               "--provenance: runs[chal-fx]: challenged_runs must name loc-fx")
        refuse(lambda d: d["runs"]["chal-fx"]["sampling"].update(sample_rate=2),
               "--provenance: runs[chal-fx]: sampling is")
        refuse(lambda d: d["runs"].pop("chal-fx"), "--provenance: runs[chal-fx]: is missing")

    def test_the_runs_windows_are_checked_against_the_seal_and_one_another(self):
        # A run's harvest is no later than the export, the challenge starts after the locate run it
        # re-checked was harvested, and times compare as times (a fraction of a second counts).
        def refuse(edit, fragment):
            self.fresh()
            doc = self.provenance_doc()
            edit(doc)
            self.refuse_with(fragment, provenance=self.write("provenance.json", doc))

        def window(run: str, start: str | None = None, end: str | None = None):
            def edit(d):
                w = d["runs"][run]["window_utc"]
                w.update({k: v for k, v in (("start", start), ("end", end)) if v is not None})
                d["runs"][run]["times"]["harvested_at"] = w["end"]
            return edit

        refuse(window("chal-fx", end="2026-11-01T14:25:18Z"),
               "--provenance: runs[chal-fx]: window_utc.end, the harvest, must be no later than the seal's created")
        refuse(window("loc-fx", start="2026-11-01T14:30:00Z", end="2026-11-01T15:00:00Z"),
               "--provenance: runs[loc-fx]: window_utc.end, the harvest, must be no later than the seal's created")
        refuse(window("chal-fx", start="2026-10-31T04:47:59Z"),
               "--provenance: runs[chal-fx]: window_utc.start must be no earlier than the harvest of loc-fx, the locate "
               "run it re-checked")
        refuse(window("loc-fx", start="2026-10-31T04:48:00.5Z"),
               "--provenance: runs[loc-fx]: window_utc is {start, end}: ISO 8601 UTC times, start first")
        refuse(window("loc-fx", start="2026-13-30T23:50:00Z"), "--provenance: runs[loc-fx]: window_utc is {start, end}")
        # The same instant, once with a fraction of a second, is still in order.
        self.fresh()
        doc = self.provenance_doc()
        window("chal-fx", start="2026-10-31T04:48:00.000Z")(doc)
        build_store.build(self.set, supplements=self.supplements(provenance=self.write("provenance.json", doc)))

    def test_the_inventory_run_that_chose_the_passages_is_cited_and_checked(self):
        path = self.write("provenance.json", self.provenance_doc(inventory=True))
        tree = build_store.build(self.set, supplements=self.supplements(provenance=path))
        seal = json.loads((self.set / "SEAL.json").read_text())
        inventory = {"run_id": "inv-fx", "mode": "locate-inventory", "model": "model-x", "model_name": "Model X",
                     "reviewer_model": "model-x", "reviewer_model_name": "Model X", "operator": "alice",
                     "started": "2026-10-30T06:52:00Z", "harvested": "2026-10-30T20:00:47Z",
                     "skill_commit": seal["skill_commit"]}
        for pageid in (1001, 1003):
            view = json.loads(tree[f"atlas/v1/articles/{pageid}.json"])
            self.assertEqual(view["provenance"]["inventory"], inventory)
        runs = json.loads(tree["atlas/v1/current.json"])["provenance"]["runs"]
        self.assertEqual([run["run_id"] for run in runs], ["chal-fx", "inv-fx", "loc-fx"])
        self.assertEqual(runs[1], inventory)

        def refuse(edit, fragment):
            self.fresh()
            doc = self.provenance_doc(inventory=True)
            edit(doc)
            self.refuse_with(fragment, provenance=self.write("provenance.json", doc))

        refuse(lambda d: d["runs"].pop("inv-fx"), "--provenance: runs[inv-fx]: is missing")
        refuse(lambda d: d["runs"]["inv-fx"].update(mode="locate"),
               "--provenance: runs[inv-fx]: mode must be 'locate-inventory': the locate run names it as its inventory run")
        refuse(lambda d: d["runs"]["loc-fx"].update(inventory_run="inv fx"),
               "--provenance: runs[loc-fx]: inventory_run names the inventory run whose passages it searched, or is null")
        refuse(lambda d: d["runs"]["inv-fx"]["model_evidence"]["observed"]["messages_by_model"].update({"model-y": 1}),
               "--provenance: runs[inv-fx]: model_evidence.observed.messages_by_model must name only the models")
        refuse(lambda d: d["runs"]["inv-fx"].update(window_utc={"start": "2026-10-30T06:52:00Z",
                                                                "end": "2026-10-30T23:50:01Z"},
                                                    times={"harvested_at": "2026-10-30T23:50:01Z"}),
               "--provenance: runs[inv-fx]: window_utc.end must be no later than the start of loc-fx, the locate run "
               "that searched its passages")
        # Without an inventory_run, the views name none, as before.
        self.fresh()
        tree = build_store.build(self.set, supplements=self.supplements(
            provenance=self.write("provenance.json", self.provenance_doc())))
        self.assertNotIn("inventory", json.loads(tree["atlas/v1/articles/1001.json"])["provenance"])

    # ---- the audit summary ----------------------------------------------------------------

    # A made-up audit's two figures: a share weighted by relation, and an unweighted one.
    FIGURES = [{"id": "strong", "label": "Strong supporting cards judged right", "share": 0.8,
                "ci95": [0.62, 0.91], "weights": "relation and library", "n_eff": 12.5, "drawn": 16, "population": 400},
               {"id": "absences", "label": "Absences judged right", "share": 0.75, "ci95": [0.301, 0.954],
                "weights": None, "n_eff": 4, "drawn": 4, "population": 300}]

    def audit_doc(self) -> dict:
        """A made-up audit of the fixture's set: its run, models, sample size and two figures."""
        seal_sha = hashlib.sha256((self.set / "SEAL.json").read_bytes()).hexdigest()
        return {"schema": build_store.AUDIT_SUMMARY_SCHEMA, "record_sets": [seal_sha],
                "audit": {"run_id": "audit-fx", "operator": "alice",
                          "window_utc": {"start": "2026-11-03T02:44:00Z", "end": "2026-11-03T05:07:00Z"},
                          "models": [{"model": "model-z", "model_name": "Model Z"}], "blind": True, "independent": False},
                "items": 20, "figures": json.loads(json.dumps(self.FIGURES))}

    def test_an_audit_summary_is_cited_by_every_article_and_by_current_json(self):
        plain = build_store.build(self.set)
        path = self.write("audit.json", self.audit_doc())
        tree = build_store.build(self.set, supplements=self.supplements(audit_summary=path))
        audit = {"run_id": "audit-fx", "operator": "alice", "started": "2026-11-03T02:44:00Z",
                 "ended": "2026-11-03T05:07:00Z", "models": [{"model": "model-z", "model_name": "Model Z"}],
                 "blind": True, "independent": False, "items": 20, "figures": self.FIGURES}
        identity = {"schema": build_store.AUDIT_SUMMARY_SCHEMA, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        current = json.loads(tree["atlas/v1/current.json"])
        self.assertEqual((current["audit"], current["supplements"]), (audit, {"audit_summary": identity}))
        self.assertEqual(self.without(current, ("audit", "supplements")), json.loads(plain["atlas/v1/current.json"]))
        for pageid in (1001, 1003):
            view = json.loads(tree[f"atlas/v1/articles/{pageid}.json"])
            self.assertEqual((view["audit"], view["supplements"]), (audit, {"audit_summary": identity}))
            self.assertEqual(self.without(view, ("audit", "supplements")),
                             json.loads(plain[f"atlas/v1/articles/{pageid}.json"]))
        # Records, index and universe are the plain build's.
        changed = sorted(p for p in tree if tree[p] != plain[p])
        self.assertEqual(changed, ["atlas/v1/articles/1001.json", "atlas/v1/articles/1003.json",
                                   "atlas/v1/current.json", "atlas/v1/manifest.json"])

    def test_an_audit_summary_that_does_not_describe_the_build_is_refused(self):
        def refuse(edit, fragment):
            doc = self.audit_doc()
            edit(doc)
            self.refuse_with(fragment, audit_summary=self.write("audit.json", doc))

        refuse(lambda d: d.update(record_sets=["0" * 64]),
               "--audit-summary: record_sets must list the seal sha256 of each record set of this build, each once")
        refuse(lambda d: d["record_sets"].append(d["record_sets"][0]), "record_sets must list the seal sha256")
        refuse(lambda d: d.update(record_sets=[]), "record_sets must list the seal sha256")
        refuse(lambda d: d.update(note="the audit was fine"), "--audit-summary: unknown key 'note'")
        refuse(lambda d: d["audit"].update(run_id="audit fx"), "audit.run_id must be a run id")
        refuse(lambda d: d["audit"].update(operator="two words"), "audit.operator must be the login that ran it")
        refuse(lambda d: d["audit"].pop("operator"), "audit is the run that judged the sample")
        refuse(lambda d: d["audit"]["window_utc"].update(end="2026-11-02T00:00:00Z"), "audit.window_utc is {start, end}")
        refuse(lambda d: d["audit"].update(models=[]), "audit.models lists 1 to 4 models")
        refuse(lambda d: d["audit"]["models"].append({"model": "model-z", "model_name": "Model Z"}),
               "audit.models lists 1 to 4 models")
        refuse(lambda d: d["audit"]["models"][0].update(model_name="<b>Model</b>"), "audit.models lists 1 to 4 models")
        refuse(lambda d: d.update(items=0), "items must be the number of items the audit judged")
        refuse(lambda d: d.update(items=True), "items must be the number of items the audit judged")
        refuse(lambda d: d.update(figures=[]), "figures must list 1 to 12 figures")
        # A share and its interval: numbers from 0 to 1, the share inside the interval.
        for change in ({"share": 1.2}, {"share": True}, {"share": "0.9"}, {"ci95": [0.9, 0.95]}, {"ci95": [0.5]},
                       {"ci95": [0.5, 1.5]}, {"ci95": "0.5-0.9"}, {"ci95": [None, 0.9]}):
            with self.subTest(change=str(change)):
                refuse(lambda d: d["figures"][0].update(change), "figure 0: share is the share judged right")
        # What a share rests on: drawn items of the sample (at most items), of a population at least as large.
        for change in ({"drawn": 21}, {"drawn": 0}, {"drawn": 2.5}, {"population": 15}, {"population": None}):
            with self.subTest(change=str(change)):
                refuse(lambda d: d["figures"][0].update(change), "figure 0: drawn is how many of the sample's items")
        # Weights leave an effective sample above 0 and at most drawn; without them it is drawn itself.
        for figure, change in ((0, {"n_eff": 17}), (0, {"n_eff": 0}), (0, {"weights": "<b>x</b>"}), (0, {"weights": 3}),
                               (1, {"n_eff": 3.5}), (0, {"n_eff": True})):
            with self.subTest(change=str(change)):
                refuse(lambda d: d["figures"][figure].update(change), f"figure {figure}: weights says what the items")
        refuse(lambda d: d["figures"][0].update(label="<script>"), "figure 0: label must be plain text")
        refuse(lambda d: d["figures"][1].update(id="strong"), "each figure's id is its own")
        refuse(lambda d: d["figures"][0].update(weight=2), "figure 0 is exactly ['id', 'label', 'share', 'ci95'")
        refuse(lambda d: d["figures"][0].pop("population"), "figure 0 is exactly")
        # Whether it was blind and independent, said as true or false.
        refuse(lambda d: d["audit"].update(blind="yes"), "audit.blind and audit.independent are true or false")
        refuse(lambda d: d["audit"].pop("independent"), "audit is the run that judged the sample")
        # An audit judges the sets as they were exported, so it starts no earlier than the latest seal's
        # created; its times compare as times.
        refuse(lambda d: d["audit"].update(window_utc={"start": "2020-01-01T00:00:00Z", "end": "2020-01-02T00:00:00Z"}),
               "audit.window_utc.start must be no earlier than the created time of the latest seal it read")
        refuse(lambda d: d["audit"].update(window_utc={"start": "2026-11-03T02:00:00.5Z", "end": "2026-11-03T02:00:00Z"}),
               "audit.window_utc is {start, end}: ISO 8601 UTC times, start first")
        doc = self.audit_doc()
        doc["schema"] = "wikilean.atlas.audit-summary/0"
        with self.assertRaises(build_store.AtlasError) as caught:
            self.supplements(audit_summary=self.write("audit.json", doc))
        self.assertIn(f"schema must be {build_store.AUDIT_SUMMARY_SCHEMA!r}", str(caught.exception))

    def test_the_cli_merges_an_audit_summary_and_check_compares_with_it(self):
        path = self.write("audit.json", self.audit_doc())
        out = self.tmp / "store"

        def run_main(*args: str) -> tuple[int, str]:
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                code = build_store.main(["--record-set", str(self.set), "--out", str(out), *args])
            return code, err.getvalue()
        self.assertEqual(run_main("--audit-summary", str(path)), (0, ""))
        self.assertEqual(run_main("--audit-summary", str(path), "--check"), (0, ""))
        self.assertEqual(json.loads((out / "atlas/v1/current.json").read_text())["audit"]["run_id"], "audit-fx")
        code, err = run_main("--check")
        self.assertEqual(code, 1)
        self.assertIn("differs: atlas/v1/current.json", err)

    def test_no_malformed_side_file_value_crashes_the_builder(self):
        # Every value a side file may carry wrongly is a refusal (or, where it only narrows what
        # is merged, a build), never another exception: as MalformedValueTests for the record set.
        self.with_formula_passage()
        key, entry = self.text_entry(self.CONJECTURE_ID, 1001, self.CONJECTURE_LAYER, self.CONJECTURE_SEGMENTS)
        mathlib = self.commits["mathlib"]
        decl = ("signatures", "mathlib", mathlib, "Widget.exists_spanning_gadget")
        docs = {
            "passage_text": ({"schema": build_store.PASSAGE_TEXT_SCHEMA, "passages": {key: entry}},
                             [("passages",), ("passages", key), *[("passages", key, k) for k in entry],
                              ("passages", key, "segments", 0), ("passages", key, "segments", 3, "tex"),
                              ("passages", key, "segments", 3, "display")]),
            "signatures": (json.loads(self.signature_file(self.signature_table()).read_text()),
                           [("source",), ("source", "universe_sha256"), ("source", "corpus_built"), ("signatures",),
                            decl[:2], decl[:3], decl, *[(*decl, k) for k in ("signature", "kind", "elided",
                                                                          "proof_status", "type")],
                            (*decl[:3], "Gadget.equivVertex", "deprecated_to")]),
            "provenance": (self.provenance_doc(inventory=True),
                           [("runs",), ("runs", "loc-fx"), ("runs", "chal-fx", "sampling"),
                            ("runs", "loc-fx", "inventory_run"), ("runs", "inv-fx"), ("runs", "inv-fx", "window_utc"),
                            ("runs", "chal-fx", "challenged_runs"),
                            *[("runs", "loc-fx", k) for k in ("run_id", "mode", "model", "model_name", "roles",
                                                              "model_evidence", "operator", "window_utc", "times",
                                                              "skill", "universe")],
                            ("runs", "loc-fx", "roles", "reviewer"), ("record_sets",), ("record_sets", "fixture-set"),
                            *[("record_sets", "fixture-set", k) for k in ("seal_sha256", "campaign", "locate_run",
                                                                          "challenge_run")],
                            ("records",), ("records", "1001"), ("records", "1001", 0),
                            ("records", "1001", 1, "challenge"), ("records", "1001", 0, "line")]),
            "audit_summary": (self.audit_doc(),
                              [("record_sets",), ("record_sets", 0), ("audit",), ("items",), ("figures",),
                               *[("audit", k) for k in ("run_id", "operator", "window_utc", "models", "blind",
                                                        "independent")],
                               ("audit", "window_utc", "start"), ("audit", "models", 0),
                               ("audit", "models", 0, "model"), ("audit", "models", 0, "model_name"),
                               ("figures", 0), *[("figures", 1, k) for k in build_store.AUDIT_FIGURE_KEYS],
                               ("figures", 0, "ci95", 0)]),
        }
        for kind, (doc, paths) in docs.items():
            for path in paths:
                for value in MalformedValueTests.HOSTILE:
                    with self.subTest(kind=kind, path=path, value=value):
                        broken = json.loads(json.dumps(doc))
                        target = broken
                        for step in path[:-1]:
                            target = target[step]
                        target[path[-1]] = value
                        side = self.write(f"{kind}.json", broken)
                        try:
                            build_store.build(self.set, supplements=self.supplements(**{kind: side}))
                        except build_store.AtlasError:
                            pass

    # ---- all three, through the CLI -------------------------------------------------------

    def test_the_cli_merges_the_side_files_and_check_compares_with_them(self):
        self.with_formula_passage()
        paths = {
            "--passage-text": self.passage_text_file(
                self.text_entry(self.CONJECTURE_ID, 1001, self.CONJECTURE_LAYER, self.CONJECTURE_SEGMENTS)),
            "--signatures": self.signature_file(self.signature_table()),
            "--provenance": self.write("provenance.json", self.provenance_doc()),
        }
        side = [arg for flag, path in paths.items() for arg in (flag, str(path))]
        out = self.tmp / "store"

        def run_main(*args: str) -> tuple[int, str]:
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                code = build_store.main(["--record-set", str(self.set), "--out", str(out), *args])
            return code, err.getvalue()
        self.assertEqual(run_main(*side), (0, ""))
        self.assertEqual(run_main(*side, "--check"), (0, ""))
        view = json.loads((out / "atlas/v1/articles/1001.json").read_text())
        self.assertEqual(sorted(view["supplements"]), ["passage_text", "provenance", "signatures"])
        # A check without the side files, or with one of them changed, finds the store differs.
        code, err = run_main("--check")
        self.assertEqual(code, 1)
        self.assertIn("differs: atlas/v1/articles/1001.json", err)
        table = self.signature_table()
        table["mathlib"][self.commits["mathlib"]]["Widget.exists_spanning_gadget"]["signature"] += " "
        self.signature_file(table)
        code, err = run_main(*side, "--check")
        self.assertEqual(code, 1)
        self.assertIn("differs: atlas/v1/current.json", err)
        # A side file that cannot be read is a refusal (exit 2), and the store stays as it was.
        before = (out / "atlas/v1/current.json").read_bytes()
        code, err = run_main("--passage-text", str(self.tmp / "missing.json"), "--replace")
        self.assertEqual(code, 2)
        self.assertIn("--passage-text", err)
        self.assertEqual((out / "atlas/v1/current.json").read_bytes(), before)



class ReaderHtmlTests(SetCopyTest):
    """--reader-html (build_store.attach_reader_html): each article's revision with its passages marked.

    The Widget lemma's body here is synthetic: a hatnote, the four passages (the bound's text runs
    through a link, so it is three marks; the conjecture holds a citation marker and a formula, each
    wrapped whole) and a reference list. The records are given the spans and span_sha256 of those
    passages on the body's layer, and sources.jsonl the layer's sha256, as an export pins them.
    A passage with no status is never marked, so the conjecture, unresolved in the fixture, is a
    statement-only exact match here (locate_conjecture); unlocate_conjecture puts it back.
    """

    STYLE = ('<style data-mw-deduplicate="TemplateStyles:r1">.mw-parser-output .hatnote{font-style:italic}'
             '@media screen{body.ns-0 .mw-parser-output .navbox+.navbox{margin-top:-1px}}</style>')
    FORMULA = ('<span class="mwe-math-element mwe-math-element-inline"><span class="mwe-math-mathml-a11y">'
               '<math xmlns="http://www.w3.org/1998/Math/MathML" alttext="{\\displaystyle x^{2}}"><semantics><mrow>'
               '<msup><mi>x</mi><mn>2</mn></msup></mrow><annotation encoding="application/x-tex">{\\displaystyle x^{2}}'
               '</annotation></semantics></math></span></span>')
    TEXTS = {"3fa91c07": "every finite widget admits a spanning gadget",
             "5b0e44a1": "Moreover, the number of spanning gadgets of a widget is at most its rank.",
             "91aa02de": "a widget is balanced if every gadget has equal weight",
             "0c4d77b1": "every balanced widget is the image of a regular one,[3] x2{\\displaystyle x^{2}} ."}
    STATUS = {"3fa91c07": "formalized", "5b0e44a1": "statement_only", "91aa02de": "not_formalized",
              "0c4d77b1": "statement_only"}
    IDS = {hex_: f"enwiki-1001-2002::In-text #{hex_}" for hex_ in TEXTS}
    # A Commons file and an English Wikipedia upload as a body fetches them, and the licences the
    # entry names for them (made up).
    COMMONS = "https://upload.wikimedia.org/wikipedia/commons/a/ab/Widget_diagram.svg"
    COMMONS_THUMB = "https://thumb.wikimedia.org/wikipedia/commons/thumb/a/ab/Widget_diagram.svg/220px-Widget_diagram.svg.png"
    EN_THUMB = ("https://thumb.wikimedia.org/wikipedia/en/thumb/9/99/Gadget_icon.svg/40px-Gadget_icon.svg.png"
                "?utm_source=en.wikipedia.org&utm_campaign=parser")
    # Every file carries its file page's restrictions ([] for none), and an English Wikipedia upload
    # also its categories, as the API lists them.
    FREE = ["Category:All free media"]
    # Each file's credit as the page lists it: its author as its file page names them, and a link to
    # the licence where the licence has a text of its own (made up, as every value here).
    CC_BY_SA_4 = "https://creativecommons.org/licenses/by-sa/4.0/"
    NO_CREDIT = {"licence_url": None, "author": None}
    IMAGES = {"Widget_diagram.svg": {"project": "commons", "licence": "CC BY-SA 4.0", "licence_id": "cc-by-sa-4.0",
                                     "licence_url": CC_BY_SA_4, "author": "A. Widgetmaker", "restrictions": []},
              "Gadget_icon.svg": {"project": "en", "licence": "Public domain", "licence_id": "pd", **NO_CREDIT,
                                  "restrictions": [], "categories": FREE}}

    def mark(self, hex_: str, inner: str, first: bool = True) -> str:
        anchor = f' id="p-{hex_}"' if first else ""
        return f'<mark class="atl" data-p="{hex_}" data-status="{self.STATUS[hex_]}"{anchor}>{inner}</mark>'

    def body(self, omit: tuple[str, ...] = ()) -> str:
        m = (lambda h, inner, first=True: inner if h in omit else self.mark(h, inner, first))
        return (f'<div class="mw-parser-output">{self.STYLE}\n'
                '<div role="note" class="hatnote">For gadgets, see <a href="/atlas/1003/Gadget_(mathematics)">'
                'Gadget (mathematics)</a>.</div>\n'
                f'<p>The widget lemma states that {m("3fa91c07", "every finite widget admits a spanning gadget")} '
                'of minimal degree.<sup class="reference" id="cite_ref-1"><a href="#cite_note-1">[1]</a></sup></p>\n'
                '<h2 id="Definitions">Definitions</h2>\n'
                f'<p>In this article, {m("91aa02de", "a widget is balanced if every gadget has equal weight")}.</p>\n'
                '<h2 id="Statement">Statement</h2>\n'
                f'<p>{m("5b0e44a1", "Moreover, the number of ")}<a href="https://en.wikipedia.org/wiki/Spanning_tree" '
                f'target="_blank" rel="noopener noreferrer">{m("5b0e44a1", "spanning gadgets", False)}</a>'
                f'{m("5b0e44a1", " of a widget is at most its rank.", False)}</p>\n'
                f'<ul><li>{m("0c4d77b1", "every balanced widget is the image of a regular one,")}'
                f'{m("0c4d77b1", "<sup class=\"reference\"><a href=\"#cite_note-3\">[3]</a></sup>", False)} '
                f'{m("0c4d77b1", self.FORMULA, False)} {m("0c4d77b1", ".", False)}</li></ul>\n'
                '<ol class="references"><li id="cite_note-1"><span class="reference-text">A. Smith, <i>Widgets</i> '
                '&amp; gadgets (2020).</span></li></ol>\n</div>')

    @staticmethod
    def layer_of(html_text: str) -> str:
        """The layer the way a reader would compute it by hand: tags and style out, references decoded."""
        import html as html_lib
        text = re.sub(r"<style\b[^>]*>.*?</style>", "", html_text, flags=re.S)
        return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", "", text))).strip()

    def setUp(self):
        super().setUp()
        self.layer = self.layer_of(self.body())
        self.spans = {}
        for hex_, text in self.TEXTS.items():
            start = self.layer.index(text)
            self.assertEqual(self.layer.count(text), 1, text)
            self.spans[hex_] = [start, start + len(text)]
        layer_sha = hashlib.sha256(self.layer.encode("utf-8")).hexdigest()
        sources = self.set / "sources.jsonl"
        rows = [json.loads(line) for line in sources.read_text(encoding="utf-8").splitlines()]
        rows[0]["layer_sha256"] = layer_sha
        sources.write_text("".join(make_fixture.dumps(row) + "\n" for row in rows), encoding="utf-8")
        self.source = rows[0]

        def place(rows):
            for row in rows:
                hex_ = row["id"].rsplit("#", 1)[1]
                ext = row["subject"][0]["extensions"][LOCATE]
                ext["span"] = self.spans[hex_]
                ext["layer_sha256"] = layer_sha
                ext["span_sha256"] = hashlib.sha256(self.TEXTS[hex_].encode("utf-8")).hexdigest()
        self.edit_widget(place)
        self.edit_widget(self.locate_conjecture)

    def locate_conjecture(self, rows: list[dict]) -> None:
        """Make the conjecture's unresolved record an exact match to a statement-only declaration."""
        bound = next(row for row in rows if row["id"] == self.IDS["5b0e44a1"])
        for row in rows:
            if row["id"] == self.IDS["0c4d77b1"]:
                end = json.loads(json.dumps(bound["object"][0]))
                end["location"]["declaration"] = "TauCeti.Widget.balanced_is_regular_image"
                row.update(object=[end], relation="exactMatch", status="statement_only")
                row["extensions"][LOCATE].update(rank=1, confidence="medium",
                                                 location_key=hashlib.sha256(b"the conjecture's location").hexdigest())

    def unlocate_conjecture(self) -> None:
        """The conjecture as the fixture has it: one unresolved record, a passage with no status."""
        original = next(row for row in read_jsonl(RECORDSET / "records/enwiki-1001-2002.jsonl")
                        if row["id"] == self.IDS["0c4d77b1"])

        def restore(rows):
            for i, row in enumerate(rows):
                if row["id"] == self.IDS["0c4d77b1"]:
                    placed = row["subject"][0]["extensions"][LOCATE]
                    back = json.loads(json.dumps(original))
                    back["subject"][0]["extensions"][LOCATE].update(
                        {key: placed[key] for key in ("span", "layer_sha256", "span_sha256")})
                    rows[i] = back
        self.edit_widget(restore)

    def entry(self, html_text: str | None = None, **change) -> dict:
        html_text = self.body() if html_text is None else html_text
        return {"source_id": "enwiki-1001-2002", "revision": 2002, "html_sha256": self.source["html_sha256"],
                "layer_sha256": self.source["layer_sha256"], "renderer": "web-html/1", "html": html_text,
                "unplaced": [], **change}

    def side_file(self, *entries: tuple[str, dict], schema: str = build_store.READER_HTML_SCHEMA) -> Path:
        path = self.tmp / "reader-html.json"
        path.write_text(json.dumps({"schema": schema, "articles": dict(entries)}, ensure_ascii=False), encoding="utf-8")
        return path

    def build_with(self, path: Path) -> dict[str, bytes]:
        return build_store.build(self.set, supplements=build_store.Supplements.read(reader_html=path))

    def refuse_html(self, fragment: str, html_text: str | None = None, **change) -> None:
        with self.assertRaises(build_store.AtlasError) as caught:
            self.build_with(self.side_file(("1001", self.entry(html_text, **change))))
        self.assertIn(fragment, str(caught.exception))

    # ---- present, absent --------------------------------------------------------------------

    def test_without_the_side_file_the_store_is_the_plain_build(self):
        self.assertIsNone(build_store.Supplements.read(reader_html=None))
        tree = build_store.build(self.set)
        self.assertFalse([name for name in tree if "/reader/" in name])
        self.assertNotIn(b'"reader":', tree["atlas/v1/articles/1001.json"])

    def test_the_marked_revision_is_stored_where_it_checks_out(self):
        plain = build_store.build(self.set)
        html_text = self.body()
        path = self.side_file(("1001", self.entry()), ("9", self.entry(source_id="enwiki-9-9")))
        tree = self.build_with(path)
        data = html_text.encode("utf-8")
        self.assertEqual(tree["atlas/v1/reader/1001.html"], data)
        self.assertEqual(sorted(set(tree) - set(plain)), ["atlas/v1/reader/1001.html"])
        view = json.loads(tree["atlas/v1/articles/1001.json"])
        self.assertEqual(view["reader"], {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "marks": 9,
                                          "passages": 4, "unplaced": []})
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(view["supplements"], {"reader_html": {"schema": build_store.READER_HTML_SCHEMA, "sha256": sha}})
        self.assertNotIn("reader", json.loads(tree["atlas/v1/articles/1003.json"]))
        current = json.loads(tree["atlas/v1/current.json"])
        self.assertEqual(current["supplements"]["reader_html"],
                         {"schema": build_store.READER_HTML_SCHEMA, "sha256": sha, "articles": 1, "passages": 4, "marks": 9})
        manifest = json.loads(tree["atlas/v1/manifest.json"])
        self.assertEqual(manifest["files"]["atlas/v1/reader/1001.html"],
                         {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        keys = ("reader", "supplements")
        self.assertEqual(SupplementTests.without(view, keys), json.loads(plain["atlas/v1/articles/1001.json"]))
        self.assertEqual(SupplementTests.without(current, keys), json.loads(plain["atlas/v1/current.json"]))
        for name in plain:
            if "/articles/" not in name and not name.endswith(("current.json", "manifest.json")):
                self.assertEqual(tree[name], plain[name], name)
        # The same inputs build the same bytes.
        self.assertEqual(self.build_with(path), tree)

    def test_a_passage_with_no_status_is_never_marked(self):
        # It gets no highlight until it is read: its marks are refused, and unplaced lists it.
        self.unlocate_conjecture()
        unmarked = self.body(omit=("0c4d77b1",))
        listed = [{"item": "In-text #0c4d77b1", "reason": "no status: not highlighted until it is read"}]
        tree = self.build_with(self.side_file(("1001", self.entry(unmarked, unplaced=listed))))
        reader = json.loads(tree["atlas/v1/articles/1001.json"])["reader"]
        self.assertEqual((reader["passages"], reader["marks"], reader["unplaced"]), (3, 5, [self.IDS["0c4d77b1"]]))
        for status in ("unresolved", "statement_only"):
            with self.subTest(status=status):
                marked = self.body().replace('data-p="0c4d77b1" data-status="statement_only"',
                                             f'data-p="0c4d77b1" data-status="{status}"')
                self.refuse_html("a mark on a passage with no status (it gets no highlight until it is read; "
                                 "unplaced lists it)", marked, unplaced=listed)

    def test_a_passage_left_unmarked_is_unplaced_and_must_be_listed(self):
        html_text = self.body(omit=("0c4d77b1",))
        listed = [{"item": "In-text #0c4d77b1", "reason": "the proof named it"}]
        tree = self.build_with(self.side_file(("1001", self.entry(html_text, unplaced=listed))))
        reader = json.loads(tree["atlas/v1/articles/1001.json"])["reader"]
        self.assertEqual((reader["passages"], reader["marks"], reader["unplaced"]), (3, 5, [self.IDS["0c4d77b1"]]))
        self.refuse_html("unplaced must list, as {item, reason}, exactly the passages the html leaves unmarked", html_text)
        self.refuse_html("unplaced must list", unplaced=listed)

    # ---- refused -----------------------------------------------------------------------------

    def test_marks_that_are_not_their_passages_are_refused(self):
        body = self.body()
        first = self.mark("3fa91c07", "every finite widget admits a spanning gadget")
        cases = [
            # The text must be the revision's, every character.
            (body.replace("minimal degree", "maximal degree"), "is not the revision's layer (layer_sha256)"),
            (body.replace("&amp; gadgets", "&amp;amp; gadgets"), "is not the revision's layer"),
            # A mark one character late or early covers text outside the span.
            (body.replace(f"{first} of minimal", first[:-7] + " o</mark>f minimal"), "its marks cover layer["),
            (body.replace(f"states that {first}", "states tha" + first.replace(">every", ">t every", 1)), "its marks cover layer["),
            # A piece of the passage left unmarked between two of its marks.
            (body.replace('<a href="https://en.wikipedia.org/wiki/Spanning_tree" target="_blank" rel="noopener noreferrer">'
                          + self.mark("5b0e44a1", "spanning gadgets", False),
                          '<a href="https://en.wikipedia.org/wiki/Spanning_tree" target="_blank" rel="noopener noreferrer">'
                          "spanning gadgets"), "leave text of the passage unmarked"),
            (body.replace('data-p="91aa02de" data-status="not_formalized"', 'data-p="91aa02de" data-status="partial"'),
             "a mark's data-status is not its passage's status"),
            (body.replace('data-p="91aa02de"', 'data-p="deadbeef"'), "a mark's data-p names no passage of this article"),
            (body.replace(f"{first} of minimal", first.replace("spanning gadget", self.mark("91aa02de", "spanning gadget", False))
                          + " of minimal"), "a mark is nested in another"),
            (body.replace("weight</mark>.</p>", "weight.</p><p></mark>"), "crosses an element boundary"),
            (body.replace(' id="p-3fa91c07"', ""), 'its first mark carries id="p-3fa91c07"'),
            (body.replace('data-status="statement_only">spanning', 'data-status="statement_only" id="p-5b0e44a1">spanning'),
             "only a passage's first mark carries its id"),
            (body.replace(' id="p-3fa91c07">', ' id="p-3fa91c07" title="x">'), 'a mark carries class="atl", data-p'),
        ]
        for html_text, fragment in cases:
            with self.subTest(fragment=fragment):
                self.assertNotEqual(html_text, body)
                self.refuse_html(fragment, html_text)

    def test_html_a_page_must_not_serve_is_refused(self):
        body = self.body()
        end = "\n</div>"
        cases = [
            ('<script>alert(1)</script>', "a <script> element is not allowed in reader HTML"),
            ('<iframe src="https://example.org/"></iframe>', "a <iframe> element is not allowed"),
            ('<form action="/x"></form>', "a <form> element is not allowed"),
            ('<p onclick="steal()">x</p>', "the onclick attribute (on a <p>) is not allowed"),
            ('<a href="javascript:steal()">x</a>', "a href must be an in-page fragment, an http(s) page or an atlas article"),
            ('<a href="//evil.example/">x</a>', "a href must be"),
            ('<a href="https://example.org/" target="_blank">x</a>', 'target="_blank" and rel noopener'),
            ('<img src="https://evil.example/x.png" alt="">', "a <img> src must be https on https://upload.wikimedia.org/"),
            ('<img src="https://upload.wikimedia.org/a.png" srcset="//thumb.wikimedia.org/b.png 2x" alt="">', "a srcset URL must be"),
            ('<span style="background:url(https://evil.example/)">x</span>', "a style attribute holds url()"),
            ('<div style="position: fixed; inset: 0">x</div>', "a style attribute holds"),
            ('<style>body{display:none}</style>', "the selector 'body' is not scoped to .mw-parser-output"),
            ('<style>.mw-parser-output ~ footer{display:none}</style>', "is not scoped to .mw-parser-output"),
            ('<style>@import url(https://upload.wikimedia.org/x.css);</style>', "a style element holds @import"),
            ('<style>.mw-parser-output .x{background:url("//evil.example/x.png")}</style>', "a style element fetches"),
            ('<style>@font-face{font-family:x}</style>', "the at-rule '@font-face' is not allowed"),
            ('<!-- a comment -->', "a comment (the canonical body has none)"),
            ('<div id="atlr-panel">x</div>', "the id 'atlr-panel' is the page's own"),
            ('<span id="p-3fa91c07">x</span>', "the id 'p-3fa91c07' is the page's own"),
            ('<span/>', "a self-closing <span/>"),
            ('<math xlink:href="javascript:x()"><mi>x</mi></math>', "the xlink:href attribute"),
        ]
        for extra, fragment in cases:
            with self.subTest(fragment=fragment):
                self.refuse_html(fragment, body.replace(end, extra + end))
        # The ones that add no text leave the layer alone: refused for themselves, not for that.
        with self.assertRaises(build_store.AtlasError) as caught:
            self.build_with(self.side_file(("1001", self.entry(body.replace(end, '<p onclick="x()"></p>' + end)))))
        self.assertNotIn("layer_sha256", str(caught.exception))

    def test_markup_a_browser_reads_differently_is_refused(self):
        # html.parser reads a <style> as CSS wherever it is, and keeps every element where its tags
        # are; a browser does neither inside MathML, and moves, closes and reopens elements by
        # HTML's tree rules. What the builder checks must be what the page holds.
        body = self.body()
        end = "\n</div>"
        wm = "https://upload.wikimedia.org/x.png"
        cases = [
            ("<math><style><img src=x onerror=alert(1)></style></math>", "a style element inside a formula"),
            ("<math><mrow><style><img src=x onerror=alert(1)></style></mrow></math>", "a style element inside a formula"),
            ('<math><semantics><mrow></mrow><annotation encoding="application/x-tex"><style><b>x</b></style>'
             "</annotation></semantics></math>", "a style element inside a formula"),
            ("<math><mi><style>.mw-parser-output a{x:y}</style></mi></math>", "a style element inside a formula"),
            ("<math><mrow><b>x</b></mrow></math>", "a <b> inside a formula"),
            ("<style>.mw-parser-output a{x:y}<img></style>", "a style element holds '<'"),
            ("<mi>x</mi>", "a <mi> outside a formula"),
            ("<div>", "an element is never closed (<div>)"),
            ("<table><tbody><tr><td>", "a </div> closes a <div> with a <td> still open inside it"),
            ('<a href="https://evil.example/">', "a </div> closes a <div> with a <a> still open inside it"),
            ("<p><b>x</p></b>", "a </p> closes a <p> with a <b> still open inside it"),
            ("<b><p>x</b></p>", "a </b> closes a <b> with a <p> still open inside it"),
            ("<p>x</br></p>", "an end tag </br> of a void element"),
            ('<a href="#a">x<a href="#b">y</a></a>', "a link inside a link"),
            ("<p><span><div>x</div></span></p>", "a <div> inside a <p>"),
            ("<h3><h4>x</h4></h3>", "a heading inside a heading"),
            ("<ul><li><li>x</li></li></ul>", "a <li> inside a <li> of the same list"),
            ("<dl><dd><dt>x</dt></dd></dl>", "a <dt> inside a <dd> of the same list"),
            (f'<table><tbody><tr><td></td></tr></tbody><img src="{wm}" alt=""></table>',
             "a <img> directly in a <table>"),
            ("<table><tbody><tr>x<td></td></tr></tbody></table>", "text directly in a <tr>"),
            ("<div><td>x</td></div>", "a <td> outside its place in a table"),
            (f'<img name="getElementById" src="{wm}" alt="">', "the name attribute (on a <img>) is not allowed"),
            (f'<img src="{wm}" alt="" referrerpolicy="unsafe-url">', "a referrerpolicy must be no-referrer"),
            ('<video autoplay src="https://upload.wikimedia.org/x.webm"></video>', "the autoplay attribute"),
            (f'<a href="{wm}" download>x</a>', "the download attribute"),
            ("<span>\x00</span>", "html holds a control character"),
        ]
        for extra, fragment in cases:
            with self.subTest(fragment=fragment, extra=extra):
                self.refuse_html(fragment, body.replace(end, extra + end))
        # Left open at the very end, an element runs on into the page around the article.
        for extra, fragment in (("<table><tbody><tr><td>", "an element is never closed (<tbody>, <tr>, <td>)"),
                                ('<a href="https://evil.example/">', "an element is never closed (<a>)")):
            with self.subTest(fragment=fragment):
                self.refuse_html(fragment, body + extra)

    # ---- what a browser does that html.parser cannot see: requests, paths, the page's own classes ----

    def builds(self, html_text: str, **change) -> dict[str, bytes]:
        return self.build_with(self.side_file(("1001", self.entry(html_text, **change))))

    def test_an_attribute_outside_the_reader_list_is_refused(self):
        # An attribute is allowed only by name (READER_ATTRS, data-*, aria-*): a browser may give a
        # new one an effect. attributionsrc makes Chromium send a request to the hosts it names.
        body = self.body()
        end = "\n</div>"
        wm = "https://upload.wikimedia.org/x.png"
        cases = [
            (f'<img src="{wm}" attributionsrc="https://evil.example/r" alt="">', "the attributionsrc attribute (on a <img>)"),
            (f'<img src="{wm}" attributionsrc alt="">', "the attributionsrc attribute (on a <img>)"),
            ('<a href="https://example.org/" attributionsrc="https://evil.example/r"></a>', "the attributionsrc attribute (on a <a>)"),
            (f'<img src="{wm}" browsingtopics alt="">', "the browsingtopics attribute (on a <img>)"),
            (f'<img src="{wm}" sharedstoragewritable alt="">', "the sharedstoragewritable attribute (on a <img>)"),
            (f'<img src="{wm}" lowsrc="https://evil.example/l.png" alt="">', "the lowsrc attribute (on a <img>)"),
            (f'<img src="{wm}" usemap="#m" alt="">', "the usemap attribute (on a <img>)"),
            (f'<img src="{wm}" crossorigin="use-credentials" alt="">', "the crossorigin attribute (on a <img>)"),
            ('<span popovertarget="atlr-panel"></span>', "the popovertarget attribute (on a <span>)"),
            ('<a href="#x" interestfor="atlr-panel"></a>', "the interestfor attribute (on a <a>)"),
            # The name is read as written, entity and all, by html.parser and a browser alike.
            (f'<img src="{wm}" o&#110;error="alert(1)" alt="">', "the o&#110;error attribute (on a <img>)"),
        ]
        for extra, fragment in cases:
            with self.subTest(extra=extra):
                self.refuse_html(fragment + " is not allowed", body.replace(end, extra + end))
        # What Wikipedia's parse HTML and its MathML carry still builds, data-* and aria-* among it.
        kept = (f'<figure typeof="mw:File/Thumb"><span><img src="{self.COMMONS_THUMB}" srcset="{self.COMMONS} 2x" decoding="async" '
                f'loading="lazy" width="220" height="165" data-file-width="400" data-file-height="300" class="mw-file-element" '
                'alt="" referrerpolicy="no-referrer"></span></figure>'
                '<table class="wikitable" border="1" cellpadding="2" role="presentation"><tbody><tr align="center" valign="top">'
                '<th scope="col" colspan="2" rowspan="1" data-sort-value="1"></th></tr></tbody></table>'
                '<span aria-hidden="true" title="" lang="la" dir="ltr" data-mw-x="1"></span>'
                '<math xmlns="http://www.w3.org/1998/Math/MathML" display="block" alttext="x"><mstyle displaystyle="true" '
                'scriptlevel="0" mathcolor="#000"><mo stretchy="false" fence="true" form="prefix" lspace="0" rspace="0" '
                'minsize="1.2em" maxsize="1.2em" symmetric="true" movablelimits="false" accent="false"></mo>'
                '<mtable columnalign="left" columnspacing="1em" rowspacing="4pt" rowlines="none" columnlines="none">'
                '</mtable><mpadded width="0" height="0" depth="0"></mpadded><mfrac linethickness="0"></mfrac>'
                '<menclose notation="box"></menclose></mstyle></math>')
        self.builds(body.replace(end, kept + end), images={"Widget_diagram.svg": self.IMAGES["Widget_diagram.svg"]})
        listed = build_store.READER_ATTRS
        self.assertFalse(listed & build_store.READER_ACTIVE_ATTRS)
        self.assertFalse([name for name in listed if name.startswith("on") or ":" in name or name == "name"])
        self.assertFalse([name for name in ("href", "src", "srcset", "poster", "cite", "style", "id", "class") if name not in listed])

    def test_an_atlas_link_keeps_to_the_slug_grammar(self):
        # /atlas/<pageid>[/<slug>], the slug one path segment as atlasSlug writes it. A browser
        # removes dot segments, %2e ones too, so /atlas/1/../../stats is /stats: a link would
        # leave the atlas for another path of the site. html.parser decodes the attribute first,
        # as the browser does.
        body = self.body()
        end = "\n</div>"
        refused = [
            "/atlas/1/../../stats", "/atlas/1/%2e%2e/%2e%2e/stats", "/atlas/1/%2E%2e/stats", "/atlas/1/.%2e/x",
            "/atlas/1/Gadget/../../stats", "/atlas/1003/Gadget_(mathematics)/x", "/atlas/1/", "/atlas/1//stats",
            "/atlas/1/Gadget?view=cards", "/atlas/1/Gadget\\..\\..\\stats", "/atlas/1/.\t./stats", "/atlas/1/%2", "/atlas/x",
            "/atlas/1/&#46;&#46;/&#46;&#46;/stats",
        ]
        for href in refused:
            with self.subTest(href=href):
                self.refuse_html("a link into the atlas is /atlas/<pageid> or /atlas/<pageid>/<slug>",
                                 body.replace(end, f'<a href="{href}"></a>' + end))
        # A whole segment that is a dot segment: a browser resolves /atlas/1/.. to /atlas/.
        for href in ("/atlas/1/..", "/atlas/1/.", "/atlas/1/%2e", "/atlas/1/%2E%2E", "/atlas/1/.%2E", "/atlas/1/..#p-3fa91c07"):
            with self.subTest(href=href):
                self.refuse_html("a link into the atlas has a dot segment", body.replace(end, f'<a href="{href}"></a>' + end))
        # What atlasSlug writes builds: the bare page id, escapes, and a fragment.
        for href in ("/atlas/1003", "/atlas/1003/Gadget_(mathematics)", "/atlas/1003/M%C3%B6bius_strip#Properties",
                     "/atlas/1003/Euler's_identity", "/atlas/1003/1%2F2_%2B_1%2F4", "/atlas/1003/..._(ellipsis)"):
            with self.subTest(href=href):
                self.builds(body.replace(end, f'<a href="{href}"></a>' + end))

    def test_a_link_to_this_site_itself_is_refused(self):
        # The reader reaches the site only through atlas articles: an absolute link to its own
        # host reaches every other path of it. A host is plain ASCII, so no escape, user or
        # backslash reads as another host to the check and as this one to a browser.
        body = self.body()
        end = "\n</div>"
        for href in ("https://wikilean.jackmccarthy.org/stats", "http://wikilean.jackmccarthy.org/stats",
                     "https://WikiLean.JackMcCarthy.org./stats", "https://wikilean.jackmccarthy.org:443/stats",
                     "https://wikilean.jackmccarthy.org", "https://wikilean.jackmccarthy.org?x", "https://wikilean.jackmccarthy.org#x"):
            with self.subTest(href=href):
                self.refuse_html("a link to this site itself", body.replace(end, f'<a href="{href}"></a>' + end))
        for href in ("https://wikilean.jackmccarthy.org\\@evil.example/", "https://evil.example\\@wikilean.jackmccarthy.org/",
                     "https://x@wikilean.jackmccarthy.org/stats", "https://wikilean%2ejackmccarthy.org/stats",
                     "https://wikilean。jackmccarthy。org/stats", "https://wikilean.jackmccarthy.org/st\tats",
                     "https:/wikilean.jackmccarthy.org/stats", "https:wikilean.jackmccarthy.org/stats"):
            with self.subTest(href=href):
                self.refuse_html("a href must be an in-page fragment, an http(s) page or an atlas article",
                                 body.replace(end, f'<a href="{href}"></a>' + end))
        for href in ("https://en.wikipedia.org/wiki/Gadget", "http://example.org:8080/a?b=c#d", "https://wikilean.example.org/stats",
                     "https://jackmccarthy.org/", "https://doi.org/10.1000/182"):
            with self.subTest(href=href):
                self.builds(body.replace(end, f'<a href="{href}"></a>' + end))

    def test_the_pages_own_classes_are_refused(self):
        # The article must not wear the page's classes: .atlr-peek and .atlr-fab are position:fixed
        # over the page, .atlas-chip[data-filter] a filter control, .wl-header the sticky header.
        body = self.body()
        end = "\n</div>"
        for value, token in (("atlr-fab atlas-chip", "atlr-fab"), ("x atlr-peek", "atlr-peek"), ("WL-header", "WL-header"),
                             ("atlas-fb", "atlas-fb"), ("mw-parser-output\tatlr-panel", "atlr-panel")):
            with self.subTest(value=value):
                self.refuse_html(f"the class {token!r} is the page's own", body.replace(end, f'<div class="{value}"></div>' + end))
        # Wikipedia's own, and a prefix only as a whole token's start.
        self.builds(body.replace(end, '<div class="mw-halign-right skin-invert latlas-x x-atlr hatnote"></div>' + end))

    def test_a_file_is_shown_only_with_its_licence_and_never_a_wikimedia_mark(self):
        # Commons is free by its policy; an English Wikipedia upload may not be, so it is shown only
        # with a free licence named. Every file the body fetches, a TemplateStyles url() included,
        # is named in `images`, which the view lists for the page's attribution.
        body = self.body()
        end = "\n</div>"
        lock = "https://upload.wikimedia.org/wikipedia/commons/6/65/Lock-green.svg"
        media = (f'<figure typeof="mw:File/Thumb"><a href="https://en.wikipedia.org/wiki/File:Widget_diagram.svg" '
                 f'target="_blank" rel="noopener noreferrer"><img src="{self.COMMONS_THUMB}" srcset="{self.COMMONS} 2x" '
                 f'alt=""></a><figcaption></figcaption></figure><span typeof="mw:File"><img src="{self.EN_THUMB}" alt="">'
                 '</span><video poster="https://thumb.wikimedia.org/wikipedia/commons/thumb/c/c6/Spin.webm/220px--Spin.webm.jpg" '
                 'controls preload="none"><source src="https://upload.wikimedia.org/wikipedia/commons/transcoded/c/c6/Spin.webm/'
                 'Spin.webm.480p.vp9.webm" type="video/webm"></video>'
                 f'<style>.mw-parser-output .lock a{{background:url("{lock}")right 0.1em center/9px no-repeat}}</style>')
        # Spin.webm's licence has no id and is free by its name; it asks for its author's name.
        images = {**self.IMAGES, "Spin.webm": {"project": "commons", "licence": "Attribution", "licence_id": None,
                                               "licence_url": None, "author": "B. Spinner", "restrictions": []},
                  "Lock-green.svg": {"project": "commons", "licence": "Public domain", "licence_id": "pd",
                                     **self.NO_CREDIT, "restrictions": ["personality"]}}
        html_text = body.replace(end, media + end)
        tree = self.builds(html_text, images=images)
        self.assertEqual(tree["atlas/v1/reader/1001.html"], html_text.encode("utf-8"))
        self.assertEqual(json.loads(tree["atlas/v1/articles/1001.json"])["reader"]["images"], [
            {"file": "Gadget_icon.svg", "project": "en", "licence": "Public domain", "licence_url": None, "author": None},
            {"file": "Lock-green.svg", "project": "commons", "licence": "Public domain", "licence_url": None, "author": None},
            {"file": "Spin.webm", "project": "commons", "licence": "Attribution", "licence_url": None,
             "author": "B. Spinner"},
            {"file": "Widget_diagram.svg", "project": "commons", "licence": "CC BY-SA 4.0", "licence_url": self.CC_BY_SA_4,
             "author": "A. Widgetmaker"}])
        # A body that shows no file has no list.
        self.assertNotIn("images", json.loads(self.builds(body)["atlas/v1/articles/1001.json"])["reader"])

        def refuse(fragment, extra="", **listed):
            self.refuse_html(fragment, body.replace(end, extra + end), images=listed.get("images", images))

        en = f'<span typeof="mw:File"><img src="{self.EN_THUMB}" alt=""></span>'
        refuse("images names no licence for File:Gadget_icon.svg (an English Wikipedia upload)", en, images={})
        refuse("File:Gadget_icon.svg is an English Wikipedia upload without a free licence (licence_id 'cc-by-nc-4.0')", en,
               images={"Gadget_icon.svg": {**self.NO_CREDIT, "project": "en", "licence": "CC BY-NC 4.0",
                                           "licence_id": "cc-by-nc-4.0", "restrictions": [],
                                           "categories": ["Category:All non-free media"]}})
        refuse("File:Gadget_icon.svg is an English Wikipedia upload without a free licence (licence_id None)", en,
               images={"Gadget_icon.svg": {**self.NO_CREDIT, "project": "en", "licence": None, "licence_id": None,
                                           "restrictions": [], "categories": self.FREE}})
        refuse("images names no licence for File:Gadget_icon.svg (an English Wikipedia upload)", en,
               images={"Gadget_icon.svg": {**self.NO_CREDIT, "project": "commons", "licence": "PD", "licence_id": "pd",
                                           "restrictions": []}})
        refuse("File:Widget_diagram.svg's licence id 'cc-by-nc-sa-3.0' is not a free licence", f'<img src="{self.COMMONS}" alt="">',
               images={"Widget_diagram.svg": {**self.NO_CREDIT, "project": "commons", "licence": "CC BY-NC-SA 3.0",
                                              "licence_id": "cc-by-nc-sa-3.0", "restrictions": []}})
        refuse("images names no licence for File:Widget_diagram.svg (a Commons file)", f'<img src="{self.COMMONS}" alt="">',
               images={})
        refuse("images lists File:Gadget_icon.svg, which the html does not show", "",
               images={"Gadget_icon.svg": self.IMAGES["Gadget_icon.svg"]})
        refuse("images[Gadget_icon.svg]: licence is the licence's printable name or null", en,
               images={"Gadget_icon.svg": {**self.NO_CREDIT, "project": "en", "licence": "<b>PD</b>", "licence_id": "pd",
                                           "restrictions": [], "categories": self.FREE}})
        refuse("images names no licence for File:Gadget_icon.svg", en,
               images={"Gadget_icon.svg": {**self.NO_CREDIT, "project": "en", "licence": "PD", "licence_id": "pd",
                                           "restrictions": [], "categories": self.FREE, "note": "x"}})
        # Every file names its restrictions; an English Wikipedia upload names its categories, and a
        # Commons file names none.
        refuse("images names no licence for File:Gadget_icon.svg (an English Wikipedia upload)", en,
               images={"Gadget_icon.svg": {**self.NO_CREDIT, "project": "en", "licence": "Public domain", "licence_id": "pd",
                                           "restrictions": []}})
        refuse("images names no licence for File:Gadget_icon.svg (an English Wikipedia upload)", en,
               images={"Gadget_icon.svg": {**self.NO_CREDIT, "project": "en", "licence": "Public domain", "licence_id": "pd",
                                           "categories": self.FREE}})
        refuse("images names no licence for File:Widget_diagram.svg (a Commons file)", f'<img src="{self.COMMONS}" alt="">',
               images={"Widget_diagram.svg": {**self.NO_CREDIT, "project": "commons", "licence": "CC BY-SA 4.0",
                                              "licence_id": "cc-by-sa-4.0"}})
        refuse("images names no licence for File:Widget_diagram.svg (a Commons file)", f'<img src="{self.COMMONS}" alt="">',
               images={"Widget_diagram.svg": {**self.IMAGES["Widget_diagram.svg"], "categories": self.FREE}})
        for categories in ("Category:All free media", [3], ["All free media"], ["Category:<b>x</b>"],
                           ["Category:x"] * 201):
            with self.subTest(categories=str(categories)[:40]):
                refuse("images[Gadget_icon.svg]: categories is the list of the upload's categories", en,
                       images={"Gadget_icon.svg": {**self.IMAGES["Gadget_icon.svg"], "categories": categories}})
        for restrictions in ("trademarked", [3], ["Trademarked"], ["trademarked", "trademarked"], ["x y"],
                             ["personality"] * 17):
            with self.subTest(restrictions=str(restrictions)[:40]):
                refuse("images[Widget_diagram.svg]: restrictions is the list of the file page's restrictions",
                       f'<img src="{self.COMMONS}" alt="">',
                       images={"Widget_diagram.svg": {**self.IMAGES["Widget_diagram.svg"], "restrictions": restrictions}})
        # A file page that marks a file trademarked makes it a mark whatever its name, its project or its
        # licence; a restriction on its use (personality rights, say) does not make it non-free.
        for name, project, url in (
                ("Gadget_seal.svg", "commons", "https://upload.wikimedia.org/wikipedia/commons/7/7e/Gadget_seal.svg"),
                ("Gadget_icon.svg", "en", self.EN_THUMB)):
            with self.subTest(trademarked=name):
                entry = {**(self.IMAGES["Widget_diagram.svg"] if project == "commons" else self.IMAGES["Gadget_icon.svg"]),
                         "restrictions": ["personality", "trademarked"]}
                refuse(f"File:{name}'s file page marks it trademarked, so it is a mark", f'<img src="{url}" alt="">',
                       images={name: entry})
        self.builds(body.replace(end, f'<img src="{self.COMMONS}" alt="">' + end),
                    images={"Widget_diagram.svg": {**self.IMAGES["Widget_diagram.svg"], "restrictions": ["personality"]}})
        # A free licence id alone does not make an upload free everywhere. This is the shape of the API's
        # record of a photo English Wikipedia hosts under freedom of panorama (free in the United States
        # only), the file made up: its licence id is free, its categories say what it is, and the reader
        # leaves it out.
        photo = "Gadget_tower_photo.jpg"
        photo_url = "https://upload.wikimedia.org/wikipedia/en/thumb/4/4f/Gadget_tower_photo.jpg/220px-Gadget_tower_photo.jpg"
        refuse(f"File:{photo} is an English Wikipedia upload that is not free everywhere (it is in "
               "'Category:All free in US media')", f'<img src="{photo_url}" alt="">',
               images={photo: {**self.NO_CREDIT, "project": "en", "licence": "CC-BY-SA-2.5", "licence_id": "cc-by-sa-2.5",
                               "restrictions": [],
                               "categories": ["Category:All free in US media", "Category:All free media",
                                              "Category:Wikipedia files not suitable for Commons",
                                              "Category:Wikipedia images using freedom of panorama"]}})
        for barred in ("Category:Wikipedia files not suitable for Commons",
                       "Category:Wikipedia images using freedom of panorama", "Category:All non-free media"):
            with self.subTest(category=barred):
                refuse(f"File:Gadget_icon.svg is an English Wikipedia upload that is not free everywhere (it is in "
                       f"{barred!r})", en,
                       images={"Gadget_icon.svg": {**self.IMAGES["Gadget_icon.svg"], "categories": [*self.FREE, barred]}})
        refuse("File:Gadget_icon.svg is an English Wikipedia upload that is not free everywhere (it is not in "
               "'Category:All free media')", en,
               images={"Gadget_icon.svg": {**self.IMAGES["Gadget_icon.svg"], "categories": []}})
        refuse("images must map each file the html shows", en, images=["Gadget_icon.svg"])
        # A Commons file is shown only with a free licence named: by its id, or by its name when the API
        # gives it no id. Hosting on Commons is not a licence (a file page whose licence template is
        # broken gives the API none).
        diagram = f'<img src="{self.COMMONS}" alt="">'
        widget = self.IMAGES["Widget_diagram.svg"]
        refuse("File:Widget_diagram.svg is a Commons file whose licence images does not name", diagram,
               images={"Widget_diagram.svg": {**widget, "licence": None, "licence_id": None}})
        refuse("File:Widget_diagram.svg's licence 'CC BY-NC-SA 3.0' has no licence id and is no free licence by its name",
               diagram, images={"Widget_diagram.svg": {**widget, "licence": "CC BY-NC-SA 3.0", "licence_id": None}})
        for name, author in (("GPL", "C. Coder"), ("LGPL", "C. Coder"), ("Copyrighted free use", None),
                             ("Public domain", None)):
            with self.subTest(free_by_name=name):
                self.builds(body.replace(end, diagram + end), images={"Widget_diagram.svg": {
                    **widget, "licence": name, "licence_id": None, "author": author,
                    "licence_url": "https://www.gnu.org/licenses/gpl.html" if "GPL" in name else None}})
        # The licences that ask for it: the author's name, and the licence's own page for those with a text.
        refuse("File:Widget_diagram.svg is under CC BY-SA 4.0, which asks for its author's name with every copy, and "
               "images names no author", diagram, images={"Widget_diagram.svg": {**widget, "author": None}})
        refuse("File:Widget_diagram.svg is under Attribution, which asks for its author's name", diagram,
               images={"Widget_diagram.svg": {**widget, "licence": "Attribution", "licence_id": None, "author": None,
                                              "licence_url": None}})
        refuse("File:Widget_diagram.svg is under CC BY-SA 4.0, whose terms ask for a link to the licence with every copy, "
               "and images gives no licence_url", diagram, images={"Widget_diagram.svg": {**widget, "licence_url": None}})
        refuse("File:Gadget_icon.svg is under Cc-by-sa-3.0, which asks for its author's name", en,
               images={"Gadget_icon.svg": {**self.IMAGES["Gadget_icon.svg"], "licence": "Cc-by-sa-3.0",
                                           "licence_id": "cc-by-sa-3.0"}})
        # Every free licence but the public domain and CC0 asks for the author's name, whatever its id.
        for licence_id in ("cc-by-2.0", "cc-by-sa-3.0-de", "gfdl-1.2", "gpl-2.0+", "lgpl", "fal", "ogl-3",
                           "attribution"):
            with self.subTest(attribution=licence_id):
                refuse("which asks for its author's name", diagram,
                       images={"Widget_diagram.svg": {**widget, "licence": "Some licence", "licence_id": licence_id,
                                                      "author": None}})
        for licence_id in ("pd", "pd-self", "cc0"):
            with self.subTest(no_attribution=licence_id):
                self.builds(body.replace(end, diagram + end), images={"Widget_diagram.svg": {
                    **widget, "licence": "Some licence", "licence_id": licence_id, "author": None, "licence_url": None}})
        for author in ("<b>A. Widgetmaker</b>", " A. Widgetmaker", "A.\nWidgetmaker", "x" * 301, 7, ""):
            with self.subTest(author=str(author)[:40]):
                refuse("images[Widget_diagram.svg]: author is the file's author as its file page credits them", diagram,
                       images={"Widget_diagram.svg": {**widget, "author": author}})
        for url in ("http://creativecommons.org/licenses/by-sa/4.0/", "https://evil.example/licenses/by-sa/4.0/",
                    "https://creativecommons.org/licenses/by-sa/4.0/?x=1", "https://creativecommons.org.evil.example/",
                    "https://creativecommons.org", "javascript:alert(1)", 3):
            with self.subTest(licence_url=str(url)[:50]):
                refuse("images[Widget_diagram.svg]: licence_url is the licence's own page, https on", diagram,
                       images={"Widget_diagram.svg": {**widget, "licence_url": url}})
        # The Wikimedia projects' marks and the Foundation's (logos, wordmarks, icons, the Wikipedia W),
        # wherever they are hosted and however listed, whether or not "logo" is in the name.
        for name, url in (
                ("Commons-logo.svg", "https://thumb.wikimedia.org/wikipedia/en/thumb/4/4a/Commons-logo.svg/40px-Commons-logo.svg.png"),
                ("Wiktionary-logo-en-v2.svg", "https://upload.wikimedia.org/wikipedia/commons/9/99/Wiktionary-logo-en-v2.svg"),
                ("Wikiversity_logo_2017.svg", "https://upload.wikimedia.org/wikipedia/commons/0/0b/Wikiversity_logo_2017.svg"),
                ("Wiki_letter_w_cropped.svg", "https://upload.wikimedia.org/wikipedia/commons/1/1c/Wiki_letter_w_cropped.svg"),
                ("Wikipedia-W-bold-in-square.svg", "https://upload.wikimedia.org/wikipedia/commons/5/5a/Wikipedia-W-bold-in-square.svg"),
                ("Wikipedia_wordmark.svg", "https://upload.wikimedia.org/wikipedia/commons/b/bb/Wikipedia_wordmark.svg"),
                ("Wiktionary_small.svg", "https://upload.wikimedia.org/wikipedia/commons/f/f9/Wiktionary_small.svg"),
                ("MediaWiki-2020-icon.svg", "https://upload.wikimedia.org/wikipedia/commons/a/a3/MediaWiki-2020-icon.svg"),
                ("Wikimedia_Foundation_logo.svg", "https://upload.wikimedia.org/wikipedia/commons/8/81/Wikimedia_Foundation_logo.svg"),
                ("Notification-icon-Wikipedia-logo.svg",
                 "https://upload.wikimedia.org/wikipedia/commons/6/6d/Notification-icon-Wikipedia-logo.svg"),
                ("WMF_logo.svg", "https://upload.wikimedia.org/wikipedia/commons/2/2e/WMF_logo.svg"),
                ("CommonsLogo.svg", "https://upload.wikimedia.org/wikipedia/commons/3/3c/CommonsLogo.svg")):
            with self.subTest(mark=name):
                refuse(f"File:{name} is a Wikimedia logo or mark", f'<img src="{url}" alt="">',
                       images={name: {**self.NO_CREDIT, "project": "commons", "licence": "CC BY-SA 3.0",
                                      "licence_id": "cc-by-sa-3.0",
                                      "restrictions": []}})
        # A name is a mark's only as a project's name, not a longer word, and a project word needs a
        # mark word beside it unless it is a project's own name.
        for name in ("Wikipedian_gadget_chart.svg", "Commons_widget_area.svg", "Widget_icon.svg", "Metawidget_logo.svg"):
            with self.subTest(not_a_mark=name):
                self.builds(body.replace(end, f'<img src="https://upload.wikimedia.org/wikipedia/commons/1/1a/{name}" alt="">' + end),
                            images={name: {**self.NO_CREDIT, "project": "commons", "licence": "CC0", "licence_id": "cc0",
                                           "restrictions": []}})
        ws = "https://upload.wikimedia.org/wikipedia/commons/4/4c/Wikisource-logo.svg"
        refuse("File:Wikisource-logo.svg is a Wikimedia logo or mark",
               f'<style>.mw-parser-output .ws a{{background:url("{ws}")}}</style>', images={})
        # A URL on an upload host that names no file is refused too.
        refuse("a <img> fetches 'https://upload.wikimedia.org/math/x.png', which is no file of Wikimedia Commons",
               '<img src="https://upload.wikimedia.org/math/x.png" alt="">', images={})
        refuse("a <style> fetches 'https://upload.wikimedia.org/x.svg', which is no file",
               '<style>.mw-parser-output .x{background:url("https://upload.wikimedia.org/x.svg")}</style>', images={})

    def test_more_hostile_cases_stay_refused(self):
        # Hostile cases this class did not yet hold by name, kept as named regressions (each was
        # refused already, and stays refused).
        body = self.body()
        end = "\n</div>"
        evil = "https://evil.example"
        cases = [
            (f'<meta http-equiv="refresh" content="0;url={evil}">', "a <meta> element is not allowed"),
            (f'<base href="{evil}/">', "a <base> element is not allowed"),
            ("<svg onload=alert(1)></svg>", "a <svg> element is not allowed"),
            (f'<link rel="stylesheet" href="{evil}/x.css">', "a <link> element is not allowed"),
            (f'<button formaction="{evil}/"></button>', "a <button> element is not allowed"),
            ('<a href="&#106;avascript:alert(1)"></a>', "a href must be an in-page fragment"),
            ('<a href="java\tscript:alert(1)"></a>', "a href must be an in-page fragment"),
            ('<a href="JAVASCRIPT:alert(1)"></a>', "a href must be an in-page fragment"),
            ('<a href=" javascript:alert(1)"></a>', "a href must be an in-page fragment"),
            ('<a href="data:text/html,x"></a>', "a href must be an in-page fragment"),
            ('<a href="/\\evil.example/"></a>', "a href must be an in-page fragment"),
            ('<math href="javascript:alert(1)"><mi></mi></math>', "a href must be an in-page fragment"),
            (f'<math><mi href="{evil}/"></mi></math>', "a href must be an in-page fragment"),
            (f'<math><style><form action={evil}/><button>go</button></form></style></math>', "a style element inside a formula"),
            (f'<math><style><link rel=stylesheet href={evil}/x.css></style></math>', "a style element inside a formula"),
            ('<math><mtext><table><mglyph><style><img src=x onerror=alert(1)></style></mglyph></table></mtext></math>',
             "a <table> inside a formula"),
            ('<math><semantics><mrow></mrow><annotation-xml encoding="text/html"><img src=x onerror=alert(1)></annotation-xml>'
             '</semantics></math>', "a <annotation-xml> element is not allowed"),
            ('<noscript><p title="</noscript><img src=x onerror=alert(1)>"></p></noscript>', "a <noscript> element is not allowed"),
            ('<noembed><p title="</noembed><img src=x onerror=alert(1)>"></p></noembed>', "a <noembed> element is not allowed"),
            ("<xmp><img src=x onerror=alert(1)></xmp>", "a <xmp> element is not allowed"),
            ("<template><img src=x onerror=alert(1)></template>", "a <template> element is not allowed"),
            ("<textarea><img src=x onerror=alert(1)></textarea>", "a <textarea> element is not allowed"),
            ("<title><img src=x onerror=alert(1)></title>", "a <title> element is not allowed"),
            ("<details open ontoggle=alert(1)></details>", "a <details> element is not allowed"),
            (f'<object data="{evil}/x"></object>', "a <object> element is not allowed"),
            (f'<embed src="{evil}/x">', "a <embed> element is not allowed"),
            (f'<math><mglyph src="{evil}/x.png"></mglyph></math>', "a <mglyph> element is not allowed"),
            ('<style>.mw-parser-output a{content:"</style><img src=x onerror=alert(1)>"}</style>', "a style string is never closed"),
            ('<img src="https://upload.wikimedia.org@evil.example/x.png" alt="">', "a <img> src must be https on"),
            ('<img src="https://upload.wikimedia.org\\@evil.example/x.png" alt="">', "a <img> src must be https on"),
            ('<img src="https://upload.wikimedia.org/x.png" srcset="https://upload.wikimedia.org/a.png 1x, '
             f'{evil}/b.png 2x" alt="">', "a srcset URL must be https on"),
            (f'<img src="https://upload.wikimedia.org/x.png" srcset="data:image/png;base64,AAAA 2x" alt="">', "a srcset URL must be"),
            (f'<a href="https://example.org/" ping="{evil}/p"></a>', "the ping attribute (on a <a>) is not allowed"),
            (f'<video poster="{evil}/p.png"></video>', "a <video> poster must be https on"),
            (f'<video><source src="{evil}/v.webm"></video>', "a <source> src must be https on"),
            (f'<video><track src="{evil}/t.vtt"></video>', "a <track> src must be https on"),
            (f'<audio src="{evil}/a.mp3"></audio>', "a <audio> src must be https on"),
            (f'<table background="{evil}/x.png"><tbody><tr><td></td></tr></tbody></table>', "the background attribute"),
            ('<span style="width:expression(alert(1))"></span>', "a style attribute holds"),
            ('<style>@layer x{.mw-parser-output .a{color:red}}</style>', "the at-rule '@layer x' is not allowed"),
            ('<style>@container (min-width:1px){.mw-parser-output .a{color:red}}</style>', "the at-rule '@container"),
            ("<style>@media screen{.wl-header{display:none}}</style>", "is not scoped to .mw-parser-output"),
            ("<style>:where(.mw-parser-output) .a{color:red}</style>", "is not scoped to .mw-parser-output"),
            ("<style>:root{--paper:red}</style>", "is not scoped to .mw-parser-output"),
        ]
        for extra, fragment in cases:
            with self.subTest(extra=extra):
                self.refuse_html(fragment, body.replace(end, extra + end))

    def test_css_that_fetches_escapes_its_scope_or_hides_from_the_check_is_refused(self):
        evil = "https://evil.example"
        cases = [
            # An escape spells a keyword or a function no pattern on the text would see.
            (f'@\\69mport "{evil}/x.css"', "a CSS escape outside a string"),
            (f".mw-parser-output a{{background:u\\72l({evil}/p.png)}}", "a CSS escape outside a string"),
            (".mw-parser-output\\,body{x:y}", "a CSS escape outside a string"),
            # The last statement needs no ';'.
            (f'@import "{evil}/x.css"', "a style element holds @import"),
            (".mw-parser-output a{x:y}@charset \"x\"", "stands outside any rule"),
            # A string that is a URL, and url() however it is spelled.
            (f'.mw-parser-output a{{background-image:image-set("{evil}/p.png" 1x)}}', "image-set()"),
            (f'.mw-parser-output a{{background:-webkit-image-set("{evil}/p.png" 1x)}}', "image-set()"),
            (f'.mw-parser-output a{{background:image("{evil}/p.png")}}', "image()"),
            (f".mw-parser-output a{{background:URL({evil}/p.png)}}", f"a style element fetches '{evil}/p.png'"),
            ('.mw-parser-output a{background:url("\\68ttps://evil.example/p.png")}',
             f"a style element fetches '{evil}/p.png'"),
            (".mw-parser-output a{content:attr(title)}", "attr()"),
            # The scope inside a pseudo-class or an attribute matches elements outside the article.
            (":not(.mw-parser-output){outline:5px solid red}", "is not scoped to .mw-parser-output"),
            ("html:has(.mw-parser-output) .atlas-notice{display:none}", "is not scoped to .mw-parser-output"),
            (":is(.mw-parser-output, body) .wl-header{display:none}", "is not scoped to .mw-parser-output"),
            ('[title=".mw-parser-output"] ~ *{display:none}', "is not scoped to .mw-parser-output"),
            (".mw-parser-output:is(*) ~ footer{display:none}", "is not scoped to .mw-parser-output"),
            ("@media print{body{display:none}}", "the selector 'body' is not scoped"),
            # CSS nesting reaches the article's siblings from inside a scoped rule.
            (".mw-parser-output{& + footer{display:none}}", "nested in another rule"),
            (".mw-parser-output{@media print{x:y}}", "nested in a rule"),
            # Strings and comments must end.
            ('.mw-parser-output a{content:"x}', "a style string is never closed"),
            (".mw-parser-output a{x:y}/*", "a style comment is never closed"),
        ]
        for css, fragment in cases:
            with self.subTest(css=css):
                self.assertTrue(any(fragment in problem for problem in build_store.reader_css_problems(css)),
                                build_store.reader_css_problems(css))
        # Strings and comments hold what they like; escapes in a string are the string's.
        good = ('.mw-parser-output .a::after{content:"\\a0 } { ;"}/* } { ; */.mw-parser-output .b{font-family:"x,y"}'
                '.mw-parser-output :is(.c, .d) > .e{x:y}.mw-parser-output .f:not(.g, .h){x:y}')
        self.assertEqual(build_store.reader_css_problems(good), [])
        for value, ok in (("color:red;padding:0 4px", True), ('font-family:"a;b"', True),
                          (f"background:u\\72l({evil}/p.png)", False), (f"background-image:image-set('{evil}' 1x)", False),
                          (f"background:URL({evil})", False), ("position:sticky", False), ("x:y}body{x:y", False)):
            with self.subTest(style=value):
                self.assertEqual(build_store.reader_style_attr_problems(value) == [], ok)
        # In the page: a style block that does not check out refuses the article.
        body = self.body()
        self.refuse_html("a CSS escape outside a string",
                         body.replace("\n</div>", f'<style>@\\69mport "{evil}/x.css"</style>\n</div>'))
        self.refuse_html("a CSS escape outside a string",
                         body.replace("\n</div>", f'<span style="background:u\\72l({evil}/p.png)"></span>\n</div>'))

    def test_a_passage_context_is_the_revisions_text_next_to_its_span(self):
        # With the reader html the build has proven the revision's text layer (its sha256 is the
        # records' layer_sha256), so a passage's context, which the page shows in grey as the same
        # revision's text, must be that layer's text right before or after the passage's span. Without
        # the reader html, only the context's shape is checked (SupplementTests).
        def text_entry(hex_: str, **context: str) -> tuple[str, dict]:
            text = self.TEXTS[hex_]
            sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
            entry = {"id": self.IDS[hex_], "pageid": 1001, "span_sha256": sha, "layer": text,
                     "segments": [{"text": text}]}
            for side, layer in context.items():
                entry[side] = {"layer": layer, "segments": [{"text": layer}]}
            return build_store.passage_key(1001, sha), entry

        def passage_text(*entries: tuple[str, dict]) -> Path:
            path = self.tmp / "passage-text.json"
            path.write_text(json.dumps({"schema": build_store.PASSAGE_TEXT_SCHEMA, "passages": dict(entries)}),
                            encoding="utf-8")
            return path

        reader = self.side_file(("1001", self.entry()))

        def build(path: Path, with_reader: bool = True) -> dict[str, bytes]:
            return build_store.build(self.set, supplements=build_store.Supplements.read(
                passage_text=path, reader_html=reader if with_reader else None))

        start = self.spans["3fa91c07"][0]
        self.assertTrue(self.layer[:start].endswith("For gadgets, see Gadget (mathematics). The widget lemma states that "))
        # The rest of the passage's sentence, and a context reaching back into the block before.
        tree = build(passage_text(text_entry("3fa91c07", before="The widget lemma states that ", after=" of minimal degree."),
                                  text_entry("91aa02de", before="Definitions In this article, ", after=".")))
        texts = {p["id"]: p.get("text") for p in json.loads(tree["atlas/v1/articles/1001.json"])["passages"]}
        self.assertEqual(texts[self.IDS["3fa91c07"]]["before"]["layer"], "The widget lemma states that ")
        self.assertEqual(texts[self.IDS["3fa91c07"]]["after"]["layer"], " of minimal degree.")
        self.assertEqual(texts[self.IDS["91aa02de"]]["before"]["layer"], "Definitions In this article, ")
        cases = [
            # One word off, with the record's quote prefix still where the context meets the passage.
            ("91aa02de", "before", "Notations In this article, "),
            ("3fa91c07", "before", "For widgets, see Gadget (mathematics). The widget lemma states that "),
            # The article's own text, from elsewhere in it (the bound's record quotes no prefix or suffix).
            ("5b0e44a1", "before", "The widget lemma states that "),
            ("5b0e44a1", "after", " of minimal degree."),
        ]
        for hex_, side, layer in cases:
            entry = text_entry(hex_, **{side: layer})
            with self.subTest(passage=hex_, side=side, context=layer):
                with self.assertRaises(build_store.AtlasError) as caught:
                    build(passage_text(entry))
                self.assertIn(f"--passage-text: passages[{entry[0]}]: {side}: is not the revision's text right {side} "
                              "the passage", str(caught.exception))
                # Without the reader html the build cannot see the layer: the shape alone is checked.
                plain = json.loads(build(passage_text(entry), with_reader=False)["atlas/v1/articles/1001.json"])
                self.assertEqual({p["id"]: p.get("text") for p in plain["passages"]}[self.IDS[hex_]][side]["layer"], layer)

    def test_a_url_holding_a_space_a_browser_keeps_is_refused(self):
        # HTML's srcset parser and CSS's url() skip ASCII whitespace alone. A browser keeps U+00A0,
        # U+3000 and the like inside the URL, which then resolves against the page (a request to the
        # site itself, which img-src 'self' allows), while Python's str.strip() or a Unicode \s would
        # drop the character and check a clean Wikimedia URL. Each such URL is refused by name.
        body, end = self.body(), "\n</div>"
        for space in (" ", "　", " ", "\u0085"):
            for markup, tag, name in (
                    (f'<img alt="" srcset=" {space}{self.COMMONS} 2x">', "img", "srcset"),
                    (f'<img alt="" srcset="{self.COMMONS}{space}2x">', "img", "srcset"),
                    (f'<img alt="" src="{self.COMMONS}{space}">', "img", "src"),
                    (f'<video poster="{space}{self.COMMONS_THUMB}"></video>', "video", "poster"),
                    (f'<a href="https://en.wikipedia.org/wiki/Widget{space}">x</a>', "a", "href"),
                    (f'<q cite="https://example.org/{space}x">x</q>', "q", "cite")):
                with self.subTest(space=f"U+{ord(space):04X}", markup=markup[:24]):
                    self.refuse_html(f"a <{tag}> {name} holds a space character other than ASCII whitespace",
                                     body.replace(end, markup + end))
            # In a TemplateStyles url(), CSS skips only its own whitespace: the URL checked is the URL
            # fetched, the character included, and it is refused for not being on the upload hosts.
            lock = "https://upload.wikimedia.org/wikipedia/commons/6/65/Lock-green.svg"
            for css in (f".mw-parser-output .lock a{{background:url({space}{lock})}}",
                        f".mw-parser-output .lock a{{background:url( {space}{lock} )}}"):
                with self.subTest(space=f"U+{ord(space):04X}", css=css[:40]):
                    media: list[str] = []
                    problems = build_store.reader_css_problems(css, media)
                    self.assertIn(f"a style element fetches {(space + lock)[:60]!r}: only https on", problems[0])
                    self.assertEqual(media, [])
        # ASCII whitespace still separates a srcset's candidates and descriptors, and pads a url().
        scan = build_store.ReaderScan({}, frozenset())
        scan.feed(f'<img alt="" srcset="{self.COMMONS} 1x,\t{self.COMMONS_THUMB}\n2x , \f{self.COMMONS}\r3x">')
        scan.finish()
        self.assertEqual(scan.problems(), [])
        self.assertEqual(scan.media, [("img", self.COMMONS), ("img", self.COMMONS_THUMB), ("img", self.COMMONS)])
        media = []
        self.assertEqual(build_store.reader_css_problems(
            ".mw-parser-output .lock a{background:url( \t\n" + self.COMMONS + "\f )}", media), [])
        self.assertEqual(media, [self.COMMONS])
        # A space spelled as an escape inside a quoted URL is the string's, and refused as before.
        self.assertIn(f"a style element fetches {chr(0xa0) + self.COMMONS[:59]!r}",
                      build_store.reader_css_problems(
                          '.mw-parser-output .x{background:url("\\a0 ' + self.COMMONS + '")}')[0])

    def test_an_entry_for_another_revision_or_of_another_shape_is_refused(self):
        cases = [
            ({"source_id": "enwiki-1001-2001"}, "source_id must be the article's 'enwiki-1001-2002'"),
            ({"revision": 2001}, "revision must be the article's 2002"),
            ({"html_sha256": "0" * 64}, "html_sha256 must be the article's"),
            ({"layer_sha256": "0" * 64}, "layer_sha256 must be the article's"),
            ({"renderer": "web-html/3"}, "renderer must be the records' 'web-html/1'"),
            ({"marks": 6}, "marks says 6; the html has 9"),
            ({"passages": 5}, "passages says 5; the html has 4"),
            ({"page": 1}, "unknown key 'page'"),
            ({"html": ""}, "html must be the article's body"),
            ({"html": 7}, "html must be the article's body"),
        ]
        for change, fragment in cases:
            with self.subTest(change=change):
                self.refuse_html(fragment, **change)
        with self.assertRaises(build_store.AtlasError) as caught:
            self.build_with(self.side_file(("1001", "not an object")))
        self.assertIn("an article is an object", str(caught.exception))
        with self.assertRaises(build_store.AtlasError) as caught:
            self.build_with(self.side_file(("9", self.entry())))
        self.assertIn("--reader-html: matches no article of this build", str(caught.exception))
        path = self.tmp / "articles-list.json"
        path.write_text(json.dumps({"schema": build_store.READER_HTML_SCHEMA, "articles": []}))
        with self.assertRaises(build_store.AtlasError) as caught:
            self.build_with(path)
        self.assertIn("articles must be an object of entries keyed by page id", str(caught.exception))
        with self.assertRaises(build_store.AtlasError) as caught:
            build_store.Supplements.read(reader_html=self.side_file(("1001", self.entry()), schema="x/1"))
        self.assertIn(f"schema must be {build_store.READER_HTML_SCHEMA!r}", str(caught.exception))

    def test_every_style_block_of_the_fixture_kind_is_scoped(self):
        # TemplateStyles qualify the scope (body.ns-0, html.skin-...) and combine siblings inside it.
        good = (".mw-parser-output .a{x:y}body.ns-0 .mw-parser-output .hatnote+.hatnote{x:y}"
                "html.skin-theme-clientpref-night .mw-parser-output .b:not(.c){x:y}"
                "@media screen and (min-width:640px){.mw-parser-output tr+tr>.navbox-list{x:y}}"
                ".mw-parser-output .d[class~=e]{x:y}.mw-parser-output li:nth-child(2n+1){x:y}"
                '.mw-parser-output .lock{background:url("https://upload.wikimedia.org/x.svg")}')
        self.assertEqual(build_store.reader_css_problems(good), [])
        self.assertEqual(len(build_store.reader_css_problems(".mw-parser-output+div{x:y}")), 1)
        self.assertEqual(len(build_store.reader_css_problems("{x:y}")), 1)

    def test_the_cli_merges_the_side_file_and_check_compares_with_it(self):
        path = self.side_file(("1001", self.entry()))
        out = self.tmp / "store"

        def run_main(*args: str) -> tuple[int, str]:
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                code = build_store.main(["--record-set", str(self.set), "--out", str(out), *args])
            return code, err.getvalue()
        self.assertEqual(run_main("--reader-html", str(path)), (0, ""))
        self.assertEqual(run_main("--reader-html", str(path), "--check"), (0, ""))
        self.assertTrue((out / "atlas/v1/reader/1001.html").is_file())
        code, err = run_main("--check")
        self.assertEqual(code, 1)
        self.assertIn("unexpected: atlas/v1/reader/1001.html", err)
        broken = self.side_file(("1001", self.entry(self.body().replace("minimal", "maximal"))))
        code, err = run_main("--reader-html", str(broken), "--replace")
        self.assertEqual(code, 2)
        self.assertIn("--reader-html: articles[1001]", err)
        self.assertTrue((out / "atlas/v1/reader/1001.html").is_file())

class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atlas-cli-"))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_main(self, *args: str) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = build_store.main(list(args))
        return code, err.getvalue()

    def test_write_check_and_refuse_overwrite(self):
        out = self.tmp / "store"
        self.assertEqual(self.run_main("--record-set", str(RECORDSET), "--out", str(out))[0], 0)
        self.assertEqual(self.run_main("--record-set", str(RECORDSET), "--out", str(out), "--check")[0], 0)
        code, err = self.run_main("--record-set", str(RECORDSET), "--out", str(out))
        self.assertEqual(code, 2)
        self.assertIn("not empty", err)
        target = out / "atlas/v1/articles/1001.json"
        target.write_bytes(target.read_bytes().replace(b"Widget", b"Wodget"))
        code, err = self.run_main("--record-set", str(RECORDSET), "--out", str(out), "--check")
        self.assertEqual(code, 1)
        self.assertIn("differs: atlas/v1/articles/1001.json", err)
        self.assertEqual(self.run_main("--record-set", str(RECORDSET), "--out", str(out), "--replace")[0], 0)
        self.assertEqual(self.run_main("--record-set", str(RECORDSET), "--out", str(out), "--check")[0], 0)

    def test_replace_refuses_a_directory_holding_more_than_a_store(self):
        out = self.tmp / "public"
        self.assertEqual(self.run_main("--record-set", str(RECORDSET), "--out", str(out / "store"))[0], 0)
        (out / "precious.txt").write_text("keep me\n")
        code, err = self.run_main("--record-set", str(RECORDSET), "--out", str(out), "--replace")
        self.assertEqual(code, 2)
        self.assertIn("holds more than an atlas store (precious.txt, store)", err)
        self.assertEqual((out / "precious.txt").read_text(), "keep me\n")
        self.assertTrue((out / "store/atlas/v1/current.json").is_file())
        (out / "store/atlas/v0").mkdir()
        code, err = self.run_main("--record-set", str(RECORDSET), "--out", str(out / "store"), "--replace")
        self.assertEqual(code, 2)
        self.assertIn("(atlas/v0)", err)

    def test_unknown_status_exits_2_not_1(self):
        broken = self.tmp / "set"
        shutil.copytree(RECORDSET, broken)
        path = broken / "records/enwiki-1003-2004.jsonl"
        path.write_text(path.read_text().replace('"status":"formalized"', '"status":"disputed"'))
        reseal(broken)
        code, err = self.run_main("--record-set", str(broken), "--out", str(STORE), "--check")
        self.assertEqual(code, 2)
        self.assertIn("a location record carries a derived status", err)

    def test_internal_error_exits_3(self):
        original = build_store.build
        build_store.build = lambda *args, **kwargs: {}["boom"]
        try:
            code, err = self.run_main("--record-set", str(RECORDSET), "--out", str(self.tmp / "o"))
        finally:
            build_store.build = original
        self.assertEqual(code, 3)
        self.assertIn("internal error, nothing was written (KeyError", err)
        self.assertFalse((self.tmp / "o").exists())

    def test_refused_set_exits_2_without_writing(self):
        broken = self.tmp / "set"
        shutil.copytree(RECORDSET, broken)
        (broken / "SEAL.json").unlink()
        out = self.tmp / "out"
        code, err = self.run_main("--record-set", str(broken), "--out", str(out))
        self.assertEqual(code, 2)
        self.assertIn("SEAL.json is missing", err)
        self.assertFalse(out.exists())

    def test_only_cpython_3_12_13_or_a_later_3_12_builds(self):
        # ReaderScan's rules were checked against that html.parser; 3.9's reads attributes by
        # another grammar, so the builder refuses any other interpreter before it reads anything.
        self.assertIsNone(build_store.interpreter_problem())    # the one running these tests (CI's 3.12)
        for version in ((3, 12, 13), (3, 12, 15), (3, 12, 40)):
            self.assertIsNone(build_store.interpreter_problem(version, "cpython"), version)
        for version, implementation in (((3, 9, 6), "cpython"), ((3, 12, 12), "cpython"), ((3, 12, 0), "cpython"),
                                        ((3, 11, 15), "cpython"), ((3, 13, 13), "cpython"), ((3, 14, 0), "cpython"),
                                        ((3, 12, 13), "pypy")):
            with self.subTest(version=version, implementation=implementation):
                problem = build_store.interpreter_problem(version, implementation)
                self.assertIn("build_store needs CPython 3.12.13 or a later 3.12", problem)
                self.assertIn(f"this is {implementation} {'.'.join(map(str, version))}", problem)
        original = build_store.interpreter_problem
        build_store.interpreter_problem = lambda: original((3, 9, 6), "cpython")
        try:
            code, err = self.run_main("--record-set", str(RECORDSET), "--out", str(self.tmp / "o"))
        finally:
            build_store.interpreter_problem = original
        self.assertEqual(code, 3)
        self.assertIn("atlas: build_store needs CPython 3.12.13 or a later 3.12", err)
        self.assertIn("nothing was written", err)
        self.assertFalse((self.tmp / "o").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
