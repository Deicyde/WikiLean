#!/usr/bin/env python3
"""Hermetic contract tests for the hybrid JSONL/SQLite Brain store.

Expected public API::

    build_snapshot.build_snapshot(*, data_dir: Path, output: Path) -> Path
    build_snapshot.build_jsonl_only(*, data_dir: Path) -> str
    store.open_store(*, data_dir: Path, backend: str,
                     sqlite_path: Path | None = None) -> store

The returned store must provide ``iter_nodes()``, ``iter_edges(endpoint=None,
direction="both", kinds=None)``, ``resolve_owner(organ_id)``, and ``close()``.
When the cell layer is supported it also provides ``iter_cells()`` and
``iter_synapses(endpoint=None, kinds=None)``. ``get_node``/``get_cell`` and a
``backend`` name are useful but are not required by this contract.

Run: python3 brain/test_store.py
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

ORGAN_GENERATION = "2030-01-01T00:00:00+00:00"
CELL_GENERATION = "2030-01-01T00:01:00+00:00"
NEXT_ORGAN_GENERATION = "2030-01-02T00:00:00+00:00"
NEXT_CELL_GENERATION = "2030-01-02T00:01:00+00:00"

PROV = [
    {"source": "fixture", "method": "direct", "pin": "fixture-a"},
    {"source": "fixture-links", "method": "projected", "pin": "fixture-b"},
]

NODES = [
    {"id": "Q1", "type": "concept", "label": "Alpha"},
    {"id": "Q2", "type": "concept", "label": "Beta"},
    {"id": "Q9", "type": "concept", "label": "Field"},
    {"id": "decl:Mathlib:A", "type": "decl", "label": "A"},
    {"id": "decl:Mathlib:B", "type": "decl", "label": "B"},
    {"id": "path:Mathlib", "type": "container", "label": "Mathlib"},
]

MAIN_EDGES = [
    {"src": "Q1", "dst": "decl:Mathlib:A", "kind": "formalizes",
     "provenance": PROV[0], "confidence": "high", "evidence": {"match_kind": "exact"}},
    {"src": "decl:Mathlib:A", "dst": "decl:Mathlib:B", "kind": "depends",
     "provenance": PROV[0], "confidence": "high", "evidence": {}},
]

LINK_EDGES = [
    {"src": "Q2", "dst": "Q1", "kind": "links",
     "provenance": PROV[1], "confidence": "medium",
     "evidence": {"projected": True, "via": "fixture"}},
]

CELLS = [
    {"id": "cell:Q1", "anchor": "Q1", "label": "Alpha",
     "organs": [
         {"kind": "concept", "id": "Q1", "bond": "exact", "prov": 0},
         {"kind": "decl", "id": "decl:Mathlib:A", "bond": "exact", "prov": 0},
     ], "supercells": ["path:Mathlib"], "xy": [1.0, 2.0]},
    {"id": "cell:Q2", "anchor": "Q2", "label": "Beta",
     "organs": [
         {"kind": "concept", "id": "Q2", "bond": "exact", "prov": 0},
         {"kind": "decl", "id": "decl:Mathlib:B", "bond": "exact", "prov": 0},
     ], "supercells": ["path:Mathlib"], "xy": [3.0, 4.0]},
]

SYNAPSES = [
    {"src": "cell:Q1", "dst": "cell:Q2", "weight": 2,
     "kinds": {"depends": 1, "links": 1},
     "traces": [
         # This direction opposes the lexically sorted aggregate endpoints.
         {"kind": "depends", "src": "decl:Mathlib:B",
          "dst": "decl:Mathlib:A", "evidence": {}, "prov": 0},
         {"kind": "links", "src": "Q1", "dst": "Q2",
          "evidence": {"projected": True}, "prov": 1},
     ]},
]


def write_jsonl(path: Path, meta: dict[str, Any], rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"_meta": meta}, sort_keys=True) + "\n")
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def make_fixture(
    data_dir: Path,
    *,
    organ_generation: str = ORGAN_GENERATION,
    cell_generation: str = CELL_GENERATION,
    links_generation: str | None = None,
    synapse_generation: str | None = None,
    include_links: bool = True,
    extra_nodes: Iterable[dict[str, Any]] = (),
) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    nodes = [*NODES, *extra_nodes]
    edge_counts = {"formalizes": 1, "depends": 1, "links": len(LINK_EDGES) if include_links else 0}
    write_jsonl(data_dir / "nodes.jsonl", {
        "schema": "brain/SCHEMA.md", "generated_at": organ_generation,
        "counts": {"nodes": len(nodes)},
    }, nodes)
    write_jsonl(data_dir / "edges.jsonl", {
        "schema": "brain/SCHEMA.md", "generated_at": organ_generation,
        "counts": {"edges": edge_counts},
    }, MAIN_EDGES)
    if include_links:
        write_jsonl(data_dir / "edges_links.jsonl", {
            "schema": "brain/SCHEMA.md", "generated_at": links_generation or organ_generation,
            "split_from": "edges.jsonl", "counts": {"edges": {"links": len(LINK_EDGES)}},
        }, LINK_EDGES)
    else:
        (data_dir / "edges_links.jsonl").unlink(missing_ok=True)
    cell_meta = {
        "schema": "brain/SCHEMA.md#v3", "generated_at": cell_generation,
        "prov": PROV, "supercell_organs": {
            "path:Mathlib": [{"kind": "concept", "id": "Q9", "bond": "field", "prov": 0}],
        }, "counts": {"cells": len(CELLS), "organs": 5},
    }
    write_jsonl(data_dir / "cells.jsonl", cell_meta, CELLS)
    write_jsonl(data_dir / "synapses.jsonl", {
        "schema": "brain/SCHEMA.md#v3",
        "generated_at": synapse_generation or cell_generation,
        "prov": PROV, "counts": {"cells": len(CELLS), "synapses": len(SYNAPSES)},
    }, SYNAPSES)


def canonical(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize mappings and order, without dropping semantically meaningful fields."""
    normalized = [json.loads(json.dumps(dict(row), sort_keys=True)) for row in rows]
    return sorted(normalized, key=lambda row: json.dumps(row, sort_keys=True))


def call_iter(store: Any, name: str, **kwargs: Any) -> list[dict[str, Any]]:
    method = getattr(store, name, None)
    if not callable(method):
        raise AssertionError(f"store must implement {name}(...) for the hybrid contract")
    return [dict(row) for row in method(**kwargs)]


def close_store(store: Any) -> None:
    close = getattr(store, "close", None)
    if callable(close):
        close()


class HybridStoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.store_module = importlib.import_module("store")
            cls.snapshot_module = importlib.import_module("build_snapshot")
        except ImportError as exc:
            raise RuntimeError(
                "hybrid store API is not ready; expected brain/store.py and "
                "brain/build_snapshot.py with the calls documented at the top of "
                "brain/test_store.py"
            ) from exc
        open_store = getattr(cls.store_module, "open_store", None)
        build_snapshot = getattr(cls.snapshot_module, "build_snapshot", None)
        if not callable(open_store) or not callable(build_snapshot):
            raise RuntimeError(
                "expected open_store(data_dir=..., backend=..., sqlite_path=...) and "
                "build_snapshot(data_dir=..., output=...)"
            )
        cls.open_store = staticmethod(open_store)
        cls.build_snapshot = staticmethod(build_snapshot)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data_dir = self.root / "data"
        self.db_path = self.root / "brain.sqlite"
        make_fixture(self.data_dir)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def build(self) -> Path:
        result = self.build_snapshot(data_dir=self.data_dir, output=self.db_path)
        self.assertTrue(self.db_path.exists(), "build_snapshot did not create output")
        return Path(result) if result is not None else self.db_path

    def test_jsonl_only_matches_default_bytes_without_touching_sqlite(self) -> None:
        jsonl_only_dir = self.root / "jsonl-only"
        default_dir = self.root / "default"
        expected_dir = self.root / "expected-without-snapshot-id"
        jsonl_only_dir.mkdir()
        expected_dir.mkdir()

        sqlite_sentinel = jsonl_only_dir / self.snapshot_module.DEFAULT_SQLITE_NAME
        sqlite_sentinel.write_bytes(b"existing sqlite must remain byte-identical")
        sentinel_stat = sqlite_sentinel.stat()

        meta = {
            "schema": "brain/SCHEMA.md",
            "generated_at": ORGAN_GENERATION,
            "counts": {"nodes": 2, "edges": {"formalizes": 1, "links": 1}},
        }
        nodes = [NODES[0], NODES[3]]
        edges = [MAIN_EDGES[0], LINK_EDGES[0]]

        expected_nodes = expected_dir / "nodes.jsonl"
        expected_edges = expected_dir / "edges.jsonl"
        expected_links = expected_dir / "edges_links.jsonl"
        self.snapshot_module.write_jsonl(expected_nodes, meta, nodes)
        self.snapshot_module.write_edges(
            edges, meta, expected_edges, expected_links,
        )
        identity = hashlib.sha256()
        for path in (expected_nodes, expected_edges, expected_links):
            identity.update(self.snapshot_module.digest_file(path).encode("ascii"))
        expected_snapshot_id = identity.hexdigest()

        def fake_sqlite_builder(output: Path, _data_dir: Path) -> Path:
            output = Path(output)
            output.write_bytes(b"sqlite projection")
            return output

        with (
            mock.patch.object(
                self.snapshot_module,
                "build",
                side_effect=[(nodes, edges, meta), (nodes, edges, meta)],
            ),
            mock.patch.object(
                self.snapshot_module,
                "write_sqlite_from_jsonl",
                side_effect=fake_sqlite_builder,
            ) as sqlite_builder,
        ):
            snapshot_id = self.snapshot_module.build_jsonl_only(
                data_dir=jsonl_only_dir,
            )
            default_db = default_dir / self.snapshot_module.DEFAULT_SQLITE_NAME
            result = self.snapshot_module.build_live_snapshot(
                data_dir=default_dir,
                output=default_db,
            )

        self.assertEqual(snapshot_id, expected_snapshot_id)
        self.assertEqual(result, default_db)
        self.assertEqual(sqlite_builder.call_count, 1)
        self.assertEqual(sqlite_sentinel.read_bytes(),
                         b"existing sqlite must remain byte-identical")
        self.assertEqual(sqlite_sentinel.stat().st_ino, sentinel_stat.st_ino)
        self.assertEqual(sqlite_sentinel.stat().st_mtime_ns, sentinel_stat.st_mtime_ns)

        for name in ("nodes.jsonl", "edges.jsonl", "edges_links.jsonl"):
            jsonl_only_bytes = (jsonl_only_dir / name).read_bytes()
            self.assertEqual(jsonl_only_bytes, (default_dir / name).read_bytes())
            metadata = json.loads(jsonl_only_bytes.splitlines()[0])["_meta"]
            self.assertEqual(metadata["snapshot_id"], expected_snapshot_id)

    def test_jsonl_only_cli_dispatches_without_sqlite_builder(self) -> None:
        snapshot_id = "a" * 64
        with (
            mock.patch.object(
                self.snapshot_module, "build_jsonl_only", return_value=snapshot_id,
            ) as jsonl_builder,
            mock.patch.object(self.snapshot_module, "build_live_snapshot") as live_builder,
            mock.patch.object(self.snapshot_module, "build_snapshot") as sqlite_builder,
            mock.patch.object(
                sys,
                "argv",
                [
                    "build_snapshot.py",
                    "--data-dir",
                    str(self.data_dir),
                    "--jsonl-only",
                ],
            ),
            mock.patch("builtins.print") as output,
        ):
            self.assertEqual(self.snapshot_module.main(), 0)

        jsonl_builder.assert_called_once_with(data_dir=self.data_dir)
        live_builder.assert_not_called()
        sqlite_builder.assert_not_called()
        payload = json.loads(output.call_args.args[0])
        self.assertEqual(payload, {
            "data_dir": str(self.data_dir),
            "snapshot_id": snapshot_id,
        })

    def test_jsonl_only_creates_a_fresh_nested_data_root(self) -> None:
        data_dir = self.root / "fresh" / "nested" / "data"
        meta = {
            "schema": "brain/SCHEMA.md",
            "generated_at": ORGAN_GENERATION,
            "counts": {"nodes": 1, "edges": {"formalizes": 1}},
        }
        with (
            mock.patch.object(
                self.snapshot_module,
                "build",
                return_value=([NODES[0]], [MAIN_EDGES[0]], meta),
            ),
            mock.patch.object(
                self.snapshot_module, "write_sqlite_from_jsonl",
            ) as sqlite_builder,
        ):
            snapshot_id = self.snapshot_module.build_jsonl_only(data_dir=data_dir)

        self.assertRegex(snapshot_id, r"^[0-9a-f]{64}$")
        self.assertEqual(
            {path.name for path in data_dir.iterdir()},
            {"nodes.jsonl", "edges.jsonl", "edges_links.jsonl"},
        )
        sqlite_builder.assert_not_called()

    def open(self, backend: str):
        return self.open_store(data_dir=self.data_dir, backend=backend,
                               sqlite_path=self.db_path)

    def assert_rejected(self, fn, message: str) -> None:
        with self.assertRaises(Exception, msg=message):
            fn()

    def test_jsonl_sqlite_parity_and_edge_partition(self) -> None:
        self.build()
        jsonl = self.open("jsonl")
        sqlite = self.open("sqlite")
        try:
            self.assertEqual(canonical(call_iter(jsonl, "iter_nodes")),
                             canonical(call_iter(sqlite, "iter_nodes")))
            all_jsonl = canonical(call_iter(jsonl, "iter_edges"))
            all_sqlite = canonical(call_iter(sqlite, "iter_edges"))
            self.assertEqual(all_jsonl, all_sqlite)
            self.assertEqual({row["kind"] for row in all_sqlite},
                             {"formalizes", "depends", "links"})
            self.assertEqual([row["kind"] for row in call_iter(
                sqlite, "iter_edges", kinds={"links"})], ["links"])
            self.assertNotIn("links", {row["kind"] for row in call_iter(
                sqlite, "iter_edges", kinds={"formalizes", "depends"})})
        finally:
            close_store(jsonl)
            close_store(sqlite)

    def test_snapshot_aware_base_allows_missing_optional_links(self) -> None:
        make_fixture(self.data_dir)
        for name in ("nodes.jsonl", "edges.jsonl"):
            path = self.data_dir / name
            lines = path.read_text(encoding="utf-8").splitlines()
            meta = json.loads(lines[0])
            meta["_meta"]["snapshot_id"] = "snapshot-a"
            path.write_text(json.dumps(meta) + "\n" + "\n".join(lines[1:]) + "\n",
                            encoding="utf-8")
        for name in ("cells.jsonl", "synapses.jsonl"):
            path = self.data_dir / name
            lines = path.read_text(encoding="utf-8").splitlines()
            meta_row = json.loads(lines[0])
            meta_row["_meta"]["base_generated_at"] = ORGAN_GENERATION
            meta_row["_meta"]["base_snapshot_id"] = "snapshot-a"
            path.write_text(json.dumps(meta_row) + "\n" + "\n".join(lines[1:]) + "\n",
                            encoding="utf-8")
        (self.data_dir / "edges_links.jsonl").unlink()
        self.build()
        with closing(sqlite3.connect(self.db_path)) as conn:
            snapshot_id, base_snapshot_id, projection_id = conn.execute(
                "SELECT snapshot_id, base_snapshot_id, projection_id FROM snapshot"
            ).fetchone()
        self.assertEqual(snapshot_id, "snapshot-a")
        self.assertEqual(base_snapshot_id, "snapshot-a")
        self.assertNotEqual(projection_id, "snapshot-a")
        sqlite_store = self.open("sqlite")
        try:
            self.assertEqual(call_iter(sqlite_store, "iter_edges"), MAIN_EDGES)
        finally:
            close_store(sqlite_store)

    def test_jsonl_store_pins_validated_files_across_rename(self) -> None:
        store = self.open("jsonl")
        path = self.data_dir / "nodes.jsonl"
        replacement = self.data_dir / "nodes.next"
        write_jsonl(
            replacement,
            {"schema": "brain/SCHEMA.md", "generated_at": ORGAN_GENERATION},
            NODES + [{"id": "Q999", "type": "concept"}],
        )
        replacement.replace(path)
        try:
            self.assertEqual(call_iter(store, "iter_nodes"), NODES)
        finally:
            close_store(store)

    def test_missing_links_partition_is_valid_and_empty(self) -> None:
        make_fixture(self.data_dir, include_links=False)
        self.build()
        with closing(sqlite3.connect(self.db_path)) as conn:
            source = conn.execute(
                "SELECT source_present, raw_digest, source_digest "
                "FROM artifacts WHERE name = 'edges_links'"
            ).fetchone()
        self.assertEqual(source, (0, None, None))
        for backend in ("jsonl", "sqlite"):
            opened = self.open(backend)
            try:
                self.assertEqual(call_iter(opened, "iter_edges", kinds={"links"}), [])
                self.assertEqual(len(call_iter(opened, "iter_edges")), len(MAIN_EDGES))
            finally:
                close_store(opened)

    def test_directed_edge_traversal_and_kind_filter(self) -> None:
        self.build()
        for backend in ("jsonl", "sqlite"):
            opened = self.open(backend)
            try:
                outgoing = call_iter(opened, "iter_edges", endpoint="Q1", direction="out")
                incoming = call_iter(opened, "iter_edges", endpoint="Q1", direction="in")
                self.assertEqual({(row["kind"], row["dst"]) for row in outgoing},
                                 {("formalizes", "decl:Mathlib:A")})
                self.assertEqual({(row["kind"], row["src"]) for row in incoming},
                                 {("links", "Q2")})
                self.assertEqual(call_iter(opened, "iter_edges", endpoint="Q1",
                                           direction="in", kinds={"depends"}), [])
            finally:
                close_store(opened)

    def test_endpoint_kind_query_preserves_order_and_deduplicates_self_loops(self) -> None:
        main_edges = [
            {"src": "Q1", "dst": "Q1", "kind": "depends"},
            {"src": "Q1", "dst": "Q2", "kind": "depends"},
            {"src": "Q2", "dst": "Q1", "kind": "formalizes"},
            {"src": "Q2", "dst": "Q1", "kind": "depends"},
        ]
        link_edges = [
            {"src": "Q1", "dst": "Q9", "kind": "links"},
            {"src": "Q9", "dst": "Q1", "kind": "links"},
            {"src": "Q1", "dst": "Q1", "kind": "links"},
        ]
        base_meta = {
            "schema": "brain/SCHEMA.md",
            "generated_at": ORGAN_GENERATION,
        }
        write_jsonl(self.data_dir / "edges.jsonl", base_meta, main_edges)
        write_jsonl(
            self.data_dir / "edges_links.jsonl",
            {**base_meta, "split_from": "edges.jsonl"},
            link_edges,
        )
        self.build()
        jsonl = self.open("jsonl")
        sqlite_store = self.open("sqlite")
        try:
            cases = (
                {"endpoint": "Q1", "direction": "both"},
                {
                    "endpoint": "Q1",
                    "direction": "both",
                    "kinds": {"depends", "links"},
                },
                {
                    "endpoint": "Q1",
                    "direction": "both",
                    "kinds": {"links"},
                    "stream": "links",
                },
                {"endpoint": "Q1", "direction": "out", "kinds": {"depends"}},
                {"endpoint": "Q1", "direction": "in", "kinds": {"depends"}},
            )
            for kwargs in cases:
                self.assertEqual(
                    call_iter(sqlite_store, "iter_edges", **kwargs),
                    call_iter(jsonl, "iter_edges", **kwargs),
                )
            both = call_iter(
                sqlite_store,
                "iter_edges",
                endpoint="Q1",
                direction="both",
                kinds={"depends", "links"},
            )
            self.assertEqual(both, [main_edges[0], main_edges[1], main_edges[3], *link_edges])
            self.assertEqual(both.count(main_edges[0]), 1)
            self.assertEqual(both.count(link_edges[2]), 1)
        finally:
            close_store(jsonl)
            close_store(sqlite_store)

    def test_cells_synapses_and_owner_parity_when_supported(self) -> None:
        self.build()
        jsonl = self.open("jsonl")
        sqlite = self.open("sqlite")
        try:
            self.assertEqual(jsonl.resolve_owner("Q1"), "cell:Q1")
            self.assertEqual(sqlite.resolve_owner("decl:Mathlib:A"), "cell:Q1")
            self.assertEqual(jsonl.resolve_owner("Q9"), "path:Mathlib")
            self.assertEqual(sqlite.resolve_owner("Q9"), "path:Mathlib")
            self.assertIsNone(sqlite.resolve_owner("Q404"))

            if not all(callable(getattr(s, "iter_cells", None)) for s in (jsonl, sqlite)):
                self.skipTest("iter_cells is optional until the cell store surface lands")
            self.assertEqual(canonical(call_iter(jsonl, "iter_cells")),
                             canonical(call_iter(sqlite, "iter_cells")))

            if not all(callable(getattr(s, "iter_synapses", None)) for s in (jsonl, sqlite)):
                self.skipTest("iter_synapses is optional until the cell store surface lands")
            self.assertEqual(canonical(call_iter(jsonl, "iter_synapses")),
                             canonical(call_iter(sqlite, "iter_synapses")))
            depends = call_iter(sqlite, "iter_synapses", endpoint="cell:Q1",
                                kinds={"depends"})
            self.assertEqual(len(depends), 1)
            self.assertEqual({trace["kind"] for trace in depends[0]["traces"]}, {"depends"})
            # Aggregate src/dst are sorted; trace src/dst preserve the reverse direction.
            self.assertEqual(depends[0]["src"], "cell:Q1")
            self.assertEqual(depends[0]["dst"], "cell:Q2")
            self.assertEqual(depends[0]["traces"][0]["src"], "decl:Mathlib:B")
            self.assertEqual(depends[0]["traces"][0]["dst"], "decl:Mathlib:A")
        finally:
            close_store(jsonl)
            close_store(sqlite)

    def test_streaming_builder_indexes_bare_decl_for_supercell_owner(self) -> None:
        cells_path = self.data_dir / "cells.jsonl"
        metadata, cells = self.store_module.read_jsonl(cells_path)
        metadata["supercell_organs"]["path:Mathlib"].append(
            {
                "kind": "decl",
                "id": "decl:Mathlib:Fallback",
                "bond": "field",
                "prov": 0,
            }
        )
        write_jsonl(cells_path, metadata, cells)

        self.build()
        with closing(sqlite3.connect(self.db_path)) as connection:
            owner = connection.execute(
                "SELECT owner_id, organ_kind, bare_decl FROM organ_owners "
                "WHERE organ_id = ?",
                ("decl:Mathlib:Fallback",),
            ).fetchone()
        self.assertEqual(owner, ("path:Mathlib", "decl", "Fallback"))

    def test_consistent_layers_may_have_different_generations(self) -> None:
        # Organ artifacts and cell artifacts are built in different pipeline phases.
        self.assertNotEqual(ORGAN_GENERATION, CELL_GENERATION)
        self.build()
        opened = self.open("sqlite")
        try:
            self.assertEqual(len(call_iter(opened, "iter_nodes")), len(NODES))
            if callable(getattr(opened, "iter_cells", None)):
                self.assertEqual(len(call_iter(opened, "iter_cells")), len(CELLS))
        finally:
            close_store(opened)

    def test_mixed_source_generations_are_rejected(self) -> None:
        make_fixture(self.data_dir, links_generation=NEXT_ORGAN_GENERATION)
        self.assert_rejected(self.build, "main and links edge generations must match")

        make_fixture(self.data_dir, synapse_generation=NEXT_CELL_GENERATION)
        self.assert_rejected(self.build, "cell and synapse generations must match")

        make_fixture(self.data_dir)
        edges = self.data_dir / "edges.jsonl"
        rows = edges.read_text(encoding="utf-8").splitlines()
        meta = json.loads(rows[0])
        meta["_meta"]["generated_at"] = NEXT_ORGAN_GENERATION
        edges.write_text(json.dumps(meta) + "\n" + "\n".join(rows[1:]) + "\n",
                         encoding="utf-8")
        self.assert_rejected(self.build, "node and main edge generations must match")

    def test_stale_snapshot_falls_back_without_mixing_generations(self) -> None:
        self.build()
        snapshot_only = {"id": "QOLD", "type": "concept", "label": "old snapshot row"}
        # Put a sentinel in generation A and rebuild it into SQLite.
        make_fixture(self.data_dir, extra_nodes=[snapshot_only])
        self.build()
        fresh_only = {"id": "QNEW", "type": "concept", "label": "fresh JSONL row"}
        make_fixture(self.data_dir, organ_generation=NEXT_ORGAN_GENERATION,
                     cell_generation=NEXT_CELL_GENERATION, extra_nodes=[fresh_only])

        self.assert_rejected(lambda: self.open("sqlite"),
                             "forced sqlite must reject a stale snapshot")
        automatic = self.open("auto")
        try:
            ids = {row["id"] for row in call_iter(automatic, "iter_nodes")}
            self.assertIn("QNEW", ids)
            self.assertNotIn("QOLD", ids, "auto mode mixed stale SQLite with fresh JSONL")
        finally:
            close_store(automatic)

    def test_auto_falls_back_when_sqlite_path_is_not_a_database(self) -> None:
        self.db_path.mkdir()
        automatic = self.open("auto")
        try:
            self.assertEqual(len(call_iter(automatic, "iter_nodes")), len(NODES))
        finally:
            close_store(automatic)
        self.assert_rejected(lambda: self.open("sqlite"),
                             "forced sqlite must report an invalid database path")

    def test_application_id_rejects_an_unrecognized_sqlite_database(self) -> None:
        self.build()
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("PRAGMA application_id = 0")
        with self.assertRaisesRegex(Exception, "not a Brain SQLite database"):
            self.open("sqlite")

    def test_absent_or_incomplete_snapshot_falls_back_but_forced_sqlite_rejects(self) -> None:
        automatic = self.open("auto")
        try:
            self.assertEqual({row["id"] for row in call_iter(automatic, "iter_nodes")},
                             {row["id"] for row in NODES})
        finally:
            close_store(automatic)
        self.assert_rejected(lambda: self.open("sqlite"),
                             "forced sqlite must reject an absent snapshot")

        with closing(sqlite3.connect(self.db_path)):
            pass
        self.assert_rejected(lambda: self.open("sqlite"),
                             "a valid but empty SQLite file is not a complete snapshot")
        automatic = self.open("auto")
        try:
            self.assertEqual(len(call_iter(automatic, "iter_nodes")), len(NODES))
        finally:
            close_store(automatic)

    def test_snapshot_aware_base_rejects_unpinned_derived_layer(self) -> None:
        make_fixture(self.data_dir)
        for name in ("nodes.jsonl", "edges.jsonl", "edges_links.jsonl"):
            path = self.data_dir / name
            lines = path.read_text(encoding="utf-8").splitlines()
            meta_row = json.loads(lines[0])
            meta_row["_meta"]["snapshot_id"] = "snapshot-a"
            path.write_text(json.dumps(meta_row) + "\n" + "\n".join(lines[1:]) + "\n",
                            encoding="utf-8")
        self.assert_rejected(self.build, "derived layer must name its base snapshot")

    def test_snapshot_ids_must_match_when_present(self) -> None:
        make_fixture(self.data_dir)
        for name, snapshot_id in (("nodes.jsonl", "snapshot-a"),
                                  ("edges.jsonl", "snapshot-b"),
                                  ("edges_links.jsonl", "snapshot-a")):
            path = self.data_dir / name
            lines = path.read_text(encoding="utf-8").splitlines()
            meta = json.loads(lines[0])
            meta["_meta"]["snapshot_id"] = snapshot_id
            path.write_text(json.dumps(meta) + "\n" + "\n".join(lines[1:]) + "\n",
                            encoding="utf-8")
        self.assert_rejected(self.build, "mixed snapshot IDs must fail")

    def test_query_relevant_indexes_exist(self) -> None:
        self.build()
        with closing(sqlite3.connect(self.db_path)) as conn:
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
            analyzed = {
                row[0]
                for row in conn.execute("SELECT idx FROM sqlite_stat1 WHERE idx IS NOT NULL")
            }
        self.assertTrue({"edges_src_kind_idx", "edges_dst_kind_idx",
                         "synapses_src_idx", "synapses_dst_idx",
                         "organ_owners_bare_decl_idx"} <= indexes)
        self.assertTrue(
            {"edges_src_kind_idx", "edges_dst_kind_idx"} <= analyzed,
            "builder must persist ANALYZE statistics for endpoint planning",
        )

        sqlite_store = self.open("sqlite")
        try:
            sql, _ = sqlite_store._edge_query(
                endpoint="Q1",
                direction="both",
                kinds={"depends", "formalizes"},
            )
            details = sqlite_store.edge_query_plan(
                endpoint="Q1",
                direction="both",
                kinds={"depends", "formalizes"},
            )
        finally:
            close_store(sqlite_store)
        self.assertIn("UNION ALL", sql)
        self.assertTrue(any("edges_src_kind_idx" in detail for detail in details))
        self.assertTrue(any("edges_dst_kind_idx" in detail for detail in details))
        self.assertFalse(any(detail == "SCAN edges" for detail in details), details)

    def test_both_sqlite_builders_persist_analyze_statistics(self) -> None:
        self.build()
        direct_path = self.root / "direct.sqlite"
        artifacts = self.store_module.load_artifacts(self.data_dir)
        raw_digests = {
            name: self.store_module.digest_file(
                self.data_dir / self.store_module.DEFAULT_ARTIFACT_FILES[name]
            )
            for name in artifacts
        }
        self.store_module.write_sqlite_snapshot(
            direct_path, artifacts, source_digests=raw_digests
        )
        for path in (self.db_path, direct_path):
            with closing(sqlite3.connect(path)) as conn:
                stats = conn.execute("SELECT count(*) FROM sqlite_stat1").fetchone()[0]
            self.assertGreater(stats, 0, f"ANALYZE statistics missing from {path.name}")

    def test_schema_v2_separates_base_projection_and_raw_logical_digests(self) -> None:
        self.build()

        def identities() -> tuple[str, str, str]:
            with closing(sqlite3.connect(self.db_path)) as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertEqual(
                    conn.execute("PRAGMA application_id").fetchone()[0],
                    self.store_module.APPLICATION_ID,
                )
                snapshot = conn.execute(
                    "SELECT snapshot_id, base_snapshot_id, projection_id FROM snapshot"
                ).fetchone()
                rows = list(conn.execute(
                    "SELECT name, digest, source_digest, logical_digest, raw_digest, "
                    "source_present FROM artifacts ORDER BY name"
                ))
            self.assertEqual(snapshot[0], snapshot[1])
            logical = self.store_module.artifact_digests(
                self.store_module.load_artifacts(self.data_dir)
            )
            for name, digest, source, logical_digest, raw, present in rows:
                self.assertEqual(digest, logical_digest)
                self.assertEqual(logical_digest, logical[name])
                self.assertEqual(source, raw)
                self.assertEqual(present, 1)
                self.assertEqual(
                    raw,
                    self.store_module.digest_file(
                        self.data_dir / self.store_module.DEFAULT_ARTIFACT_FILES[name]
                    ),
                )
            return snapshot

        before = identities()
        cell_path = self.data_dir / "cells.jsonl"
        cell_meta, cells = self.store_module.read_jsonl(cell_path)
        cells[0] = {**cells[0], "label": "Alpha revised"}
        write_jsonl(cell_path, cell_meta, cells)
        self.build()
        after = identities()
        self.assertEqual(before[1], after[1], "derived changes altered base identity")
        self.assertNotEqual(
            before[2], after[2], "derived changes did not alter projection identity"
        )

    def test_builder_rejects_source_replacement_and_preserves_database(self) -> None:
        self.build()
        before = hashlib.sha256(self.db_path.read_bytes()).digest()
        original = self.store_module._inspect_source_handle
        replaced = False

        def replace_after_inspection(handle, path):
            nonlocal replaced
            result = original(handle, path)
            if path.name == "nodes.jsonl" and not replaced:
                replacement = path.with_suffix(".next")
                write_jsonl(
                    replacement,
                    result[0],
                    [*NODES, {"id": "Q999", "type": "concept"}],
                )
                replacement.replace(path)
                replaced = True
            return result

        with mock.patch.object(
            self.store_module,
            "_inspect_source_handle",
            side_effect=replace_after_inspection,
        ):
            with self.assertRaisesRegex(Exception, "replaced while building SQLite"):
                self.build()
        self.assertEqual(hashlib.sha256(self.db_path.read_bytes()).digest(), before)

    def test_atomic_publish_fsyncs_database_and_parent_directory(self) -> None:
        kinds: list[str] = []
        real_fsync = os.fsync

        def observed_fsync(fd: int) -> None:
            mode = os.fstat(fd).st_mode
            kinds.append("directory" if stat.S_ISDIR(mode) else "file")
            real_fsync(fd)

        with mock.patch.object(
            self.store_module.os, "fsync", side_effect=observed_fsync
        ):
            self.build()
        self.assertEqual(kinds[-2:], ["file", "directory"])

    def test_failed_rebuild_preserves_previous_database(self) -> None:
        self.build()
        before = hashlib.sha256(self.db_path.read_bytes()).digest()
        synapses = self.data_dir / "synapses.jsonl"
        with synapses.open("a", encoding="utf-8") as fh:
            fh.write('{"src":"cell:late"')
        self.assert_rejected(self.build, "malformed late input must fail the rebuild")
        self.assertEqual(hashlib.sha256(self.db_path.read_bytes()).digest(), before,
                         "failed rebuild replaced the previous good database")
        # The preserved snapshot is stale only because the source was made invalid;
        # validate it directly at the SQLite level without invoking freshness policy.
        with closing(sqlite3.connect(self.db_path)) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()
        self.assertEqual(result, ("ok",))


def main() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(HybridStoreTest)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
