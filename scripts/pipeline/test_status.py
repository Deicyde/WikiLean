#!/usr/bin/env python3
"""Tests for scripts/pipeline/status.py, the derived status under rules 1, 2 and 3.

New code. The producer stamps the status; scripts/articles/build_store.py re-derives it
and refuses a set that disagrees. So the test that matters is parity, three ways:

1. **the producer's truth tables** — scripts/articles/fixtures/producer_derive_status.json
   (1,679 cases, status rule 1) and -2/-3.json (3,197 each), generated from
   formal-companion's own derive_status. These are the answers a real set carries, and
   -2/-3 were generated at d85385aa, the commit that produced batch 1;
2. **the hand-written tables both sides run** — status_parity{,-2,-3}.json, whose
   expectations were written from the decisions (D1, its round-three reading, Q9,
   R-BROAD) rather than from either implementation, refusals included;
3. **the consumer in this repository** — build_store.derive_status and
   derive_passage_status, case for case, so the two implementations cannot drift
   (docs/PIPELINE.md section 3, stage 3, and question Q1).

Run: python3 scripts/pipeline/test_status.py
"""
from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "scripts" / "articles"))

import build_store  # noqa: E402
import status  # noqa: E402
import vocab  # noqa: E402

FIXTURES = ROOT / "scripts" / "articles" / "fixtures"
PRODUCER_TABLES = {1: FIXTURES / "producer_derive_status.json",
                   2: FIXTURES / "producer_derive_status-2.json",
                   3: FIXTURES / "producer_derive_status-3.json"}
SHARED_TABLES = {1: FIXTURES / "status_parity.json",
                 2: FIXTURES / "status_parity-2.json",
                 3: FIXTURES / "status_parity-3.json"}
# The producer's commit each table was generated at. -2 and -3 are batch 1's skill_commit.
PRODUCER_COMMITS = {1: "4fc2aa040084fdf84f9a914531da8c20357cd7f3",
                    2: "d85385aa4eb2818373e9b6b8b41b765ffaf53750",
                    3: "d85385aa4eb2818373e9b6b8b41b765ffaf53750"}


def key(n: int) -> str:
    """A case's nth location key as the 64-hex key a record carries."""
    return hashlib.sha256(b"key%d" % n).hexdigest()


def hex_key(name):
    """A shared case's key ("k1") as 64 hex; anything else is left as it is, so it stays malformed."""
    return hashlib.sha256(name.encode("utf-8")).hexdigest() if isinstance(name, str) else name


def producer_case(ends: list, claimed: dict | None) -> tuple[list[dict], dict | None]:
    """One producer-table case as `derive`'s arguments: ranked locations and its claim."""
    locations = [{"relation": relation, "proof_status": proof_status, "statement": statement,
                  "rank": rank, "location_key": key(rank)}
                 for rank, (relation, proof_status, statement) in enumerate(ends, 1)]
    claim = None
    if claimed:
        claim = {"complete": True, "challenge": claimed["challenge"],
                 "parts": [locations[i]["location_key"] for i in claimed["parts"]]}
        if "relation" in claimed:  # status rules 2 and 3: a bundle carries its relation
            claim["relation"] = claimed["relation"]
    return locations, claim


def shared_case(case: dict, status_rule: int):
    """`status.derive` on one case of a shared table: a status, None, or "refused"."""
    locations = [{**loc, "location_key": hex_key(loc.get("location_key"))}
                 for loc in case["locations"]]
    coverage = case["coverage"]
    if isinstance(coverage, dict) and isinstance(coverage.get("parts"), list):
        coverage = {**coverage, "parts": [hex_key(name) for name in coverage["parts"]]}
    if isinstance(coverage, dict) and isinstance(coverage.get("conjuncts"), list):
        coverage = {**coverage,
                    "conjuncts": [{**c, "part": hex_key(c["part"])}
                                  if isinstance(c, dict) and "part" in c else c
                                  for c in coverage["conjuncts"]]}
    try:
        return status.derive(case["passage_kind"], locations, case["absent"], coverage, status_rule)
    except status.StatusProblem:
        return "refused"


def consumer_shared_case(case: dict, status_rule: int):
    """The same case through the store builder, so the two implementations are compared."""
    locations = [{**loc, "location_key": hex_key(loc.get("location_key"))}
                 for loc in case["locations"]]
    coverage = case["coverage"]
    if isinstance(coverage, dict) and isinstance(coverage.get("parts"), list):
        coverage = {**coverage, "parts": [hex_key(name) for name in coverage["parts"]]}
    if isinstance(coverage, dict) and isinstance(coverage.get("conjuncts"), list):
        coverage = {**coverage,
                    "conjuncts": [{**c, "part": hex_key(c["part"])}
                                  if isinstance(c, dict) and "part" in c else c
                                  for c in coverage["conjuncts"]]}
    try:
        return build_store.derive_passage_status(case["passage_kind"], locations, case["absent"],
                                                 coverage, status_rule)
    except build_store.StatusRefused:
        return "refused"


class ProducerTableTests(unittest.TestCase):
    """The producer's own answers, per status rule, case for case."""

    def table(self, status_rule: int) -> dict:
        return json.loads(PRODUCER_TABLES[status_rule].read_text(encoding="utf-8"))

    def test_the_tables_are_the_producers_and_name_their_commit(self):
        for rule, path in PRODUCER_TABLES.items():
            with self.subTest(status_rule=rule):
                doc = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(doc["producer"]["repository"], "formal-companion")
                self.assertEqual(doc["producer"]["commit"], PRODUCER_COMMITS[rule])
                self.assertEqual(doc.get("status_rule", 1), rule)
                self.assertGreater(len(doc["cases"]), 1000)

    def run_table(self, status_rule: int) -> list:
        mismatches = []
        for kind, ends, expected, *claimed in self.table(status_rule)["cases"]:
            locations, claim = producer_case(ends, claimed[0] if claimed else None)
            try:
                got = status.derive(kind, locations, False, claim, status_rule)
            except status.StatusProblem as exc:
                got = "refused" if expected == "refused" else f"refused: {exc}"
            if got != expected:
                mismatches.append((kind, ends, claimed, expected, got))
        return mismatches

    def test_status_rule_1(self):
        mismatches = self.run_table(1)
        self.assertEqual(mismatches[:5], [], f"{len(mismatches)} cases differ from the producer")

    def test_status_rule_2(self):
        mismatches = self.run_table(2)
        self.assertEqual(mismatches[:5], [], f"{len(mismatches)} cases differ from the producer")

    def test_status_rule_3(self):
        mismatches = self.run_table(3)
        self.assertEqual(mismatches[:5], [], f"{len(mismatches)} cases differ from the producer")

    def test_r_broad_changes_exactly_the_broad_match_rows(self):
        # Status rule 3 is status rule 2 with one change: a broadMatch formalizes, whether
        # the declaration is proved, data or a definition. Every case the two tables answer
        # differently has a broadMatch in it.
        def inputs(case):  # everything but the expected status, which is case[2]
            return json.dumps([case[0], case[1], case[3:]], sort_keys=True)

        two = {inputs(case): case[2] for case in self.table(2)["cases"]}
        differ = [case for case in self.table(3)["cases"] if two.get(inputs(case)) != case[2]]
        self.assertTrue(differ)
        for case in differ:
            # either the location is a broadMatch, or the bundle standing in for one is
            self.assertIn("broadMatch", inputs(case), case)

    def test_the_store_builder_answers_the_same(self):
        # Question Q1 of docs/PIPELINE.md: two implementations, held together here.
        for rule in (1, 2, 3):
            with self.subTest(status_rule=rule):
                mismatches = []
                for kind, ends, expected, *claimed in self.table(rule)["cases"]:
                    if expected == "refused":
                        continue
                    locations, claim = producer_case(ends, claimed[0] if claimed else None)
                    mine = status.derive(kind, locations, False, claim, rule)
                    theirs, _ = build_store.derive_status(kind, locations, claim, rule)
                    if mine != theirs:
                        mismatches.append((kind, ends, claimed, mine, theirs))
                self.assertEqual(mismatches[:5], [], f"{len(mismatches)} cases differ")


class SharedTableTests(unittest.TestCase):
    """The hand-written tables, written from the decisions, refusals included."""

    def test_the_tables_name_their_decision(self):
        expected = {1: ("cf-locate-status-parity/1", "D1"),
                    2: ("cf-locate-status-parity/2", "Q9"),
                    3: ("cf-locate-status-parity/3", "R-BROAD")}
        for rule, (schema, decision) in expected.items():
            with self.subTest(status_rule=rule):
                doc = json.loads(SHARED_TABLES[rule].read_text(encoding="utf-8"))
                self.assertEqual(doc["schema"], schema)
                self.assertEqual(doc["decision"]["id"], decision)
                self.assertEqual(doc.get("status_rule", 1), rule)
                for case in doc["cases"]:  # a case says one thing: a status, or a refusal
                    self.assertNotEqual("expected" in case, bool(case.get("refused")), case["id"])

    def run_table(self, status_rule: int) -> list:
        doc = json.loads(SHARED_TABLES[status_rule].read_text(encoding="utf-8"))
        mismatches = []
        for case in doc["cases"]:
            wanted = "refused" if case.get("refused") else case["expected"]
            got = shared_case(case, status_rule)
            if got != wanted:
                mismatches.append((case["id"], wanted, got, case.get("rule")))
        return mismatches

    def test_status_rule_1(self):
        self.assertEqual(self.run_table(1), [])

    def test_status_rule_2(self):
        self.assertEqual(self.run_table(2), [])

    def test_status_rule_3(self):
        self.assertEqual(self.run_table(3), [])

    def test_every_refusal_of_the_tables_is_a_refusal_here_too(self):
        total = 0
        for rule in (1, 2, 3):
            doc = json.loads(SHARED_TABLES[rule].read_text(encoding="utf-8"))
            for case in doc["cases"]:
                if case.get("refused"):
                    total += 1
                    self.assertEqual(shared_case(case, rule), "refused", case["id"])
        self.assertGreaterEqual(total, 22)

    def test_the_store_builder_answers_the_same(self):
        for rule in (1, 2, 3):
            doc = json.loads(SHARED_TABLES[rule].read_text(encoding="utf-8"))
            for case in doc["cases"]:
                with self.subTest(status_rule=rule, case=case["id"]):
                    self.assertEqual(shared_case(case, rule), consumer_shared_case(case, rule))

    def test_a_survived_claim_confirms_its_parts_as_the_table_says(self):
        doc = json.loads(SHARED_TABLES[1].read_text(encoding="utf-8"))
        cases = [case for case in doc["cases"] if "expected_challenges" in case]
        self.assertGreaterEqual(len(cases), 5)
        for case in cases:
            with self.subTest(case=case["id"]):
                locations = [{"location_key": hex_key(loc["location_key"]),
                              "challenge": loc["challenge"]} for loc in case["locations"]]
                coverage = case["coverage"]
                shown = locations if coverage is None else status.confirmed_parts(
                    {**coverage, "parts": [hex_key(name) for name in coverage["parts"]]}, locations)
                self.assertEqual({loc["location_key"]: loc["challenge"] for loc in shown},
                                 {hex_key(name): value
                                  for name, value in case["expected_challenges"].items()})


class RefusalTests(unittest.TestCase):
    """What the rules refuse rather than derive a status from."""

    def location(self, **over) -> dict:
        base = {"location_key": key(1), "relation": "exactMatch", "proof_status": "proved",
                "statement": False}
        base.update(over)
        return base

    def test_an_unknown_status_rule_is_refused(self):
        for bad in (0, 4, "2", 2.0, None, True):
            with self.subTest(status_rule=bad), self.assertRaises(status.StatusProblem):
                status.derive("theorem", [self.location()], False, None, bad)

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(status.StatusProblem):
            status.derive("lemma", [self.location()])

    def test_an_unknown_relation_or_proof_status_is_refused(self):
        with self.assertRaises(status.StatusProblem):
            status.derive("theorem", [self.location(relation="noMatch")])
        with self.assertRaises(status.StatusProblem):
            status.derive("theorem", [self.location(proof_status="sorry")])

    def test_the_statement_flag_on_a_proved_declaration_is_refused(self):
        with self.assertRaises(status.StatusProblem):
            status.derive("theorem", [self.location(statement=True)])
        # ... and it is fine on a definition, where it marks a Prop-valued one.
        self.assertEqual(status.derive("theorem", [self.location(proof_status="definition",
                                                                 statement=True)]),
                         "statement_only")

    def test_a_statement_flag_that_is_not_a_bool_is_refused(self):
        with self.assertRaises(status.StatusProblem):
            status.derive("theorem", [self.location(proof_status="definition", statement="yes")])
        # absent or null is false
        loc = self.location(proof_status="definition")
        del loc["statement"]
        self.assertEqual(status.derive("theorem", [loc]), "formalized")

    def test_one_location_key_on_two_locations_is_refused(self):
        with self.assertRaises(status.StatusProblem):
            status.derive("theorem", [self.location(), self.location(relation="hasPart")])

    def test_a_location_key_that_is_not_64_hex_is_refused(self):
        with self.assertRaises(status.StatusProblem):
            status.derive("theorem", [self.location(location_key="k1")])

    def test_an_absence_beside_a_counting_location_is_refused(self):
        with self.assertRaises(status.StatusProblem):
            status.derive("theorem", [self.location()], True)
        # An absence sits beside relatedMatch locations only.
        self.assertEqual(status.derive("theorem", [self.location(relation="relatedMatch")], True),
                         "not_formalized")

    def test_a_related_only_passage_has_no_status(self):
        self.assertIsNone(status.derive("theorem", [self.location(relation="relatedMatch")]))
        self.assertIsNone(status.derive("theorem", []))

    def claim(self, **over) -> dict:
        base = {"complete": True, "parts": [key(1), key(2)], "challenge": "survived"}
        base.update(over)
        return base

    def parts(self) -> list[dict]:
        return [self.location(location_key=key(1), relation="hasPart"),
                self.location(location_key=key(2), relation="hasPart")]

    def test_a_claim_the_record_format_does_not_carry_is_refused(self):
        # The three inputs parity round one found the two sides apart on, each refused.
        for over in ({"challenge": None}, {"note": "both clauses"}, {"challenge": "refuted"}):
            with self.subTest(claim=over), self.assertRaises(status.StatusProblem):
                status.derive("theorem", self.parts(), False, self.claim(**over))

    def test_a_claim_with_one_part_or_a_repeated_part_is_refused(self):
        for parts in ([key(1)], [key(1), key(1)]):
            with self.subTest(parts=parts), self.assertRaises(status.StatusProblem):
                status.derive("theorem", self.parts(), False, self.claim(parts=parts))

    def test_a_part_that_is_not_a_has_part_location_is_refused(self):
        locations = [self.location(location_key=key(1), relation="hasPart"),
                     self.location(location_key=key(2), relation="narrowMatch")]
        with self.assertRaises(status.StatusProblem):
            status.derive("theorem", locations, False, self.claim())
        with self.assertRaises(status.StatusProblem):
            status.derive("theorem", self.parts(), False, self.claim(parts=[key(1), key(9)]))

    def test_complete_false_is_refused(self):
        with self.assertRaises(status.StatusProblem):
            status.derive("theorem", self.parts(), False, self.claim(complete=False))

    def test_under_rule_2_a_bundle_needs_its_relation(self):
        with self.assertRaises(status.StatusProblem):
            status.derive("theorem", self.parts(), False, self.claim(), 2)
        self.assertEqual(status.derive("theorem", self.parts(), False,
                                       self.claim(relation="exactMatch"), 2), "formalized")

    def test_under_rule_2_a_bundles_relation_is_one_of_the_strong_three(self):
        for bad in ("hasPart", "narrowMatch", "relatedMatch", "noMatch", None):
            with self.subTest(relation=bad), self.assertRaises(status.StatusProblem):
                status.derive("theorem", self.parts(), False, self.claim(relation=bad), 2)

    def test_under_rule_1_a_bundles_extra_keys_are_refused(self):
        with self.assertRaises(status.StatusProblem):
            status.derive("theorem", self.parts(), False, self.claim(relation="exactMatch"), 1)

    def test_a_bundles_conjuncts_partition_its_parts(self):
        bundle = self.claim(relation="exactMatch",
                            conjuncts=[{"id": "c1", "text": "one", "part": key(1)},
                                       {"id": "c2", "text": "two", "part": key(2)}])
        self.assertEqual(status.derive("theorem", self.parts(), False, bundle, 2), "formalized")
        # a part that states no conjunct
        idle = {**bundle, "conjuncts": [{"id": "c1", "text": "one", "part": key(1)},
                                        {"id": "c2", "text": "two", "part": key(1)}]}
        with self.assertRaises(status.StatusProblem):
            status.derive("theorem", self.parts(), False, idle, 2)
        # ids out of order
        misnumbered = {**bundle, "conjuncts": [{"id": "c2", "text": "one", "part": key(1)},
                                               {"id": "c1", "text": "two", "part": key(2)}]}
        with self.assertRaises(status.StatusProblem):
            status.derive("theorem", self.parts(), False, misnumbered, 2)


class BasisTests(unittest.TestCase):
    """Which piece of evidence justifies the status the passage takes."""

    def loc(self, n: int, relation: str, proof_status: str = "proved") -> dict:
        return {"location_key": key(n), "relation": relation, "proof_status": proof_status,
                "statement": False, "rank": n}

    def test_the_basis_is_the_highest_ranked_location_that_reaches_the_status(self):
        locations = [self.loc(1, "hasPart"), self.loc(2, "exactMatch"), self.loc(3, "exactMatch")]
        found, basis = status.derive_with_basis("theorem", locations)
        self.assertEqual(found, "formalized")
        self.assertIs(basis, locations[1])

    def test_the_claim_is_the_basis_only_when_no_location_reaches_the_status(self):
        parts = [self.loc(1, "hasPart"), self.loc(2, "hasPart")]
        claim = {"complete": True, "parts": [key(1), key(2)], "challenge": "survived"}
        found, basis = status.derive_with_basis("theorem", parts, claim)
        self.assertEqual((found, basis), ("formalized", vocab.COVERAGE_BASIS))
        # On a tie the location is preferred.
        tied = [*parts, self.loc(3, "exactMatch")]
        found, basis = status.derive_with_basis("theorem", tied, claim)
        self.assertEqual(found, "formalized")
        self.assertIs(basis, tied[2])

    def test_a_claim_that_did_not_survive_adds_nothing(self):
        parts = [self.loc(1, "hasPart"), self.loc(2, "hasPart")]
        for challenge in ("downgraded", "not_required", "sampled_out", "challenge_pending"):
            with self.subTest(challenge=challenge):
                claim = {"complete": True, "parts": [key(1), key(2)], "challenge": challenge}
                self.assertEqual(status.derive("theorem", parts, False, claim), "partial")


if __name__ == "__main__":
    unittest.main(verbosity=2)
