#!/usr/bin/env python3
"""Tests for scripts/pipeline/tally.py and carry.py — stage 8.

New code. Everything here is synthetic: a two-page corpus built in a temporary
directory, with made-up declarations in a made-up universe. WikiLean is public, so no
article text and no batch-1 data is committed; the batch-1 reproduction is an operator
run and its result is recorded in docs/PIPELINE.md section 8.

What is held:

* the finding rows of an inventory and a locate run, and their ids;
* every challenge action — confirm, uphold, downgrade, refute, overturn — and what each
  leaves on the row;
* the reopen-wins ordering: a prominence overturn to central makes an unjudged part
  ``challenge_pending``, and a confirmed claim does not confirm it back;
* ``confirm_parts``, the bundle's weakest-part relation, a claim that lapses;
* a located passage the challenge empties — the owed hunt — and a ``fills`` that fills it;
* a conditional verdict, which applies only beside the prominence overturn it waits on;
* the status join from the declaration index, and a document that writes ``proof_status``;
* a planned passage no document carries (``missing``) and a scoped-out one;
* the carry-over map's five fates and what carries.

Run: python3 scripts/pipeline/test_tally.py
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

import carry  # noqa: E402
import index as index_mod  # noqa: E402
import keys  # noqa: E402
import tally  # noqa: E402
import vocab  # noqa: E402

COMMIT = "a" * 40
LAYER = ("Alpha states that every widget is a gadget and that every gadget is a widget. "
         "Beta is the name for a gadget that is also a widget. "
         "Gamma is prose about the history of widgets and nothing more. "
         "Delta says that a widget has a dual.")
SPANS = {"alpha": [0, 77], "beta": [78, 130], "gamma": [131, 192], "delta": [193, 229]}
PAGEID = 4242
SOURCE = f"enwiki-{PAGEID}-1000000001"

UNIVERSE = {
    "id": "u-test",
    "universe_sha256": keys.sha256_text("u-test"),
    "frozen": "2026-01-01",
    "libraries": [
        {"id": "testlib", "brain_lib": "testlib", "repo": "https://example.invalid/testlib",
         "commit": COMMIT, "toolchain": "leanprover/lean4:v4.0.0", "roots": ["Testlib"],
         "licence": {"spdx": "Apache-2.0"}},
    ],
    "excluded": [],
}

#: The declaration index the join reads. Nothing here is a real Lean declaration.
DECLS = [
    {"library": "testlib", "commit": COMMIT, "declaration": "Testlib.widgetGadget",
     "proof_status": "proved", "statement": False, "module": "Testlib.Basic",
     "path": "Testlib/Basic.lean", "line": 10, "end_line": 12, "kind": "theorem"},
    {"library": "testlib", "commit": COMMIT, "declaration": "Testlib.gadgetWidget",
     "proof_status": "proved", "statement": False, "module": "Testlib.Basic",
     "path": "Testlib/Basic.lean", "line": 20, "end_line": 22, "kind": "theorem"},
    {"library": "testlib", "commit": COMMIT, "declaration": "Testlib.Gadget",
     "proof_status": "definition", "statement": False, "module": "Testlib.Defs",
     "path": "Testlib/Defs.lean", "line": 5, "end_line": 7, "kind": "structure"},
    {"library": "testlib", "commit": COMMIT, "declaration": "Testlib.Dual",
     "proof_status": "definition", "statement": False, "module": "Testlib.Defs",
     "path": "Testlib/Defs.lean", "line": 30, "end_line": 31, "kind": "def"},
]


def location_key(item: str, declaration: str) -> str:
    return keys.location_key(SOURCE, item, "testlib", COMMIT, declaration)


def item(name: str) -> str:
    return "In-text #" + keys.sha256_text(name)[:8]


ALPHA, BETA, GAMMA, DELTA = (item(name) for name in ("alpha", "beta", "gamma", "delta"))


def inventory_passage(name: str, label: str, kind: str, prominence: str, scope: str,
                      **extra) -> dict:
    span = SPANS[name]
    return {"item": label, "span": span, "head_words": 12, "section": [],
            "quote": {"exact": LAYER[span[0]:span[0] + 30], "prefix": "", "suffix": ""},
            "passage_kind": kind, "prominence": prominence, "scope": scope,
            "statement": f"A restatement of {name}.", **extra}


INVENTORY_PASSAGES = [
    inventory_passage("alpha", ALPHA, "theorem", "subject", "formal_math",
                      conjuncts=[{"id": "c1", "text": "every widget is a gadget"},
                                 {"id": "c2", "text": "every gadget is a widget"}],
                      shape="conjunction"),
    inventory_passage("beta", BETA, "definition", "supporting", "formal_math"),
    inventory_passage("gamma", GAMMA, "other", "supporting", "expository"),
    inventory_passage("delta", DELTA, "theorem", "supporting", "formal_math"),
]


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def receipt(complete: bool = True, ok: bool = True) -> dict:
    return {"complete": complete, "gate": {"ok": ok}, "harvested_at": "2026-01-02T03:04:05Z"}


class Fixture:
    """A synthetic workspace: one inventory run, one locate run, one challenge run."""

    def __init__(self, root: Path):
        self.root = root
        self.corpus = root / "corpus"
        self.runs = root / "runs"
        self.sources = root / "sources"
        (self.sources / SOURCE).mkdir(parents=True, exist_ok=True)
        (self.sources / SOURCE / "layer.txt").write_text(LAYER, encoding="utf-8")
        self.universe_path = root / "universe.json"
        write_json(self.universe_path, UNIVERSE)
        index_path = root / "decls.jsonl"
        index_path.write_text("".join(json.dumps(row) + "\n" for row in DECLS), encoding="utf-8")
        self.index_path = index_path
        self.write_inventory()

    # -- the three runs -------------------------------------------------------------

    def run_json(self, run_id: str, mode: str, **locate) -> None:
        write_json(self.runs / run_id / "run.json",
                   {"run_id": run_id, "mode": mode,
                    "locate": {"mode": mode, "status_rule": vocab.STATUS_RULE_BROAD,
                               "sourceset": {"id": "ss-test", "version": 1,
                                             "manifest_sha256": keys.sha256_text("m")},
                               "universe": {"id": UNIVERSE["id"],
                                            "sha256": UNIVERSE["universe_sha256"]},
                               "source_ids": [SOURCE], **locate}})

    def write_inventory(self, run_id: str = "inv-1", passages=None) -> None:
        self.run_json(run_id, vocab.MODE_INVENTORY)
        write_json(self.corpus / run_id / f"{run_id}-t1" / f"{SOURCE}.json",
                   {"schema": vocab.DOCUMENT_SCHEMA, "mode": vocab.MODE_INVENTORY,
                    "run_id": run_id, "task_id": f"{run_id}-t1", "source_id": SOURCE,
                    "source_sha": "h" * 64, "layer_sha256": keys.sha256_text(LAYER),
                    "renderer": "web-html/3",
                    "passages": INVENTORY_PASSAGES if passages is None else passages})
        write_json(self.corpus / run_id / "RECEIPT.json", receipt())

    def write_locate(self, passages: list[dict], *, run_id: str = "loc-1",
                     planned_items=(ALPHA, BETA, DELTA)) -> None:
        self.run_json(run_id, vocab.MODE_LOCATE, inventory_run="inv-1",
                      passages=len(planned_items))
        write_json(self.runs / run_id / "planned.json",
                   {"run_id": run_id, "schema": "plan/1",
                    "tasks": [{"task_id": f"{run_id}-t1", "run_id": run_id,
                               "mode": vocab.MODE_LOCATE,
                               "documents": [{"source_id": SOURCE,
                                              "passages": [{"item": label}
                                                           for label in planned_items]}]}]})
        write_json(self.corpus / run_id / f"{run_id}-t1" / f"{SOURCE}.json",
                   {"schema": vocab.DOCUMENT_SCHEMA, "mode": vocab.MODE_LOCATE,
                    "run_id": run_id, "task_id": f"{run_id}-t1", "source_id": SOURCE,
                    "source_sha": "h" * 64, "layer_sha256": keys.sha256_text(LAYER),
                    "renderer": "web-html/3", "universe": UNIVERSE["id"],
                    "universe_sha256": UNIVERSE["universe_sha256"], "passages": passages})
        write_json(self.corpus / run_id / "RECEIPT.json", receipt())

    def write_challenge(self, records: list[dict], verdicts: list[dict], *,
                        run_id: str = "chl-1", challenged: str = "loc-1",
                        extra_document: dict | None = None) -> None:
        self.run_json(run_id, vocab.MODE_CHALLENGE, challenged_runs=[challenged])
        write_json(self.runs / run_id / "challenge-plan.json",
                   {"schema": vocab.CHALLENGE_PLAN_SCHEMA, "run_id": run_id,
                    "challenged_runs": [challenged], "records": records})
        document = {"schema": vocab.DOCUMENT_SCHEMA, "mode": vocab.MODE_CHALLENGE,
                    "run_id": run_id, "task_id": f"{run_id}-t1", "source_id": SOURCE,
                    "source_sha": "h" * 64, "layer_sha256": keys.sha256_text(LAYER),
                    "renderer": "web-html/3", "universe": UNIVERSE["id"],
                    "universe_sha256": UNIVERSE["universe_sha256"], "verdicts": verdicts}
        document.update(extra_document or {})
        write_json(self.corpus / run_id / f"{run_id}-t1" / f"{SOURCE}.json", document)
        write_json(self.corpus / run_id / "RECEIPT.json", receipt())

    # -- running it ------------------------------------------------------------------

    def build(self, *, challenge_runs=(), run_id: str = "loc-1",
              unchallenged: bool = False) -> dict:
        workspace = tally.Workspace(self.corpus, self.runs, self.sources)
        declarations = index_mod.JsonlIndex.load(self.index_path)
        return tally.build(workspace, run_id, challenge_runs=challenge_runs,
                           universe=tally.Universe.load(self.universe_path),
                           declarations=declarations, unchallenged=unchallenged)

    def rows_by_item(self, built: dict) -> dict[str, dict]:
        return {row["item"]: row for row in built["outcomes"]}


def located(label: str, locations: list[dict], **extra) -> dict:
    return {"item": label, "outcome": "located", "locations": locations, **extra}


def location(declaration: str, relation: str, rank: int, **extra) -> dict:
    return {"library": "testlib", "declaration": declaration, "relation": relation,
            "rank": rank, "confidence": "high", "note": f"a note about {declaration}",
            **extra}


def plan_record(target: str, label: str, planned: str, **extra) -> dict:
    key = tally.record_key(target, SOURCE, label, extra.pop("location_key", None))
    return {"key": key, "run_id": "loc-1", "source_id": SOURCE, "item": label,
            "target": target, "planned": planned, **extra}


def plan_location(label: str, declaration: str, planned: str, relation: str,
                  **extra) -> dict:
    key = location_key(label, declaration)
    return {"key": key, "run_id": "loc-1", "source_id": SOURCE, "item": label,
            "target": "location", "location_key": key, "planned": planned,
            "relation": relation, "library": "testlib", "declaration": declaration, **extra}


class TallyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fx = Fixture(Path(self._tmp.name))

    # -- rows and the join -----------------------------------------------------------

    def test_inventory_rows_are_one_per_passage(self):
        workspace = tally.Workspace(self.fx.corpus, self.fx.runs, self.fx.sources)
        built = tally.build(workspace, "inv-1",
                            universe=tally.Universe.load(self.fx.universe_path))
        self.assertEqual(built["counts"]["passages"], 4)
        self.assertEqual(built["counts"]["by_scope"], {"expository": 1, "formal_math": 3})
        self.assertTrue(all(row["kind"] == vocab.ROW_PASSAGE for row in built["rows"]))
        self.assertEqual(built["rows"][0]["finding_id"],
                         keys.finding_id("inv-1", SOURCE, ALPHA, vocab.ROW_PASSAGE, []))

    def test_locate_rows_are_one_per_location_absence_and_unresolved(self):
        self.fx.write_locate([
            located(ALPHA, [location("Testlib.widgetGadget", "hasPart", 1, clauses=["c1"],
                                     conjunct_relation="exactMatch"),
                            location("Testlib.gadgetWidget", "hasPart", 2, clauses=["c2"],
                                     conjunct_relation="exactMatch")]),
            {"item": BETA, "outcome": "absent", "locations": [],
             "receipt": {"queries": ["gadget widget", "widget gadget", "dual"],
                         "candidates": []}},
            {"item": DELTA, "outcome": "unresolved", "locations": []},
        ])
        built = self.fx.build()
        kinds = {row["kind"] for row in built["rows"]}
        self.assertEqual(kinds, {vocab.ROW_LOCATION, vocab.ROW_ABSENCE, vocab.ROW_UNRESOLVED})
        self.assertEqual(built["counts"]["rows"],
                         {"ABSENCE": 1, "LOCATION": 2, "UNRESOLVED": 1})
        self.assertEqual(built["counts"]["by_outcome"],
                         {"absent": 1, "located": 1, "none": 1, "unresolved": 1})
        # gamma was inventoried expository and is in no plan: a scoped-out row, no outcome
        row = self.fx.rows_by_item(built)[GAMMA]
        self.assertIsNone(row["outcome"])
        self.assertEqual(row["reason"], "scoped expository")
        self.assertEqual(built["counts"]["scoped_out"], 1)

    def test_the_proof_status_is_joined_and_never_read_from_a_document(self):
        self.fx.write_locate([
            located(DELTA, [location("Testlib.Dual", "exactMatch", 1,
                                     proof_status="statement_only")]),
        ], planned_items=(DELTA,))
        built = self.fx.build()
        row = self.fx.rows_by_item(built)[DELTA]
        self.assertEqual(row["locations"][0]["proof_status"], "definition")
        # the written value is ignored and named; the location itself still stands, so the
        # problem is the run's and not the row's
        self.assertTrue(any("it is joined from the index" in text
                            for text in built["problems"]))
        # a theorem passage at an exact match to data: decision D1 formalizes it
        self.assertEqual(row["status"], "formalized")

    def test_a_declaration_outside_the_index_drops_its_location(self):
        self.fx.write_locate([
            located(DELTA, [location("Testlib.NotThere", "exactMatch", 1)]),
        ], planned_items=(DELTA,))
        built = self.fx.build()
        row = self.fx.rows_by_item(built)[DELTA]
        self.assertEqual(row["locations"], [])
        self.assertIn("is not in the declaration index", row["problem"])

    def test_a_planned_passage_with_no_document_is_missing(self):
        self.fx.write_locate([located(DELTA, [location("Testlib.Dual", "exactMatch", 1)])])
        built = self.fx.build()
        rows = self.fx.rows_by_item(built)
        self.assertTrue(rows[ALPHA]["missing"])
        self.assertTrue(rows[BETA]["missing"])
        self.assertEqual(built["counts"]["missing"], 2)
        self.assertIsNone(rows[ALPHA]["status"])
        self.assertIsNone(rows[ALPHA]["challenge"])

    # -- the challenge ---------------------------------------------------------------

    def _challenged(self, records, verdicts, *, locate=None, extra_document=None):
        self.fx.write_locate(locate if locate is not None else [
            located(DELTA, [location("Testlib.Dual", "exactMatch", 1)]),
        ], planned_items=(DELTA,))
        self.fx.write_challenge(records, verdicts, extra_document=extra_document)
        return self.fx.build(challenge_runs=["chl-1"])

    def test_confirm_makes_the_record_survive(self):
        built = self._challenged(
            [plan_location(DELTA, "Testlib.Dual", "required", "exactMatch")],
            [{"item": DELTA, "target": "location",
              "location_key": location_key(DELTA, "Testlib.Dual"), "action": "confirm",
              "witness": "The declaration says it.", "instantiation": "at a widget"}])
        row = self.fx.rows_by_item(built)[DELTA]
        self.assertEqual(row["locations"][0]["challenge"], "survived")
        self.assertEqual(row["locations"][0]["instantiation"], "at a widget")
        self.assertEqual(row["challenge"], "survived")
        self.assertEqual(row["runs"]["challenge"], "chl-1")

    def test_a_required_record_nobody_judged_is_owed(self):
        built = self._challenged(
            [plan_location(DELTA, "Testlib.Dual", "required", "exactMatch")], [])
        row = self.fx.rows_by_item(built)[DELTA]
        self.assertIsNone(row["locations"][0]["challenge"])
        self.assertIsNone(row["challenge"])
        self.assertEqual(built["counts"]["by_challenge"], {"owed": 1})

    def test_downgrade_must_name_a_weaker_relation(self):
        key = location_key(DELTA, "Testlib.Dual")
        built = self._challenged(
            [plan_location(DELTA, "Testlib.Dual", "required", "exactMatch")],
            [{"item": DELTA, "target": "location", "location_key": key,
              "action": "downgrade", "relation": "exactMatch", "witness": "No."}])
        row = self.fx.rows_by_item(built)[DELTA]
        self.assertEqual(row["locations"][0]["relation"], "exactMatch")
        self.assertIsNone(row["locations"][0]["challenge"])
        self.assertIn("is not weaker", row["problem"])

    def test_a_downgrade_to_closematch_takes_its_note_from_the_witness(self):
        key = location_key(DELTA, "Testlib.Dual")
        built = self._challenged(
            [plan_location(DELTA, "Testlib.Dual", "required", "exactMatch")],
            [{"item": DELTA, "target": "location", "location_key": key,
              "action": "downgrade", "relation": "closeMatch",
              "witness": "It differs by a convention. R-SPECIFIC miss: testlib Other."}],
            locate=[located(DELTA, [location("Testlib.Dual", "exactMatch", 1, note="")])])
        loc = self.fx.rows_by_item(built)[DELTA]["locations"][0]
        self.assertEqual(loc["relation"], "closeMatch")
        self.assertEqual(loc["note"], "It differs by a convention.")
        self.assertEqual(loc["challenge"], "downgraded")

    def test_refute_moves_the_location_to_refuted_and_renumbers(self):
        built = self._challenged(
            [plan_location(DELTA, "Testlib.Dual", "required", "exactMatch"),
             plan_location(DELTA, "Testlib.Gadget", "sampled_out", "broadMatch")],
            [{"item": DELTA, "target": "location",
              "location_key": location_key(DELTA, "Testlib.Dual"), "action": "refute",
              "witness": "It states something else."}],
            locate=[located(DELTA, [location("Testlib.Dual", "exactMatch", 1),
                                    location("Testlib.Gadget", "broadMatch", 2)])])
        row = self.fx.rows_by_item(built)[DELTA]
        self.assertEqual([loc["declaration"] for loc in row["locations"]], ["Testlib.Gadget"])
        self.assertEqual(row["locations"][0]["rank"], 1)
        self.assertEqual([loc["declaration"] for loc in row["refuted"]], ["Testlib.Dual"])
        self.assertEqual(row["challenge"], "downgraded")

    def test_rank_one_after_a_downgrade_is_the_strongest_surviving_card(self):
        built = self._challenged(
            [plan_location(DELTA, "Testlib.Dual", "required", "exactMatch"),
             plan_location(DELTA, "Testlib.Gadget", "sampled_out", "broadMatch")],
            [{"item": DELTA, "target": "location",
              "location_key": location_key(DELTA, "Testlib.Dual"), "action": "downgrade",
              "relation": "narrowMatch", "witness": "Only a special case."}],
            locate=[located(DELTA, [location("Testlib.Dual", "exactMatch", 1),
                                    location("Testlib.Gadget", "broadMatch", 2)])])
        row = self.fx.rows_by_item(built)[DELTA]
        self.assertEqual([loc["declaration"] for loc in row["locations"]],
                         ["Testlib.Gadget", "Testlib.Dual"])
        self.assertEqual([loc["rank"] for loc in row["locations"]], [1, 2])

    def test_uphold_keeps_an_absence_and_records_the_challengers_queries(self):
        built = self._challenged(
            [plan_record("absence", BETA, "required", outcome="absent")],
            [{"item": BETA, "target": "absence", "action": "uphold",
              "queries": ["gadget", "widget", "dual"],
              "witness": "Nothing in the library states it."}],
            locate=[{"item": BETA, "outcome": "absent", "locations": [],
                     "receipt": {"queries": ["a", "b", "c"], "candidates": []}}])
        row = self.fx.rows_by_item(built)[BETA]
        self.assertEqual(row["absence"]["challenge"], "survived")
        self.assertEqual(row["challenge_receipt"]["queries"], ["gadget", "widget", "dual"])
        self.assertEqual(row["status"], "not_formalized")

    def test_overturn_replaces_an_absence_with_the_challengers_own_locations(self):
        built = self._challenged(
            [plan_record("absence", BETA, "required", outcome="absent")],
            [{"item": BETA, "target": "absence", "action": "overturn",
              "witness": "It is right here.",
              "locations": [location("Testlib.Gadget", "exactMatch", 1)]}],
            locate=[{"item": BETA, "outcome": "absent", "locations": [],
                     "receipt": {"queries": ["a", "b", "c"], "candidates": []}}])
        row = self.fx.rows_by_item(built)[BETA]
        self.assertEqual(row["outcome"], "located")
        self.assertIsNone(row["absence"])
        self.assertEqual(row["locations"][0]["declaration"], "Testlib.Gadget")
        self.assertEqual(row["locations"][0]["challenge"], "survived")
        self.assertEqual(row["challenge"], "downgraded")
        self.assertEqual(row["status"], "formalized")

    def test_a_verdict_on_a_record_no_plan_required_is_named_and_ignored(self):
        built = self._challenged(
            # a second, required record, so the run has documents at all: a plan that
            # requires nothing is never harvested and none of its documents are read
            [plan_location(DELTA, "Testlib.Dual", "sampled_out", "exactMatch"),
             plan_record("prominence", DELTA, "required", outcome="located")],
            [{"item": DELTA, "target": "location",
              "location_key": location_key(DELTA, "Testlib.Dual"), "action": "refute",
              "witness": "No."},
             {"item": DELTA, "target": "prominence", "action": "uphold",
              "witness": "It is supporting."}])
        row = self.fx.rows_by_item(built)[DELTA]
        self.assertEqual(row["locations"][0]["challenge"], "sampled_out")
        self.assertEqual(row["refuted"], [])
        self.assertTrue(any("the verdict is ignored" in text for text in built["problems"]))

    def test_a_verdict_on_a_relation_the_plan_did_not_see_leaves_it_owed(self):
        built = self._challenged(
            [plan_location(DELTA, "Testlib.Dual", "required", "broadMatch")],
            [{"item": DELTA, "target": "location",
              "location_key": location_key(DELTA, "Testlib.Dual"), "action": "confirm",
              "witness": "Yes."}])
        row = self.fx.rows_by_item(built)[DELTA]
        self.assertIsNone(row["locations"][0]["challenge"])
        self.assertIn("it is owed again", row["problem"])

    # -- the coverage bundle ---------------------------------------------------------

    def _bundle_locate(self):
        return [located(ALPHA,
                        [location("Testlib.widgetGadget", "hasPart", 1, clauses=["c1"],
                                  conjunct_relation="exactMatch"),
                         location("Testlib.gadgetWidget", "hasPart", 2, clauses=["c2"],
                                  conjunct_relation="broadMatch")],
                        coverage={"complete": True, "parts": [1, 2],
                                  "note": "The two parts state both clauses."})]

    def test_a_confirmed_bundle_takes_its_weakest_parts_relation(self):
        parts = [location_key(ALPHA, "Testlib.widgetGadget"),
                 location_key(ALPHA, "Testlib.gadgetWidget")]
        built = self._challenged(
            [plan_record("coverage", ALPHA, "required", parts=parts),
             plan_location(ALPHA, "Testlib.widgetGadget", "sampled_out", "hasPart"),
             plan_location(ALPHA, "Testlib.gadgetWidget", "sampled_out", "hasPart")],
            [{"item": ALPHA, "target": "coverage", "action": "confirm",
              "witness": "Both clauses are stated."}],
            locate=self._bundle_locate())
        row = self.fx.rows_by_item(built)[ALPHA]
        self.assertEqual(row["coverage"]["challenge"], "survived")
        # exactMatch and broadMatch parts: the bundle is a broadMatch
        self.assertEqual(row["coverage"]["relation"], "broadMatch")
        # confirming the claim confirms the parts no rule required
        self.assertEqual([loc["challenge"] for loc in row["locations"]],
                         ["survived", "survived"])
        self.assertEqual(built["counts"]["bundles"],
                         {"written": 1, "survived": 1, "by_relation": {"broadMatch": 1}})

    def test_a_refuted_bundle_leaves_its_parts_plain_haspart_locations(self):
        parts = [location_key(ALPHA, "Testlib.widgetGadget"),
                 location_key(ALPHA, "Testlib.gadgetWidget")]
        built = self._challenged(
            [plan_record("coverage", ALPHA, "required", parts=parts),
             plan_location(ALPHA, "Testlib.widgetGadget", "sampled_out", "hasPart"),
             plan_location(ALPHA, "Testlib.gadgetWidget", "sampled_out", "hasPart")],
            [{"item": ALPHA, "target": "coverage", "action": "refute",
              "witness": "The second clause is unstated.", "unstated": ["c2"]}],
            locate=self._bundle_locate())
        row = self.fx.rows_by_item(built)[ALPHA]
        self.assertIsNone(row["coverage"])
        self.assertEqual(row["coverage_refuted"]["challenge"], "downgraded")
        self.assertEqual(row["coverage_refuted"]["unstated"], ["c2"])
        self.assertEqual([loc["challenge"] for loc in row["locations"]],
                         ["sampled_out", "sampled_out"])
        self.assertEqual(row["status"], "partial")  # two hasPart locations and nothing else

    def test_a_claim_whose_part_was_refuted_lapses(self):
        parts = [location_key(ALPHA, "Testlib.widgetGadget"),
                 location_key(ALPHA, "Testlib.gadgetWidget")]
        built = self._challenged(
            [plan_record("coverage", ALPHA, "required", parts=parts),
             plan_location(ALPHA, "Testlib.widgetGadget", "required", "hasPart"),
             plan_location(ALPHA, "Testlib.gadgetWidget", "sampled_out", "hasPart")],
            [{"item": ALPHA, "target": "coverage", "action": "confirm", "witness": "Yes."},
             {"item": ALPHA, "target": "location", "location_key": parts[0],
              "action": "refute", "witness": "It states neither clause."}],
            locate=self._bundle_locate())
        row = self.fx.rows_by_item(built)[ALPHA]
        self.assertIn("the claim no longer holds", row["problem"])
        self.assertEqual(built["counts"]["bundles"]["survived"], 0)

    def test_the_reopen_wins_over_a_confirmed_claim(self):
        """A prominence overturn to central makes an unjudged part pending, and stays so."""
        locate = [located(BETA,
                          [location("Testlib.widgetGadget", "hasPart", 1, clauses=["c1"],
                                    conjunct_relation="exactMatch"),
                           location("Testlib.gadgetWidget", "hasPart", 2, clauses=["c2"],
                                    conjunct_relation="exactMatch")],
                          coverage={"complete": True, "parts": [1, 2],
                                    "note": "The two parts state both clauses."})]
        # beta is inventoried `supporting`; the challenge overturns it to `central`
        beta_parts = [location_key(BETA, "Testlib.widgetGadget"),
                      location_key(BETA, "Testlib.gadgetWidget")]
        built = self._challenged(
            [plan_record("coverage", BETA, "required", parts=beta_parts),
             plan_record("prominence", BETA, "required", outcome="located"),
             plan_location(BETA, "Testlib.widgetGadget", "sampled_out", "hasPart"),
             plan_location(BETA, "Testlib.gadgetWidget", "sampled_out", "hasPart")],
            [{"item": BETA, "target": "coverage", "action": "confirm", "witness": "Yes."},
             {"item": BETA, "target": "prominence", "action": "overturn",
              "prominence": "central", "witness": "The article is about it."}],
            locate=locate)
        row = self.fx.rows_by_item(built)[BETA]
        self.assertEqual(row["prominence"], "central")
        self.assertEqual([loc["challenge"] for loc in row["locations"]],
                         ["challenge_pending", "challenge_pending"])
        self.assertEqual(row["coverage"]["challenge"], "survived")
        self.assertEqual(row["challenge"], "challenge_pending")

    def test_a_conditional_verdict_applies_only_beside_its_prominence_overturn(self):
        key = location_key(DELTA, "Testlib.Dual")
        records = [plan_record("prominence", DELTA, "required", outcome="located"),
                   plan_location(DELTA, "Testlib.Dual", "sampled_out", "exactMatch",
                                 conditional="prominence")]
        refute = {"item": DELTA, "target": "location", "location_key": key,
                  "action": "refute", "witness": "It states something else."}
        # without the overturn: named and ignored
        built = self._challenged(records, [refute])
        row = self.fx.rows_by_item(built)[DELTA]
        self.assertEqual(row["refuted"], [])
        self.assertTrue(any("no overturn of the passage's prominence" in text
                            for text in built["problems"]))
        # with it: applied
        self.setUp()
        built = self._challenged(records, [
            refute,
            {"item": DELTA, "target": "prominence", "action": "overturn",
             "prominence": "central", "witness": "It is the subject."}])
        row = self.fx.rows_by_item(built)[DELTA]
        self.assertEqual([loc["declaration"] for loc in row["refuted"]], ["Testlib.Dual"])

    # -- the owed hunt and the fill ---------------------------------------------------

    def test_a_located_passage_the_challenge_empties_is_owed_a_hunt(self):
        built = self._challenged(
            [plan_location(DELTA, "Testlib.Dual", "required", "exactMatch")],
            [{"item": DELTA, "target": "location",
              "location_key": location_key(DELTA, "Testlib.Dual"), "action": "refute",
              "witness": "It states something else."}])
        row = self.fx.rows_by_item(built)[DELTA]
        self.assertEqual(row["owed"], "hunt")
        self.assertEqual(row["challenge"], "challenge_pending")
        self.assertIsNone(row["status"])
        self.assertIn("it is owed a hunt", row["problem"])
        self.assertEqual(built["counts"]["owed_hunt"], 1)

    def test_a_fill_replaces_the_last_counting_location(self):
        built = self._challenged(
            [plan_location(DELTA, "Testlib.Dual", "required", "exactMatch")],
            [{"item": DELTA, "target": "location",
              "location_key": location_key(DELTA, "Testlib.Dual"), "action": "refute",
              "witness": "It states something else."}],
            extra_document={"fills": {DELTA: {"outcome": "absent",
                                              "receipt": {"queries": ["a", "b", "c"],
                                                          "candidates": []}}}})
        row = self.fx.rows_by_item(built)[DELTA]
        self.assertEqual(row["outcome"], "absent")
        self.assertEqual(row["absence"]["challenge"], "survived")
        self.assertEqual(row["status"], "not_formalized")
        self.assertNotIn("owed", row)
        self.assertEqual(built["counts"]["owed_hunt"], 0)

    # -- the exportable rows ----------------------------------------------------------

    def test_export_rows_carry_a_cut_and_a_bundles_conjuncts(self):
        parts = [location_key(ALPHA, "Testlib.widgetGadget"),
                 location_key(ALPHA, "Testlib.gadgetWidget")]
        built = self._challenged(
            [plan_record("coverage", ALPHA, "required", parts=parts),
             plan_location(ALPHA, "Testlib.widgetGadget", "sampled_out", "hasPart"),
             plan_location(ALPHA, "Testlib.gadgetWidget", "sampled_out", "hasPart")],
            [{"item": ALPHA, "target": "coverage", "action": "confirm", "witness": "Yes."}],
            locate=self._bundle_locate())
        workspace = tally.Workspace(self.fx.corpus, self.fx.runs, self.fx.sources)
        rows, refused = tally.export_rows(built, workspace)
        self.assertEqual(refused, [])
        row = next(r for r in rows if r["item"] == ALPHA)
        self.assertEqual(row["cut"]["span_sha256"],
                         keys.sha256_text(LAYER[SPANS["alpha"][0]:SPANS["alpha"][1]]))
        self.assertEqual([c["id"] for c in row["coverage"]["conjuncts"]], ["c1", "c2"])
        self.assertEqual(row["coverage"]["conjuncts"][0]["part"], parts[0])
        # the outcome row itself keeps the claim the tally worked with, with no conjuncts
        self.assertNotIn("conjuncts", tally.outcome_row(
            self.fx.rows_by_item(built)[ALPHA])["coverage"])

    # -- an unchallenged run ------------------------------------------------------------

    def _unchallenged_locate(self):
        self.fx.write_locate(self._bundle_locate() + [
            {"item": BETA, "outcome": "absent", "locations": [],
             "receipt": {"queries": ["gadget widget", "widget gadget", "dual"],
                         "candidates": []}},
            located(DELTA, [location("Testlib.Dual", "exactMatch", 1)])])

    def test_an_unchallenged_run_reads_not_required_and_owes_nothing(self):
        self._unchallenged_locate()
        built = self.fx.build(unchallenged=True)
        rows = self.fx.rows_by_item(built)
        self.assertEqual([loc["challenge"] for loc in rows[ALPHA]["locations"]],
                         ["not_required", "not_required"])
        self.assertEqual(rows[ALPHA]["coverage"]["challenge"], "not_required")
        # a claim no challenge confirmed upgrades nothing: two hasPart locations
        self.assertEqual(rows[ALPHA]["status"], "partial")
        self.assertEqual(rows[BETA]["absence"]["challenge"], "not_required")
        self.assertEqual(rows[BETA]["status"], "not_formalized")
        self.assertEqual(rows[DELTA]["locations"][0]["challenge"], "not_required")
        self.assertEqual(rows[DELTA]["status"], "formalized")
        self.assertEqual({rows[label]["challenge"] for label in (ALPHA, BETA, DELTA)},
                         {"not_required"})
        self.assertIsNone(rows[DELTA]["runs"]["challenge"])
        # gamma was inventoried expository: its scope record is stamped too
        self.assertEqual(rows[GAMMA]["challenge"], "not_required")
        self.assertEqual(built["counts"]["by_challenge"], {"not_required": 3})
        self.assertTrue(built["unchallenged"])
        workspace = tally.Workspace(self.fx.corpus, self.fx.runs, self.fx.sources)
        exported, refused = tally.export_rows(built, workspace)
        self.assertEqual(refused, [])
        self.assertEqual({row["item"] for row in exported}, {ALPHA, BETA, DELTA})
        # no mark rides on the rows: the subject passage (alpha) keeps the article's QID
        self.assertFalse(any("unchallenged" in row for row in exported))

    def test_an_unresolved_passage_of_an_unchallenged_run_is_exported(self):
        self.fx.write_locate([{"item": DELTA, "outcome": "unresolved", "locations": []}],
                             planned_items=(DELTA,))
        built = self.fx.build(unchallenged=True)
        row = self.fx.rows_by_item(built)[DELTA]
        self.assertEqual(row["outcome"], "unresolved")
        self.assertEqual(row["challenge"], "not_required")
        workspace = tally.Workspace(self.fx.corpus, self.fx.runs, self.fx.sources)
        exported, refused = tally.export_rows(built, workspace)
        self.assertEqual(refused, [])
        self.assertEqual([r["item"] for r in exported], [DELTA])
        self.assertEqual(exported[0]["challenge"], "not_required")

    def test_the_same_run_without_a_challenge_or_the_flag_is_owed(self):
        self._unchallenged_locate()
        built = self.fx.build()
        self.assertEqual(built["counts"]["by_challenge"], {"owed": 3})
        self.assertFalse(built["unchallenged"])

    def test_unchallenged_is_refused_beside_a_challenge_run(self):
        self.fx.write_locate([located(DELTA, [location("Testlib.Dual", "exactMatch", 1)])],
                             planned_items=(DELTA,))
        with self.assertRaises(tally.TallyProblem) as caught:
            self.fx.build(challenge_runs=["chl-1"], unchallenged=True)
        self.assertIn("not both", str(caught.exception))

    def test_the_command_line_writes_unchallenged_export_rows(self):
        self._unchallenged_locate()
        out = Path(self._tmp.name) / "out"
        code = tally.main(["--run-id", "loc-1", "--corpus", str(self.fx.corpus),
                           "--runs", str(self.fx.runs), "--sources", str(self.fx.sources),
                           "--universe", str(self.fx.universe_path),
                           "--index", str(self.fx.index_path), "--unchallenged",
                           "--export-rows", str(out / "rows.jsonl"),
                           "--report", str(out / "report.json")])
        self.assertEqual(code, 0)
        rows = [json.loads(line) for line in
                (out / "rows.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertTrue(all(loc["challenge"] == "not_required"
                                for loc in row["locations"]))
        report = json.loads((out / "report.json").read_text(encoding="utf-8"))
        self.assertTrue(report["unchallenged"])
        self.assertEqual(report["challenge_runs"], [])


class BundleRelationTests(unittest.TestCase):
    def test_the_bundle_takes_the_weakest_part(self):
        parts = [{"location_key": "a", "conjunct_relation": "exactMatch"},
                 {"location_key": "b", "conjunct_relation": "broadMatch"}]
        self.assertEqual(tally.bundle_relation(parts), "broadMatch")

    def test_a_confirm_may_weaken_a_part_but_never_raise_it(self):
        parts = [{"location_key": "a", "conjunct_relation": "exactMatch"},
                 {"location_key": "b", "conjunct_relation": "broadMatch"}]
        self.assertEqual(tally.bundle_relation(parts, {"a": "closeMatch"}), "broadMatch")
        self.assertEqual(tally.bundle_relation(parts, {"b": "exactMatch"}), "broadMatch")

    def test_a_part_with_no_conjunct_relation_gives_no_bundle(self):
        self.assertIsNone(tally.bundle_relation([{"location_key": "a"}]))


class ItemGrammarTests(unittest.TestCase):
    def test_clean_hashes_and_join_key_joins(self):
        import items

        self.assertEqual(items.clean("In-text\t#3FA91C07."), "In-text #3FA91C07")
        self.assertEqual(items.join_key("In-text#3FA91C07"), "In-text #3fa91c07")
        self.assertEqual(items.join_key("In-text #3fa91c07"), "In-text #3fa91c07")
        # a label the grammar refuses keeps itself, never guessed onto another passage
        self.assertEqual(items.join_key("Exercise 1.3.19"), "Exercise 1.3.19")
        self.assertFalse(items.is_passage_item("In-text #xyz"))


class CarryTests(unittest.TestCase):
    """The five fates, computed from spans and span text alone."""

    def cut(self, layer: str, spans: list[list[int]], scopes=None) -> carry.Cut:
        scopes = scopes or ["formal_math"] * len(spans)
        return carry.Cut(PAGEID, SOURCE, layer, keys.sha256_text(layer),
                         [{"item": item(f"p{number}"), "span": span, "scope": scope}
                          for number, (span, scope) in enumerate(zip(spans, scopes))])

    def test_the_same_cut_twice_is_all_unchanged(self):
        cut = self.cut(LAYER, [SPANS["alpha"], SPANS["beta"]])
        page = carry.map_page(cut, cut)
        self.assertEqual(page["counts"], {"unchanged": 2})
        self.assertTrue(page["same_layer"])
        self.assertEqual(page["old"]["cut_id"], page["new"]["cut_id"])

    def test_the_five_fates(self):
        old = self.cut(LAYER, [[0, 40], [40, 77], [78, 130], [131, 192], [193, 229]])
        new = self.cut(LAYER, [[0, 20], [20, 40], [40, 77], [131, 192]])
        page = carry.map_page(old, new)
        fates = {tuple(entry["span"]): entry["fate"] for entry in page["passages"]}
        self.assertEqual(fates[(0, 40)], "split")        # two new passages share it
        self.assertEqual(fates[(40, 77)], "unchanged")   # the same text
        self.assertEqual(fates[(78, 130)], "removed")    # nothing overlaps it
        self.assertEqual(fates[(131, 192)], "unchanged")
        self.assertEqual(fates[(193, 229)], "removed")

    def test_end_moved_merged_and_reshaped(self):
        old = self.cut(LAYER, [[10, 40]])
        for spans, fate in (([[10, 50]], "end_moved"), ([[0, 60]], "merged"),
                            ([[20, 50]], "reshaped")):
            with self.subTest(fate=fate):
                page = carry.map_page(old, self.cut(LAYER, spans))
                self.assertEqual(page["passages"][0]["fate"], fate)

    def test_a_new_passage_nothing_went_into_is_new(self):
        page = carry.map_page(self.cut(LAYER, [[0, 40]]), self.cut(LAYER, [[0, 40], [78, 130]]))
        self.assertEqual(page["counts"], {"new": 1, "unchanged": 1})
        self.assertEqual(page["new_passages"][0]["fate"], "new")

    def test_a_passage_is_placed_by_its_text_on_another_layer(self):
        old_layer = "PREFIX. " + LAYER
        old = carry.Cut(PAGEID, SOURCE, old_layer, keys.sha256_text(old_layer),
                        [{"item": ALPHA, "span": [8, 85], "scope": "formal_math"}])
        new = self.cut(LAYER, [SPANS["alpha"]])
        page = carry.map_page(old, new)
        self.assertFalse(page["same_layer"])
        self.assertEqual(page["passages"][0]["fate"], "unchanged")

    def test_two_passages_of_one_cut_with_one_key_refuse_the_page(self):
        layer = "Same text. Same text."
        cut = carry.Cut(PAGEID, SOURCE, layer, keys.sha256_text(layer),
                        [{"item": ALPHA, "span": [0, 10], "scope": "formal_math"},
                         {"item": BETA, "span": [11, 21], "scope": "formal_math"}])
        with self.assertRaises(carry.CarryRefused):
            carry.map_page(cut, cut)

    def test_what_carries(self):
        unchanged = {"fate": "unchanged", "conjuncts_changed": False,
                     "into": [{"scope": "formal_math"}]}
        self.assertTrue(carry.carries(unchanged))
        self.assertFalse(carry.carries({**unchanged, "fate": "end_moved"}))
        self.assertFalse(carry.carries({**unchanged, "into": [{"scope": "expository"}]}))
        self.assertFalse(carry.carries({**unchanged, "conjuncts_changed": True}))
        # a row that read no conjunct is not asked again over a conjunct change
        self.assertTrue(carry.carries({**unchanged, "conjuncts_changed": True},
                                      reads_conjuncts=False))

    def test_carry_rows_marks_each_row(self):
        cut = self.cut(LAYER, [SPANS["alpha"]])
        doc = {"pages": [carry.map_page(cut, cut)]}
        key = doc["pages"][0]["passages"][0]["passage_key"]
        rows, counts = carry.carry_rows(doc, [{"passage_key": key, "verdict": "confirm"},
                                              {"passage_key": "f" * 64}])
        self.assertTrue(rows[0]["carried"])
        self.assertFalse(rows[1]["carried"])
        self.assertEqual(counts, {"rows": 2, "carried": 1, "asked_again": 1, "unmatched": 1})


if __name__ == "__main__":
    unittest.main(verbosity=2)
