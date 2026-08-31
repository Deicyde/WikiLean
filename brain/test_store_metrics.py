#!/usr/bin/env python3
"""Shape and query-plan tests for tools/measure_store.py.

Run: python3 brain/test_store_metrics.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


HERE = Path(__file__).resolve().parent
MODULE_PATH = HERE / "tools" / "measure_store.py"
SPEC = importlib.util.spec_from_file_location("measure_store", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load {MODULE_PATH}")
measure_store = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(measure_store)


def make_database(path: Path, *, embedded_release: str | None = None) -> None:
    connection = sqlite3.connect(path)
    release_column = "release_id TEXT," if embedded_release is not None else ""
    connection.executescript(
        f"""
        PRAGMA application_id = {measure_store.BRAIN_APPLICATION_ID};
        PRAGMA user_version = 2;
        CREATE TABLE snapshot (
          singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
          schema_version INTEGER NOT NULL,
          build_state TEXT NOT NULL,
          snapshot_id TEXT NOT NULL,
          base_snapshot_id TEXT NOT NULL,
          projection_id TEXT NOT NULL,
          {release_column}
          metadata_json TEXT NOT NULL
        );
        CREATE TABLE artifacts (
          name TEXT PRIMARY KEY,
          row_count INTEGER NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE nodes (
          ordinal INTEGER PRIMARY KEY,
          id TEXT NOT NULL UNIQUE,
          type TEXT NOT NULL,
          label TEXT,
          payload_json TEXT NOT NULL
        );
        CREATE TABLE edges (
          stream TEXT NOT NULL,
          ordinal INTEGER NOT NULL,
          src TEXT NOT NULL,
          dst TEXT NOT NULL,
          kind TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          PRIMARY KEY (stream, ordinal)
        ) WITHOUT ROWID;
        CREATE INDEX edges_src_kind_idx ON edges(src, kind);
        CREATE INDEX edges_dst_kind_idx ON edges(dst, kind);
        CREATE TABLE cells (
          ordinal INTEGER PRIMARY KEY,
          id TEXT NOT NULL UNIQUE,
          payload_json TEXT NOT NULL
        );
        CREATE TABLE organ_owners (
          organ_id TEXT PRIMARY KEY,
          owner_id TEXT NOT NULL,
          organ_kind TEXT,
          bare_decl TEXT
        ) WITHOUT ROWID;
        CREATE INDEX organ_owners_owner_idx ON organ_owners(owner_id);
        CREATE TABLE synapses (
          ordinal INTEGER PRIMARY KEY,
          src TEXT NOT NULL,
          dst TEXT NOT NULL,
          weight INTEGER NOT NULL,
          payload_json TEXT NOT NULL,
          UNIQUE(src, dst)
        );
        CREATE INDEX synapses_src_idx ON synapses(src);
        CREATE INDEX synapses_dst_idx ON synapses(dst);

        INSERT INTO artifacts VALUES ('nodes', 3);
        INSERT INTO artifacts VALUES ('edges', 3);
        INSERT INTO artifacts VALUES ('cells', 2);
        INSERT INTO artifacts VALUES ('synapses', 2);
        INSERT INTO nodes VALUES (0, 'Q1', 'concept', 'One', '{{"id":"Q1"}}');
        INSERT INTO nodes VALUES (1, 'Q2', 'concept', 'Two', '{{"id":"Q2"}}');
        INSERT INTO nodes VALUES (2, 'Q3', 'concept', 'Three', '{{"id":"Q3"}}');
        INSERT INTO edges VALUES ('main', 0, 'Q1', 'Q2', 'depends', '{{}}');
        INSERT INTO edges VALUES ('main', 1, 'Q3', 'Q1', 'depends', '{{}}');
        INSERT INTO edges VALUES ('links', 0, 'Q1', 'Q1', 'links', '{{}}');
        INSERT INTO cells VALUES (0, 'cell:Q1', '{{}}');
        INSERT INTO cells VALUES (1, 'cell:Q2', '{{}}');
        INSERT INTO organ_owners VALUES ('Q1', 'cell:Q1', 'concept', NULL);
        INSERT INTO organ_owners VALUES (
          'decl:Mathlib:One', 'cell:Q1', 'decl', 'One'
        );
        INSERT INTO synapses VALUES (0, 'cell:Q1', 'cell:Q2', 2, '{{}}');
        INSERT INTO synapses VALUES (1, 'cell:Q2', 'cell:Q1', 3, '{{}}');
        ANALYZE;
        """
    )
    columns = (
        "singleton, schema_version, build_state, snapshot_id, "
        "base_snapshot_id, projection_id, "
        + ("release_id, " if embedded_release is not None else "")
        + "metadata_json"
    )
    values: list[object] = [
        1,
        2,
        "complete",
        "base-1",
        "base-1",
        "projection-1",
    ]
    if embedded_release is not None:
        values.append(embedded_release)
    values.append("{}")
    placeholders = ",".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO snapshot ({columns}) VALUES ({placeholders})", values
    )
    connection.commit()
    connection.close()


class StoreMetricsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "brain.sqlite3"
        make_database(self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def measure(self, **kwargs: object) -> dict[str, object]:
        return measure_store.measure_database(
            self.database,
            iterations=3,
            warmup=0,
            limit=2,
            check_limit=5,
            **kwargs,
        )

    def test_report_shape_counts_checks_and_analyze_stats(self) -> None:
        report = self.measure(release_id="sha256:release")

        self.assertTrue(report["ok"])
        self.assertEqual(report["schema"], measure_store.REPORT_SCHEMA)
        self.assertIsInstance(report["duration_ms"], float)
        self.assertGreaterEqual(report["duration_ms"], 0)
        self.assertIsInstance(report["max_rss_bytes"], int)
        self.assertGreater(report["max_rss_bytes"], 0)
        self.assertEqual(report["identity"]["release_id"], "sha256:release")
        self.assertEqual(report["identity"]["base_snapshot_id"], "base-1")
        self.assertEqual(report["identity"]["projection_id"], "projection-1")
        self.assertTrue(report["identity"]["snapshot_aliases_base"])
        self.assertEqual(
            report["identity"]["snapshot_id_alias"], "base_snapshot_id"
        )

        database = report["database"]
        self.assertEqual(database["application_id"], measure_store.BRAIN_APPLICATION_ID)
        self.assertEqual(database["user_version"], 2)
        self.assertTrue(database["read_only"])
        self.assertTrue(database["immutable"])
        self.assertTrue(database["query_only"])
        self.assertGreater(database["page_size_bytes"], 0)
        self.assertEqual(
            database["allocated_bytes"],
            database["page_size_bytes"] * database["page_count"],
        )
        self.assertEqual(
            database["used_pages"],
            database["page_count"] - database["freelist_pages"],
        )

        self.assertEqual(report["counts"]["tables"]["nodes"], 3)
        self.assertEqual(report["counts"]["tables"]["edges"], 3)
        self.assertEqual(report["counts"]["artifacts"]["nodes"], 3)
        self.assertEqual(report["counts"]["edges_by_stream"], {"links": 1, "main": 2})
        self.assertTrue(report["analyze"]["present"])
        self.assertGreater(report["analyze"]["entry_count"], 0)
        self.assertIn("edges_src_kind_idx", report["analyze"]["indexes"])
        self.assertEqual(report["checks"]["quick_check"]["messages"], ["ok"])
        self.assertEqual(report["checks"]["integrity_check"]["messages"], ["ok"])

    def test_bounded_probes_expose_latency_shapes_and_indexed_plans(self) -> None:
        queries = self.measure(release_id="sha256:release")["queries"]
        self.assertEqual(
            set(queries),
            {"owner_lookup", "edge_neighborhood", "synapse_neighborhood"},
        )
        for probe in queries.values():
            self.assertEqual(probe["status"], "ok")
            self.assertEqual(probe["limit"], 2)
            self.assertEqual(probe["iterations"], 3)
            self.assertLessEqual(probe["rows_returned"], probe["limit"])
            self.assertTrue(probe["plan"])
            self.assertEqual(
                set(probe["latency_ms"]), {"min", "p50", "p95", "mean", "max"}
            )
            self.assertTrue(
                all(value >= 0 for value in probe["latency_ms"].values())
            )
            self.assertFalse(probe["plan_summary"]["base_table_full_scans"])

        owner_details = " ".join(
            row["detail"] for row in queries["owner_lookup"]["plan"]
        )
        self.assertIn("SEARCH organ_owners", owner_details)
        self.assertEqual(
            queries["edge_neighborhood"]["plan_summary"]["used_expected_indexes"],
            ["edges_src_kind_idx", "edges_dst_kind_idx"],
        )
        self.assertEqual(queries["edge_neighborhood"]["sample_key"], "Q1|depends")
        self.assertEqual(
            queries["synapse_neighborhood"]["plan_summary"]["used_expected_indexes"],
            ["synapses_src_idx", "synapses_dst_idx"],
        )

    def test_connection_uses_read_only_immutable_uri(self) -> None:
        with mock.patch.object(
            measure_store.sqlite3, "connect", wraps=sqlite3.connect
        ) as connect:
            self.measure(release_id="sha256:release")
        uri = connect.call_args.args[0]
        self.assertIn("mode=ro", uri)
        self.assertIn("immutable=1", uri)
        self.assertTrue(connect.call_args.kwargs["uri"])

    def test_additive_snapshot_release_id_is_discovered(self) -> None:
        database = self.root / "future.sqlite3"
        make_database(database, embedded_release="sha256:embedded")
        report = measure_store.measure_database(
            database, iterations=1, warmup=0, limit=1, check_limit=5
        )
        self.assertEqual(report["identity"]["release_id"], "sha256:embedded")
        self.assertEqual(
            report["identity"]["release_id_source"], "snapshot.release_id"
        )

    def test_cli_emits_one_json_document(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = measure_store.main(
                [
                    "--database",
                    str(self.database),
                    "--release-id",
                    "sha256:release",
                    "--iterations",
                    "1",
                    "--warmup",
                    "0",
                    "--limit",
                    "1",
                    "--check-limit",
                    "5",
                ]
            )
        self.assertEqual(code, 0)
        document = json.loads(output.getvalue())
        self.assertTrue(document["ok"])
        self.assertEqual(document["schema"], measure_store.REPORT_SCHEMA)


if __name__ == "__main__":
    unittest.main()
