#!/usr/bin/env python3
"""Tests for the promoter: an accepted set becomes reviewable overlay rows, and no more.

New code. Everything runs against the **synthetic** ``/2`` record set in
``scripts/pipeline/fixtures/recordset`` (three fictional libraries, two fictional
Wikipedia pages) and against an in-memory SQLite standing in for D1's shape. No
network, no D1, no R2, no Worker, and no real article.

What is checked:

* the rows' **shape**: one per located pair, keyed by ``pair_key``, carrying the
  passage, the repository and the declaration the pair is made of;
* what is **not** a row: a checked absence, an unresolved passage, and a ``/1`` set —
  each named in the report rather than dropped;
* **state** is ``pending`` and goes to ``live`` only for a trusted submitter;
* the generated SQL **parses and runs** against a table with the plan's columns, and
  its ``ON CONFLICT`` clause **supersedes a pipeline row and leaves a human's alone** —
  which is the single rule section 3.3 calls the most important in it;
* a set the intake contract refuses is **not promoted at all**;
* nothing is written to D1: the command's whole output is files.

Run: python3 scripts/articles/test_promote_submission.py
"""
from __future__ import annotations

import io
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for _path in (str(HERE), str(ROOT / "scripts" / "pipeline")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import promote_submission as promoter  # noqa: E402

FIXTURE = ROOT / "scripts" / "pipeline" / "fixtures" / "recordset"
FIXTURE_V1 = ROOT / "scripts" / "articles" / "fixtures" / "recordset"
AT = "2026-11-06T00:00:00Z"


def quiet(call, *args, **kwargs):
    with io.StringIO() as sink, redirect_stdout(sink):
        return call(*args, **kwargs)


def create_table(conn: sqlite3.Connection) -> None:
    """A stand-in for migration 0019, built from the promoter's own column list.

    This is **not** R1's migration — it does not exist yet. It is the promoter's
    assumption about it, made executable: if R1 names a column differently the
    promoter's SQL fails there, and this test is what keeps the two lists in one place.
    """
    columns = ", ".join(
        f"{name} INTEGER" if name in ("pageid", "version") else f"{name} TEXT"
        for name in promoter.COLUMNS)
    conn.execute(f"CREATE TABLE {promoter.TABLE} ({columns}, PRIMARY KEY (pair_key))")
    conn.execute(f"CREATE INDEX ix_tags_page ON {promoter.TABLE} (pageid, state)")
    conn.execute(f"CREATE INDEX ix_tags_actor ON {promoter.TABLE} (added_by, created_at)")
    conn.execute(f"CREATE INDEX ix_tags_state ON {promoter.TABLE} (state, created_at)")


class PromoteCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wl-promote-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def promote(self, *sets, **kwargs):
        options = {"added_by": "u-123", "created_at": AT, "machine_id": "test-mac"}
        options.update(kwargs)
        return promoter.promote([Path(s) for s in (sets or (FIXTURE,))], **options)


class TestRows(PromoteCase):
    def test_one_row_per_located_pair_keyed_by_pair_key(self):
        doc = self.promote()
        rows = doc["rows"]
        self.assertTrue(rows)
        self.assertEqual(len(rows), len({row["pair_key"] for row in rows}))
        for row in rows:
            self.assertEqual(sorted(row), sorted(promoter.COLUMNS))
            self.assertEqual(len(row["pair_key"]), 64)
            self.assertEqual(len(row["passage_key"]), 64)
            self.assertTrue(row["repository"].startswith("https://"))
            self.assertTrue(row["declaration"])
            self.assertEqual(row["origin"], promoter.ORIGIN)
            self.assertEqual(row["version"], promoter.FIRST_VERSION)
            self.assertEqual(row["created_at"], AT)
            self.assertEqual(row["updated_at"], AT)
            self.assertIsNone(row["removed_by"])
            self.assertIsNone(row["removed_at"])
            self.assertIsNone(row["removed_reason"])
            # what a community row overrides is read from the live store by R1
            self.assertIsNone(row["base_relation"])

    def test_the_relation_is_the_store_s_own_vocabulary(self):
        import vocab  # the pipeline's, which is where the store's values come from

        for row in self.promote()["rows"]:
            self.assertIn(row["relation"], vocab.LOCATION_RELATIONS)

    def test_attribution_rides_on_every_row(self):
        doc = self.promote(token_id="mt-7", model="a-model", run_id="loc-7",
                           actor_type="agent", via="submission")
        for row in doc["rows"]:
            self.assertEqual(row["added_by"], "u-123")
            self.assertEqual(row["actor_type"], "agent")
            self.assertEqual(row["via"], "submission")
            self.assertEqual(row["token_id"], "mt-7")
            self.assertEqual(row["model"], "a-model")
            self.assertEqual(row["run_id"], "loc-7")

    def test_the_run_id_is_read_off_a_campaign_token_when_one_names_it(self):
        self.assertEqual(promoter.run_of("run:loc-7"), "loc-7")
        self.assertEqual(promoter.run_of("run:loc-7+chal-7"), "loc-7")
        self.assertIsNone(promoter.run_of("fixture-2026-11"))
        # the fixture's campaign names no run, so the column is NULL rather than guessed
        self.assertTrue(all(row["run_id"] is None for row in self.promote()["rows"]))

    def test_the_set_s_own_pins_ride_on_every_row(self):
        doc = self.promote()
        seal = doc["sets"][0]
        for row in doc["rows"]:
            self.assertEqual(row["campaign"], seal["campaign"])
            self.assertEqual(row["universe_sha256"], seal["universe_sha256"])
            # record_set is the seal's sha256, which is what the articles plane already
            # puts on a feedback row
            self.assertEqual(row["record_set"], seal["seal_sha256"])


class TestState(PromoteCase):
    def test_an_untrusted_submitter_s_rows_are_pending(self):
        doc = self.promote()
        self.assertEqual(doc["state"], promoter.STATE_PENDING)
        self.assertTrue(all(row["state"] == "pending" for row in doc["rows"]))

    def test_a_trusted_submitter_s_rows_are_live(self):
        doc = self.promote(trusted=True)
        self.assertEqual(doc["state"], promoter.STATE_LIVE)
        self.assertTrue(all(row["state"] == "live" for row in doc["rows"]))


class TestWhatIsNotARow(PromoteCase):
    def test_a_checked_absence_is_named_and_not_promoted(self):
        doc = self.promote()
        said = " ".join(doc["not_promoted"])
        self.assertIn("a checked absence", said)
        self.assertIn("no pair_key", said)

    def test_an_unresolved_passage_is_named_and_not_promoted(self):
        self.assertIn("unresolved", " ".join(self.promote()["not_promoted"]))

    def test_a_v1_set_is_refused_by_the_intake_contract_and_nothing_is_promoted(self):
        with self.assertRaises(promoter.PromotionProblem) as caught:
            self.promote(FIXTURE_V1)
        self.assertIn("not an accepted submission", str(caught.exception))
        self.assertIn("schema_not_accepted", str(caught.exception))

    def test_a_set_that_is_not_a_set_is_refused(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        with self.assertRaises(promoter.PromotionProblem):
            self.promote(empty)


class TestDeduplication(PromoteCase):
    def rows(self, pair, **over):
        base = {name: None for name in promoter.COLUMNS}
        base.update({"pair_key": pair, "pageid": 1, "passage_key": "p" * 64,
                     "library": "l", "repository": "https://example.invalid/r",
                     "declaration": "D", "relation": "exactMatch", "note": None,
                     "record_set": "set-a", "_source_id": "s", "_item": "i",
                     "_rank": 1})
        base.update(over)
        return base

    def test_two_sets_at_one_pair_make_one_row_and_the_later_wins(self):
        kept, superseded, refusals = promoter.deduplicate([
            self.rows("a" * 64, record_set="set-a", relation="closeMatch"),
            self.rows("a" * 64, record_set="set-b", relation="exactMatch"),
        ])
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["relation"], "exactMatch")
        self.assertEqual(len(superseded), 1)
        self.assertEqual(refusals, [])

    def test_one_set_stating_two_judgements_for_one_pair_promotes_neither(self):
        kept, _superseded, refusals = promoter.deduplicate([
            self.rows("a" * 64, relation="closeMatch"),
            self.rows("a" * 64, relation="broadMatch"),
            self.rows("b" * 64),
        ])
        self.assertEqual([row["pair_key"] for row in kept], ["b" * 64])
        self.assertEqual(len(refusals), 1)
        self.assertIn("two judgements", refusals[0])

    def test_a_third_row_at_a_refused_pair_does_not_revive_it(self):
        kept, _superseded, refusals = promoter.deduplicate([
            self.rows("a" * 64, relation="closeMatch"),
            self.rows("a" * 64, relation="broadMatch"),
            self.rows("a" * 64, relation="exactMatch"),
        ])
        self.assertEqual(kept, [])
        self.assertEqual(len(refusals), 1)

    def test_one_set_repeating_an_identical_row_is_not_a_conflict(self):
        kept, _superseded, refusals = promoter.deduplicate([
            self.rows("a" * 64), self.rows("a" * 64)])
        self.assertEqual(len(kept), 1)
        self.assertEqual(refusals, [])


class TestTheSql(PromoteCase):
    def setUp(self):
        super().setUp()
        self.doc = self.promote(trusted=True)
        self.sql = promoter.sql_for(self.doc["rows"], added_by=self.doc["added_by"],
                                    state=self.doc["state"], sets=1,
                                    provenance=self.doc["provenance"])
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        create_table(self.conn)

    def test_it_runs_against_the_plan_s_table_and_inserts_every_row(self):
        self.conn.executescript(self.sql)
        count, = self.conn.execute(
            f"SELECT count(*) FROM {promoter.TABLE}").fetchone()
        self.assertEqual(count, len(self.doc["rows"]))

    def test_running_it_twice_is_one_row_per_pair_with_a_bumped_version(self):
        self.conn.executescript(self.sql)
        self.conn.executescript(self.sql)
        count, = self.conn.execute(
            f"SELECT count(*) FROM {promoter.TABLE}").fetchone()
        self.assertEqual(count, len(self.doc["rows"]))
        versions = {row[0] for row in self.conn.execute(
            f"SELECT version FROM {promoter.TABLE}")}
        self.assertEqual(versions, {promoter.FIRST_VERSION + 1})

    def test_it_supersedes_a_pipeline_row(self):
        pair = self.doc["rows"][0]["pair_key"]
        self.conn.execute(
            f"INSERT INTO {promoter.TABLE} (pair_key, origin, relation, version) "
            "VALUES (?, 'pipeline', 'broadMatch', 4)", (pair,))
        self.conn.executescript(self.sql)
        origin, relation, version = self.conn.execute(
            f"SELECT origin, relation, version FROM {promoter.TABLE} WHERE pair_key = ?",
            (pair,)).fetchone()
        self.assertEqual(origin, promoter.ORIGIN)
        self.assertEqual(relation, self.doc["rows"][0]["relation"])
        self.assertEqual(version, 5)

    def test_it_never_overwrites_a_human_s_edit(self):
        """Section 3.3's most important line, enforced by the generated statement."""
        pair = self.doc["rows"][0]["pair_key"]
        self.conn.execute(
            f"INSERT INTO {promoter.TABLE} (pair_key, origin, relation, added_by, "
            "version) VALUES (?, 'community', 'narrowMatch', 'a-human', 9)", (pair,))
        self.conn.executescript(self.sql)
        origin, relation, added_by, version = self.conn.execute(
            f"SELECT origin, relation, added_by, version FROM {promoter.TABLE} "
            "WHERE pair_key = ?", (pair,)).fetchone()
        self.assertEqual((origin, relation, added_by, version),
                         ("community", "narrowMatch", "a-human", 9))

    def test_it_never_resurrects_a_row_a_moderator_removed(self):
        """A tombstone is not a row to update: the three removed_* columns are not in
        the update at all, and the update does not run against a removed row."""
        pair = self.doc["rows"][0]["pair_key"]
        self.conn.execute(
            f"INSERT INTO {promoter.TABLE} (pair_key, origin, state, relation, "
            "removed_by, removed_at, removed_reason, version) VALUES "
            "(?, 'contributed', 'removed', 'broadMatch', 'a-moderator', "
            "'2026-11-01T00:00:00Z', 'spam', 3)", (pair,))
        self.conn.executescript(self.sql)
        state, removed_by, reason, version = self.conn.execute(
            f"SELECT state, removed_by, removed_reason, version FROM {promoter.TABLE} "
            "WHERE pair_key = ?", (pair,)).fetchone()
        self.assertEqual((state, removed_by, reason, version),
                         ("removed", "a-moderator", "spam", 3))
        self.assertNotIn("removed_by = excluded", self.sql)
        self.assertNotIn("removed_at = excluded", self.sql)
        self.assertNotIn("removed_reason = excluded", self.sql)

    def test_a_row_with_no_origin_is_not_superseded_either(self):
        pair = self.doc["rows"][0]["pair_key"]
        self.conn.execute(
            f"INSERT INTO {promoter.TABLE} (pair_key, relation, version) "
            "VALUES (?, 'narrowMatch', 2)", (pair,))
        self.conn.executescript(self.sql)
        relation, version = self.conn.execute(
            f"SELECT relation, version FROM {promoter.TABLE} WHERE pair_key = ?",
            (pair,)).fetchone()
        self.assertEqual((relation, version), ("narrowMatch", 2))

    def test_the_header_says_nothing_has_been_applied(self):
        self.assertIn("NOTHING HERE HAS BEEN APPLIED", self.sql)
        self.assertIn("BEGIN TRANSACTION;", self.sql)
        self.assertTrue(self.sql.rstrip().endswith("COMMIT;"))

    def test_a_quote_in_a_value_is_escaped_and_a_control_character_is_refused(self):
        self.assertEqual(promoter.sql_literal("it's"), "'it''s'")
        self.assertEqual(promoter.sql_literal(None), "NULL")
        self.assertEqual(promoter.sql_literal(12), "12")
        with self.assertRaises(promoter.PromotionProblem):
            promoter.sql_literal("a\x00b")
        with self.assertRaises(promoter.PromotionProblem):
            promoter.sql_literal(1.5)

    def test_every_statement_names_the_provenance_of_its_row(self):
        for pair, where in self.doc["provenance"].items():
            self.assertIn(f"-- {where['source_id']} {where['item']} ", self.sql)
            self.assertIn(pair, self.sql)

    def test_every_comment_line_in_the_file_is_a_comment_line(self):
        """No value can end the comment it is written into and start a statement."""
        for line in self.sql.splitlines():
            if line.startswith("--"):
                continue
            self.assertFalse(line.lstrip().startswith("DROP"), line)

    def test_a_line_break_in_a_declaration_is_refused_before_anything_is_written(self):
        """The injection this guard exists for: a newline ends the `-- ` comment, and
        whatever follows it is a top-level statement in a file somebody runs on D1."""
        rows = [dict(self.doc["rows"][0])]
        rows[0]["declaration"] = "Foo.bar\nDROP TABLE article_tags; --"
        with self.assertRaises(promoter.PromotionProblem) as caught:
            promoter.sql_for(rows, added_by="u", state="pending", sets=1)
        self.assertIn("line break", str(caught.exception))
        self.assertIn("declaration", str(caught.exception))

    def test_a_line_break_is_refused_in_every_structural_column(self):
        for name in ("library", "repository", "relation", "added_by", "campaign"):
            rows = [dict(self.doc["rows"][0])]
            rows[0][name] = "a\nb"
            with self.subTest(column=name), \
                    self.assertRaises(promoter.PromotionProblem):
                promoter.sql_for(rows, added_by="u", state="pending", sets=1)

    def test_a_note_may_hold_a_line_break_and_is_never_put_in_a_comment(self):
        """`note` is the one free-text column; it is quoted, never interpolated."""
        rows = [dict(self.doc["rows"][0])]
        rows[0]["note"] = "two lines\nof convention difference"
        sql = promoter.sql_for(rows, added_by="u", state="pending", sets=1)
        self.assertIn("'two lines\nof convention difference'", sql)
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        create_table(conn)
        conn.executescript(sql)
        note, = conn.execute(
            f"SELECT note FROM {promoter.TABLE}").fetchone()
        self.assertEqual(note, "two lines\nof convention difference")

    def test_a_control_character_anywhere_is_refused(self):
        rows = [dict(self.doc["rows"][0])]
        rows[0]["note"] = "a\x01b"
        with self.assertRaises(promoter.PromotionProblem) as caught:
            promoter.sql_for(rows, added_by="u", state="pending", sets=1)
        self.assertIn("control character", str(caught.exception))


class TestCommandLine(PromoteCase):
    def argv(self, *extra):
        return ["--record-set", str(FIXTURE), "--added-by", "u-123",
                "--created-at", AT, "--machine-id", "test-mac", *extra]

    def test_it_writes_both_files_and_exits_one_when_something_was_not_promoted(self):
        sql, out = self.tmp / "rows.sql", self.tmp / "rows.json"
        code = quiet(promoter.main, self.argv("--sql", str(sql), "--json", str(out)))
        self.assertEqual(code, 1)  # the fixture has an absence and two unresolved
        self.assertTrue(sql.is_file())
        doc = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(doc["schema"], promoter.PROMOTION_SCHEMA)
        self.assertFalse(doc["applied"])
        self.assertFalse(doc["writes_to_d1"])
        self.assertEqual(doc["table"], promoter.TABLE)
        self.assertEqual(doc["columns"], list(promoter.COLUMNS))

    def test_it_refuses_to_run_without_an_output_file(self):
        self.assertEqual(quiet(promoter.main, self.argv()), 2)

    def test_a_refused_set_exits_two_and_writes_nothing(self):
        sql = self.tmp / "nope.sql"
        code = quiet(promoter.main, ["--record-set", str(FIXTURE_V1),
                                     "--added-by", "u-1", "--created-at", AT,
                                     "--sql", str(sql)])
        self.assertEqual(code, 2)
        self.assertFalse(sql.exists())

    def test_a_missing_submitter_is_refused(self):
        with self.assertRaises(promoter.PromotionProblem):
            self.promote(added_by="")

    def test_a_bad_clock_is_refused(self):
        with self.assertRaises(promoter.PromotionProblem):
            self.promote(created_at="now")

    def test_a_bad_actor_type_is_refused(self):
        with self.assertRaises(promoter.PromotionProblem):
            self.promote(actor_type="robot")

    def test_the_report_says_nothing_was_written_to_d1(self):
        with io.StringIO() as sink, redirect_stdout(sink):
            promoter.main(self.argv("--json", str(self.tmp / "a.json")))
            self.assertIn("nothing was written to D1", sink.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
