#!/usr/bin/env python3
"""brain_d1_import against a local SQLite database standing in for D1.

The stand-in is created from the migrations in wiki/migrations-brain, with foreign keys on as D1
has them. A fake runner takes each `wrangler d1 execute --command` the importer builds, splits
it into statements the way Wrangler does, executes them, and answers Wrangler's --json shape.
It can apply a call and still report failure, fail a call before applying it, crash the
importer at any call (optionally after applying part of it), and tamper with rows. Fixtures
are built by brain/build_serving_db.py from its own test fixture.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import random
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "brain"))

import brain_d1_import as bdi  # noqa: E402
import brain_deploy_journal as journal  # noqa: E402
import build_serving_db as bsd  # noqa: E402
import test_build_serving_db as fixture  # noqa: E402
from brain_promote_release import RunResult  # noqa: E402

MIGRATION = REPO / "wiki" / "migrations-brain" / "0001_brain.sql"
EXACT_INDEX_MIGRATION = REPO / "wiki" / "migrations-brain" / "0002_name_keys_exact.sql"
EXPLORER_MIGRATION = REPO / "wiki" / "migrations-brain" / "0003_explorer.sql"
# The Worker tests' port of the builder's row logic (and of name_parts), checked here
ROWS_PORT = REPO / "wiki" / "test" / "helpers" / "brain-d1-rows.ts"
TARGET = "https://example.test"
BUDGET = 2000
LIVE_A = "sha256:" + "a" * 64
LIVE_B = "sha256:" + "b" * 64


class Crash(BaseException):
    """The importer process dying between (or inside) calls."""


def split_sql(sql: str) -> list[str]:
    """Split on semicolons outside quoted strings and identifiers, dropping -- comments, as
    Wrangler's splitter does."""
    statements, current, i, n = [], [], 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in "'\"":
            j = i + 1
            while j < n:
                if sql[j] == ch:
                    if j + 1 < n and sql[j + 1] == ch:
                        j += 2
                        continue
                    break
                j += 1
            current.append(sql[i:j + 1])
            i = j + 1
            continue
        if sql.startswith("--", i):
            while i < n and sql[i] != "\n":
                i += 1
            continue
        if ch == ";":
            statements.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    statements.append("".join(current))
    return [s.strip() for s in statements if s.strip()]


class FakeD1:
    """Executes each --command against a local database created from the migrations."""

    def __init__(self, *, migrate: bool = True) -> None:
        self.db = sqlite3.connect(":memory:", isolation_level=None)
        self.db.execute("PRAGMA foreign_keys = ON")
        if migrate:
            for name in bdi.MIGRATIONS[bdi.SUPPORTED_SCHEMA_VERSIONS[-1]]:
                self.db.executescript((MIGRATION.parent / name).read_text())
        self.calls = 0
        self.commands: list[list[str]] = []
        self.statements: list[str] = []
        self.fail_before: set[int] = set()
        self.fail_after: set[int] = set()
        self.crash_at: tuple[int, int] | None = None   # (call, statements applied first)
        self.tamper = None          # callable(fake, statements) after a call applies
        self.reject = None          # callable(statement) -> error text: the call fails, applying nothing
        self.duration = 0.5         # meta.duration of every statement, in ms
        self.verified: dict[str, str] = {}   # manifest path -> release id for verify_release
        self.records: list[dict] = []

    def log(self, record: dict) -> None:
        self.records.append(record)

    def run(self, args, *, cwd, timeout, env):
        args = [str(a) for a in args]
        if args[1].endswith("verify_release.py"):
            manifest = args[args.index("--manifest") + 1]
            body = json.dumps({"ok": True, "release_id": self.verified[manifest]})
            return RunResult(tuple(args), 0, body.encode(), b"")
        assert args[2:8] == ["d1", "execute", "wikilean-brain", "--remote", "--json", "--config"], args
        assert args[9] == "--command" and len(args) == 11, args
        self.commands.append(args)
        self.calls += 1
        statements = split_sql(args[10])
        if self.crash_at is not None and self.crash_at[0] == self.calls:
            for statement in statements[: self.crash_at[1]]:
                self.db.execute(statement)
            raise Crash(f"crash at call {self.calls}")
        if self.calls in self.fail_before:
            return RunResult(tuple(args), 1, b"", b"D1_ERROR: network")
        errors = [self.reject(s) for s in statements] if self.reject is not None else []
        if any(errors):
            return RunResult(tuple(args), 1, b"", next(e for e in errors if e).encode())
        results = []
        for statement in statements:
            self.statements.append(statement)
            cursor = self.db.execute(statement)
            rows = ([dict(zip([c[0] for c in cursor.description], r)) for r in cursor.fetchall()]
                    if cursor.description else [])
            pages = self.db.execute("PRAGMA page_count").fetchone()[0]
            results.append({"results": rows, "success": True, "meta": {
                "duration": self.duration, "changes": max(cursor.rowcount, 0), "rows_written": max(cursor.rowcount, 0),
                "size_after": pages * 4096}})
        if self.tamper is not None:
            self.tamper(self, statements)
        if self.calls in self.fail_after:
            return RunResult(tuple(args), 1, b"", b"Error: fetch failed")
        return RunResult(tuple(args), 0, json.dumps(results).encode(), b"")

    def one(self, sql, *params):
        return self.db.execute(sql, params).fetchone()

    def all(self, sql, *params):
        return self.db.execute(sql, params).fetchall()


class Tree:
    """The attributes test_build_serving_db.ServingDbTest.write_tree uses."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.cells = tmp / "cells"
        self.nodes = tmp / "nodes.jsonl"


EXTRA_LABELS = (
    # a long row (its aka column alone exceeds the budget) with NULs, quotes and non-ASCII
    [{"id": "cell:Q20", "label": "O'Brien\x00lemma",
      "aka": [f"alias {i} it's \x00 ünïcödé \U0001d53d '{i}'" for i in range(60)]},
     {"id": "cell:Q21", "label": "Long note"}]
    + [{"id": f"cell:F{i:02}", "label": f"Filler {i:02} – quote's"} for i in range(40)])


def build_fixture(tmp: Path, *, name: str = "serving", extra: list[dict] = EXTRA_LABELS,
                  description: str | None = None) -> tuple[Path, Path]:
    """A serving file and sidecar from build_serving_db's fixture plus rows that stress the importer."""
    tree = Tree(tmp)
    labels = list(fixture.LABELS) + list(extra)
    shards = {key: dict(rows) for key, rows in fixture.SHARDS.items()}
    shards["extra"] = {r["id"]: fixture.entry(r["label"], [0.25, 0.5]) for r in extra}
    if "cell:Q21" in shards["extra"]:
        shards["extra"]["cell:Q21"]["cell"]["note"] = "q'uote ü \U0001d53d \\ \"x\" " * 300
    nodes = [dict(n) for n in fixture.NODES]
    if description is not None:
        next(n for n in nodes if n.get("id") == "Q1")["description"] = description
    fixture.ServingDbTest.write_tree(tree, shards=shards, labels=labels, nodes=nodes)
    serving, sidecar = tmp / f"{name}.sqlite3", tmp / f"{name}-tables.json"
    bsd.build(tree.cells, tree.nodes, serving, sidecar_path=sidecar)
    return serving, sidecar


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls.tmpdir.name).resolve()
        cls.serving, cls.sidecar = build_fixture(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    def setUp(self):
        self.work = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.repo = self.work / "repo"
        self.repo.mkdir(mode=0o700)
        self.root = journal.initialize_target_receipt_root(self.work / "receipts", self.repo, TARGET)
        self.live = [None, None]
        self.local = bdi.open_local(self.serving, self.sidecar, full_check=True)
        self.addCleanup(self.local.close)

    def selector(self):
        return tuple(self.live)

    @staticmethod
    def _resume(fake):
        fake.fail_before, fake.fail_after, fake.crash_at, fake.tamper = set(), set(), None, None
        return fake

    def importer(self, fake, *, execute=True, budget=BUDGET, ranges=3, attempts=3, root="default",
                 call_bytes=None, sleep=lambda seconds: None):
        call_bytes = call_bytes or budget * (ranges + 1) + 4096
        d1 = bdi.D1(repo=REPO, database="wikilean-brain", config=Path("/nowhere/wrangler.json"),
                    runner=fake, statement_bytes=budget, call_bytes=call_bytes, attempts=attempts,
                    node=Path("node"), wrangler=Path("wrangler.js"), sleep=sleep, log=fake.log)
        options = bdi.Options(execute=execute, statement_bytes=budget, call_bytes=call_bytes,
                              ranges_per_call=ranges)
        return bdi.Importer(d1, options=options, selector=self.selector,
                            receipt_root=self.root if root == "default" else root)

    def do_import(self, fake, local=None, release_id=None, names_mode=None, **kwargs):
        local = local or self.local
        importer = self.importer(fake, **kwargs)
        with importer.locked():
            return importer.import_dataset(local, release_id or bdi.SCRATCH_PREFIX + local.dataset_id, "f" * 64,
                                           names_mode)

    def do_prune(self, fake, **kwargs):
        importer = self.importer(fake, **kwargs)
        with importer.locked():
            return importer.prune()

    def do_supersede(self, fake, release_id, **kwargs):
        importer = self.importer(fake, **kwargs)
        with importer.locked():
            return importer.supersede(release_id)

    def ds(self, fake, local=None):
        row = fake.one("SELECT ds FROM datasets WHERE dataset_id = ?", (local or self.local).dataset_id)
        return None if row is None else row[0]

    def assert_imported(self, fake, local=None):
        """Every copied column of every table equals the local file, and the FTS tables are whole."""
        local = local or self.local
        ds = self.ds(fake, local)
        self.assertEqual(fake.one("SELECT state FROM datasets WHERE ds = ?", ds), ("ready",))
        for table, columns in bdi.D1_COLUMNS.items():
            want = local.db.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY seq").fetchall()
            got = fake.all(f"SELECT {', '.join(columns)} FROM {table} WHERE ds = ? ORDER BY seq", ds)
            self.assertEqual(got, want, table)
        for kind, (source, column) in bdi.FTS_TABLES.items():
            name = bdi.fts_table(kind, local.hex)
            self.assertEqual([r[0] for r in fake.all(f"SELECT id FROM {name}_docsize ORDER BY id")],
                             list(range(1, local.rows(source) + 1)), kind)
        hits = fake.all(f"SELECT rowid FROM names_{local.hex} WHERE names_{local.hex} MATCH ? ORDER BY rowid",
                        '"brien"')
        if fake.one("SELECT names_mode FROM datasets WHERE ds = ?", ds) == ("trigram",):
            self.assertEqual(hits, local.db.execute("SELECT rowid FROM names WHERE names MATCH ? ORDER BY rowid",
                                                    ('"brien"',)).fetchall())
        self.assertTrue(hits)
        covered = {}
        for tbl, lo, hi in fake.all("SELECT tbl, lo, hi FROM import_ranges WHERE ds = ? ORDER BY tbl, lo", ds):
            covered.setdefault(tbl, []).append((lo, hi))
        for name in (*bdi.DATA_TABLES, *bdi.FTS_TABLES):
            spans = covered.get(name, [])
            self.assertEqual(bdi.gaps(spans, local.rows(name)), [], name)
            self.assertEqual(sum(hi - lo + 1 for lo, hi in spans), local.rows(name), name)

    def control(self, fake):
        return (fake.all("SELECT dataset_id, schema_version, tables_json, longest_name, sup_names_from, "
                         "names_mode, state FROM datasets ORDER BY dataset_id"),
                fake.all("SELECT release_id, d.dataset_id, r.state FROM releases r JOIN datasets d USING (ds) "
                         "ORDER BY release_id"))

    def incomplete_attempt(self, final=False, staged=None, predeploy=None):
        with journal.PromotionLock(self.root):
            attempt = journal.EventJournal.create_with_intent(self.root, f"attempt-{secrets.token_hex(4)}", {
                "staged_selector": {"release_id": staged, "previous_release_id": None},
                "predeploy": {"release_id": predeploy, "previous_release_id": None}})
            if final:
                attempt.append("final_state", {"outcome": "no_production_change"})
        return journal.EventJournal.load(attempt.attempt_dir)


class EscapingTest(unittest.TestCase):
    CASES = ["", "plain", "it's", "''", "a\x00b", "\x00", "\x00\x00lead", "trail\x00", "O'Brien\x00'x'",
             "Cauchy–Schwarz", "Ångström", "\U0001d53dield", "line\nbreak\r\ttab", "back\\slash",
             'double "quote"', "semi;colon -- not a comment /* nor this */", " ﻿‮",
             "\x00" * 3000 + "'x;--$$\\'"]     # far past SQLite's expression depth of 1000

    def test_literals_round_trip_through_sqlite(self):
        db = sqlite3.connect(":memory:")
        for value in self.CASES:
            literal = bdi.sql_literal(value)
            self.assertNotIn("\x00", literal)
            self.assertEqual(bdi.literal_bytes(value), len(literal.encode("utf-8")), value)
            got, kind = db.execute(f"SELECT {literal}, typeof({literal})").fetchone()
            self.assertEqual((got, kind), (value, "text"), value)
            # the statement splitter sees one statement however the value looks
            self.assertEqual(split_sql(f"SELECT {literal}; SELECT 1"), [f"SELECT {literal}", "SELECT 1"])
        for value in (0, 7, -3, bdi.MAX_JSON_INT, -bdi.MAX_JSON_INT):
            self.assertEqual(db.execute(f"SELECT {bdi.sql_literal(value)}").fetchone(), (value,))
        self.assertEqual(bdi.sql_literal(None), "NULL")
        # a string with NUL is one node however many NULs it holds; without NUL it stays quoted
        self.assertEqual(bdi.sql_literal("a\x00'"), "CAST(X'610027' AS TEXT)")
        self.assertEqual(bdi.sql_literal("it's"), "'it''s'")

    def test_refuses_what_does_not_round_trip(self):
        for value in (1.5, 12.0, True, b"bytes", bdi.MAX_JSON_INT + 1, "\ud800", ["list"]):
            with self.assertRaises(bdi.D1ImportError, msg=repr(value)):
                bdi.sql_literal(value)

    def test_chunks_fit_their_budget_and_rejoin(self):
        for text in ("a'\x00é\U0001d53d" * 50, "a'é\U0001d53d" * 50):     # hex form, quoted form
            for budget in (25, 26, 27, 40, 333):
                pos, parts = 0, []
                while pos < len(text):
                    end = bdi.chunk_end(text, pos, budget)
                    self.assertGreater(end, pos)
                    self.assertLessEqual(len(bdi.sql_literal(text[pos:end]).encode()), budget)
                    if end < len(text):   # maximal: one more character would not fit
                        self.assertGreater(len(bdi.sql_literal(text[pos:end + 1]).encode()), budget)
                    parts.append(text[pos:end])
                    pos = end
                self.assertEqual("".join(parts), text)

    def test_identifiers_are_fixed_names_or_checked_hex(self):
        self.assertEqual(bdi.fts_table("names", "0" * 64), "names_" + "0" * 64)
        for kind, hex_id in (("names", "0" * 63), ("names", "G" * 64), ("cells", "0" * 64),
                             ("texts", "0" * 64 + "; DROP TABLE x")):
            with self.assertRaises(bdi.D1ImportError):
                bdi.fts_table(kind, hex_id)
        with self.assertRaises(bdi.D1ImportError):
            bdi.dataset_hex("sha256:" + "0" * 63)
        with self.assertRaises(bdi.D1ImportError):
            bdi.checked_table("labels; --")

    def test_name_parts(self):
        self.assertEqual(bdi.name_parts("TauCeti.StronglyContinuousSemigroup.mk"),
                         "Tau Ceti Strongly Continuous Semigroup mk")
        self.assertEqual(bdi.name_parts("Mathlib/Analysis/inner_product"), "Mathlib Analysis inner product")


class MigrationTest(Base):
    def test_the_migration_list_is_the_directory(self):
        self.assertEqual(sorted(p.name for p in MIGRATION.parent.glob("*.sql")), list(bdi.MIGRATIONS[2]))
        self.assertEqual(bdi.MIGRATIONS[1], bdi.MIGRATIONS[2][:2])
        self.assertEqual(sorted(bdi.MIGRATIONS), sorted(bdi.REQUIRED_COLUMNS))

    def test_migration_matches_the_builder_minus_two_columns(self):
        fake = FakeD1()
        for path in (MIGRATION, EXPLORER_MIGRATION):         # idempotent: a rerun changes nothing
            fake.db.executescript(path.read_text())
        for table, columns in bdi.D1_COLUMNS.items():
            d1 = [r[1] for r in fake.all(f"PRAGMA table_info({table})")]
            self.assertEqual(d1, ["ds", *columns], table)
            local = [r[1] for r in self.local.db.execute(f"PRAGMA table_info({table})")]
            self.assertEqual(local, [*columns, *bdi.LOCAL_ONLY.get(table, ())], table)
            self.assertIn((table,), fake.all("SELECT tbl_name FROM sqlite_master WHERE type = 'index' "
                                             "AND sql IS NULL AND tbl_name = ?", table))   # UNIQUE(ds, seq)
        for version in (1, 2):
            for table, columns in bdi.REQUIRED_COLUMNS[version].items():
                self.assertEqual({r[1] for r in fake.all(f"PRAGMA table_info({table})")}, set(columns), table)
        self.assertEqual(set(bdi.REQUIRED_COLUMNS[2]) - set(bdi.REQUIRED_COLUMNS[1]),
                         {"explorer_edges", "explorer_scopes"})
        indexes = {r[0]: r[1] for r in fake.all("SELECT name, sql FROM sqlite_master WHERE type = 'index' "
                                                "AND sql IS NOT NULL")}
        self.assertIn("xrefs(ds, node, page_ord)", indexes["xrefs_node"])
        self.assertIn("supercells(ds, parent, ord)", indexes["supercells_parent"])
        self.assertIn("explorer_edges(ds, b)", indexes["explorer_edges_b"])
        # the builder's local copy has the same index, without ds
        self.assertEqual([r[2] for r in self.local.db.execute("PRAGMA index_info(explorer_edges_b)")], ["b"])

    def test_migration_text_survives_command_and_statement_splitting(self):
        for path in (MIGRATION, EXPLORER_MIGRATION):
            text = path.read_text()
            self.assertIn("--command", text.split("\n\n")[0])
            self.assertIn("NEVER --file", text)
            # comments carry no quote or semicolon, so even a splitter blind to comments agrees
            for line in text.splitlines():
                comment = line.split("--", 1)[1] if "--" in line else ""
                self.assertNotIn("'", comment, line)
                self.assertNotIn(";", comment, line)
            statements = split_sql(text)
            self.assertEqual(len(statements), text.count(";"))
            self.assertTrue(all(s.startswith(("CREATE TABLE IF NOT EXISTS", "CREATE INDEX IF NOT EXISTS"))
                                for s in statements), statements)
        # 0003 adds the two explorer tables and one index on a table it creates, and nothing else
        statements = split_sql(EXPLORER_MIGRATION.read_text())
        self.assertEqual([" ".join(s.split()[:6]) for s in statements],
                         ["CREATE TABLE IF NOT EXISTS explorer_edges(", "CREATE INDEX IF NOT EXISTS explorer_edges_b",
                          "CREATE TABLE IF NOT EXISTS explorer_scopes("])


class ImportTest(Base):
    def test_scratch_import_copies_every_table_in_budgeted_ranges(self):
        fake = FakeD1()
        report = self.do_import(fake)
        self.assertTrue(report["imported"])
        self.assert_imported(fake)
        dataset_id = self.local.dataset_id
        self.assertEqual(fake.all("SELECT release_id, state FROM releases"),
                         [(f"scratch:{dataset_id}", "candidate")])
        self.assertEqual(fake.one("SELECT names_mode, tables_json, longest_name, sup_names_from FROM datasets"),
                         ("trigram", self.local.tables_json, self.local.sidecar["longest_name"],
                          self.local.sidecar["sup_names_from"]))
        sizes = [len(s.encode()) for s in fake.statements]
        self.assertLessEqual(max(sizes), BUDGET)
        self.assertGreater(max(sizes), BUDGET * 0.7)       # ranges fill the budget
        appends = [s for s in fake.statements if "length(CAST(" in s]
        self.assertTrue(any(s.startswith("UPDATE cell_pages SET json = json ||") for s in appends))
        self.assertTrue(any(s.startswith("UPDATE labels SET aka = aka ||") and "CAST(X'" in s for s in appends))
        ranges = fake.all("SELECT tbl, count(*) FROM import_ranges GROUP BY tbl")
        self.assertGreater(dict(ranges)["name_keys"], 3)
        self.assertGreater(len(fake.commands), 20)
        self.assertTrue(all(sum(s.startswith("DELETE FROM") for s in split_sql(c[10])) <= 3
                            for c in fake.commands))
        self.assertTrue(any(s.startswith(f"INSERT INTO names_{self.local.hex}(names_{self.local.hex}, rank) "
                                         "VALUES ('automerge', 0)") for s in fake.statements))
        self.assertTrue(any("VALUES ('merge', 500)" in s for s in fake.statements))
        self.assertFalse(any("optimize" in s for s in fake.statements))
        self.assertFalse(any("INSERT OR IGNORE" in s for s in fake.statements))

    def test_statements_stay_under_every_budget(self):
        for budget in (1800, 3000, 9000):
            fake = FakeD1()
            self.do_import(fake, budget=budget, ranges=2)
            self.assert_imported(fake)
            self.assertLessEqual(max(len(s.encode()) for s in fake.statements), budget, budget)

    def test_dry_run_changes_nothing(self):
        fake = FakeD1()
        report = self.do_import(fake, execute=False)
        self.assertEqual(report["dataset_state"], None)
        self.assertEqual(report["copy"]["cell_pages"], self.local.rows("cell_pages"))
        self.assertTrue(all(s.startswith("SELECT") for s in fake.statements))
        self.assertEqual(fake.all("SELECT * FROM datasets"), [])
        self.do_import(fake)
        before = len(fake.statements)
        self.assertEqual(self.do_import(fake, execute=False)["copy"], "skip: the dataset is ready")
        self.assertTrue(all(s.startswith("SELECT") for s in fake.statements[before:]))

    def baseline_calls(self, **kwargs):
        fake = FakeD1()
        self.do_import(fake, **kwargs)
        return fake.calls, self.control(fake)

    def test_a_call_applied_but_reported_failed_is_repaired(self):
        calls, control = self.baseline_calls()
        for n in range(1, calls + 1):
            with self.subTest(call=n):
                fake = FakeD1()
                fake.fail_after = {n}
                self.do_import(fake)
                self.assert_imported(fake)
                self.assertEqual(self.control(fake), control)

    def test_a_call_that_fails_before_applying_is_retried(self):
        calls, control = self.baseline_calls()
        for n in range(1, calls + 1):
            with self.subTest(call=n):
                fake = FakeD1()
                fake.fail_before = {n}
                self.do_import(fake)
                self.assert_imported(fake)
                self.assertEqual(self.control(fake), control)

    def test_a_call_failing_on_every_attempt_stops_the_import(self):
        fake = FakeD1()
        fake.fail_before = set(range(8, 30))
        with self.assertRaises(bdi.D1CallError):
            self.do_import(fake, attempts=2)
        self.do_import(self._resume(fake))
        self.assert_imported(fake)

    def test_a_crash_between_or_inside_any_two_steps_resumes(self):
        calls, control = self.baseline_calls()
        for n in range(1, calls + 1):
            for applied in (0, 1):
                with self.subTest(call=n, applied=applied):
                    fake = FakeD1()
                    fake.crash_at = (n, applied)
                    with self.assertRaises(Crash):
                        self.do_import(fake)
                    self.do_import(self._resume(fake))
                    self.assert_imported(fake)
                    self.assertEqual(self.control(fake), control)

    def test_resume_from_recorded_coverage_after_the_range_plan_changes(self):
        for first, second in ((1800, 4000), (4000, 1800)):
            with self.subTest(first=first, second=second):
                fake = FakeD1()
                calls = 0

                def stop(fake_, statements):
                    nonlocal calls
                    if any(s.startswith("INSERT INTO import_ranges") for s in statements):
                        calls += 1
                        if calls == 9:
                            raise Crash("mid-copy")
                fake.tamper = stop
                with self.assertRaises(Crash):
                    self.do_import(fake, budget=first, ranges=2)
                recorded = fake.all("SELECT tbl, lo, hi FROM import_ranges")
                self.assertTrue(recorded)
                self.do_import(self._resume(fake), budget=second, ranges=5)
                self.assert_imported(fake)
                for row in recorded:      # never re-planned: every earlier range is still there
                    self.assertIn(row, fake.all("SELECT tbl, lo, hi FROM import_ranges"))
                # stray rows a crashed range left in a gap were replaced, not duplicated
                ds = self.ds(fake)
                self.assertEqual(fake.one("SELECT count(*) FROM labels WHERE ds = ?", ds)[0],
                                 self.local.rows("labels"))

    def test_delete_then_insert_repairs_a_range_that_reads_back_wrong(self):
        fake = FakeD1()
        damaged = []

        def damage(fake_, statements):
            if not damaged and any(s.startswith("INSERT INTO labels") for s in statements):
                fake_.db.execute("UPDATE labels SET label = 'damaged' WHERE seq = 2")
                damaged.append(True)
        fake.tamper = damage
        importer = self.importer(fake)
        with importer.locked():
            importer.import_dataset(self.local, "scratch:" + self.local.dataset_id, "f" * 64)
        self.assert_imported(fake)
        self.assertEqual([a["rewrite"][0][0] for a in importer.actions], ["labels"])
        labels = [s for s in fake.statements if s.startswith("DELETE FROM labels WHERE ds = 1 AND seq BETWEEN 1 AND")]
        self.assertEqual(len(labels), 2)

    def test_conditional_appends_never_apply_twice(self):
        fake = FakeD1()
        self.do_import(fake)
        appends = [s for s in fake.statements if "length(CAST(" in s]
        self.assertGreater(len(appends), 3)
        before = fake.all("SELECT seq, aka FROM labels ORDER BY seq"), fake.all("SELECT seq, json FROM cell_pages")
        for statement in appends:
            self.assertEqual(fake.db.execute(statement).rowcount, 0)
        self.assertEqual((fake.all("SELECT seq, aka FROM labels ORDER BY seq"),
                          fake.all("SELECT seq, json FROM cell_pages")), before)

    def test_a_final_update_that_changes_no_row_fails(self):
        fake = FakeD1()

        def interfere(fake_, statements):
            if any("datasets SET state = 'ready'" in s for s in statements):
                return
            if any(s.startswith("SELECT rowid FROM names_") for s in statements):
                fake_.db.execute("UPDATE datasets SET state = 'deleting'")   # someone else moved it
        fake.tamper = interfere
        with self.assertRaisesRegex(bdi.D1ImportError, "changed 0 rows"):
            self.do_import(fake)

    def test_size_ceiling_stops_the_import(self):
        fake = FakeD1()
        importer = self.importer(fake)
        importer.d1.size_ceiling = 1
        with importer.locked(), self.assertRaisesRegex(bdi.D1ImportError, "ceiling"):
            importer.import_dataset(self.local, "scratch:" + self.local.dataset_id, "f" * 64)

    def test_names_mode_parts(self):
        fake = FakeD1()
        importer = self.importer(fake)
        with importer.locked():
            importer.import_dataset(self.local, "scratch:" + self.local.dataset_id, "f" * 64, "parts")
        hex_id = self.local.hex
        self.assertEqual(fake.one("SELECT names_mode FROM datasets"), ("parts",))
        self.assertIn("prefix='2 3'", fake.one("SELECT sql FROM sqlite_master WHERE name = ?", f"names_{hex_id}")[0])
        hits = fake.all(f"SELECT rowid FROM names_{hex_id} WHERE names_{hex_id} MATCH 'Semigroup' ORDER BY rowid")
        self.assertIn(self.local.db.execute("SELECT seq FROM name_keys WHERE name = ?",
                                            ("StronglyContinuousSemigroup.mk",)).fetchone(), hits)
        with importer.locked(), self.assertRaisesRegex(bdi.D1ImportError, "names_mode parts.*supersede"):
            importer.import_dataset(self.local, "scratch:" + self.local.dataset_id, "f" * 64, "trigram")

    def test_parts_import_and_verify_with_pre_343_local_fts(self):
        # Only the local reference lacks the newer FTS option; FakeD1 still
        # supports contentless deletes for range repair and pruning.
        connect = sqlite3.connect

        class LegacyFTSConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if "contentless_delete" in sql.lower():
                    raise sqlite3.OperationalError('unrecognized option: "contentless_delete"')
                return super().execute(sql, parameters)

        def legacy_connect(*args, **kwargs):
            return connect(*args, **kwargs, factory=LegacyFTSConnection)

        for mode in ("parts", None):
            with self.subTest(names_mode=mode):
                fake = FakeD1()
                self.addCleanup(fake.db.close)
                if mode is None:
                    fake.reject = lambda s: ("D1_ERROR: no such tokenizer: trigram"
                                             if "tokenize='trigram'" in s else None)
                importer = self.importer(fake)
                with mock.patch.object(bdi.sqlite3, "connect", side_effect=legacy_connect):
                    with importer.locked():
                        result = importer.import_dataset(
                            self.local, "scratch:" + self.local.dataset_id, "f" * 64, mode)
                    self.assertTrue(result["imported"])
                    self.assertEqual(fake.one("SELECT names_mode FROM datasets"), ("parts",))
                    self.assert_imported(fake)
                    report = importer.verify([self.local], 1000, random.Random(1))
                    self.assertTrue(report["ok"], report["mismatches"])

    def test_an_fts_mismatch_empties_that_index_for_the_rerun(self):
        fake = FakeD1()

        def drop_one(fake_, statements):
            if any(s.startswith(f"INSERT INTO texts_{self.local.hex}(rowid") for s in statements):
                fake_.db.execute(f"DELETE FROM texts_{self.local.hex} WHERE rowid = 1")
                fake_.db.execute(f"INSERT INTO texts_{self.local.hex}(rowid, body) VALUES (1, 'nothing')")
        fake.tamper = drop_one
        with self.assertRaisesRegex(bdi.D1ImportError, "texts MATCH.*rerun the import"):
            self.do_import(fake)
        ds = self.ds(fake)
        self.assertEqual(fake.one("SELECT count(*) FROM import_ranges WHERE ds = ? AND tbl = 'texts'", ds), (0,))
        self.assertGreater(fake.one("SELECT count(*) FROM import_ranges WHERE ds = ? AND tbl = 'names'", ds)[0], 0)
        self.assertIsNone(fake.one("SELECT name FROM sqlite_master WHERE name = ?", f"texts_{self.local.hex}"))
        before = len(fake.statements)
        self.do_import(self._resume(fake))
        self.assert_imported(fake)
        self.assertFalse([s for s in fake.statements[before:] if s.startswith("INSERT INTO labels")])

    def test_a_range_that_read_back_wrong_is_not_journaled_before_its_rewrite(self):
        fake = FakeD1()
        seen = []

        def damage_then_crash(fake_, statements):
            if any(s.startswith("DELETE FROM labels WHERE ds = 1 AND seq BETWEEN 1 AND") for s in statements):
                seen.append(True)
                if len(seen) == 1:
                    fake_.db.execute("UPDATE labels SET label = 'damaged' WHERE seq = 2")
                else:
                    raise Crash("during the rewrite")
        fake.tamper = damage_then_crash
        with self.assertRaises(Crash):
            self.do_import(fake)
        self.assertEqual(len(seen), 2)
        self.assertEqual(fake.all("SELECT lo FROM import_ranges WHERE tbl = 'labels' AND lo = 1"), [])
        self.do_import(self._resume(fake))
        self.assert_imported(fake)

    def test_a_refused_trigram_tokenizer_falls_back_to_parts_before_any_data_range(self):
        fake = FakeD1()
        fake.reject = lambda s: "D1_ERROR: no such tokenizer: trigram" if "tokenize='trigram'" in s else None
        report = self.do_import(fake)
        self.assertTrue(report["imported"])
        self.assertEqual(fake.one("SELECT names_mode FROM datasets"), ("parts",))
        self.assertIn("prefix='2 3'", fake.one("SELECT sql FROM sqlite_master WHERE name = ?",
                                               f"names_{self.local.hex}")[0])
        creates = [i for i, s in enumerate(fake.statements) if s.startswith("CREATE VIRTUAL TABLE")]
        first_data = next(i for i, s in enumerate(fake.statements) if s.startswith("INSERT INTO cell_pages"))
        self.assertEqual(len(creates), 2)
        self.assertLess(max(creates), first_data)
        self.assert_imported(fake)

    def test_a_names_mode_change_is_free_until_a_names_range_is_copied(self):
        fake = FakeD1()
        fake.reject = lambda s: "D1_ERROR: no such tokenizer: trigram" if "tokenize='trigram'" in s else None
        with self.assertRaisesRegex(bdi.D1ImportError, "rerun with --names-mode parts"):
            self.do_import(fake, names_mode="trigram")
        for table in bdi.DATA_TABLES:       # it failed before the copy
            self.assertEqual(fake.one(f"SELECT count(*) FROM {table}"), (0,), table)
        self.assertEqual(fake.one("SELECT names_mode, state FROM datasets"), ("trigram", "importing"))
        self.assertEqual(self.do_import(fake, names_mode="parts", execute=False)["names_mode"], "trigram -> parts")
        self.assertTrue(self.do_import(fake, names_mode="parts")["imported"])
        self.assertEqual(fake.one("SELECT names_mode FROM datasets"), ("parts",))
        self.assert_imported(fake)
        with self.assertRaisesRegex(bdi.D1ImportError, "it is ready.*supersede scratch:"):
            self.do_import(fake, names_mode="trigram")
        # an importing dataset with names ranges journaled keeps its mode too
        fake = FakeD1()

        def stop(fake_, statements):
            if any(s.startswith("INSERT INTO import_ranges") and ", 'names', " in s for s in statements):
                raise Crash("mid-names")
        fake.tamper = stop
        with self.assertRaises(Crash):
            self.do_import(fake)
        with self.assertRaisesRegex(bdi.D1ImportError, "names ranges are already copied"):
            self.do_import(self._resume(fake), names_mode="parts")
        self.do_import(fake)
        self.assert_imported(fake)

    def test_what_could_never_finish_is_refused_before_any_call(self):
        (self.work / "long").mkdir()
        serving, sidecar = build_fixture(self.work / "long", description="word " * 1000)
        local = bdi.open_local(serving, sidecar, full_check=True)
        self.addCleanup(local.close)
        fake = FakeD1()
        with self.assertRaisesRegex(bdi.D1ImportError, "text_ids seq .* does not fit one 2000-byte statement"):
            self.do_import(fake, local=local)
        with mock.patch.object(bdi, "ROW_BYTES", 5000), \
                self.assertRaisesRegex(bdi.D1ImportError, "cell_pages seq .* over D1's 5000-byte row limit"):
            self.do_import(fake)
        self.assertEqual(fake.calls, 0)

    def test_a_gap_in_a_local_table_is_refused(self):
        gap = self.work / "gap.sqlite3"
        gap.write_bytes(self.serving.read_bytes())
        with contextlib.closing(sqlite3.connect(gap)) as db:
            db.execute("DELETE FROM labels WHERE seq = 5")
            db.commit()
        local = bdi.open_local(gap, self.sidecar, full_check=False)
        self.addCleanup(local.close)
        with self.assertRaisesRegex(bdi.D1ImportError, "labels has seq 6 where 5 belongs"):
            self.do_import(FakeD1(), local=local)

    def test_finish_refuses_coverage_with_a_hole(self):
        fake = FakeD1()

        def hole(fake_, statements):
            if any(s.startswith("SELECT rowid FROM names_") for s in statements):
                fake_.db.execute("DELETE FROM import_ranges WHERE tbl = 'labels' AND lo = 1")
        fake.tamper = hole
        with self.assertRaisesRegex(bdi.D1ImportError, "import_ranges cover .* rows of labels"):
            self.do_import(fake)
        self.assertEqual(fake.one("SELECT state FROM datasets"), ("importing",))

    def test_a_datasets_row_that_disagrees_with_its_sidecar_is_refused(self):
        fake = FakeD1()
        self.do_import(fake)
        fake.db.execute("UPDATE datasets SET longest_name = longest_name + 1")
        with self.assertRaisesRegex(bdi.D1ImportError, "disagrees with its sidecar"):
            self.do_import(fake)

    def test_groups_count_every_separator_against_the_call_budget(self):
        importer = self.importer(FakeD1(), ranges=5, call_bytes=405)
        items = [bdi.Range("labels", i, i, "d", ["A" * 100, "B" * 100]) for i in (1, 2, 3)]
        groups = []

        def land(ds, group, read):
            groups.append(list(group))
            return len(group)
        with mock.patch.object(importer, "_land", land):
            importer._copy_spans(1, [(1, 3)], lambda a, b: iter(items), lambda r: "", name="labels", total=3)
            self.assertEqual([len(g) for g in groups], [1, 1, 1])     # two ranges make a 407-byte call
            importer.options.call_bytes = 407
            groups.clear()
            importer._copy_spans(1, [(1, 3)], lambda a, b: iter(items), lambda r: "", name="labels", total=3)
            self.assertEqual([len(g) for g in groups], [2, 1])
        # a whole import at the smallest call budget the command line allows over the statements
        fake = FakeD1()
        self.do_import(fake, budget=2000, ranges=5, call_bytes=3100)
        self.assert_imported(fake)
        self.assertLessEqual(max(len(c[10].encode()) for c in fake.commands), 3100)

    def test_every_copy_call_logs_its_range_and_progress(self):
        fake = FakeD1()
        self.do_import(fake)
        writes = [r for r in fake.records if r.get("phase") == "copy" and r.get("table") == "labels"
                  and r.get("rows_written")]
        self.assertTrue(writes)
        for key in ("lo", "hi", "duration_ms", "rows_written", "size_after"):
            self.assertIn(key, writes[0])
        progress = [r for r in fake.records if r.get("progress") == "labels"]
        self.assertGreater(len(progress), 1)
        self.assertEqual((progress[-1]["rows_done"], progress[-1]["rows_total"]),
                         (self.local.rows("labels"),) * 2)

    def test_a_slow_call_makes_the_importer_pause(self):
        fake = FakeD1()
        fake.duration = 5000
        slept = []
        self.importer(fake, sleep=slept.append).d1.call(["SELECT 1"])
        self.assertEqual(slept, [5.0])


class SchemaTest(Base):
    def test_refuses_an_unsupported_schema_version(self):
        self.assertEqual(self.local.sidecar["schema_version"], 2)
        sidecar = json.loads(self.sidecar.read_text())
        for version in (1, 3):
            sidecar["schema_version"] = version
            path = self.work / f"v{version}.json"
            path.write_text(json.dumps(sidecar))
            with self.assertRaisesRegex(bdi.D1ImportError, f"schema_version {version} is not one"):
                bdi.open_local(self.serving, path, full_check=False)
        with self.assertRaisesRegex(bdi.D1ImportError, "schema_version 3"):
            self.importer(FakeD1()).require_schema(3)

    def test_refuses_when_the_migration_is_not_applied(self):
        fake = FakeD1(migrate=False)
        fake.db.executescript(MIGRATION.read_text().replace("live_sups TEXT NOT NULL,", ""))
        fake.db.executescript(EXACT_INDEX_MIGRATION.read_text())
        fake.db.executescript(EXPLORER_MIGRATION.read_text())
        with self.assertRaisesRegex(bdi.D1ImportError, "hot_prefixes.live_sups"):
            self.do_import(fake)
        self.assertEqual(fake.all("SELECT * FROM datasets"), [])

    def test_refuses_without_the_explorer_migration(self):
        fake = FakeD1(migrate=False)
        fake.db.executescript(MIGRATION.read_text())
        fake.db.executescript(EXACT_INDEX_MIGRATION.read_text())
        with self.assertRaisesRegex(bdi.D1ImportError, r"schema_version 2 \(0001_brain\.sql, then "
                                    r"0002_name_keys_exact\.sql, then 0003_explorer\.sql\).*"
                                    r"missing explorer_edges\.ds, explorer_edges\.seq, "):
            self.do_import(fake)
        self.assertEqual(fake.all("SELECT * FROM datasets"), [])

    def test_refuses_before_the_first_dataset_without_the_explorer_index_or_keys(self):
        # explorer_edges_b can only land while explorer_edges is empty, like 0002's index
        text = EXPLORER_MIGRATION.read_text()
        index = "CREATE INDEX IF NOT EXISTS explorer_edges_b ON explorer_edges(ds, b);"
        self.assertIn(index, text)
        for broken, message in (
                (text.replace(index, ""), r"missing explorer_edges INDEX explorer_edges_b\(ds, b\)$"),
                (text.replace("UNIQUE(ds, seq), UNIQUE(ds, a, b));", "UNIQUE(ds, seq));"),
                 r"missing explorer_edges UNIQUE\(ds, a, b\)$"),
                (text.replace("UNIQUE(ds, seq), UNIQUE(ds, scope, part));", "UNIQUE(ds, seq));"),
                 r"missing explorer_scopes UNIQUE\(ds, scope, part\)$")):
            with self.subTest(message=message):
                self.assertNotEqual(broken, text)
                fake = FakeD1(migrate=False)
                fake.db.executescript(MIGRATION.read_text())
                fake.db.executescript(EXACT_INDEX_MIGRATION.read_text())
                fake.db.executescript(broken)
                with self.assertRaisesRegex(bdi.D1ImportError, message):
                    self.do_import(fake)
                self.assertEqual(fake.all("SELECT * FROM datasets"), [])

    def test_refuses_before_the_first_dataset_without_the_exact_name_index(self):
        # 0002 can only land while the tables are empty, so its absence must stop the first import
        fake = FakeD1(migrate=False)
        fake.db.executescript(MIGRATION.read_text())
        fake.db.executescript(EXPLORER_MIGRATION.read_text())
        with self.assertRaisesRegex(bdi.D1ImportError,
                                    r"0002_name_keys_exact\.sql.*missing name_keys INDEX name_keys_exact\(ds, name_lc, seq\)$"):
            self.do_import(fake)
        self.assertEqual(fake.all("SELECT * FROM datasets"), [])
        fake.db.execute("CREATE INDEX name_keys_exact ON name_keys(ds, seq, name_lc)")     # wrong columns
        with self.assertRaisesRegex(bdi.D1ImportError, "missing name_keys INDEX name_keys_exact"):
            self.do_import(fake)
        fake.db.execute("DROP INDEX name_keys_exact")
        fake.db.executescript(EXACT_INDEX_MIGRATION.read_text())
        self.do_import(fake)
        self.assert_imported(fake)

    def test_refuses_a_table_without_its_seq_key(self):
        fake = FakeD1(migrate=False)
        text = MIGRATION.read_text()
        keyed = "  UNIQUE(ds, seq), UNIQUE(ds, ord), UNIQUE(ds, id));\nCREATE INDEX IF NOT EXISTS labels_p"
        self.assertIn(keyed, text)
        fake.db.executescript(text.replace(keyed, "  UNIQUE(ds, ord), UNIQUE(ds, id));\n"
                                                  "CREATE INDEX IF NOT EXISTS labels_p"))
        fake.db.executescript(EXACT_INDEX_MIGRATION.read_text())
        fake.db.executescript(EXPLORER_MIGRATION.read_text())
        with self.assertRaisesRegex(bdi.D1ImportError, r"missing labels UNIQUE\(ds, seq\)$"):
            self.do_import(fake)
        self.assertEqual(fake.all("SELECT * FROM datasets"), [])

    def test_refuses_a_serving_file_that_differs_from_its_sidecar(self):
        sidecar = json.loads(self.sidecar.read_text())
        sidecar["tables"]["labels"]["digest"] = "0" * 64
        path = self.work / "bad.json"
        path.write_text(json.dumps(sidecar))
        with self.assertRaisesRegex(bdi.D1ImportError, "different tables"):
            bdi.open_local(self.serving, path, full_check=True)


class ProtectionTest(Base):
    def test_refuses_while_an_attempt_is_incomplete(self):
        fake = FakeD1()
        self.incomplete_attempt()
        for action in (lambda: self.do_import(fake), lambda: self.do_prune(fake),
                       lambda: self.do_supersede(fake, LIVE_A)):
            with self.assertRaisesRegex(bdi.D1ImportError, "incomplete promotion attempt.*--reconcile-attempt"):
                action()
        self.assertEqual(fake.calls, 0)

    def test_attempt_selectors_protect_only_until_the_final_state(self):
        open_ = self.incomplete_attempt(staged=LIVE_A, predeploy=LIVE_B)
        named = bdi.attempt_selector_releases([open_])
        self.assertEqual(set(named), {LIVE_A, LIVE_B})
        state = bdi.RemoteState((bdi.ReleaseRow(LIVE_A, 1, 1, "superseded", "sha256:" + "1" * 64, "ready"),
                                 bdi.ReleaseRow(LIVE_B, 2, 2, "superseded", "sha256:" + "2" * 64, "ready")), ())
        self.assertEqual(bdi.protected_set(state, (None, None), named).datasets, {1, 2})
        with journal.PromotionLock(self.root):
            open_.append("final_state", {"outcome": "no_production_change"})
        closed = journal.EventJournal.load(open_.attempt_dir)
        self.assertEqual(bdi.attempt_selector_releases([closed]), {})
        self.assertEqual(bdi.protected_set(state, (None, None), {}).datasets, frozenset())

    def second_dataset(self):
        tmp = self.work / "second"
        tmp.mkdir()
        serving, sidecar = build_fixture(tmp, description="a different description")
        local = bdi.open_local(serving, sidecar, full_check=True)
        self.addCleanup(local.close)
        self.assertNotEqual(local.dataset_id, self.local.dataset_id)
        return local

    def test_refuses_a_second_candidate_dataset_until_superseded(self):
        fake = FakeD1()
        self.do_import(fake, release_id=LIVE_A)
        other = self.second_dataset()
        with self.assertRaisesRegex(bdi.D1ImportError, "different dataset.*supersede it first"):
            self.do_import(fake, local=other, release_id=LIVE_B)
        self.do_supersede(fake, LIVE_A)
        self.do_import(fake, local=other, release_id=LIVE_B)
        self.assert_imported(fake, other)
        # the superseded candidate and its dataset were pruned by the second import
        self.assertEqual(self.control(fake)[1], [(LIVE_B, other.dataset_id, "candidate")])
        self.assertIsNone(self.ds(fake))
        self.assertEqual(fake.one("SELECT count(*) FROM import_ranges WHERE ds = 1"), (0,))
        self.assertEqual(fake.one("SELECT count(*) FROM labels WHERE ds = 1"), (0,))
        self.assertIsNone(fake.one("SELECT name FROM sqlite_master WHERE name = ?", f"names_{self.local.hex}"))

    def test_candidates_newer_than_the_live_current_are_protected(self):
        fake = FakeD1()
        self.do_import(fake, release_id=LIVE_A)
        other = self.second_dataset()
        self.live[:] = [LIVE_A, None]           # A went live; B is newer and protected
        self.do_import(fake, local=other, release_id=LIVE_B)
        report = self.do_prune(fake)
        self.assertEqual(report["delete_releases"], [])
        self.assertEqual(report["protected"], {LIVE_A: "live current",
                                               LIVE_B: "candidate newer than the live current"})
        # B goes live with A previous, then B is live alone: A is neither live nor newer
        self.live[:] = [LIVE_B, LIVE_A]
        self.assertEqual(self.do_prune(fake)["delete_releases"], [])
        self.live[:] = [LIVE_B, None]
        report = self.do_prune(fake)
        self.assertEqual(report["delete_releases"], [LIVE_A])
        self.assertEqual(report["delete_datasets"], [self.local.dataset_id])
        self.assert_imported(fake, other)

    def test_supersede_refuses_live_releases_and_is_idempotent(self):
        fake = FakeD1()
        self.do_import(fake, release_id=LIVE_A)
        self.live[:] = [LIVE_A, None]
        with self.assertRaisesRegex(bdi.D1ImportError, "live current"):
            self.do_supersede(fake, LIVE_A)
        self.live[:] = [LIVE_B, LIVE_A]
        with self.assertRaisesRegex(bdi.D1ImportError, "live previous"):
            self.do_supersede(fake, LIVE_A)
        self.live[:] = [None, None]
        self.assertFalse(self.do_supersede(fake, LIVE_A, execute=False)["changed"])
        self.assertEqual(fake.one("SELECT state FROM releases"), ("candidate",))
        self.assertTrue(self.do_supersede(fake, LIVE_A)["changed"])
        self.assertFalse(self.do_supersede(fake, LIVE_A)["changed"])
        self.assertEqual(fake.one("SELECT state FROM releases"), ("superseded",))
        with self.assertRaisesRegex(bdi.D1ImportError, "no release row"):
            self.do_supersede(fake, LIVE_B)

    def test_a_shared_dataset_is_kept_while_any_release_references_it(self):
        fake = FakeD1()
        self.do_import(fake, release_id=LIVE_A)
        self.do_import(fake, release_id=LIVE_B)       # same tables: no copy
        self.assertEqual(fake.one("SELECT count(*) FROM datasets"), (1,))
        self.live[:] = [LIVE_A, None]
        self.do_supersede(fake, LIVE_B)
        report = self.do_prune(fake)
        self.assertEqual((report["delete_releases"], report["delete_datasets"]), ([LIVE_B], []))
        self.assert_imported(fake)
        # static releases go live: A, still a candidate, stays protected until superseded
        self.live[:] = ["sha256:" + "c" * 64, "sha256:" + "d" * 64]
        self.assertEqual(self.do_prune(fake)["protected"][LIVE_A], "candidate newer than the live current")
        self.do_supersede(fake, LIVE_A)
        report = self.do_prune(fake)
        self.assertEqual((report["delete_releases"], report["delete_datasets"]),
                         ([LIVE_A], [self.local.dataset_id]))
        self.assertEqual(fake.all("SELECT * FROM datasets"), [])
        for table in bdi.DATA_TABLES:
            self.assertEqual(fake.one(f"SELECT count(*) FROM {table}"), (0,), table)

    def test_register_before_prune_keeps_a_reused_dataset(self):
        fake = FakeD1()
        self.do_import(fake, release_id=LIVE_A)
        self.do_supersede(fake, LIVE_A)
        before = len(fake.statements)
        report = self.do_import(fake, release_id=LIVE_B)
        self.assertFalse(report["imported"])
        self.assertEqual(report["prune"]["delete_releases"], [LIVE_A])
        self.assertEqual(report["prune"]["delete_datasets"], [])
        writes = [s for s in fake.statements[before:] if not s.startswith("SELECT")]
        self.assertFalse([s for s in writes if any(s.startswith(f"{verb} {t}") for t in bdi.DATA_TABLES
                                                   for verb in ("INSERT INTO", "DELETE FROM"))])
        self.assertEqual(self.control(fake)[1], [(LIVE_B, self.local.dataset_id, "candidate")])
        self.assert_imported(fake)

    def test_an_importing_candidate_is_protected_until_superseded(self):
        fake = FakeD1()
        fake.crash_at = (25, 0)
        with self.assertRaises(Crash):
            self.do_import(fake, release_id=LIVE_A)
        self._live_static()
        state = self.importer(self._resume(fake)).remote_state()
        self.assertEqual(bdi.protected_set(state, tuple(self.live)).releases[LIVE_A],
                         "candidate newer than the live current")
        # even registered before the live current, an importing candidate stays protected
        fake.db.execute("INSERT INTO releases VALUES (?, 99, 1, 'x', 'candidate', 'now')", (LIVE_B,))
        self.live[:] = [LIVE_B, None]
        state = self.importer(fake).remote_state()
        self.assertEqual(bdi.protected_set(state, tuple(self.live)).releases[LIVE_A],
                         "candidate whose dataset is importing")
        self.assertEqual(self.do_prune(fake)["delete_datasets"], [])
        self.do_supersede(fake, LIVE_A)
        fake.db.execute("DELETE FROM releases WHERE release_id = ?", (LIVE_B,))
        self.assertEqual(self.do_prune(fake)["delete_datasets"], [self.local.dataset_id])

    def test_a_protected_dataset_being_deleted_is_left_to_the_operator(self):
        fake = FakeD1()
        self.do_import(fake, release_id=LIVE_A)
        self.live[:] = [LIVE_A, None]
        fake.db.execute("UPDATE datasets SET state = 'deleting'")
        before = self.control(fake)
        with self.assertRaisesRegex(bdi.D1ImportError, "resolve by hand"):
            self.do_prune(fake)
        self.assertEqual(self.control(fake), before)

    def _live_static(self):
        self.live[:] = ["sha256:" + "c" * 64, None]


class PruneTest(Base):
    def imported_then_abandoned(self, local=None):
        fake = FakeD1()
        self.do_import(fake, local=local, release_id=LIVE_A)
        self.do_supersede(fake, LIVE_A)
        return fake

    def test_dry_run_prune_changes_nothing(self):
        fake = self.imported_then_abandoned()
        before = self.control(fake)
        report = self.do_prune(fake, execute=False)
        self.assertEqual(report["delete_datasets"], [self.local.dataset_id])
        self.assertEqual(self.control(fake), before)

    def test_prune_deletes_by_delete_bounds_and_journal_rows_first(self):
        # the same tables with 2,000-byte delete bounds: a different dataset id, several deletes
        (self.work / "small").mkdir()
        with mock.patch.object(bsd, "DELETE_BOUND_BYTES", 2000):
            serving, sidecar = build_fixture(self.work / "small")
        local = bdi.open_local(serving, sidecar, full_check=True)
        self.addCleanup(local.close)
        fake = self.imported_then_abandoned(local)
        start = len(fake.statements)
        self.do_prune(fake)
        steps = fake.statements[start:]
        steps = [s for s in steps if not s.startswith("SELECT")]
        self.assertTrue(steps[1].startswith("UPDATE datasets SET state = 'deleting'"))
        self.assertTrue(steps[2].startswith("DELETE FROM import_ranges"))
        self.assertTrue(steps[3].startswith("DROP TABLE IF EXISTS names_"))
        self.assertTrue(steps[-1].startswith("DELETE FROM datasets"))
        for table in bdi.DATA_TABLES:
            bounds, rows = local.tables[table]["delete_bounds"], local.rows(table)
            spans = [f"seq BETWEEN {lo} AND {hi}" for lo, hi in zip([1, *(b + 1 for b in bounds)], [*bounds, rows])
                     if hi >= lo]
            deletes = [s for s in steps if s.startswith(f"DELETE FROM {table} ")]
            self.assertEqual([s.split(" AND ", 1)[1] for s in deletes], spans, table)
            self.assertEqual(fake.one(f"SELECT count(*) FROM {table}"), (0,), table)
        self.assertTrue(local.tables["cell_pages"]["delete_bounds"])
        self.assertEqual(fake.all("SELECT * FROM datasets"), [])

    def test_an_interrupted_prune_is_finished_by_the_next_prune_or_import(self):
        fake = self.imported_then_abandoned()
        start = fake.calls
        self.do_prune(fake)
        prune_calls = fake.calls - start
        for n in range(1, prune_calls + 1):
            for finisher in ("prune", "import"):
                with self.subTest(call=n, finisher=finisher):
                    fake = self.imported_then_abandoned()
                    fake.crash_at = (fake.calls + n, 0)
                    with self.assertRaises(Crash):
                        self.do_prune(fake)
                    fake.crash_at = None
                    state = fake.one("SELECT state FROM datasets")
                    # journal rows go before any data row: while one remains, no row is deleted
                    if fake.one("SELECT count(*) FROM import_ranges") != (0,):
                        for table in bdi.DATA_TABLES:
                            self.assertEqual(fake.one(f"SELECT count(*) FROM {table}"),
                                             (self.local.rows(table),), table)
                    if finisher == "prune":
                        self.do_prune(fake)
                        self.assertEqual(fake.all("SELECT * FROM datasets"), [])
                        self.assertEqual(fake.all("SELECT * FROM releases"), [])
                        for table in bdi.DATA_TABLES:
                            self.assertEqual(fake.one(f"SELECT count(*) FROM {table}"), (0,), table)
                    else:
                        # a dataset the prune began deleting is finished and imported fresh;
                        # one it had not reached yet is simply kept for the new release
                        report = self.do_import(fake, release_id=LIVE_B)
                        self.assertEqual(report["imported"], state == ("deleting",))
                        self.assert_imported(fake)
                        self.assertEqual(self.control(fake)[1], [(LIVE_B, self.local.dataset_id, "candidate")])


    def test_prune_deletes_by_the_dataset_s_own_schema_version(self):
        fake = self.imported_then_abandoned()
        fake.db.execute("UPDATE datasets SET schema_version = 9")
        before = self.control(fake)
        with self.assertRaisesRegex(bdi.D1ImportError, "schema_version this importer does not know"):
            self.do_prune(fake)
        self.assertEqual(self.control(fake), before)
        # a v2 dataset's tables under schema_version 1 are not v1's tables
        fake.db.execute("UPDATE datasets SET schema_version = 1")
        before = self.control(fake)
        with self.assertRaisesRegex(bdi.D1ImportError, "malformed tables_json.*schema_version 1"):
            self.do_prune(fake)
        self.assertEqual(self.control(fake), before)
        # a v1 dataset (its tables_json has v1's ten tables) is deleted by those ten alone
        fake.db.execute("UPDATE datasets SET tables_json = json_remove(tables_json, '$.explorer_edges', "
                        "'$.explorer_scopes')")
        start = len(fake.statements)
        self.do_prune(fake)
        deleted = {s.split()[2] for s in fake.statements[start:] if s.startswith("DELETE FROM ")}
        # every v1 table that holds rows (an empty one has no seq range to delete)
        self.assertEqual(deleted - {"releases", "import_ranges", "datasets"},
                         {table for table in bdi.TABLES_BY_VERSION[1] if self.local.rows(table)})
        self.assertEqual(len(bdi.TABLES_BY_VERSION[1]), 10)
        self.assertEqual(fake.all("SELECT * FROM datasets"), [])
        for table in bdi.TABLES_BY_VERSION[1]:
            self.assertEqual(fake.one(f"SELECT count(*) FROM {table}"), (0,), table)
        # the stand-in's v2 rows stay: a real v1 dataset never had any
        self.assertTrue(fake.one("SELECT count(*) FROM explorer_scopes")[0])

    def test_prune_deletes_a_v2_dataset_by_its_twelve_tables(self):
        fake = self.imported_then_abandoned()
        start = len(fake.statements)
        self.do_prune(fake)
        deleted = {s.split()[2] for s in fake.statements[start:] if s.startswith("DELETE FROM ")}
        # every v2 table that holds rows (an empty one has no seq range to delete)
        self.assertEqual(deleted - {"releases", "import_ranges", "datasets"},
                         {table for table in bdi.TABLES_BY_VERSION[2] if self.local.rows(table)})
        self.assertIn("explorer_scopes", deleted)
        self.assertEqual(len(bdi.TABLES_BY_VERSION[2]), 12)
        for table in bdi.DATA_TABLES:
            self.assertEqual(fake.one(f"SELECT count(*) FROM {table}"), (0,), table)


class VerifyTest(Base):
    def test_verify_takes_no_lock_and_finds_a_damaged_row(self):
        fake = FakeD1()
        self.do_import(fake)
        importer = self.importer(fake, root=None)
        with journal.PromotionLock(self.root):        # the promoter holds the lock
            report = importer.verify([self.local], 1000, random.Random(1))
        self.assertTrue(report["ok"])
        total = fake.one("SELECT count(*) FROM import_ranges")[0]
        self.assertEqual(report["checked"][0]["ranges"], total)
        fake.db.execute("UPDATE xrefs SET node = 'Q999' WHERE seq = 1")
        fake.db.execute(f"DELETE FROM texts_{self.local.hex} WHERE rowid = 1")
        report = importer.verify([self.local], 1000, random.Random(2))
        self.assertFalse(report["ok"])
        self.assertEqual(len([m for m in report["mismatches"] if "D1 differs" in m]), 2)
        self.assertTrue(any(" xrefs " in m for m in report["mismatches"]))
        self.assertTrue(any(" texts " in m for m in report["mismatches"]))
        self.assertTrue(all(s.startswith("SELECT") for s in fake.statements[-20:]))

    def test_verify_finds_damaged_explorer_rows(self):
        fake = FakeD1()
        self.do_import(fake)
        importer = self.importer(fake, root=None)
        self.assertTrue(importer.verify([self.local], 1000, random.Random(1))["ok"])
        self.assertTrue(fake.one("SELECT count(*) FROM explorer_edges")[0])
        fake.db.execute("UPDATE explorer_scopes SET json = json || ' ' WHERE seq = 1")
        fake.db.execute("UPDATE explorer_edges SET w = w + 1 WHERE seq = 1")
        report = importer.verify([self.local], 1000, random.Random(2))
        self.assertFalse(report["ok"])
        self.assertTrue(any(" explorer_scopes " in m for m in report["mismatches"]), report["mismatches"])
        self.assertTrue(any(" explorer_edges " in m for m in report["mismatches"]), report["mismatches"])

    def test_verify_runs_the_fts_sample_queries(self):
        fake = FakeD1()
        self.do_import(fake)
        importer = self.importer(fake, root=None)
        self.assertTrue(importer.verify([self.local], 1000, random.Random(1))["ok"])
        fake.db.execute(f"DELETE FROM texts_{self.local.hex} WHERE rowid = 1")     # same count, other text
        fake.db.execute(f"INSERT INTO texts_{self.local.hex}(rowid, body) VALUES (1, 'nothing')")
        report = importer.verify([self.local], 1000, random.Random(1))
        self.assertFalse(report["ok"])
        self.assertTrue(report["mismatches"])
        self.assertTrue(all("texts MATCH" in m for m in report["mismatches"]), report["mismatches"])

    def test_verify_refuses_a_dataset_that_is_not_ready(self):
        with self.assertRaisesRegex(bdi.D1ImportError, "absent"):
            self.importer(FakeD1(), root=None).verify([self.local], 3, random.Random(0))


class CommandLineTest(Base):
    def args(self, *extra):
        return bdi.parse_args(["--repo-root", str(REPO), "--receipt-dir", str(self.root),
                               "--database-id", "01234567-89ab-cdef-0123-456789abcdef",
                               "--statement-bytes", "2500", "--call-bytes", "12000", *extra])

    def run_cli(self, fake, *extra):
        seen = []
        real_run = fake.run

        def run(args, **kwargs):
            if "--config" in args:
                config = json.loads(Path(args[args.index("--config") + 1]).read_text())
                seen.append(config["d1_databases"][0])
            return real_run(args, **kwargs)
        fake.run = run
        result = bdi.run(self.args(*extra), runner=fake, selector=self.selector, base_url=TARGET,
                         node=Path("node"), wrangler=Path("wrangler.js"), sleep=lambda s: None, log=fake.log)
        fake.run = real_run
        return result, seen

    def test_scratch_import_prune_and_verify_from_the_command_line(self):
        fake = FakeD1()
        result, seen = self.run_cli(fake, "--execute", "import", "--scratch", str(self.serving), str(self.sidecar))
        self.assertTrue(result["ok"] and result["imported"])
        self.assertEqual(seen[0], {"binding": "BRAIN_DB", "database_name": "wikilean-brain",
                                   "database_id": "01234567-89ab-cdef-0123-456789abcdef"})
        self.assertEqual(result["calls"]["calls"], fake.calls)
        self.assertLessEqual(result["calls"]["largest_statement_bytes"], 2500)
        self.assert_imported(fake)
        result, _ = self.run_cli(fake, "verify", "--sample", "5", "--scratch", str(self.serving),
                                 str(self.sidecar), "--seed", "3")
        self.assertTrue(result["ok"])
        result, _ = self.run_cli(fake, "--execute", "supersede", f"scratch:{self.local.dataset_id}")
        result, _ = self.run_cli(fake, "--dry-run", "prune")
        self.assertEqual(result["delete_datasets"], [self.local.dataset_id])
        self.assertEqual(fake.one("SELECT count(*) FROM datasets"), (1,))   # a dry run
        self.run_cli(fake, "--execute", "prune")
        self.assertEqual(fake.one("SELECT count(*) FROM datasets"), (0,))

    def test_frozen_release_import_runs_the_verifier(self):
        root = self.work / ("e" * 64)
        (root / "brain" / "data").mkdir(parents=True)
        for name, source in ((bdi.SERVING_PATH, self.serving), (bdi.SIDECAR_PATH, self.sidecar)):
            (root / name).write_bytes(source.read_bytes())
        release_id = "sha256:" + "9" * 64
        manifest = {"release_id": release_id, "artifacts": [
            {"path": bdi.SERVING_PATH, "logical_root": None},
            {"path": bdi.SIDECAR_PATH, "logical_root": self.local.dataset_id}]}
        (root / "release.json").write_text(json.dumps(manifest))
        fake = FakeD1()
        fake.verified[str(root / "release.json")] = release_id
        result, _ = self.run_cli(fake, "--execute", "import", "--release-root", str(root),
                                 "--python", sys.executable)
        self.assertTrue(result["imported"])
        self.assertEqual(fake.all("SELECT release_id FROM releases"), [(release_id,)])
        manifest["artifacts"][1]["logical_root"] = "sha256:" + "0" * 64
        (root / "release.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(bdi.D1ImportError, "different sidecar"):
            self.run_cli(fake, "--execute", "import", "--release-root", str(root), "--python", sys.executable)
        fake.verified[str(root / "release.json")] = "sha256:" + "8" * 64
        manifest["artifacts"][1]["logical_root"] = self.local.dataset_id
        (root / "release.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(bdi.D1ImportError, "did not confirm"):
            self.run_cli(fake, "--execute", "import", "--release-root", str(root), "--python", sys.executable)

    def test_modes_timeout_and_the_linux_argument_limit(self):
        base = ["--repo-root", str(REPO), "--receipt-dir", str(self.root),
                "--database-id", "01234567-89ab-cdef-0123-456789abcdef"]
        for argv in ([*base, "prune"], [*base, "--dry-run", "--execute", "prune"],
                     [*base, "--execute", "verify", "--sample", "3", "--scratch", "a", "b"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                bdi.parse_args(argv)
        self.assertTrue(bdi.parse_args([*base, "--dry-run", "prune"]).dry_run)
        with mock.patch.dict(os.environ, {"WIKILEAN_BRAIN_COMMAND_TIMEOUT": "12"}):
            self.assertEqual(bdi.parse_args([*base, "--dry-run", "prune"]).command_timeout, 12)
        with mock.patch.object(bdi.sys, "platform", "linux"), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            bdi.parse_args([*base, "--call-bytes", "400000", "--dry-run", "prune"])

    def test_main_reports_every_failure_as_json_with_the_call_stats(self):
        fake = FakeD1()
        fake.reject = lambda s: "D1_ERROR: boom" if s.startswith("INSERT INTO labels") else None
        argv = ["--repo-root", str(REPO), "--receipt-dir", str(self.root),
                "--database-id", "01234567-89ab-cdef-0123-456789abcdef", "--statement-bytes", "2500",
                "--call-bytes", "12000", "--call-attempts", "1", "--execute",
                "import", "--scratch", str(self.serving), str(self.sidecar)]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = bdi.main(argv, runner=fake, selector=self.selector, base_url=TARGET, node=Path("node"),
                            wrangler=Path("wrangler.js"), sleep=lambda s: None, log=fake.log)
        self.assertEqual(code, 1)
        record = json.loads(err.getvalue())
        self.assertFalse(record["ok"])
        self.assertIn("phase=copy table=labels lo=1", record["error"])
        self.assertEqual(record["calls"]["calls"], fake.calls)
        self.assertEqual((record["last_call"]["phase"], record["last_call"]["table"]), ("copy", "labels"))
        for exc, want in ((KeyboardInterrupt(), 130), (OSError("node is missing"), 1),
                          (sqlite3.OperationalError("disk I/O error"), 1)):
            err = io.StringIO()
            with mock.patch.object(bdi, "run", side_effect=exc), contextlib.redirect_stderr(err):
                self.assertEqual(bdi.main(argv), want, exc)
            self.assertFalse(json.loads(err.getvalue())["ok"])

    def test_requires_one_database_target_and_a_receipt_root(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            bdi.parse_args(["--receipt-dir", str(self.root), "prune"])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            bdi.parse_args(["--database-id", "x", "--wrangler-config", "/y", "prune"])
        with mock.patch.dict(os.environ, {"WIKILEAN_BRAIN_RECEIPT_DIR": ""}), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            bdi.parse_args(["--database-id", "x", "prune"])
        with self.assertRaisesRegex(bdi.D1ImportError, "UUID"):
            bdi.WranglerConfig(config=None, database_id="not-a-uuid", database="wikilean-brain")


class WranglerBoundaryTest(unittest.TestCase):
    """D1.call against literal Wrangler answers: everything unexpected fails closed."""

    @staticmethod
    def d1(runner, node=Path("node")):
        return bdi.D1(repo=REPO, database="wikilean-brain", config=Path("/nowhere/wrangler.json"), runner=runner,
                      attempts=1, node=node, wrangler=Path("wrangler.js"), sleep=lambda s: None,
                      log=lambda record: None)

    def test_parse_accepts_only_a_successful_result_list(self):
        ok = [{"results": [{"n": 1}], "success": True, "meta": {}}]

        def parse(stdout, **kwargs):
            return bdi.D1._parse(RunResult(("wrangler",), 0, stdout, b""), **kwargs)[0]
        self.assertEqual(parse(json.dumps(ok).encode()), ok)
        banner = b"[wrangler:warn] a warning line\n\xe2\x9b\x85 wrangler 4\n" + json.dumps(ok, indent=1).encode() + b"\n"
        self.assertEqual(parse(banner), ok)
        for bad in (b'[{"success": false, "results": []}]', b"[]", b"{}", b"no json at all",
                    json.dumps(ok).encode() + b"\ntrailing text", b'[{"success": true, "results": [1]}]',
                    b'[{"success": true, "results": {}}]'):
            self.assertIsNone(parse(bad), bad)
        self.assertIsNone(parse(b'[{"success": true}]', expect_results=True))
        self.assertEqual(parse(b'[{"success": true}]'), [{"success": True}])

    def test_calls_fail_closed_at_the_boundary(self):
        class Runner:
            def __init__(self, stdout=b"", exc=None):
                self.stdout, self.exc = stdout, exc

            def run(self, args, *, cwd, timeout, env):
                if self.exc is not None:
                    raise self.exc
                return RunResult(tuple(args), 0, self.stdout, b"")
        one = json.dumps([{"results": [], "success": True, "meta": {}}]).encode()
        with self.assertRaisesRegex(bdi.D1ImportError, "1 results for 2 statements"):
            self.d1(Runner(one)).call(["DELETE FROM a", "DELETE FROM b"])       # a write, too
        with self.assertRaisesRegex(bdi.D1ImportError, "lower --call-bytes"):
            self.d1(Runner(exc=OSError(7, "Argument list too long"))).call(["SELECT 1"])
        meta = json.dumps([{"results": [], "success": True, "meta": {"duration": "slow"}}]).encode()
        with self.assertRaisesRegex(bdi.D1ImportError, "malformed meta"):
            self.d1(Runner(meta)).call(["SELECT 1"])
        with self.assertRaisesRegex(bdi.D1CallError, "not a successful D1 result"):
            self.d1(Runner(b'[{"success": true}]')).call(["SELECT 1"], expect_results=True)

    def test_a_real_node_gets_a_scrubbed_environment_with_the_cloudflare_token(self):
        seen = []

        class Runner:
            def run(self, args, *, cwd, timeout, env):
                seen.append(env)
                return RunResult(tuple(args), 0, json.dumps([{"results": [], "success": True}]).encode(), b"")
        node = Path(sys.executable).resolve()
        with mock.patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "test-token", "UNRELATED_SETTING": "x"}):
            self.d1(Runner(), node=node).call(["SELECT 1"])
        self.assertEqual(seen[0]["CLOUDFLARE_API_TOKEN"], "test-token")
        self.assertTrue(seen[0]["PATH"].startswith(str(node.parent)))
        self.assertNotIn("UNRELATED_SETTING", seen[0])


def run_rows_port(*args: str, stdin: str | None = None):
    """wiki/test/helpers/brain-d1-rows.ts under node (CI's python job has Node 22)."""
    node = shutil.which("node")
    if node is None:
        raise AssertionError("node is required: the Worker tests' serving-row port is checked against the builder here")
    done = subprocess.run([node, "--experimental-strip-types", "--no-warnings", str(ROWS_PORT), *args],
                          input=stdin, capture_output=True, text=True, encoding="utf-8",
                          cwd=REPO, check=False)
    if done.returncode != 0:
        raise AssertionError(f"brain-d1-rows.ts {args[0]} failed: {done.stderr}")
    return json.loads(done.stdout)


def ordered_json(text):
    """A JSON value with every object kept as its (key, value) pairs in order: the Worker serves
    the parsed rows, so the key order matters and the number spelling (12.0 or 12) does not."""
    return None if text is None else json.loads(text, object_pairs_hook=list)


class WorkerRowsPortTest(unittest.TestCase):
    """The Worker tests build their D1 fixture with a TypeScript port of the builder's row logic
    (wiki/test/helpers/brain-d1-rows.ts), so it is compared with the builder here, column by
    column for every column the D1 store reads, on two trees: the importer's fixture tree, and
    the parity tree the Worker tests serve (wiki/test/helpers/brain-fixture-data.ts), which the
    port writes out and the real builder then builds."""

    def assert_port_matches(self, cells: Path, xref: Path, serving: Path, sidecar: Path, hot: int):
        port = run_rows_port("rows", str(cells), str(xref), str(hot))
        tables = json.loads(sidecar.read_text())
        self.assertEqual(port["longest_name"], tables["longest_name"])
        self.assertEqual(port["sup_names_from"], tables["sup_names_from"])
        db = sqlite3.connect(serving)
        self.addCleanup(db.close)
        json_columns = {"json", "syn", "cells", "sups"}
        for table, spec in port["tables"].items():
            columns = spec["columns"]
            self.assertLessEqual(set(columns), set(bdi.D1_COLUMNS[table]) | set(bdi.LOCAL_ONLY.get(table, ())))
            want = db.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY seq").fetchall()
            self.assertEqual(len(spec["rows"]), len(want), table)
            for got_row, want_row in zip(spec["rows"], want):
                for column, got, wanted in zip(columns, got_row, want_row):
                    if column in json_columns:
                        got, wanted = ordered_json(got), ordered_json(wanted)
                    self.assertEqual(got, wanted, f"{table}.{column} seq {want_row[0]}")
        if hot == 2:
            self.assertTrue(port["tables"]["hot_prefixes"]["rows"], "the small threshold made no prefix hot")
        return port

    def test_the_port_builds_the_rows_the_builder_builds(self):
        for hot in (bsd.HOT_PREFIX_NAMES, 2):      # 2 reaches the hot-prefix rows on a small tree
            with self.subTest(hot_prefix_names=hot), tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                with mock.patch.object(bsd, "HOT_PREFIX_NAMES", hot):
                    serving, sidecar = build_fixture(tmp)
                self.assert_port_matches(tmp / "cells", tmp / "xref_index.json", serving, sidecar, hot)

    def test_the_builder_builds_the_worker_parity_rows_from_the_parity_tree(self):
        # the rows the Worker's parity tests serve come from the port; here the real builder
        # builds the same tree, so a row the builder would not make (or a tree it refuses) fails
        for hot in (bsd.HOT_PREFIX_NAMES, 2):
            with self.subTest(hot_prefix_names=hot), tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                run_rows_port("parity-tree", str(tmp))
                nodes = tmp / "nodes.jsonl"
                nodes.write_text(json.dumps({"_meta": {}}) + "\n"
                                 + json.dumps({"id": "Q181296", "label": "Abelian group",
                                               "description": "group whose operation is commutative"}) + "\n")
                serving, sidecar = tmp / "serving.sqlite3", tmp / "serving-tables.json"
                with mock.patch.object(bsd, "HOT_PREFIX_NAMES", hot):
                    # writeTree writes explorer.json and the cells.jsonl _meta the builder reads
                    bsd.build(tmp / "cells", nodes, serving, sidecar_path=sidecar,
                              cells_path=tmp / "cells.jsonl")
                port = self.assert_port_matches(tmp / "cells", tmp / "xref_index.json", serving, sidecar, hot)
                # the inputs the builder's own fixture lacks are in this tree
                self.assertTrue(port["tables"]["synapse_traces"]["rows"])
                db = sqlite3.connect(serving)
                self.addCleanup(db.close)
                self.assertTrue(db.execute("SELECT count(*) FROM xrefs").fetchone()[0])
                self.assertEqual(db.execute("SELECT parent FROM supercells WHERE path = 'path:Mathlib/Topology/Order'")
                                 .fetchone(), ("path:Mathlib/Topology",))

    def test_the_fixture_creates_the_names_table_as_the_importer_does(self):
        hex_id = "0123456789abcdef" * 4
        for mode in bdi.NAMES_MODES:
            self.assertEqual(run_rows_port("fts", mode, hex_id), bdi.fts_create("names", hex_id, mode))

    def test_the_port_splits_names_as_the_importer_does(self):
        cases = ["TauCeti.StronglyContinuousSemigroup.mk", "Mathlib/Analysis/inner_product",
                 "AddCircle.fourierCoeff", "fourierCoeff", "...", "", "  lead  trail ", "HTTPServer",
                 "ÅngströmUnit", "\U0001d53dieldInner", "ℝPlus", "l2Space", "½A", "\u00e9B", "e\u0301B",
                 "x\x1fY", "a\x85B", "a\ufeffB", "a\u00a0B", "a\u3000B", "Cauchy–Schwarz", "snake_Case"]
        self.assertEqual(run_rows_port("parts", stdin=json.dumps(cases)), [bdi.name_parts(c) for c in cases])

    def test_the_exact_name_index_migration_is_one_idempotent_index(self):
        text = EXACT_INDEX_MIGRATION.read_text()
        self.assertIn("--command", text)
        self.assertIn("NEVER --file", text)
        for line in text.splitlines():
            comment = line.split("--", 1)[1] if "--" in line else ""
            self.assertNotIn("'", comment, line)
            self.assertNotIn(";", comment, line)
        self.assertEqual(split_sql(text), ["CREATE INDEX IF NOT EXISTS name_keys_exact ON name_keys(ds, name_lc, seq)"])
        fake = FakeD1()
        fake.db.executescript(MIGRATION.read_text())
        fake.db.executescript(text)
        fake.db.executescript(text)                  # a rerun changes nothing
        self.assertEqual([r[2] for r in fake.all("PRAGMA index_info(name_keys_exact)")], ["ds", "name_lc", "seq"])


if __name__ == "__main__":
    unittest.main()
