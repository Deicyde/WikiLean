#!/usr/bin/env python3
"""Stage 9a: the sourceset's append-only manifest, on synthetic entries.

docs/PIPELINE.md sections 3 and 8. The batch-1 proof — the ``wpm3`` sourceset's
seven-version chain read back, every entry validated, the append-only rule checked at
every delta step, and the pin ``04abae03…`` that both sealed sets name reproduced — is
an operator run over data this repository does not hold; its result is in the report.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import sourceset  # noqa: E402
import vocab  # noqa: E402


def entry(pageid: int, revision: int, **over) -> dict:
    row = {"assessment": {"class": "Start", "importance": "low"}, "family": "enwiki",
           "fetch": None, "html_sha256": None, "layer_sha256": None,
           "licence_class": "open_share_alike", "origin": vocab.SOURCE_ORIGIN_ENUMERATED,
           "pageid": pageid, "raw_sha256": None, "renderer": None, "revision": revision,
           "source_id": f"enwiki-{pageid}-{revision}", "title": f"Page {pageid}",
           "wikidata": None}
    row.update(over)
    return row


def rendered(pageid: int, revision: int, **over) -> dict:
    row = {"html_sha256": "a" * 64, "layer_sha256": "b" * 64, "raw_sha256": "c" * 64,
           "renderer": vocab.RENDERER_WEB_HTML,
           "fetch": {"api": "action=parse", "params_sha256": "d" * 64},
           "flags": {"biography": False, "disambiguation": False, "list": False,
                     "redirect": False}}
    row.update(over)
    return entry(pageid, revision, **row)


class SourcesetCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "wpm-test"
        self.ss = sourceset.Sourceset.create(self.dir, "wpm-test", "enwiki",
                                             created="2026-01-01T00:00:00Z")
        self.addCleanup(self._tmp.cleanup)


class TestCreation(SourcesetCase):
    def test_version_one_is_an_empty_snapshot(self):
        manifest = self.ss.latest()
        self.assertEqual(manifest.version, 1)
        self.assertEqual(len(manifest), 0)
        self.assertFalse(manifest.is_delta)
        self.assertIsNone(manifest.header["previous_sha256"])
        self.assertEqual(manifest.pin()["id"], "wpm-test")

    def test_a_second_create_is_refused(self):
        with self.assertRaises(sourceset.SourcesetError):
            sourceset.Sourceset.create(self.dir, "wpm-test", "enwiki")

    def test_an_id_a_seal_could_not_name_is_refused_where_it_is_chosen(self):
        for bad in ("", "a b", "x" * 200, "/etc", None):
            with self.subTest(bad=bad):
                with self.assertRaises(sourceset.SourcesetError):
                    sourceset.check_new_id(bad)
        self.assertEqual(sourceset.check_new_id("wpm-3"), "wpm-3")

    def test_an_unknown_family_is_refused(self):
        with self.assertRaises(Exception):
            sourceset.Sourceset.create(self.dir.parent / "other", "other", "frwiki")

    def test_state_is_written(self):
        state = json.loads((self.dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["schema"], sourceset.STATE_SCHEMA)
        self.assertEqual(state["manifest"]["version"], 1)
        self.assertEqual(state["counts"]["sources"], 0)
        self.assertEqual(state["commands"]["create"]["version"], 1)


class TestAppendOnly(SourcesetCase):
    def test_a_version_chains_to_the_one_before(self):
        one = self.ss.latest()
        entries = {e["source_id"]: e for e in (entry(1, 11), entry(2, 22))}
        two = self.ss.write_version(entries, created="2026-01-02T00:00:00Z")
        self.assertEqual(two.version, 2)
        self.assertEqual(two.header["previous_sha256"], one.sha256)
        self.assertEqual(len(two), 2)
        self.assertEqual(sorted(two.entries), ["enwiki-1-11", "enwiki-2-22"])

    def test_a_null_field_may_be_filled_in_and_a_recorded_one_may_not(self):
        entries = {e["source_id"]: e for e in (entry(1, 11), entry(2, 22))}
        self.ss.write_version(entries)
        filled = dict(entries)
        filled["enwiki-1-11"] = rendered(1, 11)
        three = self.ss.write_version(filled)
        self.assertEqual(three.get("enwiki-1-11")["renderer"], vocab.RENDERER_WEB_HTML)
        changed = dict(filled)
        changed["enwiki-1-11"] = rendered(1, 11, layer_sha256="e" * 64)
        with self.assertRaises(sourceset.SourcesetError) as caught:
            self.ss.write_version(changed)
        self.assertIn("layer_sha256", str(caught.exception))
        self.assertIn("never changes", str(caught.exception))

    def test_a_dropped_source_is_refused(self):
        entries = {e["source_id"]: e for e in (entry(1, 11), entry(2, 22))}
        self.ss.write_version(entries)
        with self.assertRaises(sourceset.SourcesetError) as caught:
            self.ss.write_version({"enwiki-1-11": entries["enwiki-1-11"]})
        self.assertIn("never drops a source", str(caught.exception))

    def test_a_determined_flag_never_changes_and_a_null_one_may_be_determined(self):
        base = rendered(1, 11)
        base["flags"] = {"biography": None, "disambiguation": False, "list": False,
                         "redirect": False}
        self.ss.write_version({base["source_id"]: base})
        later = dict(base, flags=dict(base["flags"], biography=True))
        self.assertEqual(len(self.ss.write_version({base["source_id"]: later})), 1)
        wrong = dict(later, flags=dict(later["flags"], redirect=True))
        with self.assertRaises(sourceset.SourcesetError) as caught:
            self.ss.write_version({base["source_id"]: wrong})
        self.assertIn("flag redirect", str(caught.exception))

    def test_a_version_that_says_nothing_new_is_refused(self):
        entries = {e["source_id"]: e for e in (entry(1, 11),)}
        self.ss.write_version(entries)
        with self.assertRaises(sourceset.SourcesetError) as caught:
            self.ss.write_version(entries)
        self.assertIn("written once", str(caught.exception))

    def test_a_version_is_never_rewritten(self):
        entries = {e["source_id"]: e for e in (entry(1, 11),)}
        self.ss.write_version(entries)
        with self.assertRaises(sourceset.SourcesetError):
            sourceset._write_new(self.ss.manifest_path(2), "x\n")


class TestDeltas(SourcesetCase):
    def seed(self, count: int = 10) -> dict:
        entries = {e["source_id"]: e for e in (entry(n, 100 + n) for n in range(1, count + 1))}
        self.ss.write_version(entries)
        return entries

    def test_a_small_change_is_a_delta_and_replays_onto_the_snapshot(self):
        entries = self.seed()
        entries["enwiki-1-101"] = rendered(1, 101)
        three = self.ss.write_version(entries)
        self.assertTrue(three.is_delta)
        self.assertEqual(three.chain, 1)
        self.assertEqual(len(three), 10)
        self.assertEqual(sourceset.split_lines(
            self.ss.manifest_path(3).read_text(encoding="utf-8")).__len__(), 2)
        self.assertEqual(three.header["changed"], 1)
        self.assertEqual(three.get("enwiki-2-102"), entries["enwiki-2-102"])

    def test_a_big_change_is_a_snapshot(self):
        entries = self.seed()
        for n in range(1, 9):
            entries[f"enwiki-{n}-{100 + n}"] = rendered(n, 100 + n)
        three = self.ss.write_version(entries)
        self.assertFalse(three.is_delta)
        self.assertEqual(three.chain, 0)

    def test_a_rewritten_earlier_version_is_caught_by_the_chain(self):
        entries = self.seed()
        entries["enwiki-1-101"] = rendered(1, 101)
        self.ss.write_version(entries)
        path = self.ss.manifest_path(2)
        lines = sourceset.split_lines(path.read_text(encoding="utf-8"))
        header = json.loads(lines[0])
        header["created"] = "2026-06-06T06:06:06Z"
        path.write_text("\n".join([sourceset.canonical_line(header)] + lines[1:]) + "\n",
                        encoding="utf-8")
        with self.assertRaises(sourceset.SourcesetError) as caught:
            self.ss.load(3)
        self.assertIn("previous_sha256", str(caught.exception))

    def test_a_delta_that_breaks_the_rule_is_refused_when_read(self):
        entries = self.seed()
        entries["enwiki-1-101"] = rendered(1, 101)
        self.ss.write_version(entries)
        path = self.ss.manifest_path(3)
        lines = sourceset.split_lines(path.read_text(encoding="utf-8"))
        row = json.loads(lines[1])
        row["title"] = "Another title"
        path.write_text("\n".join([lines[0], sourceset.canonical_line(row)]) + "\n",
                        encoding="utf-8")
        with self.assertRaises(sourceset.SourcesetError) as caught:
            self.ss.load(3)
        self.assertIn("title", str(caught.exception))

    def test_a_delta_that_repeats_an_entry_unchanged_is_refused(self):
        entries = self.seed()
        entries["enwiki-1-101"] = rendered(1, 101)
        self.ss.write_version(entries)
        path = self.ss.manifest_path(3)
        lines = sourceset.split_lines(path.read_text(encoding="utf-8"))
        header = json.loads(lines[0])
        header["changed"] = 2
        extra = sourceset.canonical_line(entries["enwiki-2-102"])
        path.write_text("\n".join([sourceset.canonical_line(header), lines[1], extra]) + "\n",
                        encoding="utf-8")
        with self.assertRaises(sourceset.SourcesetError) as caught:
            self.ss.load(3)
        self.assertIn("unchanged", str(caught.exception))

    def test_version_one_cannot_be_a_delta(self):
        path = self.ss.manifest_path(1)
        header = json.loads(sourceset.split_lines(path.read_text(encoding="utf-8"))[0])
        header["schema"] = sourceset.DELTA_SCHEMA
        path.write_text(sourceset.canonical_line(header) + "\n", encoding="utf-8")
        with self.assertRaises(sourceset.SourcesetError):
            self.ss.load(1)


class TestEntries(unittest.TestCase):
    def test_a_good_entry_has_no_problems(self):
        self.assertEqual(sourceset.entry_problems(entry(1, 2)), [])
        self.assertEqual(sourceset.entry_problems(rendered(1, 2)), [])

    def test_the_refusals(self):
        cases = {
            "source_id": dict(source_id="enwiki-9-9"),
            "licence_class": dict(licence_class="all_rights_reserved"),
            "origin": dict(origin="guessed"),
            "wikidata": dict(wikidata="P31"),
            "html_sha256": dict(html_sha256="nope"),
            "renderer": dict(renderer="a renderer"),
            "flag redirect": dict(flags={"redirect": "yes"}),
            "unknown key(s)": dict(extra=1),
        }
        for want, over in cases.items():
            with self.subTest(want=want):
                problems = sourceset.entry_problems(entry(1, 2, **over))
                self.assertTrue(problems, want)
                self.assertTrue(any(want in p for p in problems), f"{want}: {problems}")

    def test_a_layer_and_its_renderer_are_recorded_together(self):
        problems = sourceset.entry_problems(entry(1, 2, layer_sha256="b" * 64))
        self.assertTrue(any("recorded together" in p for p in problems))

    def test_adopted_and_the_skip_reasons(self):
        self.assertFalse(sourceset.is_adopted(entry(1, 2)))
        self.assertTrue(sourceset.is_adopted(entry(1, 2, origin=vocab.SOURCE_ORIGIN_ADOPTED)))
        self.assertTrue(sourceset.is_adopted(entry(1, 2, adopted_from={"consumer": "w"})))
        self.assertEqual(sourceset.skip_reason(entry(1, 2)), vocab.SKIP_NOT_RENDERED)
        self.assertIsNone(sourceset.skip_reason(rendered(1, 2)))
        flagged = rendered(1, 2)
        flagged["flags"] = dict(flagged["flags"], redirect=True)
        self.assertEqual(sourceset.skip_reason(flagged),
                         f"{vocab.SKIP_NO_CONCEPT} (redirect)")


class TestPreferredAndFiles(SourcesetCase):
    def test_an_adopted_revision_wins_over_an_enumerated_one_of_the_same_page(self):
        rows = [entry(1, 500), rendered(1, 100, origin=vocab.SOURCE_ORIGIN_ADOPTED),
                entry(2, 7)]
        manifest = self.ss.write_version({r["source_id"]: r for r in rows})
        self.assertEqual(manifest.preferred(), {1: "enwiki-1-100", 2: "enwiki-2-7"})

    def test_the_highest_enumerated_revision_otherwise(self):
        rows = [entry(1, 500), entry(1, 100)]
        manifest = self.ss.write_version({r["source_id"]: r for r in rows})
        self.assertEqual(manifest.preferred(), {1: "enwiki-1-500"})

    def test_a_source_whose_files_disagree_with_its_entry_is_named(self):
        row = rendered(1, 2)
        self.ss.write_version({row["source_id"]: row})
        self.assertEqual(sorted(self.ss.source_problems(row)),
                         ["body.html is missing, and the entry records its sha",
                          "layer.txt is missing, and the entry records its sha",
                          "source.raw.html is missing, and the entry records its sha"])
        directory = self.ss.source_dir(row["source_id"])
        directory.mkdir(parents=True)
        (directory / "body.html").write_text("hello", encoding="utf-8")
        problems = self.ss.source_problems(row)
        self.assertTrue(any(p.startswith("body.html is ") and "…" in p for p in problems))

    def test_counts_are_of_pages_not_sources(self):
        rows = [entry(1, 500), entry(1, 100), rendered(2, 7)]
        manifest = self.ss.write_version({r["source_id"]: r for r in rows})
        counts = manifest.counts()
        self.assertEqual(counts["sources"], 3)
        self.assertEqual(counts["pages"], 2)
        self.assertEqual(counts["rendered"], 1)
        self.assertEqual(sum(counts["by_class"].values()), counts["pages"])


class TestCanonicalLine(unittest.TestCase):
    def test_sorted_keys_no_spaces_and_the_line_breakers_escaped(self):
        self.assertEqual(sourceset.canonical_line({"b": 1, "a": "x"}), '{"a":"x","b":1}')
        for raw in ("\u0085", "\u2028", "\u2029"):
            line = sourceset.canonical_line({"a": raw})
            self.assertNotIn(raw, line)
            self.assertEqual(json.loads(line)["a"], raw)

    def test_non_ascii_is_written_as_itself(self):
        self.assertEqual(sourceset.canonical_line({"a": "é"}), '{"a":"é"}')

    def test_split_lines_splits_on_lf_only(self):
        self.assertEqual(sourceset.split_lines("a\u2028b\n"), ["a\u2028b"])
        self.assertEqual(sourceset.split_lines("a b\n"), ["a b"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
