#!/usr/bin/env python3
"""Tests for scripts/atlas/build_store.py, the atlas store builder (docs/ATLAS.md).

New code. Runs offline on the hand-built fixture under scripts/atlas/fixtures/:
the committed record set must equal what make_fixture.py writes, every record must
pass the vendored Literature Links schema, the committed golden store must equal a
fresh build, and each broken variant of the record set must be refused with a
message naming the rule it breaks. The Worker tests (wiki/test/atlas.test.ts) read
the same golden store, so the two languages share one contract fixture.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
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
        mismatches = []
        for kind, ends, expected, *claimed in self.table["cases"]:
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
            if claimed:  # a coverage case: {parts: [indexes into the locations], challenge}
                claim = {"complete": True, "challenge": claimed[0]["challenge"],
                         "parts": [locations[i]["location_key"] for i in claimed[0]["parts"]]}
                self.assertEqual(build_store.coverage_part_problems(claim, locations), [], (kind, ends, claimed))
            status, basis = build_store.derive_status(kind, locations, claim)
            if status != expected:
                mismatches.append((kind, ends, expected, status, claimed))
            elif status is None:
                self.assertIsNone(basis)
            elif basis == build_store.COVERAGE_BASIS:
                # The claim is the basis only when it does better than every location.
                self.assertEqual(build_store.coverage_status(kind, claim, locations), status)
                self.assertFalse(any(build_store.location_status(kind, loc) == status for loc in locations))
            else:
                # The basis is the highest-ranked location that reaches the passage's status.
                reaching = [loc for loc in locations if build_store.location_status(kind, loc) == status]
                self.assertIs(basis, reaching[0], (kind, ends))
        self.assertEqual(mismatches, [], f"{len(mismatches)} cases differ from the producer")


def _hex_key(key):
    """A shared case's location key ("k1") as the 64-hex key a record carries, one to one.

    Anything that is not a string is left as it is, so a malformed part stays malformed.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest() if isinstance(key, str) else key


def _shared_case_status(case: dict):
    """build_store.derive_passage_status on one case of the shared table: a status, None, or "refused"."""
    locations = [{**loc, "location_key": _hex_key(loc.get("location_key"))} for loc in case["locations"]]
    coverage = case["coverage"]
    if isinstance(coverage, dict) and isinstance(coverage.get("parts"), list):
        coverage = {**coverage, "parts": [_hex_key(key) for key in coverage["parts"]]}
    try:
        return build_store.derive_passage_status(case["passage_kind"], locations, case["absent"], coverage)
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
        self.refuse("names every universe library")

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
        self.refuse("libraries must be sorted by id, each once ('batteries' comes after 'batteries')")

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
                self.refuse('a checked absence names each library by a minimal lean end, {"type": "lean", "uri", '
                            '"version"} only')

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

    def test_a_receipt_count_is_an_integer(self):
        # 2.0 == 2 in Python, so a float count once passed as the list's length.
        for value in (2.0, True, "2", None):
            with self.subTest(candidates=value):
                self.fresh()
                self.edit_widget(lambda rows, value=value: rows[3]["extensions"][LOCATE]["receipt"].update(
                    candidates=value))
                self.refuse("records/enwiki-1001-2002.jsonl:4: receipt.candidates must be an integer: the length of "
                            "the candidates list in receipts/")

    def test_a_valid_span_builds_to_the_same_view(self):
        # The span and layer_sha256 are optional: a set with them builds to the same passages,
        # and the records, which the store keeps verbatim, keep them.
        before = self.article()["passages"]
        for tail, sha in ((0, True), (60, False)):
            with self.subTest(tail=tail):
                self.fresh()
                self.add_spans(tail=tail, sha=sha)
                tree = build_store.build(self.set)
                self.assertEqual(json.loads(tree["atlas/v1/articles/1001.json"])["passages"], before)
                records = [json.loads(line) for line in tree["atlas/v1/records/1001.jsonl"].decode().splitlines()]
                self.assertEqual(self.subject_ext(records[0])["span"], [0, 44 + tail])
                self.assertEqual(self.subject_ext(records[0])["layer_sha256"], self.layer_sha())
        # A tail the suffix gives in full (one character): the span's text is exact and the
        # suffix's first character, and span_sha256 must be its sha256.
        self.fresh()
        self.add_spans(tail=1, sha=False)
        self.refuse("records/enwiki-1001-2002.jsonl:1: span_sha256 is not the sha256 of the span's text, which the "
                    "quote gives in full (exact and the first 1 characters of its suffix)")

        def space_sha(rows):
            for row in rows:
                quote = row["subject"][0]["location"]["quote"]
                text = quote["exact"] + quote["suffix"][:1]
                self.subject_ext(row)["span_sha256"] = hashlib.sha256(text.encode()).hexdigest()
        self.edit_widget(space_sha)
        self.assertEqual(self.article()["passages"], before)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
