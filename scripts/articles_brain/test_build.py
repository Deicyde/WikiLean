#!/usr/bin/env python3
"""Offline tests of the build: the section 13 rules, determinism, and fold compatibility.

Every fixture is synthetic (scripts/articles_brain/fixtures.py): no real article text is
in this repository, which is public.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "brain"))  # the fold imports its siblings flatly

from scripts.articles_brain import fixtures  # noqa: E402
from scripts.articles_brain.build import (  # noqa: E402
    EVIDENCE_KEY, BuildError, build, valid_declaration,
)
from scripts.articles_brain.canon import read_json  # noqa: E402
from scripts.articles_brain.fetch import fetch  # noqa: E402

#: brain/fold_proposals.main's row-type inference, verbatim. The function is local to
#: main(), so it cannot be imported; test_the_folds_type_inference_is_the_one_we_target
#: pins this copy against the fold's own source.
RTYPE_SOURCE = """\
    def rtype(r: dict) -> str:
        if r.get("action"):
            return r["action"]
        if r.get("path") and not r.get("decl"):
            return "container"
        return "discover"
"""


def rtype(row: dict) -> str:
    if row.get("action"):
        return row["action"]
    if row.get("path") and not row.get("decl"):
        return "container"
    return "discover"


class Sleeper:
    def __call__(self, seconds: float) -> None:
        pass


def acquire(tmp: str, pages=None) -> str:
    inputs = os.path.join(tmp, "inputs")
    os.makedirs(inputs, exist_ok=True)
    fetch(inputs, None, fixtures.FakeOpener(pages), site=fixtures.SITE, delay=0.0,
          sleep=Sleeper())
    return inputs


def text(*parts: str) -> str:
    with open(os.path.join(*parts), encoding="utf-8") as handle:
        return handle.read()


def rows_of(target: str, name: str) -> list[dict]:
    return [json.loads(line) for line in text(target, name).splitlines() if line.strip()]


def run_build(tmp: str, pages=None, include_author: bool = False) -> tuple[str, dict, list, list]:
    inputs = acquire(tmp, pages)
    target, manifest = build(inputs, os.path.join(tmp, "out"), include_author)
    return target, manifest, rows_of(target, "proposals.jsonl"), rows_of(target, "rejected.jsonl")


def by_decl(rows: list[dict]) -> dict[str, dict]:
    return {row["decl"]: row for row in rows}


class RuleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target, self.manifest, self.rows, self.rejected = run_build(self.tmp.name)
        self.rows_by_decl = by_decl(self.rows)
        self.reasons = {row["reason"] for row in self.rejected}

    # ---- rule 1: formalizes ------------------------------------------------

    def test_a_subject_passage_formalized_in_mathlib_gives_one_formalizes_row(self):
        row = self.rows_by_decl["Widget.lemma_one"]
        self.assertIsNone(row.get("action"))          # the fold reads it as `discover`
        self.assertEqual(rtype(row), "discover")
        self.assertEqual(row["qid"], "Q101")
        self.assertEqual(row["qid_label"], "Widget lemma")
        self.assertEqual(row["match_kind"], "exact")
        self.assertEqual(row["module"], "Mathlib.Widget.Basic")
        self.assertNotIn("kind", row)                 # the discover branch mints formalizes

    def test_a_formalized_broad_match_is_a_formalizes_row_that_does_not_fuse(self):
        # SCHEMA.md rule 1 fuses concept and decl on `match_kind == exact` only; the
        # store's own relation token rides along instead, and no Brain rule reads it.
        row = self.rows_by_decl["Widget.general"]
        self.assertIsNone(row.get("action"))
        self.assertEqual(row["match_kind"], "broadMatch")

    def test_the_formalizes_rows_are_the_subject_passages_mathlib_statements(self):
        formalizes = [r for r in self.rows if r.get("action") is None]
        self.assertEqual(sorted(r["decl"] for r in formalizes),
                         ["Widget.close", "Widget.general", "Widget.lemma_one"])

    def test_a_non_subject_passage_gives_nothing(self):
        self.assertNotIn("Widget.helper", self.rows_by_decl)
        self.assertIn("not_subject_passage", self.reasons)

    def test_an_article_with_no_qid_gives_nothing(self):
        self.assertNotIn("Widget.lemma_three", self.rows_by_decl)
        self.assertIn("article_has_no_qid", self.reasons)
        self.assertTrue(all(row["qid"] != "Q102" for row in self.rows))

    def test_a_location_the_store_does_not_count_formalizes_nothing(self):
        # A hasPart location gives the passage `partial`, by the store's own rule.
        self.assertNotIn("Widget.part", self.rows_by_decl)
        self.assertIn("location_does_not_formalize", self.reasons)

    def test_formalizes_is_many_to_many(self):
        # A second Mathlib declaration of the same passage is a second row, with its
        # own match_kind; brain/SCHEMA.md makes formalizes many-to-many by construction.
        self.assertEqual(self.rows_by_decl["Widget.close"]["match_kind"], "closeMatch")
        self.assertEqual(self.rows_by_decl["Widget.close"]["qid"], "Q101")

    def test_a_library_outside_the_channels_is_rejected_whatever_it_states(self):
        self.assertNotIn("Widget.core", self.rows_by_decl)
        self.assertIn("no_brain_channel_for_library", self.reasons)

    # ---- rule 2: non-Mathlib libraries are mentions-only -------------------

    def test_a_non_mathlib_library_gives_a_mentions_row(self):
        row = self.rows_by_decl["TauCeti.Widget.one"]
        self.assertEqual(rtype(row), "repo_link")
        self.assertEqual(row["repo"], "tauceti")
        self.assertEqual(row["kind"], "mentions")
        self.assertEqual(row["qid"], "Q101")

    def test_no_row_outside_mathlib_is_ever_a_formalizes_row(self):
        for row in self.rows:
            if row.get(EVIDENCE_KEY, {}).get("brain_lib") != "Mathlib":
                self.assertEqual(row["kind"], "mentions")

    def test_a_library_with_no_frontier_harvest_is_rejected(self):
        rejected = [r for r in self.rejected if r["reason"] == "no_brain_channel_for_library"]
        self.assertEqual({r["brain_lib"] for r in rejected}, {"LeanCore"})

    # ---- rule 3: community tags count only when accepted -------------------

    def test_a_live_community_tag_gives_a_mentions_row(self):
        row = self.rows_by_decl["TauCeti.Widget.tagged"]
        self.assertEqual(row["repo"], "tauceti")
        self.assertEqual(row["kind"], "mentions")
        self.assertEqual(row[EVIDENCE_KEY]["source"], "overlay")
        self.assertEqual(row[EVIDENCE_KEY]["overlay_version"], 3)
        self.assertEqual(row[EVIDENCE_KEY]["actor_type"], "human")
        self.assertEqual(row["confidence"], "high")

    def test_a_withdrawn_tag_produces_nothing(self):
        self.assertNotIn("TauCeti.Widget.withdrawn", self.rows_by_decl)
        withdrawn = [r for r in self.rejected if r["reason"] == "withdrawn_by_the_overlay"]
        self.assertEqual({r["pair_key"] for r in withdrawn},
                         {fixtures.K_TAG_REMOVED, fixtures.K_MATHLIB_TWO})

    def test_a_community_removal_withdraws_the_pipelines_own_location(self):
        # Passage E is sealed `formalized` on an exactMatch basis; a community removal at
        # that pair key takes the location out of the merge, so no formalizes row.
        self.assertNotIn("Widget.lemma_two", self.rows_by_decl)

    def test_a_mathlib_community_tag_never_mints_an_identity_claim(self):
        self.assertNotIn("Widget.community", self.rows_by_decl)
        self.assertIn("mathlib_community_tag_has_no_mentions_channel", self.reasons)

    def test_a_tag_on_an_unchecked_statement_still_mentions(self):
        row = self.rows_by_decl["TauCeti.Widget.unchecked"]
        self.assertEqual(row["kind"], "mentions")
        self.assertEqual(row["confidence"], "medium")  # an agent wrote it
        self.assertEqual(row[EVIDENCE_KEY]["actor_type"], "ai")

    def test_a_sealed_related_match_is_never_a_row(self):
        self.assertNotIn("Widget.unrelated", self.rows_by_decl)
        self.assertIn("relation_related_match", self.reasons)

    def test_a_tag_whose_passage_is_not_in_the_article_is_rejected(self):
        self.assertNotIn("TauCeti.Widget.orphan", self.rows_by_decl)
        self.assertIn("tag_passage_not_in_the_article", self.reasons)

    # ---- evidence and credit ----------------------------------------------

    def test_every_row_cites_its_evidence_by_pointer(self):
        store = read_json(os.path.join(self.tmp.name, "inputs", "PINS.json"))
        store_sha = store["inputs"]["current"]["sha256"]
        for row in self.rows:
            found = row[EVIDENCE_KEY]
            self.assertEqual(found["store"], store_sha)
            for key in ("pageid", "passage_key", "pair_key", "actor_type", "source"):
                self.assertIn(key, found)
            self.assertIn(found["actor_type"], ("human", "ai"))
            if found["source"] == "record_set":
                self.assertIn("record_set", found)
            else:
                self.assertIn("overlay_version", found)
            self.assertTrue(row["evidence"].startswith("WikiLean articles plane: "))

    def test_no_user_identity_is_carried_by_default(self):
        blob = text(self.target, "proposals.jsonl") + text(self.target, "rejected.jsonl")
        self.assertNotIn("user-1", blob)
        self.assertNotIn("A Person", blob)
        self.assertFalse(self.manifest["options"]["include_author_identity"])

    def test_the_author_option_is_off_but_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            _target, manifest, rows, _rejected = run_build(tmp, include_author=True)
            tagged = by_decl(rows)["TauCeti.Widget.tagged"]
            self.assertEqual(tagged[EVIDENCE_KEY]["added_by"], "user-1")
            self.assertTrue(manifest["options"]["include_author_identity"])

    def test_no_passage_text_or_note_is_copied(self):
        blob = text(self.target, "proposals.jsonl") + text(self.target, "rejected.jsonl")
        self.assertNotIn("quote", blob)
        self.assertNotIn("\"note\"", blob)
        self.assertNotIn("signature", blob)

    # ---- counts and the audit trail ---------------------------------------

    def test_the_manifest_counts_what_the_export_holds(self):
        counts = self.manifest["counts"]
        self.assertEqual(counts["articles"], 3)
        self.assertEqual(counts["rows"], len(self.rows))
        self.assertEqual(counts["rows_by_kind"], {"formalizes": 3, "mentions": 3})
        self.assertEqual(counts["rows_by_repo"], {"tauceti": 3})
        self.assertEqual(counts["rejections"], sum(r["count"] for r in self.rejected))
        self.assertEqual(sum(counts["rejections_by_reason"].values()), counts["rejections"])

    def test_every_rejection_says_why_and_where(self):
        for row in self.rejected:
            self.assertTrue(row["reason"])
            self.assertIn(row["kind"], ("location", "tag"))
            self.assertIn("pageid", row)
            self.assertTrue(row["id"].startswith(row["kind"] + ":"))

    def test_the_declaration_shape_check_is_the_overlays(self):
        self.assertTrue(valid_declaration("Nat.add_comm"))
        self.assertFalse(valid_declaration("Nat..add"))
        self.assertFalse(valid_declaration("Nat add"))
        self.assertFalse(valid_declaration("Nat.add​"))
        self.assertFalse(valid_declaration("x" * 241))


class CommunityRelabellingTest(unittest.TestCase):
    """A community row at a sealed Mathlib location's pair key may relabel it, and the
    relation IS the identity claim (SCHEMA.md rule 1 fuses on `match_kind == exact`).
    One editor's or one agent's relabelling must never mint or strengthen a
    `formalizes` edge, and no row may cite the record set for a relation it does not
    state."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        pages = fixtures.site_bodies()
        pages["/api/articles/overlay/101"] = fixtures.body(fixtures.relabelled_overlay())
        _t, _m, self.rows, self.rejected = run_build(self.tmp.name, pages)
        self.rows_by_decl = by_decl(self.rows)
        self.reasons = {row["reason"] for row in self.rejected}

    def test_an_upgraded_close_match_never_becomes_an_exact_identity_claim(self):
        self.assertNotIn("Widget.close", self.rows_by_decl)
        self.assertIn("community_relation_change_on_a_mathlib_location", self.reasons)

    def test_a_relabelled_related_match_never_becomes_a_formalizes_row(self):
        self.assertNotIn("Widget.unrelated", self.rows_by_decl)

    def test_no_row_claims_a_match_kind_the_record_set_does_not_state(self):
        for row in self.rows:
            found = row[EVIDENCE_KEY]
            if found["source"] != "record_set":
                continue
            self.assertEqual(found.get("base_relation", found["relation"]),
                             found["relation"], row["decl"])

    def test_a_note_only_change_leaves_the_sealed_row_standing(self):
        row = self.rows_by_decl["Widget.lemma_one"]
        self.assertEqual(row["match_kind"], "exact")
        self.assertEqual(row[EVIDENCE_KEY]["actor_type"], "human")
        # The merge records the sealed relation whenever a live row changed anything;
        # here it is the relation the row already carries, which is the whole point.
        self.assertEqual(row[EVIDENCE_KEY]["base_relation"], "exactMatch")

    def test_the_relabelling_is_recorded_where_it_is_rejected(self):
        relabelled = [r for r in self.rejected
                      if r["reason"] == "community_relation_change_on_a_mathlib_location"]
        self.assertEqual({r["pair_key"] for r in relabelled},
                         {fixtures.K_MATHLIB_CLOSE, fixtures.K_MATHLIB_RELATED})
        for row in relabelled:
            self.assertEqual(row["relation"], "exactMatch")           # what was written
            self.assertIn(row["base_relation"], ("closeMatch", "relatedMatch"))  # sealed

    def test_no_note_text_rides_along(self):
        blob = json.dumps(self.rows) + json.dumps(self.rejected)
        self.assertNotIn("printed statement", blob)


class WholeBuildTest(unittest.TestCase):
    """A pins file is data: it can be replayed and edited, so every check `fetch`
    makes over the answers the build makes again over the pinned bytes."""

    def edit_pins(self, inputs: str, change) -> None:
        path = os.path.join(inputs, "PINS.json")
        pins = read_json(path)
        change(pins)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(pins, handle)

    def test_pins_that_drop_a_page_refuse_the_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs = acquire(tmp)

            def drop(pins):
                del pins["inputs"]["articles"]["101"]
                del pins["inputs"]["overlay"]["101"]

            self.edit_pins(inputs, drop)
            with self.assertRaises(BuildError) as caught:
                build(inputs, os.path.join(tmp, "out"))
            self.assertIn("not the build's pages", str(caught.exception))

    def test_pins_that_drop_an_index_shard_refuse_the_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs = acquire(tmp)
            self.edit_pins(inputs, lambda pins: pins["inputs"]["index"].clear())
            with self.assertRaises(BuildError) as caught:
                build(inputs, os.path.join(tmp, "out"))
            self.assertIn("index shards", str(caught.exception))

    def test_another_articles_overlay_refuses_the_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs = acquire(tmp)
            self.edit_pins(inputs, lambda pins: pins["inputs"]["overlay"].__setitem__(
                "101", dict(pins["inputs"]["overlay"]["103"])))
            with self.assertRaises(BuildError) as caught:
                build(inputs, os.path.join(tmp, "out"))
            self.assertIn("not this article's overlay", str(caught.exception))

    def test_a_pin_that_names_a_file_outside_the_inputs_refuses_the_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs = acquire(tmp)
            outside = os.path.join(tmp, "outside.json")
            with open(os.path.join(inputs, "articles", "101.json"), "rb") as handle:
                blob = handle.read()
            with open(outside, "wb") as handle:
                handle.write(blob)
            self.edit_pins(inputs, lambda pins: pins["inputs"]["articles"]["101"].update(
                {"path": os.path.join("..", "outside.json")}))
            with self.assertRaises(BuildError) as caught:
                build(inputs, os.path.join(tmp, "out"))
            self.assertIn("outside --inputs", str(caught.exception))


class DeterminismTest(unittest.TestCase):
    def test_two_builds_of_the_same_inputs_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs = acquire(tmp)
            first, manifest_one = build(inputs, os.path.join(tmp, "one"))
            second, manifest_two = build(inputs, os.path.join(tmp, "two"))
            self.assertEqual(manifest_one["logical_root"], manifest_two["logical_root"])
            self.assertEqual(os.path.basename(first), manifest_one["logical_root"])
            for name in ("proposals.jsonl", "rejected.jsonl", "manifest.json"):
                with open(os.path.join(first, name), "rb") as a, \
                        open(os.path.join(second, name), "rb") as b:
                    self.assertEqual(a.read(), b.read(), name)

    def test_a_refetch_of_the_same_bytes_keeps_the_logical_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = build(acquire(tmp), os.path.join(tmp, "one"))[1]
            second_inputs = os.path.join(tmp, "again")
            os.makedirs(second_inputs)
            fetch(second_inputs, None, fixtures.FakeOpener(), site=fixtures.SITE,
                  delay=0.0, sleep=Sleeper())
            second = build(second_inputs, os.path.join(tmp, "two"))[1]
            self.assertEqual(first["logical_root"], second["logical_root"])

    def test_a_pin_mismatch_refuses_the_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs = acquire(tmp)
            path = os.path.join(inputs, "articles", "101.json")
            article = read_json(path)
            article["title"] = "Tampered"
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(article, handle)
            with self.assertRaises(BuildError) as caught:
                build(inputs, os.path.join(tmp, "out"))
            self.assertIn("articles/101.json", str(caught.exception))
            self.assertIn("pinned", str(caught.exception))

    def test_it_refuses_an_out_inside_a_git_work_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs = acquire(tmp)
            out = os.path.join(tmp, "repo", "export")
            os.makedirs(os.path.join(tmp, "repo", ".git"))
            with self.assertRaises(BuildError) as caught:
                build(inputs, out)
            self.assertIn("git work tree", str(caught.exception))

    def test_it_refuses_inputs_that_straddle_two_builds(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs = acquire(tmp)
            # A pinned, self-consistent input set whose article names another universe:
            # the fetch guard cannot be the only one, because the pins can be replayed.
            path = os.path.join(inputs, "articles", "101.json")
            article = read_json(path)
            article["universe"]["sha256"] = fixtures.OTHER_UNIVERSE_SHA
            blob = json.dumps(article, sort_keys=True, separators=(",", ":")).encode()
            with open(path, "wb") as handle:
                handle.write(blob)
            pins_path = os.path.join(inputs, "PINS.json")
            pins = read_json(pins_path)
            import hashlib
            pins["inputs"]["articles"]["101"]["sha256"] = hashlib.sha256(blob).hexdigest()
            with open(pins_path, "w", encoding="utf-8") as handle:
                json.dump(pins, handle)
            with self.assertRaises(BuildError) as caught:
                build(inputs, os.path.join(tmp, "out"))
            self.assertIn("two store builds must never be mixed", str(caught.exception))


class FoldCompatibilityTest(unittest.TestCase):
    """The rows must parse and key the way brain/fold_proposals.py reads them."""

    @classmethod
    def setUpClass(cls):
        import fold_proposals  # noqa: PLC0415 — the fold, imported from brain/
        cls.fold = fold_proposals
        cls.tmp = tempfile.TemporaryDirectory()
        cls.target, cls.manifest, cls.rows, _rejected = run_build(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_folds_type_inference_is_the_one_we_target(self):
        source = text(REPO, "brain", "fold_proposals.py")
        self.assertIn(RTYPE_SOURCE, source,
                      "brain/fold_proposals.py's rtype changed; re-check the row shapes")

    def test_the_fold_ignores_the_namespaced_evidence_key(self):
        source = text(REPO, "brain", "fold_proposals.py")
        self.assertNotIn(EVIDENCE_KEY, source)

    def test_every_row_reads_as_one_of_the_two_channels(self):
        for row in self.rows:
            self.assertIn(rtype(row), ("discover", "repo_link"))

    def test_the_folds_join_key_is_defined_and_unique_for_every_row(self):
        keys = [self.fold.row_key(row) for row in self.rows]
        self.assertEqual(len(set(keys)), len(keys))
        for row, key in zip(self.rows, keys):
            self.assertEqual(key[0], row["qid"])
            self.assertEqual(key[1], row["decl"])

    def test_every_qid_passes_the_folds_own_check(self):
        for row in self.rows:
            self.assertTrue(self.fold.is_qid(row["qid"]), row["qid"])

    def test_every_repo_link_names_a_real_frontier_source(self):
        keys = self.fold.frontier_repo_keys()
        self.assertTrue(keys, "catalog/data/source_registry.json has no frontier_sources")
        for row in self.rows:
            if rtype(row) != "repo_link":
                continue
            self.assertTrue(self.fold.REPO_KEY_RE.match(row["repo"]))
            self.assertIn(row["repo"], keys)
            self.assertEqual(row["kind"], "mentions")   # the channel folds nothing else
            self.assertTrue(row["evidence"].strip())    # the channel requires evidence text
            self.assertTrue(row["qid_label"].strip())   # and a label to check

    def test_every_confidence_is_one_the_fold_grades(self):
        for row in self.rows:
            self.assertIn(row["confidence"], self.fold.CONF_ORDER)

    def test_a_discover_row_carries_no_key_that_would_retype_it(self):
        for row in self.rows:
            if rtype(row) != "discover":
                continue
            self.assertIsNone(row.get("action"))
            self.assertIsNone(row.get("path"))
            self.assertIsInstance(row.get("decl"), str)

    def test_the_rows_round_trip_as_a_proposal_shard(self):
        # The fold reads brain/proposals/*.jsonl line by line with json.loads.
        lines = [line for line in text(self.target, "proposals.jsonl").splitlines() if line.strip()]
        self.assertEqual(len(lines), len(self.rows))
        for line, row in zip(lines, self.rows):
            self.assertEqual(json.loads(line), row)


if __name__ == "__main__":
    unittest.main()
