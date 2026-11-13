#!/usr/bin/env python3
"""Tests for scripts/pipeline/records.py and sources.py: stage 5, the records of a passage.

New code, on a synthetic layer and a synthetic universe (WikiLean is public: no article
text, no real page, no real declaration). What is checked:

* the cut — the quote capped at twelve words and thirty-two characters of context,
  widened only until it is unique, and refused when it cannot be made unique; the span's
  sha256 over the characters as they stand, the trailing whitespace the quote was
  stripped of included;
* the three record shapes and their ends, the absence's one end per distinct
  ``(repo, commit)``, and the receipt sidecar whose name is the sha256 of its own bytes;
* the revision-free keys recomputed rather than copied, and a row that states another
  refused;
* decision B5: only a subject passage's records carry the article's QID, and not a
  record a prominence overturn reopened; a record of an unchallenged run carries it;
* every refusal the export reports passage by passage.

Run: python3 scripts/pipeline/test_records.py
"""
from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import keys  # noqa: E402
import links  # noqa: E402
import records  # noqa: E402
import sources  # noqa: E402
import vocab  # noqa: E402

# A synthetic layer: three passages joined by one space. The first and the third are the
# same sentence, so the first one's quote has to be widened before it is unique.
_PASSAGES = ["A widget is a gadget with a handle.",
             "Every finite widget admits a spanning gadget and two such gadgets are equal.",
             "A widget is a gadget with a handle."]
LAYER = " ".join(_PASSAGES)
FIRST = [0, len(_PASSAGES[0])]
SECOND = [FIRST[1] + 1, FIRST[1] + 1 + len(_PASSAGES[1])]

UNIVERSE = {
    "id": "u-test", "sha256": "22" * 32,
    "libraries": [
        {"id": "widgetlib", "repo": "https://github.com/example-org/widgetlib", "commit": "a" * 40},
        {"id": "init", "repo": "https://github.com/example-org/core", "commit": "b" * 40},
        {"id": "std", "repo": "https://github.com/example-org/core", "commit": "b" * 40},
    ],
}


def source(**over) -> dict:
    entry = {"source_id": "enwiki-1001-2002", "pageid": 1001, "revision": 2002,
             "title": "Widget lemma", "wikidata": "Q99999001", "html_sha256": "44" * 32,
             "layer_sha256": hashlib.sha256(LAYER.encode("utf-8")).hexdigest(),
             "renderer": "web-html/3", "assessment": None}
    entry.update(over)
    return sources.source_record(entry)


def location(byte: str, **over) -> dict:
    base = {"location_key": byte * 64, "relation": "exactMatch", "library": "widgetlib",
            "declaration": "WidgetLib.Widget", "module": "WidgetLib.Widget",
            "path": "WidgetLib/Widget.lean", "line": 4, "end_line": 9,
            "proof_status": "definition", "rank": 1, "confidence": "high",
            "challenge": "survived"}
    base.update(over)
    return base


def row(**over) -> dict:
    base = {"source_id": "enwiki-1001-2002", "item": "In-text #1a2b3c4d",
            "passage_kind": "definition", "span": list(SECOND), "section": ["Statement"],
            "prominence": "central", "challenge": "survived", "status": "formalized",
            "outcome": "located", "locations": [location("1")]}
    base.update(over)
    return base


def build(row_value: dict, *, status_rule: int = 3, layer: str = ""):
    cut = records.cut_passage(LAYER, row_value["span"])
    return records.passage_records(source(), row_value, cut, UNIVERSE,
                                   status_rule=status_rule, layer=layer)


class CutTests(unittest.TestCase):
    def test_a_unique_quote_needs_no_context(self):
        cut = records.cut_passage(LAYER, SECOND)
        self.assertEqual(cut.quote["prefix"], "")
        self.assertEqual(cut.quote["suffix"], "")
        self.assertEqual(len(cut.quote["exact"].split()), vocab.QUOTE_MAX_WORDS)

    def test_the_quote_is_the_passages_first_twelve_words(self):
        cut = records.cut_passage(LAYER, SECOND)
        self.assertEqual(cut.quote["exact"],
                         " ".join(LAYER[SECOND[0]:SECOND[1]].split()[:vocab.QUOTE_MAX_WORDS]))

    def test_a_repeated_quote_is_widened_until_it_is_unique(self):
        cut = records.cut_passage(LAYER, FIRST)
        self.assertTrue(cut.quote["prefix"] or cut.quote["suffix"])
        self.assertEqual(records.find_quote(LAYER, cut.quote["exact"], cut.quote["prefix"],
                                            cut.quote["suffix"]), [FIRST[0]])
        for name in ("prefix", "suffix"):
            self.assertLessEqual(len(cut.quote[name]), vocab.AFFIX_MAX_CHARS)

    def test_a_quote_that_cannot_be_made_unique_is_refused(self):
        word = "widget"
        layer = " ".join([word] * 40)
        with self.assertRaises(records.RecordProblem) as caught:
            records.cut_passage(layer, [0, len(word)])
        self.assertIn("occurs more than once", str(caught.exception))

    def test_the_span_sha_hashes_the_span_as_it_stands(self):
        cut = records.cut_passage(LAYER, SECOND)
        self.assertEqual(cut.span_sha256, keys.sha256_text(LAYER[SECOND[0]:SECOND[1]]))

    def test_trailing_whitespace_is_hashed_and_stripped_from_the_quote(self):
        layer = "A short passage ends here.  And another begins."
        cut = records.cut_passage(layer, [0, 27])      # the span keeps the two spaces
        self.assertEqual(cut.quote["exact"], "A short passage ends here.")
        self.assertEqual(cut.span_sha256, keys.sha256_text(layer[0:27]))

    def test_a_span_outside_the_layer_is_refused(self):
        for span in ([len(LAYER), len(LAYER) + 1], [0, len(LAYER) + 1], [5, 5], [-1, 4],
                     [0], "x", None):
            with self.subTest(span=span), self.assertRaises(records.RecordProblem):
                records.cut_passage(LAYER, span)

    def test_a_cut_past_the_caps_is_refused_when_a_record_is_built(self):
        long_quote = records.Cut({"exact": " ".join(f"w{n}" for n in range(13)),
                                  "prefix": "", "suffix": ""}, "0" * 64)
        with self.assertRaises(records.RecordProblem):
            long_quote.checked("In-text #1")
        wide = records.Cut({"exact": "a", "prefix": "p" * 33, "suffix": ""}, "0" * 64)
        with self.assertRaises(records.RecordProblem):
            wide.checked("In-text #1")


class LocationRecordTests(unittest.TestCase):
    def test_the_record_validates_and_carries_what_the_contract_asks(self):
        made, sidecars = build(row())
        self.assertEqual((len(made), sidecars), (1, []))
        self.assertEqual(links.validate_all(made), [])
        record = made[0]
        self.assertEqual(record["id"], "enwiki-1001-2002::In-text #1a2b3c4d")
        self.assertEqual(record["relation"], "exactMatch")
        self.assertEqual(record["status"], "formalized")
        self.assertEqual(record["kind"], "definition")
        ext = record["extensions"][vocab.LOCATE]
        self.assertEqual(set(ext), {*vocab.RECORD_EXT_KEYS_BUNDLE,
                                    *vocab.SHAPE_EXT_KEYS_BUNDLE["location"]})
        self.assertEqual(ext["section"], ["Statement"])
        self.assertEqual(ext["prominence"], "central")

    def test_the_subject_end_is_the_pinned_revision(self):
        record = build(row())[0][0]
        end = record["subject"][0]
        self.assertEqual(end["type"], "html")
        self.assertEqual(end["version"], "2002")
        self.assertEqual(end["uri"],
                         "https://en.wikipedia.org/w/index.php?curid=1001&oldid=2002")
        self.assertEqual(end["citation"]["id"], "enwiki-1001-2002")
        self.assertEqual(end["citation"]["container-title"], "Wikipedia")
        ext = end["extensions"][vocab.LOCATE]
        self.assertEqual(set(ext), {*vocab.SUBJECT_EXT_KEYS, *vocab.SUBJECT_EXT_OPTIONAL})
        self.assertEqual(ext["licence"], "CC-BY-SA-4.0")
        self.assertEqual(ext["span"], SECOND)

    def test_the_lean_end_names_the_declaration_and_its_source(self):
        end = build(row())[0][0]["object"][0]
        self.assertEqual((end["type"], end["role"]), ("lean", "key"))
        self.assertEqual(end["uri"], "https://github.com/example-org/widgetlib")
        self.assertEqual(end["version"], "a" * 40)
        self.assertEqual(end["location"], {"declaration": "WidgetLib.Widget",
                                           "module": "WidgetLib.Widget"})
        self.assertEqual(set(end["extensions"][vocab.LOCATE]),
                         {*vocab.LEAN_EXT_KEYS, "end_line"})

    def test_a_location_with_no_end_line_carries_none(self):
        record = build(row(locations=[location("1", end_line=None)]))[0][0]
        self.assertNotIn("end_line", record["object"][0]["extensions"][vocab.LOCATE])

    def test_the_prop_valued_definition_flag_is_written_only_when_set(self):
        made = build(row(status="statement_only",
                         locations=[location("1", statement=True)]))[0]
        self.assertIs(made[0]["object"][0]["extensions"][vocab.LOCATE]["statement"], True)
        self.assertNotIn("statement", build(row())[0][0]["object"][0]["extensions"][vocab.LOCATE])

    def test_locations_are_written_by_rank_then_library_then_declaration(self):
        made = build(row(locations=[
            location("2", rank=2, declaration="WidgetLib.B"),
            location("1", rank=1, declaration="WidgetLib.A")]))[0]
        self.assertEqual([r["object"][0]["location"]["declaration"] for r in made],
                         ["WidgetLib.A", "WidgetLib.B"])

    def test_a_location_carries_its_own_challenge_not_its_passages(self):
        record = build(row(challenge="survived",
                           locations=[location("1", challenge="downgraded")]))[0][0]
        self.assertEqual(record["extensions"][vocab.LOCATE]["challenge"], "downgraded")


class KeyTests(unittest.TestCase):
    def test_the_keys_are_recomputed_not_copied(self):
        record = build(row())[0][0]
        ext = record["extensions"][vocab.LOCATE]
        cut = records.cut_passage(LAYER, SECOND)
        passage = keys.passage_key(1001, cut.span_sha256)
        self.assertEqual(ext["passage_key"], passage)
        self.assertEqual(ext["pair_key"], keys.pair_key(
            passage, "https://github.com/example-org/widgetlib", "WidgetLib.Widget"))

    def test_a_row_that_states_another_passage_key_is_refused(self):
        with self.assertRaises(records.RecordProblem) as caught:
            build(row(passage_key="0" * 64))
        self.assertIn("passage_key", str(caught.exception))

    def test_under_status_rule_1_there_are_no_revision_free_keys(self):
        record = build(row(), status_rule=1)[0][0]
        ext = record["extensions"][vocab.LOCATE]
        self.assertNotIn("passage_key", ext)
        self.assertNotIn("pair_key", ext)


class AbsenceTests(unittest.TestCase):
    def absent_row(self, **over) -> dict:
        base = row(outcome="absent", status="not_formalized", locations=[],
                   absence={"challenge": "survived",
                            "receipt": {"queries": ["one", "two", "three"],
                                        "candidates": [{"library": "widgetlib",
                                                        "declaration": "WidgetLib.X",
                                                        "verdict": "rejected",
                                                        "reason": "it states something else"}]}})
        base.update(over)
        return base

    def test_an_absence_names_each_repository_once(self):
        made, sidecars = build(self.absent_row())
        self.assertEqual(len(made), 1)
        record = made[0]
        self.assertEqual(record["relation"], "noMatch")
        self.assertEqual(record["status"], "not_formalized")
        ends = record["object"]
        # three libraries, two of them one repository at one commit: two ends
        self.assertEqual([(end["uri"], end["version"]) for end in ends],
                         [("https://github.com/example-org/core", "b" * 40),
                          ("https://github.com/example-org/widgetlib", "a" * 40)])
        for end in ends:
            self.assertEqual(set(end), set(vocab.ABSENCE_END_KEYS))
        self.assertEqual(len(sidecars), 1)

    def test_the_receipt_is_named_by_the_sha256_of_its_own_bytes(self):
        made, sidecars = build(self.absent_row())
        digest, text = sidecars[0]
        self.assertEqual(digest, keys.sha256_text(text))
        reference = made[0]["extensions"][vocab.LOCATE]["receipt"]
        self.assertEqual(reference, {"sha256": digest, "queries": 3, "candidates": 1})

    def test_a_repeated_query_is_listed_once_and_counted_once(self):
        made, sidecars = build(self.absent_row(
            absence={"challenge": "survived",
                     "receipt": {"queries": ["one", "two", "one", "three"], "candidates": []}}))
        self.assertEqual(made[0]["extensions"][vocab.LOCATE]["receipt"]["queries"], 3)
        self.assertIn('"queries":["one","two","three"]', sidecars[0][1])

    def test_a_candidate_keeps_its_documented_keys_only(self):
        _, sidecars = build(self.absent_row(
            absence={"challenge": "survived",
                     "receipt": {"queries": ["a", "b", "c"],
                                 "candidates": [{"library": "widgetlib", "declaration": "X",
                                                 "verdict": "rejected", "reason": "no",
                                                 "secret": "should not travel"}]}}))
        self.assertNotIn("secret", sidecars[0][1])

    def test_an_absence_beside_a_related_match_keeps_both_records(self):
        made, _ = build(self.absent_row(locations=[location("9", relation="relatedMatch",
                                                            proof_status="proved")]))
        self.assertEqual([r["relation"] for r in made], ["relatedMatch", "noMatch"])

    def test_a_receipt_query_that_quotes_the_source_past_the_cap_is_refused(self):
        quoted = " ".join(LAYER[SECOND[0]:SECOND[1]].split()[:13])
        with self.assertRaises(records.RecordProblem) as caught:
            build(self.absent_row(
                absence={"challenge": "survived",
                         "receipt": {"queries": [quoted, "two", "three"], "candidates": []}}),
                layer=LAYER)
        self.assertIn("past the 12-word quote cap", str(caught.exception))


class UnresolvedTests(unittest.TestCase):
    def test_an_unresolved_record_has_no_object_and_no_status(self):
        made, _ = build(row(outcome="unresolved", locations=[], status=None))
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0]["object"], [])
        self.assertNotIn("status", made[0])
        self.assertNotIn("relation", made[0])
        self.assertEqual(links.validate_all(made), [])


class ArticleQidTests(unittest.TestCase):
    """Decision B5: the article's QID rides on its subject passages' records only."""

    def test_a_subject_passage_carries_it(self):
        record = build(row(prominence="subject"))[0][0]
        self.assertEqual(record["subject"][0]["identifiers"], {"wikidata": "Q99999001"})

    def test_no_other_prominence_carries_it(self):
        for prominence in ("central", "supporting"):
            with self.subTest(prominence=prominence):
                record = build(row(prominence=prominence))[0][0]
                self.assertNotIn("identifiers", record["subject"][0])

    def test_a_reopened_record_of_a_subject_passage_carries_none(self):
        # A prominence overturn to subject that no challenger judged under that rule.
        record = build(row(prominence="subject",
                           locations=[location("1", challenge="challenge_pending")]))[0][0]
        self.assertNotIn("identifiers", record["subject"][0])

    def test_a_record_of_an_unchallenged_run_carries_it(self):
        # tally.py --unchallenged stamps every record not_required, and the locate
        # worker's subject call stands without a challenger (Jack, 2026-11-13).
        record = build(row(prominence="subject",
                           locations=[location("1", challenge="not_required")]))[0][0]
        self.assertEqual(record["subject"][0]["identifiers"], {"wikidata": "Q99999001"})

    def test_a_wikidata_value_that_is_no_qid_is_refused(self):
        cut = records.cut_passage(LAYER, SECOND)
        with self.assertRaises(records.RecordProblem):
            records.passage_records(source(wikidata="not-a-qid"), row(prominence="subject"),
                                    cut, UNIVERSE, status_rule=3)


class CoverageTests(unittest.TestCase):
    def bundle_row(self, **over) -> dict:
        base = row(locations=[location("3", relation="hasPart", declaration="WidgetLib.A"),
                              location("4", relation="hasPart", rank=2,
                                       declaration="WidgetLib.B")],
                   coverage={"complete": True, "parts": ["3" * 64, "4" * 64],
                             "challenge": "survived", "relation": "exactMatch",
                             "conjuncts": [{"id": "c1", "text": "one", "part": "3" * 64},
                                           {"id": "c2", "text": "two", "part": "4" * 64}]})
        base.update(over)
        return base

    def test_every_record_of_the_passage_carries_the_same_bundle(self):
        made, _ = build(self.bundle_row())
        bundles = [r["extensions"][vocab.LOCATE]["coverage"] for r in made]
        self.assertEqual(len(bundles), 2)
        self.assertEqual(bundles[0], bundles[1])
        self.assertEqual(set(bundles[0]), set(vocab.COVERAGE_KEYS_BUNDLE))

    def test_the_bundle_key_is_recomputed(self):
        made, _ = build(self.bundle_row())
        ext = made[0]["extensions"][vocab.LOCATE]
        pairs = [r["extensions"][vocab.LOCATE]["pair_key"] for r in made]
        self.assertEqual(ext["coverage"]["bundle_key"],
                         keys.bundle_key(ext["passage_key"], pairs))

    def test_a_bundle_that_states_another_key_is_refused(self):
        with self.assertRaises(records.RecordProblem) as caught:
            build(self.bundle_row(coverage={**self.bundle_row()["coverage"],
                                            "bundle_key": "0" * 64}))
        self.assertIn("bundle_key", str(caught.exception))

    def test_a_part_that_is_not_a_has_part_location_is_refused(self):
        with self.assertRaises(records.RecordProblem):
            build(self.bundle_row(locations=[
                location("3", relation="hasPart", declaration="WidgetLib.A"),
                location("4", relation="narrowMatch", rank=2, declaration="WidgetLib.B")]))

    def test_under_status_rule_1_the_claim_is_three_keys(self):
        made, _ = build(row(locations=self.bundle_row()["locations"],
                            coverage={"complete": True, "parts": ["3" * 64, "4" * 64],
                                      "challenge": "survived"}), status_rule=1)
        self.assertEqual(set(made[0]["extensions"][vocab.LOCATE]["coverage"]),
                         set(vocab.COVERAGE_KEYS))


class RefusalTests(unittest.TestCase):
    def test_a_row_with_no_challenge_is_refused(self):
        with self.assertRaises(records.RecordProblem):
            build(row(challenge=None, locations=[location("1", challenge=None)]))

    def test_a_row_with_no_section_or_prominence_is_refused(self):
        with self.assertRaises(records.RecordProblem):
            build(row(section=None))
        with self.assertRaises(records.RecordProblem):
            build(row(prominence="headline"))

    def test_a_close_match_with_no_note_is_refused(self):
        with self.assertRaises(records.RecordProblem) as caught:
            build(row(locations=[location("1", relation="closeMatch")]))
        self.assertIn("names no convention difference", str(caught.exception))

    def test_a_location_with_no_module_path_or_line_is_refused(self):
        for over in ({"module": None}, {"path": None}, {"path": "/abs/X.lean"},
                     {"path": "../X.lean"}, {"path": "X.txt"}, {"line": 0}, {"line": "4"},
                     {"end_line": 1}):
            with self.subTest(over=over), self.assertRaises(records.RecordProblem):
                build(row(locations=[location("1", **over)]))

    def test_a_library_outside_the_universe_is_refused(self):
        with self.assertRaises(records.RecordProblem):
            build(row(locations=[location("1", library="nowhere")]))

    def test_more_than_five_locations_or_a_shared_rank_is_refused(self):
        many = [location(str(n), rank=n, declaration=f"WidgetLib.D{n}") for n in range(1, 7)]
        with self.assertRaises(records.RecordProblem):
            build(row(locations=many))
        with self.assertRaises(records.RecordProblem):
            build(row(locations=[location("1", rank=1, declaration="A"),
                                 location("2", rank=1, declaration="B")]))

    def test_a_note_that_quotes_the_source_past_the_cap_is_refused(self):
        quoted = " ".join(LAYER[SECOND[0]:SECOND[1]].split()[:13])
        with self.assertRaises(records.RecordProblem) as caught:
            build(row(locations=[location("1", relation="closeMatch", note=quoted)]),
                  layer=LAYER)
        self.assertIn("past the 12-word quote cap", str(caught.exception))

    def test_an_unknown_status_rule_is_refused(self):
        with self.assertRaises(records.RecordProblem):
            build(row(), status_rule=4)


class SourceTests(unittest.TestCase):
    def test_the_family_gives_the_permalink_the_container_and_the_licence(self):
        entry = source()
        self.assertEqual(entry["uri"],
                         "https://en.wikipedia.org/w/index.php?curid=1001&oldid=2002")
        self.assertEqual(entry["container_title"], "Wikipedia")
        self.assertEqual(entry["licence"], "CC-BY-SA-4.0")

    def test_a_source_id_that_disagrees_with_its_page_and_revision_is_refused(self):
        with self.assertRaises(sources.SourceProblem):
            source(source_id="enwiki-1001-9999")

    def test_an_unknown_family_is_refused(self):
        with self.assertRaises(sources.SourceProblem):
            sources.family_of("dewiki-1-2")

    def test_only_a_licence_class_that_admits_quoting_passes(self):
        enwiki = sources.FAMILIES["enwiki"]
        self.assertIsNone(sources.licence_problem(enwiki))
        closed = sources.Family(id="x", title="X", container_title="X",
                                licence_class="all_rights_reserved", spdx="",
                                revision_url="https://x/{pageid}/{revision}")
        self.assertIn("may be quoted", sources.licence_problem(closed))


if __name__ == "__main__":
    unittest.main(verbosity=2)
